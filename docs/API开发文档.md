# Proxy Manager Studio —— API 开发文档

> 适用版本：**v1.6.4**（文档随版本更新；接口前缀 `/api/v1` 自 v1.6.0 起稳定，不会在 1.x 内变更）
> 唯一权威文档仍是根目录《软件升级迭代记录.md》，本文件只讲**怎么用接口**。

---

## 一、总览：四种用法同一套实现

| 用法 | 入口 | 适合 |
|---|---|---|
| 内置 HTTP REST | `http://127.0.0.1:20811/api/v1/...` | 脚本、定时任务、其它语言 |
| Clash / sing-box External Controller 兼容 | `http://127.0.0.1:20811/proxies` 等（**不带 `/api/v1`**） | yacd、Clash Dashboard 等现成面板 |
| Python 包接口 | `from pms.api import PmsApi` | 同进程内调用，无需开 HTTP |
| 导出端点 | `GET /api/v1/export/*` | 取走配置文本（**含真实口令，见风险提示**） |

**三者共用同一个门面 `pms/api.py` 的 `PmsApi`**，因此字段与返回结构完全一致，不存在「网页接口一套、脚本接口另一套」。

- 接口**默认关闭**：设置 → 接口 → 启用。
- 默认监听 `127.0.0.1:20811`，地址与端口可改。
- token **只存 `.env`**（`PMS_API_TOKEN`），不进 `pms_settings.json`，不打印到日志。
- 非本机地址（如 `0.0.0.0`）+ 关闭鉴权 = **拒绝启动**。

---

## 二、鉴权

三种模式（设置 → 接口 → 鉴权）：

| 模式 | 说明 |
|---|---|
| `auto`（默认） | 首次启用自动生成 token 并写入 `.env` |
| `manual` | 自己填，写入 `.env` |
| `off` | 关闭鉴权（**仅允许本机地址**） |

请求头二选一：

```
Authorization: Bearer <token>
X-PMS-Token: <token>
```

没带 / 带错 → `401`。**头名按小写比对**，不用担心大小写。

---

## 三、统一返回体

自有接口一律返回：

```json
{ "ok": true,  "data": { ... } }
{ "ok": false, "data": null, "error": { "code": "xxx", "message": "人类可读的原因" } }
```

兼容层（`/proxies` 等）返回 **Clash 风格**响应体：`{"message": "...", "error": "..."}`，主要看状态码。

### 状态码

| 状态码 | 含义 | 触发示例 |
|---|---|---|
| 200 | 成功 | 查询、导出、测速完成 |
| 204 | 成功且无内容 | `PUT /proxies/GLOBAL` 切换成功、`OPTIONS` 预检 |
| 400 | 请求不合法 | 缺字段、JSON 坏、节点字段校验不过 |
| 401 | 未鉴权 | token 缺失或不正确 |
| 403 | 写操作未开启 | `api.write_enabled = false` 时调用写接口 |
| 404 | 找不到 | 路径不存在、uid / 节点名不存在 |
| 405 | 方法不允许 | 用 POST 打只读路径 |
| 500 | 服务端异常 | 内部报错（已记日志） |
| 504 | 等待超时 | 动作派发后 5 秒没结果（写操作） |

### 常见错误码

`bad_request` `unauthorized` `write_disabled` `not_ready` `action_not_allowed`
`node_not_found` `no_node` `no_valid_node` `invalid_node` `invalid_link` `parse_failed`
`core_not_running` `core_not_found` `invalid_profile` `start_failed`
`tun_unsupported` `need_admin` `test_running` `timeout` `db_error` `server_error`

---

## 四、只读端点（`GET /api/v1/...`）

| 路径 | 返回要点 |
|---|---|
| `/health` | `{"status":"ok","app":...,"version":...}` |
| `/version` | 程序名 / 版本 / 接口前缀 |
| `/status` | 核心是否运行、核心类型、当前节点、系统代理、TUN、**是否在测速**、节点数、接口自身配置 |
| `/nodes` | 节点数组；支持 `?group=分组名`、`?enabled=1` |
| `/nodes/<uid>` | 单个节点 |
| `/groups` | `[{"name":"...","count":N}]` |
| `/traffic` | `{"totals":{...},"speed_bps":{...},"source":"..."}` |
| `/test/status` | `{"running":bool,"write_enabled":bool}` |
| `/export/links` | 分享链接文本；`?base64=1` 出 base64 订阅正文；`?group=` 过滤 |
| `/export/backup` | 完整备份 JSON（节点 + 分组 + 订阅） |
| `/export/clash` | Clash YAML |
| `/export/singbox` | sing-box 出站 JSON |

