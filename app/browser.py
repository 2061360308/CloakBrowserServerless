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
