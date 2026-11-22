# -*- coding: utf-8 -*-
"""本地 Web 面板（v1.9.35）：由本地接口服务直接吐一张自带样式的单页，浏览器里看状态 / 节点 / 动作。

设计要点（改动前先读这里）：
1. **面板页本身不含任何数据**，所以不需要 token；所有数据都走既有的 `/api/v1/*` 接口，
   带 token 才给（`api.auth_mode=off` 时除外）。这样「打开页面」与「能不能看到数据」是两件事。
2. 写操作**完全跟随 `api.write_enabled`**（默认关）：关着时面板只读，按钮直接置灰并写明原因。
3. 页面**绝不外链**任何 CDN / 字体 / 图片（内联 CSS + 内联 SVG favicon）——本程序面向的常常是
   需要代理才能上网的环境，外链会让面板白屏。
4. HTML 是**常量字符串**，组装用 `str.replace` 填几个占位符（别用 f-string：里面满是 `{` `}`）。
"""
from __future__ import annotations

from typing import Any

from pms.constants import PMS_API_TOKEN_ENV  # noqa: F401 - 面板提示里要写清 token 存在哪
from pms.constants import PMS_THEME_DARK, PMS_THEME_LIGHT

# 面板路径：**不能用 `/`**——`/` 已经被 Clash 兼容层占着（mihomo 客户端会访问它做健康检查）
PMS_WEB_PANEL_PATH = "/panel"

_PMS_LIGHT_VARS = (
    "--bg:#f7f8fa;--panel:#ffffff;--panel-alt:#f0f2f5;--border:#d9dde3;--fg:#1b1e24;--sub:#5f6673;"
    "--accent:#2b6cb0;--input:#ffffff;--input-border:#c9cfd8;--hover:#eef1f5;--line:#e6e9ee;"
    "--ok:#1b7f43;--bad:#c0392b;--warn:#8a5a00;--ok-bg:#e6f4ea;--ok-fg:#1b5e20;--bad-bg:#fdecea;--bad-fg:#a4282a"
)
_PMS_DARK_VARS = (
    "--bg:#14161a;--panel:#1b1e24;--panel-alt:#22262e;--border:#2f3641;--fg:#e6e8eb;--sub:#8a8f98;"
    "--accent:#69b4ff;--input:#22262e;--input-border:#333a45;--hover:#20242c;--line:#262b33;"
    "--ok:#3ec46d;--bad:#e05a5a;--warn:#e6b955;--ok-bg:#1d3f2b;--ok-fg:#7ed3a0;--bad-bg:#43212a;--bad-fg:#ef9aa8"
)

_PMS_PANEL_STYLE = """
__PMS_SCHEME_ROOT__
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 "Segoe UI",system-ui,-apple-system,"Microsoft YaHei",sans-serif}
a{color:var(--accent)}
header{display:flex;align-items:center;gap:12px;padding:12px 18px;background:var(--panel);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:5;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}
.badge{font-size:12px;padding:2px 8px;border-radius:10px;background:var(--panel-alt);color:var(--sub)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;background:var(--sub);margin-right:6px}
.dot.on{background:var(--ok)}.dot.off{background:var(--bad)}
main{padding:16px 18px 40px;max-width:1180px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(185px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.card h2{margin:0 0 6px;font-size:12px;color:var(--sub);font-weight:500}
.card .v{font-size:17px;font-weight:600;word-break:break-all}
.card .s{font-size:12px;color:var(--sub);margin-top:4px}
section{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-bottom:16px}
section h3{margin:0 0 10px;font-size:13px;font-weight:600;color:var(--fg)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
input,select,button{font:inherit;background:var(--input);color:var(--fg);border:1px solid var(--input-border);border-radius:7px;padding:6px 10px}
button{cursor:pointer}
button:hover:not(:disabled){border-color:var(--accent)}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--sub);font-weight:500;position:sticky;top:0;background:var(--panel)}
tbody tr:hover{background:var(--hover)}
td.name{white-space:normal;max-width:320px}
.pill{font-size:11px;padding:1px 7px;border-radius:9px;background:var(--panel-alt)}
.pill.ok{background:var(--ok-bg);color:var(--ok-fg)}.pill.bad{background:var(--bad-bg);color:var(--bad-fg)}
#events{font:12px/1.6 Consolas,"Courier New",monospace;max-height:200px;overflow:auto;color:var(--sub);white-space:pre-wrap}
.hint{color:var(--sub);font-size:12px}
.warn{color:var(--warn)}
footer{color:var(--sub);font-size:12px;padding:0 18px 24px;max-width:1180px;margin:0 auto}
code{background:var(--panel-alt);padding:1px 5px;border-radius:4px}
__PMS_DARK_BLOCK__
"""


