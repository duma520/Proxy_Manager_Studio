# -*- coding: utf-8 -*-
"""主窗口节点区分页标签（v1.9.76）：全部节点 / TgProxy / TgWebProxy 三个独立分页标签。

★ 设计动机（用户原话）：「新增一个分页标签，用另一个/两个列表（TgProxy、TgWebProxy）
存放电报抓到的两类节点，电报抓取仍然走代理」。
电报抓取链路（`pms/gui/mainwindow_import.py`）与入库逻辑**零改动**：
t.me/proxy → `tg_proxy`、t.me/webproxy → `tg_webproxy`，自 v1.9.74 起就能入库。
这里只做**展示层**分流：两个新标签各自一套（筛选层 + 分页层 + 表格 + 分页栏），
源模型仍是同一个 `self.pmw_node_model`（55k 节点只加载/排序一次，不复制数据）。

★ 兼容性铁律（规则 12）：所有旧引用点（`self.pmw_node_proxy` / `pmw_node_page` /
`pmw_node_table` / 分页栏控件……共 130+ 处，含 `_scaffold/probe_*.py` 探针）**一行不改**。
做法是把它们做成「活动标签转发属性」：哪个标签页当前可见，属性就返回哪个实例。
「全部节点」页的模型栈与旧版逐字节等价，所以老行为零变化。

★ 为什么不用「一套模型栈 + Tab 只切 protocol 筛选」：分页状态（`pnfp_page` / 页内排序
`pnfp_here_sort`）活在代理模型实例上，共用一个筛选层就无法让两个新标签**各自独立翻页**
（用户明确要求的是「独立分页标签」）。

★ 命名前缀 `pmwt_`（PmsMainWindowTabs）为新代码专用；`ptt_` 已被 `pms/dctest.py` 的
`PmsTelegramTestCoordinator` 信号占用，禁用。
"""
from __future__ import annotations

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pms.constants import PMS_COLUMN_MIN_WIDTHS
from pms.gui.qtmodels import PmsLatencyBarDelegate, PmsNodeFilterProxyModel
from pms.gui.widgets import PMS_TABLE_PAGE_SIZES

# ------------------------------------------------------------------ 标签页标识
PMWT_TAB_ALL = "all"            # 全部节点（旧行为，源 = 侧栏筛选条件）
PMWT_TAB_TG_PROXY = "tg_proxy"   # 只放 tg_proxy（MTProto 电报代理）
PMWT_TAB_TG_WEBPROXY = "tg_webproxy"  # 只放 tg_webproxy（标准 SOCKS5）

# 标签页 → 钉死的协议（空串 = 不钉死，跟随侧栏筛选）
PMWT_TAB_PROTOCOL = {
    PMWT_TAB_ALL: "",
    PMWT_TAB_TG_PROXY: "tg_proxy",
    PMWT_TAB_TG_WEBPROXY: "tg_webproxy",
}
PMWT_TAB_TITLES = {
    PMWT_TAB_ALL: "全部节点",
    PMWT_TAB_TG_PROXY: "TgProxy（电报 MTProto）",
    PMWT_TAB_TG_WEBPROXY: "TgWebProxy（电报 SOCKS5）",
}
# 右键菜单里「一键发送到 Telegram」的动作名（走 add_action 登记，自动继承启用状态刷新）
PMWT_ACTION_SEND_SELECTED = "tg_send_selected"
PMWT_ACTION_SEND_PAGE = "tg_send_page"
# 每页条数记忆键：全部页沿用旧的 ui.table_page_size，两个新页各自一份（规则 3：实时持久化）
PMWT_PAGE_KEY_SIZE = {
    PMWT_TAB_ALL: "ui.table_page_size",
    PMWT_TAB_TG_PROXY: "ui.tg_proxy_tab_page_size",
    PMWT_TAB_TG_WEBPROXY: "ui.tg_webproxy_tab_page_size",
}
PMWT_PAGE_KEY_INDEX = {
    PMWT_TAB_ALL: "ui.table_page_index",
    PMWT_TAB_TG_PROXY: "ui.tg_proxy_tab_page_index",
    PMWT_TAB_TG_WEBPROXY: "ui.tg_webproxy_tab_page_index",
}


