#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凭据与环境变量

.env 读取（系统环境变量优先）与节点凭据加密（PBKDF2 + HMAC-SHA256 密钥流 + HMAC 标签）。
"""

from __future__ import annotations

from pathlib import Path
import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import secrets

from pms.optional import (
    PMS_HAS_DOTENV,
    pms_optional_dotenv_values,
)
from pms.constants import PMS_LOGGER_NAME
from pms.utils import (
    pms_atomic_write_text,
    pms_mask_secret,
    pms_read_text_smart,
)

class PmsEnvStore:
    """读取 .env（API Key / 数据库密码只在这里）：系统环境变量 > .env > 默认值。"""

    def __init__(self, pms_env_file: Path) -> None:
        self.pes_logger = logging.getLogger(PMS_LOGGER_NAME + ".env")
        self.pes_path = Path(pms_env_file)
        self.pes_values: dict[str, str] = {}
        self.pes_loaded = False
        self.pes_load()

    def pes_load(self) -> bool:
        """加载 .env；python-dotenv 缺失时用内置解析（同样支持引号与 # 注释）。"""
        self.pes_values = {}
        existed = self.pes_path.is_file()
        if existed:
            if PMS_HAS_DOTENV and pms_optional_dotenv_values is not None:
                try:
                    raw = pms_optional_dotenv_values(str(self.pes_path), encoding="utf-8")
                    for key, value in dict(raw).items():
                        if key:
                            self.pes_values[str(key).strip()] = "" if value is None else str(value)
                except Exception as exc:  # noqa: BLE001 - 兼容各种 dotenv 异常
                    self.pes_logger.error(".env 解析失败（python-dotenv），改用内置解析：%s", exc)
                    self.pes_values = self.pes_parse_manually(self.pes_path)
            else:
                self.pes_values = self.pes_parse_manually(self.pes_path)
            self.pes_logger.info("已加载 .env：%s（%d 项）", self.pes_path, len(self.pes_values))
        else:
            self.pes_logger.info(".env 不存在（%s），全部使用环境变量或默认值", self.pes_path)
        self.pes_loaded = True
        return existed

    @staticmethod
    def pes_parse_manually(pms_path: Path) -> dict[str, str]:
        """内置 .env 解析：支持 KEY=VALUE、单双引号、行内 # 注释、export 前缀。"""
        values: dict[str, str] = {}
        for line in pms_read_text_smart(pms_path).splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if text.lower().startswith("export "):
                text = text[7:].strip()
            if "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if key:
                values[key] = value
        return values

    def pes_get(self, pms_key: str, pms_default: str = "") -> str:
        """系统环境变量优先，其次 .env，最后默认值。"""
        env_value = os.environ.get(pms_key)
        if env_value not in (None, ""):
            return str(env_value)
        value = self.pes_values.get(pms_key)
        if value not in (None, ""):
            return str(value)
        return pms_default

    def pes_set(self, pms_key: str, pms_value: str) -> bool:
        """写入 .env（文件不存在则创建），并同步内存值；失败返回 False。

        只用于程序自己生成的凭据（如 API token）：**用户的 API Key / 数据库密码不由本方法写入**。
        注意：读取时系统环境变量优先于 .env，若同名变量已在系统环境变量里，改 .env 不会生效。
        """
        key = str(pms_key).strip()
        if not key:
            return False
        try:
            lines: list[str] = []
            if self.pes_path.is_file():
                lines = pms_read_text_smart(self.pes_path).splitlines()
            wanted = f"{key}="
            replaced = False
            for index, line in enumerate(lines):
                text = line.strip()
                if not text or text.startswith("#") or "=" not in text:
                    continue
                if text.lower().startswith("export "):
                    text = text[7:].strip()
                if text.split("=", 1)[0].strip() == key:
                    lines[index] = wanted + str(pms_value)
                    replaced = True
                    break
            if not replaced:
                if lines and not lines[-1].strip():
                    lines.append(wanted + str(pms_value))
                else:
                    lines.extend(["", wanted + str(pms_value)])
            pms_atomic_write_text(self.pes_path, "\n".join(lines).rstrip() + "\n")
            self.pes_values[key] = str(pms_value)
            self.pes_logger.info("已写入 .env：%s（值已脱敏保存）", key)
            return True
        except OSError as exc:
            self.pes_logger.error("写入 .env 失败（%s）：%s", key, exc)
            return False

    def pes_has(self, pms_key: str) -> bool:
        return self.pes_get(pms_key, "") != ""

    def pes_redacted_snapshot(self) -> dict[str, str]:
        """脱敏快照（界面展示 .env 状态用，绝不显示明文）。"""
        result: dict[str, str] = {}
        for key, value in sorted(self.pes_values.items()):
            if not value:
                result[key] = "（空）"
            elif any(token in key.upper() for token in ("KEY", "TOKEN", "PASSWORD", "SECRET", "PASS")):
                result[key] = pms_mask_secret(value, 4)
            else:
                result[key] = value
        return result

