# -*- coding: utf-8 -*-
"""导入导出与订阅（剪贴板 / 文件 / 电报 / Clash / sing-box / 分享链接 / 备份）——v1.9.36 从 `pms/gui/mainwindow.py` 拆出的 mixin。

★ 拆分原则：**方法名与 `self.pmw_*` 属性名一个字都没改**，外部引用（主窗口之外用到
`pmw_*` 的 80 处）全都不用动；mixins 只是把方法按功能域挪个文件。
"""
from __future__ import annotations

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMenu, QMessageBox
from pathlib import Path
from pms.constants import (
    PMS_CORE_KIND_SINGBOX,
    PMS_PROTOCOL_HTTP,
    PMS_PROTOCOL_HYSTERIA2,
    PMS_PROTOCOL_SHADOWSOCKS,
    PMS_PROTOCOL_SOCKS,
    PMS_PROTOCOL_TG_PROXY,
    PMS_PROTOCOL_TROJAN,
    PMS_PROTOCOL_TUIC,
    PMS_PROTOCOL_VLESS,
    PMS_PROTOCOL_VMESS,
    PMS_SECURITY_NONE,
    PMS_SECURITY_REALITY,
    PMS_SECURITY_TLS,
    PMS_TRANSPORT_GRPC,
    PMS_TRANSPORT_HTTPUPGRADE,
    PMS_TRANSPORT_WS,
    PMS_TRANSPORT_XHTTP,
)
from pms.coreconfig import PmsSingBoxConfigBuilder
from pms.coreprofile import PmsCoreLaunchProfile
from pms.gui.dialogs.importdialog import PmsImportDialog
from pms.gui.dialogs.shareqr import PmsShareQrDialog
from pms.gui.dialogs.subscriptiondialog import PmsSubscriptionDialog
from pms.models import PmsProxyNode, PmsSubscriptionRecord
from pms.sharelinks import PmsShareLinkParser
from pms.telegram import (
    PMS_TELEGRAM_FETCH_MODE_FULL,
    PMS_TELEGRAM_FETCH_MODE_INCREMENTAL,
    pms_telegram_channel_from_url,
)
from pms.subkind import PMS_SUBSCRIPTION_FILTER_ALL, pms_subscription_matches_filter
from pms.telegramimport import PmsTelegramImportSignals, PmsTelegramImportWorker, pms_telegram_import_openers
from pms.utils import pms_atomic_write_text, pms_json_pretty
from typing import Any, Sequence


