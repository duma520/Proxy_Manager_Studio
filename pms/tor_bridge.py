#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tor 桥接出口（sing-box 的 `tor` 出站）：桥接行解析 + torrc/出站生成 + 二进制路径约定。

**只支持 sing-box 核心**：Xray 没有 tor 出站，检测到 Xray 时启动前会明确拦截并提示
（文案见 `ptb_core_kind_error()`）。

支持的桥接行格式（`ptb_parse_bridge_line()` 的解析口径）：::

    obfs4      obfs4 <ip:port> <40 位指纹> cert=<base64> iat-mode=<0|1|2>
    webtunnel  webtunnel <[ipv6]:port> <指纹> url=<https 地址> [ver=<版本>]
    meek       meek_lite <ip:port> <指纹> url=<https 地址> front=<域名>
    snowflake  snowflake <ip:port> <指纹> [url=... front=...]

★ **真核心实测**（sing-box 1.14.3，`sing-box check` 逐个变体跑过，不猜）：

* `torrc` 是「字符串 → 字符串」的表，`Bridge` / `ClientTransportPlugin` **不能写数组**——
  写成数组直接报 `cannot unmarshal array into Go struct field TorOutboundOptions.torrc`；
* 所以**多个桥接必须走 `extra_args`**（`--Bridge` 重复出现），单桥接才写进 `torrc`；
* 字段名写错会被 `unknown field` 拒掉：字段名以本模块为准，别手改；
* `data_directory` 用**持久目录** `data/tor`（Tor 要把 consensus / 指纹缓存在这里，
  放临时目录会导致每次启动重新拉 consensus、桥接成功率明显下降）。

Expert Bundle 里带了 `tor.exe`、`lyrebird.exe`（同时支持 obfs4 与 WebTunnel），
**没有** `obfs4proxy.exe`——所以 obfs4 的插件行写 `obfs4 exec <lyrebird.exe>`。

★ v1.9.55 实查 Tor **15.0.25** 的 Expert Bundle（`tarfile` 逐个条目列过，19 个条目）：
`tor/tor.exe`、`tor/tor-gencert.exe`、`tor/pluggable_transports/lyrebird.exe`、
`tor/pluggable_transports/conjure-client.exe` + 文档与 geoip。
**`snowflake-client.exe` 已经不在包里了**（上游用 conjure 取代了它），
`meek-client.exe` 也一直不在。因此这两个插件按「可选」处理，
只有 `tor.exe` 与 `lyrebird.exe` 是必需，详见 `PMS_TOR_BUNDLE_REQUIRED`。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional, Sequence

# constants 是叶子模块（只定义常量、不 import 任何 pms 模块），不会形成循环
from pms.constants import PMS_TOR_MAX_BRIDGES_IN_CONFIG  # noqa: E402

PMS_TOR_TAG = "tor-bridged"                     # sing-box 出站 tag（路由 final 会指向它）
PMS_TOR_DIRNAME = "tor"
PMS_TOR_DATA_DIRNAME = "data"
PMS_TOR_EXE_NAME = "tor.exe"
PMS_TOR_LYREBIRD_NAME = "lyrebird.exe"
PMS_TOR_SNOWFLAKE_NAME = "snowflake-client.exe"
PMS_TOR_MEEK_NAME = "meek-client.exe"
PMS_TOR_CONJURE_NAME = "conjure-client.exe"

