#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用工具函数

无状态的 pms_* 工具：时间/字节格式化、原子写盘、宽松 base64、域名归一化、端口探测、文本读取等。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Iterable,
    Optional,
    Sequence,
)
import base64
import binascii
import json
from functools import lru_cache
import os
import re
import socket
import tempfile
import threading

from pms.constants import (
    PMS_DEFAULT_SPEED_TEST_URL,
    PMS_SPEED_SOURCE_CHECK_SIZES,
    PMS_SPEED_SOURCE_CUSTOM,
    PMS_SPEED_SOURCE_TEMPLATES,
)

# --------------------------------------------------------------------------------------
# 通用工具函数（无状态，pms_ 前缀）
# --------------------------------------------------------------------------------------
def pms_now_text(pms_fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """本地当前时间字符串。"""
    return datetime.now().strftime(pms_fmt)

def pms_now_millis_text() -> str:
    """带毫秒的本地时间字符串（延迟/测速记录用）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

@lru_cache(maxsize=512)
def pms_format_bytes(pms_bytes: float) -> str:
    """把字节数格式化成人类可读文本（与 NekoBox 一致的 KiB/MiB 风格）。

    带 LRU 缓存：同一刷新周期内相同字节数复用结果，避免流量面板每秒几十次
    格式化时的重复除法与字符串构造（A 档第 3 项性能优化）。连续变化的流量值
    命中率有限，但缓存无副作用，且对同帧内重复引用同一数值有效。
    """
    try:
        value = float(pms_bytes)
    except (TypeError, ValueError):
        return "-"
    if value < 0:
        return "-"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    index = 0
    while value >= 1024.0 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.2f} {units[index]}"

def pms_format_speed(pms_bytes_per_second: float) -> str:
    """把字节/秒格式化成人类可读速率。"""
    if pms_bytes_per_second is None or pms_bytes_per_second < 0:
        return "-"
    return pms_format_bytes(pms_bytes_per_second) + "/s"

def pms_format_latency(pms_millis: Optional[int]) -> str:
    """延迟文本：无结果 → '-'；超时 → '超时'；其余为毫秒。"""
    if pms_millis is None:
        return "-"
    if pms_millis < 0:
        return "超时"
    if pms_millis == 0:
        return "<1 ms"
    return f"{pms_millis} ms"

def pms_fail_short_text(pms_note: str) -> str:
    """失败原因的**短文本**（延迟列那一格用）。

    v1.9.21：默认口径是「超时」，用户在设置里可以改成「显示核心的原始结果」。
    这一格很窄，所以先挑最有信息量的一句：**核心给的 HTTP 状态码**（503 = 节点自己报错、
    504 = 超时），其次给一类原因（超时 / 连接失败 / 核心错误…）；完整原话在提示气泡与测试历史里。
    """
    text = str(pms_note or "").strip()
    if not text:
        return "失败"
    match = re.search(r"HTTP\s*(\d{3})", text)
    if match:
        return f"HTTP {match.group(1)}"
    lowered = text.lower()
    if "超时" in text or "timeout" in lowered or "timed out" in lowered:
        return "超时"
    if "连接失败" in text:
        return "连接失败"
    if "请求异常" in text:
        return "请求异常"
    if "核心" in text:
        return "核心错误"
    return text[:12]

def pms_safe_int(pms_value: Any, pms_default: int = 0, pms_minimum: Optional[int] = None, pms_maximum: Optional[int] = None) -> int:
    """尽量把任意值转成整数，越界时夹紧到区间内（绝不抛异常）。"""
    try:
        result = int(float(str(pms_value).strip())) if isinstance(pms_value, str) else int(pms_value)
    except (TypeError, ValueError, OverflowError):
        result = pms_default
    if pms_minimum is not None and result < pms_minimum:
        result = pms_minimum
    if pms_maximum is not None and result > pms_maximum:
        result = pms_maximum
    return result

def pms_safe_float(pms_value: Any, pms_default: float = 0.0) -> float:
    """尽量把任意值转成浮点数。"""
    try:
        return float(str(pms_value).strip()) if isinstance(pms_value, str) else float(pms_value)
    except (TypeError, ValueError, OverflowError):
        return pms_default

def pms_safe_bool(pms_value: Any, pms_default: bool = False) -> bool:
    """把 'true'/'1'/'on'/'yes' 等字符串也识别成布尔值（.env 与设置文件都要用）。"""
    if isinstance(pms_value, bool):
        return pms_value
    if pms_value is None:
        return pms_default
    if isinstance(pms_value, (int, float)):
        return bool(pms_value)
    text = str(pms_value).strip().lower()
    if text in ("1", "true", "yes", "on", "y", "t", "开启", "是"):
        return True
    if text in ("0", "false", "no", "off", "n", "f", "关闭", "否", ""):
        return False
    return pms_default

def pms_atomic_write_text(pms_path: Path, pms_text: str, pms_encoding: str = "utf-8") -> bool:
    """原子写文本：先写同目录临时文件再替换，避免断电/崩溃留下半个文件。"""
    try:
        pms_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=pms_path.name + ".", suffix=".tmp", dir=str(pms_path.parent))
        try:
            with os.fdopen(fd, "w", encoding=pms_encoding, newline="\n") as handle:
                handle.write(pms_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, pms_path)
            return True
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return False

def pms_atomic_write_bytes(pms_path: Path, pms_payload: bytes) -> bool:
    """原子写二进制（导出压缩包 / 备份用）。"""
    try:
        pms_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=pms_path.name + ".", suffix=".tmp", dir=str(pms_path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(pms_payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, pms_path)
            return True
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return False

def pms_read_text_smart(pms_path: Path, pms_limit: Optional[int] = None) -> str:
    """按 UTF-8 → UTF-8-BOM → GB18030 → Latin-1 的顺序尝试读取，中文节点名不会乱码。"""
    try:
        payload = pms_path.read_bytes()
    except OSError:
        return ""
    if pms_limit is not None and pms_limit > 0:
        payload = payload[:pms_limit]
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")

def pms_mask_secret(pms_text: str, pms_keep: int = 4) -> str:
    """脱敏显示：只保留尾部若干字符。"""
    if not pms_text:
        return ""
    text = str(pms_text)
    if len(text) <= pms_keep:
        return "*" * len(text)
    return "*" * (len(text) - pms_keep) + text[-pms_keep:]

def pms_is_port_available(pms_port: int, pms_host: str = "127.0.0.1") -> bool:
    """检测端口是否可用（能绑定即视为可用）。"""
    if not 0 < int(pms_port) < 65536:
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if pms_host not in ("127.0.0.1", "0.0.0.0", "localhost"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((pms_host, int(pms_port)))
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except OSError:
            pass

# v1.6.7：端口分配改为「进程内加锁 + 游标 + 近期已发放名单」。
# 原先多线程同时调用时都会拿到同一个「当前空闲」端口，随后多个临时核心抢绑同一端口，
# 报 bind: Only one usage of each socket address，测速 / 测延迟直接失败。
_PMS_PORT_LOCK = threading.Lock()
_PMS_PORT_CURSOR: dict[str, int] = {}
_PMS_PORT_RECENT: dict[str, list[int]] = {}
_PMS_PORT_RECENT_MAX = 256

def pms_find_free_port(pms_preferred: int, pms_host: str = "127.0.0.1", pms_span: int = 400) -> int:
    """优先返回首选端口；被占用时在 [preferred, preferred+span] 内找空闲端口，最后交给系统分配。

    并发安全：同一进程内串行分配，且跳过「近期已发放但可能还没被绑定」的端口，
    避免多个临时核心抢同一个端口。
    """
    start = max(1, min(65535, int(pms_preferred)))
    span = max(1, int(pms_span))
    with _PMS_PORT_LOCK:
        recent = _PMS_PORT_RECENT.setdefault(pms_host, [])
        cursor = _PMS_PORT_CURSOR.get(pms_host, start)
        if cursor < start or cursor > start + span:
            cursor = start
        chosen = 0
        for offset in range(span + 1):
            candidate = start + ((cursor - start + offset) % (span + 1))
            if candidate > 65535 or candidate in recent:
                continue
            if pms_is_port_available(candidate, pms_host):
                chosen = candidate
                break
        if not chosen:
            # 区间内全被占（或都在近期名单里）→ 让操作系统直接分配一个。
            chosen = 0
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    probe.bind((pms_host, 0))
                    chosen = int(probe.getsockname()[1])
                finally:
                    probe.close()
            except OSError:
                chosen = 0
            chosen = chosen or start
        _PMS_PORT_CURSOR[pms_host] = chosen + 1
        recent.append(chosen)
        if len(recent) > _PMS_PORT_RECENT_MAX:
            del recent[: len(recent) - _PMS_PORT_RECENT_MAX]
        return chosen

def pms_local_lan_ip() -> str:
    """获取本机在局域网中的 IPv4 地址（供手机 / 平板连接本机代理使用）。

    策略（全程 try/except，绝不抛异常）：
      1. 用 UDP socket 连接公网地址（只挑路由、不真正发包）读取本机出口 IP；
      2. 失败则用主机名解析兜底；
      3. 再失败返回 "127.0.0.1"。

    排序优先级：私有网段(192.168/10/172.16-31) > 其它非回环 > 169.254 链路本地 > 127.x。
    """
    candidates: list[str] = []
    # 1) UDP 探测出口地址（不发送任何数据包）
    for probe_target in (("8.8.8.8", 80), ("223.5.5.5", 80), ("1.1.1.1", 80)):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            sock.connect(probe_target)
            probe_ip = str(sock.getsockname()[0] or "").strip()
            if probe_ip:
                candidates.append(probe_ip)
        except OSError:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if candidates:
            break
    # 2) 主机名解析兜底
    if not candidates:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                fallback_ip = str(info[4][0] or "").strip()
                if fallback_ip:
                    candidates.append(fallback_ip)
        except OSError:
            pass
    # 3) 过滤出合法 IPv4 并按私有网段优先排序
    def _rank(pms_ip: str) -> tuple[int, str]:
        if pms_ip.startswith("192.168.") or pms_ip.startswith("10.") or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", pms_ip):
            return (0, pms_ip)
        if pms_ip.startswith("169.254."):
            return (3, pms_ip)
        if pms_ip.startswith("127."):
            return (4, pms_ip)
        return (1, pms_ip)

    valid = [ip for ip in candidates if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip)]
    if not valid:
        return "127.0.0.1"
    valid.sort(key=_rank)
    return valid[0]


def pms_lan_proxy_address(pms_settings: "PmsSettingsManager", pms_default_port: int = 30808) -> str:
    """返回手机 / 平板应填写的代理地址 ``host:port``（复用 inbound 设置）。

    规则与核心配置保持一致：
      · ``inbound.allow_lan`` 为真，或监听地址为空 / ``0.0.0.0`` / ``*`` 时，
        宿主取本机局域网 IPv4（供同网段设备连接）；
      · 否则取用户填写的监听地址（通常仅本机 ``127.0.0.1``）；
      · 端口取 ``inbound.mixed_port``。

    该地址同时被「手机 / 平板代理助手」对话框与工具栏「复制代理地址」复用，
    避免在多处重复实现同一套计算逻辑。
    """
    allow_lan = pms_settings.psm_get_bool("inbound.allow_lan", False)
    listen = str(pms_settings.psm_get_str("inbound.listen_address", "127.0.0.1") or "").strip()
    if allow_lan or listen in ("", "0.0.0.0", "*"):
        host = pms_local_lan_ip()
    else:
        host = listen
    port = pms_settings.psm_get_int("inbound.mixed_port", pms_default_port)
    return f"{host}:{int(port)}"


def pms_normalize_domain_entry(pms_raw: str) -> str:
    """把用户输入的一行域名规则整理成核心可用的后缀形式（去掉通配与协议头）。"""
    text = str(pms_raw or "").strip().lower()
    if not text:
        return ""
    for prefix in ("http://", "https://", "socks5://", "socks://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.split("/")[0].split(":")[0]
    if text.startswith("*."):
        text = text[2:]
    if text.startswith("."):
        text = text[1:]
    if text in ("localhost",) or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", text) or ":" in text:
        return text
    if not re.fullmatch(r"[a-z0-9]([a-z0-9\-_.]*[a-z0-9])?", text):
        return ""
    return text

def pms_split_multi_line(pms_text: str) -> list[str]:
    """把多行文本切成去空、去注释的行列表。"""
    result: list[str] = []
    for line in str(pms_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        result.append(item)
    return result

def pms_dedupe_keep_order(pms_items: Iterable[str]) -> list[str]:
    """去重且保留原顺序（订阅更新去重、规则列表合并都用它）。"""
    seen: set[str] = set()
    output: list[str] = []
    for item in pms_items:
        key = item
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output

def pms_json_pretty(pms_payload: Any) -> str:
    """统一风格的 JSON 文本（配置文件写盘用）。"""
    return json.dumps(pms_payload, ensure_ascii=False, indent=2)

def pms_b64_decode_loose(pms_text: str) -> str:
    """宽松 base64 解码：自动补 padding，支持 urlsafe 与去换行，失败返回空串。"""
    raw = re.sub(r"\s+", "", str(pms_text or ""))
    if not raw:
        return ""
    raw = raw.replace("-", "+").replace("_", "/")
    padding = (-len(raw)) % 4
    if padding:
        raw += "=" * padding
    try:
        decoded_bytes = base64.b64decode(raw)
        del raw  # 释放原始大字符串，避免（输入串 + 字节 + 字符串）三者同时驻留导致峰值翻倍
        return decoded_bytes.decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""

def pms_b64_encode_text(pms_text: str) -> str:
    """标准 base64 编码（去掉换行）。"""
    return base64.b64encode(str(pms_text).encode("utf-8")).decode("ascii")

def pms_looks_like_base64(pms_text: str) -> bool:
    """判断一段文本是否整体像 base64 订阅内容。"""
    raw = re.sub(r"\s+", "", str(pms_text or ""))
    if len(raw) < 16:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=_\-]+", raw):
        return False
    decoded = pms_b64_decode_loose(raw)
    if not decoded:
        return False
    markers = ("://", "proxies", "outbounds", "{", "[", "server")
    return any(marker in decoded for marker in markers)


def pms_normalize_cert_sha256(pms_raw: str) -> str:
    """规范化 Xray ``pinnedPeerCertSha256``（SHA-256 证书指纹）。

    接受大小写十六进制，可带空格 / 冒号 / 连字符分隔（证书指纹常见展示形式），
    统一去除分隔符并转小写。必须是刚好 64 位十六进制，否则返回空串（表示无效 / 留空）。
    这样 Xray 永远只拿到合法指纹，避免核心启动失败且报错难懂。
    """
    if not pms_raw:
        return ""
    cleaned = re.sub(r"[\s:_-]", "", str(pms_raw))
    cleaned = cleaned.lower()
    if len(cleaned) != 64 or not re.fullmatch(r"[0-9a-f]{64}", cleaned):
        return ""
    return cleaned

def pms_human_duration(pms_seconds: float) -> str:
    """秒 → 人类可读时长。"""
    try:
        total = int(max(0, float(pms_seconds)))
    except (TypeError, ValueError):
        return "-"
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        return f"{total // 60} 分 {total % 60} 秒"
    if total < 86400:
        return f"{total // 3600} 小时 {(total % 3600) // 60} 分"
    return f"{total // 86400} 天 {(total % 86400) // 3600} 小时"

def pms_days_until(pms_timestamp: Optional[int]) -> Optional[int]:
    """距离某 Unix 时间戳（秒）还有多少天；已过期返回负数，未知返回 None。"""
    if not pms_timestamp:
        return None
    try:
        delta = datetime.fromtimestamp(int(pms_timestamp)) - datetime.now()
    except (ValueError, OSError, OverflowError):
        return None
    return int(delta.total_seconds() // 86400)

def pms_slugify_filename(pms_text: str, pms_fallback: str = "export") -> str:
    """把任意文本转成安全的文件名片段（节点名导出用）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(pms_text or "")).strip(" ._")
    return cleaned[:80] or pms_fallback

