#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置中心的「二级标签」与底部按钮（v1.9.17）

**为什么单独成文件**：`settingsdialog.py` 正顶在 1000 行红线（项目宪法第 6 条），
而本次要做的两件事都要动结构（把每页的分组框装进二级标签、重做底部按钮），
按 v1.6.5 立下的规矩放进独立模块，`settingsdialog.py` 里只留 import 与两行调用。

**二级标签怎么做**：设置中心最外层已有 16 个分类标签，但每页内部仍是
「分组框一条路排到底」——核心 / 路由分流 / 测速 / 版本与环境 这几页尤其长，
找一项要滚半天。这里**不改动任何一页的内容**，只把该页里已经画好的
分组框（`QGroupBox`）与独立设置部件（`PmsCoreVersionWidget` 等）按顺序
重新装进二级标签页：**一页里有 2 个以上「分组」才有二级标签**，
只有一组的页（本地入口 / 系统代理 / TUN / 数据库 …）保持原样，不硬套壳。
分组后面的说明标签（`pms_make_hint_label` 产生的 `QLabel`）跟着它所属的分组进同一个二级页。

**底部按钮**（用户要求）：原来那个「立即保存并关闭」是多余的（每次改动都已自动保存），
改成「恢复默认（本页）」+「恢复上次修改（时间戳）」+「关闭」：
- 恢复默认：只重置**当前页**对应的设置分区（确认框里会写明分区与项数），
  全部重置仍然在「高级」页里那个「恢复全部默认」按钮上。
- 恢复上次修改：回退到**上一条内容不同的快照**（时间戳直接写在按钮上）。
- 关闭设置：若内容有变化就补记一条快照（标签「关闭设置」），
  这样「恢复前 N 次」总有东西可回退；快照条数由 `advanced.settings_history_max` 控制。

恢复动作执行后**会关闭设置窗口**：设置值已经被批量改写，而各控件还是旧值，
留在界面上继续点就会把旧值又写回去（变更即保存的固有风险），关掉重开最稳。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pms.constants import PMS_SETTINGS_DEFAULTS

# 独立设置部件 → 二级标签名（这些部件本身就是「一个分组」，只是画在 QGroupBox 之外）
PST_SECTION_WIDGET_TITLES: dict[str, str] = {
    "PmsTorBridgeWidget": "Tor 桥接出口",
    "PmsCoreVersionWidget": "内核版本一览",
    "PmsCoreMatrixWidget": "协议能力矩阵",
    "PmsMaintenanceWidget": "日志与临时文件清理",
    "PmsStatsSettingsWidget": "统计来源与端口",
    "PmsApiSettingsWidget": "接口设置",
    "PmsAutoSwitchSettingsWidget": "自动切换",
    "PmsUiLayoutWidget": "面板可见性",
    "PmsTestSortWidget": "测试后自动排序",
    "PmsHealthCheckWidget": "节点健康检查",
}

# 顶层标签 → 该页覆盖的设置分区（「恢复默认（本页）」用；键的前缀就是分区名）
PST_TAB_SECTIONS: dict[str, tuple[str, ...]] = {
    "常规": ("general",),
    "核心": ("core",),
    "本地入口": ("inbound",),
    "系统代理": ("system_proxy",),
    "TUN 模式": ("tun",),
    "路由分流": ("routing",),
    "延迟测试": ("test", "health"),
    "测速": ("speed", "test"),
    "订阅": ("subscription", "telegram"),
    "界面": ("ui",),
    "数据库": ("database",),
    "统计": ("stats",),
    "高级": ("advanced",),
    "接口": ("api",),
    "自动切换": ("autoswitch",),
    "版本与环境": (),          # 只显示状态，没有可恢复的设置项
}


