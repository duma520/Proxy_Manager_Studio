#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计接口客户端

Clash API（连接列表 / 累计流量 / 真连接延迟）与 Xray Stats API（statsquery 增量字节）。
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Optional,
)
import json
import logging
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

from pms.constants import (
    PMS_IS_WINDOWS,
    PMS_LOGGER_NAME,
)
from pms.utils import pms_safe_int

def _pms_clash_http_error_text(pms_exc: urllib.error.HTTPError) -> str:
    """把 Clash API 的 HTTP 错误连同**核心自己给的说明**拼成一句话（v1.9.19）。

    核心（sing-box / mihomo）的失败原因就写在响应体里：
    503 是 `{"message":"An error occurred in the delay test"}`（节点侧报错），
    504 是 `{"message":"Timeout"}`（超时）。原来只留 `HTTP 503` / `HTTP 504`，
    界面上「节点自己坏了」和「这个地址太慢超时」长得一模一样，
    用户只能看到「超时或无响应」，根本没法判断是不是程序的问题。
    """
    text = f"HTTP {pms_exc.code}"
    try:
        payload = pms_exc.read()
    except Exception:  # noqa: BLE001 - 读不出响应体不影响错误信息本身
        payload = b""
    message = ""
    if payload:
        try:
            detail = json.loads(payload.decode("utf-8", errors="replace"))
        except ValueError:
            detail = {}
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail.get("error") or detail.get("detail") or "")
    return f"{text}（{message}）" if message else text

# --------------------------------------------------------------------------------------
# Clash API 客户端（sing-box 的连接列表 / 流量 / 真连接延迟测试）
# --------------------------------------------------------------------------------------
class PmsClashApiClient:
    """访问 sing-box 的 Clash API（experimental.clash_api）。"""

    def __init__(self, pms_port: int, pms_token: str = "", pms_logger: Optional[logging.Logger] = None) -> None:
        self.pca_port = int(pms_port or 0)
        self.pca_token = str(pms_token or "")
        self.pca_logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".clashapi")

    def pca_base_url(self) -> str:
        return f"http://127.0.0.1:{self.pca_port}"

    def pca_request(self, pms_path: str, pms_method: str = "GET", pms_timeout: float = 5.0) -> tuple[bool, Any]:
        """发起一次请求，返回 (是否成功, 数据或错误文本)。"""
        if not self.pca_port:
            return False, "未配置 Clash API 端口"
        request = urllib.request.Request(self.pca_base_url() + pms_path, method=pms_method)
        if self.pca_token:
            request.add_header("Authorization", f"Bearer {self.pca_token}")
        try:
            with urllib.request.urlopen(request, timeout=pms_timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            return False, _pms_clash_http_error_text(exc)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return False, str(exc)
        if not payload:
            return True, {}
        try:
            return True, json.loads(payload.decode("utf-8", errors="replace"))
        except ValueError:
            return True, {}

    def pca_is_available(self, pms_timeout: float = 2.0) -> bool:
        ok, _ = self.pca_request("/version", pms_timeout=pms_timeout)
        return ok

    def pca_get_connections(self, pms_timeout: float = 4.0) -> Optional[dict[str, Any]]:
        """获取连接快照（含累计流量）。"""
        ok, data = self.pca_request("/connections", pms_timeout=pms_timeout)
        if not ok or not isinstance(data, dict):
            if not ok:
                self.pca_logger.debug("Clash /connections 失败：%s", data)
            return None
        return data

    def pca_get_version(self) -> str:
        ok, data = self.pca_request("/version", pms_timeout=3.0)
        if ok and isinstance(data, dict):
            return str(data.get("version") or "")
        return ""

    def pca_get_proxies(self) -> dict[str, Any]:
        ok, data = self.pca_request("/proxies", pms_timeout=5.0)
        return data if ok and isinstance(data, dict) else {}

    def pca_delay_test_detail(self, pms_tag: str, pms_url: str, pms_timeout_ms: int) -> tuple[int, str]:
        """真连接延迟测试（**带失败原因**）：返回 (毫秒, 说明)。

        v1.9.19：原 `pca_delay_test()` 把所有失败压成一个 -1，
        调用方只能自己编一句「超时或无响应」——核心明明回了
        `503 An error occurred in the delay test`（节点自己报错）也看不出来。
        这里把核心的原话交给上层，界面 / 历史 / 汇总才有话可说。
        """
        query = urllib.parse.urlencode({"timeout": int(pms_timeout_ms), "url": str(pms_url)})
        ok, data = self.pca_request(
            f"/proxies/{urllib.parse.quote(str(pms_tag), safe='')}/delay?{query}",
            pms_timeout=(int(pms_timeout_ms) / 1000.0) + 3.0,
        )
        if not ok:
            self.pca_logger.debug("延迟测试 %s 失败：%s", pms_tag, data)
            return -1, f"核心接口错误（{data}）"
        if isinstance(data, dict) and data.get("delay") is not None:
            try:
                return int(data["delay"]), ""
            except (TypeError, ValueError):
                return -1, "核心返回的延迟值无法解析"
        return -1, "核心没返回延迟值"

    def pca_delay_test(self, pms_tag: str, pms_url: str, pms_timeout_ms: int) -> int:
        """真连接延迟测试：GET /proxies/{tag}/delay。返回毫秒，失败 -1（**契约不变**）。"""
        return self.pca_delay_test_detail(pms_tag, pms_url, pms_timeout_ms)[0]

    def pca_close_connection(self, pms_connection_id: str) -> bool:
        ok, _ = self.pca_request(f"/connections/{urllib.parse.quote(str(pms_connection_id), safe='')}", pms_method="DELETE", pms_timeout=4.0)
        return ok

    def pca_close_all_connections(self) -> bool:
        ok, _ = self.pca_request("/connections", pms_method="DELETE", pms_timeout=6.0)
        return ok

    def pca_set_mode(self, pms_mode: str) -> bool:
        payload = json.dumps({"mode": str(pms_mode)}).encode("utf-8")
        request = urllib.request.Request(self.pca_base_url() + "/configs", data=payload, method="PATCH")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5.0):
                return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.pca_logger.debug("设置 Clash 模式失败：%s", exc)
            return False

