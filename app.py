"""DouyinBarrage Web 控制面板。

独立 Flask 应用，不修改 DouyinBarrage 项目本身。
读取 /data 下的弹幕数据库和录制文件，提供：
  - 仪表盘：房间状态、录制状态、弹幕统计
  - 录制文件列表 + 视频播放（含弹幕同步）
  - 弹幕数据查看
  - 日志查看
  - 文件管理器（代理 dufs）
"""

import glob
import json
import logging
import os
import re
import sqlite3
import time
import collections
import ast
import urllib.request
import urllib.error
from datetime import datetime
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

import yaml
from flask import Flask, jsonify, request, render_template, Response, send_from_directory, g

# ── 日志配置 ──
LOG_DIR = "/data/logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "panel.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
)
logger = logging.getLogger("panel")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
            template_folder=os.path.join(_BASE_DIR, "templates"))

# ── 路径 ──
BARRAGE_DIR = "/data/barrage"
RECORDING_DIR = "/data/barrage"

# ── Buckets ──
HF_USER = os.environ.get("HF_USER", "sunset139")
HF_BUCKET = os.environ.get("HF_BUCKET", "Douyin-storage")
BUCKETS_BASE = f"https://huggingface.co/buckets/{HF_USER}/{HF_BUCKET}/resolve"
CDN_BASE = f"https://openw.cc.cd/buckets/{HF_USER}"
CDN_BARRAGE = f"{CDN_BASE}/{HF_BUCKET}/resolve"  # Cloudflare 代理 Douyin-storage 桶
CDN_COMMENT = f"{CDN_BASE}/douyin/resolve"  # Cloudflare 代理 douyin 桶

# ── DouyinComment ──
COMMENT_DIR = "/data2/DouyinComment/data"
COMMENT_DB = os.path.join(COMMENT_DIR, "database", "sqlite.db")
COMMENT_CACHE_JSON = "/data/comment_cache.json"
COMMENT_CACHE_TTL = 900  # mtime不变时15分钟有效
_comment_stats_cache = {"data": None, "ts": 0, "mtime": 0}
_comment_users_cache = {"data": None, "ts": 0, "mtime": 0}
BARRAGE_CACHE_TTL = 1200  # mtime不变时20分钟有效
_barrage_cache = {}  # key -> {"data": [...], "ts": float, "mtime": float}
BARRAGE_CACHE_MAX = 50  # 最多缓存50个查询

# ── 请求限流 ──
_rate_limits = {}  # ip -> deque of timestamps
RATE_LIMIT = 30  # 每分钟最多30次请求
RATE_WINDOW = 60  # 滑动窗口60秒

def _check_rate_limit(ip):
    """简单内存滑动窗口限流。"""
    now = time.time()
    if ip not in _rate_limits:
        _rate_limits[ip] = collections.deque()
    dq = _rate_limits[ip]
    while dq and dq[0] < now - RATE_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        return False
    dq.append(now)
    # 清理过期 IP（防止内存泄漏）
    if len(_rate_limits) > 500:
        for k in list(_rate_limits):
            if not _rate_limits[k] or _rate_limits[k][-1] < now - RATE_WINDOW * 2:
                del _rate_limits[k]
    return True

# ── 弹幕进程 API ──
BARRAGE_API = os.environ.get("BARRAGE_API", "http://127.0.0.1:8088")

# ── FUSE 超时保护 ──
# /data 是 FUSE 挂载，文件操作可能卡住。用线程池 + 超时保护。
_FUSE_TIMEOUT = 8  # 秒
_fuse_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='fuse')


def _fuse_safe(fn, fallback=None, timeout=_FUSE_TIMEOUT):
    """在线程池中执行 FUSE 操作，超时返回 fallback。"""
    try:
        return _fuse_pool.submit(fn).result(timeout=timeout)
    except FuturesTimeout:
        logger.warning(f"[FUSE] 操作超时 ({timeout}s): {fn.__name__ if hasattr(fn, '__name__') else 'lambda'}")
        return fallback
    except Exception as e:
        logger.warning(f"[FUSE] 操作异常: {e}")
        return fallback

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})")
_TS_RE_NEW = re.compile(r"(\d{8})_(\d{4})_\d{3}")

