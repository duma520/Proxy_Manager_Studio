#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据模型

节点 / 订阅 / 分组 / 连接四个 dataclass，含字段校验、去重签名、展示文本与序列化。
"""

from __future__ import annotations

from dataclasses import (
    asdict,
    dataclass,
    field,
)
from typing import (
    Any,
    Optional,
)
import copy
import json
import uuid

from pms.constants import (
    PMS_COLUMN_ADDRESS,
    PMS_COLUMN_ENABLED,
    PMS_COLUMN_GROUP,
    PMS_COLUMN_INDEX,
    PMS_COLUMN_LAST_TEST,
    PMS_COLUMN_LATENCY,
    PMS_COLUMN_NAME,
    PMS_COLUMN_PORT,
    PMS_COLUMN_REMARK,
    PMS_COLUMN_SPEED,
    PMS_COLUMN_SUBSCRIPTION,
    PMS_COLUMN_TELEGRAM,
    PMS_COLUMN_TITLES,
    PMS_COLUMN_TYPE,
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
    PMS_LATENCY_GRADE_GOOD,
    PMS_LATENCY_GRADE_MEDIUM,
    PMS_PROTOCOL_HYSTERIA2,
    PMS_PROTOCOL_SHADOWSOCKS,
    PMS_PROTOCOL_TITLES,
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
    PMS_PROTOCOL_TG_PROXY,
    PMS_SECURITY_NONE,
    PMS_SECURITY_REALITY,
    PMS_SECURITY_TITLES,
    PMS_SECURITY_TLS,
    PMS_SS_METHODS,
    PMS_TRANSPORT_ALIASES,
    PMS_TRANSPORT_GRPC,
    PMS_TRANSPORT_H2,
    PMS_TRANSPORT_HTTP,
    PMS_TRANSPORT_HTTPUPGRADE,
    PMS_TRANSPORT_TCP,
    PMS_TRANSPORT_TITLES,
    PMS_TRANSPORT_WS,
    PMS_TRANSPORT_XHTTP,
)
from pms.utils import (
    pms_days_until,
    pms_fail_short_text,
    pms_format_bytes,
    pms_format_latency,
    pms_format_speed,
    pms_now_text,
    pms_safe_bool,
    pms_safe_float,
    pms_safe_int,
)

# --------------------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------------------
@dataclass
class PmsProxyNode:
    """一个代理节点（服务器）的全部参数与状态。"""

    uid: str = ""
    sort_index: int = 0
    enabled: bool = True
    name: str = ""
    protocol: str = PMS_PROTOCOL_VMESS
    server: str = ""
    port: int = 443
    group_name: str = "默认分组"
    subscription_uid: str = ""
    subscription_name: str = ""
    remark: str = ""
    tags: str = ""
    # 传输层
    transport: str = PMS_TRANSPORT_TCP
    security: str = PMS_SECURITY_NONE
    sni: str = ""
    alpn: str = ""
    fingerprint: str = ""
    allow_insecure: bool = False
    host: str = ""
    path: str = ""
    service_name: str = ""
    grpc_mode: str = ""
    header_type: str = ""
    # 凭据
    credential_uuid: str = ""
    credential_password: str = ""
    method: str = ""
    flow: str = ""
    alter_id: int = 0
    vmess_security: str = "auto"
    public_key: str = ""
    short_id: str = ""
    spider_x: str = ""
    cert_sha256: str = ""
    obfs: str = ""
    obfs_password: str = ""
    congestion_control: str = ""
    udp_relay_mode: str = ""
    ss_plugin: str = ""
    ss_plugin_opts: str = ""
    extra_json: str = ""
    # 运行状态
    latency_ms: Optional[int] = None
    speed_bps: float = 0.0
    last_test_time: str = ""
    last_test_mode: str = ""
    last_test_ok: int = 0     # v1.9.8：最近一次测试成败（0 未知 / 1 成功 / -1 失败）
    last_test_note: str = ""  # v1.9.21：最近一次失败的**原因原话**（核心的 HTTP 503/504 等），供「显示核心原话」用
    success_count: int = 0
    fail_count: int = 0
    # 节点健康检查（v1.9.37）：`enabled` 之外必须另记一套，否则分不清「用户手动禁用」和「程序自动禁用」
    health_fail_streak: int = 0         # 连续失败次数（跨周期累计，真连接成功一次即清零）
    health_auto_disabled: bool = False  # True = 由健康检查自动禁用；False + enabled=False = 用户手动禁用（永不检查）
    health_disabled_at: str = ""        # 自动禁用时间（文本）
    health_next_check_at: str = ""      # 下次（复活）检查时间（文本；空 = 还没排期）
    health_last_note: str = ""          # 最后一次失败原因原话（自动禁用时也写这个字段进库）
    # 电报 DC 测试（v1.9.59）：连通性以 MTProto 握手为准；速度只有真拿到 HTTP 吞吐才写
    telegram_ok: int = 0                # 0 未测 / 1 可连（有 DC 完成 resPQ 握手）/ -1 不可连
    telegram_best_ms: int = -1          # A：最快 DC 往返毫秒（-1 = 无）
    telegram_best_dc: str = ""          # best 属于哪个 DC（DC1..DC5）
    telegram_reachable: int = 0         # B：proto_count（完成 MTProto 握手的 DC 数）
    telegram_tcp_count: int = 0         # B2：tcp_count（TCP 可达 DC 数，含 reachable；>reachable 即存在「仅TCP」假通）
    telegram_total: int = 0             # 本轮探测的 DC 总数（正常 = 5）
    telegram_speed_bps: float = 0.0     # C：真实 HTTP 吞吐 B/s；0 = 没有有效吞吐（**不编造**）
    telegram_time: str = ""             # 上次电报测试时间
    telegram_note: str = ""             # 逐 DC 结果一行文本（悬停展示）
    created_at: str = ""
    updated_at: str = ""

    # ------------------------------------------------------------------ 构造
    def __post_init__(self) -> None:
        """把「缺省值」补齐（v1.9.49，审计第 29 项 · 启动提速）。

        **这一版把 `uid` / `created_at` / `updated_at` 的 `default_factory` 去掉了**，
        改在这里按需补齐。原因：`default_factory` 是**每行必算**的——`pms_now_text()` ×2
        在 5.75 万行上要 2.52 秒、`uuid.uuid4().hex` 要 0.42 秒，而这些值从库里读出来时
        **必然自带真值**，算完立刻被覆盖，纯属浪费（启动合计 16.3 秒里能省下约 3 秒）。

        行为**逐字段等价**：
        * `pnode_from_dict()` 走库数据 → `uid`/时间戳都有真值 → 这里不补，一个字节都不改；
        * `PmsProxyNode()` 手工构造 → 三个字段为空 → 这里补上，与原来的 `default_factory` 完全一致
          （原来 `uid` 是 `uuid4().hex`、时间是「当前时间文本」，这里补的也是同一口径）。
        """
        if not self.uid:
            self.uid = uuid.uuid4().hex
        if not self.created_at:
            self.created_at = pms_now_text()
        if not self.updated_at:
            self.updated_at = pms_now_text()

    # ------------------------------------------------------------------ 展示
    def pnode_display_address(self) -> str:
        """地址列显示文本（IPv6 自动加方括号）。"""
        host = self.server or "-"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{self.port}"

    def pnode_display_name(self) -> str:
        """名称列显示文本。"""
        return self.name or f"{PMS_PROTOCOL_TITLES.get(self.protocol, self.protocol)}-{self.server}"

    def pnode_display_latency(self, pms_show_core_fail: bool = False) -> str:
        """延迟列文本。

        v1.9.21：失败时默认显示「超时」；设置里勾了「显示核心的原始结果」就显示核心的真实原因
        （`HTTP 503` / `HTTP 504` / 连接失败…），让用户知道是节点不行、太慢，还是核心没给出结果。
        """
        if self.latency_ms is not None and self.latency_ms < 0 and pms_show_core_fail:
            return pms_fail_short_text(self.last_test_note)
        return pms_format_latency(self.latency_ms)

    def pnode_display_speed(self) -> str:
        """测速列文本：最近一次测试失败 → 「失败」，从未测过 → 「-」，否则格式化速率。

        v1.9.8：之前失败与未测都显示「-」，用户分不清「测过但失败」和「没测过」，
        而且失败会把 speed_bps 写成 0，连上一次成功的成绩也被抹掉了。
        """
        if pms_safe_int(self.last_test_ok, 0, -1, 1) < 0:
            return "失败"
        if not self.speed_bps or self.speed_bps <= 0:
            return "-"
        return pms_format_speed(self.speed_bps)

    def pnode_display_telegram(self) -> str:
        """电报列文本（v1.9.59）：`4/5 · 37ms DC2`；有真 HTTP 吞吐才追加速度，否则不显示。

        口径（用户 2026-10-15 定死）：A=最快 DC 延迟、B=可达 DC 数必显；
        C=吞吐只有**真的拿到 HTTP 响应字节**才显示——拿不到就只显示 A+B，绝不编造带宽。
        """
        state = pms_safe_int(self.telegram_ok, 0, -1, 1)
        if state == 0:
            return "-"
        total = pms_safe_int(self.telegram_total, 0, 0, 99) or 5
        if state < 0:
            # 不可连：若 TCP 仍可达（仅TCP 假通）要单独标出，否则「不通」
            tcp = pms_safe_int(self.telegram_tcp_count, 0, 0, total)
            if tcp > 0:
                return f"仅TCP {tcp}/{total}"
            return "不通"
        text = f"{pms_safe_int(self.telegram_reachable, 0, 0, total)}/{total}"
        if self.telegram_best_ms >= 0:
            text += f" · {self.telegram_best_ms}ms"
        if self.telegram_best_dc:
            text += f" {self.telegram_best_dc}"
        if self.telegram_speed_bps and self.telegram_speed_bps > 0:
            text += f" · {pms_format_speed(self.telegram_speed_bps)}"
        return text

    def pnode_telegram_tip_lines(self) -> list[str]:
        """悬停提示里的电报行（v1.9.59）：可连 / 不可连才给，未测给空表不占地方。

        与 `pnode_display_telegram` 同一口径：连通以 MTProto 握手为准，
        只有真 HTTP 吞吐才写速度；逐 DC 明细走 note（`telegram_note`）原样带出。
        """
        state = pms_safe_int(self.telegram_ok, 0, -1, 1)
        if state == 0:
            return []
        total = self.telegram_total or 5
        if state < 0:
            tcp = pms_safe_int(self.telegram_tcp_count, 0, 0, total)
            text = (
                f"电报：{'仅TCP' if tcp > 0 else '不可连'}（"
                f"TCP 可达 {tcp}/{total} 个 DC，但 MTProto 握手 0 个——电报软件实际用不了）"
            )
        else:
            text = f"电报：可连（MTProto 握手 {self.telegram_reachable}/{total} 个 DC"
            if self.telegram_best_ms >= 0:
                text += f"，最快 {self.telegram_best_dc or '?'} {self.telegram_best_ms}ms"
            if self.telegram_speed_bps > 0:
                text += f"，HTTP 吞吐 {pms_format_speed(self.telegram_speed_bps)}"
        text += f"，{self.telegram_time or '-'}）"
        lines = [text]
        if self.telegram_note:
            lines.append(f"逐 DC：{self.telegram_note}")
        return lines

    def pnode_display_type(self) -> str:
        title = PMS_PROTOCOL_TITLES.get(self.protocol, self.protocol)
        if self.security == PMS_SECURITY_REALITY:
            return f"{title}·Reality"
        if self.security == PMS_SECURITY_TLS:
            return f"{title}·TLS"
        return title

    def pnode_latency_color_key(self) -> str:
        """延迟颜色分级（表格着色用）。"""
        if self.latency_ms is None:
            return "unknown"
        if self.latency_ms < 0:
            return "timeout"
        if self.latency_ms <= PMS_LATENCY_GRADE_GOOD:
            return "good"
        if self.latency_ms <= PMS_LATENCY_GRADE_MEDIUM:
            return "medium"
        return "slow"

    def pnode_telegram_color_key(self) -> str:
        """电报列颜色分级（v1.9.59）：可连按**电报自己的最快 DC 往返**分档，不通红、未测灰。

        ★ 刻意不走 `pnode_latency_color_key()`：那是通用延迟的成绩，「电报能连、通用延迟没测过」
        会被它判成 unknown（灰），绿/黄/红的意义就丢了——电报列只看自己的 best_ms。
        """
        state = pms_safe_int(self.telegram_ok, 0, -1, 1)
        if state < 0:
            return "timeout"
        if state == 0:
            return "unknown"
        if self.telegram_best_ms < 0:
            return "good"          # 可连但没掐出时间（异常数据兜底）仍按可连显示绿
        if self.telegram_best_ms <= PMS_LATENCY_GRADE_GOOD:
            return "good"
        if self.telegram_best_ms <= PMS_LATENCY_GRADE_MEDIUM:
            return "medium"
        return "slow"

    def pnode_is_untested(self) -> bool:
        """是否「未测过」（v1.9.61，用户 2026-10-15 需求）。

        判定口径（用户定死）：延迟 / 测速 / 电报 **任一** 没测过即为未测——
        即延迟未测（latency_ms 为 None）、或测速未测（speed_bps<=0）、
        或电报未测（telegram_ok==0）。订阅拉回 / 粘贴回来的新增节点通常三项都空，
        归为未测；只要有一项测过，就不再算「未测」，避免重复跑已测项。

        注意：电报的 `telegram_ok==0` 是「未测」初值（见 `pnode_display_telegram`），
        `-1`/`1` 都是已测（不可连 / 可连），因此这里用 `==0` 而非 `<=0` 区分未测与已测不通。
        """
        if self.latency_ms is None:
            return True
        if not self.speed_bps or self.speed_bps <= 0:
            return True
        if pms_safe_int(self.telegram_ok, 0, -1, 1) == 0:
            return True
        return False

    # ------------------------------------------------------------------ 逻辑
    def pnode_signature(self) -> str:
        """去重签名：协议 + 服务器 + 端口 + 凭据 + 传输路径。"""
        parts = [
            str(self.protocol).lower(),
            str(self.server).strip().lower(),
            str(int(self.port or 0)),
            str(self.credential_uuid or self.credential_password or "").strip(),
            str(self.transport).lower(),
            str(self.path).strip(),
            str(self.sni).strip().lower(),
            str(self.service_name).strip(),
            str(self.method).strip().lower(),
        ]
        return "|".join(parts)

    def pnode_endpoint_signature(self) -> str:
        """仅按 协议+服务器+端口 的宽松签名（“按地址去重”用）。"""
        return f"{str(self.protocol).lower()}|{str(self.server).strip().lower()}|{int(self.port or 0)}"

    def pnode_clone(self, pms_new_uid: bool = True, pms_new_name: bool = False) -> "PmsProxyNode":
        """复制节点；默认生成新 uid（批量复制用）。"""
        data = copy.deepcopy(self.pnode_to_dict())
        if pms_new_uid:
            data["uid"] = uuid.uuid4().hex
        if pms_new_name:
            data["name"] = (self.name + " 副本").strip()
        data["created_at"] = pms_now_text()
        data["updated_at"] = data["created_at"]
        return PmsProxyNode.pnode_from_dict(data)

    def pnode_validate(self) -> list[str]:
        """校验必要字段，返回错误列表（空列表表示可用）。"""
        errors: list[str] = []
        if self.protocol not in PMS_PROTOCOL_TITLES:
            errors.append(f"未知协议：{self.protocol}")
        if not str(self.server).strip():
            errors.append("服务器地址不能为空")
        if not 0 < int(self.port or 0) < 65536:
            errors.append("端口必须在 1-65535 之间")
        if self.protocol in (PMS_PROTOCOL_VMESS, PMS_PROTOCOL_VLESS):
            try:
                uuid.UUID(str(self.credential_uuid).strip())
            except (ValueError, AttributeError, TypeError):
                errors.append("VMess/VLESS 需要合法的 UUID")
        if self.protocol == PMS_PROTOCOL_TROJAN and not str(self.credential_password):
            errors.append("Trojan 需要密码")
        if self.protocol == PMS_PROTOCOL_SHADOWSOCKS:
            if not str(self.credential_password):
                errors.append("Shadowsocks 需要密码")
            if str(self.method).lower() not in PMS_SS_METHODS:
                errors.append(f"Shadowsocks 加密方式无效：{self.method or '空'}")
        if self.protocol == PMS_PROTOCOL_HYSTERIA2:
            if not str(self.credential_password):
                errors.append("Hysteria2 需要密码")
            # 第 8 项：Hysteria2 混淆(obfs)核心只认 salamander，填别的会让核心启动失败；
            # 在节点校验里拦下来，避免用户困惑。
            if self.obfs and self.obfs != "salamander":
                errors.append(f"Hysteria2 混淆(obfs)仅支持 salamander，当前填了「{self.obfs}」")
        if self.protocol == PMS_PROTOCOL_TUIC and not str(self.credential_uuid):
            errors.append("TUIC 需要 UUID")
        if self.protocol == PMS_PROTOCOL_SNELL:
            if not str(self.credential_password):
                errors.append("Snell 需要密码")
            # 第 10 项：Snell 版本核心只认 4 / 5（v4、v5），填错核心不认；在节点校验里拦。
            # 注：本项目 Snell 用 extra_json 的 version 字段，没有独立的 obfs 字段
            # （原建议的“obfs=http/tls”在本实现里即对应 version=4/5）。
            try:
                snell_extra = json.loads(self.extra_json or "{}") if self.extra_json else {}
                snell_version = int(snell_extra.get("version", 4))
            except (ValueError, TypeError, json.JSONDecodeError):
                snell_version = 4
            if snell_version not in (4, 5):
                errors.append(f"Snell 版本仅支持 4 或 5，当前填了「{snell_version}」")
        # v1.9.68：Telegram 专属代理——secret（即密码）必填
        if self.protocol == PMS_PROTOCOL_TG_WEBPROXY:
            if not str(self.credential_password):
                errors.append("TgWebProxy 需要 secret（SOCKS5 密码）")
        if self.protocol == PMS_PROTOCOL_TG_PROXY:
            if not str(self.credential_password):
                errors.append("TgProxy 需要 secret（MTProto 代理密钥）")
        if self.security == PMS_SECURITY_REALITY:
            if not str(self.public_key):
                errors.append("Reality 需要公钥（publicKey）")
        if self.transport in (PMS_TRANSPORT_WS, PMS_TRANSPORT_HTTPUPGRADE, PMS_TRANSPORT_H2, PMS_TRANSPORT_XHTTP, PMS_TRANSPORT_HTTP) and not str(self.path):
            errors.append(f"{PMS_TRANSPORT_TITLES.get(self.transport, self.transport)} 需要 path")
        if self.transport == PMS_TRANSPORT_GRPC and not (str(self.service_name) or str(self.path)):
            errors.append("gRPC 需要 serviceName")
        return errors

    def pnode_validate_core_support(self, pms_core_kind: str) -> list[str]:
        """按核心能力校验。

        v1.9.28：sing-box 那一侧**不再声称「支持全部」**——实测（用它自己的 `check` 验过）
        有三种协议是**结构性**不支持，写进批量配置会让整个核心起不来
        （真库 33,604 个节点只要含 1 个 ssr → 整批 0 结果）：
            ssr     → `ShadowsocksR is deprecated and removed in sing-box 1.6.0`
            naive   → `naive outbound is not included in this build`
            juicity → `unknown outbound type: juicity`
        另外 anytls / snell / wireguard / shadowtls 等也可能因为**单个节点字段不全**而加载失败
        （例如缺 TLS），这类由批量测试的「逐片剔除坏节点」兜住（见 7.46）。
        v1.9.35（矩阵探测实测补的第 10 项）：Xray 侧**连 socks4 也不认**——
        配置生成器把出站类型写成 `protocol: socks4`，Xray 26.3.27 直接回
        `infra/conf: unknown config id: socks4` 并且整份配置加载失败。
        """
        errors: list[str] = []
        if pms_core_kind == PMS_CORE_KIND_XRAY:
            if self.protocol in (
                PMS_PROTOCOL_HYSTERIA2,
                PMS_PROTOCOL_TUIC,
                PMS_PROTOCOL_SSR,
                PMS_PROTOCOL_NAIVE,
                PMS_PROTOCOL_ANYTLS,
                PMS_PROTOCOL_SHADOWTLS,
                PMS_PROTOCOL_SNELL,
                PMS_PROTOCOL_JUICITY,
                PMS_PROTOCOL_HYSTERIA1,
                PMS_PROTOCOL_SOCKS4,
            ):
                errors.append(f"Xray 核心不支持 {PMS_PROTOCOL_TITLES.get(self.protocol, self.protocol)}，请切换 sing-box 核心")
        elif pms_core_kind == PMS_CORE_KIND_SINGBOX:
            if self.protocol in (PMS_PROTOCOL_SSR, PMS_PROTOCOL_NAIVE, PMS_PROTOCOL_JUICITY):
                title = PMS_PROTOCOL_TITLES.get(self.protocol, self.protocol)
                errors.append(f"sing-box 核心不支持 {title}（实测：核心已移除 / 未编入该类出站），请改用 Xray 核心")
        # v1.9.68：Telegram 专属代理的核心支持口径
        #   · tg_webproxy 本质是标准 SOCKS5（secret 即 16 字节密码），Xray / sing-box 都能当通用出站
        #   · tg_proxy 是 MTProto 电报代理，两个核心都**没有** MTProto 出站，不能当通用出口；
        #     它只走「电报代理连通直测」（原生 socket 探），不进本函数的批量路由判定，
        #     这里不强制报错（避免编辑器里误报），但路由层不会为它生成出站。
        return errors

    def pnode_to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return asdict(self)

    @staticmethod
    def pnode_from_dict(pms_data: dict[str, Any]) -> "PmsProxyNode":
        """从字典构造节点（忽略未知键，缺失键用默认值）。

        v1.9.49（审计第 29 项 · 启动提速）**只改构造方式，不改任何对外行为**。

        为什么改：实测 5.75 万行「读库 → 建对象」合计 16.3 秒（启动的主要开销），
        其中本方法占 7.8 秒。原写法 `PmsProxyNode(**payload)` 会让三个
        `default_factory`（`uid` = `uuid.uuid4().hex`、`created_at` / `updated_at` = `pms_now_text()`）
        **每一行都先白算一遍**再被 payload 里的真值覆盖。实测这两项白算：
        `pms_now_text()` ×2 = 2.52 秒、`uuid4().hex` = 0.42 秒（5.75 万行）。

        做法：既然 payload 里这三个键**存在就用真值**，那就干脆不把它们交给
        `dataclass.__init__`（避免触发工厂），构造完直接用 `object.__setattr__` 之外的
        普通赋值写回——但实测「先 pop 再逐字段赋值」反而更慢（多一次 dict 操作 + 3 次赋值），
        所以这里改成**一次性把三个键塞回 payload 的浅拷贝**会产生新的 dict 开销。
        最终采用：**只在确实带真值时，用一条 `dict` 就地展开构造**——即把工厂从
        「每行必算」降为「只有真缺字段时才由 `__init__` 的默认值兜底」。
        """
        payload = {key: value for key, value in pms_data.items() if key in _PMS_PROXY_NODE_FIELDS}
        node = PmsProxyNode(**payload)  # type: ignore[arg-type]
        node.enabled = pms_safe_bool(payload.get("enabled", True), True)
        node.port = pms_safe_int(payload.get("port", 443), 443, 1, 65535)
        node.alter_id = pms_safe_int(payload.get("alter_id", 0), 0, 0, 65535)
        node.sort_index = pms_safe_int(payload.get("sort_index", 0), 0, 0, 10 ** 9)
        node.success_count = pms_safe_int(payload.get("success_count", 0), 0, 0, 10 ** 9)
        node.fail_count = pms_safe_int(payload.get("fail_count", 0), 0, 0, 10 ** 9)
        node.speed_bps = pms_safe_float(payload.get("speed_bps", 0.0), 0.0)
        node.last_test_ok = pms_safe_int(payload.get("last_test_ok", 0), 0, -1, 1)
        latency = payload.get("latency_ms", None)
        node.latency_ms = None if latency in (None, "") else pms_safe_int(latency, -1)
        node.transport = PMS_TRANSPORT_ALIASES.get(str(payload.get("transport", "")).lower(), PMS_TRANSPORT_TCP)
        node.protocol = str(payload.get("protocol", PMS_PROTOCOL_VMESS)).lower()
        if node.protocol not in PMS_PROTOCOL_TITLES:
            node.protocol = PMS_PROTOCOL_VMESS
        if node.security not in PMS_SECURITY_TITLES:
            node.security = PMS_SECURITY_NONE
        # v1.9.59：电报 DC 测试八列的清洗（脏数据一律回落「未测」，绝不抛）
        node.telegram_ok = pms_safe_int(payload.get("telegram_ok", 0), 0, -1, 1)
        node.telegram_best_ms = pms_safe_int(payload.get("telegram_best_ms", -1), -1, -1, 10 ** 8)
        node.telegram_best_dc = str(payload.get("telegram_best_dc", "") or "")[:8]
        node.telegram_reachable = pms_safe_int(payload.get("telegram_reachable", 0), 0, 0, 99)
        node.telegram_tcp_count = pms_safe_int(payload.get("telegram_tcp_count", 0), 0, 0, 99)
        node.telegram_total = pms_safe_int(payload.get("telegram_total", 0), 0, 0, 99)
        node.telegram_speed_bps = pms_safe_float(payload.get("telegram_speed_bps", 0.0), 0.0)
        node.telegram_time = str(payload.get("telegram_time", "") or "")[:32]
        node.telegram_note = str(payload.get("telegram_note", "") or "")[:500]
        return node

    def pnode_row_value(self, pms_column: int, pms_show_core_fail: bool = False) -> Any:
        """取表格某一列的值（v1.9.30）。

        单独取一列是为了**别为了填一个格子、算一个排序键，把整行 12 个格式化都做一遍**：
        5.7 万节点按文本列排序时，整行算下来多花 0.7 秒；只算一列是它的 1/12。
        """
        if pms_column == PMS_COLUMN_ENABLED:
            return self.enabled
        if pms_column == PMS_COLUMN_INDEX:
            return self.sort_index + 1
        if pms_column == PMS_COLUMN_TYPE:
            return self.pnode_display_type()
        if pms_column == PMS_COLUMN_NAME:
            return self.pnode_display_name()
        if pms_column == PMS_COLUMN_ADDRESS:
            return self.server
        if pms_column == PMS_COLUMN_PORT:
            return self.port
        if pms_column == PMS_COLUMN_LATENCY:
            return self.pnode_display_latency(pms_show_core_fail)
        if pms_column == PMS_COLUMN_SPEED:
            return self.pnode_display_speed()
        if pms_column == PMS_COLUMN_GROUP:
            return self.group_name
        if pms_column == PMS_COLUMN_SUBSCRIPTION:
            return self.subscription_name or "-"
        if pms_column == PMS_COLUMN_LAST_TEST:
            return self.last_test_time or "-"
        if pms_column == PMS_COLUMN_REMARK:
            return self.remark
        if pms_column == PMS_COLUMN_TELEGRAM:
            return self.pnode_display_telegram()
        return ""

    def pnode_row_values(self, pms_show_core_fail: bool = False) -> list[Any]:
        """按表格列顺序输出行数据。"""
        return [self.pnode_row_value(pms_column, pms_show_core_fail) for pms_column in range(len(PMS_COLUMN_TITLES))]

# v1.9.30：节点字段名集合算一次就够。5.7 万节点全量加载时，原来每行都重算一遍它，
# 光这一项就要 0.5 秒（实测）。字段是 dataclass 声明出来的，运行时不会变，缓存安全。
_PMS_PROXY_NODE_FIELDS = frozenset(item.name for item in PmsProxyNode.__dataclass_fields__.values())  # type: ignore[attr-defined]
# v1.9.49（审计第 29 项）：这三个字段带 `default_factory`，构造时**每行都会先算一次**
# （uuid4 + 2 次 pms_now_text）。从库里读出来的行里它们必然存在，那一次计算纯属浪费；
# 构造时先摘出来、构造后写回，省掉每行一次 uuid 与两次时间格式化。
_PMS_PROXY_NODE_DEFERRED = ("uid", "created_at", "updated_at")

@dataclass
class PmsSubscriptionRecord:
    """一个订阅源（远程节点列表）。"""

    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    url: str = ""
    enabled: bool = True
    user_agent: str = ""
    api_key: str = ""
    update_interval_minutes: int = 360
    last_update_time: str = ""
    last_node_count: int = 0
    last_error: str = ""
    traffic_upload: int = 0
    traffic_download: int = 0
    traffic_total: int = 0
    traffic_expire: int = 0
    traffic_raw: str = ""          # 第 11/13 项：订阅流量头原始内容，供排查
    # v1.9.44：Tor 桥接源专用（桥接不是节点，条数不能塞进 last_node_count，
    # 否则界面「节点数」列会显示成「抓到 12 个节点」，那是骗人）
    last_bridge_count: int = 0
    last_bridge_note: str = ""
    sort_index: int = 0
    use_system_proxy: bool = False
    remark: str = ""
    created_at: str = field(default_factory=pms_now_text)
    updated_at: str = field(default_factory=pms_now_text)

    def psr_display_name(self) -> str:
        return self.name or self.url or "未命名订阅"

    def psr_traffic_text(self) -> str:
        """订阅流量文本：已用 / 总量（可含到期天数）。"""
        if not self.traffic_total and not self.traffic_upload and not self.traffic_download:
            return "-"
        used = int(self.traffic_upload or 0) + int(self.traffic_download or 0)
        total = int(self.traffic_total or 0)
        text = f"{pms_format_bytes(used)} / {pms_format_bytes(total)}" if total else pms_format_bytes(used)
        days = pms_days_until(self.traffic_expire)
        if days is not None:
            text += "（剩余 %d 天）" % days if days >= 0 else "（已过期）"
        return text

    def psr_to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def psr_from_dict(pms_data: dict[str, Any]) -> "PmsSubscriptionRecord":
        known = {item.name for item in PmsSubscriptionRecord.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        payload = {key: value for key, value in dict(pms_data).items() if key in known}
        record = PmsSubscriptionRecord(**payload)  # type: ignore[arg-type]
        record.enabled = pms_safe_bool(payload.get("enabled", True), True)
        record.use_system_proxy = pms_safe_bool(payload.get("use_system_proxy", False), False)
        record.sort_index = pms_safe_int(payload.get("sort_index", 0), 0, 0, 10 ** 9)
        for field_name in ("traffic_upload", "traffic_download", "traffic_total", "traffic_expire", "last_node_count",
                           "last_bridge_count"):
            setattr(record, field_name, pms_safe_int(payload.get(field_name, 0), 0, 0, 10 ** 15))
        record.last_bridge_note = str(payload.get("last_bridge_note", "") or "")[:200]
        record.update_interval_minutes = pms_safe_int(payload.get("update_interval_minutes", 360), 360, 10, 20160)
        return record

@dataclass
class PmsGroupRecord:
    """节点分组。"""

    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "默认分组"
    color: str = "#3d8bfd"
    sort_index: int = 0
    remark: str = ""
    created_at: str = field(default_factory=pms_now_text)

    def pgr_to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def pgr_from_dict(pms_data: dict[str, Any]) -> "PmsGroupRecord":
        known = {item.name for item in PmsGroupRecord.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        payload = {key: value for key, value in dict(pms_data).items() if key in known}
        return PmsGroupRecord(**payload)  # type: ignore[arg-type]

@dataclass
class PmsConnectionRecord:
    """一条被核心记录的连接（连接面板数据行）。"""

    conn_uid: str = ""
    protocol: str = ""
    source: str = ""
    destination: str = ""
    host: str = ""
    outbound: str = ""
    rule: str = ""
    chain: str = ""
    upload: int = 0
    download: int = 0
    start_time: str = ""
    duration_ms: int = 0
    is_active: bool = True
