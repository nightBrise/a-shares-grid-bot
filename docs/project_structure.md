# 项目结构说明 - A 股网格交易系统 v1.0.0

## 完整目录结构

```
auto_grid_trading_system/
│
├── 📄 核心代码文件
│   ├── main.py                  # 主入口：命令行解析、流程调度
│   ├── strategy.py              # 核心策略：选股、优化、信号生成、回测
│   ├── data.py                  # 数据模块：增量更新、清洗、双数据源
│   ├── data_http.py             # HTTP Session 管理器：UA轮换、连接复用
│   ├── indicators.py             # 技术指标：Hurst、OU半衰期、ADX、ATR（Numba加速）
│   ├── screener.py              # 多因子选股器：横截面打分
│   ├── grid_engine.py           # 动态网格引擎：波动率区间自适应
│   ├── risk.py                  # 增强风控：T+1追踪、分层滑点
│   ├── utils.py                 # 工具模块：费用计算、配置加载、日志
│   └── risk_control.py          # 风控模块：熔断机制、盈亏监控
│
├── ⚙️ 配置文件
│   ├── config_base.yaml         # 基础配置（包含所有参数说明）
│   ├── config_enhanced.yaml     # 增强配置（含完整风控参数）
│   ├── config_state.json        # 动态状态（自动维护，无需手动编辑）
│   └── .env.example             # 环境变量模板
│
├── 📦 依赖配置
│   ├── requirements.txt         # pip 依赖列表
│   ├── environment.yml          # conda 环境配置
│   └── .gitignore               # Git 忽略规则
│
├── 📚 文档
│   ├── README.md                # 项目主文档（GitHub 首页展示）
│   ├── VERSION.md               # 版本发布说明
│   ├── PROJECT_STRUCTURE.md     # 本文件：项目结构说明
│   │
│   └── docs/                    # 详细功能文档目录
│       ├── INCREMENTAL_UPDATE_README.md    # 增量数据更新详解
│       ├── DYNAMIC_RISK_CONTROL_README.md  # 动态风控详解
│       ├── WALK_FORWARD_README.md          # Walk-Forward 分析详解
│       ├── FEE_AND_SLIPPAGE_ANALYSIS.md    # 费用与滑点分析
│       ├── LISTING_DURATION_DEFENSE.md     # 新股防御机制
│       ├── LOOKAHEAD_BIAS_FIX.md           # 前视偏差修复
│       └── MERGE_LOGIC_EXPLANATION.md      # 数据合并逻辑
│
├── 🧪 测试脚本
│   └── tests/
│       ├── test_merge_logic.py             # 数据合并逻辑测试
│       ├── test_merge_simple.py            # 简化版合并测试
│       ├── test_listing_duration.py        # 上市时间检查测试
│       └── test_listing_simple.py          # 简化版上市时间测试
│
├── 📁 数据目录（运行时自动生成）
│   └── data/
│       ├── *.csv                           # 股票行情数据缓存
│       └── metadata.json                   # 元数据：记录每只股票最后更新日期
│
└── 📤 输出目录（运行时自动生成）
    └── output/
        ├── signals.csv                     # 交易信号
        ├── stock_selection.csv             # 选股结果
        ├── report.json                     # 优化报告
        ├── wf_*.json                       # Walk-Forward 分析报告
        └── log.txt                         # 运行日志
```

## 核心模块职责

### 1. main.py - 主入口

**职责**:
- 解析命令行参数（`--mode`, `--config`, `--rolling` 等）
- 加载配置文件和状态文件
- 设置日志系统
- 调度各模块执行流程

**关键函数**:
```python
def parse_arguments() -> argparse.Namespace
def main() -> int
```

### 2. strategy.py - 核心策略

**职责**:
- 选股逻辑：多因子横截面打分、股票池构建
- 参数优化：Optuna 贝叶斯优化
- 信号生成：网格价格计算、动态参数调整
- 回测引擎：模拟历史交易、计算绩效指标

**关键函数**:
```python
def build_universe_with_wf(config, wf_window) -> pd.DataFrame
def optimize_parameters_wf(config, wf_window, stock_pool) -> Dict
def generate_signals(config) -> pd.DataFrame
def backtest_grid_strategy(df, ...) -> Dict
def run_walk_forward_analysis(config, ...) -> Dict
```

### 3. data.py - 数据管理

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

### 4. utils.py - 工具函数

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

### 5. risk_control.py - 风控模块

**职责**:
- 熔断机制：个股熔断、全局熔断
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

### 6. indicators.py - 技术指标计算（Numba JIT 加速）

