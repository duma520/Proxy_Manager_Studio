#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.9.75 离线探针：验证订阅更新「6 分类」筛选逻辑（无 GUI、无网络）。

验证点：
1. pms_subscription_matches_filter 对 url/github/t.me/tor_bridge 地址的归类。
   - tor_bridge 地址本身是 t.me/...，必须归 t.me 桶（符合用户「t.me」字面）。
2. 未知 filter 值退化为全部通过（不漏更）。
3. 6 个中文标题齐全。
"""

from pms.subkind import (
    PMS_SUBSCRIPTION_FILTER_ALL,
    PMS_SUBSCRIPTION_FILTER_NON_TG,
    PMS_SUBSCRIPTION_FILTER_TG,
    PMS_SUBSCRIPTION_UPDATE_TITLES,
    pms_subscription_matches_filter,
)

URLS = {
    "普通订阅": "https://example.com/subscription.txt",
    "GitHub": "https://github.com/owner/repo",
    "t.me频道": "https://t.me/my_channel",
    "t.me桥接": "https://t.me/s/tor_bridges",  # 内容判定为 tor_bridge，地址仍是 t.me
}

CASES = []


def check(name, cond):
    CASES.append((name, bool(cond)))
    print(("[OK] " if cond else "[FAIL] ") + name)


# 1. all 桶：全部通过
for label, url in URLS.items():
    check(f"all 桶应含 {label}", pms_subscription_matches_filter(url, PMS_SUBSCRIPTION_FILTER_ALL))

# 2. non_tg 桶：仅非 t.me（普通订阅 / GitHub），不含 t.me / 桥接
check("non_tg 含 普通订阅", pms_subscription_matches_filter(URLS["普通订阅"], PMS_SUBSCRIPTION_FILTER_NON_TG))
check("non_tg 含 GitHub", pms_subscription_matches_filter(URLS["GitHub"], PMS_SUBSCRIPTION_FILTER_NON_TG))
check("non_tg 不含 t.me频道", not pms_subscription_matches_filter(URLS["t.me频道"], PMS_SUBSCRIPTION_FILTER_NON_TG))
check("non_tg 不含 t.me桥接", not pms_subscription_matches_filter(URLS["t.me桥接"], PMS_SUBSCRIPTION_FILTER_NON_TG))

# 3. tg 桶：仅 t.me（含频道与桥接），不含普通订阅 / GitHub
check("tg 不含 普通订阅", not pms_subscription_matches_filter(URLS["普通订阅"], PMS_SUBSCRIPTION_FILTER_TG))
check("tg 不含 GitHub", not pms_subscription_matches_filter(URLS["GitHub"], PMS_SUBSCRIPTION_FILTER_TG))
check("tg 含 t.me频道", pms_subscription_matches_filter(URLS["t.me频道"], PMS_SUBSCRIPTION_FILTER_TG))
check("tg 含 t.me桥接（地址是 t.me，符合字面）", pms_subscription_matches_filter(URLS["t.me桥接"], PMS_SUBSCRIPTION_FILTER_TG))

# 4. 空 / 未知 filter 退化为 all
check("空 filter 退化为全部", pms_subscription_matches_filter(URLS["普通订阅"], ""))
check("未知 filter 退化为全部", pms_subscription_matches_filter(URLS["GitHub"], "weird"))

# 5. 6 个标题齐全
for key in ("all_full", "all_incremental", "non_tg_full", "non_tg_incremental", "tg_full", "tg_incremental"):
    check(f"标题存在：{key}", key in PMS_SUBSCRIPTION_UPDATE_TITLES and bool(PMS_SUBSCRIPTION_UPDATE_TITLES[key]))

failed = [name for name, ok in CASES if not ok]
if failed:
    print(f"\n失败 {len(failed)} 项：{failed}")
    raise SystemExit(1)
print("\n全部通过（{} 项）".format(len(CASES)))
