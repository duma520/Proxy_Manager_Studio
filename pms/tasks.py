#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台任务

延迟测试、测速、TCP 握手测试的 QRunnable 任务与它们的信号载体（多线程执行单元）。
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Optional,
    Sequence,
)
from PySide6.QtCore import (
    QObject,
    QRunnable,
    Signal,
    Slot,
)
import socket
import time
import urllib.error
import urllib.request

from pms.constants import (
    PMS_DEFAULT_SPEED_TEST_URL,
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_DEFAULT_TEST_URLS,
)
from pms.utils import pms_safe_int
from pms.paths import PmsRuntimePaths
from pms.models import PmsProxyNode
from pms.statsapi import PmsClashApiClient
from pms.tempsession import PmsTempCoreSession

class PmsTaskSignals(QObject):
    """工作线程 → 主线程的信号载体（延迟测试 / 测速共用）。"""

    ptask_progress = Signal(str, int, int)          # 节点 uid, 已完成/总量（或已下载字节）
    ptask_node_done = Signal(str, int, float, bool, str)  # uid, 延迟ms, 速率, 是否成功, 说明
    ptask_message = Signal(str)
    ptask_finished = Signal(dict)

def _ptask_url_list(pms_url: Any) -> list[str]:
    """把「单个地址」或「地址列表」统一成非空列表（全空则回落内置默认地址）。

    v1.9.19：延迟任务开始接受**多个测试地址**——调度器把设置里的整张列表发下来，
    任务在重试时按顺序换下一个（见 `run()`）。
    """
    if isinstance(pms_url, str):
        items = [pms_url]
    else:
        items = [str(item) for item in (pms_url or [])]
    cleaned = [item.strip() for item in items if str(item).strip()]
    return cleaned or list(PMS_DEFAULT_TEST_URLS)

class PmsClashDelayTask(QRunnable):
    """通过 Clash API 对某个出站做真连接延迟测试（单次）。"""

    def __init__(
        self,
        pms_signals: PmsTaskSignals,
        pms_client: PmsClashApiClient,
        pms_node: PmsProxyNode,
        pms_tag: str,
        pms_url: Any,
        pms_timeout_ms: int,
        pms_attempts: int,
    ) -> None:
        super().__init__()
        self.plt_signals = pms_signals
        self.plt_client = pms_client
        self.plt_node = pms_node
        self.plt_tag = pms_tag
        self.plt_urls = _ptask_url_list(pms_url)
        self.plt_timeout_ms = int(pms_timeout_ms)
        self.plt_attempts = max(1, int(pms_attempts))
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        best = -1
        error = ""
        index = 0
        for _ in range(self.plt_attempts):
            url = self.plt_urls[index % len(self.plt_urls)]
            latency, reason = self.plt_client.pca_delay_test_detail(self.plt_tag, url, self.plt_timeout_ms)
            if latency >= 0:
                best = latency if best < 0 else min(best, latency)
                error = ""
            else:
                # v1.9.19：①保留核心给的真实原因（原来一律写成「超时或无响应」，
                # 503「节点自己报错」被说成超时）；②这个地址没成，下次换下一个地址重试。
                error = reason or "超时或无响应"
                index += 1
        self.plt_signals.ptask_node_done.emit(self.plt_node.uid, best, 0.0, best >= 0, error)