# Expert Bundle / Tor 浏览器包里**真正要用的文件**（v1.9.46 实查：包内只有这几个 + 一堆文档/依赖）
#
# ★ v1.9.55 拆分「必需 / 可选」——因为 Tor 15.0.25 起 Expert Bundle **不再带
# `snowflake-client.exe`**（上游换成 `conjure-client.exe` 了，v1.9.55 实查 15.0.25
# 的 tar.gz：19 个条目里只有 tor.exe / tor-gencert.exe / lyrebird.exe / conjure-client.exe）。
# 老代码把三者都当必需，于是 15.0.25 下载完就弹
# 「压缩包里缺少组件：snowflake-client.exe（上游包结构可能变了）」，
# **连只用 obfs4 / webtunnel 的用户也被一起挡死**——那两类只要 lyrebird。
#
# 所以：
#   * 必需 = tor.exe + lyrebird.exe（obfs4 / webtunnel / meek 都靠它，绝大多数用户只用这两类）；
#   * 可选 = snowflake-client.exe / meek-client.exe / conjure-client.exe
#     （上游可能不带，缺了只影响用这几类的用户，
#     由 `ptb_binaries_state()` 按当前桥接类型单独判定，**不让它拦死整条下载链路**）。
#
# ★ v1.9.58 补 `conjure-client.exe`：v1.9.55 已经查明上游用 conjure 取代了 snowflake，
# 但它一直没被写进成员清单，于是 conjure 桥接类型**永远拿不到客户端**（属于真漏，不是设计）。
# v1.9.58 实测 Tor 浏览器便携版与 Expert Bundle 15.0.25 里 conjure-client.exe 都在（12832 KB），
# SHA-256 与 Expert Bundle 完全一致，见 `probe_v1960`。
PMS_TOR_BUNDLE_MEMBERS: tuple[str, ...] = (
    PMS_TOR_EXE_NAME,
    PMS_TOR_LYREBIRD_NAME,
    PMS_TOR_SNOWFLAKE_NAME,
    PMS_TOR_CONJURE_NAME,
)
# 缺了就必须算失败的核心组件（v1.9.55）
PMS_TOR_BUNDLE_REQUIRED: tuple[str, ...] = (
    PMS_TOR_EXE_NAME,
    PMS_TOR_LYREBIRD_NAME,
)
# 上游可能不带、缺了只提示不拦死的组件（v1.9.55；v1.9.58 补 conjure）
PMS_TOR_BUNDLE_OPTIONAL: tuple[str, ...] = (
    PMS_TOR_SNOWFLAKE_NAME,
    PMS_TOR_MEEK_NAME,
    PMS_TOR_CONJURE_NAME,
)

# 桥接类型（小写）→ 插件名；`meek_lite` 是 meek 的实际插件名（tor 里就叫这个）
# v1.9.47：加 `vanilla`（普通 Tor 桥接，`<IP:PORT> <40 位指纹>`，**不带类型前缀**，
# 采集站的 `vanilla_tested.txt` 就是这种形状 —— 实查见 probe_v1947）
# v1.9.58：加 `conjure` —— 上游 Tor 15.0.25 起用 `conjure-client.exe` 取代了
# `snowflake-client.exe`（v1.9.55 实查，v1.9.58 实测浏览器包里也在），
# 采集站的 `conjure_tested.txt` 也是同形状；不加这个类型，conjure 桥接行会被判成「认不出」。
PMS_TOR_BRIDGE_TYPES: tuple[str, ...] = ("obfs4", "webtunnel", "meek", "meek_lite", "snowflake", "conjure", "vanilla")
PMS_TOR_BRIDGE_TITLES: dict[str, str] = {
    "obfs4": "obfs4（lyrebird）",
    "webtunnel": "WebTunnel（lyrebird）",
    "meek": "meek / meek_lite",
    "meek_lite": "meek_lite",
    "snowflake": "snowflake",
    "conjure": "conjure（conjure-client）",
    "vanilla": "vanilla（普通桥接，无插件）",
}
# 每种桥接类型对应的「本地传输插件」：插件名 + 候选可执行文件（取第一个存在的）
# `vanilla` 不在表里 —— 它不需要任何插件（`ptb_plugin_lines()` 会跳过）
PMS_TOR_PLUGINS: dict[str, tuple[str, tuple[str, ...]]] = {
    "obfs4": ("obfs4", (PMS_TOR_LYREBIRD_NAME, "obfs4proxy.exe")),
    "webtunnel": ("webtunnel", (PMS_TOR_LYREBIRD_NAME, "obfs4proxy.exe")),
    "meek": ("meek_lite", (PMS_TOR_MEEK_NAME, PMS_TOR_LYREBIRD_NAME, "obfs4proxy.exe")),
    "meek_lite": ("meek_lite", (PMS_TOR_MEEK_NAME, PMS_TOR_LYREBIRD_NAME, "obfs4proxy.exe")),
    "snowflake": ("snowflake", (PMS_TOR_SNOWFLAKE_NAME,)),
    "conjure": ("conjure", (PMS_TOR_CONJURE_NAME,)),
}
_FINGERPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
_KV_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)=(.*)$")


