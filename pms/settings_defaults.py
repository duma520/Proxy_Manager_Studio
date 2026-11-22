#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置默认值与它依赖的常量（v1.9.42 从 `pms/constants.py` 整块搬出来）

为什么单独一个文件：`pms/constants.py` 长期顶在 1000 行红线上（余量 1 行），
而「新增设置项」是最高频的改动。搬出来之后：

* 新增/调整设置默认值只动本文件，`constants.py` 不再涨；
* `pms/constants.py` 仍然把这里所有名字**再导出一遍**，所以全项目
  `from pms.constants import X` 的写法一行都不用改；
* 本文件**不许 import `pms.constants`**（否则循环导入）。
"""

from __future__ import annotations

from typing import Any


PMS_TRAFFIC_TICK_MS = 1000
# --------------------------------------------------------------------------------------
# 本地 HTTP 接口（v1.6.0）
# --------------------------------------------------------------------------------------
# 默认端口避开已占用的 30808（混合入口）/ 20809（HTTP 入口）/ 20810（SOCKS 入口）
# v1.9.58：混合入口由 20808 挪到 30808（与 http 30809 / socks 30810 同段），
# 目的是避开用户机器的外部代理端口 ***已脱敏***-内网IP:20808——详见下面 inbound.mixed_port 的注释。
PMS_API_DEFAULT_PORT = 20811
PMS_API_DEFAULT_HOST = "127.0.0.1"
PMS_API_AUTH_AUTO = "auto"              # 首次启用自动生成并写入 .env
# --------------------------------------------------------------------------------------
# 定时健康检测与失效自动切换（v1.6.1）
# --------------------------------------------------------------------------------------
PMS_AUTOSWITCH_DETECT_TCP = "tcp"        # TCP 握手：直连节点服务器端口（默认，最快最省）
PMS_FASTEST_SCOPE_TESTED = "tested"      # v1.9.33 测速后切最快的候选范围：本次测到的 / 全部已启用 / 当前分组
# 默认 60 秒一轮；太短会频繁打扰网络，太长则失效发现不及时
PMS_AUTOSWITCH_DEFAULT_INTERVAL_SECONDS = 60
# 连续失败几次才判定「当前节点无效」（单次抖动不切，避免来回横跳）
PMS_AUTOSWITCH_DEFAULT_FAIL_THRESHOLD = 2
PMS_MAX_CONNECTION_ROWS = 800
PMS_CORE_KIND_AUTO = "auto"
PMS_ROUTING_MODE_RULE = "rule"
# v1.9.7：新增 native，表示真正的系统原生风格（Windows 经典控件）
PMS_THEME_LIGHT, PMS_THEME_DARK, PMS_THEME_SYSTEM, PMS_THEME_NATIVE = "light", "dark", "system", "native"
PMS_TEST_MODE_URL = "url"
# v1.9.49：系统代理模式（`system_proxy.mode`）与 PAC 文件名。
#   fixed = 现在的固定代理（写 ProxyServer，行为与本版之前逐键一致）；
#   pac   = 写 AutoConfigURL 指向本程序生成的 PAC 脚本，按域名分流。
# PAC 脚本放数据目录（路径解析统一在 pms/paths.py 的 prt_pac_file）。
PMS_SYSTEM_PROXY_MODE_FIXED, PMS_SYSTEM_PROXY_MODE_PAC = "fixed", "pac"
PMS_PAC_FILENAME = "pms_system_proxy.pac"
# v1.9.62：内置 PAC 编辑器产物文件（用户可手写 / 小白模式生成），与上面的自动 PAC 完全独立，
# 二者不互相覆盖（见 pms/gui/dialogs/paceditor.py 与 coresys.pwsp_enable_pac_file）。
PMS_CUSTOM_PAC_FILENAME = "pms_custom.pac"
# v1.9.73：电报抓取通路次序自定义默认值（仅 auto 模式生效，其它预设模式忽略）。
# token 顺序即退让优先级；与旧 auto（本程序→外部→直连）一致，向后兼容。
PMS_TELEGRAM_DEFAULT_PROXY_ORDER = "local,external,direct"# v1.9.12：测试结束后的自动排序范围（延迟与测速各自独立设置）
PMS_SORT_SCOPE_ALL, PMS_SORT_SCOPE_PAGE = "all", "page"
PMS_STATS_SOURCE_AUTO = "auto"
PMS_DEFAULT_TEST_URLS: tuple[str, ...] = (
    "http://cp.cloudflare.com/generate_204",
    "http://www.gstatic.com/generate_204",
    "http://connectivitycheck.platform.hicloud.com/generate_204",
)
PMS_DEFAULT_SPEED_TEST_URL = "https://speed.cloudflare.com/__down?bytes=52428800"
# ---- 测速源预设（v1.4.3）----
# 模板占位符：{bytes} 精确字节数、{mb} 兆字节数；两个都必须传，模板各取所需。
PMS_SPEED_SOURCE_CLOUDFLARE = "cloudflare"
# ---- 电报频道抓取（v1.4.4）----
# 抓取通道：auto = 本程序代理（当前节点）→ 外部代理 → 直连，任一级失败自动降级下一级。
# t.me 在国内必须走代理，故默认 auto，并预置用户环境里的外部代理地址（可在设置里改）。
PMS_TELEGRAM_PROXY_AUTO = "auto"
PMS_DEFAULT_TELEGRAM_EXTERNAL_PROXY = "***已脱敏***-内网IP:20808"
PMS_DEFAULT_SUB_USER_AGENT = "v2rayN/6.31 (Proxy Manager Studio)"
PMS_DEFAULT_RULESET_GEOSITE_CN = (
    "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-cn.srs"
)
# ---- 规则集来源：本地文件优先，避免联网下载 ----
PMS_RULESET_SOURCE_LOCAL = "local"
PMS_COLUMN_INDEX = 1
PMS_DEFAULT_TUN_INTERFACE = "PmsTunnel"
PMS_BYPASS_DOMAIN_PRESET: tuple[str, ...] = (
    "localhost",
    "127.0.0.1",
    "::1",
    "*.local",
    "*.lan",
    "*.internal",
)
PMS_DIRECT_DOMAIN_PRESET: tuple[str, ...] = (
    "cn",
    "baidu.com",
    "qq.com",
    "taobao.com",
    "tmall.com",
    "alipay.com",
    "jd.com",
    "bilibili.com",
    "163.com",
    "126.com",
    "aliyun.com",
    "alicdn.com",
    "tencent.com",
    "weixin.qq.com",
    "bytedance.com",
    "douyin.com",
    "mi.com",
    "huawei.com",
    "csdn.net",
    "zhihu.com",
    "360.cn",
    "sohu.com",
    "sina.com.cn",
    "sinaimg.cn",
    "youdao.com",
    "iqiyi.com",
    "youku.com",
    "meituan.com",
    "dianping.com",
    "pinduoduo.com",
    "ximalaya.com",
    "kuaishou.com",
)
PMS_BLOCK_AD_DOMAIN_PRESET: tuple[str, ...] = (
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "adservice.google.com",
    "ads.pubmatic.com",
    "admaster.com.cn",
    "umeng.com",
    "umengcloud.com",
    "cnzz.com",
    "mmstat.com",
    "adnxs.com",
    "cpro.baidu.com",
)
# --------------------------------------------------------------------------------------
# 设置默认值：新增设置项必须同时补到这里 + 文档「九、设置项清单」
# --------------------------------------------------------------------------------------
PMS_SETTINGS_DEFAULTS: dict[str, Any] = {
    # ---- 常规 ----
    "general.language": "zh_CN",
    "general.auto_start_core": True,
    "general.auto_connect_last_node": True,
    "general.last_node_uid": "",
    "general.minimize_to_tray": True,
    "general.close_to_tray": True,
    "general.confirm_on_exit": False,
    "general.auto_start_with_windows": False,
    "general.notify_on_core_error": True,
    "general.notify_on_subscription_done": True,
    "general.single_instance": True,
    "general.check_core_on_launch": True,
    # ---- 应用元数据（v1.9.67）----
    # ★ 必须是 defaults 里的**正式键**，不能只当“未知项”存：`psm_load_from_disk()` 只会把
    #   「key 在 defaults 里」的磁盘值并回 `psm_values`，不在 defaults 的键会被丢进
    #   `psm_unknown_keys`。而 `psm_check_and_migrate()` 只读 `psm_values` 里的本键，
    #   于是每次启动都读不到上次写的版本号 → 打成「未知」→ 每次都重跑一遍版本迁移
    #   （用户看到的 `版本迁移：未知 → 1.9.66` 刷屏就是这个问题）。
    "app.version_last_seen": "",
    # ---- 核心 ----
    "core.kind": PMS_CORE_KIND_AUTO,
    "core.xray_path": "",
    "core.singbox_path": "",
    "core.log_level": "info",
    "core.access_log": False,
    "core.auto_restart": True,
    "core.restart_max_times": 5,
    "core.kill_existing_on_start": True,
    "core.keep_config_file": True,
    "core.download_mirror": "", "core.seven_zip_path": "",   # 镜像=GitHub 下载前缀；7z=7z.exe 路径，留空自动探测（Python 解压失败时兜底，v1.8.1）
    "core.disable_geo_assets": False,
    "core.mux_enabled": False,
    "core.mux_concurrency": 8,
    "core.tcp_fast_open": True,
    "core.sniff_override_dest": True,
    # ---- Tor 桥接出口（v1.9.43 第一期：只用 sing-box 的 tor 出站，不新增内核）----
    # 成对写同一行是为了守「设置项表不无限膨胀」的老规矩（constants.py 已腾出余量，这里沿用写法）
    "tor.enabled": False, "tor.bridge_type": "obfs4",
    "tor.bridge_lines": [],               # 每行一条桥接（支持 obfs4 / webtunnel / meek / snowflake）
    "tor.keep_unverified": False,         # v1.9.48：验活失败也保存候选（存进 tor.unverified_lines + <数据目录>/tor/bridges_unverified.txt，**不进配置**）
    "tor.unverified_lines": [],           # v1.9.48：未验活的候选（保存前照样真握手验过，只是没通过；界面标成「未验活、可能不可用」）
    "tor.bundle_version": "",             # 已安装的 Tor Expert Bundle 版本（下载成功后写回）
    "tor.tor_dir": "",                    # 留空 = <数据目录>/tor
    "tor.data_directory": "",             # 留空 = <数据目录>/tor/data（必须是持久目录）
    # ---- 本地入口 ----
    # v1.9.58：混合入口默认端口 20808 → 30808。
    # 原因（用户 2026-10-14 确认）：这台机器的「外部代理」恰好是 ***已脱敏***-内网IP:**20808**，
    # 而 Telegram / 订阅 / Tor 下载的退让链第一级是「本程序代理 127.0.0.1:mixed_port」。
    # 默认值停在 20808 时，一旦有人把 mixed 改回默认，第一级就会去连 127.0.0.1:20808——
    # 那台机器上是外部代理的端口、不是本机入口，必然拒绝连接，白耗一次尝试才降级。
    # 挪到 30808 段（http 30809 / socks 30810 同段）彻底避开这个巧合。
    "inbound.listen_address": "127.0.0.1",
    "inbound.mixed_enabled": True,
    "inbound.mixed_port": 30808,
    "inbound.auto_pick_port": False,
    "inbound.http_enabled": False,
    "inbound.http_port": 20809,
    "inbound.socks_enabled": False,
    "inbound.socks_port": 20810,
    "inbound.udp_enabled": True,
    "inbound.sniff_enabled": True,
    "inbound.auth_enabled": False,
    "inbound.username": "",
    "inbound.password": "",
    "inbound.allow_lan": False,
    # ---- 系统代理 ----
    "system_proxy.enabled": False,
    "system_proxy.bypass_list": "localhost;127.*;10.*;172.16.*;192.168.*;*.local;<local>",
    "system_proxy.restore_on_exit": True,
    "system_proxy.backup_saved": False,
    "system_proxy.original_enable": 0,
    "system_proxy.original_server": "",
    "system_proxy.original_bypass": "",
    "system_proxy.original_auto_config_url": "",   # v1.9.49：**必存**——还原时要把用户原来的 PAC 一起还原，否则人家的 AutoConfigURL 被永久抹掉
    "system_proxy.mode": PMS_SYSTEM_PROXY_MODE_FIXED,   # v1.9.49：fixed=固定代理（默认，行为与旧版一致）/ pac=写 AutoConfigURL
    "system_proxy.pac_direct_domains": [],         # v1.9.49：PAC 里判为「直连」的域名后缀；留空则用 routing.direct_domains 预设
    "system_proxy.pac_source": PMS_SYSTEM_PROXY_MODE_FIXED,   # v1.9.62：pac 模式下用哪份脚本——fixed(=auto)=本程序自动生成的 pms_system_proxy.pac；custom=用户用 PAC 编辑器写的 pms_custom.pac
    # ---- TUN ----
    "tun.enabled": False,
    "tun.interface_name": PMS_DEFAULT_TUN_INTERFACE,
    "tun.stack": "mixed",
    "tun.mtu": 9000,
    "tun.auto_route": True,
    "tun.strict_route": False,
    "tun.ipv6": False,
    # ---- 路由 ----
    "routing.mode": PMS_ROUTING_MODE_RULE,
    "routing.bypass_domains": list(PMS_BYPASS_DOMAIN_PRESET),
    "routing.direct_domains": list(PMS_DIRECT_DOMAIN_PRESET),
    "routing.block_ad_domains": list(PMS_BLOCK_AD_DOMAIN_PRESET),
    "routing.block_ads_enabled": True,
    "routing.bypass_private_ips": True,
    "routing.dns_remote": "https://1.1.1.1/dns-query",
    "routing.dns_direct": "223.5.5.5",
    "routing.domain_strategy": "IPIfNonMatch",
    "routing.use_remote_ruleset": False,
    "routing.ruleset_source": PMS_RULESET_SOURCE_LOCAL,
    "routing.ruleset_geosite_cn_url": PMS_DEFAULT_RULESET_GEOSITE_CN,
    # ---- 延迟测试 ----
    "test.mode": PMS_TEST_MODE_URL,
    "test.url_list": list(PMS_DEFAULT_TEST_URLS),
    "test.timeout_ms": 5000, "test.fail_show_core": False, "test.speed_result_dialog": True,  # v1.9.21：失败时延迟列显示核心原话（HTTP 503/504…）还是「超时」（默认）；v1.9.22：用户手动发起的测速跑完弹汇总框（状态栏 + 托盘通知始终都有）——三键同行守 1000 行红线
    "test.concurrency": 16,
    "test.retry_times": 1,
    "test.remove_duplicates_before_test": True,
    "test.sort_after_test": False, "test.sort_after_speed": False,  # v1.9.12：测试后自动排序（延迟=升序 / 测速=降序，各自独立开关）
    "test.sort_scope_latency": PMS_SORT_SCOPE_ALL, "test.sort_scope_speed": PMS_SORT_SCOPE_ALL,  # 同行：v1.9.12 自动排序范围（all=全量，排完回第 1 页 / page=只排当前页，不跳页）
    "test.auto_test_on_start": False, "test.auto_test_interval_minutes": 0,   # v1.9.37 同行（守 1000 行红线）
    "test.only_test_enabled": False, "test.tcp_timeout_ms": 3000,   # v1.9.37 同行（守 1000 行红线）
    # ---- 测速 ----
    "speed.source": PMS_SPEED_SOURCE_CLOUDFLARE,
    "speed.size_mb": 10,
    "speed.url": PMS_DEFAULT_SPEED_TEST_URL,
    "speed.duration_seconds": 10,
    "speed.min_samples": 3,
    "speed.concurrency": 4,
    "speed.per_node_limit_mb": 30,
    # 自检通过的档位，元素形如 "<源key>:<MB>"
    "speed.ok_sizes": [],
    # ---- 电报频道 ----
    "telegram.pages": 1, "telegram.fetch_mode": "full", "telegram.fetch_to_beginning": False,  # 同行：文件顶在 1000 行红线；full=全量 / incremental=增量 / fetch_to_beginning=全量时翻到频道开头（慢）
    "telegram.backfill_enabled": False, "telegram.backfill_pages_per_round": 10, "telegram.backfill_delay_seconds": 2,  # 同行：v1.9.6 慢速回填历史（每轮页数 / 每页停顿秒数）
    "telegram.proxy_mode": PMS_TELEGRAM_PROXY_AUTO,
    "telegram.proxy_order": PMS_TELEGRAM_DEFAULT_PROXY_ORDER,  # v1.9.73：auto 模式下的通路退让次序（local/external/direct）
    "telegram.external_proxy": PMS_DEFAULT_TELEGRAM_EXTERNAL_PROXY,
    "telegram.timeout_seconds": 20,
    # ---- 订阅 ----
    "subscription.update_on_launch": False,
    "subscription.auto_update_enabled": False,
    "subscription.auto_update_interval_minutes": 360,
    "subscription.user_agent": PMS_DEFAULT_SUB_USER_AGENT,
    "subscription.use_env_api_key": True,
    "subscription.keep_previous_on_failure": True,
    "subscription.remove_duplicates": True,
    "subscription.prune_removed": True, "subscription.dedup_across_subscriptions": True,  # 同行：文件顶在 1000 行红线
    "subscription.timeout_seconds": 25,
    "subscription.use_system_proxy": False,
    "subscription.last_update_time": "", "subscription.github_max_nodes": 500, "subscription.github_max_files": 20, "subscription.github_max_total_mb": 24, "subscription.github_follow_links": True,  # v1.9.18 同行追加：GitHub 仓库订阅（守 1000 行红线）
    # ---- 界面 ----
    "ui.theme": PMS_THEME_NATIVE,  # v1.9.7 默认改成系统原生风格，不再默认套自定义 QSS
    "ui.ui_scale": 1.0,
    "ui.table_row_height": 26, "ui.latency_bar_height": 5,  # v1.9.23：延迟列进度条粗细（像素，0=不画）；与行高同行守 1000 行红线
    "ui.table_font_size": 9.5,
    "ui.log_font_size": 9.0,
    "ui.table_sort_column": PMS_COLUMN_INDEX,
    "ui.table_sort_order": 0,
    "ui.table_column_widths": [],
    "ui.table_column_visible": [],
    "ui.table_page_size": 50, "ui.table_page_index": 1, "ui.header_sort_scope": PMS_SORT_SCOPE_ALL,  # v1.7.0 分页：每页条数（0=不分页）/ 当前页码（启动恢复，带夹取）；v1.9.14 点表头排序范围（all=全量 / page=本页）
    # v1.9.76 方案 B：TgProxy / TgWebProxy 两个专属分页标签各自的「每页条数 / 当前页码」记忆（独立翻页）
    "ui.tg_proxy_tab_page_size": 50, "ui.tg_proxy_tab_page_index": 1, "ui.tg_webproxy_tab_page_size": 50, "ui.tg_webproxy_tab_page_index": 1,
    "ui.filter_group": "",
    "ui.filter_search": "",
    "ui.filter_protocol": "",
    "ui.show_connection_panel": True,
    "ui.show_quick_actions": True,     # v1.6.5：左侧「快捷操作」面板可隐藏
    "ui.show_left_sidebar": True,      # v1.6.6：整个左侧边栏（分组+筛选+快捷操作）可隐藏
    "ui.show_quick_stats": True,
    "ui.show_status_traffic": True,
    "ui.log_max_lines": 2000,
    "ui.log_show_time": True,
    "ui.log_auto_scroll": True,
    "ui.remember_geometry": True,
    "ui.window_geometry": "",
    "ui.window_state": "",
    "ui.horizontal_splitter": [],
    "ui.vertical_splitter": [],
    "ui.dock_splitter": [],
    "ui.compact_toolbar": True,
    # ---- 数据库 ----
    "database.auto_vacuum": True, "database.keep_latency_history": True,   # v1.9.33 成对同行（守 1000 行红线）
    "database.latency_history_days": 30, "database.connection_history_days": 7,
    "database.keep_connection_history": False, "database.max_connection_rows": PMS_MAX_CONNECTION_ROWS,
    "database.backup_on_exit": False, "database.backup_dir": "",
    # ---- 日志 / 临时文件自动清理（v1.9.33）----
    "maintenance.auto_clean_enabled": True, "maintenance.retention_days": 7,   # 默认开启，保留 7 天
    # ---- 统计 ----
    "stats.source": PMS_STATS_SOURCE_AUTO, "stats.interval_ms": PMS_TRAFFIC_TICK_MS,
    "stats.clash_api_port": 0, "stats.xray_api_port": 0, "stats.count_direct_traffic": True,   # v1.9.37 同行
    # ---- 高级 ----
    "advanced.debug_mode": False, "advanced.clipboard_watch": False,
    "advanced.max_log_file_mb": 8, "advanced.settings_history_max": 50,   # v1.9.17 同一行追加（守 1000 行红线）
    "advanced.dedup_by_server_port": True, "advanced.import_skip_invalid": True,
    "advanced.show_import_report": True,
    # ---- 接口（v1.6.0：本地 HTTP API / Python 包接口）----
    "api.enabled": False, "api.host": PMS_API_DEFAULT_HOST,
    "api.port": PMS_API_DEFAULT_PORT, "api.auth_mode": PMS_API_AUTH_AUTO, "api.write_enabled": False,   # v1.9.37 同行（守 1000 行红线）
    # v1.6.2：写操作总开关，**默认关闭**（只读不受影响）；v1.9.35：本地 Web 面板（`/panel`，默认开；页面不含数据）
    "api.web_panel_enabled": True,
    # ---- 自动切换（v1.6.1：定时健康检测 + 当前节点失效自动切到可用节点）----
    "autoswitch.enabled": False, "autoswitch.interval_seconds": PMS_AUTOSWITCH_DEFAULT_INTERVAL_SECONDS,
    "autoswitch.fail_threshold": PMS_AUTOSWITCH_DEFAULT_FAIL_THRESHOLD,
    "autoswitch.detect_mode": PMS_AUTOSWITCH_DETECT_TCP, "autoswitch.notify": True,   # v1.9.33 同一行追加
    # v1.9.33：测速结束后自动切到最快节点（与定时自动切换共用同一条切换链路）
    "autoswitch.fastest_after_speed": True, "autoswitch.fastest_scope": PMS_FASTEST_SCOPE_TESTED, "autoswitch.fastest_min_speed": 0,
    # ---- 节点健康检查（v1.9.37）----
    # 与「订阅」「自动切换」都不是一回事：周期扫全库非手动禁用节点，连续失败到阈值先真连接确认再自动禁用，
    # 被自己禁用的节点下个周期重新探测，真连接可用就自动复活。默认**开启**，但首轮只建周期（见 healthcheck.py）。
    "health.enabled": True, "health.period_days": 7, "health.fail_threshold": 3,   # 周期 1~30 天；阈值 1~10 次
    "health.batch_size": 300, "health.jitter_minutes": 30, "health.notify": True,   # 每批最多探测多少个；抖动 ±分钟
}
