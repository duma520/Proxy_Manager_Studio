#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tor 桥接自动获取（v1.9.47）：从社区采集站抓 `*_tested.txt` → 本地 TCP 握手验活 → 交给 tor 配置

**与 `pms/tor_bridge.py` 的分工（两个文件名很像，别 import 错）**：

    pms/tor_bridge.py  —— 桥接的「形状」：解析、包装容错、合并去重、torrc / sing-box 出站生成
    pms/torbridge.py   —— 桥接的「来源」：从采集站抓、验活、缓存、失败回退（本文件）

**实查（2026-10-12，经本机代理实测两个仓库的 `bridge/` 目录与样本文件，不要凭印象改）**：

    https://api.github.com/repos/center2055/OnionHop-Bridges-Collector/contents/bridge
        obfs4_tested.txt / vanilla_tested.txt / webtunnel_tested.txt / snowflake_tested.txt /
        meek-azure_tested.txt（**0 字节**，要当「没有内容」跳过，不算失败）/ conjure_tested.txt …
    https://api.github.com/repos/Delta-Kronecker/Tor-Bridges-Collector/contents/bridge
        obfs4_tested.txt / obfs4_ipv6_tested.txt / vanilla_tested.txt / vanilla_ipv6_tested.txt /
        webtunnel_tested.txt / webtunnel_ipv6_tested.txt（**没有 snowflake_tested.txt**）

    样本行（原样抄下来的，探针里当契约）：
        obfs4 103.149.168.186:8443 2D16215A… cert=ebVLQlo1wEnp/HYtJ+nSuikwe+dSt632ka33wJAiUeVK0IeYs5M6w5DwoPX8gYLsJfADSA iat-mode=0
        103.149.168.242:9443 91637DE9ED5B069722DA7A5796926EE13238694D          ← vanilla：**没有类型前缀**
        webtunnel [2001:db8:1169:5d59:447d:1feb:3595:b174]:443 9E22636EA817AD4FE43F2EC14DEED5131FED1CB0 url=https://… ver=0.0.2
        snowflake 192.0.2.3:80 2B280B23… fingerprint=2B280B23… url=https://… fronts=… ice=… utls-imitate=hellorandomizedalpn

**验活口径**（本地 TCP 握手，**发不出包就等于不可用**）：

    obfs4 / webtunnel / vanilla → 连 `address` 的 IP:PORT
    snowflake / meek           → 连 broker 域名（`url=` 的 host）的 **443**（桥接本身是 P2P / 前置域名，没有固定端口可连）

**失败不许静默**：两个源都抓不到时先回退**本地缓存**（上次成功抓取的结果，存 `data/tor/bridges_cache.txt`），
缓存也没有就返回明确错误并说明「可以手工粘贴桥接」。**我们不会自己编桥接行**——编出来的连不上，
只会让用户以为程序坏了。
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from pms.constants import PMS_DEFAULT_SUB_USER_AGENT, PMS_LOGGER_NAME
from pms.tor_bridge import (
    PMS_TOR_BRIDGE_TYPES,
    ptb_bridge_to_torrc,
    ptb_extract_bridges_from_text,
    ptb_merge_bridge_lines,
    ptb_parse_bridge_line,
)

