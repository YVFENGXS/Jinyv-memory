#!/usr/bin/env bash
# 拉取两个独立仓库到 external/，供适配层检测；不复制源码进主仓库。
# 用法：bash scripts/install_external_plugins.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${JINYV_EXTERNAL_DIR:-$ROOT/external}"
mkdir -p "$DEST"

clone_or_pull() {
  local name="$1" repo="$2"
  if [ ! -d "$DEST/$name/.git" ]; then
    git clone --depth 1 "$repo" "$DEST/$name"
  else
    git -C "$DEST/$name" fetch --depth 1 origin
    git -C "$DEST/$name" reset --hard origin/HEAD
  fi
}

clone_or_pull "Jinyu-pet" "https://github.com/YVFENGXS/Jinyu-pet.git"
clone_or_pull "jinyu-video-call" "https://github.com/YVFENGXS/jinyu-video-call.git"

echo "==> 已拉取到 $DEST"
echo "在 config/plugins/desktop-pet.toml 设置："
echo "  client_dir = \"$DEST/Jinyu-pet\""
echo "在 config/plugins/video-call.toml 设置："
echo "  repo_dir = \"$DEST/jinyu-video-call\""
echo "注意：external/ 已在 .gitignore 中，源码不会进入主仓库。"
