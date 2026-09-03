"""通过 Playwright connect_over_cdp 连接 FC 云函数上的 CloakBrowser 进行测试.

用法:
    python test_fc_browser.py <CDP_BASE_URL>          # 命令行参数
    FC_CDP_URL=<CDP_BASE_URL> python test_fc_browser.py  # 或环境变量

CDP_BASE_URL 形如 https://your-fc-endpoint.aliyuncs.com/...
本脚本不内置任何云端地址, URL 一律由外部传入。
服务端已兼容 /json/version/ 尾斜杠, 可直接 connect_over_cdp(base) 直连。
"""
import asyncio
import os
import sys
from datetime import datetime

from playwright.async_api import async_playwright

TEST_URL = "https://www.douyin.com/chat"


def get_cdp_base() -> str:
    url = None
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = os.environ.get("FC_CDP_URL")
    if not url:
        print("[FAIL] 缺少云函数地址: 请传命令行参数或设置环境变量 FC_CDP_URL")
        sys.exit(2)
    return url.rstrip("/")


async def main() -> None:
    cdp_base = get_cdp_base()
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_base, timeout=30_000)
        print(f"[OK] 已连接远端浏览器, version={browser.version}")

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        ua = await page.evaluate("navigator.userAgent")
        print(f"[OK] UA: {ua}")

        print(f"--> goto {TEST_URL}")
        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=45_000)
        # 留一点时间让页面渲染/加载
        await page.wait_for_timeout(3000)
        title = await page.title()
        final_url = page.url
        print(f"[OK] title={title!r} final_url={final_url}")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"douyin_chat_{ts}.png")
        await page.screenshot(path=shot, full_page=False)
        print(f"[OK] 截图已保存: {shot}")

        print("[DONE] 测试通过, 断开连接(远端浏览器进程不受影响)")
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {type(e).__name__}: {e}")
        sys.exit(1)
