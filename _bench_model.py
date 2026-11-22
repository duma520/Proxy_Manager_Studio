# -*- coding: utf-8 -*-
"""离线基准：测量节点表格模型热路径的耗时（不启动完整 GUI）。

重点验证两个假设：
  H1. model.data() 的 ToolTipRole 路径每格调 pnode_validate()（UUID/JSON 解析），
      若每帧每可见格都被问到，会在大库下造成卡顿；
  H2. 筛选行表重建 pnfp_compute_rows() 在大库下是否昂贵。
"""
from __future__ import annotations

import time
import uuid
from PySide6.QtCore import Qt, QModelIndex

from pms.models import PmsProxyNode
from pms.gui.qtmodels import PmsNodeTableModel


def make_node(i: int) -> PmsProxyNode:
    data = {
        "uid": uuid.uuid4().hex,
        "name": f"node-{i}",
        "protocol": "vmess",
        "server": f"1.2.{i // 256}.{i % 256}",
        "port": 1000 + i,
        "credential_uuid": str(uuid.uuid4()),
        "group_name": "默认分组",
        "subscription_name": "sub1",
        "latency_ms": i % 500,
        "speed_bps": (i % 1000) * 1000,
        "enabled": True,
    }
    return PmsProxyNode.pnode_from_dict(data)


def main():
    for total in (200, 57_500):
        nodes = [make_node(i) for i in range(total)]
        model = PmsNodeTableModel()
        model.pntm_set_nodes(nodes)

        # ---- H1: data() 各角色，对「可见页」约 50 行 × 列数 调一次 ----
        page_rows = min(50, total)
        cols = 13
        idx = QModelIndex()

        def time_role(role):
            t0 = time.perf_counter()
            for r in range(page_rows):
                for c in range(cols):
                    model.data(model.index(r, c), role)
            return time.perf_counter() - t0

        roles = {
            "DisplayRole": Qt.ItemDataRole.DisplayRole,
            "ForegroundRole": Qt.ItemDataRole.ForegroundRole,
            "TextAlignmentRole": Qt.ItemDataRole.TextAlignmentRole,
            "ToolTipRole(含validate)": Qt.ItemDataRole.ToolTipRole,
        }
        print(f"\n=== 节点数 {total} ===")
        for name, role in roles.items():
            dt = time_role(role)
            per = dt / (page_rows * cols) * 1e6
            print(f"  {name:28s}: 全页 {dt*1000:.1f} ms  ({per:.1f} µs/格)")
        # 单行 tooltip 单次
        t0 = time.perf_counter()
        for _ in range(1000):
            model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole)
        print(f"  ToolTip 单次 ×1000: {(time.perf_counter()-t0)*1000:.1f} ms  -> {((time.perf_counter()-t0)*1000/1000):.3f} ms/次")


if __name__ == "__main__":
    main()
