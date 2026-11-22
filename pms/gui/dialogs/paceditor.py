# -*- coding: utf-8 -*-
"""内置 PAC 脚本编辑器（v1.9.62）。

两种模式：
    - 专业模式：直接手写 / 编辑完整 FindProxyForURL 脚本（适合懂 JS 的用户）。
    - 小白模式：表单逐条加「匹配规则 + 动作 + 代理地址 + 默认动作」，点「生成代码」回填
      专业框；提供「常用模板」一键套用、以及「向导」分步问答生成，连完全不懂 PAC 的人都
      能照着大白话把复杂的分流场景配出来。

编辑器产物写独立文件 pms_custom.pac，与系统代理「自动 PAC」（pms_system_proxy.pac，由
coresys.pwsp_build_pac_text 自动生成、只支持直连域名 + 单代理）完全独立、互不覆盖。
「应用到系统代理」只把 AutoConfigURL 指向本文件并把 ProxyEnable 置 0，不重新生成内容。

设计取舍（务必让小白用户知道）：
    - 重新在「系统代理」里开关系统代理时，若 pac_source=auto 会改用自动 PAC；本编辑器的
      「应用到系统代理」会把 pac_source 切成 custom，从而让自定义脚本长期生效，直到用户在
      设置里把它切回 auto。退出程序时仍会按「退出还原」原样恢复用户原本的代理设置。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from pms.constants import (
    PMS_APP_NAME,
    PMS_APP_VERSION,
    PMS_CUSTOM_PAC_FILENAME,
    PMS_LOGGER_NAME,
    PMS_SYSTEM_PROXY_MODE_PAC,
)
from pms.coreprofile import PmsCoreLaunchProfile
from pms.coresys import PmsWindowsSystemProxy
from pms.gui.widgets import pms_make_hint_label
from pms.paths import PmsRuntimePaths
from pms.settings import PmsSettingsManager
from pms.utils import pms_atomic_write_text, pms_multiline_to_list


# --------------------------------------------------------------------------------------
# 小白模式的字段定义
# --------------------------------------------------------------------------------------
_PAC_MATCH_TYPES = ["域名后缀", "关键词", "IP 段", "全部流量"]          # 匹配「什么」的网站
_PAC_ACTIONS = ["直连 DIRECT", "走代理 PROXY", "走 SOCKS SOCKS"]         # 命中后「怎么走」
_PAC_DEFAULT_ACTIONS = ["默认走代理", "默认直连"]                        # 没命中任何规则时

# 常用模板（小白一键套用）：每条是 (名称, 默认动作, [(匹配类型, 内容, 动作, 代理地址), ...])
_PAC_TEMPLATES: list[tuple[str, str, list[tuple[str, str, str, str]]]] = [
    ("国内网站直连，其余走代理（最常用）", "默认走代理", [
        ("域名后缀", "cn", "直连 DIRECT", ""),
        ("关键词", "baidu", "直连 DIRECT", ""),
        ("域名后缀", "taobao.com", "直连 DIRECT", ""),
        ("域名后缀", "qq.com", "直连 DIRECT", ""),
    ]),
    ("只有指定网站走代理，其余直连", "默认直连", [
        ("域名后缀", "google.com", "走代理 PROXY", ""),
        ("域名后缀", "youtube.com", "走代理 PROXY", ""),
        ("域名后缀", "telegram.org", "走代理 PROXY", ""),
    ]),
    ("全部流量都走代理", "默认走代理", []),
    ("全部流量都直连（相当于不代理）", "默认直连", []),
]


def _pac_action_to_token(pms_action: str) -> str:
    """小白「动作」文字 → PAC 关键字（DIRECT / PROXY / SOCKS）。"""
    if pms_action.startswith("直连"):
        return "DIRECT"
    if pms_action.startswith("走代理"):
        return "PROXY"
    if pms_action.startswith("走 SOCKS"):
        return "SOCKS"
    return "DIRECT"


def _pac_compile_rules(
    pms_rules: list[dict[str, str]],
    pms_default_action: str,
    pms_proxy: str,
    pms_default_proxy: str = "",
) -> str:
    """把小白规则表编译成完整 PAC 脚本文本。

    v1.9.67：新增 `pms_default_proxy`（小白模式的「默认走代理的地址设置」）。
    留空时仍按老逻辑用 `pms_proxy`（本程序核心入口地址+端口）；填了就把它当作
    「没单独写代理地址的规则」的兜底代理——这样用户可以整体切到外部代理，
    而不必逐条规则去改地址。
    """
    # v1.9.67：整条规则没写代理地址时的兜底——用户指定的「默认走代理地址」优先，
    # 留空则回落到本程序入口（保持 v1.9.62 起的原行为不变）。
    fallback_proxy = (pms_default_proxy or "").strip() or pms_proxy.strip()
    # v1.9.67 修正：`PMS_PROXY` / `PMS_DEFAULT` 必须与规则兜底用**同一个地址**。
    # 否则用户把「默认走代理地址」改成外部代理时，规则走 7890、而 `PMS_DEFAULT`
    # （没命中规则时的返回值）仍指向本程序 30808，同一个脚本里两套代理地址、行为自相矛盾。
    proxy_token = "PROXY " + fallback_proxy if fallback_proxy else "DIRECT"
    default_token = "DIRECT" if pms_default_action.startswith("默认直连") else proxy_token
    # 域名后缀按「子域通吃」语义编译：.x.com 覆盖 x.com 自身；所以把用户可能写出的
    # *.x.com / .x.com / x.com 三种写法统一归一成 x.com（v1.9.62：实测发现
    # 原样拼进 dnsDomainIs 会生成 ".*.github.com" 这种永假条件，等于规则静默失效）。

    lines = [
        "// 由 代理节点管理控制台（Proxy Manager Studio）PAC 编辑器「小白模式」生成。",
        f"// v{PMS_APP_VERSION}：小白模式生成的规则；如需更精细的控制可在「专业模式」里改。",
        "// 本文件由 PAC 编辑器维护，可在「专业模式」自由编辑。",
        "",
        "var PMS_PROXY = " + '("' + proxy_token + '")' + ";",
        "var PMS_DEFAULT = " + '("' + default_token + '")' + ";",
        "",
        "function FindProxyForURL(url, host) {",
        "    host = (host || \"\").toLowerCase();",
        "    if (isPlainHostName(host) || host === \"localhost\" || host === \"127.0.0.1\" || host === \"::1\") {",
        "        return \"DIRECT\";",
        "    }",
    ]
    for rule in pms_rules:
        mtype = rule.get("match", "")
        content = (rule.get("content", "") or "").strip().lower()
        token = _pac_action_to_token(rule.get("action", "直连 DIRECT"))
        if token == "DIRECT":
            target = "DIRECT"
        else:
            addr = (rule.get("proxy", "") or "").strip()
            # v1.9.67：规则没填地址时，用「默认走代理地址」兜底（留空则用本程序入口），
            # 都没有才退化成 DIRECT——原来这一支在 pms_proxy 为空时会直接返回 DIRECT。
            use_addr = addr or fallback_proxy
            target = (token + " " + use_addr) if use_addr else "DIRECT"
        if mtype == "域名后缀" and content:
            domain = content[2:] if content.startswith("*.") else content.lstrip(".")
            if domain:
                lines.append(f'    if (dnsDomainIs(host, ".{domain}") || host === "{domain}") {{ return "{target}"; }}')
        elif mtype == "关键词" and content:
            lines.append(f'    if (shExpMatch(url, "*{content}*") || shExpMatch(host, "*{content}*")) {{ return "{target}"; }}')
        elif mtype == "IP 段" and content:
            # 小白填写形如 ***已脱敏***-内网IP/24；PAC 用 isInNet，需要拆出 ip 与掩码
            if "/" in content:
                ip, mask = content.split("/", 1)
                mask = _pac_cidr_to_mask(mask.strip())
            else:
                ip, mask = content, "255.255.255.255"
            lines.append(f'    if (isInNet(host, "{ip.strip()}", "{mask}")) {{ return "{target}"; }}')
        elif mtype == "全部流量":
            lines.append(f'    return "{target}";')
    lines.append('    return PMS_DEFAULT;')
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _pac_cidr_to_mask(pms_cidr: str) -> str:
    """把 /24 之类的位数转成 255.255.255.0 掩码。"""
    try:
        bits = int(pms_cidr)
    except ValueError:
        return pms_cidr
    if bits < 0 or bits > 32:
        return "255.255.255.255"
    mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
    return f"{(mask >> 24) & 255}.{(mask >> 16) & 255}.{(mask >> 8) & 255}.{mask & 255}"


def _pac_parse_js(pms_text: str) -> tuple[list[dict[str, str]], str, str]:
    """尽力把专业模式的 JS 反解析回小白规则表（best-effort）。

    返回 (规则列表, 默认动作, 代理地址)。解析不出来时返回 (空, 默认走代理, "")，
    调用方据此提示「请用专业模式」。
    """
    rules: list[dict[str, str]] = []
    proxy_addr = ""
    default_action = "默认走代理"

    # 默认代理地址：找 var PMS_PROXY = "PROXY 1.2.3.4:5678"
    m = re.search(r'PMS_PROXY\s*=\s*\("?(PROXY|SOCKS)\s*([^")]+)"?\)', pms_text)
    if m:
        proxy_addr = m.group(2).strip()
    # 默认动作
    m = re.search(r'PMS_DEFAULT\s*=\s*\("?(DIRECT|PROXY|SOCKS)[^")]*"?\)', pms_text)
    if m and m.group(1) == "DIRECT":
        default_action = "默认直连"

    for line in pms_text.splitlines():
        s = line.strip()
        # 域名后缀：dnsDomainIs(host, ".baidu.com") || host === "baidu.com"
        m = re.search(r'dnsDomainIs\(host,\s*"\.([^"]+)"\)', s)
        if m:
            target = _pac_pick_target(s)
            rules.append({"match": "域名后缀", "content": m.group(1), "action": target, "proxy": ""})
            continue
        # 通配域名（shExpMatch 写法）：*.github.com / shExpMatch(host, "*.github.com")
        m = re.search(r'shExpMatch\((?:url|host),\s*"\*\.([^"*]+)"\)', s)
        if m:
            target = _pac_pick_target(s)
            rules.append({"match": "域名后缀", "content": m.group(1), "action": target, "proxy": ""})
            continue
        # 关键词：shExpMatch(url, "*kw*")
        m = re.search(r'shExpMatch\((?:url|host),\s"\*([^"*]+)\*"\)', s)
        if m:
            target = _pac_pick_target(s)
            rules.append({"match": "关键词", "content": m.group(1), "action": target, "proxy": ""})
            continue
        # IP 段：isInNet(host, "***已脱敏***-内网IP", "255.255.255.0")
        m = re.search(r'isInNet\(host,\s*"([^"]+)"\s*,\s*"([^"]+)"\)', s)
        if m:
            target = _pac_pick_target(s)
            rules.append({"match": "IP 段", "content": f"{m.group(1)}/{_pac_mask_to_cidr(m.group(2))}", "action": target, "proxy": ""})
            continue

    # 去重（同一内容 + 动作）
    seen = set()
    unique: list[dict[str, str]] = []
    for rule in rules:
        key = (rule["match"], rule["content"], rule["action"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(rule)
    return unique, default_action, proxy_addr


def _pac_pick_target(pms_line: str) -> str:
    """从一行 return 语句里挑出动作文字。"""
    if '"DIRECT"' in pms_line:
        return "直连 DIRECT"
    m = re.search(r'return\s+"(PROXY|SOCKS)\s*([^"]*)"', pms_line)
    if m:
        kind = "走代理 PROXY" if m.group(1) == "PROXY" else "走 SOCKS SOCKS"
        addr = m.group(2).strip()
        return kind if not addr else kind  # 地址单独存 proxy 字段，这里只留动作
    return "直连 DIRECT"


def _pac_mask_to_cidr(pms_mask: str) -> str:
    try:
        a, b, c, d = (int(x) for x in pms_mask.split("."))
    except Exception:
        return pms_mask
    bits = 0
    for octet in (a, b, c, d):
        bits += bin(octet).count("1")
    return str(bits)


# --------------------------------------------------------------------------------------
# 向导（让完全不懂的人也能一步步配出来）
# --------------------------------------------------------------------------------------
class _PacWizard(QWizard):
    """三步向导：① 想要什么效果 → ② 哪些网站特殊 → ③ 代理地址 / 默认动作。"""

    def __init__(self, pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.setWindowTitle("PAC 分流向导（小白专用）")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.pw_result: dict[str, Any] = {"default": "默认走代理", "rules": [], "proxy": ""}

        # 第 1 页：整体意图
        p1 = QWizardPage()
        p1.setTitle("第 1 步：你想要的效果")
        p1.setSubTitle("先选一个你最想要的整体效果，后面再单独挑「例外」网站。")
        layout = QVBoxLayout(p1)
        self.pw_choice_all_proxy = QRadioButton("绝大多数网站都走代理，国内几个直连")
        self.pw_choice_all_direct = QRadioButton("绝大多数网站都直连，只有少数走代理")
        self.pw_choice_proxy_only = QRadioButton("只有我选的网站走代理，其它一律直连")
        self.pw_choice_direct_only = QRadioButton("全部直连（等于不代理）")
        self.pw_choice_all_proxy.setChecked(True)
        layout.addWidget(self.pw_choice_all_proxy)
        layout.addWidget(self.pw_choice_all_direct)
        layout.addWidget(self.pw_choice_proxy_only)
        layout.addWidget(self.pw_choice_direct_only)
        p1.setLayout(layout)
        self.addPage(p1)

        # 第 2 页：特殊网站
        p2 = QWizardPage()
        p2.setTitle("第 2 步：哪些网站要「特殊对待」")
        p2.setSubTitle("每行填一个，例如 baidu.com 或 google.com。留空表示没有例外。")
        layout = QVBoxLayout(p2)
        self.pw_special_edit = QPlainTextEdit()
        self.pw_special_edit.setPlaceholderText("每行一个，例如：\nbaidu.com\ngoogle.com\ntaobao.com")
        layout.addWidget(self.pw_special_edit)
        p2.setLayout(layout)
        self.addPage(p2)

        # 第 3 页：代理地址与默认动作
        p3 = QWizardPage()
        p3.setTitle("第 3 步：代理地址")
        p3.setSubTitle("PAC 分流最终要落到你本机的代理入口（由核心提供）。")
        layout = QVBoxLayout(p3)
        form = QFormLayout()
        self.pw_proxy_edit = QLineEdit("127.0.0.1:30808")
        form.addRow("代理地址（IP:端口）", self.pw_proxy_edit)
        self.pw_proxy_hint = pms_make_hint_label(
            "默认就是本程序核心的「混合入口」地址。除非你清楚在改什么，否则别动它。"
        )
        layout.addLayout(form)
        layout.addWidget(self.pw_proxy_hint)
        p3.setLayout(layout)
        self.addPage(p3)

    def accept(self) -> None:
        if self.pw_choice_all_proxy.isChecked():
            default = "默认走代理"
            special_action = "直连 DIRECT"
        elif self.pw_choice_all_direct.isChecked():
            default = "默认直连"
            special_action = "走代理 PROXY"
        elif self.pw_choice_proxy_only.isChecked():
            default = "默认直连"
            special_action = "走代理 PROXY"
        else:  # 全部直连
            default = "默认直连"
            special_action = "直连 DIRECT"
        specials = [s.strip().lower() for s in pms_multiline_to_list(self.pw_special_edit.toPlainText()) if s.strip()]
        rules = []
        for site in specials:
            rules.append({"match": "域名后缀", "content": site, "action": special_action, "proxy": ""})
        self.pw_result = {"default": default, "rules": rules, "proxy": self.pw_proxy_edit.text().strip()}
        super().accept()


# --------------------------------------------------------------------------------------
# 主对话框
# --------------------------------------------------------------------------------------
class PmsPacEditorDialog(QDialog):
    """内置 PAC 脚本编辑器：专业模式（手写 JS）+ 小白模式（表单 / 模板 / 向导）。"""

    pced_action = Signal(str, dict)  # 与 aboutdialog 同源的事件出口，便于上层统一分发

    def __init__(
        self,
        pms_paths: PmsRuntimePaths,
        pms_settings: PmsSettingsManager,
        pms_profile: Optional[PmsCoreLaunchProfile] = None,
        pms_parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pced_paths = pms_paths
        self.pced_settings = pms_settings
        self.pced_profile = pms_profile
        self.setWindowTitle(f"{PMS_APP_NAME} · PAC 脚本编辑器")
        self.setMinimumSize(820, 640)
        self.pced_rules: list[dict[str, str]] = []
        self.pced_build_ui()
        self.pced_load_file()

    # ------------------------------------------------------------------ UI
    def pced_build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        root.addWidget(pms_make_hint_label(
            "PAC 是一份「访问每个网站时该直连还是走代理」的判断脚本。这里内置了两种写法："
            "<b>专业模式</b>直接写脚本，<b>小白模式</b>用大白话填表就能生成。产物文件 "
            f"<b>{PMS_CUSTOM_PAC_FILENAME}</b> 与系统代理的自动脚本互不干扰。"
        ))

        self.pced_tabs = QTabWidget()
        root.addWidget(self.pced_tabs, 1)
        self.pced_tabs.addTab(self.pced_build_pro_tab(), "专业模式")
        self.pced_tabs.addTab(self.pced_build_beginner_tab(), "小白模式")

        buttons = QHBoxLayout()
        save_button = QPushButton("保存到文件")
        save_button.clicked.connect(self.pced_save)
        apply_button = QPushButton("应用到系统代理")
        apply_button.clicked.connect(self.pced_apply)
        open_button = QPushButton("打开 PAC 文件所在目录")
        open_button.clicked.connect(lambda: self.pced_action.emit("open_folder", {"path": str(self.pced_paths.prt_data_dir)}))
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(save_button)
        buttons.addWidget(apply_button)
        buttons.addWidget(open_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

    def pced_build_pro_tab(self) -> QWidget:
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("直接编辑 FindProxyForURL 脚本（懂 JS 才改，小白请用「小白模式」）："))
        self.pced_pro_edit = QPlainTextEdit()
        self.pced_pro_edit.setFont(QFont("Consolas", 11))
        self.pced_pro_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.pced_pro_edit, 1)
        layout.addWidget(pms_make_hint_label(
            "保存后点「应用到系统代理」即可生效。若你在这里写的脚本无法被小白模式反向识别，"
            "小白表格会清空——这是正常的，不代表脚本出错。"
        ))
        return area

    def pced_build_beginner_tab(self) -> QWidget:
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(4, 4, 4, 4)

        # 模板 + 向导快捷入口
        top = QHBoxLayout()
        top.addWidget(QLabel("常用模板："))
        self.pced_template_combo = QComboBox()
        self.pced_template_combo.addItem("（选择一个模板一键套用…）")
        for name, _default, _rules in _PAC_TEMPLATES:
            self.pced_template_combo.addItem(name)
        self.pced_template_combo.currentIndexChanged.connect(self.pced_apply_template)
        top.addWidget(self.pced_template_combo, 1)
        wizard_button = QPushButton("向导（一步步问）")
        wizard_button.clicked.connect(self.pced_run_wizard)
        top.addWidget(wizard_button)
        layout.addLayout(top)

        # 默认动作
        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("没命中任何规则时（默认）："))
        self.pced_default_combo = QComboBox()
        self.pced_default_combo.addItems(_PAC_DEFAULT_ACTIONS)
        default_row.addWidget(self.pced_default_combo)
        default_row.addStretch(1)
        layout.addLayout(default_row)

        # v1.9.67：默认走代理的地址设置（留空 = 本程序地址和端口）
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(QLabel("默认走代理的地址："))
        self.pced_default_proxy_edit = QLineEdit()
        self.pced_default_proxy_edit.setPlaceholderText("留空 = 用本程序的地址和端口")
        self.pced_default_proxy_edit.setMinimumWidth(220)
        self.pced_default_proxy_edit.textChanged.connect(self.pced_refresh_explain)
        proxy_row.addWidget(self.pced_default_proxy_edit, 1)
        layout.addLayout(proxy_row)
        layout.addWidget(pms_make_hint_label(
            "这里填「走代理」时统一用的地址。<b>留空就是本程序自己的地址和端口</b>（面板上显示的入口）；"
            "要改用外部代理（例如 Clash / v2ray 在别的端口），在这里填 <b>127.0.0.1:端口</b> 即可。"
            "规则里单独填了地址的，优先用规则里那个。"
        ))

        # 规则表
        self.pced_table = QTableWidget(0, 5)
        self.pced_table.setHorizontalHeaderLabels(["匹配类型", "匹配内容", "动作", "代理地址（走代理时填）", "备注"])
        self.pced_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # v1.9.67：允许多选——配合下面的「批量修改」按钮，一次改多条规则的
        # 动作 / 代理地址 / 备注（用户需求原文：「列表可以多选修改 动作 和 代理地址 备注 列的内容」）。
        self.pced_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # v1.9.67：动作 / 代理地址 / 备注 三列改为可直接双击编辑；
        # 匹配类型、匹配内容仍走「编辑选中」弹窗（避免手抖改坏匹配语义）。
        self.pced_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self.pced_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.pced_table, 1)

        # 增删改 + 生成代码
        row = QHBoxLayout()
        add_button = QPushButton("+ 添加规则")
        add_button.clicked.connect(self.pced_add_rule)
        edit_button = QPushButton("编辑选中")
        edit_button.clicked.connect(self.pced_edit_rule)
        del_button = QPushButton("删除选中")
        del_button.clicked.connect(self.pced_del_rule)
        gen_button = QPushButton("生成代码 → 专业模式")
        gen_button.clicked.connect(self.pced_generate)
        row.addWidget(add_button)
        row.addWidget(edit_button)
        row.addWidget(del_button)
        row.addStretch(1)
        row.addWidget(gen_button)
        layout.addLayout(row)

        # v1.9.67：批量修改（多选后一次改多条规则的动作 / 代理地址 / 备注）
        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("批量修改选中规则："))
        batch_action_button = QPushButton("改「动作」")
        batch_action_button.clicked.connect(self.pced_batch_edit_action)
        batch_proxy_button = QPushButton("改「代理地址」")
        batch_proxy_button.clicked.connect(self.pced_batch_edit_proxy)
        batch_note_button = QPushButton("改「备注」")
        batch_note_button.clicked.connect(self.pced_batch_edit_note)
        batch_row.addWidget(batch_action_button)
        batch_row.addWidget(batch_proxy_button)
        batch_row.addWidget(batch_note_button)
        batch_row.addStretch(1)
        layout.addLayout(batch_row)

        # 实时中文解释
        self.pced_explain = QLabel("")
        self.pced_explain.setWordWrap(True)
        self.pced_explain.setObjectName("hintLabel")
        layout.addWidget(self.pced_explain)

        self.pced_table.itemSelectionChanged.connect(self.pced_refresh_explain)
        self.pced_table.itemChanged.connect(self.pced_on_item_changed)
        return area

    # ------------------------------------------------------------------ 文件读写
    def pced_load_file(self) -> None:
        path: Path = self.pced_paths.prt_custom_pac_file
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = ""
        else:
            text = ""
        self.pced_pro_edit.setPlainText(text)
        rules, default_action, proxy = _pac_parse_js(text)
        # v1.9.67：把解析出的代理地址回填到「默认走代理的地址」框里。
        # 注意：只有当解析值与本程序当前入口**不同**时才回填——否则用户明明没设过
        # 外部地址，一打开对话框就被塞成显式值，之后核心换端口就不跟随了。
        if proxy and proxy != self.pced_current_proxy_addr():
            self.pced_default_proxy_edit.setText(proxy)
        if rules or default_action != "默认走代理" or proxy:
            self.pced_rules = rules
            idx = self.pced_default_combo.findText(default_action)
            if idx >= 0:
                self.pced_default_combo.setCurrentIndex(idx)
            self.pced_refresh_table()
            if not rules and text.strip():
                self.pced_explain.setText(
                    "已读入现有脚本，但其中的规则无法自动转成小白表格（可能是复杂写法）。"
                    "可在「专业模式」直接编辑；小白表格留空不影响保存。"
                )
        else:
            # 全新：给一条示例，避免小白面对空表不知所措
            self.pced_rules = [
                {"match": "域名后缀", "content": "baidu.com", "action": "直连 DIRECT", "proxy": "", "note": "示例：百度直连"},
            ]
            self.pced_refresh_table()

    def pced_save(self) -> bool:
        text = self.pced_pro_edit.toPlainText()
        path = self.pced_paths.prt_custom_pac_file
        try:
            ok = pms_atomic_write_text(path, text)
        except OSError as exc:
            self.pced_action.emit("toast", {"text": f"保存失败：{exc}"})
            return False
        if not ok:
            self.pced_action.emit("toast", {"text": "保存失败（原子写返回 False）"})
            return False
        self.pced_action.emit("toast", {"text": f"已保存：{path}"})
        return True

    def pced_apply(self) -> None:
        # 1) 先确保已保存最新脚本
        if not self.pced_save():
            return
        # 2) 备份用户原系统代理（若还没备份），避免还原时把原值弄丢
        controller = PmsWindowsSystemProxy(logging.getLogger(PMS_LOGGER_NAME + ".paceditor"))
        if not self.pced_settings.psm_get_bool("system_proxy.backup_saved", False):
            controller.pwsp_backup_to_settings(self.pced_settings)
        # 3) 切到「用户 PAC」来源 + 开启系统代理（PAC 模式）
        self.pced_settings.psm_set("system_proxy.enabled", True)
        self.pced_settings.psm_set("system_proxy.mode", PMS_SYSTEM_PROXY_MODE_PAC)
        self.pced_settings.psm_set("system_proxy.pac_source", "custom")
        # 4) 真正写注册表（只指向已存在的自定义文件，不重新生成内容）
        ok = controller.pwsp_enable_pac_file(self.pced_paths.prt_custom_pac_file)
        if ok:
            self.pced_action.emit("toast", {"text": "已应用到系统代理（使用 PAC 编辑器脚本）"})
            # 让主窗口同步开关状态（若上层把自己传进来了）
            parent = self.parent()
            if parent is not None and hasattr(parent, "pmw_sync_system_proxy_action"):
                try:
                    parent.pmw_sync_system_proxy_action(True)  # type: ignore[attr-defined]
                except Exception:
                    pass
        else:
            self.pced_action.emit("toast", {"text": "应用失败（请检查权限，或确认核心已启动提供代理入口）"})

    # ------------------------------------------------------------------ 小白模式逻辑
    def pced_refresh_table(self) -> None:
        # v1.9.67：重填表期间屏蔽 itemChanged，避免程序性写表被当成「用户编辑」回写覆盖规则
        self.pced_table.blockSignals(True)
        try:
            self.pced_table.setRowCount(len(self.pced_rules))
            for row, rule in enumerate(self.pced_rules):
                # 动作 / 代理地址 / 备注 三列可编辑；匹配类型与内容只读（走弹窗改）
                for col, key in ((0, "match"), (1, "content"), (2, "action"), (3, "proxy"), (4, "note")):
                    item = QTableWidgetItem(rule.get(key, ""))
                    if col in (0, 1):
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.pced_table.setItem(row, col, item)
        finally:
            self.pced_table.blockSignals(False)
        self.pced_refresh_explain()

    def pced_on_item_changed(self, pms_item: QTableWidgetItem) -> None:
        """v1.9.67：把列表里就地编辑的「动作 / 代理地址 / 备注」写回规则数据。

        只认这三列（匹配类型 / 内容列已设成不可编辑，双保险）。
        """
        row, col = pms_item.row(), pms_item.column()
        if not (0 <= row < len(self.pced_rules)):
            return
        key = {2: "action", 3: "proxy", 4: "note"}.get(col)
        if key is None:
            return
        self.pced_rules[row][key] = pms_item.text().strip()
        self.pced_refresh_explain()

    def pced_refresh_explain(self) -> None:
        row = self.pced_table.currentRow()
        if 0 <= row < len(self.pced_rules):
            rule = self.pced_rules[row]
            action_text = "直连（不走代理）" if rule.get("action", "").startswith("直连") else f"走代理（{rule.get('proxy') or '默认入口'}）"
            match_text = {
                "域名后缀": f"当网站域名以「{rule.get('content','')}」结尾时",
                "关键词": f"当网址里包含「{rule.get('content','')}」时",
                "IP 段": f"当目标 IP 落在「{rule.get('content','')}」段内时",
                "全部流量": "对所有网站",
            }.get(rule.get("match", ""), "匹配时")
            self.pced_explain.setText(f"这条规则的意思是：{match_text}，{action_text}。")
        else:
            default = self.pced_default_combo.currentText()
            # v1.9.67：把「默认走代理的地址」也说清楚，用户才知道留空到底走了哪儿
            fallback = self.pced_default_proxy_edit.text().strip() or self.pced_current_proxy_addr()
            fallback_desc = (
                f"本程序入口 {fallback}" if not self.pced_default_proxy_edit.text().strip()
                else f"外部代理 {fallback}"
            )
            self.pced_explain.setText(
                f"当前共 {len(self.pced_rules)} 条规则；没命中的网站将「{default}」"
                f"（{ '直连' if default.startswith('默认直连') else '走代理' }）。"
                f"规则里没填代理地址的，统一走 {fallback_desc}。"
            )

    def pced_add_rule(self) -> None:
        self.pced_edit_rule_dialog(None)

    def pced_edit_rule(self) -> None:
        row = self.pced_table.currentRow()
        if row < 0:
            self.pced_action.emit("toast", {"text": "请先选中一条规则再编辑"})
            return
        self.pced_edit_rule_dialog(row)

    def pced_edit_rule_dialog(self, pms_row: Optional[int]) -> None:
        rule = self.pced_rules[pms_row].copy() if pms_row is not None else {
            "match": _PAC_MATCH_TYPES[0], "content": "", "action": _PAC_ACTIONS[0], "proxy": "", "note": ""}
        dlg = _PacRuleDialog(rule, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if pms_row is None:
                self.pced_rules.append(dlg.pac_rule)
            else:
                self.pced_rules[pms_row] = dlg.pac_rule
            self.pced_refresh_table()

    def pced_del_rule(self) -> None:
        row = self.pced_table.currentRow()
        if row < 0:
            self.pced_action.emit("toast", {"text": "请先选中一条规则再删除"})
            return
        self.pced_rules.pop(row)
        self.pced_refresh_table()

    def pced_apply_template(self, pms_index: int) -> None:
        if pms_index <= 0:
            return
        name, default_action, rules = _PAC_TEMPLATES[pms_index - 1]
        self.pced_rules = [dict(r) for r in rules]
        idx = self.pced_default_combo.findText(default_action)
        if idx >= 0:
            self.pced_default_combo.setCurrentIndex(idx)
        self.pced_refresh_table()
        self.pced_action.emit("toast", {"text": f"已套用模板：{name}（可继续增删改）"})

    def pced_run_wizard(self) -> None:
        wizard = _PacWizard(self)
        if wizard.exec() == QDialog.DialogCode.Accepted:
            result = wizard.pw_result
            self.pced_rules = result["rules"]
            idx = self.pced_default_combo.findText(result["default"])
            if idx >= 0:
                self.pced_default_combo.setCurrentIndex(idx)
            # 代理地址存进每条「走代理」规则里，方便展示；生成代码时统一取第一条
            for rule in self.pced_rules:
                if rule["action"].startswith("走代理"):
                    rule["proxy"] = result["proxy"]
            self.pced_refresh_table()
            self.pced_generate()
            self.pced_action.emit("toast", {"text": "向导已完成，已生成代码，检查后保存即可"})

    def pced_generate(self) -> None:
        proxy = ""
        for rule in self.pced_rules:
            if rule.get("proxy"):
                proxy = rule["proxy"]
                break
        if not proxy:
            proxy = self.pced_current_proxy_addr()
        # v1.9.67：「默认走代理的地址设置」——留空则 internal 回落为本程序入口
        default_proxy = self.pced_default_proxy_edit.text().strip()
        text = _pac_compile_rules(
            self.pced_rules, self.pced_default_combo.currentText(), proxy, default_proxy
        )
        self.pced_pro_edit.setPlainText(text)
        self.pced_tabs.setCurrentWidget(self.pced_tabs.widget(0))
        self.pced_action.emit("toast", {"text": "已生成代码，已切到「专业模式」，可保存"})

    # ------------------------------------------------------------------ 批量修改（v1.9.67）
    def pced_selected_rows(self) -> list[int]:
        """当前选中的规则行号（升序去重）。多选模式下可能一次改多条。"""
        rows = sorted({index.row() for index in self.pced_table.selectedIndexes()})
        return [row for row in rows if 0 <= row < len(self.pced_rules)]

    def pced_batch_edit_action(self) -> None:
        """批量改「动作」列。"""
        rows = self.pced_selected_rows()
        if not rows:
            self.pced_action.emit("toast", {"text": "请先在列表里选中要修改的规则（可按住 Ctrl / Shift 多选）"})
            return
        text, ok = QInputDialog.getItem(
            self, "批量修改动作", f"把选中的 {len(rows)} 条规则的动作统一改为：", _PAC_ACTIONS, 0, False
        )
        if not ok:
            return
        for row in rows:
            self.pced_rules[row]["action"] = text
        self.pced_refresh_table()
        self.pced_action.emit("toast", {"text": f"已把 {len(rows)} 条规则的动作改为「{text}」"})

    def pced_batch_edit_proxy(self) -> None:
        """批量改「代理地址」列。留空表示改回用默认入口。"""
        rows = self.pced_selected_rows()
        if not rows:
            self.pced_action.emit("toast", {"text": "请先在列表里选中要修改的规则（可按住 Ctrl / Shift 多选）"})
            return
        current = self.pced_rules[rows[0]].get("proxy", "")
        text, ok = QInputDialog.getText(
            self, "批量修改代理地址",
            f"把选中的 {len(rows)} 条规则的代理地址统一改为：\n（留空 = 改用「默认走代理的地址」）",
            text=current,
        )
        if not ok:
            return
        for row in rows:
            self.pced_rules[row]["proxy"] = str(text).strip()
        self.pced_refresh_table()
        self.pced_action.emit("toast", {"text": f"已修改 {len(rows)} 条规则的代理地址"})

    def pced_batch_edit_note(self) -> None:
        """批量改「备注」列。"""
        rows = self.pced_selected_rows()
        if not rows:
            self.pced_action.emit("toast", {"text": "请先在列表里选中要修改的规则（可按住 Ctrl / Shift 多选）"})
            return
        current = self.pced_rules[rows[0]].get("note", "")
        text, ok = QInputDialog.getText(
            self, "批量修改备注", f"把选中的 {len(rows)} 条规则的备注统一改为：", text=current
        )
        if not ok:
            return
        for row in rows:
            self.pced_rules[row]["note"] = str(text).strip()
        self.pced_refresh_table()
        self.pced_action.emit("toast", {"text": f"已修改 {len(rows)} 条规则的备注"})

    def pced_current_proxy_addr(self) -> str:
        if self.pced_profile is not None:
            port = self.pced_profile.pclp_primary_port()
            if port:
                return f"127.0.0.1:{port}"
        return "127.0.0.1:30808"


# --------------------------------------------------------------------------------------
# 单条规则编辑对话框
# --------------------------------------------------------------------------------------
class _PacRuleDialog(QDialog):
    """小白模式下「添加 / 编辑一条规则」的弹窗，全中文 + 大白话。"""

    def __init__(self, pms_rule: dict[str, str], pms_parent: Optional[QWidget] = None) -> None:
        super().__init__(pms_parent)
        self.setWindowTitle("编辑一条分流规则")
        self.pac_rule = dict(pms_rule)
        layout = QFormLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        self.pac_match = QComboBox()
        self.pac_match.addItems(_PAC_MATCH_TYPES)
        self.pac_match.setCurrentText(pms_rule.get("match", _PAC_MATCH_TYPES[0]))
        layout.addRow("① 匹配哪种网站", self.pac_match)

        self.pac_content = QLineEdit(pms_rule.get("content", ""))
        self.pac_content.setPlaceholderText("例如 baidu.com 或 *.github.com 或 google")
        layout.addRow("② 填什么（域名 / 关键词 / IP段）", self.pac_content)

        self.pac_action = QComboBox()
        self.pac_action.addItems(_PAC_ACTIONS)
        self.pac_action.setCurrentText(pms_rule.get("action", _PAC_ACTIONS[0]))
        layout.addRow("③ 命中后怎么走", self.pac_action)

        self.pac_proxy = QLineEdit(pms_rule.get("proxy", ""))
        self.pac_proxy.setPlaceholderText("走代理时填，如 127.0.0.1:30808（留空用默认入口）")
        layout.addRow("④ 代理地址", self.pac_proxy)

        self.pac_note = QLineEdit(pms_rule.get("note", ""))
        self.pac_note.setPlaceholderText("给自己看的备注，随便写")
        layout.addRow("⑤ 备注", self.pac_note)

        layout.addRow(pms_make_hint_label(
            "大白话：①选「按域名结尾认」最常用；②填 baidu.com 这类；填 *.github.com 也行，"
            "等价于「github.com 及其所有子站」；③选直连或走代理；"
            "④走代理才需要填地址。不懂就保持默认，点确定就行。"
        ))

        buttons = QHBoxLayout()
        ok_button = QPushButton("确定")
        ok_button.clicked.connect(self.accept)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(ok_button)
        buttons.addWidget(cancel_button)
        layout.addRow(buttons)

    def accept(self) -> None:
        self.pac_rule = {
            "match": self.pac_match.currentText(),
            "content": self.pac_content.text().strip(),
            "action": self.pac_action.currentText(),
            "proxy": self.pac_proxy.text().strip(),
            "note": self.pac_note.text().strip(),
        }
        super().accept()
