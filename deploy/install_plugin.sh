#!/usr/bin/env bash
# 把插件装进已有的 NoneBot2 项目：deploy/install_plugin.sh /opt/qqbot/bot
# 默认插件目录 <bot>/src/plugins；项目用别的目录就把第二个参数写上。
set -euo pipefail

BOT_DIR="${1:?用法: install_plugin.sh <机器人项目目录> [插件目录]}"
PLUGIN_DIR="${2:-$BOT_DIR/src/plugins}"
SRC="$(cd "$(dirname "$0")/.." && pwd)/jinyu_memory"
DEST="$PLUGIN_DIR/jinyu_memory"

mkdir -p "$PLUGIN_DIR"
if [ -e "$DEST" ]; then
    echo "目标已存在，先备份：$DEST -> ${DEST}.bak.$(date +%s)"
    mv "$DEST" "${DEST}.bak.$(date +%s)"
fi
mkdir -p "$DEST"
cp -r "$SRC/." "$DEST/"
echo "已安装到 $DEST"
echo "确认 pyproject.toml 里 [tool.nonebot] plugin_dirs 包含：$PLUGIN_DIR"
