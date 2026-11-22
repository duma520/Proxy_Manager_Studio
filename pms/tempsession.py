#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时核心会话

为单个节点临时拉起核心（本机 HTTP 入站），用于真实延迟测量与真实下载测速。
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Callable,
    Optional,
)
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from pms.constants import (
    PMS_CORE_KIND_XRAY,
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_IS_WINDOWS,
    PMS_LOGGER_NAME,
    PMS_ROUTING_MODE_GLOBAL,
)
from pms.utils import (
    pms_atomic_write_text,
    pms_find_free_port,
    pms_is_port_available,
    pms_json_pretty,
)
from pms.paths import PmsRuntimePaths
from pms.models import PmsProxyNode
from pms.coreprofile import PmsCoreLaunchProfile
from pms.coreconfig import (
    PmsSingBoxConfigBuilder,
    PmsXrayConfigBuilder,
)

# --------------------------------------------------------------------------------------
# 临时核心会话（延迟测试 / 测速：单独起一个只带本地 HTTP 入口的核心实例）
# --------------------------------------------------------------------------------------
class PmsTempCoreSession:
    """为一个节点临时拉起核心，提供本机 HTTP 代理入口，用于真实延迟与测速。"""

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_paths: PmsRuntimePaths,
        pms_core_kind: str,
        pms_core_path: Path,
        pms_node: PmsProxyNode,
        pms_logger: Optional[logging.Logger] = None,
    ) -> None:
        self.ptcs_settings = pms_settings
        self.ptcs_paths = pms_paths
        self.ptcs_core_kind = pms_core_kind
        self.ptcs_core_path = Path(pms_core_path)
        self.ptcs_node = pms_node
        self.ptcs_logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".tempsession")
        self.ptcs_process: Optional[subprocess.Popen] = None
        self.ptcs_port = 0
        self.ptcs_config_path: Optional[Path] = None
        self.ptcs_output: list[str] = []
        self.ptcs_reader: Optional[threading.Thread] = None
        self.ptcs_lock = threading.RLock()
        # v1.9.9：退出/停止时要把阻塞在 IO 上的测量打断，否则线程池线程一直不回来，
        # Python 进程退不出去（表现就是「退出流程完成」但命令行窗口还卡着）。
        self.ptcs_abort_event = threading.Event()
        self.ptcs_response: Optional[Any] = None
        # v1.9.16：最近一次下载测速的稳态速度（B/s）；0 表示没有采样结果，调用方回落 总字节/总耗时
        self.ptcs_last_speed = 0.0

    # ------------------------------------------------------------------ 中断（v1.9.9）
    def ptcs_is_aborted(self) -> bool:
        """是否已请求中断（测量循环里定期问一次，收到立刻收手）。"""
        return self.ptcs_abort_event.is_set()

    def ptcs_set_response(self, pms_response: Optional[Any]) -> None:
        """记下当前正在读的 urllib 响应，供 `ptcs_abort()` 直接关掉底层连接。"""
        self.ptcs_response = pms_response

    def ptcs_abort(self) -> None:
        """请求中断：置标志 + 关闭正在读的响应 + 关掉临时核心。

        关响应这一步是关键——测量线程卡在 `response.read()` 上，光置标志没用，
        必须让底层 socket 断开，read() 才能抛异常退出来。
        """
        self.ptcs_abort_event.set()
        response = self.ptcs_response
        self.ptcs_response = None
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - 关闭失败无关紧要，标志位已经能兜住
                pass
        self.ptcs_close()

    # ------------------------------------------------------------------ 配置
    def ptcs_build_config(self) -> dict[str, Any]:
        """最小配置：仅一个本机 HTTP 入口 + 目标节点出站。"""
        node = self.ptcs_node
        profile = PmsCoreLaunchProfile.pclp_from_settings(self.ptcs_settings, node, self.ptcs_core_kind)
        profile.tun_enabled = False
        profile.routing_mode = PMS_ROUTING_MODE_GLOBAL
        profile.udp_enabled = True
        profile.mux_enabled = False
        profile.clash_api_port = 0
        profile.stats_api_port = 0
        profile.sniff_enabled = False
        if self.ptcs_core_kind == PMS_CORE_KIND_XRAY:
            outbounds = PmsXrayConfigBuilder().pxc_build_outbounds(profile)
            return {
                "log": {"loglevel": "warning", "timestamp": True},
                "inbounds": [{
                    "tag": "probe-in",
                    "listen": "127.0.0.1",
                    "port": int(self.ptcs_port),
                    "protocol": "http",
                    "settings": {"accounts": [], "allowTransparent": False},
                }],
                "outbounds": outbounds[:2],
                "routing": {"domainStrategy": "AsIs", "rules": []},
            }
        outbound = PmsSingBoxConfigBuilder().psc_build_proxy_outbound(profile)
        return {
            "log": {"level": "warning", "timestamp": True},
            "dns": PmsSingBoxConfigBuilder.psc_build_minimal_dns(profile.dns_direct),
            "inbounds": [{
                "type": "http",
                "tag": "probe-in",
                "listen": "127.0.0.1",
                "listen_port": int(self.ptcs_port),
            }],
            "outbounds": [outbound, {"type": "direct", "tag": "direct", "domain_resolver": "dns-direct"}],
            "route": {"rules": [], "final": "proxy", "auto_detect_interface": True,
                      "default_domain_resolver": {"server": "dns-direct", "strategy": "prefer_ipv4"}},
        }

    # ------------------------------------------------------------------ 生命周期
    def ptcs_open(self, pms_timeout: float = 12.0, pms_attempts: int = 2) -> str:
        """启动临时核心并等待端口就绪；返回错误说明（空串表示成功）。

        v1.9.16：**端口撞车自动换端口重试**。用户实测库里有一批 `临时核心已退出：…
        bind: Only one usage of each socket address` 的失败——端口在「探测空闲」到「核心真正
        bind」之间被别的进程/上一轮还没退干净的核心占走了。这类失败重试一次就能过，
        所以识别出 bind 冲突就换一个新端口再试（默认最多 2 次），其它错误立即返回。
        """
        error = ""
        for _attempt in range(max(1, int(pms_attempts))):
            error = self._ptcs_open_once(pms_timeout)
            if not error:
                return ""
            if "Only one usage of each socket address" not in error and "bind" not in error:
                return error
        return error

    def _ptcs_open_once(self, pms_timeout: float) -> str:
        """单次启动尝试（内部用；失败由 `ptcs_open()` 决定要不要换端口重试）。"""
        # 重试前把上一轮的输出与残留进程清掉（**不能调 ptcs_close()**：它会置 abort 标志，
        # 重试会被当成「程序正在退出」直接失败）。
        with self.ptcs_lock:
            self.ptcs_output.clear()
        previous = self.ptcs_process
        self.ptcs_process = None
        if previous is not None and previous.poll() is None:
            try:
                previous.terminate()
                previous.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    previous.kill()
                except OSError:
                    pass
        self.ptcs_port = pms_find_free_port(24000 + (os.getpid() % 2000), "127.0.0.1")
        config = self.ptcs_build_config()
        config_path = self.ptcs_paths.prt_temp_config_file(f"probe_{self.ptcs_node.uid[:8]}")
        if not pms_atomic_write_text(config_path, pms_json_pretty(config)):
            return "临时配置写入失败"
        self.ptcs_config_path = config_path
        command = [str(self.ptcs_core_path)]
        if self.ptcs_core_kind == PMS_CORE_KIND_XRAY:
            command += ["run", "-c", str(config_path)]
        else:
            command += ["run", "-c", str(config_path), "-D", str(self.ptcs_paths.prt_data_dir)]
        environment = dict(os.environ)
        if self.ptcs_core_kind == PMS_CORE_KIND_XRAY:
            environment["XRAY_LOCATION_ASSET"] = str(self.ptcs_core_path.parent)
        try:
            self.ptcs_process = subprocess.Popen(
                command,
                cwd=str(self.ptcs_core_path.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if PMS_IS_WINDOWS else 0,  # type: ignore[attr-defined]
            )
        except OSError as exc:
            return f"临时核心启动失败：{exc}"
        self.ptcs_reader = threading.Thread(target=self.ptcs_read_output, daemon=True)
        self.ptcs_reader.start()
        deadline = time.time() + max(2.0, float(pms_timeout))
        while time.time() < deadline:
            if self.ptcs_is_aborted():        # v1.9.9：退出时不再空等端口就绪
                self.ptcs_close()
                return "临时核心启动被打断（程序正在退出）"
            if self.ptcs_process.poll() is not None:
                return "临时核心已退出：" + self.ptcs_last_output()
            if pms_is_port_available(self.ptcs_port, "127.0.0.1"):
                time.sleep(0.05)
                continue
            time.sleep(0.02)
            if self.ptcs_is_aborted():
                self.ptcs_close()
                return "临时核心启动被打断（程序正在退出）"
            return ""
        return "临时核心端口未就绪（超时）" + ("：" + self.ptcs_last_output() if self.ptcs_output else "")

    def ptcs_read_output(self) -> None:
        process = self.ptcs_process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                with self.ptcs_lock:
                    self.ptcs_output.append(line.rstrip("\n"))
                    if len(self.ptcs_output) > 120:
                        del self.ptcs_output[:60]
        except (ValueError, OSError):
            return

    def ptcs_last_output(self, pms_lines: int = 3) -> str:
        with self.ptcs_lock:
            return " | ".join(self.ptcs_output[-pms_lines:])[:300]

    def ptcs_close(self) -> None:
        """关闭临时核心并删除临时配置。

        v1.9.9：退出路径上这里会被调用两回（调度器 abort 一回、任务自己的 finally 一回），
        所以必须幂等——先置标志、取走 response（置 None），进程句柄也取走（置 None），
        重复调用不会重复关闭。关进程改成「先 terminate 等 1 秒，再 kill 等 2 秒」，
        原来无条件等 4 秒会把收尾拖长，退出时就卡在这里。
        """
        self.ptcs_abort_event.set()
        response = self.ptcs_response
        self.ptcs_response = None
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        process = self.ptcs_process
        self.ptcs_process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                    process.wait(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        if self.ptcs_config_path is not None:
            try:
                self.ptcs_config_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ 测量
    def ptcs_open_proxy(self) -> Any:
        """构造指向临时入口的 urllib opener。"""
        handler = urllib.request.ProxyHandler({
            "http": f"http://127.0.0.1:{self.ptcs_port}",
            "https": f"http://127.0.0.1:{self.ptcs_port}",
        })
        return urllib.request.build_opener(handler)

    def ptcs_request(self, pms_url: str, pms_extra_headers: Optional[dict[str, str]] = None) -> Any:
        """构造带正常 User-Agent 的请求。

        v1.9.15：**延迟与测速都必须带 UA**。用 urllib 的默认 UA（`Python-urllib/3.x`）请求
        `https://speed.cloudflare.com/__down`，Cloudflare 直接回 **403 Forbidden**；
        实测同样地址换浏览器 UA / curl UA / 甚至空 UA 都是 200。
        而「自检可用档位」（`pms_probe_speed_url`）本来就带 UA，于是出现
        「自检说地址可用、真测速全 403」的怪象——根因就在这里。
        """
        headers = {"User-Agent": PMS_DEFAULT_SUB_USER_AGENT}
        headers.update(dict(pms_extra_headers or {}))
        return urllib.request.Request(str(pms_url), headers=headers)

    def ptcs_measure_latency(self, pms_url: str, pms_timeout_ms: int, pms_attempts: int = 1) -> tuple[int, str]:
        """经临时核心请求测试地址，返回 (毫秒, 错误说明)；全部失败返回 (-1, 错误)。

        v1.9.9：每轮开始前先问一次 `ptcs_is_aborted()`，并把当次响应记进
        `ptcs_response`——退出时调度器调 `ptcs_abort()` 会关掉它，
        卡在 `response.read()` 上的线程随即抛异常退出，不再死等。
        """
        opener = self.ptcs_open_proxy()
        timeout = max(0.5, float(pms_timeout_ms) / 1000.0)
        last_error = ""
        best = -1
        for _attempt in range(max(1, int(pms_attempts))):
            if self.ptcs_is_aborted():
                return best, (last_error or "已中断")
            start = time.perf_counter()
            try:
                response = opener.open(self.ptcs_request(pms_url), timeout=timeout)
                self.ptcs_set_response(response)
                try:
                    response.read(1024)
                    status = getattr(response, "status", 200)
                finally:
                    self.ptcs_set_response(None)
                    response.close()
                if status >= 400:
                    last_error = f"HTTP {status}"
                    continue
                elapsed = int((time.perf_counter() - start) * 1000)
                best = elapsed if best < 0 else min(best, elapsed)
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
            except urllib.error.URLError as exc:
                last_error = f"连接失败（{getattr(exc, 'reason', exc)}）"
            except (OSError, ValueError) as exc:
                last_error = f"请求异常（{exc}）"
        if best < 0:
            return -1, last_error or "无响应"
        return best, ""

    def ptcs_download_measure(
        self,
        pms_url: str,
        pms_duration_seconds: int,
        pms_limit_bytes: int,
        pms_progress: Optional[Callable[[int, float], None]] = None,
        pms_min_samples: int = 3,
    ) -> tuple[int, float, str]:
        """下载测速：返回 (字节数, 耗时秒, 错误说明)。

        v1.9.9：把响应记进 `ptcs_response` 供 `ptcs_abort()` 关闭，
        并在每轮 `read()` 前问一次 `ptcs_is_aborted()`。
        原来只有 `deadline / limit` 两个出口，退出时线程会一路读到 20 秒超时才回来。

        v1.9.16：**用上 `speed.min_samples`（原来这个设置项没人读）**。
        下载过程中按间隔记「瞬时吞吐样本」，样本够（≥ min_samples）就丢掉**第一个**样本
        （含 TLS 握手与 TCP 慢启动，明显偏低）再取其余样本的平均值作为速度——
        这样报出来的数更接近常见测速软件的口径，而不是被握手期拖低；
        样本不足（比如几秒就下完的快节点）时**保持原来的「总字节 / 总耗时」**，行为不变。

        返回值仍是 `(总字节, 耗时秒, 错误说明)`（**契约不变**，调用方与探针都不用改）——
        采样后的速度放在 `ptcs_last_speed`（B/s），调用方优先取它：
            `speed = session.ptcs_last_speed or total / elapsed`
        """
        if self.ptcs_is_aborted():
            return 0, 0.0, "已中断"
        opener = self.ptcs_open_proxy()
        duration = max(1, int(pms_duration_seconds))
        samples_count = max(1, int(pms_min_samples))
        sample_interval = max(0.2, duration / float(samples_count))
        deadline = time.time() + duration
        total = 0
        started = time.perf_counter()
        self.ptcs_last_speed = 0.0
        samples: list[float] = []
        sample_base_bytes = 0
        sample_base_time = started
        try:
            response = opener.open(self.ptcs_request(pms_url), timeout=20)
            self.ptcs_set_response(response)
            try:
                while True:
                    if self.ptcs_is_aborted():
                        break
                    if time.time() >= deadline or total >= int(pms_limit_bytes):
                        break
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    now = time.perf_counter()
                    if now - sample_base_time >= sample_interval:
                        samples.append((total - sample_base_bytes) / max(0.001, now - sample_base_time))
                        sample_base_bytes = total
                        sample_base_time = now
                    if pms_progress is not None:
                        pms_progress(total, now - started)
            finally:
                self.ptcs_set_response(None)
                response.close()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if self.ptcs_is_aborted():
                return total, max(0.001, time.perf_counter() - started), "已中断"
            if total <= 0:
                return 0, 0.0, f"下载失败（{exc}）"
        elapsed = max(0.001, time.perf_counter() - started)
        self.ptcs_last_speed = self.ptcs_steady_speed(total, elapsed, samples, samples_count)
        return total, elapsed, ""

    @staticmethod
    def ptcs_steady_speed(
        pms_total_bytes: int,
        pms_elapsed: float,
        pms_samples: list[float],
        pms_min_samples: int,
    ) -> float:
        """稳态速度（B/s）：样本够就丢掉第一个（握手 + 慢启动）取平均，否则回落整体平均。

        纯函数（不碰会话状态），便于离线探针直接验证；两种情况都返回 > 0 的速率。
        """
        if len(pms_samples) >= max(2, int(pms_min_samples) + 1):
            steady = pms_samples[1:]
            average = sum(steady) / max(1, len(steady))
            if average > 0:
                return float(average)
        return float(pms_total_bytes) / max(0.001, float(pms_elapsed))