# 采集站（v1.9.47 实查确认存在；`repo` + `path` + 只收 `*_tested.txt`）
PMS_TOR_BRIDGE_SOURCES: tuple[dict[str, str], ...] = (
    {
        "name": "OnionHop-Bridges-Collector",
        "repo": "center2055/OnionHop-Bridges-Collector",
        "path": "bridge",
    },
    {
        "name": "Delta-Kronecker/Tor-Bridges-Collector",
        "repo": "Delta-Kronecker/Tor-Bridges-Collector",
        "path": "bridge",
    },
)
PMS_TOR_BRIDGES_CACHE_NAME = "bridges_cache.txt"
# v1.9.48：验活**没通过**的候选另存一份（用户明确要「保存未验活的桥接」，但必须先验证 —
# 所以验活照跑，只是失败的候选不丢，单独落盘 + 写进 `tor.unverified_lines`，界面标成未验活）
PMS_TOR_UNVERIFIED_NAME = "bridges_unverified.txt"
PMS_TOR_MAX_UNVERIFIED = 800
PMS_TOR_FETCH_TIMEOUT = 25.0
PMS_TOR_TESTED_SUFFIX = "_tested.txt"
PMS_TOR_MAX_FILES_PER_SOURCE = 12      # 一个源最多取多少个 *_tested.txt（防目录里塞了几十个时打爆）
PMS_TOR_MAX_BRIDGES = 800              # 验活上限（几万个桥接逐个握手会把时间拉爆）
PMS_TOR_PROBE_CONCURRENCY = 32         # 并发握手数（本地 TCP，32 够快也不至于把路由器打爆）
PMS_TOR_PROBE_TIMEOUT = 3.0
# 逐条「握手失败」说明最多列几条（全挂时几百行说明会把界面刷满，反而看不到重点）
PMS_TOR_MAX_FAIL_NOTES = 12
PMS_TOR_BROKER_PORT = 443
# 「连 broker 443」的桥接类型（本身没有可直连的固定端口）
PMS_TOR_P2P_TYPES: tuple[str, ...] = ("snowflake", "meek", "meek_lite")


# --------------------------------------------------------------------------------------
# HTTP：所有请求都必须带 PMS_DEFAULT_SUB_USER_AGENT
# --------------------------------------------------------------------------------------
def ptb_http_get(pms_url: str, pms_openers: Sequence[tuple[str, Any]], pms_timeout: float = PMS_TOR_FETCH_TIMEOUT,
                 pms_max_bytes: int = 4 * 1024 * 1024, pms_logger: Optional[logging.Logger] = None,
                 pms_accept: str = "application/vnd.github+json") -> tuple[bytes, str]:
    """按通路退让 GET 一个 URL；返回 (内容, 错误说明)。**UA 一律用 `PMS_DEFAULT_SUB_USER_AGENT`。**

    为什么这么强调 UA：GitHub 对匿名请求按 UA 分流，缺 UA / 用 python-urllib 默认 UA
    会被当成脚本流量（限流更狠），而采集站与已有下载链路本来就用这一个 UA，口径必须一致。
    """
    request = urllib.request.Request(
        str(pms_url), headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT, "Accept": pms_accept},
    )
    errors: list[str] = []
    for name, opener in (pms_openers or []):
        try:
            with opener.open(request, timeout=float(pms_timeout)) as response:
                body = response.read(int(pms_max_bytes))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"[{name}] {exc}")
            continue
        if not body:
            # HTTP 拿到了 200 但正文是空的：这不是「网络失败」。采集站的 `meek-azure_tested.txt`
            # **实查就是 0 字节**，当成失败会让整个源被误判挂掉（v1.9.47 实查踩到过）。
            return b"", ""
        return body, ""
    return b"", "；".join(errors) if errors else "没有可用网络通路"


def ptb_source_file_urls(pms_source: dict[str, str], pms_openers: Sequence[tuple[str, Any]],
                         pms_logger: Optional[logging.Logger] = None) -> tuple[list[tuple[str, str]], str]:
    """列出一个采集源里所有 `*_tested.txt` 的 [(文件名, 下载地址)]；返回 (列表, 错误说明)。

    走 GitHub contents API（一次请求拿目录），**不猜文件名**——上游加/删文件都能跟上。
    """
    api = f"https://api.github.com/repos/{pms_source['repo']}/contents/{pms_source['path']}"
    body, error = ptb_http_get(api, pms_openers, pms_logger=pms_logger)
    if error:
        return [], f"{pms_source['name']}：{error}"
    try:
        listing = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        return [], f"{pms_source['name']}：目录响应不是 JSON（{exc}）"
    if not isinstance(listing, list):
        return [], f"{pms_source['name']}：目录响应结构异常"
    files: list[tuple[str, str]] = []
    for item in listing:
        if not isinstance(item, dict) or str(item.get("type")) != "file":
            continue
        name = str(item.get("name") or "")
        if not name.endswith(PMS_TOR_TESTED_SUFFIX):
            continue
        url = str(item.get("download_url") or "")
        if not url:
            continue
        files.append((name, url))
    files.sort(key=lambda pair: pair[0])
    if not files:
        return [], f"{pms_source['name']}：目录里没有 {PMS_TOR_TESTED_SUFFIX} 文件（上游结构可能变了）"
    return files[:PMS_TOR_MAX_FILES_PER_SOURCE], ""


