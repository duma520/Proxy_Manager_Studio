#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""未命中内核文件的**显式**清理（v1.9.49，审计表第 30 项）

用户要求的原话口径是「加一个『清理未命中内核文件』按钮」。它解决的是这种现场：

    `core\\` 目录里同时躺着好几代同族内核——`mihomo-compatible.exe`（能跑，被程序检测命中）
    与 `mihomo.exe`（跑不起来，18 MB）——程序**只更新检测命中的那一个**，同目录里其它旧文件
    永远不会被清理（程序不做任何自动删除动作，怕删掉用户自己放的东西）。

所以这个模块只提供**「扫描出名单 → 用户确认 → 才删」**的一条链路，没有任何自动触发点。

⚠️ 现状说明（v1.9.45 之后）
-------------------------
`PMS_STANDALONE_EXE_CANDIDATES` 与 `pms/standalone.py` 已随 v1.9.45「独立内核去功能」整块下线，
`core\\` 下现在只剩 `core\\xray\\xray.exe` 与 `core\\sing-box\\sing-box.exe` 两个正式核心。
**因此本机当前扫出来的未命中名单是空的**——这个按钮今天是**防御性**的：等用户哪天手工把
`mihomo.exe` / `xray_old.exe` 之类塞回内核目录，它才有东西可清。

安全边界（改动不得绕过）
------------------------
1. **只扫已知内核落地目录**：`core\\xray` / `core\\sing-box` / `core\\core` / `core` / 程序根。
   目录列表由 `PmsRuntimePaths.prt_core_search_dirs()` 派生，但**只取位于程序目录里的那些**
   ——用户「设置 → 核心」里指定的额外核心目录是用户自己的位置，本模块一律不碰。
   扫描**不递归、不用 `**`**：一层 `iterdir()` 而已。
2. **命中项永不入选**：当前被 `PclCoreLocator.pcl_find_any()` 解析出来的正式核心（`xray.exe` /
   `sing-box.exe`）既按文件名排除、也按解析结果排除（双保险）。
3. **未知文件名永不入选**：`README.md` / `geoip.dat` / `wintune.dll` / `LICENSE` 这类
   名字不像已知内核的一律不进名单，也不在任何删除规则里。
4. **删除只走显式入口** `pms_core_clean_run()`：本模块**不注册任何定时器、不在导入时删东西**，
   也**不接入 `pms/app.py`** 的启动流程（探针里有源码断言钉死这条）。
5. 删不掉的（被占用 / 权限）只记进 `errors`，不抛异常、不中断其它项。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from pms.constants import PMS_CORE_KIND_SINGBOX, PMS_CORE_KIND_XRAY, PMS_LOGGER_NAME

if TYPE_CHECKING:  # 仅类型检查用，避免与核心定位模块循环导入
    from pms.coresys import PmsCoreLocator
    from pms.paths import PmsRuntimePaths

PMS_CORE_CLEAN_LOG_PREFIX = PMS_LOGGER_NAME + ".coreclean"

# 已知内核的**名字前缀**（v1.9.49 基线：两个在用的主核心 + 历史上出现过的独立内核族）。
# 只做前缀匹配，后面允许跟 `-` / `_` / 数字 / 版本 / 架构等尾巴（见 PMS_CORE_CLEAN_NAME_RE）。
PMS_CORE_CLEAN_NAME_PREFIXES: tuple[str, ...] = (
    "xray",
    "sing-box",
    "singbox",
    "mihomo",
    "clash-meta",
    "clash",
    "hysteria2",
    "hysteria",
    "tuic",
    "juicity",
    "anytls",
    "naiveproxy",
    "naive",
    "v2ray",
    "v2fly",
    "sagernet",
    "snell",
    "brook",
    "tun2socks",
    "trojan",
    "shadowsocks",
    "sslocal",
    "ssserver",
    "mieru",
)

# 算得上「内核可执行文件」的扩展名（**不含** .dll / .dat / .md / .txt —— 那些不是内核本体）
PMS_CORE_CLEAN_SUFFIXES: tuple[str, ...] = (".exe", ".bin")

# 允许出现在内核名尾部的「版本 / 架构 / 变体」标记（整段用 `-` / `_` 分隔）
PMS_CORE_CLEAN_TAIL_TOKENS: frozenset[str] = frozenset({
    "compatible", "amd64", "x86", "x64", "x86_64", "arm64", "aarch64", "arm", "386", "32", "64",
    "win", "windows", "linux", "darwin", "macos", "osx", "freebsd", "android",
    "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8",
    "old", "new", "backup", "bak", "copy", "tmp", "test", "debug",
    "beta", "alpha", "rc", "stable", "latest", "prev", "orig",
    "lite", "full", "mini", "small", "nano", "plus", "pro", "gui", "cli",
})
# 尾部允许的「纯数字」（版本号 1 / 19 / 1916 …）与「x.y.z」版本号
PMS_CORE_CLEAN_TAIL_NUMBER_RE = re.compile(r"^\d{1,4}$")
PMS_CORE_CLEAN_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+){1,3}$")


