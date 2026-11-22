#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试断点续跑：未完轮次（round）的数据与持久化（v1.8.0）

延迟测试（`pms/latency.py`）与测速（`pms/speedtest.py`）共用这一套断点逻辑：

    plt_start / pst_start   →  _ptstore_save（建轮次：pending = 全部待测 uid）
    每个节点完成             →  _ptround_mark_done（出队；失败按 retry_max 回队重试）
    停止 / 退出 / 崩溃       →  _ptstore_save（把剩下的 pending 写盘 = 断点）
    下次点「继续上次测试」    →  _ptstore_load → 只跑 pending 里的，不重测已完成

★ 为什么本模块的对外符号全部私有（下划线开头）
    `_scaffold/check_modules.py` 强制「启动器必须再导出包内全部公开类与模块级函数」，
    而 `Proxy_Manager_Studio.py` 自 v1.7.0 起正好 400 行、压在红线上。
    新增任何公开类 / 模块级函数都会直接破线，所以这里一律私有化——
    与 v1.6.7 把 `psc_utls_disabled` 改名为 `_psc_utls_disabled` 是同一套办法。

★ 为什么仍是 `pms_meta` 多键 KV 而不是新建表（v1.9.54，审计表第 19 项）
    v1.8.0 的代价是「单条 KV 只支持**一个**未完轮次」：延迟测到一半去测速，
    延迟那轮的断点就被直接冲掉（老用户的断点说没就没）。v1.9.54 改成**多轮并存**：

        test.round.<轮次id>   每轮一条 JSON（与 v1.8.0 的单条记录**逐字段同构**）
        test.round.index      轮次索引（只记 id / 时间 / 进度，列清单时不必解析大 JSON）

    仍然复用 `pdb_set_meta` / `pdb_get_meta`：不动数据库 schema、不用升 `schema_version`，
    与 telegram 的 `telegram.last_id.<频道名>` 是同一套路子（宪法第 20 条）。

★ 旧格式一定要能读（硬要求）
    老版本只写 `test.pending_round` 一条。它**继续原样保留**，只是不再往里写：
    读的时候一旦发现它还在，就当一条普通轮次读出来，并在同一把锁内先写新键、后清旧键——
    中途崩也不会把老用户的断点弄丢。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
)

from pms.constants import PMS_LOGGER_NAME

if TYPE_CHECKING:  # 仅类型检查，运行时不导入，避免循环依赖
    from pms.database import PmsDatabaseEngine

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------
# 断点存在 pms_meta 里的键名。
# ★ v1.9.54：`PMS_TEST_ROUND_META_KEY` 变成**旧格式兼容键**——只读不写，读到就迁移
#   （老用户升级上来的第一条断点，绝不能丢）。
PMS_TEST_ROUND_META_KEY = "test.pending_round"

# v1.9.54（审计表第 19 项）：多轮并存的键名。每轮一条 `test.round.<轮次id>`，
# 再配一条只读索引 `test.round.index`——索引里只有 id / 时间 / 进度，
# 列「有哪些断点」时不必把每轮的 pending 大 JSON 全解析一遍。
# 用独立前缀而不是共用旧键，是为了**不改数据库 schema、不用写迁移脚本**：
# `pms_meta` 本来就是 KV 表，多键并存天然支持（与 `telegram.last_id.<频道名>` 同一套路）。
PMS_TEST_ROUND_KEY_PREFIX = "test.round."
PMS_TEST_ROUND_INDEX_KEY = "test.round.index"
PMS_TEST_ROUND_JSON_ENTRY_LIMIT = 64   # 索引与轮次 id 之外的兜底上限（防脏数据堆成列表洪水）

# v1.9.54：同时最多保留几轮未完断点。**只在这里**定义（不建成设置项）——
# constants.py / settings_defaults.py 都顶在 1000 行红线，多一个设置键就得再挤一行；
# 而「留几轮断点」是内部维护参数，不是用户要调的开关。
PMS_TEST_ROUND_LIST_MAX = 5

# 单个节点最多重试几次（失败后回队）。**刻意不做成设置项**：constants.py 已顶在
# 1000 行红线（v1.7.0 起余量为 0），多加一个默认键就得再挤一行。
PMS_TEST_ROUND_RETRY_MAX = 1

