#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tor Expert Bundle 自动下载与管理（v1.9.46 = Tor 桥接出口第二期）

**做什么**：从 Tor Project 官方目录拉 Windows x64 的 Expert Bundle（`.tar.gz`，约 20 MB），
解压出 `tor.exe` / `lyrebird.exe`（obfs4 + WebTunnel 都靠它），
放进 `<数据目录>\\tor`，并把版本号写进 `tor.bundle_version`。

★ v1.9.55 实查 **Tor 15.0.25**：Expert Bundle 里**已经没有 `snowflake-client.exe`**
（上游换成 `conjure-client.exe`），`meek-client.exe` 也一直不在。
所以这两个按「可选组件」处理——缺了只在成功消息里说明，**不拦死安装**
（老代码把三者都当必需，导致只用 obfs4 / webtunnel 的用户也被弹错装不上，见 `PMS_TOR_BUNDLE_REQUIRED`）。

**实查（2026-10-12，经本机代理实测，不要凭印象改）**：

    https://dist.torproject.org/torbrowser/                      → 版本目录列表（如 15.0.24/）
    https://dist.torproject.org/torbrowser/15.0.24/
        tor-expert-bundle-windows-x86_64-15.0.24.tar.gz           → HTTP 200，就是要这个
    （同目录还有 android / linux / macOS 的各种包，按 `windows-x86_64` 精确匹配）

**复用既有机制**（不另写一套）：下载链路用 `pms.telegram.pms_telegram_build_openers()`
（本程序代理 → 外部代理 → 直连，与核心下载 / 电报抓取同一套退让）；
落盘用 `pms.utils.pms_atomic_write_bytes()`；旧文件先改名 `.bak`（与
`download.pcdl_write_core_file()` 同一约定）；`.tar.gz` **必须**用标准库 `tarfile`
（7-Zip 对 `.tar.gz` 只解一层，见第十三章第 25 条）。

**为什么不做断点续传**：桥接包只有 ~20 MB，而 Tor 的引导与桥接成功率才是真正的难点；
v1.9.41 已经给「0 字节 / 不完整」加了同链路重试一次，这里沿用同一套退让即可。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import (
    QObject,
    QRunnable,
    Signal,
    Slot,
)

from pms.constants import (
    PMS_CORE_KIND_SINGBOX,
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_LOGGER_NAME,
    PMS_TELEGRAM_PROXY_AUTO,
)
from pms.utils import pms_atomic_write_bytes
from pms.telegram import pms_telegram_build_openers
from pms.tor_bridge import (
    PMS_TOR_BUNDLE_MEMBERS,
    PMS_TOR_BUNDLE_OPTIONAL,
    PMS_TOR_BUNDLE_REQUIRED,
    PMS_TOR_EXE_NAME,
    ptb_binaries_state,
    ptb_build_tor_outbound,
    ptb_tor_paths,
)
from pms.torsource import (
    PMS_TOR_ARCHIVE_INDEX,
    PMS_TOR_DIST_INDEX,
    PMS_TOR_MAX_ARCHIVE_VERSIONS,
    pts_archive_url_exists,
    pts_browser_url,
    pts_bundle_url,
    pts_download_bytes,
    pts_list_versions,
    pts_local_browser_members,
    pts_sevenzip_members,
    pts_tar_members,
)

PMS_TOR_INDEX_URL = "https://dist.torproject.org/torbrowser/"
PMS_TOR_BUNDLE_TEMPLATE = "tor-expert-bundle-windows-x86_64-{version}.tar.gz"
PMS_TOR_CHECK_URL = "dist.torproject.org"
PMS_TOR_HTTP_TIMEOUT = 30.0
PMS_TOR_DOWNLOAD_ATTEMPTS = 2   # 同一条链路「0 字节 / 不完整」时重试一次（与核心下载同口径）

_VERSION_DIR_RE = re.compile(r'href="(\d+\.\d+\.\d+)/"')


def pms_tor_bundle_members() -> tuple[str, ...]:
    """Expert Bundle 里程序真正要用的三个文件名（导出给启动器 / 界面展示）。"""
    return tuple(PMS_TOR_BUNDLE_MEMBERS)


def ptd_version_key(pms_version: str) -> tuple[int, ...]:
    """版本号 → 可比较元组（`15.0.24` → `(15, 0, 24)`）。"""
    return tuple(int(item) for item in re.findall(r"\d+", str(pms_version or "")))


