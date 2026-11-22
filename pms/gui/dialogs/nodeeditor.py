#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：节点编辑

新增/编辑节点：字段随协议动态显隐、实时校验、分享链接双向转换。
"""

from __future__ import annotations

from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
import uuid

import json

from pms.constants import (
    PMS_PROTOCOL_HTTP,
    PMS_PROTOCOL_HYSTERIA2,
    PMS_PROTOCOL_ORDER,
    PMS_PROTOCOL_SHADOWSOCKS,
    PMS_PROTOCOL_SOCKS,
    PMS_PROTOCOL_TITLES,
    PMS_PROTOCOL_TROJAN,
    PMS_PROTOCOL_TUIC,
    PMS_PROTOCOL_VLESS,
    PMS_PROTOCOL_VMESS,
    PMS_PROTOCOL_SSR,
    PMS_PROTOCOL_NAIVE,
    PMS_PROTOCOL_ANYTLS,
    PMS_PROTOCOL_SHADOWTLS,
    PMS_PROTOCOL_SNELL,
    PMS_PROTOCOL_WIREGUARD,
    PMS_PROTOCOL_JUICITY,
    PMS_PROTOCOL_HYSTERIA1,
    PMS_PROTOCOL_SOCKS4,
    PMS_PROTOCOL_TG_WEBPROXY,
    PMS_PROTOCOL_TG_PROXY,
    PMS_SECURITY_NONE,
    PMS_SECURITY_REALITY,
    PMS_SECURITY_TITLES,
    PMS_SECURITY_TLS,
    PMS_SS_METHODS,
    PMS_TLS_FINGERPRINTS,
    PMS_TRANSPORT_GRPC,
    PMS_TRANSPORT_H2,
    PMS_TRANSPORT_HTTP,
    PMS_TRANSPORT_HTTPUPGRADE,
    PMS_TRANSPORT_TCP,
    PMS_TRANSPORT_TITLES,
    PMS_TRANSPORT_WS,
    PMS_TRANSPORT_XHTTP,
    PMS_TUIC_CONGESTION,
    PMS_TUIC_UDP_MODES,
    PMS_VMESS_SECURITY,
)
from pms.utils import pms_now_text, pms_normalize_cert_sha256
from pms.models import PmsProxyNode
from pms.sharelinks import (
    PmsShareLinkParser,
    PmsShareLinkWriter,
)
from pms.importers import PmsTextImportClassifier
from pms.gui.widgets import (
    pms_make_hint_label,
    pms_make_row,
)
from pms.gui.dialogs.shareqr import PmsShareQrDialog

class PmsNodeEditorDialog(QDialog):
    """新增 / 编辑单个节点：字段随协议动态显隐，支持链接双向转换。"""

    def __init__(self, pms_node: Optional[PmsProxyNode], pms_settings: "PmsSettingsManager", pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.pned_settings = pms_settings
        self.pned_parser = PmsShareLinkParser()
        self.pned_writer = PmsShareLinkWriter()
        self.pned_node = pms_node.pnode_clone(pms_new_uid=False) if pms_node is not None else PmsProxyNode()
        self.pned_is_new = pms_node is None
        self.pned_result_node: Optional[PmsProxyNode] = None
        self._pned_loading = False
        self.setWindowTitle("编辑节点" if not self.pned_is_new else "新增节点")
        self.setMinimumSize(720, 560)
        self.pned_build_ui()
        self.pned_load_node(self.pned_node)
        self.pned_update_visibility()
        self.pned_update_validation()

    # ------------------------------------------------------------------ 界面
    def pned_build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        self.pned_link_edit = QLineEdit()
        self.pned_link_edit.setPlaceholderText("粘贴分享链接（vmess:// vless:// trojan:// ss:// hysteria2:// tuic:// socks:// http://）后点「解析填充」")
        self.pned_btn_parse = QPushButton("解析填充")
        self.pned_btn_parse.clicked.connect(self.pned_parse_from_link)
        self.pned_btn_clipboard = QPushButton("从剪贴板")
        self.pned_btn_clipboard.clicked.connect(self.pned_parse_from_clipboard)
        self.pned_btn_link = QPushButton("复制分享链接")
        self.pned_btn_link.clicked.connect(self.pned_copy_share_link)
        self.pned_btn_qr = QPushButton("二维码")
        self.pned_btn_qr.clicked.connect(self.pned_show_qr)
        top_row.addWidget(self.pned_link_edit, 1)
        for button in (self.pned_btn_parse, self.pned_btn_clipboard, self.pned_btn_link, self.pned_btn_qr):
            top_row.addWidget(button)
        root.addLayout(top_row)

        self.pned_tabs = QTabWidget()
        root.addWidget(self.pned_tabs, 1)
        self.pned_tabs.addTab(self.pned_build_basic_tab(), "基本")
        self.pned_tabs.addTab(self.pned_build_credential_tab(), "凭据")
        self.pned_tabs.addTab(self.pned_build_transport_tab(), "传输与安全")
        self.pned_tabs.addTab(self.pned_build_extra_tab(), "高级")

        self.pned_validation_label = QLabel("")
        self.pned_validation_label.setWordWrap(True)
        root.addWidget(self.pned_validation_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.pned_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def pned_build_basic_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setContentsMargins(8, 8, 8, 8)
        self.pned_name_edit = QLineEdit()
        self.pned_group_edit = QComboBox()
        self.pned_group_edit.setEditable(True)
        self.pned_group_edit.addItems(self.pned_known_groups())
        self.pned_protocol_combo = QComboBox()
        for protocol in PMS_PROTOCOL_ORDER:
            self.pned_protocol_combo.addItem(PMS_PROTOCOL_TITLES[protocol], protocol)
        self.pned_protocol_combo.currentIndexChanged.connect(self.pned_update_visibility)
        self.pned_server_edit = QLineEdit()
        self.pned_server_edit.setPlaceholderText("域名或 IP（IPv6 直接填地址，不要加方括号）")
        self.pned_port_spin = QSpinBox()
        self.pned_port_spin.setRange(1, 65535)
        self.pned_port_spin.setValue(443)
        self.pned_enabled_check = QCheckBox("启用该节点")
        self.pned_remark_edit = QLineEdit()
        self.pned_tags_edit = QLineEdit()
        self.pned_tags_edit.setPlaceholderText("可选，逗号分隔，用于筛选")
        self.pned_subscription_label = QLabel("-")
        form.addRow("名称", self.pned_name_edit)
        form.addRow("分组", self.pned_group_edit)
        form.addRow("协议", self.pned_protocol_combo)
        form.addRow("服务器地址", self.pned_server_edit)
        form.addRow("端口", self.pned_port_spin)
        form.addRow("标签", self.pned_tags_edit)
        form.addRow("备注", self.pned_remark_edit)
        form.addRow("所属订阅", self.pned_subscription_label)
        form.addRow("", self.pned_enabled_check)
        form.addRow("", pms_make_hint_label("提示：分享链接解析会自动识别协议、传输与安全参数；保存后立即写入数据库。"))
        return page

    def pned_build_credential_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setContentsMargins(8, 8, 8, 8)
        self.pned_uuid_edit = QLineEdit()
        self.pned_uuid_btn = QPushButton("随机生成")
        self.pned_uuid_btn.clicked.connect(lambda: self.pned_uuid_edit.setText(str(uuid.uuid4())))
        self.pned_password_edit = QLineEdit()
        self.pned_password_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        self.pned_method_combo = QComboBox()
        self.pned_method_combo.setEditable(True)
        self.pned_method_combo.addItems(list(PMS_SS_METHODS))
        self.pned_flow_combo = QComboBox()
        self.pned_flow_combo.setEditable(True)
        self.pned_flow_combo.addItems(["", "xtls-rprx-vision", "xtls-rprx-vision-udp443"])
        self.pned_alterid_spin = QSpinBox()
        self.pned_alterid_spin.setRange(0, 65535)
        self.pned_vmess_security_combo = QComboBox()
        self.pned_vmess_security_combo.addItems(list(PMS_VMESS_SECURITY))
        self.pned_socks_user_edit = QLineEdit()
        self.pned_socks_user_edit.setPlaceholderText("SOCKS / HTTP 代理用户名（可选）")
        self.pned_obfs_edit = QLineEdit()
        self.pned_obfs_edit.setPlaceholderText("Hysteria2 混淆类型，如 salamander")
        self.pned_obfs_password_edit = QLineEdit()
        self.pned_obfs_password_edit.setPlaceholderText("Hysteria2 混淆密码")
        self.pned_congestion_combo = QComboBox()
        self.pned_congestion_combo.addItems(list(PMS_TUIC_CONGESTION))
        self.pned_udp_mode_combo = QComboBox()
        self.pned_udp_mode_combo.addItems(list(PMS_TUIC_UDP_MODES))
        form.addRow("UUID", pms_make_row(self.pned_uuid_edit, self.pned_uuid_btn))
        form.addRow("密码", self.pned_password_edit)
        self.pned_row_method = form.rowCount()
        form.addRow("加密方式（SS）", self.pned_method_combo)
        self.pned_row_flow = form.rowCount()
        form.addRow("流控（VLESS）", self.pned_flow_combo)
        self.pned_row_alterid = form.rowCount()
        form.addRow("alterId（VMess）", self.pned_alterid_spin)
        self.pned_row_vmess_security = form.rowCount()
        form.addRow("VMess 加密", self.pned_vmess_security_combo)
        self.pned_row_socks_user = form.rowCount()
        form.addRow("代理用户名", self.pned_socks_user_edit)
        self.pned_row_obfs = form.rowCount()
        form.addRow("Hysteria2 混淆", self.pned_obfs_edit)
        self.pned_row_obfs_password = form.rowCount()
        form.addRow("混淆密码", self.pned_obfs_password_edit)
        self.pned_row_congestion = form.rowCount()
        form.addRow("TUIC 拥塞控制", self.pned_congestion_combo)
        self.pned_row_udp_mode = form.rowCount()
        form.addRow("TUIC UDP 模式", self.pned_udp_mode_combo)
        self.pned_credential_form = form
        return page

    def pned_build_transport_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setContentsMargins(8, 8, 8, 8)
        self.pned_transport_combo = QComboBox()
        for key, title in PMS_TRANSPORT_TITLES.items():
            self.pned_transport_combo.addItem(title, key)
        self.pned_transport_combo.currentIndexChanged.connect(self.pned_update_visibility)
        self.pned_security_combo = QComboBox()
        for key, title in PMS_SECURITY_TITLES.items():
            self.pned_security_combo.addItem(title, key)
        self.pned_security_combo.currentIndexChanged.connect(self.pned_update_visibility)
        self.pned_host_edit = QLineEdit()
        self.pned_host_edit.setPlaceholderText("WebSocket Host / HTTP Host（伪装域名）")
        self.pned_path_edit = QLineEdit()
        self.pned_path_edit.setPlaceholderText("如 /ws 或 /path?ed=2048")
        self.pned_service_edit = QLineEdit()
        self.pned_service_edit.setPlaceholderText("gRPC serviceName")
        self.pned_grpc_mode_combo = QComboBox()
        self.pned_grpc_mode_combo.addItems(["", "gun", "multi"])
        self.pned_header_type_combo = QComboBox()
        self.pned_header_type_combo.addItems(["", "none", "http"])
        self.pned_sni_edit = QLineEdit()
        self.pned_alpn_edit = QLineEdit()
        self.pned_alpn_edit.setPlaceholderText("如 h2,http/1.1")
        self.pned_fingerprint_combo = QComboBox()
        self.pned_fingerprint_combo.setEditable(True)
        self.pned_fingerprint_combo.addItems([""] + list(PMS_TLS_FINGERPRINTS))
        self.pned_insecure_check = QCheckBox("允许不安全证书（allowInsecure；Xray 26 已不支持，建议用 sing-box）")
        self.pned_public_key_edit = QLineEdit()
        self.pned_short_id_edit = QLineEdit()
        self.pned_spider_x_edit = QLineEdit()
        self.pned_spider_x_edit.setPlaceholderText("默认 /")
        self.pned_cert_edit = QLineEdit()
        self.pned_cert_edit.setPlaceholderText("可选：64 位十六进制 SHA-256（Xray pinnedPeerCertSha256）")
        form.addRow("传输方式", self.pned_transport_combo)
        form.addRow("安全类型", self.pned_security_combo)
        form.addRow("伪装 Host", self.pned_host_edit)
        self.pned_row_path = form.rowCount()
        form.addRow("路径 Path", self.pned_path_edit)
        self.pned_row_service = form.rowCount()
        form.addRow("gRPC 服务名", self.pned_service_edit)
        self.pned_row_grpc_mode = form.rowCount()
        form.addRow("gRPC 模式", self.pned_grpc_mode_combo)
        self.pned_row_header_type = form.rowCount()
        form.addRow("TCP 伪装头", self.pned_header_type_combo)
        form.addRow("SNI / 服务器名", self.pned_sni_edit)
        form.addRow("ALPN", self.pned_alpn_edit)
        form.addRow("TLS 指纹", self.pned_fingerprint_combo)
        form.addRow("", self.pned_insecure_check)
        self.pned_row_public_key = form.rowCount()
        form.addRow("Reality 公钥", self.pned_public_key_edit)
        self.pned_row_short_id = form.rowCount()
        form.addRow("Reality ShortId", self.pned_short_id_edit)
        self.pned_row_spider_x = form.rowCount()
        form.addRow("Reality SpiderX", self.pned_spider_x_edit)
        self.pned_row_cert = form.rowCount()
        form.addRow("证书指纹", self.pned_cert_edit)
        form.addRow("", pms_make_hint_label("传输与安全字段会随协议自动显隐：TCP 无需 path；gRPC 只认 serviceName；Reality 仅 VLESS 支持。"))
        self.pned_transport_form = form
        return page

    def pned_build_extra_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        group = QGroupBox("运行与测试状态")
        form = QFormLayout(group)
        self.pned_latency_label = QLabel("-")
        self.pned_speed_label = QLabel("-")
        self.pned_last_test_label = QLabel("-")
        self.pned_success_label = QLabel("-")
        form.addRow("当前延迟", self.pned_latency_label)
        form.addRow("最近测速", self.pned_speed_label)
        form.addRow("最近测试时间", self.pned_last_test_label)
        form.addRow("成功 / 失败", self.pned_success_label)
        layout.addWidget(group)
        # 协议专属扩展参数（extra_json）兜底编辑框：WireGuard 密钥、SSR obfs、Hysteria1 auth_str 等
        group_extra = QGroupBox("协议专属扩展参数（extra_json）")
        group_extra.setCheckable(True)
        group_extra.setChecked(True)
        extra_layout = QVBoxLayout(group_extra)
        self.pned_extra_json_edit = QPlainTextEdit()
        self.pned_extra_json_edit.setPlaceholderText(
            "协议专属参数（JSON 对象），留空则不使用；保存时校验为合法 JSON 对象。\n"
            "WireGuard: {\"peer_public_key\":\"...\",\"private_key\":\"...\",\"local_address\":\"***已脱敏***-内网IP/32\",\"mtu\":1420}\n"
            "SSR: {\"ssr_protocol\":\"origin\",\"obfs\":\"plain\",\"obfs_param\":\"\",\"proto_param\":\"\"}\n"
            "Hysteria1: {\"auth_str\":\"...\",\"up_mbps\":50,\"down_mbps\":100}    ShadowTLS/Snell: {\"version\":3}"
        )
        self.pned_extra_json_edit.setMinimumHeight(120)
        extra_layout.addWidget(self.pned_extra_json_edit)
        extra_layout.addWidget(pms_make_hint_label(
            "说明：此框为协议专属参数的兜底编辑入口（WireGuard 密钥组、SSR obfs/协议、"
            "Hysteria1 auth_str、ShadowTLS/Snell 版本号等）。从分享链接导入的节点会自动填充此框；"
            "手动新建的扩展协议节点可在此补齐专属参数，否则核心将按默认参数尽力连接。"
        ))
        layout.addWidget(group_extra)
        layout.addWidget(pms_make_hint_label(
            "说明：延迟与测速结果由「延迟测试 / 测速」功能写入；节点级参数全部保存在数据库，"
            "并随设置实时落盘，程序重启后自动恢复。"
        ))
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ 数据读写
    def pned_known_groups(self) -> list[str]:
        groups = ["默认分组", "导入", "订阅"]
        return groups

    def pned_load_node(self, pms_node: PmsProxyNode) -> None:
        """把节点数据填入界面控件（加载期间屏蔽 collect 反向回写，避免清掉既有值）。"""
        self._pned_loading = True
        try:
            self._pned_do_load(pms_node)
        finally:
            self._pned_loading = False

    def _pned_do_load(self, pms_node: PmsProxyNode) -> None:
        """实际填充控件（由 pned_load_node 在加载守卫内调用）。"""
        node = pms_node
        self.pned_name_edit.setText(node.name)
        self.pned_group_edit.setCurrentText(node.group_name or "默认分组")
        index = self.pned_protocol_combo.findData(node.protocol)
        self.pned_protocol_combo.setCurrentIndex(max(0, index))
        self.pned_server_edit.setText(node.server)
        self.pned_port_spin.setValue(int(node.port or 443))
        self.pned_enabled_check.setChecked(bool(node.enabled))
        self.pned_remark_edit.setText(node.remark)
        self.pned_tags_edit.setText(node.tags)
        self.pned_subscription_label.setText(node.subscription_name or "-")
        self.pned_uuid_edit.setText(node.credential_uuid)
        self.pned_password_edit.setText(node.credential_password)
        self.pned_method_combo.setCurrentText(node.method or "aes-256-gcm")
        self.pned_flow_combo.setCurrentText(node.flow)
        self.pned_alterid_spin.setValue(int(node.alter_id or 0))
        self.pned_vmess_security_combo.setCurrentText(node.vmess_security or "auto")
        self.pned_socks_user_edit.setText(node.host)
        self.pned_obfs_edit.setText(node.obfs)
        self.pned_obfs_password_edit.setText(node.obfs_password)
        self.pned_congestion_combo.setCurrentText(node.congestion_control or "bbr")
        self.pned_udp_mode_combo.setCurrentText(node.udp_relay_mode or "native")
        transport_index = self.pned_transport_combo.findData(node.transport)
        self.pned_transport_combo.setCurrentIndex(max(0, transport_index))
        security_index = self.pned_security_combo.findData(node.security)
        self.pned_security_combo.setCurrentIndex(max(0, security_index))
        self.pned_host_edit.setText(node.host if node.protocol not in (PMS_PROTOCOL_SOCKS, PMS_PROTOCOL_HTTP) else "")
        self.pned_path_edit.setText(node.path)
        self.pned_service_edit.setText(node.service_name)
        self.pned_grpc_mode_combo.setCurrentText(node.grpc_mode)
        self.pned_header_type_combo.setCurrentText(node.header_type)
        self.pned_sni_edit.setText(node.sni)
        self.pned_alpn_edit.setText(node.alpn)
        self.pned_fingerprint_combo.setCurrentText(node.fingerprint)
        self.pned_insecure_check.setChecked(bool(node.allow_insecure))
        self.pned_public_key_edit.setText(node.public_key)
        self.pned_short_id_edit.setText(node.short_id)
        self.pned_spider_x_edit.setText(node.spider_x)
        self.pned_cert_edit.setText(node.cert_sha256)
        self.pned_latency_label.setText(node.pnode_display_latency())
        self.pned_speed_label.setText(node.pnode_display_speed())
        self.pned_last_test_label.setText(node.last_test_time or "-")
        self.pned_success_label.setText(f"{node.success_count} / {node.fail_count}")
        # 扩展参数（extra_json）：美化显示；非对象内容原样展示以便排查
        if getattr(self, "pned_extra_json_edit", None) is not None:
            if node.extra_json:
                try:
                    parsed = json.loads(node.extra_json)
                    self.pned_extra_json_edit.setPlainText(json.dumps(parsed, ensure_ascii=False, indent=2))
                except ValueError:
                    self.pned_extra_json_edit.setPlainText(node.extra_json)
            else:
                self.pned_extra_json_edit.clear()

    def pned_collect_node(self) -> PmsProxyNode:
        """把界面数据收集回节点对象。"""
        node = self.pned_node
        node.name = self.pned_name_edit.text().strip()
        node.group_name = self.pned_group_edit.currentText().strip() or "默认分组"
        node.protocol = str(self.pned_protocol_combo.currentData() or PMS_PROTOCOL_VMESS)
        node.server = self.pned_server_edit.text().strip().strip("[]")
        node.port = int(self.pned_port_spin.value())
        node.enabled = bool(self.pned_enabled_check.isChecked())
        node.remark = self.pned_remark_edit.text().strip()
        node.tags = self.pned_tags_edit.text().strip()
        node.credential_uuid = self.pned_uuid_edit.text().strip()
        node.credential_password = self.pned_password_edit.text()
        node.method = self.pned_method_combo.currentText().strip().lower()
        node.flow = self.pned_flow_combo.currentText().strip()
        node.alter_id = int(self.pned_alterid_spin.value())
        node.vmess_security = self.pned_vmess_security_combo.currentText().strip() or "auto"
        node.host = self.pned_socks_user_edit.text().strip() if node.protocol in (PMS_PROTOCOL_SOCKS, PMS_PROTOCOL_HTTP) else self.pned_host_edit.text().strip()
        node.obfs = self.pned_obfs_edit.text().strip()
        node.obfs_password = self.pned_obfs_password_edit.text()
        node.congestion_control = self.pned_congestion_combo.currentText().strip()
        node.udp_relay_mode = self.pned_udp_mode_combo.currentText().strip()
        node.transport = str(self.pned_transport_combo.currentData() or PMS_TRANSPORT_TCP)
        node.security = str(self.pned_security_combo.currentData() or PMS_SECURITY_NONE)
        node.path = self.pned_path_edit.text().strip()
        node.service_name = self.pned_service_edit.text().strip()
        node.grpc_mode = self.pned_grpc_mode_combo.currentText().strip()
        node.header_type = self.pned_header_type_combo.currentText().strip()
        node.sni = self.pned_sni_edit.text().strip()
        node.alpn = self.pned_alpn_edit.text().strip()
        node.fingerprint = self.pned_fingerprint_combo.currentText().strip()
        node.allow_insecure = bool(self.pned_insecure_check.isChecked())
        node.public_key = self.pned_public_key_edit.text().strip()
        node.short_id = self.pned_short_id_edit.text().strip()
        node.spider_x = self.pned_spider_x_edit.text().strip()
        raw_cert = self.pned_cert_edit.text().strip()
        node.cert_sha256 = pms_normalize_cert_sha256(raw_cert)
        if raw_cert and not node.cert_sha256:
            QMessageBox.warning(
                self, "证书指纹无效",
                "pinnedPeerCertSha256 必须是 64 位十六进制 SHA-256\n"
                "（可带空格 / 冒号 / 连字符，会自动去除并转小写）。\n"
                "当前填写的值不合法，已忽略。",
            )
        # 加载期间（_pned_loading）不要回写，避免把尚未填充的控件内容清掉节点既有值
        if getattr(self, "_pned_loading", False):
            return node
        # 扩展参数（extra_json）：仅当为合法 JSON 对象时回写，非法保留原值由校验拦截
        extra_text = self.pned_extra_json_edit.toPlainText().strip()
        if extra_text:
            try:
                extra_data = json.loads(extra_text)
                if isinstance(extra_data, dict):
                    node.extra_json = json.dumps(extra_data, ensure_ascii=False)
            except ValueError:
                pass
        else:
            node.extra_json = ""
        node.updated_at = pms_now_text()
        return node

    # ------------------------------------------------------------------ 显隐与校验
    def pned_update_visibility(self) -> None:
        """按协议显隐字段，减少无关选项干扰。"""
        protocol = str(self.pned_protocol_combo.currentData() or PMS_PROTOCOL_VMESS)
        transport = str(self.pned_transport_combo.currentData() or PMS_TRANSPORT_TCP)
        security = str(self.pned_security_combo.currentData() or PMS_SECURITY_NONE)
        credential_form = getattr(self, "pned_credential_form", None)
        if credential_form is not None:
            credential_form.setRowVisible(self.pned_row_method, protocol == PMS_PROTOCOL_SHADOWSOCKS)
            credential_form.setRowVisible(self.pned_row_flow, protocol == PMS_PROTOCOL_VLESS)
            credential_form.setRowVisible(self.pned_row_alterid, protocol == PMS_PROTOCOL_VMESS)
            credential_form.setRowVisible(self.pned_row_vmess_security, protocol == PMS_PROTOCOL_VMESS)
            # tg_webproxy 是标准 SOCKS5，但账号形式就是 secret 一个密码（落在「密码」行），没有独立的代理用户名；
            # 故这里不显示「代理用户名」行，避免误填。tg_proxy（MTProto）也无此概念。
            credential_form.setRowVisible(self.pned_row_socks_user, protocol in (PMS_PROTOCOL_SOCKS, PMS_PROTOCOL_HTTP))
            credential_form.setRowVisible(self.pned_row_obfs, protocol == PMS_PROTOCOL_HYSTERIA2)
            credential_form.setRowVisible(self.pned_row_obfs_password, protocol == PMS_PROTOCOL_HYSTERIA2)
            credential_form.setRowVisible(self.pned_row_congestion, protocol == PMS_PROTOCOL_TUIC)
            credential_form.setRowVisible(self.pned_row_udp_mode, protocol == PMS_PROTOCOL_TUIC)
        transport_form = getattr(self, "pned_transport_form", None)
        if transport_form is not None:
            uses_path = transport in (PMS_TRANSPORT_WS, PMS_TRANSPORT_HTTP, PMS_TRANSPORT_H2, PMS_TRANSPORT_XHTTP, PMS_TRANSPORT_HTTPUPGRADE)
            transport_form.setRowVisible(self.pned_row_path, uses_path)
            transport_form.setRowVisible(self.pned_row_service, transport == PMS_TRANSPORT_GRPC)
            transport_form.setRowVisible(self.pned_row_grpc_mode, transport == PMS_TRANSPORT_GRPC)
            transport_form.setRowVisible(self.pned_row_header_type, transport == PMS_TRANSPORT_TCP and protocol == PMS_PROTOCOL_VMESS)
            transport_form.setRowVisible(self.pned_row_public_key, security == PMS_SECURITY_REALITY)
            transport_form.setRowVisible(self.pned_row_short_id, security == PMS_SECURITY_REALITY)
            transport_form.setRowVisible(self.pned_row_spider_x, security == PMS_SECURITY_REALITY)
            transport_form.setRowVisible(self.pned_row_cert, security == PMS_SECURITY_TLS)
        for widget in (self.pned_uuid_edit, self.pned_uuid_btn):
            widget.setEnabled(protocol in (PMS_PROTOCOL_VMESS, PMS_PROTOCOL_VLESS, PMS_PROTOCOL_TUIC, PMS_PROTOCOL_JUICITY))
        self.pned_password_edit.setEnabled(protocol in (
            PMS_PROTOCOL_TROJAN, PMS_PROTOCOL_SHADOWSOCKS, PMS_PROTOCOL_HYSTERIA2, PMS_PROTOCOL_TUIC, PMS_PROTOCOL_SOCKS, PMS_PROTOCOL_HTTP,
            PMS_PROTOCOL_SSR, PMS_PROTOCOL_NAIVE, PMS_PROTOCOL_ANYTLS, PMS_PROTOCOL_SHADOWTLS, PMS_PROTOCOL_SNELL, PMS_PROTOCOL_JUICITY, PMS_PROTOCOL_SOCKS4,
            PMS_PROTOCOL_TG_WEBPROXY, PMS_PROTOCOL_TG_PROXY,
        ))
        self.pned_update_validation()

    def pned_extra_json_error(self) -> str:
        """校验扩展参数文本框是否为合法 JSON 对象（空串视为无）。"""
        text = self.pned_extra_json_edit.toPlainText().strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
        except ValueError as exc:
            return f"扩展参数不是合法 JSON：{exc}"
        if not isinstance(data, dict):
            return "扩展参数必须是 JSON 对象（{}）"
        return ""

    def pned_update_validation(self) -> None:
        """实时校验并提示。"""
        node = self.pned_collect_node()
        errors = node.pnode_validate()
        extra_err = self.pned_extra_json_error()
        if extra_err:
            errors.append(extra_err)
        if errors:
            self.pned_validation_label.setObjectName("errorLabel")
            self.pned_validation_label.setText("配置问题：" + "；".join(errors))
        else:
            self.pned_validation_label.setObjectName("okLabel")
            self.pned_validation_label.setText("配置有效，可以保存。")
        self.pned_validation_label.style().unpolish(self.pned_validation_label)
        self.pned_validation_label.style().polish(self.pned_validation_label)

    # ------------------------------------------------------------------ 动作
    def pned_parse_from_link(self) -> None:
        """解析输入框中的分享链接并填充表单。"""
        text = self.pned_link_edit.text().strip()
        if not text:
            QMessageBox.information(self, "提示", "请先粘贴分享链接")
            return
        nodes, report = PmsTextImportClassifier(self.pned_parser).ptic_import_text(text, self.pned_group_edit.currentText().strip() or "导入")
        if not nodes:
            QMessageBox.warning(self, "解析失败", report.pimp_summary_text())
            return
        self.pned_load_node(nodes[0])
        self.pned_update_visibility()
        QMessageBox.information(self, "解析成功", f"已解析并填充：{nodes[0].pnode_display_name()}")

    def pned_parse_from_clipboard(self) -> None:
        """从剪贴板解析。"""
        clipboard = QApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        if not text.strip():
            QMessageBox.information(self, "提示", "剪贴板为空")
            return
        self.pned_link_edit.setText(text.strip().splitlines()[0])
        self.pned_parse_from_link()

    def pned_copy_share_link(self) -> None:
        """把当前表单内容转成分享链接并复制。"""
        node = self.pned_collect_node()
        link = self.pned_writer.pslw_node_to_link(node)
        if not link:
            QMessageBox.warning(self, "无法生成", "当前协议不支持生成分享链接")
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(link)
        QMessageBox.information(self, "已复制", link[:400])

    def pned_show_qr(self) -> None:
        """显示当前节点的二维码。"""
        node = self.pned_collect_node()
        link = self.pned_writer.pslw_node_to_link(node)
        if not link:
            QMessageBox.warning(self, "无法生成", "当前协议不支持二维码分享")
            return
        dialog = PmsShareQrDialog(link, node.pnode_display_name(), self)
        dialog.exec()

    def pned_accept(self) -> None:
        """保存前再校验一次。"""
        node = self.pned_collect_node()
        extra_err = self.pned_extra_json_error()
        if extra_err:
            QMessageBox.warning(self, "无法保存", extra_err)
            return
        errors = node.pnode_validate()
        if errors:
            QMessageBox.warning(self, "无法保存", "；".join(errors))
            return
        self.pned_result_node = node
        self.accept()
