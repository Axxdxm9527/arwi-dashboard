# 🛡️ ARWI 3.0 资产风险预警看板

每日自动抓取六大宏观指标，计算综合风险指数 ARWI（Asset Risk Warning Index），
与前 10 个交易日滚动均值对比，生成可视化预警看板。

## ✨ 功能特性

| 特性 | 说明 |
|------|------|
| 📊 KPI 状态栏 | 今日 ARWI 数值、红/黄/绿信号灯、较前10日均值变化方向 |
| 📈 双图联动 | ARWI 趋势（60/30/10/5日切换 + MA5/10/30/60均线）+ 5因子今日 Z-Score 条形图 |
| 📊 MOVE 看板 | ICE BofA MOVE 债券波动率（债市"VIX"）连续 60 日走势 + MA20 + 最新/均值/峰值 |
| 🌐 宏观风险仪表盘 | VIX / MOVE / IG-OAS / HY-OAS 四指标：20日滚动 Z-Score + 阈值五档分级 + 系统风险综合判定；OAS 来自 FRED 自动抓取，MOVE 来自 investing.com 自动抓取 |
| 📋 对比明细表 | 6 指标：今日值 / 前5/10/30/60日均值 / 偏离 / Z-Score / 状态 |
| 🤖 自动总结 | 每期自动生成中文文本解读（驱动因素 + 操作建议） |
| 🔔 交互提醒 | 信号变化灯号闪烁3次、\|Z\|>2σ 极端值黄条、数据源异常提示 |
| 🌗 主题切换 | 浅色 / 深色 / 跟随系统，本地记忆 |
| 🕘 定时任务 | 每日 09:00（北京时间）自动更新数据库，防 API 限流 |

## 📁 项目结构

```
arwi-dashboard/
├── app.py              # Flask 主程序（接口 + 定时任务）
├── arwi_service.py     # 数据抓取（国内源）、容灾、滚动Z-Score、ARWI 合成
├── db.py               # SQLite 存储层
├── config.py           # 因子定义、权重、阈值、调度参数
├── seed_demo.py        # （可选）离线演示数据工具
├── test_logic.py       # （可选）算法离线自检
├── static/
│   └── echarts.min.js  # 本地内置图表库（离线可用）
├── templates/
│   └── index.html      # 前端看板（单文件，CSS/JS 内嵌）
├── requirements.txt
└── arwi.db             # 运行时自动生成
```

## 🚀 快速启动

**方式一（推荐）：双击 `start.bat`**
- 自动检查并安装缺失依赖（首次运行需要联网），自动打开浏览器 http://127.0.0.1:5000
- 关闭窗口即停止服务

**方式二：命令行**

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动服务
python app.py

