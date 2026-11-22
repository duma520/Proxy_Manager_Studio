#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""延迟测试调度

批量延迟测试：sing-box 走 Clash API 快通道，Xray/单节点走临时核心通道；带进度与汇总。
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    Sequence,
)
from PySide6.QtCore import (
    QObject,
    QThreadPool,
    QTimer,
    Signal,
)
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

if TYPE_CHECKING:  # 仅类型检查，运行时不导入，避免循环依赖
    from pms.database import PmsDatabaseEngine

from pms.constants import (
    PMS_CORE_KIND_AUTO,
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
    PMS_DEFAULT_SPEED_TEST_URL,
    PMS_DEFAULT_TEST_URLS,
    PMS_EXIT_POOL_WAIT_MS,
    PMS_IS_WINDOWS,
    PMS_LOGGER_NAME,
    PMS_PROTOCOL_TITLES,
    PMS_ROUTING_MODE_GLOBAL,
    PMS_SPEED_SOURCE_CLOUDFLARE,
)
from pms.utils import (
    pms_atomic_write_text,
    pms_find_free_port,
    pms_format_bytes,
    pms_is_port_available,
    pms_json_pretty,
    pms_resolve_speed_url,
)
from pms.paths import PmsRuntimePaths
from pms.models import PmsProxyNode
from pms.coreprofile import PmsCoreLaunchProfile
from pms.coreconfig import (
    PmsSingBoxConfigBuilder,
    PmsXrayConfigBuilder,
)
from pms.coresys import PmsCoreLocator
from pms.statsapi import PmsClashApiClient
from pms.tempsession import PmsTempCoreSession
from pms.tasks import (
    PmsClashDelayTask,
    PmsTaskSignals,
    PmsTempCoreTask,
    PmsXrayBatchDelayTask,
)
from pms.testresume import (
    _PmsTestRound,
    _PmsTestRoundStore,
)

import re

from pms.latencybatch import (
    PMS_LATENCY_BATCH_MAX_OUTBOUNDS,
    _PmsBatchRepairer,
    _plt_build_core_queue,
    _plt_chunk_note,
    _plt_read_core_output,
    _plt_reject_note,
    _plt_resolve_node_kinds,
    _plt_unsupported_nodes,
)

from pms.latencybatch import _PltBatchMixin

# --------------------------------------------------------------------------------------
# 延迟测试 / 测速调度器
# --------------------------------------------------------------------------------------
def _plt_reason_bucket(pms_note: str) -> str:
    """把失败说明归成界面一眼能看懂的几类（v1.9.19）。

    用户抱怨「测延迟全是超时」时，最需要知道的不是「失败了多少个」，
    而是**失败在什么地方**：是这个地址太慢、还是节点自己坏了、还是核心接口不通。
    """
    text = str(pms_note or "")
    lowered = text.lower()
    if "超时" in text or "timeout" in lowered or "timed out" in lowered:
        return "超时"
    if "核心接口错误" in text or "HTTP 5" in text or "核心没返回延迟值" in text or "无法解析" in text:
        return "核心报错"
    if "连接失败" in text or "请求异常" in text:
        return "连不上节点"
    if not text:
        return "未测试"
    return "其它"


