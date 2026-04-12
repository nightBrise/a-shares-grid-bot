# Walk-Forward 时间窗口切割策略实现文档

## 概述

Walk-Forward 分析是一种滚动的时间窗口验证方法，通过将历史数据划分为多个不重叠的时间段，确保模型在任何时刻都只使用过去的数据进行训练和验证，从而**完全避免前视偏差（no forward-looking bias）**。

## 时间窗口定义

假设当前日期为 **T**，系统严格定义三个时间段：

| 时间段 | 英文名称 | 日期范围 | 用途 |
|--------|----------|----------|------|
| 选股池构建期 | Universe Selection | `T - 1.5 年` 至 `T - 3 个月` | 计算四因子指标（OU半衰期、ADX、波动率、VR），筛选候选股票 |
| 样本内优化期 | In-Sample (IS) | `T - 1 年` 至 `T - 3 个月` | Optuna 贝叶斯参数优化 |
| 样本外验证期 | Out-of-Sample (OOS) | `T - 3 个月` 至 `T` | 独立验证最佳参数的表现 |

### 关键规则

1. **禁止使用未来数据**：选股池构建期不使用 `T - 3 个月` 之后的任何数据
2. **OOS 数据绝对隔离**：样本外数据不参与任何形式的训练或参数选择
3. **所有数据切片明确标注**：每个 DataFrame 操作都有注释说明时间范围

## 核心组件

### 1. WalkForwardWindow 类

```python
from strategy import WalkForwardWindow
from datetime import datetime

# 创建窗口（默认使用今天作为 T）
wf = WalkForwardWindow()

# 或指定当前日期
wf = WalkForwardWindow(datetime(2024, 12, 31))

# 获取各时期日期范围
universe_start, universe_end = wf.get_universe_period()    # T-1.5Y ~ T-3M
ins_start, ins_end = wf.get_ins_sample_period()             # T-1Y ~ T-3M
oos_start, oos_end = wf.get_oos_sample_period()             # T-3M ~ T
```

#### 主要方法

| 方法 | 说明 |
|------|------|
| `get_universe_period()` | 返回选股池构建期日期范围 |
| `get_ins_sample_period()` | 返回样本内优化期日期范围 |
| `get_oos_sample_period()` | 返回样本外验证期日期范围 |
| `slice_dataframe_by_period(df, period)` | 根据时期名称切割 DataFrame |
| `roll_forward(period)` | 向前滚动时间窗口 |

### 2. 选股池构建函数

```python
from strategy import build_universe_with_wf

# 基于 Walk-Forward 窗口构建选股池
df_universe = build_universe_with_wf(config, wf_window)
```

**数据切片注释示例**（strategy.py 第 875 行）:
```python
# === 关键：按 Walk-Forward 窗口切割数据 ===
# 仅使用选股池构建期的数据 [T-1.5Y, T-3M)
df_universe = wf_window.slice_dataframe_by_period(df, period='universe')
```

### 3. 参数优化函数

```python
from strategy import optimize_parameters_wf

# 在选股池中进行参数优化
results = optimize_parameters_wf(config, wf_window, stock_pool)
```

**数据切片注释示例**（strategy.py 第 1016 行）:
```python
# === 关键：按 Walk-Forward 窗口切割数据 ===
# In-Sample 数据：用于参数优化 [T-1Y, T-3M)
df_ins = wf_window.slice_dataframe_by_period(df, period='ins')

# Out-of-Sample 数据：用于独立验证 [T-3M, T]
df_oos = wf_window.slice_dataframe_by_period(df, period='oos')
```

### 4. 完整流程函数

```python
from strategy import run_walk_forward_analysis

# 执行完整的 Walk-Forward 分析
summary = run_walk_forward_analysis(
    config, 
    current_date=datetime(2024, 12, 31),  # 可选：指定 T
    rolling_period='1m'                    # 可选：每月滚动
)
```

## 使用方法

### 命令行方式

#### 基本用法

```bash
# 执行 Walk-Forward 分析（使用今天作为 T）
python main.py --mode wf

# 指定当前日期 T
python main.py --mode wf --wf-date 2024-12-31

# 启用滚动窗口（每月滚动一次，共 12 个窗口）
python main.py --mode wf --rolling 1m

# 指定日期并滚动
python main.py --mode wf --wf-date 2024-01-01 --rolling 1q
```

