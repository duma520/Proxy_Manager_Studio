#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置对话框的「维护与关于」页（数据库 / 高级 / 接口 / 统计 / 自动切换 / 关于 + 导入导出复位）——v1.9.42 从 `pms/gui/dialogs/settingsdialog.py` 拆出的私有 mixin。

* 私有类名（`_` 开头）：不进启动器再导出，外部引用一个字都不用改；
* 方法体与 `self.pset_*` / `self.plt_*` 属性名原样保留；
* **不许**反向 import 源模块（否则循环导入）。
"""
from __future__ import annotations

import platform
import sys
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)
from pathlib import Path
from pms.constants import PMS_APP_BUILD_DATE, PMS_APP_NAME, PMS_APP_VERSION
from pms.gui.dialogs.apiwidget import PmsApiSettingsWidget
from pms.gui.dialogs.autoswitchwidget import PmsAutoSwitchSettingsWidget
from pms.gui.dialogs.maintenancewidget import _pmw_build
from pms.gui.dialogs.statswidget import PmsStatsSettingsWidget
from pms.gui.widgets import pms_make_hint_label
from pms.optional import PMS_HAS_DOTENV, PMS_HAS_PSUTIL, PMS_HAS_QRCODE, PMS_HAS_YAML
from pms.utils import pms_format_bytes
from typing import Any


class _PsetTabsMixin:
    """设置对话框的「维护与关于」页（数据库 / 高级 / 接口 / 统计 / 自动切换 / 关于 + 导入导出复位）（17 个方法，v1.9.42 从 `settingsdialog.py` 拆出）。

    ★ 搬出来只是为了给 `settingsdialog.py` 腾红线余量（方法名、self.pset_* 属性名一个字没改）。
    """

    # ------------------------------------------------------------------ 数据库
    def pset_build_database_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        group = QGroupBox("存储与维护")
        form = QFormLayout(group)
        self.pset_chk_vacuum = self.pset_bind_bool("database.auto_vacuum", QCheckBox("退出时整理数据库（VACUUM）"))
        self.pset_chk_keep_latency = self.pset_bind_bool("database.keep_latency_history", QCheckBox("保留延迟历史"))
        self.pset_spin_latency_days = self.pset_bind_int("database.latency_history_days", QSpinBox(), (1, 3650))
        self.pset_chk_keep_conn = self.pset_bind_bool("database.keep_connection_history", QCheckBox("保留连接历史"))
        self.pset_spin_conn_days = self.pset_bind_int("database.connection_history_days", QSpinBox(), (1, 3650))
        self.pset_spin_conn_rows = self.pset_bind_int("database.max_connection_rows", QSpinBox(), (50, 5000))
        self.pset_chk_backup_exit = self.pset_bind_bool("database.backup_on_exit", QCheckBox("退出时备份数据库"))
        self.pset_backup_dir_edit = self.pset_bind_text("database.backup_dir", QLineEdit())
        form.addRow(self.pset_chk_vacuum)
        form.addRow(self.pset_chk_keep_latency)
        form.addRow("延迟历史保留天数", self.pset_spin_latency_days)
        form.addRow(self.pset_chk_keep_conn)
        form.addRow("连接历史保留天数", self.pset_spin_conn_days)
        form.addRow("连接面板最大行数", self.pset_spin_conn_rows)
        form.addRow(self.pset_chk_backup_exit)
        backup_row = QHBoxLayout()
        browse_backup = QPushButton("选择备份目录…")
        browse_backup.clicked.connect(self.pset_browse_backup_dir)
        backup_row.addWidget(self.pset_backup_dir_edit, 1)
        backup_row.addWidget(browse_backup)
        form.addRow("备份目录", backup_row)
        action_row = QHBoxLayout()
        for title, action in (("立即备份数据库", "backup_database"), ("立即整理（VACUUM）", "vacuum_database"),
                              ("清理过期历史", "prune_history"), ("完整性检查", "integrity_check"), ("导出全部节点", "export_all")):
            button = QPushButton(title)
            button.clicked.connect(lambda _=False, name=action: self.psed_action.emit(name, {}))
            action_row.addWidget(button)
        action_row.addStretch(1)
        form.addRow("维护操作", action_row)
        self.pset_db_status_label = QLabel("")
        self.pset_db_status_label.setWordWrap(True)
        form.addRow("数据库信息", self.pset_db_status_label)
        layout.addWidget(group)
        layout.addWidget(pms_make_hint_label(
            "说明：节点、分组、订阅、延迟历史、流量采样都保存在本地 SQLite（WAL 模式）。"
            "若在 .env 中配置了数据库密码，节点凭据字段会先加密再落盘。"
        ))
        layout.addStretch(1)
        return area

    def pset_browse_backup_dir(self) -> None:
        target = QFileDialog.getExistingDirectory(self, "选择备份目录", str(self.pset_paths.prt_data_dir))
        if target:
            self.pset_backup_dir_edit.setText(target)

    def pset_refresh_database_status(self) -> None:
        """显示数据库统计与完整性。"""
        try:
            stats = self.pset_repo.pnr_engine.pdb_stats()
        except Exception as exc:  # noqa: BLE001
            self.pset_db_status_label.setText(f"读取失败：{exc}")
            return
        self.pset_db_status_label.setText(
            f"文件：{self.pset_repo.pnr_engine.pdb_path}（{pms_format_bytes(stats.get('file_bytes', 0))}）\n"
            f"节点 {stats.get('pms_nodes', 0)} 个，分组 {stats.get('pms_groups', 0)} 个，订阅 {stats.get('pms_subscriptions', 0)} 个，"
            f"延迟历史 {stats.get('pms_latency_history', 0)} 条，连接历史 {stats.get('pms_connection_history', 0)} 条"
        )

    # ------------------------------------------------------------------ 统计
    def pset_build_stats_tab(self) -> QWidget:  # 控件全在 statswidget.py（v1.6.1 迁出，给本文件腾行数）
        area, _container, layout = self.pset_make_scroll()
        layout.addWidget(PmsStatsSettingsWidget(self.pset_settings, self))
        return area

    def pset_build_autoswitch_tab(self) -> QWidget:  # 控件全在 autoswitchwidget.py（本文件已在红线边上）
        area, _container, layout = self.pset_make_scroll()
        widget = PmsAutoSwitchSettingsWidget(self.pset_settings, self)
        widget.paww_action.connect(self.psed_action)
        layout.addWidget(widget)
        return area

    # ------------------------------------------------------------------ 高级
    def pset_build_advanced_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        group = QGroupBox("高级选项")
        form = QFormLayout(group)
        self.pset_chk_debug = self.pset_bind_bool("advanced.debug_mode", QCheckBox("调试模式（界面日志显示 DEBUG 级别）"))
        self.pset_chk_clipboard = self.pset_bind_bool("advanced.clipboard_watch", QCheckBox("监听剪贴板中的分享链接并提示导入"))
        self.pset_spin_logfile = self.pset_bind_int("advanced.max_log_file_mb", QSpinBox(), (1, 512))
        self.pset_chk_dedupe_addr = self.pset_bind_bool("advanced.dedup_by_server_port", QCheckBox("按「协议+服务器+端口」宽松去重"))
        self.pset_chk_skip_invalid = self.pset_bind_bool("advanced.import_skip_invalid", QCheckBox("导入时跳过无效节点"))
        self.pset_chk_import_report = self.pset_bind_bool("advanced.show_import_report", QCheckBox("导入后显示详细报告"))
        form.addRow(self.pset_chk_debug); form.addRow(self.pset_chk_clipboard)
        form.addRow("日志文件上限（MB）", self.pset_spin_logfile)
        form.addRow(self.pset_chk_dedupe_addr); form.addRow(self.pset_chk_skip_invalid); form.addRow(self.pset_chk_import_report)
        layout.addWidget(group)
        self.pset_maintenance_widget = _pmw_build(self); layout.addWidget(self.pset_maintenance_widget)   # v1.9.33：日志/临时文件自动清理
        config_group = QGroupBox("设置文件")
        config_layout = QHBoxLayout(config_group)
        export_button = QPushButton("导出设置…")
        export_button.clicked.connect(self.pset_export_settings)
        import_button = QPushButton("导入设置…")
        import_button.clicked.connect(self.pset_import_settings)
        reload_button = QPushButton("重新载入磁盘设置")
        reload_button.clicked.connect(self.pset_reload_settings)
        reset_button = QPushButton("恢复全部默认")
        reset_button.setObjectName("dangerButton")
        reset_button.clicked.connect(self.pset_reset_all)
        for widget in (export_button, import_button, reload_button, reset_button):
            config_layout.addWidget(widget)
        config_layout.addStretch(1)
        layout.addWidget(config_group)
        layout.addWidget(pms_make_hint_label(f"设置文件位置：{self.pset_paths.prt_settings_file}（改动 250ms 防抖后自动保存）"))
        layout.addStretch(1)
        return area

    def pset_build_api_tab(self) -> QWidget:  # 控件全在 apiwidget.py（本文件已在红线边上，不再往里塞）
        area, _container, layout = self.pset_make_scroll()
        widget = PmsApiSettingsWidget(self.pset_settings, self.pset_env, self)
        widget.psaw_action.connect(self.psed_action)
        layout.addWidget(widget)
        return area

    def pset_export_settings(self) -> None:
        target, _ = QFileDialog.getSaveFileName(self, "导出设置", "pms_settings_export.json", "JSON (*.json)")
        if not target:
            return
        if self.pset_settings.psm_export_to_file(Path(target)):
            QMessageBox.information(self, "已导出", target)
        else:
            QMessageBox.warning(self, "导出失败", target)

    def pset_import_settings(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, "导入设置", "", "JSON (*.json)")
        if not source:
            return
        ok, message = self.pset_settings.psm_import_from_file(Path(source))
        if ok:
            QMessageBox.information(self, "导入完成", message + "\n部分界面项需要重开窗口才会同步。")
        else:
            QMessageBox.warning(self, "导入失败", message)

    def pset_reload_settings(self) -> None:
        self.pset_settings.psm_reload()
        QMessageBox.information(self, "已重新载入", "磁盘设置已重新加载；为保证一致，建议重启程序后生效。")

    def pset_reset_all(self) -> None:
        if QMessageBox.question(self, "确认", "确定把所有设置恢复默认值吗？") == QMessageBox.StandardButton.Yes:
            self.pset_settings.psm_reset_all()
            QMessageBox.information(self, "已恢复", "全部设置已恢复默认值，请重启程序以获得最佳效果。")

    # ------------------------------------------------------------------ 版本与环境
    def pset_build_about_tab(self) -> QWidget:
        area, _container, layout = self.pset_make_scroll()
        info_group = QGroupBox("版本信息")
        info_form = QFormLayout(info_group)
        self.pset_version_label = QLabel(f"{PMS_APP_NAME} v{PMS_APP_VERSION}（构建日期 {PMS_APP_BUILD_DATE}）")
        self.pset_doc_label = QLabel("")
        self.pset_env_label = QLabel("")
        self.pset_env_label.setWordWrap(True)
        info_form.addRow("程序版本", self.pset_version_label)
        info_form.addRow("文档一致性", self.pset_doc_label)
        info_form.addRow(".env 状态", self.pset_env_label)
        layout.addWidget(info_group)

        path_group = QGroupBox("目录")
        path_layout = QGridLayout(path_group)
        rows = [
            ("程序根目录", self.pset_paths.prt_root),
            ("数据目录", self.pset_paths.prt_data_dir),
            ("日志目录", self.pset_paths.prt_logs_dir),
            ("核心目录", self.pset_paths.prt_core_dir),
            ("版本备份", self.pset_paths.prt_version_dir),
            ("脚手架目录", self.pset_paths.prt_scaffold_dir),
        ]
        for index, (title, path) in enumerate(rows):
            label = QLabel(str(path))
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            button = QPushButton("打开")
            button.clicked.connect(lambda _=False, target=path: self.psed_action.emit("open_folder", {"path": str(target)}))
            path_layout.addWidget(QLabel(title), index, 0)
            path_layout.addWidget(label, index, 1)
            path_layout.addWidget(button, index, 2)
        layout.addWidget(path_group)

        env_group = QGroupBox("依赖与环境")
        env_form = QFormLayout(env_group)
        dependencies = [
            f"Python：{platform.python_version()}（{sys.executable}）",
            f"PySide6：{self.pset_pyside_version()}",
            f"requests：{'可用' if self.pset_module_available('requests') else '缺失（使用标准库替代）'}",
            f"python-dotenv：{'可用' if PMS_HAS_DOTENV else '缺失（使用内置 .env 解析）'}",
            f"qrcode：{'可用' if PMS_HAS_QRCODE else '缺失（二维码功能不可用）'}",
            f"PyYAML：{'可用' if PMS_HAS_YAML else '缺失（无法导入 Clash YAML）'}",
            f"psutil：{'可用' if PMS_HAS_PSUTIL else '缺失（残留进程清理退化为 taskkill）'}",
            f"系统：{platform.platform()}",
        ]
        env_form.addRow(QLabel("\n".join(dependencies)))
        layout.addWidget(env_group)

        backup_group = QGroupBox("版本备份")
        backup_layout = QHBoxLayout(backup_group)
        backup_button = QPushButton("备份当前主程序到 version\\")
        backup_button.clicked.connect(lambda: self.psed_action.emit("backup_version", {"note": "手动备份"}))
        backup_layout.addWidget(backup_button)
        backup_layout.addStretch(1)
        layout.addWidget(backup_group)
        layout.addStretch(1)
        return area

    @staticmethod
    def pset_pyside_version() -> str:
        try:
            import PySide6  # type: ignore

            return str(getattr(PySide6, "__version__", "未知"))
        except Exception:  # noqa: BLE001
            return "未知"

    @staticmethod
    def pset_module_available(pms_name: str) -> bool:
        import importlib.util as pms_importlib_util

        try:
            return pms_importlib_util.find_spec(pms_name) is not None
        except (ImportError, ValueError):
            return False

    def pset_refresh_env_status(self) -> None:
        """显示 .env 读到的配置项（脱敏）。"""
        snapshot = self.pset_env.pes_redacted_snapshot()
        if not snapshot:
            self.pset_env_label.setText(
                f"未找到 {self.pset_paths.prt_env_file}（API Key / 数据库密码将只从系统环境变量读取）"
            )
            return
        lines = [f"{key} = {value}" for key, value in snapshot.items()]
        self.pset_env_label.setText("\n".join(lines))

    def pset_refresh_doc_status(self, pms_report: dict[str, Any]) -> None:
        """由主窗口回填文档一致性结果。"""
        if pms_report.get("consistent"):
            self.pset_doc_label.setText(f"一致（代码 v{pms_report.get('code_version')}，文档 v{pms_report.get('doc_version')}，备份已存在）")
            self.pset_doc_label.setObjectName("okLabel")
        else:
            self.pset_doc_label.setText(
                f"需要同步：代码 v{pms_report.get('code_version')}，文档 v{pms_report.get('doc_version') or '未读到'}，"
                f"当前版本备份 {'已存在' if pms_report.get('backup_exists') else '缺失'}（备份数 {pms_report.get('backup_count', 0)}）"
            )
            self.pset_doc_label.setObjectName("errorLabel")
        self.pset_doc_label.style().unpolish(self.pset_doc_label)
        self.pset_doc_label.style().polish(self.pset_doc_label)

    def pset_show_core_batch_result(self, pms_text: str) -> None:
        """整批更新结束后的结果行（v1.9.17，主窗口回填）——关掉弹窗后还能在这里看到。"""
        self.pset_last_download_label.setText(f"上次更新：{pms_text}")
