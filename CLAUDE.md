# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在本代码仓库中工作时提供指导。

## 项目概述

A 股网格交易系统 - 基于均值回归原理的量化交易自动化工具，实现智能选股、参数优化、信号生成和实时风控。

## 常用命令

```bash
# 环境搭建（需要先安装 conda）
conda env create -f environment.yml
conda activate rain

# 运行模式
python main.py --mode select        # 选股模式（约10分钟，多因子横截面打分）
python main.py --mode optimize      # 参数优化（约5分钟）
python main.py --mode signal        # 信号生成（约1分钟）
python main.py --mode wf           # Walk-Forward分析（约30分钟）

# 运行单个测试
python -m pytest tests/test_stock_selection.py

# 运行全部测试
python -m pytest tests/

# 代码质量检查（需先安装 ruff, mypy, bandit）
/code-scan

# 强制重新选股（忽略缓存的股票列表）
python main.py --force-select
```

## 架构设计

### 核心模块

| 文件 | 行数 | 职责 |
|------|------|------|
| `main.py` | 266 | 入口点，CLI解析，模式调度 |
| `strategy.py` | 2044 | 选股、回测引擎、优化、信号生成、Walk-Forward分析 |
| `data.py` | 1980 | 数据获取（AkShare/Baostock双数据源）、数据清洗、增量更新、自适应限流 |
| `data_http.py` | 336 | HTTP Session 管理器（UA轮换、连接复用） |
| `indicators.py` | 636 | 向量化技术指标计算（Numba JIT 加速） |
| `screener.py` | 949 | 高级多因子横截面打分选股器（含正交化） |
| `grid_engine.py` | 553 | 动态网格引擎（波动率区间自适应） |
| `risk.py` | 644 | 增强风控模块（T+1追踪、分层滑点、费用计算） |
| `utils.py` | 362 | 配置加载、交易费用计算、验证工具 |
| `risk_control.py` | 564 | 熔断风控（个股15%/全局10%） |

### 新增模块说明

**indicators.py** - 向量化技术指标计算（Numba JIT 加速）:
- `calculate_hurst_60d()` - Hurst 指数（R/S 分析）
- `calculate_ou_half_life()` - OU 过程半衰期
- `calculate_adx()` - ADX 趋向指标
- `calculate_volatility_60d()` - 年化波动率
- `calculate_atr()` - ATR 平均真实波幅
- `calculate_variance_ratio()` / `calculate_path_memory()` - 方差比检验（Path_Memory 因子）

**screener.py** - 高级多因子横截面打分选股器（v2）:
- 四因子正交化模型：F1(Reversion_Speed)、F2(Trend_Strength)、F3(Vol_Quality)、F4(Path_Memory)
- 双轨权重（ETF vs 股票）
- 动态阈值（adaptive_quantile 模式）
- 现金缓冲机制

**grid_engine.py** - 动态网格引擎:
- 波动率区间自适应 k 系数（低=2.5、中=2.0、高=1.5）
- T+1 最小间距约束、强制平仓机制

**risk.py** - 增强风控:
- T+1 持仓追踪、可用数量计算
- 分层滑点模型（0.1% 基础 + 价格分层）
- 阶梯费率计算（佣金、印花税、过户费）

**data_http.py** - HTTP Session 管理器:
- Session 复用（TCP 连接池）
- UA 轮换（12 个真实浏览器 UA）
- 定期更换 UA（默认 30 秒间隔）

### 运行模式

1. **select** - 高级多因子横截面打分筛选股票（四因子正交化 + 双轨权重）
2. **optimize** - 使用 Optuna 贝叶斯优化最大化 Calmar Ratio
3. **signal** - 基于 ATR 和波动率动态调整网格间距，生成次日买卖计划
4. **wf** - Walk-Forward 滚动窗口分析

### Walk-Forward 时间窗口（关键）

