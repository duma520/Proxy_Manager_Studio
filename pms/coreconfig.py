#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心配置生成

Xray 与 sing-box 两套配置生成器：入站、出站、传输/安全、路由、DNS，以及配置原子写盘。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING
import logging
import re
import json

if TYPE_CHECKING:  # 仅类型检查用，避免与运行路径模块产生循环导入
    from pms.paths import PmsRuntimePaths

from pms.constants import (
    PMS_LOGGER_NAME,
    PMS_PROTOCOL_HTTP,
    PMS_PROTOCOL_HYSTERIA2,
    PMS_PROTOCOL_SHADOWSOCKS,
    PMS_PROTOCOL_SOCKS,
    PMS_PROTOCOL_TROJAN,
    PMS_PROTOCOL_TUIC,
    PMS_PROTOCOL_VLESS,
    PMS_PROTOCOL_VMESS,
    PMS_PROTOCOL_SSR,
    PMS_PROTOCOL_NAIVE,
    PMS_PROTOCOL_ANYTLS,
    PMS_PROTOCOL_SHADOWTLS,
    PMS_PROTOCOL_SNELL,
    PMS_PROTOCOL_WIREGUARD,
    PMS_PROTOCOL_JUICITY,
    PMS_PROTOCOL_HYSTERIA1,
    PMS_PROTOCOL_SOCKS4,
    PMS_PROTOCOL_TG_WEBPROXY,
    PMS_ROUTING_MODE_DIRECT,
    PMS_ROUTING_MODE_RULE,
    PMS_RULESET_LOCAL_GEOSITE_CN,
    PMS_RULESET_SOURCE_LOCAL,
    PMS_SECURITY_NONE,
    PMS_SECURITY_REALITY,
    PMS_SECURITY_TLS,
    PMS_TRANSPORT_GRPC,
    PMS_TRANSPORT_H2,
    PMS_TRANSPORT_HTTP,
    PMS_TRANSPORT_HTTPUPGRADE,
    PMS_TRANSPORT_TCP,
    PMS_TRANSPORT_WS,
    PMS_TRANSPORT_XHTTP,
)
from pms.utils import (
    pms_atomic_write_text,
    pms_dedupe_keep_order,
    pms_json_pretty,
    pms_normalize_cert_sha256,
    pms_normalize_domain_entry,
)
from pms.models import PmsProxyNode
from pms.coreprofile import PmsCoreLaunchProfile
from pms.tor_bridge import ptb_apply_singbox_tor  # Tor 桥接出口（v1.9.43）：生成逻辑整体在 tor_bridge.py

# --------------------------------------------------------------------------------------
# sing-box uTLS 指纹白名单（v1.6.7）
# 实测 sing-box 1.14.2：unsafe / none / chrome_auto 会被拒绝，导致整个配置解析失败、
# 临时核心起不来，测速与测延迟全部拿不到结果。不在白名单里的一律回退到 chrome。
# --------------------------------------------------------------------------------------
PSC_SINGBOX_UTLS_FINGERPRINTS = (
    "chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq",
    "random", "randomized", "chrome_psk",
)
PSC_SINGBOX_UTLS_DEFAULT = "chrome"

# --------------------------------------------------------------------------------------
# Xray uTLS 指纹白名单（v1.6.7）
# 实测 Xray 26.3.27：chrome_auto / chrome_psk / none / helloworld 会被拒绝，
# 配置加载直接失败。注意 **与 sing-box 不通用**（Xray 收 unsafe、sing-box 收 chrome_psk），
# 所以两个核心各用一份名单，不能合并。
# --------------------------------------------------------------------------------------
PSC_XRAY_UTLS_FINGERPRINTS = (
    "chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq",
    "random", "randomized", "unsafe",
)
PSC_XRAY_UTLS_DEFAULT = "chrome"

# --------------------------------------------------------------------------------------
# 「明确关闭 uTLS」取值（v1.6.7）
# 分享链接里 fp=none 表示「用 Go 原生 TLS，不做指纹伪装」，此时应**省略** fingerprint
# 字段，而不是回退成 chrome（回退会把指纹悄悄改成 Chrome，语义被篡改）。
# --------------------------------------------------------------------------------------
PSC_UTLS_DISABLED_VALUES = ("none", "off", "disabled", "false", "0")


def _psc_utls_disabled(pms_fingerprint: str) -> bool:
    """判断指纹值是否表示「关闭 uTLS」（是则两个核心都不写 fingerprint 字段）。"""
    return str(pms_fingerprint or "").strip().lower() in PSC_UTLS_DISABLED_VALUES


