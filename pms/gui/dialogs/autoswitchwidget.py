#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动切换设置部件（v1.6.1 新建）

为什么单独成文件：settingsdialog.py 已顶到 1000 行红线（项目宪法第 6 条），
新增设置项一律走独立部件（同 v1.6.0 的 apiwidget、v1.6.1 的 statswidget）。

本部件只负责「显示 + 改设置 + 发动作」：
    - 开关 / 间隔 / 失败次数 / 检测方式 / 提醒 → 直接写设置（改动即保存）
    - 「立即检测一次」发 `paww_action("autoswitch_check_now")`，由主窗口驱动服务
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pms.constants import (
    PMS_AUTOSWITCH_DEFAULT_FAIL_THRESHOLD,
    PMS_AUTOSWITCH_DEFAULT_INTERVAL_SECONDS,
    PMS_AUTOSWITCH_DETECT_TCP,
    PMS_AUTOSWITCH_DETECT_TITLES,
    PMS_AUTOSWITCH_MAX_FAIL_THRESHOLD,
    PMS_AUTOSWITCH_MAX_INTERVAL_SECONDS,
    PMS_AUTOSWITCH_MIN_FAIL_THRESHOLD,
    PMS_AUTOSWITCH_MIN_INTERVAL_SECONDS,
    PMS_FASTEST_SCOPE_TESTED,
    PMS_FASTEST_SCOPE_TITLES,
)

if TYPE_CHECKING:  # 仅类型检查用，避免与运行时模块循环导入
    from pms.settings import PmsSettingsManager


