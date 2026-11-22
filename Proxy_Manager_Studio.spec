# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（备用方案；主推 Nuitka 脚本 0_nuitka_pyside6_clang_auto.bat）。

用法：
    pyinstaller --clean --noconfirm Proxy_Manager_Studio.spec
产物：build_output\\Proxy_Manager_Studio\\Proxy_Manager_Studio.exe

要点：
    · 入口是启动器 Proxy_Manager_Studio.py，实现全在 pms\\ 包里；
      pathex 指向项目根，Analysis 会跟随 `import pms.*` 自动收集全部模块
      （下列 hiddenimports 里显式列出包内模块，防止个别动态引用被漏掉）；
    · icon.ico 存在时作为 exe 图标与附加数据；
    · core\\ 目录整体打包（不想要内置核心就删掉 core 相关条目）；
    · 运行期数据（设置 / 数据库 / 日志）写在 exe 同级目录，注意放到有写权限的位置。
"""

import os
from pathlib import Path

ROOT = Path(os.getcwd())
DATAS = [("软件升级迭代记录.md", "."), (".env.example", ".")]
if (ROOT / "icon.ico").is_file():
    DATAS.append(("icon.ico", "."))
CORE_DIR = ROOT / "core"
if CORE_DIR.is_dir():
    DATAS.append(("core", "core"))

PMS_MODULES = [
    "pms",
    "pms.app",
    "pms.constants",
    "pms.utils",
    "pms.paths",
    "pms.settings",
    "pms.envstore",
    "pms.logbus",
    "pms.models",
    "pms.database",
    "pms.sharelinks",
    "pms.importers",
    "pms.coreprofile",
    "pms.coreconfig",
    "pms.coresys",
    "pms.statsapi",
    "pms.logparse",
    "pms.coreprocess",
    "pms.tempsession",
    "pms.tasks",
    "pms.latency",
    "pms.speedtest",
    "pms.traffic",
    "pms.subscription",
    "pms.download",
    "pms.versioning",
    "pms.theme",
    "pms.icons",
    "pms.optional",
    "pms.gui",
    "pms.gui.widgets",
    "pms.gui.qtmodels",
    "pms.gui.tray",
    "pms.gui.mainwindow",
    "pms.gui.dialogs",
    "pms.gui.dialogs.nodeeditor",
    "pms.gui.dialogs.shareqr",
    "pms.gui.dialogs.importdialog",
    "pms.gui.dialogs.batchops",
    "pms.gui.dialogs.subscriptiondialog",
    "pms.gui.dialogs.settingsdialog",
    "pms.gui.dialogs.aboutdialog",
    "pms.gui.dialogs.versionhistory",
]
HIDDEN_IMPORTS = PMS_MODULES + ["PySide6.QtNetwork", "yaml", "qrcode", "psutil"]
EXCLUDES = ["tkinter", "matplotlib", "numpy", "PIL", "PySide6.QtMultimedia", "PySide6.QtWebEngineCore"]

a = Analysis(
    ["Proxy_Manager_Studio.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Proxy_Manager_Studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="icon.ico" if (ROOT / "icon.ico").is_file() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Proxy_Manager_Studio",
)
