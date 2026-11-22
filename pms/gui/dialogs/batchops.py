#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：批量操作

对选中节点批量启用/停用/分组/重命名/去重/排序/测试/删除。
"""

from __future__ import annotations

from typing import (
    Optional,
    Sequence,
)
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from pms.models import PmsProxyNode
from pms.gui.widgets import pms_make_hint_label

# --------------------------------------------------------------------------------------
# 对话框：批量操作
# --------------------------------------------------------------------------------------
class PmsBatchOperationDialog(QDialog):
    """对选中节点批量操作：启用/停用、移动分组、重命名、去重、删除、测试。"""

    pbod_operation_requested = Signal(str, dict)

    def __init__(self, pms_selected: Sequence[PmsProxyNode], pms_groups: Sequence[str], pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.pbod_selected = list(pms_selected)
        self.pbod_groups = list(pms_groups)
        self.setWindowTitle("批量操作")
        self.setMinimumWidth(560)
        self.pbod_build_ui()

    def pbod_build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(pms_make_hint_label(f"已选中 {len(self.pbod_selected)} 个节点。操作会立即写入数据库并刷新列表。"))
        form = QFormLayout()
        self.pbod_operation_combo = QComboBox()
        operations = [
            ("启用节点", "enable"),
            ("停用节点", "disable"),
            ("移动到分组", "move_group"),
            ("重命名（加前缀/后缀）", "rename"),
            ("按地址去重（保留延迟最低）", "dedupe"),
            ("排序（按延迟从快到慢）", "sort_latency"),
            ("测试延迟", "test_latency"),
            ("测试延迟 + 并发测速", "test_latency_speed"),
            ("测速", "speed"),
            ("删除节点", "delete"),
        ]
        for title, key in operations:
            self.pbod_operation_combo.addItem(title, key)
        self.pbod_operation_combo.currentIndexChanged.connect(self.pbod_update_visibility)
        form.addRow("操作", self.pbod_operation_combo)
        self.pbod_group_combo = QComboBox()
        self.pbod_group_combo.setEditable(True)
        self.pbod_group_combo.addItems(self.pbod_groups or ["默认分组"])
        form.addRow("目标分组", self.pbod_group_combo)
        self.pbod_prefix_edit = QLineEdit()
        self.pbod_suffix_edit = QLineEdit()
        form.addRow("名称前缀", self.pbod_prefix_edit)
        form.addRow("名称后缀", self.pbod_suffix_edit)
        self.pbod_confirm_check = QCheckBox("我确认执行该操作（删除不可恢复）")
        form.addRow("", self.pbod_confirm_check)
        root.addLayout(form)
        root.addWidget(pms_make_hint_label("提示：延迟/测速类操作会调用真实测试流程，可在主界面看到进度与结果。"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.pbod_ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.pbod_ok_button.setText("执行")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.pbod_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.pbod_update_visibility()

    def pbod_update_visibility(self) -> None:
        operation = str(self.pbod_operation_combo.currentData())
        self.pbod_group_combo.setEnabled(operation == "move_group")
        self.pbod_prefix_edit.setEnabled(operation == "rename")
        self.pbod_suffix_edit.setEnabled(operation == "rename")
        self.pbod_confirm_check.setEnabled(operation == "delete")

    def pbod_accept(self) -> None:
        operation = str(self.pbod_operation_combo.currentData())
        if operation == "delete" and not self.pbod_confirm_check.isChecked():
            QMessageBox.warning(self, "需要确认", "删除操作不可恢复，请先勾选确认框。")
            return
        params = {
            "group": self.pbod_group_combo.currentText().strip(),
            "prefix": self.pbod_prefix_edit.text(),
            "suffix": self.pbod_suffix_edit.text(),
        }
        self.pbod_operation_requested.emit(operation, params)
        self.accept()
