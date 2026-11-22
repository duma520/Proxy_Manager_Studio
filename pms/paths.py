#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行路径解析

开发态与编译态的程序根目录、数据/日志/核心/版本目录与关键文件路径；icon.ico 存在性检测。
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Optional,
    Sequence,
)
import os
import re
import sys
import uuid

from pms.constants import (
    PMS_APP_DOC_NAME,
    PMS_DATABASE_FILENAME,
    PMS_DIR_CONFIGS,
    PMS_DIR_CORE,
    PMS_DIR_DATA,
    PMS_DIR_LOGS,
    PMS_DIR_SCAFFOLD,
    PMS_DIR_TEMP,
    PMS_DIR_VERSION,
    PMS_ENV_FILENAME,
    PMS_ICON_FILENAME,
    PMS_LOG_FILENAME,
    PMS_PAC_FILENAME,
    PMS_CUSTOM_PAC_FILENAME,
    PMS_RULESET_LOCAL_DIR_NAMES,
    PMS_SETTINGS_FILENAME,
    PMS_SOURCE_FILENAME,
)

# --------------------------------------------------------------------------------------
# 路径解析：开发态（.py）与编译态（.exe）都要正确
# --------------------------------------------------------------------------------------
class PmsRuntimePaths:
    """统一解析程序根目录、数据目录、核心目录、版本备份目录等。"""

    def __init__(self, pms_base_override: Optional[Path] = None, pms_core_roots: Optional[Sequence[Path]] = None) -> None:
        if pms_base_override is not None:
            self.prt_root = Path(pms_base_override)
        else:
            self.prt_root = self.prt_detect_root()
        self.prt_extra_core_roots: list[Path] = [Path(item) for item in (pms_core_roots or [])]
        self.prt_root.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 根目录探测
    @staticmethod
    def prt_is_frozen() -> bool:
        """是否运行在编译产物里（Nuitka / PyInstaller 都会设置标志）。"""
        return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()

    @staticmethod
    def prt_root_markers() -> tuple[str, ...]:
        """用于识别“程序根目录”的标志物（只在真正的项目根目录/发布目录里出现）。"""
        return (PMS_SOURCE_FILENAME, PMS_APP_DOC_NAME, PMS_DIR_CORE, PMS_DIR_SCAFFOLD, PMS_ICON_FILENAME, PMS_ENV_FILENAME)

    @staticmethod
    def prt_candidate_roots() -> list[Path]:
        """开发态候选根目录（按可信度排序）：启动脚本所在目录 → 包目录的上一级 → 当前工作目录。"""
        candidates: list[Path] = []
        try:
            script = Path(sys.argv[0]).resolve()
            if script.is_file() and script.suffix.lower() in (".py", ".pyw"):
                candidates.append(script.parent)
        except (OSError, ValueError):
            pass
        candidates.append(Path(__file__).resolve().parent.parent)
        try:
            candidates.append(Path.cwd())
        except (OSError, ValueError):
            pass
        unique: list[Path] = []
        for item in candidates:
            if item not in unique:
                unique.append(item)
        return unique

    @staticmethod
    def prt_is_portable() -> bool:
        """是否便携模式：命令行带 --portable，或程序同目录存在 portable.txt 标记。

        便携模式下数据目录（设置/数据库/日志/核心）跟随 exe，对 U 盘用户友好；
        无论开发态还是编译态，根目录一律取启动脚本/可执行文件所在目录。
        """
        if "--portable" in sys.argv:
            return True
        try:
            exe_dir = Path(sys.executable).resolve().parent if PmsRuntimePaths.prt_is_frozen() else Path(sys.argv[0]).resolve().parent
            if (exe_dir / "portable.txt").is_file():
                return True
        except (OSError, ValueError):
            pass
        return False

    @staticmethod
    def prt_detect_root() -> Path:
        """开发态取“含主程序/文档/core 的目录”；编译态取 .exe 所在目录（保证 data/ 与 exe 同级）。

        为什么需要标志物判断：主程序已拆分为 `pms` 包，`__file__` 指向的是 `pms/`，
        直接取它的父目录会变成包目录；因此按标志物从候选目录里挑真正的程序根目录。
        """
        if PmsRuntimePaths.prt_is_frozen() or PmsRuntimePaths.prt_is_portable():
            # 便携模式：无论是否打包，根目录都跟随启动脚本/可执行文件所在目录。
            try:
                if PmsRuntimePaths.prt_is_frozen():
                    return Path(sys.executable).resolve().parent
                return Path(sys.argv[0]).resolve().parent
            except (OSError, ValueError):
                pass
        markers = PmsRuntimePaths.prt_root_markers()
        for candidate in PmsRuntimePaths.prt_candidate_roots():
            try:
                if any((candidate / marker).exists() for marker in markers):
                    return candidate
            except (OSError, ValueError):
                continue
        return Path(__file__).resolve().parent.parent

    # ---------------------------------------------------------------- 目录
    def prt_dir(self, pms_name: str, pms_create: bool = True) -> Path:
        """返回根目录下的子目录，默认自动创建。"""
        target = self.prt_root / pms_name
        if pms_create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    @property
    def prt_data_dir(self) -> Path:
        return self.prt_dir(PMS_DIR_DATA)

    @property
    def prt_logs_dir(self) -> Path:
        return self.prt_dir(PMS_DIR_LOGS)

    @property
    def prt_core_dir(self) -> Path:
        return self.prt_dir(PMS_DIR_CORE)

    @property
    def prt_version_dir(self) -> Path:
        return self.prt_dir(PMS_DIR_VERSION)

    @property
    def prt_scaffold_dir(self) -> Path:
        return self.prt_dir(PMS_DIR_SCAFFOLD, pms_create=False)

    @property
    def prt_configs_dir(self) -> Path:
        return self.prt_dir(Path(PMS_DIR_DATA) / PMS_DIR_CONFIGS)

    @property
    def prt_temp_dir(self) -> Path:
        return self.prt_dir(Path(PMS_DIR_DATA) / PMS_DIR_TEMP)

    # ---------------------------------------------------------------- 文件
    def prt_file(self, pms_name: str) -> Path:
        return self.prt_root / pms_name

    @property
    def prt_settings_file(self) -> Path:
        return self.prt_file(PMS_SETTINGS_FILENAME)

    @property
    def prt_database_file(self) -> Path:
        return self.prt_file(PMS_DATABASE_FILENAME)

    @property
    def prt_log_file(self) -> Path:
        return self.prt_file(PMS_LOG_FILENAME)

    @property
    def prt_core_record_file(self) -> Path:
        """核心进程归属记录（只用来清理本程序自己启动过的残留核心，避免误杀）。"""
        return self.prt_data_dir / "pms_core_record.json"

    @property
    def prt_pac_file(self) -> Path:
        """系统代理 PAC 脚本路径（v1.9.49：PAC 模式用）。

        放在数据目录下：PAC 由本程序生成、随本程序更新，不该跟着程序根目录走
        （便携模式与编译态下数据目录才是可写的用户数据位置，见 prt_data_dir）。
        只拼路径、不创建文件——创建交给 `PmsWindowsSystemProxy.pwsp_write_pac_file()`。
        """
        return self.prt_data_dir / PMS_PAC_FILENAME

    @property
    def prt_custom_pac_file(self) -> Path:
        """用户手写 PAC 脚本路径（v1.9.62：内置 PAC 编辑器产物文件 pms_custom.pac）。

        与上面的自动 PAC（pms_system_proxy.pac，由 coresys.pwsp_build_pac_text 自动生成）完全独立：
        - 自动 PAC 只支持「直连域名 + 单代理」；
        - 用户 PAC 支持任意 FindProxyForURL 逻辑（小白模式也只能生成相对简单的规则）。
        二者不互相覆盖；切换谁生效由 settings_defaults 的 `system_proxy.pac_source` 决定。
        只拼路径、不创建文件——创建交给 `PmsWindowsSystemProxy.pwsp_write_pac_file()`。
        """
        return self.prt_data_dir / PMS_CUSTOM_PAC_FILENAME

    @property
    def prt_env_file(self) -> Path:
        return self.prt_file(PMS_ENV_FILENAME)

    @property
    def prt_icon_file(self) -> Path:
        return self.prt_file(PMS_ICON_FILENAME)

    @property
    def prt_source_file(self) -> Path:
        return self.prt_file(PMS_SOURCE_FILENAME)

    @property
    def prt_doc_file(self) -> Path:
        return self.prt_file(PMS_APP_DOC_NAME)

    def prt_icon_exists(self) -> bool:
        """icon.ico 是否存在（存在才设置图标，否则保持无图标）。"""
        try:
            return self.prt_icon_file.is_file() and self.prt_icon_file.stat().st_size > 0
        except OSError:
            return False

    def prt_temp_config_file(self, pms_tag: str) -> Path:
        """生成临时核心配置路径（延迟测试 / 测速用）。"""
        safe = re.sub(r"[^0-9A-Za-z_\-]+", "_", str(pms_tag))[:48] or "temp"
        return self.prt_temp_dir / f"{safe}_{uuid.uuid4().hex[:8]}.json"

    def prt_persistent_config_file(self, pms_tag: str) -> Path:
        """生成持久核心配置路径（主核心运行用，可在设置里关闭保留）。"""
        safe = re.sub(r"[^0-9A-Za-z_\-]+", "_", str(pms_tag))[:48] or "config"
        return self.prt_configs_dir / f"{safe}.json"

    def prt_core_search_dirs(self) -> list[Path]:
        """核心可执行文件搜索顺序：额外指定目录 → core 子目录 → core 根 → 程序根 → PATH 目录。"""
        candidates: list[Path] = list(self.prt_extra_core_roots)
        candidates.extend([
            self.prt_core_dir / "xray",
            self.prt_core_dir / "sing-box",
            self.prt_core_dir / "core",
            self.prt_core_dir,
            self.prt_root,
        ])
        for entry in str(os.environ.get("PATH", "")).split(os.pathsep):
            text = entry.strip()
            if not text:
                continue
            try:
                candidates.append(Path(text))
            except (OSError, ValueError):
                continue
        return candidates

    def prt_ruleset_dir(self) -> Path:
        """规则集（.srs）目录：core 下第一个真实存在的候选子目录，都没有时回落到 core 根。

        独立于核心搜索目录，避免把规则文件误当可执行文件。
        """
        for name in PMS_RULESET_LOCAL_DIR_NAMES:
            candidate = self.prt_core_dir / name
            if candidate.is_dir():
                return candidate
        return self.prt_core_dir

    def prt_ruleset_file(self, pms_filename: str) -> Path:
        """取一个规则集文件的绝对路径（只拼路径，不保证存在）。"""
        return self.prt_ruleset_dir() / str(pms_filename)
