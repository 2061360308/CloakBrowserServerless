"""浏览器进程管理: 以 remote debugging(远程调用)模式启动 CloakBrowser
免费版 stealth Chromium, 并等待 CDP HTTP 接口就绪。

零依赖设计: 不引入任何 Python wrapper/驱动。Chromium 二进制由 Dockerfile
构建期从 CloakBrowser 官方 GitHub Release 下载(v146, keyless)并预置在
/opt/cloakbrowser/chrome; stealth 启动参数按上游默认集(经源码核实)原生
生成, 核心只有:
    --no-sandbox --fingerprint=<seed> --fingerprint-platform=windows
(可选: --fingerprint-timezone / --fingerprint-locale / --lang /
 --proxy-server / --start-maximized)

浏览器仅监听 127.0.0.1:<CDP_PORT>, 不直接对外暴露; 由 WS 代理负责对外转发。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("cbapp.browser")

# 容器默认以 root 运行, Chromium 需要 --no-sandbox(见 stealth 默认集)
BASE_CHROME_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-popup-blocking",
    "--disable-background-networking",
    "--metrics-recording-only",
    "--ignore-gpu-blocklist",
]

# 构建期预置二进制的默认路径(可用 CLOAKBROWSER_BINARY_PATH 覆盖)
_DEFAULT_BINARY = "/opt/cloakbrowser/chrome"


class StealthBrowser:
    """单个 stealth Chromium 实例的生命周期管理。"""

    def __init__(
        self,
        cdp_port: int = 9222,
        headless: bool = False,
        seed: str | None = None,
        profile_dir: str | None = None,
        timezone: str | None = None,
        locale: str | None = None,
        proxy: str | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.cdp_port = cdp_port
        self.headless = headless
        self.timezone = timezone
        self.locale = locale
        self.proxy = proxy
        self.extra_args = extra_args or []
        self.process: subprocess.Popen | None = None

        if seed:
            # 固定指纹: 使用持久化 profile 目录, 可跨容器保留登录态/身份
            self.seed = seed
            self.profile_dir = Path(profile_dir or "/data/profile")
            self._tmp_profile = False
        else:
            # 随机指纹: 每次全新临时 profile, 退出自动清理, 不留垃圾
            self.seed = str(random.randint(10000, 99999))
            self.profile_dir = Path(tempfile.mkdtemp(prefix="cloak-profile-"))
            self._tmp_profile = True

    # ------------------------------------------------------------------
    def binary_path(self) -> str:
        """返回预置的 stealth Chromium 可执行文件路径(构建期已下载)。"""
        path = os.environ.get("CLOAKBROWSER_BINARY_PATH") or _DEFAULT_BINARY
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"stealth Chromium 未找到: {path}。请确认镜像构建时已从 "
                "GitHub Release 下载并预置该二进制(见 Dockerfile)。"
            )
        return path

    # ------------------------------------------------------------------
    def _stealth_args(self) -> list[str]:
        """生成 CloakBrowser stealth 启动参数(等价上游 build_args 的
        stealth_args=True 输出, 原生实现, 不依赖 wrapper)。

        上游默认 stealth 集(源码核实, Linux/Windows 平台一律伪装 Windows):
            --no-sandbox
            --fingerprint=<seed>
            --fingerprint-platform=windows
        条件参数: timezone/locale/proxy/start_maximized(与 build_args 一致)。
        """
        args = [
            "--no-sandbox",
            f"--fingerprint={self.seed}",
            "--fingerprint-platform=windows",
        ]
        if self.timezone:
            args.append(f"--fingerprint-timezone={self.timezone}")
        if self.locale:
            args.append(f"--fingerprint-locale={self.locale}")
            args.append(f"--lang={self.locale}")
        if not self.headless:
            args.append("--start-maximized")
        if self.proxy:
            args.append(f"--proxy-server={self.proxy}")
        return args

    # ------------------------------------------------------------------
    def start(self) -> subprocess.Popen:
        """启动浏览器进程(幂等: 已在运行则直接返回)。"""
        if self.process is not None and self.process.poll() is None:
            return self.process

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        binary = self.binary_path()

        base = list(BASE_CHROME_ARGS)
        # 裸进程方式需显式启用 headless(上游 build_args 不注入 --headless)
        if self.headless:
            base.append("--headless=new")

        chrome_args = self._stealth_args()

        # "浏览器开启远程调用启动": 绑定 127.0.0.1, 仅内网可达
        cdp_flags = [
            f"--remote-debugging-port={self.cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.profile_dir}",
        ]

        cmd = [binary, *base, *chrome_args, *cdp_flags, *self.extra_args]
        logger.info(
            "启动 Chromium (seed=%s, cdp=127.0.0.1:%d, headless=%s, profile=%s)",
            self.seed, self.cdp_port, self.headless, self.profile_dir,
        )
        # stdout/stderr 继承容器日志, 便于 docker logs 排障
        self.process = subprocess.Popen(cmd)
        return self.process

    # ------------------------------------------------------------------
    def wait_ready(self, timeout: float = 90.0) -> dict:
        """轮询 CDP HTTP 接口, 直到浏览器就绪; 超时/退出则抛错。"""
        url = f"http://127.0.0.1:{self.cdp_port}/json/version"
        deadline = time.monotonic() + timeout
        delay = 0.2
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(
                    f"Chromium 进程提前退出, exit code={self.process.returncode}。"
                    "请用 docker logs 查看浏览器 stderr 定位缺少的系统依赖。"
                )
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    data = json.load(resp)
                logger.info(
                    "浏览器 CDP 就绪: %s", data.get("webSocketDebuggerUrl", url)
                )
                return data
            except Exception as exc:  # noqa: BLE001 - 启动期任何错误都视为未就绪
                last_err = exc
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
        raise TimeoutError(
            f"浏览器 CDP 在 {timeout}s 内未就绪: {url} (最后错误: {last_err})"
        )

    # ------------------------------------------------------------------
    def stop(self, timeout: float = 10.0) -> None:
        """优雅停止浏览器并清理临时 profile。"""
        proc = self.process
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            logger.info("Chromium 已退出 (code=%s)", proc.returncode)
            self.process = None
        if self._tmp_profile and self.profile_dir.exists():
            shutil.rmtree(self.profile_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 按客户端会话(sessionID 身份 + X-Browser-Cfg 配置)懒启动/复用/切换的管理器
# ---------------------------------------------------------------------------
class BrowserBusy(Exception):
    """当前浏览器正被其他会话占用, 无法按新配置切换。"""


def normalize_cfg(raw: dict) -> dict:
    """把客户端传入的 JSON 配置规范化为等价稳定 dict(用于幂等/复用比较)。

    只保留真正影响浏览器进程的参数; None/空串归一为 None; extra_args 排序。
    """

    def _opt(name: str):
        v = raw.get(name)
        return v if v else None

    extra = raw.get("extra_args") or raw.get("extra") or []
    if isinstance(extra, str):
        extra = extra.split()
    return {
        "seed": str(_opt("seed")) if _opt("seed") else None,
        "timezone": _opt("timezone"),
        "locale": _opt("locale"),
        "proxy": _opt("proxy"),
        "extra_args": sorted(str(a) for a in extra),
    }


class BrowserManager:
    """单个实例内的浏览器门(gate): 按会话懒启动/复用/切换 stealth Chromium。

    设计前提: 同一时刻一个活跃会话(FC 单实例并发=1 天然保证), 同实例内
    最多一个浏览器进程(内存约束)。语义(以 sessionID 为会话身份, 配置由
    X-Browser-Cfg 头下发, 见 ws_proxy._handle_start):

      - 相同 sessionID 再次请求    -> 复用当前进程(同会话重连直接命中);
      - 无进程                     -> 按配置启动并等待 CDP 就绪;
      - 不同 sessionID 且空闲      -> 停旧启新(按新配置重建浏览器);
      - 不同 sessionID 但有活跃 WS -> 抛 BrowserBusy(409), 不踢人;
      - 进程崩溃                   -> supervisor watchdog 按当前配置重启
                                      (sessionID 不变, 亲和仍命中本实例)。

    浏览器由客户端 GET /start 决定, 容器启动时不再预启动; 断连后进程保留
    复用, 由 FC 空闲回收实例兜底清理。
    """

    def __init__(
        self,
        cdp_port: int = 9222,
        headless: bool = True,
        ready_timeout: float = 50.0,
    ) -> None:
        self.cdp_port = cdp_port
        self.headless = headless
        self.ready_timeout = ready_timeout
        self._lock = asyncio.Lock()
        self._browser: StealthBrowser | None = None
        self._cfg: dict | None = None
        self._sid: str | None = None

    @property
    def browser(self) -> StealthBrowser | None:
        return self._browser

    def chrome_exists(self) -> bool:
        """是否已创建浏览器进程(含启动中/已就绪)。代理据此快速失败/等待。"""
        return self._browser is not None

    @property
    def current_session_id(self) -> str | None:
        """当前浏览器所属会话 ID(代理层据此防跨会话误连)。"""
        return self._sid

    def _running(self) -> bool:
        b = self._browser
        return b is not None and b.process is not None and b.process.poll() is None

    # ------------------------------------------------------------------
    def _new_browser(self, cfg: dict) -> StealthBrowser:
        """按归一化配置构造浏览器对象(不启动)。"""
        return StealthBrowser(
            cdp_port=self.cdp_port,
            headless=self.headless,
            seed=cfg["seed"],
            profile_dir=os.environ.get("PROFILE_DIR") or None,
            timezone=cfg["timezone"],
            locale=cfg["locale"],
            proxy=cfg["proxy"],
            extra_args=cfg["extra_args"],
        )

    def _describe(self) -> dict:
        return {"status": "ready", "seed": self._cfg.get("seed") if self._cfg else None}

    # ------------------------------------------------------------------
    async def ensure(self, session_id: str, cfg_raw: dict,
                     active_ws: int = 0) -> dict:
        """确保存在与 session_id 匹配且 CDP 就绪的浏览器; 返回状态 dict。"""
        cfg = normalize_cfg(cfg_raw)
        async with self._lock:
            if self._running() and self._sid == session_id:
                logger.info("会话 %s 复用当前浏览器", session_id)
            elif self._running():
                if active_ws > 0:
                    raise BrowserBusy(
                        "浏览器正被其他会话占用且配置不同, 无法切换")
                logger.info("切换会话: %s -> %s (浏览器按新配置重建)",
                            self._sid, session_id)
                await self._stop_locked()
                self._start_locked(session_id, cfg)
            else:
                self._start_locked(session_id, cfg)
            await self._wait_ready_locked()
            return self._describe()

    def _start_locked(self, session_id: str, cfg: dict) -> None:
        self._sid = session_id
        self._cfg = cfg
        self._browser = self._new_browser(cfg)
        self._browser.start()

    async def _stop_locked(self) -> None:
        if self._browser is not None:
            await asyncio.to_thread(self._browser.stop)
        self._browser = None
        self._cfg = None
        self._sid = None

    async def _wait_ready_locked(self) -> None:
        assert self._browser is not None
        await asyncio.to_thread(self._browser.wait_ready, self.ready_timeout)

    # ------------------------------------------------------------------
    async def restart_current(self) -> None:
        """watchdog 用: 当前浏览器进程崩溃后按原 cfg 重启(幂等)。

        重启失败(如持续起不来)则清空引用, 由下一次 ensure 重新创建,
        避免 watchdog 按过期配置空转。
        """
        async with self._lock:
            if self._browser is None:
                return
            cfg = self._cfg
            try:
                await asyncio.to_thread(self._browser.start)
                await asyncio.to_thread(self._browser.wait_ready, self.ready_timeout)
                logger.info("浏览器已按配置重启并就绪: %s", cfg)
            except Exception:  # noqa: BLE001
                logger.exception("浏览器重启失败, 标记为不可用(下次请求时重建)")
                self._browser = None
                self._cfg = None
                self._sid = None

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()
