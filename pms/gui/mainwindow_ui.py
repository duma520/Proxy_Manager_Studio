# -*- coding: utf-8 -*-
"""主窗口界面搭建（工具栏 / 侧栏 / 各面板 / 状态栏 / 信号接线与定时器）——v1.9.36 从 `pms/gui/mainwindow.py` 拆出的 mixin。

★ 拆分原则：**方法名与 `self.pmw_*` 属性名一个字都没改**，外部引用（主窗口之外用到
`pmw_*` 的 80 处）全都不用动；mixins 只是把方法按功能域挪个文件。
"""
from __future__ import annotations

import platform
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableView,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from pms.constants import (
    PMS_COLUMN_MIN_WIDTHS,
    PMS_CORE_KIND_TITLES,
    PMS_LOG_LEVEL_TITLES,
    PMS_PROTOCOL_ORDER,
    PMS_PROTOCOL_TITLES,
    PMS_STATUS_TICK_MS,
)
from pms.telegram import (
    PMS_TELEGRAM_FETCH_MODE_FULL,
    PMS_TELEGRAM_FETCH_MODE_INCREMENTAL,
)
from pms.subkind import (
    PMS_SUBSCRIPTION_FILTER_ALL,
    PMS_SUBSCRIPTION_FILTER_NON_TG,
    PMS_SUBSCRIPTION_FILTER_TG,
)
from pms.coresys import PmsPrivilegeUtils, PmsWindowsAutoStart
from pms.gui.dialogs.settingsdialog import PmsSettingsDialog
from pms.gui.qtmodels import (
    PmsConnectionTableModel,
    PmsLatencyBarDelegate,
    PmsLogTableModel,
    PmsNodeFilterProxyModel,
    PmsNodeTableModel,
)
from pms.gui.widgets import PMS_TABLE_PAGE_SIZES, PmsToolbarFilterButton, pms_make_hint_label
from pms.logbus import PMS_ERROR_LOG_MAX
from typing import Any


