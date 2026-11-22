#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分享链接解析与生成

8 种协议的解析与还原（vmess/vless/trojan/ss/hy2/tuic/socks/http），含导入统计报告。
"""

from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from typing import (
    Any,
    Optional,
    Sequence,
)
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid

from pms.constants import (
    PMS_HAPP_SCHEME,
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
    PMS_SHARE_SCHEME_TO_PROTOCOL,
    PMS_TRANSPORT_ALIASES,
    PMS_TRANSPORT_GRPC,
    PMS_TRANSPORT_H2,
    PMS_TRANSPORT_TCP,
    PMS_TRANSPORT_XHTTP,
    PMS_VMESS_SECURITY,
    PMS_EXTRA_PROTOCOLS,
    PMS_TG_PROXY_HOSTS,
    PMS_TG_PROXY_PATHS,
    PMS_PROTOCOL_TG_WEBPROXY,
    PMS_PROTOCOL_TG_PROXY,
)
from pms.utils import (
    pms_b64_decode_loose,
    pms_b64_encode_text,
    pms_looks_like_base64,
    pms_safe_int,
)
from pms.models import PmsProxyNode
from pms.sharelinks_extra import (
    PmsShareLinkParserExtraMixin,
    PmsShareLinkWriterExtraMixin,
)

# --------------------------------------------------------------------------------------
# 分享链接解析（vmess/vless/trojan/ss/hy2/tuic/socks/http）
# --------------------------------------------------------------------------------------
@dataclass
class PmsImportReport:
    """一次导入的统计报告（界面导入结果对话框 / 日志都用它）。"""

    total_lines: int = 0
    parsed: int = 0
    invalid: int = 0
    duplicates: int = 0
    by_protocol: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    source: str = ""

    def pimp_note_protocol(self, pms_protocol: str) -> None:
        key = PMS_PROTOCOL_TITLES.get(pms_protocol, pms_protocol)
        self.by_protocol[key] = self.by_protocol.get(key, 0) + 1

    def pimp_summary_text(self) -> str:
        """人类可读的摘要文本。"""
        parts = [
            f"来源：{self.source or '未知'}",
            f"有效行：{self.total_lines}",
            f"成功解析：{self.parsed}",
            f"无效：{self.invalid}",
            f"重复：{self.duplicates}",
        ]
        if self.by_protocol:
            detail = "、".join(f"{key} {value}" for key, value in sorted(self.by_protocol.items()))
            parts.append(f"协议分布：{detail}")
        if self.errors:
            parts.append("错误示例：\n" + "\n".join(self.errors[:8]))
        return "\n".join(parts)

class PmsShareLinkParser(PmsShareLinkParserExtraMixin):
    """把一个分享链接（或一大段文本）解析成节点对象。所有方法都不允许抛异常。"""

    def __init__(self, pms_logger: Optional[logging.Logger] = None) -> None:
        self.pslp_logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".parser")

    # ------------------------------------------------------------------ 文本级
    def pslp_parse_text(
        self,
        pms_text: str,
        pms_default_group: str = "导入",
        pms_default_subscription_uid: str = "",
        pms_default_subscription_name: str = "",
    ) -> tuple[list[PmsProxyNode], PmsImportReport]:
        """解析整段文本：支持分享链接列表、base64 订阅正文、混合内容。"""
        report = PmsImportReport(source="文本")
        nodes: list[PmsProxyNode] = []
        text = str(pms_text or "")
        if pms_looks_like_base64(text) and "://" not in text:
            decoded = pms_b64_decode_loose(text)
            if decoded:
                text = decoded
                report.source = "base64 订阅内容"
        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.strip().strip('",')
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            report.total_lines += 1
            if "://" not in line:
                report.invalid += 1
                continue
            node, error = self.pslp_parse_line(line)
            if node is None:
                report.invalid += 1
                if len(report.errors) < 30:
                    report.errors.append(f"{error}：{line[:90]}")
                continue
            node.group_name = node.group_name or pms_default_group
            node.subscription_uid = pms_default_subscription_uid
            node.subscription_name = pms_default_subscription_name
            report.parsed += 1
            report.pimp_note_protocol(node.protocol)
            nodes.append(node)
        return nodes, report

    def pslp_parse_line(self, pms_line: str) -> tuple[Optional[PmsProxyNode], str]:
        """解析单行分享链接，返回 (节点, 错误说明)。"""
        line = str(pms_line or "").strip()
        if not line:
            return None, "空行"
        scheme = line.split("://", 1)[0].strip().lower()
        if scheme == PMS_HAPP_SCHEME:
            return None, self.pslp_happ_hint(line)
        # v1.9.68：Telegram 专属代理链接是 https://t.me/<path>?... 形态（路径在主机之后，
        # 不是 scheme://），逐行解析时按「主机 + 路径」特判，不在 PMS_SHARE_SCHEME_TO_PROTOCOL 里。
        if scheme in ("https", "http"):
            host_path = line.split("://", 1)[1].split("?", 1)[0].strip("/").split("/", 1)
            host = host_path[0].split(":")[0].strip().lower()
            path = host_path[1].split("/")[0].lower() if len(host_path) > 1 else ""
            if host in PMS_TG_PROXY_HOSTS and path in PMS_TG_PROXY_PATHS:
                try:
                    return self.pslp_parse_tg_proxy(line, path)
                except Exception as exc:  # noqa: BLE001
                    self.pslp_logger.debug("tg 代理解析失败：%s（%s）", exc, line[:80])
                    return None, f"解析异常 {type(exc).__name__}: {exc}"
        protocol = PMS_SHARE_SCHEME_TO_PROTOCOL.get(scheme)
        if protocol is None:
            return None, f"不支持的协议头 {scheme}://"
        try:
            if protocol == PMS_PROTOCOL_VMESS:
                node = self.pslp_parse_vmess(line)
            elif protocol == PMS_PROTOCOL_VLESS:
                node = self.pslp_parse_vless(line)
            elif protocol == PMS_PROTOCOL_TROJAN:
                node = self.pslp_parse_trojan(line)
            elif protocol == PMS_PROTOCOL_SHADOWSOCKS:
                node = self.pslp_parse_shadowsocks(line)
            elif protocol == PMS_PROTOCOL_HYSTERIA2:
                node = self.pslp_parse_hysteria2(line)
            elif protocol == PMS_PROTOCOL_TUIC:
                node = self.pslp_parse_tuic(line)
            elif protocol in (PMS_PROTOCOL_SOCKS, PMS_PROTOCOL_HTTP):
                node = self.pslp_parse_socks_http(line, protocol)
            elif protocol in PMS_EXTRA_PROTOCOLS:
                node = self.pslxp_dispatch(line, protocol)
            else:
                return None, f"未实现的协议 {protocol}"
        except Exception as exc:  # noqa: BLE001 - 任何一行坏数据都不能中断导入
            self.pslp_logger.debug("解析失败：%s（%s）", exc, line[:80])
            return None, f"解析异常 {type(exc).__name__}: {exc}"
        if node is None:
            return None, "字段缺失"
        errors = node.pnode_validate()
        if errors:
            return None, "；".join(errors)
        node.name = node.name or f"{PMS_PROTOCOL_TITLES.get(node.protocol, node.protocol)}-{node.server}"
        return node, ""

    # ------------------------------------------------------------------ Happ 链接（订阅链接，非节点链接）
    def pslp_happ_subscription_url(self, pms_line: str) -> str:
        """从 happ://add/<订阅地址> 取出明文订阅地址；不是该形式则返回空串。

        Happ 的加密形式 happ://crypt*/ 里装的是 RSA 加密后的订阅地址，解密密钥未公开，
        本程序无法还原，只能给出提示（见 pslp_happ_hint）。
        """
        line = str(pms_line or "").strip()
        if "://" not in line:
            return ""
        scheme, payload = line.split("://", 1)
        if scheme.strip().lower() != PMS_HAPP_SCHEME or "/" not in payload:
            return ""
        kind, inner = payload.split("/", 1)
        if kind.strip().lower() != "add":
            return ""
        url = urllib.parse.unquote(inner.strip())
        return url if url.lower().startswith(("http://", "https://")) else ""

    def pslp_happ_hint(self, pms_line: str) -> str:
        """Happ 链接说明：明文 add 链接给出可用地址，加密链接讲清无法解析的原因。"""
        url = self.pslp_happ_subscription_url(pms_line)
        if url:
            return f"Happ 明文订阅链接（happ://add/）：可添加该订阅地址 {url}"
        payload = pms_line.split("://", 1)[1] if "://" in pms_line else ""
        kind = payload.split("/", 1)[0].strip().lower() if payload else ""
        if kind.startswith("crypt"):
            return (
                f"Happ 加密订阅链接（{kind}）：内容是 RSA 加密的订阅地址，解密密钥未公开，本程序无法还原；"
                f"请向服务商索取明文 https 订阅地址，或改用 happ://add/<订阅地址> 形式"
            )
        return "Happ 客户端链接（happ://）：本程序支持节点链接与标准 http(s) 订阅地址，不支持该形式"

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def pslp_split_fragment(pms_line: str) -> tuple[str, str]:
        """把链接拆成 (主体, 名称)：支持 #名称 与 URL 编码名称。"""
        if "#" not in pms_line:
            return pms_line, ""
        body, fragment = pms_line.split("#", 1)
        name = urllib.parse.unquote(fragment.strip())
        return body, name

    @staticmethod
    def pslp_query_dict(pms_query: str) -> dict[str, str]:
        """解析查询串为小写键字典（键统一小写，值去空白）。"""
        result: dict[str, str] = {}
        for key, value in urllib.parse.parse_qsl(str(pms_query or "").lstrip("?"), keep_blank_values=True):
            result[str(key).strip().lower()] = str(value).strip()
        return result

    @staticmethod
    def pslp_pick(pms_query: dict[str, str], pms_keys: Sequence[str], pms_default: str = "") -> str:
        """按候选键顺序取第一个非空值。"""
        for key in pms_keys:
            value = pms_query.get(key)
            if value not in (None, ""):
                return value
        return pms_default

    @staticmethod
    def pslp_bool_text(pms_value: Any, pms_default: bool = False) -> bool:
        """true/1/yes/tls 等一律识别为真（各家客户端写法不一）。"""
        if pms_value is None:
            return pms_default
        text = str(pms_value).strip().lower()
        if text in ("", "0", "false", "no", "off", "none", "null"):
            return False
        return True

    def pslp_apply_stream_fields(self, pms_node: PmsProxyNode, pms_query: dict[str, str]) -> None:
        """把 type/host/path/serviceName/security/sni 等通用传输字段写到节点上。"""
        node = pms_node
        raw_type = self.pslp_pick(pms_query, ("type", "net", "network", "transport"), "")
        node.transport = PMS_TRANSPORT_ALIASES.get(str(raw_type).lower(), PMS_TRANSPORT_TCP)
        security = self.pslp_pick(pms_query, ("security", "tls"), "").lower()
        if security in ("reality",):
            node.security = PMS_SECURITY_REALITY
        elif security in ("tls", "xtls", "1", "true"):
            node.security = PMS_SECURITY_TLS
        elif node.transport == PMS_TRANSPORT_H2 and security == "":
            node.security = PMS_SECURITY_TLS
        else:
            node.security = PMS_SECURITY_NONE
        node.sni = self.pslp_pick(pms_query, ("sni", "peer", "servername", "host") if node.security != PMS_SECURITY_NONE else ("sni", "peer", "servername"), "")
        node.alpn = self.pslp_pick(pms_query, ("alpn", ""), "")
        node.fingerprint = self.pslp_pick(pms_query, ("fp", "fingerprint"), "")
        node.allow_insecure = self.pslp_bool_text(self.pslp_pick(pms_query, ("allowinsecure", "insecure", "skip-cert-verify"), ""))
        node.host = self.pslp_pick(pms_query, ("host", "hostheader"), "")
        node.path = self.pslp_pick(pms_query, ("path",), "")
        node.service_name = self.pslp_pick(pms_query, ("servicename", "service_name", "servicename", "grpc-service-name"), "")
        node.grpc_mode = self.pslp_pick(pms_query, ("mode",), "")
        node.header_type = self.pslp_pick(pms_query, ("headertype", "header_type"), "")
        node.public_key = self.pslp_pick(pms_query, ("pbk", "publickey", "public_key"), "")
        node.short_id = self.pslp_pick(pms_query, ("sid", "shortid", "short_id"), "")
        node.spider_x = self.pslp_pick(pms_query, ("spx", "spiderx", "spider_x"), "")
        if node.transport == PMS_TRANSPORT_GRPC and not node.service_name:
            node.service_name = node.path
            node.path = ""
        if node.transport == PMS_TRANSPORT_XHTTP and not node.path:
            node.path = self.pslp_pick(pms_query, ("path", "xhttp_path"), "")
        if node.security == PMS_SECURITY_REALITY and not node.fingerprint:
            node.fingerprint = "chrome"
        if node.security == PMS_SECURITY_REALITY and not node.sni:
            node.sni = node.host

    @staticmethod
    def pslp_split_host_port(pms_text: str, pms_default_port: int = 0) -> tuple[str, int]:
        """拆 host:port，支持 IPv6 的 [::1]:443 写法。"""
        text = str(pms_text or "").strip()
        if text.startswith("["):
            closing = text.find("]")
            if closing > 0:
                host = text[1:closing]
                rest = text[closing + 1:].lstrip(":")
                port = pms_safe_int(rest, pms_default_port, 0, 65535) if rest else pms_default_port
                return host, port
        if text.count(":") == 1:
            host, port_text = text.rsplit(":", 1)
            return host.strip(), pms_safe_int(port_text, pms_default_port, 0, 65535)
        if text.count(":") > 1 and not text.startswith("["):
            return text, pms_default_port
        return text, pms_default_port

    # ------------------------------------------------------------------ 各协议
    def pslp_parse_vmess(self, pms_line: str) -> Optional[PmsProxyNode]:
        """VMess：兼容 v2rayN 的 JSON base64 形式与 AEAD 文本形式。"""
        body = pms_line[len("vmess://"):]
        body, name = self.pslp_split_fragment(body)
        body = body.strip()
        payload: dict[str, Any] = {}
        decoded = pms_b64_decode_loose(body)
        if decoded.strip().startswith("{"):
            try:
                loaded = json.loads(decoded)
                if isinstance(loaded, dict):
                    payload = loaded
            except ValueError:
                payload = {}
        if not payload and "@" in body:
            # vmess://<security>:<uuid>@host:port?<query>
            head, rest = body.split("@", 1)
            security_prefix = ""
            if ":" in head:
                security_prefix, uuid_text = head.split(":", 1)
            else:
                uuid_text = head
            host_port, _, query = rest.partition("?")
            host, port = self.pslp_split_host_port(host_port, 443)
            query_dict = self.pslp_query_dict(query)
            payload = {
                "id": uuid_text,
                "add": host,
                "port": port,
                "scy": security_prefix or "auto",
                "net": query_dict.get("type", "tcp"),
                "host": query_dict.get("host", ""),
                "path": query_dict.get("path", ""),
                "tls": "tls" if query_dict.get("security", "") else "",
                "sni": query_dict.get("sni", ""),
                "aid": query_dict.get("alterid", 0),
                "fp": query_dict.get("fp", ""),
                "alpn": query_dict.get("alpn", ""),
            }
        if not payload:
            return None
        node = PmsProxyNode(protocol=PMS_PROTOCOL_VMESS)
        node.name = str(payload.get("ps") or name or "").strip()
        node.server = str(payload.get("add") or "").strip().strip("[]")
        node.port = pms_safe_int(payload.get("port"), 443, 0, 65535)
        node.credential_uuid = str(payload.get("id") or "").strip()
        node.alter_id = pms_safe_int(payload.get("aid"), 0, 0, 65535)
        scy = str(payload.get("scy") or payload.get("security") or "auto").strip().lower()
        node.vmess_security = scy if scy in PMS_VMESS_SECURITY else "auto"
        node.transport = PMS_TRANSPORT_ALIASES.get(str(payload.get("net") or "tcp").lower(), PMS_TRANSPORT_TCP)
        tls_text = str(payload.get("tls") or "").strip().lower()
        node.security = PMS_SECURITY_TLS if tls_text in ("tls", "reality", "true", "1") else PMS_SECURITY_NONE
        node.sni = str(payload.get("sni") or "").strip()
        node.alpn = str(payload.get("alpn") or "").strip()
        node.fingerprint = str(payload.get("fp") or "").strip()
        node.host = str(payload.get("host") or "").strip()
        node.path = str(payload.get("path") or "").strip()
        node.header_type = str(payload.get("type") or "").strip()
        node.service_name = str(payload.get("path") or "").strip() if node.transport == PMS_TRANSPORT_GRPC else ""
        node.remark = "经 vmess:// 导入"
        return node

    def pslp_parse_vless(self, pms_line: str) -> Optional[PmsProxyNode]:
        """VLESS：vless://uuid@host:port?...#名称（含 Reality）。"""
        body, name = self.pslp_split_fragment(pms_line[len("vless://"):])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        credential, host_port = head.rsplit("@", 1)
        host, port = self.pslp_split_host_port(host_port, 443)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_VLESS)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        node.credential_uuid = urllib.parse.unquote(credential.strip())
        query_dict = self.pslp_query_dict(query)
        self.pslp_apply_stream_fields(node, query_dict)
        node.flow = self.pslp_pick(query_dict, ("flow",), "")
        node.remark = "经 vless:// 导入"
        return node

    def pslp_parse_trojan(self, pms_line: str) -> Optional[PmsProxyNode]:
        """Trojan：trojan://password@host:port?...#名称。"""
        scheme_length = len("trojan-go://") if pms_line.lower().startswith("trojan-go://") else len("trojan://")
        body, name = self.pslp_split_fragment(pms_line[scheme_length:])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        password, host_port = head.rsplit("@", 1)
        host, port = self.pslp_split_host_port(host_port, 443)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_TROJAN)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        node.credential_password = urllib.parse.unquote(password.strip())
        query_dict = self.pslp_query_dict(query)
        self.pslp_apply_stream_fields(node, query_dict)
        if node.security == PMS_SECURITY_NONE and not query_dict.get("security"):
            node.security = PMS_SECURITY_TLS
        node.remark = "经 trojan:// 导入"
        return node

    def pslp_parse_shadowsocks(self, pms_line: str) -> Optional[PmsProxyNode]:
        """Shadowsocks：兼容 SIP002 与旧版整段 base64 形式，含 simple-obfs / v2ray-plugin。"""
        body, name = self.pslp_split_fragment(pms_line[len("ss://"):])
        body = body.strip()
        query = ""
        if "?" in body:
            body, query = body.split("?", 1)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_SHADOWSOCKS)
        node.name = name
        if "@" in body:
            credential_part, host_port = body.rsplit("@", 1)
            credential_text = urllib.parse.unquote(credential_part)
            decoded = pms_b64_decode_loose(credential_text)
            if ":" in decoded:
                method, password = decoded.split(":", 1)
            elif ":" in credential_text:
                method, password = credential_text.split(":", 1)
            else:
                return None
            host, port = self.pslp_split_host_port(host_port, 8388)
            node.method = method.strip().lower()
            node.credential_password = password
        else:
            decoded = pms_b64_decode_loose(body)
            if not decoded or "@" not in decoded:
                return None
            credential_part, host_port = decoded.rsplit("@", 1)
            if ":" not in credential_part:
                return None
            method, password = credential_part.split(":", 1)
            host, port = self.pslp_split_host_port(host_port, 8388)
            node.method = method.strip().lower()
            node.credential_password = password
        node.server = host.strip("[]")
        node.port = port
        query_dict = self.pslp_query_dict(query)
        plugin_text = self.pslp_pick(query_dict, ("plugin",), "")
        if plugin_text:
            parts = [item for item in str(plugin_text).split(";") if item]
            node.ss_plugin = parts[0].strip()
            options: dict[str, str] = {}
            for item in parts[1:]:
                if "=" in item:
                    key, value = item.split("=", 1)
                    options[key.strip().lower()] = value.strip()
                else:
                    options[item.strip().lower()] = ""
            node.ss_plugin_opts = ";".join(f"{key}={value}" if value else key for key, value in options.items())
            if "obfs" in options:
                node.obfs = options.get("obfs", "")
                node.obfs_password = options.get("obfs-host", "")
            if options.get("tls") or options.get("host") or options.get("path"):
                node.security = PMS_SECURITY_TLS if options.get("tls") else PMS_SECURITY_NONE
                node.host = options.get("host", node.host)
                node.path = options.get("path", node.path)
        node.remark = "经 ss:// 导入"
        return node

    def pslp_parse_hysteria2(self, pms_line: str) -> Optional[PmsProxyNode]:
        """Hysteria2：hysteria2://password@host:port?obfs=salamander&obfs-password=...#名称。"""
        prefix_length = len("hysteria2://") if pms_line.lower().startswith("hysteria2://") else (len("hysteria://") if pms_line.lower().startswith("hysteria://") else len("hy2://"))
        body, name = self.pslp_split_fragment(pms_line[prefix_length:])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        credential, host_port = head.rsplit("@", 1)
        host, port = self.pslp_split_host_port(host_port, 443)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_HYSTERIA2)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        node.credential_password = urllib.parse.unquote(credential.strip())
        query_dict = self.pslp_query_dict(query)
        self.pslp_apply_stream_fields(node, query_dict)
        if node.security == PMS_SECURITY_NONE:
            node.security = PMS_SECURITY_TLS
        node.obfs = self.pslp_pick(query_dict, ("obfs",), "")
        node.obfs_password = self.pslp_pick(query_dict, ("obfs-password", "obfspassword"), "")
        node.allow_insecure = self.pslp_bool_text(self.pslp_pick(query_dict, ("insecure", "allowinsecure"), "1"), True)
        node.remark = "经 hysteria2:// 导入"
        return node

    def pslp_parse_tuic(self, pms_line: str) -> Optional[PmsProxyNode]:
        """TUIC：tuic://uuid:password@host:port?congestion_control=bbr&alpn=h3#名称。"""
        body, name = self.pslp_split_fragment(pms_line[len("tuic://"):])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        credential, host_port = head.rsplit("@", 1)
        credential = urllib.parse.unquote(credential.strip())
        host, port = self.pslp_split_host_port(host_port, 443)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_TUIC)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        if ":" in credential:
            uuid_text, password = credential.split(":", 1)
            node.credential_uuid = uuid_text.strip()
            node.credential_password = password
        else:
            node.credential_uuid = credential
            node.credential_password = ""
        query_dict = self.pslp_query_dict(query)
        self.pslp_apply_stream_fields(node, query_dict)
        node.security = PMS_SECURITY_TLS
        node.congestion_control = self.pslp_pick(query_dict, ("congestion_control", "congestion"), "bbr").lower()
        node.udp_relay_mode = self.pslp_pick(query_dict, ("udp_relay_mode", "udp-relay-mode"), "native").lower()
        if not node.alpn:
            node.alpn = "h3"
        node.remark = "经 tuic:// 导入"
        if not node.credential_password and not node.credential_uuid:
            return None
        if not node.credential_uuid:
            node.credential_uuid = str(uuid.uuid4())
        return node

    def pslp_parse_socks_http(self, pms_line: str, pms_protocol: str) -> Optional[PmsProxyNode]:
        """Socks / HTTP(S)：socks5://user:pass@host:port、http://user:pass@host:port。"""
        scheme = pms_line.split("://", 1)[0].lower()
        body, name = self.pslp_split_fragment(pms_line.split("://", 1)[1])
        head, _, query = body.partition("?")
        host_port = head
        credential = ""
        if "@" in head:
            credential, host_port = head.rsplit("@", 1)
        host, port = self.pslp_split_host_port(host_port, 1080 if pms_protocol == PMS_PROTOCOL_SOCKS else 8080)
        if not host:
            return None
        node = PmsProxyNode(protocol=pms_protocol)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        credential = urllib.parse.unquote(credential.strip())
        if credential:
            decoded = pms_b64_decode_loose(credential)
            if ":" in decoded:
                credential = decoded
            if ":" in credential:
                username, password = credential.split(":", 1)
                node.host = username.strip()
                node.credential_password = password
        query_dict = self.pslp_query_dict(query)
        if query_dict.get("username"):
            node.host = query_dict["username"]
        if query_dict.get("password"):
            node.credential_password = query_dict["password"]
        node.security = PMS_SECURITY_TLS if scheme == "https" else PMS_SECURITY_NONE
        node.sni = self.pslp_pick(query_dict, ("sni", "peer"), "")
        node.allow_insecure = self.pslp_bool_text(self.pslp_pick(query_dict, ("allowinsecure", "insecure"), ""))
        node.remark = f"经 {scheme}:// 导入"
        return node

    # ------------------------------------------------------------------ Telegram 专属代理（t.me/proxy · t.me/webproxy）
    def pslp_parse_tg_proxy(self, pms_line: str, pms_path: str) -> tuple[Optional[PmsProxyNode], str]:
        """解析 https://t.me/<proxy|webproxy>?server=..&port=..&secret=.. 链接。

        · webproxy → TgWebProxy（标准 SOCKS5，secret 即 16 字节密码，能当通用出口）
        · proxy    → TgProxy（MTProto 电报代理，仅测电报连通，不当通用出口）
        secret **原样**存进 credential_password（不在此解码字节，避免二进制/超长破坏 DB 与导出）；
        MTProto 密钥的真正解码在探测模块 pms/tgproxy.py 里按前缀（dd/ee/裸 16B）处理。
        """
        parts = urllib.parse.urlsplit(pms_line)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        server = (query.get("server") or [""])[0].strip()
        port_text = (query.get("port") or [""])[0].strip()
        secret = (query.get("secret") or [""])[0].strip()
        if not server:
            return None, "缺少 server 参数"
        if not port_text or not port_text.isdigit():
            return None, "缺少合法 port 参数"
        if not secret:
            return None, "缺少 secret 参数"
        if pms_path == "webproxy":
            node = PmsProxyNode(protocol=PMS_PROTOCOL_TG_WEBPROXY)
            node.credential_password = secret      # 即 SOCKS5 的 16 字节密码
            node.remark = "经 t.me/webproxy 导入"
        else:  # proxy / socks
            node = PmsProxyNode(protocol=PMS_PROTOCOL_TG_PROXY)
            node.credential_password = secret      # MTProto secret（原样保存）
            node.remark = f"经 t.me/{pms_path} 导入"
        node.server = server
        node.port = int(port_text)
        node.name = ""
        return node, ""

