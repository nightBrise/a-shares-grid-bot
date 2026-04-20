# 项目结构说明 - A 股网格交易系统 v1.3.1

## 完整目录结构

```
auto_grid_trading_system/
│
├── 📄 根目录
│   ├── main.py                  # 主入口：命令行解析、流程调度
│   ├── README.md                # 项目主文档（GitHub 首页展示）
│   ├── VERSION.md               # 版本发布说明
│   ├── CLAUDE.md                # Claude Code 项目指南
│   ├── requirements.txt         # pip 依赖列表
│   ├── environment.yml          # conda 环境配置
│   └── .gitignore               # Git 忽略规则
│
├── 📂 trading_core/             # 核心交易逻辑
│   ├── strategy.py              # 策略：选股、回测、两阶段优化、信号
│   ├── screener.py              # 多因子选股器：横截面打分
│   ├── grid_engine.py           # 动态网格引擎：波动率区间自适应
│   └── indicators.py            # 技术指标：Hurst、OU半衰期、ADX、ATR（Numba加速）
│
├── 📂 data_layer/               # 数据层
│   ├── fetcher.py               # 数据获取：增量更新、清洗、双数据源
│   └── session_manager.py       # HTTP Session 管理器：UA轮换、连接复用
│
├── 📂 risk_management/          # 风险管理
│   ├── enhanced_risk.py         # 增强风控：T+1追踪、分层滑点
│   ├── circuit_breaker.py        # 熔断机制：个股/全局熔断
│   └── market_regime.py          # 市场状态门控：三级响应+硬底线
│
├── 📂 configuration/            # 配置
│   ├── config.yaml              # 主配置文件（包含所有参数说明）
│   ├── config_base.yaml         # 基础配置
│   └── config_state.json        # 动态状态（自动维护，无需手动编辑）
│
├── 📂 cache/                    # 数据缓存（运行时自动生成）
│   ├── *.parquet                # 股票行情数据缓存
│   └── metadata.json            # 元数据：记录每只股票最后更新日期
│
├── 📂 output/                   # 输出目录（运行时自动生成）
│   ├── signals.csv              # 交易信号
│   ├── stock_selection.csv      # 选股结果
│   └── report.json              # 优化报告
│
├── 📂 docs/                     # 文档
│   ├── project_structure.md     # 本文件：项目结构说明
│   ├── user_guides/             # 用户指南
│   │   ├── quick_start.md
│   │   ├── config_files_guide.md
│   │   ├── fee_and_slippage.md
│   │   └── walk_forward.md
│   └── archive/                 # 开发归档
│       ├── dynamic_risk_control.md
│       ├── incremental_update.md
│       ├── listing_duration_defense.md
│       ├── lookahead_bias_fix.md
│       └── merge_logic_explanation.md
│
├── 📂 tests/                    # 测试
│   ├── test_merge_logic.py
│   ├── test_merge_simple.py
│   ├── test_listing_duration.py
│   ├── test_listing_simple.py
│   └── test_stock_selection.py
│
└── 📂 utils/                    # 工具
    └── utils.py                 # 费用计算、配置加载、日志
```

## 核心模块职责

### 1. main.py - 主入口

**职责**:
- 解析命令行参数（`--mode`, `--config` 等）
- 加载配置文件和状态文件
- 设置日志系统
- 调度各模块执行流程

**运行模式**:
```bash
python main.py --mode select     # 选股模式
python main.py --mode optimize    # 两阶段优化（贝叶斯 + WF微调）
python main.py --mode signal     # 信号生成
```

### 2. trading_core/strategy.py - 核心策略

**职责**:
- 选股逻辑：多因子横截面打分、股票池构建
- 两阶段优化：Phase1 贝叶斯优化 + Phase2 WF微调
- 信号生成：网格价格计算、动态参数调整
- 回测引擎：模拟历史交易、计算绩效指标

**关键函数**:
```python
def run_selection(config) -> pd.DataFrame
def run_two_phase_optimization(config) -> Dict
def generate_signals(config) -> pd.DataFrame
def backtest_grid_strategy(df, ...) -> Dict
```

### 3. data_layer/fetcher.py - 数据管理

**职责**:
- 数据获取：AkShare/Baostock双数据源 + HTTP Session 复用
- 增量更新：仅拉取新增数据
- 数据清洗：处理缺失值、格式转换
- 技术指标：Hurst、OU半衰期、ADX、ATR、波动率

**关键函数**:
```python
def get_stock_data(code, ...) -> pd.DataFrame
def incremental_update(code, ...) -> pd.DataFrame
def append_new_data(df_existing, df_new, code) -> pd.DataFrame
def check_data_integrity(df, code) -> Tuple[bool, List]
def calculate_atr(df, period) -> pd.Series
def calculate_hurst_exponent(prices) -> float
```

