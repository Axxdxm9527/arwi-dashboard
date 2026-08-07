# -*- coding: utf-8 -*-
"""ARWI 静态快照导出：抓取最新数据 → 渲染 index.html → 内联 JSON → 输出 dist/index.html

用途：
    GitHub Actions 每日定时运行本脚本，生成静态快照推送到 GitHub Pages。
    （静态页面无法请求 Flask 后端，因此把 /api/data 与 /api/macro_risk 的
    JSON 直接内联进 HTML，前端检测到 window.__INLINE_DATA__ 后免请求渲染。）

用法：
    python export_static.py [--no-fetch] [--out dist]

    --no-fetch   跳过实时抓取，直接用现有 arwi.db 数据导出（调试用）
    --out        输出目录（默认 dist）
"""
import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db as database
from arwi_service import build_macro_risk_payload, build_payload, sync_data

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("export_static")

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "templates", "index.html")
INLINE_MARK = "window.__INLINE_DATA__ = null;   // INLINE_DATA_PLACEHOLDER"


def render_static_html(data_payload, macro_payload, generated_at):
    """渲染 index.html 并把两份 JSON 内联进 __INLINE_DATA__。"""
    with open(TEMPLATE, "r", encoding="utf-8") as f:
        html = f.read()

    inline = {
        "generated_at": generated_at,
        "data": data_payload,
        "macro": macro_payload,
    }
    inject = (
        "window.__INLINE_DATA__ = "
        + json.dumps(inline, ensure_ascii=False, separators=(",", ":"))
        + ";"
    )
    # 模板里预留的占位（如果没有占位则插入到 </head> 前）
    if INLINE_MARK in html:
        html = html.replace(INLINE_MARK, inject)
    else:
        marker = "</head>"
        html = html.replace(marker, f"<script>{inject}</script>{marker}", 1)
    return html


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-fetch", action="store_true", help="跳过实时抓取")
    parser.add_argument("--out", default="dist", help="输出目录")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 1) 抓取最新数据（--no-fetch 跳过，直接用 DB）
    if not args.no_fetch:
        log.info("开始实时抓取…（investing.com 在云环境可能失败，会自动降级沿用 DB）")
        try:
            ok, msg = sync_data(force=True)
            log.info("sync 结果: %s | %s", ok, msg)
        except Exception as e:  # noqa: BLE001
            log.warning("sync_data 异常（继续用 DB 导出）: %s", e)

    # 2) 组装两份载荷
    data_payload = build_payload()
    macro_payload = build_macro_risk_payload()
    data_payload.setdefault("success", True)
    macro_payload.setdefault("success", True)

    # 3) 渲染 + 内联
    html = render_static_html(data_payload, macro_payload, generated_at)
    out_path = os.path.join(args.out, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 4) 复制静态资源（echarts 等）到输出目录
    src_static = os.path.join(HERE, "static")
    dst_static = os.path.join(args.out, "static")
    if os.path.isdir(src_static):
        shutil.copytree(src_static, dst_static, dirs_exist_ok=True)
        log.info("静态资源已复制: %s -> %s", src_static, dst_static)

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    log.info("静态快照已生成: %s (%.2f MB) | 数据日期: %s",
             out_path, size_mb, data_payload.get("data_date"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
