"""容器编排器(容器内 PID 1): WS 代理 + 浏览器门(gate)的懒启动编排。

浏览器生命周期(懒启动):
  容器启动时**不再预启动浏览器**, 只把 WS 代理监听起来 —— 健康检查
  GET / 恒 200, FC 立即判定实例启动成功; 浏览器由首个 GET /start
  (携带 sessionID header, 内含浏览器参数)按需创建/复用/切换。
  WS 断开后浏览器进程保留复用(同 session 重连直接命中), 由 FC 空闲
  回收实例兜底清理。

启动时序:
  1. WS 代理监听 0.0.0.0:<PROXY_PORT>(此刻即对外可用);
  2. GET /start 首次到达: 浏览器门按客户端参数启动 stealth Chromium,
     等待 CDP 就绪后返回 {"status":"ready","ws":<稳定根地址>};
  3. 守护: 浏览器运行中崩溃自动按原配置重启; SIGTERM/SIGINT 优雅退出。

环境变量(均可选):
  CDP_PORT             浏览器内部 remote debugging 端口, 默认 9222
  PROXY_PORT           对外 WS 代理端口, 默认 9000(FC 要求 9000, 勿改)
  BROWSER_HEADLESS     true/false, 默认 true(无需 Xvfb, FC 友好)
  BROWSER_READY_TIMEOUT  浏览器就绪超时秒数(懒启动冷启动), 默认 90
  PROFILE_DIR          客户端传固定 seed 时的 profile 目录; 默认 /data/profile

浏览器指纹参数(seed/timezone/locale/proxy/extra_args)一律由客户端
sessionID header 下发, 不再使用 FINGERPRINT_SEED 等环境变量注入。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from app.browser import BrowserManager
from app.ws_proxy import env_int, serve as serve_proxy

logger = logging.getLogger("cbapp.supervisor")

_HEADLESS_TRUE = {"1", "true", "yes", "on"}
_HEADLESS_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool = True) -> bool:
    val = (os.environ.get(name) or "").strip().lower()
    if not val:
        return default
    if val in _HEADLESS_FALSE:
        return False
    return val in _HEADLESS_TRUE or default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


async def _browser_watchdog(gate: BrowserManager, stop: asyncio.Event) -> None:
    """守护: 已由 /start 创建的浏览器运行中崩溃 -> 按原配置自动重启。

    浏览器尚未创建(从未 /start)时空转; 重启失败由 gate 内部标记为不可用,
    下一次 /start 会按新请求重建, 避免 watchdog 按过期配置反复空转。
    重启期间 GET / 健康检查仍返回 200(实例存活, FC 不会误杀)。
    """
    check_interval = 2.0
    while not stop.is_set():
        browser = gate.browser
        proc = browser.process if browser is not None else None
        if proc is not None and proc.poll() is not None:
            logger.warning("浏览器进程退出 (code=%s), 自动重启中...",
                           proc.returncode)
            await gate.restart_current()
        try:
            await asyncio.wait_for(stop.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cdp_port = env_int("CDP_PORT", 9222)
    proxy_port = env_int("PROXY_PORT", 9000)
    headless = _env_bool("BROWSER_HEADLESS", default=True)
    ready_timeout = _env_float("BROWSER_READY_TIMEOUT", 90.0)

    logger.info(
        "配置: headless=%s, PROXY_PORT=%s, CDP_PORT=%s, "
        "BROWSER_READY_TIMEOUT=%ss (浏览器参数由客户端 sessionID 下发)",
        headless, proxy_port, cdp_port, ready_timeout,
    )

    gate = BrowserManager(
        cdp_port=cdp_port,
        headless=headless,
        ready_timeout=ready_timeout,
    )

    # 停机信号提前注册(后续各阶段共用)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # ---------------- 阶段 1: WS 代理先行监听(浏览器懒启动) ----------------
    # 健康检查 GET / 恒 200 => FC 在实例启动窗口内立即判定"启动成功";
    # 浏览器等待首个 GET /start 按其 sessionID 配置懒启动。
    serve_task = asyncio.create_task(
        serve_proxy(cdp_port=cdp_port, proxy_port=proxy_port, stop=stop,
                    gate=gate))
    # 让 serve 先跑起来完成端口绑定(其内部会打"已监听"日志)
    await asyncio.sleep(0)
    logger.info(
        "WS 代理已监听 0.0.0.0:%d -> 127.0.0.1:%d; 浏览器懒启动: "
        "首个 GET /start(携带 sessionID header)按需创建", proxy_port, cdp_port,
    )

    # ---------------- 阶段 2: 守护 + 优雅停机 ------------------------------
    watchdog = asyncio.create_task(_browser_watchdog(gate, stop))
    await stop.wait()

    logger.info("收到停机信号, 正在清理...")
    watchdog.cancel()
    serve_task.cancel()
    await asyncio.gather(watchdog, serve_task, return_exceptions=True)
    await gate.stop()
    logger.info("已优雅退出")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
