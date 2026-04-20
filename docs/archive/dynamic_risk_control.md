# 动态网格参数调整与实盘熔断风控机制

## 概述

本次更新为 A 股网格交易系统新增两大核心功能：

1. **动态网格参数调整**：根据实时波动率与历史波动率的对比，自动调整网格间距
2. **实盘熔断风控机制**：监控持仓浮动盈亏和账户回撤，触发时暂停买入或全局停止

## 一、动态网格参数调整

### 工作原理

在生成交易信号前，系统执行以下流程：

```
1. 计算当前波动率（最近 30 个交易日的 Realized Volatility）
   ↓
2. 读取历史参考波动率（来自 Walk-Forward 优化结果）
   ↓
3. 对比并调整网格间距
   ↓
4. 生成带有 param_source 标记的交易信号
```

### 调整规则

| 条件 | 调整动作 | 示例 |
|------|----------|------|
| 当前波动率 > 参考 × 1.5 | 扩大 20% | 2.0% → 2.4% |
| 当前波动率 < 参考 × 0.5 | 缩小 20% | 2.0% → 1.6% |
| 其他 | 保持不变 | 2.0% → 2.0% |

### 波动率计算公式

```python
# Realized Volatility (年化)
log_returns = np.log(close[t] / close[t-1])
realized_vol = std(log_returns) * sqrt(252)
```

### 代码示例

```python
from strategy import calculate_realized_volatility, adjust_grid_spacing

# 计算当前波动率（近 30 日）
current_vol = calculate_realized_volatility(df['close'], period=30)

# 获取参考波动率（从历史优化）
reference_vol = opt_history[code]['ins_volatility']

# 动态调整
adjusted_spacing, param_source = adjust_grid_spacing(
    current_vol, reference_vol, base_spacing=2.0, config=config
)

print(f"调整后间距：{adjusted_spacing:.2f}%")
print(f"参数来源：{param_source}")  # "optimized" 或 "adjusted"
```

### 日志输出示例

```
INFO - 加载历史优化数据...
INFO - 找到历史优化记录：2024-12-31
INFO - 使用优化最佳参数：grid_spacing=2.5%
INFO - 当前波动率（近 30 日）：28.50%
INFO - 波动率对比：当前=28.50%, 参考=18.20%, 比率=1.57
INFO - 📈 高波动率检测：当前波动率是参考值的 1.57 倍 
       → 网格间距扩大 20% (2.50% → 3.00%)
INFO - 参数来源：adjusted
INFO - 最终网格间距：3.00%
```

## 二、实盘熔断风控机制

### 触发条件

| 熔断类型 | 触发条件 | 影响范围 |
|----------|----------|----------|
| 个股熔断 | 单只股票未实现亏损 ≥ 15% | 暂停该股买入，允许卖出 |
| 全局熔断 | 总账户回撤 ≥ 10% | 全局停止所有买入，仅保留卖出 |

### 风控检查流程

```
1. 读取持仓数据（从 config_state.json）
   ↓
2. 计算各持仓未实现盈亏
   ↓
3. 计算账户总市值和回撤
   ↓
4. 检查是否触发熔断
   ↓
5. 过滤被禁止的买入信号
   ↓
6. 保存熔断状态到 risk_state.json
```

### 配置参数

```yaml
risk_control:
  enabled: true                      # 是否启用
  
  single_stock_loss_threshold: 0.15  # 个股亏损阈值 (15%)
  max_drawdown_threshold: 0.10       # 全局回撤阈值 (10%)
  
  initial_peak: 1000000.0            # 初始峰值市值
  
  vol_adjustment_enabled: true       # 启用波动率调整
```

### 使用示例

```python
from risk_control import RiskControlManager, create_risk_control_manager

# 创建风控管理器
rc = create_risk_control_manager(config)

# 构建账户状态
positions_data = [
    {'code': '600519.SH', 'cost_price': 1800.0, 'quantity': 100},
    {'code': '000858.SZ', 'cost_price': 15.0, 'quantity': 1000}
]

account = rc.get_account_status(positions_data, cash=500000)

# 执行熔断检查
state = rc.check_circuit_breaker(account)

# 检查买入许可
if rc.should_allow_buy('600519.SH'):
    print("允许买入 600519.SH")
else:
    print("禁止买入 600519.SH (触发熔断)")

# 手动重置全局熔断（市场恢复后）
rc.reset_global_breaker()
```

