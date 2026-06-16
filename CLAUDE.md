# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

A 股网格交易系统 v2.0.0 — 基于均值回归原理的量化交易自动化工具，实现智能选股、两阶段优化、回测和实时风控。

## Commit 规范

使用 Conventional Commits：`feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`。提交时保持单行简洁。

## 代码质量

无自动化 lint/format/type-check 流水线配置。依赖手动 review 和 pytest 验证。不要寻找不存在的配置文件（pyproject.toml, .pre-commit-config.yaml, CI workflows 均不存在）。

## 常用命令

```bash
# 环境
conda env create -f environment.yml
conda activate rain
# 或使用 pip
pip install -r requirements.txt

# 运行模式
python main.py --select              # 选股模式
python main.py --optimize            # 两阶段优化：贝叶斯 + WF微调
python main.py --backtest            # 历史回测
python main.py --select --force-select   # 强制重新选股（忽略缓存）
python main.py --download-db         # 智能下载/更新全市场历史数据到 SQLite

# CLI 参数（main.py）
python main.py --config my_config.yaml      # 自定义配置文件（默认：configuration/config.yaml）
python main.py --state my_state.json        # 自定义状态文件（默认：configuration/config_state.json）
python main.py --log-level DEBUG            # 日志级别：DEBUG/INFO/WARNING/ERROR
python main.py --version                    # 显示版本号
python main.py --download-start-date 2023-01-01   # 指定下载起始日期
python main.py --download-max-stocks 50           # 限制下载股票数量（调试用）

# 可视化仪表板
./dashboard_ctl.sh start      # 启动本地 Dashboard（http://127.0.0.1:7860）
./dashboard_ctl.sh share      # 启动并生成公网链接（Gradio tunnel）
./dashboard_ctl.sh stop       # 停止 Dashboard
./dashboard_ctl.sh status     # 查看运行状态
./dashboard_ctl.sh restart    # 重启

# 测试
python -m pytest tests/                              # 全部测试
python -m pytest tests/test_stock_selection.py       # 选股测试
python -m pytest tests/test_incremental_update.py    # 增量更新测试
python -m pytest tests/test_merge_logic.py           # 数据合并逻辑测试
python -m pytest tests/test_listing_duration.py      # 上市时间过滤测试
python -m pytest tests/test_listing_simple.py        # 上市时间简化测试
python -m pytest tests/test_merge_simple.py          # 合并逻辑简化测试
python test_rate_limit_fix.py                        # 限流修复验证（独立脚本）
```

## 目录结构

实际文件布局（与 README 中旧版扁平结构不同）：

```
auto_grid_trading_system/
├── main.py                       # CLI 入口（parse_arguments → execute_strategy）
├── dashboard.py                  # Gradio + Plotly 可视化仪表板（读取本地 parquet）
├── dashboard_ctl.sh              # Dashboard 进程管理脚本（start/stop/status/share）
├── test_rate_limit_fix.py        # 限流修复验证独立脚本
├── trading_core/                 # 核心交易逻辑
│   ├── strategy.py               # execute_strategy / run_selection / run_multi_factor_selection
│   ├── screener.py               # 多因子横截面打分选股器
│   ├── grid_engine.py            # 动态网格引擎（波动率自适应k系数）
│   └── indicators.py             # 技术指标计算（Numba JIT加速）
├── data_layer/                   # 数据层
│   ├── fetcher.py                # 统一数据入口（5数据源轮询 + 增量更新引擎）
│   ├── market_db.py              # SQLite 本地数据库（替代分散 parquet）
│   ├── tencent_fetcher.py        # 腾讯财经直接API
│   ├── eastmoney_fetcher.py      # 东方财富直接API
│   ├── sina_fetcher.py           # 新浪/网易直接API
│   └── session_manager.py        # HTTP Session管理器
├── risk_management/
│   ├── circuit_breaker.py        # 熔断风控（个股15%/全局10%）
│   └── market_regime.py          # 市场状态门控（ADX + 波动率分位数）
├── configuration/
│   ├── config.yaml               # 用户配置（~20行：资金、风控、可选覆盖）
│   └── config_state.json         # 动态状态（系统自动维护）
├── utils/
│   ├── utils.py                  # 工具函数（配置加载、日志、费用计算）
│   └── defaults.py               # 系统默认值（16个配置节）
├── tests/                        # 测试文件
├── data/                         # SQLite 数据库
├── output/                       # 输出目录
└── docs/                         # 文档目录（user_guides/）
```