def ptd_latest_version(pms_openers: list[tuple[str, Any]], pms_logger: Optional[logging.Logger] = None) -> tuple[str, str]:
    """抓官方目录列表挑最高版本；返回 (版本, 错误说明)。**只挑版本目录，不猜文件名。**"""
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".tor")
    request = urllib.request.Request(PMS_TOR_INDEX_URL, headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT})
    errors: list[str] = []
    for name, opener in (pms_openers or []):
        try:
            with opener.open(request, timeout=PMS_TOR_HTTP_TIMEOUT) as response:
                body = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"[{name}] {exc}")
            continue
        versions = _VERSION_DIR_RE.findall(body)
        if not versions:
            errors.append(f"[{name}] 目录列表里没有版本目录")
            continue
        best = max(versions, key=ptd_version_key)
        logger.info("Tor Expert Bundle：最新版本 %s（通路「%s」）", best, name)
        return best, ""
    return "", "查询 Tor 版本失败：" + ("；".join(errors) if errors else "没有可用网络通路")


def ptd_bundle_url(pms_version: str) -> str:
    """某个版本的 Windows x64 Expert Bundle 直链。"""
    return PMS_TOR_INDEX_URL + f"{pms_version}/" + PMS_TOR_BUNDLE_TEMPLATE.format(version=pms_version)


def ptd_extract_members(pms_archive: Path) -> dict[str, bytes]:
    """`.tar.gz` → {文件名: 内容}，只挑系统真正要用的三个可执行文件。

    ★ `.tar.gz` 必须用 `tarfile`：7-Zip 对两层压缩只解一层（第十三章第 25 条实测）。
    """
    wanted = {name.lower() for name in PMS_TOR_BUNDLE_MEMBERS}
    members: dict[str, bytes] = {}
    with tarfile.open(pms_archive, "r:gz") as archive:
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


def ptd_write_member(pms_target: Path, pms_payload: bytes) -> str:
    """落位一个文件：旧文件先改名 `.bak`，写失败回滚（与核心下载同一约定）。

    返回**错误说明**（空串 = 成功）；底层 `pms_atomic_write_bytes()` 是「True = 成功」，
    这里帮调用方把它翻译成项目统一的「空串即成功」口径。
    """
    backup = pms_target.with_suffix(pms_target.suffix + ".bak")
    try:
        if pms_target.is_file():
            try:
                if backup.exists():
                    backup.unlink()
                pms_target.rename(backup)
            except OSError as exc:
                return f"备份旧文件失败：{exc}"
        if pms_atomic_write_bytes(pms_target, pms_payload):
            return ""
        raise OSError("原子写返回失败")
    except OSError as exc:
        try:
            if backup.is_file() and not pms_target.is_file():
                backup.rename(pms_target)
        except OSError:
            pass
        return f"写入失败：{exc}"


def ptd_download_bytes(
    pms_url: str, pms_openers: list[tuple[str, Any]], pms_logger: Optional[logging.Logger] = None,
    pms_on_progress: Any = None,
) -> tuple[bytes, str]:
    """按通路退让下载；返回 (字节, 错误说明)。同链路「0 字节」会重试一次。"""
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".tor")
    errors: list[str] = []
    for name, opener in (pms_openers or []):
        for attempt in range(PMS_TOR_DOWNLOAD_ATTEMPTS):
            try:
                with opener.open(urllib.request.Request(
                        pms_url, headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT}), timeout=PMS_TOR_HTTP_TIMEOUT) as response:
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
                            pms_on_progress(got, total, f"已下载 {got // 1024} KB" + (f" / {total // 1024} KB" if total else ""))
                payload = b"".join(chunks)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                errors.append(f"[{name}] {exc}")
                payload = b""
            if payload:
                if len(payload) < 1024 * 1024:
                    errors.append(f"[{name}] 只收到 {len(payload)} 字节（不完整）")
                    payload = b""
                else:
                    logger.info("Tor Expert Bundle 下载完成：%d 字节（通路「%s」）", len(payload), name)
                    return payload, ""
            if attempt == 0:
                time.sleep(1.0)
        errors.append(f"[{name}] 放弃")
    return b"", "下载 Tor 组件失败：" + ("；".join(errors) if errors else "没有可用网络通路")


