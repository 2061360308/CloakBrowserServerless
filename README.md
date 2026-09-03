# cloakbrowser keyless Docker (WS 代理转发版)

基于开源 [CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser)
自建的 Docker 镜像：容器内以 **remote debugging(远程调用)** 模式启动
stealth Chromium，再由一个 **Python WS 代理** 把真实的浏览器控制面转发到
`0.0.0.0:9000`，供外部 CDP 客户端接入。**默认 headless(无头)模式**，
无需 Xvfb/显示环境，可直接部署到阿里云函数计算等无头平台。

## 许可证与密钥说明

- CloakBrowser Python wrapper 为 **MIT 开源**（发布在 PyPI 的 `cloakbrowser`
  包），本仓库按固定版本（默认 `0.5.10`，可用构建 ARG 覆盖）从 PyPI 安装，
  不依赖 GitHub。
- 未配置任何密钥（keyless）时，wrapper 自动下载并校验**免费版 Chromium
  v146**，**无需密钥、无需登录**，构建期即预下载进镜像层。
- 若需要 Pro/新版浏览器，可自行设置 `CLOAKBROWSER_LICENSE_KEY` 等，
  本仓库默认按免费 keyless 构建。

## 架构

```
   +------------------------------ 容器启动时序(顺序式) ------------------------------+
   | 阶段 1: 启动 stealth Chromium(remote debugging 127.0.0.1:9222)   ← 未就绪不算启动成功 |
   |          阻塞等待 CDP 就绪(默认上限 50s)                                          |
   | 阶段 2: 浏览器就绪后, WS 代理才监听 0.0.0.0:9000          ← 此刻函数"启动成功"      |
   | 阶段 3: 守护: 浏览器崩溃自动重启; SIGTERM/SIGINT 优雅退出                          |
   +---------------------------------------------------------------------------------+
                              ▲
       外部客户端(浏览器就绪后才可达)
   Playwright / puppeteer / 任意 ws 客户端 ──► ws://host:9000 ──双向转发──► 127.0.0.1:9222
```

- **阶段 1**：`app/browser.py` 以远程调试模式启动浏览器（带 stealth 指纹、
  反检测参数、`--headless=new`），并轮询 `/json/version` 直到就绪。
  浏览器未就绪时**不监听 9000**，超时(默认 50s)直接以非 0 码退出——
  容器启动失败，FC 会判定实例启动失败。
- **阶段 2**：浏览器就绪后 `app/ws_proxy.py` 才开始监听 `0.0.0.0:9000`，
  此刻平台健康探测才能返回 200。代理提供 `GET /json/version`、
  `GET /json/list` 与 WebSocket 双向转发（`/devtools/...` 与根路径
  browser-level 连接均可）。
- **阶段 3**：浏览器运行中崩溃自动重启；容器收到 `SIGTERM`/`SIGINT` 优雅退出。

### 浏览器重启期间的连接语义

服务成功启动后，如果浏览器在运行中崩溃并自动重启：

- `GET /`（健康检查/探活）：仍即时返回 200（实例存活，FC 不会误杀），
  body 内 `browser_status=restarting`。
- `GET /json/version`、`GET /json/list`、`/devtools/...` WS、根路径 WS 等
  **业务端点**：浏览器未就绪时**自动阻塞等待**（默认最长 180 秒，
  可用 `WAIT_BROWSER_TIMEOUT` 调整），就绪后立即返回真实数据——
  **Playwright `connect_over_cdp` 在此场景下无需自写重试**。仅当超过等待
  上限仍未就绪才返回 502。
- **已建立的 CDP 连接会断开**（playwright 报 `Target closed`/连接错误），
  需客户端捕获后重连，重连请求自动走上述等待逻辑。

## 构建与运行

