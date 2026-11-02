#!/usr/bin/env bash
# 在 QQ 机器人服务器上安装 desktop-v1 适配器（rp-desktop.service）。
#
# 用法（把 desktop_server.py 一起 scp 到 /tmp 后执行；必须指定要共用的会话）：
#   scp -i .deploy_key desktop_server.py ubuntu@HOST:/tmp/desktop_server.py
#   scp -i .deploy_key deploy/install_desktop_adapter.sh ubuntu@HOST:/tmp/
#   ssh ... "sed -i 's/\r$//' /tmp/install_desktop_adapter.sh && \
#            sudo RP_SESSION=qq-u你的QQ号 bash /tmp/install_desktop_adapter.sh"
#
# 密钥不在脚本里复制：服务通过 EnvironmentFile 读取现有的 /opt/qqbot/bot/.env。
# 桌宠不直接读写 QQ 主库（同一个 RP_DB 只能有一个归档进程，否则游标互抢），
# 而是通过 QQ 进程里的宿主接口 POST /jinyu/chat 写入。脚本把
# RP_HOST_BASE / RP_HOST_TOKEN / RP_SESSION 写进 /etc/rp-desktop.env（600 root），
# 不再写 RP_DB。首次给 bot .env 补 RP_HOST_TOKEN 时需要重启 QQ 机器人。
set -euo pipefail

BOT_DIR=/opt/qqbot/bot
SRC=${1:-/tmp/desktop_server.py}
DST_DIR=/opt/qqbot/desktop
UNIT=/etc/systemd/system/rp-desktop.service
ENVFILE=/etc/rp-desktop.env
BOT_ENV=$BOT_DIR/.env
HOST_PORT=${RP_HOST_PORT:-8080}

test -f "$SRC" || { echo "missing $SRC"; exit 1; }
test -x "$BOT_DIR/.venv/bin/python" || { echo "missing bot venv"; exit 1; }
test -f "$BOT_ENV" || { echo "missing $BOT_ENV"; exit 1; }
if [ -z "${RP_SESSION:-}" ]; then
  echo "用法: sudo RP_SESSION=qq-u你的QQ号 bash $0 [desktop_server.py]"
  echo "（会话号不再内置，必须显式指定；它必须和 QQ 私聊使用的会话一致）"
  exit 1
fi

install -d -o qqbot -g qqbot -m 755 "$DST_DIR"
install -o qqbot -g qqbot -m 644 "$SRC" "$DST_DIR/desktop_server.py"

# 宿主接口令牌：优先复用 bot .env 里已有的；没有就生成并追加（需重启 QQ 才生效）
read_env() {
  grep -m1 "^$1=" "$BOT_ENV" | cut -d= -f2- | tr -d '"\r' | tr -d "'" || true
}
HOST_TOKEN=$(read_env RP_HOST_TOKEN)
BOT_RESTART=0
if [ -z "$HOST_TOKEN" ]; then
  HOST_TOKEN=$(openssl rand -hex 24)
  printf '\nRP_HOST_TOKEN=%s\n' "$HOST_TOKEN" >> "$BOT_ENV"
  BOT_RESTART=1
fi

# 桌面侧写入的会话必须与宿主（QQ 进程）一致：以 bot .env 为准，没有就用入参补上
BOT_SESSION=$(read_env RP_SESSION)
if [ -z "$BOT_SESSION" ]; then
  BOT_SESSION=$RP_SESSION
  printf 'RP_SESSION=%s\n' "$BOT_SESSION" >> "$BOT_ENV"
  BOT_RESTART=1
fi
if [ "$BOT_SESSION" != "$RP_SESSION" ]; then
  echo "注意：bot .env 已有 RP_SESSION=$BOT_SESSION，桌面侧沿用该会话（忽略入参 $RP_SESSION）"
fi
RP_SESSION=$BOT_SESSION

umask 077
touch "$ENVFILE"
set_env() {
  if grep -q "^$1=" "$ENVFILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENVFILE"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENVFILE"
  fi
}
set_env RP_DESKTOP_HOST 127.0.0.1
set_env RP_DESKTOP_PORT 8787
grep -q '^RP_DESKTOP_TOKEN=' "$ENVFILE" || set_env RP_DESKTOP_TOKEN "$(openssl rand -hex 16)"
# 老文件补行：只监听回环时，SSH 隧道进来的连接不需要再带令牌
set_env RP_DESKTOP_TRUST_LOCAL 1
set_env RP_CORE_DIR "$BOT_DIR/src/plugins/jinyu_memory/core"
# 关键：走宿主接口，不再让桌宠进程直连 QQ 主库
sed -i '/^RP_DB=/d' "$ENVFILE"
set_env RP_HOST_BASE "http://127.0.0.1:$HOST_PORT"
set_env RP_HOST_TOKEN "$HOST_TOKEN"
set_env RP_SESSION "$RP_SESSION"
chown root:root "$ENVFILE"
chmod 600 "$ENVFILE"

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
EnvironmentFile=$BOT_ENV
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
curl -s "http://127.0.0.1:8787/desktop/v1/health" || true
echo
if [ "$BOT_RESTART" = "1" ]; then
  echo "已更新 $BOT_ENV：请重启 QQ 机器人（python tools/monitor.py restart）后宿主接口才会就绪。"
fi
