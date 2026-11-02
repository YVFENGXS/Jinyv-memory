#!/usr/bin/env bash
# 桌宠 desktop-v1 服务端适配器部署（插件在 /opt/jinyv-plugins/desktop-pet）。
# 用法：sudo bash deploy/install_desktop_adapter.sh
set -euo pipefail

PLUGINS="${JINYV_PLUGINS_DIR:-/opt/jinyv-plugins}"
ENV_FILE=/etc/jinyv-memory.env

[ -d "$PLUGINS/desktop-pet" ] || { echo "缺少插件目录：$PLUGINS/desktop-pet"; exit 1; }
if [ -f "$PLUGINS/desktop-pet/requirements.txt" ]; then
  /opt/jinyv-memory/.venv/bin/pip install -r "$PLUGINS/desktop-pet/requirements.txt"
fi

touch "$ENV_FILE"
grep -q '^JINYV_DESKTOP_TOKEN=' "$ENV_FILE" \
  || echo "JINYV_DESKTOP_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$ENV_FILE"
echo "在 config/plugins/desktop-pet.toml 设置："
echo "  serve = true"
echo "  host = \"127.0.0.1\""
echo "  port = 8787"
echo "然后：sudo systemctl restart jinyv-memory"
