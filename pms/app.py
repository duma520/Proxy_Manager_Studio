#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""程序入口与装配

QApplication 初始化、服务装配、快捷键、单实例、主窗口启动、
命令行辅助模式（--version / --selfcheck / --backup-version <说明>）。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import (
    Optional,
    Sequence,
)
from PySide6.QtCore import (
    QLibraryInfo,
    QLocale,
    QThreadPool,
    QTimer,
    Qt,
    QTranslator,
)
from PySide6.QtGui import (
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import QApplication

from pms.constants import (
    PMS_APP_BUILD_DATE,
    PMS_APP_NAME,
    PMS_APP_ORG,
    PMS_APP_VERSION,
    PMS_LOGGER_NAME,
    PMS_MAX_LOG_LINES,
    PMS_VERSION_BACKUP_NOTE_PLACEHOLDER,
)
from pms.utils import pms_now_text
from pms.paths import PmsRuntimePaths
from pms.settings import PmsSettingsManager
from pms.envstore import (
    PmsEnvStore,
    PmsSecretVault,
)
from pms.logbus import PmsLogBus
from pms.maintenance import (
    PmsMaintenanceSignals,
    PmsMaintenanceTask,
    pms_maintenance_summary_text,
)
from pms.database import (
    PmsDatabaseEngine,
    PmsNodeRepository,
)
from pms.coresys import PmsCoreLocator
from pms.versioning import (
    PmsSingleInstanceGuard,
    PmsVersionBackupHelper,
)
from pms.icons import pms_load_app_icon
from pms.gui.mainwindow import PmsMainWindow

# --------------------------------------------------------------------------------------
# 快捷键
# --------------------------------------------------------------------------------------
def pms_install_shortcuts(pms_window: PmsMainWindow) -> None:
    """给主窗口安装快捷键（全部可在界面上找到对应动作）。"""
    bindings = [
        ("F5", "启动 / 停止核心", pms_window.pmw_toggle_core),
        ("F6", "系统代理开关", lambda: pms_window.pmw_toggle_system_proxy(not pms_window.pmw_settings.psm_get_bool("system_proxy.enabled", False))),
        ("F7", "测试选中节点延迟", pms_window.pmw_test_latency_selected),
        ("Shift+F7", "测试全部节点延迟", pms_window.pmw_test_latency_all),
        ("F8", "测试选中节点速度", pms_window.pmw_test_speed_selected),
        ("Shift+F8", "测试全部节点速度", pms_window.pmw_test_speed_all),
        ("F9", "连接选中节点", pms_window.pmw_connect_selected_node),
        ("Ctrl+N", "新增节点", pms_window.pmw_add_node),
        ("Ctrl+E", "编辑选中节点", pms_window.pmw_edit_node),
        ("Ctrl+I", "导入节点", pms_window.pmw_open_import_dialog),
        ("Ctrl+V", "粘贴导入（从剪贴板）", pms_window.pmw_paste_import_from_clipboard),
        ("Ctrl+U", "订阅管理", pms_window.pmw_open_subscription_dialog),
        ("Ctrl+W", "导出节点", pms_window.pmw_export_nodes),
        ("Ctrl+Shift+V", "查看版本记录", pms_window.pmw_open_version_history),
        ("Ctrl+,", "打开设置", pms_window.pmw_open_settings_dialog),
        ("Ctrl+L", "清空日志面板", pms_window.pmw_clear_log_panel),
        ("Ctrl+R", "重新加载节点", pms_window.pmw_reload_nodes_async),
    ]
    for sequence, description, handler in bindings:
        shortcut = QShortcut(QKeySequence(sequence), pms_window)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(handler)
        shortcut.setWhatsThis(description)
    # v1.6.7：Ctrl+C 复制选中节点分享链接。
    # 只挂在节点表上（WidgetWithChildrenShortcut），避免抢走搜索框 / 日志框里的正常文本复制。
    copy_shortcut = QShortcut(QKeySequence("Ctrl+C"), pms_window.pmw_node_table)
    copy_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    copy_shortcut.activated.connect(pms_window.pmw_copy_node_link)
    copy_shortcut.setWhatsThis("复制选中节点分享链接")
    # v1.7.0：Ctrl+A = 全选**全部筛选结果**（跨页）。
    # 只挂在节点表上，避免抢走搜索框 / 日志框里的正常全选。
    select_all_shortcut = QShortcut(QKeySequence("Ctrl+A"), pms_window.pmw_node_table)
    select_all_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    select_all_shortcut.activated.connect(pms_window.pmw_select_all_filtered)
    select_all_shortcut.setWhatsThis("选中全部筛选结果")
    # v1.9.76 方案 B：TgProxy / TgWebProxy 两个专属标签页也挂同一对快捷键
    # （WidgetWithChildrenShortcut 只对「焦点在该表子树内」生效，各挂各的互不冲突）
    for _pmwt_view in getattr(pms_window, "pmwt_views", {}).values():
        _pmwt_table = getattr(_pmwt_view, "pmwt_table", None)
        if _pmwt_table is None:
            continue
        _pmwt_copy = QShortcut(QKeySequence("Ctrl+C"), _pmwt_table)
        _pmwt_copy.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        _pmwt_copy.activated.connect(pms_window.pmw_copy_node_link)
        _pmwt_copy.setWhatsThis("复制选中节点分享链接")
        _pmwt_select = QShortcut(QKeySequence("Ctrl+A"), _pmwt_table)
        _pmwt_select.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        _pmwt_select.activated.connect(pms_window.pmw_select_all_filtered)
        _pmwt_select.setWhatsThis("选中全部筛选结果")

# --------------------------------------------------------------------------------------
# 程序入口
# --------------------------------------------------------------------------------------
def pms_setup_high_dpi() -> None:
    """高分屏适配：只在用户没显式指定时补上缩放开关，**不再手动设 DPI awareness**。

    v1.9.67 修复：这里原来在 `QApplication` 之前调 `SetProcessDpiAwareness(2)`，
    而 Qt6 的 `QApplication` 构造时会自己调 `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`。
    Windows 规定「进程 DPI awareness 只能声明一次」，第二次调用必然返回
    `E_ACCESSDENIED`（控制台里那两行 `SetProcessDpiAwarenessContext() failed: 拒绝访问`）。
    程序功能不受影响，但每次都刷 warning 很吓人。Qt6 默认就是 Per-Monitor v2，
    让 Qt 自己去设即可，这里删掉手动调用以消除冲突。
    """
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

def pms_install_qt_translator(app: QApplication) -> bool:
    """装入 Qt 自带的中文翻译文件，使原生控件（弹窗 / 文件对话框 / 输入对话框）显示中文。

    读取设置项 `general.language`：
      - 中文化值：zh_CN / zh / Chinese / 中文 / cn → 装入 qtbase_zh_CN.qm + qt_zh_CN.qm；
      - 其它（含 en / 空）→ 不装载翻译（保持英文）；
      - 设置项缺失时回退到默认值（constants 里默认 zh_CN），即默认中文。

    翻译文件优先从 PySide6 安装目录的 translations 取（运行时自适应，打包后也能用），
    同时回退到程序根目录下的 translations/（便于自带定制）。加载失败一律静默忽略，
    绝不让翻译问题阻断启动。返回是否成功装上了中文翻译。
    """
    from pms.constants import PMS_APP_VERSION  # 仅用于日志标记，避免顶层循环依赖
    settings = None
    try:
        from pms.settings import PmsSettingsManager
        from pms.paths import PmsRuntimePaths
        settings = PmsSettingsManager(PmsRuntimePaths().prt_settings_file)
    except Exception:  # noqa: BLE001
        settings = None
    lang = "zh_CN"
    if settings is not None:
        try:
            lang = (settings.psm_get_str("general.language", "zh_CN") or "zh_CN").strip().lower()
        except Exception:  # noqa: BLE001
            lang = "zh_CN"
    chinese = lang in ("zh_cn", "zh", "chinese", "中文", "cn", "zh-hans")

    if not chinese:
        logging.getLogger(PMS_LOGGER_NAME).info(
            "general.language=%s，不装载 Qt 中文翻译（保持原生英文控件）", lang
        )
        return False

    translations_dirs = []
    try:
        tp = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if tp:
            translations_dirs.append(tp)
    except Exception:  # noqa: BLE001
        pass
    # 程序根目录自带 translations/（覆盖 / 定制用）
    try:
        from pms.paths import PmsRuntimePaths
        translations_dirs.append(str(PmsRuntimePaths().prt_root / "translations"))
    except Exception:  # noqa: BLE001
        pass

    locale = QLocale(QLocale.Language.Chinese, QLocale.Country.China)
    loaded = 0
    installed = []
    for name in ("qtbase", "qt"):
        for d in translations_dirs:
            tr = QTranslator()
            try:
                if tr.load(locale, name, "_", d) and tr.isEmpty() is False:
                    if app.installTranslator(tr):
                        installed.append(tr)
                        loaded += 1
                    break
            except Exception:  # noqa: BLE001
                continue
    if loaded:
        #  translators 须随 app 生命周期存活，挂到 app 上防止被 GC
        app.setProperty("_pms_qt_translators", installed)
        logging.getLogger(PMS_LOGGER_NAME).info(
            "已装载 Qt 中文翻译（%d 个：qtbase/qt），原生控件显示中文", loaded
        )
        return True
    logging.getLogger(PMS_LOGGER_NAME).warning(
        "未找到可用的 Qt 中文翻译文件（qt_zh_CN.qm / qtbase_zh_CN.qm），原生控件仍为英文"
    )
    return False

def pms_run_application(pms_argv: Optional[Sequence[str]] = None) -> int:
    """创建 QApplication、初始化全部服务并进入事件循环。"""
    pms_setup_high_dpi()
    app = QApplication(list(pms_argv) if pms_argv is not None else sys.argv)
    # v1.9.72：装入 Qt 自带的中文翻译，把 QMessageBox / 文件对话框 / 输入对话框等
    # 原生控件的英文（OK / Cancel / Yes / No / Browse…）变成中文。
    # 以前 general.language 默认虽是 zh_CN，但全代码没有读取点，语言设置是摆设；
    # 这里真正按该设置切换：zh_CN / zh / 中文 走中文，其余（含 en）不装载翻译。
    pms_install_qt_translator(app)
    app.setApplicationName(PMS_APP_NAME)
    app.setApplicationDisplayName(PMS_APP_NAME)
    app.setOrganizationName(PMS_APP_ORG)
    app.setApplicationVersion(PMS_APP_VERSION)

    paths = PmsRuntimePaths()
    log_bus = PmsLogBus(paths.prt_log_file, PMS_MAX_LOG_LINES)
    logger = logging.getLogger(PMS_LOGGER_NAME)
    logger.info("========== %s v%s 启动（%s）==========", PMS_APP_NAME, PMS_APP_VERSION, pms_now_text())
    settings = PmsSettingsManager(paths.prt_settings_file)
    env_store = PmsEnvStore(paths.prt_env_file)
    vault_password = env_store.pes_get("PMS_DB_PASSWORD", "")
    vault = PmsSecretVault(vault_password)
    if not vault_password:
        logger.info("未配置 PMS_DB_PASSWORD，节点凭据以明文保存在本地 SQLite（可在 .env 中开启加密）")
    engine = PmsDatabaseEngine(paths.prt_database_file, logger)
    engine.pdb_open()
    repo = PmsNodeRepository(engine, vault)

    icon = pms_load_app_icon(paths)
    if icon.isNull():
        logger.info("icon.ico 不存在或不可读，窗口图标保持为空（符合“存在才用”的约定）")
    else:
        app.setWindowIcon(icon)

    guard: Optional[PmsSingleInstanceGuard] = None
    if settings.psm_get_bool("general.single_instance", True):
        guard = PmsSingleInstanceGuard()
        if not guard.psig_try_acquire():
            logger.info("检测到已运行的实例，本次启动自动退出")
            engine.pdb_close()
            log_bus.plb_shutdown()
            return 0

    window = PmsMainWindow(paths, settings, env_store, log_bus, engine, repo, vault)
    pms_install_shortcuts(window)
    window.pmw_setup_tray()
    if guard is not None:
        guard.psig_server.newConnection.connect(window.pmw_restore_from_tray)
    window.show()
    # v1.9.33：启动后自动清理过期的日志 / 临时文件（保留天数见设置；后台线程，不拖慢启动）
    if settings.psm_get_bool("maintenance.auto_clean_enabled", True):
        maintenance = PmsMaintenanceSignals(window)
        maintenance.pmn_finished.connect(lambda result: window.pmw_show_message(pms_maintenance_summary_text(result)))
        QThreadPool.globalInstance().start(PmsMaintenanceTask(
            maintenance, paths, settings.psm_get_int("maintenance.retention_days", 7),
        ))
    if settings.psm_get_bool("general.auto_start_core", False):
        # v1.9.72 卡顿修复：核心启动依赖节点库（要连「上次使用的节点」），而启动加载已改后台异步，
        # 不能再靠固定 600ms 定时器抢跑——改为由启动加载完成的回调统一触发（见 pmw_on_startup_after_load）。
        window.pmw_startup_auto_core = True
    else:
        window.pmw_startup_auto_core = False
    if settings.psm_get_bool("subscription.update_on_launch", False):
        # v1.9.72：同上，启动订阅也依赖节点库就绪，改由启动加载完成回调触发，避免在空库上跑。
        window.pmw_startup_auto_subscription = True
    else:
        window.pmw_startup_auto_subscription = False
    exit_code = app.exec()
    if guard is not None:
        guard.psig_release()
    try:
        log_bus.plb_shutdown()
    except Exception:  # noqa: BLE001
        pass
    return int(exit_code)

def main() -> int:
    """命令行入口：支持 --version / --selfcheck / --backup-version 三种辅助模式。"""
    argv = list(sys.argv[1:])
    if "--version" in argv:
        print(f"{PMS_APP_NAME} v{PMS_APP_VERSION}（构建日期 {PMS_APP_BUILD_DATE}）")
        return 0
    if "--selfcheck" in argv:
        paths = PmsRuntimePaths()
        locator = PmsCoreLocator(paths)
        print(f"程序根目录：{paths.prt_root}")
        print(f"设置文件：{paths.prt_settings_file}（存在={paths.prt_settings_file.is_file()}）")
        print(f"数据库文件：{paths.prt_database_file}（存在={paths.prt_database_file.is_file()}）")
        print(f"icon.ico：{'存在' if paths.prt_icon_exists() else '不存在（不使用图标）'}")
        print(f"迭代记录：{'存在' if paths.prt_doc_file.is_file() else '不存在'}")
        for kind, path in locator.pcl_find_any(None).items():
            version = locator.pcl_detect_version(path) if path else "-"
            print(f"核心 {kind}：{path if path else '未找到'}（版本 {version}）")
        env_store = PmsEnvStore(paths.prt_env_file)
        snapshot = env_store.pes_redacted_snapshot()
        print(f".env 项：{snapshot if snapshot else '（空）'}")
        return 0
    if "--backup-version" in argv:
        note = ""
        index = argv.index("--backup-version")
        if index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            note = argv[index + 1]
        paths = PmsRuntimePaths()
        helper = PmsVersionBackupHelper(paths)
        report = helper.pvbh_consistency_report()
        ok, message = helper.pvbh_backup_source(PMS_APP_VERSION, note or PMS_VERSION_BACKUP_NOTE_PLACEHOLDER)
        print(message)
        print(
            f"版本一致性：代码 v{report['code_version']}，文档 "
            + (f"v{report['doc_version']}" if report["doc_version"] else "未读到")
            + f"，备份 {report['backup_count']} 份"
        )
        return 0 if ok else 1
    return pms_run_application()
