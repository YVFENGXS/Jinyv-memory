#!/usr/bin/env bash
# 安装 Jinyv-memory 主仓库到 /opt/jinyv-memory，并注册 systemd 服务。
# 用法：sudo bash deploy/install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${JINYV_HOME:-/opt/jinyv-memory}"
SERVICE=/etc/systemd/system/jinyv-memory.service
ENV_FILE=/etc/jinyv-memory.env

command -v python3 >/dev/null || { echo "缺少 python3"; exit 1; }
command -v git >/dev/null || { echo "缺少 git"; exit 1; }

if [ ! -d "$DEST/.git" ]; then
  echo "==> 复制主仓库到 $DEST"
  mkdir -p "$DEST"
  cp -a "$ROOT/." "$DEST/"
fi

cd "$DEST"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[qqbot,desktop,video]"

mkdir -p var secrets config/plugins
[ -f "$ENV_FILE" ] || { cp deploy/jinyv-memory.env.example "$ENV_FILE"; chmod 600 "$ENV_FILE"; }
cp deploy/jinyv-memory.service "$SERVICE"
systemctl daemon-reload
systemctl enable jinyv-memory
echo "==> 已安装：$DEST"
echo "请编辑 $ENV_FILE 填入密钥与 JINYV_PLUGIN_PATHS，然后："
echo "  sudo systemctl start jinyv-memory"
