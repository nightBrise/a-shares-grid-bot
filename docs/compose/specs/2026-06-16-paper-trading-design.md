# 全自动模拟盘交易模块设计

## [S1] 设计目标

实现**全自动模拟盘**：后台常驻进程，每日自动获取实时行情、生成信号、模拟执行、记录持仓和盈亏，无需人工干预。

**与实盘的关系**：
- 模拟盘验证通过后，切换实盘只需改配置 `trading.mode: live` + 配置券商API
- 模拟盘和实盘共用同一套信号生成、风控、持仓管理逻辑
- 通用接口层提供 `MockAdapter`（模拟盘）和真实券商适配器（实盘）

## [S2] 核心设计原则

1. **全自动运行** — 后台进程每日自动执行，无需手动触发
2. **实时行情驱动** — 接入实时API获取价格，按实时价格判断触发
3. **T+1 严格模拟** — 今日买入的份额明日才能卖出
4. **风控前置** — 信号生成阶段过滤，执行阶段二次确认
5. **逐笔成交模拟** — 按实时价格逐笔判断，非收盘统一执行

## [S3] 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                     全自动模拟盘引擎                           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ 行情获取模块  │  │ 信号生成模块  │  │ 模拟执行模块        │  │
│  │ (实时API轮询)│  │ (generate_  │  │ (MockAdapter)       │  │
│  │             │  │  signals)    │  │                     │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
│         │                │                    │             │
│         └────────────────┼────────────────────┘             │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              风控与持仓管理模块                        │  │
│  │  RiskControlManager + 虚拟持仓表 + 虚拟账户表          │  │
│  └─────────────────────────────────────────────────────┘  │
│                          │                                  │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              数据持久化层 (SQLite)                     │  │
│  │  paper_positions | paper_trades | paper_account        │  │
│  └─────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## [S4] 运行流程（收盘后模拟模式）

### 设计原则：简单、与回测一致

模拟盘采用**收盘后统一模拟**模式：
1. **数据准备** — 检查并增量更新数据到今天收盘
2. **生成信号** — 复用现有 `generate_signals()`，使用 T-1 数据生成预设网格价格
3. **收盘后模拟** — 获取当日收盘价，判断是否触及预设信号价格
4. **触及即成交** — 收盘价触及预设买入/卖出价时，按收盘价模拟成交
5. **更新持仓** — 更新虚拟持仓、账户、交易记录

### 每日执行流程

```python
def run_paper_trading(config: dict) -> None:
    """收盘后模拟盘执行 — 每日运行一次"""
    
    # 1. 数据准备：检查并增量更新数据到今天收盘
    logger.info("检查数据更新...")
    stocks = config.get('stocks', [])
    for code in stocks:
        # 检查数据是否最新
        latest_date = get_latest_data_date(code)
        if latest_date < today():
            logger.info(f"{code} 数据需要更新: {latest_date} -> {today()}")
            # 增量更新数据
            incremental_update(code, start_date=latest_date, end_date=today())
    
    # 2. 生成信号（复用现有 generate_signals()）
    logger.info("生成当日交易信号...")
    signals_df = generate_signals(config)
    
    # 3. 获取当日收盘价
    logger.info("获取当日行情数据...")
    today_data = get_today_data(config['stocks'])
    
    # 4. 加载虚拟持仓和账户
    broker = MockAdapter(config)
    broker.connect(config)
    positions = broker.get_positions()
    account = broker.get_account()
    
    # 5. 运行风控检查
    rc = RiskControlManager(config)
    rc.check_circuit_breaker(build_account_status(positions, account))
    
    # 6. 模拟执行信号
    for _, signal in signals_df.iterrows():
        code = signal['code']
        today_close = today_data.get(code, {}).get('close')
        
        if not today_close:
            continue
        
        # 检查收盘价是否触发信号
        if is_triggered(signal, today_close):
            execute_paper_signal(signal, today_close, account, positions, broker)
    
    # 7. 更新账户和持仓
    broker.save_account(account)
    broker.save_positions(positions)
    
    # 8. 生成日报
    generate_paper_report(account, positions, today_data)
    
    logger.info("模拟盘执行完成")


def is_triggered(signal: pd.Series, close_price: float) -> bool:
    """检查收盘价是否触发信号"""
    if signal['direction'] == "buy":
        # 买入信号：收盘价 <= 预设买入价（价格跌到网格买入点）
        return close_price <= signal['price']
    else:  # sell
        # 卖出信号：收盘价 >= 预设卖出价（价格涨到网格卖出点）
        return close_price >= signal['price']


def execute_paper_signal(signal, close_price, account, positions, broker):
    """执行单个信号"""
    code = signal['code']
    direction = signal['direction']
    quantity = signal['quantity']
    
    if direction == "buy":
        # 检查资金
        needed = close_price * quantity * (1 + FEE_RATE)
        if account.cash < needed:
            return
        
        # 模拟买入（按收盘价成交）
        broker.buy(code, close_price, quantity)
        
    else:  # sell
        # 检查可卖持仓（T+1）
        pos = positions.get(code)
        if not pos or pos.available_quantity < quantity:
            return
        
        # 模拟卖出
        broker.sell(code, close_price, quantity)
```

