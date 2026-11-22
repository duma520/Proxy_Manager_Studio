#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常量与设置默认值

全部 PM S_* 常量、173 项设置默认值与区块标题；新增设置项请同时更新本文件与《软件升级迭代记录.md》第九章。
"""

from __future__ import annotations

from typing import Any
import sys
# --------------------------------------------------------------------------------------
# 以下常量与设置默认值定义在 `pms/settings_defaults.py`（v1.9.42 拆出去腾红线），
# 这里再导出一遍，保证 `from pms.constants import X` 的老写法全部继续可用。
# **必须放在文件靠前**：本文件后面还有别的常量表会引用这些名字（第一版放到文件尾，
# 结果 `PMS_API_AUTH_TITLES` 先执行、NameError）。
# --------------------------------------------------------------------------------------
from pms.settings_defaults import (  # noqa: E402,F401
    PMS_API_AUTH_AUTO,
    PMS_API_DEFAULT_HOST,
    PMS_API_DEFAULT_PORT,
    PMS_AUTOSWITCH_DEFAULT_FAIL_THRESHOLD,
    PMS_AUTOSWITCH_DEFAULT_INTERVAL_SECONDS,
    PMS_AUTOSWITCH_DETECT_TCP,
    PMS_BLOCK_AD_DOMAIN_PRESET,
    PMS_BYPASS_DOMAIN_PRESET,
    PMS_COLUMN_INDEX,
    PMS_CORE_KIND_AUTO,
    PMS_DEFAULT_RULESET_GEOSITE_CN,
    PMS_DEFAULT_SPEED_TEST_URL,
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_DEFAULT_TELEGRAM_EXTERNAL_PROXY,
    PMS_DEFAULT_TEST_URLS,
    PMS_DEFAULT_TUN_INTERFACE,
    PMS_DIRECT_DOMAIN_PRESET,
    PMS_FASTEST_SCOPE_TESTED,
    PMS_MAX_CONNECTION_ROWS,
    PMS_PAC_FILENAME,
    PMS_CUSTOM_PAC_FILENAME,   # v1.9.62：内置 PAC 编辑器产出的自定义脚本（与系统代理自动生成的那个分开）
    PMS_ROUTING_MODE_RULE,
    PMS_RULESET_SOURCE_LOCAL,
    PMS_SETTINGS_DEFAULTS,
    PMS_SORT_SCOPE_ALL,
    PMS_SORT_SCOPE_PAGE,
    PMS_SPEED_SOURCE_CLOUDFLARE,
    PMS_STATS_SOURCE_AUTO,
    PMS_SYSTEM_PROXY_MODE_FIXED,
    PMS_SYSTEM_PROXY_MODE_PAC,
    PMS_TELEGRAM_PROXY_AUTO,
    PMS_TEST_MODE_URL,
    PMS_THEME_DARK,
    PMS_THEME_LIGHT,
    PMS_THEME_NATIVE,
    PMS_THEME_SYSTEM,
    PMS_TRAFFIC_TICK_MS,
)


PMS_IS_WINDOWS = sys.platform.startswith("win")

# --------------------------------------------------------------------------------------
# 应用常量（改名会导致文档、脚手架、打包脚本失配，务必同步更新）
# --------------------------------------------------------------------------------------
PMS_APP_NAME = "代理节点管理控制台"

PMS_APP_EN_NAME = "Proxy_Manager_Studio"

PMS_APP_VERSION = "1.9.77"

PMS_APP_BUILD_DATE = "2026-10-20"

PMS_APP_ORG = "DumaStudio"

PMS_APP_DOC_NAME = "软件升级迭代记录.md"

PMS_ICON_FILENAME = "icon.ico"

PMS_ENV_FILENAME = ".env"

PMS_SETTINGS_FILENAME = "pms_settings.json"

PMS_DATABASE_FILENAME = "pms_store.db"

PMS_LOG_FILENAME = "pms_runtime.log"

PMS_SOURCE_FILENAME = PMS_APP_EN_NAME + ".py"

PMS_LOGGER_NAME = "pms"

PMS_EXE_FILENAME = PMS_APP_EN_NAME + ".exe"

PMS_DIR_CORE = "core"

PMS_DIR_DATA = "data"

PMS_DIR_LOGS = "logs"

PMS_DIR_VERSION = "version"

PMS_DIR_SCAFFOLD = "_scaffold"

PMS_DIR_CONFIGS = "configs"

PMS_DIR_TEMP = "temp"

PMS_SETTINGS_SAVE_DEBOUNCE_MS = 250

PMS_STATUS_TICK_MS = 1000


PMS_CONNECTION_TICK_MS = 1500

# 退出流程里等待线程池收尾的上限（毫秒）：超时就不再等；v1.9.9 由 3000 提到 10000，给「打断会话」留收尾时间
PMS_EXIT_POOL_WAIT_MS = 10000


PMS_API_PATH_PREFIX = "/api/v1"

PMS_API_TOKEN_ENV = "PMS_API_TOKEN"     # token 只存 .env，绝不进代码与 pms_settings.json

PMS_API_TOKEN_BYTES = 24                # 24 字节 → 32 位 urlsafe base64


PMS_API_AUTH_MANUAL = "manual"          # 用户自己填，写入 .env

PMS_API_AUTH_OFF = "off"                # 关闭鉴权（只允许本机地址）

PMS_API_AUTH_TITLES: dict[str, str] = {
    PMS_API_AUTH_AUTO: "自动生成（写入 .env）",
    PMS_API_AUTH_MANUAL: "自己填（写入 .env）",
    PMS_API_AUTH_OFF: "关闭鉴权（仅限本机地址）",
}

# 视为「本机」的监听地址；非本机地址一律强制要求鉴权，拒绝裸奔
PMS_API_LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1", "[::1]")

# --------------------------------------------------------------------------------------
# 写操作（v1.6.2）
# --------------------------------------------------------------------------------------
# 写操作请求要派发到界面线程执行，等待结果的超时（毫秒）
PMS_API_WRITE_TIMEOUT_MS = 5000
PMS_API_WRITE_MIN_TIMEOUT_MS = 500
PMS_API_WRITE_MAX_TIMEOUT_MS = 60000

# 允许通过接口执行的写动作（**白名单**：不在这里面的一律拒绝）
PMS_API_WRITE_ACTIONS: tuple[str, ...] = (
    "core_start",
    "core_stop",
    "core_restart",
    "node_switch",
    "system_proxy",
    "tun",
    "node_add",
    "node_update",
    "node_delete",
    # v1.6.3：测速与导入
    "test_latency",
    "test_speed",
    "test_stop", "test_resume",  # v1.8.0：test_resume = 断点续跑（行尾追加，不增加行数）
    "nodes_import",
)

# v1.6.3：只读但仍需走界面线程的动作（导出不改数据，**不受 `api.write_enabled` 限制**，
# 但同样受白名单约束，避免接口被人拿来当任意函数调用器）
PMS_API_READ_ACTIONS: tuple[str, ...] = (
    "export_links",
    "export_backup",
    "export_clash",
    "export_singbox",
)

# v1.6.3：测速带 `wait=true` 时的最长等待（毫秒）。
# 注意：等待是在界面线程用 QTimer 轮询，**不阻塞界面**，超限只返回已测到的部分结果。
PMS_API_WAIT_DEFAULT_TIMEOUT_MS = 30000
PMS_API_WAIT_MIN_TIMEOUT_MS = 1000
PMS_API_WAIT_MAX_TIMEOUT_MS = 120000
PMS_API_WAIT_POLL_INTERVAL_MS = 300
PMS_API_WAIT_GRACE_MS = 3000      # 发起后最多给这么久「还没跑起来」的宽限

PMS_AUTOSWITCH_DETECT_HTTP = "http"      # HTTP 探测：走本地代理端口发一次请求（验证真实通路）
PMS_FASTEST_SCOPE_ALL = "all"; PMS_FASTEST_SCOPE_GROUP = "group"   # 同行追加（守 1000 行红线）

PMS_AUTOSWITCH_DETECT_TITLES: dict[str, str] = {
    PMS_AUTOSWITCH_DETECT_TCP: "TCP 握手（连节点服务器端口）", PMS_AUTOSWITCH_DETECT_HTTP: "HTTP 探测（经本地代理访问测试网址）",
}

# v1.9.33：测速结束后「自动切到最快节点」的候选范围（与定时自动切换整合）
PMS_FASTEST_SCOPE_TITLES: dict[str, str] = {PMS_FASTEST_SCOPE_TESTED: "本次测速的节点", PMS_FASTEST_SCOPE_ALL: "全部已启用节点", PMS_FASTEST_SCOPE_GROUP: "当前分组内的节点"}

PMS_AUTOSWITCH_MIN_INTERVAL_SECONDS = 10
PMS_AUTOSWITCH_MAX_INTERVAL_SECONDS = 3600

PMS_AUTOSWITCH_MIN_FAIL_THRESHOLD = 1
PMS_AUTOSWITCH_MAX_FAIL_THRESHOLD = 10

# 一轮最多探测多少个候选节点（防止几千个节点时打满线程）
PMS_AUTOSWITCH_MAX_CANDIDATES = 24

# 候选探测的并发度（TCP 握手很轻，并发跑完再挑最快）
PMS_AUTOSWITCH_PROBE_CONCURRENCY = 8

# 启动后延迟多久做第一次检测：避开启动高峰，别刚起核心就被判失败
PMS_AUTOSWITCH_FIRST_DELAY_MS = 20000

# 发出切换请求后的冷却：核心重启需要时间，这段时间不再检测
PMS_AUTOSWITCH_COOLDOWN_SECONDS = 8

PMS_MAX_LOG_LINES = 5000


PMS_CORE_RESTART_MIN_DELAY_MS = 800

PMS_CORE_RESTART_MAX_DELAY_MS = 20000

# v1.9.56：核心稳定运行超过这么久，才把「连续异常退出计数」清零。
# 之前是「启动成功就清零」——启动即崩（如桥接撑爆命令行）时计数永远停在 1，
# `core.restart_max_times` 形同虚设，日志刷成重启风暴（用户实测报障）。
PMS_CORE_RESTART_RESET_AFTER_SECONDS = 60.0

# v1.9.56：Tor 桥接写进配置的上限（条）。sing-box 把 torrc + extra_args 全部拼进
# tor 的命令行，Windows CreateProcess 命令行上限 32767 字符；实测每条桥接
# （`--Bridge` + 行）约 166 字符，1615 条 ≈ 268KB 直接撑爆：
# `fork/exec tor.exe: the filename or extension is too long`（真进程取证）。
# 150 条 ≈ 25KB，留一半余量给插件行与路径。
PMS_TOR_MAX_BRIDGES_IN_CONFIG = 150

PMS_PROCESS_STOP_GRACE_MS = 1500


PMS_CORE_KIND_XRAY = "xray"

PMS_CORE_KIND_SINGBOX = "singbox"

PMS_CORE_KIND_TITLES: dict[str, str] = {
    PMS_CORE_KIND_AUTO: "自动（按节点选：优先 sing-box，不行用 Xray）",
    PMS_CORE_KIND_XRAY: "Xray-core（xray.exe）",
    PMS_CORE_KIND_SINGBOX: "sing-box（sing-box.exe）",
}

PMS_CORE_BINARY_NAMES: dict[str, tuple[str, ...]] = {
    PMS_CORE_KIND_XRAY: ("xray.exe", "xray"),
    PMS_CORE_KIND_SINGBOX: ("sing-box.exe", "sing-box"),
}

PMS_ROUTING_MODE_GLOBAL = "global"


PMS_ROUTING_MODE_DIRECT = "direct"

PMS_ROUTING_MODE_TITLES: dict[str, str] = {
    PMS_ROUTING_MODE_GLOBAL: "全局代理（全部流量走代理）",
    PMS_ROUTING_MODE_RULE: "规则分流（大陆直连 / 其余走代理）",
    PMS_ROUTING_MODE_DIRECT: "直连（只启动本地入口，不转发）",
}

PMS_THEME_TITLES: dict[str, str] = {PMS_THEME_LIGHT: "浅色", PMS_THEME_DARK: "深色", PMS_THEME_SYSTEM: "跟随系统", PMS_THEME_NATIVE: "原生（系统默认）"}

PMS_TEST_MODE_TCP = "tcp"


PMS_TEST_MODE_TITLES: dict[str, str] = {
    PMS_TEST_MODE_URL: "真连接延迟（URL 测试，经核心真实请求）",
    PMS_TEST_MODE_TCP: "TCP 握手延迟（直连目标端口）",
}


PMS_SORT_SCOPE_TITLES: dict[str, str] = {PMS_SORT_SCOPE_ALL: "全量（整个筛选结果）", PMS_SORT_SCOPE_PAGE: "当前页"}


PMS_STATS_SOURCE_CLASH = "clash_api"

PMS_STATS_SOURCE_XRAY = "xray_api"

PMS_STATS_SOURCE_LOG = "log_only"

PMS_STATS_SOURCE_TITLES: dict[str, str] = {
    PMS_STATS_SOURCE_AUTO: "自动（sing-box 用 Clash API，Xray 用 Stats API）",
    PMS_STATS_SOURCE_CLASH: "Clash API（experimental.clash_api）",
    PMS_STATS_SOURCE_XRAY: "Xray Stats API（xray api statsquery）",
    PMS_STATS_SOURCE_LOG: "仅日志统计（不查询核心接口）",
}

PMS_PROTOCOL_VMESS = "vmess"

PMS_PROTOCOL_VLESS = "vless"

PMS_PROTOCOL_TROJAN = "trojan"

PMS_PROTOCOL_SHADOWSOCKS = "shadowsocks"

PMS_PROTOCOL_HYSTERIA2 = "hysteria2"

PMS_PROTOCOL_TUIC = "tuic"

PMS_PROTOCOL_SOCKS = "socks"

PMS_PROTOCOL_HTTP = "http"

PMS_PROTOCOL_SSR = "ssr"
PMS_PROTOCOL_NAIVE = "naive"
PMS_PROTOCOL_ANYTLS = "anytls"
PMS_PROTOCOL_SHADOWTLS = "shadowtls"
PMS_PROTOCOL_SNELL = "snell"
PMS_PROTOCOL_WIREGUARD = "wireguard"
PMS_PROTOCOL_JUICITY = "juicity"
PMS_PROTOCOL_HYSTERIA1 = "hysteria1"
PMS_PROTOCOL_SOCKS4 = "socks4"

# v1.9.68：Telegram 专属代理（来自 t.me/webproxy 与 t.me/proxy 分享链接）
#   · tg_webproxy：标准 SOCKS5 代理，secret 就是 16 字节密码；能当通用出口，也能复用延迟/测速/电报测试
#   · tg_proxy：MTProto 电报代理（中间人代理），Xray/sing-box 没有 MTProto 出站，**不能**当通用出口，
#               只用于「电报代理连通直测」（独立弹窗粘贴链接，原生 socket 握 MTProto 看能否连通电报）
#   两者都进 TITLES / ORDER（类型列、筛选、按类型复制、编辑器下拉都要能识别），但**不进** EXTRA_PROTOCOLS：
#   EXTRA 是「解析进 extra_json、核心不原生支持」的扩展协议，而这两个是本项目原生支持、需要落库的。
PMS_PROTOCOL_TG_WEBPROXY = "tg_webproxy"
PMS_PROTOCOL_TG_PROXY = "tg_proxy"

PMS_EXTRA_PROTOCOLS: frozenset[str] = frozenset((
    PMS_PROTOCOL_SSR,
    PMS_PROTOCOL_NAIVE,
    PMS_PROTOCOL_ANYTLS,
    PMS_PROTOCOL_SHADOWTLS,
    PMS_PROTOCOL_SNELL,
    PMS_PROTOCOL_WIREGUARD,
    PMS_PROTOCOL_JUICITY,
    PMS_PROTOCOL_HYSTERIA1,
    PMS_PROTOCOL_SOCKS4,
))

PMS_PROTOCOL_ORDER: tuple[str, ...] = (
    PMS_PROTOCOL_VMESS,
    PMS_PROTOCOL_VLESS,
    PMS_PROTOCOL_TROJAN,
    PMS_PROTOCOL_SHADOWSOCKS,
    PMS_PROTOCOL_HYSTERIA2,
    PMS_PROTOCOL_TUIC,
    PMS_PROTOCOL_SOCKS,
    PMS_PROTOCOL_HTTP,
    PMS_PROTOCOL_SSR,
    PMS_PROTOCOL_NAIVE,
    PMS_PROTOCOL_ANYTLS,
    PMS_PROTOCOL_SHADOWTLS,
    PMS_PROTOCOL_SNELL,
    PMS_PROTOCOL_WIREGUARD,
    PMS_PROTOCOL_JUICITY,
    PMS_PROTOCOL_HYSTERIA1,
    PMS_PROTOCOL_SOCKS4,
    PMS_PROTOCOL_TG_WEBPROXY,
    PMS_PROTOCOL_TG_PROXY,
)

PMS_PROTOCOL_TITLES: dict[str, str] = {
    PMS_PROTOCOL_VMESS: "VMess",
    PMS_PROTOCOL_VLESS: "VLESS",
    PMS_PROTOCOL_TROJAN: "Trojan",
    PMS_PROTOCOL_SHADOWSOCKS: "Shadowsocks",
    PMS_PROTOCOL_HYSTERIA2: "Hysteria2",
    PMS_PROTOCOL_TUIC: "TUIC",
    PMS_PROTOCOL_SOCKS: "Socks",
    PMS_PROTOCOL_HTTP: "HTTP",
    PMS_PROTOCOL_SSR: "SSR",
    PMS_PROTOCOL_NAIVE: "NaiveProxy",
    PMS_PROTOCOL_ANYTLS: "AnyTLS",
    PMS_PROTOCOL_SHADOWTLS: "ShadowTLS",
    PMS_PROTOCOL_SNELL: "Snell",
    PMS_PROTOCOL_WIREGUARD: "WireGuard",
    PMS_PROTOCOL_JUICITY: "Juicity",
    PMS_PROTOCOL_HYSTERIA1: "Hysteria1",
    PMS_PROTOCOL_SOCKS4: "Socks4",
    # v1.9.68：Telegram 专属代理类型名——特意不用「Socks5」字样，便于一眼看出来源
    PMS_PROTOCOL_TG_WEBPROXY: "TgWebProxy",
    PMS_PROTOCOL_TG_PROXY: "TgProxy",
}

PMS_HAPP_SCHEME = "happ"   # Happ 客户端链接（happ://crypt*/加密订阅、happ://add/明文订阅），不是节点协议

# v1.9.68：Telegram 专属代理分享链接（不是标准 scheme://，而是 https://t.me/<path>?... 形态）
#   · https://t.me/webproxy?server=..&port=..&secret=..  → 标准 SOCKS5（secret 即 16 字节密码）
#   · https://t.me/proxy?server=..&port=..&secret=..      → MTProto 电报代理（仅测电报连通）
# 解析层在 pslp_parse_line 里按「主机 t.me + 路径」特判，不进 PMS_SHARE_SCHEME_TO_PROTOCOL。
PMS_TG_PROXY_HOSTS: frozenset[str] = frozenset(("t.me", "telegram.me", "tg.dev"))
PMS_TG_PROXY_PATHS: frozenset[str] = frozenset(("proxy", "webproxy", "socks"))

PMS_SHARE_SCHEME_TO_PROTOCOL: dict[str, str] = {
    "vmess": PMS_PROTOCOL_VMESS,
    "vless": PMS_PROTOCOL_VLESS,
    "trojan": PMS_PROTOCOL_TROJAN,
    "trojan-go": PMS_PROTOCOL_TROJAN,
    "ss": PMS_PROTOCOL_SHADOWSOCKS,
    "shadowsocks": PMS_PROTOCOL_SHADOWSOCKS,
    "hy2": PMS_PROTOCOL_HYSTERIA2,
    "hysteria2": PMS_PROTOCOL_HYSTERIA2,
    "hysteria": PMS_PROTOCOL_HYSTERIA2,
    "tuic": PMS_PROTOCOL_TUIC,
    "socks": PMS_PROTOCOL_SOCKS,
    "socks5": PMS_PROTOCOL_SOCKS,
    "http": PMS_PROTOCOL_HTTP,
    "https": PMS_PROTOCOL_HTTP,
    "ssr": PMS_PROTOCOL_SSR,
    "naive": PMS_PROTOCOL_NAIVE,
    "naive+https": PMS_PROTOCOL_NAIVE,
    "anytls": PMS_PROTOCOL_ANYTLS,
    "shadowtls": PMS_PROTOCOL_SHADOWTLS,
    "snell": PMS_PROTOCOL_SNELL,
    "wireguard": PMS_PROTOCOL_WIREGUARD,
    "wg": PMS_PROTOCOL_WIREGUARD,
    "juicity": PMS_PROTOCOL_JUICITY,
    "hysteria1": PMS_PROTOCOL_HYSTERIA1,
    "socks4": PMS_PROTOCOL_SOCKS4,
}

PMS_TRANSPORT_TCP = "tcp"

PMS_TRANSPORT_WS = "ws"

PMS_TRANSPORT_GRPC = "grpc"

PMS_TRANSPORT_HTTP = "http"

PMS_TRANSPORT_H2 = "h2"

PMS_TRANSPORT_XHTTP = "xhttp"

PMS_TRANSPORT_HTTPUPGRADE = "httpupgrade"

PMS_TRANSPORT_TITLES: dict[str, str] = {
    PMS_TRANSPORT_TCP: "TCP（原始）",
    PMS_TRANSPORT_WS: "WebSocket",
    PMS_TRANSPORT_GRPC: "gRPC",
    PMS_TRANSPORT_HTTP: "HTTP/1.1（http 伪装）",
    PMS_TRANSPORT_H2: "HTTP/2",
    PMS_TRANSPORT_XHTTP: "XHTTP（Xray 新传输）",
    PMS_TRANSPORT_HTTPUPGRADE: "HTTPUpgrade",
}

PMS_TRANSPORT_ALIASES: dict[str, str] = {
    "": PMS_TRANSPORT_TCP,
    "tcp": PMS_TRANSPORT_TCP,
    "raw": PMS_TRANSPORT_TCP,
    "none": PMS_TRANSPORT_TCP,
    "ws": PMS_TRANSPORT_WS,
    "websocket": PMS_TRANSPORT_WS,
    "grpc": PMS_TRANSPORT_GRPC,
    "gun": PMS_TRANSPORT_GRPC,
    "http": PMS_TRANSPORT_HTTP,
    "h2": PMS_TRANSPORT_H2,
    "h2c": PMS_TRANSPORT_H2,
    "http2": PMS_TRANSPORT_H2,
    "xhttp": PMS_TRANSPORT_XHTTP,
    "splithttp": PMS_TRANSPORT_XHTTP,
    "httpupgrade": PMS_TRANSPORT_HTTPUPGRADE,
}

PMS_SECURITY_NONE = "none"

PMS_SECURITY_TLS = "tls"

PMS_SECURITY_REALITY = "reality"

PMS_SECURITY_TITLES: dict[str, str] = {
    PMS_SECURITY_NONE: "无（明文）",
    PMS_SECURITY_TLS: "TLS",
    PMS_SECURITY_REALITY: "Reality",
}

PMS_SS_METHODS: tuple[str, ...] = (
    "aes-128-gcm",
    "aes-192-gcm",
    "aes-256-gcm",
    "aes-128-cfb",
    "aes-192-cfb",
    "aes-256-cfb",
    "aes-128-ctr",
    "aes-192-ctr",
    "aes-256-ctr",
    "chacha20-ietf-poly1305",
    "xchacha20-ietf-poly1305",
    "chacha20-ietf",
    "chacha20",
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
    "none",
)

PMS_VMESS_SECURITY: tuple[str, ...] = ("auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero")

PMS_TLS_FINGERPRINTS: tuple[str, ...] = (
    "chrome",
    "firefox",
    "safari",
    "ios",
    "android",
    "edge",
    "360",
    "qq",
    "random",
    "randomized",
)

PMS_TUIC_CONGESTION: tuple[str, ...] = ("bbr", "cubic", "new_reno")

PMS_TUIC_UDP_MODES: tuple[str, ...] = ("native", "quic")

PMS_LATENCY_GRADE_GOOD = 300

PMS_LATENCY_GRADE_MEDIUM = 800


PMS_SPEED_SOURCE_CACHEFLY = "cachefly"

PMS_SPEED_SOURCE_CUSTOM = "custom"

PMS_SPEED_SOURCE_CHECK_SIZES: tuple[int, ...] = (1, 2, 5, 10, 20, 50, 100)

PMS_SPEED_SOURCE_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "key": PMS_SPEED_SOURCE_CLOUDFLARE,
        "name": "Cloudflare（任意 MB）",
        "template": "https://speed.cloudflare.com/__down?bytes={bytes}",
        "verified": True,
        "note": "已实测可用：bytes 参数精确生效，1/5/10/20 MB 均返回 200 且长度一致，可任意指定大小。",
    },
    {
        "key": PMS_SPEED_SOURCE_CACHEFLY,
        "name": "CacheFly（固定档位）",
        "template": "http://cachefly.cachefly.net/{mb}mb.test",
        "verified": False,
        "note": "本机网络不可达，未实测。公开资料仅 10mb / 100mb 有真实下载佐证，其余档位说法互相矛盾，请用「自检可用档位」在你的网络下验证。",
    },
)

PMS_SPEED_SOURCE_TITLES: dict[str, str] = {
    str(preset["key"]): str(preset["name"])
    for preset in PMS_SPEED_SOURCE_PRESETS
}

PMS_SPEED_SOURCE_TEMPLATES: dict[str, str] = {
    str(preset["key"]): str(preset["template"])
    for preset in PMS_SPEED_SOURCE_PRESETS
}


PMS_TELEGRAM_PROXY_LOCAL = "local"

PMS_TELEGRAM_PROXY_EXTERNAL = "external"

PMS_TELEGRAM_PROXY_NONE = "none"

PMS_TELEGRAM_PROXY_MODE_TITLES: dict[str, str] = {
    PMS_TELEGRAM_PROXY_AUTO: "自动（按自定义次序退让）",
    PMS_TELEGRAM_PROXY_LOCAL: "只用本程序代理（当前节点）",
    PMS_TELEGRAM_PROXY_EXTERNAL: "只用外部代理",
    PMS_TELEGRAM_PROXY_NONE: "直连（不使用代理）",
}

# v1.9.73：订阅/电报抓取通路次序自定义。
# token 集合（与 proxy_mode 预设解耦）：本程序代理 / 外部代理 / 直连。
PMS_TELEGRAM_ROUTE_LOCAL = "local"
PMS_TELEGRAM_ROUTE_EXTERNAL = "external"
PMS_TELEGRAM_ROUTE_DIRECT = "direct"
PMS_TELEGRAM_ROUTE_TITLES: dict[str, str] = {
    PMS_TELEGRAM_ROUTE_LOCAL: "本程序代理",
    PMS_TELEGRAM_ROUTE_EXTERNAL: "外部代理",
    PMS_TELEGRAM_ROUTE_DIRECT: "直连",
}
# 默认次序与旧 auto 行为一致，向后兼容；仅 auto 模式生效，其它预设模式忽略。
PMS_TELEGRAM_DEFAULT_PROXY_ORDER = "local,external,direct"


PMS_DEFAULT_RULESET_GEOIP_CN = (
    "https://raw.githubusercontent.com/SagerNet/sing-geoip/rule-set/geoip-cn.srs"
)

PMS_RULESET_SOURCE_REMOTE = "remote"
PMS_RULESET_SOURCE_TITLES: dict[str, str] = {
    PMS_RULESET_SOURCE_LOCAL: "本地文件（core\\srss）",
    PMS_RULESET_SOURCE_REMOTE: "远程下载（GitHub）",
}
# core 目录下存放 .srs 规则的候选子目录（按序探测，第一个存在的胜出）
PMS_RULESET_LOCAL_DIR_NAMES: tuple[str, ...] = ("srss", "ruleset", "rulesets")
PMS_RULESET_LOCAL_GEOSITE_CN = "geosite-cn.srs"
PMS_RULESET_LOCAL_GEOIP_CN = "geoip-cn.srs"

PMS_COLUMN_ENABLED = 0


PMS_COLUMN_TYPE = 2

PMS_COLUMN_NAME = 3

PMS_COLUMN_ADDRESS = 4

PMS_COLUMN_PORT = 5

PMS_COLUMN_LATENCY = 6

PMS_COLUMN_SPEED = 7

PMS_COLUMN_TELEGRAM = 8

PMS_COLUMN_GROUP = 9

PMS_COLUMN_SUBSCRIPTION = 10

PMS_COLUMN_LAST_TEST = 11

PMS_COLUMN_REMARK = 12

PMS_COLUMN_TITLES: tuple[str, ...] = (
    "启用",
    "序号",
    "类型",
    "名称",
    "地址",
    "端口",
    "延迟",
    "测速",
    "电报",
    "分组",
    "订阅",
    "上次测试",
    "备注",
)

PMS_COLUMN_MIN_WIDTHS: tuple[int, ...] = (46, 46, 86, 190, 220, 62, 82, 96, 120, 96, 120, 132, 200)

PMS_LOG_LEVEL_TITLES: dict[str, str] = {
    "debug": "调试（debug，最详细）",
    "info": "信息（info，推荐）",
    "warning": "警告（warning）",
    "error": "错误（error，最简）",
}

PMS_LOG_LEVEL_ORDER: dict[str, int] = {"debug": 10, "info": 20, "warning": 30, "error": 40}

PMS_TUN_STACK_TITLES: dict[str, str] = {
    "mixed": "mixed（推荐，TCP gvisor + UDP system）",
    "gvisor": "gvisor（全用户态，兼容性最好）",
    "system": "system（性能最好，需驱动支持）",
}


PMS_VERSION_BACKUP_NOTE_PLACEHOLDER = "初始完整版"


PMS_SETTINGS_SECTION_TITLES: dict[str, str] = {
    "general": "常规",
    "core": "核心",
    "inbound": "本地入口",
    "system_proxy": "系统代理",
    "tun": "TUN 模式",
    "routing": "路由与分流",
    "test": "延迟测试",
    "speed": "测速",
    "subscription": "订阅",
    "telegram": "电报频道",
    "ui": "界面",
    "database": "数据库",
    "stats": "统计",
    "advanced": "高级",
    "api": "接口",
    "autoswitch": "自动切换",
    "health": "节点健康检查",
}

# --------------------------------------------------------------------------------------
# 订阅更新服务
# --------------------------------------------------------------------------------------
PMS_SUB_API_KEY_ENV = "PMS_SUB_API_KEY"

