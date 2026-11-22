#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扩展协议解析与生成（Mixin）

9 种协议：SSR / NaiveProxy / AnyTLS / ShadowTLS / Snell / WireGuard / Juicity / Hysteria1 / Socks4。

作为 mixin 挂到 `PmsShareLinkParser` / `PmsShareLinkWriter` 上，避免主文件 `sharelinks.py` 超过
宪法规定的 1000 行上限。所有方法都不允许抛异常（与现有解析器一致）。协议专属参数统一存于
节点 `extra_json`（模型已预留该字段且已落库），不新增模型列、不改数据库结构。

说明：解析/存储/导入为可用；真正连上取决于核心能力（Xray 不支持的 7 种由
`PmsProxyNode.pnode_validate_core_support` 跳过并提示，与现有 Hysteria2/TUIC 同待遇）。
"""

from __future__ import annotations

import json
import urllib.parse
import uuid
from typing import (
    Any,
    Optional,
)

from pms.constants import (
    PMS_EXTRA_PROTOCOLS,
    PMS_PROTOCOL_ANYTLS,
    PMS_PROTOCOL_HYSTERIA1,
    PMS_PROTOCOL_JUICITY,
    PMS_PROTOCOL_NAIVE,
    PMS_PROTOCOL_SHADOWTLS,
    PMS_PROTOCOL_SNELL,
    PMS_PROTOCOL_SOCKS4,
    PMS_PROTOCOL_SSR,
    PMS_PROTOCOL_WIREGUARD,
    PMS_SECURITY_NONE,
    PMS_SECURITY_TLS,
)
from pms.utils import (
    pms_b64_decode_loose,
    pms_b64_encode_text,
    pms_safe_int,
)
from pms.models import PmsProxyNode


# --------------------------------------------------------------------------------------
# 解析 Mixin
# --------------------------------------------------------------------------------------
class PmsShareLinkParserExtraMixin:
    """9 种扩展协议的解析方法（复用主类的 pslp_* helper）。"""

    def pslxp_dispatch(self, pms_line: str, pms_protocol: str) -> Optional[PmsProxyNode]:
        """按协议分发到具体解析方法。"""
        if pms_protocol == PMS_PROTOCOL_SSR:
            return self.pslxp_parse_ssr(pms_line)
        if pms_protocol == PMS_PROTOCOL_NAIVE:
            return self.pslxp_parse_naive(pms_line)
        if pms_protocol == PMS_PROTOCOL_ANYTLS:
            return self.pslxp_parse_anytls(pms_line)
        if pms_protocol == PMS_PROTOCOL_SHADOWTLS:
            return self.pslxp_parse_shadowtls(pms_line)
        if pms_protocol == PMS_PROTOCOL_SNELL:
            return self.pslxp_parse_snell(pms_line)
        if pms_protocol == PMS_PROTOCOL_WIREGUARD:
            return self.pslxp_parse_wireguard(pms_line)
        if pms_protocol == PMS_PROTOCOL_JUICITY:
            return self.pslxp_parse_juicity(pms_line)
        if pms_protocol == PMS_PROTOCOL_HYSTERIA1:
            return self.pslxp_parse_hysteria1(pms_line)
        if pms_protocol == PMS_PROTOCOL_SOCKS4:
            return self.pslxp_parse_socks4(pms_line)
        return None

    # ------------------------------------------------------------------ SSR
    def pslxp_parse_ssr(self, pms_line: str) -> Optional[PmsProxyNode]:
        """SSR：ssr://base64(host:port:protocol:method:obfs:password)/?obfsparam=&protoparam=&remarks=&group="""
        body = pms_line[len("ssr://"):]
        main, _, params = body.partition("/?")
        decoded = pms_b64_decode_loose(main)
        if not decoded or decoded.count(":") < 5:
            return None
        host, port, ssr_protocol, method, obfs, password = decoded.split(":", 5)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_SSR)
        node.server = host.strip().strip("[]")
        node.port = pms_safe_int(port, 443, 0, 65535)
        node.method = method.strip().lower()
        node.credential_password = password
        extra: dict[str, Any] = {"ssr_protocol": ssr_protocol.strip(), "obfs": obfs.strip()}
        if params:
            query = urllib.parse.parse_qs(params, keep_blank_values=True)
            pick = lambda key: (query.get(key) or [""])[0]  # noqa: E731
            extra["obfs_param"] = pms_b64_decode_loose(pick("obfsparam")) or ""
            extra["proto_param"] = pms_b64_decode_loose(pick("protoparam")) or ""
            remarks = pms_b64_decode_loose(pick("remarks"))
            group = pms_b64_decode_loose(pick("group"))
            if remarks:
                node.name = remarks.strip()
            if group:
                node.group_name = group.strip()
        node.extra_json = json.dumps(extra, ensure_ascii=False)
        node.remark = "经 ssr:// 导入"
        return node

    # ------------------------------------------------------------------ NaiveProxy
    def pslxp_parse_naive(self, pms_line: str) -> Optional[PmsProxyNode]:
        """NaiveProxy：naive+https://user:pass@host:port?...#名称。

        userinfo 有**两种**写法，服务端都收：明文 `user:pass`，或整体 base64 的
        `dXNlcjpwYXNz`（v1.9.52 补）。原来只认明文，真实链接多是用 base64 的，
        于是「解析成功但凭据悄悄丢了」——节点看着能用，连上必然 401。
        """
        body, name = self.pslp_split_fragment(pms_line.split("://", 1)[1])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        credential, host_port = head.rsplit("@", 1)
        host, port = self.pslp_split_host_port(host_port, 443)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_NAIVE)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        credential = urllib.parse.unquote(credential.strip())
        if ":" not in credential:
            # 明文形式一定含 `:`；不含就试 base64（解出来仍必须含 `:`，否则判为「真的没凭据」）
            decoded = pms_b64_decode_loose(credential)
            if ":" in decoded:
                credential = decoded
            elif credential:
                self.pslp_logger.warning(
                    "naive:// 链接的凭据既不是 user:pass、base64 也解不出来，已按无凭据导入（服务器:%s:%s）",
                    node.server, node.port,
                )
        if ":" in credential:
            username, password = credential.split(":", 1)
            node.host = username.strip()
            node.credential_password = password
        node.security = PMS_SECURITY_TLS
        query_dict = self.pslp_query_dict(query)
        node.sni = self.pslp_pick(query_dict, ("sni", "peer"), "")
        node.allow_insecure = self.pslp_bool_text(self.pslp_pick(query_dict, ("insecure", "allowinsecure"), ""))
        node.remark = "经 naive:// 导入"
        return node

    # ------------------------------------------------------------------ AnyTLS
    def pslxp_parse_anytls(self, pms_line: str) -> Optional[PmsProxyNode]:
        """AnyTLS：anytls://user:pass@host:port?...#名称。"""
        body, name = self.pslp_split_fragment(pms_line.split("://", 1)[1])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        credential, host_port = head.rsplit("@", 1)
        host, port = self.pslp_split_host_port(host_port, 443)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_ANYTLS)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        credential = urllib.parse.unquote(credential.strip())
        if ":" in credential:
            username, password = credential.split(":", 1)
            node.host = username.strip()
            node.credential_password = password
        else:
            node.credential_password = credential
        node.security = PMS_SECURITY_TLS
        self.pslp_apply_stream_fields(node, self.pslp_query_dict(query))
        node.remark = "经 anytls:// 导入"
        return node

    # ------------------------------------------------------------------ ShadowTLS
    def pslxp_parse_shadowtls(self, pms_line: str) -> Optional[PmsProxyNode]:
        """ShadowTLS：shadowtls://password@host:port?version=3&sni=...#名称。"""
        body, name = self.pslp_split_fragment(pms_line.split("://", 1)[1])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        password, host_port = head.rsplit("@", 1)
        host, port = self.pslp_split_host_port(host_port, 443)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_SHADOWTLS)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        node.credential_password = urllib.parse.unquote(password.strip())
        node.security = PMS_SECURITY_TLS
        query_dict = self.pslp_query_dict(query)
        node.sni = self.pslp_pick(query_dict, ("sni", "peer"), "")
        extra = {"version": pms_safe_int(self.pslp_pick(query_dict, ("version",), "3"), 3, 1, 3)}
        node.extra_json = json.dumps(extra, ensure_ascii=False)
        node.remark = "经 shadowtls:// 导入"
        return node

    # ------------------------------------------------------------------ Snell
    def pslxp_parse_snell(self, pms_line: str) -> Optional[PmsProxyNode]:
        """Snell：snell://password@host:port?version=4#名称。"""
        body, name = self.pslp_split_fragment(pms_line.split("://", 1)[1])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        password, host_port = head.rsplit("@", 1)
        host, port = self.pslp_split_host_port(host_port, 443)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_SNELL)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        node.credential_password = urllib.parse.unquote(password.strip())
        node.security = PMS_SECURITY_TLS
        query_dict = self.pslp_query_dict(query)
        extra = {"version": pms_safe_int(self.pslp_pick(query_dict, ("version",), "4"), 4, 1, 4)}
        node.extra_json = json.dumps(extra, ensure_ascii=False)
        node.remark = "经 snell:// 导入"
        return node

    # ------------------------------------------------------------------ WireGuard
    def pslxp_parse_wireguard(self, pms_line: str) -> Optional[PmsProxyNode]:
        """WireGuard：wireguard://publickey@host:port?publickey=&privatekey=&ip=&mtu=&dns=...#名称。"""
        body, name = self.pslp_split_fragment(pms_line.split("://", 1)[1])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        pubkey, host_port = head.rsplit("@", 1)
        host, port = self.pslp_split_host_port(host_port, 51820)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_WIREGUARD)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        query_dict = self.pslp_query_dict(query)
        extra: dict[str, Any] = {
            "peer_public_key": (pubkey.strip() or self.pslp_pick(query_dict, ("publickey", "public"), "")),
            "private_key": self.pslp_pick(query_dict, ("privatekey", "private"), ""),
            "pre_shared_key": self.pslp_pick(query_dict, ("presharedkey", "psk"), ""),
            "local_address": self.pslp_pick(query_dict, ("ip", "localaddress", "address"), ""),
            "mtu": pms_safe_int(self.pslp_pick(query_dict, ("mtu",), "1420"), 1420, 1, 65535),
            "reserved": self.pslp_pick(query_dict, ("reserved",), ""),
            "dns": self.pslp_pick(query_dict, ("dns",), ""),
        }
        node.extra_json = json.dumps(extra, ensure_ascii=False)
        node.remark = "经 wireguard:// 导入"
        return node

    # ------------------------------------------------------------------ Juicity
    def pslxp_parse_juicity(self, pms_line: str) -> Optional[PmsProxyNode]:
        """Juicity：juicity://uuid:password@host:port?congestion_control=&udp_relay_mode=#名称。"""
        body, name = self.pslp_split_fragment(pms_line.split("://", 1)[1])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        credential, host_port = head.rsplit("@", 1)
        credential = urllib.parse.unquote(credential.strip())
        host, port = self.pslp_split_host_port(host_port, 443)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_JUICITY)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        if ":" in credential:
            uuid_text, password = credential.split(":", 1)
            node.credential_uuid = uuid_text.strip()
            node.credential_password = password
        else:
            node.credential_uuid = credential
        node.security = PMS_SECURITY_TLS
        query_dict = self.pslp_query_dict(query)
        node.congestion_control = self.pslp_pick(query_dict, ("congestion_control", "congestion"), "bbr").lower()
        node.udp_relay_mode = self.pslp_pick(query_dict, ("udp_relay_mode", "udp-relay-mode"), "native").lower()
        node.remark = "经 juicity:// 导入"
        if not node.credential_uuid:
            node.credential_uuid = str(uuid.uuid4())
        return node

    # ------------------------------------------------------------------ Hysteria1
    def pslxp_parse_hysteria1(self, pms_line: str) -> Optional[PmsProxyNode]:
        """Hysteria1：hysteria1://auth@host:port?peer=&upmbps=&downmbps=&insecure=#名称。"""
        body, name = self.pslp_split_fragment(pms_line.split("://", 1)[1])
        head, _, query = body.partition("?")
        if "@" not in head:
            return None
        auth, host_port = head.rsplit("@", 1)
        host, port = self.pslp_split_host_port(host_port, 443)
        node = PmsProxyNode(protocol=PMS_PROTOCOL_HYSTERIA1)
        node.name = name
        node.server = host.strip("[]")
        node.port = port
        query_dict = self.pslp_query_dict(query)
        extra: dict[str, Any] = {
            "auth_str": urllib.parse.unquote(auth.strip()),
            "up_mbps": pms_safe_int(self.pslp_pick(query_dict, ("upmbps", "up"), "100"), 100, 1, 100000),
            "down_mbps": pms_safe_int(self.pslp_pick(query_dict, ("downmbps", "down"), "100"), 100, 1, 100000),
        }
        node.extra_json = json.dumps(extra, ensure_ascii=False)
        node.security = PMS_SECURITY_TLS
        node.sni = self.pslp_pick(query_dict, ("peer", "sni"), "")
        node.allow_insecure = self.pslp_bool_text(self.pslp_pick(query_dict, ("insecure", "allowinsecure"), "1"), True)
        node.remark = "经 hysteria1:// 导入"
        return node

    # ------------------------------------------------------------------ Socks4
    def pslxp_parse_socks4(self, pms_line: str) -> Optional[PmsProxyNode]:
        """Socks4：socks4://user:pass@host:port，复用 Socks/HTTP 解析逻辑，仅协议标记不同。"""
        node = self.pslp_parse_socks_http(pms_line, PMS_PROTOCOL_SOCKS4)
        if node is None:
            return None
        node.remark = "经 socks4:// 导入"
        return node