def pms_web_panel_scheme(pms_theme: str) -> str:
    """程序主题 → 面板配色方案（v1.9.39）。

    · `light` / `dark`：用户在设置里**明确**选了浅色 / 深色 → 面板就写死成那一种；
    · 其余（默认的「原生（系统默认）」、以及「跟随系统」）：返回 `auto`
      —— 交给浏览器的 `prefers-color-scheme`，也就是**跟随系统默认**，
      原来整页写死深色（`:root{color-scheme:dark}` + `#14161a`），浅色系统里打开就是一坨黑。
    """
    theme = str(pms_theme or "").strip().lower()
    if theme in (PMS_THEME_LIGHT, PMS_THEME_DARK):
        return theme
    return "auto"


def pms_web_panel_style(pms_scheme: str = "auto") -> str:
    """按配色方案拼出面板样式表（CSS 变量 + 可选的深色媒体查询）。"""
    scheme = str(pms_scheme or "auto").strip().lower()
    if scheme == "light":
        root, dark_block = f":root{{color-scheme:light;{_PMS_LIGHT_VARS}}}", ""
    elif scheme == "dark":
        root, dark_block = f":root{{color-scheme:dark;{_PMS_DARK_VARS}}}", ""
    else:
        root = f":root{{color-scheme:light dark;{_PMS_LIGHT_VARS}}}"
        dark_block = f"@media (prefers-color-scheme:dark){{:root{{{_PMS_DARK_VARS}}}}}"
    return _PMS_PANEL_STYLE.replace("__PMS_SCHEME_ROOT__", root).replace("__PMS_DARK_BLOCK__", dark_block).strip()

