"""
模拟盘适配器 — 用 SQLite 模拟券商接口
实现虚拟持仓、虚拟交易、虚拟账户管理
"""

import logging
import uuid
from datetime import datetime
from typing import List

from broker_adapter.base import BrokerAdapter, OrderResult, PositionInfo, AccountInfo
from data_layer.market_db import (
    get_paper_account, save_paper_account,
    get_paper_position, save_paper_position, delete_paper_position, get_all_paper_positions,
    save_paper_trade
)

logger = logging.getLogger("grid_trading")


class MockAdapter(BrokerAdapter):
    """模拟盘适配器 — 用 SQLite 模拟券商接口"""

    def __init__(self, config: dict):
        self.config = config
        self.fee_rate = config.get('trading', {}).get('fee_rate', 0.00015)
        self.stamp_tax_rate = config.get('trading', {}).get('stamp_tax_rate', 0.0005)
        self.min_fee = config.get('trading', {}).get('min_fee', 5.0)
        self.data_dir = config.get('paths', {}).get('data_dir', './data')

    def connect(self, config: dict) -> bool:
        """初始化账户和持仓"""
        account = get_paper_account(self.data_dir)
        if account is None:
            # 首次运行，初始化
            initial_cash = config.get('paper_trading', {}).get('initial_cash', 1000000)
            save_paper_account(
                cash=initial_cash,
                total_value=initial_cash,
                peak_value=initial_cash,
                max_drawdown=0.0,
                data_dir=self.data_dir
            )
            logger.info(f"模拟盘账户初始化完成，初始资金：{initial_cash:,.2f}")
        return True

    def buy(self, code: str, price: float, quantity: int) -> OrderResult:
        """模拟买入"""
        # 计算费用
        amount = price * quantity
        fee = max(amount * self.fee_rate, self.min_fee)
        total_cost = amount + fee

        # 检查资金
        account = get_paper_account(self.data_dir)
        if account is None or account['cash'] < total_cost:
            return OrderResult(
                order_id=f"MOCK_{uuid.uuid4().hex[:8]}",
                status="rejected",
                filled_quantity=0,
                filled_price=0,
                fee=0,
                message="资金不足"
            )

        # 更新账户
        new_cash = account['cash'] - total_cost
        save_paper_account(
            cash=new_cash,
            total_value=account['total_value'],
            peak_value=account['peak_value'],
            max_drawdown=account['max_drawdown'],
            data_dir=self.data_dir
        )

        # 更新持仓（T+1：今日买入冻结）
        pos = get_paper_position(code, self.data_dir)
        if pos:
            # 更新平均成本
            total_cost_basis = pos['avg_cost_price'] * pos['total_quantity'] + price * quantity
            new_total = pos['total_quantity'] + quantity
            new_frozen = pos['frozen_quantity'] + quantity
            new_avg = total_cost_basis / new_total
            save_paper_position(
                code=code,
                total_quantity=new_total,
                available_quantity=pos['available_quantity'],  # 今日买入，明日可用
                frozen_quantity=new_frozen,
                avg_cost_price=new_avg,
                market_value=price * new_total,
                data_dir=self.data_dir
            )
        else:
            save_paper_position(
                code=code,
                total_quantity=quantity,
                available_quantity=0,  # 今日买入，明日可用
                frozen_quantity=quantity,
                avg_cost_price=price,
                market_value=price * quantity,
                data_dir=self.data_dir
            )

        # 记录交易
        trade_id = save_paper_trade(
            code=code,
            direction="buy",
            price=price,
            quantity=quantity,
            amount=amount,
            fee=fee,
            stamp_tax=0,
            trade_date=datetime.now().strftime('%Y-%m-%d'),
            trade_time=datetime.now().strftime('%H:%M:%S'),
            status="filled",
            data_dir=self.data_dir
        )

        return OrderResult(
            order_id=trade_id,
            status="filled",
            filled_quantity=quantity,
            filled_price=price,
            fee=fee,
            message="模拟成交"
        )

    def sell(self, code: str, price: float, quantity: int) -> OrderResult:
        """模拟卖出"""
        pos = get_paper_position(code, self.data_dir)
        if pos is None or pos['available_quantity'] < quantity:
            return OrderResult(
                order_id=f"MOCK_{uuid.uuid4().hex[:8]}",
                status="rejected",
                filled_quantity=0,
                filled_price=0,
                fee=0,
                message="可卖持仓不足"
            )

        # 计算费用
        amount = price * quantity
        fee = max(amount * self.fee_rate, self.min_fee)
        stamp_tax = amount * self.stamp_tax_rate  # 仅卖出收印花税
        total_cost = fee + stamp_tax
        net_amount = amount - total_cost

        # 计算盈亏
        cost_basis = pos['avg_cost_price'] * quantity
        pnl = net_amount - cost_basis

        # 更新账户
        account = get_paper_account(self.data_dir)
        if account:
            new_cash = account['cash'] + net_amount
            save_paper_account(
                cash=new_cash,
                total_value=account['total_value'],
                peak_value=account['peak_value'],
                max_drawdown=account['max_drawdown'],
                data_dir=self.data_dir
            )

        # 更新持仓
        new_total = pos['total_quantity'] - quantity
        new_available = pos['available_quantity'] - quantity
        if new_total <= 0:
            delete_paper_position(code, self.data_dir)
        else:
            save_paper_position(
                code=code,
                total_quantity=new_total,
                available_quantity=new_available,
                frozen_quantity=pos['frozen_quantity'],
                avg_cost_price=pos['avg_cost_price'],
                market_value=price * new_total,
                data_dir=self.data_dir
            )

        # 记录交易
        trade_id = save_paper_trade(
            code=code,
            direction="sell",
            price=price,
            quantity=quantity,
            amount=amount,
            fee=fee,
            stamp_tax=stamp_tax,
            trade_date=datetime.now().strftime('%Y-%m-%d'),
            trade_time=datetime.now().strftime('%H:%M:%S'),
            pnl=pnl,
            status="filled",
            data_dir=self.data_dir
        )

        return OrderResult(
            order_id=trade_id,
            status="filled",
            filled_quantity=quantity,
            filled_price=price,
            fee=fee + stamp_tax,
            message="模拟成交"
        )

    def get_positions(self) -> List[PositionInfo]:
        """查询虚拟持仓"""
        positions = get_all_paper_positions(self.data_dir)
        return [
            PositionInfo(
                code=p['code'],
                quantity=p['total_quantity'],
                available_quantity=p['available_quantity'],
                avg_cost_price=p['avg_cost_price'],
                current_price=p['market_value'] / p['total_quantity'] if p['total_quantity'] > 0 else 0,
                market_value=p['market_value']
            )
            for p in positions
        ]

    def get_account(self) -> AccountInfo:
        """查询虚拟账户"""
        acc = get_paper_account(self.data_dir)
        positions = self.get_positions()
        market_value = sum(p.market_value for p in positions)
        total_value = (acc['cash'] if acc else 0) + market_value
        return AccountInfo(
            cash=acc['cash'] if acc else 0,
            total_value=total_value,
            market_value=market_value,
            frozen_cash=0
        )

    def disconnect(self) -> None:
        """断开连接（模拟盘无需操作）"""

    def release_t1_positions(self) -> None:
        """释放 T+1 冻结持仓（每日收盘后调用）"""
        positions = get_all_paper_positions(self.data_dir)
        for pos in positions:
            if pos['frozen_quantity'] > 0:
                save_paper_position(
                    code=pos['code'],
                    total_quantity=pos['total_quantity'],
                    available_quantity=pos['available_quantity'] + pos['frozen_quantity'],
                    frozen_quantity=0,
                    avg_cost_price=pos['avg_cost_price'],
                    market_value=pos['market_value'],
                    data_dir=self.data_dir
                )
                logger.debug(f"T+1 释放：{pos['code']} {pos['frozen_quantity']} 股")