### 4. utils/utils.py - 工具函数

**职责**:
- 配置加载：YAML/JSON文件解析
- 费用计算：佣金、印花税、过户费
- 日志设置：RotatingFileHandler
- 辅助函数：版本号获取、状态管理

**关键函数**:
```python
def load_config(config_path) -> dict
def load_state(state_path) -> dict
def calculate_transaction_fee(amount, trade_type, ...) -> float
def setup_logging(output_dir, ...) -> logging.Logger
def get_version() -> str
```

### 5. risk_management/circuit_breaker.py - 风控模块

**职责**:
- 熔断机制：个股熔断（15%）、全局熔断（10%）
- 盈亏监控：未实现盈亏计算
- 账户回撤：峰值跟踪
- 信号过滤：根据风控状态过滤买入信号

**关键类**:
```python
class RiskControlManager:
    def check_circuit_breaker(account_status) -> CircuitBreakerState
    def should_allow_buy(code) -> bool
    def should_allow_sell(code) -> bool
    def reset_global_breaker() -> bool
```

### 6. trading_core/indicators.py - 技术指标计算（Numba JIT 加速）

**职责**:
- Hurst 指数：R/S 分析判断均值回归特性
- OU 半衰期：Ornstein-Uhlenbeck 过程半衰期
- ADX：平均趋向指数（趋势强度）
- ATR：平均真实波幅
- 年化波动率：对数收益率标准差 × √252
- Variance Ratio：路径记忆因子（q=5 对齐网格周期）

**关键函数**:
```python
def calculate_hurst_60d(df, price_col, window) -> pd.Series
def calculate_ou_half_life(df, price_col, min_periods) -> pd.Series
def calculate_adx(df, period) -> pd.DataFrame
def calculate_volatility_60d(df, price_col) -> pd.Series
def calculate_variance_ratio(log_returns, q) -> pd.Series
def calculate_all_indicators(df) -> pd.DataFrame
```

### 7. trading_core/screener.py - 高级多因子选股器

**职责**:
- 四因子正交化横截面打分：F1(OU半衰期)、F2(ADX)、F3(波动质量)、F4(VR正交化)
- 双轨权重：ETF vs 股票差异化权重
- 动态阈值：adaptive_quantile 模式，软上限 0.82
- 行业分散约束：单一行业最多3只（ETF隔离处理）
- SignalStabilizer：连续3日达标 + 2日冷却期

**关键类**:
```python
class AdvancedMultiFactorScreener:
    def screen(stocks_data, asset_type, top_n) -> pd.DataFrame
    def calculate_scores(df, asset_type) -> pd.DataFrame
    def _apply_concentration_limits(df, max_per_industry) -> pd.DataFrame
```

### 8. trading_core/grid_engine.py - 动态网格引擎

**职责**:
- 波动率区间判断：低/中/高波动自适应 k 系数
- 网格间距计算：ΔP = base_spacing × vol_ratio
- 上下轨计算：P_ref ± 2 × σ_60d
- T+1 最小间距约束

**关键类**:
```python
class DynamicGridEngine:
    def calculate_grid_parameters(df, config) -> GridParameters
    def determine_volatility_regime(volatility, thresholds) -> str
    def generate_signals(df, grid_params) -> pd.DataFrame
```

### 9. risk_management/enhanced_risk.py - 增强风控模块

**职责**:
- T+1 持仓追踪：记录买入日期，计算可用数量
- 单股/全局熔断：亏损阈值触发
- 分层滑点模型：0.1% 基础 + 价格分层
- 阶梯费率计算：佣金、印花税、过户费

**关键类**:
```python
class EnhancedRiskControl:
    def can_buy(code, quantity, price) -> Tuple[bool, str]
    def can_sell(code, quantity) -> Tuple[bool, str]
    def record_trade(code, direction, price, quantity, ...)
    def calculate_fees(trade_amount, direction, code) -> float
    def calculate_slippage(trade_amount, price, direction) -> float
```

### 10. data_layer/session_manager.py - HTTP Session 管理器

**职责**:
- Session 复用：TCP 连接池，避免频繁建立连接
- UA 轮换：模拟不同浏览器，降低被识别概率
- 完整请求头：Accept、Accept-Language、DNT 等

**关键类**:
```python
class HTTPSessionManager:
    def get_session() -> requests.Session
    def refresh_session()
    def record_success()
    def record_failure() -> bool
```

### 11. risk_management/market_regime.py - 市场状态门控

**职责**:
- 市场状态判断：趋势/震荡/不明
- 三级响应机制
- 硬底线保护

