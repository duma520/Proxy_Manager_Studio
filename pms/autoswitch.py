#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时健康检测与失效自动切换（v1.6.1；v1.8.0 加候选综合评分与滞回）

做什么
------
定时给「当前正在用的节点」做一次健康检查，连续失败到设定次数（默认 2 次）就判定它失效，
然后在候选节点里**先探测再挑最快的一个**，交给主窗口去真正切换。

为什么单独成模块
----------------
1. `mainwindow.py` 已远超 1000 行红线，定时器 + 探测 + 决策塞进去只会更糟；
2. 本服务**不认识主窗口**：当前节点 / 候选列表 / 本地代理端口全部由状态提供者（provider）喂进来，
   决策结果只通过信号发出去（`pasw_switch_requested`），真正切换由主窗口执行——这样探针可以离屏驱动它。

硬规则（见文档 7.15）
--------------------
· **单次抖动不切换**：必须连续失败 N 次（默认 2）才判定失效，避免一次网络抖一下就换节点来回横跳。
· **先测后切**：切换前先把候选探测一遍（TCP 握手，不重启核心、不打扰用户），挑延迟最低的；
  探测方式只影响「判定当前节点是否失效」，候选排序一律用 TCP 握手（HTTP 探测要重启核心，代价太高）。
· **核心没运行就不动**：自动切换只在「正在连接」时生效，核心没跑不检测、也不自动拉起核心。
· **候选 = 全部已启用节点**（排除当前节点），并排除「刚切过去仍然失败」的节点，防止在两个坏节点间来回切。
· **失败就说明白**：跳过检测（无当前节点 / 核心未运行 / 冷却中 / HTTP 模式缺代理端口）都会给出原因，不静默。
· **候选综合评分**（v1.8.0）：候选先按「等效延迟」排序再截断到前 N 个——
  等效延迟 = 基准延迟（测过用实测值；没测过用历史平均；都没有才用固定中间值 **不垫底**）
  + 历史失败惩罚（成功率低、最近连败）− 同分组 / 同协议加分。
· **滞回**（v1.8.0）：本轮最优必须比「上次自动选中的候选」明显更快（≥ `PMS_AUTOSWITCH_MIN_GAIN_MS`
  毫秒 **或** ≥ `PMS_AUTOSWITCH_MIN_GAIN_RATIO` 比例）才换，否则沿用上次选择，避免两个节点来回横跳。
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence, TYPE_CHECKING
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
import logging
import time
import urllib.error
import urllib.request

from pms.constants import (
    PMS_AUTOSWITCH_COOLDOWN_SECONDS,
    PMS_AUTOSWITCH_DEFAULT_FAIL_THRESHOLD,
    PMS_AUTOSWITCH_DEFAULT_INTERVAL_SECONDS,
    PMS_AUTOSWITCH_DETECT_HTTP,
    PMS_AUTOSWITCH_DETECT_TCP,
    PMS_AUTOSWITCH_DETECT_TITLES,
    PMS_AUTOSWITCH_FIRST_DELAY_MS,
    PMS_AUTOSWITCH_MAX_CANDIDATES,
    PMS_AUTOSWITCH_MIN_INTERVAL_SECONDS,
    PMS_AUTOSWITCH_PROBE_CONCURRENCY,
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_DEFAULT_TEST_URLS,
    PMS_FASTEST_SCOPE_ALL,
    PMS_FASTEST_SCOPE_GROUP,
    PMS_FASTEST_SCOPE_TESTED,
    PMS_FASTEST_SCOPE_TITLES,
    PMS_LOGGER_NAME,
)
from pms.tasks import (
    PmsTcpPingSignals,
    PmsTcpPingTask,
)
from pms.utils import pms_format_speed

if TYPE_CHECKING:  # 仅类型检查用，避免与运行时模块循环导入
    from pms.models import PmsProxyNode
    from pms.settings import PmsSettingsManager

# 状态提供者需要给出的键（缺哪个就按「不做」处理，不猜）
PMS_AUTOSWITCH_STATE_KEYS: tuple[str, ...] = ("core_running", "current_node", "candidates", "local_proxy_port")