### 日志输出示例

```
INFO - ======================================================================
INFO - 实盘熔断风控模块已初始化
INFO - 启用状态：是
INFO - 单股亏损阈值：15.0%
INFO - 最大回撤阈值：10.0%
INFO - 历史峰值：1,000,000.00
INFO - ======================================================================
INFO - 
INFO - 账户状态:
INFO -   总市值：920,000.00
INFO -   历史峰值：1,000,000.00
INFO -   当前回撤：8.00%
INFO -   持仓数量：2
INFO - 
INFO - 开始执行熔断检查...
WARNING - ⚠️  个股熔断触发：000858.SZ 未实现亏损 -20.00% (阈值：15.0%)
WARNING - 000858.SZ 个股熔断已激活，暂停该股买入
INFO - 熔断检查结果:
INFO -   全局熔断：否
INFO -   个股熔断：1 只
INFO -     - 000858.SZ
INFO - ----------------------------------------------------------------------
```

## 三、signals.csv 输出格式更新

### 新增字段

| 字段名 | 说明 | 取值 |
|--------|------|------|
| `strategy_version` | 策略版本号 | 从 `get_version()` 获取 |
| `param_source` | 参数来源 | `"optimized"` 或 `"adjusted"` |

### 完整字段列表

```csv
code,direction,price,quantity,amount,reason,valid_date,strategy_version,param_source
600519.SH,buy,1720.50,100,172050.00，网格第 1 层买入，间距 3.0%,2024-01-15,v1.0.0,adjusted
600519.SH,buy,1685.30,100,168530.00，网格第 2 层买入，间距 3.0%,2024-01-15,v1.0.0,adjusted
000858.SZ,sell,15.80,600,9480.00，网格第 1 层卖出，间距 2.5%,2024-01-15,v1.0.0,optimized
```

### 字段说明

- **param_source = "optimized"**: 使用 Walk-Forward 优化得到的原始参数
- **param_source = "adjusted"**: 根据实时波动率动态调整后的参数

## 四、运行模式说明

### Signal 模式（增强版）

```bash
# 使用增强配置文件
python main.py --mode signal --config config_enhanced.yaml

# 生成信号时自动执行：
# 1. 加载历史优化数据
# 2. 计算当前波动率
# 3. 动态调整网格参数
# 4. 执行熔断风控检查
# 5. 生成带 version 和 param_source 的信号
```

### Walk-Forward 模式

```bash
# 执行 Walk-Forward 分析（更新优化历史）
python main.py --mode wf --rolling 1m

# 优化结果会自动保存到：
# - output/wf_optimization_report_*.json
# - config_state.json (optimization_history 字段)
```

## 五、配置文件结构

### 完整配置示例

参见 `config_enhanced.yaml`，关键配置项：

```yaml
# 网格基础参数
grid:
  base_spacing: 2.0
  grid_amount: 10000
  max_grids: 10

# 熔断风控
risk_control:
  enabled: true
  single_stock_loss_threshold: 0.15
  max_drawdown_threshold: 0.10
  vol_adjustment_enabled: true

# 持仓数据（用于风控计算）
positions:
  - code: "600519.SH"
    cost_price: 1750.0
    quantity: 100

cash: 500000
```

## 六、状态文件说明

### config_state.json

```json
{
  "version": "v1.0.0",
  "optimization_history": {
    "600519.SH": {
      "optimization_date": "2024-12-31",
      "best_params": {
        "grid_spacing": 2.5,
        "grid_amount": 10000,
        "initial_position": 50,
        "max_grids": 8
      },
      "in_sample": {
        "calmar_ratio": 1.2345
      },
      "out_of_sample": {
        "calmar_ratio": 1.0567
      }
    }
  },
  "positions": [
    {"code": "600519.SH", "cost_price": 1750.0, "quantity": 100}
  ],
  "cash": 500000
}
```

### risk_state.json