def ptb_bridge_type(pms_line: str) -> str:
    """取桥接行的类型（小写）；认不出来返回空串。

    v1.9.47：`vanilla`（`<IP:PORT> <指纹>`，无类型前缀）也能认出来，所以这里直接问解析器，
    不再「只看第一个词」——否则 vanilla 行会被当成「没有类型」。
    """
    parsed = ptb_parse_bridge_line(pms_line)
    return str(parsed["type"]) if parsed else ""


def ptb_normalize_bridge_line(pms_line: str) -> str:
    """把一行「人手抄来 / 从频道贴来」的文本整理成 `类型 地址 指纹 …`。

    v1.9.44（Tor 桥接订阅）新增：真实频道贴出来的桥接行常带各种包装——
    `Bridge obfs4 …` 前缀、`- ` / `• ` / `1) ` 列表符号、反引号与引号、
    `obfs4 bridge: …` 这种「类型 + bridge 标签」写法、行尾 `#注释`。
    这些**都是同一座桥**，不该因为包装不同就被判成非法；整理只动包装，不动字段本身。
    """
    text = str(pms_line or "").strip()
    for _ in range(3):  # 最多剥三层包装（例如「- Bridge `obfs4 …`」）
        text = text.strip().strip("`").strip("\"'“”‘’").lstrip("-*•·‣▪> \t").strip()
        # 列表符号「1. / 2) 」后面必须跟空白：否则会把 `103.149.168.242:9443 …` 这种
        # **无类型前缀的 vanilla 桥接**开头的 `103.` 当成序号吃掉（v1.9.47 实查踩到过）。
        text = re.sub(r"^\d{1,3}[.)](?=\s)\s*", "", text)
        head = text.split(" ", 1)
        first = head[0].strip().rstrip(":").lower() if head else ""
        if first in ("bridge", "bridges") and len(head) > 1:
            text = head[1].lstrip(": \t").strip()
            continue
        # 「obfs4 bridge: <地址> …」：类型写在标签里，剥掉标签后要把类型补回最前面；
        # 若写法是「obfs4 bridge: obfs4 <地址> …」（类型写了两次），只留一份
        # v1.9.58：正则补 `conjure`（上游已用 conjure-client 取代 snowflake-client）
        found = re.match(r"^(obfs4|webtunnel|meek|meek_lite|snowflake|conjure)\s+bridge\s*:?\s*(.+)$",
                         text, re.IGNORECASE)
        if found:
            kind, rest = found.group(1).lower(), found.group(2).strip()
            text = rest if rest.split(" ", 1)[0].lower() in PMS_TOR_BRIDGE_TYPES else f"{kind} {rest}"
        break
    text = re.sub(r"\s+#.*$", "", text)  # 行尾注释（桥接行本身不含 '#'）
    return " ".join(text.split()).strip()


def ptb_parse_bridge_line(pms_line: str) -> Optional[dict[str, Any]]:
    """解析一行桥接配置；**认不出来返回 None**（调用方负责把原因报出来）。

    容错：空行、`#` 注释、首尾空白、多余空格、频道贴出来的各种包装一律先整理
    （见 `ptb_normalize_bridge_line`）；`key=value` 段全部收进 `extra`
    （cert / iat-mode / url / front / ver 也单独取一份方便界面展示）。

    **v1.9.47：`vanilla`（普通桥接）也认**——采集站的 `vanilla_tested.txt` 每行是
    `<IP:PORT> <40 位指纹>`，**没有类型前缀**，所以「首词是合法 IP:PORT + 次词是 40 位指纹」
    时按 vanilla 解析（`raw` 里补回 `vanilla ` 前缀，交给 torrc 时形状正确）。
    """
    text = ptb_normalize_bridge_line(pms_line)
    if not text or text.startswith("#"):
        return None
    parts = text.split()
    kind = parts[0].strip().lower()
    vanilla = False
    if kind not in PMS_TOR_BRIDGE_TYPES and len(parts) >= 2 and _looks_like_endpoint(parts[0]):
        kind, vanilla = "vanilla", True
    if kind not in PMS_TOR_BRIDGE_TYPES:
        return None
    if vanilla and len(parts) < 2:
        return None
    if len(parts) < 3 and not vanilla:
        return None
    address = parts[0].strip() if vanilla else parts[1].strip()
    fingerprint = parts[1].strip() if vanilla else parts[2].strip()
    if not address or ":" not in address:
        return None
    if not _FINGERPRINT_RE.match(fingerprint):
        return None
    extra: dict[str, str] = {}
    for item in parts[2 if vanilla else 3:]:
        found = _KV_RE.match(item)
        if found:
            extra[found.group(1).lower()] = found.group(2)
    tail = parts[2:] if vanilla else parts[3:]
    return {
        "type": kind,
        "address": address,
        "fingerprint": fingerprint,
        "cert": extra.get("cert", ""),
        "iat_mode": extra.get("iat-mode", extra.get("iat_mode", "")),
        "url": extra.get("url", ""),
        "front": extra.get("front", extra.get("fronts", "")),
        "extra": extra,
        # 指纹后面的一切（`cert=` / `iat-mode=` / `url=` / `ver=` …）**必须原样保留**：
        # tor 靠 `cert=` 才能握手，丢了它配置看着对、连不上。
        "raw": " ".join([kind, address, fingerprint, *tail]),
    }