_PMS_PANEL_SCRIPT = """
const PMS = { version: "__PMS_VERSION__", writeEnabled: __PMS_WRITE__, authNeeded: __PMS_AUTH__,
              tokenKey: "pms.panel.token", page: 1, pageSize: 50, nodes: [], groups: [], lastSeq: 0 };
const $ = (id) => document.getElementById(id);

function token(){ return localStorage.getItem(PMS.tokenKey) || ""; }
function setToken(v){ localStorage.setItem(PMS.tokenKey, v || ""); }
function headers(){ const h = {"Content-Type":"application/json"}; const t = token(); if (t) h["X-Pms-Token"] = t; return h; }

async function api(path, options){
  const res = await fetch("/api/v1" + path, Object.assign({headers: headers()}, options || {}));
  let body = {};
  try { body = await res.json(); } catch (e) { body = {ok:false, error:{message:"返回不是 JSON"}}; }
  if (res.status === 401) { $("tokenrow").style.display = "flex"; throw new Error(body.error ? body.error.message : "需要 token"); }
  if (!body.ok) throw new Error(body.error ? body.error.message : ("HTTP " + res.status));
  return body.data;
}

function bytes(n){ n = Number(n)||0; const u=["B","KB","MB","GB","TB"]; let i=0; while(n>=1024 && i<u.length-1){n/=1024;i++;} return n.toFixed(i?1:0)+" "+u[i]; }
function rate(n){ return bytes(n)+"/s"; }
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function proto(n){ return (n.protocol||"").toUpperCase(); }

function applyWriteState(){
  document.querySelectorAll("[data-write]").forEach(el => {
    el.disabled = !PMS.writeEnabled;
    el.title = PMS.writeEnabled ? "" : "写操作已关闭：请在「设置 → 接口设置」里打开「允许写操作」";
  });
  $("writehint").textContent = PMS.writeEnabled ? "" : "（只读模式：写操作已在接口设置里关闭）";
}

async function loadStatus(){
  try{
    const s = await api("/status");
    $("dot").className = "dot " + (s.core_running ? "on" : "off");
    $("coretext").textContent = s.core_running ? ("运行中 · " + (s.core_kind || "")) : "未运行";
    $("nodetotal").textContent = s.node_count;
    $("sysproxy").textContent = s.system_proxy ? "已开启" : "未开启";
    $("tun").textContent = s.tun ? "已开启" : "未开启";
    $("testing").textContent = s.test_running ? "正在测试" : "空闲";
    const cur = s.current_node_uid ? PMS.nodes.find(n => n.uid === s.current_node_uid) : null;
    if (!cur && s.current_node_uid) { try { PMS.nodes.push(await api("/nodes/" + s.current_node_uid)); } catch(e){} }
    const node = PMS.nodes.find(n => n.uid === s.current_node_uid);
    $("current").textContent = node ? (node.name + " [" + proto(node) + "]") : (s.current_node_uid || "未选择");
    $("currents").textContent = node ? (node.server + ":" + node.port) : "";
    $("err").textContent = "";
  } catch(e){ $("err").textContent = e.message || String(e); }
}

async function loadTraffic(){
  try{
    const t = await api("/traffic");
    $("up").textContent = rate((t.speed_bps||{}).up); $("down").textContent = rate((t.speed_bps||{}).down);
    $("uptotal").textContent = bytes((t.totals||{}).up) + " 累计"; $("downtotal").textContent = bytes((t.totals||{}).down) + " 累计";
    $("tsource").textContent = t.source ? ("来源 " + t.source) : "";
  } catch(e){}
}

async function loadGroups(){
  try{
    PMS.groups = await api("/groups");
    const sel = $("group");
    sel.innerHTML = '<option value="">全部分组</option>' + PMS.groups.map(g =>
      '<option value="' + esc(g.name) + '">' + esc(g.name) + "（" + g.count + "）</option>").join("");
  } catch(e){}
}

async function loadNodes(){
  try{
    const q = $("q").value.trim().toLowerCase();
    const group = $("group").value; const only = $("onlyenabled").checked ? "?enabled=1" : "";
    PMS.nodes = await api("/nodes" + only + (group ? (only ? "&" : "?") + "group=" + encodeURIComponent(group) : ""));
    const filtered = q ? PMS.nodes.filter(n => (n.name+" "+n.server+" "+proto(n)+" "+(n.group_name||"")).toLowerCase().includes(q)) : PMS.nodes;
    const pages = Math.max(1, Math.ceil(filtered.length / PMS.pageSize));
    if (PMS.page > pages) PMS.page = pages;
    const slice = filtered.slice((PMS.page-1)*PMS.pageSize, PMS.page*PMS.pageSize);
    $("nodelist").innerHTML = slice.map(n => '<tr><td class="name">' + esc(n.name) + '</td><td>' + proto(n) +
      '</td><td>' + esc(n.server) + ":" + n.port + '</td><td>' + (n.latency_ms == null ? "-" : n.latency_ms + " ms") +
      '</td><td>' + (n.speed_bps ? rate(n.speed_bps) : "-") + '</td><td>' + (n.enabled ? '<span class="pill ok">启用</span>' : '<span class="pill bad">停用</span>') +
      '</td><td><button data-write onclick="switchNode(\\'' + esc(n.uid) + '\\')">切换</button></td></tr>').join("");
    $("pageinfo").textContent = "第 " + PMS.page + " / " + pages + " 页 · 共 " + filtered.length + " 条（全部 " + PMS.nodes.length + " 条）";
    applyWriteState();
  } catch(e){ $("err").textContent = e.message || String(e); }
}

async function switchNode(uid){
  try { await api("/node/switch", {method:"POST", body: JSON.stringify({uid: uid})}); await loadStatus(); }
  catch(e){ $("err").textContent = "切换失败：" + (e.message || e); }
}
async function action(path, body){
  try { await api(path, {method:"POST", body: JSON.stringify(body || {})}); await loadStatus(); }
  catch(e){ $("err").textContent = "操作失败：" + (e.message || e); }
}

function connectEvents(){ loadEvents(); setInterval(() => { if (!document.hidden) loadEvents(); }, 3000); }

async function loadEvents(){
  try{
    const d = await api("/events/recent?since=" + PMS.lastSeq);
    if (!d.events || !d.events.length) { PMS.lastSeq = d.last_seq || PMS.lastSeq; return; }
    const box = $("events");
    const lines = d.events.slice().reverse().map(e => e.time + "  " + e.kind + "  " +
      (e.data && Object.keys(e.data).length ? JSON.stringify(e.data) : ""));
    box.textContent = lines.join("\\n") + "\\n" + box.textContent;
    if (box.textContent.length > 6000) box.textContent = box.textContent.slice(0, 6000);
    PMS.lastSeq = d.last_seq || PMS.lastSeq;
  } catch(e){}
}

function boot(){
  $("ver").textContent = "v" + PMS.version;
  if (!PMS.authNeeded) { $("tokenrow").style.display = "none"; }
  else { $("tokenrow").style.display = token() ? "none" : "flex"; }
  $("save").onclick = () => { setToken($("token").value.trim()); loadStatus(); loadNodes(); connectEvents(); };
  $("refresh").onclick = () => { loadStatus(); loadTraffic(); loadNodes(); };
  $("q").oninput = () => { PMS.page = 1; loadNodes(); };
  $("group").onchange = () => { PMS.page = 1; loadNodes(); };
  $("onlyenabled").onchange = () => { PMS.page = 1; loadNodes(); };
  $("prev").onclick = () => { if (PMS.page > 1) { PMS.page--; loadNodes(); } };
  $("next").onclick = () => { PMS.page++; loadNodes(); };
  $("bstart").onclick = () => action("/core/start");
  $("bstop").onclick = () => action("/core/stop");
  $("bstoptest").onclick = () => action("/test/stop");
  loadStatus().then(loadTraffic); loadGroups().then(loadNodes); connectEvents();
  applyWriteState();
  setInterval(() => { if (!document.hidden) { loadStatus(); loadTraffic(); } }, 5000);
}
window.addEventListener("DOMContentLoaded", boot);
"""

