#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测速调度

批量下载测速调度：按并发限制为每个节点起临时核心，实时上报速率与进度。
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
    Signal,
)
import logging
import threading

if TYPE_CHECKING:  # 仅类型检查，运行时不导入，避免循环依赖
    from pms.database import PmsDatabaseEngine

from pms.constants import (
    PMS_CORE_KIND_AUTO,
    PMS_DEFAULT_SPEED_TEST_URL,
    PMS_EXIT_POOL_WAIT_MS,
    PMS_LOGGER_NAME,
    PMS_SPEED_SOURCE_CLOUDFLARE,
)
from pms.utils import (
    pms_format_speed,
    pms_resolve_speed_url,
)
from pms.paths import PmsRuntimePaths
from pms.models import PmsProxyNode
from pms.coresys import PmsCoreLocator
from pms.tasks import (
    PmsSpeedTestTask,
    PmsTaskSignals,
)
from pms.testresume import (
    _PmsTestRound,
    _PmsTestRoundStore,
)

class PmsSpeedTestCoordinator(QObject):
    """批量下载测速调度：每个节点起一个临时核心，按并发限制下载并计速。"""

    pst_progress = Signal(int, int, str, int)          # 已完成, 总数, 当前节点名, 当前字节数
    pst_node_result = Signal(str, float, int, bool, str)  # uid, 速率, 字节, 成功, 说明
    pst_finished = Signal(dict)
    pst_message = Signal(str)

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_paths: PmsRuntimePaths,
        pms_locator: PmsCoreLocator,
        pms_parent: Optional[QObject] = None,
        pms_engine: Optional["PmsDatabaseEngine"] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pst_settings = pms_settings
        self.pst_paths = pms_paths
        self.pst_locator = pms_locator
        self.pst_logger = logging.getLogger(PMS_LOGGER_NAME + ".speedtest")
        self.pst_pool = QThreadPool(self)
        self.pst_signals = PmsTaskSignals(self)
        self.pst_signals.ptask_node_done.connect(self.pst_on_node_done)
        self.pst_signals.ptask_progress.connect(self.pst_on_task_progress)
        self.pst_running = False
        self.pst_total = 0
        self.pst_done = 0
        self.pst_results: dict[str, tuple[float, int, bool, str]] = {}
        self.pst_nodes: dict[str, PmsProxyNode] = {}
        # v1.8.0 断点续跑：延迟测试与测速共用 pms_meta 里同一条 KV（同时只存在一个断点）
        self.pst_store = _PmsTestRoundStore(pms_engine)
        self.pst_round: Optional[_PmsTestRound] = None
        # v1.9.9：失败重试要重新派发任务，得记住本轮的核心与测速参数
        self.pst_core_kind = ""
        self.pst_core_path: Optional[Any] = None
        # v1.9.29：逐节点选核心时缓存「核心可用性」，免得每个节点都重搜一遍核心目录
        self.pst_available: Optional[dict[str, Optional[Path]]] = None
        self.pst_config: dict[str, Any] = {}
        # v1.9.9：正在跑的临时核心会话。停止时逐个打断，
        # 否则线程卡在 read() 上不回来，Python 进程退不出去。
        self.pst_sessions: set[Any] = set()
        self.pst_sessions_lock = threading.RLock()

    # ------------------------------------------------------------------ 会话登记（v1.9.9）
    def pst_register_session(self, pms_session: Any) -> None:
        """任务开跑时登记自己的测量会话。"""
        with self.pst_sessions_lock:
            self.pst_sessions.add(pms_session)

    def pst_unregister_session(self, pms_session: Any) -> None:
        """任务收尾时注销（discard 而非 remove：重复注销不能抛）。"""
        with self.pst_sessions_lock:
            self.pst_sessions.discard(pms_session)

    def pst_abort_sessions(self) -> int:
        """打断所有还在跑的测量会话，返回打断的个数。"""
        with self.pst_sessions_lock:
            sessions = list(self.pst_sessions)
        for session in sessions:
            try:
                session.ptcs_abort()
            except Exception:  # noqa: BLE001 - 退出路径上单个会话失败不能连累其余
                continue
        return len(sessions)

    def pst_is_running(self) -> bool:
        return self.pst_running

    def pst_start(self, pms_nodes: Sequence[PmsProxyNode], pms_scope: str = "selected") -> tuple[bool, str]:
        """开始批量测速。

        v1.9.10：`pms_scope`（selected / page / all）会写进断点，
        「继续上次测试」才能按同一范围重新取节点——否则全量测速中断后续跑
        会退化成只跑选中行，剩下的永远补不回来。
        """
        if self.pst_running:
            return False, "已有测速在进行中"
        nodes = [node for node in pms_nodes if node]
        if not nodes:
            return False, "没有可测速的节点"
        core_kind = self.pst_locator.pcl_resolve_kind(self.pst_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO), self.pst_settings)
        if core_kind is None:
            return False, "未找到可用的核心程序"
        core_path = self.pst_locator.pcl_find(core_kind, self.pst_settings)
        if core_path is None:
            return False, "未找到可用的核心程序"
        concurrency = max(1, min(16, self.pst_settings.psm_get_int("speed.concurrency", 4)))
        self.pst_pool.setMaxThreadCount(concurrency)
        self.pst_running = True
        self.pst_total = len(nodes)
        self.pst_done = 0
        self.pst_results = {}
        self.pst_nodes = {node.uid: node for node in nodes}
        self.pst_core_kind = str(core_kind)
        self.pst_core_path = core_path
        self.pst_config = self.pst_build_config()
        # v1.8.0 断点：pending = 全部待测 uid（同一时刻只允许一个断点，旧的会被覆盖）
        # v1.9.10：记下范围，续跑按同一范围取节点
        scope = str(pms_scope or "selected")
        if scope not in ("selected", "page", "all"):
            scope = "selected"
        self.pst_round = _PmsTestRound("speed", False, scope, [node.uid for node in nodes])
        self.pst_message.emit(
            f"开始{'全部' if scope == 'all' else ('本页' if scope == 'page' else '')}测速 {len(nodes)} 个节点"
            f"（并发 {concurrency}，单次 {self.pst_config['size_mb']} MB，"
            f"单节点上限 {self.pst_config['limit_mb']} MB / {self.pst_config['duration_seconds']} 秒）"
        )
        self.pst_submit(nodes, None)
        self.pst_store._ptstore_save(self.pst_round)
        return True, f"已开始{'全部' if scope == 'all' else ('本页' if scope == 'page' else '')}测速 {len(nodes)} 个节点"

    def pst_build_config(self) -> dict[str, Any]:
        """构造本轮测速参数（重试时要复用同一份）。"""
        limit_mb = self.pst_settings.psm_get_int("speed.per_node_limit_mb", 30)
        size_mb = self.pst_settings.psm_get_int("speed.size_mb", 10)
        config: dict[str, Any] = {
            "url": pms_resolve_speed_url(
                self.pst_settings.psm_get_str("speed.source", PMS_SPEED_SOURCE_CLOUDFLARE),
                size_mb,
                self.pst_settings.psm_get_str("speed.url", PMS_DEFAULT_SPEED_TEST_URL),
            ),
            "duration_seconds": self.pst_settings.psm_get_int("speed.duration_seconds", 10),
            "limit_bytes": int(limit_mb) * 1024 * 1024,
            # v1.9.16：采样次数（原来这个设置项没人读）
            "min_samples": self.pst_settings.psm_get_int("speed.min_samples", 3),
        }
        config["size_mb"] = size_mb
        config["limit_mb"] = limit_mb
        return config

    def pst_node_core(self, pms_node: PmsProxyNode) -> tuple[str, Optional[Path]]:
        """给这个节点选核心（v1.9.29）：`auto` = 优先 sing-box，它载不动就兜底 Xray。

        测速本来就是「每个节点起一个临时核心」，所以逐节点换核心不需要额外成本。
        ★ 核心可用性**每个节点只查一次**（`pst_available` 缓存）——逐节点重搜核心目录实测 28 ms，
        几万个节点会白等十几分钟。
        """
        requested = self.pst_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO)
        if self.pst_available is None:
            self.pst_available = self.pst_locator.pcl_find_any(self.pst_settings)
        kind = self.pst_locator.pcl_resolve_kind_for_node(pms_node, requested, self.pst_settings, self.pst_available)
        if kind is None:
            return "", None
        return str(kind), self.pst_available.get(str(kind))

    def pst_submit(self, pms_nodes: Sequence[PmsProxyNode], pms_only_uids: Optional[set[str]] = None) -> None:
        """按并发限制派发测速任务；``pms_only_uids`` 不为空时只派这些（续跑用）。

        v1.9.29：**每个节点各自选核心**，所以一条测速队列里可以混着 sing-box 与 Xray 的任务。
        """
        if not self.pst_core_kind:
            self.pst_running = False
            self.pst_finish()
            return
        for node in pms_nodes:
            if pms_only_uids is not None and node.uid not in pms_only_uids:
                continue
            kind, path = self.pst_node_core(node)
            if not kind or path is None:
                continue
            self.pst_pool.start(PmsSpeedTestTask(
                self.pst_signals, self.pst_settings, self.pst_paths, kind, path, node, self.pst_config,
                self,   # v1.9.9：把调度器交给任务，停止时才能打断它
            ))

    def pst_resume(self, pms_nodes: Sequence[PmsProxyNode], pms_round_id: str = "") -> tuple[bool, str]:
        """继续未完成的测速（断点续跑），语义同 ``PmsLatencyTestCoordinator.plt_resume``。

        v1.9.54：`pms_round_id` 指定续哪一轮；留空 = 最新一轮。
        """
        if self.pst_running:
            return False, "已有测速在进行中"
        round_obj = self.pst_store._ptstore_load_round(pms_round_id)
        if round_obj is None:
            return False, "没有未完成的测试，无需续跑"
        if str(round_obj.kind) != "speed":
            return False, "上一次未完成的是「延迟测试」，请用延迟测试的续跑入口"
        nodes = [node for node in pms_nodes if node]
        if not nodes:
            return False, "没有可继续测速的节点"
        round_obj._ptround_drop_missing({node.uid for node in nodes})
        round_obj._ptround_add_new([node.uid for node in nodes])
        if round_obj._ptround_is_finished():
            self.pst_store._ptstore_clear()
            return False, "上一次测速其实已经跑完了"
        pending_uids = set(round_obj.pending)
        target = [node for node in nodes if node.uid in pending_uids]
        if not target:
            self.pst_store._ptstore_clear()
            return False, "断点里的节点都已经不存在了"
        core_kind = self.pst_locator.pcl_resolve_kind(self.pst_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO), self.pst_settings)
        if core_kind is None:
            return False, "未找到可用的核心程序"
        core_path = self.pst_locator.pcl_find(core_kind, self.pst_settings)
        if core_path is None:
            return False, "未找到可用的核心程序"
        concurrency = max(1, min(16, self.pst_settings.psm_get_int("speed.concurrency", 4)))
        self.pst_pool.setMaxThreadCount(concurrency)
        self.pst_core_kind = str(core_kind)
        self.pst_core_path = core_path
        self.pst_config = self.pst_build_config()
        self.pst_round = round_obj
        self.pst_nodes = {node.uid: node for node in nodes}
        self.pst_results = {}
        self.pst_running = True
        self.pst_total = max(len(target), int(round_obj.total))
        self.pst_done = max(0, int(round_obj.total) - int(round_obj._ptround_pending_count()))
        self.pst_message.emit(
            f"继续上次测速：还剩 {len(target)} 个节点（已完成 {self.pst_done}/{self.pst_total}，并发 {concurrency}）"
        )
        self.pst_submit(target, None)
        self.pst_store._ptstore_save(round_obj)
        return True, f"继续上次测速：还剩 {len(target)} 个节点（已完成 {self.pst_done}/{self.pst_total}）"

    def pst_pending_info(self) -> dict[str, Any]:
        """断点信息（无断点返回空 dict）。"""
        round_obj = self.pst_store._ptstore_load()
        if round_obj is None:
            return {}
        return round_obj._ptround_info()

    def pst_clear_round(self, pms_round_id: str = "") -> int:
        """丢弃断点。

        v1.9.54：带 `pms_round_id` 时只丢那一轮（返回 1/0）；不带时丢掉测速这一类全部断点。
        """
        if str(pms_round_id or "").strip():
            return 1 if self.pst_store._ptstore_drop_round(pms_round_id) else 0
        self.pst_round = None
        self.pst_store._ptstore_clear()
        return 1

    def pst_on_task_progress(self, pms_uid: str, pms_bytes: int, pms_elapsed_ms: int) -> None:
        if not self.pst_running:
            return
        node = self.pst_nodes.get(str(pms_uid))
        name = node.pnode_display_name() if node else str(pms_uid)
        self.pst_progress.emit(self.pst_done, self.pst_total, name, int(pms_bytes))

    def pst_on_node_done(self, pms_uid: str, pms_latency: int, pms_speed: float, pms_success: bool, pms_note: str) -> None:
        """一个节点测完：记结果、出队；失败且没到重试上限就回队重测一次（v1.8.0）。"""
        if not self.pst_running:
            return
        uid = str(pms_uid)
        self.pst_results[uid] = (float(pms_speed), int(pms_latency), bool(pms_success), str(pms_note))
        node = self.pst_nodes.get(uid)
        name = node.pnode_display_name() if node else uid
        self.pst_node_result.emit(uid, float(pms_speed), int(pms_latency), bool(pms_success), str(pms_note))
        requeued = False
        if self.pst_round is not None:
            requeued = self.pst_round._ptround_mark_done(uid, bool(pms_success))
            self.pst_store._ptstore_save_throttled(self.pst_round)
        if requeued:
            self.pst_submit_retry(uid)
            self.pst_progress.emit(self.pst_done, self.pst_total, f"{name} 重试", 0)
            return
        self.pst_done += 1
        self.pst_progress.emit(self.pst_done, self.pst_total, name, 0)
        # v1.9.27：与延迟测试同一口径——只看断点 pending 会在断点被截断时误判「测完了」
        all_reported = self.pst_done >= self.pst_total
        if self.pst_round is not None:
            if all_reported and self.pst_round._ptround_is_finished():
                self.pst_finish()
        elif all_reported:
            self.pst_finish()

    def pst_submit_retry(self, pms_uid: str) -> None:
        """把一个失败的节点重新排进线程池（用**它自己**的核心）。"""
        node = self.pst_nodes.get(str(pms_uid))
        if node is None:
            return
        kind, path = self.pst_node_core(node)
        if not kind or path is None:
            return
        self.pst_pool.start(PmsSpeedTestTask(
            self.pst_signals, self.pst_settings, self.pst_paths, kind, path, node, self.pst_config,
            self,   # v1.9.9：同上
        ))

    def pst_finish(self) -> None:
        """收尾：汇总结果；**真的跑完**才清断点（停止由 pst_stop 保留断点）。"""
        self.pst_running = False
        pending = self.pst_round._ptround_pending_count() if self.pst_round is not None else 0
        if self.pst_round is not None and self.pst_round._ptround_is_finished():
            self.pst_store._ptstore_clear()
            self.pst_round = None
        speeds = [item[0] for item in self.pst_results.values() if item[2] and item[0] > 0]
        success = sum(1 for item in self.pst_results.values() if item[2])
        summary = {
            "total": self.pst_total,
            "success": success,
            "failed": sum(1 for item in self.pst_results.values() if not item[2]),
            "average_speed": sum(speeds) / len(speeds) if speeds else 0.0,
            "best_speed": max(speeds) if speeds else 0.0,
            "stopped": False,
            "pending": pending,
            # v1.9.33：本次测速测过的 uid（「测速后切到最快节点」按这个范围挑候选）
            "uids": [str(uid) for uid in self.pst_results],
        }
        self.pst_message.emit(
            f"测速完成：成功 {summary['success']}/{self.pst_total}"
            + (f"，平均 {pms_format_speed(summary['average_speed'])}，最快 {pms_format_speed(summary['best_speed'])}" if speeds else "")
        )
        self.pst_finished.emit(summary)

    def pst_stop(self) -> None:
        """停止测速：保留已测结果与未完成清单（断点），下次可「继续上次测试」（v1.8.0）。"""
        if not self.pst_running:
            return
        self.pst_running = False
        # v1.9.9：只 clear() 是取消「排队中」的任务，已经在跑的不会停。
        # 必须先把它们的会话打断，再等线程池收尾，否则线程卡在 IO 上，进程退不出去。
        aborted = self.pst_abort_sessions()
        self.pst_pool.clear()
        if aborted and not self.pst_pool.waitForDone(PMS_EXIT_POOL_WAIT_MS):
            self.pst_logger.warning("测速线程池在 %d ms 内未收尾（已打断 %d 个会话）", PMS_EXIT_POOL_WAIT_MS, aborted)
        pending = self.pst_round._ptround_pending_count() if self.pst_round is not None else 0
        if self.pst_round is not None:
            self.pst_store._ptstore_save(self.pst_round)
            if self.pst_round._ptround_is_finished():
                self.pst_store._ptstore_clear()
                self.pst_round = None
        success = sum(1 for item in self.pst_results.values() if item[2])
        self.pst_message.emit(
            f"已停止测速：保留已测结果 {success} 个"
            + (f"，还剩 {pending} 个节点可继续（点「继续上次测试」续跑）" if pending else "，这一轮已经测完了")
        )
        self.pst_finished.emit({
            "total": self.pst_total, "success": success, "failed": max(0, self.pst_total - success),
            "average_speed": 0.0, "best_speed": 0.0, "stopped": True, "pending": pending,
        })