def pms_core_clean_known_name(pms_name: str) -> bool:
    """文件名是不是「已知内核名模式」（`xray.exe` / `mihomo-compatible.exe` / `tuic_v1.0.0.exe`）。

    判定口径（全部用 `-` / `_` / `.` 切段，逐段收）：
        第 1 段必须**恰好是**已知内核前缀；后面每段要么是已知尾巴词，要么是纯数字 / 版本号。
    于是：`mihomo.exe` ✔、`xray_old.exe` ✔、`hysteria2-v2.2.0.exe` ✔、
    而 `README.md` ✘（扩展名与名首都不符合）、`xraygeoip.exe` ✘（第 1 段不是已知前缀）。
    """
    name = str(pms_name or "").strip().lower()
    if not name:
        return False
    stem, dot, suffix = name.rpartition(".")
    if dot:
        if f".{suffix}" not in PMS_CORE_CLEAN_SUFFIXES:
            return False
    else:
        # 没有扩展名（Linux / PATH 里那种裸名字）：照样按已知前缀判，别漏掉
        stem = name
    # 保护 `sing-box.exe` 这种「前缀自带连字符」的写法：先看整名是不是已知前缀
    if stem in PMS_CORE_CLEAN_NAME_PREFIXES:
        return True
    tail = ""
    for prefix in PMS_CORE_CLEAN_NAME_PREFIXES:
        if stem.startswith(prefix + "-") or stem.startswith(prefix + "_"):
            tail = stem[len(prefix):]
            break
    else:
        return False
    segments = [seg for seg in re.split(r"[-_.]+", tail) if seg]
    if not segments:
        return False
    for segment in segments:
        if segment in PMS_CORE_CLEAN_TAIL_TOKENS:
            continue
        if PMS_CORE_CLEAN_TAIL_NUMBER_RE.match(segment) or PMS_CORE_CLEAN_VERSION_RE.match(segment):
            continue
        return False
    return True


def pms_core_clean_dirs(pms_paths: "PmsRuntimePaths") -> list[Path]:
    """该扫哪几个目录（**去重、保序、只留程序目录里的那些**）。

    直接复用 `prt_core_search_dirs()` 的本地候选（额外核心目录是用户自己指定的，剔除），
    再滤掉 PATH 目录与不存在的目录——返回的每个都是**真实存在**的目录。
    """
    root = Path(pms_paths.prt_root).resolve()
    extra = {Path(item).resolve() for item in getattr(pms_paths, "prt_extra_core_roots", []) if item}
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in pms_paths.prt_core_search_dirs():
        try:
            resolved = Path(candidate).resolve()
            resolved.relative_to(root)          # 不在程序目录里 → 抛 ValueError，直接跳过
        except (OSError, ValueError):
            continue
        if resolved in extra:
            continue
        key = str(resolved).lower()
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        result.append(resolved)
    return result


def pms_core_clean_hits(pms_paths: "PmsRuntimePaths", pms_locator: Optional["PmsCoreLocator"] = None) -> set[str]:
    """当前被**检测命中**的核心文件名（小写）——这些名字永不入选清理名单。

    `pcl_find_any()` 给的是「程序真正会去用的那一个」；另外把两个主核心的二进制名
    （`xray.exe` / `sing-box.exe`）无条件加进来，解析器没挂时也一样安全（双保险）。
    """
    hits: set[str] = {"xray.exe", "sing-box.exe"}
    if pms_locator is None:
        return hits
    try:
        found = pms_locator.pcl_find_any(None)
    except Exception as exc:  # noqa: BLE001 - 定位失败不该让清理扫不成，但**不许静默**
        logging.getLogger(PMS_CORE_CLEAN_LOG_PREFIX).warning("检测命中核心失败，只能按文件名兜底排除：%s", exc)
        return hits
    for kind in (PMS_CORE_KIND_XRAY, PMS_CORE_KIND_SINGBOX):
        path = found.get(kind)
        if path is not None:
            hits.add(Path(path).name.lower())
    return hits


def pms_core_clean_plan(
    pms_paths: "PmsRuntimePaths", pms_locator: Optional["PmsCoreLocator"] = None,
) -> list[dict[str, Any]]:
    """列出「现在点清理会删掉哪些」——**只看不删**，界面清单与探针都用它。

    每一项：`{name, path, size, dir}`。空名单是正常结果（当前版本 `core\\` 里就应该是空的）。
    """
    logger = logging.getLogger(PMS_CORE_CLEAN_LOG_PREFIX)
    hits = pms_core_clean_hits(pms_paths, pms_locator)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory in pms_core_clean_dirs(pms_paths):
        try:
            children = sorted(directory.iterdir())   # 一层，**不递归**
        except OSError as exc:
            logger.warning("内核目录读不了，本次跳过：%s（%s）", directory, exc)
            continue
        for path in children:
            try:
                if not path.is_file():
                    continue
                name = path.name
                lowered = name.lower()
                if lowered in hits or lowered in seen:
                    continue
                if not pms_core_clean_known_name(name):
                    continue
                stat = path.stat()
            except OSError as exc:
                logger.warning("内核文件查不了，本次跳过：%s（%s）", path, exc)
                continue
            seen.add(lowered)
            items.append({"name": name, "path": path, "size": int(stat.st_size), "dir": str(directory)})
    items.sort(key=lambda item: (-int(item["size"]), str(item["name"]).lower()))
    return items