# --------------------------------------------------------------------------------------
# 生成 Mixin
# --------------------------------------------------------------------------------------
class PmsShareLinkWriterExtraMixin:
    """9 种扩展协议的分享链接生成（导出 / 二维码 / 复制）。"""

    def pslxp_node_to_link(self, pms_node: PmsProxyNode) -> str:
        """按协议还原成分享链接（不支持的组合返回空串）。"""
        node = pms_node
        name = urllib.parse.quote(node.pnode_display_name(), safe="")
        try:
            extra = json.loads(node.extra_json or "{}") if node.extra_json else {}
        except ValueError:
            extra = {}
        host_port = self.pslw_host_port(node)
        if node.protocol == PMS_PROTOCOL_SSR:
            main = "{}:{}:{}:{}:{}:{}".format(
                node.server,
                int(node.port),
                extra.get("ssr_protocol", "origin"),
                node.method or "aes-256-cfb",
                extra.get("obfs", "plain"),
                node.credential_password,
            )
            payload = pms_b64_encode_text(main)
            tails = []
            if extra.get("obfs_param"):
                tails.append("obfsparam=" + urllib.parse.quote(pms_b64_encode_text(extra["obfs_param"]), safe=""))
            if extra.get("proto_param"):
                tails.append("protoparam=" + urllib.parse.quote(pms_b64_encode_text(extra["proto_param"]), safe=""))
            if node.name:
                tails.append("remarks=" + urllib.parse.quote(pms_b64_encode_text(node.name), safe=""))
            tail = ("/?" + "&".join(tails)) if tails else ""
            return "ssr://" + payload + tail
        if node.protocol == PMS_PROTOCOL_NAIVE:
            cred = urllib.parse.quote(f"{node.host}:{node.credential_password}", safe="") + "@" if (node.host or node.credential_password) else ""
            return f"naive+https://{cred}{host_port}#{name}"
        if node.protocol == PMS_PROTOCOL_ANYTLS:
            cred = urllib.parse.quote(f"{node.host}:{node.credential_password}", safe="") + "@" if (node.host or node.credential_password) else ""
            return f"anytls://{cred}{host_port}#{name}"
        if node.protocol == PMS_PROTOCOL_SHADOWTLS:
            cred = urllib.parse.quote(node.credential_password, safe="") + "@" if node.credential_password else ""
            return f"shadowtls://{cred}{host_port}#{name}"
        if node.protocol == PMS_PROTOCOL_SNELL:
            cred = urllib.parse.quote(node.credential_password, safe="") + "@" if node.credential_password else ""
            return f"snell://{cred}{host_port}#{name}"
        if node.protocol == PMS_PROTOCOL_WIREGUARD:
            params = [
                "publickey=" + urllib.parse.quote(extra.get("peer_public_key", ""), safe=""),
                "privatekey=" + urllib.parse.quote(extra.get("private_key", ""), safe=""),
            ]
            if extra.get("local_address"):
                params.append("ip=" + urllib.parse.quote(extra["local_address"], safe=""))
            if extra.get("mtu"):
                params.append("mtu=" + str(extra["mtu"]))
            if extra.get("dns"):
                params.append("dns=" + urllib.parse.quote(extra["dns"], safe=""))
            pub = urllib.parse.quote(extra.get("peer_public_key", ""), safe="")
            return f"wireguard://{pub}@{host_port}?{'&'.join(params)}#{name}"
        if node.protocol == PMS_PROTOCOL_JUICITY:
            cred = f"{node.credential_uuid}:{node.credential_password}" if node.credential_password else node.credential_uuid
            return f"juicity://{urllib.parse.quote(cred, safe='')}@{host_port}#{name}"
        if node.protocol == PMS_PROTOCOL_HYSTERIA1:
            auth = urllib.parse.quote(extra.get("auth_str", ""), safe="")
            params = [
                "peer=" + urllib.parse.quote(node.sni or "", safe=""),
                "upmbps=" + str(extra.get("up_mbps", 100)),
                "downmbps=" + str(extra.get("down_mbps", 100)),
            ]
            return f"hysteria1://{auth}@{host_port}?{'&'.join(params)}#{name}"
        if node.protocol == PMS_PROTOCOL_SOCKS4:
            cred = urllib.parse.quote(f"{node.host}:{node.credential_password}", safe="") + "@" if (node.host or node.credential_password) else ""
            return f"socks4://{cred}{host_port}#{name}"
        return ""
