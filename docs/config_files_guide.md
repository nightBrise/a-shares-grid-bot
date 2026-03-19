# 配置文件详解 - A 股网格交易系统 v1.0.0

## 📋 配置文件概览

系统共有 4 个配置文件，各自承担不同职责：

| 文件名 | 类型 | 作用 | 是否必需 | 建议保留 |
|--------|------|------|----------|----------|
| `config_base.yaml` | 静态配置 | **主配置文件**，包含所有参数定义和详细说明 | ✅ 必需 | ✅ 保留 |
| `config_enhanced.yaml` | 静态配置 | 增强配置模板，包含完整风控参数示例 | ⚠️ 参考 | ✅ 保留（作为示例） |
| `config_state.json` | 动态状态 | 自动维护的运行状态（选股结果、优化历史等） | ✅ 必需 | ✅ 保留（自动生成） |
| `config.yaml` | 工作配置 | 用户实际使用的配置文件（由 base 复制） | ✅ 必需 | ✅ 保留（用户编辑） |

---

## 🔍 详细分析

### 1. config_base.yaml - 基础配置文件

**作用**: 
- 包含所有可配置参数的**完整定义**
- 每个参数都有详细的中文注释说明
- 作为配置模板和参考文档

**内容结构**:
```yaml
version: "1.0.0"              # 系统版本
mode: select                  # 运行模式
stocks: [...]                 # 示例股票池

grid:                         # 网格参数
  base_spacing: 2.0
  grid_amount: 10000
  max_grids: 10
  # ... 更多参数

risk:                         # 风险参数
  min_turnover: 5000
  # ... 更多参数

backtest:                     # 回测参数
  n_trials: 50
  commission_rate: 0.00015
  # ... 更多参数

paths:                        # 路径配置
  data_dir: "./data"
  output_dir: "./output"
  # ... 更多参数

selection:                    # 选股阈值
  hurst_threshold: 0.5
  # ... 更多参数

network:                      # 网络优化
  min_delay_per_stock: 2.0
  # ... 更多参数

logging:                      # 日志配置
  level: INFO
  backup_count: 30
  # ... 更多参数
```

**特点**:
- ✅ **参数最全**：包含所有可配置项
- ✅ **注释最详细**：每个参数都有说明
- ✅ **适合作为参考**：不知道某个参数时查阅此文件

**是否需要保留**: ✅ **必须保留**
- 这是配置的"源头"，所有参数的定义都在这里
- 新用户通过此文件了解系统支持哪些配置
- 不应直接修改此文件，而是复制为 `config.yaml` 后修改

---

### 2. config_enhanced.yaml - 增强配置文件

**作用**:
- 展示**高级功能**的配置方法
- 包含完整的风控参数示例
- 包含持仓和现金配置示例

**新增内容**（相比 base）:
```yaml
# ==================== 实盘熔断风控配置 ====================
risk_control:
  enabled: true
  single_stock_loss_threshold: 0.15  # 个股亏损≥15% 熔断
  max_drawdown_threshold: 0.10       # 账户回撤≥10% 全局熔断
  vol_adjustment_enabled: true       # 启用波动率动态调整
  vol_ratio_high: 1.5
  vol_ratio_low: 0.5
  adjustment_factor: 0.2

# ==================== 持仓配置 (示例) ====================
positions:
  - code: "600519.SH"
    cost_price: 1750.0
    quantity: 100
  
  - code: "000858.SZ"
    cost_price: 14.5
    quantity: 1000

cash: 500000  # 可用现金 (元)

# ==================== Walk-Forward 配置 ====================
walk_forward:
  rolling_period: "1m"
  max_rolls: 12
  default_date: ""
```

**特点**:
- ✅ **展示高级功能**：风控、持仓、Walk-Forward 等
- ✅ **适合进阶用户**：需要这些功能时参考
- ⚠️ **不是必需**：基础功能不需要这些配置

**是否需要保留**: ✅ **建议保留（作为参考模板）**
- 当用户需要使用风控功能时，可以复制此文件
- 展示了如何配置持仓和现金（用于风控计算）
- 可以作为"最佳实践"示例

**改进建议**:
- 重命名为 `config_enhanced_example.yaml` 更清晰
- 在文件中添加说明：此为示例，不要直接使用

---

### 3. config_state.json - 动态状态文件

**作用**:
- 存储**运行时产生的动态数据**
- 记录选股状态、优化历史、持仓信息等
- 由系统**自动维护**，用户无需手动编辑

**内容结构**:
```json
{
  "version": "1.0.0",
  
  "selection_status": {
    "completed": false,           // 是否已完成选股
    "last_selection_date": "",    // 最后选股日期
    "last_data_update_date": "",  // 最后数据更新日期
    "selection_count": 0,         // 选出的股票数量
    "strategy_version": "v1.0.0"  // 选股策略版本
  },
  
  "runtime_state": {
    "last_run_date": "2026-03-17",
    "last_run_mode": "select",
    "consecutive_failures": 0
  },
  
  "optimization_history": {
    "600519.SH": {
      "optimization_date": "2024-12-31",
      "best_params": {...},
      "in_sample": {...},
      "out_of_sample": {...}
    }
  },
  
  "positions": [
    {"code": "600519.SH", "cost_price": 1750.0, "quantity": 100}
  ],
  
  "cash": 500000
}
```

**特点**:
- ✅ **自动生成**：首次运行时创建
- ✅ **自动更新**：每次运行后更新状态
- ✅ **持久化状态**：下次运行时读取
- ❌ **不要手动编辑**：容易出错

