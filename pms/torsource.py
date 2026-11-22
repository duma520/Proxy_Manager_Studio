#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tor 组件多源兜底下载（v1.9.58）

**为什么单独一个模块**：`pms/tordownload.py` 原先只有「官方目录 → 最新版 Expert Bundle」
一条路。v1.9.58 实测发现三件事，逼着它必须能退让：

1. `dist.torproject.org/torbrowser/` **只保留 2 个版本**（2026-10-14 实测：15.0.20 / 15.0.25）。
   一旦新版目录被清或下载失败，原先那条 URL 拼出来就是 404（实测 15.0.24 直链确实 404）。
2. `archive.torproject.org/tor-package-archive/torbrowser/` 有 **243 个历史版本**
   （实测 15.0.24 的 Expert Bundle 就在里面，HEAD 200，21 MB）——这是**同包同格式**的退让源。
3. Tor 浏览器便携版 `tor-browser-windows-x86_64-portable-<版本>.exe` 里也有
   `tor.exe` / `lyrebird.exe` / `conjure-client.exe`（实测 15.0.25 的 108 MB 包，
   解出 182 个文件，SHA-256 与 Expert Bundle **逐字节一致**）。代价是 5 倍流量，
   所以它是**最后手段**。

**顺序**（用户 v1.9.58 确认：全都要，流量不是问题）：
    ① 官方最新版 Expert Bundle（21 MB，最快）
    ② 归档站 Expert Bundle（21 MB，按版本号精确取）
    ③ Tor 浏览器便携版提取（108 MB，需 7-Zip）
    ④ 本地已装的 Tor 浏览器目录复制（0 流量，用户本机装过浏览器时）
    ⑤ 手动放置（提示用户自己把文件放进 data/tor）

**7-Zip 依赖**：便携版是 7z SFX（`SubType = NSIS-3 Unicode 64-bit`，实测），
Python 标准库解不了，只能调 `7z.exe`。**不要求用户装 7-Zip**——找不到就把 ③ 标成
「本机没有 7-Zip，跳过」并继续退让，绝不报错中断。

**安全边界**：第三方 GitHub 镜像**不在这里自动跑**。实测 `matinrco/tor` 的 release 是
私有构建的 `tor-real.exe` + 已停止维护的 OpenSSL 1.1 DLL，来源与版本都不可控。
它只能由用户手动触发（`torwidget` 里独立的按钮 + 明确风险确认），不进入静默自动链。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from pms.constants import PMS_DEFAULT_SUB_USER_AGENT, PMS_LOGGER_NAME
from pms.tor_bridge import (
    PMS_TOR_CONJURE_NAME,
    PMS_TOR_EXE_NAME,
    PMS_TOR_LYREBIRD_NAME,
    PMS_TOR_SNOWFLAKE_NAME,
)

# 官方源（版本目录列表 + Expert Bundle）
PMS_TOR_DIST_INDEX = "https://dist.torproject.org/torbrowser/"
PMS_TOR_DIST_BUNDLE = "tor-expert-bundle-windows-x86_64-{version}.tar.gz"
# 归档源（243 个历史版本，实测可用）
PMS_TOR_ARCHIVE_INDEX = "https://archive.torproject.org/tor-package-archive/torbrowser/"
PMS_TOR_ARCHIVE_BUNDLE = PMS_TOR_DIST_BUNDLE
# Tor 浏览器便携版（最后手段，108 MB）
PMS_TOR_BROWSER_EXE = "tor-browser-windows-x86_64-portable-{version}.exe"
# 便携版里三个文件在 7z 条目里的相对路径（2026-10-14 实测 15.0.25，182 个文件）
PMS_TOR_BROWSER_PATHS: dict[str, str] = {
    PMS_TOR_EXE_NAME: "Browser\\TorBrowser\\Tor\\tor.exe",
    PMS_TOR_LYREBIRD_NAME: "Browser\\TorBrowser\\Tor\\PluggableTransports\\lyrebird.exe",
    PMS_TOR_CONJURE_NAME: "Browser\\TorBrowser\\Tor\\PluggableTransports\\conjure-client.exe",
    PMS_TOR_SNOWFLAKE_NAME: "Browser\\TorBrowser\\Tor\\PluggableTransports\\snowflake-client.exe",
}
# 便携版 zip 变体（有的版本只发 zip，不发 exe；实测 exe 是 7z SFX）
PMS_TOR_BROWSER_ZIP = "tor-browser-windows-x86_64-portable-{version}.zip"

