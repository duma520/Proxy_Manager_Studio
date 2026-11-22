#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试后自动排序设置部件（v1.9.12 从 settingsdialog.py 迁出）

为什么单独成文件：`settingsdialog.py` 已顶到 1000 行红线（项目宪法第 6 条），
v1.9.12 要新增「延迟 / 测速 测试结束后自动排序（全量 / 当前页）」，
按 v1.6.5 立下的规矩放进独立部件。同一部件实例化两次（延迟页 / 测速页）互不干扰。

职责边界：本部件只做「显示 + 改设置」（改即保存）；真正的排序由主窗口执行——
延迟测试结束在 `pmw_on_latency_finished()`、测速结束在 `pmw_on_speed_finished()`，
两者都调 `pmw_apply_auto_sort("latency" / "speed")`，范围由本部件写下的设置决定。
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QVBoxLayout,
    QWidget,
)

from pms.constants import PMS_SORT_SCOPE_TITLES
from pms.gui.widgets import pms_make_hint_label

if TYPE_CHECKING:  # 仅类型检查用，避免与运行时模块循环导入
    from pms.settings import PmsSettingsManager


class PmsTestSortWidget(QWidget):
    """「测试后自动排序」分组：开关 + 范围（全量 / 当前页）。

    两份实例（同一个类，靠 pms_kind 区分）：
        延迟页 → pms_kind="latency"：键 `test.sort_after_test` / `test.sort_scope_latency`，延迟升序
        测速页 → pms_kind="speed"  ：键 `test.sort_after_speed` / `test.sort_scope_speed`，速度降序
    """

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_kind: str,
        pms_parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.ptsw_settings = pms_settings
        self.ptsw_kind = "speed" if str(pms_kind) == "speed" else "latency"
        self.ptsw_build_ui()

    # ------------------------------------------------------------------ 界面
    def ptsw_build_ui(self) -> None:
        if self.ptsw_kind == "speed":
            toggle_key, scope_key = "test.sort_after_speed", "test.sort_scope_speed"
            title = "测速后自动排序"
            toggle_text = "测速结束后自动按速度排序（速度高的排前面）"
            hint = (
                "范围「全量」= 对整个筛选结果按速度降序排一遍并自动回到第 1 页，最快的一批直接落在第一页；"
                "「当前页」= 只重排当前这一页，不跳页、不动全量顺序。"
            )
        else:
            toggle_key, scope_key = "test.sort_after_test", "test.sort_scope_latency"
            title = "延迟测试后自动排序"
            toggle_text = "延迟测试结束后自动按延迟排序（延迟低的排前面）"
            hint = (
                "范围「全量」= 对整个筛选结果按延迟升序排一遍并自动回到第 1 页，延迟最低的一批直接落在第一页；"
                "「当前页」= 只重排当前这一页，不跳页、不动全量顺序。"
            )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        group = QGroupBox(title)
        form = QFormLayout(group)
        self.ptsw_chk_sort = self.ptsw_bind_bool(toggle_key, QCheckBox(toggle_text))
        self.ptsw_combo_scope = self.ptsw_bind_combo(scope_key, QComboBox(), PMS_SORT_SCOPE_TITLES)
        # 没开自动排序时范围无意义，置灰（值照旧保存，下次打开开关即沿用）
        self.ptsw_combo_scope.setEnabled(self.ptsw_chk_sort.isChecked())
        self.ptsw_chk_sort.toggled.connect(self.ptsw_combo_scope.setEnabled)
        form.addRow(self.ptsw_chk_sort)
        form.addRow("排序范围", self.ptsw_combo_scope)
        layout.addWidget(group)
        layout.addWidget(pms_make_hint_label(hint))

    # ------------------------------------------------------------------ 绑定
    def ptsw_bind_bool(self, pms_key: str, pms_widget: QCheckBox) -> QCheckBox:
        """复选框 ↔ 布尔设置（双向，改动即保存）。"""
        pms_widget.setChecked(self.ptsw_settings.psm_get_bool(pms_key, False))
        pms_widget.toggled.connect(lambda value, key=pms_key: self.ptsw_settings.psm_set(key, bool(value)))
        return pms_widget

    def ptsw_bind_combo(self, pms_key: str, pms_widget: QComboBox, pms_titles: dict[str, str]) -> QComboBox:
        """下拉框 ↔ 枚举设置（双向，改动即保存）。"""
        for value, title in pms_titles.items():
            pms_widget.addItem(title, value)
        index = pms_widget.findData(self.ptsw_settings.psm_get_str(pms_key, ""))
        pms_widget.setCurrentIndex(max(0, index))
        pms_widget.currentIndexChanged.connect(
            lambda _index, key=pms_key, widget=pms_widget: self.ptsw_settings.psm_set(key, str(widget.currentData()))
        )
        return pms_widget
