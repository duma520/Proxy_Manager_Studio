#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram 数据中心（DC）探测（v1.9.59）

**为什么测 DC 而不是 t.me**（用户 2026-10-15 指正）：电报客户端连的是 MTProto 数据中心端口，
t.me 只是网页预览（HTML + CDN），t.me 通 ≠ 电报软件能收发。所以连通性以 **DC 的
TCP 可达 + MTProto `req_pq_multi` → `resPQ` 真握手** 为准。

**探测三层**（对应确认过的 A / B / C 三口径，全部产出、分列展示）：
  A. 最快 DC 延迟：`CONNECT dc_ip:443` 起表，拿到 resPQ 为止的往返毫秒（逐 DC 各测一次）。
  B. 可达 DC 数：五个生产 DC 逐个探测，`x/5`（TCP 通但 MTProto 无响应记「部分」）。
  C. HTTP 吞吐（尽力而为）：经隧道对 DC 发一次明文 HTTP GET，**能拿到真实 HTTP 响应**才计
     字节/秒；拿不到（DC 是 MTProto 协议端口，这是常态）就**不显示速度，绝不编造数字**——
     这是用户定死的口径：「如果拿不到有效 HTTP 资源，电报测速就不做测速，只做 A 和 B」。

**已核实的协议事实**（2026-10-15，来源都写明，不许凭想象改）：
  · DC 地址：Pyrogram 官方 FAQ《What are the IP addresses of Telegram Data Centers?》生产环境表
    （DC1 149.154.175.53 / DC2 149.154.167.51 / DC3 149.154.175.100 / DC4 149.154.167.91 /
    DC5 91.108.56.130）；另用 alexbers·geovex 等开源实现交叉核对到一批**历史别名**
    （149.154.175.50 / 149.154.167.50 / 149.154.171.5），主地址失败时按序兜底重试。
  · MTProto 构造器：`req_pq_multi#be7e8ef1 nonce:int128 = ResPQ`、
    `resPQ#05162463 ...`、`new_session_created#9ec20908 ...`——编号取自 tl.telethon.dev 页面原文。
  · 传输层首字节：abridged `0xEF`、intermediate `0xEEEEEEEE`（tgp v0.9.11 常量与
    ferogram transport_kind.rs 两处交叉一致）。本探测用 **abridged（0xEF）**，明文消息
    （握手前无会话密钥，req_pq_multi 本来就允许明文发送）。
  · abridged 未加密消息帧：`int32(消息字节数/4)` + `msg_id(int64)` + TL 载荷；
    req_pq_multi 载荷 = 构造器 4B + nonce 16B = 20B，帧体 28B，长度前缀 = 7。

★ 常量放置：列索引 `PMS_COLUMN_TELEGRAM` 放 `pms/constants.py`（与其余 12 个列常量同处）；
  本模块只放 DC 清单与 MTProto 常量。探测入口是**私有函数** `_pms_telegram_probe_dcs`——
  启动器 `Proxy_Manager_Studio.py` 顶在 400 行红线（v1.9.59 实测 399），公开函数要在启动器
  再导出就直接破线（宪法铁律：新工具函数优先私有），且它只有 `dctest.py` 一个调用方。
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import time
from typing import Any, Optional

from pms.constants import PMS_LOGGER_NAME

# --------------------------------------------------------------------------------------
# DC 清单与端口（核实来源见模块 docstring）
# --------------------------------------------------------------------------------------
# (名称, 地址候选表)——第一个是 Pyrogram 生产表的主地址，其余是历史别名兜底。
PMS_TELEGRAM_DC_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DC1", ("149.154.175.53", "149.154.175.50")),
    ("DC2", ("149.154.167.51", "149.154.167.50")),
    ("DC3", ("149.154.175.100",)),
    ("DC4", ("149.154.167.91",)),
    ("DC5", ("91.108.56.130", "149.154.171.5")),
)

PMS_TELEGRAM_DC_PORT = 443

# MTProto / TL 常量（编号已核实）
_PMS_MTPROTO_REQ_PQ_MULTI = 0xBE7E8EF1
_PMS_MTPROTO_RES_PQ = 0x05162463
_PMS_MTPROTO_NEW_SESSION_CREATED = 0x9EC20908
_PMS_MTPROTO_TRANSPORT_ABRIDGED = 0xEF
_PMS_MTPROTO_MSG_ID_SHIFT = 23          # 客户端消息 msg_id 对齐约定（低两位为 0）
_PMS_DC_RECV_CHUNK = 4096
_PMS_DC_MAX_FRAMES = 3                  # 一帧应答最多跳过几条无关容器（防死循环）


