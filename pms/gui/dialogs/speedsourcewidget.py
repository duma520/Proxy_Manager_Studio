#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测速源选择小部件（v1.4.3）

「测速源预设 + 单次测速大小(MB) + 自检可用档位」三件套。

为什么单独成文件：settingsdialog.py 已接近 1000 行红线（项目第 8 条），
再往里塞控件会超限，因此按功能拆成独立部件，由设置对话框挂接。
"""

from __future__ import annotations

from typing import (
    Optional,
    Sequence,
)
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from urllib.error import HTTPError
from urllib.request import (
    Request,
    urlopen,
)
import logging

from pms.constants import (
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_LOGGER_NAME,
    PMS_SPEED_SOURCE_CLOUDFLARE,
    PMS_SPEED_SOURCE_CUSTOM,
    PMS_SPEED_SOURCE_TITLES,
)
from pms.utils import (
    pms_resolve_speed_url,
    pms_speed_candidate_sizes,
    pms_speed_ok_key,
    pms_speed_ok_sizes,
)

PMS_SPEED_CHECK_TIMEOUT_SECONDS = 8.0

PMS_SPEED_CHECK_PROBE_BYTES = 1024

# --------------------------------------------------------------------------------------
# 自检：逐档探测
# --------------------------------------------------------------------------------------
def pms_probe_speed_url(pms_url: str, pms_timeout: float = PMS_SPEED_CHECK_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """探测一个测速地址能否真实下载。

    带 Range 只取前 1 KB 就断开：既不浪费流量，又能确认地址确实可用。
    返回 (是否可用, 说明)。任何异常都被吞掉转成「不可用」，绝不向外抛。
    """
    if not str(pms_url or "").strip():
        return False, "地址为空"
    request = Request(
        pms_url,
        headers={
            "Range": f"bytes=0-{PMS_SPEED_CHECK_PROBE_BYTES - 1}",
            "User-Agent": PMS_DEFAULT_SUB_USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=pms_timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            if status not in (200, 206):
                return False, f"HTTP {status}"
            response.read(PMS_SPEED_CHECK_PROBE_BYTES)
            return True, f"HTTP {status}"
    except HTTPError as exc:
        return False, f"HTTP {int(getattr(exc, 'code', 0) or 0)}"
    except Exception as exc:  # 超时 / DNS 失败 / 连接重置等，一律记为不可用
        return False, str(exc)[:40] or type(exc).__name__

class PmsSpeedSourceCheckSignals(QObject):
    """自检工作线程 → 主线程。"""

    pssc_progress = Signal(str)

    pssc_done = Signal(object, str)      # 通过的档位列表, 汇总说明

class PmsSpeedSourceCheckWorker(QRunnable):
    """逐档探测某个测速源的各 MB 档位。"""

    def __init__(
        self,
        pms_signals: PmsSpeedSourceCheckSignals,
        pms_source: str,
        pms_sizes: Sequence[int],
    ) -> None:
        super().__init__()
        self.pscw_signals = pms_signals
        self.pscw_source = str(pms_source or "").strip().lower()
        self.pscw_sizes = [int(size) for size in pms_sizes or []]

    def run(self) -> None:  # noqa: D102
        logger = logging.getLogger(PMS_LOGGER_NAME)
        passed: list[int] = []
        for size_mb in self.pscw_sizes:
            url = pms_resolve_speed_url(self.pscw_source, size_mb)
            ok, note = pms_probe_speed_url(url)
            if ok:
                passed.append(int(size_mb))
            self.pscw_signals.pssc_progress.emit(f"{size_mb} MB → {note}")
        summary = "、".join(f"{size} MB" for size in passed) or "无"
        logger.info("测速源自检 %s：可用档位 %s（共 %d 档）", self.pscw_source, summary, len(self.pscw_sizes))
        self.pscw_signals.pssc_done.emit(passed, f"可用档位：{summary}（共检测 {len(self.pscw_sizes)} 档）")

class PmsSpeedSourceWidget(QWidget):
    """测速源下拉 + 单次大小数字框 + 自检按钮 + 结果回显。"""

    def __init__(
        self,
        pms_settings,
        pms_custom_edit: Optional[QLineEdit] = None,
        pms_parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pssw_settings = pms_settings
        self.pssw_custom_edit = pms_custom_edit
        self.pssw_checking = False
        self.pssw_titles = dict(PMS_SPEED_SOURCE_TITLES)
        self.pssw_titles[PMS_SPEED_SOURCE_CUSTOM] = "自定义地址（下方手填）"
        self.pssw_combo_source = QComboBox()
        self.pssw_spin_size = QSpinBox()
        self.pssw_btn_check = QPushButton("自检可用档位")
        self.pssw_bar_check = QProgressBar()
        self.pssw_label_preview = QLabel("-")
        self.pssw_label_ok = QLabel("尚未自检")
        self.pssw_signals = PmsSpeedSourceCheckSignals()
        self.pssw_build()
        self.pssw_bind()
        self.pssw_refresh()

    # ------------------------------------------------------------------ 构建
    def pssw_build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        row_top = QHBoxLayout()
        row_top.setContentsMargins(0, 0, 0, 0)
        row_top.setSpacing(6)
        row_top.addWidget(QLabel("测速源"))
        row_top.addWidget(self.pssw_combo_source, 1)
        row_top.addWidget(QLabel("单次大小"))
        row_top.addWidget(self.pssw_spin_size)
        row_top.addWidget(QLabel("MB"))
        layout.addLayout(row_top)

        row_bottom = QHBoxLayout()
        row_bottom.setContentsMargins(0, 0, 0, 0)
        row_bottom.setSpacing(6)
        row_bottom.addWidget(self.pssw_btn_check)
        row_bottom.addWidget(self.pssw_bar_check, 1)
        layout.addLayout(row_bottom)

        for label in (self.pssw_label_preview, self.pssw_label_ok):
            label.setObjectName("hintLabel")
            label.setWordWrap(True)
            layout.addWidget(label)

    def pssw_bind(self) -> None:
        for value, title in self.pssw_titles.items():
            self.pssw_combo_source.addItem(title, value)
        index = self.pssw_combo_source.findData(self.pssw_settings.psm_get_str("speed.source", PMS_SPEED_SOURCE_CLOUDFLARE))
        self.pssw_combo_source.setCurrentIndex(max(0, index))
        self.pssw_combo_source.currentIndexChanged.connect(self.pssw_on_source_changed)
        # 先设范围再赋值：QSpinBox 默认 0-99，顺序反了会把 500 显示成 99
        self.pssw_spin_size.setRange(1, 1024)
        self.pssw_spin_size.setKeyboardTracking(False)
        self.pssw_spin_size.setValue(self.pssw_settings.psm_get_int("speed.size_mb", 10))
        self.pssw_spin_size.valueChanged.connect(self.pssw_on_size_changed)
        self.pssw_btn_check.clicked.connect(self.pssw_start_check)
        self.pssw_signals.pssc_progress.connect(self.pssw_on_check_progress)
        self.pssw_signals.pssc_done.connect(self.pssw_on_check_done)

    # ------------------------------------------------------------------ 刷新
    def pssw_refresh(self) -> None:
        source = str(self.pssw_combo_source.currentData() or PMS_SPEED_SOURCE_CLOUDFLARE)
        size_mb = self.pssw_spin_size.value()
        self.pssw_label_preview.setText(
            f"实际测速地址：{pms_resolve_speed_url(source, size_mb, self.pssw_settings.psm_get_str('speed.url', ''))}"
        )
        if self.pssw_custom_edit is not None:
            self.pssw_custom_edit.setEnabled(source == PMS_SPEED_SOURCE_CUSTOM)
        if not pms_speed_candidate_sizes(source):
            self.pssw_btn_check.setEnabled(False)
            self.pssw_label_ok.setText("自定义源按下方地址原样使用，无需自检。")
            return
        self.pssw_btn_check.setEnabled(not self.pssw_checking)
        if self.pssw_checking:
            return
        ok_sizes = pms_speed_ok_sizes(self.pssw_settings.psm_get_list("speed.ok_sizes"), source)
        if not ok_sizes:
            self.pssw_label_ok.setText("尚未自检 —— 点「自检可用档位」在你的网络下实测并记录。")
            return
        text = "自检通过档位：" + "、".join(f"{size} MB" for size in ok_sizes)
        if size_mb not in ok_sizes:
            nearest = min(ok_sizes, key=lambda item: abs(int(item) - int(size_mb)))
            text += f"（当前 {size_mb} MB 不在通过档位内，建议改用 {nearest} MB）"
        self.pssw_label_ok.setText(text)

    # ------------------------------------------------------------------ 交互
    @Slot(int)
    def pssw_on_source_changed(self, _pms_index: int) -> None:
        self.pssw_settings.psm_set("speed.source", str(self.pssw_combo_source.currentData() or PMS_SPEED_SOURCE_CLOUDFLARE))
        self.pssw_refresh()

    @Slot(int)
    def pssw_on_size_changed(self, pms_value: int) -> None:
        self.pssw_settings.psm_set("speed.size_mb", int(pms_value))
        self.pssw_refresh()

    @Slot()
    def pssw_start_check(self) -> None:
        if self.pssw_checking:
            return
        source = str(self.pssw_combo_source.currentData() or "")
        sizes = pms_speed_candidate_sizes(source)
        if not sizes:
            return
        self.pssw_checking = True
        self.pssw_btn_check.setEnabled(False)
        self.pssw_bar_check.setRange(0, len(sizes))
        self.pssw_bar_check.setValue(0)
        self.pssw_label_ok.setText(f"正在自检 {len(sizes)} 个档位，请稍候…")
        QThreadPool.globalInstance().start(PmsSpeedSourceCheckWorker(self.pssw_signals, source, sizes))

    @Slot(str)
    def pssw_on_check_progress(self, pms_text: str) -> None:
        self.pssw_bar_check.setValue(min(self.pssw_bar_check.maximum(), self.pssw_bar_check.value() + 1))
        self.pssw_label_ok.setText(f"自检中：{pms_text}")

    @Slot(object, str)
    def pssw_on_check_done(self, pms_passed: object, pms_summary: str) -> None:
        self.pssw_checking = False
        source = str(self.pssw_combo_source.currentData() or "")
        passed = [int(item) for item in (list(pms_passed) if pms_passed else []) if str(item).strip()]
        prefix = f"{source}:"
        kept = [item for item in self.pssw_settings.psm_get_list("speed.ok_sizes") if not str(item).startswith(prefix)]
        kept.extend(pms_speed_ok_key(source, size) for size in passed)
        self.pssw_settings.psm_set("speed.ok_sizes", kept)
        self.pssw_bar_check.setValue(self.pssw_bar_check.maximum())
        self.pssw_btn_check.setEnabled(True)
        self.pssw_label_ok.setText(f"{pms_summary}；已写入设置。")
