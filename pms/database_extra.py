# -*- coding: utf-8 -*-
"""节点仓储的「测试结果落库」分片（v1.9.59）

`PmsNodeRepository` 的测试结果家当集中放这里，用 mixin 挂回 `pms/database.py`——与
`pms/sharelinks_extra.py` 同一套**红线腾行**办法（铁律 7.58：往顶线文件里加功能，成块
实现拆到 extra 模块，主文件只留合并点；本 mixin 类名按该条第 2 要求以 `_` 开头 →
不进启动器再导出，不占 400 行预算）。本文件承载：

  · 电报八列的迁移清单与「列归属」常量（`PMS_DB_TELEGRAM_COLUMNS` /
    `PMS_NODE_TELEGRAM_COLUMNS`）——`database.py` 在迁移表与 upsert 排除表里拼上它们；
  · `pnr_update_test_result()`：延迟 / 测速成绩落库（v1.9.59 从 database.py **原样搬入**，
    方法体逐字节等价，顺手把 database.py 治回 ≤1000——基线 v1.9.58 就是 1001，一直压破红线）；
  · `pnr_telegram_row_values()`：`pnr_node_to_row` 末尾那 8 个值的取法；
  · `pnr_update_telegram_result()`：单节点电报成绩落库（连通以 MTProto 握手为准，
    速度只在真拿到 HTTP 吞吐时才 > 0，**绝不编造**——用户 2026-10-15 定死的口径）。
"""

from __future__ import annotations

from typing import (
    Any,
    Optional,
)

from pms.utils import pms_now_millis_text, pms_now_text, pms_safe_int

# 电报八列（v1.9.59）：列名 + 建表/迁移用的类型片段。
# 迁移登记表（PMS_DB_REQUIRED_COLUMNS）与建表语句都从这里取，一份定义两处用，不再抄两遍。
PMS_DB_TELEGRAM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("telegram_ok", "INTEGER DEFAULT 0"), ("telegram_best_ms", "INTEGER DEFAULT -1"),
    ("telegram_best_dc", "TEXT DEFAULT ''"), ("telegram_reachable", "INTEGER DEFAULT 0"),
    ("telegram_tcp_count", "INTEGER DEFAULT 0"),
    ("telegram_total", "INTEGER DEFAULT 0"), ("telegram_speed_bps", "REAL DEFAULT 0"),
    ("telegram_time", "TEXT DEFAULT ''"), ("telegram_note", "TEXT DEFAULT ''"),
)

# 这八列**只由电报测试写**：订阅刷新 / 导入合并的 upsert 更新分支必须跳过它们，
# 否则用户刚测出来的成绩会被一次订阅更新抹掉（与 v1.9.37 健康检查五列同口径）。
PMS_NODE_TELEGRAM_COLUMNS: tuple[str, ...] = tuple(name for name, _type in PMS_DB_TELEGRAM_COLUMNS)


