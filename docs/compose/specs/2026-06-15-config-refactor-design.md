# 配置系统重构设计

## [S1] 问题

当前配置系统存在三个核心问题：

1. **职责混乱**：`config.yaml` 既是静态配置又是动态状态（选股后写回 stocks 列表）
2. **冗余文件**：5 个配置文件中 2 个是死代码（`config_base.yaml`、`candidate_stocks.json`）
3. **参数暴露过多**：266 行配置中大部分是系统内部参数（网格间距、波动率 k 系数、限流策略等），用户不需要也不应该修改

此外还有 3 个 Bug（已修复）：
- `save_state()` 默认路径不匹配
- `update_config_data_date()` 修改错误文件（已删除）
- 根目录 `config_state.json` 孤立（已删除）

## [S2] 新配置架构

三层存储，职责清晰：

```
config.yaml          → 用户配置（资金 + 风险偏好 + 可选覆盖）
config_state.json    → 运行时状态（系统自动维护）
SQLite               → 优化结果 + 增量更新记录 + 回测结果
```

### config.yaml（用户配置）

```yaml
version: "2.0.0"

# === 必填 ===
capital:
  total: 1000000              # 总资金（元）
  cash_reserve_ratio: 0.4     # 现金保留比例

risk:
  max_drawdown_threshold: 0.1        # 全局最大回撤
  single_stock_loss_threshold: 0.15  # 单股最大亏损
  max_positions: 5                   # 最大持仓数

# === 可选覆盖（注释掉则用系统默认值） ===
# stock_pool: auto                   # auto=系统选股, manual=用户指定
# manual_stocks: []                  # stock_pool=manual 时填写
# sectors:
#   exclude: ["银行", "地产"]         # 排除行业
# rebalance_days: 90                 # 重新优化周期（天）
# backtest:
#   n_trials: 300                    # 优化算力
# selection:
#   min_price: 5.0
#   max_price: 500.0
# fees:
#   commission_rate: 0.00015
#   stamp_tax: 0.0005
```

### config_state.json（运行时状态，系统自动维护）

```json
{
  "selection_status": {
    "completed": true,
    "last_selection_date": "2026-06-07",
    "selection_count": 83
  },
  "runtime_state": {
    "last_run_date": "2026-06-07",
    "last_run_mode": "optimize"
  },
  "regime_filter_state": {
    "confirmed_state": "normal",
    "consecutive_days": 0
  }
}
```

注意：`trading_stocks` 和 `optimization_history` 迁移到 SQLite。

### SQLite 新增表

```sql
-- 替代 output/report.json 的数据存储
CREATE TABLE optimization_results (
    code TEXT,
    optimize_date TEXT,
    final_params TEXT,          -- JSON
    phase1_params TEXT,         -- JSON
    final_calmar REAL,
    final_return REAL,
    final_drawdown REAL,
    PRIMARY KEY (code, optimize_date)
);

-- 替代 update_checkpoint.json
CREATE TABLE update_log (
    code TEXT PRIMARY KEY,
    last_success TEXT,
    last_date TEXT,
    consecutive_failures INTEGER DEFAULT 0
);
```

## [S3] 系统参数默认值硬编码

当前散落在各模块的默认值统一收敛到 `trading_core/defaults.py`：

```python
# 网格参数（优化搜索空间的默认中心值）
GRID_BASE_SPACING = 0.02
GRID_ATR_PERIOD = 20
GRID_ATR_COEF = 1.5

# 波动率区间
VOLATILITY_REGIME_K = {"low": 2.5, "medium": 2.0, "high": 1.5}
VOLATILITY_THRESHOLDS = {"low": 0.20, "high": 0.35}

# 优化搜索空间
OPTIMIZATION_RANGES = {
    "buy_spacing": (0.01, 0.06),
    "sell_spacing": (0.01, 0.06),
    "amount_multiplier": (0.3, 3.0),
    "initial_position": (0.15, 0.75),
    "max_grids": (3, 15),
    "spacing_decay": (0.5, 2.0),
}

# 费率（A 股标准）
DEFAULT_COMMISSION_RATE = 0.00015
DEFAULT_STAMP_TAX = 0.0005
DEFAULT_TRANSFER_FEE = 0.00002
DEFAULT_SLIPPAGE_RATE = 0.001

# 风控
DEFAULT_MAX_DRAWDOWN = 0.10
DEFAULT_SINGLE_STOCK_LOSS = 0.15
DEFAULT_MAX_POSITIONS = 5

# 选股
DEFAULT_MIN_PRICE = 5.0
DEFAULT_MAX_PRICE = 500.0
DEFAULT_REBALANCE_DAYS = 90
```

