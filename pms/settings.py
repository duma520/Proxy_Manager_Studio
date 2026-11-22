#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置仓库

pms_settings.json 的加载/纠正/防抖原子保存/导入导出/分区块重置（改动即保存，绝不丢设置）。
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Optional,
    Sequence,
)
from PySide6.QtCore import (
    QMetaObject,
    QObject,
    QThread,
    QTimer,
    Qt,
    Signal,
    Slot,
)
import copy
import hashlib
import json
import logging
import os
import shutil
import time

from pms.coremeta import pms_version_tuple
from pms.constants import (
    PMS_API_AUTH_AUTO,
    PMS_APP_VERSION,
    PMS_API_AUTH_TITLES,
    PMS_API_DEFAULT_HOST,
    PMS_API_DEFAULT_PORT,
    PMS_AUTOSWITCH_DEFAULT_FAIL_THRESHOLD,
    PMS_AUTOSWITCH_DEFAULT_INTERVAL_SECONDS,
    PMS_AUTOSWITCH_DETECT_TCP,
    PMS_AUTOSWITCH_DETECT_TITLES,
    PMS_AUTOSWITCH_MAX_FAIL_THRESHOLD,
    PMS_AUTOSWITCH_MAX_INTERVAL_SECONDS,
    PMS_AUTOSWITCH_MIN_FAIL_THRESHOLD,
    PMS_AUTOSWITCH_MIN_INTERVAL_SECONDS,
    PMS_COLUMN_INDEX,
    PMS_COLUMN_TITLES,
    PMS_CORE_KIND_AUTO,
    PMS_CORE_KIND_TITLES,
    PMS_DEFAULT_SPEED_TEST_URL,
    PMS_DEFAULT_TEST_URLS,
    PMS_DEFAULT_TUN_INTERFACE,
    PMS_LOGGER_NAME,
    PMS_LOG_LEVEL_TITLES,
    PMS_MAX_CONNECTION_ROWS,
    PMS_MAX_LOG_LINES,
    PMS_ROUTING_MODE_RULE,
    PMS_ROUTING_MODE_TITLES,
    PMS_RULESET_SOURCE_LOCAL,
    PMS_RULESET_SOURCE_TITLES,
    PMS_SETTINGS_DEFAULTS,
    PMS_SETTINGS_FILENAME,
    PMS_SETTINGS_SAVE_DEBOUNCE_MS,
    PMS_SORT_SCOPE_ALL,
    PMS_SORT_SCOPE_TITLES,
    PMS_SPEED_SOURCE_CLOUDFLARE,
    PMS_SPEED_SOURCE_CUSTOM,
    PMS_SPEED_SOURCE_TITLES,
    PMS_STATS_SOURCE_AUTO,
    PMS_STATS_SOURCE_TITLES,
    PMS_SYSTEM_PROXY_MODE_FIXED,
    PMS_SYSTEM_PROXY_MODE_PAC,
    PMS_TELEGRAM_PROXY_AUTO,
    PMS_TELEGRAM_PROXY_MODE_TITLES,
    PMS_TEST_MODE_TITLES,
    PMS_TEST_MODE_URL,
    PMS_THEME_DARK,
    PMS_THEME_TITLES,
    PMS_TRAFFIC_TICK_MS,
    PMS_TUN_STACK_TITLES,
)
from pms.telegram import (  # 抓取模式常量放在 telegram.py：constants.py 已顶 1000 行红线
    PMS_TELEGRAM_FETCH_MODE_FULL,
    PMS_TELEGRAM_FETCH_MODES,
    PMS_TELEGRAM_MAX_PAGES,
)
from pms.tor_bridge import PMS_TOR_BRIDGE_TYPES  # Tor 桥接类型白名单放在 tor_bridge.py（同上）
from pms.utils import (
    pms_atomic_write_text,
    pms_dedupe_keep_order,
    pms_json_pretty,
    pms_now_text,
    pms_safe_bool,
    pms_safe_float,
    pms_safe_int,
)

