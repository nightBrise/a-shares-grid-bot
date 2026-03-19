# 版本发布说明

当前版本：**v1.0.0** (2026-03-18)

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

## v1.0.0 (2026-03-18) - 初始版本

### ✨ Added - 新增功能

#### 1. 智能选股系统
- 基于 Hurst 指数的均值回归特性识别算法
- 多维度过滤机制（流动性、价格、波动率）
- 自动排除上市时间不足 1.5 年的新股
- 支持全市场并行筛选

#### 2. 参数优化引擎
- Optuna 贝叶斯超参数优化框架集成
- In-Sample/OOS 样本内外验证机制
- Calmar Ratio 最大化目标函数
- 支持 50+ 次并行试验

#### 3. 信号生成系统
- 动态网格参数调整（基于实时波动率）
- 实盘熔断风控机制（个股 15%/全局 10%）
- A 股规则适配（T+1 交易、涨跌停检查）
- 次日交易计划自动生成

#### 4. 数据管理引擎
- 增量数据更新机制（效率提升 10 倍）
- 双数据源支持（AkShare + Baostock）
- 数据完整性检查与自动补全
- 元数据管理（metadata.json）

#### 5. Walk-Forward 分析
- 严格时间窗口分割（无前视偏差）
- 滚动窗口执行机制（支持月/季/年）
- 多期对比报告生成
- T-1.5Y/T-1Y/T-3M/T时间轴设计

#### 6. 风控机制
- 个股熔断：未实现亏损≥15% 触发
- 全局熔断：账户回撤≥10% 触发
- 状态持久化与手动重置功能
- 盈亏实时监控

### ⚡ Performance - 性能优化

- 增量数据更新：从 5 秒/只降至 0.5 秒/只（提升 10 倍）
- 全市场选股：~10 分钟（100 只股票）
- 参数优化：~30 秒/只（50 次 Optuna 试验）
- 信号生成：~0.1 秒/只

### 📚 Documentation - 文档

#### 核心文档
- README.md - 项目主文档
- VERSION.md - 本文件

#### 功能文档（docs/目录）
- quick_start.md - 快速开始指南
- project_structure.md - 项目结构详解
- walk_forward.md - Walk-Forward 分析详解
- incremental_update.md - 增量数据更新详解
- dynamic_risk_control.md - 动态风控详解
- fee_and_slippage.md - 费用与滑点分析
- listing_duration_defense.md - 新股防御机制
- lookahead_bias_fix.md - 前视偏差修复
- merge_logic_explanation.md - 数据合并逻辑
- config_files_guide.md - 配置文件指南

### ⚠️ Known Issues - 已知限制

1. **滑点成本**: 当前回测未包含滑点，建议手动扣除 0.1-0.2%
2. **数据源依赖**: 依赖第三方 API，可能存在延迟或错误
3. **市场限制**: 仅支持 A 股，暂不支持港股/美股

### 🔜 Planned Features - 后续计划

#### v1.1.0 (计划中)
- [ ] 添加滑点参数配置
- [ ] 增加更多技术指标（RSI, MACD 等）
- [ ] 支持多因子选股模型

#### v1.2.0 (计划中)
- [ ] 图形化界面（Web Dashboard）
- [ ] 实时行情接入
- [ ] 自动化交易接口对接

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