## [S5] 存储设计（SQLite）

```sql
-- 虚拟持仓表
CREATE TABLE paper_positions (
    code TEXT PRIMARY KEY,
    total_quantity INTEGER,      -- 总持仓（含冻结）
    available_quantity INTEGER,   -- 可卖持仓（T+1后释放）
    frozen_quantity INTEGER,      -- 今日买入冻结数量
    avg_cost_price REAL,         -- 平均成本价
    market_value REAL,            -- 当前市值
    last_update TEXT
);

-- 虚拟交易记录表
CREATE TABLE paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT,
    direction TEXT,               -- buy/sell
    price REAL,                   -- 成交价格
    quantity INTEGER,
    amount REAL,                -- 成交金额
    fee REAL,                    -- 手续费
    stamp_tax REAL,              -- 印花税（仅卖出）
    trade_date TEXT,
    trade_time TEXT,             -- 成交时间
    signal_id TEXT,              -- 关联信号ID
    pnl REAL,                    -- 盈亏（卖出时计算）
    status TEXT                  -- filled/partial/rejected
);

-- 虚拟账户表（单条记录，id=1）
CREATE TABLE paper_account (
    id INTEGER PRIMARY KEY DEFAULT 1,
    cash REAL,                   -- 可用现金
    total_value REAL,            -- 总市值
    peak_value REAL,             -- 历史峰值
    max_drawdown REAL,           -- 当前回撤
    daily_pnl REAL,              -- 当日盈亏
    total_trades INTEGER,        -- 总交易次数
    last_update TEXT
);

-- 每日结算快照
CREATE TABLE paper_daily_snapshots (
    date TEXT PRIMARY KEY,
    cash REAL,
    total_value REAL,
    market_value REAL,
    max_drawdown REAL,
    daily_pnl REAL,
    trade_count INTEGER,
    positions TEXT               -- JSON格式持仓列表
);
```

## [S6] MockAdapter（模拟盘适配器）

