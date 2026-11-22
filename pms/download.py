#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心下载更新

从官方 GitHub Release 查询、下载、解压并安装核心（带进度与镜像前缀）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, TYPE_CHECKING
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    Signal,
    Slot,
)
import json
import logging
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from pms.constants import (
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_LOGGER_NAME,
    PMS_TELEGRAM_PROXY_AUTO,
)
from pms.utils import (
    pms_atomic_write_bytes,
    pms_safe_int,
)
from pms.paths import PmsRuntimePaths
from pms.coremeta import pms_core_title   # v1.9.45：核心显示名从已删的独立内核模块搬到 coremeta.py
from pms.telegram import pms_telegram_build_openers

if TYPE_CHECKING:  # 仅类型检查用，避免与设置模块循环导入
    from pms.settings import PmsSettingsManager

# --------------------------------------------------------------------------------------
# 核心下载服务（从官方 GitHub Release 获取，支持镜像前缀与进度）
# --------------------------------------------------------------------------------------
@dataclass
class PmsCoreReleaseInfo:
    """一次核心下载所需的信息。"""

    core_kind: str = ""
    version: str = ""
    download_url: str = ""
    asset_name: str = ""
    page_url: str = ""

class PmsCoreDownloadSignals(QObject):
    """下载任务信号。"""

    pcdl_progress = Signal(str, int, int, int, str)   # core_kind, 百分比, 已下载, 总量, 说明
    pcdl_finished = Signal(str, bool, str)            # core_kind, 成功, 说明

def pms_core_api_url(pms_kind: str) -> str:
    """按内核代号拼 GitHub 查询地址（Xray / sing-box 走各自老仓库）。

    v1.9.45：独立内核整块下线，只剩 Xray / sing-box 两个老仓库，一律走 `releases/latest`。
    """
    repo = PmsCoreDownloadTask.pcdl_REPOS.get(str(pms_kind or "").strip(), ("", ""))[0]
    return f"https://api.github.com/repos/{repo}/releases/latest"


def pms_core_select_release(pms_kind: str, pms_payload: object) -> dict:
    """从查询响应里挑出要用的那个 release 对象；挑不到返回空 dict。"""
    if isinstance(pms_payload, dict):
        return pms_payload
    if not isinstance(pms_payload, list):
        return {}
    items = [item for item in pms_payload if isinstance(item, dict)]
    if not items:
        return {}
    stable = [item for item in items if not bool(item.get("prerelease"))]
    return stable[0] if stable else items[0]


# --------------------------------------------------------------------------------------
# 压缩包识别与 7-Zip 兜底（v1.8.1）
#
# 背景（实测，不要凭印象改）：走代理下载 GitHub Release 的 22 MB 包经常被中途截断，
# `zipfile` 对「截断的包」和「压根不是压缩包」报的是同一句 `File is not a zip file`，
# 无法区分，排查时只能瞎猜。因此拆成两步：
#   ① 下载完先校验 魔数 + 长度，把「不完整」和「不是压缩包」分开报清楚（pcdl_verify_archive）；
#   ② 真压缩包但 Python 解不开（压缩算法不支持 / 上游改发 .tar.gz）时，用 7-Zip 兜底。
# 7-Zip 退出码非 0 一律**不落位**——它对截断包会「解出半个 exe 还说成功」，
# 装上去就是个跑不起来的内核，比直接失败更难排查（v1.8.1 明确取舍）。
# --------------------------------------------------------------------------------------
# 7z.exe 的常见安装位置；找不到再退到 PATH。用户可在设置里指定路径覆盖。
PMS_SEVEN_ZIP_CANDIDATES = (
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
)
PMS_SEVEN_ZIP_TIMEOUT_SECONDS = 300        # 7z 解压超时（秒），防止代理环境下无限卡住
PMS_SEVEN_ZIP_WORK_DIRNAME = "_7z_extract"  # 7z 兜底解压的临时子目录，用完即删
PMS_ARCHIVE_SNIFF_BYTES = 64               # 只 sniff 文件头这么多字节
# v1.9.41：同一条代理链路上「断流/不完整」最多重试几次（网页伪装那类错误不重试，直接换链路）
PMS_DOWNLOAD_ATTEMPTS_PER_ROUTE = 2
# v1.9.52：断点续传（Range）——`.part` 至少要攒够这么多字节才值得续传，太小不如重下
PMS_DOWNLOAD_RESUME_MIN_BYTES = 262144


