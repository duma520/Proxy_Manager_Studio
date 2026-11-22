#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub 仓库订阅源（v1.9.18）

**用户只需要填一个仓库地址**（`https://github.com/owner/repo` 或 `owner/repo`），
剩下的全自动：调用 GitHub API 动态发现仓库里的节点文件 → 按优先级挑文件 → 下载合并 →
交给现成的 `PmsTextImportClassifier` 解析。**没有任何写死的路径**。

## 为什么不能写死路径（实查过的真实仓库结构，2026-10-10）

| 仓库 | 结构 | 说明 |
|---|---|---|
| `sevcator/5ubscrpt10n` | `mini/m1n1-5ub-1..102.txt`（每片 ~2.4 MB）、`protocols/{ss,tr,vl,vm}.txt`（26–105 MB！）、`working/links.json`（**内容是「订阅地址列表」而不是节点**）、`working/countries/*.txt`（259 个） | 同一个仓库里既有小片也有 100 MB 巨物，必须**按字节预算挑**，否则会下几百 MB |
| `MatinGhanbari/v2ray-configs` | `subscriptions/base64/all_sub.txt`、`subscriptions/filtered/subs/vmess.txt`… | 有 `base64` 目录，正文是 base64（分类器本来就认） |
| `Epodonios/v2ray-configs` | `All_Configs_base64_Sub.txt`、`Splitted-By-Protocol/vless.txt`、`Base64/Sub7_base64.txt`、`Sub7.txt` | 分片 + 协议拆分 + base64 三种混在一起 |
| `free-nodes/v2rayfree` | `v20261010`、`v20261009`、`v20261008`、`sub`（**没有扩展名**！） | 用户说的「文件名可能是时间戳」就是这个：**按文件名里的日期取最新** |
| `hans-thomas/v2ray-subscription` | `servers.txt` | 名字里没有任何关键词，只有后缀能用 |

## 三级下载链路（实测 raw.githubusercontent.com 在本机被重置，api.github.com 可用）

```
1. raw.githubusercontent.com/<owner>/<repo>/<sha>/<path>     ← 不占 API 配额（首选）
2. <镜像前缀>/raw.githubusercontent.com/...                  ← 复用 core.download_mirror（设了就试）
3. api.github.com/repos/<o>/<r>/git/blobs/<blob_sha>         ← Accept: application/vnd.github.raw（兜底，占 1 次配额）
```

## 配额保护（匿名 60 次/小时）

- **缓存 commit SHA**（写 `pms_meta` 的 `github.sha.<订阅uid>`）：SHA 没变**一个文件都不下**，
  一次检查只花 **1 次** API 调用。
- SHA 变了才拉文件树（+1 次），之后全走 raw（不占配额）。
- 每次 API 调用都记数并在日志里报剩余配额（`X-RateLimit-Remaining`）。
- 403/429 且剩余为 0 时给出「频率超限，X 分钟后重试」的明确提示，而不是一句 HTTP 403。
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------------------
# 常量（放本模块而不放 constants.py：那个文件已经顶在 1000 行红线上，见项目宪法第 6 条）
# --------------------------------------------------------------------------------------
PMS_GITHUB_API_ROOT = "https://api.github.com"
PMS_GITHUB_RAW_ROOT = "https://raw.githubusercontent.com"
PMS_GITHUB_WEB_ROOT = "https://github.com"