# 断点保留上限：超过这个小时数视为过期垃圾，读出来直接丢弃（避免软件几天没开，
# 一启动就提示「继续上次测试」，而那批节点早就不存在了）。
PMS_TEST_ROUND_MAX_AGE_HOURS = 72

# 单个轮次最多记多少个 uid，防止极端情况下 JSON 无限膨胀。
# v1.9.27：原来这里是 20000，于是「4 万节点」这种量级的测试**只能记到前 2 万个**，
# 而调用方一看到 pending 清空就收工 → 后 2 万个节点根本没测（真机复现过）。
# 现在它只是「防御性上限」而不是功能上限，并且一旦真的截断会**大声告警**，不再静默出错。
PMS_TEST_ROUND_MAX_UIDS = 200000

# 断点落盘节流（v1.9.27）：测试中每完成一个节点都会请求写一次断点，
# 4 万节点时那等于把约 2 MB 的 JSON 写 4 万遍——实测单次 41 ms / 820 KB，
# 2 万次就是 825 秒、15.6 GB 纯磁盘 I/O，比测试本身还慢。
# 改成「够久或够多才写」：崩溃保护基本不变（结果本身早已逐条写进 pms_nodes，
# 断点只记「还剩谁没测」，丢一点最多重测几十个节点），但写盘次数降到几百次。
PMS_TEST_ROUND_SAVE_INTERVAL_S = 60.0    # 距上次落盘超过这么久就写一次（很慢的测试也兜得住）
PMS_TEST_ROUND_SAVE_MAX_SKIP = 1000      # 或者攒够这么多节点没写就写一次（很快的测试也兜得住）

_logger = logging.getLogger(PMS_LOGGER_NAME + ".testresume")


