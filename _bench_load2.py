# -*- coding: utf-8 -*-
"""拆分加载耗时：SQL 查询 vs 逐行构造 PmsProxyNode 对象。"""
from __future__ import annotations

import time
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
sys.path.insert(0, str(Path.cwd()))

from pms.database import PmsDatabaseEngine
from pms.settings import PmsSettingsManager
from pms.envstore import PmsSecretVault
from pms.database import PmsNodeRepository


def main():
    app = QApplication([])
    engine = PmsDatabaseEngine(Path("pms_store.db"))
    engine.pdb_open()
    repo = PmsNodeRepository(engine, PmsSecretVault(""))

    # SQL 查询本身
    t0 = time.perf_counter()
    rows = engine.pdb_query("SELECT * FROM pms_nodes ORDER BY sort_index ASC, created_at ASC")
    dt_query = time.perf_counter() - t0
    print(f"SQL 查询 {len(rows)} 行: {dt_query*1000:.1f} ms")

    # 仅构造对象
    t0 = time.perf_counter()
    nodes = [repo.pnr_row_to_node(row) for row in rows]
    dt_build = time.perf_counter() - t0
    print(f"逐行构造 {len(nodes)} 个 PmsProxyNode: {dt_build*1000:.1f} ms")

    # 单对象构造细拆：pnode_from_dict
    t0 = time.perf_counter()
    for node in nodes[:1000]:
        node.pnode_search_text()  # 触发一次属性计算（非缓存预热）
    dt_search = time.perf_counter() - t0
    print(f"1000 次 pnode_search_text(): {dt_search*1000:.1f} ms")


if __name__ == "__main__":
    main()
