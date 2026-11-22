#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志 / 临时文件自动清理部件（v1.9.33）

用户要求：「增加对 日志 / 临时文件自动清理 7 天。」

本部件挂在「设置 → 高级」页（与「日志文件大小上限」同一页，语义最近），提供：
    - 「启动时自动清理」开关 + 保留天数（默认 7 天）；
    - 「预览可清理项」：**只看不删**，列出当前会被清理的文件（类别 / 名称 / 体积 / 修改时间）；
    - 「立即清理」：后台线程真删，跑完报「删了几个 / 释放多少」；
    - 说明文字写清**清哪些、不清哪些**（`version\\` 备份与 `%TEMP%` 不在范围内——用户拍板的范围）。

为什么单独成文件：`settingsdialog.py` 顶在 1000 行红线（项目第 8 条），控件一律拆部件
（同 `coreupdatewidget.py` / `coreversionwidget.py`）；设置对话框只调一行私有工厂 `_pmw_build()`。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from PySide6.QtCore import QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pms.maintenance import (
    PMS_MAINTENANCE_DEFAULT_DAYS,
    PMS_MAINTENANCE_MAX_DAYS,
    PMS_MAINTENANCE_MIN_DAYS,
    PmsMaintenanceSignals,
    PmsMaintenanceTask,
    pms_maintenance_days,
    pms_maintenance_plan,
    pms_maintenance_size_text,
    pms_maintenance_summary_text,
)

if TYPE_CHECKING:  # 仅类型检查用，避免与路径 / 设置模块循环导入
    from pms.paths import PmsRuntimePaths
    from pms.settings import PmsSettingsManager


