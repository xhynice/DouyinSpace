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
# 流程(每天 3 步):
#   1. 采集数据:每个用户最多 10 个视频 + 其全部评论/回复,只入 DB 不下载媒体
#   2. 直接模式迁移:用 DB 里存的原始 URL 下载媒体 + 上传 Bucket(快,可能漏过期 URL)
#   3. API 模式迁移:通过 API 重新签名 URL,下载 + 上传 Bucket,补第 2 步漏的
###############################################################################

set -e

LOG_FILE="/data2/logs/daily_crawl_$(date +%Y%m%d).log"
mkdir -p "$(dirname "$LOG_FILE")"

APP_DIR="/app/DouyinComment"
WORK_DIR="/data2/DouyinComment"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

cd "$APP_DIR"

log "=========================================="
log "DouyinComment 每日采集开始"
log "=========================================="

# ---------------------------------------------------------------------------
# 1. 采集数据到 DB(config.yaml 的 data_dir 决定写入位置)
# ---------------------------------------------------------------------------
log "[1/3] 采集数据..."
if python main.py --all --limit 10 >> "$LOG_FILE" 2>&1; then
    log "[1/3] 采集完成"
else
    log "[1/3] 采集失败,继续执行后续步骤"
fi

# ---------------------------------------------------------------------------
# 2. 直接模式迁移(用 DB 原始 URL,可能过期)
# ---------------------------------------------------------------------------
log "[2/3] 直接模式迁移..."
if python scripts/migrate_to_bucket.py --data-dir "$WORK_DIR/data" --direct --author all >> "$LOG_FILE" 2>&1; then
    log "[2/3] 直接模式完成"
else
    log "[2/3] 直接模式失败,继续执行后续步骤"
fi

# ---------------------------------------------------------------------------
# 3. API 模式迁移(重新签名 URL,补第 2 步漏的过期 URL)
# ---------------------------------------------------------------------------
log "[3/3] API 模式迁移..."
if python scripts/migrate_to_bucket.py --data-dir "$WORK_DIR/data" --author all >> "$LOG_FILE" 2>&1; then
    log "[3/3] API 模式完成"
else
    log "[3/3] API 模式失败"
fi

log "=========================================="
log "DouyinComment 每日采集结束"
log "=========================================="