_PMS_PANEL_BODY = """
<header>
  <span id="dot" class="dot"></span>
  <h1>代理节点管理控制台</h1>
  <span class="badge" id="ver"></span>
  <span class="badge">本地面板</span>
  <span style="flex:1"></span>
  <span class="hint" id="writehint"></span>
  <button id="refresh">刷新</button>
</header>
<main>
  <div class="row" id="tokenrow" style="display:none;flex-direction:column;align-items:flex-start">
    <div class="hint">面板页本身不含数据；数据接口要 token。请在「设置 → 接口设置 → 复制 token」后粘到这里（只存在本机浏览器里）。</div>
    <div class="row" style="margin:0"><input id="token" type="password" placeholder="粘贴接口 token" size="44"><button class="primary" id="save">保存</button></div>
  </div>
  <div class="hint" id="err" style="color:var(--bad)"></div>
  <div class="cards">
    <div class="card"><h2>核心</h2><div class="v" id="coretext">-</div>
      <div class="row" style="margin:8px 0 0"><button data-write id="bstart">启动</button><button data-write id="bstop">停止</button></div></div>
    <div class="card"><h2>当前节点</h2><div class="v" id="current">-</div><div class="s" id="currents"></div></div>
    <div class="card"><h2>速率 / 累计</h2><div class="v" id="down">-</div><div class="s" id="downtotal"></div>
      <div class="v" id="up" style="margin-top:6px;font-size:15px">-</div><div class="s" id="uptotal"></div></div>
    <div class="card"><h2>节点 / 系统</h2><div class="v" id="nodetotal">-</div>
      <div class="s">系统代理：<span id="sysproxy">-</span> · TUN：<span id="tun">-</span></div>
      <div class="s">测试：<span id="testing">-</span> <button data-write id="bstoptest" style="margin-left:6px">停止测试</button></div></div>
  </div>
  <section>
    <h3>节点列表</h3>
    <div class="row">
      <input id="q" placeholder="搜索名称 / 地址 / 协议 / 分组" size="30">
      <select id="group"></select>
      <label class="hint"><input id="onlyenabled" type="checkbox"> 仅启用</label>
      <span style="flex:1"></span>
      <button id="prev">上一页</button><span class="hint" id="pageinfo"></span><button id="next">下一页</button>
    </div>
    <table><thead><tr><th>名称</th><th>协议</th><th>地址</th><th>延迟</th><th>速度</th><th>状态</th><th>操作</th></tr></thead>
    <tbody id="nodelist"></tbody></table>
  </section>
  <section><h3>事件流（轮询 <code>/api/v1/events/recent</code>，最近若干条）</h3><div id="events" class="hint">等待事件…</div></section>
</main>
<footer>
  面板地址 <code>__PMS_PANEL_PATH__</code> · 数据接口 <code>/api/v1/*</code> · token 存在 <code>__PMS_TOKEN_ENV__</code>（.env）。
  写操作跟随「接口设置 → 允许写操作」；面板关闭开关是「接口设置 → 启用本地 Web 面板」。
</footer>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%232b6cb0'/%3E%3Ctext x='16' y='22' font-size='17' text-anchor='middle' fill='white'%3EP%3C/text%3E%3C/svg%3E">
"""


