"""Python WS/HTTP 代理: 把真实浏览器的 CDP 控制面转发到外部。

监听 0.0.0.0:<PROXY_PORT>(默认 9000), 后端为 127.0.0.1:<CDP_PORT> 上
以 remote debugging 启动的 stealth Chromium。支持:

  - GET /json/version  -> 转发浏览器版本信息, 并把 webSocketDebuggerUrl
                          的 host 重写为客户端视角的地址
  - GET /json/list     -> 转发页面/目标列表(同样重写)
  - WS  /devtools/<type>/<id> -> 双向转发到浏览器同名 CDP WebSocket
  - WS  /(根路径)      -> 双向转发到 browser-level CDP WebSocket
                          (方便裸 ws 客户端直接连 ws://host:9000)
  - GET /              -> 健康检查

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
import time
from urllib.parse import urlsplit

import aiohttp
import websockets
from aiohttp import web
from websockets.exceptions import WebSocketException

logger = logging.getLogger("cbapp.ws_proxy")

# 允许的"页面内"来源(devtools 前端), 用于 CSRF 防护判断
_TRUSTED_ORIGINS = {
    "devtools://devtools",
    "chrome-devtools://devtools",
    "http://localhost",
    "https://localhost",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _external_host(request: web.Request) -> str:
    """客户端视角的 host: 优先 X-Forwarded-Host(反代场景), 否则 Host 头。"""
    fwd = request.headers.get("X-Forwarded-Host")
    if fwd:
        return fwd
    host = request.host or request.url.host
    return host if host else "localhost"


def _ws_scheme(request: web.Request) -> str:
    fwd_proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
    scheme = request.scheme or "http"
    return "wss" if ("https" in fwd_proto or scheme == "https") else "ws"


def _origin_allowed(request: web.Request) -> bool:
    """CSRF 防护: 仅拦截"来自浏览器页面"的跨站 ws 连接。

    无 Origin(纯 CDP 客户端/脚本)一律放行; 对 loopback 访问保留
    devtools 来源白名单, 防止本地恶意网页劫持浏览器控制。
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True
    if origin in _TRUSTED_ORIGINS:
        return True
    o = urlsplit(origin)
    # 同源(页面由代理自身提供)或非浏览器控制面 → 放行
    if o.hostname and o.hostname in ("localhost", "127.0.0.1"):
        return o.port in (None, env_int("PROXY_PORT", 9000))
    return True  # 公开端口场景: 外部客户端可直连, 无需 origin 白名单


def _backend_base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _browser_wait_timeout() -> float:
    """业务端点等待浏览器就绪的最长时间(秒), WAIT_BROWSER_TIMEOUT 可调。"""
    try:
        return float(os.environ.get("WAIT_BROWSER_TIMEOUT", "180"))
    except ValueError:
        return 180.0


async def _wait_browser_ready(cdp_port: int) -> bool:
    """等待浏览器 CDP 就绪(轮询后端 /json/version)。

    - 业务端点(/json/version, /json/list, /devtools/..., 根路径 WS)在调用
      前先经过这里: 浏览器未就绪时连接会"自动等待"(客户端无感), 超时才报错;
    - GET / 健康检查不走此函数(恒 200, 供 FC 探活), 只把就绪状态写入 body。

    浏览器运行中崩溃重启时, 守护循环会自动拉起新实例, 该函数会等到
    新实例就绪, 期间到达的连接同样自动等待。
    """
    timeout = _browser_wait_timeout()
    deadline = time.monotonic() + timeout
    delay = 0.5
    warned = False
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    f"{_backend_base(cdp_port)}/json/version",
                    timeout=aiohttp.ClientTimeout(total=1.0),
                ) as resp:
                    if resp.status == 200:
                        return True
            except Exception:  # noqa: BLE001 - 就绪前连接拒绝/超时都属正常
                pass
            if not warned:
                logger.info(
                    "浏览器尚未就绪: 本连接将自动等待(最长 %ss, "
                    "可用 WAIT_BROWSER_TIMEOUT 调整)...", timeout,
                )
                warned = True
            if time.monotonic() >= deadline:
                logger.warning("浏览器在 %ss 内未就绪, 返回 502", timeout)
                return False
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
async def _fetch_json(session: aiohttp.ClientSession, port: int, path: str) -> dict:
    async with session.get(f"{_backend_base(port)}{path}") as resp:
        resp.raise_for_status()
        return await resp.json()


