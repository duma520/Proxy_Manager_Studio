# Proxy_Manager_Studio v256

> **一句话**：Proxy_Manager_Studio —— v1.9.74 离线探针：电报频道抽取 tg 专属代理链接（无 GUI、无网络）。
>
> **适合谁**：想直接用现成工具、不想自己写代码的 Windows 用户。
>
> **解决什么问题**：把「v1.9.74 离线探针：电报频道抽取 tg 专属代理链接（无 GUI、无网络）」这件事做成开箱即用的程序 —— 下载解压、装上依赖、双击就能跑。

---

## 目录

- [1. 这是什么](#1-这是什么)
- [2. 零基础入门（5 分钟跑起来）](#2-零基础入门5-分钟跑起来)
- [3. 功能与界面](#3-功能与界面)
- [4. 命令行参数](#4-命令行参数)
- [5. 配置文件与数据](#5-配置文件与数据)
- [6. 工作原理（技术参考）](#6-工作原理技术参考)
- [7. 排错手册](#7-排错手册)
- [8. 常见问题 FAQ](#8-常见问题-faq)
- [9. 目录结构](#9-目录结构)
- [10. 开发与构建](#10-开发与构建)
- [11. 已知限制与安全说明](#11-已知限制与安全说明)
- [12. 许可](#12-许可)

## 1. 这是什么

| 项目 | 说明 |
| --- | --- |
| 程序名 | `Proxy_Manager_Studio` |
| 当前版本 | v256 |
| 主程序 | `Proxy_Manager_Studio.py` |
| 界面技术 | PySide6（Qt6 官方 Python 绑定） |
| 语言 / 运行平台 | Python 3.10 / Windows |
| 源码规模 | 根目录及子模块共 293 个 .py 文件 |
| 仓库 | https://github.com/duma520/Proxy_Manager_Studio |

## 2. 零基础入门（5 分钟跑起来）

### 第 1 步：装 Python

1. 打开 <https://www.python.org/downloads/windows/>，下载 Python 3.10 或更新版本。
2. 安装时**务必勾选** `Add python.exe to PATH`（安装向导第一页最下方）。
3. 装完按 `Win + R` 输入 `cmd`，敲 `python --version`，能看到版本号就成功了。

### 第 2 步：拿到源码

- **下载压缩包**：在本仓库 Releases 页下载 `Proxy_Manager_Studio-*.zip`，解压到任意目录。
- **git 克隆**：`git clone https://github.com/duma520/Proxy_Manager_Studio.git`

### 第 3 步：装依赖

在源码目录打开命令行，执行：

```
pip install PySide6 PyYAML cProfile check_release_consistency cryptography dcprobe_fake fcntl psutil python-dotenv qrcode scaffold_quiet scaffold_sandbox scaffold_window_source
```

> 清单是从源码的实际 import 汇总出来的；若某个包名不准确，以 pip 报错提示的正确包名为准。

### 第 4 步：启动

```bat
python Proxy_Manager_Studio.py
```

目录里还带了 `1.bat`，双击等同于上面的命令（前提是 Python 在 PATH 里）。

## 3. 功能与界面

> 本节只列源码里能确认的事实；具体按钮与交互以运行后的界面为准。

| 维度 | 实际情况 |
| --- | --- |
| 界面框架 | PySide6（Qt6 官方 Python 绑定） |
| 是否有 main 入口 | 是，可直接 python 主程序.py 运行 |
| 第三方依赖数 | 13 个 |

### 版本变更（来自项目自带迭代记录）

| 版本 | 说明 |
| --- | --- |
| 5 | `pms/coresys.py` `pwsp_enable_pac()` + `pms/gui/dialogs/sysproxywidget.py` + `pmw_apply_system_proxy()` 按 mode 分流 |
| 8 | `pms/sharelinks_extra.py` `pslxp_parse_ssr/naive/shadowtls` + 导出 |
| 19 | `pms/testresume.py` 多轮并存（`test.round.<id>` + 索引），旧键 `test.pending_round` 只读并迁移 |
| 22 | `pms/database.py` `event.log.<seq>` 落 `pms_meta` + `pdb_prune_meta_prefix()` |
| 23 | `pms/api.py` 计数器名与方法名分开 |
| 24 | `pms/download.py` Range 断点续传 + 同链路重试 |
| 29 | 读库与建模型提速（取证见 `_scaffold/_diag_v1955_*.py`） |
| 30 | `pms/coreclean.py` + 「清理未命中的内核文件」按钮 |

## 4. 命令行参数

主程序支持的参数（从 argparse 定义抽取）：

| 参数 | 说明 |
| --- | --- |
| `--only` | 逗号分隔的探针名（可省 probe_ 前缀） |
| `--timeout` | 覆盖单探针超时秒数 |
| `--record` | 把实际结果写回 probe_registry.json |
| `--list` | 只列登记表 |
| `--logs` | 失败的探针打印完整尾部输出 |
| `--max-output` | --logs 时每个探针最多打印多少字符 |

## 5. 配置文件与数据

源码目录里可见的配置/数据文件：

- `pms_settings.json`
- `pms_settings_history.json`

这些文件在运行时读写；个人数据（路径、账号、历史记录）不会提交到仓库，由 .gitignore 排除，首次运行会自动生成。

## 6. 工作原理（技术参考）

```
用户操作 → PySide6 → 业务逻辑 → 结果输出
```

- 入口文件 `Proxy_Manager_Studio.py`，带 if __name__ == '__main__' 保护。
- 依赖的第三方库：`PySide6`、`PyYAML`、`cProfile`、`check_release_consistency`、`cryptography`、`dcprobe_fake`、`fcntl`、`psutil`、`python-dotenv`、`qrcode`、`scaffold_quiet`、`scaffold_sandbox`、`scaffold_window_source`。
- 全部业务逻辑在仓库内可见，没有编译产物、没有联网下发代码。

## 7. 排错手册

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| python 不是内部或外部命令 | 安装时没勾 PATH | 重装 Python 并勾选 Add python.exe to PATH，或改用 py 命令 |
| ModuleNotFoundError: No module named xxx | 依赖没装 | 按第 3 步 pip install 对应包 |
| 双击 .bat 一闪而过 | 出错后窗口立即关闭 | 用 cmd 进入目录再执行命令，看清报错 |
| 界面显示乱码 | 控制台编码不是 UTF-8 | 先执行 chcp 65001，或用 python -X utf8 主程序.py |
| 杀毒软件拦截 | 未签名脚本被误判 | 加入信任/白名单；本项目只提供源码，可自行审阅 |

## 8. 常见问题 FAQ

**Q1：需要装 Visual Studio 之类的编译工具吗？**
A：不需要。这里只提供 Python 源码，pip 装的是预编译轮子。

**Q2：能在 macOS / Linux 上跑吗？**
A：本项目按 Windows 环境开发与验证；其他系统未测试，可能需要改路径与系统 API 调用。

**Q3：为什么 Releases 里只有源码 zip，没有 exe？**
A：只分发源码，避免二进制被误判；需要 exe 请按第 10 节自行打包。

**Q4：改了代码怎么运行？**
A：直接 python 主程序.py，改动立即生效，无需编译。

**Q5：数据存在哪？会不会被上传？**
A：只存在本机（程序目录或用户目录）。本程序不含上传逻辑。

**Q6：怎么反馈问题？**
A：到 https://github.com/duma520/Proxy_Manager_Studio/issues 提 Issue，附上报错原文与复现步骤。

## 9. 目录结构

```
.dsh/
.workbuddy/
Proxy_Manager_Studio/
_scaffold/
build_output/
core/
data/
docs/
logs/
pms/
.env
.env.example
.gitignore
0_nuitka_pyside6_clang_auto.bat
1.bat
2_debug.bat
Proxy_Manager_Studio.py
Proxy_Manager_Studio.spec
README.md
_bench_load.py
_bench_load2.py
_bench_model.py
_dbg_handshake.py
_probe_async_load.py
_probe_qrunnable.py
_smoke_e2e.txt
_smoke_e2e2.txt
_smoke_gui_modules.txt
_symbols.log
_tmp_mtproxy_src.py
_torver.txt
icon.ico
pms_runtime.log
pms_settings.json
pms_settings_history.json
pms_store.db
probe_v1973_proxy_order.py
probe_v1974_tg_proxy.py
probe_v1975_update_filters.py
requirements.txt
smoke_full.log
smoke_modules.log
软件升级迭代记录.md
软件升级迭代记录.md.pre-inverse-bak
.workbuddy\memory/
```

## 10. 开发与构建

- 直接运行（开发态）：`python Proxy_Manager_Studio.py`
- 语法自检：`python -m py_compile Proxy_Manager_Studio.py`
- 打包 exe（可选）：装 pyinstaller 后执行 `pyinstaller -F -w Proxy_Manager_Studio.py`

## 11. 已知限制与安全说明

- 只提供源码，不提供预编译二进制。
- 仓库不含任何凭据；.env / *.pem / *.key / 日志与运行期数据均在 .gitignore 中排除。
- 若程序需要联网，请在源码中确认目标地址后再运行。

## 12. 许可

AGPL-3.0，详见仓库根目录 LICENSE。