def pms_web_panel_html(
    pms_version: str,
    pms_write_enabled: bool,
    pms_auth_needed: bool,
    pms_scheme: str = "auto",
) -> str:
    """拼出面板页（纯字符串，不含任何用户数据，所以**不需要 token**）。

    `pms_scheme`（v1.9.39）：`auto` = 跟随系统默认（原生），`light` / `dark` = 程序里明确选了就跟随程序。
    """
    script = (
        _PMS_PANEL_SCRIPT
        .replace("__PMS_VERSION__", str(pms_version or ""))
        .replace("__PMS_WRITE__", "true" if pms_write_enabled else "false")
        .replace("__PMS_AUTH__", "true" if pms_auth_needed else "false")
    )
    body = _PMS_PANEL_BODY.replace("__PMS_PANEL_PATH__", PMS_WEB_PANEL_PATH).replace(
        "__PMS_TOKEN_ENV__", PMS_API_TOKEN_ENV
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta name=\"robots\" content=\"noindex\">\n"
        f"<title>代理节点管理控制台 v{str(pms_version or '')} · 本地面板</title>\n"
        f"<style>{pms_web_panel_style(pms_scheme)}</style>\n</head>\n<body>\n{body}\n<script>{script}</script>\n</body>\n</html>\n"
    )


def pms_web_panel_disabled_html(pms_scheme: str = "auto") -> str:
    """面板被关掉时给一张人话说明页——浏览器里看到 JSON 404 会以为程序坏了。

    v1.9.39：这张说明页原来也写死深色，现在与主页面一样按方案取色（默认跟随系统）。
    """
    scheme = str(pms_scheme or "auto").strip().lower()
    if scheme == "light":
        vars_text, color_scheme = _PMS_LIGHT_VARS, "light"
    elif scheme == "dark":
        vars_text, color_scheme = _PMS_DARK_VARS, "dark"
    else:
        vars_text, color_scheme = _PMS_LIGHT_VARS, "light dark"
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>本地 Web 面板已关闭</title>\n"
        f"<style>:root{{color-scheme:{color_scheme};{vars_text}}}"
        f"body{{font:14px/1.7 system-ui,'Microsoft YaHei',sans-serif;background:var(--bg);color:var(--fg);padding:40px}}"
        f"code{{background:var(--panel-alt);padding:1px 5px;border-radius:4px}}"
        f".hint{{color:var(--sub)}}</style></head>\n<body>\n"
        "<h1 style=\"font-size:18px\">本地 Web 面板已关闭</h1>\n"
        "<p>在「设置 → 接口设置」里勾上 <b>启用本地 Web 面板</b> 即可打开本页。</p>\n"
        f"<p class=\"hint\">面板地址：<code>{PMS_WEB_PANEL_PATH}</code>；数据接口仍是 "
        "<code>/api/v1/*</code>（本页与关闭开关无关）。</p>\n"
        "</body></html>\n"
    )


def pms_web_panel_path_match(pms_path: str) -> bool:
    """是不是面板路径（`/panel`、`/panel/`、`/panel/index.html` 都算）。"""
    path = str(pms_path or "").split("?")[0].split("#")[0].rstrip("/")
    return path in (PMS_WEB_PANEL_PATH, PMS_WEB_PANEL_PATH + "/index.html")


def pms_web_panel_url(pms_host: str, pms_port: Any) -> str:
    """给界面显示的完整面板地址（`api.host` 是 0.0.0.0 时换成 127.0.0.1，点开就能用）。"""
    host = str(pms_host or "").strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    return f"http://{host}:{int(pms_port or 0)}{PMS_WEB_PANEL_PATH}"