def ptb_fetch_source_bridges(pms_source: dict[str, str], pms_openers: Sequence[tuple[str, Any]],
                             pms_logger: Optional[logging.Logger] = None) -> tuple[list[str], list[str]]:
    """抓一个源的全部 `*_tested.txt`，返回 (桥接行, 说明)。空文件算「没内容」，不算失败。"""
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".torbridge")
    files, error = ptb_source_file_urls(pms_source, pms_openers, logger)
    if error:
        return [], [error]
    lines: list[str] = []
    notes: list[str] = []
    for name, url in files:
        body, error = ptb_http_get(url, pms_openers, pms_logger=logger, pms_accept="text/plain")
        if error:
            notes.append(f"{pms_source['name']}/{name}：{error}")
            continue
        bridges, bad = ptb_extract_bridges_from_text(body.decode("utf-8", errors="replace"))
        if not bridges and not bad:
            notes.append(f"{pms_source['name']}/{name}：文件里没有桥接行（多为空文件，跳过）")
            continue
        lines.extend(str(item["raw"]) for item in bridges)
        if bad:
            notes.append(f"{pms_source['name']}/{name}：{len(bad)} 行像桥接但解析不了")
        logger.info("桥接采集：%s/%s 取到 %d 条（%d 行解析不了）", pms_source["name"], name, len(bridges), len(bad))
    return lines, notes


def ptb_fetch_all_bridge_lines(pms_sources: Optional[Sequence[dict[str, str]]] = None,
                               pms_openers: Optional[Sequence[tuple[str, Any]]] = None,
                               pms_logger: Optional[logging.Logger] = None) -> tuple[list[str], list[str]]:
    """抓所有采集源并按指纹去重；返回 (桥接行, 说明)。**两条源都失败时返回空 + 明确原因。**"""
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".torbridge")
    merged: list[str] = []
    notes: list[str] = []
    for source in (pms_sources if pms_sources is not None else PMS_TOR_BRIDGE_SOURCES):
        lines, source_notes = ptb_fetch_source_bridges(dict(source), pms_openers or [], logger)
        notes.extend(source_notes)
        if lines:
            merged, added = ptb_merge_bridge_lines(merged, lines)
            notes.append(f"{source['name']}：取到 {len(lines)} 条（新增 {added}，去重后共 {len(merged)}）")
    return merged, notes


# --------------------------------------------------------------------------------------
# 验活：本地 TCP 握手
# --------------------------------------------------------------------------------------
def ptb_probe_target(pms_bridge: dict[str, Any]) -> tuple[str, int]:
    """这条桥接该握手哪个地址：普通桥接连自己的 IP:PORT；snowflake / meek 连 broker 域名的 443。"""
    kind = str(pms_bridge.get("type") or "").lower()
    if kind in PMS_TOR_P2P_TYPES:
        host = ""
        url = str(pms_bridge.get("url") or "").strip()
        if url:
            host = urllib.parse.urlsplit(url if "://" in url else f"https://{url}").hostname or ""
        if not host:
            host = str(pms_bridge.get("front") or "").split(",")[0].strip()
        return host, PMS_TOR_BROKER_PORT
    address = str(pms_bridge.get("address") or "").strip()
    host, _, port = address.rpartition(":")
    host = host.strip("[]")
    return host, int(port) if port.isdigit() else 0