class _PmwUiMixin:
    """主窗口界面搭建（工具栏 / 侧栏 / 各面板 / 状态栏 / 信号接线与定时器）（23 个方法）。

    v1.9.76：节点模型栈与分页栏控件的属性（`pmw_node_proxy` / `pmw_node_page` /
    `pmw_node_table` / `pmw_bar_delegate` / 分页栏 9 个控件 / `pmw_node_summary_label`）
    改成「活动标签页转发属性」——由 `pms/gui/mainwindow_tgtabs.py` 的 `_PmwTgTabsMixin`
    在 `pmwt_build_tabs()` 里登记三个页（全部节点 / TgProxy / TgWebProxy）。
    旧引用点 130+ 处一字不改，全部跟着当前标签页走；「全部节点」页拿到的仍是同一批实例，
    行为与改造前逐字节等价（规则 12）。
    """

    # ------------------------------------------------------------------ 活动标签页转发属性
    @property
    def pmw_node_proxy(self):
        """节点筛选层（活动标签页）。"""
        view = self.pmwt_active_view()
        return getattr(view, "pmwt_proxy", None) or self.__dict__.get("_pmw_node_proxy")

    @pmw_node_proxy.setter
    def pmw_node_proxy(self, pms_value) -> None:
        self.__dict__["_pmw_node_proxy"] = pms_value

    @property
    def pmw_node_page(self):
        """节点分页层（活动标签页）。"""
        view = self.pmwt_active_view()
        return getattr(view, "pmwt_page", None) or self.__dict__.get("_pmw_node_page")

    @pmw_node_page.setter
    def pmw_node_page(self, pms_value) -> None:
        self.__dict__["_pmw_node_page"] = pms_value

    @property
    def pmw_node_table(self):
        """节点表格（活动标签页）。"""
        view = self.pmwt_active_view()
        return getattr(view, "pmwt_table", None) or self.__dict__.get("_pmw_node_table")

    @pmw_node_table.setter
    def pmw_node_table(self, pms_value) -> None:
        self.__dict__["_pmw_node_table"] = pms_value

    @property
    def pmw_bar_delegate(self):
        """延迟列进度条委托（活动标签页）。"""
        view = self.pmwt_active_view()
        delegate = getattr(view, "pmwt_delegate", None)
        return delegate if delegate is not None else self.__dict__.get("_pmw_bar_delegate")

    @pmw_bar_delegate.setter
    def pmw_bar_delegate(self, pms_value) -> None:
        self.__dict__["_pmw_bar_delegate"] = pms_value

    @property
    def pmw_node_summary_label(self):
        """节点摘要标签（活动标签页）。"""
        view = self.pmwt_active_view()
        label = getattr(view, "pmwt_summary_label", None)
        return label if label is not None else self.__dict__.get("_pmw_node_summary_label")

    @pmw_node_summary_label.setter
    def pmw_node_summary_label(self, pms_value) -> None:
        self.__dict__["_pmw_node_summary_label"] = pms_value

    def __pmwt_bar_widget(self, pms_name: str):
        """分页栏控件（活动标签页）：构造期写入 `_pmw_<name>`，运行时按页转发。"""
        view = self.pmwt_active_view()
        value = getattr(view, pms_name, None)
        return value if value is not None else self.__dict__.get(f"_pmw_{pms_name}")

    def __pmwt_set_bar_widget(self, pms_name: str, pms_value) -> None:
        self.__dict__[f"_pmw_{pms_name}"] = pms_value

    pmw_label_page = property(lambda self: self.__pmwt_bar_widget("pmwt_label_page"),
                              lambda self, value: self.__pmwt_set_bar_widget("pmwt_label_page", value))
    pmw_combo_page_size = property(lambda self: self.__pmwt_bar_widget("pmwt_combo_page_size"),
                                   lambda self, value: self.__pmwt_set_bar_widget("pmwt_combo_page_size", value))
    pmw_label_selection = property(lambda self: self.__pmwt_bar_widget("pmwt_label_selection"),
                                   lambda self, value: self.__pmwt_set_bar_widget("pmwt_label_selection", value))
    pmw_btn_first_page = property(lambda self: self.__pmwt_bar_widget("pmwt_btn_first_page"),
                                  lambda self, value: self.__pmwt_set_bar_widget("pmwt_btn_first_page", value))
    pmw_btn_prev_page = property(lambda self: self.__pmwt_bar_widget("pmwt_btn_prev_page"),
                                 lambda self, value: self.__pmwt_set_bar_widget("pmwt_btn_prev_page", value))
    pmw_btn_next_page = property(lambda self: self.__pmwt_bar_widget("pmwt_btn_next_page"),
                                 lambda self, value: self.__pmwt_set_bar_widget("pmwt_btn_next_page", value))
    pmw_btn_last_page = property(lambda self: self.__pmwt_bar_widget("pmwt_btn_last_page"),
                                 lambda self, value: self.__pmwt_set_bar_widget("pmwt_btn_last_page", value))
    pmw_btn_select_all = property(lambda self: self.__pmwt_bar_widget("pmwt_btn_select_all"),
                                  lambda self, value: self.__pmwt_set_bar_widget("pmwt_btn_select_all", value))
    pmw_btn_select_page = property(lambda self: self.__pmwt_bar_widget("pmwt_btn_select_page"),
                                   lambda self, value: self.__pmwt_set_bar_widget("pmwt_btn_select_page", value))
    def pmw_build_ui(self) -> None:
        """搭好主窗口骨架（工具栏 / 速览条 / 主体 / 状态栏）。"""
        self.pmw_apply_window_title()
        if not self.pmw_icon.isNull():
            self.setWindowIcon(self.pmw_icon)
        self.setMinimumSize(940, 620)
        self.pmw_toolbar = self.addToolBar("主工具栏")
        self.pmw_toolbar.setMovable(False)
        self.pmw_build_toolbar()
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(6, 6, 6, 6)
        central_layout.setSpacing(6)
        central_layout.addWidget(self.pmw_build_quick_stats_bar())
        central_layout.addWidget(self.pmw_build_body(), 1)
        self.setCentralWidget(central)
        self.pmw_build_status_bar()
        self.pmw_subscription.psu_set_local_proxy_port(0)

    def pmw_build_toolbar(self) -> None:
        """工具栏：折叠为 6 个下拉功能组（核心 / 测试 / 节点 / 导入导出 / 手机代理 / 更多）。

        所有动作仍登记在 ``self.pmw_actions`` 中（键名保持不变），仅改变其宿主：
        原先直接挂在工具栏上，现归入下拉菜单。这样 ``pmw_update_connect_actions``、
        ``pmw_sync_tun_action``、核心文案切换、托盘同步等既有逻辑完全不受影响。
        """
        compact = self.pmw_settings.psm_get_bool("ui.compact_toolbar", True)

        def short(pms_full: str, pms_compact: str) -> str:
            return pms_compact if compact else pms_full

        self.pmw_actions: dict[str, QAction] = {}

        def add_action(pms_key: str, pms_text: str, pms_handler: Any, pms_tip: str = "", pms_checkable: bool = False) -> QAction:
            action = QAction(pms_text, self)
            if pms_tip:
                action.setToolTip(pms_tip)
                action.setStatusTip(pms_tip)
            if pms_checkable:
                action.setCheckable(True)
            action.triggered.connect(pms_handler)
            self.pmw_actions[pms_key] = action
            return action

        # 核心组
        add_action("core", short("启动 / 停止核心", "启动核心"), self.pmw_toggle_core, "启动或停止核心进程（F5）")
        add_action("system_proxy", "系统代理", self.pmw_toggle_system_proxy, "开关 Windows 系统代理（F6）", True)
        add_action("tun", "TUN", self.pmw_toggle_tun, "开关 TUN 模式（需要 sing-box + 管理员）", True)

        # 测试组
        add_action("test", short("测试延迟", "测延迟"), self.pmw_test_latency_selected, "测试选中节点的真实延迟（F7）")
        # v1.7.0：三个范围并列——本页 / 选中 / 全部筛选结果（自动测速走「全部」，语义不变）
        add_action("test_page", short("测试本页延迟", "本页测延迟"), self.pmw_test_latency_page, "只测试当前这一页的节点（分页后不用为了一屏拉全库）")
        add_action("test_all", short("测试全部延迟", "全部测延迟"), self.pmw_test_latency_all, "测试当前筛选出的全部节点（Shift+F7，跨页）")
        add_action("speed", short("下载测速", "测速"), self.pmw_test_speed_selected, "测试选中节点的下载速度（F8）")
        # v1.9.10：与「全部测延迟」对称——测速也要能一次覆盖全部筛选结果（跨页）
        add_action("speed_all", short("全部测速", "全部测速"), self.pmw_test_speed_all,
                   "测试当前筛选出的全部节点下载速度（Shift+F8，跨页；不受分页限制）")
        # v1.9.59 电报 DC 测试：连通以 MTProto 握手（req_pq_multi→resPQ）为准，逐节点起临时核心
        add_action("telegram", short("测试电报连通", "电报连通"), self.pmw_test_telegram_selected,
                   "测试选中节点能否连上 Telegram 数据中心（逐 DC：TCP + MTProto 握手；有真 HTTP 吞吐才显示速度）")
        add_action("telegram_all", short("全部测电报连通", "全部电报"), self.pmw_test_telegram_all,
                   "测试当前筛选出的全部节点能否连上 Telegram 数据中心（跨页；逐节点起临时核心，耗时较长）")
        # v1.8.0 断点续跑：接着跑上一次没测完的节点（按钮文案会带上还剩几个）
        add_action("test_resume", short("继续上次测试", "续跑"), self.pmw_resume_test_round, "继续上一次未完成的延迟 / 测速，已测过的不会重测")
        self.pmw_actions["test_resume"].setProperty("pms_base_text", short("继续上次测试", "续跑"))
        add_action("test_resume_clear", "丢弃断点", self.pmw_clear_test_round, "丢掉上一次未完成的测试清单（不想续跑了）")
        self.pmw_actions["test_resume_clear"].setProperty("pms_base_text", "丢弃断点")
        # v1.9.37 节点健康检查：立即运行一次 / 只查自动禁用的 / 暂停或继续（与手动测试互不抢资源）
        add_action("health_run", short("立即健康检查", "体检一次"), self.pmw_health_run_now,
                   "立刻体检一批节点（复用延迟测试的真连接通道，低优先级，跑多慢都不影响你手动测延迟）")
        add_action("health_auto", short("只检查自动禁用的节点", "只查自动禁用"), self.pmw_health_run_auto_only,
                   "只对被健康检查自动禁用的节点做一次真连接复活探测（验证它们到底恢复没有）")
        add_action("health_pause", "暂停健康检查", self.pmw_health_set_paused,
                   "暂停 / 继续节点健康检查（暂停会立刻停下正在跑的这一批，跑完的结果照旧落库）", True)
        # v1.9.17：一键停止（暂停）测试——已测结果保留，剩下的写进断点，可「继续上次测试」
        add_action("stop_tests", "停止测试", lambda: self.pmw_stop_tests(""), "停止正在跑的延迟 / 测速 / TCP 延迟测试（已测结果保留，可续跑）")
        add_action("stop_latency", "停止延迟测试", lambda: self.pmw_stop_tests("latency"), "只停延迟测试（含 TCP 握手模式）")
        add_action("stop_speed", "停止测速", lambda: self.pmw_stop_tests("speed"), "只停测速")
        # v1.9.17：一键清除成绩（范围固定为全部节点，用户确认过的口径）
        add_action("clear_latency", "一键清除延迟", lambda: self.pmw_clear_test_results("latency"), "把全部节点的延迟成绩清空（速度成绩与历史记录不动）")
        add_action("clear_speed", "一键清除测速", lambda: self.pmw_clear_test_results("speed"), "把全部节点的测速成绩清空（延迟成绩与历史记录不动）")
        # v1.9.67：补上电报列，并把「全部」口径正式扩成 延迟+测速+电报 三列（用户要求）
        add_action("clear_telegram", "一键清除电报", lambda: self.pmw_clear_test_results("telegram"), "把全部节点的电报（DC 探测）成绩清空（延迟与测速成绩不动）")
        add_action("clear_tests", "一键清除全部测试成绩", lambda: self.pmw_clear_test_results("all"), "延迟、测速、电报三列成绩一起清空，并把「最近测试时间」复位")
        # v1.9.61：「仅测新增」勾选——勾上后延迟/测速/电报三种测试的范围都只跑没测过的节点
        # v1.9.65：菜单里的散项已收敛到「选择测试…」弹窗，这里保留注册（快捷键 tooltip /
        #           托盘 / 批量操作等仍可能引用），勾选状态由弹窗与 pmw_toggle_only_untested 同步
        add_action("only_untested", "仅测新增", self.pmw_toggle_only_untested,
                   "勾选后，延迟 / 测速 / 电报只跑「没测过的」节点（延迟、测速、电报任一没测即算未测）", True)
        # v1.9.65：选择测试弹窗入口（范围单选 + 测试项多选），替代原来散落的 6 个测试项
        add_action("test_picker", "选择测试…", self.pmw_open_test_picker,
                   "打开「选择测试」弹窗：先选范围（选中 / 本页 / 全部筛选 / 续测），再勾测试项（延迟 / 测速 / 电报）")
        # v1.9.68：电报代理连通直测——MTProto 代理不能当通用出口，走独立弹窗粘贴 t.me/proxy 链接逐条原生 socket 探
        add_action("tg_proxy_test", "电报代理连通直测…", self.pmw_open_tg_proxy_test,
                   "粘贴 t.me/proxy 链接，逐条直连代理本身做 MTProto 握手，验证能否连通电报 DC（MTProto 代理不当通用出口）")
        add_action("delete_all_nodes", "一键删除全部节点", self.pmw_delete_all_nodes, "删除全部节点（需输入确认词；分组与订阅保留）")

        # 节点组
        add_action("add", short("新增节点", "新增"), self.pmw_add_node, "手工新增节点（Ctrl+N）")
        add_action("edit", short("编辑节点", "编辑"), self.pmw_edit_node, "编辑选中节点（Enter）")
        add_action("duplicate", "复制", self.pmw_duplicate_node, "复制选中节点")
        add_action("delete", short("删除节点", "删除"), self.pmw_delete_nodes, "删除选中节点（Delete）")
        add_action("batch", short("批量操作", "批量"), self.pmw_open_batch_dialog, "批量启用 / 分组 / 去重 / 测试 / 删除")

        # 导入导出组
        add_action("import", short("导入节点", "导入"), self.pmw_open_import_dialog, "导入分享链接 / 订阅 / 配置文件（Ctrl+I）")
        add_action("paste_import", short("粘贴导入", "粘贴"), self.pmw_paste_import_from_clipboard, "从剪贴板读取并直接批量导入节点（免开对话框，Ctrl+V）")
        add_action("subscription", short("订阅管理", "订阅"), self.pmw_open_subscription_dialog, "管理订阅源并更新（Ctrl+U）")
        # v1.9.75：订阅更新「6 分类」入口（不分格式 / 非 t.me / t.me × 全量 / 增量）。
        # 一次性模式覆盖，不改动设置项 telegram.fetch_mode。
        add_action("update_all_full", "全量更新·全部", lambda: self.pmw_update_subscriptions_by_kind(
            PMS_TELEGRAM_FETCH_MODE_FULL, PMS_SUBSCRIPTION_FILTER_ALL),
            "把全部启用的订阅按「全量」更新：电报频道从最新一路翻到上限，尽量抓全（一次性，不改设置）")
        add_action("update_all_incremental", "增量更新·全部", lambda: self.pmw_update_subscriptions_by_kind(
            PMS_TELEGRAM_FETCH_MODE_INCREMENTAL, PMS_SUBSCRIPTION_FILTER_ALL),
            "把全部启用的订阅按「增量」更新：电报频道只抓上次之后的新贴（一次性，不改设置）")
        add_action("update_nontg_full", "全量更新·非 t.me", lambda: self.pmw_update_subscriptions_by_kind(
            PMS_TELEGRAM_FETCH_MODE_FULL, PMS_SUBSCRIPTION_FILTER_NON_TG),
            "只把地址不是 t.me 的启用订阅（普通订阅 / GitHub 等）按「全量」更新（一次性，不改设置）")
        add_action("update_nontg_incremental", "增量更新·非 t.me", lambda: self.pmw_update_subscriptions_by_kind(
            PMS_TELEGRAM_FETCH_MODE_INCREMENTAL, PMS_SUBSCRIPTION_FILTER_NON_TG),
            "只把地址不是 t.me 的启用订阅按「增量」更新（一次性，不改设置）")
        add_action("update_tg_full", "全量更新·t.me", lambda: self.pmw_update_subscriptions_by_kind(
            PMS_TELEGRAM_FETCH_MODE_FULL, PMS_SUBSCRIPTION_FILTER_TG),
            "只把 t.me 地址的启用订阅（电报频道 / Tor 桥接）按「全量」更新（一次性，不改设置）")
        add_action("update_tg_incremental", "增量更新·t.me", lambda: self.pmw_update_subscriptions_by_kind(
            PMS_TELEGRAM_FETCH_MODE_INCREMENTAL, PMS_SUBSCRIPTION_FILTER_TG),
            "只把 t.me 地址的启用订阅（电报频道 / Tor 桥接）按「增量」更新（一次性，不改设置）")
        add_action("export", short("导出节点", "导出"), self.pmw_export_nodes, "导出为分享链接 / base64 / 备份 JSON")
        add_action("share", short("分享 / 二维码", "分享"), self.pmw_share_node, "生成分享链接与二维码")
        add_action("copy_link", "复制链接", self.pmw_copy_node_link, "复制选中节点分享链接到剪贴板（Ctrl+C）")
        add_action("copy_config", "复制配置", self.pmw_copy_core_config, "复制当前节点的核心配置 JSON")
        add_action("cleanup", short("清理与去重", "去重"), self.pmw_cleanup_nodes, "按地址去重并清理无效节点")

        # 手机代理组（新增便捷入口，复用现有 inbound 设置，不新增冗余设置项）
        add_action("lan_proxy", "手机 / 平板代理助手…", self.pmw_open_lan_proxy_dialog, "查看本机代理地址、生成二维码、一键开放局域网")
        add_action("allow_lan", "允许局域网（手机 / 平板连接）", self.pmw_toggle_allow_lan, "开放局域网监听，让同网段手机 / 平板连接本机代理", True)
        add_action("copy_lan_address", "复制代理地址", self.pmw_copy_lan_proxy_address, "复制当前本机代理地址 host:port，便于填到手机 / 平板")

        # 更多组
        add_action("settings", "设置", self.pmw_open_settings_dialog, "打开设置中心（Ctrl+,）")
        add_action("version", "版本", self.pmw_open_version_history, "查看迭代记录与历史备份")
        add_action("about", "关于", self.pmw_open_about_dialog, "关于与环境信息")
        # 第 14、15 项：系统工具组（网络诊断 + 设置历史）
        add_action("diagnostics", "网络诊断", self.pmw_open_diagnostics_dialog, "检查 DNS / TCP / 直连 / 核心 API 端口连通性（不经过系统代理）")
        add_action("settings_history", "设置历史", self.pmw_open_settings_history_dialog, "查看 / 回滚历史设置快照")
        # v1.9.62：内置 PAC 编辑器（专业手写 JS + 小白表单 / 模板 / 向导），产物 pms_custom.pac
        add_action("pac_editor", "PAC 编辑器", self.pmw_open_pac_editor_dialog, "内置 PAC 脚本编辑器：专业手写或小白表单，一键应用到系统代理")

        # 初始化勾选态
        self.pmw_sync_allow_lan_action(self.pmw_settings.psm_get_bool("inbound.allow_lan", False))

        # 组装下拉按钮（标题带 ▾，点击即弹出菜单；同一 QAction 可挂多个菜单）
        core_menu = self.pmw_add_menu_button("核心 ▾")
        core_menu.addAction(self.pmw_actions["core"])
        core_menu.addAction(self.pmw_actions["system_proxy"])
        core_menu.addAction(self.pmw_actions["tun"])

        test_menu = self.pmw_add_menu_button("测试 ▾")
        # v1.9.65：原来 6 个散项 + 「测新增的 ▾」二级子菜单收敛成一个弹窗入口
        test_menu.addAction(self.pmw_actions["test_picker"])
        # v1.9.68：电报代理连通直测（MTProto 代理专用，不走核心隧道）
        test_menu.addAction(self.pmw_actions["tg_proxy_test"])
        # v1.8.0：续跑入口与弹窗并列在最外层，方便一键续测
        test_menu.addAction(self.pmw_actions["test_resume"])
        test_menu.addSeparator()
        # v1.9.66：丢弃断点 / 健康检查 / 停止 / 清除收进「更多 ▾」，菜单更聚焦
        more_test_menu = self.pmw_add_submenu(test_menu, "更多 ▾")
        more_test_menu.addAction(self.pmw_actions["test_resume_clear"])
        more_test_menu.addSeparator()
        # v1.9.37：节点健康检查入口（与「手动测试」并列但互不抢占：体检会让路给手动测试）
        more_test_menu.addAction(self.pmw_actions["health_run"])
        more_test_menu.addAction(self.pmw_actions["health_auto"])
        more_test_menu.addAction(self.pmw_actions["health_pause"])
        more_test_menu.addSeparator()
        # v1.9.17：停止与清除也进工具栏菜单（右键菜单里同样有）
        more_test_menu.addAction(self.pmw_actions["stop_tests"])
        more_test_menu.addAction(self.pmw_actions["clear_latency"])
        more_test_menu.addAction(self.pmw_actions["clear_speed"])
        more_test_menu.addAction(self.pmw_actions["clear_telegram"])   # v1.9.67
        more_test_menu.addAction(self.pmw_actions["clear_tests"])

        node_menu = self.pmw_add_menu_button("节点 ▾")
        node_menu.addAction(self.pmw_actions["add"])
        node_menu.addAction(self.pmw_actions["edit"])
        node_menu.addAction(self.pmw_actions["duplicate"])
        node_menu.addAction(self.pmw_actions["delete"])
        node_menu.addSeparator()
        node_menu.addAction(self.pmw_actions["delete_all_nodes"])   # v1.9.17：危险操作单独隔开
        node_menu.addAction(self.pmw_actions["batch"])

        io_menu = self.pmw_add_menu_button("导入导出 ▾")
        io_menu.addAction(self.pmw_actions["import"])
        io_menu.addAction(self.pmw_actions["paste_import"])
        io_menu.addAction(self.pmw_actions["subscription"])
        # v1.9.75：「更新订阅」子菜单：6 分类（不分格式 / 非 t.me / t.me × 全量 / 增量）
        update_sub_menu = self.pmw_add_submenu(io_menu, "更新订阅")
        update_sub_menu.addAction(self.pmw_actions["update_all_full"])
        update_sub_menu.addAction(self.pmw_actions["update_all_incremental"])
        update_sub_menu.addSeparator()
        update_sub_menu.addAction(self.pmw_actions["update_nontg_full"])
        update_sub_menu.addAction(self.pmw_actions["update_nontg_incremental"])
        update_sub_menu.addSeparator()
        update_sub_menu.addAction(self.pmw_actions["update_tg_full"])
        update_sub_menu.addAction(self.pmw_actions["update_tg_incremental"])
        io_menu.addSeparator()
        io_menu.addAction(self.pmw_actions["export"])
        io_menu.addAction(self.pmw_actions["share"])
        io_menu.addAction(self.pmw_actions["copy_link"])
        io_menu.addAction(self.pmw_actions["copy_config"])
        io_menu.addAction(self.pmw_actions["cleanup"])

        lan_menu = self.pmw_add_menu_button("手机代理 ▾")
        lan_menu.addAction(self.pmw_actions["lan_proxy"])
        lan_menu.addSeparator()
        lan_menu.addAction(self.pmw_actions["allow_lan"])
        lan_menu.addAction(self.pmw_actions["copy_lan_address"])
        lan_menu.addSeparator()
        lan_menu.addAction(self.pmw_actions["settings"])  # 复用设置动作（同对象可挂多菜单）

        more_menu = self.pmw_add_menu_button("更多 ▾")
        more_menu.addAction(self.pmw_actions["settings"])
        more_menu.addAction(self.pmw_actions["version"])
        more_menu.addAction(self.pmw_actions["about"])
        more_menu.addAction(self.pmw_actions["diagnostics"])
        more_menu.addAction(self.pmw_actions["settings_history"])
        more_menu.addSeparator()
        more_menu.addAction(self.pmw_actions["pac_editor"])

        # v1.9.50（审计第 12 项）：「分组 / 搜索」入口。
        # 控件本体在 pms/gui/widgets.py 的 PmsToolbarFilterButton 里，这里只放按钮 + 接线。
        # 它借用的 `pmw_group_filter_box` 就是侧栏那条（同一份控件），所以两边永远同步。
        # ⚠️ 按钮在**工具栏**里建，但要等侧栏建好才能接线：主线顺序是
        #    pmw_build_ui() → pmw_build_toolbar() → pmw_build_body() → pmw_build_sidebar()，
        #    所以这里先建按钮（保持「核心 / 测试 / 节点 / …」的既有位置），
        #    真正的 ptfb_set_panel() 在 pmw_build_sidebar() 末尾调用。
        self.pmw_filter_button = PmsToolbarFilterButton(self.pmw_toolbar, self)
        self.pmw_toolbar.addWidget(self.pmw_filter_button)

    def pmw_add_menu_button(self, pms_title: str) -> QMenu:
        """创建一个「标题 ▾」下拉工具按钮，返回其菜单（动作后续 addAction 进去）。"""
        button = QToolButton(self)
        button.setText(pms_title)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        menu = QMenu(button)
        button.setMenu(menu)
        self.pmw_toolbar.addWidget(button)
        return menu

    def pmw_build_quick_stats_bar(self) -> QWidget:
        """顶部速览条：核心状态、当前节点、延迟、流量、连接数与总进度条。"""
        self.pmw_quick_frame = QFrame()
        self.pmw_quick_frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self.pmw_quick_frame)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(12)
        self.pmw_label_core_state = QLabel("核心：未运行")
        self.pmw_label_core_state.setObjectName("accentLabel")
        self.pmw_label_current_node = QLabel("当前节点：-")
        self.pmw_label_latency = QLabel("延迟：-")
        self.pmw_label_traffic = QLabel("代理：0 B ↑ / 0 B ↓")
        self.pmw_label_speed = QLabel("速度：-")
        self.pmw_label_connections = QLabel("连接：0")
        self.pmw_label_source = QLabel("统计源：未启动")
        self.pmw_label_source.setObjectName("hintLabel")
        for widget in (self.pmw_label_core_state, self.pmw_label_current_node, self.pmw_label_latency,
                       self.pmw_label_traffic, self.pmw_label_speed, self.pmw_label_connections):
            layout.addWidget(widget)
            separator = QFrame()
            separator.setFrameShape(QFrame.Shape.VLine)
            separator.setFrameShadow(QFrame.Shadow.Sunken)
            layout.addWidget(separator)
        layout.addWidget(self.pmw_label_source)
        layout.addStretch(1)
        self.pmw_quick_progress = QProgressBar()
        self.pmw_quick_progress.setRange(0, 100)
        self.pmw_quick_progress.setValue(0)
        self.pmw_quick_progress.setFixedWidth(220)
        self.pmw_quick_progress.setFormat("%p%")
        layout.addWidget(self.pmw_quick_progress)
        self.pmw_quick_progress.hide()
        if not self.pmw_settings.psm_get_bool("ui.show_quick_stats", True):
            self.pmw_quick_frame.hide()
        return self.pmw_quick_frame

    def pmw_build_body(self) -> QWidget:
        """主体：左侧筛选 + 中间节点表 + 底部日志/连接/统计。"""
        self.pmw_main_splitter = QSplitter(Qt.Orientation.Vertical)
        upper = QSplitter(Qt.Orientation.Horizontal)
        self.pmw_sidebar_panel = self.pmw_build_sidebar()
        self.pmw_sidebar_width = 240
        upper.addWidget(self.pmw_sidebar_panel)
        # v1.9.76：节点区外面再包一层「全部节点 / TgProxy / TgWebProxy」三标签容器
        # （独立分页标签，源模型仍是同一个 pmw_node_model）
        upper.addWidget(self.pmwt_build_tabs(self.pmw_build_node_area()))
        upper.setStretchFactor(0, 0)
        upper.setStretchFactor(1, 1)
        upper.setSizes([self.pmw_sidebar_width, 870])
        self.pmw_upper_splitter = upper
        self.pmw_main_splitter.addWidget(upper)
        self.pmw_bottom_tabs = QTabWidget()
        self.pmw_bottom_tabs.addTab(self.pmw_build_log_panel(), "日志")
        self.pmw_bottom_tabs.addTab(self.pmw_build_connection_panel(), "连接")
        self.pmw_bottom_tabs.addTab(self.pmw_build_statistics_panel(), "统计")
        # v1.8.0：统一「最近错误」面板（状态栏红点也指向这一页）
        self.pmw_bottom_tabs.addTab(self.pmw_build_error_panel(), "错误")
        self.pmw_error_tab_index = self.pmw_bottom_tabs.count() - 1
        self.pmw_main_splitter.addWidget(self.pmw_bottom_tabs)
        self.pmw_main_splitter.setStretchFactor(0, 3)
        self.pmw_main_splitter.setStretchFactor(1, 2)
        self.pmw_main_splitter.setSizes([520, 240])
        if not self.pmw_settings.psm_get_bool("ui.show_connection_panel", True):
            self.pmw_bottom_tabs.setTabVisible(1, False)
        if not self.pmw_settings.psm_get_bool("ui.show_left_sidebar", True):
            self.pmw_sidebar_panel.hide()
        return self.pmw_main_splitter

    def pmw_set_sidebar_visible(self, pms_visible: bool) -> None:
        """显示 / 隐藏整个左侧边栏，并保留恢复宽度（v1.6.6）。"""
        panel = self.pmw_sidebar_panel
        if pms_visible:
            if panel.isHidden():
                panel.show()
                total = max(1, self.pmw_upper_splitter.width())
                node_width = max(200, total - self.pmw_sidebar_width)
                self.pmw_upper_splitter.setSizes([self.pmw_sidebar_width, node_width])
        else:
            if not panel.isHidden():
                self.pmw_sidebar_width = max(panel.width(), 200)
                panel.hide()

    def pmw_build_group_filter_widget(self) -> QWidget:
        """构造「分组 + 筛选」控件块（侧栏主体），并把控件挂到 `self.pmw_*`。

        v1.9.50（审计第 12 项）：从 `pmw_build_sidebar()` 里原样抽出来**是为了复用**——
        `ui.show_left_sidebar` 关掉整条侧栏后，分组与筛选就没入口了；工具栏因此需要再挂
        一组**同一份**控件（`pms/gui/widgets.py` 的 `PmsToolbarFilterButton` 负责呈现与接线）。
        控件实例始终只有这一份，工具栏那个是 `setWidget()` 临时接管父对象，
        所以两边**永远同步**：改哪边都是同一条 `pmw_on_filter_changed` / `pmw_on_group_changed`，
        不存在「两套状态打架」。整条侧栏本来就已由分栏器隐藏，这里不额外隐藏控件，
        免得把工具栏那份一起收走（铁律 7.48）。
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)

        # ---- 分组 ----
        layout.addWidget(self.pmw_make_section_label("分组"))
        self.pmw_group_list = QListWidget()
        self.pmw_group_list.setMaximumHeight(200)
        self.pmw_group_list.currentItemChanged.connect(self.pmw_on_group_changed)
        self.pmw_group_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.pmw_group_list.customContextMenuRequested.connect(self.pmw_show_group_menu)
        layout.addWidget(self.pmw_group_list, 1)
        group_menu_button = QToolButton()
        group_menu_button.setText("分组管理 ▾")
        group_menu_button.setToolTip("新增 / 重命名 / 删除分组")
        group_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.pmw_group_menu = QMenu(group_menu_button)
        self.pmw_group_menu.addAction("新增分组", self.pmw_add_group)
        self.pmw_group_menu.addAction("重命名", self.pmw_rename_group)
        self.pmw_group_menu.addAction("删除", self.pmw_delete_group)
        group_menu_button.setMenu(self.pmw_group_menu)
        layout.addWidget(group_menu_button)

        # ---- 筛选 ----
        layout.addSpacing(9)
        layout.addWidget(self.pmw_make_section_separator())
        layout.addSpacing(7)
        layout.addWidget(self.pmw_make_section_label("筛选"))
        self.pmw_protocol_combo = QComboBox()
        self.pmw_protocol_combo.addItem("全部协议", "")
        for protocol in PMS_PROTOCOL_ORDER:
            self.pmw_protocol_combo.addItem(PMS_PROTOCOL_TITLES[protocol], protocol)
        self.pmw_protocol_combo.currentIndexChanged.connect(self.pmw_on_filter_changed)
        layout.addWidget(self.pmw_protocol_combo)
        self.pmw_search_edit = QLineEdit()
        self.pmw_search_edit.setPlaceholderText("搜索名称 / 地址 / 备注")
        self.pmw_search_edit.textChanged.connect(self.pmw_on_filter_changed)
        layout.addWidget(self.pmw_search_edit)
        self.pmw_chk_only_enabled = QCheckBox("只看已启用")
        self.pmw_chk_only_available = QCheckBox("只看可用（有延迟）")
        self.pmw_chk_only_failed = QCheckBox("只看不可用")
        for check in (self.pmw_chk_only_enabled, self.pmw_chk_only_available, self.pmw_chk_only_failed):
            check.toggled.connect(self.pmw_on_filter_changed)
            layout.addWidget(check)
        return box

    def pmw_build_sidebar(self) -> QWidget:
        """左侧：分组、协议筛选、关键字与快捷操作（分区留白，避免拥挤与文字截断）。"""
        panel = QFrame()
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel.setMinimumWidth(230)
        panel.setMaximumWidth(340)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # v1.9.50：分组与筛选整块由 pmw_build_group_filter_widget() 提供（工具栏要用同一份）
        self.pmw_group_filter_box = self.pmw_build_group_filter_widget()
        layout.addWidget(self.pmw_group_filter_box, 1)

        # ---- 快捷操作（v1.6.5：整块装进容器，设置里可整体隐藏）----
        self.pmw_quick_actions_box = QWidget()
        quick_box = QVBoxLayout(self.pmw_quick_actions_box)
        quick_box.setContentsMargins(10, 0, 10, 10)
        quick_box.setSpacing(5)
        quick_box.addSpacing(9)
        quick_box.addWidget(self.pmw_make_section_separator())
        quick_box.addSpacing(7)
        quick_box.addWidget(self.pmw_make_section_label("快捷操作"))
        quick_common = QGridLayout()
        quick_common.setSpacing(5)
        common_actions = [
            ("全部启用", lambda: self.pmw_set_all_enabled(True)),
            ("全部停用", lambda: self.pmw_set_all_enabled(False)),
            ("反转选择", self.pmw_invert_selection),
            ("按延迟排序", self.pmw_sort_by_latency),
        ]
        for index, (title, handler) in enumerate(common_actions):
            button = QPushButton(title)
            button.clicked.connect(handler)
            quick_common.addWidget(button, index // 2, index % 2)
        quick_box.addLayout(quick_common)
        more_button = QToolButton()
        more_button.setText("更多操作 ▾")
        more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        more_menu = QMenu(more_button)
        act_enable_sel = more_menu.addAction("启用选中")
        act_enable_sel.triggered.connect(lambda: self.pmw_set_selected_enabled(True))
        act_disable_sel = more_menu.addAction("停用选中")
        act_disable_sel.triggered.connect(lambda: self.pmw_set_selected_enabled(False))
        more_menu.addSeparator()
        # v1.7.0：分页后「全选」= 全部筛选结果（跨页），另给「只选本页」
        act_select_all = more_menu.addAction("全选（筛选结果）")
        act_select_all.triggered.connect(self.pmw_select_all_filtered)
        act_select_page = more_menu.addAction("只选本页")
        act_select_page.triggered.connect(self.pmw_select_current_page)
        more_menu.addSeparator()
        act_copy = more_menu.addAction("复制地址")
        act_copy.triggered.connect(self.pmw_copy_selected_address)
        act_connect = more_menu.addAction("连接选中")
        act_connect.triggered.connect(self.pmw_connect_selected_node)
        more_button.setMenu(more_menu)
        quick_box.addWidget(more_button)
        layout.addWidget(self.pmw_quick_actions_box)
        if not self.pmw_settings.psm_get_bool("ui.show_quick_actions", True):
            self.pmw_quick_actions_box.hide()
        # v1.9.50（审计第 12 项）：工具栏「分组 / 搜索」按钮与这份控件接线。
        # 放在**这里**而不是 pmw_build_toolbar()：按钮先建好保证工具栏顺序不变，
        # 控件要等侧栏建完才存在。
        self.pmw_filter_button.ptfb_set_panel(self.pmw_group_filter_box, panel)
        return panel

    def pmw_make_section_label(self, pms_title: str) -> QLabel:
        """侧栏小节标题（加粗，配合分隔线形成分区）。"""
        label = QLabel(pms_title)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def pmw_make_section_separator(self) -> QFrame:
        """侧栏分区细分隔线。"""
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        return separator

    def pmw_build_node_area(self) -> QWidget:
        """节点表格区域（「全部节点」标签页；TgProxy / TgWebProxy 两页见 `mainwindow_tgtabs.py`）。

        v1.9.76：本方法只负责「全部节点」这一页。控件赋值后会被 `_PmwTgTabsMixin.pmwt_build_tabs()`
        收进 `self.pmwt_views`，之后 `self.pmw_node_table` 等属性一律按「活动标签页」转发。
        """
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.pmw_node_model = PmsNodeTableModel(self)
        self.pmw_node_proxy = PmsNodeFilterProxyModel(self)          # 筛选 + 排序（全量）
        self.pmw_node_proxy.setSourceModel(self.pmw_node_model)
        # v1.7.0：分页层。源 = 筛选层，所以它看到的序号就是「筛选后的序号」，
        # 直接切区间即可；排序必须转交给筛选层，否则会只排当前页，看着像乱序。
        self.pmw_node_page = PmsNodeFilterProxyModel(self)
        self.pmw_node_page.setSourceModel(self.pmw_node_proxy)
        self.pmw_node_page.pnfp_sort_target = self.pmw_node_proxy
        self.pmw_node_table = QTableView()
        self.pmw_node_table.setModel(self.pmw_node_page)
        self.pmw_node_table.setSortingEnabled(True)
        self.pmw_node_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.pmw_node_table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.pmw_node_table.setAlternatingRowColors(True)
        self.pmw_node_table.setWordWrap(False)
        self.pmw_node_table.verticalHeader().setVisible(False)
        # v1.9.23：代理实例要留着——延迟列进度条的粗细由设置 `ui.latency_bar_height` 灌进它
        self.pmw_bar_delegate = PmsLatencyBarDelegate(self.pmw_node_table)
        self.pmw_node_table.setItemDelegate(self.pmw_bar_delegate)
        # v1.6.1：双击 = 连接该节点（启动核心），编辑改到右键菜单里
        self.pmw_node_table.doubleClicked.connect(lambda _index: self.pmw_connect_selected_node())
        # v1.6.7：选中变化必须刷新工具栏动作启用状态。
        # 之前只在「重新加载 / 筛选变化」时刷新，导致启动后选中节点，
        # 「编辑 / 删除 / 分享 / 复制链接 / 复制配置 / 复制节点」一直是禁用的，点了没反应。
        # v1.7.0：同时要清掉「全选筛选结果」这个虚拟范围——用户一旦手动改选，就以手动的为准。
        self.pmw_node_table.selectionModel().selectionChanged.connect(self.pmw_on_selection_changed)
        self.pmw_node_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.pmw_node_table.customContextMenuRequested.connect(self.pmw_show_node_context_menu)
        header = self.pmw_node_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.sectionResized.connect(self.pmw_on_column_resized)
        header.sortIndicatorChanged.connect(self.pmw_on_sort_changed)
        # v1.9.14：sectionPressed 早于 sortIndicatorChanged 发出，用它把「这是用户点表头」标记下来，
        # 才能在 pmw_on_sort_changed() 里区分「点表头」与「代码里主动排序」（自动排序 / 启动恢复）。
        header.sectionPressed.connect(self.pmw_on_header_section_pressed)
        for index, width in enumerate(PMS_COLUMN_MIN_WIDTHS):
            self.pmw_node_table.setColumnWidth(index, width)
        self.pmw_connect_signals_pmwt_placeholder = None  # 占位：真正的接线在 pmw_connect_signals()
        layout.addWidget(self.pmw_node_table, 1)
        layout.addWidget(self.pmw_build_pagination_bar())
        self.pmw_node_summary_label = QLabel("节点：-")
        self.pmw_node_summary_label.setObjectName("hintLabel")
        layout.addWidget(self.pmw_node_summary_label)
        return container

    def pmw_build_pagination_bar(self) -> QWidget:
        """节点表格分页栏（v1.7.0）：翻页 + 每页条数 + 选中范围提示。

        v1.9.76：本方法只给「全部节点」页用；两个电报专属页的分页栏由
        `_PmwTgTabsMixin.pmwt_build_tg_pagination_bar()` 生成（控件名 pmwt_ 前缀）。
        按钮 handler 都是无参 lambda，读的 `pmw_page_index/pmw_page_count/pmw_goto_page`
        本身按「活动标签页」转发，所以三页各自翻页互不干扰。
        """
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.pmw_btn_first_page = QPushButton("«")
        self.pmw_btn_prev_page = QPushButton("‹")
        self.pmw_btn_next_page = QPushButton("›")
        self.pmw_btn_last_page = QPushButton("»")
        for button, handler in (
            (self.pmw_btn_first_page, lambda: self.pmw_goto_page(1)),
            (self.pmw_btn_prev_page, lambda: self.pmw_goto_page(self.pmw_page_index() - 1)),
            (self.pmw_btn_next_page, lambda: self.pmw_goto_page(self.pmw_page_index() + 1)),
            (self.pmw_btn_last_page, lambda: self.pmw_goto_page(self.pmw_page_count())),
        ):
            button.setFixedWidth(26)
            button.clicked.connect(handler)
            layout.addWidget(button)
        self.pmw_label_page = QLabel("第 1 / 1 页")
        self.pmw_label_page.setObjectName("hintLabel")
        layout.addWidget(self.pmw_label_page)
        self.pmw_combo_page_size = QComboBox()
        for value in PMS_TABLE_PAGE_SIZES:
            self.pmw_combo_page_size.addItem(f"每页 {value} 条", value)
        self.pmw_combo_page_size.setFixedWidth(92)
        self.pmw_combo_page_size.currentIndexChanged.connect(self.pmw_on_page_size_changed)
        layout.addWidget(self.pmw_combo_page_size)
        self.pmw_btn_select_all = QPushButton("全选筛选结果")
        self.pmw_btn_select_all.setToolTip("选中**全部筛选结果**（跨页），不只是当前页")
        self.pmw_btn_select_all.clicked.connect(self.pmw_select_all_filtered)
        layout.addWidget(self.pmw_btn_select_all)
        self.pmw_btn_select_page = QPushButton("选本页")
        self.pmw_btn_select_page.setToolTip("只选中当前页的节点")
        self.pmw_btn_select_page.clicked.connect(self.pmw_select_current_page)
        layout.addWidget(self.pmw_btn_select_page)
        self.pmw_label_selection = QLabel("")
        self.pmw_label_selection.setObjectName("hintLabel")
        layout.addWidget(self.pmw_label_selection)
        layout.addStretch(1)
        return bar

    def pmw_build_log_panel(self) -> QWidget:
        """日志面板：级别过滤、关键字、自动滚动、导出、清空。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        control_row = QHBoxLayout()
        self.pmw_log_level_combo = QComboBox()
        for key, title in PMS_LOG_LEVEL_TITLES.items():
            self.pmw_log_level_combo.addItem(title, key)
        index = self.pmw_log_level_combo.findData(self.pmw_settings.psm_get_str("core.log_level", "info"))
        self.pmw_log_level_combo.setCurrentIndex(max(0, index))
        self.pmw_log_level_combo.currentIndexChanged.connect(self.pmw_on_log_level_changed)
        self.pmw_log_filter_edit = QLineEdit()
        self.pmw_log_filter_edit.setPlaceholderText("日志关键字过滤")
        self.pmw_log_filter_edit.textChanged.connect(self.pmw_on_log_filter_changed)
        self.pmw_chk_log_scroll = QCheckBox("自动滚动")
        self.pmw_chk_log_scroll.setChecked(self.pmw_settings.psm_get_bool("ui.log_auto_scroll", True))
        self.pmw_chk_log_scroll.toggled.connect(lambda value: self.pmw_settings.psm_set("ui.log_auto_scroll", bool(value)))
        export_button = QPushButton("导出日志")
        export_button.clicked.connect(self.pmw_export_log)
        clear_button = QPushButton("清空面板")
        clear_button.clicked.connect(self.pmw_clear_log_panel)
        control_row.addWidget(QLabel("级别"))
        control_row.addWidget(self.pmw_log_level_combo)
        control_row.addWidget(self.pmw_log_filter_edit, 1)
        control_row.addWidget(self.pmw_chk_log_scroll)
        control_row.addWidget(export_button)
        control_row.addWidget(clear_button)
        layout.addLayout(control_row)
        self.pmw_log_model = PmsLogTableModel(self.pmw_settings.psm_get_int("ui.log_max_lines", 2000), self)
        self.pmw_log_view = QTableView()
        self.pmw_log_view.setModel(self.pmw_log_model)
        self.pmw_log_view.verticalHeader().setVisible(False)
        self.pmw_log_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.pmw_log_view.setWordWrap(False)
        header = self.pmw_log_view.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.pmw_log_view.setColumnWidth(0, 96)
        self.pmw_log_view.setColumnWidth(1, 64)
        self.pmw_log_view.setColumnWidth(2, 150)
        # v1.9.16：ui.log_show_time 原来没人读（勾了不生效），这里真正隐藏/显示「时间」列
        self.pmw_log_view.setColumnHidden(0, not self.pmw_settings.psm_get_bool("ui.log_show_time", True))
        font = QFont(self.pmw_log_view.font())
        font.setPointSizeF(self.pmw_settings.psm_get_float("ui.log_font_size", 9.0))
        self.pmw_log_view.setFont(font)
        layout.addWidget(self.pmw_log_view, 1)
        return panel

    def pmw_build_connection_panel(self) -> QWidget:
        """连接面板：真实连接列表（Clash API 或日志解析），支持关闭单条/全部。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        row = QHBoxLayout()
        self.pmw_connection_label = QLabel("尚未获取连接")
        self.pmw_connection_label.setObjectName("hintLabel")
        close_one = QPushButton("关闭选中连接")
        close_one.clicked.connect(self.pmw_close_selected_connection)
        close_all = QPushButton("关闭全部连接")
        close_all.clicked.connect(self.pmw_close_all_connections)
        clear_button = QPushButton("清空列表")
        clear_button.clicked.connect(lambda: self.pmw_connection_model.pctm_clear())
        row.addWidget(self.pmw_connection_label, 1)
        row.addWidget(close_one)
        row.addWidget(close_all)
        row.addWidget(clear_button)
        layout.addLayout(row)
        self.pmw_connection_model = PmsConnectionTableModel(self)
        self.pmw_connection_view = QTableView()
        self.pmw_connection_view.setModel(self.pmw_connection_model)
        self.pmw_connection_view.verticalHeader().setVisible(False)
        self.pmw_connection_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.pmw_connection_view.setWordWrap(False)
        self.pmw_connection_view.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.pmw_connection_view.setColumnWidth(0, 140)
        layout.addWidget(self.pmw_connection_view, 1)
        return panel

    def pmw_build_statistics_panel(self) -> QWidget:
        """统计面板：累计流量、速度、采样历史与来源说明。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        grid = QGridLayout()
        self.pmw_stats_labels: dict[str, QLabel] = {}
        rows = [
            ("proxy_up", "代理上传"), ("proxy_down", "代理下载"),
            ("direct_up", "直连上传"), ("direct_down", "直连下载"),
            ("speed_up", "当前上传速度"), ("speed_down", "当前下载速度"),
            ("connections", "活动连接数"), ("source", "统计来源"),
            ("uptime", "核心运行时长"), ("last_test", "最近一次测试"),
        ]
        for index, (key, title) in enumerate(rows):
            grid.addWidget(QLabel(title), index // 2, (index % 2) * 2)
            label = QLabel("-")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.pmw_stats_labels[key] = label
            grid.addWidget(label, index // 2, (index % 2) * 2 + 1)
        layout.addLayout(grid)
        history_label = QLabel("最近 20 次延迟测试记录")
        layout.addWidget(history_label)
        self.pmw_history_table = QTableWidget(0, 6)
        self.pmw_history_table.setHorizontalHeaderLabels(["时间", "节点", "方式", "延迟", "速率", "说明"])
        self.pmw_history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.pmw_history_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.pmw_history_table.verticalHeader().setVisible(False)
        layout.addWidget(self.pmw_history_table, 1)
        layout.addWidget(pms_make_hint_label(
            "统计来源说明：sing-box → Clash API 真实连接与字节数；Xray → xray api statsquery 真实字节数；"
            "仅日志模式只统计连接数量。所有数字都来自核心自身，不做猜测。"
        ))
        return panel

    def pmw_build_error_panel(self) -> QWidget:
        """错误面板（v1.8.0）：只收 error 及以上，最多 200 条，状态栏红点即入口。

        ★ 为什么不再建一个模型类：日志模型 ``PmsLogTableModel`` 就是「时间/级别/来源/内容」
        四列，复用它即可；这里把「级别」列藏掉（全是 error，重复没意义）。
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self.pmw_error_label = QLabel("暂无错误")
        self.pmw_error_label.setObjectName("hintLabel")
        copy_button = QPushButton("复制全部")
        copy_button.clicked.connect(self.pmw_copy_error_panel)
        export_button = QPushButton("导出")
        export_button.clicked.connect(self.pmw_export_error_panel)
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self.pmw_clear_error_panel)
        row = QHBoxLayout()
        row.addWidget(self.pmw_error_label, 1)
        row.addWidget(copy_button)
        row.addWidget(export_button)
        row.addWidget(clear_button)
        layout.addLayout(row)
        self.pmw_error_model = PmsLogTableModel(PMS_ERROR_LOG_MAX, self)
        self.pmw_error_model.pltm_set_min_level("error")
        self.pmw_error_view = QTableView()
        self.pmw_error_view.setModel(self.pmw_error_model)
        self.pmw_error_view.verticalHeader().setVisible(False)
        self.pmw_error_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.pmw_error_view.setWordWrap(False)
        self.pmw_error_view.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.pmw_error_view.setColumnWidth(0, 96)
        self.pmw_error_view.setColumnWidth(2, 150)
        self.pmw_error_view.setColumnHidden(1, True)   # 全是 error，「级别」列没有信息量
        font = QFont(self.pmw_error_view.font())
        font.setPointSizeF(self.pmw_settings.psm_get_float("ui.log_font_size", 9.0))
        self.pmw_error_view.setFont(font)
        layout.addWidget(self.pmw_error_view, 1)
        return panel

    def pmw_build_status_bar(self) -> None:
        """状态栏：核心状态、监听地址、流量、速度与全局进度条。"""
        status = QStatusBar(self)
        self.setStatusBar(status)
        self.pmw_status_core = QLabel("核心：未运行")
        self.pmw_status_listen = QLabel("监听：-")
        self.pmw_status_proxy = QLabel("代理：0 B ↑ / 0 B ↓")
        self.pmw_status_direct = QLabel("直连：0 B ↑ / 0 B ↓")
        self.pmw_status_speed = QLabel("速度：-")
        self.pmw_status_message = QLabel("就绪")
        # v1.8.0：错误红点——数字是**未读数**，点开「错误」页签即清零；为 0 时整个按钮隐藏
        self.pmw_btn_errors = QPushButton("⚠ 0")
        self.pmw_btn_errors.setObjectName("errorBadge")
        self.pmw_btn_errors.setToolTip("查看最近的错误（点开即把未读清零）")
        self.pmw_btn_errors.setStyleSheet(
            "QPushButton#errorBadge{color:#ffffff;background:#d0342c;border:none;border-radius:9px;"
            "padding:2px 9px;font-weight:600;}"
            "QPushButton#errorBadge:hover{background:#e5534b;}"
        )
        self.pmw_btn_errors.clicked.connect(self.pmw_open_error_panel)
        self.pmw_btn_errors.hide()
        self.pmw_status_progress = QProgressBar()
        self.pmw_status_progress.setRange(0, 100)
        self.pmw_status_progress.setValue(0)
        self.pmw_status_progress.setFixedWidth(180)
        for widget in (self.pmw_status_core, self.pmw_status_listen, self.pmw_status_proxy, self.pmw_status_direct, self.pmw_status_speed):
            status.addPermanentWidget(widget)
        status.addPermanentWidget(self.pmw_btn_errors)
        status.addWidget(self.pmw_status_message, 1)
        status.addPermanentWidget(self.pmw_status_progress)
        status.showMessage("就绪", 3000)

    def pmw_connect_signals(self) -> None:
        """把所有服务信号接到界面处理函数。"""
        self.pmw_node_model.pntm_node_toggled.connect(self.pmw_on_node_toggled)
        self.pmw_core.pcp_state_changed.connect(self.pmw_on_core_state_changed)
        self.pmw_core.pcp_output_received.connect(self.pmw_on_core_output)
        self.pmw_core.pcp_core_error.connect(self.pmw_on_core_error)
        self.pmw_core.pcp_core_stopped.connect(self.pmw_on_core_stopped)
        self.pmw_tracker.pct_connections_updated.connect(self.pmw_on_connections_updated)
        self.pmw_traffic.pts_traffic_updated.connect(self.pmw_on_traffic_updated)
        self.pmw_traffic.pts_source_changed.connect(lambda text: self.pmw_label_source.setText(f"统计源：{text}"))
        self.pmw_latency.plt_progress.connect(self.pmw_on_latency_progress)
        self.pmw_latency.plt_node_result.connect(self.pmw_on_latency_node_result)
        self.pmw_latency.plt_message.connect(self.pmw_show_message)
        self.pmw_latency.plt_finished.connect(self.pmw_on_latency_finished)
        self.pmw_speed.pst_progress.connect(self.pmw_on_speed_progress)
        self.pmw_speed.pst_node_result.connect(self.pmw_on_speed_node_result)
        self.pmw_speed.pst_message.connect(self.pmw_show_message)
        self.pmw_speed.pst_finished.connect(self.pmw_on_speed_finished)
        # v1.9.59：电报 DC 测试（进度 / 单节点结果 / 消息 / 收尾）
        self.pmw_telegram.ptt_progress.connect(self.pmw_on_telegram_progress)
        self.pmw_telegram.ptt_node_result.connect(self.pmw_on_telegram_node_result)
        self.pmw_telegram.ptt_message.connect(self.pmw_show_message)
        self.pmw_telegram.ptt_finished.connect(self.pmw_on_telegram_finished)
        self.pmw_subscription.psu_progress.connect(self.pmw_on_subscription_progress)
        self.pmw_subscription.psu_subscription_done.connect(self.pmw_on_subscription_done)
        self.pmw_subscription.psu_message.connect(self.pmw_show_message)
        self.pmw_subscription.psu_finished.connect(self.pmw_on_subscription_finished)
        self.pmw_downloader.pcdl_progress.connect(self.pmw_on_core_download_progress)
        self.pmw_downloader.pcdl_finished.connect(self.pmw_on_core_download_finished)
        # v1.9.17：整批（队列）跑完给一次明确汇总
        self.pmw_downloader.pcdl_batch_finished.connect(self.pmw_on_core_batch_finished)
        self.pmw_settings.psm_value_changed.connect(self.pmw_on_setting_changed)
        # v1.6.7：导入设置 / 重置设置发的是不带键的 psm_values_reloaded，
        # 之前没人订阅，导入完界面、主题、定时器、核心全是旧的。
        self.pmw_settings.psm_values_reloaded.connect(self.pmw_reapply_all_settings)
        self.pmw_log_bus.plb_record_emitted.connect(self.pmw_on_log_record)
        self.pmw_log_bus.plb_attach_bus_handler(self.pmw_settings.psm_get_str("core.log_level", "info"))
        # v1.8.0：用户自己翻到「错误」页签也算看过，未读清零
        self.pmw_bottom_tabs.currentChanged.connect(self.pmw_on_bottom_tab_changed)

    def pmw_start_timers(self) -> None:
        """启动各类定时器：界面刷新、订阅自动更新、定时测试、剪贴板监听。"""
        self.pmw_ui_timer = QTimer(self)
        self.pmw_ui_timer.timeout.connect(self.pmw_on_ui_tick)
        self.pmw_ui_timer.start(PMS_STATUS_TICK_MS)
        self.pmw_subscription_timer = QTimer(self)
        self.pmw_subscription_timer.timeout.connect(self.pmw_on_auto_subscription_tick)
        interval = self.pmw_settings.psm_get_int("subscription.auto_update_interval_minutes", 360)
        if self.pmw_settings.psm_get_bool("subscription.auto_update_enabled", False) and interval > 0:
            self.pmw_subscription_timer.start(max(10, interval) * 60 * 1000)
        self.pmw_test_timer = QTimer(self)
        self.pmw_test_timer.timeout.connect(self.pmw_on_auto_test_tick)
        test_interval = self.pmw_settings.psm_get_int("test.auto_test_interval_minutes", 0)
        if test_interval > 0:
            self.pmw_test_timer.start(test_interval * 60 * 1000)
        self.pmw_clipboard_timer = QTimer(self)
        self.pmw_clipboard_timer.timeout.connect(self.pmw_on_clipboard_tick)
        if self.pmw_settings.psm_get_bool("advanced.clipboard_watch", False):
            self.pmw_clipboard_timer.start(1200)
        # 本地 HTTP 接口：状态提供者只给数据，服务自己管线程与鉴权
        self.pmw_api.papi_set_state_provider(self.pmw_api_state)
        self.pmw_api_server.pas_failed.connect(self.pmw_on_api_server_failed)
        self.pmw_api_server.pas_apply_settings()
        # 定时健康检测与自动切换：状态由本窗口提供，切换请求回到本窗口执行
        self.pmw_autoswitch.pasw_set_state_provider(self.pmw_autoswitch_state)
        # v1.8.0：候选综合评分要的历史成功率，由仓库按 uid 批量查（服务本身不认识数据库）
        self.pmw_autoswitch.pasw_set_stats_provider(self.pmw_autoswitch_stats)
        self.pmw_autoswitch.pasw_status.connect(self.pmw_on_autoswitch_status)
        self.pmw_autoswitch.pasw_checked.connect(self.pmw_on_autoswitch_checked)
        self.pmw_autoswitch.pasw_switch_requested.connect(self.pmw_on_autoswitch_switch)
        self.pmw_autoswitch.pasw_start()
        # v1.9.37：节点健康检查（周期体检 + 自动禁用 / 自动复活）——与手动测试、订阅都无关
        self.pmw_setup_health_check()

    def pmw_add_submenu(self, pms_parent: QMenu, pms_title: str) -> QMenu:
        """给菜单挂一个二级菜单，返回它（v1.9.17）。

        **必须显式指定父对象**：PySide6 下 `parent.addMenu("标题")` 返回的 `QMenu` 被判定为
        「Python 拥有」，Python 包装对象一出作用域 C++ 对象就被销毁——子菜单会变成空壳
        （用 `menu.actions()[i].menu()` 一访问就报 `Internal C++ object already deleted`，已实测）。
        用 `QMenu(标题, 父菜单)` + `addMenu(qmenu)` 才稳。
        """
        submenu = QMenu(pms_title, pms_parent)
        pms_parent.addMenu(submenu)
        return submenu

    def pmw_ask_text(self, pms_title: str, pms_label: str, pms_default: str = "") -> tuple[str, bool]:
        """简易输入框（避免为单个输入引入额外对话框类）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle(pms_title)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(pms_label))
        line_edit = QLineEdit(pms_default)
        layout.addWidget(line_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        result = dialog.exec() == QDialog.DialogCode.Accepted
        return line_edit.text(), result

    def pmw_show_environment_summary(self) -> None:
        """启动时把关键环境信息写入日志（便于排查）。"""
        cores = self.pmw_locator.pcl_find_any(self.pmw_settings)
        report = self.pmw_backup.pvbh_consistency_report()
        self.pmw_logger.info(
            "环境：Python %s / PySide6 %s / 管理员 %s",
            platform.python_version(), PmsSettingsDialog.pset_pyside_version(),
            "是" if PmsPrivilegeUtils.ppu_is_admin() else "否",
        )
        for kind, path in cores.items():
            if path is None:
                self.pmw_logger.warning("未找到核心：%s", PMS_CORE_KIND_TITLES.get(kind, kind))
            else:
                self.pmw_logger.info("核心 %s：%s（版本 %s）", kind, path.name, self.pmw_locator.pcl_detect_version(path))
        if not report["consistent"]:
            self.pmw_logger.warning("版本/文档/备份未同步：%s", report)
        if self.pmw_settings.psm_get_bool("general.auto_start_with_windows", False):
            controller = PmsWindowsAutoStart(self.pmw_paths)
            if not controller.pwas_is_enabled():
                self.pmw_logger.info("开机自启设置为开启，但注册表项不存在，正在补写…")
                controller.pwas_set_enabled(True)
        self.pmw_log_model.pltm_set_min_level(self.pmw_effective_log_level())

    def pmw_show_message(self, pms_text: str) -> None:
        """状态栏短提示 + 日志（避免弹窗打断）。"""
        text = str(pms_text or "").strip()
        if not text:
            return
        self.pmw_status_message.setText(text)
        self.statusBar().showMessage(text, 5000)
        self.pmw_logger.info("%s", text)