def _pms_recv_exactly(pms_socket: socket.socket, pms_count: int, pms_deadline: float) -> bytes:
    """收满 pms_count 字节才返回；超时 / 断开 / 到点返回已收部分（调用方按失败处理）。"""
    buffer = bytearray()
    while len(buffer) < pms_count:
        left = pms_deadline - time.monotonic()
        if left <= 0:
            return bytes(buffer)
        pms_socket.settimeout(min(left, 5.0))
        try:
            chunk = pms_socket.recv(min(pms_count - len(buffer), _PMS_DC_RECV_CHUNK))
        except (socket.timeout, OSError):
            return bytes(buffer)
        if not chunk:
            return bytes(buffer)
        buffer.extend(chunk)
    return bytes(buffer)


def _pms_dc_open_tunnel(pms_proxy_host: str, pms_proxy_port: int, pms_dc_host: str,
                        pms_dc_port: int, pms_timeout: float) -> tuple[Optional[socket.socket], float, str]:
    """经临时核心的 HTTP 代理向 DC 发起 CONNECT 隧道。

    返回 (隧道 socket 或 None, 毫秒, 错误说明)——毫秒就是 A 口径的「TCP 可达延迟」：
    从发起 CONNECT 到代理回 200（代理与 DC 的 TCP 握手已完成）的往返时间。
    """
    started = time.perf_counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(pms_timeout)
    try:
        sock.connect((pms_proxy_host, int(pms_proxy_port)))
    except OSError as exc:
        sock.close()
        return None, 0.0, f"代理不可达（{exc}）"
    request = (
        f"CONNECT {pms_dc_host}:{pms_dc_port} HTTP/1.1\r\n"
        f"Host: {pms_dc_host}:{pms_dc_port}\r\n\r\n"
    ).encode("ascii")
    try:
        sock.sendall(request)
        head = b""
        deadline = started + pms_timeout
        while b"\r\n\r\n" not in head:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(max(0.1, min(left, 5.0)))
            chunk = sock.recv(512)
            if not chunk:
                break
            head += chunk
            if len(head) > 8192:
                break
    except OSError as exc:
        sock.close()
        return None, 0.0, f"CONNECT 失败（{exc}）"
    elapsed_ms = max(1, int((time.perf_counter() - started) * 1000))
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace") if head else "无响应"
    if b" 200" not in head[:32]:
        sock.close()
        return None, elapsed_ms, f"CONNECT 被拒（{status_line[:40]}）"
    return sock, elapsed_ms, ""


def _pms_abridged_write_frame(pms_body: bytes) -> bytes:
    """abridged 出站帧：首字节 = 字数（字节数/4）；≥128 时写 0x00 + 3 字节小端字数。

    （已核实的规范，见模块 docstring 来源。）
    """
    words = len(pms_body) // 4
    if words < 128:
        return bytes([words]) + pms_body
    return bytes([0]) + struct.pack("<I", words)[:3] + pms_body


def _pms_abridged_read_frame(pms_socket: socket.socket, pms_deadline: float) -> tuple[Optional[bytes], str]:
    """abridged 入站帧：读 1 字节字数（0 则再读 3 字节），再读 字数×4 字节。

    返回 (载荷 或 None, 错误说明)。
    """
    first = _pms_recv_exactly(pms_socket, 1, pms_deadline)
    if len(first) < 1:
        return None, "MTProto 应答截断"
    words = first[0]
    if words == 0:
        extra = _pms_recv_exactly(pms_socket, 3, pms_deadline)
        if len(extra) < 3:
            return None, "MTProto 长度头截断"
        words = int.from_bytes(extra, "little")
    if words <= 0 or words > 1024 * 1024:
        return None, f"MTProto 帧长异常（{words}）"
    payload = _pms_recv_exactly(pms_socket, words * 4, pms_deadline)
    if len(payload) < 12:
        return None, "MTProto 载荷截断"
    return payload, ""


