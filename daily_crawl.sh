#!/bin/bash
###############################################################################
# DouyinComment Space 每日采集脚本
# 由 cron 每天定时触发
#
# 部署模式(与 DouyinBarrage 一致):
#   /app/DouyinComment/config.yaml  ← 从 /app/space/config.yaml 覆盖
#   /app/DouyinComment/cookie.txt   ← 从 $DOUYIN_COOKIE 注入
#   /data2/DouyinComment/data/      ← 数据持久化(挂载卷)
#
# 流程(每天 6 步):
#   1. 采集数据: 每个用户最多 N 个视频 + 其全部评论/回复,只入 DB 不下载媒体
#   2. DB 修复: 清理残留 WAL,确保 DB 完整
#   3. 直接模式迁移: 用 DB 里存的原始 URL 下载媒体 + 上传 Bucket
#   4. API 模式迁移: 通过 API 重新签名 URL,补第 3 步漏的
#   5. 构建前端数据
#   6. 生成评论统计缓存
###############################################################################

set -e

# === 防止并发执行：文件锁 ===
LOCKFILE="/tmp/daily_crawl.lock"
exec 200>"$LOCKFILE"
flock -n 200 || { echo "[$(date '+%Y-%m-%d %H:%M:%S')] daily_crawl.sh 已有进程在运行，跳过"; exit 0; }

LOG_FILE="/data/logs/daily_crawl_$(date +%Y%m%d).log"
mkdir -p "$(dirname "$LOG_FILE")"

