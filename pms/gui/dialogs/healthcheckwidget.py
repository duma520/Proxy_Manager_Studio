#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""节点健康检查设置部件（v1.9.37）

为什么单独成文件：`settingsdialog.py` 已顶在 1000 行红线（宪法第 6 条），
按 v1.6.5 立下的规矩，新增设置一律走独立部件。

本部件只做「显示 + 改设置 + 发动作」：
    · 开关 / 周期 / 阈值 / 每批数量 / 抖动 / 通知 → 直接写设置（改动即保存）；
    · 三个按钮发 `phw_action`，由主窗口驱动服务（立即运行一次 / 只查自动禁用的 / 暂停或继续）；
    · 状态行由主窗口注入的 provider 提供（部件本身不认识数据库，与 autoswitchwidget 同一路数）。
"""

from __future__ import annotations

from typing import Callable, Optional, TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pms.gui.widgets import pms_make_hint_label
from pms.healthcheck import (
    PMS_HEALTH_MAX_BATCH,
    PMS_HEALTH_MAX_JITTER_MINUTES,
    PMS_HEALTH_MAX_PERIOD_DAYS,
    PMS_HEALTH_MAX_THRESHOLD,
    PMS_HEALTH_MIN_BATCH,
    PMS_HEALTH_MIN_PERIOD_DAYS,
    PMS_HEALTH_MIN_THRESHOLD,
)

if TYPE_CHECKING:  # 仅类型检查用，避免与运行时模块循环导入
    from pms.settings import PmsSettingsManager

PMS_HEALTH_HINT = (
    "· 周期体检：每周期把所有**非手动禁用**的节点扫一遍（启用中的 + 被本功能自动禁用的），"
    "分批探测、每拍最多一批，绝不集中发请求；\n"
    "· 连续失败到阈值 → **先做一次真连接确认**，确认也失败才自动禁用（不删节点，只是不再参与候选与自动切换）；\n"
    "· 被自动禁用的节点下个周期重新探测，**真连接可用就自动复活**并清零连续失败计数；\n"
    "· 你手动禁用的节点**永远不检查**，也不会被自动复活（手动启用时清零计数，恢复正常参与）；\n"
    "· TCP 握手只当初筛，禁用与复活都以「经核心真实请求测试地址成功」为准；\n"
    "· 复用延迟测试的批量通道与分片能力，并发只有手动测试的 1/4（2~8），"
    "手动测延迟 / 测速一开始，体检立刻让路；\n"
    "· 默认开启，但**首轮只建周期**：第一次启用不会立刻探测，一个周期之后才开始第一轮。"
)


class PmsHealthCheckWidget(QWidget):
    """「设置 → 测试 → 节点健康检查」二级标签。"""

    phw_action = Signal(str, dict)   # 动作名, 参数（由主窗口执行）

    def __init__(self, pms_settings: "PmsSettingsManager", pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.phw_settings = pms_settings
        self.phw_status_provider: Optional[Callable[[], str]] = None
        self.phw_build_ui()
        self.phw_refresh_state()

    # ------------------------------------------------------------------ 界面
    def phw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("节点健康检查（周期体检：自动禁用失效节点 / 自动复活已恢复节点）")
        form = QFormLayout(group)
        self.phw_chk_enabled = self.phw_bind_bool("health.enabled", QCheckBox("启用节点健康检查（默认开启）"))
        self.phw_spin_days = self.phw_bind_int("health.period_days", QSpinBox(),
                                               PMS_HEALTH_MIN_PERIOD_DAYS, PMS_HEALTH_MAX_PERIOD_DAYS, " 天")
        self.phw_spin_fail = self.phw_bind_int("health.fail_threshold", QSpinBox(),
                                               PMS_HEALTH_MIN_THRESHOLD, PMS_HEALTH_MAX_THRESHOLD, " 次")
        self.phw_spin_batch = self.phw_bind_int("health.batch_size", QSpinBox(),
                                                PMS_HEALTH_MIN_BATCH, PMS_HEALTH_MAX_BATCH, " 个 / 批")
        self.phw_spin_jitter = self.phw_bind_int("health.jitter_minutes", QSpinBox(),
                                                 0, PMS_HEALTH_MAX_JITTER_MINUTES, " 分钟")
        self.phw_chk_notify = self.phw_bind_bool("health.notify", QCheckBox("自动禁用 / 自动复活时弹一次托盘气泡（绝不逐节点弹窗）"))
        form.addRow(self.phw_chk_enabled)
        form.addRow("检查周期", self.phw_spin_days)
        form.addRow("连续失败几次才禁用", self.phw_spin_fail)
        form.addRow("每批探测节点数", self.phw_spin_batch)
        form.addRow("周期时间抖动 ±", self.phw_spin_jitter)
        form.addRow(self.phw_chk_notify)
        layout.addWidget(group)

        buttons = QHBoxLayout()
        self.phw_btn_run = QPushButton("立即运行一次")
        self.phw_btn_run.clicked.connect(lambda: self.phw_action.emit("health_run_now", {}))
        self.phw_btn_auto = QPushButton("只检查自动禁用的节点")
        self.phw_btn_auto.clicked.connect(lambda: self.phw_action.emit("health_run_auto_only", {}))
        self.phw_btn_pause = QPushButton("暂停健康检查")
        self.phw_btn_pause.setCheckable(True)
        self.phw_btn_pause.toggled.connect(
            lambda pms_checked: self.phw_action.emit("health_pause", {"paused": bool(pms_checked)}))
        for button in (self.phw_btn_run, self.phw_btn_auto, self.phw_btn_pause):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.phw_label_status = QLabel("运行状态：待命")
        self.phw_label_status.setWordWrap(True)
        layout.addWidget(self.phw_label_status)
        layout.addWidget(pms_make_hint_label(PMS_HEALTH_HINT))

    # ------------------------------------------------------------------ 绑定
    def phw_bind_bool(self, pms_key: str, pms_widget: QCheckBox) -> QCheckBox:
        """复选框 ↔ 布尔设置（双向，改动即保存）。"""
        pms_widget.setChecked(self.phw_settings.psm_get_bool(pms_key, False))
        pms_widget.toggled.connect(lambda value, key=pms_key: self.phw_settings.psm_set(key, bool(value)))
        return pms_widget

    def phw_bind_int(self, pms_key: str, pms_widget: QSpinBox, pms_min: int, pms_max: int, pms_suffix: str) -> QSpinBox:
        """数字框 ↔ 整数设置（双向，改动即保存；越界由设置层再夹一次）。"""
        pms_widget.setRange(int(pms_min), int(pms_max))
        pms_widget.setSuffix(str(pms_suffix))
        pms_widget.setValue(self.phw_settings.psm_get_int(pms_key, int(pms_widget.value())))
        pms_widget.valueChanged.connect(lambda value, key=pms_key: self.phw_settings.psm_set(key, int(value)))
        return pms_widget

    # ------------------------------------------------------------------ 状态
    def phw_set_status_provider(self, pms_provider: Optional[Callable[[], str]]) -> None:
        """主窗口注入「现在什么状态」的取数函数（部件不认识数据库）。"""
        self.phw_status_provider = pms_provider
        self.phw_refresh_state()

    def phw_refresh_state(self) -> None:
        """按当前设置刷新按钮文案与状态行。"""
        enabled = self.phw_settings.psm_get_bool("health.enabled", True)
        self.phw_btn_pause.blockSignals(True)
        self.phw_btn_pause.setChecked(not enabled)
        self.phw_btn_pause.setText("继续健康检查" if not enabled else "暂停健康检查")
        self.phw_btn_pause.blockSignals(False)
        self.phw_btn_run.setEnabled(enabled)
        self.phw_btn_auto.setEnabled(enabled)
        text = "运行状态：已暂停"
        if enabled and self.phw_status_provider is not None:
            try:
                text = f"运行状态：{self.phw_status_provider()}"
            except Exception:  # noqa: BLE001 - 取状态失败不该影响设置页
                text = "运行状态：待命"
        elif enabled:
            text = "运行状态：待命（首轮只建周期，一个周期后开始第一轮探测）"
        self.phw_label_status.setText(text)

    def showEvent(self, pms_event: object) -> None:   # noqa: N802 - Qt 标准虚函数名
        """设置页每次打开都刷一次状态（用户多半是来看「现在什么情况」的）。"""
        super().showEvent(pms_event)   # type: ignore[arg-type]
        self.phw_refresh_state()
