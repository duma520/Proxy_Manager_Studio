#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 仓储

SQLite 引擎（WAL + 线程锁 + 原子事务）与节点/分组/订阅/历史/采样的仓储层。
"""

from __future__ import annotations

from datetime import (
    datetime,
    timedelta,
)
from pathlib import Path
from typing import (
    Any,
    Optional,
    Sequence,
)
import logging
import sqlite3
import threading

from pms.constants import (
    PMS_APP_EN_NAME,
    PMS_APP_VERSION,
    PMS_LOGGER_NAME,
)
from pms.utils import (
    pms_now_millis_text,
    pms_now_text,
    pms_safe_bool,
    pms_safe_int,
)
from pms.envstore import PmsSecretVault
# v1.9.59：电报八列的迁移清单 / 列归属常量 / 行值与写库方法全在 database_extra.py
# （铁律 7.58 腾行法：顶线文件只留合并点，成块实现拆 extra，先例 `pms/sharelinks_extra.py`）
from pms.database_extra import (
    PMS_DB_TELEGRAM_COLUMNS,
    PMS_NODE_TELEGRAM_COLUMNS,
    _PmsNodeRepositoryTestMixin,
)
from pms.models import (
    PmsGroupRecord,
    PmsProxyNode,
    PmsSubscriptionRecord,
)

# --------------------------------------------------------------------------------------
# 数据库：SQLite（WAL + 线程锁），所有写操作都可原子回滚
# --------------------------------------------------------------------------------------
PMS_DB_SCHEMA_VERSION = 1

# v1.9.54（审计表第 22 项）：SSE 事件落库的键名前缀与条数上限。
# 键名 `event.log.<seq 零填充 20 位>` → 字典序 = seq 序，`ORDER BY meta_key` 就是时间序。
# ★ **刻意不做成设置项**（不写进 PMS_SETTINGS_DEFAULTS）：constants.py / settings_defaults.py
#   都顶在 1000 行红线，加键就要再挤一行；而「事件最多存多少条」是内部维护参数。
PMS_EVENT_LOG_KEY_PREFIX = "event.log."
PMS_EVENT_LOG_MAX = 2000

# v1.9.54（审计表第 19 项）：`pms_meta` 里「按前缀成批堆积」的键前缀登记表。
# 只给 `Probe` / 维护动作做统一兜底清理用；**常量本身不含 LIKE 通配符**（由 SQL 层拼 `%`）。
PMS_DB_PRUNE_PREFIXES: tuple[str, ...] = (PMS_EVENT_LOG_KEY_PREFIX, "test.round.", "telegram.last_id.")

# v1.9.58：SQLite 单条语句的绑定变量上限。
# SQLite 编译期默认 `SQLITE_MAX_VARIABLE_NUMBER = 999`（3.32 之前的旧默认是 999，
# 新版上限提高到 32766，但**运行时按编译时的值算**，不能假设）。
# 用户 2026-10-14 报障：点「一键清除全部测试成绩」时
# `sqlite3.OperationalError: too many SQL variables` —— 整库 4 万+ 节点的 uid
# 被塞进一条 `WHERE uid IN (?,?,?…)`。所以**任何按 uid/meta_key 批量拼 IN 的地方
# 都必须分批**，分批大小统一取这里，别在各处各写一个魔数。
# 取 800 而不是 999：给 SET 子句、LIMIT 等额外参数留余量。
PMS_SQL_MAX_VARIABLES = 800

# 第 13 项：结构迁移所需的“必需列”登记表（表 -> [(列名, 类型)]）。
# 缺失则自动补，已存在则跳过；新增迁移只改这里，无需手写一次性脚本。
PMS_DB_REQUIRED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "pms_subscriptions": [
        ("traffic_raw", "TEXT DEFAULT ''"),
        # v1.9.44：Tor 桥接源（`t.me/tor_bridges` 这类）——桥接条数与说明单独存，
        # 不能塞进 last_node_count（那条列是「节点数」，界面直接显示给用户看）
        ("last_bridge_count", "INTEGER DEFAULT 0"), ("last_bridge_note", "TEXT DEFAULT ''"),
    ],
    # v1.9.21：最近一次失败的原始原因（核心给的 HTTP 503/504 等），供「延迟列显示核心原话」用
    # v1.9.37：节点健康检查五列（自动禁用与手动禁用必须能区分，见 healthcheck.py）
    # v1.9.59：电报 DC 测试八列（清单本体在 database_extra.py，一处定义迁移与归属共用）
    "pms_nodes": [
        ("last_test_ok", "INTEGER DEFAULT 0"), ("last_test_note", "TEXT DEFAULT ''"),
        ("health_fail_streak", "INTEGER DEFAULT 0"), ("health_auto_disabled", "INTEGER DEFAULT 0"),
        ("health_disabled_at", "TEXT DEFAULT ''"), ("health_next_check_at", "TEXT DEFAULT ''"),
        ("health_last_note", "TEXT DEFAULT ''"),
        *PMS_DB_TELEGRAM_COLUMNS,
    ],
}

# 节点健康检查的五列（v1.9.37）：这些列**只由健康检查服务与「用户手动启用/禁用」写**，
# `pnr_upsert_nodes` 的更新分支刻意跳过它们——否则每次订阅刷新都会把自动禁用状态洗掉。
PMS_NODE_HEALTH_COLUMNS: tuple[str, ...] = (
    "health_fail_streak", "health_auto_disabled", "health_disabled_at", "health_next_check_at", "health_last_note",
)

class PmsDatabaseEngine:
    """SQLite 引擎：单连接 + 可重入锁（WAL 模式），供多线程共享。"""

    def __init__(self, pms_db_file: Path, pms_logger: Optional[logging.Logger] = None) -> None:
        self.pdb_path = Path(pms_db_file)
        self.pdb_logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".db")
        self.pdb_lock = threading.RLock()
        self.pdb_connection: Optional[sqlite3.Connection] = None
        self.pdb_closed = False

    def pdb_open(self) -> sqlite3.Connection:
        """打开连接并建表（幂等）。"""
        with self.pdb_lock:
            if self.pdb_connection is not None:
                return self.pdb_connection
            self.pdb_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.pdb_path), timeout=15.0, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=8000")
            self.pdb_connection = connection
            self.pdb_closed = False
            self.pdb_create_schema()
            self.pdb_migrate()
            self.pdb_logger.info("数据库就绪：%s", self.pdb_path)
            return connection

    def pdb_create_schema(self) -> None:
        """建表 + 建索引 + 记录版本号。"""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS pms_nodes (
                uid TEXT PRIMARY KEY, sort_index INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
                name TEXT DEFAULT '', protocol TEXT DEFAULT '', server TEXT DEFAULT '', port INTEGER DEFAULT 0,
                group_name TEXT DEFAULT '', subscription_uid TEXT DEFAULT '', subscription_name TEXT DEFAULT '',
                remark TEXT DEFAULT '', tags TEXT DEFAULT '', transport TEXT DEFAULT '', security TEXT DEFAULT '',
                sni TEXT DEFAULT '', alpn TEXT DEFAULT '', fingerprint TEXT DEFAULT '', allow_insecure INTEGER DEFAULT 0,
                host TEXT DEFAULT '', path TEXT DEFAULT '', service_name TEXT DEFAULT '', grpc_mode TEXT DEFAULT '',
                header_type TEXT DEFAULT '', credential_uuid TEXT DEFAULT '', credential_password TEXT DEFAULT '',
                method TEXT DEFAULT '', flow TEXT DEFAULT '', alter_id INTEGER DEFAULT 0, vmess_security TEXT DEFAULT 'auto',
                public_key TEXT DEFAULT '', short_id TEXT DEFAULT '', spider_x TEXT DEFAULT '', cert_sha256 TEXT DEFAULT '',
                obfs TEXT DEFAULT '',
                obfs_password TEXT DEFAULT '', congestion_control TEXT DEFAULT '', udp_relay_mode TEXT DEFAULT '',
                ss_plugin TEXT DEFAULT '', ss_plugin_opts TEXT DEFAULT '', extra_json TEXT DEFAULT '',
                latency_ms INTEGER, speed_bps REAL DEFAULT 0, last_test_time TEXT DEFAULT '', last_test_mode TEXT DEFAULT '',
                last_test_note TEXT DEFAULT '',
                success_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0,
                health_fail_streak INTEGER DEFAULT 0, health_auto_disabled INTEGER DEFAULT 0,
                health_disabled_at TEXT DEFAULT '', health_next_check_at TEXT DEFAULT '', health_last_note TEXT DEFAULT '',
                telegram_ok INTEGER DEFAULT 0, telegram_best_ms INTEGER DEFAULT -1, telegram_best_dc TEXT DEFAULT '',
                telegram_reachable INTEGER DEFAULT 0, telegram_tcp_count INTEGER DEFAULT 0,
                telegram_total INTEGER DEFAULT 0, telegram_speed_bps REAL DEFAULT 0,
                telegram_time TEXT DEFAULT '', telegram_note TEXT DEFAULT '',
                created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pms_groups (
                uid TEXT PRIMARY KEY, name TEXT UNIQUE, color TEXT DEFAULT '#3d8bfd',
                sort_index INTEGER DEFAULT 0, remark TEXT DEFAULT '', created_at TEXT DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pms_subscriptions (
                uid TEXT PRIMARY KEY, name TEXT DEFAULT '', url TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
                user_agent TEXT DEFAULT '', api_key TEXT DEFAULT '', update_interval_minutes INTEGER DEFAULT 360,
                last_update_time TEXT DEFAULT '', last_node_count INTEGER DEFAULT 0, last_error TEXT DEFAULT '',
                traffic_upload INTEGER DEFAULT 0, traffic_download INTEGER DEFAULT 0, traffic_total INTEGER DEFAULT 0,
                traffic_expire INTEGER DEFAULT 0, sort_index INTEGER DEFAULT 0, use_system_proxy INTEGER DEFAULT 0,
                traffic_raw TEXT DEFAULT '', remark TEXT DEFAULT '', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
                last_bridge_count INTEGER DEFAULT 0, last_bridge_note TEXT DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pms_latency_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, node_uid TEXT, node_name TEXT, test_mode TEXT,
                latency_ms INTEGER, speed_bps REAL, tested_at TEXT, detail TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pms_connection_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, node_uid TEXT, host TEXT, destination TEXT,
                outbound TEXT, bytes_up INTEGER, bytes_down INTEGER, started_at TEXT, ended_at TEXT, duration_ms INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pms_traffic_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sampled_at TEXT, proxy_up INTEGER, proxy_down INTEGER,
                direct_up INTEGER, direct_down INTEGER, active_nodes INTEGER
            )
            """,
            "CREATE TABLE IF NOT EXISTS pms_meta (meta_key TEXT PRIMARY KEY, meta_value TEXT)",
            "CREATE INDEX IF NOT EXISTS idx_pms_nodes_sub ON pms_nodes(subscription_uid)",
            "CREATE INDEX IF NOT EXISTS idx_pms_nodes_sort ON pms_nodes(sort_index)",
            "CREATE INDEX IF NOT EXISTS idx_pms_latency_node ON pms_latency_history(node_uid, tested_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_pms_conn_time ON pms_connection_history(started_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_pms_traffic_time ON pms_traffic_samples(sampled_at DESC)",
        ]
        with self.pdb_lock:
            connection = self.pdb_connection
            if connection is None:
                raise RuntimeError("数据库尚未打开")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO pms_meta(meta_key, meta_value) VALUES('schema_version', ?) "
                "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
                (str(PMS_DB_SCHEMA_VERSION),),
            )
            connection.commit()

    def pdb_migrate(self) -> None:
        """结构迁移（第 13 项）：按 pms_meta.schema_version 与当前 PMS_DB_SCHEMA_VERSION 比对。

        采用“确保已知表拥有必需列”的幂等策略：缺失则补，已存在则跳过，可重复运行。
        未来新增列/表只需在 PMS_DB_REQUIRED_COLUMNS 里追加，不必手写一次性迁移脚本。
        """
        connection = self.pdb_connection
        if connection is None:
            return
        with self.pdb_lock:
            for table, columns in PMS_DB_REQUIRED_COLUMNS.items():
                try:
                    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                except sqlite3.OperationalError:
                    continue
                for col_name, col_type in columns:
                    if col_name in existing:
                        continue
                    try:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                        self.pdb_logger.info("数据库迁移：为 %s 补列 %s", table, col_name)
                    except sqlite3.OperationalError as exc:
                        self.pdb_logger.warning("补列失败 %s.%s：%s", table, col_name, exc)
            connection.execute(
                "INSERT INTO pms_meta(meta_key, meta_value) VALUES('schema_version', ?) "
                "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
                (str(PMS_DB_SCHEMA_VERSION),),
            )
            connection.commit()

    # ------------------------------------------------------------------ 基础操作
    def pdb_execute(self, pms_sql: str, pms_params: Sequence[Any] = ()) -> int:
        """执行写语句，返回受影响行数（参数既支持序列也支持具名字典）。"""
        bind = pms_params if isinstance(pms_params, dict) else tuple(pms_params)
        with self.pdb_lock:
            connection = self.pdb_open()
            cursor = connection.execute(pms_sql, bind)
            connection.commit()
            return cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0

    def pdb_execute_many(self, pms_sql: str, pms_rows: Sequence[Sequence[Any]]) -> int:
        """批量执行（单事务，失败整体回滚）。"""
        if not pms_rows:
            return 0
        first = pms_rows[0]
        rows = list(pms_rows) if isinstance(first, dict) else [tuple(row) for row in pms_rows]
        with self.pdb_lock:
            connection = self.pdb_open()
            try:
                cursor = connection.executemany(pms_sql, rows)
                connection.commit()
                return cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0
            except sqlite3.Error:
                connection.rollback()
                raise

    def pdb_query(self, pms_sql: str, pms_params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """查询并返回全部行（Row 支持按列名取值）。"""
        with self.pdb_lock:
            connection = self.pdb_open()
            cursor = connection.execute(pms_sql, tuple(pms_params))
            return list(cursor.fetchall())

    def pdb_query_one(self, pms_sql: str, pms_params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.pdb_query(pms_sql, pms_params)
        return rows[0] if rows else None

    def pdb_scalar(self, pms_sql: str, pms_params: Sequence[Any] = (), pms_default: Any = 0) -> Any:
        row = self.pdb_query_one(pms_sql, pms_params)
        if row is None or len(row) == 0:
            return pms_default
        value = row[0]
        return pms_default if value is None else value

    def pdb_transaction(self, pms_statements: Sequence[tuple[str, Sequence[Any]]]) -> bool:
        """把多条语句放进一个事务（配置导入/批量删除用）。"""
        with self.pdb_lock:
            connection = self.pdb_open()
            try:
                connection.execute("BEGIN")
                for sql, params in pms_statements:
                    bind = params if isinstance(params, dict) else tuple(params)
                    connection.execute(sql, bind)
                connection.commit()
                return True
            except sqlite3.Error as exc:
                connection.rollback()
                self.pdb_logger.error("事务回滚：%s", exc)
                return False

    def pdb_set_meta(self, pms_key: str, pms_value: str) -> None:
        self.pdb_execute(
            "INSERT INTO pms_meta(meta_key, meta_value) VALUES(?, ?) "
            "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
            (str(pms_key), str(pms_value)),
        )

    def pdb_get_meta(self, pms_key: str, pms_default: str = "") -> str:
        value = self.pdb_scalar("SELECT meta_value FROM pms_meta WHERE meta_key=?", (str(pms_key),), None)
        return pms_default if value is None else str(value)

    def pdb_prune_history(
        self,
        pms_latency_days: int,
        pms_connection_days: int,
        pms_keep_connections: bool,
        pms_keep_latency: bool = True,
    ) -> int:
        """清理过期历史数据，返回删除行数。

        v1.9.16：`pms_keep_latency=False`（设置项 `database.keep_latency_history` 未勾选）时
        **整表清空延迟/测速历史**——原来这个设置项只被写进配置、从没人读，勾不勾都一样。
        默认 True = 只按天数清理（与改动前完全一致）。
        """
        removed = 0
        latency_limit = (datetime.now() - timedelta(days=max(1, int(pms_latency_days)))).strftime("%Y-%m-%d %H:%M:%S")
        if pms_keep_latency:
            removed += self.pdb_execute("DELETE FROM pms_latency_history WHERE tested_at < ?", (latency_limit,))
        else:
            removed += self.pdb_execute("DELETE FROM pms_latency_history")
        connection_limit = (datetime.now() - timedelta(days=max(1, int(pms_connection_days)))).strftime("%Y-%m-%d %H:%M:%S")
        if pms_keep_connections:
            removed += self.pdb_execute("DELETE FROM pms_connection_history WHERE started_at < ?", (connection_limit,))
        else:
            removed += self.pdb_execute("DELETE FROM pms_connection_history")
        removed += self.pdb_execute("DELETE FROM pms_traffic_samples WHERE sampled_at < ?", (latency_limit,))
        return removed

    def pdb_prune_prefixed_meta(self, pms_prefixes: Sequence[str], pms_keep_latest: int = 0) -> int:
        """清掉 `pms_meta` 里**按前缀成批堆积**的键，返回删除行数（v1.9.54，审计表第 19 / 22 项）。

        为什么要有它：`pms_meta` 原本只放单条配置（schema_version / test.pending_round …），
        v1.9.54 起多轮断点用 `test.round.<轮次id>`、SSE 事件落库用 `event.log.<seq>`，
        都是「按前缀不停追加」的写法。**不能无限涨**，所以给出一个统一的、按前缀兜底的清理入口。

        · `pms_keep_latest > 0` 时，每个前缀只保留**键名最大**（键名里带零填充的序号，
          所以字典序 = 时间序）的这么多条，其余删除；
        · `pms_keep_latest <= 0` 时清掉该前缀下**全部**键。

        调用方传的应当是**不含通配符的前缀常量**（如 `PMS_API_EVENT_LOG_PREFIX`），
        这里只拼一个 `%` 给 LIKE，避免调用方自己拼出 LIKE 注入式的键。
        """
        keep = max(0, int(pms_keep_latest))
        removed = 0
        for prefix in pms_prefixes:
            text = str(prefix or "")
            if not text:
                continue
            try:
                if keep <= 0:
                    removed += self.pdb_execute("DELETE FROM pms_meta WHERE meta_key LIKE ?", (text + "%",))
                    continue
                rows = self.pdb_query(
                    "SELECT meta_key FROM pms_meta WHERE meta_key LIKE ? ORDER BY meta_key DESC LIMIT -1 OFFSET ?",
                    (text + "%", keep),
                )
                stale = [str(row["meta_key"]) for row in rows]
                if not stale:
                    continue
                # v1.9.58：同样按变量上限分批删（`stale` 通常很小，但不设防就是隐患）
                for start in range(0, len(stale), PMS_SQL_MAX_VARIABLES):
                    batch = stale[start:start + PMS_SQL_MAX_VARIABLES]
                    placeholders = ",".join("?" * len(batch))
                    removed += self.pdb_execute(
                        f"DELETE FROM pms_meta WHERE meta_key IN ({placeholders})", tuple(batch))
            except sqlite3.Error as exc:
                # 清理失败绝不能影响主流程（测试 / 事件推送都还要继续），但**绝不静默**
                self.pdb_logger.warning("清理过期元数据失败（前缀 %s）：%s", text, exc)
        return removed

    def pdb_vacuum(self) -> bool:
        """整理数据库文件（设置里可开启退出时自动整理）。"""
        with self.pdb_lock:
            try:
                connection = self.pdb_open()
                connection.execute("VACUUM")
                connection.commit()
                return True
            except sqlite3.Error as exc:
                self.pdb_logger.error("VACUUM 失败：%s", exc)
                return False

    def pdb_backup_to(self, pms_target: Path) -> bool:
        """用 SQLite 备份 API 生成一致性快照。"""
        with self.pdb_lock:
            try:
                connection = self.pdb_open()
                Path(pms_target).parent.mkdir(parents=True, exist_ok=True)
                target = sqlite3.connect(str(pms_target))
                try:
                    connection.backup(target)
                finally:
                    target.close()
                return True
            except sqlite3.Error as exc:
                self.pdb_logger.error("数据库备份失败：%s", exc)
                return False

    def pdb_integrity_check(self) -> str:
        result = self.pdb_scalar("PRAGMA integrity_check", (), "unknown")
        return str(result)

    def pdb_stats(self) -> dict[str, int]:
        """统计各表行数（设置/关于页面展示用）。"""
        sizes: dict[str, int] = {}
        for table in ("pms_nodes", "pms_groups", "pms_subscriptions", "pms_latency_history", "pms_connection_history", "pms_traffic_samples"):
            sizes[table] = pms_safe_int(self.pdb_scalar(f"SELECT COUNT(*) FROM {table}", (), 0), 0)
        try:
            sizes["file_bytes"] = self.pdb_path.stat().st_size
        except OSError:
            sizes["file_bytes"] = 0
        return sizes

    def pdb_close(self) -> None:
        """关闭连接（退出流程调用）。"""
        with self.pdb_lock:
            if self.pdb_connection is None:
                return
            try:
                self.pdb_connection.commit()
            except sqlite3.Error:
                pass
            try:
                self.pdb_connection.close()
            except sqlite3.Error:
                pass
            self.pdb_connection = None
            self.pdb_closed = True

class PmsNodeRepository(_PmsNodeRepositoryTestMixin):
    """节点 / 分组 / 订阅 / 历史的仓储层（唯一允许拼 SQL 的地方）。

    v1.9.59：测试成绩的落库（延迟 / 测速 / 电报）在 `_PmsNodeRepositoryTestMixin`
    （`pms/database_extra.py`，铁律 7.58 腾行），本文件只留合并点。
    """

    def __init__(self, pms_engine: PmsDatabaseEngine, pms_vault: PmsSecretVault) -> None:
        self.pnr_engine = pms_engine
        self.pnr_vault = pms_vault
        self.pnr_logger = logging.getLogger(PMS_LOGGER_NAME + ".repo")
        self._pnr_columns = (
            "uid", "sort_index", "enabled", "name", "protocol", "server", "port", "group_name",
            "subscription_uid", "subscription_name", "remark", "tags", "transport", "security", "sni",
            "alpn", "fingerprint", "allow_insecure", "host", "path", "service_name", "grpc_mode",
            "header_type", "credential_uuid", "credential_password", "method", "flow", "alter_id",
            "vmess_security", "public_key", "short_id", "spider_x", "cert_sha256", "obfs", "obfs_password",
            "congestion_control", "udp_relay_mode", "ss_plugin", "ss_plugin_opts", "extra_json",
            "latency_ms", "speed_bps", "last_test_time", "last_test_mode", "success_count", "fail_count",
            "last_test_ok",  # v1.9.8：最近一次测试成败，测速失败时列里才能显示「失败」而不是「-」
            "last_test_note",  # v1.9.21：最近一次失败的原因原话（延迟列「显示核心原话」用）
            # v1.9.37：健康检查五列（顺序必须与 pnr_node_to_row 返回的元组一致）
            "health_fail_streak", "health_auto_disabled", "health_disabled_at", "health_next_check_at", "health_last_note",
            # v1.9.59：电报 DC 八列（清单与行值顺序都在 database_extra.py，更新分支靠它排除）
            *PMS_NODE_TELEGRAM_COLUMNS,
            "created_at", "updated_at",
        )

    # ------------------------------------------------------------------ 节点
    def pnr_node_to_row(self, pms_node: PmsProxyNode) -> tuple[Any, ...]:
        """节点 → 数据库行（凭据字段在此加密）。"""
        node = pms_node
        return (
            node.uid, int(node.sort_index), 1 if node.enabled else 0, node.name, node.protocol, node.server,
            int(node.port), node.group_name, node.subscription_uid, node.subscription_name, node.remark,
            node.tags, node.transport, node.security, node.sni, node.alpn, node.fingerprint,
            1 if node.allow_insecure else 0, node.host, node.path, node.service_name, node.grpc_mode,
            node.header_type,
            self.pnr_vault.psv_encrypt(node.credential_uuid),
            self.pnr_vault.psv_encrypt(node.credential_password),
            node.method, node.flow, int(node.alter_id), node.vmess_security, node.public_key,
            node.short_id, node.spider_x, node.cert_sha256, node.obfs,
            self.pnr_vault.psv_encrypt(node.obfs_password),
            node.congestion_control, node.udp_relay_mode, node.ss_plugin, node.ss_plugin_opts, node.extra_json,
            node.latency_ms if node.latency_ms is not None else None, float(node.speed_bps or 0.0),
            node.last_test_time, node.last_test_mode, int(node.success_count), int(node.fail_count),
            pms_safe_int(getattr(node, "last_test_ok", 0), 0, -1, 1),
            str(getattr(node, "last_test_note", "") or ""),  # v1.9.21：失败原因原话
            int(getattr(node, "health_fail_streak", 0) or 0),
            1 if getattr(node, "health_auto_disabled", False) else 0,
            str(getattr(node, "health_disabled_at", "") or ""),
            str(getattr(node, "health_next_check_at", "") or ""),
            str(getattr(node, "health_last_note", "") or ""),
            # v1.9.59：电报八列的行值在 database_extra.py（顺序与 _pnr_columns 对齐）
            *self.pnr_telegram_row_values(node),
            node.created_at, node.updated_at,
        )

    def pnr_row_to_node(self, pms_row: sqlite3.Row) -> PmsProxyNode:
        """数据库行 → 节点（凭据字段在此解密）。"""
        payload: dict[str, Any] = dict(pms_row)  # C 层直接转换，比逐键取值快得多（5.7 万行差 3 秒）
        payload["enabled"] = pms_safe_bool(payload.get("enabled"), True)
        payload["allow_insecure"] = pms_safe_bool(payload.get("allow_insecure"), False)
        payload["health_auto_disabled"] = pms_safe_bool(payload.get("health_auto_disabled"), False)   # v1.9.37
        payload["credential_uuid"] = self.pnr_vault.psv_decrypt(str(payload.get("credential_uuid") or ""))
        payload["credential_password"] = self.pnr_vault.psv_decrypt(str(payload.get("credential_password") or ""))
        payload["obfs_password"] = self.pnr_vault.psv_decrypt(str(payload.get("obfs_password") or ""))
        return PmsProxyNode.pnode_from_dict(payload)

    def pnr_upsert_nodes(self, pms_nodes: Sequence[PmsProxyNode]) -> int:
        """批量写入/更新节点（单事务）。

        v1.9.37：从 `INSERT OR REPLACE` 改成 `ON CONFLICT(uid) DO UPDATE`，而且：
            · **不碰** `PMS_NODE_HEALTH_COLUMNS` 五列——原来的写法每次订阅刷新都会删行重建，
              把健康检查的「自动禁用 / 连续失败 / 下次检查时间」一起洗掉；
            · `enabled` 用 CASE 兜一下：库里那行若被健康检查自动禁用（`health_auto_disabled=1`），
              就用库里的值，别被订阅刷新重新启用（否则自动禁用形同虚设）。
        """
        if not pms_nodes:
            return 0
        placeholders = ", ".join("?" for _ in self._pnr_columns)
        parts = [
            "enabled=CASE WHEN pms_nodes.health_auto_disabled=1 THEN pms_nodes.enabled ELSE excluded.enabled END",
            *[f"{name}=excluded.{name}" for name in self._pnr_columns
              if name not in PMS_NODE_HEALTH_COLUMNS and name not in PMS_NODE_TELEGRAM_COLUMNS and name != "enabled"],
        ]
        sql = (
            f"INSERT INTO pms_nodes ({', '.join(self._pnr_columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(uid) DO UPDATE SET {', '.join(parts)}"
        )
        rows = [self.pnr_node_to_row(node) for node in pms_nodes]
        return self.pnr_engine.pdb_execute_many(sql, rows)

    def pnr_upsert_node(self, pms_node: PmsProxyNode) -> int:
        return self.pnr_upsert_nodes([pms_node])

    def pnr_load_all_nodes(self) -> list[PmsProxyNode]:
        """按 sort_index 升序加载全部节点。"""
        rows = self.pnr_engine.pdb_query("SELECT * FROM pms_nodes ORDER BY sort_index ASC, created_at ASC")
        return [self.pnr_row_to_node(row) for row in rows]

    def pnr_load_node(self, pms_uid: str) -> Optional[PmsProxyNode]:
        row = self.pnr_engine.pdb_query_one("SELECT * FROM pms_nodes WHERE uid=?", (str(pms_uid),))
        return self.pnr_row_to_node(row) if row is not None else None

    def pnr_delete_nodes(self, pms_uids: Sequence[str]) -> int:
        if not pms_uids:
            return 0
        removed = 0
        for chunk_start in range(0, len(pms_uids), 400):
            chunk = [str(item) for item in pms_uids[chunk_start:chunk_start + 400]]
            marks = ", ".join("?" for _ in chunk)
            removed += self.pnr_engine.pdb_execute(f"DELETE FROM pms_nodes WHERE uid IN ({marks})", chunk)
        return removed

    def pnr_delete_by_subscription(self, pms_subscription_uid: str) -> int:
        return self.pnr_engine.pdb_execute("DELETE FROM pms_nodes WHERE subscription_uid=?", (str(pms_subscription_uid),))

    def pnr_update_enabled(self, pms_uids: Sequence[str], pms_enabled: bool) -> int:
        if not pms_uids:
            return 0
        updated = 0
        for chunk_start in range(0, len(pms_uids), 400):
            chunk = [str(item) for item in pms_uids[chunk_start:chunk_start + 400]]
            marks = ", ".join("?" for _ in chunk)
            updated += self.pnr_engine.pdb_execute(
                f"UPDATE pms_nodes SET enabled=?, updated_at=? WHERE uid IN ({marks})",
                [1 if pms_enabled else 0, pms_now_text()] + chunk,
            )
        return updated

    def pnr_update_sort_indices(self, pms_pairs: Sequence[tuple[str, int]]) -> int:
        """批量写入排序号。"""
        if not pms_pairs:
            return 0
        rows = [(int(index), pms_now_text(), str(uid)) for uid, index in pms_pairs]
        return self.pnr_engine.pdb_execute_many("UPDATE pms_nodes SET sort_index=?, updated_at=? WHERE uid=?", rows)

    def pnr_clear_test_results(self, pms_uids: Sequence[str], pms_mode: str) -> int:
        """清空延迟 / 测速 / 电报成绩，返回受影响行数（v1.9.17；v1.9.58 修大批量崩溃）。

        `pms_mode`：`latency`=只清延迟（并把 `last_test_ok` 复位成 0）／`speed`=只清速度／
        `telegram`=只清电报（v1.9.67 新增）／`both`=延迟+测速都清且把「最近一次测试时间 /
        方式」也复位——成绩都清了还留着时间戳会自相矛盾／`all`=延迟+测速+电报三列一起清
        （v1.9.67：用户要求「一键清理测试数据应当包括 延迟 测速 电报 三列」）。
        只动 `pms_nodes`，**不碰 `pms_latency_history`**（历史是统计用的独立记录，由「清理过期历史」管）。

        ★ v1.9.58：**必须分批**。SQLite 的绑定变量上限默认 999（`SQLITE_MAX_VARIABLE_NUMBER`），
        而「一键清除全部测试成绩」会把**整库节点**（实测 4 万+）的 uid 塞进一条
        `WHERE uid IN (?,?,?…)`，直接抛 `sqlite3.OperationalError: too many SQL variables`，
        界面整个崩掉（用户 2026-10-14 报障，traceback 见 `mainwindow_ui.py:138` 那条链）。
        现在按 `PMS_SQL_MAX_VARIABLES` 切批，每批单独执行、行数累加。
        """
        uids = [str(uid) for uid in pms_uids if str(uid)]
        if not uids:
            return 0
        mode = str(pms_mode or "").strip().lower()
        sets = ["updated_at=?"]
        if mode in ("latency", "both", "all"):
            # v1.9.21：清延迟也要把失败原因一起清掉，否则「显示核心原话」会一直挂着上次的原因
            sets.extend(["latency_ms=NULL", "last_test_ok=0", "last_test_note=''"])
        if mode in ("speed", "both", "all"):
            sets.append("speed_bps=0")
        if mode in ("telegram", "all"):
            # v1.9.67：电报成绩整列复位。`telegram_best_ms` 用 -1（列默认值是 -1，不是 0）、
            # `telegram_ok` 用 0（0 = 未测，见 models.pnode_display_telegram），
            # `telegram_best_dc` / `telegram_note` 清成空串，其余计数归 0。
            sets.extend([
                "telegram_ok=0", "telegram_best_ms=-1", "telegram_best_dc=''",
                "telegram_reachable=0", "telegram_tcp_count=0", "telegram_total=0",
                "telegram_speed_bps=0", "telegram_note=''",
            ])
        if mode in ("both", "all"):
            sets.extend(["last_test_time=''", "last_test_mode=''"])
        if len(sets) == 1:
            return 0
        # 每批的 uid 数 = 变量上限 - SET 子句占用的参数个数（updated_at 占 1 个）
        chunk = max(1, PMS_SQL_MAX_VARIABLES - len(sets))
        statement = f"UPDATE pms_nodes SET {', '.join(sets)} WHERE uid IN ({{}})"
        affected = 0
        for start in range(0, len(uids), chunk):
            batch = uids[start:start + chunk]
            placeholders = ",".join("?" * len(batch))
            affected += self.pnr_engine.pdb_execute(
                statement.format(placeholders),
                tuple([pms_now_text()]) + tuple(batch),
            )
        return affected

    def pnr_update_group_name(self, pms_group_old: str, pms_group_new: str) -> int:
        return self.pnr_engine.pdb_execute(
            "UPDATE pms_nodes SET group_name=?, updated_at=? WHERE group_name=?",
            (str(pms_group_new), pms_now_text(), str(pms_group_old)),
        )

    def pnr_update_subscription_name(self, pms_subscription_uid: str, pms_name: str) -> int:
        return self.pnr_engine.pdb_execute(
            "UPDATE pms_nodes SET subscription_name=?, updated_at=? WHERE subscription_uid=?",
            (str(pms_name), pms_now_text(), str(pms_subscription_uid)),
        )

    def pnr_count_nodes(self) -> int:
        return pms_safe_int(self.pnr_engine.pdb_scalar("SELECT COUNT(*) FROM pms_nodes", (), 0), 0)

    def pnr_count_nodes_of_subscription(self, pms_subscription_uid: str) -> int:
        return pms_safe_int(
            self.pnr_engine.pdb_scalar("SELECT COUNT(*) FROM pms_nodes WHERE subscription_uid=?", (str(pms_subscription_uid),), 0),
            0,
        )

    def pnr_group_summary(self) -> list[tuple[str, int]]:
        """分组及其节点数（用于分组树）。"""
        rows = self.pnr_engine.pdb_query(
            "SELECT group_name AS g, COUNT(*) AS c FROM pms_nodes GROUP BY group_name ORDER BY g ASC"
        )
        return [(str(row["g"] or "默认分组"), int(row["c"])) for row in rows]

    # ------------------------------------------------------------------ 分组
    def pnr_load_groups(self) -> list[PmsGroupRecord]:
        rows = self.pnr_engine.pdb_query("SELECT * FROM pms_groups ORDER BY sort_index ASC, name ASC")
        groups: list[PmsGroupRecord] = []
        for row in rows:
            payload = {key: row[key] for key in row.keys()}
            payload["sort_index"] = pms_safe_int(payload.get("sort_index"), 0, 0, 10 ** 6)
            groups.append(PmsGroupRecord.pgr_from_dict(payload))
        return groups

    def pnr_upsert_group(self, pms_group: PmsGroupRecord) -> int:
        return self.pnr_engine.pdb_execute(
            "INSERT OR REPLACE INTO pms_groups (uid, name, color, sort_index, remark, created_at) VALUES (?,?,?,?,?,?)",
            (pms_group.uid, pms_group.name, pms_group.color, int(pms_group.sort_index), pms_group.remark, pms_group.created_at),
        )

    def pnr_delete_group(self, pms_uid: str) -> int:
        return self.pnr_engine.pdb_execute("DELETE FROM pms_groups WHERE uid=?", (str(pms_uid),))

    # ------------------------------------------------------------------ 订阅
    def pnr_load_subscriptions(self) -> list[PmsSubscriptionRecord]:
        rows = self.pnr_engine.pdb_query("SELECT * FROM pms_subscriptions ORDER BY sort_index ASC, created_at ASC")
        records: list[PmsSubscriptionRecord] = []
        for row in rows:
            payload = {key: row[key] for key in row.keys()}
            payload["api_key"] = self.pnr_vault.psv_decrypt(str(payload.get("api_key") or ""))
            records.append(PmsSubscriptionRecord.psr_from_dict(payload))
        return records

    def pnr_upsert_subscription(self, pms_record: PmsSubscriptionRecord) -> int:
        payload = pms_record.psr_to_dict()
        payload["api_key"] = self.pnr_vault.psv_encrypt(pms_record.api_key)
        return self.pnr_engine.pdb_execute(
            "INSERT OR REPLACE INTO pms_subscriptions (uid, name, url, enabled, user_agent, api_key, "
            "update_interval_minutes, last_update_time, last_node_count, last_error, traffic_upload, "
            "traffic_download, traffic_total, traffic_expire, sort_index, use_system_proxy, traffic_raw, remark, "
            "created_at, updated_at, last_bridge_count, last_bridge_note) "
            "VALUES (:uid, :name, :url, :enabled, :user_agent, :api_key, :update_interval_minutes, :last_update_time, "
            ":last_node_count, :last_error, :traffic_upload, :traffic_download, :traffic_total, :traffic_expire, "
            ":sort_index, :use_system_proxy, :traffic_raw, :remark, :created_at, :updated_at, "
            ":last_bridge_count, :last_bridge_note)",
            {
                **payload,
                "enabled": 1 if pms_record.enabled else 0,
                "use_system_proxy": 1 if pms_record.use_system_proxy else 0,
            },
        )

    def pnr_delete_subscription(self, pms_uid: str) -> int:
        return self.pnr_engine.pdb_execute("DELETE FROM pms_subscriptions WHERE uid=?", (str(pms_uid),))

    # ------------------------------------------------------------------ 历史与统计
    def pnr_record_latency_history(self, pms_node: PmsProxyNode, pms_mode: str, pms_detail: str = "") -> int:
        return self.pnr_engine.pdb_execute(
            "INSERT INTO pms_latency_history (node_uid, node_name, test_mode, latency_ms, speed_bps, tested_at, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                pms_node.uid, pms_node.pnode_display_name(), str(pms_mode),
                pms_node.latency_ms if pms_node.latency_ms is not None else None,
                float(pms_node.speed_bps or 0.0), pms_now_millis_text(), str(pms_detail)[:500],
            ),
        )

    def pnr_load_latency_history(self, pms_node_uid: str = "", pms_limit: int = 200) -> list[sqlite3.Row]:
        limit = max(1, int(pms_limit))
        if pms_node_uid:
            return self.pnr_engine.pdb_query(
                "SELECT * FROM pms_latency_history WHERE node_uid=? ORDER BY id DESC LIMIT ?",
                (str(pms_node_uid), limit),
            )
        return self.pnr_engine.pdb_query("SELECT * FROM pms_latency_history ORDER BY id DESC LIMIT ?", (limit,))

    def pnr_node_success_stats(self, pms_uids: Sequence[str], pms_sample: int = 10) -> dict[str, dict[str, Any]]:
        """批量读节点历史成功率（v1.8.0：自动切换综合评分用，**不改表结构**）。

        判定口径：`pms_latency_history.latency_ms` 为空或为负 = 那次测试失败，其余算成功
        （与 `pnr_record_latency_history` 的写入口径一致：失败时节点的 latency_ms 是 None）。

        返回 `{uid: {"total": 总次数, "success": 成功次数, "rate": 成功率 0~1,
        "recent_fails": 最近连续失败次数, "avg_ms": 成功样本平均延迟或 None}}`；
        **没有任何历史的节点不出现在返回里**，调用方按「无历史、不惩罚」处理。
        """
        uids = [str(item) for item in (pms_uids or []) if str(item)]
        if not uids:
            return {}
        sample = max(1, min(50, int(pms_sample)))
        stats: dict[str, dict[str, Any]] = {}
        try:
            # ★ v1.9.58：**必须分批**。调用方会把整库节点（4 万+）的 uid 传进来查历史，
            #   一条 `WHERE node_uid IN (?,?,?…)` 直接超 SQLite 变量上限（999）抛
            #   `too many SQL variables`，与「一键清除全部测试成绩」是同一条根因。
            #   分批查、结果合并（GROUP BY 的结果按批累加即可，无需跨批二次聚合）。
            for start in range(0, len(uids), PMS_SQL_MAX_VARIABLES):
                batch = uids[start:start + PMS_SQL_MAX_VARIABLES]
                placeholders = ",".join("?" * len(batch))
                for row in self.pnr_engine.pdb_query(
                    "SELECT node_uid, COUNT(*) AS total, "
                    "SUM(CASE WHEN latency_ms IS NOT NULL AND latency_ms >= 0 THEN 1 ELSE 0 END) AS ok, "
                    "SUM(CASE WHEN latency_ms IS NOT NULL AND latency_ms >= 0 THEN latency_ms ELSE 0 END) AS sum_ms "
                    f"FROM pms_latency_history WHERE node_uid IN ({placeholders}) GROUP BY node_uid",
                    tuple(batch),
                ):
                    total = int(row["total"] or 0)
                    ok = int(row["ok"] or 0)
                    stats[str(row["node_uid"])] = {
                        "total": total,
                        "success": ok,
                        "rate": (ok / total) if total else 0.0,
                        "recent_fails": 0,
                        "avg_ms": int(row["sum_ms"] / ok) if ok else None,
                    }
                # 「最近连续失败」也只在本批内数（按 id 倒序取本批样本）
                recent = self.pnr_engine.pdb_query(
                    "SELECT node_uid, latency_ms FROM pms_latency_history "
                    f"WHERE node_uid IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                    tuple(batch) + (len(batch) * sample,),
                )
                # 从最新一条往回数**连续**失败：遇到第一条成功就停（只统计「最近」这一段）。
                # ★ 这段必须留在批内循环里：`recent` 是每批各自查的，
                #   挪到循环外会只按最后一批算，且 `recent` 变量在 except 分支不可达。
                counting = {uid: True for uid in stats}
                for row in recent:
                    uid = str(row["node_uid"])
                    item = stats.get(uid)
                    if item is None or not counting.get(uid, False):
                        continue
                    value = row["latency_ms"]
                    if value is None or int(value) < 0:
                        item["recent_fails"] = int(item["recent_fails"]) + 1
                        if int(item["recent_fails"]) >= sample:
                            counting[uid] = False
                    else:
                        counting[uid] = False
        except sqlite3.Error as exc:
            self.pnr_engine.pdb_logger.warning("读取节点历史成功率失败：%s", exc)
            return {}
        return stats

    def pnr_record_connection_history(self, pms_rows: Sequence[tuple[Any, ...]]) -> int:
        if not pms_rows:
            return 0
        return self.pnr_engine.pdb_execute_many(
            "INSERT INTO pms_connection_history (node_uid, host, destination, outbound, bytes_up, bytes_down, "
            "started_at, ended_at, duration_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            pms_rows,
        )

    def pnr_load_recent_connections(self, pms_limit: int = 200) -> list[sqlite3.Row]:
        return self.pnr_engine.pdb_query(
            "SELECT * FROM pms_connection_history ORDER BY id DESC LIMIT ?", (max(1, int(pms_limit)),)
        )

    def pnr_record_traffic_sample(self, pms_sample: dict[str, int]) -> int:
        return self.pnr_engine.pdb_execute(
            "INSERT INTO pms_traffic_samples (sampled_at, proxy_up, proxy_down, direct_up, direct_down, active_nodes) "
            "VALUES (?,?,?,?,?,?)",
            (
                pms_now_text(), pms_safe_int(pms_sample.get("proxy_up"), 0), pms_safe_int(pms_sample.get("proxy_down"), 0),
                pms_safe_int(pms_sample.get("direct_up"), 0), pms_safe_int(pms_sample.get("direct_down"), 0),
                pms_safe_int(pms_sample.get("active_nodes"), 0),
            ),
        )

    def pnr_load_traffic_samples(self, pms_limit: int = 120) -> list[sqlite3.Row]:
        return self.pnr_engine.pdb_query(
            "SELECT * FROM pms_traffic_samples ORDER BY id DESC LIMIT ?", (max(1, int(pms_limit)),)
        )

    # ------------------------------------------------------------------ 备份 / 恢复
    def pnr_export_payload(self) -> dict[str, Any]:
        """导出全部节点 / 组 / 订阅（明文凭据，便于跨机迁移）。"""
        return {
            "app": PMS_APP_EN_NAME,
            "version": PMS_APP_VERSION,
            "exported_at": pms_now_text(),
            "nodes": [node.pnode_to_dict() for node in self.pnr_load_all_nodes()],
            "groups": [group.pgr_to_dict() for group in self.pnr_load_groups()],
            "subscriptions": [record.psr_to_dict() for record in self.pnr_load_subscriptions()],
        }

    def pnr_import_payload(self, pms_payload: dict[str, Any], pms_replace: bool = False) -> dict[str, int]:
        """导入备份：返回各类别数量。replace=True 时先清空。"""
        nodes = [PmsProxyNode.pnode_from_dict(item) for item in pms_payload.get("nodes", []) if isinstance(item, dict)]
        groups = [PmsGroupRecord.pgr_from_dict(item) for item in pms_payload.get("groups", []) if isinstance(item, dict)]
        subs = [PmsSubscriptionRecord.psr_from_dict(item) for item in pms_payload.get("subscriptions", []) if isinstance(item, dict)]
        if pms_replace:
            self.pnr_engine.pdb_execute("DELETE FROM pms_nodes")
            self.pnr_engine.pdb_execute("DELETE FROM pms_groups")
            self.pnr_engine.pdb_execute("DELETE FROM pms_subscriptions")
        self.pnr_upsert_nodes(nodes)
        for group in groups:
            self.pnr_upsert_group(group)
        for record in subs:
            self.pnr_upsert_subscription(record)
        return {"nodes": len(nodes), "groups": len(groups), "subscriptions": len(subs)}

    # ------------------------------------------------------------------ 节点健康检查（v1.9.37）
    def pnr_set_enabled_manual(self, pms_uids: Sequence[str], pms_enabled: bool) -> int:
        """**用户手动**启用 / 禁用：同时清掉健康检查那套状态。

        为什么不能直接调 `pnr_update_enabled`：
            · 手动禁用 → 这个节点从此永不参与健康检查，`health_auto_disabled` 必须一起清 0，
              否则它会被当成「程序自动禁用」而在下个周期被复活（验收标准第 1 条）；
            · 手动启用 → 连续失败计数清零、排期清空，恢复正常参与（需求第六节第 2 条）。
        """
        if not pms_uids:
            return 0
        updated = 0
        for chunk_start in range(0, len(pms_uids), 400):
            chunk = [str(item) for item in pms_uids[chunk_start:chunk_start + 400]]
            marks = ", ".join("?" for _ in chunk)
            updated += self.pnr_engine.pdb_execute(
                f"UPDATE pms_nodes SET enabled=?, health_auto_disabled=0, health_fail_streak=0, "
                f"health_disabled_at='', health_next_check_at='', health_last_note='', updated_at=? "
                f"WHERE uid IN ({marks})",
                [1 if pms_enabled else 0, pms_now_text()] + chunk,
            )
        return updated

    def pnr_health_targets(
        self,
        pms_only_auto: bool = False,
        pms_due_before: str = "",
        pms_limit: int = 0,
    ) -> list[PmsProxyNode]:
        """取健康检查的候选节点（验收标准第 1、2 条的口径就写在这条 SQL 里）。

        候选 = 「正常启用」+「被健康检查自动禁用」；**用户手动禁用的节点永远不在里面**。
        `pms_only_auto=True` → 只取被自动禁用的那批（「只检查自动禁用的节点」入口用）。
        `pms_due_before` 非空 → 只取「下次检查时间为空或已到点」的节点（分片铺开用）。
        `pms_limit` > 0 → 限制本批数量（几万节点必须分批探测，不能一次全派）。
        """
        where = ["(enabled=1 OR health_auto_disabled=1)"]
        params: list[Any] = []
        if pms_only_auto:
            where.append("health_auto_disabled=1")
        if pms_due_before:
            where.append("(health_next_check_at='' OR health_next_check_at<=?)")
            params.append(str(pms_due_before))
        sql = (
            "SELECT * FROM pms_nodes WHERE " + " AND ".join(where)
            + " ORDER BY health_next_check_at ASC, sort_index ASC, created_at ASC"
        )
        if int(pms_limit or 0) > 0:
            sql += " LIMIT ?"
            params.append(int(pms_limit))
        return [self.pnr_row_to_node(row) for row in self.pnr_engine.pdb_query(sql, tuple(params))]

    def pnr_update_health(self, pms_rows: Sequence[dict[str, Any]]) -> int:
        """批量写健康检查状态（每行一个 dict：uid / streak / auto / disabled_at / next_check_at / note）。

        只动 `PMS_NODE_HEALTH_COLUMNS` 五列 + `enabled`（自动禁用/复活会改它），
        **不动测试成绩列**——成绩由既有的 `pnr_update_test_result` 负责，两者互不干扰。
        """
        if not pms_rows:
            return 0
        sql = (
            "UPDATE pms_nodes SET enabled=?, health_fail_streak=?, health_auto_disabled=?, "
            "health_disabled_at=?, health_next_check_at=?, health_last_note=?, updated_at=? WHERE uid=?"
        )
        rows = [
            (
                1 if item.get("enabled", 1) else 0,
                int(item.get("streak", 0) or 0),
                1 if item.get("auto_disabled", False) else 0,
                str(item.get("disabled_at", "") or ""),
                str(item.get("next_check_at", "") or ""),
                str(item.get("note", "") or ""),
                pms_now_text(),
                str(item.get("uid", "")),
            )
            for item in pms_rows if str(item.get("uid", ""))
        ]
        return self.pnr_engine.pdb_execute_many(sql, rows)

    def pnr_health_counts(self, pms_due_before: str = "") -> dict[str, int]:
        """健康检查概况（状态栏 / 设置页用）：启用 / 自动禁用 / 手动禁用 / 到点待检查。"""
        def _count(pms_sql: str, pms_params: tuple[Any, ...] = ()) -> int:
            row = self.pnr_engine.pdb_query_one(pms_sql, pms_params)
            return int(row[0]) if row is not None else 0

        counts = {
            "enabled": _count("SELECT COUNT(*) FROM pms_nodes WHERE enabled=1"),
            "auto_disabled": _count("SELECT COUNT(*) FROM pms_nodes WHERE health_auto_disabled=1"),
            "manual_disabled": _count("SELECT COUNT(*) FROM pms_nodes WHERE enabled=0 AND health_auto_disabled=0"),
            "total": _count("SELECT COUNT(*) FROM pms_nodes"),
        }
        if pms_due_before:
            counts["due"] = _count(
                "SELECT COUNT(*) FROM pms_nodes WHERE (enabled=1 OR health_auto_disabled=1) "
                "AND (health_next_check_at='' OR health_next_check_at<=?)", (str(pms_due_before),),
            )
        return counts

    # ------------------------------------------------------------------ SSE 事件落库（v1.9.54，审计表第 22 项）
    # 为什么落在 `pms_meta` 而不是新建表：`pms_meta` 是现成的 KV 表，键名 `event.log.<seq>`
    # （seq 零填充 20 位）字典序就等于 seq 序，`ORDER BY meta_key` 天然是按时间取；
    # 这样**不动 schema、不用升 schema_version、不用写迁移**，与多轮断点同一套路子。
    def pnr_event_log_append(self, pms_seq: int, pms_event_json: str) -> None:
        """追加一条事件（**只允许主线程调用**，HTTP 线程只读）。"""
        self.pnr_engine.pdb_set_meta(PMS_EVENT_LOG_KEY_PREFIX + f"{max(0, int(pms_seq)):020d}", str(pms_event_json))

    def pnr_event_log_load(self, pms_since: int = 0, pms_max_rows: int = 0) -> list[str]:
        """取 seq 严格大于 `pms_since` 的事件 JSON（**只读**，HTTP 线程也走这里）。

        `pms_max_rows > 0` 时先按倒序取这么多条、再翻回时间正序返回——客户端落后太多时
        只需要「最靠近 since 的那一段」，不必把整份日志读进内存。
        """
        try:
            since = max(0, int(pms_since))
        except (TypeError, ValueError):
            since = 0
        limit = max(0, int(pms_max_rows or 0))
        sql = "SELECT meta_key, meta_value FROM pms_meta WHERE meta_key LIKE ? AND meta_key > ? ORDER BY meta_key"
        params: tuple[Any, ...] = (PMS_EVENT_LOG_KEY_PREFIX + "%", PMS_EVENT_LOG_KEY_PREFIX + f"{since:020d}",)
        if limit:
            sql += " DESC LIMIT ?"
            params = params + (limit,)
        try:
            rows = self.pnr_engine.pdb_query(sql, params)
        except sqlite3.Error as exc:
            # 只读补发失败不能让 SSE 断掉：调用方拿到空表就按「环里有什么推什么」处理
            self.pnr_engine.pdb_logger.warning("读取事件落库失败（本次不补发）：%s", exc)
            return []
        items = [str(row["meta_value"] or "") for row in rows]
        if limit:
            items.reverse()
        return [item for item in items if item]

    def pnr_event_log_count(self) -> int:
        """落库事件条数（给自检 / 面板看「有没有无限涨」）。"""
        return int(self.pnr_engine.pdb_scalar(
            "SELECT COUNT(*) FROM pms_meta WHERE meta_key LIKE ?", (PMS_EVENT_LOG_KEY_PREFIX + "%",), 0))

    def pnr_event_log_prune(self, pms_keep: int = PMS_EVENT_LOG_MAX) -> int:
        """把落库事件压到 `pms_keep` 条以内（**只在主线程写时调用**），返回删除行数。"""
        return self.pnr_engine.pdb_prune_prefixed_meta((PMS_EVENT_LOG_KEY_PREFIX,), max(0, int(pms_keep)))