def _looks_like_endpoint(pms_token: str) -> bool:
    """`IP:PORT` / `[IPv6]:PORT` / `host:PORT` 的粗判定（端口必须是数字且在 1..65535）。"""
    text = str(pms_token or "").strip()
    if ":" not in text:
        return False
    head, _, tail = text.rpartition(":")
    if not head or not tail.isdigit():
        return False
    port = int(tail)
    return 1 <= port <= 65535


def ptb_parse_bridge_lines(pms_lines: Sequence[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """解析多行桥接配置，返回 (解析成功的桥接, 出错说明)。

    出错说明写成「第 N 行：…」，且**不会静默丢弃**——界面上要能看出是哪一行有问题。
    """
    bridges: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, line in enumerate(pms_lines or [], 1):
        text = ptb_normalize_bridge_line(line)
        if not text or text.startswith("#"):
            continue
        parsed = ptb_parse_bridge_line(text)
        if parsed is None:
            errors.append(f"第 {index} 行不是合法的桥接配置：{text[:60]}")
            continue
        if parsed["raw"] in seen:
            continue
        seen.add(parsed["raw"])
        bridges.append(parsed)
    return bridges, errors


def ptb_extract_bridges_from_text(pms_text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """从**一堆自由文本**里挑出桥接行（订阅频道正文抓取用）。

    与 `ptb_parse_bridge_lines` 的区别是「报错口径」：
    频道正文里绝大多数是人话（公告、说明、链接），**不该逐行报错**；
    所以只有「整理后看着像桥接行（首词是已知类型）但解析不了」的行才进 `errors`，
    其余行安静跳过。返回值仍是 (桥接, 出错说明)。
    """
    bridges: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(str(pms_text or "").splitlines(), 1):
        text = ptb_normalize_bridge_line(raw)
        if not text or text.startswith("#"):
            continue
        kind = text.split(" ", 1)[0].lower()
        if kind not in PMS_TOR_BRIDGE_TYPES and not _looks_like_endpoint(kind):
            continue
        parsed = ptb_parse_bridge_line(text)
        if parsed is None:
            errors.append(f"第 {index} 行像桥接但解析不了：{text[:60]}")
            continue
        if parsed["raw"] in seen:
            continue
        seen.add(parsed["raw"])
        bridges.append(parsed)
    return bridges, errors


def ptb_bridge_identity(pms_bridge: dict[str, Any]) -> str:
    """桥接的「身份」：指纹优先（同一座桥换个 cert 也还是它），退化用整理后的整行。"""
    fingerprint = str(pms_bridge.get("fingerprint") or "").strip().lower()
    return fingerprint or str(pms_bridge.get("raw") or "").strip().lower()


def ptb_merge_bridge_lines(pms_existing: Sequence[str], pms_incoming: Sequence[str]) -> tuple[list[str], int]:
    """把新抓到的桥接合并进已有列表，返回 (合并后的原始行, 新增条数)。

    口径：**只增不删**（订阅是增量来源，删桥接是用户的事）+ 按指纹去重
    （同一座桥出现在多个频道 / 重复贴里只留第一条）。
    """
    merged = [ptb_normalize_bridge_line(line) for line in (pms_existing or [])]
    merged = [line for line in merged if line]
    known = {ptb_bridge_identity(item) for item in ptb_parse_bridge_lines(merged)[0]}
    added = 0
    for line in (pms_incoming or []):
        parsed = ptb_parse_bridge_line(line)
        if parsed is None:
            continue
        key = ptb_bridge_identity(parsed)
        if key in known:
            continue
        known.add(key)
        merged.append(str(parsed["raw"]))
        added += 1
    return merged, added


def ptb_apply_bridge_lines_to_settings(pms_settings: Any, pms_incoming: Sequence[str]) -> tuple[int, int, list[str]]:
    """把抓到的桥接合并写进 `tor.bridge_lines`（**只能在主线程调用**）。

    ★ 为什么强调主线程：`PmsSettingsManager` 是 QObject，工作线程里改它会触发
    `QObject::startTimer: Timers cannot be started from another thread`（v1.9.32 踩过），
    所以订阅任务只负责「解析 + 把行放进结果字典」，落盘交给主线程的完成回调。

    返回 (新增条数, 合并后总数, 出错说明)。
    """
    existing = [str(item) for item in pms_settings.psm_get_list("tor.bridge_lines")]
    merged, added = ptb_merge_bridge_lines(existing, pms_incoming)
    if added:
        pms_settings.psm_set_many({"tor.bridge_lines": merged}, pms_emit=True)
    errors = ptb_parse_bridge_lines(list(pms_incoming or []))[1]
    return added, len(merged), errors



def ptb_plugin_for(pms_bridge_type: str, pms_tor_dir: Path) -> tuple[str, Optional[Path]]:
    """该桥接类型对应的 (插件名, 插件可执行文件)。文件不存在时返回 (插件名, None)。"""
    plugin, candidates = PMS_TOR_PLUGINS.get(str(pms_bridge_type or "").lower(), ("", ()))
    if not plugin:
        return "", None
    for name in candidates:
        target = Path(pms_tor_dir) / name
        if target.is_file():
            return plugin, target
    return plugin, None


def ptb_resolve_paths(pms_paths: Any, pms_tor_dir: str = "", pms_data_dir: str = "") -> dict[str, Path]:
    """按「设置里的自定义目录优先，否则 <数据目录>/tor」解析全部路径。

    配置生成器没有设置对象，所以路径解析只有这一个入口，避免两处算法漂移。
    """
    base = Path(pms_tor_dir) if str(pms_tor_dir or "").strip() else Path(pms_paths.prt_data_dir) / PMS_TOR_DIRNAME
    data = Path(pms_data_dir) if str(pms_data_dir or "").strip() else base / PMS_TOR_DATA_DIRNAME
    return {
        "tor_dir": base,
        "tor_exe": base / PMS_TOR_EXE_NAME,
        "data_dir": data,
        "lyrebird": base / PMS_TOR_LYREBIRD_NAME,
        "snowflake": base / PMS_TOR_SNOWFLAKE_NAME,
        "meek": base / PMS_TOR_MEEK_NAME,
    }


def ptb_tor_dir(pms_paths: Any, pms_settings: Any = None) -> Path:
    """Tor 工作目录：设置里填了就用它，否则用 `<数据目录>/tor`（持久）。"""
    custom = ""
    if pms_settings is not None:
        custom = str(pms_settings.psm_get_str("tor.tor_dir", "") or "")
    return ptb_resolve_paths(pms_paths, custom)["tor_dir"]


def ptb_tor_paths(pms_paths: Any, pms_settings: Any = None) -> dict[str, Path]:
    """Tor 相关文件路径：可执行文件、持久数据目录、三个插件（从设置读自定义目录）。"""
    if pms_settings is None:
        return ptb_resolve_paths(pms_paths)
    return ptb_resolve_paths(
        pms_paths,
        str(pms_settings.psm_get_str("tor.tor_dir", "") or ""),
        str(pms_settings.psm_get_str("tor.data_directory", "") or ""),
    )


def ptb_binaries_state(pms_paths: Any, pms_settings: Any = None) -> dict[str, Any]:
    """二进制就绪情况（界面状态行 + 启动前检查都用它）。

    ★ **按当前桥接类型要插件**：obfs4/WebTunnel 要 lyrebird，snowflake 要 snowflake-client，
    不能一律要求 lyrebird —— 否则只用 snowflake 的用户会被误判成「缺文件」。
    """
    paths = ptb_tor_paths(pms_paths, pms_settings)
    bridge_type = PMS_TOR_BRIDGE_TYPES[0]
    if pms_settings is not None:
        bridge_type = str(pms_settings.psm_get_str("tor.bridge_type", bridge_type) or "").strip().lower()
        bridge_type = bridge_type if bridge_type in PMS_TOR_BRIDGE_TYPES else PMS_TOR_BRIDGE_TYPES[0]
    plugin, plugin_exe = ptb_plugin_for(bridge_type, paths["tor_dir"])
    candidates = PMS_TOR_PLUGINS.get(bridge_type, ("", ()))[1]
    present = {name: path.is_file() for name, path in paths.items()
               if name in ("tor_exe", "lyrebird", "snowflake", "meek")}
    missing: list[str] = []
    if not present.get("tor_exe", False):
        missing.append(PMS_TOR_EXE_NAME)
    if plugin_exe is None:
        missing.append((candidates[0] if candidates else plugin) or plugin)
    return {
        "paths": paths, "present": present, "missing": missing, "bridge_type": bridge_type,
        "plugin": plugin, "plugin_exe": plugin_exe, "tor_dir": paths["tor_dir"],
        "data_dir": paths["data_dir"], "ready": present.get("tor_exe", False) and plugin_exe is not None,
    }


def ptb_plugin_lines(pms_bridges: Sequence[dict[str, Any]], pms_tor_dir: Path) -> list[str]:
    """按桥接类型生成 `ClientTransportPlugin` 行（去重、按插件名排序，保证可复现）。"""
    lines: dict[str, str] = {}
    for bridge in pms_bridges or []:
        plugin, exe = ptb_plugin_for(str(bridge.get("type") or ""), pms_tor_dir)
        if not plugin:
            continue
        target = exe or (Path(pms_tor_dir) / PMS_TOR_LYREBIRD_NAME)
        lines[plugin] = f"{plugin} exec {target}"
    return [lines[key] for key in sorted(lines)]


def ptb_build_torrc(pms_bridges: Sequence[dict[str, Any]], pms_plugin_lines: Sequence[str]) -> dict[str, str]:
    """生成 `torrc`（字符串→字符串）。

    ★ 单桥接 + 单插件才写进这里（torrc 不能出现重复键）；多桥接/多插件走 `ptb_build_extra_args()`。
    """
    torrc: dict[str, str] = {"UseBridges": "1"}
    if len(pms_plugin_lines) == 1:
        torrc["ClientTransportPlugin"] = str(pms_plugin_lines[0])
    if len(pms_bridges or []) == 1:
        torrc["Bridge"] = str(pms_bridges[0]["raw"])
    return torrc


def ptb_build_extra_args(pms_bridges: Sequence[dict[str, Any]], pms_plugin_lines: Sequence[str]) -> list[str]:
    """重复键走命令行：`--Bridge <行>` 每条一次、`--ClientTransportPlugin <行>` 每种一次。

    ★ 为什么必须这样：`torrc` 是 map，重复键只能留一个（sing-box 也会直接报错），
    而 tor 的命令行选项**可以重复出现**（这正是「多桥接」唯一的合法出口）。
    """
    args: list[str] = []
    for line in pms_plugin_lines if len(pms_plugin_lines) > 1 else []:
        args += ["--ClientTransportPlugin", str(line)]
    for bridge in (pms_bridges if len(pms_bridges or []) > 1 else []):
        args += ["--Bridge", str(bridge["raw"])]
    return args


def ptb_build_tor_outbound(
    pms_bridges: Sequence[dict[str, Any]],
    pms_paths: Any,
    pms_tor_dir: str = "",
    pms_data_dir: str = "",
    pms_tag: str = PMS_TOR_TAG,
) -> dict[str, Any]:
    """生成 sing-box 的 `tor` 出站（字段名以真核心实测为准，见模块 docstring）。"""
    binaries = ptb_resolve_paths(pms_paths, pms_tor_dir, pms_data_dir)
    plugin_lines = ptb_plugin_lines(pms_bridges, binaries["tor_dir"])
    outbound: dict[str, Any] = {
        "type": "tor",
        "tag": str(pms_tag),
        "executable_path": str(binaries["tor_exe"]),
        "data_directory": str(binaries["data_dir"]),
        "torrc": ptb_build_torrc(pms_bridges, plugin_lines),
    }
    extra_args = ptb_build_extra_args(pms_bridges, plugin_lines)
    if extra_args:
        outbound["extra_args"] = extra_args
    return outbound


def ptb_apply_singbox_tor(pms_config: dict[str, Any], pms_payload: Any, pms_paths: Any) -> dict[str, Any]:
    """把 Tor 出站挂进 sing-box 配置并把 `route.final` 指过去（未启用时原样返回）。

    ★ 路径缺失**直接抛错**，不做静默降级——否则用户会以为走了 Tor 其实没走。
    放在这里而不是 `coreconfig.py`：`coreconfig.py` 已经贴着 1000 行红线。
    """
    payload = pms_payload if isinstance(pms_payload, dict) else {}
    if not payload.get("enabled"):
        return pms_config
    if pms_paths is None:
        raise ValueError("Tor 出口需要运行路径（缺少 paths），无法生成 tor 出站")
    outbounds = pms_config.get("outbounds")
    if isinstance(outbounds, list):
        outbounds.append(ptb_build_tor_outbound(
            payload.get("bridges") or [], pms_paths,
            str(payload.get("tor_dir") or ""), str(payload.get("data_directory") or ""),
        ))
    route = pms_config.get("route")
    if isinstance(route, dict):
        route["final"] = PMS_TOR_TAG
    return pms_config


def ptb_settings_payload(pms_settings: Any) -> dict[str, Any]:
    """把设置里的 Tor 项整理成「配置档载荷」（`PmsCoreLaunchProfile.tor`）。

    只做解析与整理，不碰磁盘——配置生成与启动前检查各自需要时再查文件。

    ★ v1.9.56：桥接条数**截到 `PMS_TOR_MAX_BRIDGES_IN_CONFIG`**。
    根因（真进程取证）：sing-box 把 torrc + extra_args 全部拼进 tor 的命令行，
    Windows CreateProcess 命令行上限 32767 字符；用户从采集站抓到 1615 条桥接
    （≈268KB）后核心必崩：`fork/exec tor.exe: the filename or extension is too long`，
    表现为「核心反复重启、退出码 1」。`IncludeConfig` 也救不了（sing-box 启动时
    会校验「UseBridges 必须至少一条 Bridge 在命令行上」，实跑验证过），
    所以唯一正确的口径是**限制条数**——tor 用前几条桥引导成功即可，
    1615 条对引导成功率毫无意义（实测 150 条 ≈ 25KB，留一半余量）。
    截断必须说出来（`notes`），不允许静默吞掉用户的桥接。
    """
    enabled = False
    bridge_type = PMS_TOR_BRIDGE_TYPES[0]
    tor_dir = ""
    data_dir = ""
    lines: list[str] = []
    if pms_settings is not None:
        enabled = bool(pms_settings.psm_get_bool("tor.enabled", False))
        bridge_type = str(pms_settings.psm_get_str("tor.bridge_type", PMS_TOR_BRIDGE_TYPES[0]) or "").lower()
        tor_dir = str(pms_settings.psm_get_str("tor.tor_dir", "") or "").strip()
        data_dir = str(pms_settings.psm_get_str("tor.data_directory", "") or "").strip()
        lines = [str(item) for item in pms_settings.psm_get_list("tor.bridge_lines")]
    notes: list[str] = []
    if len(lines) > PMS_TOR_MAX_BRIDGES_IN_CONFIG:
        notes.append(f"桥接共 {len(lines)} 条，超出命令行安全上限，只取前 "
                     f"{PMS_TOR_MAX_BRIDGES_IN_CONFIG} 条进配置"
                     f"（全部塞进配置会让 tor 命令行超限、核心起不来）")
        lines = lines[:PMS_TOR_MAX_BRIDGES_IN_CONFIG]
    bridges, errors = ptb_parse_bridge_lines(lines)
    return {"enabled": enabled, "bridge_type": bridge_type, "bridges": bridges,
            "errors": errors, "raw_lines": lines, "tor_dir": tor_dir, "data_directory": data_dir,
            "notes": notes}


def ptb_bridge_to_torrc(
    pms_bridges: Sequence[Any], pms_tor_dir: Any,
) -> tuple[dict[str, str], list[str], list[str]]:
    """桥接（行或已解析对象）→ `(torrc, extra_args, errors)` —— **Tor 抓取链路的统一出口**。

    v1.9.47 新增：`pms/torbridge.py`（采集站抓取 + 连通性验证）与界面都调这一个入口，
    免得「谁自己拼一份 torrc」导致口径分叉：

    * 单桥接 + 单插件 → 全写进 `torrc`（map 不能有重复键）；
    * 多桥接 / 多插件 → `Bridge` 与 `ClientTransportPlugin` 走 `extra_args`（`--Bridge` 重复出现）；
    * `vanilla` 没有插件，不产生 `ClientTransportPlugin`。

    `errors` 是「行里解析不了的那几条」，**不静默丢**（调用方负责展示）。
    """
    bridges: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in pms_bridges or []:
        if isinstance(item, dict):
            bridges.append(dict(item))
            continue
        text = str(item)
        if text.lstrip().startswith("#"):
            continue  # 注释行（例如界面标出的「# 未验活」）是**有意留的说明**，不算解析错误
        parsed = ptb_parse_bridge_line(text)
        if parsed is None:
            errors.append(f"不是合法的桥接行：{text[:60]}")
            continue
        bridges.append(parsed)
    plugin_lines = ptb_plugin_lines(bridges, Path(str(pms_tor_dir)))
    torrc = ptb_build_torrc(bridges, plugin_lines)
    return torrc, ptb_build_extra_args(bridges, plugin_lines), errors


# 未验活候选在界面 / 设置里的标记（v1.9.48）：以 `#` 开头 → `ptb_parse_bridge_lines()` 与
# `ptb_bridge_to_torrc()` 都当**注释**跳过，绝不会混进 torrc（用户要「保存未验活的桥接」，
# 但又不能让它悄悄进配置，这就是那条边界）。
PMS_TOR_UNVERIFIED_PREFIX = "# 未验活（可能不可用）"


def ptb_mark_unverified(pms_lines: Sequence[Any]) -> list[str]:
    """给未验活的桥接行加「未验活」注释前缀（原始行原样留在后面，用户想试就直接去掉前缀）。"""
    rows: list[str] = []
    for item in pms_lines or []:
        text = str(item).strip()
        if not text or text.startswith("#"):
            continue
        rows.append(f"{PMS_TOR_UNVERIFIED_PREFIX} {text}")
    return rows


def ptb_unverified_lines(pms_lines: Sequence[Any]) -> list[str]:
    """从一堆行里挑出带「未验活」标记的行，返回**去掉标记**的原始桥接行。"""
    rows: list[str] = []
    for item in pms_lines or []:
        text = str(item).strip()
        if text.startswith(PMS_TOR_UNVERIFIED_PREFIX):
            rows.append(text[len(PMS_TOR_UNVERIFIED_PREFIX):].strip())
    return rows


def ptb_merge_unverified_lines(pms_existing: Sequence[Any], pms_incoming: Sequence[Any]) -> list[str]:
    """把未验活候选合并进已有列表（**只增不删** + 去重）。

    与 `ptb_merge_bridge_lines()` 分开：那条路按**指纹**去重（要解析），这条路存的是
    「验活没通过的原始行」，解析不出来也得留着（用户可能想手工修），所以按整行文本去重。
    """
    merged: list[str] = []
    for item in list(pms_existing or []) + list(pms_incoming or []):
        text = str(item).strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def ptb_core_kind_error(pms_core_kind: str) -> str:
    """核心种类检查：返回空串表示可以用，返回文案表示要拦截（界面直接显示这句话）。"""
    from pms.constants import PMS_CORE_KIND_SINGBOX

    if str(pms_core_kind or "") == PMS_CORE_KIND_SINGBOX:
        return ""
    return "Tor 出口仅支持 sing-box 核心（当前核心是 Xray，请在「设置 → 核心」里切到 sing-box）"