def ptd_install_bundle(
    pms_payload: bytes, pms_paths: Any, pms_settings: Any = None, pms_version: str = "",
    pms_members: Optional[dict[str, bytes]] = None,
) -> tuple[bool, str]:
    """把下载到的 tar.gz 解压落位到 `<数据目录>/tor`，并记录版本。返回 (成功, 说明)。

    v1.9.58：`pms_members` 允许调用方直接传已解好的成员（多源兜底时，
    Tar 浏览器便携版这一级是 7-Zip 抽出来的，不再是 tar.gz）。
    """
    logger = logging.getLogger(PMS_LOGGER_NAME + ".tor")
    paths = ptb_tor_paths(pms_paths, pms_settings)
    tor_dir = Path(paths["tor_dir"])
    try:
        tor_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"创建目录失败：{exc}"
    if pms_members is None:
        archive = tor_dir / f"tor-expert-bundle-{pms_version or 'download'}.tar.gz"
        if not pms_atomic_write_bytes(archive, pms_payload):
            return False, f"写入压缩包失败：{archive}"
        try:
            members = ptd_extract_members(archive)
        except (tarfile.TarError, OSError) as exc:
            return False, f"解压 Tor 组件失败：{exc}"
        finally:
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        members = dict(pms_members)
    missing = [name for name in PMS_TOR_BUNDLE_REQUIRED if name not in members]
    if missing:
        return False, f"压缩包里缺少组件：{'、'.join(missing)}（上游包结构可能变了，请反馈）"
    # v1.9.55：可选组件（snowflake-client / meek-client）缺了**不拦死**——Tor 15.0.25 起
    # Expert Bundle 已经不带 snowflake-client.exe 了（上游换成 conjure），
    # 老代码把三者都当必需，导致只用 obfs4 / webtunnel 的用户也被弹错装不上。
    # 这里只把缺了的可选组件说清楚，安装照算成功。
    absent = [name for name in PMS_TOR_BUNDLE_OPTIONAL if name not in members]
    note = ""
    if absent:
        note = f"（上游未提供 {'、'.join(absent)}，这几类桥接暂不可用，不影响 obfs4 / webtunnel）"
        logger.info("Tor 组件：上游未提供可选组件 %s", "、".join(absent))
    written: list[str] = []
    for name, payload in members.items():
        error = ptd_write_member(tor_dir / name, payload)
        if error:
            return False, f"安装 {name} 失败：{error}"
        written.append(name)
    if pms_settings is not None and pms_version:
        try:
            pms_settings.psm_set_many({"tor.bundle_version": str(pms_version)}, pms_emit=True)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("记录 Tor 组件版本失败：%s", exc)
    logger.info("Tor 组件已安装到 %s：%s（版本 %s）%s", tor_dir, "、".join(written), pms_version or "未知", note)
    return True, f"Tor 组件已安装：{'、'.join(written)}（版本 {pms_version or '未知'}）{note}"


