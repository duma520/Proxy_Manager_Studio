#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流量与连接统计

轮询统计源，维护累计流量 / 实时速率 / 活动连接数，并定期写入流量采样。
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Optional,
)
from PySide6.QtCore import (
    QObject,
    QTimer,
    Signal,
)
import logging
import time

from pms.constants import (
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
    PMS_LOGGER_NAME,
    PMS_STATS_SOURCE_AUTO,
    PMS_STATS_SOURCE_CLASH,
    PMS_STATS_SOURCE_XRAY,
    PMS_TRAFFIC_TICK_MS,
)
from pms.utils import pms_is_port_available, pms_safe_int
from pms.database import PmsNodeRepository
from pms.statsapi import (
    PmsClashApiClient,
    PmsXrayStatsClient,
)
from pms.logparse import PmsConnectionTracker

class PmsTrafficMonitorService(QObject):
    """真实流量统计：sing-box 走 Clash API，Xray 走 Stats API；同时驱动连接面板。"""

    pts_traffic_updated = Signal(dict)
    pts_source_changed = Signal(str)

    def __init__(self, pms_settings: "PmsSettingsManager", pms_tracker: PmsConnectionTracker, pms_parent: Optional[QObject] = None) -> None:
        super().__init__(pms_parent)
        self.pts_settings = pms_settings
        self.pts_tracker = pms_tracker
        self.pts_logger = logging.getLogger(PMS_LOGGER_NAME + ".traffic")
        self.pts_timer = QTimer(self)
        self.pts_timer.timeout.connect(self.pts_tick)
        self.pts_clash_client: Optional[PmsClashApiClient] = None
        self.pts_xray_client: Optional[PmsXrayStatsClient] = None
        self.pts_core_kind = ""
        self.pts_clash_port = 0
        self.pts_stats_port = 0
        self.pts_totals = {"proxy_up": 0, "proxy_down": 0, "direct_up": 0, "direct_down": 0}
        self.pts_speed = {"proxy_up": 0.0, "proxy_down": 0.0, "direct_up": 0.0, "direct_down": 0.0}
        self.pts_last_clash_totals = (0, 0)
        self.pts_last_tick = 0.0
        self.pts_sample_counter = 0
        self.pts_source_text = "未启动"
        self.pts_count_first_sample = False
        self.pts_repo: Optional[PmsNodeRepository] = None
        # v1.9.41：Clash API 迟到的补挂（端口一就绪就试，最多 200 拍）
        self.pts_client_retry_left = 200
        self.pts_client_retry_tick = 0

    def pts_attach_repository(self, pms_repo: PmsNodeRepository) -> None:
        """挂上仓储层（用于定期写入流量采样）。"""
        self.pts_repo = pms_repo

    # ------------------------------------------------------------------ 生命周期
    def pts_configure(self, pms_core_kind: str, pms_core_path: Optional[Path], pms_ports: dict[str, int]) -> None:
        """核心启动后配置统计源。"""
        self.pts_core_kind = str(pms_core_kind)
        self.pts_client_retry_left = 200     # v1.9.41：每次核心启动都重新给 200 拍补挂机会
        self.pts_client_retry_tick = 0
        self.pts_clash_port = int(pms_ports.get("clash_api") or 0)
        self.pts_stats_port = int(pms_ports.get("stats_api") or 0)
        source_setting = self.pts_settings.psm_get_str("stats.source", PMS_STATS_SOURCE_AUTO)
        self.pts_clash_client = None
        self.pts_xray_client = None
        if source_setting in (PMS_STATS_SOURCE_AUTO, PMS_STATS_SOURCE_CLASH) and pms_core_kind == PMS_CORE_KIND_SINGBOX:
            if self.pts_clash_port:
                client = PmsClashApiClient(self.pts_clash_port, pms_logger=self.pts_logger)
                if client.pca_is_available(2.5):
                    self.pts_clash_client = client
        if self.pts_clash_client is None and source_setting in (PMS_STATS_SOURCE_AUTO, PMS_STATS_SOURCE_XRAY) and pms_core_kind == PMS_CORE_KIND_XRAY:
            if pms_core_path is not None and self.pts_stats_port:
                client = PmsXrayStatsClient(Path(pms_core_path), self.pts_stats_port, pms_logger=self.pts_logger)
                if client.pxs_is_available():
                    self.pts_xray_client = client
        if self.pts_clash_client is not None:
            self.pts_set_source("Clash API（连接列表与流量）")
        elif self.pts_xray_client is not None:
            self.pts_set_source("Xray Stats API（流量统计）")
        else:
            self.pts_set_source("日志统计（仅连接计数）")

    def pts_set_source(self, pms_text: str) -> None:
        self.pts_source_text = pms_text
        self.pts_source_changed.emit(pms_text)

    def pts_ensure_clash_client(self) -> bool:
        """采样前补挂 Clash API 客户端（v1.9.41）。

        ★ 为什么需要：核心刚起来时 Clash API 端口往往还没监听（实测 sing-box 启动后要 1–3 秒，
        而且端口还可能被占用后换端口）。`pts_configure()` 只在那一刻探一次 2.5 秒，
        错过就**永久**退回「日志统计（仅连接计数）」——界面上代理流量永远是 0 B，
        用户看到的「统计不到流量」就是这么来的（冒烟 e2e 也因此常年挂一项）。
        现在每次采样前补一次探测：探到就升级统计源，探不到就留着下次再试。
        """
        if self.pts_clash_client is not None or self.pts_xray_client is not None or not self.pts_clash_port:
            return False
        if self.pts_core_kind != PMS_CORE_KIND_SINGBOX:
            return False
        source_setting = self.pts_settings.psm_get_str("stats.source", PMS_STATS_SOURCE_AUTO)
        if source_setting not in (PMS_STATS_SOURCE_AUTO, PMS_STATS_SOURCE_CLASH):
            return False
        client = PmsClashApiClient(self.pts_clash_port, pms_logger=self.pts_logger)
        if not client.pca_is_available(0.4):
            return False
        self.pts_clash_client = client
        self.pts_set_source("Clash API（连接列表与流量）")
        self.pts_logger.info("统计源已升级：Clash API（端口 %d）", self.pts_clash_port)
        return True

    def pts_start(self) -> None:
        """启动轮询定时器。"""
        interval = max(300, self.pts_settings.psm_get_int("stats.interval_ms", PMS_TRAFFIC_TICK_MS))
        self.pts_timer.start(interval)
        self.pts_last_tick = time.time()
        self.pts_last_clash_totals = (0, 0)
        self.pts_totals = {"proxy_up": 0, "proxy_down": 0, "direct_up": 0, "direct_down": 0}
        self.pts_speed = {"proxy_up": 0.0, "proxy_down": 0.0, "direct_up": 0.0, "direct_down": 0.0}
        self.pts_logger.info("流量统计已启动（源：%s，间隔 %d ms）", self.pts_source_text, interval)

    def pts_stop(self) -> None:
        """停止轮询并清空。"""
        self.pts_timer.stop()
        self.pts_clash_client = None
        self.pts_xray_client = None
        self.pts_set_source("未启动")

    def pts_reset_totals(self) -> None:
        """核心（重新）启动时调用：清零累计值，并把首个采样按“自核心启动起的累计”计入。"""
        self.pts_totals = {"proxy_up": 0, "proxy_down": 0, "direct_up": 0, "direct_down": 0}
        self.pts_last_clash_totals = (0, 0)
        self.pts_count_first_sample = True

    # ------------------------------------------------------------------ 轮询
    def pts_tick(self) -> None:
        """一次统计采样。"""
        now = time.time()
        elapsed = max(0.2, now - self.pts_last_tick) if self.pts_last_tick else 1.0
        self.pts_last_tick = now
        if self.pts_clash_client is None and self.pts_xray_client is None:
            # v1.9.41：核心刚起来时补挂统计源。先用「端口有没有人在听」做**毫秒级**预检，
            # 端口起来了才做一次 HTTP 探测（最多 0.4 秒）——所以每拍都试也不贵，
            # 核心一就绪就升级统计源（冒烟 e2e 的「统计到真实代理字节」就靠这一步稳定下来）。
            if self.pts_client_retry_left > 0 and self.pts_clash_port \
                    and not pms_is_port_available(self.pts_clash_port, "127.0.0.1"):
                self.pts_client_retry_left -= 1
                self.pts_ensure_clash_client()
        payload: dict[str, Any] = {
            "source": self.pts_source_text,
            "active_connections": self.pts_tracker.pct_active_count(),
            "elapsed": elapsed,
        }
        if self.pts_clash_client is not None:
            snapshot = self.pts_clash_client.pca_get_connections()
            if snapshot is not None:
                self.pts_tracker.pct_apply_clash_snapshot(snapshot)
                upload_total = pms_safe_int(snapshot.get("uploadTotal"), 0)
                download_total = pms_safe_int(snapshot.get("downloadTotal"), 0)
                previous_up, previous_down = self.pts_last_clash_totals
                if self.pts_count_first_sample:
                    # 首个采样：Clash 的累计值就是从核心启动算起的，直接全部计入
                    delta_up = upload_total
                    delta_down = download_total
                    self.pts_count_first_sample = False
                else:
                    delta_up = max(0, upload_total - previous_up)
                    delta_down = max(0, download_total - previous_down)
                self.pts_last_clash_totals = (upload_total, download_total)
                self.pts_totals["proxy_up"] += delta_up
                self.pts_totals["proxy_down"] += delta_down
                self.pts_speed["proxy_up"] = delta_up / elapsed
                self.pts_speed["proxy_down"] = delta_down / elapsed
                self.pts_tracker.pct_evict_stale()
            else:
                payload["error"] = "Clash API 无响应"
        elif self.pts_xray_client is not None:
            up, down = self.pts_xray_client.pxs_proxy_traffic()
            self.pts_totals["proxy_up"] += up
            self.pts_totals["proxy_down"] += down
            self.pts_speed["proxy_up"] = up / elapsed
            self.pts_speed["proxy_down"] = down / elapsed
            in_up, in_down = self.pts_xray_client.pxs_inbound_total()
            direct_up = max(0, in_up - up)
            direct_down = max(0, in_down - down)
            if self.pts_settings.psm_get_bool("stats.count_direct_traffic", True):
                self.pts_totals["direct_up"] += direct_up
                self.pts_totals["direct_down"] += direct_down
            self.pts_speed["direct_up"] = direct_up / elapsed
            self.pts_speed["direct_down"] = direct_down / elapsed
            self.pts_tracker.pct_evict_stale()
        else:
            self.pts_speed["proxy_up"] = 0.0
            self.pts_speed["proxy_down"] = 0.0
        payload.update(self.pts_totals)
        payload.update({"speed": dict(self.pts_speed)})
        self.pts_traffic_updated.emit(payload)
        self.pts_sample_counter += 1
        if self.pts_repo is not None and self.pts_sample_counter % 10 == 0:
            try:
                self.pts_repo.pnr_record_traffic_sample({
                    "proxy_up": self.pts_totals["proxy_up"],
                    "proxy_down": self.pts_totals["proxy_down"],
                    "direct_up": self.pts_totals["direct_up"],
                    "direct_down": self.pts_totals["direct_down"],
                    "active_nodes": payload["active_connections"],
                })
            except Exception as exc:  # noqa: BLE001 - 采样失败不影响统计
                self.pts_logger.debug("写入流量采样失败：%s", exc)
