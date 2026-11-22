#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心代号 → 显示名 / 版本比较（v1.9.45 从 `pms/standalone.py` 搬出）。

为什么单独成模块：`pms/standalone.py`（独立内核适配框架）已随「只保留 Xray + sing-box」
整块下线（用户 2026-10-12 拍板），但这个文件里的两件事**主核心还要用**：

* `pms_core_title()` —— 「启动核心」按钮、状态栏、更新汇总、下载提示都要显示核心名；
* `pms_version_tuple()` / `pms_version_newer()` —— 「设置 → 内核版本一览」判断「线上版本是否更新」；
* `pms_version_base()` / `pms_version_effective_local()` —— 同一件事的**口径修正**，
  见下面各自的 docstring（v1.9.55 补：`standalone.py` 下线时这两个被漏掉了，
  而 `coreversionwidget.py` 还在调 `_pms_version_effective_local()`，一进「内核版本一览」
  就 NameError，界面直接卡死）。

所以把它们单独放一个无可执行依赖的小模块（只 import 常量），
`pms/coresys.py`（核心定位）与下载/界面各取所需。
"""

from __future__ import annotations

import re

from pms.constants import PMS_CORE_KIND_TITLES


def pms_core_title(pms_kind: str) -> str:
    """核心代号 → 显示名（未知代号原样返回）。"""
    target = str(pms_kind or "").strip()
    return PMS_CORE_KIND_TITLES.get(target) or target


def pms_version_tuple(pms_text: str) -> tuple[int, ...]:
    """把版本文本转成可比较的整数元组，解析不出来返回空元组。

    只取文本里的数字序列，所以 `v1.19.32` / `app/v2.13.0` / `Xray-windows-64-v26.3.27`
    都能归一成同一套比较口径。
    """
    return tuple(int(item) for item in re.findall(r"\d+", str(pms_text or "")))


def pms_version_newer(pms_remote: str, pms_local: str) -> bool:
    """远端版本是否比本地新；任一侧解析不出来就返回 False（宁可不更新，不误更新）。

    长度不同时按较短长度补齐后再比，避免 `1.2` 与 `1.2.0` 判成不同。
    """
    remote = pms_version_tuple(pms_remote)
    local = pms_version_tuple(pms_local)
    if not remote or not local:
        return False
    width = max(len(remote), len(local))
    remote += (0,) * (width - len(remote))
    local += (0,) * (width - len(local))
    return remote > local


def pms_version_base(pms_text: str) -> tuple[int, ...]:
    """**主版本**（剥掉末尾 `-<数字>` 打包号）的可比较元组（v1.9.31 起）。

    为什么需要它：上游 tag 常带打包号，例如 NaiveProxy 的 `v154.0.8037.49-4`
    ——`-4` 是同一份构建的第 4 次打包，二进制自报只会说 `154.0.8037.49`。
    不剥掉就没法判断「两份文本说的是不是同一版」，于是每次都误判成「有更新」。
    """
    return pms_version_tuple(re.sub(r"-\d+$", "", str(pms_text or "").strip()))


def pms_version_effective_local(pms_probed: str, pms_recorded: str) -> str:
    """比对用的本地版本：记录值的主版本与探测值一致时，用**记录值**比（v1.9.31）。

    为什么需要它（实测，用户真机报的「每次都在更新，每次好像都没更新一样」）：
    NaiveProxy 二进制自报 `154.0.8037.49`，远端 tag 是 `v154.0.8037.49-4`。
    只拿自报值比 → 远端永远「更新」→ 每次检查都重复下载同一份文件。
    记录值（下载成功时写进 `core.standalone_versions`）带着 `-4`，主版本又和自报值一致，
    说明记录的就是当前这份文件，用它能正确判断「要等到 `-5` 才算新」。
    探测值与记录值主版本不一致（用户手动换过 exe）→ 以探测值为准。

    ★ v1.9.55：原名 `_pms_version_effective_local()`（私有，在 `pms/standalone.py` 里）。
    `standalone.py` 随「只保留 Xray + sing-box」整块下线时，这个函数被漏掉了，
    而 `pms/gui/dialogs/coreversionwidget.py` 还在调它——一进「内核版本一览」就 NameError。
    这里去掉前导下划线重新导出：它现在是跨模块共用件，私有名只会再骗一次人。
    """
    probed = str(pms_probed or "").strip()
    recorded = str(pms_recorded or "").strip()
    if not recorded:
        return probed
    if not probed:
        return recorded
    return recorded if pms_version_base(recorded) == pms_version_tuple(probed) else probed
