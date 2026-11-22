#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""节点健康检查的界面接线（v1.9.37）

为什么单独成文件：主窗口按功能域拆成 mixin（铁律 7.53）。健康检查有自己的一整套
「后台服务 + 定时器 + 状态栏 + 托盘 + 设置页动作」，塞进 `mainwindow_tests.py` 会把
「手动测试」和「自动体检」两件事搅在一起，而用户明确要求**不要和手动测试混在一起**。

分工：
    · 逻辑全在 `pms/healthcheck.py`（`PmsHealthCheckService`），本文件只做接线与展示；
    · **进度走状态栏 + 托盘提示，不碰手动测试那台共享进度条**（规则 7.41：进度条是共享状态机）；
    · 「通知一次」= 一批只弹一次托盘气泡（`phs_notice`），绝不逐节点弹窗。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer

from pms.constants import PMS_APP_NAME
from pms.healthcheck import PMS_HEALTH_TICK_MS, PmsHealthCheckService, pms_health_summary_text


class _PmwHealthMixin:
    """节点健康检查：服务装配、定时器、状态栏与托盘入口。"""

    # ------------------------------------------------------------------ 装配
    def pmw_setup_health_check(self) -> None:
        """装配健康检查服务（由 `pmw_start_timers()` 调用，那时服务依赖都已就绪）。"""
        self.pmw_health = PmsHealthCheckService(
            self.pmw_settings, self.pmw_repo, self.pmw_latency, self.pmw_engine, self,
            pms_busy_provider=self.pmw_health_manual_busy,
        )
        self.pmw_health.phs_status.connect(self.pmw_on_health_status)
        self.pmw_health.phs_progress.connect(self.pmw_on_health_progress)
        self.pmw_health.phs_summary.connect(self.pmw_on_health_summary)
        self.pmw_health.phs_notice.connect(self.pmw_on_health_notice)
        self.pmw_health.phs_log.connect(self.pmw_show_message)
        self.pmw_health_timer = QTimer(self)
        self.pmw_health_timer.timeout.connect(self.pmw_on_health_tick)
        self.pmw_health_timer.start(PMS_HEALTH_TICK_MS)
        self.pmw_apply_health_state()
        self.pmw_logger.info("节点健康检查：服务已装配（每 %d 秒看一次有没有到点的节点）", PMS_HEALTH_TICK_MS // 1000)

    def pmw_health_manual_busy(self) -> bool:
        """手动测试 / 测速在跑吗——在跑就让路（用户显式发起的操作优先级更高）。

        v1.9.59：电报 DC 测试同样逐节点起临时核心，必须一起算「忙」。"""
        if self.pmw_latency.plt_is_running() or self.pmw_speed.pst_is_running() or self.pmw_telegram.ptg_is_running():
            return True
        return int(getattr(self, "pmw_tcp_done", 0)) < int(getattr(self, "pmw_tcp_total", 0))

    def pmw_on_health_tick(self) -> None:
        """定时器每拍调一次：真正干不干活由服务自己按设置与到点情况决定。"""
        try:
            self.pmw_health.phs_on_tick()
        except Exception as exc:  # noqa: BLE001 - 后台体检绝不能把主界面带崩
            self.pmw_logger.warning("节点健康检查：本拍出错（已跳过）：%s", exc)

    # ------------------------------------------------------------------ 入口
    def pmw_health_run_now(self) -> None:
        """「立即运行一次」（工具栏菜单 / 托盘 / 设置页共用）。"""
        self.pmw_health_yield_to_manual()
        self.pmw_show_message(self.pmw_health.phs_run_now())

    def pmw_health_run_auto_only(self) -> None:
        """「只检查自动禁用的节点」——想单独验证被自己禁用的那批能不能复活时用。"""
        self.pmw_health_yield_to_manual()
        self.pmw_show_message(self.pmw_health.phs_run_now(pms_only_auto=True))

    def pmw_health_set_paused(self, pms_paused: bool) -> None:
        """暂停 / 继续健康检查（暂停会立刻停掉正在跑的这一批，跑完的照旧落库）。"""
        if pms_paused:
            self.pmw_show_message(self.pmw_health.phs_pause())
        else:
            self.pmw_show_message(self.pmw_health.phs_resume())
        self.pmw_apply_health_state()

    def pmw_is_health_round(self) -> bool:
        """当前这一轮延迟测试是不是「健康检查」发起的（它的进度与收尾都不走手动测试那套 UI）。"""
        return str(getattr(self.pmw_latency, "plt_scope", "") or "") == "health"

    def pmw_health_yield_to_manual(self) -> None:
        """给手动测试腾地方：手动测试要开始时，让健康检查先退下去（不抢核心与线程池）。"""
        if getattr(self, "pmw_health", None) is not None and self.pmw_health.phs_is_running():
            self.pmw_health.phs_stop()
            self.pmw_logger.info("节点健康检查：让路给手动测试（这一批没跑完的节点下一拍继续）")

    def pmw_apply_health_state(self) -> None:
        """把「是否暂停 / 进度」同步到托盘与菜单（设置改完也走它）。"""
        tray = getattr(self, "pmw_tray", None)
        if tray is not None:
            tray.ptrc_set_health_state(not self.pmw_health.phs_is_enabled())
            tray.ptrc_set_tooltip(self.pmw_health_status_line())
        action = self.pmw_actions.get("health_pause")
        if action is not None:
            action.blockSignals(True)
            action.setChecked(not self.pmw_health.phs_is_enabled())
            action.blockSignals(False)
            action.setText("继续健康检查" if not self.pmw_health.phs_is_enabled() else "暂停健康检查")

    def pmw_health_status_line(self) -> str:
        """设置页与托盘提示共用的一行状态。"""
        counts = self.pmw_health.phs_counts()
        if not counts:
            return self.pmw_health.phs_status_text()
        return (
            f"{self.pmw_health.phs_status_text()}｜启用 {counts.get('enabled', 0)}｜"
            f"自动禁用 {counts.get('auto_disabled', 0)}｜手动禁用 {counts.get('manual_disabled', 0)}"
        )

    # ------------------------------------------------------------------ 信号
    def pmw_on_health_status(self, pms_text: str) -> None:
        self.pmw_status_message.setText(str(pms_text))

    def pmw_on_health_progress(self, pms_done: int, pms_total: int) -> None:
        """进度只显示在状态栏 + 托盘提示（不动手动测试的进度条）。"""
        self.pmw_status_message.setText(self.pmw_health.phs_status_text())
        tray = getattr(self, "pmw_tray", None)
        if tray is not None:
            tray.ptrc_set_tooltip(f"{PMS_APP_NAME}｜节点健康检查 {int(pms_done)}/{int(pms_total)}")

    def pmw_on_health_summary(self, pms_summary: dict) -> None:
        self.pmw_show_message(pms_health_summary_text(dict(pms_summary or {})))
        tray = getattr(self, "pmw_tray", None)
        if tray is not None:
            tray.ptrc_set_tooltip(self.pmw_health_status_line())
        self.pmw_emit_event("health.summary", {
            "checked": int((pms_summary or {}).get("checked") or 0),
            "disabled": len((pms_summary or {}).get("disabled") or []),
            "revived": len((pms_summary or {}).get("revived") or []),
        })

    def pmw_on_health_notice(self, pms_title: str, pms_body: str) -> None:
        """自动禁用 / 自动复活：一批只通知一次（托盘气泡 + 状态栏 + 日志）。"""
        tray = getattr(self, "pmw_tray", None)
        if tray is not None:
            tray.ptrc_notify(str(pms_title), str(pms_body))
        self.pmw_show_message(f"{pms_title}：{pms_body}")

    def pmw_on_health_action(self, pms_action: str, pms_params: dict) -> None:
        """设置页「节点健康检查」部件发来的动作。"""
        if pms_action == "health_run_now":
            self.pmw_health_run_now()
        elif pms_action == "health_run_auto_only":
            self.pmw_health_run_auto_only()
        elif pms_action == "health_pause":
            self.pmw_health_set_paused(bool(pms_params.get("paused", True)))
        elif pms_action == "health_refresh":
            self.pmw_show_message(self.pmw_health_status_line())
        else:
            self.pmw_logger.warning("未知的健康检查动作：%s", pms_action)

    def pmw_health_shutdown(self) -> None:
        """退出流程里调用：停止本批 + 落盘，不留半截状态。"""
        service = getattr(self, "pmw_health", None)
        if service is None:
            return
        timer = getattr(self, "pmw_health_timer", None)
        if timer is not None:
            timer.stop()
        service.phs_shutdown()