class PmsTempCoreTask(QRunnable):
    """为单个节点拉起临时核心，测延迟（可带一次测速）。"""

    def __init__(
        self,
        pms_signals: PmsTaskSignals,
        pms_settings: "PmsSettingsManager",
        pms_paths: PmsRuntimePaths,
        pms_core_kind: str,
        pms_core_path: Path,
        pms_node: PmsProxyNode,
        pms_urls: Sequence[str],
        pms_timeout_ms: int,
        pms_attempts: int,
        pms_speed_config: Optional[dict[str, Any]] = None,
        pms_coordinator: Optional[Any] = None,
    ) -> None:
        super().__init__()
        self.plt_signals = pms_signals
        self.plt_settings = pms_settings
        self.plt_paths = pms_paths
        self.plt_core_kind = pms_core_kind
        self.plt_core_path = Path(pms_core_path)
        self.plt_node = pms_node
        self.plt_urls = _ptask_url_list(pms_urls)
        self.plt_timeout_ms = int(pms_timeout_ms)
        self.plt_attempts = max(1, int(pms_attempts))
        self.plt_speed_config = dict(pms_speed_config or {})
        # v1.9.9：调度器引用，用来把本次的测量会话登记进去——
        # 停止时要靠它逐个打断（否则线程卡在 IO 上，进程退不出去）。可为空。
        self.plt_coordinator = pms_coordinator
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        session = PmsTempCoreSession(self.plt_settings, self.plt_paths, self.plt_core_kind, self.plt_core_path, self.plt_node)
        coordinator = self.plt_coordinator
        if coordinator is not None:
            coordinator.plt_register_session(session)
        try:
            error = session.ptcs_open()
            if error:
                self.plt_signals.ptask_node_done.emit(self.plt_node.uid, -1, 0.0, False, error)
                return
            best = -1
            last_error = ""
            for url in self.plt_urls:
                latency, error_text = session.ptcs_measure_latency(url, self.plt_timeout_ms, self.plt_attempts)
                if latency >= 0:
                    best = latency if best < 0 else min(best, latency)
                    break
                last_error = error_text
            speed_bps = 0.0
            if best >= 0 and self.plt_speed_config:
                url = str(self.plt_speed_config.get("url") or "")
                if url:
                    total_bytes, elapsed, speed_error = session.ptcs_download_measure(
                        url,
                        pms_safe_int(self.plt_speed_config.get("duration_seconds"), 8),
                        pms_safe_int(self.plt_speed_config.get("limit_bytes"), 32 * 1024 * 1024),
                        pms_progress=lambda downloaded, seconds: self.plt_signals.ptask_progress.emit(
                            self.plt_node.uid, int(downloaded), 0
                        ),
                        pms_min_samples=pms_safe_int(self.plt_speed_config.get("min_samples"), 3),
                    )
                    if total_bytes > 0:
                        # v1.9.16：同测速任务，优先用稳态速度
                        speed_bps = float(getattr(session, "ptcs_last_speed", 0.0) or 0.0) or (total_bytes / max(0.001, elapsed))
                    else:
                        last_error = speed_error or last_error
            self.plt_signals.ptask_node_done.emit(self.plt_node.uid, best, speed_bps, best >= 0, last_error)
        finally:
            session.ptcs_close()
            if coordinator is not None:
                coordinator.plt_unregister_session(session)

class PmsXrayBatchDelayTask(QRunnable):
    """Xray 单核多出站批量延迟任务。

    与 Clash 快通道不同，Xray 没有 Clash API；本任务复刻 tempsession 的「经本地 HTTP 代理出口做真连接」思路，
    但所有节点共享同一个 Xray 核心（每个节点对应一个独立 HTTP 入站），避免 N 个节点起 N 个进程的额外开销。
    """

    def __init__(
        self,
        pms_signals: PmsTaskSignals,
        pms_node: PmsProxyNode,
        pms_port: int,
        pms_url: Any,
        pms_timeout_ms: int,
        pms_attempts: int,
    ) -> None:
        super().__init__()
        self.plt_signals = pms_signals
        self.plt_node = pms_node
        self.plt_port = int(pms_port)
        self.plt_urls = _ptask_url_list(pms_url)
        self.plt_timeout_ms = int(pms_timeout_ms)
        self.plt_attempts = max(1, int(pms_attempts))
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        handler = urllib.request.ProxyHandler({
            "http": f"http://127.0.0.1:{self.plt_port}",
            "https": f"http://127.0.0.1:{self.plt_port}",
        })
        opener = urllib.request.build_opener(handler)
        timeout = max(0.5, float(self.plt_timeout_ms) / 1000.0)
        best = -1
        last_error = ""
        index = 0
        for _ in range(self.plt_attempts):
            # v1.9.19：这个地址失败后，重试换下一个测试地址（设置里那一列地址才会真正被用到）
            url = self.plt_urls[index % len(self.plt_urls)]
            start = time.perf_counter()
            try:
                # v1.9.15：必须带正常 User-Agent（urllib 默认的 Python-urllib 会被 Cloudflare 回 403），
                # 与 PmsTempCoreSession.ptcs_request() 保持一致。
                request = urllib.request.Request(str(url), headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT})
                with opener.open(request, timeout=timeout) as response:
                    response.read(1024)
                    status = getattr(response, "status", 200)
                if status >= 400:
                    last_error = f"HTTP {status}"
                    index += 1
                    continue
                elapsed = int((time.perf_counter() - start) * 1000)
                best = elapsed if best < 0 else min(best, elapsed)
                last_error = ""
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                index += 1
            except urllib.error.URLError as exc:
                last_error = f"连接失败（{getattr(exc, 'reason', exc)}）"
                index += 1
            except (OSError, ValueError) as exc:
                last_error = f"请求异常（{exc}）"
                index += 1
        if best < 0 and not last_error:
            last_error = "无响应"
        # v1.9.19：成功时说明必须为空——原来这里写 `last_error or "无响应"`，
        # 于是「第一次失败、换地址后成功」的节点会带着一句「无响应」标记为成功，自相矛盾。
        self.plt_signals.ptask_node_done.emit(self.plt_node.uid, best, 0.0, best >= 0, last_error if best < 0 else "")

