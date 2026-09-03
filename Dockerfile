# =============================================================================
# CloakBrowser(免费版 Chromium 二进制) + 最简 WS 转发 容器镜像
#
# 架构:
#   stealth Chromium (remote debugging, 仅监听 127.0.0.1:9222)
#        ^
#        | 纯 websockets 代理 (0.0.0.0:9000)
#        |
#   外部 CDP 客户端 (Playwright connect_over_cdp / Puppeteer / 任意 ws 客户端)
#
# 极简原则(镜像尽量小):
#   - 不装任何 wrapper/驱动: Chromium 二进制直接从 CloakBrowser 官方
#     GitHub Release 下载(v146 keyless 免费版, 构建期 sha256 校验),
#     stealth 启动参数(--fingerprint=<seed> --fingerprint-platform=windows
#     等, 上游默认集即此 3 项)由 app/browser.py 原生生成;
#   - 第三方 Python 依赖只有 websockets 一个(HTTP 探活/列表用标准库实现);
#   - 默认 headless(无需 Xvfb), 适配阿里云函数计算等无头平台。
#
# 国内构建节点适配:
#   - 无 docker.io 依赖: 基础镜像走 DaoCloud 公共代理, pip 走阿里云,
#     Chromium 走 GitHub Releases(CNB/ACR 节点已验证可 clone github.com);
#   - 不用 # syntax=docker/dockerfile:1(BuildKit 前端镜像会从 docker.io 拉取)。
# =============================================================================
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

# ---- 可覆盖的构建参数 ------------------------------------------------------
# CloakBrowser 免费版 Chromium 版本(v146 = wrapper 0.5.10 对应发布)
ARG CLOAK_CHROMIUM_VERSION=146.0.7680.177.5
# 官方 release asset 的 sha256(构建期校验, 防下载被篡改)
ARG CLOAK_BINARY_SHA256=4a12bcde95fa1bb1beef2b41ab5e5c27c36be78e3be3d0dac8c64d705216670e
# 是否安装 headed 模式依赖(Xvfb/openbox/xdotool), 默认不装(镜像更小)
ARG ENABLE_HEADED=false
# pip 镜像源(默认阿里云; 也可换腾讯云/清华源)
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
# apt 镜像源(默认阿里云 debian 源)
ARG APT_MIRROR=mirrors.aliyun.com

# ---- 1. 系统运行库(Chromium 最小依赖集, 不含中文字体/emoji 等大字体) -------
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
    libxss1 libxtst6 fonts-liberation \
    curl ca-certificates \
    && if [ "${ENABLE_HEADED}" = "true" ]; then \
         apt-get install -y --no-install-recommends xvfb xdotool openbox; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# ---- 2. 下载免费版 stealth Chromium(GitHub Release, 构建期预置进镜像) ------
# 运行时不再联网; 解压后二进制固定在 /opt/cloakbrowser/chrome
RUN set -eux; \
    tag="chromium-v${CLOAK_CHROMIUM_VERSION}"; \
    url="https://github.com/CloakHQ/CloakBrowser/releases/download/${tag}/cloakbrowser-linux-x64.tar.gz"; \
    echo "==> downloading ${url}"; \
    curl -fL --retry 3 --retry-delay 2 -o /tmp/cb.tar.gz "${url}"; \
    echo "${CLOAK_BINARY_SHA256}  /tmp/cb.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/cloakbrowser; \
    tar -xzf /tmp/cb.tar.gz -C /tmp; \
    rm -f /tmp/cb.tar.gz; \
    chrome="$(find /tmp -maxdepth 4 -type f -name chrome -perm -u+x | head -n 1)"; \
    test -n "${chrome}" || { echo "chrome binary not found in archive" >&2; exit 1; }; \
    mv "${chrome}" /opt/cloakbrowser/chrome; \
    chmod +x /opt/cloakbrowser/chrome; \
    rm -rf /tmp/*; \
    echo "==> binary check:"; /opt/cloakbrowser/chrome --version

# ---- 3. 唯一的第三方 Python 依赖: websockets(HTTP/WS 代理) ------------------
RUN pip install --no-cache-dir --index-url ${PIP_INDEX_URL} "websockets==17.1"

# ---- 4. 本仓库代码: 编排器 + 浏览器启动 + 转发代理 --------------------------
WORKDIR /app
COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 默认 headless, 不依赖 Xvfb(阿里云 FC 等平台友好);
# 设 BROWSER_HEADLESS=false 走 headed(需 ENABLE_HEADED=true 构建)
ENV PROXY_PORT=9000 \
    CDP_PORT=9222 \
    BROWSER_HEADLESS=true \
    DISPLAY=:99 \
    CLOAKBROWSER_BINARY_PATH=/opt/cloakbrowser/chrome

# FC 要求容器 HTTP 服务监听 0.0.0.0:9000
EXPOSE 9000

ENTRYPOINT ["/entrypoint.sh"]