# 3. 浏览器访问
http://127.0.0.1:5000
```

> ⏱️ **首次运行**：会自动回填约 120 个交易日的历史数据（约 10~30 秒），
> 期间页面显示加载动画，完成后自动渲染。

> ⚠️ **若提示"无法连接后端服务"**：请确认浏览器地址栏是 **http://127.0.0.1:5000**（端口必须是 5000）。
> 预览面板/静态副本（其他端口）无法访问数据接口；若 5000 端口也连不上，多为依赖缺失
> （`pip install -r requirements.txt`）或端口被占用。

### 🎬 离线演示（可选）

中国大陆网络无法访问 Yahoo Finance（地域封锁）时，可先用演示数据预览看板：

```bash
python seed_demo.py   # 写入 60 天演示数据（脚本化逼真样例，非真实行情）
python app.py         # 访问 http://127.0.0.1:5000
```

清除演示数据后即回到真实数据模式：
`python -c "import sqlite3;c=sqlite3.connect('arwi.db');c.execute('DELETE FROM daily_metrics');c.execute('DELETE FROM meta');c.commit()"`

算法自检（无需网络）：
```bash
python test_logic.py  # 验证滚动Z-Score / ARWI合成 / 信号灯规则
```

## 🔌 数据源（国内优先，免费公开接口）

| 指标 | 主数据源 | 备用 |
|------|----------|------|
| 美元指数 DXY | **investing.com 美元指数**（Playwright 真实 Chrome 抓取，pairId=942611，≈160 日）| 新浪财经 `DINIW`（实时，历史自累积） |
| VIX 恐慌指数 | **investing.com VIX**（Playwright 真实 Chrome 抓取，pairId=44336，≈160 日）| 腾讯行情 `us.VIX`（日K）/ 新浪 `b_VIX`（实时） |
| 10Y 名义美债收益率 | FRED `DGS10` | Yahoo `^TNX` |
| 10Y TIPS 实际利率 | FRED `DFII10` | FRED `T10YIE`（名义−盈亏平衡） |
| 伦敦现货黄金 | 新浪外盘期货 `XAU`（全量日K）+ `hf_XAU` 实时 | 腾讯 `hf_XAU` 实时 |
| 布伦特原油 | 新浪外盘期货 `OIL`（全量日K）+ `hf_OIL` 实时 | 腾讯 `hf_OIL` 实时 |
| ICE BofA MOVE（独立看板 120 日 ≈ 半年） | **自动抓取**：Playwright + 真实 Chrome 绕过 Cloudflare → investing.com 历史 API（pairId=1164091，≈160 点 ≈ 7 个月，折线图取最近 120 个交易日） | 手动录入（`/api/macro_risk/manual` + 批量 `manual_batch`，兜底） |

> ⚠️ **MOVE 指数数据源说明**：
> - 腾讯 `usMOVE` / `us.MOVE` 是**美股 MOVE 股票**（Corvex Inc.，股价约 12 美元），**不是** ICE BofA MOVE 指数（债市"VIX"，数值约 50~160）；
> - **FRED 上没有 MOVE 系列**（`https://fred.stlouisfed.org/graph/fredgraph.csv?id=MOVE` 返回 404；网上流传的"FRED MOVE 系列"是误传），只有 48 个 ICE BofA 固收指数（OAS/收益率/总回报），不含 MOVE 波动率指数；
> - 唯一可用的源是 **investing.com** `https://cn.investing.com/indices/ice-bofaml-move`（NYSE 真实 ticker `^MOVE`，历史 API `pairId=1164091`）；
> - investing.com 有 Cloudflare 反爬，**数据中心 IP 直接请求 API 返回 403**；看板通过 **Playwright + 系统真实 Chrome** 启动浏览器并拦截 `financialdata/1164091/historical/chart/` 响应，绕过 Cloudflare 取到 ≈160 个日 K 线（含 open/high/low/close/volume，取第 5 列 close）；
> - 数据列含义：`[timestamp_ms, open, high, low, close, volume, change_pct]`，close 在第 5 列；
> - **手动录入接口保留**（`POST /api/macro_risk/manual` + 批量 `manual_batch`，支持 CSV / K 线数据粘贴）作为浏览器抓取失败时的兜底。

> - 新浪 / 腾讯接口国内直连；FRED 国内亦可访问（实际利率与美债收益率无国内免费替代源，故保留 FRED，且同源口径一致）。
> - 美元指数新浪仅提供实时值，首启后逐日自累积历史（约 10 个交易日后 Z-Score 完整）；若网络可达 Yahoo 会自动回填完整历史。
> - yfinance 仅为可选增强：失败静默跳过，不影响国内主链与状态标记。
> - 图表库 ECharts 已随项目内置（`static/echarts.min.js`），离线可用，无需外网 CDN。

### 容灾备份
- 每个因子多源链顺序尝试；单请求失败自动重试 **3 次**（指数退避），每次请求状态码打印在后台日志。
- `DFII10` 拉取失败 → 自动改用 **名义收益率 − 10年盈亏平衡通胀率（T10YIE）** 替代，前端标记「估算」。
- 仍失败 → 沿用前一日数据并标记「待更新」；**连续 3 天失败** → 前端显示「⚠️ 数据源异常」，看板使用最新有效值继续展示。
- 当日数据已存在则不重复拉取（SQLite 缓存，防限流）；【手动刷新】按钮可强制重新拉取。

## 🧮 核心算法

```
第一步  滚动标准化：对每个因子取最近11个交易日，Z = (当日值 − 前10日均值) / 前10日标准差
第二步  方向调整：实际利率 / 美元 / VIX / 原油 取 +Z，黄金取 −Z（避险反向）
第三步  合成：ARWI = 0.2×实际利率 + 0.2×美元 + 0.2×VIX + 0.2×原油 − 0.2×黄金
第四步  信号灯：
        🔴 红灯 ARWI > +1.0（≥3因子恶化）     → 高风险
        🟡 黄灯 -0.5 ≤ ARWI ≤ +1.0            → 观望
        🟢 绿灯 ARWI < -0.5（≥3因子改善）      → 积极
```