```python
class MockAdapter(BrokerAdapter):
    """模拟盘适配器 — 用SQLite模拟券商接口"""
    
    def __init__(self, config: dict):
        self.config = config
        self.db = MarketDB(config['database']['path'])
        self.slippage = config['paper_trading']['slippage']
        self.fee_rate = config['trading']['fee_rate']
        self.stamp_tax_rate = config['trading']['stamp_tax_rate']
    
    def connect(self, config: dict) -> bool:
        """初始化账户和持仓"""
        # 检查是否已有账户数据
        account = self.db.get_paper_account()
        if not account:
            # 首次运行，初始化
            self.db.init_paper_account(
                cash=config['paper_trading']['initial_cash']
            )
        return True
    
    def buy(self, code: str, price: float, quantity: int) -> OrderResult:
        """模拟买入"""
        # 应用滑点
        fill_price = price * (1 + self.slippage)
        
        # 计算费用
        amount = fill_price * quantity
        fee = max(amount * self.fee_rate, 5.0)  # 最低5元
        total_cost = amount + fee
        
        # 检查资金
        account = self.db.get_paper_account()
        if account.cash < total_cost:
            return OrderResult(
                order_id=f"MOCK_{uuid4()}",
                status="rejected",
                filled_quantity=0,
                filled_price=0,
                fee=0,
                message="资金不足"
            )
        
        # 更新账户
        account.cash -= total_cost
        self.db.save_paper_account(account)
        
        # 更新持仓（T+1：今日买入冻结）
        pos = self.db.get_paper_position(code)
        if pos:
            # 更新平均成本
            total_cost_basis = pos.avg_cost_price * pos.total_quantity + fill_price * quantity
            pos.total_quantity += quantity
            pos.frozen_quantity += quantity
            pos.avg_cost_price = total_cost_basis / pos.total_quantity
        else:
            pos = PaperPosition(
                code=code,
                total_quantity=quantity,
                available_quantity=0,  # 今日买入，明日可用
                frozen_quantity=quantity,
                avg_cost_price=fill_price,
                market_value=fill_price * quantity
            )
        
        self.db.save_paper_position(pos)
        
        # 记录交易
        trade = PaperTrade(
            code=code,
            direction="buy",
            price=fill_price,
            quantity=quantity,
            amount=amount,
            fee=fee,
            stamp_tax=0,
            trade_date=today(),
            trade_time=now(),
            status="filled"
        )
        self.db.save_paper_trade(trade)
        
        return OrderResult(
            order_id=trade.id,
            status="filled",
            filled_quantity=quantity,
            filled_price=fill_price,
            fee=fee,
            message="模拟成交"
        )
    
    def sell(self, code: str, price: float, quantity: int) -> OrderResult:
        """模拟卖出"""
        pos = self.db.get_paper_position(code)
        if not pos or pos.available_quantity < quantity:
            return OrderResult(
                order_id=f"MOCK_{uuid4()}",
                status="rejected",
                filled_quantity=0,
                filled_price=0,
                fee=0,
                message="可卖持仓不足"
            )
        
        # 应用滑点
        fill_price = price * (1 - self.slippage)
        
        # 计算费用
        amount = fill_price * quantity
        fee = max(amount * self.fee_rate, 5.0)
        stamp_tax = amount * self.stamp_tax_rate  # 仅卖出收印花税
        total_cost = fee + stamp_tax
        net_amount = amount - total_cost
        
        # 计算盈亏
        cost_basis = pos.avg_cost_price * quantity
        pnl = net_amount - cost_basis
        
        # 更新账户
        account = self.db.get_paper_account()
        account.cash += net_amount
        self.db.save_paper_account(account)
        
        # 更新持仓
        pos.total_quantity -= quantity
        pos.available_quantity -= quantity
        if pos.total_quantity == 0:
            self.db.delete_paper_position(code)
        else:
            self.db.save_paper_position(pos)
        
        # 记录交易
        trade = PaperTrade(
            code=code,
            direction="sell",
            price=fill_price,
            quantity=quantity,
            amount=amount,
            fee=fee,
            stamp_tax=stamp_tax,
            trade_date=today(),
            trade_time=now(),
            pnl=pnl,
            status="filled"
        )
        self.db.save_paper_trade(trade)
        
        return OrderResult(
            order_id=trade.id,
            status="filled",
            filled_quantity=quantity,
            filled_price=fill_price,
            fee=fee + stamp_tax,
            message="模拟成交"
        )
    
    def get_positions(self) -> List[PositionInfo]:
        """查询虚拟持仓"""
        positions = self.db.get_all_paper_positions()
        return [
            PositionInfo(
                code=p.code,
                quantity=p.total_quantity,
                available_quantity=p.available_quantity,
                avg_cost_price=p.avg_cost_price,
                current_price=p.market_value / p.total_quantity if p.total_quantity > 0 else 0,
                market_value=p.market_value
            )
            for p in positions
        ]
    
    def get_account(self) -> AccountInfo:
        """查询虚拟账户"""
        acc = self.db.get_paper_account()
        positions = self.get_positions()
        market_value = sum(p.market_value for p in positions)
        return AccountInfo(
            cash=acc.cash,
            total_value=acc.cash + market_value,
            market_value=market_value,
            frozen_cash=0  # 模拟盘简化处理
        )
    
    def get_quote(self, code: str) -> Dict:
        """获取实时行情（复用现有数据源）"""
        # 调用现有 fetcher 获取实时价格
        from data_layer.fetcher import get_realtime_quote
        return get_realtime_quote(code)
```

