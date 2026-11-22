# -*- coding: utf-8 -*-
"""节点增删改查与分组（含右键菜单、批量操作、启用状态、清理）——v1.9.36 从 `pms/gui/mainwindow.py` 拆出的 mixin。

★ 拆分原则：**方法名与 `self.pmw_*` 属性名一个字都没改**，外部引用（主窗口之外用到
`pmw_*` 的 80 处）全都不用动；mixins 只是把方法按功能域挪个文件。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMenu, QMessageBox
from pms.gui.dialogs.batchops import PmsBatchOperationDialog
from pms.gui.dialogs.nodeeditor import PmsNodeEditorDialog
from pms.constants import (
    PMS_PROTOCOL_TITLES,
    PMS_PROTOCOL_TG_PROXY,
    PMS_PROTOCOL_TG_WEBPROXY,
)
from pms.models import PmsGroupRecord, PmsProxyNode
from pms.utils import pms_dedupe_keep_order, pms_now_text
from typing import Sequence


class _PmwNodeMixin:
    """节点增删改查与分组（含右键菜单、批量操作、启用状态、清理）（21 个方法）。"""

    def pmw_show_node_context_menu(self, pms_position: QPoint) -> None:
        """在鼠标位置弹出节点右键菜单。"""
        self.pmw_build_node_context_menu().exec(self.pmw_node_table.viewport().mapToGlobal(pms_position))

    def pmw_build_node_context_menu(self) -> QMenu:
        """构造节点右键菜单（v1.9.17：改成子菜单分组，找起来更快）。

        单独成方法是为了能被离线探针直接检查菜单结构（`exec()` 会阻塞，没法在探针里真弹）。

        结构：连接 / 编辑 / 副本 → 启用停用 → **测试 ▸**（**选择测试…** 弹窗 · **停止**）
        → **复制/分享 ▸** → **选择 ▸** → **清除 ▸**（清延迟 · 清测速 · 清全部成绩）
        → **危险操作 ▸**（删除选中 · 删除全部节点）→ 新增 / 导入 / 订阅管理。
        没选中节点时也**显示同一套菜单**（不相关的项置灰），避免「同一位置两套菜单」找不到东西。

        v1.9.65：原来「测试 ▸」里 12 个散项 + 「测新增的 ▸」二级子菜单收敛成一个
        **选择测试…** 弹窗入口（范围单选 + 测试项多选，用户 2026-10-15 需求）；
        续跑 / 清除成绩等仍在顶部「测试 ▾」菜单里，右键只留最常用的「选择测试 + 停止」。

        v1.9.50（审计第 11 项）：侧栏「快捷操作」面板可用 `ui.show_quick_actions` 整块关掉，
        面板里 5 个动作**别处没有同名入口**（全部启用 / 全部停用 / 反转选择 / 按延迟排序 / 复制地址）。
        这里给它们补上右键镜像入口，**复用面板按钮同一个槽函数**（不另写一套逻辑）：
        前 4 个落在新二级菜单 **整体操作 ▸**，「复制地址」落在 **复制/分享 ▸**（那里本来就有
        行内 `复制节点地址 host:port`，两个动作都要留着、语义不同：前者是面板原样的「复制地址」，
        后者是行内补的 host:port 版本）。镜像入口**与面板可见性无关**，面板隐藏时照样能用。
        """
        menu = QMenu(self)
        selected = self.pmw_selected_nodes()
        has_selection = bool(selected)
        visible_count = self.pmw_node_proxy.rowCount()

        def add(pms_key: str, pms_enabled: bool = True) -> None:
            action = self.pmw_actions[pms_key]
            menu.addAction(action)
            action.setEnabled(pms_enabled)

        connect_action = menu.addAction("连接该节点（启动核心）", lambda: self.pmw_connect_selected_node())
        connect_action.setEnabled(has_selection)
        add("edit", has_selection)
        add("duplicate", has_selection)
        menu.addSeparator()
        enable_action = menu.addAction("启用", lambda: self.pmw_set_selected_enabled(True))
        disable_action = menu.addAction("停用", lambda: self.pmw_set_selected_enabled(False))
        enable_action.setEnabled(has_selection)
        disable_action.setEnabled(has_selection)

        # v1.9.65：原来 12 个散项 + 「测新增的 ▸」二级子菜单收敛成一个「选择测试…」弹窗入口
        test_menu = self.pmw_add_submenu(menu, "测试 ▸")
        test_menu.addAction(self.pmw_actions["test_picker"])
        self.pmw_actions["test_picker"].setEnabled(visible_count > 0 or bool(self.pmw_test_round_list()))
        # v1.9.68：电报代理连通直测（粘贴 t.me/proxy 链接，不依赖节点表选中）
        test_menu.addAction(self.pmw_actions["tg_proxy_test"])
        self.pmw_actions["tg_proxy_test"].setEnabled(True)
        test_menu.addSeparator()
        for key in ("stop_latency", "stop_speed", "stop_tests"):
            action = self.pmw_actions[key]
            test_menu.addAction(action)
            action.setEnabled(True)

        copy_menu = self.pmw_add_submenu(menu, "复制 / 分享 ▸")
        for key in ("copy_link", "copy_config", "share"):
            action = self.pmw_actions[key]
            copy_menu.addAction(action)
            action.setEnabled(has_selection)
        copy_menu.addAction("复制节点地址 host:port", self.pmw_copy_selected_address)
        # v1.9.67：用户要求「复制也一样，可以复制已经选择的类型」——
        # 直接按类型复制分享链接，不必先选中再复制。
        copy_menu.addSeparator()
        type_copy_menu = self.pmw_add_submenu(copy_menu, "按类型复制 ▸")
        self.pmw_type_menu(type_copy_menu, "copy")
        # v1.9.71：钉死的「Telegram 专属代理」复制入口——两种始终可见、计数取自全量节点，
        # 不被当前筛选条件遮蔽（动态「按类型复制」只在当前筛选结果里恰好有该类型时才出现，
        # 被其它筛选排除时就点不到，这两种是用户高频要复制的电报代理类型）。
        copy_menu.addSeparator()
        tg_copy_menu = self.pmw_add_submenu(copy_menu, "Telegram 专属代理 ▸")
        tg_copy_menu.addAction(
            f"{PMS_PROTOCOL_TITLES[PMS_PROTOCOL_TG_PROXY]}（{self.pmw_count_all_of_type(PMS_PROTOCOL_TG_PROXY)} 个）",
            lambda: self.pmw_copy_all_of_type(PMS_PROTOCOL_TG_PROXY),
        )
        tg_copy_menu.addAction(
            f"{PMS_PROTOCOL_TITLES[PMS_PROTOCOL_TG_WEBPROXY]}（{self.pmw_count_all_of_type(PMS_PROTOCOL_TG_WEBPROXY)} 个）",
            lambda: self.pmw_copy_all_of_type(PMS_PROTOCOL_TG_WEBPROXY),
        )
        # v1.9.76 方案 B：「一键发送到 Telegram」——直接唤起客户端打开 t.me/proxy 链接，
        # 唤起失败时自动退回「复制到剪贴板」（Telegram 桌面/手机点链接即可加代理）。
        tg_copy_menu.addSeparator()
        tg_send_menu = self.pmw_add_submenu(tg_copy_menu, "用 Telegram 打开 ▸")
        tg_send_menu.addAction("发送选中的电报代理", self.pmwt_send_selected_to_telegram)
        tg_send_menu.addAction("发送当前页可见的电报代理", self.pmwt_send_page_to_telegram)

        select_menu = self.pmw_add_submenu(menu, "选择 ▸")
        select_menu.addAction(f"全选（筛选结果 {visible_count} 条）", self.pmw_select_all_filtered)
        select_menu.addAction("只选本页", self.pmw_select_current_page)
        select_menu.addAction("反转选择（本页）", self.pmw_invert_selection)
        # v1.9.67：用户要求「右键选择再增加按类型选择」。
        select_menu.addSeparator()
        type_select_menu = self.pmw_add_submenu(select_menu, "按类型选择 ▸")
        self.pmw_type_menu(type_select_menu, "select")

        # v1.9.70：右键「过滤显示 ▸」——复用侧栏那组筛选控件（与工具栏「分组 / 搜索」同一份），
        # 不新建状态：点一下即把对应筛选条件设好并刷新；「清除筛选」一键还原全部条件。
        filter_menu = self.pmw_add_submenu(menu, "过滤显示 ▸")
        self.pmw_build_filter_submenu(filter_menu)

        # v1.9.50（审计第 11 项）：「快捷操作」面板里的 5 个动作补镜像入口。
        # 槽函数与面板按钮**完全同一份**（pmw_set_all_enabled / pmw_invert_selection /
        # pmw_sort_by_latency / pmw_copy_selected_address），这里只多挂一个入口。
        # 触发用 QAction.trigger() + 真实触发（不是直接调槽），所以「点面板按钮」与
        # 「点右键菜单」在 Qt 看来是同一条路：同样发 triggered 信号、同样只跑一次。
        bulk_menu = self.pmw_add_submenu(menu, "整体操作 ▸")
        self.pmw_quick_mirror_actions: dict[str, QAction] = {}
        for pms_key, pms_title, pms_handler in (
            ("quick_enable_all", "全部启用", lambda: self.pmw_set_all_enabled(True)),
            ("quick_disable_all", "全部停用", lambda: self.pmw_set_all_enabled(False)),
            ("quick_invert_selection", "反转选择（本页）", self.pmw_invert_selection),
            ("quick_sort_latency", "按延迟排序", self.pmw_sort_by_latency),
            ("quick_copy_address", "复制地址", self.pmw_copy_selected_address),
        ):
            action = QAction(pms_title, bulk_menu)
            action.setObjectName(f"pmw_{pms_key}")
            action.triggered.connect(pms_handler)
            self.pmw_quick_mirror_actions[pms_key] = action
        bulk_menu.addAction(self.pmw_quick_mirror_actions["quick_enable_all"])
        bulk_menu.addAction(self.pmw_quick_mirror_actions["quick_disable_all"])
        bulk_menu.addSeparator()
        bulk_menu.addAction(self.pmw_quick_mirror_actions["quick_invert_selection"])
        bulk_menu.addSeparator()
        bulk_menu.addAction(self.pmw_quick_mirror_actions["quick_sort_latency"])
        bulk_menu.addAction(self.pmw_quick_mirror_actions["quick_copy_address"])

        menu.addSeparator()
        clear_menu = self.pmw_add_submenu(menu, "清除 ▸")
        for key in ("clear_latency", "clear_speed", "clear_telegram", "clear_tests"):
            clear_menu.addAction(self.pmw_actions[key])

        danger_menu = self.pmw_add_submenu(menu, "危险操作 ▸")
        danger_menu.addAction(self.pmw_actions["delete"])
        danger_menu.addAction(self.pmw_actions["delete_all_nodes"])
        menu.addSeparator()
        menu.addAction(self.pmw_actions["add"])
        menu.addAction(self.pmw_actions["import"])
        menu.addAction(self.pmw_actions["subscription"])
        menu.addAction(self.pmw_actions["cleanup"])
        return menu

    def pmw_add_node(self) -> None:
        """新增节点。"""
        dialog = PmsNodeEditorDialog(None, self.pmw_settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.pned_result_node is not None:
            node = dialog.pned_result_node
            node.sort_index = len(self.pmw_nodes)
            self.pmw_repo.pnr_upsert_node(node)
            # v1.9.72 卡顿修复：单节点新增走增量刷新，不再全量重载 55k 库（省 ~13.7s）
            self.pmw_apply_nodes_added([node], node.uid)
            self.pmw_show_message(f"已新增节点：{node.pnode_display_name()}")

    def pmw_edit_node(self) -> None:
        """编辑选中节点（双击或回车）。"""
        nodes = self.pmw_selected_nodes()
        if not nodes:
            QMessageBox.information(self, "提示", "请先选择一个节点")
            return
        dialog = PmsNodeEditorDialog(nodes[0], self.pmw_settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.pned_result_node is not None:
            self.pmw_repo.pnr_upsert_node(dialog.pned_result_node)
            self.pmw_update_node_everywhere(dialog.pned_result_node)
            self.pmw_reload_groups()
            self.pmw_show_message(f"已保存节点：{dialog.pned_result_node.pnode_display_name()}")

    def pmw_duplicate_node(self) -> None:
        """复制选中节点。"""
        nodes = self.pmw_selected_nodes()
        if not nodes:
            return
        clones = [node.pnode_clone(pms_new_uid=True, pms_new_name=True) for node in nodes]
        for index, clone in enumerate(clones):
            clone.sort_index = len(self.pmw_nodes) + index
        self.pmw_repo.pnr_upsert_nodes(clones)
        # v1.9.72 卡顿修复：复制走增量刷新，不再全量重载
        self.pmw_apply_nodes_added(clones, clones[0].uid)
        self.pmw_show_message(f"已复制 {len(clones)} 个节点")

    def pmw_delete_nodes(self) -> None:
        """删除选中节点（需要确认）。"""
        nodes = self.pmw_selected_nodes()
        if not nodes:
            QMessageBox.information(self, "提示", "请先选择要删除的节点")
            return
        if QMessageBox.question(self, "确认删除", f"确定删除 {len(nodes)} 个节点吗？该操作不可恢复。") != QMessageBox.StandardButton.Yes:
            return
        self.pmw_repo.pnr_delete_nodes([node.uid for node in nodes])
        # v1.9.72 卡顿修复：删除走增量刷新，不再全量重载
        self.pmw_apply_nodes_removed([node.uid for node in nodes])
        self.pmw_show_message(f"已删除 {len(nodes)} 个节点")

    def pmw_clear_test_results(self, pms_mode: str) -> None:
        """一键清除测试成绩（v1.9.17）：范围固定为**全部节点**（用户确认过的口径）。

        `pms_mode`：latency / speed / telegram / both / all。只清 `pms_nodes` 里的成绩列，
        **不动 `pms_latency_history`**——那是统计与自动切换评分用的独立历史。

        v1.9.67：新增 `telegram`（只清电报）与 `all`（延迟+测速+电报三列一起清）——
        用户要求「一键清理测试数据应当包括 延迟 测速 电报 三列」。
        """
        nodes = self.pmw_nodes
        label = {
            "latency": "延迟", "speed": "测速", "telegram": "电报",
            "both": "延迟与测速", "all": "延迟、测速与电报",
        }.get(pms_mode, pms_mode)
        if not nodes:
            QMessageBox.information(self, "提示", "节点列表是空的")
            return
        extra = "（历史记录与自动切换评分不受影响）" if pms_mode in ("latency", "speed", "telegram") \
            else "（历史记录保留，「最近测试时间」会一起复位）"
        answer = QMessageBox.question(
            self, "确认清除",
            f"确定清除全部 {len(nodes)} 个节点的{label}成绩吗？{extra}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pmw_repo.pnr_clear_test_results([node.uid for node in nodes], pms_mode)
        clear_latency = pms_mode in ("latency", "both", "all")
        clear_speed = pms_mode in ("speed", "both", "all")
        clear_telegram = pms_mode in ("telegram", "all")
        for node in nodes:
            if clear_latency:
                node.latency_ms = None
                node.last_test_ok = 0
                node.last_test_note = ""
            if clear_speed:
                node.speed_bps = 0.0
            if clear_telegram:
                # v1.9.67：与 database.pnr_clear_test_results 的 mode=telegram 分支保持一致
                node.telegram_ok = 0
                node.telegram_best_ms = -1
                node.telegram_best_dc = ""
                node.telegram_reachable = 0
                node.telegram_tcp_count = 0
                node.telegram_total = 0
                node.telegram_speed_bps = 0.0
                node.telegram_note = ""
            if pms_mode in ("both", "all"):
                node.last_test_time = ""
                node.last_test_mode = ""
        self.pmw_node_model.pntm_update_nodes(nodes)
        self.pmw_refresh_summary()
        self.pmw_update_connect_actions()
        self.pmw_show_message(f"已清除全部节点的{label}成绩（{len(nodes)} 个）")

    def pmw_delete_all_nodes(self) -> None:
        """一键删除全部节点（v1.9.17）：要**手动输入确认词**，双击确认太容易误触。

        只删节点，分组与订阅记录保留（订阅下次更新会把节点重新拉回来）。
        """
        total = len(self.pmw_nodes)
        if not total:
            QMessageBox.information(self, "提示", "节点列表是空的")
            return
        text, ok = QInputDialog.getText(
            self, "删除全部节点",
            f"将删除**全部 {total} 个节点**，此操作不可恢复（分组与订阅记录会保留，订阅下次更新会重新拉回节点）。\n\n请输入「删除全部」以确认：",
        )
        if not ok or str(text).strip() != "删除全部":
            self.pmw_show_message("已取消删除全部节点（确认词不匹配）")
            return
        removed = self.pmw_repo.pnr_delete_nodes([node.uid for node in self.pmw_nodes])
        self.pmw_current_node_uid = ""
        self.pmw_settings.psm_set("general.last_node_uid", "", pms_emit=False)
        self.pmw_reload_nodes()
        self.pmw_show_message(f"已删除全部节点（{removed} 个）")

    def pmw_set_all_enabled(self, pms_enabled: bool) -> None:
        """整表启用 / 停用。"""
        if not self.pmw_nodes:
            return
        self.pmw_repo.pnr_update_enabled([node.uid for node in self.pmw_nodes], pms_enabled)
        for node in self.pmw_nodes:
            node.enabled = bool(pms_enabled)
        self.pmw_node_model.pntm_set_all_enabled(pms_enabled)
        self.pmw_show_message("已全部启用" if pms_enabled else "已全部停用")

    def pmw_set_selected_enabled(self, pms_enabled: bool) -> None:
        """启用 / 停用选中节点。"""
        nodes = self.pmw_selected_nodes()
        if not nodes:
            return
        self.pmw_repo.pnr_update_enabled([node.uid for node in nodes], pms_enabled)
        for node in nodes:
            node.enabled = bool(pms_enabled)
            self.pmw_node_model.pntm_update_node(node)
        self.pmw_refresh_summary()
        self.pmw_show_message(f"已{'启用' if pms_enabled else '停用'} {len(nodes)} 个节点")

    def pmw_invert_selection(self) -> None:
        """反转选择（**只在当前页内**——跨页反转无法在表格上表达，故明确限定范围）。"""
        selection = self.pmw_node_table.selectionModel()
        if selection is None:
            return
        self.pmw_select_all_scope = ""
        self.pmw_select_by_type_uids = set()   # v1.9.67：反转选择不是类型范围
        for row in range(self.pmw_node_page.rowCount()):
            index = self.pmw_node_page.index(row, 0)
            selection.select(index, selection.SelectionFlag.Toggle | selection.SelectionFlag.Rows)
        self.pmw_refresh_selection_label()

    # ---------------------------------------------------------------- 按类型选择 / 复制（v1.9.67）
    def pmw_available_types(self) -> list[tuple[str, int]]:
        """当前**筛选结果**里出现过的协议类型，返回 [(协议值, 条数), ...]，按条数降序。

        v1.9.67：供右键「选择 ▸ 按类型」「复制 ▸ 按类型」列出候选。
        只统计 `pmw_visible_nodes()`（用户眼前这套筛选结果），与「全选（筛选结果 N 条）」
        的口径保持一致——不然菜单里会列出当前根本没显示的节点类型。
        """
        counter: dict[str, int] = {}
        for node in self.pmw_visible_nodes():
            key = str(node.protocol or "").lower()
            counter[key] = counter.get(key, 0) + 1
        return sorted(counter.items(), key=lambda item: (-item[1], item[0]))

    def pmw_build_filter_submenu(self, pms_menu: QMenu) -> None:
        """构造右键「过滤显示 ▸」子菜单（v1.9.70）。

        复用侧栏那组筛选控件（`pmw_protocol_combo` / `pmw_search_edit` /
        `pmw_chk_only_enabled` / `pmw_chk_only_available` / `pmw_chk_only_failed`）——
        与工具栏「分组 / 搜索 ▾」按钮是**同一份控件**，所以这里只负责把对应条件设好并触发
        `pmw_on_filter_changed`，绝不新建一份筛选状态（避免「两套筛选」打架）。

        协议筛选用「按类型过滤」：当前筛选结果里有哪些协议类型就列哪些（带计数），
        点一下把 `pmw_protocol_combo` 切到该协议。关键字清空后逐项点选即在原结果内再筛。
        「清除筛选」一键还原全部条件。子菜单始终可用（没有选中节点也能按协议 / 状态浏览列表）。
        """
        # ---- 按协议类型过滤（在「当前筛选结果」里再缩）----
        protocol_menu = self.pmw_add_submenu(pms_menu, "按类型过滤 ▸")
        types = self.pmw_available_types()
        if not types:
            empty = protocol_menu.addAction("（当前结果里没有节点）")
            empty.setEnabled(False)
        else:
            protocol_menu.addAction(
                "全部协议", lambda: self.pmw_filter_set_protocol("")
            ).setCheckable(True)
            current_protocol = str(self.pmw_protocol_combo.currentData() or "")
            protocol_menu.actions()[-1].setChecked(current_protocol == "")
            protocol_menu.addSeparator()
            for protocol, count in types:
                title = PMS_PROTOCOL_TITLES.get(protocol, protocol)
                action = protocol_menu.addAction(
                    f"{title}（{count} 个）",
                    lambda p=protocol: self.pmw_filter_set_protocol(p),
                )
                action.setCheckable(True)
                action.setChecked(current_protocol == protocol)

        # ---- 按状态过滤（三选，可叠加）----
        status_menu = self.pmw_add_submenu(pms_menu, "按状态过滤 ▸")
        for check, title in (
            (self.pmw_chk_only_enabled, "只看已启用"),
            (self.pmw_chk_only_available, "只看可用（有延迟）"),
            (self.pmw_chk_only_failed, "只看不可用"),
        ):
            action = status_menu.addAction(title, lambda c=check: self.pmw_filter_toggle_check(c))
            action.setCheckable(True)
            action.setChecked(check.isChecked())

        pms_menu.addSeparator()
        # v1.9.71：钉死的「Telegram 专属代理」过滤入口——两种始终可见、计数取自全量节点，
        # 不被当前筛选条件遮蔽（动态「按类型过滤」只在当前筛选结果里恰好有该类型时才出现）。
        # 点一下把协议筛切到该类型，直接「只看」这两类电报代理。
        tg_menu = self.pmw_add_submenu(pms_menu, "Telegram 专属代理 ▸")
        current_protocol = str(self.pmw_protocol_combo.currentData() or "")
        for protocol in (PMS_PROTOCOL_TG_PROXY, PMS_PROTOCOL_TG_WEBPROXY):
            title = PMS_PROTOCOL_TITLES.get(protocol, protocol)
            action = tg_menu.addAction(
                f"只看 {title}（{self.pmw_count_all_of_type(protocol)} 个）",
                lambda p=protocol: self.pmw_filter_set_protocol(p),
            )
            action.setCheckable(True)
            action.setChecked(current_protocol == protocol)

        pms_menu.addSeparator()
        reset = pms_menu.addAction("清除筛选（显示全部）", self.pmw_filter_clear_all)
        reset.setToolTip("还原协议 / 关键字 / 三个状态勾选，回到「全部节点」")

    def pmw_filter_set_protocol(self, pms_protocol: str) -> None:
        """把协议筛选切到指定协议（"" = 全部），并刷新列表。"""
        index = self.pmw_protocol_combo.findData(pms_protocol)
        if index < 0:
            index = self.pmw_protocol_combo.findData("")
        self.pmw_protocol_combo.setCurrentIndex(max(0, index))
        self.pmw_on_filter_changed()

    def pmw_filter_toggle_check(self, pms_check) -> None:
        """翻转某个状态筛选勾选（点一下在「勾 / 不勾」之间切，复用同一控件）。"""
        pms_check.setChecked(not pms_check.isChecked())
        self.pmw_on_filter_changed()

    def pmw_filter_clear_all(self) -> None:
        """一键还原全部筛选条件（协议、关键字、三个状态勾选）。"""
        self.pmw_protocol_combo.setCurrentIndex(max(0, self.pmw_protocol_combo.findData("")))
        self.pmw_search_edit.setText("")
        for check in (self.pmw_chk_only_enabled, self.pmw_chk_only_available, self.pmw_chk_only_failed):
            if check.isChecked():
                check.setChecked(False)
        self.pmw_on_filter_changed()
        self.pmw_show_message("已清除筛选，显示全部节点")

    def pmw_type_menu(self, pms_parent, pms_kind: str) -> None:
        """往菜单里追加「按类型」子项（pms_kind：select / copy）。

        两种用法共用同一份类型清单与同一条节点过滤逻辑，只是末尾动作不同：
        `select` 选中这些节点，`copy` 复制这些节点的分享链接。
        """
        options = self.pmw_available_types()
        if not options:
            action = pms_parent.addAction("按类型（当前没有可用的节点）")
            action.setEnabled(False)
            return
        for protocol, count in options:
            title = PMS_PROTOCOL_TITLES.get(protocol, protocol)
            label = f"{title}（{count} 个）"
            if pms_kind == "select":
                pms_parent.addAction(label, lambda p=protocol: self.pmw_select_by_type(p))
            else:
                pms_parent.addAction(label, lambda p=protocol: self.pmw_copy_by_type(p))

    def pmw_nodes_of_type(self, pms_protocol: str) -> list[PmsProxyNode]:
        """筛选结果里指定协议类型的全部节点（保持原顺序）。"""
        target = str(pms_protocol or "").lower()
        return [node for node in self.pmw_visible_nodes() if str(node.protocol or "").lower() == target]

    def pmw_select_by_type(self, pms_protocol: str) -> None:
        """按协议类型选中节点（跨页：记在 `pmw_select_all_scope` 上，与「全选筛选结果」同机制）。

        ★ 复用既有的 `pmw_bulk_selected_nodes()` 口径：选中范围用
        `pmw_select_all_scope="filtered"` + 过滤条件表达，避免因为表格只渲染当前页而漏选。
        """
        nodes = self.pmw_nodes_of_type(pms_protocol)
        title = PMS_PROTOCOL_TITLES.get(str(pms_protocol or "").lower(), pms_protocol)
        if not nodes:
            self.pmw_show_message(f"当前筛选结果里没有「{title}」类型的节点")
            return
        # 与该类型的节点集合绑定：既做视觉反馈（尽可能选中当前页可见的那些），
        # 又让后续批量 / 复制动作拿到完整范围。
        wanted = {node.uid for node in nodes}
        self.pmw_applying_selection = True
        try:
            self.pmw_select_all_scope = "filtered"
            self.pmw_node_table.clearSelection()
            selection = self.pmw_node_table.selectionModel()
            if selection is not None:
                for row in range(self.pmw_node_page.rowCount()):
                    index = self.pmw_node_page.index(row, 0)
                    source = self.pmw_node_page.mapToSource(index)
                    node = self.pmw_node_model.pntm_node_at(source.row())
                    if node is not None and node.uid in wanted:
                        selection.select(index, selection.SelectionFlag.Select | selection.SelectionFlag.Rows)
        finally:
            self.pmw_applying_selection = False
        self.pmw_select_by_type_uids = wanted  # 供 pmw_selected_nodes 精确取用
        self.pmw_update_connect_actions()
        self.pmw_refresh_selection_label()
        self.pmw_show_message(f"已选中「{title}」类型 {len(nodes)} 个节点")

    def pmw_copy_by_type(self, pms_protocol: str) -> None:
        """按协议类型复制分享链接（用户需求：「复制也一样，可以复制已经选择的类型」）。"""
        nodes = self.pmw_nodes_of_type(pms_protocol)
        title = PMS_PROTOCOL_TITLES.get(str(pms_protocol or "").lower(), pms_protocol)
        if not nodes:
            self.pmw_show_message(f"当前筛选结果里没有「{title}」类型的节点")
            return
        self.pmw_copy_links_to_clipboard(nodes)

    # v1.9.71：钉死的「Telegram 专属代理」入口用——计数与复制只认全量节点，
    # 不受「当前筛选结果」影响（动态列表被其它筛选条件排除时就点不到，这两种是高频类型）。
    def pmw_count_all_of_type(self, pms_protocol: str) -> int:
        """全量节点里指定协议类型的条数（不看当前筛选）。"""
        target = str(pms_protocol or "").lower()
        return sum(1 for node in self.pmw_nodes if str(node.protocol or "").lower() == target)

    def pmw_copy_all_of_type(self, pms_protocol: str) -> None:
        """复制全量节点里指定协议类型的分享链接（不看当前筛选）。"""
        title = PMS_PROTOCOL_TITLES.get(str(pms_protocol or "").lower(), pms_protocol)
        nodes = [node for node in self.pmw_nodes if str(node.protocol or "").lower() == str(pms_protocol or "").lower()]
        if not nodes:
            self.pmw_show_message(f"节点库里没有「{title}」类型的节点（从 t.me/proxy 或 t.me/webproxy 导入后这里才有）")
            return
        self.pmw_copy_links_to_clipboard(nodes)
        self.pmw_show_message(f"已复制「{title}」类型 {len(nodes)} 个节点的分享链接")

    def pmw_copy_selected_address(self) -> None:
        """复制选中节点的地址。"""
        nodes = self.pmw_selected_nodes()
        if not nodes:
            return
        text = "\n".join(node.pnode_display_address() for node in nodes)
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        self.pmw_show_message(f"已复制 {len(nodes)} 个地址")

    def pmw_connect_selected_node(self) -> None:
        """连接选中节点（双击 / 右键「连接该节点」）。"""
        nodes = self.pmw_selected_nodes()
        if not nodes:
            QMessageBox.information(self, "提示", "请先选择一个节点")
            return
        self.pmw_connect_node(nodes[0])

    def pmw_connect_node(self, pms_node: PmsProxyNode) -> None:
        """把节点设为当前节点并启动核心（双击、右键、自动切换都走这里）。"""
        errors = pms_node.pnode_validate()
        if errors:
            QMessageBox.warning(self, "节点配置有误", "；".join(errors))
            return
        self.pmw_current_node_uid = pms_node.uid
        self.pmw_settings.psm_set("general.last_node_uid", pms_node.uid)
        self.pmw_highlight_current_node()
        if self.pmw_core.pcp_is_running():
            self.pmw_core.pcp_stop("切换节点")
            QTimer.singleShot(400, lambda: self.pmw_start_core(pms_node))
        else:
            self.pmw_start_core(pms_node)

    def pmw_cleanup_nodes(self) -> None:
        """按地址去重（保留延迟最低的）并删除明显无效的节点。"""
        nodes = self.pmw_repo.pnr_load_all_nodes()
        if not nodes:
            return
        groups: dict[str, list[PmsProxyNode]] = {}
        invalid: list[str] = []
        for node in nodes:
            errors = node.pnode_validate()
            if errors:
                invalid.append(node.uid)
                continue
            key = node.pnode_endpoint_signature() if self.pmw_settings.psm_get_bool("advanced.dedup_by_server_port", True) else node.pnode_signature()
            groups.setdefault(key, []).append(node)
        removed: list[str] = list(invalid)
        for key, items in groups.items():
            if len(items) <= 1:
                continue
            items.sort(key=lambda item: (item.latency_ms is None or item.latency_ms < 0, item.latency_ms if item.latency_ms is not None and item.latency_ms >= 0 else 10 ** 9))
            removed.extend(item.uid for item in items[1:])
        if not removed:
            QMessageBox.information(self, "清理完成", "没有发现重复或无效的节点。")
            return
        if QMessageBox.question(self, "确认清理", f"将删除 {len(removed)} 个重复/无效节点（重复项保留延迟最低的），继续？") != QMessageBox.StandardButton.Yes:
            return
        self.pmw_repo.pnr_delete_nodes(removed)
        self.pmw_reload_nodes()
        self.pmw_show_message(f"清理完成，删除 {len(removed)} 个节点")

    def pmw_show_group_menu(self, pms_pos: QPoint) -> None:
        """分组列表右键菜单（与下方「分组管理 ▾」按钮同一份）。"""
        self.pmw_group_menu.exec(self.pmw_group_list.mapToGlobal(pms_pos))

    def pmw_add_group(self) -> None:
        """新增分组（仅建记录，节点归属在使用时产生）。"""
        name, ok = self.pmw_ask_text("新增分组", "请输入分组名称：")
        if not ok or not name.strip():
            return
        group = PmsGroupRecord(name=name.strip(), sort_index=len(self.pmw_repo.pnr_load_groups()))
        self.pmw_repo.pnr_upsert_group(group)
        self.pmw_reload_groups()
        self.pmw_show_message(f"已新增分组：{group.name}")

    def pmw_rename_group(self) -> None:
        """重命名分组（同步更新该分组下所有节点）。"""
        item = self.pmw_group_list.currentItem()
        group_name = str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
        if not group_name:
            QMessageBox.information(self, "提示", "请先在左侧选择一个分组（“全部分组”不能重命名）")
            return
        new_name, ok = self.pmw_ask_text("重命名分组", "请输入新名称：", group_name)
        if not ok or not new_name.strip() or new_name.strip() == group_name:
            return
        self.pmw_repo.pnr_update_group_name(group_name, new_name.strip())
        for group in self.pmw_repo.pnr_load_groups():
            if group.name == group_name:
                self.pmw_repo.pnr_delete_group(group.uid)
                break
        self.pmw_repo.pnr_upsert_group(PmsGroupRecord(name=new_name.strip()))
        # v1.9.72 卡顿修复：就地改内存里该分组下所有节点的 group_name，行级刷新，不整库重载
        changed = [node for node in self.pmw_nodes if node.group_name == group_name]
        for node in changed:
            node.group_name = new_name.strip()
            node.updated_at = pms_now_text()
        self.pmw_node_model.pntm_update_nodes(changed)
        self.pmw_reload_groups()
        self.pmw_refresh_summary()
        self.pmw_show_message(f"分组已重命名：{group_name} → {new_name.strip()}")

    def pmw_delete_group(self) -> None:
        """删除分组（节点移动到默认分组）。"""
        item = self.pmw_group_list.currentItem()
        group_name = str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
        if not group_name:
            QMessageBox.information(self, "提示", "请先选择一个分组")
            return
        if QMessageBox.question(self, "确认删除", f"删除分组「{group_name}」？其中的节点会移动到「默认分组」。") != QMessageBox.StandardButton.Yes:
            return
        self.pmw_repo.pnr_update_group_name(group_name, "默认分组")
        for group in self.pmw_repo.pnr_load_groups():
            if group.name == group_name:
                self.pmw_repo.pnr_delete_group(group.uid)
        # v1.9.72 卡顿修复：把该分组下节点就地挪到「默认分组」并刷新，不整库重载
        changed = [node for node in self.pmw_nodes if node.group_name == group_name]
        for node in changed:
            node.group_name = "默认分组"
            node.updated_at = pms_now_text()
        self.pmw_node_model.pntm_update_nodes(changed)
        self.pmw_reload_groups()
        self.pmw_refresh_summary()
        self.pmw_show_message(f"已删除分组：{group_name}")

    def pmw_open_batch_dialog(self) -> None:
        """打开批量操作对话框（针对选中节点）。"""
        nodes = self.pmw_selected_nodes()
        if not nodes:
            nodes = self.pmw_visible_nodes()
        if not nodes:
            QMessageBox.information(self, "提示", "没有可操作的节点")
            return
        groups = pms_dedupe_keep_order([group.name for group in self.pmw_repo.pnr_load_groups()] + [node.group_name for node in nodes])
        dialog = PmsBatchOperationDialog(nodes, groups, self)
        dialog.pbod_operation_requested.connect(lambda operation, params, nodes=nodes: self.pmw_handle_batch_operation(operation, params, nodes))
        dialog.exec()

    def pmw_handle_batch_operation(self, pms_operation: str, pms_params: dict, pms_nodes: Sequence[PmsProxyNode]) -> None:
        """执行批量操作。"""
        nodes = list(pms_nodes)
        if pms_operation == "enable" or pms_operation == "disable":
            enabled = pms_operation == "enable"
            self.pmw_repo.pnr_update_enabled([node.uid for node in nodes], enabled)
            for node in nodes:
                node.enabled = enabled
            # v1.9.72 卡顿修复：批量启停只改字段，行级刷新，不整库重载
            self.pmw_apply_nodes_updated(nodes)
            self.pmw_show_message(f"已{'启用' if enabled else '停用'} {len(nodes)} 个节点")
        elif pms_operation == "move_group":
            group = str(pms_params.get("group") or "默认分组")
            self.pmw_repo.pnr_engine.pdb_execute_many(
                "UPDATE pms_nodes SET group_name=?, updated_at=? WHERE uid=?",
                [(group, pms_now_text(), node.uid) for node in nodes],
            )
            self.pmw_repo.pnr_upsert_group(PmsGroupRecord(name=group))
            # v1.9.72 卡顿修复：就地改内存 group_name 并刷新，不整库重载
            for node in nodes:
                node.group_name = group
                node.updated_at = pms_now_text()
            self.pmw_apply_nodes_updated(nodes)
            self.pmw_reload_groups()
            self.pmw_show_message(f"已把 {len(nodes)} 个节点移动到分组「{group}」")
        elif pms_operation == "rename":
            prefix = str(pms_params.get("prefix") or "")
            suffix = str(pms_params.get("suffix") or "")
            renamed = 0
            renamed_nodes: list[PmsProxyNode] = []
            for node in nodes:
                new_name = (prefix + node.pnode_display_name() + suffix).strip()
                if not new_name or new_name == node.name:
                    continue
                node.name = new_name
                node.updated_at = pms_now_text()
                self.pmw_repo.pnr_upsert_node(node)
                renamed += 1
                renamed_nodes.append(node)
            # v1.9.72 卡顿修复：重命名只改字段，行级刷新，不整库重载
            self.pmw_apply_nodes_updated(renamed_nodes)
            self.pmw_show_message(f"已重命名 {renamed} 个节点")
        elif pms_operation == "dedupe":
            seen: set[str] = set()
            duplicates: list[str] = []
            ordered = sorted(nodes, key=lambda item: (item.latency_ms is None or item.latency_ms < 0,
                                                      item.latency_ms if item.latency_ms is not None and item.latency_ms >= 0 else 10 ** 9))
            for node in ordered:
                key = node.pnode_endpoint_signature()
                if key in seen:
                    duplicates.append(node.uid)
                else:
                    seen.add(key)
            if duplicates:
                self.pmw_repo.pnr_delete_nodes(duplicates)
            # v1.9.72 卡顿修复：去重删除走增量删除，不整库重载
            self.pmw_apply_nodes_removed(duplicates)
            self.pmw_show_message(f"去重完成，删除 {len(duplicates)} 个重复节点")
        elif pms_operation == "sort_latency":
            ordered = sorted(nodes, key=lambda item: (item.latency_ms is None or item.latency_ms < 0,
                                                      item.latency_ms if item.latency_ms is not None and item.latency_ms >= 0 else 10 ** 9))
            # v1.9.72 卡顿修复：只写库里的 sort_index（持久化顺序），视觉排序交给下面
            # `pmw_sort_by_latency`（内存内 pntm_sort_by 发 layoutChanged，~1.7s，比 13.7s 全量重载轻得多）
            self.pmw_repo.pnr_update_sort_indices([(node.uid, index) for index, node in enumerate(ordered)])
            self.pmw_sort_by_latency()
        elif pms_operation == "delete":
            self.pmw_repo.pnr_delete_nodes([node.uid for node in nodes])
            # v1.9.72 卡顿修复：批量删除走增量删除，不整库重载
            self.pmw_apply_nodes_removed([node.uid for node in nodes])
            self.pmw_show_message(f"已删除 {len(nodes)} 个节点")
        elif pms_operation in ("test_latency", "test_latency_speed"):
            ok, message = self.pmw_latency.plt_start(nodes, pms_operation == "test_latency_speed")
            self.pmw_show_message(message)
            if ok:
                self.pmw_begin_test_progress("latency", len(nodes), message)
        elif pms_operation == "speed":
            ok, message = self.pmw_speed.pst_start(nodes)
            self.pmw_show_message(message)
            if ok:
                self.pmw_begin_test_progress("speed", len(nodes), message)
                self.pmw_speed_dialog_wanted = True   # v1.9.22：用户从批量操作发起的，测完弹汇总
        else:
            self.pmw_logger.warning("未知的批量操作：%s", pms_operation)