## 📡 API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/data` | GET | 看板数据（页面加载调用；当日已有数据则不重复拉取） |
| `/api/refresh` | POST | 手动强制刷新（绕过缓存，重新拉取全部数据源） |
| `/api/macro_risk` | GET | 全球宏观风险仪表盘数据（VIX/MOVE/IG-OAS/HY-OAS + 系统判定 + 底部注释） |
| `/api/macro_risk/manual` | POST | 手动录入 MOVE：`{"key":"move","value":123.4,"date":"YYYY-MM-DD"(可选,默认最新交易日)}` |
| `/api/macro_risk/cdx` | POST | （旧接口，等价于 `/api/macro_risk/manual`，兼容老客户端） |
| `/health` | GET | 健康检查 |

`/api/data` 返回 JSON：`arwi`、`signal`、`factors[].final`、`history[]`、`table[]`、`warnings[]`、`summary` 等。

## 🌐 全球宏观风险仪表盘

监控 VIX / MOVE / IG-OAS / HY-OAS 四大金融风险指标，**阈值与 Z-Score 规则严格按需求固定，不可修改**：

| 指标 | 平静 | 中性 | 警惕 | 危机 | 极端危机 |
|------|------|------|------|------|---------|
| VIX | <15 | 15-25 | 25-35 | 35-45 | >45 |
| MOVE | <80 | 80-120 | 120-160 | 160-220 | >220 |
| IG-OAS (bp) | <60 | 60-100 | 100-150 | 150-250 | >250 |
| HY-OAS (bp) | <350 | 350-500 | 500-700 | 700-1000 | >1000 |

- **20 个交易日滚动 Z-Score**：`Z = (当前值 − 20日均值) / 20日标准差`（全部指标统一）
- **Z 警报**：`Z>3` 🔴 极端事件警报；`Z>2` 🟠 显著异常警报；`Z>1` 🟡 偏离常态；`Z≤1` 🟢 正常波动范围
- **系统风险综合判定**（统计 Z>2 的指标数量）：`≥3` → ⚠️系统性风险预警；`1-2` → ⚠️局部风险信号；`0` → ✅整体风险处于正常区间
- **数据源（全部公开接口，无第三方爬虫，禁止虚构）**：
  - VIX：腾讯 `us.VIX` 日K / 新浪 `b_VIX` 实时（自动）
  - **IG-OAS**：FRED `BAMLC0A0CM`（ICE-BofA 投资级 OAS，百分比 ×100 转 bp，自动）
  - **HY-OAS**：FRED `BAMLH0A0HYM2`（ICE-BofA 高收益 OAS，百分比 ×100 转 bp，自动）
  - MOVE：investing.com（Playwright 真实 Chrome 自动抓取，pairId=1164091，≈160 点 ≈ 7 个月）
  - OAS 为 CDX 的现货替代（沿用原 CDX bp 阈值判断风险等级）
- **MOVE 近半年看板**：折线图展示最近 120 个交易日（MOVE 主线 + MA20），数据由 Playwright + investing.com 每日自动抓取入库；卡片下方备注数据源并附直达链接
- **底部强制注释**：`⚠️说明：OAS：市场上真实交易的企业债券，收益率高出无风险美债的差价。本仪表盘使用现货债券OAS利差和美债已实现波动率做代理；在流动性危机阶段，代理指标会和原版衍生品指数出现基差偏离；数据仅供宏观研究，不构成投资建议。`

## ⚙️ 常见问题

- **数据源全部「待更新」**：检查本机网络能否访问新浪 `hq.sinajs.cn`、腾讯 `qt.gtimg.cn`、FRED；断网时会自动沿用前一日数据并在表内标记状态。
- **yfinance 拉取失败 / 限流**：yfinance 仅为可选回填增强（中国大陆网络 Yahoo 被封属预期，自动静默跳过）；不影响国内主链。代理环境下可自动回填美元指数历史。
- **美元指数历史不足**：新浪仅提供实时值，首启后约 10 个交易日自累积完成；期间该因子 Z-Score 按 0（中性）处理。
- **FRED 数据滞后一天**：实际利率/通胀率偶有 1 日滞后，程序自动向前填充对齐，不影响 Z-Score 计算。
- **周末无新数据**：定时任务仅周一至周五 09:00 运行，页面显示最新交易日数据。
- **图表不显示**：图表库已内置 `static/echarts.min.js`，直接访问 `http://127.0.0.1:5000` 即可；若用纯静态方式打开 `index.html` 文件则无法调用数据接口（页面会给出指引）。

## 🧰 技术栈

Python 3.9+ · Flask · requests · 新浪/腾讯财经接口 · FRED (pandas-datareader) · APScheduler · SQLite · ECharts（本地内置 + CDN 兜底）
