#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：版本记录

查看《软件升级迭代记录.md》与 version\ 下的历史备份。
"""

from __future__ import annotations

from typing import Optional
from PySide6.QtCore import (
    Qt,
    Signal,
)
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pms.constants import PMS_APP_DOC_NAME
from pms.utils import (
    pms_format_bytes,
    pms_read_text_smart,
)
from pms.paths import PmsRuntimePaths
from pms.versioning import PmsVersionBackupHelper

# --------------------------------------------------------------------------------------
# 对话框：版本迭代记录
# --------------------------------------------------------------------------------------
class PmsVersionHistoryDialog(QDialog):
    """查看 软件升级迭代记录.md 与历史版本备份。"""

    pvhd_action = Signal(str, dict)

    def __init__(self, pms_paths: PmsRuntimePaths, pms_backup: PmsVersionBackupHelper, pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.pvhd_paths = pms_paths
        self.pvhd_backup = pms_backup
        self.setWindowTitle("软件升级迭代记录 / 版本备份")
        self.setMinimumSize(880, 640)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)
        self.pvhd_text = QPlainTextEdit()
        self.pvhd_text.setReadOnly(True)
        font = QFont("Consolas")
        font.setPointSizeF(9.0)
        self.pvhd_text.setFont(font)
        splitter.addWidget(self.pvhd_text)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("历史版本备份（version\\）"))
        self.pvhd_table = QTableWidget(0, 3)
        self.pvhd_table.setHorizontalHeaderLabels(["文件", "大小", "时间"])
        self.pvhd_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.pvhd_table.verticalHeader().setVisible(False)
        right_layout.addWidget(self.pvhd_table, 1)
        splitter.addWidget(right)
        splitter.setSizes([600, 280])
        buttons = QHBoxLayout()
        reload_button = QPushButton("刷新")
        reload_button.clicked.connect(self.pvhd_reload)
        backup_button = QPushButton("备份当前版本")
        backup_button.setObjectName("primaryButton")
        backup_button.clicked.connect(lambda: self.pvhd_action.emit("backup_version", {"note": "版本记录页手动备份"}))
        open_doc_button = QPushButton("用系统程序打开文档")
        open_doc_button.clicked.connect(lambda: self.pvhd_action.emit("open_document", {}))
        open_dir_button = QPushButton("打开 version 目录")
        open_dir_button.clicked.connect(lambda: self.pvhd_action.emit("open_folder", {"path": str(self.pvhd_paths.prt_version_dir)}))
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        for widget in (reload_button, backup_button, open_doc_button, open_dir_button):
            buttons.addWidget(widget)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        root.addLayout(buttons)
        self.pvhd_reload()

    def pvhd_reload(self) -> None:
        """重新读取文档与备份列表。"""
        doc = self.pvhd_paths.prt_doc_file
        if doc.is_file():
            self.pvhd_text.setPlainText(pms_read_text_smart(doc))
        else:
            self.pvhd_text.setPlainText(f"未找到文档：{doc}\n\n请在项目根目录创建 {PMS_APP_DOC_NAME} 并记录迭代内容。")
        backups = self.pvhd_backup.pvbh_list_backups()
        self.pvhd_table.setRowCount(len(backups))
        for row, (name, size, mtime) in enumerate(reversed(backups)):
            self.pvhd_table.setItem(row, 0, QTableWidgetItem(name))
            self.pvhd_table.setItem(row, 1, QTableWidgetItem(pms_format_bytes(size)))
            self.pvhd_table.setItem(row, 2, QTableWidgetItem(mtime))
