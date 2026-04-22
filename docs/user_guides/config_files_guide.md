# 配置文件详解 - A 股网格交易系统 v1.6.0

## 配置文件概览

系统共有 3 个配置文件，各自承担不同职责：

| 文件名 | 类型 | 作用 | 是否必需 | 建议保留 |
|--------|------|------|----------|----------|
| `configuration/config.yaml` | 静态配置 | **主配置文件**，包含所有参数定义和详细说明 | 必需 | 保留 |
| `configuration/config_base.yaml` | 静态配置 | 基础配置（与 config.yaml 相同） | 可选 | 保留（兼容） |
| `configuration/config_state.json` | 动态状态 | 自动维护的运行状态（选股结果、优化历史等） | 必需 | 保留（自动生成） |

---

## 1. configuration/config.yaml - 主配置文件

**作用**:
- 包含所有可配置参数的**完整定义**
- 每个参数都有详细的中文注释说明
- 作为配置模板和参考文档

**内容结构**:
```yaml
version: "1.5.0"              # 系统版本
mode: select                  # 运行模式
stocks: [...]                 # 示例股票池

grid:                         # 网格参数
  base_spacing: 2.0
  grid_amount: 10000
  max_grids: 10

risk:                         # 风险参数
  min_turnover: 5000

backtest:                     # 回测参数
  n_trials: 50
  commission_rate: 0.00015

paths:                        # 路径配置
  data_dir: "./cache"
  output_dir: "./output"

selection:                    # 选股阈值
  hurst_threshold: 0.5

network:                      # 网络优化
  min_delay_per_stock: 2.0

logging:                      # 日志配置
  level: INFO
  backup_count: 30
```

**特点**:
- **参数最全**：包含所有可配置项
- **注释最详细**：每个参数都有说明
- **适合作为参考**：不知道某个参数时查阅此文件

**使用流程**:
```bash
# 编辑配置文件
vim configuration/config.yaml

# 运行系统
python main.py
```

---

## 2. configuration/config_state.json - 动态状态文件

**作用**:
- 存储**运行时产生的动态数据**
- 记录选股状态、优化历史、持仓信息等
- 由系统**自动维护**，用户无需手动编辑

**内容结构**:
```json
{
  "version": "1.5.0",

  "selection_status": {
    "completed": false,
    "last_selection_date": "",
    "last_data_update_date": "",
    "selection_count": 0,
    "strategy_version": "v1.5.0"
  },

  "runtime_state": {
    "last_run_date": "2026-03-17",
    "last_run_mode": "select",
    "consecutive_failures": 0
  },

  "optimization_history": {
    "600519.SH": {
      "final_params": {...},
      "phase1_params": {...}
    }
  },

  "positions": [
    {"code": "600519.SH", "cost_price": 1750.0, "quantity": 100}
  ],

  "cash": 500000
}
```

**特点**:
- **自动生成**：首次运行时创建
- **自动更新**：每次运行后更新状态
- **持久化状态**：下次运行时读取
- **不要手动编辑**：容易出错

---

## 3. configuration/config_base.yaml - 基础配置

**作用**:
- 展示**高级功能**的配置方法
- 包含完整的风控参数示例
- 包含持仓和现金配置示例

**新增内容**（相比基础配置）:
```yaml
# ==================== 实盘熔断风控配置 ====================
risk_control:
  enabled: true
  single_stock_loss_threshold: 0.15  # 个股亏损≥15% 熔断
  max_drawdown_threshold: 0.10       # 账户回撤≥10% 全局熔断
  vol_adjustment_enabled: true       # 启用波动率动态调整

# ==================== 持仓配置 (示例) ====================
positions:
  - code: "600519.SH"
    cost_price: 1750.0
    quantity: 100

cash: 500000  # 可用现金 (元)
```

---

## 配置文件关系图

```
configuration/
├── config.yaml         # 主配置文件（用户编辑）
├── config_base.yaml    # 基础配置（参考）
└── config_state.json   # 动态状态（自动维护）
```

---

## 搜索空间自动计算

### 核心约束

系统根据 `capital.total` 配置自动计算网格参数搜索空间：

1. **网格资金池约束**: `每格金额 × 层数 ≤ 分配资金 × (1 - initial_position) × 95%`
2. **摩擦成本**: 网格间距 > 2 × 往返摩擦成本 ≈ 0.24%
3. **仓位约束**: 单格金额 ≤ 总资金 × 10%

### 资金级别

| 级别 | 资金范围 | `grid_amount` 候选 | `max_grids` 范围 |
|------|----------|---------------------|------------------|
| `small` | <10万 | [2000, 3000, 4000, 5000] | [4, 6] |
| `medium` | 10-50万 | [5000, 8000, 10000, 15000] | [4, 9] |
| `large` | >50万 | [10000, 20000, 30000, 50000] | [4, 9] |

### 计算函数

位于 `trading_core/strategy.py` 的 `build_adaptive_search_space()` 函数：

```python
from trading_core.strategy import build_adaptive_search_space

# 根据资金计算搜索空间（启动时动态裁剪）
search_space = build_adaptive_search_space(15000, 0.45)
# {'grid_amount_choices': [2000], 'max_grids_range': [3, 3],
#  'grid_spacing_range': [0.015, 0.035], 'grid_pool': 8250.0, ...}
```

---

## 多目标优化复合分数

### 为什么要多目标惩罚？

小资金策略面临三个核心矛盾：
1. **频率 vs 成本**：高频网格（密间距）在A股摩擦下吞噬本金
2. **收益 vs 回撤**：高Calmar组合往往伴随高回撤，实盘波动率跳变时易崩溃
3. **密度 vs 韧性**：密集网格（多层+小间距）在市场波动加大时断仓

### 复合分数公式

```
score = Calmar
      − max(0, (回撤比 − 0.5) × 0.5)               # 回撤软惩罚
      − max(0, (月均交易次数 − 4) × 0.15)          # 频率惩罚
      − max(0, (摩擦率 − 0.03) × 10)              # 成本惩罚
      − 0.3 × log10(密度指数 / 1.5)               # 密度惩罚（>1.5时）
      − 1.5  (若年化收益 < 0)
```

### 各惩罚项详解

| 惩罚项 | 触发条件 | 物理含义 |
|--------|----------|---------|
| 回撤硬约束 | max_dd > 12% | 12%是小资金存活红线 |
| 回撤软惩罚 | max_dd > 6% | 鼓励低回撤，但量级控制在 0.25 以内 |
| 频率惩罚 | >4笔/月/股 | A股网格合理值为3~6笔/月 |
| 成本惩罚 | 摩擦/本金 > 3% | 使用本金作分母，防毛收益为零时的奇点 |
| 密度惩罚 | max_grids / (spacing × 100) > 1.5 | 1.5%间距+6层 = 密度4.0 → 惩罚0.28 |
| 负收益 | annual_return < 0 | 直接下移1.5，避免优化器偏好微亏策略 |

---

## .gitignore 建议

```gitignore
# 配置文件
configuration/config.yaml
configuration/config_state.json

# 数据和输出
cache/*.parquet
cache/metadata.json
output/*.csv
output/*.json
output/log.txt

# 环境变量
.env
```

---

## 总结

### 必须保留的文件

1. **`configuration/config.yaml`** - 主配置文件
2. **`configuration/config_state.json`** - 动态状态

### 废弃文件

1. **`configuration/config_base.yaml`** - 已废弃，与 config.yaml 相同，仅为兼容性保留

---

**Last Updated**: 2026-04-22
**Version**: v1.6.0