**职责**:
- Hurst 指数：R/S 分析判断均值回归特性
- OU 半衰期：Ornstein-Uhlenbeck 过程半衰期
- ADX：平均趋向指数（趋势强度）
- ATR：平均真实波幅
- 年化波动率：对数收益率标准差 × √252

**关键函数**:
```python
def calculate_hurst_60d(df, price_col, window) -> pd.Series
def calculate_ou_half_life(df, price_col, min_periods) -> pd.Series
def calculate_adx(df, period) -> pd.DataFrame
def calculate_volatility_60d(df, price_col) -> pd.Series
def calculate_all_indicators(df) -> pd.DataFrame
```

### 7. screener.py - 多因子选股器

**职责**:
- 多因子横截面打分：OU半衰期、Hurst、ADX、波动率适配
- 初筛过滤：成交额、价格、上市时间
- Top N 排序输出

**关键类**:
```python
class MultiFactorScreener:
    def screen_universe(df_stocks, config) -> pd.DataFrame
    def calculate_factor_scores(df) -> pd.DataFrame
```

### 8. grid_engine.py - 动态网格引擎

**职责**:
- 波动率区间判断：低/中/高波动自适应 k 系数
- 网格间距计算：ΔP = k × ATR(20)
- 上下轨计算：P_ref ± 2 × σ_60d
- T+1 最小间距约束

**关键类**:
```python
class DynamicGridEngine:
    def calculate_grid_parameters(df, config) -> GridParameters
    def determine_volatility_regime(volatility, thresholds) -> str
    def generate_signals(df, grid_params) -> pd.DataFrame
```

### 9. risk.py - 增强风控模块

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

### 10. data_http.py - HTTP Session 管理器

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

## 配置文件说明

### config_base.yaml

**用途**: 静态配置文件，包含所有固定参数

**主要章节**:
```yaml
version: "1.0.0"           # 系统版本
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

### config_state.json

**用途**: 动态状态文件，自动维护

**结构**:
```json
{
  "version": "1.0.0",
  "selection_status": {
    "completed": true,
    "last_selection_date": "2026-03-18",
    "selection_count": 20
  },
  "optimization_history": {...},
  "positions": [...],
  "cash": 500000
}
```

### config_enhanced.yaml

**用途**: 增强配置，包含完整风控参数

**新增章节**:
```yaml
risk_control:
  enabled: true
  single_stock_loss_threshold: 0.15
  max_drawdown_threshold: 0.10
  vol_adjustment_enabled: true
  
positions:
  - code: "600519.SH"
    cost_price: 1750.0
    quantity: 100

cash: 500000
```

## 运行模式详解

### 1. select - 选股模式

**流程**:
```
1. 获取全市场股票列表
2. 并行获取每只股票历史数据
3. 计算 Hurst 指数、波动率等指标
4. 应用过滤条件
5. 保存选股结果到 CSV 和 config_state.json
```

**输出**:
- `output/stock_selection.csv`
- 更新 `config_state.json` 的 `stocks` 字段

### 2. optimize - 参数优化模式

**流程**:
```
1. 读取选股结果
2. 对每只股票执行 Optuna 优化
3. In-Sample 训练 + OOS 验证
4. 保存最佳参数和绩效指标
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

### 4. wf - Walk-Forward 模式

**流程**:
```
1. 创建 WalkForwardWindow（定义 T-1.5Y, T-1Y, T-3M, T）
2. 构建选股池（使用 T-1.5Y~T-3M 数据）
3. 参数优化（使用 T-1Y~T-3M 数据）
4. OOS 验证（使用 T-3M~T 数据）
5. 滚动窗口（如指定 --rolling）
6. 保存汇总报告
```

**输出**:
- `output/wf_stock_selection_*.csv`
- `output/wf_optimization_report_*.json`
- `output/wf_summary_report_*.json`

## 数据流向图

```
┌─────────────────┐
│  AkShare API    │
│  Baostock API   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   data.py       │  ◄─── 增量更新引擎
│  (数据获取)     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  data/*.csv     │  ◄─── 本地缓存
│  metadata.json  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  strategy.py    │  ◄─── 策略计算
│  (指标/选股)    │
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
  ├── strategy.py
  │     ├── data.py
  │     ├── utils.py
  │     └── risk_control.py
  │
  ├── data.py
  │     └── utils.py
  │
  └── utils.py
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
| 参数优化 | ~30 秒 | 50 次 Optuna 试验 |
| 信号生成 | ~0.1 秒 | 单次运行 |

## 扩展开发指南

### 添加新指标

在 `data.py` 中添加：

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
    from strategy import your_function
    result = your_function(config)
```

在 `strategy.py` 中实现：

```python
def your_function(config: dict):
    # 实现逻辑
    pass
```

---

**Last Updated**: 2026-03-18  
**Version**: v1.0.0