## 配置文件说明

### configuration/config.yaml

**用途**: 静态配置文件，包含所有固定参数

**位置**: `configuration/config.yaml`

**主要章节**:
```yaml
version: "1.3.1"           # 系统版本
mode: select               # 运行模式
stocks: [...]              # 股票池
grid:                      # 网格参数
  base_spacing: 2.0
  grid_amount: 10000
risk:                      # 风险参数
backtest:                  # 回测参数
paths:                     # 路径配置
selection:                 # 选股阈值
network:                   # 网络优化
logging:                   # 日志配置
```

### configuration/config_state.json

**用途**: 动态状态文件，自动维护

**结构**:
```json
{
  "version": "1.3.1",
  "selection_status": {
    "completed": true,
    "last_selection_date": "2026-04-20",
    "selection_count": 20
  },
  "optimization_history": {...},
  "positions": [...],
  "cash": 500000
}
```

## 运行模式详解

### 1. select - 选股模式

**流程**:
```
1. 获取全市场股票列表
2. 按成交额排序取 Top N 候选
3. 并行获取每只股票历史数据
4. 计算四因子：Hurst、OU半衰期、ADX、VR
5. 横截面多元正交化 + 双轨权重打分
6. 保存选股结果到 CSV 和 config_state.json
```

**输出**:
- `output/stock_selection.csv`
- 更新 `configuration/config_state.json` 的 `stocks` 字段

### 2. optimize - 两阶段优化模式

**流程**:
```
Phase 1 (贝叶斯优化):
  - 数据范围：T-1Y ~ T-3M
  - 使用 Optuna TPE 优化
  - 目标：最大化复合分数

Phase 2 (WF微调):
  - 数据范围：T-3M ~ T
  - 以 Phase1 参数为中心 ±10% 搜索
  - 目标：在未见数据上验证并微调
```

**输出**:
- `output/report.json`

### 3. signal - 信号模式

**流程**:
```
1. 加载历史优化参数
2. 初始化风控管理器
3. 执行熔断检查
4. 计算实时波动率并动态调整参数
5. 生成网格买卖信号
6. 应用风控过滤
7. 保存信号到 CSV
```

**输出**:
- `output/signals.csv`

## 数据流向图

```
┌─────────────────┐
│  AkShare API    │
│  Baostock API   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ data_layer/     │  ◄─── fetcher.py 增量更新
│   fetcher.py    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  cache/*.parquet│  ◄─── 本地缓存
│  metadata.json   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ trading_core/   │  ◄─── strategy.py 策略计算
│   strategy.py   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  output/*.csv   │  ◄─── 输出结果
│  report.json    │
└─────────────────┘
```

## 依赖关系图

```
main.py
  ├── trading_core/strategy.py
  │     ├── trading_core/screener.py
  │     ├── trading_core/indicators.py
  │     ├── trading_core/grid_engine.py
  │     ├── data_layer/fetcher.py
  │     ├── risk_management/enhanced_risk.py
  │     ├── risk_management/circuit_breaker.py
  │     └── risk_management/market_regime.py
  │
  └── utils/utils.py
```

## 版本兼容性

| 组件 | 最低版本 | 推荐版本 |
|------|----------|----------|
| Python | 3.11 | 3.11+ |
| pandas | 2.0.0 | 2.2+ |
| numpy | 1.24.0 | 1.26+ |
| akshare | 1.10.0 | 最新稳定版 |
| optuna | 3.0.0 | 4.0+ |

## 性能基准

| 操作 | 耗时（单只股票） | 备注 |
|------|------------------|------|
| 全量数据下载 | ~5 秒 | 首次运行 |
| 增量数据更新 | ~0.5 秒 | 第二次及以后 |
| 选股计算 | ~2 秒 | 包含 Hurst 指数 |
| 两阶段优化 | ~60 秒 | Phase1 + Phase2 |

## 扩展开发指南

### 添加新指标

在 `trading_core/indicators.py` 中添加：

```python
def calculate_your_indicator(df: pd.DataFrame, param: int = 20) -> pd.Series:
    """
    计算你的自定义指标

    参数:
        df: 包含 OHLC 数据的 DataFrame
        param: 指标参数

    返回:
        指标值 Series
    """
    # 实现逻辑
    return result
```

### 添加新的运行模式

在 `main.py` 中添加：

```python
if mode == 'your_mode':
    from trading_core.strategy import your_function
    result = your_function(config)
```

在 `trading_core/strategy.py` 中实现：

```python
def your_function(config: dict):
    # 实现逻辑
    pass
```

---

**Last Updated**: 2026-04-20
**Version**: v1.3.1
