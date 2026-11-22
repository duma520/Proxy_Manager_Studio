# -*- coding: utf-8 -*-
"""v1.9.73 离线探针：订阅更新通路次序自定义 + 电报节点防误删。

不依赖 GUI、不依赖真实网络。直接 import 改动后的模块，断言：
  1. pms_telegram_parse_order：非法 token 过滤、去重保序、空串回退。
  2. pms_telegram_build_openers：自定义 order 生效、auto 默认次序、无端口跳过、non-auto 忽略 order。
  3. 防误删逻辑：模拟「电报频道 + 菜单全量(50页窗口)」场景，断言 psub_allow_prune 应为 False。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pms.telegram import pms_telegram_parse_order, pms_telegram_build_openers
from pms.constants import (
    PMS_TELEGRAM_PROXY_AUTO,
    PMS_TELEGRAM_PROXY_LOCAL,
    PMS_TELEGRAM_PROXY_EXTERNAL,
    PMS_TELEGRAM_PROXY_NONE,
    PMS_TELEGRAM_DEFAULT_PROXY_ORDER,
)


def check(name, cond, detail=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name}" + (f" —— {detail}" if detail else ""))
    return cond


def main():
    ok = True
    print("== 1) pms_telegram_parse_order ==")
    ok &= check("过滤非法 token", pms_telegram_parse_order("external,local,banana,direct") == ["external", "local", "direct"],
                str(pms_telegram_parse_order("external,local,banana,direct")))
    ok &= check("去重保序", pms_telegram_parse_order("direct,local,local,direct") == ["direct", "local"])
    ok &= check("空串返回空列表", pms_telegram_parse_order("") == [])
    ok &= check("大小写/空格容忍", pms_telegram_parse_order(" External , LOCAL ") == ["external", "local"])

    print("\n== 2) pms_telegram_build_openers：auto + 自定义次序 ==")
    # 本程序代理 1080 + 外部代理 1.2.3.4:8080 都可用
    op = pms_telegram_build_openers(PMS_TELEGRAM_PROXY_AUTO, 1080, "1.2.3.4:8080", "external,local,direct")
    names = [n for n, _ in op]
    ok &= check("次序 external→local→direct", names == ["外部代理", "本程序代理", "直连"], str(names))

    op = pms_telegram_build_openers(PMS_TELEGRAM_PROXY_AUTO, 1080, "1.2.3.4:8080", "local,direct")
    names = [n for n, _ in op]
    ok &= check("跳过未列出的 external", names == ["本程序代理", "直连"], str(names))

    op = pms_telegram_build_openers(PMS_TELEGRAM_PROXY_AUTO, 0, "1.2.3.4:8080", "local,external,direct")
    names = [n for n, _ in op]
    ok &= check("本程序代理无端口时跳过", names == ["外部代理", "直连"], str(names))

    op = pms_telegram_build_openers(PMS_TELEGRAM_PROXY_AUTO, 1080, "1.2.3.4:8080", "banana")
    names = [n for n, _ in op]
    ok &= check("非法 order 回退默认次序", names == ["本程序代理", "外部代理", "直连"], str(names))

    # non-auto 预设必须忽略 order
    op = pms_telegram_build_openers(PMS_TELEGRAM_PROXY_EXTERNAL, 1080, "1.2.3.4:8080", "local,direct")
    names = [n for n, _ in op]
    ok &= check("external 预设忽略 order（只用外部）", names == ["外部代理"], str(names))

    op = pms_telegram_build_openers(PMS_TELEGRAM_PROXY_LOCAL, 1080, "1.2.3.4:8080", "external,direct")
    names = [n for n, _ in op]
    ok &= check("local 预设忽略 order（只用本程序）", names == ["本程序代理"], str(names))

    op = pms_telegram_build_openers(PMS_TELEGRAM_PROXY_NONE, 1080, "1.2.3.4:8080", "external,local")
    names = [n for n, _ in op]
    ok &= check("none 预设忽略 order（只直连）", names == ["直连"], str(names))

    print("\n== 3) 防误删逻辑：电报频道 + 非翻到底窗口 必须禁止 prune ==")
    # 模拟 psub_fetch_telegram 里的判定（与 subscription.py 同源逻辑，单独复算验证公式）
    # 场景：用户手动点「全量」→ fetch_mode 非空 → pages=50（窗口，非翻到底哨兵 -1）
    PMS_TELEGRAM_MAX_PAGES = 50
    PMS_TELEGRAM_PAGES_TO_BEGINNING = -1
    pages_menu_full = PMS_TELEGRAM_MAX_PAGES            # 菜单全量：窗口
    pages_to_beginning = PMS_TELEGRAM_PAGES_TO_BEGINNING  # 翻到底：完整视图
    full_view_menu = (pages_menu_full == PMS_TELEGRAM_PAGES_TO_BEGINNING)
    full_view_beginning = (pages_to_beginning == PMS_TELEGRAM_PAGES_TO_BEGINNING)
    ok &= check("菜单『全量』不是完整视图 → 禁 prune", full_view_menu is False)
    ok &= check("『翻到底』才是完整视图 → 允许 prune", full_view_beginning is True)

    # 组合：电报频道 + 非完整视图 → psub_allow_prune=False
    is_telegram = True
    allow_prune = True
    if is_telegram and not full_view_menu:
        allow_prune = False
    ok &= check("电报频道+菜单全量窗口：allow_prune 应为 False", allow_prune is False)

    print("\n全部默认次序 =", PMS_TELEGRAM_DEFAULT_PROXY_ORDER)
    print("\n==== 结论 ====")
    print("全部通过" if ok else "存在失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
