# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

A 股网格交易系统 - 基于均值回归原理的量化交易自动化工具，实现智能选股、参数优化、信号生成和实时风控。

## 常用命令

```bash
# 环境搭建
conda env create -f environment.yml
conda activate rain

# 运行模式
python main.py --mode select        # 选股模式（约10分钟）
python main.py --mode optimize      # 参数优化（约5分钟）
python main.py --mode signal        # 信号生成（约1分钟）
python main.py --mode wf           # Walk-Forward分析（约30分钟）

# 强制重新选股（忽略缓存）
python main.py --force-select

# 运行测试
python -m pytest tests/                    # 全部测试
python -m pytest tests/test_stock_selection.py  # 单个测试
```

## 架构设计

### 核心模块

| 文件 | 职责 |
|------|------|
| `main.py` | CLI解析、模式调度 |
| `strategy.py` | 选股、回测、优化、信号生成、Walk-Forward |
| `data.py` | 数据获取（AkShare/Baostock）、清洗、增量更新 |
| `indicators.py` | 技术指标计算（Numba JIT加速）：Hurst、OU半衰期、ADX、ATR |
| `screener.py` | 多因子横截面打分选股器 |
| `grid_engine.py` | 动态网格引擎（波动率自适应k系数） |
| `risk.py` | 增强风控：T+1追踪、分层滑点、费用计算 |
| `risk_control.py` | 熔断风控（个股15%/全局10%） |
| `data_http.py` | HTTP Session管理器（UA轮换、连接复用） |

### 数据流程

```
AkShare/Baostock → data.py → indicators.py/screener.py → grid_engine.py → output/signals.csv
                                                    ↓
                                            risk.py（增强风控）
```

### 运行模式

1. **select** - 多因子横截面打分筛选股票（四因子正交化 + 双轨权重）
2. **optimize** - Optuna贝叶斯优化最大化Calmar Ratio
3. **signal** - 基于ATR动态调整网格间距，生成次日买卖计划
4. **wf** - Walk-Forward滚动窗口分析

### Walk-Forward时间窗口

```
T = 当前日期
├── 选股池构建期：T-1.5年 ~ T-3个月
├── 样本内优化期：T-1年 ~ T-3个月
└── 样本外验证期：T-3个月 ~ T
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

### 动态网格引擎

- 网格间距 = k × ATR(20)
- 低波动(k=2.5)、中波动(k=2.0)、高波动(k=1.5)

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

- `config.yaml` - 静态配置
- `config_state.json` - 动态状态（持仓等）

## 输出文件

- `output/signals.csv` - 交易信号
- `output/report.json` - 优化报告
- `output/stock_selection.csv` - 选股结果
- `output/wf_*.json` - Walk-Forward报告
