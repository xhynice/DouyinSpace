#!/usr/bin/env python3
"""
预生成弹幕数据缓存（录制文件 + 弹幕 DB 统计），供面板首页直接读取。
每天采集完成后由 daily_crawl.sh [7/8] 调用。

避免每次首页访问都扫 FUSE + 打开 SQLite 做 COUNT。
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

DATA_DIR = "/data/barrage"
OUTPUT_PATH = "/data/barrage_cache.json"

VIDEO_EXTS = {'.ts', '.mp4', '.mkv', '.flv'}

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})")
_TS_RE_NEW = re.compile(r"(\d{8})_(\d{4})_\d{3}")


def extract_start_time(name):
    """从文件名提取录制开始时间戳，与 app.py 一致。"""
    m = _TS_RE.search(name)
    if m:
        try:
            return int(datetime.strptime(
                f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                "%Y-%m-%d %H:%M:%S"
            ).timestamp())
        except (ValueError, TypeError):
            pass
    m = _TS_RE_NEW.search(name)
    if m:
        try:
            return int(datetime.strptime(
                f"{m.group(1)} {m.group(2)}", "%Y%m%d %H%M"
            ).timestamp())
        except (ValueError, TypeError):
            pass
    return None


def open_db_conn(db_path):
    """FUSE 兼容只读连接。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=5)
        conn.execute("SELECT 1")
        return conn
    except Exception:
        pass
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    return conn


def scan_recordings():
    """扫描录制文件（两级子目录）。"""
    files = []
    try:
        for anchor in os.scandir(DATA_DIR):
            if not anchor.is_dir():
                continue
            for sub in os.scandir(anchor.path):
                if sub.is_dir():
                    for f in os.scandir(sub.path):
                        if f.is_file() and os.path.splitext(f.name)[1].lower() in VIDEO_EXTS:
                            try:
                                s = os.stat(f.path)
                                files.append({
                                    "path": f.path, "name": f.name,
                                    "size_mb": round(s.st_size / 1048576, 2),
                                    "modified": datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M"),
                                    "start_ts": extract_start_time(f.name),
                                })
                            except OSError:
                                pass
                elif sub.is_file() and os.path.splitext(sub.name)[1].lower() in VIDEO_EXTS:
                    try:
                        s = os.stat(sub.path)
                        files.append({
                            "path": sub.path, "name": sub.name,
                            "size_mb": round(s.st_size / 1048576, 2),
                            "modified": datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            "start_ts": extract_start_time(sub.name),
                        })
                    except OSError:
                        pass
    except OSError:
        pass
    files.sort(key=lambda x: x.get("name", ""), reverse=True)
    return files


def scan_barrage_dbs():
    """扫描所有 data.db 并统计消息数。"""
    dbs = []
    try:
        for anchor in os.scandir(DATA_DIR):
            if not anchor.is_dir():
                continue
            db_path = os.path.join(anchor.path, "data.db")
            if not os.path.isfile(db_path):
                continue
            try:
                s = os.stat(db_path)
            except OSError:
                continue
            anchor_name = anchor.name
            msg_count = 0
            try:
                conn = open_db_conn(db_path)
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for tbl in ("chat", "gift", "like", "social", "lucky_bag"):
                    if tbl in tables:
                        try:
                            tbl_name = f'"{tbl}"' if tbl == 'like' else tbl
                            row = conn.execute(f"SELECT COUNT(*) FROM {tbl_name}").fetchone()
                            if row and row[0]:
                                msg_count += row[0]
                        except Exception:
                            pass
                conn.close()
            except Exception:
                pass
            dbs.append({
                "anchor": anchor_name, "path": db_path,
                "size_mb": round(s.st_size / 1048576, 2),
                "modified": datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "msg_count": msg_count,
                "avatar_url": f"https://openw.cc.cd/buckets/sunset139/Douyin-storage/resolve/barrage/{anchor_name}/avatar.jpg?download=true",
                "cover_url": f"https://openw.cc.cd/buckets/sunset139/Douyin-storage/resolve/barrage/{anchor_name}/cover.jpg?download=true",
            })
    except OSError:
        pass
    dbs.sort(key=lambda x: x.get("anchor", ""))
    return dbs


def main():
    print("[barrage_cache] 扫描录制文件...")
    recordings = scan_recordings()
    print(f"[barrage_cache] 找到 {len(recordings)} 个录制文件")

    print("[barrage_cache] 扫描弹幕 DB...")
    barrage_dbs = scan_barrage_dbs()
    print(f"[barrage_cache] 找到 {len(barrage_dbs)} 个弹幕数据库")

    cache = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "recordings": recordings,
        "barrage_dbs": barrage_dbs,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    total_msgs = sum(db["msg_count"] for db in barrage_dbs)
    total_size = sum(r["size_mb"] for r in recordings)
    print(f"[barrage_cache] 已生成: {OUTPUT_PATH}")
    print(f"[barrage_cache]   录制: {len(recordings)} 个 ({total_size:.0f}MB)")
    print(f"[barrage_cache]   弹幕: {total_msgs} 条 ({len(barrage_dbs)} DB)")


if __name__ == "__main__":
    main()
