#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计设置部件（v1.6.1 从 settingsdialog.py 迁出）

为什么单独成文件：settingsdialog.py 已顶到 1000 行红线（项目宪法第 6 条），
v1.6.1 要新增「自动切换」页签，先把这一页挪出来腾行数，逻辑一字不改。
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pms.constants import (
    PMS_STATS_SOURCE_TITLES,
    PMS_TRAFFIC_TICK_MS,
)
from pms.gui.widgets import pms_make_hint_label

if TYPE_CHECKING:  # 仅类型检查用，避免与运行时模块循环导入
    from pms.settings import PmsSettingsManager


class PmsStatsSettingsWidget(QWidget):
    """「设置 → 统计」页：统计来源、轮询间隔、Clash / Stats API 端口、直连流量。"""

    def __init__(self, pms_settings: "PmsSettingsManager", pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.pssw_settings = pms_settings
        self.pssw_build_ui()
        self.pssw_load()

    # ------------------------------------------------------------------ 界面
    def pssw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        group = QGroupBox("流量与连接统计")
        form = QFormLayout(group)
        self.pssw_combo_source = QComboBox()
        for value, title in PMS_STATS_SOURCE_TITLES.items():
            self.pssw_combo_source.addItem(title, value)
        self.pssw_spin_interval = QSpinBox()
        self.pssw_spin_interval.setRange(300, 10000)
        self.pssw_spin_clash_port = QSpinBox()
        self.pssw_spin_clash_port.setRange(0, 65535)
        self.pssw_spin_clash_port.setSpecialValueText("自动")
        self.pssw_spin_stats_port = QSpinBox()
        self.pssw_spin_stats_port.setRange(0, 65535)
        self.pssw_spin_stats_port.setSpecialValueText("自动")
        self.pssw_chk_count_direct = QCheckBox("统计直连流量（Xray 由上/下行差值估算）")
        form.addRow("统计来源", self.pssw_combo_source)
        form.addRow("轮询间隔（毫秒）", self.pssw_spin_interval)
        form.addRow("Clash API 端口", self.pssw_spin_clash_port)
        form.addRow("Stats API 端口", self.pssw_spin_stats_port)
        form.addRow(self.pssw_chk_count_direct)
        layout.addWidget(group)
        layout.addWidget(pms_make_hint_label(
            "说明：sing-box 通过 Clash API 读取真实连接列表与累计流量；Xray 通过 `xray api statsquery` 读取真实字节数。"
            "两者都是核心自身统计，不是估算或模拟。"
        ))
        layout.addStretch(1)

    def pssw_load(self) -> None:
        settings = self.pssw_settings
        current = str(settings.psm_get_str("stats.source", ""))
        index = self.pssw_combo_source.findData(current)
        self.pssw_combo_source.setCurrentIndex(max(0, index))
        self.pssw_spin_interval.setValue(int(settings.psm_get_int("stats.interval_ms", PMS_TRAFFIC_TICK_MS)))
        self.pssw_spin_clash_port.setValue(int(settings.psm_get_int("stats.clash_api_port", 0)))
        self.pssw_spin_stats_port.setValue(int(settings.psm_get_int("stats.xray_api_port", 0)))
        self.pssw_chk_count_direct.setChecked(bool(settings.psm_get_bool("stats.count_direct_traffic", True)))
        self.pssw_combo_source.currentIndexChanged.connect(
            lambda _index: settings.psm_set("stats.source", str(self.pssw_combo_source.currentData()))
        )
        self.pssw_spin_interval.valueChanged.connect(lambda value: settings.psm_set("stats.interval_ms", int(value)))
        self.pssw_spin_clash_port.valueChanged.connect(lambda value: settings.psm_set("stats.clash_api_port", int(value)))
        self.pssw_spin_stats_port.valueChanged.connect(lambda value: settings.psm_set("stats.xray_api_port", int(value)))
        self.pssw_chk_count_direct.toggled.connect(lambda value: settings.psm_set("stats.count_direct_traffic", bool(value)))