class PmsAutoSwitchSettingsWidget(QWidget):
    """「设置 → 自动切换」页：开关、间隔、失败阈值、检测方式、提醒、立即检测。"""

    paww_action = Signal(str, dict)   # 动作名, 参数（由主窗口执行）

    def __init__(self, pms_settings: "PmsSettingsManager", pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.paww_settings = pms_settings
        self.paww_build_ui()
        self.paww_load()
        self.paww_refresh_hint()

    # ------------------------------------------------------------------ 界面
    def paww_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("定时健康检测与自动切换（默认关闭）")
        form = QFormLayout(group)
        self.paww_chk_enabled = QCheckBox("核心运行中定时检测当前节点，失效就自动切换")
        self.paww_spin_interval = QSpinBox()
        self.paww_spin_interval.setRange(PMS_AUTOSWITCH_MIN_INTERVAL_SECONDS, PMS_AUTOSWITCH_MAX_INTERVAL_SECONDS)
        self.paww_spin_interval.setSuffix(" 秒")
        self.paww_spin_fail = QSpinBox()
        self.paww_spin_fail.setRange(PMS_AUTOSWITCH_MIN_FAIL_THRESHOLD, PMS_AUTOSWITCH_MAX_FAIL_THRESHOLD)
        self.paww_spin_fail.setSuffix(" 次")
        self.paww_combo_mode = QComboBox()
        for value, title in PMS_AUTOSWITCH_DETECT_TITLES.items():
            self.paww_combo_mode.addItem(title, value)
        self.paww_chk_notify = QCheckBox("切换后弹托盘气泡提醒")
        form.addRow(self.paww_chk_enabled)
        form.addRow("检测间隔", self.paww_spin_interval)
        form.addRow("连续失败几次才切换", self.paww_spin_fail)
        form.addRow("检测方式", self.paww_combo_mode)
        form.addRow(self.paww_chk_notify)
        layout.addWidget(group)

        button_row = QHBoxLayout()
        self.paww_btn_check = QPushButton("立即检测一次")
        self.paww_btn_check.clicked.connect(lambda: self.paww_action.emit("autoswitch_check_now", {}))
        button_row.addWidget(self.paww_btn_check)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        # v1.9.33：测速后自动切到最快节点（与上面这套定时检测共用同一条切换链路）
        fastest_group = QGroupBox("测速后自动切到最快节点")
        fastest_form = QFormLayout(fastest_group)
        self.paww_chk_fastest = QCheckBox("测速结束后，自动连上最快的那一个节点")
        self.paww_combo_fastest_scope = QComboBox()
        for value, title in PMS_FASTEST_SCOPE_TITLES.items():
            self.paww_combo_fastest_scope.addItem(title, value)
        self.paww_spin_fastest_min = QSpinBox()
        self.paww_spin_fastest_min.setRange(0, 1024 * 1024)
        self.paww_spin_fastest_min.setSuffix(" KB/s")
        self.paww_spin_fastest_min.setToolTip("0 = 不限；填了门槛时，最快节点低于它就不切")
        fastest_form.addRow(self.paww_chk_fastest)
        fastest_form.addRow("候选范围（分组策略）", self.paww_combo_fastest_scope)
        fastest_form.addRow("最低速度门槛", self.paww_spin_fastest_min)
        layout.addWidget(fastest_group)

        self.paww_hint = QLabel("")
        self.paww_hint.setObjectName("hintLabel")
        self.paww_hint.setWordWrap(True)
        layout.addWidget(self.paww_hint)
        layout.addStretch(1)

    def paww_load(self) -> None:
        settings = self.paww_settings
        self.paww_chk_enabled.setChecked(bool(settings.psm_get_bool("autoswitch.enabled", False)))
        self.paww_spin_interval.setValue(int(settings.psm_get_int(
            "autoswitch.interval_seconds", PMS_AUTOSWITCH_DEFAULT_INTERVAL_SECONDS,
        )))
        self.paww_spin_fail.setValue(int(settings.psm_get_int(
            "autoswitch.fail_threshold", PMS_AUTOSWITCH_DEFAULT_FAIL_THRESHOLD,
        )))
        mode = str(settings.psm_get_str("autoswitch.detect_mode", PMS_AUTOSWITCH_DETECT_TCP))
        index = self.paww_combo_mode.findData(mode)
        self.paww_combo_mode.setCurrentIndex(index if index >= 0 else 0)
        self.paww_chk_notify.setChecked(bool(settings.psm_get_bool("autoswitch.notify", True)))
        self.paww_chk_fastest.setChecked(bool(settings.psm_get_bool("autoswitch.fastest_after_speed", True)))
        scope = str(settings.psm_get_str("autoswitch.fastest_scope", PMS_FASTEST_SCOPE_TESTED))
        index = self.paww_combo_fastest_scope.findData(scope)
        self.paww_combo_fastest_scope.setCurrentIndex(index if index >= 0 else 0)
        self.paww_spin_fastest_min.setValue(int(settings.psm_get_int("autoswitch.fastest_min_speed", 0)))
        self.paww_chk_enabled.toggled.connect(lambda value: self.paww_on_changed("autoswitch.enabled", bool(value)))
        self.paww_spin_interval.valueChanged.connect(lambda value: self.paww_on_changed("autoswitch.interval_seconds", int(value)))
        self.paww_spin_fail.valueChanged.connect(lambda value: self.paww_on_changed("autoswitch.fail_threshold", int(value)))
        self.paww_combo_mode.currentIndexChanged.connect(
            lambda _index: self.paww_on_changed("autoswitch.detect_mode", str(self.paww_combo_mode.currentData()))
        )
        self.paww_chk_notify.toggled.connect(lambda value: self.paww_on_changed("autoswitch.notify", bool(value)))
        self.paww_chk_fastest.toggled.connect(lambda value: self.paww_on_changed("autoswitch.fastest_after_speed", bool(value)))
        self.paww_combo_fastest_scope.currentIndexChanged.connect(
            lambda _index: self.paww_on_changed("autoswitch.fastest_scope", str(self.paww_combo_fastest_scope.currentData()))
        )
        self.paww_spin_fastest_min.valueChanged.connect(
            lambda value: self.paww_on_changed("autoswitch.fastest_min_speed", int(value))
        )

    # ------------------------------------------------------------------ 事件
    def paww_on_changed(self, pms_key: str, pms_value: object) -> None:
        """写设置（改动即保存）+ 刷新说明；主窗口靠设置变更信号重建定时器。"""
        self.paww_settings.psm_set(pms_key, pms_value)
        self.paww_refresh_hint()

    def paww_refresh_hint(self) -> None:
        mode = str(self.paww_combo_mode.currentData() or PMS_AUTOSWITCH_DETECT_TCP)
        lines = [
            f"每 {int(self.paww_spin_interval.value())} 秒检测一次；连续失败 {int(self.paww_spin_fail.value())} 次才判定当前节点失效。"
            "单次抖动不会切换，避免来回横跳。",
            "候选范围：全部已启用节点（排除当前节点）；切换前先对候选做一轮 TCP 握手探测，挑延迟最低的再切。",
            "检测方式只用于判定「当前节点是否失效」："
            + ("TCP 握手——直连节点服务器端口，最快最省。" if mode == PMS_AUTOSWITCH_DETECT_TCP
               else "HTTP 探测——经本地代理访问测试网址，验证真实通路（超时用「延迟测试」里的超时设置）。"),
        ]
        if not self.paww_chk_enabled.isChecked():
            lines.append("当前未启用：打勾后保存即生效，重启程序也会自动继续。")
        lines.append("注意：核心没运行时不会检测，也不会自动拉起核心。")
        scope_title = str(self.paww_combo_fastest_scope.currentData() or PMS_FASTEST_SCOPE_TESTED)
        fastest_line = f"测速后自动切最快：{'已开启' if self.paww_chk_fastest.isChecked() else '已关闭'}"
        if self.paww_chk_fastest.isChecked():
            fastest_line += f"，候选范围 = {PMS_FASTEST_SCOPE_TITLES.get(scope_title, scope_title)}"
            minimum = int(self.paww_spin_fastest_min.value())
            fastest_line += f"；门槛 = {'不限' if minimum <= 0 else str(minimum) + ' KB/s'}"
            fastest_line += "。核心没在跑时会跳过（不会替你拉起核心）。"
        lines.append(fastest_line)
        self.paww_hint.setText("\n".join(lines))