def ptd_fetch_members(
    pms_openers: list[tuple[str, Any]],
    pms_logger: Optional[logging.Logger] = None,
    pms_paths: Any = None,
    pms_settings: Any = None,
    pms_on_progress: Any = None,
) -> tuple[dict[str, bytes], str, str, str]:
    """**多源兜底**拿 tor 组件成员；返回 (成员, 版本, 来源标签, 错误说明)。

    v1.9.58 起因（实测，不是设计假设）：
      * `dist.torproject.org/torbrowser/` **只保留 2 个版本**（2026-10-14：15.0.20 / 15.0.25），
        15.0.24 的直链已经 404；
      * `archive.torproject.org/tor-package-archive/torbrowser/` 有 **243 个历史版本**；
      * Tor 浏览器便携版（108 MB）里也有这三个文件，SHA-256 与 Expert Bundle 逐字节一致；
      * 这台机器 **torproject.org 直连不通**，只能走外部代理（用户机器上常见）。

    所以按「先小后大、先官方后兜底」的顺序逐个试，**任一级拿到就用**，
    每一级失败的原因都收进日志/返回值，方便用户知道为什么兜底到后面几级。
    """
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".tor")
    attempts: list[str] = []

    # ---- ① 官方最新版 Expert Bundle（21 MB，最快）
    self_versions, error = pts_list_versions(PMS_TOR_DIST_INDEX, pms_openers, logger)
    if error:
        attempts.append(f"官方目录：{error}")
    else:
        for version in self_versions[:3]:
            url = pts_bundle_url(version)
            logger.info("Tor 组件：尝试官方 %s", url)
            payload, error = pts_download_bytes(url, pms_openers, logger, pms_on_progress)
            if error:
                attempts.append(f"官方 {version}：{error}")
                continue
            try:
                members = pts_tar_members(payload)
            except (tarfile.TarError, OSError, ValueError) as exc:
                attempts.append(f"官方 {version}：解压失败 {exc}")
                continue
            if members.get(PMS_TOR_EXE_NAME):
                return members, version, f"官方 Expert Bundle {version}", ""
            attempts.append(f"官方 {version}：包里没有 tor.exe（上游结构可能变了）")

    # ---- ② 归档站 Expert Bundle（21 MB，243 个历史版本，实测可用）
    # ★ 这一级**不依赖 pms_paths**——它只是换个域名按版本号取同一个包。
    #   早先写成 `if pms_paths is not None` 是错的：paths 为 None 时归档站被整级跳过，
    #   等于把「官方目录只剩 2 个版本」这个真实缺口又暴露出来（探针抓到的真 bug）。
    archive_versions, error = pts_list_versions(PMS_TOR_ARCHIVE_INDEX, pms_openers, logger)
    if error:
        attempts.append(f"归档站：{error}")
    else:
        for version in archive_versions[:PMS_TOR_MAX_ARCHIVE_VERSIONS]:
            url = pts_bundle_url(version, PMS_TOR_ARCHIVE_INDEX)
            if not pts_archive_url_exists(url, pms_openers, logger):
                attempts.append(f"归档站 {version}：不存在（已跳过下载）")
                continue
            logger.info("Tor 组件：尝试归档站 %s", url)
            payload, error = pts_download_bytes(url, pms_openers, logger, pms_on_progress)
            if error:
                attempts.append(f"归档站 {version}：{error}")
                continue
            try:
                members = pts_tar_members(payload)
            except (tarfile.TarError, OSError, ValueError) as exc:
                attempts.append(f"归档站 {version}：解压失败 {exc}")
                continue
            if members.get(PMS_TOR_EXE_NAME):
                return members, version, f"归档站 Expert Bundle {version}", ""
            attempts.append(f"归档站 {version}：包里没有 tor.exe")

    # ---- ③ Tor 浏览器便携版提取（108 MB，最后手段；需本机有 7-Zip）
    browser_versions = self_versions or []
    for version in browser_versions[:2]:
        url = pts_browser_url(version)
        logger.info("Tor 组件：尝试 Tor 浏览器便携版 %s（约 108 MB）", url)
        payload, error = pts_download_bytes(url, pms_openers, logger, pms_on_progress)
        if error:
            attempts.append(f"Tor 浏览器 {version}：{error}")
            continue
        members, error = pts_sevenzip_members(payload, logger)
        if error:
            attempts.append(f"Tor 浏览器 {version}：{error}")
            continue
        if members.get(PMS_TOR_EXE_NAME):
            return members, version, f"Tor 浏览器便携版 {version}（7-Zip 提取）", ""
        attempts.append(f"Tor 浏览器 {version}：解出来没有 tor.exe")

    # ---- ④ 本机已装的 Tor 浏览器（0 流量）
    members = pts_local_browser_members(logger)
    if members.get(PMS_TOR_EXE_NAME):
        return members, "本机", "本机已装的 Tor 浏览器（无流量）", ""
    attempts.append("本机已装浏览器：没找到（或没装）")

    # ★ 截断要**保头保尾**：头部是用户最先该看的（官方为什么失败），
    #   尾部是「本机浏览器 / 手动放置」这类最终兜底说明。
    #   早先 `attempts[:8]` 会把尾部挤掉——实测 6 级失败时正好把最后两级切没，
    #   于是用户看不到「本机也没找到」和「手动放置目录」的指引（探针抓到的真问题）。
    if len(attempts) > 8:
        kept = attempts[:5] + [f"…另有 {len(attempts) - 7} 级同类失败略…"] + attempts[-2:]
    else:
        kept = attempts
    detail = "；".join(kept) if kept else "没有可用网络通路"
    tor_hint = (ptb_tor_paths(pms_paths, pms_settings)["tor_dir"]
                if pms_paths is not None else "data\\tor")
    return {}, "", "", ("Tor 组件获取失败，已试过这些来源：\n" + detail +
                        "\n\n兜底：把 tor.exe / lyrebird.exe 等文件直接放进下面的目录即可生效：" +
                        f"\n{tor_hint}")