```json
{
  "is_global_breaker": false,
  "single_stock_breakers": {
    "000858.SZ": true
  },
  "trigger_reason": "单只股票未实现亏损超过阈值 15.0%",
  "trigger_time": "2024-01-15 10:30:00",
  "peak_value": 1000000.0,
  "last_update": "2024-01-15 10:30:00"
}
```

## 七、故障排查

### 问题 1: 无法加载历史优化数据

**症状**: 日志显示"未找到历史优化记录"

**解决**:
```bash
# 先执行 Walk-Forward 优化
python main.py --mode wf --wf-date 2024-12-31

# 检查输出文件
ls -la output/wf_optimization_report_*.json
cat output/wf_optimization_report_*.json | jq '.results[0]'
```

### 问题 2: 风控状态文件不存在

**症状**: 首次运行时提示"未找到历史熔断状态文件"

**解决**: 这是正常的，系统会自动创建。如需重置：
```bash
rm output/risk_state.json
```

### 问题 3: 所有买入信号都被过滤

**症状**: signals.csv 中只有卖出信号

**检查**:
```bash
# 查看风控状态
cat output/risk_state.json

# 检查是否触发全局熔断
# is_global_breaker: true 表示全局熔断已触发
```

**解决**:
```python
# 手动重置全局熔断
from risk_control import RiskControlManager
rc = RiskControlManager(config)
rc.reset_global_breaker()
```

## 八、最佳实践

### 1. 定期执行 Walk-Forward 分析

建议每月执行一次，更新最优参数：

```bash
# 每月 1 号执行
python main.py --mode wf --rolling 1m --wf-date $(date -d 'last month' +%Y-%m-%d)
```

### 2. 每日生成信号前检查风控状态

```bash
# 查看当前熔断状态
cat output/risk_state.json | jq '.'

# 如有全局熔断，确认市场是否恢复后再重置
python main.py --mode signal
```

### 3. 波动率参数调优

根据市场特性调整阈值：

```yaml
risk_control:
  # 高波动市场（如创业板）
  vol_ratio_high: 1.3    # 原 1.5
  vol_ratio_low: 0.7     # 原 0.5
  adjustment_factor: 0.15 # 原 0.2
```

### 4. 风控阈值设置建议

| 投资风格 | 个股亏损阈值 | 全局回撤阈值 |
|----------|--------------|--------------|
| 保守型 | 10% | 8% |
| 平衡型 | 15% | 10% |
| 激进型 | 20% | 15% |

## 九、性能监控

### 关键指标

建议定期检查以下指标：

1. **参数调整频率**: `param_source="adjusted"` 的信号占比
2. **熔断触发次数**: 从 `risk_state.json` 查看
3. **波动率变化趋势**: 从日志中提取

### 监控脚本示例

```bash
#!/bin/bash
# 统计本周信号中动态调整的比例

SIGNAL_FILE="output/signals.csv"

total=$(tail -n +2 $SIGNAL_FILE | wc -l)
adjusted=$(grep ",adjusted$" $SIGNAL_FILE | wc -l)

echo "总信号数：$total"
echo "动态调整数：$adjusted"
echo "调整比例：$(echo "scale=2; $adjusted * 100 / $total" | bc)%"
```

## 十、版本兼容性

- **向后兼容**: 原有配置文件可继续使用，新增功能默认启用
- **配置迁移**: 使用 `config_enhanced.yaml` 作为模板更新现有配置
- **状态文件格式**: `config_state.json` 和 `risk_state.json` 自动创建

## 十一、相关文档

- [Walk-Forward 分析文档](WALK_FORWARD_README.md)
- [增量数据更新文档](INCREMENTAL_UPDATE_README.md)
- [系统主文档](README.md)

## 十二、更新日志

### v1.0.0 (2026-03-18)

**新增功能**:
- ✅ 动态网格参数调整（基于实时波动率）
- ✅ 实盘熔断风控机制
- ✅ signals.csv 增加 strategy_version 和 param_source 字段
- ✅ 风险状态持久化（risk_state.json）

**改进**:
- ✅ generate_signals() 函数重构，集成风控检查
- ✅ 优化历史数据加载逻辑
- ✅ 日志输出增强，包含详细的风控信息

**配置文件**:
- ✅ 新增 config_enhanced.yaml 示例
- ✅ 新增 risk_control 配置节
