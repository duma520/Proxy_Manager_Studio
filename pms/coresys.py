#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心定位与系统集成

核心查找/版本探测/配置校验、端口规划与避让、管理员权限、Windows 系统代理、开机自启。
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Optional,
    Sequence,
)
import ctypes
import logging
import os
import re
import subprocess
import sys

from pms.constants import (
    PMS_APP_EN_NAME,
    PMS_CORE_BINARY_NAMES,
    PMS_CORE_KIND_AUTO,
    PMS_CORE_KIND_SINGBOX,
    PMS_CORE_KIND_XRAY,
    PMS_IS_WINDOWS,
    PMS_LOGGER_NAME,
)
from pms.utils import (
    pms_atomic_write_text,
    pms_find_free_port,
    pms_safe_int,
)
from pms.paths import PmsRuntimePaths

class PmsCoreLocator:
    """定位并校验核心可执行文件（xray.exe / sing-box.exe）。"""

    def __init__(self, pms_paths: PmsRuntimePaths) -> None:
        self.pcl_paths = pms_paths
        self.pcl_logger = logging.getLogger(PMS_LOGGER_NAME + ".core")
        self.pcl_version_cache: dict[str, str] = {}

    def pcl_find(self, pms_kind: str, pms_settings: Optional["PmsSettingsManager"] = None) -> Optional[Path]:
        """查找核心：设置里的显式路径 → core 目录 → 程序根目录 → PATH。"""
        if pms_settings is not None:
            key = "core.xray_path" if pms_kind == PMS_CORE_KIND_XRAY else "core.singbox_path"
            explicit = str(pms_settings.psm_get_str(key, "")).strip()
            if explicit:
                candidate = Path(explicit)
                if candidate.is_file():
                    return candidate
                self.pcl_logger.warning("设置中的核心路径不存在：%s", explicit)
        names = PMS_CORE_BINARY_NAMES.get(pms_kind, ())
        for directory in self.pcl_paths.prt_core_search_dirs():
            if not directory.is_dir():
                continue
            for name in names:
                candidate = directory / name
                if candidate.is_file():
                    return candidate
        return None

    def pcl_find_any(self, pms_settings: Optional["PmsSettingsManager"] = None) -> dict[str, Optional[Path]]:
        """返回 {kind: 路径 or None}，用于“自动”选择与设置页展示。"""
        return {kind: self.pcl_find(kind, pms_settings) for kind in (PMS_CORE_KIND_SINGBOX, PMS_CORE_KIND_XRAY)}

    def pcl_resolve_kind(self, pms_requested: str, pms_settings: Optional["PmsSettingsManager"] = None) -> Optional[str]:
        """确定实际使用的核心种类（auto 时优先 sing-box，缺失则 Xray）。

        v1.9.29 起 `auto` 的语义是**逐节点**解析：见 `pcl_resolve_kind_for_node()`。
        本方法保留「给一个全局答案」的老语义，供不需要看节点的场合使用。
        """
        available = self.pcl_find_any(pms_settings)
        if pms_requested == PMS_CORE_KIND_AUTO:
            for kind in (PMS_CORE_KIND_SINGBOX, PMS_CORE_KIND_XRAY):
                if available.get(kind):
                    return kind
            return None
        if available.get(pms_requested):
            return pms_requested
        return None

    @staticmethod
    def pcl_core_supports(pms_kind: str, pms_node: Any) -> bool:
        """这个核心能不能承载这个节点（按协议能力判断）。

        `pnode_validate_core_support()` 是既有的、**用真实核心实测过**的能力表：
        sing-box 侧结构性不支持 ssr（1.6.0 起移除）/ naive（官方构建未编入）/ juicity（无此出站类型）；
        Xray 侧不支持 hysteria2 / tuic / anytls / shadowtls / snell / juicity / hysteria1 / ssr / naive。
        """
        if pms_node is None:
            return False
        try:
            return not pms_node.pnode_validate_core_support(pms_kind)
        except Exception:  # noqa: BLE001 - 替身对象没这个方法时按「支持」处理，不要因此丢掉节点
            return True

    def pcl_resolve_kind_for_node(
        self,
        pms_node: Any,
        pms_requested: str,
        pms_settings: Optional["PmsSettingsManager"] = None,
        pms_available: Optional[dict[str, Optional[Path]]] = None,
    ) -> Optional[str]:
        """**逐节点**选核心（v1.9.29）：

        · `auto`：**优先 sing-box**；这个节点 sing-box 载不动（例如 SSR）就**兜底 Xray**；
          两个都不行才返回 None。装了哪个核心才算哪个候选——只装了一个就只用那一个。
        · 明确指定 `singbox` / `xray`：**严格照办**（用户显式选择就要可预期），
          即使这个节点该核心载不动也返回它，由上层给出「不支持」的明确提示。

        ★ `pms_available` 是 `pcl_find_any()` 的结果。**批量给几万个节点逐个调用时必须传它**，
        否则每个节点都要重新搜一遍核心目录——实测单次 28 ms，3.3 万个节点要跑 **16 分钟**。
        """
        available = pms_available if pms_available is not None else self.pcl_find_any(pms_settings)
        if pms_requested != PMS_CORE_KIND_AUTO:
            return pms_requested if available.get(pms_requested) else None
        if pms_node is None:
            for kind in (PMS_CORE_KIND_SINGBOX, PMS_CORE_KIND_XRAY):
                if available.get(kind):
                    return kind
            return None
        for kind in (PMS_CORE_KIND_SINGBOX, PMS_CORE_KIND_XRAY):
            if available.get(kind) and self.pcl_core_supports(kind, pms_node):
                return kind
        return None

    def pcl_resolve_kinds_for_nodes(
        self,
        pms_nodes: Sequence[Any],
        pms_requested: str,
        pms_settings: Optional["PmsSettingsManager"] = None,
    ) -> dict[str, str]:
        """一次给一批节点定核心：**只查一遍核心可用性、每种协议只判一次能力**，返回 {uid: 核心代号}。

        两个核心都载不动的节点不在结果里（调用方必须把它们记成「测不了」）。
        ★ 逐节点重查会白等十几分钟：`pcl_find_any()` 单次 11 ms、按节点判能力单次 28 ms，
        3.3 万个节点就是 16 分钟；按**协议**缓存后整批约 12 ms。
        """
        available = self.pcl_find_any(pms_settings)
        cache: dict[str, Optional[str]] = {}
        kinds: dict[str, str] = {}
        for node in pms_nodes:
            protocol = str(getattr(node, "protocol", "") or "")
            if protocol not in cache:
                cache[protocol] = self.pcl_resolve_kind_for_node(node, pms_requested, pms_settings, available)
            kind = cache[protocol]
            if kind:
                kinds[str(getattr(node, "uid", "") or "")] = str(kind)
        return kinds

    def pcl_detect_version(self, pms_path: Path, pms_use_cache: bool = True) -> str:
        """执行 `core version` 取版本号（带缓存，失败返回空串）。"""
        key = str(pms_path)
        if pms_use_cache and key in self.pcl_version_cache:
            return self.pcl_version_cache[key]
        if not pms_path.is_file():
            return ""
        command = [str(pms_path), "version"]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=12,
                creationflags=subprocess.CREATE_NO_WINDOW if PMS_IS_WINDOWS else 0,  # type: ignore[attr-defined]
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            match = re.search(r"(\d+\.\d+\.\d+)", output)
            version = match.group(1) if match else output.strip().splitlines()[0][:60] if output.strip() else ""
        except (OSError, subprocess.SubprocessError) as exc:
            self.pcl_logger.warning("核心版本探测失败（%s）：%s", pms_path.name, exc)
            version = ""
        self.pcl_version_cache[key] = version
        return version

    def pcl_validate_config(self, pms_path: Path, pms_config_file: Path) -> tuple[bool, str]:
        """调用核心自带的配置校验（xray -test / sing-box check），返回 (是否通过, 输出)。

        v1.9.41：**超时会再试一次**（第二次给 45 秒）。原因：机器满载时
        （实测跑批 / 后台测速把 CPU 吃满）`xray run -test` 会超过 20 秒被掐掉，
        表现为「xray 配置校验失败：Command ... timed out」——那不是配置错，是等不到 CPU
        （冒烟 `--only config` 空闲时 7/0、满载时随机挂一个用例，已登记为环境性抖动）。
        """
        if not pms_path.is_file():
            return False, "核心程序不存在"
        if pms_path.name.lower().startswith("xray"):
            command = [str(pms_path), "run", "-test", "-config", str(pms_config_file)]
        else:
            command = [str(pms_path), "check", "-c", str(pms_config_file)]
        last_error = ""
        for attempt, timeout in enumerate((20, 45)):
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    creationflags=subprocess.CREATE_NO_WINDOW if PMS_IS_WINDOWS else 0,  # type: ignore[attr-defined]
                )
            except subprocess.TimeoutExpired:
                last_error = f"校验超时（{timeout} 秒，机器可能满载）"
                if attempt == 0:
                    self.pcl_logger.warning("配置校验超时（%d 秒），负载高时常见，重试一次", timeout)
                    continue
                return False, last_error
            except (OSError, subprocess.SubprocessError) as exc:
                return False, f"校验执行失败：{exc}"
            output = ((completed.stdout or "") + (completed.stderr or "")).strip()
            return completed.returncode == 0, output
        return False, last_error or "校验失败"

