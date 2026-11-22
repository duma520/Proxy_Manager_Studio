#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clash / sing-box External Controller 兼容层（v1.6.4）

为什么单独一个模块：
    Clash 面板（yacd / Clash Dashboard 等）只会按 **Clash 自己的一套路径** 访问控制器：
    `/proxies`、`/proxies/:name/delay`、`/version`、`/traffic`……它们不带 `/api/v1` 前缀，
    返回体也不是 `{"ok": true, "data": ...}`。把这些「翻译」塞进 `apiserver.py` 会让路由层
    两套语义混杂，所以单独成模块：**本模块只做语义翻译**，
    数据一律取自 `pms/api.py` 的门面（`PmsApi`），动作仍走 v1.6.2 的派发器过界面线程。

兼容范围（刻意保守）：
    只读：`GET /version` `/configs` `/proxies` `/proxies/:name` `/rules` `/traffic`
          `/memory` `/connections` `/providers/proxies`
    写：`PUT /proxies/:name`（切换节点）、`GET /proxies/:name/delay`（测延迟，
          内部**复用 v1.6.3 的 `wait=true` 同步等待**，不阻塞界面）、
          `GET /group/:name/delay`（整组延迟）、`DELETE /connections`（空操作）

刻意不实现：
    `PATCH /configs`（改配置）、规则增删改、按 id 断连接——本程序的配置与连接由核心进程
    和界面负责，接口层不打算复制一份核心控制台；遇到这些请求返回 400 并说明原因，
    **不静默假装成功**。

安全：
    · 与自有接口**共用同一个端口与同一套 token 鉴权**（`Authorization: Bearer <token>`）；
    · 写操作同样受 `api.write_enabled` 约束（不勾选 → 403）；
    · 节点口令字段**一律不输出**（门面已经剔过，面板也不需要）。
