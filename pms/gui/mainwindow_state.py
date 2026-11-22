# -*- coding: utf-8 -*-
"""主窗口界面状态同步（列显隐 / 分页 / 筛选 / 排序 / 标题 / 当前节点高亮）——v1.9.36 从 `pms/gui/mainwindow.py` 拆出的 mixin。

★ 拆分原则：**方法名与 `self.pmw_*` 属性名一个字都没改**，外部引用（主窗口之外用到
`pmw_*` 的 80 处）全都不用动；mixins 只是把方法按功能域挪个文件。
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QObject, Qt, QRunnable, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QListWidgetItem, QTableWidgetItem
from pms.constants import (
    PMS_APP_NAME,
    PMS_APP_VERSION,
    PMS_COLUMN_INDEX,
    PMS_COLUMN_LATENCY,
    PMS_COLUMN_SPEED,
    PMS_COLUMN_TITLES,
    PMS_PROTOCOL_TITLES,
    PMS_SORT_SCOPE_ALL,
    PMS_SORT_SCOPE_PAGE,
    PMS_THEME_DARK,
)
from pms.gui.widgets import PMS_TABLE_PAGE_SIZES, PMS_TABLE_PAGE_SIZE_DEFAULT
from pms.gui.mainwindow_tgtabs import (
    PMWT_PAGE_KEY_INDEX,
    PMWT_PAGE_KEY_SIZE,
    PMWT_TAB_ALL,
    PMWT_TAB_TG_PROXY,
    PMWT_TAB_TG_WEBPROXY,
)
from pms.models import PmsProxyNode
from pms.utils import pms_format_latency, pms_format_speed, pms_safe_int
from typing import Any, Optional, Sequence


class _PmwNodesLoadSignals(QObject):
    """后台整库重载的信号载体（v1.9.72 卡顿修复）。

    把「全量重新加载」里最贵的那一步——`pnr_load_all_nodes()`（55k 库实测 13.7s，
    含 SQL 查询 + 逐行构造 + 3 次凭据解密）——挪到 `QThreadPool` 跑，跑完用信号把
    结果回主线程。主线程只做 `pntm_set_nodes`（~42ms）与后续的轻量刷新，UI 不再冻结。
    """

    pmw_loaded = Signal(list, str)   # 加载到的节点列表, 待选中 uid
    pmw_failed = Signal(str)         # 错误信息


class _PmwNodesLoadTask(QRunnable):
    """后台整库加载任务（v1.9.72 卡顿修复）。

    数据库引擎是线程安全的（`PmsDatabaseEngine` 用 `threading.RLock()` 保护，且
    `sqlite3.connect(..., check_same_thread=False)`），所以后台线程跑 `pnr_load_all_nodes`
    安全。任务只负责「读库+构造对象」，绝不碰任何 Qt 控件；结果通过信号回主线程。
    """

    def __init__(self, pms_repo: Any, pms_signals: _PmwNodesLoadSignals, pms_select_uid: str) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.pmw_repo = pms_repo
        self.pmw_signals = pms_signals
        self.pmw_select_uid = str(pms_select_uid or "")

    @Slot()
    def run(self) -> None:  # noqa: D102
        try:
            nodes = self.pmw_repo.pnr_load_all_nodes()
            self.pmw_signals.pmw_loaded.emit(list(nodes), self.pmw_select_uid)
        except Exception as exc:  # noqa: BLE001 - 后台异常必须兜住，否则 QRunnable 会把栈打进 stderr
            self.pmw_signals.pmw_failed.emit(f"{exc}")


class _PmwStateMixin:
    """主窗口界面状态同步（列显隐 / 分页 / 筛选 / 排序 / 标题 / 当前节点高亮）（41 个方法）。"""

    def pmw_restore_ui_state(self) -> None:
        """恢复窗口几何、分隔条、表格列宽/排序/筛选等上次状态。"""
        settings = self.pmw_settings
        if settings.psm_get_bool("ui.remember_geometry", True):
            geometry_text = settings.psm_get_str("ui.window_geometry", "")
            if geometry_text:
                try:
                    self.restoreGeometry(QByteArray.fromBase64(geometry_text.encode("ascii")))
                except (ValueError, TypeError):
                    self.pmw_logger.debug("窗口几何恢复失败")
        for key, splitter in (("ui.horizontal_splitter", getattr(self, "pmw_upper_splitter", None)),
                              ("ui.vertical_splitter", getattr(self, "pmw_main_splitter", None))):
            if splitter is None:
                continue
            sizes = settings.psm_get_list(key)
            if sizes:
                try:
                    splitter.setSizes([pms_safe_int(item, 0, 0) for item in sizes])
                except (TypeError, ValueError):
                    pass
        widths = settings.psm_get_list("ui.table_column_widths")
        if widths:
            for index, width in enumerate(widths[: len(PMS_COLUMN_TITLES)]):
                value = pms_safe_int(width, 0, 0)
                if value > 10:
                    self.pmw_node_table.setColumnWidth(index, value)
        self.pmw_apply_column_visibility()
        column = settings.psm_get_int("ui.table_sort_column", PMS_COLUMN_INDEX)
        order = settings.psm_get_int("ui.table_sort_order", 0)
        self.pmw_node_table.sortByColumn(column, Qt.SortOrder.DescendingOrder if order else Qt.SortOrder.AscendingOrder)
        # v1.9.77：恢复侧栏协议下拉时，若存档值是电报专属页的协议（tg_proxy / tg_webproxy），
        # 一律视为无效并回落到「全部协议」——否则「全部节点」页会被钉死成只显示该协议（库里若为 0 条则全空）。
        _pms_filter_protocol = settings.psm_get_str("ui.filter_protocol", "")
        if _pms_filter_protocol in (PMWT_TAB_TG_PROXY, PMWT_TAB_TG_WEBPROXY):
            _pms_filter_protocol = ""
            settings.psm_set("ui.filter_protocol", "", pms_emit=False)
        self.pmw_protocol_combo.setCurrentIndex(max(0, self.pmw_protocol_combo.findData(_pms_filter_protocol)))
        self.pmw_search_edit.setText(settings.psm_get_str("ui.filter_search", ""))
        self.pmw_chk_only_enabled.setChecked(False)
        row_height = settings.psm_get_int("ui.table_row_height", 26)
        self.pmw_node_table.verticalHeader().setDefaultSectionSize(row_height)
        # v1.9.23：延迟列进度条粗细（0 = 不画条）
        self.pmw_apply_latency_bar_height()
        font = QFont(self.pmw_node_table.font())
        font.setPointSizeF(settings.psm_get_float("ui.table_font_size", 9.5))
        self.pmw_node_table.setFont(font)
        self.pmw_node_model.pntm_set_theme_dark(self.pmw_theme.pthm_resolve(settings.psm_get_str("ui.theme", PMS_THEME_DARK)) == PMS_THEME_DARK)
        self.pmw_chk_only_enabled.setChecked(False)
        # v1.7.0：先取上次页码。下面 on_filter_changed() 会把页码重置回第 1 页并写盘，
        # 所以必须在它**之前**把值读出来，否则「页码记忆」永远只剩第 1 页。
        # v1.9.76：恢复顺序改成「先全部节点页、后两个电报专属页」——两个新页各自读自己的键，
        # 页码互不干扰；恢复期间活动页临时切回「全部节点」，与旧版启动行为一致。
        pms_restore_page = max(1, settings.psm_get_int("ui.table_page_index", 1))
        if hasattr(self, "pmwt_views") and self.pmwt_views:
            saved_index = self.pmwt_tabs.currentIndex()
            self.pmwt_tabs.setCurrentIndex(0)
        self.pmw_on_filter_changed()
        # v1.7.0：分页初值（每页条数下拉 + 上次页码）。
        self.pmw_applying_page_size = True
        try:
            size = self.pmw_page_size()
            index = self.pmw_combo_page_size.findData(size)
            self.pmw_combo_page_size.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.pmw_applying_page_size = False
        self.pmw_apply_pagination(pms_restore_page)
        # v1.9.76：两个电报专属页也把各自记忆的页码恢复出来（每页条数与全部节点页共用一份）
        if hasattr(self, "pmwt_views") and self.pmwt_views:
            for _pmwt_key in (PMWT_TAB_TG_PROXY, PMWT_TAB_TG_WEBPROXY):
                _pmwt_view = self.pmwt_views.get(_pmwt_key)
                if _pmwt_view is None:
                    continue
                _pmwt_page = max(1, settings.psm_get_int(PMWT_PAGE_KEY_INDEX[_pmwt_key], 1))
                _pmwt_view.pmwt_page.pnfp_set_pagination(_pmwt_page, self.pmw_page_size())
                self.pmwt_sync_pagination_bar(_pmwt_view)
            self.pmwt_tabs.setCurrentIndex(saved_index)
        # v1.8.0：启动时先看有没有上次没跑完的测试，把「继续上次测试」按钮点亮
        self.pmw_refresh_resume_action()
        self.pmw_reload_error_panel()

    def pmw_apply_column_visibility(self) -> None:
        """应用「节点表格显示列」设置（v1.6.7：设置一改就立刻生效，不用重启）。

        存储值可能是布尔 / 字符串；长度对不上时按「全部显示」处理，绝不抛异常。
        """
        visible = self.pmw_settings.psm_get_list("ui.table_column_visible")
        if len(visible) != len(PMS_COLUMN_TITLES):
            visible = ["true"] * len(PMS_COLUMN_TITLES)
        for index, flag in enumerate(visible):
            self.pmw_node_table.setColumnHidden(index, str(flag).lower() not in ("true", "1", "yes"))

    def pmw_save_ui_state(self) -> None:
        """保存窗口几何、分隔条、列宽、排序与筛选状态（退出前与定期调用）。"""
        settings = self.pmw_settings
        if settings.psm_get_bool("ui.remember_geometry", True):
            geometry = self.saveGeometry().toBase64().data().decode("ascii")
            settings.psm_set("ui.window_geometry", geometry, pms_emit=False)
        settings.psm_set("ui.horizontal_splitter", list(self.pmw_upper_splitter.sizes()), pms_emit=False)
        settings.psm_set("ui.vertical_splitter", list(self.pmw_main_splitter.sizes()), pms_emit=False)
        settings.psm_set("ui.table_column_widths", [self.pmw_node_table.columnWidth(index) for index in range(len(PMS_COLUMN_TITLES))], pms_emit=False)
        header = self.pmw_node_table.horizontalHeader()
        # v1.9.14：这两个键记的是**全量排序**（启动恢复、以及「本页」范围下撤销全量排序都要用它）。
        # 设置成「本页」时表头可能停在某个只排过本页的列上，此时不能拿它覆盖全量排序记录。
        if settings.psm_get_str("ui.header_sort_scope", PMS_SORT_SCOPE_ALL) == PMS_SORT_SCOPE_ALL:
            settings.psm_set("ui.table_sort_column", int(header.sortIndicatorSection()), pms_emit=False)
            settings.psm_set("ui.table_sort_order", int(header.sortIndicatorOrder().value), pms_emit=False)
        # v1.9.77：仅在「全部节点」页才写 ui.filter_protocol（它是全部节点页自己的筛选器）。
        # 电报专属页期间侧栏协议下拉被禁用且停在上一个 tg 协议，若照写会把全部节点页永久过滤空。
        if getattr(self, "pmwt_active_key", lambda: PMWT_TAB_ALL)() == PMWT_TAB_ALL:
            settings.psm_set("ui.filter_protocol", str(self.pmw_protocol_combo.currentData() or ""), pms_emit=False)
        settings.psm_set("ui.filter_search", self.pmw_search_edit.text(), pms_emit=False)
        # v1.7.0：页码与每页条数（翻页时已实时写入，这里补一次确保落盘）。
        # v1.9.76：两个电报专属标签页也各存一份（键名见 PMWT_PAGE_KEY_*），
        # 这样「全部节点停在 3 页、TgProxy 停在 1 页」能分别记住。
        for _pmwt_key in (PMWT_TAB_ALL, PMWT_TAB_TG_PROXY, PMWT_TAB_TG_WEBPROXY):
            _pmwt_view = getattr(self, "pmwt_views", {}).get(_pmwt_key)
            if _pmwt_view is None:
                continue
            settings.psm_set(PMWT_PAGE_KEY_SIZE[_pmwt_key], _pmwt_view.pmwt_page.pnfp_page_size, pms_emit=False)
            settings.psm_set(PMWT_PAGE_KEY_INDEX[_pmwt_key], max(1, int(_pmwt_view.pmwt_page.pnfp_page)), pms_emit=False)
        # v1.9.13：「上次使用的节点」在这里补一次落地——退出前那一瞬间的当前节点才是用户心里的
        # 「上次的节点」，不能只依赖「启动核心时写过一次」（核心停掉后再也不写，中途换过节点的
        # 情况也可能漏掉）。
        settings.psm_set("general.last_node_uid", str(self.pmw_current_node_uid or ""), pms_emit=False)
        settings.psm_flush()

    def pmw_restore_last_node(self) -> None:
        """启动时恢复「上次使用的节点」为当前节点（v1.9.13）。

        两个开关各管各的（用户明确选择的口径）：
            general.auto_connect_last_node（默认开）→ 本方法：把上次的节点恢复成**当前节点**；
            general.auto_start_core（默认开）→ 由 pms/app.py 的定时器调 pmw_toggle_core() 启动核心，
            而 pmw_toggle_core() 优先用 pmw_current_node()，于是「启动的就是恢复出来的那个节点」。
        两个都开（默认）= 开程序即连上上次退出前用的节点。

        **故意不调 pmw_highlight_current_node()**：那个方法为了定位节点会 `pmw_reveal_node()`
        切页，会把 v1.7.0 的「页码记忆」顶掉。这里只置「当前节点」+ 名称列高亮 + 标签，
        等真正启动核心时（pmw_start_core 内部）再做完整的高亮与定位。
        """
        if not self.pmw_settings.psm_get_bool("general.auto_connect_last_node", True):
            return
        if not self.pmw_nodes:
            # 库里一个节点都没有：别把记着的 uid 清掉，下次有节点了还能用
            return
        uid = str(self.pmw_settings.psm_get_str("general.last_node_uid", "") or "")
        if not uid:
            return
        node = self.pmw_node_model.pntm_node_by_uid(uid)
        if node is None:
            # 记着的节点已经不在了（被删 / 被订阅更新移除）：清掉悬空 uid，免得每次启动都白找
            self.pmw_settings.psm_set("general.last_node_uid", "")
            self.pmw_logger.info("上次使用的节点已不存在（uid=%s），已清除记录", uid)
            return
        self.pmw_current_node_uid = node.uid
        self.pmw_node_model.pntm_active_uid = node.uid
        self.pmw_node_model.pntm_update_node(node)
        self.pmw_set_current_node_label(f"当前节点：{node.pnode_display_name()}（{node.pnode_display_address()}）")
        self.pmw_logger.info("已恢复上次使用的节点：%s", node.pnode_display_name())

    def pmw_reload_nodes(self, pms_select_uid: str = "") -> None:
        """从数据库重新加载节点并刷新界面（**同步**全量重载）。

        ⚠️ 55k 节点库下，这里会因为 `pnr_load_all_nodes()` 的逐行构造（实测 ~13.7s）
        冻结主线程。能走增量刷新的地方请改调 `pmw_apply_nodes_added/removed/updated`；
        需要整库重读的场合（启动 / 导入 / 订阅完成 / 手动刷新）请改调
        `pmw_reload_nodes_async()`，把最重的那一步挪到后台线程。
        """
        self.pmw_loading = True
        try:
            self.pmw_nodes = self.pmw_repo.pnr_load_all_nodes()
            for index, node in enumerate(self.pmw_nodes):
                node.sort_index = index
            if pms_select_uid:
                self.pmw_current_node_uid = str(pms_select_uid)
            self.pmw_node_model.pntm_set_nodes(self.pmw_nodes)
            # v1.7.0：节点增删后总页数会变，这里重算分页并夹取页码（不回第 1 页，保留用户翻到哪）
            self.pmw_apply_pagination()
            self.pmw_reload_groups()
            self.pmw_refresh_summary()
            self.pmw_reload_history_table()
            if self.pmw_current_node_uid:
                self.pmw_highlight_current_node()
            else:
                # v1.9.26：当前节点被删掉后标签与标题不能还挂着它（原来只清 uid，界面留着旧名字）
                self.pmw_set_current_node_label("当前节点：-")
            self.pmw_update_connect_actions()
        finally:
            self.pmw_loading = False

    def pmw_reload_nodes_async(self, pms_select_uid: str = "", pms_on_done: Optional[Any] = None) -> None:
        """后台整库重载（v1.9.72 卡顿修复）。

        把 `pnr_load_all_nodes()`（55k 节点 ~13.7s 主线程冻结）挪到 `QThreadPool` 跑，
        主线程只做回包后的轻量刷新。用于在「全量重读」无法避免的场合（启动 / 导入 /
        订阅完成 / 手动刷新 `Ctrl+R`）替代同步 `pmw_reload_nodes()`，让大库下 UI 不再卡死。

        `pms_on_done`：可选回调，回包后在主线程执行（签名 `callback(select_uid: str)`），
        用于把「加载完成后才该发生」的事（如启动恢复上次节点、自动启动核心、启动订阅）
        延迟到数据就绪之后，避免 Race：控件已显示、数据还没到。
        """
        from PySide6.QtCore import QThreadPool

        if self.pmw_loading:
            # 已经有一次整库重载在飞：后来的重复请求直接忽略，避免重复构造 / 信号打架
            return
        self.pmw_loading = True
        signals = _PmwNodesLoadSignals()
        signals.pmw_loaded.connect(lambda nodes, uid: self.pmw_on_nodes_loaded(nodes, uid, pms_on_done))
        signals.pmw_failed.connect(lambda err: self.pmw_on_nodes_load_failed(err))
        task = _PmwNodesLoadTask(self.pmw_repo, signals, pms_select_uid)
        # v1.9.72 卡顿修复（防 GC）：PySide6 里 QRunnable 设了 setAutoDelete(True) 后，
        # 所有权转给 C++ 线程池，但 Python 包装对象若没有强引用会被提前回收，
        # 导致任务在被线程池取到之前就被析构（activeThreadCount 一直为 0、pmw_loading 卡死）。
        # 这里把 task / signals 挂在 self 上保活，等回包（成功或失败）后再清掉。
        self.pmw_load_task = task
        self.pmw_load_signals = signals
        QThreadPool.globalInstance().start(task)

    @Slot(list, str, object)
    def pmw_on_nodes_loaded(self, pms_nodes: list, pms_select_uid: str, pms_on_done: Optional[Any]) -> None:
        """后台整库加载完成（主线程）：落内存 + 装模型 + 轻量刷新 + 跑后续回调。"""
        self.pmw_nodes = list(pms_nodes)
        for index, node in enumerate(self.pmw_nodes):
            node.sort_index = index
        if pms_select_uid:
            self.pmw_current_node_uid = str(pms_select_uid)
        self.pmw_node_model.pntm_set_nodes(self.pmw_nodes)
        self.pmw_apply_pagination()
        self.pmw_reload_groups()
        self.pmw_refresh_summary()
        self.pmw_reload_history_table()
        if self.pmw_current_node_uid:
            self.pmw_highlight_current_node()
        else:
            self.pmw_set_current_node_label("当前节点：-")
        self.pmw_update_connect_actions()
        self.pmw_loading = False
        # v1.9.72：回包后释放后台任务引用（防 GC 的保活引用，避免内存泄漏 / 下次重载串味）
        self.pmw_load_task = None
        self.pmw_load_signals = None
        if pms_on_done is not None:
            try:
                pms_on_done(pms_select_uid)
            except Exception as exc:  # noqa: BLE001 - 回调异常不能把进度/状态搞乱
                self.pmw_logger.warning("后台加载完成后的回调异常（已忽略）：%s", exc)

    @Slot(str)
    def pmw_on_nodes_load_failed(self, pms_error: str) -> None:
        """后台整库加载失败（主线程）：兜底回退到同步重载，保证界面不空。"""
        self.pmw_loading = False
        self.pmw_load_task = None
        self.pmw_load_signals = None
        self.pmw_logger.error("后台整库加载失败，回退同步重载：%s", pms_error)
        try:
            self.pmw_reload_nodes("")
        except Exception as exc:  # noqa: BLE001 - 回退也失败就别再炸
            self.pmw_logger.error("同步重载兜底也失败：%s", exc)
            self.pmw_show_message(f"加载节点库失败：{exc}")

    def pmw_on_startup_after_load(self) -> None:
        """启动整库加载完成回调（主线程）：执行依赖节点库就绪的一次性启动动作。

        这些动作原本在 `PmsMainWindow.__init__` 末尾靠固定延迟（600ms / 1200ms）抢跑，
        但启动加载已改后台异步（55k 库 ~13.7s），固定延迟必然会落在「库还没读到」的窗口里。
        统一收拢到「加载完成」这一刻再触发，避免 Race：

            · 恢复上次使用的节点（auto_connect_last_node，默认开）；
            · 自动启动核心（general.auto_start_core，由 app.py 置 `pmw_startup_auto_core`）；
            · 启动订阅更新（subscription.update_on_launch，由 app.py 置 `pmw_startup_auto_subscription`）。
        """
        if not getattr(self, "pmw_nodes", None):
            self.pmw_logger.warning("启动加载完成但节点库为空，跳过自动启动核心 / 订阅")
        else:
            # 1) 先恢复「上次使用的节点」为当前节点（auto_start_core 会优先连它）
            try:
                self.pmw_restore_last_node()
            except Exception as exc:  # noqa: BLE001
                self.pmw_logger.warning("启动恢复上次节点异常（已忽略）：%s", exc)
            # 2) 自动启动核心（依赖当前节点已就绪）
            if getattr(self, "pmw_startup_auto_core", False):
                try:
                    self.pmw_toggle_core()
                except Exception as exc:  # noqa: BLE001
                    self.pmw_logger.warning("启动自动启动核心异常（已忽略）：%s", exc)
            # 3) 启动订阅更新（依赖节点库就绪）
            if getattr(self, "pmw_startup_auto_subscription", False):
                try:
                    self.pmw_on_auto_subscription_tick()
                except Exception as exc:  # noqa: BLE001
                    self.pmw_logger.warning("启动订阅更新异常（已忽略）：%s", exc)

    # ------------------------------------------------------------------ 增量刷新（v1.9.72 卡顿修复）
    def pmw_apply_nodes_added(self, pms_nodes: Sequence[PmsProxyNode], pms_select_uid: str = "") -> None:
        """增量新增节点（新增 / 复制 / 单个导入）。

        把节点就地加入 `self.pmw_nodes` 并走模型层 `pntm_insert_nodes`（只发 `rowsInserted`，
        不再 `beginResetModel`），避免 55k 库下 `pnr_load_all_nodes()` 13.7s 全量重载。
        新增后置当前节点、刷新分组计数与汇总、应用分页（重算总页数，不跳页）。
        """
        if not pms_nodes:
            return
        self.pmw_nodes.extend(pms_nodes)
        if pms_select_uid:
            self.pmw_current_node_uid = str(pms_select_uid)
        self.pmw_node_model.pntm_insert_nodes(pms_nodes)
        if pms_select_uid:
            self.pmw_highlight_current_node()
        self.pmw_apply_pagination()
        self.pmw_refresh_summary()
        self.pmw_update_connect_actions()

    def pmw_apply_nodes_removed(self, pms_uids: Sequence[str]) -> None:
        """增量删除节点（删除选中 / 批量删除）。

        与 `pmw_apply_nodes_added` 对称：只动被删的那几行（模型层 `pntm_remove_nodes_by_uids`
        发 `rowsRemoved`），整库不重载。被删节点若是当前节点，则清掉高亮与记录。
        """
        if not pms_uids:
            return
        uid_set = {str(uid) for uid in pms_uids}
        self.pmw_nodes = [node for node in self.pmw_nodes if node.uid not in uid_set]
        if self.pmw_current_node_uid in uid_set:
            self.pmw_current_node_uid = ""
            self.pmw_settings.psm_set("general.last_node_uid", "")
            self.pmw_set_current_node_label("当前节点：-")
        self.pmw_node_model.pntm_remove_nodes_by_uids(list(uid_set))
        self.pmw_apply_pagination()
        self.pmw_refresh_summary()
        self.pmw_update_connect_actions()

    def pmw_apply_nodes_updated(self, pms_nodes: Sequence[PmsProxyNode]) -> None:
        """增量更新若干节点的字段（批量启用/停用/重命名/移动分组/去重后保留项）。

        节点集合不变、只改字段，走 `pntm_update_node`（逐行 `dataChanged`，触发筛选代理按需
        增量重建一次）。**不再**调用 `pmw_reload_nodes()`。
        """
        if not pms_nodes:
            return
        for node in pms_nodes:
            self.pmw_update_node_everywhere_inline(node)
        self.pmw_refresh_summary()

    def pmw_update_node_everywhere_inline(self, pms_node: PmsProxyNode) -> None:
        """就地替换内存中的节点并刷新单行（不重排整表）。"""
        for index, node in enumerate(self.pmw_nodes):
            if node.uid == pms_node.uid:
                self.pmw_nodes[index] = pms_node
                break
        self.pmw_node_model.pntm_update_node(pms_node)

    def pmw_reload_groups(self) -> None:
        """重建分组列表（保留当前选择）。

        v1.9.16 两处修正：
        1. 原来拿 ``currentItem().text()``（形如「香港（12）」）去和 ``UserRole`` 里的裸分组名比，
           永远比不中 → 每次重建分组都跳回「全部分组」，设置项 `ui.filter_group` 只写不读。
           现在比 ``UserRole``。
        2. 首次加载（列表还是空的）时，从设置里恢复上次选的分组，这样重启后筛选是接着上次的。
        """
        item = self.pmw_group_list.currentItem()
        if item is not None:
            current = str(item.data(Qt.ItemDataRole.UserRole) or "")
        else:
            current = self.pmw_settings.psm_get_str("ui.filter_group", "")
        self.pmw_group_list.blockSignals(True)
        self.pmw_group_list.clear()
        summary = self.pmw_repo.pnr_group_summary()
        all_item = QListWidgetItem(f"全部分组（{len(self.pmw_nodes)}）")
        all_item.setData(Qt.ItemDataRole.UserRole, "")
        self.pmw_group_list.addItem(all_item)
        for name, count in summary:
            item = QListWidgetItem(f"{name}（{count}）")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.pmw_group_list.addItem(item)
        target_row = 0
        for row in range(self.pmw_group_list.count()):
            if self.pmw_group_list.item(row).data(Qt.ItemDataRole.UserRole) == current:
                target_row = row
                break
        self.pmw_group_list.setCurrentRow(target_row)
        self.pmw_group_list.blockSignals(False)
        # v1.9.16：走「不跳页」版本——重建分组列表是节点刷新的一部分，
        # 用 pmw_on_filter_changed() 会把启动恢复的页码冲回第 1 页
        self.pmw_reapply_filters(False)

    def pmw_selected_nodes(self) -> list[PmsProxyNode]:
        """当前选中的节点（按表格顺序）。

        v1.7.0 分页后：表格里只有当前页的行，「全选」的意思却是**全部筛选结果**（跨页）。
        所以点全选时把范围记在 ``pmw_select_all_scope``，这里直接返回全量筛选结果；
        否则返回表格真实选中的行。

        v1.9.67：「按类型选择」也复用 ``pmw_select_all_scope``，但范围**不是全部筛选结果**，
        而是其中某一协议类型的子集。若这里无脑返回 `pmw_visible_nodes()`，「按类型选中」后
        再执行清除 / 批量删除 / 导出就会**误伤全部节点**——所以按类型时必须走 uid 白名单过滤。
        """
        if self.pmw_select_all_scope == "filtered":
            # 按类型选择时带 uid 白名单：只认白名单里的节点，避免误伤其它类型
            uids = getattr(self, "pmw_select_by_type_uids", None)
            if uids:
                return [node for node in self.pmw_visible_nodes() if node.uid in uids]
            return self.pmw_visible_nodes()
        rows = self.pmw_node_table.selectionModel().selectedRows() if self.pmw_node_table.selectionModel() else []
        nodes: list[PmsProxyNode] = []
        for index in sorted(rows, key=lambda item: item.row()):
            # 视图 → 分页层 → 筛选层 → 源模型，两层都要映射回去。
            source_index = self.pmw_node_proxy.mapToSource(self.pmw_node_page.mapToSource(index))
            node = self.pmw_node_model.pntm_node_at(source_index.row())
            if node is not None:
                nodes.append(node)
        return nodes

    def pmw_visible_nodes(self) -> list[PmsProxyNode]:
        """当前筛选条件下的**全部**节点（跨页，不受分页影响）。

        v1.7.0：分页放在第二层代理上，所以这里遍历的仍是筛选层的全量结果。
        「测试全部延迟」/ 批量操作 / 导出都走它，语义与分页前完全一致。
        """
        nodes: list[PmsProxyNode] = []
        for row in range(self.pmw_node_proxy.rowCount()):
            source_index = self.pmw_node_proxy.mapToSource(self.pmw_node_proxy.index(row, 0))
            node = self.pmw_node_model.pntm_node_at(source_index.row())
            if node is not None:
                nodes.append(node)
        return nodes

    def pmw_page_nodes(self) -> list[PmsProxyNode]:
        """**当前页**的节点（按表格顺序，v1.7.0 新增）。"""
        nodes: list[PmsProxyNode] = []
        for row in range(self.pmw_node_page.rowCount()):
            source_index = self.pmw_node_proxy.mapToSource(self.pmw_node_page.mapToSource(self.pmw_node_page.index(row, 0)))
            node = self.pmw_node_model.pntm_node_at(source_index.row())
            if node is not None:
                nodes.append(node)
        return nodes

    def pmw_page_index(self) -> int:
        """当前页码（1 起）。"""
        return max(1, int(self.pmw_node_page.pnfp_page))

    def pmw_page_count(self) -> int:
        """总页数（至少 1）。"""
        return self.pmw_node_page.pnfp_page_count()

    def pmw_page_size(self) -> int:
        """当前每页条数；0 表示不分页。

        v1.9.76：TgProxy / TgWebProxy 两个标签页各记一份（键名见 `PMWT_PAGE_KEY_SIZE`），
        「全部节点」页仍用旧的 `ui.table_page_size`，老设置与行为完全不变。
        """
        key = self.pmwt_active_page_key_size()
        size = self.pmw_settings.psm_get_int(key, PMS_TABLE_PAGE_SIZE_DEFAULT)
        return size if size in PMS_TABLE_PAGE_SIZES else PMS_TABLE_PAGE_SIZE_DEFAULT

    def pmw_apply_pagination(self, pms_page: int = 0, pms_reset_to_first: bool = False) -> None:
        """按设置重算分页并把分页栏显示同步好。

        ``pms_reset_to_first`` 用于筛选 / 排序 / 重新加载之后——结果集变了，
        继续停在第 8 页很可能是一片空白，必须回第 1 页。
        """
        size = self.pmw_page_size()
        key_size = self.pmwt_active_page_key_size()
        key_index = self.pmwt_active_page_key_index()
        wanted = 1 if pms_reset_to_first else (int(pms_page) or self.pmw_settings.psm_get_int(key_index, 1))
        page, _pages = self.pmw_node_page.pnfp_set_pagination(wanted, size)
        self.pmw_settings.psm_set(key_size, size, pms_emit=False)
        # v1.9.16：节点库还没载入时**不回写页码**。那一刻只有 1 页，回写会把「上次停在第 N 页」
        # 冲成 1，等节点载入后再读设置就只剩第 1 页——「页码记忆」在全新启动时失效的老毛病就是这个。
        # v1.9.76：改成按活动标签页回写（键名见 pmwt_active_page_key_index），各页各自记忆。
        if self.pmw_node_model.rowCount() > 0:
            self.pmw_settings.psm_set(key_index, page, pms_emit=False)
        self.pmw_sync_pagination_bar()

    def pmw_goto_page(self, pms_page: int) -> None:
        """翻到指定页（超出范围会被夹取）。

        注意：**不要**在这里调 pmw_highlight_current_node——它会为了定位当前节点
        把页码又切回去，翻页就永远翻不动了。
        """
        # 先取快照：apply_pagination 会 invalidate 掉当前页的行 → 选中被清空 →
        # selectionChanged 里 pmw_clear_selection_scope 会把范围标记抹掉。
        pms_scope = self.pmw_select_all_scope
        self.pmw_apply_pagination(int(pms_page))
        if pms_scope == "filtered":
            # 跨页全选中翻页：新页的行也要整页选上，视觉才一致。
            self.pmw_applying_selection = True
            try:
                self.pmw_node_table.selectAll()
            finally:
                self.pmw_applying_selection = False
            self.pmw_select_all_scope = "filtered"
            self.pmw_refresh_selection_label()

    def pmw_on_page_size_changed(self) -> None:
        """每页条数改变：回到第 1 页并立即保存（宪法第 15 条）。"""
        if getattr(self, "pmw_applying_page_size", False):
            return
        value = self.pmw_combo_page_size.currentData()
        size = pms_safe_int(value, PMS_TABLE_PAGE_SIZE_DEFAULT, 0)
        self.pmw_settings.psm_set(self.pmwt_active_page_key_size(), size)
        self.pmw_apply_pagination(1, pms_reset_to_first=True)

    def pmw_sync_pagination_bar(self) -> None:
        """把页码 / 总页数 / 按钮可用性刷到分页栏上。"""
        page = self.pmw_page_index()
        pages = self.pmw_page_count()
        total = self.pmw_node_proxy.rowCount()
        size = self.pmw_node_page.pnfp_page_size
        if size > 0 and total:
            start = (page - 1) * size + 1
            end = min(total, page * size)
            self.pmw_label_page.setText(f"第 {page} / {pages} 页（第 {start}-{end} 条，共 {total} 条）")
        else:
            self.pmw_label_page.setText(f"共 {total} 条")
        self.pmw_btn_first_page.setEnabled(page > 1)
        self.pmw_btn_prev_page.setEnabled(page > 1)
        self.pmw_btn_next_page.setEnabled(page < pages)
        self.pmw_btn_last_page.setEnabled(page < pages)
        self.pmw_refresh_selection_label()

    def pmw_refresh_selection_label(self) -> None:
        """显示当前到底选中了什么范围（分页后这点必须说清楚，否则用户会以为只选了一页）。"""
        if self.pmw_select_all_scope == "filtered":
            self.pmw_label_selection.setText(f"已选全部筛选结果 {self.pmw_node_proxy.rowCount()} 条")
        else:
            count = len(self.pmw_selected_nodes())
            self.pmw_label_selection.setText(f"已选 {count} 条（本页内）" if count else "")

    def pmw_clear_selection_scope(self) -> None:
        """用户手动改选 → 取消「全选筛选结果」这个虚拟范围。"""
        if self.pmw_select_all_scope and not self.pmw_applying_selection:
            self.pmw_select_all_scope = ""
            self.pmw_select_by_type_uids = set()   # v1.9.67：范围作废，类型白名单必须一并失效
            self.pmw_refresh_selection_label()

    def pmw_select_all_filtered(self) -> None:
        """全选 = **全部筛选结果**（跨页）。

        表格只渲染当前页，所以这里额外把当前页整页选中做视觉反馈，
        真正的范围记在 ``pmw_select_all_scope`` 上，供测试 / 批量操作 / 导出使用。

        v1.9.67：必须先清掉「按类型选择」留下的 uid 白名单，否则全选会被上一次的类型筛选截断。
        """
        total = self.pmw_node_proxy.rowCount()
        if total <= 0:
            return
        self.pmw_select_by_type_uids = set()   # 全选没有类型限制
        self.pmw_applying_selection = True
        try:
            self.pmw_select_all_scope = "filtered"
            self.pmw_node_table.selectAll()
        finally:
            self.pmw_applying_selection = False
        self.pmw_update_connect_actions()
        self.pmw_refresh_selection_label()
        self.pmw_show_message(f"已选中全部筛选结果 {total} 条（跨页，不只是当前页）")

    def pmw_select_current_page(self) -> None:
        """只选中当前页的节点（需要只操作眼前这一屏时用）。"""
        self.pmw_applying_selection = True
        try:
            self.pmw_select_all_scope = ""
            self.pmw_select_by_type_uids = set()   # v1.9.67：当前页选择没有类型范围
            self.pmw_node_table.selectAll()
        finally:
            self.pmw_applying_selection = False
        self.pmw_update_connect_actions()
        self.pmw_refresh_selection_label()
        self.pmw_show_message(f"已选中当前页 {self.pmw_node_page.rowCount()} 条")

    def pmw_reveal_node(self, pms_node: "PmsProxyNode") -> int:
        """把节点所在页切出来，返回它在**当前页**里的行号；不在筛选结果里返回 -1。"""
        for filtered_row in range(self.pmw_node_proxy.rowCount()):
            source_index = self.pmw_node_proxy.mapToSource(self.pmw_node_proxy.index(filtered_row, 0))
            if self.pmw_node_model.pntm_node_at(source_index.row()) is not pms_node:
                continue
            target_page = self.pmw_node_page.pnfp_page_of_source_row(filtered_row)
            if target_page != self.pmw_page_index():
                self.pmw_apply_pagination(target_page)
            size = self.pmw_node_page.pnfp_page_size
            return filtered_row if size <= 0 else filtered_row - (self.pmw_page_index() - 1) * size
        return -1

    def pmw_node_index(self) -> dict[str, PmsProxyNode]:
        """uid → 节点 的索引（结果回调的热路径，必须 O(1)）。

        v1.9.27：列表被换过（重新加载节点）或长度变了就重建一次。
        校验用「对象身份 + 长度」两个 O(1) 判断，保证索引永不返回过期节点。
        """
        if self.pmw_node_index_source is not self.pmw_nodes or len(self.pmw_node_index_cache) != len(self.pmw_nodes):
            self.pmw_node_index_cache = {node.uid: node for node in self.pmw_nodes}
            self.pmw_node_index_source = self.pmw_nodes
        return self.pmw_node_index_cache

    def pmw_node_by_uid(self, pms_uid: str) -> Optional[PmsProxyNode]:
        """按 uid 取节点（找不到返回 None）。测试结果回调一律走它，不要再用线性查找。"""
        return self.pmw_node_index().get(str(pms_uid))

    def pmw_current_node(self) -> Optional[PmsProxyNode]:
        """当前用于连接的核心节点。"""
        if not self.pmw_current_node_uid:
            return None
        return self.pmw_node_by_uid(self.pmw_current_node_uid)

    def pmw_highlight_current_node(self) -> None:
        """高亮并选中当前连接节点。"""
        node = self.pmw_current_node()
        if node is None:
            return
        self.pmw_node_model.pntm_active_uid = node.uid
        row = self.pmw_reveal_node(node)
        if row >= 0:
            self.pmw_node_table.selectRow(row)
            self.pmw_node_table.scrollTo(self.pmw_node_page.index(row, 0))
        self.pmw_set_current_node_label(f"当前节点：{node.pnode_display_name()}（{node.pnode_display_address()}）")

    def pmw_window_title(self) -> str:
        """窗口标题：程序名 + 版本；有当前节点时补上「名称 [类型] - 地址」。

        v1.9.26：用户要在标题栏一眼看到现在用的是哪个节点。
        """
        title = f"{PMS_APP_NAME} v{PMS_APP_VERSION}"
        node = self.pmw_current_node()
        if node is None:
            return title
        kind = PMS_PROTOCOL_TITLES.get(node.protocol) or node.protocol
        return f"{title}　—　{node.pnode_display_name()} [{kind}] - {node.pnode_display_address()}"

    def pmw_apply_window_title(self) -> None:
        """把标题重新算一遍并写回（内容没变就不动，避免任务栏反复重绘）。"""
        title = self.pmw_window_title()
        if self.windowTitle() != title:
            self.setWindowTitle(title)

    def pmw_set_current_node_label(self, pms_text: str) -> None:
        """统一更新「当前节点」标签与窗口标题——两处显示必须永远一致。"""
        self.pmw_label_current_node.setText(pms_text)
        self.pmw_label_current_node.setToolTip(pms_text)
        self.pmw_apply_window_title()

    def pmw_show_core_fail(self) -> bool:
        """失败节点的延迟列/标签显示什么：False=「超时」（默认），True=核心原话（HTTP 503 / 504…）。

        v1.9.21：用户看着一屏「超时」分不清是节点自己坏了（503）还是太慢（504），
        设置里给一个开关把核心的原话直接摊到列表上；完整原因一直留在提示气泡与「最近测试记录」里。
        """
        return bool(self.pmw_settings.psm_get_bool("test.fail_show_core", False))

    def pmw_apply_fail_text_mode(self) -> None:
        """把「失败提示口径」灌进模型并重画延迟列（**只发一次 dataChanged，不整表重置**）。

        v1.9.21：整表重置在 3 万节点下要 18 秒以上（见 7.39），而这里只改了显示口径、
        数据一点没变——发一个「延迟列整列变了」的信号就够了，视图只会重画看得见的那几行。
        """
        show_core = self.pmw_show_core_fail()
        if bool(getattr(self.pmw_node_model, "pntm_show_core_fail", False)) == show_core:
            return
        self.pmw_node_model.pntm_show_core_fail = show_core
        row_count = self.pmw_node_model.rowCount()
        if row_count:
            self.pmw_node_model.dataChanged.emit(
                self.pmw_node_model.index(0, PMS_COLUMN_LATENCY),
                self.pmw_node_model.index(row_count - 1, PMS_COLUMN_LATENCY),
                [Qt.ItemDataRole.DisplayRole],
            )
        node = self.pmw_node_by_uid(self.pmw_current_node_uid)
        if node is not None:
            self.pmw_label_latency.setText(f"延迟：{node.pnode_display_latency(show_core)}")
            self.pmw_set_current_node_label(
                f"当前节点：{node.pnode_display_name()}（延迟 {node.pnode_display_latency(show_core)}）"
            )

    def pmw_refresh_summary(self) -> None:
        """刷新节点数量统计文本。"""
        total = len(self.pmw_nodes)
        enabled = sum(1 for node in self.pmw_nodes if node.enabled)
        tested = sum(1 for node in self.pmw_nodes if node.latency_ms is not None and node.latency_ms >= 0)
        visible = self.pmw_node_proxy.rowCount()
        page_rows = self.pmw_node_page.rowCount()
        # v1.7.0：分页后两个数都要说清楚——筛选结果多少条、当前页多少条
        self.pmw_node_summary_label.setText(
            f"节点总数 {total}（启用 {enabled}，已测出延迟 {tested}），筛选 {visible} 条，本页 {page_rows} 条"
        )
        self.pmw_label_current_node.setToolTip(self.pmw_label_current_node.text())

    def pmw_on_node_toggled(self, pms_uid: str, pms_enabled: bool) -> None:
        """表格勾选启用状态 → 立即写库。"""
        self.pmw_repo.pnr_update_enabled([str(pms_uid)], bool(pms_enabled))
        for node in self.pmw_nodes:
            if node.uid == str(pms_uid):
                node.enabled = bool(pms_enabled)
                break
        self.pmw_refresh_summary()
        self.pmw_update_connect_actions()

    def pmw_update_node_everywhere(self, pms_node: PmsProxyNode) -> None:
        """节点数据变化后刷新内存与模型。"""
        for index, existing in enumerate(self.pmw_nodes):
            if existing.uid == pms_node.uid:
                self.pmw_nodes[index] = pms_node
                break
        self.pmw_node_model.pntm_update_node(pms_node)
        self.pmw_refresh_summary()

    def pmw_reload_history_table(self) -> None:
        """刷新“最近测试记录”表。

        v1.9.21：补上「说明」列——失败原因（核心给的 `HTTP 503（…）` / `HTTP 504（Timeout）`）
        一直在 `pms_latency_history.detail` 里存着，却从来没显示出来，用户只能看到一屏「超时」。
        """
        rows = self.pmw_repo.pnr_load_latency_history("", 20)
        self.pmw_history_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = [
                str(row["tested_at"] or ""),
                str(row["node_name"] or ""),
                str(row["test_mode"] or ""),
                pms_format_latency(row["latency_ms"] if row["latency_ms"] is not None else None),
                pms_format_speed(float(row["speed_bps"] or 0.0)) if row["speed_bps"] else "-",
                str(row["detail"] or ""),
            ]
            for column, text in enumerate(values):
                self.pmw_history_table.setItem(index, column, QTableWidgetItem(text))

    def pmw_reapply_filters(self, pms_reset_page: bool) -> None:
        """把当前筛选控件的状态灌进代理模型并落盘。

        v1.9.16 从 `pmw_on_filter_changed()` 里抽出来，只为一件事：重建分组列表时
        （`pmw_reload_groups()`）**不能回第 1 页**——否则启动恢复好的页码会被自己冲掉
        （`ui.table_page_index` 的「页码记忆」在全新启动时失效的老毛病就是这个）。
        用户真的改筛选条件（下拉 / 搜索框 / 复选框 / 分组）时仍按老行为回第 1 页。
        """
        item = self.pmw_group_list.currentItem()
        group = str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
        self.pmw_node_proxy.pnfp_set_filters(
            pms_group=group,
            pms_protocol=str(self.pmw_protocol_combo.currentData() or ""),
            pms_keyword=self.pmw_search_edit.text(),
            pms_only_enabled=bool(self.pmw_chk_only_enabled.isChecked()),
            pms_only_available=bool(self.pmw_chk_only_available.isChecked()),
            pms_only_failed=bool(self.pmw_chk_only_failed.isChecked()),
        )
        # v1.9.77：仅在「全部节点」页才把 ui.filter_protocol 落盘（它是全部节点页自己的筛选器）。
        # 电报专属页期间侧栏协议下拉已禁用，照写会把全部节点页永久过滤空。
        _pmwt_save_filter_protocol = (
            getattr(self, "pmwt_active_key", lambda: PMWT_TAB_ALL)() == PMWT_TAB_ALL
        )
        self.pmw_settings.psm_set_many(
            ({"ui.filter_protocol": str(self.pmw_protocol_combo.currentData() or "")} if _pmwt_save_filter_protocol else {})
            | {"ui.filter_search": self.pmw_search_edit.text()},
            pms_emit=False,
        )
        # v1.9.16：分组列表还没建起来时（启动早期）不能把已保存的分组名清成空，
        # 否则「记住上次分组」会在 pmw_restore_ui_state() 这一步就被自己抹掉。
        if item is not None:
            self.pmw_settings.psm_set("ui.filter_group", group, pms_emit=False)
        # v1.7.0：筛选条件变了，结果集就变了——继续停在第 N 页很可能是一片空白，回第 1 页。
        # v1.9.16：重建分组列表这条路径（pms_reset_page=False）不跳页，只把页码夹取到有效范围。
        if pms_reset_page:
            self.pmw_apply_pagination(1, pms_reset_to_first=True)
        else:
            self.pmw_apply_pagination()
        self.pmw_refresh_summary()
        self.pmw_update_connect_actions()

    def pmw_restore_selected_uids(self, pms_uids: list[str]) -> None:
        """排完按 uid 把选中还回去（为什么需要：见 `PmsNodeTableModel.pntm_sort_by` 的说明）。"""
        selection = self.pmw_node_table.selectionModel()
        if selection is not None:
            self.pmw_node_page.pnfp_restore_selection(selection, pms_uids)

    def pmw_update_connect_actions(self) -> None:
        """根据选择状态启用 / 禁用工具栏动作。"""
        has_selection = bool(self.pmw_selected_nodes())
        for key in ("edit", "delete", "share", "copy_link", "copy_config", "duplicate"):
            action = self.pmw_actions.get(key)
            if action is not None:
                action.setEnabled(has_selection)
        core_action = self.pmw_actions.get("core")
        if core_action is not None:
            core_action.setEnabled(self.pmw_current_node() is not None or self.pmw_core.pcp_is_running())

    def pmw_sort_by_latency(self) -> None:
        """按延迟排序（把未测出的排到最后）。"""
        uids = [node.uid for node in self.pmw_selected_nodes()]
        self.pmw_node_table.sortByColumn(PMS_COLUMN_LATENCY, Qt.SortOrder.AscendingOrder)
        # v1.9.14：表头已经是「延迟 + 升序」时 Qt 不再发 sortIndicatorChanged，
        # pmw_on_sort_changed() 里那次「回第 1 页」就落空了。全量排序必须回第 1 页
        # （否则延迟最低的一批在末页看不见），所以这里自己补一次，不依赖信号。
        self.pmw_apply_pagination(1, pms_reset_to_first=True)
        self.pmw_restore_selected_uids(uids)
        self.pmw_show_message("已按延迟排序")

    def pmw_sort_by_speed(self) -> None:
        """按速度排序（降序：速度高的排前面，没测出速度的自然沉到最后）。"""
        uids = [node.uid for node in self.pmw_selected_nodes()]
        self.pmw_node_table.sortByColumn(PMS_COLUMN_SPEED, Qt.SortOrder.DescendingOrder)
        self.pmw_apply_pagination(1, pms_reset_to_first=True)
        self.pmw_restore_selected_uids(uids)
        self.pmw_show_message("已按速度排序")

    def pmw_sort_current_page(self, pms_column: int, pms_order: Qt.SortOrder) -> None:
        """只把**当前页**的行排序（不重排全量、也不跳回第 1 页）。

        v1.9.12：走分页层自己的 pnfp_sort_here()，不经过视图的 sortByColumn
        （那条路会被转交给全量筛选层，等价于「全量排序」）。
        表头指示器同步成这一列，让用户看得出按什么排的；因为绕开了视图，不会触发
        pmw_on_sort_changed() 的「回第 1 页」。

        v1.9.14：**故意不写 `ui.table_sort_column` / `ui.table_sort_order`**——这两个键记的是
        「全量排序」，启动恢复要用它，点表头选「本页」时还要靠它把视图刚做的全量排序还原回去。
        本页排序只体现在表头箭头（视觉）上，不改这份记录。
        """
        uids = [node.uid for node in self.pmw_selected_nodes()]
        self.pmw_node_page.pnfp_sort_here(pms_column, pms_order)
        self.pmw_restore_selected_uids(uids)
        header = self.pmw_node_table.horizontalHeader()
        header.blockSignals(True)
        try:
            header.setSortIndicator(pms_column, pms_order)
        finally:
            header.blockSignals(False)

    def pmw_apply_auto_sort(self, pms_kind: str) -> None:
        """测试结束后的自动排序入口（延迟 / 测速各自一份开关与范围）。

        范围 `all`：对整个筛选结果排序——延迟升序 / 速度降序，
        排完必然要回第 1 页，否则「延迟最低 / 速度最高」的一批根本不在当前页，
        用户看着就像没排序（这正是 v1.9.12 要解决的问题）。
        范围 `page`：只在当前页内重排，保持当前页码不变。
        """
        if pms_kind == "speed":
            scope = self.pmw_settings.psm_get_str("test.sort_scope_speed", PMS_SORT_SCOPE_ALL)
            if scope == PMS_SORT_SCOPE_PAGE:
                self.pmw_sort_current_page(PMS_COLUMN_SPEED, Qt.SortOrder.DescendingOrder)
                self.pmw_show_message("已按速度排序（仅当前页）")
            else:
                self.pmw_sort_by_speed()
            return
        scope = self.pmw_settings.psm_get_str("test.sort_scope_latency", PMS_SORT_SCOPE_ALL)
        if scope == PMS_SORT_SCOPE_PAGE:
            self.pmw_sort_current_page(PMS_COLUMN_LATENCY, Qt.SortOrder.AscendingOrder)
            self.pmw_show_message("已按延迟排序（仅当前页）")
        else:
            self.pmw_sort_by_latency()