class _PmwTgTabView:
    """一个标签页的全部活对象（模型栈 + 表格 + 分页栏）。

    用普通类而不是 dict，只为读起来有类型提示；纯容器，没有行为。
    """

    __slots__ = (
        "pmwt_key",
        "pmwt_protocol",
        "pmwt_proxy",
        "pmwt_page",
        "pmwt_table",
        "pmwt_delegate",
        "pmwt_bar",
        "pmwt_btn_first_page",
        "pmwt_btn_prev_page",
        "pmwt_btn_next_page",
        "pmwt_btn_last_page",
        "pmwt_label_page",
        "pmwt_combo_page_size",
        "pmwt_btn_select_all",
        "pmwt_btn_select_page",
        "pmwt_label_selection",
        "pmwt_summary_label",
    )

    def __init__(self, pms_key: str, pms_protocol: str) -> None:
        self.pmwt_key = pms_key
        self.pmwt_protocol = pms_protocol
        for _pmwt_slot in _PmwTgTabView.__slots__:
            if _pmwt_slot not in ("pmwt_key", "pmwt_protocol"):
                setattr(self, _pmwt_slot, None)


class _PmwTgTabsMixin:
    """节点区三标签：全部节点 + TgProxy + TgWebProxy（v1.9.76，方案 B）。

    只增加能力，不改旧语义：`_PmwUiMixin.pmw_build_node_area()` 建的手「全部节点」那一套
    （`pmw_node_proxy` / `pmw_node_page` / `pmw_node_table` / 分页栏控件）原样保留，
    由本 mixin 把它们与新标签的三套一起收进 `self.pmwt_views`，并提供转发属性。
    """

    # ------------------------------------------------------------------ 构建
    def pmwt_build_tabs(self, pms_all_area: QWidget) -> QWidget:
        """把 `pmw_build_node_area()` 建好的「全部节点」页包进 QTabWidget，再补两个电报专属页。

        调用点只有一处：`_PmwUiMixin.pmw_build_body()`（把 addWidget(node_area) 换成本方法）。
        """
        self.pmwt_views: dict[str, _PmwTgTabView] = {}
        self.pmwt_switching = False
        # 「全部节点」页沿用旧实例（属性已在 _PmwUiMixin 里赋值），只补登记。
        # 注意：此时 pmwt_views 为空，_PmwUiMixin 的转发属性会回落到赋值期写入的实例，
        # 所以下面读到的正是「全部节点」页那一套（与改造前逐字节等价）。
        # 映射表：_PmwTgTabView 槽位 → _PmwUiMixin 转发属性写入的 __dict__ 键
        _pmwt_slot_to_key = {
            "pmwt_proxy": "_pmw_node_proxy",
            "pmwt_page": "_pmw_node_page",
            "pmwt_table": "_pmw_node_table",
            "pmwt_delegate": "_pmw_bar_delegate",
            "pmwt_summary_label": "_pmw_node_summary_label",
            "pmwt_label_page": "_pmw_pmw_label_page",
            "pmwt_combo_page_size": "_pmw_pmw_combo_page_size",
            "pmwt_label_selection": "_pmw_pmw_label_selection",
            "pmwt_btn_first_page": "_pmw_pmw_btn_first_page",
            "pmwt_btn_prev_page": "_pmw_pmw_btn_prev_page",
            "pmwt_btn_next_page": "_pmw_pmw_btn_next_page",
            "pmwt_btn_last_page": "_pmw_pmw_btn_last_page",
            "pmwt_btn_select_all": "_pmw_pmw_btn_select_all",
            "pmwt_btn_select_page": "_pmw_pmw_btn_select_page",
        }
        all_view = _PmwTgTabView(PMWT_TAB_ALL, "")
        for _pmwt_slot, _pmwt_key_name in _pmwt_slot_to_key.items():
            _pmwt_value = self.__dict__.get(_pmwt_key_name)
            if _pmwt_value is None:
                # 兜底：万一路径变了，直接读属性（此时 pmwt_views 仍为空 → 回落赋值期实例）
                _pmwt_value = getattr(self, _pmwt_slot.replace("pmwt_", "pmw_", 1), None)
            if _pmwt_value is not None:
                setattr(all_view, _pmwt_slot, _pmwt_value)
        self.pmwt_views[PMWT_TAB_ALL] = all_view
        tabs = QTabWidget()
        tabs.setObjectName("pmwt_node_tabs")
        tabs.setDocumentMode(True)
        tabs.setMovable(False)
        tabs.addTab(pms_all_area, PMWT_TAB_TITLES[PMWT_TAB_ALL])
        for key in (PMWT_TAB_TG_PROXY, PMWT_TAB_TG_WEBPROXY):
            tabs.addTab(self.pmwt_build_tg_page(key), PMWT_TAB_TITLES[key])
        self.pmwt_tabs = tabs
        tabs.currentChanged.connect(self.pmwt_on_tab_changed)
        return tabs

    def pmwt_build_tg_page(self, pms_key: str) -> QWidget:
        """建一个电报专属标签页：独立的筛选层（协议钉死）+ 分页层 + 表格 + 分页栏。"""
        protocol = PMWT_TAB_PROTOCOL[pms_key]
        view = _PmwTgTabView(pms_key, protocol)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        # 源模型仍然是同一个 pmw_node_model：55k 节点只加载/排序一次，不复制数据。
        # 多个筛选层挂同一源模型互不干扰（PmsNodeFilterProxyModel.setSourceModel 各自持槽）。
        view.pmwt_proxy = PmsNodeFilterProxyModel(self)
        view.pmwt_proxy.setSourceModel(self.pmw_node_model)
        view.pmwt_page = PmsNodeFilterProxyModel(self)
        view.pmwt_page.setSourceModel(view.pmwt_proxy)
        view.pmwt_page.pnfp_sort_target = view.pmwt_proxy
        # 钉死协议（空分组 + 空关键字 + 三个勾选全 False）：这个页只装这一种协议
        view.pmwt_proxy.pnfp_set_filters(
            pms_group="",
            pms_protocol=protocol,
            pms_keyword="",
            pms_only_enabled=False,
            pms_only_available=False,
            pms_only_failed=False,
        )
        table = QTableView()
        table.setObjectName(f"pmwt_table_{pms_key}")
        table.setModel(view.pmwt_page)
        table.setSortingEnabled(True)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        delegate = PmsLatencyBarDelegate(table)
        table.setItemDelegate(delegate)
        view.pmwt_table = table
        view.pmwt_delegate = delegate
        table.doubleClicked.connect(lambda _index: self.pmw_connect_selected_node())
        table.selectionModel().selectionChanged.connect(self.pmw_on_selection_changed)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self.pmw_show_node_context_menu)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.sectionResized.connect(self.pmw_on_column_resized)
        header.sortIndicatorChanged.connect(self.pmw_on_sort_changed)
        header.sectionPressed.connect(self.pmw_on_header_section_pressed)
        for index, width in enumerate(PMS_COLUMN_MIN_WIDTHS):
            table.setColumnWidth(index, width)
        # 行高 / 字号 / 延迟条粗细：跟随主设置（设置一改，三个页都要生效）
        table.verticalHeader().setDefaultSectionSize(
            self.pmw_settings.psm_get_int("ui.table_row_height", 26)
        )
        self.pmwt_apply_font_to(table)
        self.pmwt_apply_bar_height_to(delegate, table)
        layout.addWidget(table, 1)
        summary = QLabel("节点：-")
        summary.setObjectName("hintLabel")
        layout.addWidget(summary)
        view.pmwt_summary_label = summary
        view.pmwt_bar = container
        self.pmwt_views[pms_key] = view
        # 每页条数：沿用主设置（用户只有一份「每页 N 条」习惯），页码按页各自记忆
        size = self.pmw_page_size()
        view.pmwt_page.pnfp_set_pagination(1, size)
        layout.addWidget(self.pmwt_build_tg_pagination_bar(view, size))
        self.pmwt_sync_pagination_bar(view)
        return container

    def pmwt_build_tg_pagination_bar(self, pms_view: _PmwTgTabView, pms_size: int) -> QWidget:
        """电报专属标签页的分页栏（按钮语义与主分页栏完全一致）。"""
        layout_owner = QWidget()
        layout = QHBoxLayout(layout_owner)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        view = pms_view
        view.pmwt_btn_first_page = QPushButton("«")
        view.pmwt_btn_prev_page = QPushButton("‹")
        view.pmwt_btn_next_page = QPushButton("›")
        view.pmwt_btn_last_page = QPushButton("»")
        for button, handler in (
            (view.pmwt_btn_first_page, lambda: self.pmw_goto_page(1)),
            (view.pmwt_btn_prev_page, lambda: self.pmw_goto_page(self.pmw_page_index() - 1)),
            (view.pmwt_btn_next_page, lambda: self.pmw_goto_page(self.pmw_page_index() + 1)),
            (view.pmwt_btn_last_page, lambda: self.pmw_goto_page(self.pmw_page_count())),
        ):
            button.setFixedWidth(26)
            button.clicked.connect(handler)
            layout.addWidget(button)
        view.pmwt_label_page = QLabel("第 1 / 1 页")
        view.pmwt_label_page.setObjectName("hintLabel")
        layout.addWidget(view.pmwt_label_page)
        combo = QComboBox()
        for value in PMS_TABLE_PAGE_SIZES:
            combo.addItem(f"每页 {value} 条", value)
        combo.setFixedWidth(92)
        combo.currentIndexChanged.connect(self.pmw_on_page_size_changed)
        view.pmwt_combo_page_size = combo
        layout.addWidget(combo)
        view.pmwt_btn_select_all = QPushButton("全选筛选结果")
        view.pmwt_btn_select_all.setToolTip("选中**全部筛选结果**（跨页），不只是当前页")
        view.pmwt_btn_select_all.clicked.connect(self.pmw_select_all_filtered)
        layout.addWidget(view.pmwt_btn_select_all)
        view.pmwt_btn_select_page = QPushButton("选本页")
        view.pmwt_btn_select_page.setToolTip("只选中当前页的节点")
        view.pmwt_btn_select_page.clicked.connect(self.pmw_select_current_page)
        layout.addWidget(view.pmwt_btn_select_page)
        view.pmwt_label_selection = QLabel("")
        view.pmwt_label_selection.setObjectName("hintLabel")
        layout.addWidget(view.pmwt_label_selection)
        layout.addStretch(1)
        index = combo.findData(pms_size)
        combo.setCurrentIndex(index if index >= 0 else 0)
        return layout_owner

    # ------------------------------------------------------------------ 活动页路由
    def pmwt_active_key(self) -> str:
        """当前活动标签页标识（兜底「全部节点」）。"""
        tabs = getattr(self, "pmwt_tabs", None)
        if tabs is None or not hasattr(tabs, "currentIndex"):
            return PMWT_TAB_ALL
        index = tabs.currentIndex()
        if index <= 0:
            return PMWT_TAB_ALL
        keys = [PMWT_TAB_TG_PROXY, PMWT_TAB_TG_WEBPROXY]
        if 1 <= index < 1 + len(keys):
            return keys[index - 1]
        return PMWT_TAB_ALL

    def pmwt_active_view(self) -> "_PmwTgTabView":
        """当前活动标签页的活对象（活动页不存在时回落到「全部节点」）。"""
        views = getattr(self, "pmwt_views", None)
        if not views:
            # 极早期调用（建页过程中）：回落单栈，保持旧路径可用
            view = _PmwTgTabView(PMWT_TAB_ALL, "")
            view.pmwt_proxy = self.__dict__.get("_pmw_node_proxy")
            view.pmwt_page = self.__dict__.get("_pmw_node_page")
            view.pmwt_table = self.__dict__.get("_pmw_node_table")
            return view
        key = self.pmwt_active_key()
        return views.get(key) or views[PMWT_TAB_ALL]

    def pmwt_active_page_key_size(self) -> str:
        """当前活动标签页的「每页条数」设置键（v1.9.76）。"""
        return PMWT_PAGE_KEY_SIZE.get(self.pmwt_active_key(), PMWT_PAGE_KEY_SIZE[PMWT_TAB_ALL])

    def pmwt_active_page_key_index(self) -> str:
        """当前活动标签页的「当前页码」设置键（v1.9.76）。"""
        return PMWT_PAGE_KEY_INDEX.get(self.pmwt_active_key(), PMWT_PAGE_KEY_INDEX[PMWT_TAB_ALL])

    def pmwt_on_tab_changed(self, _index: int = 0) -> None:
        """切标签：把「当前页」的分页状态与侧栏协议下拉对齐，再刷新一排界面。

        主力行为只有四件：同步侧栏协议下拉（Tg 页显示对应协议，用户改下拉不会打破钉死）、
        刷新分页栏 / 摘要 / 选中计数 / 工具栏可用性。**不碰**任何模型数据，
        也不重设分页（每页各自记住自己的页码，这正是「独立分页标签」的意义）。
        """
        if getattr(self, "pmwt_switching", False):
            return
        self.pmwt_switching = True
        try:
            view = self.pmwt_active_view()
            combo = getattr(self, "pmw_protocol_combo", None)
            if combo is not None:
                if view.pmwt_key == PMWT_TAB_ALL:
                    # 「全部节点」页：侧栏协议下拉就是它自己的筛选器，恢复成真实保存值，绝不被电报页钉死。
                    combo.setEnabled(True)
                    saved = self.pmw_settings.psm_get_str("ui.filter_protocol", "")
                    index = combo.findData(saved)
                    if index < 0:
                        index = combo.findData("")
                    try:
                        combo.blockSignals(True)
                        combo.setCurrentIndex(max(0, index))
                    finally:
                        combo.blockSignals(False)
                else:
                    # 电报专属页：自己的协议已钉死在独立筛选层，侧栏下拉只是展示提示，
                    # 必须禁用，绝不能把 tg_proxy/tg_webproxy 写回 ui.filter_protocol（那是「全部节点」页的筛选器）。
                    combo.setEnabled(False)
                    idx = combo.findData(view.pmwt_protocol)
                    if idx < 0:
                        idx = 0
                    try:
                        combo.blockSignals(True)
                        combo.setCurrentIndex(max(0, idx))
                    finally:
                        combo.blockSignals(False)
            self.pmw_sync_pagination_bar()
            self.pmw_refresh_summary()
            self.pmw_refresh_selection_label()
            self.pmw_update_connect_actions()
            # 行高 / 字号 / 延迟条：主设置改过之后新页才同步，切页时再兜一次
            self.pmwt_apply_font_to(view.pmwt_table)
            self.pmwt_apply_bar_height_to(view.pmwt_delegate, view.pmwt_table)
        finally:
            self.pmwt_switching = False

    def pmwt_apply_font_to(self, pms_table) -> None:
        """把主设置里的表格字号灌给指定表格（新增两个页也要跟随）。"""
        if pms_table is None:
            return
        font = pms_table.font()
        font.setPointSizeF(self.pmw_settings.psm_get_float("ui.table_font_size", 9.5))
        pms_table.setFont(font)

    def pmwt_apply_bar_height_to(self, pms_delegate, pms_table) -> None:
        """把延迟条粗细灌给指定页（新增两个页也要跟随）。"""
        if pms_delegate is None or pms_table is None:
            return
        pms_delegate.plbd_bar_height = max(0, self.pmw_settings.psm_get_int("ui.latency_bar_height", 5))
        pms_table.viewport().update()

    def pmwt_sync_all_tab_settings(self) -> None:
        """主表格设置变化（行高/字号/延迟条）时同步三个页（供 _PmwSettingsMixin 调用）。"""
        for view in getattr(self, "pmwt_views", {}).values():
            self.pmwt_apply_font_to(view.pmwt_table)
            self.pmwt_apply_bar_height_to(view.pmwt_delegate, view.pmwt_table)
    def pmwt_sync_pagination_bar(self, pms_view: _PmwTgTabView) -> None:
        """按指定页刷新分页栏文案与按钮（建页/切页用；主路径走 pmw_sync_pagination_bar）。"""
        page = max(1, int(pms_view.pmwt_page.pnfp_page))
        pages = pms_view.pmwt_page.pnfp_page_count()
        total = pms_view.pmwt_proxy.rowCount()
        size = pms_view.pmwt_page.pnfp_page_size
        if pages > 1:
            start = (page - 1) * max(1, size) + 1
            end = min(total, page * max(1, size))
            text = f"第 {page} / {pages} 页（第 {start}-{end} 条，共 {total} 条）"
        else:
            text = f"共 {total} 条"
        pms_view.pmwt_label_page.setText(text)
        pms_view.pmwt_btn_first_page.setEnabled(page > 1)
        pms_view.pmwt_btn_prev_page.setEnabled(page > 1)
        pms_view.pmwt_btn_next_page.setEnabled(page < pages)
        pms_view.pmwt_btn_last_page.setEnabled(page < pages)
        _pmwt_label = getattr(pms_view, "pmwt_label_selection", None)
        if _pmwt_label is not None:
            _pmwt_label.setText("")

    # ------------------------------------------------------------------ 一键发送到 Telegram
    def pmwt_tg_links_of_nodes(self, pms_nodes) -> list[str]:
        """取节点的 t.me 分享链接（只保留 tg_proxy / tg_webproxy 两类）。"""
        links: list[str] = []
        writer = self.pmw_link_writer
        for node in pms_nodes:
            protocol = str(getattr(node, "protocol", "") or "").lower()
            if protocol not in (PMWT_TAB_TG_PROXY, PMWT_TAB_TG_WEBPROXY):
                continue
            try:
                text = str(writer.pslw_node_to_link(node) or "").strip()
            except Exception:  # noqa: BLE001 - 展示层容错：单条坏数据不影响整批
                text = ""
            if text.startswith("https://t.me/"):
                links.append(text)
        return links

    def pmwt_send_tg_links_to_telegram(self, pms_nodes=None) -> int:
        """一键用 Telegram 打开选中（或当前页可见）的电报代理链接。

        可靠性分级（已向用户说明）：
          1. 逐条 `QDesktopServices.openUrl()`——本机装了 Telegram 且注册了
             `tg://` / `https://t.me` 协议时，客户端会弹「是否启用该代理」；
          2. 打不开时把链接写进剪贴板并提示手动粘贴，功能不损失。
        返回成功唤起 Client 的条数（0 = 全部走剪贴板兜底）。
        """
        if pms_nodes is None:
            selected = self.pmw_selected_nodes()
            pms_nodes = selected if selected else self.pmw_page_nodes()
        links = self.pmwt_tg_links_of_nodes(pms_nodes)
        if not links:
            self.pmw_show_message("当前选中的节点里没有 TgProxy / TgWebProxy 电报代理")
            return 0
        opened = 0
        for link in links:
            if QDesktopServices.openUrl(QUrl.fromUserInput(link)):
                opened += 1
        if opened:
            self.pmw_show_message(f"已唤起 Telegram 打开 {opened} 条代理链接（未响应时请手动粘贴）")
        else:
            self.pmw_copy_links_to_clipboard(pms_nodes)
            self.pmw_show_message("本机未能唤起 Telegram，链接已复制到剪贴板，去 Telegram 粘贴即可")
        return opened

    def pmwt_send_selected_to_telegram(self) -> None:
        """右键菜单 / 工具栏入口：把选中的电报代理一键发给 Telegram。"""
        self.pmwt_send_tg_links_to_telegram(self.pmw_selected_nodes())

    def pmwt_send_page_to_telegram(self) -> None:
        """把当前页可见的电报代理一键发给 Telegram（未选中任何行时更好用）。"""
        self.pmwt_send_tg_links_to_telegram(self.pmw_page_nodes())
