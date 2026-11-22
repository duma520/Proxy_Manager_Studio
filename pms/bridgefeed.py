#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅侧 Tor 桥接源（v1.9.44）：从电报频道正文里抓桥接行，交给主线程写进 Tor 出口。

**为什么单独一个模块**：`pms/subscription.py` 是 992 行的顶红线文件（探针 `probe_v1918`
盯着 ≤1000），而「频道里挑桥接行」这件事本身与「订阅节点」是两条数据线：
节点要走 `pms_importers` 解析成 `PmsProxyNode` 入库，桥接只是一串**要写进设置**的行。
所以这里用 mixin 挂到既有类上，`pms/subscription.py` 里只留 6 行接线（见铁律 7.58）。

数据流（每一步都可以单独验）：

    t.me/s/<频道> 预览页
      → `psub_telegram_fetch_with_text()`：既取节点链接，也把**消息正文**收进 sink
      → `psub_collect_tor_bridges()`：用 `pms/tor_bridge.py` 从正文里挑出桥接行
      → 工作线程只把结果放进 `result["tor_bridges"]`（**不碰 QObject 设置**）
      → 主线程 `psu_apply_tor_bridges()`：按指纹去重合并进 `tor.bridge_lines`

★ **为什么写入必须回主线程**：`PmsSettingsManager` 是 QObject，工作线程里 `psm_set_*`
会打印 `QObject::startTimer: Timers cannot be started from another thread`（v1.9.32 踩过）；
订阅任务是 QRunnable，所以它只产出数据，落盘交给信号回到主线程的完成回调。

★ **桥接不是节点**：纯桥接频道**不入节点库**、也不参与「移除本次未出现的节点」
（否则频道里没有节点链接这一条，会把该订阅已有的节点全删掉）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pms.constants import PMS_LOGGER_NAME
from pms.utils import pms_safe_int
from pms.tor_bridge import (
    ptb_apply_bridge_lines_to_settings,
    ptb_extract_bridges_from_text,
)

# 订阅源类型常量与「类型显示」都在 `pms/subkind.py`（含 `tor_bridge` 这一型）