class PmsXrayConfigBuilder:
    """生成 Xray-core 配置（JSON dict）。"""

    def __init__(self) -> None:
        self.pxc_logger = logging.getLogger(PMS_LOGGER_NAME + ".xray")

    # ------------------------------------------------------------------ 入口
    def pxc_build(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """构造完整 Xray 配置。"""
        profile = pms_profile
        config: dict[str, Any] = {
            "log": {
                "loglevel": profile.pclp_log_level_core(),
                "access": "" if not profile.access_log else str(Path("logs") / "pms_xray_access.log"),
                "error": "",
                "timestamp": True,
            },
            "stats": {},
            "policy": {
                "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
                "system": {
                    "statsInboundUplink": True,
                    "statsInboundDownlink": True,
                    "statsOutboundUplink": True,
                    "statsOutboundDownlink": True,
                },
            },
            "inbounds": self.pxc_build_inbounds(profile),
            "outbounds": self.pxc_build_outbounds(profile),
            "routing": self.pxc_build_routing(profile),
            "dns": self.pxc_build_dns(profile),
        }
        if profile.stats_api_port:
            config["api"] = {"tag": "api", "services": ["HandlerService", "LoggerService", "StatsService"]}
        return config

    def pxc_build_inbounds(self, pms_profile: PmsCoreLaunchProfile) -> list[dict[str, Any]]:
        """本地入口。Xray 一个端口只能承载一种协议，因此“混合入口”= SOCKS + 独立 HTTP 端口。"""
        profile = pms_profile
        listen = profile.pclp_listen_host()
        sniffing = {
            "enabled": bool(profile.sniff_enabled),
            "destOverride": ["http", "tls", "quic"] if profile.sniff_enabled else [],
            "routeOnly": not profile.sniff_override_dest,
        }
        inbounds: list[dict[str, Any]] = []
        socks_user = {"user": profile.username, "pass": profile.password} if profile.auth_enabled and profile.username else None
        if profile.mixed_enabled:
            inbounds.append({
                "tag": "mixed-in",
                "listen": listen,
                "port": int(profile.mixed_port),
                "protocol": "socks",
                "settings": {
                    "auth": "password" if socks_user else "noauth",
                    "accounts": [socks_user] if socks_user else [],
                    "udp": bool(profile.udp_enabled),
                    "ip": listen,
                },
                "sniffing": dict(sniffing),
            })
        if profile.http_enabled or profile.mixed_enabled:
            inbounds.append({
                "tag": "http-in",
                "listen": listen,
                "port": profile.pclp_effective_http_port(),
                "protocol": "http",
                "settings": {"accounts": [socks_user] if socks_user else [], "allowTransparent": False},
                "sniffing": dict(sniffing),
            })
        if profile.socks_enabled and int(profile.socks_port) != int(profile.mixed_port):
            inbounds.append({
                "tag": "socks-in",
                "listen": listen,
                "port": int(profile.socks_port),
                "protocol": "socks",
                "settings": {"auth": "password" if socks_user else "noauth", "accounts": [socks_user] if socks_user else [], "udp": bool(profile.udp_enabled)},
                "sniffing": dict(sniffing),
            })
        if profile.stats_api_port:
            inbounds.append({
                "tag": "api-in",
                "listen": "127.0.0.1",
                "port": int(profile.stats_api_port),
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            })
        return inbounds

    def pxc_build_outbounds(self, pms_profile: PmsCoreLaunchProfile) -> list[dict[str, Any]]:
        """出站：代理 + 直连(freedom) + 阻断(blackhole)。"""
        profile = pms_profile
        node = profile.node
        stream_settings = self.pxc_build_stream_settings(profile)
        outbound: dict[str, Any] = {
            "tag": "proxy",
            "protocol": node.protocol,
            "settings": self.pxc_build_outbound_settings(node),
            "streamSettings": stream_settings,
        }
        mux = self.pxc_build_mux_settings(profile)
        if mux:
            outbound["mux"] = mux
        direct: dict[str, Any] = {
            "tag": "direct",
            "protocol": "freedom",
            "settings": {"domainStrategy": "UseIP" if profile.domain_strategy != "AsIs" else "AsIs"},
        }
        direct["streamSettings"] = {"sockopt": {"tcpFastOpen": bool(profile.tcp_fast_open)}}
        block: dict[str, Any] = {"tag": "block", "protocol": "blackhole", "settings": {"response": {"type": "http"}}}
        return [outbound, direct, block]

    def pxc_build_outbound_settings(self, pms_node: PmsProxyNode) -> dict[str, Any]:
        """按协议生成 outbound.settings。"""
        node = pms_node
        if node.protocol == PMS_PROTOCOL_VMESS:
            user: dict[str, Any] = {
                "id": node.credential_uuid,
                "alterId": int(node.alter_id),
                "security": node.vmess_security or "auto",
            }
            return {"vnext": [{"address": node.server, "port": int(node.port), "users": [user]}]}
        if node.protocol == PMS_PROTOCOL_VLESS:
            user = {"id": node.credential_uuid, "encryption": "none"}
            if node.flow:
                user["flow"] = node.flow
            return {"vnext": [{"address": node.server, "port": int(node.port), "users": [user]}]}
        if node.protocol == PMS_PROTOCOL_TROJAN:
            return {"servers": [{"address": node.server, "port": int(node.port), "password": node.credential_password}]}
        if node.protocol == PMS_PROTOCOL_SHADOWSOCKS:
            server = {
                "address": node.server,
                "port": int(node.port),
                "method": node.method,
                "password": node.credential_password,
            }
            if node.ss_plugin:
                server["plugin"] = self.pxc_map_ss_plugin(node.ss_plugin)
                server["pluginOpts"] = node.ss_plugin_opts
            return {"servers": [server]}
        if node.protocol in (PMS_PROTOCOL_SOCKS, PMS_PROTOCOL_TG_WEBPROXY):
            server = {"address": node.server, "port": int(node.port)}
            if node.host or node.credential_password:
                server["users"] = [{"user": node.host, "pass": node.credential_password}]
            return {"servers": [server]}
        if node.protocol == PMS_PROTOCOL_HTTP:
            server = {"address": node.server, "port": int(node.port)}
            if node.host or node.credential_password:
                server["users"] = [{"user": node.host, "pass": node.credential_password}]
            return {"servers": [server]}
        if node.protocol == PMS_PROTOCOL_WIREGUARD:
            # v1.9.35（矩阵实测）：Xray 26.3.27 **认** protocol=wireguard，但要的是
            # `secretKey` + `peers[{publicKey, endpoint, allowedIPs}]`；原来落到下面那句
            # 「Xray 不支持的协议」返回 `{}`，核心就报 `invalid WireGuard secret key: key must not be empty`。
            extra = self.pxc_node_extra(node)
            return {"secretKey": extra.get("private_key", ""), "mtu": int(extra.get("mtu", 1420)),
                    "address": [extra.get("local_address") or "***已脱敏***-内网IP/32"],
                    "peers": [{"publicKey": extra.get("peer_public_key", ""), "allowedIPs": ["0.0.0.0/0", "::/0"],
                               "endpoint": f"{node.server}:{int(node.port)}"}]}
        self.pxc_logger.error("Xray 不支持的协议：%s", node.protocol)
        return {}

    @staticmethod
    def pxc_node_extra(pms_node: "PmsProxyNode") -> dict[str, Any]:
        """安全解析节点 extra_json（协议专属参数，Xray 侧 wireguard 用得上）。"""
        if not pms_node.extra_json:
            return {}
        try:
            data = json.loads(pms_node.extra_json)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def pxc_map_ss_plugin(pms_plugin: str) -> str:
        """把各客户端插件名映射到 Xray 的 plugin 名称。"""
        name = str(pms_plugin or "").strip().lower()
        if name in ("obfs-local", "simple-obfs"):
            return "obfs-local"
        if name in ("v2ray-plugin", "v2ray"):
            return "v2ray-plugin"
        return name

    @staticmethod
    def pxc_safe_utls(pms_fingerprint: str) -> str:
        """把任意 uTLS 指纹收敛到 Xray 接受的取值（未知值回退 chrome）。

        与 sing-box 的名单**不通用**，别图省事合并成一个函数。
        """
        text = str(pms_fingerprint or "").strip().lower()
        return text if text in PSC_XRAY_UTLS_FINGERPRINTS else PSC_XRAY_UTLS_DEFAULT

    def pxc_build_stream_settings(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """传输层：network + security + 各传输细节 + sockopt。"""
        profile = pms_profile
        node = profile.node
        settings: dict[str, Any] = {
            "network": node.transport or PMS_TRANSPORT_TCP,
            "security": node.security if node.security != PMS_SECURITY_REALITY else "reality",
        }
        if node.security == PMS_SECURITY_TLS:
            tls_settings: dict[str, Any] = {}
            if node.sni:
                tls_settings["serverName"] = node.sni
            if node.alpn:
                tls_settings["alpn"] = [item for item in re.split(r"[,\s]+", node.alpn) if item]
            if node.fingerprint and not _psc_utls_disabled(node.fingerprint):
                tls_settings["fingerprint"] = self.pxc_safe_utls(node.fingerprint)
            # Xray 26 已移除 allowInsecure（证书校验不可关闭），如需指纹固定请填 64 位十六进制 SHA-256
            if node.cert_sha256:
                # 防御性规范化：即使导入 / 历史脏数据带了大写或分隔符，也保证写出合法指纹，
                # 避免 Xray 因 pinnedPeerCertSha256 非法而启动失败且报错难懂。
                pinned = pms_normalize_cert_sha256(node.cert_sha256)
                if pinned:
                    tls_settings["pinnedPeerCertSha256"] = pinned
            settings["tlsSettings"] = tls_settings
        elif node.security == PMS_SECURITY_REALITY:
            reality: dict[str, Any] = {
                "show": False,
                "fingerprint": self.pxc_safe_utls(node.fingerprint or PSC_XRAY_UTLS_DEFAULT),
                "serverName": node.sni or node.host or node.server,
                "publicKey": node.public_key,
                "shortId": node.short_id or "",
                "spiderX": node.spider_x or "/",
            }
            settings["realitySettings"] = reality
        transport = node.transport
        if transport == PMS_TRANSPORT_WS:
            ws_settings: dict[str, Any] = {"path": node.path or "/"}
            if node.host:
                ws_settings["host"] = node.host
            settings["wsSettings"] = ws_settings
        elif transport == PMS_TRANSPORT_GRPC:
            grpc: dict[str, Any] = {"serviceName": node.service_name or node.path or ""}
            if node.grpc_mode:
                grpc["multiMode"] = str(node.grpc_mode).lower() in ("multi", "true", "1")
            settings["grpcSettings"] = grpc
        elif transport == PMS_TRANSPORT_HTTP:
            http_settings: dict[str, Any] = {"path": node.path or "/"}
            if node.host:
                http_settings["host"] = [item for item in re.split(r"[,\s]+", node.host) if item]
            settings["httpSettings"] = http_settings
        elif transport == PMS_TRANSPORT_H2:
            h2: dict[str, Any] = {"path": node.path or "/"}
            if node.host:
                h2["host"] = [item for item in re.split(r"[,\s]+", node.host) if item]
            settings["httpSettings"] = h2
        elif transport == PMS_TRANSPORT_XHTTP:
            xhttp: dict[str, Any] = {"path": node.path or "/", "mode": "auto"}
            if node.host:
                xhttp["host"] = node.host
            settings["xhttpSettings"] = xhttp
        elif transport == PMS_TRANSPORT_HTTPUPGRADE:
            upgrade: dict[str, Any] = {"path": node.path or "/"}
            if node.host:
                upgrade["host"] = node.host
            settings["httpupgradeSettings"] = upgrade
        elif transport == PMS_TRANSPORT_TCP and node.header_type and node.header_type not in ("none", ""):
            settings["tcpSettings"] = {
                "header": {
                    "type": node.header_type,
                    "request": {
                        "path": [item for item in (node.path or "/").split(",") if item],
                        "headers": {"Host": [item for item in (node.host or node.server).split(",") if item]},
                    },
                }
            }
        settings["sockopt"] = {
            "tcpFastOpen": bool(profile.tcp_fast_open),
            "tcpNoDelay": True,
            "domainStrategy": "UseIP" if profile.domain_strategy != "AsIs" else "AsIs",
        }
        return settings

    def pxc_build_mux_settings(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """多路复用（仅 TCP/WS/HTTPUpgrade 等非 gRPC/非 Reality 场景默认允许）。"""
        profile = pms_profile
        if not profile.mux_enabled:
            return {}
        if profile.node.transport == PMS_TRANSPORT_GRPC or profile.node.security == PMS_SECURITY_REALITY:
            return {}
        return {"enabled": True, "concurrency": max(1, int(profile.mux_concurrency)), "xudpConcurrency": 16}

    def pxc_build_routing(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """路由：API 入站 → 内网直连 → 拦截广告 → 直连域名/网段 → 兜底策略。"""
        profile = pms_profile
        rules: list[dict[str, Any]] = []
        if profile.stats_api_port:
            rules.append({"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"})
        if profile.bypass_private_ips:
            rules.append({"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"})
        if profile.block_ads_enabled and profile.block_ad_domains:
            rules.append({
                "type": "field",
                "domain": [f"domain:{item}" for item in profile.block_ad_domains],
                "outboundTag": "block",
            })
        direct_domains: list[str] = []
        direct_ips: list[str] = []
        for item in profile.bypass_domains:
            cleaned = pms_normalize_domain_entry(item)
            if not cleaned:
                continue
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", cleaned):
                direct_ips.append(cleaned)
            else:
                direct_domains.append(f"domain:{cleaned}")
        if profile.routing_mode == PMS_ROUTING_MODE_RULE:
            for item in profile.direct_domains:
                cleaned = pms_normalize_domain_entry(item)
                if not cleaned:
                    continue
                if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", cleaned):
                    direct_ips.append(cleaned)
                else:
                    direct_domains.append(f"domain:{cleaned}")
            direct_domains.append("geosite:cn")
            direct_ips.append("geoip:cn")
        if direct_domains:
            rules.append({"type": "field", "domain": direct_domains, "outboundTag": "direct"})
        if direct_ips:
            rules.append({"type": "field", "ip": direct_ips, "outboundTag": "direct"})
        if profile.routing_mode == PMS_ROUTING_MODE_DIRECT:
            rules.append({"type": "field", "network": "tcp,udp", "outboundTag": "direct"})
        routing: dict[str, Any] = {
            "domainStrategy": profile.domain_strategy or "IPIfNonMatch",
            "domainMatcher": "hybrid",
            "rules": rules,
        }
        return routing

    def pxc_build_dns(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """DNS：直连域名走本地 DNS，其余走远程加密 DNS。"""
        profile = pms_profile
        if profile.routing_mode == PMS_ROUTING_MODE_DIRECT:
            return {"servers": [profile.dns_direct], "queryStrategy": "UseIPv4"}
        return {
            "servers": [
                {"address": profile.dns_remote, "domains": []},
                {"address": profile.dns_direct, "domains": [f"domain:{item}" for item in profile.direct_domains], "expectIPs": []},
                "localhost",
            ],
            "queryStrategy": "UseIP" if profile.domain_strategy != "AsIs" else "UseIPv4",
            "disableFallback": False,
            "tag": "dns-in",
        }

class PmsSingBoxConfigBuilder:
    """生成 sing-box 配置（JSON dict）。支持 TUN 入站与 Clash API。"""

    def __init__(self, pms_paths: Optional["PmsRuntimePaths"] = None) -> None:
        self.psc_logger = logging.getLogger(PMS_LOGGER_NAME + ".singbox")
        # 只为主核心传运行路径：解析本地规则集文件要用；延迟/临时会话不需要路由规则集
        self.psc_paths = pms_paths

    # ------------------------------------------------------------------ 入口
    def psc_build(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """构造完整 sing-box 配置。"""
        profile = pms_profile
        config: dict[str, Any] = {
            "log": {
                "level": self.psc_map_log_level(profile.pclp_log_level_core()),
                "timestamp": True,
                "output": "",
            },
            "dns": self.psc_build_dns(profile),
            "inbounds": self.psc_build_inbounds(profile),
            "outbounds": self.psc_build_outbounds(profile),
            "route": self.psc_build_route(profile),
            "experimental": self.psc_build_experimental(profile),
        }
        # v1.9.35（矩阵实测）：sing-box 1.14 的 wireguard **只能在 `endpoints` 段声明**，
        # 写进 `outbounds` 会被 `outbounds[0].server: json: unknown field "server"` 拒掉；
        # 所以额外挂一段 endpoints，并把同 tag 的代理出站撤掉（route.final 仍写 `proxy`）。
        if str(profile.node.protocol) == PMS_PROTOCOL_WIREGUARD:
            config["endpoints"] = [self.psc_build_wireguard_endpoint(profile)]
            config["outbounds"] = [item for item in config["outbounds"] if str(item.get("tag")) != "proxy"]
        # v1.9.43：Tor 出口（enabled 时追加 tor-bridged 出站并把 route.final 指过去）
        ptb_apply_singbox_tor(config, profile.tor, self.psc_paths)
        return config

    def psc_build_wireguard_endpoint(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """wireguard 端点的正确形状（实测通过）：`address` + `peers[{address,port,public_key,allowed_ips}]`。"""
        node, extra = pms_profile.node, self._psc_extra(pms_profile.node)
        peer: dict[str, Any] = {"address": node.server, "port": int(node.port),
                                "public_key": extra.get("peer_public_key", ""), "allowed_ips": ["0.0.0.0/0", "::/0"]}
        if extra.get("pre_shared_key"):
            peer["pre_shared_key"] = extra["pre_shared_key"]
        reserved = self._psc_wireguard_reserved(extra)
        if reserved:
            peer["reserved"] = reserved
        endpoint: dict[str, Any] = {
            "type": "wireguard", "tag": "proxy", "address": [extra.get("local_address") or "***已脱敏***-内网IP/32"],
            "private_key": extra.get("private_key", ""), "peers": [peer], "mtu": int(extra.get("mtu", 1420)),
        }
        if extra.get("dns"):
            endpoint["dns"] = [extra["dns"]]
        return endpoint

    @staticmethod
    def _psc_wireguard_reserved(pms_extra: dict[str, Any]) -> list[int]:
        """WARP 的 `reserved` 必须是 [int,int,int]，格式不对就忽略（别污染配置让核心起不来）。"""
        raw = str(pms_extra.get("reserved") or "").strip().strip("[]")
        if not raw:
            return []
        try:
            values = [int(item) for item in (part.strip() for part in raw.split(",")) if item]
        except (TypeError, ValueError):
            return []
        return values if len(values) == 3 else []

    @staticmethod
    def psc_map_log_level(pms_level: str) -> str:
        """sing-box 用 warn 而不是 warning。"""
        level = str(pms_level or "info").lower()
        return "warn" if level == "warning" else level

    def psc_build_inbounds(self, pms_profile: PmsCoreLaunchProfile) -> list[dict[str, Any]]:
        """入口：mixed / http / socks / tun。

        注意：sing-box 1.11 起 inbound 上的 sniff / sniff_override_destination / domain_strategy
        属于“遗留字段”，1.13 起已移除 —— 嗅探由 route 规则里的 {"action":"sniff"} 承担，
        因此这里只保留端口、认证、UDP 超时等仍然有效的字段。
        """
        profile = pms_profile
        listen = profile.pclp_listen_host()
        inbounds: list[dict[str, Any]] = []
        users = [{"username": profile.username, "password": profile.password}] if profile.auth_enabled and profile.username else []
        if profile.mixed_enabled:
            inbound: dict[str, Any] = {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": listen,
                "listen_port": int(profile.mixed_port),
            }
            if users:
                inbound["users"] = users
            if profile.udp_enabled:
                inbound["udp_timeout"] = "5m"
            inbounds.append(inbound)
        if profile.http_enabled:
            appended: dict[str, Any] = {
                "type": "http",
                "tag": "http-in",
                "listen": listen,
                "listen_port": int(profile.http_port),
            }
            if users:
                appended["users"] = users
            inbounds.append(appended)
        if profile.socks_enabled:
            appended = {
                "type": "socks",
                "tag": "socks-in",
                "listen": listen,
                "listen_port": int(profile.socks_port),
            }
            if users:
                appended["users"] = users
            if profile.udp_enabled:
                appended["udp_timeout"] = "5m"
            inbounds.append(appended)
        if profile.tun_enabled:
            addresses = ["***已脱敏***-内网IP/30"]
            if profile.tun_ipv6:
                addresses.append("fdfe:dcba:9876::1/126")
            inbounds.append({
                "type": "tun",
                "tag": "tun-in",
                "interface_name": profile.tun_interface,
                "address": addresses,
                "mtu": int(profile.tun_mtu),
                "auto_route": bool(profile.tun_auto_route),
                "strict_route": bool(profile.tun_strict_route),
                "stack": profile.tun_stack or "mixed",
            })
        return inbounds

    @staticmethod
    def psc_map_domain_strategy(pms_strategy: str) -> str:
        """sing-box 的 domain_strategy 值域：prefer_ipv4 / prefer_ipv6 / ipv4_only / ipv6_only。"""
        mapping = {"AsIs": "prefer_ipv4", "IPIfNonMatch": "prefer_ipv4", "IPOnDemand": "prefer_ipv4"}
        return mapping.get(str(pms_strategy), "prefer_ipv4")

    def psc_build_dns(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """DNS：远程加密 DNS 走代理，直连域名走本地 DNS。

        实机验证要点（sing-box 1.14）：
            · 带 outbound 的 DNS 规则项已移除 → 只用「域名匹配 → 指定服务器」；
            · 用作 DNS detour 的 direct 出站不能是“空出站”，必须带 domain_resolver
              （否则启动直接 FATAL：detour to an empty direct outbound makes no sense）；
            · 解析节点自身域名需要本地解析器，因此 DNS 服务器/出站都显式指定 domain_resolver。
        """
        profile = pms_profile
        servers: list[dict[str, Any]] = [
            {"type": "https", "tag": "dns-remote", "server": self.psc_strip_dns_scheme(profile.dns_remote), "detour": "proxy", "domain_resolver": "dns-direct"},
            {"type": "udp", "tag": "dns-direct", "server": self.psc_strip_dns_scheme(profile.dns_direct), "detour": "direct"},
            {"type": "local", "tag": "dns-local"},
        ]
        direct_suffix = [
            cleaned
            for cleaned in (pms_normalize_domain_entry(item) for item in profile.direct_domains)
            if cleaned and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", cleaned)
        ]
        dns_rules: list[dict[str, Any]] = []
        if profile.routing_mode == PMS_ROUTING_MODE_RULE and direct_suffix:
            dns_rules.append({"domain_suffix": direct_suffix, "server": "dns-direct"})
        dns_rules.append({"clash_mode": "direct", "server": "dns-direct"})
        return {
            "servers": servers,
            "rules": dns_rules,
            "final": "dns-remote" if profile.routing_mode != PMS_ROUTING_MODE_DIRECT else "dns-direct",
        }

    @staticmethod
    def psc_strip_dns_scheme(pms_address: str) -> str:
        """把 https://1.1.1.1/dns-query 这类地址整理成 sing-box 需要的 host 形式。"""
        text = str(pms_address or "").strip()
        if not text:
            return "1.1.1.1"
        for prefix in ("https://", "http://", "tls://", "quic://", "udp://"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        return text.split("/")[0] or "1.1.1.1"

    def psc_build_outbounds(self, pms_profile: PmsCoreLaunchProfile) -> list[dict[str, Any]]:
        """出站：proxy / direct / block。

        direct 出站必须带 domain_resolver：它既用于解析节点域名，也避免
        “detour to an empty direct outbound” 启动致命错误（sing-box 1.14 实测）。
        """
        profile = pms_profile
        outbound = self.psc_build_proxy_outbound(profile)
        direct: dict[str, Any] = {"type": "direct", "tag": "direct", "domain_resolver": "dns-direct"}
        block: dict[str, Any] = {"type": "block", "tag": "block"}
        return [outbound, direct, block]

    def psc_build_proxy_outbound(self, pms_profile: PmsCoreLaunchProfile, pms_with_resolver: bool = True) -> dict[str, Any]:
        """按协议构造 sing-box 出站对象（pms_with_resolver 需与 dns 段一起提供）。"""
        profile = pms_profile
        node = profile.node
        if node.protocol == PMS_PROTOCOL_TUIC:
            # TUIC 由 sing-box 自身实现，无需额外字段顺序要求
            outbound: dict[str, Any] = {"type": "tuic", "tag": "proxy", "server": node.server, "server_port": int(node.port)}
        else:
            outbound = {"type": self.psc_map_protocol(node.protocol), "tag": "proxy", "server": node.server, "server_port": int(node.port)}
        if node.protocol in (PMS_PROTOCOL_VMESS, PMS_PROTOCOL_VLESS):
            outbound["uuid"] = node.credential_uuid
            if node.protocol == PMS_PROTOCOL_VMESS:
                outbound["security"] = node.vmess_security or "auto"
                outbound["alter_id"] = int(node.alter_id)
            elif node.flow:
                outbound["flow"] = node.flow
        if node.protocol in (PMS_PROTOCOL_TROJAN, PMS_PROTOCOL_HYSTERIA2):
            outbound["password"] = node.credential_password
        if node.protocol == PMS_PROTOCOL_SHADOWSOCKS:
            outbound["method"] = node.method
            outbound["password"] = node.credential_password
            plugin = self.psc_map_ss_plugin(node.ss_plugin)
            if plugin:
                outbound["plugin"] = plugin
                outbound["plugin_opts"] = node.ss_plugin_opts
        if node.protocol == PMS_PROTOCOL_TUIC:
            outbound["uuid"] = node.credential_uuid
            if node.credential_password:
                outbound["password"] = node.credential_password
            outbound["congestion_control"] = node.congestion_control or "bbr"
            outbound["udp_relay_mode"] = node.udp_relay_mode or "native"
            outbound["udp_over_stream"] = False
            outbound["zero_rtt_handshake"] = False
        if node.protocol in (PMS_PROTOCOL_SOCKS, PMS_PROTOCOL_HTTP, PMS_PROTOCOL_TG_WEBPROXY):
            if node.protocol in (PMS_PROTOCOL_SOCKS, PMS_PROTOCOL_TG_WEBPROXY):
                outbound["version"] = "5"
            if node.host:
                outbound["username"] = node.host
            if node.credential_password:
                outbound["password"] = node.credential_password
        # ---- 扩展协议（sing-box 支持的尽力生成；Xray 不支持的由校验跳过） ----
        if node.protocol == PMS_PROTOCOL_SSR:
            outbound["method"] = node.method
            outbound["password"] = node.credential_password
            extra = self._psc_extra(node)
            if extra.get("ssr_protocol") and extra["ssr_protocol"] != "origin":
                outbound["protocol"] = extra["ssr_protocol"]
                outbound["protocol_param"] = extra.get("proto_param", "")
            if extra.get("obfs") and extra["obfs"] != "plain":
                outbound["plugin"] = "obfs-local"
                outbound["plugin_opts"] = "obfs=" + str(extra.get("obfs", "")) + ";obfs-host=" + str(extra.get("obfs_param", ""))
        if node.protocol == PMS_PROTOCOL_NAIVE:
            if node.host:
                outbound["username"] = node.host
            if node.credential_password:
                outbound["password"] = node.credential_password
        if node.protocol == PMS_PROTOCOL_ANYTLS:
            # v1.9.35（矩阵探测实测）：AnyTLS 出站**没有 username 字段**，
            # 写进去 sing-box 会直接 `unknown field "username"` 拒绝整份配置。
            if node.credential_password:
                outbound["password"] = node.credential_password
        if node.protocol == PMS_PROTOCOL_SHADOWTLS:
            outbound["password"] = node.credential_password
            outbound["version"] = int(self._psc_extra(node).get("version", 3))
        if node.protocol == PMS_PROTOCOL_SNELL:
            # v1.9.35（矩阵探测实测）：Snell 出站用的是 `psk`，不是 `password`
            outbound["psk"] = node.credential_password
            outbound["version"] = int(self._psc_extra(node).get("version", 4))
        if node.protocol == PMS_PROTOCOL_JUICITY:
            outbound["uuid"] = node.credential_uuid
            if node.credential_password:
                outbound["password"] = node.credential_password
            outbound["congestion_control"] = node.congestion_control or "bbr"
            outbound["udp_relay_mode"] = node.udp_relay_mode or "native"
        if node.protocol == PMS_PROTOCOL_HYSTERIA1:
            extra = self._psc_extra(node)
            outbound["auth_str"] = extra.get("auth_str", "")
            outbound["up_mbps"] = int(extra.get("up_mbps", 100))
            outbound["down_mbps"] = int(extra.get("down_mbps", 100))
        if node.protocol == PMS_PROTOCOL_SOCKS4:
            if node.host:
                outbound["username"] = node.host
            if node.credential_password:
                outbound["password"] = node.credential_password
            outbound["version"] = "4"
        if node.protocol == PMS_PROTOCOL_WIREGUARD:
            # v1.9.35：wireguard 不走 `outbounds`（sing-box 1.14 只认 `endpoints`，见 `psc_build`），
            # 这里返回一个占位对象，真正的形状在 `psc_build_wireguard_endpoint()` 里。
            return outbound
        if node.protocol == PMS_PROTOCOL_HYSTERIA2:
            obfs = self.psc_build_obfs(node)
            if obfs:
                outbound["obfs"] = obfs
        tls = self.psc_build_tls(node)
        if tls:
            outbound["tls"] = tls
        transport = self.psc_build_transport(node)
        if transport:
            outbound["transport"] = transport
        multiplex = self.psc_build_multiplex(profile)
        if multiplex:
            outbound["multiplex"] = multiplex
        if node.protocol in (PMS_PROTOCOL_VLESS, PMS_PROTOCOL_VMESS) and profile.udp_enabled:
            outbound["packet_encoding"] = "xudp"
        if pms_with_resolver:
            # 解析节点自身域名必须走本地直连 DNS（避免“用代理解析代理服务器”的引导死锁）
            outbound["domain_resolver"] = "dns-direct"
        return outbound

    @staticmethod
    def psc_build_minimal_dns(pms_dns_direct: str = "223.5.5.5") -> dict[str, Any]:
        """给临时/批量核心用的最小 DNS 段（提供 domain_resolver 需要的服务器标签）。"""
        return {
            "servers": [
                {"type": "udp", "tag": "dns-direct", "server": pms_dns_direct},
                {"type": "local", "tag": "dns-local"},
            ],
            "final": "dns-direct",
        }

    @staticmethod
    def psc_map_protocol(pms_protocol: str) -> str:
        """界面协议名 → sing-box outbound type。"""
        mapping = {
            PMS_PROTOCOL_VMESS: "vmess",
            PMS_PROTOCOL_VLESS: "vless",
            PMS_PROTOCOL_TROJAN: "trojan",
            PMS_PROTOCOL_SHADOWSOCKS: "shadowsocks",
            PMS_PROTOCOL_HYSTERIA2: "hysteria2",
            PMS_PROTOCOL_TUIC: "tuic",
            PMS_PROTOCOL_SOCKS: "socks",
            PMS_PROTOCOL_HTTP: "http",
            PMS_PROTOCOL_SSR: "shadowsocksr",
            PMS_PROTOCOL_NAIVE: "naive",
            PMS_PROTOCOL_ANYTLS: "anytls",
            PMS_PROTOCOL_SHADOWTLS: "shadowtls",
            PMS_PROTOCOL_SNELL: "snell",
            PMS_PROTOCOL_WIREGUARD: "wireguard",
            PMS_PROTOCOL_JUICITY: "juicity",
            PMS_PROTOCOL_HYSTERIA1: "hysteria",
            PMS_PROTOCOL_SOCKS4: "socks",
            PMS_PROTOCOL_TG_WEBPROXY: "socks",
        }
        return mapping.get(pms_protocol, pms_protocol)

    @staticmethod
    def _psc_extra(pms_node: "PmsProxyNode") -> dict[str, Any]:
        """安全解析节点 extra_json（协议专属参数）。"""
        if not pms_node.extra_json:
            return {}
        try:
            data = json.loads(pms_node.extra_json)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def psc_map_ss_plugin(pms_plugin: str) -> str:
        """Shadowsocks 插件名映射（sing-box 支持 obfs-local / v2ray-plugin / shadow-tls）。"""
        name = str(pms_plugin or "").strip().lower()
        if name in ("obfs-local", "simple-obfs", "obfs"):
            return "obfs-local"
        if name in ("v2ray-plugin", "v2ray"):
            return "v2ray-plugin"
        if name in ("shadow-tls", "shadowtls"):
            return "shadow-tls"
        return name

    def psc_build_obfs(self, pms_node: PmsProxyNode) -> dict[str, Any]:
        """Hysteria2 混淆（salamander）。"""
        if not pms_node.obfs:
            return {}
        obfs: dict[str, Any] = {"type": pms_node.obfs}
        if pms_node.obfs_password:
            obfs["password"] = pms_node.obfs_password
        return obfs

    def psc_build_tls(self, pms_node: PmsProxyNode) -> dict[str, Any]:
        """TLS / Reality / uTLS 配置。"""
        node = pms_node
        if node.security == PMS_SECURITY_NONE and node.protocol not in (PMS_PROTOCOL_HYSTERIA2, PMS_PROTOCOL_TUIC):
            return {}
        tls: dict[str, Any] = {
            "enabled": True,
            "insecure": bool(node.allow_insecure),
        }
        if node.sni:
            tls["server_name"] = node.sni
        if node.alpn:
            tls["alpn"] = [item for item in re.split(r"[,\s]+", node.alpn) if item]
        if node.fingerprint and not _psc_utls_disabled(node.fingerprint):
            tls["utls"] = {"enabled": True, "fingerprint": self.psc_safe_utls(node.fingerprint)}
        if node.security == PMS_SECURITY_REALITY:
            # v1.6.7：sing-box 1.14.x 已移除 tls.reality.spider_x，写了会整体解析失败。
            # 该值只用于分享链接（spx=）与 Xray 配置（spiderX），不再写进 sing-box 配置。
            tls["reality"] = {
                "enabled": True,
                "public_key": node.public_key,
                "short_id": node.short_id or "",
            }
            # Reality 必须带 uTLS，即使原值是 none 也强制给一个合法指纹。
            tls["utls"] = {"enabled": True, "fingerprint": self.psc_safe_utls(node.fingerprint or PSC_SINGBOX_UTLS_DEFAULT)}
        return tls

    @staticmethod
    def psc_safe_utls(pms_fingerprint: str) -> str:
        """把任意 uTLS 指纹收敛到 sing-box 接受的取值（未知值回退 chrome）。"""
        text = str(pms_fingerprint or "").strip().lower()
        return text if text in PSC_SINGBOX_UTLS_FINGERPRINTS else PSC_SINGBOX_UTLS_DEFAULT

    @staticmethod
    def psc_build_transport(pms_node: PmsProxyNode) -> dict[str, Any]:
        """传输层（ws / grpc / http / httpupgrade / xhttp→httpupgrade 兼容）。"""
        node = pms_node
        transport = node.transport
        if transport == PMS_TRANSPORT_WS:
            result: dict[str, Any] = {"type": "ws", "path": node.path or "/"}
            if node.host:
                result["headers"] = {"Host": node.host}
            return result
        if transport == PMS_TRANSPORT_GRPC:
            return {"type": "grpc", "service_name": node.service_name or node.path or ""}
        if transport == PMS_TRANSPORT_HTTP:
            result = {"type": "http", "path": node.path or "/"}
            if node.host:
                result["host"] = [item for item in re.split(r"[,\s]+", node.host) if item]
            return result
        if transport == PMS_TRANSPORT_H2:
            result = {"type": "http", "path": node.path or "/"}
            if node.host:
                result["host"] = [item for item in re.split(r"[,\s]+", node.host) if item]
            return result
        if transport == PMS_TRANSPORT_HTTPUPGRADE:
            result = {"type": "httpupgrade", "path": node.path or "/"}
            if node.host:
                result["host"] = node.host
            return result
        if transport == PMS_TRANSPORT_XHTTP:
            result = {"type": "httpupgrade", "path": node.path or "/"}
            if node.host:
                result["host"] = node.host
            return result
        return {}

    def psc_build_multiplex(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """多路复用（gRPC / Reality / Hysteria2 / TUIC / SOCKS / HTTP 场景下禁用）。"""
        profile = pms_profile
        if not profile.mux_enabled:
            return {}
        if profile.node.transport == PMS_TRANSPORT_GRPC:
            return {}
        if profile.node.security == PMS_SECURITY_REALITY:
            return {}
        if profile.node.protocol in (
            PMS_PROTOCOL_HYSTERIA2,
            PMS_PROTOCOL_TUIC,
            PMS_PROTOCOL_SOCKS,
            PMS_PROTOCOL_HTTP,
        ):
            return {}
        return {"enabled": True, "protocol": "h2mux", "max_streams": max(1, int(profile.mux_concurrency))}

    def psc_build_route(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """路由规则：嗅探 → DNS 劫持 → 域名解析 → 内网直连 → 广告阻断 → 直连域名 → 兜底。

        注意：sing-box 1.12 起已移除内置 geoip 规则，大陆直连改用
        （1）域名后缀表（内置常用域名 + .cn 关键字）与（2）可选远程规则集 rule_set。
        """
        profile = pms_profile
        rules: list[dict[str, Any]] = []
        if profile.sniff_enabled:
            rules.append({"action": "sniff"})
        rules.append({"protocol": "dns", "action": "hijack-dns"})
        rules.append({"action": "resolve", "strategy": self.psc_map_domain_strategy(profile.domain_strategy)})
        if profile.bypass_private_ips:
            rules.append({"ip_is_private": True, "action": "route", "outbound": "direct"})
        if profile.block_ads_enabled and profile.block_ad_domains:
            rules.append({"domain_suffix": list(profile.block_ad_domains), "action": "route", "outbound": "block"})
        direct_suffix: list[str] = []
        for item in list(profile.bypass_domains) + (list(profile.direct_domains) if profile.routing_mode == PMS_ROUTING_MODE_RULE else []):
            cleaned = pms_normalize_domain_entry(item)
            if not cleaned or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", cleaned):
                continue
            direct_suffix.append(cleaned)
        rule_set_tags: list[str] = []
        if profile.routing_mode == PMS_ROUTING_MODE_RULE and profile.use_remote_ruleset:
            rule_set_tags.append("geosite-cn")
        if direct_suffix:
            rules.append({"domain_suffix": pms_dedupe_keep_order(direct_suffix), "action": "route", "outbound": "direct"})
        if profile.routing_mode == PMS_ROUTING_MODE_RULE:
            rules.append({"domain_keyword": [".cn"], "action": "route", "outbound": "direct"})
        if rule_set_tags:
            rules.append({"rule_set": rule_set_tags, "action": "route", "outbound": "direct"})
        final_outbound = "proxy" if profile.routing_mode != PMS_ROUTING_MODE_DIRECT else "direct"
        route: dict[str, Any] = {
            "rules": rules,
            "final": final_outbound,
            "auto_detect_interface": True,
            "default_domain_resolver": {"server": "dns-direct", "strategy": self.psc_map_domain_strategy(profile.domain_strategy)},
        }
        if rule_set_tags:
            route["rule_set"] = [self.psc_build_geosite_ruleset(profile)]
        return route

    def psc_build_geosite_ruleset(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """geosite-cn 规则集条目：本地文件优先，本地缺失才回落到远程下载。

        本地来源读 `core\\srss\\geosite-cn.srs`（`type=local` + `path`），
        不产生任何联网请求；远程来源沿用 `type=remote` + `download_detour`。
        本地文件不存在时自动回落远程，避免「选了本地却一条规则都没有」的静默失效。
        """
        profile = pms_profile
        if profile.ruleset_source == PMS_RULESET_SOURCE_LOCAL and self.psc_paths is not None:
            try:
                local_file = self.psc_paths.prt_ruleset_file(PMS_RULESET_LOCAL_GEOSITE_CN)
            except OSError as exc:
                self.psc_logger.warning("本地规则集路径解析失败：%s", exc)
                local_file = None
            if local_file is not None and local_file.is_file():
                return {
                    "type": "local",
                    "tag": "geosite-cn",
                    "format": "binary",
                    "path": str(local_file),
                }
            self.psc_logger.warning("本地规则集不存在，回落到远程下载：%s", local_file)
        return {
            "type": "remote",
            "tag": "geosite-cn",
            "format": "binary",
            "url": profile.ruleset_geosite_cn_url,
            "download_detour": "proxy",
            "update_interval": "7d",
        }

    def psc_build_experimental(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """实验特性：Clash API（连接与流量统计）与缓存文件。"""
        profile = pms_profile
        experimental: dict[str, Any] = {}
        if profile.clash_api_port:
            experimental["clash_api"] = {
                "external_controller": f"127.0.0.1:{int(profile.clash_api_port)}",
                "default_mode": "rule" if profile.routing_mode == PMS_ROUTING_MODE_RULE else "global",
            }
        experimental["cache_file"] = {"enabled": True, "store_fakeip": False}
        return experimental

class PmsCoreConfigWriter:
    """把配置字典写盘，并保证 UTF-8 与缩进一致。"""

    def __init__(self) -> None:
        self.pcw_logger = logging.getLogger(PMS_LOGGER_NAME + ".config")

    def pcw_write(self, pms_config: dict[str, Any], pms_target: Path) -> bool:
        """写配置文件，返回是否成功。"""
        text = pms_json_pretty(pms_config)
        ok = pms_atomic_write_text(Path(pms_target), text)
        if ok:
            self.pcw_logger.debug("核心配置已写入：%s", pms_target)
        else:
            self.pcw_logger.error("核心配置写入失败：%s", pms_target)
        return ok
