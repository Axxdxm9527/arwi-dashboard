# -*- coding: utf-8 -*-
"""离线算法自检：用模拟数据验证滚动Z-Score、ARWI合成与信号灯逻辑（不依赖网络）。"""
import math
import statistics
import sys

sys.path.insert(0, r"E:\WB工作空间\2026-08-06-13-59-24\arwi-dashboard")

from config import FACTORS, WEIGHTS, MIN_HISTORY_DAYS
from arwi_service import compute_arwi_history, signal_for


def make_rows(n, base):
    """构造 n 行模拟数据（逐步抬升制造风险累积）。"""
    rows = []
    for i in range(n):
        rows.append({
            "date": f"2026-0{i % 9 + 1}-{10 + i:02d}",
            "dxy": base["dxy"] + i * 0.5,
            "vix": base["vix"] + i * 0.8,
            "tnx": 4.2 + i * 0.01,
            "dfii10": base["dfii10"] + i * 0.06,
            "gold": base["gold"] - i * 3.0,   # 黄金下跌 → 反向因子恶化
            "oil": base["oil"] + i * 0.9,
        })
    return rows


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


ok = True
# ---- 场景1：连续抬升 15 天 → 应进入红灯 ----
rows = make_rows(20, {"dxy": 100, "vix": 15, "dfii10": 1.0, "gold": 2000, "oil": 70})
hist = compute_arwi_history(rows)
assert len(hist) == 20 - (MIN_HISTORY_DAYS - 1), "历史点数应为 10"
t = hist[-1]
ok &= check("上升场景：ARWI 为正", t["arwi"] > 1.0)
ok &= check("上升场景：红灯", t["signal"] == "red")
ok &= check("上升场景：≥3因子恶化", t["worsened"] >= 3)
print(f"  -> ARWI={t['arwi']:.2f}, 信号={t['signal_text']}, 恶化={t['worsened']}, 改善={t['improved']}")
print("  因子最终值:", {f['key']: round(t['finals'][f['key']], 2) for f in FACTORS})

# ---- 场景2：高位回落后 → 应进入绿灯 ----
rows2 = []
for i in range(15):
    rows2.append({
        "date": f"2026-03-{i + 1:02d}",
        "dxy": 105 - i * 0.6,
        "vix": 28 - i * 1.2,
        "tnx": 4.5,
        "dfii10": 2.5 - i * 0.08,
        "gold": 1850 + i * 8.0,   # 黄金上涨 → 反向因子改善
        "oil": 85 - i * 1.0,
    })
hist2 = compute_arwi_history(rows2)
t2 = hist2[-1]
ok &= check("回落场景：ARWI 为负", t2["arwi"] < -0.5)
ok &= check("回落场景：绿灯", t2["signal"] == "green")
ok &= check("回落场景：≥3因子改善", t2["improved"] >= 3)
print(f"  -> ARWI={t2['arwi']:.2f}, 信号={t2['signal_text']}, 恶化={t2['worsened']}, 改善={t2['improved']}")
print("  因子最终值:", {f['key']: round(t2['finals'][f['key']], 2) for f in FACTORS})

# ---- 场景3：平稳震荡 → 黄灯 ----
import random
random.seed(42)
rows3 = [{"date": f"2026-04-{i+1:02d}", "dxy": 103, "vix": 18, "tnx": 4.2,
          "dfii10": 2.0, "gold": 2000, "oil": 78}
         for i in range(20)]
for i in range(20):
    for k in ("dxy", "vix", "dfii10", "gold", "oil"):
        rows3[i][k] += random.uniform(-0.4, 0.4)
hist3 = compute_arwi_history(rows3)
t3 = hist3[-1]
ok &= check("震荡场景：|ARWI| 不大", abs(t3["arwi"]) < 1.0)
print(f"  -> ARWI={t3['arwi']:.2f}, 信号={t3['signal_text']}")

# ---- 权重与方向核对：黄金大涨应显著降低 ARWI ----
z_test = {"dfii10": 1.0, "dxy": 1.0, "vix": 1.0, "oil": 1.0, "gold": 2.0}
arwi_calc = sum(z_test[f["key"]] * f["direction"] * WEIGHTS[f["key"]] for f in FACTORS)
expected = 0.2 * (1 + 1 + 1 + 1 - 2)  # = 0.4
ok &= check("合成公式：ARWI=Σ(方向调整后Z×权重)", abs(arwi_calc - expected) < 1e-9)
print(f"  -> 手工核对 ARWI = {arwi_calc:.2f}（期望 {expected}）")

# ---- 信号判定边界 ----
ok &= check("红灯条件：ARWI>1 且≥3恶化", signal_for(1.2, 3, 1)[0] == "red")
ok &= check("绿灯条件：ARWI<-0.5 且≥3改善", signal_for(-0.8, 1, 3)[0] == "green")
ok &= check("黄灯兜底", signal_for(0.2, 2, 2)[0] == "yellow")
ok &= check("ARWI>1 但恶化不足3 → 黄灯(严格按规格)", signal_for(1.2, 2, 2)[0] == "yellow")

print("\n==========" + ("ALL PASS ✔" if ok else "SOME FAILED ✘") + "==========")
sys.exit(0 if ok else 1)
