# -*- coding: utf-8 -*-
"""窗口外壳与退出（托盘 / 窗口事件 / 线程池收尾 / 退出序列 / 数据库备份）——v1.9.36 从 `pms/gui/mainwindow.py` 拆出的 mixin。

★ 拆分原则：**方法名与 `self.pmw_*` 属性名一个字都没改**，外部引用（主窗口之外用到
`pmw_*` 的 80 处）全都不用动；mixins 只是把方法按功能域挪个文件。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QThreadPool, Qt
from PySide6.QtWidgets import QMessageBox, QSystemTrayIcon
from pathlib import Path
from pms.constants import PMS_APP_EN_NAME, PMS_APP_NAME, PMS_EXIT_POOL_WAIT_MS
from pms.coresys import PmsWindowsSystemProxy
from pms.gui.tray import PmsTrayController
from pms.icons import pms_build_fallback_icon
from pms.utils import pms_now_text
from typing import Any


class _PmwShellMixin:
    """窗口外壳与退出（托盘 / 窗口事件 / 线程池收尾 / 退出序列 / 数据库备份）（8 个方法）。"""

    def pmw_setup_tray(self) -> None:
        """创建托盘图标（无 icon.ico 时使用内置绘制图标）。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.pmw_logger.warning("系统托盘不可用，托盘功能已跳过")
            return
        icon = self.pmw_icon if not self.pmw_icon.isNull() else pms_build_fallback_icon()
        self.pmw_tray = PmsTrayController(icon, self)
        self.pmw_tray.ptrc_show_window.connect(self.pmw_restore_from_tray)
        self.pmw_tray.ptrc_toggle_core.connect(self.pmw_toggle_core)
        self.pmw_tray.ptrc_toggle_system_proxy.connect(self.pmw_toggle_system_proxy)
        self.pmw_tray.ptrc_toggle_tun.connect(self.pmw_toggle_tun)
        self.pmw_tray.ptrc_test_latency.connect(self.pmw_test_latency_selected)
        # v1.9.37：健康检查三个入口（与手动测试并列，互不抢资源）
        self.pmw_tray.ptrc_health_run.connect(self.pmw_health_run_now)
        self.pmw_tray.ptrc_health_auto.connect(self.pmw_health_run_auto_only)
        self.pmw_tray.ptrc_health_pause.connect(self.pmw_health_set_paused)
        self.pmw_tray.ptrc_quit.connect(self.pmw_request_quit)
        self.pmw_tray.ptrc_set_core_state(self.pmw_core.pcp_is_running())
        self.pmw_tray.ptrc_set_proxy_state(self.pmw_settings.psm_get_bool("system_proxy.enabled", False))
        self.pmw_tray.ptrc_set_tun_state(self.pmw_settings.psm_get_bool("tun.enabled", False))
        self.pmw_tray.ptrc_show()
        self.pmw_apply_health_state()   # v1.9.37：把「暂停 / 继续」与状态提示同步到托盘
        self.pmw_logger.info("托盘图标已就绪（icon.ico %s）", "存在" if self.pmw_paths.prt_icon_exists() else "不存在，使用内置图标")

    def pmw_restore_from_tray(self) -> None:
        """从托盘恢复窗口。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def pmw_request_quit(self) -> None:
        """请求退出程序（托盘菜单调用）。"""
        self.pmw_quitting = True
        self.close()

    def changeEvent(self, pms_event: Any) -> None:  # noqa: N802 - Qt 只认这个标准虚函数名
        """最小化事件：按设置 `general.minimize_to_tray` 缩到托盘（v1.9.16）。

        这个设置项原来**没人读**：勾不勾，点最小化都只是普通最小化到任务栏。
        现在勾上 → 最小化时隐藏窗口 + 托盘气泡提示；没勾 → 保持默认行为。
        """
        super().changeEvent(pms_event)
        if pms_event.type() != QEvent.Type.WindowStateChange:
            return
        if self.pmw_quitting or self.pmw_tray is None or not self.isMinimized():
            return
        if not self.pmw_settings.psm_get_bool("general.minimize_to_tray", True):
            return
        # 缩到托盘后立刻取消最小化标志，否则从托盘 showNormal() 回来会被这行再次收走
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.hide()
        self.pmw_tray.ptrc_notify(PMS_APP_NAME, "已最小化到托盘，双击图标可重新打开")

    def closeEvent(self, pms_event: Any) -> None:  # noqa: N802 - Qt 只认这个标准虚函数名
        """关闭事件：按设置最小化到托盘或真正退出。

        注意：方法名必须是 closeEvent，写成 pmw_closeEvent 之类 Qt 永远不会调用它，
        结果就是「最小化到托盘」不生效、退出流程（停核心 / 还原系统代理 / 保存状态）也不执行。
        """
        if not self.pmw_quitting and self.pmw_settings.psm_get_bool("general.close_to_tray", True) and self.pmw_tray is not None:
            pms_event.ignore()
            self.hide()
            self.pmw_tray.ptrc_notify(PMS_APP_NAME, "已最小化到托盘，双击图标可重新打开")
            return
        if not self.pmw_quitting and self.pmw_settings.psm_get_bool("general.confirm_on_exit", False):
            answer = QMessageBox.question(self, "确认退出", "确定退出程序吗？正在运行的代理会被停止。")
            if answer != QMessageBox.StandardButton.Yes:
                pms_event.ignore()
                return
        self.pmw_quitting = True
        self.pmw_shutdown_sequence()
        pms_event.accept()

    def pmw_shutdown_thread_pools(self) -> None:
        """退出前清空所有挂在窗口下的线程池并限期等待，避免残留任务把进程拖住不退出。

        v1.9.9 两处修正：
        1. 先**全部** `clear()` 再逐个 `waitForDone()`。原来是一边 clear 一边 wait，
           第一个池子会独占整整一轮超时（10 秒），后面的池子等它等完才轮到自己被中断，
           退出被拖成「池子数量 × 超时」——这正是日志里连着报两回「未收尾」的原因。
        2. 补上全局池 `QThreadPool.globalInstance()`：`findChildren` 只找挂在窗口下的，
           挂在别处或走全局池的任务会被整个漏掉。
        """
        pools = [pool for pool in self.findChildren(QThreadPool)]
        global_pool = QThreadPool.globalInstance()
        if global_pool is not None and not any(pool is global_pool for pool in pools):
            pools.append(global_pool)
        for pool in pools:
            pool.clear()
        for pool in pools:
            if pool.activeThreadCount() <= 0:
                continue
            if not pool.waitForDone(PMS_EXIT_POOL_WAIT_MS):
                self.pmw_logger.warning("线程池 %s 在 %d ms 内未收尾，放弃等待", pool, PMS_EXIT_POOL_WAIT_MS)

    def pmw_shutdown_sequence(self, pms_restart_admin: bool = False) -> None:
        """退出流程：停核心、还原系统代理、保存状态、维护数据库、关闭日志。"""
        self.pmw_logger.info("开始退出流程…")
        try:
            # v1.9.9：先**无条件**打断所有在跑的测量会话 / 订阅抓取，再走各自的 stop 收尾。
            # 不能只看 is_running()——「本轮已判定结束」和「池子里还有任务在跑」是两回事，
            # 只按标志位跳过的话，收尾阶段残留的那几个线程照样把进程拖住。
            self.pmw_latency.plt_abort_sessions()
            self.pmw_speed.pst_abort_sessions()
            self.pmw_subscription.psu_abort_tasks()
            # v1.9.37：健康检查先自己收尾（停本批 + 落盘），别让它把手动测试的断点搅了
            try:
                self.pmw_health_shutdown()
            except Exception as exc:  # noqa: BLE001 - 退出路径上它出错不能挡住退出
                self.pmw_logger.warning("退出时收尾节点健康检查失败：%s", exc)
            if self.pmw_latency.plt_is_running():
                self.pmw_latency.plt_stop()
            if self.pmw_speed.pst_is_running():
                self.pmw_speed.pst_stop()
            if self.pmw_subscription.psu_is_running():
                self.pmw_subscription.psu_stop()
            self.pmw_api_server.pas_stop("程序退出")
            self.pmw_autoswitch.pasw_stop()
            if self.pmw_core.pcp_is_running():
                self.pmw_core.pcp_stop("程序退出")
            self.pmw_traffic.pts_stop()
            self.pmw_shutdown_thread_pools()
            if self.pmw_settings.psm_get_bool("system_proxy.restore_on_exit", True) and not pms_restart_admin:
                PmsWindowsSystemProxy(self.pmw_logger).pwsp_restore_from_settings(self.pmw_settings)
            self.pmw_save_ui_state()
            if self.pmw_settings.psm_get_bool("database.backup_on_exit", False):
                self.pmw_backup_database(False)
            self.pmw_engine.pdb_prune_history(
                self.pmw_settings.psm_get_int("database.latency_history_days", 30),
                self.pmw_settings.psm_get_int("database.connection_history_days", 7),
                self.pmw_settings.psm_get_bool("database.keep_connection_history", False),
                self.pmw_settings.psm_get_bool("database.keep_latency_history", True),
            )
            if self.pmw_settings.psm_get_bool("database.auto_vacuum", True):
                self.pmw_engine.pdb_vacuum()
            self.pmw_settings.psm_flush()
        except Exception as exc:  # noqa: BLE001 - 退出流程绝不能抛异常
            self.pmw_logger.exception("退出流程出现异常：%s", exc)
        finally:
            self.pmw_engine.pdb_close()
            if self.pmw_tray is not None:
                self.pmw_tray.ptrc_hide()
            self.pmw_logger.info("退出流程完成")

    def pmw_backup_database(self, pms_notify: bool) -> bool:
        """备份数据库到配置目录或默认 data\\backups。"""
        directory = self.pmw_settings.psm_get_str("database.backup_dir", "").strip()
        target_dir = Path(directory) if directory else (self.pmw_paths.prt_data_dir / "backups")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{PMS_APP_EN_NAME}_{pms_now_text('%Y%m%d_%H%M%S')}.db"
        ok = self.pmw_engine.pdb_backup_to(target)
        if pms_notify:
            (QMessageBox.information if ok else QMessageBox.warning)(
                self, "数据库备份", f"已备份到：{target}" if ok else "备份失败，请查看日志"
            )
        if ok:
            self.pmw_show_message(f"数据库已备份：{target.name}")
        return ok
