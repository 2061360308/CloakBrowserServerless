"""Python WS/HTTP 代理: 把真实浏览器的 CDP 控制面转发到外部。

仅依赖 websockets 一个第三方库(HTTP 探活/列表转发用标准库 urllib 实现)。
监听 0.0.0.0:<PROXY_PORT>(默认 9000), 后端为 127.0.0.1:<CDP_PORT> 上以
remote debugging 启动的 stealth Chromium。支持:

  - GET /json/version  -> 转发浏览器版本信息, 并把 webSocketDebuggerUrl
                          的 host 重写为客户端视角的地址
  - GET /json/list     -> 转发页面/目标列表(同样重写)
  - WS  /devtools/<type>/<id> -> 双向转发到浏览器同名 CDP WebSocket
  - WS  /(根路径)      -> 双向转发到 browser-level CDP WebSocket
                          (方便裸 ws 客户端直接连 ws://host:9000)
  - GET /              -> 健康检查(恒 200, 供 FC 探活)

用法:
    python -m app.ws_proxy                # 后端默认 127.0.0.1:9222
    CDP_PORT=9222 PROXY_PORT=9000 python -m app.ws_proxy
"""
from __future__ import annotations

import asyncio
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
    """把后端返回的 ws://127.0.0.1:9222/... 重写为对外可达地址。"""
    tail = ws_url.split("/devtools/", 1)
    if len(tail) != 2:
        return ws_url
    return f"{_ws_scheme(headers)}://{_external_host(headers)}/devtools/{tail[1]}"


async def _version_payload(cdp_port: int, headers: Headers) -> dict:
    data = await _fetch_json(cdp_port, "/json/version")
    ws_url = data.get("webSocketDebuggerUrl", "")
    if ws_url:
        data["webSocketDebuggerUrl"] = _rewrite_ws_url(headers, ws_url)
    return data


async def _list_payload(cdp_port: int, headers: Headers) -> list:
    data = await _fetch_json(cdp_port, "/json/list")
    for target in data:
        ws_url = target.get("webSocketDebuggerUrl")
        if ws_url:
            target["webSocketDebuggerUrl"] = _rewrite_ws_url(headers, ws_url)
    return data


async def _health_payload(cdp_port: int, headers: Headers) -> dict:
    # 健康检查语义(适配阿里云 FC): GET / 只要服务存活即返回 200,
    # 浏览器就绪状态放入 body; FC 探活依赖 200 判定实例存活。
    body: dict[str, Any] = {
        "status": "ok",
        "service": "cloakbrowser ws proxy",
        "listen": f"0.0.0.0:{env_int('PROXY_PORT', 9000)}",
        "backend_cdp": f"127.0.0.1:{cdp_port}",
    }
    try:
        version = await _fetch_json(cdp_port, "/json/version")
        body["browser"] = version.get("Browser", "")
        body["browser_ws"] = _rewrite_ws_url(
            headers, version.get("webSocketDebuggerUrl", ""))
        body["browser_status"] = "ready"
    except Exception as exc:  # noqa: BLE001
        body["browser_status"] = "starting"
        body["browser_error"] = str(exc)
    return body


# ---------------------------------------------------------------------------
# 非 WS HTTP 请求处理(新版 process_request 回调):
# 签名 (connection, request); 返回 None 继续 WS 握手,
# 返回 Response 对象则按 HTTP 响应返回。
# ---------------------------------------------------------------------------
async def _process_request(
    cdp_port: int, connection: ServerConnection, request: Any,
) -> Optional[Response]:
    headers: Headers = request.headers
    path = request.path
    is_upgrade = (headers.get("Upgrade") or "").lower() == "websocket"

    # GET / 健康检查: 恒 200(不等待浏览器, FC 探活/崩溃重启期间均存活)
    if path == "/" and not is_upgrade:
        payload = await _health_payload(cdp_port, headers)
        return _json_response(connection, 200, payload)

    # 其余请求(业务端点/WS 升级)在浏览器就绪前自动等待
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
async def serve(cdp_port: int, proxy_port: int, stop: asyncio.Event) -> None:
    """启动 0.0.0.0:<proxy_port> 服务, 直到 stop 事件被置位后优雅关闭。"""
    server = await websockets.serve(
        lambda ws: route_ws(cdp_port, ws),
        "0.0.0.0", proxy_port,
        process_request=lambda connection, request: _process_request(
            cdp_port, connection, request),
        max_size=_MAX_MSG_SIZE,
        ping_interval=None,
        ping_timeout=None,
    )
    logger.info(
        "WS 代理已监听 0.0.0.0:%d -> 127.0.0.1:%d (CDP)", proxy_port, cdp_port,
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
