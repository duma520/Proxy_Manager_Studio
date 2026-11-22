#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志与临时文件自动清理（v1.9.33）

用户要求：「增加对 日志 / 临时文件自动清理 7 天。」

清理范围（用户拍板选「只清本程序自己产生的垃圾」这一层，最安全）：

    ① 日志轮转件      `pms_runtime.log.1/.2/.3`（**正在写的 `pms_runtime.log` 永不动**）
    ② 日志目录里的旧件 `logs/*`
    ③ 临时文件        `data\\temp\\**`（下载的压缩包、试抓结果、临时配置）
    ④ 核心临时配置     `data\\configs\\*.json`（每次起核心生成一份，运行时那份是新的，按时间躲开）
    ⑤ 旧备份          `*.bak-*`（例如 `pms_store.db.bak-20261010-131617`）
    ⑥ 损坏的设置快照   `*.broken.json`

**不在范围里**（用户明确不选，且动它们风险大）：`version\\` 版本备份、`%TEMP%` 下本程序自己的临时目录、
以及任何非白名单文件。

硬规则（改动不得绕过）
----------------------
· **只按 mtime 判年龄**：保留天数来自设置（默认 7 天，范围 1–365）；
· **白名单匹配才删**：这里从不「清空目录」；`core` / `pms` / `version` / `_scaffold` / 数据库 /
  设置文件 / `.env` 都不在任何规则里；
· **正在写的日志文件永不删**（`pms_runtime.log` 本身不在规则里，只有它的轮转件才匹配）;
· 删不掉的（被占用等）**只记进 errors**，不抛异常、不中断其它项；
· 每次都返回明细（类别 / 路径 / 字节），界面与日志都能看明白清了什么。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from pms.constants import PMS_LOGGER_NAME

if TYPE_CHECKING:  # 仅类型检查用，避免与运行路径模块循环导入
    from pms.paths import PmsRuntimePaths

PMS_MAINTENANCE_DEFAULT_DAYS = 7
PMS_MAINTENANCE_MIN_DAYS = 1
PMS_MAINTENANCE_MAX_DAYS = 365
PMS_MAINTENANCE_LOG_PREFIX = PMS_LOGGER_NAME + ".maintenance"


def pms_maintenance_rules(pms_paths: "PmsRuntimePaths") -> tuple[tuple[str, str, Path], ...]:
    """清理规则：(类别, 通配, 查找根目录)。顺序固定，界面与探针都按它对照。"""
    root = pms_paths.prt_root
    return (
        ("日志轮转件", "pms_runtime.log.*", root),
        ("日志目录旧件", "logs/*", root),
        ("临时文件", "data/temp/**/*", root),
        ("核心临时配置", "data/configs/*.json", root),
        ("旧备份", "*.bak-*", root),
        ("损坏的设置快照", "*.broken.json", root),
    )


def pms_maintenance_days(pms_days: Any) -> int:
    """保留天数校验：非数字或小于下限（0 / 负数）一律回落**默认 7 天**，超上限夹到 365。

    为什么不把 0 夹成 1 天：那等于「把昨天的一切都删掉」，是手改设置文件写坏时最危险的落点；
    回落默认值最保守。上限方向夹到 365（多留不删，同样偏保守）。
    """
    try:
        value = int(pms_days)
    except (TypeError, ValueError):
        return PMS_MAINTENANCE_DEFAULT_DAYS
    if value < PMS_MAINTENANCE_MIN_DAYS:
        return PMS_MAINTENANCE_DEFAULT_DAYS
    return min(PMS_MAINTENANCE_MAX_DAYS, value)