def pst_section_title(pms_widget: QWidget) -> str:
    """取一个「分组」的二级标签名（分组框标题去掉括号里的说明；未知部件返回空串）。"""
    if isinstance(pms_widget, QGroupBox):
        return str(pms_widget.title()).split("（")[0].split("(")[0].strip()
    if type(pms_widget).__name__ == "PmsTestSortWidget":
        # 同一个部件在延迟页 / 测速页各挂一份，靠它自己的 kind 区分
        return "测速后自动排序" if getattr(pms_widget, "ptsw_kind", "") == "speed" else "测试后自动排序"
    return PST_SECTION_WIDGET_TITLES.get(type(pms_widget).__name__, "")


def pst_make_scroll() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    """二级标签每一页都用独立滚动区（外层不再套滚动区，避免双层滚动条）。"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(8)
    area.setWidget(container)
    return area, container, layout


def pst_tab_sections(pms_page: QWidget) -> list[tuple[str, list[QWidget]]]:
    """扫一遍页面，按顺序分出「(二级标签名, [该组的控件…])」。"""
    if not isinstance(pms_page, QScrollArea) or pms_page.widget() is None:
        return []
    layout = pms_page.widget().layout()
    if layout is None:
        return []
    sections: list[tuple[str, list[QWidget]]] = []
    extras: list[QWidget] = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if widget is None:
            continue
        title = pst_section_title(widget)
        if title:
            sections.append((title, [widget]))
            extras = sections[-1][1]
        else:
            extras.append(widget)
    return sections


def pst_apply_subtabs(pms_tabs: QTabWidget) -> int:
    """把每个顶层页里的「分组」装进二级标签页；返回被改造的页数。

    少于 2 个分组的页保持原样（不给只有一项的页面硬套一层标签）。
    """
    converted = 0
    for index in range(pms_tabs.count()):
        page = pms_tabs.widget(index)
        title = pms_tabs.tabText(index)
        if len(pst_tab_sections(page)) < 2:
            continue
        # 确认要改造后才真正把控件从旧布局里取出来（取出来就得装回去，不留半改状态）
        old_layout = page.widget().layout()
        widgets: list[QWidget] = []
        while old_layout.count():
            widget = old_layout.takeAt(0).widget()
            if widget is not None:
                widgets.append(widget)
        new_page = QWidget()
        outer = QVBoxLayout(new_page)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.addWidget(pst_build_subtabs(widgets))
        pms_tabs.removeTab(index)
        pms_tabs.insertTab(index, new_page, title)
        page.deleteLater()
        converted += 1
    return converted


def pst_build_subtabs(pms_widgets: list[QWidget]) -> QTabWidget:
    """把「分组 + 它后面的说明标签」按顺序装进二级标签页。"""
    sub_tabs = QTabWidget()
    buckets: list[tuple[str, list[QWidget]]] = []
    for widget in pms_widgets:
        title = pst_section_title(widget)
        if title:
            buckets.append((title, [widget]))
            continue
        if not buckets:      # 分组之前的散件（正常没有）单独收一页，别把它们弄丢
            buckets.append(("其它", []))
        buckets[-1][1].append(widget)
    for title, widgets in buckets:
        area, _child, layout = pst_make_scroll()
        for widget in widgets:
            layout.addWidget(widget)
        layout.addStretch(1)
        sub_tabs.addTab(area, title)
    return sub_tabs


# --------------------------------------------------------------------------------------
# 底部按钮
# --------------------------------------------------------------------------------------
def pst_section_key_count(pms_sections: tuple[str, ...]) -> int:
    """某个分区里有多少个设置项（确认框里告诉用户会改多少项）。"""
    return sum(1 for key in PMS_SETTINGS_DEFAULTS if str(key).split(".")[0] in pms_sections)


def pst_build_footer(pms_dialog: QWidget, pms_settings: Any, pms_tabs: QTabWidget) -> QWidget:
    """底部：状态提示 + 恢复默认（本页）+ 恢复上次修改 + 关闭。

    `pms_dialog` 是设置对话框（用它的 accept() 关窗、以及 finished 信号兜底落盘）。
    """
    footer = QWidget()
    layout = QHBoxLayout(footer)
    layout.setContentsMargins(0, 0, 0, 0)
    status = QLabel(f"改动已自动保存（{getattr(pms_settings.psm_path, 'name', '')}）；关闭时若有变化会存一条快照，可随时回退")
    status.setObjectName("hintLabel")
    status.setWordWrap(True)
    layout.addWidget(status, 1)
    # 兼容旧探针 / 旧调用：这个提示标签仍挂在对话框上（v1.9.17 换成新底部后保留属性名）
    setattr(pms_dialog, "pset_status_label", status)

    def on_reset_page() -> None:
        title = pms_tabs.tabText(pms_tabs.currentIndex())
        sections = PST_TAB_SECTIONS.get(title, ())
        if not sections:
            QMessageBox.information(pms_dialog, "无需恢复", f"「{title}」页只显示状态，没有可恢复的设置项。")
            return
        count = pst_section_key_count(sections)
        names = "、".join(f"{name}.*" for name in sections)
        answer = QMessageBox.question(
            pms_dialog, "恢复本页默认",
            f"把「{title}」页的设置恢复为默认值吗？\n\n"
            f"影响范围：{names}（共 {count} 项）\n"
            f"其它页面的设置不受影响；当前设置会先存一条快照，随时可在「设置历史」里回退。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        total = 0
        for name in sections:
            total += int(pms_settings.psm_reset_section(name) or 0)
        pms_settings.psm_capture_snapshot("恢复本页默认")
        QMessageBox.information(pms_dialog, "已恢复", f"「{title}」页已恢复默认值（{total} 项）。设置窗口将关闭，重开后即为新值。")
        pms_dialog.accept()

    reset_button = QPushButton("恢复默认（本页）")
    reset_button.setObjectName("dangerButton")
    reset_button.setToolTip("只重置当前标签页对应的设置分区；全部重置在「高级」页里")
    reset_button.clicked.connect(on_reset_page)
    layout.addWidget(reset_button)

    previous = pms_settings.psm_previous_snapshot()
    undo_button = QPushButton("恢复上次修改")
    if previous:
        undo_button.setText(f"恢复上次修改（{str(previous.get('time', ''))[11:19] or '上一版'}）")
        undo_button.setToolTip(
            f"回退到 {previous.get('time', '')}（标签：{previous.get('label', '')}）的整套设置；"
            "当前状态会作为一条新快照保留，随时能再换回来"
        )
    else:
        undo_button.setEnabled(False)
        undo_button.setToolTip("还没有可回退的上一版本（改过设置并保存后就会出现）")

    def on_undo() -> None:
        snapshot = pms_settings.psm_previous_snapshot()
        if not snapshot:
            QMessageBox.information(pms_dialog, "没有可回退的版本", "还没有记录到上一版设置。")
            return
        answer = QMessageBox.question(
            pms_dialog, "恢复上次修改",
            f"把设置回退到 {snapshot.get('time', '')}（{snapshot.get('label', '')}）那一版吗？\n\n"
            "当前设置会先存一条快照，随时可以再换回来。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if pms_settings.psm_restore_snapshot(str(snapshot.get("id", ""))):
            pms_settings.psm_capture_snapshot("恢复上次修改")
            QMessageBox.information(pms_dialog, "已回退", f"设置已回退到 {snapshot.get('time', '')} 那一版。设置窗口将关闭，重开后即为新值。")
            pms_dialog.accept()
        else:
            QMessageBox.warning(pms_dialog, "回退失败", "快照不存在或已损坏。")

    undo_button.clicked.connect(on_undo)
    layout.addWidget(undo_button)

    def on_close() -> None:
        pms_settings.psm_flush("关闭设置")
        pms_dialog.accept()

    close_button = QPushButton("关闭")
    close_button.clicked.connect(on_close)
    layout.addWidget(close_button)
    # 点右上角 X / Esc 也要落盘并记快照（accept / reject 都会发 finished）
    pms_dialog.finished.connect(lambda _code: pms_settings.psm_flush("关闭设置"))
    return footer
