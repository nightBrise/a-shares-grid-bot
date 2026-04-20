# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

A 股网格交易系统 - 基于均值回归原理的量化交易自动化工具，实现智能选股、两阶段优化、信号生成和实时风控。

## 常用命令

```bash
# 环境搭建
conda env create -f environment.yml
conda activate rain

# 运行模式
python main.py --mode select        # 选股模式（约10分钟）
python main.py --mode optimize      # 两阶段优化：贝叶斯 + WF微调（约10分钟）
python main.py --mode signal        # 信号生成（约1分钟）

# 强制重新选股（忽略缓存）
python main.py --force-select

# 运行测试
python -m pytest tests/                    # 全部测试
python -m pytest tests/test_stock_selection.py  # 单个测试
```

## 目录结构

```
auto_grid_trading_system/
├── trading_core/               # 核心交易逻辑
│   ├── main.py                 # CLI 入口
│   ├── strategy.py             # 策略（选股/回测/优化/信号/WF）
│   ├── screener.py             # 多因子横截面打分选股器
│   ├── grid_engine.py          # 动态网格引擎
│   └── indicators.py           # 技术指标计算（Numba JIT加速）
├── data_layer/                 # 数据层
│   ├── fetcher.py              # 数据获取（AkShare/Baostock）、清洗、增量更新
│   └── session_manager.py      # HTTP Session管理器
├── risk_management/            # 风险管理
│   ├── enhanced_risk.py         # 增强风控：T+1追踪、分层滑点、费用计算
│   ├── circuit_breaker.py      # 熔断风控（个股15%/全局10%）
│   └── market_regime.py        # 市场状态门控
├── configuration/              # 配置
│   ├── config.yaml             # 静态配置
│   ├── config_base.yaml        # 基础配置
│   └── config_state.json       # 动态状态
├── cache/                      # 数据缓存
├── output/                     # 输出
├── tests/                      # 测试
└── utils/
    └── utils.py                # 工具函数
```

## 架构设计

### 核心模块

| 文件 | 职责 |
|------|------|
| `trading_core/strategy.py` | 选股、回测、两阶段优化、信号生成 |
| `trading_core/screener.py` | 多因子横截面打分选股器 |
| `trading_core/grid_engine.py` | 动态网格引擎（波动率自适应k系数） |
| `trading_core/indicators.py` | 技术指标：Hurst、OU半衰期、ADX、ATR |
| `data_layer/fetcher.py` | 数据获取、清洗、增量更新 |
| `risk_management/enhanced_risk.py` | T+1追踪、分层滑点、费用计算 |
| `risk_management/circuit_breaker.py` | 熔断风控 |
| `risk_management/market_regime.py` | 市场状态门控 |

### 数据流程

```
AkShare/Baostock → data_layer/fetcher.py → indicators.py/screener.py → grid_engine.py → output/signals.csv
                                                        ↓
                                                risk_management/（增强风控）
```

### 运行模式

1. **select** - 多因子横截面打分筛选股票（四因子正交化 + 双轨权重）
2. **optimize** - 两阶段优化：
   - Phase 1: 贝叶斯优化（T-1Y ~ T-3M），最大化复合分数
   - Phase 2: WF微调（T-3M ~ T），以Phase1结果为 priors
3. **signal** - 基于波动率动态调整网格间距，生成次日买卖计划

### 两阶段优化时间窗口

```
T = 当前日期
├── Phase 1 (贝叶斯优化)：T-1年 ~ T-3个月
└── Phase 2 (WF微调)：T-3个月 ~ T
```
**严格禁止未来数据泄露** - 所有数据切片必须仅使用该时间点之前的数据。

## 关键实现

### 多因子选股模型（四因子正交化）

| 因子 | 含义 | 股票权重 | ETF权重 |
|------|------|---------|---------|
| F1: Reversion_Speed | OU半衰期 | 25% | 35% |
| F2: Trend_Strength | ADX | 35% | 15% |
| F3: Vol_Quality | 波动率 | 20% | 30% |
| F4: Path_Memory | Variance Ratio | 20% | 20% |

### 两阶段优化

**Phase 1: 贝叶斯优化**
- 数据：T-1Y ~ T-3M
- 算法：Optuna TPE
- 目标：最大化复合分数（Calmar - 惩罚项）

**Phase 2: WF微调**
- 数据：T-3M ~ T
- 搜索空间：以Phase1参数为中心 ±10%
- 目标：在未见数据上验证并微调

### 动态网格引擎

- 网格间距 = base_spacing × clip(vol_ratio, 0.8, 1.2)，统一使用百分比量纲
- 低波动(k=2.5)、中波动(k=2.0)、高波动(k=1.5)
- T+1 强制平仓：仅对可用持仓执行平仓，避免冻结份额导致委托拒单
- 支持 20 日滚动中位数波动率平滑，避免信号闪烁

### 风控机制

- T+1追踪：今日买入不可卖
- 个股未实现亏损≥15% → 暂停买入
- 全局最大回撤≥10% → 停止所有买入

### A股特殊规则

- T+1交易制度
- 涨跌停检查（±9.8%阈值）
- 手续费：万1.5（最低5元），印花税：万5（仅卖出）

## 数据源与反爬虫

- 优先Baostock，AkShare备用
- 自适应限流（失败指数退避）
- HTTP Session复用 + UA轮换

## 配置文件

- `configuration/config.yaml` - 静态配置
- `configuration/config_state.json` - 动态状态（持仓等）

## 输出文件

- `output/signals.csv` - 交易信号
- `output/report.json` - 优化报告
- `output/stock_selection.csv` - 选股结果
