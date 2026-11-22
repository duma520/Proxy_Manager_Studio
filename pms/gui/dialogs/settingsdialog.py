#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：设置中心

14 个标签页的设置中心，所有控件双向绑定到设置仓库（改动即保存）。
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Optional,
)
from PySide6.QtCore import (
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
import platform
import sys

from pms.optional import (
    PMS_HAS_DOTENV,
    PMS_HAS_PSUTIL,
    PMS_HAS_QRCODE,
    PMS_HAS_YAML,
)
from pms.constants import (
    PMS_APP_BUILD_DATE,
    PMS_APP_NAME,
    PMS_APP_VERSION,
    PMS_BLOCK_AD_DOMAIN_PRESET,
    PMS_BYPASS_DOMAIN_PRESET,
    PMS_COLUMN_TITLES,
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_TITLES,
    PMS_CORE_KIND_XRAY,
    PMS_DIRECT_DOMAIN_PRESET,
    PMS_LOG_LEVEL_TITLES,
    PMS_MAX_LOG_LINES,
    PMS_ROUTING_MODE_TITLES,
    PMS_RULESET_LOCAL_GEOSITE_CN,
    PMS_RULESET_SOURCE_TITLES,
    PMS_SUB_API_KEY_ENV,
    PMS_TEST_MODE_TITLES,
    PMS_THEME_TITLES,
    PMS_TUN_STACK_TITLES,
)
from pms.utils import (
    pms_format_bytes,
    pms_is_port_available,
    pms_multiline_to_list,
)
from pms.paths import PmsRuntimePaths
from pms.envstore import PmsEnvStore
from pms.database import PmsNodeRepository
from pms.coresys import (
    PmsCoreLocator,
    PmsPrivilegeUtils,
)
from pms.gui.widgets import pms_make_hint_label
from pms.gui.dialogs.coreversionwidget import _pcvw_build; from pms.gui.dialogs.torwidget import _ptw_build; from pms.gui.dialogs.maintenancewidget import _pmw_build; from pms.gui.dialogs.corematrixwidget import _pcmw_build  # v1.9.32/33/35：同行导入（本文件顶在 1000 行红线，多一行即破线）
from pms.gui.dialogs.apiwidget import PmsApiSettingsWidget
from pms.gui.dialogs.autoswitchwidget import PmsAutoSwitchSettingsWidget
from pms.gui.dialogs.statswidget import PmsStatsSettingsWidget
from pms.gui.dialogs.speedsourcewidget import PmsSpeedSourceWidget
from pms.gui.dialogs.testsortwidget import PmsTestSortWidget; from pms.gui.dialogs.testnoticewidget import PmsTestNoticeWidget; from pms.gui.dialogs.healthcheckwidget import PmsHealthCheckWidget  # v1.9.21/22/37：同行导入（本文件顶在 1000 行红线，多一行即破线）
from pms.gui.dialogs.settingssubtabs import pst_apply_subtabs, pst_build_footer
from pms.gui.dialogs.sysproxywidget import PmsSystemProxyWidget   # v1.9.49：系统代理 PAC 模式（控件在独立部件，本文件顶 1000 行红线）
from pms.gui.dialogs.githubwidget import PmsGithubSettingsWidget
from pms.gui.dialogs.uiwidget import PmsUiLayoutWidget
from pms.gui.dialogs.telegramwidget import PmsTelegramSettingsWidget

from pms.gui.dialogs.settingsdialog_tabs import _PsetTabsMixin

# --------------------------------------------------------------------------------------
# 对话框：设置（全部设置项改动即保存）
# --------------------------------------------------------------------------------------
class PmsSettingsDialog(_PsetTabsMixin, QDialog):
    """设置中心：所有改动立即写入 pms_settings.json，下次启动自动加载。"""

    psed_action = Signal(str, dict)   # 动作名, 参数（由主窗口执行：下载核心 / 提权 / 端口自检 …）

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_paths: PmsRuntimePaths,
        pms_env: PmsEnvStore,
        pms_locator: PmsCoreLocator,
        pms_repo: PmsNodeRepository,
        pms_parent: Optional[QWidget] = None,
        pms_local_proxy_port: int = 0,
    ) -> None:
        super().__init__(pms_parent)
        self.pset_settings = pms_settings
        self.pset_paths = pms_paths
        self.pset_env = pms_env
        self.pset_locator = pms_locator
        self.pset_repo = pms_repo
        # 电报部件需要知道本程序代理端口；主窗口没传就回落到设置里的混合端口
        self.pset_local_proxy_port = int(pms_local_proxy_port or 0) or pms_settings.psm_get_int("inbound.mixed_port", 0)
        self.pset_progress_bars: dict[str, QProgressBar] = {}
        self.setWindowTitle("设置")
        self.setMinimumSize(880, 700)
        self.pset_build_ui()
        self.pset_refresh_core_status()
        self.pset_refresh_env_status()
        self.pset_refresh_database_status()

    # ------------------------------------------------------------------ 绑定辅助
    def pset_bind_bool(self, pms_key: str, pms_widget: QCheckBox) -> QCheckBox:
        """复选框 ↔ 布尔设置（双向）。"""
        pms_widget.setChecked(self.pset_settings.psm_get_bool(pms_key, False))
        pms_widget.toggled.connect(lambda value, key=pms_key: self.pset_settings.psm_set(key, bool(value)))
        return pms_widget

    def pset_bind_int(self, pms_key: str, pms_widget: QSpinBox, pms_range: Optional[tuple[int, int]] = None) -> QSpinBox:
        """数字框 ↔ 整数设置。

        pms_range 必须在取值之前设置：QSpinBox 构造默认范围 0-99，
        若先 setValue 后 setRange，超过 99 的数值（如端口 20808）会被截断显示成 99。
        同时关闭 keyboardTracking：编辑完成（回车 / 失焦 / 点箭头）才写设置，
        避免逐键保存与「每敲一个键重启一次核心」。
        """
        if pms_range is not None:
            pms_widget.setRange(pms_range[0], pms_range[1])
        pms_widget.setKeyboardTracking(False)
        pms_widget.setValue(self.pset_settings.psm_get_int(pms_key, pms_widget.value()))
        pms_widget.valueChanged.connect(lambda value, key=pms_key: self.pset_settings.psm_set(key, int(value)))
        return pms_widget

    def pset_bind_float(self, pms_key: str, pms_widget: QDoubleSpinBox) -> QDoubleSpinBox:
        """浮点框 ↔ 浮点设置。"""
        pms_widget.setValue(self.pset_settings.psm_get_float(pms_key, pms_widget.value()))
        pms_widget.valueChanged.connect(lambda value, key=pms_key: self.pset_settings.psm_set(key, float(value)))
        return pms_widget

    def pset_bind_text(self, pms_key: str, pms_widget: QLineEdit) -> QLineEdit:
        """单行输入 ↔ 字符串设置。"""
        pms_widget.setText(self.pset_settings.psm_get_str(pms_key, ""))
        pms_widget.textChanged.connect(lambda value, key=pms_key: self.pset_settings.psm_set(key, str(value)))
        return pms_widget

    def pset_bind_combo(self, pms_key: str, pms_widget: QComboBox, pms_titles: dict[str, str]) -> QComboBox:
        """下拉框 ↔ 枚举设置。"""
        for value, title in pms_titles.items():
            pms_widget.addItem(title, value)
        current = self.pset_settings.psm_get_str(pms_key, "")
        index = pms_widget.findData(current)
        pms_widget.setCurrentIndex(max(0, index))
        pms_widget.currentIndexChanged.connect(
            lambda _index, key=pms_key, widget=pms_widget: self.pset_settings.psm_set(key, str(widget.currentData()))
        )
        return pms_widget

    def pset_bind_list(self, pms_key: str, pms_widget: QPlainTextEdit) -> QPlainTextEdit:
        """多行文本 ↔ 字符串列表（一行一项）。"""
        pms_widget.setPlainText("\n".join(self.pset_settings.psm_get_list(pms_key)))
        pms_widget.textChanged.connect(
            lambda key=pms_key, widget=pms_widget: self.pset_settings.psm_set(key, pms_multiline_to_list(widget.toPlainText()))
        )
        return pms_widget

    # ------------------------------------------------------------------ 界面骨架
    def pset_build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        self.pset_tabs = QTabWidget()
        self.pset_tabs.addTab(self.pset_build_general_tab(), "常规")
        self.pset_tabs.addTab(self.pset_build_core_tab(), "核心")
        self.pset_tabs.addTab(self.pset_build_inbound_tab(), "本地入口")
        self.pset_tabs.addTab(self.pset_build_system_proxy_tab(), "系统代理")
        self.pset_tabs.addTab(self.pset_build_tun_tab(), "TUN 模式")
        self.pset_tabs.addTab(self.pset_build_routing_tab(), "路由分流")
        self.pset_tabs.addTab(self.pset_build_test_tab(), "延迟测试")
        self.pset_tabs.addTab(self.pset_build_speed_tab(), "测速")
        self.pset_tabs.addTab(self.pset_build_subscription_tab(), "订阅")
        self.pset_tabs.addTab(self.pset_build_ui_tab(), "界面")
        self.pset_tabs.addTab(self.pset_build_database_tab(), "数据库")
        self.pset_tabs.addTab(self.pset_build_stats_tab(), "统计")
        self.pset_tabs.addTab(self.pset_build_advanced_tab(), "高级")
        self.pset_tabs.addTab(self.pset_build_api_tab(), "接口")
        self.pset_tabs.addTab(self.pset_build_autoswitch_tab(), "自动切换")
        self.pset_tabs.addTab(self.pset_build_about_tab(), "版本与环境")
        root.addWidget(self.pset_tabs, 1)
        # v1.9.17：把每页的分组框装进二级标签（一页 ≥2 组才有），并换上新的底部按钮
        pst_apply_subtabs(self.pset_tabs)
        root.addWidget(pst_build_footer(self, self.pset_settings, self.pset_tabs))

    @staticmethod
    def pset_make_scroll() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
        """返回带滚动区域的容器（小分辨率下也能完整显示设置项）。"""
        area = QScrollArea()
        area.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        area.setWidget(container)
        return area, container, layout

    # ------------------------------------------------------------------ 常规
    def pset_build_general_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        startup = QGroupBox("启动与退出")
        startup_form = QFormLayout(startup)
        self.pset_chk_auto_core = self.pset_bind_bool("general.auto_start_core", QCheckBox("启动程序后自动启动核心"))
        self.pset_chk_auto_node = self.pset_bind_bool("general.auto_connect_last_node", QCheckBox("自动连接上次使用的节点"))
        self.pset_chk_check_core = self.pset_bind_bool("general.check_core_on_launch", QCheckBox("启动核心前校验配置（调用核心自带 check）"))
        self.pset_chk_autostart = self.pset_bind_bool("general.auto_start_with_windows", QCheckBox("开机自动启动本程序（HKCU Run）"))
        self.pset_chk_single = self.pset_bind_bool("general.single_instance", QCheckBox("只允许运行一个实例"))
        startup_form.addRow(self.pset_chk_auto_core)
        startup_form.addRow(self.pset_chk_auto_node)
        startup_form.addRow(self.pset_chk_check_core)
        startup_form.addRow(self.pset_chk_autostart)
        startup_form.addRow(self.pset_chk_single)
        layout.addWidget(startup)
        tray = QGroupBox("托盘与提示")
        tray_form = QFormLayout(tray)
        self.pset_chk_min_tray = self.pset_bind_bool("general.minimize_to_tray", QCheckBox("最小化到托盘"))
        self.pset_chk_close_tray = self.pset_bind_bool("general.close_to_tray", QCheckBox("关闭窗口时最小化到托盘（不退出）"))
        self.pset_chk_confirm_exit = self.pset_bind_bool("general.confirm_on_exit", QCheckBox("退出前二次确认"))
        self.pset_chk_notify_error = self.pset_bind_bool("general.notify_on_core_error", QCheckBox("核心异常时托盘气泡提示"))
        self.pset_chk_notify_sub = self.pset_bind_bool("general.notify_on_subscription_done", QCheckBox("订阅更新完成后托盘气泡提示"))
        for widget in (self.pset_chk_min_tray, self.pset_chk_close_tray, self.pset_chk_confirm_exit, self.pset_chk_notify_error, self.pset_chk_notify_sub):
            tray_form.addRow(widget)
        layout.addWidget(tray)
        layout.addWidget(pms_make_hint_label("「自动连接上次使用的节点」只负责恢复并高亮上次退出前使用的节点；是否真的启动核心由「启动程序后自动启动核心」决定——两项都开（默认）= 开程序就自动连上上次用的那个节点。"))
        layout.addStretch(1)
        return area

    # ------------------------------------------------------------------ 核心
    def pset_build_core_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        group = QGroupBox("核心选择")
        form = QFormLayout(group)
        self.pset_combo_core = self.pset_bind_combo("core.kind", QComboBox(), PMS_CORE_KIND_TITLES)
        form.addRow("核心类型", self.pset_combo_core)
        self.pset_xray_path_edit = QLineEdit()
        self.pset_xray_path_edit.setPlaceholderText("留空则自动搜索 core\\xray\\ 与 PATH")
        self.pset_xray_path_edit.editingFinished.connect(self.pset_save_core_paths)
        xray_row = QHBoxLayout()
        browse_xray = QPushButton("浏览…")
        browse_xray.clicked.connect(lambda: self.pset_browse_core("core.xray_path", self.pset_xray_path_edit))
        xray_row.addWidget(self.pset_xray_path_edit, 1)
        xray_row.addWidget(browse_xray)
        form.addRow("xray.exe 路径", xray_row)
        self.pset_singbox_path_edit = QLineEdit()
        self.pset_singbox_path_edit.setPlaceholderText("留空则自动搜索 core\\sing-box\\ 与 PATH")
        self.pset_singbox_path_edit.editingFinished.connect(self.pset_save_core_paths)
        sing_row = QHBoxLayout()
        browse_sing = QPushButton("浏览…")
        browse_sing.clicked.connect(lambda: self.pset_browse_core("core.singbox_path", self.pset_singbox_path_edit))
        sing_row.addWidget(self.pset_singbox_path_edit, 1)
        sing_row.addWidget(browse_sing)
        form.addRow("sing-box.exe 路径", sing_row)
        self.pset_xray_path_edit.setText(self.pset_settings.psm_get_str("core.xray_path", ""))
        self.pset_singbox_path_edit.setText(self.pset_settings.psm_get_str("core.singbox_path", ""))
        self.pset_core_status_label = QLabel("")
        self.pset_core_status_label.setWordWrap(True)
        form.addRow("检测结果", self.pset_core_status_label)
        layout.addWidget(group)

        download_group = QGroupBox("下载 / 更新核心")
        download_layout = QGridLayout(download_group)
        self.pset_mirror_edit = self.pset_bind_text("core.download_mirror", QLineEdit())
        self.pset_mirror_edit.setPlaceholderText("可选：GitHub 加速前缀，如 https://ghfast.top")
        download_layout.addWidget(QLabel("下载镜像前缀"), 0, 0)
        download_layout.addWidget(self.pset_mirror_edit, 0, 1, 1, 2)
        button_xray = QPushButton("下载 / 更新 Xray")
        button_xray.clicked.connect(lambda: self.psed_action.emit("download_core", {"kind": PMS_CORE_KIND_XRAY}))
        button_sing = QPushButton("下载 / 更新 sing-box")
        button_sing.clicked.connect(lambda: self.psed_action.emit("download_core", {"kind": PMS_CORE_KIND_SINGBOX}))
        # v1.9.11：一键更新 sing-box + Xray，避免每个都点一遍（v1.9.45：独立内核已下线）
        button_all_cores = QPushButton("全部更新核心")
        button_all_cores.setToolTip("按顺序下载 / 更新 sing-box 与 Xray（两个主核心，串行执行）")
        button_all_cores.clicked.connect(lambda: self.psed_action.emit("download_core", {"kind": "__all__"}))
        download_layout.addWidget(button_all_cores, 1, 0)
        download_layout.addWidget(button_xray, 1, 1)
        download_layout.addWidget(button_sing, 1, 2)
        self.pset_core_progress = QProgressBar()
        self.pset_core_progress.setRange(0, 100)
        self.pset_core_progress.setValue(0)
        download_layout.addWidget(self.pset_core_progress, 2, 0, 1, 3)
        self.pset_core_progress_label = QLabel("未开始")
        self.pset_core_progress_label.setObjectName("hintLabel")
        download_layout.addWidget(self.pset_core_progress_label, 3, 0, 1, 3)
        # v1.9.17：整批更新的最终结果留一行——用户反馈「更新完不知道成没成」
        self.pset_last_download_label = QLabel("上次更新：尚未执行")
        self.pset_last_download_label.setObjectName("hintLabel")
        self.pset_last_download_label.setWordWrap(True)
        download_layout.addWidget(self.pset_last_download_label, 4, 0, 1, 3)
        # v1.9.45：7-Zip 路径输入从已删的「独立内核更新」部件搬到这里（Python 解压失败时的兜底工具）
        self.pset_seven_zip_edit = self.pset_bind_text("core.seven_zip_path", QLineEdit())
        self.pset_seven_zip_edit.setPlaceholderText("可选：7z.exe 路径，留空自动探测（Python 解压失败时兜底）")
        download_layout.addWidget(QLabel("7-Zip 路径"), 5, 0)
        download_layout.addWidget(self.pset_seven_zip_edit, 5, 1, 1, 2)
        layout.addWidget(download_group)
        # 「内核版本一览」等大部件单独成文件，避免本文件超过 1000 行红线
        self.pset_tor_widget = _ptw_build(self); layout.addWidget(self.pset_tor_widget)   # v1.9.46：Tor 桥接出口
        self.pset_version_widget = _pcvw_build(self); layout.addWidget(self.pset_version_widget)
        self.pset_matrix_widget = _pcmw_build(self); layout.addWidget(self.pset_matrix_widget)   # v1.9.35：协议能力矩阵

        advanced = QGroupBox("运行参数")
        adv_form = QFormLayout(advanced)
        self.pset_combo_loglevel = self.pset_bind_combo("core.log_level", QComboBox(), PMS_LOG_LEVEL_TITLES)
        self.pset_chk_access_log = self.pset_bind_bool("core.access_log", QCheckBox("记录访问日志（Xray access.log）"))
        self.pset_chk_auto_restart = self.pset_bind_bool("core.auto_restart", QCheckBox("核心异常退出后自动重启"))
        self.pset_spin_restart_max = self.pset_bind_int("core.restart_max_times", QSpinBox(), (0, 100))
        self.pset_chk_kill_leftover = self.pset_bind_bool("core.kill_existing_on_start", QCheckBox("启动前清理本程序上次残留的核心进程"))
        self.pset_chk_keep_config = self.pset_bind_bool("core.keep_config_file", QCheckBox("保留生成的配置文件（data\\configs）"))
        self.pset_chk_mux = self.pset_bind_bool("core.mux_enabled", QCheckBox("启用多路复用（mux）"))
        self.pset_spin_mux = self.pset_bind_int("core.mux_concurrency", QSpinBox(), (1, 128))
        self.pset_chk_tfo = self.pset_bind_bool("core.tcp_fast_open", QCheckBox("启用 TCP Fast Open"))
        self.pset_chk_sniff_override = self.pset_bind_bool("core.sniff_override_dest", QCheckBox("嗅探结果覆盖目标地址（sing-box 始终启用）"))
        adv_form.addRow("日志级别", self.pset_combo_loglevel)
        adv_form.addRow(self.pset_chk_access_log)
        adv_form.addRow(self.pset_chk_auto_restart)
        adv_form.addRow("最大重启次数", self.pset_spin_restart_max)
        adv_form.addRow(self.pset_chk_kill_leftover)
        adv_form.addRow(self.pset_chk_keep_config)
        adv_form.addRow(self.pset_chk_mux)
        adv_form.addRow("mux 并发", self.pset_spin_mux)
        adv_form.addRow(self.pset_chk_tfo)
        adv_form.addRow(self.pset_chk_sniff_override)
        layout.addWidget(advanced)
        layout.addStretch(1)
        return area

    def pset_browse_core(self, pms_key: str, pms_edit: QLineEdit) -> None:
        """选择核心可执行文件。"""
        target, _ = QFileDialog.getOpenFileName(self, "选择核心程序", str(self.pset_paths.prt_core_dir), "可执行文件 (*.exe);;所有文件 (*.*)")
        if not target:
            return
        pms_edit.setText(target)
        self.pset_settings.psm_set(pms_key, target)
        self.pset_refresh_core_status()

    def pset_save_core_paths(self) -> None:
        self.pset_settings.psm_set("core.xray_path", self.pset_xray_path_edit.text().strip())
        self.pset_settings.psm_set("core.singbox_path", self.pset_singbox_path_edit.text().strip())
        self.pset_refresh_core_status()

    def pset_refresh_core_status(self) -> None:
        """检测核心并显示版本。"""
        found = self.pset_locator.pcl_find_any(self.pset_settings)
        lines = []
        for kind, title in ((PMS_CORE_KIND_SINGBOX, "sing-box"), (PMS_CORE_KIND_XRAY, "Xray")):
            path = found.get(kind)
            if path is None:
                lines.append(f"{title}：未找到（可点“下载 / 更新”自动获取）")
            else:
                version = self.pset_locator.pcl_detect_version(path)
                lines.append(f"{title}：{path}（版本 {version or '未知'}）")
        self.pset_core_status_label.setText("\n".join(lines))

    # ------------------------------------------------------------------ 本地入口
    def pset_build_inbound_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        group = QGroupBox("监听与端口")
        form = QFormLayout(group)
        self.pset_listen_edit = self.pset_bind_text("inbound.listen_address", QLineEdit())
        self.pset_listen_edit.setPlaceholderText("127.0.0.1（仅本机）或 0.0.0.0（允许局域网）")
        self.pset_chk_mixed = self.pset_bind_bool("inbound.mixed_enabled", QCheckBox("启用混合入口（HTTP + SOCKS）"))
        self.pset_spin_mixed_port = self.pset_bind_int("inbound.mixed_port", QSpinBox(), (1, 65535))
        self.pset_chk_auto_port = self.pset_bind_bool("inbound.auto_pick_port", QCheckBox("端口被占用时自动换端口"))
        self.pset_chk_http = self.pset_bind_bool("inbound.http_enabled", QCheckBox("启用独立 HTTP 入口"))
        self.pset_spin_http_port = self.pset_bind_int("inbound.http_port", QSpinBox(), (1, 65535))
        self.pset_chk_socks = self.pset_bind_bool("inbound.socks_enabled", QCheckBox("启用独立 SOCKS 入口"))
        self.pset_spin_socks_port = self.pset_bind_int("inbound.socks_port", QSpinBox(), (1, 65535))
        self.pset_chk_udp = self.pset_bind_bool("inbound.udp_enabled", QCheckBox("允许 UDP 转发"))
        self.pset_chk_sniff = self.pset_bind_bool("inbound.sniff_enabled", QCheckBox("启用协议嗅探（推荐）"))
        self.pset_chk_lan = self.pset_bind_bool("inbound.allow_lan", QCheckBox("允许局域网设备连接（0.0.0.0）"))
        self.pset_chk_auth = self.pset_bind_bool("inbound.auth_enabled", QCheckBox("本地入口需要用户名密码"))
        self.pset_user_edit = self.pset_bind_text("inbound.username", QLineEdit())
        self.pset_pass_edit = self.pset_bind_text("inbound.password", QLineEdit())
        self.pset_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("监听地址", self.pset_listen_edit)
        form.addRow(self.pset_chk_mixed)
        form.addRow("混合端口", self.pset_spin_mixed_port)
        form.addRow(self.pset_chk_auto_port)
        form.addRow(self.pset_chk_http)
        form.addRow("HTTP 端口", self.pset_spin_http_port)
        form.addRow(self.pset_chk_socks)
        form.addRow("SOCKS 端口", self.pset_spin_socks_port)
        form.addRow(self.pset_chk_udp)
        form.addRow(self.pset_chk_sniff)
        form.addRow(self.pset_chk_lan)
        form.addRow(self.pset_chk_auth)
        form.addRow("用户名", self.pset_user_edit)
        form.addRow("密码", self.pset_pass_edit)
        check_button = QPushButton("检测端口占用情况")
        check_button.clicked.connect(self.pset_check_ports)
        form.addRow("", check_button)
        self.pset_port_status_label = QLabel("")
        self.pset_port_status_label.setWordWrap(True)
        form.addRow("检测结果", self.pset_port_status_label)
        layout.addWidget(group)
        layout.addWidget(pms_make_hint_label(
            "说明：Xray 一个端口只能承载一种协议，因此开启混合入口时，HTTP 入口会自动使用「混合端口 + 1」；"
            "sing-box 的 mixed 入口本身同时支持 HTTP 与 SOCKS，直接用混合端口。"
        ))
        layout.addStretch(1)
        return area

    def pset_check_ports(self) -> None:
        """检测各入口端口是否可用。"""
        candidates = [
            ("混合", self.pset_settings.psm_get_int("inbound.mixed_port", 30808)),
            ("HTTP", self.pset_settings.psm_get_int("inbound.http_port", 20809)),
            ("SOCKS", self.pset_settings.psm_get_int("inbound.socks_port", 20810)),
        ]
        lines = []
        for title, port in candidates:
            available = pms_is_port_available(port, self.pset_settings.psm_get_str("inbound.listen_address", "127.0.0.1"))
            lines.append(f"{title} 端口 {port}：{'可用' if available else '已被占用（建议启用自动换端口）'}")
        self.pset_port_status_label.setText("\n".join(lines))

    # ------------------------------------------------------------------ 系统代理
    def pset_build_system_proxy_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        group = QGroupBox("Windows 系统代理")
        form = QFormLayout(group)
        self.pset_chk_sysproxy = self.pset_bind_bool("system_proxy.enabled", QCheckBox("启用系统代理（写入 HKCU Internet Settings）"))
        self.pset_bypass_edit = self.pset_bind_text("system_proxy.bypass_list", QLineEdit())
        self.pset_bypass_edit.setPlaceholderText("localhost;127.*;10.*;172.16.*;192.168.*;<local>")
        self.pset_chk_restore = self.pset_bind_bool("system_proxy.restore_on_exit", QCheckBox("退出时还原原系统代理设置"))
        form.addRow(self.pset_chk_sysproxy)
        form.addRow("绕过列表", self.pset_bypass_edit)
        form.addRow(self.pset_chk_restore)
        info_row = QHBoxLayout()
        backup_button = QPushButton("备份当前系统代理")
        backup_button.clicked.connect(lambda: self.psed_action.emit("backup_system_proxy", {}))
        restore_button = QPushButton("还原备份")
        restore_button.clicked.connect(lambda: self.psed_action.emit("restore_system_proxy", {}))
        # v1.9.62：内置 PAC 编辑器入口（专业手写 / 小白表单，产物 pms_custom.pac 与上面的自动 PAC 独立）
        pac_editor_button = QPushButton("打开 PAC 编辑器…")
        pac_editor_button.setToolTip("专业模式手写脚本；小白模式用大白话填表生成。保存后可一键应用到系统代理。")
        pac_editor_button.clicked.connect(lambda: self.psed_action.emit("open_pac_editor", {}))
        info_row.addWidget(backup_button)
        info_row.addWidget(restore_button)
        info_row.addWidget(pac_editor_button)
        form.addRow("", info_row)
        layout.addWidget(group)
        self.pset_sysproxy_widget = PmsSystemProxyWidget(self.pset_settings, self.pset_paths, self); layout.addWidget(self.pset_sysproxy_widget)   # v1.9.49：PAC 模式
        layout.addWidget(pms_make_hint_label("提示：系统代理指向本地 HTTP 入口；核心停止时会自动关闭系统代理，避免断网。"))
        layout.addStretch(1)
        return area

    # ------------------------------------------------------------------ TUN
    def pset_build_tun_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        group = QGroupBox("TUN 模式（需要 sing-box 核心 + 管理员权限）")
        form = QFormLayout(group)
        self.pset_chk_tun = self.pset_bind_bool("tun.enabled", QCheckBox("启用 TUN 模式（全局虚拟网卡接管）"))
        self.pset_tun_iface_edit = self.pset_bind_text("tun.interface_name", QLineEdit())
        self.pset_combo_tun_stack = self.pset_bind_combo("tun.stack", QComboBox(), PMS_TUN_STACK_TITLES)
        self.pset_spin_tun_mtu = self.pset_bind_int("tun.mtu", QSpinBox(), (1280, 65535))
        self.pset_chk_tun_auto = self.pset_bind_bool("tun.auto_route", QCheckBox("自动配置路由（auto_route）"))
        self.pset_chk_tun_strict = self.pset_bind_bool("tun.strict_route", QCheckBox("严格路由（strict_route，可能影响局域网访问）"))
        self.pset_chk_tun_ipv6 = self.pset_bind_bool("tun.ipv6", QCheckBox("启用 IPv6 地址"))
        form.addRow(self.pset_chk_tun)
        form.addRow("网卡名称", self.pset_tun_iface_edit)
        form.addRow("协议栈", self.pset_combo_tun_stack)
        form.addRow("MTU", self.pset_spin_tun_mtu)
        form.addRow(self.pset_chk_tun_auto)
        form.addRow(self.pset_chk_tun_strict)
        form.addRow(self.pset_chk_tun_ipv6)
        self.pset_admin_label = QLabel("")
        form.addRow("管理员权限", self.pset_admin_label)
        restart_button = QPushButton("以管理员身份重新启动程序")
        restart_button.clicked.connect(lambda: self.psed_action.emit("restart_as_admin", {}))
        form.addRow("", restart_button)
        layout.addWidget(group)
        self.pset_refresh_admin_status()
        layout.addStretch(1)
        return area

    def pset_refresh_admin_status(self) -> None:
        is_admin = PmsPrivilegeUtils.ppu_is_admin()
        if is_admin:
            self.pset_admin_label.setText("已以管理员身份运行（可启用 TUN）")
            self.pset_admin_label.setObjectName("okLabel")
        else:
            self.pset_admin_label.setText("当前不是管理员，TUN 模式无法创建虚拟网卡（可点击下方按钮提权重启）")
            self.pset_admin_label.setObjectName("errorLabel")
        self.pset_admin_label.style().unpolish(self.pset_admin_label)
        self.pset_admin_label.style().polish(self.pset_admin_label)

    # ------------------------------------------------------------------ 路由分流
    def pset_ruleset_hint_text(self) -> str:
        """规则集提示：本地 .srs 是否存在，以及缺失时的回落行为。"""
        try:
            local_file = self.pset_paths.prt_ruleset_file(PMS_RULESET_LOCAL_GEOSITE_CN)
            exists = local_file.is_file()
        except OSError:
            return "提示：本地规则集目录不可用，规则分流将改用远程地址下载。"
        if exists:
            return f"提示：本地规则集已就绪（{local_file}），选「本地文件」时不产生任何联网请求。"
        return f"提示：未找到本地规则集（{local_file}），选「本地文件」时会自动回落到远程地址下载。"

    def pset_build_routing_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        group = QGroupBox("分流模式")
        form = QFormLayout(group)
        self.pset_combo_routing = self.pset_bind_combo("routing.mode", QComboBox(), PMS_ROUTING_MODE_TITLES)
        self.pset_chk_ruleset = self.pset_bind_bool("routing.use_remote_ruleset", QCheckBox("规则分流时使用 geosite-cn 规则集（sing-box）"))
        self.pset_combo_ruleset_source = self.pset_bind_combo("routing.ruleset_source", QComboBox(), PMS_RULESET_SOURCE_TITLES)
        self.pset_ruleset_edit = self.pset_bind_text("routing.ruleset_geosite_cn_url", QLineEdit())
        self.pset_chk_block_ads = self.pset_bind_bool("routing.block_ads_enabled", QCheckBox("拦截广告域名（返回空响应）"))
        self.pset_chk_bypass_private = self.pset_bind_bool("routing.bypass_private_ips", QCheckBox("内网/本机地址直连（关闭后连内网也走代理，即真正全局）"))
        form.addRow("分流模式", self.pset_combo_routing)
        form.addRow(self.pset_chk_ruleset)
        form.addRow("规则集来源", self.pset_combo_ruleset_source)
        form.addRow("规则集地址", self.pset_ruleset_edit)
        form.addRow(self.pset_chk_block_ads)
        form.addRow(self.pset_chk_bypass_private)
        layout.addWidget(group)
        layout.addWidget(pms_make_hint_label(self.pset_ruleset_hint_text()))
        dns_group = QGroupBox("DNS")
        dns_form = QFormLayout(dns_group)
        self.pset_dns_remote_edit = self.pset_bind_text("routing.dns_remote", QLineEdit())
        self.pset_dns_direct_edit = self.pset_bind_text("routing.dns_direct", QLineEdit())
        self.pset_combo_dns_strategy = self.pset_bind_combo("routing.domain_strategy", QComboBox(), {
            "IPIfNonMatch": "IPIfNonMatch（推荐）", "IPOnDemand": "IPOnDemand", "AsIs": "AsIs（不解析）",
        })
        dns_form.addRow("远程 DNS", self.pset_dns_remote_edit)
        dns_form.addRow("直连 DNS", self.pset_dns_direct_edit)
        dns_form.addRow("域名解析策略", self.pset_combo_dns_strategy)
        layout.addWidget(dns_group)
        lists_group = QGroupBox("域名列表（一行一条；直连=强制走直连，绕过=本机/内网直连）")
        lists_layout = QGridLayout(lists_group)
        self.pset_direct_text = self.pset_bind_list("routing.direct_domains", QPlainTextEdit())
        self.pset_bypass_text = self.pset_bind_list("routing.bypass_domains", QPlainTextEdit())
        self.pset_block_ads_text = self.pset_bind_list("routing.block_ad_domains", QPlainTextEdit())
        for widget in (self.pset_direct_text, self.pset_bypass_text, self.pset_block_ads_text):
            widget.setMaximumHeight(140)
        lists_layout.addWidget(QLabel("直连域名（大陆）"), 0, 0)
        lists_layout.addWidget(QLabel("绕过/内网域名"), 0, 1)
        lists_layout.addWidget(QLabel("广告域名"), 0, 2)
        lists_layout.addWidget(self.pset_direct_text, 1, 0)
        lists_layout.addWidget(self.pset_bypass_text, 1, 1)
        lists_layout.addWidget(self.pset_block_ads_text, 1, 2)
        preset_row = QHBoxLayout()
        preset_button = QPushButton("恢复默认列表")
        preset_button.clicked.connect(self.pset_restore_route_presets)
        preset_row.addWidget(preset_button)
        preset_row.addStretch(1)
        lists_layout.addLayout(preset_row, 2, 0, 1, 3)
        layout.addWidget(lists_group)
        layout.addStretch(1)
        return area

    def pset_restore_route_presets(self) -> None:
        """恢复内置的直连/绕过/广告域名列表。"""
        self.pset_settings.psm_set("routing.direct_domains", list(PMS_DIRECT_DOMAIN_PRESET))
        self.pset_settings.psm_set("routing.bypass_domains", list(PMS_BYPASS_DOMAIN_PRESET))
        self.pset_settings.psm_set("routing.block_ad_domains", list(PMS_BLOCK_AD_DOMAIN_PRESET))
        self.pset_direct_text.setPlainText("\n".join(PMS_DIRECT_DOMAIN_PRESET))
        self.pset_bypass_text.setPlainText("\n".join(PMS_BYPASS_DOMAIN_PRESET))
        self.pset_block_ads_text.setPlainText("\n".join(PMS_BLOCK_AD_DOMAIN_PRESET))

    # ------------------------------------------------------------------ 延迟测试
    def pset_build_test_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        group = QGroupBox("测试参数")
        form = QFormLayout(group)
        self.pset_combo_test_mode = self.pset_bind_combo("test.mode", QComboBox(), PMS_TEST_MODE_TITLES)
        self.pset_test_urls_text = self.pset_bind_list("test.url_list", QPlainTextEdit())
        self.pset_test_urls_text.setMaximumHeight(120)
        self.pset_spin_timeout = self.pset_bind_int("test.timeout_ms", QSpinBox(), (500, 60000))
        self.pset_spin_tcp_timeout = self.pset_bind_int("test.tcp_timeout_ms", QSpinBox(), (200, 60000))
        self.pset_spin_concurrency = self.pset_bind_int("test.concurrency", QSpinBox(), (1, 256))
        self.pset_spin_retry = self.pset_bind_int("test.retry_times", QSpinBox(), (0, 5))
        self.pset_chk_dedupe_test = self.pset_bind_bool("test.remove_duplicates_before_test", QCheckBox("测试前按地址去重"))
        self.pset_sort_widget_latency = PmsTestSortWidget(self.pset_settings, "latency", self); self.pset_notice_widget = PmsTestNoticeWidget(self.pset_settings, self); self.pset_health_widget = PmsHealthCheckWidget(self.pset_settings, self); self.pset_health_widget.phw_action.connect(self.psed_action)  # v1.9.21/22/37：同行挂件 + 健康检查按钮转交（本文件顶在 1000 行红线，加行即破线）
        self.pset_chk_auto_test = self.pset_bind_bool("test.auto_test_on_start", QCheckBox("启动核心后自动测试当前节点"))
        self.pset_spin_auto_interval = self.pset_bind_int("test.auto_test_interval_minutes", QSpinBox(), (0, 10080))
        self.pset_chk_only_enabled_test = self.pset_bind_bool("test.only_test_enabled", QCheckBox("只测试已启用的节点"))
        form.addRow("测试方式", self.pset_combo_test_mode)
        form.addRow("测试地址（每行一个）", self.pset_test_urls_text)
        form.addRow("URL 测试超时", self.pset_spin_timeout)
        form.addRow("TCP 测试超时", self.pset_spin_tcp_timeout)
        form.addRow("并发线程", self.pset_spin_concurrency)
        form.addRow("重试次数", self.pset_spin_retry)
        form.addRow(self.pset_chk_dedupe_test)
        form.addRow(self.pset_chk_auto_test)
        form.addRow("自动测试间隔（分钟，0=关闭）", self.pset_spin_auto_interval)
        form.addRow(self.pset_chk_only_enabled_test)
        layout.addWidget(group)
        layout.addWidget(self.pset_sort_widget_latency); layout.addWidget(self.pset_notice_widget); layout.addWidget(self.pset_health_widget)  # v1.9.21/22/37：同行挂件（守 1000 行红线）
        layout.addWidget(pms_make_hint_label("说明：sing-box 核心下批量测试走 Clash API（一次起核测完，速度快）；Xray 核心下每个节点临时起一个核心做真实请求（准确但较慢）。两者都是真实流量，不做任何模拟。「测试地址」按顺序当重试备选：某节点用第一个地址失败后，重试会自动换下一个地址（临时核心 / 单节点模式逐个试到成功为止）。建议第一条用 204 地址（如 http://cp.cloudflare.com/generate_204）——网页类地址（如 github.com）不含 204、握手又慢，会把延迟报得虚高、更容易撞上 URL 测试超时。失败节点的真实原因（核心报的 503 / 504 等）会写进测试历史与本次汇总。"))
        layout.addStretch(1)
        return area

    # ------------------------------------------------------------------ 测速
    def pset_build_speed_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        self.pset_speed_url_edit = self.pset_bind_text("speed.url", QLineEdit())
        group_source = QGroupBox("测速源与单次大小")
        form_source = QFormLayout(group_source)
        self.pset_speed_source_widget = PmsSpeedSourceWidget(self.pset_settings, self.pset_speed_url_edit)
        form_source.addRow(self.pset_speed_source_widget)
        layout.addWidget(group_source)
        group = QGroupBox("下载测速参数")
        form = QFormLayout(group)
        self.pset_spin_speed_duration = self.pset_bind_int("speed.duration_seconds", QSpinBox(), (3, 300))
        self.pset_spin_speed_samples = self.pset_bind_int("speed.min_samples", QSpinBox(), (1, 60))
        self.pset_spin_speed_concurrency = self.pset_bind_int("speed.concurrency", QSpinBox(), (1, 32))
        self.pset_spin_speed_limit = self.pset_bind_int("speed.per_node_limit_mb", QSpinBox(), (1, 2048))
        self.pset_sort_widget_speed = PmsTestSortWidget(self.pset_settings, "speed", self)
        form.addRow("自定义测速地址", self.pset_speed_url_edit)
        form.addRow("单节点时长（秒）", self.pset_spin_speed_duration)
        form.addRow("最少采样次数", self.pset_spin_speed_samples)
        form.addRow("并发节点数", self.pset_spin_speed_concurrency)
        form.addRow("单节点流量上限（MB）", self.pset_spin_speed_limit)
        layout.addWidget(group)
        layout.addWidget(self.pset_sort_widget_speed)
        layout.addWidget(pms_make_hint_label("提示：测速会真实消耗流量，请按需设置时长与上限；每个节点都会单独起临时核心。"))
        layout.addStretch(1)
        return area

    # ------------------------------------------------------------------ 订阅
    def pset_build_subscription_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        group = QGroupBox("订阅更新")
        form = QFormLayout(group)
        self.pset_chk_sub_launch = self.pset_bind_bool("subscription.update_on_launch", QCheckBox("启动程序时更新所有订阅"))
        self.pset_chk_sub_auto = self.pset_bind_bool("subscription.auto_update_enabled", QCheckBox("定时自动更新订阅"))
        self.pset_spin_sub_interval = self.pset_bind_int("subscription.auto_update_interval_minutes", QSpinBox(), (10, 20160))
        self.pset_sub_ua_edit = self.pset_bind_text("subscription.user_agent", QLineEdit())
        self.pset_chk_sub_env_key = self.pset_bind_bool("subscription.use_env_api_key", QCheckBox(f"未单独填写时使用 .env 中的 {PMS_SUB_API_KEY_ENV}"))
        self.pset_chk_sub_prune = self.pset_bind_bool("subscription.prune_removed", QCheckBox("移除订阅中已消失的节点"))
        self.pset_chk_sub_dedupe = self.pset_bind_bool("subscription.remove_duplicates", QCheckBox("订阅内部按地址去重"))
        self.pset_chk_sub_dedupe_cross = self.pset_bind_bool("subscription.dedup_across_subscriptions", QCheckBox("跨订阅去重（与其他订阅 / 手动添加的节点重复时不再重复入库）"))
        self.pset_chk_sub_keep = self.pset_bind_bool("subscription.keep_previous_on_failure", QCheckBox("更新失败时保留旧节点"))
        self.pset_spin_sub_timeout = self.pset_bind_int("subscription.timeout_seconds", QSpinBox(), (3, 300))
        self.pset_chk_sub_proxy = self.pset_bind_bool("subscription.use_system_proxy", QCheckBox("更新订阅时走本地代理"))
        form.addRow(self.pset_chk_sub_launch)
        form.addRow(self.pset_chk_sub_auto)
        form.addRow("自动更新间隔（分钟）", self.pset_spin_sub_interval)
        form.addRow("User-Agent", self.pset_sub_ua_edit)
        form.addRow(self.pset_chk_sub_env_key)
        form.addRow(self.pset_chk_sub_prune)
        form.addRow(self.pset_chk_sub_dedupe)
        form.addRow(self.pset_chk_sub_dedupe_cross)
        form.addRow(self.pset_chk_sub_keep)
        form.addRow("请求超时（秒）", self.pset_spin_sub_timeout)
        form.addRow(self.pset_chk_sub_proxy)
        self.pset_sub_last_label = QLabel(self.pset_settings.psm_get_str("subscription.last_update_time", "暂无"))
        form.addRow("上次更新", self.pset_sub_last_label)
        layout.addWidget(group)
        # v1.9.18：GitHub 仓库订阅（控件在 githubwidget.py，本文件已顶在 1000 行红线）
        self.pset_github_widget = PmsGithubSettingsWidget(self.pset_settings)
        layout.addWidget(self.pset_github_widget)
        group_telegram = QGroupBox("电报频道（t.me 频道当订阅源）")
        form_telegram = QFormLayout(group_telegram)
        self.pset_telegram_widget = PmsTelegramSettingsWidget(self.pset_settings, self.pset_local_proxy_port)
        form_telegram.addRow(self.pset_telegram_widget)
        form_telegram.addRow(pms_make_hint_label(
            "订阅地址可直接填 https://t.me/&lt;频道名&gt;，程序抓取 /s/ 预览页消息里的节点链接。"
            "只支持公开频道；私有频道与邀请链接需要登录，本程序不处理。"
        ))
        layout.addWidget(group_telegram)
        layout.addStretch(1)
        return area

    # ------------------------------------------------------------------ 界面
    def pset_build_ui_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        group = QGroupBox("外观与布局")
        form = QFormLayout(group)
        self.pset_combo_theme = self.pset_bind_combo("ui.theme", QComboBox(), PMS_THEME_TITLES)
        self.pset_spin_scale = QDoubleSpinBox()
        self.pset_spin_scale.setRange(0.75, 2.0)
        self.pset_spin_scale.setSingleStep(0.05)
        self.pset_spin_scale.setDecimals(2)
        self.pset_bind_float("ui.ui_scale", self.pset_spin_scale)
        self.pset_spin_row_height = self.pset_bind_int("ui.table_row_height", QSpinBox(), (18, 64))
        self.pset_spin_table_font = QDoubleSpinBox()
        self.pset_spin_table_font.setRange(7.0, 20.0)
        self.pset_spin_table_font.setSingleStep(0.5)
        self.pset_spin_table_font.setDecimals(1)
        self.pset_bind_float("ui.table_font_size", self.pset_spin_table_font)
        self.pset_spin_log_font = QDoubleSpinBox()
        self.pset_spin_log_font.setRange(7.0, 20.0)
        self.pset_spin_log_font.setSingleStep(0.5)
        self.pset_spin_log_font.setDecimals(1)
        self.pset_bind_float("ui.log_font_size", self.pset_spin_log_font)
        self.pset_spin_log_max = self.pset_bind_int("ui.log_max_lines", QSpinBox(), (100, PMS_MAX_LOG_LINES))
        self.pset_chk_log_time = self.pset_bind_bool("ui.log_show_time", QCheckBox("日志显示时间列"))
        self.pset_chk_log_scroll = self.pset_bind_bool("ui.log_auto_scroll", QCheckBox("日志自动滚动到底部"))
        self.pset_chk_remember = self.pset_bind_bool("ui.remember_geometry", QCheckBox("记住窗口位置与大小"))
        self.pset_chk_compact = self.pset_bind_bool("ui.compact_toolbar", QCheckBox("紧凑工具栏（图标+短文字）"))
        # v1.6.5：面板可见性四项（含新增的「快捷操作」）整体搬到 uiwidget.py，本文件不再往里塞
        self.pset_ui_layout = PmsUiLayoutWidget(self.pset_settings, self)
        form.addRow("主题", self.pset_combo_theme)
        form.addRow("界面缩放", self.pset_spin_scale)
        form.addRow("表格行高", self.pset_spin_row_height)
        form.addRow("表格字号", self.pset_spin_table_font)
        form.addRow("日志字号", self.pset_spin_log_font)
        form.addRow("日志最大行数", self.pset_spin_log_max)
        for widget in (self.pset_chk_log_time, self.pset_chk_log_scroll,
                       self.pset_chk_remember, self.pset_chk_compact):
            form.addRow(widget)
        layout.addWidget(group)
        layout.addWidget(self.pset_ui_layout)

        column_group = QGroupBox("节点表格显示列")
        column_layout = QGridLayout(column_group)
        self.pset_column_checks: list[QCheckBox] = []
        visible = self.pset_settings.psm_get_list("ui.table_column_visible")
        if len(visible) != len(PMS_COLUMN_TITLES):
            visible = ["true"] * len(PMS_COLUMN_TITLES)
        for index, title in enumerate(PMS_COLUMN_TITLES):
            check = QCheckBox(title)
            check.setChecked(str(visible[index]).lower() in ("true", "1", "yes"))
            check.toggled.connect(self.pset_save_column_visibility)
            self.pset_column_checks.append(check)
            column_layout.addWidget(check, index // 6, index % 6)
        layout.addWidget(column_group)
        layout.addStretch(1)
        return area

    def pset_save_column_visibility(self) -> None:
        """保存列显示设置（立即生效）。"""
        self.pset_settings.psm_set("ui.table_column_visible", [check.isChecked() for check in self.pset_column_checks])

















    def pset_refresh_core_download_progress(self, pms_percent: int, pms_downloaded: int, pms_total: int, pms_note: str) -> None:
        """下载核心的进度回填。"""
        self.pset_core_progress.setValue(int(pms_percent))
        if pms_total:
            self.pset_core_progress_label.setText(f"{pms_note}（{pms_format_bytes(pms_downloaded)} / {pms_format_bytes(pms_total)}）")
        else:
            self.pset_core_progress_label.setText(pms_note)


