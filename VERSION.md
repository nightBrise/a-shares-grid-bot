# 版本发布说明

当前版本：**v1.2.0** (2026-04-12)

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
