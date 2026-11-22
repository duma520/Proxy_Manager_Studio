#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram 专属代理（MTProxy）原生连通探测（v1.9.68）

**为什么单独写这个模块**（不是复用 `dcprobe.py`）：
`dcprobe.py` 里的是「经临时核心的 HTTP 代理 → CONNECT 到 Telegram DC → 在隧道上做
MTProto 握手」的探测（测的是**普通代理节点能否连电报**）。而 `t.me/proxy` 给的是
**MTProxy 电报代理本身**——它既不是 HTTP 代理、也不是 SOCKS，而是 MTProto 中间人代理，
Xray / sing-box 都没有 MTProto 出站，所以**不能当通用出口**，只能「直连代理本身、用
MTProxy 混淆握手看它能不能把流量送进电报 DC」。

**握手严格移植自 Telethon `tcpmtproxy.py`**（已核实源码，非凭记忆）：
  1. 解析 secret：dd+16B（PaddedIntermediate）/ ee+16B[+域名]（FakeTLS）/ 裸 16B（Obfuscated Abridged）。
     用于密钥派生的 `proxy_secret` 都是剥掉前缀后的 16 字节；ee 的域名只用于 FakeTLS 的 SNI。
  2. 构造 64 字节混淆头：随机 64 字节 → 写 tag（abridged=0xEF / intermediate=0xEEEEEEEE）到 [56:60]、
     dc_id 到 [60:62]；用 proxy_secret 派生 AES-CTR 的 encrypt/decrypt 两把密钥。
  3. 首包 = `encryptor.encrypt(混淆头)` 发出；随后 `encryptor.encrypt(abridged(req_pq_multi))` 发出。
  4. 收回应按 `decryptor` 解密，解析 abridged 帧，命中 `resPQ` 且 nonce 回显即证明：
     **代理本身在线 + 它能把 MTProto 流量送进电报 DC**（最强连通证据）。

**不实现 FakeTLS 的 TLS 细节**：ee+域名的 FakeTLS 理论上要先 TLS 握手带 SNI，但实测多数
MTProxy 在 443 端口裸 TCP 即接受混淆头（Telethon 默认也是这样连的），所以本探测对 ee 形态
直接用 abridged 头 + 可选 TLS 包装（若后续发现需要再补 `ssl` 包装）。dd / 裸 16B 形态不需要 TLS。

**测试结果口径**（与 dcprobe 一致，绝不编造）：能拿到 resPQ → 可连；TCP 都不通 → 代理不可达；
握手/超时无 resPQ → 代理拒绝或不是 MTProxy。
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from pms.constants import PMS_LOGGER_NAME
from pms.dcprobe import (
    _PMS_MTPROTO_MSG_ID_SHIFT,
    _PMS_MTPROTO_NEW_SESSION_CREATED,
    _PMS_MTPROTO_REQ_PQ_MULTI,
    _PMS_MTPROTO_RES_PQ,
    _PMS_MTPROTO_TRANSPORT_ABRIDGED,
)

# 与 dcprobe 相同的帧常量（这里直接复用 import，不重复定义）

# MTProxy 三种混淆头的 tag（与 Telethon packet_codec.obfuscated_tag 一致）
_PMS_TG_TAG_ABRIDGED = bytes([0xEF])              # abridged（裸 16B / ee 形态）
_PMS_TG_TAG_INTERMEDIATE = b"\xee\xee\xee\xee"   # intermediate（dd 形态，含随机填充）

# 构造混淆头时，首字节 / 前 4 字节 / 4-8 字节必须避开这些「看起来像 HTTP / 像 MTProto 帧」的值
_PMS_TG_RESERVED_PREFIXES = (b"PVRG", b"GET ", b"POST ", b"\xee\xee\xee\xee")


@dataclass
class PmsTgProxySecret:
    """解析后的 MTProxy secret。

    form: "dd" | "ee" | "raw"；domain 仅 ee 形态有（FakeTLS 的 SNI 域名）。
    raw 是**剥掉前缀后**用于密钥派生的 16 字节；secret_original 是用户贴的原始字符串。
    """

    form: str = ""
    raw: bytes = b""
    domain: str = ""
    secret_original: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and len(self.raw) == 16


