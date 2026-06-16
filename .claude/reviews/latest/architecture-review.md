# 架构深度审查报告 - 2026-06-07 (第3轮：间距统一最终状态)

## 审查范围

`trading_core/strategy.py` 和 `trading_core/grid_engine.py` 中 `compute_adaptive_spacing()` 间距统一功能的最终状态审查。覆盖 5 个指定审查项。

## 前置轮次已修复项确认

| 原发现 | 状态 | 验证方式 |
|--------|------|---------|
| config_base.yaml 钳位值 [0.015, 0.035] | **已修复** | 当前为 `min_spacing: 0.003, max_spacing: 0.15` |
| grid_engine.py:225-229 return 后死代码 | **已修复** | `get_effective_spacing_multiplier` 的 `return 1.0` 后直接进入 `determine_volatility_regime` |
| T+1 floor 在 clamp 之后执行可突破上限 | **已修复** | L75-77 T+1 先于 L79 clamp |
| 回测不传 daily_volatility | **已修复** | L714 计算 `daily_vol` 并传入 L718/L723 |
| strategy.py:715 循环内冗余 import | **已修复** | 删除 L691 import，改为模块顶部导入 `compute_adaptive_spacing` |
| grid_engine.py:462 硬编码回退 0.02 | **已修复** | 分层回退：ATR→波动率→默认，均带 warning 日志 |
| daily_volatility 语义不一致 | **已修复** | 回测和实盘统一优先使用 ATR ratio (`atr_20 / ref_price`) |

---

## 1. 参数默认值一致性矩阵（审查项 4）

| 参数 | grid_engine `__init__` (L155-156) | config.yaml (L14-15) | config_base.yaml (L37-39) | compute_adaptive_spacing 默认 (L46-47) | backtest 调用 (L719, L724) |
|------|---|---|---|---|---|
| min_spacing / clamp_low | `grid_cfg.get("min_spacing", 0.003)` | `0.003` | `0.003` | `clamp_low=0.003` | `clamp_low=0.003` |
| max_spacing / clamp_high | `grid_cfg.get("max_spacing", 0.15)` | `0.15` | `0.15` | `clamp_high=0.15` | `clamp_high=0.15` |
| atr_coef / base_atr_coef | `grid_cfg.get("atr_coef", 1.5)` (L152) | `1.5` (L13) | `1.5` (L35) | `atr_coef=1.5` (L44) | `atr_coef = 1.5` (L653) |

**结论：全部 5 处完全一致。无偏差。**

---

## 2. compute_adaptive_spacing 逐项审查（审查项 1）

### 2.1 除零保护 (grid_engine.py:68-69)

```python
if current_price <= 0:
    current_price = 1.0
```

`current_price <= 0` 时修复为 1.0，确保 `current_atr / current_price` 安全。行为正确。注意：`<=` 涵盖零和负数，覆盖面充分。

### 2.2 ATR ratio 计算 (grid_engine.py:70)

```python
atr_ratio = current_atr / current_price
```

公式正确。`current_atr` 为 ATR(20) 绝对值，除以 `current_price` 得到比率形式，与归一化系数 `0.02` 对齐。

### 2.3 T+1 floor 和 clamp 执行顺序 (grid_engine.py:72-79)

```python
72:    adjusted = base_spacing_pct * (atr_ratio / 0.02) * atr_coef   # Step A: ATR缩放
74:    # T+1 floor: 先于钳位
75:    if daily_volatility > 0:
76:        t1_min = 1.5 * daily_volatility
77:        adjusted = max(adjusted, t1_min)                           # Step B: T+1抬升
79:    adjusted = max(clamp_low, min(adjusted, clamp_high))           # Step C: 钳位
```

执行顺序：**ATR计算 → T+1 floor → clamp**。与 docstring 第 54-55 行描述完全一致。已从上一轮的 "clamp 先 T+1 后" 修正为正确顺序。T+1 抬升后的值仍受 `clamp_high` 约束，避免极端波动率时间距失控。

### 2.4 daily_volatility 触发条件 (grid_engine.py:75)

```python
if daily_volatility > 0:
```

