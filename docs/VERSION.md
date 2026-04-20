# 版本发布说明

当前版本：**v1.4.1** (2026-04-20) ⚠️ 测试版

**模块状态**：
- ✅ 选股模块 - 已稳定运行
- ✅ 网格参数计算模块 - 已完成

---

## v1.4.1 (2026-04-20) - 动态股票数量计算

### 核心改进

#### 资金驱动的动态股票数量
- 新增 `calculate_optimal_stock_count()` 函数
- 公式：`可用资金 / (每格金额 × 2层)` → 向下取整
- 范围约束：1-20 只

#### 计算示例

| 资金 | 现金保留 | 可用资金 | 单股占用 | 最终数量 |
|------|---------|---------|---------|---------|
| 1万 | 40% | 6千 | 6000 | 1 |
| 5万 | 40% | 3万 | 6000 | 5 |
| 10万 | 40% | 6万 | 6000 | 10 |
| 50万+ | 40% | 30万+ | 6000 | 20 (上限) |

#### 配置依赖
- `capital.total` - 总资金
- `capital.cash_reserve_ratio` - 现金保留比例（默认 40%）
- `grid.grid_amount` - 每格金额（默认 3000）

---

## v1.4.0 (2026-04-14) - 实盘股票选择逻辑

### 核心改进

#### 1. 实盘股票选择机制
- 优化阶段完成后，根据样本外回测收益选择实盘股票
- 实盘股票保存到 `config_state.json['trading_stocks']`
- 信号生成阶段只对实盘股票获取实时数据

#### 2. 增量更新优化
- 仅对选出的 top_n 股票执行增量更新
- 实盘交易股票获取盘中实时数据
- 非实盘股票仅在收盘后更新数据

#### 3. 资金驱动的股票数量
- 实盘股票数量由资金量决定：`max_stocks = min(资金可支撑数量, 候选股票数)`
- 未来计划：支持显式配置和动态调整

### 新增配置

```json
// config_state.json
{
  "trading_stocks": ["600519.SH", "000858.SZ"],
  "optimization_history": {
    "600519.SH": {
      "best_params": {...},
      "out_of_sample_return": 0.15
    }
  }
}
```

---

## v1.3.1 (2026-04-14) - 动态网格引擎修复

### 核心改进

#### 1. 统一动态网格间距量纲
- 新增 `calculate_volatility_adjusted_spacing()`，使用波动率比率替代 ATR 绝对价格计算间距
- 公式：`adjusted = base_spacing × clip(vol_ratio, 0.8, 1.2)`
- 解决回测优化百分比、实盘用 ATR 绝对价格的量纲冲突问题

#### 2. T+1 强制平仓逻辑修复
- `check_force_close()` 新增 `available_position` 参数
- 仅对 T+1 可用持仓执行平仓，避免当日买入冻结导致委托拒单或逻辑死锁
- 新增日志记录计划卖出量、T+1 可用量、总持仓量

#### 3. 信号闪烁预防
- `calculate_grid_parameters()` 支持传入 `vol_smoothing_median` 参数
- 可使用 20 日滚动中位数波动率替代瞬时波动率，避免分钟级波动率跳变导致间距频繁重算

---

## v1.3.0 (2026-04-14) - 市场状态门控 + 因子模型优化

### 核心改进

#### 1. 市场状态门控（Regime Filter）
- 新增 `regime_filter.py` 模块，基于宽基指数（沪深300）判断市场状态
- 三级响应机制：
  - 正常区：ADX<25 且 波动率分位30%~70%，max_position=30%，spacing=1.0x
  - 预警区：ADX 25~35 或 波动率偏离边界，max_position=20%，spacing=1.2x
  - 熔断软区：ADX>35 或 波动率分位>85%/<15%，max_position=10%，spacing=1.5x
  - 熔断硬底线：全策略暂停，max_position=0%
- 状态平滑：3日移动平均 + 连续2日确认机制
- 硬底线触发条件：指数跌幅≥5%+跌停>200，或3日成交量萎缩

#### 2. OU半衰期下限过滤
- 半衰期 < 7天 视为订单簿微观噪声，返回 np.inf
- 避免网格高频触发导致手续费吞噬利润

#### 3. F3波动率打分升级
- 改用高斯核倒U型函数：exp(-(σ-σ_opt)² / 2σ₀²)
- 辅过滤：横截面排名剔除后20%尾部标的
- 保持物理意义同时适应市场周期

#### 4. 网格引擎集成门控参数
- `DynamicGridEngine` 支持动态仓位上限、网格间距乘数
- `generate_signals` 支持 can_buy/can_open_new 权限控制

### 新增文件

| 文件 | 说明 |
|------|------|
| `regime_filter.py` | 市场状态门控核心模块 |

### 配置新增

```yaml
regime_filter:
  benchmark_index: "000300.SH"
  adx_normal_max: 25
  adx_warning_max: 35
  vol_normal_low: 0.30
  vol_normal_high: 0.70
  smoothing_days: 3
  confirm_days: 2
  hard_stop:
    index_drop_threshold: 0.05
    limit_down_count: 200
```

---

