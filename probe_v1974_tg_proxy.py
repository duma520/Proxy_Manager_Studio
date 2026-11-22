"""v1.9.74 离线探针：电报频道抽取 tg 专属代理链接（无 GUI、无网络）。

验证：
1. pms_telegram_extract_links 在节点链接混合文本里补抓到 tg 链接。
2. pms_telegram_extract_tg_proxies 单独抽取正确（含 &amp; 还原、去结尾标点）。
3. 模拟 fetch 翻页：source HTML 含 t.me/proxy <a href> 也能抽到。
"""
import html
import sys

from pms.telegram import (
    pms_telegram_extract_links,
    pms_telegram_extract_tg_proxies,
)

ok = 0
fail = 0

def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"[OK]   {name}")
    else:
        fail += 1
        print(f"[FAIL] {name}")

# 1) 混合文本：一段 vmess + 一段 t.me/proxy（电报频道常见的「节点 + 专属代理」混发）
mixed = (
    "vmess://eyJ2IjoiMiIsInBzIjoidGVzdCIsImFkZCI6IjEuMi4zLjQifQ==\n"
    "https://t.me/proxy?server=5.6.7.8&port=443&secret=ee0102030405060708090a0b0c0d0e0f "
    "这是电报专属代理，欢迎使用。\n"
    "vless://uuid@9.9.9.9:443?type=tcp#vless-test"
)
links = pms_telegram_extract_links(mixed)
print("extract_links ->", links)
check("extract_links 含 vmess", any(l.startswith("vmess://") for l in links))
check("extract_links 含 vless", any(l.startswith("vless://") for l in links))
check("extract_links 含 tg proxy", any(l.startswith("https://t.me/proxy") for l in links))
check("extract_links 不含 t.me 被整段丢", "https://t.me/proxy?server=5.6.7.8&port=443&secret=ee0102030405060708090a0b0c0d0e0f" in links)

# 2) 独立抽取：含 HTML 实体 &amp; 与结尾标点
raw_html = 'https://t.me/webproxy?server=1.1.1.1&amp;port=1080&amp;secret=abcdef0123456789.'
got = pms_telegram_extract_tg_proxies(raw_html)
print("extract_tg_proxies ->", got)
check("extract_tg_proxies 还原 &amp;", "https://t.me/webproxy?server=1.1.1.1&port=1080&secret=abcdef0123456789" in got)
check("extract_tg_proxies 去结尾标点", not any(g.endswith(".") for g in got))

# 3) 非 tg 的普通 https 不应被当成 tg 代理
noise = "https://example.com/subscribe?token=abc 正常订阅地址 https://t.me/somechannel 普通频道"
got2 = pms_telegram_extract_tg_proxies(noise)
check("extract_tg_proxies 不抓普通订阅", "https://example.com/subscribe?token=abc" not in got2)
check("extract_tg_proxies 不抓普通频道", "https://t.me/somechannel" not in got2)

# 4) 大小写 / 多主机
variants = "HTTPS://TELEGRAM.ME/proxy?server=a&port=1&secret=ff https://tg.dev/webproxy?server=b&port=2&secret=00"
got3 = pms_telegram_extract_tg_proxies(variants)
check("extract_tg_proxies 大小写主机", "https://telegram.me/proxy?server=a&port=1&secret=ff" in got3 or "HTTPS://TELEGRAM.ME/proxy?server=a&port=1&secret=ff" in got3)
check("extract_tg_proxies tg.dev", any("tg.dev/webproxy" in g for g in got3))

# 5) 模拟 fetch 翻页 source HTML（<a href="https://t.me/proxy?..."> 形态）也能抽到
sample_source = (
    '<div class="tgme_widget_message_text"><br>'
    '推荐代理 <a href="https://t.me/proxy?server=2.2.2.2&amp;port=443&amp;secret=00112233445566778899aabbccddeeff">点此</a><br>'
    'vmess://abc</div>'
)
# 翻页循环里对 source 扫 tg 链接，等价于 links.extend(pms_telegram_extract_tg_proxies(source))
page_links = pms_telegram_extract_links(html.unescape(sample_source))
page_links += pms_telegram_extract_tg_proxies(sample_source)
check("fetch 翻页 source 抽到 tg proxy", any(l.startswith("https://t.me/proxy") for l in page_links))

print(f"\n结果：{ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
