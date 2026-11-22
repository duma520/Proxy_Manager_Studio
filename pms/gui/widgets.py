#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面小部件

布局辅助（提示标签、紧凑单行布局）、工具栏「分组 / 搜索」按钮（v1.9.50）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

# --------------------------------------------------------------------------------------
# 节点表格分页（v1.7.0）
# --------------------------------------------------------------------------------------
# 每页可选条数。**不含 0（不分页）**——不分页只作为兜底值存在于设置里，
# 不在界面上暴露，避免用户不小心一次铺几千行把界面拖死。
PMS_TABLE_PAGE_SIZES = (20, 50, 100, 200, 500)
PMS_TABLE_PAGE_SIZE_DEFAULT = 50


# --------------------------------------------------------------------------------------
# 对话框：节点编辑
# --------------------------------------------------------------------------------------
def pms_make_hint_label(pms_text: str) -> QLabel:
    """统一样式的提示标签。"""
    label = QLabel(pms_text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


def pms_make_row(*pms_widgets: QWidget) -> QWidget:
    """把若干控件横向排成一行（减少空白，提高空间利用率）。"""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    for widget in pms_widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return container


# --------------------------------------------------------------------------------------
# 工具栏「分组 / 搜索」入口（v1.9.50，审计第 12 项）
# --------------------------------------------------------------------------------------
# 背景：`ui.show_left_sidebar`（v1.6.6）能把整条左侧栏关掉，关掉之后
# **分组与筛选就没入口了**——中间节点表还在，用户却没法换分组、也没法搜索。
#
# 做法（宪法：主窗口只留动作入口，控件放独立部件）：
#   侧栏那份「分组 + 筛选」统一由 `pmw_build_group_filter_widget()` 构造，
#   工具栏这里**临时接管同一份控件**（`addWidget()` 会改父对象），
#   所以两边状态天然同步——不存在「两套筛选」打架，也不需要任何同步代码。
class PmsToolbarFilterButton(QToolButton):
    """工具栏「分组 / 搜索 ▾」按钮：弹出的小面板里装的是侧栏那份**真控件**。

    接线分两步（**顺序不能反**：工具栏比侧栏先建好，`pmw_group_filter_box` 要等侧栏才存在）::

        # pmw_build_toolbar() 里：
        self.pmw_filter_button = PmsToolbarFilterButton(self.pmw_toolbar, self)
        self.pmw_toolbar.addWidget(self.pmw_filter_button)

        # pmw_build_sidebar() 末尾：
        self.pmw_filter_button.ptfb_set_panel(self.pmw_group_filter_box, panel)

    ⚠️ 按钮用 `InstantPopup`（点一下直接弹菜单）而不是 `MenuButtonPopup`——后者主按钮那一下
    不会弹菜单，探针与用户都得再多点一次箭头。`QMenu` 与 `QWidgetAction` 都**显式给父对象**
    （铁律 7.33：子菜单不显式给父对象会被 Python 回收，菜单只剩空壳）。
    """

    def __init__(self, pms_parent: QWidget, pms_window: QWidget | None = None) -> None:
        super().__init__(pms_parent)
        # ⚠️ 必须**显式**拿到主窗口：按钮的父对象是工具栏（`addWidget()` 给的），
        # 所以 `self.parent()` 只能拿到 QToolBar，`getattr(toolbar, "pmw_set_sidebar_visible")`
        # 永远是 None —— 侧栏就不会让位（实测踩过：按钮「点了没反应」）。
        self.ptfb_window = pms_window
        self.ptfb_panel: QWidget | None = None
        self.ptfb_home: QWidget | None = None
        self.ptfb_holder = QWidget()
        self.ptfb_holder.setMinimumWidth(250)
        self.ptfb_layout = QVBoxLayout(self.ptfb_holder)
        self.ptfb_layout.setContentsMargins(6, 6, 6, 6)
        self.ptfb_layout.setSpacing(4)
        self.ptfb_menu = QMenu(self)
        self.ptfb_action_return = self.ptfb_menu.addAction("在左侧栏显示这一块", self.ptfb_return_panel)
        self.ptfb_action_return.setEnabled(False)
        self.ptfb_menu.addSeparator()
        self.ptfb_widget_action = QWidgetAction(self.ptfb_menu)
        self.ptfb_widget_action.setDefaultWidget(self.ptfb_holder)
        self.ptfb_menu.addAction(self.ptfb_widget_action)
        self.setText("分组 / 搜索 ▾")
        self.setToolTip("选择分组、按关键字 / 协议 / 状态筛选（侧栏隐藏后照样能用；用的是同一份控件）")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setMenu(self.ptfb_menu)

    def ptfb_set_panel(self, pms_panel: QWidget, pms_home: QWidget | None = None,
                       pms_window: QWidget | None = None) -> None:
        """登记要借用的控件（`pms_panel`）与它原来的位置（`pms_home` = 侧栏容器）。

        `pms_window` 是主窗口；构造期给不出时（工具栏比窗口引用早）在这里补上。
        """
        self.ptfb_panel = pms_panel
        self.ptfb_home = pms_home
        if pms_window is not None:
            self.ptfb_window = pms_window
        self.ptfb_action_return.setEnabled(pms_home is not None)

    def ptfb_is_panel_borrowed(self) -> bool:
        """控件当前是不是被工具栏借走了（判据是**父对象**，不是 `isHidden()`）。

        为什么不能用 `isHidden()`：控件被 `addWidget()` 搬到 `holder` 之后，
        它自己的 hidden 标志还是「不隐藏」，而侧栏（现在是个空壳）也一样报「不隐藏」。
        父对象是唯一可靠的判据。
        """
        return self.ptfb_panel is not None and self.ptfb_panel.parent() is self.ptfb_holder

    def ptfb_prepare(self) -> None:
        """菜单弹出**之前**把真控件借进来。

        必须先 `close()` 菜单再借控件：菜单开着的时候调用方（Qt）还在用它，
        直接把 `holder` 里的控件搬走会让菜单卡在「下次点了什么都不显示」的状态。
        """
        if self.ptfb_menu.isVisible():
            self.ptfb_menu.close()
        if self.ptfb_panel is None:
            return
        self.ptfb_hide_sidebar_block()
        self.ptfb_layout.addWidget(self.ptfb_panel)
        self.ptfb_panel.show()

    def ptfb_hide_sidebar_block(self) -> None:
        """侧栏本来开着时，把整条侧栏收掉——同一份控件不能同时出现在两处。

        ⚠️ 顺序很重要：**先把控件搬走再收侧栏**。反过来（先 `pmw_set_sidebar_visible(False)`）
        会踩到 `pmw_set_sidebar_visible(True)` 的恢复分支——它按 `panel.isHidden()` 判断
        「要不要恢复宽度」，而此时面板因为被搬空了反而不报 hidden，恢复宽度那步就落空。

        侧栏本来就隐藏（`ui.show_left_sidebar` = False）时**什么都不做**：
        控件留在工具栏，入口与侧栏可见性无关（这正是第 12 项要的）。
        **故意用 `pmw_set_sidebar_visible(False)` 而不是 `hide()` 子控件**——
        那条路会把保留宽度一起做掉；直接 hide 子控件会让侧栏变成「空壳还占着宽度」。
        """
        if self.ptfb_is_panel_borrowed():
            return
        window = self.ptfb_window
        sidebar = getattr(window, "pmw_sidebar_panel", None)
        if sidebar is None or sidebar.isHidden():
            return
        setter = getattr(window, "pmw_set_sidebar_visible", None)
        if callable(setter):
            setter(False)

    def ptfb_return_panel(self) -> None:
        """菜单里的「在左侧栏显示这一块」：把控件还回侧栏并让侧栏重新出现。"""
        if self.ptfb_panel is None or self.ptfb_home is None:
            return
        layout = self.ptfb_home.layout()
        if layout is not None:
            layout.insertWidget(0, self.ptfb_panel, 1)
        self.ptfb_panel.show()
        window = self.ptfb_window
        setter = getattr(window, "pmw_set_sidebar_visible", None)
        if callable(setter):
            setter(True)
        settings = getattr(window, "pmw_settings", None)
        if settings is not None:
            settings.psm_set("ui.show_left_sidebar", True)

    def mousePressEvent(self, pms_event) -> None:  # noqa: N802 - Qt 只认这个标准虚函数名
        """`InstantPopup` 在鼠标按下时就弹菜单，准备动作必须抢在它前面。"""
        if pms_event.button() == Qt.MouseButton.LeftButton:
            self.ptfb_prepare()
        super().mousePressEvent(pms_event)
