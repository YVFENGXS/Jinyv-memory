#!/usr/bin/env bash
# 在 QQ 机器人服务器上安装 desktop-v1 适配器（rp-desktop.service）。
#
# 用法（把 desktop_server.py 一起 scp 到 /tmp 后执行）：
#   scp -i .deploy_key desktop_server.py ubuntu@HOST:/tmp/desktop_server.py
#   scp -i .deploy_key deploy/install_desktop_adapter.sh ubuntu@HOST:/tmp/
#   ssh ... "sed -i 's/\r$//' /tmp/install_desktop_adapter.sh && sudo bash /tmp/install_desktop_adapter.sh"
#
# 密钥不在脚本里复制：服务通过 EnvironmentFile 读取现有的 /opt/qqbot/bot/.env，
# 只把端口、令牌、数据库路径写进 /etc/rp-desktop.env（600 root）。
# 数据库指向 QQ 机器人主库，会话默认接主私聊（qq-u3521544885），两边共享热窗口与长期记忆。
set -euo pipefail

BOT_DIR=/opt/qqbot/bot
SRC=${1:-/tmp/desktop_server.py}
DST_DIR=/opt/qqbot/desktop
UNIT=/etc/systemd/system/rp-desktop.service
ENVFILE=/etc/rp-desktop.env

test -f "$SRC" || { echo "missing $SRC"; exit 1; }
test -x "$BOT_DIR/.venv/bin/python" || { echo "missing bot venv"; exit 1; }

install -d -o qqbot -g qqbot -m 755 "$DST_DIR"
install -o qqbot -g qqbot -m 644 "$SRC" "$DST_DIR/desktop_server.py"

if [ ! -f "$ENVFILE" ]; then
  TOKEN=$(openssl rand -hex 16)
  umask 077
  cat > "$ENVFILE" <<EOF
RP_DESKTOP_HOST=127.0.0.1
RP_DESKTOP_PORT=8787
RP_DESKTOP_TOKEN=$TOKEN
RP_DESKTOP_TRUST_LOCAL=1
RP_DB=/opt/qqbot/data/rp_memory.db
RP_SESSION=qq-u3521544885
RP_CORE_DIR=/opt/qqbot/bot/src/plugins/jinyu_memory/core
EOF
  chown root:root "$ENVFILE"
  chmod 600 "$ENVFILE"
fi
# 老文件补行：只监听回环时，SSH 隧道进来的连接不需要再带令牌
grep -q RP_DESKTOP_TRUST_LOCAL "$ENVFILE" || echo "RP_DESKTOP_TRUST_LOCAL=1" >> "$ENVFILE"
# 老文件迁移：从独立 desktop_memory.db 换到 QQ 主库，并补上要共享的会话
if grep -q '^RP_DB=' "$ENVFILE"; then
  sed -i 's|^RP_DB=.*|RP_DB=/opt/qqbot/data/rp_memory.db|' "$ENVFILE"
else
  echo "RP_DB=/opt/qqbot/data/rp_memory.db" >> "$ENVFILE"
fi
grep -q '^RP_SESSION=' "$ENVFILE" || echo "RP_SESSION=qq-u3521544885" >> "$ENVFILE"

cat > "$UNIT" <<EOF
[Unit]
Description=Jinyu desktop-v1 adapter (RP memory)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=qqbot
Group=qqbot
WorkingDirectory=$DST_DIR
EnvironmentFile=$BOT_DIR/.env
EnvironmentFile=$ENVFILE
ExecStart=$BOT_DIR/.venv/bin/python $DST_DIR/desktop_server.py
Restart=always
RestartSec=5
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rp-desktop >/dev/null 2>&1 || true
systemctl restart rp-desktop
sleep 1
systemctl is-active rp-desktop
grep RP_DESKTOP_TOKEN "$ENVFILE"
curl -s http://127.0.0.1:8787/desktop/v1/health || true
echo