```
T = 当前日期
├── 选股池构建期：T-1.5年 ~ T-3个月
├── 样本内优化期：T-1年 ~ T-3个月
└── 样本外验证期：T-3个月 ~ T
```
**严格禁止未来数据泄露** - 所有数据切片必须仅使用该时间点之前的数据。

### 数据流程

```
AkShare/Baostock → data.py → indicators.py/screener.py → grid_engine.py → output/signals.csv
                                                    ↓
                                            risk.py（增强风控）
```

### 关键配置文件

- `config_base.yaml` - 静态基础配置（含新增的 screening、dynamic_grid、risk_control、slippage、http 配置）
- `config_state.json` - 动态状态（持仓、上次运行日期）
- `output/` - 输出文件目录

### 输出文件

- `output/signals.csv` - 交易信号
- `output/report.json` - 优化报告
- `output/stock_selection.csv` - 选股结果
- `output/wf_*.json` - Walk-Forward 报告

## 核心实现细节

### 网格策略参数
- 网格间距：1.0% ~ 5.0%（ATR动态调整）
- 每格金额：5000/10000/20000/50000 元
- 初始仓位：30% ~ 70%
- 最大网格层数：5 ~ 15 层

### 选股标准（高级多因子打分 v2）
**四因子正交化模型**:
- F1: Reversion_Speed (OU半衰期) - 均值回归速度，越短越好
- F2: Trend_Strength (ADX) - 趋势持续性，越低越好
- F3: Vol_Quality (波动率) - 倒U型函数，0.25 最优
- F4: Path_Memory (Variance Ratio) - 分形结构，正交化处理

**双轨权重**:
- ETF: F1=0.35, F2=0.15, F3=0.30, F4=0.20
- 股票: F1=0.25, F2=0.35, F3=0.20, F4=0.20

**质量阈值**: Score ≥ 0.65（动态阈值模式：max(0.65, quantile(0.75))）

### 风控熔断机制
- T+1 追踪：今日买入不可卖，可用数量 = 总持仓 - 今日买入
- 个股未实现亏损 ≥ 15% → 暂停该股买入，仅允许卖出
- 账户最大回撤 ≥ 10% → 全局停止所有买入

### A 股特殊规则
- T+1 交易制度（当日买入不能当日卖出）
- 涨跌停检查（±9.8% 阈值）
- 手续费：万1.5（最低5元），印花税：万5（仅卖出），过户费：万0.2

### 数据源与反爬虫机制

**双数据源策略**
- 优先使用 Baostock（更稳定，对请求频率限制宽松）
- AkShare 作为备用（被限制时自动切换）

**自适应限流器（AdaptiveRateLimiter）**
- 失败时指数退避：delay = base * multiplier^failures
- 成功后逐步恢复：delay *= recovery_factor
- 最大延迟限制：300 秒

**HTTP Session 管理器（data_http.py）**
- Session 复用（TCP 连接池，10/20 连接）
- UA 轮换（每 30 秒更换）
- 完整请求头（Accept、Accept-Language、DNT 等）

**批次处理器（BatchProcessor）**
- 批次大小：5 只股票
- 每 3 批长休息 30 秒
- 失败率高时自动延长休息时间

**关键配置参数**
```yaml
network:
  prefer_baostock: true           # 优先使用 Baostock
  aggressive_switch: true         # 快速失败切换
  batch_size: 5                   # 批次大小
  long_rest_after_batches: 3      # 每3批休息
  long_rest_duration: 30.0        # 长休息30秒
  adaptive_cooldown_base: 30.0    # 基础冷却
  adaptive_cooldown_multiplier: 2.0 # 指数退避倍数

http:
  session_enabled: true           # 启用 Session 复用
  ua_change_interval: 30         # UA 更换间隔（秒）
  pool_connections: 10           # 连接池连接数
  pool_maxsize: 20              # 连接池最大连接数
```

### 依赖要求
- Python 3.11+
- numba（技术指标 JIT 加速）
- scipy（用于 Hurst 指数计算）
- akshare, baostock（数据源）
- optuna（参数优化）
- pandas, numpy（数据处理）
