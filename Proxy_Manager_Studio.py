#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代理节点管理控制台（Proxy Manager Studio）—— 启动器 / 唯一编译源。

程序本体已拆分为多模块包 `pms/`（每个模块一个功能域，便于增减功能与单独测试）：
    pms/constants.py     常量与 137 项设置默认值（版本号 PMS_APP_VERSION 也在这里）
    pms/utils.py         通用工具            pms/paths.py      运行路径解析
    pms/settings.py      设置仓库（改动即保存）  pms/envstore.py   .env 与凭据加密
    pms/logbus.py        日志总线            pms/models.py     数据模型
    pms/database.py      SQLite 仓储         pms/sharelinks.py 分享链接解析/生成
    pms/importers.py     配置导入            pms/coreprofile.py 启动配置档
    pms/coreconfig.py    核心配置生成        pms/coresys.py    核心定位与系统集成
    pms/statsapi.py      统计接口客户端      pms/logparse.py   日志解析与连接追踪
    pms/coreprocess.py   核心进程控制        pms/tempsession.py 临时核心会话
    pms/tasks.py         后台任务            pms/latency.py    延迟测试调度
    pms/speedtest.py     测速调度            pms/traffic.py    流量与连接统计
    pms/subscription.py  订阅更新            pms/download.py   核心下载更新
    pms/versioning.py    版本备份与单实例    pms/theme.py      主题与样式
    pms/icons.py         图标与二维码        pms/app.py        程序入口与装配
    pms/gui/             界面：qtmodels / widgets / tray / dialogs.* / mainwindow

本文件承担两件事：
    1. 入口：`python Proxy_Manager_Studio.py`（支持 --version / --selfcheck / --backup-version <说明>）；
    2. 编译源：Nuitka / PyInstaller 都以本文件为入口（`pms` 包会被一并打包）。
