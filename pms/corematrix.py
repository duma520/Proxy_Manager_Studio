# -*- coding: utf-8 -*-
"""协议能力矩阵自动探测（v1.9.35）。

要解决的问题：`PmsProxyNode.pnode_validate_core_support()` 里那张「哪个核心支持哪些协议」的表
是**人手写死**的——核心升级、上游增删出站类型、官方构建改了编译选项，表就会悄悄过期，
而它一过期就会出现两种真事故（v1.9.28 就吃过一次「整批 0 结果」）：

· 表说「支持」但核心其实不认 → 批量配置整批起不来；
· 表说「不支持」但核心其实能用 → 一批节点永远测不到。

本模块的做法：**拿真实的节点当样本，去问核心自己**——用既有配置生成器造一份只含该协议的
最小配置，跑核心自带的自检（`xray run -test` / `sing-box check`），按核心的**原话**分类：

    supported     核心说没问题（自检通过）
    unsupported   核心明说「不认这个出站类型 / 已移除 / 本构建未编入」
    bad_sample    自检没过，但不是「不认协议」——多半是样本字段不全（原话记在 `detail`）
    missing_core  本机没装这个核心
    no_sample     连内置样本都没有这个协议（理论上不该出现）

★ 本模块**只探测与展示**，绝不参与运行时选核心：`pcl_core_supports()` 那条路一行都不动
（用户拍板：探测结果只用来核对内置表，不自动改行为）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from pms.constants import (
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
    PMS_LOGGER_NAME,
    PMS_PROTOCOL_ANYTLS,
    PMS_PROTOCOL_HYSTERIA1,
    PMS_PROTOCOL_HYSTERIA2,
    PMS_PROTOCOL_HTTP,
    PMS_PROTOCOL_JUICITY,
    PMS_PROTOCOL_NAIVE,
    PMS_PROTOCOL_ORDER,
    PMS_PROTOCOL_SHADOWSOCKS,
    PMS_PROTOCOL_SHADOWTLS,
    PMS_PROTOCOL_SNELL,
    PMS_PROTOCOL_SOCKS,
    PMS_PROTOCOL_SOCKS4,
    PMS_PROTOCOL_SSR,
    PMS_PROTOCOL_TITLES,
    PMS_PROTOCOL_TROJAN,
    PMS_PROTOCOL_TUIC,
    PMS_PROTOCOL_VLESS,
    PMS_PROTOCOL_VMESS,
    PMS_PROTOCOL_WIREGUARD,
    PMS_SECURITY_NONE,
    PMS_SECURITY_TLS,
)
from pms.coreconfig import PmsCoreConfigWriter, PmsSingBoxConfigBuilder, PmsXrayConfigBuilder
from pms.coreprofile import PmsCoreLaunchProfile
from pms.models import PmsProxyNode

if True:  # 仅类型检查用，避免与设置 / 核心定位模块循环导入
    from pms.coresys import PmsCoreLocator
    from pms.paths import PmsRuntimePaths
    from pms.settings import PmsSettingsManager

# 状态与文案
PMS_MATRIX_SUPPORTED = "supported"
PMS_MATRIX_UNSUPPORTED = "unsupported"
PMS_MATRIX_BAD_SAMPLE = "bad_sample"
PMS_MATRIX_MISSING_CORE = "missing_core"
PMS_MATRIX_NO_SAMPLE = "no_sample"
PMS_MATRIX_STATE_TITLES: dict[str, str] = {
    PMS_MATRIX_SUPPORTED: "支持",
    PMS_MATRIX_UNSUPPORTED: "不支持",
    PMS_MATRIX_BAD_SAMPLE: "配置没过",
    PMS_MATRIX_MISSING_CORE: "核心未安装",
    PMS_MATRIX_NO_SAMPLE: "无样本",
}

# 核心「明说不认」的说法（都是实测抓到的原话片段，大小写不敏感）
PMS_MATRIX_UNSUPPORTED_HINTS: tuple[str, ...] = (
    "unknown outbound",              # sing-box：unknown outbound type: juicity
    "unknown config id",             # xray：unknown config id: hysteria2（socks4 也是这句）
    "unknown protocol",
    "not included in this build",    # sing-box：naive outbound is not included in this build
    "deprecated and removed",        # sing-box：ShadowsocksR is deprecated and removed in sing-box 1.6.0
    "unsupported outbound",
    "unsupported protocol",
    "is not supported",
)

# 输出里的噪音行（版本横幅 / 读配置的 Info 行）：不算「核心的看法」，拼说明时要丢掉
PMS_MATRIX_NOISE_HINTS: tuple[str, ...] = (
    "a unified platform for anti-censorship",
    "penetrates everything",
    "reading config:",
    "[info]",
)

PMS_MATRIX_CACHE_NAME = "core_matrix.json"      # data/core_matrix.json
PMS_MATRIX_CONFIG_SUBDIR = "configs"
PMS_MATRIX_SAMPLE_SERVER = "203.0.113.9"        # TEST-NET-3：只进配置，不做真实连接
PMS_MATRIX_DETAIL_LIMIT = 240


def pms_core_matrix_kinds() -> tuple[str, ...]:
    """要探测的核心（只有 Xray / sing-box 两个主核心参与「按节点选核心」）。"""
    return (PMS_CORE_KIND_XRAY, PMS_CORE_KIND_SINGBOX)


def pms_core_matrix_protocols() -> tuple[str, ...]:
    return tuple(PMS_PROTOCOL_ORDER)


def pms_core_matrix_sample_node(pms_protocol: str) -> Optional[PmsProxyNode]:
    """内置样本节点：**字段按各协议最小可加载集合**给足（凭据是假的，只为了让核心自检）。

    这些样本只用于「库里没有该协议节点」时兜底，界面上会写明样本来源是「内置样本」。
    """
    protocol = str(pms_protocol or "").strip().lower()
    if protocol not in PMS_PROTOCOL_TITLES:
        return None
    node = PmsProxyNode(
        uid=f"matrix-{protocol}", name=f"matrix-{protocol}", protocol=protocol,
        server=PMS_MATRIX_SAMPLE_SERVER, port=443, group_name="矩阵样本",
    )
    node.credential_uuid = "11111111-2222-3333-4444-555555555555"
    node.credential_password = "matrix-password"
    node.method = "aes-128-gcm"
    node.host = "matrix-user"
    node.sni = "example.com"
    node.flow = ""
    node.vmess_security = "auto"
    if protocol in (PMS_PROTOCOL_HYSTERIA2, PMS_PROTOCOL_TUIC, PMS_PROTOCOL_ANYTLS,
                    PMS_PROTOCOL_JUICITY, PMS_PROTOCOL_HYSTERIA1, PMS_PROTOCOL_NAIVE):
        node.security = PMS_SECURITY_TLS
    else:
        node.security = PMS_SECURITY_NONE
    if protocol == PMS_PROTOCOL_WIREGUARD:
        node.extra_json = json.dumps({
            "local_address": "***已脱敏***-内网IP/32",
            "private_key": "a" * 43 + "=",
            "peer_public_key": "b" * 43 + "=",
            "mtu": 1420,
        })
    if protocol == PMS_PROTOCOL_SNELL:
        node.extra_json = json.dumps({"version": 4})
    if protocol == PMS_PROTOCOL_SHADOWTLS:
        # 实测：sing-box 的 shadowtls 出站**必须**带 TLS（`initialize outbound[0]: TLS required`）
        node.security = PMS_SECURITY_TLS
        node.extra_json = json.dumps({"version": 3})
    if protocol == PMS_PROTOCOL_HYSTERIA1:
        node.extra_json = json.dumps({"auth_str": "matrix-auth", "up_mbps": 100, "down_mbps": 100})
    return node


def pms_core_matrix_sample_nodes() -> dict[str, PmsProxyNode]:
    """全部协议的内置样本（能造出来的那些）。"""
    samples: dict[str, PmsProxyNode] = {}
    for protocol in pms_core_matrix_protocols():
        node = pms_core_matrix_sample_node(protocol)
        if node is not None:
            samples[protocol] = node
    return samples


def pms_core_matrix_pick_samples(pms_nodes: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """从一堆真实节点里**每种协议挑一个**（优先已启用、有凭据的），库里缺的用内置样本补。

    返回 `{协议: {"node": 节点, "source": "库里节点" / "内置样本"}}`。
    """
    picked: dict[str, dict[str, Any]] = {}
    for node in pms_nodes:
        protocol = str(getattr(node, "protocol", "") or "")
        if protocol not in PMS_PROTOCOL_TITLES or protocol in picked:
            continue
        if not str(getattr(node, "server", "") or "").strip():
            continue
        if node.uid in (None, "") or str(node.uid).startswith("matrix-"):
            continue                      # 别把上一轮的内置样本又当成真实节点
        current = picked.get(protocol)
        if current is None or (bool(getattr(node, "enabled", False)) and not bool(current["node"].enabled)):
            picked[protocol] = {"node": node, "source": "库里节点"}
    for protocol, node in pms_core_matrix_sample_nodes().items():
        picked.setdefault(protocol, {"node": node, "source": "内置样本"})
    return picked


def pms_core_matrix_builtin_state(pms_kind: str, pms_protocol: str) -> str:
    """内置表（`pnode_validate_core_support`）对「核心 × 协议」怎么说——用于对照列。"""
    node = pms_core_matrix_sample_node(pms_protocol)
    if node is None:
        return PMS_MATRIX_NO_SAMPLE
    return PMS_MATRIX_SUPPORTED if not node.pnode_validate_core_support(pms_kind) else PMS_MATRIX_UNSUPPORTED


def pms_core_matrix_detail(pms_output: str, pms_limit: int = PMS_MATRIX_DETAIL_LIMIT) -> str:
    """从核心输出里挑出「它到底在抱怨什么」：丢掉版本横幅与 Info 行，取最后几条有内容的。

    ★ 直接截前 N 个字符是**错**的：xray 会把版本横幅先打出来（实测），
    结果「说明」列全是那两行版权信息，真正的原因被截掉了。
    """
    kept: list[str] = []
    for raw in str(pms_output or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if any(hint in line.lower() for hint in PMS_MATRIX_NOISE_HINTS):
            continue
        kept.append(line)
    joined = " ｜ ".join(kept[-2:])
    return joined[:int(pms_limit)]


def pms_core_matrix_classify(pms_ok: bool, pms_output: str) -> tuple[str, str]:
    """按核心自检的结果与原话分类：返回 `(状态, 说明)`。"""
    text = " ".join(str(pms_output or "").split())
    detail = pms_core_matrix_detail(pms_output)
    if pms_ok:
        return PMS_MATRIX_SUPPORTED, detail
    lowered = text.lower()
    if any(hint in lowered for hint in PMS_MATRIX_UNSUPPORTED_HINTS):
        return PMS_MATRIX_UNSUPPORTED, detail
    return PMS_MATRIX_BAD_SAMPLE, detail


def pms_core_matrix_config_file(pms_paths: "PmsRuntimePaths", pms_kind: str, pms_protocol: str) -> Path:
    """探测用配置的落盘位置（`data/configs` 下，和核心临时配置同一处，事后由清理规则收走）。"""
    return Path(pms_paths.prt_data_dir) / PMS_MATRIX_CONFIG_SUBDIR / f"matrix_{pms_kind}_{pms_protocol}.json"


def pms_core_matrix_probe_profile(
    pms_paths: "PmsRuntimePaths",
    pms_settings: "PmsSettingsManager",
    pms_kind: str,
    pms_node: Any,
) -> Optional[Path]:
    """按「一个协议一个节点」造最小配置并落盘；失败返回 None（原因写日志）。

    特意把入口压到最小：只留混合入口、不写日志文件、不开 mux/clash-api——
    这样才能保证「自检没过」的原因**只可能来自协议本身**，而不是别的设置。
    """
    logger = logging.getLogger(PMS_LOGGER_NAME + ".corematrix")
    try:
        profile = PmsCoreLaunchProfile.pclp_from_settings(pms_settings, pms_node, pms_kind)
        profile.mixed_enabled = True
        profile.http_enabled = False
        profile.socks_enabled = False
        profile.tun_enabled = False
        profile.allow_lan = False
        profile.auth_enabled = False
        profile.mux_enabled = False
        profile.access_log = False
        profile.clash_api_port = 0
        profile.stats_api_port = 0
        profile.udp_enabled = True
        # v1.9.43：能力矩阵探的是「节点协议本身能不能被核心跑起来」，
        # 必须**摘掉** Tor 出口——否则矩阵会给所有协议生成一份 route.final=tor-bridged 的配置，
        # 探测结果整体失真（也不能为此报「不支持」，那同样失真）。
        profile.tor = {}
        if pms_kind == PMS_CORE_KIND_XRAY:
            config = PmsXrayConfigBuilder().pxc_build(profile)
        else:
            config = PmsSingBoxConfigBuilder(pms_paths).psc_build(profile)
        target = pms_core_matrix_config_file(pms_paths, pms_kind, str(pms_node.protocol))
        if not PmsCoreConfigWriter().pcw_write(config, target):
            logger.warning("矩阵探测：配置写不下去（%s）", target.name)
            return None
        return target
    except Exception as exc:  # noqa: BLE001 - 生成失败算这一格失败，不能连累整轮探测
        logger.warning("矩阵探测：生成 %s 的 %s 配置失败：%s", pms_kind, pms_node.protocol, exc)
        return None


def pms_core_matrix_probe(
    pms_paths: "PmsRuntimePaths",
    pms_settings: "PmsSettingsManager",
    pms_locator: "PmsCoreLocator",
    pms_samples: Optional[dict[str, dict[str, Any]]] = None,
    pms_kinds: Optional[Sequence[str]] = None,
    pms_protocols: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """跑一整轮探测，返回可 JSON 落盘的载荷。

    每格流程：造配置 → 跑核心自检 → 按原话分类。核心没装就整列 `missing_core`（不报错）。
    """
    logger = logging.getLogger(PMS_LOGGER_NAME + ".corematrix")
    kinds = tuple(pms_kinds or pms_core_matrix_kinds())
    protocols = tuple(pms_protocols or pms_core_matrix_protocols())
    samples = pms_samples if pms_samples is not None else pms_core_matrix_sample_nodes()
    available = pms_locator.pcl_find_any(pms_settings)
    payload: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kinds": list(kinds),
        "protocols": list(protocols),
        "cores": {},
        "cells": {},
    }
    for kind in kinds:
        core_path = available.get(kind)
        payload["cores"][kind] = {
            "path": str(core_path) if core_path else "",
            "version": pms_locator.pcl_detect_version(core_path) if core_path else "",
        }
    for kind in kinds:
        core_path = available.get(kind)
        for protocol in protocols:
            entry = samples.get(protocol)
            node = entry["node"] if isinstance(entry, dict) else entry
            source = str(entry.get("source", "内置样本")) if isinstance(entry, dict) else "内置样本"
            cell: dict[str, Any] = {
                "protocol": protocol,
                "kind": kind,
                "builtin": pms_core_matrix_builtin_state(kind, protocol),
                "source": source,
                "state": PMS_MATRIX_NO_SAMPLE,
                "detail": "",
            }
            if node is None:
                payload["cells"][f"{kind}:{protocol}"] = cell
                continue
            if not core_path:
                cell["state"] = PMS_MATRIX_MISSING_CORE
                payload["cells"][f"{kind}:{protocol}"] = cell
                continue
            target = pms_core_matrix_probe_profile(pms_paths, pms_settings, kind, node)
            if target is None:
                cell["state"] = PMS_MATRIX_BAD_SAMPLE
                cell["detail"] = "程序生成的配置没能落盘"
                payload["cells"][f"{kind}:{protocol}"] = cell
                continue
            ok, output = pms_locator.pcl_validate_config(core_path, target)
            cell["state"], cell["detail"] = pms_core_matrix_classify(ok, output)
            if cell["state"] == PMS_MATRIX_SUPPORTED:
                cell["detail"] = ""      # 过了就别把核心的 debug 日志塞进「说明」列
            cell["sample_name"] = str(getattr(node, "name", "") or "")
            payload["cells"][f"{kind}:{protocol}"] = cell
            logger.debug("矩阵探测：%s × %s → %s", kind, protocol, cell["state"])
    return payload


def pms_core_matrix_cache_file(pms_paths: "PmsRuntimePaths") -> Path:
    return Path(pms_paths.prt_data_dir) / PMS_MATRIX_CACHE_NAME


def pms_core_matrix_cache_save(pms_paths: "PmsRuntimePaths", pms_payload: dict[str, Any]) -> bool:
    """把探测结果写进 `data/core_matrix.json`（失败只记日志，不影响界面）。"""
    logger = logging.getLogger(PMS_LOGGER_NAME + ".corematrix")
    target = pms_core_matrix_cache_file(pms_paths)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(pms_payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("矩阵结果写入失败（%s）：%s", target, exc)
        return False


def pms_core_matrix_cache_load(pms_paths: "PmsRuntimePaths") -> dict[str, Any]:
    """读缓存；没有 / 坏了都返回空载荷（界面显示「还没探测过」）。"""
    target = pms_core_matrix_cache_file(pms_paths)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def pms_core_matrix_rows(pms_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把载荷摊成「一行一个协议」的表：内置表 Xray / 内置表 sing-box / 实测 Xray / 实测 sing-box / 说明。"""
    cells = pms_payload.get("cells") if isinstance(pms_payload.get("cells"), dict) else {}
    kinds = tuple(pms_payload.get("kinds") or pms_core_matrix_kinds())
    protocols = tuple(pms_payload.get("protocols") or pms_core_matrix_protocols())
    rows: list[dict[str, Any]] = []
    for protocol in protocols:
        row: dict[str, Any] = {"protocol": protocol, "title": PMS_PROTOCOL_TITLES.get(protocol, protocol)}
        notes: list[str] = []
        for kind, prefix in zip(kinds, ("xray", "singbox")):
            cell = cells.get(f"{kind}:{protocol}") if isinstance(cells, dict) else None
            cell = cell if isinstance(cell, dict) else {}
            row[f"builtin_{prefix}"] = str(cell.get("builtin") or PMS_MATRIX_NO_SAMPLE)
            row[f"probed_{prefix}"] = str(cell.get("state") or PMS_MATRIX_NO_SAMPLE)
            row[f"detail_{prefix}"] = str(cell.get("detail") or "")
            row[f"source_{prefix}"] = str(cell.get("source") or "")
            if row[f"detail_{prefix}"]:
                notes.append(f"{kind}: {row[f'detail_{prefix}']}")
        row["note"] = " ｜ ".join(notes)
        row["mismatch"] = any(
            row[f"builtin_{prefix}"] != row[f"probed_{prefix}"]
            for prefix in ("xray", "singbox")[: len(kinds)]
        )
        rows.append(row)
    return rows