# ── 启动时加载 emoji ──
_emoji_data = None
def _load_emoji():
    global _emoji_data
    if _emoji_data is None:
        p = os.path.join(_BASE_DIR, "templates", "emoji.json")
        try:
            with open(p, "r", encoding="utf-8") as f:
                _emoji_data = f.read()
        except Exception:
            _emoji_data = '{"emoji_list": []}'
    return _emoji_data


# ══════════════════════════════════════
#  工具
# ══════════════════════════════════════

def buckets_url(local_path):
    """生成资源 URL：全部走 Cloudflare 代理"""
    # /data/ → Douyin-storage 桶
    if local_path.startswith("/data/"):
        rel = local_path[6:]  # 去掉 /data/ 前缀
        return CDN_BARRAGE + "/" + quote(rel, safe="/") + "?download=true"
    # /data2/ → douyin 桶
    if local_path.startswith("/data2/"):
        rel = local_path[7:]  # 去掉 /data2/ 前缀
        return CDN_COMMENT + "/" + quote(rel, safe="/") + "?download=true"
    # 其他 → CDN（兜底）
    rel = local_path.replace("/data/", "", 1)
    return BUCKETS_BASE + "/" + quote(rel, safe="/") + "?download=true"


def read_log(path, n=200):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, 8192 * n)
            f.seek(max(0, size - block))
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
            return [l + "\n" for l in lines[-n:]]
    except Exception:
        return []


def extract_start_time(path):
    basename = os.path.basename(path)
    # 旧格式: YYYY-MM-DD_HH-MM-SS
    m = _TS_RE.search(basename)
    if m:
        try:
            return int(datetime.strptime(
                f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                "%Y-%m-%d %H:%M:%S"
            ).timestamp())
        except (ValueError, TypeError):
            pass
    # 新格式: YYYYMMDD_HHMM_000
    m = _TS_RE_NEW.search(basename)
    if m:
        try:
            return int(datetime.strptime(
                f"{m.group(1)} {m.group(2)}",
                "%Y%m%d %H%M"
            ).timestamp())
        except (ValueError, TypeError):
            pass
    return None


def _scan_files(base_dir, valid_ext, cache_key, cache_ttl, stat_fn):
    """通用文件扫描+缓存（扫描两级子目录）。"""
    cache = _file_caches[cache_key]
    now = time.time()
    if now - cache["ts"] < cache_ttl and cache["data"]:
        return cache["data"]
    files = []
    try:
        # 扫描两级子目录：data/barrage/<主播>/<时间戳>/文件
        for entry in os.scandir(base_dir):
            if entry.is_dir():
                for sub in os.scandir(entry.path):
                    if sub.is_dir():
                        for f in os.scandir(sub.path):
                            if f.is_file() and os.path.splitext(f.name)[1].lower() in valid_ext:
                                try:
                                    files.append(stat_fn(f.path, f.name))
                                except OSError:
                                    pass
                    elif sub.is_file() and os.path.splitext(sub.name)[1].lower() in valid_ext:
                        try:
                            files.append(stat_fn(sub.path, sub.name))
                        except OSError:
                            pass
    except OSError:
        pass
    files.sort(key=lambda x: x.get("name", ""), reverse=True)
    if files:
        _file_caches[cache_key] = {"data": files, "ts": now}
    return files

_FILE_EXT = {'.ts', '.mp4', '.mkv', '.flv'}
_file_caches = {"recordings": {"data": [], "ts": 0}, "barrage": {"data": [], "ts": 0}}

def _stat_recording(path, name):
    s = os.stat(path)
    return {
        "path": path, "name": name,
        "size_mb": round(s.st_size / 1048576, 2),
        "modified": datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "start_ts": extract_start_time(path),
    }

def get_recordings():
    return _scan_files(RECORDING_DIR, _FILE_EXT, "recordings", 60, _stat_recording)