def pms_multiline_to_list(pms_text: str) -> list[str]:
    """多行文本 → 去空去重列表（设置项多行输入统一走这里）。"""
    return pms_dedupe_keep_order([line.strip() for line in str(pms_text or "").replace("\r\n", "\n").split("\n") if line.strip()])

# --------------------------------------------------------------------------------------
# 测速源解析（v1.4.3：预设源 + 可选 MB + 自检记录）
# --------------------------------------------------------------------------------------

def pms_resolve_speed_url(
    pms_source: str,
    pms_size_mb: int,
    pms_custom_url: str = "",
    pms_fallback: str = "",
) -> str:
    """按「测速源 + 大小(MB)」拼出测速地址。

    - custom（或源不在预设表内）→ 直接用自定义地址，为空才回退；
    - cloudflare / cachefly → 用预设模板渲染，{bytes} 与 {mb} 两个占位符都提供，模板各取所需。

    任何异常都回退到 pms_fallback / 内置默认地址，绝不向外抛异常。
    """
    source = str(pms_source or "").strip().lower()
    custom = str(pms_custom_url or "").strip()
    fallback = str(pms_fallback or "").strip() or PMS_DEFAULT_SPEED_TEST_URL
    if source == PMS_SPEED_SOURCE_CUSTOM or source not in PMS_SPEED_SOURCE_TEMPLATES:
        return custom or fallback
    size_mb = pms_safe_int(pms_size_mb, 10, 1, 1024)
    try:
        rendered = PMS_SPEED_SOURCE_TEMPLATES[source].format(bytes=int(size_mb) * 1024 * 1024, mb=int(size_mb)).strip()
    except (AttributeError, IndexError, KeyError, ValueError):
        return fallback
    return rendered or fallback

def pms_speed_candidate_sizes(pms_source: str) -> list[int]:
    """某测速源的自检候选档位（MB）；自定义源没有档位概念，返回空。"""
    if str(pms_source or "").strip().lower() == PMS_SPEED_SOURCE_CUSTOM:
        return []
    return [int(size) for size in PMS_SPEED_SOURCE_CHECK_SIZES]

def pms_speed_ok_key(pms_source: str, pms_size_mb: int) -> str:
    """自检记录键，形如 "cachefly:10"。"""
    return f"{str(pms_source or '').strip().lower()}:{pms_safe_int(pms_size_mb, 0, 0, 1048576)}"

def pms_speed_ok_sizes(pms_ok_list: Optional[Sequence[str]], pms_source: str) -> list[int]:
    """从自检记录里挑出某源已通过的档位（MB），升序去重。"""
    prefix = f"{str(pms_source or '').strip().lower()}:"
    sizes: list[int] = []
    for item in pms_ok_list or []:
        text = str(item or "").strip()
        if not text.startswith(prefix):
            continue
        value = pms_safe_int(text[len(prefix):], 0, 0, 1048576)
        if value > 0:
            sizes.append(int(value))
    return sorted(set(sizes))
