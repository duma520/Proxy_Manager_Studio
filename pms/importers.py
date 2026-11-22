#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置导入

Clash YAML / sing-box JSON 导入，以及“文本 → 格式识别 → 节点”的分类入口。
"""

from __future__ import annotations

from typing import (
    Any,
    Optional,
)
import json
import logging
import re
import uuid

from pms.optional import (
    PMS_HAS_YAML,
    pms_optional_yaml,
)
from pms.constants import (
    PMS_LOGGER_NAME,
    PMS_PROTOCOL_HTTP,
    PMS_PROTOCOL_HYSTERIA2,
    PMS_PROTOCOL_SHADOWSOCKS,
    PMS_PROTOCOL_SOCKS,
    PMS_PROTOCOL_TITLES,
    PMS_PROTOCOL_TROJAN,
    PMS_PROTOCOL_TUIC,
    PMS_PROTOCOL_VLESS,
    PMS_PROTOCOL_VMESS,
    PMS_SECURITY_NONE,
    PMS_SECURITY_REALITY,
    PMS_SECURITY_TLS,
    PMS_TRANSPORT_ALIASES,
    PMS_TRANSPORT_GRPC,
    PMS_TRANSPORT_H2,
    PMS_TRANSPORT_HTTP,
    PMS_TRANSPORT_HTTPUPGRADE,
    PMS_TRANSPORT_TCP,
    PMS_TRANSPORT_WS,
    PMS_TRANSPORT_XHTTP,
)
from pms.utils import (
    pms_b64_decode_loose,
    pms_looks_like_base64,
    pms_safe_bool,
    pms_safe_int,
)
from pms.models import PmsProxyNode
from pms.sharelinks import (
    PmsImportReport,
    PmsShareLinkParser,
)

class PmsClashYamlImporter:
    """Clash / mihomo 配置导入（proxies 段）。"""

    def pcyi_import_text(self, pms_text: str) -> list[PmsProxyNode]:
        """从 YAML 文本导入；缺少 PyYAML 时返回空列表（日志会说明原因）。"""
        if not PMS_HAS_YAML:
            logging.getLogger(PMS_LOGGER_NAME + ".import").warning("未安装 PyYAML，无法导入 Clash 配置")
            return []
        try:
            payload = pms_optional_yaml.safe_load(pms_text)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(PMS_LOGGER_NAME + ".import").error("Clash YAML 解析失败：%s", exc)
            return []
        if not isinstance(payload, dict):
            return []
        return self.pcyi_import_payload(payload)

    def pcyi_import_payload(self, pms_payload: dict[str, Any]) -> list[PmsProxyNode]:
        proxies = pms_payload.get("proxies") or pms_payload.get("Proxy") or []
        nodes: list[PmsProxyNode] = []
        if not isinstance(proxies, list):
            return nodes
        for item in proxies:
            if not isinstance(item, dict):
                continue
            node = self.pcyi_proxy_to_node(item)
            if node is not None:
                nodes.append(node)
        return nodes

    def pcyi_proxy_to_node(self, pms_item: dict[str, Any]) -> Optional[PmsProxyNode]:
        """把一条 Clash proxy 转成节点。"""
        kind = str(pms_item.get("type", "")).lower()
        server = str(pms_item.get("server", "")).strip()
        if not server:
            return None
        port = pms_safe_int(pms_item.get("port"), 443, 0, 65535)
        node: Optional[PmsProxyNode] = None
        if kind == "vmess":
            node = PmsProxyNode(protocol=PMS_PROTOCOL_VMESS)
            node.credential_uuid = str(pms_item.get("uuid", ""))
            node.alter_id = pms_safe_int(pms_item.get("alterId"), 0, 0, 65535)
            node.vmess_security = str(pms_item.get("cipher", "auto")).lower()
        elif kind == "vless":
            node = PmsProxyNode(protocol=PMS_PROTOCOL_VLESS)
            node.credential_uuid = str(pms_item.get("uuid", ""))
            node.flow = str(pms_item.get("flow", "") or "")
        elif kind == "trojan":
            node = PmsProxyNode(protocol=PMS_PROTOCOL_TROJAN)
            node.credential_password = str(pms_item.get("password", ""))
        elif kind in ("ss", "shadowsocks"):
            node = PmsProxyNode(protocol=PMS_PROTOCOL_SHADOWSOCKS)
            node.method = str(pms_item.get("cipher", "")).lower()
            node.credential_password = str(pms_item.get("password", ""))
            plugin = str(pms_item.get("plugin", "") or "")
            if plugin:
                node.ss_plugin = plugin
                node.ss_plugin_opts = ";".join(
                    f"{key}={value}" for key, value in dict(pms_item.get("plugin-opts") or {}).items()
                )
        elif kind in ("hysteria2", "hysteria"):
            node = PmsProxyNode(protocol=PMS_PROTOCOL_HYSTERIA2)
            node.credential_password = str(pms_item.get("password") or pms_item.get("auth") or "")
            node.obfs = str(pms_item.get("obfs", "") or "")
            node.obfs_password = str(pms_item.get("obfs-password", "") or "")
            node.allow_insecure = pms_safe_bool(pms_item.get("skip-cert-verify"), False)
        elif kind == "tuic":
            node = PmsProxyNode(protocol=PMS_PROTOCOL_TUIC)
            node.credential_uuid = str(pms_item.get("uuid", "") or uuid.uuid4())
            node.credential_password = str(pms_item.get("password", "") or "")
            node.congestion_control = str(pms_item.get("congestion-controller", "bbr"))
            node.udp_relay_mode = str(pms_item.get("udp-relay-mode", "native"))
        elif kind in ("socks5", "socks"):
            node = PmsProxyNode(protocol=PMS_PROTOCOL_SOCKS)
            node.host = str(pms_item.get("username", "") or "")
            node.credential_password = str(pms_item.get("password", "") or "")
        elif kind in ("http", "https"):
            node = PmsProxyNode(protocol=PMS_PROTOCOL_HTTP)
            node.host = str(pms_item.get("username", "") or "")
            node.credential_password = str(pms_item.get("password", "") or "")
            node.security = PMS_SECURITY_TLS if (kind == "https" or pms_safe_bool(pms_item.get("tls"), False)) else PMS_SECURITY_NONE
        if node is None:
            return None
        node.name = str(pms_item.get("name", "") or f"{PMS_PROTOCOL_TITLES.get(node.protocol, node.protocol)}-{server}")
        node.server = server
        node.port = port
        node.transport = PMS_TRANSPORT_ALIASES.get(str(pms_item.get("network", "tcp")).lower(), PMS_TRANSPORT_TCP)
        node.sni = str(pms_item.get("servername") or pms_item.get("sni") or "")
        node.alpn = ",".join(pms_item.get("alpn") or []) if isinstance(pms_item.get("alpn"), list) else str(pms_item.get("alpn") or "")
        node.fingerprint = str(pms_item.get("client-fingerprint", "") or "")
        tls_flag = pms_safe_bool(pms_item.get("tls"), False) or pms_safe_bool(pms_item.get("reality-opts"), False)
        if tls_flag and node.security == PMS_SECURITY_NONE:
            node.security = PMS_SECURITY_TLS
        if pms_item.get("reality-opts"):
            node.security = PMS_SECURITY_REALITY
            reality = dict(pms_item.get("reality-opts") or {})
            node.public_key = str(reality.get("public-key", "") or "")
            node.short_id = str(reality.get("short-id", "") or "")
        for ws_key in ("ws-opts", "h2-opts", "grpc-opts", "http-opts"):
            options = pms_item.get(ws_key)
            if not isinstance(options, dict):
                continue
            headers = dict(options.get("headers") or {})
            node.host = str(headers.get("Host") or headers.get("host") or options.get("host") or node.host or "")
            node.path = str(options.get("path") or (options.get("paths") or [""])[0] or node.path or "")
            if ws_key == "grpc-opts":
                node.service_name = str(options.get("grpc-service-name", "") or node.service_name)
        if node.allow_insecure is False and pms_safe_bool(pms_item.get("skip-cert-verify"), False):
            node.allow_insecure = True
        node.remark = "经 Clash 配置导入"
        return node

class PmsSingBoxJsonImporter:
    """sing-box 配置导入（outbounds 段）。"""

    def psbi_import_payload(self, pms_payload: dict[str, Any]) -> list[PmsProxyNode]:
        outbounds = pms_payload.get("outbounds") or []
        nodes: list[PmsProxyNode] = []
        if not isinstance(outbounds, list):
            return nodes
        for item in outbounds:
            if not isinstance(item, dict):
                continue
            node = self.psbi_outbound_to_node(item)
            if node is not None:
                nodes.append(node)
        return nodes

    def psbi_outbound_to_node(self, pms_item: dict[str, Any]) -> Optional[PmsProxyNode]:
        """把一条 sing-box outbound 转成节点（跳过 direct/block/dns 等内置出站）。"""
        kind = str(pms_item.get("type", "")).lower()
        mapping = {
            "vless": PMS_PROTOCOL_VLESS,
            "vmess": PMS_PROTOCOL_VMESS,
            "trojan": PMS_PROTOCOL_TROJAN,
            "shadowsocks": PMS_PROTOCOL_SHADOWSOCKS,
            "hysteria2": PMS_PROTOCOL_HYSTERIA2,
            "tuic": PMS_PROTOCOL_TUIC,
            "socks": PMS_PROTOCOL_SOCKS,
            "http": PMS_PROTOCOL_HTTP,
        }
        protocol = mapping.get(kind)
        if protocol is None:
            return None
        server = str(pms_item.get("server", "")).strip()
        if not server:
            return None
        node = PmsProxyNode(protocol=protocol)
        node.server = server
        node.port = pms_safe_int(pms_item.get("server_port"), 443, 0, 65535)
        node.name = str(pms_item.get("tag", "") or f"{PMS_PROTOCOL_TITLES.get(protocol, protocol)}-{server}")
        tls = pms_item.get("tls") if isinstance(pms_item.get("tls"), dict) else {}
        transport = pms_item.get("transport") if isinstance(pms_item.get("transport"), dict) else {}
        if protocol in (PMS_PROTOCOL_VLESS, PMS_PROTOCOL_VMESS):
            node.credential_uuid = str(pms_item.get("uuid", "") or "")
        if protocol == PMS_PROTOCOL_VMESS:
            node.vmess_security = str(pms_item.get("security", "auto") or "auto").lower()
            node.alter_id = pms_safe_int(pms_item.get("alter_id"), 0, 0, 65535)
        if protocol in (PMS_PROTOCOL_TROJAN, PMS_PROTOCOL_SHADOWSOCKS, PMS_PROTOCOL_HYSTERIA2):
            node.credential_password = str(pms_item.get("password", "") or "")
        if protocol == PMS_PROTOCOL_SHADOWSOCKS:
            node.method = str(pms_item.get("method", "") or "").lower()
        if protocol == PMS_PROTOCOL_TUIC:
            node.credential_uuid = str(pms_item.get("uuid", "") or "")
            node.credential_password = str(pms_item.get("password", "") or "")
            node.congestion_control = str(pms_item.get("congestion_control", "bbr"))
            node.udp_relay_mode = str(pms_item.get("udp_relay_mode", "native"))
        if protocol in (PMS_PROTOCOL_SOCKS, PMS_PROTOCOL_HTTP):
            node.host = str(pms_item.get("username", "") or "")
            node.credential_password = str(pms_item.get("password", "") or "")
        node.flow = str(pms_item.get("flow", "") or "")
        if isinstance(tls, dict) and tls.get("enabled"):
            reality = tls.get("reality") if isinstance(tls.get("reality"), dict) else {}
            if isinstance(reality, dict) and reality.get("enabled"):
                node.security = PMS_SECURITY_REALITY
                node.public_key = str(reality.get("public_key", "") or "")
                node.short_id = str(reality.get("short_id", "") or "")
            else:
                node.security = PMS_SECURITY_TLS
            node.sni = str(tls.get("server_name", "") or "")
            alpn = tls.get("alpn")
            node.alpn = ",".join(str(item) for item in alpn) if isinstance(alpn, list) else str(alpn or "")
            node.allow_insecure = pms_safe_bool(tls.get("insecure"), False)
            utls = tls.get("utls") if isinstance(tls.get("utls"), dict) else {}
            if isinstance(utls, dict) and utls.get("enabled"):
                node.fingerprint = str(utls.get("fingerprint", "") or "")
        if isinstance(transport, dict):
            transport_type = str(transport.get("type", "tcp")).lower()
            node.transport = PMS_TRANSPORT_ALIASES.get(transport_type, PMS_TRANSPORT_TCP)
            if node.transport in (PMS_TRANSPORT_WS, PMS_TRANSPORT_HTTPUPGRADE, PMS_TRANSPORT_HTTP, PMS_TRANSPORT_H2, PMS_TRANSPORT_XHTTP):
                node.path = str(transport.get("path", "") or "")
                headers = transport.get("headers") if isinstance(transport.get("headers"), dict) else {}
                node.host = str(headers.get("Host") or headers.get("host") or node.host or "")
            if node.transport == PMS_TRANSPORT_GRPC:
                node.service_name = str(transport.get("service_name", "") or "")
        obfs = pms_item.get("obfs") if isinstance(pms_item.get("obfs"), dict) else {}
        if isinstance(obfs, dict) and obfs:
            node.obfs = str(obfs.get("type", "") or "")
            node.obfs_password = str(obfs.get("password", "") or "")
        node.remark = "经 sing-box 配置导入"
        return node

class PmsTextImportClassifier:
    """判断一段文本属于哪种导入格式，并调用对应解析器。"""

    def __init__(self, pms_parser: Optional[PmsShareLinkParser] = None) -> None:
        self.ptic_parser = pms_parser or PmsShareLinkParser()
        self.ptic_clash = PmsClashYamlImporter()
        self.ptic_singbox = PmsSingBoxJsonImporter()
        self.ptic_logger = logging.getLogger(PMS_LOGGER_NAME + ".import")

    def ptic_classify(self, pms_text: str) -> str:
        """返回 links / base64 / singbox_json / clash_json / clash_yaml / unknown。"""
        text = str(pms_text or "").strip()
        if not text:
            return "unknown"
        if "://" in text and re.search(r"(?im)^\s*[a-z][a-z0-9+.\-]*://", text):
            return "links"
        stripped = text.lstrip("\ufeff \t\r\n")
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except ValueError:
                return "unknown"
            if isinstance(payload, dict) and ("outbounds" in payload or "inbounds" in payload):
                return "singbox_json"
            if isinstance(payload, dict) and ("proxies" in payload or "Proxy" in payload):
                return "clash_json"
            if isinstance(payload, dict) and any(key in payload for key in ("dns", "route", "experimental", "log")):
                return "singbox_json"
            return "unknown"
        if pms_looks_like_base64(text):
            return "base64"
        if re.search(r"(?im)^\s*(proxies|proxy-groups|Proxy)\s*:", text):
            return "clash_yaml"
        return "unknown"

    def ptic_import_text(
        self,
        pms_text: str,
        pms_default_group: str = "导入",
    ) -> tuple[list[PmsProxyNode], PmsImportReport]:
        """自动识别格式并导入，返回 (节点列表, 报告)。"""
        text = str(pms_text or "")
        kind = self.ptic_classify(text)
        if kind == "links":
            nodes, report = self.ptic_parser.pslp_parse_text(text, pms_default_group)
            report.source = "分享链接文本"
            return nodes, report
        if kind == "base64":
            decoded = pms_b64_decode_loose(text)
            nodes, report = self.ptic_parser.pslp_parse_text(decoded, pms_default_group)
            report.source = "base64 订阅正文"
            return nodes, report
        if kind == "singbox_json":
            try:
                payload = json.loads(text)
            except ValueError as exc:
                self.ptic_logger.error("sing-box JSON 解析失败：%s", exc)
                payload = {}
            nodes = self.ptic_singbox.psbi_import_payload(payload if isinstance(payload, dict) else {})
            report = PmsImportReport(source="sing-box JSON", total_lines=len(nodes), parsed=len(nodes))
            if not nodes:
                report.errors.append("该 JSON 是客户端运行配置，未包含可作为节点的出站（outbounds 里没有 vless/vmess/trojan/ss/hy2/tuic 等）")
            for node in nodes:
                report.pimp_note_protocol(node.protocol)
                node.group_name = pms_default_group
            return nodes, report
        if kind in ("clash_json", "clash_yaml"):
            if kind == "clash_json":
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = {}
                payload = payload if isinstance(payload, dict) else {}
            else:
                payload = {}
            nodes = self.ptic_clash.pcyi_import_payload(payload) if kind == "clash_json" else self.ptic_clash.pcyi_import_text(text)
            report = PmsImportReport(source="Clash 配置", total_lines=len(nodes), parsed=len(nodes))
            for node in nodes:
                report.pimp_note_protocol(node.protocol)
                node.group_name = pms_default_group
            return nodes, report
        # v1.9.69：从 Telegram 频道预览页（t.me/s/<频道> 这类 HTML 正文）里抽取代理链接。
        # 频道页是 HTML 不是裸链接列表，ptic_classify 会判成 unknown；这里先做一轮兜底，
        # 把正文里出现的 t.me/{proxy,webproxy,socks}?<query> 链接抽出来，再按单行分享链接导入。
        # ① 沙箱内 web_fetch t.me 实测 fetch failed，真抓取只能在本机跑，这里只负责「萃取到链接」；
        # ② 去重 + 反转 &amp; HTML 实体；其它协议头（vmess://…）的正则也会顺带命中，一并交给 pslp_parse_line。
        html_links = self.ptic_extract_tg_links(text)
        if html_links:
            return self.ptic_parser.pslp_parse_text("\n".join(html_links), pms_default_group)
        report = PmsImportReport(source="无法识别")
        report.errors.append("无法识别的内容格式（支持分享链接、base64 订阅、sing-box JSON、Clash YAML/JSON、Telegram 频道预览页）")
        return [], report

    @staticmethod
    def ptic_extract_tg_links(pms_text: str) -> list[str]:
        """从一堆自由文本（通常是 Telegram 频道预览页 HTML）里抽出可导入的分享链接。

        返回去重后的链接列表（已反转 `&amp;` → `&`）。命中两类：
        · t.me/proxy / t.me/webproxy / t.me/socks（含 telegram.me / tg.dev 镜像）这类电报专属代理；
        · 标准协议头链接 vmess:// vless:// trojan:// ss:// …（频道里常混着贴）。
        """
        text = str(pms_text or "")
        if not text.strip():
            return []
        candidates: list[str] = []
        # 先抓 t.me 系（路径形态，非 scheme://）：host 允许 t.me/telegram.me/tg.dev，路径 proxy|webproxy|socks
        candidates.extend(re.findall(r"https?://(?:t\.me|telegram\.me|tg\.dev)/(?:proxy|webproxy|socks)\?[^\s\"'<>)]+", text))
        # 再抓标准协议头链接（vmess://…、socks5://… 等）
        candidates.extend(re.findall(r"[a-z][a-z0-9+.\-]*://[^\s\"'<>)]+", text))
        seen: set[str] = set()
        result: list[str] = []
        for raw in candidates:
            link = raw.replace("&amp;", "&").strip().rstrip(".,;")
            if not link or link in seen:
                continue
            seen.add(link)
            result.append(link)
        return result