def _pms_now_text() -> str:
    """当前时间文本（与日志同一格式）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pms_round_age_hours(pms_created_at: str) -> float:
    """断点已经放了几个小时；时间文本解析不出来时返回 -1（调用方按「不过期」处理）。"""
    text = str(pms_created_at or "").strip()
    if not text:
        return -1.0
    try:
        created = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return -1.0
    return (datetime.now() - created).total_seconds() / 3600.0


def _pms_round_is_stale(pms_round: "_PmsTestRound") -> bool:
    """这一轮的创建时间是不是已经超过 `PMS_TEST_ROUND_MAX_AGE_HOURS`（超龄垃圾）。

    ★ 沿用 v1.8.0 的既有常量和口径：时间文本解析不出来时 `_pms_round_age_hours()` 返回 -1，
    按「不过期」处理（绝不因为一个格式怪的时间戳就把用户的断点丢掉）。
    """
    age = _pms_round_age_hours(pms_round.created_at)
    return 0 <= age > PMS_TEST_ROUND_MAX_AGE_HOURS


class _PmsTestRound:
    """一轮未完的测试（断点）。

    ``done`` + ``pending`` 互斥且共同构成本轮的全部目标：
        · pending：还没测（或失败待重试）的 uid，**续跑只跑这些**
        · done：已完成的 uid，续跑时跳过，也用于「中途新增节点自动补测」时判断谁是新的
    """

    __slots__ = ("round_id", "kind", "include_speed", "scope", "created_at", "total", "done", "pending", "failed", "retry_max")

    def __init__(
        self,
        pms_kind: str = "latency",
        pms_include_speed: bool = False,
        pms_scope: str = "all",
        pms_uids: Optional[list[str]] = None,
        pms_retry_max: int = PMS_TEST_ROUND_RETRY_MAX,
    ) -> None:
        self.round_id = uuid.uuid4().hex          # v1.9.54：轮次 id（多轮并存的键名靠它）
        self.kind = str(pms_kind or "latency")
        self.include_speed = bool(pms_include_speed)
        self.scope = str(pms_scope or "all")
        self.created_at = _pms_now_text()
        targets = [str(uid) for uid in (pms_uids or [])]
        self.total = len(targets)
        self.done: list[str] = []
        self.pending: list[str] = targets[:PMS_TEST_ROUND_MAX_UIDS]
        self.failed: dict[str, int] = {}
        self.retry_max = max(0, int(pms_retry_max))
        if len(targets) > len(self.pending):
            # v1.9.27：绝不允许「静默少测」——真撞上防御上限时必须留下痕迹
            _logger.error(
                "本轮目标节点 %d 个，超过断点上限 %d，只跟踪前 %d 个（剩下的仍会被测，但不参与断点续跑）",
                len(targets), PMS_TEST_ROUND_MAX_UIDS, len(self.pending),
            )

    # ---------------------------------------------------------------- 序列化
    def _ptround_to_dict(self) -> dict[str, Any]:
        """★ 字段清单与 v1.8.0 **逐字段一致**（只多一个轮次 id）。

        多出来的 `round_id` 是 v1.9.54 加的：它是 `test.round.<id>` 的键名，
        `_ptround_from_dict()` 会读它、`_ptstore` 会按它归位；除此之外一个字段都没动，
        所以「单轮语义与改动前逐字段一致」这条验收能靠探针直接比对字典。
        """
        return {
            "round_id": self.round_id,
            "kind": self.kind,
            "include_speed": self.include_speed,
            "scope": self.scope,
            "created_at": self.created_at,
            "total": self.total,
            "done": self.done,
            "pending": self.pending,
            "failed": self.failed,
            "retry_max": self.retry_max,
        }

    @classmethod
    def _ptround_from_dict(cls, pms_data: Any) -> Optional["_PmsTestRound"]:
        """从字典还原；结构不对 / 缺字段一律返回 None（**绝不抛异常**，脏数据直接当没断点）。

        v1.9.54：多读一个 `round_id`（旧格式没有它 → 自动生成一个），
        其余字段的解析口径与 v1.8.0 逐字保持不变。
        """
        if not isinstance(pms_data, dict):
            return None
        pending = pms_data.get("pending")
        if not isinstance(pending, list):
            return None
        done = pms_data.get("done")
        failed = pms_data.get("failed")
        round_obj = cls(
            pms_kind=str(pms_data.get("kind") or "latency"),
            pms_include_speed=bool(pms_data.get("include_speed")),
            pms_scope=str(pms_data.get("scope") or "all"),
            pms_uids=[str(uid) for uid in pending],
            pms_retry_max=int(pms_data.get("retry_max") or PMS_TEST_ROUND_RETRY_MAX),
        )
        round_obj.round_id = str(pms_data.get("round_id") or round_obj.round_id)
        round_obj.created_at = str(pms_data.get("created_at") or _pms_now_text())
        round_obj.total = int(pms_data.get("total") or (len(pending) + len(done if isinstance(done, list) else [])))
        round_obj.done = [str(uid) for uid in done] if isinstance(done, list) else []
        round_obj.failed = {str(k): int(v) for k, v in failed.items()} if isinstance(failed, dict) else {}
        return round_obj

    # ---------------------------------------------------------------- 状态
    def _ptround_is_finished(self) -> bool:
        return not self.pending

    def _ptround_done_count(self) -> int:
        return len(self.done)

    def _ptround_pending_count(self) -> int:
        return len(self.pending)

    def _ptround_retry_count(self) -> int:
        """当前排队重试的次数（失败回队的节点数），用于进度条文案。"""
        return sum(1 for uid in self.pending if self.failed.get(uid, 0) > 0)

    def _ptround_info(self) -> dict[str, Any]:
        """给界面用的快照（避免把轮次对象直接漏到 UI 层）。"""
        return {
            "round_id": self.round_id,      # v1.9.54：多轮并存时靠它区分「丢哪一轮」
            "kind": self.kind,
            "scope": self.scope,
            "created_at": self.created_at,
            "total": self.total,
            "pending": self._ptround_pending_count(),
            "retry": self._ptround_retry_count(),
            "include_speed": self.include_speed,
        }

    def _ptround_key(self) -> str:
        """本轮的 `pms_meta` 键名（v1.9.54：一轮一条，互不覆盖）。"""
        return PMS_TEST_ROUND_KEY_PREFIX + str(self.round_id)

    def _ptround_index_entry(self) -> dict[str, Any]:
        """索引里那一行：只放「列清单 / 开界面」需要的轻量字段，不放 done / pending 大列表。"""
        return {
            "round_id": self.round_id,
            "kind": self.kind,
            "scope": self.scope,
            "total": int(self.total),
            "done": self._ptround_done_count(),
            "pending": self._ptround_pending_count(),
            "retry": self._ptround_retry_count(),
            "include_speed": self.include_speed,
            "created_at": self.created_at,
        }

    def _ptround_mark_done(self, pms_uid: str, pms_success: bool) -> bool:
        """标记一个节点完成，返回 True 表示它被**重新排进了队尾**（失败重试）。

        成功：从 pending 移除 → 进 done，并清掉失败计数。
        失败：从 pending 移除 → 进 done；只要失败次数还没到 retry_max 就回队尾重测。
        """
        uid = str(pms_uid)
        if uid in self.pending:
            self.pending.remove(uid)
        if uid not in self.done:
            self.done.append(uid)
        if pms_success:
            self.failed.pop(uid, None)
            return False
        count = int(self.failed.get(uid, 0)) + 1
        self.failed[uid] = count
        if count <= self.retry_max:
            self.pending.append(uid)
            return True
        return False

    def _ptround_add_new(self, pms_uids: list[str]) -> int:
        """把「新的」uid 补进 pending（中途新增节点自动补测）。

        只补既不在 pending 也不在 done 的（已测过的不重测）。返回补了几个。
        """
        known = set(self.pending) | set(self.done)
        added = 0
        for uid in pms_uids:
            uid = str(uid)
            if uid in known:
                continue
            if len(self.pending) >= PMS_TEST_ROUND_MAX_UIDS:
                break
            self.pending.append(uid)
            known.add(uid)
            added += 1
        if added:
            self.total += added
        return added

    def _ptround_drop_missing(self, pms_valid_uids: set[str]) -> int:
        """把已经不存在的 uid（被删掉的节点）从 pending 里剔除，返回剔除数。

        total 同步减掉——否则进度条分母虚高，会出现「已完成 90 / 总数 100，
        但剩下 0 个待测」这种对不上的显示。
        """
        before = len(self.pending)
        self.pending = [uid for uid in self.pending if uid in pms_valid_uids]
        self.done = [uid for uid in self.done if uid in pms_valid_uids]
        removed = before - len(self.pending)
        self.total = max(0, self.total - removed)
        return removed


class _PmsTestRoundStore:
    """断点的读写与清理（v1.9.54：`pms_meta` 里**每轮一条** KV，多轮并存互不覆盖）。

    ★ 线程：`pdb_set_meta` / `pdb_get_meta` 内部走 `pdb_lock`（可重入锁），但「读索引 → 写本轮 →
      清旧键」这一串必须**整体原子**，否则两个调度器同时开测会互相把对方的索引覆盖掉。
      所以这里自己再加一把 `threading.RLock` 把整串操作包起来（与 telegram 的写入口径一致）。
    """

    def __init__(self, pms_engine: "PmsDatabaseEngine") -> None:
        self._ptstore_engine = pms_engine
        self._ptstore_last_save_at = 0.0
        self._ptstore_skipped = 0
        self._ptstore_lock = threading.RLock()

    # ---------------------------------------------------------------- 内部：KV 小工具
    def _ptstore_own_kind(self) -> str:
        """这个 store 归哪一类测试——**由调用方调度器在构造后设置**（v1.9.59）。

        ★ 为什么需要：`_ptstore_scan_rounds()` 扫的是 `test.round.*` **全部**前缀，
        延迟 / 测速 / 电报三个调度器各持一个 store 实例，不设归属就会互相读到
        对方的断点（`pmw_test_round_list()` 把同一个延迟轮次列三遍就是这个原因）。
        空串 = 不设归属（兼容历史：v1.9.54 的探针与旧代码都当「全量」用）。
        """
        return str(getattr(self, "_ptstore_kind", "") or "")

    def _ptstore_take_live(self) -> list[_PmsTestRound]:
        """本类调度器**该管的**那些未完轮次（旧 → 新）。

        归属过滤在这里做，并且**只影响读取侧**：清理 / 淘汰 / 索引写回仍然走全量视图，
        否则一个 store 只会看见自己的轮次，`_ptstore_write_index()` 就会把别的测试的
        索引项抹掉（延迟在跑、电报的断点凭空消失，是最难查的那种 bug）。
        """
        kind = self._ptstore_own_kind()
        live = self._ptstore_gather_live()
        return live if not kind else [item for item in live if item.kind == kind]

    def _ptstore_gather_live(self) -> list[_PmsTestRound]:
        """**不加锁**地把「全部还有用的未完轮次」读出来（调用方必须已持有 `_ptstore_lock`）。

        顺序即优先级：索引里的顺序是「旧 → 新」，所以**列表末尾就是最新一轮**，
        「继续上次测试」取的就是它。索引里认不出 / 已脏 / 已超龄的轮次会**顺手被清掉**，
        绝不静默留垃圾。
        """
        rounds = self._ptstore_read_index()
        entries = self._ptstore_read_legacy()
        for item in entries:
            if all(existing.round_id != item.round_id for existing in rounds):
                rounds.append(item)
        live: list[_PmsTestRound] = []
        retired: list[str] = []
        for item in rounds:
            if item._ptround_is_finished():
                retired.append(item.round_id)
                continue
            age = _pms_round_age_hours(item.created_at)
            if 0 <= age > PMS_TEST_ROUND_MAX_AGE_HOURS:
                # 超龄 = 过期垃圾（软件几天没开，那批节点早就不在了），读出来直接丢
                retired.append(item.round_id)
                continue
            live.append(item)
        if retired:
            self._ptstore_forget(retired)
        if len(live) > PMS_TEST_ROUND_LIST_MAX:
            # 超出上限：**淘汰最旧的**（列表前面的是旧的）
            dropped = self._ptstore_drop_oldest(live)
            if dropped:
                _logger.info("未完断点超过上限 %d 个，已淘汰最旧的 %d 轮：%s",
                             PMS_TEST_ROUND_LIST_MAX, len(dropped), "、".join(dropped))
        return live

    def _ptstore_drop_oldest(self, pms_rounds: list[_PmsTestRound]) -> list[str]:
        """**不加锁**：留下最新 N 轮，把更旧的连索引带 KV 一起删掉，返回被删的轮次 id。"""
        excess = len(pms_rounds) - PMS_TEST_ROUND_LIST_MAX
        if excess <= 0:
            return []
        dropped = [item.round_id for item in pms_rounds[:excess]]
        self._ptstore_forget(dropped)
        del pms_rounds[:excess]
        return dropped

    def _ptstore_read_index(self) -> list[_PmsTestRound]:
        """**不加锁**：按索引把每轮读出来；索引里没有但 KV 还活着的轮次也会被捞回来（自愈）。"""
        engine = self._ptstore_engine
        ids = self._ptstore_index_ids()
        by_id = {item.round_id: item for item in self._ptstore_scan_rounds()}
        rounds: list[_PmsTestRound] = []
        order: list[str] = []
        for round_id in ids:
            item = by_id.pop(round_id, None)
            if item is None:
                continue        # 索引里的孤儿（KV 已被清）→ 下面会顺手从索引里剔掉
            rounds.append(item)
            order.append(round_id)
        # KV 里存在、索引里漏了的（旧版升级残留 / 写索引失败）→ 补进列表并补回索引
        for round_id, item in list(by_id.items()):
            rounds.append(item)
            order.append(round_id)
        if order != ids:
            # 自愈：把真实存在的轮次顺序写回索引（孤儿出局、漏网的进来）
            self._ptstore_write_index(rounds)
            _logger.warning("断点索引与实际记录不一致，已自愈重建（%d 条 → %d 条）", len(ids), len(order))
        return rounds

    def _ptstore_read_legacy(self) -> list[_PmsTestRound]:
        """**不加锁**：读 v1.8.0 的单条旧格式断点。

        ★ 这是「老用户断点绝不能丢」的那条硬要求：旧格式照原样能读，
        读出来就当一条普通轮次用，并在**同一把锁内**先写新键、后清旧键地迁移。
        """
        engine = self._ptstore_engine
        try:
            raw = engine.pdb_get_meta(PMS_TEST_ROUND_META_KEY, "")
        except Exception as exc:  # noqa: BLE001 - 数据库异常一律按「没断点」处理，不能拖垮启动
            _logger.warning("读取旧格式断点失败（按无断点处理）：%s: %s", type(exc).__name__, exc)
            return []
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            self._ptstore_clear_legacy()
            return []
        round_obj = _PmsTestRound._ptround_from_dict(data)
        if round_obj is None or round_obj._ptround_is_finished() or _pms_round_is_stale(round_obj):
            # 旧格式本身就是坏的 / 已跑完 / 已超龄 → 按 v1.8.0 的老口径当作没有断点
            self._ptstore_clear_legacy()
            return []
        self._ptstore_migrate_legacy(round_obj)
        return [round_obj]

    def _ptstore_write_index(self, pms_rounds: list[_PmsTestRound]) -> None:
        """**不加锁**：把轮次清单写进索引（只有轻量字段，不搬 pending 大列表）。"""
        engine = self._ptstore_engine
        if engine is None:
            return
        if not pms_rounds:
            engine.pdb_set_meta(PMS_TEST_ROUND_INDEX_KEY, "")
            return
        payload = [item._ptround_index_entry() for item in pms_rounds][-PMS_TEST_ROUND_JSON_ENTRY_LIMIT:]
        engine.pdb_set_meta(PMS_TEST_ROUND_INDEX_KEY, json.dumps(payload, ensure_ascii=False))

    def _ptstore_index_ids(self) -> list[str]:
        """**不加锁**：索引里的轮次 id 顺序（旧 → 新）。索引坏掉时返回空表，由自愈补回来。"""
        engine = self._ptstore_engine
        try:
            raw = engine.pdb_get_meta(PMS_TEST_ROUND_INDEX_KEY, "")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("读取断点索引失败（按空索引处理）：%s: %s", type(exc).__name__, exc)
            return []
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            _logger.warning("断点索引不是合法 JSON，已按空索引处理（会由自愈重建）")
            return []
        if not isinstance(data, list):
            return []
        ids: list[str] = []
        for entry in data[:PMS_TEST_ROUND_JSON_ENTRY_LIMIT]:
            round_id = ""
            if isinstance(entry, dict):
                round_id = str(entry.get("round_id") or "").strip()
            if round_id and round_id not in ids:
                ids.append(round_id)
        return ids

    def _ptstore_set_meta(self, pms_key: str, pms_value: str) -> bool:
        """**不加锁**：写一条 KV；数据库任何异常都只告警，绝不让测试本身崩掉。"""
        engine = self._ptstore_engine
        if engine is None:
            return False
        try:
            engine.pdb_set_meta(pms_key, pms_value)
            return True
        except Exception as exc:  # noqa: BLE001 - 写盘失败不能影响测试继续跑
            _logger.warning("断点写盘失败（%s）：%s: %s", pms_key, type(exc).__name__, exc)
            return False

    def _ptstore_forget(self, pms_round_ids: list[str]) -> None:
        """**不加锁**：按轮次 id 删（清空 KV + 从索引里剔掉）。"""
        if not pms_round_ids:
            return
        for round_id in pms_round_ids:
            self._ptstore_set_meta(PMS_TEST_ROUND_KEY_PREFIX + str(round_id), "")
        self._ptstore_write_index([item for item in self._ptstore_scan_rounds()
                                   if item.round_id not in set(pms_round_ids)])

    def _ptstore_forget_kind(self, pms_kind: str, pms_exclude: str = "") -> None:
        """**不加锁**：把某一类的未完断点**全部**丢进回收站（`pms_exclude` = 保留这一轮）。

        v1.9.54：同类型（延迟 / 测速）各自只留**最新一轮**——多轮并存是为了「延迟和测速
        各留各的」，不是让同一类测试堆 5 轮（那样 4 轮都是过期现场，用户反而挑不清）。
        """
        kind = str(pms_kind or "")
        if not kind:
            return
        victims = [item.round_id for item in self._ptstore_scan_rounds()
                   if item.kind == kind and item.round_id != str(pms_exclude or "")]
        if victims:
            self._ptstore_forget(victims)

    def _ptstore_scan_rounds(self) -> list[_PmsTestRound]:
        """**不加锁**：把 `test.round.*` 前缀的 KV 全部读出来（不解析索引、不做清理）。

        索引丢了 / 被写坏时靠它自愈；`pdb_query` 在引擎上只是读（HTTP 线程不碰这里）。
        """
        engine = self._ptstore_engine
        if engine is None:
            return []
        try:
            rows = engine.pdb_query(
                "SELECT meta_key, meta_value FROM pms_meta WHERE meta_key LIKE ? ORDER BY meta_key",
                (PMS_TEST_ROUND_KEY_PREFIX + "%",),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("扫描断点 KV 失败（按无断点处理）：%s: %s", type(exc).__name__, exc)
            return []
        rounds: list[_PmsTestRound] = []
        for row in rows:
            raw = str(row["meta_value"] or "")
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue        # 脏 KV 留给上面的超龄 / 完成判定去清，这里只跳过
            item = _PmsTestRound._ptround_from_dict(data)
            if item is None:
                continue
            key_id = str(row["meta_key"])[len(PMS_TEST_ROUND_KEY_PREFIX):]
            if key_id:
                item.round_id = key_id        # **键名就是权威 id**，内容里的 round_id 只当备份
            rounds.append(item)
        rounds.sort(key=lambda item: str(item.created_at))     # 旧 → 新，末尾是最新
        return rounds

    def _ptstore_migrate_legacy(self, pms_round: _PmsTestRound) -> bool:
        """**不加锁**：把 v1.8.0 的单条旧断点迁成一条多轮记录（先写新、后清旧）。

        顺序刻意这样：新键写成功了才清旧键。中途崩 / 断电最多留下「新旧各一份」，
        读到两份时会清掉**多出来的那一份**（id 去重），**绝不会两边都没有**。
        """
        if not self._ptstore_set_meta(pms_round._ptround_key(),
                                      json.dumps(pms_round._ptround_to_dict(), ensure_ascii=False)):
            return False        # 新键没写成功 → 旧键一个字都不动，下次再试
        existing = [item for item in self._ptstore_scan_rounds() if item.round_id != pms_round.round_id]
        self._ptstore_write_index(existing + [pms_round])
        self._ptstore_clear_legacy()
        _logger.info("已把 v1.8.0 的单条断点（%s，剩 %d 个）迁移成多轮记录 %s",
                     pms_round.kind, pms_round._ptround_pending_count(), pms_round.round_id)
        return True

    def _ptstore_clear_legacy(self) -> None:
        """**不加锁**：清掉旧格式那条 KV（只在新格式接管之后调用）。"""
        self._ptstore_set_meta(PMS_TEST_ROUND_META_KEY, "")

    # ---------------------------------------------------------------- 对外
    def _ptstore_load(self) -> Optional[_PmsTestRound]:
        """读**最新一轮**未完断点；没有 / 脏数据 / 已过期 → 返回 None（顺手清掉垃圾）。

        v1.9.54：「继续上次测试」默认取最新一轮，与 v1.8.0 的「唯一那一轮」语义对齐。
        """
        if self._ptstore_engine is None:
            return None
        with self._ptstore_lock:
            rounds = self._ptstore_take_live()
        return rounds[-1] if rounds else None

    def _ptstore_load_round(self, pms_round_id: str = "") -> Optional[_PmsTestRound]:
        """按轮次 id 读一轮；`pms_round_id` 留空 = **最新一轮**（v1.8.0 的老语义）。

        v1.9.54：多轮并存后，「继续上次测试」默认衔接最新一轮；
        界面上点名某一轮时（`_ptstore_list()` 里的 id）就按 id 精确取。
        """
        if self._ptstore_engine is None:
            return None
        target = str(pms_round_id or "").strip()
        with self._ptstore_lock:
            rounds = self._ptstore_take_live()
        if not target:
            return rounds[-1] if rounds else None
        for item in rounds:
            if item.round_id == target:
                return item
        return None

    def _ptstore_list(self) -> list[dict[str, Any]]:
        """列出全部未完断点的轻量信息（**旧 → 新**，末尾是最新一轮）。v1.9.54 多轮并存的新入口。

        ★ v1.9.59：`_ptstore_take_live()`——store 设过 `_ptstore_kind` 就只列本类；
        没设的（延迟 / 测速）保持 v1.9.54 的「全类」旧行为（第 6 项探针的逐字段对照不破）。
        """
        if self._ptstore_engine is None:
            return []
        with self._ptstore_lock:
            rounds = self._ptstore_take_live()
        return [item._ptround_index_entry() for item in rounds]

    def _ptstore_drop_round(self, pms_round_id: str) -> bool:
        """按轮次 id 丢一轮，返回「是否真的丢了」。

        ★ 只删点名的那一轮，**其余轮次一个都不碰**（验收里的「丢一轮不误伤其他轮」）。
        """
        target = str(pms_round_id or "").strip()
        if not target or self._ptstore_engine is None:
            return False
        with self._ptstore_lock:
            if all(item.round_id != target for item in self._ptstore_scan_rounds()):
                return False
            self._ptstore_forget([target])
        return True

    def _ptstore_save(self, pms_round: Optional[_PmsTestRound]) -> bool:
        """写入断点；传 None 或已完成的轮次等于「清断点」。**这是必须真的写的那一条路径。**"""
        engine = self._ptstore_engine
        if engine is None:
            return False
        self._ptstore_last_save_at = time.monotonic()
        self._ptstore_skipped = 0
        if pms_round is None or pms_round._ptround_is_finished():
            self._ptstore_clear()
            return True
        with self._ptstore_lock:
            # 先收掉超龄 / 已完成 / 超出上限的垃圾，再决定这一轮的去留
            live = self._ptstore_gather_live()
            # 同一类测试只留最新一轮（避免堆 5 轮「延迟」全是过期现场）
            self._ptstore_forget_kind(pms_round.kind, pms_round.round_id)
            live = [item for item in live if item.round_id != pms_round.round_id and item.kind != pms_round.kind]
            live.append(pms_round)
            self._ptstore_drop_oldest(live)
            saved = self._ptstore_set_meta(pms_round._ptround_key(),
                                           json.dumps(pms_round._ptround_to_dict(), ensure_ascii=False))
            self._ptstore_write_index(live)
        return saved

    def _ptstore_save_throttled(self, pms_round: Optional[_PmsTestRound]) -> bool:
        """按节流策略写断点（v1.9.27）：**测试中每完成一个节点都会调它**。

        为什么必须节流：4 万节点时断点 JSON 约 2 MB，逐节点写等于 15.6 GB 纯 I/O
        （实测 20 万次 × 41 ms = 825 秒），比测试本身还慢。

        **该写的时候一次都不能省**：轮次跑完 / 传 None（= 清断点）一律立刻写，
        否则界面会一直显示「还有 N 个可以续跑」。
        """
        if pms_round is None or pms_round._ptround_is_finished():
            return self._ptstore_save(pms_round)
        self._ptstore_skipped += 1
        elapsed = time.monotonic() - self._ptstore_last_save_at
        if elapsed >= PMS_TEST_ROUND_SAVE_INTERVAL_S or self._ptstore_skipped >= PMS_TEST_ROUND_SAVE_MAX_SKIP:
            return self._ptstore_save(pms_round)
        return False

    def _ptstore_clear(self) -> None:
        """丢弃断点。v1.9.54：**本轮所在的这一类**全部丢掉（旧格式那条也一起清）。

        为什么不是「只清最新一轮」：`plt_clear_round()` / `pst_clear_round()` 传进来的
        只有调度器，拿不到「点名哪一轮」的上下文；而「丢一轮」的精确入口是
        `_ptstore_drop_round(轮次id)`（界面按 id 调，绝不误伤其他轮）。

        ★ v1.9.59：设过归属（`_ptstore_kind`）的 store **只清本类**。电报调度器设了
        `telegram_dc`，它跑完一轮不能把用户留着续跑的延迟 / 测速断点一起抹掉
        （上面 docstring 说的「这一类」到这一刻才名副其实；延迟 / 测速没设归属，
        保持 v1.9.54 的「全清」旧行为不变）。
        """
        engine = self._ptstore_engine
        self._ptstore_last_save_at = time.monotonic()
        self._ptstore_skipped = 0
        if engine is None:
            return
        kind = self._ptstore_own_kind()
        with self._ptstore_lock:
            if kind:
                victims = [item.round_id for item in self._ptstore_scan_rounds() if item.kind == kind]
                if victims:
                    self._ptstore_forget(victims)   # forget 内部按全量视图重写索引，别类索引项一条不少
                return
            self._ptstore_write_index([])       # 整体清空：索引先清，保证界面立刻看不到残留
            self._ptstore_forget([item.round_id for item in self._ptstore_scan_rounds()])
            self._ptstore_clear_legacy()

    def _ptstore_has_pending(self) -> bool:
        """是否存在未完轮次（供界面决定「继续上次测试」按钮能不能点）。"""
        return self._ptstore_load() is not None
