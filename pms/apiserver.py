#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 HTTP 接口服务（v1.6.0）

设计取舍（都为了「零新依赖 + 不碰界面线程」）：
    1. 用标准库 `http.server.ThreadingHTTPServer`，不引入 Flask / FastAPI，
       打包体积与依赖风险都不增加；
    2. 服务跑在**守护线程**里（`daemon=True` + `daemon_threads=True`），
       退出时不会拖住进程（v1.5.4 刚修过「线程池把进程拖住不退出」，同类坑不再犯）；
    3. 业务逻辑一律转发 `pms/api.py` 的门面，本文件只做 HTTP ↔ JSON 的转换；
    4. 结果通过 Qt 信号回主线程，**工作线程绝不碰界面**。

v1.8.0 事件推送（SSE）：
    `GET {PMS_API_PATH_PREFIX}/events` 返回 `text/event-stream`，服务端按 1 秒节奏增量推事件
    （`retry: 3000` + 15 秒心跳保活）；断线重连带 `?since=<最后收到的 seq>` 可接着推。
    响应体用 `Transfer-Encoding: chunked`，不写 Content-Length（长连接本来就没有确定长度）。
    服务停止时置 `pms_stop_event`，所有 SSE 线程 1 秒内醒过来收尾，不会拖住退出。

安全：
    - token 只存在 `.env`（`PMS_API_TOKEN`），由 `PmsEnvStore` 读写，不进设置文件与日志；
    - 非本机地址（如 0.0.0.0）**强制要求鉴权**，否则拒绝启动；
    - 口令比较用 `hmac.compare_digest`（防时序侧信道）。