def pms_core_matrix_summary_text(pms_payload: dict[str, Any]) -> str:
    """一句话结论（日志 / 状态栏用）：多少格与内置表不一致。"""
    if not pms_payload:
        return "还没有探测过协议能力矩阵"
    rows = pms_core_matrix_rows(pms_payload)
    diff = [row["title"] for row in rows if row.get("mismatch")]
    cores = pms_payload.get("cores") if isinstance(pms_payload.get("cores"), dict) else {}
    versions = "、".join(
        f"{kind} {info.get('version') or '版本未知'}" for kind, info in cores.items() if isinstance(info, dict)
    )
    head = f"协议能力矩阵探测完成（{len(rows)} 个协议，{versions or '未识别到核心'}）"
    if diff:
        return f"{head}；**{len(diff)} 项与内置表不一致**：{'、'.join(diff[:6])}"
    return f"{head}；与内置表一致（没有过期项）"


class PmsCoreMatrixSignals(QObject):
    """探测任务 → 主线程。"""

    pcm_finished = Signal(dict)


class PmsCoreMatrixTask(QRunnable):
    """后台跑一轮矩阵探测（每格要起一个核心进程做自检，别放界面线程）。"""

    def __init__(
        self,
        pms_signals: PmsCoreMatrixSignals,
        pms_paths: "PmsRuntimePaths",
        pms_settings: "PmsSettingsManager",
        pms_locator: "PmsCoreLocator",
        pms_samples: Optional[dict[str, dict[str, Any]]] = None,
        pms_save_cache: bool = True,
    ) -> None:
        super().__init__()
        self.pcm_signals = pms_signals
        self.pcm_paths = pms_paths
        self.pcm_settings = pms_settings
        self.pcm_locator = pms_locator
        self.pcm_samples = pms_samples
        self.pcm_save_cache = bool(pms_save_cache)
        self.pcm_logger = logging.getLogger(PMS_LOGGER_NAME + ".corematrix")
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        try:
            payload = pms_core_matrix_probe(self.pcm_paths, self.pcm_settings, self.pcm_locator, self.pcm_samples)
            if self.pcm_save_cache:
                pms_core_matrix_cache_save(self.pcm_paths, payload)
        except Exception as exc:  # noqa: BLE001 - 探测失败也要给界面一个交代，不能把线程搞崩
            self.pcm_logger.warning("协议能力矩阵探测异常：%s", exc)
            payload = {"error": str(exc), "cells": {}, "protocols": list(pms_core_matrix_protocols())}
        try:
            self.pcm_signals.pcm_finished.emit(payload)
        except RuntimeError:
            # 程序正在退出（信号对象已销毁）：结果没人要就算了，别从工作线程往外抛
            self.pcm_logger.debug("矩阵探测结果无人接收（程序正在退出），已丢弃")