**节点字段一律不含口令**：`credential_password` / `obfs_password` / `credential_uuid` 全部剔除，另给 `has_credential` 布尔标记。

---

## 五、写操作端点（默认关闭）

先到「设置 → 接口」勾选 **允许写操作**（`api.write_enabled`），否则一律 `403 write_disabled`。

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| POST | `/core/start` | `{"uid":"可选"}` | 不传 uid 用当前节点 |
| POST | `/core/stop` | — | |
| POST | `/core/restart` | `{"uid":"可选"}` | |
| POST | `/node/switch` | `{"uid":"必填"}` | 含「先停后起」 |
| POST/PUT | `/system-proxy` | `{"enabled":true}` | 核心没起会被拦 |
| POST/PUT | `/tun` | `{"enabled":true}` | 需 sing-box + 管理员 |
| POST | `/nodes` | `{"link":"trojan://..."}` 或节点字段 | 新增 |
| PUT | `/nodes/<uid>` | 要改的字段 | `uid` 不可改 |
| DELETE | `/nodes/<uid>` | — | |
| POST | `/nodes/import` | `{"text":"...","links":[],"group":"导入"}` | 同步导入（**不抓订阅 URL**） |
| POST | `/test/latency` | 见下 | 延迟测试 |
| POST | `/test/speed` | 见下 | 真实测速 |
| POST | `/test/stop` | — | 停止三种测速通道 |

写操作会**派发到界面线程执行**（Qt 信号自动排队），请求线程最多等 5 秒，超时返回 `504 timeout`，绝不死等。

### 导入

```json
{ "text": "trojan://pwd@host:443#名字\nvmess://...", "group": "接口导入" }
```
或 `{"links": ["trojan://...", "vmess://..."]}`。

返回 `{"added":N,"updated":N,"invalid":N,"duplicates":N,"group":"...","nodes":[...]}`：
**命中已有节点按主程序规则走「更新」**（沿用 uid / 创建时间 / 延迟 / 测速 / 排序），**不会插出重复行**。
支持分享链接、base64 订阅正文、Clash YAML、sing-box JSON。

---

## 六、测速：`wait=true` 同步等结果

请求体：

```json
{ "uids": ["可选"], "scope": "all|current|enabled", "include_speed": false,
  "wait": true, "timeout_ms": 30000 }
```

- `wait` 省略 / `false`：**发起型**，立即返回 `{"started":true,"count":N,"uids":[...],"mode":"url|tcp|speed","wait":false}`。
- `wait: true`：**同步等到测完**再返回；

```json
{ "results": [ {"uid":"...","name":"...","server":"...","port":443,
                "latency_ms":88,"speed_bps":1024.0,"success":true} ],
  "count": 1, "kind": "latency", "waited_ms": 1234, "partial": false, "wait": true }
```

**等待机制**：结果由界面线程的 `QTimer`（300 ms 一轮）轮询收尾，**界面全程不卡**。
默认上限 30 秒（夹在 1–120 秒）；**超时也返回结果**，标 `partial: true`（有结果就返回，不死等）。
已有测试在跑会返回 `400 test_running`，先 `POST /test/stop` 或等它结束。

---

## 七、Clash / sing-box External Controller 兼容层

**同一个端口、同一套 token**（`Authorization: Bearer <token>`），只是路径不带 `/api/v1`。
启用接口服务即可用，**不需要额外开关**；写操作仍受 `api.write_enabled` 约束。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 自我介绍 |
| GET | `/version` | `{"version":"v1.6.4","premium":false,"meta":true}` |
| GET | `/configs` | 入口端口、`allow-lan`、TUN、核心状态 |
| GET | `/proxies` | 全部代理（含 `GLOBAL` 选择器 + `DIRECT` + `REJECT`） |
| GET | `/proxies/<name>` | 单个代理 |
| GET | `/proxies/<name>/delay?timeout=5000` | 测延迟，返回 `{"delay":毫秒}`（测不出为 `0`） |
| PUT | `/proxies/GLOBAL` | `{"name":"节点名"}` 切换，成功 **204** |
| GET | `/group/GLOBAL/delay?timeout=5000` | 整组延迟 `{"delays":{"名":毫秒}}` |
| GET | `/traffic` | `{"up":字节/秒,"down":字节/秒}` |
| GET | `/connections` | `{"connections":[],"downloadTotal":N,"uploadTotal":N}` |
| DELETE | `/connections` | 空操作（**本程序不维护连接表**），返回 204 |
| GET | `/rules` | `{"rules":[]}` |
| GET | `/memory` | `{"inuse":0,"oslimit":0}` |
| GET | `/providers/proxies` | `{"providers":{}}` |
| — | `PATCH /configs` | **不支持**，返回 400 并提示去界面改（不静默假装成功） |