传入 `0.0`（默认值）时条件为 False，跳过 T+1。设计意图明确——零值表示 "不应用 T+1 约束"。行为正确。

但当 `daily_volatility` 为 `None` 时会抛出 `TypeError`。详见第 6 节 finding #4。

### 2.5 daily_volatility 类型安全 (grid_engine.py:45)

函数签名 `daily_volatility: float = 0.0` 声明为 `float`。当前两个调用方都正确传入了 `float`（`calculate_grid_parameters` 提前转换了 None，回测传的是除法结果）。

但函数体无运行时 `None` 防御。详见第 6 节 finding #4。

### 2.6 clamp_low/clamp_high 默认值一致性

函数默认值 `clamp_low=0.003, clamp_high=0.15` 与 `grid_engine.__init__`（读取 config）、config.yaml、config_base.yaml 完全一致。`calculate_grid_parameters` 调用处（L319, L324）传入 `self.min_spacing`/`self.max_spacing` 覆盖默认值，这些值同样来自 config。

---

## 3. backtest_grid_strategy 中 compute_adaptive_spacing 调用（审查项 2）

### 3.1 daily_vol 除零风险 (strategy.py:714)

```python
714:    daily_vol = current_atr / center_price  # ATR ratio ≈ 日波动率
```

`center_price = prev_price` (L708)，来自 `prices = df['close'].values` (L650)。无显式 `center_price > 0` 检查。

若 `center_price == 0`（理论上 A 股最低价 5 元不可能，但脏数据可能）：
- L712-713: `current_atr` 被设为 `center_price * 0.02 = 0`
- L714: `daily_vol = 0 / 0 = NaN`
- `NaN` 传入 `compute_adaptive_spacing`，`NaN > 0` 为 False，T+1 跳过
- 最终输出 `clamp_low` (0.003)，静默掩盖数据异常

对比：`compute_adaptive_spacing` 内部对 `current_price` 有保护（L68-69），但 `daily_vol` 在函数外部计算，不受保护。

**严重级别：🟡 High（见第 6 节 finding #3）**

### 3.2 buy/sell 调用均传 daily_volatility

```
L718:    daily_volatility=daily_vol,   # buy 调用
L723:    daily_volatility=daily_vol,   # sell 调用
```

两个调用都传入 `daily_volatility`。确认正确。

### 3.3 import 仍在循环内 (strategy.py:715)

```python
715:    import trading_core.grid_engine as _grid_engine
```

位于 `for i, price in enumerate(prices[1:], start=1):` 循环体内（L699）。模块顶部 L34 已有 `import trading_core.grid_engine as grid_engine`。循环内 import 导致每迭代一天执行一次无意义的 `sys.modules` 查找和局部变量绑定。

此外，函数内同一模块存在两个别名：`grid_engine`（L34 模块级）和 `_grid_engine`（L715 局部）。L2208 还有第三个 import：`from trading_core.grid_engine import DynamicGridEngine`。

**严重级别：🔴 Critical（见第 6 节 finding #1）**

### 3.4 clamp 值 0.003/0.15 vs 函数默认值

完全一致。虽然显式传参与函数默认值相同（冗余），但增强了可读性和修改灵活性。安全冗余。

---

## 4. calculate_grid_parameters 中 compute_adaptive_spacing 调用（审查项 3）

### 4.1 daily_volatility 的 None 处理 (grid_engine.py:316-325)

```python
316:    buy_sp = compute_adaptive_spacing(
317:        buy_spacing_pct, ref_price, atr_20, self.base_atr_coef,
318:        daily_volatility=daily_volatility if daily_volatility is not None else 0.0,
319:        clamp_low=self.min_spacing, clamp_high=self.max_spacing,
320:    )
321:    sell_sp = compute_adaptive_spacing(
322:        sell_spacing_pct, ref_price, atr_20, self.base_atr_coef,
323:        daily_volatility=daily_volatility if daily_volatility is not None else 0.0,
324:        clamp_low=self.min_spacing, clamp_high=self.max_spacing,
325:    )
```

