#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试提示设置部件（v1.9.21 建立 / v1.9.22 扩展）

为什么单独成文件：`settingsdialog.py` 顶在 1000 行红线（项目宪法第 6 条），
测试相关的提示类开关一律放独立部件。

两个开关（都属于「测试完要怎么告诉我」）：
    · `test.fail_show_core`   —— 失败的节点延迟列显示核心原话（HTTP 503 / 504…）还是统一「超时」
    · `test.speed_result_dialog` —— 用户手动发起的测速跑完后弹一个汇总框

职责边界：本部件只做「显示 + 改设置」（改即保存）。
· 失败显示口径由主窗口 `pmw_apply_fail_text_mode()` 灌进节点表模型
  （`PmsNodeTableModel.pntm_show_core_fail`），失败原话存在 `pms_nodes.last_test_note`；
· 测速汇总框由主窗口 `pmw_on_speed_finished()` → `pmw_notify_test_finished()` 弹。
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QVBoxLayout,
    QWidget,
)

from pms.gui.widgets import pms_make_hint_label

if TYPE_CHECKING:  # 仅类型检查用，避免与运行时模块循环导入
    from pms.settings import PmsSettingsManager


class PmsTestNoticeWidget(QWidget):
    """「测试提示」分组：失败怎么显示 + 测速完成要不要弹汇总框。"""

    def __init__(self, pms_settings: "PmsSettingsManager", pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.ptnw_settings = pms_settings
        self.ptnw_build_ui()

    # ------------------------------------------------------------------ 界面
    def ptnw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        group = QGroupBox("测试提示")
        inner = QVBoxLayout(group)
        self.ptnw_chk_core = QCheckBox("延迟列显示核心的原始结果（HTTP 503 / HTTP 504 …），而不是统一写「超时」")
        self.ptnw_chk_core.setToolTip(
            "探测失败时，核心会给出具体原因：\n"
            "· HTTP 503（An error occurred in the delay test）= 这个节点自己没做成这次请求（多半已失效 / 协议不被支持）\n"
            "· HTTP 504（Timeout）= 节点在「URL 测试超时」内没返回，太慢或不通\n"
            "· 连接失败（…）= 本地到临时核心这一跳就没通\n"
            "两者都不是程序出错。勾上后列表里直接显示核心给的那个码，方便判断是节点的问题还是地址的问题。"
        )
        self.ptnw_chk_core.setChecked(self.ptnw_settings.psm_get_bool("test.fail_show_core", False))
        self.ptnw_chk_core.toggled.connect(
            lambda value: self.ptnw_settings.psm_set("test.fail_show_core", bool(value))
        )
        self.ptnw_chk_speed_dialog = QCheckBox("测速结束后弹窗汇总结果（成功 / 失败个数与最快速度）")
        self.ptnw_chk_speed_dialog.setToolTip(
            "只有**你自己手点**发起的测速才会弹；通过本地接口（POST /test/speed）发起的不会弹。\n"
            "不管勾不勾，测速跑完都会有状态栏文字与托盘通知。"
        )
        self.ptnw_chk_speed_dialog.setChecked(self.ptnw_settings.psm_get_bool("test.speed_result_dialog", True))
        self.ptnw_chk_speed_dialog.toggled.connect(
            lambda value: self.ptnw_settings.psm_set("test.speed_result_dialog", bool(value))
        )
        inner.addWidget(self.ptnw_chk_core)
        inner.addWidget(self.ptnw_chk_speed_dialog)
        layout.addWidget(group)
        layout.addWidget(pms_make_hint_label(
            "失败原因（完整原话）始终保留在鼠标悬停提示与「最近 20 次延迟测试记录」的「说明」列里，"
            "与上面这个开关无关。测延迟完成同样有状态栏与托盘通知（不弹框，避免打断连续测试）。"
        ))