`/proxies` 结构示例：

```json
{ "proxies": {
    "DIRECT":    {"name":"DIRECT","type":"Direct","udp":true,"history":[]},
    "REJECT":    {"name":"REJECT","type":"Reject","udp":true,"history":[]},
    "种子节点":  {"name":"种子节点","type":"Trojan","udp":true,
                "history":[{"time":"2026-10-09T02:00:00","delay":88}],
                "uid":"...","server":"seed.example.com","port":443},
    "GLOBAL":    {"name":"GLOBAL","type":"Selector","udp":true,
                "now":"种子节点","all":["种子节点","二号节点"],"history":[]}
}}
```

- `type` 由协议映射（trojan→Trojan / vmess→Vmess / shadowsocks→Shadowsocks / hysteria2→Hysteria2 …，未收录的显示 `Unknown`）。
- 代理对象额外带 `uid`，方便拿到之后再去调自有接口。
- **不含任何口令字段**。
- `GLOBAL.all` 只列真实节点：`DIRECT` / `REJECT` 不能当切换目标。
- yacd 等面板从浏览器跨域访问，服务已返回 CORS 头，并放行 `OPTIONS` 预检。

---

## 八、Python 包接口

```python
from pms.api import PmsApi

api = PmsApi(settings, repo)          # 与主程序同源，字段与 HTTP 完全一致
result = api.papi_status()
if result.pmsar_ok:
    print(result.pmsar_data)          # dict
else:
    print(result.pmsar_code, result.pmsar_message)
```

- 只读方法直接可用；写方法需要先 `api.papi_set_commander(commander)`（主窗口已装配）。
- 常用：`papi_health()` `papi_version()` `papi_status()` `papi_nodes(group, only_enabled)`
  `papi_node(uid)` `papi_groups()` `papi_traffic()` `papi_test_status()`
  `papi_export_links(group, base64)` `papi_export_backup()` `papi_export_clash(group)` `papi_export_singbox(group)`
- 写方法：`papi_core_start/stop/restart` `papi_switch_node` `papi_set_system_proxy` `papi_set_tun`
  `papi_node_add/update/delete` `papi_nodes_import` `papi_test_latency/speed/stop`

---

## 九、curl 速查

```bash
TOKEN=你的token
BASE=http://127.0.0.1:20811

# 健康检查
curl -H "Authorization: Bearer $TOKEN" $BASE/api/v1/health

# 节点列表（只看某分组）
curl -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/nodes?group=接口导入"

# 切到指定节点（需先勾选「允许写操作」）
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"uid":"<节点uid>"}' $BASE/api/v1/node/switch

# 发起延迟测试并等到结果
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"wait":true,"timeout_ms":20000}' $BASE/api/v1/test/latency

# 导入节点
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"text":"trojan://pwd@host:443#名字","group":"接口导入"}' $BASE/api/v1/nodes/import

# Clash 兼容层：看代理、测延迟、切换
curl -H "Authorization: Bearer $TOKEN" $BASE/proxies
curl -H "Authorization: Bearer $TOKEN" "$BASE/proxies/种子节点/delay?timeout=5000"
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"name":"种子节点"}' $BASE/proxies/GLOBAL
```

---

## 十、风险提示（务必阅读）

1. **导出内容含真实口令与 UUID**：`GET /api/v1/export/*` 不受写操作开关限制（用户拍板：导出属只读），
   **token 是唯一防线**。不要把 token 与导出内容外传，也不要把接口暴露到公网。
2. **写操作默认关闭**是刻意设计；开启后接口可执行启停核心、改系统代理、开 TUN、增删节点，**请确认监听地址是本机**。
3. **导入不抓订阅 URL**（只做文本 / 链接同步导入）：抓订阅要联网与进度条，不适合同步 HTTP 请求。
4. **测速 `wait=true` 最长 120 秒**：超过只返回已测到的部分结果（`partial: true`），客户端要能接受部分结果。
5. **接口服务不维护连接表**：`/connections` 只回累计流量，`DELETE /connections` 是空操作。
