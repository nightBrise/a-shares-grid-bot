"""
grid_engine.py - 动态网格参数引擎

根据波动率区间自适应计算网格间距，生成买卖信号。

核心功能：
1. 波动率自适应间距: k * ATR(20)，k 根据波动率区间变化
2. 动态上下轨: P_ref ± 2*σ_60d
3. T+1 适配: 最小间距 >= 1.5 * 日均振幅
4. 强制平仓触发: 价格跌破下轨 3 格 → 平仓 50%
"""

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("grid_trading")


# ==================== Volatility Regime Configuration ====================

VOLATILITY_REGIME_K = {
    "low": 2.5,  # 低波动: k=2.5，间距宽松
    "medium": 2.0,  # 中波动: k=2.0
    "high": 1.5,  # 高波动: k=1.5，间距紧密
}

VOLATILITY_THRESHOLDS = {
    "low": 0.20,  # 年化波动率 < 20% = 低波动区间
    "high": 0.35,  # 年化波动率 > 35% = 高波动区间
}


# ==================== Data Classes ====================


@dataclass
class GridParameters:
    """网格参数容器。"""

    spacing_pct: float  # 网格间距百分比
    k_coef: float  # 使用的 ATR 系数
    upper_rail_pct: float  # 上轨偏离百分比
    lower_rail_pct: float  # 下轨偏离百分比
    n_grids: int  # 网格层数
    atr_value: float  # 当前 ATR 值
    regime: str  # 波动率区间: 'low', 'medium', 'high'


@dataclass
class GridSignal:
    """单个网格交易信号容器。"""

    code: str
    direction: str  # 'buy' or 'sell'
    price: float
    quantity: int
    grid_level: int  # 网格层级 (1 = 距中心最近)
    reason: str  # 人类可读的原因
    atr_adjusted: bool  # 是否应用了 ATR 调整
    signal_type: str  # 'normal', 't1_adaptation', 'force_close'


# ==================== Dynamic Grid Engine ====================