所有模块从 `defaults.py` 导入默认值，不再各自硬编码。

### 默认值合并机制

`load_config()` 实现 defaults + overrides 模式：

```python
def load_config(config_path="configuration/config.yaml") -> dict:
    # 1. 从 defaults.py 加载完整默认配置
    config = get_defaults()
    # 2. 加载用户配置文件
    user_config = yaml.safe_load(open(config_path))
    # 3. 递归合并：用户值覆盖默认值
    deep_merge(config, user_config)
    return config
```

用户只需在 config.yaml 中填写想覆盖的参数，其余用系统默认值：

| 用户 config.yaml | defaults.py | 最终生效 |
|-----------------|-------------|---------|
| `total: 1000000` | `TOTAL: 1000000` | 用户值 |
| `max_positions: 3` | `MAX_POSITIONS: 5` | 用户值 3 |
| 不填 `rebalance_days` | `REBALANCE_DAYS: 90` | 默认值 90 |
| 不填 `n_trials` | `N_TRIALS: 300` | 默认值 300 |

## [S4] 代码改动清单

### 新建文件
| 文件 | 内容 |
|------|------|
| `trading_core/defaults.py` | 系统参数默认值统一定义 |

### 修改文件
| 文件 | 改动 |
|------|------|
| `configuration/config.yaml` | 重写为用户配置格式（~30 行） |
| `utils/utils.py` | `load_config()` 支持新结构 + 默认值合并 |
| `trading_core/strategy.py` | 1. 删除 `stocks` 写回逻辑 2. 读取 `max_positions`、`rebalance_days` 3. 新增 `stock_pool=manual` 分支 4. 优化结果写入 SQLite |
| `trading_core/screener.py` | 1. 新增行业过滤（`sectors.exclude`） 2. 默认值从 `defaults.py` 导入 |
| `trading_core/grid_engine.py` | 默认值从 `defaults.py` 导入 |
| `risk_management/market_regime.py` | 默认值从 `defaults.py` 导入 |
| `risk_management/circuit_breaker.py` | 默认值从 `defaults.py` 导入 |
| `data_layer/fetcher.py` | 1. 删除 `candidate_stocks.json` 引用 2. 删除 `update_checkpoint.json` 引用，改用 SQLite `update_log` |
| `data_layer/market_db.py` | 新增 `optimization_results` 和 `update_log` 表的读写函数 |
| `dashboard.py` | 股票列表从 SQLite 读取，不再读 config.yaml |

### 删除文件
| 文件 | 原因 |
|------|------|
| `configuration/config_base.yaml` | 死代码，从未被读取 |
| `configuration/candidate_stocks.json` | 死代码，函数从未被调用 |
| `configuration/update_checkpoint.json` | 迁移到 SQLite `update_log` 表 |

## [S5] 迁移策略

1. **阶段 1** ✅：新建 `defaults.py` + 重写 `config.yaml` + 修改 `load_config()` 支持默认值合并
2. **阶段 2** ✅：修改各模块读取逻辑（从 defaults.py 导入默认值，config.yaml 只覆盖用户指定的值）
3. **阶段 3** ✅：SQLite 新增表 + 优化结果写入迁移
4. **阶段 4** ✅：删除死代码文件 + 清理旧逻辑
5. **阶段 5** ✅：全量测试验证

每阶段独立可验证，不影响其他阶段。

## [S6] 兼容性

- `load_config()` 保持原有签名，新增默认值合并逻辑
- 旧格式 `config.yaml` 仍可加载（向后兼容），新字段缺失时使用 defaults.py 默认值
- `config_state.json` 结构不变
- Dashboard 数据源从 config.yaml → SQLite 平滑切换
