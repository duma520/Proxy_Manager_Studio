#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内核版本一览部件（v1.9.32 创建）

用户要求：「能不能有个地方，直接查看所有核的版本列表，还有检测线上版本，让用户直观知道本地版本多少，
线上版本多少，是否进行更新（用户原话：想在「设置 → 核心」里直接看到本机版本 / 线上版本 / 要不要更新）
和 一键更新。」

因此本部件就是**一张表**：两个主核心（Xray / sing-box）各一行（v1.9.45 起），
列出「本地版本 / 线上版本 / 状态（已是最新 / 可更新 / 未安装 / 无法判定 / 检查失败）」，
外加「检查线上版本」「刷新本机版本」「下载 / 更新选中内核」「一键更新可更新的（N）」。

v1.9.49 再加一个显式按钮「清理未命中的内核文件」（审计表第 30 项，见 `pms/coreclean.py`）：
同目录里那些**没被检测命中**、但名字属于已知内核族的旧文件（如 `mihomo.exe`），
以前只能手工删；现在**先列清单 → 二次确认 → 才删 → 报删了几个 / 释放多少**。
本机当前名单是空的（`core\\` 下只有两个正式核心），这个按钮是**防御性**的。

为什么单独成文件：`settingsdialog.py` 顶在 1000 行红线（项目第 8 条），控件一律拆部件
（同 `coreupdatewidget.py` / `testsortwidget.py`）。设置对话框只调 `_pcvw_build()` 一行把它挂上。

职责边界
--------
- **本部件只做显示 + 发动作**：真正的下载由主窗口的下载服务执行（`pcvw_action` → `psed_action`）。
- 版本查询走既有的 `PmsCoreUpdateCheckTask`（它本来就是通用实现：`pms_core_api_url()` 对主核心与
  两个主核心都成立），用 `QThreadPool.globalInstance()` 串行跑，**不阻塞界面**；
  与「定时自动更新服务」各自持有独立的信号对象，互不干扰（手动检查**只查不下**）。
- 内核文件清理**只在本部件里由用户点击触发**，删除动作全在 `pms/coreclean.py`；
  本部件绝不自动清理（没有任何 `showEvent` / 启动钩子去删文件）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from PySide6.QtCore import QThreadPool, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pms.constants import (
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_TITLES,
    PMS_CORE_KIND_XRAY,
)
from pms.coreclean import (
    PmsCoreCleanSignals,
    PmsCoreCleanTask,
    pms_core_clean_plan,
    pms_core_clean_plan_text,
    pms_core_clean_size_text,
    pms_core_clean_summary_text,
)
from pms.coreupdate import PmsCoreUpdateCheckSignals, PmsCoreUpdateCheckTask
# v1.9.45：版本比较搬到 coremeta.py。v1.9.55：`_pms_version_effective_local()` 也一起搬
# （它在旧的下线模块里被漏掉，「内核版本一览」一打开就 NameError）。
from pms.coremeta import pms_version_effective_local, pms_version_newer

if TYPE_CHECKING:  # 仅类型检查用，避免与路径 / 设置 / 核心模块循环导入
    from pms.coresys import PmsCoreLocator
    from pms.paths import PmsRuntimePaths
    from pms.settings import PmsSettingsManager

# 主核心在表里的短名字（`PMS_CORE_KIND_TITLES` 那套是给下拉框用的长名字）
PCVW_MAIN_TITLES: dict[str, str] = {
    PMS_CORE_KIND_XRAY: "Xray-core",
    PMS_CORE_KIND_SINGBOX: "sing-box",
}
PCVW_MAIN_ORDER: tuple[str, ...] = (PMS_CORE_KIND_SINGBOX, PMS_CORE_KIND_XRAY)

PCVW_STATE_LATEST = "latest"
PCVW_STATE_STALE = "stale"
PCVW_STATE_MISSING = "missing"
PCVW_STATE_UNKNOWN = "unknown"
PCVW_STATE_FAILED = "failed"
PCVW_STATE_CHECKING = "checking"