def pms_tg_parse_secret(pms_secret: str) -> PmsTgProxySecret:
    """把 t.me/proxy 的 secret 字符串解析成 16 字节 proxy_secret + 形态 + 域名。

    secret 原始形态可能是：
      · 全 hex 且长度为偶数  → 按 hex 解码（ee+域名、dd 等都走 hex）
      · 否则                  → 按 base64url 解码（如 3XnnAQIAAQAH8AMDhuJMOt0）
    解码后的字节判断前缀：
      · 首字节 0xDD + 16B            → dd 形态（PaddedIntermediate）
      · 首字节 0xEE + 16B [+ 域名]   → ee 形态（FakeTLS，域名即 SNI）
      · 恰好 16B                     → 裸 abridged 形态
    其余按失败处理（标记 error）。
    """
    text = str(pms_secret or "").strip()
    result = PmsTgProxySecret(secret_original=text)
    if not text:
        result.error = "secret 为空"
        return result
    # 1) 解码成原始字节
    try:
        raw = bytes.fromhex(text) if (len(text) % 2 == 0 and _pms_tg_is_hex(text)) else _pms_tg_b64url_decode(text)
    except Exception:
        result.error = "secret 既不是合法 hex 也不是合法 base64"
        return result
    if not raw:
        result.error = "secret 解码为空"
        return result
    # 2) 判断前缀
    if raw[0] == 0xDD and len(raw) == 17:
        result.form = "dd"
        result.raw = raw[1:17]
        return result
    if raw[0] == 0xEE and len(raw) >= 17:
        result.form = "ee"
        result.raw = raw[1:17]
        # 域名可能是 ASCII（FakeTLS 把域名拼在 secret 尾部）
        tail = raw[17:]
        try:
            domain = tail.decode("ascii")
            if domain and all(c.isprintable() and c not in "\x00" for c in domain):
                result.domain = domain
        except Exception:
            result.domain = ""
        return result
    if len(raw) == 16:
        result.form = "raw"
        result.raw = raw
        return result
    result.error = f"secret 长度/前缀不认识（{len(raw)} 字节，首字节 0x{raw[0]:02x}）"
    return result


def _pms_tg_is_hex(pms_text: str) -> bool:
    try:
        int(pms_text, 16)
        return True
    except ValueError:
        return False


def _pms_tg_b64url_decode(pms_text: str) -> bytes:
    """宽松 base64url 解码（补 padding，接受 -_ 与 +/）。"""
    text = pms_text.strip().replace("-", "+").replace("_", "/")
    pad = (-len(text)) % 4
    if pad:
        text += "=" * pad
    import base64
    return base64.b64decode(text)


def _pms_tg_make_ctr(pms_key: bytes, pms_iv: bytes) -> Cipher:
    """AES-CTR（与 Telethon AESModeCTR 一致：iv 为 16 字节计数器初值）。"""
    return Cipher(algorithms.AES(pms_key), modes.CTR(pms_iv))