**是否需要保留**: ✅ **必须保留（但由系统自动管理）**
- 用户不需要手动编辑此文件
- 删除后系统会自动重建（但会丢失历史记录）
- 建议加入 `.gitignore`（包含敏感持仓信息）

---

### 4. config.yaml - 工作配置文件

**作用**:
- **用户实际使用的配置文件**
- 从 `config_base.yaml` 复制而来
- 用户根据自己需求修改此文件

**使用流程**:
```bash
# 1. 首次使用时复制
cp config_base.yaml config.yaml

# 2. 编辑配置文件
vim config.yaml

# 3. 运行系统
python main.py --config config.yaml
```

**特点**:
- ✅ **用户编辑**：这是用户唯一需要修改的文件
- ✅ **灵活配置**：可以根据不同场景创建多个副本
  - `config_conservative.yaml` - 保守参数
  - `config_aggressive.yaml` - 激进参数
- ⚠️ **可能包含敏感信息**：如股票池、持仓等

**是否需要保留**: ✅ **必须保留**
- 这是用户的"工作区"配置文件
- 建议将 `config.yaml` 加入 `.gitignore`（避免提交个人配置）
- 提供 `config.example.yaml` 作为模板

---

## 📊 配置文件关系图

```
┌─────────────────────┐
│ config_base.yaml    │  ◄─── 基础模板（参数最全，注释最详细）
│ (基础配置模板)      │
└──────────┬──────────┘
           │ 复制
           ▼
┌─────────────────────┐
│ config.yaml         │  ◄─── 用户工作配置（实际使用）
│ (用户工作配置)      │
└──────────┬──────────┘
           │ 运行时读取
           ▼
┌─────────────────────┐
│ config_state.json   │  ◄─── 动态状态（自动维护）
│ (运行时状态)        │
└─────────────────────┘

┌─────────────────────┐
│ config_enhanced.    │  ◄─── 增强示例（参考用）
│ yaml                │
│ (增强配置示例)      │
└─────────────────────┘
```

---

## ✅ 最佳实践建议

### 文件保留策略

| 文件 | 保留建议 | Git 管理 | 说明 |
|------|----------|----------|------|
| `config_base.yaml` | ✅ 保留 | ✅ 提交 | 基础模板，包含完整参数说明 |
| `config_enhanced.yaml` | ✅ 保留（重命名） | ✅ 提交 | 建议改为 `config_enhanced_example.yaml` |
| `config_state.json` | ✅ 保留 | ❌ 忽略 | 自动生​​成，包含敏感信息 |
| `config.yaml` | ✅ 保留 | ❌ 忽略 | 用户工作配置，包含个人设置 |

### 推荐的项目结构

```
auto_grid_trading_system/
├── config_base.yaml              # ✅ 提交：基础配置模板
├── config_enhanced_example.yaml  # ✅ 提交：增强配置示例（重命名）
├── config.example.yaml           # ✅ 提交：简化版模板（可选）
│
├── config.yaml                   # ❌ 忽略：用户工作配置
├── config_state.json             # ❌ 忽略：动态状态
├── config_custom_*.yaml          # ❌ 忽略：用户自定义配置
│
└── .gitignore
```

### .gitignore 建议

```gitignore
# 用户配置文件（包含个人设置和敏感信息）
config.yaml
config_state.json
config_custom_*.yaml

# 数据和输出
data/*.csv
output/*.csv
output/*.json
output/log.txt

# 环境变量
.env
```

---

## 🔧 使用说明

### 新用户快速开始

```bash
# 1. 复制基础配置
cp config_base.yaml config.yaml

# 2. 编辑配置（修改股票池、参数等）
vim config.yaml

# 3. 运行系统
python main.py

# 4. （可选）使用增强配置
cp config_enhanced.yaml config.yaml
# 编辑并添加风控参数、持仓信息等
```

### 多场景配置

```bash
# 保守策略
cp config_base.yaml config_conservative.yaml
# 编辑：调小 grid_spacing, 降低仓位...

# 激进策略
cp config_base.yaml config_aggressive.yaml
# 编辑：调大 grid_spacing, 提高仓位...

# 分别运行
python main.py --config config_conservative.yaml
python main.py --config config_aggressive.yaml
```

---

## 📝 总结

### 必须保留的文件

1. **`config_base.yaml`** - 基础配置模板（参数定义最全）
2. **`config_state.json`** - 动态状态（系统自动维护）
3. **`config.yaml`** - 用户工作配置（实际使用）

### 建议保留的文件

1. **`config_enhanced.yaml`** - 增强配置示例（建议重命名为 `config_enhanced_example.yaml`）
   - 展示高级功能配置方法
   - 作为参考模板很有价值

### 改进建议

1. **重命名** `config_enhanced.yaml` → `config_enhanced_example.yaml`
   - 更清晰地表明这是示例文件
   - 避免用户直接使用

2. **创建简化版模板** `config.example.yaml`
   - 只包含最常用参数
   - 适合新手快速上手

3. **更新 .gitignore**
   - 忽略 `config.yaml` 和 `config_state.json`
   - 保护用户隐私和敏感信息

4. **在 README.md 中明确说明**
   - 各配置文件的作用
   - 应该修改哪个文件
   - 哪些文件不应该提交到 Git

---

**Last Updated**: 2026-03-18  
**Version**: v1.0.0
