"""
regime_filter.py - 市场状态门控模块

根据宽基指数的ADX和波动率分位数，判断市场状态并返回参数调整系数。

三级响应机制：
- 正常区：ADX < 25 且 波动率分位 30%~70%
- 预警区：ADX 25~35 或 波动率偏离边界
- 熔断区（软）：ADX > 35 或 波动率分位 >85% 或 <15%
- 熔断区（硬底线）：极端尾部风险事件

软收缩原则：
- 通过降低仓位上限、扩大网格间距、减少网格层数来收缩风险敞口
- 保持策略连续运行，避免硬切断导致的仓位错配
"""

import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from trading_core.defaults import get_defaults

import numpy as np
import pandas as pd


logger = logging.getLogger("grid_trading")


class RegimeState(Enum):
    """市场状态枚举"""
    NORMAL = "normal"              # 正常区
    WARNING = "warning"            # 预警区
    SOFT_CIRCUIT_BREAK = "soft_circuit_break"  # 熔断区（软）
    HARD_CIRCUIT_BREAK = "hard_circuit_break"  # 熔断区（硬底线）


@dataclass
class RegimeParams:
    """市场状态对应的参数映射"""
    max_position_per_stock: float   # 单股最大仓位
    initial_position: float         # 初始仓位
    grid_spacing_multiplier: float  # 网格间距乘数
    max_grids: int                  # 最大网格层数


# 默认参数映射
DEFAULT_REGIME_PARAMS = {
    RegimeState.NORMAL: RegimeParams(
        max_position_per_stock=0.30,
        initial_position=0.45,
        grid_spacing_multiplier=1.0,
        max_grids=5
    ),
    RegimeState.WARNING: RegimeParams(
        max_position_per_stock=0.20,
        initial_position=0.35,
        grid_spacing_multiplier=1.2,
        max_grids=4
    ),
    RegimeState.SOFT_CIRCUIT_BREAK: RegimeParams(
        max_position_per_stock=0.10,
        initial_position=0.25,
        grid_spacing_multiplier=1.5,
        max_grids=3
    ),
    RegimeState.HARD_CIRCUIT_BREAK: RegimeParams(
        max_position_per_stock=0.0,
        initial_position=0.0,
        grid_spacing_multiplier=0.0,
        max_grids=0
    ),
}