"""

from __future__ import annotations

from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from typing import (
    Any,
    Callable,
    Optional,
)
from urllib.parse import (
    parse_qs,
    urlparse,
)
from PySide6.QtCore import (
    QObject,
    Signal,
)
import base64
import hmac
import json
import logging
import secrets
import threading
import time

from pms.constants import (
    PMS_API_AUTH_AUTO,
    PMS_API_AUTH_OFF,
    PMS_API_DEFAULT_HOST,
    PMS_API_DEFAULT_PORT,
    PMS_API_LOOPBACK_HOSTS,
    PMS_API_PATH_PREFIX,
    PMS_API_TOKEN_BYTES,
    PMS_API_TOKEN_ENV,
    PMS_APP_VERSION,
    PMS_LOGGER_NAME,
)

if True:  # 仅类型检查用，避免与设置 / 环境模块循环导入
    from pms.envstore import PmsEnvStore
    from pms.settings import PmsSettingsManager
    from pms.api import PmsApi
    from pms.apiclash import PmsClashCompat

from pms.webpanel import (  # noqa: F401
    pms_web_panel_disabled_html,
    pms_web_panel_html,
    pms_web_panel_path_match,
    pms_web_panel_scheme,
    pms_web_panel_url,
)


def pms_api_generate_token() -> str:
    """生成一个随机 token（32 位上下 urlsafe base64）；只生成不落盘。"""
    raw = secrets.token_bytes(int(PMS_API_TOKEN_BYTES))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# v1.8.0 SSE：推送节奏（秒）——轮询间隔 1 秒，15 秒没事件就发一个心跳注释保活
PMS_API_SSE_POLL_SECONDS = 1.0
PMS_API_SSE_HEARTBEAT_SECONDS = 15.0
PMS_API_SSE_RETRY_MS = 3000
# v1.9.54（审计表第 22 项）：重连时一次性最多补发多少条（环 + 落库窗口）。
# 上限与 `pms/api.py` 的 `papi_events_recent` 的 limit 口径一致（200），
# 免得一个「睡了三天」的客户端一次性被灌几万条事件。
PMS_API_SSE_CATCHUP_MAX = 200

# 写操作结果码 → HTTP 状态码（只读路径仍按 v1.6.0 一律 200，避免改坏已有行为）
PMS_API_STATUS_BY_CODE: dict[str, int] = {
    "": 200,
    "bad_request": 400,
    "unauthorized": 401,
    "write_disabled": 403,
    "not_found": 404,
    "node_not_found": 404,
    "timeout": 504,
}


# --------------------------------------------------------------------------------------
# HTTP 处理器（只做协议转换）
# --------------------------------------------------------------------------------------
class PmsApiRequestHandler(BaseHTTPRequestHandler):
    """把 HTTP 请求转成门面调用，再 JSON 回去。"""

    protocol_version = "HTTP/1.1"
    server_version = "PmsApiServer"
    sys_version = ""

    def _pms_send(self, pms_status: int, pms_payload: dict) -> None:
        status = int(pms_status)
        # 204（Clash 的切换成功）不能有响应体；其余一律 JSON
        body = b"" if status in (204, 304) or not pms_payload else json.dumps(pms_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # v1.6.4：Clash 面板（yacd 等）是浏览器页面，跨域访问必须给 CORS 头；
        # 与 Clash 官方控制器一致用 `*`，安全仍由 token 把关
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _pms_send_html(self, pms_status: int, pms_html: str) -> None:
        """回一张 HTML 页面（v1.9.35：本地 Web 面板）。

        面板页**不含任何用户数据**（数据全靠 `fetch` 走 `/api/v1/*`），所以这一路不需要 token——
        也让「面板页能不能打开」与「数据能不能看到」两件事分开。
        """
        body = str(pms_html or "").encode("utf-8")
        self.send_response(int(pms_status))
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _pms_read_body(self) -> tuple[Optional[dict], str]:
        """读请求体：没有体返回 `(None, "")`，解析失败返回 `(None, 原因)`。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return None, ""
        try:
            raw = self.rfile.read(length)
        except OSError:
            return None, "读取请求体失败"
        try:
            parsed = json.loads(raw.decode("utf-8", "ignore"))
        except ValueError:
            return None, "请求体不是合法 JSON"
        if not isinstance(parsed, dict):
            return None, "请求体必须是 JSON 对象（键值形式）"
        return parsed, ""

    def _pms_dispatch(self, pms_method: str) -> None:
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
        owner = getattr(self.server, "pms_owner", None)
        if owner is None:
            self._pms_send(500, {"ok": False, "error": {"code": "server_error", "message": "接口服务未初始化"}})
            return
        body, body_error = self._pms_read_body()
        status, payload = owner.pas_handle(parsed.path, query, dict(self.headers), pms_method, body, body_error)
        self._pms_send(status, payload)

    def do_GET(self) -> None:  # noqa: N802 - 标准库要求这个名
        # v1.8.0：事件流是长连接，走的不是「收请求 → 回 JSON」那套流程，先拦下来
        owner = getattr(self.server, "pms_owner", None)
        if owner is not None and owner.pas_is_events_path(str(self.path or "/")):
            self._pms_stream_events(owner)
            return
        # v1.9.35：本地 Web 面板同样是「不返回 JSON」的一路，也先拦下来
        if owner is not None and owner.pas_is_panel_path(str(self.path or "/")):
            status, page = owner.pas_handle_panel(str(self.path or "/"))
            self._pms_send_html(status, page)
            return
        self._pms_dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._pms_dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802 - v1.6.2 起开放写操作
        self._pms_dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._pms_dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._pms_dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802 - v1.6.4：浏览器跨域预检（Clash 面板要）
        self._pms_dispatch("OPTIONS")

    def _pms_chunk(self, pms_text: str) -> None:
        """按 chunked 编码写一帧；传空串表示写结束块（长连接收尾用）。"""
        if not pms_text:
            self.wfile.write(b"0\r\n\r\n")
        else:
            raw = str(pms_text).encode("utf-8")
            self.wfile.write(f"{len(raw):X}\r\n".encode("ascii") + raw + b"\r\n")
        self.wfile.flush()

    def _pms_stream_events(self, pms_owner: "PmsApiServer") -> None:
        """SSE 事件流（`GET .../events`）：有新事件就推，没事件发心跳，客户端断开就安静结束。

        鉴权与普通接口**完全一致**（同一个 token），没带 token 直接 401 而不是开着流让人白等。
        """
        allowed, reason = pms_owner.pas_check_auth(dict(self.headers))
        if not allowed:
            self._pms_send(401, {"ok": False, "error": {"code": "unauthorized", "message": reason}})
            return
        query = {key: values[0] for key, values in parse_qs(urlparse(str(self.path or "/")).query).items() if values}
        try:
            seq = int(str(query.get("since") or 0))
        except (TypeError, ValueError):
            seq = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")     # 反向代理别缓冲住长连接
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Transfer-Encoding", "chunked")  # 长连接没有确定长度，不能用 Content-Length
        self.end_headers()
        api = pms_owner.pas_api
        stop_event = getattr(self.server, "pms_stop_event", None)
        idle = 0.0
        try:
            self._pms_chunk(f"retry: {PMS_API_SSE_RETRY_MS}\n\n")
            self._pms_chunk("event: hello\ndata: " + json.dumps({"seq": api.papi_event_last_seq()}, ensure_ascii=False) + "\n\n")
            # v1.9.54（审计表第 22 项）：客户端带的 since 落后于内存环容量时，环里已经没有那些事件了。
            # 先按 since **补一轮**（环 + 落库合并窗口），再进常规轮询——否则断线超过 200 条就永久漏。
            #
            # ★ v1.9.55 修一个真 bug：上面这句在 `since=0`（全新客户端 / 面板首次连接）时会把
            # **落库里几千条历史事件一次性全灌给客户端**——真实环境里 `event.log` 会一直涨，
            # 新连上来的面板等于被倒一整套历史，`node.switched` 这类新事件被埋在上千帧后面，
            # 6 秒内根本读不到（probe_v180 的 SSE 断言就是这样超时的）。
            # 修法：只有 since 指向的确实是「环装不下的旧位置」时才补，并且补的量有上限；
            # since<=0 表示新客户端，**只认环**（与 v1.9.48 行为一致），不从落库倒历史。
            catchup = []
            if seq > 0:
                catchup = api.papi_events_window(seq, PMS_API_SSE_CATCHUP_MAX)
            for item in catchup:
                seq = max(seq, int(item.get("seq") or 0))
                self._pms_chunk(f"event: {item.get('kind')}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n")
            while True:
                events = api.papi_events_since(seq)
                for item in events:
                    seq = max(seq, int(item.get("seq") or 0))
                    self._pms_chunk(
                        f"event: {item.get('kind')}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                    )
                if events or catchup:
                    idle = 0.0
                    catchup = []
                else:
                    idle += PMS_API_SSE_POLL_SECONDS
                    if idle >= PMS_API_SSE_HEARTBEAT_SECONDS:
                        self._pms_chunk(": ping\n\n")
                        idle = 0.0
                if stop_event is not None:
                    if stop_event.wait(PMS_API_SSE_POLL_SECONDS):   # 服务要停了，立刻收尾
                        break
                else:
                    time.sleep(PMS_API_SSE_POLL_SECONDS)
        except (OSError, ValueError):
            pass    # 客户端断开 / 连接被重置：正常现象，不打错误日志
        except Exception as exc:  # noqa: BLE001 - 单条推送出错不能连累整个服务
            logging.getLogger(PMS_LOGGER_NAME + ".api").warning("事件推送中断：%s", exc)
        finally:
            try:
                self._pms_chunk("")
            except OSError:
                pass

    def log_message(self, pms_format: str, *pms_args: Any) -> None:
        """默认会往 stderr 打日志，这里改成走程序日志（DEBUG 级，不刷屏）。"""
        logging.getLogger(PMS_LOGGER_NAME + ".api").debug("HTTP %s", pms_format % pms_args)


class PmsApiHttpServer(ThreadingHTTPServer):
    """带属主的 HTTP 服务：`pms_owner` 指回 `PmsApiServer`。"""

    daemon_threads = True          # 请求线程随进程退出，绝不拖住退出流程
    allow_reuse_address = True

    def __init__(self, pms_address: tuple[str, int], pms_owner: "PmsApiServer") -> None:
        super().__init__(pms_address, PmsApiRequestHandler)
        self.pms_owner = pms_owner
        self.pms_stop_event = threading.Event()   # v1.8.0：服务停止时唤醒所有 SSE 长连接


# --------------------------------------------------------------------------------------
# 服务：起停 / 鉴权 / 路由
# --------------------------------------------------------------------------------------
class PmsApiServer(QObject):
    """本地 HTTP 接口服务（起停、鉴权、路由转发）。"""

    pas_started = Signal(str)      # 已启动（带可访问地址）
    pas_stopped = Signal(str)      # 已停止（原因）
    pas_failed = Signal(str)       # 启动失败（原因，进状态栏与日志）

    def __init__(
        self,
        pms_settings: "PmsSettingsManager",
        pms_env: "PmsEnvStore",
        pms_api: "PmsApi",
        pms_parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(pms_parent)
        self.pas_settings = pms_settings
        self.pas_env = pms_env
        self.pas_api = pms_api
        self.pas_clash: Optional["PmsClashCompat"] = None     # v1.6.4：Clash 兼容层（可为空）
        self.pas_logger = logging.getLogger(PMS_LOGGER_NAME + ".apiserver")
        self.pas_httpd: Optional[PmsApiHttpServer] = None
        self.pas_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ 配置
    def pas_is_running(self) -> bool:
        return self.pas_httpd is not None

    def pas_host(self) -> str:
        return str(self.pas_settings.psm_get_str("api.host", PMS_API_DEFAULT_HOST) or PMS_API_DEFAULT_HOST).strip()

    def pas_port(self) -> int:
        return int(self.pas_settings.psm_get_int("api.port", PMS_API_DEFAULT_PORT))

    def pas_auth_mode(self) -> str:
        return str(self.pas_settings.psm_get_str("api.auth_mode", PMS_API_AUTH_AUTO) or PMS_API_AUTH_AUTO)

    def pas_is_enabled(self) -> bool:
        return bool(self.pas_settings.psm_get_bool("api.enabled", False))

    def pas_base_url(self) -> str:
        return f"http://{self.pas_host()}:{self.pas_port()}{PMS_API_PATH_PREFIX}"

    def pas_set_clash_compat(self, pms_compat: "PmsClashCompat") -> None:
        """挂上 Clash / sing-box External Controller 兼容层（v1.6.4，主窗口装配一次）。"""
        self.pas_clash = pms_compat

    def pas_is_loopback(self) -> bool:
        host = self.pas_host().lower()
        return host in PMS_API_LOOPBACK_HOSTS

    def pas_is_events_path(self, pms_path: str) -> bool:
        """是不是 SSE 事件流路径（`GET /api/v1/events`，v1.8.0）。

        它不能走 `pas_handle` 那套「收请求 → 回 JSON」流程（那是长连接），
        所以由 `do_GET` 先用本方法拦一道；路径不匹配就照旧当作普通路由处理。
        """
        path = str(pms_path or "/")
        if not path.startswith(PMS_API_PATH_PREFIX):
            return False
        return path[len(PMS_API_PATH_PREFIX):].strip("/").split("?")[0] == "events"

    def pas_is_panel_path(self, pms_path: str) -> bool:
        """是不是本地 Web 面板的路径（v1.9.35，`/panel`）。

        与 SSE 一样，它不走 `pas_handle` 那套「收请求 → 回 JSON」，由 `do_GET` 先拦一道。
        ★ 不能用 `/` 做面板：根路径已经被 Clash 兼容层占着（mihomo 客户端会访问它做健康检查）。
        """
        return pms_web_panel_path_match(pms_path)

    def pas_panel_enabled(self) -> bool:
        return bool(self.pas_settings.psm_get_bool("api.web_panel_enabled", True))

    def pas_panel_url(self) -> str:
        return pms_web_panel_url(self.pas_host(), self.pas_port())

    def pas_panel_scheme(self) -> str:
        """面板配色方案（v1.9.39）：默认「原生（系统默认）」→ `auto`，交给浏览器按系统配色；
        用户在设置里明确选了浅色 / 深色就跟程序走。"""
        return pms_web_panel_scheme(self.pas_settings.psm_get_str("ui.theme", ""))

    def pas_handle_panel(self, pms_path: str) -> tuple[int, str]:
        """面板页（**不含数据，所以不需要 token**）；关掉了就给一张人话说明页。"""
        if not self.pas_panel_enabled():
            return 404, pms_web_panel_disabled_html(self.pas_panel_scheme())
        write_enabled = bool(self.pas_settings.psm_get_bool("api.write_enabled", False))
        auth_needed = not (self.pas_auth_mode() == PMS_API_AUTH_OFF and self.pas_is_loopback())
        return 200, pms_web_panel_html(PMS_APP_VERSION, write_enabled, auth_needed, self.pas_panel_scheme())

    # ------------------------------------------------------------------ token
    def pas_generate_token(self) -> str:
        """生成一个新的随机 token（不落盘，落盘由调用方决定）。"""
        return pms_api_generate_token()

    def pas_token(self) -> str:
        """取当前 token；自动生成模式下若 .env 没有就生成并写入（**值不进日志**）。"""
        if self.pas_auth_mode() == PMS_API_AUTH_OFF:
            return ""
        token = str(self.pas_env.pes_get(PMS_API_TOKEN_ENV, "") or "").strip()
        if token:
            return token
        if self.pas_auth_mode() == PMS_API_AUTH_AUTO:
            token = self.pas_generate_token()
            if self.pas_env.pes_set(PMS_API_TOKEN_ENV, token):
                self.pas_logger.info("已自动生成接口 token 并写入 .env（%s，值不打印）", PMS_API_TOKEN_ENV)
            return token
        return ""

    def pas_regenerate_token(self) -> str:
        """强制换一个 token 并写入 .env；返回新值（界面显示用，**不写日志**）。"""
        token = self.pas_generate_token()
        self.pas_env.pes_set(PMS_API_TOKEN_ENV, token)
        return token

    def pas_expected_token(self) -> str:
        return self.pas_token()

    # ------------------------------------------------------------------ 起停
    def pas_start(self) -> bool:
        """按当前设置启动；已启动就先停再起。失败时发 `pas_failed` 并返回 False。"""
        self.pas_stop("重启接口服务")
        if not self.pas_is_enabled():
            return False
        if not self.pas_is_loopback() and self.pas_auth_mode() == PMS_API_AUTH_OFF:
            message = f"监听地址 {self.pas_host()} 不是本机地址，必须开启鉴权才能启动接口服务"
            self.pas_logger.warning(message)
            self.pas_failed.emit(message)
            return False
        host = self.pas_host()
        port = self.pas_port()
        try:
            httpd = PmsApiHttpServer((host, port), self)
        except OSError as exc:
            message = f"接口服务启动失败：{host}:{port} 无法监听（{exc}）——换个端口或检查是否被占用"
            self.pas_logger.warning(message)
            self.pas_failed.emit(message)
            return False
        self.pas_httpd = httpd
        self.pas_thread = threading.Thread(target=httpd.serve_forever, name="PmsApiServer", daemon=True)
        self.pas_thread.start()
        url = self.pas_base_url()
        auth_text = "关闭鉴权" if self.pas_auth_mode() == PMS_API_AUTH_OFF else f"鉴权={self.pas_auth_mode()}"
        self.pas_logger.info("接口服务已启动：%s（%s）", url, auth_text)
        self.pas_started.emit(url)
        return True

    def pas_stop(self, pms_reason: str = "停止") -> None:
        """停止服务并等线程收尾（最多 3 秒，超时不死等）。"""
        httpd = self.pas_httpd
        thread = self.pas_thread
        self.pas_httpd = None
        self.pas_thread = None
        if httpd is None:
            return
        try:
            stop_event = getattr(httpd, "pms_stop_event", None)
            if stop_event is not None:
                stop_event.set()          # v1.8.0：先叫醒 SSE 长连接，再关监听
            httpd.shutdown()
            httpd.server_close()
        except OSError as exc:
            self.pas_logger.warning("关闭接口服务时出现异常：%s", exc)
        if thread is not None and thread.is_alive():
            thread.join(3.0)
        self.pas_logger.info("接口服务已停止（%s）", pms_reason)
        self.pas_stopped.emit(pms_reason)

    def pas_apply_settings(self) -> None:
        """设置变更后调用：关就停、开就按新设置起。"""
        if not self.pas_is_enabled():
            self.pas_stop("接口已关闭")
            return
        self.pas_start()

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def pas_parse_bool(pms_value: Any) -> Optional[bool]:
        """把请求体里的开关值解析成 bool；认不出来（含缺失）返回 None。"""
        if isinstance(pms_value, bool):
            return pms_value
        text = str(pms_value or "").strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        return None

    # ------------------------------------------------------------------ 鉴权与路由
    def pas_check_auth(self, pms_headers: dict) -> tuple[bool, str]:
        """校验请求头里的 token；返回 (是否通过, 失败原因)。"""
        if self.pas_auth_mode() == PMS_API_AUTH_OFF:
            return True, ""
        expected = self.pas_expected_token()
        if not expected:
            return False, "服务端尚未配置 token：请到「设置 → 接口」生成或填写"
        # HTTP 头名大小写不保证（urllib 送的是 X-Pms-Token，curl 送的是 X-PMS-Token），
        # 因此统一按小写键比对，否则会出现「明明带了 token 却 401」
        lowered = {str(key).lower(): value for key, value in dict(pms_headers or {}).items()}
        presented = ""
        raw_authorization = str(lowered.get("authorization") or "").strip()
        if raw_authorization.lower().startswith("bearer "):
            presented = raw_authorization[7:].strip()
        if not presented:
            presented = str(lowered.get("x-pms-token") or "").strip()
        # 注意：compare_digest 只接受纯 ASCII 的 str，遇到中文等字符会抛 TypeError，
        # 所以统一按字节比较（客户端送什么都行，不会把请求线程打崩）
        presented_bytes = presented.encode("utf-8", "ignore")
        expected_bytes = expected.encode("utf-8", "ignore")
        if presented_bytes and hmac.compare_digest(presented_bytes, expected_bytes):
            return True, ""
        return False, "token 不正确或缺失（用 Authorization: Bearer <token> 或 X-PMS-Token 头）"

    def pas_handle(
        self,
        pms_path: str,
        pms_query: Optional[dict] = None,
        pms_headers: Optional[dict] = None,
        pms_method: str = "GET",
        pms_body: Optional[dict] = None,
        pms_body_error: str = "",
    ) -> tuple[int, dict]:
        """路由与应答（**与 HTTP 解耦**，探针可以不启服务直接测）。

        v1.6.4：路径不再只有 `/api/v1`——**不带前缀且命中 Clash 兼容层的路径**交给
        `pms/apiclash.py`；两套共用同一个端口与同一套 token 鉴权。
        """
        path = str(pms_path or "/")
        query = pms_query or {}
        headers = {str(key): value for key, value in dict(pms_headers or {}).items()}
        method = str(pms_method or "GET").upper()
        if method == "OPTIONS":
            return 204, {}      # 跨域预检不带 token，必须放行
        if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            return 405, {"ok": False, "error": {"code": "method_not_allowed", "message": f"不支持的请求方法：{method}"}}
        # 先定归属（决定 404），再鉴权——顺序反了会把「路径不存在」报成 401
        compat = self.pas_clash
        if path.startswith(PMS_API_PATH_PREFIX):
            use_clash = False
        elif compat is not None and compat.pcl_is_clash_path(path):
            use_clash = True
        else:
            prefix = PMS_API_PATH_PREFIX
            return 404, {"ok": False, "error": {"code": "not_found", "message": f"路径不存在：{path}（接口前缀是 {prefix}）"}}
        allowed, reason = self.pas_check_auth(headers)
        if not allowed:
            if use_clash and compat is not None:
                return compat.pcl_error(401, reason, "unauthorized")     # pcl_error 本身已带状态码
            return 401, {"ok": False, "error": {"code": "unauthorized", "message": reason}}
        if use_clash and compat is not None:
            return compat.pcl_handle(path, query, method, pms_body, pms_body_error)
        tail = path[len(PMS_API_PATH_PREFIX):].strip("/")
        api = self.pas_api
        if method in ("GET", "HEAD"):
            return self.pas_handle_read(api, tail, query)
        return self.pas_handle_write(api, tail, method, pms_body, pms_body_error)

    # ------------------------------------------------------------------ 只读路由
    def pas_handle_read(self, pms_api: "PmsApi", pms_tail: str, pms_query: dict) -> tuple[int, dict]:
        """只读路由（v1.6.0 起的那批，行为保持不变）。"""
        try:
            if pms_tail in ("", "health", "ping"):
                result = pms_api.papi_health()
            elif pms_tail == "status":
                result = pms_api.papi_status()
            elif pms_tail == "version":
                result = pms_api.papi_version()
            elif pms_tail == "groups":
                result = pms_api.papi_groups()
            elif pms_tail == "traffic":
                result = pms_api.papi_traffic()
            elif pms_tail == "test/status":            # v1.6.3：测速是否在跑（只读）
                result = pms_api.papi_test_status()
            elif pms_tail == "events/recent":          # v1.9.35：最近事件（给本地 Web 面板轮询；只读）
                try:
                    since = int(str(pms_query.get("since") or 0))
                except (TypeError, ValueError):
                    since = 0
                try:
                    limit = int(str(pms_query.get("limit") or 40))
                except (TypeError, ValueError):
                    limit = 40
                result = pms_api.papi_events_recent(since, limit)
            elif pms_tail == "export/links":           # v1.6.3：导出分享链接 / base64（只读）
                base64_only = str(pms_query.get("base64") or "").strip().lower() in ("1", "true", "yes")
                result = pms_api.papi_export_links(str(pms_query.get("group") or "").strip(), base64_only)
            elif pms_tail == "export/backup":          # v1.6.3：导出完整备份 JSON（只读）
                result = pms_api.papi_export_backup()
            elif pms_tail == "export/clash":           # v1.6.3：导出 Clash YAML（只读）
                result = pms_api.papi_export_clash(str(pms_query.get("group") or "").strip())
            elif pms_tail == "export/singbox":         # v1.6.3：导出 sing-box JSON（只读）
                result = pms_api.papi_export_singbox(str(pms_query.get("group") or "").strip())
            elif pms_tail == "nodes":
                group = str(pms_query.get("group") or "").strip()
                only_enabled = str(pms_query.get("enabled") or "").strip().lower() in ("1", "true", "yes")
                result = pms_api.papi_nodes(group, only_enabled)
            elif pms_tail.startswith("nodes/"):
                result = pms_api.papi_node(pms_tail[len("nodes/"):])
            else:
                return 404, {"ok": False, "error": {"code": "not_found", "message": f"没有这个接口：{pms_tail}"}}
        except Exception as exc:  # noqa: BLE001 - 单个请求出错不能连累整个服务
            self.pas_logger.exception("处理接口请求出错（%s）：%s", pms_tail, exc)
            return 500, {"ok": False, "error": {"code": "server_error", "message": f"处理请求失败：{exc}"}}
        return 200, result.pmsar_to_dict()

    # ------------------------------------------------------------------ 写操作路由
    def pas_handle_write(
        self,
        pms_api: "PmsApi",
        pms_tail: str,
        pms_method: str,
        pms_body: Optional[dict],
        pms_body_error: str,
    ) -> tuple[int, dict]:
        """写操作路由（v1.6.2）：先定路由，再交给门面派发到界面线程。

        顺序很关键：**先判「这个方法 + 路径存不存在」再判「写操作开关」**，
        否则 POST 到只读路径会返回 403 而不是 405，语义就错了。
        """
        if str(pms_body_error or ""):
            return 400, {"ok": False, "error": {"code": "bad_request", "message": str(pms_body_error)}}
        body = dict(pms_body or {})
        method = str(pms_method or "").upper()
        tail = str(pms_tail or "")
        handler: Optional[Callable[[], Any]] = None
        if method == "POST" and tail == "core/start":
            handler = lambda: pms_api.papi_core_start(str(body.get("uid") or "").strip())
        elif method == "POST" and tail == "core/stop":
            handler = lambda: pms_api.papi_core_stop()
        elif method == "POST" and tail == "core/restart":
            handler = lambda: pms_api.papi_core_restart(str(body.get("uid") or "").strip())
        elif method == "POST" and tail == "node/switch":
            handler = lambda: pms_api.papi_switch_node(str(body.get("uid") or "").strip())
        elif tail == "system-proxy" and method in ("POST", "PUT"):
            handler = lambda: pms_api.papi_set_system_proxy(self.pas_parse_bool(body.get("enabled")))
        elif tail == "tun" and method in ("POST", "PUT"):
            handler = lambda: pms_api.papi_set_tun(self.pas_parse_bool(body.get("enabled")))
        elif method == "POST" and tail == "nodes":
            handler = lambda: pms_api.papi_node_add(body)
        elif method == "PUT" and tail.startswith("nodes/"):
            handler = lambda: pms_api.papi_node_update(tail[len("nodes/"):], body)
        elif method == "DELETE" and tail.startswith("nodes/"):
            handler = lambda: pms_api.papi_node_delete(tail[len("nodes/"):])
        elif method == "POST" and tail == "test/latency":
            handler = lambda: pms_api.papi_test_latency(
                body.get("uids") if isinstance(body.get("uids"), list) else None,
                str(body.get("scope") or "all"),
                bool(body.get("include_speed")),
                str(body.get("wait") or "").strip().lower() in ("1", "true", "yes"),
                int(body.get("timeout_ms") or 0) if str(body.get("timeout_ms") or "0").isdigit() else 0,
            )
        elif method == "POST" and tail == "test/speed":
            handler = lambda: pms_api.papi_test_speed(
                body.get("uids") if isinstance(body.get("uids"), list) else None,
                str(body.get("scope") or "all"),
                str(body.get("wait") or "").strip().lower() in ("1", "true", "yes"),
                int(body.get("timeout_ms") or 0) if str(body.get("timeout_ms") or "0").isdigit() else 0,
            )
        elif method == "POST" and tail == "test/stop":
            handler = lambda: pms_api.papi_test_stop()
        elif method == "POST" and tail == "test/resume":  # v1.8.0 断点续跑
            handler = lambda: pms_api.papi_test_resume()
        elif method == "POST" and tail == "nodes/import":
            handler = lambda: pms_api.papi_nodes_import(body)
        if handler is None:
            return 405, {"ok": False, "error": {"code": "method_not_allowed", "message": f"{tail or '根路径'} 不支持 {method} 方法"}}
        try:
            result = handler()
        except Exception as exc:  # noqa: BLE001 - 单个请求出错不能连累整个服务
            self.pas_logger.exception("处理写操作请求出错（%s %s）：%s", method, tail, exc)
            return 500, {"ok": False, "error": {"code": "server_error", "message": f"处理请求失败：{exc}"}}
        status = PMS_API_STATUS_BY_CODE.get(result.pmsar_code, 400)
        return status, result.pmsar_to_dict()
