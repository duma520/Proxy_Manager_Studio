#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本备份与单实例

把源码快照备份进 version\、读取文档版本、单实例守护（QLocalServer）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Optional,
)
from PySide6.QtCore import QObject
from PySide6.QtNetwork import (
    QLocalServer,
    QLocalSocket,
)
import hashlib
import logging
import os
import re
import shutil

from pms.constants import (
    PMS_APP_EN_NAME,
    PMS_APP_VERSION,
    PMS_LOGGER_NAME,
    PMS_VERSION_BACKUP_NOTE_PLACEHOLDER,
)
from pms.utils import (
    pms_now_text,
    pms_read_text_smart,
    pms_slugify_filename,
)
from pms.paths import PmsRuntimePaths


def pms_file_sha256(pms_path: Path, pms_chunk: int = 1 << 20) -> str:
    """计算文件 SHA-256（备份清单用，便于事后核对文件是否被改动）。"""
    digest = hashlib.sha256()
    with open(pms_path, "rb") as handle:
        while True:
            block = handle.read(pms_chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()

# --------------------------------------------------------------------------------------
# 版本备份 / 单实例 / 主题 / 图标 / 二维码
# --------------------------------------------------------------------------------------
class PmsVersionBackupHelper:
    """版本备份助手：把源码快照备份到 version\\ 目录，并校验与文档的一致性。

    主程序已拆分为 `pms/` 包，因此“备份主程序”= 备份启动器 + 整个包目录：
        version\\Proxy_Manager_Studio - 1.1.0 说明\\
            Proxy_Manager_Studio.py
            _backup_manifest.txt   （文件清单 + SHA-256，便于核对）
            pms\\**.py
    若包目录不存在（老的单文件形态），则退化为备份单个 .py 文件。
    """

    def __init__(self, pms_paths: PmsRuntimePaths) -> None:
        self.pvbh_paths = pms_paths
        self.pvbh_logger = logging.getLogger(PMS_LOGGER_NAME + ".version")

    @staticmethod
    def pvbh_package_dir(pms_paths: PmsRuntimePaths) -> Optional[Path]:
        """主程序包目录（pms），不存在时返回 None。"""
        candidate = pms_paths.prt_root / "pms"
        return candidate if candidate.is_dir() else None

    def pvbh_source_files(self) -> list[Path]:
        """需要备份的源文件清单：启动器 + 包内全部 .py（按路径排序）。"""
        files: list[Path] = []
        package = self.pvbh_package_dir(self.pvbh_paths)
        launcher = self.pvbh_paths.prt_source_file
        if package is not None:
            files.extend(sorted(path for path in package.rglob("*.py") if "__pycache__" not in path.parts))
        if launcher.is_file():
            files.insert(0, launcher)
        return files

    def pvbh_backup_source(self, pms_version: str, pms_note: str) -> tuple[bool, str]:
        """把当前源码备份到 version\\ 目录（多文件时为目录快照，单文件时为单文件副本）。"""
        note = pms_slugify_filename(pms_note or PMS_VERSION_BACKUP_NOTE_PLACEHOLDER, PMS_VERSION_BACKUP_NOTE_PLACEHOLDER)
        files = self.pvbh_source_files()
        if not files:
            return False, f"未找到可备份的源码：{self.pvbh_paths.prt_source_file}（编译版请手工备份源码）"
        package = self.pvbh_package_dir(self.pvbh_paths)
        if package is None:
            target_file = self.pvbh_paths.prt_version_dir / f"{PMS_APP_EN_NAME} - {pms_version} {note}.py"
            if target_file.exists():
                target_file = self.pvbh_paths.prt_version_dir / f"{PMS_APP_EN_NAME} - {pms_version} {note} {pms_now_text('%Y%m%d_%H%M%S')}.py"
            try:
                shutil.copy2(files[0], target_file)
            except OSError as exc:
                return False, f"备份失败：{exc}"
            self.pvbh_logger.info("已备份版本（单文件）：%s", target_file.name)
            return True, f"已备份：{target_file.name}"
        target_dir = self.pvbh_paths.prt_version_dir / f"{PMS_APP_EN_NAME} - {pms_version} {note}"
        if target_dir.exists():
            target_dir = self.pvbh_paths.prt_version_dir / f"{PMS_APP_EN_NAME} - {pms_version} {note} {pms_now_text('%Y%m%d_%H%M%S')}"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            root = self.pvbh_paths.prt_root
            manifest: list[str] = [f"源码快照：{PMS_APP_EN_NAME} v{pms_version}（{note}）", f"备份时间：{pms_now_text()}", ""]
            for path in files:
                relative = path.relative_to(root)
                destination = target_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                manifest.append(f"{relative.as_posix()}  {destination.stat().st_size:>8} 字节  SHA256 {pms_file_sha256(path)[:16]}")
            manifest.append("")
            manifest.append(f"文件数：{len(files)}")
            (target_dir / "_backup_manifest.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            return False, f"备份失败：{exc}"
        self.pvbh_logger.info("已备份版本（%d 个源文件）：%s", len(files), target_dir.name)
        return True, f"已备份 {len(files)} 个源文件到：{target_dir.name}\\"

    def pvbh_list_backups(self) -> list[tuple[str, int, str]]:
        """列出备份 (名称, 字节, 修改时间)；目录快照按总字节计。"""
        items: list[tuple[str, int, str]] = []
        directory = self.pvbh_paths.prt_version_dir
        if not directory.is_dir():
            return items
        for path in sorted(directory.iterdir()):
            try:
                if path.is_dir():
                    total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
                    stamp = max((item.stat().st_mtime for item in path.rglob("*") if item.is_file()), default=path.stat().st_mtime)
                    items.append((path.name + os.sep, total, datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")))
                elif path.suffix.lower() in (".py", ".zip"):
                    stat = path.stat()
                    items.append((path.name, stat.st_size, datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")))
            except OSError:
                continue
        return items

    def pvbh_doc_version(self) -> str:
        """从 软件升级迭代记录.md 里读出“当前版本”。"""
        doc = self.pvbh_paths.prt_doc_file
        if not doc.is_file():
            return ""
        for line in pms_read_text_smart(doc).splitlines():
            if "当前版本" in line:
                match = re.search(r"v?(\d+\.\d+\.\d+)", line)
                if match:
                    return match.group(1)
        return ""

    def pvbh_consistency_report(self) -> dict[str, Any]:
        """一致性自检：代码版本 / 文档版本 / 当前版本备份 是否齐备。"""
        doc_version = self.pvbh_doc_version()
        backups = self.pvbh_list_backups()
        has_current = any(f"- {PMS_APP_VERSION} " in name for name, _, _ in backups)
        return {
            "code_version": PMS_APP_VERSION,
            "doc_version": doc_version,
            "doc_exists": self.pvbh_paths.prt_doc_file.is_file(),
            "backup_exists": has_current,
            "backup_count": len(backups),
            "consistent": bool(doc_version == PMS_APP_VERSION and has_current),
        }

class PmsSingleInstanceGuard:
    """单实例守护：靠 QLocalServer 独占命名管道实现，第二次启动会通知已运行实例。"""

    psig_SERVER_NAME = "ProxyManagerStudio.SingleInstance.v1"

    def __init__(self, pms_parent: Optional[QObject] = None) -> None:
        self.psig_server = QLocalServer(pms_parent)
        self.psig_secondary = False
        self.psig_logger = logging.getLogger(PMS_LOGGER_NAME + ".singleinstance")

    def psig_try_acquire(self) -> bool:
        """尝试成为唯一实例；返回 True 表示本进程是主实例。"""
        QLocalServer.removeServer(self.psig_SERVER_NAME)
        if self.psig_server.listen(self.psig_SERVER_NAME):
            return True
        socket = QLocalSocket()
        socket.connectToServer(self.psig_SERVER_NAME)
        if socket.waitForConnected(800):
            socket.write(b"activate\n")
            socket.flush()
            socket.waitForBytesWritten(600)
        socket.abort()
        self.psig_secondary = True
        self.psig_logger.info("检测到已运行实例，本次启动退出")
        return False

    def psig_release(self) -> None:
        try:
            self.psig_server.close()
            QLocalServer.removeServer(self.psig_SERVER_NAME)
        except Exception:  # noqa: BLE001
            pass
