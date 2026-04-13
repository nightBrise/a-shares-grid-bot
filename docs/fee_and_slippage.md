# 回测费用与滑点分析报告

## 问题检查

### 1. 滑点（Slippage）✅❌

**现状**: **未考虑滑点**

在 `backtest_grid_strategy()` 函数中，所有交易都使用**当日收盘价**成交：

```python
# strategy.py:359-425
for i, price in enumerate(prices[1:], start=1):
    # ...
    
    # 买入：使用当日价格
    if price <= buy_price:
        cost = buy_qty * buy_price  # ← 直接使用理论价格
        # 没有考虑滑点
    
    # 卖出：使用当日价格
    if price >= sell_price:
        revenue = sell_qty * sell_price  # ← 直接使用理论价格
        # 没有考虑滑点
```

**问题**: 
- 实际交易中，买入成交价通常高于预期价（不利滑点）
- 卖出成交价通常低于预期价（不利滑点）
- 回测结果会**高估收益**

**建议改进**:

```python
def backtest_grid_strategy(df, ..., slippage_rate: float = 0.001, ...) -> Dict:
    """
    参数:
        slippage_rate: 滑点比率（默认 0.1%，即千 1）
    """
    
    # 买入时：成交价 = 理论价 × (1 + 滑点率)
    actual_buy_price = buy_price * (1 + slippage_rate)
    cost = buy_qty * actual_buy_price
    
    # 卖出时：成交价 = 理论价 × (1 - 滑点率)
    actual_sell_price = sell_price * (1 - slippage_rate)
    revenue = sell_qty * actual_sell_price
```

### 2. 交易费用 ✅

**现状**: **已完整考虑**

