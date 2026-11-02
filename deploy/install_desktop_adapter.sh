#!/usr/bin/env bash
# 桌宠 desktop-v1 服务端适配器部署（插件在 /opt/jinyv-plugins/desktop-pet）。
# 用法：sudo bash deploy/install_desktop_adapter.sh [embedded|local|server] [core_url]
#   embedded：桌宠适配层与核心同进程，直接走 MemoryAPI（默认）
#   local    ：桌宠服务独立运行，连本机 http://127.0.0.1:8710
#   server   ：桌宠服务独立运行，连远程核心（必须提供 core_url）
set -euo pipefail

MODE="${1:-${JINYV_DESKTOP_CORE_MODE:-embedded}}"
PLUGINS="${JINYV_PLUGINS_DIR:-/opt/jinyv-plugins}"
ENV_FILE=/etc/jinyv-memory.env
CORE_URL="${2:-${JINYV_DESKTOP_CORE_URL:-}}"

case "$MODE" in
  embedded) CORE_URL="" ;;
  local)    CORE_URL="${CORE_URL:-http://127.0.0.1:8710}" ;;
  server)
    [ -n "$CORE_URL" ] || { echo "server 模式必须提供 core_url：$0 server https://core.example"; exit 1; }
    ;;
  *) echo "未知模式：$MODE（可选 embedded|local|server）"; exit 1 ;;
esac

[ -d "$PLUGINS/desktop-pet" ] || { echo "缺少插件目录：$PLUGINS/desktop-pet"; exit 1; }
if [ -f "$PLUGINS/desktop-pet/requirements.txt" ]; then
  /opt/jinyv-memory/.venv/bin/pip install -r "$PLUGINS/desktop-pet/requirements.txt"
fi

touch "$ENV_FILE"
set_env() {
  local key="$1" value="$2"
  if grep -q "^$key=" "$ENV_FILE"; then
    sed -i "s|^$key=.*|$key=$value|" "$ENV_FILE"
  else
    echo "$key=$value" >> "$ENV_FILE"
  fi
}
grep -q '^JINYV_DESKTOP_TOKEN=' "$ENV_FILE" \
  || echo "JINYV_DESKTOP_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$ENV_FILE"
if [ "$MODE" != "embedded" ]; then
  grep -q '^JINYV_HOST_API_TOKEN=' "$ENV_FILE" \
    || echo "JINYV_HOST_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$ENV_FILE"
fi
set_env JINYV_PLUGIN_DESKTOP_PET_CORE_MODE "$MODE"
set_env JINYV_PLUGIN_DESKTOP_PET_CORE_URL "$CORE_URL"

cat <<EOF
==> 桌宠接入模式：$MODE
在 config/plugins/desktop-pet.toml 设置（环境变量会覆盖同名项）：
  serve = true
  host = "127.0.0.1"
  port = 8787
  core_mode = "$MODE"
  core_url = "$CORE_URL"
然后：sudo systemctl restart jinyv-memory

独立运行桌宠服务（不与核心同进程）：
  JINYV_DESKTOP_TOKEN=\${JINYV_DESKTOP_TOKEN} \\
  JINYV_HOST_API_TOKEN=\${JINYV_HOST_API_TOKEN} \\
  python3 $PLUGINS/desktop-pet/standalone.py --core-url "$CORE_URL"
EOF