# v1.8.0 综合评分与滞回的权重（放本模块，不去挤 constants.py 那 1000 行的红线）
# 单位统一折算成「等效毫秒」：值越小越优先，便于和真实延迟直接相加比较。
PMS_AUTOSWITCH_UNKNOWN_LATENCY_MS = 800      # 既没实测延迟、也没历史平均时的兜底值（中等偏后，但不垫底）
PMS_AUTOSWITCH_SCORE_HISTORY_WEIGHT = 150    # 历史成功率惩罚上限：成功率 0% 全额扣，100% 不扣
PMS_AUTOSWITCH_SCORE_FAIL_WEIGHT = 60        # 最近连续失败：每次惩罚
PMS_AUTOSWITCH_SCORE_FAIL_CAP = 5            # 连续失败最多算几次（一次性抽风不至于把节点永久压死）
PMS_AUTOSWITCH_SCORE_SAME_BONUS = 40         # 与当前节点同分组 / 同协议，各减这么多
PMS_AUTOSWITCH_MIN_HISTORY_SAMPLES = 3       # 历史样本少于这个数就不按成功率惩罚（样本太少不可信）
PMS_AUTOSWITCH_MIN_GAIN_MS = 20              # 滞回：新候选要比上次选中的至少快这么多毫秒
PMS_AUTOSWITCH_MIN_GAIN_RATIO = 0.30         # 滞回：或者快这么多比例（两条满足其一即可换）
PMS_AUTOSWITCH_STATS_SAMPLE = 10             # 每个节点回看多少条历史来数「最近连续失败」


# --------------------------------------------------------------------------------------
# 后台任务：HTTP 探测（经本地代理端口，验证真实通路）
# --------------------------------------------------------------------------------------
class PmsHttpProbeSignals(QObject):
    """HTTP 探测结果 → 服务（主线程）。"""

    pphp_done = Signal(str, bool, int, str)   # 标记（节点 uid）, 成功, 耗时毫秒, 说明


class PmsHttpProbeTask(QRunnable):
    """经本地代理端口发一次 HTTP 请求：能拿到 2xx/3xx 才算这条代理通路可用。"""

    def __init__(
        self,
        pms_signals: PmsHttpProbeSignals,
        pms_uid: str,
        pms_proxy_port: int,
        pms_url: str,
        pms_timeout_ms: int,
    ) -> None:
        super().__init__()
        self.pht_signals = pms_signals
        self.pht_uid = str(pms_uid)
        self.pht_proxy_port = int(pms_proxy_port or 0)
        self.pht_url = str(pms_url or "").strip()
        self.pht_timeout_ms = max(500, int(pms_timeout_ms))
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        if self.pht_proxy_port <= 0 or not self.pht_url:
            self.pht_signals.pphp_done.emit(self.pht_uid, False, 0, "本地代理端口或测试网址缺失")
            return
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({
            "http": f"http://127.0.0.1:{self.pht_proxy_port}",
            "https": f"http://127.0.0.1:{self.pht_proxy_port}",
        }))
        request = urllib.request.Request(self.pht_url, headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT})
        started = time.perf_counter()
        try:
            with opener.open(request, timeout=self.pht_timeout_ms / 1000.0) as response:
                code = int(getattr(response, "status", 0) or 0)
                response.read(1024)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            self.pht_signals.pphp_done.emit(self.pht_uid, False, max(1, elapsed), f"HTTP 探测失败：{exc}")
            return
        elapsed = max(1, int((time.perf_counter() - started) * 1000))
        if code and not 200 <= code < 400:
            self.pht_signals.pphp_done.emit(self.pht_uid, False, elapsed, f"HTTP 状态码 {code}")
            return
        self.pht_signals.pphp_done.emit(self.pht_uid, True, elapsed, "")


