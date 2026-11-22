#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tor 桥接出口设置部件（v1.9.46 = Tor 桥接出口第三期）

挂在「设置 → 核心 → Tor 桥接出口」二级标签下（`settingssubtabs.PST_SECTION_WIDGET_TITLES`
按类名映射，本部件只负责「显示 + 发动作」）。

界面四件事：

1. **开关 + 桥接类型**：`tor.enabled` / `tor.bridge_type`；
2. **桥接行**：多行文本框（一行一条，支持从电报频道 / 官网整段粘贴），
   「保存」时用 `pms/tor_bridge.py` 解析，成功的写进 `tor.bridge_lines`，
   坏行**逐行报出来**（不静默丢）；
3. **组件状态**：`tor.exe` / 插件在不在、版本多少，一键「下载 / 重新下载 Tor 组件」
   （下载与解压都在 `pms/tordownload.py`）；
4. **测试连接**：说明写死的一句话——这里跑的是**真 `sing-box check`**，
   校验「配置与组件就位」；**能不能连上 Tor 要启动核心后看日志**（引导通常 10–60 秒）。

线程约定：下载与校验都在 `QThreadPool` 里跑，结果用信号回主线程；设置只在主线程写
（工作线程写 `PmsSettingsManager` 会打出 `QObject::startTimer` 警告，v1.9.32 踩过）。
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pms.constants import PMS_CORE_KIND_SINGBOX, PMS_TELEGRAM_PROXY_AUTO
from pms.gui.widgets import pms_make_hint_label
from pms.tor_bridge import (
    PMS_TOR_BRIDGE_TITLES,
    PMS_TOR_BRIDGE_TYPES,
    ptb_binaries_state,
    ptb_core_kind_error,
    ptb_merge_bridge_lines,
    ptb_merge_unverified_lines,
    ptb_parse_bridge_line,
    ptb_parse_bridge_lines,
)
from pms.tordownload import (
    PmsTorDownloadSignals,
    PmsTorDownloadTask,
    ptd_check_config,
)


class PmsTorCheckSignals(QObject):
    """配置校验任务 → 主线程。"""

    ptc_finished = Signal(bool, str)


class PmsTorCheckTask(QRunnable):
    """真 `sing-box check` 校验（在后台线程跑，别卡住设置对话框）。"""

    def __init__(self, pms_signals: PmsTorCheckSignals, pms_settings: Any, pms_paths: Any) -> None:
        super().__init__()
        self.ptc_signals = pms_signals
        self.ptc_settings = pms_settings
        self.ptc_paths = pms_paths
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        try:
            ok, detail = ptd_check_config(self.ptc_settings, self.ptc_paths)
        except Exception as exc:  # noqa: BLE001 - 设置页里的按钮绝不能让异常逃逸
            ok, detail = False, f"校验异常：{exc}"
        self.ptc_signals.ptc_finished.emit(bool(ok), str(detail))


class PmsTorCollectSignals(QObject):
    """社区桥接抓取任务 → 主线程。"""

    ptk_progress = Signal(str, int, int)   # 说明, 已完成, 总数
    ptk_finished = Signal(dict)            # 结果包（ptb_collect_valid_bridges 的返回）


class PmsTorCollectTask(QRunnable):
    """抓采集站 → TCP 验活 → 缓存（v1.9.47）。全部在后台线程，结果回主线程。"""

    def __init__(self, pms_signals: PmsTorCollectSignals, pms_paths: Any, pms_settings: Any) -> None:
        super().__init__()
        self.ptk_signals = pms_signals
        self.ptk_paths = pms_paths
        self.ptk_settings = pms_settings
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        from pms.telegram import pms_telegram_build_openers
        from pms.torbridge import ptb_collect_valid_bridges

        openers = pms_telegram_build_openers(
            str(self.ptk_settings.psm_get_str("telegram.proxy_mode", PMS_TELEGRAM_PROXY_AUTO)),
            int(self.ptk_settings.psm_get_int("inbound.mixed_port", 0)),
            str(self.ptk_settings.psm_get_str("telegram.external_proxy", "")),
        )
        try:
            payload = ptb_collect_valid_bridges(
                self.ptk_paths, openers,
                pms_progress=lambda text, done, total: self.ptk_signals.ptk_progress.emit(str(text), int(done), int(total)),
                # v1.9.48：验活**照跑**，这个开关只决定「没验过的候选丢不丢」
                pms_keep_unverified=bool(self.ptk_settings.psm_get_bool("tor.keep_unverified", False)),
            )
        except Exception as exc:  # noqa: BLE001 - 设置页按钮绝不能让异常逃逸
            payload = {"ok": False, "lines": [], "total": 0, "failed": 0, "torrc": {}, "extra_args": [],
                       "source": "", "notes": [f"抓取异常：{exc}"], "unverified": [], "unverified_file": ""}
        self.ptk_signals.ptk_finished.emit(dict(payload))


