#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统工具对话框（第 14、15 项）：网络诊断 + 设置历史。

网络诊断在后台线程跑 ``pms_diagnostics`` 的 ``pms_quick_diagnostics``，避免阻塞主线程；
设置历史直接复用 ``PmsSettingsManager`` 的快照接口，支持查看 / 回滚 / 删除。
"""

from __future__ import annotations

from typing import Any, Optional
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from pms.diagnostics import pms_quick_diagnostics
from pms.settings import PmsSettingsManager


class _PmsDiagnosticsSignals(QObject):
    finished = Signal(list)


class _PmsDiagnosticsWorker(QRunnable):
    """后台线程跑诊断，结果通过信号回传（不阻塞界面）。"""

    def __init__(self, pms_test_url: str, pms_api_port: int, pms_timeout_ms: int) -> None:
        super().__init__()
        self._url = pms_test_url
        self._port = int(pms_api_port or 0)
        self._timeout = int(pms_timeout_ms)
        self.signals = _PmsDiagnosticsSignals()

    def run(self) -> None:  # noqa: D102
        try:
            results = pms_quick_diagnostics(self._url, self._port, self._timeout)
        except Exception as exc:  # noqa: BLE001
            results = [{
                "category": "error", "name": "诊断异常", "target": "",
                "ok": False, "detail": str(exc), "latency_ms": -1,
            }]
        self.signals.finished.emit(results)


class PmsDiagnosticsDialog(QDialog):
    """网络诊断对话框：一键检查 DNS / TCP / 直连 / 核心 API 端口连通性。"""

    def __init__(
        self,
        pms_settings: PmsSettingsManager,
        pms_core_api_port: int = 0,
        pms_parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pdw_settings = pms_settings
        self.pdw_api_port = int(pms_core_api_port or 0)
        self.pdw_pool = QThreadPool(self)
        self.setWindowTitle("网络诊断")
        self.setMinimumWidth(580)
        self._pdw_build_ui()

    def _pdw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.pdw_hint = QLabel("检查 DNS 解析、TCP 可达性、直连访问与核心 API 端口（不经过系统代理）。")
        self.pdw_hint.setWordWrap(True)
        layout.addWidget(self.pdw_hint)
        self.pdw_table = QTableWidget(0, 5)
        self.pdw_table.setHorizontalHeaderLabels(["项目", "类型", "目标", "结果", "延迟/详情"])
        header = self.pdw_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.pdw_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.pdw_table)
        self.pdw_run = QPushButton("开始诊断")
        self.pdw_run.clicked.connect(self.pdw_run_diagnostics)
        layout.addWidget(self.pdw_run)
        self.pdw_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.pdw_buttons.rejected.connect(self.reject)
        layout.addWidget(self.pdw_buttons)

    def pdw_run_diagnostics(self) -> None:
        self.pdw_run.setEnabled(False)
        self.pdw_hint.setText("诊断进行中…")
        url_list = self.pdw_settings.psm_get_list("test.url_list")
        test_url = url_list[0] if url_list else ""
        worker = _PmsDiagnosticsWorker(
            test_url,
            self.pdw_api_port,
            self.pdw_settings.psm_get_int("test.timeout_ms", 5000),
        )
        worker.signals.finished.connect(self._pdw_on_finished)
        self.pdw_pool.start(worker)

    @Slot(list)
    def _pdw_on_finished(self, pms_results: list[dict[str, Any]]) -> None:
        from PySide6.QtCore import Qt
        self.pdw_table.setRowCount(len(pms_results))
        for row, item in enumerate(pms_results):
            self.pdw_table.setItem(row, 0, QTableWidgetItem(str(item.get("name", ""))))
            self.pdw_table.setItem(row, 1, QTableWidgetItem(str(item.get("category", ""))))
            self.pdw_table.setItem(row, 2, QTableWidgetItem(str(item.get("target", ""))))
            ok = bool(item.get("ok"))
            result_item = QTableWidgetItem("✓ 正常" if ok else "✗ 异常")
            result_item.setForeground(Qt.GlobalColor.darkGreen if ok else Qt.GlobalColor.red)
            self.pdw_table.setItem(row, 3, result_item)
            detail = str(item.get("detail", ""))
            latency = int(item.get("latency_ms", -1))
            if latency >= 0:
                detail = f"{latency} ms | {detail}"
            self.pdw_table.setItem(row, 4, QTableWidgetItem(detail))
        self.pdw_run.setEnabled(True)
        ok_count = sum(1 for r in pms_results if r.get("ok"))
        self.pdw_hint.setText(f"诊断完成：{ok_count}/{len(pms_results)} 项正常")


class PmsSettingsHistoryDialog(QDialog):
    """设置历史快照对话框：查看 / 回滚 / 删除历史设置。"""

    def __init__(self, pms_settings: PmsSettingsManager, pms_parent: Optional[QObject] = None) -> None:
        super().__init__(pms_parent)
        self.pdh_settings = pms_settings
        self.setWindowTitle("设置历史")
        self.setMinimumWidth(580)
        self.pdh_snapshots: list[dict[str, Any]] = []
        self._pdh_build_ui()
        self.pdh_refresh()

    def _pdh_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.pdh_hint = QLabel(
            "按内容去重自动记录设置变更（启动时、关闭设置时、每次真正改动时各一条），"
            "回滚会把设置恢复到所选快照（当前状态会先生成一条新快照，随时能再换回来）。"
        )
        self.pdh_hint.setWordWrap(True)
        layout.addWidget(self.pdh_hint)
        keep_row = QHBoxLayout()
        keep_row.addWidget(QLabel("保留最近"))
        self.pdh_spin_keep = QSpinBox()
        self.pdh_spin_keep.setRange(5, 500)
        self.pdh_spin_keep.setSuffix(" 条")
        self.pdh_spin_keep.setValue(max(5, int(self.pdh_settings.psm_get_int("advanced.settings_history_max", 50))))
        self.pdh_spin_keep.setToolTip("超出条数后自动丢弃最旧的快照（v1.9.17 可调，改完立即生效）")
        self.pdh_spin_keep.valueChanged.connect(self._pdh_on_keep_changed)
        keep_row.addWidget(self.pdh_spin_keep)
        self.pdh_clear_all = QPushButton("清空历史")
        self.pdh_clear_all.clicked.connect(self._pdh_clear_all)
        keep_row.addWidget(self.pdh_clear_all)
        keep_row.addStretch(1)
        layout.addLayout(keep_row)
        self.pdh_table = QTableWidget(0, 4)
        self.pdh_table.setHorizontalHeaderLabels(["时间", "标签", "原因", "ID"])
        header = self.pdh_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.pdh_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pdh_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.pdh_table)
        self.pdh_restore = QPushButton("回滚到所选")
        self.pdh_restore.clicked.connect(self.pdh_do_restore)
        self.pdh_delete = QPushButton("删除所选")
        self.pdh_delete.clicked.connect(self.pdh_do_delete)
        self.pdh_refresh_btn = QPushButton("刷新")
        self.pdh_refresh_btn.clicked.connect(self.pdh_refresh)
        bar = QDialogButtonBox()
        bar.addButton(self.pdh_restore, QDialogButtonBox.ButtonRole.ActionRole)
        bar.addButton(self.pdh_delete, QDialogButtonBox.ButtonRole.ActionRole)
        bar.addButton(self.pdh_refresh_btn, QDialogButtonBox.ButtonRole.ActionRole)
        bar.addButton(QDialogButtonBox.StandardButton.Close)
        bar.rejected.connect(self.reject)
        layout.addWidget(bar)

    def pdh_refresh(self) -> None:
        self.pdh_snapshots = self.pdh_settings.psm_list_snapshots()
        self.pdh_table.setRowCount(len(self.pdh_snapshots))
        for row, snap in enumerate(self.pdh_snapshots):
            self.pdh_table.setItem(row, 0, QTableWidgetItem(str(snap.get("time", ""))))
            self.pdh_table.setItem(row, 1, QTableWidgetItem(str(snap.get("label", ""))))
            self.pdh_table.setItem(row, 2, QTableWidgetItem(str(snap.get("reason", ""))))
            self.pdh_table.setItem(row, 3, QTableWidgetItem(str(snap.get("id", ""))))

    def _pdh_on_keep_changed(self, pms_value: int) -> None:
        """改「保留条数」：写设置 + 立即裁掉多余的旧快照（v1.9.17）。"""
        self.pdh_settings.psm_set("advanced.settings_history_max", int(pms_value))
        self.pdh_settings.psm_history.psh_max = max(5, int(pms_value))
        self.pdh_settings.psm_history.psh_trim()
        self.pdh_refresh()

    def _pdh_clear_all(self) -> None:
        """清空全部快照（危险操作，需确认）。"""
        if QMessageBox.question(self, "确认清空", "确定清空全部设置历史吗？清空后无法再回退到旧版本。") != QMessageBox.StandardButton.Yes:
            return
        removed = self.pdh_settings.psm_history.psh_clear()
        QMessageBox.information(self, "已清空", f"已清空 {removed} 条设置快照。")
        self.pdh_refresh()

    def _pdh_selected_id(self) -> Optional[str]:
        row = self.pdh_table.currentRow()
        if row < 0 or row >= len(self.pdh_snapshots):
            return None
        return str(self.pdh_snapshots[row].get("id", ""))

    def pdh_do_restore(self) -> None:
        sid = self._pdh_selected_id()
        if not sid:
            QMessageBox.information(self, "提示", "请先选择一条快照。")
            return
        answer = QMessageBox.question(
            self, "确认回滚",
            "确定把设置恢复到所选快照吗？当前设置会生成一条新快照。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.pdh_settings.psm_restore_snapshot(sid):
            QMessageBox.information(self, "完成", "已回滚到所选快照。")
            self.pdh_refresh()
        else:
            QMessageBox.warning(self, "失败", "回滚失败：快照不存在或已损坏。")

    def pdh_do_delete(self) -> None:
        sid = self._pdh_selected_id()
        if not sid:
            QMessageBox.information(self, "提示", "请先选择一条快照。")
            return
        self.pdh_settings.psm_delete_snapshot(sid)
        self.pdh_refresh()