def ptb_tcp_alive(pms_host: str, pms_port: int, pms_timeout: float = PMS_TOR_PROBE_TIMEOUT) -> bool:
    """一次 TCP 握手；连上就 True（**不验证协议**：桥接握手内容只有 tor 自己认得）。"""
    if not pms_host or not pms_port:
        return False
    try:
        with socket.create_connection((pms_host, int(pms_port)), timeout=float(pms_timeout)):
            return True
    except OSError:
        return False


def ptb_validate_bridges(
    pms_lines: Sequence[str],
    pms_concurrency: int = PMS_TOR_PROBE_CONCURRENCY,
    pms_timeout: float = PMS_TOR_PROBE_TIMEOUT,
    pms_progress: Optional[Callable[[int, int], None]] = None,
    pms_probe: Optional[Callable[[str, int, float], bool]] = None,
) -> tuple[list[str], list[str], list[str]]:
    """逐条 TCP 验活；返回 (通过的行, 未通过的行, 说明)。

    * 解析不了的行直接进「未通过」并说明（不让坏行混进配置）；
    * 并发握手（默认 32），**不阻塞界面**（界面侧本来就在工作线程里调它）；
    * `pms_probe` 可注入（探针用假的握手函数，避免真去连外网）。
    """
    parsed: list[tuple[str, dict[str, Any]]] = []
    failed: list[str] = []
    notes: list[str] = []
    seen: set[str] = set()
    for line in (pms_lines or [])[:PMS_TOR_MAX_BRIDGES]:
        item = ptb_parse_bridge_line(line)
        if item is None:
            failed.append(str(line))
            continue
        key = str(item["raw"])
        if key in seen:
            continue
        seen.add(key)
        parsed.append((str(item["raw"]), item))
    if len(pms_lines or []) > PMS_TOR_MAX_BRIDGES:
        notes.append(f"桥接太多，本轮只验前 {PMS_TOR_MAX_BRIDGES} 条（采集站给的是全量列表）")
    probe = pms_probe or ptb_tcp_alive
    ok_lines: list[str] = []
    total = len(parsed)
    done = 0

    def run(pms_pair: tuple[str, dict[str, Any]]) -> tuple[str, bool, str, int]:
        line, item = pms_pair
        host, port = ptb_probe_target(item)
        if not host:
            return line, False, "取不到可握手的地址（snowflake / meek 需要 url= 或 front=）", 0
        return line, bool(probe(host, port, pms_timeout)), f"{host}:{port}", port

    if not parsed:
        return [], failed, notes
    dead_lines: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(pms_concurrency))) as pool:
        for line, alive, target, _port in pool.map(run, parsed):
            done += 1
            if pms_progress is not None and (done % 8 == 0 or done == total):
                pms_progress(done, total)
            if alive:
                ok_lines.append(line)
            else:
                failed.append(line)
                dead_lines.append((line, target))
    # 逐条报失败原因，但**封顶**：全挂时几百行说明会把界面刷满，人反而看不到重点。
    for line, target in dead_lines[:PMS_TOR_MAX_FAIL_NOTES]:
        notes.append(f"握手失败（{target}）：{line[:60]}")
    if len(dead_lines) > PMS_TOR_MAX_FAIL_NOTES:
        notes.append(f"另有 {len(dead_lines) - PMS_TOR_MAX_FAIL_NOTES} 条握手失败，原因同上（不再逐条列出）")
    if parsed and not ok_lines:
        notes.append("本轮一条都没握上手：要么这些桥接对本机不可达（网络受限 / 桥接已失效），"
                     "要么本机到国外 IP 的 TCP 本身被拦——可以用系统工具试连任意一条的 IP:PORT 复核")
    return ok_lines, failed, notes


# --------------------------------------------------------------------------------------
# 缓存（「内置社区桥接列表」的落点）
# --------------------------------------------------------------------------------------
def ptb_cache_file(pms_paths: Any) -> Path:
    """缓存文件位置：`<数据目录>/tor/bridges_cache.txt`（与 Tor 组件同目录，随实例数据走）。"""
    from pms.tor_bridge import ptb_tor_paths

    return Path(ptb_tor_paths(pms_paths)["tor_dir"]) / PMS_TOR_BRIDGES_CACHE_NAME


