# A 股网格交易系统 v1.0.0 发布说明

## 版本信息

- **版本号**: v1.0.0
- **发布日期**: 2026-03-18
- **Python 版本**: 3.11+
- **许可证**: MIT

## 核心功能模块

### 1. 主程序模块
- `main.py` - 系统入口，命令行解析与流程调度
- `strategy.py` - 核心策略实现（选股、优化、信号生成）
- `data.py` - 数据管理（增量更新、清洗、指标计算）
- `utils.py` - 工具函数（费用计算、配置加载、日志）
- `risk_control.py` - 风控模块（熔断机制、盈亏监控）

### 2. 配置文件
- `config_base.yaml` - 基础配置文件（包含所有参数说明）
- `config_enhanced.yaml` - 增强配置文件（含完整风控参数）
- `config_state.json` - 动态状态文件（自动维护）
- `.env.example` - 环境变量模板

### 3. 依赖配置
- `requirements.txt` - pip 依赖列表
- `environment.yml` - conda 环境配置
- `.gitignore` - Git 忽略规则

## v1.0.0 新特性

### 已实现功能

1. **智能选股系统**
   - 基于 Hurst 指数的均值回归特性识别
   - 多维度过滤（流动性、价格、波动率）
   - 自动排除上市时间不足 1.5 年的新股

2. **参数优化引擎**
   - Optuna 贝叶斯超参数优化
   - In-Sample/OOS样本内外验证
   - Calmar Ratio 最大化目标函数

3. **信号生成系统**
   - 动态网格参数调整（基于实时波动率）
   - 实盘熔断风控（个股 15%/全局 10%）
   - A 股规则适配（T+1、涨跌停）

4. **数据管理引擎**
   - 增量数据更新（效率提升 10 倍）
   - 双数据源支持（AkShare + Baostock）
   - 数据完整性检查与自动补全

5. **Walk-Forward 分析**
   - 严格时间窗口分割（无前视偏差）
   - 滚动窗口执行机制
   - 多期对比报告

6. **风控机制**
   - 个股熔断：未实现亏损≥15%
   - 全局熔断：账户回撤≥10%
   - 状态持久化与手动重置

## 文档清单

### 核心文档
- `README.md` - 项目主文档（GitHub 首页展示）
- `VERSION.md` - 版本发布说明（本文件）

### 功能文档
- `WALK_FORWARD_README.md` - Walk-Forward 分析详解
- `INCREMENTAL_UPDATE_README.md` - 增量数据更新详解
- `DYNAMIC_RISK_CONTROL_README.md` - 动态风控详解
- `FEE_AND_SLIPPAGE_ANALYSIS.md` - 费用与滑点分析
- `LISTING_DURATION_DEFENSE.md` - 新股防御机制
- `LOOKAHEAD_BIAS_FIX.md` - 前视偏差修复
- `MERGE_LOGIC_EXPLANATION.md` - 数据合并逻辑

### 测试文档
- `test_*.py` - 各功能模块测试脚本（开发用）

## 安装与使用

### 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/yourusername/a-share-grid-trading-system.git
cd auto_grid_trading_system

# 2. 创建环境
conda env create -f environment.yml
conda activate rain

# 3. 运行选股
python main.py --mode select

# 4. 参数优化
python main.py --mode optimize

# 5. 生成信号
python main.py --mode signal
```

### 运行模式

| 模式 | 命令 | 功能 |
|------|------|------|
| 选股 | `--mode select` | 筛选适合网格交易的股票 |
| 优化 | `--mode optimize` | Optuna 贝叶斯参数优化 |
| 信号 | `--mode signal` | 生成次日交易计划 |
| Walk-Forward | `--mode wf` | 滚动窗口验证 |

## 已知限制

1. **滑点未实现**：当前回测未包含滑点成本，建议手动扣除 0.1-0.2%
2. **数据源依赖**：依赖 AkShare/Baostock，可能存在延迟或错误
3. **A 股限制**：仅支持 A 股市场，不支持港股/美股

## 后续改进计划

### v1.1.0 (计划中)
- [ ] 添加滑点参数支持
- [ ] 增加更多技术指标
- [ ] 支持多因子选股

### v1.2.0 (计划中)
- [ ] 图形化界面
- [ ] 实时行情接入
- [ ] 自动化交易接口

## 贡献者

感谢以下开源项目的支持：
- AkShare - A 股数据接口
- Baostock - 证券数据服务
- Optuna - 超参数优化框架
- Pandas - 数据分析库

## 免责声明

本系统仅供学习研究使用，不构成任何投资建议。

量化交易存在风险，包括但不限于：
- 市场风险：单边下跌行情可能导致亏损
- 技术风险：系统 Bug 或数据错误
- 合规风险：请确保符合当地法律法规

使用者应自行承担风险，作者不对任何损失承担责任。

## 联系方式

- GitHub Issues: https://github.com/yourusername/a-share-grid-trading-system/issues
- Email: your.email@example.com

## 许可证

MIT License - 详见 LICENSE 文件

---

**发布时间**: 2026-03-18  
**版本状态**: ✅ Stable
