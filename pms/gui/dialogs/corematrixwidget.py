# -*- coding: utf-8 -*-
"""协议能力矩阵部件（v1.9.35）：设置 → 核心 →「协议能力矩阵」。

交互：点「开始探测」→ 后台线程拿**真实核心**逐格自检（每格起一个核心进程，17 协议 × 2 核心 ≈ 半分钟）
→ 表格显示「内置表 vs 实测」对照，不一致的行标黄。

为什么值得单独一个部件：
1. `pnode_validate_core_support()` 那张表是手写的，核心一升级就可能过期——这个部件就是它的体检报告；
2. 探测结果**不参与运行时选核心**（用户拍板），只用来核对，所以放在设置里、不自动跑。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pms.constants import PMS_CORE_KIND_SINGBOX, PMS_CORE_KIND_XRAY, PMS_LOGGER_NAME, PMS_PROTOCOL_TITLES
from pms.corematrix import (
    PMS_MATRIX_STATE_TITLES,
    PmsCoreMatrixSignals,
    PmsCoreMatrixTask,
    pms_core_matrix_builtin_state,
    pms_core_matrix_cache_load,
    pms_core_matrix_pick_samples,
    pms_core_matrix_protocols,
    pms_core_matrix_rows,
    pms_core_matrix_summary_text,
)

if True:  # 仅类型检查用，避免循环导入
    from pms.coresys import PmsCoreLocator
    from pms.paths import PmsRuntimePaths
    from pms.settings import PmsSettingsManager

PMS_MATRIX_COLUMNS = ("协议", "内置表 Xray", "内置表 sing-box", "实测 Xray", "实测 sing-box", "样本", "说明（核心原话）")


class PmsCoreMatrixWidget(QWidget):
    """协议能力矩阵：内置表 vs 实测（只核对，不改运行时行为）。"""

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_paths: "PmsRuntimePaths",
        pms_locator: "PmsCoreLocator",
        pms_repo: Any = None,
        pms_parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pcmw_settings = pms_settings
        self.pcmw_paths = pms_paths
        self.pcmw_locator = pms_locator
        self.pcmw_repo = pms_repo                     # 与主窗口同一个仓储对象；没有就只用内置样本
        self.pcmw_logger = logging.getLogger(PMS_LOGGER_NAME + ".corematrix")
        self.pcmw_payload: dict[str, Any] = {}
        self.pcmw_running = False
        self.pcmw_build_ui()
        self.pcmw_load_cache()

    # ---------------------------------------------------------------- 界面
    def pcmw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(
            "拿**本机真实核心**逐格自检：给每个协议造一份只含该协议的最小配置，跑一次 "
            "`xray run -test` / `sing-box check`，按核心原话判定「支持 / 不支持 / 配置没过」，"
            "并在「说明」列写下核心的原话。<br>"
            "样本优先取库里已有的真实节点（每种协议一个），库里没有的协议用内置样本兜底，"
            "「样本」列会写明这一格用的是哪种。<br>"
            "★ 探测结果**只用来核对内置表**，不会自动改变运行时「按节点选核心」的判断；"
            "「内置表」列就是 `pnode_validate_core_support()` 现在怎么说的。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8a8f98")
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.pcmw_btn_start = QPushButton("开始探测（约半分钟）")
        self.pcmw_btn_start.clicked.connect(self.pcmw_start)
        row.addWidget(self.pcmw_btn_start)
        self.pcmw_chk_real = QCheckBox("样本优先用库里的真实节点")
        self.pcmw_chk_real.setChecked(True)
        self.pcmw_chk_real.setToolTip("库里有该协议节点就用它当样本（更贴近真实字段）；否则用内置样本")
        row.addWidget(self.pcmw_chk_real)
        row.addStretch(1)
        layout.addLayout(row)
        self.pcmw_status = QLabel("还没有探测过")
        self.pcmw_status.setWordWrap(True)
        layout.addWidget(self.pcmw_status)
        self.pcmw_table = QTableWidget(0, len(PMS_MATRIX_COLUMNS))
        self.pcmw_table.setHorizontalHeaderLabels(list(PMS_MATRIX_COLUMNS))
        self.pcmw_table.verticalHeader().setVisible(False)
        self.pcmw_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.pcmw_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.pcmw_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.pcmw_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.pcmw_table, 1)

    # ---------------------------------------------------------------- 数据
    def pcmw_load_cache(self) -> None:
        """进设置时先把上次的结果摆上（不用等探测）。"""
        payload = pms_core_matrix_cache_load(self.pcmw_paths)
        self.pcmw_payload = payload
        self.pcmw_fill_rows(pms_core_matrix_rows(payload) if payload else [])
        if payload:
            when = str(payload.get("generated_at") or "")
            self.pcmw_status.setText("（上次结果）" + pms_core_matrix_summary_text(payload) +
                                     (f"　探测时间：{when}" if when else ""))
        else:
            self.pcmw_status.setText("还没有探测过——点上面的「开始探测」跑一轮（结果会缓存，下次进来直接看）")

    def pcmw_start(self) -> None:
        if self.pcmw_running:
            return
        self.pcmw_running = True
        self.pcmw_btn_start.setEnabled(False)
        self.pcmw_btn_start.setText("探测中…（每格起一个核心进程，界面不卡）")
        samples = None
        if self.pcmw_chk_real.isChecked():
            samples = pms_core_matrix_pick_samples(self.pcmw_collect_nodes())
        signals = PmsCoreMatrixSignals(self)
        signals.pcm_finished.connect(self.pcmw_on_finished)
        QThreadPool.globalInstance().start(PmsCoreMatrixTask(
            signals, self.pcmw_paths, self.pcmw_settings, self.pcmw_locator, samples,
        ))
        source = "库里节点优先" if samples else "内置样本"
        self.pcmw_status.setText(f"正在探测（样本：{source}）：逐个协议起核心做自检，跑完自动刷新")

    def pcmw_collect_nodes(self) -> list[Any]:
        """取库里的节点当样本（用的就是主窗口那个仓储对象，不另开数据库连接）。"""
        if self.pcmw_repo is None:
            return []
        try:
            return list(self.pcmw_repo.pnr_load_all_nodes())
        except Exception as exc:  # noqa: BLE001 - 读不到库就退回内置样本，不打断探测
            self.pcmw_logger.warning("矩阵探测：读节点当样本失败（改用内置样本）：%s", exc)
            return []

    def pcmw_on_finished(self, pms_payload: dict) -> None:
        self.pcmw_running = False
        self.pcmw_btn_start.setEnabled(True)
        self.pcmw_btn_start.setText("开始探测（约半分钟）")
        if pms_payload.get("error"):
            self.pcmw_status.setText(f"探测失败：{pms_payload['error']}")
            return
        self.pcmw_payload = pms_payload
        self.pcmw_fill_rows(pms_core_matrix_rows(pms_payload))
        when = str(pms_payload.get("generated_at") or "")
        self.pcmw_status.setText(pms_core_matrix_summary_text(pms_payload) + (f"　探测时间：{when}" if when else ""))

    def pcmw_fill_rows(self, pms_rows: list[dict[str, Any]]) -> None:
        protocols = pms_core_matrix_protocols()
        by_protocol = {str(row.get("protocol")): row for row in pms_rows}
        self.pcmw_table.setRowCount(len(protocols))
        for index, protocol in enumerate(protocols):
            row = by_protocol.get(protocol, {})
            builtin_x = str(row.get("builtin_xray") or pms_core_matrix_builtin_state(PMS_CORE_KIND_XRAY, protocol))
            builtin_s = str(row.get("builtin_singbox") or pms_core_matrix_builtin_state(PMS_CORE_KIND_SINGBOX, protocol))
            probed_x = str(row.get("probed_xray") or "")
            probed_s = str(row.get("probed_singbox") or "")
            note = str(row.get("note") or "")
            texts = (
                PMS_PROTOCOL_TITLES.get(protocol, protocol),
                PMS_MATRIX_STATE_TITLES.get(builtin_x, builtin_x),
                PMS_MATRIX_STATE_TITLES.get(builtin_s, builtin_s),
                PMS_MATRIX_STATE_TITLES.get(probed_x, probed_x) if probed_x else "未探测",
                PMS_MATRIX_STATE_TITLES.get(probed_s, probed_s) if probed_s else "未探测",
                str(row.get("source_xray") or row.get("source_singbox") or ""),
                note,
            )
            for column, text in enumerate(texts):
                item = QTableWidgetItem(str(text))
                if column in (1, 2, 3, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column >= 5:
                    item.setToolTip(note)
                self.pcmw_table.setItem(index, column, item)
            if bool(row.get("mismatch")) and probed_x:
                for column in range(len(PMS_MATRIX_COLUMNS)):
                    item = self.pcmw_table.item(index, column)
                    if item is not None:
                        item.setBackground(QColor(92, 76, 28))

    def pcmw_rows(self) -> list[list[str]]:
        """探针用：当前表里每一行的文字。"""
        return [
            [
                self.pcmw_table.item(index, column).text() if self.pcmw_table.item(index, column) else ""
                for column in range(len(PMS_MATRIX_COLUMNS))
            ]
            for index in range(self.pcmw_table.rowCount())
        ]


def _pcmw_build(pms_dialog) -> "PmsCoreMatrixWidget":
    """把「协议能力矩阵」挂到设置对话框上（设置对话框只调这一行，不动启动器重导出清单）。"""
    return PmsCoreMatrixWidget(
        pms_dialog.pset_settings, pms_dialog.pset_paths, pms_dialog.pset_locator,
        getattr(pms_dialog, "pset_repo", None), pms_dialog,
    )