# 协议 / 订阅关键词：名字里带这些的优先级最高
PMS_GITHUB_STRONG_KEYWORDS: tuple[str, ...] = (
    "vless", "vmess", "trojan", "shadowsocks", "hysteria", "tuic", "reality", "ssr",
)
PMS_GITHUB_WEAK_KEYWORDS: tuple[str, ...] = (
    "sub", "config", "node", "proxy", "v2ray", "clash", "link", "list", "full", "all", "server",
)
# 允许的正文类型（没有扩展名的也算——`v20261010` 这种就是）
PMS_GITHUB_TEXT_EXTS: tuple[str, ...] = (".txt", ".json", ".yaml", ".yml", ".list", ".conf")
# 目录黑名单（这些目录里不放节点数据）
PMS_GITHUB_EXCLUDE_DIRS: tuple[str, ...] = (
    ".github/", "node_modules/", ".git/", "docs/", "doc/", "test/", "tests/", "example/",
    "examples/", "sample/", "samples/", "assets/", "images/", "img/", "static/",
)
# 文件名黑名单（README / 许可证 / 脚本 / 依赖清单等我方用不上的）
PMS_GITHUB_EXCLUDE_NAMES: tuple[str, ...] = (
    "readme", "license", "licence", "changelog", "contributing", "package.json", "package-lock.json",
    "tsconfig", "composer.json", "requirements.txt", "cargo.toml", "go.mod", "go.sum", ".gitignore",
    "makefile", "dockerfile", "yarn.lock", "pnpm-lock", "index.js", "main.js", "action.yml",
)
# 明确不是节点正文的后缀
PMS_GITHUB_BINARY_EXTS: tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".zip", ".gz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh",
    ".bat", ".ps1", ".cmd", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".css", ".scss", ".html",
    ".htm", ".lock", ".woff", ".woff2", ".ttf", ".otf", ".db", ".sqlite", ".md",
)
# 「内容是订阅地址列表」的线索（命中就顺着往下抓一层，用户明确要求处理这种情况）
PMS_GITHUB_LINK_HINTS: tuple[str, ...] = (
    "raw.githubusercontent.com", "/subscribe", "/sub?", "/api/v1/client/subscribe", "/clash",
    "/v2ray", "/sub/", "sub.txt", "/link", "/config",
)


