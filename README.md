# 📊 A 股网格交易系统 v1.0.0

基于均值回归原理的量化交易自动化工具，实现智能选股、参数优化、信号生成和实时风控。

[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

---

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| 📊 **智能选股** | Hurst 指数筛选均值回归标的，多维度过滤 |
| 🎯 **参数优化** | Optuna 贝叶斯优化寻找最佳网格参数 |
| 📈 **信号生成** | 自动生成次日买卖计划，动态调整参数 |
| 🛡️ **严格风控** | 个股 15%/全局 10% 双重熔断机制 |
| ⚡ **高效更新** | 增量数据更新，效率提升 10 倍 |

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
# 复制配置文件
cp config_base.yaml config.yaml

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
├── main.py                  # 主入口
├── strategy.py              # 核心策略 (~1920 行)
├── data.py                  # 数据管理 (~1700 行)
├── utils.py                 # 工具函数 (~450 行)
├── risk_control.py          # 风控模块 (~450 行)
│
├── config_base.yaml         # 基础配置
├── config_enhanced.yaml     # 增强配置（含完整风控）
│
├── README.md                # 本文件
├── VERSION.md               # 版本发布说明
│
└── docs/                    # 详细文档目录
    ├── quick_start.md       # 快速开始指南
    ├── project_structure.md # 项目结构详解
    ├── walk_forward.md      # Walk-Forward 分析
    ├── incremental_update.md# 增量数据更新
    └── ... (其他 7 个文档)
```

**完整结构说明**: [📖 项目结构详解](docs/project_structure.md)

---

## 🔧 核心功能

### 1. 智能选股系统

**选股标准**:
- Hurst 指数 < 0.5 (均值回归特性)
- 日均成交额 ≥ 5000 万元
- 股价 5-500 元
- 上市时间 ≥ 1.5 年

**输出**: `output/stock_selection.csv`

### 2. 参数优化引擎

**搜索空间**:
- 网格间距：1.0% ~ 5.0%
- 每格金额：5000/10000/20000/50000 元
- 初始仓位：30% ~ 70%
- 最大层数：5 ~ 15

**目标**: 最大化 Calmar Ratio (年化收益/最大回撤)

**输出**: `output/report.json`

### 3. 信号生成系统

**特性**:
- 动态参数调整（根据实时波动率）
- 实盘熔断检查
- A 股规则适配（T+1、涨跌停）

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
- **核心库**: pandas, numpy, optuna, akshare, baostock
- **数据源**: AkShare (主), Baostock (备)
- **配置**: YAML + JSON

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
- 📧 **联系**: your.email@example.com

---

## 📄 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

---

**🎉 开始你的量化交易之旅！**

*Last Updated: 2026-03-18*