def pms_tg_build_obfuscated_header(pms_raw_secret: bytes, pms_dc_id: int, pms_tag: bytes) -> tuple[bytes, "Cipher", "Cipher"]:
    """构造 64 字节混淆头（明文态）+ 收发两把 AES-CTR（严格按 Telethon init_header）。

    返回 (首包密文 64 字节, encryptor, decryptor)：
      · 首包 = encryptor.encrypt(混淆头明文)，发给代理；
      · 后续出站用 encryptor.encrypt(...)；
      · 入站用 decryptor.decrypt(...) 还原成 MTProto 明文帧。
    """
    assert len(pms_raw_secret) == 16, "proxy_secret 必须是 16 字节"
    assert 1 <= pms_dc_id <= 5, "dc_id 应在 1-5"
    while True:
        random = bytearray(os.urandom(64))
        if (random[0] != _PMS_MTPROTO_TRANSPORT_ABRIDGED
                and bytes(random[:4]) not in _PMS_TG_RESERVED_PREFIXES
                and bytes(random[4:8]) != b"\x00\x00\x00\x00"):
            break
    random_reversed = bytes(random[55:7:-1])  # 48 字节（索引 55..8）
    encrypt_key = hashlib.sha256(bytes(random[8:40]) + pms_raw_secret).digest()
    encrypt_iv = bytes(random[40:56])
    decrypt_key = hashlib.sha256(bytes(random_reversed[:32]) + pms_raw_secret).digest()
    decrypt_iv = bytes(random_reversed[32:48])
    encryptor = _pms_tg_make_ctr(encrypt_key, encrypt_iv).encryptor()
    decryptor = _pms_tg_make_ctr(decrypt_key, decrypt_iv).decryptor()
    # tag：abridged 只看首字节 0xEF；intermediate 看 4 字节 0xEEEEEEEE
    if len(pms_tag) == 1:
        random[56] = pms_tag[0]
    else:
        random[56:60] = pms_tag
    dc_id_bytes = pms_dc_id.to_bytes(2, "little", signed=True)
    header = bytes(random[:60]) + dc_id_bytes + bytes(random[62:])
    # ★ 与 Telethon 完全一致：encryptor 必须「处理整段 64 字节」来推进 CTR（虽然只保留
    #   [56:64] 这 8 字节密文），否则代理端的解密 CTR 与客户端加密 CTR 不对齐，帧解不出。
    #   前 56 字节保持明文 random（代理用它们推导密钥），只把 [56:64] 放回密文。
    encrypted = encryptor.update(bytes(header))  # 推进 CTR 64 字节
    first_packet = bytes(header[:56]) + encrypted[56:64]
    return first_packet, encryptor, decryptor


def pms_tg_build_req_pq_frame(pms_encryptor: "Cipher") -> tuple[bytes, bytes]:
    """构造 abridged(req_pq_multi) 帧并返回 (已加密出站字节, 本次 nonce)。"""
    nonce = os.urandom(16)
    msg_id = (int(time.time() * 1000) << _PMS_MTPROTO_MSG_ID_SHIFT) | (os.urandom(1)[0] & 0b111)
    msg_id &= ~0b11
    body = struct.pack("<QI", msg_id, _PMS_MTPROTO_REQ_PQ_MULTI) + nonce
    words = len(body) // 4
    if words < 128:
        frame = bytes([words]) + body
    else:
        frame = bytes([0]) + struct.pack("<I", words)[:3] + body
    return pms_encryptor.update(frame), nonce


def _pms_tg_parse_abridged_frame(pms_buf: bytes) -> tuple[Optional[bytes], int]:
    """从明文缓冲里尝试解析一个 abridged 帧。

    返回 (payload 或 None, 已消费的字节数)。payload 为 None 表示缓冲还不完整（需更多数据）。
    payload 是去掉长度前缀后的 MTProto 消息体（msg_id + TL + ...）。
    """
    if len(pms_buf) < 1:
        return None, 0
    first = pms_buf[0]
    if first == 0:
        if len(pms_buf) < 4:
            return None, 0
        words = int.from_bytes(pms_buf[1:4], "little")
        header_len = 4
    else:
        words = first
        header_len = 1
    if words <= 0 or words > 1024 * 1024:
        raise ValueError(f"MTProto 帧长异常（{words}）")
    total = header_len + words * 4
    if len(pms_buf) < total:
        return None, 0
    return pms_buf[header_len:total], total


