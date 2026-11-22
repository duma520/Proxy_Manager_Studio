# -*- coding: utf-8 -*-
"""主窗口

v1.9.36：本文件从 4445 行拆到 10 个 mixin（见 `mainwindow_*.py`），这里只留 `__init__` 与类定义；**方法名、`self.pmw_*` 属性名、对外行为全部不变**。
"""
from __future__ import annotations

import logging
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QMainWindow, QWidget
from pms.api import PmsApi
from pms.apiclash import PmsClashCompat
from pms.apicommands import PmsApiCommander, PmsApiDispatcher
from pms.apiserver import PmsApiServer
from pms.autoswitch import PmsAutoSwitchService
from pms.constants import PMS_LOGGER_NAME
from pms.coreprocess import PmsCoreProcessController
from pms.coresys import PmsCoreLocator, PmsPortAllocator
from pms.database import PmsDatabaseEngine, PmsNodeRepository
from pms.dctest import PmsTelegramTestCoordinator
from pms.download import PmsCoreDownloadService
from pms.envstore import PmsEnvStore, PmsSecretVault
from pms.gui.dialogs.settingsdialog import PmsSettingsDialog
from pms.gui.dialogs.subscriptiondialog import PmsSubscriptionDialog
from pms.gui.tray import PmsTrayController
from pms.icons import pms_load_app_icon
from pms.importers import PmsTextImportClassifier
from pms.latency import PmsLatencyTestCoordinator
from pms.logbus import PmsLogBus
from pms.logparse import PmsConnectionTracker
from pms.models import PmsProxyNode
from pms.paths import PmsRuntimePaths
from pms.sharelinks import PmsShareLinkParser, PmsShareLinkWriter
from pms.speedtest import PmsSpeedTestCoordinator
from pms.subscription import PmsSubscriptionService
from pms.theme import PmsThemeManager
from pms.traffic import PmsTrafficMonitorService
from pms.versioning import PmsVersionBackupHelper
from typing import Optional

from pms.gui.mainwindow_ui import _PmwUiMixin
from pms.gui.mainwindow_events import _PmwEventMixin
from pms.gui.mainwindow_state import _PmwStateMixin
from pms.gui.mainwindow_nodes import _PmwNodeMixin
from pms.gui.mainwindow_tgtabs import _PmwTgTabsMixin
from pms.gui.mainwindow_import import _PmwImportMixin
from pms.gui.mainwindow_tests import _PmwTestMixin
from pms.gui.mainwindow_core import _PmwCoreMixin
from pms.gui.mainwindow_settings import _PmwSettingsMixin
from pms.gui.mainwindow_log import _PmwLogMixin
from pms.gui.mainwindow_shell import _PmwShellMixin
from pms.gui.mainwindow_health import _PmwHealthMixin


