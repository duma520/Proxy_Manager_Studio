#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心进程控制

生成配置 → 校验 → 启动/停止/重启/自动重启 → 残留进程清理，并把核心输出转成事件。
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import (
    Any,
    Optional,
)
from PySide6.QtCore import (
    QObject,
    QProcess,
    QTimer,
    Signal,
)
import logging
import os
import json
import subprocess
import time

from pms.optional import (
    PMS_HAS_PSUTIL,
    pms_optional_psutil,
)
from pms.constants import (
    PMS_CORE_KIND_XRAY,
    PMS_CORE_RESTART_MAX_DELAY_MS,
    PMS_CORE_RESTART_MIN_DELAY_MS,
    PMS_CORE_RESTART_RESET_AFTER_SECONDS,
    PMS_IS_WINDOWS,
    PMS_LOGGER_NAME,
    PMS_PROCESS_STOP_GRACE_MS,
)
from pms.utils import (
    pms_human_duration,
    pms_safe_float,
    pms_safe_int,
    pms_slugify_filename,
)
from pms.paths import PmsRuntimePaths
from pms.coreprofile import PmsCoreLaunchProfile
from pms.coreconfig import (
    PmsCoreConfigWriter,
    PmsSingBoxConfigBuilder,
    PmsXrayConfigBuilder,
)
from pms.coresys import PmsCoreLocator
from pms.tor_bridge import (  # v1.9.43：Tor 出口只支持 sing-box，拦截文案放 tor_bridge.py
    PMS_TOR_EXE_NAME,
    PMS_TOR_LYREBIRD_NAME,
    PMS_TOR_MEEK_NAME,
    PMS_TOR_SNOWFLAKE_NAME,
    ptb_binaries_state,
    ptb_core_kind_error,
    ptb_tor_paths,
)

# --------------------------------------------------------------------------------------
# 进程身份比对（残留核心清理只认「自己启动过的那一个」）
# --------------------------------------------------------------------------------------
def pcp_normalize_path(pms_path: Any) -> str:
    """把路径归一化成可比较的形式（Windows 下大小写、斜杠、相对路径都不敏感）。"""
    try:
        return os.path.normcase(os.path.abspath(str(pms_path)))
    except (OSError, ValueError):
        return str(pms_path or "").strip().lower()

def pcp_process_identity(pms_pid: int) -> Optional[tuple[str, float]]:
    """查进程身份，返回 (归一化可执行文件路径, 进程创建时间)。

    进程已退出、查不到或无权查看时返回 None（调用方一律按「查不到就不动手」处理）。
    没有 psutil 时退化为 PowerShell CIM 单次查询（拿不到创建时间时用 0.0 表示未知）。
    """
    pid = int(pms_pid or 0)
    if pid <= 0:
        return None
    if PMS_HAS_PSUTIL and pms_optional_psutil is not None:
        try:
            process = pms_optional_psutil.Process(pid)
            return (pcp_normalize_path(process.exe()), float(process.create_time()))
        except Exception:  # noqa: BLE001 - 进程可能已退出 / 无权限
            return None
    return pcp_process_identity_shell(pid)

def pcp_process_identity_shell(pms_pid: int) -> Optional[tuple[str, float]]:
    """没有 psutil 时的兜底：用 PowerShell 查一次可执行文件路径（拿不到返回 None）。"""
    try:
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pms_pid)}' | "
                "Select-Object -First 1 -ExpandProperty ExecutablePath)",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if PMS_IS_WINDOWS else 0,  # type: ignore[attr-defined]
        )
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001 - 查询失败一律当作查不到
        logging.getLogger(PMS_LOGGER_NAME + ".coreproc").debug("PowerShell 进程查询失败：%s", exc)
        return None
    path = (completed.stdout or "").strip().splitlines()
    if not path or not path[0].strip():
        return None
    return (pcp_normalize_path(path[0].strip()), 0.0)