def pms_tg_probe_proxy(
    pms_secret: str,
    pms_host: str,
    pms_port: int,
    pms_timeout: float = 8.0,
    pms_dc_id: int = 2,
    pms_logger: Optional[logging.Logger] = None,
) -> dict:
    """探测一个 MTProxy 电报代理能否连通电报。

    返回 dict：{ok, tcp_ms, detail, form, domain}。
      · ok=True  → 完成 MTProto 握手、拿到 resPQ（代理在线且能送流量进电报 DC）
      · ok=False → 失败原因在 detail（代理不可达 / 握手失败 / 超时无 resPQ 等）
    """
    log = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".tgproxy")
    report: dict = {"ok": False, "tcp_ms": 0, "detail": "", "form": "", "domain": ""}
    secret = pms_tg_parse_secret(pms_secret)
    report["form"] = secret.form
    report["domain"] = secret.domain
    if not secret.ok:
        report["detail"] = f"secret 解析失败：{secret.error}"
        return report

    tag = _PMS_TG_TAG_INTERMEDIATE if secret.form == "dd" else _PMS_TG_TAG_ABRIDGED
    try:
        first_packet, encryptor, decryptor = pms_tg_build_obfuscated_header(secret.raw, pms_dc_id, tag)
    except Exception as exc:  # noqa: BLE001
        report["detail"] = f"构造混淆头失败：{exc}"
        return report

    started = time.perf_counter()
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(pms_timeout)
        sock.connect((pms_host, int(pms_port)))
        report["tcp_ms"] = max(1, int((time.perf_counter() - started) * 1000))
        # 1) 发首包（混淆头密文）
        sock.sendall(first_packet)
        # 2) 发 abridged(req_pq_multi)
        frame, nonce = pms_tg_build_req_pq_frame(encryptor)
        sock.sendall(frame)
        # 3) 收回应，解密后解析 abridged 帧，命中 resPQ 且 nonce 回显
        raw_buf = bytearray()
        plain_buf = bytearray()
        deadline = time.perf_counter() + pms_timeout
        while time.perf_counter() < deadline:
            try:
                sock.settimeout(max(0.2, deadline - time.perf_counter()))
                chunk = sock.recv(4096)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            raw_buf.extend(chunk)
            plain_buf.extend(decryptor.update(bytes(chunk)))
            # 尝试从明文缓冲解析多帧（可能先来 new_session_created，再来 resPQ）
            consumed = 0
            while True:
                try:
                    payload, used = _pms_tg_parse_abridged_frame(bytes(plain_buf[consumed:]))
                except ValueError as ve:
                    report["detail"] = f"MTProto 帧解析异常：{ve}"
                    return report
                if payload is None:
                    break
                consumed += used
                if len(payload) < 12:
                    continue
                constructor = struct.unpack("<I", payload[8:12])[0]
                if constructor == _PMS_MTPROTO_RES_PQ:
                    if payload[12:28] == nonce:
                        report["ok"] = True
                        report["detail"] = "完成 MTProto 握手，已拿到 resPQ（代理在线且能连通电报 DC）"
                        return report
                    continue  # 不是本会话应答，继续读下一帧
                if constructor == _PMS_MTPROTO_NEW_SESSION_CREATED:
                    continue
                report["detail"] = f"代理返回了未知构造器 {constructor:#010x}（可能不是 MTProxy）"
                return report
            del plain_buf[:consumed]
        if not raw_buf:
            report["detail"] = "代理无响应（握手后未回任何数据，可能拒绝该 secret）"
        else:
            report["detail"] = "握手超时：未在本会话拿到 resPQ"
    except OSError as exc:
        report["detail"] = f"代理不可达（{exc}）"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return report


# 给单测 / 对话框用的便捷函数：从完整 t.me/proxy 链接里抽取 server/port/secret 再探测
def pms_tg_probe_from_link(pms_link: str, pms_timeout: float = 8.0,
                           pms_dc_id: int = 2,
                           pms_logger: Optional[logging.Logger] = None) -> dict:
    """从 `https://t.me/proxy?server=..&port=..&secret=..` 抽取字段后探测。

    返回在原 report 基础上增加 server/port/secret 的 dict。
    pms_dc_id 指定要连的电报 DC（1-5，默认 2）。
    """
    import urllib.parse
    parts = urllib.parse.urlsplit(str(pms_link or "").strip())
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    server = (query.get("server") or [""])[0].strip()
    port_text = (query.get("port") or [""])[0].strip()
    secret = (query.get("secret") or [""])[0].strip()
    report = {"server": server, "port": int(port_text) if port_text.isdigit() else 0, "secret": secret}
    if not server or not port_text.isdigit() or not secret:
        report.update({"ok": False, "tcp_ms": 0, "detail": "链接缺少 server / port / secret", "form": "", "domain": ""})
        return report
    probe = pms_tg_probe_proxy(secret, server, int(port_text), pms_timeout, pms_dc_id, pms_logger)
    report.update(probe)
    return report