## 核心模块

| 文件 | 职责 | 关键函数 |
|------|------|----------|
| `main.py` | CLI 入口、参数解析 | `parse_arguments()`, `main()` |
| `dashboard.py` | 可视化仪表板（K线、指标、网格参数） | Gradio UI + Plotly 图表 |
| `trading_core/strategy.py` | 选股/回测/优化/信号/WF | `execute_strategy()`, `run_selection()`, `run_multi_factor_selection()` |
| `trading_core/screener.py` | 多因子选股打分 | `MultiFactorScreener.screen_stocks()` |
| `trading_core/grid_engine.py` | 网格参数生成、信号计算 | `DynamicGridEngine.generate_signals()` |
| `trading_core/indicators.py` | 技术指标（Hurst、OU、ADX、ATR） | Numba JIT 加速 |
| `data_layer/fetcher.py` | 数据获取、限流、增量更新 | `get_stock_data()`, `fetch_stock_history()`, `incremental_update()` |
| `data_layer/market_db.py` | SQLite 本地数据库 | `init_db()`, `save_stock_data()`, `get_stock_data()`, `save_optimization_results()`, `save_update_checkpoint()` |
| `risk_management/market_regime.py` | 市场状态判断 | `RegimeFilter.check_current()` |
| `trading_core/defaults.py` | 系统默认值 | `get_defaults()` |

## 数据架构

### 5 数据源轮询系统

`fetch_stock_history()` 内部使用 `DataSourceManager` 管理 5 个数据源：

1. **Baostock** — 历史数据较全，优先使用（`prefer_baostock: true`）
2. **AkShare** — 功能丰富，但易触发反爬虫
3. **腾讯财经** — 直接 HTTP API，稳定性好
4. **东方财富** — `eastmoney_fetcher.py` 直接 API
5. **新浪/网易** — `sina_fetcher.py` 通过 163 CSV 接口

**关键设计**：每个数据源有独立的 `SourceRateLimiter`，一个源被限流时不影响其他源。`DataSourceManager` 按健康分数动态排序，单 attempt 内轮询所有源，失败立即切换。

### 增量更新引擎

`get_stock_data(code, enable_incremental=True)` 对已有缓存股票执行增量更新：
- 读取本地 parquet 缓存
- 计算缺失日期范围
- 调用 `fetch_incremental_data()` 获取增量
- 追加合并后保存

### SQLite 本地数据库

`data_layer/market_db.py` 提供统一 SQLite 存储：
- 表：`daily_kline`, `stock_metadata`, `update_log`, `stock_screening`, `backtest_results`, `optimization_results`, `update_checkpoint`
- 关键函数：`init_db()`, `save_stock_data()`, `get_stock_data()`, `save_optimization_results()`, `save_update_checkpoint()`
- 数据库路径：`data/market_data.db`
- 优化结果存储：替代 `output/report.json`，支持历史追溯
- 增量更新检查点：替代 `configuration/update_checkpoint.json`

### 智能选股缓存

`run_multi_factor_selection()` 开头有缓存逻辑：
- 检查 `config.selection_status.last_selection_date`
- 距上次选股 **< 90 天** 且未加 `--force-select`：跳过全市场选股，仅对 `config.stocks` 做增量数据补充
- 距上次选股 **>= 90 天** 或 `--force-select`：执行全市场预过滤 + 多因子打分

