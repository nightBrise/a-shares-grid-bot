# 回测费用与滑点分析报告

## 费用计算

### utils/utils.py

文件：`utils/utils.py` 第 203-236 行

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

### 印花税方向区分

在 `calculate_transaction_fee()` 中：

```python
tax = trade_amount * stamp_tax if trade_type == 'sell' else 0.0
#                          ↑
#                    关键判断：仅卖出收取
```

**调用示例** (`trading_core/strategy.py`):

```python
# 买入时
fee = calculate_transaction_fee(cost, 'buy', commission_rate, stamp_tax)
#                                       ↑

# 卖出时
fee = calculate_transaction_fee(revenue, 'sell', commission_rate, stamp_tax)
#                                        ↑
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

在 `configuration/config.yaml` 中：

```yaml
backtest:
  # 佣金费率：万 1.5 (0.015%)
  commission_rate: 0.00015

  # 印花税率：万 5 (0.05%) - 仅卖出收取
  stamp_tax: 0.0005

  # 滑点率：千 1 (0.1%)
  slippage_rate: 0.001
```

**当前默认值**:
| 费用类型 | 费率 | 收取方式 |
|----------|------|----------|
| 佣金 | 0.015% (万 1.5) | 买卖双向，最低 5 元 |
| 印花税 | 0.05% (万 5) | **仅卖出** |
| 过户费 | 0.002% (万 2) | 买卖双向 |
| 滑点 | 0.1% (千 1) | 买入/卖出 |

## 滑点影响

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

---

**Last Updated**: 2026-04-20
**Version**: v1.3.1
