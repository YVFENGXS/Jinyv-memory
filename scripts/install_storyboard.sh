#!/usr/bin/env bash
# 安装第三方视频分镜 CLI（dsh-video-storyboard），不把源码复制进本仓库。
# 用法：bash scripts/install_storyboard.sh [安装目录]
# 环境变量：RP_STORYBOARD_REF 固定上游提交；RP_STORYBOARD_REPO 覆盖仓库地址
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${RP_STORYBOARD_REPO:-https://github.com/SAOS-3624/dsh-video-storyboard.git}"
REF="${RP_STORYBOARD_REF:-6ef68acd7d755f9ba494317458b13d272dda8ff5}"
DEST="${1:-${RP_STORYBOARD_DIR:-$ROOT/var/vendor/dsh-video-storyboard}}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少 $1，请先安装"; exit 1; }; }
need git
need node
need ffmpeg

MAJOR="$(node -v | sed 's/^v//' | cut -d. -f1)"
if [ "$MAJOR" -lt 20 ]; then
  echo "需要 Node >= 20，当前 $(node -v)"; exit 1
fi

if [ ! -d "$DEST/.git" ]; then
  echo "==> 克隆到 $DEST"
  mkdir -p "$(dirname "$DEST")"
  git clone "$REPO" "$DEST"
fi

echo "==> 切到 $REF"
git -C "$DEST" fetch --depth 1 origin "$REF"
git -C "$DEST" -c advice.detachedHead=false checkout FETCH_HEAD
test -f "$DEST/bin/storyboard.mjs"

echo "==> 已安装：$DEST (commit $(git -C "$DEST" rev-parse --short HEAD))"
echo "在 config/plugins/dsh-video-storyboard.toml 设置："
echo "  storyboard_dir = \"$DEST\""