# --------------------------------------------------------------------------------------
# 服务：定时检测 → 判定失效 → 探测候选 → 请求切换
# --------------------------------------------------------------------------------------
class PmsAutoSwitchService(QObject):
    """定时健康检测与自动切换调度器（不碰界面，只发信号）。"""

    pasw_status = Signal(str)                    # 状态文本（给状态栏 / 日志）
    pasw_checked = Signal(str, bool, int, str)   # 节点名, 成功, 耗时毫秒, 说明
    pasw_switch_requested = Signal(str, str)     # 目标节点 uid, 切换原因

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pasw_settings = pms_settings
        self.pasw_logger = logging.getLogger(PMS_LOGGER_NAME + ".autoswitch")
        self.pasw_provider: Optional[Callable[[], dict[str, Any]]] = None
        self.pasw_tcp_signals = PmsTcpPingSignals(self)
        self.pasw_http_signals = PmsHttpProbeSignals(self)
        self.pasw_tcp_signals.ptcp_result.connect(self.pasw_on_tcp_result)
        self.pasw_http_signals.pphp_done.connect(self.pasw_on_http_result)
        self.pasw_pool = QThreadPool(self)
        self.pasw_pool.setMaxThreadCount(PMS_AUTOSWITCH_PROBE_CONCURRENCY)
        self.pasw_timer = QTimer(self)
        self.pasw_timer.timeout.connect(self.pasw_on_tick)
        self.pasw_phase = ""                     # "" 空闲 / "check" 检测当前节点 / "probe" 探测候选
        self.pasw_fail_count = 0
        self.pasw_done = 0
        self.pasw_expected = 0
        self.pasw_results: dict[str, int] = {}   # uid → TCP 延迟毫秒（只有成功的）
        self.pasw_avoid_uids: list[str] = []     # 刚切过去仍然失败的节点，下一轮绕开
        self.pasw_probe_nodes: dict[str, Any] = {}   # uid → 节点（探测阶段留一份，收尾算综合评分要用）
        self.pasw_stats_provider: Optional[Callable[[Sequence[str]], dict[str, dict]]] = None
        self.pasw_stats: dict[str, dict] = {}    # uid → 历史统计（由提供者喂，见 pasw_load_stats）
        self.pasw_last_switch_uid = ""           # 上次自动切换选中的节点（滞回的比较基准）
        self.pasw_probe_reason = ""
        self.pasw_last_note = ""
        self.pasw_last_switch_at = 0.0

    # ---------------------------------------------------------------- 设置
    def pasw_is_enabled(self) -> bool:
        return bool(self.pasw_settings.psm_get_bool("autoswitch.enabled", False))

    def pasw_interval_seconds(self) -> int:
        return int(self.pasw_settings.psm_get_int("autoswitch.interval_seconds", PMS_AUTOSWITCH_DEFAULT_INTERVAL_SECONDS))

    def pasw_fail_threshold(self) -> int:
        return int(self.pasw_settings.psm_get_int("autoswitch.fail_threshold", PMS_AUTOSWITCH_DEFAULT_FAIL_THRESHOLD))

    def pasw_detect_mode(self) -> str:
        mode = str(self.pasw_settings.psm_get_str("autoswitch.detect_mode", PMS_AUTOSWITCH_DETECT_TCP))
        return mode if mode in PMS_AUTOSWITCH_DETECT_TITLES else PMS_AUTOSWITCH_DETECT_TCP

    def pasw_notify_enabled(self) -> bool:
        return bool(self.pasw_settings.psm_get_bool("autoswitch.notify", True))

    def pasw_tcp_timeout_ms(self) -> int:
        """TCP 握手超时：复用延迟测试的设置（`test.tcp_timeout_ms`），不另造一套。"""
        return int(self.pasw_settings.psm_get_int("test.tcp_timeout_ms", 3000))

    def pasw_http_timeout_ms(self) -> int:
        """HTTP 探测超时：复用延迟测试的设置（`test.timeout_ms`）。"""
        return int(self.pasw_settings.psm_get_int("test.timeout_ms", 5000))

    def pasw_probe_url(self) -> str:
        """HTTP 探测用的网址：取延迟测试网址列表第一项。"""
        urls = self.pasw_settings.psm_get_list("test.url_list")
        return str(urls[0]) if urls else str(PMS_DEFAULT_TEST_URLS[0])

    # ---------------------------------------------------------------- 状态来源
    def pasw_set_state_provider(self, pms_provider: Optional[Callable[[], dict[str, Any]]]) -> None:
        """登记状态提供者（主窗口传入，返回 dict；服务不认识主窗口）。"""
        self.pasw_provider = pms_provider

    def pasw_state(self) -> dict[str, Any]:
        """读一份运行时状态；提供者异常时返回空 dict（不静默失败，记日志）。"""
        if self.pasw_provider is None:
            return {}
        try:
            state = self.pasw_provider()
        except Exception as exc:  # noqa: BLE001
            self.pasw_logger.warning("自动切换状态提供者异常：%s", exc)
            return {}
        return state if isinstance(state, dict) else {}

    # ---------------------------------------------------------------- 定时器
    @Slot()
    def pasw_start(self) -> None:
        """按当前设置启动定时器；开启时启动后延迟做第一次检测。"""
        self.pasw_timer.stop()
        if not self.pasw_is_enabled():
            return
        self.pasw_timer.start(max(PMS_AUTOSWITCH_MIN_INTERVAL_SECONDS, self.pasw_interval_seconds()) * 1000)
        QTimer.singleShot(PMS_AUTOSWITCH_FIRST_DELAY_MS, self.pasw_on_tick)

    @Slot()
    def pasw_stop(self) -> None:
        self.pasw_timer.stop()
        self.pasw_phase = ""

    @Slot()
    def pasw_apply_settings(self) -> None:
        """设置改了就重建定时器（开关、间隔、次数、方式都走这里）。"""
        self.pasw_logger.info(
            "自动切换设置已变更：开关=%s 间隔=%s 秒 失败阈值=%s 次 检测方式=%s",
            self.pasw_is_enabled(), self.pasw_interval_seconds(), self.pasw_fail_threshold(), self.pasw_detect_mode(),
        )
        self.pasw_fail_count = 0
        self.pasw_start()

    @Slot()
    def pasw_on_tick(self) -> None:
        if not self.pasw_is_enabled():
            return
        self.pasw_check_now()

    # ---------------------------------------------------------------- 检测当前节点
    def pasw_check_now(self, pms_manual: bool = False) -> bool:
        """立即检测一次当前节点；返回是否真的发起了检测（没发起会说明原因）。"""
        if self.pasw_phase:
            return False
        if time.monotonic() - self.pasw_last_switch_at < PMS_AUTOSWITCH_COOLDOWN_SECONDS:
            if pms_manual:
                self.pasw_status.emit("刚切换过核心，冷却中，稍后再检测")
            return False
        state = self.pasw_state()
        node = state.get("current_node")
        if node is None:
            if pms_manual:
                self.pasw_status.emit("当前没有连接中的节点，无需检测")
            return False
        if not bool(state.get("core_running", False)):
            if pms_manual:
                self.pasw_status.emit("核心未运行，自动切换只在连接中生效")
            return False
        if self.pasw_detect_mode() == PMS_AUTOSWITCH_DETECT_HTTP:
            port = int(state.get("local_proxy_port", 0) or 0)
            if port <= 0:
                if pms_manual:
                    self.pasw_status.emit("HTTP 探测需要本地代理端口，当前端口为 0，跳过本轮")
                return False
            self.pasw_phase = "check"
            self.pasw_pool.start(PmsHttpProbeTask(
                self.pasw_http_signals, str(node.uid), port, self.pasw_probe_url(), self.pasw_http_timeout_ms(),
            ))
            return True
        self.pasw_phase = "check"
        self.pasw_pool.start(PmsTcpPingTask(node, self.pasw_tcp_timeout_ms(), self.pasw_tcp_signals))
        return True

    @Slot(str, int, str)
    def pasw_on_tcp_result(self, pms_uid: str, pms_latency: int, pms_error: str) -> None:
        """TCP 握手结果：检测阶段与探测阶段共用，靠 phase 区分。"""
        if self.pasw_phase == "check":
            note = str(pms_error or "").strip()
            self.pasw_finish_check(
                int(pms_latency) >= 0, max(0, int(pms_latency)), note or "" if int(pms_latency) >= 0 else "TCP 握手失败",
            )
            return
        if self.pasw_phase == "probe":
            self.pasw_collect_probe(str(pms_uid), int(pms_latency))
            return

    @Slot(str, bool, int, str)
    def pasw_on_http_result(self, pms_uid: str, pms_ok: bool, pms_latency: int, pms_note: str) -> None:
        """HTTP 探测结果（只用于检测当前节点；候选排序不用它，代价太高）。"""
        if self.pasw_phase == "check":
            note = str(pms_note or "").strip()
            self.pasw_finish_check(
                bool(pms_ok), max(0, int(pms_latency)), note or "" if pms_ok else "HTTP 探测失败",
            )

    def pasw_finish_check(self, pms_ok: bool, pms_latency: int, pms_note: str) -> None:
        """一次检测收尾：成功清零计数，失败累加到阈值就转去探测候选。"""
        self.pasw_phase = ""
        self.pasw_last_note = pms_note
        node = self.pasw_state().get("current_node")
        name = node.pnode_display_name() if node is not None else "当前节点"
        self.pasw_checked.emit(name, bool(pms_ok), int(pms_latency), pms_note)
        if pms_ok:
            self.pasw_fail_count = 0
            self.pasw_avoid_uids = []
            return
        self.pasw_fail_count += 1
        threshold = self.pasw_fail_threshold()
        if self.pasw_fail_count < threshold:
            self.pasw_status.emit(f"当前节点检测未通过（{self.pasw_fail_count}/{threshold}）：{pms_note}")
            return
        self.pasw_fail_count = 0
        self.pasw_start_probe(f"当前节点连续 {threshold} 次检测失败（{pms_note}）")

    # ---------------------------------------------------------------- 历史统计（v1.8.0）
    def pasw_set_stats_provider(
        self,
        pms_provider: Optional[Callable[[Sequence[str]], dict[str, dict]]],
    ) -> None:
        """登记历史统计提供者：给它 uid 列表，它返回 `{uid: {...}}`（主窗口用仓库查，本服务不认识数据库）。"""
        self.pasw_stats_provider = pms_provider

    def pasw_load_stats(self, pms_uids: Sequence[str]) -> None:
        """按候选 uid 取历史统计；提供者缺失或异常时按「无历史」处理（记 warning，不静默吞掉）。"""
        self.pasw_stats = {}
        uids = [str(item) for item in (pms_uids or []) if str(item)]
        if self.pasw_stats_provider is None or not uids:
            return
        try:
            data = self.pasw_stats_provider(uids)
        except Exception as exc:  # noqa: BLE001
            self.pasw_logger.warning("读取节点历史统计失败：%s", exc)
            return
        self.pasw_stats = data if isinstance(data, dict) else {}

    def pasw_stats_of(self, pms_uid: str) -> dict:
        """取某个节点的历史统计；没有就返回空 dict（调用方按「无历史、不惩罚」处理）。"""
        item = self.pasw_stats.get(str(pms_uid))
        return item if isinstance(item, dict) else {}

    # ---------------------------------------------------------------- 综合评分（v1.8.0）
    def pasw_penalty_ms(self, pms_uid: str, pms_node: Any, pms_current: Any) -> float:
        """综合评分里「与本次实测延迟无关」的那部分（等效毫秒，越小越好，负数 = 加分）。

        组成：历史成功率惩罚 + 最近连续失败惩罚 − 同分组加分 − 同协议加分。
        """
        penalty = 0.0
        stats = self.pasw_stats_of(pms_uid)
        if stats:
            total = int(stats.get("total") or 0)
            rate = float(stats.get("rate") or 0.0)
            if total >= PMS_AUTOSWITCH_MIN_HISTORY_SAMPLES:
                penalty += (1.0 - max(0.0, min(1.0, rate))) * PMS_AUTOSWITCH_SCORE_HISTORY_WEIGHT
            penalty += min(int(stats.get("recent_fails") or 0), PMS_AUTOSWITCH_SCORE_FAIL_CAP) * PMS_AUTOSWITCH_SCORE_FAIL_WEIGHT
        if pms_current is not None:
            group = str(getattr(pms_node, "group_name", "") or "")
            if group and group == str(getattr(pms_current, "group_name", "") or ""):
                penalty -= PMS_AUTOSWITCH_SCORE_SAME_BONUS
            protocol = str(getattr(pms_node, "protocol", "") or "")
            if protocol and protocol == str(getattr(pms_current, "protocol", "") or ""):
                penalty -= PMS_AUTOSWITCH_SCORE_SAME_BONUS
        return penalty

    def pasw_score_candidate(self, pms_node: Any, pms_current: Any = None) -> float:
        """候选综合评分（等效毫秒，越小越好）= 基准延迟 + 历史惩罚 − 同分组 / 同协议加分。

        基准延迟取值顺序：实测延迟 → 历史成功样本平均 → 都没有才用 `PMS_AUTOSWITCH_UNKNOWN_LATENCY_MS`
        （**中等偏后，但不垫底**：没测过的节点也有机会被探一次，探出来就是真实数据）。
        """
        latency = getattr(pms_node, "latency_ms", None)
        uid = str(getattr(pms_node, "uid", "") or "")
        if latency is None or int(latency) < 0:
            average = self.pasw_stats_of(uid).get("avg_ms")
            base = float(average) if isinstance(average, (int, float)) else float(PMS_AUTOSWITCH_UNKNOWN_LATENCY_MS)
        else:
            base = float(int(latency))
        return max(0.0, base + self.pasw_penalty_ms(uid, pms_node, pms_current))

    # ---------------------------------------------------------------- 探测候选
    def pasw_candidate_nodes(self) -> "list[PmsProxyNode]":
        """候选 = 全部已启用、配置合法、不是当前节点、且不在「刚切过去还失败」名单里的节点。

        超过上限时按**综合评分**升序截断（v1.8.0 起不再只看已知延迟）：先探最有希望的一批，别把线程池打满。
        """
        state = self.pasw_state()
        current = state.get("current_node")
        current_uid = str(getattr(current, "uid", "") or "")
        nodes = [
            node for node in state.get("candidates", [])
            if node is not None
            and bool(getattr(node, "enabled", True))
            # v1.9.37：被节点健康检查自动禁用的节点不参与候选（双重保险——它的 enabled 本来就是 0）
            and not bool(getattr(node, "health_auto_disabled", False))
            and str(node.uid) != current_uid
            and not node.pnode_validate()
        ]
        preferred = [node for node in nodes if str(node.uid) not in self.pasw_avoid_uids]
        pool = preferred or nodes
        self.pasw_load_stats([str(node.uid) for node in pool])
        pool.sort(key=lambda item: self.pasw_score_candidate(item, current))
        return pool[: PMS_AUTOSWITCH_MAX_CANDIDATES]

    def pasw_start_probe(self, pms_reason: str) -> int:
        """对候选做一轮 TCP 握手探测，返回提交的任务数（0 = 没有候选）。"""
        candidates = self.pasw_candidate_nodes()
        if not candidates:
            self.pasw_status.emit("没有可用候选节点，保持当前连接")
            return 0
        self.pasw_phase = "probe"
        self.pasw_probe_reason = pms_reason
        self.pasw_results = {}
        self.pasw_probe_nodes = {str(node.uid): node for node in candidates}
        self.pasw_done = 0
        self.pasw_expected = len(candidates)
        for node in candidates:
            self.pasw_pool.start(PmsTcpPingTask(node, self.pasw_tcp_timeout_ms(), self.pasw_tcp_signals))
        self.pasw_status.emit(f"正在探测 {self.pasw_expected} 个候选节点（按综合评分挑最优的切换）…")
        return self.pasw_expected

    def pasw_collect_probe(self, pms_uid: str, pms_latency: int) -> None:
        """收一个候选结果；收齐就定目标。"""
        if int(pms_latency) >= 0:
            self.pasw_results[str(pms_uid)] = int(pms_latency)
        self.pasw_done += 1
        if self.pasw_done >= self.pasw_expected:
            self.pasw_finish_probe()

    def pasw_finish_probe(self) -> None:
        """探测收尾：按「实测延迟 + 综合评分惩罚」挑候选，并加滞回（避免两个节点来回横跳）。

        滞回规则：如果上次自动选中的候选本轮也探通了，那么本轮最优必须比它明显更快
        （≥ `PMS_AUTOSWITCH_MIN_GAIN_MS` 毫秒 **或** ≥ `PMS_AUTOSWITCH_MIN_GAIN_RATIO` 比例）才换，
        否则沿用上次的选择——否则两个延迟相近的节点会被轮流选中，核心跟着反复重启。
        """
        self.pasw_phase = ""
        if not self.pasw_results:
            self.pasw_status.emit("候选节点全部不可用，保持当前连接")
            return
        current = self.pasw_state().get("current_node")
        scored = {
            uid: float(ms) + self.pasw_penalty_ms(uid, self.pasw_probe_nodes.get(uid), current)
            for uid, ms in self.pasw_results.items()
        }
        best_uid = min(scored, key=lambda uid: scored[uid])
        best_ms = self.pasw_results[best_uid]
        incumbent = str(self.pasw_last_switch_uid or "")
        if incumbent and incumbent in scored and incumbent != best_uid:
            margin = max(float(PMS_AUTOSWITCH_MIN_GAIN_MS), scored[incumbent] * PMS_AUTOSWITCH_MIN_GAIN_RATIO)
            if scored[best_uid] > scored[incumbent] - margin:
                kept_ms = self.pasw_results[incumbent]
                self.pasw_last_switch_at = time.monotonic()
                self.pasw_status.emit(
                    f"候选最快 {best_ms} ms，但没明显优于上次选中的 {kept_ms} ms（需快 ≥{int(margin)} ms），沿用上次选择以避免抖动"
                )
                self.pasw_switch_requested.emit(
                    incumbent,
                    f"{self.pasw_probe_reason}；滞回：本轮最优 {best_ms} ms 未明显优于上次选中的 {kept_ms} ms，沿用上次选择",
                )
                return
        self.pasw_last_switch_uid = best_uid
        self.pasw_avoid_uids.append(best_uid)
        self.pasw_avoid_uids = self.pasw_avoid_uids[-5:]
        self.pasw_last_switch_at = time.monotonic()
        self.pasw_status.emit(f"已选定候选节点（TCP {best_ms} ms，综合评分 {scored[best_uid]:.0f}），正在切换…")
        self.pasw_switch_requested.emit(
            best_uid,
            f"{self.pasw_probe_reason}；候选综合最优 TCP 握手 {best_ms} ms（评分 {scored[best_uid]:.0f}）",
        )

    # ---------------------------------------------------------------- 查询
    def pasw_is_checking(self) -> bool:
        return bool(self.pasw_phase)

    def pasw_current_fail_count(self) -> int:
        return int(self.pasw_fail_count)

    # ---------------------------------------------------------------- 测速后切最快（v1.9.33）
    def pasw_fastest_enabled(self) -> bool:
        return bool(self.pasw_settings.psm_get_bool("autoswitch.fastest_after_speed", True))

    def pasw_fastest_scope(self) -> str:
        scope = str(self.pasw_settings.psm_get_str("autoswitch.fastest_scope", PMS_FASTEST_SCOPE_TESTED))
        return scope if scope in PMS_FASTEST_SCOPE_TITLES else PMS_FASTEST_SCOPE_TESTED

    def pasw_fastest_min_speed(self) -> float:
        """最低速度门槛（字节/秒）；设置里填的是 KB/s，0 = 不限。"""
        return max(0.0, float(self.pasw_settings.psm_get_int("autoswitch.fastest_min_speed", 0)) * 1024.0)

    def pasw_fastest_candidates(self, pms_tested_uids: Sequence[str] = ()) -> "list[PmsProxyNode]":
        """按「分组策略」挑出可参与比较的节点：已启用、配置合法、不是当前节点、**测出过速度**。

        范围（`autoswitch.fastest_scope`）：
            `tested` 本次测速测过的（默认，用户拍板的口径）；
            `all`    全部已启用节点（用它们最近一次测得的速度）；
            `group`  当前分组筛选（`ui.filter_group`）范围内的节点。
        """
        state = self.pasw_state()
        current = state.get("current_node")
        current_uid = str(getattr(current, "uid", "") or "")
        pool = [node for node in state.get("candidates", []) if node is not None]
        scope = self.pasw_fastest_scope()
        if scope == PMS_FASTEST_SCOPE_TESTED:
            wanted = {str(uid) for uid in pms_tested_uids}
            pool = [node for node in pool if str(node.uid) in wanted]
        elif scope == PMS_FASTEST_SCOPE_GROUP:
            group = str(self.pasw_settings.psm_get_str("ui.filter_group", "") or "")
            pool = [node for node in pool if str(getattr(node, "group_name", "") or "") == group]
        return [
            node for node in pool
            if bool(getattr(node, "enabled", True))
            and not bool(getattr(node, "health_auto_disabled", False))   # v1.9.37：自动禁用的不参与
            and str(node.uid) != current_uid
            and not node.pnode_validate()
            and float(getattr(node, "speed_bps", 0.0) or 0.0) > 0
        ]

    def pasw_pick_fastest(self, pms_tested_uids: Sequence[str] = ()) -> Optional[Any]:
        """挑最快节点：先比速度，速度相同再比延迟；低于门槛的整批不算（返回 None）。"""
        candidates = self.pasw_fastest_candidates(pms_tested_uids)
        threshold = self.pasw_fastest_min_speed()
        if threshold > 0:
            candidates = [node for node in candidates if float(node.speed_bps or 0.0) >= threshold]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda node: (
                float(getattr(node, "speed_bps", 0.0) or 0.0),
                -int(getattr(node, "latency_ms", None) or 10 ** 9),
            ),
        )

    def pasw_on_speedtest_finished(self, pms_summary: Optional[dict] = None) -> bool:
        """测速结束 → 可选「切到最快节点」（与定时自动切换**共用同一条切换链路**）。

        与自动切换的整合点：
        · 决策走这里，真正切换由主窗口接 `pasw_switch_requested` 执行（本服务不碰界面与核心）；
        · 沿用 `pasw_last_switch_uid` / `pasw_last_switch_at` 这套「刚切过」记账，避免与定时检测互相打架；
        · **核心没在跑就不切**（否则用户只是测个速，却被动拉起核心连上代理，太意外）。
        """
        if not self.pasw_fastest_enabled():
            return False
        state = self.pasw_state()
        if not bool(state.get("core_running")):
            self.pasw_status.emit("测速后自动切换已跳过：核心没在运行（不会替你拉起核心）")
            return False
        summary = dict(pms_summary or {})
        node = self.pasw_pick_fastest(summary.get("uids") or ())
        if node is None:
            threshold = self.pasw_fastest_min_speed()
            self.pasw_status.emit(
                "测速后自动切换已跳过：范围内没有测出速度的节点"
                + (f"（门槛 {threshold / 1024:.0f} KB/s）" if threshold > 0 else "")
            )
            return False
        current = state.get("current_node")
        speed_text = pms_format_speed(node.speed_bps)
        if current is not None and str(current.uid) == str(node.uid):
            self.pasw_status.emit(f"测速后自动切换：当前节点就是最快（{speed_text}），保持不动")
            return False
        self.pasw_last_switch_uid = str(node.uid)
        self.pasw_last_switch_at = time.monotonic()
        self.pasw_avoid_uids.append(str(node.uid))
        self.pasw_avoid_uids = self.pasw_avoid_uids[-5:]
        self.pasw_status.emit(f"测速后自动切换：本次最快是 {node.pnode_display_name()}（{speed_text}），正在切换…")
        self.pasw_switch_requested.emit(
            str(node.uid),
            f"测速后自动切到最快节点（{speed_text}，范围：{PMS_FASTEST_SCOPE_TITLES.get(self.pasw_fastest_scope(), '')}）",
        )
        return True