class DynamicGridEngine:
    """
    动态网格参数计算和信号生成引擎。

    Features:
    1. 波动率自适应间距: k * ATR(20)，k 因波动率区间而异
    2. 动态上下轨: P_ref ± 2*σ_60d
    3. T+1 适配: 间距 >= 1.5 * 日均振幅
    4. 强制平仓: 价格跌破下轨 3 格时平仓 50%

    Grid Spacing Formula:
        ΔP = k * ATR(20)
        其中 k ∈ {1.5, 2.0, 2.5} 根据波动率区间

    Rail Formula:
        Upper = P_ref * (1 + 2 * σ_60d / P_ref)
        Lower = P_ref * (1 - 2 * σ_60d / P_ref)
    """

    def __init__(self, config: Optional[dict] = None):
        """
        初始化网格引擎。

        Parameters:
            config: 配置字典，包含 grid 和 dynamic_grid 参数
        """
        self.config = config or {}

        grid_cfg = self.config.get("grid", {})
        dyn_grid_cfg = self.config.get("dynamic_grid", {})

        # 基础参数
        self.base_spacing = grid_cfg.get("base_spacing", 0.02)  # 小数格式 (2.0%)
        self.atr_period = grid_cfg.get("atr_period", 20)
        self.base_atr_coef = grid_cfg.get("atr_coef", 1.5)
        self.max_grids = grid_cfg.get("max_grids", 5)
        # 间距硬约束（防止参数漂移）
        self.min_spacing = grid_cfg.get("min_spacing", 0.015)  # 1.5%
        self.max_spacing = grid_cfg.get("max_spacing", 0.035)  # 3.5%

        # 波动率区间配置
        regime_k = dyn_grid_cfg.get("volatility_regime_k", VOLATILITY_REGIME_K)
        self.vol_regime_k = {
            "low": regime_k.get("low", 2.5),
            "medium": regime_k.get("medium", 2.0),
            "high": regime_k.get("high", 1.5),
        }

        vol_thresh = dyn_grid_cfg.get("volatility_thresholds", VOLATILITY_THRESHOLDS)
        self.vol_low_threshold = vol_thresh.get("low", 0.20)
        self.vol_high_threshold = vol_thresh.get("high", 0.35)

        # ATR 通道系数 (用于上下轨计算)
        self.rail_z_coef = dyn_grid_cfg.get("rail_z_coef", 2.0)

        # T+1 适配
        self.t1_min_spacing_coef = dyn_grid_cfg.get("t1_min_spacing_coef", 1.5)
        self.daily_vol_period = 20

        # 强制平仓参数
        force_cfg = dyn_grid_cfg.get("force_close", {})
        self.force_close_grids_below = force_cfg.get("grids_below", 3)
        self.force_close_pct = force_cfg.get("close_pct", 0.50)

        # 持仓限制
        self.max_position_pct = dyn_grid_cfg.get("position_limit_pct", 0.05)

        # 市场状态门控参数（可动态传入或从配置读取默认）
        self.regime_params = None  # 运行时由外部传入

    def set_regime_params(self, regime_params: dict) -> None:
        """
        设置市场状态门控参数。

        Parameters:
            regime_params: 从 RegimeFilter.check() 返回的 params 字典
            {
                "max_position_per_stock": 0.30,
                "initial_position": 0.45,
                "grid_spacing_multiplier": 1.0,
                "max_grids": 5
            }
        """
        self.regime_params = regime_params
        logger.debug(
            f"市场参数已更新: max_pos={regime_params.get('max_position_per_stock', 0.3):.0%}, "
            f"spacing_mult={regime_params.get('grid_spacing_multiplier', 1.0):.1f}x, "
            f"max_grids={regime_params.get('max_grids', 5)}"
        )

    def get_effective_max_position(self) -> float:
        """获取有效的单股最大仓位（考虑门控）"""
        if self.regime_params:
            return self.regime_params.get("max_position_per_stock", self.max_position_pct)
        return self.max_position_pct

    def get_effective_max_grids(self) -> int:
        """获取有效的最大网格层数（考虑门控）"""
        if self.regime_params:
            return self.regime_params.get("max_grids", self.max_grids)
        return self.max_grids

    def get_effective_spacing_multiplier(self) -> float:
        """获取有效的网格间距乘数（考虑门控）"""
        if self.regime_params:
            return self.regime_params.get("grid_spacing_multiplier", 1.0)
        return 1.0

        logger.info(
            f"DynamicGridEngine 初始化: base_spacing={self.base_spacing:.3f}, "
            f"max_grids={self.max_grids}, "
            f"spacing_clip=[{self.min_spacing:.3f}, {self.max_spacing:.3f}]"
        )

    def determine_volatility_regime(
        self, volatility_60d: float
    ) -> Tuple[str, float]:
        """
        确定波动率区间并选择 k 系数。

        Parameters:
            volatility_60d: 年化波动率 (例如 0.25 表示 25%)

        Returns:
            (regime_name, k_coefficient)
        """
        if volatility_60d < self.vol_low_threshold:
            return "low", self.vol_regime_k["low"]
        elif volatility_60d > self.vol_high_threshold:
            return "high", self.vol_regime_k["high"]
        else:
            return "medium", self.vol_regime_k["medium"]

    def calculate_t1_min_spacing(self, daily_volatility: float) -> float:
        """
        计算 T+1 规则导致的最小网格间距。

        Rule: 网格间距 >= 1.5 * 日均振幅

        Parameters:
            daily_volatility: 日波动率 (例如 0.02 表示 2%)

        Returns:
            最小间距百分比
        """
        return self.t1_min_spacing_coef * daily_volatility * 100

    def calculate_grid_parameters(
        self,
        ref_price: float,
        atr_20: float,
        volatility_60d: float,
        daily_volatility: Optional[float] = None,
    ) -> GridParameters:
        """
        为股票计算动态网格参数。

        Parameters:
            ref_price: 参考价格 (通常为前一日收盘价)
            atr_20: 20日 ATR 值
            volatility_60d: 60日年化波动率
            daily_volatility: 可选的日波动率用于 T+1 计算

        Returns:
            GridParameters 对象，包含所有网格设置
        """
        # Step 1: 确定波动率区间
        regime, k_coef = self.determine_volatility_regime(volatility_60d)

        # Step 2: 计算基于 ATR 的基础间距
        # ATR 比率 = ATR / price，表示价格波动相对于价格的比例
        atr_ratio = atr_20 / ref_price if ref_price > 0 else 0.02

        # 间距 = 基础间距 * (ATR比率 / 0.02) * k系数
        # 0.02 是参考 ATR/价格 比率 (~2%)
        atr_adjusted_spacing = self.base_spacing * (atr_ratio / 0.02) * k_coef

        # Step 3: 应用 T+1 约束
        if daily_volatility is not None:
            t1_min_spacing = self.calculate_t1_min_spacing(daily_volatility)
            atr_adjusted_spacing = max(atr_adjusted_spacing, t1_min_spacing)

        # Step 4: 应用间距钳位（防止参数漂移）
        spacing_pct = np.clip(atr_adjusted_spacing, self.min_spacing, self.max_spacing)

        # Step 5: 使用 ATR 通道计算上下轨（修正量纲问题）
        # 上轨 = P_ref + z × ATR，下轨 = P_ref - z × ATR
        upper_rail_price = ref_price + self.rail_z_coef * atr_20
        lower_rail_price = ref_price - self.rail_z_coef * atr_20
        upper_rail_pct = self.rail_z_coef * atr_20 / ref_price  # 偏离比例
        lower_rail_pct = self.rail_z_coef * atr_20 / ref_price  # 偏离比例

        # Step 6: 确定在轨道内能容纳的网格层数
        total_range_pct = 2 * upper_rail_pct  # 上轨到下轨的总范围
        n_grids = max(3, int(total_range_pct / spacing_pct))
        n_grids = min(n_grids, self.max_grids)

        return GridParameters(
            spacing_pct=spacing_pct,
            k_coef=k_coef,
            upper_rail_pct=upper_rail_pct,
            lower_rail_pct=lower_rail_pct,
            n_grids=n_grids,
            atr_value=atr_20,
            regime=regime,
        )

    def generate_grid_prices(
        self,
        ref_price: float,
        params: GridParameters,
        direction: str = "both",
        spacing_multiplier: float = 1.0,
    ) -> Tuple[List[float], List[float]]:
        """
        生成网格买入和卖出价格。

        Parameters:
            ref_price: 参考价格 (网格中心)
            params: GridParameters 对象
            direction: 'both', 'buy', 或 'sell'
            spacing_multiplier: 间距乘数（市场状态门控）

        Returns:
            (buy_prices, sell_prices) 价格列表
        """
        buy_prices = []
        sell_prices = []

        # 应用间距乘数
        effective_spacing = params.spacing_pct * spacing_multiplier

        for i in range(1, params.n_grids + 1):
            # 买入价格: 低于参考价
            buy_price = ref_price * (1 - effective_spacing / 100 * i)
            # 卖出价格: 高于参考价
            sell_price = ref_price * (1 + effective_spacing / 100 * i)

            if direction in ["both", "buy"]:
                buy_prices.append(round(buy_price, 2))
            if direction in ["both", "sell"]:
                sell_prices.append(round(sell_price, 2))

        return buy_prices, sell_prices

    def check_force_close(
        self,
        current_price: float,
        ref_price: float,
        params: GridParameters,
        current_position: int,
    ) -> Optional[Tuple[str, int]]:
        """
        检查是否触发强制平仓。

        Trigger: 价格跌破下轨 3 格

        Returns:
            Tuple of (action, quantity_to_close) 或 None
        """
        # 计算下轨价格
        lower_rail_price = ref_price * (1 - params.lower_rail_pct)

        # 计算触发价格 (下轨以下 N 格)
        trigger_distance = params.spacing_pct / 100 * self.force_close_grids_below
        trigger_price = lower_rail_price * (1 - trigger_distance)

        if current_price <= trigger_price and current_position > 0:
            close_qty = int(current_position * self.force_close_pct)
            close_qty = (close_qty // 100) * 100  # 整手

            if close_qty >= 100:
                return ("force_close_sell", close_qty)

        return None

    def check_limit_status(
        self,
        current_price: float,
        prev_close: float,
        threshold: float = 9.8,
    ) -> Tuple[bool, bool]:
        """
        检查是否涨跌停。

        Parameters:
            current_price: 当前价格
            prev_close: 昨日收盘价
            threshold: 涨跌停阈值 (默认 9.8%)

        Returns:
            (is_limit_up, is_limit_down)
        """
        if prev_close <= 0:
            return False, False

        change_pct = abs(current_price - prev_close) / prev_close * 100

        is_limit_up = change_pct >= threshold and current_price > prev_close
        is_limit_down = change_pct >= threshold and current_price < prev_close

        return is_limit_up, is_limit_down

    def generate_signals(
        self,
        code: str,
        ref_price: float,
        atr_20: float,
        volatility_60d: float,
        available_position: int = 0,
        current_position: int = 0,
        cash: float = 0.0,
        grid_amount: float = 10000.0,
        prev_close: Optional[float] = None,
        can_buy: bool = True,
        can_open_new: bool = True,
    ) -> List[GridSignal]:
        """
        为股票生成完整的网格信号集。

        Parameters:
            code: 股票代码
            ref_price: 参考价格 (T-1 收盘价)
            atr_20: 20日 ATR
            volatility_60d: 60日年化波动率
            available_position: 可用来卖出的持仓 (T+1 约束)
            current_position: 当前总持仓
            cash: 可用于买入的现金
            grid_amount: 每格买入金额 (元)
            prev_close: 昨日收盘价，用于涨跌停检查

        Returns:
            GridSignal 对象列表
        """
        signals = []

        # 计算日波动率 (年化转日)
        daily_vol = volatility_60d / np.sqrt(252) if volatility_60d > 0 else 0.02
        params = self.calculate_grid_parameters(ref_price, atr_20, volatility_60d, daily_vol)

        # 涨跌停检查
        if prev_close is not None:
            is_limit_up, is_limit_down = self.check_limit_status(ref_price, prev_close)
            if is_limit_up or is_limit_down:
                logger.info(
                    f"{code}: 涨跌停状态，跳过信号生成 "
                    f"(limit_up={is_limit_up}, limit_down={is_limit_down})"
                )
                return signals

        # 应用网格间距乘数（市场状态门控）
        spacing_mult = self.get_effective_spacing_multiplier()

        # 生成网格价格
        buy_prices, sell_prices = self.generate_grid_prices(ref_price, params, spacing_multiplier=spacing_mult)

        # 检查强制平仓触发
        force_action = self.check_force_close(
            ref_price, ref_price, params, current_position
        )

        if force_action:
            action, qty = force_action
            signals.append(
                GridSignal(
                    code=code,
                    direction="sell",
                    price=ref_price,
                    quantity=qty,
                    grid_level=0,
                    reason=f"FORCE_CLOSE: 价格跌破下轨 {self.force_close_grids_below} 格",
                    atr_adjusted=True,
                    signal_type="force_close",
                )
            )

        # 获取动态仓位限制
        effective_max_position = self.get_effective_max_position()
        max_position_value = cash * effective_max_position
        position_value = current_position * ref_price

        # 检查能否买入/开新仓
        if not can_buy:
            logger.debug(f"{code}: 市场状态禁止买入信号")

        for i, buy_price in enumerate(buy_prices):
            # 检查是否应该买入
            if position_value >= max_position_value:
                break

            # 检查能否开新仓（仓位已满时不允许）
            if not can_open_new and position_value <= 0:
                break

            # 检查价格是否合理 (不能跌为负)
            if buy_price <= 0:
                continue

            buy_qty = int(grid_amount / buy_price)
            buy_qty = (buy_qty // 100) * 100  # 整手

            if buy_qty > 0 and buy_qty * buy_price <= cash:
                signals.append(
                    GridSignal(
                        code=code,
                        direction="buy",
                        price=buy_price,
                        quantity=buy_qty,
                        grid_level=i + 1,
                        reason=f"Grid L{i+1}: spacing={params.spacing_pct:.2f}%, k={params.k_coef}, regime={params.regime}",
                        atr_adjusted=True,
                        signal_type="normal",
                    )
                )

                position_value += buy_qty * buy_price

        # 生成卖出信号 (遵守 T+1)
        for i, sell_price in enumerate(sell_prices):
            if available_position <= 0:
                break

            if sell_price <= 0:
                continue

            sell_qty = min(available_position, int(grid_amount / sell_price))
            sell_qty = (sell_qty // 100) * 100  # 整手

            if sell_qty >= 100:
                signals.append(
                    GridSignal(
                        code=code,
                        direction="sell",
                        price=sell_price,
                        quantity=sell_qty,
                        grid_level=i + 1,
                        reason=f"Grid L{i+1}: spacing={params.spacing_pct:.2f}% (T+1 constrained)",
                        atr_adjusted=True,
                        signal_type="t1_adaptation",
                    )
                )

                available_position -= sell_qty

        return signals

    def validate_position_limit(
        self,
        stock_position_value: float,
        total_capital: float,
    ) -> bool:
        """
        检查持仓是否超过 5% 限制。

        Parameters:
            stock_position_value: 当前持仓市值
            total_capital: 总资本

        Returns:
            True if within limit
        """
        if total_capital <= 0:
            return True
        return (stock_position_value / total_capital) <= self.max_position_pct

    def calculate_grid_profit_potential(
        self,
        ref_price: float,
        params: GridParameters,
        n_complete_cycles: int = 1,
    ) -> Dict[str, float]:
        """
        计算网格策略的潜在利润。

        假设价格从下轨运动到上轨，完成完整网格循环。

        Parameters:
            ref_price: 参考价格
            params: 网格参数
            n_complete_cycles: 完整循环次数

        Returns:
            包含利润指标的字典
        """
        buy_prices, sell_prices = self.generate_grid_prices(ref_price, params)

        if not buy_prices or not sell_prices:
            return {
                "total_profit_pct": 0.0,
                "profit_per_grid": 0.0,
                "n_grids": 0,
            }

        # 每格理论利润 = (卖出价 - 买入价) / 买入价
        profits = []
        for buy_p, sell_p in zip(buy_prices[: len(sell_prices)], sell_prices):
            profit_pct = (sell_p - buy_p) / buy_p * 100
            profits.append(profit_pct)

        total_profit_pct = sum(profits) * n_complete_cycles
        profit_per_grid = np.mean(profits) if profits else 0.0

        return {
            "total_profit_pct": total_profit_pct,
            "profit_per_grid": profit_per_grid,
            "n_grids": params.n_grids,
            "spacing_pct": params.spacing_pct,
            "regime": params.regime,
        }


def create_grid_report(
    code: str,
    ref_price: float,
    params: GridParameters,
    signals: List[GridSignal],
) -> str:
    """
    创建网格策略报告。

    Parameters:
        code: 股票代码
        ref_price: 参考价格
        params: 网格参数
        signals: 信号列表

    Returns:
        格式化的报告字符串
    """
    lines = []
    lines.append("=" * 60)
    lines.append(f"网格策略报告 - {code}")
    lines.append("=" * 60)
    lines.append(f"参考价格: {ref_price:.2f}")
    lines.append(f"波动率区间: {params.regime}")
    lines.append(f"网格间距: {params.spacing_pct:.2f}% (k={params.k_coef})")
    lines.append(f"上轨偏离: {params.upper_rail_pct*100:.2f}%")
    lines.append(f"下轨偏离: {params.lower_rail_pct*100:.2f}%")
    lines.append(f"网格层数: {params.n_grids}")
    lines.append(f"ATR(20): {params.atr_value:.2f}")
    lines.append("")
    lines.append("信号列表:")

    for sig in signals:
        lines.append(
            f"  [{sig.direction.upper():4s}] L{sig.grid_level:2d} @ {sig.price:.2f} "
            f"x {sig.quantity} ({sig.signal_type})"
        )

    lines.append("=" * 60)

    return "\n".join(lines)