def _rewrite_ws_url(request: web.Request, ws_url: str) -> str:
    """把后端返回的 ws://127.0.0.1:9222/... 重写为对外可达地址。"""
    tail = ws_url.split("/devtools/", 1)
    if len(tail) != 2:
        return ws_url
    return f"{_ws_scheme(request)}://{_external_host(request)}/devtools/{tail[1]}"


async def handle_version(request: web.Request, cdp_port: int) -> web.Response:
    if not await _wait_browser_ready(cdp_port):
        return web.json_response(
            {"error": "browser not ready after timeout",
             "browser": "starting",
             "hint": "retry later, or increase WAIT_BROWSER_TIMEOUT"},
            status=502)
    try:
        async with aiohttp.ClientSession() as session:
            data = await _fetch_json(session, cdp_port, "/json/version")
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc), "browser": "unreachable"},
                                 status=502)
    ws_url = data.get("webSocketDebuggerUrl", "")
    if ws_url:
        data["webSocketDebuggerUrl"] = _rewrite_ws_url(request, ws_url)
    return web.json_response(data)


async def handle_list(request: web.Request, cdp_port: int) -> web.Response:
    if not await _wait_browser_ready(cdp_port):
        return web.json_response(
            {"error": "browser not ready after timeout", "targets": []},
            status=502)
    try:
        async with aiohttp.ClientSession() as session:
            data = await _fetch_json(session, cdp_port, "/json/list")
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc), "targets": []}, status=502)
    for target in data:
        ws_url = target.get("webSocketDebuggerUrl")
        if ws_url:
            target["webSocketDebuggerUrl"] = _rewrite_ws_url(request, ws_url)
    return web.json_response(data)


async def handle_health(request: web.Request, cdp_port: int) -> web.Response:
    # 健康检查语义(适配阿里云 FC): GET / 只要服务存活即返回 200,
    # 浏览器就绪状态放入 body; FC 探活依赖 200 判定实例存活。
    body = {
        "status": "ok",
        "service": "cloakbrowser ws proxy",
        "listen": f"0.0.0.0:{env_int('PROXY_PORT', 9000)}",
        "backend_cdp": f"127.0.0.1:{cdp_port}",
    }
    try:
        async with aiohttp.ClientSession() as session:
            version = await _fetch_json(session, cdp_port, "/json/version")
        body["browser"] = version.get("Browser", "")
        body["browser_ws"] = _rewrite_ws_url(request, version.get(
            "webSocketDebuggerUrl", ""))
        body["browser_status"] = "ready"
    except Exception as exc:  # noqa: BLE001
        body["browser_status"] = "starting"
        body["browser_error"] = str(exc)
    return web.json_response(body)


# ---------------------------------------------------------------------------
# WebSocket 双向转发
# ---------------------------------------------------------------------------
async def _pump(
    client_ws: web.WebSocketResponse,
    backend_ws: websockets.WebSocketClientProtocol,
) -> None:
    """backend -> client 方向泵送。"""
    async for message in backend_ws:
        if isinstance(message, bytes):
            await client_ws.send_bytes(message)
        else:
            await client_ws.send_str(message)
    # 后端关闭(浏览器重启/断开) -> 关闭客户端连接, 让其重连
    if not client_ws.closed:
        await client_ws.close(code=1011, message=b"backend closed")


