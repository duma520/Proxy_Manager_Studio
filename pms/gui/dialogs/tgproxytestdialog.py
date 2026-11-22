#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：电报代理连通直测（v1.9.68）

**为什么单独写这个对话框**：
`t.me/proxy`（MTProto）代理不能当通用出口（Xray/sing-box 无 MTProto 出站），也走不了
节点表里的延迟 / 测速 / 电报 DC 测试（那些都经临时核心隧道）。它的价值**只有一条**：
「直连代理本身、用 MTProto 混淆握手看它能不能把流量送进电报 DC」。这个弹窗就是
让用户把一批 `t.me/proxy` 链接粘进来，逐条原生 socket 探测、把结果列出来的专用入口。

**口径与 dcprobe 一致，绝不编造**：
能拿到 resPQ → 可连；TCP 都不通 → 代理不可达；握手/超时无 resPQ → 代理拒绝或不是 MTProxy。
真连通只能在用户本机验证（沙箱拉不到 t.me，也连不到外网代理）。

**符合宪法**：PySide6 + 中文界面 + QThread 不阻塞主线程 + 进度条 + 自适应布局。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pms.constants import PMS_LOGGER_NAME
from pms.tgproxy import pms_tg_probe_from_link


class _PmsTgProxyProbeThread(QThread):
    """后台逐条探测线程（不阻塞 UI）。

    每条探测完通过 `ptp_one_done` 回传 (index, link, report)，全部跑完发 `ptp_all_done`。
    """

    ptp_one_done = Signal(int, str, dict)
    ptp_all_done = Signal(int, int)  # ok_count, total_count

    def __init__(self, pms_links: list[str], pms_timeout: float, pms_dc_id: int = 2,
                 pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.ptp_links = [str(x or "").strip() for x in pms_links if str(x or "").strip()]
        self.ptp_timeout = float(pms_timeout)
        self.ptp_dc_id = int(pms_dc_id)
        self.ptp_stop = False
        self.ptp_logger = logging.getLogger(PMS_LOGGER_NAME + ".tgproxy.dialog")

    def run(self) -> None:  # noqa: PLR0912, PLR0915
        ok_count = 0
        total = len(self.ptp_links)
        for index, link in enumerate(self.ptp_links):
            if self.ptp_stop:
                break
            try:
                report = pms_tg_probe_from_link(link, self.ptp_timeout, self.ptp_dc_id, self.ptp_logger)
            except Exception as exc:  # noqa: BLE001
                report = {"ok": False, "detail": f"探测异常：{exc}", "tcp_ms": 0, "form": "", "domain": ""}
            if report.get("ok"):
                ok_count += 1
            self.ptp_one_done.emit(index, link, report)
        self.ptp_all_done.emit(ok_count, total)

    def ptp_cancel(self) -> None:
        self.ptp_stop = True


class PmsTgProxyTestDialog(QDialog):
    """电报代理连通直测弹窗：粘链接 → 逐条探测 → 结果表 + 进度条。

    用法：主窗口调 `PmsTgProxyTestDialog(pms_parent=self).exec()` 即可，无需信号回传
    （探测结果只展示，不落库节点——MTProto 代理不进路由）。
    """

    def __init__(self, pms_parent: Optional[QWidget] = None, pms_initial_links: str = "") -> None:
        super().__init__(pms_parent)
        self.setWindowTitle("电报代理连通直测")
        self.setMinimumSize(720, 520)
        self.ptp_thread: Optional[_PmsTgProxyProbeThread] = None
        self.ptp_build_ui()
        if pms_initial_links:
            self.ptp_links_edit.setPlainText(str(pms_initial_links))

    # ------------------------------------------------------------------ 界面
    def ptp_build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        root.addWidget(QLabel(
            "把 t.me/proxy 链接粘在下面（一行一个），点「开始探测」逐条直连代理本身做 "
            "MTProto 握手。能拿到 resPQ 即证明代理在线且能连通电报 DC。\n"
            "（MTProto 代理不能当通用出口，这里只测电报连通性，不落库节点。）"
        ))

        self.ptp_links_edit = QPlainTextEdit()
        self.ptp_links_edit.setPlaceholderText(
            "https://t.me/proxy?server=1.2.3.4&port=443&secret=...\n"
            "https://t.me/proxy?server=example.com&port=443&secret=..."
        )
        self.ptp_links_edit.setMinimumHeight(100)
        root.addWidget(self.ptp_links_edit)

        # 参数行：超时 + DC + 开始 / 停止
        param_row = QHBoxLayout()
        param_row.addWidget(QLabel("超时(秒)："))
        self.ptp_timeout_spin = QSpinBox()
        self.ptp_timeout_spin.setRange(2, 30)
        self.ptp_timeout_spin.setValue(8)
        param_row.addWidget(self.ptp_timeout_spin)
        param_row.addSpacing(12)
        param_row.addWidget(QLabel("DC："))
        self.ptp_dc_combo = QComboBox()
        for dc, label in ((2, "DC2 (默认)"), (1, "DC1"), (3, "DC3"), (4, "DC4"), (5, "DC5")):
            self.ptp_dc_combo.addItem(label, dc)
        param_row.addWidget(self.ptp_dc_combo)
        param_row.addStretch()
        self.ptp_start_btn = QPushButton("开始探测")
        self.ptp_start_btn.clicked.connect(self.ptp_start)
        self.ptp_stop_btn = QPushButton("停止")
        self.ptp_stop_btn.setEnabled(False)
        self.ptp_stop_btn.clicked.connect(self.ptp_stop)
        param_row.addWidget(self.ptp_start_btn)
        param_row.addWidget(self.ptp_stop_btn)
        root.addLayout(param_row)

        # 进度条
        self.ptp_progress = QProgressBar()
        self.ptp_progress.setRange(0, 100)
        self.ptp_progress.setValue(0)
        self.ptp_progress.setTextVisible(True)
        self.ptp_progress.setFormat("待开始")
        root.addWidget(self.ptp_progress)

        # 结果表
        self.ptp_table = QTableWidget(0, 6)
        self.ptp_table.setHorizontalHeaderLabels(["#", "服务器", "端口", "形态", "TCP(ms)", "结果"])
        self.ptp_table.verticalHeader().setVisible(False)
        self.ptp_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ptp_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.ptp_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.ptp_table, 1)

        # 底部按钮
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------ 探测
    def ptp_links(self) -> list[str]:
        text = self.ptp_links_edit.toPlainText()
        return [line.strip() for line in text.splitlines() if line.strip()]

    def ptp_start(self) -> None:
        links = self.ptp_links()
        if not links:
            self.ptp_progress.setFormat("请先粘入至少一条链接")
            return
        self.ptp_table.setRowCount(0)
        self.ptp_progress.setRange(0, len(links))
        self.ptp_progress.setValue(0)
        self.ptp_progress.setFormat("0 / %d" % len(links))
        self.ptp_start_btn.setEnabled(False)
        self.ptp_stop_btn.setEnabled(True)
        self.ptp_links_edit.setReadOnly(True)
        self.ptp_thread = _PmsTgProxyProbeThread(
            links, float(self.ptp_timeout_spin.value()), int(self.ptp_dc_combo.currentData() or 2), self)
        self.ptp_thread.ptp_one_done.connect(self.ptp_on_one_done)
        self.ptp_thread.ptp_all_done.connect(self.ptp_on_all_done)
        self.ptp_thread.start()

    def ptp_stop(self) -> None:
        if self.ptp_thread is not None:
            self.ptp_thread.ptp_cancel()

    def ptp_on_one_done(self, pms_index: int, pms_link: str, pms_report: dict) -> None:
        row = self.ptp_table.rowCount()
        self.ptp_table.insertRow(row)
        ok = bool(pms_report.get("ok"))
        server = str(pms_report.get("server") or "")
        port = str(pms_report.get("port") or "")
        form = str(pms_report.get("form") or "")
        tcp_ms = str(pms_report.get("tcp_ms") or "")
        detail = str(pms_report.get("detail") or "")
        items = (
            str(pms_index + 1),
            server,
            port,
            form,
            tcp_ms,
            ("✅ " if ok else "❌ ") + detail,
        )
        for col, text in enumerate(items):
            cell = QTableWidgetItem(text)
            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if col == 5:
                cell.setForeground(Qt.GlobalColor.darkGreen if ok else Qt.GlobalColor.red)
            self.ptp_table.setItem(row, col, cell)
        self.ptp_progress.setValue(row + 1)
        self.ptp_progress.setFormat(f"{row + 1} / {self.ptp_progress.maximum()}")

    def ptp_on_all_done(self, pms_ok: int, pms_total: int) -> None:
        self.ptp_progress.setFormat(f"完成：{pms_ok} / {pms_total} 可连")
        self.ptp_start_btn.setEnabled(True)
        self.ptp_stop_btn.setEnabled(False)
        self.ptp_links_edit.setReadOnly(False)
        self.ptp_thread = None

    def reject(self) -> None:  # noqa: D401
        if self.ptp_thread is not None:
            self.ptp_thread.ptp_cancel()
            self.ptp_thread.wait(2000)
        super().reject()