def pms_github_repo_from_url(pms_text: str) -> tuple[str, str]:
    """从各种写法里提取 (owner, repo)；不是 GitHub 仓库引用时返回 ("", "")。

    认这些写法（大小写不敏感、允许尾部斜杠 / `.git` / `tree|blob/<分支>/...` 子路径）：
        https://github.com/owner/repo
        http://github.com/owner/repo.git
        github.com/owner/repo/tree/main/mini
        owner/repo
        git@github.com:owner/repo.git
    """
    text = str(pms_text or "").strip()
    if not text:
        return "", ""
    text = text.split("#", 1)[0].split("?", 1)[0].strip()
    match = re.search(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", text, re.IGNORECASE)
    if match:
        owner, repo = match.group(1), match.group(2)
    elif re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?", text):
        owner, repo = text.rstrip("/").split("/", 1)
    else:
        return "", ""
    repo = re.sub(r"\.git$", "", repo, flags=re.IGNORECASE)
    if not owner or not repo or owner.lower() in ("orgs", "users", "topics", "collections"):
        return "", ""
    if repo in (".", ".."):
        return "", ""
    return owner, repo


def pms_github_url(owner: str, repo: str) -> str:
    """规范化成标准仓库地址（存进订阅记录用）。"""
    return f"{PMS_GITHUB_WEB_ROOT}/{owner}/{repo}"


def pms_github_is_repo_url(pms_text: str) -> bool:
    """这段文本能不能当成 GitHub 仓库地址用。"""
    owner, repo = pms_github_repo_from_url(pms_text)
    return bool(owner and repo)


def pms_github_name_dates(pms_name: str) -> int:
    """从文件名里取「日期」当新旧排序键（0 = 名字里没有日期）。

    只认 8~14 位且以 19/20 开头的数字串（`v20261010` → 20261010，`20261010-1.txt` → 20261010），
    普通分片序号（`1.txt` / `25.txt`）**不算日期**，否则会误判成「25 比 1 新」而丢掉前 24 片。
    """
    best = 0
    for chunk in re.findall(r"\d{8,14}", str(pms_name or "")):
        if chunk[:2] in ("19", "20"):
            best = max(best, int(chunk))
    return best


def pms_github_tokens(pms_name: str) -> list[str]:
    """把文件名切成词（按非字母数字切分，再拆开「字母/数字」边界）。

    `m1n1-5ub-1.txt` → `['m1n1', '5', 'ub', '1', 'txt']`。
    切词是为了让短关键词**必须整词匹配**：否则 `Wallis` 里的 `all`、`class` 里的 `ss` 会误命中，
    实测 `working/countries/Wallis & Futuna.txt` 就会因为名字里有 `all` 而被排到前面去。
    """
    return [chunk for chunk in re.split(r"[^0-9a-z]+|(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", str(pms_name or "").lower()) if chunk]


def pms_github_keyword_bonus(pms_name: str) -> int:
    """关键词加分：协议名认子串（`vless` / `vmess`…），短词只认**整词或词首**。

    为什么短词要这么严：`Wallis` 里有 `all`、`class` 里有 `ss`，纯子串匹配会把
    `working/countries/Wallis & Futuna.txt` 当成「节点文件」排到前面（实测踩过）。
    但也不能只认整词——`links.json`（复数）就匹配不上 `link`，所以放宽到「词首」。
    """
    name = str(pms_name or "").lower()
    tokens = pms_github_tokens(name)
    bonus = 0
    for word in PMS_GITHUB_STRONG_KEYWORDS:
        if word in name:
            bonus += 120
            break
    for word in PMS_GITHUB_WEAK_KEYWORDS:
        if (len(word) >= 5 and word in name) or any(token.startswith(word) for token in tokens):
            bonus += 80
            break
    return bonus


def pms_github_stem(pms_path: str) -> str:
    """把路径里的数字串换成 `#`，得到「同族名」——`mini/x-1.txt` 与 `mini/x-2.txt` 同族。"""
    return re.sub(r"\d+", "#", str(pms_path or "").lower())


def pms_github_file_score(pms_path: str) -> int:
    """给一个仓库文件打分（越大越优先）；返回 -1 表示「明确不要」。"""
    path = str(pms_path or "")
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    if not name or name.startswith("."):
        return -1
    if any(part in low for part in PMS_GITHUB_EXCLUDE_DIRS):
        return -1
    if any(mark in name for mark in PMS_GITHUB_EXCLUDE_NAMES):
        return -1
    ext = ("." + name.rsplit(".", 1)[1]) if "." in name else ""
    if ext in PMS_GITHUB_BINARY_EXTS:
        return -1
    if name == "sub" or not ext:            # `free-nodes/v2rayfree` 的 `v20261010` / `sub` 就是没有扩展名
        score = 40
    elif ext in PMS_GITHUB_TEXT_EXTS:
        score = 70 if ext == ".txt" else 55
    else:
        return -1
    score += pms_github_keyword_bonus(name)
    if "base64" in low:
        score += 10                        # base64 正文分类器认，但要排在明文后面
    if "country" in low or "geo" in low:
        score -= 10                        # 按国家拆的碎片很多，略微降权
    return score


def pms_github_pick_files(
    pms_tree: list[dict[str, Any]],
    pms_max_files: int = 20,
    pms_max_total_bytes: int = 24 * 1024 * 1024,
    pms_max_file_bytes: int = 12 * 1024 * 1024,
) -> tuple[list[dict[str, Any]], list[str]]:
    """从文件树里挑出要下载的文件，返回 (挑中的文件, 跳过说明)。

    排序：**名字里的日期新的优先** → **关键词得分高的优先** → **文件小的优先** → 路径字典序。

    - 「日期优先」是用户要求（时间戳命名取最新，`free-nodes/v2rayfree` 的 `v20261010` 就是这种）。
    - 「关键词优先」在实查里很关键：`sevcator/5ubscrpt10n` 里 `mini/m1n1-5ub-1.txt` 这类**分片**
      名字里没有标准关键词，靠「同族文件多」这条加分（`mini/…-1.txt` 有 102 个兄弟）就能排到前面，
      而不是被一堆几百字节的 `working/countries/X.txt` 挤掉。
    - 「文件小的优先」只作**同分时的兜底**：同一个仓库里既有 2.4 MB 的分片也有 105 MB 的合并版，
      预算有限时先拿小片更划算。
    """
    families: dict[str, int] = {}
    for item in pms_tree:
        if str(item.get("type") or "") == "blob":
            stem = pms_github_stem(str(item.get("path") or ""))
            families[stem] = families.get(stem, 0) + 1
    candidates: list[tuple[int, int, int, str, dict[str, Any]]] = []
    for item in pms_tree:
        if str(item.get("type") or "") != "blob":
            continue
        path = str(item.get("path") or "")
        score = pms_github_file_score(path)
        if score < 0:
            continue
        size = int(item.get("size") or 0)
        if size <= 0:
            continue
        if families.get(pms_github_stem(path), 0) >= 3:
            score += 45                    # 同族（分片）文件：1.txt / 2.txt / … 这种
        candidates.append((pms_github_name_dates(path), score, size, path, item))
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    picked: list[dict[str, Any]] = []
    skipped: list[str] = []
    total = 0
    for _date, _score, size, path, item in candidates:
        if len(picked) >= max(1, int(pms_max_files)):
            skipped.append(f"{path}（超出文件数上限）")
            continue
        if size > int(pms_max_file_bytes):
            skipped.append(f"{path}（单文件 {size / 1024 / 1024:.1f} MB 超过上限）")
            continue
        if total + size > int(pms_max_total_bytes):
            skipped.append(f"{path}（超出总字节预算）")
            continue
        picked.append(item)
        total += size
    return picked, skipped


def pms_github_extract_urls(pms_text: str, pms_limit: int = 5) -> list[str]:
    """从正文里挑出「看起来是订阅地址」的链接（用于「文件里放的是订阅地址」这种情况）。"""
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s\"'<>()\[\],;]+", str(pms_text or "")):
        url = match.group(0).rstrip(".,;:)")
        low = url.lower()
        if any(low.endswith(ext) or f"{ext}?" in low for ext in PMS_GITHUB_BINARY_EXTS):
            continue
        if any(mark in low for mark in PMS_GITHUB_LINK_HINTS):
            if url not in urls:
                urls.append(url)
        if len(urls) >= max(1, int(pms_limit)):
            break
    return urls


