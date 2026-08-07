# -*- coding: utf-8 -*-
"""离线演示数据工具（可选，不影响真实数据路径）。

用途：
  - 完全断网环境下预览看板界面（真实模式依赖新浪/腾讯/FRED 网络）。
  - 验证前端渲染 / 算法计算链路。

用法：
    python seed_demo.py
    python app.py        # 然后浏览器访问 http://127.0.0.1:5000

说明：
  - 写入的数据为脚本化的逼真样例（含一次“risk-off”行情剧本），非真实行情。
  - 删除数据：python -c "import sqlite3;c=sqlite3.connect('arwi.db');
    c.execute('DELETE FROM daily_metrics');c.execute('DELETE FROM meta');c.commit()"
"""
import random
import sys
from datetime import date, timedelta

import db as database

END_DATE = date(2026, 8, 5)   # 最新美盘交易日（北京时间 08-06 的“昨日”）
N_DAYS = 60
random.seed(7)


def business_days(n, end):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def build_scenario():
    """三段式剧本：平稳 → risk-off（VIX/美元/原油上行，黄金避险大涨）→ 降温。"""
    days = business_days(N_DAYS, END_DATE)
    rows = []
    for i, d in enumerate(days):
        if i < 25:      # 平稳期
            vix = random.uniform(15.5, 17.0)
            dxy = random.uniform(99.0, 100.0)
            tnx = random.uniform(4.40, 4.55)
            dfii10 = random.uniform(2.20, 2.35)
            gold = 4180 + i * 2.5 + random.uniform(-25, 25)
            oil = random.uniform(75.0, 79.0)
            move = 72 + random.uniform(-4, 4)
        elif i < 40:    # risk-off 脉冲
            p = (i - 25) / 15.0
            vix = 16.5 + p * 11 + random.uniform(-0.5, 0.5)
            dxy = 99.5 + p * 2.0 + random.uniform(-0.15, 0.15)
            tnx = 4.48 + p * 0.22 + random.uniform(-0.02, 0.02)
            dfii10 = 2.28 + p * 0.28 + random.uniform(-0.02, 0.02)
            gold = 4250 + p * 130 + random.uniform(-12, 12)
            oil = 77 + p * 8 + random.uniform(-0.5, 0.5)
            move = 76 + p * 38 + random.uniform(-3, 3)      # 债市波动随避险上升
        else:           # 降温期（回到当前真实区间：vix~15.8、dxy~99.7、gold~4260、oil~80）
            q = (i - 40) / 19.0
            vix = 27.5 - q * 11.5 + random.uniform(-0.4, 0.4)
            dxy = 101.5 - q * 1.7 + random.uniform(-0.12, 0.12)
            tnx = 4.70 - q * 0.07 + random.uniform(-0.015, 0.015)
            dfii10 = 2.56 - q * 0.16 + random.uniform(-0.015, 0.015)
            gold = 4380 - q * 110 + random.uniform(-12, 12)
            oil = 85 - q * 5 + random.uniform(-0.4, 0.4)
            move = 80 - q * 6 + random.uniform(-2, 2)
        rows.append({
            "date": d.isoformat(), "dxy": round(dxy, 3), "vix": round(vix, 2),
            "tnx": round(tnx, 4), "dfii10": round(dfii10, 4),
            "gold": round(gold, 2), "oil": round(oil, 2),
            "move": round(move, 2),
            "status": "ok", "updated_at": "demo",
        })
    # 最后一天贴合 2026-08-05 真实收盘价（investing.com）
    rows[-1]["dxy"] = 99.75
    rows[-1]["vix"] = 15.82
    rows[-1]["tnx"] = 4.63
    rows[-1]["dfii10"] = 2.40
    rows[-1]["gold"] = 4261.74
    rows[-1]["oil"] = 80.10
    rows[-1]["move"] = 73.58
    return rows


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="ARWI 离线演示数据工具")
    ap.add_argument("--move-only", action="store_true",
                    help="仅写入 MOVE 演示数据（60 天），不覆盖真实六因子数据")
    args = ap.parse_args()

    database.init_db()
    records = build_scenario()
    if args.move_only:
        # 仅更新 move 列：保留真实六因子，MOVE 用演示曲线预览
        with database.get_conn() as conn:
            for rec in records:
                conn.execute("UPDATE daily_metrics SET move=? WHERE date=?",
                             (rec["move"], rec["date"]))
        print(f"已写入 MOVE 演示数据（{records[0]['date']} ~ {records[-1]['date']}，60 天）。")
        print("MOVE 为演示曲线（investing.com 在数据中心 IP 下被反爬，住宅网络可自动抓取真实数据）。")
    else:
        database.upsert_rows(records)
        database.set_meta("real_rate_substituted", "0")
        for key in ("dxy", "vix", "tnx", "dfii10", "gold", "oil"):
            database.reset_fail(key)
        n = database.count_rows()
        print(f"已写入 {n} 天演示数据（{records[0]['date']} ~ {records[-1]['date']}）。")
        print("现在运行  python app.py  并访问  http://127.0.0.1:5000  即可预览看板。")