PMS_TOR_HTTP_TIMEOUT = 30.0
PMS_TOR_BROWSER_HEAD_TIMEOUT = 25.0
PMS_TOR_SEVENZIP_NAMES = ("7z.exe", "7za.exe")
PMS_TOR_SEVENZIP_DIRS = (
    r"C:\Program Files\7-Zip",
    r"C:\Program Files (x86)\7-Zip",
)
PMS_TOR_MAX_ARCHIVE_VERSIONS = 3   # 归档站版本很多（实测 243 个），只从最新往回试这么多个
                                    # ★ 这是**性能**约束不是正确性约束：每个版本都要先 HEAD 探存在性、
                                    #   再下载 21 MB，试 8 个会让「全部源都挂」的场景白等几分钟。
                                    #   3 个已覆盖「官方最新版目录被清」的常见缺口。
# 下载下限：**只**用来挡「拿到一个 HTML 错误页 / 空文件」这种明显异常。
# 不用来判断包是否完整——tar.gz 压缩率很高（实测 21 MB 的包解出来 41 MB），
# 拿压缩后字节数当完整性判据本就不可靠。真正的完整性由「能不能解出 tor.exe」决定（见调用方）。
# v1.9.58 从 1 MB 降到 4 KB：1 MB 会把「上游将来去掉 geoip 后的小包」误判成不完整。
PMS_TOR_MIN_BUNDLE_BYTES = 4 * 1024

_VERSION_DIR_RE = re.compile(r'href="(\d+\.\d+\.\d+)/"')


@dataclass(frozen=True)
class PmsTorCandidate:
    """一个「可尝试的源」：描述 + 取成员字节的 callable。

    `label` 直接进日志与界面说明；`cost` 只用于告诉用户大概要等多久。
    """

    label: str
    kind: str           # dist | archive | browser | local
    version: str
    fetch: Any          # () -> dict[str, bytes]
    cost: str           # 「约 21 MB」/「约 108 MB」/「本机文件，无流量」


def pts_version_key(pms_version: str) -> tuple[int, ...]:
    """`15.0.25` → `(15, 0, 25)`；用于版本排序。非数字一律当 0。"""
    numbers = re.findall(r"\d+", str(pms_version or ""))
    return tuple(int(item) for item in numbers) if numbers else (0,)


def pts_list_versions(pms_index_url: str, pms_openers: list[tuple[str, Any]],
                      pms_logger: Optional[logging.Logger] = None) -> tuple[list[str], str]:
    """拉版本目录列表，按版本号**降序**返回；返回 (版本列表, 错误说明)。"""
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".tor")
    errors: list[str] = []
    for name, opener in (pms_openers or []):
        try:
            request = urllib.request.Request(pms_index_url, headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT})
            with opener.open(request, timeout=PMS_TOR_HTTP_TIMEOUT) as response:
                body = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"[{name}] {exc}")
            continue
        versions = _VERSION_DIR_RE.findall(body)
        if not versions:
            errors.append(f"[{name}] 目录列表里没有版本目录")
            continue
        unique = sorted(set(versions), key=pts_version_key, reverse=True)
        logger.info("Tor 版本列表：%s 共 %d 个（最高 %s）", pms_index_url, len(unique), unique[0])
        return unique, ""
    return [], "；".join(errors) if errors else "没有可用网络通路"


def pts_bundle_url(pms_version: str, pms_index: str = PMS_TOR_DIST_INDEX) -> str:
    """Expert Bundle 直链（官方 / 归档同文件名，只是 index 不同）。"""
    return f"{pms_index.rstrip('/')}/{pms_version}/" + PMS_TOR_DIST_BUNDLE.format(version=pms_version)


