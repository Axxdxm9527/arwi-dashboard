# -*- coding: utf-8 -*-
"""SQLite 存储层：daily_metrics（每日因子数据）+ meta（源状态/失败计数）。"""
import os
import sqlite3
from datetime import datetime

from config import DB_PATH

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, DB_PATH)


def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建表（幂等）。"""
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS daily_metrics (
                date       TEXT PRIMARY KEY,
                dxy        REAL,
                vix        REAL,
                tnx        REAL,
                dfii10     REAL,
                gold       REAL,
                oil        REAL,
                move       REAL,              -- ICE BofA MOVE 债券波动率（手动录入）
                oas_ig     REAL,              -- FRED BAMLC0A0CM 投资级 OAS (bp)
                oas_hy     REAL,              -- FRED BAMLH0A0HYM2 高收益 OAS (bp)
                status     TEXT DEFAULT 'ok',   -- ok / stale(待更新)
                updated_at TEXT
            )"""
        )
        # 兼容旧库：补充新增列（旧列 cdx_ig/cdx_hy 已被 oas_ig/oas_hy 取代，保留不删以免损坏旧数据）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(daily_metrics)")}
        for col in ("move", "oas_ig", "oas_hy"):
            if col not in cols:
                conn.execute(f"ALTER TABLE daily_metrics ADD COLUMN {col} REAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )


def last_n(n):
    """取最近 n 个交易日（升序返回）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_metrics ORDER BY date DESC LIMIT ?", (n,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def latest_date():
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(date) d FROM daily_metrics").fetchone()
    return row["d"] if row else None


def count_rows():
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM daily_metrics").fetchone()
    return row["c"]


def upsert_rows(records):
    """批量写入（同日期覆盖更新）。"""
    if not records:
        return
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO daily_metrics
               (date, dxy, vix, tnx, dfii10, gold, oil, move,
                oas_ig, oas_hy, status, updated_at)
               VALUES (:date, :dxy, :vix, :tnx, :dfii10, :gold, :oil, :move,
                       :oas_ig, :oas_hy, :status, :updated_at)""",
            records,
        )


def update_manual(date_str, key, value):
    """手动录入（无公开免费源，需用户每日自行维护）。
    支持 key: move。写入指定交易日行；行不存在则创建。"""
    if key != "move":
        raise ValueError(f"unsupported manual key: {key}")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT date FROM daily_metrics WHERE date=?", (date_str,)
        ).fetchone()
        if row:
            conn.execute(
                f"UPDATE daily_metrics SET {key}=?, updated_at=? WHERE date=?",
                (value, now_str, date_str),
            )
        else:
            conn.execute(
                f"INSERT INTO daily_metrics (date, {key}, status, updated_at) "
                f"VALUES (?, ?, 'manual', ?)",
                (date_str, value, now_str),
            )
    return True


def prune(keep=420):
    """清理过旧数据，控制库体积（保留 ≥420 天，保证长周期均线计算）。"""
    with get_conn() as conn:
        conn.execute(
            """DELETE FROM daily_metrics WHERE date NOT IN (
                   SELECT date FROM daily_metrics ORDER BY date DESC LIMIT ?
               )""",
            (keep,),
        )


def set_meta(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value))
        )


def get_meta(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def bump_fail(key, delta=1):
    """源失败计数 +delta（>=0）。"""
    cur = int(get_meta("fail_" + key, 0) or 0)
    set_meta("fail_" + key, max(0, cur + delta))


def reset_fail(key):
    set_meta("fail_" + key, 0)


def get_fail(key):
    return int(get_meta("fail_" + key, 0) or 0)