def ptb_write_cache(pms_paths: Any, pms_lines: Sequence[str]) -> str:
    """把**验活通过**的桥接写进缓存（下次抓不到时用）。返回错误说明（空串=成功）。"""
    from pms.utils import pms_atomic_write_bytes

    good = [str(line) for line in (pms_lines or []) if str(line).strip()]
    if not good:
        return "没有可缓存的桥接"
    try:
        ok = pms_atomic_write_bytes(ptb_cache_file(pms_paths), ("\n".join(good) + "\n").encode("utf-8"))
    except OSError as exc:
        return f"写缓存失败：{exc}"
    return "" if ok else "写缓存失败"


def ptb_read_cache(pms_paths: Any) -> list[str]:
    """读缓存（没有文件 / 读不动都返回空列表，不算错误）。"""
    try:
        body = ptb_cache_file(pms_paths).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith("#")]


# --------------------------------------------------------------------------------------
# 未验活候选（v1.9.48：用户要「保存未验活的桥接」——**保存前照样真握手验过**）
# --------------------------------------------------------------------------------------
def ptb_unverified_file(pms_paths: Any) -> Path:
    """未验活候选取的落点：`<数据目录>/tor/bridges_unverified.txt`。"""
    from pms.tor_bridge import ptb_tor_paths

    return Path(ptb_tor_paths(pms_paths)["tor_dir"]) / PMS_TOR_UNVERIFIED_NAME


def ptb_write_unverified(pms_paths: Any, pms_lines: Sequence[str]) -> str:
    """把未验活的候选写进独立文件（**不写进 bridges_cache.txt**，免得下次回退时被当成可用桥接）。"""
    from pms.utils import pms_atomic_write_bytes

    rows = [str(line).strip() for line in (pms_lines or []) if str(line).strip()][:PMS_TOR_MAX_UNVERIFIED]
    if not rows:
        return "没有要保存的候选"
    header = ("# 未验活（可能不可用）：这些候选来自社区采集站，本机 TCP 握手**没通过**。\n"
              "# 程序不会把它们拼进配置；要试就把某行单独粘到「设置 → 核心 → Tor 桥接出口」的桥接行里。\n")
    try:
        ok = pms_atomic_write_bytes(ptb_unverified_file(pms_paths), (header + "\n".join(rows) + "\n").encode("utf-8"))
    except OSError as exc:
        return f"写未验活文件失败：{exc}"
    return "" if ok else "写未验活文件失败"


def ptb_read_unverified(pms_paths: Any) -> list[str]:
    """读未验活候选（跳过 `#` 说明行；没有文件返回空列表）。"""
    try:
        body = ptb_unverified_file(pms_paths).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith("#")]


