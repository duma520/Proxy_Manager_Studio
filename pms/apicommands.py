#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外接口的写操作派发（v1.6.2）

为什么单独一个文件：
    1. 写操作（启停核心、切节点、系统代理、TUN、节点增删）**必须在界面线程执行**——
       HTTP 请求跑在 `ThreadingHTTPServer` 的工作线程里，直接调主窗口方法会踩 Qt 线程规则；
    2. 主窗口已经 2800+ 行（超宪法红线），动作表不能再往里堆，因此单独成模块；
    3. 派发器只认「主窗口对象」，探针可以用假窗口对象驱动，离屏也能测完整链路。

执行链路：
    工作线程 `PmsApiCommander.pcmd_execute(action, payload)`
        → 发 Qt 信号（跨线程自动排队）
        → 界面线程 `PmsApiDispatcher.papd_on_command()` 执行动作
        → `PmsApiCommander.pcmd_finish(request_id, result)` 置事件
        → 工作线程等事件返回（超时默认 5 秒，**绝不死等**）

安全约定：
    1. 动作名走 `PMS_API_WRITE_ACTIONS` **白名单**，不在名单里一律拒绝；
    2. 每个动作先做前置校验，**只有确认不会弹模态框了才调主窗口方法**
       （模态框在无人点击时会把接口请求一直挂住，这是本模块最重要的防坑点）；
    3. 节点口令字段不出现在任何返回值里。
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Optional,
)
from PySide6.QtCore import (
    QObject,
    QTimer,
    Signal,
)
import logging
import threading
import time
import uuid

from pms.constants import (
    PMS_API_READ_ACTIONS,
    PMS_API_WAIT_DEFAULT_TIMEOUT_MS,
    PMS_API_WAIT_GRACE_MS,
    PMS_API_WAIT_MAX_TIMEOUT_MS,
    PMS_API_WAIT_MIN_TIMEOUT_MS,
    PMS_API_WAIT_POLL_INTERVAL_MS,
    PMS_API_WRITE_ACTIONS,
    PMS_API_WRITE_MAX_TIMEOUT_MS,
    PMS_API_WRITE_MIN_TIMEOUT_MS,
    PMS_API_WRITE_TIMEOUT_MS,
    PMS_CORE_KIND_SINGBOX,
    PMS_LOGGER_NAME,
    PMS_TEST_MODE_TCP,
    PMS_TEST_MODE_URL,
)
from pms.coresys import PmsPrivilegeUtils
from pms.importers import PmsTextImportClassifier
from pms.sharelinks import PmsShareLinkParser

# 注意：这里**只能**用 TYPE_CHECKING——主窗口会导入 api / apicommands，
# 若在运行时 import 主窗口就会形成循环导入（v1.6.2 开工第一坑，已在此堵死）
if TYPE_CHECKING:
    from pms.gui.mainwindow import PmsMainWindow
    from pms.models import PmsProxyNode


# --------------------------------------------------------------------------------------
# 结果字典（用 dict 而不是再定义一个结果类：接口层统一转成 PmsApiResult，避免两套返回体）
# --------------------------------------------------------------------------------------
def pms_api_cmd_ok(pms_data: Any = None) -> dict[str, Any]:
    return {"ok": True, "code": "", "message": "", "data": pms_data}


def pms_api_cmd_fail(pms_code: str, pms_message: str) -> dict[str, Any]:
    return {"ok": False, "code": str(pms_code), "message": str(pms_message), "data": None}


