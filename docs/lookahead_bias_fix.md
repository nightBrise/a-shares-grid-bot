# 前视数据（Lookahead Bias）修复报告

## 问题发现

在检查 `strategy.py` 中的技术指标计算时，发现了**严重的前视数据问题**：

### ❌ 原代码问题（第 872-921 行）

```python
# 问题 1: 使用 T 日收盘价作为当前价格
latest = df.iloc[-1]              # T 日数据（未来数据！）
current_price = latest['close']   # ← 在 T 日开盘前不可用

# 问题 2: 使用 T 日 ATR 值
df['atr'] = calculate_atr(df, 20)
current_atr = df['atr'].iloc[-1]  # ← T 日 ATR（需要 T 日 H/L/C 数据）

# 问题 3: 波动率计算包含 T 日数据
current_vol = calculate_realized_volatility(df['close'], period=30)
# ↑ 如果 df 包含 T 日数据，则使用了未来数据
```

## 为什么这是前视偏差？

### 场景：T 日开盘前生成交易信号

```
时间线：
T-1 日 15:00  →  T-1 日收盘，数据可用
     ↓
T 日 09:00    →  生成 T 日交易信号（此时运行程序）
     ↓
T 日 15:00    →  T 日收盘，T 日数据产生
```

在 **T 日 09:00** 运行时：
- ✅ **可用数据**: T-1 日及之前的所有数据
- ❌ **不可用数据**: T 日的 Open/High/Low/Close

如果使用 `.iloc[-1]`（最新一天的数据），实际上是使用了**当晚才会产生的 T 日数据**，这就是**前视偏差（Lookahead Bias）**。

## 修复方案

### ✅ 修正后的代码

```python
# 修正 1: 使用 T-1 日收盘价作为基准
if len(df) >= 2:
    prev_close = df.iloc[-2]['close']  # T-1 日收盘价
    current_price = prev_close          # 使用 T-1 日收盘价
    logger.debug(f"使用 T-1 日收盘价：{current_price:.2f}")
else:
    current_price = df.iloc[-1]['close']  # 数据不足时的降级处理

# 修正 2: 使用 T-1 日的 ATR 值
df['atr'] = calculate_atr(df, grid_cfg.get('atr_period', 20))

if len(df) >= 2:
    current_atr = df['atr'].iloc[-2]  # T-1 日 ATR
    logger.debug(f"使用 T-1 日 ATR: {current_atr:.2f}")
else:
    current_atr = df['atr'].iloc[-1]  # 降级处理

# 修正 3: 波动率计算排除 T 日数据
current_vol = calculate_realized_volatility(df['close'].iloc[:-1], period=30)
# ↑ .iloc[:-1] 排除最后一天（T 日），仅使用 T-1 日及之前的数据
logger.info(f"当前波动率（近 30 日，T-1 数据）：{current_vol*100:.2f}%")
```

## 修复位置清单

| 文件 | 行号 | 问题 | 修复方式 |
|------|------|------|----------|
| strategy.py | 872-880 | 使用 T 日收盘价 | 改为使用 T-1 日收盘价 (`iloc[-2]`) |
| strategy.py | 931-944 | 使用 T 日 ATR | 改为使用 T-1 日 ATR (`iloc[-2]`) |
| strategy.py | 948 | 波动率计算包含 T 日 | 排除 T 日数据 (`.iloc[:-1]`) |

## 验证正确的代码

以下代码**已经正确使用历史数据**，无需修改：

### ✅ data.py - 技术指标计算

```python
# calculate_atr() - 正确使用 shift(1)
prev_close = close.shift(1)  # T-1 日收盘价
tr2 = abs(high - prev_close)  # |T 日 High - T-1 日 Close|

# calculate_volatility() - 正确使用 shift(1)
log_returns = np.log(df['close'] / df['close'].shift(1))
# ↑ 计算的是 T 日收益率（需要 T 日和 T-1 日收盘价）
# 但在回测中，这是历史数据，不存在前视问题

# calculate_hurst_exponent() - 仅使用传入的价格序列
prices = price_series.values  # 使用已提供的历史数据
```

