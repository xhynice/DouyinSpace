#!/usr/bin/env python3
"""CSV → SQLite 转换脚本：将每个主播的 CSV 会话目录合并为一个 data.db。

DB 结构与 DouyinBarrage 原生 SQLite 完全一致（表名、字段、类型、索引）。

目录结构:
    /data/barrage/{主播名}/{会话1}/chat.csv, gift.csv, ...
    /data/barrage/{主播名}/{会话2}/chat.csv, gift.csv, ...
    → 合并为:
    /data/barrage/{主播名}/data.db

增量更新: 每个已导入的会话目录名记录在 _imported_sessions 表中，
重复运行时跳过已导入的会话。

用法:
    python csv_to_sqlite.py [--data-dir /data/barrage] [--anchor 主播名]
"""

import argparse
import csv
import glob
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger('csv2db')

# ── 与 DouyinBarrage base/output.py 完全一致的表结构 ──

CSV_FIELDS = {
    'chat':      ['time', 'user_id', 'user_name', 'content', 'grade', 'fans_club'],
    'lucky_bag': ['time', 'user_id', 'user_name', 'content', 'grade', 'fans_club'],
    'gift':      ['time', 'user_id', 'user_name', 'gift_name', 'gift_count', 'diamond_total', 'grade', 'fans_club'],
    'like':      ['time', 'user_id', 'user_name', 'count', 'total', 'grade', 'fans_club'],
    'member':    ['time', 'user_id', 'user_name', 'gender', 'grade', 'fans_club', 'member_count'],
    'social':    ['time', 'user_id', 'user_name', 'action', 'follow_count', 'grade', 'fans_club'],
    'fansclub':  ['time', 'user_id', 'user_name', 'type', 'content', 'grade', 'fans_club'],
    'emoji':     ['time', 'user_id', 'user_name', 'emoji_id', 'content', 'grade', 'fans_club'],
    'stats':     ['time', 'current', 'total_pv', 'total_user', 'online_anchor'],
    'roomstats': ['time', 'detail', 'total'],
    'room':      ['time', 'is_top', 'room_id', 'content', 'biz_scene'],
    'rank':      ['time', 'ranks'],
    'control':   ['time', 'status'],
}

INTEGER_FIELDS = {
    'gift':   {'gift_count', 'diamond_total'},
    'like':   {'count', 'total'},
    'member': {'member_count'},
    'social': {'follow_count'},
    'stats':  {'current'},
}

# 会话目录名模式: YYYYMMDD_HHMM 或旧格式
SESSION_DIR_RE = re.compile(r'^\d{8}_\d{4}')


def parse_time_to_unix(time_str):
    """将 CSV 中的时间字符串转为 Unix 时间戳（秒）。

    支持格式: '2026-06-27 14:30:00' 或 '2026-06-27 14:30'
    """
    if not time_str:
        return None
    time_str = str(time_str).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return int(datetime.strptime(time_str, fmt).timestamp())
        except (ValueError, TypeError):
            continue
    return None


def init_db(db_path):
    """创建与 DouyinBarrage 一致的表结构。"""
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA cache_size=-8000')
    conn.execute('PRAGMA temp_store=MEMORY')

    for msg_type, fields in CSV_FIELDS.items():
        int_fields = INTEGER_FIELDS.get(msg_type, set())
        col_defs = []
        for f in fields:
            col_type = 'INTEGER' if (f in int_fields or f == 'time') else 'TEXT'
            col_defs.append(f'"{f}" {col_type}')
        all_cols = 'id INTEGER PRIMARY KEY AUTOINCREMENT, ' + ', '.join(col_defs)

        table = f'"{msg_type}"' if msg_type == 'like' else msg_type
        conn.execute(f'CREATE TABLE IF NOT EXISTS {table} ({all_cols})')
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{msg_type}_time ON {table}("time")')

    # 导入跟踪表
    conn.execute('''CREATE TABLE IF NOT EXISTS _imported_sessions (
        session_dir TEXT PRIMARY KEY,
        imported_at INTEGER NOT NULL
    )''')
    conn.commit()
    return conn


def get_imported_sessions(conn):
    """获取已导入的会话目录名集合。"""
    rows = conn.execute('SELECT session_dir FROM _imported_sessions').fetchall()
    return {r[0] for r in rows}


