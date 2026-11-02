#!/usr/bin/env bash
# 视频通话服务联调部署：上游服务独立仓库，主仓库只装适配层。
# 用法：sudo bash deploy/install_call_adapter.sh [embedded|local|server] [core_url]
#   embedded：适配层与核心同进程（默认）
#   local    ：适配层连本机 http://127.0.0.1:8710
#   server   ：适配层连远程核心（必须提供 core_url）
# 通话服务地址用 JINYV_VIDEO_CALL_SERVICE_URL 覆盖，默认 http://127.0.0.1:8790
set -euo pipefail

MODE="${1:-${JINYV_VIDEO_CALL_CORE_MODE:-embedded}}"
PLUGINS="${JINYV_PLUGINS_DIR:-/opt/jinyv-plugins}"
UPSTREAM="${JINYV_VIDEO_CALL_DIR:-/opt/jinyv-video-call}"
ENV_FILE=/etc/jinyv-memory.env
CORE_URL="${2:-${JINYV_VIDEO_CALL_CORE_URL:-}}"
SERVICE_URL="${JINYV_VIDEO_CALL_SERVICE_URL:-http://127.0.0.1:8790}"

case "$MODE" in
  embedded) CORE_URL="" ;;
  local)    CORE_URL="${CORE_URL:-http://127.0.0.1:8710}" ;;
  server)
    [ -n "$CORE_URL" ] || { echo "server 模式必须提供 core_url：$0 server https://core.example"; exit 1; }
    ;;
  *) echo "未知模式：$MODE（可选 embedded|local|server）"; exit 1 ;;
esac

[ -d "$PLUGINS/video-call" ] || { echo "缺少插件目录：$PLUGINS/video-call"; exit 1; }

if [ ! -d "$UPSTREAM/.git" ]; then
  echo "==> 克隆通话服务上游仓库"
  git clone https://github.com/YVFENGXS/jinyu-video-call.git "$UPSTREAM"
fi
if [ -f "$UPSTREAM/requirements.txt" ]; then
  /opt/jinyv-memory/.venv/bin/pip install -r "$UPSTREAM/requirements.txt"
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
grep -q '^JINYV_VIDEO_CALL_TOKEN=' "$ENV_FILE" \
  || echo "JINYV_VIDEO_CALL_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$ENV_FILE"
if [ "$MODE" != "embedded" ]; then
  grep -q '^JINYV_HOST_API_TOKEN=' "$ENV_FILE" \
    || echo "JINYV_HOST_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$ENV_FILE"
fi
set_env JINYV_PLUGIN_VIDEO_CALL_CORE_MODE "$MODE"
set_env JINYV_PLUGIN_VIDEO_CALL_CORE_URL "$CORE_URL"
set_env JINYV_PLUGIN_VIDEO_CALL_SERVICE_URL "$SERVICE_URL"

cat <<EOF
==> 通话适配层记忆核心模式：$MODE
在 config/plugins/video-call.toml 设置（环境变量会覆盖同名项）：
  repo_dir = "$UPSTREAM"
  service_url = "$SERVICE_URL"
  serve = false
  core_mode = "$MODE"
  core_url = "$CORE_URL"
启动通话服务后：sudo systemctl restart jinyv-memory
EOF