class PmsMainWindow(_PmwUiMixin, _PmwEventMixin, _PmwStateMixin, _PmwTgTabsMixin, _PmwNodeMixin, _PmwImportMixin, _PmwTestMixin, _PmwCoreMixin, _PmwSettingsMixin, _PmwLogMixin, _PmwShellMixin, _PmwHealthMixin, QMainWindow):
    """主窗口"""

    def __init__(
        self,
        pms_paths: PmsRuntimePaths,
        pms_settings: "PmsSettingsManager",
        pms_env: PmsEnvStore,
        pms_log_bus: PmsLogBus,
        pms_engine: PmsDatabaseEngine,
        pms_repo: PmsNodeRepository,
        pms_vault: PmsSecretVault,
        pms_parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pmw_paths = pms_paths
        self.pmw_settings = pms_settings
        self.pmw_env = pms_env
        self.pmw_log_bus = pms_log_bus
        self.pmw_engine = pms_engine
        self.pmw_repo = pms_repo
        self.pmw_vault = pms_vault
        self.pmw_logger = logging.getLogger(PMS_LOGGER_NAME + ".window")
        self.pmw_theme = PmsThemeManager()
        self.pmw_locator = PmsCoreLocator(pms_paths)
        self.pmw_allocator = PmsPortAllocator(pms_settings)
        self.pmw_core = PmsCoreProcessController(pms_settings, pms_paths, self.pmw_locator, self)
        self.pmw_tracker = PmsConnectionTracker(self)
        self.pmw_traffic = PmsTrafficMonitorService(pms_settings, self.pmw_tracker, self)
        self.pmw_traffic.pts_attach_repository(pms_repo)
        # v1.8.0：把数据库引擎交给两个调度器，断点才能写进 pms_meta
        self.pmw_latency = PmsLatencyTestCoordinator(pms_settings, pms_paths, self.pmw_locator, self, pms_engine)
        self.pmw_speed = PmsSpeedTestCoordinator(pms_settings, pms_paths, self.pmw_locator, self, pms_engine)
        # v1.9.59：电报 DC 测试调度器（逐节点起临时核心，断点 kind=telegram_dc）
        self.pmw_telegram = PmsTelegramTestCoordinator(pms_settings, pms_paths, self.pmw_locator, self, pms_engine)
        self.pmw_import_classifier = PmsTextImportClassifier(PmsShareLinkParser())
        self.pmw_link_writer = PmsShareLinkWriter()
        self.pmw_subscription = PmsSubscriptionService(pms_settings, pms_repo, pms_env, self.pmw_import_classifier, self)
        self.pmw_downloader = PmsCoreDownloadService(pms_paths, self, pms_settings)
        self.pmw_api = PmsApi(pms_settings, pms_repo)
        self.pmw_api_server = PmsApiServer(pms_settings, pms_env, self.pmw_api, self)
        # v1.6.2 写操作：派发器持有本窗口（动作表在 apicommands.py，不进主窗口），
        # 工作线程发信号 → 这里在界面线程执行 → 结果回填给等待中的请求线程
        self.pmw_api_commander = PmsApiCommander(self)
        self.pmw_api_dispatcher = PmsApiDispatcher(self, self.pmw_api_commander, self)
        self.pmw_api.papi_set_commander(self.pmw_api_commander)
        self.pmw_api_commander.pcmd_requested.connect(self.pmw_api_dispatcher.papd_on_command)
        # v1.6.4：Clash / sing-box External Controller 兼容层（只做语义翻译，数据仍走上面这套）
        self.pmw_clash_compat = PmsClashCompat(pms_settings, self.pmw_api, self)
        self.pmw_api_server.pas_set_clash_compat(self.pmw_clash_compat)
        # 定时健康检测与自动切换：服务只管检测与决策，真正切换由 pmw_on_autoswitch_switch 执行
        self.pmw_autoswitch = PmsAutoSwitchService(pms_settings, self)
        self.pmw_backup = PmsVersionBackupHelper(pms_paths)
        self.pmw_nodes: list[PmsProxyNode] = []
        # v1.9.72 卡顿修复：标记「是否正在整库重载」。后台异步加载期间为 True，
        # 用于防止重复发起加载、并在加载完成前屏蔽依赖数据的后续操作（如自动启动核心）。
        self.pmw_loading = False
        # v1.9.72 卡顿修复：后台整库加载任务的保活引用（防 PySide6 把 QRunnable 提前 GC）。
        # 任务开始时挂上、回包后清空；初值 None 避免首次访问未定义。
        self.pmw_load_task: Any = None
        self.pmw_load_signals: Any = None
        # v1.9.72 卡顿修复：启动加载完成后再触发的两个一次性动作（依赖节点库就绪）。
        # 由 app.py 按设置项置 True；加载完成的回调 pmw_on_startup_after_load() 消费它们。
        self.pmw_startup_auto_core = False
        self.pmw_startup_auto_subscription = False
        # v1.9.27：uid → 节点 的索引缓存。测试时**每报回一个节点**都要按 uid 取节点，
        # 4 万节点用 `next(... for ... in pmw_nodes)` 线性找 → 实测 4 万次共 **542 秒**纯界面线程 CPU，
        # 换字典后 0.04 秒。缓存与 pmw_nodes 用「对象身份 + 长度」双重校验，换过列表就自动重建。
        self.pmw_node_index_cache: dict[str, PmsProxyNode] = {}
        self.pmw_node_index_source: Optional[list[PmsProxyNode]] = None
        # v1.7.0 分页：pmw_select_all_scope="filtered" 表示「用户选的是全部筛选结果（跨页）」；
        # pmw_applying_selection 用来区分「程序化全选」与「用户手动改选」，避免把自己清掉。
        self.pmw_select_all_scope = ""
        self.pmw_applying_selection = False
        # v1.9.67：`pmw_select_all_scope="filtered"` 现在可能带一个**协议类型白名单**
        # （右键 ▸ 选择 ▸ 按类型选择）。空集合 = 没有类型限制（= 真正的「全选筛选结果」）。
        # `pmw_selected_nodes()` 会据此把范围收窄，避免「按类型选中」后批量操作误伤全部节点。
        self.pmw_select_by_type_uids: set[str] = set()
        self.pmw_applying_page_size = False
        # v1.9.76：节点区三标签（全部节点 / TgProxy / TgWebProxy）。转发属性在 _PmwUiMixin 里，
        # 这里先占位，保证 pmw_build_node_area() 赋值前的读取不会 AttributeError
        for _pmw_name in (
            "_pmw_node_proxy", "_pmw_node_page", "_pmw_node_table",
            "_pmw_bar_delegate", "_pmw_node_summary_label",
            "_pmw_pmw_label_page", "_pmw_pmw_combo_page_size", "_pmw_pmw_label_selection",
            "_pmw_pmw_btn_first_page", "_pmw_pmw_btn_prev_page", "_pmw_pmw_btn_next_page",
            "_pmw_pmw_btn_last_page", "_pmw_pmw_btn_select_all", "_pmw_pmw_btn_select_page",
        ):
            setattr(self, _pmw_name, None)
        # v1.9.14：区分「用户点表头排序」与「代码主动排序」——sectionPressed 里置 True，
        # pmw_on_sort_changed() 里消费掉；默认 False（= 代码主动排序，按「全量」处理）
        self.pmw_header_sort_click = False
        # v1.9.30：点表头前选中的 uid（排完按 uid 还原选中，见 pmw_restore_selected_uids）
        self.pmw_pending_selected_uids: list[str] = []
        self.pmw_port_plan: dict[str, int] = {}
        self.pmw_current_node_uid = ""
        self.pmw_settings_dialog: Optional[PmsSettingsDialog] = None
        self.pmw_subscription_dialog: Optional[PmsSubscriptionDialog] = None
        # v1.9.62：内置 PAC 脚本编辑器（不 modal，与设置对话框同一缓存口径）
        self.pmw_pac_editor_dialog: Optional[Any] = None
        self.pmw_last_clipboard_text = ""
        self.pmw_quitting = False
        self.pmw_tray: Optional[PmsTrayController] = None
        # v1.8.0：错误面板未读数（点开「错误」页签即清零，不落盘——重启后没有「未读」这个概念）
        self.pmw_error_unread = 0
        self.pmw_error_tab_index = 0
        # v1.8.0：事件推送的去重 / 节流基准（node.switched 只在真的换节点时发；traffic 至少隔 5 秒发一次）
        self.pmw_event_node_uid = ""
        self.pmw_event_traffic_at = 0.0
        # 线程池统一在构造期建好：TCP 延迟测试与电报纯抓共用，
        # 避免「先用电报导入、没测过 TCP」时用到还不存在的池子
        self.pmw_tcp_pool = QThreadPool(self)
        self.pmw_tcp_pool.setMaxThreadCount(max(1, pms_settings.psm_get_int("test.concurrency", 8)))
        self.pmw_tcp_results: dict[str, int] = {}
        self.pmw_tcp_total = 0
        self.pmw_tcp_done = 0
        # v1.9.22：进度条统一由 pmw_begin/end_test_progress() 管；这两个字段记本轮在测什么、共几个节点。
        # 以前各入口各自 show()/hide()，API 驱动的测试与部分批量操作分支漏了；而且条子长在「速览条」里，
        # 用户把速览条隐藏后就完全看不到进度（pmw_progress_restore_quick 负责测完还原）。
        self.pmw_progress_kind = ""
        self.pmw_progress_total = 0
        self.pmw_progress_restore_quick = False
        # v1.9.22：本轮测速要不要在完成后弹汇总框（用户手动发起的要，接口 / 自动发起的不打扰）
        self.pmw_speed_dialog_wanted = False
        # v1.9.65：串行测试链状态（延迟→测速→电报，由「选择测试」弹窗按勾选项启动）；默认空链，未运行时无副作用
        self.pmw_test_chain: list[tuple[str, str]] = []
        self.pmw_test_chain_total = 0
        self.pmw_test_chain_done = 0
        self.pmw_test_chain_ran_any = False
        self.pmw_test_chain_stopped = False
        self.pmw_test_chain_only_untested = False
        # v1.9.61：「仅测新增」勾选开关：勾上后延迟/测速/电报三种测试的范围自动加 _untested
        self.pmw_only_untested = False
        self.pmw_icon = pms_load_app_icon(pms_paths)
        self.pmw_build_ui()
        self.pmw_connect_signals()
        self.pmw_restore_ui_state()
        # v1.9.72 卡顿修复：启动整库加载改走后台线程（55k 节点 ~13.7s 不再卡 UI）。
        # 节点加载完**之后**才做「恢复上次节点 / 自动启动核心 / 启动订阅」，
        # 这些动作依赖 self.pmw_nodes，必须等数据就绪——放在 pms_on_done 回调里延迟执行，
        # 否则会出现「核心在 600ms 时就想连，但库还没读到」导致启动连不上上次节点。
        self.pmw_reload_nodes_async(
            "",
            pms_on_done=lambda _uid: self.pmw_on_startup_after_load(),
        )
        self.pmw_apply_theme()
        # v1.9.16：界面/后台杂项设置（日志时间列、DEBUG、日志文件大小、连接行数上限）
        self.pmw_apply_aux_settings()
        self.pmw_start_timers()
        self.pmw_show_environment_summary()