```bash
# 构建镜像(默认 headless; 可用 --build-arg CLOAKBROWSER_VERSION=0.5.10 锁版本)
docker build -t cloakbrowser-ws-proxy:local .

# 基础镜像/pip/apt 默认均已走国内源(DaoCloud Docker Hub 代理 + 阿里云 pip/apt),
# 不依赖 docker.io, 可直接在 CNB / 阿里云 ACR 等国内构建节点构建; 仍可按需覆盖:
#   --build-arg PYTHON_IMAGE=python:3.12-slim                        # 切回官方源
#   --build-arg PYTHON_IMAGE=mirror.ccs.tencentyun.com/library/python:3.12-slim  # 腾讯云内网源
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
#   --build-arg APT_MIRROR=mirrors.tencentyun.com

# 需要 headed(可视化)模式: 多装 Xvfb/openbox
docker build --build-arg ENABLE_HEADED=true -t cloakbrowser-ws-proxy:local .

# 运行
docker run -d --name cloakbrowser -p 9000:9000 cloakbrowser-ws-proxy:local

# 或使用 compose
docker compose up -d --build

# 查看日志(浏览器启动过程/代理连接)
docker logs -f cloakbrowser
```

## 客户端接入示例

### Playwright / Puppeteer(CDP over HTTP)

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9000")
    page = browser.new_page()
    page.goto("https://example.com")
    print(page.title())
    browser.close()
```

Puppeteer 等价写法：`puppeteer.connect({ browserWSEndpoint: "ws://host:9000/devtools/browser/<id>" })`
或直接使用 `GET http://host:9000/json/version` 返回的 `webSocketDebuggerUrl`
（代理已自动把地址重写为对外可达）。

### 裸 WebSocket(CDP 协议)

```python
import asyncio, json, websockets

async def main():
    # 根路径自动转发到 browser-level CDP
    async with websockets.connect("ws://127.0.0.1:9000") as ws:
        await ws.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
        print(await ws.recv())

asyncio.run(main())
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PROXY_PORT` | `9000` | 对外监听端口(FC 要求 9000, 勿改) |
| `CDP_PORT` | `9222` | 浏览器内部 remote debugging 端口(仅 127.0.0.1) |
| `BROWSER_HEADLESS` | `true` | `true` 无头(推荐, 无需 Xvfb)；`false` 需 `ENABLE_HEADED=true` 构建 |
| `BROWSER_READY_TIMEOUT` | `50` | 启动期浏览器就绪上限(秒)；需 < FC 60s 探测窗口 |
| `FINGERPRINT_SEED` | 空 | 固定 stealth 指纹；留空则每次启动随机新身份(适合防关联) |
| `PROFILE_DIR` | `/data/profile` | 固定 seed 时的持久化 profile |
| `CLOAK_TIMEZONE` | 空 | 浏览器时区, 如 `Asia/Shanghai`(可空, 自动) |
| `CLOAK_LOCALE` | 空 | 浏览器语言, 如 `zh-CN` |
| `PROXY` | 空 | 出站代理, 如 `http://user:pass@host:port`、`socks5://host:port` |
| `EXTRA_BROWSER_ARGS` | 空 | 附加浏览器命令行参数, 空格分隔 |
| `WAIT_BROWSER_TIMEOUT` | `180` | 业务端点自动等待浏览器就绪的最长时间(秒) |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 部署到阿里云函数计算(FC)

本镜像已针对 FC 自定义容器运行时的约束做适配：

1. **启动时序(顺序式)**：浏览器先就绪、转发后才监听 `0.0.0.0:9000`。
   FC 平台在实例创建后 **60 秒内**发起健康探测 `GET /`，要求返回
   2xx/3xx；**首次探测失败即判定实例启动失败**。因此未就绪时绝不提前
   监听 9000 —— 监听即代表浏览器就绪、函数启动成功。
2. **60 秒窗口预算**：浏览器就绪上限默认 50s（`BROWSER_READY_TIMEOUT`
   可调，需留足余量给 FC 探测）。headless + 镜像内预下载二进制，冷启动
   通常 5~20s，余量充足。若镜像较大或 CPU 弱，建议在 FC 控制台把健康
   检查的**首次探测延迟时间**调大（如 15~30s），让首次探测落在就绪后。
3. **监听地址/端口**：固定 `0.0.0.0:9000`（FC 探测与请求转发都指向容器
   9000 端口）。**不要改 `PROXY_PORT`**。