class _PsubBridgeMixin:
    """订阅任务 / 订阅服务共用的 Tor 桥接逻辑（私有 mixin，不进启动器再导出）。"""

    # 类级占位：`None` 表示还没建（**不能**用可变默认值，否则多个订阅会共用同一个列表）
    psub_bridge_sink: Optional[list[str]] = None

    # ------------------------------------------------------------------ 抓取侧
    def psub_bridge_texts(self) -> list[str]:
        """本次抓取的消息正文收集器（惰性建，每个任务一个）。"""
        if self.psub_bridge_sink is None:
            self.psub_bridge_sink = []
        return self.psub_bridge_sink

    def psub_telegram_fetch_with_text(self, *pms_args: Any, **pms_kwargs: Any) -> tuple[str, dict[str, int]]:
        """包一层 `pms_telegram_fetch_channel`：额外把消息正文收进 sink。

        包在 mixin 里而不是改调用点，是为了让 `pms/subscription.py` 的接线保持一行
        （那个文件顶在 1000 行红线上）。
        """
        from pms.telegram import pms_telegram_fetch_channel

        pms_kwargs["pms_text_sink"] = self.psub_bridge_texts()
        return pms_telegram_fetch_channel(*pms_args, **pms_kwargs)

    def psub_collect_tor_bridges(self) -> tuple[list[str], list[str]]:
        """从已收下的正文里挑桥接行，返回 (整理后的行, 出错说明)。"""
        bridges, errors = ptb_extract_bridges_from_text("\n".join(self.psub_bridge_texts()))
        lines = [str(item.get("raw") or "") for item in bridges if item.get("raw")]
        return lines, errors

    def psub_tor_payload(self, pms_lines: list[str], pms_errors: list[str], pms_channel: str) -> dict[str, Any]:
        """结果包：行 + 条数 + 出错说明 + 频道名 + 一句人话（界面与日志共用）。"""
        channel = str(pms_channel or "")
        note = f"Tor 桥接源「{channel or '未命名频道'}」：本次抓到 {len(pms_lines)} 条桥接"
        if pms_errors:
            note += f"（另有 {len(pms_errors)} 行像桥接但解析不了）"
        return {
            "lines": list(pms_lines),
            "total": len(pms_lines),
            "errors": list(pms_errors[:5]),
            "channel": channel,
            "note": note,
        }

    def psub_channel_name(self) -> str:
        """本次抓取用的频道名（统计里带着它）。"""
        stats = getattr(self, "psub_telegram_stats", {}) or {}
        return str(stats.get("channel") or "")

    def psub_remember_bridge_stats(self, pms_total: int, pms_note: str) -> None:
        """把桥接结果记到订阅行上（列表里要能看出「抓到了多少条、什么时候抓的」）。

        写数据库（线程安全，与节点落库同一条链路）；**设置项的写入交给主线程**。
        """
        try:
            pms_record = getattr(self, "psub_record", None)
            repo = getattr(self, "psub_repo", None)
            if pms_record is None or repo is None:
                return
            from pms.utils import pms_now_text

            pms_record.last_bridge_count = max(0, int(pms_total))
            pms_record.last_bridge_note = str(pms_note or "")[:200]
            pms_record.last_update_time = pms_now_text()
            pms_record.last_error = ""
            repo.pnr_upsert_subscription(pms_record)
        except Exception as exc:  # noqa: BLE001 - 统计写不进去不能拖垮整次订阅更新
            logging.getLogger(PMS_LOGGER_NAME + ".subscription").warning("写订阅桥接统计失败：%s", exc)

    # ------------------------------------------------------------------ 任务主流程的两个挂点
    def psub_has_tor_bridges(self) -> bool:
        """正文里有没有桥接行（没有节点链接时用它区分「纯桥接频道」与「真的空频道」）。"""
        return bool(self.psub_collect_tor_bridges()[0])

    def psub_finish_tor_only(self, pms_result: dict[str, Any], pms_text: str) -> bool:
        """纯桥接频道：填结果、发信号并返回 True（调用方直接 return）。

        判定条件：正文里挑出了桥接行 **且** 没有任何节点链接、也不是 GitHub 仓库。
        """
        if str(pms_text).strip() or getattr(self, "psub_github_files", None):
            return False
        lines, errors = self.psub_collect_tor_bridges()
        if not lines:
            return False
        payload = self.psub_tor_payload(lines, errors, self.psub_channel_name())
        self.psub_remember_bridge_stats(payload["total"], str(payload["note"]))
        pms_result.update({"ok": True, "node_count": 0, "note": str(payload["note"]), "tor_bridges": payload})
        self.psub_signals.psubs_task_done.emit(self.psub_record.uid, pms_result)
        return True

    def psub_attach_tor_payload(self, pms_result: dict[str, Any]) -> None:
        """混合频道（既有节点链接、也发桥接）：节点照旧入库，桥接一并交给主线程。"""
        lines, errors = self.psub_collect_tor_bridges()
        if not lines:
            return
        payload = self.psub_tor_payload(lines, errors, self.psub_channel_name())
        pms_result["tor_bridges"] = payload
        self.psub_remember_bridge_stats(payload["total"], str(payload["note"]))
        note = str(pms_result.get("note") or "").strip()
        pms_result["note"] = f"{note}；{payload['note']}" if note else str(payload["note"])

    # ------------------------------------------------------------------ 服务侧（主线程）
    def psu_tor_added_map(self) -> dict[str, int]:
        """本轮每个订阅写进桥接列表的**新增**条数（惰性建）。"""
        if getattr(self, "psu_tor_added", None) is None:
            self.psu_tor_added = {}
        return self.psu_tor_added

    def psu_apply_tor_bridges(self, pms_uid: str, pms_payload: dict[str, Any]) -> str:
        """把订阅抓到的 Tor 桥接合并进 `tor.bridge_lines`，并把结果播报出去。

        ★ 必须在**主线程**调用（见模块 docstring 的说明）。
        合并口径是「只增不删 + 按指纹去重」，见 `pms/tor_bridge.py ptb_merge_bridge_lines()`。
        """
        payload = pms_payload if isinstance(pms_payload, dict) else {}
        lines = [str(item) for item in (payload.get("lines") or []) if str(item).strip()]
        if not lines:
            return ""
        logger = logging.getLogger(PMS_LOGGER_NAME + ".subscription")
        try:
            added, total, errors = ptb_apply_bridge_lines_to_settings(self.psu_settings, lines)
        except Exception as exc:  # noqa: BLE001 - 桥接写失败不能拖垮订阅更新
            logger.warning("写入 Tor 桥接列表失败：%s", exc)
            self.psu_message.emit(f"Tor 桥接写入失败：{exc}")
            return ""
        self.psu_tor_added_map()[str(pms_uid)] = added
        channel = str(payload.get("channel") or "")
        suffix = f"（来自频道「{channel}」）" if channel else ""
        text = (f"Tor 桥接：新增 {added} 条，桥接列表现有 {total} 条{suffix}" if added
                else f"Tor 桥接：没有新桥接，列表仍为 {total} 条{suffix}")
        if errors:
            text += f"；另有 {len(errors)} 行像桥接但解析不了，已跳过"
        logger.info("Tor 桥接合并完成：新增 %d 条，共 %d 条（%s）", added, total, channel or "未命名频道")
        self.psu_message.emit(text)
        return text

    def psu_tor_summary_into(self, pms_summary: dict[str, Any]) -> None:
        """把桥接统计并进「全部更新」的汇总（桥接源节点数恒为 0，不单独说会显得什么都没干）。"""
        added = self.psu_tor_added_map()
        pms_summary["tor_added"] = sum(pms_safe_int(value, 0) for value in added.values())
        pms_summary["tor_sources"] = len(added)
