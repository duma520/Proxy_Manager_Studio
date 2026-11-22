# core 目录说明（内置代理核心）

程序本身不带代理能力，它负责**管理**并**驱动**下面这些开源核心：

| 目录 | 文件 | 版本（本项目验证过的） | 用途 |
|---|---|---|---|
| `core\xray\` | `xray.exe`、`geoip.dat`、`geosite.dat`、`wintun.dll`、`LICENSE`、`LICENSE-xray.txt` | Xray 26.3.27 | 主力核心（VLESS/VMess/Trojan/SS，XTLS-Vision、Reality、XHTTP 等特性最全） |
| `core\sing-box\` | `sing-box.exe`、`libcronet.dll`、`LICENSE` | sing-box 1.14.2 | 需要 TUN 模式、Hysteria2 / TUIC、Clash API 连接列表时使用 |

程序的搜索顺序（设置里留空路径时）：

1. `core\xray\`、`core\sing-box\`、`core\core\`、`core\`
2. 程序根目录（`Proxy_Manager_Studio.py` / `.exe` 所在目录）
3. 系统 `PATH`
4. 上面都不满足时用「设置 → 核心」里手填的绝对路径

## 获取方式（二选一）

* **程序内下载**：`设置 → 核心 → 下载 / 更新 Xray / sing-box`（从官方 GitHub Release 拉取，带进度条；
  国内网络慢时可填「下载镜像前缀」，或在 `.env` 里设置 `PMS_CORE_DOWNLOAD_MIRROR`）。
* **手工放置**：从官方发布页下载 Windows x64 压缩包，把 `xray.exe` / `sing-box.exe` 等文件解压到对应子目录即可。

* Xray-core：<https://github.com/XTLS/Xray-core/releases>（MPL-2.0）
* sing-box：<https://github.com/SagerNet/sing-box/releases>（GPL-3.0）

> 本目录下的二进制均来自上述官方发布页，仅为本机运行程序所需，请遵守各自许可证。
> 若只需要一种核心，删掉另一个子目录即可，程序会自动使用剩下的那个。
