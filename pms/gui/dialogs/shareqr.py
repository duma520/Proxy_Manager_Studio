#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：分享二维码

展示分享链接二维码，支持复制与保存 PNG。
"""

from __future__ import annotations

from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pms.optional import PMS_HAS_QRCODE
from pms.icons import pms_qr_pixmap

# --------------------------------------------------------------------------------------
# 对话框：分享 / 二维码
# --------------------------------------------------------------------------------------
class PmsShareQrDialog(QDialog):
    """显示分享链接的二维码，并支持复制与保存图片。"""

    def __init__(self, pms_link: str, pms_title: str, pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.psqr_link = str(pms_link)
        self.setWindowTitle(f"分享二维码 - {pms_title}")
        self.setMinimumSize(420, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        self.psqr_image_label = QLabel()
        self.psqr_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.psqr_image_label.setMinimumHeight(340)
        pixmap = pms_qr_pixmap(self.psqr_link, 340)
        if pixmap is None:
            self.psqr_image_label.setText("未安装 qrcode 库，无法生成二维码（可复制下方链接）" if not PMS_HAS_QRCODE else "二维码生成失败")
        else:
            self.psqr_image_label.setPixmap(pixmap)
        root.addWidget(self.psqr_image_label, 1)
        self.psqr_text = QPlainTextEdit(self.psqr_link)
        self.psqr_text.setReadOnly(True)
        self.psqr_text.setMaximumHeight(120)
        root.addWidget(self.psqr_text)
        buttons = QHBoxLayout()
        copy_button = QPushButton("复制链接")
        copy_button.clicked.connect(self.psqr_copy)
        save_button = QPushButton("保存二维码图片")
        save_button.clicked.connect(self.psqr_save)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(copy_button)
        buttons.addWidget(save_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

    def psqr_copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.psqr_link)
        QMessageBox.information(self, "已复制", "分享链接已复制到剪贴板")

    def psqr_save(self) -> None:
        pixmap = self.psqr_image_label.pixmap()
        if pixmap is None:
            QMessageBox.warning(self, "无法保存", "没有可保存的二维码图片")
            return
        target, _ = QFileDialog.getSaveFileName(self, "保存二维码", "share_qr.png", "PNG 图片 (*.png)")
        if not target:
            return
        if pixmap.save(target, "PNG"):
            QMessageBox.information(self, "已保存", target)
        else:
            QMessageBox.warning(self, "保存失败", target)
