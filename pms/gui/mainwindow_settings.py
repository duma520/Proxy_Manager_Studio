# -*- coding: utf-8 -*-
"""设置响应（设置中心动作、改设置后重生效、主题与界面参数）——v1.9.36 从 `pms/gui/mainwindow.py` 拆出的 mixin。

★ 拆分原则：**方法名与 `self.pmw_*` 属性名一个字都没改**，外部引用（主窗口之外用到
`pmw_*` 的 80 处）全都不用动；mixins 只是把方法按功能域挪个文件。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import QApplication, QMessageBox
from pathlib import Path
from pms.constants import (
    PMS_APP_VERSION,
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
    PMS_MAX_CONNECTION_ROWS,
    PMS_THEME_NATIVE,
    PMS_VERSION_BACKUP_NOTE_PLACEHOLDER,
)
from pms.coresys import PmsPrivilegeUtils, PmsWindowsAutoStart, PmsWindowsSystemProxy
from pms.gui.dialogs.aboutdialog import PmsAboutDialog
from pms.gui.dialogs.healthcheckwidget import PmsHealthCheckWidget
from pms.gui.dialogs.settingsdialog import PmsSettingsDialog
from pms.gui.dialogs.systemtools import PmsDiagnosticsDialog, PmsSettingsHistoryDialog
from pms.gui.dialogs.versionhistory import PmsVersionHistoryDialog
from pms.logbus import PMS_ERROR_LOG_MAX
from pms.coremeta import pms_core_title
from pms.utils import pms_safe_int
from typing import Any


class _PmwSettingsMixin:
    """设置响应（设置中心动作、改设置后重生效、主题与界面参数）（17 个方法）。"""

    def pmw_open_settings_dialog(self, pms_tab_index: int = -1) -> None:
        """打开设置中心。"""
        if self.pmw_settings_dialog is None:
            dialog = PmsSettingsDialog(self.pmw_settings, self.pmw_paths, self.pmw_env, self.pmw_locator, self.pmw_repo, self)
            dialog.psed_action.connect(self.pmw_handle_settings_action)
            # v1.9.37：健康检查部件的「运行状态」由主窗口给（部件不认识数据库，同 autoswitchwidget 的做法）
            for widget in dialog.findChildren(PmsHealthCheckWidget):
                widget.phw_set_status_provider(self.pmw_health_status_line)
            dialog.pset_refresh_doc_status(self.pmw_backup.pvbh_consistency_report())
            dialog.finished.connect(self.pmw_on_settings_dialog_closed)
            self.pmw_settings_dialog = dialog
        else:
            dialog = self.pmw_settings_dialog
            dialog.pset_refresh_core_status()
            dialog.pset_refresh_database_status()
            dialog.pset_refresh_env_status()
            dialog.pset_refresh_doc_status(self.pmw_backup.pvbh_consistency_report())
        if pms_tab_index >= 0:
            dialog.pset_tabs.setCurrentIndex(pms_tab_index)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def pmw_open_about_dialog(self) -> None:
        """打开关于对话框。"""
        dialog = PmsAboutDialog(self.pmw_paths, self.pmw_settings, self.pmw_env, self.pmw_locator, self.pmw_repo, self.pmw_backup, self)
        dialog.pabd_action.connect(self.pmw_handle_settings_action)
        dialog.exec()

    def pmw_open_version_history(self) -> None:
        """打开版本迭代记录对话框。"""
        dialog = PmsVersionHistoryDialog(self.pmw_paths, self.pmw_backup, self)
        dialog.pvhd_action.connect(self.pmw_handle_settings_action)
        dialog.exec()
        if self.pmw_settings_dialog is not None:
            self.pmw_settings_dialog.pset_refresh_doc_status(self.pmw_backup.pvbh_consistency_report())

    def pmw_open_diagnostics_dialog(self) -> None:
        """打开网络诊断对话框（不经过系统代理，检查本地出网能力）。"""
        api_port = self.pmw_settings.psm_get_int("stats.xray_api_port", 0) or self.pmw_settings.psm_get_int("stats.clash_api_port", 0)
        dialog = PmsDiagnosticsDialog(self.pmw_settings, int(api_port), self)
        dialog.exec()

    def pmw_open_pac_editor_dialog(self) -> None:
        """打开内置 PAC 脚本编辑器（v1.9.62：专业手写 + 小白表单双模式）。

        入口有三处，全部汇到这里：工具栏「更多 ▾ → PAC 编辑器」、系统代理设置页的
        「打开 PAC 编辑器」按钮（经 pmw_handle_settings_action 的 open_pac_editor 分支）。
        用 show() 而非 exec()：编辑器内部发出来的 toast / open_folder 动作要回到主窗口，
        非模态才不会挡住用户去点别的。
        """
        from pms.gui.dialogs.paceditor import PmsPacEditorDialog

        if self.pmw_pac_editor_dialog is None:
            dialog = PmsPacEditorDialog(self.pmw_paths, self.pmw_settings, self.pmw_core.pcp_profile, self)
            dialog.pced_action.connect(self.pmw_handle_settings_action)
            dialog.finished.connect(self.pmw_on_pac_editor_closed)
            self.pmw_pac_editor_dialog = dialog
        else:
            dialog = self.pmw_pac_editor_dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def pmw_on_pac_editor_closed(self, _result: int) -> None:
        """PAC 编辑器关闭：清缓存引用，下次打开按最新设置重建（与设置对话框同一口径）。"""
        self.pmw_pac_editor_dialog = None

    def pmw_open_settings_history_dialog(self) -> None:
        """打开设置历史对话框（查看 / 回滚历史设置快照）。"""
        dialog = PmsSettingsHistoryDialog(self.pmw_settings, self)
        dialog.exec()

    def pmw_core_download_mirror(self) -> str:
        """核心下载镜像前缀：设置优先，其次 .env 的 PMS_CORE_DOWNLOAD_MIRROR。"""
        return (
            self.pmw_settings.psm_get_str("core.download_mirror", "").strip()
            or self.pmw_env.pes_get("PMS_CORE_DOWNLOAD_MIRROR", "").strip()
        )

    def pmw_handle_settings_action(self, pms_action: str, pms_params: dict) -> None:
        """集中处理设置 / 关于 / 版本对话框发来的动作请求。"""
        if pms_action == "download_core":
            kind = pms_params.get("kind")
            # v1.9.11：支持一键批量下载核心；设置页可能发来特殊 token 或 list[str]
            core_kinds: list[str] = []
            if isinstance(kind, list):
                core_kinds = [str(k).strip() for k in kind if str(k).strip()]
            elif kind == "__all__":
                core_kinds = [PMS_CORE_KIND_SINGBOX, PMS_CORE_KIND_XRAY]
            elif isinstance(kind, str) and kind.strip():
                core_kinds = [kind.strip()]
            if not core_kinds:
                return
            if self.pmw_downloader.pcdl_is_running():
                # 已在下载：把剩余加入队列并提示，而不是直接拒绝
                self.pmw_downloader.pcdl_start_list(
                    core_kinds,
                    self.pmw_core_download_mirror(),
                    self.pmw_settings.psm_get_int("inbound.mixed_port", 30808),
                    self.pmw_settings.psm_get_str("telegram.external_proxy", ""),
                    pms_notify=True,     # v1.9.17：用户手动发起，整批跑完要弹汇总
                )
                remaining = self.pmw_downloader.pcdl_queue_count()
                self.pmw_show_message(f"当前有核心在下载，已把其余 {remaining} 个加入队列，完成后自动继续")
                return
            started = self.pmw_downloader.pcdl_start_list(
                core_kinds,
                self.pmw_core_download_mirror(),
                self.pmw_settings.psm_get_int("inbound.mixed_port", 30808),
                self.pmw_settings.psm_get_str("telegram.external_proxy", ""),
                pms_notify=True,         # v1.9.17：同上
            )
            if started:
                titles = " / ".join(pms_core_title(k) for k in core_kinds[:5])
                if len(core_kinds) > 5:
                    titles += f" 等 {len(core_kinds)} 个"
                self.pmw_show_message(f"开始下载核心队列：{titles}（共 {len(core_kinds)} 个，串行）")
            return
        elif pms_action == "restart_as_admin":
            if PmsPrivilegeUtils.ppu_is_admin():
                QMessageBox.information(self, "无需提权", "当前已是管理员权限")
                return
            if QMessageBox.question(self, "以管理员身份重启", "将关闭当前程序并以管理员身份重新启动，继续？") != QMessageBox.StandardButton.Yes:
                return
            self.pmw_quitting = True
            self.pmw_shutdown_sequence(pms_restart_admin=True)
            if PmsPrivilegeUtils.ppu_relaunch_as_admin():
                QApplication.quit()
            else:
                QMessageBox.warning(self, "提权失败", "无法以管理员身份启动，请手动右键“以管理员身份运行”。")
                self.pmw_quitting = False
        elif pms_action == "backup_version":
            ok, message = self.pmw_backup.pvbh_backup_source(PMS_APP_VERSION, str(pms_params.get("note") or PMS_VERSION_BACKUP_NOTE_PLACEHOLDER))
            (QMessageBox.information if ok else QMessageBox.warning)(self, "版本备份", message)
            if self.pmw_settings_dialog is not None:
                self.pmw_settings_dialog.pset_refresh_doc_status(self.pmw_backup.pvbh_consistency_report())
        elif pms_action == "open_folder":
            target = Path(str(pms_params.get("path") or self.pmw_paths.prt_root))
            target.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        elif pms_action == "open_document":
            doc = self.pmw_paths.prt_doc_file
            if doc.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(doc)))
            else:
                QMessageBox.information(self, "文档不存在", f"未找到：{doc}")
        elif pms_action == "backup_database":
            self.pmw_backup_database(True)
        elif pms_action == "vacuum_database":
            ok = self.pmw_engine.pdb_vacuum()
            (QMessageBox.information if ok else QMessageBox.warning)(self, "数据库整理", "已完成 VACUUM 整理" if ok else "整理失败，请查看日志")
        elif pms_action == "prune_history":
            removed = self.pmw_engine.pdb_prune_history(
                self.pmw_settings.psm_get_int("database.latency_history_days", 30),
                self.pmw_settings.psm_get_int("database.connection_history_days", 7),
                self.pmw_settings.psm_get_bool("database.keep_connection_history", False),
                self.pmw_settings.psm_get_bool("database.keep_latency_history", True),
            )
            QMessageBox.information(self, "历史清理", f"已删除 {removed} 条过期记录")
            if self.pmw_settings_dialog is not None:
                self.pmw_settings_dialog.pset_refresh_database_status()
        elif pms_action == "integrity_check":
            result = self.pmw_engine.pdb_integrity_check()
            QMessageBox.information(self, "完整性检查", f"结果：{result}")
        elif pms_action == "export_all":
            self.pmw_export_backup()
        elif pms_action == "backup_system_proxy":
            state = PmsWindowsSystemProxy(self.pmw_logger).pwsp_backup_to_settings(self.pmw_settings)
            QMessageBox.information(self, "已备份", f"原系统代理：启用={state.get('enable')}\n服务器={state.get('server') or '（空）'}")
        elif pms_action == "restore_system_proxy":
            ok = PmsWindowsSystemProxy(self.pmw_logger).pwsp_restore_from_settings(self.pmw_settings)
            (QMessageBox.information if ok else QMessageBox.warning)(
                self, "还原系统代理", "已按备份还原系统代理" if ok else "没有可还原的备份"
            )
        elif pms_action == "autoswitch_check_now":
            if not self.pmw_autoswitch.pasw_check_now(pms_manual=True):
                self.pmw_logger.info("自动切换：手动检测未发起（见上一条说明）")
        elif pms_action == "open_pac_editor":   # v1.9.62：系统代理设置页的「打开 PAC 编辑器」按钮
            self.pmw_open_pac_editor_dialog()
        elif pms_action == "toast":   # v1.9.64：PAC 编辑器等对话框的轻量提示（状态栏 5 秒，不弹窗打断）
            text = str(pms_params.get("text") or "").strip()
            if text:
                self.pmw_show_message(text)
            return
        elif str(pms_action).startswith("health_"):   # v1.9.37 节点健康检查（设置 → 测试）
            self.pmw_on_health_action(str(pms_action), dict(pms_params or {}))
        else:
            self.pmw_logger.warning("未知的设置动作：%s", pms_action)

    def pmw_on_setting_changed(self, pms_key: str, pms_value: Any) -> None:
        """设置项变化 → 立即生效（能立即生效的都立即生效，不能的提示重启核心）。"""
        if str(pms_key).startswith("health."):   # v1.9.37：健康检查的开关 / 周期 / 阈值改完立刻生效
            self.pmw_apply_health_state()
            return
        if pms_key in ("ui.theme", "ui.ui_scale"):
            self.pmw_apply_theme()
        elif pms_key == "ui.show_connection_panel":
            self.pmw_bottom_tabs.setTabVisible(1, bool(pms_value))
        elif pms_key == "ui.show_left_sidebar":
            self.pmw_set_sidebar_visible(bool(pms_value))
        elif pms_key == "ui.show_quick_actions":
            self.pmw_quick_actions_box.setVisible(bool(pms_value))
        elif pms_key == "ui.show_quick_stats":
            self.pmw_quick_frame.setVisible(bool(pms_value))
        elif pms_key == "ui.show_status_traffic":
            self.pmw_status_proxy.setVisible(bool(pms_value))
            self.pmw_status_direct.setVisible(bool(pms_value))
        elif pms_key in ("ui.table_row_height", "ui.table_font_size", "ui.log_font_size"):
            self.pmw_apply_table_settings()
        elif pms_key == "ui.latency_bar_height":
            # v1.9.23：延迟列进度条粗细（0 = 不画条）——改完立刻重画
            self.pmw_apply_latency_bar_height()
        elif pms_key == "ui.table_column_visible":
            self.pmw_apply_column_visibility()
        elif pms_key == "test.fail_show_core":
            # v1.9.21：失败提示口径改了立刻重画（只发延迟列 dataChanged，不整表重置）
            self.pmw_apply_fail_text_mode()
        elif pms_key == "ui.table_page_size":
            # v1.7.0：每页条数改了立刻重切页（下拉里改、或从设置导入进来都走这里）
            if not self.pmw_applying_page_size:
                self.pmw_applying_page_size = True
                try:
                    size = self.pmw_page_size()
                    index = self.pmw_combo_page_size.findData(size)
                    self.pmw_combo_page_size.setCurrentIndex(index if index >= 0 else 0)
                finally:
                    self.pmw_applying_page_size = False
            self.pmw_apply_pagination(1, pms_reset_to_first=True)
        elif pms_key == "ui.log_max_lines":
            self.pmw_log_model.pltm_set_max_lines(pms_safe_int(pms_value, 2000, 100))
            self.pmw_log_bus.plb_set_max_lines(pms_safe_int(pms_value, 2000, 100))
        elif pms_key == "core.log_level":
            self.pmw_log_bus.plb_set_level(str(pms_value))
            self.pmw_log_level_combo.blockSignals(True)
            self.pmw_log_level_combo.setCurrentIndex(max(0, self.pmw_log_level_combo.findData(str(pms_value))))
            self.pmw_log_level_combo.blockSignals(False)
            self.pmw_log_model.pltm_set_min_level(self.pmw_effective_log_level())
        # v1.9.16：以下四项原来都是「界面里有、代码没人读」的死设置，现在改动即时生效
        elif pms_key == "advanced.debug_mode":
            self.pmw_log_model.pltm_set_min_level(self.pmw_effective_log_level())
        elif pms_key == "ui.log_show_time":
            self.pmw_log_view.setColumnHidden(0, not bool(pms_value))
        elif pms_key == "advanced.max_log_file_mb":
            self.pmw_log_bus.plb_set_max_file_mb(pms_safe_int(pms_value, 8, 1, 512))
        elif pms_key == "database.max_connection_rows":
            self.pmw_tracker.pct_set_max_rows(pms_safe_int(pms_value, PMS_MAX_CONNECTION_ROWS, 50, 5000))
            self.pmw_connection_model.pctm_set_records(self.pmw_tracker.pct_snapshot())
        elif pms_key == "subscription.auto_update_enabled" or pms_key == "subscription.auto_update_interval_minutes":
            interval = self.pmw_settings.psm_get_int("subscription.auto_update_interval_minutes", 360)
            if bool(self.pmw_settings.psm_get_bool("subscription.auto_update_enabled", False)) and interval > 0:
                self.pmw_subscription_timer.start(max(10, interval) * 60 * 1000)
            else:
                self.pmw_subscription_timer.stop()
        elif pms_key == "test.auto_test_interval_minutes":
            interval = pms_safe_int(pms_value, 0)
            if interval > 0:
                self.pmw_test_timer.start(interval * 60 * 1000)
            else:
                self.pmw_test_timer.stop()
        elif pms_key == "advanced.clipboard_watch":
            if bool(pms_value):
                self.pmw_clipboard_timer.start(1200)
            else:
                self.pmw_clipboard_timer.stop()
        elif pms_key == "stats.source" or pms_key == "stats.interval_ms":
            if self.pmw_core.pcp_is_running():
                # v1.9.29：统计源要按**正在跑的那个核心**重配（逐节点选核心后，全局答案可能和实际不符）
                core_kind = self.pmw_core.pcp_core_kind or None
                core_path = self.pmw_core.pcp_core_path
                if core_kind and core_path:
                    self.pmw_traffic.pts_stop()
                    self.pmw_traffic.pts_configure(core_kind, core_path, self.pmw_port_plan)
                    self.pmw_traffic.pts_start()
        elif pms_key == "general.auto_start_with_windows":
            controller = PmsWindowsAutoStart(self.pmw_paths)
            ok = controller.pwas_set_enabled(bool(pms_value))
            if not ok:
                QMessageBox.warning(self, "设置失败", "无法写入开机自启项（请检查权限）")
        elif pms_key == "system_proxy.enabled":
            if self.pmw_core.pcp_is_running():
                self.pmw_apply_system_proxy(bool(pms_value))
        elif pms_key in ("system_proxy.mode", "system_proxy.pac_direct_domains"):
            # v1.9.49：改了模式（fixed↔pac）或 PAC 直连域名要立刻生效——系统代理正开着就按新配置重开，
            # 否则会同时留下 ProxyServer 与 AutoConfigURL（两种模式互斥，行为不可预期）。
            self.pmw_reopen_system_proxy_for_mode_change()
        elif pms_key == "tun.enabled":
            self.pmw_sync_tun_action(bool(pms_value))
            self.pmw_restart_core_for_settings()
        elif pms_key == "inbound.allow_lan":
            self.pmw_sync_allow_lan_action(bool(pms_value))
            self.pmw_restart_core_for_settings()
        elif pms_key in (
            # v1.6.7：以下设置只影响核心配置，改了必须重启核心才生效。
            # 之前只有 core.kind / 三个端口 / routing.mode 会重启，监听地址、TUN、
            # mux、DNS、 sniff 等改完毫无反应（和「显示列不生效」同一个毛病）。
            "core.kind", "routing.mode",
            "inbound.mixed_enabled", "inbound.mixed_port",
            "inbound.http_enabled", "inbound.http_port",
            "inbound.socks_enabled", "inbound.socks_port",
            "inbound.listen_address", "inbound.udp_enabled", "inbound.sniff_enabled",
            "inbound.auth_enabled", "inbound.username", "inbound.password",
            "core.mux_enabled", "core.mux_concurrency", "core.tcp_fast_open", "core.sniff_override_dest",
            "routing.dns_remote", "routing.dns_direct",
            "tun.interface_name", "tun.stack", "tun.mtu",
            "tun.auto_route", "tun.strict_route", "tun.ipv6",
        ):
            self.pmw_restart_core_for_settings()
        elif pms_key in ("api.enabled", "api.host", "api.port", "api.auth_mode"):
            self.pmw_api_server.pas_apply_settings()
        elif pms_key in (
            "autoswitch.enabled", "autoswitch.interval_seconds", "autoswitch.fail_threshold", "autoswitch.detect_mode",
        ):
            self.pmw_autoswitch.pasw_apply_settings()

    def pmw_restart_core_for_settings(self) -> None:
        """核心相关设置改了 → 运行中就重启一次核心让新值生效（v1.6.7）。

        三处（局域网监听 / TUN / 核心参数）原来各写一遍同样的重启代码，
        这里收敛成一个，行为一致且不会重复重启。
        """
        if not self.pmw_core.pcp_is_running():
            return
        node = self.pmw_current_node()
        if node is None:
            self.pmw_show_message("核心相关设置已保存，将在下次启动核心时生效")
            return
        self.pmw_show_message("核心相关设置已修改，正在重启核心以生效…")
        self.pmw_core.pcp_stop("应用新设置")
        QTimer.singleShot(500, lambda: self.pmw_start_core(node))

    def pmw_reapply_all_settings(self) -> None:
        """设置被**整体重载**后，把界面与运行参数按新值全部重刷一遍（v1.6.7）。

        ``psm_value_changed`` 只在单键修改时发射；「导入设置文件 / 重置分区 /
        全部重置」走的是 ``psm_values_reloaded``（不带键），之前没人订阅，
        导致导入完界面还是旧样子。这里不逐个键派发，直接调底层 apply 方法，
        避免把核心重启 N 次。
        """
        settings = self.pmw_settings
        self.pmw_apply_theme()
        self.pmw_apply_column_visibility()
        self.pmw_bottom_tabs.setTabVisible(1, settings.psm_get_bool("ui.show_connection_panel", True))
        self.pmw_set_sidebar_visible(settings.psm_get_bool("ui.show_left_sidebar", True))
        self.pmw_quick_actions_box.setVisible(settings.psm_get_bool("ui.show_quick_actions", True))
        self.pmw_quick_frame.setVisible(settings.psm_get_bool("ui.show_quick_stats", True))
        traffic = settings.psm_get_bool("ui.show_status_traffic", True)
        self.pmw_status_proxy.setVisible(traffic)
        self.pmw_status_direct.setVisible(traffic)
        log_level = settings.psm_get_str("core.log_level", "info")
        self.pmw_log_bus.plb_set_level(log_level)
        self.pmw_log_model.pltm_set_max_lines(settings.psm_get_int("ui.log_max_lines", 2000))
        self.pmw_log_bus.plb_set_max_lines(settings.psm_get_int("ui.log_max_lines", 2000))
        self.pmw_apply_aux_settings()
        self.pmw_log_level_combo.blockSignals(True)
        self.pmw_log_level_combo.setCurrentIndex(max(0, self.pmw_log_level_combo.findData(log_level)))
        self.pmw_log_level_combo.blockSignals(False)
        self.pmw_autoswitch.pasw_apply_settings()
        self.pmw_api_server.pas_apply_settings()
        self.pmw_apply_timers()
        if self.pmw_core.pcp_is_running() and self.pmw_current_node() is not None:
            self.pmw_restart_core_for_settings()
        self.pmw_show_message("设置已重新载入并应用到界面")

    def pmw_apply_timers(self) -> None:
        """按当前设置重算三个定时任务（订阅自动更新 / 自动测速 / 剪贴板监听）。"""
        settings = self.pmw_settings
        interval = settings.psm_get_int("subscription.auto_update_interval_minutes", 360)
        if settings.psm_get_bool("subscription.auto_update_enabled", False) and interval > 0:
            self.pmw_subscription_timer.start(max(10, interval) * 60 * 1000)
        else:
            self.pmw_subscription_timer.stop()
        test_interval = settings.psm_get_int("test.auto_test_interval_minutes", 0)
        if test_interval > 0:
            self.pmw_test_timer.start(test_interval * 60 * 1000)
        else:
            self.pmw_test_timer.stop()
        if settings.psm_get_bool("advanced.clipboard_watch", False):
            self.pmw_clipboard_timer.start(1200)
        else:
            self.pmw_clipboard_timer.stop()

    def pmw_effective_log_level(self) -> str:
        """界面日志面板实际生效的最低级别（v1.9.16）。

        `advanced.debug_mode`（「调试模式（界面日志显示 DEBUG 级别）」）以前**没人读**，
        勾了也不生效。现在：勾上 → 面板按 debug 显示；没勾 → 用 `core.log_level`。
        """
        if self.pmw_settings.psm_get_bool("advanced.debug_mode", False):
            return "debug"
        return self.pmw_settings.psm_get_str("core.log_level", "info")

    def pmw_apply_aux_settings(self) -> None:
        """应用「界面 / 后台杂项」设置（v1.9.16 集中接线，原来这几项都是死设置）。

        ui.log_show_time（日志时间列显隐）、advanced.debug_mode（面板是否收 DEBUG）、
        advanced.max_log_file_mb（日志文件轮转大小）、database.max_connection_rows（连接面板行数上限）。
        v1.9.21：再加 test.fail_show_core（失败节点的延迟列显示「超时」还是核心原话）。
        """
        settings = self.pmw_settings
        self.pmw_log_view.setColumnHidden(0, not settings.psm_get_bool("ui.log_show_time", True))
        self.pmw_log_bus.plb_set_max_file_mb(settings.psm_get_int("advanced.max_log_file_mb", 8))
        self.pmw_tracker.pct_set_max_rows(settings.psm_get_int("database.max_connection_rows", PMS_MAX_CONNECTION_ROWS))
        self.pmw_log_model.pltm_set_min_level(self.pmw_effective_log_level())
        self.pmw_apply_fail_text_mode()

    def pmw_apply_table_settings(self) -> None:
        """应用表格行高、字号与延迟列进度条粗细设置。

        v1.9.76：三个标签页（全部节点 / TgProxy / TgWebProxy）各有一套表格，
        行高 / 字号 / 延迟条全部灌一遍，避免只有当前页生效。
        """
        self.pmwt_sync_all_tab_settings()
        row_height = self.pmw_settings.psm_get_int("ui.table_row_height", 26)
        font_size = self.pmw_settings.psm_get_float("ui.table_font_size", 9.5)
        views = getattr(self, "pmwt_views", None) or {}
        if views:
            for view in views.values():
                table = getattr(view, "pmwt_table", None)
                if table is None:
                    continue
                table.verticalHeader().setDefaultSectionSize(row_height)
                font = QFont(table.font())
                font.setPointSizeF(font_size)
                table.setFont(font)
        else:
            # 标签还没建（极早期调用）：回落到活动页单栈
            self.pmw_node_table.verticalHeader().setDefaultSectionSize(row_height)
            font = QFont(self.pmw_node_table.font())
            font.setPointSizeF(font_size)
            self.pmw_node_table.setFont(font)
        log_font = QFont(self.pmw_log_view.font())
        log_font.setPointSizeF(self.pmw_settings.psm_get_float("ui.log_font_size", 9.0))
        self.pmw_log_view.setFont(log_font)

    def pmw_apply_latency_bar_height(self) -> None:
        """把「延迟列进度条粗细」灌进绘制代理并重画（v1.9.23）。

        v1.9.76：三个标签页各有一个绘制代理，全部灌一遍再各重绘一次。
        """
        height = max(0, self.pmw_settings.psm_get_int("ui.latency_bar_height", 5))
        views = getattr(self, "pmwt_views", None) or {}
        for view in views.values():
            delegate = getattr(view, "pmwt_delegate", None)
            table = getattr(view, "pmwt_table", None)
            if delegate is None or table is None:
                continue
            delegate.plbd_bar_height = height
            table.viewport().update()
        if not views:
            # 标签还没建（极早期调用）：回落到活动页单栈
            delegate = getattr(self, "pmw_bar_delegate", None)
            if delegate is None:
                return
            delegate.plbd_bar_height = height
            self.pmw_node_table.viewport().update()

    def pmw_apply_theme(self) -> None:
        """应用主题与缩放（跟随系统时每次启动重新判定；v1.9.7 原生模式用 Windows 默认绘制）。"""
        theme_setting = self.pmw_settings.psm_get_str("ui.theme", PMS_THEME_NATIVE)
        resolved = self.pmw_theme.pthm_resolve(theme_setting)
        scale = self.pmw_settings.psm_get_float("ui.ui_scale", 1.0)
        app = QApplication.instance()
        if app is not None:
            if resolved == PMS_THEME_NATIVE:
                # v1.9.7：系统原生风格——清空自定义样式表、强制 Windows 风格绘制
                app.setStyleSheet("")
                app.setStyle("windows")
            else:
                app.setStyleSheet(self.pmw_theme.pthm_build_qss(theme_setting, scale))
            base_font = QFont(app.font())
            base_font.setPointSizeF(max(8.0, 9.5 * scale))
            app.setFont(base_font)
        self.pmw_node_model.pntm_set_theme_dark(self.pmw_theme.pthm_is_dark(theme_setting))
        self.pmw_apply_table_settings()

    def pmw_refresh_error_badge(self) -> None:
        """刷新红点：未读 > 0 才显示，数字就是未读数；同时更新面板抬头。"""
        total = self.pmw_error_model.rowCount()
        if self.pmw_error_label is not None:
            self.pmw_error_label.setText(
                f"最近错误 {total} 条（只收 error 级，最多 {PMS_ERROR_LOG_MAX} 条）"
                + (f"，未读 {self.pmw_error_unread} 条" if self.pmw_error_unread else "")
            )
        if self.pmw_error_unread > 0:
            self.pmw_btn_errors.setText(f"⚠ {self.pmw_error_unread}")
            self.pmw_btn_errors.setToolTip(
                f"{self.pmw_error_unread} 条未读错误（共 {total} 条）\n点这里打开「错误」页签，打开即已读"
            )
            self.pmw_btn_errors.show()
        else:
            self.pmw_btn_errors.hide()
