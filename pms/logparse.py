#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志解析与连接追踪

把核心输出解析成结构化事件，并维护连接列表（Clash 快照优先，日志解析兜底）。
"""

from __future__ import annotations

from datetime import datetime
from typing import (
    Any,
    Optional,
)
from PySide6.QtCore import (
    QObject,
    Signal,
)
import logging
import re
import threading
import time

from pms.constants import (
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
    PMS_LOGGER_NAME,
    PMS_MAX_CONNECTION_ROWS,
)
from pms.utils import (
    pms_now_millis_text,
    pms_safe_int,
)
from pms.models import PmsConnectionRecord

# --------------------------------------------------------------------------------------
# 核心日志解析 & 连接追踪
# --------------------------------------------------------------------------------------
PMS_LOG_SINGBOX_LINE = re.compile(
    r"^(?:\+\d{4}\s+)?(?:(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+)?"
    r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s*"
    r"(?:\[(?P<conn>\d+)\s+(?P<elapsed>\d+)ms\]\s*)?"
    r"(?:(?P<module>[A-Za-z0-9_\-/\.]+)(?:\[(?P<tag>[^\]]*)\])?:\s*)?"
    r"(?P<message>.*)$"
)

PMS_LOG_XRAY_ACCEPTED = re.compile(
    r"^(?P<ts>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
    r"from\s+(?P<source>\S+)\s+accepted\s+(?P<destination>\S+)\s+\[(?P<chain>[^\]]+)\]$"
)

PMS_LOG_XRAY_DETAIL = re.compile(
    r"^(?P<ts>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
    r"\[(?P<level>[A-Za-z]+)\]\s+(?:\[(?P<conn>\d+)\]\s+)?(?:(?P<module>[A-Za-z0-9_\-/\.]+):\s*)?(?P<message>.*)$"
)

class PmsCoreLogParser:
    """把核心输出行解析成结构化事件（连接、错误、启动标记）。"""

    def __init__(self, pms_logger: Optional[logging.Logger] = None) -> None:
        self.pclp_logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".logparse")

    def pclp_parse(self, pms_line: str) -> dict[str, Any]:
        """解析一行日志，返回事件字典；无法识别时 kind='other'。"""
        line = str(pms_line or "").rstrip("\r\n")
        if not line.strip():
            return {"kind": "empty"}
        event: dict[str, Any] = {"kind": "other", "raw": line, "level": "INFO", "text": line}
        singbox = PMS_LOG_SINGBOX_LINE.match(line)
        if singbox:
            level = str(singbox.group("level") or "INFO")
            module = str(singbox.group("module") or "")
            tag = str(singbox.group("tag") or "")
            message = str(singbox.group("message") or "")
            conn = singbox.group("conn")
            event.update({
                "level": "WARN" if level == "WARN" else level,
                "module": module,
                "tag": tag,
                "message": message,
                "conn_id": str(conn) if conn else "",
                "engine": PMS_CORE_KIND_SINGBOX,
                "text": f"{module}[{tag}]: {message}" if tag else (f"{module}: {message}" if module else message),
            })
            if "sing-box started" in message or "started (" in message:
                event["kind"] = "started"
                return event
            if "inbound connection from" in message:
                event["kind"] = "connection"
                event["phase"] = "inbound_from"
                event["source"] = message.split("inbound connection from", 1)[1].strip()
                return event
            if "inbound connection to" in message:
                event["kind"] = "connection"
                event["phase"] = "inbound_to"
                event["destination"] = message.split("inbound connection to", 1)[1].strip()
                return event
            if "outbound connection to" in message:
                event["kind"] = "connection"
                event["phase"] = "outbound_to"
                event["destination"] = message.split("outbound connection to", 1)[1].strip()
                return event
            if "connection closed" in message or "connection ends" in message:
                event["kind"] = "connection"
                event["phase"] = "closed"
                return event
            if level in ("ERROR", "FATAL") or " error" in message.lower():
                event["kind"] = "error"
            return event
        xray_accepted = PMS_LOG_XRAY_ACCEPTED.match(line)
        if xray_accepted:
            chain = str(xray_accepted.group("chain") or "")
            chain = chain.replace(">>", "->")
            parts = [item.strip() for item in chain.split("->")]
            event.update({
                "kind": "connection",
                "phase": "accepted",
                "engine": PMS_CORE_KIND_XRAY,
                "source": str(xray_accepted.group("source") or ""),
                "destination": str(xray_accepted.group("destination") or ""),
                "inbound": parts[0] if parts else "",
                "outbound": parts[1] if len(parts) > 1 else "",
                "text": f"{xray_accepted.group('source')} → {xray_accepted.group('destination')} [{chain}]",
            })
            return event
        xray_detail = PMS_LOG_XRAY_DETAIL.match(line)
        if xray_detail:
            message = str(xray_detail.group("message") or "")
            level = str(xray_detail.group("level") or "Info")
            module = str(xray_detail.group("module") or "")
            event.update({
                "engine": PMS_CORE_KIND_XRAY,
                "level": level.upper(),
                "module": module,
                "message": message,
                "conn_id": str(xray_detail.group("conn") or ""),
                "text": f"{module}: {message}" if module else message,
            })
            lowered_message = message.lower()
            if "started" in lowered_message and ("xray" in lowered_message or "core" in module.lower() or "sing-box" in lowered_message):
                event["kind"] = "started"
                return event
            if "connection ends" in message or "Connection closed" in message:
                event["kind"] = "connection"
                event["phase"] = "closed"
                return event
            if level.lower() in ("error", "warning"):
                event["kind"] = "error" if level.lower() == "error" else "warning"
                return event
            return event
        if line.startswith("Xray ") or line.startswith("sing-box version"):
            event["kind"] = "banner"
            return event
        lowered = line.lower()
        if "failed to start" in lowered or "failed to load config" in lowered or "fatal" in lowered:
            event["kind"] = "error"
            event["level"] = "ERROR"
            return event
        if "started" in lowered and ("xray" in lowered or "sing-box" in lowered):
            event["kind"] = "started"
        return event

class PmsConnectionTracker(QObject):
    """维护“当前连接”列表：优先用 Clash API 快照，缺失时退化到日志解析。"""

    pct_connections_updated = Signal(list)

    def __init__(self, pms_parent: Optional[QObject] = None) -> None:
        super().__init__(pms_parent)
        self.pct_records: dict[str, PmsConnectionRecord] = {}
        self.pct_lock = threading.RLock()
        self.pct_logger = logging.getLogger(PMS_LOGGER_NAME + ".connections")
        self.pct_last_emit = 0.0
        self.pct_source = "日志解析"
        # v1.9.16：连接面板行数上限（设置项 database.max_connection_rows 用上它）
        self.pct_max_rows = PMS_MAX_CONNECTION_ROWS

    # ------------------------------------------------------------------ 日志路径
    def pct_feed_event(self, pms_event: dict[str, Any]) -> None:
        """处理一条解析后的事件（日志路径）。"""
        if pms_event.get("kind") != "connection":
            return
        engine = str(pms_event.get("engine") or "")
        phase = str(pms_event.get("phase") or "")
        with self.pct_lock:
            self.pct_source = "日志解析"
            if engine == PMS_CORE_KIND_SINGBOX:
                conn_id = str(pms_event.get("conn_id") or "")
                if not conn_id:
                    return
                record = self.pct_records.get(conn_id)
                if record is None:
                    record = PmsConnectionRecord(conn_uid=conn_id, start_time=pms_now_millis_text(), protocol="TCP")
                    self.pct_records[conn_id] = record
                if phase == "inbound_from":
                    record.source = str(pms_event.get("source") or record.source)
                elif phase == "inbound_to":
                    record.destination = str(pms_event.get("destination") or record.destination)
                elif phase == "outbound_to":
                    record.destination = str(pms_event.get("destination") or record.destination)
                    record.outbound = str(pms_event.get("tag") or record.outbound)
                elif phase == "closed":
                    record.is_active = False
                record.host = record.destination
            elif engine == PMS_CORE_KIND_XRAY:
                if phase == "accepted":
                    key = f"{pms_event.get('source')}|{pms_event.get('destination')}"
                    record = PmsConnectionRecord(
                        conn_uid=key,
                        source=str(pms_event.get("source") or ""),
                        destination=str(pms_event.get("destination") or ""),
                        host=str(pms_event.get("destination") or ""),
                        outbound=str(pms_event.get("outbound") or ""),
                        rule=str(pms_event.get("inbound") or ""),
                        start_time=pms_now_millis_text(),
                    )
                    self.pct_records[record.conn_uid] = record
                elif phase == "closed":
                    self.pct_evict_stale(force=True)
            self.pct_maybe_emit()

    def pct_apply_clash_snapshot(self, pms_snapshot: dict[str, Any]) -> list[PmsConnectionRecord]:
        """用 Clash API 快照重建连接列表（包含真实字节数）。"""
        items = pms_snapshot.get("connections") if isinstance(pms_snapshot, dict) else None
        records: list[PmsConnectionRecord] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            chains = item.get("chains")
            record = PmsConnectionRecord(
                conn_uid=str(item.get("id") or ""),
                protocol=str(metadata.get("type") or metadata.get("network") or ""),
                source=f"{metadata.get('sourceIP', '')}:{metadata.get('sourcePort', '')}".strip(":"),
                destination=f"{metadata.get('destinationIP', '')}:{metadata.get('destinationPort', '')}".strip(":"),
                host=str(metadata.get("host") or metadata.get("destinationIP") or ""),
                outbound=" → ".join(str(chain) for chain in chains) if isinstance(chains, list) else "",
                rule=str(item.get("rule") or item.get("rulePayload") or ""),
                upload=pms_safe_int(item.get("upload"), 0),
                download=pms_safe_int(item.get("download"), 0),
                start_time=str(item.get("start") or ""),
                is_active=True,
            )
            try:
                if record.start_time:
                    started = datetime.fromisoformat(record.start_time.replace("Z", "+00:00").split("+")[0])
                    record.duration_ms = int((datetime.now() - started).total_seconds() * 1000)
            except (ValueError, TypeError):
                record.duration_ms = 0
            records.append(record)
        with self.pct_lock:
            self.pct_source = "Clash API"
            self.pct_records = {record.conn_uid: record for record in records if record.conn_uid}
            snapshot = self.pct_snapshot_locked()
        self.pct_connections_updated.emit(snapshot)
        self.pct_last_emit = time.time()
        return snapshot

    # ------------------------------------------------------------------ 公共
    def pct_evict_stale(self, pms_idle_seconds: float = 180.0, pms_force: bool = False) -> None:
        """清理长期无更新的连接（Xray 日志模式下没有可靠的关闭事件）。"""
        if pms_force:
            with self.pct_lock:
                self.pct_records = {key: value for key, value in self.pct_records.items() if value.is_active}
            return
        cutoff = time.time() - max(10.0, float(pms_idle_seconds))
        with self.pct_lock:
            for key in list(self.pct_records.keys()):
                record = self.pct_records[key]
                try:
                    stamp = datetime.strptime(record.start_time[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                except (ValueError, TypeError):
                    stamp = time.time()
                if stamp < cutoff and not record.is_active:
                    self.pct_records.pop(key, None)

    def pct_snapshot_locked(self) -> list[PmsConnectionRecord]:
        records = list(self.pct_records.values())
        records.sort(key=lambda item: (not item.is_active, item.start_time), reverse=False)
        return records[:self.pct_max_rows]

    def pct_set_max_rows(self, pms_rows: int) -> None:
        """改「连接面板最大行数」（v1.9.16：原来写死用 PMS_MAX_CONNECTION_ROWS，设置项形同虚设）。"""
        self.pct_max_rows = max(50, pms_safe_int(pms_rows, PMS_MAX_CONNECTION_ROWS, 50, 5000))

    def pct_snapshot(self) -> list[PmsConnectionRecord]:
        with self.pct_lock:
            return self.pct_snapshot_locked()

    def pct_clear(self) -> None:
        with self.pct_lock:
            self.pct_records.clear()
        self.pct_connections_updated.emit([])

    def pct_active_count(self) -> int:
        with self.pct_lock:
            return sum(1 for item in self.pct_records.values() if item.is_active)

    def pct_maybe_emit(self, pms_min_interval: float = 0.4) -> None:
        """限流发射（日志密集时不至于刷爆界面）。"""
        now = time.time()
        if now - self.pct_last_emit < pms_min_interval:
            return
        self.pct_last_emit = now
        self.pct_connections_updated.emit(self.pct_snapshot())
