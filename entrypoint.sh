#!/bin/bash
set -e
echo "=== DouyinBarrage HF Space ==="
mkdir -p /data/barrage /data/logs

# 拉取最新代码
echo "[update] Pulling latest code from GitHub..."
cd /app/DouyinBarrage && git pull origin main || echo "[update] git pull failed, continuing with existing code"

# 用 space 分支的配置覆盖主分支默认值
cp /app/rooms.txt /app/DouyinBarrage/rooms.txt 2>/dev/null
cp /app/config.yaml /app/DouyinBarrage/config.yaml 2>/dev/null

[ -n "$DOUYIN_COOKIE" ] && echo "$DOUYIN_COOKIE" > /app/DouyinBarrage/cookie.txt && echo "[init] Cookie injected"
export DUFS_PASSWORD="${DUFS_PASSWORD:?请设置 DUFS_PASSWORD Secret}"
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