class PmsGithubRepoSource:
    """一个 GitHub 仓库订阅源的抓取实现（无 Qt 依赖，便于离线探针直接测）。"""

    def __init__(
        self,
        pms_owner: str,
        pms_repo: str,
        pms_user_agent: str = "",
        pms_timeout: float = 25.0,
        pms_token: str = "",
        pms_mirror: str = "",
        pms_logger: Optional[Any] = None,
        pms_should_abort: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.pgs_owner = str(pms_owner or "").strip()
        self.pgs_repo = str(pms_repo or "").strip()
        self.pgs_user_agent = str(pms_user_agent or "").strip() or "pms-github-source"
        self.pgs_timeout = max(3.0, float(pms_timeout))
        self.pgs_token = str(pms_token or "").strip()
        self.pgs_mirror = str(pms_mirror or "").strip().rstrip("/")
        self.pgs_logger = pms_logger
        self.pgs_should_abort = pms_should_abort or (lambda: False)
        self.pgs_api_calls = 0
        self.pgs_download_bytes = 0
        self.pgs_rate_remaining: Optional[int] = None
        self.pgs_quota_low = False
        self.pgs_notes: list[str] = []
        self.pgs_last_error = ""

    # ------------------------------------------------------------------ 基础设施
    def pgs_log(self, pms_level: str, pms_message: str, *pms_args: Any) -> None:
        if self.pgs_logger is not None:
            getattr(self.pgs_logger, pms_level, self.pgs_logger.info)(pms_message, *pms_args)

    def pgs_headers(self, pms_accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "User-Agent": self.pgs_user_agent,
            "Accept": pms_accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.pgs_token:
            headers["Authorization"] = f"Bearer {self.pgs_token}"
        return headers

    def pgs_api(self, pms_opener: Any, pms_path: str, pms_accept: str = "application/vnd.github+json") -> tuple[Any, str]:
        """发一次 API 请求，返回 (解析后的 JSON, 错误说明)。"""
        if self.pgs_should_abort():
            return None, "已中断"
        url = f"{PMS_GITHUB_API_ROOT}{pms_path}"
        self.pgs_api_calls += 1
        request = urllib.request.Request(url, headers=self.pgs_headers(pms_accept))
        try:
            with pms_opener.open(request, timeout=self.pgs_timeout) as response:
                self.pgs_rate_remaining = self._pgs_rate_header(response)
                payload = response.read(32 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            self.pgs_rate_remaining = self._pgs_rate_header(exc)
            return None, self._pgs_http_error(exc)
        except urllib.error.URLError as exc:
            return None, f"网络错误：{getattr(exc, 'reason', exc)}"
        except (OSError, ValueError) as exc:
            return None, f"请求异常：{exc}"
        if pms_accept.endswith("vnd.github.raw"):
            return payload.decode("utf-8", errors="replace"), ""
        try:
            return json.loads(payload.decode("utf-8", errors="replace")), ""
        except ValueError as exc:
            return None, f"GitHub 返回的不是合法 JSON：{exc}"

    @staticmethod
    def _pgs_rate_header(pms_response: Any) -> Optional[int]:
        try:
            value = pms_response.headers.get("X-RateLimit-Remaining")
            return None if value is None else int(value)
        except (AttributeError, TypeError, ValueError):
            return None

    def _pgs_http_error(self, pms_exc: urllib.error.HTTPError) -> str:
        code = int(getattr(pms_exc, "code", 0) or 0)
        if code == 404:
            return f"仓库不存在或不是公开仓库：{self.pgs_owner}/{self.pgs_repo}"
        if code in (403, 429):
            remaining = self.pgs_rate_remaining
            if remaining == 0:
                reset = ""
                try:
                    stamp = pms_exc.headers.get("X-RateLimit-Reset")
                    if stamp:
                        minutes = max(1, int((int(stamp) - time.time()) // 60) + 1)
                        reset = f"，约 {minutes} 分钟后恢复"
                except (TypeError, ValueError):
                    reset = ""
                return f"GitHub API 频率超限（匿名请求 60 次/小时{reset}）；可启用代理或稍后再试"
            return f"GitHub 拒绝访问（HTTP {code}）：{getattr(pms_exc, 'reason', '')}"
        return f"HTTP {code} {getattr(pms_exc, 'reason', '')}"

    # ------------------------------------------------------------------ 三步流程
    def pgs_head_sha(self, pms_opener: Any) -> tuple[str, str]:
        """取仓库最新 commit SHA（`/commits?per_page=1`，1 次 API 调用）。"""
        payload, error = self.pgs_api(pms_opener, f"/repos/{self.pgs_owner}/{self.pgs_repo}/commits?per_page=1")
        if error:
            return "", error
        if isinstance(payload, list) and payload:
            return str(payload[0].get("sha") or ""), ""
        if isinstance(payload, dict) and payload.get("message"):
            return "", f"仓库不可用：{payload.get('message')}"
        return "", "GitHub 未返回任何提交（空仓库？）"

    def pgs_tree(self, pms_opener: Any, pms_sha: str) -> tuple[list[dict[str, Any]], str]:
        """取整个文件树（`/git/trees/<sha>?recursive=1`，1 次 API 调用）。"""
        payload, error = self.pgs_api(pms_opener, f"/repos/{self.pgs_owner}/{self.pgs_repo}/git/trees/{pms_sha}?recursive=1")
        if error:
            return [], error
        if not isinstance(payload, dict):
            return [], "文件树格式异常"
        tree = [item for item in payload.get("tree", []) if isinstance(item, dict)]
        if payload.get("truncated"):
            self.pgs_notes.append("仓库文件树过大被 GitHub 截断，只扫描了部分文件")
        return tree, ""

    def pgs_raw_url(self, pms_sha: str, pms_path: str) -> str:
        quoted = urllib.parse.quote(str(pms_path).lstrip("/"))
        return f"{PMS_GITHUB_RAW_ROOT}/{self.pgs_owner}/{self.pgs_repo}/{pms_sha}/{quoted}"

    def pgs_download(
        self,
        pms_opener: Any,
        pms_sha: str,
        pms_path: str,
        pms_blob_sha: str = "",
        pms_max_bytes: int = 12 * 1024 * 1024,
    ) -> tuple[str, str]:
        """下载一个文件正文，返回 (正文, 错误说明)。raw → 镜像 → Blob API 三级退让。"""
        errors: list[str] = []
        for label, url, accept in self._pgs_download_candidates(pms_sha, pms_path, pms_blob_sha):
            if self.pgs_should_abort():
                return "", "已中断"
            try:
                payload = self._pgs_read_url(
                    pms_opener, url, accept, pms_max_bytes, pms_count_api=label == "Blob API",
                )
            except urllib.error.HTTPError as exc:
                errors.append(f"{label}：HTTP {exc.code}")
                continue
            except urllib.error.URLError as exc:
                errors.append(f"{label}：{getattr(exc, 'reason', exc)}")
                continue
            except (OSError, ValueError) as exc:
                errors.append(f"{label}：{exc}")
                continue
            self.pgs_download_bytes += len(payload)
            if label == "Blob API" and self.pgs_rate_remaining is not None and self.pgs_rate_remaining <= 5:
                # raw 打不通时会一路走 Blob API（每个文件占 1 次配额）。剩余不多就先停手，
                # 把「没下完」如实写在说明里——总比把 60 次/小时的配额烧光、后面全报频率超限强。
                self.pgs_quota_low = True
            return payload.decode("utf-8", errors="replace"), ""
        return "", f"下载失败（{ '；'.join(errors) or '无可用通路' }）"
    def _pgs_download_candidates(self, pms_sha: str, pms_path: str, pms_blob_sha: str) -> list[tuple[str, str, str]]:
        raw_url = self.pgs_raw_url(pms_sha, pms_path)
        candidates: list[tuple[str, str, str]] = [("raw", raw_url, "text/plain")]
        if self.pgs_mirror:
            candidates.append(("镜像", f"{self.pgs_mirror}/{raw_url}", "text/plain"))
        if pms_blob_sha:
            candidates.append((
                "Blob API",
                f"{PMS_GITHUB_API_ROOT}/repos/{self.pgs_owner}/{self.pgs_repo}/git/blobs/{pms_blob_sha}",
                "application/vnd.github.raw",
            ))
        return candidates

    def _pgs_read_url(self, pms_opener: Any, pms_url: str, pms_accept: str, pms_max_bytes: int, pms_count_api: bool = False) -> bytes:
        request = urllib.request.Request(pms_url, headers=self.pgs_headers(pms_accept))
        if pms_count_api:
            self.pgs_api_calls += 1      # 只有真的走 Blob API 才占配额
        with pms_opener.open(request, timeout=self.pgs_timeout) as response:
            if pms_accept.endswith("vnd.github.raw"):
                self.pgs_rate_remaining = self._pgs_rate_header(response)
            return response.read(int(pms_max_bytes) + 1)[: int(pms_max_bytes)]

    # ------------------------------------------------------------------ 对外主流程
    def pgs_fetch(
        self,
        pms_opener: Any,
        pms_cached_sha: str = "",
        pms_max_files: int = 20,
        pms_max_total_mb: int = 24,
        pms_max_nodes: int = 500,
        pms_follow_links: bool = True,
    ) -> dict[str, Any]:
        """跑完整流程：查 SHA → （变了才）拉文件树 → 挑文件 → 下载。

        返回 ``{ok, changed, sha, chunks, skipped, notes, error, api_calls, bytes}``；
        `chunks` 是 ``[(来源路径, 正文), …]``，由调用方逐个解析（不同文件可能是 base64 / YAML / 明文）。
        """
        result: dict[str, Any] = {
            "ok": False, "changed": False, "sha": "", "chunks": [], "skipped": [],
            "notes": [], "error": "", "api_calls": 0, "bytes": 0,
        }
        sha, error = self.pgs_head_sha(pms_opener)
        if error:
            result["error"] = error
            result["api_calls"] = self.pgs_api_calls
            return result
        result["sha"] = sha
        if pms_cached_sha and pms_cached_sha == sha:
            self.pgs_notes.append("仓库没有新提交，跳过下载")
            result.update({"ok": True, "changed": False, "notes": list(self.pgs_notes)})
            result["api_calls"] = self.pgs_api_calls
            return result
        tree, error = self.pgs_tree(pms_opener, sha)
        if error:
            result["error"] = error
            result["api_calls"] = self.pgs_api_calls
            return result
        max_bytes = max(1, int(pms_max_total_mb)) * 1024 * 1024
        picked, skipped = pms_github_pick_files(tree, pms_max_files, max_bytes, min(max_bytes, 12 * 1024 * 1024))
        result["skipped"] = skipped[:20]
        if not picked:
            result["error"] = (
                "仓库里没有找到可用的节点文件（只认 .txt/.json/.yaml/.yml 或没有扩展名的数据文件，"
                "README / LICENSE / 脚本 / 图片都会被忽略）"
            )
            result["api_calls"] = self.pgs_api_calls
            return result
        self.pgs_notes.append(f"发现 {len(picked)} 个候选文件（共 {len(tree)} 个）")
        chunks: list[tuple[str, str]] = []
        for item in picked:
            if self.pgs_should_abort():
                result["error"] = "已中断"
                result["api_calls"] = self.pgs_api_calls
                return result
            path = str(item.get("path") or "")
            text, error = self.pgs_download(pms_opener, sha, path, str(item.get("sha") or ""), 12 * 1024 * 1024)
            if error:
                self.pgs_notes.append(f"{path} 下载失败：{error}")
                continue
            chunks.append((path, text))
            if self.pgs_quota_low:
                self.pgs_notes.append(
                    f"GitHub API 配额剩余不多（{self.pgs_rate_remaining}），先停在这里；"
                    "剩下的文件下次配额恢复后再取（也可以填 GitHub token 提升到 5000 次/小时）"
                )
                break
        if not chunks:
            result["error"] = "候选文件都没能下载下来（网络或仓库限流），详见日志"
            result["api_calls"] = self.pgs_api_calls
            return result
        if pms_follow_links:
            self._pgs_follow(chunks, pms_opener, pms_max_nodes)
        result.update({"ok": True, "changed": True, "chunks": chunks})
        result["notes"] = list(self.pgs_notes)
        result["api_calls"] = self.pgs_api_calls
        result["bytes"] = self.pgs_download_bytes
        return result

    def _pgs_follow(self, pms_chunks: list[tuple[str, str]], pms_opener: Any, pms_max_nodes: int) -> None:
        """有些仓库的 json / txt 里放的是**别的订阅地址**（实测 `5ubscrpt10n/working/links.json` 就是）。

        这里顺着抓一层（最多 3 个、每个 4 MB），把抓到的正文并进 chunks，
        由分类器按内容格式解析。**只跟一层**，不递归，避免下载风暴。
        """
        followed = 0
        for path, text in list(pms_chunks):
            if followed >= 3 or pms_max_nodes < 500:
                break
            if "://" not in text:
                continue
            for url in pms_github_extract_urls(text, 3):
                if followed >= 3 or self.pgs_should_abort():
                    return
                try:
                    payload = self._pgs_read_url(pms_opener, url, "text/plain", 4 * 1024 * 1024)
                except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
                    self.pgs_notes.append(f"{url} 跟随抓取失败：{exc}")
                    continue
                self.pgs_download_bytes += len(payload)
                pms_chunks.append((url, payload.decode("utf-8", errors="replace")))
                followed += 1
                self.pgs_notes.append(f"文件里是订阅地址，已跟随抓取：{url}")

    # ------------------------------------------------------------------ 兼容性小工具
    @staticmethod
    def pgs_decode_base64_text(pms_text: str) -> str:
        """把 base64 正文还原成明文（分类器也会做，这里给探针单独用）。"""
        try:
            return base64.b64decode(str(pms_text).strip() + "===", validate=False).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""
