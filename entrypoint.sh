#!/bin/bash
# =============================================================================
# 容器入口。
#
# 默认(推荐): BROWSER_HEADLESS=true —— 无头模式, 无需 Xvfb/显示环境,
#   适配阿里云函数计算等无头平台。
# 可选:      BROWSER_HEADLESS=false —— headed 模式, 自动拉起 Xvfb + openbox
#   (需构建镜像时加 --build-arg ENABLE_HEADED=true 预装 xvfb/openbox)
#
# 启动任务(见 app/supervisor.py):
#   阶段1: stealth Chromium 以 remote debugging 模式启动, 等待 CDP 就绪
#   阶段2: 就绪后 WS 代理对外转发(0.0.0.0:9000)
# =============================================================================
set -e

# 清除上次容器残留的 X 锁(非 tmpfs 的 /tmp 在 docker restart 后仍保留)
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

_HEADLESS="${BROWSER_HEADLESS:-true}"
if [[ "${_HEADLESS,,}" == "true" ]]; then
  export BROWSER_HEADLESS=true
  exec python -m app.supervisor
fi

# ---- headed 模式: 需要预装的 X 相关二进制 ----
for _bin in Xvfb openbox xdotool; do
  command -v "${_bin}" >/dev/null 2>&1 || {
    echo "[entrypoint] 缺少 ${_bin}: headed 模式需重新构建镜像: \
--build-arg ENABLE_HEADED=true" >&2
    exit 1
  }
done

Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
export DISPLAY=:99
# 轮询等待 X server 真正可用(而不是盲目 sleep)
for _ in $(seq 1 50); do
  DISPLAY=:99 xdotool getdisplaygeometry >/dev/null 2>&1 && break
  sleep 0.2
done
DISPLAY=:99 openbox &

exec python -m app.supervisor
