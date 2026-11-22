#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志总线

同时输出到轮转文件、内存环形缓冲与界面信号；界面日志面板与导出都从这里取数。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional
from PySide6.QtCore import (
    QObject,
    Signal,
)
import logging
import sys
import threading
import traceback

from pms.constants import (
    PMS_LOGGER_NAME,
    PMS_LOG_LEVEL_ORDER,
)
from pms.utils import pms_atomic_write_text, pms_safe_int

# v1.8.0：错误面板只收这几个级别（「只收 error」= error 及其以上，FATAL 更严重没理由漏掉）。
# 级别文本来自 logging 的 record.levelname，是**大写**，这里统一大写比较。
PMS_LOG_ERROR_LEVELS = ("ERROR", "CRITICAL", "FATAL")

# 错误面板最多留多少条（和主日志分开限流，避免报错风暴把面板刷爆）
PMS_ERROR_LOG_MAX = 200

# v1.9.16：日志文件轮转大小（MB）的兜底默认值——真正的值来自设置项
# `advanced.max_log_file_mb`（窗口启动时通过 plb_set_max_file_mb() 灌进来）。
# 写成模块私有常量而不是放到 constants.py：那边已顶在 1000 行红线。
_PLB_MAX_FILE_MB_DEFAULT = 8

