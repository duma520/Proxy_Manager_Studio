#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量测试分片与坏节点剔除（v1.9.28）

**为什么单独一个模块**：`pms/latency.py` 顶在 1000 行红线上（宪法「单文件 ≤ 1000 行」），
这批逻辑（分片 + 从核心输出里定位坏出站 + 逐个剔除重试）自成一块，拆出来两边都清爽。

**要解决的问题（真机实测）**：批量通道是「一个核心带 N 个节点出站」，只要**任何一个**节点
核心加载不了，整个核心就起不来，而且原来核心输出被丢给 DEVNULL、配置也不先校验，
用户看到的是「成功 0/33604」，日志里只有一句「批量测试核心提前退出」。
真库实测（33,604 个节点、11 MB 配置）：

    FATAL[0013] decode config: outbounds[24266].plugin: json: unknown field "plugin"

一个 ShadowsocksR 节点（sing-box 1.6.0 起已移除该协议）就让整批 0 结果。

**做法**：① 分片——每个核心最多带 `PMS_LATENCY_BATCH_MAX_OUTBOUNDS` 个出站，配置小、启动快，
坏节点只影响自己那一批；② 剔除重试——核心的原话里通常直接点名 `outbounds[N].xxx`，
N 就是这一片里第几个节点，把它摘掉再试，最多 `PMS_LATENCY_BATCH_MAX_REPAIR` 轮。
这样**一个坏节点只损失它自己**。

★ 本模块的对外符号全部私有（下划线开头）：`_scaffold/check_modules.py` 要求启动器再导出
  包内全部公开类与模块级函数，而 `Proxy_Manager_Studio.py` 压在 400 行红线上。
  与 `pms/testresume.py` 是同一套办法。
