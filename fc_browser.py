# -*- coding: utf-8 -*-
"""阿里云 FC(函数计算)云函数浏览器会话封装类。

把整套「会话亲和 + 懒启动 + Playwright 直连」流程封装成高层对象:

    mgr = FcBrowser(public_base="https://<fc-http-域名或url>",
                    api_endpoint="<uid>.<region>.fc.aliyuncs.com")

    sess = mgr.create({                       # 返回一个新专属会话实例
        "seed": "12345", "timezone": "Asia/Shanghai",
        "locale": "zh-CN", "proxy": "http://u:p@host:3128",
    })
    print(sess.session_id, sess.ws_url)       # 已 ready, 可直接连

    # Playwright 直连(两步中的第二步, 无需再取地址)
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(
            sess.ws_url, headers={"sessionID": sess.session_id})
        ...

    sess.destroy()                            # FC SDK DeleteSession 停掉该实例

设计语义:
  - create() = 提供构建 sessionID 的参数; 会先 ListSessions 查询该 sessionID
    是否已有存活实例; 若有(上次未销毁/未回收), 就给配置字典里的序号键 seq
    自增重建 sessionID -> HeaderField 亲和路由会落到全新实例;
  - 之后 GET /start(携带最终 sessionID)等浏览器就绪, 返回稳定 ws 根地址;
  - destroy() 通过 FC SDK DeleteSession 停掉对应会话的实例。

依赖(仅 aliyun SDK 部分需要, 已惰性导入):
    pip install alibabacloud_fc20230330 alibabacloud_credentials \
                alibabacloud_tea_openapi alibabacloud_tea_util

凭据走默认凭据链(见 alibabacloud_credentials): 环境变量
ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET 或
~/.alibabacloud/credentials 配置文件, 无需在代码里写 AK/SK。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

#: sessionID header 名(服务端大小写不敏感读取, 客户端统一此写法)
SESSION_HEADER = "sessionID"
#: 服务端会话配置中作为「序号」的键, 服务端 normalize 时会忽略未知键, 只用于区分会话
_SEQ_KEY = "seq"


def build_session_id(cfg: dict) -> str:
    """浏览器配置 dict -> sessionID header 值(base64url(JSON))。

    编码方式与 test_fc_browser.py / 服务端 _decode_session 完全一致。
    """
    raw = json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_session_id(sid: str) -> Optional[dict]:
    """sessionID -> 原配置 dict; 解码失败返回 None。"""
    try:
        b = sid.encode("ascii")
        raw = base64.urlsafe_b64decode(b + b"=" * (-len(b) % 4))
        cfg = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return cfg if isinstance(cfg, dict) else None


def _derive_ws_url(public_base: str) -> str:
    """从 http(s) 公网地址推导对应的 wss/ws 根地址。"""
    parts = urlsplit(public_base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    netloc = parts.netloc or parts.path  # 兼容裸 host 写法
    return f"{scheme}://{netloc}/"


@dataclass
class BrowserSession:
    """一次 create() 的成果: 已就绪的专属会话实例。

    session_id / ws_url 可直接用于 Playwright connect_over_cdp 的
    headers={"sessionID": session_id} + ws_url。
    """

    manager: "FcBrowser"
    session_id: str
    ws_url: str
    cfg: dict
    created_at: float = field(default_factory=time.time)

    #: 便捷直连时内部持有的 playwright 对象(browser/pw)
    _pw: Any = field(default=None, repr=False)
    _browser: Any = field(default=None, repr=False)

    # ------------------------------------------------------------------
    @property
    def seq(self) -> int:
        return int(self.cfg.get(_SEQ_KEY, 0))

    def connect_headers(self) -> dict:
        """给 Playwright connect_over_cdp 的 headers。"""
        return {SESSION_HEADER: self.session_id}

    async def connect(self, **kwargs) -> Any:
        """用 Playwright 直连本会话(惰性导入), 返回 browser 对象。

        可选 kwargs 透传给 connect_over_cdp(如 timeout=...)。
        结束后调用 await self.close()。
        """
        from playwright.async_api import async_playwright  # noqa: PLC0415

        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(
            self.ws_url, headers=self.connect_headers(), **kwargs)
        self._pw = pw
        self._browser = browser
        return browser

    async def close(self, destroy_instance: bool = True) -> None:
        """断开 Playwright(可选顺带销毁实例)。"""
        if self._browser is not None:
            try:
                await self._browser.close()
            finally:
                self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
        if destroy_instance:
            self.destroy()

    def destroy(self) -> bool:
        """停掉本会话在 FC 侧的实例。"""
        return self.manager.destroy(self.session_id)

    def __repr__(self) -> str:  # 避免误打印 sessionID(可被解码出敏感配置)
        return (f"<BrowserSession seq={self.seq} ws={self.ws_url} "
                f"cfg_keys={sorted(k for k in self.cfg if k != _SEQ_KEY)}>")


class FcBrowser:
    """FC 云函数浏览器会话管理器。

    参数:
      public_base : 云函数对外 HTTP 地址(供 GET /start 与 Playwright 直连),
                    形如 https://<域名或FC公网URL>, 必填(可经环境变量 FC_PUBLIC_BASE)。
      api_endpoint: FC OpenAPI endpoint, 形如 <uid>.<region>.fc.aliyuncs.com;
                    缺省时取环境变量 FC_API_ENDPOINT, 再不行用
                    f"{account_id}.{region}.fc.aliyuncs.com"。
      function_name: 会话所属函数名(样例里为 ''; 你的场景传空即可),
                    可经环境变量 FC_FUNCTION_NAME 提供。
      http_timeout : GET /start 的超时(秒), 冷启动可能数十秒, 默认 180。
    """

    def __init__(
        self,
        public_base: str = "",
        api_endpoint: Optional[str] = None,
        function_name: str = "",
        account_id: Optional[str] = None,
        region: str = "cn-hangzhou",
        http_timeout: float = 180.0,
        client: Any = None,  # 测试注入用; 平时由凭据链自动创建
    ) -> None:
        self.public_base = (public_base or os.environ.get("FC_PUBLIC_BASE") or "").rstrip("/")
        if not self.public_base:
            raise ValueError(
                "缺少 public_base: 请传云函数对外 HTTP 地址, 或设置环境变量 FC_PUBLIC_BASE")

        if api_endpoint:
            self.api_endpoint = api_endpoint
        elif os.environ.get("FC_API_ENDPOINT"):
            self.api_endpoint = os.environ["FC_API_ENDPOINT"]
        elif account_id:
            self.api_endpoint = f"{account_id}.{region}.fc.aliyuncs.com"
        else:
            raise ValueError(
                "缺少 api_endpoint: 请传 FC OpenAPI endpoint, 或设置环境变量 "
                "FC_API_ENDPOINT, 或提供 account_id + region")

        self.function_name = function_name or os.environ.get("FC_FUNCTION_NAME", "")
        self.http_timeout = http_timeout

        print(f"[FcBrowser] 初始化阿里云 FC SDK ... endpoint={self.api_endpoint}")
        self._client = client or self._create_client(self.api_endpoint)
        print(f"[FcBrowser] SDK 初始化完成; 公网入口={self.public_base}")

    # ------------------------------------------------------------------
    # SDK client
    # ------------------------------------------------------------------
    @staticmethod
    def _create_client(endpoint: str):
        # 惰性导入: 便于本模块在未安装 aliyun SDK 的环境中做纯逻辑测试
        from alibabacloud_fc20230330.client import Client as FC20230330Client  # noqa: PLC0415
        from alibabacloud_credentials.client import Client as CredentialClient  # noqa: PLC0415
        from alibabacloud_tea_openapi import models as open_api_models  # noqa: PLC0415

        # 默认凭据链(环境变量 AK / 凭据文件), 无需在代码里写 AK/SK
        credential = CredentialClient()
        config = open_api_models.Config(credential=credential)
        config.endpoint = endpoint
        return FC20230330Client(config)

    # ------------------------------------------------------------------
    # 会话查询(ListSessions)
    # ------------------------------------------------------------------
    def _runtime_options(self):
        from alibabacloud_tea_util import models as util_models  # noqa: PLC0415
        return util_models.RuntimeOptions()

    def _extract_sessions(self, resp: Any) -> list[dict]:
        """从 ListSessions 响应里稳妥地取出会话列表(结构随 SDK 版本有差异)。"""
        body = getattr(resp, "body", None)
        raw = getattr(body, "sessions", None) if body is not None else None
        if raw is None:
            # 兜底: 序列化整包交给调用方用字符串判定
            try:
                text = json.dumps(
                    resp.to_map() if hasattr(resp, "to_map") else resp,
                    default=str)
            except Exception:  # noqa: BLE001
                text = ""
            return [{"_raw": text}]
        items: list[dict] = []
        for it in raw or []:
            d: dict[str, Any] = {}
            for attr in ("session_id", "instance_id", "state",
                         "created_time", "last_access_time", "id"):
                v = getattr(it, attr, None)
                if v is not None:
                    d[attr] = v
            items.append(d)
        return items

    def list_sessions(self, session_id: Optional[str] = None) -> list[dict]:
        """列出当前存活会话实例(可只精确匹配一个 sessionID)。

        返回 [{session_id, instance_id, state, ...}] 的宽松解析结果。
        """
        from alibabacloud_fc20230330 import models as fc20230330_models  # noqa: PLC0415

        req = fc20230330_models.ListSessionsRequest(
            session_id=session_id) if session_id is not None \
            else fc20230330_models.ListSessionsRequest()
        resp = self._client.list_sessions_with_options(
            self.function_name, req, {}, self._runtime_options())
        return self._extract_sessions(resp)

    def _session_alive(self, sid: str) -> bool:
        """该 sessionID 是否已有存活实例。查询失败视为无(降级: /start 会兜底)。"""
        try:
            items = self.list_sessions(session_id=sid)
        except Exception as exc:  # noqa: BLE001
            print(f"[FcBrowser] 查询会话失败(降级为无存活), 继续: {exc}")
            return False
        for it in items:
            if it.get("session_id") == sid:
                return True
            if "_raw" in it and f'"{sid}"' in it["_raw"]:
                return True
        return False

    # ------------------------------------------------------------------
    # 创建会话(核心)
    # ------------------------------------------------------------------
    def create(self, cfg: dict, seq_start: int = 0, force_new: bool = True,
               reuse: bool = False) -> BrowserSession:
        """创建一个已就绪的专属会话实例。

        参数:
          cfg      : 浏览器配置(seed/timezone/locale/proxy/extra_args 等),
                     即「构建 sessionID 的参数」。None/空值会被剔除。
          seq_start: 序号键起点(默认 0)。通常不需要改。
          force_new: True(默认)= 若相同参数构造出的 sessionID 已有存活实例,
                     则给配置字典的序号键 seq 自增, 直到构建出「无存活实例」
                     的新 sessionID -> FC 亲和路由激发全新实例。
          reuse    : True 时不再无条件开新实例: 若已存在「非 seq 参数一致」
                     的存活会话, 直接复用其 sessionID(不调 /start, 立即可用)。

        返回 BrowserSession(已 GET /start 成功、浏览器就绪)。
        """
        base = {k: v for k, v in cfg.items() if v is not None and k != _SEQ_KEY}

        if reuse:
            hit = self._find_reusable(base)
            if hit is not None:
                print(f"[FcBrowser] 命中存活会话直接复用: {hit}")
                return BrowserSession(
                    manager=self, session_id=hit, ws_url=_derive_ws_url(self.public_base),
                    cfg=decode_session_id(hit) or dict(base))

        seq = int(seq_start or 0)
        sid = ""
        while True:
            trial = dict(base)
            trial[_SEQ_KEY] = seq
            sid = build_session_id(trial)
            if not force_new or not self._session_alive(sid):
                break
            print(f"[FcBrowser] sessionID(seq={seq}) 已有存活实例, 序号自增 -> {seq + 1}")
            seq += 1

        print(f"[FcBrowser] 请求 /start(seq={seq}, sid_len={len(sid)}) ...")
        body = self._request_start(sid)
        ws = body.get("ws") or _derive_ws_url(self.public_base)
        print(f"[FcBrowser] /start 就绪: browser={body.get('browser', '')} ws={ws}")
        return BrowserSession(manager=self, session_id=sid, ws_url=ws,
                              cfg=decode_session_id(sid) or trial)

    def _find_reusable(self, base_cfg: dict) -> Optional[str]:
        """在存活会话中找「非 seq 配置一致」的一个, 返回其 sessionID。"""
        try:
            items = self.list_sessions()
        except Exception as exc:  # noqa: BLE001
            print(f"[FcBrowser] 查询可复用会话失败, 走新建: {exc}")
            return None
        if any("_raw" in it for it in items):
            return None  # 结构无法解析, 放弃复用
        wanted = {k: v for k, v in base_cfg.items() if v is not None}
        for it in items:
            sid = it.get("session_id")
            if not sid:
                continue
            dec = decode_session_id(sid) or {}
            dec_no_seq = {k: v for k, v in dec.items() if k != _SEQ_KEY and v is not None}
            if dec_no_seq == wanted:
                return sid
        return None

    # ------------------------------------------------------------------
    # GET /start(HTTP)
    # ------------------------------------------------------------------
    def _request_start(self, sid: str) -> dict:
        url = f"{self.public_base}/start"
        req = urllib.request.Request(
            url, headers={SESSION_HEADER: sid}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc)
            except Exception:  # noqa: BLE001
                detail = {"raw": exc.read().decode("utf-8", "replace")}
            code = exc.code
            if code == 409:
                raise RuntimeError(
                    "浏览器正被其他会话占用且配置不同(409)。若想开新实例, "
                    f"重新 create() 会自动自增 seq: {detail}") from exc
            raise RuntimeError(f"GET /start 失败 HTTP {code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GET /start 网络错误(云函数可达吗?): {exc}") from exc

    # ------------------------------------------------------------------
    # 销毁会话(DeleteSession)
    # ------------------------------------------------------------------
    def destroy(self, session_id: str) -> bool:
        """通过 FC SDK DeleteSession 停掉该会话绑定的实例。"""
        from alibabacloud_fc20230330 import models as fc20230330_models  # noqa: PLC0415

        req = fc20230330_models.DeleteSessionRequest()
        try:
            self._client.delete_session_with_options(
                self.function_name, session_id, req, {}, self._runtime_options())
        except Exception as exc:  # noqa: BLE001
            msg = getattr(exc, "message", str(exc))
            low = msg.lower()
            if "not found" in low or "不存在" in msg or "no session" in low:
                print(f"[FcBrowser] 会话本就不存在或已回收, 视为销毁完成: {session_id}")
                return True
            raise RuntimeError(f"DeleteSession 失败: {exc}") from exc
        print(f"[FcBrowser] 已销毁会话实例: session_id={session_id}")
        return True


# ---------------------------------------------------------------------------
# 命令行小入口(便于快速自测; 依赖环境变量 FC_PUBLIC_BASE / FC_API_ENDPOINT)
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    usage = (
        "用法:\n"
        "  python fc_browser.py create '<cfg json>'   # 创建会话, 打印 session_id/ws\n"
        "  python fc_browser.py destroy <session_id>  # 销毁会话实例\n"
        "  python fc_browser.py list [session_id]     # 列出存活会话\n"
        "环境变量: FC_PUBLIC_BASE(必), FC_API_ENDPOINT 或 FC_FUNCTION_NAME 可选\n"
    )
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(usage)
        return 0 if argv[1:] else 2

    mgr = FcBrowser()
    cmd = argv[1]
    if cmd == "create":
        cfg = json.loads(argv[2]) if len(argv) > 2 else {}
        sess = mgr.create(cfg)
        print(f"session_id={sess.session_id}")
        print(f"ws_url={sess.ws_url}")
        print(f"seq={sess.seq}")
    elif cmd == "destroy":
        mgr.destroy(argv[2])
    elif cmd == "list":
        for it in mgr.list_sessions(argv[2] if len(argv) > 2 else None):
            print(it)
    else:
        print(usage)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv))
    except Exception as exc:  # noqa: BLE001
        print(f"[FcBrowser] 失败: {type(exc).__name__}: {exc}")
        sys.exit(1)
