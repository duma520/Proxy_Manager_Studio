#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接口设置部件（v1.6.0 新建）

为什么单独成文件：settingsdialog.py 已在 1000 行红线边上（项目宪法第 6 条），
新增设置项一律走独立部件（同 v1.5.1 的 coreupdatewidget、v1.4.4 的 telegramwidget）。

本部件只负责「显示 + 改设置 + 发动作」：
    - 开关 / 监听地址 / 端口 / 鉴权模式 → 直接写设置（改动即保存）
    - token 只走 .env（`PMS_API_TOKEN`），**绝不写进 pms_settings.json**
    - 任何改动都发 `psaw_action("api_settings_changed")`，由主窗口重启接口服务
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING
from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pms.apiserver import pms_api_generate_token
from pms.webpanel import pms_web_panel_url
from pms.constants import (
    PMS_API_AUTH_MANUAL,
    PMS_API_AUTH_OFF,
    PMS_API_AUTH_TITLES,
    PMS_API_DEFAULT_HOST,
    PMS_API_DEFAULT_PORT,
    PMS_API_PATH_PREFIX,
    PMS_API_TOKEN_ENV,
)

if TYPE_CHECKING:  # 仅类型检查用，避免与设置 / 环境模块循环导入
    from pms.envstore import PmsEnvStore
    from pms.settings import PmsSettingsManager


