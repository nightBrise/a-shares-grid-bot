"""
risk.py - 增强风控模块

Features:
- T+1 规则追踪
- 单股熔断 (15% 亏损阈值)
- 全局熔断 (10% 回撤阈值)
- 涨跌停跳过
- 滑点模型 (0.1% 基础 + 分层)
- 阶梯费率计算
"""

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("grid_trading")


# ==================== Fee Schedule ====================

# A 股费率表
FEE_SCHEDULE = {
    "commission": {
        "rate": 0.00015,  # 万1.5
        "min": 5.0,  # 最低5元
        "etf_exempt_min": True,  # ETF 免收佣金最低限制
    },
    "stamp_tax": {
        "rate": 0.0005,  # 万5 (仅卖出收取)
        "exempt": ["ETF", "Bond"],
    },
    "transfer": {
        "rate": 0.00002,  # 万0.2 (双向收取)
        "exempt": ["ETF"],
    },
}

# 滑点模型
SLIPPAGE_MODEL = {
    "base_rate": 0.001,  # 0.1% 基础滑点
    "tiered_by_price": True,
    "price_tiers": {
        "high": (100, 0.0015),  # Price > 100: 0.15%
        "medium": (20, 0.001),  # Price 20-100: 0.1%
        "low": (0, 0.002),  # Price < 20: 0.2%
    },
}


# ==================== Data Classes ====================


@dataclass
class RiskMetrics:
    """风控指标容器。"""

    sharpe_ratio: float
    max_drawdown: float
    calmar_ratio: float
    total_trades: int
    win_rate: float
    avg_slippage_cost: float
    total_slippage_cost: float
    force_close_count: int
    limit_up_skip_count: int
    limit_down_skip_count: int


@dataclass
class PositionState:
    """单只股票持仓状态，用于 T+1 追踪。"""

    code: str
    quantity: int  # 总持仓
    available_qty: int  # 可卖出持仓 (T+1 约束)
    avg_cost: float
    last_buy_date: str  # 用于 T+1 追踪


@dataclass
class TradeRecord:
    """交易记录。"""

    code: str
    direction: str  # 'buy' or 'sell'
    price: float
    quantity: int
    amount: float  # 成交金额
    fees: float
    slippage_cost: float
    date: str
    realized_pnl: float = 0.0  # 已实现盈亏 (仅卖出时)


# ==================== Enhanced Risk Control ====================


