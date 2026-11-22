#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：用与 smoke 测试相同的 load_module() 构造主窗口，看后台异步加载到底跑没跑、信号有没有回包。"""
from __future__ import annotations
import sys, time, tempfile, importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = ROOT / "Proxy_Manager_Studio.py"
sys.path.insert(0, str(ROOT))
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QThreadPool

# 与 smoke_test 相同的 load_module
spec = importlib.util.spec_from_file_location("pms_smoke", MAIN)
pms = importlib.util.module_from_spec(spec)
sys.modules["pms_smoke"] = pms
assert spec.loader is not None
spec.loader.exec_module(pms)

def pms_core_search_roots():
    return [ROOT / "core" / "xray", ROOT / "core" / "sing-box", ROOT / "core"]

workdir = Path(tempfile.mkdtemp(prefix="pms_probe_"))
app = QApplication.instance() or QApplication([])
paths = pms.PmsRuntimePaths(workdir, pms_core_roots=pms_core_search_roots())
log_bus = pms.PmsLogBus(paths.prt_log_file, 500)
settings = pms.PmsSettingsManager(paths.prt_settings_file)
settings.psm_set_many({
    "general.single_instance": False,
    "general.auto_start_core": False,
    "general.close_to_tray": False,
    "ui.remember_geometry": False,
}, pms_emit=False)
env_store = pms.PmsEnvStore(paths.prt_env_file)
vault = pms.PmsSecretVault("")
engine = pms.PmsDatabaseEngine(paths.prt_database_file)
engine.pdb_open()
repo = pms.PmsNodeRepository(engine, vault)
repo.pnr_upsert_node(pms.PmsProxyNode(name="界面测试节点", protocol=pms.PMS_PROTOCOL_VMESS,
                                      server="gui.example.com", port=443,
                                      credential_uuid="b831381d-6324-4d53-ad4f-8cda48b30811",
                                      group_name="默认分组"))

print("构造前：没有 pmw_loading 属性可查")
window = pms.PmsMainWindow(paths, settings, env_store, log_bus, engine, repo, vault)
print("构造后 pmw_loading =", window.pmw_loading)
print("构造后 rowCount   =", window.pmw_node_model.rowCount())
print("globalThreadPool activeThreadCount =", QThreadPool.globalInstance().activeThreadCount())
print("globalThreadPool maxThreadCount    =", QThreadPool.globalInstance().maxThreadCount())

for i in range(80):
    app.processEvents()
    time.sleep(0.1)
    if i % 5 == 0 or not window.pmw_loading:
        print(f"  t={i*0.1:.1f}s loading={window.pmw_loading} rowCount={window.pmw_node_model.rowCount()} activeThreads={QThreadPool.globalInstance().activeThreadCount()}")
    if not window.pmw_loading and window.pmw_node_model.rowCount() >= 1:
        print(">>> 加载完成")
        break

print("最终 loading =", window.pmw_loading, "rowCount =", window.pmw_node_model.rowCount())
window.pmw_shutdown_sequence()
