#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅源类型判定（只此一处判定，界面标签与抓取分流都读这里）。

**为什么单独成模块**：`pms/subscription.py` 是顶在 1000 行红线的文件（探针 `probe_v1918`
盯着 ≤1000），而「地址/内容属于哪种订阅源」是一段纯判定逻辑，跟「抓取 + 落库」不绑在一起；
按铁律 7.58 把它搬出来，`pms/subscription.py` 再导出一遍，外部引用一行都不用改。

四种类型：

    github     —— https://github.com/owner/repo 或 owner/repo（API 自动发现节点文件）
    telegram   —— https://t.me/<频道名>（抓 /s/ 预览页）
    tor_bridge —— **内容判定**：电报/普通订阅抓下来发现里面是 Tor 桥接行而没有节点链接
                  （v1.9.44，例如 https://t.me/s/tor_bridges；地址上看不出区别，
                  所以靠订阅行上的 `last_bridge_count` / `last_bridge_note` 记住结论）
    url        —— 普通订阅地址（直接 GET 正文）
"""

from __future__ import annotations

from typing import Any

from pms.utils import pms_safe_int
from pms.telegram import pms_telegram_channel_from_url
from pms.githubsource import pms_github_repo_from_url

PMS_SUBSCRIPTION_KIND_URL = "url"
PMS_SUBSCRIPTION_KIND_TELEGRAM = "telegram"
PMS_SUBSCRIPTION_KIND_GITHUB = "github"
PMS_SUBSCRIPTION_KIND_TOR_BRIDGE = "tor_bridge"
PMS_SUBSCRIPTION_KIND_TOR_BRIDGE_TITLE = "Tor 桥接"
PMS_SUBSCRIPTION_KIND_TITLES: dict[str, str] = {
    PMS_SUBSCRIPTION_KIND_URL: "普通订阅",
    PMS_SUBSCRIPTION_KIND_TELEGRAM: "电报频道",
    PMS_SUBSCRIPTION_KIND_GITHUB: "GitHub 仓库",
    PMS_SUBSCRIPTION_KIND_TOR_BRIDGE: PMS_SUBSCRIPTION_KIND_TOR_BRIDGE_TITLE,
}

# v1.9.75：订阅更新「6 分类」筛选桶（不分格式 / 非 t.me / t.me）。
# 注意：桶按「地址是否 t.me」划分，不是按 pms_subscription_kind 返回值——
# tor_bridge 的地址本身就是 t.me/...（内容判定，subkind.py 注释已说明），归 t.me 桶。
PMS_SUBSCRIPTION_FILTER_ALL = "all"
PMS_SUBSCRIPTION_FILTER_NON_TG = "non_tg"
PMS_SUBSCRIPTION_FILTER_TG = "tg"
PMS_SUBSCRIPTION_FILTER_TITLES: dict[str, str] = {
    PMS_SUBSCRIPTION_FILTER_ALL: "不分格式",
    PMS_SUBSCRIPTION_FILTER_NON_TG: "非 t.me",
    PMS_SUBSCRIPTION_FILTER_TG: "t.me",
}
# 6 个更新入口的中文标题（顺序：全部 → 非 t.me → t.me，每个含全量/增量）。
PMS_SUBSCRIPTION_UPDATE_TITLES: dict[str, str] = {
    "all_full": "全量更新·全部",
    "all_incremental": "增量更新·全部",
    "non_tg_full": "全量更新·非 t.me",
    "non_tg_incremental": "增量更新·非 t.me",
    "tg_full": "全量更新·t.me",
    "tg_incremental": "增量更新·t.me",
}


def pms_subscription_kind(pms_url: str) -> str:
    """按**地址**判定订阅源类型（github / telegram / url）。"""
    text = str(pms_url or "").strip()
    if pms_telegram_channel_from_url(text):
        return PMS_SUBSCRIPTION_KIND_TELEGRAM
    if pms_github_repo_from_url(text)[0]:
        return PMS_SUBSCRIPTION_KIND_GITHUB
    return PMS_SUBSCRIPTION_KIND_URL


def pms_subscription_matches_filter(pms_url: str, pms_filter: str) -> bool:
    """判断某订阅地址是否落在指定更新筛选桶内。

    - `all`     —— 全部类型都过。
    - `non_tg`  —— 地址不是 t.me（普通订阅 / GitHub / 任何非电报地址）。
    - `tg`      —— 地址是 t.me（含电报频道与 Tor 桥接，因为二者地址都是 t.me/...）。

    按地址判而不按 pms_subscription_kind 返回判：tor_bridge 经内容判定归入
    PMS_SUBSCRIPTION_KIND_TOR_BRIDGE，但地址仍是 t.me，按用户「t.me」字面应进 t.me 桶。
    """
    pms_filter = str(pms_filter or "").strip().lower()
    if pms_filter in ("", PMS_SUBSCRIPTION_FILTER_ALL):
        return True
    is_tg = bool(pms_telegram_channel_from_url(str(pms_url or "").strip()))
    if pms_filter == PMS_SUBSCRIPTION_FILTER_TG:
        return is_tg
    if pms_filter == PMS_SUBSCRIPTION_FILTER_NON_TG:
        return not is_tg
    # 未知筛选值退化为全部通过，避免漏更。
    return True


def pms_subscription_kind_title(pms_url: str) -> str:
    """订阅源类型的中文名（界面表格「类型」列）。"""
    return PMS_SUBSCRIPTION_KIND_TITLES.get(pms_subscription_kind(pms_url), pms_subscription_kind(pms_url))


def pms_subscription_record_kind_title(pms_record: Any) -> str:
    """订阅行的类型显示：**抓到过桥接的显示「Tor 桥接」**，其余按地址判类型。

    为什么不按地址判：`t.me/tor_bridges` 与普通频道地址长得一模一样，只有内容能说明问题；
    所以「第一次更新之后」类型列会从「电报频道」变成「Tor 桥接」——这也是用户要的可见反馈。
    """
    if pms_safe_int(getattr(pms_record, "last_bridge_count", 0), 0) > 0 or str(
        getattr(pms_record, "last_bridge_note", "") or ""
    ).strip():
        return PMS_SUBSCRIPTION_KIND_TOR_BRIDGE_TITLE
    return pms_subscription_kind_title(str(getattr(pms_record, "url", "") or ""))