def pcp_kill_process_tree(pms_pid: int, pms_logger: Optional[logging.Logger] = None) -> int:
    """结束一个进程**及其全部子孙进程**，返回成功结束的进程数（v1.9.57）。

    ★ 为什么必须有这一条：核心（sing-box）会自己拉起 `tor.exe`，tor 再拉起
    `lyrebird.exe`。原来只 `terminate()` 核心本身，Windows 上子进程**不会**跟着走——
    孤儿 tor 继续持有 `data/tor/data/lock`，下一次核心再拉 tor，tor 就是：

        [warn] It looks like another Tor process is running with the same data directory.
        [err]  No, it's still there.  Exiting.

    于是 sing-box 读控制通道拿到 `wsarecv: an existing connection was forcibly closed`，
    `FATAL` 退出码 1 → 自动重启 5 次全撞同一个锁（用户实测报障）。

    Windows 用 `taskkill /T /F` 一次带走整棵树；其它平台用 psutil 递归收子进程。
    """
    logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".coreproc")
    pid = int(pms_pid or 0)
    if pid <= 0:
        return 0
    if PMS_IS_WINDOWS:
        # 先数一遍这棵树（含自己）再杀，否则只能靠解析 taskkill 文案猜数量（不可靠）
        before = 0
        if PMS_HAS_PSUTIL and pms_optional_psutil is not None:
            try:
                process = pms_optional_psutil.Process(pid)
                before = 1 + len(process.children(recursive=True))
            except Exception:  # noqa: BLE001 - 进程已退出
                return 0
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
            )
        except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
            logger.warning("清理进程树 PID %s 失败：%s", pid, exc)
            return 0
        # taskkill 报「没有找到」说明进程已不在；报「拒绝访问」说明没杀成——
        # 两种情况都用事后核查兜底，而不是拿输出文案计数。
        time.sleep(0.3)
        if PMS_HAS_PSUTIL and pms_optional_psutil is not None:
            try:
                return 0 if pms_optional_psutil.pid_exists(pid) else before
            except Exception:  # noqa: BLE001
                return before
        return before
    if PMS_HAS_PSUTIL and pms_optional_psutil is not None:
        killed = 0
        try:
            process = pms_optional_psutil.Process(pid)
            victims = process.children(recursive=True) + [process]
        except Exception:  # noqa: BLE001 - 进程已退出
            return 0
        for victim in victims:
            try:
                victim.kill()
                killed += 1
            except Exception:  # noqa: BLE001
                continue
        return killed
    return 0


def pcp_expected_tor_binaries(pms_paths: Any, pms_settings: Any = None) -> set[str]:
    """本程序会拉起的 Tor 系可执行文件（归一化路径集合）。

    只认**我们自己的** `data/tor/tor.exe` 与插件——用户自己装的 Tor Browser 在别的
    目录，路径对不上就永远不会被误杀。
    """
    try:
        paths = ptb_tor_paths(pms_paths, pms_settings)
    except Exception:  # noqa: BLE001 - 路径解析失败就不做这项清理
        return set()
    names = (PMS_TOR_EXE_NAME, PMS_TOR_LYREBIRD_NAME, PMS_TOR_SNOWFLAKE_NAME, PMS_TOR_MEEK_NAME)
    wanted = {pcp_normalize_path(paths["tor_dir"] / name) for name in names}
    return {item for item in wanted if item}


def pcp_orphan_tor_processes(pms_paths: Any, pms_settings: Any = None) -> list[tuple[int, str]]:
    """找出**父进程已死**的 Tor 系孤儿进程（v1.9.57；v1.9.58 按铁律重构）。

    判定两项：① 可执行文件路径是我们的 tor 系二进制；② 父进程已不存在（孤儿）。
    两项都要对——正在被活着的核心使用的 tor 不会被误杀。

    ★ **为什么不按进程名匹配**：项目铁律（`probe_v1924` 第 9 节守着）明令
    「不按进程名杀进程、不做全机进程表遍历」——那条老逻辑正是误杀用户自己
    Tor Browser 的根源。v1.9.57 第一版为了找 tor 孤儿，走的是 psutil 的
    批量进程表枚举 + 读 name 字段，**违反了铁律的字面与精神**
    （虽然当时加了 exe 路径校验、并不会真误杀）。
    v1.9.58 重构为：逐个 PID 按 **exe 路径**判定，完全不看进程名。
    """
    wanted = pcp_expected_tor_binaries(pms_paths, pms_settings)
    if not wanted:
        return []
    if not (PMS_HAS_PSUTIL and pms_optional_psutil is not None):
        return []
    orphans: list[tuple[int, str]] = []
    try:
        all_pids = list(pms_optional_psutil.pids())
    except Exception:  # noqa: BLE001
        return []
    for pid in all_pids:
        try:
            # 只按 exe 路径判定，**不看进程名**（铁律：不按 name 匹配）
            identity = pcp_process_identity(pid)
            if identity is None:
                continue
            exe = identity[0]
            if exe not in wanted:
                continue
            # ppid 单独取：`pcp_process_identity()` 的返回签名是 (exe, create_time)，
            # 给它在第 3 位加 ppid 会波及 `pcp_kill_recorded_core()` 等一堆调用方。
            ppid = int(pms_optional_psutil.Process(pid).ppid() or 0)
            # 父进程还活着 → 属于某个正在运行的核心，不动手
            if ppid > 0 and pms_optional_psutil.pid_exists(ppid):
                continue
            orphans.append((int(pid), Path(exe).name))
        except Exception:  # noqa: BLE001 - 进程可能刚退出
            continue
    return orphans


