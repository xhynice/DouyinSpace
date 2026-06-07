"""弹幕 DB 列表 JSON 缓存管理。

后台线程每 3 分钟检查弹幕 API，仅对正在录制的主播刷新 DB 列表缓存。
弹幕消息查询仍走 SQLite（immutable=1 模式已解决 FUSE 兼容问题）。
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

CACHE_DIR = "/data/barrage_cache"
BARRAGE_DIR = "/data/barrage"
BARRAGE_API = os.environ.get("BARRAGE_API", "http://127.0.0.1:8088")
UPDATE_INTERVAL = 180  # 3 分钟

_db_list_cache = {"data": None, "ts": 0}


def _query_api(path):
    try:
        req = urllib.request.Request(f"{BARRAGE_API}{path}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _open_conn(db_path):
    """FUSE 兼容的只读连接。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=5)
        conn.execute("SELECT 1")
        return conn
    except Exception:
        pass
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    return conn


def _atomic_write(path, data):
    """原子写入 JSON 文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# ── DB 列表 ──

def _scan_all_dbs():
    """扫描所有弹幕 DB，返回列表。"""
    dbs = []
    if not os.path.isdir(BARRAGE_DIR):
        return dbs
    for anchor_dir in os.scandir(BARRAGE_DIR):
        if not anchor_dir.is_dir():
            continue
        anchor = anchor_dir.name
        for session_dir in os.scandir(anchor_dir.path):
            if not session_dir.is_dir():
                continue
            db_path = os.path.join(session_dir.path, "sqlite.db")
            if not os.path.isfile(db_path):
                continue
            try:
                s = os.stat(db_path)
                from urllib.parse import quote
                anchor_encoded = quote(anchor, safe='/')
                dbs.append({
                    "anchor": anchor,
                    "path": db_path,
                    "session": session_dir.name,
                    "size_mb": round(s.st_size / 1048576, 2),
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(s.st_mtime)),
                    "avatar_url": f"https://huggingface.co/buckets/sunset139/douyin/resolve/barrage/{anchor_encoded}/avatar.jpg?download=true",
                    "cover_url": f"https://huggingface.co/buckets/sunset139/douyin/resolve/barrage/{anchor_encoded}/cover.jpg?download=true",
                })
            except OSError:
                pass
    dbs.sort(key=lambda x: x.get("modified", ""), reverse=True)
    return dbs


def _stat_db(db):
    """为单个 DB 查询弹幕数量。"""
    msg_count = 0
    conn = None
    try:
        conn = _open_conn(db["path"])
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for tbl in ("chat", "gift", "like", "social", "lucky_bag"):
            if tbl in existing:
                try:
                    row = conn.execute(f"SELECT MAX(rowid) FROM [{tbl}]").fetchone()
                    if row and row[0]:
                        msg_count += row[0]
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        if conn:
            conn.close()
    db["msg_count"] = msg_count
    return db


def update_db_list_cache():
    """刷新 DB 列表缓存。"""
    dbs = _scan_all_dbs()
    for db in dbs:
        _stat_db(db)
    _atomic_write(os.path.join(CACHE_DIR, "db_list.json"), dbs)
    _db_list_cache["data"] = dbs
    _db_list_cache["ts"] = time.time()
    logger.info(f"[Cache] DB 列表已更新: {len(dbs)} 个数据库")


def load_db_list():
    """读取 DB 列表（内存缓存 → JSON 文件 → 实时扫描）。"""
    now = time.time()
    if _db_list_cache["data"] and now - _db_list_cache["ts"] < 60:
        return _db_list_cache["data"]
    cache_path = os.path.join(CACHE_DIR, "db_list.json")
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _db_list_cache["data"] = data
        _db_list_cache["ts"] = now
        return data
    except Exception:
        pass
    return _scan_all_dbs()


# ── 后台更新线程 ──

def _cache_update_loop(stop_event):
    """后台更新循环。"""
    while not stop_event.is_set():
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            _do_update()
        except Exception as e:
            logger.warning(f"[Cache] 更新失败: {e}")
        stop_event.wait(UPDATE_INTERVAL)


def _do_update():
    """执行一次缓存更新：仅更新 DB 列表。"""
    rooms = _query_api("/api/rooms")
    if rooms is None:
        return
    active = False
    for room in rooms:
        if room.get("live_status") == "collecting" or room.get("is_recording"):
            active = True
            break
    if active:
        update_db_list_cache()


def start_cache_updater():
    """启动后台缓存更新线程。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    stop_event = threading.Event()
    t = threading.Thread(target=_cache_update_loop, args=(stop_event,), daemon=True)
    t.start()
    logger.info("[Cache] DB 列表缓存更新线程已启动 (间隔 3 分钟)")
    return stop_event