class PmsPortAllocator:
    """为本地入口与统计接口分配端口（避免端口冲突导致核心启动失败）。"""

    def __init__(self, pms_settings: "PmsSettingsManager") -> None:
        self.ppa_settings = pms_settings
        self.ppa_logger = logging.getLogger(PMS_LOGGER_NAME + ".ports")

    def ppa_resolve(self, pms_core_kind: str, pms_listen_host: str = "127.0.0.1") -> dict[str, int]:
        """解析出本次启动实际使用的端口集合。

        返回：
            mixed / http / socks      本地入口端口（0 表示该入口未启用）
            clash_api / stats_api     统计接口端口
            system_proxy              系统代理应指向的 HTTP 端口
        """
        settings = self.ppa_settings
        auto = settings.psm_get_bool("inbound.auto_pick_port", False)
        mixed_enabled = settings.psm_get_bool("inbound.mixed_enabled", True)
        http_enabled = settings.psm_get_bool("inbound.http_enabled", False)
        socks_enabled = settings.psm_get_bool("inbound.socks_enabled", False)
        mixed_port = settings.psm_get_int("inbound.mixed_port", 30808)   # v1.9.58：默认 20808 → 30808
        http_port = settings.psm_get_int("inbound.http_port", 20809)
        socks_port = settings.psm_get_int("inbound.socks_port", 20810)
        taken: set[int] = set()

        def pick(preferred: int, label: str, force_free: bool) -> int:
            if preferred <= 0:
                return 0
            if force_free or auto or preferred in taken:
                chosen = pms_find_free_port(preferred, pms_listen_host)
                while chosen in taken:
                    chosen = pms_find_free_port(chosen + 1, pms_listen_host)
                if chosen != preferred:
                    self.ppa_logger.warning("%s 端口 %d 不可用，已改用 %d", label, preferred, chosen)
                taken.add(chosen)
                return chosen
            taken.add(preferred)
            return preferred

        result = {"mixed": 0, "http": 0, "socks": 0, "clash_api": 0, "stats_api": 0, "system_proxy": 0}
        if mixed_enabled:
            result["mixed"] = pick(mixed_port, "混合入口", False)
        if pms_core_kind == PMS_CORE_KIND_XRAY:
            # Xray 一个端口只能承载一种协议：混合入口 = SOCKS(混合端口) + HTTP(混合端口+1)
            if mixed_enabled and http_enabled and http_port != mixed_port:
                result["http"] = pick(http_port, "HTTP 入口", False)
            elif mixed_enabled or http_enabled:
                want = http_port if (http_enabled and http_port != mixed_port) else (mixed_port + 1 if mixed_enabled else http_port)
                result["http"] = pick(want, "HTTP 入口", True)
        else:
            if http_enabled:
                result["http"] = pick(http_port, "HTTP 入口", False)
        if socks_enabled and socks_port != result["mixed"]:
            result["socks"] = pick(socks_port, "SOCKS 入口", False)
        result["clash_api"] = pick(settings.psm_get_int("stats.clash_api_port", 0) or 29090, "Clash API", True)
        result["stats_api"] = pick(settings.psm_get_int("stats.xray_api_port", 0) or 28900, "Stats API", True)
        if pms_core_kind == PMS_CORE_KIND_XRAY:
            result["system_proxy"] = result["http"] or result["socks"] or 0
        else:
            result["system_proxy"] = result["mixed"] or result["http"] or result["socks"] or 0
        return result

    def ppa_apply_to_profile(self, pms_profile: "PmsCoreLaunchProfile", pms_ports: dict[str, int]) -> None:
        """把解析结果写回启动配置档（核心生成器只读配置档）。"""
        profile = pms_profile
        if pms_ports.get("mixed"):
            profile.mixed_port = int(pms_ports["mixed"])
        if pms_ports.get("socks"):
            profile.socks_port = int(pms_ports["socks"])
        if pms_ports.get("http"):
            profile.http_port = int(pms_ports["http"])
            if profile.core_kind == PMS_CORE_KIND_XRAY and not profile.http_enabled:
                profile.http_enabled = True
        profile.clash_api_port = int(pms_ports.get("clash_api") or 0)
        profile.stats_api_port = int(pms_ports.get("stats_api") or 0)