class PmsApiSettingsWidget(QWidget):
    """「设置 → 接口」页：开关、监听地址、端口、鉴权三选一、token 管理。"""

    psaw_action = Signal(str, dict)   # 动作名, 参数（由主窗口重启接口服务）

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_env: "PmsEnvStore",
        pms_parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.psaw_settings = pms_settings
        self.psaw_env = pms_env
        self.psaw_token_visible = False
        self.psaw_build_ui()
        self.psaw_bind()
        self.psaw_refresh_hint()

    # ------------------------------------------------------------------ 界面
    def psaw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("本地 HTTP 接口（默认关闭）")
        form = QFormLayout(group)
        self.psaw_chk_enabled = QCheckBox("启用本地 HTTP 接口")
        form.addRow(self.psaw_chk_enabled)

        host_row = QHBoxLayout()
        self.psaw_host_edit = QLineEdit()
        self.psaw_host_edit.setPlaceholderText("127.0.0.1")
        self.psaw_port_spin = QSpinBox()
        self.psaw_port_spin.setRange(1, 65535)
        host_row.addWidget(self.psaw_host_edit, 1)
        host_row.addWidget(QLabel("端口"))
        host_row.addWidget(self.psaw_port_spin)
        form.addRow("监听地址", host_row)

        self.psaw_auth_combo = QComboBox()
        for value, title in PMS_API_AUTH_TITLES.items():
            self.psaw_auth_combo.addItem(title, value)
        form.addRow("鉴权方式", self.psaw_auth_combo)

        # v1.6.2：写操作总开关（**默认关闭**），只读接口不受影响
        self.psaw_chk_write = QCheckBox("允许写操作（启停核心 / 切节点 / 系统代理 / TUN / 节点增删）")
        form.addRow(self.psaw_chk_write)

        # v1.9.35：本地 Web 面板（`/panel`）。面板页本身不含数据，数据仍要 token；
        # 写操作**跟随上面那个开关**，不另造一套开关。
        self.psaw_chk_panel = QCheckBox("启用本地 Web 面板（浏览器打开 /panel，只读；写操作看上面那个开关）")
        self.psaw_btn_panel = QPushButton("打开 Web 面板")
        self.psaw_btn_panel.clicked.connect(self.psaw_on_open_panel)
        panel_row = QHBoxLayout()
        panel_row.addWidget(self.psaw_chk_panel, 1)
        panel_row.addWidget(self.psaw_btn_panel)
        form.addRow(panel_row)

        token_row = QHBoxLayout()
        self.psaw_token_edit = QLineEdit()
        self.psaw_token_edit.setPlaceholderText("（自动生成模式下留空即由程序生成）")
        self.psaw_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.psaw_btn_view = QPushButton("显示")
        self.psaw_btn_view.setCheckable(True)
        self.psaw_btn_copy = QPushButton("复制")
        self.psaw_btn_regen = QPushButton("重新生成")
        token_row.addWidget(self.psaw_token_edit, 1)
        token_row.addWidget(self.psaw_btn_view)
        token_row.addWidget(self.psaw_btn_copy)
        token_row.addWidget(self.psaw_btn_regen)
        form.addRow("Token", token_row)
        layout.addWidget(group)

        self.psaw_hint = QLabel("")
        self.psaw_hint.setObjectName("hintLabel")
        self.psaw_hint.setWordWrap(True)
        layout.addWidget(self.psaw_hint)

        self.psaw_url_label = QLabel("")
        self.psaw_url_label.setObjectName("hintLabel")
        self.psaw_url_label.setWordWrap(True)
        self.psaw_url_label.setTextInteractionFlags(self.psaw_url_label.textInteractionFlags())
        layout.addWidget(self.psaw_url_label)

    def psaw_bind(self) -> None:
        settings = self.psaw_settings
        self.psaw_chk_enabled.setChecked(bool(settings.psm_get_bool("api.enabled", False)))
        self.psaw_host_edit.setText(str(settings.psm_get_str("api.host", PMS_API_DEFAULT_HOST) or PMS_API_DEFAULT_HOST))
        self.psaw_port_spin.setValue(int(settings.psm_get_int("api.port", PMS_API_DEFAULT_PORT)))
        mode = str(settings.psm_get_str("api.auth_mode", "auto") or "auto")
        index = self.psaw_auth_combo.findData(mode)
        self.psaw_auth_combo.setCurrentIndex(index if index >= 0 else 0)
        self.psaw_chk_write.setChecked(bool(settings.psm_get_bool("api.write_enabled", False)))
        self.psaw_chk_panel.setChecked(bool(settings.psm_get_bool("api.web_panel_enabled", True)))
        self.psaw_refresh_token_view()

        self.psaw_chk_enabled.toggled.connect(self.psaw_on_enabled_changed)
        self.psaw_host_edit.editingFinished.connect(self.psaw_on_host_changed)
        self.psaw_port_spin.valueChanged.connect(self.psaw_on_port_changed)
        self.psaw_auth_combo.currentIndexChanged.connect(self.psaw_on_auth_changed)
        self.psaw_chk_write.toggled.connect(self.psaw_on_write_changed)
        self.psaw_chk_panel.toggled.connect(self.psaw_on_panel_changed)
        self.psaw_token_edit.editingFinished.connect(self.psaw_on_token_edited)
        self.psaw_btn_view.toggled.connect(self.psaw_on_view_toggled)
        self.psaw_btn_copy.clicked.connect(self.psaw_on_copy_token)
        self.psaw_btn_regen.clicked.connect(self.psaw_on_regenerate_token)

    # ------------------------------------------------------------------ 事件
    def psaw_on_enabled_changed(self, pms_checked: bool) -> None:
        self.psaw_settings.psm_set("api.enabled", bool(pms_checked))
        self.psaw_refresh_hint()
        self.psaw_action.emit("api_settings_changed", {})

    def psaw_on_host_changed(self) -> None:
        host = self.psaw_host_edit.text().strip() or PMS_API_DEFAULT_HOST
        self.psaw_host_edit.setText(host)
        self.psaw_settings.psm_set("api.host", host)
        self.psaw_refresh_hint()
        self.psaw_action.emit("api_settings_changed", {})

    def psaw_on_port_changed(self, pms_value: int) -> None:
        self.psaw_settings.psm_set("api.port", int(pms_value))
        self.psaw_refresh_hint()
        self.psaw_action.emit("api_settings_changed", {})

    def psaw_on_auth_changed(self, _pms_index: int) -> None:
        self.psaw_settings.psm_set("api.auth_mode", str(self.psaw_auth_combo.currentData() or "auto"))
        self.psaw_refresh_hint()
        self.psaw_refresh_token_view()
        self.psaw_action.emit("api_settings_changed", {})

    def psaw_on_write_changed(self, pms_checked: bool) -> None:
        """写操作总开关（v1.6.2）：只写设置，不需要重启接口服务。"""
        self.psaw_settings.psm_set("api.write_enabled", bool(pms_checked))
        self.psaw_refresh_hint()

    def psaw_on_panel_changed(self, pms_checked: bool) -> None:
        """本地 Web 面板开关（v1.9.35）：只写设置——面板路由每次都实时读设置，不用重启服务。"""
        self.psaw_settings.psm_set("api.web_panel_enabled", bool(pms_checked))
        self.psaw_refresh_hint()

    def psaw_on_open_panel(self) -> None:
        """用系统默认浏览器打开面板（地址按当前 host/port 算；0.0.0.0 会换成 127.0.0.1）。"""
        url = pms_web_panel_url(self.psaw_host_edit.text().strip() or PMS_API_DEFAULT_HOST, self.psaw_port_spin.value())
        QDesktopServices.openUrl(QUrl(url))
        self.psaw_hint.setText(
            f"已用浏览器打开：{url}" + ("" if self.psaw_chk_enabled.isChecked()
                                    else "　（注意：接口服务当前未启用，页面打不开——先勾上「启用接口服务」）")
        )

    def psaw_on_token_edited(self) -> None:
        """手动模式的 token：写进 .env（**不进设置文件**），空值表示不改动。"""
        text = self.psaw_token_edit.text().strip()
        if not text:
            self.psaw_refresh_token_view()
            return
        if str(self.psaw_auth_combo.currentData() or "auto") != PMS_API_AUTH_MANUAL:
            self.psaw_settings.psm_set("api.auth_mode", PMS_API_AUTH_MANUAL)
            index = self.psaw_auth_combo.findData(PMS_API_AUTH_MANUAL)
            if index >= 0:
                self.psaw_auth_combo.setCurrentIndex(index)
        self.psaw_env.pes_set(PMS_API_TOKEN_ENV, text)
        self.psaw_refresh_hint()
        self.psaw_action.emit("api_settings_changed", {})

    def psaw_on_view_toggled(self, pms_checked: bool) -> None:
        self.psaw_token_visible = bool(pms_checked)
        self.psaw_btn_view.setText("隐藏" if pms_checked else "显示")
        self.psaw_token_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if pms_checked else QLineEdit.EchoMode.Password
        )

    def psaw_on_copy_token(self) -> None:
        token = str(self.psaw_env.pes_get(PMS_API_TOKEN_ENV, "") or "").strip()
        if not token:
            self.psaw_hint.setText("还没有 token：点「重新生成」先生成一个（会写入 .env）。")
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(token)
        self.psaw_hint.setText("token 已复制到剪贴板（只存在 .env，不会写进设置文件与日志）。")

    def psaw_on_regenerate_token(self) -> None:
        """重新生成：换一个随机 token 写入 .env，并提醒用户脚本要同步换。"""
        token = pms_api_generate_token()
        self.psaw_env.pes_set(PMS_API_TOKEN_ENV, token)
        self.psaw_refresh_token_view()
        self.psaw_hint.setText("已生成新 token 并写入 .env；之前用旧 token 的脚本需要同步更新。")
        self.psaw_action.emit("api_settings_changed", {})

    # ------------------------------------------------------------------ 显示
    def psaw_current_token(self) -> str:
        return str(self.psaw_env.pes_get(PMS_API_TOKEN_ENV, "") or "").strip()

    def psaw_refresh_token_view(self) -> None:
        token = self.psaw_current_token()
        if self.psaw_token_visible:
            self.psaw_token_edit.setText(token)
        else:
            self.psaw_token_edit.setText("*" * min(len(token), 32))
        self.psaw_token_edit.setReadOnly(str(self.psaw_auth_combo.currentData() or "auto") != PMS_API_AUTH_MANUAL)
        self.psaw_btn_regen.setEnabled(True)

    def psaw_refresh_hint(self) -> None:
        host = self.psaw_host_edit.text().strip() or PMS_API_DEFAULT_HOST
        port = int(self.psaw_port_spin.value())
        mode = str(self.psaw_auth_combo.currentData() or "auto")
        lines = [f"接口地址：http://{host}:{port}{PMS_API_PATH_PREFIX}　（只读：status / nodes / groups / traffic …）"]
        if self.psaw_chk_write.isChecked():
            lines.append("写操作**已开启**：POST core/start·core/stop·core/restart·node/switch·system-proxy·tun·nodes，PUT / DELETE nodes/<uid>；"
                         "每个请求都要派发到界面线程执行，超时 5 秒返回 504。")
        else:
            lines.append("写操作**未开启**（默认）：上面那些写接口会返回 403 write_disabled，只读接口不受影响。")
        lines.append("⚠ 导出接口（GET export/links · export/backup · export/clash · export/singbox）"
                     "返回的内容**含真实口令与 UUID**，且**不受写操作开关限制**——token 是唯一防线，切勿外传。")
        if not self.psaw_chk_enabled.isChecked():
            lines.append("当前未启用：打勾后保存即启动，重启程序也会自动拉起。")
        if host not in ("127.0.0.1", "localhost", "::1"):
            lines.append("监听地址不是本机地址（会暴露到局域网）：")
            if mode == PMS_API_AUTH_OFF:
                lines.append("　**已关闭鉴权 + 非本机地址 = 拒绝启动**，请改用「自动生成」或「自己填」并设好防火墙。")
            else:
                lines.append("　已开启鉴权，启动后仍建议配合防火墙限制来源 IP。")
        if mode == PMS_API_AUTH_OFF:
            lines.append("关闭鉴权：本机任何程序都能调接口（只读照常，写操作还需勾「允许写操作」）。")
        panel_url = pms_web_panel_url(host, port)
        if self.psaw_chk_panel.isChecked():
            lines.append(f"本地 Web 面板**已启用**：浏览器打开 {panel_url}（面板页不含数据，数据接口仍要 token；"
                         "写操作跟随上面的开关）。")
        else:
            lines.append(f"本地 Web 面板**已关闭**：访问 {panel_url} 只会看到一张说明页；数据接口照常。")
        self.psaw_hint.setText("\n".join(lines))
        self.psaw_url_label.setText(
            "curl 示例：curl -H \"Authorization: Bearer <token>\" "
            f"http://{host}:{port}{PMS_API_PATH_PREFIX}/status"
        )