async def _proxy_ws_to_backend(
    request: web.Request, target_url: str, retries: int = 6
) -> web.StreamResponse:
    """把一条客户端 WebSocket 双向代理到 target_url。

    浏览器重启等短暂不可用场景: 启动前先轻量重试, 提升稳定性。
    """
    if not _origin_allowed(request):
        raise web.HTTPForbidden(reason="cross-origin websocket blocked")

    client_ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024,
                                      autoping=True)
    await client_ws.prepare(request)
    logger.info("WS 已连接: %s", request.path)

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            async with websockets.connect(
                target_url,
                max_size=None,
                ping_interval=None,
                ping_timeout=None,
            ) as backend_ws:
                # client -> backend
                async def client_to_backend():
                    async for message in client_ws:
                        if message.type == web.WSMsgType.TEXT:
                            await backend_ws.send(message.data)
                        elif message.type == web.WSMsgType.BINARY:
                            await backend_ws.send(message.data)
                        elif message.type == web.WSMsgType.CLOSE:
                            break

                producer = asyncio.create_task(client_to_backend())
                consumer = asyncio.create_task(_pump(client_ws, backend_ws))
                done, pending = await asyncio.wait(
                    {producer, consumer},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc and not isinstance(exc, (asyncio.CancelledError,
                                                    ConnectionError)):
                        logger.debug("ws pump error: %s", exc)
                return client_ws
        except (OSError, WebSocketException, aiohttp.ClientError,
                asyncio.TimeoutError) as exc:
            last_exc = exc
            await asyncio.sleep(0.5)
    logger.warning("无法连接后端 %s: %s", target_url, last_exc)
    await client_ws.close(code=1011, message=b"backend unreachable")
    return client_ws


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------
def build_app(cdp_port: int) -> web.Application:
    app = web.Application()
    app["cb_cdp_port"] = cdp_port

    async def route_version(request: web.Request) -> web.Response:
        return await handle_version(request, cdp_port)

    async def route_list(request: web.Request) -> web.Response:
        return await handle_list(request, cdp_port)

    async def route_health(request: web.Request) -> web.Response:
        return await handle_health(request, cdp_port)

    async def route_ws(request: web.Request) -> web.StreamResponse:
        # /devtools/<type>/<id> -> 与浏览器同名端点保持一致的转发
        # 浏览器未就绪时自动等待, 就绪后连接即建立
        if not await _wait_browser_ready(cdp_port):
            raise web.HTTPServiceUnavailable(
                reason="browser not ready after timeout")
        path = request.match_info["path"]
        target = f"ws://127.0.0.1:{cdp_port}/devtools/{path}"
        return await _proxy_ws_to_backend(request, target)

    async def route_root(request: web.Request) -> web.StreamResponse:
        # 若以 WebSocket 升级方式访问根路径 -> 视为 browser-level CDP
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if not await _wait_browser_ready(cdp_port):
                raise web.HTTPServiceUnavailable(
                    reason="browser not ready after timeout")
            async with aiohttp.ClientSession() as session:
                try:
                    version = await _fetch_json(session, cdp_port,
                                                "/json/version")
                    target = version.get("webSocketDebuggerUrl")
                except Exception:  # noqa: BLE001
                    target = None
            if not target:
                raise web.HTTPServiceUnavailable(
                    reason="browser CDP not ready yet")
            return await _proxy_ws_to_backend(request, target)
        return await handle_health(request, cdp_port)

    app.router.add_get("/json/version", route_version)
    app.router.add_get("/json/list", route_list)
    app.router.add_get("/json", route_list)
    app.router.add_get("/devtools/{path:.*}", route_ws)
    app.router.add_get("/", route_root)
    return app


# ---------------------------------------------------------------------------
# 独立运行入口(本地调试/复用)
# ---------------------------------------------------------------------------
def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cdp_port = env_int("CDP_PORT", 9222)
    proxy_port = env_int("PROXY_PORT", 9000)

    app = build_app(cdp_port)
    runner = web.AppRunner(app)
    stop = asyncio.Event()

    async def serve() -> None:
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", proxy_port)
        await site.start()
        logger.info("WS 代理已启动: 0.0.0.0:%d -> ws://127.0.0.1:%d (CDP)",
                    proxy_port, cdp_port)
        await stop.wait()
        await runner.cleanup()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        loop.run_until_complete(serve())
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("WS 代理已退出")


if __name__ == "__main__":
    _main()
