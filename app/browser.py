"""浏览器进程管理: 以 remote debugging(远程调用)模式启动 CloakBrowser
stealth Chromium, 并等待 CDP HTTP 接口就绪。

设计参考上游 cloakserve: 浏览器仅监听 127.0.0.1:<CDP_PORT>,
不直接对外暴露; 由 WS 代理负责对外转发。
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

from cloakbrowser.browser import build_args
from cloakbrowser.download import ensure_binary

logger = logging.getLogger("cbapp.browser")

# 容器默认以 root 运行, Chromium 需要 --no-sandbox
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
        """确保(或复用缓存的)免费版 stealth Chromium, 返回可执行文件路径。
        keyless: 不带密钥, 走免费版 v146, 无需任何密钥。
        """
        return ensure_binary()

    # ------------------------------------------------------------------
    def start(self) -> subprocess.Popen:
        """启动浏览器进程(幂等: 已在运行则直接返回)。"""
        if self.process is not None and self.process.poll() is None:
            return self.process

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        binary = self.binary_path()

        # --fingerprint=<seed>: CloakBrowser 的 stealth 指纹机制
        fp_extra = [f"--fingerprint={self.seed}"]
        if self.proxy:
            fp_extra.append(f"--proxy-server={self.proxy}")
        if not self.headless:
            fp_extra.append("--start-maximized")

        chrome_args = build_args(
            stealth_args=True,
            extra_args=fp_extra,
            timezone=self.timezone,
            locale=self.locale,
            headless=self.headless,
        )

        base = list(BASE_CHROME_ARGS)
        # 裸进程方式需显式启用 headless(上游 build_args 不注入 --headless)
        if self.headless:
            base.append("--headless=new")
        # 容器普遍无 userns/SUID sandbox 权限; 确保 --no-sandbox 存在(去重)
        if not any(a.split("=", 1)[0] == "--no-sandbox" for a in chrome_args):
            base.append("--no-sandbox")

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
