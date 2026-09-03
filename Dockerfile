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
#   - CloakBrowser wrapper 为 MIT 开源(PyPI: cloakbrowser); 未配置任何密钥
#     时自动使用免费版 Chromium v146(keyless), 无需付费/登录即可构建运行。
#   - 默认 headless 模式(无需 Xvfb), 适配阿里云函数计算等无头平台;
#     需要 headed 模式时: docker build --build-arg ENABLE_HEADED=true
#
# 国内网络构建适配(针对 CNB/腾讯云等国内构建节点 docker.io 拉取超时):
#   - 不使用 # syntax=docker/dockerfile:1(该指令会额外从 docker.io 拉取
#     BuildKit 前端镜像, 国内构建机易超时); 本 Dockerfile 未用到其扩展语法。
#   - 基础镜像与 pip 源均走国内可替换入口(ARG / 环境变量), 默认值可在
#     构建时覆盖:
#        docker build --build-arg PYTHON_IMAGE=... \
#                     --build-arg PIP_INDEX_URL=...
# =============================================================================
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

# 上游 wrapper 版本(PyPI 包版本), 用于锁定构建可复现性
ARG CLOAKBROWSER_VERSION=0.5.10
# 是否安装 headed 模式依赖(Xvfb/openbox/xdotool), 默认不装(镜像更小)
ARG ENABLE_HEADED=false
# pip 镜像源(默认阿里云, 国内构建节点可达; 也可换腾讯云/清华源)
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
# apt 镜像源(默认阿里云 debian 源)
ARG APT_MIRROR=mirrors.aliyun.com

# apt 换国内源 + 安装 Chromium 运行所需系统库 + 字体/工具(headed 依赖可选)
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    else \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
    libxcb1 libxext6 libxshmfence1 libglib2.0-0 libgtk-3-0 \
    libpangocairo-1.0-0 libcairo-gobject2 libgdk-pixbuf-2.0-0 \
    libxss1 libxtst6 fonts-liberation fonts-wqy-zenhei fonts-noto-color-emoji \
    curl ca-certificates \
    && if [ "${ENABLE_HEADED}" = "true" ]; then \
         apt-get install -y --no-install-recommends xvfb xdotool openbox; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# 安装 CloakBrowser Python wrapper(MIT 开源) + serve 依赖。
# 走 PyPI(国内镜像), 不依赖 GitHub; 不装 [geoip], 保持 keyless 轻量。
RUN pip install --no-cache-dir \
    --index-url ${PIP_INDEX_URL} \
    "cloakbrowser[serve]==${CLOAKBROWSER_VERSION}"

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