同时它把包内公开名字再导出，方便脚手架脚本与外部工具按老方式使用（pms.xxx）；
新增公开类/函数时，记得同步加进下面的再导出列表。
"""

from __future__ import annotations

import sys

from pms.app import main

from pms.optional import (PMS_HAS_DOTENV, PMS_HAS_PSUTIL, PMS_HAS_QRCODE, PMS_HAS_YAML, pms_optional_dotenv_values,
                          pms_optional_psutil, pms_optional_qrcode, pms_optional_yaml)
from pms.constants import (
    PMS_APP_BUILD_DATE,
    PMS_APP_DOC_NAME,
    PMS_APP_EN_NAME,
    PMS_APP_NAME,
    PMS_APP_ORG,
    PMS_APP_VERSION,
    PMS_BLOCK_AD_DOMAIN_PRESET,
    PMS_BYPASS_DOMAIN_PRESET,
    PMS_COLUMN_ADDRESS,
    PMS_COLUMN_ENABLED,
    PMS_COLUMN_GROUP,
    PMS_COLUMN_INDEX,
    PMS_COLUMN_LAST_TEST,
    PMS_COLUMN_LATENCY,
    PMS_COLUMN_MIN_WIDTHS,
    PMS_COLUMN_NAME,
    PMS_COLUMN_PORT,
    PMS_COLUMN_REMARK,
    PMS_COLUMN_SPEED,
    PMS_COLUMN_SUBSCRIPTION,
    PMS_COLUMN_TITLES,
    PMS_COLUMN_TYPE,
    PMS_CONNECTION_TICK_MS,
    PMS_CORE_BINARY_NAMES,
    PMS_CORE_KIND_AUTO,
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_TITLES,
    PMS_CORE_KIND_XRAY,
    PMS_CORE_RESTART_MAX_DELAY_MS,
    PMS_CORE_RESTART_MIN_DELAY_MS,
    PMS_DATABASE_FILENAME,
    PMS_DEFAULT_RULESET_GEOIP_CN,
    PMS_DEFAULT_RULESET_GEOSITE_CN,
    PMS_DEFAULT_SPEED_TEST_URL,
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_DEFAULT_TEST_URLS,
    PMS_DEFAULT_TUN_INTERFACE,
    PMS_DIRECT_DOMAIN_PRESET,
    PMS_DIR_CONFIGS,
    PMS_DIR_CORE,
    PMS_DIR_DATA,
    PMS_DIR_LOGS,
    PMS_DIR_SCAFFOLD,
    PMS_DIR_TEMP,
    PMS_DIR_VERSION,
    PMS_ENV_FILENAME,
    PMS_EXE_FILENAME,
    PMS_ICON_FILENAME,
    PMS_IS_WINDOWS,
    PMS_LATENCY_GRADE_GOOD,
    PMS_LATENCY_GRADE_MEDIUM,
    PMS_LOGGER_NAME,
    PMS_LOG_FILENAME,
    PMS_LOG_LEVEL_ORDER,
    PMS_LOG_LEVEL_TITLES,
    PMS_MAX_CONNECTION_ROWS,
    PMS_MAX_LOG_LINES,
    PMS_PROCESS_STOP_GRACE_MS,
    PMS_PROTOCOL_HTTP,
    PMS_PROTOCOL_HYSTERIA2,
    PMS_PROTOCOL_ORDER,
    PMS_PROTOCOL_SHADOWSOCKS,
    PMS_PROTOCOL_SOCKS,
    PMS_PROTOCOL_TITLES,
    PMS_PROTOCOL_TROJAN,
    PMS_PROTOCOL_TUIC,
    PMS_PROTOCOL_VLESS,
    PMS_PROTOCOL_VMESS,
    PMS_ROUTING_MODE_DIRECT,
    PMS_ROUTING_MODE_GLOBAL,
    PMS_ROUTING_MODE_RULE,
    PMS_ROUTING_MODE_TITLES,
    PMS_SECURITY_NONE,
    PMS_SECURITY_REALITY,
    PMS_SECURITY_TITLES,
    PMS_SECURITY_TLS,
    PMS_SETTINGS_DEFAULTS,
    PMS_SETTINGS_FILENAME,
    PMS_SETTINGS_SAVE_DEBOUNCE_MS,
    PMS_SETTINGS_SECTION_TITLES,
    PMS_SHARE_SCHEME_TO_PROTOCOL,
    PMS_SOURCE_FILENAME,
    PMS_SS_METHODS,
    PMS_STATS_SOURCE_AUTO,
    PMS_STATS_SOURCE_CLASH,
    PMS_STATS_SOURCE_LOG,
    PMS_STATS_SOURCE_TITLES,
    PMS_STATS_SOURCE_XRAY,
    PMS_STATUS_TICK_MS,
    PMS_SUB_API_KEY_ENV,
    PMS_TEST_MODE_TCP,
    PMS_TEST_MODE_TITLES,
    PMS_TEST_MODE_URL,
    PMS_THEME_DARK,
    PMS_THEME_LIGHT,
    PMS_THEME_SYSTEM,
    PMS_THEME_TITLES,
    PMS_TLS_FINGERPRINTS,
    PMS_TRAFFIC_TICK_MS,
    PMS_TRANSPORT_ALIASES,
    PMS_TRANSPORT_GRPC,
    PMS_TRANSPORT_H2,
    PMS_TRANSPORT_HTTP,
    PMS_TRANSPORT_HTTPUPGRADE,
    PMS_TRANSPORT_TCP,
    PMS_TRANSPORT_TITLES,
    PMS_TRANSPORT_WS,
    PMS_TRANSPORT_XHTTP,
    PMS_TUIC_CONGESTION,
    PMS_TUIC_UDP_MODES,
    PMS_TUN_STACK_TITLES,
    PMS_VERSION_BACKUP_NOTE_PLACEHOLDER,
    PMS_VMESS_SECURITY,
)
from pms.utils import (
    pms_atomic_write_bytes,
    pms_atomic_write_text,
    pms_b64_decode_loose,
    pms_b64_encode_text,
    pms_days_until,
    pms_dedupe_keep_order,
    pms_fail_short_text,
    pms_find_free_port,
    pms_format_bytes,
    pms_format_latency,
    pms_format_speed,
    pms_human_duration,
    pms_is_port_available,
    pms_json_pretty,
    pms_lan_proxy_address,
    pms_looks_like_base64,
    pms_local_lan_ip,
    pms_mask_secret,
    pms_multiline_to_list,
    pms_normalize_domain_entry, pms_normalize_cert_sha256,
    pms_now_millis_text,
    pms_now_text,
    pms_read_text_smart,
    pms_resolve_speed_url,
    pms_safe_bool,
    pms_safe_float,
    pms_safe_int,
    pms_slugify_filename,
    pms_speed_candidate_sizes,
    pms_speed_ok_key,
    pms_speed_ok_sizes,
    pms_split_multi_line,
)
from pms.paths import (
    PmsRuntimePaths,
)
from pms.settings import (PmsSettingsManager, PmsSettingsHistory)
from pms.envstore import (
    PmsEnvStore,
    PmsSecretVault,
)
from pms.logbus import (
    PmsLogBus,
)
from pms.models import (
    PmsConnectionRecord,
    PmsGroupRecord,
    PmsProxyNode,
    PmsSubscriptionRecord,
)
from pms.database import (
    PMS_DB_SCHEMA_VERSION,
    PmsDatabaseEngine,
    PmsNodeRepository,
)
from pms.sharelinks import (
    PmsImportReport,
    PmsShareLinkParser,
    PmsShareLinkParserExtraMixin,
    PmsShareLinkWriter,
    PmsShareLinkWriterExtraMixin,
)
from pms.importers import (
    PmsClashYamlImporter,
    PmsSingBoxJsonImporter,
    PmsTextImportClassifier,
)
from pms.coremeta import pms_core_title, pms_version_base, pms_version_effective_local, pms_version_newer, pms_version_tuple
from pms.tordownload import (PmsTorDownloadSignals, PmsTorDownloadTask, pms_tor_bundle_members, ptd_bundle_url, ptd_check_config, ptd_download_bytes, ptd_extract_members, ptd_fetch_members, ptd_install_bundle, ptd_latest_version, ptd_probe_config, ptd_version_key, ptd_write_member)
from pms.torsource import (PmsTorCandidate, PMS_TOR_ARCHIVE_INDEX, PMS_TOR_DIST_INDEX, pts_archive_url_exists, pts_browser_url, pts_bundle_url, pts_download_bytes, pts_find_7z, pts_list_versions, pts_local_browser_dirs, pts_local_browser_members, pts_sevenzip_members, pts_tar_members, pts_version_key)
from pms.coreprofile import (
    PmsCoreLaunchProfile,
)
from pms.coreconfig import (
    PmsCoreConfigWriter,
    PmsSingBoxConfigBuilder,
    PmsXrayConfigBuilder,
)
from pms.coresys import (
    PmsCoreLocator,
    PmsPortAllocator,
    PmsPrivilegeUtils,
    PmsWindowsAutoStart,
    PmsWindowsSystemProxy,
)
from pms.gui.dialogs.sysproxywidget import PmsSystemProxyWidget   # v1.9.49：设置页「系统代理」PAC 模式部件
from pms.statsapi import (
    PmsClashApiClient,
    PmsXrayStatsClient,
)
from pms.logparse import (
    PMS_LOG_SINGBOX_LINE,
    PMS_LOG_XRAY_ACCEPTED,
    PMS_LOG_XRAY_DETAIL,
    PmsConnectionTracker,
    PmsCoreLogParser,
)
from pms.coreprocess import (
    PmsCoreProcessController, pcp_expected_tor_binaries, pcp_kill_process_tree,
    pcp_normalize_path, pcp_orphan_tor_processes, pcp_process_identity,
    pcp_process_identity_shell, pcp_tor_data_dir_locked,
)
from pms.tempsession import (
    PmsTempCoreSession,
)
from pms.tasks import (PmsClashDelayTask, PmsSpeedTestTask, PmsTaskSignals, PmsTcpPingSignals, PmsTcpPingTask, PmsTempCoreTask, PmsXrayBatchDelayTask)
from pms.latency import PmsLatencyTestCoordinator   # v1.9.59：三行块并成单行，给 dctest 再导出腾预算
from pms.speedtest import PmsSpeedTestCoordinator
from pms.dctest import PmsTelegramTestCoordinator   # v1.9.59 电报 DC 测试（其余新符号全私有，守 400 行红线）
from pms.traffic import (
    PmsTrafficMonitorService,
)
from pms.subscription import (
    PmsSubscriptionService,
    PmsSubscriptionSignals,
    PmsSubscriptionUpdateTask,
    pms_subscription_kind,
    pms_subscription_kind_title,
    pms_subscription_matches_filter,
    pms_subscription_record_kind_title,
)
from pms.telegram import (pms_telegram_build_openers, pms_telegram_channel_from_url, pms_telegram_extract_links, pms_telegram_extract_subscriptions, pms_telegram_extract_tg_proxies, pms_telegram_fetch_channel, pms_telegram_fetch_channel_detailed, pms_telegram_html_to_text, pms_telegram_is_channel_url, pms_telegram_message_ids, pms_telegram_parse_messages, pms_telegram_parse_order, pms_telegram_preview_url, pms_telegram_proxy_url)
from pms.gui.dialogs.torwidget import (PmsTorBridgeWidget, PmsTorCheckSignals, PmsTorCheckTask, PmsTorCollectSignals, PmsTorCollectTask)
from pms.torbridge import (PMS_TOR_BRIDGE_SOURCES, ptb_cache_file, ptb_collect_valid_bridges, ptb_fetch_all_bridge_lines, ptb_fetch_source_bridges, ptb_http_get, ptb_probe_target, ptb_read_cache, ptb_read_unverified, ptb_source_file_urls, ptb_tcp_alive, ptb_unverified_file, ptb_validate_bridges, ptb_write_cache, ptb_write_unverified)
from pms.tor_bridge import (ptb_apply_bridge_lines_to_settings, ptb_bridge_to_torrc,
                            ptb_mark_unverified, ptb_merge_unverified_lines, ptb_unverified_lines, ptb_apply_singbox_tor, ptb_binaries_state, ptb_bridge_identity, ptb_bridge_type, ptb_build_extra_args, ptb_build_tor_outbound, ptb_build_torrc, ptb_core_kind_error, ptb_extract_bridges_from_text, ptb_merge_bridge_lines, ptb_normalize_bridge_line, ptb_parse_bridge_line, ptb_parse_bridge_lines, ptb_plugin_for, ptb_plugin_lines, ptb_resolve_paths, ptb_settings_payload, ptb_tor_dir, ptb_tor_paths)
from pms.download import (
    PMS_DOWNLOAD_RESUME_MIN_BYTES,
    PmsCoreDownloadService,
    PmsCoreDownloadSignals,
    PmsCoreDownloadTask,
    PmsCoreReleaseInfo,
    pms_core_api_url,
    pms_core_select_release,
)
from pms.coreupdate import (
    PmsCoreUpdateCheckSignals,
    PmsCoreUpdateCheckTask,
)
from pms.telegramimport import (
    PMS_TELEGRAM_IMPORT_MAX_SUBS,
    PmsTelegramImportSignals,
    PmsTelegramImportWorker,
    pms_telegram_import_last_id_key,
    pms_telegram_import_mode,
    pms_telegram_import_openers,
    pms_telegram_import_pages,
    pms_telegram_import_subscription_text,
    pms_telegram_import_timeout,
)
from pms.versioning import (
    PmsSingleInstanceGuard,
    PmsVersionBackupHelper,
    pms_file_sha256,
)
from pms.theme import PmsThemeManager
from pms.icons import (
    pms_build_fallback_icon,
    pms_load_app_icon,
    pms_qr_pixmap,
)
from pms.gui.widgets import (
    PmsToolbarFilterButton,
    pms_make_hint_label,
    pms_make_row,
)
from pms.gui.qtmodels import (
    PmsConnectionTableModel,
    PmsLatencyBarDelegate,
    PmsLogTableModel,
    PmsNodeFilterProxyModel,
    PmsNodeTableModel,
)
from pms.gui.tray import PmsTrayController
from pms.gui.dialogs.nodeeditor import PmsNodeEditorDialog
from pms.gui.dialogs.shareqr import PmsShareQrDialog
from pms.gui.dialogs.importdialog import PmsImportDialog
from pms.gui.dialogs.batchops import PmsBatchOperationDialog
from pms.gui.dialogs.subscriptiondialog import (
    PmsGithubTrialSignals,
    PmsGithubTrialWorker,
    PmsSubscriptionDialog,
    PmsTelegramTrialSignals,
    PmsTelegramTrialWorker,
)
from pms.apiclash import PmsClashCompat
from pms.apicommands import PmsApiCommander, PmsApiDispatcher, pms_api_cmd_fail, pms_api_cmd_ok
from pms.api import PmsApi, PmsApiResult, pms_api_event_seq_check, pms_api_event_seq_guard  # v1.9.49：事件序号防呆（审计第 23 项）
from pms.apiserver import PmsApiServer, PmsApiHttpServer, PmsApiRequestHandler, pms_api_generate_token
from pms.autoswitch import PmsAutoSwitchService, PmsHttpProbeSignals, PmsHttpProbeTask
from pms.maintenance import PmsMaintenanceSignals, PmsMaintenanceTask, pms_maintenance_days, pms_maintenance_plan, pms_maintenance_rules, pms_maintenance_run, pms_maintenance_size_text, pms_maintenance_summary_text
from pms.corematrix import PmsCoreMatrixSignals, PmsCoreMatrixTask, pms_core_matrix_builtin_state, pms_core_matrix_cache_file, pms_core_matrix_cache_load, pms_core_matrix_cache_save, pms_core_matrix_classify, pms_core_matrix_config_file, pms_core_matrix_detail, pms_core_matrix_kinds, pms_core_matrix_pick_samples, pms_core_matrix_probe, pms_core_matrix_probe_profile, pms_core_matrix_protocols, pms_core_matrix_rows, pms_core_matrix_sample_node, pms_core_matrix_sample_nodes, pms_core_matrix_summary_text
from pms.healthcheck import PmsHealthCheckService, pms_health_managed, pms_health_node_note, pms_health_parse, pms_health_summary_text, pms_health_text  # v1.9.37：节点健康检查

from pms.webpanel import pms_web_panel_disabled_html, pms_web_panel_html, pms_web_panel_path_match, pms_web_panel_scheme, pms_web_panel_style, pms_web_panel_url
from pms.gui.dialogs.apiwidget import PmsApiSettingsWidget
from pms.gui.dialogs.autoswitchwidget import PmsAutoSwitchSettingsWidget
from pms.gui.dialogs.statswidget import PmsStatsSettingsWidget
from pms.gui.dialogs.testsortwidget import PmsTestSortWidget
from pms.gui.dialogs.testnoticewidget import PmsTestNoticeWidget  # v1.9.21/22：测试提示（失败显示口径 + 测速汇总框）
from pms.gui.dialogs.uiwidget import PmsUiLayoutWidget
from pms.gui.dialogs.sysproxywidget import PmsSystemProxyWidget  # v1.9.49：系统代理模式（fixed / PAC，审计第 5 项）
from pms.gui.dialogs.settingsdialog import PmsSettingsDialog
from pms.gui.dialogs.settingssubtabs import (
    pst_apply_subtabs, pst_build_footer, pst_build_subtabs, pst_make_scroll,
    pst_section_key_count, pst_section_title, pst_tab_sections,
)
from pms.gui.dialogs.githubwidget import PmsGithubSettingsWidget
from pms.githubsource import (
    PmsGithubRepoSource, pms_github_extract_urls, pms_github_file_score, pms_github_is_repo_url,
    pms_github_keyword_bonus, pms_github_name_dates, pms_github_pick_files, pms_github_repo_from_url,
    pms_github_stem, pms_github_tokens, pms_github_url,
)
from pms.gui.dialogs.coreversionwidget import PmsCoreVersionWidget
from pms.coreclean import (
    PmsCoreCleanSignals, PmsCoreCleanTask, pms_core_clean_dirs, pms_core_clean_hits, pms_core_clean_known_name,
    pms_core_clean_plan, pms_core_clean_plan_text, pms_core_clean_run, pms_core_clean_size_text, pms_core_clean_summary_text,
)  # v1.9.49：未命中内核文件清理（审计第 30 项）
from pms.gui.dialogs.maintenancewidget import PmsMaintenanceWidget  # v1.9.33：日志与临时文件清理设置部件
from pms.gui.dialogs.corematrixwidget import PmsCoreMatrixWidget  # v1.9.35：协议能力矩阵部件
from pms.gui.dialogs.healthcheckwidget import PmsHealthCheckWidget  # v1.9.37：节点健康检查设置部件
from pms.gui.dialogs.speedsourcewidget import (
    PmsSpeedSourceCheckSignals,
    PmsSpeedSourceCheckWorker,
    PmsSpeedSourceWidget,
    pms_probe_speed_url,
)
from pms.gui.dialogs.telegramwidget import (
    PmsTelegramProbeSignals,
    PmsTelegramProbeWorker,
    PmsTelegramSettingsWidget,
)
from pms.gui.dialogs.aboutdialog import PmsAboutDialog
from pms.gui.dialogs.versionhistory import PmsVersionHistoryDialog
from pms.gui.dialogs.lanproxydialog import PmsLanProxyDialog
from pms.gui.dialogs.testpickerdialog import PmsTestPickerDialog  # v1.9.65：「选择测试」弹窗（范围单选 + 测试项多选）
from pms.gui.dialogs.tgproxytestdialog import PmsTgProxyTestDialog  # v1.9.68：电报代理连通直测弹窗
from pms.tgproxy import (PmsTgProxySecret, pms_tg_parse_secret, pms_tg_build_obfuscated_header, pms_tg_build_req_pq_frame, pms_tg_probe_proxy, pms_tg_probe_from_link)  # v1.9.68：MTProto 握手与连通探测
from pms.gui.dialogs.paceditor import PmsPacEditorDialog  # v1.9.62：内置 PAC 脚本编辑器（专业 / 小白双模式）
from pms.gui.dialogs.systemtools import (PmsDiagnosticsDialog, PmsSettingsHistoryDialog)
from pms.diagnostics import (PmsNetworkDiagnostics, pms_quick_diagnostics, pms_diagnose_targets)
from pms.gui.mainwindow import PmsMainWindow
from pms.app import (pms_install_qt_translator, pms_install_shortcuts, pms_run_application, pms_setup_high_dpi)

if __name__ == "__main__":
    sys.exit(main())