def _pms_mtproto_req_pq(pms_socket: socket.socket, pms_deadline: float) -> tuple[int, str]:
    """在已建立的 DC 隧道上做一次 MTProto 握手（abridged + req_pq_multi → resPQ）。

    返回 (0=拿到 resPQ / -1=失败, 错误说明)。耗时由调用方掐表。
    """
    try:
        pms_socket.sendall(bytes([_PMS_MTPROTO_TRANSPORT_ABRIDGED]))
        nonce = os.urandom(16)
        msg_id = (int(time.time() * 1000) << _PMS_MTPROTO_MSG_ID_SHIFT) | (os.urandom(1)[0] & 0b111)
        msg_id &= ~0b11
        body = struct.pack("<QI", msg_id, _PMS_MTPROTO_REQ_PQ_MULTI) + nonce
        pms_socket.sendall(_pms_abridged_write_frame(body))
        for _ in range(_PMS_DC_MAX_FRAMES):
            if pms_deadline - time.monotonic() <= 0:
                return -1, "MTProto 应答超时"
            payload, error = _pms_abridged_read_frame(pms_socket, pms_deadline)
            if payload is None:
                return -1, error
            constructor = struct.unpack("<I", payload[8:12])[0]
            if constructor == _PMS_MTPROTO_RES_PQ:
                # TL 布局 resPQ = 构造器(4) + nonce(16) + …：nonce 必回显本次请求，
                # 不核对就可能把别的会话的应答当成「本节点能连 DC」（规范保证，校验零成本）。
                if payload[12:28] != nonce:
                    continue      # 不是本会话的应答，读下一帧
                return 0, ""
            if constructor == _PMS_MTPROTO_NEW_SESSION_CREATED:
                continue          # 服务器可能先发 salt 通知，跳过再读下一帧
            return -1, f"应答构造器未知（{constructor:#010x}）"
        return -1, "没等到本会话的 resPQ"
    except OSError as exc:
        return -1, f"MTProto 收发失败（{exc}）"


