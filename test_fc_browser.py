"""通过 fc_browser.FcBrowser 封装类连接 FC 云函数上按需创建的 CloakBrowser, 打开抖音并截图。

流程(全部交给 fc_browser.py 封装):
  1. FcBrowser(public_base, api_endpoint) 初始化阿里云 FC SDK;
  2. mgr.create(cfg) 内部: 用浏览器参数(seed/timezone/locale/proxy/extra_args)
     构建 sessionID -> ListSessions 查询, 若已有存活实例则 seq 自增激新实例
     -> GET /start 等浏览器就绪, 返回 BrowserSession(含最终 session_id/ws);
  3. sess.connect() = Playwright connect_over_cdp(ws, headers={"sessionID": ...})
     直连浏览器;
  4. 结束后 sess.close(destroy_instance=False): 仅断开 WS, 实例保留复用,
     等 FC 自动回收(与 fc_browser.py destroy() 区分: destroy=True 走 SDK 停实例)。

用法:
    python test_fc_browser.py <FC_BASE_URL> ['{"seed":"123","timezone":"..."}'] [function_name]

    FC_BASE_URL 形如 https://your-fc-endpoint(代码不内置任何云端地址), 也可用
    环境变量 FC_PUBLIC_BASE(旧名 FC_CDP_URL 仍兼容)。
    配置可空: 空配置 = 服务端随机身份 + 默认参数(环境变量 FC_CFG_JSON)。
    FC SDK 所需 endpoint 取环境变量 FC_API_ENDPOINT; 没有则用
    FC_ACCOUNT_ID + FC_REGION(默认 cn-hangzhou)拼出。
"""
import asyncio
import json
import os
import sys
from datetime import datetime

from fc_browser import FcBrowser
from playwright.async_api import async_playwright

TEST_URL = "https://www.douyin.com/chat"


def get_base_url() -> str:
    url = None
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = os.environ.get("FC_PUBLIC_BASE") or os.environ.get("FC_CDP_URL")
    if not url:
        print("[FAIL] 缺少云函数地址: 请传命令行参数, 或设置环境变量 FC_PUBLIC_BASE")
        sys.exit(2)
    return url.rstrip("/")


def build_cfg() -> dict:
    raw = None
    if len(sys.argv) > 2:
        raw = sys.argv[2]
    else:
        raw = os.environ.get("FC_CFG_JSON")
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[FAIL] FC_CFG_JSON 不是合法 JSON: {exc}")
        sys.exit(2)
    if not isinstance(cfg, dict):
        print("[FAIL] 配置必须是 JSON 对象(dict)")
        sys.exit(2)
    return cfg


async def main() -> None:
    base = get_base_url()
    cfg = build_cfg()
    function_name = (sys.argv[3] if len(sys.argv) > 3
                     else os.environ.get("FC_FUNCTION_NAME", ""))
    api_endpoint = os.environ.get("FC_API_ENDPOINT")
    account_id = os.environ.get("FC_ACCOUNT_ID")
    region = os.environ.get("FC_REGION", "cn-hangzhou")

    # 打印配置概要(避免打印 proxy 等敏感字段的明文值)
    shown = {k: v for k, v in cfg.items() if k != "proxy"}
    if "proxy" in cfg:
        shown["proxy"] = "<已设置, 略>"
    print(f"[1/4] FcBrowser(base={base})  配置={shown}")

    mgr = FcBrowser(public_base=base, api_endpoint=api_endpoint,
                    function_name=function_name, account_id=account_id,
                    region=region)

    print(f"[2/4] create(cfg): ListSessions 检查 + GET /start ...")
    sess = mgr.create(cfg)
    print(f"[OK] 会话就绪: seq={sess.seq} sid_len={len(sess.session_id)} "
          f"ws={sess.ws_url}")

    async with async_playwright() as p:
        print(f"[3/4] connect_over_cdp(ws, sessionID) ...")
        browser = await p.chromium.connect_over_cdp(
            sess.ws_url, headers=sess.connect_headers(), timeout=60_000)
        print(f"[OK] 已连接远端浏览器, version={browser.version}")

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        ua = await page.evaluate("navigator.userAgent")
        print(f"[OK] UA: {ua}")

        print(f"[4/4] --> goto {TEST_URL}")
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

        # 仅断开 WS: 远端浏览器进程保留复用, 等待 FC 回收(不销毁实例)
        print("[DONE] 测试通过, 断开连接(实例保留, destroy_instance=False)")
        await browser.close()
        sess.destroy() # 销毁实例

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {type(e).__name__}: {e}")
        sys.exit(1)
