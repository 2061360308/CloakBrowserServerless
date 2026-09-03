"""Python WS/HTTP 代理: 把真实浏览器的 CDP 控制面转发到外部。

仅依赖 websockets 一个第三方库(HTTP 探活/列表转发用标准库 urllib 实现)。
监听 0.0.0.0:<PROXY_PORT>(默认 9000), 后端为 127.0.0.1:<CDP_PORT> 上以
remote debugging 启动的 stealth Chromium。支持:

  - GET /start          -> 懒启动入口: 携带两个头 —— sessionID(FC HeaderField
                           亲和会话 ID, 1-64 字符、^[A-Za-z0-9_-]*$) +
                           X-Browser-Cfg(base64url JSON 浏览器配置); 服务端
                           启动/复用/切换对应浏览器后返回
                           {"status":"ready","ws": 稳定根地址}; 供后续直连
  - GET /json/version  -> 转发浏览器版本信息; webSocketDebuggerUrl 返回
                          无实例状态的稳定入口 ws(s)://<host>/(根路径,
                          由代理动态解析当前浏览器), 跨实例/重启均有效
  - GET /json/list     -> 转发页面/目标列表(重写 host, 保留 target uuid)
  - WS  /devtools/<type>/<id> -> 双向转发到浏览器同名 CDP WebSocket
  - WS  /(根路径)      -> 双向转发到 browser-level CDP WebSocket
                          (方便裸 ws 客户端直接连 ws://host:9000)
  - GET /              -> 健康检查(恒 200, 供 FC 探活)

浏览器生命周期: 不再于容器启动时预启动, 而是由首个 GET /start 按客户端
sessionID(配置在 X-Browser-Cfg 头)懒启动; WS 断开后进程保留复用, 由 FC
空闲回收实例兜底清理。
serve() 可传入一个 BrowserManager(gate) 提供 /start 能力与"浏览器是否已
创建"判定; gate 为 None 时行为与旧版一致(假定后端浏览器已由外部启动)。


用法:
    python -m app.ws_proxy                # 后端默认 127.0.0.1:9222
    CDP_PORT=9222 PROXY_PORT=9000 python -m app.ws_proxy
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
import urllib.request
from typing import Any, Optional
from urllib.parse import urlsplit

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.http11 import Response
from websockets.protocol import State

from app.browser import BrowserBusy

# 说明: 跟随 websockets 最新版(17.x)默认的 asyncio 实现:
# serve() 的 process_request 回调签名是 (connection, request); 返回
# None(继续 WS 握手) 或 Response(以该 HTTP 响应终止握手)。常用
# connection.respond(status, text) 构造纯文本响应后再改 headers。
# 连接对象上 request.path / request.headers 即请求路径与头;
# 连接是否已关闭用 state is State.OPEN / State.CLOSED 判断(旧 .closed 已移除)。

logger = logging.getLogger("cbapp.ws_proxy")

# 允许的"页面内"来源(devtools 前端), 用于 CSRF 防护判断
_TRUSTED_ORIGINS = {
    "devtools://devtools",
    "chrome-devtools://devtools",
    "http://localhost",
    "https://localhost",
}

_MAX_MSG_SIZE = 64 * 1024 * 1024  # CDP 大消息(如 DOM snapshot)需要 64MB 上限
_REQUEST_TIMEOUT = 2.0            # 后端 localhost 请求超时(秒)

# 客户端两个自定义头:
#  - sessionID: FC HeaderField 亲和会话 ID(1-64 字符 ^[A-Za-z0-9_-]*$),
#    平台对值长度/字符集有硬限制, 因此只放"会话身份"(客户端取配置的 sha256);
#  - X-Browser-Cfg: 完整浏览器配置(base64url JSON), 只在 GET /start 携带。
_SID_HEADER = "sessionid"          # 查找时统一小写
_CFG_HEADER = "x-browser-cfg"      # 查找时统一小写


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _backend_base(cdp_port: int) -> str:
    return f"http://127.0.0.1:{cdp_port}"


def _external_host(headers: Headers) -> str:
    """客户端视角的 host: 优先 X-Forwarded-Host(反代场景), 否则 Host 头。"""
    fwd = headers.get("X-Forwarded-Host")
    if fwd:
        return fwd
    host = headers.get("Host") or "localhost"
    return host.split(":")[0] if ":" in host and not host.startswith("[") else host


def _ws_scheme(headers: Headers) -> str:
    fwd_proto = (headers.get("X-Forwarded-Proto") or "").lower()
    return "wss" if "https" in fwd_proto else "ws"


def _origin_allowed(headers: Headers) -> bool:
    """CSRF 防护: 仅拦截"来自浏览器页面"的跨站 ws 连接。

    无 Origin(纯 CDP 客户端/脚本)一律放行; 对 loopback 访问保留
    devtools 来源白名单, 防止本地恶意网页劫持浏览器控制。
    """
    origin = headers.get("Origin")
    if not origin:
        return True
    if origin in _TRUSTED_ORIGINS:
        return True
    o = urlsplit(origin)
    # 同源(页面由代理自身提供)或非浏览器控制面 → 放行
    if o.hostname and o.hostname in ("localhost", "127.0.0.1"):
        return o.port in (None, env_int("PROXY_PORT", 9000))
    return True  # 公开端口场景: 外部客户端可直连, 无需 origin 白名单


def _browser_wait_timeout() -> float:
    """业务端点等待浏览器就绪的最长时间(秒), WAIT_BROWSER_TIMEOUT 可调。"""
    return env_float("WAIT_BROWSER_TIMEOUT", 180.0)


# ---------------------------------------------------------------------------
# 后端(localhost CDP)HTTP 访问: 标准库 urllib, 零第三方依赖
# ---------------------------------------------------------------------------
def _http_get_json_sync(url: str, timeout: float = _REQUEST_TIMEOUT) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


async def _fetch_json(cdp_port: int, path: str) -> Any:
    return await asyncio.to_thread(
        _http_get_json_sync, f"{_backend_base(cdp_port)}{path}", _REQUEST_TIMEOUT,
    )


async def _wait_browser_ready(cdp_port: int) -> bool:
    """等待浏览器 CDP 就绪(轮询后端 /json/version)。

    - 业务端点(/json/version, /json/list, /devtools/..., 根路径 WS)在调用
      前先经过这里: 浏览器未就绪时连接会"自动等待"(客户端无感), 超时才报错;
    - GET / 健康检查不走此函数(恒 200, 供 FC 探活), 只把就绪状态写入 body。

    浏览器运行中崩溃重启时, 守护循环会自动拉起新实例, 该函数会等到
    新实例就绪, 期间到达的连接同样自动等待。
    """
    timeout = _browser_wait_timeout()
    deadline = asyncio.get_running_loop().time() + timeout
    delay = 0.5
    warned = False
    while True:
        try:
            data = await asyncio.to_thread(
                _http_get_json_sync,
                f"{_backend_base(cdp_port)}/json/version",
                1.0,
            )
            if isinstance(data, dict) and data.get("webSocketDebuggerUrl"):
                return True
        except Exception:  # noqa: BLE001 - 就绪前连接拒绝/超时都属正常
            pass
        if not warned:
            logger.info(
                "浏览器尚未就绪: 本连接将自动等待(最长 %ss, "
                "可用 WAIT_BROWSER_TIMEOUT 调整)...", timeout,
            )
            warned = True
        if asyncio.get_running_loop().time() >= deadline:
            logger.warning("浏览器在 %ss 内未就绪, 返回 5xx/关闭", timeout)
            return False
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# HTTP 响应构造(新版 process_request 返回 Response 对象)
# ---------------------------------------------------------------------------
def _json_response(connection: ServerConnection, status: int,
                   payload: Any) -> Response:
    """connection.respond() 构造纯文本响应后把 Content-Type 改为 JSON。

    Content-Length / Connection 等由 websockets 发送时自动处理。
    """
    resp = connection.respond(status, json.dumps(payload))
    resp.headers["Content-Type"] = "application/json"
    return resp


def _text_response(connection: ServerConnection, status: int,
                   body: str = "") -> Response:
    return connection.respond(status, body)


def _rewrite_ws_url(headers: Headers, ws_url: str) -> str:
    """把后端返回的 ws://127.0.0.1:9222/... 重写为对外可达地址。

    保留 /devtools/<type>/<id> 路径, 供 /json/list 的页面 target 使用
    (DevTools 调试需要精确到实例内某个 target; 同实例内必然存在)。
    """
    tail = ws_url.split("/devtools/", 1)
    if len(tail) != 2:
        return ws_url
    return f"{_ws_scheme(headers)}://{_external_host(headers)}/devtools/{tail[1]}"


def _stable_browser_ws(headers: Headers) -> str:
    """浏览器级 CDP 稳定入口(根路径), 不含任何实例本地状态。

    代理收到根路径 WS 时动态解析当前浏览器真实的 webSocketDebuggerUrl
    (见 route_ws), 因此该地址与"具体实例/浏览器进程"解耦 —— FC 弹性
    扩容把请求调度到别的实例、或浏览器崩溃重启(uuid 每次随机)后依然
    有效, 消除"先取地址后连接"两步落在不同实例时的 uuid 失配。
    """
    return f"{_ws_scheme(headers)}://{_external_host(headers)}/"


def _header_value(headers: Headers, name: str) -> str | None:
    """大小写不敏感地读取 header(FC 网关可能改写 header 名大小写)。"""
    low = name.lower()
    for key, value in headers.items():
        if key.lower() == low:
            return value
    return None


def _decode_cfg_header(headers: Headers) -> tuple[dict, str | None]:
    """读取 X-Browser-Cfg(base64url JSON)为配置 dict。

    返回 (cfg, err): 头缺失 -> ({}, None)(= 服务端默认随机配置);
    解码失败/非对象 -> ({}, 错误提示)。
    """
    value = _header_value(headers, _CFG_HEADER)
    if value is None:
        return {}, None
    try:
        b = value.encode("ascii")
        raw = base64.urlsafe_b64decode(b + b"=" * (-len(b) % 4))
        cfg = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 任何解码失败一律视为非法配置头
        return {}, "X-Browser-Cfg 解码失败: 须为 base64url 编码的 JSON 对象"
    if not isinstance(cfg, dict):
        return {}, "X-Browser-Cfg 解码结果必须是 JSON 对象(dict)"
    return cfg, None


async def _version_payload(cdp_port: int, headers: Headers) -> dict:
    data = await _fetch_json(cdp_port, "/json/version")
    if data.get("webSocketDebuggerUrl"):
        # 返回稳定根路径而非带随机 uuid 的具体地址, 见 _stable_browser_ws
        data["webSocketDebuggerUrl"] = _stable_browser_ws(headers)
    return data


async def _list_payload(cdp_port: int, headers: Headers) -> list:
    data = await _fetch_json(cdp_port, "/json/list")
    for target in data:
        ws_url = target.get("webSocketDebuggerUrl")
        if ws_url:
            target["webSocketDebuggerUrl"] = _rewrite_ws_url(headers, ws_url)
    return data


async def _health_payload(cdp_port: int, headers: Headers,
                          gate: Any | None = None) -> dict:
    # 健康检查语义(适配阿里云 FC): GET / 只要服务存活即返回 200,
    # 浏览器就绪状态放入 body; FC 探活依赖 200 判定实例存活。
    body: dict[str, Any] = {
        "status": "ok",
        "service": "cloakbrowser ws proxy",
        "listen": f"0.0.0.0:{env_int('PROXY_PORT', 9000)}",
        "backend_cdp": f"127.0.0.1:{cdp_port}",
    }
    # 懒启动模式: 浏览器尚未创建时如实标注并给提示(仍返回 200, FC 探活不受影响)
    if gate is not None and not gate.chrome_exists():
        body["browser_status"] = "not_created"
        body["hint"] = ("GET /start 并携带 sessionID + X-Browser-Cfg 头"
                        "以按需启动浏览器")
        return body
    try:
        version = await _fetch_json(cdp_port, "/json/version")
        body["browser"] = version.get("Browser", "")
        body["browser_ws"] = _stable_browser_ws(headers)
        body["browser_status"] = "ready"
    except Exception as exc:  # noqa: BLE001
        body["browser_status"] = "starting"
        body["browser_error"] = str(exc)
    return body


# ---------------------------------------------------------------------------
# GET /start: 懒启动入口(读取 sessionID header, 启动/复用/切换浏览器)
# ---------------------------------------------------------------------------
async def _handle_start(
    cdp_port: int,
    connection: ServerConnection,
    request: Any,
    gate: Any,
    active_ws: int,
) -> Response:
    """按两个头确保浏览器就绪: sessionID(会话身份) + X-Browser-Cfg(配置)。

    sessionID 为 FC HeaderField 亲和会话 ID(1-64 字符 ^[A-Za-z0-9_-]*$),
    本身不含配置; 浏览器参数由 X-Browser-Cfg(base64url JSON)下发, 缺失时
    走服务端默认随机配置。

    成功返回 200 {"status":"ready","ws":<稳定根地址>,"seed":...,"browser":...};
    失败: 400(缺 sessionID / 配置头解码失败) / 409(浏览器被其他活跃会话占用) /
          503(启动超时或失败)。
    """
    sid = _header_value(request.headers, _SID_HEADER)
    if not sid:
        return _json_response(connection, 400, {
            "error": "missing sessionID header",
            "hint": "sessionID = FC HeaderField 亲和会话 ID(1-64 字符, "
                    "^[A-Za-z0-9_][A-Za-z0-9_-]*$); 浏览器配置请放 "
                    "X-Browser-Cfg header(base64url JSON)",
        })
    cfg, err = _decode_cfg_header(request.headers)
    if err is not None:
        return _json_response(connection, 400, {
            "error": err,
            "hint": "浏览器配置键: seed/timezone/locale/proxy/extra_args",
        })
    try:
        status = await gate.ensure(sid, cfg, active_ws)
    except BrowserBusy as exc:
        return _json_response(connection, 409, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        logger.error("浏览器按会话 %s 启动失败: %s", sid, exc)
        return _json_response(connection, 503, {
            "error": "browser startup failed", "detail": str(exc),
        })
    body: dict[str, Any] = dict(status)
    body["ws"] = _stable_browser_ws(request.headers)
    try:
        version = await _fetch_json(cdp_port, "/json/version")
        body["browser"] = version.get("Browser", "")
    except Exception:  # noqa: BLE001
        body["browser"] = ""
    logger.info("/start 完成: session=%s seed=%s, active_ws=%s",
                sid, body.get("seed"), active_ws)
    return _json_response(connection, 200, body)


# ---------------------------------------------------------------------------
# 非 WS HTTP 请求处理(新版 process_request 回调):
# 签名 (connection, request); 返回 None 继续 WS 握手,
# 返回 Response 对象则按 HTTP 响应返回。
# ---------------------------------------------------------------------------
async def _process_request(
    cdp_port: int, connection: ServerConnection, request: Any,
    gate: Any | None = None, active_ref: list[int] | None = None,
) -> Optional[Response]:
    headers: Headers = request.headers
    path = request.path
    is_upgrade = (headers.get("Upgrade") or "").lower() == "websocket"

    # GET / 健康检查: 恒 200(不等待浏览器, FC 探活/崩溃重启期间均存活)
    if path == "/" and not is_upgrade:
        payload = await _health_payload(cdp_port, headers, gate)
        return _json_response(connection, 200, payload)

    # GET /start: 懒启动入口(浏览器创建/复用/切换由 gate 完成, 内部自等就绪)
    if path in ("/start", "/start/"):
        if gate is None:
            return _json_response(connection, 501, {
                "error": "browser gate not configured",
                "hint": "由 supervisor 编排运行(本独立进程未接管浏览器)",
            })
        return await _handle_start(
            cdp_port, connection, request, gate,
            active_ref[0] if active_ref else 0,
        )

    # 业务端点/WS 升级: 懒启动模式下浏览器未创建则快速失败(避免空等超时);
    # 已创建则等待其就绪(启动中/崩溃重启期间连接自动等待, 客户端无感)
    if gate is not None and not gate.chrome_exists():
        return _json_response(connection, 503, {
            "error": "browser not created",
            "hint": "call GET /start with sessionID(+X-Browser-Cfg) headers first",
        })
    # 会话一致性防串台: HeaderField 亲和保证同会话落在同实例, 但扩容/切换
    # 的瞬间可能出现"别的会话请求误入本实例"。此时快速 409 让客户端重试
    # (换实例), 绝不拿本实例浏览器去服务别的会话。
    if gate is not None and gate.chrome_exists():
        cur = getattr(gate, "current_session_id", None)
        req_sid = _header_value(headers, _SID_HEADER)
        if cur and req_sid and req_sid != cur:
            return _json_response(connection, 409, {
                "error": f"session mismatch: 本实例正服务会话 {cur[:12]}…, "
                         f"请求属于 {req_sid[:12]}…",
                "hint": "HeaderField 亲和应避免跨会话路由; 请重试或重新 GET /start",
            })
    if not await _wait_browser_ready(cdp_port):
        return _json_response(connection, 503, {
            "error": "browser not ready after timeout",
            "hint": "retry later, or increase WAIT_BROWSER_TIMEOUT",
        })

    if is_upgrade:
        return None  # 进入 WS handler(route_ws 按 path 转发)

    try:
        # 兼容 Playwright connect_over_cdp: 它会自动请求 /json/version/(带尾斜杠)
        if path in ("/json/version", "/json/version/"):
            return _json_response(connection, 200,
                                  await _version_payload(cdp_port, headers))
        if path in ("/json/list", "/json/list/", "/json"):
            return _json_response(connection, 200,
                                  await _list_payload(cdp_port, headers))
        return _text_response(connection, 404)
    except Exception as exc:  # noqa: BLE001
        logger.warning("HTTP %s 转发失败: %s", path, exc)
        return _json_response(connection, 502, {
            "error": str(exc), "browser": "unreachable",
        })


# ---------------------------------------------------------------------------
# WebSocket 双向转发
# ---------------------------------------------------------------------------
async def _pump(src: ServerConnection, dst: ServerConnection) -> None:
    """单向泵送: src -> dst。连接关闭/异常时结束并向外抛。"""
    async for message in src:
        await dst.send(message)


async def _relay(client_ws: ServerConnection, target_url: str,
                 retries: int = 6) -> None:
    """把一条客户端 WebSocket 双向代理到 target_url。

    浏览器重启等短暂不可用场景: 连接后端前先轻量重试, 提升稳定性。
    """
    last_exc: Exception | None = None
    for _attempt in range(1, retries + 1):
        try:
            async with websockets.connect(
                target_url, max_size=None, ping_interval=None, ping_timeout=None,
            ) as backend_ws:
                logger.info("WS 已连接: %s -> %s",
                            client_ws.request.path, target_url)
                c2b = asyncio.create_task(_pump(client_ws, backend_ws))
                b2c = asyncio.create_task(_pump(backend_ws, client_ws))
                done, pending = await asyncio.wait(
                    {c2b, b2c}, return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc and not isinstance(exc, (asyncio.CancelledError,
                                                    ConnectionError)):
                        logger.debug("ws pump error: %s", exc)
                # 后端关闭(浏览器重启/断开) -> 关闭客户端, 让其重连
                if b2c in done and client_ws.state is State.OPEN:
                    try:
                        await client_ws.close(code=1011, reason="backend closed")
                    except ConnectionClosed:
                        pass
                return
        except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
            last_exc = exc
            await asyncio.sleep(0.5)
    logger.warning("无法连接后端 %s: %s", target_url, last_exc)
    if client_ws.state is State.OPEN:
        try:
            await client_ws.close(code=1011, reason="backend unreachable")
        except ConnectionClosed:
            pass


async def route_ws(cdp_port: int, client_ws: ServerConnection) -> None:
    """WS 升级请求的 handler: 按路径转发到浏览器同名 CDP WebSocket。"""
    headers = client_ws.request.headers
    if not _origin_allowed(headers):
        try:
            await client_ws.close(code=1008, reason="cross-origin blocked")
        except ConnectionClosed:
            pass
        return

    path = client_ws.request.path
    if path.startswith("/devtools/"):
        # 与浏览器同名端点保持一致(浏览器 target/页面 ws 均走同名路径)
        target = f"ws://127.0.0.1:{cdp_port}{path}"
    else:
        # 根路径等 -> browser-level CDP WebSocket
        try:
            version = await _fetch_json(cdp_port, "/json/version")
            target = version.get("webSocketDebuggerUrl")
        except Exception:  # noqa: BLE001
            target = None
        if not target:
            try:
                await client_ws.close(code=1013, reason="browser not ready")
            except ConnectionClosed:
                pass
            return
    await _relay(client_ws, target)


# ---------------------------------------------------------------------------
# 服务装配: websockets.serve(唯一监听入口, HTTP + WS 同端口)
# ---------------------------------------------------------------------------
async def serve(cdp_port: int, proxy_port: int, stop: asyncio.Event,
                gate: Any | None = None) -> None:
    """启动 0.0.0.0:<proxy_port> 服务, 直到 stop 事件被置位后优雅关闭。

    gate: 可选 BrowserManager。提供后启用 GET /start 懒启动入口,
    并对"浏览器未创建"的业务请求快速失败(不再空等)。
    """
    # 活跃 WS 会话计数(同事件循环内单线程增减; 供 /start 判断能否切换浏览器)
    active_ws: list[int] = [0]

    async def _ws_handler(client_ws: ServerConnection) -> None:
        active_ws[0] += 1
        try:
            await route_ws(cdp_port, client_ws)
        finally:
            active_ws[0] -= 1

    server = await websockets.serve(
        _ws_handler,
        "0.0.0.0", proxy_port,
        process_request=lambda connection, request: _process_request(
            cdp_port, connection, request, gate=gate, active_ref=active_ws),
        max_size=_MAX_MSG_SIZE,
        ping_interval=None,
        ping_timeout=None,
    )
    logger.info(
        "WS 代理已监听 0.0.0.0:%d -> 127.0.0.1:%d (CDP); gate=%s",
        proxy_port, cdp_port, "on" if gate is not None else "off",
    )
    await stop.wait()
    server.close()
    await server.wait_closed()
    logger.info("WS 代理已停止监听")


# ---------------------------------------------------------------------------
# 独立运行入口(本地调试/复用; 生产由 supervisor 编排)
# ---------------------------------------------------------------------------
def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cdp_port = env_int("CDP_PORT", 9222)
    proxy_port = env_int("PROXY_PORT", 9000)
    stop = asyncio.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await serve(cdp_port, proxy_port, stop)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("WS 代理已退出")


if __name__ == "__main__":
    _main()