# --------------------------------------------------------------------------------------
# 一站式入口（界面 / 订阅 / 命令都调它）
# --------------------------------------------------------------------------------------
def ptb_collect_valid_bridges(
    pms_paths: Any,
    pms_openers: Sequence[tuple[str, Any]],
    pms_logger: Optional[logging.Logger] = None,
    pms_sources: Optional[Sequence[dict[str, str]]] = None,
    pms_probe: Optional[Callable[[str, int, float], bool]] = None,
    pms_cache: bool = True,
    pms_progress: Optional[Callable[[str, int, int], None]] = None,
    pms_keep_unverified: bool = False,
) -> dict[str, Any]:
    """抓取 → 验活 → 缓存 → 生成 torrc；返回一个**可展示**的结果包（永不抛异常）。

    返回 dict：
        ok          —— 是否拿到至少一条验活通过的桥接
        lines       —— 验活通过的桥接行
        total       —— 抓到的候选条数
        failed      —— 握手失败的条数
        torrc       —— 交给 tor 的 torrc（单桥接形态；多条时 extra_args 承担）
        extra_args  —— 多桥接的额外命令行参数
        source      —— `collector`（采集站）/ `cache`（回退缓存）
        notes       —— 人话说明（含每个源的条数与失败原因，**不静默**）
        unverified  —— v1.9.48：验活没通过的候选（**只有 `pms_keep_unverified=True` 才收**）
        unverified_file —— 未验活候选的落盘位置（没保存时是空串）

    **「保存前必须验证」的口径**：`pms_keep_unverified` 只决定「失败的候选丢不丢」，
    验活本身永远照跑——未验活候选既不会进 `torrc`，也不会写进 `bridges_cache.txt`。
    """
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".torbridge")
    from pms.tor_bridge import ptb_tor_paths

    notes: list[str] = []
    lines, fetch_notes = ptb_fetch_all_bridge_lines(pms_sources, pms_openers, logger)
    notes.extend(fetch_notes)
    source = "collector"
    if not lines:
        notes.append("所有采集源都没抓到桥接")
        if pms_cache:
            cached = ptb_read_cache(pms_paths)
            if cached:
                lines = cached
                source = "cache"
                notes.append(f"回退到本地缓存（上次成功抓取的 {len(cached)} 条）")
        if not lines:
            notes.append("缓存也是空的：可以手工把桥接行粘进「设置 → 核心 → Tor 桥接出口」（程序不会自己编桥接）")
            return {"ok": False, "lines": [], "total": 0, "failed": 0, "torrc": {}, "extra_args": [],
                    "source": "", "notes": notes, "unverified": [], "unverified_file": ""}
    if pms_progress is not None:
        pms_progress("正在逐条 TCP 验活…", 0, len(lines))
    valid, failed, probe_notes = ptb_validate_bridges(
        lines, pms_probe=pms_probe,
        pms_progress=(lambda done, total: pms_progress("验活中", done, total)) if pms_progress is not None else None,
    )
    notes.extend(probe_notes[:20])
    if len(probe_notes) > 20:
        notes.append(f"（另有 {len(probe_notes) - 20} 条握手失败未逐条列出）")
    if source == "collector" and valid and pms_cache:
        if ptb_write_cache(pms_paths, valid):
            notes.append("缓存写入失败（不影响本次使用）")
        else:
            notes.append(f"已把 {len(valid)} 条验活通过的桥接写入缓存")
    unverified: list[str] = []
    unverified_file = ""
    if pms_keep_unverified:
        passed = set(valid)
        unverified = [line for line in lines
                      if line not in passed and ptb_parse_bridge_line(line) is not None]
        if unverified:
            unverified_file = str(ptb_unverified_file(pms_paths))
            error = ptb_write_unverified(pms_paths, unverified)
            if error:
                notes.append(f"{error}（未验活候选这次没保存成功）")
                unverified_file = ""
            else:
                notes.append(f"另存 {len(unverified)} 条未验活候选到 {unverified_file}"
                             "（标成「未验活、可能不可用」，不会进配置）")
                if len(unverified) > PMS_TOR_MAX_UNVERIFIED:
                    notes.append(f"未验活候选超过上限，只存了前 {PMS_TOR_MAX_UNVERIFIED} 条")
    tor_dir = Path(ptb_tor_paths(pms_paths)["tor_dir"])
    torrc, extra_args, errors = ptb_bridge_to_torrc(valid, tor_dir)
    if errors:
        notes.append(f"{len(errors)} 行没能进 torrc（解析不了）")
    logger.info("桥接采集完成：候选 %d 条，验活通过 %d 条，失败 %d 条（来源 %s）",
                len(lines), len(valid), len(failed), source or "无")
    return {
        "ok": bool(valid),
        "lines": valid,
        "total": len(lines),
        "failed": len(failed),
        "torrc": torrc,
        "extra_args": extra_args,
        "source": source,
        "notes": notes,
        "unverified": unverified,
        "unverified_file": unverified_file,
    }
