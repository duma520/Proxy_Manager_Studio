# -*- coding: utf-8 -*-
"""离线基准：用真实数据库测量加载 / 筛选 / 排序的真实耗时。"""
from __future__ import annotations

import time
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path.cwd()))

from pms.database import PmsDatabaseEngine
from pms.settings import PmsSettingsManager
from pms.gui.qtmodels import PmsNodeTableModel, PmsNodeFilterProxyModel


def main():
    app = QApplication([])
    db_path = Path("pms_store.db")
    settings = PmsSettingsManager()
    engine = PmsDatabaseEngine(db_path, settings.pms_get_logger() if hasattr(settings, "pms_get_logger") else None)
    # 直接打开引擎并拿到 repo 需要的依赖
    conn = engine.pdb_open()

    # PmsNodeRepository 构造需要 engine + vault
    from pms.envstore import PmsSecretVault
    vault = PmsSecretVault("")
    from pms.database import PmsNodeRepository
    repo = PmsNodeRepository(engine, vault)

    # 1) 真实加载
    t0 = time.perf_counter()
    nodes = repo.pnr_load_all_nodes()
    dt_load = time.perf_counter() - t0
    print(f"节点总数: {len(nodes)}")
    print(f"pnr_load_all_nodes(): {dt_load*1000:.1f} ms")

    model = PmsNodeTableModel()
    t0 = time.perf_counter()
    model.pntm_set_nodes(nodes)
    dt_set = time.perf_counter() - t0
    print(f"pntm_set_nodes(): {dt_set*1000:.1f} ms")

    t0 = time.perf_counter()
    while not model.pntm_warm_filter_text_done():
        QApplication.processEvents()
        time.sleep(0.005)
    print(f"搜索串预热完成: {(time.perf_counter()-t0)*1000:.1f} ms")

    proxy = PmsNodeFilterProxyModel()
    proxy.setSourceModel(model)
    t0 = time.perf_counter()
    proxy.pnfp_set_filters("", "", "test", False, False, False)
    proxy.pnfp_rebuild()
    dt_filter = time.perf_counter() - t0
    print(f"关键字筛选重建: {dt_filter*1000:.1f} ms (结果 {proxy.rowCount()} 行)")

    t0 = time.perf_counter()
    model.pntm_sort_column = 5
    model.pntm_sort_in_place()
    dt_sort = time.perf_counter() - t0
    print(f"全量按延迟排序: {dt_sort*1000:.1f} ms")


if __name__ == "__main__":
    main()