## [S7] 配置设计

```yaml
# 新增配置节
trading:
  mode: paper              # paper=模拟盘, live=实盘
  fee_rate: 0.00015       # 手续费万1.5
  stamp_tax_rate: 0.0005  # 印花税万5（仅卖出）
  min_fee: 5.0           # 最低手续费5元

paper_trading:
  enabled: true
  initial_cash: 1000000   # 初始资金100万
  
  # 定时运行配置（cron格式）
  schedule: "0 15 * * 1-5"  # 工作日15:00运行（收盘后）
  
  # 成交价格设置
  fill_price: close       # 收盘价成交（可选：open/vwap）
```

## [S8] CLI 集成

```bash
# 运行模拟盘（收盘后执行）
python main.py --paper

# 初始化模拟盘（重置持仓和资金）
python main.py --paper --reset

# 查看模拟盘状态
python main.py --paper-status

# 切换实盘（需配置券商API）
python main.py --live
```

## [S9] 定时运行（cron）

```bash
# 编辑 crontab
crontab -e

# 添加定时任务（工作日15:00运行）
0 15 * * 1-5 cd /path/to/project && python main.py --paper >> logs/paper_trading.log 2>&1

# 查看日志
tail -f logs/paper_trading.log
```

## [S9] 新增文件清单

| 文件 | 内容 | 行数预估 |
|------|------|---------|
| `broker_adapter/base.py` | 抽象基类 + 数据类 | 80 |
| `broker_adapter/mock_adapter.py` | MockAdapter 实现 | 150 |
| `trading_core/paper_trading.py` | 收盘后模拟执行逻辑 | 150 |
| `data_layer/market_db.py` | 新增4张表 + 操作方法 | 150 |
| `main.py` | 新增 --paper 参数 | 30 |
| **总计** | | **~560** |

## [S10] 与现有系统的集成点

| 现有模块 | 复用方式 |
|---------|---------|
| `generate_signals()` | 直接调用，生成当日信号 |
| `RiskControlManager` | 传入虚拟持仓和账户状态 |
| `DynamicGridEngine` | 信号生成内部已使用 |
| `data_layer/fetcher.py` | 获取当日行情数据 |
| `data_layer/market_db.py` | 新增模拟盘相关表 |
| `utils/utils.py` | 配置加载、日志 |

## [S11] 风险控制

1. **模拟盘资金隔离** — 虚拟资金与真实资金完全隔离
2. **价格异常保护** — 涨跌停检查，避免废单
3. **持仓上限** — 单票最大持仓比例限制
4. **回撤控制** — 全局回撤超10%暂停买入
5. **T+1 强制遵守** — 今日买入份额标记为冻结，次日释放

## [S12] 测试策略

1. **单元测试** — MockAdapter 买入/卖出/T+1 逻辑
2. **集成测试** — 完整流程：信号生成 → 收盘模拟 → 持仓更新
3. **回归测试** — 对比模拟盘与历史回测结果一致性
4. **性能测试** — 50只股票同时模拟执行速度

---

*Last Updated: 2026-06-16*
*Version: v2.0.0*