class _PmsNodeRepositoryTestMixin:
    """`PmsNodeRepository` 的测试结果分片（方法前缀 `pnr_` 与主类一致，命名唯一性不破）。"""

    def pnr_update_test_result(
        self,
        pms_uid: str,
        pms_latency_ms: Optional[int],
        pms_speed_bps: float,
        pms_mode: str,
        pms_success: bool,
        pms_last_ok: int = 0,
        pms_note: str = "",
    ) -> None:
        """写入一次测试结果（延迟 / 速率 / 成败计数 / 失败原因）。

        v1.9.8：`pms_last_ok` 记最近一次测试成败（0 未知 / 1 成功 / -1 失败）。
        失败时调用方应传**保留下来的旧速度**，不要把历史成绩清零。
        v1.9.21：`pms_note` 记失败原因原话（核心给的 `HTTP 503（…）` / `HTTP 504（Timeout）` 等），
        成功时传空串把它清掉——否则「上次为什么失败」会一直挂着。
        """
        self.pnr_engine.pdb_execute(
            "UPDATE pms_nodes SET latency_ms=?, speed_bps=?, last_test_time=?, last_test_mode=?, "
            "success_count=success_count+?, fail_count=fail_count+?, last_test_ok=?, last_test_note=?, "
            "updated_at=? WHERE uid=?",
            (
                pms_latency_ms if pms_latency_ms is not None else None,
                float(pms_speed_bps or 0.0),
                pms_now_millis_text(),
                str(pms_mode),
                1 if pms_success else 0,
                0 if pms_success else 1,
                pms_safe_int(pms_last_ok, 0, -1, 1),
                "" if pms_success else str(pms_note or "")[:200],
                pms_now_text(),
                str(pms_uid),
            ),
        )

    def pnr_telegram_row_values(self, pms_node: Any) -> tuple[Any, ...]:
        """节点 → 电报八列的入库值（顺序必须与 PMS_NODE_TELEGRAM_COLUMNS 一致）。

        用 getattr 兜历史对象（旧脚本构造的裸 dataclass 也可能缺列），全部夹到合法域。
        """
        node = pms_node
        return (
            pms_safe_int(getattr(node, "telegram_ok", 0), 0, -1, 1),
            pms_safe_int(getattr(node, "telegram_best_ms", -1), -1, -1, 10 ** 8),
            str(getattr(node, "telegram_best_dc", "") or "")[:8],
            pms_safe_int(getattr(node, "telegram_reachable", 0), 0, 0, 99),
            pms_safe_int(getattr(node, "telegram_tcp_count", 0), 0, 0, 99),
            pms_safe_int(getattr(node, "telegram_total", 0), 0, 0, 99),
            float(getattr(node, "telegram_speed_bps", 0.0) or 0.0),
            str(getattr(node, "telegram_time", "") or "")[:32],
            str(getattr(node, "telegram_note", "") or "")[:500],
        )

    def pnr_update_telegram_result(
        self,
        pms_uid: str,
        pms_ok: bool,
        pms_best_ms: int,
        pms_best_dc: str,
        pms_reachable: int,
        pms_tcp_count: int,
        pms_total: int,
        pms_speed_bps: float,
        pms_note: str = "",
    ) -> None:
        """写入一次电报 DC 测试结果（v1.9.59 起；v1.9.61 增 telegram_tcp_count）。

        ★ 口径：连通以 **MTProto 握手**（resPQ）为准；`pms_speed_bps` 只有真拿到
        HTTP 响应字节才 > 0——没有有效吞吐就写 0，列里不显示速度（用户定死：不编造）。
        `pms_tcp_count` = TCP 可达 DC 数（含握手成功者）；当 `pms_tcp_count > pms_reachable`
        即存在「仅 TCP 假通」节点——主表据此标 `仅TCP n/5` 而不是笼统「不通」。
        刻意**不动** latency / speed / last_test_* 与 success / fail 计数：电报成绩不掺进
        自动切换评分（那是「通用出口质量」，电报 DC 可达性是另一回事）。
        """
        self.pnr_engine.pdb_execute(
            "UPDATE pms_nodes SET telegram_ok=?, telegram_best_ms=?, telegram_best_dc=?, "
            "telegram_reachable=?, telegram_tcp_count=?, telegram_total=?, telegram_speed_bps=?, "
            "telegram_time=?, telegram_note=?, updated_at=? WHERE uid=?",
            (
                1 if pms_ok else -1,
                pms_safe_int(pms_best_ms, -1, -1, 10 ** 8),
                str(pms_best_dc or "")[:8],
                pms_safe_int(pms_reachable, 0, 0, 99),
                pms_safe_int(pms_tcp_count, 0, 0, 99),
                pms_safe_int(pms_total, 0, 0, 99),
                float(pms_speed_bps or 0.0),
                pms_now_millis_text(),
                str(pms_note or "")[:500],
                pms_now_text(),
                str(pms_uid),
            ),
        )