class PmsSettingsManager(QObject):
    """设置仓库：读取/写入 pms_settings.json，改动即保存（250ms 防抖 + 原子写盘）。"""

    psm_value_changed = Signal(str, object)
    psm_values_reloaded = Signal()

    def __init__(self, pms_settings_file: Optional[Path] = None, pms_parent: Optional[QObject] = None) -> None:
        super().__init__(pms_parent)
        self.psm_path = Path(pms_settings_file) if pms_settings_file is not None else Path(PMS_SETTINGS_FILENAME)
        self.psm_logger = logging.getLogger(PMS_LOGGER_NAME + ".settings")
        self.psm_values: dict[str, Any] = dict(PMS_SETTINGS_DEFAULTS)
        self.psm_unknown_keys: dict[str, Any] = {}
        self.psm_loaded: bool = False
        self.psm_save_timer = QTimer(self)
        self.psm_save_timer.setSingleShot(True)
        self.psm_save_timer.setInterval(PMS_SETTINGS_SAVE_DEBOUNCE_MS)
        self.psm_save_timer.timeout.connect(self.psm_save_to_disk)
        self.psm_load_from_disk()
        self.psm_history = PmsSettingsHistory(self.psm_path.with_name("pms_settings_history.json"))
        self.psm_check_and_migrate()
        # v1.9.17：启动时先记一条基线快照——否则「只改过一次设置」时，历史里只有改动后的那一条，
        # 「恢复上次修改」就没有可回退的上一状态（这正是用户抱怨“想退回上次修改”的缺口）。
        self.psm_capture_snapshot("启动时")

    # ------------------------------------------------------------------ 加载与保存
    def psm_load_from_disk(self) -> bool:
        """读取设置文件并与默认值合并：补齐新项、保留未知项（向前兼容）。"""
        merged: dict[str, Any] = copy.deepcopy(PMS_SETTINGS_DEFAULTS)
        unknown: dict[str, Any] = {}
        existed = self.psm_path.is_file()
        if existed:
            try:
                raw = json.loads(self.psm_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.psm_logger.error("设置文件解析失败，已回退默认值：%s", exc)
                raw = None
                try:
                    broken = self.psm_path.with_suffix(".broken.json")
                    shutil.copy2(self.psm_path, broken)
                    self.psm_logger.warning("损坏的设置已备份为：%s", broken.name)
                except OSError:
                    pass
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key in PMS_SETTINGS_DEFAULTS:
                        merged[key] = value
                    else:
                        unknown[key] = value
        self.psm_values = merged
        self.psm_unknown_keys = unknown
        self.psm_normalize()
        if not existed:
            self.psm_save_to_disk()
        self.psm_loaded = True
        self.psm_logger.info("设置已加载：%s（未知项 %d 个已保留）", self.psm_path, len(unknown))
        return existed

    def psm_check_and_migrate(self) -> str:
        """启动迁移检查（第 13 项）：比对上次记录的版本，跨版本则执行迁移并落盘新版本号。

        目前迁移以“记录版本 + 补默认值（psm_normalize 已做）”为主；数据库 schema 的结构迁移
        在 database.py 的 pdb_migrate() 中按 schema_version 执行。返回迁移说明，无则空串。
        """
        last = str(self.psm_values.get("app.version_last_seen") or "").strip()
        current = PMS_APP_VERSION
        if last == current:
            return ""
        self._psm_migrate_telegram_column(last, current)
        note = f"版本迁移：{last or '未知'} → {current}"
        self.psm_logger.info(note)
        self.psm_values["app.version_last_seen"] = current
        # 同步把新版本号落盘（直接保存，不走防抖定时器，确保启动即生效）。
        try:
            self.psm_save_to_disk()
        except OSError as exc:
            self.psm_logger.warning("迁移后版本号落盘失败（下次启动会再试）：%s", exc)
        return note

    def _psm_migrate_telegram_column(self, pms_last: str, pms_current: str) -> None:
        """v1.9.60：电报列从末尾（index 12）移到「测速」之后（index 8）。

        老用户（v1.9.59）的 `ui.table_column_widths` / `ui.table_column_visible` / `ui.table_sort_column`
        都是**按列索引持久化**的，列序一改会让保存值错位（电报宽落到备注、排序指到备注、显隐全串）。
        此处用版本门控做一次性的按索引重排，确保幂等：仅当列表仍是旧 13 项、且上次版本早于 1.9.60 才执行；
        首装（last 为空）或跳版均安全，且 normalize 已把长度不符的列表重置为全默认，不会重复位移。
        """
        last_tuple = pms_version_tuple(pms_last)
        if not last_tuple:
            return  # 首装：normalize 已落成全默认，无需迁移
        if last_tuple >= pms_version_tuple("1.9.60"):
            return  # 已经迁移过，幂等退出
        old_index = 12  # 电报列旧位置（v1.9.59）
        if len(PMS_COLUMN_TITLES) != 13 or PMS_COLUMN_TITLES[8] != "电报":
            return  # 列序定义与预期不符，放弃重排以免错杀

        for key in ("ui.table_column_widths", "ui.table_column_visible"):
            raw = self.psm_values.get(key)
            if not isinstance(raw, list) or len(raw) != 13:
                continue  # normalize 已重置为默认长度，无需迁移
            moved = list(raw[:8]) + [raw[old_index]] + list(raw[8:old_index])
            if moved != raw:
                self.psm_values[key] = moved

        sort_col = self.psm_values.get("ui.table_sort_column")
        if isinstance(sort_col, int):
            if sort_col == old_index:
                self.psm_values["ui.table_sort_column"] = 8
            elif 8 <= sort_col <= 11:
                self.psm_values["ui.table_sort_column"] = sort_col + 1

    def psm_normalize(self) -> None:
        """把越界、类型异常的值纠正到合法区间，避免旧配置或手改文件破坏程序。"""
        values = self.psm_values
        values["general.language"] = "zh_CN"
        values["core.kind"] = values.get("core.kind") if values.get("core.kind") in PMS_CORE_KIND_TITLES else PMS_CORE_KIND_AUTO
        values["core.log_level"] = values.get("core.log_level") if values.get("core.log_level") in PMS_LOG_LEVEL_TITLES else "info"
        values["core.restart_max_times"] = pms_safe_int(values.get("core.restart_max_times"), 5, 0, 100)
        values["core.mux_concurrency"] = pms_safe_int(values.get("core.mux_concurrency"), 8, 1, 128)
        for key in (
            "general.auto_start_core",
            "general.auto_connect_last_node",
            "general.minimize_to_tray",
            "general.close_to_tray",
            "general.confirm_on_exit",
            "general.auto_start_with_windows",
            "general.notify_on_core_error",
            "general.notify_on_subscription_done",
            "general.single_instance",
            "general.check_core_on_launch",
            "core.access_log",
            "core.auto_restart",
            "core.kill_existing_on_start",
            "core.keep_config_file",
            "core.disable_geo_assets",
            "core.mux_enabled",
            "core.tcp_fast_open",
            "core.sniff_override_dest",
            "inbound.mixed_enabled",
            "inbound.auto_pick_port",
            "inbound.http_enabled",
            "inbound.socks_enabled",
            "inbound.udp_enabled",
            "inbound.sniff_enabled",
            "inbound.auth_enabled",
            "inbound.allow_lan",
            "system_proxy.restore_on_exit",
            "tun.enabled",
            "tun.auto_route",
            "tun.strict_route",
            "tun.ipv6",
            "routing.block_ads_enabled",
            "routing.bypass_private_ips",
            "routing.use_remote_ruleset",
            "test.remove_duplicates_before_test",
            "test.sort_after_test",
            "test.sort_after_speed",
            "test.fail_show_core",
            "test.speed_result_dialog",
            "test.auto_test_on_start",
            "test.only_test_enabled",
            "subscription.update_on_launch",
            "subscription.auto_update_enabled",
            "subscription.use_env_api_key",
            "subscription.keep_previous_on_failure",
            "subscription.remove_duplicates",
            "subscription.prune_removed",
            "subscription.use_system_proxy",
            "ui.show_connection_panel",
            "ui.show_quick_stats",
            "ui.show_status_traffic",
            "ui.log_show_time",
            "ui.log_auto_scroll",
            "ui.remember_geometry",
            "ui.compact_toolbar",
            "database.auto_vacuum",
            "database.keep_latency_history",
            "database.keep_connection_history",
            "database.backup_on_exit",
            "stats.count_direct_traffic",
            "advanced.debug_mode",
            "advanced.clipboard_watch",
            "advanced.dedup_by_server_port",
            "advanced.import_skip_invalid",
            "advanced.show_import_report",
            "api.enabled",
            "api.web_panel_enabled",
            "api.write_enabled",
            "autoswitch.enabled",
            "autoswitch.notify",
        ):
            values[key] = pms_safe_bool(values.get(key), bool(PMS_SETTINGS_DEFAULTS.get(key, False)))

        values["inbound.listen_address"] = str(values.get("inbound.listen_address") or "127.0.0.1").strip() or "127.0.0.1"
        values["inbound.mixed_port"] = pms_safe_int(values.get("inbound.mixed_port"), 30808, 1, 65535)
        values["inbound.http_port"] = pms_safe_int(values.get("inbound.http_port"), 20809, 1, 65535)
        values["inbound.socks_port"] = pms_safe_int(values.get("inbound.socks_port"), 20810, 1, 65535)
        values["routing.mode"] = values.get("routing.mode") if values.get("routing.mode") in PMS_ROUTING_MODE_TITLES else PMS_ROUTING_MODE_RULE
        # v1.9.49：系统代理模式只认 fixed / pac，脏值一律回落 fixed（= 改动前的固定代理行为，绝不静默切到 PAC）。
        pms_sysproxy_mode = str(values.get("system_proxy.mode") or "").strip().lower()
        values["system_proxy.mode"] = pms_sysproxy_mode if pms_sysproxy_mode in (PMS_SYSTEM_PROXY_MODE_FIXED, PMS_SYSTEM_PROXY_MODE_PAC) else PMS_SYSTEM_PROXY_MODE_FIXED
        # 备份键必须存在（还原逻辑按「键在不在」决定写不写）：老的配置文件里没有这一项，
        # 缺失时补默认空串，还原时按空串处理成「删除 AutoConfigURL」，与旧版行为一致。
        values["system_proxy.original_auto_config_url"] = str(values.get("system_proxy.original_auto_config_url") or "")
        # PAC 直连域名：逐项去空白、丢空项、小写、去重（保留顺序）
        pms_pac_raw = values.get("system_proxy.pac_direct_domains")
        if isinstance(pms_pac_raw, str):
            pms_pac_raw = [pms_pac_raw]
        if not isinstance(pms_pac_raw, (list, tuple)):
            pms_pac_raw = []
        values["system_proxy.pac_direct_domains"] = pms_dedupe_keep_order(
            [str(item).strip().lower().lstrip(".") for item in pms_pac_raw if str(item).strip()]
        )
        values["routing.domain_strategy"] = values.get("routing.domain_strategy") if values.get("routing.domain_strategy") in ("AsIs", "IPIfNonMatch", "IPOnDemand") else "IPIfNonMatch"
        values["routing.ruleset_source"] = values.get("routing.ruleset_source") if values.get("routing.ruleset_source") in PMS_RULESET_SOURCE_TITLES else PMS_RULESET_SOURCE_LOCAL
        # v1.9.43：Tor 桥接出口（只用 sing-box 的 tor 出站）
        # 桥接类型只认四种（obfs4 / webtunnel / meek / snowflake，另收 meek_lite），脏值回落 obfs4
        pms_tor_type = str(values.get("tor.bridge_type") or "").strip().lower()
        values["tor.bridge_type"] = pms_tor_type if pms_tor_type in PMS_TOR_BRIDGE_TYPES else PMS_TOR_BRIDGE_TYPES[0]
        # 桥接行：逐行去空白、丢空行、去重（**保留原样**，解析交给 pms/tor_bridge.py，脏行不在这里吞掉）
        raw_lines = values.get("tor.bridge_lines")
        if isinstance(raw_lines, str):
            raw_lines = [raw_lines]
        cleaned: list[str] = []
        for item in (raw_lines if isinstance(raw_lines, (list, tuple)) else []):
            text = str(item or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        values["tor.bridge_lines"] = cleaned
        # v1.9.48：未验活候选（保存前照样真握手验过，只是没通过 → 单存一份，**绝不混进 bridge_lines**）
        raw_unverified = values.get("tor.unverified_lines")
        if isinstance(raw_unverified, str):
            raw_unverified = [raw_unverified]
        kept: list[str] = []
        for item in (raw_unverified if isinstance(raw_unverified, (list, tuple)) else []):
            text = str(item or "").strip()
            if text and text not in kept:
                kept.append(text)
        values["tor.unverified_lines"] = kept
        values["tor.keep_unverified"] = bool(values.get("tor.keep_unverified"))
        values["tor.tor_dir"] = str(values.get("tor.tor_dir") or "").strip()
        values["tor.data_directory"] = str(values.get("tor.data_directory") or "").strip()
        values["tor.bundle_version"] = str(values.get("tor.bundle_version") or "").strip()
        values["test.mode"] = values.get("test.mode") if values.get("test.mode") in PMS_TEST_MODE_TITLES else PMS_TEST_MODE_URL
        # v1.9.12 / v1.9.14：自动排序范围（all / page），脏值一律回落 all（全量），免得排出来只在页内生效
        for pms_sort_key in ("test.sort_scope_latency", "test.sort_scope_speed", "ui.header_sort_scope"):
            pms_sort_value = values.get(pms_sort_key)
            values[pms_sort_key] = pms_sort_value if isinstance(pms_sort_value, str) and pms_sort_value in PMS_SORT_SCOPE_TITLES else PMS_SORT_SCOPE_ALL
        values["test.timeout_ms"] = pms_safe_int(values.get("test.timeout_ms"), 5000, 500, 60000)
        values["test.tcp_timeout_ms"] = pms_safe_int(values.get("test.tcp_timeout_ms"), 3000, 200, 60000)
        values["test.concurrency"] = pms_safe_int(values.get("test.concurrency"), 16, 1, 256)
        values["test.retry_times"] = pms_safe_int(values.get("test.retry_times"), 1, 0, 5)
        values["test.auto_test_interval_minutes"] = pms_safe_int(values.get("test.auto_test_interval_minutes"), 0, 0, 10080)
        # v1.9.37：节点健康检查（周期 1~30 天 / 阈值 1~10 次 / 每批 50~5000 / 抖动 0~1440 分钟）
        values["health.enabled"] = pms_safe_bool(values.get("health.enabled"), True)
        values["health.notify"] = pms_safe_bool(values.get("health.notify"), True)
        values["health.period_days"] = pms_safe_int(values.get("health.period_days"), 7, 1, 30)
        values["health.fail_threshold"] = pms_safe_int(values.get("health.fail_threshold"), 3, 1, 10)
        values["health.batch_size"] = pms_safe_int(values.get("health.batch_size"), 300, 50, 5000)
        values["health.jitter_minutes"] = pms_safe_int(values.get("health.jitter_minutes"), 30, 0, 1440)
        values["speed.duration_seconds"] = pms_safe_int(values.get("speed.duration_seconds"), 10, 3, 300)
        values["speed.min_samples"] = pms_safe_int(values.get("speed.min_samples"), 3, 1, 60)
        values["speed.concurrency"] = pms_safe_int(values.get("speed.concurrency"), 4, 1, 32)
        values["speed.per_node_limit_mb"] = pms_safe_int(values.get("speed.per_node_limit_mb"), 30, 1, 2048)
        values["speed.size_mb"] = pms_safe_int(values.get("speed.size_mb"), 10, 1, 1024)
        # 兼容旧配置：老版本只有 speed.url，若用户手改过则判为 custom，避免被预设源静默覆盖
        if values.get("speed.source") not in PMS_SPEED_SOURCE_TITLES and values.get("speed.source") != PMS_SPEED_SOURCE_CUSTOM:
            legacy_url = str(values.get("speed.url") or "").strip()
            values["speed.source"] = PMS_SPEED_SOURCE_CUSTOM if legacy_url and legacy_url != PMS_DEFAULT_SPEED_TEST_URL else PMS_SPEED_SOURCE_CLOUDFLARE
        # v1.9.2：页数上限 10 → 50（电报每页约 18–20 条消息）
        values["telegram.pages"] = pms_safe_int(values.get("telegram.pages"), 1, 1, PMS_TELEGRAM_MAX_PAGES)
        values["telegram.fetch_mode"] = values.get("telegram.fetch_mode") if values.get("telegram.fetch_mode") in PMS_TELEGRAM_FETCH_MODES else PMS_TELEGRAM_FETCH_MODE_FULL
        values["telegram.proxy_mode"] = values.get("telegram.proxy_mode") if values.get("telegram.proxy_mode") in PMS_TELEGRAM_PROXY_MODE_TITLES else PMS_TELEGRAM_PROXY_AUTO
        # v1.9.73：auto 模式下的自定义退让次序；非法 token 过滤、去重保序，过滤后为空退回默认次序
        from pms.telegram import pms_telegram_parse_order  # 局部导入，避免 settings 与 telegram 顶层循环
        _order_tokens = pms_telegram_parse_order(values.get("telegram.proxy_order") or "")
        values["telegram.proxy_order"] = (
            ",".join(_order_tokens) if _order_tokens else PMS_TELEGRAM_DEFAULT_PROXY_ORDER
        )
        values["telegram.external_proxy"] = str(values.get("telegram.external_proxy") or "").strip()
        values["telegram.timeout_seconds"] = pms_safe_int(values.get("telegram.timeout_seconds"), 20, 3, 120)
        values["subscription.auto_update_interval_minutes"] = pms_safe_int(values.get("subscription.auto_update_interval_minutes"), 360, 10, 20160)
        values["subscription.timeout_seconds"] = pms_safe_int(values.get("subscription.timeout_seconds"), 25, 3, 300)
        values["ui.theme"] = values.get("ui.theme") if values.get("ui.theme") in PMS_THEME_TITLES else PMS_THEME_DARK
        values["ui.ui_scale"] = max(0.75, min(2.0, pms_safe_float(values.get("ui.ui_scale"), 1.0)))
        values["ui.table_row_height"] = pms_safe_int(values.get("ui.table_row_height"), 26, 18, 64)
        values["ui.latency_bar_height"] = pms_safe_int(values.get("ui.latency_bar_height"), 5, 0, 14)
        values["ui.table_font_size"] = max(7.0, min(20.0, pms_safe_float(values.get("ui.table_font_size"), 9.5)))
        values["ui.log_font_size"] = max(7.0, min(20.0, pms_safe_float(values.get("ui.log_font_size"), 9.0)))
        values["ui.table_sort_column"] = pms_safe_int(values.get("ui.table_sort_column"), PMS_COLUMN_INDEX, 0, len(PMS_COLUMN_TITLES) - 1)
        values["ui.table_sort_order"] = 1 if pms_safe_int(values.get("ui.table_sort_order"), 0) >= 1 else 0
        values["ui.log_max_lines"] = pms_safe_int(values.get("ui.log_max_lines"), 2000, 100, PMS_MAX_LOG_LINES)
        values["database.max_connection_rows"] = pms_safe_int(values.get("database.max_connection_rows"), PMS_MAX_CONNECTION_ROWS, 50, 5000)
        values["database.latency_history_days"] = pms_safe_int(values.get("database.latency_history_days"), 30, 1, 3650)
        values["database.connection_history_days"] = pms_safe_int(values.get("database.connection_history_days"), 7, 1, 3650)
        values["stats.source"] = values.get("stats.source") if values.get("stats.source") in PMS_STATS_SOURCE_TITLES else PMS_STATS_SOURCE_AUTO
        values["stats.interval_ms"] = pms_safe_int(values.get("stats.interval_ms"), PMS_TRAFFIC_TICK_MS, 300, 10000)
        values["stats.clash_api_port"] = pms_safe_int(values.get("stats.clash_api_port"), 0, 0, 65535)
        values["stats.xray_api_port"] = pms_safe_int(values.get("stats.xray_api_port"), 0, 0, 65535)
        values["advanced.max_log_file_mb"] = pms_safe_int(values.get("advanced.max_log_file_mb"), 8, 1, 512)
        values["advanced.settings_history_max"] = pms_safe_int(values.get("advanced.settings_history_max"), 50, 5, 500)
        # v1.9.18：GitHub 仓库订阅的三个数字框（文件数 / 总字节预算 / 节点上限）夹取到合理范围
        values["subscription.github_max_nodes"] = pms_safe_int(values.get("subscription.github_max_nodes"), 500, 50, 200000)
        values["subscription.github_max_files"] = pms_safe_int(values.get("subscription.github_max_files"), 20, 1, 200)
        values["subscription.github_max_total_mb"] = pms_safe_int(values.get("subscription.github_max_total_mb"), 24, 1, 512)
        # v1.9.17：保留条数改了立刻作用到快照仓库（并顺手裁掉多余的旧快照）
        if getattr(self, "psm_history", None) is not None:
            self.psm_history.psh_max = int(values["advanced.settings_history_max"])
            self.psm_history.psh_trim()
        values["tun.mtu"] = pms_safe_int(values.get("tun.mtu"), 9000, 1280, 65535)
        values["tun.stack"] = values.get("tun.stack") if values.get("tun.stack") in PMS_TUN_STACK_TITLES else "mixed"
        values["tun.interface_name"] = str(values.get("tun.interface_name") or PMS_DEFAULT_TUN_INTERFACE).strip() or PMS_DEFAULT_TUN_INTERFACE
        # 本地 HTTP 接口（v1.6.0）：端口夹在 1–65535，鉴权模式不在三选一里就回落自动生成
        values["api.host"] = str(values.get("api.host") or PMS_API_DEFAULT_HOST).strip() or PMS_API_DEFAULT_HOST
        values["api.port"] = pms_safe_int(values.get("api.port"), PMS_API_DEFAULT_PORT, 1, 65535)
        values["api.auth_mode"] = values.get("api.auth_mode") if values.get("api.auth_mode") in PMS_API_AUTH_TITLES else PMS_API_AUTH_AUTO
        # 定时自动切换（v1.6.1）：间隔 10 秒 ~ 1 小时，连续失败次数 1~10，检测方式不在二选一里就回落 TCP
        values["autoswitch.interval_seconds"] = pms_safe_int(
            values.get("autoswitch.interval_seconds"), PMS_AUTOSWITCH_DEFAULT_INTERVAL_SECONDS,
            PMS_AUTOSWITCH_MIN_INTERVAL_SECONDS, PMS_AUTOSWITCH_MAX_INTERVAL_SECONDS,
        )
        values["autoswitch.fail_threshold"] = pms_safe_int(
            values.get("autoswitch.fail_threshold"), PMS_AUTOSWITCH_DEFAULT_FAIL_THRESHOLD,
            PMS_AUTOSWITCH_MIN_FAIL_THRESHOLD, PMS_AUTOSWITCH_MAX_FAIL_THRESHOLD,
        )
        values["autoswitch.detect_mode"] = (
            values.get("autoswitch.detect_mode")
            if values.get("autoswitch.detect_mode") in PMS_AUTOSWITCH_DETECT_TITLES
            else PMS_AUTOSWITCH_DETECT_TCP
        )
        for key in ("routing.bypass_domains", "routing.direct_domains", "routing.block_ad_domains"):
            self.psm_values[key] = self.psm_clean_string_list(values.get(key))
        self.psm_values["test.url_list"] = self.psm_clean_string_list(values.get("test.url_list")) or list(PMS_DEFAULT_TEST_URLS)
        self.psm_values["speed.ok_sizes"] = self.psm_clean_string_list(values.get("speed.ok_sizes"))
        widths = values.get("ui.table_column_widths")
        if not isinstance(widths, list):
            widths = []
        self.psm_values["ui.table_column_widths"] = [pms_safe_int(item, 0, 0, 4000) for item in widths[: len(PMS_COLUMN_TITLES)]]
        visible = values.get("ui.table_column_visible")
        if not isinstance(visible, list) or len(visible) != len(PMS_COLUMN_TITLES):
            visible = [True] * len(PMS_COLUMN_TITLES)
        self.psm_values["ui.table_column_visible"] = [pms_safe_bool(item, True) for item in visible]
        for key in ("ui.horizontal_splitter", "ui.vertical_splitter", "ui.dock_splitter"):
            raw = values.get(key)
            if not isinstance(raw, list):
                self.psm_values[key] = []
            else:
                self.psm_values[key] = [pms_safe_int(item, 0, 0, 20000) for item in raw[:8]]

    @staticmethod
    def psm_clean_string_list(pms_raw: Any) -> list[str]:
        """把列表/字符串设置整理成去空去重的字符串列表。"""
        if pms_raw is None:
            return []
        if isinstance(pms_raw, str):
            items = [line.strip() for line in pms_raw.replace(",", "\n").split("\n")]
        elif isinstance(pms_raw, (list, tuple, set)):
            items = [str(item).strip() for item in pms_raw]
        else:
            items = [str(pms_raw).strip()]
        return pms_dedupe_keep_order([item for item in items if item])

    def psm_save_to_disk(self, pms_label: str = "") -> bool:
        """把当前设置原子写盘（防抖计时器触发或显式调用）。

        v1.9.17：`pms_label` 会写进设置快照的标签（如「关闭设置」/「启动时」），
        方便用户在“设置历史”里认出这次改动是什么时候产生的。

        v1.9.38：环境变量 `PMS_SETTINGS_READONLY=1` 时**一律不落盘**。
        起因是一次真实事故：自检脚本 `_scaffold/launch_check_gui.py` 为了静默启动把
        `general.auto_start_core` 改成 False，跑完又把这份「测试用设置」存回了用户的真配置，
        于是用户的「启动程序后自动启动核心」被悄悄关掉（用户以为它默认是关的）。
        自检 / 探针请用临时设置文件，并设上这个环境变量兜底。
        """
        if os.environ.get("PMS_SETTINGS_READONLY") == "1":
            self.psm_logger.info("PMS_SETTINGS_READONLY=1：本次不写设置文件（%s）", self.psm_path)
            return False
        payload = dict(self.psm_values)
        payload.update(self.psm_unknown_keys)
        text = pms_json_pretty(payload)
        if pms_atomic_write_text(self.psm_path, text):
            self.psm_logger.debug("设置已保存：%s", self.psm_path)
            # v1.9.0：设置历史（第 15 项）——按内容去重记录快照，仅在“确有变化”时新增，
            # 不会因防抖/重复保存而刷屏；异常被吞掉，绝不影响主保存流程。
            try:
                label = str(pms_label or "自动保存")
                self.psm_history.psh_capture(self.psm_snapshot(), pms_label=label, pms_reason=label)
            except Exception:  # noqa: BLE001
                pass
            return True
        self.psm_logger.error("设置保存失败：%s", self.psm_path)
        return False

    def psm_flush(self, pms_label: str = "") -> bool:
        """立即落盘（退出、切页、关键操作前调用）。"""
        if self.psm_save_timer.isActive():
            self.psm_save_timer.stop()
        return self.psm_save_to_disk(pms_label)

    def psm_reload(self) -> None:
        """丢弃内存值，重新从磁盘加载（供“重新载入设置”按钮用）。"""
        self.psm_load_from_disk()
        self.psm_values_reloaded.emit()

    # ------------------------------------------------------------------ 读写
    def psm_get(self, pms_key: str, pms_default: Any = None) -> Any:
        if pms_key in self.psm_values:
            return self.psm_values[pms_key]
        if pms_key in PMS_SETTINGS_DEFAULTS:
            return PMS_SETTINGS_DEFAULTS[pms_key]
        return pms_default

    def psm_get_str(self, pms_key: str, pms_default: str = "") -> str:
        value = self.psm_get(pms_key, pms_default)
        return pms_default if value is None else str(value)

    def psm_get_int(self, pms_key: str, pms_default: int = 0) -> int:
        return pms_safe_int(self.psm_get(pms_key, pms_default), pms_default)

    def psm_get_float(self, pms_key: str, pms_default: float = 0.0) -> float:
        return pms_safe_float(self.psm_get(pms_key, pms_default), pms_default)

    def psm_get_bool(self, pms_key: str, pms_default: bool = False) -> bool:
        return pms_safe_bool(self.psm_get(pms_key, pms_default), pms_default)

    def psm_get_list(self, pms_key: str, pms_default: Optional[Sequence[str]] = None) -> list[str]:
        value = self.psm_get(pms_key, list(pms_default) if pms_default else [])
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        if value in (None, ""):
            return list(pms_default) if pms_default else []
        return [str(value)]

    @Slot()
    def psm_request_save(self) -> None:
        """触发防抖保存（**工作线程安全**）。

        v1.9.31 实测（用户贴的告警就是它）：`psm_save_timer` 属于创建它的主线程，
        工作线程里 `start()` 会被 Qt 拒绝并往控制台打
        `QObject::startTimer: Timers cannot be started from another thread`，
        而且**定时器根本没启动**——于是工作线程写的那项设置只能等下一次「碰巧有别的保存」
        （例如退出时的 `psm_flush`）才落盘，中间崩一次就丢了。
        所以这里分流：主线程照旧防抖；工作线程排队回主线程再启动定时器。
        """
        if QThread.currentThread() is self.thread():
            self.psm_save_timer.start()
            return
        QMetaObject.invokeMethod(self, "psm_request_save", Qt.ConnectionType.QueuedConnection)

    def psm_set(self, pms_key: str, pms_value: Any, pms_emit: bool = True) -> None:
        """写入一项设置：内存立即生效 → 触发防抖保存 → 通知界面。"""
        if pms_key not in PMS_SETTINGS_DEFAULTS and pms_key not in self.psm_unknown_keys:
            self.psm_unknown_keys[pms_key] = pms_value
        previous = self.psm_values.get(pms_key, None)
        self.psm_values[pms_key] = pms_value
        # v1.9.17：改了「设置快照保留条数」立即生效（否则要等下次启动 normalize 才裁）
        if pms_key == "advanced.settings_history_max":
            self.psm_history.psh_max = max(5, int(pms_safe_int(pms_value, 50, 5, 500)))
            self.psm_history.psh_trim()
        self.psm_request_save()
        if pms_emit and previous != pms_value:
            self.psm_value_changed.emit(pms_key, pms_value)

    def psm_set_many(self, pms_items: dict[str, Any], pms_emit: bool = True) -> None:
        """批量写入，只触发一次保存与一次通知。"""
        changed: list[tuple[str, Any]] = []
        for key, value in pms_items.items():
            previous = self.psm_values.get(key, None)
            if key not in PMS_SETTINGS_DEFAULTS:
                self.psm_unknown_keys[key] = value
            self.psm_values[key] = value
            if previous != value:
                changed.append((key, value))
        self.psm_request_save()
        if pms_emit:
            for key, value in changed:
                self.psm_value_changed.emit(key, value)

    def psm_snapshot(self) -> dict[str, Any]:
        """返回设置副本（导出 / 文档统计用）。"""
        payload = copy.deepcopy(self.psm_values)
        payload.update(copy.deepcopy(self.psm_unknown_keys))
        return payload

    def psm_export_to_file(self, pms_target: Path) -> bool:
        """导出设置为 JSON 文件。"""
        return pms_atomic_write_text(Path(pms_target), pms_json_pretty(self.psm_snapshot()))

    def psm_import_from_file(self, pms_source: Path) -> tuple[bool, str]:
        """从 JSON 文件导入设置（只接受 dict，逐项覆盖并落盘）。"""
        try:
            payload = json.loads(Path(pms_source).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return False, f"读取失败：{exc}"
        if not isinstance(payload, dict):
            return False, "文件内容不是 JSON 对象"
        applied = 0
        for key, value in payload.items():
            if key in PMS_SETTINGS_DEFAULTS:
                self.psm_values[key] = value
                applied += 1
            else:
                self.psm_unknown_keys[key] = value
                applied += 1
        self.psm_normalize()
        self.psm_flush()
        self.psm_values_reloaded.emit()
        return True, f"已导入 {applied} 项设置"

    def psm_reset_section(self, pms_section: str) -> int:
        """把某个区块的设置恢复默认值，返回恢复项数。"""
        prefix = str(pms_section).rstrip(".") + "."
        count = 0
        for key, value in PMS_SETTINGS_DEFAULTS.items():
            if key.startswith(prefix):
                self.psm_values[key] = copy.deepcopy(value)
                count += 1
        self.psm_normalize()
        self.psm_flush()
        self.psm_values_reloaded.emit()
        return count

    def psm_reset_all(self) -> None:
        """全部恢复默认值（保留未知项）。"""
        self.psm_values = copy.deepcopy(PMS_SETTINGS_DEFAULTS)
        self.psm_normalize()
        self.psm_flush()
        self.psm_values_reloaded.emit()

    # ------------------------------------------------------------------ 设置历史（第 15 项）
    def psm_capture_snapshot(self, pms_label: str) -> bool:
        """按当前值记一条设置快照（内容与上一条相同则只刷新时间戳）。"""
        try:
            self.psm_history.psh_capture(self.psm_snapshot(), pms_label=str(pms_label), pms_reason=str(pms_label))
            return True
        except Exception:  # noqa: BLE001 - 快照是附加能力，失败不能影响主流程
            return False

    def psm_previous_snapshot(self) -> Optional[dict[str, Any]]:
        """上一条**内容与当前不同**的快照摘要（v1.9.17「恢复上次修改」用）；没有则 None。"""
        return self.psm_history.psh_previous(self.psm_snapshot())

    def psm_list_snapshots(self) -> list[dict[str, Any]]:
        """返回历史快照摘要列表（不含完整值，避免体积过大）。"""
        return self.psm_history.psh_list()

    def psm_restore_snapshot(self, pms_id: str) -> bool:
        """回滚到指定快照：用快照里的全部设置覆盖当前值并落盘（会生成一条新快照）。"""
        values = self.psm_history.psh_restore_values(pms_id)
        if not values:
            return False
        self.psm_set_many(values)
        return True

    def psm_delete_snapshot(self, pms_id: str) -> bool:
        """删除一条历史快照。"""
        return self.psm_history.psh_delete(pms_id)


class PmsSettingsHistory:
    """设置快照历史（第 15 项）：按内容去重记录设置变更，支持查看与回滚。

    快照写在设置文件同目录的 ``pms_settings_history.json``；连续相同内容只保留一份，
    总数上限可配（默认 50），超出丢弃最旧。每条快照含 id / time / label / reason / values。
    """

    def __init__(self, pms_history_file: Path, pms_max: int = 50) -> None:
        self.psh_path = Path(pms_history_file)
        self.psh_max = max(1, int(pms_max))
        self.psh_snapshots: list[dict[str, Any]] = []
        self._psh_load()

    def _psh_load(self) -> None:
        try:
            raw = json.loads(self.psh_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        self.psh_snapshots = [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []

    def _psh_save(self) -> None:
        try:
            self.psh_path.write_text(json.dumps(self.psh_snapshots, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _psh_hash(self, pms_values: dict) -> str:
        text = json.dumps(pms_values, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def psh_capture(self, pms_values: dict, pms_label: str = "", pms_reason: str = "") -> dict:
        """记录一次快照；若内容与上一条相同则只刷新时间戳，不新增。"""
        content = dict(pms_values)
        digest = self._psh_hash(content)
        last = self.psh_snapshots[-1] if self.psh_snapshots else None
        if last and last.get("_hash") == digest:
            last["time"] = pms_now_text()
            if pms_label:
                last["label"] = str(pms_label)
            if pms_reason:
                last["reason"] = pms_reason
            self._psh_save()
            return last
        snapshot = {
            "id": f"{int(time.time() * 1000)}",
            "time": pms_now_text(),
            "label": str(pms_label or ""),
            "reason": str(pms_reason or ""),
            "_hash": digest,
            "values": content,
        }
        self.psh_snapshots.append(snapshot)
        while len(self.psh_snapshots) > self.psh_max:
            self.psh_snapshots.pop(0)
        self._psh_save()
        return snapshot

    def psh_list(self) -> list[dict[str, Any]]:
        """快照摘要列表（去掉 values / _hash 内部字段）。"""
        return [
            {key: value for key, value in item.items() if key not in ("values", "_hash")}
            for item in self.psh_snapshots
        ]

    def psh_previous(self, pms_values: dict) -> Optional[dict]:
        """上一条与 `pms_values` 内容不同的快照摘要（v1.9.17：恢复上一次修改之前的状态）。"""
        digest = self._psh_hash(dict(pms_values))
        for item in reversed(self.psh_snapshots):
            if item.get("_hash") != digest:
                return {key: value for key, value in item.items() if key not in ("values", "_hash")}
        return None

    def psh_trim(self) -> None:
        """按当前 `psh_max` 裁掉最旧的快照并落盘（改「保留条数」后立即生效）。"""
        while len(self.psh_snapshots) > self.psh_max:
            self.psh_snapshots.pop(0)
        self._psh_save()

    def psh_get(self, pms_id: str) -> Optional[dict]:
        for item in self.psh_snapshots:
            if item.get("id") == str(pms_id):
                return item
        return None

    def psh_restore_values(self, pms_id: str) -> Optional[dict]:
        item = self.psh_get(pms_id)
        return dict(item.get("values", {})) if item else None

    def psh_delete(self, pms_id: str) -> bool:
        before = len(self.psh_snapshots)
        self.psh_snapshots = [item for item in self.psh_snapshots if item.get("id") != str(pms_id)]
        if len(self.psh_snapshots) != before:
            self._psh_save()
            return True
        return False

    def psh_clear(self) -> int:
        """清空全部快照，返回清掉条数（v1.9.17 起返回条数，供「设置历史」对话框提示用）。"""
        count = len(self.psh_snapshots)
        self.psh_snapshots = []
        self._psh_save()
        return count
