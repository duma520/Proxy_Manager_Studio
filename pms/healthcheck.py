#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""节点健康检查（v1.9.37）

和「订阅」没有任何关系，也不是 `autoswitch` 那套「定时探测当前节点、失败就切换」——
这里做的是**全库周期体检**：

    连续失败到阈值 → 先做一次**真连接确认** → 真的不行才自动禁用；
    被自己禁用的节点，下个周期重新纳入探测，**真连接可用**就自动复活并清零计数。

三条硬约束（用户点名要的，后续改动不得绕过）：

    1. **用户手动禁用的节点永远不参与**：候选口径 = `enabled=1 OR health_auto_disabled=1`
       （写死在 `PmsNodeRepository.pnr_health_targets` 的 SQL 里）。用户手动启用 / 禁用会
       一起清掉健康状态（`pnr_set_enabled_manual`），所以不会出现「手动禁用的节点被自动复活」。
    2. **TCP 握手只当初筛**：自动禁用与自动复活都必须以**真连接成功**为准，
       真连接走既有的延迟测试批量通道（Clash API 快通道 / Xray 单核多出站 / 临时核心）。
    3. **不另起调度器、不抢手动测试资源**：真连接探测直接复用 `PmsLatencyTestCoordinator`
       （分片、逐节点选核心、`test.retry_times` 重试全部继承），并发用 `pms_max_threads` 封顶；
       发现手动延迟测试 / 测速在跑就让路，下一拍再说。

调度与续跑（几万节点也能扛）：

    · 一个周期 = 「把全库候选节点滚动扫一遍」。每拍（默认 60 秒）最多探测
      `health.batch_size` 个**到点**的节点（`health_next_check_at <= now`），
      所以永远不会出现「几万个节点同时发请求」。
    · 每个节点探完就把 `health_next_check_at` 推到**下个周期**，
      所以中途关软件 / 被手动测试挤掉都不会丢进度——没探到的节点仍然是「到点」状态，
      下一拍接着来（这就是断点续跑，不需要另存断点文件）。
    · **首轮只建周期**（用户拍板）：第一次启用时不立刻探测，而是把 `health_next_check_at`
      统一推到「现在 + 周期（±抖动）」，7 天后才开始第一轮实际探测。
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import QObject, QThreadPool, Signal

from pms.constants import PMS_LOGGER_NAME
from pms.tasks import PmsTcpPingSignals, PmsTcpPingTask

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------
PMS_HEALTH_META_KEY = "health.plan"          # 周期计划（首次初始化标记 + 上次检查时间）
PMS_HEALTH_STAGE_IDLE = ""
PMS_HEALTH_STAGE_TCP = "tcp"                 # 初筛：TCP 握手
PMS_HEALTH_STAGE_PROBE = "probe"             # 真连接探测（也是「自动复活」的判定）
PMS_HEALTH_STAGE_CONFIRM = "confirm"         # 阈值后的真连接确认（只有它失败才禁用）
PMS_HEALTH_MIN_PERIOD_DAYS = 1
PMS_HEALTH_MAX_PERIOD_DAYS = 30
PMS_HEALTH_MIN_THRESHOLD = 1
PMS_HEALTH_MAX_THRESHOLD = 10
PMS_HEALTH_MIN_BATCH = 50
PMS_HEALTH_MAX_BATCH = 5000
PMS_HEALTH_MAX_JITTER_MINUTES = 1440
PMS_HEALTH_TICK_MS = 60000                   # 每拍检查一次「有没有到点的节点」

_logger = logging.getLogger(PMS_LOGGER_NAME + ".health")


def pms_health_text(pms_moment: datetime) -> str:
    """与库里其它时间字段同格式的文本（字符串比较即可当时间比较用）。"""
    return pms_moment.strftime("%Y-%m-%d %H:%M:%S")