#### 滚动周期格式

| 格式 | 说明 | 示例 |
|------|------|------|
| `1w` | 每周滚动 | `--rolling 1w` |
| `1m` | 每月滚动 | `--rolling 1m` |
| `3m` | 每 3 个月滚动 | `--rolling 3m` |
| `1q` | 每季度滚动 | `--rolling 1q` |

### Python API 方式

```python
from datetime import datetime
from strategy import run_walk_forward_analysis, WalkForwardWindow

# 方式 1: 直接调用完整流程
config = load_config('config_base.yaml')
result = run_walk_forward_analysis(
    config,
    current_date=datetime(2024, 12, 31),
    rolling_period='1m'
)

# 方式 2: 分步执行
wf = WalkForwardWindow(datetime(2024, 12, 31))

# 步骤 1: 构建选股池
df_universe = build_universe_with_wf(config, wf)
stock_pool = df_universe['code'].tolist()

# 步骤 2: 参数优化
opt_results = optimize_parameters_wf(config, wf, stock_pool)

# 步骤 3: 滚动到下一个窗口
wf_next = wf.roll_forward('1m')
```

## 输出文件

Walk-Forward 分析会生成以下文件（均在 `output/` 目录）：

### 1. 选股池结果

```
wf_stock_selection_20241231.csv
```

包含列：
- `code`: 股票代码
- `price`: 最新价格
- `atr`: ATR 指标
- `volatility`: 波动率
- `hurst`: Hurst 指数
- `avg_turnover`: 日均成交额（万元）
- `rank`: 排名
- `reason`: 推荐理由

### 2. 参数优化报告

```
wf_optimization_report_20241231.json
```

结构：
```json
{
  "optimization_date": "2024-12-31 23:59:59",
  "window_config": {
    "current_date": "2024-12-31",
    "universe_period": ["2023-07-01", "2024-10-01"],
    "ins_sample_period": ["2023-12-31", "2024-10-01"],
    "oos_sample_period": ["2024-10-01", "2024-12-31"]
  },
  "results": [
    {
      "code": "600519.SH",
      "best_params": {
        "grid_spacing": 2.5,
        "grid_amount": 10000,
        "initial_position": 50,
        "max_grids": 8
      },
      "in_sample": {
        "calmar_ratio": 1.2345,
        "trials": 50
      },
      "out_of_sample": {
        "total_return": 0.0523,
        "annual_return": 0.2145,
        "max_drawdown": 0.0823,
        "sharpe_ratio": 1.5678,
        "calmar_ratio": 2.6087,
        "n_trades": 45
      }
    }
  ]
}
```

### 3. 汇总报告（滚动模式）

```
wf_summary_report_20241231_235959.json
```

包含所有滚动窗口的结果汇总。

## 日志输出示例

```
2024-12-31 10:00:00 - grid_trading - INFO - ======================================================================
2024-12-31 10:00:00 - grid_trading - INFO - Walk-Forward 时间窗口配置
2024-12-31 10:00:00 - grid_trading - INFO - ======================================================================
2024-12-31 10:00:00 - grid_trading - INFO - 当前日期 (T): 2024-12-31
2024-12-31 10:00:00 - grid_trading - INFO - 选股池构建期：2023-07-01 至 2024-10-01 (T-1.5Y ~ T-3M)
2024-12-31 10:00:00 - grid_trading - INFO - 样本内优化期：2023-12-31 至 2024-10-01 (T-1Y ~ T-3M)
2024-12-31 10:00:00 - grid_trading - INFO - 样本外验证期：2024-10-01 至 2024-12-31 (T-3M ~ T)
2024-12-31 10:00:00 - grid_trading - INFO - ======================================================================
2024-12-31 10:00:00 - grid_trading - INFO - ======================================================================
2024-12-31 10:00:00 - grid_trading - INFO - Walk-Forward 选股池构建
2024-12-31 10:00:00 - grid_trading - INFO - ======================================================================
2024-12-31 10:00:00 - grid_trading - INFO - 选股数据期间：2023-07-01 至 2024-10-01
2024-12-31 10:00:00 - grid_trading - INFO - 注意：禁止使用 2024-10-01 之后的数据（防止前视偏差）
```

## 搜索空间配置