`selection_status` 结构（写入 config_state.json）：
```json
{
  "selection_status": {
    "completed": true,
    "last_selection_date": "2026-04-29",
    "last_data_update_date": "2026-04-29",
    "selection_count": 7,
    "version": "v2.0.0"
  }
}
```

## 运行模式

1. **select** — 多因子横截面打分筛选股票（四因子正交化 + 双轨权重）
2. **optimize** — 两阶段优化：
   - Phase 1: 贝叶斯优化（T-1Y ~ T-3M），Optuna TPE，最大化复合分数
   - Phase 2: WF微调（T-3M ~ T），以 Phase1 结果为中心 ±10% 搜索
3. **backtest** — 历史回测验证

**严格禁止未来数据泄露** — 所有数据切片必须仅使用该时间点之前的数据。

### 两阶段优化时间窗口

```
T = 当前日期
├── Phase 1 (贝叶斯优化)：T-1年 ~ T-3个月
└── Phase 2 (WF微调)：T-3个月 ~ T
```

### 并行优化

- 多只股票同时执行 Phase1 + Phase2（ThreadPoolExecutor）
- 主线程完成数据准备，worker 线程执行 Optuna 优化
- 通过 pickle 序列化 DataFrame 切片避免线程间共享引用
- 可通过 `parallel_optimization.enabled: false` 禁用并行回退串行

## 关键实现

### 多因子选股模型（四因子正交化）

| 因子 | 含义 | 股票权重 | ETF权重 |
|------|------|---------|---------|
| F1: Reversion_Speed | OU半衰期 | 25% | 35% |
| F2: Trend_Strength | ADX | 35% | 15% |
| F3: Vol_Quality | 波动率 | 20% | 30% |
| F4: Path_Memory | Variance Ratio | 20% | 20% |

初筛条件：成交额 >= 1亿元，股价 5-500元，上市时间 >= 1.5年

### 动态网格引擎

- 网格间距 = base_spacing × clip(vol_ratio, 0.8, 1.2)，统一使用百分比量纲
- 低波动(k=2.5)、中波动(k=2.0)、高波动(k=1.5)
- T+1 强制平仓：仅对可用持仓执行平仓，避免冻结份额导致委托拒单
- 支持 20 日滚动中位数波动率平滑，避免信号闪烁

### 风控机制

- T+1追踪：今日买入不可卖
- 个股未实现亏损 ≥ 15% → 暂停买入
- 全局最大回撤 ≥ 10% → 停止所有买入
- 市场状态门控：RegimeFilter（三级响应 + 硬底线），基于沪深300 ADX + 波动率分位数

### A股特殊规则

- T+1交易制度
- 涨跌停检查（±9.8%阈值）
- 手续费：万1.5（最低5元），印花税：万5（仅卖出）

## 配置文件

- `configuration/config.yaml` — 用户配置（资金、风险偏好、可选覆盖项）
- `configuration/config_state.json` — 动态状态（选股日期、市场状态、运行记录）
- `trading_core/defaults.py` — 系统默认值（网格参数、波动率k系数、限流策略等）

**配置加载机制**：`load_config()` 从 `defaults.py` 加载完整默认配置，再与 `config.yaml` 递归合并，用户值覆盖默认值。

关键配置节：
- `capital` — 资金配置（total, cash_reserve_ratio）
- `risk` — 风控阈值（max_drawdown_threshold, single_stock_loss_threshold, max_positions）
- `selection_status` — 选股日期追踪（写入 config_state.json，用于缓存判断）
- `advanced_screening` — 高级选股配置（双轨权重、质量阈值、行业集中度限制）
- `regime_filter` — 市场状态门控参数（ADX阈值、波动率分位、平滑天数、硬底线条件）
- `dynamic_grid` — 动态网格参数（波动率区间k系数、T+1适配、强制平仓）

## 输出文件

- `output/signals.csv` — 交易信号
- `output/report.json` — 优化报告（JSON 格式，Dashboard 渲染缓存）
- `output/stock_selection.csv` — 选股结果
- `data/market_data.db` — SQLite 数据库（含 optimization_results、update_checkpoint 等表）