def pms_core_clean_run(
    pms_paths: "PmsRuntimePaths", pms_locator: Optional["PmsCoreLocator"] = None,
) -> dict[str, Any]:
    """**按计划真删**（只该由用户点确认后调用），返回 `{files, bytes, freed, errors, names}`。

    删不掉的只记进 `errors`，绝不抛异常——清理失败不该影响程序运行。
    """
    logger = logging.getLogger(PMS_CORE_CLEAN_LOG_PREFIX)
    plan = pms_core_clean_plan(pms_paths, pms_locator)
    result: dict[str, Any] = {"files": 0, "bytes": 0, "freed": "", "errors": [], "names": []}
    for item in plan:
        path = Path(item["path"])
        try:
            path.unlink()
        except OSError as exc:
            result["errors"].append(f"{path.name}：{exc}")
            continue
        result["files"] += 1
        result["bytes"] += int(item["size"])
        result["names"].append(str(item["name"]))
    result["freed"] = pms_core_clean_size_text(result["bytes"])
    if result["files"]:
        logger.info("清理未命中内核文件：删除 %d 个（%s）：%s",
                    result["files"], result["freed"], "、".join(result["names"][:8]))
    if result["errors"]:
        logger.warning("清理未命中内核文件有 %d 项没删掉：%s", len(result["errors"]), "；".join(result["errors"][:3]))
    return result


def pms_core_clean_size_text(pms_bytes: Any) -> str:
    """人类可读体积（清理结果提示用；不引入 utils 依赖，保持本模块可独立测）。"""
    try:
        value = float(pms_bytes or 0)
    except (TypeError, ValueError):
        value = 0.0
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def pms_core_clean_summary_text(pms_result: dict[str, Any]) -> str:
    """把结果拼成一句话（界面与日志共用，避免两处口径不一致）。"""
    files = int(pms_result.get("files") or 0)
    if not files:
        text = "没有可清理的未命中内核文件"
    else:
        text = f"已删除 {files} 个未命中的内核文件，释放 {pms_result.get('freed') or pms_core_clean_size_text(pms_result.get('bytes'))}"
    errors = list(pms_result.get("errors") or [])
    if errors:
        text += f"；{len(errors)} 项没删掉（{errors[0]}）"
    return text


def pms_core_clean_plan_text(pms_plan: list[dict[str, Any]], pms_limit: int = 12) -> str:
    """把名单拼成二次确认框里的明细（逐条给「大小 + 路径」，超出上限只汇总）。"""
    if not pms_plan:
        return "（名单是空的：同目录里没有被检测命中的旧内核文件）"
    lines = [f"· {pms_core_clean_size_text(item['size'])}　{item['path']}" for item in pms_plan[:pms_limit]]
    if len(pms_plan) > pms_limit:
        lines.append(f"…还有 {len(pms_plan) - pms_limit} 个，合计 {pms_core_clean_size_text(sum(int(i['size']) for i in pms_plan))}")
    return "\n".join(lines)


class PmsCoreCleanSignals(QObject):
    """清理任务 → 主线程。"""

    pcc_finished = Signal(dict)


class PmsCoreCleanTask(QRunnable):
    """后台跑一次清理（删文件会碰磁盘，别放在界面线程）。"""

    def __init__(
        self,
        pms_signals: PmsCoreCleanSignals,
        pms_paths: "PmsRuntimePaths",
        pms_locator: Optional["PmsCoreLocator"] = None,
    ) -> None:
        super().__init__()
        self.pcc_signals = pms_signals
        self.pcc_paths = pms_paths
        self.pcc_locator = pms_locator
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        try:
            payload = pms_core_clean_run(self.pcc_paths, self.pcc_locator)
        except Exception as exc:  # noqa: BLE001 - 清理失败绝不该把程序带崩
            logging.getLogger(PMS_CORE_CLEAN_LOG_PREFIX).warning("清理未命中内核文件异常：%s", exc)
            payload = {"files": 0, "bytes": 0, "freed": "", "errors": [str(exc)], "names": []}
        try:
            self.pcc_signals.pcc_finished.emit(payload)
        except RuntimeError:
            # 同 v1.9.34 的口径：程序正在退出时信号对象可能已销毁，
            # emit 会抛 `RuntimeError: Signal source has been deleted`——结果没人要就算了。
            logging.getLogger(PMS_CORE_CLEAN_LOG_PREFIX).debug("清理结果无人接收（程序正在退出），已丢弃")