Optuna 参数优化使用以下搜索空间：

| 参数 | 类型 | 范围 | 步长/选项 |
|------|------|------|-----------|
| `grid_spacing` | float | 1.0% ~ 5.0% | 0.1% |
| `grid_amount` | categorical | - | [5000, 10000, 20000, 50000] |
| `initial_position` | float | 30% ~ 70% | 5% |
| `max_grids` | int | 5 ~ 15 | 1 |

目标函数：**最大化 Calmar Ratio**（年化收益 / 最大回撤）

## 配置文件兼容性

新增的 Walk-Forward 功能与原有配置结构完全兼容。可通过配置文件控制以下参数：

```yaml
# config_base.yaml

backtest:
  n_trials: 50              # Optuna 试验次数
  commission_rate: 0.00015  # 佣金费率
  stamp_tax: 0.0005         # 印花税率
  max_optimize_stocks: 5    # 最多优化几只股票

selection:
  hurst_threshold: 0.5      # Hurst 指数阈值
  min_price: 5.0            # 最低股价
  max_price: 500.0          # 最高股价
  volatility_threshold: 0.8 # 波动率阈值
  max_stocks_to_process: 200 # 最大处理股票数

risk:
  min_turnover: 5000        # 最小日均成交额（万元）
```

## 非交易日对齐

系统使用 `pd.DateOffset` 确保正确处理非交易日：

```python
# 正确处理方式
from pandas import DateOffset

# T - 3 个月
oos_start = current_date - DateOffset(months=3)

# T - 1 年
ins_start = current_date - DateOffset(years=1)
```

如需进一步对齐到交易日，可在后续版本中添加：

```python
# 可选：对齐到最近的交易日
def align_to_trading_day(date, direction='backward'):
    """将日期对齐到交易日"""
    # 检查是否为周末或节假日
    while date.weekday() >= 5 or is_holiday(date):
        if direction == 'backward':
            date -= timedelta(days=1)
        else:
            date += timedelta(days=1)
    return date
```

## 代码注释规范

按照任务要求，所有 DataFrame 切片操作都添加了明确的注释：

```python
# strategy.py 875-877 in-sample period: T-1Y to T-3M
df_ins = df[(df['date'] >= ins_start) & (df['date'] < oos_start)]

# strategy.py 879-881 out-of-sample period: T-3M to T
df_oos = df[(df['date'] >= oos_start) & (df['date'] <= current_date)]
```

## 稳定性分析

通过滚动窗口机制，可以评估策略参数在不同时间段的稳定性：

```python
# 从汇总报告中提取各窗口的最佳参数
import json

with open('output/wf_summary_report_*.json') as f:
    summary = json.load(f)

# 分析参数稳定性
for window in summary['windows']:
    print(f"窗口日期：{window['window_date']}")
    for result in window['optimization_results']:
        print(f"  {result['code']}: grid_spacing={result['best_params']['grid_spacing']}%")
```

稳定的参数应该在多个滚动窗口中保持一致。

## 故障排查

### 问题 1: 选股池为空

**原因**: 数据不足或过滤条件过严

**解决**:
```bash
# 检查日志中的选股期数据量
# 降低过滤阈值
hurst_threshold: 0.55  # 原 0.5
min_turnover: 3000     # 原 5000
```

### 问题 2: OOS 数据不足

**原因**: 当前日期 T 距离数据结束太近

**解决**:
```bash
# 使用更早的日期作为 T
python main.py --mode wf --wf-date 2024-09-30
```

### 问题 3: 滚动窗口超过当前日期

**解决**: 这是正常行为，系统会自动停止滚动。

## 性能优化建议

1. **减少优化股票数**: `max_optimize_stocks: 3`
2. **减少 Optuna 试验次数**: `n_trials: 30`
3. **使用增量数据更新**: 确保 `data/metadata.json` 存在

## 参考论文

1. Bailey, D.H., López de Prado, M. (2014). "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and Non-Normality."
2. López de Prado, M. (2018). "Advances in Financial Machine Learning." Wiley.

## 版本历史

- **v1.0** (2026-03-18): 初始实现
  - WalkForwardWindow 类
  - 选股池构建函数
  - 参数优化函数（支持 IS/OOS 分割）
  - 滚动窗口机制
  - main.py 集成