def _pms_response_status(pms_response: Any) -> int:
    """取 HTTP 响应状态码，**对所有桩对象形态都安全**（拿不到就返回 0）。

    为什么不能直接写 `response.status or response.getcode()`：`or` 会在左侧是假值时
    **继续求值右侧**，而历史探针（`_scaffold/probe_v1941.py`）的桩是裸 `io.BytesIO` ——
    既没有 `status` 也没有 `getcode()`，于是 AttributeError 把整支老探针崩掉，
    等于把 v1.9.41 的「下载断流重试」回归保护打没了（v1.9.52 修）。
    老探针的桩形态就是既有契约，新代码不许假设它更「真」。
    """
    for attr in ("status", "getcode"):
        value = getattr(pms_response, attr, None)
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - 桩对象千奇百怪，取不到就当没有
                continue
        if value:
            return pms_safe_int(value, 0)
    return 0


def _pms_content_range_start(pms_header: str) -> Optional[int]:
    """解析 `Content-Range: bytes 100-999/1000` 里的起始偏移；格式不对返回 None。

    返回 None 就代表「服务端没有按我们请求的偏移给」→ 调用方必须回退全量重下。
    """
    text = str(pms_header or "").strip().lower()
    if not text.startswith("bytes"):
        return None
    span = text[len("bytes"):].strip().split("/", 1)[0].strip()
    start_text = span.split("-", 1)[0].strip()
    if not start_text.isdigit():
        return None
    return int(start_text)


def _pms_resume_offset(pms_target: Path) -> int:
    """看看 `.part` 里已经攒了多少字节，够格才续传。返回 0 表示「从头下」。"""
    try:
        if not pms_target.is_file():
            return 0
        size = pms_target.stat().st_size
    except OSError:
        return 0
    return size if size >= PMS_DOWNLOAD_RESUME_MIN_BYTES else 0


def _pms_part_size(pms_target: Path) -> int:
    """`pms_target` 当前字节数（不存在或读不到按 0）。"""
    try:
        return pms_target.stat().st_size if pms_target.is_file() else 0
    except OSError:
        return 0

def _pms_archive_signature(pms_head: bytes) -> str:
    """按文件头判断压缩包类型，返回 zip / gzip / 7z / exe / 空串（未知）。

    只认这四类：前三类是要解压的压缩包，exe 是「发布的就是裸可执行文件」（如 hysteria2）。
    返回空串意味着下载到的多半是 HTML 错误页——代理或镜像返回网页时常见。
    """
    if pms_head[:2] == b"PK":
        return "zip"
    if pms_head[:2] == b"\x1f\x8b":
        return "gzip"
    if pms_head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if pms_head[:2] == b"MZ":
        return "exe"
    return ""

def _pms_find_7z(pms_custom: str = "") -> str:
    """找 7z.exe：设置项指定 → 常见安装目录 → PATH。找不到返回空串（交给 Python 解压）。"""
    for candidate in (str(pms_custom or "").strip(),) + tuple(PMS_SEVEN_ZIP_CANDIDATES):
        if candidate and Path(candidate).is_file():
            return candidate
    return shutil.which("7z") or shutil.which("7za") or ""


