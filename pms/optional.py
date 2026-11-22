#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可选依赖探测

yaml / qrcode / python-dotenv / psutil 的探测与降级标志（缺失时程序自动回退）。
"""

from __future__ import annotations



# --------------------------------------------------------------------------------------
# 可选依赖：缺失时自动降级，绝不静默失败
# --------------------------------------------------------------------------------------
try:  # pragma: no cover - 环境相关
    import yaml as pms_optional_yaml  # type: ignore

    PMS_HAS_YAML = True
except Exception:  # noqa: BLE001
    pms_optional_yaml = None  # type: ignore
    PMS_HAS_YAML = False

try:  # pragma: no cover - 环境相关
    import qrcode as pms_optional_qrcode  # type: ignore

    PMS_HAS_QRCODE = True
except Exception:  # noqa: BLE001
    pms_optional_qrcode = None  # type: ignore
    PMS_HAS_QRCODE = False

try:  # pragma: no cover - 环境相关
    from dotenv import dotenv_values as pms_optional_dotenv_values  # type: ignore

    PMS_HAS_DOTENV = True
except Exception:  # noqa: BLE001
    pms_optional_dotenv_values = None  # type: ignore
    PMS_HAS_DOTENV = False

try:  # pragma: no cover - 环境相关
    import psutil as pms_optional_psutil  # type: ignore

    PMS_HAS_PSUTIL = True
except Exception:  # noqa: BLE001
    pms_optional_psutil = None  # type: ignore
    PMS_HAS_PSUTIL = False
