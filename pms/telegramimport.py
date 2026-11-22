#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电报频道「纯抓添加」：抓频道 → 得到节点链接文本（可选继续抓频道内的订阅地址）。

与「电报频道型订阅」的区别（v1.5.3）：
    · 订阅链路（`pms/subscription.py`）：建一条订阅记录，之后按间隔反复更新；
    · 本模块：只把抓到的节点**加进节点库**，**不创建任何订阅记录**——
      用户明确要的是「有时候不想订阅，就是纯抓添加」。

复用而不是重写（宪法第 19 条）：
    · 通路组装、页面解析、链接抽取全部沿用 `pms/telegram.py` 既有函数；
    · 设置沿用 `telegram.proxy_mode` / `telegram.external_proxy` / `telegram.pages`
      / `telegram.timeout_seconds` 与 `inbound.mixed_port`，**不新增任何设置项**。
"""

from __future__ import annotations

import logging
import urllib.request
from typing import TYPE_CHECKING, Any, Iterable, Optional

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from pms.constants import (
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_LOGGER_NAME,
    PMS_TELEGRAM_PROXY_AUTO,
)
from pms.telegram import (
    PMS_TELEGRAM_FETCH_MODE_FULL,
    PMS_TELEGRAM_FETCH_MODE_INCREMENTAL,
    PMS_TELEGRAM_MAX_PAGES,
    PMS_TELEGRAM_PAGES_TO_BEGINNING,
    pms_telegram_build_openers,
    pms_telegram_channel_from_url,
    pms_telegram_fetch_channel_detailed,
)
from pms.utils import pms_b64_decode_loose, pms_dedupe_keep_order, pms_looks_like_base64, pms_safe_int

if TYPE_CHECKING:  # 仅类型检查用，避免与运行时模块循环导入
    from pms.settings import PmsSettingsManager

# 「继续抓」一次最多跟几个订阅地址：避免一个频道里挂一串地址把人网卡死
PMS_TELEGRAM_IMPORT_MAX_SUBS = 5


def pms_telegram_import_openers(pms_settings: "PmsSettingsManager") -> list[tuple[str, Any]]:
    """按当前设置组装电报抓取通路 [(通路名, opener)]，与订阅链路同一套退让顺序。"""
    return pms_telegram_build_openers(
        pms_settings.psm_get_str("telegram.proxy_mode", PMS_TELEGRAM_PROXY_AUTO),
        pms_settings.psm_get_int("inbound.mixed_port", 0),
        pms_settings.psm_get_str("telegram.external_proxy", ""),
        pms_settings.psm_get_str("telegram.proxy_order", ""),
    )


def pms_telegram_import_pages(pms_settings: "PmsSettingsManager") -> int:
    """本次导入要翻的页数：沿用「设置 → 电报频道 → 抓取页数」（v1.9.2：1–50）。

    v1.9.5：全量模式且开了「翻到底」时返回哨兵值 `PMS_TELEGRAM_PAGES_TO_BEGINNING`（-1），
    调用方直接透传给 `pms_telegram_fetch_channel_detailed()` 即可，它会一直翻到频道开头。
    """
    if (pms_telegram_import_mode(pms_settings) == PMS_TELEGRAM_FETCH_MODE_FULL
            and pms_settings.psm_get_bool("telegram.fetch_to_beginning", False)):
        return PMS_TELEGRAM_PAGES_TO_BEGINNING
    return pms_safe_int(pms_settings.psm_get_int("telegram.pages", 1), 1, 1, PMS_TELEGRAM_MAX_PAGES)


def pms_telegram_import_mode(pms_settings: "PmsSettingsManager") -> str:
    """抓取模式（v1.9.2）：`full` 全量 / `incremental` 增量，与订阅链路同一套设置。"""
    mode = str(pms_settings.psm_get_str("telegram.fetch_mode", PMS_TELEGRAM_FETCH_MODE_FULL) or "").strip().lower()
    return mode if mode in (PMS_TELEGRAM_FETCH_MODE_FULL, PMS_TELEGRAM_FETCH_MODE_INCREMENTAL) else PMS_TELEGRAM_FETCH_MODE_FULL


def pms_telegram_import_last_id_key(pms_channel: str) -> str:
    """频道「上次抓到的最大消息号」在 `pms_meta` 里的键——订阅链路与导入链路共用同一份。"""
    return f"telegram.last_id.{str(pms_channel or '').strip()}"


def pms_telegram_import_timeout(pms_settings: "PmsSettingsManager") -> float:
    """单页超时：沿用「设置 → 电报频道 → 超时秒数」（3–120）。"""
    return float(max(3, pms_safe_int(pms_settings.psm_get_int("telegram.timeout_seconds", 20), 20, 3, 120)))


def pms_telegram_import_subscription_text(pms_text: str) -> str:
    """订阅正文 → 每行一条的节点链接文本（base64 正文先解码，解不开就原样返回）。"""
    raw = str(pms_text or "").strip()
    if not raw:
        return ""
    if pms_looks_like_base64(raw):
        decoded = pms_b64_decode_loose(raw).strip()
        if decoded:
            return decoded
    return raw


# --------------------------------------------------------------------------------------
# 信号与后台任务
# --------------------------------------------------------------------------------------
class PmsTelegramImportSignals(QObject):
    """抓取任务 → 主线程。"""

    ptis_message = Signal(str)                  # 过程提示（哪条通路、抓到多少）
    ptis_done = Signal(str, dict, str)          # 链接文本, 统计, 错误说明（成功时为空串）


class PmsTelegramImportWorker(QRunnable):
    """抓取电报频道（或频道内的订阅地址），把结果作为「每行一条节点链接」的文本交回去。

    两种模式二选一（构造时决定）：
        · 频道模式 `pms_channel`：翻页抓频道消息，抽节点链接，并顺带报出频道内的订阅地址；
        · 订阅模式 `pms_subscriptions`：抓这些 http(s) 订阅地址，正文还原成链接文本。
    任何异常都不外抛，统一走 `ptis_done` 的错误说明（后台线程抛异常会丢结果）。
    """

    def __init__(
        self,
        pms_signals: PmsTelegramImportSignals,
        pms_settings: "PmsSettingsManager",
        pms_channel: str = "",
        pms_subscriptions: Iterable[str] = (),
        pms_engine: Any = None,
    ) -> None:
        super().__init__()
        self.ptiw_signals = pms_signals
        self.ptiw_settings = pms_settings
        self.ptiw_channel = str(pms_channel or "").strip()
        self.ptiw_subscriptions = [str(item).strip() for item in pms_subscriptions if str(item).strip()]
        self.ptiw_engine = pms_engine   # 增量模式靠它读写 pms_meta；为 None 时退化为全量
        self.ptiw_logger = logging.getLogger(PMS_LOGGER_NAME + ".telegramimport")
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        try:
            if self.ptiw_channel:
                self.ptiw_fetch_channel()
            else:
                self.ptiw_fetch_subscriptions()
        except Exception as exc:  # 兜底：后台线程里任何异常都必须变成界面能显示的说明
            self.ptiw_logger.warning("电报抓取异常：%s", exc)
            self.ptiw_signals.ptis_done.emit("", {}, f"电报抓取异常：{exc}")

    # ---------------------------------------------------------------- 增量进度
    def ptiw_read_last_id(self, pms_channel: str) -> int:
        """读上次抓到的最大消息号（0 = 无记录 → 首次，按上限抓基线）。失败按 0 处理。"""
        if self.ptiw_engine is None:
            return 0
        try:
            return pms_safe_int(
                self.ptiw_engine.pdb_get_meta(pms_telegram_import_last_id_key(pms_channel), "0"), 0, 0, 10 ** 15,
            )
        except Exception as exc:  # noqa: BLE001 - 读失败只影响增量，退化为全量即可
            self.ptiw_logger.warning("读取电报历史消息号失败（退化为全量）：%s", exc)
            return 0

    def ptiw_write_last_id(self, pms_channel: str, pms_max_id: Any) -> None:
        """写回本次抓到的最大消息号。数据库有 RLock，后台线程写是安全的。"""
        value = pms_safe_int(pms_max_id, 0, 0, 10 ** 15)
        if self.ptiw_engine is None or value <= 0:
            return
        try:
            self.ptiw_engine.pdb_set_meta(pms_telegram_import_last_id_key(pms_channel), str(value))
        except Exception as exc:  # noqa: BLE001 - 写失败只影响下次增量，不影响本次结果
            self.ptiw_logger.warning("写回电报历史消息号失败：%s", exc)

    # ---------------------------------------------------------------- 频道模式
    def ptiw_fetch_channel(self) -> None:
        channel = self.ptiw_channel or pms_telegram_channel_from_url(self.ptiw_channel)
        if not channel:
            self.ptiw_signals.ptis_done.emit("", {}, "不是可抓取的电报公开频道（仅支持 https://t.me/<频道名>）")
            return
        openers = pms_telegram_import_openers(self.ptiw_settings)
        if not openers:
            self.ptiw_signals.ptis_done.emit(
                "", {}, "电报抓取没有可用通路：请到「设置 → 电报频道」检查代理设置（本程序未启动代理 / 外部代理地址为空）",
            )
            return
        mode = pms_telegram_import_mode(self.ptiw_settings)
        after_id = self.ptiw_read_last_id(channel) if mode == PMS_TELEGRAM_FETCH_MODE_INCREMENTAL else 0
        # 增量（含首次基线）统一按上限翻页：越过上次边界会自动停，不会真的翻满
        pages = PMS_TELEGRAM_MAX_PAGES if mode == PMS_TELEGRAM_FETCH_MODE_INCREMENTAL else pms_telegram_import_pages(self.ptiw_settings)
        timeout = pms_telegram_import_timeout(self.ptiw_settings)
        user_agent = self.ptiw_settings.psm_get_str("subscription.user_agent", PMS_DEFAULT_SUB_USER_AGENT)
        tried: list[str] = []
        for name, opener in openers:
            self.ptiw_signals.ptis_message.emit(f"电报频道 {channel}：正在尝试「{name}」")
            text, subscriptions, stats = pms_telegram_fetch_channel_detailed(
                channel, opener, pages, timeout, user_agent, self.ptiw_logger, after_id,
                pms_route=name,   # v1.9.72：带上通路名，日志可区分良性退让
            )
            if pms_safe_int(stats.get("messages"), 0) <= 0:
                tried.append(f"{name}（未取到页面）")
                continue
            stats = dict(stats)
            stats["channel"] = channel
            stats["route"] = name
            stats["mode"] = mode
            stats["after_id"] = after_id
            stats["subscriptions_list"] = list(subscriptions[:PMS_TELEGRAM_IMPORT_MAX_SUBS])
            self.ptiw_write_last_id(channel, stats.get("max_id", 0))
            if not str(text).strip():
                if after_id > 0 and pms_safe_int(stats.get("new_messages"), 0, 0, 10 ** 9) <= 0:
                    self.ptiw_signals.ptis_done.emit("", stats, f"电报频道 {channel} 自上次抓取（消息号 {after_id}）以来没有新贴")
                    return
                hint = f"，其中 {len(subscriptions)} 个是订阅地址（可点「继续抓」）" if subscriptions else ""
                self.ptiw_signals.ptis_done.emit(
                    "", stats, f"电报频道 {channel} 已抓到 {stats.get('messages')} 条消息，但没有节点链接{hint}",
                )
                return
            self.ptiw_signals.ptis_message.emit(
                f"电报频道 {channel}：{stats.get('pages')} 页 / {stats.get('messages')} 条消息 / "
                f"{stats.get('links')} 条节点链接（{name}）",
            )
            self.ptiw_signals.ptis_done.emit(text, stats, "")
            return
        self.ptiw_signals.ptis_done.emit(
            "", {}, f"电报频道 {channel} 抓取失败：{'；'.join(tried)}。t.me 需要代理，请到「设置 → 电报频道」检查代理设置",
        )

    # ---------------------------------------------------------------- 订阅模式
    def ptiw_fetch_subscriptions(self) -> None:
        targets = self.ptiw_subscriptions[:PMS_TELEGRAM_IMPORT_MAX_SUBS]
        if not targets:
            self.ptiw_signals.ptis_done.emit("", {}, "没有可继续抓取的订阅地址")
            return
        openers = pms_telegram_import_openers(self.ptiw_settings)
        if not openers:
            self.ptiw_signals.ptis_done.emit("", {}, "没有可用通路：请到「设置 → 电报频道」检查代理设置")
            return
        timeout = pms_telegram_import_timeout(self.ptiw_settings)
        user_agent = self.ptiw_settings.psm_get_str("subscription.user_agent", PMS_DEFAULT_SUB_USER_AGENT)
        headers = {"User-Agent": user_agent, "Accept": "*/*", "Accept-Encoding": "identity"}
        blocks: list[str] = []
        ok = 0
        failed: list[str] = []
        opener = openers[0][1] if openers else urllib.request.build_opener()
        route_name = openers[0][0] if openers else "直连"
        for url in targets:
            self.ptiw_signals.ptis_message.emit(f"正在抓取订阅地址（{route_name}）：{url}")
            try:
                with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                    payload = response.read(8 * 1024 * 1024)
            except Exception as exc:
                failed.append(f"{url}（{exc}）")
                continue
            text = ""
            for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
                try:
                    text = payload.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            body = pms_telegram_import_subscription_text(text)
            if body:
                ok += 1
                blocks.append(body)
        if not blocks:
            self.ptiw_signals.ptis_done.emit("", {}, "订阅地址全部抓取失败：" + "；".join(failed))
            return
        merged = "\n".join(block for block in blocks if block.strip())
        links = [line.strip() for line in merged.splitlines() if line.strip()]
        stats = {
            "subscriptions_fetched": ok,
            "subscriptions_failed": len(failed),
            "links": len(pms_dedupe_keep_order(links)),
        }
        message = f"已抓取 {ok} 个订阅地址，得到 {stats['links']} 条链接"
        if failed:
            message += f"，{len(failed)} 个失败"
        self.ptiw_signals.ptis_message.emit(message)
        self.ptiw_signals.ptis_done.emit("\n".join(pms_dedupe_keep_order(links)), stats, "")
