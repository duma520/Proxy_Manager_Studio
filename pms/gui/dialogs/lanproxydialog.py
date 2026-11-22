#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：手机 / 平板代理（局域网共享）

把本机代理入口共享给同一 Wi-Fi 下的手机 / 平板：

    · 可视化编辑「监听地址 / 混合端口」（写进 inbound.listen_address / inbound.mixed_port）
    · 一键开关「允许局域网设备连接」（inbound.allow_lan → 核心监听 0.0.0.0）
    · 显示本机局域网 IP 与「手机 / 平板应填写的代理地址」，支持复制与二维码

所有改动实时写入设置仓库（防抖 + 原子写盘），重启后沿用。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pms.icons import pms_qr_pixmap
from pms.settings import PmsSettingsManager
from pms.utils import pms_local_lan_ip, pms_lan_proxy_address
from pms.gui.widgets import pms_make_hint_label


class PmsLanProxyDialog(QDialog):
    """手机 / 平板代理设置对话框：编辑入口 + 显示手机可填地址 + 二维码。"""

    def __init__(self, pms_settings: PmsSettingsManager, pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.plpd_settings = pms_settings
        self.setWindowTitle("手机 / 平板代理（局域网共享）")
        self.setMinimumSize(560, 520)
        self.plpd_build_ui()
        self.plpd_refresh_address()

    # ------------------------------------------------------------------ 绑定辅助（实时保存）
    def plpd_bind_text(self, pms_key: str, pms_widget: QLineEdit) -> QLineEdit:
        """单行输入 ↔ 字符串设置（双向，改动即存）。"""
        pms_widget.setText(self.plpd_settings.psm_get_str(pms_key, ""))
        pms_widget.textChanged.connect(lambda value, key=pms_key: self.plpd_settings.psm_set(key, str(value)))
        return pms_widget

    def plpd_bind_int(self, pms_key: str, pms_widget: QSpinBox, pms_range: Optional[tuple[int, int]] = None) -> QSpinBox:
        """数字框 ↔ 整数设置（双向，改动即存）。

        pms_range 必须在取值之前设置：QSpinBox 构造默认范围 0-99，
        先 setValue 后 setRange 会把端口 20808 截断显示成 99。
        关闭 keyboardTracking，编辑完成才写设置，避免逐键保存。
        """
        if pms_range is not None:
            pms_widget.setRange(pms_range[0], pms_range[1])
        pms_widget.setKeyboardTracking(False)
        pms_widget.setValue(self.plpd_settings.psm_get_int(pms_key, pms_widget.value()))
        pms_widget.valueChanged.connect(lambda value, key=pms_key: self.plpd_settings.psm_set(key, int(value)))
        return pms_widget

    def plpd_bind_bool(self, pms_key: str, pms_widget: QCheckBox) -> QCheckBox:
        """复选框 ↔ 布尔设置（双向，改动即存）。"""
        pms_widget.setChecked(self.plpd_settings.psm_get_bool(pms_key, False))
        pms_widget.toggled.connect(lambda value, key=pms_key: self.plpd_settings.psm_set(key, bool(value)))
        return pms_widget

    # ------------------------------------------------------------------ 界面
    def plpd_build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ---- 监听与端口 ----
        listen_group = QGroupBox("本地入口（写进核心配置）")
        listen_form = QFormLayout(listen_group)
        self.plpd_listen_edit = self.plpd_bind_text("inbound.listen_address", QLineEdit())
        self.plpd_listen_edit.setPlaceholderText("127.0.0.1（仅本机）或 0.0.0.0（允许局域网）")
        self.plpd_spin_port = self.plpd_bind_int("inbound.mixed_port", QSpinBox(), (1, 65535))
        self.plpd_chk_lan = self.plpd_bind_bool("inbound.allow_lan", QCheckBox("允许局域网设备连接（手机 / 平板）"))
        listen_form.addRow("监听地址", self.plpd_listen_edit)
        listen_form.addRow("混合端口", self.plpd_spin_port)
        listen_form.addRow(self.plpd_chk_lan)
        listen_form.addRow("", pms_make_hint_label(
            "提示：勾选「允许局域网设备连接」后，核心会实际监听 0.0.0.0（上面的监听地址会被覆盖）；"
            "手机 / 平板需与本机处于同一 Wi-Fi，并在首次连接时允许 Windows 防火墙放行该端口。"
        ))
        # 任一入口参数变化 → 立即刷新手机可填地址
        self.plpd_listen_edit.textChanged.connect(lambda _value: self.plpd_refresh_address())
        self.plpd_spin_port.valueChanged.connect(lambda _value: self.plpd_refresh_address())
        self.plpd_chk_lan.toggled.connect(lambda _value: self.plpd_refresh_address())
        root.addWidget(listen_group)

        # ---- 手机 / 平板填写 ----
        share_group = QGroupBox("手机 / 平板填写")
        share_layout = QVBoxLayout(share_group)
        self.plpd_ip_label = QLabel("-")
        self.plpd_ip_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.plpd_addr_label = QLabel("-")
        self.plpd_addr_label.setObjectName("accentLabel")
        self.plpd_addr_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        share_layout.addWidget(QLabel("本机局域网 IP："))
        share_layout.addWidget(self.plpd_ip_label)
        share_layout.addWidget(QLabel("代理地址（HTTP / SOCKS 混合入口）："))
        self.plpd_addr_edit = QLineEdit()
        self.plpd_addr_edit.setReadOnly(True)
        share_layout.addWidget(self.plpd_addr_edit)
        self.plpd_warn_label = QLabel("")
        self.plpd_warn_label.setWordWrap(True)
        share_layout.addWidget(self.plpd_warn_label)
        action_row = QHBoxLayout()
        copy_button = QPushButton("复制代理地址")
        copy_button.clicked.connect(self.plpd_copy_address)
        qr_button = QPushButton("生成 / 刷新二维码")
        qr_button.clicked.connect(self.plpd_show_qr)
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.plpd_refresh_address)
        for widget in (copy_button, qr_button, refresh_button):
            action_row.addWidget(widget)
        action_row.addStretch(1)
        share_layout.addLayout(action_row)
        self.plpd_qr_label = QLabel("")
        self.plpd_qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.plpd_qr_label.setMinimumHeight(0)
        share_layout.addWidget(self.plpd_qr_label)
        root.addWidget(share_group, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        bottom.addWidget(close_button)
        root.addLayout(bottom)

    # ------------------------------------------------------------------ 逻辑
    def plpd_effective_host(self) -> str:
        """返回手机 / 平板实际应填写的宿主（从统一入口计算）。"""
        return pms_lan_proxy_address(self.plpd_settings).rsplit(":", 1)[0]

    def plpd_effective_address(self) -> str:
        """返回手机 / 平板应填写的「宿主:端口」（统一入口）。"""
        return pms_lan_proxy_address(self.plpd_settings)

    def plpd_refresh_address(self) -> None:
        """刷新 IP / 地址显示与提示。"""
        lan_ip = pms_local_lan_ip()
        address = self.plpd_effective_address()
        self.plpd_ip_label.setText(lan_ip)
        self.plpd_addr_edit.setText(address)
        allow_lan = self.plpd_settings.psm_get_bool("inbound.allow_lan", False)
        host = self.plpd_effective_host()
        if not allow_lan and host in ("127.0.0.1", "::1", "localhost"):
            self.plpd_warn_label.setText("⚠ 当前仅本机可连：请勾选上方「允许局域网设备连接（手机 / 平板）」。")
        elif host == "127.0.0.1":
            self.plpd_warn_label.setText("⚠ 未检测到局域网 IP，手机可能无法连接；请确认已连接 Wi-Fi / 网线。")
        else:
            self.plpd_warn_label.setText("✓ 手机 / 平板与本机连同一 Wi-Fi 后，按上方地址配置即可。")

    def plpd_copy_address(self) -> None:
        """复制代理地址到剪贴板。"""
        address = self.plpd_effective_address()
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(address)
        QMessageBox.information(self, "已复制", f"代理地址已复制：{address}")

    def plpd_show_qr(self) -> None:
        """生成 / 刷新代理地址二维码。"""
        address = self.plpd_effective_address()
        pixmap = pms_qr_pixmap(address, 240)
        if pixmap is None:
            self.plpd_qr_label.setText("未安装 qrcode 库，无法生成二维码（可复制地址手动填写）")
            return
        self.plpd_qr_label.setPixmap(pixmap)
        self.plpd_qr_label.setMinimumHeight(pixmap.height())