DB_EXT = {'.db'}
_table_cache = {}  # key -> {"tables": set, "ts": float}
_db_connections = {}  # 复用连接
_count_cache = {}  # db_path -> {"count": int, "ts": float}
DB_CONN_MAX = 10  # 最多缓存10个连接
TABLE_CACHE_TTL = 600  # 表结构缓存 10 分钟过期
COUNT_CACHE_TTL = 600  # 消息数缓存 10 分钟

def _open_db_conn(db_path):
    """打开一个新的只读数据库连接，FUSE 兼容。"""
    # 优先用 immutable 模式（FUSE 上更稳定，跳过 WAL 恢复）
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True,
                               timeout=5)
        conn.execute("PRAGMA cache_size=-32000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("SELECT 1")  # 健康检查
        return conn
    except Exception:
        pass
    # immutable 失败则退回普通只读模式
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA cache_size=-32000")
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    return conn

def _get_db_conn(db_path):
    """获取只读数据库连接（复用 + mtime 检查 + 健康检查）。"""
    try:
        current_mtime = os.path.getmtime(db_path)
    except OSError:
        current_mtime = 0
    if db_path in _db_connections:
        entry = _db_connections[db_path]
        # 文件更新过 → 关闭旧连接，重建，清弹幕缓存
        if entry.get("mtime") != current_mtime:
            try:
                entry["conn"].close()
            except Exception:
                pass
            del _db_connections[db_path]
            # 清除该 db 的弹幕查询缓存
            stale_keys = [k for k in _barrage_cache if k[0] == db_path]
            for k in stale_keys:
                del _barrage_cache[k]
            _table_cache.pop(db_path, None)
            _count_cache.pop(db_path, None)
        else:
            try:
                entry["conn"].execute("SELECT 1")
                entry["ts"] = time.time()
                return entry["conn"]
            except Exception:
                logger.warning(f"[DB] 连接失效，重建: {db_path}")
                try:
                    entry["conn"].close()
                except Exception:
                    pass
                del _db_connections[db_path]
    if len(_db_connections) >= DB_CONN_MAX:
        oldest_key = min(_db_connections, key=lambda k: _db_connections[k]["ts"])
        try:
            _db_connections[oldest_key]["conn"].close()
        except Exception:
            pass
        del _db_connections[oldest_key]
    conn = _open_db_conn(db_path)
    _db_connections[db_path] = {"conn": conn, "ts": time.time(), "mtime": current_mtime}
    return conn

def _get_existing_tables(db_path):
    """缓存表结构（带 TTL，失败不缓存空集合）。"""
    now = time.time()
    if db_path in _table_cache:
        entry = _table_cache[db_path]
        if now - entry["ts"] < TABLE_CACHE_TTL and entry["tables"]:
            return entry["tables"]
    try:
        conn = _get_db_conn(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if tables:
            _table_cache[db_path] = {"tables": tables, "ts": now}
            return tables
    except Exception as e:
        logger.warning(f"[DB] 获取表结构失败 ({db_path}): {e}")
    # 失败时不缓存，下次重试；若旧缓存未过期则继续用旧的
    if db_path in _table_cache and _table_cache[db_path]["tables"]:
        return _table_cache[db_path]["tables"]
    return set()

def _stat_barrage_db(path, name):
    s = os.stat(path)
    anchor = os.path.basename(os.path.dirname(path))
    msg_count = 0
    now = time.time()
    # 优先用缓存的消息数
    if path in _count_cache and now - _count_cache[path]["ts"] < COUNT_CACHE_TTL:
        msg_count = _count_cache[path]["count"]
    else:
        existing = _get_existing_tables(path)
        if existing:
            for attempt in range(2):
                try:
                    conn = _get_db_conn(path)
                    total = 0
                    for tbl in ("chat", "gift", "like", "social", "lucky_bag"):
                        if tbl in existing:
                            try:
                                row = conn.execute(f"SELECT COUNT(*) FROM [{tbl}]").fetchone()
                                if row and row[0]:
                                    total += row[0]
                            except Exception as e:
                                logger.debug(f"[DB] COUNT {tbl} 失败: {e}")
                    msg_count = total
                    _count_cache[path] = {"count": total, "ts": now}
                    break
                except Exception as e:
                    logger.warning(f"[DB] 读取弹幕DB失败 ({path}, 第{attempt+1}次): {e}")
                    if attempt == 0:
                        _db_connections.pop(path, None)
                        _table_cache.pop(path, None)
    return {
        "anchor": anchor, "path": path,
        "size_mb": round(s.st_size / 1048576, 2),
        "modified": datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "msg_count": msg_count,
        "avatar_url": f"{CDN_BARRAGE}/barrage/{quote(anchor, safe='/')}/avatar.jpg?download=true",
        "cover_url": f"{CDN_BARRAGE}/barrage/{quote(anchor, safe='/')}/cover.jpg?download=true",
    }

def get_barrage_dbs():
    return _scan_files(BARRAGE_DIR, DB_EXT, "barrage", 300, _stat_barrage_db)


def query_api(path):
    try:
        req = urllib.request.Request(f"{BARRAGE_API}{path}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _load_comment_cache():
    """从 JSON 缓存加载评论统计（采集后由 generate_comment_cache.py 生成）。"""
    try:
        mtime = os.path.getmtime(COMMENT_CACHE_JSON)
        with open(COMMENT_CACHE_JSON, "r", encoding="utf-8") as f:
            return json.load(f), mtime
    except Exception:
        return None, 0


def get_comment_stats():
    """获取 DouyinComment 采集统计。优先读 JSON 缓存，失败回退查 SQLite。"""
    now = time.time()
    # ── 优先读 JSON 缓存 ──
    cache_data, cache_mtime = _load_comment_cache()
    if cache_data and cache_mtime:
        if _comment_stats_cache["data"] and _comment_stats_cache["mtime"] == cache_mtime and now - _comment_stats_cache["ts"] < COMMENT_CACHE_TTL:
            return _comment_stats_cache["data"]
        stats = cache_data.get("stats", {"users": 0, "videos": 0, "comments": 0, "replies": 0})
        _comment_stats_cache["data"] = stats
        _comment_stats_cache["ts"] = now
        _comment_stats_cache["mtime"] = cache_mtime
        return stats
    # ── JSON 不存在，回退到 SQLite（首次启动或缓存生成失败时）──
    return _get_comment_stats_from_db()


def _get_comment_stats_from_db():
    """从 SQLite 直接查询评论统计（fallback）。"""
    now = time.time()
    mtimes = []
    try:
        mtimes.append(os.path.getmtime("/app/DouyinComment/config.yaml"))
    except OSError:
        mtimes.append(0)
    for db_file in sorted(glob.glob(os.path.join(COMMENT_DIR, "*", "sqlite.db"))):
        try:
            mtimes.append(os.path.getmtime(db_file))
        except OSError:
            mtimes.append(0)
    db_mtime = tuple(mtimes)
    if _comment_stats_cache["data"] and _comment_stats_cache["mtime"] == db_mtime and now - _comment_stats_cache["ts"] < COMMENT_CACHE_TTL:
        return _comment_stats_cache["data"]
    stats = {"users": 0, "videos": 0, "comments": 0, "replies": 0}
    config_path = "/app/DouyinComment/config.yaml"
    enabled_users = []
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        enabled_users = [u for u in config.get("users", []) if u.get("enabled", True) and not str(u.get("sec_uid", "")).startswith("#")]
        stats["users"] = len(enabled_users)
    except Exception as e:
        logger.warning(f"[评论统计] 读取配置失败: {e}")
    for u in enabled_users:
        user_db = os.path.join(COMMENT_DIR, u.get("sec_uid", ""), "sqlite.db")
        if not os.path.exists(user_db):
            continue
        conn = None
        try:
            conn = _open_db_conn(user_db)
            stats["videos"] += conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            stats["comments"] += conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
            stats["replies"] += conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
        except Exception as e:
            logger.warning(f"[评论统计] 查询数据库失败 ({u.get('nickname', '')}): {e}")
        finally:
            if conn:
                conn.close()
    _comment_stats_cache["data"] = stats
    _comment_stats_cache["ts"] = now
    _comment_stats_cache["mtime"] = db_mtime
    return stats


def get_latest_daily_crawl_log():
    """获取最新的 daily_crawl 日志。"""
    log_dir = "/data/logs"
    pattern = os.path.join(log_dir, "daily_crawl_*.log")
    files = sorted(glob.glob(pattern), reverse=True)
    if files:
        return read_log(files[0], 100)
    return []


def get_comment_users():
    """获取每个用户的评论采集统计。优先读 JSON 缓存，失败回退查 SQLite。"""
    now = time.time()
    cache_data, cache_mtime = _load_comment_cache()
    if cache_data and cache_mtime:
        if _comment_users_cache["data"] and _comment_users_cache["mtime"] == cache_mtime and now - _comment_users_cache["ts"] < COMMENT_CACHE_TTL:
            return _comment_users_cache["data"]
        users = cache_data.get("users", [])
        for u in users:
            u["avatar_url"] = f"{CDN_COMMENT}/DouyinComment/data/{u.get('sec_uid', '')}/avatar.jpg?download=true"
        _comment_users_cache["data"] = users
        _comment_users_cache["ts"] = now
        _comment_users_cache["mtime"] = cache_mtime
        return users
    return _get_comment_users_from_db()


def _get_comment_users_from_db():
    """从 SQLite 直接查询用户统计（fallback）。"""
    now = time.time()
    mtimes = []
    try:
        mtimes.append(os.path.getmtime("/app/DouyinComment/config.yaml"))
    except OSError:
        mtimes.append(0)
    for db_file in sorted(glob.glob(os.path.join(COMMENT_DIR, "*", "sqlite.db"))):
        try:
            mtimes.append(os.path.getmtime(db_file))
        except OSError:
            mtimes.append(0)
    cfg_mtime = tuple(mtimes)
    if _comment_users_cache["data"] and _comment_users_cache["mtime"] == cfg_mtime and now - _comment_users_cache["ts"] < COMMENT_CACHE_TTL:
        return _comment_users_cache["data"]
    users = []
    config_path = "/app/DouyinComment/config.yaml"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        for u in config.get("users", []):
            if u.get("enabled", True) and not str(u.get("sec_uid", "")).startswith("#"):
                users.append({
                    "sec_uid": u.get("sec_uid", ""),
                    "nickname": u.get("nickname", "未知"),
                    "avatar_url": f"{CDN_COMMENT}/DouyinComment/data/{u.get('sec_uid', '')}/avatar.jpg?download=true",
                    "videos": 0, "comments": 0, "replies": 0,
                    "media_downloaded": 0, "last_update": None,
                })
    except Exception as e:
        logger.warning(f"[用户统计] 读取配置失败: {e}")
    for user in users:
        user_db = os.path.join(COMMENT_DIR, user["sec_uid"], "sqlite.db")
        if not os.path.exists(user_db):
            continue
        conn = None
        try:
            conn = _open_db_conn(user_db)
            user["videos"] = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            user["comments"] = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
            user["replies"] = conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
            user["media_downloaded"] = _count_downloaded_media(conn)
            row = conn.execute("SELECT MAX(create_time) FROM videos").fetchone()
            if row and row[0]:
                user["last_update"] = datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            logger.warning(f"[用户统计] 查询数据库失败 ({user['nickname']}): {e}")
        finally:
            if conn:
                conn.close()
    _comment_users_cache["data"] = users
    _comment_users_cache["ts"] = now
    _comment_users_cache["mtime"] = cfg_mtime
    return users


def _count_downloaded_media(conn):
    """统计已下载资源数量（md5 文件名 = 已下载，URL = 未下载）。"""
    downloaded = 0
    # 单值字段：用 WHERE 过滤空值
    for sql in [
        "SELECT thumb FROM videos WHERE thumb IS NOT NULL AND thumb != '' AND thumb NOT LIKE 'http%'",
        "SELECT video FROM videos WHERE video IS NOT NULL AND video != '' AND video NOT LIKE 'http%'",
    ]:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()
            if row:
                downloaded += row[0]
        except Exception:
            pass
    # 数组字段
    for sql in [
        "SELECT images FROM videos WHERE images IS NOT NULL AND images != ''",
        "SELECT image_list FROM comments WHERE image_list IS NOT NULL AND image_list != ''",
        "SELECT image_list FROM replies WHERE image_list IS NOT NULL AND image_list != ''",
    ]:
        for row in conn.execute(sql):
            downloaded += _count_in_field(row[0])
    # avatar + sticker
    for sql in [
        "SELECT user_avatar, sticker FROM comments WHERE (user_avatar IS NOT NULL AND user_avatar != '') OR (sticker IS NOT NULL AND sticker != '')",
        "SELECT user_avatar, sticker FROM replies WHERE (user_avatar IS NOT NULL AND user_avatar != '') OR (sticker IS NOT NULL AND sticker != '')",
    ]:
        for row in conn.execute(sql):
            for field in row:
                if field and field.strip():
                    if field.strip().startswith("http"):
                        continue
                    if field.strip().startswith("["):
                        downloaded += _count_in_field(field)
                    else:
                        downloaded += 1
    return downloaded


def _count_in_field(value):
    """解析字段值，统计非 URL 的资源数量。"""
    if not value or not value.strip():
        return 0
    try:
        items = ast.literal_eval(value) if value.strip().startswith("[") else [value]
        return sum(1 for item in items if item and str(item).strip() and not str(item).strip().startswith("http"))
    except Exception as e:
        logger.debug(f"[资源统计] 解析字段失败: {e}")
        return 0


def find_db_for_video(video_path, dbs=None):
    for db in (dbs or get_barrage_dbs()):
        if db["anchor"] in video_path:
            return db["path"], db["anchor"]
    return None, None


def get_anchor_files(video_path, recordings=None):
    """Get all video recordings for the same anchor."""
    all_files = recordings if recordings is not None else get_recordings()
    # Extract anchor name from path: /data/barrage/<anchor>/...
    parts = video_path.split("/")
    anchor = ""
    for i, p in enumerate(parts):
        if p == "barrage" and i + 1 < len(parts):
            anchor = parts[i + 1]
            break
    if anchor:
        return [f for f in all_files if anchor in f["path"]]
    return all_files


def query_barrage(db_path, t_from=None, t_to=None, limit=0, cursor=0, sort="asc", types=None, user=None):
    # 安全检查：必须有时间范围，防止全表扫描
    if not t_from and not t_to and not cursor:
        logger.warning(f"[弹幕] 查询缺少时间范围，拒绝全表扫描: {db_path}")
        return []
    cache_key = (db_path, t_from, t_to, limit, cursor, sort, tuple(sorted(types)) if types else None, user)
    now = time.time()
    try:
        db_mtime = os.path.getmtime(db_path)
    except OSError:
        db_mtime = 0
    if cache_key in _barrage_cache:
        cached = _barrage_cache[cache_key]
        if cached["mtime"] == db_mtime and now - cached["ts"] < BARRAGE_CACHE_TTL:
            return cached["data"]
    conds, params = [], []
    if t_from:
        conds.append("time >= ?"); params.append(int(t_from))
    if t_to:
        conds.append("time <= ?"); params.append(int(t_to))
    if cursor and sort == "asc":
        conds.append("time >= ?"); params.append(int(cursor))
    elif cursor and sort == "desc":
        conds.append("time <= ?"); params.append(int(cursor))
    if user:
        conds.append("user_name = ?"); params.append(user)
    where = " AND ".join(conds) if conds else "1=1"

    ALL_QUERIES = {
        "chat":      f"SELECT time,'chat',user_name,content,'',grade,fans_club FROM chat WHERE {where}",
        "gift":      f"SELECT time,'gift',user_name,gift_name,CAST(gift_count AS TEXT),grade,fans_club FROM gift WHERE {where}",
        "like":      f"SELECT time,'like',user_name,CAST(count AS TEXT),'',grade,fans_club FROM [like] WHERE {where}",
        "social":    f"SELECT time,'social',user_name,action,'',grade,fans_club FROM social WHERE {where}",
        "lucky_bag": f"SELECT time,'lucky_bag',user_name,content,'',grade,fans_club FROM lucky_bag WHERE {where}",
    }

    if types:
        QUERIES = {k: v for k, v in ALL_QUERIES.items() if k in types}
    else:
        QUERIES = ALL_QUERIES

    results = []
    for attempt in range(2):
        try:
            existing = _get_existing_tables(db_path)
            conn = _get_db_conn(db_path)
            subs, sp = [], []
            for tbl, q in QUERIES.items():
                if tbl in existing:
                    subs.append(q); sp.extend(params)
            if not subs:
                return []
            limit_sql = f" LIMIT {limit}" if limit > 0 else ""
            order = "DESC" if sort == "desc" else "ASC"
            sql = " UNION ALL ".join(subs) + f" ORDER BY time {order}{limit_sql}"
            rows = list(conn.execute(sql, sp))
            if sort == "desc":
                rows.reverse()
            for r in rows:
                    t, tp, user, content, extra, grade, fans_club = r[0], r[1], r[2] or "", r[3] or "", r[4], r[5] or "", r[6] or ""
                    grade_num = ""
                    if grade:
                        m = re.search(r'\d+', grade)
                        if m:
                            grade_num = m.group()
                    if tp == "gift":
                        cnt = int(extra) if extra and extra.isdigit() and int(extra) > 1 else 0
                        content = f"🎁 {content}" + (f"x{cnt}" if cnt else "")
                    elif tp == "like":
                        content = f"👍 {extra or 1}赞"
                    elif tp == "social":
                        content = f"❤️ {content or '关注'}"
                    elif tp == "lucky_bag":
                        content = f"🧧 {content or '福袋'}"
                    results.append({
                        "time": t, "type": tp, "user": user, "content": content,
                        "grade": grade_num, "fans_club": fans_club
                    })
            break
        except Exception as e:
            logger.warning(f"[DB] 查询弹幕失败 ({db_path}, 第{attempt+1}次): {e}")
            if attempt == 0:
                _db_connections.pop(db_path, None)
                _table_cache.pop(db_path, None)
    if len(_barrage_cache) >= BARRAGE_CACHE_MAX:
        oldest_key = min(_barrage_cache, key=lambda k: _barrage_cache[k]["ts"])
        del _barrage_cache[oldest_key]
    _barrage_cache[cache_key] = {"data": results, "ts": time.time(), "mtime": db_mtime}
    return results


# ══════════════════════════════════════
#  请求日志
# ══════════════════════════════════════

@app.before_request
def _log_request_start():
    g._req_start = time.time()

@app.after_request
def _log_request(response):
    if request.path.startswith("/static") or request.path == "/favicon.ico":
        return response
    duration = time.time() - getattr(g, "_req_start", time.time())
    status = response.status_code
    level = logging.WARNING if status >= 400 else logging.INFO
    logger.log(level, "%s %s -> %s (%.1fms)", request.method, request.path, status, duration * 1000)
    return response

# ══════════════════════════════════════
#  路由
# ══════════════════════════════════════

@app.route("/")
def index():
    api_status = _fuse_safe(lambda: query_api("/api/status"))
    api_rooms = _fuse_safe(lambda: query_api("/api/rooms"))
    recordings = _fuse_safe(get_recordings, fallback=[])
    barrage_dbs = _fuse_safe(get_barrage_dbs, fallback=[])
    total_size = sum(f["size_mb"] for f in recordings)
    total_barrage_count = sum(db.get("msg_count", 0) for db in barrage_dbs)
    comment_stats = _fuse_safe(get_comment_stats, fallback={"users": 0, "videos": 0, "comments": 0, "replies": 0})
    comment_users = _fuse_safe(get_comment_users, fallback=[])
    return render_template("index.html",
        api_status=api_status,
        api_rooms=api_rooms or [],
        recordings=recordings[:50],
        barrage_dbs=barrage_dbs,
        total_size=total_size,
        total_barrage_count=total_barrage_count,
        comment_stats=comment_stats,
        comment_users=comment_users,
        logs=_fuse_safe(lambda: read_log(os.path.join(LOG_DIR, "barrage_stdout.log"), 150), fallback=[]),
        cron_logs=_fuse_safe(get_latest_daily_crawl_log, fallback=[]),
    )


@app.route("/play/<path:filepath>")
def play_page(filepath):
    if not filepath.startswith("/"):
        filepath = "/" + filepath
    if not filepath.startswith("/data/"):
        return "Forbidden", 403
    filename = os.path.basename(filepath)
    video_start = extract_start_time(filepath) or 0
    recordings = _fuse_safe(get_recordings, fallback=[])
    db_path, anchor = _fuse_safe(lambda: find_db_for_video(filepath, recordings), fallback=(None, None))
    anchor_files = _fuse_safe(lambda: get_anchor_files(filepath, recordings), fallback=[])
    # Use file mtime as video_end
    try:
        video_end = int(_fuse_safe(lambda: os.stat(filepath).st_mtime, fallback=0))
    except (OSError, TypeError):
        video_end = 0
    return render_template("play.html",
        filename=filename,
        video_url=buckets_url(filepath),
        seek_time=request.args.get("t", "0"),
        video_start=video_start,
        video_end=video_end,
        db_path=db_path or "",
        has_barrage=db_path is not None,
        anchor_files=anchor_files,
        current_path=filepath,
        anchor=anchor or "",
    )


@app.route("/api/barrage")
def api_barrage():
    if not _check_rate_limit(request.remote_addr or "unknown"):
        return jsonify({"error": "rate limited", "barrage": []}), 429
    db = request.args.get("db_path", "")
    t_from = request.args.get("time_from", type=int)
    t_to = request.args.get("time_to", type=int)
    limit = request.args.get("limit", 0, type=int)
    cursor = request.args.get("cursor", 0, type=int)
    sort = request.args.get("sort", "asc")
    types_str = request.args.get("types", "")
    types = set(types_str.split(",")) if types_str else None
    user = request.args.get("user", "").strip() or None
    if not db or not _fuse_safe(lambda: os.path.exists(db), fallback=False):
        return jsonify({"error": "not found", "barrage": []})
    results = _fuse_safe(lambda: query_barrage(db, t_from, t_to, limit=limit, cursor=cursor, sort=sort, types=types, user=user), fallback=[])
    for r in results:
        r["offset"] = max(0, r["time"] - t_from) if t_from else 0
    next_cursor = results[-1]["time"] if results else cursor
    return jsonify({"barrage": results, "count": len(results), "cursor": next_cursor})


@app.route("/api/recordings")
def api_recordings():
    if not _check_rate_limit(request.remote_addr or "unknown"):
        return jsonify({"error": "rate limited"}), 429
    return jsonify({"files": _fuse_safe(get_recordings, fallback=[])})


@app.route("/api/barrage-dbs")
def api_barrage_dbs():
    if not _check_rate_limit(request.remote_addr or "unknown"):
        return jsonify({"error": "rate limited"}), 429
    dbs = _fuse_safe(get_barrage_dbs, fallback=[])
    dbs = [{k: v for k, v in d.items() if not k.startswith("_")} for d in dbs]
    return jsonify({"dbs": dbs})


@app.route("/api/proxy/<path:path>")
def api_proxy(path):
    result = query_api(f"/{path}")
    if result is None:
        return jsonify({"error": "barrage API unavailable"}), 502
    return jsonify(result)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ── 弹幕前端展示 ──
BARRAGE_DOCS_DIR = "/app/DouyinBarrage/docs"

@app.route("/barrage/")
@app.route("/barrage/<path:filepath>")
def barrage_page(filepath=""):
    if not filepath:
        filepath = "index.html"
    return send_from_directory(BARRAGE_DOCS_DIR, filepath)


# ── 评论前端展示 ──
COMMENT_DOCS_DIR = "/app/DouyinComment/docs"

@app.route("/comment/")
@app.route("/comment/<path:filepath>")
def comment_page(filepath=""):
    if not filepath:
        filepath = "index.html"
    return send_from_directory(COMMENT_DOCS_DIR, filepath)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "favicon.ico", mimetype="image/x-icon")


@app.route("/emoji.json")
def serve_emoji():
    return _load_emoji(), 200, {"Content-Type": "application/json"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7861)
