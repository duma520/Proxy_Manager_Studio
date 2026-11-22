#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电报 DC 测试调度（v1.9.59）

批量「Telegram 数据中心」测试调度：每个节点起一个临时核心（同测速机制），经它的本机代理
逐个探测五个生产 DC（TCP 可达 + MTProto `req_pq_multi→resPQ` 真握手 + 尽力而为的 HTTP 吞吐），
按并发限制实时上报进度；断点续跑沿用 `pms/testresume.py` 的多轮 KV（kind=`telegram_dc`）。

★ 为什么逐节点起临时核心：电报客户端连的是 DC 的 MTProto 端口，节点是 vmess/vless 等
  v2ray 协议、不是裸 HTTP 代理，urllib 的 ProxyHandler 塞不进去——只有把该节点跑成本机
  HTTP 代理（`PmsTempCoreSession`，与延迟/测速同一套）才能真正「通过这个节点连 DC」。
  这也天然满足用户要求的「让我知道连接哪个节点」：每行结果就是那一行节点的。

★ 速度口径（用户 2026-10-15 定死）：A（最快 DC 延迟）+ B（可达 DC 数）必做；
  C（HTTP 吞吐）尽力尝试，DC 端口不响应 HTTP 就**不显示速度，绝不编造数字**。

★ 公开 / 私有与启动器 400 行红线：只有主窗口真正持有的 `PmsTelegramTestCoordinator` 是公开类
  （需在启动器再导出，check_modules 第 [4] 项）；任务类与探测函数做成私有的——启动器实测
  399 行、余量只剩 1 行，多公开一个符号就要多挤一行再导出，直接破线。
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
    QRunnable,
    QThreadPool,
    Signal,
    Slot,
)
import logging
import threading

if TYPE_CHECKING:  # 仅类型检查，运行时不导入，避免循环依赖
    from pms.database import PmsDatabaseEngine

from pms.constants import (
    PMS_CORE_KIND_AUTO,
    PMS_EXIT_POOL_WAIT_MS,
    PMS_LOGGER_NAME,
)
from pms.paths import PmsRuntimePaths
from pms.models import PmsProxyNode
from pms.coresys import PmsCoreLocator
from pms.tasks import PmsTaskSignals
from pms.tempsession import PmsTempCoreSession
from pms.dcprobe import _pms_telegram_probe_dcs
from pms.testresume import (
    _PmsTestRound,
    _PmsTestRoundStore,
)

_PMS_TELEGRAM_ROUND_KIND = "telegram_dc"   # 断点轮次类型（test.round.<id> 里记的 kind）

class _PmsTelegramDcTask(QRunnable):
    """单节点电报 DC 测试：起临时核心 → 逐 DC 探测 → 汇报。

    ★ 私有类（下划线开头）：启动器顶 400 行红线（v1.9.59 实测 399），只有主窗口
      真正拿在手里用的 `PmsTelegramTestCoordinator` 保持公开并再导出；任务类只被
      本模块自己派发，公开它就得给启动器多挤一行，不值当。
    """

    def __init__(
        self,
        pms_signals: PmsTaskSignals,
        pms_settings: "PmsSettingsManager",
        pms_paths: PmsRuntimePaths,
        pms_core_kind: str,
        pms_core_path: Any,
        pms_node: PmsProxyNode,
        pms_coordinator: Optional[Any] = None,
    ) -> None:
        super().__init__()
        self.ptg_signals = pms_signals
        self.ptg_settings = pms_settings
        self.ptg_paths = pms_paths
        self.ptg_core_kind = pms_core_kind
        self.ptg_core_path = pms_core_path
        self.ptg_node = pms_node
        self.ptg_coordinator = pms_coordinator
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        session = PmsTempCoreSession(self.ptg_settings, self.ptg_paths, self.ptg_core_kind, self.ptg_core_path, self.ptg_node)
        coordinator = self.ptg_coordinator
        if coordinator is not None:
            coordinator.ptg_register_session(session)
        try:
            error = session.ptcs_open()
            if error:
                self.ptg_signals.ptask_node_done.emit(self.ptg_node.uid, -1, 0.0, False, error)
                return
            timeout = float(max(3, min(60, self.ptg_settings.psm_get_int("telegram.timeout_seconds", 20))))
            result = _pms_telegram_probe_dcs(
                "127.0.0.1", int(session.ptcs_port), timeout,
                pms_abort_event=session.ptcs_abort_event,
                pms_logger=logging.getLogger(PMS_LOGGER_NAME + ".dctest"),
            )
            # 走通用 ptask_node_done：uid / best_ms(当延迟) / speed_bps / ok / note。
            # best_dc / per_dc 等细节由协调器在本轮 _ptg_details 里按 uid 暂存后一并转发。
            if coordinator is not None:
                coordinator.ptg_store_details(self.ptg_node.uid, result)
            self.ptg_signals.ptask_node_done.emit(
                self.ptg_node.uid,
                int(result.get("best_ms", -1)),
                float(result.get("speed_bps", 0.0)),
                bool(result.get("ok", False)),
                str(result.get("note", "")),
            )
        finally:
            session.ptcs_close()
            if coordinator is not None:
                coordinator.ptg_unregister_session(session)