"""

from __future__ import annotations

from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
)
from urllib.parse import unquote
from PySide6.QtCore import QObject
import logging

from pms.constants import (
    PMS_APP_NAME,
    PMS_APP_VERSION,
    PMS_API_WAIT_MAX_TIMEOUT_MS,
    PMS_API_WAIT_MIN_TIMEOUT_MS,
    PMS_LOGGER_NAME,
    PMS_PROTOCOL_ANYTLS,
    PMS_PROTOCOL_HTTP,
    PMS_PROTOCOL_HYSTERIA1,
    PMS_PROTOCOL_HYSTERIA2,
    PMS_PROTOCOL_JUICITY,
    PMS_PROTOCOL_NAIVE,
    PMS_PROTOCOL_SHADOWSOCKS,
    PMS_PROTOCOL_SHADOWTLS,
    PMS_PROTOCOL_SNELL,
    PMS_PROTOCOL_SOCKS,
    PMS_PROTOCOL_SOCKS4,
    PMS_PROTOCOL_TG_WEBPROXY,
    PMS_PROTOCOL_SSR,
    PMS_PROTOCOL_TROJAN,
    PMS_PROTOCOL_TUIC,
    PMS_PROTOCOL_VLESS,
    PMS_PROTOCOL_VMESS,
    PMS_PROTOCOL_WIREGUARD,
)

if TYPE_CHECKING:  # 只标注类型，避免与本模块的使用方循环依赖
    from pms.api import PmsApi
    from pms.settings import PmsSettingsManager

# 对外暴露的「选择器」组名：面板靠它做切换下拉
PMS_CLASH_GROUP_NAME = "GLOBAL"

PMS_CLASH_DIRECT_NAME = "DIRECT"

PMS_CLASH_REJECT_NAME = "REJECT"

# 我们的协议 → Clash 面板认识的 type（不在表里的回落 Unknown，面板只会显示成未知类型）
PMS_CLASH_TYPE_BY_PROTOCOL: dict[str, str] = {
    PMS_PROTOCOL_VMESS: "Vmess",
    PMS_PROTOCOL_VLESS: "Vless",
    PMS_PROTOCOL_TROJAN: "Trojan",
    PMS_PROTOCOL_SHADOWSOCKS: "Shadowsocks",
    PMS_PROTOCOL_SSR: "ShadowsocksR",
    PMS_PROTOCOL_HYSTERIA2: "Hysteria2",
    PMS_PROTOCOL_HYSTERIA1: "Hysteria",
    PMS_PROTOCOL_TUIC: "Tuic",
    PMS_PROTOCOL_SOCKS: "Socks5",
    PMS_PROTOCOL_SOCKS4: "Socks4",
    PMS_PROTOCOL_HTTP: "Http",
    PMS_PROTOCOL_SNELL: "Snell",
    PMS_PROTOCOL_WIREGUARD: "WireGuard",
    PMS_PROTOCOL_JUICITY: "Juicity",
    PMS_PROTOCOL_ANYTLS: "AnyTLS",
    PMS_PROTOCOL_SHADOWTLS: "ShadowTLS",
    PMS_PROTOCOL_NAIVE: "Naive",
    # v1.9.68：Telegram 专属代理 — tg_webproxy 当 SOCKS5，tg_proxy 不路由（MTProto 面板不认）
    PMS_PROTOCOL_TG_WEBPROXY: "Socks5",
}

PMS_CLASH_TYPE_UNKNOWN = "Unknown"

# `/proxies/:name/delay` 的超时（毫秒）：Clash 默认 5000，夹在等待机制的下限 1 秒与上限 120 秒之间
PMS_CLASH_DELAY_TIMEOUT_MS = 5000
PMS_CLASH_DELAY_MIN_TIMEOUT_MS = int(PMS_API_WAIT_MIN_TIMEOUT_MS)
PMS_CLASH_DELAY_MAX_TIMEOUT_MS = int(PMS_API_WAIT_MAX_TIMEOUT_MS)

# 认领的兼容路径前缀（**不带 /api/v1**）；命中才交给本层，其余仍按自有接口的 404 处理
PMS_CLASH_PATH_PREFIXES: tuple[str, ...] = (
    "/proxies",
    "/group",
    "/configs",
    "/version",
    "/traffic",
    "/memory",
    "/connections",
    "/rules",
    "/providers",
)


class PmsClashCompat(QObject):
    """Clash / sing-box External Controller 语义 ↔ 本程序门面的翻译层。

    不持有主窗口、不碰数据库、不弹任何对话框：数据从 `PmsApi` 来，动作经派发器走界面线程。
    """

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_api: "PmsApi",
        pms_parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pcl_settings = pms_settings
        self.pcl_api = pms_api
        self.pcl_logger = logging.getLogger(PMS_LOGGER_NAME + ".apiclash")

    # ------------------------------------------------------------------ 认领与入口
    @staticmethod
    def pcl_is_clash_path(pms_path: str) -> bool:
        """判断一个路径是否属于兼容层（根路径也算，用于面板探测）。"""
        try:
            path = unquote(str(pms_path or "/")).strip()
        except Exception:  # noqa: BLE001 - 畸形 URL 不能让请求线程崩掉
            path = "/"
        if path in ("", "/"):
            return True
        return any(path == prefix or path.startswith(prefix + "/") for prefix in PMS_CLASH_PATH_PREFIXES)

    def pcl_handle(
        self,
        pms_path: str,
        pms_query: Optional[dict] = None,
        pms_method: str = "GET",
        pms_body: Optional[dict] = None,
        pms_body_error: str = "",
    ) -> tuple[int, dict]:
        """处理一个兼容层请求；返回 `(HTTP 状态码, 响应体)`（**响应体是 Clash 风格**）。"""
        try:
            path = unquote(str(pms_path or "/")).strip()
        except Exception:  # noqa: BLE001
            path = "/"
        query = dict(pms_query or {})
        method = str(pms_method or "GET").upper()
        body = dict(pms_body or {})
        try:
            return self.pcl_route(path, query, method, body, str(pms_body_error or ""))
        except Exception as exc:  # noqa: BLE001 - 单个请求出错不能连累整个服务
            self.pcl_logger.exception("处理兼容层请求出错（%s %s）：%s", method, path, exc)
            return self.pcl_error(500, f"处理请求失败：{exc}", "server_error")

    # ------------------------------------------------------------------ 路由
    def pcl_route(self, pms_path: str, pms_query: dict, pms_method: str, pms_body: dict, pms_body_error: str) -> tuple[int, dict]:
        if pms_method == "OPTIONS":
            return 204, {}
        if pms_method == "HEAD":
            return 200, {}
        if pms_path in ("", "/"):
            return 200, {"app": PMS_APP_NAME, "version": PMS_APP_VERSION, "clash_compat": True}
        if pms_method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return self.pcl_error(405, f"不支持的请求方法：{pms_method}", "method_not_allowed")
        if str(pms_body_error or ""):
            return self.pcl_error(400, str(pms_body_error), "bad_request")

        if pms_path == "/version":
            return self.pcl_version()
        if pms_path == "/configs":
            return self.pcl_configs(pms_method)
        if pms_path == "/traffic":
            return self.pcl_traffic()
        if pms_path == "/memory":
            return 200, {"inuse": 0, "oslimit": 0}
        if pms_path == "/connections":
            return self.pcl_connections(pms_method)
        if pms_path == "/rules":
            return 200, {"rules": []}
        if pms_path == "/providers/proxies":
            return 200, {"providers": {}}
        if pms_path == "/proxies":
            if pms_method == "GET":
                return 200, {"proxies": self.pcl_proxies()}
            return self.pcl_error(405, "/proxies 只支持 GET（切换请用 PUT /proxies/GLOBAL）", "method_not_allowed")

        # /proxies/<name> 与 /proxies/<name>/delay
        if pms_path.startswith("/proxies/"):
            tail = pms_path[len("/proxies/"):].strip("/")
            if tail.endswith("/delay"):
                name = tail[: -len("/delay")]
                if pms_method != "GET":
                    return self.pcl_error(405, "延迟测试只支持 GET", "method_not_allowed")
                return self.pcl_delay(name, pms_query)
            if pms_method == "GET":
                entry = self.pcl_find(tail)
                if entry is None:
                    return self.pcl_error(404, f"没有这个代理：{tail}", "not_found")
                return 200, self.pcl_proxy_object(entry[0], entry[1])
            if pms_method == "PUT":
                return self.pcl_switch(tail, pms_body)
            return self.pcl_error(405, f"/proxies/{tail} 只支持 GET 与 PUT", "method_not_allowed")

        # /group/<name>/delay：整组延迟
        if pms_path.startswith("/group/") and pms_path.endswith("/delay"):
            name = pms_path[len("/group/"): -len("/delay")].strip("/")
            if pms_method != "GET":
                return self.pcl_error(405, "整组延迟测试只支持 GET", "method_not_allowed")
            return self.pcl_group_delay(name, pms_query)

        return self.pcl_error(404, f"没有这个接口：{pms_path}", "not_found")

    # ------------------------------------------------------------------ 各端点
    def pcl_version(self) -> tuple[int, dict]:
        """面板探测版本；`meta=true` 让面板按 Mihomo 的方式显示。"""
        return 200, {"version": f"v{PMS_APP_VERSION}", "premium": False, "meta": True}

    def pcl_configs(self, pms_method: str) -> tuple[int, dict]:
        """当前生效的入口与开关快照（**只读**；改配置走界面，不在这里做）。"""
        if pms_method != "GET":
            return self.pcl_error(
                400,
                "不支持通过接口修改配置：请到程序「设置 → 本地入口 / 路由与分流」里改",
                "unsupported",
            )
        settings = self.pcl_settings
        state = self.pcl_api.papi_state()
        core = state.get("core") if isinstance(state.get("core"), dict) else {}
        return 200, {
            "port": int(settings.psm_get_int("inbound.http_port", 0)),
            "socks-port": int(settings.psm_get_int("inbound.socks_port", 0)),
            "mixed-port": int(settings.psm_get_int("inbound.mixed_port", 0)),
            "allow-lan": bool(settings.psm_get_bool("inbound.allow_lan", False)),
            "mode": "rule",
            "log-level": "info",
            "ipv6": False,
            "tun": {"enable": bool(state.get("tun", False))},
            "core_running": bool(core.get("running", False)),
            "core_kind": str(core.get("kind") or ""),
        }

    def pcl_traffic(self) -> tuple[int, dict]:
        """实时速率（字节/秒）；核心没起时为 0。"""
        result = self.pcl_api.papi_traffic()
        speed = result.pmsar_data.get("speed_bps") if isinstance(result.pmsar_data, dict) else {}
        speed = dict(speed or {})
        up = float(speed.get("proxy_up") or 0.0) + float(speed.get("direct_up") or 0.0)
        down = float(speed.get("proxy_down") or 0.0) + float(speed.get("direct_down") or 0.0)
        return 200, {"up": int(up), "down": int(down)}

    def pcl_connections(self, pms_method: str) -> tuple[int, dict]:
        """连接列表：**本程序不维护连接表**，只回累计流量；DELETE 是空操作（面板会调它）。"""
        result = self.pcl_api.papi_traffic()
        totals = result.pmsar_data.get("totals") if isinstance(result.pmsar_data, dict) else {}
        totals = dict(totals or {})
        if pms_method == "DELETE":
            self.pcl_logger.debug("兼容层收到 DELETE /connections：本程序不维护连接表，空操作")
            return 204, {}
        if pms_method != "GET":
            return self.pcl_error(405, "/connections 只支持 GET 与 DELETE", "method_not_allowed")
        return 200, {
            "connections": [],
            "downloadTotal": int(totals.get("proxy_down") or 0) + int(totals.get("direct_down") or 0),
            "uploadTotal": int(totals.get("proxy_up") or 0) + int(totals.get("direct_up") or 0),
        }

    def pcl_delay(self, pms_name: str, pms_query: dict) -> tuple[int, dict]:
        """测单个节点的延迟：**复用 v1.6.3 的 `wait=true`**，同步等到结果但不阻塞界面。

        Clash 语义：成功 `{"delay": 毫秒}`，测不出来 `{"delay": 0}`（`url` 参数不支持，
        测试网址沿用程序设置 `test.url_list`）。
        """
        entry = self.pcl_find(pms_name)
        if entry is None:
            return self.pcl_error(404, f"没有这个代理：{pms_name}", "not_found")
        timeout_ms = self.pcl_delay_timeout(pms_query.get("timeout"))
        result = self.pcl_api.papi_test_latency(
            pms_uids=[str(entry[1].get("uid") or "")],
            pms_wait=True,
            pms_timeout_ms=timeout_ms,
        )
        if not result.pmsar_ok:
            status = self.pcl_status_by_code(result.pmsar_code)
            return self.pcl_error(status, str(result.pmsar_message or "延迟测试失败"), str(result.pmsar_code))
        rows = list((result.pmsar_data or {}).get("results") or [])
        delay = 0
        if rows:
            raw = rows[0].get("latency_ms")
            success = bool(rows[0].get("success"))
            delay = int(raw) if isinstance(raw, (int, float)) and int(raw) >= 0 and success else 0
        return 200, {"delay": delay}

    def pcl_group_delay(self, pms_name: str, pms_query: dict) -> tuple[int, dict]:
        """整组延迟：只对 `GLOBAL` 有效，一次测完并按显示名返回 `{名: 毫秒}`。"""
        if pms_name != PMS_CLASH_GROUP_NAME:
            return self.pcl_error(404, f"没有这个代理组：{pms_name}（本程序只暴露 {PMS_CLASH_GROUP_NAME}）", "not_found")
        timeout_ms = self.pcl_delay_timeout(pms_query.get("timeout"))
        result = self.pcl_api.papi_test_latency(pms_scope="all", pms_wait=True, pms_timeout_ms=timeout_ms)
        if not result.pmsar_ok:
            status = self.pcl_status_by_code(result.pmsar_code)
            return self.pcl_error(status, str(result.pmsar_message or "延迟测试失败"), str(result.pmsar_code))
        entries = self.pcl_entries()
        name_by_uid = {str(row.get("uid") or ""): display for display, row in entries}
        delays: dict[str, int] = {}
        for row in list((result.pmsar_data or {}).get("results") or []):
            uid = str(row.get("uid") or "")
            display = name_by_uid.get(uid)
            if not display:
                continue
            raw = row.get("latency_ms")
            success = bool(row.get("success"))
            delays[display] = int(raw) if isinstance(raw, (int, float)) and int(raw) >= 0 and success else 0
        return 200, {"delays": delays}

    def pcl_switch(self, pms_name: str, pms_body: dict) -> tuple[int, dict]:
        """切换节点：`PUT /proxies/GLOBAL {"name": "节点名"}`（**受 `api.write_enabled` 约束**）。"""
        target_name = str(pms_body.get("name") or "").strip()
        if not target_name:
            return self.pcl_error(400, "缺少 name（要切到哪个节点）", "bad_request")
        entry = self.pcl_find(target_name)
        if entry is None:
            # 面板会把「组名」也传进来，这里宽容处理：路径名就是目标时也认
            entry = self.pcl_find(pms_name)
        if entry is None:
            return self.pcl_error(404, f"没有这个代理：{target_name}", "not_found")
        result = self.pcl_api.papi_switch_node(str(entry[1].get("uid") or ""))
        if not result.pmsar_ok:
            status = self.pcl_status_by_code(result.pmsar_code)
            return self.pcl_error(status, str(result.pmsar_message or "切换失败"), str(result.pmsar_code))
        return 204, {}      # Clash 的切换成功是 204 No Content

    # ------------------------------------------------------------------ 节点 ↔ 代理对象
    def pcl_entries(self) -> list[tuple[str, dict]]:
        """`(显示名, 节点字典)` 列表；显示名一定非空且不重复（面板靠名字寻址）。"""
        result = self.pcl_api.papi_nodes()
        rows = list(result.pmsar_data or []) if result.pmsar_ok else []
        entries: list[tuple[str, dict]] = []
        used: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            display = str(row.get("name") or "").strip() or f"{row.get('server') or ''}:{row.get('port') or 0}"
            if display in used:
                used[display] += 1
                display = f"{display}#{used[display]}"
            else:
                used[display] = 1
            entries.append((display, row))
        return entries

    def pcl_find(self, pms_name: str) -> Optional[tuple[str, dict]]:
        """按显示名找节点（先精确、后忽略大小写），再按 uid 找；找不到返回 None。"""
        name = str(pms_name or "").strip()
        if not name:
            return None
        entries = self.pcl_entries()
        for display, row in entries:
            if display == name:
                return display, row
        lowered = name.lower()
        for display, row in entries:
            if display.lower() == lowered:
                return display, row
        for display, row in entries:
            if str(row.get("uid") or "") == name:
                return display, row
        return None

    def pcl_proxies(self) -> dict[str, dict]:
        """Clash 的 `/proxies` 返回体：内含一个 `GLOBAL` 选择器与全部节点。"""
        entries = self.pcl_entries()
        current_uid = str(self.pcl_api.papi_state().get("current_node_uid") or "")
        proxies: dict[str, dict] = {
            PMS_CLASH_DIRECT_NAME: {"name": PMS_CLASH_DIRECT_NAME, "type": "Direct", "udp": True, "history": []},
            PMS_CLASH_REJECT_NAME: {"name": PMS_CLASH_REJECT_NAME, "type": "Reject", "udp": True, "history": []},
        }
        names: list[str] = []
        now_name = ""
        for display, row in entries:
            proxies[display] = self.pcl_proxy_object(display, row)
            names.append(display)
            if current_uid and str(row.get("uid") or "") == current_uid:
                now_name = display
        proxies[PMS_CLASH_GROUP_NAME] = {
            "name": PMS_CLASH_GROUP_NAME,
            "type": "Selector",
            "udp": True,
            "now": now_name or (names[0] if names else ""),
            "all": list(names),       # 只列真实节点：DIRECT / REJECT 不能当切换目标
            "history": [],
        }
        return proxies

    def pcl_proxy_object(self, pms_display: str, pms_row: dict) -> dict:
        """单个节点 → Clash 代理对象（**不含任何口令字段**）。"""
        protocol = str(pms_row.get("protocol") or "").strip().lower()
        history: list[dict[str, Any]] = []
        raw_latency = pms_row.get("latency_ms")
        if isinstance(raw_latency, (int, float)):
            history.append({"time": self.pcl_time_text(pms_row.get("last_test_time")), "delay": int(raw_latency)})
        return {
            "name": str(pms_display),
            "type": PMS_CLASH_TYPE_BY_PROTOCOL.get(protocol, PMS_CLASH_TYPE_UNKNOWN),
            "udp": True,
            "history": history,
            "uid": str(pms_row.get("uid") or ""),        # 额外字段：方便脚本拿 uid 再调自有接口
            "server": str(pms_row.get("server") or ""),
            "port": int(pms_row.get("port") or 0),
        }

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def pcl_time_text(pms_value: Any) -> str:
        """把 `last_test_time`（可能是时间戳也可能是字符串）转成面板能吃的时间文本。"""
        if isinstance(pms_value, (int, float)) and float(pms_value) > 0:
            try:
                return datetime.fromtimestamp(float(pms_value)).isoformat(timespec="seconds")
            except (ValueError, OSError, OverflowError):
                return ""
        return str(pms_value or "")

    @staticmethod
    def pcl_delay_timeout(pms_value: Any) -> int:
        """`timeout` 查询参数（毫秒）→ 夹取后的等待上限。"""
        try:
            timeout_ms = int(str(pms_value or "").strip() or 0)
        except (TypeError, ValueError):
            timeout_ms = 0
        if timeout_ms <= 0:
            timeout_ms = int(PMS_CLASH_DELAY_TIMEOUT_MS)
        return max(PMS_CLASH_DELAY_MIN_TIMEOUT_MS, min(PMS_CLASH_DELAY_MAX_TIMEOUT_MS, timeout_ms))

    @staticmethod
    def pcl_status_by_code(pms_code: str) -> int:
        """动作结果码 → HTTP 状态码（与自有接口同一套映射，另加「正在测速」的 400）。"""
        code = str(pms_code or "")
        if code in ("write_disabled", "unauthorized"):
            return 403
        if code in ("node_not_found", "not_found"):
            return 404
        if code == "timeout":
            return 504
        return 400

    @staticmethod
    def pcl_error(pms_status: int, pms_message: str, pms_code: str = "") -> tuple[int, dict]:
        """Clash 风格的错误体：面板主要看状态码，这里同时给 `message` 与 `error`。"""
        payload: dict[str, Any] = {"message": str(pms_message)}
        if str(pms_code or ""):
            payload["error"] = str(pms_code)
        return int(pms_status), payload
