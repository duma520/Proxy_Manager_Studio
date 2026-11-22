# -*- coding: utf-8 -*-
"""核心进程（起停 / 系统代理 / TUN / 局域网共享 / 内核下载）——v1.9.36 从 `pms/gui/mainwindow.py` 拆出的 mixin。

★ 拆分原则：**方法名与 `self.pmw_*` 属性名一个字都没改**，外部引用（主窗口之外用到
`pmw_*` 的 80 处）全都不用动；mixins 只是把方法按功能域挪个文件。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from pathlib import Path
from pms.constants import (PMS_APP_NAME, PMS_CORE_KIND_AUTO, PMS_CORE_KIND_SINGBOX,
                           PMS_SYSTEM_PROXY_MODE_FIXED, PMS_SYSTEM_PROXY_MODE_PAC)   # v1.9.49：PAC 模式分流
from pms.coreprocess import PmsCoreProcessController
from pms.coreprofile import PmsCoreLaunchProfile
from pms.coresys import PmsPrivilegeUtils, PmsWindowsSystemProxy
from pms.gui.dialogs.lanproxydialog import PmsLanProxyDialog
from pms.logparse import PmsCoreLogParser
from pms.models import PmsProxyNode
from pms.coremeta import pms_core_title   # v1.9.45：核心显示名从已删的独立内核模块搬到 coremeta.py
from pms.utils import pms_lan_proxy_address, pms_now_text
from typing import Optional


class _PmwCoreMixin:
    """核心进程（起停 / 系统代理 / TUN / 局域网共享 / 内核下载）（21 个方法）。"""

    def pmw_resolve_core(self, pms_node: Optional[PmsProxyNode] = None) -> tuple[Optional[str], Optional[Path]]:
        """解析要用哪个核心与它的路径。

        v1.9.29：传了节点就**逐节点**选（`auto` = 优先 sing-box，这个节点 sing-box 载不动就兜底 Xray）；
        不传节点时退化成「给一个全局答案」的老行为。
        """
        requested = self.pmw_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO)
        if pms_node is None:
            kind = self.pmw_locator.pcl_resolve_kind(requested, self.pmw_settings)
        else:
            kind = self.pmw_locator.pcl_resolve_kind_for_node(pms_node, requested, self.pmw_settings)
        if kind is None:
            return None, None
        return kind, self.pmw_locator.pcl_find(kind, self.pmw_settings)

    def pmw_build_profile(self, pms_node: PmsProxyNode, pms_core_kind: str) -> PmsCoreLaunchProfile:
        """构造启动配置档并把端口分配结果写回。"""
        profile = PmsCoreLaunchProfile.pclp_from_settings(
            self.pmw_settings, pms_node, pms_core_kind,
            pms_clash_api_port=0, pms_stats_api_port=0,
        )
        ports = self.pmw_allocator.ppa_resolve(pms_core_kind, profile.pclp_listen_host())
        self.pmw_port_plan = ports
        self.pmw_allocator.ppa_apply_to_profile(profile, ports)
        return profile

    def pmw_toggle_core(self) -> None:
        """工具栏 / 托盘：启动或停止核心。"""
        if self.pmw_core.pcp_is_running():
            self.pmw_stop_core()
            return
        node = self.pmw_current_node()
        if node is None:
            nodes = self.pmw_selected_nodes() or self.pmw_visible_nodes() or self.pmw_nodes
            if not nodes:
                QMessageBox.information(self, "提示", "还没有节点，请先新增或导入节点。")
                return
            node = nodes[0]
            self.pmw_current_node_uid = node.uid
            self.pmw_settings.psm_set("general.last_node_uid", node.uid)
        self.pmw_start_core(node)

    def pmw_start_core(self, pms_node: PmsProxyNode) -> bool:
        """启动核心（含配置文件生成、校验、监听提示与统计启动）。

        v1.9.29：按**这个节点**选核心（`auto` = 优先 sing-box，它载不动这个节点就兜底 Xray）。
        """
        core_kind, core_path = self.pmw_resolve_core(pms_node)
        if core_kind is None or core_path is None:
            answer = QMessageBox.question(
                self, "未找到核心",
                "没有找到 xray.exe / sing-box.exe。\n是否现在打开设置去下载核心？",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.pmw_open_settings_dialog(3)
            return False
        profile = self.pmw_build_profile(pms_node, core_kind)
        errors = profile.pclp_verify()
        if errors:
            QMessageBox.warning(self, "无法启动", "\n".join(errors))
            self.pmw_show_message("启动被校验拦截：" + "；".join(errors))
            return False
        self.pmw_status_progress.setRange(0, 0)
        self.pmw_show_message(f"正在启动核心（{core_kind}）…")
        self.pmw_current_node_uid = pms_node.uid
        self.pmw_settings.psm_set_many({"general.last_node_uid": pms_node.uid, "tun.enabled": profile.tun_enabled}, pms_emit=False)
        # v1.9.26：启动过程中标题栏也要立刻指向目标节点（启动失败时不会留下上一次的名字）
        self.pmw_set_current_node_label(f"当前节点：{pms_node.pnode_display_name()}（{pms_node.pnode_display_address()}）")
        ok, message = self.pmw_core.pcp_start(
            profile, core_path, core_kind,
            pms_validate=self.pmw_settings.psm_get_bool("general.check_core_on_launch", True),
        )
        self.pmw_status_progress.setRange(0, 100)
        self.pmw_status_progress.setValue(100 if ok else 0)
        if not ok:
            QMessageBox.warning(self, "核心启动失败", message)
            return False
        self.pmw_traffic.pts_configure(core_kind, core_path, self.pmw_port_plan)
        self.pmw_traffic.pts_start()
        self.pmw_traffic.pts_reset_totals()
        self.pmw_tracker.pct_clear()
        self.pmw_subscription.psu_set_local_proxy_port(profile.pclp_primary_port())
        self.pmw_update_listen_label()
        self.pmw_highlight_current_node()
        self.pmw_show_message(f"核心已启动，本地入口：{self.pmw_settings.psm_get_str('inbound.listen_address', '127.0.0.1')}:{profile.pclp_primary_port()}")
        if self.pmw_settings.psm_get_bool("system_proxy.enabled", False):
            self.pmw_apply_system_proxy(True)
        if self.pmw_settings.psm_get_bool("test.auto_test_on_start", False):
            QTimer.singleShot(1500, lambda: self.pmw_test_latency_selected() if self.pmw_selected_nodes() else self.pmw_test_latency_all())
        return True

    def pmw_stop_core(self) -> None:
        """停止核心并收尾（关闭系统代理、停止统计）。"""
        self.pmw_status_progress.setRange(0, 0)
        self.pmw_show_message("正在停止核心…")
        self.pmw_core.pcp_stop("手动停止")
        self.pmw_traffic.pts_stop()
        self.pmw_tracker.pct_clear()
        self.pmw_subscription.psu_set_local_proxy_port(0)
        self.pmw_status_progress.setRange(0, 100)
        self.pmw_status_progress.setValue(0)
        if self.pmw_settings.psm_get_bool("system_proxy.enabled", False):
            self.pmw_apply_system_proxy(False)

    def pmw_on_core_state_changed(self, pms_state: str, pms_detail: str) -> None:
        """核心状态变化 → 更新所有状态显示。"""
        titles = {
            PmsCoreProcessController.pcp_STATE_STOPPED: "已停止",
            PmsCoreProcessController.pcp_STATE_STARTING: "启动中",
            PmsCoreProcessController.pcp_STATE_RUNNING: "运行中",
            PmsCoreProcessController.pcp_STATE_STOPPING: "停止中",
            PmsCoreProcessController.pcp_STATE_ERROR: "异常",
        }
        text = titles.get(pms_state, pms_state)
        self.pmw_label_core_state.setText(f"核心：{text}")
        self.pmw_status_core.setText(f"核心：{text}")
        self.pmw_apply_window_title()   # v1.9.26：核心状态一变就重算标题（覆盖所有进入路径）
        self.pmw_show_message(pms_detail)
        running = self.pmw_core.pcp_is_running()
        action = self.pmw_actions.get("core")
        if action is not None:
            action.setText("停止核心" if running else ("启动核心" if self.pmw_settings.psm_get_bool("ui.compact_toolbar", True) else "启动 / 停止核心"))
        if self.pmw_tray is not None:
            self.pmw_tray.ptrc_set_core_state(running)
        if pms_state == PmsCoreProcessController.pcp_STATE_ERROR:
            self.pmw_status_core.setObjectName("errorLabel")
        else:
            self.pmw_status_core.setObjectName("")
        self.pmw_status_core.style().unpolish(self.pmw_status_core)
        self.pmw_status_core.style().polish(self.pmw_status_core)
        self.pmw_update_listen_label()
        # v1.8.0 事件推送：核心启停 + 真的换了节点才发 node.switched（重启同一个节点不算切换）
        node = self.pmw_current_node()
        payload = {
            "core_kind": self.pmw_core.pcp_core_kind,
            "node_uid": self.pmw_current_node_uid,
            "node_name": node.pnode_display_name() if node is not None else "",
        }
        if pms_state == PmsCoreProcessController.pcp_STATE_RUNNING:
            self.pmw_emit_event("core.started", payload)
            if self.pmw_current_node_uid and self.pmw_current_node_uid != self.pmw_event_node_uid:
                self.pmw_event_node_uid = self.pmw_current_node_uid
                self.pmw_emit_event("node.switched", payload)
        elif pms_state == PmsCoreProcessController.pcp_STATE_STOPPED:
            self.pmw_event_node_uid = ""
            self.pmw_emit_event("core.stopped", payload)

    def pmw_on_core_output(self, pms_line: str) -> None:
        """核心输出 → 日志面板 + 连接追踪。"""
        text = str(pms_line)
        self.pmw_log_bus.plb_append(pms_now_text("%H:%M:%S"), "CORE", f"core.{self.pmw_core.pcp_core_kind}", text)
        event = PmsCoreLogParser().pclp_parse(text)
        if event.get("kind") == "connection":
            self.pmw_tracker.pct_feed_event(event)

    def pmw_on_core_error(self, pms_message: str) -> None:
        """核心错误提示。

        v1.8.0：原来只弹状态栏 + 托盘，**不进日志总线**，等于核心报错在日志里查不到、
        错误面板也收不到。这里补上 error 级记录，让「最近错误」成为唯一入口。
        """
        text = str(pms_message or "").strip() or "核心异常（无详细信息）"
        self.pmw_log_bus.plb_append(pms_now_text("%H:%M:%S"), "ERROR", f"core.{self.pmw_core.pcp_core_kind}", text)
        self.pmw_show_message(text)
        self.pmw_emit_event("error.raised", {"source": f"core.{self.pmw_core.pcp_core_kind}", "message": text})
        if self.pmw_settings.psm_get_bool("general.notify_on_core_error", True) and self.pmw_tray is not None:
            self.pmw_tray.ptrc_notify(f"{PMS_APP_NAME} - 核心异常", text[:200])

    def pmw_on_core_stopped(self, pms_exit_code: int, pms_reason: str) -> None:
        self.pmw_show_message(f"核心已退出：{pms_reason}")
        self.pmw_traffic.pts_stop()

    def pmw_update_listen_label(self) -> None:
        """更新监听地址显示与系统代理端口。"""
        if not self.pmw_core.pcp_is_running():
            self.pmw_status_listen.setText("监听：-")
            return
        profile = self.pmw_core.pcp_profile
        if profile is None:
            self.pmw_status_listen.setText("监听：-")
            return
        listen = profile.pclp_listen_host()
        parts = []
        if profile.mixed_enabled:
            parts.append(f"混合 {listen}:{profile.mixed_port}")
        if profile.http_enabled:
            parts.append(f"HTTP {listen}:{profile.pclp_effective_http_port()}")
        if profile.socks_enabled:
            parts.append(f"SOCKS {listen}:{profile.socks_port}")
        if profile.tun_enabled:
            parts.append(f"TUN {profile.tun_interface}")
        self.pmw_status_listen.setText("监听：" + "，".join(parts))

    def pmw_toggle_system_proxy(self, pms_checked: bool) -> None:
        """工具栏 / 托盘：开关系统代理。"""
        if pms_checked and not self.pmw_core.pcp_is_running():
            QMessageBox.information(self, "需要先启动核心", "系统代理需要一个正在运行的本地 HTTP 入口，请先启动核心。")
            action = self.pmw_actions.get("system_proxy")
            if action is not None:
                action.blockSignals(True)
                action.setChecked(False)
                action.blockSignals(False)
            if self.pmw_tray is not None:
                self.pmw_tray.ptrc_set_proxy_state(False)
            return
        self.pmw_settings.psm_set("system_proxy.enabled", bool(pms_checked))
        self.pmw_apply_system_proxy(bool(pms_checked))

    def pmw_apply_system_proxy(self, pms_enable: bool) -> None:
        """真正写入 / 关闭 Windows 系统代理（按 `system_proxy.mode` 分流 fixed / pac）。

        v1.9.49：新增 PAC 分流。三条路径的语义**必须分清**（这是本功能最容易搞错的地方）：

            开启（fixed）  → pwsp_enable()：ProxyEnable=1 + ProxyServer=host:port，并**删除** AutoConfigURL
            开启（pac）    → pwsp_enable_pac()：写 PAC 文件 → AutoConfigURL=file:///...，并 **ProxyEnable=0**
            关闭           → pwsp_disable()：ProxyEnable=0 + **删除** AutoConfigURL
                             （只撤销「本程序施加的代理」，**不动备份**——用户原值仍安全存在设置里，
                              下次还能由「退出还原」或设置页的「还原备份」放回去）
            退出/手动还原  → pwsp_restore_from_settings()（**在别的调用点**，见 mainwindow_shell.py：
                              `pmw_close_event` 里 pwsp_restore_from_settings）——那才是把用户
                              **原本的 4 个键**（含 original_auto_config_url）写回注册表的唯一路径。

        **为什么关闭路径绝不能顺手调 restore**：关闭系统代理是用户/核心停止时的常规动作，可能发生
        任意多次；而 restore 会把「备份时的旧值」当成当前值写进注册表。若在关闭里调 restore，
        用户先手动关了一次代理、再去设置里改 ProxyServer 时就会被旧备份覆盖。两条路径职责不同，
        所以这里只做「撤销」，还原一律留给显式还原入口。

        fixed 分支的调用与写入顺序**逐字节保持改动前的样子**（回归红线）。
        """
        controller = PmsWindowsSystemProxy(self.pmw_logger)
        profile = self.pmw_core.pcp_profile
        if pms_enable:
            if profile is None:
                return
            if not self.pmw_settings.psm_get_bool("system_proxy.backup_saved", False):
                controller.pwsp_backup_to_settings(self.pmw_settings)
            host = "127.0.0.1"
            port = profile.pclp_primary_port()
            bypass = self.pmw_settings.psm_get_str("system_proxy.bypass_list", "localhost;127.*;<local>")
            if self.pmw_system_proxy_mode() == PMS_SYSTEM_PROXY_MODE_PAC:
                # PAC 模式：直连域名取本功能的设置，留空回落路由页的直连预设（与设置页部件同一口径）
                domains = self.pmw_system_proxy_direct_domains()
                # v1.9.62：pac_source=custom 时用用户 PAC 编辑器写的 pms_custom.pac（不重新生成内容，
                # 由 coresys.pwsp_enable_pac_file 只写 AutoConfigURL 指向它）；其余仍走自动生成路径。
                pac_source = str(self.pmw_settings.psm_get_str("system_proxy.pac_source", PMS_SYSTEM_PROXY_MODE_FIXED) or "").strip().lower()
                if pac_source == "custom":
                    ok = controller.pwsp_enable_pac_file(self.pmw_paths.prt_custom_pac_file)
                    self.pmw_logger.info(
                        "系统代理模式=pac（用户 PAC）：AutoConfigURL=%s（ProxyEnable=0）",
                        controller.pwsp_file_url(self.pmw_paths.prt_custom_pac_file),
                    )
                    self.pmw_show_message("系统代理已开启（用户 PAC 模式）：使用 PAC 编辑器写的脚本" if ok
                                          else "系统代理开启失败（请检查权限）")
                else:
                    ok = controller.pwsp_enable_pac(host, port, domains, self.pmw_paths.prt_pac_file)
                    # 日志要能看出走的是哪种模式（不许静默）：写入成功的行由 coresys 记，
                    # 这里补一条带模式与域名条数的，方便排查「为什么某个站点没走代理」
                    self.pmw_logger.info(
                        "系统代理模式=pac：AutoConfigURL=%s（ProxyEnable=0），直连域名 %d 条",
                        controller.pwsp_file_url(self.pmw_paths.prt_pac_file), len(domains),
                    )
                    self.pmw_show_message(f"系统代理已开启（PAC 模式）：直连 {len(domains)} 条规则" if ok
                                          else "系统代理开启失败（请检查权限）")
            else:
                ok = controller.pwsp_enable(host, port, bypass)
                self.pmw_logger.info("系统代理模式=fixed：ProxyServer=%s:%s", host, port)
                self.pmw_show_message(f"系统代理已开启：{host}:{port}" if ok else "系统代理开启失败（请检查权限）")
        else:
            ok = controller.pwsp_disable(self.pmw_settings.psm_get_str("system_proxy.bypass_list", ""))
            self.pmw_logger.info("系统代理已关闭（模式=%s，AutoConfigURL 一并清除）", self.pmw_system_proxy_mode())
            self.pmw_show_message("系统代理已关闭" if ok else "系统代理关闭失败")
        self.pmw_sync_system_proxy_action(pms_enable)

    def pmw_system_proxy_mode(self) -> str:
        """当前系统代理模式（脏值一律当 fixed——与 psm_normalize 的口径一致）。"""
        mode = str(self.pmw_settings.psm_get_str("system_proxy.mode", PMS_SYSTEM_PROXY_MODE_FIXED) or "").strip().lower()
        return mode if mode == PMS_SYSTEM_PROXY_MODE_PAC else PMS_SYSTEM_PROXY_MODE_FIXED

    def pmw_system_proxy_direct_domains(self) -> list[str]:
        """PAC 生效用的直连域名：本功能设置优先，留空回落路由页的直连预设。"""
        domains = self.pmw_settings.psm_get_list("system_proxy.pac_direct_domains")
        return domains or self.pmw_settings.psm_get_list("routing.direct_domains")

    def pmw_sync_system_proxy_action(self, pms_enable: bool) -> None:
        """把工具栏勾选态与托盘状态同步成实际开关状态（信号阻塞，避免回环再触发一次）。"""
        action = self.pmw_actions.get("system_proxy")
        if action is not None:
            action.blockSignals(True)
            action.setChecked(bool(pms_enable))
            action.blockSignals(False)
        if self.pmw_tray is not None:
            self.pmw_tray.ptrc_set_proxy_state(bool(pms_enable))

    def pmw_reopen_system_proxy_for_mode_change(self) -> None:
        """`system_proxy.mode` 改了：系统代理当前开着就**先撤旧再上新**。

        不同时留下 ProxyServer 与 AutoConfigURL（两种模式语义互斥，同时存在行为不可预期）。
        `pwsp_enable()` 会删 AutoConfigURL、`pwsp_enable_pac()` 会把 ProxyEnable 置 0，
        所以「重新走一遍 pmw_apply_system_proxy(True)」本身就完成了先撤旧再上新；
        没开核心 / 没开代理时什么都不做（只改了下次开启用的模式）。
        """
        if not self.pmw_settings.psm_get_bool("system_proxy.enabled", False):
            return
        if not self.pmw_core.pcp_is_running():
            return
        self.pmw_logger.info("系统代理模式已切换为 %s，按新模式重开", self.pmw_system_proxy_mode())
        self.pmw_apply_system_proxy(True)

    def pmw_toggle_tun(self, pms_checked: bool) -> None:
        """工具栏 / 托盘：开关 TUN 模式（校验核心与权限）。"""
        if pms_checked:
            core_kind, _ = self.pmw_resolve_core()
            if core_kind != PMS_CORE_KIND_SINGBOX:
                QMessageBox.warning(self, "无法启用 TUN", "TUN 模式需要 sing-box 核心（Xray 不支持 TUN 入站）。")
                self.pmw_sync_tun_action(False)
                return
            if not PmsPrivilegeUtils.ppu_is_admin():
                answer = QMessageBox.question(self, "需要管理员权限", "TUN 模式需要管理员权限，是否以管理员身份重新启动程序？")
                if answer == QMessageBox.StandardButton.Yes:
                    self.pmw_handle_settings_action("restart_as_admin", {})
                self.pmw_sync_tun_action(False)
                return
        # v1.6.7：这里只改设置。重启核心交给 pmw_on_setting_changed →
        # pmw_restart_core_for_settings 统一处理，否则会连着重启两次。
        self.pmw_settings.psm_set("tun.enabled", bool(pms_checked))
        self.pmw_sync_tun_action(bool(pms_checked))

    def pmw_sync_tun_action(self, pms_enabled: bool) -> None:
        """同步 TUN 勾选状态到工具栏与托盘。"""
        action = self.pmw_actions.get("tun")
        if action is not None:
            action.blockSignals(True)
            action.setChecked(bool(pms_enabled))
            action.blockSignals(False)
        if self.pmw_tray is not None:
            self.pmw_tray.ptrc_set_tun_state(bool(pms_enabled))

    def pmw_toggle_allow_lan(self, pms_checked: bool) -> None:
        """工具栏 / 手机代理：开关局域网监听（让手机 / 平板连接本机代理）。

        仅写入设置；同步工具栏勾选 + 必要时重启核心由 ``pmw_on_setting_changed``
        统一处理（``psm_value_changed`` 信号同步触发，避免重复重启）。
        """
        self.pmw_settings.psm_set("inbound.allow_lan", bool(pms_checked))

    def pmw_sync_allow_lan_action(self, pms_enabled: bool) -> None:
        """同步局域网监听勾选状态到工具栏。"""
        action = self.pmw_actions.get("allow_lan")
        if action is not None:
            action.blockSignals(True)
            action.setChecked(bool(pms_enabled))
            action.blockSignals(False)

    def pmw_open_lan_proxy_dialog(self) -> None:
        """打开『手机 / 平板代理助手』对话框。"""
        dialog = PmsLanProxyDialog(self.pmw_settings, self)
        dialog.exec()

    def pmw_copy_lan_proxy_address(self) -> None:
        """复制当前本机代理地址 host:port 到剪贴板。"""
        address = pms_lan_proxy_address(self.pmw_settings)
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(address)
        self.pmw_show_message(f"已复制代理地址：{address}")

    def pmw_on_core_download_progress(self, pms_kind: str, pms_percent: int, pms_downloaded: int, pms_total: int, pms_note: str) -> None:
        """核心下载进度 → 速览条 + 设置对话框。"""
        if self.pmw_progress_kind != "download" or self.pmw_progress_total != 100:
            self.pmw_begin_test_progress("download", 100)
        self.pmw_update_test_progress(int(pms_percent), pms_note)
        self.pmw_status_message.setText(f"下载核心：{pms_note}")
        if self.pmw_settings_dialog is not None:
            self.pmw_settings_dialog.pset_refresh_core_download_progress(pms_percent, pms_downloaded, pms_total, pms_note)

    def pmw_on_core_download_finished(self, pms_kind: str, pms_ok: bool, pms_message: str) -> None:
        """核心下载结束：刷新版本缓存与「内核版本一览」，并把结果提示给用户。"""
        self.pmw_end_test_progress()
        self.pmw_show_message(pms_message)
        if pms_ok:
            self.pmw_locator.pcl_version_cache.clear()
            if self.pmw_settings_dialog is not None:
                self.pmw_settings_dialog.pset_refresh_core_status()
                self.pmw_settings_dialog.pset_version_widget.pcvw_on_download_finished(pms_kind, pms_ok)
            if self.pmw_tray is not None:
                self.pmw_tray.ptrc_notify(f"{PMS_APP_NAME} - 核心下载", pms_message)
        else:
            QMessageBox.warning(self, "核心下载失败", pms_message)

    def pmw_on_core_batch_finished(self, pms_notify: bool, pms_ok: int, pms_fail: int, pms_details: list) -> None:
        """整批核心 / 内核下载结束（v1.9.17）：明确告诉用户「到底完成了没有」。

        用户反馈：点了「全部更新」之后只有进度条，跑完什么提示都没有，不知道成没成。
        现在：状态栏 + 托盘通知给一句话；**用户手动发起**的那一批再弹一次汇总框
        （自动更新走 `pcdl_start()`，`pms_notify=False`，半夜不会弹框打扰）。
        """
        stamp = pms_now_text("%H:%M:%S")
        summary = f"核心 / 内核更新完成：成功 {pms_ok} 个" + (f"，失败 {pms_fail} 个" if pms_fail else "，全部成功")
        lines = [f"{'成功' if ok else '失败'}　{pms_core_title(kind)}：{message}" for kind, ok, message in pms_details]
        self.pmw_logger.info("%s（%s）", summary, stamp)
        self.pmw_show_message(f"{summary}（{stamp}）")
        if self.pmw_settings_dialog is not None:
            self.pmw_settings_dialog.pset_show_core_batch_result(f"{summary}（{stamp}）")
        if self.pmw_tray is not None:
            self.pmw_tray.ptrc_notify(f"{PMS_APP_NAME} - 更新完成", summary)
        if pms_notify:
            detail = "\n".join(lines[:14]) or "（没有可显示的明细）"
            if len(lines) > 14:
                detail += f"\n…还有 {len(lines) - 14} 条，详见日志"
            icon = QMessageBox.Icon.Information if not pms_fail else QMessageBox.Icon.Warning
            box = QMessageBox(self)
            box.setIcon(icon)
            box.setWindowTitle("更新完成")
            box.setText(summary)
            box.setInformativeText(detail)
            box.exec()
