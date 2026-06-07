#!/usr/bin/env python3
"""采集完成后生成评论统计 JSON 缓存，供面板直接读取，避免 FUSE 上频繁打开 SQLite。"""

import ast
import json
import os
import sqlite3
import sys
from datetime import datetime

import yaml

COMMENT_DIR = "/data2/DouyinComment/data"
CONFIG_PATH = "/app/DouyinComment/config.yaml"
OUTPUT_PATH = "/data/comment_cache.json"


def _count_in_field(value):
    if not value or not str(value).strip():
        return 0
    try:
        items = ast.literal_eval(value) if str(value).strip().startswith("[") else [value]
        return sum(1 for item in items if item and str(item).strip() and not str(item).strip().startswith("http"))
    except Exception:
        return 0


def _count_downloaded_media(conn):
    downloaded = 0
    for row in conn.execute("SELECT thumb, video FROM videos"):
        for field in row:
            if field and field.strip() and not field.strip().startswith("http"):
                downloaded += 1
    for sql in [
        "SELECT images FROM videos WHERE images IS NOT NULL AND images != ''",
        "SELECT image_list FROM comments WHERE image_list IS NOT NULL AND image_list != ''",
        "SELECT image_list FROM replies WHERE image_list IS NOT NULL AND image_list != ''",
    ]:
        for row in conn.execute(sql):
            downloaded += _count_in_field(row[0])
    for sql in [
        "SELECT user_avatar, sticker FROM comments",
        "SELECT user_avatar, sticker FROM replies",
    ]:
        for row in conn.execute(sql):
            for field in row:
                if field and str(field).strip() and not str(field).strip().startswith("http"):
                    if str(field).strip().startswith("["):
                        downloaded += _count_in_field(field)
                    else:
                        downloaded += 1
    return downloaded


def _open_conn(db_path):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=5)
        conn.execute("SELECT 1")
        return conn
    except Exception:
        pass
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    return conn


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else CONFIG_PATH
    comment_dir = sys.argv[2] if len(sys.argv) > 2 else COMMENT_DIR
    output_path = sys.argv[3] if len(sys.argv) > 3 else OUTPUT_PATH

    enabled_users = []
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        enabled_users = [
            u for u in config.get("users", [])
            if u.get("enabled", True) and not str(u.get("sec_uid", "")).startswith("#")
        ]
    except Exception as e:
        print(f"[ERROR] 读取配置失败: {e}", file=sys.stderr)

    stats = {"users": len(enabled_users), "videos": 0, "comments": 0, "replies": 0}
    users = []

    for u in enabled_users:
        sec_uid = u.get("sec_uid", "")
        nickname = u.get("nickname", "未知")
        user_db = os.path.join(comment_dir, sec_uid, "sqlite.db")
        avatar_url = f"https://huggingface.co/buckets/sunset139/douyin/resolve/DouyinComment/data/{sec_uid}/avatar.jpg"

        user_entry = {
            "sec_uid": sec_uid,
            "nickname": nickname,
            "avatar_url": avatar_url,
            "videos": 0,
            "comments": 0,
            "replies": 0,
            "media_downloaded": 0,
            "last_update": None,
        }

        if not os.path.exists(user_db):
            users.append(user_entry)
            continue

        conn = None
        try:
            conn = _open_conn(user_db)
            user_entry["videos"] = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            user_entry["comments"] = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
            user_entry["replies"] = conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
            user_entry["media_downloaded"] = _count_downloaded_media(conn)
            row = conn.execute("SELECT MAX(create_time) FROM videos").fetchone()
            if row and row[0]:
                user_entry["last_update"] = datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            print(f"[WARN] 查询失败 ({nickname}): {e}", file=sys.stderr)
        finally:
            if conn:
                conn.close()

        stats["videos"] += user_entry["videos"]
        stats["comments"] += user_entry["comments"]
        stats["replies"] += user_entry["replies"]
        users.append(user_entry)

    cache = {"stats": stats, "users": users}
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"[OK] 评论缓存已生成: {output_path} ({len(users)} 用户, "
          f"{stats['videos']} 视频, {stats['comments']} 评论, {stats['replies']} 回复)")


if __name__ == "__main__":
    main()
