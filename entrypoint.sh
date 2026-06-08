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

# 2. 拉取最新 DouyinSpace 配置
echo "[3/6] 拉取最新配置文件..."
if git clone --depth 1 https://github.com/xhynice/DouyinSpace.git /tmp/space 2>/dev/null; then
    cp /tmp/space/DouyinBarrage/config.yaml /app/DouyinBarrage/config.yaml 2>/dev/null || true
    cp /tmp/space/DouyinBarrage/rooms.txt /app/DouyinBarrage/rooms.txt 2>/dev/null || true
    cp /tmp/space/DouyinComment/config.yaml /app/DouyinComment/config.yaml 2>/dev/null || true
    cp /tmp/space/app.py /app/app.py 2>/dev/null || true
    cp /tmp/space/generate_comment_cache.py /app/generate_comment_cache.py 2>/dev/null || true
    cp -r /tmp/space/templates /app/ 2>/dev/null || true
    cp /tmp/space/daily_crawl.sh /app/daily_crawl.sh 2>/dev/null || true
    cp -r /tmp/space/static /app/ 2>/dev/null || true
    # 更新自身并重新执行
    if ! cmp -s /tmp/space/entrypoint.sh /app/entrypoint.sh 2>/dev/null; then
        cp /tmp/space/entrypoint.sh /app/entrypoint.sh
        chmod +x /app/entrypoint.sh
        rm -rf /tmp/space
        echo "[3/6] entrypoint.sh 已更新，重新执行..."
        exec /bin/bash /app/entrypoint.sh
    fi
    rm -rf /tmp/space
    echo "[3/6] 配置文件已更新"
else
    echo "[3/6] 配置拉取失败，使用现有配置"
fi

# 3. 每天凌晨 3 点运行评论采集
echo "[4/6] 配置定时任务..."
echo "0 3 * * * /bin/bash /app/daily_crawl.sh >> /data/logs/cron.log 2>&1" | crontab -
echo "[4/6] 定时任务已配置"

# ── 调试: 输出 cron 相关路径信息 ──
echo "=== [调试] 环境信息 ==="
echo "PATH=$PATH"
echo "which python3: $(which python3 2>/dev/null || echo '未找到')"
echo "python3 --version: $(python3 --version 2>&1 || echo '无法执行')"
echo "httpx location: $(python3 -c 'import httpx; print(httpx.__file__)' 2>&1 || echo 'import 失败')"
echo "=== [调试] cron 环境模拟 ==="
env -i HOME=/root PATH=/usr/bin:/bin SHELL=/bin/bash sh -c 'echo "cron PATH=$PATH"; echo "cron which python3=$(which python3 2>/dev/null || echo 未找到)"; python3 -c "import httpx; print(\"httpx OK\")" 2>&1 || echo "cron import httpx 失败"'
echo "=== [调试] 结束 ==="

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
