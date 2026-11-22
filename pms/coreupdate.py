#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心最新版本查询（v1.9.45 精简：独立内核已整块下线）

做什么
------
查一个核心在 GitHub 上的最新 tag（**只查，不下载任何文件**）。
「设置 → 内核版本一览」用它给 Xray / sing-box 填「线上版本」与「是否有更新」。

历史
----
v1.5.2～v1.9.44 这里还有一个 `PmsCoreAutoUpdateService`（独立内核定时自动更新：
Mihomo / Hysteria2 / TUIC / Juicity / AnyTLS / NaiveProxy）。用户 2026-10-12 拍板
「只保留 Xray + sing-box」，那 6 个内核的目录与代码一并下线，服务类随之删除。

硬规则
------
· **一次只查一个**：单任务线程池串行，不并发打 GitHub；
· **退出瞬间不炸**：信号对象可能已销毁，`emit` 的 RuntimeError 一律咽下去（v1.9.34 修）。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
import json
import logging
import urllib.error
import urllib.request

from pms.constants import (
    PMS_DEFAULT_SUB_USER_AGENT,
    PMS_LOGGER_NAME,
    PMS_TELEGRAM_PROXY_AUTO,
)
from pms.download import (
    pms_core_api_url,
    pms_core_select_release,
)
from pms.telegram import pms_telegram_build_openers

# --------------------------------------------------------------------------------------
# 后台任务：只查最新版本，不下载
# --------------------------------------------------------------------------------------
class PmsCoreUpdateCheckSignals(QObject):
    """检查任务 → 主线程。"""

    pcus_checked = Signal(str, bool, str, str)   # 核心代号, 成功, 最新版本, 说明


class PmsCoreUpdateCheckTask(QRunnable):
    """查询某个核心的最新版本（只查 tag，不下载任何文件）。"""

    def __init__(
        self,
        pms_signals: PmsCoreUpdateCheckSignals,
        pms_kind: str,
        pms_local_proxy_port: int = 0,
        pms_external_proxy: str = "",
    ) -> None:
        super().__init__()
        self.pcut_signals = pms_signals
        self.pcut_kind = str(pms_kind)
        self.pcut_logger = logging.getLogger(PMS_LOGGER_NAME + ".coreupdate")
        # 与下载、电报抓同一套退让链路：本程序代理 → 外部代理 → 直连
        self.pcut_openers = pms_telegram_build_openers(
            PMS_TELEGRAM_PROXY_AUTO, int(pms_local_proxy_port or 0), str(pms_external_proxy or "").strip(),
        )
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102
        request = urllib.request.Request(
            pms_core_api_url(self.pcut_kind),
            headers={"User-Agent": PMS_DEFAULT_SUB_USER_AGENT, "Accept": "application/vnd.github+json"},
        )
        errors: list[str] = []
        for name, opener in self.pcut_openers:
            try:
                with opener.open(request, timeout=25) as response:
                    payload = json.loads(response.read().decode("utf-8", errors="replace"))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                errors.append(f"[{name}] {exc}")
                continue
            release = pms_core_select_release(self.pcut_kind, payload)
            version = str(release.get("tag_name") or "") if release else ""
            if version:
                self.pcut_emit(True, version, "")
            else:
                self.pcut_emit(False, "", "未找到可用的发行版本")
            return
        self.pcut_emit(False, "", "查询失败：" + ("；".join(errors) if errors else "无可用网络通路"))

    def pcut_emit(self, pms_ok: bool, pms_version: str, pms_message: str) -> None:
        """回主线程发结果；**信号对象已经销毁（程序正在退出）时静默丢弃**。

        v1.9.34 修：退出瞬间正好有一轮检查在跑时，服务（连同它的信号对象）可能已经被销毁，
        这时 `emit` 会抛 `RuntimeError: Signal source has been deleted`，
        Qt 随即打印一段「Error calling Python override of QRunnable::run()」。
        程序其实能正常退出，但控制台那段红字会让人以为出了大事——这里一律咽下去。
        """
        try:
            self.pcut_signals.pcus_checked.emit(self.pcut_kind, pms_ok, pms_version, pms_message)
        except RuntimeError:
            self.pcut_logger.debug("版本检查结果无人接收（程序正在退出），已丢弃：%s", self.pcut_kind)


# --------------------------------------------------------------------------------------
# 服务：定时检查 + 排队下载
# --------------------------------------------------------------------------------------