class RegimeFilter:
    """
    市场状态过滤器 - 基于宽基指数判断市场状态，返回参数调整系数

    核心逻辑：
    1. 使用宽基指数（沪深300）的60日已实现波动率和ADX
    2. 计算波动率在过去252个交易日的滚动分位数
    3. 应用3日移动平均平滑 + 连续2日确认机制防止抖动
    4. 三级响应：正常/预警/熔断（软/硬）
    """

    def __init__(self, config: Optional[dict] = None):
        """
        初始化市场状态过滤器。

        Parameters:
            config: 配置字典，包含 regime_filter 参数
        """
        self.config = config or {}

        defaults = get_defaults()
        rf_cfg = {**defaults.get("regime_filter", {}), **self.config.get("regime_filter", {})}

        # 基准指数
        self.benchmark_index = rf_cfg.get("benchmark_index", "000300.SH")

        # ADX阈值
        self.adx_normal_max = rf_cfg.get("adx_normal_max", 25)
        self.adx_warning_max = rf_cfg.get("adx_warning_max", 35)

        # 波动率分位阈值
        self.vol_normal_low = rf_cfg.get("vol_normal_low", 0.30)
        self.vol_normal_high = rf_cfg.get("vol_normal_high", 0.70)
        self.vol_extreme_low = rf_cfg.get("vol_extreme_low", 0.15)
        self.vol_extreme_high = rf_cfg.get("vol_extreme_high", 0.85)

        # 状态平滑
        self.smoothing_days = rf_cfg.get("smoothing_days", 3)
        self.confirm_days = rf_cfg.get("confirm_days", 2)

        # 硬底线触发条件
        hard_cfg = rf_cfg.get("hard_stop", {"index_drop_threshold": 0.05, "limit_down_count": 200, "volume_shrink_percentile": 0.10})
        self.hard_stop_index_drop = hard_cfg.get("index_drop_threshold", 0.05)
        self.hard_stop_limit_down_count = hard_cfg.get("limit_down_count", 200)
        self.hard_stop_volume_shrink_pct = hard_cfg.get("volume_shrink_percentile", 0.10)

        # 状态参数映射（可覆盖默认）
        self.regime_params = DEFAULT_REGIME_PARAMS.copy()

        # 平滑后的历史状态
        self._adx_history: list = []
        self._vol_pct_history: list = []
        self._confirmed_state = RegimeState.NORMAL
        self._consecutive_days = 0

        logger.info(
            f"RegimeFilter 初始化: benchmark={self.benchmark_index}, "
            f"ADX_thresh=[{self.adx_normal_max},{self.adx_warning_max}], "
            f"vol_pct=[{self.vol_normal_low},{self.vol_normal_high}], "
            f"smoothing={self.smoothing_days}d, confirm={self.confirm_days}d"
        )

    def calculate_market_volatility(
        self,
        close_prices: pd.Series,
        period: int = 60
    ) -> Tuple[float, float]:
        """
        计算宽基指数的60日已实现波动率和其历史分位数。

        Parameters:
            close_prices: 收盘价序列（足够长，至少252+60天）
            period: 波动率计算周期

        Returns:
            (current_vol, vol_percentile): 当前波动率，历史分位数
        """
        # 60日已实现波动率（年化）
        returns = close_prices.pct_change().dropna()
        vol_60d = returns.rolling(period).std() * np.sqrt(252)

        if vol_60d.empty or pd.isna(vol_60d.iloc[-1]):
            return 0.20, 0.50  # 默认中等波动

        current_vol = vol_60d.iloc[-1]

        # 过去252个交易日的波动率历史
        vol_history = vol_60d.dropna().iloc[-252:]
        if len(vol_history) < 50:
            return current_vol, 0.50  # 数据不足返回中位

        # 计算当前波动率在历史分布中的分位数
        vol_percentile = (vol_history < current_vol).mean()

        return current_vol, vol_percentile

    def calculate_adx_approx(
        self,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> float:
        """
        ADX 计算 — 委托给 indicators.py 的标准 Wilder 平滑实现。

        与选股流程共用同一套 Numba JIT 加速的 ADX 算法，
        消除 rolling.mean 近似带来的系统性偏差。

        Parameters:
            high, low, close: 价格数据
            period: 计算周期

        Returns:
            ADX 值 [0, 100]
        """
        from trading_core.indicators import calculate_adx as _std_adx

        tmp = pd.DataFrame({'high': high, 'low': low, 'close': close})
        result = _std_adx(tmp, period=period)

        if result.empty or result['adx'].dropna().empty:
            return 25.0

        return float(result['adx'].dropna().iloc[-1])

    def check_hard_stop_conditions(
        self,
        index_daily_return: float,
        limit_down_count: int,
        volume_shrinking_days: int,
        volume_history: pd.Series
    ) -> bool:
        """
        检查是否触发硬底线条件。

        触发条件（满足其一）：
        1. 宽基指数单日跌幅 >= 5% 且 全市场跌停家数 > 200
        2. 连续3日成交量萎缩至过去60日最低 10%分位
        3. 交易所宣布临时停牌/熔断机制启动（需要外部信号）

        Parameters:
            index_daily_return: 宽基指数单日收益率（负值表示下跌）
            limit_down_count: 跌停家数
            volume_shrinking_days: 成交量萎缩的连续天数
            volume_history: 成交量历史序列

        Returns:
            是否触发硬底线
        """
        # 条件1：指数暴跌 + 跌停家数多
        condition1 = (
            index_daily_return <= -self.hard_stop_index_drop
            and limit_down_count > self.hard_stop_limit_down_count
        )

        # 条件2：成交量连续萎缩
        if len(volume_history) >= 60:
            vol_60d_min = volume_history.iloc[-60:].min()
            condition2 = (
                volume_shrinking_days >= 3
                and volume_history.iloc[-1] <= vol_60d_min * (1 + self.hard_stop_volume_shrink_pct)
            )
        else:
            condition2 = False

        # 条件3：需要外部信号，在外部处理

        if condition1 or condition2:
            logger.critical(
                f"硬底线触发! condition1={condition1}, condition2={condition2}, "
                f"index_return={index_daily_return:.2%}, limit_down={limit_down_count}"
            )

        return condition1 or condition2

    def _apply_smoothing(self, adx: float, vol_pct: float) -> Tuple[float, float]:
        """
        应用3日移动平均平滑。

        Parameters:
            adx: 当前ADX
            vol_pct: 当前波动率分位

        Returns:
            平滑后的 (adx, vol_pct)
        """
        self._adx_history.append(adx)
        self._vol_pct_history.append(vol_pct)

        # 保持最近smoothing_days个
        if len(self._adx_history) > self.smoothing_days:
            self._adx_history = self._adx_history[-self.smoothing_days:]
        if len(self._vol_pct_history) > self.smoothing_days:
            self._vol_pct_history = self._vol_pct_history[-self.smoothing_days:]

        # 计算移动平均
        smooth_adx = np.mean(self._adx_history) if self._adx_history else adx
        smooth_vol_pct = np.mean(self._vol_pct_history) if self._vol_pct_history else vol_pct

        return smooth_adx, smooth_vol_pct

    def _check_state_transition(self, new_state: RegimeState) -> RegimeState:
        """
        检查状态切换是否满足连续确认条件。

        Parameters:
            new_state: 初步判断的新状态

        Returns:
            确认后的状态（可能仍是旧状态）
        """
        if new_state == self._confirmed_state:
            self._consecutive_days += 1
        else:
            self._consecutive_days = 1

        # 硬底线直接切换
        if new_state == RegimeState.HARD_CIRCUIT_BREAK:
            self._confirmed_state = new_state
            return new_state

        # 需要连续confirm_days天满足条件才切换
        if self._consecutive_days >= self.confirm_days:
            if self._confirmed_state != new_state:
                logger.warning(
                    f"市场状态切换: {self._confirmed_state.value} -> {new_state.value} "
                    f"(已连续{self._consecutive_days}天满足条件)"
                )
            self._confirmed_state = new_state
        else:
            logger.debug(
                f"市场状态确认中: {new_state.value}, "
                f"第{self._consecutive_days}/{self.confirm_days}天"
            )

        return self._confirmed_state

    def determine_regime(
        self,
        adx: float,
        vol_pct: float
    ) -> RegimeState:
        """
        根据ADX和波动率分位判断市场状态。

        Parameters:
            adx: ADX值
            vol_pct: 波动率历史分位数 [0, 1]

        Returns:
            市场状态
        """
        # 硬底线在 determine_regime 之前检查
        # 此处只做软判断

        # 熔断区（软）：ADX > warning_max 或 波动率极端
        if adx > self.adx_warning_max or vol_pct < self.vol_extreme_low or vol_pct > self.vol_extreme_high:
            return RegimeState.SOFT_CIRCUIT_BREAK

        # 预警区：ADX 25~35 或 波动率偏离正常边界
        if adx > self.adx_normal_max or vol_pct < self.vol_normal_low or vol_pct > self.vol_normal_high:
            return RegimeState.WARNING

        # 正常区
        return RegimeState.NORMAL

    def check(
        self,
        benchmark_data: dict
    ) -> dict:
        """
        检查市场状态，返回参数调整系数。

        Parameters:
            benchmark_data: 宽基指数数据字典，包含：
                - close: 收盘价序列 (pd.Series)
                - high: 最高价序列 (pd.Series)
                - low: 最低价序列 (pd.Series)
                - volume: 成交量序列 (pd.Series, 可选)
                - index_daily_return: 单日收益率 (float, 可选)
                - limit_down_count: 跌停家数 (int, 可选)

        Returns:
            {
                "state": str,  # normal/warning/soft_circuit_break/hard_circuit_break
                "params": {
                    "max_position_per_stock": float,
                    "initial_position": float,
                    "grid_spacing_multiplier": float,
                    "max_grids": int
                },
                "can_open_new": bool,   # 能否开新仓
                "can_buy": bool,        # 能否买入
                "log_msg": str,         # 日志消息
                "adx": float,
                "vol_percentile": float,
                "vol_current": float
            }
        """
        close = benchmark_data.get("close")
        if close is None or len(close) < 100:
            logger.warning("RegimeFilter: 缺少足够的基准指数数据，使用默认正常区参数")
            return self._default_response(RegimeState.NORMAL)

        # 计算当前波动率和历史分位数
        vol_current, vol_pct = self.calculate_market_volatility(close)

        # 计算ADX
        high = benchmark_data.get("high", close)
        low = benchmark_data.get("low", close)
        adx = self.calculate_adx_approx(high, low, close)

        # 应用平滑
        smooth_adx, smooth_vol_pct = self._apply_smoothing(adx, vol_pct)

        # 检查硬底线条件
        index_return = benchmark_data.get("index_daily_return", 0.0)
        limit_down_count = benchmark_data.get("limit_down_count", 0)
        volume = benchmark_data.get("volume")
        volume_shrink_days = benchmark_data.get("volume_shrink_days", 0)

        if self.check_hard_stop_conditions(
            index_return, limit_down_count, volume_shrink_days, volume if volume is not None else pd.Series()
        ):
            state = RegimeState.HARD_CIRCUIT_BREAK
            self._confirmed_state = state
            self._consecutive_days = 0
        else:
            # 初步判断状态
            raw_state = self.determine_regime(smooth_adx, smooth_vol_pct)
            # 应用状态平滑确认
            state = self._check_state_transition(raw_state)

        # 获取参数
        params = self.regime_params.get(state, self.regime_params[RegimeState.NORMAL])

        # 判断能否开仓/买入
        can_open_new = state in (RegimeState.NORMAL,)
        can_buy = state in (RegimeState.NORMAL, RegimeState.WARNING)

        # 生成日志消息
        state_desc = {
            RegimeState.NORMAL: "正常区",
            RegimeState.WARNING: "预警区",
            RegimeState.SOFT_CIRCUIT_BREAK: "熔断区(软)",
            RegimeState.HARD_CIRCUIT_BREAK: "熔断区(硬)"
        }
        log_msg = (
            f"{state_desc[state]} | "
            f"ADX={smooth_adx:.1f}, 波动率={vol_current:.1%}, "
            f"波动率分位={smooth_vol_pct:.0%} | "
            f"max_position={params.max_position_per_stock:.0%}, "
            f"spacing={params.grid_spacing_multiplier:.1f}x"
        )

        if state == RegimeState.HARD_CIRCUIT_BREAK:
            logger.critical(f"市场状态: {log_msg}")
        elif state == RegimeState.WARNING:
            logger.warning(f"市场状态: {log_msg}")
        else:
            logger.info(f"市场状态: {log_msg}")

        return {
            "state": state.value,
            "params": {
                "max_position_per_stock": params.max_position_per_stock,
                "initial_position": params.initial_position,
                "grid_spacing_multiplier": params.grid_spacing_multiplier,
                "max_grids": params.max_grids
            },
            "can_open_new": can_open_new,
            "can_buy": can_buy,
            "log_msg": log_msg,
            "adx": smooth_adx,
            "vol_percentile": smooth_vol_pct,
            "vol_current": vol_current
        }

    def _default_response(self, state: RegimeState) -> dict:
        """返回默认响应（数据不足时）"""
        params = self.regime_params[state]
        return {
            "state": state.value,
            "params": {
                "max_position_per_stock": params.max_position_per_stock,
                "initial_position": params.initial_position,
                "grid_spacing_multiplier": params.grid_spacing_multiplier,
                "max_grids": params.max_grids
            },
            "can_open_new": True,
            "can_buy": True,
            "log_msg": f"{state.value}: 数据不足，使用默认参数",
            "adx": 25.0,
            "vol_percentile": 0.50,
            "vol_current": 0.20
        }

    def reset(self) -> None:
        """重置状态（用于测试或策略重启）"""
        self._adx_history.clear()
        self._vol_pct_history.clear()
        self._confirmed_state = RegimeState.NORMAL
        self._consecutive_days = 0
        logger.info("RegimeFilter 已重置")

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """将 RegimeFilter 内部状态序列化为字典，用于持久化存储。"""
        return {
            "confirmed_state": self._confirmed_state.value,
            "consecutive_days": self._consecutive_days,
            "adx_history": self._adx_history.copy(),
            "vol_pct_history": self._vol_pct_history.copy(),
            "saved_at": pd.Timestamp.now().isoformat(),
        }

    def from_dict(self, data: dict) -> None:
        """从字典恢复 RegimeFilter 内部状态。"""
        if not data:
            return
        try:
            self._confirmed_state = RegimeState(data.get("confirmed_state", "normal"))
        except ValueError:
            self._confirmed_state = RegimeState.NORMAL
        self._consecutive_days = data.get("consecutive_days", 0)
        self._adx_history = data.get("adx_history", []).copy()
        self._vol_pct_history = data.get("vol_pct_history", []).copy()
        logger.info(
            f"RegimeFilter 状态已恢复: {self._confirmed_state.value}, "
            f"连续{self._consecutive_days}天, 历史记录{len(self._adx_history)}条"
        )

    # ------------------------------------------------------------------
    # 历史市场状态查询（用于回测/优化）
    # ------------------------------------------------------------------
    def check_historical(
        self,
        benchmark_data: dict,
        as_of_date: pd.Timestamp = None,
    ) -> dict:
        """
        检查指定历史日期的市场状态（跳过平滑和确认机制）。

        与 check() 的区别：
        - check() 面向实时信号，应用平滑+确认，并更新内部状态
        - check_historical() 面向回测/优化，直接返回该日期的原始状态，不修改内部状态

        Parameters:
            benchmark_data: 宽基指数完整历史数据字典
            as_of_date: 评估日期（默认使用最后一天）

        Returns:
            与 check() 格式相同的字典，但 state 字段为原始状态（未经确认）
        """
        close = benchmark_data.get("close")
        if close is None or len(close) < 100:
            logger.warning("RegimeFilter: 历史数据不足，使用默认正常区参数")
            return self._default_response(RegimeState.NORMAL)

        if as_of_date is None:
            as_of_date = close.index[-1]

        # 截取到 as_of_date 的数据（避免未来数据泄露）
        close_hist = close[close.index <= as_of_date]
        high_hist = benchmark_data.get("high", close)[close.index <= as_of_date]
        low_hist = benchmark_data.get("low", close)[close.index <= as_of_date]

        if len(close_hist) < 100:
            return self._default_response(RegimeState.NORMAL)

        # 计算该日期的波动率和分位数
        vol_current, vol_pct = self.calculate_market_volatility(close_hist)

        # 计算该日期的 ADX
        adx = self.calculate_adx_approx(high_hist, low_hist, close_hist)

        # 直接判断状态（不应用平滑和确认）
        raw_state = self.determine_regime(adx, vol_pct)

        params = self.regime_params.get(raw_state, self.regime_params[RegimeState.NORMAL])

        state_desc = {
            RegimeState.NORMAL: "正常区",
            RegimeState.WARNING: "预警区",
            RegimeState.SOFT_CIRCUIT_BREAK: "熔断区(软)",
            RegimeState.HARD_CIRCUIT_BREAK: "熔断区(硬)",
        }
        log_msg = (
            f"{state_desc[raw_state]} | "
            f"ADX={adx:.1f}, 波动率={vol_current:.1%}, "
            f"波动率分位={vol_pct:.0%} | "
            f"max_position={params.max_position_per_stock:.0%}, "
            f"spacing={params.grid_spacing_multiplier:.1f}x "
            f"(日期: {as_of_date.strftime('%Y-%m-%d')})"
        )
        logger.info(f"历史市场状态: {log_msg}")

        return {
            "state": raw_state.value,
            "params": {
                "max_position_per_stock": params.max_position_per_stock,
                "initial_position": params.initial_position,
                "grid_spacing_multiplier": params.grid_spacing_multiplier,
                "max_grids": params.max_grids,
            },
            "can_open_new": raw_state in (RegimeState.NORMAL,),
            "can_buy": raw_state in (RegimeState.NORMAL, RegimeState.WARNING),
            "log_msg": log_msg,
            "adx": adx,
            "vol_percentile": vol_pct,
            "vol_current": vol_current,
        }
