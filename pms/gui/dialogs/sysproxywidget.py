#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统代理（PAC 模式）设置部件（v1.9.49 新增）

为什么单独成文件：`settingsdialog.py` 顶在 1000 行红线（项目宪法第 6 条），
v1.9.49 要新增「系统代理模式（固定 / PAC）+ PAC 直连域名 + PAC 预览与写入」，
按 v1.6.5 立下的规矩放进独立部件；`settingsdialog.py` 只加「同行 import + 1 行挂载」。

职责边界：本部件只做「显示 + 改设置 + 生成/写入 PAC 文件」，**不动注册表**。
真正的注册表开启 / 关闭 / 还原由主窗口调 `PmsWindowsSystemProxy` 完成
（`pmw_apply_system_proxy()`），这样测试也不需要真写 HKCU。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, TYPE_CHECKING
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
import logging

from pms.constants import (
    PMS_LOGGER_NAME,
    PMS_SYSTEM_PROXY_MODE_FIXED,
    PMS_SYSTEM_PROXY_MODE_PAC,
)
from pms.coresys import PmsWindowsSystemProxy
from pms.gui.widgets import pms_make_hint_label
from pms.utils import pms_multiline_to_list

if TYPE_CHECKING:  # 仅类型检查用，避免与运行时模块循环导入
    from pms.paths import PmsRuntimePaths
    from pms.settings import PmsSettingsManager

# 模式下拉的取值与标题（放这里而不是 constants.py：constants.py 已顶红线，且只有本部件用）
PMS_SYSTEM_PROXY_MODE_TITLES: dict[str, str] = {
    PMS_SYSTEM_PROXY_MODE_FIXED: "固定代理（写 ProxyServer，兼容性最好）",
    PMS_SYSTEM_PROXY_MODE_PAC: "PAC 自动配置脚本（写 AutoConfigURL，按域名直连）",
}


