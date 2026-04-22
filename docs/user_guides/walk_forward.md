# Walk-Forward 时间窗口切割策略

## 概述

Walk-Forward 分析通过滚动时间窗口验证策略稳定性，确保模型在任何时刻都只使用过去的数据进行训练和验证，**完全避免前视偏差**。

> **注意**: Walk-Forward 验证已整合到 `--mode optimize` 的 Phase 2 中，无需单独运行 wf 模式。

## 时间窗口定义

假设当前日期为 **T**：

| 时间段 | 日期范围 | 用途 |
|--------|----------|------|
| Phase 1 (贝叶斯优化) | `T - 1 年` 至 `T - 3 个月` | Optuna 贝叶斯参数优化 |
| Phase 2 (WF微调) | `T - 3 个月` 至 `T` | 在未见数据上验证并微调 |

**关键规则**:
1. **禁止使用未来数据**：选股池构建期不使用 `T - 3 个月` 之后的任何数据
2. **OOS 数据绝对隔离**：样本外数据不参与任何形式的训练或参数选择

## 两阶段优化流程

```
--mode optimize
├── Phase 1: 贝叶斯优化
│     数据：T-1Y ~ T-3M
│     目标：最大化复合分数
│     输出：初始网格参数
│
└── Phase 2: WF微调
      数据：T-3M ~ T
      目标：在WF窗口上验证并微调
      搜索空间：以Phase1为中心 ±10%
      输出：最终稳健参数
```

## 核心组件

### WalkForwardWindow 类

```python
from trading_core.strategy import WalkForwardWindow
from datetime import datetime

# 创建窗口（默认使用今天作为 T）
wf = WalkForwardWindow()

# 获取各时期日期范围
universe_start, universe_end = wf.get_universe_period()    # T-1.5Y ~ T-3M
ins_start, ins_end = wf.get_ins_sample_period()             # T-1Y ~ T-3M
oos_start, oos_end = wf.get_oos_sample_period()             # T-3M ~ T
```

### 两阶段优化函数

```python
from trading_core.strategy import run_two_phase_optimization

# 执行两阶段优化
config = load_config('configuration/config.yaml')
results = run_two_phase_optimization(config)
```

## 数据切片示例

两阶段优化中的数据切片：

```python
# Phase 1 数据：用于贝叶斯优化 [T-1Y, T-3M)
df_ins = wf_window.slice_dataframe_by_period(df, period='ins')

# Phase 2 数据：用于WF微调 [T-3M, T]
df_oos = wf_window.slice_dataframe_by_period(df, period='oos')
```

## 输出文件

```
output/report.json              # 优化报告（JSON 格式）
output/优化参数报告_{date}.md    # 优化参数解释报告（Markdown 格式）
```

**report.json 结构**：
```json
{
  "optimization_date": "2026-04-20 10:00:00",
  "optimization_type": "two_phase",
  "phase1_period": "2025-04-20 至 2026-01-20",
  "phase2_period": "2026-01-20 至 2026-04-20",
  "capital_allocation": {...},
  "results": [
    {
      "code": "600519.SH",
      "phase1_params": {
        "grid_spacing": 2.5,
        "grid_amount": 10000,
        "initial_position": 50,
        "max_grids": 8
      },
      "phase1_score": 1.2345,
      "final_params": {
        "grid_spacing": 2.6,
        "grid_amount": 10000,
        "initial_position": 48,
        "max_grids": 8
      },
      "final_calmar": 2.6087,
      "final_drawdown": 0.0823,
      "final_trades": 45
    }
  ]
}
```

## 搜索空间配置

### Phase 1 - 贝叶斯优化

| 参数 | 类型 | 范围 | 步长/选项 |
|------|------|------|-----------|
| `grid_spacing` | float | 1.0% ~ 5.0% | 0.1% |
| `grid_amount` | categorical | - | 根据资金级别离散候选 |
| `initial_position` | float | 30% ~ 70% | 1% |
| `max_grids` | int | 5 ~ 15 | 1 |

### Phase 2 - WF微调

以 Phase 1 结果为中心 ±10% 局部搜索：

| 参数 | 搜索范围 |
|------|----------|
| `grid_spacing` | Phase1 ± 10% |
| `initial_position` | Phase1 ± 10% |
| `grid_amount` | 保持 Phase1 |
| `max_grids` | 保持 Phase1 |

## 交易日对齐

系统使用 `pd.DateOffset` 计算时间窗口边界，然后通过 `align_to_trading_day()` 对齐到真实 A 股交易日：

```python
from data_layer.fetcher import align_to_trading_day, get_trade_calendar

# WalkForwardWindow 内部实现
self.current_date = align_to_trading_day(raw_date, direction='backward')

# T - 3 个月，对齐到真实交易日
oos_start_raw = self.current_date - pd.DateOffset(months=3)
self.oos_start = align_to_trading_day(oos_start_raw, direction='backward')
```

**交易日历来源**（优先级）:
1. AKShare (`tool_trade_date_hist_sina`) - 优先
2. Baostock (`query_trade_dates`) - 交叉校验
3. TuShare (`trade_cal`) - 仲裁
4. Fallback（仅排除周末）- 兜底

## 复合分数公式

```
score = Calmar
      − 回撤惩罚 − 频率惩罚 − 成本惩罚 − 密度惩罚
```

| 惩罚项 | 阈值 | 量级 |
|--------|------|------|
| 回撤硬约束 | max_dd > 12% → 无效 | - |
| 回撤软惩罚 | >6% 时线性增长 | max 0.25 |
| 交易频率 | >4笔/月/股时惩罚 | max ~0.4 |
| 成本率 | >3% 初始本金时惩罚 | max ~1.0 |
| 网格密度 | density > 1.5 时对数惩罚 | max ~0.4 |
| 负年化收益 | score − 1.5 | 硬下移 |

---

**Last Updated**: 2026-04-22
**Version**: v1.6.0