# --------------------------------------------------------------------------------------
# 派发器：工作线程 → 界面线程
# --------------------------------------------------------------------------------------
class PmsApiCommander(QObject):
    """把写操作请求派发到界面线程，并把结果带回来。

    构造在界面线程（主窗口创建它），工作线程调 `pcmd_execute()`。
    Qt 的自动连接会判断「发信号的线程 ≠ 接收者所在线程」从而排队，天然安全。
    """

    pcmd_requested = Signal(str, str, dict)   # request_id, action, payload

    def __init__(self, pms_parent: Optional[QObject] = None) -> None:
        super().__init__(pms_parent)
        self.pcmd_timeout_ms = int(PMS_API_WRITE_TIMEOUT_MS)
        self.pcmd_logger = logging.getLogger(PMS_LOGGER_NAME + ".apicommand")
        self.pcmd_lock = threading.Lock()
        self.pcmd_events: dict[str, threading.Event] = {}
        self.pcmd_results: dict[str, dict[str, Any]] = {}
        self.pcmd_seq = 0

    # ------------------------------------------------------------------ 配置
    def pcmd_set_timeout_ms(self, pms_timeout_ms: int) -> None:
        """设置等待超时（毫秒），夹在 500 ms ~ 60 秒之间。"""
        try:
            value = int(pms_timeout_ms)
        except (TypeError, ValueError):
            value = int(PMS_API_WRITE_TIMEOUT_MS)
        self.pcmd_timeout_ms = max(int(PMS_API_WRITE_MIN_TIMEOUT_MS), min(int(PMS_API_WRITE_MAX_TIMEOUT_MS), value))

    def pcmd_is_allowed_action(self, pms_action: str) -> bool:
        """写动作 + 只读动作两份白名单（导出是只读，但也要走界面线程，故同样受白名单约束）。"""
        action = str(pms_action or "")
        return action in PMS_API_WRITE_ACTIONS or action in PMS_API_READ_ACTIONS

    def pcmd_pending_count(self) -> int:
        with self.pcmd_lock:
            return len(self.pcmd_events)

    # ------------------------------------------------------------------ 执行
    def pcmd_execute(
        self,
        pms_action: str,
        pms_payload: Optional[dict] = None,
        pms_timeout_ms: int = 0,
    ) -> dict[str, Any]:
        """发起一次写操作并等待结果；超时返回 `timeout`，绝不无限等待。"""
        action = str(pms_action or "")
        if not self.pcmd_is_allowed_action(action):
            return pms_api_cmd_fail("action_not_allowed", f"不允许的写操作：{action or '空'}")
        with self.pcmd_lock:
            self.pcmd_seq += 1
            request_id = f"{self.pcmd_seq}-{uuid.uuid4().hex[:12]}"
            event = threading.Event()
            self.pcmd_events[request_id] = event
        self.pcmd_requested.emit(request_id, action, dict(pms_payload or {}))
        timeout_ms = int(pms_timeout_ms) if int(pms_timeout_ms or 0) > 0 else self.pcmd_timeout_ms
        event.wait(max(0.05, float(timeout_ms) / 1000.0))
        with self.pcmd_lock:
            result = self.pcmd_results.pop(request_id, None)
            self.pcmd_events.pop(request_id, None)
        if result is None:
            return pms_api_cmd_fail("timeout", f"写操作 {action} 在 {timeout_ms} ms 内没有返回结果")
        return result

    def pcmd_finish(self, pms_request_id: str, pms_result: Optional[dict]) -> None:
        """由执行方（界面线程）回填结果并放行等待中的工作线程。"""
        request_id = str(pms_request_id or "")
        with self.pcmd_lock:
            event = self.pcmd_events.get(request_id)
            if event is None:
                return
            self.pcmd_results[request_id] = dict(pms_result or {})
            event.set()


