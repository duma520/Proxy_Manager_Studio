#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外接口门面（Python 包接口 + HTTP 接口的同一层实现）

为什么单独一个门面：
    HTTP 服务、以后要做的 Clash / sing-box External Controller 兼容层、
    以及用户脚本 `from pms.api import PmsApi` 直接调用，都应当得到**同一份**结果与
    同一套字段，避免「网页接口一套、脚本接口另一套」这种最容易出错的双实现。
    因此路由层（`apiserver.py`）只做「HTTP → 调门面 → JSON」，业务逻辑全在这里。

v1.6.0 范围：只读（状态 / 节点 / 分组 / 流量 / 版本 / 连通性自检）。
v1.6.2 起补写操作（启停核心、切节点、系统代理、TUN、节点增删），
统一经 `pms/apicommands.py` 派发到界面线程执行，**本模块只做校验与结果封装**。
v1.8.0 起补「事件推送」：主窗口把核心启停 / 节点切换 / 错误 / 测试完成 / 订阅更新 / 流量更新
写进本模块的事件环（`papi_emit_event`），HTTP 层用 SSE（`GET /api/v1/events`）增量推给订阅方。

安全约定：
    1. 节点口令类字段（`credential_password` / `obfs_password`）**一律不对外输出**；
    2. token 只在 `.env` 里，由 `PmsEnvStore` 读写，本模块不落任何凭据到设置文件。
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Optional,
    Sequence,
)

from pms.constants import (
    PMS_APP_NAME,
    PMS_APP_VERSION,
    PMS_API_DEFAULT_HOST,
    PMS_API_DEFAULT_PORT,
    PMS_API_LOOPBACK_HOSTS,
    PMS_API_PATH_PREFIX,
    PMS_LOGGER_NAME,
)
from pms.utils import pms_now_text

if True:  # 仅类型检查用，避免与数据库 / 设置模块循环导入
    from pms.database import (
        PMS_EVENT_LOG_MAX,
        PmsNodeRepository,
    )
    from pms.settings import PmsSettingsManager

if TYPE_CHECKING:  # 写操作派发器由主窗口装配，这里只标注类型，避免与派发模块循环依赖
    from pms.apicommands import PmsApiCommander

# 对外一律不输出的字段（口令类；凭据 uuid 只是 vault 里的引用，同样不外泄）
PMS_API_HIDDEN_NODE_FIELDS: tuple[str, ...] = ("credential_password", "obfs_password", "credential_uuid")

logger = logging.getLogger(PMS_LOGGER_NAME + ".api")

# v1.9.49（审计表第 23 项）：事件计数器属性名与「读最新序号」方法的命名红线
# （两个自检函数放在 `PmsApi` 类定义之后，见文件末尾）
PMS_API_EVENT_SEQ_ATTR = "papi_event_seq"
PMS_API_EVENT_SEQ_METHOD = "papi_event_last_seq"

# v1.8.0 事件推送：环形缓冲只留最近这么多条，超了丢最旧的（推送是「通知」，不是可靠队列）
PMS_API_EVENT_MAX = 200
# v1.9.54（审计表第 22 项）：环只留最近 200 条，客户端断线超过 200 条就漏事件。
# 现在把事件**同时落库兜底**（`pms_meta` 的 `event.log.<seq>`，见 pms/database.py），
# 带 `since` 的客户端落后于环容量时从库里补齐；条数上限与清理口径同 `PMS_EVENT_LOG_MAX`。
PMS_API_EVENT_PERSIST = True
PMS_API_EVENT_KINDS: tuple[str, ...] = (
    "core.started",        # 核心启动成功
    "core.stopped",        # 核心停止
    "node.switched",       # 当前节点切换（手动或自动）
    "error.raised",        # 错误（核心报错等）
    "test.finished",       # 延迟 / 测速一轮结束
    "subscription.updated",  # 订阅更新完成
    "traffic.updated",     # 流量采样更新
)


class PmsApiResult:
    """统一返回体：`ok` + `data`，失败时 `ok=False` 且带 `error`。

    HTTP 层直接 JSON 序列化它，脚本层可直接取 `.pmsar_data`，两边表现一致。
    """

    def __init__(self, pms_data: Any = None, pms_code: str = "", pms_message: str = "") -> None:
        self.pmsar_data = pms_data
        self.pmsar_code = pms_code
        self.pmsar_message = pms_message

    @property
    def pmsar_ok(self) -> bool:
        return not self.pmsar_code

    def pmsar_to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.pmsar_ok, "data": self.pmsar_data}
        if not self.pmsar_ok:
            payload["error"] = {"code": self.pmsar_code, "message": self.pmsar_message}
        return payload

    @staticmethod
    def pmsar_ok_with(pms_data: Any) -> "PmsApiResult":
        return PmsApiResult(pms_data)

    @staticmethod
    def pmsar_fail(pms_code: str, pms_message: str) -> "PmsApiResult":
        return PmsApiResult(None, str(pms_code), str(pms_message))


