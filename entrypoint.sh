#!/bin/bash
echo "=== 抖音弹幕采集 HF Space ==="
mkdir -p /data/barrage /data/logs

# 1. 拉取最新代码
echo "[1/6] 拉取弹幕采集最新代码..."
cd /app/DouyinBarrage && git pull origin main || echo "[1/6] 弹幕采集拉取失败，使用现有代码"

echo "[2/6] 拉取评论采集最新代码..."
cd /app/DouyinComment && git pull origin main || echo "[2/6] 评论采集拉取失败，使用现有代码"

# 2. 拉取最新 DouyinSpace 配置
echo "[3/6] 拉取最新配置文件..."
if git clone --depth 1 https://github.com/xhynice/DouyinSpace.git /tmp/space 2>/dev/null; then
    cp /tmp/space/DouyinBarrage/config.yaml /app/DouyinBarrage/config.yaml 2>/dev/null || true
    cp /tmp/space/DouyinBarrage/rooms.txt /app/DouyinBarrage/rooms.txt 2>/dev/null || true
    cp /tmp/space/DouyinComment/config.yaml /app/DouyinComment/config.yaml 2>/dev/null || true
    rm -rf /tmp/space
    echo "[3/6] 配置文件已更新"
else
    echo "[3/6] 配置拉取失败，使用现有配置"
fi

# 3. 每天凌晨 3 点运行评论采集
echo "[4/6] 配置定时任务..."
echo "0 3 * * * /bin/bash /app/daily_crawl.sh >> /data/logs/cron.log 2>&1" | crontab -
echo "[4/6] 定时任务已配置"

# 4. 注入 Cookie
if [ -n "$DOUYIN_COOKIE" ]; then
    echo "$DOUYIN_COOKIE" > /app/DouyinBarrage/cookie.txt
    echo "[5/6] 弹幕采集 Cookie 已注入"
    echo "$DOUYIN_COOKIE" > /app/DouyinComment/cookie.txt
    echo "[5/6] 评论采集 Cookie 已注入"
else
    echo "[5/6] 未设置 DOUYIN_COOKIE，跳过注入"
fi

# 检查 DUFS_PASSWORD
if [ -z "$DUFS_PASSWORD" ]; then
    echo "[错误] 未设置 DUFS_PASSWORD Secret，无法启动文件管理器"
    exit 1
fi

# 5. 启动服务
echo "[6/6] 启动 supervisord..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