def pms_health_parse(pms_text: str) -> Optional[datetime]:
    """解析库里的时间文本；解析不出来返回 None。"""
    text = str(pms_text or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def pms_health_managed(pms_node: Any) -> bool:
    """这个节点参不参与健康检查（验收标准第 1 条：用户手动禁用的永不参与）。"""
    return bool(getattr(pms_node, "enabled", True)) or bool(getattr(pms_node, "health_auto_disabled", False))


class PmsHealthCheckService(QObject):
    """节点健康检查服务：周期体检 → 自动禁用 / 自动复活。"""

    phs_status = Signal(str)            # 状态栏 / 托盘提示文本（进度也走它）
    phs_progress = Signal(int, int)     # 本批已完成 / 本批总数
    phs_summary = Signal(dict)          # 一批结束的汇总
    phs_notice = Signal(str, str)       # 标题, 正文 —— 「通知一次」由界面接（托盘气泡）
    phs_log = Signal(str)               # 写进主程序日志面板

    def __init__(
        self,
        pms_settings: Any,
        pms_repo: Any,
        pms_latency: Any,
        pms_engine: Any = None,
        pms_parent: Optional[QObject] = None,
        pms_busy_provider: Optional[Callable[[], bool]] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.phc_settings = pms_settings
        self.phc_repo = pms_repo
        self.phc_latency = pms_latency
        self.phc_engine = pms_engine
        self.phc_busy_provider = pms_busy_provider
        self.phc_pool = QThreadPool(self)
        self.phc_tcp_signals = PmsTcpPingSignals(self)
        self.phc_tcp_signals.ptcp_result.connect(self.phs_on_tcp_result)
        # 真连接结果与轮次结束都从既有的延迟测试协调器来（不另起调度器）
        if self.phc_latency is not None:
            self.phc_latency.plt_node_result.connect(self.phs_on_node_result)
            self.phc_latency.plt_finished.connect(self.phs_on_round_finished)
        self.phc_stage = PMS_HEALTH_STAGE_IDLE
        self.phc_batch: dict[str, Any] = {}          # uid → 节点（本批正在处理的）
        self.phc_tcp_ok: set[str] = set()             # 初筛通过的 uid
        self.phc_tcp_bad: dict[str, str] = {}         # 初筛失败的 uid → 原因
        self.phc_tcp_done = 0
        self.phc_probe_results: dict[str, tuple[bool, str]] = {}
        self.phc_updates: dict[str, dict[str, Any]] = {}
        self.phc_disabled: list[str] = []
        self.phc_revived: list[str] = []
        self.phc_checked = 0
        self.phc_next_due = ""
        self.phc_only_auto = False
        self.phc_forced = False                       # 「立即运行一次」强制绕过节流
        self.phc_preempted = False                    # 被手动测试挤掉过
        self.phc_stage_total = 0

    # ------------------------------------------------------------------ 配置
    def phs_is_enabled(self) -> bool:
        return self.phc_settings.psm_get_bool("health.enabled", True)

    def phs_period_days(self) -> int:
        value = self.phc_settings.psm_get_int("health.period_days", 7)
        return max(PMS_HEALTH_MIN_PERIOD_DAYS, min(PMS_HEALTH_MAX_PERIOD_DAYS, int(value or 0) or 7))

    def phs_fail_threshold(self) -> int:
        value = self.phc_settings.psm_get_int("health.fail_threshold", 3)
        return max(PMS_HEALTH_MIN_THRESHOLD, min(PMS_HEALTH_MAX_THRESHOLD, int(value or 0) or 3))

    def phs_batch_size(self) -> int:
        value = self.phc_settings.psm_get_int("health.batch_size", 300)
        return max(PMS_HEALTH_MIN_BATCH, min(PMS_HEALTH_MAX_BATCH, int(value or 0) or 300))

    def phs_jitter_minutes(self) -> int:
        value = self.phc_settings.psm_get_int("health.jitter_minutes", 30)
        return max(0, min(PMS_HEALTH_MAX_JITTER_MINUTES, int(value or 0)))

    def phs_max_threads(self) -> int:
        """本功能的并发上限：手动测试的 1/4，夹在 2~8 —— 低优先级，不跟手动测试抢资源。"""
        manual = max(1, self.phc_settings.psm_get_int("test.concurrency", 8))
        return max(2, min(8, manual // 4 or 2))

    def phs_is_running(self) -> bool:
        return self.phc_stage != PMS_HEALTH_STAGE_IDLE

    def phs_stage_text(self) -> str:
        return {
            PMS_HEALTH_STAGE_TCP: "TCP 初筛",
            PMS_HEALTH_STAGE_PROBE: "真连接探测",
            PMS_HEALTH_STAGE_CONFIRM: "真连接确认",
        }.get(self.phc_stage, "空闲")

    def phs_status_text(self) -> str:
        if not self.phs_is_enabled():
            return "节点健康检查：已暂停"
        if not self.phs_is_running():
            return "节点健康检查：待命"
        total = len(self.phc_batch) or 0
        done = self.phc_checked if self.phc_stage != PMS_HEALTH_STAGE_TCP else self.phc_tcp_done
        return f"节点健康检查：{self.phs_stage_text()} {done}/{self.phc_stage_total or total}（自动禁用 {len(self.phc_disabled)}，复活 {len(self.phc_revived)}）"

    # ------------------------------------------------------------------ 控制入口
    def phs_pause(self) -> str:
        """暂停健康检查（写设置，设置页能看到开关状态）。"""
        self.phs_stop()
        self.phc_settings.psm_set("health.enabled", False, pms_emit=False)
        self.phs_status.emit(self.phs_status_text())
        return "节点健康检查已暂停（设置 → 测试 → 节点健康检查 里可以重新打开）"

    def phs_resume(self, pms_reschedule: bool = True) -> str:
        """继续健康检查；按用户要求：重新启用时把周期重新铺一遍（不做立即探测）。"""
        self.phc_settings.psm_set("health.enabled", True, pms_emit=False)
        if pms_reschedule:
            count = self.phs_initialize_cycle(pms_force=True)
            self.phs_status.emit(self.phs_status_text())
            return f"节点健康检查已启用：本轮只建周期，{count} 个节点将在 {self.phs_next_due_text()} 前后开始探测"
        return "节点健康检查已启用"

    def phs_run_now(self, pms_only_auto: bool = False) -> str:
        """立即运行一次（可只查被自动禁用的节点）。等当前这批跑完再点会直接排下一拍。"""
        if self.phc_latency is not None and self.phc_latency.plt_is_running():
            return "延迟测试正在跑，健康检查等它结束（它优先级更高）"
        if self.phs_is_running():
            return f"上一批还在跑（{self.phs_stage_text()}），跑完会自动接着来"
        self.phc_forced = True
        self.phc_only_auto = bool(pms_only_auto)
        started = self.phs_start_batch()
        if not started:
            self.phs_status.emit(self.phs_status_text())
            return "当前没有需要检查的节点（用户手动禁用的节点永远不检查）"
        return f"已开始健康检查（{self.phs_stage_text()}，本批 {len(self.phc_batch)} 个节点）"

    def phs_stop(self) -> None:
        """停掉当前这一批（手动测试要开始时让路）。没跑完的节点仍然「到点」，下一拍接着来。"""
        if not self.phs_is_running():
            return
        self.phc_pool.clear()
        # 只有「真连接」两个阶段才占用着协调器；TCP 阶段动它的 stop 会把用户的手动测试掐掉
        if self.phc_stage in (PMS_HEALTH_STAGE_PROBE, PMS_HEALTH_STAGE_CONFIRM) \
                and self.phc_latency is not None and self.phc_latency.plt_is_running():
            try:
                self.phc_latency.plt_stop()
            except Exception as exc:  # noqa: BLE001 - 让路失败不能连累调用方
                _logger.warning("健康检查让路时停止探测失败：%s", exc)
        self.phc_preempted = True
        self.phs_flush_updates()
        self.phc_stage = PMS_HEALTH_STAGE_IDLE
        self.phc_batch = {}
        self.phc_tcp_ok = set()
        self.phc_tcp_bad = {}
        self.phc_probe_results = {}

    def phs_set_busy_provider(self, pms_provider: Optional[Callable[[], bool]]) -> None:
        """由界面注入「手动测试 / 测速在跑吗」的探针。"""
        self.phc_busy_provider = pms_provider

    def phs_counts(self) -> dict[str, int]:
        try:
            return dict(self.phc_repo.pnr_health_counts(pms_due_before=pms_health_text(datetime.now())))
        except Exception as exc:  # noqa: BLE001 - 统计失败不能影响主流程
            _logger.warning("健康检查概况统计失败：%s", exc)
            return {}

    # ------------------------------------------------------------------ 周期与调度
    def phs_plan(self) -> dict[str, Any]:
        if self.phc_engine is None:
            return {}
        try:
            raw = self.phc_engine.pdb_get_meta(PMS_HEALTH_META_KEY, "")
            return dict(json.loads(raw)) if raw else {}
        except Exception:  # noqa: BLE001
            return {}

    def phs_save_plan(self, pms_plan: dict[str, Any]) -> None:
        if self.phc_engine is None:
            return
        try:
            self.phc_engine.pdb_set_meta(PMS_HEALTH_META_KEY, json.dumps(pms_plan, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("健康检查周期计划落盘失败：%s", exc)

    def phs_next_due_text(self) -> str:
        moment = pms_health_parse(self.phc_next_due)
        return moment.strftime("%Y-%m-%d %H:%M") if moment else "（未排期）"

    def phs_due_at(self, pms_moment: Optional[datetime] = None) -> str:
        """下个周期的到点时间：现在 + 周期天数 ± 抖动（避免全库同一刻集中请求）。"""
        base = (pms_moment or datetime.now()) + timedelta(days=self.phs_period_days())
        jitter = self.phs_jitter_minutes()
        if jitter > 0:
            base += timedelta(minutes=random.randint(-jitter, jitter))
        return pms_health_text(base)

    def phs_initialize_cycle(self, pms_force: bool = False) -> int:
        """**首轮只建周期**：把所有候选节点的下次检查时间铺到下个周期，不立刻探测。

        返回铺设的节点数；`pms_force=True` 时不看初始化标记（用户重新打开开关时重铺）。
        """
        if self.phc_engine is None:
            return 0
        plan = self.phs_plan()
        if plan.get("initialized") and not pms_force:
            return 0
        self.phc_next_due = self.phs_due_at()
        try:
            row = self.phc_engine.pdb_query_one(
                "SELECT COUNT(*) FROM pms_nodes WHERE (enabled=1 OR health_auto_disabled=1)")
            count = int(row[0]) if row is not None else 0
        except Exception as exc:  # noqa: BLE001
            _logger.warning("健康检查统计候选节点失败：%s", exc)
            count = 0
        # 一次 UPDATE 铺完全库（节点自己的抖动由「到点扫描 + 每拍限量」保证，不会集中请求）
        try:
            self.phc_engine.pdb_execute(
                "UPDATE pms_nodes SET health_next_check_at=? WHERE (enabled=1 OR health_auto_disabled=1)",
                (self.phc_next_due,),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("健康检查铺设周期失败：%s", exc)
            return 0
        plan.update({
            "initialized": True, "period_days": self.phs_period_days(),
            "initialized_at": pms_health_text(datetime.now()), "jitter_minutes": self.phs_jitter_minutes(),
        })
        self.phs_save_plan(plan)
        self.phs_log.emit(
            f"节点健康检查：已建周期（{count} 个节点），首轮探测从 {self.phs_next_due_text()} 前后开始"
        )
        return int(count or 0)

    def phs_on_tick(self) -> None:
        """定时驱动（由主窗口 60 秒一次的定时器调用）。"""
        if not self.phs_is_enabled():
            return
        if self.phs_is_running():
            return
        if self.phs_busy():
            self.phc_preempted = True
            return
        if not self.phs_plan().get("initialized"):
            self.phs_initialize_cycle()
            return
        self.phc_only_auto = False
        self.phs_start_batch()

    def phs_busy(self) -> bool:
        """手动测试 / 测速在跑吗（在跑就让路 —— 它们是用户显式发起的，优先级更高）。"""
        if self.phc_busy_provider is not None:
            try:
                if self.phc_busy_provider():
                    return True
            except Exception:  # noqa: BLE001
                pass
        if self.phc_latency is not None and self.phc_latency.plt_is_running():
            return True
        return False

    # ------------------------------------------------------------------ 一批的执行
    def phs_start_batch(self) -> bool:
        due_before = "" if self.phc_forced else pms_health_text(datetime.now())
        nodes = list(self.phc_repo.pnr_health_targets(
            pms_only_auto=self.phc_only_auto, pms_due_before=due_before, pms_limit=self.phs_batch_size()))
        if not nodes:
            self.phc_forced = False
            return False
        self.phc_batch = {str(node.uid): node for node in nodes}
        self.phc_tcp_ok = set()
        self.phc_tcp_bad = {}
        self.phc_tcp_done = 0
        self.phc_probe_results = {}
        self.phc_updates = {}
        self.phc_disabled = []
        self.phc_revived = []
        self.phc_checked = 0
        self.phc_preempted = False
        self.phc_next_due = self.phs_due_at()
        self.phc_stage_total = len(nodes)
        self.phc_stage = PMS_HEALTH_STAGE_TCP
        self.phs_status.emit(self.phs_status_text())
        self.phs_log.emit(
            f"节点健康检查：开始一批（{len(nodes)} 个节点，{self.phs_stage_text()}，并发 {self.phs_max_threads()}）"
        )
        timeout_ms = max(500, self.phc_settings.psm_get_int("test.tcp_timeout_ms", 3000))
        self.phc_pool.clear()
        self.phc_pool.setMaxThreadCount(self.phs_max_threads())
        for node in nodes:
            self.phc_pool.start(PmsTcpPingTask(node, timeout_ms, self.phc_tcp_signals))
        return True

    def phs_on_tcp_result(self, pms_uid: str, pms_latency: int, pms_error: str) -> None:
        """TCP 初筛结果：只用来筛掉明显连不上的（真连接才作数）。"""
        if self.phc_stage != PMS_HEALTH_STAGE_TCP:
            return
        uid = str(pms_uid)
        if uid not in self.phc_batch:
            return
        node = self.phc_batch[uid]
        self.phc_tcp_done += 1
        self.phs_progress.emit(self.phc_tcp_done, len(self.phc_batch))
        if pms_latency >= 0:
            self.phc_tcp_ok.add(uid)
        else:
            self.phc_tcp_bad[uid] = str(pms_error or "TCP 握手失败")
            # 已经「自动禁用」的节点：初筛不过 → 保持禁用，顺延到下个周期（**不做复活判定**）
            if bool(getattr(node, "health_auto_disabled", False)):
                self.phs_schedule(uid, pms_streak=int(getattr(node, "health_fail_streak", 0) or 0) + 1,
                                  pms_auto=True, pms_enabled=False, pms_note="TCP 握手失败")
            else:
                self.phs_schedule(uid, pms_streak=int(getattr(node, "health_fail_streak", 0) or 0) + 1,
                                  pms_auto=False, pms_enabled=True, pms_note="TCP 握手失败")
        if self.phc_tcp_done >= len(self.phc_batch):
            self.phs_after_tcp()

    def phs_after_tcp(self) -> None:
        """初筛结束：TCP 通的送真连接探测；阈值已到的（含 TCP 就不通的）留给真连接确认。"""
        self.phs_flush_updates()
        probe_nodes = [self.phc_batch[uid] for uid in self.phc_batch if uid in self.phc_tcp_ok]
        self.phc_checked = len(self.phc_batch) - len(probe_nodes)
        if not probe_nodes:
            # 一个都没通过初筛：直接进「确认」判定（TCP 死的节点也可能已经连败到阈值，
            # 那时按需求第四节第 1 条用**真连接**再确认一次，确认失败才禁用）
            self.phs_decide_confirm()
            return
        self.phc_stage = PMS_HEALTH_STAGE_PROBE
        self.phc_stage_total = len(probe_nodes)
        ok, message = self.phc_latency.plt_start(
            probe_nodes, False, "health", pms_track_round=False, pms_max_threads=self.phs_max_threads())
        if not ok:
            # 起不来（没有核心 / 被别人占着）：这些节点保持「到点」，下一拍重来
            self.phc_stage = PMS_HEALTH_STAGE_IDLE
            self.phc_batch = {}
            self.phs_log.emit(f"节点健康检查：真连接通道起不来（{message}），本批稍后重试")
            self.phs_status.emit(self.phs_status_text())
            return
        self.phs_status.emit(self.phs_status_text())
        self.phs_log.emit(f"节点健康检查：{len(probe_nodes)} 个节点进入真连接探测")

    def phs_on_node_result(self, pms_uid: str, pms_latency: int, pms_speed: float,
                           pms_success: bool, pms_note: str) -> None:
        """真连接结果（探测 / 确认两个阶段共用）。"""
        if self.phc_stage not in (PMS_HEALTH_STAGE_PROBE, PMS_HEALTH_STAGE_CONFIRM):
            return
        uid = str(pms_uid)
        node = self.phc_batch.get(uid)
        if node is None:
            return
        self.phc_probe_results[uid] = (bool(pms_success), str(pms_note or ""))
        self.phc_checked += 1
        self.phs_progress.emit(self.phc_checked, len(self.phc_batch))
        streak = int(getattr(node, "health_fail_streak", 0) or 0)
        was_auto = bool(getattr(node, "health_auto_disabled", False))
        if pms_success:
            # 成功一次即清零（验收标准第 4 条）；被自动禁用的 → 自动复活
            if was_auto:
                self.phs_schedule(uid, pms_streak=0, pms_auto=False, pms_enabled=True, pms_note="")
                self.phc_revived.append(str(getattr(node, "pnode_display_name", lambda: uid)()))
            else:
                self.phs_schedule(uid, pms_streak=0, pms_auto=False, pms_enabled=True, pms_note="")
            return
        if self.phc_stage == PMS_HEALTH_STAGE_CONFIRM:
            # 真连接确认也失败 → 自动禁用（**不删节点**，只标记）
            self.phs_schedule(uid, pms_streak=streak + 1, pms_auto=True, pms_enabled=False,
                              pms_note=str(pms_note or "真连接确认失败"))
            name = str(getattr(node, "pnode_display_name", lambda: uid)())
            if name not in self.phc_disabled:
                self.phc_disabled.append(name)
            return
        if was_auto:
            # 复活探测失败 → 保持禁用，顺延到下个周期
            self.phs_schedule(uid, pms_streak=streak + 1, pms_auto=True, pms_enabled=False,
                              pms_note=str(pms_note or "真连接失败"))
            return
        self.phs_schedule(uid, pms_streak=streak + 1, pms_auto=False, pms_enabled=True,
                          pms_note=str(pms_note or "真连接失败"))

    def phs_decide_confirm(self) -> None:
        """阈值判定：连败达阈值的节点再做**一次真连接确认**，只有这次也失败才自动禁用。

        注意两点（都是需求里的硬约束）：
            · 不限制于「TCP 初筛通过」的节点——TCP 就不通的节点同样要经过真连接确认才禁用；
            · **被自动禁用的节点不在这里**（它们走「复活」判定，而复活必须真连接成功）。
        """
        threshold = self.phs_fail_threshold()
        confirm_nodes = []
        for uid, node in self.phc_batch.items():
            if bool(getattr(node, "health_auto_disabled", False)):
                continue
            streak = int(self.phc_updates.get(uid, {}).get("streak", getattr(node, "health_fail_streak", 0)))
            if streak >= threshold:
                confirm_nodes.append(node)
        if not confirm_nodes:
            self.phs_finish_batch("本批没有节点达到禁用阈值")
            return
        self.phc_stage = PMS_HEALTH_STAGE_CONFIRM
        self.phc_probe_results = {}
        self.phc_checked = 0
        self.phc_stage_total = len(confirm_nodes)
        ok, message = self.phc_latency.plt_start(
            confirm_nodes, False, "health", pms_track_round=False, pms_max_threads=self.phs_max_threads())
        if not ok:
            self.phc_stage = PMS_HEALTH_STAGE_IDLE
            self.phc_batch = {}
            self.phs_log.emit(f"节点健康检查：真连接确认起不来（{message}），本批稍后重试")
            self.phs_status.emit(self.phs_status_text())
            return
        self.phs_status.emit(self.phs_status_text())
        self.phs_log.emit(f"节点健康检查：{len(confirm_nodes)} 个节点连败达阈值，做真连接确认")

    def phs_on_round_finished(self, pms_summary: Optional[dict] = None) -> None:
        """真连接轮次结束：探测阶段 → 做阈值确认判定；确认阶段 → 收尾。"""
        if self.phc_stage == PMS_HEALTH_STAGE_PROBE:
            self.phs_flush_updates()
            self.phs_decide_confirm()
            return
        if self.phc_stage == PMS_HEALTH_STAGE_CONFIRM:
            self.phs_finish_batch("真连接确认完成")
            return

    def phs_finish_batch(self, pms_reason: str) -> None:
        """收尾：落盘、通知一次、出汇总。"""
        self.phs_flush_updates()
        summary = {
            "checked": self.phc_checked, "total": len(self.phc_batch),
            "disabled": list(self.phc_disabled), "revived": list(self.phc_revived),
            "reason": str(pms_reason or ""), "next_due": self.phc_next_due,
        }
        self.phc_stage = PMS_HEALTH_STAGE_IDLE
        self.phc_batch = {}
        self.phc_tcp_ok = set()
        self.phc_tcp_bad = {}
        self.phc_probe_results = {}
        self.phc_forced = False
        self.phs_status.emit(self.phs_status_text())
        self.phs_summary.emit(summary)
        self.phs_log.emit(
            f"节点健康检查：本批结束（检查 {summary['checked']} 个，自动禁用 {len(summary['disabled'])} 个，"
            f"复活 {len(summary['revived'])} 个，下次到点 {self.phs_next_due_text()}）"
        )
        if not self.phc_settings.psm_get_bool("health.notify", True):
            return
        # 「通知一次」——一批只弹一次托盘气泡，绝不逐节点弹窗
        if summary["disabled"] and summary["revived"]:
            self.phs_notice.emit(
                "节点健康检查",
                f"自动禁用 {len(summary['disabled'])} 个失效节点、复活 {len(summary['revived'])} 个已恢复节点"
                f"（{summary['disabled'][0]} … / {summary['revived'][0]} …）",
            )
        elif summary["disabled"]:
            shown = "、".join(summary["disabled"][:3])
            self.phs_notice.emit("节点健康检查", f"已自动禁用 {len(summary['disabled'])} 个失效节点：{shown}")
        elif summary["revived"]:
            shown = "、".join(summary["revived"][:3])
            self.phs_notice.emit("节点健康检查", f"已自动复活 {len(summary['revived'])} 个恢复正常的节点：{shown}")

    # ------------------------------------------------------------------ 落盘
    def phs_schedule(self, pms_uid: str, *, pms_streak: int, pms_auto: bool, pms_enabled: bool,
                     pms_note: str) -> None:
        """记下这个节点的最新健康状态（批量落盘，别一个节点一条 UPDATE）。"""
        uid = str(pms_uid)
        node = self.phc_batch.get(uid)
        enabled = bool(pms_enabled)
        disabled_at = ""
        if pms_auto and not enabled:
            # 已经是自动禁用的：保留原来的禁用时间，别每批都刷一遍
            previous = str(getattr(node, "health_disabled_at", "") or "") if node is not None else ""
            disabled_at = previous or pms_health_text(datetime.now())
        self.phc_updates[uid] = {
            "uid": uid, "streak": max(0, int(pms_streak)), "auto_disabled": bool(pms_auto),
            "enabled": enabled, "disabled_at": disabled_at,
            "next_check_at": self.phc_next_due, "note": str(pms_note or ""),
        }
        if node is not None:
            # 内存里也同步一份：本批后续判断（阈值、是否已自动禁用）看的是这个
            try:
                node.health_fail_streak = max(0, int(pms_streak))
                node.health_auto_disabled = bool(pms_auto)
                node.health_disabled_at = disabled_at
                node.health_next_check_at = self.phc_next_due
                node.health_last_note = str(pms_note or "")
                node.enabled = enabled
            except Exception:  # noqa: BLE001 - 内存同步失败不影响落盘
                pass

    def phs_flush_updates(self) -> int:
        if not self.phc_updates:
            return 0
        rows = list(self.phc_updates.values())
        self.phc_updates = {}
        try:
            return int(self.phc_repo.pnr_update_health(rows))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("健康检查状态落盘失败：%s", exc)
            return 0

    def phs_shutdown(self) -> None:
        """退出时调用：让路 + 落盘，不留半截状态。"""
        self.phs_stop()
        self.phs_flush_updates()


def pms_health_summary_text(pms_summary: dict) -> str:
    """给状态栏 / 托盘用的一句话汇总。"""
    if not pms_summary:
        return "节点健康检查：没有可汇报的内容"
    parts = [f"检查 {int(pms_summary.get('checked') or 0)} 个节点"]
    disabled = pms_summary.get("disabled") or []
    revived = pms_summary.get("revived") or []
    if disabled:
        parts.append(f"自动禁用 {len(disabled)} 个")
    if revived:
        parts.append(f"自动复活 {len(revived)} 个")
    if not disabled and not revived:
        parts.append("没有需要禁用或复活的节点")
    return "节点健康检查：" + "，".join(parts)


def pms_health_node_note(pms_node: Any) -> str:
    """单个节点的健康说明（探针与界面提示共用，避免各处自己拼）。"""
    if not bool(getattr(pms_node, "health_auto_disabled", False)):
        return ""
    streak = int(getattr(pms_node, "health_fail_streak", 0) or 0)
    disabled_at = str(getattr(pms_node, "health_disabled_at", "") or "")
    note = str(getattr(pms_node, "health_last_note", "") or "")
    return f"健康检查自动禁用（连败 {streak} 次｜{disabled_at}｜{note}）"