文件：[`utils.py`](file:///home/zhangny/rain/auto_grid_trading_system/utils.py) 第 203-236 行

```python
def calculate_transaction_fee(trade_amount: float, trade_type: str,
                              commission_rate: float = 0.00015,
                              stamp_tax: float = 0.0005,
                              transfer_fee: float = 0.00002) -> float:
    """
    计算 A 股交易费用
    
    费用构成:
    1. 佣金：成交金额 × 费率，最低 5 元 (买卖双向收取)
    2. 印花税：成交金额 × 费率 (仅卖出收取) ✅
    3. 过户费：成交金额 × 费率 (买卖双向收取)
    """
    # 佣金 (最低 5 元)
    commission = max(trade_amount * commission_rate, 5.0)
    
    # 印花税 (仅卖出) ✅
    tax = trade_amount * stamp_tax if trade_type == 'sell' else 0.0
    
    # 过户费
    transfer = trade_amount * transfer_fee
    
    total_fee = commission + tax + transfer
    
    return total_fee
```

**验证**:
- ✅ 佣金：买卖双向收取，最低 5 元
- ✅ 印花税：**仅卖出收取**（符合 A 股规则）
- ✅ 过户费：买卖双向收取

### 3. 印花税方向区分 ✅

**现状**: **正确区分买卖方向**

在 `calculate_transaction_fee()` 中（第 229 行）:

```python
tax = trade_amount * stamp_tax if trade_type == 'sell' else 0.0
#                          ↑
#                    关键判断：仅卖出收取
```

**调用示例** (`strategy.py`):

```python
# 买入时
fee = calculate_transaction_fee(cost, 'buy', commission_rate, stamp_tax)
#                                       ↑
#                                  trade_type='buy' → 印花税=0

# 卖出时
fee = calculate_transaction_fee(revenue, 'sell', commission_rate, stamp_tax)
#                                        ↑
#                                  trade_type='sell' → 印花税=revenue×0.0005
```

## 费用计算示例

### 场景：买入 10 万元，卖出 11 万元

```python
# 买入费用
buy_amount = 100000
buy_fee = calculate_transaction_fee(buy_amount, 'buy')
# 佣金：max(100000 × 0.00015, 5) = 15 元
# 印花税：0 (买入不收)
# 过户费：100000 × 0.00002 = 2 元
# 总费用：15 + 0 + 2 = 17 元

# 卖出费用
sell_amount = 110000
sell_fee = calculate_transaction_fee(sell_amount, 'sell')
# 佣金：max(110000 × 0.00015, 5) = 16.5 元
# 印花税：110000 × 0.0005 = 55 元 (仅卖出收取！)
# 过户费：110000 × 0.00002 = 2.2 元
# 总费用：16.5 + 55 + 2.2 = 73.7 元

# 净利润
net_profit = sell_amount - sell_fee - buy_amount - buy_fee
           = 110000 - 73.7 - 100000 - 17
           = 9909.3 元
```

## 配置参数

在 `config.yaml` 中：

```yaml
backtest:
  # 佣金费率：万 1.5 (0.015%)
  commission_rate: 0.00015
  
  # 印花税率：万 5 (0.05%) - 仅卖出收取
  stamp_tax: 0.0005
  
  # 过户费率：万 2 (0.002%)
  # 注意：配置文件中可能没有此项，使用默认值
```

**当前默认值**:
| 费用类型 | 费率 | 收取方式 |
|----------|------|----------|
| 佣金 | 0.015% (万 1.5) | 买卖双向，最低 5 元 |
| 印花税 | 0.05% (万 5) | **仅卖出** |
| 过户费 | 0.002% (万 2) | 买卖双向 |

## 回测代码中的费用处理

### 初始建仓 (`strategy.py`:328-335)

```python
# 初始建仓
position = int(initial_investment / first_price / 100) * 100
cash -= position * first_price + calculate_transaction_fee(
    position * first_price, 'buy', commission_rate, stamp_tax
)
#                                                ↑
#                                          买入，无印花税
```

### 网格买入 (`strategy.py`:373-398)

```python
if price <= buy_price and cash > grid_amount * 1.1:
    buy_qty = validate_buy_quantity(int(grid_amount / buy_price))
    
    if buy_qty > 0 and cash >= buy_qty * buy_price * 1.01:
        cost = buy_qty * buy_price
        fee = calculate_transaction_fee(
            cost, 'buy', commission_rate, stamp_tax
        )
        #                        ↑
        #                     买入，无印花税
        
        cash -= cost + fee
```

### 网格卖出 (`strategy.py`:400-425)

```python
if price >= sell_price and position > 0:
    sell_qty = min(position, validate_buy_quantity(int(grid_amount / sell_price)))
    
    if sell_qty > 0:
        revenue = sell_qty * sell_price
        fee = calculate_transaction_fee(
            revenue, 'sell', commission_rate, stamp_tax
        )
        #                         ↑
        #                      卖出，有印花税
        
        cash += revenue - fee  # 卖出收入减去费用
```

## 缺失的滑点影响

### 滑点对收益的影响

假设滑点率为 **0.1%** (千 1，对于流动性好的股票是合理估计)：

```python
# 无滑点（当前回测）
买入价：10.00 元
卖出价：10.50 元
毛利：(10.50 - 10.00) × 1000 = 500 元

# 有滑点（实际情况）
买入价：10.00 × 1.001 = 10.01 元 (滑点导致成本上升)
卖出价：10.50 × 0.999 = 10.4895 元 (滑点导致收入下降)
毛利：(10.4895 - 10.01) × 1000 = 479.5 元

滑点损失：500 - 479.5 = 20.5 元 (占毛利的 4.1%)
```

### 高频交易下滑点累积

网格交易的特点是**多次交易**，滑点会累积：

```
假设每次网格交易：
- 买入滑点损失：0.1%
- 卖出滑点损失：0.1%
- 单次交易滑点总损失：0.2%

如果一年交易 100 次：
滑点总损失：0.2% × 100 = 20%

这将显著降低回测收益！
```

## 改进建议

### 1. 添加滑点参数

```python
def backtest_grid_strategy(df: pd.DataFrame, 
                           grid_spacing: float,
                           grid_amount: float,
                           initial_position: float,
                           max_grids: int,
                           commission_rate: float,
                           stamp_tax: float,
                           slippage_rate: float = 0.001) -> Dict:  # 新增
    """
    网格交易回测引擎（增强版）
    
    参数:
        slippage_rate: 滑点比率（默认 0.1%）
    """
```

### 2. 在交易计算中应用滑点

```python
# 买入：实际成交价 = 理论价 × (1 + 滑点率)
actual_buy_price = buy_price * (1 + slippage_rate)
cost = buy_qty * actual_buy_price

# 卖出：实际成交价 = 理论价 × (1 - 滑点率)
actual_sell_price = sell_price * (1 - slippage_rate)
revenue = sell_qty * actual_sell_price
```

### 3. 配置文件支持

```yaml
backtest:
  commission_rate: 0.00015
  stamp_tax: 0.0005
  slippage_rate: 0.001  # 新增：滑点率 0.1%
```

### 4. 日志输出滑点成本

```python
logger.info(f"滑点成本统计:")
logger.info(f"  买入滑点损失：{total_buy_slippage:.2f}元")
logger.info(f"  卖出滑点损失：{total_sell_slippage:.2f}元")
logger.info(f"  滑点总损失：{total_slippage:.2f}元 "
            f"(占总收益的{slippage_ratio*100:.2f}%)")
```

## 总结

| 项目 | 状态 | 说明 |
|------|------|------|
| **佣金计算** | ✅ 已实现 | 买卖双向，最低 5 元 |
| **印花税** | ✅ 已实现 | **仅卖出收取**，正确区分方向 |
| **过户费** | ✅ 已实现 | 买卖双向 |
| **滑点** | ❌ 未实现 | **回测高估收益，建议添加** |
| **费用参数化** | ✅ 已实现 | 通过 config 配置 |
| **买卖方向区分** | ✅ 已实现 | `trade_type` 参数控制 |

**建议优先级**:
1. 🔴 **高优先级**: 添加滑点参数（当前回测结果偏乐观）
2. 🟡 **中优先级**: 增加滑点成本日志输出
3. 🟢 **低优先级**: 提供滑点率配置选项