class PmsSpeedTestTask(QRunnable):
    """单节点下载测速（通过临时核心）。"""

    def __init__(
        self,
        pms_signals: PmsTaskSignals,
        pms_settings: "PmsSettingsManager",
        pms_paths: PmsRuntimePaths,
        pms_core_kind: str,
        pms_core_path: Path,
        pms_node: PmsProxyNode,
        pms_speed_config: dict[str, Any],
        pms_coordinator: Optional[Any] = None,
    ) -> None:
        super().__init__()
        self.pst_signals = pms_signals
        self.pst_settings = pms_settings
        self.pst_paths = pms_paths
        self.pst_core_kind = pms_core_kind
        self.pst_core_path = Path(pms_core_path)
        self.pst_node = pms_node
        self.pst_config = dict(pms_speed_config or {})
        # v1.9.9：调度器引用，用途同 PmsTempCoreTask.plt_coordinator。
        self.pst_coordinator = pms_coordinator
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        session = PmsTempCoreSession(self.pst_settings, self.pst_paths, self.pst_core_kind, self.pst_core_path, self.pst_node)
        coordinator = self.pst_coordinator
        if coordinator is not None:
            coordinator.pst_register_session(session)
        try:
            error = session.ptcs_open()
            if error:
                self.pst_signals.ptask_node_done.emit(self.pst_node.uid, -1, 0.0, False, error)
                return
            url = str(self.pst_config.get("url") or PMS_DEFAULT_SPEED_TEST_URL)
            total_bytes, elapsed, error_text = session.ptcs_download_measure(
                url,
                pms_safe_int(self.pst_config.get("duration_seconds"), 10),
                pms_safe_int(self.pst_config.get("limit_bytes"), 32 * 1024 * 1024),
                pms_progress=lambda downloaded, seconds: self.pst_signals.ptask_progress.emit(
                    self.pst_node.uid, int(downloaded), int(seconds * 1000)
                ),
                pms_min_samples=pms_safe_int(self.pst_config.get("min_samples"), 3),
            )
            if total_bytes <= 0:
                self.pst_signals.ptask_node_done.emit(self.pst_node.uid, -1, 0.0, False, error_text or "未取到数据")
                return
            # v1.9.16：优先用稳态速度（丢掉握手/慢启动那一段），没有采样就回落整体平均
            speed = float(getattr(session, "ptcs_last_speed", 0.0) or 0.0) or (total_bytes / max(0.001, elapsed))
            self.pst_signals.ptask_node_done.emit(self.pst_node.uid, int(elapsed * 1000), speed, True, "")
        finally:
            session.ptcs_close()
            if coordinator is not None:
                coordinator.pst_unregister_session(session)

# --------------------------------------------------------------------------------------
# TCP 握手延迟测试任务（PmsMainWindow 使用，定义在类之后以便主线程信号排队）
# --------------------------------------------------------------------------------------
class PmsTcpPingSignals(QObject):
    """TCP 延迟结果信号。"""

    ptcp_result = Signal(str, int, str)   # 节点 uid, 延迟毫秒（-1 失败）, 错误说明

class PmsTcpPingTask(QRunnable):
    """对节点服务器端口做一次 TCP 握手测速（不经过代理，真实网络往返）。"""

    def __init__(self, pms_node: PmsProxyNode, pms_timeout_ms: int, pms_signals: PmsTcpPingSignals) -> None:
        super().__init__()
        self.pmw_node = pms_node
        self.pmw_timeout_ms = max(200, int(pms_timeout_ms))
        self.pmw_signals = pms_signals
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        host = str(self.pmw_node.server or "").strip()
        port = int(self.pmw_node.port or 0)
        if not host or not 0 < port < 65536:
            self.pmw_signals.ptcp_result.emit(self.pmw_node.uid, -1, "地址或端口无效")
            return
        timeout = self.pmw_timeout_ms / 1000.0
        started = time.perf_counter()
        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            elapsed = int((time.perf_counter() - started) * 1000)
            self.pmw_signals.ptcp_result.emit(self.pmw_node.uid, max(1, elapsed), "")
        except socket.timeout:
            self.pmw_signals.ptcp_result.emit(self.pmw_node.uid, -1, f"连接超时（>{self.pmw_timeout_ms} ms）")
        except OSError as exc:
            self.pmw_signals.ptcp_result.emit(self.pmw_node.uid, -1, f"连接失败：{exc}")
        finally:
            try:
                sock.close()
            except OSError:
                pass
