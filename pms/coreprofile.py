#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动配置档

一次核心启动所需的全部参数（节点 + 设置 + 端口 + 路由 + TUN）与启动前自检。
"""

from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from typing import Any

from pms.constants import (
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
    PMS_DEFAULT_RULESET_GEOSITE_CN,
    PMS_DEFAULT_TUN_INTERFACE,
    PMS_ROUTING_MODE_RULE,
    PMS_RULESET_SOURCE_LOCAL,
)
from pms.models import PmsProxyNode
from pms.tor_bridge import ptb_settings_payload  # Tor 桥接载荷（桥接行解析在 tor_bridge.py）

# --------------------------------------------------------------------------------------
# 启动配置档（把设置 + 节点整理成核心生成器需要的输入）
# --------------------------------------------------------------------------------------
@dataclass
class PmsCoreLaunchProfile:
    """一次核心启动所需的全部参数（配置生成器的唯一输入）。"""

    node: PmsProxyNode
    core_kind: str = PMS_CORE_KIND_SINGBOX
    routing_mode: str = PMS_ROUTING_MODE_RULE
    bypass_domains: list[str] = field(default_factory=list)
    direct_domains: list[str] = field(default_factory=list)
    block_ad_domains: list[str] = field(default_factory=list)
    block_ads_enabled: bool = True
    listen_address: str = "127.0.0.1"
    mixed_enabled: bool = True
    mixed_port: int = 30808   # v1.9.58：20808 → 30808，避开用户外部代理端口（见 settings_defaults.py 注释）
    http_enabled: bool = False
    http_port: int = 20809
    socks_enabled: bool = False
    socks_port: int = 20810
    udp_enabled: bool = True
    sniff_enabled: bool = True
    auth_enabled: bool = False
    username: str = ""
    password: str = ""
    allow_lan: bool = False
    log_level: str = "info"
    access_log: bool = False
    mux_enabled: bool = False
    mux_concurrency: int = 8
    tcp_fast_open: bool = True
    sniff_override_dest: bool = True
    tun_enabled: bool = False
    tun_interface: str = PMS_DEFAULT_TUN_INTERFACE
    tun_stack: str = "mixed"
    tun_mtu: int = 9000
    tun_auto_route: bool = True
    tun_strict_route: bool = False
    tun_ipv6: bool = False
    clash_api_port: int = 0
    stats_api_port: int = 0
    dns_remote: str = "https://1.1.1.1/dns-query"
    dns_direct: str = "223.5.5.5"
    domain_strategy: str = "IPIfNonMatch"
    use_remote_ruleset: bool = False
    ruleset_source: str = PMS_RULESET_SOURCE_LOCAL
    ruleset_geosite_cn_url: str = PMS_DEFAULT_RULESET_GEOSITE_CN
    bypass_private_ips: bool = True
    tor: dict[str, Any] = field(default_factory=dict)  # Tor 桥接出口载荷（空 = 不启用）

    def pclp_primary_port(self) -> int:
        """系统代理要指向的端口（Windows 只认 HTTP 代理，优先返回 HTTP 入口）。"""
        if self.core_kind == PMS_CORE_KIND_XRAY:
            if self.mixed_enabled or self.http_enabled:
                return self.pclp_effective_http_port()
            return int(self.socks_port)
        if self.mixed_enabled:
            return int(self.mixed_port)
        if self.http_enabled:
            return int(self.http_port)
        return int(self.socks_port)

    def pclp_effective_http_port(self) -> int:
        """实际使用的 HTTP 入口端口。

        Xray 不允许 SOCKS 与 HTTP 绑定同一端口，因此开启“混合入口”时：
            显式设置了独立 HTTP 端口 → 用它；否则自动使用 混合端口 + 1。
        sing-box 的 mixed 本身就同时支持 HTTP，直接用混合端口。
        """
        if self.core_kind == PMS_CORE_KIND_XRAY:
            if self.http_enabled and int(self.http_port) != int(self.mixed_port):
                return int(self.http_port)
            if self.mixed_enabled:
                return int(self.mixed_port) + 1
            return int(self.http_port)
        return int(self.http_port if self.http_enabled else self.mixed_port)

    def pclp_listen_host(self) -> str:
        return "0.0.0.0" if self.allow_lan else (self.listen_address or "127.0.0.1")

    def pclp_log_level_core(self) -> str:
        """把界面级别映射成核心日志级别（xray 用 warning / sing-box 用 warn）。"""
        mapping = {"debug": "debug", "info": "info", "warning": "warning", "error": "error"}
        return mapping.get(str(self.log_level).lower(), "info")

    def pclp_verify(self) -> list[str]:
        """启动前自检：返回错误列表（空表示可启动）。"""
        errors: list[str] = []
        errors.extend(self.node.pnode_validate())
        errors.extend(self.node.pnode_validate_core_support(self.core_kind))
        if not (self.mixed_enabled or self.http_enabled or self.socks_enabled or self.tun_enabled):
            errors.append("至少要启用一个本地入口（混合 / HTTP / SOCKS / TUN）")
        ports: list[tuple[str, int]] = []
        if self.mixed_enabled:
            ports.append(("混合入口", int(self.mixed_port)))
        if self.core_kind == PMS_CORE_KIND_XRAY:
            # Xray 的“混合入口”= SOCKS + 独立 HTTP 端口（pclp_effective_http_port 会自动避让）
            if self.mixed_enabled or self.http_enabled:
                ports.append(("HTTP 入口", int(self.pclp_effective_http_port())))
        elif self.http_enabled:
            ports.append(("HTTP 入口", int(self.http_port)))
        if self.socks_enabled:
            ports.append(("SOCKS 入口", int(self.socks_port)))
        seen: dict[int, str] = {}
        for title, port in ports:
            if not 0 < int(port) < 65536:
                errors.append(f"{title} 端口非法：{port}")
                continue
            if port in seen:
                errors.append(f"{title} 端口 {port} 与「{seen[port]}」重复（Xray 不允许同端口多协议，已自动改用相邻端口）")
            else:
                seen[port] = title
        if self.tun_enabled and self.core_kind != PMS_CORE_KIND_SINGBOX:
            errors.append("TUN 模式需要 sing-box 核心（Xray 不支持 TUN 入站）")
        if self.node.allow_insecure and self.core_kind == PMS_CORE_KIND_XRAY:
            errors.append("Xray 26 已移除 allowInsecure（无法跳过证书校验），该节点建议改用 sing-box 核心")
        return errors

    @staticmethod
    def pclp_from_settings(
        pms_settings: "PmsSettingsManager",
        pms_node: PmsProxyNode,
        pms_core_kind: str,
        pms_clash_api_port: int = 0,
        pms_stats_api_port: int = 0,
    ) -> "PmsCoreLaunchProfile":
        """从设置 + 节点构造配置档（所有端口/规则都在这里汇总）。"""
        return PmsCoreLaunchProfile(
            node=pms_node,
            core_kind=pms_core_kind,
            routing_mode=pms_settings.psm_get_str("routing.mode", PMS_ROUTING_MODE_RULE),
            bypass_domains=pms_settings.psm_get_list("routing.bypass_domains"),
            direct_domains=pms_settings.psm_get_list("routing.direct_domains"),
            block_ad_domains=pms_settings.psm_get_list("routing.block_ad_domains"),
            block_ads_enabled=pms_settings.psm_get_bool("routing.block_ads_enabled", True),
            listen_address=pms_settings.psm_get_str("inbound.listen_address", "127.0.0.1"),
            mixed_enabled=pms_settings.psm_get_bool("inbound.mixed_enabled", True),
            mixed_port=pms_settings.psm_get_int("inbound.mixed_port", 30808),
            http_enabled=pms_settings.psm_get_bool("inbound.http_enabled", False),
            http_port=pms_settings.psm_get_int("inbound.http_port", 20809),
            socks_enabled=pms_settings.psm_get_bool("inbound.socks_enabled", False),
            socks_port=pms_settings.psm_get_int("inbound.socks_port", 20810),
            udp_enabled=pms_settings.psm_get_bool("inbound.udp_enabled", True),
            sniff_enabled=pms_settings.psm_get_bool("inbound.sniff_enabled", True),
            auth_enabled=pms_settings.psm_get_bool("inbound.auth_enabled", False),
            username=pms_settings.psm_get_str("inbound.username", ""),
            password=pms_settings.psm_get_str("inbound.password", ""),
            allow_lan=pms_settings.psm_get_bool("inbound.allow_lan", False),
            log_level=pms_settings.psm_get_str("core.log_level", "info"),
            access_log=pms_settings.psm_get_bool("core.access_log", False),
            mux_enabled=pms_settings.psm_get_bool("core.mux_enabled", False),
            mux_concurrency=pms_settings.psm_get_int("core.mux_concurrency", 8),
            tcp_fast_open=pms_settings.psm_get_bool("core.tcp_fast_open", True),
            sniff_override_dest=pms_settings.psm_get_bool("core.sniff_override_dest", True),
            tun_enabled=pms_settings.psm_get_bool("tun.enabled", False),
            tun_interface=pms_settings.psm_get_str("tun.interface_name", PMS_DEFAULT_TUN_INTERFACE),
            tun_stack=pms_settings.psm_get_str("tun.stack", "mixed"),
            tun_mtu=pms_settings.psm_get_int("tun.mtu", 9000),
            tun_auto_route=pms_settings.psm_get_bool("tun.auto_route", True),
            tun_strict_route=pms_settings.psm_get_bool("tun.strict_route", False),
            tun_ipv6=pms_settings.psm_get_bool("tun.ipv6", False),
            clash_api_port=int(pms_clash_api_port or 0),
            stats_api_port=int(pms_stats_api_port or 0),
            dns_remote=pms_settings.psm_get_str("routing.dns_remote", "https://1.1.1.1/dns-query"),
            dns_direct=pms_settings.psm_get_str("routing.dns_direct", "223.5.5.5"),
            domain_strategy=pms_settings.psm_get_str("routing.domain_strategy", "IPIfNonMatch"),
            use_remote_ruleset=pms_settings.psm_get_bool("routing.use_remote_ruleset", False),
            ruleset_source=pms_settings.psm_get_str("routing.ruleset_source", PMS_RULESET_SOURCE_LOCAL),
            ruleset_geosite_cn_url=pms_settings.psm_get_str("routing.ruleset_geosite_cn_url", PMS_DEFAULT_RULESET_GEOSITE_CN),
            bypass_private_ips=pms_settings.psm_get_bool("routing.bypass_private_ips", True),
            tor=ptb_settings_payload(pms_settings),
        )
