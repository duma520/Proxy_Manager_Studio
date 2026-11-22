#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网络诊断工具（第 14 项）

提供一组**不依赖系统代理**的本地网络检查：DNS 解析、TCP 端口可达性、直连 HTTP 访问、
本地核心入口端口监听探测、ICMP 探测。结果与 GUI、API 共用统一结构，便于直接展示。
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from pms.utils import pms_now_text


class PmsNetworkDiagnostics:
    """单实例网络诊断器：每个方法返回统一结构的检查结果字典。

    结果字典字段：category / name / target / ok / detail / latency_ms
        · ok        是否成功
        · latency_ms 耗时（毫秒，-1 表示不适用或失败）
        · detail    人类可读说明
    """

    def __init__(self, pms_timeout_ms: int = 3000, pms_logger: Optional[Any] = None) -> None:
        self.pnd_timeout_ms = max(200, int(pms_timeout_ms))
        self.pnd_timeout = self.pnd_timeout_ms / 1000.0
        self.pnd_logger = pms_logger

    def pnd_resolve(self, pms_host: str, pms_label: str = "") -> dict[str, Any]:
        """DNS 解析：返回该主机解析到的 IP 列表。"""
        started = time.perf_counter()
        try:
            infos = socket.getaddrinfo(str(pms_host), None)
            ips = sorted({info[4][0] for info in infos})
            elapsed = int((time.perf_counter() - started) * 1000)
            return {"category": "dns", "name": pms_label or "DNS 解析", "target": str(pms_host),
                    "ok": bool(ips), "detail": "、".join(ips[:4]) or "无地址", "latency_ms": elapsed}
        except (socket.gaierror, OSError) as exc:
            return {"category": "dns", "name": pms_label or "DNS 解析", "target": str(pms_host),
                    "ok": False, "detail": f"解析失败：{exc}", "latency_ms": -1}

    def pnd_tcp(self, pms_host: str, pms_port: int, pms_label: str = "") -> dict[str, Any]:
        """TCP 端口可达性：真实建连一次（不经代理）。"""
        started = time.perf_counter()
        host = str(pms_host)
        try:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(self.pnd_timeout)
            sock.connect((host, int(pms_port)))
            elapsed = int((time.perf_counter() - started) * 1000)
            sock.close()
            return {"category": "tcp", "name": pms_label or "TCP 连接", "target": f"{host}:{int(pms_port)}",
                    "ok": True, "detail": "端口可达", "latency_ms": elapsed}
        except (socket.timeout, OSError) as exc:
            return {"category": "tcp", "name": pms_label or "TCP 连接", "target": f"{host}:{int(pms_port)}",
                    "ok": False, "detail": f"连接失败：{exc}", "latency_ms": -1}

    def pnd_http(self, pms_url: str, pms_label: str = "") -> dict[str, Any]:
        """直连 HTTP 访问（不经过系统代理，验证真实出网能力）。"""
        started = time.perf_counter()
        url = str(pms_url)
        try:
            with urllib.request.urlopen(url, timeout=self.pnd_timeout) as resp:
                resp.read(1024)
                status = getattr(resp, "status", 200)
            elapsed = int((time.perf_counter() - started) * 1000)
            return {"category": "http", "name": pms_label or "直连 HTTP", "target": url,
                    "ok": int(status) < 400, "detail": f"HTTP {status}", "latency_ms": elapsed}
        except urllib.error.HTTPError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return {"category": "http", "name": pms_label or "直连 HTTP", "target": url,
                    "ok": int(exc.code) < 400, "detail": f"HTTP {exc.code}", "latency_ms": elapsed}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"category": "http", "name": pms_label or "直连 HTTP", "target": url,
                    "ok": False, "detail": f"请求失败：{getattr(exc, 'reason', exc)}", "latency_ms": -1}

    def pnd_local_port(self, pms_port: int, pms_label: str = "", pms_host: str = "127.0.0.1") -> dict[str, Any]:
        """本地端口是否已有进程监听（用于判断核心 API / 入口是否就绪）。"""
        host = str(pms_host)
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(self.pnd_timeout)
            probe.connect((host, int(pms_port)))
            probe.close()
            return {"category": "local", "name": pms_label or "本地端口", "target": f"{host}:{int(pms_port)}",
                    "ok": True, "detail": "端口已监听（服务在线）", "latency_ms": -1}
        except (socket.timeout, OSError):
            return {"category": "local", "name": pms_label or "本地端口", "target": f"{host}:{int(pms_port)}",
                    "ok": False, "detail": "端口无监听（服务未起/未就绪）", "latency_ms": -1}

    def pnd_ping(self, pms_host: str, pms_label: str = "") -> dict[str, Any]:
        """ICMP 探测（尽力而为）：Windows 普通用户常无权限，失败只作参考。"""
        import subprocess
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                ["ping", "-n", "1", "-w", str(self.pnd_timeout_ms), str(pms_host)],
                capture_output=True,
                timeout=self.pnd_timeout + 3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            ok = completed.returncode == 0
            return {"category": "ping", "name": pms_label or "ICMP 探测", "target": str(pms_host),
                    "ok": ok, "detail": "通" if ok else "不通（可能无权限或被防火墙拦截）", "latency_ms": elapsed}
        except Exception as exc:  # noqa: BLE001 - 探测失败绝不影响主流程
            return {"category": "ping", "name": pms_label or "ICMP 探测", "target": str(pms_host),
                    "ok": False, "detail": f"探测失败：{exc}", "latency_ms": -1}


def pms_quick_diagnostics(
    pms_test_url: str,
    pms_core_api_port: int = 0,
    pms_timeout_ms: int = 3000,
) -> list[dict[str, Any]]:
    """一键快速诊断：解析 / 连接测试地址、直连访问测试地址、探测核心 API 端口。"""
    diag = PmsNetworkDiagnostics(pms_timeout_ms=pms_timeout_ms)
    results: list[dict[str, Any]] = []
    url = str(pms_test_url or "").strip()
    host = ""
    port = 0
    if url:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host:
        results.append(diag.pnd_resolve(host, "测试地址 DNS"))
        if port:
            results.append(diag.pnd_tcp(host, port, "测试地址 TCP"))
    if url:
        results.append(diag.pnd_http(url, "直连测试地址"))
    if pms_core_api_port:
        results.append(diag.pnd_local_port(int(pms_core_api_port), "核心 API 端口"))
    return results


def pms_diagnose_targets(pms_targets: list[dict[str, Any]], pms_timeout_ms: int = 3000) -> list[dict[str, Any]]:
    """对任意目标列表做诊断；每个目标形如 {host, port, url, label}。"""
    diag = PmsNetworkDiagnostics(pms_timeout_ms=pms_timeout_ms)
    results: list[dict[str, Any]] = []
    for target in pms_targets:
        host = str(target.get("host") or "")
        port = int(target.get("port") or 0)
        url = str(target.get("url") or "")
        label = str(target.get("label") or "")
        if host and port:
            results.append(diag.pnd_resolve(host, f"{label} DNS" if label else "DNS 解析"))
            results.append(diag.pnd_tcp(host, port, f"{label} TCP" if label else "TCP 连接"))
        if url:
            results.append(diag.pnd_http(url, label or "直连 HTTP"))
    return results