class PmsLatencyTestCoordinator(_PltBatchMixin, QObject):
    """批量延迟测试调度：sing-box 走 Clash API 快通道，Xray 走“临时核心”通道。"""

    plt_progress = Signal(int, int, str)               # 已完成, 总数, 当前节点名
    plt_node_result = Signal(str, int, float, bool, str)  # uid, 延迟, 速率, 成功, 说明
    plt_finished = Signal(dict)
    plt_message = Signal(str)

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_paths: PmsRuntimePaths,
        pms_locator: PmsCoreLocator,
        pms_parent: Optional[QObject] = None,
        pms_engine: Optional["PmsDatabaseEngine"] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.plt_settings = pms_settings
        self.plt_paths = pms_paths
        self.plt_locator = pms_locator
        self.plt_logger = logging.getLogger(PMS_LOGGER_NAME + ".latency")
        self.plt_pool = QThreadPool(self)
        self.plt_signals = PmsTaskSignals(self)
        self.plt_signals.ptask_node_done.connect(self.plt_on_node_done)
        self.plt_signals.ptask_progress.connect(self.plt_on_task_progress)
        self.plt_running = False
        self.plt_total = 0
        self.plt_done = 0
        self.plt_results: dict[str, tuple[int, float, bool, str]] = {}
        self.plt_nodes: dict[str, PmsProxyNode] = {}
        self.plt_mode = ""
        self.plt_speed_enabled = False
        self.plt_batch_session: Optional[PmsTempCoreSession] = None
        self.plt_batch_process: Optional[subprocess.Popen] = None
        self.plt_batch_client: Optional[PmsClashApiClient] = None
        # v1.6.7：批量测试核心的临时配置文件路径，收尾时要删掉。
        self.plt_batch_config_path: Optional[Path] = None
        self.plt_batch_port = 0
        self.plt_batch_tag_prefix = "pms-node-"
        # v1.9.0：Xray 单核多出站批量通道（Xray 无 Clash API，改用「每节点独立 HTTP 入站 + 路由」复用单核）
        self.plt_xray_ports: dict[str, int] = {}
        self.plt_xray_tags: dict[str, str] = {}
        self.plt_xray_process: Optional[subprocess.Popen] = None
        self.plt_xray_config_path: Optional[Path] = None
        # v1.8.0 断点续跑：pms_engine 只在主界面装配时传入；测试脚本不传也能跑，
        # 此时 _PmsTestRoundStore 内部全部读写直接退化成空操作。
        self.plt_store = _PmsTestRoundStore(pms_engine)
        self.plt_round: Optional[_PmsTestRound] = None
        self.plt_scope = "all"
        # v1.8.0：失败重试要重新派发任务，得记住本轮用的是哪个核心与每个节点的 Clash tag
        self.plt_core_kind = ""
        self.plt_core_path: Optional[Path] = None
        self.plt_tag_of_uid: dict[str, str] = {}
        # v1.9.9：正在跑的临时核心会话（tempcore 模式下每个节点一个）。
        # 停止时逐个打断，否则线程卡在 IO 上不回来，Python 进程退不出去。
        self.plt_sessions: set[Any] = set()
        self.plt_sessions_lock = threading.RLock()
        # v1.9.28 分片：批量通道一次只跑一个分片，跑完再换下一个核心。
        # plt_chunk_queue 是还没跑的分片；plt_chunk_remaining 是当前分片还剩几个节点没出最终结果。
        self.plt_chunk_queue: list[list[PmsProxyNode]] = []
        self.plt_chunk_nodes: list[PmsProxyNode] = []
        self.plt_chunk_remaining = 0
        self.plt_chunk_index = 0
        self.plt_chunk_total = 0
        # v1.9.29：每个节点用哪个核心（auto = 优先 sing-box、载不动兜底 Xray）
        self.plt_node_kinds: dict[str, str] = {}
        # 核心加载不了被剔除的节点：uid → 核心原话（收尾时当作失败结果写进统计）
        self.plt_batch_rejected: dict[str, str] = {}
        self.plt_last_prepare_error = ""
        self.plt_last_validate_error = ""

    # ------------------------------------------------------------------ 会话登记（v1.9.9）
    def plt_register_session(self, pms_session: Any) -> None:
        """任务开跑时登记自己的测量会话。"""
        with self.plt_sessions_lock:
            self.plt_sessions.add(pms_session)

    def plt_unregister_session(self, pms_session: Any) -> None:
        """任务收尾时注销（discard 而非 remove：重复注销不能抛）。"""
        with self.plt_sessions_lock:
            self.plt_sessions.discard(pms_session)

    def plt_abort_sessions(self) -> int:
        """打断所有还在跑的测量会话，返回打断的个数。"""
        with self.plt_sessions_lock:
            sessions = list(self.plt_sessions)
        for session in sessions:
            try:
                session.ptcs_abort()
            except Exception:  # noqa: BLE001 - 退出路径上单个会话失败不能连累其余
                continue
        return len(sessions)

    def plt_is_running(self) -> bool:
        return self.plt_running

    # ------------------------------------------------------------------ 启动
    def plt_mark_unsupported(self, pms_nodes: Sequence[PmsProxyNode]) -> int:
        """把「已装的核心都载不动」的节点先记成失败结果（否则测试永远结不了）。"""
        marked = 0
        for node, reason in _plt_unsupported_nodes(pms_nodes, self.plt_node_kinds):
            self.plt_record_rejected(node, reason)
            marked += 1
        return marked

    def plt_resolve_node_kinds(self, pms_nodes: Sequence[PmsProxyNode]) -> dict[str, str]:
        return _plt_resolve_node_kinds(self.plt_locator, self.plt_settings, pms_nodes)

    def plt_build_batch_queue(self, pms_nodes: Sequence[PmsProxyNode]) -> list[tuple[str, list[PmsProxyNode]]]:
        return _plt_build_core_queue(pms_nodes, self.plt_node_kinds, PMS_LATENCY_BATCH_MAX_OUTBOUNDS)

    def plt_start(  # v1.9.37：pms_track_round=False → 「健康检查」用的一次性轮次（不写手动断点）；pms_max_threads>0 → 并发封顶（低优先级）
        self, pms_nodes: Sequence[PmsProxyNode], pms_include_speed: bool = False, pms_scope: str = "all",
        pms_track_round: bool = True, pms_max_threads: int = 0,
    ) -> tuple[bool, str]:
        """开始**一轮全新**的批量测试。返回 (是否已开始, 说明)。

        v1.8.0：会先清掉旧断点再建新轮次（同一时刻只允许存在一个断点）。
        v1.9.29：`core.kind = auto` 时**逐节点**选核心（优先 sing-box，兜底 Xray），
        两种核心的节点被排成一条队列串行跑完——这样 sing-box 载不动的那几个节点
        也会被 Xray 覆盖到，不再有「永远测不到」的节点。
        """
        if self.plt_running:
            return False, "已有测试在进行中"
        nodes = [node for node in pms_nodes if node]
        if not nodes:
            return False, "没有可测试的节点"
        core_kind = self.plt_locator.pcl_resolve_kind(self.plt_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO), self.plt_settings)
        if core_kind is None:
            return False, "未找到可用的核心程序（请先在设置里指定 xray.exe / sing-box.exe）"
        total = len(nodes)
        self.plt_scope = str(pms_scope or "all"); self.plt_core_kind = str(core_kind)   # v1.9.37 同行（守 1000 行红线）
        self.plt_core_path = self.plt_locator.pcl_find(core_kind, self.plt_settings)
        self.plt_tag_of_uid = {}
        self.plt_running = True
        self.plt_total = total
        self.plt_done = 0
        self.plt_results = {}
        # v1.8.0 断点：pending = 全部待测 uid；跑完 / 停止时按实际情况回收
        self.plt_round = _PmsTestRound("latency", bool(pms_include_speed), self.plt_scope, [node.uid for node in nodes]) if pms_track_round else None
        # v1.6.7：模式必须先清空。原来启动失败时 plt_mode 还留着上一次的值，
        # 随后 plt_finish / plt_stop 会按旧模式去关批量核心，清错对象。
        self.plt_mode = ""
        self.plt_nodes = {node.uid: node for node in nodes}
        self.plt_node_kinds = self.plt_resolve_node_kinds(nodes)
        self.plt_mark_unsupported(nodes)
        self.plt_speed_enabled = bool(pms_include_speed)
        concurrency = max(1, min(64, self.plt_settings.psm_get_int("test.concurrency", 8))); concurrency = min(concurrency, int(pms_max_threads)) if int(pms_max_threads) > 0 else concurrency
        self.plt_pool.setMaxThreadCount(concurrency)
        kinds_used = {self.plt_node_kinds.get(str(node.uid), "") for node in nodes}
        mixed_note = "，sing-box + Xray 两种核心" if len(kinds_used) > 1 else ""
        self.plt_message.emit(
            f"开始测试 {total} 个节点（{self._plt_mode_label(self.plt_resolve_batch_mode(core_kind, len(nodes)))}"
            f"，并发 {concurrency}{mixed_note}）"
        )
        queue = self.plt_build_batch_queue(nodes)
        self.plt_chunk_queue = list(queue)
        self.plt_chunk_index = 0
        self.plt_chunk_total = len(queue)
        self.plt_batch_rejected = {}
        if not self.plt_open_next_chunk(""):
            self.plt_running = False
            self.plt_round = None
            self.plt_store._ptstore_clear() if pms_track_round else None
            self.plt_finish("准备批量测试核心失败")
            return False, f"准备批量测试核心失败：{self.plt_last_prepare_error or '未知原因'}"
        # v1.8.0：派发完成即刻落盘——这样即使软件被强杀 / 崩溃，断点也已经在了
        if not self.plt_running:  # 极端情况：核心找不到，plt_open_next_chunk 已收尾
            self.plt_round = None
        self.plt_store._ptstore_save(self.plt_round) if pms_track_round else None   # 健康检查轮次不写手动断点
        return True, f"已开始测试 {total} 个节点"

    def plt_resume(self, pms_nodes: Sequence[PmsProxyNode], pms_round_id: str = "") -> tuple[bool, str]:
        """继续未完成的测试（断点续跑）。

        · 只派发断点里还在 pending 的节点，已测过的**绝不重测**
        · 断点里已被删除的节点自动剔除，中途新增的节点自动补进本轮
        · 返回值与 ``plt_start`` 同构，界面可以走同一套提示
        · v1.9.54：`pms_round_id` 指定续哪一轮；留空 = **最新一轮**（与 v1.8.0 一致）
        """
        if self.plt_running:
            return False, "已有测试在进行中"
        round_obj = self.plt_store._ptstore_load_round(pms_round_id)
        if round_obj is None:
            return False, "没有未完成的测试，无需续跑"
        if str(round_obj.kind) != "latency":
            return False, "上一次未完成的是「测速」，请用测速的续跑入口"
        nodes = [node for node in pms_nodes if node]
        if not nodes:
            return False, "没有可继续测试的节点"
        # 断点可能放了几天：先剔除已被删掉的节点，再把新增的节点补进本轮
        round_obj._ptround_drop_missing({node.uid for node in nodes})
        round_obj._ptround_add_new([node.uid for node in nodes])
        if round_obj._ptround_is_finished():
            self.plt_store._ptstore_clear()
            return False, "上一次测试其实已经跑完了"
        pending_uids = set(round_obj.pending)
        target = [node for node in nodes if node.uid in pending_uids]
        if not target:
            self.plt_store._ptstore_clear()
            return False, "断点里的节点都已经不存在了"
        core_kind = self.plt_locator.pcl_resolve_kind(self.plt_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO), self.plt_settings)
        if core_kind is None:
            return False, "未找到可用的核心程序（请先在设置里指定 xray.exe / sing-box.exe）"
        self.plt_core_kind = str(core_kind)
        self.plt_core_path = self.plt_locator.pcl_find(core_kind, self.plt_settings)
        self.plt_tag_of_uid = {}
        self.plt_round = round_obj
        self.plt_scope = str(round_obj.scope or "all")
        self.plt_speed_enabled = bool(round_obj.include_speed)
        self.plt_nodes = {node.uid: node for node in nodes}
        self.plt_mode = ""
        self.plt_results = {}
        self.plt_running = True
        self.plt_total = max(len(target), int(round_obj.total))
        self.plt_done = max(0, int(round_obj.total) - int(round_obj._ptround_pending_count()))
        concurrency = max(1, min(64, self.plt_settings.psm_get_int("test.concurrency", 8)))
        self.plt_pool.setMaxThreadCount(concurrency)
        self.plt_message.emit(
            f"继续上次测试：还剩 {len(target)} 个节点（已完成 {self.plt_done}/{self.plt_total}，并发 {concurrency}）"
        )
        # v1.9.29：续跑也逐节点选核心，只把 pending 里的节点排进队列（已测过的绝不重测）
        self.plt_node_kinds = self.plt_resolve_node_kinds(target)
        self.plt_mark_unsupported(target)
        self.plt_chunk_queue = self.plt_build_batch_queue(target)
        self.plt_chunk_index = 0
        self.plt_chunk_total = len(self.plt_chunk_queue)
        self.plt_batch_rejected = {}
        if not self.plt_open_next_chunk(""):
            self.plt_running = False
            self.plt_store._ptstore_save(round_obj)
            self.plt_finish("准备批量测试核心失败")
            return False, "准备批量测试核心失败（断点已保留）"
        self.plt_store._ptstore_save(round_obj)
        return True, f"继续上次测试：还剩 {len(target)} 个节点（已完成 {self.plt_done}/{self.plt_total}）"

    def plt_pending_info(self) -> dict[str, Any]:
        """断点信息（无断点返回空 dict）：round_id / kind / scope / pending / total / created_at …

        v1.9.54：默认仍是**最新一轮**（`_ptstore_load()` 的新语义）；要列全部轮次用
        `_PmsTestRoundStore._ptstore_list()`。
        """
        round_obj = self.plt_store._ptstore_load()
        if round_obj is None:
            return {}
        return round_obj._ptround_info()

    def plt_pending_count(self) -> int:
        """断点里还剩几个节点没测（0 = 没有断点）。"""
        return int(self.plt_pending_info().get("pending") or 0)

    def plt_clear_round(self, pms_round_id: str = "") -> int:
        """丢弃断点（「不想续跑了」）。

        v1.9.54：带 `pms_round_id` 时**只丢那一轮**，其余轮次一个都不碰，返回丢掉的轮次数；
        不带（默认）时把**本轮所在的这一类**全部丢掉，与 v1.8.0 的「丢弃断点」表现一致。
        """
        if str(pms_round_id or "").strip():
            return 1 if self.plt_store._ptstore_drop_round(pms_round_id) else 0
        self.plt_round = None
        self.plt_store._ptstore_clear()
        return 1

    def plt_resolve_batch_mode(self, pms_core_kind: str, pms_node_count: int) -> str:
        """返回本轮批量测试使用的通道模式：

        · ``"clash"``      —— sing-box 多节点，Clash API 一次起核测完（快通道）
        · ``"xray_batch"`` —— Xray 多节点，单核 + 每节点一个独立 HTTP 入站 + 路由（v1.9.0 新增）
        · ``"tempcore"``   —— 单节点或兜底，每个节点单独起临时核心
        """
        if pms_core_kind == PMS_CORE_KIND_SINGBOX and pms_node_count >= 2:
            return "clash"
        if pms_core_kind == PMS_CORE_KIND_XRAY and pms_node_count >= 2:
            return "xray_batch"
        return "tempcore"

    @staticmethod
    def _plt_mode_label(pms_mode: str) -> str:
        """把模式常量翻译成界面文案。"""
        return {
            "clash": "Clash API 快通道",
            "xray_batch": "Xray 单核多出站批量通道",
            "tempcore": "临时核心通道",
        }.get(pms_mode, "临时核心通道")

    # ------------------------------------------------------------------ Clash 批量通道
    def plt_build_chunks(self, pms_nodes: Sequence[PmsProxyNode]) -> list[list[PmsProxyNode]]:
        return _PmsBatchRepairer.pbp_split(pms_nodes, PMS_LATENCY_BATCH_MAX_OUTBOUNDS)


    def plt_close_clash_batch(self) -> None:
        """关闭批量测试核心并清理（含临时配置文件）。"""
        process = self.plt_batch_process
        self.plt_batch_process = None
        self.plt_batch_client = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
        # v1.6.7：核心退出后再删配置，避免 Windows 上文件被占用删不掉。
        config_path = self.plt_batch_config_path
        self.plt_batch_config_path = None
        if config_path is None:
            return
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            self.plt_logger.debug("批量测试临时配置清理失败：%s", config_path)

    # ------------------------------------------------------------------ 分片调度与坏节点剔除（v1.9.28）
    def plt_batch_repair_chunk(self, pms_core_kind: str, pms_chunk: list[PmsProxyNode]) -> tuple[list[PmsProxyNode], list[PmsProxyNode], Optional[str]]:
        """按核心点名的 `outbounds[N]` 逐个剔除坏节点后重试（策略在 `pms/latencybatch.py`）。

        核心原话里的 N 就是这一片里第几个节点；这样**一个坏节点只损失它自己**。
        """
        self.plt_core_kind = pms_core_kind or self.plt_core_kind
        repairer = _PmsBatchRepairer(
            pms_try_prepare=lambda nds: self.plt_prepare_batch_for_chunk(pms_core_kind, nds),
            pms_last_error=lambda: self.plt_last_prepare_error,
            pms_logger=self.plt_logger, pms_rejected_sink=self.plt_batch_rejected,
        )
        return repairer.pbp_repair(pms_chunk)

    def plt_validate_batch_config(self, pms_core_path: Path, pms_config_path: Path) -> bool:
        """调核心自带 check 验一遍批量配置；顺手把核心原话留在 `plt_last_validate_error`。"""
        ok, output = self.plt_locator.pcl_validate_config(pms_core_path, pms_config_path)
        self.plt_last_validate_error = str(output or "")
        return bool(ok)

    def plt_prepare_batch_for_chunk(self, pms_core_kind: str, pms_chunk: Sequence[PmsProxyNode]) -> bool:
        """按当前模式（clash / xray_batch）为一片节点起一个新的批量核心。"""
        if self.plt_mode == "xray_batch":
            return self.plt_prepare_xray_batch(pms_core_kind, pms_chunk)
        return self.plt_prepare_clash_batch(pms_core_kind, pms_chunk)

    def plt_submit_chunk(self, pms_core_kind: str, pms_chunk: Sequence[PmsProxyNode]) -> None:
        """为当前这一片派发任务（片内全部节点）。"""
        if self.plt_mode == "xray_batch":
            self.plt_submit_xray_tasks(pms_chunk, None)
        else:
            self.plt_submit_clash_tasks(pms_chunk, None)

    def plt_record_rejected(self, pms_node: PmsProxyNode, pms_reason: str) -> None:
        """把「核心加载不了」的节点记成失败结果（界面能看到原因，断点也不再等它）。"""
        uid = str(pms_node.uid)
        note = _plt_reject_note(pms_reason)
        self.plt_results[uid] = (-1, 0.0, False, note)
        self.plt_node_result.emit(uid, -1, 0.0, False, note)
        if self.plt_round is not None:
            # 先把失败次数顶过重试上限，再标记完成——否则会被当成「可重试失败」回队，来回死循环
            self.plt_round.failed[uid] = int(self.plt_round.retry_max) + 1
            self.plt_round._ptround_mark_done(uid, False)
            self.plt_store._ptstore_save_throttled(self.plt_round)
        self.plt_done += 1
        self.plt_progress.emit(self.plt_done, self.plt_total, pms_node.pnode_display_name())

    def _plt_core_label(self, pms_kind: str) -> str:
        """给状态栏用的短核心名。"""
        return {PMS_CORE_KIND_SINGBOX: "sing-box 核心", PMS_CORE_KIND_XRAY: "Xray 核心"}.get(str(pms_kind), str(pms_kind))


    def plt_close_batch_core(self) -> None:
        """关掉当前批量核心（按模式选）。"""
        if self.plt_mode == "xray_batch":
            self.plt_close_xray_batch()
        else:
            self.plt_close_clash_batch()

    def plt_chunk_advance(self) -> None:
        """当前分片的节点都出了结果 → 换下一片（用 singleShot 跳出回调栈，避免重入）。"""
        if not self.plt_running:
            return
        QTimer.singleShot(0, lambda: self.plt_open_next_chunk(""))


    def plt_close_xray_batch(self) -> None:
        """关闭 Xray 批量测试核心并清理（含临时配置文件）。"""
        process = self.plt_xray_process
        self.plt_xray_process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
        config_path = self.plt_xray_config_path
        self.plt_xray_config_path = None
        if config_path is None:
            return
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            self.plt_logger.debug("Xray 批量测试临时配置清理失败：%s", config_path)

    def plt_submit_xray_tasks(self, pms_nodes: Sequence[PmsProxyNode], pms_only_uids: Optional[set[str]] = None) -> None:
        """为每个节点提交一个“经其独立 HTTP 入站出口”的延迟测试任务。

        v1.8.0 语义对齐：``pms_only_uids`` 不为空时只派发这些节点（续跑用）；
        **端口按全量 pms_nodes 的下标分配**，才和 plt_prepare_xray_batch 建的配置对得上。
        """
        if not self.plt_xray_ports:
            return
        urls = self.plt_test_urls()
        timeout_ms = self.plt_settings.psm_get_int("test.timeout_ms", 5000)
        attempts = max(1, self.plt_settings.psm_get_int("test.retry_times", 1) + 1)
        for node in pms_nodes:
            if pms_only_uids is not None and node.uid not in pms_only_uids:
                continue
            port = self.plt_xray_ports.get(node.uid)
            if port is None:
                continue
            task = PmsXrayBatchDelayTask(self.plt_signals, node, port, urls, timeout_ms, attempts)
            self.plt_pool.start(task)

    def plt_submit_clash_tasks(self, pms_nodes: Sequence[PmsProxyNode], pms_only_uids: Optional[set[str]] = None) -> None:
        """为每个节点提交一个 Clash API 延迟测试任务。

        v1.8.0：``pms_only_uids`` 不为空时只派发这些节点（续跑用）；
        **但 tag 仍按全量 pms_nodes 的下标生成**，才和 plt_prepare_clash_batch 建的配置对得上。
        """
        client = self.plt_batch_client
        if client is None:
            return
        urls = self.plt_test_urls()
        timeout_ms = self.plt_settings.psm_get_int("test.timeout_ms", 5000)
        attempts = max(1, self.plt_settings.psm_get_int("test.retry_times", 1) + 1)
        for index, node in enumerate(pms_nodes):
            tag = f"{self.plt_batch_tag_prefix}{index}"
            self.plt_tag_of_uid[node.uid] = tag
            if pms_only_uids is not None and node.uid not in pms_only_uids:
                continue
            task = PmsClashDelayTask(self.plt_signals, client, node, tag, urls, timeout_ms, attempts)
            self.plt_pool.start(task)

    def plt_submit_tempcore_tasks(self, pms_core_kind: str, pms_nodes: Sequence[PmsProxyNode], pms_only_uids: Optional[set[str]] = None) -> None:
        """为每个节点提交“临时核心”任务（Xray 场景或单节点场景）。

        v1.8.0：``pms_only_uids`` 不为空时只派发这些节点（续跑用）。
        """
        core_path = self.plt_core_path if self.plt_core_path is not None else self.plt_locator.pcl_find(pms_core_kind, self.plt_settings)
        if core_path is None:
            self.plt_running = False
            self.plt_finish("未找到核心程序")
            return
        self.plt_core_path = core_path
        urls = self.plt_test_urls()
        timeout_ms = self.plt_settings.psm_get_int("test.timeout_ms", 5000)
        attempts = max(1, self.plt_settings.psm_get_int("test.retry_times", 1) + 1)
        speed_config = self.plt_build_speed_config() if self.plt_speed_enabled else None
        for node in pms_nodes:
            if pms_only_uids is not None and node.uid not in pms_only_uids:
                continue
            task = PmsTempCoreTask(
                self.plt_signals, self.plt_settings, self.plt_paths, pms_core_kind, core_path, node,
                urls, timeout_ms, attempts, speed_config,
                self,   # v1.9.9：把调度器交给任务，停止时才能打断它
            )
            self.plt_pool.start(task)

    def plt_build_speed_config(self) -> dict[str, Any]:
        """构造测速参数（供“延迟+测速”一次完成）。"""
        settings = self.plt_settings
        limit_mb = settings.psm_get_int("speed.per_node_limit_mb", 30)
        return {
            "url": pms_resolve_speed_url(
                settings.psm_get_str("speed.source", PMS_SPEED_SOURCE_CLOUDFLARE),
                settings.psm_get_int("speed.size_mb", 10),
                settings.psm_get_str("speed.url", PMS_DEFAULT_SPEED_TEST_URL),
            ),
            "duration_seconds": settings.psm_get_int("speed.duration_seconds", 10),
            "limit_bytes": int(limit_mb) * 1024 * 1024,
            # v1.9.16：采样次数（原来这个设置项没人读）
            "min_samples": settings.psm_get_int("speed.min_samples", 3),
        }

    def plt_test_urls(self) -> list[str]:
        """本轮**按顺序**要用的测试地址列表（v1.9.19）。

        设置里的「测试地址（每行一个）」原来只有一个调用点 `plt_pick_test_url()`——
        **永远只用第一条**，后面几条填了等于没填（临时核心的任务类本来写了「逐个试」的循环，
        接线时却被喂成 `[url]` 单条）。现在整张列表下发给任务：
        · 临时核心 / 单节点：一个地址不行就试下一个（原来就是这个设计，只是没接上）；
        · Clash 快通道 / Xray 单核：**重试时换下一个地址**，不额外拉长测试时间。
        """
        urls = [str(item).strip() for item in self.plt_settings.psm_get_list("test.url_list") if str(item).strip()]
        return urls or list(PMS_DEFAULT_TEST_URLS)

    def plt_pick_test_url(self) -> str:
        """取第一个可用测试地址（可自定义）。**契约不变**，内部改用 `plt_test_urls()`。"""
        return self.plt_test_urls()[0]

    # ------------------------------------------------------------------ 结果处理
    def plt_on_task_progress(self, pms_uid: str, pms_done: int, pms_total: int) -> None:
        """任务内部进度（测速时的字节数进度）。"""
        if not self.plt_running:
            return
        node = self.plt_nodes.get(str(pms_uid))
        if node is not None:
            self.plt_progress.emit(self.plt_done, self.plt_total, f"{node.pnode_display_name()} 测速中 {pms_format_bytes(pms_done)}")

    def plt_on_node_done(self, pms_uid: str, pms_latency: int, pms_speed: float, pms_success: bool, pms_note: str) -> None:
        """一个节点测试完成：记结果、出队；失败且没到重试上限就**回队重测一次**。

        v1.8.0：每完成一个节点就落一次盘，软件被强杀 / 崩溃时断点也已经在了。
        """
        if not self.plt_running:
            return
        uid = str(pms_uid)
        self.plt_results[uid] = (int(pms_latency), float(pms_speed), bool(pms_success), str(pms_note))
        node = self.plt_nodes.get(uid)
        name = node.pnode_display_name() if node else uid
        self.plt_node_result.emit(uid, int(pms_latency), float(pms_speed), bool(pms_success), str(pms_note))
        requeued = False
        if self.plt_round is not None:
            requeued = self.plt_round._ptround_mark_done(uid, bool(pms_success))
            self.plt_store._ptstore_save_throttled(self.plt_round)
        if requeued:
            # 还没到重试上限：重新派一次，本次不算「完成」，进度也不往前走
            self.plt_submit_retry(uid)
            self.plt_progress.emit(self.plt_done, self.plt_total, f"{name} 重试")
            return
        self.plt_done += 1
        self.plt_progress.emit(self.plt_done, self.plt_total, name)
        # v1.9.28：分片模式下这一片的节点都出结果了 → 换下一个分片的核心
        if self.plt_chunk_total and self.plt_chunk_remaining:
            self.plt_chunk_remaining -= 1
            if self.plt_chunk_remaining <= 0:
                self.plt_chunk_advance()
                return
        # v1.9.27：收工条件必须看**真实完成数**，不能只看断点的 pending 是否空。
        # 原来只判 `_ptround_is_finished()`：断点列表一旦被上限截断，
        # 前一半节点报回来就会误判「测完了」并立刻关核心 → 后一半节点永远测不到（4 万节点实测复现）。
        # 用 `plt_done >= plt_total` 兜底后，即使断点列表不全，也会一直跑到全部节点都有结果。
        all_reported = self.plt_done >= self.plt_total
        if self.plt_round is not None:
            if all_reported and self.plt_round._ptround_is_finished():
                self.plt_finish("")
        elif all_reported:
            self.plt_finish("")

    def plt_submit_retry(self, pms_uid: str) -> None:
        """把一个失败的节点重新排进线程池（重试时批量核心 / 核心路径还在）。"""
        node = self.plt_nodes.get(str(pms_uid))
        if node is None:
            return
        timeout_ms = self.plt_settings.psm_get_int("test.timeout_ms", 5000)
        attempts = max(1, self.plt_settings.psm_get_int("test.retry_times", 1) + 1)
        urls = self.plt_test_urls()
        if self.plt_mode == "clash":
            client = self.plt_batch_client
            tag = self.plt_tag_of_uid.get(node.uid, "")
            if client is None or not tag:
                return
            self.plt_pool.start(PmsClashDelayTask(self.plt_signals, client, node, tag, urls, timeout_ms, attempts))
            return
        if self.plt_mode == "xray_batch":
            port = self.plt_xray_ports.get(node.uid)
            if port is None:
                return
            self.plt_pool.start(PmsXrayBatchDelayTask(self.plt_signals, node, port, urls, timeout_ms, attempts))
            return
        core_path = self.plt_core_path
        if core_path is None or not self.plt_core_kind:
            return
        self.plt_pool.start(PmsTempCoreTask(
            self.plt_signals, self.plt_settings, self.plt_paths, self.plt_core_kind, core_path, node,
            urls, timeout_ms, attempts,
            self.plt_build_speed_config() if self.plt_speed_enabled else None,
            self,   # v1.9.9：同上
        ))

    def plt_finish(self, pms_error: str) -> None:
        """收尾：关闭批量核心、汇总结果。

        v1.8.0：只有当这一轮**真的跑完**（pending 清空）时才清断点；
        停止造成的收尾由 plt_stop 自己负责保留断点。
        """
        self.plt_running = False   # v1.9.37：四句同行（守 1000 行红线）
        self.plt_chunk_queue = []; self.plt_chunk_nodes = []; self.plt_chunk_remaining = 0
        if self.plt_mode == "clash":
            self.plt_close_clash_batch()
        elif self.plt_mode == "xray_batch":
            self.plt_close_xray_batch()
        pending = self.plt_round._ptround_pending_count() if self.plt_round is not None else 0
        if self.plt_round is not None and self.plt_round._ptround_is_finished():
            self.plt_store._ptstore_clear()
            self.plt_round = None
        ok_count = sum(1 for item in self.plt_results.values() if item[2])
        latencies = [item[0] for item in self.plt_results.values() if item[2] and item[0] >= 0]
        # v1.9.19：失败原因分类统计——「全是超时」时至少能看出失败在哪一环
        fail_reasons: dict[str, int] = {}
        for _latency, _speed, ok, note in self.plt_results.values():
            if ok:
                continue
            bucket = _plt_reason_bucket(note)
            fail_reasons[bucket] = fail_reasons.get(bucket, 0) + 1
        summary = {
            "total": self.plt_total,
            "success": ok_count,
            "failed": max(0, self.plt_total - ok_count),
            "average_latency": int(sum(latencies) / len(latencies)) if latencies else -1,
            "best_latency": min(latencies) if latencies else -1,
            "worst_latency": max(latencies) if latencies else -1,
            "mode": self.plt_mode,
            "error": pms_error,
            "fail_reasons": fail_reasons,
            # v1.8.0：stopped=True 表示「被中断，断点已保留」，pending 是还剩几个
            "stopped": False,
            "pending": pending,
        }
        reason_text = ""
        if fail_reasons:
            top = sorted(fail_reasons.items(), key=lambda item: (-item[1], item[0]))[:3]
            reason_text = "；失败原因：" + "、".join(f"{name} {count} 个" for name, count in top)
        self.plt_message.emit(
            f"测试完成：成功 {ok_count}/{self.plt_total}"
            + (f"，平均延迟 {summary['average_latency']} ms，最快 {summary['best_latency']} ms" if latencies else "")
            + reason_text
        )
        self.plt_finished.emit(summary)

    def plt_stop(self) -> None:
        """停止测试：保留已测结果与未完成清单（断点），下次可「继续上次测试」。

        v1.8.0 语义变化：原来是「丢弃一切」，现在是「暂停」——
        已完成的节点留在 done 里不重测，剩下的写进断点。
        ★ 停止那一刻**已经派发但还没回来**的任务结果会被丢弃（它们的 uid 仍在 pending），
        下次续跑会补测，不会算成「测过了」。
        """
        if not self.plt_running:
            return
        self.plt_running = False
        self.plt_chunk_queue = []
        self.plt_chunk_nodes = []
        self.plt_chunk_remaining = 0
        # v1.9.9：只 clear() 取消的是「排队中」的任务，已经在跑的不会停。
        # 顺序有讲究——先打断临时核心会话、再关批量核心（两者都会让阻塞中的请求失败），
        # 最后才等线程池收尾。原来跳过这两步，线程就一直卡在 IO 上，进程退不出去。
        aborted = self.plt_abort_sessions()
        if self.plt_mode == "clash":
            self.plt_close_clash_batch()
        elif self.plt_mode == "xray_batch":
            self.plt_close_xray_batch()
        self.plt_pool.clear()
        if (aborted or self.plt_mode) and not self.plt_pool.waitForDone(PMS_EXIT_POOL_WAIT_MS):
            self.plt_logger.warning(
                "延迟测试线程池在 %d ms 内未收尾（模式=%s，已打断 %d 个会话）", PMS_EXIT_POOL_WAIT_MS, self.plt_mode, aborted,
            )
        pending = self.plt_round._ptround_pending_count() if self.plt_round is not None else 0
        if self.plt_round is not None:
            self.plt_store._ptstore_save(self.plt_round)
            if self.plt_round._ptround_is_finished():
                self.plt_store._ptstore_clear()
                self.plt_round = None
        ok_count = sum(1 for item in self.plt_results.values() if item[2])
        self.plt_message.emit(
            f"已停止测试：保留已测结果 {ok_count} 个"
            + (f"，还剩 {pending} 个节点可继续（点「继续上次测试」续跑）" if pending else "，这一轮已经测完了")
        )
        self.plt_finished.emit({
            "total": self.plt_total, "success": ok_count,
            "failed": max(0, self.plt_total - ok_count),
            "average_latency": -1, "best_latency": -1, "worst_latency": -1,
            "mode": self.plt_mode, "error": "用户中止",
            "fail_reasons": {},
            "stopped": True, "pending": pending,
        })