## v0.2.0 (2026-04-13) - 交易日严格对齐

### 核心改进

#### 1. 多数据源交叉校验日历
- AKShare (`tool_trade_date_hist_sina`) 优先
- Baostock (`query_trade_dates`) 实时校验
- TuShare (`trade_cal`) 仲裁
- Fallback（仅排除周末）兜底

#### 2. 日期严格对齐
- `WalkForwardWindow` 所有边界日期对齐真实交易日
- `generate_signals` 使用 `get_next_trading_day()` 计算 `valid_date`
- `align_to_trading_day(date, direction)` 支持 forward/backward 两种模式

#### 3. 数据对齐验证
- `verify_date_alignment()` 严格检查数据是否包含非交易日
- 识别并警告缺失的交易日

### 新增函数

| 函数 | 说明 |
|------|------|
| `get_trade_calendar()` | 获取 A 股完整日历（带缓存） |
| `is_trading_day(date)` | 判断是否为交易日 |
| `get_previous_trading_day(date, n)` | 获取前 n 个交易日 |
| `get_next_trading_day(date, n)` | 获取后 n 个交易日 |
| `align_to_trading_day(date, direction)` | 对齐到最近交易日 |
| `verify_date_alignment(code, df)` | 验证数据交易日对齐 |

---

## v1.2.0 (2026-04-12) - 选股系统生产级改进

### 新增功能

#### 1. SignalStabilizer 信号稳定性过滤器
- 连续3日总分 ≥ 动态阈值 → 生成可交易信号
- 触发后进入2日冷却期，降低换手率
- 策略层实现（无状态设计）

#### 2. 动态阈值软上限
- 阈值范围限制在 0.65 ~ 0.82
- 牛市时防止候选池急剧收缩

#### 3. 行业分散约束
- 单一申万一级行业最多3只
- ETF 与股票隔离处理

#### 4. VR q 参数固化
- q=5 固化，对齐网格触发频率（3-10日）
- F4 与 F1/F2 正交化，减少共线性

---

## v1.1.0 (2026-04-11) - 多因子打分升级

### 新增功能

#### 1. 高级多因子选股系统
- 四因子正交化模型：F1(Reversion_Speed)、F2(Trend_Strength)、F3(Vol_Quality)、F4(Path_Memory)
- Variance Ratio 作为 Path_Memory 因子（替代 R/S 分析）
- 双轨权重机制（ETF vs 股票差异化权重）
- 动态阈值（adaptive_quantile 模式）
- 现金缓冲机制

#### 2. 动态网格引擎
- 波动率区间自适应 k 系数（低=2.5、中=2.0、高=1.5）
- T+1 最小间距约束
- 强制平仓机制

#### 3. 增强风控模块
- T+1 持仓追踪
- 分层滑点模型（0.1% 基础 + 价格分层）
- 阶梯费率计算（佣金、印花税、过户费）

#### 4. HTTP Session 管理器
- Session 复用（TCP 连接池）
- UA 轮换（12 个真实浏览器 UA）
- 定期更换 UA（默认 30 秒间隔）

#### 5. indicators.py 模块
- Numba JIT 加速的向量化指标计算
- `calculate_hurst_60d()` - Hurst 指数
- `calculate_ou_half_life()` - OU 过程半衰期
- `calculate_adx()` - ADX 趋向指标
- `calculate_volatility_60d()` - 年化波动率
- `calculate_atr()` - ATR 平均真实波幅
- `calculate_variance_ratio()` / `calculate_path_memory()` - 方差比检验

### 修复

- data.py 错误处理修复（异常传播、错误分类）
- source_order 重置问题
- RemoteDisconnected 等网络错误识别
- calculate_atr 委托实现，消除代码重复

---

## v1.0.0 (2026-03-18) - 初始版本

### 新增功能

- 智能选股系统（Hurst 指数 + 多维过滤）
- 参数优化引擎（Optuna 贝叶斯优化）
- 信号生成系统（动态参数调整）
- 增量数据更新（效率提升 10 倍）
- Walk-Forward 分析（滚动窗口验证）
- 双重熔断风控（个股 15%/全局 10%）

---

## 核心模块

| 文件 | 职责 |
|------|------|
| `main.py` | 入口点，CLI解析，模式调度 |
| `strategy.py` | 选股、回测引擎、优化、信号生成、Walk-Forward分析 |
| `data.py` | 数据获取、清洗、增量更新、自适应限流 |
| `indicators.py` | 向量化技术指标计算（Numba JIT 加速） |
| `screener.py` | 多因子横截面打分选股器 |
| `grid_engine.py` | 动态网格引擎 |
| `risk.py` | 增强风控模块 |
| `risk_control.py` | 熔断风控 |

## 依赖要求

- Python 3.11+
- numba（技术指标 JIT 加速）
- scipy（用于 Hurst 指数计算）
- akshare, baostock（数据源）
- optuna（参数优化）
- pandas, numpy（数据处理）

## 免责声明

本系统仅供学习研究使用，不构成任何投资建议。量化交易存在风险，使用者应自行承担风险。

---

**发布时间**: 2026-04-12
**版本状态**: ✅ Stable