PCVW_STATE_TITLES: dict[str, str] = {
    PCVW_STATE_LATEST: "已是最新",
    PCVW_STATE_STALE: "可更新",
    PCVW_STATE_MISSING: "未安装",
    PCVW_STATE_UNKNOWN: "无法判定",
    PCVW_STATE_FAILED: "检查失败",
    PCVW_STATE_CHECKING: "检查中…",
}
PCVW_STATE_COLORS: dict[str, str] = {
    PCVW_STATE_LATEST: "#1a7f37",
    PCVW_STATE_STALE: "#9a6700",
    PCVW_STATE_MISSING: "#8a9199",
    PCVW_STATE_UNKNOWN: "#8a9199",
    PCVW_STATE_FAILED: "#c1352c",
    PCVW_STATE_CHECKING: "#57606a",
}


class PmsCoreVersionWidget(QWidget):
    """内核版本一览：本地 / 线上 / 状态 + 一键更新。"""

    pcvw_action = Signal(str, dict)   # 动作名, 参数（下载由主窗口执行）

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_paths: "PmsRuntimePaths",
        pms_locator: "PmsCoreLocator",
        pms_parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pcvw_settings = pms_settings
        self.pcvw_paths = pms_paths
        self.pcvw_locator = pms_locator
        # 查询结果（kind → 值）；只查不下，与定时自动更新服务各用各的信号对象
        self.pcvw_remote: dict[str, str] = {}
        self.pcvw_remote_ok: dict[str, bool] = {}
        self.pcvw_remote_error: dict[str, str] = {}
        self.pcvw_local: dict[str, str] = {}          # kind → 本机版本（空串 = 探测不出）
        self.pcvw_local_file: dict[str, str] = {}     # kind → 本机可执行文件名（空串 = 未安装）
        self.pcvw_queue: list[str] = []
        self.pcvw_checking = False
        self.pcvw_checking_kind = ""
        self.pcvw_local_reading = False
        self.pcvw_local_ready = False
        self.pcvw_last_check = ""
        self.pcvw_cleaning = False
        self.pcvw_check_signals = PmsCoreUpdateCheckSignals(self)
        self.pcvw_check_signals.pcus_checked.connect(self.pcvw_on_checked)
        self.pcvw_clean_signals = PmsCoreCleanSignals(self)
        self.pcvw_clean_signals.pcc_finished.connect(self.pcvw_on_clean_finished)
        self.pcvw_build_ui()
        self.pcvw_render()
        self.pcvw_refresh_clean_plan()

    # ------------------------------------------------------------------ 界面
    def pcvw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.pcvw_table = QTableWidget(len(self.pcvw_core_rows()), len(self.PCVW_COLUMNS))
        self.pcvw_table.setHorizontalHeaderLabels(list(self.PCVW_COLUMNS))
        self.pcvw_table.verticalHeader().setVisible(False)
        self.pcvw_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pcvw_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pcvw_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.pcvw_table.setAlternatingRowColors(True)
        self.pcvw_table.setMinimumHeight(210)
        header = self.pcvw_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.pcvw_table)

        buttons = QHBoxLayout()
        self.pcvw_btn_check = QPushButton("检查线上版本")
        self.pcvw_btn_check.setToolTip("逐个查询各自官方 GitHub Release 的最新版本（只查不下，不阻塞界面）")
        self.pcvw_btn_check.clicked.connect(self.pcvw_start_check)
        buttons.addWidget(self.pcvw_btn_check)
        self.pcvw_btn_local = QPushButton("刷新本机版本")
        self.pcvw_btn_local.setToolTip("重新执行一次「内核 version」读本机版本（结果有缓存，这里强制刷新）")
        self.pcvw_btn_local.clicked.connect(lambda: self.pcvw_refresh_local(pms_force=True))
        buttons.addWidget(self.pcvw_btn_local)
        self.pcvw_btn_pick = QPushButton("下载 / 更新选中内核")
        self.pcvw_btn_pick.setToolTip("按表格里选中的那一行下载 / 更新（未安装的也能用它装上）")
        self.pcvw_btn_pick.clicked.connect(self.pcvw_on_update_selected)
        buttons.addWidget(self.pcvw_btn_pick)
        self.pcvw_btn_update = QPushButton("一键更新可更新的（0）")
        self.pcvw_btn_update.setToolTip("只更新表格里状态为「可更新」的那些内核，串行下载")
        self.pcvw_btn_update.clicked.connect(self.pcvw_on_update_stale)
        buttons.addWidget(self.pcvw_btn_update)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.pcvw_note = QLabel("")
        self.pcvw_note.setObjectName("hintLabel")
        self.pcvw_note.setWordWrap(True)
        layout.addWidget(self.pcvw_note)

        # v1.9.49（审计表第 30 项）：显式清理「未命中内核文件」
        # 只在用户点按钮时扫描 / 删除，本部件没有任何自动清理钩子。
        clean_row = QHBoxLayout()
        self.pcvw_btn_clean = QPushButton("清理未命中的内核文件")
        self.pcvw_btn_clean.setToolTip(
            "同目录里没被检测命中、但名字属于已知内核族的旧文件（如 mihomo.exe）——"
            "程序自己从不删它们，只在这里由你确认后删；正在使用的核心与未知文件名永不在名单里"
        )
        self.pcvw_btn_clean.clicked.connect(self.pcvw_on_clean_clicked)
        clean_row.addWidget(self.pcvw_btn_clean)
        clean_row.addStretch(1)
        layout.addLayout(clean_row)

        self.pcvw_clean_table = QTableWidget(0, len(self.PCVW_CLEAN_COLUMNS))
        self.pcvw_clean_table.setHorizontalHeaderLabels(list(self.PCVW_CLEAN_COLUMNS))
        self.pcvw_clean_table.verticalHeader().setVisible(False)
        self.pcvw_clean_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pcvw_clean_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pcvw_clean_table.setMaximumHeight(150)
        clean_header = self.pcvw_clean_table.horizontalHeader()
        clean_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        clean_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.pcvw_clean_table)

        self.pcvw_clean_note = QLabel("")
        self.pcvw_clean_note.setObjectName("hintLabel")
        self.pcvw_clean_note.setWordWrap(True)
        layout.addWidget(self.pcvw_clean_note)

    PCVW_CLEAN_COLUMNS = ("文件", "体积", "路径")

    PCVW_COLUMNS = ("内核", "类别", "本地版本", "线上版本", "状态", "说明")

    def pcvw_core_rows(self) -> list[tuple[str, str, str]]:
        """[(内核代号, 显示名, 类别)]——v1.9.45 起只有两个主核心（Xray / sing-box）。"""
        rows: list[tuple[str, str, str]] = []
        for kind in PCVW_MAIN_ORDER:
            rows.append((kind, PCVW_MAIN_TITLES.get(kind, PMS_CORE_KIND_TITLES.get(kind, kind)), "主核心"))
        return rows

    def pcvw_row_of_kind(self, pms_kind: str) -> int:
        for row, (kind, _title, _group) in enumerate(self.pcvw_core_rows()):
            if kind == str(pms_kind):
                return row
        return -1

    def pcvw_kind_of_row(self, pms_row: int) -> str:
        rows = self.pcvw_core_rows()
        return rows[pms_row][0] if 0 <= pms_row < len(rows) else ""

    # ------------------------------------------------------------------ 本机版本
    def pcvw_refresh_local(self, pms_force: bool = False) -> None:
        """读一遍本机两个主核心的版本（走 locator 的带缓存探测）。

        读的过程会跑两个子进程（典型 0.5 秒内、最坏受核心 8 秒超时限制），
        所以用 `pcvw_local_reading` 挡住重入，并在读之前先把提示文字画出来。
        """
        if self.pcvw_local_reading or (self.pcvw_local_ready and not pms_force):
            return
        if pms_force:
            try:
                self.pcvw_locator.pcl_version_cache.clear()
            except AttributeError:  # 缓存字段改名时不影响显示
                pass
        self.pcvw_local_reading = True
        self.pcvw_note.setText("正在读取本机版本…")
        QApplication.processEvents()
        try:
            for kind, _title, _group in self.pcvw_core_rows():
                self.pcvw_local_file[kind] = ""
                self.pcvw_local[kind] = ""
                found = self.pcvw_locator.pcl_find_any(self.pcvw_settings)
                path = found.get(kind)
                if path is not None:
                    self.pcvw_local_file[kind] = Path(path).name
                    self.pcvw_local[kind] = str(self.pcvw_locator.pcl_detect_version(Path(path)) or "")
        finally:
            self.pcvw_local_reading = False
            self.pcvw_local_ready = True
        self.pcvw_render()

    # ------------------------------------------------------------------ 线上版本
    def pcvw_local_port(self) -> int:
        return int(self.pcvw_settings.psm_get_int("inbound.mixed_port", 0))

    def pcvw_external_proxy(self) -> str:
        return str(self.pcvw_settings.psm_get_str("telegram.external_proxy", "") or "").strip()

    def pcvw_start_check(self) -> None:
        if self.pcvw_checking:
            return
        if not self.pcvw_local_ready:
            self.pcvw_refresh_local()
        self.pcvw_remote.clear()
        self.pcvw_remote_ok.clear()
        self.pcvw_remote_error.clear()
        self.pcvw_queue = [kind for kind, _title, _group in self.pcvw_core_rows()]
        self.pcvw_checking = True
        self.pcvw_last_check = ""
        self.pcvw_render()
        self.pcvw_next_check()

    def pcvw_next_check(self) -> None:
        if not self.pcvw_queue:
            self.pcvw_checking = False
            self.pcvw_checking_kind = ""
            self.pcvw_last_check = datetime.now().strftime("%H:%M:%S")
            self.pcvw_render()
            return
        kind = self.pcvw_queue.pop(0)
        self.pcvw_checking_kind = kind
        self.pcvw_render()
        QThreadPool.globalInstance().start(PmsCoreUpdateCheckTask(
            self.pcvw_check_signals, kind, self.pcvw_local_port(), self.pcvw_external_proxy(),
        ))

    @Slot(str, bool, str, str)
    def pcvw_on_checked(self, pms_kind: str, pms_ok: bool, pms_version: str, pms_message: str) -> None:
        self.pcvw_remote_ok[str(pms_kind)] = bool(pms_ok)
        self.pcvw_remote[str(pms_kind)] = str(pms_version or "")
        self.pcvw_remote_error[str(pms_kind)] = str(pms_message or "")
        self.pcvw_next_check()

    # ------------------------------------------------------------------ 状态判定
    def pcvw_state_of(self, pms_kind: str) -> str:
        """一个内核当前该显示什么状态（纯判定，便于探针直接验）。"""
        if not self.pcvw_local_file.get(pms_kind):
            return PCVW_STATE_MISSING
        if self.pcvw_checking and (pms_kind == self.pcvw_checking_kind or pms_kind in self.pcvw_queue):
            return PCVW_STATE_CHECKING
        if pms_kind not in self.pcvw_remote_ok:
            return PCVW_STATE_UNKNOWN
        if not self.pcvw_remote_ok.get(pms_kind):
            return PCVW_STATE_FAILED
        remote = self.pcvw_remote.get(pms_kind, "")
        # v1.9.61：standalone.py 下线后 `core.standalone_versions` 记录值已不存在，
        # `pcvw_recorded_version` 从未被定义（遗留悬空调用，一进线上版本检查就 AttributeError）。
        # 记录值现已无来源，直接以探测到的本机版本为准（pms_version_effective_local 在
        # recorded 为空时回退为 probed，行为等价）。
        local = pms_version_effective_local(self.pcvw_local.get(pms_kind, ""), "")
        if not local or not remote:
            return PCVW_STATE_UNKNOWN
        return PCVW_STATE_STALE if pms_version_newer(remote, local) else PCVW_STATE_LATEST

    def pcvw_stale_kinds(self) -> list[str]:
        """状态为「可更新」的内核代号（「一键更新」只动这些）。"""
        return [kind for kind, _title, _group in self.pcvw_core_rows() if self.pcvw_state_of(kind) == PCVW_STATE_STALE]

    def pcvw_cell_text(self, pms_kind: str, pms_column: int) -> str:
        rows = {kind: (title, group) for kind, title, group in self.pcvw_core_rows()}
        title, group = rows.get(pms_kind, (pms_kind, ""))
        exe_name = self.pcvw_local_file.get(pms_kind, "")
        if pms_column == 0:
            return title
        if pms_column == 1:
            return group
        if pms_column == 2:
            if not exe_name:
                return "未安装"
            return self.pcvw_local.get(pms_kind, "") or "取不到"
        if pms_column == 3:
            if pms_kind in self.pcvw_remote_ok:
                return self.pcvw_remote.get(pms_kind, "") or "-"
            if self.pcvw_checking:
                return "…"
            return "未检查"
        if pms_column == 4:
            return PCVW_STATE_TITLES.get(self.pcvw_state_of(pms_kind), "")
        if pms_column == 5:
            if self.pcvw_state_of(pms_kind) == PCVW_STATE_FAILED:
                return self.pcvw_remote_error.get(pms_kind, "")[:80]
            return exe_name
        return ""

    def pcvw_render(self) -> None:
        """把当前状态画进表格（每次只重画 8 行 × 6 列，很便宜）。"""
        for row, (kind, _title, _group) in enumerate(self.pcvw_core_rows()):
            state = self.pcvw_state_of(kind)
            for column in range(len(self.PCVW_COLUMNS)):
                text = self.pcvw_cell_text(kind, column)
                item = self.pcvw_table.item(row, column)
                if item is None:
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    self.pcvw_table.setItem(row, column, item)
                elif item.text() != text:
                    item.setText(text)
                if column == 4:
                    item.setForeground(QColor(PCVW_STATE_COLORS.get(state, "#57606a")))
        stale = self.pcvw_stale_kinds()
        self.pcvw_btn_update.setText(f"一键更新可更新的（{len(stale)}）")
        self.pcvw_btn_update.setEnabled(bool(stale) and not self.pcvw_checking)
        self.pcvw_btn_check.setEnabled(not self.pcvw_checking)
        self.pcvw_btn_pick.setEnabled(not self.pcvw_checking)
        if self.pcvw_checking:
            self.pcvw_note.setText(
                f"正在查询线上版本（{len(self.pcvw_remote_ok)}/{len(self.pcvw_core_rows())}）："
                f"{self.pcvw_checking_kind}（走本程序代理 → 外部代理 → 直连）"
            )
            return
        parts = []
        if self.pcvw_last_check:
            parts.append(f"上次检查 {self.pcvw_last_check}")
        parts.append(f"可更新 {len(stale)} 个" if stale else "没有需要更新的")
        failed = [kind for kind, _t, _g in self.pcvw_core_rows() if self.pcvw_state_of(kind) == PCVW_STATE_FAILED]
        if failed:
            parts.append(f"检查失败 {len(failed)} 个")
        self.pcvw_note.setText(
            "；".join(parts)
            + "。线上版本来自各自官方 GitHub Release（2 个内核 = 2 次请求，匿名限流 60 次/小时）；"
            "本地版本取不到的内核（如 AnyTLS）用「已装版本记录」比对。"
        )

    # ------------------------------------------------------------------ 动作
    def pcvw_on_update_stale(self) -> None:
        kinds = self.pcvw_stale_kinds()
        if not kinds:
            return
        self.pcvw_ask_download(kinds, f"开始更新 {len(kinds)} 个有新版本的内核（串行下载）")
        for kind in kinds:
            self.pcvw_remote_ok.pop(kind, None)      # 更新完要重新检查，先撤掉旧结论
        self.pcvw_render()

    def pcvw_on_update_selected(self) -> None:
        row = self.pcvw_table.currentRow()
        kind = self.pcvw_kind_of_row(row)
        if kind:
            self.pcvw_ask_download([kind], f"开始下载 / 更新：{self.pcvw_cell_text(kind, 0)}")

    def pcvw_ask_download(self, pms_kinds: list[str], pms_note: str) -> None:
        self.pcvw_action.emit("download_core", {"kind": list(pms_kinds)})
        self.pcvw_note.setText(pms_note + "　下载完成后这里会自动刷新本机版本。")

    def pcvw_on_download_finished(self, pms_kind: str, pms_ok: bool) -> None:
        """主窗口在任意核心下载结束时调用：刷新本机版本并重新判定那一行。"""
        if not pms_ok:
            return
        self.pcvw_refresh_local(pms_force=True)
        self.pcvw_remote_ok.pop(str(pms_kind), None)
        self.pcvw_render()

    # ------------------------------------------------------------------ 未命中内核文件清理（v1.9.49，审计表第 30 项）
    def pcvw_clean_plan(self) -> list:
        """当前会进清理名单的文件（只读扫描；名单为空是正常结果）。"""
        return pms_core_clean_plan(self.pcvw_paths, self.pcvw_locator)

    def pcvw_refresh_clean_plan(self) -> list:
        """把名单画进小表并更新说明（只读；本部件从不自动删文件）。"""
        try:
            plan = self.pcvw_clean_plan()
        except Exception as exc:  # noqa: BLE001 - 扫描失败要说出来，不能让设置页崩掉
            self.pcvw_clean_note.setText(f"扫描未命中内核文件失败：{type(exc).__name__}: {exc}")
            return []
        self.pcvw_clean_table.setRowCount(len(plan))
        for row, item in enumerate(plan):
            path = Path(item["path"])
            for column, text in enumerate((str(item["name"]), pms_core_clean_size_text(item["size"]), str(path))):
                cell = self.pcvw_clean_table.item(row, column)
                if cell is None:
                    self.pcvw_clean_table.setItem(row, column, QTableWidgetItem(text))
                else:
                    cell.setText(text)
        total = sum(int(item["size"]) for item in plan)
        self.pcvw_btn_clean.setEnabled(bool(plan) and not self.pcvw_cleaning)
        if plan:
            self.pcvw_clean_note.setText(
                f"未命中内核文件：{len(plan)} 个，合计 {pms_core_clean_size_text(total)}。"
                "点「清理未命中的内核文件」会先弹出清单让你确认，删完报告删了几个 / 释放多少；"
                "正在使用的核心（xray.exe / sing-box.exe）与未知文件名永不在名单里。"
            )
        else:
            self.pcvw_clean_note.setText(
                "未命中内核文件：无。内核目录里没有「名字像内核、但没被检测命中」的旧文件"
                "（本版只保留 Xray + sing-box 两个正式核心，所以这里通常是空的）。"
            )
        return plan

    def pcvw_on_clean_clicked(self) -> None:
        """清理按钮：**先列清单 → 二次确认（默认取消）→ 才真删**。"""
        if self.pcvw_cleaning:
            return
        plan = self.pcvw_refresh_clean_plan()
        if not plan:
            QMessageBox.information(self, "没有可清理的文件", pms_core_clean_summary_text({"files": 0}))
            return
        total = sum(int(item["size"]) for item in plan)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("确认删除内核文件")
        box.setText(f"将删除 {len(plan)} 个未命中的内核文件，共 {pms_core_clean_size_text(total)}。")
        box.setInformativeText(
            "这些文件都不在「检测命中的核心」名单里，删掉不影响正在使用的核心。\n\n"
            + pms_core_clean_plan_text(plan) + "\n\n确定要删除吗？（此操作不可撤销）"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)   # 默认取消，避免误按回车
        if box.exec() != QMessageBox.StandardButton.Yes:
            self.pcvw_clean_note.setText(f"已取消：{len(plan)} 个文件一个都没删。")
            return
        self.pcvw_cleaning = True
        self.pcvw_btn_clean.setEnabled(False)
        self.pcvw_clean_note.setText(f"正在删除 {len(plan)} 个未命中的内核文件…")
        QThreadPool.globalInstance().start(PmsCoreCleanTask(self.pcvw_clean_signals, self.pcvw_paths, self.pcvw_locator))

    @Slot(dict)
    def pcvw_on_clean_finished(self, pms_result: dict) -> None:
        """删完：报「删了几个 / 释放多少」，并刷新清单。"""
        self.pcvw_cleaning = False
        self.pcvw_refresh_clean_plan()
        summary = pms_core_clean_summary_text(pms_result)
        self.pcvw_clean_note.setText(summary)
        if int(pms_result.get("files") or 0):
            self.pcvw_clean_table.setRowCount(0)
            QMessageBox.information(self, "清理完成", summary)

    # ------------------------------------------------------------------ 事件
    def showEvent(self, pms_event) -> None:  # noqa: N802, D102
        super().showEvent(pms_event)
        if not self.pcvw_local_ready:
            # 首次切到这个标签页才去读两个主核心的版本（不让设置对话框的构建变慢）
            QTimer.singleShot(0, self.pcvw_refresh_local)
        # 只刷新清单（只读），**不会**因为切页就删任何东西
        self.pcvw_refresh_clean_plan()


def _pcvw_build(pms_dialog) -> "PmsCoreVersionWidget":
    """把「内核版本一览」挂到设置对话框上并接好动作（设置对话框只调这一行）。

    做成模块私有是为了**不动启动器的重导出清单**（启动器只剩十几行余量）。
    """
    widget = PmsCoreVersionWidget(pms_dialog.pset_settings, pms_dialog.pset_paths, pms_dialog.pset_locator, pms_dialog)
    widget.pcvw_action.connect(pms_dialog.psed_action)
    return widget
