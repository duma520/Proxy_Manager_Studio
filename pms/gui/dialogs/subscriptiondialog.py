#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：订阅管理

订阅增删改查、立即/全部更新、流量与到期展示、更新进度。
"""

from __future__ import annotations

import logging
import urllib.request
from typing import Optional
from PySide6.QtCore import (
    QObject,
    Qt,
    QRunnable,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pms.constants import (
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_SUB_API_KEY_ENV,
    PMS_TELEGRAM_PROXY_AUTO,
    PMS_TELEGRAM_PROXY_MODE_TITLES,
    PMS_TELEGRAM_ROUTE_TITLES,
)
from pms.utils import (
    pms_now_text,
    pms_safe_int,
)
from pms.models import PmsSubscriptionRecord
from pms.database import PmsNodeRepository
from pms.githubsource import (
    PMS_GITHUB_STRONG_KEYWORDS,
    PmsGithubRepoSource,
    pms_github_repo_from_url,
)
from pms.subscription import (
    PMS_SUBSCRIPTION_KIND_GITHUB,
    PMS_SUBSCRIPTION_KIND_TELEGRAM,
    PMS_SUBSCRIPTION_KIND_TITLES,
    PMS_SUBSCRIPTION_KIND_TOR_BRIDGE,
    pms_subscription_kind,
    pms_subscription_kind_title,
    pms_subscription_record_kind_title,
)
from pms.subkind import (
    PMS_SUBSCRIPTION_FILTER_ALL,
    PMS_SUBSCRIPTION_FILTER_NON_TG,
    PMS_SUBSCRIPTION_FILTER_TG,
    pms_subscription_matches_filter,
)
from pms.telegram import (
    PMS_TELEGRAM_FETCH_MODE_FULL,
    PMS_TELEGRAM_FETCH_MODE_INCREMENTAL,
    PMS_TELEGRAM_MAX_PAGES,
    pms_telegram_build_openers,
    pms_telegram_channel_from_url,
    pms_telegram_fetch_channel,
)
from pms.tor_bridge import ptb_extract_bridges_from_text   # v1.9.44：试抓里也要能数出桥接
from pms.gui.widgets import pms_make_hint_label

# v1.9.20：订阅详情「改动即保存」的防抖毫秒数。
# 放这里而不是 constants.py——那边**顶在 1000 行红线**上，一个模块私有的节流常量不值得破线。
_PSUD_AUTOSAVE_MS = 500

# --------------------------------------------------------------------------------------
# 后台任务：电报频道「试抓」（只探测不入库，避免误导入一堆节点）
# --------------------------------------------------------------------------------------
class PmsTelegramTrialSignals(QObject):
    """试抓任务 → 主线程。"""

    ptts_finished = Signal(str, str)   # 结果文本, 错误（空串=成功）

class PmsTelegramTrialWorker(QRunnable):
    """抓一页频道，统计消息数与节点链接数，返回给人看的文本。"""

    def __init__(
        self,
        pms_signals: PmsTelegramTrialSignals,
        pms_channel: str,
        pms_openers: list,
        pms_pages: int,
        pms_timeout: float,
        pms_user_agent: str,
    ) -> None:
        super().__init__()
        self.pttw_signals = pms_signals
        self.pttw_channel = pms_channel
        self.pttw_openers = pms_openers
        self.pttw_pages = pms_pages
        self.pttw_timeout = pms_timeout
        self.pttw_user_agent = pms_user_agent
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        tried: list[str] = []
        for name, opener in self.pttw_openers:
            texts: list[str] = []
            text, stats = pms_telegram_fetch_channel(
                self.pttw_channel, opener, self.pttw_pages, self.pttw_timeout, self.pttw_user_agent, None,
                pms_text_sink=texts,   # v1.9.44：桥接频道要正文才数得出桥接
                pms_route=name,   # v1.9.72：带上通路名，日志可区分良性退让
            )
            if int(stats.get("messages") or 0) <= 0:
                tried.append(name)
                continue
            tor_lines = [item.get("raw", "") for item in ptb_extract_bridges_from_text("\n".join(texts))[0]]
            lines = [
                f"频道：{self.pttw_channel}",
                f"通路：{name}",
                f"页数：{stats.get('pages')}（设置上限 {self.pttw_pages} 页）",
                f"消息：{stats.get('messages')} 条",
                f"节点链接：{stats.get('links')} 条",
                f"Tor 桥接：{len(tor_lines)} 条",
            ]
            if tor_lines:
                lines.append("（桥接会写进「设置 → 核心 → Tor 桥接出口」，不是节点）")
                lines.append(f"桥接示例：{str(tor_lines[0])[:72]}{'…' if len(str(tor_lines[0])) > 72 else ''}")
            sample = next((line for line in str(text).splitlines() if line.strip()), "")
            if sample:
                lines.append(f"示例：{sample[:72]}{'…' if len(sample) > 72 else ''}")
            if int(stats.get("subscriptions") or 0) > 0:
                lines.append(f"疑似 http 订阅地址：{stats.get('subscriptions')} 个（本版不自动跟随）")
            self.pttw_signals.ptts_finished.emit("\n".join(lines), "")
            return
        self.pttw_signals.ptts_finished.emit(
            "", f"没抓到任何消息（试过：{'、'.join(tried) or '无可用通路'}）。t.me 需要代理，请到「设置 → 电报频道」检查代理设置。"
        )

class PmsGithubTrialSignals(QObject):
    """GitHub 仓库试抓任务 → 主线程。"""

    pgts_finished = Signal(str, str)   # 结果文本, 错误（空串=成功）

class PmsGithubTrialWorker(QRunnable):
    """按「订阅更新」同一条链路试抓仓库：查 SHA → 发现文件 → 下载 → 统计能解析出多少节点（不入库）。"""

    def __init__(
        self,
        pms_signals: PmsGithubTrialSignals,
        pms_settings: "PmsSettingsManager",
        pms_owner: str,
        pms_repo: str,
        pms_user_agent: str,
        pms_classifier: object,
    ) -> None:
        super().__init__()
        self.pgtw_signals = pms_signals
        self.pgtw_settings = pms_settings
        self.pgtw_owner = pms_owner
        self.pgtw_repo = pms_repo
        self.pgtw_user_agent = pms_user_agent
        self.pgtw_classifier = pms_classifier
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        source = PmsGithubRepoSource(
            self.pgtw_owner,
            self.pgtw_repo,
            pms_user_agent=self.pgtw_user_agent,
            pms_timeout=float(max(5, self.pgtw_settings.psm_get_int("subscription.timeout_seconds", 25))),
            pms_mirror=self.pgtw_settings.psm_get_str("core.download_mirror", ""),
        )
        outcome = source.pgs_fetch(
            urllib.request.build_opener(),
            pms_max_files=max(1, self.pgtw_settings.psm_get_int("subscription.github_max_files", 20)),
            pms_max_total_mb=max(1, self.pgtw_settings.psm_get_int("subscription.github_max_total_mb", 24)),
            pms_max_nodes=max(1, self.pgtw_settings.psm_get_int("subscription.github_max_nodes", 500)),
            pms_follow_links=self.pgtw_settings.psm_get_bool("subscription.github_follow_links", True),
        )
        if not outcome.get("ok"):
            self.pgtw_signals.pgts_finished.emit("", str(outcome.get("error") or "试抓失败"))
            return
        seen: set[str] = set()
        total = 0
        protocols: dict[str, int] = {}
        for path, text in outcome.get("chunks", []):
            nodes, report = self.pgtw_classifier.ptic_import_text(text, f"{self.pgtw_repo} 试抓")  # type: ignore[attr-defined]
            for node in nodes:
                key = node.pnode_endpoint_signature()
                if key not in seen:
                    seen.add(key)
                    total += 1
            for key, value in dict(report.by_protocol).items():
                protocols[key] = protocols.get(key, 0) + value
        lines = [
            f"仓库：{self.pgtw_owner}/{self.pgtw_repo}",
            f"最新提交：{str(outcome.get('sha') or '')[:12]}（API 调用 {outcome.get('api_calls')} 次，"
            f"下载 {int(outcome.get('bytes') or 0) / 1024:.0f} KB）",
            f"选中文件：{len(outcome.get('chunks', []))} 个",
        ]
        for path, _text in outcome.get("chunks", []):
            lines.append(f"    · {path}")
        lines.append(f"解析出节点：{total} 条（去重后；本次最多收 {self.pgtw_settings.psm_get_int('subscription.github_max_nodes', 500)} 条）")
        if protocols:
            lines.append("协议分布：" + "、".join(f"{key} {value}" for key, value in sorted(protocols.items())))
        if outcome.get("skipped"):
            lines.append("已跳过：" + "；".join(str(item) for item in outcome.get("skipped", [])[:5]))
        if outcome.get("notes"):
            lines.append("说明：" + "；".join(str(item) for item in outcome.get("notes", [])[:5]))
        self.pgtw_signals.pgts_finished.emit("\n".join(lines), "")

# --------------------------------------------------------------------------------------
# 对话框：订阅管理
# --------------------------------------------------------------------------------------
class PmsSubscriptionDialog(QDialog):
    """订阅管理：增删改查 + 立即更新（带进度）+ 流量信息展示。"""

    psud_update_requested = Signal(list, str)  # 需要更新的订阅记录, 抓取模式（空串=沿用设置；v1.9.3）
    # v1.9.20：这个信号现在**只在节点集合真的变了时**才发（删除该订阅的节点 / 连节点一起删订阅）。
    # 主窗口把它接到 `pmw_reload_nodes()`，而整表重载很贵——3 万节点实测 20 秒以上
    # （`pnr_load_all_nodes()` 9.4 s + `pntm_set_nodes()` 18.3 s），
    # 所以「改动即保存」的字段编辑一律不发它，否则改一个字就卡 20 秒。
    psud_changed = Signal()

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_repo: PmsNodeRepository,
        pms_parent: Optional[QWidget] = None,
        pms_local_proxy_port: int = 0,
    ) -> None:
        super().__init__(pms_parent)
        self.psud_settings = pms_settings
        self.psud_repo = pms_repo
        self.psud_records: list[PmsSubscriptionRecord] = []
        self.psud_local_proxy_port = int(pms_local_proxy_port or 0)
        # v1.9.20：改动即保存。下面两个状态是它的地基——
        #   psud_form_uid：表单此刻在编辑哪一条订阅（防抖还没到期就切行/关窗时，改动要落回**它**，不能落错）；
        #   psud_loading ：程序自己在填表单（选中行、重建列表）时要把信号忽略掉，否则会用旧值回写数据库。
        self.psud_form_uid = ""
        self.psud_loading = False
        self.psud_save_timer = QTimer(self)
        self.psud_save_timer.setSingleShot(True)
        self.psud_save_timer.setInterval(_PSUD_AUTOSAVE_MS)
        self.psud_save_timer.timeout.connect(self.psud_save_now)
        self.psud_trial_signals = PmsTelegramTrialSignals(self)
        self.psud_trial_signals.ptts_finished.connect(self.psud_on_trial_done)
        self.psud_github_trial_signals = PmsGithubTrialSignals(self)
        self.psud_github_trial_signals.pgts_finished.connect(self.psud_on_trial_done)
        self.setWindowTitle("订阅管理")
        self.setMinimumSize(940, 620)
        self.psud_build_ui()
        self.psud_bind_autosave()
        self.psud_reload()

    # ------------------------------------------------------------------ 界面
    def psud_build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        self.psud_table = QTableWidget(0, 8)
        self.psud_table.setHorizontalHeaderLabels(["启用", "名称", "类型", "订阅地址", "上次更新", "节点数", "流量信息", "错误"])
        self.psud_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.psud_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.psud_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 4, 5):
            self.psud_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.psud_table.verticalHeader().setVisible(False)
        self.psud_table.itemSelectionChanged.connect(self.psud_on_selection_changed)
        self.psud_table.setAlternatingRowColors(True)
        root.addWidget(self.psud_table, 1)

        edit_group = QGroupBox("订阅详情（改动即自动保存）")
        group_box_layout = QVBoxLayout(edit_group)
        group_box_layout.setContentsMargins(8, 4, 8, 6)
        group_box_layout.setSpacing(4)
        # v1.9.55：详情改成两个分页标签——用户报「上面的订阅列表太窄，下面占用太多」。
        # 原来 11 行表单整个摊开占了半屏；拆成「基本信息 / 更新选项」两页后各 5~6 行，
        # 表格能拿回约一半高度。控件属性名与「改动即保存」接线**一个都没动**（铁律 7.39）。
        self.psud_tabs = QTabWidget()
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setContentsMargins(10, 10, 10, 6)
        self.psud_name_edit = QLineEdit()
        self.psud_url_edit = QLineEdit()
        self.psud_url_edit.setPlaceholderText(
            "订阅地址 / https://t.me/<频道名> / GitHub 仓库（https://github.com/owner/repo 或 owner/repo）"
        )
        self.psud_url_edit.textChanged.connect(self.psud_refresh_source_hint)
        self.psud_btn_trial = QPushButton("试抓")
        self.psud_btn_trial.setToolTip("只抓取并统计，不写入节点；用于确认频道能通、能解析出多少节点")
        self.psud_btn_trial.clicked.connect(self.psud_trial_fetch)
        self.psud_telegram_hint = pms_make_hint_label("")
        self.psud_telegram_hint.setVisible(False)
        self.psud_enabled_check = QCheckBox("启用（纳入批量更新）")
        self.psud_user_agent_edit = QLineEdit()
        self.psud_interval_spin = QSpinBox()
        self.psud_interval_spin.setRange(10, 20160)
        self.psud_interval_spin.setSuffix(" 分钟")
        self.psud_api_key_edit = QLineEdit()
        self.psud_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.psud_api_key_edit.setPlaceholderText(f"留空则使用 .env 的 {PMS_SUB_API_KEY_ENV}")
        self.psud_proxy_check = QCheckBox("更新时走本地代理")
        self.psud_remark_edit = QLineEdit()
        self.psud_traffic_label = QLabel("-")
        # v1.9.20：自动保存状态就写在这里——不去抢 `psud_status`（那是更新进度在用的，会互相覆盖）
        self.psud_save_label = QLabel(f"改动即保存（停手 {_PSUD_AUTOSAVE_MS} 毫秒后落库）")
        self.psud_save_label.setObjectName("hintLabel")
        form.addRow("名称", self.psud_name_edit)
        url_row = QHBoxLayout()
        url_row.setContentsMargins(0, 0, 0, 0)
        url_row.setSpacing(6)
        url_row.addWidget(self.psud_url_edit, 1)
        url_row.addWidget(self.psud_btn_trial)
        url_widget = QWidget()
        url_widget.setLayout(url_row)
        form.addRow("订阅地址", url_widget)
        form.addRow("", self.psud_telegram_hint)
        form.addRow("", self.psud_enabled_check)
        form.addRow("备注", self.psud_remark_edit)
        form.addRow("流量信息", self.psud_traffic_label)
        basic_page = QWidget()
        basic_page.setLayout(form)
        # 第二页：抓取参数（User-Agent / 间隔 / API Key / 代理）——不常改，收进标签页
        form2 = QFormLayout()
        form2.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form2.setContentsMargins(10, 10, 10, 6)
        form2.addRow("User-Agent", self.psud_user_agent_edit)
        form2.addRow("自动更新间隔", self.psud_interval_spin)
        form2.addRow("API Key", self.psud_api_key_edit)
        form2.addRow("", self.psud_proxy_check)
        options_page = QWidget()
        options_page.setLayout(form2)
        self.psud_tabs.addTab(basic_page, "基本信息")
        self.psud_tabs.addTab(options_page, "抓取选项")
        group_box_layout.addWidget(self.psud_tabs)
        # 保存状态行放在标签页外：切到哪页都看得见「已自动保存」，不会被页签藏住
        group_box_layout.addWidget(self.psud_save_label)
        root.addWidget(edit_group)

        button_row = QHBoxLayout()
        self.psud_btn_new = QPushButton("新增")
        self.psud_btn_new.clicked.connect(self.psud_new_record)
        self.psud_btn_delete = QPushButton("删除")
        self.psud_btn_delete.setObjectName("dangerButton")
        self.psud_btn_delete.clicked.connect(self.psud_delete_current)
        self.psud_btn_delete_nodes = QPushButton("删除该订阅的节点")
        self.psud_btn_delete_nodes.clicked.connect(self.psud_delete_nodes_of_current)
        # v1.9.75：订阅更新「6 分类」按钮（不分格式 / 非 t.me / t.me × 全量 / 增量）。
        # 替换原「更新选中 / 更新全部 / 全量更新选中 / 增量更新选中」四个按钮：
        # 旧「更新选中（沿用设置）」与「更新全部（沿用设置）」语义靠对话框「抓取模式」设置项兜底，
        # 现统一显式分类；分类按「地址是否 t.me」筛选本对话框的全部订阅记录后一次性更新。
        def _mk_update_btn(pms_text, pms_filter, pms_mode):
            btn = QPushButton(pms_text)
            btn.setToolTip(
                f"本次按「{pms_mode_title} / {pms_filter_title}」更新本对话框里符合该分类的订阅"
                f"（一次性覆盖抓取模式，不改设置项）"
            )
            btn.clicked.connect(lambda _checked=False, f=pms_filter, m=pms_mode: self.psud_update_by_kind(f, m))
            return btn

        pms_filter_title = "全部"
        pms_mode_title = "全量"
        self.psud_btn_all_full = _mk_update_btn("全量·全部", PMS_SUBSCRIPTION_FILTER_ALL, PMS_TELEGRAM_FETCH_MODE_FULL)
        pms_mode_title = "增量"
        self.psud_btn_all_incremental = _mk_update_btn("增量·全部", PMS_SUBSCRIPTION_FILTER_ALL, PMS_TELEGRAM_FETCH_MODE_INCREMENTAL)
        pms_filter_title = "非 t.me"
        pms_mode_title = "全量"
        self.psud_btn_nontg_full = _mk_update_btn("全量·非 t.me", PMS_SUBSCRIPTION_FILTER_NON_TG, PMS_TELEGRAM_FETCH_MODE_FULL)
        pms_mode_title = "增量"
        self.psud_btn_nontg_incremental = _mk_update_btn("增量·非 t.me", PMS_SUBSCRIPTION_FILTER_NON_TG, PMS_TELEGRAM_FETCH_MODE_INCREMENTAL)
        pms_filter_title = "t.me"
        pms_mode_title = "全量"
        self.psud_btn_tg_full = _mk_update_btn("全量·t.me", PMS_SUBSCRIPTION_FILTER_TG, PMS_TELEGRAM_FETCH_MODE_FULL)
        pms_mode_title = "增量"
        self.psud_btn_tg_incremental = _mk_update_btn("增量·t.me", PMS_SUBSCRIPTION_FILTER_TG, PMS_TELEGRAM_FETCH_MODE_INCREMENTAL)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        for widget in (self.psud_btn_new, self.psud_btn_delete, self.psud_btn_delete_nodes,
                       self.psud_btn_all_full, self.psud_btn_all_incremental,
                       self.psud_btn_nontg_full, self.psud_btn_nontg_incremental,
                       self.psud_btn_tg_full, self.psud_btn_tg_incremental):
            button_row.addWidget(widget)
        button_row.addStretch(1)
        button_row.addWidget(close_button)
        root.addLayout(button_row)

        self.psud_progress = QProgressBar()
        self.psud_progress.setRange(0, 100)
        self.psud_progress.setValue(0)
        self.psud_status = QLabel("就绪")
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.psud_progress, 1)
        progress_row.addWidget(self.psud_status)
        root.addLayout(progress_row)
        root.addWidget(pms_make_hint_label(
            f"提示：API Key 建议放在项目根目录 .env 的 {PMS_SUB_API_KEY_ENV}= 中（界面填写会加密保存到数据库）。"
            "电报频道可直接填 https://t.me/&lt;频道名&gt;，程序按 /s/ 预览页抓取消息里的节点链接；"
            "抓取通路与页数在「设置 → 电报频道」里配置。"
            "订阅详情改完就存：文本框停手约半秒、勾选框一点就写库，切换订阅或关闭窗口时也会把没存完的改动补上，"
            "不再需要点「保存修改」。地址必须是 http(s) 开头（电报频道可填 https://t.me/频道名），没写完不会落库。"
        ))

    # ------------------------------------------------------------------ 改动即保存（v1.9.20）
    def psud_bind_autosave(self) -> None:
        """把「改动即保存」接上所有详情控件。

        文本 / 数字类走**防抖**（`_PSUD_AUTOSAVE_MS`，默认 500 ms）——每敲一个字就写一次库没必要，
        还会把 `updated_at` 刷得没法看；勾选类是一次点击就定，直接立刻保存。
        """
        for widget in (self.psud_name_edit, self.psud_url_edit, self.psud_user_agent_edit,
                       self.psud_api_key_edit, self.psud_remark_edit):
            widget.textChanged.connect(self.psud_schedule_save)
        self.psud_interval_spin.valueChanged.connect(self.psud_schedule_save)
        self.psud_enabled_check.toggled.connect(self.psud_save_now)
        self.psud_proxy_check.toggled.connect(self.psud_save_now)

    def psud_schedule_save(self, pms_text: str = "") -> None:
        """内容变了 → 重启防抖计时（`pms_text` 只是接信号用，Qt 会把它传进来）。"""
        if self.psud_loading:
            return
        self.psud_save_timer.start()

    def psud_save_now(self, pms_checked: bool = False) -> bool:
        """**立刻**把表单落库：防抖到期、勾选框变化、切换订阅前、关闭窗口前都会走这里。

        返回是否真的写了库（值没变、地址没填完、没选中记录都返回 False，且**不弹任何框**）。
        """
        self.psud_save_timer.stop()
        if self.psud_loading:
            return False
        return self.psud_save_current(pms_notify=False)

    def psud_flush_pending(self) -> bool:
        """把「还没到点」的改动先落库。切换订阅、重建列表、关闭窗口前必须调它，否则改动会丢。"""
        if self.psud_loading or not self.psud_save_timer.isActive():
            return False
        return self.psud_save_now()

    # ------------------------------------------------------------------ 数据
    def psud_reload(self) -> None:
        """从数据库重新加载订阅列表（**重建前先把挂起的改动落库**，不然切换/刷新会吃掉刚改的内容）。"""
        self.psud_flush_pending()
        self.psud_records = self.psud_repo.pnr_load_subscriptions()
        self.psud_table.setRowCount(len(self.psud_records))
        for row, record in enumerate(self.psud_records):
            self.psud_fill_row(row, record)
        if self.psud_records:
            self.psud_table.selectRow(0)
        else:
            self.psud_clear_form()

    def psud_fill_row(self, pms_row: int, pms_record: PmsSubscriptionRecord) -> None:
        """填充一行表格。"""
        node_count = self.psud_repo.pnr_count_nodes_of_subscription(pms_record.uid)
        # v1.9.44：Tor 桥接源没有节点，条数写在「节点数」列会误导 → 桥接条数单独显示
        if pms_safe_int(pms_record.last_bridge_count, 0) > 0 or str(pms_record.last_bridge_note or "").strip():
            count_text = f"桥接 {pms_safe_int(pms_record.last_bridge_count, 0)} 条"
        else:
            count_text = f"{node_count}（源 {pms_record.last_node_count}）"
        values = [
            "是" if pms_record.enabled else "否",
            pms_record.psr_display_name(),
            pms_subscription_record_kind_title(pms_record),
            pms_record.url,
            pms_record.last_update_time or "-",
            count_text,
            pms_record.psr_traffic_text(),
            (pms_record.last_bridge_note or pms_record.last_error or "")[:80],
        ]
        for column, text in enumerate(values):
            item = QTableWidgetItem(str(text))
            item.setData(Qt.ItemDataRole.UserRole, pms_record.uid)
            self.psud_table.setItem(pms_row, column, item)

    def psud_current_record(self) -> Optional[PmsSubscriptionRecord]:
        """当前选中的订阅。"""
        row = self.psud_table.currentRow()
        if 0 <= row < len(self.psud_records):
            return self.psud_records[row]
        return None

    def psud_clear_form(self) -> None:
        self.psud_loading = True
        self.psud_form_uid = ""
        try:
            self.psud_name_edit.clear()
            self.psud_url_edit.clear()
            self.psud_enabled_check.setChecked(True)
            self.psud_user_agent_edit.setText(self.psud_settings.psm_get_str("subscription.user_agent", PMS_DEFAULT_SUB_USER_AGENT))
            self.psud_interval_spin.setValue(360)
            self.psud_api_key_edit.clear()
            self.psud_proxy_check.setChecked(False)
            self.psud_remark_edit.clear()
            self.psud_traffic_label.setText("-")
            self.psud_save_label.setText("改动即保存（未选中订阅）")
        finally:
            self.psud_loading = False

    def psud_on_selection_changed(self) -> None:
        """选中行 → 填充详情表单。

        v1.9.20：进函数先把**上一行**没落库的改动存掉（此时表单里还是上一行的值，且 `psud_form_uid` 还指着它），
        再填新行的值。填值期间用 `psud_loading` 挡住信号，否则会用旧值回写刚选中的这条。
        """
        self.psud_flush_pending()
        record = self.psud_current_record()
        if record is None:
            self.psud_clear_form()
            return
        self.psud_loading = True
        self.psud_form_uid = record.uid
        try:
            self.psud_name_edit.setText(record.name)
            self.psud_url_edit.setText(record.url)
            self.psud_enabled_check.setChecked(record.enabled)
            self.psud_user_agent_edit.setText(record.user_agent)
            self.psud_interval_spin.setValue(int(record.update_interval_minutes or 360))
            self.psud_api_key_edit.setText(record.api_key)
            self.psud_proxy_check.setChecked(record.use_system_proxy)
            self.psud_remark_edit.setText(record.remark)
            self.psud_traffic_label.setText(record.psr_traffic_text())
            self.psud_save_label.setText("改动即保存")
        finally:
            self.psud_loading = False
        self.psud_refresh_source_hint()

    # ------------------------------------------------------------------ 源类型提示与试抓
    def psud_refresh_source_hint(self) -> None:
        """地址栏变化 → 按类型给提示（电报频道 / GitHub 仓库 / 普通订阅），并决定「试抓」是否可用。"""
        url = self.psud_url_edit.text()
        kind = pms_subscription_kind(url)
        self.psud_btn_trial.setEnabled(kind in (PMS_SUBSCRIPTION_KIND_TELEGRAM, PMS_SUBSCRIPTION_KIND_GITHUB))
        if kind == PMS_SUBSCRIPTION_KIND_GITHUB:
            owner, repo = pms_github_repo_from_url(url)
            self.psud_telegram_hint.setText(
                f"已识别为 GitHub 仓库「{owner}/{repo}」：程序用 GitHub API 动态发现仓库里的节点文件"
                f"（分片 / 时间戳命名 / base64 / JSON / YAML 都认，README 与脚本会自动忽略），合并去重后导入；"
                f"单次最多收 {pms_safe_int(self.psud_settings.psm_get_int('subscription.github_max_nodes', 500), 500, 1)} 条、"
                f"最多下 {pms_safe_int(self.psud_settings.psm_get_int('subscription.github_max_files', 20), 20, 1)} 个文件；"
                "commit SHA 没变就跳过下载。点「试抓」可先看能发现哪些文件、解析出多少节点（不入库）。"
            )
            self.psud_telegram_hint.setVisible(True)
            return
        channel = pms_telegram_channel_from_url(url)
        if not channel:
            self.psud_telegram_hint.setVisible(False)
            return
        pages = pms_safe_int(self.psud_settings.psm_get_int("telegram.pages", 1), 1, 1, PMS_TELEGRAM_MAX_PAGES)
        mode = str(self.psud_settings.psm_get_str("telegram.proxy_mode", PMS_TELEGRAM_PROXY_AUTO) or PMS_TELEGRAM_PROXY_AUTO)
        if mode == PMS_TELEGRAM_PROXY_AUTO:
            from pms.telegram import pms_telegram_parse_order  # 局部导入，避免 GUI 模块顶层循环
            _titles = PMS_TELEGRAM_ROUTE_TITLES
            _order = " → ".join(_titles.get(t, t) for t in pms_telegram_parse_order(self.psud_settings.psm_get_str("telegram.proxy_order", "")) or ["local", "external", "direct"])
            _route_text = f"通路次序（auto）{_order}"
        else:
            _route_text = f"通路「{PMS_TELEGRAM_PROXY_MODE_TITLES.get(mode, mode)}」"
        self.psud_telegram_hint.setText(
            f"已识别为电报公开频道「{channel}」：将抓取 https://t.me/s/{channel} 预览页，共 {pages} 页；"
            f"{_route_text}。点「试抓」可先确认能抓到多少节点（不入库）。"
            "**若该频道发的是 Tor 桥接行（例如 t.me/s/tor_bridges）**：桥接会合并进"
            "「设置 → 核心 → Tor 桥接出口」的桥接列表，不会变成节点（节点数保持 0）。"
        )
        self.psud_telegram_hint.setVisible(True)

    def psud_trial_fetch(self) -> None:
        """试抓当前地址：电报频道 / GitHub 仓库都只统计，不入库。"""
        url = self.psud_url_edit.text()
        kind = pms_subscription_kind(url)
        if kind == PMS_SUBSCRIPTION_KIND_GITHUB:
            self.psud_trial_fetch_github(url)
            return
        channel = pms_telegram_channel_from_url(url)
        if not channel:
            QMessageBox.information(self, "试抓", "当前地址不是电报频道或 GitHub 仓库，普通订阅直接点「立即更新」即可")
            return
        local_port = self.psud_local_proxy_port or pms_safe_int(self.psud_settings.psm_get_int("inbound.mixed_port", 0), 0)
        openers = pms_telegram_build_openers(
            self.psud_settings.psm_get_str("telegram.proxy_mode", PMS_TELEGRAM_PROXY_AUTO),
            local_port,
            self.psud_settings.psm_get_str("telegram.external_proxy", ""),
            self.psud_settings.psm_get_str("telegram.proxy_order", ""),
        )
        if not openers:
            QMessageBox.warning(self, "试抓", "没有可用通路：请到「设置 → 电报频道」检查代理设置")
            return
        self.psud_btn_trial.setEnabled(False)
        self.psud_status.setText(f"正在试抓电报频道 {channel}…")
        QThreadPool.globalInstance().start(PmsTelegramTrialWorker(
            self.psud_trial_signals,
            channel,
            openers,
            pms_safe_int(self.psud_settings.psm_get_int("telegram.pages", 1), 1, 1, PMS_TELEGRAM_MAX_PAGES),
            float(max(3, pms_safe_int(self.psud_settings.psm_get_int("telegram.timeout_seconds", 20), 20, 3, 120))),
            str(self.psud_user_agent_edit.text().strip()
                or self.psud_settings.psm_get_str("subscription.user_agent", PMS_DEFAULT_SUB_USER_AGENT)),
        ))

    @Slot(str, str)
    def psud_on_trial_done(self, pms_text: str, pms_error: str) -> None:
        """试抓结束：恢复按钮并弹出结果。"""
        kind = pms_subscription_kind(self.psud_url_edit.text())
        self.psud_btn_trial.setEnabled(kind in (PMS_SUBSCRIPTION_KIND_TELEGRAM, PMS_SUBSCRIPTION_KIND_GITHUB))
        if pms_error:
            self.psud_status.setText("试抓失败")
            QMessageBox.warning(self, "试抓失败", pms_error)
            return
        self.psud_status.setText("试抓完成（未入库）")
        QMessageBox.information(self, "试抓结果", pms_text)

    def psud_trial_fetch_github(self, pms_url: str) -> None:
        """GitHub 仓库试抓：起后台线程走真实发现链路，避免卡界面。"""
        owner, repo = pms_github_repo_from_url(pms_url)
        if not owner or not repo:
            QMessageBox.information(self, "试抓", "仓库地址格式不对（应形如 https://github.com/owner/repo 或 owner/repo）")
            return
        self.psud_btn_trial.setEnabled(False)
        self.psud_status.setText(f"正在试抓仓库 {owner}/{repo}…")
        from pms.importers import PmsTextImportClassifier

        worker = PmsGithubTrialWorker(
            self.psud_github_trial_signals,
            self.psud_settings,
            owner,
            repo,
            str(self.psud_user_agent_edit.text().strip()
                or self.psud_settings.psm_get_str("subscription.user_agent", PMS_DEFAULT_SUB_USER_AGENT)),
            PmsTextImportClassifier(),
        )
        QThreadPool.globalInstance().start(worker)

    # ------------------------------------------------------------------ 动作
    def psud_new_record(self) -> None:
        """新增一个空订阅（v1.9.20：先把当前表单挂起的改动落库，再建新记录）。"""
        self.psud_flush_pending()
        record = PmsSubscriptionRecord(
            name=f"新订阅 {len(self.psud_records) + 1}",
            user_agent=self.psud_settings.psm_get_str("subscription.user_agent", PMS_DEFAULT_SUB_USER_AGENT),
            update_interval_minutes=self.psud_settings.psm_get_int("subscription.auto_update_interval_minutes", 360),
        )
        self.psud_repo.pnr_upsert_subscription(record)
        self.psud_reload()
        self.psud_table.selectRow(max(0, len(self.psud_records) - 1))
        # v1.9.20：新增订阅不碰任何节点，**不发 psud_changed**（省掉一次几十秒的整表重载）

    def psud_form_record(self) -> Optional[PmsSubscriptionRecord]:
        """表单正在编辑的那条订阅（按 `psud_form_uid` 找，找不到再退回当前选中行）。"""
        uid = str(self.psud_form_uid or "")
        if uid:
            for record in self.psud_records:
                if record.uid == uid:
                    return record
        return self.psud_current_record()

    @staticmethod
    def psud_url_complete(pms_url: str) -> bool:
        """地址是否**已经写完整**——自动保存靠它决定要不要落库。

        v1.9.20：老版是「点保存才写库」，所以只要以 `http://` 开头就算过；
        现在停手半秒就写一次，必须把 `http://`、`https://` 这种**敲了一半**的中间态挡掉，
        否则库里会存下一堆半截地址（等更新时才报错，更难查）。
        """
        text = str(pms_url or "").strip()
        lowered = text.lower()
        for scheme in ("http://", "https://"):
            if lowered.startswith(scheme):
                return bool(text[len(scheme):].strip("/"))
        return False

    def psud_save_current(self, pms_notify: bool = False) -> bool:
        """把详情表单写进数据库——**这是订阅唯一的写入口**（v1.9.20 起由「改动即保存」自动调用）。

        `pms_notify=True` 才弹框（老版点「保存修改」的行为，现已没有按钮会这么调）；
        自动保存一律 `pms_notify=False`：**不弹任何框**，地址没写完（`http://` 这种半截地址，
        见 `psud_url_complete()`）就静静跳过，等写完整再落库——否则每敲一个字弹一次框根本没法用。
        值一个都没变时不写库（免得每次切行、每次刷新都白刷一次 `updated_at`）。返回是否真的写了。
        """
        record = self.psud_form_record()
        if record is None:
            if pms_notify:
                QMessageBox.information(self, "提示", "请先选择或新增一个订阅")
            return False
        url = self.psud_url_edit.text().strip()
        channel = pms_telegram_channel_from_url(url)
        if channel:
            url = f"https://t.me/{channel}"   # 统一成普通频道链接形态，/s/ 预览页由抓取逻辑自行拼
        elif url and not self.psud_url_complete(url):
            if pms_notify:
                QMessageBox.warning(self, "地址无效", "订阅地址必须以 http:// 或 https:// 开头（电报频道填 https://t.me/<频道名>）")
            self.psud_save_label.setText("地址没写完（需 http:// 或 https:// 开头），暂未保存")
            return False
        values = {
            "name": self.psud_name_edit.text().strip() or (channel or record.name),
            "url": url,
            "enabled": bool(self.psud_enabled_check.isChecked()),
            "user_agent": self.psud_user_agent_edit.text().strip(),
            "interval": int(self.psud_interval_spin.value()),
            "api_key": self.psud_api_key_edit.text().strip(),
            "use_system_proxy": bool(self.psud_proxy_check.isChecked()),
            "remark": self.psud_remark_edit.text().strip(),
        }
        unchanged = (
            values["name"] == record.name
            and values["url"] == record.url
            and values["enabled"] == bool(record.enabled)
            and values["user_agent"] == record.user_agent
            and values["interval"] == int(record.update_interval_minutes or 360)
            and values["api_key"] == record.api_key
            and values["use_system_proxy"] == bool(record.use_system_proxy)
            and values["remark"] == record.remark
        )
        if unchanged:
            return False
        record.name = values["name"]
        record.url = values["url"]
        record.enabled = values["enabled"]
        record.user_agent = values["user_agent"]
        record.update_interval_minutes = values["interval"]
        record.api_key = values["api_key"]
        record.use_system_proxy = values["use_system_proxy"]
        record.remark = values["remark"]
        record.updated_at = pms_now_text()
        self.psud_repo.pnr_upsert_subscription(record)
        self.psud_repo.pnr_update_subscription_name(record.uid, record.name)
        row = self.psud_table.currentRow()
        for index, item in enumerate(self.psud_records):
            if item.uid == record.uid:
                row = index
                break
        if 0 <= row < len(self.psud_records):
            self.psud_fill_row(row, record)
        self.psud_save_label.setText(f"已自动保存 {pms_now_text()}")
        # v1.9.20：**故意不发 psud_changed**——它会让主窗口重建节点表，
        # 3 万节点实测 20 秒以上（见信号声明处）。字段改动不影响节点集合，
        # 名字关联已由上面的 `pnr_update_subscription_name()` 直接写进 `pms_nodes`，
        # 节点表里的「订阅」列会在下一次正常重载时刷新。
        return True

    def accept(self) -> None:
        """关闭窗口前把挂起的改动补存（防抖还没到点就点了关闭/右上角 X）。"""
        self.psud_flush_pending()
        super().accept()

    def reject(self) -> None:
        self.psud_flush_pending()
        super().reject()

    def psud_delete_current(self) -> None:
        """删除订阅（可选择是否连同节点）。"""
        self.psud_flush_pending()
        record = self.psud_current_record()
        if record is None:
            return
        answer = QMessageBox.question(
            self, "确认删除",
            f"确定删除订阅「{record.psr_display_name()}」吗？\n同时删除该订阅下的节点？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return
        removed = 0
        if answer == QMessageBox.StandardButton.Yes:
            removed = self.psud_repo.pnr_delete_by_subscription(record.uid)
        self.psud_repo.pnr_delete_subscription(record.uid)
        self.psud_reload()
        self.psud_status.setText("已删除订阅")
        # v1.9.20：只有**真删掉节点**才需要主窗口重建节点表；只删订阅记录时节点还在，不必花那几十秒
        if removed:
            self.psud_changed.emit()

    def psud_delete_nodes_of_current(self) -> None:
        self.psud_flush_pending()
        record = self.psud_current_record()
        if record is None:
            return
        removed = self.psud_repo.pnr_delete_by_subscription(record.uid)
        record.last_node_count = 0
        self.psud_repo.pnr_upsert_subscription(record)
        self.psud_reload()
        self.psud_status.setText(f"已删除 {removed} 个节点")
        # v1.9.20：没删到东西就不用重载节点表（重载一次 20 秒以上，见信号声明处）
        if removed:
            self.psud_changed.emit()

    def psud_update_by_kind(self, pms_filter: str, pms_mode: str) -> None:
        """v1.9.75：按「更新筛选桶 × 抓取模式」更新本对话框里的订阅。

        `pms_filter` 取 subkind 的 PMS_SUBSCRIPTION_FILTER_*；`pms_mode` 取 telegram 的
        PMS_TELEGRAM_FETCH_MODE_*。筛选本对话框记录后一次性更新（不改设置项）。
        """
        self.psud_flush_pending()
        records = [record for record in self.psud_records if record.url]
        if pms_filter and pms_filter != PMS_SUBSCRIPTION_FILTER_ALL:
            records = [record for record in records if pms_subscription_matches_filter(record.url, pms_filter)]
        if not records:
            QMessageBox.information(self, "提示", "没有符合该分类的订阅")
            return
        self.psud_update_requested.emit(records, pms_mode)

    def psud_on_progress(self, pms_done: int, pms_total: int, pms_name: str) -> None:
        """更新进度回调（由主窗口转发）。"""
        self.psud_progress.setValue(int(pms_done * 100 / max(1, pms_total)))
        self.psud_status.setText(f"{pms_done}/{pms_total}：{pms_name}")

    def psud_on_done(self, pms_uid: str, pms_result: dict) -> None:
        """单个订阅完成：刷新表格行。"""
        for row, record in enumerate(self.psud_records):
            if record.uid == str(pms_uid):
                if pms_result.get("ok"):
                    record.last_update_time = pms_now_text()
                    record.last_node_count = pms_safe_int(pms_result.get("node_count"), 0)
                    record.last_error = ""
                    tor = pms_result.get("tor_bridges") or {}
                    if tor:
                        # v1.9.44：桥接源的结果也要落到这一行（否则关掉对话框后行上的
                        # 「桥接 N 条」会被这次 upsert 洗回 0）
                        record.last_bridge_count = pms_safe_int(tor.get("total"), 0)
                        record.last_bridge_note = str(tor.get("note") or "")[:200]
                    traffic = pms_result.get("traffic") or {}
                    record.traffic_upload = pms_safe_int(traffic.get("upload"), record.traffic_upload)
                    record.traffic_download = pms_safe_int(traffic.get("download"), record.traffic_download)
                    record.traffic_total = pms_safe_int(traffic.get("total"), record.traffic_total)
                    record.traffic_expire = pms_safe_int(traffic.get("expire"), record.traffic_expire)
                    # 第 11/13 项：保留订阅流量头原始内容，供排查（不同机场 header 格式差异大）。
                    record.traffic_raw = str(traffic.get("raw_header", "") or "")
                else:
                    record.last_error = str(pms_result.get("error") or "未知错误")
                record.updated_at = pms_now_text()
                self.psud_repo.pnr_upsert_subscription(record)
                self.psud_fill_row(row, record)
                if row == self.psud_table.currentRow():
                    self.psud_traffic_label.setText(record.psr_traffic_text())
                break

    def psud_on_finished(self, pms_summary: dict) -> None:
        self.psud_progress.setValue(100)
        self.psud_status.setText(
            f"完成：成功 {pms_summary.get('success', 0)} / 失败 {pms_summary.get('failed', 0)}，"
            f"新增 {pms_summary.get('added', 0)}"
        )