class PmsApi:
    """对外接口门面（**只读**，v1.6.0）。

    `pms_state` 是「运行时状态提供者」：由主窗口传一个返回字典的可调用对象，
    这样本模块不需要认识主窗口，也不会把主窗口的 2700 行拖进来。
    """

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_repo: "PmsNodeRepository",
        pms_state: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        self.papi_settings = pms_settings
        self.papi_repo = pms_repo
        self.papi_state_provider = pms_state
        self.papi_commander: Optional["PmsApiCommander"] = None
        self.papi_event_seq = 0                                  # v1.8.0：事件序号（客户端断线续传的起点）
        self.papi_events: deque = deque(maxlen=PMS_API_EVENT_MAX)  # v1.8.0：事件环（只在主线程写，HTTP 线程只读）

    # ------------------------------------------------------------------ 写操作派发器
    def papi_set_commander(self, pms_commander: "PmsApiCommander") -> None:
        """挂上写操作派发器（主窗口装配一次即可；没挂时写操作返回 `not_ready`）。"""
        self.papi_commander = pms_commander

    def papi_write_enabled(self) -> bool:
        """写操作总开关（`api.write_enabled`，**默认关闭**，只读不受影响）。"""
        return bool(self.papi_settings.psm_get_bool("api.write_enabled", False))

    def papi_dispatch(self, pms_action: str, pms_payload: Optional[dict] = None) -> PmsApiResult:
        """统一的写操作入口：开关 → 派发器 → 结果封装（**不在这里碰界面**）。"""
        return self.papi_dispatch_action(pms_action, pms_payload, pms_require_write=True)

    def papi_dispatch_action(
        self,
        pms_action: str,
        pms_payload: Optional[dict] = None,
        pms_require_write: bool = True,
    ) -> PmsApiResult:
        """派发一个动作到界面线程。

        `pms_require_write=False` 用于**只读但仍需界面线程**的动作（v1.6.3 的四个导出）：
        导出不改数据，所以不受 `api.write_enabled` 限制，但仍然要走派发器（要读界面侧的数据）。
        """
        if pms_require_write and not self.papi_write_enabled():
            return PmsApiResult.pmsar_fail(
                "write_disabled",
                "写操作未开启：到「设置 → 接口」勾选「允许写操作」后再调用（只读接口不受影响）",
            )
        if self.papi_commander is None:
            return PmsApiResult.pmsar_fail("not_ready", "动作派发器尚未装配（程序可能还在启动）")
        raw = self.papi_commander.pcmd_execute(str(pms_action), dict(pms_payload or {}))
        if dict(raw or {}).get("ok"):
            return PmsApiResult.pmsar_ok_with(raw.get("data"))
        return PmsApiResult.pmsar_fail(str(raw.get("code") or "server_error"), str(raw.get("message") or "动作执行失败"))

    # ------------------------------------------------------------------ 状态提供者
    def papi_set_state_provider(self, pms_provider: Callable[[], dict[str, Any]]) -> None:
        """挂上运行时状态提供者（主窗口调用一次即可）。"""
        self.papi_state_provider = pms_provider

    def papi_state(self) -> dict[str, Any]:
        """取运行时状态快照；没挂提供者时返回最小可用字典（不抛异常）。"""
        if self.papi_state_provider is None:
            return {}
        try:
            state = self.papi_state_provider()
        except Exception:  # noqa: BLE001 - 对外接口绝不能因为内部异常而 500
            return {}
        return dict(state) if isinstance(state, dict) else {}

    # ------------------------------------------------------------------ 对外方法
    def papi_health(self) -> PmsApiResult:
        """连通性 + 依赖健康自检（第 12 项）：单端点判断“程序状态是否健康”。

        同时回报核心是否在跑、设置是否加载、数据库是否可写，脚本不必再拼多个接口。
        """
        state = self.papi_state()
        core = state.get("core") if isinstance(state.get("core"), dict) else {}
        dependencies = {
            "core_running": bool(core.get("running", False)),
            "settings_loaded": bool(self.papi_settings and self.papi_settings.psm_loaded),
            "database_ok": self._papi_check_db(),
        }
        overall_ok = all(dependencies.values())
        return PmsApiResult.pmsar_ok_with({
            "status": "ok" if overall_ok else "degraded",
            "app": PMS_APP_NAME,
            "version": PMS_APP_VERSION,
            "dependencies": dependencies,
        })

    def _papi_check_db(self) -> bool:
        """轻量探测数据库是否可读写（不抛异常，供健康检查用）。"""
        try:
            if self.papi_repo is None:
                return False
            self.papi_repo.pnr_count_nodes()
            return True
        except Exception:  # noqa: BLE001 - 健康探测绝不允许异常逃逸
            return False

    def papi_settings_history(self) -> PmsApiResult:
        """设置历史快照列表（第 15 项，只读）：供脚本 / 界面查看“设置都改过什么”。"""
        try:
            snapshots = self.papi_settings.psm_list_snapshots()
        except Exception:  # noqa: BLE001
            return PmsApiResult.pmsar_fail("history_unavailable", "设置历史暂不可用")
        return PmsApiResult.pmsar_ok_with(snapshots)

    def papi_version(self) -> PmsApiResult:
        return PmsApiResult.pmsar_ok_with({
            "app": PMS_APP_NAME,
            "version": PMS_APP_VERSION,
            "api_prefix": PMS_API_PATH_PREFIX,
        })

    def papi_status(self) -> PmsApiResult:
        """全局状态：核心 / 当前节点 / 系统代理 / TUN / 接口自身配置。"""
        state = self.papi_state()
        core = state.get("core") if isinstance(state.get("core"), dict) else {}
        return PmsApiResult.pmsar_ok_with({
            "core_running": bool(core.get("running", False)),
            "core_kind": str(core.get("kind") or ""),
            "current_node_uid": str(state.get("current_node_uid") or ""),
            "system_proxy": bool(state.get("system_proxy", False)),
            "tun": bool(state.get("tun", False)),
            "test_running": bool(state.get("test_running", False)),
            "node_count": self.papi_node_count(),
            "api": self.papi_api_config(),
        })

    def papi_api_config(self) -> dict[str, Any]:
        """接口自身配置（**不含 token**，token 只在 .env）。"""
        settings = self.papi_settings
        return {
            "enabled": bool(settings.psm_get_bool("api.enabled", False)),
            "host": str(settings.psm_get_str("api.host", PMS_API_DEFAULT_HOST) or PMS_API_DEFAULT_HOST),
            "port": int(settings.psm_get_int("api.port", PMS_API_DEFAULT_PORT)),
            "auth_mode": str(settings.psm_get_str("api.auth_mode", "auto") or "auto"),
            "write_enabled": self.papi_write_enabled(),
        }

    def papi_node_count(self) -> int:
        try:
            return int(self.papi_repo.pnr_count_nodes())
        except Exception:  # noqa: BLE001 - 数据库异常也要能返回 0 而不是崩
            return 0

    def papi_nodes(self, pms_group: str = "", pms_only_enabled: bool = False) -> PmsApiResult:
        """节点列表；`pms_group` 为空表示全部分组。"""
        try:
            nodes = list(self.papi_repo.pnr_load_all_nodes())
        except Exception as exc:  # noqa: BLE001
            return PmsApiResult.pmsar_fail("db_error", f"读取节点失败：{exc}")
        group = str(pms_group or "").strip()
        result = []
        for node in nodes:
            if group and str(node.group_name) != group:
                continue
            if pms_only_enabled and not bool(node.enabled):
                continue
            result.append(self.papi_public_node(node))
        return PmsApiResult.pmsar_ok_with(result)

    def papi_node(self, pms_uid: str) -> PmsApiResult:
        """按 uid 取单个节点；不存在返回 404 语义的 `node_not_found`。"""
        uid = str(pms_uid or "").strip()
        if not uid:
            return PmsApiResult.pmsar_fail("bad_request", "缺少节点 uid")
        try:
            node = self.papi_repo.pnr_load_node(uid)
        except Exception as exc:  # noqa: BLE001
            return PmsApiResult.pmsar_fail("db_error", f"读取节点失败：{exc}")
        if node is None:
            return PmsApiResult.pmsar_fail("node_not_found", f"没有这个节点：{uid}")
        return PmsApiResult.pmsar_ok_with(self.papi_public_node(node))

    def papi_groups(self) -> PmsApiResult:
        """分组清单（含每个分组的节点数）。"""
        try:
            summary = list(self.papi_repo.pnr_group_summary())
        except Exception as exc:  # noqa: BLE001
            return PmsApiResult.pmsar_fail("db_error", f"读取分组失败：{exc}")
        return PmsApiResult.pmsar_ok_with([
            {"name": str(name), "count": int(count)} for name, count in summary
        ])

    def papi_traffic(self) -> PmsApiResult:
        """流量与速率快照（来自运行时状态；核心没起时为 0）。"""
        state = self.papi_state()
        totals = state.get("traffic_totals") if isinstance(state.get("traffic_totals"), dict) else {}
        speed = state.get("traffic_speed") if isinstance(state.get("traffic_speed"), dict) else {}
        return PmsApiResult.pmsar_ok_with({
            "totals": {str(key): int(value or 0) for key, value in totals.items()},
            "speed_bps": {str(key): float(value or 0.0) for key, value in speed.items()},
            "source": str(state.get("traffic_source") or ""),
        })

    # ------------------------------------------------------------------ 写操作（v1.6.2）
    def papi_core_start(self, pms_uid: str = "") -> PmsApiResult:
        """启动核心；不传 uid 就用当前节点（没有当前节点则取第一个可见节点）。"""
        return self.papi_dispatch("core_start", {"uid": str(pms_uid or "").strip()})

    def papi_core_stop(self) -> PmsApiResult:
        return self.papi_dispatch("core_stop", {})

    def papi_core_restart(self, pms_uid: str = "") -> PmsApiResult:
        return self.papi_dispatch("core_restart", {"uid": str(pms_uid or "").strip()})

    def papi_switch_node(self, pms_uid: str) -> PmsApiResult:
        uid = str(pms_uid or "").strip()
        if not uid:
            return PmsApiResult.pmsar_fail("bad_request", "缺少节点 uid")
        return self.papi_dispatch("node_switch", {"uid": uid})

    def papi_set_system_proxy(self, pms_enabled: Optional[bool]) -> PmsApiResult:
        """开关系统代理；`pms_enabled` 为 None 表示请求里没给 enabled。"""
        if pms_enabled is None:
            return PmsApiResult.pmsar_fail("bad_request", "缺少 enabled 字段（true / false）")
        return self.papi_dispatch("system_proxy", {"enabled": bool(pms_enabled)})

    def papi_set_tun(self, pms_enabled: Optional[bool]) -> PmsApiResult:
        """开关 TUN；`pms_enabled` 为 None 表示请求里没给 enabled。"""
        if pms_enabled is None:
            return PmsApiResult.pmsar_fail("bad_request", "缺少 enabled 字段（true / false）")
        return self.papi_dispatch("tun", {"enabled": bool(pms_enabled)})

    def papi_node_add(self, pms_payload: dict) -> PmsApiResult:
        payload = dict(pms_payload or {})
        if not payload:
            return PmsApiResult.pmsar_fail("bad_request", "缺少节点内容：给 link（分享链接）或节点字段")
        return self.papi_dispatch("node_add", payload)

    def papi_node_update(self, pms_uid: str, pms_payload: dict) -> PmsApiResult:
        uid = str(pms_uid or "").strip()
        if not uid:
            return PmsApiResult.pmsar_fail("bad_request", "缺少节点 uid")
        payload = dict(pms_payload or {})
        if not payload:
            return PmsApiResult.pmsar_fail("bad_request", "缺少要修改的字段")
        payload["uid"] = uid
        return self.papi_dispatch("node_update", payload)

    def papi_node_delete(self, pms_uid: str) -> PmsApiResult:
        uid = str(pms_uid or "").strip()
        if not uid:
            return PmsApiResult.pmsar_fail("bad_request", "缺少节点 uid")
        return self.papi_dispatch("node_delete", {"uid": uid})

    # ------------------------------------------------------------------ 测速（v1.6.3）
    def papi_test_latency(
        self,
        pms_uids: Optional[Sequence[str]] = None,
        pms_scope: str = "all",
        pms_include_speed: bool = False,
        pms_wait: bool = False,
        pms_timeout_ms: int = 0,
    ) -> PmsApiResult:
        """发起延迟测试；`pms_wait=True` 时会等到测完（上限 120 秒，超时返回部分结果）。"""
        payload: dict[str, Any] = {"scope": str(pms_scope or "all"), "include_speed": bool(pms_include_speed)}
        if pms_uids:
            payload["uids"] = [str(item) for item in pms_uids]
        if pms_wait:
            payload["wait"] = True
            payload["timeout_ms"] = int(pms_timeout_ms or 0)
        return self.papi_dispatch("test_latency", payload)

    def papi_test_speed(
        self,
        pms_uids: Optional[Sequence[str]] = None,
        pms_scope: str = "all",
        pms_wait: bool = False,
        pms_timeout_ms: int = 0,
    ) -> PmsApiResult:
        payload: dict[str, Any] = {"scope": str(pms_scope or "all")}
        if pms_uids:
            payload["uids"] = [str(item) for item in pms_uids]
        if pms_wait:
            payload["wait"] = True
            payload["timeout_ms"] = int(pms_timeout_ms or 0)
        return self.papi_dispatch("test_speed", payload)

    def papi_test_stop(self) -> PmsApiResult:
        return self.papi_dispatch("test_stop", {})

    def papi_test_resume(self) -> PmsApiResult:
        """继续上一次未完成的测试（v1.8.0 断点续跑，已测过的不会重测）。"""
        return self.papi_dispatch("test_resume", {})

    def papi_test_status(self) -> PmsApiResult:
        """测试是否在跑（**只读**，不需要写操作开关）。"""
        state = self.papi_state()
        return PmsApiResult.pmsar_ok_with({
            "running": bool(state.get("test_running", False)),
            "write_enabled": self.papi_write_enabled(),
        })

    # ------------------------------------------------------------------ 导入（v1.6.3）
    def papi_nodes_import(self, pms_payload: dict) -> PmsApiResult:
        payload = dict(pms_payload or {})
        if not str(payload.get("text") or "").strip() and not payload.get("links"):
            return PmsApiResult.pmsar_fail("bad_request", "缺少 text（正文）或 links（链接数组）")
        return self.papi_dispatch("nodes_import", payload)

    # ------------------------------------------------------------------ 导出（v1.6.3，只读）
    def papi_export_links(self, pms_group: str = "", pms_base64: bool = False) -> PmsApiResult:
        """导出分享链接 / base64 订阅正文（**含真实口令**，见文档风险备忘）。"""
        payload: dict[str, Any] = {"base64": bool(pms_base64)}
        if str(pms_group or "").strip():
            payload["group"] = str(pms_group).strip()
        return self.papi_dispatch_action("export_links", payload, pms_require_write=False)

    def papi_export_backup(self) -> PmsApiResult:
        """导出完整备份 JSON（节点 + 分组 + 订阅，**含真实口令**）。"""
        return self.papi_dispatch_action("export_backup", {}, pms_require_write=False)

    def papi_export_clash(self, pms_group: str = "") -> PmsApiResult:
        """导出 Clash YAML（**含真实口令**）。"""
        payload: dict[str, Any] = {}
        if str(pms_group or "").strip():
            payload["group"] = str(pms_group).strip()
        return self.papi_dispatch_action("export_clash", payload, pms_require_write=False)

    def papi_export_singbox(self, pms_group: str = "") -> PmsApiResult:
        """导出 sing-box 出站 JSON（**含真实口令**）。"""
        payload: dict[str, Any] = {}
        if str(pms_group or "").strip():
            payload["group"] = str(pms_group).strip()
        return self.papi_dispatch_action("export_singbox", payload, pms_require_write=False)

    # ------------------------------------------------------------------ 事件推送（v1.8.0）
    def papi_event_seq_value(self) -> int:
        """把计数器读成**安全的 int**（v1.9.49）：被盖成函数 / 字符串时回落 0 并告警。

        这样即使有人把 `papi_event_seq` 写坏，事件本身仍然发得出去（序号退化成 0），
        而不是让 `int(...)` 在工作线程 / 界面线程里抛 `TypeError` 把调用方带崩。
        """
        value = self.papi_event_seq
        if isinstance(value, bool) or not isinstance(value, int):
            logger.warning(
                "计数器 papi_event_seq 不是 int（现在是 %s），本次按 0 记录事件序号——"
                "请检查是不是有人把它改成了函数 / 字符串（见文档第 23 条）", type(value).__name__,
            )
            return 0
        return int(value)

    def papi_emit_event(self, pms_kind: str, pms_data: Optional[dict] = None) -> dict:
        """记一条事件并返回它；`pms_kind` 见 `PMS_API_EVENT_KINDS`（不在清单里也收，方便以后扩展）。

        **只应在主线程（界面线程）调用**：写的是 deque 的 append（原子操作），
        但事件里的 data 由调用方在主线程组装，这样 HTTP 线程只读、不会读到半截状态。
        v1.9.54：同一次调用里**顺手落库兜底**（`papi_events_persist`），
        HTTP 线程只读不写这条约束一个字都没改——写口仍然只有主线程这一个。
        """
        kind = str(pms_kind or "").strip()
        if not kind:
            return {}
        self.papi_emit_event_guarded()
        event = {
            "seq": self.papi_event_seq_value(),
            "time": pms_now_text(),
            "kind": kind,
            "data": dict(pms_data or {}),
        }
        self.papi_events.append(event)
        self.papi_events_persist(event)
        return event

    def papi_events_persist(self, pms_event: dict) -> bool:
        """把一条事件写进落库兜底（v1.9.54，审计表第 22 项）。**只应在主线程调用。**

        为什么要它：事件环只有 `PMS_API_EVENT_MAX`（200）条，客户端断线超过这个数，
        `?since=` 就再也补不回来了（审计表第 22 项定性「SSE 是通知不是队列」）。
        落库后即便环已经滚掉，也能按 seq 从 `pms_meta` 里把它捞回来。

        **写失败绝不影响事件本身**：环里那条已经进 deque 了，这里只 `logger.warning`。
        """
        if not PMS_API_EVENT_PERSIST:
            return False
        repo = self.papi_repo
        if repo is None or not pms_event:
            return False
        try:
            seq = int(pms_event.get("seq") or 0)
        except (TypeError, ValueError):
            return False
        try:
            repo.pnr_event_log_append(seq, json.dumps(dict(pms_event), ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 落库是兜底能力，写不进去也不能拖垮界面
            logger.warning("事件落库失败（seq=%s，本次只进内存环）：%s: %s", seq, type(exc).__name__, exc)
            return False
        # 上限与清理：每落一条顺手裁一次（超过上限时删最旧的那批），别让 pms_meta 无限涨
        try:
            removed = repo.pnr_event_log_prune(PMS_EVENT_LOG_MAX)
            if removed:
                logger.debug("事件落库超上限，已清理最旧的 %d 条", removed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("事件落库清理失败（不影响推送，只是库会偏大）：%s: %s", type(exc).__name__, exc)
        return True

    def papi_events_persisted_since(self, pms_seq: int, pms_limit: int = 0) -> list[dict]:
        """从**落库**里取 seq 大于 `pms_seq` 的事件（v1.9.54）。

        **线程安全**：本方法**只读**（`pms_meta` 的 SELECT），HTTP 线程调用它符合
        「主线程写、HTTP 线程只读」的既有约束；写入口仍然只有 `papi_emit_event()` 一条。
        """
        repo = self.papi_repo
        if repo is None or not PMS_API_EVENT_PERSIST:
            return []
        events: list[dict] = []
        try:
            for raw in repo.pnr_event_log_load(pms_seq, pms_limit):
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(data, dict) and str(data.get("kind") or ""):
                    events.append(data)
        except Exception as exc:  # noqa: BLE001 - 补发失败不能让 SSE 断掉
            logger.warning("读取事件落库失败（本次不补发）：%s: %s", type(exc).__name__, exc)
            return []
        return events

    def papi_events_window(self, pms_seq: int = 0, pms_limit: int = 0) -> list[dict]:
        """取 seq 大于 `pms_seq` 的事件，**环 + 落库合起来**，按 seq 升序去重（v1.9.54）。

        这就是「客户端落后超过环容量时仍能补齐」的那条路径：
        环里还有的就用环里的（最新、一定完整），环里已经滚掉的从落库里补。
        """
        try:
            since = int(pms_seq)
        except (TypeError, ValueError):
            since = 0
        merged: dict[int, dict] = {}
        for item in self.papi_events_persisted_since(since, pms_limit):
            merged[int(item.get("seq") or 0)] = item
        for item in self.papi_events_since(since):
            merged[int(item.get("seq") or 0)] = item      # 环里的覆盖库里的（内容更新，且一定完整）
        ordered = [merged[key] for key in sorted(merged) if key > since]
        if pms_limit and len(ordered) > int(pms_limit):
            ordered = ordered[-int(pms_limit):]
        return ordered

    def papi_emit_event_guarded(self) -> bool:
        """计数器 +1 之前的**防呆自检**（v1.9.49，审计表第 23 项）：过检才 `+1`，返回是否成功。

        自检不过就大声告警并跳过 +1（事件照发，只是序号不前进）——老写法是裸露的
        `self.papi_event_seq += 1`，属性一旦被写坏就直接 `TypeError`（v1.8.0 的原坑）。
        """
        value = self.papi_event_seq
        shadow = getattr(type(self), "papi_event_seq", None)
        problems: list[str] = []
        if callable(shadow) and not callable(value):
            problems.append(
                f"类 {type(self).__name__} 上出现了同名方法 papi_event_seq()——"
                "它已被实例属性盖住、永远不会被调用（v1.8.0 的老坑）"
            )
        if callable(value):
            problems.append(f"计数器属性 papi_event_seq 被盖成了可调用对象（{type(value).__name__}）")
        elif isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"计数器属性 papi_event_seq 不再是 int（现在是 {type(value).__name__}）")
        elif value < 0:
            problems.append(f"计数器属性 papi_event_seq 变成负数（{value}）")
        if not problems:
            # 自检过了：**这里才做那次 +1**（老写是裸露的 `self.papi_event_seq += 1`，
            # 属性一旦被写坏就直接抛 TypeError；现在先自检、再自增）
            try:
                self.papi_event_seq = int(value) + 1
            except (AttributeError, TypeError, ValueError) as exc:
                # property 没有 setter / 被 __slots__ 之类挡住：告警但不抛，事件照发
                logger.warning("事件序号自增失败（计数器写不进去）：%s: %s", type(exc).__name__, exc)
                return False
            return True
        for detail in problems:
            logger.warning(
                "事件序号自检失败，本次不 +1（老写是直接 `self.papi_event_seq += 1`，"
                "会抛 TypeError 或悄悄写坏序号）：%s", detail,
            )
        return False

    def papi_events_since(self, pms_seq: int = 0) -> list[dict]:
        """取 seq 大于 `pms_seq` 的事件；`pms_seq<=0` 时返回当前缓存里的全部事件。

        客户端断线重连时带上自己最后收到的 seq，就能接着推，不会漏也不会重复。
        """
        try:
            since = int(pms_seq)
        except (TypeError, ValueError):
            since = 0
        return [item for item in tuple(self.papi_events) if int(item.get("seq") or 0) > since]

    def papi_event_last_seq(self) -> int:
        """当前最新事件的 seq（0 = 还没有任何事件）。

        名字特意带 `last_`：实例上已有 `papi_event_seq` 这个**计数器属性**，同名会把方法盖掉。
        v1.9.49：走 `papi_event_seq_value()`，计数器被人写坏时回落 0 并告警，**不再抛 TypeError**
        （这方法是 SSE 握手与面板轮询都要调的，崩在这里最难看）。
        """
        return self.papi_event_seq_value()

    def papi_events_recent(self, pms_since: int = 0, pms_limit: int = 40) -> PmsApiResult:
        """最近事件 + 最新 seq（v1.9.35，给本地 Web 面板用）。

        为什么要这个：SSE 长连接（`/api/v1/events`）要靠 `Authorization` 头带 token，
        而浏览器的 `EventSource` **不能自定义请求头**——面板只能走普通 JSON 接口轮询。
        于是这里给一个「取最近 N 条」的只读接口；`since` 语义与 SSE 完全一致（只返回更新的事件）。

        v1.9.54：`since > 0` 时改成走「环 + 落库」合并窗口，客户端落后超过环容量也能补齐；
        `since` 不传（=0）时行为**与改动前完全一致**——只看内存环，不碰数据库。
        """
        try:
            limit = int(pms_limit)
        except (TypeError, ValueError):
            limit = 40
        limit = max(1, min(200, limit))
        try:
            since = int(pms_since)
        except (TypeError, ValueError):
            since = 0
        if since > 0:
            events = self.papi_events_window(since, limit)
        else:
            events = self.papi_events_since(0)[-limit:]
        return PmsApiResult.pmsar_ok_with({"events": events, "last_seq": self.papi_event_last_seq()})

    # ------------------------------------------------------------------ 字段脱敏
    def papi_public_node(self, pms_node: Any) -> dict[str, Any]:
        """节点 → 可对外输出的字典：去掉口令类字段，并补一个 `share_ready` 标记。"""
        data = dict(pms_node.pnode_to_dict())
        for field_name in PMS_API_HIDDEN_NODE_FIELDS:
            data.pop(field_name, None)
        data["has_credential"] = bool(str(getattr(pms_node, "credential_password", "") or getattr(pms_node, "credential_uuid", "") or "").strip())
        return data

    def papi_public_nodes(self, pms_nodes: Sequence[Any]) -> list[dict[str, Any]]:
        return [self.papi_public_node(item) for item in pms_nodes]

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def papi_is_loopback(pms_host: str) -> bool:
        host = str(pms_host or "").strip().lower()
        return host in PMS_API_LOOPBACK_HOSTS


# --------------------------------------------------------------------------------------
# v1.9.49（审计表第 23 项）：`papi_event_seq` 命名坑的防呆自检
# 这两个函数必须在 `PmsApi` **定义之后**（默认参数里的类名是定义期求值的）。
# --------------------------------------------------------------------------------------
def pms_api_event_seq_check(pms_api_cls: Optional[type] = None) -> bool:
    """**启动即自检**：`papi_event_seq` 这个命名坑还没被重新踩上（v1.9.49，审计表第 23 项）。

    三件事任一不成立就 `logger.warning` 并返回 False（**绝不静默**）：
    类上不能有同名方法 / property（会被实例属性盖住，v1.8.0 的 TypeError 原坑）；
    计数器名不能与取序号方法名相同；`papi_event_last_seq()` 必须还在且可调用。
    再真造一个最小实例，确认计数器确实以 int 落在实例上（类属性上看不到它）。
    `pms_api_cls` 省掉就是本模块的 `PmsApi`（函数体里解析，不在定义期求值）。
    """
    cls = pms_api_cls if pms_api_cls is not None else PmsApi
    problems: list[str] = []
    attr = cls.__dict__.get(PMS_API_EVENT_SEQ_ATTR, None)
    if callable(attr) and not isinstance(attr, property):
        problems.append(
            f"{cls.__name__}.{PMS_API_EVENT_SEQ_ATTR} 是**方法**（callable）——"
            "它会被实例属性盖住、永远不被调用，而计数一旦走回类属性就会 TypeError"
        )
    elif isinstance(attr, property):
        problems.append(
            f"{cls.__name__}.{PMS_API_EVENT_SEQ_ATTR} 被改成了 property——"
            "实例上再赋值会抛 AttributeError（事件计数写不进去）"
        )
    if not callable(getattr(cls, PMS_API_EVENT_SEQ_METHOD, None)):
        problems.append(f"{cls.__name__}.{PMS_API_EVENT_SEQ_METHOD}() 不存在或不可调用（取序号没了入口）")
    if PMS_API_EVENT_SEQ_ATTR == PMS_API_EVENT_SEQ_METHOD:
        problems.append("计数器属性名与取序号方法名相同——同名会把方法盖掉（v1.8.0 的老坑）")
    if not problems:
        # 真造一个最小实例，确认计数器真的以 int 落在实例上（类上看不到它）
        try:
            instance = cls.__new__(cls)
            cls.__init__(instance, None, None, None)
            if not pms_api_event_seq_guard(instance):
                problems.append("最小实例上的计数器不是可自增的 int（详见上一条告警）")
        except Exception as exc:  # noqa: BLE001 - 自检自己不许把导入带崩，但要说出来
            problems.append(f"最小实例自检跑不起来：{type(exc).__name__}: {exc}")
    if not problems:
        return True
    for detail in problems:
        logger.warning(
            "事件序号命名自检失败：%s。事件序号是 SSE / 面板断线续传的起点，"
            "请把计数器属性与取序号方法的名字改回不同值（见文档第 23 条）", detail,
        )
    return False


def pms_api_event_seq_guard(pms_api: Any) -> bool:
    """对一个**已建好的实例**做同样的自检（子类 / 装配现场把属性写坏时用），返回 True = 没问题。"""
    problems: list[str] = []
    value = getattr(pms_api, PMS_API_EVENT_SEQ_ATTR, None)
    if value is None:
        problems.append(f"实例上没有 {PMS_API_EVENT_SEQ_ATTR}")
    elif callable(value):
        problems.append(f"实例属性 {PMS_API_EVENT_SEQ_ATTR} 被盖成了可调用对象（{type(value).__name__}）")
    elif isinstance(value, bool) or not isinstance(value, int):
        problems.append(f"实例属性 {PMS_API_EVENT_SEQ_ATTR} 不是 int（现在是 {type(value).__name__}）")
    if not callable(getattr(pms_api, PMS_API_EVENT_SEQ_METHOD, None)):
        problems.append(f"实例上取不到 {PMS_API_EVENT_SEQ_METHOD}()")
    if not problems:
        return True
    for detail in problems:
        logger.warning("事件序号实例自检失败：%s", detail)
    return False


# 模块导入即自检：谁把这个命名改坏，程序一启动日志里就有一条 warning（绝不静默）。
pms_api_event_seq_check()