两个调用（buy/sell）都正确处理了 None 转换：`if None else 0.0`。确认正确。

### 4.2 atr_20 语义一致性

`calculate_grid_parameters` 的参数 `atr_20` 在 `generate_signals` 调用链中来自 `calculate_atr(df, period=20)`（strategy.py L2190），回测中来自 `_calc_atr(df, period=20)`（strategy.py L658）。两者同为 ATR(20)，语义一致。

### 4.3 self.base_atr_coef 一致性

`self.base_atr_coef = grid_cfg.get("atr_coef", 1.5)` — 与回测中 `atr_coef = 1.5` (L653) 一致。实盘和回测均使用 1.5。

---

## 5. T+1 行为验证（审查项 5）

给定参数：`price=100, atr=2.0, daily_vol=0.02, atr_coef=1.5`

有 `atr_ratio = 2.0/100 = 0.02`，`adjusted = base * (0.02/0.02) * 1.5 = base * 1.5`

| buy_spacing_pct | adjusted (ATR) | T+1 floor max(adj, 0.03) | clamp [0.003, 0.15] | 最终 | 用户预期 | 偏差 |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 0.02 | 0.030 | 0.030 | 0.030 | **0.030** | 0.03 | 无 |
| 0.01 | 0.015 | 0.030 | 0.030 | **0.030** | 0.03 | 无 |
| 0.005 | 0.0075 | 0.030 | 0.030 | **0.030** | 0.03 | 无 |
| 0.06 | 0.090 | 0.090 | 0.090 | **0.090** | 0.09 | 无 |

**结论：全部 4 种场景与预期完全一致，无任何偏差。**

---

## 6. 深度审查发现（新问题）

### 已修复问题

| 严重级别 | # | 文件:行号 | 问题 | 修复状态 |
|---------|---|----------|------|---------|
| 🔴 Critical | 1 | strategy.py:691 | 循环内冗余 import（模块级 L34 已有） | **已修复**：删除循环内 import，改为模块顶部导入 `compute_adaptive_spacing` |
| 🔴 Critical | 2 | grid_engine.py:462, strategy.py:714 | daily_volatility 语义不一致（回测 ATR ratio vs 实盘 vol/sqrt(252)） | **已修复**：统一优先使用 ATR ratio (`atr_20 / ref_price`) |
| 🟡 High | 3 | strategy.py:714 | center_price 除零无显式保护 | **已修复**：`max(center_price, 0.01)` 已添加保护 |
| 🟡 High | 4 | grid_engine.py:462 | 硬编码回退值 0.02 | **已修复**：分层回退（ATR→波动率→默认），均带 warning 日志 |
| 🔵 Medium | 5 | grid_engine.py:462 | daily_vol 硬编码回退值 0.02 | **已修复**：同上 |

---

## 7. 专项 Agent 触发建议

| 领域 | 是否建议 | 触发原因 |
|------|---------|---------|
| 性能优化师 | 是 | 循环内 import (strategy.py:715) + 多处重复 import；可审计全模块 import 开销 |
| 安全审计员 | 否 | 无安全敏感路径 |
| 测试架构师 | 是 | `daily_volatility` 语义不一致（ATR ratio vs vol/sqrt(252)）无跨路径一致性测试；`compute_adaptive_spacing` 零单元测试覆盖（边界条件：ATR=0, price=0, daily_vol=None, 极端值） |

---

## 8. 发现汇总

| 严重级别 | # | 文件:行号 | 问题 |
|---------|---|----------|------|
| 🔴 Critical | 1 | strategy.py:715 | 循环内冗余 import（模块级 L34 已有） |
| 🔴 Critical | 2 | grid_engine.py:75+494, strategy.py:714 | daily_volatility 语义不一致（回测 ATR ratio vs 实盘 vol/sqrt(252)） |
| 🟡 High | 3 | strategy.py:714 | center_price 除零无显式保护 |
| 🟡 High | 4 | grid_engine.py:75 | compute_adaptive_spacing 缺 None 防御 |
| 🔵 Medium | 5 | grid_engine.py:494 | daily_vol 硬编码回退值 0.02 |