def import_session_csv(conn, session_dir_path, session_dir_name):
    """导入一个会话目录的所有 CSV 文件到数据库。

    Returns:
        dict: {type: count} 导入的消息计数
    """
    stats = {}
    for msg_type, fields in CSV_FIELDS.items():
        csv_path = os.path.join(session_dir_path, f'{msg_type}.csv')
        if not os.path.isfile(csv_path):
            continue

        int_fields = INTEGER_FIELDS.get(msg_type, set())
        table = f'"{msg_type}"' if msg_type == 'like' else msg_type
        placeholders = ', '.join(['?'] * len(fields))
        sql = f'INSERT INTO {table} ({", ".join(fields)}) VALUES ({placeholders})'

        rows = []
        try:
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    converted = []
                    for field in fields:
                        val = row.get(field, '')
                        if field == 'time':
                            # CSV 存字符串，SQLite 存 Unix 时间戳
                            unix_ts = parse_time_to_unix(val)
                            converted.append(unix_ts)
                        elif field in int_fields:
                            if val is None or val == '':
                                converted.append(None)
                            else:
                                try:
                                    converted.append(int(val))
                                except (ValueError, TypeError):
                                    converted.append(None)
                        else:
                            converted.append(str(val) if val is not None else '')
                    rows.append(tuple(converted))
        except Exception as e:
            logger.warning(f"  读取失败 {csv_path}: {e}")
            continue

        if rows:
            try:
                conn.executemany(sql, rows)
                stats[msg_type] = len(rows)
            except Exception as e:
                logger.warning(f"  写入失败 {table} ({session_dir_name}): {e}")

    if stats:
        conn.execute(
            'INSERT OR REPLACE INTO _imported_sessions (session_dir, imported_at) VALUES (?, ?)',
            (session_dir_name, int(time.time()))
        )
        conn.commit()

    return stats


def process_anchor(anchor_dir, dry_run=False):
    """处理一个主播目录：扫描 CSV 会话，合并到 data.db。"""
    anchor_name = os.path.basename(anchor_dir)
    db_path = os.path.join(anchor_dir, 'data.db')

    # 扫描会话目录
    session_dirs = []
    for d in sorted(os.listdir(anchor_dir)):
        full = os.path.join(anchor_dir, d)
        if os.path.isdir(full) and SESSION_DIR_RE.match(d):
            # 确认目录里有 CSV 文件
            if glob.glob(os.path.join(full, '*.csv')):
                session_dirs.append(d)

    if not session_dirs:
        logger.info(f"[{anchor_name}] 无 CSV 会话目录，跳过")
        return

    if dry_run:
        logger.info(f"[{anchor_name}] 发现 {len(session_dirs)} 个会话 (dry-run)")
        for s in session_dirs:
            logger.info(f"  {s}")
        return

    # 打开/创建数据库
    conn = init_db(db_path)
    imported = get_imported_sessions(conn)

    pending = [s for s in session_dirs if s not in imported]
    if not pending:
        logger.info(f"[{anchor_name}] 所有 {len(session_dirs)} 个会话已导入，跳过")
        conn.close()
        return

    logger.info(f"[{anchor_name}] {len(imported)} 已导入, {len(pending)} 待处理")

    total_msgs = 0
    for session_name in pending:
        session_path = os.path.join(anchor_dir, session_name)
        stats = import_session_csv(conn, session_path, session_name)
        count = sum(stats.values())
        total_msgs += count
        if stats:
            detail = ', '.join(f'{k}:{v}' for k, v in sorted(stats.items()))
            logger.info(f"  {session_name}: {count} 条 ({detail})")
        else:
            logger.info(f"  {session_name}: 空")

    conn.close()
    logger.info(f"[{anchor_name}] 完成, 新增 {total_msgs} 条")


def main():
    parser = argparse.ArgumentParser(description='CSV → SQLite 转换（DouyinBarrage 兼容格式）')
    parser.add_argument('--data-dir', default='/data/barrage', help='弹幕数据根目录')
    parser.add_argument('--anchor', default='', help='只处理指定主播（目录名）')
    parser.add_argument('--dry-run', action='store_true', help='只扫描不写入')
    args = parser.parse_args()

    if not os.path.isdir(args.data_dir):
        logger.error(f"数据目录不存在: {args.data_dir}")
        sys.exit(1)

    if args.anchor:
        anchor_dir = os.path.join(args.data_dir, args.anchor)
        if not os.path.isdir(anchor_dir):
            logger.error(f"主播目录不存在: {anchor_dir}")
            sys.exit(1)
        process_anchor(anchor_dir, args.dry_run)
    else:
        count = 0
        for d in sorted(os.listdir(args.data_dir)):
            anchor_dir = os.path.join(args.data_dir, d)
            if os.path.isdir(anchor_dir):
                process_anchor(anchor_dir, args.dry_run)
                count += 1
        logger.info(f"共处理 {count} 个主播目录")


if __name__ == '__main__':
    main()