class PmsCoreDownloadTask(QRunnable):
    """下载并解压指定核心的官方发行包。"""

    pcdl_REPOS = {
        PMS_CORE_KIND_XRAY: ("XTLS/Xray-core", "Xray-windows-64.zip"),
        PMS_CORE_KIND_SINGBOX: ("SagerNet/sing-box", ""),
    }

    def __init__(
        self,
        pms_signals: PmsCoreDownloadSignals,
        pms_paths: PmsRuntimePaths,
        pms_core_kind: str,
        pms_mirror: str = "",
        pms_local_proxy_port: int = 0,
        pms_external_proxy: str = "",
        pms_settings: Optional["PmsSettingsManager"] = None,
    ) -> None:
        super().__init__()
        self.pcdl_signals = pms_signals
        self.pcdl_paths = pms_paths
        self.pcdl_core_kind = str(pms_core_kind)
        self.pcdl_settings = pms_settings
        self.pcdl_mirror = str(pms_mirror or "").strip()
        self.pcdl_local_proxy_port = int(pms_local_proxy_port or 0)
        self.pcdl_external_proxy = str(pms_external_proxy or "").strip()
        self.pcdl_logger = logging.getLogger(PMS_LOGGER_NAME + ".download")
        # 下载链路：本程序代理 → 外部代理 → 直连（与电报抓取同一套退让逻辑，复用不重写）
        self.pcdl_openers = pms_telegram_build_openers(
            PMS_TELEGRAM_PROXY_AUTO, self.pcdl_local_proxy_port, self.pcdl_external_proxy,
        )
        self.setAutoDelete(True)

    def pcdl_api_url(self) -> str:
        """当前核心的 GitHub 查询地址（只认 Xray / sing-box 两个主核心）。"""
        return pms_core_api_url(self.pcdl_core_kind)

    def pcdl_select_release(self, pms_payload: object) -> dict:
        """从响应里挑出要用的那个 release 对象；挑不到返回空 dict。"""
        return pms_core_select_release(self.pcdl_core_kind, pms_payload)

    def pcdl_fetch_release(self) -> tuple[Optional[PmsCoreReleaseInfo], str]:
        """查询最新发行版并挑选 Windows 资源（按代理链路逐个退让）。"""
        request = urllib.request.Request(
            self.pcdl_api_url(),
            headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT, "Accept": "application/vnd.github+json"},
        )
        errors: list[str] = []
        for name, opener in self.pcdl_openers:
            try:
                with opener.open(request, timeout=25) as response:
                    payload = json.loads(response.read().decode("utf-8", errors="replace"))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                errors.append(f"[{name}] {exc}")
                continue
            release = self.pcdl_select_release(payload)
            if not release:
                return None, "发行信息格式异常"
            version = str(release.get("tag_name") or "")
            info = PmsCoreReleaseInfo(core_kind=self.pcdl_core_kind, version=version, page_url=str(release.get("html_url") or ""))
            if self.pcdl_core_kind == PMS_CORE_KIND_XRAY:
                chosen = None
                for asset in (release.get("assets") or []):
                    if str(asset.get("name")) == "Xray-windows-64.zip":
                        chosen = asset
                        break
            else:
                chosen = None
                for asset in (release.get("assets") or []):
                    asset_name = str(asset.get("name") or "")
                    if "windows-amd64" in asset_name and asset_name.endswith(".zip"):
                        chosen = asset
                        break
            if not isinstance(chosen, dict):
                return None, "未找到适用于 Windows x64 的发行文件"
            url = str(chosen.get("browser_download_url") or "")
            if self.pcdl_mirror:
                url = self.pcdl_mirror.rstrip("/") + "/" + url
            info.download_url = url
            info.asset_name = str(chosen.get("name") or "")
            return info, ""
        return None, "查询发行信息失败：" + ("；".join(errors) if errors else "无可用网络通路")

    def pcdl_seven_zip_path(self) -> str:
        """设置项里指定的 7z.exe 路径；留空交给自动探测。"""
        if self.pcdl_settings is None:
            return ""
        try:
            return str(self.pcdl_settings.psm_get("core.seven_zip_path") or "").strip()
        except (AttributeError, TypeError, ValueError):
            return ""

    def pcdl_verify_archive(self, pms_archive: Path, pms_expect_total: int) -> str:
        """下载完先校验（v1.8.1）：空文件 / 长度不足 / 不是压缩包，三类分开报清楚。

        为什么必须校验：`response.read()` 提前返回空只是「流结束」，不会抛异常，
        代理中途断流时短文件会被当成成功写盘，最后在解压阶段报一句无法定位的错误。
        返回空串表示校验通过。
        """
        try:
            size = pms_archive.stat().st_size
            with open(pms_archive, "rb") as handle:
                head = handle.read(PMS_ARCHIVE_SNIFF_BYTES)
        except OSError as exc:
            return f"校验下载文件失败：{exc}"
        if size <= 0:
            return "下载到 0 字节（代理或镜像没有返回内容）"
        if pms_expect_total > 0 and size != pms_expect_total:
            return (
                f"下载不完整：只收到 {size} / {pms_expect_total} 字节"
                "（代理中途断流；已放弃安装以保留原内核，可换个代理或镜像重试）"
            )
        if not _pms_archive_signature(head):
            preview = head[:48].decode("utf-8", errors="replace").replace("\n", " ").strip()
            return f"下载内容不是压缩包（疑似代理/镜像返回了网页）：{preview}"
        return self.pcdl_check_archive_body(pms_archive, head)

    def pcdl_check_archive_body(self, pms_archive: Path, pms_head: bytes) -> str:
        """v1.9.52：**不看服务端声明**，直接把压缩包读一遍，验证它自身结构完整。

        为什么必须补这一层：`Content-Length` 是**服务端说的**。代理把连接截断时，
        有些镜像会回一个「恰好等于它实际发出的字节数」的长度（甚至干脆不发），
        于是 `size == expect_total` 成立、长度校验形同虚设，截断的包会被当成成功。
        压缩包自身的**尾部结构**骗不了人：zip 的中央目录、gzip/tar 的结束标记没了就是没了。
        返回空串表示结构完整（或类型未知——未知类型交给后面的解压阶段按原逻辑处理）。
        """
        if not pms_head[:2] in (b"PK", b"\x1f\x8b"):
            return ""
        try:
            if pms_head[:2] == b"PK":
                import zipfile
                with zipfile.ZipFile(pms_archive) as archive:
                    if archive.testzip() is not None:
                        return "下载的压缩包已损坏（内部校验不过，多为代理中途断流）"
            else:
                import tarfile
                with tarfile.open(pms_archive, "r:*") as tar:
                    if tar.next() is None:
                        return "下载的压缩包已损坏（读不到任何成员，多为代理中途断流）"
        except Exception as exc:  # noqa: BLE001 - 结构读不出来就是坏的，具体异常类型不重要
            return f"下载不完整或压缩包损坏：{exc}"
        return ""

    def pcdl_download_attempt(
        self, pms_info: PmsCoreReleaseInfo, pms_target: Path, pms_offset: int, pms_opener: Any,
    ) -> tuple[int, int, str]:
        """**单条链路**、一次尝试：按 `pms_offset` 续传或全量下载，返回 (总长度, 实收字节, 错误)。

        v1.9.52 断点续传（Range）：
        · `pms_offset > 0` 时带 `Range: bytes=N-`，并把响应**追加**到 `.part`；
        · 服务端不支持（回 200 而不是 206 / 没有 `Content-Range`）时**必须**回头全量重下——
          直接追加会把「已下的一半」和「从头来的整份」拼成一个坏文件，且长度校验**未必**拦得住；
        · `Content-Range` 的起始偏移必须**正好等于**请求的偏移，否则同样回退（防服务端给错段）。
        · `pms_opener` 由调用方给定，**每条链路各调一次**，退让逻辑仍在 `pcdl_download()` 里。
        """
        request = urllib.request.Request(
            pms_info.download_url, headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT},
        )
        if pms_offset > 0:
            request.add_header("Range", f"bytes={pms_offset}-")
        try:
            with pms_opener.open(request, timeout=60) as response:
                status = _pms_response_status(response)
                total = pms_safe_int(response.headers.get("Content-Length"), 0)
                range_header = response.headers.get("Content-Range")
                resume_ok = self._pcdl_resume_usable(response, status, pms_offset)
                if pms_offset > 0 and not resume_ok:
                    # 服务端不肯好好给这段（回 200 / Content-Range 偏移不对）：
                    # **必须另发一个不带 Range 的请求从头下**。不能就地「截断后继续读」——
                    # 因为服务端既然在按 Range 回内容，我们续读到的仍然只是后半段，
                    # 截断会把前半段删掉、只留下后半段，拼出一个长度对不上的坏文件。
                    self.pcdl_logger.warning(
                        "服务端未按请求偏移提供内容（HTTP %s，Content-Range=%r），改为从头全量重下",
                        status or "未知", range_header,
                    )
                    return self._pcdl_download_full(pms_info, pms_target, pms_opener)
                start = pms_offset if resume_ok else 0
                # ★ 必须在 `with` 内读完：出了 with 响应体就关了，读到的永远是空
                return self._pcdl_read_into(response, pms_target, "ab" if resume_ok else "wb",
                                            start, total, pms_info)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return 0, 0, str(exc)

    def _pcdl_download_full(
        self, pms_info: PmsCoreReleaseInfo, pms_target: Path, pms_opener: Any,
    ) -> tuple[int, int, str]:
        """不带 Range 的干净全量下载（回退路径用，保证拿到的是完整一份）。"""
        request = urllib.request.Request(
            pms_info.download_url, headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT},
        )
        try:
            with pms_opener.open(request, timeout=60) as fresh:
                total = pms_safe_int(fresh.headers.get("Content-Length"), 0)
                return self._pcdl_read_into(fresh, pms_target, "wb", 0, total, pms_info)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return 0, 0, str(exc)

    @staticmethod
    def _pcdl_resume_usable(pms_response: Any, pms_status: int, pms_offset: int) -> bool:
        """这次响应能不能真的用来续传：必须 206，且 `Content-Range` 起始偏移**正好**是请求的偏移。"""
        if pms_offset <= 0 or pms_status != 206:
            return False
        return _pms_content_range_start(pms_response.headers.get("Content-Range") or "") == pms_offset

    def _pcdl_read_into(
        self, pms_response: Any, pms_target: Path, pms_mode: str,
        pms_start: int, pms_total: int, pms_info: PmsCoreReleaseInfo,
    ) -> tuple[int, int, str]:
        """把响应体写进 `pms_target`（`pms_mode` 决定追加还是截断），带进度。返回 (总长度, 字节数, 错误)。"""
        downloaded = 0
        try:
            with open(pms_target, pms_mode) as handle:
                while True:
                    chunk = pms_response.read(262144)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    done = pms_start + downloaded
                    whole = pms_total + pms_start
                    percent = int(done * 100 / whole) if whole else 0
                    self.pcdl_signals.pcdl_progress.emit(
                        self.pcdl_core_kind, min(percent, 100), done, whole,
                        f"下载 {pms_info.asset_name}（{pms_info.version}）"
                        + (f"（断点续传 {pms_start} 字节起）" if pms_start else ""),
                    )
        except OSError as exc:
            return 0, 0, str(exc)
        return (pms_total + pms_start), _pms_part_size(pms_target), ""

    def pcdl_download(self, pms_info: PmsCoreReleaseInfo, pms_target: Path) -> str:
        """带进度地下载压缩包（按代理链路逐个退让），返回错误说明。

        v1.8.1：每条链路下载完都要过 `pcdl_verify_archive()`，校验不通过就换下一条链路，
        而不是把一个坏文件交给解压阶段。

        v1.9.41：**每条链路再加一次重试**（文档十三章第 26 条从「登记」变成「已修」）。
        原因：弱网下断流是概率事件，原来「校验失败 → 换链路」，三条链路都断流就直接失败；
        实测同一链路重试一次经常就好了。重试只针对「下载不完整 / 0 字节」这类**可能自愈**的错
        （网页伪装那类明确的服务端错误不重试，直接换链路，省得白等）。

        v1.9.52：**断点续传**（文档十三章第 24 条的续传部分）。不再从 0 开始，而是带着
        `Range: bytes=N-` 接着 `.part` 已攒的字节下（包括**上次进程退出时留下的**半截文件——
        这正是断点续传的意义）；服务端不支持就地回退全量重下。

        ★ 续传的判定只看「`.part` 里有没有够多的字节」，**不看这是第几次尝试**：
        若只看第 2 次，那么「上次启动断在 90%」这种最常见的情况反而不会续传，
        反而从头下一遍——等于白做。第 1 次就用已有字节，失败后自然继续往下接。
        """
        errors: list[str] = []
        for name, opener in self.pcdl_openers:
            for attempt in range(PMS_DOWNLOAD_ATTEMPTS_PER_ROUTE):
                # 每次尝试都重新读一遍实际字节数：上一次尝试可能又写进去一些
                offset = _pms_resume_offset(pms_target)
                total, got, error = self.pcdl_download_attempt(pms_info, pms_target, offset, opener)
                if error:
                    errors.append(f"[{name}] {error}")
                    self.pcdl_logger.warning("核心下载失败（链路「%s」，第 %d 次）：%s", name, attempt + 1, error)
                    break                     # 连不上就别在同一条链路上磨，换下一条
                verify_error = self.pcdl_verify_archive(pms_target, total)
                if not verify_error:
                    if offset > 0:
                        self.pcdl_logger.info("核心下载已断点续传完成（链路「%s」，%d 字节）", name, got)
                    return ""
                errors.append(f"[{name}] {verify_error}")
                self.pcdl_logger.warning("核心下载校验未通过（%s，第 %d 次）：%s", name, attempt + 1, verify_error)
                if "不完整" not in verify_error and "0 字节" not in verify_error:
                    break                     # 网页伪装 / 不是压缩包 → 换链路，重试没意义
                if attempt + 1 < PMS_DOWNLOAD_ATTEMPTS_PER_ROUTE:
                    self.pcdl_logger.info("核心下载断流，链路「%s」带 Range 续传重试", name)
        # 全部尝试都失败：把半截 `.part` 清掉，别让下一次启动误以为能续传
        try:
            if pms_target.is_file():
                pms_target.unlink()
        except OSError:
            pass
        return "下载失败：" + ("；".join(errors) if errors else "无可用网络通路")

    def pcdl_write_core_file(self, pms_target: Path, pms_payload: bytes) -> str:
        """写入核心可执行文件：先把旧文件改名成 .bak，写失败自动回滚。"""
        backup: Optional[Path] = None
        try:
            existed = pms_target.is_file()
        except OSError as exc:
            return f"检查旧文件失败：{exc}"
        if existed:
            backup = pms_target.with_name(pms_target.name + ".bak")
            try:
                if backup.exists():
                    backup.unlink()
                pms_target.replace(backup)
            except OSError as exc:
                return f"备份旧文件失败（未改动原文件）：{exc}"
        try:
            pms_atomic_write_bytes(pms_target, pms_payload)
        except OSError as exc:
            if backup is not None:
                try:
                    if backup.exists():
                        backup.replace(pms_target)
                except OSError:
                    pass
            return f"写入失败（已回滚）：{exc}"
        return ""

    def pcdl_extract_with_7z(self, pms_archive: Path, pms_work_dir: Path) -> str:
        """用 7-Zip 把压缩包解到临时目录，返回错误说明（空串=成功）。

        退出码非 0 一律当失败：7-Zip 对截断包会「解出半个文件还给退出码 2」，
        按 v1.8.1 的取舍，这种情况宁可失败也不能把残缺内核装进去。
        """
        seven_zip = _pms_find_7z(self.pcdl_seven_zip_path())
        if not seven_zip:
            return "未找到 7-Zip（可在「设置 → 核心 → 下载 / 更新核心 → 7-Zip 路径」里指定 7z.exe）"
        try:
            if pms_work_dir.exists():
                shutil.rmtree(pms_work_dir, ignore_errors=True)
            pms_work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"创建解压临时目录失败：{exc}"
        command = [seven_zip, "x", str(pms_archive), f"-o{pms_work_dir}", "-y", "-bso0", "-bsp0"]
        try:
            result = subprocess.run(command, capture_output=True, timeout=PMS_SEVEN_ZIP_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"调用 7-Zip 失败：{exc}"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
            tail = f"：{detail[-120:]}" if detail else ""
            return f"7-Zip 退出码 {result.returncode}（多为压缩包不完整，已保留原内核）{tail}"
        return ""

    def pcdl_read_members_python(self, pms_archive: Path) -> dict[str, bytes]:
        """用标准库读压缩包成员：zip 走 zipfile，tar.gz / tar 走 tarfile，返回 {文件名: 内容}。

        v1.8.1：实测 7-Zip 对 `.tar.gz` 只解一层（只吐出内层 .tar，退出码仍是 0），
        而标准库的 `tarfile` 本来就能直接解 gzip 流——所以按魔数分流，不依赖 7-Zip。
        返回空 dict 表示「包能打开，但里面没有文件成员」；打不开则抛异常（交给 7-Zip 兜底）。
        """
        import tarfile
        import zipfile

        members: dict[str, bytes] = {}
        with open(pms_archive, "rb") as handle:
            head = handle.read(PMS_ARCHIVE_SNIFF_BYTES)
        if _pms_archive_signature(head) == "gzip":
            with tarfile.open(pms_archive, "r:*") as tar:
                for item in tar.getmembers():
                    if not item.isfile():
                        continue
                    extracted = tar.extractfile(item)
                    if extracted is None:
                        continue
                    members[Path(item.name).name] = extracted.read()
            return members
        with zipfile.ZipFile(pms_archive) as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                members[Path(name).name] = archive.read(name)
        return members

    def pcdl_fallback_bundle(self, pms_archive: Path, pms_target_dir: Path, pms_wanted: tuple[str, ...]) -> str:
        """7-Zip 兜底安装主核心（xray / sing-box）：解到临时目录后按文件名挑需要的成员。"""
        work_dir = pms_target_dir / PMS_SEVEN_ZIP_WORK_DIRNAME
        wanted_low = [item.lower() for item in pms_wanted]
        try:
            error = self.pcdl_extract_with_7z(pms_archive, work_dir)
            if error:
                return error
            try:
                found = [path for path in work_dir.rglob("*") if path.is_file()]
            except OSError as exc:
                return f"遍历解压目录失败：{exc}"
            copied = 0
            for path in found:
                if path.name.lower() not in wanted_low:
                    continue
                try:
                    payload = path.read_bytes()
                except OSError:
                    continue
                error = self.pcdl_write_core_file(pms_target_dir / path.name, payload)
                if error:
                    return error
                copied += 1
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        if copied == 0:
            return f"7-Zip 解压后未找到预期的核心文件（{'、'.join(pms_wanted)}）"
        return ""

    def pcdl_install(self, pms_archive: Path) -> str:
        """解压并把可执行文件与资源放进 core/<kind>/（zip / tar.gz 都支持）。"""
        target_dir = self.pcdl_paths.prt_core_dir / ("xray" if self.pcdl_core_kind == PMS_CORE_KIND_XRAY else "sing-box")
        target_dir.mkdir(parents=True, exist_ok=True)
        wanted = ("xray.exe", "geoip.dat", "geosite.dat", "wintun.dll", "LICENSE") if self.pcdl_core_kind == PMS_CORE_KIND_XRAY else ("sing-box.exe", "libcronet.dll", "LICENSE")
        copied = 0
        wanted_low = [item.lower() for item in wanted]
        try:
            members = self.pcdl_read_members_python(pms_archive)
        except Exception as exc:  # noqa: BLE001 - 同上，交给 7-Zip 兜底
            self.pcdl_logger.warning("标准库解压失败（%s），改用 7-Zip 兜底", exc)
            fallback = self.pcdl_fallback_bundle(pms_archive, target_dir, wanted)
            if fallback:
                return f"解压失败：{exc}；{fallback}"
            return ""
        for base, payload in members.items():
            if base.lower() not in wanted_low:
                continue
            pms_atomic_write_bytes(target_dir / base, payload)
            copied += 1
            self.pcdl_signals.pcdl_progress.emit(self.pcdl_core_kind, 100, copied, len(wanted), f"解压 {base}")
        if copied == 0:
            return "压缩包内未找到预期的核心文件"
        return ""

    def run(self) -> None:  # noqa: D102
        label = pms_core_title(self.pcdl_core_kind)
        self.pcdl_signals.pcdl_progress.emit(self.pcdl_core_kind, 0, 0, 0, f"正在查询 {label} 最新版本…")
        info, error = self.pcdl_fetch_release()
        if info is None:
            self.pcdl_signals.pcdl_finished.emit(self.pcdl_core_kind, False, error)
            return
        archive = self.pcdl_paths.prt_temp_dir / info.asset_name
        error = self.pcdl_download(info, archive)
        if error:
            self.pcdl_signals.pcdl_finished.emit(self.pcdl_core_kind, False, error)
            return
        error = self.pcdl_install(archive)
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        if error:
            self.pcdl_signals.pcdl_finished.emit(self.pcdl_core_kind, False, error)
            return
        self.pcdl_signals.pcdl_finished.emit(self.pcdl_core_kind, True, f"{label} {info.version} 已安装到 core 目录")

class PmsCoreDownloadService(QObject):
    """核心下载调度器（单任务，带进度）。"""

    pcdl_progress = Signal(str, int, int, int, str)
    pcdl_finished = Signal(str, bool, str)
    # v1.9.17：整批（队列）跑完的汇总——要不要弹窗, 成功数, 失败数, [(内核代号, 成功, 说明), …]
    pcdl_batch_finished = Signal(bool, int, int, list)

    def __init__(
        self,
        pms_paths: PmsRuntimePaths,
        pms_parent: Optional[QObject] = None,
        pms_settings: Optional["PmsSettingsManager"] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pcdl_paths = pms_paths
        self.pcdl_settings = pms_settings
        self.pcdl_signals = PmsCoreDownloadSignals(self)
        self.pcdl_signals.pcdl_progress.connect(self.pcdl_progress)
        self.pcdl_signals.pcdl_finished.connect(self.pcdl_on_finished)
        self.pcdl_pool = QThreadPool(self)
        self.pcdl_pool.setMaxThreadCount(1)
        self.pcdl_running = False
        # v1.9.11：批量更新队列，当前核心下完后自动开始下一个
        self.pcdl_queue: list[str] = []
        self.pcdl_current_kind: str = ""
        self.pcdl_mirror: str = ""
        self.pcdl_local_proxy_port: int = 0
        self.pcdl_external_proxy: str = ""
        # v1.9.17：整批结果汇总（用户手动发起的批量更新要明确告诉「完成了没有」）
        self.pcdl_batch_results: list[tuple[str, bool, str]] = []
        self.pcdl_batch_total = 0
        self.pcdl_batch_notify = False

    def pcdl_queue_count(self) -> int:
        """队列中剩余待下载的核心数量（不含当前正在跑的）。"""
        return len(self.pcdl_queue)

    def pcdl_queue_kinds(self) -> list[str]:
        """返回队列中剩余的核心代号列表（拷贝）。"""
        return list(self.pcdl_queue)

    def pcdl_is_running(self) -> bool:
        return self.pcdl_running

    def pcdl_start(
        self,
        pms_core_kind: str,
        pms_mirror: str = "",
        pms_local_proxy_port: int = 0,
        pms_external_proxy: str = "",
    ) -> bool:
        """开始下载指定核心（pms_local_proxy_port / pms_external_proxy 走代理退让链路）。

        v1.9.11：支持队列，本函数只负责启动当前这一个；队列的串联在 `pcdl_on_finished` 里做。
        """
        if self.pcdl_running:
            return False
        self.pcdl_running = True
        self.pcdl_current_kind = str(pms_core_kind or "")
        self.pcdl_mirror = str(pms_mirror or "")
        self.pcdl_local_proxy_port = int(pms_local_proxy_port or 0)
        self.pcdl_external_proxy = str(pms_external_proxy or "")
        self.pcdl_pool.start(PmsCoreDownloadTask(
            self.pcdl_signals, self.pcdl_paths, pms_core_kind, pms_mirror,
            pms_local_proxy_port, pms_external_proxy, self.pcdl_settings,
        ))
        return True

    def pcdl_start_list(
        self,
        pms_core_kinds: Sequence[str],
        pms_mirror: str = "",
        pms_local_proxy_port: int = 0,
        pms_external_proxy: str = "",
        pms_notify: bool = False,
    ) -> bool:
        """开始批量下载核心队列（串行执行，不阻塞界面）。

        如果当前已有下载在跑，会把 `pms_core_kinds` 全部追加到队列末尾并返回 False，
        调用方应据此提示用户“已加入队列”。否则启动第一个并返回 True。

        v1.9.17：`pms_notify=True` 表示这批是**用户手动发起**的，跑完要弹汇总
        （自动更新走 `pcdl_start()`，不进汇总、不弹窗，免得定时任务半夜弹框）。
        """
        kinds = [str(kind).strip() for kind in pms_core_kinds if str(kind).strip()]
        if not kinds:
            return False
        if self.pcdl_running:
            self.pcdl_queue.extend(kinds)
            self.pcdl_batch_total += len(kinds)
            self.pcdl_batch_notify = self.pcdl_batch_notify or bool(pms_notify)
            return False
        self.pcdl_batch_results = []
        self.pcdl_batch_total = len(kinds)
        self.pcdl_batch_notify = bool(pms_notify)
        self.pcdl_queue = kinds[1:]
        return self.pcdl_start(
            kinds[0], pms_mirror, pms_local_proxy_port, pms_external_proxy,
        )

    def pcdl_on_finished(self, pms_core_kind: str, pms_ok: bool, pms_message: str) -> None:
        self.pcdl_running = False
        self.pcdl_finished.emit(pms_core_kind, pms_ok, pms_message)
        # v1.9.17：记下这一条的结果；队列空了就发整批汇总
        if self.pcdl_batch_total:
            self.pcdl_batch_results.append((str(pms_core_kind), bool(pms_ok), str(pms_message)))
        # v1.9.11：队列中还有就继续下一个
        if self.pcdl_queue and not self.pcdl_running:
            next_kind = self.pcdl_queue.pop(0)
            self.pcdl_start(
                next_kind,
                self.pcdl_mirror,
                self.pcdl_local_proxy_port,
                self.pcdl_external_proxy,
            )
            return
        if self.pcdl_batch_total and len(self.pcdl_batch_results) >= self.pcdl_batch_total:
            results = list(self.pcdl_batch_results)
            ok_count = sum(1 for item in results if item[1])
            notify = self.pcdl_batch_notify
            self.pcdl_batch_total = 0
            self.pcdl_batch_results = []
            self.pcdl_batch_notify = False
            self.pcdl_batch_finished.emit(notify, ok_count, len(results) - ok_count, results)
