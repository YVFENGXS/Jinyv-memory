#!/usr/bin/env bash
# 视频通话服务联调部署：上游服务独立仓库，主仓库只装适配层。
# 用法：sudo bash deploy/install_call_adapter.sh
set -euo pipefail

PLUGINS="${JINYV_PLUGINS_DIR:-/opt/jinyv-plugins}"
UPSTREAM="${JINYV_VIDEO_CALL_DIR:-/opt/jinyv-video-call}"
ENV_FILE=/etc/jinyv-memory.env

[ -d "$PLUGINS/video-call" ] || { echo "缺少插件目录：$PLUGINS/video-call"; exit 1; }

if [ ! -d "$UPSTREAM/.git" ]; then
  echo "==> 克隆通话服务上游仓库"
  git clone https://github.com/YVFENGXS/jinyu-video-call.git "$UPSTREAM"
fi
if [ -f "$UPSTREAM/requirements.txt" ]; then
  /opt/jinyv-memory/.venv/bin/pip install -r "$UPSTREAM/requirements.txt"
fi

touch "$ENV_FILE"
grep -q '^JINYV_VIDEO_CALL_TOKEN=' "$ENV_FILE" \
  || echo "JINYV_VIDEO_CALL_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$ENV_FILE"
echo "在 config/plugins/video-call.toml 设置："
echo "  repo_dir = \"$UPSTREAM\""
echo "  service_url = \"http://127.0.0.1:8790\""
echo "  serve = false"
echo "启动通话服务后：sudo systemctl restart jinyv-memory"
