# -*- coding: utf-8 -*-
"""主窗口事件回调（自动切换 / API / 订阅与自动任务 / 表头与选中 / 核心状态等）——v1.9.36 从 `pms/gui/mainwindow.py` 拆出的 mixin。

★ 拆分原则：**方法名与 `self.pmw_*` 属性名一个字都没改**，外部引用（主窗口之外用到
`pmw_*` 的 80 处）全都不用动；mixins 只是把方法按功能域挪个文件。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QApplication, QMessageBox
from pms.constants import (
    PMS_APP_NAME,
    PMS_COLUMN_INDEX,
    PMS_COLUMN_TITLES,
    PMS_CORE_KIND_AUTO,
    PMS_HAPP_SCHEME,
    PMS_SHARE_SCHEME_TO_PROTOCOL,
    PMS_SORT_SCOPE_ALL,
    PMS_SORT_SCOPE_PAGE,
)
from pms.sharelinks import PmsShareLinkParser
from pms.coremeta import pms_core_title   # v1.9.45：核心显示名从已删的独立内核模块搬到 coremeta.py
from pms.utils import pms_human_duration, pms_now_text, pms_safe_int
from typing import Optional


class _PmwEventMixin:
    """主窗口事件回调（自动切换 / API / 订阅与自动任务 / 表头与选中 / 核心状态等）（28 个方法）。"""

    def pmw_autoswitch_state(self) -> dict:
        """给自动切换服务的运行时状态：核心是否在跑、当前节点、候选节点、本地代理端口。"""
        current = self.pmw_current_node()
        return {
            "core_running": self.pmw_core.pcp_is_running(),
            "current_node": current,
            "candidates": [node for node in self.pmw_nodes if node.uid != self.pmw_current_node_uid],
            "local_proxy_port": int(self.pmw_settings.psm_get_int("inbound.mixed_port", 0)),
        }

    def pmw_autoswitch_stats(self, pms_uids: list) -> dict:
        """给自动切换服务的节点历史统计（v1.8.0 综合评分用）；查不到就返回空 dict（不惩罚，记日志）。"""
        try:
            return self.pmw_repo.pnr_node_success_stats(pms_uids)
        except Exception as exc:  # noqa: BLE001 - 评分辅助数据查不出来不该影响自动切换本身
            self.pmw_logger.warning("查询节点历史成功率失败：%s", exc)
            return {}

    @Slot(str)
    def pmw_on_autoswitch_status(self, pms_message: str) -> None:
        """自动切换的过程说明：进状态栏与日志，不弹窗打断用户。"""
        if not pms_message:
            return
        self.pmw_logger.info("自动切换：%s", pms_message)
        self.pmw_status_message.setText(pms_message)

    @Slot(str, bool, int, str)
    def pmw_on_autoswitch_checked(self, pms_name: str, pms_ok: bool, pms_latency: int, pms_note: str) -> None:
        """每轮检测结果：成功只在调试级留痕，失败要让用户看见（状态栏 + 日志）。"""
        if pms_ok:
            self.pmw_logger.debug("自动切换检测通过：%s（%d ms）", pms_name, pms_latency)
            return
        self.pmw_logger.warning("自动切换检测未通过：%s（%s）", pms_name, pms_note)
        self.pmw_status_message.setText(f"当前节点检测未通过：{pms_note}")

    @Slot(str, str)
    def pmw_on_autoswitch_switch(self, pms_uid: str, pms_reason: str) -> None:
        """服务选好了目标节点 → 这里真正切换（服务不碰界面与核心）。"""
        node = self.pmw_node_by_uid(pms_uid)
        if node is None:
            self.pmw_logger.warning("自动切换目标节点已不存在：%s", pms_uid)
            return
        self.pmw_logger.warning("自动切换：%s → %s", pms_reason, node.pnode_display_name())
        self.pmw_connect_node(node)
        self.pmw_show_message(f"已自动切换到：{node.pnode_display_name()}（{pms_reason}）")
        if self.pmw_settings.psm_get_bool("autoswitch.notify", True) and self.pmw_tray is not None:
            self.pmw_tray.ptrc_notify(f"{PMS_APP_NAME} - 已自动切换", f"切换到 {node.pnode_display_name()}")

    @Slot(str)
    def pmw_on_api_server_failed(self, pms_message: str) -> None:
        """接口服务启动失败：进状态栏与日志，不弹窗打断用户。"""
        self.pmw_show_message(pms_message)
        self.pmw_logger.warning("接口服务：%s", pms_message)

    def pmw_api_state(self) -> dict:
        """给对外接口用的运行时状态快照（**只取值，不放业务逻辑**）。"""
        return {
            "core": {
                "running": self.pmw_core.pcp_is_running(),
                "kind": self.pmw_settings.psm_get_str("core.kind", PMS_CORE_KIND_AUTO),
            },
            "current_node_uid": self.pmw_current_node_uid,
            "system_proxy": self.pmw_settings.psm_get_bool("system_proxy.enabled", False),
            "tun": self.pmw_settings.psm_get_bool("tun.enabled", False),
            "traffic_totals": dict(self.pmw_traffic.pts_totals),
            "traffic_speed": dict(self.pmw_traffic.pts_speed),
            "traffic_source": self.pmw_traffic.pts_source_text,
            # v1.6.3：测速是否在跑（延迟 URL 模式 / 真实测速 / TCP 握手任一；v1.9.59 起电报 DC 测试也算）
            "test_running": (
                self.pmw_latency.plt_is_running()
                or self.pmw_speed.pst_is_running()
                or self.pmw_telegram.ptg_is_running()
                or (int(getattr(self, "pmw_tcp_total", 0) or 0) > 0
                    and int(getattr(self, "pmw_tcp_done", 0) or 0) < int(getattr(self, "pmw_tcp_total", 0) or 0))
            ),
        }

    def pmw_emit_event(self, pms_kind: str, pms_data: Optional[dict] = None) -> None:
        """记一条对外事件（v1.8.0 SSE 推送用，`GET /api/v1/events`）。

        只在这里写事件环，**HTTP 线程只读**，两边不抢锁；门面没装配或出错都不影响界面本身。
        """
        api = getattr(self, "pmw_api", None)
        if api is None:
            return
        try:
            api.papi_emit_event(pms_kind, pms_data)
        except Exception as exc:  # noqa: BLE001 - 推送是附加能力，不能因为它把界面逻辑带崩
            self.pmw_logger.debug("记录事件失败（%s）：%s", pms_kind, exc)

    def pmw_on_filter_changed(self) -> None:
        """筛选条件变化（用户操作）→ 更新代理模型、保存筛选状态并回第 1 页。"""
        self.pmw_reapply_filters(True)

    def pmw_on_group_changed(self) -> None:
        self.pmw_on_filter_changed()

    def pmw_on_header_section_pressed(self, pms_column: int) -> None:
        """用户按下表头某一列 → 标记「下一次排序来自点表头」（v1.9.14）。

        为什么能这么判：实测 PySide6 6.11 下顺序是 sectionPressed → sortIndicatorChanged → sectionClicked，
        所以本回调一定早于 pmw_on_sort_changed()。代码里主动调的 sortByColumn() 不会发 sectionPressed，
        因此自动排序 / 启动恢复仍按「全量」处理。v1.9.30 起顺手把此刻的选中 uid 记下来，排完要还回去。
        """
        self.pmw_header_sort_click = True
        self.pmw_pending_selected_uids = [node.uid for node in self.pmw_selected_nodes()]

    def pmw_on_column_resized(self, pms_index: int, pms_old: int, pms_new: int) -> None:
        self.pmw_settings.psm_set("ui.table_column_widths", [self.pmw_node_table.columnWidth(index) for index in range(len(PMS_COLUMN_TITLES))], pms_emit=False)

    def pmw_on_sort_changed(self, pms_column: int, pms_order: Qt.SortOrder) -> None:
        """表头排序指示器变化 → 落盘 + （按设置）决定「全量」还是「本页」。

        v1.9.14：默认仍是**全量**（与改动前一致）。设置 `ui.header_sort_scope = page` 时，
        **用户点表头**只排当前页——此时必须先把视图刚刚做的全量排序还原回去
        （`ui.table_sort_*` 里存的就是点击前那次全量排序，因为本方法还没覆盖它），
        否则全量顺序已经被改掉，再「只排本页」也没意义。
        代码里主动排序（自动排序 / 启动恢复）不带「点击」标记，永远按全量处理。
        """
        pms_clicked = self.pmw_header_sort_click
        self.pmw_header_sort_click = False
        # v1.9.30：点表头前记下的选中项（pmw_on_header_section_pressed 抓的），排完按 uid 还回去
        uids = self.pmw_pending_selected_uids
        self.pmw_pending_selected_uids = []
        if pms_clicked and self.pmw_settings.psm_get_str("ui.header_sort_scope", PMS_SORT_SCOPE_ALL) == PMS_SORT_SCOPE_PAGE:
            pms_prev_column = pms_safe_int(self.pmw_settings.psm_get_int("ui.table_sort_column", PMS_COLUMN_INDEX), PMS_COLUMN_INDEX, 0)
            pms_prev_asc = self.pmw_settings.psm_get_int("ui.table_sort_order", 0) == 0
            self.pmw_node_proxy.sort(pms_prev_column, Qt.SortOrder.AscendingOrder if pms_prev_asc else Qt.SortOrder.DescendingOrder)
            self.pmw_sort_current_page(pms_column, pms_order)
            self.pmw_restore_selected_uids(uids)
            self.pmw_show_message("已排序（仅当前页，可在 设置 → 界面 → 点表头排序范围 改成全量）")
            return
        self.pmw_settings.psm_set_many({
            "ui.table_sort_column": int(pms_column),
            "ui.table_sort_order": int(pms_order.value),
        }, pms_emit=False)
        # v1.7.0：排序一变，第 N 页的内容就完全换了一批，回第 1 页。
        self.pmw_apply_pagination(1, pms_reset_to_first=True)
        self.pmw_restore_selected_uids(uids)

    def pmw_on_selection_changed(self, _selected=None, _deselected=None) -> None:
        """选中变化：刷新动作启用状态，并按需要取消「全选筛选结果」范围。"""
        self.pmw_clear_selection_scope()
        self.pmw_update_connect_actions()
        self.pmw_refresh_selection_label()

    def pmw_on_subscription_dialog_closed(self, pms_result: int) -> None:
        self.pmw_subscription_dialog = None

    def pmw_on_subscription_progress(self, pms_done: int, pms_total: int, pms_name: str) -> None:
        self.pmw_progress_total = max(0, int(pms_total or 0))
        self.pmw_update_test_progress(pms_done, pms_name)

    def pmw_on_subscription_done(self, pms_uid: str, pms_result: dict) -> None:
        if pms_result.get("ok"):
            self.pmw_settings.psm_set("subscription.last_update_time", pms_now_text(), pms_emit=False)

    def pmw_on_subscription_finished(self, pms_summary: dict) -> None:
        """订阅更新完成 → 重新加载节点列表。"""
        self.pmw_end_test_progress()
        self.pmw_reload_nodes()
        text = (f"订阅更新完成：成功 {pms_summary.get('success', 0)}/{pms_summary.get('total', 0)}，"
                f"新增 {pms_summary.get('added', 0)}，更新 {pms_summary.get('updated', 0)}，移除 {pms_summary.get('removed', 0)}")
        if pms_safe_int(pms_summary.get("tor_sources"), 0) > 0:
            # v1.9.44：Tor 桥接源没有节点（节点数恒为 0），不在提示里单独说一句
            # 用户会以为「更新成功了但什么都没发生」
            text += (f"，Tor 桥接源 {pms_summary.get('tor_sources', 0)} 个"
                     f"（新增桥接 {pms_summary.get('tor_added', 0)} 条 → 设置 → 核心 → Tor 桥接出口）")
        self.pmw_show_message(text)
        if self.pmw_settings.psm_get_bool("general.notify_on_subscription_done", True) and self.pmw_tray is not None:
            self.pmw_tray.ptrc_notify(f"{PMS_APP_NAME} - 订阅更新", text)
        self.pmw_emit_event("subscription.updated", {
            "success": int(pms_summary.get("success", 0) or 0),
            "total": int(pms_summary.get("total", 0) or 0),
            "added": int(pms_summary.get("added", 0) or 0),
            "updated": int(pms_summary.get("updated", 0) or 0),
            "removed": int(pms_summary.get("removed", 0) or 0),
            "tor_added": int(pms_summary.get("tor_added", 0) or 0),
            "tor_sources": int(pms_summary.get("tor_sources", 0) or 0),
        })
        self.pmw_emit_event("subscription.updated", {
            "success": int(pms_summary.get("success", 0) or 0),
            "total": int(pms_summary.get("total", 0) or 0),
            "added": int(pms_summary.get("added", 0) or 0),
            "updated": int(pms_summary.get("updated", 0) or 0),
            "removed": int(pms_summary.get("removed", 0) or 0),
        })

    def pmw_on_auto_subscription_tick(self) -> None:
        """定时自动更新订阅。"""
        records = [record for record in self.pmw_repo.pnr_load_subscriptions() if record.enabled and record.url]
        if not records or self.pmw_subscription.psu_is_running():
            return
        self.pmw_run_subscription_update(records)

    def pmw_on_auto_test_tick(self) -> None:
        """定时自动测试延迟。

        v1.8.0：撞上断点时**先把断点跑完**再启新的一轮——
        否则每到一个周期就从头测一遍，断点里剩下的节点永远轮不到。
        """
        if self.pmw_latency.plt_is_running():
            return
        if self.pmw_latency.plt_pending_count() > 0:
            self.pmw_logger.info("自动测速：发现未完成的断点，先续跑")
            self.pmw_resume_test_round()
            return
        self.pmw_run_latency_test("all", False)

    def pmw_on_settings_dialog_closed(self, pms_result: int) -> None:
        self.pmw_settings_dialog = None
        self.pmw_apply_theme()
        self.pmw_apply_table_settings()

    def pmw_on_bottom_tab_changed(self, pms_index: int) -> None:
        """切到「错误」页签即视为已读（用户自己翻过去也算看过）。"""
        if int(pms_index) == self.pmw_error_tab_index and self.pmw_error_unread:
            self.pmw_error_unread = 0
            self.pmw_refresh_error_badge()

    def pmw_on_log_level_changed(self) -> None:
        level = str(self.pmw_log_level_combo.currentData() or "info")
        self.pmw_settings.psm_set("core.log_level", level)
        self.pmw_log_model.pltm_set_min_level(level)

    def pmw_on_log_filter_changed(self, pms_text: str) -> None:
        self.pmw_log_model.pltm_set_filter_text(pms_text)

    def pmw_on_ui_tick(self) -> None:
        """每秒刷新：运行时长、状态栏提示。"""
        if self.pmw_core.pcp_is_running():
            self.pmw_stats_labels["uptime"].setText(pms_human_duration(self.pmw_core.pcp_uptime_seconds()))
            self.pmw_status_core.setText(f"核心：运行中（{pms_human_duration(self.pmw_core.pcp_uptime_seconds())}）")
        else:
            self.pmw_stats_labels["uptime"].setText("-")

    def pmw_on_clipboard_tick(self) -> None:
        """剪贴板监听：发现新的分享链接时提示导入。"""
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        text = clipboard.text().strip()
        if not text or text == self.pmw_last_clipboard_text:
            return
        self.pmw_last_clipboard_text = text
        first_line = text.splitlines()[0].strip()
        scheme = first_line.split("://", 1)[0].lower() if "://" in first_line else ""
        if scheme == PMS_HAPP_SCHEME:
            happ_url = PmsShareLinkParser().pslp_happ_subscription_url(first_line)
            if happ_url:
                answer = QMessageBox.question(
                    self, "发现 Happ 订阅链接",
                    f"剪贴板中检测到 Happ 明文订阅链接：\n{happ_url}\n\n是否添加为订阅？"
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self.pmw_import_happ_subscription(happ_url)
            return
        # v1.9.69：t.me 代理链接是 https:// 形态，不在 PMS_SHARE_SCHEME_TO_PROTOCOL 里；
        # 放行 https/http，由 pslp_parse_line 内部按「主机+路径」特判 t.me/proxy|webproxy|socks，
        # 非 tg 的普通网页 https 会因解析出 node=None 而自然跳过（不会误弹窗）。
        if scheme not in PMS_SHARE_SCHEME_TO_PROTOCOL and scheme not in ("https", "http"):
            return
        node, error = PmsShareLinkParser().pslp_parse_line(first_line)
        if node is None:
            return
        answer = QMessageBox.question(self, "发现分享链接", f"剪贴板中检测到节点：\n{node.pnode_display_name()}\n（{node.pnode_display_address()}）\n\n是否导入？")
        if answer == QMessageBox.StandardButton.Yes:
            node.sort_index = len(self.pmw_nodes)
            self.pmw_repo.pnr_upsert_node(node)
            # v1.9.72 卡顿修复：剪贴板单节点导入走增量刷新，不整库重载
            self.pmw_apply_nodes_added([node], node.uid)
            self.pmw_show_message(f"已从剪贴板导入：{node.pnode_display_name()}")
