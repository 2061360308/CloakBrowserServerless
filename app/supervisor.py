"""容器编排器(容器内 PID 1): 顺序完成"容器启动任务"。

启动时序(顺序式, 契合阿里云函数计算 FC 启动判定):
  阶段 1: 浏览器以 remote debugging(远程调用)模式启动, 等待 CDP 就绪;
          此阶段不监听 9000 —— 浏览器未就绪即"容器启动未完成";
  阶段 2: 浏览器就绪后, WS 代理才开始监听 0.0.0.0:<PROXY_PORT>;
          此刻平台健康探测 GET / 才返回 200, 函数判定为"启动成功";
  阶段 3: 守护: 浏览器运行中崩溃自动重启; 收到 SIGTERM/SIGINT 优雅退出。

FC 限制(已核实): 平台在实例创建后 60s 内对容器 9000 端口发起健康探测
GET /(需 200<=status<400); 首次探测失败即判定实例启动失败。因此浏览器
就绪超时默认 50s(小于 60s 窗口, 可通过 BROWSER_READY_TIMEOUT 调整),
headless + 镜像内预置二进制, 冷启动一般 5~20s 内即可就绪。

环境变量(均可选):
  CDP_PORT             浏览器内部 remote debugging 端口, 默认 9222
  PROXY_PORT           对外 WS 代理端口, 默认 9000(FC 要求 9000, 勿改)
  BROWSER_HEADLESS     true/false, 默认 true(无需 Xvfb, FC 友好)
  BROWSER_READY_TIMEOUT  阶段1 浏览器就绪超时秒数, 默认 50(应 < FC 60s 窗口)
  FINGERPRINT_SEED     固定 stealth 指纹; 留空则每次启动随机新身份
  PROFILE_DIR          固定 seed 时的 profile 目录; 默认 /data/profile
  CLOAK_TIMEZONE       e.g. Asia/Shanghai(可空, 自动)
  CLOAK_LOCALE         e.g. zh-CN(可空, 自动)
  PROXY                e.g. http://user:pass@host:port 或 socks5://host:port
  EXTRA_BROWSER_ARGS   附加浏览器参数, 空格分隔
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from app.browser import StealthBrowser
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


async def _browser_watchdog(browser: StealthBrowser,
                            stop: asyncio.Event) -> None:
    """守护: 浏览器运行中崩溃 -> 自动重启; 直到停机信号。

    阶段 2 之后服务已对外监听, 浏览器重启期间:
      - GET /(健康检查/探活) 仍返回 200(实例存活, FC 不会误杀);
      - 业务端点(/json/version 等)由代理内部自动等待新实例就绪。
    """
    check_interval = 2.0
    restart_delay = 3.0
    while not stop.is_set():
        proc = browser.process
        if proc is not None and proc.poll() is not None:
            logger.warning("浏览器进程退出 (code=%s), 自动重启中...",
                           proc.returncode)
            try:
                await asyncio.to_thread(browser.start)
                await asyncio.to_thread(browser.wait_ready)
                logger.info("浏览器已自动重启并就绪")
            except Exception as exc:  # noqa: BLE001
                logger.error("浏览器重启失败: %s; %ss 后重试", exc,
                             restart_delay)
                try:
                    await asyncio.wait_for(stop.wait(), restart_delay)
                    break
                except asyncio.TimeoutError:
                    continue
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
    ready_timeout = _env_float("BROWSER_READY_TIMEOUT", 50.0)
    seed = os.environ.get("FINGERPRINT_SEED") or None

    logger.info(
        "配置: headless=%s, PROXY_PORT=%s, CDP_PORT=%s, "
        "BROWSER_READY_TIMEOUT=%ss, seed=%s",
        headless, proxy_port, cdp_port, ready_timeout,
        seed or "(随机, 每次新身份)",
    )

    browser = StealthBrowser(
        cdp_port=cdp_port,
        headless=headless,
        seed=seed,
        profile_dir=os.environ.get("PROFILE_DIR") or None,
        timezone=os.environ.get("CLOAK_TIMEZONE") or None,
        locale=os.environ.get("CLOAK_LOCALE") or None,
        proxy=os.environ.get("PROXY") or None,
        extra_args=(os.environ.get("EXTRA_BROWSER_ARGS") or "").split(),
    )

    # 停机信号提前注册(阶段 2/3 共用)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # ---------------- 阶段 1: 浏览器就绪(容器启动成功的前提) ---------------
    logger.info("阶段 1/2: 启动浏览器(remote debugging 127.0.0.1:%d), "
                "等待 CDP 就绪...", cdp_port)
    try:
        await asyncio.to_thread(browser.start)
        await asyncio.to_thread(browser.wait_ready, ready_timeout)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "阶段 1/2 失败: %s。浏览器未就绪, 不监听 9000; "
            "容器启动失败退出(FC 将判定实例启动失败)。", exc,
        )
        await asyncio.to_thread(browser.stop)
        raise SystemExit(1) from exc
    logger.info("阶段 1/2 完成: 浏览器 CDP 已就绪")

    # ------- 阶段 2: 浏览器就绪后, 转发脚本才开始监听 9000 ----------
    # 此刻 FC 健康探测 GET / 才能返回 200 => 函数启动成功
    serve_task = asyncio.create_task(
        serve_proxy(cdp_port=cdp_port, proxy_port=proxy_port, stop=stop))
    # 让 serve 先跑起来完成端口绑定(其内部会打"已监听"日志)
    await asyncio.sleep(0)
    logger.info(
        "阶段 2/2 完成: WS 代理已监听 0.0.0.0:%d -> 127.0.0.1:%d; "
        "容器就绪, 函数启动成功", proxy_port, cdp_port,
    )

    # ---------------- 阶段 3: 守护 + 优雅停机 ------------------------------
    watchdog = asyncio.create_task(_browser_watchdog(browser, stop))
    await stop.wait()

    logger.info("收到停机信号, 正在清理...")
    watchdog.cancel()
    serve_task.cancel()
    await asyncio.gather(watchdog, serve_task, return_exceptions=True)
    await asyncio.to_thread(browser.stop)
    logger.info("已优雅退出")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
