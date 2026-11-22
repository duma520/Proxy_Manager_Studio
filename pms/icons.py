#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图标与二维码位图

加载 icon.ico（不存在则无图标）、内置绘制托盘图标、生成二维码位图。
"""

from __future__ import annotations

from typing import Optional
from PySide6.QtCore import (
    QPoint,
    Qt,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from pms.optional import (
    PMS_HAS_QRCODE,
    pms_optional_qrcode,
)
from pms.paths import PmsRuntimePaths

# --------------------------------------------------------------------------------------
# 图标与二维码辅助
# --------------------------------------------------------------------------------------
def pms_load_app_icon(pms_paths: PmsRuntimePaths) -> QIcon:
    """加载 icon.ico：存在就用，不存在返回空图标（界面保持无图标，绝不报错）。"""
    icon_file = pms_paths.prt_icon_file
    if pms_paths.prt_icon_exists():
        icon = QIcon(str(icon_file))
        if not icon.isNull():
            return icon
    return QIcon()

def pms_build_fallback_icon() -> QIcon:
    """内置绘制图标（无 icon.ico 时托盘用，避免托盘空白）。"""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(2, 2, size - 4, size - 4, 14, 14)
    painter.fillPath(path, QColor("#2f6fdb"))
    painter.setPen(QPen(QColor("#ffffff"), 4))
    painter.drawEllipse(QPoint(size // 2, 18), 5, 5)
    painter.drawEllipse(QPoint(18, size - 20), 5, 5)
    painter.drawEllipse(QPoint(size - 18, size - 20), 5, 5)
    painter.drawLine(QPoint(size // 2, 22), QPoint(20, size - 23))
    painter.drawLine(QPoint(size // 2, 22), QPoint(size - 20, size - 23))
    painter.drawLine(QPoint(22, size - 20), QPoint(size - 22, size - 20))
    painter.end()
    return QIcon(pixmap)

def pms_qr_pixmap(pms_text: str, pms_size: int = 320) -> Optional[QPixmap]:
    """用 qrcode 生成二维码位图（不依赖 Pillow：直接读矩阵画点）。"""
    if not PMS_HAS_QRCODE or pms_optional_qrcode is None:
        return None
    try:
        code = pms_optional_qrcode.QRCode(border=2, box_size=1, error_correction=pms_optional_qrcode.constants.ERROR_CORRECT_M)
        code.add_data(str(pms_text))
        code.make(fit=True)
        matrix = code.get_matrix()
    except Exception:  # noqa: BLE001 - 生成失败时优雅降级
        return None
    rows = len(matrix)
    columns = len(matrix[0]) if rows else 0
    if not rows or not columns:
        return None
    image = QImage(columns, rows, QImage.Format.Format_Mono)
    image.fill(1)
    for y in range(rows):
        for x in range(columns):
            if matrix[y][x]:
                image.setPixel(x, y, 0)
    return QPixmap.fromImage(image.scaled(pms_size, pms_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
