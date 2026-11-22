#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表格模型

节点 / 日志 / 连接三个表格模型与延迟进度条绘制代理（筛选排序在节点代理模型里）。
"""

from __future__ import annotations

import time

from typing import (
    Any,
    Callable,
    Optional,
    Sequence,
)
from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractProxyModel,
    QAbstractTableModel,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QRect,
    QSortFilterProxyModel,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
)
from PySide6.QtWidgets import QStyledItemDelegate

from pms.constants import (
    PMS_COLUMN_ENABLED,
    PMS_COLUMN_INDEX,
    PMS_COLUMN_LATENCY,
    PMS_COLUMN_NAME,
    PMS_COLUMN_PORT,
    PMS_COLUMN_SPEED,
    PMS_COLUMN_TELEGRAM,
    PMS_COLUMN_TITLES,
    PMS_LOG_LEVEL_ORDER,
    PMS_PROTOCOL_TITLES,
    PMS_TRANSPORT_TITLES,
)
from pms.utils import pms_format_bytes, pms_format_speed, pms_safe_int
from pms.models import (
    PmsConnectionRecord,
    PmsProxyNode,
)

# --------------------------------------------------------------------------------------
# Qt 数据模型
# --------------------------------------------------------------------------------------
# v1.9.30：排序不再交给 Qt。原来的两个坑（都在真库 5.75 万节点上实测过）：
#   1. `lessThan()` 排序：每次比较都要「索引逐层 mapToSource → 取节点 → 读字段」，
#      按延迟 **8.1 秒**、按名称 **41.7 秒**，全卡在界面线程（点表头、测完自动排序都会踩）；
#      换 `setSortRole` 让 Qt 比数值也不行（每次比较仍要回调 Python 的 `data()`，
#      3.3 万节点实测反而更慢，31–59 秒）。
#   2. 排序挂在代理上时，每次「重新加载节点」(`pntm_set_nodes`) 都要被代理重排一遍，
#      实测 **43.6 秒**。
# 现在由 `pntm_sort_by()` 在 Python 里 `list.sort(key=…)` 直接排节点列表：键只算一次
# （5.75 万次而不是 50–100 万次），全量排序 **1.0–1.7 秒**；重载也只剩筛选重算，**0.75 秒**。
# 详见 `软件升级迭代记录.md` 铁律 7.48。

# 从未测出延迟的节点排最后（升序），所以给一个比任何真实延迟都大的值
_PNTM_SORT_LATENCY_MISSING = 10 ** 9


class PmsNodeTableModel(QAbstractTableModel):
    """节点表格模型：列固定、支持勾选启用、延迟着色。"""

    # v1.9.21：失败节点的延迟列显示「超时」还是核心原话（HTTP 503 / HTTP 504…），由主窗口按设置灌进来
    pntm_show_core_fail = False

    pntm_node_toggled = Signal(str, bool)   # uid, 是否启用

    def __init__(self, pms_parent: Optional[QObject] = None) -> None:
        super().__init__(pms_parent)
        self.pntm_nodes: list[PmsProxyNode] = []
        self.pntm_index_by_uid: dict[str, int] = {}
        # v1.9.30：记住当前按哪一列排（-1 = 没排过），重载节点后接着排，见 pntm_set_nodes()
        self.pntm_sort_column = -1
        self.pntm_sort_order = Qt.SortOrder.AscendingOrder
        self.pntm_theme_dark = True
        # v1.9.13：当前节点（名称列高亮）。原来只在 pmw_highlight_current_node() 里懒赋值，
        # 启动时 pmw_restore_last_node() 也要读写它，这里显式初始化成空串。
        self.pntm_active_uid = ""
        # v1.9.41：关键字搜索串缓存（按 uid，见 pntm_search_text 的说明）。
        # 真库 5.75 万节点实测：每行现拼一遍要 1.66 秒，缓存后只要 0.18 秒。
        self.pntm_filter_text: dict[str, str] = {}
        self.pntm_warm_timer: Optional[QTimer] = None

    # ------------------------------------------------------------------ 数据装载
    def pntm_set_nodes(self, pms_nodes: Sequence[PmsProxyNode]) -> None:
        """整体替换节点列表。

        v1.9.30：替换后**接着按当前这一列排**（`pntm_sort_column`）。以前排序挂 Qt 代理上，
        重载完代理自己会重排，表看着还是排好的；现在排序落在模型上，不接上的话
        「测完自动排序 → 导入/改节点 → 表又变回序号顺序」就成新的不一致了。
        在 `beginResetModel` 之内排，不额外发信号，5.7 万节点只多 0.15 秒。
        """
        self.beginResetModel()
        self.pntm_nodes = list(pms_nodes)
        self.pntm_sort_in_place()
        self.pntm_reindex()
        self.endResetModel()
        self.pntm_warm_filter_text_start()

    # ------------------------------------------------------------------ 关键字搜索串（v1.9.41）
    def pntm_search_text(self, pms_node: PmsProxyNode) -> str:
        """一行的关键字搜索串（小写），按 uid 缓存。

        ★ 为什么要缓存：这一串是 8 个字段拼起来再 `lower()`，真库 5.75 万节点**每行现拼**
        实测 1.66 秒——用户看到的就是「搜索框里打一个字卡一秒」。缓存后第二次搜索只要
        0.18 秒。缓存键用 **uid**（排序/分页都不影响），节点被更新时按 uid 作废。
        """
        cached = self.pntm_filter_text.get(pms_node.uid)
        if cached is None:
            cached = " ".join([
                pms_node.name,
                pms_node.server,
                str(pms_node.port),
                pms_node.remark,
                pms_node.group_name,
                pms_node.subscription_name,
                PMS_PROTOCOL_TITLES.get(pms_node.protocol, pms_node.protocol),
                pms_node.tags,
            ]).lower()
            self.pntm_filter_text[pms_node.uid] = cached
        return cached

    def pntm_warm_filter_text_start(self) -> None:
        """节点装载完就**分片预热**搜索串缓存（每片 ~30 ms，不卡界面）。"""
        if self.pntm_warm_timer is None:
            self.pntm_warm_timer = QTimer(self)
            self.pntm_warm_timer.setSingleShot(True)
            self.pntm_warm_timer.timeout.connect(self.pntm_warm_filter_text_chunk)
        self.pntm_warm_timer.start(0)

    def pntm_warm_filter_text_chunk(self) -> None:
        """预热一片：最多干 30 ms 就交还事件循环，没干完就再排一片。"""
        deadline = time.perf_counter() + 0.03
        for node in self.pntm_nodes:
            if node.uid in self.pntm_filter_text:
                continue
            self.pntm_search_text(node)
            if time.perf_counter() >= deadline:
                if self.pntm_warm_timer is not None:
                    self.pntm_warm_timer.start(0)
                return

    def pntm_warm_filter_text_done(self) -> bool:
        """缓存是否已经预热完（探针与自检用）。"""
        return len(self.pntm_filter_text) >= len(self.pntm_nodes)

    def pntm_sort_in_place(self) -> None:
        """按记住的列与方向就地重排 `pntm_nodes`（没记过列就什么都不做）。"""
        column = self.pntm_sort_column
        if column < 0 or len(self.pntm_nodes) <= 1:
            return
        reverse = self.pntm_sort_order == Qt.SortOrder.DescendingOrder
        try:
            self.pntm_nodes.sort(key=lambda node: self.pntm_sort_key(node, column), reverse=reverse)
        except TypeError:  # 同一列偶尔混进不同类型（脏数据）时不要炸，退回字符串比
            self.pntm_nodes.sort(key=lambda node: str(self.pntm_sort_key(node, column)), reverse=reverse)

    def pntm_reindex(self) -> None:
        self.pntm_index_by_uid = {node.uid: index for index, node in enumerate(self.pntm_nodes)}

    def pntm_sort_key(self, pms_node: PmsProxyNode, pms_column: int) -> Any:
        """排序键（v1.9.30）：在 `pntm_sort_value()` 之上补两件事。

        - 文本列转小写 → 与原来的「大小写不敏感」一致；
        - 数字保持数字 → 「1,000 ms」不会排到「90 ms」前面。
        """
        value = self.pntm_sort_value(pms_node, pms_column)
        return value.lower() if isinstance(value, str) else value

    def pntm_sort_by(self, pms_column: int, pms_order: Qt.SortOrder) -> bool:
        """按列重排**节点列表本身**（v1.9.30）。返回是否真的动过。

        ★ 为什么不让 Qt 排：`QSortFilterProxyModel` 每次比较都要回调 Python
        （`data()` / `lessThan()`），5.75 万节点要比较 50–100 万次，真库实测
        **按延迟 8.1 秒 / 按名称 41.7 秒**，全卡在界面线程——点表头、测完自动排序都会踩。
        改成 `list.sort(key=…)` 只在算键时碰 Python（5.75 万次），实测 **1.0–1.7 秒**
        （其中约 0.6 秒是排序后两层代理重算映射，纯排序只占 0.15 秒）。

        ★ 为什么用 `layoutAboutToBeChanged` + `layoutChanged` 而不是 `beginResetModel`：
        重置会连表头的排序箭头、滚动条一起打回原形，而布局变化只是告诉视图「行换了位置」。

        ⚠️ 选中项**不会**跟着节点走（实测：链条上有两层代理时 Qt 不做这种迁移，选中会留在
        原来的行号上）。所以界面侧排完要按 uid 重新选一遍（`pnfp_restore_selection`）。
        """
        total = len(self.pntm_nodes)
        self.pntm_sort_column = int(pms_column)
        self.pntm_sort_order = pms_order
        if total <= 1:
            return False
        reverse = pms_order == Qt.SortOrder.DescendingOrder
        try:
            order = sorted(range(total), key=lambda i: self.pntm_sort_key(self.pntm_nodes[i], pms_column), reverse=reverse)
        except TypeError:  # 同一列偶尔混进不同类型（脏数据）时不要炸，退回字符串比
            order = sorted(range(total), key=lambda i: str(self.pntm_sort_key(self.pntm_nodes[i], pms_column)), reverse=reverse)
        if order == list(range(total)):
            return False
        self.layoutAboutToBeChanged.emit()
        self.pntm_nodes = [self.pntm_nodes[index] for index in order]
        self.pntm_reindex()
        self.layoutChanged.emit()
        return True

    def pntm_set_theme_dark(self, pms_dark: bool) -> None:
        self.pntm_theme_dark = bool(pms_dark)
        if self.pntm_nodes:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.pntm_nodes) - 1, len(PMS_COLUMN_TITLES) - 1))

    def pntm_node_at(self, pms_row: int) -> Optional[PmsProxyNode]:
        if 0 <= pms_row < len(self.pntm_nodes):
            return self.pntm_nodes[pms_row]
        return None

    def pntm_node_by_uid(self, pms_uid: str) -> Optional[PmsProxyNode]:
        index = self.pntm_index_by_uid.get(str(pms_uid))
        return self.pntm_nodes[index] if index is not None else None

    def pntm_update_node(self, pms_node: PmsProxyNode) -> None:
        """刷新单个节点所在行（测试结果变化时调用）。"""
        index = self.pntm_index_by_uid.get(pms_node.uid)
        if index is None:
            return
        self.pntm_nodes[index] = pms_node
        self.pntm_filter_text.pop(pms_node.uid, None)   # v1.9.41：节点变了就作废搜索串缓存
        self.dataChanged.emit(self.index(index, 0), self.index(index, len(PMS_COLUMN_TITLES) - 1))

    def pntm_update_nodes(self, pms_nodes: Sequence[PmsProxyNode]) -> None:
        for node in pms_nodes:
            self.pntm_update_node(node)

    def pntm_insert_nodes(self, pms_nodes: Sequence[PmsProxyNode]) -> None:
        """增量追加若干节点（新增 / 复制 / 导入单个）。

        v1.9.72：卡顿修复核心——单节点 / 少量节点操作不再走 `pntm_set_nodes`（beginResetModel
        触发两层代理全量重建 0.05–0.18s + 排序重排，55k 库下叠加仍明显）。这里用 `rowsInserted`
        只把新行插进表尾，成本与节点数成正比（~N 而非 55k），配合 `pnfp_on_source_rows` 的
        增量处理，任意小幅增删都从「秒级卡顿」降到「毫秒级」。

        设计取舍：新节点一律追加到 `pntm_nodes` 末尾，即使当前按某列在排。**追加后索引与 uid
        映射立即更新**；若当前有排序，视图会在下一帧按 `layoutChanged` 把新行归位（调用方可
        选是否补 `pntm_sort_in_place()`，但那会重排整表，仅在不介意时调用）。筛选代理只负责把
        通过筛选的新行并进去，不重算既有行。
        """
        add = [node for node in pms_nodes if node.uid not in self.pntm_index_by_uid]
        if not add:
            return
        first = len(self.pntm_nodes)
        self.beginInsertRows(QModelIndex(), first, first + len(add) - 1)
        for node in add:
            self.pntm_nodes.append(node)
            self.pntm_index_by_uid[node.uid] = len(self.pntm_nodes) - 1
            # 预填搜索串缓存，避免代理首次扫到这一行时现场拼（55k 库现拼一行 ~0.03ms，但攒多了也累）
            self.pntm_filter_text[node.uid] = " ".join([
                node.name, node.server, str(node.port), node.remark,
                node.group_name, node.subscription_name,
                PMS_PROTOCOL_TITLES.get(node.protocol, node.protocol), node.tags,
            ]).lower()
        self.endInsertRows()

    def pntm_remove_nodes_by_uids(self, pms_uids: Sequence[str]) -> None:
        """增量删除若干节点（删除 / 批量删除）。

        v1.9.72：同 `pntm_insert_nodes`，用 `rowsRemoved` 按 uid 精准删除，避免整表重置。
        注意索引必须**从后往前**删，否则前面的删除会打乱后面的行号。
        """
        uid_set = {str(uid) for uid in pms_uids}
        indices = [self.pntm_index_by_uid[uid] for uid in uid_set if uid in self.pntm_index_by_uid]
        if not indices:
            return
        indices.sort(reverse=True)
        # 收集被删行号区间（用于一次性 beginRemoveRows，要求连续；删除可能不连续，
        # 这里逐行发信号——Qt 允许多次 begin/end，只是比整块略多开销，但删除本就少）。
        for index in indices:
            self.beginRemoveRows(QModelIndex(), index, index)
            removed = self.pntm_nodes.pop(index)
            self.pntm_filter_text.pop(removed.uid, None)
            self.endRemoveRows()
        self.pntm_reindex()

    def pntm_uids_ordered(self) -> list[str]:
        return [node.uid for node in self.pntm_nodes]

    # ------------------------------------------------------------------ Qt 接口
    def rowCount(self, pms_parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self.pntm_nodes)

    def columnCount(self, pms_parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(PMS_COLUMN_TITLES)

    def headerData(self, pms_section: int, pms_orientation: Qt.Orientation, pms_role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if pms_role != Qt.ItemDataRole.DisplayRole:
            return None
        if pms_orientation == Qt.Orientation.Horizontal:
            if 0 <= pms_section < len(PMS_COLUMN_TITLES):
                return PMS_COLUMN_TITLES[pms_section]
            return None
        return pms_section + 1

    def flags(self, pms_index: QModelIndex) -> Qt.ItemFlag:  # noqa: N802
        if not pms_index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if pms_index.column() == PMS_COLUMN_ENABLED:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, pms_index: QModelIndex, pms_role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if not pms_index.isValid():
            return None
        node = self.pntm_node_at(pms_index.row())
        if node is None:
            return None
        column = pms_index.column()
        if pms_role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return node.pnode_row_value(column, bool(getattr(self, "pntm_show_core_fail", False)))
        if pms_role == Qt.ItemDataRole.CheckStateRole and column == PMS_COLUMN_ENABLED:
            return Qt.CheckState.Checked if node.enabled else Qt.CheckState.Unchecked
        if pms_role == Qt.ItemDataRole.ToolTipRole:
            errors = node.pnode_validate()
            tips = [
                f"名称：{node.pnode_display_name()}",
                f"协议：{node.pnode_display_type()}    传输：{PMS_TRANSPORT_TITLES.get(node.transport, node.transport)}",
                f"地址：{node.pnode_display_address()}",
                f"延迟：{node.pnode_display_latency(bool(getattr(self, 'pntm_show_core_fail', False)))}    测速：{node.pnode_display_speed()}",
                f"分组：{node.group_name}    订阅：{node.subscription_name or '-'}",
                f"测试时间：{node.last_test_time or '-'}（{node.last_test_mode or '-'}）",
                f"成功/失败次数：{node.success_count}/{node.fail_count}",
            ]
            if node.last_test_note:
                # v1.9.21：失败原因的**原话**永远留在提示里（不管列表里显示的是「超时」还是 HTTP 码）
                tips.append(f"失败原因：{node.last_test_note}")
            tips.extend(node.pnode_telegram_tip_lines())   # v1.9.59：电报成绩（逻辑在 models.py，这里只拼接）
            if node.remark:
                tips.append(f"备注：{node.remark}")
            if errors:
                tips.append("配置问题：" + "；".join(errors))
            return "\n".join(tips)
        if pms_role == Qt.ItemDataRole.ForegroundRole and column in (PMS_COLUMN_LATENCY, PMS_COLUMN_SPEED, PMS_COLUMN_NAME, PMS_COLUMN_TELEGRAM):
            grade = node.pnode_telegram_color_key() if column == PMS_COLUMN_TELEGRAM else node.pnode_latency_color_key()
            if column == PMS_COLUMN_NAME:
                return QBrush(QColor("#7fb3ff" if self.pntm_theme_dark else "#1f6feb")) if node.uid == getattr(self, "pntm_active_uid", "") else None
            colors = {
                "good": "#3fb950" if self.pntm_theme_dark else "#1a7f37",
                "medium": "#d29922" if self.pntm_theme_dark else "#9a6700",
                "slow": "#e5534b" if self.pntm_theme_dark else "#c1352c",
                "timeout": "#e5534b" if self.pntm_theme_dark else "#c1352c",
                "unknown": "#9aa4b1" if self.pntm_theme_dark else "#5b6472",
            }
            return QBrush(QColor(colors.get(grade, "#9aa4b1")))
        if pms_role == Qt.ItemDataRole.TextAlignmentRole and column in (PMS_COLUMN_ENABLED, PMS_COLUMN_INDEX, PMS_COLUMN_PORT, PMS_COLUMN_LATENCY, PMS_COLUMN_SPEED, PMS_COLUMN_TELEGRAM):
            return int(Qt.AlignmentFlag.AlignCenter)
        if pms_role == Qt.ItemDataRole.UserRole:
            return node
        return None

    @staticmethod
    def pntm_sort_value(pms_node: PmsProxyNode, pms_column: int) -> Any:
        """取某列用于排序的值（v1.9.30）：数值列给数值、文本列给显示文本。

        数值列必须给数字，否则「1,000 ms」会排到「90 ms」前面（这正是 v1.9.12 当年
        重写 `lessThan()` 的原因）。`pntm_sort_key()` 在这上面再加「文本转小写」
        「未测延迟给无穷大」两层，然后交给 `list.sort()`。
        """
        if pms_column == PMS_COLUMN_LATENCY:
            latency = pms_node.latency_ms
            return int(latency) if latency is not None and latency >= 0 else _PNTM_SORT_LATENCY_MISSING
        if pms_column == PMS_COLUMN_SPEED:
            return float(pms_node.speed_bps or 0.0)
        if pms_column == PMS_COLUMN_TELEGRAM:
            # v1.9.59：按最快 DC 往返排序；不可连给大值、未测排最后。
            # v1.9.61：分层——可连（有 MTProto 握手）按 ms；仅TCP 假通排其后；不通再其后；未测最后
            ok_state = pms_safe_int(pms_node.telegram_ok, 0, -1, 1)
            if ok_state > 0 and pms_node.telegram_best_ms >= 0:
                return int(pms_node.telegram_best_ms)
            if ok_state < 0 and pms_safe_int(pms_node.telegram_tcp_count, 0, 0, 99) > 0:
                return _PNTM_SORT_LATENCY_MISSING * 2   # 仅TCP：排在不通之前
            if ok_state < 0:
                return _PNTM_SORT_LATENCY_MISSING * 5   # 不通
            return _PNTM_SORT_LATENCY_MISSING * 10       # 未测：永远最后
        if pms_column == PMS_COLUMN_PORT:
            return pms_safe_int(pms_node.port, 0, 0)
        if pms_column == PMS_COLUMN_INDEX:
            return pms_safe_int(pms_node.sort_index, 0, 0)
        if pms_column == PMS_COLUMN_ENABLED:
            return 1 if pms_node.enabled else 0
        return pms_node.pnode_row_value(pms_column)

    def setData(self, pms_index: QModelIndex, pms_value: Any, pms_role: int = Qt.ItemDataRole.EditRole) -> bool:  # noqa: N802
        if not pms_index.isValid() or pms_index.column() != PMS_COLUMN_ENABLED:
            return False
        if pms_role != Qt.ItemDataRole.CheckStateRole:
            return False
        node = self.pntm_node_at(pms_index.row())
        if node is None:
            return False
        node.enabled = Qt.CheckState(pms_value) == Qt.CheckState.Checked if not isinstance(pms_value, bool) else bool(pms_value)
        self.dataChanged.emit(pms_index, pms_index, [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.DisplayRole])
        self.pntm_node_toggled.emit(node.uid, node.enabled)
        return True

    def pntm_set_all_enabled(self, pms_enabled: bool) -> None:
        """整表启用 / 停用。"""
        if not self.pntm_nodes:
            return
        for node in self.pntm_nodes:
            node.enabled = bool(pms_enabled)
        top_left = self.index(0, PMS_COLUMN_ENABLED)
        bottom_right = self.index(len(self.pntm_nodes) - 1, PMS_COLUMN_ENABLED)
        self.dataChanged.emit(top_left, bottom_right)
        for node in self.pntm_nodes:
            self.pntm_node_toggled.emit(node.uid, node.enabled)

class PmsNodeFilterProxyModel(QAbstractProxyModel):
    """节点筛选与排序（分组 / 协议 / 关键字 / 只看启用 / 只看可用）。

    ★ v1.9.41 重写：**自己维护「筛过的行表」，不再让 Qt 逐行回调 Python。**
    真库 5.75 万节点实测（用户报的「筛选卡顿」就是它）：

    | 做法 | 一次全量筛选 |
    |---|---|
    | 老写法 `QSortFilterProxyModel`：每行回调一次 `filterAcceptsRow` | 空筛选 **1.40 s**、带关键字 **1.89 s** |
    | 其中「Qt → Python 虚函数转发」本身（`return True` 也算） | **1.4 s**（57 549 次 × ~24 µs） |
    | 其中「每行现拼 8 字段搜索串 + lower」 | **1.66 s** |
    | 新写法：Python 里扫一遍 + 按 uid 缓存搜索串 + 只映射看得见的行 | 空筛选 **0.05 s**、带关键字 **0.18 s**（缓存预热后） |

    视图只对**当前页那 50 行**调用 `mapToSource()`，所以「行表」怎么算、算多快，才是筛选快慢的关键。
    `QSortFilterProxyModel` 的逐行回调是 PySide6 的固有开销，改不掉，只能不用它。
    """

    def __init__(self, pms_parent: Optional[QObject] = None) -> None:
        super().__init__(pms_parent)
        self.pnfp_group = ""
        self.pnfp_protocol = ""
        self.pnfp_keyword = ""
        self.pnfp_only_enabled = False
        self.pnfp_only_available = False
        self.pnfp_only_failed = False
        # v1.9.30：不再用 Qt 排序，见 pntm_sort_by() 的说明。
        # ------------------------------------------------------------------
        # v1.7.0 分页：同一个类复用两次
        #   实例一（筛选层）：源 = PmsNodeTableModel，负责筛选 + 排序，pnfp_page_size = 0；
        #   实例二（分页层）：源 = 筛选层，只按「筛选后的序号」切一页出来。
        # 之所以不新开一个类，是因为新增公开类要进启动器重导出，
        # 而启动器已顶在 400 行红线（见文档「待处理违规项」）。
        # ------------------------------------------------------------------
        self.pnfp_page = 1
        self.pnfp_page_size = 0
        self.pnfp_sort_target: Optional["PmsNodeFilterProxyModel"] = None
        # 行表：本层行号 ↔ 源模型行号（v1.9.41）
        self.pnfp_rows: list[int] = []
        self.pnfp_row_of_source: list[int] = []
        # 页内排序（「只排当前页」）：(-1, …) = 按源顺序
        self.pnfp_here_sort: tuple[int, Qt.SortOrder] = (-1, Qt.SortOrder.AscendingOrder)
        self.pnfp_source_slots: list[tuple[Any, Callable]] = []

    # ------------------------------------------------------------------ 源模型接线
    def setSourceModel(self, pms_model: QAbstractItemModel) -> None:  # noqa: N802
        for signal, slot in self.pnfp_source_slots:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.pnfp_source_slots = []
        super().setSourceModel(pms_model)
        if pms_model is not None:
            for signal, slot in (
                (pms_model.modelReset, self.pnfp_on_source_reset),
                (pms_model.layoutChanged, self.pnfp_on_source_reset),
                (pms_model.rowsInserted, self.pnfp_on_source_rows),
                (pms_model.rowsRemoved, self.pnfp_on_source_rows),
                (pms_model.dataChanged, self.pnfp_on_source_data),
            ):
                signal.connect(slot)
                self.pnfp_source_slots.append((signal, slot))
        self.pnfp_rebuild()

    def pnfp_on_source_reset(self, *_args: Any) -> None:
        self.pnfp_rebuild()

    def pnfp_on_source_rows(self, *_args: Any) -> None:
        """源模型插入/删除了行（增量 `pntm_insert_nodes` / `pntm_remove_nodes_by_uids`）。

        v1.9.72 卡顿修复：源模型侧已经用 `beginInsertRows`/`beginRemoveRows` 把视图那几行
        增量地动过了，这里只需把筛选/分页行表（`pnfp_rows`）重算一遍。重算本身只扫一遍节点
        （55k 库下 ~0.18s），远比「重新从数据库全量加载 + 逐行构造 55k 节点（13.7s）」轻。
        真正省掉的开销在模型层：增量插入只给新节点算一次搜索串缓存，不再重暖全部 55k 节点。
        """
        self.pnfp_rebuild()

    def pnfp_on_source_data(self, pms_top_left: QModelIndex, pms_bottom_right: QModelIndex, *_args: Any) -> None:
        """源模型某几行变了：**只有「筛不筛得中」可能变化时才重建**，否则只转发。

        这样「勾选启用」「测完一个节点」这种高频改动不会把整张行表重算一遍。
        """
        row_count = self.pnfp_source_count()
        if self.pnfp_row_of_source and len(self.pnfp_row_of_source) != row_count:
            self.pnfp_rebuild()
            return
        for source_row in range(pms_top_left.row(), pms_bottom_right.row() + 1):
            if source_row < 0 or source_row >= row_count:
                continue
            if self.pnfp_accepts_source_row(source_row) != (self.pnfp_row_of_source[source_row] >= 0):
                self.pnfp_rebuild()
                return
        first, last = self.mapFromSource(pms_top_left), self.mapFromSource(pms_bottom_right)
        if first.isValid() and last.isValid():
            self.dataChanged.emit(first, last)

    def pnfp_source_count(self) -> int:
        model = self.sourceModel()
        return model.rowCount() if model is not None else 0

    # ------------------------------------------------------------------ 行表
    def pnfp_accepts_source_row(self, pms_source_row: int) -> bool:
        """源模型某行过不过筛选（老 `filterAcceptsRow` 的等价逻辑，只是不再由 Qt 逐行调）。"""
        model = self.sourceModel()
        if not isinstance(model, PmsNodeTableModel):
            # v1.7.0：源不是节点模型 = 本实例是「分页层」。
            # 此时源行号就是「筛选后的序号」，天然有序，直接切区间即可。
            if self.pnfp_page_size <= 0:
                return True
            start = (max(1, self.pnfp_page) - 1) * self.pnfp_page_size
            return start <= pms_source_row < start + self.pnfp_page_size
        node = model.pntm_node_at(pms_source_row)
        if node is None:
            return False
        if self.pnfp_only_enabled and not node.enabled:
            return False
        latency = node.latency_ms
        if self.pnfp_only_available and (latency is None or latency < 0):
            return False
        if self.pnfp_only_failed and latency is not None and latency >= 0:
            return False
        if self.pnfp_group and node.group_name != self.pnfp_group:
            return False
        if self.pnfp_protocol and node.protocol != self.pnfp_protocol:
            return False
        if self.pnfp_keyword and self.pnfp_keyword not in model.pntm_search_text(node):
            return False
        return True

    def pnfp_compute_rows(self) -> list[int]:
        """整趟算一遍「哪几行通过筛选」（Python 里的单次扫描）。

        ★ v1.9.41 的第二个关键点：把筛选条件与「自己是哪一层」的判定**全部提到循环外**。
        PySide6 里 `self.xxx` 每次读取都要过 Qt 的属性机制（真机实测 ~1 µs），
        5.75 万行 × 6 个条件就是 0.3 秒白花；`pntm_search_text()` 也只在缓存未命中时才调。
        """
        model = self.sourceModel()
        if model is None:
            return []
        if not isinstance(model, PmsNodeTableModel):
            # 分页层：源行号就是「筛选后的序号」，切区间即可（切完仍要按「页内排序」重排）
            count = model.rowCount()
            if self.pnfp_page_size <= 0:
                return self.pnfp_apply_here_sort(list(range(count)))
            start = (max(1, self.pnfp_page) - 1) * self.pnfp_page_size
            return self.pnfp_apply_here_sort(list(range(start, min(start + self.pnfp_page_size, count))))
        group = self.pnfp_group
        protocol = self.pnfp_protocol
        keyword = self.pnfp_keyword
        only_enabled = self.pnfp_only_enabled
        only_available = self.pnfp_only_available
        only_failed = self.pnfp_only_failed
        texts = model.pntm_filter_text
        rows: list[int] = []
        append = rows.append
        for index, node in enumerate(model.pntm_nodes):
            if only_enabled and not node.enabled:
                continue
            if only_available or only_failed:
                latency = node.latency_ms
                if only_available and (latency is None or latency < 0):
                    continue
                if only_failed and latency is not None and latency >= 0:
                    continue
            if group and node.group_name != group:
                continue
            if protocol and node.protocol != protocol:
                continue
            if keyword:
                text = texts.get(node.uid)
                if text is None:
                    text = model.pntm_search_text(node)
                if keyword not in text:
                    continue
            append(index)
        return self.pnfp_apply_here_sort(rows)

    def pnfp_apply_here_sort(self, pms_rows: list[int]) -> list[int]:
        """按「页内排序」把行表重排（v1.9.41：Python 排，不再靠 `lessThan()` 回调）。

        注意本层可能是**分页层**（它的源是筛选层），所以「本层源行号 → 节点」要多走一层，
        排序键统一取 `pntm_sort_key()`（与全量排序同一套语义，`1,000 ms` 不会排到 `104 ms` 前）。
        """
        column, order = self.pnfp_here_sort
        node_model = self.pnfp_node_model()
        if column < 0 or len(pms_rows) <= 1 or node_model is None:
            return pms_rows
        reverse = order == Qt.SortOrder.DescendingOrder

        def sort_key(pms_source_row: int) -> Any:
            node = self.pnfp_node_for_source_row(pms_source_row)
            return node_model.pntm_sort_key(node, column) if node is not None else ""

        try:
            return sorted(pms_rows, key=sort_key, reverse=reverse)
        except TypeError:   # 同列混进不同类型（脏数据）时退回字符串比，别炸
            return sorted(pms_rows, key=lambda row: str(sort_key(row)), reverse=reverse)

    def sortColumn(self) -> int:  # noqa: N802
        """本层当前按哪一列排（-1 = 按源顺序）。v1.9.41：`QSortFilterProxyModel` 有这个接口，
        `QAbstractProxyModel` 没有，但老代码/老探针会读它，所以照着语义补上（就是页内排序列）。"""
        return int(self.pnfp_here_sort[0])

    def sortOrder(self) -> Qt.SortOrder:  # noqa: N802
        """本层当前的排序方向（与 `sortColumn()` 配套）。"""
        return self.pnfp_here_sort[1]

    def pnfp_node_model(self) -> Optional["PmsNodeTableModel"]:
        """一路往下找到真正的节点模型（分页层的源是筛选层，得绕过它）。"""
        model = self.sourceModel()
        while model is not None and not isinstance(model, PmsNodeTableModel):
            model = model.sourceModel()
        return model if isinstance(model, PmsNodeTableModel) else None

    def pnfp_node_for_source_row(self, pms_source_row: int) -> Optional[PmsProxyNode]:
        """把「本层的源行号」翻成节点（逐层套用上层的行表）。"""
        model = self.sourceModel()
        row = pms_source_row
        while model is not None and not isinstance(model, PmsNodeTableModel):
            rows = getattr(model, "pnfp_rows", None)
            if rows is None or not (0 <= row < len(rows)):
                return None
            row = rows[row]
            model = model.sourceModel()
        return model.pntm_node_at(row) if isinstance(model, PmsNodeTableModel) else None

    def pnfp_rebuild(self) -> None:
        """重算行表并（必要时）通知视图。"""
        rows = self.pnfp_compute_rows()
        if rows == self.pnfp_rows and len(self.pnfp_row_of_source) == self.pnfp_source_count():
            return
        self.beginResetModel()
        self.pnfp_rows = rows
        self.pnfp_row_of_source = [-1] * self.pnfp_source_count()
        for index, source_row in enumerate(rows):
            if 0 <= source_row < len(self.pnfp_row_of_source):
                self.pnfp_row_of_source[source_row] = index
        self.endResetModel()

    def invalidateFilter(self) -> None:  # noqa: N802 - 保留旧名字，外部调用不用改
        """重算行表（老 Qt 接口的同名替身）。"""
        self.pnfp_rebuild()

    # ------------------------------------------------------------------ Qt 代理接口
    def rowCount(self, pms_parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if pms_parent.isValid():
            return 0
        return len(self.pnfp_rows)

    def columnCount(self, pms_parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        model = self.sourceModel()
        return model.columnCount() if model is not None else 0

    def index(self, pms_row: int, pms_column: int, pms_parent: QModelIndex = QModelIndex()) -> QModelIndex:  # noqa: N802
        if pms_parent.isValid() or not (0 <= pms_row < len(self.pnfp_rows)) or pms_column < 0:
            return QModelIndex()
        return self.createIndex(pms_row, pms_column)

    def parent(self, pms_index: QModelIndex = QModelIndex()) -> QModelIndex:  # type: ignore[override]  # noqa: N802
        return QModelIndex()

    def mapToSource(self, pms_index: QModelIndex) -> QModelIndex:  # noqa: N802
        model = self.sourceModel()
        if model is None or not pms_index.isValid():
            return QModelIndex()
        row = pms_index.row()
        if not (0 <= row < len(self.pnfp_rows)):
            return QModelIndex()
        return model.index(self.pnfp_rows[row], pms_index.column())

    def mapFromSource(self, pms_index: QModelIndex) -> QModelIndex:  # noqa: N802
        if not pms_index.isValid():
            return QModelIndex()
        row = pms_index.row()
        if not (0 <= row < len(self.pnfp_row_of_source)):
            return QModelIndex()
        target = self.pnfp_row_of_source[row]
        if target < 0:
            return QModelIndex()
        return self.index(target, pms_index.column())

    def pnfp_set_filters(
        self,
        pms_group: str = "",
        pms_protocol: str = "",
        pms_keyword: str = "",
        pms_only_enabled: bool = False,
        pms_only_available: bool = False,
        pms_only_failed: bool = False,
    ) -> None:
        """更新筛选条件并立即生效（条件没变就什么都不做，省掉一次全表扫描）。"""
        wanted = (str(pms_group or ""), str(pms_protocol or ""), str(pms_keyword or "").strip().lower(),
                  bool(pms_only_enabled), bool(pms_only_available), bool(pms_only_failed))
        current = (self.pnfp_group, self.pnfp_protocol, self.pnfp_keyword,
                   self.pnfp_only_enabled, self.pnfp_only_available, self.pnfp_only_failed)
        if wanted == current:
            return
        self.pnfp_group, self.pnfp_protocol, self.pnfp_keyword, self.pnfp_only_enabled, \
            self.pnfp_only_available, self.pnfp_only_failed = wanted
        self.pnfp_rebuild()

    def sort(self, pms_column: int, pms_order: Qt.SortOrder) -> None:  # noqa: N802
        """排序。分页层不能自己排——它只看到当前页，排出来只在页内生效，看着像乱序。

        v1.7.0：分页层把自己的 sort 转交给全量筛选层（pnfp_sort_target），
        那边排完，本层跟着重算，顺序天然一致。
        v1.9.12：转交前先把自己的「页内排序」复位——用 pnfp_sort_here() 做过
        「只排当前页」之后，本层会一直按旧列重排当前页，把刚转交过来的全量排序盖掉。
        v1.9.30：**全量排序不再交给 Qt**，改由节点模型在 Python 里排自己的列表
        （`pntm_sort_by()`，5.75 万节点真库：按延迟 8.1 秒 → 1.4 秒，按名称 41.7 秒 → 1.4 秒）。
        v1.9.41：模型发 `layoutChanged` 时本层会收到并**重算行表**（见 `pnfp_rebuild`），
        所以这里不需要再补一次。
        """
        if self.pnfp_sort_target is not None:
            self.pnfp_sort_here(-1, Qt.SortOrder.AscendingOrder)
            self.pnfp_sort_target.sort(pms_column, pms_order)
            return
        model = self.sourceModel()
        if isinstance(model, PmsNodeTableModel):
            model.pntm_sort_by(pms_column, pms_order)
            return

    def pnfp_sort_here(self, pms_column: int, pms_order: Qt.SortOrder) -> None:
        """只在本层排序（不转发给全量筛选层）。

        v1.9.12「当前页」范围的自动排序用它：只重排当前这一页的行，不动全量顺序、不跳页。
        pms_column = -1 表示复位回源顺序。
        v1.9.41：改成在 Python 里对行表排序（键取 `pntm_sort_key()`，与全量排序同一套语义，
        所以「1,000 ms」不会排到「104 ms」前面）；只排一页最多几十行，快得可以忽略。
        """
        self.pnfp_here_sort = (int(pms_column), pms_order)
        self.pnfp_rebuild()

    def lessThan(self, pms_left: QModelIndex, pms_right: QModelIndex) -> bool:  # noqa: N802
        """v1.9.41 起**不再需要**：页内排序改成 Python 排行表（见 `pnfp_sort_here`）。

        保留这个方法是为了兼容老探针/外部调用的名字检查；它已不在任何热路径上
        （`QAbstractProxyModel` 不用 `lessThan`）。数值语义的唯一出处现在是
        `PmsNodeTableModel.pntm_sort_key()`（全量与页内共用，杜绝「按显示文本比」的老毛病）。
        """
        left_node = self.pnfp_node_of_index(pms_left)
        right_node = self.pnfp_node_of_index(pms_right)
        if left_node is None or right_node is None:
            return False
        column = pms_left.column()
        model = self.sourceModel()
        if isinstance(model, PmsNodeTableModel):
            left_key = model.pntm_sort_key(left_node, column)
            right_key = model.pntm_sort_key(right_node, column)
            try:
                return bool(left_key < right_key)
            except TypeError:
                return str(left_key) < str(right_key)
        return False

    def pnfp_node_of_index(self, pms_index: QModelIndex) -> Optional[PmsProxyNode]:
        """把本层收到的**源模型索引**一路往下映射到节点模型，取回对应节点。

        `lessThan()`（「只排当前页」）与界面里少数「从索引反查节点」的地方用它；
        全量排序不再用它（v1.9.30 起直接排节点模型，见模块顶部注释）。
        """
        model = self.sourceModel()
        index = pms_index
        while model is not None and not isinstance(model, PmsNodeTableModel):
            index = model.mapToSource(index)
            model = model.sourceModel()
        if isinstance(model, PmsNodeTableModel):
            return model.pntm_node_at(index.row())
        return None

    def pnfp_restore_selection(self, pms_selection: "QItemSelectionModel", pms_uids: list[str]) -> int:
        """按 uid 重新选中本层里的行，返回选中了几个（v1.9.30）。

        背景：全量排序改成「排源模型」之后，选中项**不会**再跟着节点跑，而且旧的行号会
        留在原地（实测两层代理链条上 Qt 不做这种迁移），会出现「原来勾的 3 个 + 又按 uid 找到的
        几个」混成一堆。所以这里先清干净再按 uid 选一遍；已经排到看不见的页上的就不再选它。
        """
        wanted = {str(uid) for uid in pms_uids or []}
        picked = 0
        pms_selection.blockSignals(True)
        try:
            pms_selection.clear()
            if not wanted:
                return 0
            for row in range(self.rowCount()):
                index = self.index(row, 0)
                node = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
                if node is not None and str(node.uid) in wanted:
                    pms_selection.select(index, pms_selection.SelectionFlag.Select | pms_selection.SelectionFlag.Rows)
                    picked += 1
        finally:
            pms_selection.blockSignals(False)
        return picked

    # ------------------------------------------------------------------ 分页（v1.7.0）
    def pnfp_filtered_count(self) -> int:
        """筛选结果总数（分页层看源模型行号即可；筛选层看自己 rowCount）。"""
        model = self.sourceModel()
        if isinstance(model, PmsNodeTableModel):
            return self.rowCount()
        return model.rowCount() if model is not None else 0

    def pnfp_page_count(self) -> int:
        """总页数（至少 1 页；不分页时恒为 1）。"""
        if self.pnfp_page_size <= 0:
            return 1
        return max(1, -(-self.pnfp_filtered_count() // self.pnfp_page_size))

    def pnfp_set_pagination(self, pms_page: int, pms_page_size: int) -> tuple[int, int]:
        """设置页码与每页条数，并立即生效。

        页码会被夹取到 [1, 总页数]，返回 (实际页码, 总页数) 供界面显示。
        """
        self.pnfp_page_size = max(0, pms_safe_int(pms_page_size, 0, 0))
        pages = self.pnfp_page_count()
        self.pnfp_page = max(1, min(pms_safe_int(pms_page, 1, 1), pages))
        self.invalidateFilter()
        return self.pnfp_page, self.pnfp_page_count()

    def pnfp_page_of_source_row(self, pms_source_row: int) -> int:
        """某个「筛选后的序号」落在第几页（1 起）；不分页时恒为 1。"""
        if self.pnfp_page_size <= 0 or pms_source_row < 0:
            return 1
        return pms_source_row // self.pnfp_page_size + 1

class PmsLogTableModel(QAbstractTableModel):
    """日志模型（时间 / 级别 / 来源 / 内容），带行数上限。"""

    pltm_HEADERS = ("时间", "级别", "来源", "内容")

    def __init__(self, pms_max_lines: int = 2000, pms_parent: Optional[QObject] = None) -> None:
        super().__init__(pms_parent)
        self.pltm_rows: list[tuple[str, str, str, str]] = []
        self.pltm_max_lines = max(100, int(pms_max_lines))
        self.pltm_min_level = 20
        self.pltm_filter_text = ""

    def pltm_set_max_lines(self, pms_max_lines: int) -> None:
        self.pltm_max_lines = max(100, int(pms_max_lines))
        if len(self.pltm_rows) > self.pltm_max_lines:
            self.beginResetModel()
            self.pltm_rows = self.pltm_rows[-self.pltm_max_lines:]
            self.endResetModel()

    def pltm_set_min_level(self, pms_level: str) -> None:
        """按级别过滤显示（只影响本模型，不影响日志文件）。"""
        self.pltm_min_level = PMS_LOG_LEVEL_ORDER.get(str(pms_level), 20)
        self.pltm_rebuild_view()

    def pltm_set_filter_text(self, pms_text: str) -> None:
        self.pltm_filter_text = str(pms_text or "").strip().lower()
        self.pltm_rebuild_view()

    def pltm_rebuild_view(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    def pltm_append(self, pms_time_text: str, pms_level: str, pms_source: str, pms_message: str) -> None:
        """追加一行日志（超出上限时裁剪）。"""
        self.pltm_rows.append((pms_time_text, pms_level, pms_source, pms_message))
        if len(self.pltm_rows) > self.pltm_max_lines:
            self.beginResetModel()
            self.pltm_rows = self.pltm_rows[-self.pltm_max_lines:]
            self.endResetModel()
            return
        row = len(self.pltm_rows) - 1
        self.beginInsertRows(QModelIndex(), row, row)
        self.endInsertRows()

    def pltm_clear(self) -> None:
        self.beginResetModel()
        self.pltm_rows.clear()
        self.endResetModel()

    def pltm_visible_rows(self) -> list[tuple[str, str, str, str]]:
        """当前筛选条件下可见的行。"""
        result: list[tuple[str, str, str, str]] = []
        for row in self.pltm_rows:
            if PMS_LOG_LEVEL_ORDER.get(row[1].lower().capitalize(), 20) < self.pltm_min_level and row[1].upper() not in ("WARN", "ERROR", "FATAL"):
                if PMS_LOG_LEVEL_ORDER.get(row[1].lower(), 20) < self.pltm_min_level:
                    continue
            if self.pltm_filter_text and self.pltm_filter_text not in (row[3] + row[2]).lower():
                continue
            result.append(row)
        return result

    def pltm_export_text(self) -> str:
        return "\n".join(f"{row[0]} [{row[1]}] {row[2]}: {row[3]}" for row in self.pltm_visible_rows())

    def rowCount(self, pms_parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self.pltm_visible_rows())

    def columnCount(self, pms_parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self.pltm_HEADERS)

    def headerData(self, pms_section: int, pms_orientation: Qt.Orientation, pms_role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if pms_role != Qt.ItemDataRole.DisplayRole or pms_orientation != Qt.Orientation.Horizontal:
            return None
        return self.pltm_HEADERS[pms_section] if 0 <= pms_section < len(self.pltm_HEADERS) else None

    def data(self, pms_index: QModelIndex, pms_role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if not pms_index.isValid():
            return None
        rows = self.pltm_visible_rows()
        if pms_index.row() >= len(rows):
            return None
        row = rows[pms_index.row()]
        if pms_role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return row[pms_index.column()]
        if pms_role == Qt.ItemDataRole.ForegroundRole and pms_index.column() == 1:
            level = row[1].upper()
            if level in ("ERROR", "FATAL"):
                return QBrush(QColor("#e5534b"))
            if level in ("WARN", "WARNING"):
                return QBrush(QColor("#d29922"))
            if level == "DEBUG":
                return QBrush(QColor("#9aa4b1"))
            return QBrush(QColor("#3fb950"))
        return None

class PmsConnectionTableModel(QAbstractTableModel):
    """连接列表模型（来自 Clash API 或核心日志）。"""

    pctm_HEADERS = ("开始时间", "协议", "来源", "目标", "主机", "出站", "规则", "上传", "下载", "状态")

    def __init__(self, pms_parent: Optional[QObject] = None) -> None:
        super().__init__(pms_parent)
        self.pctm_rows: list[PmsConnectionRecord] = []

    def pctm_set_records(self, pms_records: Sequence[PmsConnectionRecord]) -> None:
        self.beginResetModel()
        self.pctm_rows = list(pms_records)
        self.endResetModel()

    def pctm_clear(self) -> None:
        self.beginResetModel()
        self.pctm_rows.clear()
        self.endResetModel()

    def pctm_row_at(self, pms_row: int) -> Optional[PmsConnectionRecord]:
        if 0 <= pms_row < len(self.pctm_rows):
            return self.pctm_rows[pms_row]
        return None

    def rowCount(self, pms_parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self.pctm_rows)

    def columnCount(self, pms_parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self.pctm_HEADERS)

    def headerData(self, pms_section: int, pms_orientation: Qt.Orientation, pms_role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if pms_role != Qt.ItemDataRole.DisplayRole or pms_orientation != Qt.Orientation.Horizontal:
            return None
        return self.pctm_HEADERS[pms_section] if 0 <= pms_section < len(self.pctm_HEADERS) else None

    def data(self, pms_index: QModelIndex, pms_role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if not pms_index.isValid():
            return None
        record = self.pctm_row_at(pms_index.row())
        if record is None:
            return None
        values = [
            record.start_time or "-",
            (record.protocol or "-").upper(),
            record.source or "-",
            record.destination or "-",
            record.host or "-",
            record.outbound or "-",
            record.rule or "-",
            pms_format_bytes(record.upload) if record.upload else "-",
            pms_format_bytes(record.download) if record.download else "-",
            "活动中" if record.is_active else "已结束",
        ]
        if pms_role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return values[pms_index.column()] if 0 <= pms_index.column() < len(values) else ""
        if pms_role == Qt.ItemDataRole.TextAlignmentRole and pms_index.column() in (7, 8):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

class PmsLatencyBarDelegate(QStyledItemDelegate):
    """在延迟列绘制小进度条，直观显示快慢（进度条覆盖率 = 100% - 延迟占比）。

    v1.9.23：条的粗细变成**可配**（`plbd_bar_height`，来自设置 `ui.latency_bar_height`）。
    原来高度是从格子高度推出来的（26 px 行高时约 9 px），用户反馈「可以不用这么粗」，
    而且行高一改就跟着变、没得调。现在固定按设置的像素画，**0 = 不画条**（只留文字）。
    """

    plbd_bar_height = 5      # 像素；0 = 不画条。默认 5 比原来的约 9 px 细（用户反馈太粗）
    plbd_side_margin = 3     # 条左右各留几像素，别贴死格子边
    plbd_bottom_margin = 2   # 条离格子下沿几像素

    def paint(self, pms_painter: QPainter, pms_option: Any, pms_index: QModelIndex) -> None:  # noqa: N802
        super().paint(pms_painter, pms_option, pms_index)
        if pms_index.column() != PMS_COLUMN_LATENCY:
            return
        bar_height = int(getattr(self, "plbd_bar_height", 5) or 0)
        if bar_height <= 0:
            return
        node = pms_index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(node, PmsProxyNode) or node.latency_ms is None:
            return
        cell = pms_option.rect
        # 条贴在格子**下沿**：行高小时自动变矮（不会压住上面的文字），行高大时按设置值画
        thickness = max(1, min(bar_height, max(1, cell.height() - self.plbd_bottom_margin - 2)))
        width = max(1, cell.width() - self.plbd_side_margin * 2)
        rect = QRect(
            cell.left() + self.plbd_side_margin,
            cell.bottom() - thickness - self.plbd_bottom_margin + 1,
            width,
            thickness,
        )
        painter = pms_painter
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if node.latency_ms < 0:
            ratio = 1.0
            color = QColor("#e5534b")
        else:
            ratio = max(0.05, min(1.0, 1.0 - (node.latency_ms / 2000.0)))
            grade = node.pnode_latency_color_key()
            color = QColor({"good": "#3fb950", "medium": "#d29922", "slow": "#e5534b"}.get(grade, "#3fb950"))
        painter.fillRect(rect, QColor(color.red(), color.green(), color.blue(), 45))
        filled = QRect(rect.left(), rect.top(), max(1, int(rect.width() * ratio)), rect.height())
        painter.fillRect(filled, color)
        painter.restore()
