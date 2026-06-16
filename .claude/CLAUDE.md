# Python 代码规范 - A 股网格交易系统

参考 Google Python Style Guide，适用于本项目所有 Python 代码。

## 命名

| 类型 | 规则 | 示例 |
|------|------|------|
| 模块 | 小写 + 下划线 | `grid_engine.py`, `market_db.py` |
| 类 | 大驼峰 | `DynamicGridEngine`, `RiskControlManager` |
| 函数/方法 | 小写 + 下划线 | `compute_adaptive_spacing()`, `check_circuit_breaker()` |
| 常量 | 全大写 + 下划线 | `DEFAULT_INITIAL_CASH = 1000000.0` |
| 变量 | 小写 + 下划线 | `daily_vol`, `atr_20` |
| 私有 | 单下划线前缀 | `_update_checkpoint`, `_load_stock_names()` |
| 类型变量 | 大驼峰 | `GridParameters`, `Optional[float]` |

## 导入顺序

```python
# 1. 标准库
import os
import logging
from datetime import datetime

# 2. 第三方库
import pandas as pd
import numpy as np

# 3. 项目内部
from trading_core.indicators import calculate_atr
from data_layer.fetcher import get_stock_data
```

每组之间空一行。禁止循环内 import。

## 函数

- **长度**：不超过 60 行，超过则拆分
- **参数**：类型注解必填，默认值用不可变类型
- **返回值**：单一类型优先，复杂时用 `Dict`/`Tuple` 注解
- **文档**：docstring 格式：

```python
def compute_adaptive_spacing(
    base_spacing_pct: float,
    current_price: float,
    current_atr: float,
    atr_coef: float = 1.5,
) -> float:
    """ATR scaling + T+1 floor for consistent backtest/live spacing.
    
    Args:
        base_spacing_pct: 基础网格间距百分比
        current_price: 当前价格
        current_atr: ATR(20) 绝对值
        atr_coef: ATR 缩放系数
        
    Returns:
        调整后的网格间距百分比
    """
```

## 异常处理

- 禁止裸 `except:`，必须指定异常类型
- 禁止 `except Exception: pass` 静默吞异常，必须日志记录
- 使用 `logger.warning(..., exc_info=True)` 保留堆栈

```python
# 正确
except requests.Timeout as e:
    logger.warning("请求超时: %s", code, exc_info=True)
    raise

# 错误
except:
    pass
```

## 类型注解

- 函数参数和返回值必须注解
- 常用类型：`pd.DataFrame`, `pd.Series`, `Dict[str, float]`, `Optional[str]`
- 复杂返回用 `Tuple[bool, str]` 或 dataclass

## 常量与配置

- 硬编码值抽取为模块级常量
- 资金、阈值等配置项从 `config.yaml` 读取，禁止代码中写死

```python
# 正确
DEFAULT_INITIAL_CASH = 1000000.0
initial_cash = config.get('capital', {}).get('total', DEFAULT_INITIAL_CASH)

# 错误
def backtest(..., initial_cash: float = 1000000.0):
```

## 日志

- 模块级 logger：`logger = logging.getLogger("grid_trading")`
- 格式：`logger.info("%s: 买入 %d股 @%.2f", code, qty, price)`
- 禁止 f-string 进日志（延迟格式化）

## 测试

- 测试文件命名：`test_<module>.py`
- 函数命名：`test_<场景>_<预期行为>`
- 使用 pytest，断言用 `assert` 而非 `self.assertEqual`

## 禁止项

| 禁止 | 替代 |
|------|------|
| 循环内 import | 模块顶部统一导入 |
| `except: pass` | `except SpecificError: logger.warning(...)` |
| 代码中硬编码配置值 | 抽取为常量或从 config 读取 |
| 裸 `print` 输出 | 使用 `logger.info/debug` |
| 函数超过 60 行 | 拆分为子函数 |
| 未使用导入/变量 | 删除或注释 |
| `== True` / `== False` | 直接判断 / `not` |
| 空 f-string `f"无数据"` | 普通字符串 `"无数据"` |

## 提交规范

使用 Conventional Commits：`feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`。单行简洁。

## 工具链

- 静态检查：`ruff check .`
- 语法检查：`python -m py_compile <file>`
- 运行测试：`python -m pytest tests/ -x`

---

# 第二章 设计哲学

## 核心目的

基于均值回归原理，实现 A 股网格交易全流程自动化：选股 → 优化 → 信号 → 风控。

## 架构原则

```
data_layer/ (数据)  →  trading_core/ (策略)  →  risk_management/ (风控)
```

- **分层解耦**：每层可独立测试和替换
- **单一数据源**：SQLite 统一存储，替代分散 parquet
- **Dashboard 唯一输出**：所有可视化通过 Gradio，不生成 Markdown 报告

## 策略哲学

| 模块 | 核心思想 |
|------|----------|
| 选股 | 四因子正交化评分，消除因子间相关性 |
| 优化 | 样本内训练 (Phase1) + 样本外验证 (Phase2)，防过拟合 |
| 网格 | 波动率自适应间距：低波动宽松、高波动紧密 |
| 风控 | 事前门控而非事后补救：市场不好时主动停止交易 |

## 数据哲学

- **5 源轮询**：不依赖单一数据源，一个源被封不影响其他源
- **增量更新**：只下载缺失日期，避免重复下载
- **静态/动态分离**：config.yaml 存参数，config_state.json 存状态

## 关键约束

- A 股 T+1 制度：最小网格间距 ≥ 1.5 × 日均振幅
- 禁止未来数据泄露：所有数据切片仅使用该时间点之前的数据
- 风控硬底线：个股亏损 ≥ 15% 暂停买入，全局回撤 ≥ 10% 停止买入

---

*Last Updated: 2026-06-15*