class PmsPrivilegeUtils:
    """管理员权限检测与提权（TUN 模式、系统级操作需要）。"""

    @staticmethod
    def ppu_is_admin() -> bool:
        """是否以管理员身份运行。"""
        if not PMS_IS_WINDOWS:
            return os.geteuid() == 0 if hasattr(os, "geteuid") else False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def ppu_relaunch_as_admin(pms_arguments: Optional[Sequence[str]] = None) -> bool:
        """以管理员身份重新启动本程序（成功触发返回 True）。"""
        if not PMS_IS_WINDOWS:
            return False
        try:
            if getattr(sys, "frozen", False) or "__compiled__" in globals():
                executable = sys.executable
                parameters = " ".join(pms_arguments or [])
            else:
                executable = sys.executable
                script = str(Path(sys.argv[0]).resolve())
                parameters = " ".join([f'"{script}"'] + list(pms_arguments or []))
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, parameters, None, 1)
            return int(result) > 32
        except Exception:  # noqa: BLE001
            return False

class PmsWindowsSystemProxy:
    """Windows 系统代理开关（注册表 + 通知系统刷新）。

    v1.9.49 起支持两种模式（`system_proxy.mode`）：
        fixed —— 固定代理：写 ProxyServer=host:port + ProxyEnable=1（**与改动前逐键一致**）；
        pac   —— 自动配置脚本：写 AutoConfigURL=file:///<数据目录>/pms_system_proxy.pac，
                 并把 ProxyEnable 置 0。

    **为什么 PAC 模式必须把 ProxyEnable 置 0**（依据 Windows/WinINET 的取值口径）：
    `ProxyEnable` 与 `AutoConfigURL` 是同一组「每连接代理配置」里的两个来源，IE / WinINET
    取代理设置时先看自动配置脚本，但**系统级「使用代理服务器」开关若仍为 1**，部分上层
    程序（尤其只读 ProxyEnable/ProxyServer 的老程序）会继续走 ProxyServer，导致「一个请求
    走 PAC 判定的直连、另一个走固定代理」这种不可预期的混合行为。两者语义互斥，**同时留下
    去等于给用户一个说不清的行为**，所以开启 PAC 时显式清零 ProxyEnable；关闭 PAC 时由
    `pwsp_write_registry()` 统一决定该键写回什么（fixed 模式再显式写 1）。

    **还原纪律（最易出错的一点）**：备份必须连 `AutoConfigURL` 一起存。用户原本可能正在用
    企业 / 校园网的 PAC，只还原那三个键就等于把 `AutoConfigURL` 删掉——人家的 PAC 会被
    **永久抹掉**（退出本程序后再也连不上网）。所以：
        pwsp_backup_to_settings()  → 4 个键全存（含 system_proxy.original_auto_config_url）
        pwsp_restore_from_settings() → 4 个键全还原；原值为空串时**删除**该值而不是写空串
        （写空 REG_SZ 与「没有这个值」在 WinINET 里不等价：前者是「配置了空脚本」，
        后者才是「没配 PAC」）。
    """

    pwsp_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

    # 注册表值名（集中一处，避免三处代码各自拼字符串写错）
    pwsp_VALUE_ENABLE = "ProxyEnable"
    pwsp_VALUE_SERVER = "ProxyServer"
    pwsp_VALUE_BYPASS = "ProxyOverride"
    pwsp_VALUE_AUTO_CONFIG_URL = "AutoConfigURL"

    def __init__(self, pms_logger: Optional[logging.Logger] = None) -> None:
        self.pwsp_logger = pms_logger or logging.getLogger(PMS_LOGGER_NAME + ".sysproxy")

    # ------------------------------------------------------------------ 读写注册表
    def pwsp_read_current(self) -> dict[str, Any]:
        """读取当前系统代理状态（非 Windows 返回空状态）。

        返回键：enable / server / bypass / auto_config_url。
        `auto_config_url` 是 v1.9.49 新增——不读它就没法备份用户的 PAC。
        """
        state = {"enable": 0, "server": "", "bypass": "", "auto_config_url": ""}
        if not PMS_IS_WINDOWS:
            return state
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.pwsp_REG_PATH) as key:
                for name, field_name in (
                    (self.pwsp_VALUE_ENABLE, "enable"),
                    (self.pwsp_VALUE_SERVER, "server"),
                    (self.pwsp_VALUE_BYPASS, "bypass"),
                    (self.pwsp_VALUE_AUTO_CONFIG_URL, "auto_config_url"),
                ):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                        state[field_name] = value
                    except FileNotFoundError:
                        continue
        except OSError as exc:
            self.pwsp_logger.warning("读取系统代理失败：%s", exc)
        return state

    def pwsp_write_registry(
        self,
        pms_enable: int,
        pms_server: str,
        pms_bypass: str,
        pms_auto_config_url: Optional[str] = None,
    ) -> bool:
        """写入注册表键值。

        pms_auto_config_url：
            None 或 ""  → **删除** AutoConfigURL（fixed 模式 / 还原到「原本没有 PAC」）；
            非空字符串 → 写 REG_SZ（PAC 模式 / 还原用户原本的 PAC）。
        """
        if not PMS_IS_WINDOWS:
            return False
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.pwsp_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.pwsp_VALUE_ENABLE, 0, winreg.REG_DWORD, int(pms_enable))
                if pms_server:
                    winreg.SetValueEx(key, self.pwsp_VALUE_SERVER, 0, winreg.REG_SZ, str(pms_server))
                winreg.SetValueEx(key, self.pwsp_VALUE_BYPASS, 0, winreg.REG_SZ, str(pms_bypass))
                self.pwsp_write_auto_config_url(key, winreg, pms_auto_config_url)
            return True
        except OSError as exc:
            self.pwsp_logger.error("写入系统代理失败：%s", exc)
            return False

    def pwsp_write_auto_config_url(self, pms_key: Any, pms_winreg: Any, pms_url: Optional[str]) -> None:
        """单独处理 AutoConfigURL 键：有值就写，空值就删（**写空串不等于没配 PAC**）。"""
        text = str(pms_url or "").strip()
        if text:
            pms_winreg.SetValueEx(pms_key, self.pwsp_VALUE_AUTO_CONFIG_URL, 0, pms_winreg.REG_SZ, text)
            return
        try:
            pms_winreg.DeleteValue(pms_key, self.pwsp_VALUE_AUTO_CONFIG_URL)
        except FileNotFoundError:
            pass  # 本来就没有，正是我们想要的状态

    def pwsp_refresh_internet_options(self) -> None:
        """通知系统立即刷新代理设置（否则需重启浏览器才生效）。"""
        if not PMS_IS_WINDOWS:
            return
        try:
            internet_set_option = ctypes.windll.wininet.InternetSetOptionW
            internet_set_option(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
            internet_set_option(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH
        except Exception as exc:  # noqa: BLE001
            self.pwsp_logger.debug("刷新系统代理设置失败：%s", exc)

    # ------------------------------------------------------------------ PAC 脚本生成
    def pwsp_build_pac_text(self, pms_host: str, pms_port: int, pms_direct_domains: Sequence[str]) -> str:
        """生成 PAC（FindProxyForURL）脚本文本。

        语义：命中直连域名 → `DIRECT`；其余一律 `PROXY host:port`（本机入口，由核心转发）。
        直连匹配用后缀比较（`*.<域名>`），因此 `baidu.com` 同时命中 `www.baidu.com`；
        `localhost` / `127.0.0.1` / 内网段与 `system_proxy.bypass_list` 的常见项一并内置，
        保证 PAC 模式下访问内网地址不会绕进代理。
        """
        host = str(pms_host or "127.0.0.1").strip() or "127.0.0.1"
        port = pms_safe_int(pms_port, 0)
        proxy = f"PROXY {host}:{port}" if port > 0 else "DIRECT"
        domains = [
            str(item).strip().lower().lstrip(".")
            for item in (pms_direct_domains or [])
            if str(item).strip()
        ]
        # JS 数组字面量：域名已经在上面统一小写去点，这里只做引号内转义（防注入）
        literal = ", ".join('"' + item.replace("\\", "\\\\").replace('"', '\\"') + '"' for item in domains)
        return "\n".join([
            "// 由 代理节点管理控制台（Proxy Manager Studio）自动生成，请勿手工编辑。",
            f"// v1.9.49 PAC 模式：命中直连列表走 DIRECT，其余走 {proxy}。",
            "// 关闭系统代理 / 程序退出时会还原你原本的代理设置，本文件可安全删除。",
            "",
            "var PMS_PROXY = " + '("' + proxy + '")' + ";",
            "var PMS_DIRECT_DOMAINS = [" + literal + "];",
            "",
            "function FindProxyForURL(url, host) {",
            "    host = (host || \"\").toLowerCase();",
            "    if (isPlainHostName(host) || host === \"localhost\" || host === \"127.0.0.1\" || host === \"::1\") {",
            "        return \"DIRECT\";",
            "    }",
            "    for (var i = 0; i < PMS_DIRECT_DOMAINS.length; i++) {",
            "        var suffix = PMS_DIRECT_DOMAINS[i];",
            "        if (host === suffix || dnsDomainIs(host, \".\" + suffix)) {",
            "            return \"DIRECT\";",
            "        }",
            "    }",
            "    return PMS_PROXY;",
            "}",
            "",
        ])

    def pwsp_write_pac_file(self, pms_path: Path, pms_content: str) -> bool:
        """把 PAC 脚本原子写到指定路径（失败返回 False 并记 warning，绝不静默）。"""
        try:
            ok = pms_atomic_write_text(Path(pms_path), pms_content)
        except OSError as exc:
            self.pwsp_logger.warning("写入 PAC 文件失败：%s（%s）", pms_path, exc)
            return False
        if not ok:
            self.pwsp_logger.warning("写入 PAC 文件失败：%s", pms_path)
        return ok

    @staticmethod
    def pwsp_file_url(pms_path: Path) -> str:
        """本地文件 → `file:///...` URL（注册表 AutoConfigURL 只接受 URL 形式）。"""
        return Path(pms_path).resolve().as_uri()

    # ------------------------------------------------------------------ 业务操作
    def pwsp_enable(self, pms_host: str, pms_port: int, pms_bypass: str) -> bool:
        """开启系统代理（固定代理模式）：ProxyServer=host:port，并清掉 AutoConfigURL。"""
        host = pms_host or "127.0.0.1"
        server = f"{host}:{int(pms_port)}"
        ok = self.pwsp_write_registry(1, server, pms_bypass, "")
        if ok:
            self.pwsp_refresh_internet_options()
            self.pwsp_logger.info("系统代理已开启（固定代理）：%s", server)
        return ok

    def pwsp_enable_pac(self, pms_host: str, pms_port: int, pms_direct_domains: Sequence[str], pms_pac_file: Path) -> bool:
        """开启系统代理（PAC 模式）：写 AutoConfigURL，**并把 ProxyEnable 置 0**。

        顺序有意为之：先把 PAC 写好，注册表才切过去——如果反了，中间会有一小段时间
        `AutoConfigURL` 指向一个还不存在的文件，WinINET 会当成「无法读取配置」直连。
        """
        content = self.pwsp_build_pac_text(pms_host, pms_port, pms_direct_domains)
        if not self.pwsp_write_pac_file(pms_pac_file, content):
            return False
        url = self.pwsp_file_url(pms_pac_file)
        # ProxyEnable=0 是硬要求，理由见类注释（PAC 与固定代理互斥）；ProxyServer 保持原样不覆盖，
        # 这样用户关掉 PAC 后原来的 ProxyServer 值还在，不至于把它也一起改掉。
        current = self.pwsp_read_current()
        ok = self.pwsp_write_registry(0, str(current.get("server") or ""), str(current.get("bypass") or ""), url)
        if ok:
            self.pwsp_refresh_internet_options()
            self.pwsp_logger.info("系统代理已开启（PAC）：%s（ProxyEnable=0）", url)
        return ok

    def pwsp_enable_pac_file(self, pms_pac_file: Path) -> bool:
        """开启系统代理（PAC 模式，使用**已存在**的用户手写 PAC 文件，v1.9.62 新增）。

        与 `pwsp_enable_pac` 的区别就一点：`pwsp_enable_pac` 会先按直连域名**重新生成**一份
        PAC 写进文件，再把它指给 AutoConfigURL（那份是程序自动维护的 pms_system_proxy.pac）；
        而本方法**不碰文件内容**，只把 AutoConfigURL 指向一个已经存在的文件、并把 ProxyEnable 置 0。

        这正是「内置 PAC 编辑器」需要的语义：用户用编辑器写的是 pms_custom.pac，那份文件的
        内容必须由编辑器负责写（且支持任意 FindProxyForURL 逻辑），系统代理侧只管「指向它」。
        """
        path = Path(pms_pac_file)
        if not path.is_file():
            self.pwsp_logger.warning("启用用户 PAC 失败：文件不存在 %s", path)
            return False
        url = self.pwsp_file_url(path)
        # 复用与 pwsp_enable_pac 一致的顺序与写法：ProxyEnable=0（理由见类注释），ProxyServer 原样保留。
        current = self.pwsp_read_current()
        ok = self.pwsp_write_registry(0, str(current.get("server") or ""), str(current.get("bypass") or ""), url)
        if ok:
            self.pwsp_refresh_internet_options()
            self.pwsp_logger.info("系统代理已开启（用户 PAC）：%s（ProxyEnable=0）", url)
        return ok

    def pwsp_disable(self, pms_bypass: str = "") -> bool:
        """关闭系统代理：ProxyEnable=0 并删除 AutoConfigURL（固定代理与 PAC 一起关）。"""
        current = self.pwsp_read_current()
        ok = self.pwsp_write_registry(0, str(current.get("server") or ""), pms_bypass or str(current.get("bypass") or ""), "")
        if ok:
            self.pwsp_refresh_internet_options()
            self.pwsp_logger.info("系统代理已关闭")
        return ok

    def pwsp_backup_to_settings(self, pms_settings: "PmsSettingsManager") -> dict[str, Any]:
        """把当前系统代理状态备份进设置（退出时可还原）。

        **必须连 AutoConfigURL 一起备份**：用户原本可能正用着企业 / 校园网的 PAC，
        少存这一个键，还原时就会把它删掉（永久抹掉用户原设置）。
        """
        state = self.pwsp_read_current()
        pms_settings.psm_set_many({
            "system_proxy.backup_saved": True,
            "system_proxy.original_enable": pms_safe_int(state.get("enable"), 0),
            "system_proxy.original_server": str(state.get("server") or ""),
            "system_proxy.original_bypass": str(state.get("bypass") or ""),
            "system_proxy.original_auto_config_url": str(state.get("auto_config_url") or ""),
        })
        self.pwsp_logger.info(
            "已备份原系统代理：enable=%s server=%s auto_config_url=%s",
            state.get("enable"), state.get("server"), state.get("auto_config_url") or "（无）",
        )
        return state

    def pwsp_restore_from_settings(self, pms_settings: "PmsSettingsManager") -> bool:
        """按备份还原系统代理（仅在备份存在时动作）。

        4 个键一起还原；`original_auto_config_url` 为空串时删除该注册表值
        （**不是**写空串——两者在 WinINET 里不等价，写空串等于「配了个空脚本」）。
        """
        if not pms_settings.psm_get_bool("system_proxy.backup_saved", False):
            return False
        enable = pms_settings.psm_get_int("system_proxy.original_enable", 0)
        server = pms_settings.psm_get_str("system_proxy.original_server", "")
        bypass = pms_settings.psm_get_str("system_proxy.original_bypass", "")
        auto_config_url = pms_settings.psm_get_str("system_proxy.original_auto_config_url", "")
        ok = self.pwsp_write_registry(1 if enable else 0, server, bypass, auto_config_url)
        if ok:
            self.pwsp_refresh_internet_options()
            self.pwsp_logger.info(
                "系统代理已还原：enable=%s server=%s auto_config_url=%s",
                enable, server, auto_config_url or "（无）",
            )
        return ok

class PmsWindowsAutoStart:
    """开机自启（HKCU\...\Run）。"""

    pwas_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
    pwas_VALUE_NAME = PMS_APP_EN_NAME

    def __init__(self, pms_paths: PmsRuntimePaths) -> None:
        self.pwas_paths = pms_paths
        self.pwas_logger = logging.getLogger(PMS_LOGGER_NAME + ".autostart")

    def pwas_command_text(self) -> str:
        """自启命令行：编译态用 exe 路径，开发态用 pythonw + 主程序路径。"""
        if getattr(sys, "frozen", False) or "__compiled__" in globals():
            return f'"{Path(sys.executable).resolve()}"'
        python_executable = Path(sys.executable)
        pythonw = python_executable.with_name("pythonw.exe")
        launcher = pythonw if pythonw.is_file() else python_executable
        return f'"{launcher}" "{self.pwas_paths.prt_source_file}"'

    def pwas_is_enabled(self) -> bool:
        """读注册表判断是否已开启自启。"""
        if not PMS_IS_WINDOWS:
            return False
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.pwas_RUN_PATH) as key:
                value, _ = winreg.QueryValueEx(key, self.pwas_VALUE_NAME)
                return bool(str(value).strip())
        except OSError:
            return False

    def pwas_set_enabled(self, pms_enabled: bool) -> bool:
        """开启 / 关闭自启，返回是否成功。"""
        if not PMS_IS_WINDOWS:
            return False
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.pwas_RUN_PATH, 0, winreg.KEY_SET_VALUE) as key:
                if pms_enabled:
                    winreg.SetValueEx(key, self.pwas_VALUE_NAME, 0, winreg.REG_SZ, self.pwas_command_text())
                else:
                    try:
                        winreg.DeleteValue(key, self.pwas_VALUE_NAME)
                    except FileNotFoundError:
                        pass
            self.pwas_logger.info("开机自启已%s", "开启" if pms_enabled else "关闭")
            return True
        except OSError as exc:
            self.pwas_logger.error("设置开机自启失败：%s", exc)
            return False
