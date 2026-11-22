# -*- coding: utf-8 -*-
"""日志 / 错误面板 / 连接与流量（含日志导出与 UI 心跳）——v1.9.36 从 `pms/gui/mainwindow.py` 拆出的 mixin。

★ 拆分原则：**方法名与 `self.pmw_*` 属性名一个字都没改**，外部引用（主窗口之外用到
`pmw_*` 的 80 处）全都不用动；mixins 只是把方法按功能域挪个文件。
"""
from __future__ import annotations

import time
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from pathlib import Path
from pms.logbus import PMS_LOG_ERROR_LEVELS
from pms.utils import pms_atomic_write_text, pms_format_bytes, pms_format_speed, pms_now_text, pms_safe_int


class _PmwLogMixin:
    """日志 / 错误面板 / 连接与流量（含日志导出与 UI 心跳）（12 个方法）。"""

    def pmw_on_log_record(self, pms_time: str, pms_level: str, pms_source: str, pms_message: str) -> None:
        """日志总线 → 日志面板；error 级额外进「错误」页签并累加未读（v1.8.0）。"""
        self.pmw_log_model.pltm_append(pms_time, pms_level, pms_source, pms_message)
        if self.pmw_chk_log_scroll.isChecked():
            self.pmw_log_view.scrollToBottom()
        if str(pms_level).strip().upper() not in PMS_LOG_ERROR_LEVELS:
            return
        self.pmw_error_model.pltm_append(pms_time, pms_level, pms_source, pms_message)
        # 只有「用户当前没在看错误页」才算未读；正看着就直接算已读，红点不闪
        if self.pmw_bottom_tabs.currentIndex() != self.pmw_error_tab_index:
            self.pmw_error_unread += 1
        self.pmw_refresh_error_badge()

    def pmw_open_error_panel(self) -> None:
        """打开「错误」页签并把未读清零。"""
        self.pmw_bottom_tabs.setCurrentIndex(self.pmw_error_tab_index)
        self.pmw_error_unread = 0
        self.pmw_refresh_error_badge()
        self.pmw_error_view.scrollToBottom()

    def pmw_reload_error_panel(self) -> None:
        """用日志总线里的错误缓冲重建面板（启动 / 设置重载时用）。"""
        self.pmw_error_model.pltm_clear()
        for item in self.pmw_log_bus.plb_error_snapshot():
            self.pmw_error_model.pltm_append(*item)
        self.pmw_refresh_error_badge()

    def pmw_clear_error_panel(self) -> None:
        """清空错误面板（同时清掉日志总线里的错误缓冲，主日志不受影响）。"""
        self.pmw_error_model.pltm_clear()
        self.pmw_log_bus.plb_error_clear()
        self.pmw_error_unread = 0
        self.pmw_refresh_error_badge()
        self.pmw_show_message("错误面板已清空（主日志仍保留）")

    def pmw_copy_error_panel(self) -> None:
        """把错误面板内容复制到剪贴板（报错时最方便贴给别人看）。"""
        content = self.pmw_error_model.pltm_export_text()
        if not content.strip():
            QMessageBox.information(self, "提示", "错误面板为空")
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(content)
        self.pmw_show_message(f"已复制 {len(content.splitlines())} 条错误到剪贴板")

    def pmw_export_error_panel(self) -> None:
        """导出错误面板内容。"""
        content = self.pmw_error_model.pltm_export_text()
        if not content.strip():
            QMessageBox.information(self, "提示", "错误面板为空")
            return
        target, _ = QFileDialog.getSaveFileName(self, "导出错误", f"pms_errors_{pms_now_text('%Y%m%d_%H%M%S')}.txt", "文本文件 (*.txt)")
        if not target:
            return
        if pms_atomic_write_text(Path(target), content + "\n"):
            self.pmw_show_message(f"错误已导出：{target}")

    def pmw_export_log(self) -> None:
        """导出当前日志面板内容。"""
        content = self.pmw_log_model.pltm_export_text()
        if not content.strip():
            QMessageBox.information(self, "提示", "日志面板为空")
            return
        target, _ = QFileDialog.getSaveFileName(self, "导出日志", f"pms_log_{pms_now_text('%Y%m%d_%H%M%S')}.txt", "文本文件 (*.txt)")
        if not target:
            return
        if pms_atomic_write_text(Path(target), content + "\n"):
            self.pmw_show_message(f"日志已导出：{target}")

    def pmw_clear_log_panel(self) -> None:
        self.pmw_log_model.pltm_clear()
        self.pmw_show_message("日志面板已清空（日志文件仍保留）")

    def pmw_on_connections_updated(self, pms_records: list) -> None:
        """连接列表刷新。"""
        self.pmw_connection_model.pctm_set_records(pms_records)
        self.pmw_connection_label.setText(
            f"连接：活动 {sum(1 for item in pms_records if item.is_active)} / 共 {len(pms_records)}（来源：{self.pmw_tracker.pct_source}）"
        )
        self.pmw_label_connections.setText(f"连接：{sum(1 for item in pms_records if item.is_active)}")

    def pmw_on_traffic_updated(self, pms_payload: dict) -> None:
        """流量统计刷新（速览条 + 状态栏 + 统计面板）。"""
        proxy_up = pms_safe_int(pms_payload.get("proxy_up"), 0)
        proxy_down = pms_safe_int(pms_payload.get("proxy_down"), 0)
        direct_up = pms_safe_int(pms_payload.get("direct_up"), 0)
        direct_down = pms_safe_int(pms_payload.get("direct_down"), 0)
        speeds = pms_payload.get("speed") or {}
        self.pmw_label_traffic.setText(f"代理：{pms_format_bytes(proxy_up)} ↑ / {pms_format_bytes(proxy_down)} ↓")
        self.pmw_label_speed.setText(f"速度：↑ {pms_format_speed(speeds.get('proxy_up', 0.0))} ↓ {pms_format_speed(speeds.get('proxy_down', 0.0))}")
        self.pmw_status_proxy.setText(f"代理：{pms_format_bytes(proxy_up)} ↑ / {pms_format_bytes(proxy_down)} ↓")
        self.pmw_status_direct.setText(f"直连：{pms_format_bytes(direct_up)} ↑ / {pms_format_bytes(direct_down)} ↓")
        self.pmw_status_speed.setText(f"速度：{pms_format_speed(speeds.get('proxy_down', 0.0))}")
        labels = self.pmw_stats_labels
        labels["proxy_up"].setText(pms_format_bytes(proxy_up))
        labels["proxy_down"].setText(pms_format_bytes(proxy_down))
        labels["direct_up"].setText(pms_format_bytes(direct_up))
        labels["direct_down"].setText(pms_format_bytes(direct_down))
        labels["speed_up"].setText(pms_format_speed(speeds.get("proxy_up", 0.0)))
        labels["speed_down"].setText(pms_format_speed(speeds.get("proxy_down", 0.0)))
        labels["connections"].setText(str(pms_payload.get("active_connections", 0)))
        labels["source"].setText(str(pms_payload.get("source") or "-"))
        if pms_payload.get("error"):
            labels["source"].setText(str(pms_payload.get("source")) + "（" + str(pms_payload.get("error")) + "）")
        # v1.8.0 事件推送：流量是每秒级的高频数据，至少隔 5 秒才推一次，避免把事件环和客户端冲垮
        now = time.monotonic()
        if now - self.pmw_event_traffic_at >= 5.0:
            self.pmw_event_traffic_at = now
            self.pmw_emit_event("traffic.updated", {
                "proxy_up": proxy_up,
                "proxy_down": proxy_down,
                "direct_up": direct_up,
                "direct_down": direct_down,
                "speed_up": float(speeds.get("proxy_up", 0.0) or 0.0),
                "speed_down": float(speeds.get("proxy_down", 0.0) or 0.0),
                "active_connections": int(pms_payload.get("active_connections", 0) or 0),
                "source": str(pms_payload.get("source") or "-"),
            })

    def pmw_close_selected_connection(self) -> None:
        """关闭 Clash API 中的选中连接（Xray 日志模式下无法关闭单条）。"""
        client = self.pmw_traffic.pts_clash_client
        if client is None:
            QMessageBox.information(self, "不支持", "当前统计源不支持关闭单条连接（仅 Clash API 模式支持）")
            return
        rows = self.pmw_connection_view.selectionModel().selectedRows() if self.pmw_connection_view.selectionModel() else []
        if not rows:
            return
        record = self.pmw_connection_model.pctm_row_at(rows[0].row())
        if record is None or not record.conn_uid:
            return
        if client.pca_close_connection(record.conn_uid):
            self.pmw_show_message("已关闭该连接")
        else:
            QMessageBox.warning(self, "关闭失败", "核心未接受关闭请求")

    def pmw_close_all_connections(self) -> None:
        """关闭全部连接。"""
        client = self.pmw_traffic.pts_clash_client
        if client is None:
            QMessageBox.information(self, "不支持", "当前统计源不支持关闭连接（仅 Clash API 模式支持）")
            return
        if client.pca_close_all_connections():
            self.pmw_show_message("已请求关闭全部连接")
        else:
            QMessageBox.warning(self, "关闭失败", "核心未接受关闭请求")