APP_DIR="/app/DouyinComment"
WORK_DIR="/data2/DouyinComment"
DATA_DIR="$WORK_DIR/data"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# === DB 修复函数：清理残留 WAL + 完整性检测 + 碎片压缩 ===
repair_dbs() {
    log "[修复] 检查 DB 完整性..."
    local repaired=0
    local failed=0
    local vacuumed=0

    for db in "$DATA_DIR"/*/sqlite.db; do
        [ -f "$db" ] || continue
        local name
        name=$(basename "$(dirname "$db")")

        # 1. 检查是否有残留 WAL，先尝试 checkpoint 合并数据
        if [ -f "$db-wal" ] && [ -s "$db-wal" ]; then
            log "[修复] 发现残留 WAL: $name ($(stat -c%s "$db-wal") bytes)"
            local checkpoint_ok
            checkpoint_ok=$(python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('$db')
    r = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    conn.close()
    print('ok' if r and r[0] == 0 else 'fail')
except Exception as e:
    print(f'fail: {e}')
" 2>&1)
            if [ "$checkpoint_ok" = "ok" ]; then
                log "[修复] WAL checkpoint 成功: $name"
            else
                log "[修复] WAL checkpoint 失败 ($checkpoint_ok)，直接删除"
                rm -f "$db-wal" "$db-shm"
                log "[修复] 已删除 WAL/SHM: $name"
            fi
            repaired=$((repaired + 1))
        fi

        # 2. 完整性检测
        local result
        result=$(python3 -c "
import sqlite3, sys
try:
    conn = sqlite3.connect('$db')
    r = conn.execute('PRAGMA integrity_check').fetchone()
    conn.close()
    print(r[0] if r else 'error')
except Exception as e:
    print(f'error: {e}')
" 2>&1)

        if [ "$result" = "ok" ]; then
            log "[修复] ✓ $name 完整"
        else
            log "[修复] ✗ $name 损坏: $result"
            failed=$((failed + 1))
        fi

        # 3. 碎片压缩：freelist > 30% 页面时自动 VACUUM
        local vacuum_result
        vacuum_result=$(python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('$db')
    pages = conn.execute('PRAGMA page_count').fetchone()[0]
    freelist = conn.execute('PRAGMA freelist_count').fetchone()[0]
    if pages > 0 and freelist / pages >= 0.3:
        conn.execute('PRAGMA vacuum')
        conn.close()
        print(f'vacuumed: {freelist}/{pages} pages')
    else:
        conn.close()
        print('ok')
except Exception as e:
    print(f'fail: {e}')
" 2>&1)

        if echo "$vacuum_result" | grep -q "vacuumed"; then
            log "[修复] $name $vacuum_result"
            vacuumed=$((vacuumed + 1))
        elif echo "$vacuum_result" | grep -q "fail"; then
            log "[修复] $name VACUUM 失败: $vacuum_result"
        fi
    done

    log "[修复] 完成: 修复 $repaired 个, 损坏 $failed 个, 压缩 $vacuumed 个"
    return $failed
}

# === 等待指定进程完全退出 ===
wait_for_process() {
    local pattern="$1"
    local max_wait=30
    local waited=0
    while pgrep -f "$pattern" > /dev/null 2>&1; do
        if [ $waited -ge $max_wait ]; then
            log "[等待] 超时 ${max_wait}s，强制终止 $pattern"
            pkill -9 -f "$pattern" 2>/dev/null || true
            sleep 1
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
    if [ $waited -gt 0 ]; then
        log "[等待] $pattern 已退出 (等待了 ${waited}s)"
    fi
}

cd "$APP_DIR"

log "=========================================="
log "DouyinComment 每日采集开始"
log "=========================================="

# ---------------------------------------------------------------------------
# 1. 采集数据到 DB
# ---------------------------------------------------------------------------
log "[1/6] 采集数据..."
if python3 main.py --all --limit 7 >> "$LOG_FILE" 2>&1; then
    log "[1/6] 采集完成"
else
    log "[1/6] 采集失败,继续执行后续步骤"
fi

# 等待 main.py 进程完全退出（包括 atexit cleanup）
wait_for_process "python3 main.py"

# ---------------------------------------------------------------------------
# 2. DB 修复：清理残留 WAL + 完整性检测
# ---------------------------------------------------------------------------
repair_dbs || true  # 即使有损坏也继续

# ---------------------------------------------------------------------------
# 3. 直接模式迁移(用 DB 原始 URL,可能过期)
# ---------------------------------------------------------------------------
log "[2/6] 直接模式迁移..."
if python3 scripts/migrate_to_bucket.py --data-dir "$DATA_DIR" --direct --author all >> "$LOG_FILE" 2>&1; then
    log "[2/6] 直接模式完成"
else
    log "[2/6] 直接模式失败,继续执行后续步骤"
fi

# ---------------------------------------------------------------------------
# 4. API 模式迁移(重新签名 URL,补第 2 步漏的过期 URL)
# ---------------------------------------------------------------------------
log "[3/6] API 模式迁移..."
if python3 scripts/migrate_to_bucket.py --data-dir "$DATA_DIR" --author all >> "$LOG_FILE" 2>&1; then
    log "[3/6] API 模式完成"
else
    log "[3/6] API 模式失败"
fi

log "=========================================="
log "DouyinComment 每日采集结束"
log "=========================================="

# ---------------------------------------------------------------------------
# 5. 重新构建前端数据
# ---------------------------------------------------------------------------
log "[4/6] 构建弹幕前端数据..."
cd /app/DouyinBarrage
if python3 docs/build_barrage.py >> "$LOG_FILE" 2>&1; then
    log "[4/6] 弹幕前端构建完成"
else
    log "[4/6] 弹幕前端构建失败"
fi

log "[5/6] 构建评论前端数据..."
cd /app/DouyinComment
if python3 scripts/build_comment.py --sqlite --cdn "https://openw.cc.cd/buckets/sunset139/douyin/resolve" >> "$LOG_FILE" 2>&1; then
    log "[5/6] 评论前端构建完成"
else
    log "[5/6] 评论前端构建失败"
fi

# ---------------------------------------------------------------------------
# 6. 生成评论统计 JSON 缓存
# ---------------------------------------------------------------------------
log "[6/6] 生成评论统计缓存..."
cd /app
if python3 /app/generate_comment_cache.py >> "$LOG_FILE" 2>&1; then
    log "[6/6] 评论统计缓存已生成"
else
    log "[6/6] 评论统计缓存生成失败"
fi
