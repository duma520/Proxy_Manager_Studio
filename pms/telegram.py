#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电报频道抓取（v1.4.4）

把 t.me 频道当成一种「频道型订阅源」：
    https://t.me/<频道>  →  https://t.me/s/<频道>（服务端渲染预览页）
    → 解析消息文本 → 按协议头切分出节点链接 → 交给既有导入流程入库

**实测依据（2026-10-08，频道 oneclickvpnkeys，经代理抓取，结论写死在后，勿凭想象改）**：
1. `/s/` 预览页是**服务端渲染 HTML**，无需登录 / API / 手机号，普通 urllib 即可抓取；
2. 每页约 18–20 条消息，`?before=<消息ID>` 可向更早翻页（实测正确返回更早的 14 条）；
3. **多个节点链接经常无换行、无空格直接连排**（实测两条 vless 之间 hasNewline=False）：
   `vless://...#备注@频道vless://...` —— 因此**绝不能按行或空白切分**，
   必须按「下一个协议头出现的位置」切分；
4. 该切分法经真实数据验证：110 条切分结果里 106 条能被 `PmsShareLinkParser` 正确解析（96%），
   4 条失败是源链接本身缺参数（xhttp 缺 path、gRPC 缺 serviceName），与切分无关。

限制：只支持**公开频道**。私有频道（`t.me/c/<数字>/<数字>`）与邀请链接需要登录，本模块一律返回空。
"""

from __future__ import annotations

from typing import (
    Any,
    Optional,
)
from urllib.parse import urlparse
from urllib.request import Request
import html
import logging
import random
import re
import time
import urllib.request

from pms.constants import (
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_LOGGER_NAME,
    PMS_SHARE_SCHEME_TO_PROTOCOL,
    PMS_TELEGRAM_DEFAULT_PROXY_ORDER,
    PMS_TELEGRAM_PROXY_AUTO,
    PMS_TELEGRAM_PROXY_EXTERNAL,
    PMS_TELEGRAM_PROXY_LOCAL,
    PMS_TELEGRAM_PROXY_NONE,
    PMS_TELEGRAM_ROUTE_DIRECT,
    PMS_TELEGRAM_ROUTE_EXTERNAL,
    PMS_TELEGRAM_ROUTE_LOCAL,
    PMS_TG_PROXY_HOSTS,
    PMS_TG_PROXY_PATHS,
)
from pms.utils import pms_dedupe_keep_order

PMS_TELEGRAM_HOSTS: tuple[str, ...] = ("t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog")

PMS_TELEGRAM_PREVIEW_PATH = "s"

# v1.9.2：10 → 50。每页约 18–20 条消息，50 页约 900–1000 条，
# 足以覆盖绝大多数频道的全量；翻页遇到空页会自动停，不会白跑。
PMS_TELEGRAM_MAX_PAGES = 50

# v1.9.5：「翻到底」哨兵页数。显式传这个值 = 不受 50 页上限约束，一直往回翻到频道开头
# （遇空页 / 抓取失败自动停）。下面这个硬保险是最后一道闸：即便哨兵模式也不会超过它，
# 防止频道异常（例如页面永远返回同一批消息）时无限循环打爆 t.me。
PMS_TELEGRAM_PAGES_TO_BEGINNING = -1
PMS_TELEGRAM_ABSOLUTE_MAX_PAGES = 2000

# v1.9.6：慢速回填（点滴爬历史）的默认参数。大频道一次爬完上千页必被限流，
# 改成「每次更新只翻几页 + 页间随机停顿」，分很多轮悄悄补完。
PMS_TELEGRAM_DEFAULT_BACKFILL_PAGES = 10     # 每轮页数（1–50）
PMS_TELEGRAM_DEFAULT_BACKFILL_DELAY = 2.0    # 每页之间基准停顿秒数（0 = 不停）
PMS_TELEGRAM_BACKFILL_JITTER = 0.3           # 停顿抖动 ±30%，避免固定节奏像机器人
PMS_TELEGRAM_BACKFILL_MAX_DELAY = 30.0       # 单页停顿上限，防手滑填个天文数字

PMS_TELEGRAM_DEFAULT_TIMEOUT = 20.0

# v1.9.2：抓取模式（不新增 constants.py 行——该文件已顶在 1000 行红线，常量放本模块）
PMS_TELEGRAM_FETCH_MODE_FULL = "full"              # 全量：从最新页往回翻满设定页数（遇到空页提前停）
PMS_TELEGRAM_FETCH_MODE_INCREMENTAL = "incremental"  # 增量：只收比上次最大消息号更新的贴，越界即停
PMS_TELEGRAM_FETCH_MODES: tuple[str, ...] = (
    PMS_TELEGRAM_FETCH_MODE_FULL,
    PMS_TELEGRAM_FETCH_MODE_INCREMENTAL,
)
PMS_TELEGRAM_FETCH_MODE_TITLES: dict[str, str] = {
    PMS_TELEGRAM_FETCH_MODE_FULL: "全量（按设定页数往回翻，翻到底自动停）",
    PMS_TELEGRAM_FETCH_MODE_INCREMENTAL: "增量（只抓上次之后的新贴）",
}

# 频道名（Telegram 用户名）：字母开头，字母数字下划线，总长 5-32
PMS_TELEGRAM_CHANNEL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

# 非频道路径：私有频道 / 邀请链接 / 贴纸等，一律不支持
PMS_TELEGRAM_NON_CHANNEL_PATHS: frozenset[str] = frozenset({
    "c", "joinchat", "addstickers", "addemoji", "socks", "proxy", "iv", "share", "login", "confirmphone",
})

# 协议头白名单直接复用现有协议表（避免维护第二套名单）；长的排前面，防止 ss 抢先匹配 ssr
PMS_TELEGRAM_SCHEME_PATTERN = re.compile(
    r"(" + "|".join(sorted((re.escape(scheme) for scheme in PMS_SHARE_SCHEME_TO_PROTOCOL), key=len, reverse=True)) + r")://"
)

# 从 <a href> 找订阅地址时要排除的主机（Telegram 自身 + 常见应用商店 / 代码托管）
PMS_TELEGRAM_SUB_SKIP_HOSTS: frozenset[str] = frozenset({
    "t.me", "www.t.me", "telegram.me", "telegram.dog", "core.telegram.org",
    "play.google.com", "apps.apple.com", "itunes.apple.com", "github.com",
    "raw.githubusercontent.com", "f-droid.org",
})

# v1.9.74：Telegram 专属代理分享链接（https://t.me/proxy?... 与 https://t.me/webproxy?...）
# 既不是节点协议头（vmess://…），也不是订阅地址（t.me 在订阅跳过名单里），
# 旧版 `pms_telegram_extract_links()` 末尾显式丢弃所有 http(s):// 段，导致这类链接在抽取阶段
# 整段丢失、永远不入库。这里单独抽出来，复用 constants 里的 TG 主机/路径白名单。
PMS_TELEGRAM_TG_PROXY_PATTERN = re.compile(
    r"https?://(?:[A-Za-z0-9.-]+\.)?"
    r"(?:" + "|".join(re.escape(host) for host in sorted(PMS_TG_PROXY_HOSTS, key=len, reverse=True)) + r")"
    r"/(?:" + "|".join(re.escape(path) for path in PMS_TG_PROXY_PATHS) + r")\b[^\s]*",
    re.IGNORECASE,
)

def pms_telegram_extract_tg_proxies(pms_text: str) -> list[str]:
    """从消息文本里切分出 Telegram 专属代理链接（t.me/proxy?… 与 t.me/webproxy?…）。

    这类链接是 https:// 形态，旧版被 `pms_telegram_extract_links()` 当作「订阅地址」整段丢弃，
    导致电报频道发的 tg 专属代理永远进不了节点库。这里独立抽取，还原 `&amp;` 并去掉结尾标点。
    下游 `ptic_import_text → pslp_parse_line` 已能按「主机 + 路径」特判成 TgProxy / TgWebProxy 节点。
    """
    text = str(pms_text or "")
    if not text:
        return []
    found: list[str] = []
    for match in re.finditer(PMS_TELEGRAM_TG_PROXY_PATTERN, text):
        raw = html.unescape(match.group(0)).strip().strip(".,;:！？；，、。")
        if raw and raw.lower().startswith(("http://", "https://")):
            found.append(raw)
    return pms_dedupe_keep_order(found)

# --------------------------------------------------------------------------------------
# 链接识别与规范化
# --------------------------------------------------------------------------------------
def pms_telegram_channel_from_url(pms_url: str) -> str:
    """从任意形态的电报链接提取频道名；不是公开频道链接则返回空串。

    支持：`https://t.me/name`、`t.me/s/name`、`https://t.me/name/123`、`@name`、裸 `t.me/name`。
    不支持：私有频道 `t.me/c/<数字>/<数字>`、邀请链接 `t.me/joinchat/...`（都需要登录）。
    """
    text = str(pms_url or "").strip()
    if not text:
        return ""
    if text.startswith("@"):
        return text[1:].split("/", 1)[0].strip() if PMS_TELEGRAM_CHANNEL_NAME.match(text[1:].split("/", 1)[0].strip()) else ""
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    try:
        parts = urlparse(text)
    except ValueError:
        return ""
    host = str(parts.hostname or "").lower()
    if host not in PMS_TELEGRAM_HOSTS:
        return ""
    segments = [seg for seg in str(parts.path or "").split("/") if seg]
    if not segments:
        return ""
    head = segments[0].lower()
    if head in PMS_TELEGRAM_NON_CHANNEL_PATHS or head.startswith("joinchat"):
        return ""
    name = segments[1] if head == PMS_TELEGRAM_PREVIEW_PATH and len(segments) > 1 else segments[0]
    name = str(name or "").strip()
    return name if PMS_TELEGRAM_CHANNEL_NAME.match(name) else ""

def pms_telegram_preview_url(pms_channel: str) -> str:
    """频道名 → `/s/` 预览页地址。"""
    return f"https://t.me/{PMS_TELEGRAM_PREVIEW_PATH}/{str(pms_channel or '').strip()}"

def pms_telegram_is_channel_url(pms_url: str) -> bool:
    """该地址是不是一个可抓取的电报公开频道。"""
    return bool(pms_telegram_channel_from_url(pms_url))

def pms_telegram_proxy_url(pms_address: str) -> str:
    """外部代理地址规范化：`***已脱敏***-内网IP:20808` → `http://***已脱敏***-内网IP:20808`。

    必须带端口（代理地址没有端口没有意义）。只接受 http / https —— urllib 的 ProxyHandler
    不认 socks，放过去会变成「配了代理其实没走」的假象。缺端口或协议不支持时返回空串，由调用方报错。
    """
    text = str(pms_address or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlparse(text)
    except ValueError:
        return ""
    host = str(parts.hostname or "").strip()
    port = parts.port
    scheme = str(parts.scheme or "").lower()
    if not host or not port or scheme not in ("http", "https"):
        return ""
    return f"{scheme}://{host}:{port}"

def pms_telegram_parse_order(pms_order: str) -> list[str]:
    """把用户自定义次序字符串解析成去重保序的 token 列表，无效 token 自动丢弃。

    形如 ``"external,local,direct"`` → ``["external", "local", "direct"]``。
    非法 token（不是 local/external/direct）会被过滤；空串或过滤后为空返回空列表。
    """
    tokens: list[str] = []
    for raw in str(pms_order or "").split(","):
        token = str(raw or "").strip().lower()
        if token in (PMS_TELEGRAM_ROUTE_LOCAL, PMS_TELEGRAM_ROUTE_EXTERNAL, PMS_TELEGRAM_ROUTE_DIRECT):
            if token not in tokens:
                tokens.append(token)
    return tokens


def pms_telegram_build_openers(
    pms_mode: str,
    pms_local_port: int = 0,
    pms_external_proxy: str = "",
    pms_order: str = "",
) -> list[tuple[str, Any]]:
    """按通路模式组装电报抓取用的 opener 列表：[(通路名, opener)]，按优先级排列。

        auto     按 `pms_order` 自定义次序（默认 本程序代理 → 外部代理 → 直连）；t.me 必须走代理，逐个退让
        local    只用本程序代理（当前节点）
        external 只用外部代理
        none     直连

    **自定义次序（v1.9.73）**：`pms_mode == auto` 且 `pms_order` 非空时，按用户指定的
    `local/external/direct` 顺序组装；某条通路当前不可用时（本程序代理没端口 / 外部代理地址无效）
    自动跳过，不生成死链。其它预设模式忽略 `pms_order`，行为与旧版完全一致，向后兼容。

    一条可用通路都没有时返回空列表，由调用方给出明确报错。
    订阅更新线程与界面「试抓」共用这一套，避免两处逻辑走偏。
    """
    mode = str(pms_mode or PMS_TELEGRAM_PROXY_AUTO)
    routes: list[tuple[str, str]] = []
    if mode == PMS_TELEGRAM_PROXY_AUTO:
        # 自定义次序优先；空或非法时退回默认次序（与旧 auto 行为一致）
        tokens = pms_telegram_parse_order(pms_order) or pms_telegram_parse_order(PMS_TELEGRAM_DEFAULT_PROXY_ORDER)
        local_addr = f"http://127.0.0.1:{int(pms_local_port)}" if int(pms_local_port or 0) else ""
        external = pms_telegram_proxy_url(pms_external_proxy)
        for token in tokens:
            if token == PMS_TELEGRAM_ROUTE_LOCAL:
                if local_addr:
                    routes.append(("本程序代理", local_addr))
            elif token == PMS_TELEGRAM_ROUTE_EXTERNAL:
                if external:
                    routes.append(("外部代理", external))
            elif token == PMS_TELEGRAM_ROUTE_DIRECT:
                routes.append(("直连", ""))
    elif mode == PMS_TELEGRAM_PROXY_LOCAL:
        if int(pms_local_port or 0):
            routes.append(("本程序代理", f"http://127.0.0.1:{int(pms_local_port)}"))
    elif mode == PMS_TELEGRAM_PROXY_EXTERNAL:
        external = pms_telegram_proxy_url(pms_external_proxy)
        if external:
            routes.append(("外部代理", external))
    elif mode == PMS_TELEGRAM_PROXY_NONE:
        routes.append(("直连", ""))
    openers: list[tuple[str, Any]] = []
    for name, address in routes:
        if address:
            handler = urllib.request.ProxyHandler({"http": address, "https": address})
            openers.append((name, urllib.request.build_opener(handler)))
        else:
            openers.append((name, urllib.request.build_opener()))
    return openers

# --------------------------------------------------------------------------------------
# 页面解析
# --------------------------------------------------------------------------------------
def pms_telegram_html_to_text(pms_html: str) -> str:
    """消息 HTML 片段 → 纯文本（`<br>` 与 `</p>` 转成换行，其余标签去掉）。"""
    text = re.sub(r"(?i)<br\s*/?>", "\n", str(pms_html or ""))
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()

def pms_telegram_parse_messages(pms_html: str) -> list[tuple[str, str]]:
    """解析预览页 → [(消息ID, 消息纯文本)]。

    以 `data-post` 为锚切块，保证消息 ID 与正文严格一一对应（实测 18/18 对齐）。
    """
    source = str(pms_html or "")
    if not source:
        return []
    anchors = list(re.finditer(r'data-post="([^"]+)"', source))
    messages: list[tuple[str, str]] = []
    for index, anchor in enumerate(anchors):
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(source)
        block = source[anchor.start():end]
        found = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        messages.append((anchor.group(1), pms_telegram_html_to_text(found.group(1)) if found else ""))
    return messages

def pms_telegram_message_ids(pms_messages: list[tuple[str, str]]) -> list[int]:
    """从 [(消息ID, …)] 里取出数字消息号（data-post 形如 `频道/27124`）。"""
    ids: list[int] = []
    for post, _text in pms_messages:
        tail = str(post or "").rsplit("/", 1)[-1]
        if tail.isdigit():
            ids.append(int(tail))
    return ids

# --------------------------------------------------------------------------------------
# 链接切分（本模块的成败关键）
# --------------------------------------------------------------------------------------
def pms_telegram_extract_links(pms_text: str) -> list[str]:
    """从消息文本里切分出节点链接。

    **为什么不能按行切分**：实测多个 vless 链接无换行无空格直接连排，
    按行切会把一整串当成一个链接。这里按「下一个协议头出现的位置」切分。
    段内若仍有空白（正常分行发送的情况），截断到第一个空白。
    `http://` / `https://` 不是节点链接，排除（它们是订阅地址，由别的函数处理）。
    """
    text = str(pms_text or "")
    if not text:
        return []
    links: list[str] = []
    cursor = 0
    while True:
        match = PMS_TELEGRAM_SCHEME_PATTERN.search(text, cursor)
        if not match:
            break
        following = PMS_TELEGRAM_SCHEME_PATTERN.search(text, match.end())
        end = following.start() if following else len(text)
        segment = text[match.start():end]
        space = re.search(r"\s", segment)
        if space:
            segment = segment[:space.start()]
        segment = segment.strip().strip(",;；，、")
        if segment and not segment.lower().startswith(("http://", "https://")):
            links.append(segment)
        cursor = match.end()
    # v1.9.74：Telegram 专属代理链接是 https:// 形态，上面的协议头切分抓不到，单独补抓
    links.extend(pms_telegram_extract_tg_proxies(text))
    return pms_dedupe_keep_order(links)

def pms_telegram_extract_subscriptions(pms_html: str) -> list[str]:
    """从页面 `<a href>` 里提取可能的 http(s) 订阅地址（排除 Telegram 自身与应用商店）。"""
    source = str(pms_html or "")
    if not source:
        return []
    found: list[str] = []
    for raw in re.findall(r'<a[^>]+href="([^"]+)"', source):
        url = html.unescape(str(raw or "")).strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        try:
            host = str(urlparse(url).hostname or "").lower()
        except ValueError:
            continue
        if host and host not in PMS_TELEGRAM_SUB_SKIP_HOSTS:
            found.append(url)
    return pms_dedupe_keep_order(found)

# --------------------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------------------
def pms_telegram_fetch_channel_detailed(
    pms_channel: str,
    pms_opener: Any,
    pms_pages: int = 1,
    pms_timeout: float = PMS_TELEGRAM_DEFAULT_TIMEOUT,
    pms_user_agent: str = PMS_DEFAULT_SUB_USER_AGENT,
    pms_logger: Optional[logging.Logger] = None,
    pms_after_id: int = 0,
    pms_before_id: int = 0,
    pms_page_delay: float = 0.0,
    pms_delay_jitter: float = PMS_TELEGRAM_BACKFILL_JITTER,
    pms_abort_event: Optional[Any] = None,
    pms_text_sink: Optional[list[str]] = None,
    pms_route: str = "",
) -> tuple[str, list[str], dict[str, int]]:
    """抓取频道若干页，返回 (节点链接文本 —— 每行一条, 频道内的订阅地址列表, 统计)。

    比 `pms_telegram_fetch_channel()` 多返回订阅地址列表，供「抓完还想继续抓」的链路使用；
    统计含 pages / messages / new_messages / links / subscriptions / max_id / min_id / skipped。
    任何一页失败即停止并返回已抓到的部分，绝不向外抛异常

    **v1.9.72 日志改进**：`pms_route` 传当前尝试的通路名（本程序代理 / 外部代理 / 直连），
    失败时日志会带上通路名并说明「将自动尝试下一条通路」——单条通路失败大多为良性退让
    （例如 auto 模式下本程序核心没在跑、排首位的「本程序代理」无人监听），只要后面有通路成功
    频道照样更新成功；真正的失败由调用方的「全部通路都失败」报错体现，二者不再混为一谈。
    """
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".telegram")
    channel = str(pms_channel or "").strip()
    if not channel:
        return "", [], {"pages": 0, "messages": 0, "links": 0, "subscriptions": 0}
    raw_pages = int(pms_pages or 1)
    if raw_pages == PMS_TELEGRAM_PAGES_TO_BEGINNING:
        # v1.9.5「翻到底」：靠空页 / 抓取失败自然停止，硬保险只是防止异常时无限循环
        pages = PMS_TELEGRAM_ABSOLUTE_MAX_PAGES
    else:
        pages = max(1, min(raw_pages, PMS_TELEGRAM_MAX_PAGES))
    after_id = max(0, int(pms_after_id or 0))
    timeout = max(3.0, float(pms_timeout or PMS_TELEGRAM_DEFAULT_TIMEOUT))
    headers = {
        "User-Agent": str(pms_user_agent or PMS_DEFAULT_SUB_USER_AGENT),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }
    links: list[str] = []
    subscriptions: list[str] = []
    stats = {"pages": 0, "messages": 0, "new_messages": 0, "links": 0, "subscriptions": 0, "max_id": 0, "min_id": 0, "skipped": 0}
    # v1.9.6：起点游标（>0 时第一页直接 ?before=<游标>，用于接着往旧翻）
    before = str(max(0, int(pms_before_id or 0))) if int(pms_before_id or 0) > 0 else ""
    start_before = before
    # 页间停顿：只在「取下一页之前」睡，最后一页之后不浪费时间
    page_delay = min(PMS_TELEGRAM_BACKFILL_MAX_DELAY, max(0.0, float(pms_page_delay or 0.0)))
    jitter = min(0.9, max(0.0, float(pms_delay_jitter if pms_delay_jitter is not None else PMS_TELEGRAM_BACKFILL_JITTER)))
    for index in range(pages):
        if pms_abort_event is not None and pms_abort_event.is_set():   # v1.9.9
            logger.info("电报频道 %s 抓取被打断（已抓 %d 页，程序正在退出）", channel, stats["pages"])
            break
        if index > 0 and page_delay > 0.0:
            # 页间停顿也要可被中断：拆成小段睡，别一睡到底才去问标志位
            if pms_abort_event is None:
                time.sleep(page_delay * random.uniform(1.0 - jitter, 1.0 + jitter))
            else:
                remaining = page_delay * random.uniform(1.0 - jitter, 1.0 + jitter)
                while remaining > 0.0:
                    if pms_abort_event.is_set():
                        break
                    time.sleep(min(0.05, remaining))
                    remaining -= 0.05
                if pms_abort_event.is_set():
                    logger.info("电报频道 %s 抓取在页间停顿中被打断（已抓 %d 页）", channel, stats["pages"])
                    break
        url = pms_telegram_preview_url(channel) + (f"?before={before}" if before else "")
        try:
            with pms_opener.open(Request(url, headers=headers), timeout=timeout) as response:
                payload = response.read(8 * 1024 * 1024)
        except Exception as exc:  # 超时 / 代理失败 / 连接重置 —— 停止翻页，返回已有结果
            # v1.9.72：带上通路名，并说明「将自动尝试下一条通路」——
            # 单条通路失败通常是良性退让（如 auto 模式本程序核心未运行），并非抓取失败。
            route_name = str(pms_route or "未知通路")
            logger.warning("电报频道 %s 经「%s」抓取失败（%s）：%s —— 将自动尝试下一条通路", channel, route_name, url, exc)
            break
        source = payload.decode("utf-8", errors="replace")
        messages = pms_telegram_parse_messages(source)
        if not messages:
            break
        fresh_texts: list[str] = []
        ids: list[int] = []
        for post, text in messages:
            tail = str(post or "").rsplit("/", 1)[-1]
            msg_id = int(tail) if tail.isdigit() else 0
            if msg_id:
                ids.append(msg_id)
            if msg_id > after_id:
                fresh_texts.append(text)
            else:
                stats["skipped"] += 1
        stats["pages"] += 1
        stats["messages"] += len(messages)
        stats["new_messages"] += len(fresh_texts)
        if ids:
            stats["max_id"] = max(int(stats["max_id"]), max(ids))
            # v1.9.6：本轮翻到的最旧消息号 —— 慢速回填拿它当下一轮的起点
            lowest = min(ids)
            stats["min_id"] = lowest if not stats["min_id"] else min(int(stats["min_id"]), lowest)
        links.extend(pms_telegram_extract_links("\n".join(fresh_texts)))
        links.extend(pms_telegram_extract_tg_proxies(source))
        if pms_text_sink is not None:
            pms_text_sink.extend(fresh_texts)   # v1.9.44：桥接频道要的是正文，不是链接
        subscriptions.extend(pms_telegram_extract_subscriptions(source))
        # 增量模式：这一页全是旧贴，说明已越过上次边界，再往前翻没有意义
        if after_id > 0 and not fresh_texts:
            break
        if not ids:
            break
        # v1.9.6：页面没往旧推进（异常页面一直返回同一批）时立刻停，别把整轮页数白白跑完
        next_before = str(min(ids))
        if before and int(next_before) >= int(before):
            break
        before = next_before
    links = pms_dedupe_keep_order(links)
    subscriptions = pms_dedupe_keep_order(subscriptions)
    stats["links"] = len(links)
    stats["subscriptions"] = len(subscriptions)
    stats["to_beginning"] = 1 if raw_pages == PMS_TELEGRAM_PAGES_TO_BEGINNING else 0
    stats["exhausted"] = 1 if stats["pages"] < pages else 0  # 提前停 = 已翻到频道开头
    stats["start_before"] = int(pms_before_id or 0) if start_before else 0
    stats["delay"] = page_delay
    logger.info("电报频道 %s：%d 页%s / %d 条消息（新 %d，跳过旧 %d）/ %d 条节点链接 / %d 个订阅地址 / 最大消息号 %d",
                channel, stats["pages"], "（翻到底，已到频道开头）" if stats["exhausted"] and stats["to_beginning"] else "",
                stats["messages"], stats["new_messages"], stats["skipped"],
                stats["links"], stats["subscriptions"], stats["max_id"])
    return "\n".join(links), subscriptions, stats


def pms_telegram_fetch_channel(
    pms_channel: str,
    pms_opener: Any,
    pms_pages: int = 1,
    pms_timeout: float = PMS_TELEGRAM_DEFAULT_TIMEOUT,
    pms_user_agent: str = PMS_DEFAULT_SUB_USER_AGENT,
    pms_logger: Optional[logging.Logger] = None,
    pms_after_id: int = 0,
    pms_before_id: int = 0,
    pms_page_delay: float = 0.0,
    pms_delay_jitter: float = PMS_TELEGRAM_BACKFILL_JITTER,
    pms_abort_event: Optional[Any] = None,
    pms_text_sink: Optional[list[str]] = None,
    pms_route: str = "",
) -> tuple[str, dict[str, int]]:
    """订阅链路在用的旧签名：只返回 (节点链接文本, 统计)，订阅地址只要个数。"""
    text, _subscriptions, stats = pms_telegram_fetch_channel_detailed(
        pms_channel, pms_opener, pms_pages, pms_timeout, pms_user_agent, pms_logger,
        pms_after_id, pms_before_id, pms_page_delay, pms_delay_jitter, pms_abort_event, pms_text_sink,
        pms_route,
    )
    return text, stats
