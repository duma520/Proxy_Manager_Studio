#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub 仓库订阅的设置部件（v1.9.18）

为什么单独成文件：`settingsdialog.py` 顶在 1000 行红线上（项目宪法第 6 条），
按 v1.6.5 立下的规矩，新增设置块一律放独立部件，主对话框只留 import + 1 行挂载。

四个控件对应四个设置项（都受归一化保护）：
    `subscription.github_max_nodes`      单次最多收多少节点（默认 500）
    `subscription.github_max_files`      单次最多下载几个文件（默认 20）
    `subscription.github_max_total_mb`   单次下载总字节预算（默认 24 MB）
    `subscription.github_follow_links`   文件里放的是订阅地址时，顺着一层继续抓（默认开）
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pms.gui.widgets import pms_make_hint_label

if TYPE_CHECKING:  # 仅类型检查用，避免与运行时模块循环导入
    from pms.settings import PmsSettingsManager


class PmsGithubSettingsWidget(QWidget):
    """「GitHub 仓库订阅」设置分组（改动即保存，与设置中心其它项同规矩）。"""

    def __init__(self, pms_settings: "PmsSettingsManager", pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.pgsw_settings = pms_settings
        self.pgsw_build_ui()

    def pgsw_build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        group = QGroupBox("GitHub 仓库订阅")
        form = QFormLayout(group)
        self.pgsw_spin_nodes = QSpinBox()
        self.pgsw_spin_nodes.setRange(50, 200000)
        self.pgsw_spin_nodes.setSingleStep(50)
        self.pgsw_spin_nodes.setValue(self.pgsw_settings.psm_get_int("subscription.github_max_nodes", 500))
        self.pgsw_spin_nodes.valueChanged.connect(
            lambda value: self.pgsw_settings.psm_set("subscription.github_max_nodes", int(value))
        )
        self.pgsw_spin_files = QSpinBox()
        self.pgsw_spin_files.setRange(1, 200)
        self.pgsw_spin_files.setSuffix(" 个")
        self.pgsw_spin_files.setValue(self.pgsw_settings.psm_get_int("subscription.github_max_files", 20))
        self.pgsw_spin_files.valueChanged.connect(
            lambda value: self.pgsw_settings.psm_set("subscription.github_max_files", int(value))
        )
        self.pgsw_spin_mb = QSpinBox()
        self.pgsw_spin_mb.setRange(1, 512)
        self.pgsw_spin_mb.setSuffix(" MB")
        self.pgsw_spin_mb.setValue(self.pgsw_settings.psm_get_int("subscription.github_max_total_mb", 24))
        self.pgsw_spin_mb.valueChanged.connect(
            lambda value: self.pgsw_settings.psm_set("subscription.github_max_total_mb", int(value))
        )
        self.pgsw_chk_follow = QCheckBox("文件内容是其它订阅地址时，顺着一层继续抓（默认开）")
        self.pgsw_chk_follow.setChecked(self.pgsw_settings.psm_get_bool("subscription.github_follow_links", True))
        self.pgsw_chk_follow.toggled.connect(
            lambda value: self.pgsw_settings.psm_set("subscription.github_follow_links", bool(value))
        )
        form.addRow("单次最多节点数", self.pgsw_spin_nodes)
        form.addRow("单次最多文件数", self.pgsw_spin_files)
        form.addRow("单次下载总预算", self.pgsw_spin_mb)
        form.addRow("", self.pgsw_chk_follow)
        self.pgsw_hint = pms_make_hint_label(
            "订阅地址直接填仓库地址即可（https://github.com/owner/repo 或 owner/repo）："
            "程序用 GitHub API 动态发现仓库里的节点文件（分片 / 时间戳命名 / base64 / JSON / YAML 都认），"
            "合并去重后导入；只认新提交——commit SHA 没变就一个文件都不下载，"
            "所以默认 60 次/小时的匿名配额够用。仓库很大时可调小「最多文件数 / 总预算」。"
            "在订阅记录的 API Key 里填 GitHub token（或 .env 里放 PMS_GITHUB_TOKEN）可提到 5000 次/小时。"
        )
        layout.addWidget(group)
        layout.addWidget(self.pgsw_hint)
