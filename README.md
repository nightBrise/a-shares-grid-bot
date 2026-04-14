# 📊 A 股网格交易系统

基于均值回归原理的量化交易自动化工具，实现智能选股、参数优化、信号生成和实时风控。

[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Version](https://img.shields.io/badge/version-v1.3.1-yellow.svg)](VERSION.md)
[![License](https://img.shields.io/badge/license-CC%20BY--NC--SA%204.0-lightgrey.svg)](LICENSE)

> ⚠️ **模块状态**：选股模块 ✅ 已稳定运行 | 网格参数计算模块 🔄 优化中

---

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| 📊 **智能选股** | 多因子横截面打分（OU半衰期、Hurst、ADX、波动率） |
| 🎯 **参数优化** | Optuna 贝叶斯优化寻找最佳网格参数 |
| 📈 **信号生成** | 自动生成次日买卖计划，动态调整参数 |
| 🛡️ **严格风控** | T+1 追踪、单股/全局熔断、涨跌停跳过 |
| ⚡ **高效更新** | 增量数据更新 + HTTP Session 复用，效率提升 10 倍 |

---

## 🚀 快速开始

### 1. 安装

```bash
# Conda 方式（推荐）
conda env create -f environment.yml
conda activate rain
```

### 2. 配置

```bash
# 编辑股票池列表
vim config.yaml
```

### 3. 运行

```bash
# 选股 → 优化 → 信号
python main.py --mode select
python main.py --mode optimize
python main.py --mode signal
```

**详细说明**: [📖 快速开始指南](docs/quick_start.md)

---

## 📋 运行模式

| 模式 | 命令 | 功能 | 耗时 |
|------|------|------|------|
| **选股** | `--mode select` | 筛选适合网格交易的股票 | ~10 分钟 |
| **优化** | `--mode optimize` | Optuna 贝叶斯参数优化 | ~5 分钟 |
| **信号** | `--mode signal` | 生成次日交易计划 | ~1 分钟 |
| **Walk-Forward** | `--mode wf` | 滚动窗口验证策略稳定性 | ~30 分钟 |

---

## 📁 项目结构

```
auto_grid_trading_system/
├── main.py                  # 主入口 (~270 行)
├── strategy.py              # 核心策略 + SignalStabilizer (~2130 行)
├── data.py                  # 数据管理 (~1980 行)
├── data_http.py             # HTTP Session 管理器 (~330 行)
├── utils.py                 # 工具函数 (~360 行)
├── risk_control.py          # 熔断风控 (~560 行)
├── risk.py                  # 增强风控 (~640 行)
├── indicators.py            # 技术指标 Numba JIT (~630 行)
├── screener.py              # 高级多因子选股器 (~820 行)
├── grid_engine.py           # 动态网格引擎 (~550 行)
├── regime_filter.py         # 市场状态门控 (~400 行)
│
├── config.yaml              # 主配置文件
│
├── README.md                # 本文件
├── CLAUDE.md               # Claude Code 指导文件
├── VERSION.md              # 版本发布说明
│
└── docs/                    # 详细文档目录
    ├── quick_start.md       # 快速开始指南
    ├── project_structure.md # 项目结构详解
    ├── walk_forward.md      # Walk-Forward 分析
    ├── incremental_update.md# 增量数据更新
    └── ... (其他 6 个文档)
```

**完整结构说明**: [📖 项目结构详解](docs/project_structure.md)

---

## 🔧 核心功能

### 1. 智能选股系统

**高级多因子横截面打分 v2**（替代 Hurst<0.5 绝对阈值）:

**四因子正交化模型**:
| 因子 | 含义 | 股票权重 | ETF权重 | 逻辑 |
|------|------|---------|---------|------|
| F1: Reversion_Speed | OU半衰期 | 25% | 35% | 越短越好（快速均值回归） |
| F2: Trend_Strength | ADX | 35% | 15% | 越低越好（趋势弱） |
| F3: Vol_Quality | 波动率 | 20% | 30% | 倒U型，0.25最优 |
| F4: Path_Memory | Variance Ratio | 20% | 20% | 正交化处理 |

**信号稳定性过滤**:
- 连续3日总分 ≥ 动态阈值 → 生成可交易信号
- 触发后进入2日冷却期
- T日生成 → T+1日9:30执行

**初筛条件**:
- 成交额 ≥ 1 亿元
- 股价 5-500 元
- 上市时间 ≥ 1.5 年

**输出**: `output/stock_selection.csv`

> 💡 **为什么不用 Hurst<0.5？** A 股 Hurst > 0.5 为常态（趋势市场），绝对阈值导致无可选股票。多因子打分通过横截面相对排序选择最适合的标的。

### 2. 参数优化引擎

**搜索空间**:
- 网格间距：1.0% ~ 5.0%
- 每格金额：5000/10000/20000/50000 元
- 初始仓位：30% ~ 70%
- 最大层数：5 ~ 15

**目标**: 最大化 Calmar Ratio (年化收益/最大回撤)

**输出**: `output/report.json`

### 3. 动态网格引擎

**网格间距自适应**:

| 波动率区间 | 年化波动率 | 网格系数 k |
|------------|------------|------------|
| 低波动 | < 20% | k = 2.5（宽松） |
| 中波动 | 20% ~ 35% | k = 2.0 |
| 高波动 | > 35% | k = 1.5（紧密） |

**公式**: `ΔP = k × ATR(20)`

**输出**: `output/signals.csv`

### 4. Walk-Forward 分析

**时间窗口**:
- 选股期：T-1.5Y ~ T-3M
- 优化期：T-1Y ~ T-3M
- 验证期：T-3M ~ T

**输出**: `output/wf_*.json`

---

## 📚 文档导航

### 入门文档
- [📖 快速开始指南](docs/quick_start.md) - 3 步上手
- [📖 项目结构详解](docs/project_structure.md) - 架构说明

### 进阶文档
- [📖 Walk-Forward 分析](docs/walk_forward.md) - 滚动窗口验证
- [📖 增量数据更新](docs/incremental_update.md) - 效率提升 10 倍
- [📖 动态风控机制](docs/dynamic_risk_control.md) - 熔断保护
- [📖 费用与滑点分析](docs/fee_and_slippage.md) - 成本计算
- [📖 新股防御机制](docs/listing_duration_defense.md) - 排除次新股
- [📖 前视偏差修复](docs/lookahead_bias_fix.md) - 时间序列处理
- [📖 数据合并逻辑](docs/merge_logic_explanation.md) - 多表对齐

### 配置文档
- [📖 配置文件指南](docs/config_files_guide.md) - 参数详解

---

## ⚠️ 风险提示

**投资风险**: 量化交易可能亏损，过往业绩不代表未来表现

**代码风险**: 按"原样"提供，不保证无 Bug 或完全准确

**建议流程**:
1. ✅ 历史数据回测验证
2. ✅ 模拟盘运行≥3 个月
3. ✅ 小资金实盘测试
4. ✅ 定期监控和调整

**免责声明**: 本系统仅供学习研究，不构成投资建议。使用者应自行承担风险。

---

## 🛠️ 技术栈

- **Python**: 3.11+
- **核心库**: pandas, numpy, scipy, optuna, akshare, baostock, numba
- **数据源**: AkShare (主), Baostock (备)
- **配置**: YAML + JSON
- **加速**: Numba JIT 编译（技术指标计算）

**依赖安装**:
```bash
pip install -r requirements.txt
# 或
conda env create -f environment.yml
```

---

## 📊 性能基准

| 操作 | 耗时 (单只股票) | 备注 |
|------|----------------|------|
| 全量数据下载 | ~5 秒 | 首次运行 |
| 增量数据更新 | ~0.5 秒 | 第二次及以后 |
| 选股计算 | ~2 秒 | 包含 Hurst 指数 |
| 参数优化 | ~30 秒 | 50 次 Optuna 试验 |
| 信号生成 | ~0.1 秒 | 单次运行 |

---

## ❓ 常见问题

### Q: 需要多长时间运行一次？

**A**: 
- **选股**: 每季度 1 次（约 10 分钟）
- **优化**: 每月 1 次（约 5 分钟）
- **信号**: 每个交易日收盘后（约 1 分钟）

### Q: 支持哪些市场？

**A**: 
- ✅ A 股（上海/深圳）
- ❌ 港股/美股（暂不支持）

### Q: 需要什么基础？

**A**: 
- ✅ Python 基础（会修改配置文件即可）
- ✅ 量化交易基础知识
- ❌ 不需要编程专家

**更多 FAQ**: [📖 常见问题解答](docs/quick_start.md#常见问题)

---

## 📝 版本历史

### v1.3.1 (2026-04-14) - 动态网格引擎修复

**核心改进**:
- ✅ 统一动态网格间距量纲（百分比 vs ATR 绝对价格）
- ✅ T+1 强制平仓逻辑修复（仅对可用持仓执行平仓）
- ✅ 支持 20 日滚动中位数波动率平滑，避免信号闪烁

**核心改进**:
- ✅ 市场状态门控 RegimeFilter（三级响应 + 硬底线）
  - 正常区/预警区/熔断软区/熔断硬底线
  - 3日平滑 + 连续2日确认机制
  - 宽基指数（沪深300）ADX + 波动率分位数判断
- ✅ OU半衰期7天下限过滤微观噪声
- ✅ F3波动率打分改为高斯核倒U型 + 横截面辅过滤
- ✅ grid_engine 集成门控参数动态调整

### v0.2.0 (2026-04-13) - 交易日严格对齐

**核心改进**:
- ✅ 多数据源交叉校验 A 股日历 (AKShare + Baostock + TuShare)
- ✅ `WalkForwardWindow` 日期严格对齐真实交易日
- ✅ 信号 `valid_date` 使用 `get_next_trading_day()` 计算
- ✅ `verify_date_alignment()` 严格验证数据交易日对齐

**新增函数**:
- `get_trade_calendar()` - 多数据源交叉校验
- `is_trading_day()` / `get_previous_trading_day()` / `get_next_trading_day()`
- `align_to_trading_day()` - 日期对齐到最近交易日
- `verify_date_alignment()` - 数据对齐验证

### v1.2.0 (2026-04-12) - 选股系统生产级改进

**新增功能**:
- ✅ SignalStabilizer 信号稳定性过滤器（策略层实现，无状态设计）
  - 连续3日达标 + 冷却期机制，降低换手率
- ✅ 动态阈值软上限（0.65~0.82），牛市候选池保障
- ✅ 行业分散约束（单一行业最多3只，ETF/股票隔离）
- ✅ VR q 参数固化（q=5 对齐网格触发频率3-10日）

**架构改进**:
- ✅ 四因子正交化模型（F1-F4，减少共线性）
- ✅ 双轨权重（ETF vs 股票差异化权重）

### v1.1.0 (2026-04-10) - 多因子打分升级

**新增功能**:
- ✅ 多因子横截面打分选股器（替代 Hurst<0.5 绝对阈值）
  - F1(OUC半衰期)、F2(ADX)、F3(波动质量)、F4(Path_Memory)
  - Numba JIT 加速的向量化指标计算
- ✅ 动态网格引擎（波动率区间自适应 k=1.5/2.0/2.5）
- ✅ 增强风控模块（T+1 追踪、分层滑点、阶梯费率）
- ✅ HTTP Session 管理器（UA 轮换、连接复用）

**修复**:
- ✅ data.py 错误处理修复（异常传播、错误分类）
- ✅ source_order 重置问题
- ✅ RemoteDisconnected 等网络错误识别

### v1.0.0 (2026-03-18)

**新增功能**:
- ✅ 智能选股系统（Hurst 指数 + 多维过滤）
- ✅ 参数优化引擎（Optuna 贝叶斯优化）
- ✅ 信号生成系统（动态参数调整）
- ✅ 增量数据更新（效率提升 10 倍）
- ✅ Walk-Forward 分析（滚动窗口验证）
- ✅ 双重熔断风控（个股 15%/全局 10%）

**完整发布说明**: [📖 VERSION.md](VERSION.md)

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

- 🐛 **报告 Bug**: https://github.com/yourusername/a-share-grid-trading-system/issues
- 💡 **功能建议**: 同上
- 📧 **联系**: zhnyworking@163.com

---

## 📄 许可证

本作品采用 [知识共享署名 - 非商业性使用 - 相同方式共享 4.0 国际许可协议](http://creativecommons.org/licenses/by-nc-sa/4.0/) 进行许可。

详见 [LICENSE](LICENSE) 文件。

---

**🎉 开始你的量化交易之旅！**

*Last Updated: 2026-04-14*