"""

from __future__ import annotations

from typing import (
    Any,
    Callable,
    Optional,
    Sequence,
)

from pms.constants import (
    PMS_CORE_KIND_AUTO,
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
)

import os
import subprocess
import time
from PySide6.QtCore import QTimer
from pms.constants import PMS_IS_WINDOWS, PMS_ROUTING_MODE_GLOBAL
from pms.coreconfig import PmsSingBoxConfigBuilder, PmsXrayConfigBuilder
from pms.coreprofile import PmsCoreLaunchProfile
from pms.models import PmsProxyNode
from pms.statsapi import PmsClashApiClient
from pms.utils import pms_atomic_write_text, pms_find_free_port, pms_is_port_available, pms_json_pretty

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------
# 每个批量核心最多带几个出站。1000 个时配置约 0.2 MB、`sing-box check` 约 3 秒、启动约 1.5 秒；
# 3 万节点不分片则是 11 MB、启动十几秒，且一个坏节点全灭。
PMS_LATENCY_BATCH_MAX_OUTBOUNDS = 1000

# 单个分片因为「核心加载不了」重试剔除坏节点的最大轮数（防死循环）
PMS_LATENCY_BATCH_MAX_REPAIR = 20

# 每个分片最少这么多个节点，避免把分片切得过碎
PMS_LATENCY_BATCH_MIN_CHUNK = 50


def _plt_bad_outbound_index(pms_output: str) -> Optional[int]:
    """从核心输出里抠出「第几个出站有问题」。

    核心原话有两种写法，**都要认**（实测都出现过）：
        · `FATAL: decode config at xxx.json: outbounds[24266].plugin: json: unknown field "plugin"`
        · `FATAL: initialize outbound[265]: ShadowsocksR is deprecated and removed in sing-box 1.6.0`
    抠不到（例如报的是别的错）就返回 None——调用方必须当成「定位不了」，绝不能瞎猜一个下标去删。
    """
    import re

    match = re.search(r"outbound(s)?\[(\d+)\]", str(pms_output or ""))
    if not match:
        return None
    try:
        return int(match.group(2))
    except ValueError:
        return None


def _plt_reject_note(pms_reason: str) -> str:
    """把核心的原话压成用户能看懂的一句话（原始原因仍留在 `plt_batch_rejected` 里可查）。

    这几种是实测真出现过的：协议已被核心移除、节点字段核心不认、以及其它加载失败。
    """
    text = str(pms_reason or "")
    lowered = text.lower()
    if "deprecated and removed" in lowered or "removed in sing-box" in lowered:
        protocol = ""
        if "shadowsocksr" in lowered:
            protocol = "ShadowsocksR"
        return f"当前核心已移除该协议（{protocol}），请换用支持它的核心（如 Xray）" if protocol else "当前核心已移除该协议"
    if "not included in this build" in lowered:
        return "当前核心的构建没编入这类出站（naive），请换用支持它的核心（如 Xray）"
    if "unknown outbound type" in lowered:
        return "当前核心不认识这类出站（juicity），请换用支持它的核心（如 Xray）"
    if "unknown field" in lowered:
        return "该节点的某个字段当前核心不认识，已跳过它"
    if "tls required" in lowered:
        return "该节点缺少 TLS 配置（当前核心要求必须开 TLS），已跳过它"
    if not text:
        return "核心加载不了该节点"
    return f"核心加载不了该节点：{text[:60]}"


def _plt_resolve_node_kinds(pms_locator: Any, pms_settings: Any, pms_nodes: Sequence[Any]) -> dict[str, str]:
    """给每个节点定核心（v1.9.29）：`core.kind = auto` 时**优先 sing-box，载不动兜底 Xray**。

    返回 {uid: 核心代号}。两个核心都载不动的节点**不在字典里**——调用方必须把它们
    记成「测不了」并出队，否则它们永远不报回，测试永远结不了。

    ★ 必须走 `pcl_resolve_kinds_for_nodes()`（只查一遍核心可用性）：
    逐节点调 `pcl_resolve_kind_for_node()` 每个节点都要重搜核心目录，实测 3.3 万个节点要 16 分钟。
    """
    requested = PMS_CORE_KIND_AUTO
    try:
        requested = pms_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO)
    except Exception:  # noqa: BLE001 - 替身设置对象可能没有这个方法
        requested = PMS_CORE_KIND_AUTO
    try:
        return pms_locator.pcl_resolve_kinds_for_nodes(pms_nodes, requested, pms_settings)
    except AttributeError:  # noqa: PERF203 - 老版定位器没有批量方法时逐个退化
        kinds: dict[str, str] = {}
        for node in pms_nodes:
            kind = pms_locator.pcl_resolve_kind_for_node(node, requested, pms_settings)
            if kind:
                kinds[str(getattr(node, "uid", "") or "")] = str(kind)
        return kinds


def _plt_build_core_queue(
    pms_nodes: Sequence[Any],
    pms_kinds: dict[str, str],
    pms_chunk_size: int = PMS_LATENCY_BATCH_MAX_OUTBOUNDS,
) -> list[tuple[str, list[Any]]]:
    """把节点排成一条 `(核心代号, 分片节点)` 队列。

    **sing-box 的分片全部排在 Xray 的分片前面**：两种核心不能共用一次批量配置，
    同一种核心跑完（关核）再起另一种最省内存，也避免两个核心同时抢端口。
    同一种核心内部保持用户原本的节点顺序。
    """
    queue: list[tuple[str, list[Any]]] = []
    for kind in (PMS_CORE_KIND_SINGBOX, PMS_CORE_KIND_XRAY):
        group = [node for node in pms_nodes if pms_kinds.get(str(node.uid)) == kind]
        if not group:
            continue
        for chunk in _PmsBatchRepairer.pbp_split(group, pms_chunk_size):
            if chunk:
                queue.append((kind, chunk))
    return queue


def _plt_read_core_output(pms_process: Any) -> str:
    """取出批量核心的输出（v1.9.28：原来直接丢给 DEVNULL，出错时完全不知道原因）。

    核心退出后它的 stderr 就是我们唯一能拿到的「为什么」，所以启动时留管道、失败时读它。
    """
    if pms_process is None:
        return ""
    try:
        stream = getattr(pms_process, "stdout", None)
        if stream is None:
            return ""
        return bytes(stream.read() or b"").decode("utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return ""


def _plt_unsupported_nodes(pms_nodes: Sequence[Any], pms_kinds: dict[str, str]) -> list[tuple[Any, str]]:
    """「已装的核心都载不动」的节点 → ``[(节点, 原因)]``。

    这些节点必须先记成「测不了」并出队，否则它们永远不报回，测试永远结不了
    （只装 sing-box 却碰上 SSR 节点就是这种情况）。
    """
    from pms.constants import PMS_PROTOCOL_TITLES

    unsupported: list[tuple[Any, str]] = []
    for node in pms_nodes:
        if str(getattr(node, "uid", "") or "") in pms_kinds:
            continue
        protocol = str(getattr(node, "protocol", "") or "")
        title = PMS_PROTOCOL_TITLES.get(protocol, protocol)
        unsupported.append((node, f"当前已装的核心都不支持 {title}"))
    return unsupported


def _plt_chunk_note(pms_index: int, pms_total: int) -> str:
    """「第几片 / 共几片」（只有多片时才提）。"""
    return "" if pms_total <= 1 else f"第 {pms_index}/{pms_total} 批"


class _PmsBatchRepairer:
    """分片工具 + 「核心加载不了就摘掉那几个节点」的修复器。

    两个回调由调用方（`PmsLatencyTestCoordinator`）注入，本类不碰核心也不碰界面：

        pms_try_prepare(nodes) -> bool   按这批节点建配置并起核（内部校验通过才算成功）
        pms_last_error() -> str          上一次失败时核心的原话

    这样拆的好处：修复逻辑可以脱离核心、脱离 Qt 单独测（探针就是这么做的）。
    """

    def __init__(
        self,
        pms_try_prepare: Callable[[Sequence[Any]], bool],
        pms_last_error: Callable[[], str],
        pms_logger: Any = None,
        pms_max_rounds: int = PMS_LATENCY_BATCH_MAX_REPAIR,
        pms_rejected_sink: Optional[dict[str, str]] = None,
    ) -> None:
        self._pbr_try_prepare = pms_try_prepare
        self._pbr_last_error = pms_last_error
        self._pbr_logger = pms_logger
        self._pbr_max_rounds = max(1, int(pms_max_rounds))
        self._pbr_rejected = pms_rejected_sink if pms_rejected_sink is not None else {}

    # ---------------------------------------------------------------- 分片
    @staticmethod
    def pbp_split(pms_nodes: Sequence[Any], pms_size: int = PMS_LATENCY_BATCH_MAX_OUTBOUNDS) -> list[list[Any]]:
        """把节点切成若干片；数量小于一片时就是一片（行为与分片前完全一致）。"""
        nodes = list(pms_nodes)
        size = max(PMS_LATENCY_BATCH_MIN_CHUNK, int(pms_size or 0))
        if not nodes:
            return [[]]
        return [nodes[start:start + size] for start in range(0, len(nodes), size)]

    @staticmethod
    def pbp_bad_index(pms_output: str) -> Optional[int]:
        """（转发给模块函数，方便调用方只 import 一个类）"""
        return _plt_bad_outbound_index(pms_output)

    # ---------------------------------------------------------------- 剔除重试
    def pbp_repair(self, pms_chunk: Sequence[Any]) -> tuple[list[Any], list[Any], str]:
        """反复「起核失败 → 按核心点名摘掉坏节点 → 再试」，返回 (可用节点, 被剔除节点, 最后错误)。

        调用**前后**语义：进来时这一片的核心是起不来的（或根本没起过），
        返回时如果 `error` 为空，说明当前这一片已经成功起核、可以派发任务了。
        """
        active = list(pms_chunk)
        rejected: list[Any] = []
        reason = ""
        for _ in range(self._pbr_max_rounds):
            if self._pbr_try_prepare(active):
                return active, rejected, ""
            reason = str(self._pbr_last_error() or "核心拒绝加载该配置（无输出）")
            index = _plt_bad_outbound_index(reason)
            if index is None or not (0 <= index < len(active)):
                # 定位不了就别乱删——整片判为不可用，把核心原话交出去
                return active, rejected, reason
            node = active.pop(index)
            rejected.append(node)
            uid = str(getattr(node, "uid", "") or "")
            if uid:
                self._pbr_rejected[uid] = reason
            if self._pbr_logger is not None:
                name = ""
                try:
                    name = node.pnode_display_name()
                except Exception:  # noqa: BLE001 - 替身对象可能没这个方法
                    name = uid or "?"
                self._pbr_logger.warning("批量测试剔除核心加载不了的节点「%s」：%s", name, reason[:200])
            if not active:
                return active, rejected, reason
        return active, rejected, reason or "重试次数用尽"

class _PltBatchMixin:
    """批量测试核心的准备与分片（Clash API / Xray 真连接两条路）（3 个方法，v1.9.42 从 `latency.py` 拆出）。

    ★ 搬出来只是为了给 `pms/latency.py` 腾红线余量（方法名、self.plt_* 属性名一个字没改）。
    """

    def plt_prepare_clash_batch(self, pms_core_kind: str, pms_nodes: Sequence[PmsProxyNode]) -> bool:
        """构造“一个核心 + 全部节点出站 + Clash API”的批量测试配置，**先校验再启动**。

        返回 False 时用 `plt_last_prepare_error` 取原因（v1.9.28）。
        先校验再启动是关键：核心加载不了配置时会直接退出，原来只能等 12 秒超时且拿不到原因；
        现在 `sing-box check` 会直接点名 `outbounds[N].xxx`，调用方据此精确剔除坏节点。
        """
        self.plt_last_prepare_error = ""
        core_path = self.plt_locator.pcl_find(pms_core_kind, self.plt_settings)
        if core_path is None:
            self.plt_last_prepare_error = "未找到核心程序"
            return False
        port = pms_find_free_port(29290, "127.0.0.1")
        inbound_port = pms_find_free_port(29380, "127.0.0.1")
        self.plt_batch_port = port
        singbox_builder = PmsSingBoxConfigBuilder()
        outbounds: list[dict[str, Any]] = []
        for index, node in enumerate(pms_nodes):
            profile = PmsCoreLaunchProfile.pclp_from_settings(self.plt_settings, node, PMS_CORE_KIND_SINGBOX)
            profile.tun_enabled = False
            profile.mux_enabled = False
            outbound = singbox_builder.psc_build_proxy_outbound(profile)
            outbound["tag"] = f"{self.plt_batch_tag_prefix}{index}"
            outbounds.append(outbound)
        outbounds.append({"type": "direct", "tag": "direct", "domain_resolver": "dns-direct"})
        config: dict[str, Any] = {
            "log": {"level": "error", "timestamp": True},
            "dns": singbox_builder.psc_build_minimal_dns(),
            "inbounds": [{"type": "mixed", "tag": "batch-in", "listen": "127.0.0.1", "listen_port": inbound_port}],
            "outbounds": outbounds,
            "route": {"rules": [], "final": outbounds[0]["tag"], "auto_detect_interface": True,
                      "default_domain_resolver": {"server": "dns-direct", "strategy": "prefer_ipv4"}},
            "experimental": {"clash_api": {"external_controller": f"127.0.0.1:{port}", "default_mode": "global"}},
        }
        config_path = self.plt_paths.prt_temp_config_file("latency_batch")
        # v1.6.7：记下本次的临时配置路径，收尾时删掉（原来从不清理，一直留在临时目录）。
        self.plt_batch_config_path = config_path
        if not pms_atomic_write_text(config_path, pms_json_pretty(config)):
            self.plt_last_prepare_error = "写入批量测试配置失败"
            return False
        # v1.9.28：先让核心自己验一遍配置——通过了才起核，失败时拿到的是**可定位的原话**
        if not self.plt_validate_batch_config(core_path, config_path):
            self.plt_last_prepare_error = self.plt_last_validate_error or "核心拒绝加载该配置（无输出）"
            self.plt_logger.error("批量测试配置校验未通过：%s", self.plt_last_prepare_error[:300])
            try:
                config_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.plt_batch_config_path = None
            return False
        command = [str(core_path), "run", "-c", str(config_path), "-D", str(self.plt_paths.prt_data_dir)]
        try:
            # v1.9.28：合并到管道而不是 DEVNULL——核心退出时必须能说出「为什么」
            self.plt_batch_process = subprocess.Popen(
                command,
                cwd=str(core_path.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if PMS_IS_WINDOWS else 0,  # type: ignore[attr-defined]
            )
        except OSError as exc:
            self.plt_logger.error("批量测试核心启动失败：%s", exc)
            self.plt_last_prepare_error = f"核心启动失败：{exc}"
            return False
        client = PmsClashApiClient(port, pms_logger=self.plt_logger)
        deadline = time.time() + 12.0
        while time.time() < deadline:
            if client.pca_is_available(1.5):
                self.plt_batch_client = client
                self.plt_logger.info("批量测试核心就绪（Clash API 端口 %d）", port)
                return True
            if self.plt_batch_process.poll() is not None:
                detail = _plt_read_core_output(self.plt_batch_process)
                self.plt_last_prepare_error = detail or "核心提前退出（没有输出）"
                self.plt_logger.error("批量测试核心提前退出：%s", self.plt_last_prepare_error[:300])
                self.plt_close_clash_batch()
                return False
            time.sleep(0.2)
        self.plt_logger.error("批量测试核心 Clash API 未就绪")
        self.plt_last_prepare_error = "核心已启动但 Clash API 未就绪"
        self.plt_close_clash_batch()
        return False

    def plt_open_next_chunk(self, pms_core_kind: Optional[str] = None) -> bool:
        """打开下一片：关掉上一个批量核心 → 起新核 → 派发任务。返回是否还有活干。

        队列元素是 `(核心代号, 分片节点)`，所以**同一轮测试里可以在 sing-box 与 Xray 之间切换**
        （v1.9.29：auto 模式下先把 sing-box 的分片跑完，再跑 Xray 的那些）。
        """
        self.plt_close_batch_core()
        if not self.plt_running:
            return False
        if not self.plt_chunk_queue:
            self.plt_finish("")
            return False
        kind, chunk = self.plt_chunk_queue.pop(0)
        kind = kind or pms_core_kind or self.plt_core_kind
        self.plt_chunk_index += 1
        # 每片按自己的核心重新定模式：sing-box 走 Clash API，Xray 走单核多入站
        self.plt_mode = self.plt_resolve_batch_mode(kind, max(2, len(chunk)))
        self.plt_core_kind = kind
        self.plt_core_path = self.plt_locator.pcl_find(kind, self.plt_settings)
        active = list(chunk)
        if not self.plt_prepare_batch_for_chunk(kind, active):
            active, rejected, error = self.plt_batch_repair_chunk(kind, active)
            for node in rejected:
                self.plt_record_rejected(node, error)
            if not active:
                # 这一片全军覆没：接着看下一片，或者收工
                if self.plt_done >= self.plt_total:
                    self.plt_finish("")
                else:
                    QTimer.singleShot(0, lambda: self.plt_open_next_chunk(""))
                return True
        self.plt_chunk_nodes = active
        self.plt_chunk_remaining = len(active)
        self.plt_submit_chunk(kind, active)
        note = _plt_chunk_note(self.plt_chunk_index, self.plt_chunk_total)
        self.plt_message.emit(
            f"正在测试 {self.plt_total} 个节点（{self._plt_core_label(kind)}"
            f"{'，' + note if note else ''}，并发 {self.plt_pool.maxThreadCount()}）"
        )
        return True

    # ------------------------------------------------------------------ Xray 单核多出站批量通道（v1.9.0）
    def plt_prepare_xray_batch(self, pms_core_kind: str, pms_nodes: Sequence[PmsProxyNode]) -> bool:
        """构造“一个 Xray 核心 + 每节点一个 HTTP 入站 + 入站→出站路由”的批量测试配置，**先校验再启动**。

        与 sing-box 的 Clash 快通道思路一致：只起一个核心，所有节点共享，避免 N 个节点起 N 个进程的额外开销。
        Xray 没有 Clash API，因此每个节点配一个独立 HTTP 入站，延迟经该入站出口用 urllib 代理测量
        （复用 tempsession 的「经本地代理出口做真连接」思路，只是共享同一个核心）。
        """
        self.plt_last_prepare_error = ""
        core_path = self.plt_locator.pcl_find(pms_core_kind, self.plt_settings)
        if core_path is None:
            self.plt_last_prepare_error = "未找到核心程序"
            return False
        xray_builder = PmsXrayConfigBuilder()
        inbounds: list[dict[str, Any]] = []
        outbounds: list[dict[str, Any]] = []
        routing_rules: list[dict[str, Any]] = []
        self.plt_xray_ports = {}
        self.plt_xray_tags = {}
        used_ports: set[int] = set()
        first_tag = ""
        for index, node in enumerate(pms_nodes):
            profile = PmsCoreLaunchProfile.pclp_from_settings(self.plt_settings, node, pms_core_kind)
            profile.tun_enabled = False
            profile.mux_enabled = False
            profile.udp_enabled = True
            profile.sniff_enabled = False
            profile.routing_mode = PMS_ROUTING_MODE_GLOBAL
            out_full = xray_builder.pxc_build_outbounds(profile)
            outbound = dict(out_full[0])
            out_tag = f"{self.plt_batch_tag_prefix}{index}"
            outbound["tag"] = out_tag
            outbounds.append(outbound)
            if index == 0:
                first_tag = out_tag
            port = pms_find_free_port(29500 + index, "127.0.0.1")
            while port in used_ports:
                port = pms_find_free_port(port + 1, "127.0.0.1")
            used_ports.add(port)
            self.plt_xray_ports[node.uid] = port
            self.plt_xray_tags[node.uid] = out_tag
            inbounds.append({
                "tag": f"probe-in-{index}",
                "listen": "127.0.0.1",
                "port": int(port),
                "protocol": "http",
                "settings": {"accounts": [], "allowTransparent": False},
            })
            routing_rules.append({"type": "field", "inboundTag": [f"probe-in-{index}"], "outboundTag": out_tag})
        outbounds.append({"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "AsIs"}})
        outbounds.append({"tag": "block", "protocol": "blackhole", "settings": {"response": {"type": "http"}}})
        config: dict[str, Any] = {
            "log": {"loglevel": "warning", "timestamp": True},
            "inbounds": inbounds,
            "outbounds": outbounds,
            "routing": {"domainStrategy": "AsIs", "rules": routing_rules, "final": first_tag or outbounds[0]["tag"]},
        }
        config_path = self.plt_paths.prt_temp_config_file("latency_batch_xray")
        self.plt_xray_config_path = config_path
        if not pms_atomic_write_text(config_path, pms_json_pretty(config)):
            self.plt_last_prepare_error = "写入 Xray 批量配置失败"
            return False
        # v1.9.28：与 Clash 通道同一口径——先校验再启动，失败时能点名第几个出站
        if not self.plt_validate_batch_config(core_path, config_path):
            self.plt_last_prepare_error = self.plt_last_validate_error or "核心拒绝加载该配置（无输出）"
            self.plt_logger.error("Xray 批量测试配置校验未通过：%s", self.plt_last_prepare_error[:300])
            try:
                config_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.plt_xray_config_path = None
            return False
        environment = dict(os.environ)
        environment["XRAY_LOCATION_ASSET"] = str(core_path.parent)
        command = [str(core_path), "run", "-c", str(config_path)]
        try:
            # v1.9.28：留管道以便核心退出时能说出原因
            self.plt_xray_process = subprocess.Popen(
                command,
                cwd=str(core_path.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if PMS_IS_WINDOWS else 0,  # type: ignore[attr-defined]
            )
        except OSError as exc:
            self.plt_logger.error("Xray 批量测试核心启动失败：%s", exc)
            self.plt_last_prepare_error = f"核心启动失败：{exc}"
            return False
        deadline = time.time() + 12.0
        while time.time() < deadline:
            if self.plt_xray_process.poll() is not None:
                self.plt_logger.error("Xray 批量测试核心提前退出")
                self.plt_close_xray_batch()
                return False
            # 所有 probe 入站端口都被核心占用 = 核心已监听就绪
            if all(not pms_is_port_available(p, "127.0.0.1") for p in used_ports):
                self.plt_logger.info("Xray 批量测试核心就绪（%d 个节点出口）", len(pms_nodes))
                return True
            time.sleep(0.1)
        self.plt_logger.error("Xray 批量测试核心端口未就绪（超时）")
        self.plt_close_xray_batch()
        return False