class _PmwImportMixin:
    """导入导出与订阅（剪贴板 / 文件 / 电报 / Clash / sing-box / 分享链接 / 备份）（25 个方法）。"""

    def pmw_open_import_dialog(self) -> None:
        """打开批量导入对话框并写入数据库。"""
        dialog = PmsImportDialog(self.pmw_settings, self, self.pmw_repo.pnr_engine)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        nodes, report = dialog.pidlg_result()
        if not nodes:
            return
        self.pmw_persist_imported_nodes(nodes, report)

    def pmw_paste_import_from_clipboard(self) -> None:
        """一键粘贴导入：读取剪贴板 → 解析 → 直接入库（免开对话框）。"""
        clipboard = QApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        if not text.strip():
            QMessageBox.information(self, "提示", "剪贴板为空，没有可导入的节点。")
            return
        happ_url = PmsShareLinkParser().pslp_happ_subscription_url(text.strip().splitlines()[0])
        if happ_url:
            self.pmw_import_happ_subscription(happ_url)
            return
        channel = pms_telegram_channel_from_url(text.strip())
        if channel:
            self.pmw_import_telegram_channel(channel)
            return
        nodes, report = self.pmw_import_classifier.ptic_import_text(text, "导入")
        skip_invalid = self.pmw_settings.psm_get_bool("advanced.import_skip_invalid", True)
        dedupe = self.pmw_settings.psm_get_bool("advanced.dedup_by_server_port", True)
        seen: set[str] = set()
        result: list[PmsProxyNode] = []
        for node in nodes:
            node.enabled = True
            node.group_name = "导入"
            if skip_invalid and node.pnode_validate():
                report.invalid += 1
                continue
            if dedupe:
                key = node.pnode_endpoint_signature()
                if key in seen:
                    report.duplicates += 1
                    continue
                seen.add(key)
            result.append(node)
        if not result:
            QMessageBox.information(self, "导入结果", "剪贴板内容未解析到可用节点。\n\n" + report.pimp_summary_text())
            return
        self.pmw_persist_imported_nodes(result, report)

    def pmw_import_telegram_channel(self, pms_channel: str) -> None:
        """粘贴到 t.me 频道地址时：抓取频道里的节点链接并直接入库（**不创建订阅**）。"""
        if not pms_telegram_import_openers(self.pmw_settings):
            QMessageBox.warning(
                self, "无可用通路",
                "电报抓取没有可用通路：请到「设置 → 电报频道」检查代理设置（本程序未启动代理 / 外部代理地址为空）",
            )
            return
        signals = PmsTelegramImportSignals(self)
        signals.ptis_message.connect(self.pmw_on_telegram_import_message)
        signals.ptis_done.connect(self.pmw_on_telegram_import_done)
        self.pmw_begin_test_progress("telegram", 0, f"正在抓取电报频道 {pms_channel}…")
        self.pmw_tcp_pool.start(PmsTelegramImportWorker(signals, self.pmw_settings, pms_channel, (), self.pmw_repo.pnr_engine))

    @Slot(str)
    def pmw_on_telegram_import_message(self, pms_text: str) -> None:
        """抓取过程提示：走状态栏与日志，不打断用户。"""
        self.pmw_show_message(pms_text)
        self.pmw_logger.info("电报抓取：%s", pms_text)

    @Slot(str, dict, str)
    def pmw_on_telegram_import_done(self, pms_text: str, pms_stats: dict, pms_error: str) -> None:
        """抓取结束：成功就走既有的「解析 → 去重 → 入库」，失败明确提示。"""
        self.pmw_end_test_progress()
        if pms_error:
            self.pmw_show_message("电报抓取失败")
            QMessageBox.warning(self, "电报抓取", pms_error)
            return
        nodes, report = self.pmw_import_classifier.ptic_import_text(pms_text, "导入")
        skip_invalid = self.pmw_settings.psm_get_bool("advanced.import_skip_invalid", True)
        dedupe = self.pmw_settings.psm_get_bool("advanced.dedup_by_server_port", True)
        seen: set[str] = set()
        result: list["PmsProxyNode"] = []
        for node in nodes:
            node.enabled = True
            node.group_name = "导入"
            if skip_invalid and node.pnode_validate():
                report.invalid += 1
                continue
            if dedupe:
                key = node.pnode_endpoint_signature()
                if key in seen:
                    report.duplicates += 1
                    continue
                seen.add(key)
            result.append(node)
        if not result:
            QMessageBox.information(self, "导入结果", "频道里没抓到可用节点。\n\n" + report.pimp_summary_text())
            return
        self.pmw_persist_imported_nodes(result, report)

    def pmw_merge_imported_nodes(self, pms_nodes: list[PmsProxyNode]) -> tuple[list[PmsProxyNode], list[PmsProxyNode]]:
        """纯逻辑：按签名与已有节点比对，返回 `(新增, 更新)`——**不弹框、不写库、不动进度条**。

        命中已有签名时**沿用原 uid 与历史数据**（创建时间、延迟、测速、排序），避免重复行。
        界面导入与接口导入（v1.6.3）共用它，保证两边去重规则完全一致。
        """
        loose = self.pmw_settings.psm_get_bool("advanced.dedup_by_server_port", True)
        existing_signatures: dict[str, PmsProxyNode] = {}
        for node in self.pmw_nodes:
            existing_signatures[node.pnode_endpoint_signature() if loose else node.pnode_signature()] = node
        added: list[PmsProxyNode] = []
        updated: list[PmsProxyNode] = []
        for node in pms_nodes:
            key = node.pnode_endpoint_signature() if loose else node.pnode_signature()
            previous = existing_signatures.get(key)
            if previous is not None:
                node.uid = previous.uid
                node.created_at = previous.created_at
                node.latency_ms = previous.latency_ms
                node.speed_bps = previous.speed_bps
                node.last_test_time = previous.last_test_time
                node.sort_index = previous.sort_index
                updated.append(node)
            else:
                node.sort_index = len(self.pmw_nodes) + len(added)
                added.append(node)
                existing_signatures[key] = node
        return added, updated

    def pmw_persist_imported_nodes(self, pms_nodes: list[PmsProxyNode], pms_report: "PmsImportReport") -> None:
        """按签名与已有节点去重 / 更新，批量写入数据库并刷新界面（对话框与一键粘贴共用）。"""
        self.pmw_begin_test_progress("import", len(pms_nodes))
        added, updated = self.pmw_merge_imported_nodes(pms_nodes)
        self.pmw_update_test_progress(len(pms_nodes))
        self.pmw_repo.pnr_upsert_nodes(added + updated)
        self.pmw_end_test_progress()
        # v1.9.72 卡顿修复：导入走增量刷新（新增走插入、更新走行级刷新），不整库重载
        if added:
            self.pmw_apply_nodes_added(added)
        if updated:
            self.pmw_apply_nodes_updated(updated)
        self.pmw_reload_groups()
        summary = f"导入完成：新增 {len(added)}，更新 {len(updated)}，跳过重复 {pms_report.duplicates}，无效 {pms_report.invalid}"
        self.pmw_show_message(summary)
        if self.pmw_settings.psm_get_bool("advanced.show_import_report", True):
            QMessageBox.information(self, "导入结果", summary + "\n\n" + pms_report.pimp_summary_text())

    def pmw_export_nodes(self) -> None:
        """导出选中（或全部）节点：分享链接 / base64 / 备份 JSON / 指定格式。"""
        nodes = self.pmw_selected_nodes() or self.pmw_visible_nodes() or self.pmw_nodes
        if not nodes:
            QMessageBox.information(self, "提示", "没有可导出的节点")
            return
        menu = QMenu(self)
        menu.addAction("导出为分享链接（.txt）", lambda: self.pmw_export_links(nodes, False))
        menu.addAction("导出为 base64 订阅（.txt）", lambda: self.pmw_export_links(nodes, True))
        menu.addAction("导出为备份 JSON（节点 + 分组 + 订阅）", self.pmw_export_backup)
        menu.addAction("导出为 Clash YAML", lambda: self.pmw_export_clash_yaml(nodes))
        menu.addAction("导出为 sing-box JSON", lambda: self.pmw_export_singbox_json(nodes))
        menu.addAction("复制到剪贴板（分享链接）", lambda: self.pmw_copy_links_to_clipboard(nodes))
        menu.exec(self.pmw_node_table.viewport().mapToGlobal(self.pmw_node_table.viewport().rect().center()))

    def pmw_export_links(self, pms_nodes: Sequence[PmsProxyNode], pms_base64: bool) -> None:
        content = self.pmw_link_writer.pslw_nodes_to_base64_text(pms_nodes) if pms_base64 else self.pmw_link_writer.pslw_nodes_to_text(pms_nodes)
        suffix = "base64" if pms_base64 else "links"
        target, _ = QFileDialog.getSaveFileName(self, "导出节点", f"proxy_nodes_{suffix}.txt", "文本文件 (*.txt)")
        if not target:
            return
        if pms_atomic_write_text(Path(target), content):
            self.pmw_show_message(f"已导出 {len(pms_nodes)} 个节点到 {target}")
        else:
            QMessageBox.warning(self, "导出失败", target)

    def pmw_copy_links_to_clipboard(self, pms_nodes: Sequence[PmsProxyNode]) -> None:
        content = self.pmw_link_writer.pslw_nodes_to_text(pms_nodes)
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(content)
        self.pmw_show_message(f"已复制 {len(pms_nodes)} 条分享链接到剪贴板")

    def pmw_export_backup(self) -> None:
        """导出完整备份 JSON。"""
        payload = self.pmw_repo.pnr_export_payload()
        target, _ = QFileDialog.getSaveFileName(self, "导出备份", "proxy_manager_backup.json", "JSON (*.json)")
        if not target:
            return
        if pms_atomic_write_text(Path(target), pms_json_pretty(payload)):
            self.pmw_show_message(f"已导出备份：{target}（节点 {len(payload['nodes'])} 个）")
        else:
            QMessageBox.warning(self, "导出失败", target)

    def pmw_build_clash_yaml(self, pms_nodes: Sequence[PmsProxyNode]) -> str:
        """**只生成** Clash 兼容 YAML 文本，不弹框、不写文件。

        v1.6.3 抽出来给对外接口用：原来的 `pmw_export_clash_yaml()` 会弹 `QFileDialog`，
        接口绝不能弹（界面线程弹模态框会把 HTTP 请求挂死，见硬规则第 26 条）。
        """
        lines = ["proxies:"]
        for node in pms_nodes:
            lines.extend(self.pmw_node_to_clash_lines(node))
        return "\n".join(lines) + "\n"

    def pmw_export_clash_yaml(self, pms_nodes: Sequence[PmsProxyNode]) -> None:
        """导出 Clash 兼容 YAML（界面用：生成 + 弹保存框）。"""
        content = self.pmw_build_clash_yaml(pms_nodes)
        target, _ = QFileDialog.getSaveFileName(self, "导出 Clash 配置", "proxy_nodes_clash.yaml", "YAML (*.yaml *.yml)")
        if not target:
            return
        if pms_atomic_write_text(Path(target), content):
            self.pmw_show_message(f"已导出 Clash YAML：{target}")

    def pmw_node_to_clash_lines(self, pms_node: PmsProxyNode) -> list[str]:
        """单个节点 → Clash proxies 片段。"""
        node = pms_node
        lines = [f"  - name: \"{node.pnode_display_name()}\"", f"    server: {node.server}", f"    port: {int(node.port)}"]
        if node.protocol == PMS_PROTOCOL_VMESS:
            lines.append("    type: vmess")
            lines.append(f"    uuid: {node.credential_uuid}")
            lines.append(f"    alterId: {int(node.alter_id)}")
            lines.append(f"    cipher: {node.vmess_security or 'auto'}")
        elif node.protocol == PMS_PROTOCOL_VLESS:
            lines.append("    type: vless")
            lines.append(f"    uuid: {node.credential_uuid}")
            if node.flow:
                lines.append(f"    flow: {node.flow}")
        elif node.protocol == PMS_PROTOCOL_TROJAN:
            lines.append("    type: trojan")
            lines.append(f"    password: \"{node.credential_password}\"")
        elif node.protocol == PMS_PROTOCOL_SHADOWSOCKS:
            lines.append("    type: ss")
            lines.append(f"    cipher: {node.method}")
            lines.append(f"    password: \"{node.credential_password}\"")
        elif node.protocol == PMS_PROTOCOL_HYSTERIA2:
            lines.append("    type: hysteria2")
            lines.append(f"    password: \"{node.credential_password}\"")
            if node.obfs:
                lines.append(f"    obfs: {node.obfs}")
                lines.append(f"    obfs-password: \"{node.obfs_password}\"")
        elif node.protocol == PMS_PROTOCOL_TUIC:
            lines.append("    type: tuic")
            lines.append(f"    uuid: {node.credential_uuid}")
            lines.append(f"    password: \"{node.credential_password}\"")
        elif node.protocol == PMS_PROTOCOL_SOCKS:
            lines.append("    type: socks5")
            if node.host:
                lines.append(f"    username: \"{node.host}\"")
                lines.append(f"    password: \"{node.credential_password}\"")
        elif node.protocol == PMS_PROTOCOL_TG_WEBPROXY:
            # v1.9.69：t.me/webproxy 本质是标准 SOCKS5（secret 即 16 字节密码），可当通用出口；
            # 面板认 Socks5 型，故导出成 socks5。tg_proxy（MTProto）不能当通用出口，静默跳过。
            lines.append("    type: socks5")
            if node.credential_password:
                lines.append(f"    password: \"{node.credential_password}\"")
        elif node.protocol == PMS_PROTOCOL_HTTP:
            lines.append(f"    type: {'https' if node.security == PMS_SECURITY_TLS else 'http'}")
            if node.host:
                lines.append(f"    username: \"{node.host}\"")
                lines.append(f"    password: \"{node.credential_password}\"")
        else:
            return []
        lines.append(f"    network: {node.transport or 'tcp'}")
        if node.security != PMS_SECURITY_NONE:
            lines.append("    tls: true")
            if node.sni:
                lines.append(f"    servername: {node.sni}")
            if node.allow_insecure:
                lines.append("    skip-cert-verify: true")
            if node.fingerprint:
                lines.append(f"    client-fingerprint: {node.fingerprint}")
        if node.security == PMS_SECURITY_REALITY:
            lines.append("    reality-opts:")
            lines.append(f"      public-key: {node.public_key}")
            lines.append(f"      short-id: \"{node.short_id}\"")
        if node.transport == PMS_TRANSPORT_WS:
            lines.append("    ws-opts:")
            lines.append(f"      path: \"{node.path or '/'}\"")
            if node.host:
                lines.append("      headers:")
                lines.append(f"        Host: {node.host}")
        elif node.transport == PMS_TRANSPORT_GRPC:
            lines.append("    grpc-opts:")
            lines.append(f"      grpc-service-name: \"{node.service_name or node.path}\"")
        elif node.transport in (PMS_TRANSPORT_HTTPUPGRADE, PMS_TRANSPORT_XHTTP):
            lines.append("    network: httpupgrade" if node.transport == PMS_TRANSPORT_HTTPUPGRADE else "    network: ws")
            lines.append("    ws-opts:")
            lines.append(f"      path: \"{node.path or '/'}\"")
        return lines

    def pmw_build_singbox_json(self, pms_nodes: Sequence[PmsProxyNode]) -> str:
        """**只生成** sing-box 出站 JSON 文本，不弹框、不写文件（v1.6.3 给对外接口用）。"""
        builder = PmsSingBoxConfigBuilder()
        outbounds: list[dict[str, Any]] = []
        for index, node in enumerate(pms_nodes):
            profile = PmsCoreLaunchProfile.pclp_from_settings(self.pmw_settings, node, PMS_CORE_KIND_SINGBOX)
            outbound = builder.psc_build_proxy_outbound(profile, pms_with_resolver=False)
            outbound["tag"] = f"proxy-{index + 1}"
            outbounds.append(outbound)
        return pms_json_pretty({"outbounds": outbounds})

    def pmw_export_singbox_json(self, pms_nodes: Sequence[PmsProxyNode]) -> None:
        """导出 sing-box 出站配置（界面用：生成 + 弹保存框）。"""
        content = self.pmw_build_singbox_json(pms_nodes)
        target, _ = QFileDialog.getSaveFileName(self, "导出 sing-box 出站", "proxy_nodes_singbox.json", "JSON (*.json)")
        if not target:
            return
        if pms_atomic_write_text(Path(target), content):
            self.pmw_show_message(f"已导出 sing-box 出站配置：{target}")

    def pmw_share_node(self) -> None:
        """显示选中节点的分享二维码。"""
        nodes = self.pmw_selected_nodes()
        if not nodes:
            QMessageBox.information(self, "提示", "请先选择一个节点")
            return
        node = nodes[0]
        link = self.pmw_link_writer.pslw_node_to_link(node)
        if not link:
            QMessageBox.warning(self, "无法生成", "当前协议不支持生成分享链接")
            return
        PmsShareQrDialog(link, node.pnode_display_name(), self).exec()

    def pmw_copy_node_link(self) -> None:
        """复制选中节点的分享链接到剪贴板（v1.6.7：无节点 / 生成失败都要明确告诉用户）。"""
        nodes = self.pmw_selected_nodes()
        if not nodes:
            QMessageBox.information(self, "提示", "请先在节点表里选中要复制的节点")
            return
        links = [link for link in (self.pmw_link_writer.pslw_node_to_link(node) for node in nodes) if link]
        if not links:
            QMessageBox.warning(self, "无法复制", f"{len(nodes)} 个节点的协议都不支持生成分享链接")
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            QMessageBox.warning(self, "无法复制", "取不到系统剪贴板")
            return
        clipboard.setText("\n".join(links))
        self.pmw_show_message(f"已复制 {len(links)} 条分享链接（可直接粘贴到其它客户端）")

    def pmw_copy_core_config(self) -> None:
        """复制当前节点的核心配置（便于排查问题）。"""
        nodes = self.pmw_selected_nodes()
        node = nodes[0] if nodes else self.pmw_current_node()
        if node is None:
            QMessageBox.information(self, "提示", "请先选择一个节点")
            return
        # v1.9.69：tg_proxy 是 MTProto 电报代理，只用于电报连通测试，没有对应核心出站类型，
        # 强行生成核心配置既会报错也没意义，直接明确提示而不崩。
        if node.protocol == PMS_PROTOCOL_TG_PROXY:
            QMessageBox.information(
                self, "不支持",
                "该节点是 Telegram MTProto 代理（t.me/proxy），仅用于电报连通测试，"
                "不支持导出核心配置。如需通用代理请用 t.me/webproxy 转换来的 TgWebProxy 节点。",
            )
            return
        core_kind, _ = self.pmw_resolve_core(node)
        if core_kind is None:
            core_kind = PMS_CORE_KIND_SINGBOX
        profile = self.pmw_build_profile(node, core_kind)
        config = self.pmw_core.pcp_build_config(profile)
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(pms_json_pretty(config))
        self.pmw_show_message(f"已复制 {core_kind} 配置到剪贴板")

    def pmw_open_subscription_dialog(self) -> None:
        """打开订阅管理对话框。"""
        if self.pmw_subscription_dialog is None:
            self.pmw_subscription_dialog = PmsSubscriptionDialog(self.pmw_settings, self.pmw_repo, self)
            self.pmw_subscription_dialog.psud_update_requested.connect(self.pmw_run_subscription_update)
            self.pmw_subscription_dialog.psud_changed.connect(self.pmw_reload_nodes)
            self.pmw_subscription.psu_progress.connect(self.pmw_subscription_dialog.psud_on_progress)
            self.pmw_subscription.psu_subscription_done.connect(self.pmw_subscription_dialog.psud_on_done)
            self.pmw_subscription.psu_finished.connect(self.pmw_subscription_dialog.psud_on_finished)
            self.pmw_subscription_dialog.finished.connect(self.pmw_on_subscription_dialog_closed)
        self.pmw_subscription_dialog.show()
        self.pmw_subscription_dialog.raise_()
        self.pmw_subscription_dialog.activateWindow()

    def pmw_import_happ_subscription(self, pms_url: str) -> None:
        """把 Happ 明文链接（happ://add/<订阅地址>）落成一条订阅并立即更新一次。

        加密形式 happ://crypt*/ 无法解密（密钥未公开），只在导入报告里给出说明。
        """
        url = str(pms_url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(self, "无法添加", "Happ 链接里没有取到 http(s) 订阅地址。")
            return
        try:
            existing = [record for record in self.pmw_repo.pnr_load_subscriptions() if record.url.strip() == url]
        except Exception as exc:  # noqa: BLE001 - 读取失败不能中断导入流程
            QMessageBox.warning(self, "无法添加", f"读取订阅列表失败：{exc}")
            return
        if existing:
            self.pmw_show_message(f"该订阅地址已存在（{existing[0].name}），直接更新")
            self.pmw_run_subscription_update(existing[:1])
            return
        record = PmsSubscriptionRecord(
            name="Happ 订阅",
            url=url,
            user_agent=self.pmw_settings.psm_get_str("subscription.user_agent", ""),
            update_interval_minutes=self.pmw_settings.psm_get_int("subscription.auto_update_interval_minutes", 360),
        )
        self.pmw_repo.pnr_upsert_subscription(record)
        # v1.9.72 卡顿修复：新增订阅是库里新增一行（新增订阅记录，顺带在节点表新建一个空分组占位）；
        # 走增量刷新（新增订阅记录节点 + 占位），不整库重载
        self.pmw_reload_groups()
        self.pmw_show_message(f"已添加 Happ 订阅，开始更新：{url}")
        self.pmw_run_subscription_update([record])

    def pmw_update_subscriptions_by_kind(self, pms_mode: str, pms_filter: str) -> None:
        """v1.9.75：按「更新筛选桶 × 抓取模式」批量更新订阅。

        `pms_filter` 取 subkind 的 PMS_SUBSCRIPTION_FILTER_*（不分格式 / 非 t.me / t.me）；
        `pms_mode` 取 telegram 的 PMS_TELEGRAM_FETCH_MODE_*（full / incremental）。
        取所有「启用且填了地址」的订阅，按 pms_filter 过滤后一次性更新（不改设置项）。
        """
        try:
            records = [record for record in self.pmw_repo.pnr_load_subscriptions() if record.enabled and record.url]
        except Exception as exc:  # noqa: BLE001 - 读库失败不能崩界面
            QMessageBox.warning(self, "无法更新", f"读取订阅列表失败：{exc}")
            return
        if pms_filter and pms_filter != PMS_SUBSCRIPTION_FILTER_ALL:
            records = [record for record in records if pms_subscription_matches_filter(record.url, pms_filter)]
        if not records:
            QMessageBox.information(self, "提示", "没有符合该分类且启用、填了地址的订阅")
            return
        self.pmw_run_subscription_update(records, pms_mode)

    def pmw_run_subscription_update(self, pms_records: Sequence[PmsSubscriptionRecord], pms_fetch_mode: str = "") -> None:
        """开始更新订阅（对话框与主界面共用）。

        `pms_fetch_mode` 是一次性抓取模式覆盖（full / incremental），空串表示沿用设置项
        `telegram.fetch_mode`；只对电报频道型订阅有意义，普通 HTTP 订阅两种模式等价。
        """
        ok, message = self.pmw_subscription.psu_start(pms_records, pms_fetch_mode)
        self.pmw_show_message(message)
        if ok:
            self.pmw_begin_test_progress("subscription", len(pms_records), message)
