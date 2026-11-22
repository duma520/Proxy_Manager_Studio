#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""托盘控制

托盘图标、右键菜单与气泡通知。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QObject,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QIcon,
)
from PySide6.QtWidgets import (
    QMenu,
    QSystemTrayIcon,
)

from pms.constants import PMS_APP_NAME

# --------------------------------------------------------------------------------------
# 托盘控制器
# --------------------------------------------------------------------------------------
class PmsTrayController(QObject):
    """托盘图标与右键菜单。"""

    ptrc_show_window = Signal()
    ptrc_toggle_core = Signal()
    ptrc_toggle_system_proxy = Signal(bool)
    ptrc_toggle_tun = Signal(bool)
    ptrc_test_latency = Signal()
    ptrc_health_run = Signal()             # v1.9.37：立即跑一次节点健康检查
    ptrc_health_auto = Signal()            # v1.9.37：只检查被自动禁用的节点
    ptrc_health_pause = Signal(bool)       # v1.9.37：暂停 / 继续健康检查
    ptrc_quit = Signal()

    def __init__(self, pms_icon: QIcon, pms_parent: QObject) -> None:
        super().__init__(pms_parent)
        self.ptrc_icon = pms_icon
        self.ptrc_tray = QSystemTrayIcon(self.ptrc_icon, self)
        self.ptrc_tray.setToolTip(PMS_APP_NAME)
        menu = QMenu()
        self.ptrc_action_show = QAction("显示主窗口", menu)
        self.ptrc_action_show.triggered.connect(self.ptrc_show_window.emit)
        self.ptrc_action_core = QAction("启动 / 停止核心", menu)
        self.ptrc_action_core.triggered.connect(self.ptrc_toggle_core.emit)
        self.ptrc_action_proxy = QAction("系统代理", menu)
        self.ptrc_action_proxy.setCheckable(True)
        self.ptrc_action_proxy.toggled.connect(self.ptrc_toggle_system_proxy.emit)
        self.ptrc_action_tun = QAction("TUN 模式", menu)
        self.ptrc_action_tun.setCheckable(True)
        self.ptrc_action_tun.toggled.connect(self.ptrc_toggle_tun.emit)
        self.ptrc_action_test = QAction("测试延迟", menu)
        self.ptrc_action_test.triggered.connect(self.ptrc_test_latency.emit)
        # v1.9.37：节点健康检查（与手动测试分开的三个入口）
        self.ptrc_action_health_run = QAction("立即运行健康检查", menu)
        self.ptrc_action_health_run.triggered.connect(self.ptrc_health_run.emit)
        self.ptrc_action_health_auto = QAction("只检查自动禁用的节点", menu)
        self.ptrc_action_health_auto.triggered.connect(self.ptrc_health_auto.emit)
        self.ptrc_action_health_pause = QAction("暂停健康检查", menu)
        self.ptrc_action_health_pause.setCheckable(True)
        self.ptrc_action_health_pause.toggled.connect(self.ptrc_health_pause.emit)
        self.ptrc_action_quit = QAction("退出", menu)
        self.ptrc_action_quit.triggered.connect(self.ptrc_quit.emit)
        for action in (self.ptrc_action_show, self.ptrc_action_core):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.ptrc_action_proxy)
        menu.addAction(self.ptrc_action_tun)
        menu.addAction(self.ptrc_action_test)
        menu.addSeparator()
        menu.addAction(self.ptrc_action_health_run)
        menu.addAction(self.ptrc_action_health_auto)
        menu.addAction(self.ptrc_action_health_pause)
        menu.addSeparator()
        menu.addAction(self.ptrc_action_quit)
        self.ptrc_menu = menu
        self.ptrc_tray.setContextMenu(menu)
        self.ptrc_tray.activated.connect(self.ptrc_on_activated)

    def ptrc_show(self) -> None:
        """显示托盘图标。"""
        self.ptrc_tray.show()

    def ptrc_hide(self) -> None:
        self.ptrc_tray.hide()

    def ptrc_available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def ptrc_on_activated(self, pms_reason: QSystemTrayIcon.ActivationReason) -> None:
        if pms_reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.ptrc_show_window.emit()

    def ptrc_notify(self, pms_title: str, pms_message: str) -> None:
        """气泡通知（托盘可用时）。"""
        if self.ptrc_available():
            self.ptrc_tray.showMessage(pms_title, pms_message, self.ptrc_icon, 4000)

    def ptrc_set_tooltip(self, pms_text: str) -> None:
        """托盘悬停提示（v1.9.37：健康检查的进度也显示在这里）。"""
        self.ptrc_tray.setToolTip(str(pms_text or PMS_APP_NAME))

    def ptrc_set_health_state(self, pms_paused: bool) -> None:
        """同步「暂停健康检查」勾选状态与文案（不进信号，避免回环）。"""
        self.ptrc_action_health_pause.blockSignals(True)
        self.ptrc_action_health_pause.setChecked(bool(pms_paused))
        self.ptrc_action_health_pause.setText("继续健康检查" if pms_paused else "暂停健康检查")
        self.ptrc_action_health_pause.blockSignals(False)

    def ptrc_set_core_state(self, pms_running: bool) -> None:
        self.ptrc_action_core.setText("停止核心" if pms_running else "启动核心")

    def ptrc_set_proxy_state(self, pms_enabled: bool) -> None:
        self.ptrc_action_proxy.blockSignals(True)
        self.ptrc_action_proxy.setChecked(bool(pms_enabled))
        self.ptrc_action_proxy.blockSignals(False)

    def ptrc_set_tun_state(self, pms_enabled: bool) -> None:
        self.ptrc_action_tun.blockSignals(True)
        self.ptrc_action_tun.setChecked(bool(pms_enabled))
        self.ptrc_action_tun.blockSignals(False)