4. **无需显示环境**：默认 headless，镜像默认不装 Xvfb（更小、启动更快）。
5. **运行期健康检查**：启动成功后 FC 周期性探测 `GET /`；若浏览器运行中
   崩溃自动重启，`GET /` 仍即时 200（实例存活，不会被误杀）。
6. **优雅停机**：FC 回收实例时发送 `SIGTERM`，容器会清理浏览器进程后退出。

### 部署步骤

```bash
# 1. 推镜像到阿里云 ACR(容器镜像服务), 示例:
docker tag cloakbrowser-ws-proxy:local registry.cn-hangzhou.aliyuncs.com/<ns>/cloakbrowser:latest
docker push registry.cn-hangzhou.aliyuncs.com/<ns>/cloakbrowser:latest

# 2. FC 控制台: 创建函数 -> 使用自定义容器(Custom Container)
#    - 镜像地址: 上面推送的 ACR 地址
#    - 启动命令/参数: 留空(使用镜像 ENTRYPOINT)
#    - 端口: 9000
#    - 健康检查路径: /      (超时建议调大, 如 10s+)
```

### FC 部署注意点

- **冷启动**：FC 回收无请求的按量实例后，下次调用需重新拉实例并执行
  阶段 1（浏览器 5~20s）——期间客户端连不上 9000 属正常，平台会先等
  健康探测通过（60s 窗口）才路由请求，因此**实例未就绪时不会收到请求**；
  实例就绪后浏览器一定已就绪。若需浏览器长期驻留/保持登录态，配置
  **固定(预留)实例数 ≥ 1** 避免回收。
- **指纹与持久化**：FC 环境下只有 `/tmp` 保证可写；若需固定
  `FINGERPRINT_SEED` 的登录态，需挂载 NAS 到 `/data` 并设
  `PROFILE_DIR=/data/profile`。默认（不设 seed）每次冷启动都是全新临时
  身份，无需持久化。
- **WebSocket 长连接**：FC 自定义容器的 HTTP 触发器支持 WebSocket/长连接
  转发（平台反向代理到容器 9000），但空闲超时/最大连接数以平台为准。
  短任务(单页操作)影响不大；超长驻留连接建议关注平台文档或改用固定实例。
- **出站网络**：若站点要求特定出口 IP，可在 FC 中配置 NAT/固定公网 IP，
  并视需要设置 `PROXY` 走上游代理。
- **资源规格**：建议内存 ≥ 1 GB（Chromium 常驻约 400~800 MB）。

## 常用实践

- **想要持久的"登录态/身份"**：固定 `FINGERPRINT_SEED` 并把 `/data` 挂成
  volume（compose 已内置 `cloak-profile` 卷；FC 用 NAS）。
- **想要每次全新身份**：不设 `FINGERPRINT_SEED`，每次启动随机 5 位指纹 +
  临时 profile，退出自动清理。
- **需要可视化 headed**：`ENABLE_HEADED=true` 构建 + `BROWSER_HEADLESS=false`
  （自动拉起 Xvfb:99 + openbox）。
- **就绪检测**：`curl -s http://127.0.0.1:9000/` 看 `browser_status`；
  `curl -s http://127.0.0.1:9000/json/version` 返回 200 表示 CDP 可用。

## 文件结构

```
Dockerfile            # 系统依赖(可选 headed) + wrapper + 预下载二进制
entrypoint.sh         # headless 直通; headed 才拉起 Xvfb/openbox
app/
  supervisor.py       # 编排: 浏览器就绪 -> 监听 9000 -> 守护/优雅停机
  browser.py          # remote debugging 启动 stealth Chromium + 就绪等待
  ws_proxy.py         # HTTP/WS 代理, 0.0.0.0:9000 -> 127.0.0.1:9222
docker-compose.yml    # 本地编排 + 健康检查 + 持久化卷
```

> 注：本仓库独立实现了一个"单浏览器 + 固定端口转发"的精简代理，未直接
> 使用官方 `cloakserve`(它按需多实例 seed 管理，模型不同)。核心 stealth
> 参数/指纹/下载逻辑复用 MIT wrapper，二者可并行使用。
