# -*- coding: utf-8 -*-
"""ARWI 3.0 资产风险预警看板 - Flask 主程序。

启动：
    python app.py
访问：
    http://127.0.0.1:5000
接口：
    GET  /api/data                看板数据（页面加载自动调用，必要时增量拉取）
    POST /api/refresh             手动强制刷新（绕过“当日已有数据”缓存）
    GET  /api/macro_risk          全球宏观风险仪表盘数据（VIX/MOVE/CDX-IG/CDX-HY）
    POST /api/macro_risk/manual   手动录入 MOVE/CDX-IG/CDX-HY（JSON: {"key":"move"|"cdx_ig"|"cdx_hy","value":123.4,"date":"YYYY-MM-DD"}）
    POST /api/macro_risk/cdx      （旧接口，等价于 /api/macro_risk/manual）
    GET  /health                  健康检查
"""
import logging
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, render_template, request

import db as database
from arwi_service import build_macro_risk_payload, build_payload, log, sync_data
from config import CST

app = Flask(__name__)

# 首次回填完成事件：/api/data 会等待它，避免首页首次加载拿到空数据
_ready = threading.Event()


def bootstrap():
    """启动时确保数据就绪：首次回填全量历史，之后增量。"""
    try:
        ok, msg = sync_data(force=False)
        log.info("启动数据初始化：%s", msg)
    except Exception as e:  # noqa: BLE001
        log.error("启动初始化异常：%s", e)
    finally:
        _ready.set()


def job_daily():
    """每日 09:00（北京时间，周一至周五）自动更新数据库。"""
    try:
        ok, msg = sync_data(force=True)
        log.info("每日定时任务：%s", msg)
    except Exception as e:  # noqa: BLE001
        log.error("每日定时任务异常：%s", e)


def start_scheduler():
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(
        job_daily,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0,
                    timezone="Asia/Shanghai"),
        id="daily_sync", replace_existing=True,
    )
    sched.start()
    log.info("每日定时任务已启动：周一至周五 09:00（北京时间）")
    return sched


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")})


@app.route("/api/data")
def api_data():
    if not _ready.is_set():
        _ready.wait(timeout=180)         # 等待首次回填（后台线程执行，慢网络放宽到180s）
    ok, msg = sync_data(force=False)     # 当日已有数据则不重复拉取
    payload = build_payload()
    payload["sync_message"] = msg
    return jsonify(payload)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    ok, msg = sync_data(force=True)      # 强制重新拉取最新数据
    payload = build_payload()
    payload["sync_message"] = msg
    payload["refresh_ok"] = ok
    return jsonify(payload)


@app.route("/api/macro_risk")
def api_macro_risk():
    """全球宏观风险仪表盘：VIX/MOVE + CDX 利差（手动录入）。"""
    return jsonify(build_macro_risk_payload())


@app.route("/api/macro_risk/manual", methods=["POST"])
def api_macro_risk_manual():
    """手动录入：MOVE（无公开免费源，需用户每日自行维护；因亚太先开盘，
    日期默认取“最新已收录交易日”，并允许手动指定前一日日期）。
    请求体: {"key":"move", "value": 123.4, "date":"YYYY-MM-DD"(可选,默认最新交易日)}
    """
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    if key != "move":
        return jsonify({"success": False, "error": "仅支持手动录入 move（OAS 来自 FRED 自动抓取）"}), 400
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "value 必须是数字"}), 400
    if value < 0:
        return jsonify({"success": False, "error": "value 不能为负"}), 400

    date_str = data.get("date") or database.latest_date()
    if not date_str:
        return jsonify({"success": False, "error": "数据库为空，请先同步基础数据"}), 400

    try:
        database.update_manual(date_str, key, value)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"写入失败：{e}"}), 500
    log.info("手动录入 %s=%s @%s", key, value, date_str)
    return jsonify({"success": True, "key": key, "value": value, "date": date_str})


# 向后兼容：保留旧路由（重定向到 /api/macro_risk/manual 的语义）
@app.route("/api/macro_risk/cdx", methods=["POST"])
def api_macro_risk_cdx_legacy():
    """旧接口（兼容老客户端）：等价于 /api/macro_risk/manual。"""
    return api_macro_risk_manual()


@app.route("/api/macro_risk/manual_batch", methods=["POST"])
def api_macro_risk_manual_batch():
    """批量手动录入 MOVE（无公开免费源，每日逐条录入繁琐）。
    请求体支持两种格式（自动识别）：
      {"key":"move", "records":[{"date":"YYYY-MM-DD","value":123.4}, ...]}
      {"key":"move", "csv":"date,value\\n2026-08-05,73.58\\n2026-08-06,76.1\\n..."}
    跳过空行与 # 注释行；非法行收集到 errors 不影响其他成功行。
    """
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    if key != "move":
        return jsonify({"success": False, "error": "仅支持批量录入 move"}), 400

    records = data.get("records")
    if records is None:
        # 解析 CSV/TSV 文本：每行 "日期,数值"。支持多种分隔符（逗号/制表符/分号/空格）。
        # K 线数据（如 investing.com）有 6 列 → 取前两列：日期 + 收盘价（CLOSE）。
        # 单值 "73.58"（无日期）→ 跳过。
        csv_text = data.get("csv", "")
        records = []
        for lineno, line in enumerate(csv_text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = [p.strip() for p in s.replace("\t", ",").replace(";", ",").split(",") if p.strip()]
            if len(parts) < 2:
                continue
            try:
                val = float(parts[1])                   # 第二列=数值（K 线=CLOSE）
                records.append({"date": parts[0], "value": val})
            except (TypeError, ValueError):
                continue

    if not records:
        return jsonify({"success": False, "error": "未解析到有效记录"}), 400

    ok_cnt, fail_cnt, errors = 0, 0, []
    for rec in records:
        try:
            d = rec.get("date")
            v = float(rec.get("value"))
            if not d or v < 0:
                raise ValueError("非法日期或负值")
            database.update_manual(d, "move", v)
            ok_cnt += 1
        except Exception as e:  # noqa: BLE001
            fail_cnt += 1
            errors.append({"rec": rec, "err": str(e)})
    log.info("批量录入 move: 成功 %d / 失败 %d", ok_cnt, fail_cnt)
    return jsonify({
        "success": True, "key": key, "inserted": ok_cnt, "failed": fail_cnt,
        "errors": errors[:10]   # 仅返回前 10 条错误避免响应过大
    })


if __name__ == "__main__":
    database.init_db()
    threading.Thread(target=bootstrap, daemon=True).start()
    start_scheduler()
    # debug=False：避免 reloader 双进程重复 bootstrap；threaded=True 支持并发请求
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