class PmsTorBridgeWidget(QWidget):
    """Tor 桥接出口：开关 / 桥接类型 / 桥接行 / 组件状态 / 下载 / 测试连接。"""

    ptw_action = Signal(str, object)   # 与其它设置部件同款动作信号（预留给主窗口）

    def __init__(self, pms_settings: Any, pms_paths: Any, pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.ptw_settings = pms_settings
        self.ptw_paths = pms_paths
        self.ptw_download_signals = PmsTorDownloadSignals(self)
        self.ptw_download_signals.ptd_progress.connect(self.ptw_on_download_progress)
        self.ptw_download_signals.ptd_finished.connect(self.ptw_on_download_finished)
        self.ptw_check_signals = PmsTorCheckSignals(self)
        self.ptw_check_signals.ptc_finished.connect(self.ptw_on_check_finished)
        self.ptw_pool = QThreadPool.globalInstance()
        self.ptw_build_ui()
        self.ptw_load()

    # ------------------------------------------------------------------ 界面
    def ptw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        switch_group = QGroupBox("Tor 桥接出口（只支持 sing-box 核心）")
        switch_form = QGridLayout(switch_group)
        self.ptw_check_enabled = QCheckBox("启用 Tor 桥接出口（核心启动后整条链路走 Tor）")
        self.ptw_check_enabled.toggled.connect(self.ptw_on_enabled_toggled)
        switch_form.addWidget(self.ptw_check_enabled, 0, 0, 1, 3)
        switch_form.addWidget(QLabel("桥接类型"), 1, 0)
        self.ptw_combo_type = QComboBox()
        for kind in PMS_TOR_BRIDGE_TYPES:
            self.ptw_combo_type.addItem(PMS_TOR_BRIDGE_TITLES.get(kind, kind), kind)
        self.ptw_combo_type.currentIndexChanged.connect(self.ptw_on_type_changed)
        switch_form.addWidget(self.ptw_combo_type, 1, 1)
        self.ptw_status_core = QLabel("-")
        self.ptw_status_core.setObjectName("hintLabel")
        switch_form.addWidget(self.ptw_status_core, 2, 0, 1, 3)
        layout.addWidget(switch_group)

        lines_group = QGroupBox("桥接行（一行一条，可直接粘贴官网 / 电报频道的内容）")
        lines_layout = QVBoxLayout(lines_group)
        self.ptw_lines_edit = QPlainTextEdit()
        self.ptw_lines_edit.setPlaceholderText(
            "obfs4 217.77.223.2:9053 574B17CB… cert=… iat-mode=0\n"
            "webtunnel [2001:db8::1]:443 0123… url=https://tb.example/abcdef ver=0.0.1\n"
            "snowflake 192.0.2.3:1234 1A2B… url=https://snowflake.example/ front=cdn.example.net"
        )
        self.ptw_lines_edit.setMinimumHeight(120)
        lines_layout.addWidget(self.ptw_lines_edit)
        buttons = QHBoxLayout()
        self.ptw_btn_save = QPushButton("保存桥接")
        self.ptw_btn_save.clicked.connect(self.ptw_save_lines)
        buttons.addWidget(self.ptw_btn_save)
        self.ptw_btn_reload = QPushButton("从设置重新读取")
        self.ptw_btn_reload.clicked.connect(self.ptw_load)
        buttons.addWidget(self.ptw_btn_reload)
        self.ptw_btn_collect = QPushButton("从社区抓取并验证")
        self.ptw_btn_collect.setToolTip(
            "从 OnionHop / Delta-Kronecker 采集站抓 *_tested.txt，逐条 TCP 握手验活，" \
            "验活通过的合并进桥接列表（只增不删），并写入本地缓存"
        )
        self.ptw_btn_collect.clicked.connect(self.ptw_collect)
        buttons.addWidget(self.ptw_btn_collect)
        buttons.addStretch(1)
        lines_layout.addLayout(buttons)
        self.ptw_check_keep_unverified = QCheckBox("验活失败也保存候选（存为「未验活」，不会进配置）")
        self.ptw_check_keep_unverified.setToolTip(
            "打开后：抓到的桥接**照样逐条真握手验证**，没通过的另存到 " \
            "<数据目录>\\tor\\bridges_unverified.txt 与 tor.unverified_lines，界面标成「未验活、可能不可用」。" \
            "它们绝不会被拼进 torrc —— 要试就单独把某一行粘到上面的桥接框里。"
        )
        self.ptw_check_keep_unverified.toggled.connect(self.ptw_on_keep_unverified_toggled)
        lines_layout.addWidget(self.ptw_check_keep_unverified)
        self.ptw_label_unverified = QLabel("-")
        self.ptw_label_unverified.setWordWrap(True)
        self.ptw_label_unverified.setObjectName("hintLabel")
        lines_layout.addWidget(self.ptw_label_unverified)
        lines_layout.addWidget(pms_make_hint_label(
            "多桥接会自动走 sing-box 的 extra_args（torrc 是 map，重复键只能靠命令行重复出现）；" \
            "订阅抓到的桥接也会自动合并到这里（只增不删，按指纹去重）。"
        ))
        layout.addWidget(lines_group)

        state_group = QGroupBox("Tor 组件与校验")
        state_layout = QGridLayout(state_group)
        self.ptw_label_binaries = QLabel("-")
        self.ptw_label_binaries.setWordWrap(True)
        state_layout.addWidget(self.ptw_label_binaries, 0, 0, 1, 3)
        self.ptw_progress = QProgressBar()
        self.ptw_progress.setRange(0, 100)
        self.ptw_progress.setValue(0)
        state_layout.addWidget(self.ptw_progress, 1, 0, 1, 3)
        self.ptw_btn_download = QPushButton("下载 Tor 组件")
        self.ptw_btn_download.clicked.connect(lambda: self.ptw_download(False))
        state_layout.addWidget(self.ptw_btn_download, 2, 0)
        self.ptw_btn_redownload = QPushButton("重新下载")
        self.ptw_btn_redownload.clicked.connect(lambda: self.ptw_download(True))
        state_layout.addWidget(self.ptw_btn_redownload, 2, 1)
        self.ptw_btn_check = QPushButton("测试连接（校验配置与组件）")
        self.ptw_btn_check.clicked.connect(self.ptw_check)
        state_layout.addWidget(self.ptw_btn_check, 2, 2)
        state_layout.addWidget(pms_make_hint_label(
            "「测试连接」跑的是**真 sing-box check**：确认配置（含 tor 出站）与三个组件都就位。"
            "**能不能真的连上 Tor 要启动核心后看日志**——引导通常 10–60 秒，桥接不畅时会一直重试。"
        ), 3, 0, 1, 3)
        layout.addWidget(state_group)
        layout.addStretch(1)

    # ------------------------------------------------------------------ 读 / 写
    def ptw_load(self) -> None:
        """从设置读回界面（不动任何设置）。"""
        self.ptw_check_enabled.blockSignals(True)
        self.ptw_check_enabled.setChecked(bool(self.ptw_settings.psm_get_bool("tor.enabled", False)))
        self.ptw_check_enabled.blockSignals(False)
        bridge_type = str(self.ptw_settings.psm_get_str("tor.bridge_type", PMS_TOR_BRIDGE_TYPES[0]) or "")
        self.ptw_combo_type.blockSignals(True)
        index = self.ptw_combo_type.findData(bridge_type)
        self.ptw_combo_type.setCurrentIndex(index if index >= 0 else 0)
        self.ptw_combo_type.blockSignals(False)
        self.ptw_lines_edit.setPlainText("\n".join(self.ptw_settings.psm_get_list("tor.bridge_lines")))
        self.ptw_check_keep_unverified.blockSignals(True)
        self.ptw_check_keep_unverified.setChecked(bool(self.ptw_settings.psm_get_bool("tor.keep_unverified", False)))
        self.ptw_check_keep_unverified.blockSignals(False)
        self.ptw_refresh_state()

    def ptw_refresh_unverified(self) -> None:
        """显示「未验活候选」有多少条、存在哪（v1.9.48）。"""
        saved = [str(item) for item in self.ptw_settings.psm_get_list("tor.unverified_lines")]
        if not saved:
            self.ptw_label_unverified.setText("未验活候选：无（打开上面那个勾选框后，验活失败的候选会另存一份）")
            return
        try:
            from pms.torbridge import ptb_unverified_file

            target = ptb_unverified_file(self.ptw_paths)
        except Exception:  # noqa: BLE001 - 只是显示用，取不到路径也别炸界面
            target = ""
        self.ptw_label_unverified.setText(
            f"未验活候选：{len(saved)} 条（**可能不可用**，不会进配置）"
            + (f"　文件：{target}" if target else "")
        )

    def ptw_refresh_state(self) -> None:
        """刷新组件状态与「核心是哪个」提示。"""
        state = ptb_binaries_state(self.ptw_paths, self.ptw_settings)
        present = [name for name, exists in state["present"].items() if exists]
        version = str(self.ptw_settings.psm_get_str("tor.bundle_version", "") or "")
        self.ptw_label_binaries.setText(
            f"目录：{state['tor_dir']}\n"
            f"组件：{'、'.join(present) if present else '（还没有）'}"
            + (f"　缺少：{'、'.join(state['missing'])}" if state["missing"] else "")
            + (f"\n已安装版本：{version}" if version else "")
        )
        self.ptw_btn_download.setEnabled(not state["ready"])
        self.ptw_btn_redownload.setEnabled(True)
        core_kind = str(self.ptw_settings.psm_get_str("core.kind", "") or "")
        if core_kind in ("", "auto"):
            self.ptw_status_core.setText("核心选择：自动（逐节点解析；Tor 出口实际由 sing-box 承担）")
        else:
            error = ptb_core_kind_error(PMS_CORE_KIND_SINGBOX if core_kind == PMS_CORE_KIND_SINGBOX else core_kind)
            self.ptw_status_core.setText("核心选择：sing-box ✓" if not error else f"核心选择：{core_kind} —— {error}")

    def ptw_save_lines(self) -> bool:
        """解析并保存桥接行；坏行逐行报出来。返回是否写成功。"""
        raw_lines = self.ptw_lines_edit.toPlainText().splitlines()
        bridges, errors = ptb_parse_bridge_lines(raw_lines)
        if not bridges:
            QMessageBox.warning(self, "保存桥接", "没有解析出任何桥接行。" + ("\n".join(errors) if errors else ""))
            return False
        lines = [str(item["raw"]) for item in bridges]
        self.ptw_settings.psm_set_many({"tor.bridge_lines": lines}, pms_emit=True)
        self.ptw_lines_edit.setPlainText("\n".join(lines))
        self.ptw_refresh_state()
        text = f"已保存 {len(lines)} 条桥接"
        if errors:
            text += f"；另有 {len(errors)} 行解析不了：\n" + "\n".join(errors[:5])
        QMessageBox.information(self, "保存桥接", text)
        return True

    # ------------------------------------------------------------------ 动作
    def ptw_on_enabled_toggled(self, pms_checked: bool) -> None:
        if pms_checked and ptb_core_kind_error(PMS_CORE_KIND_SINGBOX) == "" and self.ptw_settings.psm_get_str("core.kind", "") not in ("", "auto", PMS_CORE_KIND_SINGBOX):
            QMessageBox.warning(self, "Tor 出口", ptb_core_kind_error(str(self.ptw_settings.psm_get_str("core.kind", ""))))
        self.ptw_settings.psm_set_many({"tor.enabled": bool(pms_checked)}, pms_emit=True)
        self.ptw_refresh_state()

    def ptw_on_type_changed(self, pms_index: int) -> None:
        kind = str(self.ptw_combo_type.itemData(pms_index) or PMS_TOR_BRIDGE_TYPES[0])
        self.ptw_settings.psm_set_many({"tor.bridge_type": kind}, pms_emit=True)
        self.ptw_refresh_state()

    def ptw_on_keep_unverified_toggled(self, pms_checked: bool) -> None:
        """「验活失败也保存候选」开关（v1.9.48）。**只是保存策略**——验活永远照跑。"""
        self.ptw_settings.psm_set_many({"tor.keep_unverified": bool(pms_checked)}, pms_emit=True)
        self.ptw_refresh_unverified()

    def ptw_download(self, pms_force: bool) -> None:
        """下载 / 重新下载 Tor 组件（后台线程，走与核心下载同一套代理退让）。"""
        self.ptw_btn_download.setEnabled(False)
        self.ptw_btn_redownload.setEnabled(False)
        self.ptw_progress.setValue(0)
        self.ptw_label_binaries.setText("正在查询 Tor 最新版本…")
        task = PmsTorDownloadTask(
            self.ptw_download_signals, self.ptw_paths, self.ptw_settings,
            int(self.ptw_settings.psm_get_int("inbound.mixed_port", 0)),
            str(self.ptw_settings.psm_get_str("telegram.external_proxy", "") or ""),
            pms_force=bool(pms_force),
        )
        self.ptw_pool.start(task)

    def ptw_check(self) -> None:
        self.ptw_btn_check.setEnabled(False)
        self.ptw_label_binaries.setText("正在用真 sing-box 校验配置…")
        self.ptw_pool.start(PmsTorCheckTask(self.ptw_check_signals, self.ptw_settings, self.ptw_paths))

    # ------------------------------------------------------------------ 社区抓取（v1.9.47）
    def ptw_collect(self) -> None:
        """抓采集站 → 验活 → 合并进桥接列表（只在主线程写设置）。"""
        self.ptw_btn_collect.setEnabled(False)
        self.ptw_label_binaries.setText("正在从采集站抓取桥接…")
        self.ptw_collect_signals = PmsTorCollectSignals(self)
        self.ptw_collect_signals.ptk_progress.connect(self.ptw_on_collect_progress)
        self.ptw_collect_signals.ptk_finished.connect(self.ptw_on_collect_finished)
        self.ptw_pool.start(PmsTorCollectTask(self.ptw_collect_signals, self.ptw_paths, self.ptw_settings))

    @Slot(str, int, int)
    def ptw_on_collect_progress(self, pms_text: str, pms_done: int, pms_total: int) -> None:
        suffix = f"（{pms_done}/{pms_total}）" if pms_total else ""
        self.ptw_label_binaries.setText(f"{pms_text}{suffix}")

    @Slot(dict)
    def ptw_on_collect_finished(self, pms_payload: dict) -> None:
        """抓取结果：验活通过的合并进设置（只增不删），未验活的另存并标注，说明摊给用户看。"""
        self.ptw_btn_collect.setEnabled(True)
        lines = [str(item) for item in (pms_payload.get("lines") or [])]
        if lines:
            existing = [str(item) for item in self.ptw_settings.psm_get_list("tor.bridge_lines")]
            merged, added = ptb_merge_bridge_lines(existing, lines)
            self.ptw_settings.psm_set_many({"tor.bridge_lines": merged}, pms_emit=True)
            self.ptw_lines_edit.setPlainText("\n".join(merged))
        unverified = [str(item) for item in (pms_payload.get("unverified") or [])]
        if unverified:
            # 只存「未验活」那一份设置键（**不混进 tor.bridge_lines**，免得悄悄进配置）；只增不删 + 去重
            saved = [str(item) for item in self.ptw_settings.psm_get_list("tor.unverified_lines")]
            merged_unverified = ptb_merge_unverified_lines(saved, unverified)
            self.ptw_settings.psm_set_many({"tor.unverified_lines": merged_unverified}, pms_emit=True)
        self.ptw_refresh_state()
        self.ptw_refresh_unverified()
        source = {"collector": "采集站", "cache": "本地缓存"}.get(str(pms_payload.get("source") or ""), "无")
        notes = [str(item) for item in (pms_payload.get("notes") or [])]
        if lines:
            head = f"候选 {pms_payload.get('total', 0)} 条，验活通过 {len(lines)} 条（来源：{source}）；已合并进桥接列表"
        else:
            head = ""
        if unverified:
            head += ("\n" if head else "") + \
                f"另有 {len(unverified)} 条**未验活**候选已存下（标记为「未验活、可能不可用」，不会进配置）"
        body = "\n".join(notes[:12])
        text = (f"{head}\n{body}" if head else body) or "没有拿到任何桥接"
        if not lines:
            text += "\n\n提示：抓不到通常是没有可用的网络通路（t.me / GitHub 都需要代理）；" \
                    "也可以手工把桥接行粘进上面的文本框。"
        (QMessageBox.information if (lines or unverified) else QMessageBox.warning)(self, "Tor 桥接抓取", text)

    # ------------------------------------------------------------------ 回调
    @Slot(int, int, str)
    def ptw_on_download_progress(self, pms_got: int, pms_total: int, pms_text: str) -> None:
        if pms_total > 0:
            self.ptw_progress.setValue(min(100, int(pms_got * 100 / pms_total)))
        self.ptw_label_binaries.setText(pms_text or "下载中…")

    @Slot(bool, str)
    def ptw_on_download_finished(self, pms_ok: bool, pms_message: str) -> None:
        self.ptw_progress.setValue(100 if pms_ok else 0)
        self.ptw_refresh_state()
        (QMessageBox.information if pms_ok else QMessageBox.warning)(self, "Tor 组件", pms_message)

    @Slot(bool, str)
    def ptw_on_check_finished(self, pms_ok: bool, pms_message: str) -> None:
        self.ptw_btn_check.setEnabled(True)
        self.ptw_refresh_state()
        (QMessageBox.information if pms_ok else QMessageBox.warning)(self, "测试连接", pms_message)


def _ptw_build(pms_dialog: Any) -> "PmsTorBridgeWidget":
    """设置对话框的挂载入口（只调这一行，本文件不进设置对话框主体逻辑）。"""
    return PmsTorBridgeWidget(pms_dialog.pset_settings, pms_dialog.pset_paths, pms_dialog)