class PmsSystemProxyWidget(QWidget):
    """「系统代理」分组里的模式选择 + PAC 直连域名 + 预览/写入。"""

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_paths: "PmsRuntimePaths",
        pms_parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pspw_settings = pms_settings
        self.pspw_paths = pms_paths
        self.pspw_logger = logging.getLogger(PMS_LOGGER_NAME + ".sysproxywidget")
        self.pspw_build_ui()

    # ------------------------------------------------------------------ 界面
    def pspw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        group = QGroupBox("代理模式")
        form = QFormLayout(group)
        self.pspw_combo_mode = QComboBox()
        for value, title in PMS_SYSTEM_PROXY_MODE_TITLES.items():
            self.pspw_combo_mode.addItem(title, value)
        index = self.pspw_combo_mode.findData(
            self.pspw_settings.psm_get_str("system_proxy.mode", PMS_SYSTEM_PROXY_MODE_FIXED)
        )
        self.pspw_combo_mode.setCurrentIndex(max(0, index))
        self.pspw_combo_mode.currentIndexChanged.connect(self.pspw_on_mode_changed)
        form.addRow("模式", self.pspw_combo_mode)

        self.pspw_edit_direct = QPlainTextEdit()
        self.pspw_edit_direct.setPlaceholderText("一行一个域名后缀，例如 baidu.com（留空 = 用「路由分流」页的直连域名预设）")
        self.pspw_edit_direct.setPlainText("\n".join(self.pspw_settings.psm_get_list("system_proxy.pac_direct_domains")))
        self.pspw_edit_direct.setMaximumHeight(110)
        self.pspw_edit_direct.textChanged.connect(self.pspw_on_direct_changed)
        form.addRow("PAC 直连域名", self.pspw_edit_direct)

        self.pspw_label_file = QLabel("")
        self.pspw_label_file.setWordWrap(True)
        form.addRow("PAC 文件", self.pspw_label_file)

        # v1.9.62：脚本来源选择（自动生成 / PAC 编辑器写的 pms_custom.pac）。
        # 只改设置、不写注册表——真正生效在主窗口 pmw_apply_system_proxy()。
        self.pspw_combo_source = QComboBox()
        self.pspw_combo_source.addItem("程序自动生成（只支持直连域名 + 单代理）", PMS_SYSTEM_PROXY_MODE_FIXED)
        self.pspw_combo_source.addItem("PAC 编辑器写的 pms_custom.pac", "custom")
        source_index = self.pspw_combo_source.findData(
            self.pspw_settings.psm_get_str("system_proxy.pac_source", PMS_SYSTEM_PROXY_MODE_FIXED)
        )
        self.pspw_combo_source.setCurrentIndex(max(0, source_index))
        self.pspw_combo_source.setEnabled(
            str(self.pspw_settings.psm_get_str("system_proxy.mode", PMS_SYSTEM_PROXY_MODE_FIXED)) == PMS_SYSTEM_PROXY_MODE_PAC
        )
        self.pspw_combo_source.currentIndexChanged.connect(self.pspw_on_source_changed)
        form.addRow("PAC 脚本来源", self.pspw_combo_source)

        button_row = QHBoxLayout()
        self.pspw_button_refresh = QPushButton("刷新预览")
        self.pspw_button_refresh.clicked.connect(self.pspw_refresh_preview)
        self.pspw_button_write = QPushButton("生成并写入 PAC 文件")
        self.pspw_button_write.clicked.connect(self.pspw_write_pac_file)
        button_row.addWidget(self.pspw_button_refresh)
        button_row.addWidget(self.pspw_button_write)
        button_row.addStretch(1)
        form.addRow("", button_row)

        self.pspw_preview = QPlainTextEdit()
        self.pspw_preview.setReadOnly(True)
        self.pspw_preview.setMaximumHeight(160)
        form.addRow("预览", self.pspw_preview)

        layout.addWidget(group)
        layout.addWidget(pms_make_hint_label(
            "PAC 模式写注册表 AutoConfigURL 指向本程序生成在数据目录的 .pac 脚本，"
            "并把 ProxyEnable 置 0——两者同时生效会让行为不可预期，所以互斥。"
            "退出时会把 AutoConfigURL 连同 ProxyServer / ProxyEnable / ProxyOverride 一起还原，"
            "不会覆盖你原来的 PAC 设置。"
        ))
        self.pspw_refresh_preview()

    # ------------------------------------------------------------------ 槽
    def pspw_on_mode_changed(self, _index: int) -> None:
        """模式改动即保存（与其它设置项口径一致）。

        v1.9.62：切到固定代理时把 PAC 来源复位成 auto——否则用户从 PAC 模式
        切回固定代理、以后再切回 PAC 时，会莫名用到 PAC 编辑器写的手写脚本。
        """
        mode = str(self.pspw_combo_mode.currentData() or PMS_SYSTEM_PROXY_MODE_FIXED)
        self.pspw_settings.psm_set("system_proxy.mode", mode)
        if mode == PMS_SYSTEM_PROXY_MODE_FIXED:
            self.pspw_settings.psm_set("system_proxy.pac_source", PMS_SYSTEM_PROXY_MODE_FIXED)
            self.pspw_combo_source.blockSignals(True)
            self.pspw_combo_source.setCurrentIndex(
                max(0, self.pspw_combo_source.findData(PMS_SYSTEM_PROXY_MODE_FIXED))
            )
            self.pspw_combo_source.blockSignals(False)
        else:
            self.pspw_combo_source.setEnabled(True)
        self.pspw_refresh_preview()

    def pspw_on_source_changed(self, _index: int) -> None:
        """PAC 脚本来源改动即保存（v1.9.62）。

        只改设置、不写注册表——真正生效在主窗口 `pmw_apply_system_proxy()`。
        """
        source = str(self.pspw_combo_source.currentData() or PMS_SYSTEM_PROXY_MODE_FIXED)
        self.pspw_settings.psm_set("system_proxy.pac_source", source)
        self.pspw_refresh_preview()

    def pspw_on_direct_changed(self) -> None:
        """直连域名改动即保存（逐行去空白、去空行）。"""
        self.pspw_settings.psm_set(
            "system_proxy.pac_direct_domains",
            pms_multiline_to_list(self.pspw_edit_direct.toPlainText()),
        )
        self.pspw_refresh_preview()

    def pspw_effective_direct_domains(self) -> list[str]:
        """本部件实际用于生成 PAC 的直连域名：自己的设置优先，留空回落到路由页的直连预设。"""
        domains = self.pspw_settings.psm_get_list("system_proxy.pac_direct_domains")
        if domains:
            return domains
        return self.pspw_settings.psm_get_list("routing.direct_domains")

    def pspw_proxy_port(self) -> int:
        """PAC 要指向的本地入口端口（与开启系统代理时同一个来源）。"""
        return int(self.pspw_settings.psm_get_int("inbound.mixed_port", 0) or 0)

    def pspw_refresh_preview(self) -> None:
        """按当前设置重新生成 PAC 文本并显示（只预览，不落盘）。

        v1.9.62：来源=custom 时预览的是 pms_custom.pac 的真实内容（用户手写 / 小白生成），
        不再生成自动脚本——否则用户会以为「预览的这份」就是要生效的那份。
        """
        controller = PmsWindowsSystemProxy(self.pspw_logger)
        if str(self.pspw_settings.psm_get_str("system_proxy.pac_source", PMS_SYSTEM_PROXY_MODE_FIXED)) == "custom":
            custom = Path(str(self.pspw_paths.prt_custom_pac_file))
            if custom.is_file():
                text = custom.read_text(encoding="utf-8", errors="replace")
                self.pspw_label_file.setText(f"用户 PAC（PAC 编辑器产物）：{custom}")
            else:
                text = "（还没有生成 pms_custom.pac）\n\n请点上方「打开 PAC 编辑器…」，在小白模式填表或专业模式手写，保存后即可生效。"
                self.pspw_label_file.setText(f"用户 PAC（尚未生成）：{custom}")
            self.pspw_preview.setPlainText(text)
            return
        text = controller.pwsp_build_pac_text("127.0.0.1", self.pspw_proxy_port(), self.pspw_effective_direct_domains())
        self.pspw_preview.setPlainText(text)
        self.pspw_label_file.setText(str(self.pspw_paths.prt_pac_file))

    def pspw_write_pac_file(self) -> bool:
        """把当前 PAC 文本真正写入数据目录（失败记 warning，绝不静默）。"""
        controller = PmsWindowsSystemProxy(self.pspw_logger)
        text = self.pspw_preview.toPlainText() or controller.pwsp_build_pac_text(
            "127.0.0.1", self.pspw_proxy_port(), self.pspw_effective_direct_domains()
        )
        target = self.pspw_paths.prt_pac_file
        ok = controller.pwsp_write_pac_file(target, text)
        if ok:
            self.pspw_label_file.setText(f"已写入：{target}")
        else:
            self.pspw_logger.warning("PAC 文件写入失败：%s", target)
            self.pspw_label_file.setText(f"写入失败：{target}（详见日志）")
        return ok
