# cloakbrowser keyless Docker（WS 代理 + 会话亲和懒启动）

基于开源 [CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser)
自建的 Docker 镜像：容器内置 **stealth Chromium（远程调试模式）**，再由一个
**Python WS 代理**把浏览器控制面转发到 `0.0.0.0:9000`。部署到阿里云函数计算
（FC）后，浏览器**按需懒启动**：外部客户端先 `GET /start`（携带会话与配置），
拿到稳定 `wss://` 根地址，再用 Playwright / Puppeteer / 裸 WebSocket 直连。

> 与普通"浏览器常驻服务"不同，本服务**不预启动浏览器**、也不靠环境变量注入
> 指纹：每个浏览器实例的参数（seed/时区/语言/代理等）由客户端**按请求下发**，
> 结合 FC 的 **HeaderField 会话亲和**保证同一会话的所有请求落在同一实例，
> 多用户互不干扰、各自独立实例，用完可随时销毁。

## 特性

- **keyless stealth Chromium v146**：免费版（无需密钥/登录），二进制构建期从
  CloakHQ 官方 [GitHub Releases](https://github.com/CloakHQ/CloakBrowser/releases/tag/chromium-v146.0.7680.177.5)
  下载并做 **sha256 校验**，预置进镜像层（运行时不再联网、不依赖外网下载）。
- **零 wrapper / 零驱动**：不用 CloakBrowser 的 Python wrapper（避免其强绑
  playwright 等依赖）；stealth 启动参数（`--no-sandbox --fingerprint=<seed>
  --fingerprint-platform=windows` + 可选时区/语言/代理）由 `app/browser.py`
  原生生成。
- **镜像第三方 Python 依赖仅 `websockets` 一个**（HTTP 探活/转发用标准库实现）。
- **默认 headless**，无需 Xvfb/显示环境，天然适配 FC 等无头平台。
- **懒启动 + 会话亲和**：容器启动即监听 9000（健康检查秒过），浏览器等首个
  `GET /start` 按客户端配置创建；FC HeaderField 亲和（键名 `sessionid`）把
  同一会话的后续请求（含 Playwright WS）稳定路由到同一实例。
- **按请求下发指纹**：`seed` / `timezone` / `locale` / `proxy` /
  `extra_args` 全部来自 `X-Browser-Cfg` 头，同一个容器服务多个"身份互不相同"
  的会话，互不冲突；会话级 `seq` 自增可随时**激发全新实例**（新身份）。
- **健壮**：WS 断开后浏览器保留复用（同会话重连直接命中）；进程崩溃自动按原
  配置重启；容器优雅停机（`SIGTERM`）清理浏览器。

## 工作原理

### 生命周期（懒启动）

```
  +-------------------------- 容器启动(FC 实例) ---------------------------+
  | WS 代理先行监听 0.0.0.0:9000  ← 立即就绪, GET / 恒 200, FC 判定启动成功 |
  |                                浏览器此刻尚不存在(browser_status: 未创建)|
  +------------------------------------------------------------------------+
                                   ▲
  外部客户端两步接入:
    1) GET /start   (携带 sessionID + X-Browser-Cfg)   ← 浏览器在此刻才启动
       返回 {"status":"ready","ws":"wss://<host>/"}
    2) Playwright/Puppeteer connect_over_cdp(ws, headers={"sessionID": ...})
       后续 /json/version 与 WS 握手都只带 sessionID —— FC 亲和路由 + 服务端
       会话比对保证命中同实例的同一浏览器。
```

### 两个自定义 Header（协议约定）

FC 平台对会话亲和的键（`sessionid`）值有**硬限制：1–64 字符、字符集
`^[A-Za-z0-9_][A-Za-z0-9_-]*$`**（实测整份 base64 配置会以 `400 InvalidArgument`
被网关拒绝），因此拆成两个头：

| Header | 内容 | 何时携带 |
| --- | --- | --- |
| `sessionID` | `sha256(JSON(cfg+seq), sort_keys).hexdigest()` 的 64 位 hex，即 FC 亲和会话 ID（也作 `ListSessions` / `DeleteSession` 对象） | 每个请求（`/start`、`/json/*`、WS 握手） |
| `X-Browser-Cfg` | `base64url(JSON 浏览器配置)`，键：`seed`/`timezone`/`locale`/`proxy`/`extra_args` | 仅 `GET /start`（缺失=服务端默认随机配置） |

同 `cfg+seq` → 同 sessionID → 亲和命中同一实例（**复用**）；`seq` 自增 →
新 sessionID → 亲和激发**全新实例**（新身份，互不串扰）。

### HTTP/WS 端点一览

| 端点 | 说明 |
| --- | --- |
| `GET /` | 健康检查恒 200（FC 探活）；body 含 `browser_status: not_created/starting/ready` |
| `GET /start` | 懒启动入口（需 `sessionID`，配置走 `X-Browser-Cfg`）→ `{"status":"ready","ws":稳定根地址,"browser":...}`；400=缺/坏头，409=本实例正被其他会话占用 |
| `GET /json/version` | CDP 版本信息；`webSocketDebuggerUrl` 返回**稳定根地址**（不带随机 uuid，跨实例/重启可用） |
| `GET /json/list` | CDP 目标列表（host 已重写） |
| WS `/(根路径)` | browser-level CDP 双向转发（裸 ws 客户端直连用） |
| WS `/devtools/<type>/<id>` | 页面/目标级 CDP 转发 |

## 构建与本地运行

```bash
# 构建镜像(默认 headless; 固定 chromium-v146 + sha256 校验, 构建期预下载)
docker build -t cloakbrowser-ws-proxy:local .

# 常用可覆盖构建参数(国内构建节点友好, 默认已走 DaoCloud 基镜像/阿里云 pip/中科大 apt):
#   --build-arg CLOAK_CHROMIUM_VERSION=146.0.7680.177.5   # 浏览器版本
#   --build-arg CLOAK_BINARY_SHA256=<对应 asset sha256>   # 下载校验(勿改错)
#   --build-arg CLOAK_DOWNLOAD_URL=<自建 OSS/内网完整 URL>  # Chromium 下载源(默认多个加速镜像 fallback)
#   --build-arg CLOAK_DOWNLOAD_MIRRORS="https://ghfast.top/ https://gh-proxy.com/"
#   --build-arg ENABLE_HEADED=true        # 需要 headed(可视化): 多装 Xvfb/openbox
#   --build-arg PIP_INDEX_URL=...  --build-arg APT_MIRROR=...

# 本地运行
docker run -d --name cloakbrowser -p 9000:9000 cloakbrowser-ws-proxy:local
# 或: docker compose up -d --build
docker logs -f cloakbrowser
```

本地验证两步接入：

```bash
# 1) 构造 sessionID(客户端统一取 64 位 sha256 hex; 任意稳定值亦可)
SID=$(printf '%s' 'demo-session-001' | sha256sum | cut -c1-64)
# 2) 懒启动(本地无 FC 亲和, 配置头同样生效)
curl -i -H "sessionID: $SID" \
     -H "X-Browser-Cfg: $(printf '%s' '{"seed":"12345","timezone":"Asia/Shanghai"}' | base64 -w0)" \
     http://127.0.0.1:9000/start
# 3) 直连(根地址)
curl -H "sessionID: $SID" http://127.0.0.1:9000/json/version
```

## 部署到阿里云函数计算（FC）

### 1) 构建并推送到 ACR（容器镜像服务，个人版即可）

```bash
docker build -t cloakbrowser-ws-proxy:local .

# ACR 个人版仓库地址: registry.<region>.aliyuncs.com/<命名空间>/<镜像名>
docker tag cloakbrowser-ws-proxy:local \
       registry.cn-hangzhou.aliyuncs.com/<namespace>/cloakbrowser:latest
docker login --username=<阿里云账号全名> registry.cn-hangzhou.aliyuncs.com
docker push registry.cn-hangzhou.aliyuncs.com/<namespace>/cloakbrowser:latest
```

> 个人版 ACR 也支持**控制台/制品源直接构建**；镜像较大（Chromium 约 200MB+），
> 建议推送到与 FC 函数同地域的 ACR，减少冷启动拉取时间。
> 若在 ACR/CI 里构建，记得覆盖 `CLOAK_DOWNLOAD_MIRRORS` 为可达的加速源。

### 2) 创建 FC 函数（自定义容器 Custom Container）

1. **镜像**：选上一步 ACR 地址；启动命令/参数留空（用镜像 ENTRYPOINT）。
2. **端口**：`9000`；**健康检查路径**：`/`（懒启动下代理即刻就绪，探测秒过）。
3. **请求处理超时**：调大（建议 ≥ 120s），覆盖 `/start` 的浏览器冷启动
   （就绪上限默认 90s，可用 `BROWSER_READY_TIMEOUT` 调整）。
4. **单实例并发数设为 1**：保证一个实例同一时刻只服务一个会话。
5. **开启会话亲和（关键）**：在函数配置中开启会话亲和 → 选择
   **HeaderField 亲和** → 自定义键名填 **`sessionid`**（值即上面的
   `sessionID` 头，平台要求 1–64 字符、`^[A-Za-z0-9_][A-Za-z0-9_-]*$`，
   客户端发送 64 位 sha256 hex 恰好满足）。
6. 记录**公网入口**（函数 HTTP 触发域名，形如 `https://<xxx>.cn-<region>.fcapp.run`）
   与 **FC OpenAPI endpoint**（`<account-id>.<region>.fc.aliyuncs.com`）。

> 控制台不同版本字段名可能略有差异（如"会话亲和/亲和策略/自定义 Key"），
> 本质是 HeaderField 亲和 + 键名 `sessionid`。未开启亲和的直接后果：
> `/json/version` 与 WS 可能被路由到不同实例导致连接失败。

### 3) 权限与网络

- 调用/销毁会话（`ListSessions` / `DeleteSession`）需在 RAM 为子账号授予该
  函数的相应权限，客户端用 AK/SK（环境变量
  `ALIBABA_CLOUD_ACCESS_KEY_ID`/`ALIBABA_CLOUD_ACCESS_KEY_SECRET` 或
  `~/.alibabacloud/credentials`）即可，无需写在代码里。
- 需要固定出口 IP / 访问特定站点时，可配置 FC 的 NAT/固定公网 IP，或经配置
  头 `proxy` 走上游代理。

## 客户端调用（Playwright）

### 推荐：使用 `fc_browser.py` 封装类

仓库自带一个高层封装（`/workspace/fc_browser.py`），内部完成
「构建 sessionID → `ListSessions` 检查存活 → 无则 `GET /start` → 返回就绪会话
→ `DeleteSession` 销毁」全流程，Playwright 侧只需：

```python
from fc_browser import FcBrowser
from playwright.async_api import async_playwright

mgr = FcBrowser(
    public_base="https://<fc公网域名>.fcapp.run",       # 或环境变量 FC_PUBLIC_BASE
    api_endpoint="<account-id>.<region>.fc.aliyuncs.com",  # 或 FC_API_ENDPOINT
    function_name="<你的函数名>",                          # 或 FC_FUNCTION_NAME
)

# create(): 同参数+seq 已有存活实例时会自动 seq 自增 -> 激发全新实例;
# 返回 BrowserSession(已 /start 就绪)。
sess = mgr.create({
    "seed": "12345",                 # 随机指纹种子: 同 seed 同身份
    "timezone": "Asia/Shanghai",     # 浏览器时区
    "locale": "zh-CN",               # 浏览器语言
    # "proxy": "http://user:pass@host:3128",   # 可选出站代理(密码明文)
    # "extra_args": [...],           # 附加浏览器启动参数
})

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(
            sess.ws_url, headers=sess.connect_headers(), timeout=60_000)
        page = await browser.new_page()
        await page.goto("https://example.com")
        print(await page.title())
        await browser.close()
    sess.destroy()   # FC SDK DeleteSession 停掉实例(按量计费, 用完即销毁)

# asyncio.run(main())
```

依赖：`playwright` + 阿里云 FC SDK 四件套
（`pip install alibabacloud_fc20230330 alibabacloud_credentials
alibabacloud_tea_openapi alibabacloud_tea_util`）。

### 完整可运行示例

**[`test_fc_browser.py`](test_fc_browser.py)** 是一份可运行的端到端参考脚本
（建会话 → 打开抖音聊天页 → 截图 → 销毁），用法：

```bash
export ALIBABA_CLOUD_ACCESS_KEY_ID=... ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
export FC_API_ENDPOINT=<account-id>.<region>.fc.aliyuncs.com
export FC_FUNCTION_NAME=<函数名>

python test_fc_browser.py 'https://<fc公网域名>.fcapp.run' \
    '{"seed":"test-1209","timezone":"Asia/Shanghai","locale":"zh-CN"}'
```

### 手动两步（不依赖 SDK）

```bash
SID=$(printf '%s' '{"seed":"12345","timezone":"Asia/Shanghai","seq":0}' \
      | python3 -c 'import sys,hashlib;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
CFG=$(python3 -c 'import base64,json;print(base64.urlsafe_b64encode(json.dumps(
      {"seed":"12345","timezone":"Asia/Shanghai"}).encode()).decode())')

# 1) 懒启动, 拿到稳定根地址(含 ws 字段)
curl -H "sessionID: $SID" -H "X-Browser-Cfg: $CFG" \
     https://<fc公网域名>.fcapp.run/start
# 2) Playwright 直连: connect_over_cdp("wss://<fc公网域名>.fcapp.run/",
#        headers={"sessionID": "<上面的SID>"})
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PROXY_PORT` | `9000` | 对外监听端口（FC 要求 9000，勿改） |
| `CDP_PORT` | `9222` | 浏览器内部 remote debugging 端口（仅 127.0.0.1） |
| `BROWSER_HEADLESS` | `true` | `true` 无头（推荐，FC 友好）；`false` 需 `ENABLE_HEADED=true` 构建 |
| `BROWSER_READY_TIMEOUT` | `90` | `/start` 冷启动浏览器就绪上限（秒），需 < FC 请求超时 |
| `WAIT_BROWSER_TIMEOUT` | `180` | 浏览器崩溃重启期间业务端点自动等待的最长时间（秒） |
| `PROFILE_DIR` | `/data/profile` | 固定 seed 会话的 profile 目录（FC 下可挂 NAS 持久化） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

> 浏览器指纹参数（seed/时区/语言/代理等）**不在环境变量里**，一律由客户端
> `X-Browser-Cfg` 头按请求下发（见"协议约定"）。

## 注意事项

- **计费与回收**：销毁请用 `DeleteSession`（推荐，立即停实例）；不销毁时 WS
  断开后浏览器会保留复用，按量实例由 FC 空闲回收兜底。需要浏览器长期驻留/
  保持登录态时可配**固定（预留）实例**。
- **持久化**：FC 环境只有 `/tmp` 保证可写；需保存登录态时把 NAS 挂到
  `/data` 并设 `PROFILE_DIR=/data/profile`（配固定 `seed` 才有意义）。
- **资源规格**：建议内存 ≥ 1 GB（Chromium 常驻约 400~800MB）。
- **出站网络**：访问目标站需要固定出口 IP 时，配合 FC NAT/固定公网 IP，或
  在配置里给 `proxy` 上游代理。
- **多用户隔离**：同一容器按 sessionID 区分会话；不同配置的会话需要新实例
  时，客户端 `seq` 自增即可（`fc_browser.create()` 已自动处理）。

## 文件结构

```
Dockerfile            # GitHub Release 下载 Chromium + 系统依赖(可选 headed) + websockets
entrypoint.sh         # headless 直通; headed 才拉起 Xvfb/openbox
app/
  supervisor.py       # 编排: WS 代理先行监听 -> 浏览器懒启动门 -> 崩溃守护/优雅停机
  browser.py          # BrowserManager: 按 sessionID 复用/切换/懒启动 stealth Chromium
  ws_proxy.py         # HTTP/WS 代理 0.0.0.0:9000 -> 127.0.0.1:9222; /start + 双头解析
fc_browser.py         # 客户端封装类(建会话/查询/销毁, 供 Playwright 直连)
test_fc_browser.py    # 端到端参考示例(建会话 -> 抖音截图 -> 销毁)
docker-compose.yml    # 本地编排 + 健康检查 + 持久化卷
```

> 注：本仓库独立实现了"单实例单浏览器 + 会话亲和"的精简代理，未直接使用官方
> `cloakserve`。stealth 参数与免费版 Chromium 二进制取自 CloakBrowser 上游
> （MIT 开源、GitHub Release 分发），但不引入其 Python wrapper 的任何运行时依赖。
