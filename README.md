# A 股网格交易系统 v2.0.0

基于均值回归原理的量化交易自动化工具，实现智能选股、两阶段优化、回测和实时风控。

## 快速开始

```bash
# 1. 克隆仓库
git clone <repo-url>
cd auto_grid_trading_system

# 2. 创建用户配置（从模板复制）
cp configuration/config_template.yaml configuration/config.yaml
# 编辑 config.yaml，填入你的资金和风险偏好

# 3. 安装环境
conda env create -f environment.yml && conda activate rain
# 或 pip install -r requirements.txt

# 4. 运行
python main.py --select              # 选股模式
python main.py --optimize            # 两阶段优化：贝叶斯 + WF微调
python main.py --backtest            # 历史回测
python main.py --download-db         # 智能下载/更新全市场历史数据到 SQLite

# 可视化仪表板
./dashboard_ctl.sh start      # 本地 http://127.0.0.1:7860
./dashboard_ctl.sh share      # 公网链接
```

## 项目结构

```
trading_core/       # 交易逻辑核心
├── strategy.py     # 选股/优化/回测/信号生成
├── screener.py     # 多因子横截面打分选股器
├── grid_engine.py  # 动态网格引擎（波动率自适应）
└── indicators.py   # 技术指标（Numba JIT：Hurst, OU, ADX, ATR）

data_layer/         # 数据层
├── fetcher.py      # 统一入口：5数据源轮询 + 增量更新
├── market_db.py    # SQLite 本地数据库
├── tencent_fetcher.py / eastmoney_fetcher.py / session_manager.py

risk_management/    # 风控
├── circuit_breaker.py # 熔断（个股15%/全局10%）
└── market_regime.py   # ADX + 波动率分位数市场状态门控

configuration/      # 配置（重构后：用户只需配置资金+风险偏好）
├── config.yaml     # 用户配置（~20行：资金、风控阈值、可选覆盖项）
└── config_state.json # 动态状态（系统自动维护）

utils/              # 工具
├── utils.py        # 配置加载（defaults.py + config.yaml 合并）、日志、费用计算
└── defaults.py     # 系统默认值（16个配置节，用户无需关心）
```

## 核心特性

| 特性 | 说明 |
|------|------|
| 智能选股 | 四因子正交化横截面打分（OU半衰期、Hurst、ADX、波动率） |
| 两阶段优化 | Phase1 贝叶斯（T-1Y~T-3M）+ Phase2 WF微调（T-3M~T） |
| 动态网格 | 波动率自适应 k 系数（低波2.5/中波2.0/高波1.5） |
| 严格风控 | T+1追踪、单股熔断15%、全局熔断10%、市场状态门控 |
| 增量更新 | 仅拉取新增数据，效率提升10倍 |
| 可视化 | Gradio + Plotly 仪表板（K线、指标、净值、优化结果） |

## v2.0.0 配置重构（重要变更）

```yaml
# configuration/config.yaml — 用户只需配置这些
version: "2.0.0"

capital:
  total: 1000000              # 总资金
  cash_reserve_ratio: 0.4     # 现金保留比例

risk:
  max_drawdown_threshold: 0.1        # 全局最大回撤
  single_stock_loss_threshold: 0.15  # 单股最大亏损
  max_positions: 5                   # 最大持仓数

# 其余参数（网格间距、波动率k系数、限流策略等）由系统自动优化
```

**变更要点**：
- 配置从 266 行精简到 ~20 行
- 系统默认值收敛到 `trading_core/defaults.py`
- `load_config()` 自动合并：defaults + 用户覆盖
- 删除冗余文件：`config_base.yaml`、`candidate_stocks.json`、`update_checkpoint.json`
- 优化结果存储到 SQLite `optimization_results` 表，替代 `output/report.json`
- 增量更新检查点存储到 SQLite `update_checkpoint` 表

## 文档

- `CLAUDE.md` — 项目完整指南（目录结构、核心模块、数据架构、运行模式）
- `docs/user_guides/config_files_guide.md` — 配置文件详解
- `docs/user_guides/walk_forward.md` — Walk-Forward 时间窗口切割策略
- `docs/user_guides/fee_and_slippage.md` — 回测费用与滑点分析

## 免责声明

本系统仅供学习研究使用，不构成任何投资建议。量化交易存在风险，使用者应自行承担风险。