def _pms_dc_http_throughput(pms_proxy_host: str, pms_proxy_port: int, pms_dc_host: str,
                            pms_dc_port: int, pms_timeout: float, pms_limit_bytes: int,
                            pms_abort_event: Optional[Any]) -> tuple[int, float, str]:
    """C 口径（尽力而为）：对 DC 端口试一次明文 HTTP GET，取回多少字节 / 用了多久。

    DC 是 MTProto 协议端口，**大概率不是 HTTP**——返回 (0, 0, 原因) 就是正常结果，
    调用方据此决定「不显示速度」，绝不拿握手时间编造带宽。
    """
    sock, connect_ms, error = _pms_dc_open_tunnel(pms_proxy_host, pms_proxy_port, pms_dc_host, pms_dc_port, pms_timeout)
    if sock is None:
        return 0, 0.0, error or f"隧道失败（{connect_ms}ms）"
    try:
        request = f"GET / HTTP/1.1\r\nHost: {pms_dc_host}\r\nConnection: close\r\n\r\n".encode("ascii")
        sock.sendall(request)
        deadline = time.monotonic() + max(1.0, pms_timeout)
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            if pms_abort_event is not None and pms_abort_event.is_set():
                return 0, 0.0, "已中断"
            left = deadline - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(max(0.1, min(left, 5.0)))
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        head_text = head.decode("latin-1", errors="replace")
        if "HTTP/" not in head_text[:16]:
            return 0, 0.0, "DC 端口不响应 HTTP（正常，MTProto 协议端口）"
        total = len(head.split(b"\r\n\r\n", 1)[1]) if b"\r\n\r\n" in head else 0
        started = time.perf_counter()
        while total < pms_limit_bytes:
            if pms_abort_event is not None and pms_abort_event.is_set():
                break
            left = deadline - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(max(0.05, min(left, 5.0)))
            try:
                chunk = sock.recv(_PMS_DC_RECV_CHUNK)
            except OSError:
                break
            if not chunk:
                break
            total += len(chunk)
        elapsed = max(0.001, time.perf_counter() - started)
        if total <= 0:
            return 0, 0.0, "HTTP 响应无正文"
        return total, elapsed, ""
    except OSError as exc:
        return 0, 0.0, f"HTTP 探测失败（{exc}）"
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _pms_telegram_probe_dcs(pms_proxy_host: str, pms_proxy_port: int, pms_timeout: float,
                           pms_abort_event: Optional[Any] = None,
                           pms_try_throughput_dc: Optional[str] = "",
                           pms_throughput_limit_bytes: int = 2 * 1024 * 1024,
                           pms_logger: Optional[logging.Logger] = None) -> dict[str, Any]:
    """逐 DC 探测（B：可达数 / A：最快延迟 / 协议层判定；C：对最快的可达 DC 试一次吞吐）。

    返回字典：
        ok          有任一 DC 完成 MTProto 握手（电报「真」能连上）
        proto_count 拿到 resPQ 的 DC 数
        tcp_count    TCP 可达的 DC 数（含 proto_count）
        total        探测的 DC 总数（PMS_TELEGRAM_DC_TARGETS 长度）
        best_ms      A 口径：所有成功往返里最快的毫秒（优先 MTProto 往返，回落 TCP）
        best_dc      best_ms 属于哪个 DC
        speed_bps    C 口径：HTTP 吞吐字节/秒；拿不到 HTTP 资源 = 0.0（**不编造**）
        per_dc       [(名称, 状态码, 毫秒)] 状态码 proto / tcp / fail
        note         逐 DC 一行汇总文本（含失败原因）
    """
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".dcprobe")
    per_dc: list[tuple[str, str, int]] = []
    notes: list[str] = []
    proto_count = 0
    tcp_count = 0
    best_ms = -1
    best_dc = ""
    for name, addresses in PMS_TELEGRAM_DC_TARGETS:
        if pms_abort_event is not None and pms_abort_event.is_set():
            per_dc.append((name, "fail", -1))
            notes.append(f"{name} 中断")
            continue
        status = "fail"
        elapsed_ms = -1
        reason = ""
        for host in addresses:
            sock, connect_ms, error = _pms_dc_open_tunnel(pms_proxy_host, pms_proxy_port, host, PMS_TELEGRAM_DC_PORT, pms_timeout)
            if sock is None:
                reason = error
                continue
            tcp_count += 1
            roundtrip_start = time.perf_counter()
            deadline = roundtrip_start + max(1.0, pms_timeout)
            pq_result, pq_error = _pms_mtproto_req_pq(sock, deadline)
            total_ms = max(connect_ms, int((time.perf_counter() - roundtrip_start) * 1000))
            try:
                sock.close()
            except OSError:
                pass
            if pq_result == 0:
                proto_count += 1
                status = "proto"
                elapsed_ms = total_ms
                reason = ""
            else:
                status = "tcp"
                elapsed_ms = connect_ms
                reason = pq_error
            break
        per_dc.append((name, status, elapsed_ms))
        label = {"proto": "MTProto✓", "tcp": "仅TCP", "fail": "不通"}[status]
        timing = f"{elapsed_ms}ms" if elapsed_ms >= 0 else "-"
        notes.append(f"{name} {timing} {label}" + (f"（{reason[:36]}）" if status != "proto" and reason else ""))
    # best 单独算：proto 层只比 proto，全没有 proto 才比 tcp
    proto_entries = [(n, ms) for (n, s, ms) in per_dc if s == "proto" and ms >= 0]
    tcp_entries = [(n, ms) for (n, s, ms) in per_dc if s in ("proto", "tcp") and ms >= 0]
    pool = proto_entries or tcp_entries
    if pool:
        best_dc, best_ms = min(pool, key=lambda item: item[1])
    speed_bps = 0.0
    speed_note = ""
    target_name = str(pms_try_throughput_dc or "") or best_dc      # 留空 = 自动挑最快 DC
    if proto_count > 0 and target_name:
        target = next((item for item in PMS_TELEGRAM_DC_TARGETS if item[0] == target_name), None)
        entry = next((item for item in per_dc if item[0] == target_name), None)
        if target is not None and entry is not None and entry[1] == "proto":
            total_bytes, seconds, error = _pms_dc_http_throughput(
                pms_proxy_host, pms_proxy_port, target[1][0], PMS_TELEGRAM_DC_PORT,
                pms_timeout, pms_throughput_limit_bytes, pms_abort_event, )
            if total_bytes > 0:
                speed_bps = total_bytes / max(0.001, seconds)
                speed_note = f"C：{target_name} HTTP 吞吐 {total_bytes}B/{seconds:.2f}s"
            else:
                speed_note = f"C：无 HTTP 吞吐（{error[:40]}）"
    note = " / ".join(notes)
    if speed_note:
        note += " ｜ " + speed_note
    ok = proto_count > 0
    logger.info("电报 DC 探测：proto %d / tcp %d / 总 %d，最快 %s %dms，吞吐 %.0fB/s",
                proto_count, tcp_count, len(PMS_TELEGRAM_DC_TARGETS), best_dc or "-", best_ms, speed_bps)
    return {
        "ok": ok,
        "proto_count": proto_count,
        "tcp_count": tcp_count,
        "total": len(PMS_TELEGRAM_DC_TARGETS),
        "best_ms": best_ms,
        "best_dc": best_dc,
        "speed_bps": speed_bps,
        "per_dc": per_dc,
        "note": note,
    }