### ✅ strategy.py - 回测引擎

```python
# backtest_grid_strategy() - 正确从第二天开始
for i, price in enumerate(prices[1:], start=1):
    prev_price = prices[i - 1]  # 使用前一日价格
    # ↑ 在回测中，每天都只使用当日及之前的数据
```

## 前视偏差测试方法

### 单元测试示例

```python
def test_no_lookahead_bias():
    """测试信号生成不使用未来数据"""
    
    # 模拟 T 日开盘前的场景
    df_T_minus_1 = get_historical_data(end_date='2024-01-15')  # T-1 日数据
    df_T = get_historical_data(end_date='2024-01-16')          # T 日数据
    
    # 在 T 日开盘前，只能获得 T-1 日数据
    config = load_config()
    signals = generate_signals_with_data(config, df_T_minus_1)
    
    # 验证：使用的价格应该是 T-1 日收盘价
    assert signals['base_price'] == df_T_minus_1.iloc[-1]['close']
    
    # 错误做法（会失败）：
    # assert signals['base_price'] == df_T.iloc[-1]['close']  # ← 这是 T 日数据！
```

### 日志验证

修复后，日志应显示：

```
INFO - 使用 T-1 日收盘价：1750.00
DEBUG - 使用 T-1 日 ATR: 35.20
INFO - 当前波动率（近 30 日，T-1 数据）：28.50%
```

## 影响范围

### 受影响的函数

1. **`generate_signals()`** - 主要修复对象
   - 用于生成次日交易计划
   - 在实盘场景下运行（T 日开盘前）

2. **`calculate_realized_volatility()`** - 新增参数切片
   - 调用时传入 `.iloc[:-1]` 排除 T 日

### 不受影响的函数

1. **`backtest_grid_strategy()`** - 回测中使用历史数据，无此问题
2. **`build_universe_with_wf()`** - 选股时使用完整历史数据，无此问题
3. **`optimize_parameters_wf()`** - 优化时使用 In-Sample 数据，无此问题

## 最佳实践建议

### 1. 明确数据时间点

```python
# 好的命名习惯
t_minus_1_close = df.iloc[-2]['close']    # 清晰
latest = df.iloc[-1]['close']             # 模糊，不知道是 T 日还是 T-1 日

# 添加注释说明
current_price = df.iloc[-2]['close']  # T-1 日收盘价（T 日开盘前可用）
```

### 2. 在函数文档中说明时间假设

```python
def generate_signals(config: dict) -> pd.DataFrame:
    """
    生成次日交易计划（在 T 日开盘前运行）
    
    数据可用性:
    - 可使用：T-1 日及之前的所有数据
    - 不可使用：T 日的 Open/High/Low/Close
    """
```

### 3. 添加断言验证

```python
# 在关键位置添加断言
assert len(df) >= 2, "至少需要 2 天数据（T-1 日和 T-2 日）"
assert df['date'].iloc[-1] <= trading_date - timedelta(days=1), \
    "数据日期不能超过 T-1 日"
```

## 总结

### 修复内容

- ✅ 修正 `current_price` 使用 T-1 日收盘价
- ✅ 修正 `current_atr` 使用 T-1 日 ATR
- ✅ 修正 `current_vol` 排除 T 日数据
- ✅ 添加详细注释说明时间假设

### 修复后状态

- ✅ **无前视偏差** - 所有指标计算仅基于 T-1 日及之前的数据
- ✅ **向后兼容** - 回测和选股逻辑不受影响
- ✅ **日志清晰** - 明确标注使用的数据来源

### 验收标准

运行信号生成模式，检查日志：

```bash
python main.py --mode signal --config config_enhanced.yaml

# 检查输出：
# ✓ "使用 T-1 日收盘价"
# ✓ "使用 T-1 日 ATR"
# ✓ "当前波动率（近 30 日，T-1 数据）"
```

## 相关文档

- [动态网格参数调整文档](DYNAMIC_RISK_CONTROL_README.md)
- [Walk-Forward 分析文档](WALK_FORWARD_README.md)
- [回测系统验证文档](BACKTEST_VALIDATION.md)
