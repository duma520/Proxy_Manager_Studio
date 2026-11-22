#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话框：关于

版本、核心、目录、依赖、文档一致性与数据库统计。
"""

from __future__ import annotations

from typing import Optional
from PySide6.QtCore import (
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
import platform
import sys

from pms.constants import (
    PMS_APP_BUILD_DATE,
    PMS_APP_NAME,
    PMS_APP_VERSION,
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
)
from pms.utils import pms_format_bytes
from pms.paths import PmsRuntimePaths
from pms.envstore import PmsEnvStore
from pms.database import PmsNodeRepository
from pms.coresys import (
    PmsCoreLocator,
    PmsPrivilegeUtils,
)
from pms.versioning import PmsVersionBackupHelper
from pms.gui.dialogs.settingsdialog import PmsSettingsDialog

# --------------------------------------------------------------------------------------
# 对话框：关于（版本与环境）
# --------------------------------------------------------------------------------------
class PmsAboutDialog(QDialog):
    """关于：版本、核心、目录、依赖、文档一致性、数据库统计。"""

    pabd_action = Signal(str, dict)

    def __init__(
        self,
        pms_paths: PmsRuntimePaths,
        pms_settings: "PmsSettingsManager",
        pms_env: PmsEnvStore,
        pms_locator: PmsCoreLocator,
        pms_repo: PmsNodeRepository,
        pms_backup: PmsVersionBackupHelper,
        pms_parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pabd_paths = pms_paths
        self.pabd_settings = pms_settings
        self.pabd_env = pms_env
        self.pabd_locator = pms_locator
        self.pabd_repo = pms_repo
        self.pabd_backup = pms_backup
        self.setWindowTitle("关于 " + PMS_APP_NAME)
        self.setMinimumSize(760, 620)
        self.pabd_build_ui()

    def pabd_build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        title = QLabel(f"<h2>{PMS_APP_NAME}</h2><p>版本 v{PMS_APP_VERSION}　构建日期 {PMS_APP_BUILD_DATE}<br>"
                       f"类似 NekoBox / V2RayN 的代理节点管理器：节点管理 · 订阅更新 · 双核心驱动 · 系统代理 · TUN · 真实测速</p>")
        title.setWordWrap(True)
        root.addWidget(title)
        self.pabd_tabs = QTabWidget()
        root.addWidget(self.pabd_tabs, 1)
        self.pabd_tabs.addTab(self.pabd_build_runtime_tab(), "运行环境")
        self.pabd_tabs.addTab(self.pabd_build_doc_tab(), "版本与文档")
        self.pabd_tabs.addTab(self.pabd_build_dataset_tab(), "数据统计")
        buttons = QHBoxLayout()
        backup_button = QPushButton("备份当前版本")
        backup_button.clicked.connect(lambda: self.pabd_action.emit("backup_version", {"note": "关于页手动备份"}))
        doc_button = QPushButton("打开迭代记录")
        doc_button.clicked.connect(lambda: self.pabd_action.emit("open_document", {}))
        folder_button = QPushButton("打开程序目录")
        folder_button.clicked.connect(lambda: self.pabd_action.emit("open_folder", {"path": str(self.pabd_paths.prt_root)}))
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(backup_button)
        buttons.addWidget(doc_button)
        buttons.addWidget(folder_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

    def pabd_build_runtime_tab(self) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        cores = self.pabd_locator.pcl_find_any(self.pabd_settings)
        core_lines = []
        for kind, title in ((PMS_CORE_KIND_SINGBOX, "sing-box"), (PMS_CORE_KIND_XRAY, "Xray")):
            path = cores.get(kind)
            if path is None:
                core_lines.append(f"{title}：未找到（设置 → 核心 → 下载 / 更新）")
            else:
                core_lines.append(f"{title}：{self.pabd_locator.pcl_detect_version(path) or '未知版本'}\n    {path}")
        text = [
            f"Python：{platform.python_version()}",
            f"解释器：{sys.executable}",
            f"PySide6：{PmsSettingsDialog.pset_pyside_version()}",
            f"操作系统：{platform.platform()}",
            f"管理员权限：{'是' if PmsPrivilegeUtils.ppu_is_admin() else '否（TUN 需要）'}",
            "",
            "核心：",
            "  " + "\n  ".join(core_lines),
            "",
            f"配置目录：{self.pabd_paths.prt_configs_dir}",
            f"临时目录：{self.pabd_paths.prt_temp_dir}",
            f"日志文件：{self.pabd_paths.prt_log_file}",
            f"设置文件：{self.pabd_paths.prt_settings_file}",
        ]
        label = QLabel("\n".join(text))
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(1)
        area.setWidget(container)
        return area

    def pabd_build_doc_tab(self) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        report = self.pabd_backup.pvbh_consistency_report()
        status = QLabel(
            "文档与版本一致性："
            + ("一致 ✅" if report["consistent"] else "需要同步 ⚠")
            + f"\n代码版本 v{report['code_version']}　文档版本 "
            + (f"v{report['doc_version']}" if report["doc_version"] else "未读到（文档缺失？）")
            + f"\n当前版本备份：{'已存在' if report['backup_exists'] else '缺失'}　历史备份总数：{report['backup_count']}"
        )
        status.setWordWrap(True)
        layout.addWidget(status)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["备份文件", "大小", "修改时间"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        backups = self.pabd_backup.pvbh_list_backups()
        table.setRowCount(len(backups))
        for row, (name, size, mtime) in enumerate(reversed(backups)):
            table.setItem(row, 0, QTableWidgetItem(name))
            table.setItem(row, 1, QTableWidgetItem(pms_format_bytes(size)))
            table.setItem(row, 2, QTableWidgetItem(mtime))
        layout.addWidget(table, 1)
        area.setWidget(container)
        return area

    def pabd_build_dataset_tab(self) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        stats = self.pabd_repo.pnr_engine.pdb_stats()
        groups = self.pabd_repo.pnr_group_summary()
        group_lines = "\n".join(f"  {name}：{count} 个节点" for name, count in groups[:30]) or "  （暂无分组）"
        env_snapshot = self.pabd_env.pes_redacted_snapshot()
        env_lines = "\n".join(f"  {key} = {value}" for key, value in env_snapshot.items()) or "  （.env 不存在或为空）"
        text = [
            f"数据库文件：{self.pabd_repo.pnr_engine.pdb_path}",
            f"文件大小：{pms_format_bytes(stats.get('file_bytes', 0))}",
            f"节点：{stats.get('pms_nodes', 0)}　分组：{stats.get('pms_groups', 0)}　订阅：{stats.get('pms_subscriptions', 0)}",
            f"延迟历史：{stats.get('pms_latency_history', 0)} 条　连接历史：{stats.get('pms_connection_history', 0)} 条　流量采样：{stats.get('pms_traffic_samples', 0)} 条",
            f"完整性检查：{self.pabd_repo.pnr_engine.pdb_integrity_check()}",
            "",
            "分组分布：",
            group_lines,
            "",
            f".env（{self.pabd_paths.prt_env_file}）：",
            env_lines,
        ]
        label = QLabel("\n".join(text))
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(1)
        area.setWidget(container)
        return area
