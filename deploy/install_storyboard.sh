#!/usr/bin/env bash
# 安装第三方视频分镜工具 dsh-video-storyboard（不把它复制进本仓库）。
# 用法：bash deploy/install_storyboard.sh [安装目录]
# 装完在 .env 里加一行 RP_STORYBOARD=1，重启 qqbot 即可生效。
#
# 环境变量：
#   RP_STORYBOARD_DIR    安装目录，默认 /opt/qqbot/vendor/dsh-video-storyboard
#   RP_STORYBOARD_REF    上游提交，默认固定到验证过的版本，升级时改这里
#   RP_STORYBOARD_NODE   没装 Node ≥20 时自动下载的版本
#   RP_STORYBOARD_OWNER  安装目录属主，默认 qqbot
set -euo pipefail

REPO="https://github.com/SAOS-3624/dsh-video-storyboard.git"
REF="${RP_STORYBOARD_REF:-2a6fdc6cb22190606f54a20887632ab63212a0f0}"
DEST="${1:-${RP_STORYBOARD_DIR:-/opt/qqbot/vendor/dsh-video-storyboard}}"
NODE_VER="${RP_STORYBOARD_NODE:-v20.19.0}"
OWNER="${RP_STORYBOARD_OWNER:-qqbot}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少 $1，请先安装"; exit 1; }; }
need curl
need git
need ffmpeg

# 装了 qqbot 就以它的身份跑 git，否则 root 操作别人属主的仓库会被判 dubious ownership
run_owner() {
    if id "$OWNER" >/dev/null 2>&1; then sudo -u "$OWNER" "$@"; else "$@"; fi
}

node_major() {
    node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1
}

if [ -z "$(node_major)" ] || [ "$(node_major)" -lt 20 ]; then
    echo "==> 安装 Node ${NODE_VER} 到 /opt/qqbot/node"
    TMP_XZ="$(mktemp /tmp/node-XXXXXX.tar.xz)"
    curl -fL -o "$TMP_XZ" \
        "https://nodejs.org/dist/${NODE_VER}/node-${NODE_VER}-linux-x64.tar.xz"
    sudo mkdir -p /opt/qqbot/node
    sudo tar -xJf "$TMP_XZ" -C /opt/qqbot/node --strip-components=1
    rm -f "$TMP_XZ"
    for bin in node npm npx; do
        sudo ln -sf "/opt/qqbot/node/bin/$bin" "/usr/local/bin/$bin"
    done
fi
echo "==> Node $(node -v)"

if [ ! -d "$DEST/.git" ]; then
    echo "==> 克隆到 $DEST"
    sudo mkdir -p "$(dirname "$DEST")"
    sudo mkdir -p "$DEST"
    if id "$OWNER" >/dev/null 2>&1; then
        sudo chown "$OWNER:$OWNER" "$DEST"
    fi
    run_owner git clone "$REPO" "$DEST"
fi
echo "==> 切到 $REF"
run_owner git -C "$DEST" fetch --depth 1 origin "$REF"
run_owner git -C "$DEST" -c advice.detachedHead=false checkout FETCH_HEAD

if id "$OWNER" >/dev/null 2>&1; then
    sudo chown -R "$OWNER:$OWNER" "$DEST"
fi
test -f "$DEST/bin/storyboard.mjs"          # 入口在才算装好
run_owner node --version >/dev/null

echo "==> 已安装：$DEST (commit $(run_owner git -C "$DEST" rev-parse --short HEAD))"
echo
echo "在 bot 的 .env 里加："
echo "  RP_STORYBOARD=1"
echo "  RP_STORYBOARD_DIR=$DEST    # 非默认目录才需要"
echo "然后重启：sudo systemctl restart qqbot"
