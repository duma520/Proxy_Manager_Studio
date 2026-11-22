#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电报频道抓取设置小部件（v1.4.4）

「抓取通路 + 抓取页数 + 外部代理 + 超时 + 连通性测试」五件套。

为什么单独成文件：settingsdialog.py 已接近 1000 行红线（项目第 8 条），
再往里塞控件会超限，因此按功能拆成独立部件，由设置对话框挂接（同 v1.4.3 的测速源部件）。
"""

from __future__ import annotations

from typing import Optional
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pms.constants import (
    PMS_DEFAULT_TELEGRAM_EXTERNAL_PROXY,
    PMS_TELEGRAM_DEFAULT_PROXY_ORDER,
    PMS_TELEGRAM_PROXY_AUTO,
    PMS_TELEGRAM_PROXY_MODE_TITLES,
    PMS_TELEGRAM_ROUTE_TITLES,
)
from pms.telegram import (
    PMS_TELEGRAM_DEFAULT_BACKFILL_DELAY,
    PMS_TELEGRAM_DEFAULT_BACKFILL_PAGES,
    PMS_TELEGRAM_FETCH_MODE_FULL,
    PMS_TELEGRAM_FETCH_MODE_INCREMENTAL,
    PMS_TELEGRAM_FETCH_MODE_TITLES,
    PMS_TELEGRAM_MAX_PAGES,
    pms_telegram_build_openers,
    pms_telegram_fetch_channel,
    pms_telegram_proxy_url,
)

# 连通性测试用的探针频道：官方频道，长期存在，不会因为某个节点频道关站而误报
PMS_TELEGRAM_PROBE_CHANNEL = "telegram"

# --------------------------------------------------------------------------------------
# 后台任务：连通性测试
# --------------------------------------------------------------------------------------
class PmsTelegramProbeSignals(QObject):
    """连通性测试 → 主线程。"""

    ptps_done = Signal(str)     # 结果文本

class PmsTelegramProbeWorker(QRunnable):
    """按当前通路顺序逐个试抓探针频道，报告哪条通路能通。"""

    def __init__(
        self,
        pms_signals: PmsTelegramProbeSignals,
        pms_openers: list,
        pms_timeout: float,
    ) -> None:
        super().__init__()
        self.ptpw_signals = pms_signals
        self.ptpw_openers = pms_openers
        self.ptpw_timeout = pms_timeout

    def run(self) -> None:  # noqa: D102
        tried: list[str] = []
        for name, opener in self.ptpw_openers:
            _text, stats = pms_telegram_fetch_channel(
                PMS_TELEGRAM_PROBE_CHANNEL, opener, 1, self.ptpw_timeout, None, None,
                pms_route=name,   # v1.9.72：带上通路名，日志可区分良性退让
            )
            count = int(stats.get("messages") or 0)
            if count > 0:
                self.ptpw_signals.ptps_done.emit(f"通路「{name}」可用：探针频道抓到 {count} 条消息。")
                return
            tried.append(name)
        self.ptpw_signals.ptps_done.emit(
            f"全部通路失败（试过：{'、'.join(tried) or '无可用通路'}）。"
            "t.me 必须走代理，请检查外部代理地址，或确认本程序已启动代理。"
        )

class PmsTelegramSettingsWidget(QWidget):
    """电报频道抓取设置：通路 / 页数 / 外部代理 / 超时 / 连通性测试。"""

    def __init__(self, pms_settings, pms_local_port: int = 0, pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.ptsw_settings = pms_settings
        self.ptsw_local_port = int(pms_local_port or 0)
        self.ptsw_probing = False
        self.ptsw_combo_mode = QComboBox()
        self.ptsw_combo_fetch_mode = QComboBox()
        self.ptsw_spin_pages = QSpinBox()
        self.ptsw_edit_external = QLineEdit()
        self.ptsw_spin_timeout = QSpinBox()
        self.ptsw_chk_to_beginning = QCheckBox("全量时翻到底（抓到频道开头）")   # v1.9.5
        self.ptsw_chk_backfill = QCheckBox("慢速回填历史（每次更新偷偷多爬几页）")   # v1.9.6
        self.ptsw_spin_backfill_pages = QSpinBox()                                  # v1.9.6
        self.ptsw_spin_backfill_delay = QSpinBox()                                  # v1.9.6
        self.ptsw_btn_probe = QPushButton("测试连通性")
        self.ptsw_label_note = QLabel("")
        self.ptsw_signals = PmsTelegramProbeSignals()
        self.ptsw_list_order = QListWidget()                                  # v1.9.73：通路次序自定义
        self.ptsw_list_order.setDragEnabled(False)
        self.ptsw_list_order.setSelectionMode(QAbstractItemView.SingleSelection)
        self.ptsw_btn_order_up = QPushButton("上移")
        self.ptsw_btn_order_down = QPushButton("下移")
        self.ptsw_btn_order_default = QPushButton("恢复默认")
        self.ptsw_build()
        self.ptsw_bind()
        self.ptsw_refresh()

    # ------------------------------------------------------------------ 构建
    def ptsw_build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        row_top = QHBoxLayout()
        row_top.setContentsMargins(0, 0, 0, 0)
        row_top.setSpacing(6)
        row_top.addWidget(QLabel("抓取通路"))
        row_top.addWidget(self.ptsw_combo_mode, 1)
        row_top.addWidget(QLabel("页数"))
        row_top.addWidget(self.ptsw_spin_pages)
        row_top.addWidget(QLabel("超时"))
        row_top.addWidget(self.ptsw_spin_timeout)
        row_top.addWidget(QLabel("秒"))
        layout.addLayout(row_top)

        row_mode = QHBoxLayout()
        row_mode.setContentsMargins(0, 0, 0, 0)
        row_mode.setSpacing(6)
        row_mode.addWidget(QLabel("抓取模式"))
        row_mode.addWidget(self.ptsw_combo_fetch_mode, 1)
        layout.addLayout(row_mode)

        # v1.9.73：通路次序自定义（仅「自动」模式生效）。用户可上下调顺序，决定退让优先级。
        row_order_head = QHBoxLayout()
        row_order_head.setContentsMargins(0, 0, 0, 0)
        row_order_head.setSpacing(6)
        row_order_head.addWidget(QLabel("通路次序（自动模式生效）"))
        row_order_head.addStretch(1)
        layout.addLayout(row_order_head)
        self.ptsw_list_order.setToolTip(
            "「自动」模式下抓取 t.me 时逐条退让的顺序：排在上面的优先。\n"
            "例如先外部代理 → 再本程序代理 → 最后直连。\n"
            "某条通路当前不可用（本程序代理未启动 / 外部代理地址无效）会自动跳过，不会卡住。"
        )
        layout.addWidget(self.ptsw_list_order)
        row_order_btns = QHBoxLayout()
        row_order_btns.setContentsMargins(0, 0, 0, 0)
        row_order_btns.setSpacing(6)
        row_order_btns.addWidget(self.ptsw_btn_order_up)
        row_order_btns.addWidget(self.ptsw_btn_order_down)
        row_order_btns.addWidget(self.ptsw_btn_order_default)
        row_order_btns.addStretch(1)
        layout.addLayout(row_order_btns)

        # v1.9.5：「翻到底」开关。只对全量模式生效，增量模式有自己的越界即停逻辑。
        self.ptsw_chk_to_beginning.setToolTip(
            "打开后，全量模式忽略上面的「页数」，一直往回翻到频道开头（遇空页自动停）。\n"
            "大频道会发几百次请求，耗时可能十几分钟；按页抓取时请保持关闭。"
        )
        layout.addWidget(self.ptsw_chk_to_beginning)

        # v1.9.6：慢速回填。大频道一次翻上千页必被限流，改成每次更新只补一小段，
        # 进度存在数据库里，下一轮接着往旧翻，翻到频道开头后自动停止、不再发请求。
        self.ptsw_chk_backfill.setToolTip(
            "打开后，每次（定时）更新在常规抓取之外，额外往回翻「每轮页数」页，\n"
            "页间随机停顿若干秒，靠很多轮把频道历史慢慢补完。\n"
            "进度记在数据库里，中断 / 重启后从上次的位置继续；翻到频道开头后自动停止。\n"
            "注意：开启期间电报频道不会删除「本次未出现」的节点（历史帖节点要保留）。"
        )
        layout.addWidget(self.ptsw_chk_backfill)

        row_backfill = QHBoxLayout()
        row_backfill.setContentsMargins(0, 0, 0, 0)
        row_backfill.setSpacing(6)
        row_backfill.addWidget(QLabel("每轮页数"))
        row_backfill.addWidget(self.ptsw_spin_backfill_pages)
        row_backfill.addWidget(QLabel("每页停顿"))
        row_backfill.addWidget(self.ptsw_spin_backfill_delay)
        row_backfill.addWidget(QLabel("秒"))
        row_backfill.addStretch(1)
        layout.addLayout(row_backfill)

        row_mid = QHBoxLayout()
        row_mid.setContentsMargins(0, 0, 0, 0)
        row_mid.setSpacing(6)
        row_mid.addWidget(QLabel("外部代理"))
        row_mid.addWidget(self.ptsw_edit_external, 1)
        row_mid.addWidget(self.ptsw_btn_probe)
        layout.addLayout(row_mid)

        self.ptsw_label_note.setObjectName("hintLabel")
        self.ptsw_label_note.setWordWrap(True)
        layout.addWidget(self.ptsw_label_note)

    def ptsw_bind(self) -> None:
        for value, title in PMS_TELEGRAM_PROXY_MODE_TITLES.items():
            self.ptsw_combo_mode.addItem(title, value)
        index = self.ptsw_combo_mode.findData(self.ptsw_settings.psm_get_str("telegram.proxy_mode", PMS_TELEGRAM_PROXY_AUTO))
        self.ptsw_combo_mode.setCurrentIndex(max(0, int(index)))
        self.ptsw_combo_mode.currentIndexChanged.connect(self.ptsw_on_mode_changed)
        # v1.9.2：抓取模式（全量 / 增量）
        for value, title in PMS_TELEGRAM_FETCH_MODE_TITLES.items():
            self.ptsw_combo_fetch_mode.addItem(title, value)
        index_mode = self.ptsw_combo_fetch_mode.findData(
            self.ptsw_settings.psm_get_str("telegram.fetch_mode", PMS_TELEGRAM_FETCH_MODE_FULL)
        )
        self.ptsw_combo_fetch_mode.setCurrentIndex(max(0, int(index_mode)))
        self.ptsw_combo_fetch_mode.currentIndexChanged.connect(self.ptsw_on_fetch_mode_changed)
        # 先设范围再赋值：QSpinBox 默认 0-99，顺序反了值会被夹住（v1.9.2：上限 10 → 50）
        self.ptsw_spin_pages.setRange(1, PMS_TELEGRAM_MAX_PAGES)
        self.ptsw_spin_pages.setKeyboardTracking(False)
        self.ptsw_spin_pages.setValue(self.ptsw_settings.psm_get_int("telegram.pages", 1))
        self.ptsw_spin_pages.valueChanged.connect(self.ptsw_on_pages_changed)
        # v1.9.5：「翻到底」开关
        self.ptsw_chk_to_beginning.setChecked(self.ptsw_settings.psm_get_bool("telegram.fetch_to_beginning", False))
        self.ptsw_chk_to_beginning.toggled.connect(self.ptsw_on_to_beginning_changed)
        # v1.9.6：慢速回填三件套（先设范围再赋值，否则值会被 QSpinBox 默认 0-99 夹住）
        self.ptsw_chk_backfill.setChecked(self.ptsw_settings.psm_get_bool("telegram.backfill_enabled", False))
        self.ptsw_chk_backfill.toggled.connect(self.ptsw_on_backfill_changed)
        self.ptsw_spin_backfill_pages.setRange(1, PMS_TELEGRAM_MAX_PAGES)
        self.ptsw_spin_backfill_pages.setKeyboardTracking(False)
        self.ptsw_spin_backfill_pages.setValue(
            self.ptsw_settings.psm_get_int("telegram.backfill_pages_per_round", PMS_TELEGRAM_DEFAULT_BACKFILL_PAGES)
        )
        self.ptsw_spin_backfill_pages.valueChanged.connect(self.ptsw_on_backfill_pages_changed)
        self.ptsw_spin_backfill_delay.setRange(0, 30)
        self.ptsw_spin_backfill_delay.setKeyboardTracking(False)
        self.ptsw_spin_backfill_delay.setValue(
            self.ptsw_settings.psm_get_int("telegram.backfill_delay_seconds", int(PMS_TELEGRAM_DEFAULT_BACKFILL_DELAY))
        )
        self.ptsw_spin_backfill_delay.valueChanged.connect(self.ptsw_on_backfill_delay_changed)
        self.ptsw_spin_timeout.setRange(3, 120)
        self.ptsw_spin_timeout.setKeyboardTracking(False)
        self.ptsw_spin_timeout.setValue(self.ptsw_settings.psm_get_int("telegram.timeout_seconds", 20))
        self.ptsw_spin_timeout.valueChanged.connect(self.ptsw_on_timeout_changed)
        self.ptsw_edit_external.setPlaceholderText(f"主机:端口，例如 {PMS_DEFAULT_TELEGRAM_EXTERNAL_PROXY}")
        self.ptsw_edit_external.setText(self.ptsw_settings.psm_get_str("telegram.external_proxy", PMS_DEFAULT_TELEGRAM_EXTERNAL_PROXY))
        self.ptsw_edit_external.textChanged.connect(self.ptsw_on_external_changed)
        # v1.9.73：通路次序控件绑定
        self.ptsw_btn_order_up.clicked.connect(self.ptsw_on_order_up)
        self.ptsw_btn_order_down.clicked.connect(self.ptsw_on_order_down)
        self.ptsw_btn_order_default.clicked.connect(self.ptsw_on_order_default)
        self.ptsw_fill_order_list()
        self.ptsw_btn_probe.clicked.connect(self.ptsw_start_probe)
        self.ptsw_signals.ptps_done.connect(self.ptsw_on_probe_done)

    def ptsw_fill_order_list(self) -> None:
        """把当前设置里的次序 token 填进列表控件（保序、去重）。"""
        from pms.telegram import pms_telegram_parse_order  # 局部导入，避免 GUI 模块顶层循环
        tokens = pms_telegram_parse_order(self.ptsw_settings.psm_get_str("telegram.proxy_order", "")) or \
            pms_telegram_parse_order(PMS_TELEGRAM_DEFAULT_PROXY_ORDER)
        self.ptsw_list_order.clear()
        for token in tokens:
            item = QListWidgetItem(PMS_TELEGRAM_ROUTE_TITLES.get(token, token))
            item.setData(0, token)
            self.ptsw_list_order.addItem(item)

    # ------------------------------------------------------------------ 刷新
    def ptsw_refresh(self) -> None:
        external = pms_telegram_proxy_url(self.ptsw_edit_external.text())
        pages = self.ptsw_spin_pages.value()
        mode = str(self.ptsw_combo_fetch_mode.currentData() or PMS_TELEGRAM_FETCH_MODE_FULL)
        if mode == PMS_TELEGRAM_FETCH_MODE_INCREMENTAL:
            notes = [f"增量模式：只抓上次最大消息号之后的新贴（按上限 {PMS_TELEGRAM_MAX_PAGES} 页翻，越过上次边界即停）"]
        elif self.ptsw_chk_to_beginning.isChecked():
            notes = ["全量 + 翻到底：忽略页数，一直往回翻到频道开头（大频道耗时可能十几分钟）"]
        else:
            notes = [f"全量模式：抓 https://t.me/s/<频道名> 最新 {pages} 页消息（每页约 18–20 条），遇到空页提前停"]
        if self.ptsw_chk_backfill.isChecked():
            notes.append(
                f"慢速回填：每次更新额外往回翻 {self.ptsw_spin_backfill_pages.value()} 页，"
                f"页间停顿约 {self.ptsw_spin_backfill_delay.value()} 秒（±30% 抖动），"
                "进度记在数据库里，翻到频道开头后自动停止"
            )
        if external:
            notes.append(f"外部代理 {external}")
        elif str(self.ptsw_edit_external.text() or "").strip():
            notes.append("外部代理地址无效（需要 主机:端口，只支持 http/https）")
        if self.ptsw_local_port:
            notes.append(f"本程序代理 127.0.0.1:{self.ptsw_local_port}")
        else:
            notes.append("未取到本程序代理端口，「本程序代理」通路不可用")
        # v1.9.73：通路次序仅在 auto 模式生效；其它预设模式忽略并置灰。
        mode = str(self.ptsw_combo_mode.currentData() or PMS_TELEGRAM_PROXY_AUTO)
        order_enabled = (mode == PMS_TELEGRAM_PROXY_AUTO)
        self.ptsw_list_order.setEnabled(order_enabled)
        self.ptsw_btn_order_up.setEnabled(order_enabled)
        self.ptsw_btn_order_down.setEnabled(order_enabled)
        self.ptsw_btn_order_default.setEnabled(order_enabled)
        if order_enabled:
            from pms.telegram import pms_telegram_parse_order  # 局部导入，避免 GUI 模块顶层循环
            order = pms_telegram_parse_order(self.ptsw_settings.psm_get_str("telegram.proxy_order", "")) or ["local", "external", "direct"]
            notes.append("通路次序（auto）" + " → ".join(PMS_TELEGRAM_ROUTE_TITLES.get(t, t) for t in order))
        else:
            notes.append("当前为预设模式，自定义「通路次序」已忽略（切回「自动」后生效）")
        self.ptsw_label_note.setText("；".join(notes) + "。")

    # ------------------------------------------------------------------ 交互
    @Slot(int)
    def ptsw_on_mode_changed(self, _pms_index: int) -> None:
        self.ptsw_settings.psm_set("telegram.proxy_mode", str(self.ptsw_combo_mode.currentData() or PMS_TELEGRAM_PROXY_AUTO))
        self.ptsw_fill_order_list()
        self.ptsw_refresh()

    @Slot(int)
    def ptsw_on_fetch_mode_changed(self, _pms_index: int) -> None:
        self.ptsw_settings.psm_set("telegram.fetch_mode", str(self.ptsw_combo_fetch_mode.currentData() or PMS_TELEGRAM_FETCH_MODE_FULL))
        self.ptsw_refresh()

    @Slot(int)
    def ptsw_on_pages_changed(self, pms_value: int) -> None:
        self.ptsw_settings.psm_set("telegram.pages", int(pms_value))
        self.ptsw_refresh()

    @Slot(bool)
    def ptsw_on_to_beginning_changed(self, pms_checked: bool) -> None:
        """v1.9.5：「翻到底」开关（实时保存）。"""
        self.ptsw_settings.psm_set("telegram.fetch_to_beginning", bool(pms_checked))
        self.ptsw_refresh()

    @Slot(bool)
    def ptsw_on_backfill_changed(self, pms_checked: bool) -> None:
        """v1.9.6：慢速回填开关（实时保存）。"""
        self.ptsw_settings.psm_set("telegram.backfill_enabled", bool(pms_checked))
        self.ptsw_refresh()

    @Slot(int)
    def ptsw_on_backfill_pages_changed(self, pms_value: int) -> None:
        """v1.9.6：每轮回填页数（实时保存）。"""
        self.ptsw_settings.psm_set("telegram.backfill_pages_per_round", int(pms_value))
        self.ptsw_refresh()

    @Slot(int)
    def ptsw_on_backfill_delay_changed(self, pms_value: int) -> None:
        """v1.9.6：回填时每页之间的停顿秒数（实时保存）。"""
        self.ptsw_settings.psm_set("telegram.backfill_delay_seconds", int(pms_value))
        self.ptsw_refresh()

    @Slot(int)
    def ptsw_on_timeout_changed(self, pms_value: int) -> None:
        self.ptsw_settings.psm_set("telegram.timeout_seconds", int(pms_value))
        self.ptsw_refresh()

    @Slot(str)
    def ptsw_on_external_changed(self, pms_text: str) -> None:
        self.ptsw_settings.psm_set("telegram.external_proxy", str(pms_text or "").strip())
        self.ptsw_refresh()

    # ------------------------------------------------------------------ 通路次序（v1.9.73）
    def ptsw_order_to_setting(self) -> None:
        """把列表控件当前顺序写回 `telegram.proxy_order`（保序、去重）。"""
        tokens = [str(self.ptsw_list_order.item(i).data(0) or "") for i in range(self.ptsw_list_order.count())]
        tokens = [t for t in tokens if t in ("local", "external", "direct")]
        self.ptsw_settings.psm_set("telegram.proxy_order", ",".join(tokens) if tokens else PMS_TELEGRAM_DEFAULT_PROXY_ORDER)

    @Slot()
    def ptsw_on_order_up(self) -> None:
        row = self.ptsw_list_order.currentRow()
        if row > 0:
            item = self.ptsw_list_order.takeItem(row)
            self.ptsw_list_order.insertItem(row - 1, item)
            self.ptsw_list_order.setCurrentRow(row - 1)
            self.ptsw_order_to_setting()
            self.ptsw_refresh()

    @Slot()
    def ptsw_on_order_down(self) -> None:
        row = self.ptsw_list_order.currentRow()
        last = self.ptsw_list_order.count() - 1
        if row >= 0 and row < last:
            item = self.ptsw_list_order.takeItem(row)
            self.ptsw_list_order.insertItem(row + 1, item)
            self.ptsw_list_order.setCurrentRow(row + 1)
            self.ptsw_order_to_setting()
            self.ptsw_refresh()

    @Slot()
    def ptsw_on_order_default(self) -> None:
        """恢复默认次序（本程序代理 → 外部代理 → 直连）。"""
        self.ptsw_settings.psm_set("telegram.proxy_order", PMS_TELEGRAM_DEFAULT_PROXY_ORDER)
        self.ptsw_fill_order_list()
        self.ptsw_refresh()

    @Slot()
    def ptsw_start_probe(self) -> None:
        if self.ptsw_probing:
            return
        openers = pms_telegram_build_openers(
            str(self.ptsw_combo_mode.currentData() or PMS_TELEGRAM_PROXY_AUTO),
            self.ptsw_local_port,
            self.ptsw_edit_external.text(),
        )
        if not openers:
            self.ptsw_label_note.setText("没有可用通路：请把通路改成「自动」或「外部代理」，并填好外部代理地址。")
            return
        self.ptsw_probing = True
        self.ptsw_btn_probe.setEnabled(False)
        self.ptsw_label_note.setText(f"正在测试连通性（探针频道 {PMS_TELEGRAM_PROBE_CHANNEL}）…")
        QThreadPool.globalInstance().start(
            PmsTelegramProbeWorker(self.ptsw_signals, openers, float(self.ptsw_spin_timeout.value()))
        )

    @Slot(str)
    def ptsw_on_probe_done(self, pms_text: str) -> None:
        self.ptsw_probing = False
        self.ptsw_btn_probe.setEnabled(True)
        self.ptsw_label_note.setText(pms_text)