def pcp_tor_data_dir_locked(pms_paths: Any, pms_settings: Any = None) -> bool:
    """Tor 数据目录 lock 是否被别的进程占用（v1.9.57 兜底自检）。

    ★ 必须用 Win32 `CreateFileW` + `dwShareMode=0`（独占）来问，**不能用 Python 的
    `open()`**：CPython 在 Windows 上默认以 FILE_SHARE_READ|WRITE 打开文件，即使别的
    进程已经独占持有该文件，`open()` 照样成功——拿它做判据只会一路假阴性（实测确认）。
    `CreateFileW` 请求 0 共享模式时，只要有人攥着就必然返回 `ERROR_SHARING_VIOLATION`。

    只做**只读探测**：探完立刻关句柄，不删不改，不影响正在运行的 tor。
    """
    try:
        lock = ptb_tor_paths(pms_paths, pms_settings)["data_dir"] / "lock"
    except Exception:  # noqa: BLE001
        return False
    if not lock.exists():
        return False
    if not PMS_IS_WINDOWS:
        # 非 Windows 平台用 POSIX 建议锁探一次即可（拿不到就当作未被占用）
        try:
            import fcntl
        except ImportError:
            return False
        try:
            with open(lock, "r+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
        except OSError:
            return True
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    ERROR_SHARING_VIOLATION = 32
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        handle = kernel32.CreateFileW(
            str(lock), GENERIC_READ | GENERIC_WRITE, 0, None,
            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
        )
    except Exception:  # noqa: BLE001 - 探测本身失败就不阻断启动
        return False
    if handle not in (None, INVALID_HANDLE_VALUE):
        kernel32.CloseHandle(handle)
        return False
    return ctypes.get_last_error() == ERROR_SHARING_VIOLATION


# --------------------------------------------------------------------------------------
# 核心进程控制器
# --------------------------------------------------------------------------------------
class PmsCoreProcessController(QObject):
    """启动 / 停止 / 自动重启核心进程，并把核心输出转成事件。"""

    pcp_state_changed = Signal(str, str)          # 状态, 说明
    pcp_output_received = Signal(str)             # 原始输出行
    pcp_core_started = Signal(str)                # 核心种类
    pcp_core_stopped = Signal(int, str)           # 退出码, 原因
    pcp_core_error = Signal(str)                  # 错误说明

    pcp_STATE_STOPPED = "stopped"
    pcp_STATE_STARTING = "starting"
    pcp_STATE_RUNNING = "running"
    pcp_STATE_STOPPING = "stopping"
    pcp_STATE_ERROR = "error"

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_paths: PmsRuntimePaths,
        pms_locator: PmsCoreLocator,
        pms_parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pcp_settings = pms_settings
        self.pcp_paths = pms_paths
        self.pcp_locator = pms_locator
        self.pcp_logger = logging.getLogger(PMS_LOGGER_NAME + ".coreproc")
        self.pcp_xray_builder = PmsXrayConfigBuilder()
        self.pcp_singbox_builder = PmsSingBoxConfigBuilder(pms_paths)
        self.pcp_config_writer = PmsCoreConfigWriter()
        self.pcp_process: Optional[QProcess] = None
        self.pcp_state = self.pcp_STATE_STOPPED
        self.pcp_state_detail = "未启动"
        self.pcp_core_kind = ""
        self.pcp_core_path: Optional[Path] = None
        self.pcp_config_path: Optional[Path] = None
        self.pcp_profile: Optional[PmsCoreLaunchProfile] = None
        self.pcp_restart_count = 0
        # v1.9.56：计数**不再**在「启动成功」时清零——启动即崩（如桥接撑爆命令行）时
        # 计数永远停在 1，`core.restart_max_times` 形同虚设，日志刷成重启风暴。
        # 现在只有「稳定运行满 PMS_CORE_RESTART_RESET_AFTER_SECONDS」才清零（见 pcp_handle_finished）。
        self.pcp_restart_timer = QTimer(self)
        self.pcp_restart_timer.setSingleShot(True)
        self.pcp_restart_timer.timeout.connect(self.pcp_do_scheduled_restart)
        self.pcp_intentional_stop = False
        self.pcp_started_at: float = 0.0
        self.pcp_last_error = ""
        # v1.9.56：核心最后几行输出（崩溃时把真实原因带进日志与提示，不再只有「退出码 1」）
        self.pcp_output_tail: deque[str] = deque(maxlen=12)

    # ------------------------------------------------------------------ 状态
    def pcp_set_state(self, pms_state: str, pms_detail: str) -> None:
        self.pcp_state = pms_state
        self.pcp_state_detail = pms_detail
        self.pcp_state_changed.emit(pms_state, pms_detail)

    def pcp_is_running(self) -> bool:
        return self.pcp_process is not None and self.pcp_process.state() != QProcess.ProcessState.NotRunning

    def pcp_uptime_seconds(self) -> float:
        return 0.0 if not self.pcp_started_at else max(0.0, time.time() - self.pcp_started_at)

    def pcp_status_text(self) -> str:
        if self.pcp_is_running():
            return f"核心运行中（{self.pcp_core_kind}，{pms_human_duration(self.pcp_uptime_seconds())}）"
        if self.pcp_state == self.pcp_STATE_ERROR:
            return f"核心异常：{self.pcp_last_error[:60]}"
        return "核心未运行"

    # ------------------------------------------------------------------ 配置
    def pcp_build_config(self, pms_profile: PmsCoreLaunchProfile) -> dict[str, Any]:
        """按核心种类选择生成器。

        ★ Tor 桥接出口的拦截**只在这里**（=只拦「真正启动核心」这条主管道）：
        延迟测试 / 能力矩阵 / 临时会话都是直接调生成器造小配置，把拦截写进生成器会连带
        把那些与 Tor 无关的功能一起弄坏。
        """
        self.pcp_check_tor_profile(pms_profile)
        if pms_profile.core_kind == PMS_CORE_KIND_XRAY:
            return self.pcp_xray_builder.pxc_build(pms_profile)
        return self.pcp_singbox_builder.psc_build(pms_profile)

    def pcp_check_tor_profile(self, pms_profile: PmsCoreLaunchProfile) -> None:
        """Tor 出口启动前检查：核心种类 + 二进制就位 + 数据目录锁是否被孤儿占用。

        不通过就抛 ValueError（界面会显示原因）。
        """
        payload = getattr(pms_profile, "tor", None)
        if not isinstance(payload, dict) or not payload.get("enabled"):
            return
        kind_error = ptb_core_kind_error(pms_profile.core_kind)
        if kind_error:
            raise ValueError(kind_error)
        state = ptb_binaries_state(self.pcp_paths, self.pcp_settings)
        if not state["ready"]:
            missing = "、".join(state["missing"]) or "tor.exe"
            raise ValueError(
                f"Tor 出口缺少组件（{missing}）：请在「设置 → 核心 → Tor 桥接出口」下载 Tor 组件，"
                f"或把 tor.exe 与插件放进 {state['tor_dir']}"
            )
        # v1.9.57 兜底自检：数据目录锁被占 = 还有活着的 tor 攥着它，本次启动必然失败。
        # 先按孤儿口径清一次；清完仍被占就明确报错，而不是让用户看着「退出码 1」猜。
        if pcp_tor_data_dir_locked(self.pcp_paths, self.pcp_settings):
            self.pcp_reap_core_descendants(0)
            if pcp_tor_data_dir_locked(self.pcp_paths, self.pcp_settings):
                locker = "、".join(f"{name}(PID {pid})" for pid, name in
                                   pcp_orphan_tor_processes(self.pcp_paths, self.pcp_settings)) or "另一个 Tor 进程"
                raise ValueError(
                    f"Tor 数据目录被占用（{locker}）：请结束占用该目录的 Tor 进程后重试。"
                    f"常见原因是上一次核心退出时 tor.exe 没被一起结束——"
                    f"本版本已在停止核心时连进程树一起清理，若仍出现请反馈日志。"
                )

    def pcp_prepare_config(self, pms_profile: PmsCoreLaunchProfile, pms_persistent: bool = True) -> tuple[Optional[Path], str]:
        """生成并写入核心配置，返回 (配置路径, 错误说明)。"""
        node = pms_profile.node
        tag = f"{pms_profile.core_kind}_{pms_slugify_filename(node.uid or node.pnode_display_name(), 'node')}"
        target = self.pcp_paths.prt_persistent_config_file(tag) if pms_persistent else self.pcp_paths.prt_temp_config_file(tag)
        try:
            config = self.pcp_build_config(pms_profile)
        except Exception as exc:  # noqa: BLE001 - 生成失败必须回传而不是崩溃
            self.pcp_logger.error("生成核心配置失败：%s", exc)
            return None, f"生成配置失败：{exc}"
        if not config:
            return None, "生成配置为空，请检查节点协议与核心是否匹配"
        if not self.pcp_config_writer.pcw_write(config, target):
            return None, f"写入配置失败：{target}"
        return target, ""

    # ------------------------------------------------------------------ 启动
    def pcp_start(self, pms_profile: PmsCoreLaunchProfile, pms_core_path: Path, pms_core_kind: str, pms_validate: bool = True) -> tuple[bool, str]:
        """启动核心；返回 (是否启动成功, 说明)。"""
        if self.pcp_is_running():
            return False, "核心已在运行，请先停止"
        errors = pms_profile.pclp_verify()
        if errors:
            message = "；".join(errors)
            self.pcp_logger.error("启动前自检未通过：%s", message)
            self.pcp_set_state(self.pcp_STATE_ERROR, message)
            self.pcp_core_error.emit(message)
            return False, message
        self.pcp_last_error = ""
        self.pcp_set_state(self.pcp_STATE_STARTING, "正在生成配置")
        if self.pcp_settings.psm_get_bool("core.kill_existing_on_start", True):
            self.pcp_kill_leftover_processes(pms_core_path)
            # v1.9.57：归属记录只认核心本体（sing-box.exe/xray.exe），核心拉起的
            # tor/lyrebird 从来不在记录里。它们一旦成为孤儿就会攥着 Tor 数据目录锁，
            # 让本次启动的 tor 必然「another Tor process … Exiting」。这里按孤儿口径补一刀。
            self.pcp_reap_core_descendants(0)
        self.pcp_intentional_stop = False
        config_path, error = self.pcp_prepare_config(
            pms_profile,
            pms_persistent=self.pcp_settings.psm_get_bool("core.keep_config_file", True),
        )
        if config_path is None:
            self.pcp_set_state(self.pcp_STATE_ERROR, error)
            self.pcp_core_error.emit(error)
            return False, error
        if pms_validate:
            ok, output = self.pcp_locator.pcl_validate_config(pms_core_path, config_path)
            if not ok:
                message = f"核心配置校验未通过：{output.strip().splitlines()[-1] if output.strip() else '未知原因'}"
                self.pcp_logger.error("%s（配置：%s）", message, config_path)
                self.pcp_set_state(self.pcp_STATE_ERROR, message)
                self.pcp_core_error.emit(message)
                return False, message
        process = QProcess(self)
        process.setProgram(str(pms_core_path))
        if pms_core_kind == PMS_CORE_KIND_XRAY:
            process.setArguments(["run", "-c", str(config_path)])
        else:
            process.setArguments(["run", "-c", str(config_path), "-D", str(self.pcp_paths.prt_data_dir)])
        process.setWorkingDirectory(str(pms_core_path.parent))
        environment = process.processEnvironment()
        if pms_core_kind == PMS_CORE_KIND_XRAY:
            environment.insert("XRAY_LOCATION_ASSET", str(pms_core_path.parent))
        environment.insert("PMS_CORE_KIND", pms_core_kind)
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self.pcp_read_output)
        process.finished.connect(self.pcp_handle_finished)
        process.errorOccurred.connect(self.pcp_handle_process_error)
        self.pcp_process = process
        self.pcp_core_kind = pms_core_kind
        self.pcp_core_path = Path(pms_core_path)
        self.pcp_config_path = config_path
        self.pcp_profile = pms_profile
        self.pcp_logger.info("启动核心：%s %s", pms_core_path, process.arguments())
        process.start()
        if not process.waitForStarted(8000):
            message = f"核心启动失败：{process.errorString()}"
            self.pcp_set_state(self.pcp_STATE_ERROR, message)
            self.pcp_core_error.emit(message)
            self.pcp_process = None
            return False, message
        self.pcp_write_core_record(Path(pms_core_path), int(process.processId()), pms_core_kind)
        self.pcp_started_at = time.time()
        # v1.9.56：这里**不再** `pcp_restart_count = 0`——启动成功不等于稳定运行。
        # 启动即崩的循环里，这个清零让计数永远停在 1、`core.restart_max_times` 失效
        # （用户实测：日志刷「第 1 次自动重启」几十遍）。清零改在 pcp_handle_finished：
        # 只有稳定运行满 PMS_CORE_RESTART_RESET_AFTER_SECONDS 才算「这一轮活了」。
        self.pcp_output_tail.clear()
        self.pcp_set_state(self.pcp_STATE_RUNNING, "核心已启动，等待监听端口就绪")
        self.pcp_core_started.emit(pms_core_kind)
        return True, f"核心已启动（{pms_core_kind}）"

    def pcp_read_output(self) -> None:
        if self.pcp_process is None:
            return
        payload = bytes(self.pcp_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in payload.replace("\r\n", "\n").split("\n"):
            if line.strip():
                # v1.9.56：留一份输出尾巴。核心崩溃时（如 tor 命令行超限的 FATAL），
                # 退出事件里带上真实原因，不再只有干巴巴的「退出码 1」。
                self.pcp_output_tail.append(line.strip()[:300])
                self.pcp_output_received.emit(line)

    def pcp_handle_process_error(self, pms_error: QProcess.ProcessError) -> None:
        if self.pcp_intentional_stop and pms_error == QProcess.ProcessError.Crashed:
            # 主动停止（含强杀）时 Qt 会报 Crashed，这属于预期行为，不当作故障
            self.pcp_logger.debug("核心在停止过程中被强制结束（预期行为）")
            return
        if pms_error == QProcess.ProcessError.Crashed:
            message = "核心进程崩溃"
        elif pms_error == QProcess.ProcessError.FailedToStart:
            message = "核心进程无法启动（文件缺失或被杀软拦截）"
        else:
            message = f"核心进程错误：{pms_error}"
        self.pcp_last_error = message
        self.pcp_logger.error("%s", message)
        self.pcp_core_error.emit(message)

    def pcp_handle_finished(self, pms_exit_code: int, pms_exit_status: QProcess.ExitStatus) -> None:
        """核心退出：正常停止则清理；异常退出按设置自动重启。"""
        runtime = self.pcp_uptime_seconds()
        self.pcp_started_at = 0.0
        self.pcp_process = None
        self.pcp_clear_core_record()
        # v1.9.57：核心**崩溃**退出时不会走 pcp_stop，它拉起的 tor/lyrebird 同样会变成
        # 孤儿并攥住 Tor 数据目录锁——下次自动重启必然撞锁，形成「重启 5 次全失败」。
        # 这里按孤儿口径补一次收尾（正常停止路径已在 pcp_stop 里收过，重复调用无害）。
        self.pcp_reap_core_descendants()
        crash = int(pms_exit_code) != 0 or pms_exit_status == QProcess.ExitStatus.CrashExit
        detail = f"退出码 {pms_exit_code}（运行 {pms_human_duration(runtime)}）"
        # v1.9.56：稳定运行够久才算「这一轮活了」→ 清零重启计数（原来在启动成功就清，
        # 启动即崩的循环里永远清不回上限，`core.restart_max_times` 形同虚设）。
        if not crash and runtime >= PMS_CORE_RESTART_RESET_AFTER_SECONDS:
            self.pcp_restart_count = 0
        if crash:
            # 崩溃时把核心自己的最后一条输出带进日志（真实原因在这里，
            # 例如 `fork/exec tor.exe: the filename or extension is too long`）。
            tail = next((line for line in reversed(self.pcp_output_tail)
                         if "FATAL" in line or "ERROR" in line), "")
            if not tail and self.pcp_output_tail:
                tail = self.pcp_output_tail[-1]
            if tail:
                detail += f"；核心输出：{tail[:160]}"
        self.pcp_logger.info("核心已退出：%s", detail)
        if self.pcp_intentional_stop:
            self.pcp_set_state(self.pcp_STATE_STOPPED, "已手动停止")
            self.pcp_core_stopped.emit(int(pms_exit_code), "手动停止")
            return
        self.pcp_core_stopped.emit(int(pms_exit_code), detail)
        if not crash:
            self.pcp_set_state(self.pcp_STATE_STOPPED, detail)
            return
        self.pcp_set_state(self.pcp_STATE_ERROR, detail)
        if not self.pcp_settings.psm_get_bool("core.auto_restart", True):
            return
        max_restart = self.pcp_settings.psm_get_int("core.restart_max_times", 5)
        if self.pcp_restart_count >= max_restart:
            message = f"核心连续异常退出 {self.pcp_restart_count} 次，已停止自动重启"
            self.pcp_logger.error("%s", message)
            self.pcp_core_error.emit(message)
            return
        self.pcp_restart_count += 1
        delay = min(PMS_CORE_RESTART_MAX_DELAY_MS, PMS_CORE_RESTART_MIN_DELAY_MS * (2 ** (self.pcp_restart_count - 1)))
        self.pcp_logger.warning("将在 %d ms 后第 %d 次自动重启核心", delay, self.pcp_restart_count)
        self.pcp_set_state(self.pcp_STATE_ERROR, f"核心异常退出，{delay / 1000:.1f}s 后自动重启（第 {self.pcp_restart_count} 次）")
        self.pcp_restart_timer.start(int(delay))

    def pcp_do_scheduled_restart(self) -> None:
        """定时器触发的自动重启。"""
        if self.pcp_profile is None or self.pcp_core_path is None:
            return
        self.pcp_start(self.pcp_profile, self.pcp_core_path, self.pcp_core_kind, pms_validate=False)

    def pcp_stop(self, pms_reason: str = "手动停止") -> bool:
        """停止核心（先温和终止，超时后强杀）。

        ★ v1.9.57：核心会自己拉起 `tor.exe` → `lyrebird.exe` 这棵子树。原来只停核心本身，
        Windows 上子孙进程会变成孤儿并继续攥着 `data/tor/data/lock`，下一次启动 tor 必然
        撞锁自杀（见 `pcp_kill_process_tree()` 的说明）。所以这里无论是温和退出还是强杀，
        最后都要**按进程树**收一次尾。
        """
        if self.pcp_process is None:
            self.pcp_set_state(self.pcp_STATE_STOPPED, pms_reason)
            return True
        # v1.9.56：用户主动停止 = 新一轮意图，重启计数归零
        # （否则到上限后手动重开，自动重启会一直是「已放弃」状态）。
        self.pcp_restart_count = 0
        self.pcp_intentional_stop = True
        self.pcp_restart_timer.stop()
        self.pcp_set_state(self.pcp_STATE_STOPPING, pms_reason)
        process = self.pcp_process
        core_pid = int(process.processId() or 0)
        # ★ 必须在核心**还活着**的时候抓子孙清单：核心一死，tor 的父进程就没了，
        # 只能靠「父进程已死」反推——那是猜；此刻抓是准的。
        descendants = self.pcp_collect_core_descendants(core_pid)
        process.terminate()
        if not process.waitForFinished(PMS_PROCESS_STOP_GRACE_MS):
            self.pcp_logger.warning("核心未在 %d ms 内退出，强制结束", PMS_PROCESS_STOP_GRACE_MS)
            process.kill()
            process.waitForFinished(3000)
        # v1.9.57：核心本身已退出，但它的子孙（tor/lyrebird）可能还在——必须连树一起收。
        self.pcp_reap_core_descendants(core_pid, descendants)
        self.pcp_process = None
        self.pcp_started_at = 0.0
        self.pcp_clear_core_record()
        self.pcp_set_state(self.pcp_STATE_STOPPED, pms_reason)
        return True

    def pcp_collect_core_descendants(self, pms_core_pid: int) -> list[int]:
        """核心还活着时，列出它的全部子孙 PID（抓不准就返回空表，不阻断停止流程）。"""
        pid = int(pms_core_pid or 0)
        if pid <= 0 or not (PMS_HAS_PSUTIL and pms_optional_psutil is not None):
            return []
        try:
            return [int(child.pid) for child in pms_optional_psutil.Process(pid).children(recursive=True)]
        except Exception:  # noqa: BLE001 - 进程已退出 / 无权限
            return []

    def pcp_reap_core_descendants(self, pms_core_pid: int = 0, pms_child_pids: Optional[list[int]] = None) -> int:
        """收掉核心残留的子孙进程（主要是 tor / lyrebird），返回清理个数。

        两条来源合并，**都必须核对可执行文件路径属于我们的 Tor 系二进制**才动手：
        ① `pms_child_pids`：核心还活着时抓到的子孙（最准）；
        ② 孤儿判定：父进程已死、exe 落在我们 `data/tor` 下的 tor 系进程（兜底，覆盖
           核心**崩溃**退出、没机会走 `pcp_stop` 的场景）。

        `pms_core_pid` 仅用于日志。绝不按进程名盲杀——用户自己装的 Tor Browser 路径不同，
        永远不会被误伤。
        """
        wanted = pcp_expected_tor_binaries(self.pcp_paths, self.pcp_settings)
        if not wanted:
            return 0
        targets: dict[int, str] = {}
        for pid in (pms_child_pids or []):
            identity = pcp_process_identity(pid)
            if identity is not None and identity[0] in wanted:
                targets[int(pid)] = Path(identity[0]).name
        for pid, name in pcp_orphan_tor_processes(self.pcp_paths, self.pcp_settings):
            targets.setdefault(int(pid), name)
        killed = 0
        for pid, name in targets.items():
            if pid == os.getpid():
                continue
            removed = pcp_kill_process_tree(pid, self.pcp_logger)
            if removed:
                killed += removed
                self.pcp_logger.info("已清理核心残留的 %s（PID %s，含子进程共 %d 个）", name, pid, removed)
        if killed:
            self.pcp_logger.info("已清理 %d 个核心残留子进程（含 Tor 系）", killed)
        return killed

    def pcp_restart(self) -> bool:
        """重启核心（沿用当前配置档）。"""
        if self.pcp_profile is None or self.pcp_core_path is None:
            return False
        profile = self.pcp_profile
        core_path = self.pcp_core_path
        core_kind = self.pcp_core_kind
        self.pcp_stop("重启")
        self.pcp_intentional_stop = False
        ok, _ = self.pcp_start(profile, core_path, core_kind, pms_validate=False)
        return ok

    # ------------------------------------------------------------ 归属记录
    def pcp_core_record_file(self) -> Path:
        """核心归属记录文件（放在数据目录，随实例/临时目录走）。"""
        return self.pcp_paths.prt_core_record_file

    def pcp_read_core_record(self) -> dict[str, Any]:
        """读取归属记录；文件不存在或内容损坏时返回空字典。"""
        try:
            raw = json.loads(self.pcp_core_record_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def pcp_write_core_record(self, pms_core_path: Path, pms_pid: int, pms_core_kind: str = "") -> None:
        """记下本程序刚启动的核心进程（PID + 创建时间 + 可执行文件路径）。

        记录只为下一次启动时「认领自己的残留核心」，进程身份三项缺一不可。
        """
        if int(pms_pid or 0) <= 0:
            return
        identity = pcp_process_identity(int(pms_pid))
        own = pcp_process_identity(os.getpid())
        record = {
            "core_pid": int(pms_pid),
            "core_exe": pcp_normalize_path(pms_core_path),
            "core_create_time": identity[1] if identity else 0.0,
            "core_kind": pms_core_kind,
            "app_pid": os.getpid(),
            "app_create_time": own[1] if own else 0.0,
            "started_at": round(time.time(), 3),
        }
        try:
            self.pcp_core_record_file().write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self.pcp_logger.warning("核心归属记录写入失败：%s", exc)

    def pcp_clear_core_record(self) -> None:
        """删掉归属记录（核心已退出，不再需要认领）。"""
        try:
            self.pcp_core_record_file().unlink()
        except OSError:
            pass

    def pcp_record_owner_alive(self, pms_record: dict[str, Any]) -> bool:
        """记录里的「程序进程」是否还活着（活着说明是另一个实例在用，不能碰）。"""
        app_pid = pms_safe_int(pms_record.get("app_pid"), 0, 0)
        if app_pid <= 0 or app_pid == os.getpid():
            return False
        identity = pcp_process_identity(app_pid)
        if identity is None:
            return False
        created = pms_safe_float(pms_record.get("app_create_time"), 0.0)
        if created and abs(identity[1] - created) > 1.5:
            return False  # PID 被复用，原实例其实已经退出
        return True

    def pcp_kill_recorded_core(self, pms_record: dict[str, Any]) -> int:
        """按归属记录结束那一个核心进程；身份对不上（PID 被复用）就不动手。"""
        pid = pms_safe_int(pms_record.get("core_pid"), 0, 0)
        if pid <= 0 or pid == os.getpid():
            return 0
        identity = pcp_process_identity(pid)
        if identity is None:
            return 0
        want_exe = str(pms_record.get("core_exe") or "")
        if want_exe and identity[0] != want_exe:
            self.pcp_logger.debug("PID %s 已属于别的程序（%s），不动手", pid, identity[0])
            return 0
        created = pms_safe_float(pms_record.get("core_create_time"), 0.0)
        if created and abs(identity[1] - created) > 1.5:
            self.pcp_logger.debug("PID %s 创建时间与记录不符，不动手", pid)
            return 0
        try:
            if PMS_HAS_PSUTIL and pms_optional_psutil is not None:
                pms_optional_psutil.Process(pid).kill()
            else:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW if PMS_IS_WINDOWS else 0,  # type: ignore[attr-defined]
                )
            return 1
        except Exception as exc:  # noqa: BLE001 - 进程可能已退出 / 无权限
            self.pcp_logger.warning("清理残留核心 PID %s 失败：%s", pid, exc)
            return 0

    def pcp_kill_leftover_processes(self, pms_core_path: Optional[Path] = None) -> int:
        """清理上一次运行遗留的核心进程，返回清理个数。

        v1.9.24 起只认**本程序自己启动过**的核心：比对归属记录里的
        「PID + 进程创建时间 + 可执行文件路径」三项，任何一项对不上都不动手，
        因此绝不会误杀别的程序、别的实例正在使用的核心，也不会杀掉正在跑的
        `sing-box check` / `xray -test` 这类临时校验进程。

        pms_core_path：历史签名保留（清理判定以归属记录为准，不再按进程名匹配）。
        """
        record = self.pcp_read_core_record()
        if not record:
            return 0
        if self.pcp_record_owner_alive(record):
            self.pcp_logger.warning("检测到另一个正在运行的程序实例（PID %s），跳过残留核心清理", record.get("app_pid"))
            return 0
        if pms_core_path is not None:
            want = pcp_normalize_path(pms_core_path)
            if want and want != str(record.get("core_exe") or ""):
                self.pcp_logger.debug("残留核心与本次要启动的核心不是同一个文件，仍按归属记录清理")
        killed = self.pcp_kill_recorded_core(record)
        self.pcp_clear_core_record()
        if killed:
            name = Path(str(record.get("core_exe") or "")).name or "核心"
            self.pcp_logger.info("已清理 %d 个残留核心进程（%s）", killed, name)
        else:
            self.pcp_logger.debug("归属记录里的核心 PID %s 已不在，无需清理", record.get("core_pid"))
        return killed
