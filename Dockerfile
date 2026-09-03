# syntax=docker/dockerfile:1
# =============================================================================
# CloakBrowser(keyless 免费版) + 自定义 WS 代理 容器镜像
#
# 架构:
#   stealth Chromium (remote debugging, 仅监听 127.0.0.1:9222)
#        ^
#        | python WS 代理 (0.0.0.0:9000)
#        |
#   外部 CDP 客户端 (Playwright connect_over_cdp / Puppeteer / 任意 ws 客户端)
#
# 说明:
#   - CloakBrowser wrapper 为 MIT 开源; 未配置任何密钥时自动使用免费版
#     Chromium v146(keyless), 无需付费/登录即可构建运行。
#   - 默认 headless 模式(无需 Xvfb), 适配阿里云函数计算等无头平台;
#     需要 headed 模式时: docker build --build-arg ENABLE_HEADED=true
# =============================================================================
FROM python:3.12-slim

# 上游 wrapper 版本 tag, 用于锁定构建可复现性
ARG CLOAKBROWSER_REF=v0.5.10
# 是否安装 headed 模式依赖(Xvfb/openbox/xdotool), 默认不装(镜像更小)
ARG ENABLE_HEADED=false

# Chromium 运行所需系统库 + 字体/工具(headed 依赖可选)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
    libxcb1 libxext6 libxshmfence1 libglib2.0-0 libgtk-3-0 \
    libpangocairo-1.0-0 libcairo-gobject2 libgdk-pixbuf-2.0-0 \
    libxss1 libxtst6 fonts-liberation fonts-wqy-zenhei fonts-noto-color-emoji \
    curl ca-certificates git \
    && if [ "${ENABLE_HEADED}" = "true" ]; then \
         apt-get install -y --no-install-recommends xvfb xdotool openbox; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# 安装 CloakBrowser Python wrapper(MIT 开源) + serve 依赖(aiohttp/websockets,
# WS 代理需要)。不装 [geoip], 保持 keyless 轻量。
RUN pip install --no-cache-dir \
    "cloakbrowser[serve] @ git+https://github.com/CloakHQ/CloakBrowser@${CLOAKBROWSER_REF}"

# 禁止运行期自动升级检查, 固定 keyless v146 行为
ENV CLOAKBROWSER_AUTO_UPDATE=false

# 构建期预下载免费版 stealth Chromium(无需密钥), 避免运行时联网下载
RUN python -c "from cloakbrowser.download import ensure_binary; print('stealth chromium:', ensure_binary())"

# 本仓库代码: 编排器 + 浏览器启动 + WS 代理
WORKDIR /app
COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 默认 headless, 不依赖 Xvfb(阿里云 FC 等平台友好);
# 设 BROWSER_HEADLESS=false 走 headed(需 ENABLE_HEADED=true 构建)
ENV PROXY_PORT=9000 \
    CDP_PORT=9222 \
    BROWSER_HEADLESS=true \
    DISPLAY=:99

# FC 要求容器 HTTP 服务监听 0.0.0.0:9000
EXPOSE 9000

ENTRYPOINT ["/entrypoint.sh"]