class PmsShareLinkWriter(PmsShareLinkWriterExtraMixin):
    """把节点还原成分享链接（导出 / 二维码 / 剪贴板复制）。"""

    def pslw_node_to_link(self, pms_node: PmsProxyNode) -> str:
        """生成单节点分享链接（协议不支持的组合返回空串）。"""
        node = pms_node
        name = urllib.parse.quote(node.pnode_display_name(), safe="")
        if node.protocol == PMS_PROTOCOL_VMESS:
            payload = {
                "v": "2",
                "ps": node.pnode_display_name(),
                "add": node.server,
                "port": str(int(node.port)),
                "id": node.credential_uuid,
                "aid": str(int(node.alter_id)),
                "scy": node.vmess_security or "auto",
                "net": node.transport,
                "type": node.header_type or "none",
                "host": node.host,
                "path": node.path or node.service_name,
                "tls": "tls" if node.security != PMS_SECURITY_NONE else "",
                "sni": node.sni,
                "alpn": node.alpn,
                "fp": node.fingerprint,
            }
            return "vmess://" + pms_b64_encode_text(json.dumps(payload, ensure_ascii=False))
        if node.protocol == PMS_PROTOCOL_VLESS:
            query = self.pslw_build_query(node, True)
            return f"vless://{node.credential_uuid}@{self.pslw_host_port(node)}?{query}#{name}"
        if node.protocol == PMS_PROTOCOL_TROJAN:
            query = self.pslw_build_query(node, True)
            return f"trojan://{urllib.parse.quote(node.credential_password, safe='')}@{self.pslw_host_port(node)}?{query}#{name}"
        if node.protocol == PMS_PROTOCOL_SHADOWSOCKS:
            user_info = pms_b64_encode_text(f"{node.method}:{node.credential_password}")
            query = ""
            if node.ss_plugin:
                plugin_value = node.ss_plugin
                if node.ss_plugin_opts:
                    plugin_value += ";" + node.ss_plugin_opts
                query = "?plugin=" + urllib.parse.quote(plugin_value, safe="")
            return f"ss://{user_info}@{self.pslw_host_port(node)}{query}#{name}"
        if node.protocol == PMS_PROTOCOL_HYSTERIA2:
            query_parts = [
                f"insecure={'1' if node.allow_insecure else '0'}",
            ]
            if node.sni:
                query_parts.append("sni=" + urllib.parse.quote(node.sni, safe=""))
            if node.obfs:
                query_parts.append("obfs=" + urllib.parse.quote(node.obfs, safe=""))
            if node.obfs_password:
                query_parts.append("obfs-password=" + urllib.parse.quote(node.obfs_password, safe=""))
            return f"hysteria2://{urllib.parse.quote(node.credential_password, safe='')}@{self.pslw_host_port(node)}?{'&'.join(query_parts)}#{name}"
        if node.protocol == PMS_PROTOCOL_TUIC:
            query_parts = [f"congestion_control={node.congestion_control or 'bbr'}", f"udp_relay_mode={node.udp_relay_mode or 'native'}"]
            if node.alpn:
                query_parts.append("alpn=" + urllib.parse.quote(node.alpn, safe=""))
            if node.sni:
                query_parts.append("sni=" + urllib.parse.quote(node.sni, safe=""))
            if node.allow_insecure:
                query_parts.append("allow_insecure=1")
            credential = f"{node.credential_uuid}:{node.credential_password}" if node.credential_password else node.credential_uuid
            return f"tuic://{urllib.parse.quote(credential, safe='')}@{self.pslw_host_port(node)}?{'&'.join(query_parts)}#{name}"
        if node.protocol in (PMS_PROTOCOL_SOCKS, PMS_PROTOCOL_HTTP):
            scheme = "socks5" if node.protocol == PMS_PROTOCOL_SOCKS else ("https" if node.security == PMS_SECURITY_TLS else "http")
            credential = ""
            if node.host or node.credential_password:
                credential = urllib.parse.quote(f"{node.host}:{node.credential_password}", safe="") + "@"
            return f"{scheme}://{credential}{self.pslw_host_port(node)}#{name}"
        # v1.9.68：Telegram 专属代理反向还原成 t.me 链接（secret 原样回填）
        if node.protocol == PMS_PROTOCOL_TG_WEBPROXY:
            return f"https://t.me/webproxy?server={node.server}&port={int(node.port)}&secret={node.credential_password}"
        if node.protocol == PMS_PROTOCOL_TG_PROXY:
            return f"https://t.me/proxy?server={node.server}&port={int(node.port)}&secret={node.credential_password}"
        if node.protocol in PMS_EXTRA_PROTOCOLS:
            return self.pslxp_node_to_link(node)
        return ""

    @staticmethod
    def pslw_host_port(pms_node: PmsProxyNode) -> str:
        """host:port 文本（IPv6 加方括号）。"""
        host = pms_node.server
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{int(pms_node.port)}"

    def pslw_build_query(self, pms_node: PmsProxyNode, pms_include_security: bool = True) -> str:
        """构造公共查询串（类型/传输/TLS 参数）。"""
        node = pms_node
        parts: list[str] = []
        if node.protocol == PMS_PROTOCOL_VLESS:
            parts.append("encryption=none")
            if node.flow:
                parts.append("flow=" + urllib.parse.quote(node.flow, safe=""))
        if pms_include_security:
            security = PMS_SECURITY_TLS if node.security == PMS_SECURITY_TLS else (PMS_SECURITY_REALITY if node.security == PMS_SECURITY_REALITY else "none")
            parts.append("security=" + security)
        parts.append("type=" + (node.transport or PMS_TRANSPORT_TCP))
        if node.sni:
            parts.append("sni=" + urllib.parse.quote(node.sni, safe=""))
        if node.alpn:
            parts.append("alpn=" + urllib.parse.quote(node.alpn, safe=""))
        if node.fingerprint:
            parts.append("fp=" + urllib.parse.quote(node.fingerprint, safe=""))
        if node.allow_insecure:
            parts.append("allowInsecure=1")
        if node.host:
            parts.append("host=" + urllib.parse.quote(node.host, safe=""))
        if node.path and node.transport != PMS_TRANSPORT_GRPC:
            parts.append("path=" + urllib.parse.quote(node.path, safe=""))
        if node.service_name:
            parts.append("serviceName=" + urllib.parse.quote(node.service_name, safe=""))
        if node.grpc_mode:
            parts.append("mode=" + urllib.parse.quote(node.grpc_mode, safe=""))
        if node.security == PMS_SECURITY_REALITY:
            if node.public_key:
                parts.append("pbk=" + urllib.parse.quote(node.public_key, safe=""))
            if node.short_id:
                parts.append("sid=" + urllib.parse.quote(node.short_id, safe=""))
            if node.spider_x:
                parts.append("spx=" + urllib.parse.quote(node.spider_x, safe=""))
        return "&".join(parts)

    def pslw_nodes_to_text(self, pms_nodes: Sequence[PmsProxyNode]) -> str:
        """多节点导出成文本（一行一个链接）。"""
        lines = [link for link in (self.pslw_node_to_link(node) for node in pms_nodes) if link]
        return "\n".join(lines) + ("\n" if lines else "")

    def pslw_nodes_to_base64_text(self, pms_nodes: Sequence[PmsProxyNode]) -> str:
        """多节点导出成 base64 订阅文本。"""
        return pms_b64_encode_text(self.pslw_nodes_to_text(pms_nodes))