def pms_maintenance_plan(
    pms_paths: "PmsRuntimePaths", pms_days: int = PMS_MAINTENANCE_DEFAULT_DAYS, pms_now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """列出「现在清理会删掉哪些」——**只看不删**，界面预览与探针都用它。

    只收**文件**（`data/temp` 下的空目录另由 `pms_maintenance_run()` 顺手收掉），
    并且严格要求 `mtime < 现在 - 保留天数`。
    """
    cutoff = float(pms_now if pms_now is not None else time.time()) - pms_maintenance_days(pms_days) * 86400.0
    items: list[dict[str, Any]] = []
    for category, pattern, root in pms_maintenance_rules(pms_paths):
        try:
            candidates = sorted(root.glob(pattern))
        except OSError:
            continue
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                continue
            items.append({
                "category": category,
                "path": path,
                "size": int(stat.st_size),
                "modified": float(stat.st_mtime),
            })
    return items


def pms_maintenance_run(
    pms_paths: "PmsRuntimePaths", pms_days: int = PMS_MAINTENANCE_DEFAULT_DAYS, pms_now: Optional[float] = None,
) -> dict[str, Any]:
    """按计划真删，返回 `{files, bytes, dirs, errors, by_category, days}`。

    删不掉（被占用 / 权限）的只记进 `errors`，不抛异常——清理失败绝不该影响程序运行。
    """
    days = pms_maintenance_days(pms_days)
    logger = logging.getLogger(PMS_MAINTENANCE_LOG_PREFIX)
    plan = pms_maintenance_plan(pms_paths, days, pms_now)
    result: dict[str, Any] = {
        "files": 0, "bytes": 0, "dirs": 0, "errors": [], "by_category": {}, "days": days,
    }
    for item in plan:
        category = str(item["category"])
        path = Path(item["path"])
        try:
            path.unlink()
        except OSError as exc:
            result["errors"].append(f"{path.name}：{exc}")
            continue
        result["files"] += 1
        result["bytes"] += int(item["size"])
        bucket = result["by_category"].setdefault(category, {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += int(item["size"])
    # 临时目录里被搬空的老目录也收掉（**只删空目录**，非空的一律留着；按 mtime 一起守保留天数）
    temp_dir = pms_paths.prt_temp_dir
    cutoff = float(pms_now if pms_now is not None else time.time()) - days * 86400.0
    try:
        folders = sorted(
            (child for child in temp_dir.rglob("*") if child.is_dir()),
            key=lambda item: len(item.parts), reverse=True,   # 先深后浅，避免父目录挡住子目录
        )
    except OSError:
        folders = []
    for folder in folders:
        try:
            if any(folder.iterdir()):
                continue
            if folder.stat().st_mtime >= cutoff:
                continue
            folder.rmdir()
            result["dirs"] += 1
        except OSError:
            continue
    if result["files"] or result["dirs"]:
        logger.info(
            "自动清理完成：删除 %d 个文件（%s）、%d 个空目录；保留 %d 天内的东西",
            result["files"], pms_maintenance_size_text(result["bytes"]), result["dirs"], days,
        )
    if result["errors"]:
        logger.warning("自动清理有 %d 项没删掉：%s", len(result["errors"]), "；".join(result["errors"][:3]))
    return result


def pms_maintenance_size_text(pms_bytes: Any) -> str:
    """人类可读体积（清理结果提示用；不引入 utils 的依赖，保持本模块可独立测）。"""
    try:
        value = float(pms_bytes or 0)
    except (TypeError, ValueError):
        value = 0.0
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def pms_maintenance_summary_text(pms_result: dict[str, Any]) -> str:
    """把结果拼成一句话（界面与日志共用，避免两处口径不一致）。"""
    files = int(pms_result.get("files") or 0)
    dirs = int(pms_result.get("dirs") or 0)
    size = pms_maintenance_size_text(pms_result.get("bytes"))
    if not files and not dirs:
        return f"没有超过 {int(pms_result.get('days') or PMS_MAINTENANCE_DEFAULT_DAYS)} 天需要清理的日志 / 临时文件"
    text = f"已清理 {files} 个文件（{size}）"
    if dirs:
        text += f"、{dirs} 个空目录"
    errors = list(pms_result.get("errors") or [])
    if errors:
        text += f"；{len(errors)} 项没删掉（{errors[0]}）"
    return text


class PmsMaintenanceSignals(QObject):
    """清理任务 → 主线程。"""

    pmn_finished = Signal(dict)


class PmsMaintenanceTask(QRunnable):
    """后台跑一次清理（删文件会碰磁盘，别放在界面线程）。"""

    def __init__(
        self,
        pms_signals: PmsMaintenanceSignals,
        pms_paths: "PmsRuntimePaths",
        pms_days: int = PMS_MAINTENANCE_DEFAULT_DAYS,
    ) -> None:
        super().__init__()
        self.pmn_signals = pms_signals
        self.pmn_paths = pms_paths
        self.pmn_days = pms_maintenance_days(pms_days)
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        try:
            payload = pms_maintenance_run(self.pmn_paths, self.pmn_days)
        except Exception as exc:  # noqa: BLE001 - 清理失败绝不该把程序带崩
            logging.getLogger(PMS_MAINTENANCE_LOG_PREFIX).warning("自动清理异常：%s", exc)
            payload = {"files": 0, "bytes": 0, "dirs": 0, "errors": [str(exc)], "by_category": {}, "days": self.pmn_days}
        try:
            self.pmn_signals.pmn_finished.emit(payload)
        except RuntimeError:
            # v1.9.34：程序启动后立刻退出时，窗口（连同这个信号对象）可能已经销毁——
            # 这时 emit 会抛 `RuntimeError: Signal source has been deleted`，
            # Qt 会打印一段「Error calling Python override of QRunnable::run()」。
            # 清理结果没人要就算了，绝不从工作线程往外抛。
            logging.getLogger(PMS_MAINTENANCE_LOG_PREFIX).debug("清理结果无人接收（程序正在退出），已丢弃")
