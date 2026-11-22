#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：批量导入

粘贴/文件/剪贴板/订阅 URL 导入，格式识别、去重、统计与进度。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from PySide6.QtCore import QTimer, QThreadPool, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
import urllib.error
import urllib.parse
import urllib.request

from pms.constants import (
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_ENV_FILENAME,
    PMS_SUB_API_KEY_ENV,
)
from pms.utils import pms_read_text_smart
from pms.envstore import PmsEnvStore
from pms.models import PmsProxyNode
from pms.sharelinks import (
    PmsImportReport,
    PmsShareLinkWriter,
)
from pms.importers import PmsTextImportClassifier
from pms.telegram import pms_telegram_channel_from_url
from pms.telegramimport import (
    PMS_TELEGRAM_IMPORT_MAX_SUBS,
    PmsTelegramImportSignals,
    PmsTelegramImportWorker,
    pms_telegram_import_openers,
)

# --------------------------------------------------------------------------------------
# 对话框：批量导入
# --------------------------------------------------------------------------------------
class PmsImportDialog(QDialog):
    """批量导入：粘贴文本 / 选择文件 / 剪贴板 / 直接下载订阅地址，全部带解析统计与进度。"""

    def __init__(self, pms_settings: "PmsSettingsManager", pms_parent: Optional[QWidget] = None, pms_engine: Any = None) -> None:
        super().__init__(pms_parent)
        self.pidlg_settings = pms_settings
        self.pidlg_engine = pms_engine   # v1.9.2：电报增量模式读写 pms_meta 用；为 None 时退化为全量
        self.pidlg_classifier = PmsTextImportClassifier()
        self.pidlg_writer = PmsShareLinkWriter()
        self.pidlg_nodes: list[PmsProxyNode] = []
        self.pidlg_report = PmsImportReport()
        self.pidlg_progress_timer = QTimer(self)
        self.pidlg_progress_timer.timeout.connect(self.pidlg_tick_progress)
        self.pidlg_progress_value = 0
        # 电报频道抓取：走工作线程（可能翻多页），不阻塞界面
        self.pidlg_pool = QThreadPool(self)
        self.pidlg_telegram_signals = PmsTelegramImportSignals(self)
        self.pidlg_telegram_signals.ptis_message.connect(self.pidlg_on_telegram_message)
        self.pidlg_telegram_signals.ptis_done.connect(self.pidlg_on_telegram_done)
        self.pidlg_telegram_links = ""          # 频道抓到的节点链接（继续抓订阅后与之合并）
        self.pidlg_telegram_subs: list[str] = []  # 频道里发现的订阅地址
        self.pidlg_telegram_running = False
        self.setWindowTitle("批量导入节点 / 订阅")
        self.setMinimumSize(820, 620)
        self.pidlg_build_ui()

    def pidlg_build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        source_row = QHBoxLayout()
        self.pidlg_btn_file = QPushButton("选择文件…")
        self.pidlg_btn_file.clicked.connect(self.pidlg_load_file)
        self.pidlg_btn_clipboard = QPushButton("读取剪贴板")
        self.pidlg_btn_clipboard.clicked.connect(self.pidlg_load_clipboard)
        self.pidlg_btn_classify = QPushButton("识别并解析")
        self.pidlg_btn_classify.setObjectName("primaryButton")
        self.pidlg_btn_classify.clicked.connect(self.pidlg_parse_text)
        self.pidlg_url_edit = QLineEdit()
        self.pidlg_url_edit.setPlaceholderText(
            "或直接填订阅地址（http/https），或电报频道（https://t.me/频道名），点「下载 / 抓取」"
        )
        self.pidlg_btn_url = QPushButton("下载 / 抓取")
        self.pidlg_btn_url.setToolTip("http(s) 订阅地址：直接下载解析；t.me 频道：抓取频道消息里的节点链接（不建订阅）")
        self.pidlg_btn_url.clicked.connect(self.pidlg_download_url)
        self.pidlg_btn_follow = QPushButton("继续抓 0 个订阅地址")
        self.pidlg_btn_follow.setToolTip("频道消息里发现的订阅地址，抓下来一起导入（最多 5 个）")
        self.pidlg_btn_follow.setEnabled(False)
        self.pidlg_btn_follow.clicked.connect(self.pidlg_fetch_telegram_subscriptions)
        for widget in (self.pidlg_btn_file, self.pidlg_btn_clipboard, self.pidlg_btn_classify):
            source_row.addWidget(widget)
        source_row.addWidget(self.pidlg_url_edit, 1)
        source_row.addWidget(self.pidlg_btn_url)
        source_row.addWidget(self.pidlg_btn_follow)
        root.addLayout(source_row)

        self.pidlg_text = QPlainTextEdit()
        self.pidlg_text.setPlaceholderText(
            "在此粘贴：\n"
            "  · 分享链接（每行一个：vmess:// vless:// trojan:// ss:// hy2:// tuic:// socks:// http://）\n"
            "  · base64 订阅正文\n"
            "  · sing-box JSON 配置 / Clash YAML 配置"
        )
        root.addWidget(self.pidlg_text, 1)

        option_group = QGroupBox("导入选项")
        option_layout = QGridLayout(option_group)
        self.pidlg_group_combo = QComboBox()
        self.pidlg_group_combo.setEditable(True)
        self.pidlg_group_combo.addItems(["导入", "默认分组", "订阅"])
        self.pidlg_dedupe_check = QCheckBox("按地址去重（协议+服务器+端口）")
        self.pidlg_dedupe_check.setChecked(self.pidlg_settings.psm_get_bool("advanced.dedup_by_server_port", True))
        self.pidlg_skip_invalid_check = QCheckBox("跳过无效行（推荐）")
        self.pidlg_skip_invalid_check.setChecked(self.pidlg_settings.psm_get_bool("advanced.import_skip_invalid", True))
        self.pidlg_enabled_check = QCheckBox("导入后默认启用")
        self.pidlg_enabled_check.setChecked(True)
        option_layout.addWidget(QLabel("导入到分组"), 0, 0)
        option_layout.addWidget(self.pidlg_group_combo, 0, 1)
        option_layout.addWidget(self.pidlg_dedupe_check, 0, 2)
        option_layout.addWidget(self.pidlg_skip_invalid_check, 1, 0)
        option_layout.addWidget(self.pidlg_enabled_check, 1, 2)
        root.addWidget(option_group)

        self.pidlg_summary = QPlainTextEdit()
        self.pidlg_summary.setReadOnly(True)
        self.pidlg_summary.setMaximumHeight(140)
        self.pidlg_summary.setPlaceholderText("解析结果会显示在这里")
        root.addWidget(self.pidlg_summary)

        progress_row = QHBoxLayout()
        self.pidlg_progress = QProgressBar()
        self.pidlg_progress.setRange(0, 100)
        self.pidlg_progress.setValue(0)
        self.pidlg_status_label = QLabel("待解析")
        progress_row.addWidget(self.pidlg_progress, 1)
        progress_row.addWidget(self.pidlg_status_label)
        root.addLayout(progress_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.pidlg_ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.pidlg_ok_button.setText("导入到节点列表")
        self.pidlg_ok_button.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.pidlg_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------ 数据来源
    def pidlg_load_file(self) -> None:
        """从文件加载文本（自动识别编码）。"""
        target, _ = QFileDialog.getOpenFileName(
            self, "选择节点文件", "", "文本 / 配置 (*.txt *.json *.yaml *.yml *.conf *.list);;所有文件 (*.*)"
        )
        if not target:
            return
        text = pms_read_text_smart(Path(target))
        if not text.strip():
            QMessageBox.warning(self, "文件为空", target)
            return
        self.pidlg_text.setPlainText(text)
        self.pidlg_status_label.setText(f"已载入文件：{Path(target).name}")
        self.pidlg_parse_text()

    def pidlg_load_clipboard(self) -> None:
        """从剪贴板加载。"""
        clipboard = QApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        if not text.strip():
            QMessageBox.information(self, "提示", "剪贴板为空")
            return
        self.pidlg_text.setPlainText(text)
        self.pidlg_parse_text()

    def pidlg_download_url(self) -> None:
        """直接下载订阅地址并解析；t.me 频道走抓取（真实网络请求，失败给出明确原因）。"""
        url = self.pidlg_url_edit.text().strip()
        if not url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(self, "地址无效", "请填写以 http:// 或 https:// 开头的订阅地址，或 https://t.me/频道名")
            return
        channel = pms_telegram_channel_from_url(url)
        if channel:
            self.pidlg_fetch_telegram_channel(channel)
            return
        user_agent = self.pidlg_settings.psm_get_str("subscription.user_agent", PMS_DEFAULT_SUB_USER_AGENT)
        api_key = PmsEnvStore(Path(PMS_ENV_FILENAME)).pes_get(PMS_SUB_API_KEY_ENV, "")
        headers = {"User-Agent": user_agent, "Accept": "*/*", "Accept-Encoding": "identity"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.pidlg_start_progress("正在下载订阅内容…")
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=max(5, self.pidlg_settings.psm_get_int("subscription.timeout_seconds", 25))) as response:
                payload = response.read(8 * 1024 * 1024)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.pidlg_stop_progress("下载失败")
            QMessageBox.warning(self, "下载失败", str(exc))
            return
        text = ""
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
            try:
                text = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        self.pidlg_text.setPlainText(text)
        self.pidlg_stop_progress("下载完成")
        self.pidlg_parse_text()

    # ------------------------------------------------------------------ 电报频道抓取
    def pidlg_fetch_telegram_channel(self, pms_channel: str) -> None:
        """抓取电报频道里的节点链接：**只导入节点，不创建订阅记录**（工作线程，不卡界面）。"""
        if self.pidlg_telegram_running:
            return
        if not pms_telegram_import_openers(self.pidlg_settings):
            QMessageBox.warning(
                self, "无可用通路",
                "电报抓取没有可用通路：请到「设置 → 电报频道」检查代理设置（本程序未启动代理 / 外部代理地址为空）",
            )
            return
        self.pidlg_telegram_links = ""
        self.pidlg_telegram_subs = []
        self.pidlg_telegram_running = True
        self.pidlg_btn_url.setEnabled(False)
        self.pidlg_btn_follow.setEnabled(False)
        self.pidlg_start_progress(f"正在抓取电报频道 {pms_channel}…")
        self.pidlg_pool.start(PmsTelegramImportWorker(self.pidlg_telegram_signals, self.pidlg_settings, pms_channel, (), self.pidlg_engine))

    def pidlg_fetch_telegram_subscriptions(self) -> None:
        """把频道里发现的订阅地址也抓下来，与频道链接合并后一起解析。"""
        if self.pidlg_telegram_running or not self.pidlg_telegram_subs:
            return
        self.pidlg_telegram_running = True
        self.pidlg_btn_follow.setEnabled(False)
        self.pidlg_btn_url.setEnabled(False)
        count = min(len(self.pidlg_telegram_subs), PMS_TELEGRAM_IMPORT_MAX_SUBS)
        self.pidlg_start_progress(f"正在抓取 {count} 个订阅地址…")
        self.pidlg_pool.start(
            PmsTelegramImportWorker(self.pidlg_telegram_signals, self.pidlg_settings, "", self.pidlg_telegram_subs)
        )

    def pidlg_refresh_follow_button(self) -> None:
        """按「频道里发现的订阅地址数量」刷新「继续抓」按钮。"""
        count = min(len(self.pidlg_telegram_subs), PMS_TELEGRAM_IMPORT_MAX_SUBS)
        self.pidlg_btn_follow.setText(f"继续抓 {count} 个订阅地址")
        self.pidlg_btn_follow.setEnabled(count > 0 and not self.pidlg_telegram_running)

    @Slot(str)
    def pidlg_on_telegram_message(self, pms_text: str) -> None:
        """抓取过程提示（哪条通路、抓到多少）。"""
        self.pidlg_status_label.setText(pms_text)

    @Slot(str, dict, str)
    def pidlg_on_telegram_done(self, pms_text: str, pms_stats: dict, pms_error: str) -> None:
        """抓取结束：成功就把链接文本填进文本框并解析，失败给出明确原因。"""
        self.pidlg_telegram_running = False
        self.pidlg_btn_url.setEnabled(True)
        if pms_error:
            self.pidlg_stop_progress("抓取失败")
            QMessageBox.warning(self, "电报抓取", pms_error)
            self.pidlg_refresh_follow_button()
            return
        found = [str(item) for item in (pms_stats.get("subscriptions_list") or []) if str(item).strip()]
        if found:
            self.pidlg_telegram_subs = found
        if pms_text.strip():
            if not self.pidlg_telegram_links:
                self.pidlg_telegram_links = pms_text
            # 「继续抓」回来的链接要和频道链接合并，不能把上一轮结果冲掉
            merged = "\n".join(block for block in (self.pidlg_telegram_links, pms_text) if block.strip())
            self.pidlg_text.setPlainText(merged)
            self.pidlg_stop_progress("抓取完成，正在解析…")
            self.pidlg_parse_text()
        self.pidlg_refresh_follow_button()

    # ------------------------------------------------------------------ 解析
    def pidlg_start_progress(self, pms_text: str) -> None:
        """不确定进度时用滚动条效果（真实定时推进，不假装完成）。"""
        self.pidlg_status_label.setText(pms_text)
        self.pidlg_progress_value = 0
        self.pidlg_progress.setRange(0, 0)
        self.pidlg_progress_timer.start(200)

    def pidlg_tick_progress(self) -> None:
        self.pidlg_status_label.setText(self.pidlg_status_label.text().rstrip(".") + ".")

    def pidlg_stop_progress(self, pms_text: str) -> None:
        self.pidlg_progress_timer.stop()
        self.pidlg_progress.setRange(0, 100)
        self.pidlg_progress.setValue(100)
        self.pidlg_status_label.setText(pms_text)

    def pidlg_parse_text(self) -> None:
        """识别格式并解析，显示统计。"""
        text = self.pidlg_text.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "提示", "请先粘贴内容或选择文件")
            return
        self.pidlg_start_progress("正在解析…")
        kind = self.pidlg_classifier.ptic_classify(text)
        group = self.pidlg_group_combo.currentText().strip() or "导入"
        nodes, report = self.pidlg_classifier.ptic_import_text(text, group)
        nodes = self.pidlg_apply_options(nodes, report)
        self.pidlg_nodes = nodes
        self.pidlg_report = report
        lines = [
            f"识别格式：{kind}",
            report.pimp_summary_text(),
            "",
            "预览（最多 20 条）：",
        ]
        for node in nodes[:20]:
            lines.append(f"  · {node.pnode_display_type():<16} {node.pnode_display_name()}  {node.pnode_display_address()}")
        if len(nodes) > 20:
            lines.append(f"  … 其余 {len(nodes) - 20} 条已省略")
        self.pidlg_summary.setPlainText("\n".join(lines))
        self.pidlg_stop_progress(f"解析完成：{len(nodes)} 个可用节点")
        self.pidlg_ok_button.setEnabled(bool(nodes))

    def pidlg_apply_options(self, pms_nodes: list[PmsProxyNode], pms_report: PmsImportReport) -> list[PmsProxyNode]:
        """应用去重 / 跳过无效 / 默认启用等选项。"""
        enabled = bool(self.pidlg_enabled_check.isChecked())
        group = self.pidlg_group_combo.currentText().strip() or "导入"
        result: list[PmsProxyNode] = []
        seen: set[str] = set()
        for node in pms_nodes:
            node.enabled = enabled
            node.group_name = group
            if self.pidlg_skip_invalid_check.isChecked():
                errors = node.pnode_validate()
                if errors:
                    pms_report.invalid += 1
                    continue
            if self.pidlg_dedupe_check.isChecked():
                key = node.pnode_endpoint_signature()
                if key in seen:
                    pms_report.duplicates += 1
                    continue
                seen.add(key)
            result.append(node)
        for index, node in enumerate(result):
            node.sort_index = index
        return result

    def pidlg_accept(self) -> None:
        if not self.pidlg_nodes:
            QMessageBox.information(self, "提示", "尚未解析到可用节点")
            return
        self.accept()

    def pidlg_result(self) -> tuple[list[PmsProxyNode], PmsImportReport]:
        return list(self.pidlg_nodes), self.pidlg_report
