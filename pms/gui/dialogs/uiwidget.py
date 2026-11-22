#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面布局设置部件（v1.6.5 从 settingsdialog.py 迁出，并新增「快捷操作面板可隐藏」）

为什么单独成文件：settingsdialog.py 已顶到 1000 行红线（项目宪法第 6 条），
v1.6.5 要新增「左侧快捷操作面板可隐藏」，顺手把「面板可见性」这一组整体挪出来，
settingsdialog.py 因此净减行数；以后新增界面类设置一律往本文件放。

职责边界：本部件只做「显示 + 改设置」（改即保存），不做任何界面重排；
真正的显示 / 隐藏由主窗口 `pmw_on_setting_changed` 即时响应
（PmsSettingsManager 改动后广播 setting_changed 信号）。
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

from pms.constants import PMS_SORT_SCOPE_TITLES
from pms.gui.widgets import pms_make_hint_label

if TYPE_CHECKING:  # 仅类型检查用，避免与运行时模块循环导入
    from pms.settings import PmsSettingsManager


class PmsUiLayoutWidget(QWidget):
    """「设置 → 界面」页的界面类设置：面板可见性 + 节点表格排序范围 + 延迟列进度条粗细。

    对应关系（设置键 → 主窗口里的部件）：
        ui.show_quick_actions    → 左侧「快捷操作」区（v1.6.5 新增）
        ui.show_connection_panel → 底部「连接」页签
        ui.show_quick_stats      → 顶部速览条
        ui.show_status_traffic   → 状态栏流量标签
        ui.header_sort_scope     → 点表头排序是排全量还是只排当前页（v1.9.14）
        ui.latency_bar_height    → 延迟列进度条的粗细，0 = 不画（v1.9.23）
    """

    def __init__(self, pms_settings: "PmsSettingsManager", pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.puw_settings = pms_settings
        self.puw_build_ui()

    # ------------------------------------------------------------------ 界面
    def puw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        group = QGroupBox("面板可见性")
        form = QFormLayout(group)
        self.puw_chk_left_sidebar = self.puw_bind_bool(
            "ui.show_left_sidebar", QCheckBox("显示左侧边栏（分组 + 筛选 + 快捷操作）")
        )
        self.puw_chk_quick_actions = self.puw_bind_bool(
            "ui.show_quick_actions", QCheckBox("显示左侧「快捷操作」面板")
        )
        self.puw_chk_show_conn = self.puw_bind_bool(
            "ui.show_connection_panel", QCheckBox("显示底部「连接」面板")
        )
        self.puw_chk_show_stats = self.puw_bind_bool(
            "ui.show_quick_stats", QCheckBox("显示顶部速览条")
        )
        self.puw_chk_show_traffic = self.puw_bind_bool(
            "ui.show_status_traffic", QCheckBox("状态栏显示流量")
        )
        for widget in (self.puw_chk_left_sidebar, self.puw_chk_quick_actions, self.puw_chk_show_conn,
                       self.puw_chk_show_stats, self.puw_chk_show_traffic):
            form.addRow(widget)
        layout.addWidget(group)
        layout.addWidget(pms_make_hint_label(
            "关闭「左侧边栏」后，分组、筛选、快捷操作会一起隐藏，中间节点表自动占满宽度；"
            "关闭单个分区时仍可在设置里逐项控制。"
            "注意：快捷操作区被包含在左侧边栏内，关掉整个边栏后「显示左侧快捷操作面板」选项不再生效；"
            "该区里的「全部启用 / 全部停用 / 反转选择 / 按延迟排序 / 复制地址」在别处没有同名入口，"
            "需要时请重新打开本项；启用 / 停用 / 连接 / 测试仍可从节点右键菜单进入。"
        ))
        layout.addWidget(self.puw_build_sort_group())
        layout.addWidget(self.puw_build_latency_bar_group())
        layout.addStretch(1)

    def puw_build_sort_group(self) -> QWidget:
        """「节点表格排序」分组：点表头是排全量还是只排当前页（v1.9.14）。"""
        group = QGroupBox("节点表格排序")
        outer = QVBoxLayout(group)
        form = QFormLayout()
        self.puw_combo_header_sort = self.puw_bind_combo("ui.header_sort_scope", QComboBox(), PMS_SORT_SCOPE_TITLES)
        form.addRow("点表头排序范围", self.puw_combo_header_sort)
        outer.addLayout(form)
        outer.addWidget(pms_make_hint_label(
            "「全量（默认）」= 点表头对整个筛选结果排序，并回到第 1 页（延迟最低 / 速度最高的一批直接在第一页）；"
            "「当前页」= 只把当前这一页的行排好，不跳页、也不改动全量顺序。"
        ))
        return group

    def puw_build_latency_bar_group(self) -> QWidget:
        """「延迟列进度条」分组：延迟单元格里那根彩色条的粗细（v1.9.23）。"""
        group = QGroupBox("延迟列进度条")
        outer = QVBoxLayout(group)
        form = QFormLayout()
        self.puw_spin_bar_height = QSpinBox()
        self.puw_spin_bar_height.setRange(0, 14)
        self.puw_spin_bar_height.setSuffix(" px")
        self.puw_spin_bar_height.setSpecialValueText("不显示进度条")
        self.puw_bind_int("ui.latency_bar_height", self.puw_spin_bar_height)
        form.addRow("进度条粗细", self.puw_spin_bar_height)
        outer.addLayout(form)
        outer.addWidget(pms_make_hint_label(
            "延迟列里那根条：绿色=快、黄色=中等、红色=慢，条的**长度**表示快慢（越快越长）。"
            "这里调它的**粗细**（单位像素，1 = 最细）；填 0 或选「不显示进度条」就整根不画，只留文字。"
            "条贴着单元格下沿，行高调小时会自动跟着变矮，不会压住上面的文字。"
            "默认 5 px，比 v1.9.22 之前的约 9 px 细。"
        ))
        return group

    # ------------------------------------------------------------------ 绑定
    def puw_bind_int(self, pms_key: str, pms_widget: QSpinBox) -> QSpinBox:
        """数字框 ↔ 整数设置（双向，改动即保存）。"""
        pms_widget.setValue(self.puw_settings.psm_get_int(pms_key, pms_widget.value()))
        pms_widget.valueChanged.connect(lambda value, key=pms_key: self.puw_settings.psm_set(key, int(value)))
        return pms_widget

    def puw_bind_combo(self, pms_key: str, pms_widget: QComboBox, pms_titles: dict[str, str]) -> QComboBox:
        """下拉框 ↔ 枚举设置（双向，改动即保存）。"""
        for value, title in pms_titles.items():
            pms_widget.addItem(title, value)
        index = pms_widget.findData(self.puw_settings.psm_get_str(pms_key, ""))
        pms_widget.setCurrentIndex(max(0, index))
        pms_widget.currentIndexChanged.connect(
            lambda _index, key=pms_key, widget=pms_widget: self.puw_settings.psm_set(key, str(widget.currentData()))
        )
        return pms_widget

    def puw_bind_bool(self, pms_key: str, pms_widget: QCheckBox) -> QCheckBox:
        """复选框 ↔ 布尔设置（双向，改动即保存）。"""
        pms_widget.setChecked(self.puw_settings.psm_get_bool(pms_key, True))
        pms_widget.toggled.connect(lambda value, key=pms_key: self.puw_settings.psm_set(key, bool(value)))
        return pms_widget