def pts_browser_url(pms_version: str, pms_index: str = PMS_TOR_DIST_INDEX) -> str:
    """Tor 浏览器便携版直链（最后手段）。"""
    return f"{pms_index.rstrip('/')}/{pms_version}/" + PMS_TOR_BROWSER_EXE.format(version=pms_version)


def pts_find_7z(pms_logger: Optional[logging.Logger] = None) -> str:
    """找 7-Zip；找不到返回空串（调用方据此跳过「浏览器提取」这一级，不报错）。

    v1.9.58 实测本机在 `C:\\Program Files\\7-Zip\\7z.exe`；用户机器可能没装，
    所以**只找 PATH + 两个常见安装目录**，绝不因为找不到就中断整条兜底链。
    """
    for name in PMS_TOR_SEVENZIP_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for directory in PMS_TOR_SEVENZIP_DIRS:
        for name in PMS_TOR_SEVENZIP_NAMES:
            candidate = Path(directory) / name
            if candidate.is_file():
                return str(candidate)
    return ""


def pts_archive_url_exists(pms_url: str, pms_openers: list[tuple[str, Any]],
                           pms_logger: Optional[logging.Logger] = None) -> bool:
    """HEAD 探一下归档站这个版本在不在（归档站版本号很杂，拼错就 404）。

    ★ 只有**确切的 404** 才判「不存在」。网络错误 / 超时**不判不存在**——
      那会让「HEAD 被墙但下载能通」的机器白白跳过归档站（返回 True 让调用方去真下，
      下不到再算失败，比静默跳过更稳）。
    """
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".tor")
    saw_error = False
    for name, opener in (pms_openers or []):
        try:
            request = urllib.request.Request(
                pms_url, headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT}, method="HEAD")
            with opener.open(request, timeout=PMS_TOR_BROWSER_HEAD_TIMEOUT) as response:
                if response.status == 200:
                    return True
                if response.status == 404:
                    return False
                logger.info("Tor 归档 HEAD 意外状态 %s（%s）", response.status, name)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            logger.info("Tor 归档 HEAD 失败（%s @ %s）", exc.code, name)
            saw_error = True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.info("Tor 归档 HEAD 失败（%s @ %s）", name, exc)
            saw_error = True
    # 一条通路都没给出确定答案（全网络错误）→ 当「可能存在」，让调用方真去下载
    return saw_error


def pts_download_bytes(pms_url: str, pms_openers: list[tuple[str, Any]],
                       pms_logger: Optional[logging.Logger] = None,
                       pms_on_progress: Any = None) -> tuple[bytes, str]:
    """按通路退让下载；返回 (字节, 错误说明)。沿用 `tordownload` 的同款口径。"""
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".tor")
    errors: list[str] = []
    for name, opener in (pms_openers or []):
        try:
            request = urllib.request.Request(pms_url, headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT})
            with opener.open(request, timeout=PMS_TOR_HTTP_TIMEOUT) as response:
                total = int(response.headers.get("Content-Length") or 0)
                chunks: list[bytes] = []
                got = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    got += len(chunk)
                    if pms_on_progress is not None:
                        pms_on_progress(got, total,
                                        f"已下载 {got // 1024} KB" + (f" / {total // 1024} KB" if total else ""))
            payload = b"".join(chunks)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"[{name}] {exc}")
            continue
        if payload:
            if len(payload) < PMS_TOR_MIN_BUNDLE_BYTES:
                errors.append(f"[{name}] 只收到 {len(payload)} 字节（不完整）")
            else:
                return payload, ""
    return b"", "下载失败：" + ("；".join(errors) if errors else "没有可用网络通路")


def pts_tar_members(pms_payload: bytes) -> dict[str, bytes]:
    """tar.gz 字节 → {文件名: 内容}。只挑 tor 系四个可执行文件。

    兼容 Expert Bundle（条目在 `tor/tor.exe`、`tor/pluggable_transports/…`）
    与将来可能出现的平铺布局：一律按 **basename** 匹配。
    """
    import io
    import tarfile

    wanted = {PMS_TOR_EXE_NAME.lower(), PMS_TOR_LYREBIRD_NAME.lower(),
              PMS_TOR_CONJURE_NAME.lower(), PMS_TOR_SNOWFLAKE_NAME.lower()}
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(pms_payload), mode="r:gz") as archive:
        for item in archive.getmembers():
            if not item.isfile():
                continue
            base = Path(item.name).name
            if base.lower() not in wanted:
                continue
            handle = archive.extractfile(item)
            if handle is None:
                continue
            members[base] = handle.read()
    return members