class PmsXrayStatsClient:
    """通过 `xray api statsquery` 读取 Xray 的真实流量统计。"""

    def __init__(self, pms_core_path: Path, pms_api_port: int, pms_logger: Optional[logging.Logger] = None) -> None:
        self.pxs_core_path = Path(pms_core_path)
        self.pxs_api_port = int(pms_api_port or 0)
        self.pxs_logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".xraystats")
        self.pxs_lock = threading.RLock()

    def pxs_is_available(self) -> bool:
        return bool(self.pxs_api_port) and self.pxs_core_path.is_file()

    def pxs_query(self, pms_pattern: str = "", pms_reset: bool = True, pms_timeout: float = 8.0) -> dict[str, int]:
        """执行 statsquery，返回 {统计名: 数值}（reset=True 时返回区间增量）。"""
        if not self.pxs_is_available():
            return {}
        command = [
            str(self.pxs_core_path), "api", "statsquery",
            f"--server=127.0.0.1:{self.pxs_api_port}",
            "-pattern", str(pms_pattern),
        ]
        if pms_reset:
            command.append("-reset")
        with self.pxs_lock:
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=pms_timeout,
                    cwd=str(self.pxs_core_path.parent),
                    creationflags=subprocess.CREATE_NO_WINDOW if PMS_IS_WINDOWS else 0,  # type: ignore[attr-defined]
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.pxs_logger.debug("statsquery 执行失败：%s", exc)
                return {}
        output = (completed.stdout or "").strip()
        if completed.returncode != 0 or not output:
            if completed.returncode != 0:
                self.pxs_logger.debug("statsquery 返回码 %s：%s", completed.returncode, (completed.stderr or "").strip()[:200])
            return {}
        try:
            payload = json.loads(output)
        except ValueError:
            self.pxs_logger.debug("statsquery 输出无法解析：%s", output[:200])
            return {}
        result: dict[str, int] = {}
        for item in (payload.get("stat") or []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            result[name] = pms_safe_int(item.get("value"), 0)
        return result

    def pxs_proxy_traffic(self) -> tuple[int, int]:
        """代理出站的上/下行字节（reset 语义：自上次调用以来的增量）。"""
        stats = self.pxs_query("outbound>>>proxy>>>traffic", pms_reset=True)
        up = 0
        down = 0
        for name, value in stats.items():
            if name.endswith(">>>uplink"):
                up += value
            elif name.endswith(">>>downlink"):
                down += value
        return up, down

    def pxs_inbound_total(self) -> tuple[int, int]:
        """所有入口合计的上/下行字节（含直连与代理）。"""
        stats = self.pxs_query("inbound>>>", pms_reset=True)
        up = 0
        down = 0
        for name, value in stats.items():
            if ">>>api-in>>>" in name:
                continue
            if name.endswith(">>>uplink"):
                up += value
            elif name.endswith(">>>downlink"):
                down += value
        return up, down