class EnhancedRiskControl:
    """
    增强风控管理器，支持 T+1 适配。

    Features:
    1. T+1 规则: 追踪买入日期，今日买入不可卖
    2. 单股熔断: 亏损 >= 15% → 暂停买入，仅允许卖出
    3. 全局熔断: 账户回撤 >= 10% → 停止所有买入
    4. 涨跌停跳过: 涨跌停附近不触发买入
    5. 滑点模型: 0.1% + 分层费率
    6. 强制平仓: 50% if price drops 3 grids below lower rail

    Usage:
        risk_mgr = EnhancedRiskControl(config, initial_capital=1000000)

        # 更新持仓
        risk_mgr.update_positions(positions_data)

        # 检查是否允许买入
        if risk_mgr.can_buy(code, quantity, price):
            ...

        # 记录交易
        risk_mgr.record_trade(code, 'buy', price, quantity)
    """

    def __init__(
        self,
        config: dict,
        initial_capital: float = 1000000,
    ):
        """
        初始化增强风控。

        Parameters:
            config: 配置字典
            initial_capital: 初始资金 (元)
        """
        self.config = config
        self.initial_capital = initial_capital

        # 持仓追踪
        self.positions: Dict[str, PositionState] = {}
        self.cash = initial_capital

        # 风控阈值
        risk_cfg = config.get("risk_control", {})
        self.single_stock_loss_threshold = risk_cfg.get(
            "single_stock_loss_threshold", 0.15
        )
        self.max_drawdown_threshold = risk_cfg.get("max_drawdown_threshold", 0.10)
        self.max_position_pct = risk_cfg.get("max_position_per_stock", 0.05)  # 5%
        self.limit_threshold = risk_cfg.get("limit_threshold", 9.8)  # %

        # 峰値追踪
        self.peak_value = initial_capital
        self.current_drawdown = 0.0

        # 交易统计
        self.trade_history: List[TradeRecord] = []
        self.force_close_count = 0
        self.limit_up_skip_count = 0
        self.limit_down_skip_count = 0

        # 熔断状态
        self.single_stock_breakers: Dict[str, bool] = {}  # code -> is breaker
        self.global_breaker_active = False

        logger.info(
            f"EnhancedRiskControl 初始化: capital={initial_capital}, "
            f"single_loss_thresh={self.single_stock_loss_threshold*100}%, "
            f"dd_thresh={self.max_drawdown_threshold*100}%"
        )

    def update_positions(self, positions_data: List[Dict]) -> None:
        """
        从外部数据源更新持仓状态。

        Parameters:
            positions_data: 包含 code, quantity, cost_price 的字典列表
        """
        self.positions.clear()

        for pos in positions_data:
            code = pos["code"]
            self.positions[code] = PositionState(
                code=code,
                quantity=pos["quantity"],
                available_qty=pos.get("available_qty", pos["quantity"]),
                avg_cost=pos["cost_price"],
                last_buy_date=pos.get("last_buy_date", ""),
            )

    def calculate_current_value(self, prices: Dict[str, float]) -> float:
        """
        计算当前总组合价值。

        Parameters:
            prices: code -> current_price 的字典

        Returns:
            总组合价值 (现金 + 持仓)
        """
        position_value = sum(
            state.quantity * prices.get(code, state.avg_cost)
            for code, state in self.positions.items()
        )
        return self.cash + position_value

    def update_peak_and_drawdown(self, current_value: float) -> Tuple[float, float]:
        """
        更新峰値并计算当前回撤。

        Parameters:
            current_value: 当前组合价值

        Returns:
            (peak_value, drawdown_pct)
        """
        if current_value > self.peak_value:
            self.peak_value = current_value

        if self.peak_value > 0:
            self.current_drawdown = (self.peak_value - current_value) / self.peak_value
        else:
            self.current_drawdown = 0

        return self.peak_value, self.current_drawdown

    def can_buy(self, code: str, quantity: int, price: float) -> Tuple[bool, str]:
        """
        检查买入订单是否被风控规则允许。

        Checks:
        1. 全局熔断
        2. 现金充足
        3. 单股持仓限制
        4. 单股熔断

        Parameters:
            code: 股票代码
            quantity: 买入数量
            price: 买入价格

        Returns:
            (allowed, reason)
        """
        # Check 1: 全局熔断
        if self.global_breaker_active:
            return False, f"全局熔断触发: 回撤 {self.current_drawdown*100:.2f}% >= {self.max_drawdown_threshold*100}%"

        # Check 2: 现金
        cost = quantity * price
        if cost > self.cash * 1.01:  # 1% buffer
            return False, f"现金不足: 需要 {cost:.0f}, 可用 {self.cash:.0f}"

        # Check 3: 单股持仓限制
        current_position = self.positions.get(code)
        current_position_value = current_position.quantity * price if current_position else 0
        total_value = self.cash + sum(
            s.quantity * price for s in self.positions.values()
        )

        new_position_value = current_position_value + cost
        if total_value > 0:
            new_position_pct = new_position_value / total_value
            if new_position_pct > self.max_position_pct:
                return (
                    False,
                    f"将超过 {self.max_position_pct*100:.0f}% 持仓限制 (当前: {new_position_pct*100:.1f}%)",
                )

        # Check 4: 单股熔断
        if current_position:
            unrealized_pnl_pct = (price - current_position.avg_cost) / current_position.avg_cost
            if unrealized_pnl_pct <= -self.single_stock_loss_threshold:
                return (
                    False,
                    f"单股熔断: 亏损 {unrealized_pnl_pct*100:.1f}% >= {self.single_stock_loss_threshold*100:.1f}%",
                )

        return True, "OK"

    def can_sell(self, code: str, quantity: int) -> Tuple[bool, str]:
        """
        检查卖出订单是否被 T+1 规则允许。

        Parameters:
            code: 股票代码
            quantity: 卖出数量

        Returns:
            (allowed, reason)
        """
        position = self.positions.get(code)

        if not position:
            return False, "无持仓"

        if quantity > position.available_qty:
            return (
                False,
                f"T+1 约束: 可卖={position.available_qty}, 申请={quantity}",
            )

        return True, "OK"

    def check_limit_status(
        self,
        current_price: float,
        prev_close: float,
    ) -> Tuple[bool, bool]:
        """
        检查涨跌停状态。

        Parameters:
            current_price: 当前价格
            prev_close: 昨日收盘价

        Returns:
            (is_limit_up, is_limit_down)
        """
        if prev_close <= 0:
            return False, False

        change_pct = abs(current_price - prev_close) / prev_close * 100

        is_limit_up = change_pct >= self.limit_threshold and current_price > prev_close
        is_limit_down = change_pct >= self.limit_threshold and current_price < prev_close

        return is_limit_up, is_limit_down

    def record_trade(
        self,
        code: str,
        direction: str,
        price: float,
        quantity: int,
        trade_date: Optional[str] = None,
        cost_price: Optional[float] = None,
    ) -> None:
        """
        记录交易并更新持仓状态。

        Parameters:
            code: 股票代码
            direction: 'buy' or 'sell'
            price: 成交价格
            quantity: 成交数量
            trade_date: 交易日期字符串 (YYYY-MM-DD)
            cost_price: 成本价 (仅卖出时用于计算已实现盈亏)
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        is_etf = "ETF" in code.upper()
        trade_amount = price * quantity

        # 计算费用
        fees = self.calculate_fees(trade_amount, direction, code)
        slippage_cost = self.calculate_slippage(trade_amount, price, direction)

        # 已实现盈亏 (仅卖出时有意义)
        realized_pnl = 0.0

        if direction == "buy":
            # 更新持仓
            position = self.positions.get(code)
            if position:
                # 更新平均成本
                total_cost = position.quantity * position.avg_cost + trade_amount
                position.quantity += quantity
                position.avg_cost = total_cost / position.quantity
                # 今日买入不可卖
                position.available_qty = position.quantity - quantity
                position.last_buy_date = trade_date
            else:
                # 新持仓
                self.positions[code] = PositionState(
                    code=code,
                    quantity=quantity,
                    available_qty=0,  # T+1
                    avg_cost=price,
                    last_buy_date=trade_date,
                )

            self.cash -= trade_amount + fees + slippage_cost

        else:  # sell
            position = self.positions.get(code)
            realized_pnl = 0.0

            if position:
                position.quantity -= quantity
                position.available_qty -= quantity

                # 计算已实现盈亏
                if cost_price is not None:
                    realized_pnl = (price - cost_price) * quantity - fees - slippage_cost

                if position.quantity == 0:
                    del self.positions[code]

            self.cash += trade_amount - fees - slippage_cost

        # 记录交易
        self.trade_history.append(
            TradeRecord(
                code=code,
                direction=direction,
                price=price,
                quantity=quantity,
                amount=trade_amount,
                fees=fees,
                slippage_cost=slippage_cost,
                date=trade_date,
                realized_pnl=realized_pnl,
            )
        )

    def calculate_fees(
        self,
        trade_amount: float,
        direction: str,
        code: str,
    ) -> float:
        """
        计算分级交易费用。

        Parameters:
            trade_amount: 成交金额 (元)
            direction: 'buy' or 'sell'
            code: 股票代码 (用于 ETF 检查)

        Returns:
            总费用 (元)
        """
        is_etf = "ETF" in code.upper()

        # 佣金
        if is_etf and FEE_SCHEDULE["commission"]["etf_exempt_min"]:
            commission = 0
        else:
            commission = max(
                trade_amount * FEE_SCHEDULE["commission"]["rate"],
                FEE_SCHEDULE["commission"]["min"],
            )

        # 印花税 (仅卖出，ETF 免)
        if direction == "sell" and not is_etf:
            stamp_tax = trade_amount * FEE_SCHEDULE["stamp_tax"]["rate"]
        else:
            stamp_tax = 0

        # 过户费 (ETF 免)
        if not is_etf:
            transfer = trade_amount * FEE_SCHEDULE["transfer"]["rate"]
        else:
            transfer = 0

        return commission + stamp_tax + transfer

    def calculate_slippage(
        self,
        trade_amount: float,
        price: float,
        direction: str,
    ) -> float:
        """
        基于价格分层计算机滑点成本。

        Parameters:
            trade_amount: 成交金额
            price: 成交价格
            direction: 'buy' or 'sell'

        Returns:
            滑点成本 (元)
        """
        # 确定价格分层滑点率
        if price > 100:
            slippage_rate = SLIPPAGE_MODEL["price_tiers"]["high"][1]
        elif price >= 20:
            slippage_rate = SLIPPAGE_MODEL["price_tiers"]["medium"][1]
        else:
            slippage_rate = SLIPPAGE_MODEL["price_tiers"]["low"][1]

        # 总滑点率 = 基础滑点 + 分层滑点
        total_rate = SLIPPAGE_MODEL["base_rate"] + slippage_rate

        return trade_amount * total_rate

    def advance_t1(self, current_date: str) -> None:
        """
        推进 T+1 时钟：将可卖持仓更新为总持仓。

        在每个交易日开始时调用。

        Parameters:
            current_date: 当前日期字符串 (YYYY-MM-DD)
        """
        for code, position in self.positions.items():
            if position.last_buy_date and position.last_buy_date < current_date:
                # 至少持有一天，今日买入的可卖了
                position.available_qty = position.quantity

        logger.debug(f"T+1 更新完成: {current_date}")

    def trigger_single_stock_breaker(self, code: str, reason: str) -> None:
        """
        触发单股熔断。

        Parameters:
            code: 股票代码
            reason: 触发原因
        """
        self.single_stock_breakers[code] = True
        logger.warning(f"单股熔断触发: {code} - {reason}")

    def reset_single_stock_breaker(self, code: str) -> None:
        """
        重置单股熔断。

        Parameters:
            code: 股票代码
        """
        self.single_stock_breakers[code] = False
        logger.info(f"单股熔断重置: {code}")

    def trigger_global_breaker(self, reason: str) -> None:
        """
        触发全局熔断。

        Parameters:
            reason: 触发原因
        """
        self.global_breaker_active = True
        logger.warning(f"全局熔断触发: {reason}")

    def reset_global_breaker(self) -> None:
        """重置全局熔断。"""
        self.global_breaker_active = False
        logger.info("全局熔断重置")

    def get_risk_metrics(self) -> RiskMetrics:
        """
        从交易历史计算风控指标。

        Returns:
            RiskMetrics 对象
        """
        if not self.trade_history:
            return RiskMetrics(
                sharpe_ratio=0,
                max_drawdown=self.current_drawdown,
                calmar_ratio=0,
                total_trades=0,
                win_rate=0,
                avg_slippage_cost=0,
                total_slippage_cost=0,
                force_close_count=self.force_close_count,
                limit_up_skip_count=self.limit_up_skip_count,
                limit_down_skip_count=self.limit_down_skip_count,
            )

        total_trades = len(self.trade_history)
        sells = [t for t in self.trade_history if t.direction == "sell"]

        # 胜率
        if sells:
            wins = sum(1 for t in sells if t.realized_pnl > 0)
            win_rate = wins / len(sells)
        else:
            win_rate = 0

        # 滑点
        total_slippage = sum(t.slippage_cost for t in self.trade_history)
        avg_slippage = total_slippage / total_trades if total_trades > 0 else 0

        return RiskMetrics(
            sharpe_ratio=0,  # 需要收益率序列
            max_drawdown=self.current_drawdown,
            calmar_ratio=0,  # 需要收益率序列
            total_trades=total_trades,
            win_rate=win_rate,
            avg_slippage_cost=avg_slippage,
            total_slippage_cost=total_slippage,
            force_close_count=self.force_close_count,
            limit_up_skip_count=self.limit_up_skip_count,
            limit_down_skip_count=self.limit_down_skip_count,
        )

    def get_position_summary(self) -> pd.DataFrame:
        """
        获取持仓汇总表。

        Returns:
            包含持仓信息的 DataFrame
        """
        if not self.positions:
            return pd.DataFrame()

        records = []
        for code, pos in self.positions.items():
            records.append({
                "code": code,
                "quantity": pos.quantity,
                "available": pos.available_qty,
                "avg_cost": pos.avg_cost,
                "last_buy": pos.last_buy_date,
            })

        return pd.DataFrame(records)

    def get_trade_summary(self) -> Dict[str, float]:
        """
        获取交易汇总统计。

        Returns:
            包含交易统计的字典
        """
        if not self.trade_history:
            return {
                "total_trades": 0,
                "total_buy_amount": 0,
                "total_sell_amount": 0,
                "total_fees": 0,
                "total_slippage": 0,
                "realized_pnl": 0,
            }

        buys = [t for t in self.trade_history if t.direction == "buy"]
        sells = [t for t in self.trade_history if t.direction == "sell"]

        return {
            "total_trades": len(self.trade_history),
            "total_buy_amount": sum(t.amount for t in buys),
            "total_sell_amount": sum(t.amount for t in sells),
            "total_fees": sum(t.fees for t in self.trade_history),
            "total_slippage": sum(t.slippage_cost for t in self.trade_history),
            "realized_pnl": sum(t.realized_pnl for t in self.trade_history),
        }