class PmsLogBus(QObject):
    """日志总线：把 logging 记录同时送到文件、内存环形缓冲与界面。"""

    plb_record_emitted = Signal(str, str, str, str)  # 时间 / 级别 / 来源 / 正文

    def __init__(self, pms_log_file: Path, pms_max_lines: int = 2000, pms_parent: Optional[QObject] = None) -> None:
        super().__init__(pms_parent)
        self.plb_log_file = Path(pms_log_file)
        self.plb_max_lines = max(100, int(pms_max_lines))
        # v1.9.16：文件轮转大小改成可设置（设置 → 高级 → 单个日志文件大小，MB）
        self.plb_max_file_mb = _PLB_MAX_FILE_MB_DEFAULT
        self.plb_buffer: list[tuple[str, str, str, str]] = []
        # v1.8.0：与主缓冲分开的「错误专用」环形缓冲，喂给底部「错误」页签与状态栏红点
        self.plb_error_buffer: list[tuple[str, str, str, str]] = []
        self.plb_error_max_lines = PMS_ERROR_LOG_MAX
        self.plb_lock = threading.RLock()
        self.plb_handler: Optional[logging.Handler] = None
        self.plb_file_handler: Optional[logging.Handler] = None
        self.plb_console_handler: Optional[logging.Handler] = None
        self.plb_logger = logging.getLogger(PMS_LOGGER_NAME)
        self.plb_install()

    def plb_install(self) -> None:
        """安装根日志器：文件（轮转）+ 内存缓冲 + 控制台。"""
        self.plb_logger.setLevel(logging.DEBUG)
        self.plb_logger.propagate = False
        for handler in list(self.plb_logger.handlers):
            self.plb_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
        self.plb_log_file.parent.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            str(self.plb_log_file),
            maxBytes=max(1, int(self.plb_max_file_mb)) * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        file_handler.setLevel(logging.DEBUG)
        self.plb_file_handler = file_handler
        console_handler = logging.StreamHandler(stream=sys.stdout)
        console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
        console_handler.setLevel(logging.INFO)
        self.plb_console_handler = console_handler
        self.plb_logger.addHandler(file_handler)
        self.plb_logger.addHandler(console_handler)
        self.plb_logger.info("日志总线就绪：%s", self.plb_log_file)

    def plb_attach_bus_handler(self, pms_level: str = "debug", pms_max_lines: Optional[int] = None) -> None:
        """挂载内存缓冲处理器（界面日志面板的数据源）。"""
        if pms_max_lines is not None:
            self.plb_max_lines = max(100, int(pms_max_lines))
        if self.plb_handler is not None:
            self.plb_set_level(pms_level)
            return
        bus_handler = _PmsLogBusHandler(self)
        bus_handler.setLevel(logging.DEBUG)
        bus_handler.setFormatter(logging.Formatter("%(message)s"))
        self.plb_handler = bus_handler
        self.plb_logger.addHandler(bus_handler)
        self.plb_set_level(pms_level)

    def plb_set_max_file_mb(self, pms_mb: int) -> None:
        """改日志文件的轮转大小（MB），立即生效（v1.9.16）。

        **只替换文件处理器**：`plb_install()` 会摘掉所有处理器（连内存缓冲的那个一起），
        直接重装会让界面日志面板收不到新日志（`plb_handler` 引用还在，`plb_attach_bus_handler()`
        会以为已挂好而不再挂）。所以这里单独换掉文件处理器。
        """
        size_mb = max(1, int(pms_safe_int(pms_mb, _PLB_MAX_FILE_MB_DEFAULT, 1, 512)))
        if size_mb == self.plb_max_file_mb and self.plb_file_handler is not None:
            return
        self.plb_max_file_mb = size_mb
        old_handler = self.plb_file_handler
        if old_handler is not None:
            try:
                self.plb_logger.removeHandler(old_handler)
                old_handler.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            from logging.handlers import RotatingFileHandler

            self.plb_log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                str(self.plb_log_file), maxBytes=size_mb * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
            ))
            file_handler.setLevel(logging.DEBUG)
            self.plb_file_handler = file_handler
            self.plb_logger.addHandler(file_handler)
        except OSError as exc:
            self.plb_file_handler = None
            self.plb_logger.warning("日志文件大小设置失败：%s", exc)

    def plb_set_level(self, pms_level: str) -> None:
        """设置界面捕获级别（同时调整文件日志级别）。"""
        level = PMS_LOG_LEVEL_ORDER.get(str(pms_level), 20)
        if self.plb_handler is not None:
            self.plb_handler.setLevel(level)
        if self.plb_file_handler is not None:
            self.plb_file_handler.setLevel(logging.DEBUG if level <= 10 else level)

    def plb_set_max_lines(self, pms_max_lines: int) -> None:
        with self.plb_lock:
            self.plb_max_lines = max(100, int(pms_max_lines))
            if len(self.plb_buffer) > self.plb_max_lines:
                self.plb_buffer = self.plb_buffer[-self.plb_max_lines:]

    def plb_append(self, pms_time_text: str, pms_level: str, pms_source: str, pms_message: str) -> None:
        """线程安全地追加一条记录并广播。

        v1.8.0：error 级别的**额外**进一份错误缓冲，供「错误」页签与状态栏红点使用。
        """
        entry = (pms_time_text, pms_level, pms_source, pms_message)
        is_error = str(pms_level).strip().upper() in PMS_LOG_ERROR_LEVELS
        with self.plb_lock:
            self.plb_buffer.append(entry)
            if len(self.plb_buffer) > self.plb_max_lines:
                del self.plb_buffer[: len(self.plb_buffer) - self.plb_max_lines]
            if is_error:
                self.plb_error_buffer.append(entry)
                if len(self.plb_error_buffer) > self.plb_error_max_lines:
                    del self.plb_error_buffer[: len(self.plb_error_buffer) - self.plb_error_max_lines]
        self.plb_record_emitted.emit(*entry)

    def plb_snapshot(self) -> list[tuple[str, str, str, str]]:
        with self.plb_lock:
            return list(self.plb_buffer)

    # ------------------------------------------------------------------ 错误专用（v1.8.0）
    def plb_error_snapshot(self) -> list[tuple[str, str, str, str]]:
        """最近的错误记录（只含 error 及以上，最多 ``PMS_ERROR_LOG_MAX`` 条）。"""
        with self.plb_lock:
            return list(self.plb_error_buffer)

    def plb_error_count(self) -> int:
        with self.plb_lock:
            return len(self.plb_error_buffer)

    def plb_error_clear(self) -> None:
        """只清错误面板（主日志不受影响）。"""
        with self.plb_lock:
            self.plb_error_buffer.clear()

    def plb_error_export(self, pms_target: Path) -> int:
        """把错误记录导出成文本，返回写入行数。"""
        lines = [f"{item[0]} [{item[1]}] {item[2]}: {item[3]}" for item in self.plb_error_snapshot()]
        if pms_atomic_write_text(Path(pms_target), "\n".join(lines) + "\n"):
            return len(lines)
        return -1

    def plb_clear(self) -> None:
        with self.plb_lock:
            self.plb_buffer.clear()
            self.plb_error_buffer.clear()

    def plb_export(self, pms_target: Path) -> int:
        """把内存日志导出成文本，返回写入行数。"""
        lines = [f"{item[0]} [{item[1]}] {item[2]}: {item[3]}" for item in self.plb_snapshot()]
        if pms_atomic_write_text(Path(pms_target), "\n".join(lines) + "\n"):
            return len(lines)
        return -1

    def plb_shutdown(self) -> None:
        """关闭日志处理器（退出流程调用，确保文件句柄释放）。"""
        for handler in list(self.plb_logger.handlers):
            self.plb_logger.removeHandler(handler)
            try:
                handler.flush()
                handler.close()
            except Exception:  # noqa: BLE001
                pass
        self.plb_handler = None

class _PmsLogBusHandler(logging.Handler):
    """logging.Handler 包装：把记录转成信号（跨线程安全，Qt 信号自动排队）。"""

    def __init__(self, pms_bus: PmsLogBus) -> None:
        super().__init__()
        self.pms_bus = pms_bus

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + "".join(traceback.format_exception(*record.exc_info)).strip()
            time_text = datetime.fromtimestamp(record.created).strftime("%H:%M:%S") + f".{int(record.msecs):03d}"
            self.pms_bus.plb_append(time_text, record.levelname, record.name, message)
        except Exception:  # noqa: BLE001 - 日志自身绝不能抛出
            pass
