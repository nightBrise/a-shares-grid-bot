# A 股网格交易系统 v1.0.0

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-1.0.0-green.svg)]()

> **A 股量化交易自动化工具**：基于均值回归理论，实现智能选股、参数优化、信号生成与风控一体化的网格交易系统。

---

## 📋 目录

- [项目简介](#-项目简介)
- [核心功能](#-核心功能)
- [系统架构](#-系统架构)
- [安装指南](#-安装指南)
- [快速开始](#-快速开始)
- [配置说明](#-配置说明)
- [运行模式](#-运行模式)
- [输出文件](#-输出文件)
- [高级特性](#-高级特性)
- [常见问题](#-常见问题)
- [风险提示](#-风险提示)
- [贡献指南](#-贡献指南)
- [许可证](#-许可证)

---

## 🎯 项目简介

本系统是一个专业的 **A 股网格交易量化平台**，基于均值回归原理，通过自动化选股、智能参数优化和实时风控，实现"低买高卖"的量化策略。系统支持 Walk-Forward 时间窗口分析、增量数据更新、动态参数调整等高级功能，适合有一定 Python 基础的量化交易者使用。

**核心理念**：
- 🔬 **科学选股**：基于 Hurst 指数筛选均值回归特性股票
- 🎲 **智能优化**：Optuna 贝叶斯优化寻找最佳网格参数
- ⚡ **实时风控**：熔断机制保护本金安全
- 📊 **数据驱动**：增量更新引擎高效管理历史数据

---

## ✨ 核心功能

### 1. 智能选股（Stock Selection）
- ✅ 基于 **Hurst 指数** 识别均值回归特性股票
- ✅ 多维度过滤：流动性、价格区间、波动率
- ✅ 自动排除 ST 股、问题股
- ✅ 支持全市场扫描或自定义股票池

### 2. 参数优化（Parameter Optimization）
- ✅ **Optuna 贝叶斯优化** 自动搜索最佳参数
- ✅ 搜索空间：网格间距、每格金额、初始仓位、最大层数
- ✅ 目标函数：最大化 Calmar Ratio（年化收益/最大回撤）
- ✅ 支持 In-Sample/OOS 样本内外验证

### 3. 信号生成（Signal Generation）
- ✅ 生成次日交易计划（买入/卖出价格、数量）
- ✅ **动态参数调整**：根据实时波动率自适应调整网格
- ✅ **实盘熔断风控**：个股亏损≥15% 或账户回撤≥10% 触发
- ✅ A 股规则适配：T+1 检查、涨跌停限制

### 4. 数据管理（Data Management）
- ✅ **增量更新引擎**：仅拉取新增数据，效率提升 10 倍
- ✅ 双数据源支持：AkShare + Baostock 自动切换
- ✅ 数据完整性检查：自动检测并补全缺失交易日
- ✅ 后复权处理：避免分红除权导致的价格跳空

### 5. Walk-Forward 分析（滚动窗口验证）
- ✅ 严格时间窗口分割：T-1.5Y~T-3M 选股，T-1Y~T-3M 优化，T-3M~T 验证
- ✅ 滚动执行机制：支持按月/季/周滚动回测
- ✅ 无前视偏差：所有指标计算仅使用历史数据

---

## 🏗️ 系统架构

```
auto_grid_trading_system/
├── main.py                     # 主入口，命令行解析与流程调度
├── strategy.py                 # 核心策略：选股、优化、信号生成、回测
├── data.py                     # 数据模块：增量更新、清洗、指标计算
├── utils.py                    # 工具模块：费用计算、配置加载、日志
├── risk_control.py             # 风控模块：熔断机制、盈亏监控
│
├── config_base.yaml            # 基础配置文件
├── config_enhanced.yaml        # 增强配置文件（含风控参数）
├── config_state.json           # 动态状态文件（自动维护）
├── .env.example                # 环境变量模板
│
├── requirements.txt            # pip 依赖列表
├── environment.yml             # conda 环境配置
├── .gitignore                  # Git 忽略规则
│
├── data/                       # 数据目录（自动下载 CSV 缓存）
│   ├── 600519_SH.csv
│   ├── metadata.json          # 元数据：记录每只股票最后更新日期
│   └── ...
│
└── output/                     # 输出目录（信号、报告、日志）
    ├── signals.csv            # 交易信号
    ├── stock_selection.csv    # 选股结果
    ├── report.json            # 优化报告
    ├── wf_*.json              # Walk-Forward 分析报告
    └── log.txt                # 运行日志
```

---


---

## 📊 当前系统状态

### 配置文件管理（最新简化版）

**更新日期**: 2026-03-18  
**配置策略**: 单一配置文件管理

#### 保留的配置文件
✅ **config_base.yaml** - 用户工作配置文件
- 包含所有必要的配置参数
- 可直接编辑修改
- 纳入 Git 版本控制
- 系统默认读取此文件

#### 已移除的文件
❌ ~~config.yaml~~ - 已删除（冗余）  
❌ ~~config_enhanced.yaml~~ - 已删除（冗余）  
❌ ~~config_state.json~~ - 已删除（状态自动记录到日志）

#### 为什么要简化？
1. **减少混淆**：多个配置文件容易导致用户困惑
2. **便于维护**：单一文件更容易管理和追踪变更
3. **符合直觉**：直接编辑配置文件，无需复制
4. **自动化**：运行状态自动记录到日志，无需手动维护状态文件

#### 如何使用？
```bash
# 1. 直接编辑配置文件
vim config_base.yaml

# 2. 修改需要的参数（股票池、网格参数等）

# 3. 运行系统
python main.py --config config_base.yaml
```

#### 系统运行状态查看
- **实时日志**: `output/log.txt`
- **交易信号**: `output/signals.csv`
- **优化报告**: `output/report.json`
- **选股结果**: `output/stock_selection.csv`

---
## 📦 安装指南

### 方式一：Conda 安装（推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/yourusername/a-share-grid-trading-system.git
cd a-share-grid-trading-system/auto_grid_trading_system

# 2. 创建并激活环境
conda env create -f environment.yml
conda activate rain

# 3. 验证安装
python --version  # 应显示 Python 3.11.x
```

### 方式二：pip 安装

```bash
# 1. 确保 Python 3.11+
python3 --version

# 2. 安装依赖
pip install -r requirements.txt

# 3. 验证安装
python -c "import akshare; import optuna; print('依赖安装成功')"
```

### 依赖说明

| 依赖 | 版本要求 | 用途 |
|------|----------|------|
| pandas | ≥2.0.0 | 数据处理 |
| numpy | ≥1.24.0 | 数值计算 |
| akshare | ≥1.10.0 | A 股行情数据 |
| baostock | ≥0.8.8 | 备用数据源 |
| optuna | ≥3.0.0 | 参数优化 |
| scipy | ≥1.10.0 | 统计计算 |
| PyYAML | ≥6.0 | 配置文件解析 |

---

## 🚀 快速开始

### 步骤 1：配置文件

复制示例配置文件：

```bash
cp config_base.yaml config.yaml
```

编辑 `config.yaml`，设置要交易的股票代码：

```yaml
stocks:
  - "600519.SH"  # 贵州茅台
  - "000858.SZ"  # 五粮液
  - "601318.SH"  # 中国平安

mode: "select"  # 首次运行使用选股模式
```

### 步骤 2：运行选股

```bash
python main.py --mode select
```

输出示例：
```
✓ 选股完成：20 只股票入选
结果已保存到：output/stock_selection.csv
```

### 步骤 3：参数优化（可选）

```bash
python main.py --mode optimize
```

输出示例：
```
✓ 优化完成：最佳参数 grid_spacing=2.5%, calmar_ratio=1.23
报告已保存到：output/report.json
```

### 步骤 4：生成交易信号

```bash
python main.py --mode signal
```

输出示例：
```
✓ 生成 45 个交易信号
买入信号：25 个，卖出信号：20 个
信号已保存到：output/signals.csv
```

---

## ⚙️ 配置说明

### 📁 配置文件管理（已简化）

**系统当前仅保留 1 个配置文件**，简化配置管理流程：

| 文件 | 作用 | 是否必需 | Git 管理 |
|------|------|----------|----------|
| `config_base.yaml` | **用户工作配置文件**（包含所有参数定义和详细说明） | ✅ 必需 | ✅ 提交 |

**配置管理策略**：
- ✅ **单一配置文件**：直接使用 `config_base.yaml` 作为工作配置文件
- ✅ **直接编辑**：修改 `config_base.yaml` 中的参数即可运行系统
- ✅ **版本控制**：配置文件纳入 Git 管理，方便追踪参数变更历史
- ✅ **状态自动维护**：系统运行时状态自动记录到日志文件，无需单独的状态文件

### 快速开始

```bash
# 1. 编辑配置文件
vim config_base.yaml

# 2. 修改股票池、网格参数等（详见下方核心参数说明）

# 3. 运行系统
python main.py --config config_base.yaml
```

### config_base.yaml 核心参数详解

```yaml
# ==================== 基础信息 ====================
version: "1.0.0"          # 系统版本号
mode: "select"            # 运行模式：select/optimize/signal

# ==================== 股票池配置 ====================
stocks:
  - "600519.SH"           # 贵州茅台
  - "000858.SZ"           # 五粮液
  - "601318.SH"           # 中国平安
  - "000333.SZ"           # 美的集团
  - "600036.SH"           # 招商银行

# ==================== 网格交易参数 ====================
grid:
  base_spacing: 2.0       # 基础网格间距 (%)
  grid_amount: 10000      # 每格固定金额 (元)
  max_grids: 10           # 最大网格层数
  initial_position: 50    # 初始仓位比例 (%)
  atr_period: 20          # ATR 计算周期
  atr_coef: 1.5           # ATR 调整系数

# ==================== 风险管理参数 ====================
risk:
  min_turnover: 5000      # 最小日均成交额 (万元)
  min_price: 5.0          # 最低股价限制
  max_price: 500.0        # 最高股价限制
  limit_threshold: 9.8    # 涨跌停检测阈值 (%)
  volatility_threshold: 0.8  # 年化波动率阈值

# ==================== 回测与优化参数 ====================
backtest:
  n_trials: 50            # Optuna 优化试验次数
  commission_rate: 0.00015  # 佣金费率 (万 1.5)
  stamp_tax: 0.0005       # 印花税率 (万 5)
  days: 250               # 回测天数
  oos_ratio: 0.3          # 样本外验证比例 (30%)

# ==================== 路径配置 ====================
paths:
  data_dir: "./data"      # 数据存储目录
  output_dir: "./output"  # 输出文件目录
  signal_file: "signals.csv"    # 信号文件名
  log_file: "log.txt"           # 日志文件名
  report_file: "report.json"    # 报告文件名

# ==================== 选股策略配置 ====================
selection:
  hurst_threshold: 0.5    # Hurst 指数阈值 (<0.5 为均值回归)
  min_price: 5.0          # 最低股价
  max_price: 500.0        # 最高股价
  volatility_threshold: 0.8  # 波动率阈值
  max_stocks_to_process: 200  # 最大处理股票数量
  save_top_n: 20          # 保存前 N 只最佳股票

# ==================== 网络请求配置 ====================
network:
  prefer_baostock: false  # 优先使用 Baostock 数据源
  min_delay_per_stock: 2.0  # 每只股票最小延迟 (秒)
  max_delay_per_stock: 5.0  # 每只股票最大延迟 (秒)
  batch_size: 10          # 批次处理大小
  max_retries: 5          # 最大重试次数

# ==================== 日志配置 ====================
logging:
  level: INFO             # 日志级别：DEBUG/INFO/WARNING/ERROR
  backup_count: 30        # 日志轮转保留天数
  max_bytes: 0            # 单文件最大字节数 (0=不限制)
```

### 配置参数说明

#### 1. 运行模式 (mode)
- `select`: **选股模式** - 筛选适合网格交易的标的
- `optimize`: **优化模式** - 贝叶斯优化网格参数
- `signal`: **信号模式** - 生成次日交易计划

#### 2. 网格参数 (grid)
- `base_spacing`: 网格间距百分比，如 2.0 表示股价每变动 2% 触发一次交易
- `grid_amount`: 每格固定交易金额，单位元
- `max_grids`: 最大网格层数，控制总仓位
- `initial_position`: 初始建仓时的仓位比例
- `atr_period` 和 `atr_coef`: 用于动态调整网格间距的 ATR 指标参数

#### 3. 风险参数 (risk)
- `min_turnover`: 过滤流动性不足的股票（日均成交额，单位万元）
- `min_price` / `max_price`: 避免低价股和高价股
- `limit_threshold`: 涨跌停检测阈值，用于暂停交易判断
- `volatility_threshold`: 波动率过滤阈值

#### 4. 回测参数 (backtest)
- `n_trials`: Optuna 优化时的试验次数，越多越精确但耗时
- `commission_rate`: 交易佣金费率（万 1.5 = 0.00015）
- `stamp_tax`: 卖出印花税（万 5 = 0.0005）
- `days`: 回测使用的历史数据天数
- `oos_ratio`: 样本外验证比例，用于防止过拟合

#### 5. 选股参数 (selection)
- `hurst_threshold`: Hurst 指数阈值，<0.5 表示具有均值回归特性
- `max_stocks_to_process`: 单次选股最多处理的股票数量
- `save_top_n`: 保存排名靠前的股票数量到结果文件

### 配置修改建议

**新手建议**：
1. 首次使用保持默认参数，仅修改股票池
2. 运行选股模式 (`mode: select`) 筛选优质标的
3. 根据选股结果调整股票池配置
4. 逐步尝试参数优化和信号生成

**进阶使用**：
1. 根据市场环境调整网格间距和仓位参数
2. 通过回测验证不同参数的效果
3. 关注日志文件了解系统运行状态
4. 定期检查输出目录的信号和报告文件

### 注意事项

⚠️ **重要提示**：
- 修改配置后请保存文件并重新运行命令
- 系统运行状态和日志记录在 `output/log.txt`
- 所有输出文件（信号、报告、选股结果）保存在 `output/` 目录
- 首次运行会自动下载历史数据到 `data/` 目录
- 建议定期备份重要的配置和输出文件

---
  max_drawdown_threshold: 0.10     # 账户回撤≥10% 全局熔断
  vol_adjustment_enabled: true     # 启用波动率动态调整
  
# 持仓信息（用于风控计算）
positions:
  - code: "600519.SH"
    cost_price: 1750.0
    quantity: 100

cash: 500000  # 可用现金 (元)
```

**注意**: 
- 不要直接修改 `config_base.yaml`，而是复制为 `config.yaml` 后修改
- `config_state.json` 由系统自动维护，不要手动编辑
- 建议将 `config.yaml` 和 `config_state.json` 加入 `.gitignore`

---

## 🎮 运行模式

### 1. 选股模式（select）

```bash
python main.py --mode select
```

**功能**：
- 扫描全市场股票
- 计算 Hurst 指数、波动率等指标
- 输出符合条件的股票列表

**输出文件**：`output/stock_selection.csv`

| 字段 | 说明 |
|------|------|
| code | 股票代码 |
| price | 最新股价 |
| hurst | Hurst 指数（<0.5 为均值回归） |
| volatility | 波动率 |
| avg_turnover | 日均成交额（万元） |
| rank | 排名 |
| reason | 推荐理由 |

### 2. 参数优化模式（optimize）

```bash
python main.py --mode optimize
```

**功能**：
- 使用 Optuna 进行贝叶斯优化
- 搜索最佳网格参数组合
- 输出优化报告

**输出文件**：`output/report.json`

```json
{
  "best_params": {
    "grid_spacing": 2.5,
    "grid_amount": 10000,
    "initial_position": 50,
    "max_grids": 8
  },
  "metrics": {
    "calmar_ratio": 1.2345,
    "total_return": 0.1523,
    "max_drawdown": 0.0823
  }
}
```

### 3. 信号模式（signal）

```bash
python main.py --mode signal
```

**功能**：
- 生成次日交易计划
- 应用动态参数调整
- 执行风控检查

**输出文件**：`output/signals.csv`

| 字段 | 说明 |
|------|------|
| code | 股票代码 |
| direction | buy/sell |
| price | 目标价格 |
| quantity | 数量 |
| amount | 金额 |
| reason | 触发原因 |
| valid_date | 有效日期 |
| strategy_version | 策略版本 |
| param_source | optimized/adjusted |

### 4. Walk-Forward 模式（wf）

```bash
# 基本用法
python main.py --mode wf

# 指定当前日期
python main.py --mode wf --wf-date 2024-12-31

# 滚动执行（每月一次，共 12 次）
python main.py --mode wf --rolling 1m
```

**功能**：
- 执行完整的 Walk-Forward 分析流程
- 滚动窗口验证策略稳定性
- 输出多期对比报告

**输出文件**：
- `output/wf_stock_selection_*.csv` - 选股结果
- `output/wf_optimization_report_*.json` - 优化报告
- `output/wf_summary_report_*.json` - 汇总报告

---

## 📊 输出文件详解

### signals.csv - 交易信号

```csv
code,direction,price,quantity,amount,reason,valid_date,strategy_version,param_source
600519.SH,buy,1720.50,100,172050.00，网格第 1 层买入，2024-01-16,v1.0.0,adjusted
600519.SH,sell,1785.30,100,178530.00，网格第 1 层卖出，2024-01-16,v1.0.0,optimized
```

**关键字段**：
- `param_source`: 
  - `"optimized"` = 使用历史优化参数
  - `"adjusted"` = 经波动率动态调整

### report.json - 优化报告

```json
{
  "optimization_date": "2024-12-31",
  "best_params": {...},
  "in_sample": {
    "calmar_ratio": 1.2345,
    "sharpe_ratio": 1.5678
  },
  "out_of_sample": {
    "total_return": 0.0523,
    "max_drawdown": 0.0823
  }
}
```

---

## 🚀 高级特性

### 1. 增量数据更新

第二次运行时自动仅拉取新增数据：

```bash
# 第一次：全量更新（约 5 秒/股票）
python main.py --mode select

# 第二次：增量更新（约 0.5 秒/股票）
python main.py --mode select

# 日志显示：
# "Incremental update: 5 days data fetched for 600519.SH"
```

详见：[docs/INCREMENTAL_UPDATE_README.md](docs/INCREMENTAL_UPDATE_README.md)

### 2. 动态参数调整

根据实时波动率自动调整网格间距：

```
当前波动率 > 参考×1.5 → 扩大 20% (应对大幅震荡)
当前波动率 < 参考×0.5 → 缩小 20% (加密网格)
```

详见：[DYNAMIC_RISK_CONTROL_README.md](DYNAMIC_RISK_CONTROL_README.md)

### 3. 实盘熔断风控

```
个股熔断：单只股票亏损≥15% → 暂停该股买入
全局熔断：账户回撤≥10% → 停止所有买入
```

详见：[docs/DYNAMIC_RISK_CONTROL_README.md](docs/DYNAMIC_RISK_CONTROL_README.md)

### 3. 实盘熔断风控

严格时间窗口分割，避免前视偏差：

```
选股期：T-1.5Y ~ T-3M
优化期：T-1Y ~ T-3M
验证期：T-3M ~ T
```

详见：[WALK_FORWARD_README.md](WALK_FORWARD_README.md)

---

## ❓ 常见问题

### Q1: 为什么选股结果为空？

**A**: 可能原因：
1. 过滤条件过严（如 `min_turnover` 设置过高）
2. 数据不足（新股上市时间<1.5 年自动排除）
3. 网络问题导致数据获取失败

**解决方案**：
```yaml
selection:
  hurst_threshold: 0.55  # 放宽至 0.55
  min_turnover: 3000     # 降低成交额要求
```

### Q2: 如何修改网格密度？

**A**: 两种方式：
1. **手动调整**：编辑 `config.yaml` 的 `grid.base_spacing`
2. **自动优化**：运行 `python main.py --mode optimize`

### Q3: 回测收益率很高，实盘为何不赚钱？

**A**: 可能原因：
1. **未考虑滑点**：当前回测未包含滑点成本（建议手动扣除 0.1-0.2%）
2. **交易频率过高**：实盘冲击成本被低估
3. **参数过拟合**：使用 Walk-Forward 验证稳定性

详见：[docs/WALK_FORWARD_README.md](docs/WALK_FORWARD_README.md)

---

## 📚 扩展文档

以下详细文档位于 `docs/` 目录：

| 文档 | 说明 |
|------|------|
| [增量数据更新](docs/INCREMENTAL_UPDATE_README.md) | 增量 ETL 引擎详解 |
| [动态风控机制](docs/DYNAMIC_RISK_CONTROL_README.md) | 波动率调整与熔断机制 |
| [Walk-Forward 分析](docs/WALK_FORWARD_README.md) | 时间窗口切割策略 |
| [费用与滑点](docs/FEE_AND_SLIPPAGE_ANALYSIS.md) | 回测费用计算与滑点影响 |
| [新股防御机制](docs/LISTING_DURATION_DEFENSE.md) | 上市时间检查逻辑 |
| [前视偏差修复](docs/LOOKAHEAD_BIAS_FIX.md) | 避免使用未来数据 |
| [数据合并逻辑](docs/MERGE_LOGIC_EXPLANATION.md) | 增量更新去重机制 |

---

## ❓ 常见问题

### Q1: 为什么选股结果为空？

**A**: 可能原因：
1. 过滤条件过严（如 `min_turnover` 设置过高）
2. 数据不足（新股上市时间<1.5 年自动排除）
3. 网络问题导致数据获取失败

**解决方案**：
```yaml
selection:
  hurst_threshold: 0.55  # 放宽至 0.55
  min_turnover: 3000     # 降低成交额要求
```

### Q2: 如何修改网格密度？

**A**: 两种方式：
1. **手动调整**：编辑 `config.yaml` 的 `grid.base_spacing`
2. **自动优化**：运行 `python main.py --mode optimize`

### Q3: 回测收益率很高，实盘为何不赚钱？

**A**: 可能原因：
1. **未考虑滑点**：当前回测未包含滑点成本（建议手动扣除 0.1-0.2%）
2. **交易频率过高**：实盘冲击成本被低估
3. **参数过拟合**：使用 Walk-Forward 验证稳定性

详见：[docs/FEE_AND_SLIPPAGE_ANALYSIS.md](docs/FEE_AND_SLIPPAGE_ANALYSIS.md)

### Q4: 如何恢复被熔断的股票？

**A**: 
```bash
# 查看熔断状态
cat output/risk_state.json

# 手动重置（确认市场恢复后）
python -c "from risk_control import RiskControlManager; rc = RiskControlManager(config); rc.reset_global_breaker()"
```

---

## ⚠️ 风险提示

**重要声明**：

1. **投资风险**：量化交易存在亏损风险，过往业绩不代表未来表现
2. **代码风险**：本系统按"原样"提供，不保证无 Bug 或适用于所有场景
3. **数据风险**：依赖第三方数据源（AkShare/Baostock），可能存在延迟或错误
4. **合规风险**：请确保使用方式符合当地法律法规及券商规定

**建议使用流程**：
1. 先用**历史数据回测**验证策略有效性
2. 使用**模拟盘**运行至少 3 个月
3. 实盘投入**不超过可承受损失的资金**
4. 定期**监控系统日志**，及时调整参数

**作者不对任何直接或间接损失承担责任**。

---

## 🤝 贡献指南

欢迎提交 Issue 和 Pull Request！

### 开发环境搭建

```bash
# Fork 仓库后
git clone https://github.com/YOUR_USERNAME/a-share-grid-trading-system.git
cd a-share-grid-trading-system

# 创建开发分支
git checkout -b feature/your-feature-name

# 开发完成后提交
git commit -m "feat: add your feature description"
git push origin feature/your-feature-name
```

### 代码规范

- 遵循 PEP 8 风格指南
- 函数必须包含 docstring
- 关键逻辑添加中文注释
- 新增功能需包含单元测试

### 提交类型

- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `style`: 代码格式调整
- `refactor`: 重构
- `test`: 测试相关
- `chore`: 构建/工具相关

---

## 📄 许可证

本项目采用 [MIT 许可证](LICENSE)

```
Copyright (c) 2026 A 股网格交易系统

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 📞 联系方式

- 📧 Email: your.email@example.com
- 💬 Issues: [GitHub Issues](https://github.com/yourusername/a-share-grid-trading-system/issues)
- 📚 文档：参见项目根目录下各 `.md` 文件

---

## 🙏 致谢

感谢以下开源项目：

- [AkShare](https://akshare.akfamily.xyz/) - A 股数据接口
- [Baostock](http://baostock.com/) - 证券数据服务
- [Optuna](https://optuna.org/) - 超参数优化框架
- [Pandas](https://pandas.pydata.org/) - 数据分析库

---

**⭐ 如果本项目对你有帮助，请给一个 Star！**

*Last Updated: 2026-03-18*