class PmsTelegramTestCoordinator(QObject):
    """批量电报 DC 测试调度：每个节点起临时核心，逐 DC 探测并按并发限制跑。"""

    ptt_progress = Signal(int, int, str)        # 已完成, 总数, 当前节点名
    ptt_node_result = Signal(str, dict)         # uid, 探测结果字典（含 best_dc/per_dc/speed_bps）
    ptt_finished = Signal(dict)
    ptt_message = Signal(str)

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_paths: PmsRuntimePaths,
        pms_locator: PmsCoreLocator,
        pms_parent: Optional[QObject] = None,
        pms_engine: Optional["PmsDatabaseEngine"] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.ptg_settings = pms_settings
        self.ptg_paths = pms_paths
        self.ptg_locator = pms_locator
        self.ptg_logger = logging.getLogger(PMS_LOGGER_NAME + ".dctest")
        self.ptg_pool = QThreadPool(self)
        self.ptg_signals = PmsTaskSignals(self)
        self.ptg_signals.ptask_node_done.connect(self.ptg_on_node_done)
        self.ptg_running = False
        self.ptg_total = 0
        self.ptg_done = 0
        self.ptg_results: dict[str, tuple[int, float, bool, str]] = {}
        self.ptg_details: dict[str, dict[str, Any]] = {}
        self.ptg_nodes: dict[str, PmsProxyNode] = {}
        self.ptg_store = _PmsTestRoundStore(pms_engine)
        # v1.9.59：本调度器的 store 只认电报这一类（读只列本类、清只清本类，
        # 电报跑完不能把用户留着续跑的延迟 / 测速断点抹掉）
        self.ptg_store._ptstore_kind = _PMS_TELEGRAM_ROUND_KIND
        self.ptg_round: Optional[_PmsTestRound] = None
        self.ptg_core_kind = ""
        self.ptg_core_path: Optional[Any] = None
        self.ptg_available: Optional[dict[str, Optional[Any]]] = None
        self.ptg_sessions: set[Any] = set()
        self.ptg_sessions_lock = threading.RLock()

    # ------------------------------------------------------------------ 会话登记（同测速 v1.9.9 口径）
    def ptg_register_session(self, pms_session: Any) -> None:
        with self.ptg_sessions_lock:
            self.ptg_sessions.add(pms_session)

    def ptg_unregister_session(self, pms_session: Any) -> None:
        with self.ptg_sessions_lock:
            self.ptg_sessions.discard(pms_session)

    def ptg_abort_sessions(self) -> int:
        """打断所有在跑的临时核心会话（退出 / 停止都要调，否则线程卡在 socket 上不回来）。"""
        with self.ptg_sessions_lock:
            sessions = list(self.ptg_sessions)
        for session in sessions:
            try:
                session.ptcs_abort()
            except Exception:  # noqa: BLE001 - 退出路径上单个会话失败不能连累其余
                continue
        return len(sessions)

    def ptg_is_running(self) -> bool:
        return self.ptg_running

    def ptg_store_details(self, pms_uid: str, pms_result: dict[str, Any]) -> None:
        """任务线程把完整探测字典交回协调器（信号只带精简五元组）。"""
        self.ptg_details[str(pms_uid)] = pms_result

    def ptg_start(self, pms_nodes: Sequence[PmsProxyNode], pms_scope: str = "selected") -> tuple[bool, str]:
        """开始批量电报 DC 测试（断点口径同测速 v1.9.10：scope 写进轮次）。"""
        if self.ptg_running:
            return False, "已有电报测试在进行中"
        nodes = [node for node in pms_nodes if node]
        if not nodes:
            return False, "没有可测试的节点"
        core_kind = self.ptg_locator.pcl_resolve_kind(self.ptg_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO), self.ptg_settings)
        if core_kind is None:
            return False, "未找到可用的核心程序"
        core_path = self.ptg_locator.pcl_find(core_kind, self.ptg_settings)
        if core_path is None:
            return False, "未找到可用的核心程序"
        concurrency = max(1, min(16, self.ptg_settings.psm_get_int("speed.concurrency", 4)))
        self.ptg_pool.setMaxThreadCount(concurrency)
        self.ptg_running = True
        self.ptg_total = len(nodes)
        self.ptg_done = 0
        self.ptg_results = {}
        self.ptg_details = {}
        self.ptg_nodes = {node.uid: node for node in nodes}
        self.ptg_core_kind = str(core_kind)
        self.ptg_core_path = core_path
        scope = str(pms_scope or "selected")
        if scope not in ("selected", "page", "all"):
            scope = "selected"
        self.ptg_round = _PmsTestRound(_PMS_TELEGRAM_ROUND_KIND, False, scope, [node.uid for node in nodes])
        self.ptt_message.emit(
            f"开始{'全部' if scope == 'all' else ('本页' if scope == 'page' else '')}电报 DC 测试 {len(nodes)} 个节点"
            f"（并发 {concurrency}，逐 DC：TCP + MTProto 握手）"
        )
        self.ptg_submit(nodes, None)
        self.ptg_store._ptstore_save(self.ptg_round)
        return True, f"已开始{'全部' if scope == 'all' else ('本页' if scope == 'page' else '')}电报测试 {len(nodes)} 个节点"

    def ptg_node_core(self, pms_node: PmsProxyNode) -> tuple[str, Optional[Any]]:
        """给这个节点选核心（同测速 v1.9.29：逐节点选、可用性缓存一次）。"""
        requested = self.ptg_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO)
        if self.ptg_available is None:
            self.ptg_available = self.ptg_locator.pcl_find_any(self.ptg_settings)
        kind = self.ptg_locator.pcl_resolve_kind_for_node(pms_node, requested, self.ptg_settings, self.ptg_available)
        if kind is None:
            return "", None
        return str(kind), self.ptg_available.get(str(kind))

    def ptg_submit(self, pms_nodes: Sequence[PmsProxyNode], pms_only_uids: Optional[set[str]]) -> None:
        if not self.ptg_core_kind:
            self.ptg_running = False
            self.ptg_finish()
            return
        for node in pms_nodes:
            if pms_only_uids is not None and node.uid not in pms_only_uids:
                continue
            kind, path = self.ptg_node_core(node)
            if not kind or path is None:
                continue
            self.ptg_pool.start(_PmsTelegramDcTask(
                self.ptg_signals, self.ptg_settings, self.ptg_paths, kind, path, node, self,
            ))

    def ptg_submit_retry(self, pms_uid: str) -> None:
        node = self.ptg_nodes.get(str(pms_uid))
        if node is None:
            return
        kind, path = self.ptg_node_core(node)
        if not kind or path is None:
            return
        self.ptg_pool.start(_PmsTelegramDcTask(
            self.ptg_signals, self.ptg_settings, self.ptg_paths, kind, path, node, self,
        ))

    def ptg_resume(self, pms_nodes: Sequence[PmsProxyNode], pms_round_id: str = "") -> tuple[bool, str]:
        """继续未完成的电报 DC 测试（语义同测速 pst_resume，v1.9.54 支持点名轮次）。"""
        if self.ptg_running:
            return False, "已有电报测试在进行中"
        round_obj = self.ptg_store._ptstore_load_round(pms_round_id)
        if round_obj is None:
            return False, "没有未完成的电报测试，无需续跑"
        if str(round_obj.kind) != _PMS_TELEGRAM_ROUND_KIND:
            return False, "上一次未完成的不是电报测试"
        nodes = [node for node in pms_nodes if node]
        if not nodes:
            return False, "没有可继续的节点"
        round_obj._ptround_drop_missing({node.uid for node in nodes})
        round_obj._ptround_add_new([node.uid for node in nodes])
        if round_obj._ptround_is_finished():
            self.ptg_store._ptstore_clear()
            return False, "上一次电报测试其实已经跑完了"
        pending_uids = set(round_obj.pending)
        target = [node for node in nodes if node.uid in pending_uids]
        if not target:
            self.ptg_store._ptstore_clear()
            return False, "断点里的节点都已经不存在了"
        core_kind = self.ptg_locator.pcl_resolve_kind(self.ptg_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO), self.ptg_settings)
        if core_kind is None:
            return False, "未找到可用的核心程序"
        core_path = self.ptg_locator.pcl_find(core_kind, self.ptg_settings)
        if core_path is None:
            return False, "未找到可用的核心程序"
        concurrency = max(1, min(16, self.ptg_settings.psm_get_int("speed.concurrency", 4)))
        self.ptg_pool.setMaxThreadCount(concurrency)
        self.ptg_core_kind = str(core_kind)
        self.ptg_core_path = core_path
        self.ptg_round = round_obj
        self.ptg_nodes = {node.uid: node for node in nodes}
        self.ptg_results = {}
        self.ptg_details = {}
        self.ptg_running = True
        self.ptg_total = max(len(target), int(round_obj.total))
        self.ptg_done = max(0, int(round_obj.total) - int(round_obj._ptround_pending_count()))
        self.ptt_message.emit(f"继续上次电报测试：还剩 {len(target)} 个节点（已完成 {self.ptg_done}/{self.ptg_total}，并发 {concurrency}）")
        self.ptg_submit(target, None)
        self.ptg_store._ptstore_save(round_obj)
        return True, f"继续上次电报测试：还剩 {len(target)} 个节点（已完成 {self.ptg_done}/{self.ptg_total}）"

    def ptg_pending_info(self) -> dict[str, Any]:
        """电报测试的断点信息（无断点返回空 dict）。"""
        round_obj = self.ptg_store._ptstore_load()
        if round_obj is None:
            return {}
        if str(round_obj.kind) != _PMS_TELEGRAM_ROUND_KIND:
            return {}
        return round_obj._ptround_info()

    def ptg_clear_round(self, pms_round_id: str = "") -> int:
        """丢弃电报断点（带 id 只丢那一轮；不带丢掉电报这一类全部）。"""
        if str(pms_round_id or "").strip():
            round_obj = self.ptg_store._ptstore_load_round(pms_round_id)
            if round_obj is None or str(round_obj.kind) != _PMS_TELEGRAM_ROUND_KIND:
                return 0
            return 1 if self.ptg_store._ptstore_drop_round(pms_round_id) else 0
        live = [item for item in self.ptg_store._ptstore_list() if str(item.get("kind")) == _PMS_TELEGRAM_ROUND_KIND]
        dropped = 0
        for item in live:
            dropped += 1 if self.ptg_store._ptstore_drop_round(str(item.get("round_id"))) else 0
        self.ptg_round = None
        return dropped

    def ptg_on_node_done(self, pms_uid: str, pms_latency: int, pms_speed: float, pms_success: bool, pms_note: str) -> None:
        """一个节点测完：记结果、出队；失败没到重试上限就回队重测（与测速同一口径）。"""
        if not self.ptg_running:
            return
        uid = str(pms_uid)
        self.ptg_results[uid] = (int(pms_latency), float(pms_speed), bool(pms_success), str(pms_note))
        node = self.ptg_nodes.get(uid)
        name = node.pnode_display_name() if node else uid
        self.ptt_node_result.emit(uid, dict(self.ptg_details.get(uid) or {
            "ok": bool(pms_success), "best_ms": int(pms_latency), "speed_bps": float(pms_speed), "note": str(pms_note),
        }))
        requeued = False
        if self.ptg_round is not None:
            requeued = self.ptg_round._ptround_mark_done(uid, bool(pms_success))
            self.ptg_store._ptstore_save_throttled(self.ptg_round)
        if requeued:
            self.ptg_submit_retry(uid)
            self.ptt_progress.emit(self.ptg_done, self.ptg_total, f"{name} 重试")
            return
        self.ptg_done += 1
        self.ptt_progress.emit(self.ptg_done, self.ptg_total, name)
        all_reported = self.ptg_done >= self.ptg_total
        if self.ptg_round is not None:
            if all_reported and self.ptg_round._ptround_is_finished():
                self.ptg_finish()
        elif all_reported:
            self.ptg_finish()

    def ptg_finish(self) -> None:
        """收尾：汇总；真跑完才清断点（停止由 ptg_stop 保留断点）。"""
        self.ptg_running = False
        pending = self.ptg_round._ptround_pending_count() if self.ptg_round is not None else 0
        if self.ptg_round is not None and self.ptg_round._ptround_is_finished():
            self.ptg_store._ptstore_clear()
            self.ptg_round = None
        success = sum(1 for item in self.ptg_results.values() if item[2])
        best_ms = min((item[0] for item in self.ptg_results.values() if item[2] and item[0] >= 0), default=-1)
        self.ptt_message.emit(
            f"电报测试完成：可连 {success}/{self.ptg_total}"
            + (f"，最快往返 {best_ms} ms" if best_ms >= 0 else "")
        )
        self.ptt_finished.emit({
            "total": self.ptg_total, "success": success, "failed": max(0, self.ptg_total - success),
            "best_ms": best_ms, "stopped": False, "pending": pending,
        })

    def ptg_stop(self) -> None:
        """停止电报测试：保留已测结果与断点（同测速 v1.8.0 / v1.9.9 口径）。"""
        if not self.ptg_running:
            return
        self.ptg_running = False
        aborted = self.ptg_abort_sessions()
        self.ptg_pool.clear()
        if aborted and not self.ptg_pool.waitForDone(PMS_EXIT_POOL_WAIT_MS):
            self.ptg_logger.warning("电报测试线程池在 %d ms 内未收尾（已打断 %d 个会话）", PMS_EXIT_POOL_WAIT_MS, aborted)
        pending = self.ptg_round._ptround_pending_count() if self.ptg_round is not None else 0
        if self.ptg_round is not None:
            self.ptg_store._ptstore_save(self.ptg_round)
            if self.ptg_round._ptround_is_finished():
                self.ptg_store._ptstore_clear()
                self.ptg_round = None
        success = sum(1 for item in self.ptg_results.values() if item[2])
        self.ptt_message.emit(
            f"已停止电报测试：保留已测结果 {success} 个"
            + (f"，还剩 {pending} 个节点可继续（点「继续上次测试」续跑）" if pending else "，这一轮已经测完了")
        )
        self.ptt_finished.emit({
            "total": self.ptg_total, "success": success, "failed": max(0, self.ptg_total - success),
            "best_ms": -1, "stopped": True, "pending": pending,
        })