class PmsSecretVault:
    """本地落盘加密（可选）：用 .env 里的数据库密码派生密钥，保护节点口令字段。

    算法：PBKDF2-HMAC-SHA256 派生密钥 → 以 HMAC-SHA256 为 PRF 的计数器密钥流（KCTR）
    加密 → HMAC-SHA256 认证标签（先加密后认证）。未配置密码时不做加密（与 v2rayN 一致）。
    """

    psv_MAGIC = "pmsv1:"
    psv_ITERATIONS = 60000
    psv_SALT = b"PmsSecretVault::v1::salt"

    def __init__(self, pms_password: str) -> None:
        self.psv_logger = logging.getLogger(PMS_LOGGER_NAME + ".vault")
        self.psv_password = str(pms_password or "")
        self.psv_enabled = bool(self.psv_password)
        self.psv_key: bytes = b""
        if self.psv_enabled:
            self.psv_key = hashlib.pbkdf2_hmac("sha256", self.psv_password.encode("utf-8"), self.psv_SALT, self.psv_ITERATIONS, dklen=32)

    def psv_derive_stream(self, pms_nonce: bytes, pms_length: int) -> bytes:
        """用 HMAC-SHA256 计数器模式生成密钥流。"""
        output = bytearray()
        counter = 0
        while len(output) < pms_length:
            block = hmac.new(self.psv_key, pms_nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            output.extend(block)
            counter += 1
        return bytes(output[:pms_length])

    def psv_encrypt(self, pms_plain: str) -> str:
        """加密明文；未启用密码或明文为空时原样返回。"""
        if not self.psv_enabled or not pms_plain:
            return pms_plain
        if str(pms_plain).startswith(self.psv_MAGIC):
            return pms_plain
        raw = str(pms_plain).encode("utf-8")
        nonce = secrets.token_bytes(16)
        stream = self.psv_derive_stream(nonce, len(raw))
        cipher = bytes(a ^ b for a, b in zip(raw, stream))
        tag = hmac.new(self.psv_key, nonce + cipher, hashlib.sha256).digest()[:16]
        return self.psv_MAGIC + base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")

    def psv_decrypt(self, pms_cipher_text: str) -> str:
        """解密；不是密文 / 校验失败时返回原文本（保证旧数据仍可读）。"""
        text = str(pms_cipher_text or "")
        if not text.startswith(self.psv_MAGIC):
            return text
        if not self.psv_enabled:
            return ""
        payload = text[len(self.psv_MAGIC):]
        try:
            blob = base64.urlsafe_b64decode(payload + "=" * ((-len(payload)) % 4))
        except (binascii.Error, ValueError):
            return ""
        if len(blob) < 32:
            return ""
        nonce, tag, cipher = blob[:16], blob[16:32], blob[32:]
        expect = hmac.new(self.psv_key, nonce + cipher, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expect):
            self.psv_logger.warning("密文校验失败（数据库密码可能已变更）")
            return ""
        stream = self.psv_derive_stream(nonce, len(cipher))
        return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8", errors="replace")

    def psv_is_encrypted(self, pms_text: str) -> bool:
        return str(pms_text or "").startswith(self.psv_MAGIC)
