#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：选择测试（v1.9.65）

把原来散在右键「测试 ▸」与顶部「测试 ▾」里的十来个测试入口收敛成一个弹窗：
先选**范围**（选中节点 / 本页 / 全部筛选结果 / 续测），再勾**测试项**（延迟 / 测速 / 电报，
可多选），按勾选顺序串行开跑。用户 2026-10-15 需求：「改成直接弹窗，然后选择 是本页还是全部
还是续测……，然后测哪些项目（延迟，测速，电报）可以多选，然后再按勾选的去测」。
"""

from __future__ import annotations

from typing import (
    Callable,
    Optional,
    Sequence,
)

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)


class PmsTestPickerDialog(QDialog):
    """选择测试弹窗：范围单选 + 测试项多选 + 仅测新增勾选。

    主窗口只负责接线（`pmw_open_test_picker()` 开窗、`pmw_on_test_picker_run()` 接结果），
    这里不碰任何测试逻辑——确认后通过 `tpd_run_requested` 把 (scope, kinds, only_untested)
    交出去，由主窗口按既有 `pmw_run_latency_test` / `pmw_run_speed_test` /
    `pmw_run_telegram_test` 串行跑（复用 v1.9.61 的链机制）。
    """

    # scope ∈ selected / page / all / resume；kinds 保持用户勾选顺序（延迟→测速→电报）
    tpd_run_requested = Signal(str, list, bool)

    def __init__(self, pms_selected_count: int, pms_page_count: int, pms_all_count: int,
                 pms_only_untested: bool, pms_has_pending: bool, pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.tpd_selected_count = max(0, int(pms_selected_count or 0))
        self.tpd_page_count = max(0, int(pms_page_count or 0))
        self.tpd_all_count = max(0, int(pms_all_count or 0))
        self.tpd_only_untested = bool(pms_only_untested)
        self.tpd_has_pending = bool(pms_has_pending)
        self.setWindowTitle("选择测试")
        self.setMinimumWidth(520)
        self.tpd_build_ui()

    # ------------------------------------------------------------------ 界面
    def tpd_build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(QLabel("先选范围，再勾要测的项目（可多选），按勾选顺序依次开跑。"))

        # 范围（单选）：条数实时写在标题上，置灰项说明为什么不能选
        scope_box = QGroupBox("测试范围")
        scope_layout = QVBoxLayout(scope_box)
        self.tpd_scope_group = QButtonGroup(self)
        self.tpd_scope_group.setExclusive(True)
        for key, title, count in (
            ("selected", "选中节点", self.tpd_selected_count),
            ("page", "本页", self.tpd_page_count),
            ("all", "全部筛选结果", self.tpd_all_count),
        ):
            button = QRadioButton(f"{title}（{count} 个）")
            button.setProperty("pms_scope", key)
            self.tpd_scope_group.addButton(button)
            scope_layout.addWidget(button)
            if key == "selected" and self.tpd_selected_count <= 0:
                button.setEnabled(False)
                button.setToolTip("没有选中节点（在节点表里选中几行再来）")
        self.tpd_resume_radio = QRadioButton("续测（接着上次没测完的跑）")
        self.tpd_resume_radio.setProperty("pms_scope", "resume")
        self.tpd_scope_group.addButton(self.tpd_resume_radio)
        scope_layout.addWidget(self.tpd_resume_radio)
        if not self.tpd_has_pending:
            self.tpd_resume_radio.setEnabled(False)
            self.tpd_resume_radio.setToolTip("没有未完成的测试断点（测试中断后才会出现）")
        # 默认选中：有选中节点 → 选中；否则本页；没有节点可测时也只能落在续测上
        default_scope = "selected" if self.tpd_selected_count > 0 else ("page" if self.tpd_page_count > 0 else "resume")
        for button in self.tpd_scope_group.buttons():
            if str(button.property("pms_scope")) == default_scope and button.isEnabled():
                button.setChecked(True)
                break
        else:
            self.tpd_resume_radio.setChecked(True)
        root.addWidget(scope_box)

        # 测试项（多选）：顺序固定 延迟 → 测速 → 电报
        items_box = QGroupBox("测试项目（可多选）")
        items_layout = QVBoxLayout(items_box)
        self.tpd_item_checks: dict[str, QCheckBox] = {}
        for key, title in (("latency", "延迟"), ("speed", "测速"), ("telegram", "电报")):
            check = QCheckBox(title)
            check.setProperty("pms_kind", key)
            check.setChecked(key in ("latency", "speed"))
            self.tpd_item_checks[key] = check
            items_layout.addWidget(check)
        root.addWidget(items_box)

        self.tpd_untested_check = QCheckBox("仅测新增（延迟 / 测速 / 电报任一没测过才算）")
        self.tpd_untested_check.setChecked(self.tpd_only_untested)
        self.tpd_untested_check.setToolTip("勾上后，本次所选范围里已经测过的节点会被跳过")
        root.addWidget(self.tpd_untested_check)

        self.tpd_hint_label = QLabel("")
        self.tpd_hint_label.setObjectName("hintLabel")
        self.tpd_hint_label.setWordWrap(True)
        root.addWidget(self.tpd_hint_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.tpd_ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.tpd_ok_button.setText("开始测试")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.tpd_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.tpd_scope_group.buttonToggled.connect(lambda *_: self.tpd_update_visibility())
        for check in self.tpd_item_checks.values():
            check.toggled.connect(lambda *_: self.tpd_update_visibility())
        self.tpd_update_visibility()

    # ------------------------------------------------------------------ 状态
    def tpd_current_scope(self) -> str:
        button = self.tpd_scope_group.checkedButton()
        return str(button.property("pms_scope")) if button is not None else ""

    def tpd_checked_kinds(self) -> list[str]:
        """按固定顺序返回勾中的测试项（用户勾选顺序不改变执行顺序，口径与链一致）。"""
        return [key for key in ("latency", "speed", "telegram") if self.tpd_item_checks[key].isChecked()]

    def tpd_update_visibility(self) -> None:
        resume = self.tpd_current_scope() == "resume"
        # 续测沿用断点里记录的测试项，勾选无意义 → 置灰
        for check in self.tpd_item_checks.values():
            check.setEnabled(not resume)
        self.tpd_untested_check.setEnabled(not resume)
        if resume:
            self.tpd_hint_label.setText("续测会按断点里记录的范围与测试项接着跑，已测过的不重测。")
        elif not self.tpd_checked_kinds():
            self.tpd_hint_label.setText("至少勾一个测试项目（延迟 / 测速 / 电报）。")
        else:
            names = "、".join(self.tpd_item_checks[key].text() for key in self.tpd_checked_kinds())
            self.tpd_hint_label.setText(f"将依次测试：{names}。多个项目串行跑，中途停止不会自动续下一项。")
        self.tpd_ok_button.setEnabled(bool(self.tpd_checked_kinds()) if not resume else True)

    def tpd_accept(self) -> None:
        scope = self.tpd_current_scope()
        if not scope:
            return
        if scope == "resume":
            self.tpd_run_requested.emit("resume", [], False)
        else:
            kinds = self.tpd_checked_kinds()
            if not kinds:
                return
            self.tpd_run_requested.emit(scope, kinds, self.tpd_untested_check.isChecked())
        self.accept()
