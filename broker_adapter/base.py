"""
券商交易接口抽象基类
提供统一的买入/卖出/查询接口，支持多种券商API和模拟盘
"""

from abc import ABC, abstractmethod
from typing import List
from dataclasses import dataclass


@dataclass
class OrderResult:
    """订单结果"""
    order_id: str
    status: str           # submitted/filled/partial/cancelled/rejected
    filled_quantity: int
    filled_price: float
    fee: float
    message: str


@dataclass
class PositionInfo:
    """持仓信息"""
    code: str
    quantity: int
    available_quantity: int
    avg_cost_price: float
    current_price: float
    market_value: float


@dataclass
class AccountInfo:
    """账户信息"""
    cash: float
    total_value: float
    market_value: float
    frozen_cash: float


class BrokerAdapter(ABC):
    """券商交易接口抽象基类"""

    @abstractmethod
    def connect(self, config: dict) -> bool:
        """连接券商系统"""

    @abstractmethod
    def buy(self, code: str, price: float, quantity: int) -> OrderResult:
        """买入下单"""

    @abstractmethod
    def sell(self, code: str, price: float, quantity: int) -> OrderResult:
        """卖出下单"""

    @abstractmethod
    def get_positions(self) -> List[PositionInfo]:
        """查询持仓"""

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """查询账户资金"""

    @abstractmethod
    def disconnect(self) -> None:
        """断开连接"""
