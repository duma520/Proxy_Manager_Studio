#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅更新

抓取订阅 → 识别格式 → 解析节点 → 与库中记录合并 → 批量落库，返回新增/更新/移除统计。
"""

from __future__ import annotations

from typing import (
    Any,
    Optional,
    Sequence,
)
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    Signal,
    Slot,
)
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

from pms.constants import (
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_EXIT_POOL_WAIT_MS,
    PMS_LOGGER_NAME,
    PMS_SUB_API_KEY_ENV,
    PMS_TELEGRAM_PROXY_AUTO,
)
from pms.utils import (
    pms_now_text,
    pms_safe_int,
)
from pms.telegram import (
    PMS_TELEGRAM_DEFAULT_BACKFILL_DELAY,
    PMS_TELEGRAM_DEFAULT_BACKFILL_PAGES,
    PMS_TELEGRAM_FETCH_MODE_FULL,
    PMS_TELEGRAM_FETCH_MODE_INCREMENTAL,
    PMS_TELEGRAM_FETCH_MODES,
    PMS_TELEGRAM_MAX_PAGES,
    PMS_TELEGRAM_PAGES_TO_BEGINNING,
    pms_telegram_build_openers,
    pms_telegram_channel_from_url,
    pms_telegram_fetch_channel,
)
from pms.envstore import PmsEnvStore
from pms.subkind import (   # v1.9.44：订阅源类型判定搬去 subkind.py（本文件顶 1000 行红线）
    PMS_SUBSCRIPTION_KIND_GITHUB,
    PMS_SUBSCRIPTION_KIND_TELEGRAM,
    PMS_SUBSCRIPTION_KIND_TITLES,
    PMS_SUBSCRIPTION_KIND_TOR_BRIDGE,
    PMS_SUBSCRIPTION_KIND_TOR_BRIDGE_TITLE,
    PMS_SUBSCRIPTION_KIND_URL,
    pms_subscription_kind,
    pms_subscription_kind_title,
    pms_subscription_matches_filter,
    pms_subscription_record_kind_title,
)
from pms.bridgefeed import _PsubBridgeMixin   # v1.9.44：Tor 桥接源（订阅侧）的抓取与落盘
from pms.githubsource import (
    PmsGithubRepoSource,
    pms_github_repo_from_url,
)
from pms.models import (
    PmsProxyNode,
    PmsSubscriptionRecord,
)
from pms.database import PmsNodeRepository
from pms.sharelinks import PmsImportReport
from pms.importers import PmsTextImportClassifier

# --------------------------------------------------------------------------------------
# 订阅源类型（v1.9.18）
# --------------------------------------------------------------------------------------
# 订阅源类型判定已搬到 `pms/subkind.py`（v1.9.44，本文件顶 1000 行红线），这里再导出一遍：
#   github / telegram / tor_bridge / url —— 见 `pms/subkind.py` 的模块说明
class PmsSubscriptionSignals(QObject):
    """订阅任务 → 主线程信号。"""
    psubs_task_done = Signal(str, dict)      # 订阅 uid, 结果
    psubs_task_message = Signal(str, str)    # 订阅 uid, 人类可读进度文本
    psubs_task_progress = Signal(str, int, int)  # 订阅 uid, 已下载字节, 总字节（0 表示未知）

class PmsSubscriptionUpdateTask(_PsubBridgeMixin, QRunnable):
    """抓取并解析一个订阅，写入数据库，返回统计结果。"""

    def __init__(
        self,
        pms_signals: PmsSubscriptionSignals,
        pms_settings: "PmsSettingsManager",
        pms_repo: PmsNodeRepository,
        pms_record: PmsSubscriptionRecord,
        pms_import_classifier: PmsTextImportClassifier,
        pms_env: PmsEnvStore,
        pms_local_proxy_port: int = 0,
        pms_fetch_mode: str = "",
    ) -> None:
        super().__init__()
        self.psub_signals = pms_signals
        self.psub_settings = pms_settings
        self.psub_repo = pms_repo
        self.psub_record = pms_record
        self.psub_classifier = pms_import_classifier
        self.psub_env = pms_env
        self.psub_local_proxy_port = int(pms_local_proxy_port or 0)
        # v1.9.3：本次运行的一次性抓取模式覆盖（full / incremental）。
        # 空串表示沿用设置项 telegram.fetch_mode；菜单「全量 / 增量」与对话框两个按钮走覆盖，
        # 不写回设置——一次性动作不该悄悄改掉用户的长期偏好。
        self.psub_fetch_mode = str(pms_fetch_mode or "").strip().lower()
        self.psub_telegram_stats: dict[str, Any] = {}
        # v1.9.18：GitHub 仓库订阅（仓库地址 → API 自动发现文件 → 合并解析）
        self.psub_github_files: list[tuple[str, str]] = []   # [(文件路径, 正文), …]
        self.psub_github_stats: dict[str, Any] = {}
        self.psub_github_note = ""
        self.psub_cross_matched = 0   # v1.9.1：本次更新命中「跨订阅重复」的节点数（仅用于统计展示）
        self.psub_no_new_content = False  # v1.9.2：增量模式下本次没有新贴（空更新，不算失败）
        self.psub_allow_prune = True      # v1.9.2：是否允许删除「本次未出现」的节点（增量模式必须关掉）
        # v1.9.73：本次订阅是否是电报频道、以及是否抓到了完整视图（翻到底）。
        # 电报频道「窗口化抓取」绝不能据本次窗口删历史节点，否则几万条历史帖节点会被全删。
        self.psub_is_telegram_channel = False
        self.psub_full_telegram_view = False
        # v1.9.9：退出/停止时的中断标志与当前响应句柄。
        # 订阅抓取一次最多等 25 秒，退出时若不打断，线程池线程会一直挂着，
        # Python 进程退不出去（表现就是「退出流程完成」但窗口还在）。
        self.psub_abort_event = threading.Event()
        self.psub_response: Optional[Any] = None
        self.setAutoDelete(True)

    # ------------------------------------------------------------------ 中断（v1.9.9）
    def psub_is_aborted(self) -> bool:
        """是否已请求中断。"""
        return self.psub_abort_event.is_set()

    def psub_abort(self) -> None:
        """请求中断：置标志 + 关掉正在读的响应（让阻塞中的 read() 抛异常退出）。"""
        self.psub_abort_event.set()
        response = self.psub_response
        self.psub_response = None
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - 关闭失败无关紧要，标志位已经能兜住
                pass

    # ------------------------------------------------------------------ 网络
    def psub_build_opener(self) -> Any:
        """按设置构造带（或不带）本地代理的 opener。"""
        use_proxy = bool(self.psub_record.use_system_proxy) or self.psub_settings.psm_get_bool("subscription.use_system_proxy", False)
        if not use_proxy or not self.psub_local_proxy_port:
            return urllib.request.build_opener()
        handler = urllib.request.ProxyHandler({
            "http": f"http://127.0.0.1:{self.psub_local_proxy_port}",
            "https": f"http://127.0.0.1:{self.psub_local_proxy_port}",
        })
        return urllib.request.build_opener(handler)

    def psub_build_headers(self) -> dict[str, str]:
        """请求头：User-Agent + 可选 API Key（只从订阅记录或 .env 读取，绝不硬编码）。"""
        user_agent = str(self.psub_record.user_agent or "").strip() or self.psub_settings.psm_get_str("subscription.user_agent", PMS_DEFAULT_SUB_USER_AGENT)
        headers = {
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        }
        api_key = str(self.psub_record.api_key or "").strip()
        if not api_key and self.psub_settings.psm_get_bool("subscription.use_env_api_key", True):
            api_key = self.psub_env.pes_get(PMS_SUB_API_KEY_ENV, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        return headers

    def psub_fetch(self) -> tuple[str, dict[str, str], str]:
        """抓取订阅内容，返回 (正文, 响应头, 错误说明)。

        电报频道（`https://t.me/<频道名>`）走单独的抓取链路，见 `psub_fetch_telegram`。
        """
        url = str(self.psub_record.url or "").strip()
        if pms_telegram_channel_from_url(url):
            return self.psub_fetch_telegram()
        owner, repo = self.psub_github_repo()
        if owner and repo:
            return self.psub_fetch_github()
        if not url.lower().startswith(("http://", "https://")):
            return "", {}, "订阅地址必须以 http:// 或 https:// 开头"
        timeout = max(3, self.psub_settings.psm_get_int("subscription.timeout_seconds", 25))
        request = urllib.request.Request(url, headers=self.psub_build_headers())
        opener = self.psub_build_opener()
        try:
            if self.psub_is_aborted():        # v1.9.9：退出时不再发起新请求
                return "", {}, "订阅更新已中断（程序正在退出）"
            response = opener.open(request, timeout=timeout)
            self.psub_response = response     # v1.9.9：登记给 psub_abort() 关闭
            try:
                headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
                payload = response.read(8 * 1024 * 1024)
            finally:
                self.psub_response = None
                response.close()
            self.psub_signals.psubs_task_progress.emit(self.psub_record.uid, len(payload), len(payload))
        except urllib.error.HTTPError as exc:
            return "", {}, f"HTTP {exc.code} {exc.reason}"
        except urllib.error.URLError as exc:
            return "", {}, f"网络错误：{getattr(exc, 'reason', exc)}"
        except (OSError, ValueError) as exc:
            return "", {}, f"请求异常：{exc}"
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
            try:
                return payload.decode(encoding), headers, ""
            except UnicodeDecodeError:
                continue
        return payload.decode("utf-8", errors="replace"), headers, ""

    # ------------------------------------------------------------------ 电报频道
    def psub_telegram_channel(self) -> str:
        """本订阅是不是电报公开频道；是则给出频道名，否则空串。"""
        return pms_telegram_channel_from_url(str(self.psub_record.url or "").strip())

    def psub_telegram_openers(self) -> list[tuple[str, Any]]:
        """电报抓取的通路顺序：[(通路名, opener)]。按 `telegram.proxy_mode` / `telegram.proxy_order` 组装，见 pms.telegram。"""
        return pms_telegram_build_openers(
            self.psub_settings.psm_get_str("telegram.proxy_mode", PMS_TELEGRAM_PROXY_AUTO),
            self.psub_local_proxy_port,
            self.psub_settings.psm_get_str("telegram.external_proxy", ""),
            self.psub_settings.psm_get_str("telegram.proxy_order", ""),
        )

    def psub_telegram_last_id_key(self) -> str:
        """本频道「上次抓到的最大消息号」在 `pms_meta` 里的键。"""
        return f"telegram.last_id.{self.psub_telegram_channel()}"

    def psub_telegram_last_id(self) -> int:
        """读上次抓到的最大消息号（0 = 没有历史记录）。读失败按 0 处理，退化为全量。"""
        try:
            engine = getattr(self.psub_repo, "pnr_engine", None)
            if engine is None:
                return 0
            return pms_safe_int(engine.pdb_get_meta(self.psub_telegram_last_id_key(), "0"), 0, 0, 10 ** 15)
        except Exception as exc:  # noqa: BLE001 - 读取失败只影响增量，绝不能让任务崩
            logging.getLogger(PMS_LOGGER_NAME + ".telegram").warning("读取电报历史消息号失败（退化为全量）：%s", exc)
            return 0

    def psub_telegram_remember_last_id(self, pms_max_id: int) -> None:
        """写回本次抓到的最大消息号。数据库有 RLock，后台线程调用是安全的。"""
        value = pms_safe_int(pms_max_id, 0, 0, 10 ** 15)
        if value <= 0:
            return
        try:
            engine = getattr(self.psub_repo, "pnr_engine", None)
            if engine is None:
                return
            engine.pdb_set_meta(self.psub_telegram_last_id_key(), str(value))
        except Exception as exc:  # noqa: BLE001 - 写失败只影响下次增量，不影响本次结果
            logging.getLogger(PMS_LOGGER_NAME + ".telegram").warning("写回电报历史消息号失败：%s", exc)

    def psub_telegram_mode(self) -> str:
        """本次运行的抓取模式：一次性覆盖优先，否则读设置项，非法值回落 `full`。"""
        mode = self.psub_fetch_mode or str(self.psub_settings.psm_get_str("telegram.fetch_mode", PMS_TELEGRAM_FETCH_MODE_FULL) or "")
        mode = mode.strip().lower()
        return mode if mode in PMS_TELEGRAM_FETCH_MODES else PMS_TELEGRAM_FETCH_MODE_FULL

    # ------------------------------------------------------------------ 慢速回填（v1.9.6）
    def psub_telegram_meta_get(self, pms_key: str) -> str:
        """读 `pms_meta` 里的电报游标 / 标记；读不到返回空串（数据库带 RLock，后台线程安全）。"""
        try:
            engine = getattr(self.psub_repo, "pnr_engine", None)
            if engine is None:
                return ""
            return str(engine.pdb_get_meta(str(pms_key), "") or "")
        except Exception as exc:  # noqa: BLE001 - 读失败只影响回填进度，绝不能让任务崩
            logging.getLogger(PMS_LOGGER_NAME + ".telegram").warning("读取电报回填游标失败（%s）：%s", pms_key, exc)
            return ""

    def psub_telegram_meta_set(self, pms_key: str, pms_value: str) -> None:
        """写 `pms_meta`；写失败只影响下一轮起点，不影响本次结果。"""
        try:
            engine = getattr(self.psub_repo, "pnr_engine", None)
            if engine is None:
                return
            engine.pdb_set_meta(str(pms_key), str(pms_value))
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(PMS_LOGGER_NAME + ".telegram").warning("写回电报回填游标失败（%s）：%s", pms_key, exc)

    def psub_telegram_backfill_cursor_key(self) -> str:
        """本频道「已爬到的最旧消息号」在 `pms_meta` 里的键。"""
        return f"telegram.backfill_cursor.{self.psub_telegram_channel()}"

    def psub_telegram_backfill_done_key(self) -> str:
        """本频道「历史已爬完」标记键。"""
        return f"telegram.backfill_done.{self.psub_telegram_channel()}"

    def psub_telegram_backfill_enabled(self) -> bool:
        """是否开启慢速回填；且本次不是菜单「全量 / 增量」的一次性覆盖（那两种要保持纯粹）。"""
        return (not self.psub_fetch_mode) and bool(self.psub_settings.psm_get_bool("telegram.backfill_enabled", False))

    def psub_telegram_backfill(
        self,
        pms_channel: str,
        pms_openers: list,
        pms_timeout: float,
        pms_user_agent: str,
        pms_logger: Optional[logging.Logger],
        pms_start_before: int,
    ) -> tuple[str, dict]:
        """慢速回填一轮：从上次的最旧消息号接着往旧翻几页，返回 (节点链接文本, 统计)。

        大频道（本例 2.7 万条 ≈ 1400 页）一次翻到底必被 t.me 限流，因此改成：
        每轮只翻 `telegram.backfill_pages_per_round` 页（默认 10），页间随机停顿
        `telegram.backfill_delay_seconds` 秒（默认 2，±30% 抖动），靠很多轮慢慢补完。
        进度存 `pms_meta`（`telegram.backfill_cursor.<频道>`），到频道开头后写 done 标记，
        之后不再发任何请求。

        **安全闸**：新游标没有比旧游标更旧（页面异常一直返回同一批）时判定到底并写 done，
        防止无限循环把 t.me 打爆。
        """
        logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".telegram")
        cursor_key = self.psub_telegram_backfill_cursor_key()
        done_key = self.psub_telegram_backfill_done_key()
        if self.psub_telegram_meta_get(done_key) == "1":
            return "", {}
        saved = pms_safe_int(self.psub_telegram_meta_get(cursor_key), 0, 0, 10 ** 15)
        before = saved or pms_safe_int(pms_start_before, 0, 0, 10 ** 15)
        if before <= 0:
            return "", {}
        pages = pms_safe_int(
            self.psub_settings.psm_get_int("telegram.backfill_pages_per_round", PMS_TELEGRAM_DEFAULT_BACKFILL_PAGES),
            PMS_TELEGRAM_DEFAULT_BACKFILL_PAGES, 1, PMS_TELEGRAM_MAX_PAGES,
        )
        delay = float(pms_safe_int(
            self.psub_settings.psm_get_int("telegram.backfill_delay_seconds", int(PMS_TELEGRAM_DEFAULT_BACKFILL_DELAY)),
            int(PMS_TELEGRAM_DEFAULT_BACKFILL_DELAY), 0, 60,
        ))
        for name, opener in pms_openers:
            text, stats = self.psub_telegram_fetch_with_text(
                pms_channel, opener, pages, pms_timeout, pms_user_agent, logger, 0, before, delay,
                pms_abort_event=self.psub_abort_event,   # v1.9.9：退出时立刻停止回填
            )
            if pms_safe_int(stats.get("messages"), 0, 0, 10 ** 9) <= 0:
                continue
            lowest = pms_safe_int(stats.get("min_id"), 0, 0, 10 ** 15)
            # 到底判定：提前停（空页 / 抓取失败）｜没取到消息号｜游标没往旧推进
            reached = bool(stats.get("exhausted")) or lowest <= 0 or lowest >= before
            if lowest > 0 and not reached:
                self.psub_telegram_meta_set(cursor_key, str(lowest))
            if reached:
                self.psub_telegram_meta_set(done_key, "1")
            logger.info("电报频道 %s：回填 %d 页（起点 %d，页间停顿 %.1f 秒），最旧消息号 %d，节点链接 %d 条 —— %s",
                        pms_channel, pms_safe_int(stats.get("pages"), 0), before, delay, lowest,
                        pms_safe_int(stats.get("links"), 0),
                        "已到频道开头，后续不再回填" if reached else f"下轮从 {lowest} 继续")
            stats = dict(stats)
            stats["route"] = name
            stats["cursor"] = lowest
            stats["reached_beginning"] = 1 if reached else 0
            return str(text or ""), stats
        return "", {}

    def psub_fetch_telegram(self) -> tuple[str, dict[str, str], str]:
        """抓取电报频道：按通路顺序逐个试，返回 (节点链接文本, {}, 错误说明)。

        抓到的节点链接拼成每行一条，后面仍走既有的 `ptic_import_text` → 合并 → 落库，
        不新增数据库字段。

        **v1.9.2 两种模式**（`telegram.fetch_mode`）：
            · `full` 全量：从最新页往回翻满设定页数（1–50），遇到空页提前停；
            · `incremental` 增量：只收比上次最大消息号更新的贴，越界即停。
              历史消息号存 `pms_meta`，首次没有记录时**直接抓到上限**作为基线。
              增量模式下本次结果只是「新增部分」，会自动关掉「移除已消失节点」，
              否则旧节点会被当成已消失而删光。
        """
        channel = self.psub_telegram_channel()
        if not channel:
            return "", {}, "不是可抓取的电报公开频道（仅支持 https://t.me/<频道名>）"
        self.psub_is_telegram_channel = True   # v1.9.73：本次是电报频道（窗口化抓取，绝不可据窗口删历史）
        self.psub_telegram_stats = {}
        openers = self.psub_telegram_openers()
        if not openers:
            return "", {}, "电报抓取没有可用通路：请到「设置 → 电报频道」检查代理设置（本程序未启动代理 / 外部代理地址为空）"
        pages = pms_safe_int(self.psub_settings.psm_get_int("telegram.pages", 1), 1, 1, PMS_TELEGRAM_MAX_PAGES)
        mode = self.psub_telegram_mode()
        after_id = 0
        if mode == PMS_TELEGRAM_FETCH_MODE_INCREMENTAL:
            after_id = self.psub_telegram_last_id()
            # 增量一律按上限翻页：越过上次边界会自动停，不会真的翻满 50 页
            pages = PMS_TELEGRAM_MAX_PAGES
        elif mode == PMS_TELEGRAM_FETCH_MODE_FULL and self.psub_settings.psm_get_bool("telegram.fetch_to_beginning", False):
            # v1.9.5「翻到底」：忽略设置页数，一直往回翻到频道开头（遇空页自动停，另有硬保险）
            pages = PMS_TELEGRAM_PAGES_TO_BEGINNING
        elif self.psub_fetch_mode:
            # 显式「全量」：不看设置页数，直接翻到上限（要的就是「从第一个贴开始全要」）
            pages = PMS_TELEGRAM_MAX_PAGES
        # v1.9.73：翻到底（pages 哨兵值）才是频道完整视图，此时据结果删旧节点才是安全的；
        # 否则只是窗口（最新几页 / 增量 / 回填），绝不能据它删历史节点。
        self.psub_full_telegram_view = (pages == PMS_TELEGRAM_PAGES_TO_BEGINNING)
        timeout = float(max(3, pms_safe_int(self.psub_settings.psm_get_int("telegram.timeout_seconds", 20), 20, 3, 120)))
        user_agent = str(self.psub_record.user_agent or "").strip() or self.psub_settings.psm_get_str("subscription.user_agent", PMS_DEFAULT_SUB_USER_AGENT)
        logger = logging.getLogger(PMS_LOGGER_NAME + ".telegram")
        tried: list[str] = []
        for name, opener in openers:
            self.psub_signals.psubs_task_message.emit(self.psub_record.uid, f"电报频道 {channel}：正在尝试「{name}」")
            text, stats = self.psub_telegram_fetch_with_text(
                channel, opener, pages, timeout, user_agent, logger, after_id,
                pms_abort_event=self.psub_abort_event,   # v1.9.9：退出时立刻停止翻页
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
            self.psub_telegram_stats = stats
            self.psub_telegram_remember_last_id(stats.get("max_id", 0))
            if after_id > 0:
                # 增量模式：本次结果只是「新增部分」，绝不能据它删除旧节点
                self.psub_allow_prune = False
                if pms_safe_int(stats.get("new_messages"), 0, 0, 10 ** 9) <= 0:
                    # 有消息但全是旧贴 → 空更新，不算失败，也不落库
                    self.psub_no_new_content = True
                    return "", {}, ""
            elif self.psub_is_telegram_channel and not self.psub_full_telegram_view:
                # v1.9.73：电报频道 + 非「翻到底」完整视图（含菜单「全量」50 页窗口 / 定时更新窗口）
                # 本次只是频道的一个窗口，绝不是完整视图，绝不能据它删除历史节点，否则几万条历史帖节点被全删。
                self.psub_allow_prune = False
            self.psub_signals.psubs_task_progress.emit(self.psub_record.uid, len(text), len(text))
            if not str(text).strip():
                if self.psub_has_tor_bridges():   # v1.9.44：纯 Tor 桥接频道，没有节点链接是正常的
                    return "", {}, ""
                hint = f"，其中 {stats.get('subscriptions', 0)} 个疑似订阅地址（暂不自动跟随）" if stats.get("subscriptions") else ""
                return "", {}, (f"电报频道 {channel} 已抓到 {stats.get('messages')} 条消息，但没有节点链接{hint}")
            if self.psub_telegram_backfill_enabled():
                # v1.9.6：本轮结果只是「最新一小段 + 历史一小段」，不是频道全量视图，
                # 必须关掉「移除本次未出现的节点」，否则前几轮辛苦抓回的历史节点会被删掉。
                self.psub_allow_prune = False
                extra_text, extra_stats = self.psub_telegram_backfill(
                    channel, openers, timeout, user_agent, logger, pms_safe_int(stats.get("min_id"), 0, 0, 10 ** 15),
                )
                if extra_stats:
                    stats["backfill"] = dict(extra_stats)
                    for key in ("pages", "messages", "links"):
                        stats[key] = pms_safe_int(stats.get(key), 0) + pms_safe_int(extra_stats.get(key), 0)
                if str(extra_text or "").strip():
                    text = f"{text}\n{extra_text}"
            self.psub_signals.psubs_task_message.emit(
                self.psub_record.uid,
                f"电报频道 {channel}：{stats.get('pages')} 页 / {stats.get('messages')} 条消息 / "
                f"{stats.get('links')} 条节点链接（{name}）",
            )
            return text, {}, ""
        return "", {}, f"电报频道 {channel} 抓取失败：{'；'.join(tried)}。t.me 需要代理，请到「设置 → 电报频道」检查代理设置"

    @staticmethod
    def psub_parse_traffic_header(pms_headers: dict[str, str]) -> dict[str, int]:
        """解析订阅流量头（subscription-userinfo / x-subscription-userinfo）。

        不同机场字段差异很大：有的带 expire=，有的带 reset_day= / reset_traffic=。
        解析时尽量兼容；并把原始 header 一并带回（raw_header），供排查，
        避免“解析失败只显示未知”却找不到原始内容。
        """
        result: dict[str, Any] = {
            "upload": 0, "download": 0, "total": 0,
            "expire": 0, "reset_day": 0, "reset_traffic": 0,
        }
        raw = (
            pms_headers.get("subscription-userinfo")
            or pms_headers.get("x-subscription-userinfo")
            or ""
        )
        # 兜底：标准字段名没命中时，扫描全部响应头，找含 upload=/expire= 的那条当原始头。
        if not raw:
            for _header_key, header_value in (pms_headers or {}).items():
                candidate = str(header_value or "")
                if "expire=" in candidate or "upload=" in candidate:
                    raw = candidate
                    break
        result["raw_header"] = str(raw)
        for item in str(raw).split(";"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip().lower()
            if key in result:
                result[key] = pms_safe_int(value.strip(), 0, 0, 10 ** 15)
        return result

    # ------------------------------------------------------------------ GitHub 仓库（v1.9.18）
    def psub_github_repo(self) -> tuple[str, str]:
        """本订阅是不是 GitHub 仓库；是则返回 (owner, repo)，否则 ("", "")。"""
        return pms_github_repo_from_url(str(self.psub_record.url or "").strip())

    def psub_github_meta_key(self, pms_suffix: str) -> str:
        """本订阅在 `pms_meta` 里的 GitHub 缓存键（按订阅 uid 隔离）。"""
        return f"github.{pms_suffix}.{self.psub_record.uid}"

    def psub_github_cached_sha(self) -> str:
        """上次抓到的 commit SHA（缓存在 `pms_meta`，SHA 没变就不下载任何文件）。"""
        return self.psub_telegram_meta_get(self.psub_github_meta_key("sha"))

    def psub_github_remember(self, pms_sha: str, pms_note: str) -> None:
        """记住本次的 commit SHA 与说明（写失败只影响下次是否重复下载）。"""
        if pms_sha:
            self.psub_telegram_meta_set(self.psub_github_meta_key("sha"), str(pms_sha))
        self.psub_telegram_meta_set(self.psub_github_meta_key("checked_at"), pms_now_text())
        if pms_note:
            self.psub_telegram_meta_set(self.psub_github_meta_key("note"), str(pms_note)[:300])

    def psub_github_token(self) -> str:
        """GitHub token：订阅记录里的 API Key 优先，其次 `.env`（有就能到 5000 次/小时）。"""
        token = str(self.psub_record.api_key or "").strip()
        if token:
            return token
        for env_key in ("PMS_GITHUB_TOKEN", "GITHUB_TOKEN"):
            value = str(self.psub_env.pes_get(env_key, "") or "").strip()
            if value:
                return value
        return ""

    def psub_fetch_github(self) -> tuple[str, dict[str, str], str]:
        """GitHub 仓库抓取：查 SHA → 变了才拉文件树 → 挑文件下载 → 逐个存进 `psub_github_files`。

        返回给 `run()` 的是空正文 + 空响应头（真正的正文在 `psub_github_files` 里，
        因为不同文件可能是 base64 / Clash YAML / 明文，必须**分开解析**再合并）。
        """
        owner, repo = self.psub_github_repo()
        if not owner or not repo:
            return "", {}, "GitHub 仓库地址格式不对（应形如 https://github.com/owner/repo 或 owner/repo）"
        source = PmsGithubRepoSource(
            owner,
            repo,
            pms_user_agent=self.psub_build_headers().get("User-Agent", ""),
            pms_timeout=float(max(5, self.psub_settings.psm_get_int("subscription.timeout_seconds", 25))),
            pms_token=self.psub_github_token(),
            pms_mirror=self.psub_settings.psm_get_str("core.download_mirror", ""),
            pms_logger=logging.getLogger(PMS_LOGGER_NAME + ".github"),
            pms_should_abort=self.psub_is_aborted,
        )
        self.psub_signals.psubs_task_message.emit(
            self.psub_record.uid, f"查询 {owner}/{repo} 最新提交…"
        )
        outcome = source.pgs_fetch(
            self.psub_build_opener(),
            pms_cached_sha=self.psub_github_cached_sha(),
            pms_max_files=max(1, self.psub_settings.psm_get_int("subscription.github_max_files", 20)),
            pms_max_total_mb=max(1, self.psub_settings.psm_get_int("subscription.github_max_total_mb", 24)),
            pms_max_nodes=max(1, self.psub_settings.psm_get_int("subscription.github_max_nodes", 500)),
            pms_follow_links=self.psub_settings.psm_get_bool("subscription.github_follow_links", True),
        )
        self.psub_github_stats = {
            "owner": owner,
            "repo": repo,
            "sha": str(outcome.get("sha") or "")[:12],
            "changed": bool(outcome.get("changed")),
            "files": [path for path, _text in outcome.get("chunks", [])],
            "skipped": list(outcome.get("skipped", []))[:10],
            "notes": list(outcome.get("notes", []))[:10],
            "api_calls": int(outcome.get("api_calls") or 0),
            "bytes": int(outcome.get("bytes") or 0),
            "rate_remaining": source.pgs_rate_remaining,
        }
        if not outcome.get("ok"):
            return "", {}, str(outcome.get("error") or "GitHub 仓库抓取失败")
        sha = str(outcome.get("sha") or "")
        self.psub_github_remember(sha, "；".join(outcome.get("notes", [])[:3]))
        if not outcome.get("changed"):
            # SHA 没变：算成功的空更新（不解析、不落库、不清理），一次只花 1 次 API 配额
            self.psub_no_new_content = True
            self.psub_github_note = f"{owner}/{repo} 没有新提交（SHA {sha[:12]}），已跳过下载"
            return "", {}, ""
        self.psub_github_files = list(outcome.get("chunks", []))
        notes = "；".join(str(item) for item in outcome.get("notes", []))
        self.psub_github_note = f"{owner}/{repo} @{sha[:12]}：{len(self.psub_github_files)} 个文件；{notes}"
        self.psub_signals.psubs_task_message.emit(self.psub_record.uid, self.psub_github_note)
        return "", {}, ""

    def psub_node_cap(self) -> int:
        """本次最多收多少节点：GitHub 仓库走 `subscription.github_max_nodes`（默认 500），其它订阅不限。"""
        owner, repo = self.psub_github_repo()
        if owner and repo:
            return max(1, self.psub_settings.psm_get_int("subscription.github_max_nodes", 500))
        return 0

    def psub_parse_content(self, pms_text: str, pms_group: str) -> tuple[list[Any], Any]:
        """解析订阅正文：GitHub 仓库按文件逐个解析（格式可能不一样）再合并；其它走原来的单次解析。"""
        if not self.psub_github_files:
            return self.psub_classifier.ptic_import_text(pms_text, pms_group)
        all_nodes: list[Any] = []
        report = None
        seen: set[str] = set()
        for path, chunk in self.psub_github_files:
            if self.psub_is_aborted():
                break
            if not str(chunk).strip():
                continue
            nodes, part = self.psub_classifier.ptic_import_text(chunk, pms_group)
            if report is None:
                report = part
                report.source = f"GitHub 仓库（{len(self.psub_github_files)} 个文件）"
            else:
                report.total_lines += part.total_lines
                report.parsed += part.parsed
                report.invalid += part.invalid
                report.duplicates += part.duplicates
                report.errors.extend(list(part.errors)[:3])
                for key, value in dict(part.by_protocol).items():
                    report.by_protocol[key] = report.by_protocol.get(key, 0) + value
            for node in nodes:
                key = node.pnode_endpoint_signature()
                if key in seen:
                    report.duplicates += 1
                    continue
                seen.add(key)
                all_nodes.append(node)
        if report is None:
            return [], self.psub_classifier.ptic_import_text("", pms_group)[1]
        return all_nodes, report

    # ------------------------------------------------------------------ 主流程
    @Slot()
    def run(self) -> None:  # noqa: D102
        result: dict[str, Any] = {
            "uid": self.psub_record.uid,
            "name": self.psub_record.psr_display_name(),
            "ok": False,
            "error": "",
            "added": 0,
            "updated": 0,
            "removed": 0,
            "node_count": 0,
            "traffic": {},
        }
        try:
            text, headers, error = self.psub_fetch()
            if error:
                result["error"] = error
                result["removed"] = self.psub_drop_previous_on_failure()
                self.psub_signals.psubs_task_done.emit(self.psub_record.uid, result)
                return
            if self.psub_no_new_content:
                # 增量模式没有新贴 / GitHub 仓库没有新提交：算成功的空更新，不解析、不落库、不清理
                note = self.psub_github_note or "该频道自上次抓取以来没有新贴"
                result.update({"ok": True, "note": note, "node_count": 0})
                self.psub_signals.psubs_task_done.emit(self.psub_record.uid, result)
                return
            if self.psub_finish_tor_only(result, text):
                return
            if not str(text).strip() and not self.psub_github_files:
                result["error"] = "订阅内容为空"
                result["removed"] = self.psub_drop_previous_on_failure()
                self.psub_signals.psubs_task_done.emit(self.psub_record.uid, result)
                return
            result["traffic"] = self.psub_parse_traffic_header(headers)
            logging.getLogger(PMS_LOGGER_NAME + ".subscription").debug(
                "订阅流量头原始内容（%s）：%s", self.psub_record.psr_display_name(), result["traffic"].get("raw_header", "")
            )
            if self.psub_telegram_stats:
                result["telegram"] = dict(self.psub_telegram_stats)
            if self.psub_github_stats:
                result["github"] = dict(self.psub_github_stats)
            group_name = self.psub_record.name or "订阅"
            nodes, report = self.psub_parse_content(text, group_name)
            for node in nodes:
                node.subscription_uid = self.psub_record.uid
                node.subscription_name = self.psub_record.name or "订阅"
                node.group_name = group_name
            cap = self.psub_node_cap()
            if cap and len(nodes) > cap:
                result["note"] = f"节点数超过上限 {cap}，本次只取前 {cap} 条（可在设置 → 订阅里调大）"
                nodes = nodes[:cap]
            merged = self.psub_merge_with_existing(nodes, report)
            if not merged and report.parsed == 0:
                result["error"] = "未解析到任何节点（内容格式不支持或全部无效）"
                result["removed"] = self.psub_drop_previous_on_failure()
                self.psub_signals.psubs_task_done.emit(self.psub_record.uid, result)
                return
            added, updated, removed = self.psub_apply_to_database(merged)
            result.update({
                "ok": True,
                "added": added,
                "updated": updated,
                "removed": removed,
                "node_count": len(merged),
                "report": {
                    "total_lines": report.total_lines,
                    "parsed": report.parsed,
                    "invalid": report.invalid,
                    "duplicates": report.duplicates,
                    "cross_deduped": self.psub_cross_matched,
                    "by_protocol": dict(report.by_protocol),
                    "errors": list(report.errors[:10]),
                },
            })
            if self.psub_github_note:
                # 与「节点数被截断」这类提示合并，别互相覆盖
                result["note"] = f"{result['note']}；{self.psub_github_note}" if result.get("note") else self.psub_github_note
            self.psub_attach_tor_payload(result)   # v1.9.44：混合频道（节点 + 桥接）两条线都走
        except Exception as exc:  # noqa: BLE001 - 任务线程绝不能让异常逃逸
            result["error"] = f"更新异常：{exc}"
            result["removed"] = self.psub_drop_previous_on_failure()
            logging.getLogger(PMS_LOGGER_NAME + ".subscription").exception("订阅更新失败：%s", exc)
        self.psub_signals.psubs_task_done.emit(self.psub_record.uid, result)

    def psub_drop_previous_on_failure(self) -> int:
        """更新失败时是否清掉该订阅的旧节点，返回删除数（v1.9.16）。

        设置项 `subscription.keep_previous_on_failure`（界面「更新失败时保留旧节点」）
        以前**没人读**：不管勾没勾，失败一律保留旧节点（因为失败路径直接 return，压根不碰库）。
        默认 True = 保留，与改动前完全一致；取消勾选才会在失败时清掉该订阅的旧节点。

        用户手动中止 / 程序退出不算「更新失败」，此时不动数据库。
        """
        if self.psub_is_aborted():
            return 0
        if self.psub_settings.psm_get_bool("subscription.keep_previous_on_failure", True):
            return 0
        stale = [
            node.uid
            for node in self.psub_repo.pnr_load_all_nodes()
            if node.subscription_uid == self.psub_record.uid
        ]
        if not stale:
            return 0
        removed = self.psub_repo.pnr_delete_nodes(stale)
        logging.getLogger(PMS_LOGGER_NAME + ".subscription").warning(
            "订阅「%s」更新失败，按设置清理了 %d 个旧节点", self.psub_record.psr_display_name(), removed
        )
        return removed

    def psub_build_existing_index(self) -> tuple[dict[str, PmsProxyNode], dict[str, PmsProxyNode], dict[str, PmsProxyNode], dict[str, PmsProxyNode]]:
        """构建库中已有节点的比对索引。

        返回 ``(own_exact, own_endpoint, other_exact, other_endpoint)``：
        - ``own_*``：归属**本订阅**的节点（精确签名 / 端点签名两级索引）；
        - ``other_*``：归属**其他订阅或手动添加**的节点，仅在开关
          ``subscription.dedup_across_subscriptions`` 打开时构建；关闭时为空字典，
          行为与 v1.9.1 之前完全一致。

        只全量加载一次节点表再分桶，避免每节点重复查库。
        """
        all_nodes = list(self.psub_repo.pnr_load_all_nodes())
        own_exact: dict[str, PmsProxyNode] = {}
        own_endpoint: dict[str, PmsProxyNode] = {}
        other_exact: dict[str, PmsProxyNode] = {}
        other_endpoint: dict[str, PmsProxyNode] = {}
        cross_enabled = self.psub_settings.psm_get_bool("subscription.dedup_across_subscriptions", True)
        for node in all_nodes:
            if node.subscription_uid == self.psub_record.uid:
                own_exact.setdefault(node.pnode_signature(), node)
                own_endpoint.setdefault(node.pnode_endpoint_signature(), node)
            elif cross_enabled:
                other_exact.setdefault(node.pnode_signature(), node)
                other_endpoint.setdefault(node.pnode_endpoint_signature(), node)
        return own_exact, own_endpoint, other_exact, other_endpoint

    def psub_merge_with_existing(self, pms_nodes: list[PmsProxyNode], pms_report: PmsImportReport) -> list[PmsProxyNode]:
        """与库中节点比对：保留原有 uid / 测试结果 / 启用状态，统计新增与更新。

        v1.9.1 两处改动：
        1. 比对范围由「仅同订阅」扩展为「同订阅优先 + 跨订阅兜底」——
           同一机场挂了多个订阅、或手动添加过同一节点时不再各存一份；
           跨订阅撞车时**以正在更新的订阅为准**（归属权转移给它，备注名用新的）。
        2. 宽松匹配由 ``startswith`` 改为端点签名**精确相等**：
           原写法会把端口 443 与 4430 误判为同一节点。
        """
        own_exact, own_endpoint, other_exact, other_endpoint = self.psub_build_existing_index()
        loose_dedupe = self.psub_settings.psm_get_bool("advanced.dedup_by_server_port", True)
        seen: dict[str, PmsProxyNode] = {}
        used_uids: set[str] = set()
        merged: list[PmsProxyNode] = []
        for node in pms_nodes:
            key = node.pnode_signature()
            if key in seen:
                pms_report.duplicates += 1
                continue
            seen[key] = node
            previous = own_exact.get(key)
            cross_matched = False
            if previous is None and loose_dedupe:
                previous = own_endpoint.get(node.pnode_endpoint_signature())
            if previous is None:
                previous = other_exact.get(key)
                cross_matched = previous is not None
            if previous is None and loose_dedupe:
                candidate = other_endpoint.get(node.pnode_endpoint_signature())
                if candidate is not None:
                    previous = candidate
                    cross_matched = True
            if previous is not None:
                # 同一个已有节点被多个新节点命中时只保留第一个，避免写出重复 uid。
                if previous.uid in used_uids:
                    pms_report.duplicates += 1
                    continue
                used_uids.add(previous.uid)
                node.uid = previous.uid
                node.created_at = previous.created_at
                node.latency_ms = previous.latency_ms
                node.speed_bps = previous.speed_bps
                node.last_test_time = previous.last_test_time
                node.last_test_mode = previous.last_test_mode
                node.success_count = previous.success_count
                node.fail_count = previous.fail_count
                node.enabled = previous.enabled
                # v1.9.59：电报 DC 成绩与延迟/测速同口径——订阅刷新不能把它洗成「未测」
                node.telegram_ok = previous.telegram_ok
                node.telegram_best_ms = previous.telegram_best_ms
                node.telegram_best_dc = previous.telegram_best_dc
                node.telegram_reachable = previous.telegram_reachable
                node.telegram_tcp_count = previous.telegram_tcp_count
                node.telegram_total = previous.telegram_total
                node.telegram_speed_bps = previous.telegram_speed_bps
                node.telegram_time = previous.telegram_time
                node.telegram_note = previous.telegram_note
                if cross_matched:
                    # 跨订阅撞车：以本订阅的数据为准，备注名用新的（旧的仅作兜底）
                    node.remark = node.remark or previous.remark
                    self.psub_cross_matched += 1
                else:
                    # 同订阅命中：保住用户可能手动改过的备注名
                    node.remark = previous.remark or node.remark
            node.updated_at = pms_now_text()
            merged.append(node)
        if self.psub_settings.psm_get_bool("subscription.remove_duplicates", True):
            deduped: list[PmsProxyNode] = []
            final_seen: set[str] = set()
            for node in merged:
                key = node.pnode_endpoint_signature() if loose_dedupe else node.pnode_signature()
                if key in final_seen:
                    pms_report.duplicates += 1
                    continue
                final_seen.add(key)
                deduped.append(node)
            merged = deduped
        for index, node in enumerate(merged):
            node.sort_index = index
        return merged

    def psub_apply_to_database(self, pms_nodes: list[PmsProxyNode]) -> tuple[int, int, int]:
        """写入数据库：新增 / 更新 / 删除已消失节点。

        v1.9.1：新增 / 更新的判定基准由「本订阅已有 uid」改为「全库已知 uid」——
        跨订阅复用 uid 的节点应算**更新**而非新增，否则统计会虚高。
        清理范围仍限定为本订阅原有的节点，不会误删其他订阅 / 手动添加的节点。
        """
        all_nodes = list(self.psub_repo.pnr_load_all_nodes())
        known_uids = {node.uid for node in all_nodes}
        owned_uids = {node.uid for node in all_nodes if node.subscription_uid == self.psub_record.uid}
        incoming_uids = {node.uid for node in pms_nodes}
        added = len([uid for uid in incoming_uids if uid not in known_uids])
        updated = len(incoming_uids) - added
        self.psub_repo.pnr_upsert_nodes(pms_nodes)
        removed = 0
        # v1.9.2：增量模式（psub_allow_prune=False）不清理，否则旧节点会被当成已消失而删光
        if self.psub_allow_prune and self.psub_settings.psm_get_bool("subscription.prune_removed", True):
            stale = [uid for uid in owned_uids if uid not in incoming_uids]
            if stale:
                removed = self.psub_repo.pnr_delete_nodes(stale)
        return added, updated, removed

class PmsSubscriptionService(_PsubBridgeMixin, QObject):
    """批量订阅更新调度器（多线程，带进度）。"""

    psu_progress = Signal(int, int, str)          # 已完成, 总数, 名称
    psu_subscription_done = Signal(str, dict)     # uid, 结果
    psu_finished = Signal(dict)                   # 汇总
    psu_message = Signal(str)

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_repo: PmsNodeRepository,
        pms_env: PmsEnvStore,
        pms_classifier: PmsTextImportClassifier,
        pms_parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.psu_settings = pms_settings
        self.psu_repo = pms_repo
        self.psu_env = pms_env
        self.psu_classifier = pms_classifier
        self.psu_logger = logging.getLogger(PMS_LOGGER_NAME + ".subscription")
        self.psu_signals = PmsSubscriptionSignals(self)
        self.psu_signals.psubs_task_done.connect(self.psu_on_task_done)
        self.psu_pool = QThreadPool(self)
        self.psu_pool.setMaxThreadCount(4)
        self.psu_running = False
        self.psu_total = 0
        self.psu_done = 0
        self.psu_names: dict[str, str] = {}
        self.psu_results: dict[str, dict] = {}
        self.psu_tor_added: dict[str, int] = {}   # v1.9.44：每个订阅本轮写进桥接列表的**新增**条数
        self.psu_local_proxy_port = 0
        # v1.9.9：本轮派发的订阅任务。停止时逐个打断——订阅抓取最多等 25 秒，
        # 不打断的话线程池线程一直挂着，Python 进程退不出去。
        self.psu_tasks: set[Any] = set()
        self.psu_tasks_lock = threading.RLock()

    # ------------------------------------------------------------------ 任务登记（v1.9.9）
    def psu_register_task(self, pms_task: Any) -> None:
        """派发时登记任务（每轮开始会先清空，不会无限增长）。"""
        with self.psu_tasks_lock:
            self.psu_tasks.add(pms_task)

    def psu_clear_tasks(self) -> None:
        """一轮结束 / 新一轮开始时清空登记。"""
        with self.psu_tasks_lock:
            self.psu_tasks.clear()

    def psu_abort_tasks(self) -> int:
        """打断所有已派发的订阅任务，返回打断的个数。"""
        with self.psu_tasks_lock:
            tasks = list(self.psu_tasks)
        for task in tasks:
            try:
                task.psub_abort()
            except Exception:  # noqa: BLE001 - 退出路径上单个任务失败不能连累其余
                continue
        return len(tasks)

    def psu_is_running(self) -> bool:
        return self.psu_running

    def psu_set_local_proxy_port(self, pms_port: int) -> None:
        """设置本地代理端口（订阅走代理更新时使用）。"""
        self.psu_local_proxy_port = int(pms_port or 0)

    def psu_start(self, pms_records: Sequence[PmsSubscriptionRecord], pms_fetch_mode: str = "") -> tuple[bool, str]:
        """开始更新给定订阅列表。

        `pms_fetch_mode` 为 v1.9.3 的一次性模式覆盖（full / incremental），空串表示沿用设置。
        只对电报频道型订阅有意义；普通 HTTP 订阅两种模式等价（都是重新抓一遍）。
        """
        if self.psu_running:
            return False, "订阅更新已在进行中"
        records = [record for record in pms_records if str(record.url or "").strip()]
        if not records:
            return False, "没有配置了地址的订阅"
        mode = str(pms_fetch_mode or "").strip().lower()
        if mode and mode not in PMS_TELEGRAM_FETCH_MODES:
            mode = ""
        self.psu_running = True
        self.psu_total = len(records)
        self.psu_done = 0
        self.psu_results = {}
        self.psu_tor_added = {}
        self.psu_tor_added = {}   # v1.9.44：每轮清空桥接新增计数（否则汇总会带上一轮的数字）
        self.psu_names = {record.uid: record.psr_display_name() for record in records}
        self.psu_clear_tasks()          # v1.9.9：本轮重新登记，避免带着上一轮的僵尸任务
        label = {"full": "全量", "incremental": "增量"}.get(mode, "")
        self.psu_message.emit(f"开始{label}更新 {self.psu_total} 个订阅（并发 4）")
        for record in records:
            task = PmsSubscriptionUpdateTask(
                self.psu_signals, self.psu_settings, self.psu_repo, record, self.psu_classifier, self.psu_env,
                self.psu_local_proxy_port, mode,
            )
            self.psu_register_task(task)
            self.psu_pool.start(task)
        return True, f"已开始{label}更新 {self.psu_total} 个订阅"

    def psu_on_task_done(self, pms_uid: str, pms_result: dict) -> None:
        """单个订阅完成。"""
        self.psu_done += 1
        self.psu_results[str(pms_uid)] = dict(pms_result)
        name = str(pms_result.get("name") or self.psu_names.get(str(pms_uid), ""))
        self.psu_apply_tor_bridges(str(pms_uid), pms_result.get("tor_bridges") or {})   # v1.9.44，主线程落盘
        self.psu_subscription_done.emit(str(pms_uid), dict(pms_result))
        if pms_result.get("ok"):
            text = (
                f"订阅「{name}」更新完成：新增 {pms_result.get('added', 0)}，更新 {pms_result.get('updated', 0)}，"
                f"移除 {pms_result.get('removed', 0)}，共 {pms_result.get('node_count', 0)} 个节点"
            )
            if pms_result.get("note"):
                text += f"（{pms_result.get('note')}）"
            self.psu_message.emit(text)
        else:
            self.psu_message.emit(f"订阅「{name}」更新失败：{pms_result.get('error') or '未知错误'}")
        self.psu_progress.emit(self.psu_done, self.psu_total, name)
        if self.psu_done >= self.psu_total:
            self.psu_finish()

    def psu_finish(self) -> None:
        self.psu_running = False
        self.psu_clear_tasks()      # v1.9.9：本轮已结束，登记可以清掉
        summary = {
            "total": self.psu_total,
            "success": sum(1 for item in self.psu_results.values() if item.get("ok")),
            "failed": sum(1 for item in self.psu_results.values() if not item.get("ok")),
            "added": sum(pms_safe_int(item.get("added"), 0) for item in self.psu_results.values()),
            "updated": sum(pms_safe_int(item.get("updated"), 0) for item in self.psu_results.values()),
            "removed": sum(pms_safe_int(item.get("removed"), 0) for item in self.psu_results.values()),
        }
        self.psu_tor_summary_into(summary)   # v1.9.44：桥接源数量（节点数恒为 0，必须单独体现）
        self.psu_finished.emit(summary)

    def psu_stop(self) -> None:
        if not self.psu_running:
            return
        self.psu_running = False
        # v1.9.9：只 clear() 取消的是「排队中」的任务，已经在跑的不会停。
        # 先逐个打断（关掉正在读的响应 + 置标志），再等线程池收尾。
        aborted = self.psu_abort_tasks()
        self.psu_pool.clear()
        if aborted and not self.psu_pool.waitForDone(PMS_EXIT_POOL_WAIT_MS):
            logging.getLogger(PMS_LOGGER_NAME + ".subscription").warning(
                "订阅更新线程池在 %d ms 内未收尾（已打断 %d 个任务）", PMS_EXIT_POOL_WAIT_MS, aborted,
            )
        self.psu_clear_tasks()
        self.psu_message.emit("已请求停止订阅更新（正在执行的请求会自行结束）")
        self.psu_finished.emit({"total": self.psu_total, "success": 0, "failed": self.psu_total, "added": 0, "updated": 0, "removed": 0, "tor_added": 0, "tor_sources": 0})