def pts_sevenzip_members(pms_exe: bytes, pms_logger: Optional[logging.Logger] = None) -> tuple[dict[str, bytes], str]:
    """Tor 浏览器便携版（7z SFX）→ {文件名: 内容}。

    v1.9.58 实测：`tor-browser-windows-x86_64-portable-15.0.25.exe` 是
    `SubType = NSIS-3 Unicode 64-bit` 的 7z 自解压包，7-Zip 能直接按条目抽文件，
    抽出来的 tor.exe / lyrebird.exe 与 Expert Bundle 的 **SHA-256 完全一致**。
    没有 7-Zip 就返回空 + 说明（调用方继续退让，不中断）。
    """
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".tor")
    seven_zip = pts_find_7z(logger)
    if not seven_zip:
        return {}, "本机没有 7-Zip，无法从 Tor 浏览器便携版提取（装一个 7-Zip 就能启用这一级兜底）"
    import tempfile

    with tempfile.TemporaryDirectory(prefix="pms_tor_") as work:
        archive = Path(work) / "browser.exe"
        archive.write_bytes(pms_exe)
        target = Path(work) / "out"
        target.mkdir(parents=True, exist_ok=True)
        command = [seven_zip, "x", "-y", f"-o{target}", str(archive)]
        for name in PMS_TOR_BROWSER_PATHS.values():
            command.append(name)
        try:
            result = subprocess.run(command, capture_output=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            return {}, f"调用 7-Zip 失败：{exc}"
        if result.returncode != 0:
            detail = (result.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
            return {}, "7-Zip 解压 Tor 浏览器失败：" + (detail[-1][:120] if detail else f"退出码 {result.returncode}")
        members: dict[str, bytes] = {}
        for name in PMS_TOR_BROWSER_PATHS:
            found = target / PMS_TOR_BROWSER_PATHS[name]
            if found.is_file() and found.stat().st_size > 0:
                members[name] = found.read_bytes()
        return members, ""


def pts_local_browser_dirs() -> list[Path]:
    """本机常见 Tor 浏览器安装位置（0 流量兜底：用户本机装过浏览器时直接复制）。"""
    from pathlib import Path as _Path

    roots = [
        _Path.home() / "Desktop",
        _Path.home() / "Downloads",
        _Path(r"C:\Program Files"),
        _Path(r"C:\Program Files (x86)"),
    ]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            if "tor browser" in child.name.lower():
                found.append(child)
    return found


def pts_local_browser_members(pms_logger: Optional[logging.Logger] = None) -> dict[str, bytes]:
    """从本机已装的 Tor 浏览器里复制 tor 系文件（找不到就返回空 dict）。"""
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".tor")
    members: dict[str, bytes] = {}
    for directory in pts_local_browser_dirs():
        # 便携版解包后是 `Tor Browser\Browser\TorBrowser\Tor\...`，装好后结构一致
        base = directory / "Browser" / "TorBrowser" / "Tor"
        if not base.is_dir():
            continue
        targets = {
            PMS_TOR_EXE_NAME: base / PMS_TOR_EXE_NAME,
            PMS_TOR_LYREBIRD_NAME: base / "PluggableTransports" / PMS_TOR_LYREBIRD_NAME,
            PMS_TOR_CONJURE_NAME: base / "PluggableTransports" / PMS_TOR_CONJURE_NAME,
            PMS_TOR_SNOWFLAKE_NAME: base / "PluggableTransports" / PMS_TOR_SNOWFLAKE_NAME,
        }
        for name, path in targets.items():
            if path.is_file() and path.stat().st_size > 0:
                members[name] = path.read_bytes()
        if members:
            logger.info("Tor 组件：从本机已装的 Tor 浏览器复制 %d 个文件（%s）", len(members), directory)
            break
    return members