class PmsMaintenanceWidget(QWidget):
    """日志 / 临时文件自动清理：开关、保留天数、预览、立即清理。"""

    pmw_clean_requested = Signal()

    PMW_COLUMNS = ("类别", "文件", "体积", "修改时间")

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_paths: "PmsRuntimePaths",
        pms_parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pmw_settings = pms_settings
        self.pmw_paths = pms_paths
        self.pmw_running = False
        self.pmw_signals = PmsMaintenanceSignals(self)
        self.pmw_signals.pmn_finished.connect(self.pmw_on_finished)
        self.pmw_build_ui()
        self.pmw_bind()
        self.pmw_refresh_plan()

    # ------------------------------------------------------------------ 界面
    def pmw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        row = QHBoxLayout()
        self.pmw_chk_auto = QCheckBox("启动时自动清理过期的日志与临时文件")
        row.addWidget(self.pmw_chk_auto)
        row.addWidget(QLabel("保留"))
        self.pmw_spin_days = QSpinBox()
        self.pmw_spin_days.setRange(PMS_MAINTENANCE_MIN_DAYS, PMS_MAINTENANCE_MAX_DAYS)
        self.pmw_spin_days.setSuffix(" 天")
        row.addWidget(self.pmw_spin_days)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QHBoxLayout()
        self.pmw_btn_preview = QPushButton("预览可清理项")
        self.pmw_btn_preview.setToolTip("只看不删：列出按当前保留天数会被清理的文件")
        self.pmw_btn_preview.clicked.connect(self.pmw_refresh_plan)
        buttons.addWidget(self.pmw_btn_preview)
        self.pmw_btn_clean = QPushButton("立即清理")
        self.pmw_btn_clean.setToolTip("后台线程真删（当前正在写的日志、数据库、核心、版本备份都不在范围内）")
        self.pmw_btn_clean.clicked.connect(self.pmw_on_clean)
        buttons.addWidget(self.pmw_btn_clean)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.pmw_table = QTableWidget(0, len(self.PMW_COLUMNS))
        self.pmw_table.setHorizontalHeaderLabels(list(self.PMW_COLUMNS))
        self.pmw_table.verticalHeader().setVisible(False)
        self.pmw_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pmw_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pmw_table.setMaximumHeight(170)
        header = self.pmw_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.pmw_table)

        self.pmw_note = QLabel("")
        self.pmw_note.setObjectName("hintLabel")
        self.pmw_note.setWordWrap(True)
        layout.addWidget(self.pmw_note)

    def pmw_bind(self) -> None:
        self.pmw_chk_auto.setChecked(bool(self.pmw_settings.psm_get_bool("maintenance.auto_clean_enabled", True)))
        self.pmw_spin_days.setValue(int(self.pmw_settings.psm_get_int("maintenance.retention_days", PMS_MAINTENANCE_DEFAULT_DAYS)))
        self.pmw_chk_auto.toggled.connect(lambda value: self.pmw_on_changed("maintenance.auto_clean_enabled", bool(value)))
        self.pmw_spin_days.valueChanged.connect(self.pmw_on_days_changed)

    def pmw_on_changed(self, pms_key: str, pms_value: object) -> None:
        self.pmw_settings.psm_set(pms_key, pms_value)

    def pmw_on_days_changed(self, pms_value: int) -> None:
        self.pmw_settings.psm_set("maintenance.retention_days", int(pms_value))
        self.pmw_refresh_plan()

    def pmw_current_days(self) -> int:
        return pms_maintenance_days(self.pmw_spin_days.value())

    # ------------------------------------------------------------------ 预览 / 清理
    @Slot()
    def pmw_refresh_plan(self) -> None:
        """列一遍「现在清理会删什么」（只读扫描，通常几毫秒）。"""
        items = pms_maintenance_plan(self.pmw_paths, self.pmw_current_days())
        self.pmw_table.setRowCount(len(items))
        for row, item in enumerate(items):
            path = Path(item["path"])
            stamp = datetime.fromtimestamp(item["modified"]).strftime("%Y-%m-%d %H:%M")
            for column, text in enumerate((
                str(item["category"]), path.name, pms_maintenance_size_text(item["size"]), stamp,
            )):
                cell = self.pmw_table.item(row, column)
                if cell is None:
                    self.pmw_table.setItem(row, column, QTableWidgetItem(text))
                else:
                    cell.setText(text)
        total = sum(int(item["size"]) for item in items)
        scope = "已开启启动时自动清理" if self.pmw_chk_auto.isChecked() else "启动时自动清理已关闭（只能手动点「立即清理」）"
        self.pmw_note.setText(
            f"按当前保留 {self.pmw_current_days()} 天：{len(items)} 个文件可清理，合计 {pms_maintenance_size_text(total)}。{scope}。"
            "范围：日志轮转件、logs 里的旧件、data\\temp、data\\configs 里的旧核心配置、根目录的旧 .bak / .broken.json；"
            "**不动**正在写的日志、数据库、核心、version\\ 版本备份。"
        )
        self.pmw_table.setVisible(True)

    @Slot()
    def pmw_on_clean(self) -> None:
        if self.pmw_running:
            return
        self.pmw_running = True
        self.pmw_btn_clean.setEnabled(False)
        self.pmw_btn_preview.setEnabled(False)
        self.pmw_note.setText("正在清理…")
        QThreadPool.globalInstance().start(
            PmsMaintenanceTask(self.pmw_signals, self.pmw_paths, self.pmw_current_days())
        )

    @Slot(dict)
    def pmw_on_finished(self, pms_result: dict) -> None:
        self.pmw_running = False
        self.pmw_btn_clean.setEnabled(True)
        self.pmw_btn_preview.setEnabled(True)
        self.pmw_refresh_plan()
        self.pmw_note.setText(pms_maintenance_summary_text(pms_result) + "（刚跑完手动清理）")

    # ------------------------------------------------------------------ 事件
    def showEvent(self, pms_event) -> None:  # noqa: N802, D102
        super().showEvent(pms_event)
        if self.pmw_table.rowCount() == 0:
            self.pmw_refresh_plan()


def _pmw_build(pms_dialog) -> "PmsMaintenanceWidget":
    """把「日志与临时文件清理」挂到设置对话框上（对话框只调这一行）。

    做成模块私有是为了**不动启动器的重导出清单**（启动器余量本来就少）。
    """
    return PmsMaintenanceWidget(pms_dialog.pset_settings, pms_dialog.pset_paths, pms_dialog)