# --------------------------------------------------------------------------------------
# 动作表：真正调用主窗口
# --------------------------------------------------------------------------------------
class PmsApiDispatcher(QObject):
    """把 action 映射到主窗口方法；**运行在界面线程**。"""

    papd_message = Signal(str)     # 一行日志（主窗口接到后写日志面板）

    def __init__(
        self,
        pms_window: "PmsMainWindow",
        pms_commander: PmsApiCommander,
        pms_parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.papd_window = pms_window
        self.papd_commander = pms_commander
        self.papd_logger = logging.getLogger(PMS_LOGGER_NAME + ".apidispatch")
        # handler 返回 dict = 立即完成；返回 **None** = 稍后由 `pcmd_finish()` 收尾（测速等待用）
        self.papd_handlers: dict[str, Callable[[dict, str], Optional[dict[str, Any]]]] = {
            "core_start": self.papd_core_start,
            "core_stop": self.papd_core_stop,
            "core_restart": self.papd_core_restart,
            "node_switch": self.papd_node_switch,
            "system_proxy": self.papd_system_proxy,
            "tun": self.papd_tun,
            "node_add": self.papd_node_add,
            "node_update": self.papd_node_update,
            "node_delete": self.papd_node_delete,
            "test_latency": self.papd_test_latency,
            "test_speed": self.papd_test_speed,
            "test_stop": self.papd_test_stop,
            "test_resume": self.papd_test_resume,
            "nodes_import": self.papd_nodes_import,
            "export_links": self.papd_export_links,
            "export_backup": self.papd_export_backup,
            "export_clash": self.papd_export_clash,
            "export_singbox": self.papd_export_singbox,
        }
        self.papd_waits: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 入口
    def papd_on_command(self, pms_request_id: str, pms_action: str, pms_payload: dict) -> None:
        """`PmsApiCommander.pcmd_requested` 的槽（界面线程）。

        返回 None 表示「稍后完成」——**这里绝不 finish**，由等待计时器收尾。
        """
        result = self.papd_run(str(pms_action or ""), dict(pms_payload or {}), str(pms_request_id or ""))
        if result is not None and self.papd_commander is not None:
            self.papd_commander.pcmd_finish(pms_request_id, result)

    def papd_run(self, pms_action: str, pms_payload: dict, pms_request_id: str = "") -> Optional[dict[str, Any]]:
        handler = self.papd_handlers.get(str(pms_action or ""))
        if handler is None:
            return pms_api_cmd_fail("action_not_allowed", f"不允许的动作：{pms_action or '空'}")
        try:
            return handler(dict(pms_payload or {}), str(pms_request_id or ""))
        except Exception as exc:  # noqa: BLE001 - 单个动作出错不能让接口线程崩掉
            self.papd_logger.exception("执行动作出错（%s）：%s", pms_action, exc)
            return pms_api_cmd_fail("server_error", f"执行 {pms_action} 失败：{exc}")

    # ------------------------------------------------------------------ 取节点
    def papd_find_node(self, pms_uid: str) -> Optional["PmsProxyNode"]:
        uid = str(pms_uid or "").strip()
        window = self.papd_window
        if not uid:
            return window.pmw_current_node()
        try:
            return window.pmw_repo.pnr_load_node(uid)
        except Exception:  # noqa: BLE001
            return None

    def papd_public_node(self, pms_node: "PmsProxyNode") -> dict[str, Any]:
        """只回 uid / 名称 / 协议 / 地址，**不带任何口令字段**。"""
        return {
            "uid": str(pms_node.uid),
            "name": str(pms_node.name or ""),
            "protocol": str(pms_node.protocol or ""),
            "server": str(pms_node.server or ""),
            "port": int(pms_node.port or 0),
            "enabled": bool(pms_node.enabled),
            "group_name": str(pms_node.group_name or ""),
        }

    # ------------------------------------------------------------------ 核心
    def papd_core_start(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        window = self.papd_window
        if window.pmw_core.pcp_is_running():
            return pms_api_cmd_ok({"running": True, "note": "核心已在运行，未重复启动"})
        node = self.papd_find_node(str(pms_payload.get("uid") or ""))
        if node is None:
            node = window.pmw_current_node()
        if node is None:
            candidates = window.pmw_selected_nodes() or window.pmw_visible_nodes() or list(window.pmw_nodes)
            if not candidates:
                return pms_api_cmd_fail("no_node", "还没有节点，请先新增或导入节点")
            node = candidates[0]
        errors = node.pnode_validate()
        if errors:
            return pms_api_cmd_fail("invalid_node", "；".join(errors))
        # 前置校验：核心缺失 / 配置校验失败都会让 pmw_start_core 弹模态框，这里先挡掉
        core_kind, core_path = window.pmw_resolve_core()
        if core_kind is None or core_path is None:
            return pms_api_cmd_fail("core_not_found", "没有找到 xray.exe / sing-box.exe，请先下载核心")
        profile = window.pmw_build_profile(node, core_kind)
        verify_errors = profile.pclp_verify()
        if verify_errors:
            return pms_api_cmd_fail("invalid_profile", "；".join(verify_errors))
        ok = window.pmw_start_core(node)
        if not ok:
            return pms_api_cmd_fail("start_failed", "核心启动失败，详见程序日志")
        return pms_api_cmd_ok({"running": True, "node": self.papd_public_node(node)})

    def papd_core_stop(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        self.papd_window.pmw_stop_core()
        return pms_api_cmd_ok({"running": False})

    def papd_core_restart(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        window = self.papd_window
        window.pmw_stop_core()
        return self.papd_core_start(dict(pms_payload or {}))

    def papd_node_switch(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        uid = str(pms_payload.get("uid") or "").strip()
        if not uid:
            return pms_api_cmd_fail("bad_request", "缺少节点 uid")
        node = self.papd_find_node(uid)
        if node is None:
            return pms_api_cmd_fail("node_not_found", f"没有这个节点：{uid}")
        errors = node.pnode_validate()
        if errors:
            return pms_api_cmd_fail("invalid_node", "；".join(errors))
        # pmw_connect_node 内部已含「先停后起」；uid 校验过就不会再弹模态框
        self.papd_window.pmw_connect_node(node)
        return pms_api_cmd_ok({"uid": uid, "node": self.papd_public_node(node)})

    # ------------------------------------------------------------------ 系统代理 / TUN
    def papd_system_proxy(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        if "enabled" not in pms_payload:
            return pms_api_cmd_fail("bad_request", "缺少 enabled 字段（true / false）")
        enabled = bool(pms_payload.get("enabled"))
        window = self.papd_window
        if enabled and not window.pmw_core.pcp_is_running():
            return pms_api_cmd_fail("core_not_running", "系统代理需要一个正在运行的本地入口，请先启动核心")
        if enabled and window.pmw_core.pcp_profile is None:
            return pms_api_cmd_fail("core_not_running", "核心尚未生成可用配置，无法开启系统代理")
        window.pmw_settings.psm_set("system_proxy.enabled", enabled)
        window.pmw_apply_system_proxy(enabled)
        return pms_api_cmd_ok({"system_proxy": enabled})

    def papd_tun(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        if "enabled" not in pms_payload:
            return pms_api_cmd_fail("bad_request", "缺少 enabled 字段（true / false）")
        enabled = bool(pms_payload.get("enabled"))
        window = self.papd_window
        if enabled:
            # 前置校验挡掉 pmw_toggle_tun 里的两个模态框（非 sing-box / 非管理员）
            core_kind, _ = window.pmw_resolve_core()
            if core_kind != PMS_CORE_KIND_SINGBOX:
                return pms_api_cmd_fail("tun_unsupported", "TUN 模式需要 sing-box 核心（Xray 不支持 TUN 入站）")
            if not PmsPrivilegeUtils.ppu_is_admin():
                return pms_api_cmd_fail("need_admin", "TUN 模式需要管理员权限，请以管理员身份重新启动程序")
        window.pmw_toggle_tun(enabled)
        return pms_api_cmd_ok({"tun": enabled})

    # ------------------------------------------------------------------ 节点增删
    def papd_node_add(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        window = self.papd_window
        link = str(pms_payload.get("link") or "").strip()
        if link:
            node, error = PmsShareLinkParser().pslp_parse_line(link)
            if node is None:
                return pms_api_cmd_fail("invalid_link", error or "无法解析这个分享链接")
        else:
            from pms.models import PmsProxyNode   # 局部导入：避免与模型模块循环依赖
            data = {key: value for key, value in dict(pms_payload).items() if key != "link"}
            if not str(data.get("server") or "").strip():
                return pms_api_cmd_fail("bad_request", "缺少 server（服务器地址）；也可以直接给 link 分享链接")
            node = PmsProxyNode.pnode_from_dict(data)
            if not str(node.uid or "").strip():
                node.uid = uuid.uuid4().hex
            if not str(node.name or "").strip():
                node.name = f"{node.server}:{node.port}"
        errors = node.pnode_validate()
        if errors:
            return pms_api_cmd_fail("invalid_node", "；".join(errors))
        window.pmw_repo.pnr_upsert_node(node)
        # v1.9.72 卡顿修复：单节点新增走增量刷新，不整库重载
        window.pmw_apply_nodes_added([node], node.uid)
        return pms_api_cmd_ok({"node": self.papd_public_node(node)})

    def papd_node_update(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        uid = str(pms_payload.get("uid") or "").strip()
        if not uid:
            return pms_api_cmd_fail("bad_request", "缺少节点 uid")
        window = self.papd_window
        node = self.papd_find_node(uid)
        if node is None:
            return pms_api_cmd_fail("node_not_found", f"没有这个节点：{uid}")
        merged = node.pnode_to_dict()
        for key, value in dict(pms_payload).items():
            if key in ("uid", "link"):
                continue
            merged[key] = value
        merged["uid"] = uid
        from pms.models import PmsProxyNode   # 局部导入：只在执行时用到
        updated = PmsProxyNode.pnode_from_dict(merged)
        errors = updated.pnode_validate()
        if errors:
            return pms_api_cmd_fail("invalid_node", "；".join(errors))
        window.pmw_repo.pnr_upsert_node(updated)
        # v1.9.72 卡顿修复：papd_node_update 已 update，这里不再冗余全量重载
        window.pmw_update_node_everywhere(updated)
        return pms_api_cmd_ok({"node": self.papd_public_node(updated)})

    def papd_node_delete(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        uid = str(pms_payload.get("uid") or "").strip()
        if not uid:
            return pms_api_cmd_fail("bad_request", "缺少节点 uid")
        window = self.papd_window
        removed = int(window.pmw_repo.pnr_delete_nodes([uid]))
        if removed <= 0:
            return pms_api_cmd_fail("node_not_found", f"没有这个节点：{uid}")
        # v1.9.72 卡顿修复：单节点删除走增量删除，不整库重载
        window.pmw_apply_nodes_removed([uid])
        return pms_api_cmd_ok({"uid": uid, "removed": removed})

    # ------------------------------------------------------------------ 测速（v1.6.3）
    def papd_resolve_test_nodes(self, pms_payload: dict) -> list["PmsProxyNode"]:
        """按 `uids` 或 `scope` 挑出要测的节点；挑不到返回空列表。"""
        window = self.papd_window
        raw_uids = pms_payload.get("uids")
        if isinstance(raw_uids, (list, tuple)) and raw_uids:
            nodes: list["PmsProxyNode"] = []
            for item in raw_uids:
                node = self.papd_find_node(str(item))
                if node is not None:
                    nodes.append(node)
            return nodes
        scope = str(pms_payload.get("scope") or "all").strip().lower()
        if scope == "current":
            current = window.pmw_current_node()
            return [current] if current is not None else []
        if scope == "enabled":
            return [item for item in (window.pmw_visible_nodes() or list(window.pmw_nodes)) if bool(item.enabled)]
        return list(window.pmw_visible_nodes() or window.pmw_nodes)

    def papd_test_is_running(self) -> bool:
        """四种测试通道任一在跑就算在跑（延迟 URL 模式 / 真实测速 / TCP 握手 / 电报 DC，v1.9.59）。"""
        window = self.papd_window
        try:
            if window.pmw_latency.plt_is_running():
                return True
            if window.pmw_speed.pst_is_running():
                return True
            if window.pmw_telegram.ptg_is_running():
                return True
            done = int(getattr(window, "pmw_tcp_done", 0) or 0)
            total = int(getattr(window, "pmw_tcp_total", 0) or 0)
            if total > 0 and done < total:
                return True
        except Exception:  # noqa: BLE001 - 状态读不出来就当没在跑，不能让接口 500
            return False
        return False

    def papd_show_progress(self, pms_kind: str, pms_total: int, pms_message: str, pms_dialog: bool) -> None:
        """接口驱动测试时也把进度条打开（v1.9.22）。

        **必须容错**：进度条只是界面提示，窗口对象没有这个方法（探针替身 / 裁剪过的窗口）
        也不能让接口 500——本模块前面的 `pmw_tcp_done` 读取用的就是同一套 `getattr` 兜底。
        """
        begin = getattr(self.papd_window, "pmw_begin_test_progress", None)
        if callable(begin):
            begin(pms_kind, pms_total, pms_message)
        if hasattr(self.papd_window, "pmw_speed_dialog_wanted"):
            self.papd_window.pmw_speed_dialog_wanted = bool(pms_dialog)

    def papd_test_latency(self, pms_payload: dict, pms_request_id: str = "") -> Optional[dict[str, Any]]:
        window = self.papd_window
        nodes = self.papd_resolve_test_nodes(pms_payload)
        if not nodes:
            return pms_api_cmd_fail("no_node", "没有可测试的节点（检查 uids 或 scope）")
        if self.papd_test_is_running():
            return pms_api_cmd_fail("test_running", "已有测试在跑：先 POST /test/stop 或等它结束")
        mode = str(window.pmw_settings.psm_get_str("test.mode", PMS_TEST_MODE_URL) or PMS_TEST_MODE_URL)
        if mode == PMS_TEST_MODE_TCP:
            window.pmw_run_tcp_latency_test(nodes)
            used_mode = PMS_TEST_MODE_TCP
        else:
            ok, message = window.pmw_latency.plt_start(nodes, bool(pms_payload.get("include_speed")))
            if not ok:
                return pms_api_cmd_fail("start_failed", message or "延迟测试启动失败")
            # v1.9.22：接口发起的测试也要有进度条（原来这条路径没有，界面上看不到任何进度）
            self.papd_show_progress("latency", len(nodes), message, False)
            used_mode = PMS_TEST_MODE_URL
        uids = [str(item.uid) for item in nodes]
        if pms_payload.get("wait"):
            return self.papd_defer_wait(pms_request_id, uids, pms_payload.get("timeout_ms"), "latency")
        return pms_api_cmd_ok({"started": True, "count": len(uids), "uids": uids, "mode": used_mode, "wait": False})

    def papd_test_speed(self, pms_payload: dict, pms_request_id: str = "") -> Optional[dict[str, Any]]:
        window = self.papd_window
        nodes = self.papd_resolve_test_nodes(pms_payload)
        if not nodes:
            return pms_api_cmd_fail("no_node", "没有可测试的节点（检查 uids 或 scope）")
        if self.papd_test_is_running():
            return pms_api_cmd_fail("test_running", "已有测试在跑：先 POST /test/stop 或等它结束")
        ok, message = window.pmw_speed.pst_start(nodes)
        if not ok:
            return pms_api_cmd_fail("start_failed", message or "测速启动失败")
        # v1.9.22：接口发起的测速同样要有进度条；**不弹汇总框**（外部调用者不需要人盯着看）
        self.papd_show_progress("speed", len(nodes), message, False)
        uids = [str(item.uid) for item in nodes]
        if pms_payload.get("wait"):
            return self.papd_defer_wait(pms_request_id, uids, pms_payload.get("timeout_ms"), "speed")
        return pms_api_cmd_ok({"started": True, "count": len(uids), "uids": uids, "mode": "speed", "wait": False})

    def papd_test_stop(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        window = self.papd_window
        stopped: list[str] = []
        try:
            if window.pmw_latency.plt_is_running():
                window.pmw_latency.plt_stop()
                stopped.append("latency")
            if window.pmw_speed.pst_is_running():
                window.pmw_speed.pst_stop()
                stopped.append("speed")
            if window.pmw_telegram.ptg_is_running():   # v1.9.59：电报 DC 测试也能从接口停
                window.pmw_telegram.ptg_stop()
                stopped.append("telegram")
            if int(getattr(window, "pmw_tcp_total", 0) or 0) > 0:
                window.pmw_tcp_pool.clear()
                stopped.append("tcp")
        except Exception as exc:  # noqa: BLE001
            self.papd_logger.warning("停止测试时出现异常：%s", exc)
        return pms_api_cmd_ok({"stopped": stopped, "running": self.papd_test_is_running()})

    def papd_test_resume(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        """继续上一次未完成的延迟测试 / 测速（v1.8.0 断点续跑）。

        断点里记了自己的 kind 与 scope，所以这里不需要调用方再传范围——
        传了也没用，避免「按当前筛选重取」把断点里剩下的节点漏掉。
        """
        window = self.papd_window
        info = (window.pmw_latency.plt_pending_info() or window.pmw_speed.pst_pending_info()
                or window.pmw_telegram.ptg_pending_info())   # v1.9.59：电报断点也能从接口续跑
        if not info:
            return pms_api_cmd_fail("no_pending", "没有未完成的测试，无需续跑")
        if self.papd_test_is_running():
            return pms_api_cmd_fail("test_running", "已有测试在跑：先 POST /test/stop 或等它结束")
        kind = str(info.get("kind") or "")
        try:
            if kind == "latency":
                ok, message = window.pmw_latency.plt_resume(window.pmw_pick_test_nodes(str(info.get("scope") or "all")))
            elif kind == "telegram_dc":
                ok, message = window.pmw_telegram.ptg_resume(window.pmw_pick_test_nodes(str(info.get("scope") or "selected")))
            else:
                ok, message = window.pmw_speed.pst_resume(window.pmw_pick_test_nodes(str(info.get("scope") or "selected")))
        except Exception as exc:  # noqa: BLE001 - 续跑失败要变成接口错误，不能 500
            self.papd_logger.warning("续跑失败：%s", exc)
            return pms_api_cmd_fail("resume_failed", str(exc))
        if not ok:
            return pms_api_cmd_fail("resume_failed", message or "续跑失败")
        return pms_api_cmd_ok({
            "resumed": True, "kind": info.get("kind"), "scope": info.get("scope"),
            "pending": info.get("pending"), "total": info.get("total"), "message": message,
        })

    # ------------------------------------------------------------------ 等待（wait=true，界面线程 QTimer 轮询）
    def papd_defer_wait(
        self,
        pms_request_id: str,
        pms_uids: list[str],
        pms_timeout_ms: Any,
        pms_kind: str,
    ) -> Optional[dict[str, Any]]:
        """返回 None = 稍后完成：**界面线程用 QTimer 轮询，绝不阻塞界面**。"""
        if not pms_request_id:
            return pms_api_cmd_fail("bad_request", "wait 模式需要 request_id（内部错误）")
        try:
            timeout_ms = int(pms_timeout_ms or 0)
        except (TypeError, ValueError):
            timeout_ms = 0
        if timeout_ms <= 0:
            timeout_ms = int(PMS_API_WAIT_DEFAULT_TIMEOUT_MS)
        timeout_ms = max(int(PMS_API_WAIT_MIN_TIMEOUT_MS), min(int(PMS_API_WAIT_MAX_TIMEOUT_MS), timeout_ms))
        timer = QTimer(self)
        timer.setInterval(int(PMS_API_WAIT_POLL_INTERVAL_MS))
        self.papd_waits[pms_request_id] = {
            "uids": list(pms_uids),
            "kind": str(pms_kind or ""),
            "timer": timer,
            "started": time.monotonic(),
            "timeout": timeout_ms,
            "saw_running": False,
        }
        timer.timeout.connect(lambda: self.papd_poll_wait(pms_request_id))
        timer.start()
        return None

    def papd_poll_wait(self, pms_request_id: str) -> None:
        state = self.papd_waits.get(pms_request_id)
        if state is None:
            return
        elapsed_ms = (time.monotonic() - float(state["started"])) * 1000.0
        running = self.papd_test_is_running()
        if running:
            state["saw_running"] = True
        elif not state["saw_running"] and elapsed_ms < float(PMS_API_WAIT_GRACE_MS):
            return      # 刚发起，给一点「还没跑起来」的宽限
        if running and elapsed_ms < float(state["timeout"]):
            return
        self.papd_finish_wait(pms_request_id, pms_partial=running)

    def papd_finish_wait(self, pms_request_id: str, pms_partial: bool) -> None:
        state = self.papd_waits.pop(pms_request_id, None)
        if state is None:
            return
        timer = state.get("timer")
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        elapsed_ms = int((time.monotonic() - float(state["started"])) * 1000)
        if self.papd_commander is not None:
            self.papd_commander.pcmd_finish(pms_request_id, pms_api_cmd_ok({
                "results": self.papd_collect_results(list(state["uids"])),
                "count": len(state["uids"]),
                "kind": state["kind"],
                "waited_ms": elapsed_ms,
                "partial": bool(pms_partial),
                "wait": True,
            }))

    def papd_collect_results(self, pms_uids: list[str]) -> list[dict[str, Any]]:
        """从数据库读回这批节点的最新延迟 / 速度（测速结果本来就落库）。"""
        window = self.papd_window
        collected: list[dict[str, Any]] = []
        for uid in pms_uids:
            try:
                node = window.pmw_repo.pnr_load_node(str(uid))
            except Exception:  # noqa: BLE001
                node = None
            if node is None:
                collected.append({"uid": str(uid), "found": False})
                continue
            latency = getattr(node, "latency_ms", None)
            success = latency is not None and int(latency) >= 0
            collected.append({
                "uid": str(uid),
                "found": True,
                "name": str(getattr(node, "name", "") or ""),
                "server": str(getattr(node, "server", "") or ""),
                "port": int(getattr(node, "port", 0) or 0),
                "latency_ms": None if latency is None else int(latency),
                "speed_bps": float(getattr(node, "speed_bps", 0.0) or 0.0),
                "success": bool(success),
            })
        return collected

    # ------------------------------------------------------------------ 导入（v1.6.3）
    def papd_nodes_import(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        """同步导入：分享链接 / base64 订阅正文 / Clash YAML / sing-box JSON（**不抓订阅 URL**）。"""
        window = self.papd_window
        text = str(pms_payload.get("text") or "").strip()
        raw_links = pms_payload.get("links")
        if not text and isinstance(raw_links, (list, tuple)):
            text = "\n".join(str(item) for item in raw_links if str(item).strip())
        if not text:
            return pms_api_cmd_fail("bad_request", "缺少 text（正文）或 links（链接数组）")
        group_name = str(pms_payload.get("group") or "").strip() or "导入"
        try:
            nodes, report = PmsTextImportClassifier(PmsShareLinkParser()).ptic_import_text(text, group_name)
        except Exception as exc:  # noqa: BLE001
            return pms_api_cmd_fail("parse_failed", f"解析导入内容失败：{exc}")
        skip_invalid = bool(window.pmw_settings.psm_get_bool("advanced.import_skip_invalid", True))
        dedupe = bool(window.pmw_settings.psm_get_bool("advanced.dedup_by_server_port", True))
        seen: set[str] = set()
        kept: list["PmsProxyNode"] = []
        invalid = 0
        duplicates = 0
        for node in list(nodes or []):
            node.enabled = True
            node.group_name = group_name
            if skip_invalid and node.pnode_validate():
                invalid += 1
                continue
            if dedupe:
                key = node.pnode_endpoint_signature()
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
            kept.append(node)
        if not kept:
            return pms_api_cmd_fail("no_valid_node", f"没有解析到可用节点（无效 {invalid}、重复 {duplicates}）")
        # 复用界面的 pmw_merge_imported_nodes：命中已有签名走「更新」而不是插重复行
        added, updated = window.pmw_merge_imported_nodes(kept)
        merged = list(added) + list(updated)
        if merged:
            window.pmw_repo.pnr_upsert_nodes(merged)
        window.pmw_reload_nodes(merged[0].uid if merged else "")
        return pms_api_cmd_ok({
            "added": len(added),
            "updated": len(updated),
            "invalid": invalid,
            "duplicates": duplicates,
            "group": group_name,
            "nodes": [self.papd_public_node(item) for item in merged],
        })

    # ------------------------------------------------------------------ 导出（v1.6.3，只读）
    def papd_export_nodes(self, pms_payload: dict) -> list["PmsProxyNode"]:
        """按 group / uids 挑出要导出的节点（导出走 GET，但仍要过界面线程）。"""
        window = self.papd_window
        raw_uids = pms_payload.get("uids")
        if isinstance(raw_uids, (list, tuple)) and raw_uids:
            nodes: list["PmsProxyNode"] = []
            for item in raw_uids:
                node = self.papd_find_node(str(item))
                if node is not None:
                    nodes.append(node)
            return nodes
        group = str(pms_payload.get("group") or "").strip()
        try:
            all_nodes = list(window.pmw_repo.pnr_load_all_nodes())
        except Exception:  # noqa: BLE001
            all_nodes = []
        if group:
            return [item for item in all_nodes if str(item.group_name) == group]
        return all_nodes

    def papd_export_links(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        nodes = self.papd_export_nodes(pms_payload)
        if not nodes:
            return pms_api_cmd_fail("no_node", "没有可导出的节点（检查 group 或 uids）")
        writer = self.papd_window.pmw_link_writer
        base64_text = str(pms_payload.get("base64") or "").strip().lower() in ("1", "true", "yes")
        content = writer.pslw_nodes_to_base64_text(nodes) if base64_text else writer.pslw_nodes_to_text(nodes)
        return pms_api_cmd_ok({"format": "base64" if base64_text else "links", "count": len(nodes), "content": content})

    def papd_export_backup(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        try:
            payload = self.papd_window.pmw_repo.pnr_export_payload()
        except Exception as exc:  # noqa: BLE001
            return pms_api_cmd_fail("db_error", f"读取备份数据失败：{exc}")
        return pms_api_cmd_ok({"format": "backup_json", "node_count": len(payload.get("nodes") or []), "data": payload})

    def papd_export_clash(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        nodes = self.papd_export_nodes(pms_payload)
        if not nodes:
            return pms_api_cmd_fail("no_node", "没有可导出的节点（检查 group 或 uids）")
        content = self.papd_window.pmw_build_clash_yaml(nodes)
        return pms_api_cmd_ok({"format": "clash_yaml", "count": len(nodes), "content": content})

    def papd_export_singbox(self, pms_payload: dict, pms_request_id: str = "") -> dict[str, Any]:
        nodes = self.papd_export_nodes(pms_payload)
        if not nodes:
            return pms_api_cmd_fail("no_node", "没有可导出的节点（检查 group 或 uids）")
        content = self.papd_window.pmw_build_singbox_json(nodes)
        return pms_api_cmd_ok({"format": "singbox_json", "count": len(nodes), "content": content})
