#!/usr/bin/env bash
# 部署单一插件仓库到 /opt/jinyv-plugins，并写入 JINYV_PLUGIN_PATHS。
# 用法：sudo bash deploy/install_plugins.sh [仓库地址]
set -euo pipefail

REPO="${1:-${JINYV_PLUGINS_REPO:-https://github.com/YVFENGXS/jinyv-plugins.git}}"
DEST="${JINYV_PLUGINS_DIR:-/opt/jinyv-plugins}"
ENV_FILE=/etc/jinyv-memory.env

command -v git >/dev/null || { echo "缺少 git"; exit 1; }

if [ ! -d "$DEST/.git" ]; then
  echo "==> 克隆 $REPO -> $DEST"
  mkdir -p "$(dirname "$DEST")"
  git clone "$REPO" "$DEST"
else
  echo "==> 更新 $DEST"
  git -C "$DEST" pull --ff-only
fi

touch "$ENV_FILE"
if grep -q '^JINYV_PLUGIN_PATHS=' "$ENV_FILE"; then
  sed -i "s#^JINYV_PLUGIN_PATHS=.*#JINYV_PLUGIN_PATHS=$DEST#" "$ENV_FILE"
else
  echo "JINYV_PLUGIN_PATHS=$DEST" >> "$ENV_FILE"
fi
echo "==> 已部署插件仓库：$DEST"
echo "重启主服务：sudo systemctl restart jinyv-memory"
