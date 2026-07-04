#!/bin/bash
echo "=== 抖音弹幕采集 HF Space ==="
mkdir -p /data/barrage /data/logs

# ── 拉取代码并显示版本信息 ──
pull_repo() {
    local name="$1" dir="$2"
    local before after
    before=$(git -C "$dir" log --oneline -1 2>/dev/null || echo "未知")
    if git -C "$dir" pull origin main 2>&1; then
        after=$(git -C "$dir" log --oneline -1 2>/dev/null || echo "未知")
        if [ "$before" = "$after" ]; then
            echo "  → 已是最新: $after"
        else
            echo "  → 已更新: $after"
        fi
    else
        echo "  → 拉取失败，使用现有代码: $before"
    fi
}

echo "[1/6] 拉取弹幕采集最新代码..."
pull_repo "弹幕" /app/DouyinBarrage

echo "[2/6] 拉取评论采集最新代码..."
pull_repo "评论" /app/DouyinComment

# 3. 拉取 DouyinSpace 最新代码
echo "[3/6] 拉取 DouyinSpace 最新代码..."
if git clone --depth 1 https://github.com/xhynice/DouyinSpace.git /tmp/space 2>/dev/null; then
    cp /tmp/space/DouyinBarrage/config.yaml /app/DouyinBarrage/config.yaml 2>/dev/null || true
    cp /tmp/space/DouyinBarrage/rooms.txt /app/DouyinBarrage/rooms.txt 2>/dev/null || true
    cp /tmp/space/DouyinComment/config.yaml /app/DouyinComment/config.yaml 2>/dev/null || true
    cp /tmp/space/app.py /app/app.py 2>/dev/null || true
    cp /tmp/space/generate_comment_cache.py /app/generate_comment_cache.py 2>/dev/null || true
    cp /tmp/space/generate_barrage_cache.py /app/generate_barrage_cache.py 2>/dev/null || true
    cp -r /tmp/space/templates /app/ 2>/dev/null || true
    cp /tmp/space/daily_crawl.sh /app/daily_crawl.sh 2>/dev/null || true
    cp -r /tmp/space/static /app/ 2>/dev/null || true
    # 更新自身并重新执行
    if ! cmp -s /tmp/space/entrypoint.sh /app/entrypoint.sh 2>/dev/null; then
        cp /tmp/space/entrypoint.sh /app/entrypoint.sh
        chmod +x /app/entrypoint.sh
        rm -rf /tmp/space
        echo "  → entrypoint.sh 已更新，重新执行..."
        exec /bin/bash /app/entrypoint.sh
    fi
    rm -rf /tmp/space
    echo "  → DouyinSpace 代码已更新"
else
    echo "[3/6] 拉取失败，使用现有代码"
fi

# 3.5 确保 gunicorn 已安装（web 面板多线程运行）
if ! command -v gunicorn &>/dev/null; then
    echo "  → 安装 gunicorn..."
    pip install gunicorn -q 2>&1 || echo "  → gunicorn 安装失败，将使用 Flask 内置 server"
fi

# 4. 配置定时任务（cron 环境极简，需显式传入 PATH 和 HF_TOKEN）
echo "[4/6] 配置定时任务..."
cat <<EOF | crontab -
PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin
HF_TOKEN=$HF_TOKEN
0 6 * * * /bin/bash /app/daily_crawl.sh &>/dev/null
EOF
echo "[4/6] 定时任务已配置"

# 5. 注入 Cookie
if [ -n "$DOUYIN_COOKIE" ]; then
    echo "$DOUYIN_COOKIE" > /app/DouyinBarrage/cookie.txt
    echo "$DOUYIN_COOKIE" > /app/DouyinComment/cookie.txt
    echo "[5/6] Cookie 已注入（弹幕+评论）"
else
    echo "[5/6] 未设置 DOUYIN_COOKIE，跳过注入"
fi

# 检查必要 Secret
if [ -z "$DUFS_PASSWORD" ]; then
    echo "[错误] 未设置 DUFS_PASSWORD Secret，无法启动文件管理器"
    exit 1
fi
if [ -z "$HF_TOKEN" ]; then
    echo "[警告] 未设置 HF_TOKEN Secret，每日采集上传将失败"
fi

# 6. 启动服务
echo "[6/6] 启动 supervisord..."

exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