def ptd_probe_config(pms_settings: Any, pms_paths: Any) -> tuple[dict[str, Any], str]:
    """按当前设置生成一份**最小可校验**的 sing-box 配置（含 tor 出站）。

    刻意压到最小：一个本机 mixed 入口 + tor 出站 + direct/block + 一句 route.final，
    这样「校验不过」的原因只可能来自 tor 出站本身（组件缺失 / 桥接行写错）。
    """
    from pms.coreconfig import PmsSingBoxConfigBuilder
    from pms.coreprofile import PmsCoreLaunchProfile
    from pms.models import PmsProxyNode

    node = PmsProxyNode()
    node.uid = "tor-probe"
    node.name = "Tor 探针节点"
    node.server = "127.0.0.1"
    node.port = 1
    node.protocol = "socks"
    node.enabled = True
    profile = PmsCoreLaunchProfile.pclp_from_settings(pms_settings, node, PMS_CORE_KIND_SINGBOX)
    profile.tun_enabled = False
    profile.mux_enabled = False
    profile.access_log = False
    profile.clash_api_port = 0
    profile.stats_api_port = 0
    if not profile.tor.get("enabled"):
        return {}, "还没启用 Tor 出口（先打开上面的开关并保存）"
    if not profile.tor.get("bridges"):
        return {}, "还没有可用的桥接行（粘贴一行 obfs4 / webtunnel / snowflake 桥接，或从订阅抓）"
    try:
        return PmsSingBoxConfigBuilder(pms_paths).psc_build(profile), ""
    except ValueError as exc:
        return {}, str(exc)


def ptd_check_config(pms_settings: Any, pms_paths: Any, pms_locator: Any = None) -> tuple[bool, str]:
    """**真核心校验**：按当前桥接生成含 `tor` 出站的配置，写临时文件后跑一次 `sing-box check`。

    只校验「配置与组件就位」，**不**校验「Tor 能不能连通」——那要真的把核心跑起来等引导
    （通常 10–60 秒），界面上会明确写清这一点。
    """
    config, error = ptd_probe_config(pms_settings, pms_paths)
    if error:
        return False, error
    if pms_locator is None:
        from pms.coresys import PmsCoreLocator

        pms_locator = PmsCoreLocator(pms_paths)
    exe = pms_locator.pcl_find(PMS_CORE_KIND_SINGBOX, pms_settings)
    if exe is None:
        return False, "找不到 sing-box 核心（Tor 出口只能用 sing-box）"
    target = Path(pms_paths.prt_temp_config_file("tor-check"))
    if not pms_atomic_write_bytes(target, json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")):
        return False, f"写入临时配置失败：{target}"
    try:
        ok, detail = pms_locator.pcl_validate_config(Path(exe), target)
    finally:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
    return (True, "配置校验通过（真 sing-box 认这份含 tor 出站的配置）") if ok else (False, detail)


class PmsTorDownloadSignals(QObject):
    """下载任务 → 主线程。"""

    ptd_progress = Signal(int, int, str)   # 已下载字节, 总量（0=未知）, 说明
    ptd_finished = Signal(bool, str)       # 成功, 说明


class PmsTorDownloadTask(QRunnable):
    """查最新版本 → 下载 Expert Bundle → 解压落位 → 记录版本。"""

    def __init__(
        self,
        pms_signals: PmsTorDownloadSignals,
        pms_paths: Any,
        pms_settings: Any = None,
        pms_local_proxy_port: int = 0,
        pms_external_proxy: str = "",
        pms_force: bool = False,
    ) -> None:
        super().__init__()
        self.ptd_signals = pms_signals
        self.ptd_paths = pms_paths
        self.ptd_settings = pms_settings
        self.ptd_force = bool(pms_force)
        self.ptd_logger = logging.getLogger(PMS_LOGGER_NAME + ".tor")
        self.ptd_openers = pms_telegram_build_openers(
            PMS_TELEGRAM_PROXY_AUTO, int(pms_local_proxy_port or 0), str(pms_external_proxy or "").strip(),
        )
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        state = ptb_binaries_state(self.ptd_paths, self.ptd_settings)
        if state["ready"] and not self.ptd_force:
            self.ptd_signals.ptd_finished.emit(
                True, f"Tor 组件已就绪（{state['tor_dir']}），无需重新下载；要强制更新请点「重新下载」")
            return
        members, version, source_label, error = ptd_fetch_members(
            self.ptd_openers, self.ptd_logger, self.ptd_paths, self.ptd_settings,
            pms_on_progress=lambda got, total, text: self.ptd_signals.ptd_progress.emit(got, total, text),
        )
        if error:
            self.ptd_signals.ptd_finished.emit(False, error)
            return
        ok, message = ptd_install_bundle(b"", self.ptd_paths, self.ptd_settings, version, pms_members=members)
        if ok:
            message = f"[{source_label}] " + message
        self.ptd_signals.ptd_finished.emit(ok, message)
