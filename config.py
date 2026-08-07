# -*- coding: utf-8 -*-
"""ARWI 3.0 全局配置：因子定义、权重、阈值、调度参数。"""
from datetime import timezone, timedelta

# 北京时间时区（每日 9:00 定时任务使用）
CST = timezone(timedelta(hours=8), name="Asia/Shanghai")

# ---------------------------------------------------------------------------
# ARWI 五大风险因子
#   direction: +1 = 正向因子（原始 Z 越大风险越大，直接取 +Z）
#              -1 = 反向因子（原始 Z 越大越“避险/利率下行”，取 -Z）
# ---------------------------------------------------------------------------
FACTORS = [
    {"key": "dfii10", "name": "实际利率", "label": "10年期TIPS实际收益率(%)",
     "symbol": "DFII10", "source": "fred", "direction": 1},
    {"key": "dxy", "name": "美元指数", "label": "美元指数DXY",
     "symbol": "DX-Y.NYB", "source": "yf", "direction": 1},
    {"key": "vix", "name": "VIX", "label": "VIX恐慌指数",
     "symbol": "^VIX", "source": "yf", "direction": 1},
    {"key": "oil", "name": "原油", "label": "布伦特原油(美元/桶)",
     "symbol": "BZ=F", "source": "yf", "direction": 1},
    {"key": "gold", "name": "黄金", "label": "伦敦现货黄金(美元/盎司)",
     "symbol": "XAUUSD=X", "source": "yf", "direction": -1},
]

# 底部明细表的 6 个指标（含信息性指标：名义美债收益率，不参与 ARWI 合成）
TABLE_METRICS = [
    {"key": "dxy", "name": "美元指数", "symbol": "DX-Y.NYB",
     "is_yield": False, "dev_unit": "%"},
    {"key": "vix", "name": "VIX恐慌指数", "symbol": "^VIX",
     "is_yield": False, "dev_unit": "%"},
    {"key": "tnx", "name": "10Y名义美债收益率", "symbol": "^TNX",
     "is_yield": True, "dev_unit": "bp"},
    {"key": "dfii10", "name": "10Y实际利率(TIPS)", "symbol": "DFII10",
     "is_yield": True, "dev_unit": "bp"},
    {"key": "gold", "name": "伦敦现货黄金", "symbol": "XAUUSD=X",
     "is_yield": False, "dev_unit": "%"},
    {"key": "oil", "name": "布伦特原油", "symbol": "BZ=F",
     "is_yield": False, "dev_unit": "%"},
]

# 等权 20%
WEIGHTS = {f["key"]: 0.20 for f in FACTORS}

# 风险信号灯阈值
RED_HIGH = 1.0      # ARWI > +1.0 且 ≥3 个因子恶化 → 红灯
GREEN_LOW = -0.5    # ARWI < -0.5 且 ≥3 个因子改善 → 绿灯
FLAT_EPS = 0.05     # 变化方向“持平”的判定阈值
EXTREME_SIGMA = 2.0  # 极端值警戒：|Z| > 2σ

# 计算所需的最小历史天数（11 = 当日 + 前10日）
MIN_HISTORY_DAYS = 11
# 趋势图接口返回点数：需覆盖 60 日窗口内 MA60 每一天（60+59=119），留余量
HISTORY_POINTS = 150
MA_WINDOWS = (5, 10, 30, 60)  # 趋势图叠加的均线周期：MA5 / MA10 / MA30 / MA60

# 抓取参数
FETCH_DAYS_BACKFILL = 180  # 首次回填拉取天数（保证 MA60 在 60 日窗口内每天有值）
FETCH_DAYS_INCREMENTAL = 40  # 每日增量拉取天数
RETRY_TIMES = 3              # 单数据源失败重试次数
RETRY_BACKOFF = 2            # 重试基础间隔(秒)，按次递增
FETCH_PACE = 1.0             # 相邻抓取间隔(秒)，降低触发风控概率
FILL_MAX_DAYS = 5            # 前向填充上限(天)：数据源短期滞后(如FRED 1-2天)可补，
                             # 超过则保持空缺(标记待更新)，避免旧值无限期伪装成今日值
EMPTY_RETRY_GUARD = 60       # 空库失败后，N 秒内不重复全量重试
CONSECUTIVE_FAIL_LIMIT = 3   # 连续失败 N 天 → “数据源异常”

# SQLite
DB_PATH = "arwi.db"

# yfinance 可选增强（仅当网络可访问 Yahoo 时用于补充回填；失败静默忽略，
# 不影响国内主链。中国大陆网络 Yahoo 被封，此项自动失效）
YF_OPTIONAL = True
