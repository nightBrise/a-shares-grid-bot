"""
策略模块 - A 股网格交易系统 v2.0.0
功能：选股、参数优化、信号生成 (核心大脑)
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
import pandas as pd
import numpy as np
import yaml

try:
    import optuna
except ImportError:
    raise ImportError("请安装 optuna: pip install optuna>=3.0.0")

from data_layer.fetcher import (
    get_stock_data, clean_data,
    align_to_trading_day, get_next_trading_day,
)
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import threading
from utils import (
    load_config, calculate_transaction_fee, validate_buy_quantity,
    safe_divide
)

from trading_core.indicators import calculate_all_indicators, get_latest_indicators, calculate_atr
from trading_core.screener import AdvancedMultiFactorScreener
from trading_core.grid_engine import DynamicGridEngine, compute_adaptive_spacing
from risk_management.circuit_breaker import create_risk_control_manager

logger = logging.getLogger("grid_trading")

# 默认常量
DEFAULT_INITIAL_CASH = 1000000.0


# ==================== 选股逻辑 ====================

def run_selection(config: dict, auto_update_config: bool = True,
                  force_select: bool = False) -> pd.DataFrame:
    """
    选股模式：使用多因子横截面打分筛选适合网格交易的标的

    多因子模型通过横截面相对排序选择"最适合"网格交易的股票，
    解决了 A 股 Hurst > 0.5 (趋势市场) 导致绝对阈值无法筛选的问题。

    因子:
    - OU 半衰期 (35%): 快速均值回归优先
    - Hurst 指数 (30%): 均值回归特性
    - ADX (20%): 趋势强度，越低越适合网格
    - 波动率适配 (15%): 中等波动最佳

    参数:
        config: 配置字典
        auto_update_config: 是否自动更新配置文件

    返回:
        多因子得分 Top N 股票列表 DataFrame
    """
    logger.info("=" * 60)
    logger.info("使用多因子横截面打分选股器")
    logger.info("=" * 60)
    return run_multi_factor_selection(config, auto_update_config, force_select)


# ==================== 并行化选股辅助函数 ====================


def _fetch_single_stock_data(args: Tuple) -> Tuple[str, pd.DataFrame, str]:
    """
    线程 worker：获取单只股票历史数据

    参数:
        args: (code, data_dir, force_full, config) 元组

    返回:
        (code, df, status) 元组
    """
    code, data_dir, force_full, cfg = args
    try:
        # 网络不稳定时优先使用缓存，避免重试等待
        df = get_stock_data(code, data_dir=data_dir, force_full=force_full,
                           enable_incremental=False, use_cache=True, fallback_to_cache=True)

        return (code, df, "success")
    except Exception as e:
        logger.debug(f"{code} 获取失败: {e}")
        return (code, pd.DataFrame(), f"error: {e}")


def run_multi_factor_selection(config: dict, auto_update_config: bool = True,
                               force_select: bool = False) -> pd.DataFrame:
    """
    选股模式入口

    两种路径：
    1. 自定义股票模式：config.stocks 有股票且无 --force-select
       → 直接调用 run_two_phase_optimization() 进行优化和回测
    2. 全市场选股模式：config.stocks 为空或使用 --force-select
       → 扫描全市场股票，多因子打分，筛选 top N

    参数:
        config: 配置字典
        auto_update_config: 是否自动更新配置文件
        force_select: 是否强制全市场选股

    返回:
        自定义模式: 优化结果字典
        全市场模式: 多因子得分 Top N 股票列表 DataFrame
    """
    existing_stocks = config.get('stocks', [])

    # 检查股票池是否过期（仅提示，不阻断执行）
    if existing_stocks:
        try:
            from utils import load_state
            state = load_state()
            selection_status = state.get('selection_status', {})
            last_date = selection_status.get('last_selection_date', '')
            if last_date:
                days_since = (datetime.now() - datetime.strptime(last_date, '%Y-%m-%d')).days
                if days_since > 90:
                    logger.warning(
                        f"⚠ 股票池已 {days_since} 天未更新（上次选股：{last_date}），"
                        f"建议重新选股"
                    )
        except Exception:
            pass

    # 自定义股票模式：有股票且未强制全市场选股
    if existing_stocks and not force_select:
        logger.info("=" * 60)
        logger.info(f"检测到已有股票池：{len(existing_stocks)} 只股票")
        logger.info("跳过全市场选股，直接进入网格参数优化和回测")
        logger.info("如需重新选股，请使用 --force-select 或清空 config.stocks")
        logger.info("=" * 60)

        # 调用优化和回测
        return run_two_phase_optimization(config)

    logger.info("=" * 60)
    logger.info("开始执行高级多因子横截面打分选股 (v2)...")
    logger.info("=" * 60)

    paths_cfg = config.get('paths', {})
#     selection_cfg = config.get('selection', {})
    adv_cfg = config.get('advanced_screening', {})
    data_dir = paths_cfg.get('data_dir', './data')

    # 计算每只股票的因子指标
    stocks_factors = []
    success_count = 0
    fail_count = 0

    # 获取 Path Memory 参数
    pm_cfg = adv_cfg.get('path_memory', {})
    vr_min_periods = pm_cfg.get('min_periods', 120)
    min_periods_required = max(60, vr_min_periods)

    results_lock = threading.Lock()

    def process_result(code: str, df: pd.DataFrame, status: str, is_st: bool = False):
        """处理单个股票的获取结果"""
        nonlocal success_count, fail_count

        if status != "success" or df.empty:
            with results_lock:
                fail_count += 1
            return

        try:
            # 清洗数据
            df = clean_data(df)

            # 检查数据长度
            if len(df) < min_periods_required:
                with results_lock:
                    fail_count += 1
                return

            # 计算所有指标
            df_with_indicators = calculate_all_indicators(df)
            latest = get_latest_indicators(df_with_indicators)

            if not latest or all(v is None for v in latest.values()):
                with results_lock:
                    fail_count += 1
                return

            # 检查 Path Memory 因子
            if latest.get('path_memory') is None:
                with results_lock:
                    fail_count += 1
                return

            # 获取价格和成交额
            latest_price = df['close'].iloc[-1]
            avg_turnover = df['amount'].tail(20).mean() / 10000  # 万元

            factor_data = {
                'code': code,
                'price': latest_price,
                'avg_turnover': avg_turnover,
                'is_st': is_st,
                **latest
            }

            with results_lock:
                stocks_factors.append(factor_data)
                success_count += 1

        except Exception as e:
            logger.debug(f"处理 {code} 时发生异常：{str(e)}")
            with results_lock:
                fail_count += 1

    # ============================================================
    # 严格本地模式：只读取 SQLite 数据，不足时报错
    # ============================================================
    from data_layer.market_db import get_all_codes_from_kline, load_st_flags, get_multi_stock_data

    local_codes = get_all_codes_from_kline(data_dir)

    if len(local_codes) < 500:
        raise RuntimeError(
            f"本地 SQLite 仅有 {len(local_codes)} 只股票，"
            f"请先运行: python main.py --download-db"
        )

    logger.info(f"本地 SQLite 有 {len(local_codes)} 只股票数据，使用本地优先模式")
    st_flags = load_st_flags(data_dir)

    # 计算需要的历史数据起始日期（Path Memory 需要至少 120 天，留足余量取 2 年）
    start_date = (datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')

    # 批量读取所有历史数据
    df_all = get_multi_stock_data(local_codes, start_date=start_date, data_dir=data_dir)

    if df_all is None or df_all.empty:
        raise RuntimeError("本地数据读取为空，请检查数据库或重新运行 --download-db")

    logger.info(f"SQLite 批量读取: {df_all['code'].nunique()} 只股票, {len(df_all)} 条记录")

    total = len(local_codes)
    for i, code in enumerate(local_codes):
        df_code = df_all[df_all['code'] == code].copy()
        if not df_code.empty:
            df_code = df_code.drop(columns=['code'], errors='ignore')
            is_st = st_flags.get(code, False)
            process_result(code, df_code, "success", is_st)

        # 进度日志
        if (i + 1) % max(1, total // 10) == 0:
            pct = 100 * (i + 1) // total
            logger.info(f"进度: {i + 1}/{total} ({pct}%)")

    logger.info(f"指标计算完成: 成功 {success_count}, 失败 {fail_count}")

    # 打印数据源健康报告（验证限流效果）
    try:
        from data_layer.fetcher import get_source_manager
        get_source_manager().print_health_report()
    except Exception:
        pass

    if not stocks_factors:
        logger.error("没有获取到任何股票数据")
        return pd.DataFrame()

    # 使用高级多因子打分器筛选
    screener = AdvancedMultiFactorScreener(config)

    df_result = screener.screen(stocks_factors, asset_type="stock")

    if df_result.empty:
        logger.warning("高级多因子筛选后无股票，尝试放宽阈值...")
        # 放宽条件重试
        alt_config = config.copy()
        alt_adv = alt_config.get('advanced_screening', {}).copy()
        alt_adv['quality_threshold'] = 0.55  # 降低阈值
        alt_config['advanced_screening'] = alt_adv
        screener = AdvancedMultiFactorScreener(alt_config)
        df_result = screener.screen(stocks_factors, asset_type="stock")

    if df_result.empty:
        logger.error("高级多因子筛选无结果")
        return pd.DataFrame()

    # 添加更多信息列
    df_result['reason'] = df_result.apply(
        lambda row: f"综合:{row.get('final_score', row['total_score']):.4f} "
                    f"因子:{row['total_score']:.4f} "
                    f"资金适配:{row.get('capital_fitness', 1.0):.2f} "
                    f"(F1:{row.get('F1_norm', 0):.2f} "
                    f"F2:{row.get('F2_norm', 0):.2f} "
                    f"F3:{row.get('F3_norm', 0):.2f} "
                    f"F4:{row.get('F4_ortho', 0):.2f})",
        axis=1
    )

    logger.info("\n" + "=" * 60)
    logger.info("高级多因子选股结果 (Top 10):")
    logger.info("=" * 60)

    for _, row in df_result.head(10).iterrows():
        params = row.get('grid_params', {})
        threshold_mark = "✓" if row.get('passes_threshold', True) else "⚠"
        logger.info(
            f"{threshold_mark} #{int(row['rank']):2d} {row['code']:<12s} | "
            f"{row['reason']} | 网格:{params.get('spacing_coef', 'N/A')}×ATR"
        )

    # 自动更新配置文件
    if auto_update_config and len(df_result) > 0:
        df_full = getattr(screener, 'last_full_results', df_result)
        update_config_with_selected_stocks(df_result, df_full, config)

    return df_result


def update_config_with_selected_stocks(df_selection: pd.DataFrame,
                                       df_full: pd.DataFrame,
                                       config: dict):
    """
    将选股结果写入配置文件，并将选股状态写入状态文件

    参数:
        df_selection: 选股结果 DataFrame（Top N）
        df_full: 完整评分结果 DataFrame（所有评分过的股票）
        config: 当前配置字典
    """
    selected_stocks = df_selection['code'].tolist()

    logger.info(f"\n选股结果: {len(selected_stocks)} 只股票")

    today_str = datetime.now().strftime('%Y-%m-%d')

    # 1. 保存完整评分数据到 SQLite
    data_dir = config.get('paths', {}).get('data_dir', './data')
    try:
        from data_layer.market_db import save_screening_results
        # 用 df_selection 中的 rank 更新完整 df
        df_save = df_full.copy()
        df_save = df_save.drop(columns=['rank'], errors='ignore')
        if 'rank' in df_selection.columns:
            rank_map = df_selection.set_index('code')['rank'].to_dict()
            df_save['rank'] = df_save['code'].map(rank_map).fillna(0).astype(int)
        save_screening_results(df_save, data_dir, selected_codes=selected_stocks)
        logger.info(f"✓ 选股评分数据已保存到 SQLite（{len(df_save)} 条记录，"
                    f"选中 {len(selected_stocks)} 只）")
    except Exception as e:
        logger.warning(f"保存选股结果到 SQLite 失败: {e}")

    # 2. 更新 config.yaml 中的 stocks 列表（完整 YAML 读写）
    config_path = os.path.join(os.path.dirname(__file__), '..', 'configuration', 'config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        full_config = yaml.safe_load(f)

    full_config['stocks'] = selected_stocks
    full_config.pop('selection_status', None)

    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(full_config, f, allow_unicode=True,
                       default_flow_style=False, sort_keys=False)

    # 3. 写入选股状态到 config_state.json
    from utils import load_state, save_state
    state_path = 'configuration/config_state.json'
    state = load_state(state_path)
    state['selection_status'] = {
        'completed': True,
        'last_selection_date': today_str,
        'last_data_update_date': today_str,
        'selection_count': len(selected_stocks),
        'version': 'v2.0.0',
    }
    save_state(state, state_path)

    logger.info(f"✓ 配置文件已更新：{config_path}")
    logger.info(f"✓ 选股状态已写入：{state_path}")
    logger.info(f"  新股票池：{', '.join(selected_stocks[:5])}{'...' if len(selected_stocks) > 5 else ''}")
    logger.info(f"  选股日期：{today_str}")
    logger.info("  下次运行将直接使用这些股票，无需重新选股")
    logger.info("  如需重新选股，请使用 --force-select 参数")


# ==================== 网格回测引擎 ====================

def backtest_grid_strategy(df: pd.DataFrame,
                           buy_spacing_pct: float,
                           sell_spacing_pct: float,
                           grid_amount: float,
                           amount_multiplier: float,
                           initial_position: float,
                           max_grids: int,
                           spacing_decay: float,
                           commission_rate: float,
                           stamp_tax: float,
                           slippage_rate: float = 0.001,
                            initial_cash: float = DEFAULT_INITIAL_CASH,
                           atr_coef: float = 1.5) -> Dict:
    """
    网格交易回测引擎（7参数版 — 含滑点、金字塔加仓、间距衰减）

    参数:
        df: 历史行情数据 (包含 close 列)
        buy_spacing_pct: 买入网格基础间距百分比 (如 0.02 = 2%)
        sell_spacing_pct: 卖出网格基础间距百分比
        grid_amount: 基础每格金额 (元)
        amount_multiplier: 加仓倍数 (第k层金额 = amount × multiplier^k)
        initial_position: 初始仓位 (小数，如 0.45 = 45%)
        max_grids: 最大网格层数
        spacing_decay: 间距衰减 (第k层间距 = base × decay^(k-1))
        commission_rate: 佣金费率
        stamp_tax: 印花税率
        slippage_rate: 滑点比率
        initial_cash: 初始资金
    """
    if df.empty or len(df) < 10:
        return {'calmar_ratio': 0, 'total_return': 0, 'max_drawdown': 1}

    # === 流动性硬约束检查（级数求和）===
    grid_pool = initial_cash * (1 - initial_position)
    k_vals = np.arange(max_grids)
    total_investment = float(np.sum(grid_amount * (amount_multiplier ** k_vals)))
    if total_investment > grid_pool * 0.95:
        return {
            'calmar_ratio': -999,
            'total_return': -1,
            'max_drawdown': 1,
            'reason': 'GRID_POOL_EXCEEDED',
            'grid_pool': grid_pool,
            'required': total_investment,
        }

    prices = df['close'].values

    # 预计算 ATR 序列（用于动态间距）
    atr_series = None
    if 'high' in df.columns and 'low' in df.columns:
        try:
            from trading_core.indicators import calculate_atr as _calc_atr
            atr_series = _calc_atr(df, period=20).values
        except Exception:
            pass
    # ATR 回退：若无 high/low 列或计算失败，使用价格的 2% 作为默认
    if atr_series is None:
        atr_series = np.full(len(prices), prices[0] * 0.02)

    # 初始化状态
    cash = initial_cash
    position = 0  # 持仓股数（全部，含今日买入）
    avg_cost = 0
    total_slippage_cost = 0.0

    # 初始建仓（T 日买入，T+1 才可卖）
    first_price = prices[0]
    initial_investment = cash * initial_position
    position = int(initial_investment / first_price / 100) * 100
    actual_buy_price = first_price * (1 + slippage_rate)
    cost = position * actual_buy_price
    fee = calculate_transaction_fee(cost, 'buy', commission_rate, stamp_tax)
    cash -= cost + fee
    avg_cost = actual_buy_price

    # 交易记录
    trades = []
    portfolio_values = []

    # 网格价格计算（7参数：不对称间距 + 累积衰减）
    # buy_spacing/sell_spacing 已是 ratio 形式（0.02 = 2%）
    def get_grid_prices(center: float, buy_spacing: float, sell_spacing: float,
                        decay: float, n_grids: int) -> Tuple[List, List]:
        decay_exps = np.array([decay ** j for j in range(n_grids)])
        buy_cum_full = buy_spacing * np.cumsum(decay_exps)
        sell_cum_full = sell_spacing * np.cumsum(decay_exps)
        # 截断负价格层（高 decay + 多层时 cum > 1.0）
        valid_mask = buy_cum_full < 1.0
        buy_prices = (center * (1 - buy_cum_full[valid_mask])).tolist()
        sell_prices = (center * (1 + sell_cum_full[:len(buy_prices)])).tolist()
        return buy_prices, sell_prices

    # 逐日回测
    for i, price in enumerate(prices[1:], start=1):
        prev_price = prices[i - 1]

        # === 涨跌停检查 ===
        change_pct = abs(price - prev_price) / prev_price if prev_price > 0 else 0
        is_limit_up = change_pct >= 0.098 and price > prev_price
        is_limit_down = change_pct >= 0.098 and price < prev_price

        # === 滚动网格中心 = T-1 收盘价（与实盘一致）===
        center_price = prev_price

        # === 动态间距（ATR 自适应，买卖分离，与实盘统一公式）===
        current_atr = atr_series[i] if i < len(atr_series) else atr_series[-1]
        if current_atr <= 0:
            current_atr = center_price * 0.02
        daily_vol = current_atr / max(center_price, 0.01)  # ATR ratio ≈ 日波动率
        dynamic_buy_spacing = compute_adaptive_spacing(
            buy_spacing_pct, center_price, current_atr, atr_coef,
            daily_volatility=daily_vol,
            clamp_low=0.003, clamp_high=0.15,
        )
        dynamic_sell_spacing = compute_adaptive_spacing(
            sell_spacing_pct, center_price, current_atr, atr_coef,
            daily_volatility=daily_vol,
            clamp_low=0.003, clamp_high=0.15,
        )

        # === T+1: 开盘时可卖持仓 = 昨日收盘时的全部持仓 ===
        available_position = position  # 今日买入的还没发生，所以全部可卖
        today_bought = 0

        # === 强制平仓检查 ===
        lower_rail = center_price - 2 * current_atr
        force_close_trigger = lower_rail * (1 - dynamic_buy_spacing * 3)
        if price <= force_close_trigger and available_position > 100:
            fc_qty = max(100, int(available_position * 0.5 / 100) * 100)
            fc_qty = min(fc_qty, available_position)
            actual_sell_price = price * (1 - slippage_rate)
            revenue = fc_qty * actual_sell_price
            sell_fee = calculate_transaction_fee(revenue, 'sell', commission_rate, stamp_tax)
            position -= fc_qty
            available_position -= fc_qty
            cash += revenue - sell_fee
            slippage_cost = fc_qty * (price - actual_sell_price)
            total_slippage_cost += slippage_cost
            trades.append({
                'day': i, 'type': 'sell', 'price': actual_sell_price,
                'qty': fc_qty, 'revenue': revenue - sell_fee,
                'slippage': slippage_cost, 'reason': 'force_close'
            })

        buy_prices, sell_prices = get_grid_prices(
            center_price, dynamic_buy_spacing, dynamic_sell_spacing,
            spacing_decay, max_grids)

        # === 买入（跌停不买，金字塔加仓）===
        if not is_limit_down:
            for buy_layer, buy_price in enumerate(buy_prices):
                buy_amount = grid_amount * (amount_multiplier ** buy_layer)
                if price <= buy_price and cash > buy_amount * 1.1:
                    buy_qty = validate_buy_quantity(int(buy_amount / buy_price))
                    if buy_qty > 0 and cash >= buy_qty * buy_price * 1.01:
                        actual_buy_price = buy_price * (1 + slippage_rate)
                        cost = buy_qty * actual_buy_price
                        buy_fee = calculate_transaction_fee(cost, 'buy', commission_rate, stamp_tax)
                        if cash >= cost + buy_fee:
                            total_cost = position * avg_cost + buy_qty * actual_buy_price
                            position += buy_qty
                            today_bought += buy_qty
                            avg_cost = total_cost / position if position > 0 else 0
                            cash -= cost + buy_fee
                            slippage_cost = buy_qty * (actual_buy_price - buy_price)
                            total_slippage_cost += slippage_cost
                            trades.append({
                                'day': i, 'type': 'buy', 'price': actual_buy_price,
                                'qty': buy_qty, 'cost': cost + buy_fee,
                                'slippage': slippage_cost, 'layer': buy_layer + 1
                            })

        # === 卖出（涨停不卖，T+1约束，倒金字塔）===
        if not is_limit_up:
            for sell_layer, sell_price in enumerate(sell_prices):
                if price >= sell_price and available_position > 0:
                    inv_multiplier = 1.0 / max(amount_multiplier, 0.1)
                    sell_amount = grid_amount * (inv_multiplier ** sell_layer)
                    sell_qty = min(available_position, validate_buy_quantity(
                        int(sell_amount / sell_price)
                    ))
                    if sell_qty > 0:
                        actual_sell_price = sell_price * (1 - slippage_rate)
                        revenue = sell_qty * actual_sell_price
                        sell_fee = calculate_transaction_fee(revenue, 'sell', commission_rate, stamp_tax)
                        position -= sell_qty
                        available_position -= sell_qty
                        cash += revenue - sell_fee
                        slippage_cost = sell_qty * (sell_price - actual_sell_price)
                        total_slippage_cost += slippage_cost
                        trades.append({
                            'day': i, 'type': 'sell', 'price': actual_sell_price,
                            'qty': sell_qty, 'revenue': revenue - sell_fee,
                            'slippage': slippage_cost, 'layer': sell_layer + 1
                        })

        # 计算组合市值
        portfolio_value = cash + position * price
        portfolio_values.append(portfolio_value)
    
    # === 买入持有基准 ===
    bh_initial_investment = initial_cash * initial_position
    bh_position = int(bh_initial_investment / first_price / 100) * 100
    bh_actual_buy_price = first_price * (1 + slippage_rate)
    bh_cost = bh_position * bh_actual_buy_price
    bh_fee = calculate_transaction_fee(bh_cost, 'buy', commission_rate, stamp_tax)
    bh_cash = initial_cash - bh_cost - bh_fee
    benchmark_values = [bh_cash + bh_position * p for p in prices[1:]]
    benchmark_return = (benchmark_values[-1] - initial_cash) / initial_cash if benchmark_values else 0

    # === 计算绩效指标 ===

    # 总收益率
    final_value = portfolio_values[-1] if portfolio_values else 0
    initial_value = initial_cash
    total_return = (final_value - initial_value) / initial_value
    
    # 最大回撤
    peak = initial_value
    max_drawdown = 0
    for value in portfolio_values:
        if value > peak:
            peak = value
        drawdown = (peak - value) / peak
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    
    # 年化收益率 (假设 252 个交易日)
    n_days = len(prices)
    annual_return = (1 + total_return) ** (252 / n_days) - 1 if n_days > 0 else 0
    
    # 卡尔玛比率 (年化收益 / 最大回撤)
    calmar_ratio = safe_divide(annual_return, max_drawdown, 0)
    
    # 夏普比率 (假设无风险利率为 3%)
    daily_returns = pd.Series(portfolio_values).pct_change().dropna()
    if len(daily_returns) > 1:
        excess_return = daily_returns.mean() * 252 - 0.03
        sharpe_ratio = safe_divide(
            excess_return, 
            daily_returns.std() * np.sqrt(252), 
            0
        )
    else:
        sharpe_ratio = 0
    
    # 交易次数
    n_trades = len(trades)
    
    # 滑点统计
    total_trade_amount = sum(
        abs(t.get('cost', 0) - t.get('fee', 0)) if t['type'] == 'buy' 
        else t.get('revenue', 0) + t.get('fee', 0)
        for t in trades
    )
    slippage_ratio = safe_divide(total_slippage_cost, total_trade_amount, 0) if total_trade_amount > 0 else 0
    
    return {
        'total_return': total_return,
        'annual_return': annual_return,
        'max_drawdown': max_drawdown,
        'calmar_ratio': calmar_ratio,
        'sharpe_ratio': sharpe_ratio,
        'n_trades': n_trades,
        'final_value': final_value,
        'trades': trades,
        'total_slippage_cost': total_slippage_cost,
        'slippage_ratio': slippage_ratio,
        'portfolio_values': portfolio_values,
        'initial_value': initial_cash,
        'benchmark_values': benchmark_values,
        'benchmark_return': benchmark_return,
    }


# ==================== 两阶段优化 ====================

def _optimize_single_stock_worker(
    code: str,
    config: dict,
    allocated_cash: float,
    search_space: dict,
    ins_bytes: bytes,
    oos_bytes: bytes,
    ins_start: str,
    ins_end: str,
    oos_start: str,
    oos_end: str,
    regime_state: str = 'normal',
    regime_params: dict = None,
    regime_state_phase1: str = None,
    regime_state_phase2: str = None
) -> dict:
    """
    Worker 线程主函数：对单只股票执行完整两阶段优化（Phase1 贝叶斯 + Phase2 WF微调）

    参数:
        code: 股票代码
        config: 配置字典（只读）
        allocated_cash: 每只股票分配的资金
        search_space: 搜索空间字典
        ins_bytes: 样本内数据（df_ins）的 pickle bytes
        oos_bytes: 样本外数据（df_oos）的 pickle bytes
        ins_start/ins_end/oos_start/oos_end: 日期范围字符串
        regime_state: 兼容旧接口的市场状态（当 phase1/phase2 未指定时使用）
        regime_state_phase1: Phase1 回测期结束时的市场状态
        regime_state_phase2: Phase2 回测期结束时的市场状态

    返回:
        优化结果字典（含 phase1_params, final_params, final_calmar 等）
    """
    # ProcessPoolExecutor 子进程不继承 logging 配置，需独立初始化
    import logging
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')

    # 兼容旧接口：如果未指定分阶段状态，使用统一状态
    rs_p1 = regime_state_phase1 if regime_state_phase1 is not None else regime_state
    rs_p2 = regime_state_phase2 if regime_state_phase2 is not None else regime_state
    import pickle

    # 反序列化数据（避免进程间共享 DataFrame 引用）
    df_ins = pickle.loads(ins_bytes)
    df_oos = pickle.loads(oos_bytes)

    backtest_cfg = config.get('backtest', {})
    n_startup = backtest_cfg.get('n_startup_trials', 30)
    phase2_trials = 30

    try:
        logger.info(f"[{code}] Phase 1 数据：{len(df_ins)}天 ({ins_start} 至 {ins_end})")
        logger.info(f"[{code}] Phase 2 数据：{len(df_oos)}天 ({oos_start} 至 {oos_end})")

        if len(df_ins) < 60:
            return {'code': code, 'error': f'Phase1 数据不足: {len(df_ins)}天'}

        # ========== Phase 1: 贝叶斯优化（7参数）==========
        n_trials_actual = backtest_cfg.get('n_trials', 300)
        logger.info(f"[{code}] >>> Phase 1: 贝叶斯优化 (n_trials={n_trials_actual}, 7参数)")

        def objective_phase1(trial):
            # 根据 Phase1 对应的市场状态动态调整搜索空间
            if rs_p1 == 'soft_circuit_break':
                buy_spacing_range = [max(0.02, search_space['buy_spacing_range'][0]),
                                     min(0.04, search_space['buy_spacing_range'][1])]
                sell_spacing_range = [max(0.015, search_space['sell_spacing_range'][0]),
                                      min(0.04, search_space['sell_spacing_range'][1])]
                max_grids_range = [3, 5]
                ip_range = [0.20, 0.35]
                multiplier_range = [0.5, 1.5]
            elif rs_p1 == 'warning':
                buy_spacing_range = search_space['buy_spacing_range']
                sell_spacing_range = search_space['sell_spacing_range']
                max_grids_range = [4, 8]
                ip_range = [0.25, 0.45]
                multiplier_range = [0.3, 2.5]
            else:  # normal
                buy_spacing_range = search_space['buy_spacing_range']
                sell_spacing_range = search_space['sell_spacing_range']
                max_grids_range = search_space['max_grids_range']
                ip_range = search_space['initial_position_range']
                multiplier_range = search_space['multiplier_range']

            buy_spacing = trial.suggest_float('buy_spacing_pct',
                buy_spacing_range[0], buy_spacing_range[1], step=0.001)
            sell_spacing = trial.suggest_float('sell_spacing_pct',
                sell_spacing_range[0], sell_spacing_range[1], step=0.001)
            grid_amount = trial.suggest_int('grid_amount',
                search_space['grid_amount_range'][0], search_space['grid_amount_range'][1], step=100)
            multiplier = trial.suggest_float('amount_multiplier',
                multiplier_range[0], multiplier_range[1], step=0.05)
            ip = trial.suggest_float('initial_position',
                ip_range[0], ip_range[1], step=0.01)
            max_g = trial.suggest_int('max_grids',
                max_grids_range[0], max_grids_range[1])
            decay = trial.suggest_float('spacing_decay',
                search_space['decay_range'][0], search_space['decay_range'][1], step=0.05)

            result = backtest_grid_strategy(
                df_ins,
                buy_spacing_pct=buy_spacing,
                sell_spacing_pct=sell_spacing,
                grid_amount=grid_amount,
                amount_multiplier=multiplier,
                initial_position=ip,
                max_grids=max_g,
                spacing_decay=decay,
                commission_rate=backtest_cfg.get('commission_rate', 0.00015),
                stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
                slippage_rate=backtest_cfg.get('slippage_rate', 0.001),
                initial_cash=allocated_cash
            )

            return calculate_composite_score(
                result=result,
                buy_spacing_pct=buy_spacing,
                sell_spacing_pct=sell_spacing,
                max_grids=max_g,
                amount_multiplier=multiplier,
                n_days=len(df_ins)
            )

        sampler = optuna.samplers.TPESampler(n_startup_trials=n_startup)
        study = optuna.create_study(
            direction='maximize',
            study_name=f'{code}_phase1',
            load_if_exists=False,
            sampler=sampler,
            pruner=optuna.pruners.MedianPruner()
        )

        study.optimize(objective_phase1, n_trials=n_trials_actual, show_progress_bar=False)

        phase1_params = study.best_params
        phase1_score = study.best_value

        logger.info(f"[{code}] Phase 1 最佳: buy_sp={phase1_params['buy_spacing_pct']:.3f}, "
                    f"sell_sp={phase1_params['sell_spacing_pct']:.3f}, "
                    f"amount={phase1_params['grid_amount']}, mult={phase1_params['amount_multiplier']:.2f}, "
                    f"ip={phase1_params['initial_position']*100:.0f}%, grids={phase1_params['max_grids']}, "
                    f"decay={phase1_params['spacing_decay']:.2f}, score={phase1_score:.4f}")

        # Phase 1 OOS 验证（用7参数）
        result_oos_phase1 = backtest_grid_strategy(
            df_oos,
            buy_spacing_pct=phase1_params['buy_spacing_pct'],
            sell_spacing_pct=phase1_params['sell_spacing_pct'],
            grid_amount=phase1_params['grid_amount'],
            amount_multiplier=phase1_params['amount_multiplier'],
            initial_position=phase1_params['initial_position'],
            max_grids=phase1_params['max_grids'],
            spacing_decay=phase1_params['spacing_decay'],
            commission_rate=backtest_cfg.get('commission_rate', 0.00015),
            stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
            slippage_rate=backtest_cfg.get('slippage_rate', 0.001),
            initial_cash=allocated_cash
        )

        oos_score_phase1 = calculate_composite_score(
            result=result_oos_phase1,
            buy_spacing_pct=phase1_params['buy_spacing_pct'],
            sell_spacing_pct=phase1_params['sell_spacing_pct'],
            max_grids=phase1_params['max_grids'],
            amount_multiplier=phase1_params['amount_multiplier'],
            n_days=len(df_oos)
        )

        logger.info(f"[{code}] Phase 1 OOS: score={oos_score_phase1:.4f}, "
                    f"Calmar={result_oos_phase1.get('calmar_ratio', -999):.4f}, "
                    f"DD={result_oos_phase1.get('max_drawdown', 1.0)*100:.2f}%")

        # ========== Phase 2: WF微调（仅4参数：买卖间距、倍数、衰减）==========
        logger.info(f"[{code}] >>> Phase 2: WF微调 (n_trials={phase2_trials}, 4参数)")

        # 根据 Phase2 对应的市场状态调整微调范围
        if rs_p2 == 'soft_circuit_break':
            fine_tune_range = 0.05
        elif rs_p2 == 'warning':
            fine_tune_range = 0.08
        else:  # normal
            fine_tune_range = 0.10

        # Phase2 微调范围：围绕 Phase1 最优值 ±fine_tune_range
        p2_bs_min = max(0.005, phase1_params['buy_spacing_pct'] * (1 - fine_tune_range))
        p2_bs_max = min(0.08, phase1_params['buy_spacing_pct'] * (1 + fine_tune_range))
        p2_ss_min = max(0.005, phase1_params['sell_spacing_pct'] * (1 - fine_tune_range))
        p2_ss_max = min(0.08, phase1_params['sell_spacing_pct'] * (1 + fine_tune_range))
        p2_mult_min = max(0.25, phase1_params['amount_multiplier'] * (1 - fine_tune_range))
        p2_mult_max = min(3.5, phase1_params['amount_multiplier'] * (1 + fine_tune_range))
        p2_decay_min = max(0.4, phase1_params['spacing_decay'] * (1 - fine_tune_range))
        p2_decay_max = min(2.5, phase1_params['spacing_decay'] * (1 + fine_tune_range))

        # Phase2 固定参数
        p2_grid_amount = phase1_params['grid_amount']
        p2_initial_position = phase1_params['initial_position']
        p2_max_grids = phase1_params['max_grids']

        def objective_phase2(trial):
            buy_spacing = trial.suggest_float(
                'buy_spacing_pct', p2_bs_min, p2_bs_max, step=0.0005)
            sell_spacing = trial.suggest_float(
                'sell_spacing_pct', p2_ss_min, p2_ss_max, step=0.0005)
            multiplier = trial.suggest_float(
                'amount_multiplier', p2_mult_min, p2_mult_max, step=0.02)
            decay = trial.suggest_float(
                'spacing_decay', p2_decay_min, p2_decay_max, step=0.02)

            result = backtest_grid_strategy(
                df_oos,
                buy_spacing_pct=buy_spacing,
                sell_spacing_pct=sell_spacing,
                grid_amount=p2_grid_amount,
                amount_multiplier=multiplier,
                initial_position=p2_initial_position,
                max_grids=p2_max_grids,
                spacing_decay=decay,
                commission_rate=backtest_cfg.get('commission_rate', 0.00015),
                stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
                slippage_rate=backtest_cfg.get('slippage_rate', 0.001),
                initial_cash=allocated_cash
            )

            return calculate_composite_score(
                result=result,
                buy_spacing_pct=buy_spacing,
                sell_spacing_pct=sell_spacing,
                max_grids=p2_max_grids,
                amount_multiplier=multiplier,
                n_days=len(df_oos)
            )

        study_phase2 = optuna.create_study(
            direction='maximize',
            study_name=f'{code}_phase2',
            load_if_exists=False,
            sampler=optuna.samplers.TPESampler(n_startup_trials=5),
            pruner=optuna.pruners.MedianPruner()
        )

        study_phase2.optimize(objective_phase2, n_trials=phase2_trials, show_progress_bar=False)

        final_params = {
            'buy_spacing_pct': study_phase2.best_params['buy_spacing_pct'],
            'sell_spacing_pct': study_phase2.best_params['sell_spacing_pct'],
            'grid_amount': p2_grid_amount,
            'amount_multiplier': study_phase2.best_params['amount_multiplier'],
            'initial_position': p2_initial_position,
            'max_grids': p2_max_grids,
            'spacing_decay': study_phase2.best_params['spacing_decay'],
        }
        final_score = study_phase2.best_value

        logger.info(f"[{code}] Phase 2 完成: buy_sp={final_params['buy_spacing_pct']:.4f}, "
                    f"sell_sp={final_params['sell_spacing_pct']:.4f}, "
                    f"mult={final_params['amount_multiplier']:.2f}, decay={final_params['spacing_decay']:.2f}, "
                    f"score={final_score:.4f}")

        # 最终 OOS 验证（7参数）
        result_final = backtest_grid_strategy(
            df_oos,
            buy_spacing_pct=final_params['buy_spacing_pct'],
            sell_spacing_pct=final_params['sell_spacing_pct'],
            grid_amount=final_params['grid_amount'],
            amount_multiplier=final_params['amount_multiplier'],
            initial_position=final_params['initial_position'],
            max_grids=final_params['max_grids'],
            spacing_decay=final_params['spacing_decay'],
            commission_rate=backtest_cfg.get('commission_rate', 0.00015),
            stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
            slippage_rate=backtest_cfg.get('slippage_rate', 0.001),
            initial_cash=allocated_cash
        )

        logger.info(f"[{code}] 最终 OOS: Calmar={result_final.get('calmar_ratio', -999):.4f}, "
                    f"DD={result_final.get('max_drawdown', 1.0)*100:.2f}%, "
                    f"trades={result_final.get('n_trades', 0)}, "
                    f"ret={result_final.get('total_return', -1)*100:.2f}%")

        return {
            'code': code,
            'phase1_params': phase1_params,
            'phase1_score': phase1_score,
            'phase1_oos_score': oos_score_phase1,
            'final_params': final_params,
            'final_score': final_score,
            'final_calmar': result_final.get('calmar_ratio', -999),
            'final_drawdown': result_final.get('max_drawdown', 1.0),
            'final_trades': result_final.get('n_trades', 0),
            'final_return': result_final.get('total_return', -1),
            'error': None
        }

    except Exception as e:
        logger.error(f"[{code}] 优化失败: {str(e)}")
        return {
            'code': code,
            'phase1_params': {},
            'phase1_score': 0,
            'phase1_oos_score': 0,
            'final_params': {},
            'final_score': 0,
            'final_calmar': 0,
            'final_drawdown': 0,
            'final_trades': 0,
            'final_return': 0,
            'error': str(e)
        }



def run_two_phase_optimization(config: dict) -> Dict:
    """
    两阶段优化模式：合并贝叶斯优化 + WF微调

    Phase 1 (贝叶斯优化):
    - 数据范围：T-1Y ~ T-3M（样本内）
    - 使用 Optuna 贝叶斯优化寻找初始最佳参数
    - 优化目标：最大化复合分数

    Phase 2 (WF微调):
    - 数据范围：T-3M ~ T（样本外验证）
    - 在WF窗口上验证 Phase 1 参数
    - 如果验证有效，执行局部微调

    参数:
        config: 配置字典

    返回:
        最佳参数字典（含两阶段结果）
    """
    logger.info("=" * 70)
    logger.info("两阶段优化：贝叶斯优化 + WF微调")
    logger.info("=" * 70)

    # 创建 WF 窗口
    wf_window = WalkForwardWindow(datetime.now())

    # 获取日期范围
    ins_start, ins_end = wf_window.get_ins_sample_period()
    oos_start, oos_end = wf_window.get_oos_sample_period()

    logger.info(f"Phase 1 (贝叶斯优化): {ins_start} 至 {ins_end}")
    logger.info(f"Phase 2 (WF微调): {oos_start} 至 {oos_end}")

    stocks = config.get('stocks', [])
#     grid_cfg = config.get('grid', {})
    backtest_cfg = config.get('backtest', {})
    paths_cfg = config.get('paths', {})
    capital_cfg = config.get('capital', {})

    # === 获取市场状态（RegimeFilter）===
    from risk_management.market_regime import RegimeFilter
    from utils.utils import load_state, save_state
    benchmark_code = config.get('regime_filter', {}).get('benchmark_index', '000300.SH')
    benchmark_df = get_stock_data(benchmark_code, data_dir=paths_cfg.get('data_dir', './data'),
                                  enable_incremental=False, use_cache=True, fallback_to_cache=True)

    regime_filter = RegimeFilter(config)

    # 加载持久化的 RegimeFilter 状态
    app_state = load_state()
    regime_saved = app_state.get('regime_filter_state', {})
    regime_filter.from_dict(regime_saved)

    if not benchmark_df.empty:
        if 'date' in benchmark_df.columns and not isinstance(benchmark_df.index, pd.DatetimeIndex):
            benchmark_df = benchmark_df.set_index('date')
        benchmark_data = {
            'close': benchmark_df['close'],
            'high': benchmark_df.get('high', benchmark_df['close']),
            'low': benchmark_df.get('low', benchmark_df['close']),
            'volume': benchmark_df.get('volume', pd.Series())
        }

        # Phase1 使用 ins_end 日期的历史状态（回测期结束时的市场状态）
        ins_end_dt = pd.to_datetime(ins_end)
        regime_result_p1 = regime_filter.check_historical(benchmark_data, as_of_date=ins_end_dt)
        regime_state_phase1 = regime_result_p1['state']

        # Phase2 使用当前日期的市场状态（实时 check，更新内部状态）
        regime_result_p2 = regime_filter.check(benchmark_data)
        regime_state_phase2 = regime_result_p2['state']
        regime_params = regime_result_p2['params']

        logger.info(f"Phase1 市场状态({ins_end}): {regime_state_phase1}")
        logger.info(f"Phase2 市场状态({oos_end}): {regime_state_phase2}, "
                    f"spacing={regime_params['grid_spacing_multiplier']:.1f}x, max_grids={regime_params['max_grids']}")

        # 保存更新后的 RegimeFilter 状态
        app_state['regime_filter_state'] = regime_filter.to_dict()
        save_state(app_state)
    else:
        regime_state_phase1 = 'normal'
        regime_state_phase2 = 'normal'
        regime_params = {'grid_spacing_multiplier': 1.0, 'max_grids': 9, 'initial_position': 0.45}
        logger.warning("无法获取基准指数数据，使用默认市场状态")

    logger.info("=" * 70)

    if not stocks:
        logger.error("配置文件中未指定股票列表")
        return {}

    # 资金分配逻辑
    total_cash = capital_cfg.get('total', 100000)
    max_position_pct = capital_cfg.get('max_position_per_stock', 0.30)
    cash_reserve_ratio = capital_cfg.get('cash_reserve_ratio', 0.40)
    initial_position = capital_cfg.get('initial_position', 0.45)

    investable_cash = total_cash * (1 - cash_reserve_ratio)
    max_stocks_for_trading = max(1, int(investable_cash / (total_cash * max_position_pct)))
    # 所有选出的股票都参与优化（用于参数研究），但实际资金只投入 max_stocks_for_trading 只
    stocks_to_optimize = len(stocks)
    allocated_cash = investable_cash / max_stocks_for_trading if max_stocks_for_trading > 0 else investable_cash

    logger.info(f"优化范围: {stocks_to_optimize} 只股票参与优化, "
                f"实际投入资金: {max_stocks_for_trading} 只")

    grid_pool = allocated_cash * (1 - initial_position)

    logger.info(f"资金配置: 总资金={total_cash}元, 每股分配={allocated_cash:.0f}元, "
                f"网格池={grid_pool:.0f}元 ({(1-initial_position)*100:.0f}%)")

    # 搜索空间
    search_space = build_adaptive_search_space(allocated_cash, initial_position, config=config)
    logger.info(f"Phase 1 搜索空间: grid_amount={search_space['grid_amount_range']}, "
                f"max_grids={search_space['max_grids_range']}, "
                f"buy_spacing={search_space['buy_spacing_range']}, "
                f"sell_spacing={search_space['sell_spacing_range']}")

    # 并行优化配置
    import time
    import pickle

    parallel_cfg = config.get('parallel_optimization', {})
    parallel_enabled = parallel_cfg.get('enabled', True)
    max_workers = parallel_cfg.get('max_workers', stocks_to_optimize) or stocks_to_optimize

    all_results = []

    # 获取并行化前的起始时间
    optimization_start_time = time.time()

    if parallel_enabled and stocks_to_optimize > 1:
        # ===== 并行执行路径 =====
        logger.info(f"\n{'='*70}")
        logger.info(f"启动并行优化: {stocks_to_optimize} 只股票, {max_workers} 个 worker")
        logger.info(f"{'='*70}")

        # 准备每个 stock 的数据切片（在主线程完成，避免竞态）
        stock_data_map = {}
        valid_stocks = []
        for code in stocks:
            df = get_stock_data(code, data_dir=paths_cfg['data_dir'], selected_stocks=stocks,
                               enable_incremental=False, use_cache=True, fallback_to_cache=True)
            if df.empty or len(df) < 250:
                logger.warning(f"{code} 数据不足，跳过")
                continue
            df = clean_data(df)
            df_ins = wf_window.slice_dataframe_by_period(df, period='ins')
            df_oos = wf_window.slice_dataframe_by_period(df, period='oos')
            if len(df_ins) < 60:
                logger.warning(f"{code} Phase1 数据不足 ({len(df_ins)}天)，跳过")
                continue

            stock_data_map[code] = {
                'ins_bytes': pickle.dumps(df_ins),
                'oos_bytes': pickle.dumps(df_oos),
                'full_df_bytes': pickle.dumps(df),
            }
            valid_stocks.append(code)

        n_valid = len(valid_stocks)
        if n_valid == 0:
            logger.error("没有有效股票可优化")
            return {}

        actual_workers = min(max_workers, n_valid)
        logger.info(f"有效股票 {n_valid} 只，启动 {actual_workers} 个 worker")

        # 提交所有任务（ProcessPoolExecutor 真正多核并行）
        with ProcessPoolExecutor(max_workers=actual_workers) as executor:
            futures = {}
            for code in valid_stocks:
                data = stock_data_map[code]
                future = executor.submit(
                    _optimize_single_stock_worker,
                    code,
                    config,
                    allocated_cash,
                    search_space,
                    data['ins_bytes'],
                    data['oos_bytes'],
                    ins_start,
                    ins_end,
                    oos_start,
                    oos_end,
                    'normal',
                    regime_params,
                    regime_state_phase1,
                    regime_state_phase2
                )
                futures[future] = code

            # 收集结果（按完成顺序）
            n_completed = 0
            for future in as_completed(futures):
                code = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    logger.error(f"Worker {code} 异常: {e}")
                    result = {'code': code, 'error': str(e)}

                all_results.append(result)
                n_completed += 1
                logger.info(f"优化进度: {n_completed}/{n_valid} ({code})")

        optimization_elapsed = time.time() - optimization_start_time
        logger.info(f"\n并行优化完成，耗时: {optimization_elapsed:.1f} 秒")
        logger.info(f"平均每只股票: {optimization_elapsed/n_valid:.1f} 秒")

        # 全量历史回测：用最终参数在完整数据上回测，生成完整净值曲线
        logger.info("生成全量历史净值曲线...")
        for r in all_results:
            code = r.get('code', '')
            if r.get('error') or code not in stock_data_map:
                continue
            full_df = pickle.loads(stock_data_map[code]['full_df_bytes'])
            final_params = r.get('final_params', {})
            if not final_params:
                continue
            result_full = backtest_grid_strategy(
                full_df,
                buy_spacing_pct=final_params['buy_spacing_pct'],
                sell_spacing_pct=final_params['sell_spacing_pct'],
                grid_amount=final_params['grid_amount'],
                amount_multiplier=final_params['amount_multiplier'],
                initial_position=final_params['initial_position'],
                max_grids=final_params['max_grids'],
                spacing_decay=final_params['spacing_decay'],
                commission_rate=backtest_cfg.get('commission_rate', 0.00015),
                stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
                slippage_rate=backtest_cfg.get('slippage_rate', 0.001),
                initial_cash=allocated_cash
            )
            r['full_portfolio_values'] = result_full.get('portfolio_values', [])
            r['full_initial_value'] = allocated_cash
            r['full_trades_detail'] = result_full.get('trades', [])
            r['full_benchmark_values'] = result_full.get('benchmark_values', [])
            r['full_benchmark_return'] = result_full.get('benchmark_return', 0)
            r['full_data_start'] = str(full_df['date'].iloc[0])[:10]
            r['full_data_end'] = str(full_df['date'].iloc[-1])[:10]
            logger.info(f"  {code}: 全量回测 {len(full_df)}天, 净值点数={len(result_full.get('portfolio_values', []))}")

    else:
        # ===== 串行执行路径 (回退) — 复用 worker 函数 =====
        logger.info(f"\n{'='*70}")
        logger.info(f"串行优化模式: {stocks_to_optimize} 只股票")
        logger.info(f"{'='*70}")

        full_data_map = {}
        for code in stocks:
            logger.info(f"\n{'='*70}")
            logger.info(f"处理股票：{code}")
            logger.info(f"{'='*70}")

            df = get_stock_data(code, data_dir=paths_cfg['data_dir'], selected_stocks=stocks,
                               enable_incremental=False, use_cache=True, fallback_to_cache=True)
            if df.empty or len(df) < 250:
                logger.warning(f"{code} 数据不足，跳过")
                continue
            df = clean_data(df)
            df_ins = wf_window.slice_dataframe_by_period(df, period='ins')
            df_oos = wf_window.slice_dataframe_by_period(df, period='oos')
            if len(df_ins) < 60:
                logger.warning(f"{code} Phase1 数据不足 ({len(df_ins)}天)，跳过")
                continue
            full_data_map[code] = df

            ins_bytes = pickle.dumps(df_ins)
            oos_bytes = pickle.dumps(df_oos)

            result = _optimize_single_stock_worker(
                code, config, allocated_cash, search_space,
                ins_bytes, oos_bytes,
                ins_start, ins_end, oos_start, oos_end,
                'normal', regime_params,
                regime_state_phase1, regime_state_phase2
            )
            all_results.append(result)

        optimization_elapsed = time.time() - optimization_start_time
        logger.info(f"\n串行优化完成，耗时: {optimization_elapsed:.1f} 秒")

        # 全量历史回测：用最终参数在完整数据上回测，生成完整净值曲线
        logger.info("生成全量历史净值曲线...")
        for r in all_results:
            code = r.get('code', '')
            if r.get('error') or code not in full_data_map:
                continue
            full_df = full_data_map[code]
            final_params = r.get('final_params', {})
            if not final_params:
                continue
            result_full = backtest_grid_strategy(
                full_df,
                buy_spacing_pct=final_params['buy_spacing_pct'],
                sell_spacing_pct=final_params['sell_spacing_pct'],
                grid_amount=final_params['grid_amount'],
                amount_multiplier=final_params['amount_multiplier'],
                initial_position=final_params['initial_position'],
                max_grids=final_params['max_grids'],
                spacing_decay=final_params['spacing_decay'],
                commission_rate=backtest_cfg.get('commission_rate', 0.00015),
                stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
                slippage_rate=backtest_cfg.get('slippage_rate', 0.001),
                initial_cash=allocated_cash
            )
            r['full_portfolio_values'] = result_full.get('portfolio_values', [])
            r['full_initial_value'] = allocated_cash
            r['full_trades_detail'] = result_full.get('trades', [])
            r['full_benchmark_values'] = result_full.get('benchmark_values', [])
            r['full_benchmark_return'] = result_full.get('benchmark_return', 0)
            r['full_data_start'] = str(full_df['date'].iloc[0])[:10]
            r['full_data_end'] = str(full_df['date'].iloc[-1])[:10]
            logger.info(f"  {code}: 全量回测 {len(full_df)}天, 净值点数={len(result_full.get('portfolio_values', []))}")

    # 打印数据源健康报告（验证限流效果）
    try:
        from data_layer.fetcher import get_source_manager
        get_source_manager().print_health_report()
    except Exception:
        pass

    # 过滤掉有错误的股票
    valid_results = [r for r in all_results if not r.get('error')]
    failed_results = [r for r in all_results if r.get('error')]
    if failed_results:
        logger.warning(f"以下股票优化失败: {[r['code'] for r in failed_results]}")

    # 保存优化结果
    output_dir = paths_cfg['output_dir']
    report_file = os.path.join(output_dir, 'report.json')

    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump({
            'optimization_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'optimization_type': 'two_phase',
            'phase1_period': f'{ins_start} 至 {ins_end}',
            'phase2_period': f'{oos_start} 至 {oos_end}',
            'capital_allocation': {
                'total': total_cash,
                'allocated_per_stock': allocated_cash,
                'grid_pool_per_stock': grid_pool,
                'num_stocks': max_stocks_for_trading
            },
            'results': valid_results
        }, f, ensure_ascii=False, indent=2)

    logger.info(f"\n优化报告已保存到：{report_file}")

    # 选择实盘交易股票
    trading_stocks_count = max(1, min(len(valid_results), max_stocks_for_trading))
    df_results = pd.DataFrame(valid_results)
    df_results = df_results.sort_values('final_return', ascending=False)
    trading_stocks = df_results.head(trading_stocks_count)['code'].tolist()

    logger.info("\n实盘交易股票（按收益排序）:")
    for i, code in enumerate(trading_stocks):
        row = df_results[df_results['code'] == code].iloc[0]
        logger.info(f"  {i+1}. {code} - 最终收益: {row['final_return']*100:.2f}%, "
                    f"Calmar: {row['final_calmar']:.4f}")

    # 保存实盘股票到 config_state.json
    from utils import load_state, save_state
    state = load_state()
    state['trading_stocks'] = trading_stocks
    state['optimization_history'] = {
        r['code']: {
            'final_params': r['final_params'],
            'final_return': r['final_return'],
            'phase1_params': r['phase1_params']
        } for r in all_results
    }
    save_state(state)

    logger.info("实盘股票已保存到 config_state.json")

    return all_results


def calculate_realized_volatility(prices: pd.Series, period: int = 30) -> float:
    """
    计算实际波动率（Realized Volatility）
    
    公式：收盘价对数收益率的标准差 × √252
    
    参数:
        prices: 收盘价序列
        period: 计算周期（默认 30 个交易日）
    
    返回:
        年化波动率（%）
    """
    if len(prices) < period + 1:
        logger.warning(f"数据长度不足 {period+1}，波动率计算可能不准确")
        period = max(5, len(prices) - 1)
    
    # 取最近 period+1 个价格
    recent_prices = prices.tail(period + 1)
    
    # 计算对数收益率
    log_returns = np.log(recent_prices / recent_prices.shift(1))
    
    # 去除 NaN
    log_returns = log_returns.dropna()
    
    if len(log_returns) < 2:
        return 0.0
    
    # 计算标准差并年化
    realized_vol = log_returns.std() * np.sqrt(252)
    
    return realized_vol


def load_optimization_history(config: dict) -> Dict:
    """
    加载历史优化结果（用于获取参考波动率）
    
    参数:
        config: 配置字典
    
    返回:
        优化历史字典 {code: {optimization_date, best_params, ins_volatility, ...}}
    """
    from utils import load_state
    
    # 尝试从状态文件加载
    state = load_state()
    
    optimization_history = state.get('optimization_history', {})
    
    return optimization_history


def adjust_grid_spacing(current_vol: float, reference_vol: float, 
                        base_spacing: float, config: dict) -> Tuple[float, str]:
    """
    动态调整网格间距
    
    规则:
    - 若 当前波动率 > 参考波动率 × 1.5 → 扩大 20%
    - 若 当前波动率 < 参考波动率 × 0.5 → 缩小 20%
    - 其他情况保持不变
    
    参数:
        current_vol: 当前波动率（最近 30 日）
        reference_vol: 参考波动率（优化期历史值）
        base_spacing: 基础网格间距
        config: 配置字典
    
    返回:
        (adjusted_spacing, param_source): 调整后的间距，参数来源
    """
    risk_control_cfg = config.get('risk_control', {})
    vol_adjustment_enabled = risk_control_cfg.get('vol_adjustment_enabled', True)
    
    if not vol_adjustment_enabled or reference_vol <= 0:
        logger.info("使用原始优化参数（波动率调整：禁用或参考值为 0）")
        return base_spacing, "optimized"
    
    # 计算波动率比率
    vol_ratio = current_vol / reference_vol
    
    logger.info(f"波动率对比：当前={current_vol*100:.2f}%, 参考={reference_vol*100:.2f}%, 比率={vol_ratio:.2f}")
    
    adjusted_spacing = base_spacing
    param_source = "optimized"
    
    # 高波动率场景：扩大网格间距
    if vol_ratio > 1.5:
        adjusted_spacing = base_spacing * 1.2  # 扩大 20%
        param_source = "adjusted"
        logger.info(
            f"📈 高波动率检测：当前波动率是参考值的 {vol_ratio:.2f}倍 "
            f"→ 网格间距扩大 20% ({base_spacing:.2f}% → {adjusted_spacing:.2f}%)"
        )
    
    # 低波动率场景：缩小网格间距
    elif vol_ratio < 0.5:
        adjusted_spacing = base_spacing * 0.8  # 缩小 20%
        param_source = "adjusted"
        logger.info(
            f"📉 低波动率检测：当前波动率是参考值的 {vol_ratio:.2f}倍 "
            f"→ 网格间距缩小 20% ({base_spacing:.2f}% → {adjusted_spacing:.2f}%)"
        )
    
    else:
        logger.info(f"✓ 波动率正常，使用原始优化参数 ({base_spacing:.2f}%)")
    
    return adjusted_spacing, param_source


def generate_signals(config: dict) -> pd.DataFrame:
    """
    信号模式：生成次日交易计划（增强版）
    
    新增功能:
    1. 动态网格参数调整（基于实时波动率 vs 历史波动率）
    2. 实盘熔断风控检查（暂停买入/全局停止）
    3. 输出包含 strategy_version 和 param_source
    
    参数:
        config: 配置字典
    
    返回:
        交易信号 DataFrame
    """
    logger.info("=" * 70)
    logger.info("开始生成交易信号...")
    logger.info("=" * 70)
    
    stocks = config.get('stocks', [])
    grid_cfg = config.get('grid', {})
#     risk_cfg = config.get('risk', {})
    paths_cfg = config.get('paths', {})
    
    # === 步骤 1: 加载历史优化数据（用于波动率对比） ===
    logger.info("\n加载历史优化数据...")
    opt_history = load_optimization_history(config)
    
    # === 步骤 2: 初始化风控管理器 ===
#     from risk_management.circuit_breaker import RiskControlManager, create_risk_control_manager
    
    risk_manager = create_risk_control_manager(config)
    
    # === 步骤 3: 构建账户状态（用于风控检查） ===
    # 注意：这里需要从配置文件或状态文件读取持仓信息
    # 简化处理：假设持仓信息存储在 state 中
    from utils import load_state
    state = load_state()
    positions_data = state.get('positions', [])  # 格式：[{code, cost_price, quantity}, ...]
    
    # 获取最新价格（简化处理，实际应从行情数据获取）
    positions_with_price = []
    for pos in positions_data:
        code = pos['code']
        try:
            df = get_stock_data(code, data_dir=paths_cfg['data_dir'],
                                selected_stocks=stocks)
            if not df.empty:
                current_price = df.iloc[-1]['close']
                positions_with_price.append({
                    'code': code,
                    'cost_price': pos['cost_price'],
                    'current_price': current_price,
                    'quantity': pos['quantity']
                })
        except Exception as e:
            logger.warning(f"获取 {code} 最新价格失败：{str(e)}")
    
    # 构建账户状态
    account_status = risk_manager.get_account_status(
        positions_with_price,
        cash=state.get('cash', 500000)  # 默认现金 50 万
    )
    
    logger.info("\n账户状态:")
    logger.info(f"  总市值：{account_status.total_value:,.2f}")
    logger.info(f"  历史峰值：{account_status.peak_value:,.2f}")
    logger.info(f"  当前回撤：{account_status.drawdown*100:.2f}%")
    logger.info(f"  持仓数量：{len(account_status.positions)}")
    
    # === 步骤 4: 执行熔断检查 ===
    logger.info("\n执行实盘熔断风控检查...")
    circuit_breaker_state = risk_manager.check_circuit_breaker(account_status)
    
    if circuit_breaker_state.is_global_breaker:
        logger.error("🚨 全局熔断已触发！仅生成卖出信号，暂停所有买入")

    # === 步骤 4.5: 市场状态检查 ===
    from risk_management.market_regime import RegimeFilter
    from utils.utils import load_state, save_state
    benchmark_code = config.get('regime_filter', {}).get('benchmark_index', '000300.SH')
    benchmark_df = get_stock_data(benchmark_code, data_dir=paths_cfg.get('data_dir', './data'))

    # 加载持久化的 RegimeFilter 状态
    regime_saved = state.get('regime_filter_state', {})

    if not benchmark_df.empty:
        if 'date' in benchmark_df.columns and not isinstance(benchmark_df.index, pd.DatetimeIndex):
            benchmark_df = benchmark_df.set_index('date')
        benchmark_data = {
            'close': benchmark_df['close'],
            'high': benchmark_df.get('high', benchmark_df['close']),
            'low': benchmark_df.get('low', benchmark_df['close']),
            'volume': benchmark_df.get('volume', pd.Series())
        }
        regime_filter = RegimeFilter(config)
        regime_filter.from_dict(regime_saved)
        regime_result = regime_filter.check(benchmark_data)
        regime_can_buy = regime_result['can_buy']
        logger.info(f"市场状态: {regime_result['log_msg']}")

        # 保存更新后的 RegimeFilter 状态
        state['regime_filter_state'] = regime_filter.to_dict()
        from utils.utils import save_state
        save_state(state)
    else:
        regime_can_buy = True
        logger.warning("无法获取基准指数数据，市场状态检查跳过")

    # === 步骤 5: 确定实盘交易股票 ===
    # 从 config_state.json 读取 trading_stocks
    trading_stocks = state.get('trading_stocks', [])

    if trading_stocks:
        # 优先使用配置的实盘股票
        signal_stocks = trading_stocks
        logger.info(f"使用实盘交易股票：{signal_stocks}")
    else:
        # 如果没有配置，回退到使用全部 top_n 股票
        signal_stocks = stocks
        logger.info(f"未配置实盘股票，使用候选股票池：{signal_stocks}")

    # === 步骤 5: 并行获取交易股票数据 ===
    logger.info(f"\n并行获取 {len(signal_stocks)} 只股票数据...")
    stock_data = {}
    fetch_tasks = [
        (code, paths_cfg['data_dir'], False, config)
        for code in signal_stocks
    ]
    with ThreadPoolExecutor(max_workers=min(len(signal_stocks), 5)) as executor:
        futures = {executor.submit(_fetch_single_stock_data, task): task[0] for task in fetch_tasks}
        for future in as_completed(futures):
            code = futures[future]
            try:
                _, df, status = future.result()
                if status == "success" and not df.empty:
                    stock_data[code] = df
                else:
                    logger.warning(f"{code} 数据获取失败: {status}")
            except Exception as e:
                logger.warning(f"{code} 数据获取异常: {e}")

    # === 步骤 6: 生成交易信号 ===
    signals = []
    version = config.get('version', '2.0.0')

    for code in signal_stocks:
        logger.info(f"\n{'-'*60}")
        logger.info(f"处理股票：{code}")
        logger.info(f"{'-'*60}")

        # 检查是否允许买入该股（熔断 + 市场状态双重检查）
        allow_buy = risk_manager.should_allow_buy(code) and regime_can_buy

        if not allow_buy:
            reason = '熔断' if not risk_manager.should_allow_buy(code) else '市场状态限制'
            logger.warning(f"⚠️  {code} 不允许买入: {reason}")

        df = stock_data.get(code, pd.DataFrame())

        if df.empty:
            logger.warning(f"{code} 无数据，跳过")
            continue
        
        # 清洗数据
        df = clean_data(df)
        
        # 获取最新行情
        # 关键修正：在 T 日开盘前生成信号时，只能使用 T-1 日及之前的数据
        # iloc[-1] = T 日数据（未来数据，不可用）
        # iloc[-2] = T-1 日数据（可用）
        if len(df) >= 2:
            prev_close = df.iloc[-2]['close']  # T-1 日收盘价
            current_price = prev_close  # 使用 T-1 日收盘价作为基准
            logger.debug(f"使用 T-1 日收盘价：{current_price:.2f}")
        else:
            current_price = df.iloc[-1]['close']
            prev_close = current_price
            logger.warning(f"数据长度不足，使用最新价格：{current_price:.2f}")
        
        # === 动态参数调整核心逻辑 ===
        
        # 1. 计算当前波动率（最近 30 日）
        current_vol = calculate_realized_volatility(df['close'], period=30)
        logger.info(f"当前波动率（近 30 日）：{current_vol*100:.2f}%")
        
        # 2. 获取参考波动率（从历史优化结果）
        reference_vol = 0.0
        base_spacing = grid_cfg.get('base_spacing', 2.0)
        
        best_params = {}
        if code in opt_history:
            hist_data = opt_history[code]
            logger.info(f"找到历史优化记录：{hist_data.get('optimization_date', 'N/A')}")

            reference_vol = hist_data.get('ins_volatility', 0.0)
            if reference_vol <= 0:
                logger.info("历史记录中无波动率数据，使用当前波动率的 80% 作为参考")
                reference_vol = current_vol * 0.8

            # 获取优化参数（兼容 load_optimization_history 的 best_params 键名）
            best_params = hist_data.get('best_params', {})
            if 'buy_spacing_pct' in best_params:
                base_spacing = best_params['buy_spacing_pct']
                logger.info(f"使用优化最佳参数：buy_spacing={base_spacing*100:.2f}%, "
                          f"sell_spacing={best_params.get('sell_spacing_pct', base_spacing)*100:.2f}%")
            elif 'grid_spacing' in best_params:
                base_spacing = best_params['grid_spacing'] / 100.0  # 旧格式是百分比数字
                logger.info(f"使用旧版优化参数：grid_spacing={base_spacing*100:.2f}%")
        else:
            logger.info(f"未找到 {code} 的历史优化记录，使用配置文件默认参数")
        
        # 3. 动态调整网格间距
        adjusted_spacing, param_source = adjust_grid_spacing(
            current_vol, reference_vol, base_spacing, config
        )
        
        logger.info(f"参数来源：{param_source}")
        logger.info(f"最终网格间距：{adjusted_spacing:.2f}%")
        
        # === 传统 ATR 动态调整（保留原有逻辑） ===
        # 注意：计算 ATR 需要使用完整的历史数据（包含 high, low, close）
        # 在实盘场景（T 日开盘前），我们只能获得 T-1 日及之前的数据
        # 因此使用 .iloc[-2] 获取 T-1 日的 ATR，避免前视偏差
        df['atr'] = calculate_atr(df, grid_cfg.get('atr_period', 20))
        
        # 关键修正：使用 T-1 日的 ATR 值（而非 T 日）
        # iloc[-1] = T 日（未来数据，不可用）
        # iloc[-2] = T-1 日（历史数据，可用）
        if len(df) >= 2:
            current_atr = df['atr'].iloc[-2]  # T-1 日 ATR
            logger.debug(f"使用 T-1 日 ATR: {current_atr:.2f}")
        else:
            current_atr = df['atr'].iloc[-1]
            logger.warning(f"数据长度不足，使用最新 ATR: {current_atr:.2f}")
        
        # 波动率计算同样使用 T-1 日及之前的数据
        # calculate_realized_volatility 内部已正确处理（使用 shift(1)）
        current_vol = calculate_realized_volatility(df['close'].iloc[:-1], period=30)  # 排除 T 日
        logger.info(f"当前波动率（近 30 日，T-1 数据）：{current_vol*100:.2f}%")
        
        # === 使用 DynamicGridEngine 生成7参数网格信号 ===

        # 账户状态
        cash = state.get('cash', 500000)
        current_position = sum(
            p.get('quantity', 0) for p in state.get('positions', {}).get(code, [])
        )
        available_position = state.get(f'{code}_available', current_position)

        # 从优化结果提取7参数（回退到配置默认值）
        opt_buy_sp = best_params.get('buy_spacing_pct', grid_cfg.get('base_spacing', 0.02))
        opt_sell_sp = best_params.get('sell_spacing_pct', opt_buy_sp)
        opt_mult = best_params.get('amount_multiplier', 1.0)
        opt_decay = best_params.get('spacing_decay', 1.0)
        opt_max_grids = best_params.get('max_grids', grid_cfg.get('max_grids', 10))
        opt_grid_amount = best_params.get('grid_amount', grid_cfg.get('grid_amount', 10000))

        engine = DynamicGridEngine(config)
        grid_signals = engine.generate_signals(
            code=code,
            ref_price=current_price,
            atr_20=current_atr,
            volatility_60d=current_vol,
            available_position=available_position,
            current_position=current_position,
            cash=cash,
            grid_amount=opt_grid_amount,
            amount_multiplier=opt_mult,
            prev_close=prev_close,
            can_buy=allow_buy,
            can_open_new=True,
            buy_spacing_pct=opt_buy_sp,
            sell_spacing_pct=opt_sell_sp,
            spacing_decay=opt_decay,
            max_grids=opt_max_grids,
        )

        # 转换 GridSignal 对象为下游期望的 dict 格式
        for gs in grid_signals:
            signals.append({
                'code': gs.code,
                'direction': gs.direction,
                'price': gs.price,
                'quantity': gs.quantity,
                'amount': round(gs.quantity * gs.price, 2),
                'reason': gs.reason,
                'valid_date': get_next_trading_day(datetime.now()).strftime('%Y-%m-%d'),
                'priority': gs.grid_level,
                'strategy_version': version,
                'param_source': param_source,
                'signal_type': gs.signal_type,
            })
    
    # === 步骤 6: 转换为 DataFrame 并排序 ===
    if not signals:
        logger.warning("未生成任何交易信号")
        return pd.DataFrame()
    
    df_signals = pd.DataFrame(signals)
    
    # 排序（按代码、优先级）
    df_signals = df_signals.sort_values(['code', 'priority', 'direction'])
    
    # === 步骤 7: 风控过滤（移除被禁止的买入信号） ===
    from risk_management.circuit_breaker import filter_signals_by_risk
    df_signals = filter_signals_by_risk(df_signals, circuit_breaker_state, risk_manager)
    
    # === 步骤 8: 输出信号 ===
    output_dir = paths_cfg['output_dir']
    os.makedirs(output_dir, exist_ok=True)
    signal_file = os.path.join(output_dir, config['paths'].get('signal_file', 'signals.csv'))
    
    # 选择显示的列（包含新增字段）
    display_cols = ['code', 'direction', 'price', 'quantity', 'amount', 'reason', 
                    'valid_date', 'strategy_version', 'param_source']
    
    # 确保所有列都存在
    for col in display_cols:
        if col not in df_signals.columns:
            df_signals[col] = ''
    
    # 保存到 CSV
    df_signals[display_cols].to_csv(signal_file, index=False, encoding='utf-8-sig')
    
    logger.info("\n" + "=" * 70)
    logger.info("交易信号汇总:")
    logger.info("=" * 70)
    
    for code in df_signals['code'].unique():
        df_code = df_signals[df_signals['code'] == code]
        buy_count = len(df_code[df_code['direction'] == 'buy'])
        sell_count = len(df_code[df_code['direction'] == 'sell'])
        filtered_count = len(df_code[df_code['filtered']]) if 'filtered' in df_code.columns and df_code['filtered'].any() else 0
        
        logger.info(f"{code}: {buy_count}个买入信号，{sell_count}个卖出信号，{filtered_count}个被风控过滤")
    
    logger.info(f"\n交易信号已保存到：{signal_file}")
    
    # 打印前几条信号
    logger.info("\n信号预览 (Top 10):")
    print(df_signals[display_cols].head(10).to_string(index=False))
    
    return df_signals


# ==================== Walk-Forward 时间窗口管理 ====================

class WalkForwardWindow:
    """
    Walk-Forward 时间窗口管理器
    
    严格定义三个时间段（假设当前日期为 T）：
    1. 选股池构建期 (Universe Selection): T - 1.5 年 至 T - 3 个月
    2. 样本内优化期 (In-Sample): T - 1 年 至 T - 3 个月
    3. 样本外验证期 (Out-of-Sample): T - 3 个月 至 T
    
    关键规则：
    - 禁止使用未来数据（no forward-looking bias）
    - 所有数据切片必须明确标注时间范围
    - 支持滚动窗口执行（--rolling 参数）
    """
    
    def __init__(self, current_date: datetime = None):
        """
        初始化 Walk-Forward 窗口

        参数:
            current_date: 当前日期 T（默认使用今天，对齐到真实交易日）
        """
        # 关键修正：current_date 必须对齐到真实交易日
        raw_date = current_date or datetime.now()
        self.current_date = align_to_trading_day(raw_date, direction='backward')

        # === 计算各时间窗口边界 ===
        # 使用 pandas 的 DateOffset 确保正确处理非交易日
        # 然后对齐到真实交易日

        # T - 6 个月（样本内结束/样本外开始）
        oos_start_raw = self.current_date - pd.DateOffset(months=6)
        self.oos_start = align_to_trading_day(oos_start_raw, direction='backward')

        # T - 3 年（样本内开始）
        ins_start_raw = self.current_date - pd.DateOffset(years=3)
        self.ins_start = align_to_trading_day(ins_start_raw, direction='backward')

        # T - 4 年（选股池构建开始）
        universe_start_raw = self.current_date - pd.DateOffset(years=4)
        self.universe_start = align_to_trading_day(universe_start_raw, direction='backward')

        # 格式化日期字符串（用于日志和数据过滤）
        self.current_date_str = self.current_date.strftime('%Y-%m-%d')
        self.oos_start_str = self.oos_start.strftime('%Y-%m-%d')
        self.ins_start_str = self.ins_start.strftime('%Y-%m-%d')
        self.universe_start_str = self.universe_start.strftime('%Y-%m-%d')
        
        logger.info("=" * 70)
        logger.info("Walk-Forward 时间窗口配置")
        logger.info("=" * 70)
        logger.info(f"当前日期 (T): {self.current_date_str}")
        logger.info(f"选股池构建期：{self.universe_start_str} 至 {self.oos_start_str} (T-4Y ~ T-6M)")
        logger.info(f"样本内优化期：{self.ins_start_str} 至 {self.oos_start_str} (T-3Y ~ T-6M)")
        logger.info(f"样本外验证期：{self.oos_start_str} 至 {self.current_date_str} (T-6M ~ T)")
        logger.info("=" * 70)
    
    def get_universe_period(self) -> Tuple[str, str]:
        """
        获取选股池构建期的日期范围
        
        返回:
            (start_date, end_date) 格式：'YYYY-MM-DD'
        """
        return (self.universe_start_str, self.oos_start_str)
    
    def get_ins_sample_period(self) -> Tuple[str, str]:
        """
        获取样本内优化期的日期范围
        
        返回:
            (start_date, end_date) 格式：'YYYY-MM-DD'
        """
        return (self.ins_start_str, self.oos_start_str)
    
    def get_oos_sample_period(self) -> Tuple[str, str]:
        """
        获取样本外验证期的日期范围
        
        返回:
            (start_date, end_date) 格式：'YYYY-MM-DD'
        """
        return (self.oos_start_str, self.current_date_str)
    
    def slice_dataframe_by_period(self, df: pd.DataFrame, period: str) -> pd.DataFrame:
        """
        根据时期名称切割 DataFrame
        
        参数:
            df: 包含 'date' 列的 DataFrame
            period: 时期名称 ('universe', 'ins', 'oos')
        
        返回:
            切割后的 DataFrame
        
        注意:
            - 所有日期比较均使用左闭右开区间 [start, end)
            - 确保无未来数据泄露
        """
        if df.empty or 'date' not in df.columns:
            return pd.DataFrame()
        
        # 确保 date 列为 datetime 类型
        if not pd.api.types.is_datetime64_any_dtype(df['date']):
            df = df.copy()
            df['date'] = pd.to_datetime(df['date'])
        
        if period == 'universe':
            # 选股池构建期：T-1.5Y 至 T-3M [universe_start, oos_start)
            start, end = self.universe_start_str, self.oos_start_str
            mask = (df['date'] >= start) & (df['date'] < end)
            
        elif period == 'ins':
            # 样本内优化期：T-1Y 至 T-3M [ins_start, oos_start)
            start, end = self.ins_start_str, self.oos_start_str
            mask = (df['date'] >= start) & (df['date'] < end)
            
        elif period == 'oos':
            # 样本外验证期：T-3M 至 T [oos_start, current_date]
            start, end = self.oos_start_str, self.current_date_str
            mask = (df['date'] >= start) & (df['date'] <= end)
            
        else:
            raise ValueError(f"未知时期名称：{period}. 可选值：'universe', 'ins', 'oos'")
        
        result = df[mask].copy().reset_index(drop=True)
        
        logger.debug(f"DataFrame 切片 [{period}]: {start} 至 {end}, 获取 {len(result)} 条记录")
        
        return result
    
    def roll_forward(self, period: str = '1m') -> 'WalkForwardWindow':
        """
        向前滚动时间窗口
        
        参数:
            period: 滚动周期，如 '1m' (1 个月), '1q' (1 季度), '1w' (1 周)
        
        返回:
            新的 WalkForwardWindow 实例（滚动后的日期）
        """
        # 解析滚动周期
        import re
        match = re.match(r'(\d+)([dwqm])', period.lower())
        if not match:
            raise ValueError(f"无效的滚动周期格式：{period}. 示例：'1m', '3m', '1q', '1w'")
        
        num, unit = int(match.group(1)), match.group(2)
        
        # 计算新的当前日期
        if unit == 'd':
            new_current = self.current_date + timedelta(days=num)
        elif unit == 'w':
            new_current = self.current_date + timedelta(weeks=num)
        elif unit == 'm':
            new_current = self.current_date + pd.DateOffset(months=num)
        elif unit == 'q':
            new_current = self.current_date + pd.DateOffset(months=num * 3)
        else:
            raise ValueError(f"未知的周期单位：{unit}")
        
        logger.info(f"滚动时间窗口：{period} -> 新当前日期：{new_current.strftime('%Y-%m-%d')}")
        
        return WalkForwardWindow(new_current)


# ==================== Walk-Forward 选股池构建 ====================

def check_stock_listing_duration(code: str, df: pd.DataFrame, 
                                  required_years: float = 1.5) -> Tuple[bool, str]:
    """
    检查股票上市时间是否满足要求
    
    参数:
        code: 股票代码
        df: 股票历史数据 DataFrame
        required_years: 要求的最低上市年限（默认 1.5 年）
    
    返回:
        (是否满足要求，原因说明)
    
    使用场景:
        Walk-Forward 分析需要 T-1.5 年的数据，如果股票上市不足 1.5 年，
        则无法提供足够的历史数据进行选股池构建。
    """
    if df.empty:
        return False, "无历史数据"
    
    # 确保 date 列为 datetime 类型
    if not pd.api.types.is_datetime64_any_dtype(df['date']):
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
    
    # 获取最早和最晚日期
    earliest_date = df['date'].min()
    latest_date = df['date'].max()
    
    # 计算数据覆盖的年数
    data_span = (latest_date - earliest_date).days / 365.25
    
    # 计算上市至今的天数
    days_since_listing = (datetime.now() - earliest_date).days
    years_since_listing = days_since_listing / 365.25
    
    min_required_days = int(required_years * 365.25)
    
    if days_since_listing < min_required_days:
        reason = (
            f"上市时间不足 {required_years}年："
            f"上市日期={earliest_date.strftime('%Y-%m-%d')}, "
            f"至今={years_since_listing:.2f}年 "
            f"(需≥{required_years}年)"
        )
        return False, reason
    
    if data_span < required_years:
        reason = (
            f"历史数据覆盖时间不足 {required_years}年："
            f"数据范围={earliest_date.strftime('%Y-%m-%d')}至{latest_date.strftime('%Y-%m-%d')}, "
            f"覆盖={data_span:.2f}年 "
            f"(需≥{required_years}年)"
        )
        return False, reason
    
    return True, f"上市时间充足 ({years_since_listing:.2f}年)"


# ==================== 搜索空间动态裁剪 ====================

def build_adaptive_search_space(allocated_cash: float, init_pos: float,
                                 config: dict = None) -> dict:
    """
    启动时动态裁剪搜索空间（7参数版），避免 Optuna 采样无效组合。

    核心约束: Σ(grid_amount × multiplier^k for k in 0..max_grids-1) ≤ grid_pool × 0.95

    参数:
        allocated_cash: 单股分配资金（元）
        init_pos: 初始仓位比例（中性值，实际由 Optuna 采样）
        config: 配置字典（可选，读取 backtest 节中的参数范围）

    返回:
        裁剪后的搜索空间字典
    """
    bt_cfg = (config or {}).get('backtest', {})
    grid_pool = allocated_cash * (1 - init_pos)
    max_invest = grid_pool * 0.95

    # grid_amount 连续范围（Optuna 用 suggest_int 采样）
    amount_min = max(500, int(allocated_cash * 0.01))
    amount_max = min(100000, int(max_invest / 3))  # 最少保留3层空间

    # max_grids 范围（按资金级别，上限15）
    if allocated_cash < 100000:
        max_grids_raw = 8
    elif allocated_cash < 500000:
        max_grids_raw = 12
    else:
        max_grids_raw = 15

    # 保守估计：用 multiplier=2.0 估算资金需求来限制 max_grids
    # 在 multiplier=2.0 时最多层数
    test_multiplier = 2.0
    test_grids = 3
    while test_grids <= max_grids_raw:
        test_inv = float(np.sum(amount_min * (test_multiplier ** np.arange(test_grids))))
        if test_inv > max_invest:
            break
        test_grids += 1
    feasible_max_grids = max(3, test_grids - 1)

    max_grids_range = [3, min(max_grids_raw, feasible_max_grids + 2)]

    # 从 config 读取参数范围，config 未设置时回退到默认值
    # max_grids_range 由资金自适应推导，config 中的上限作为约束
    max_grids_config = bt_cfg.get('max_grids_range', [3, 15])
    max_grids_range[1] = min(max_grids_range[1], max_grids_config[1])

    return {
        'grid_amount_range': [amount_min, amount_max],
        'max_grids_range': max_grids_range,
        'buy_spacing_range': bt_cfg.get('buy_spacing_range', [0.01, 0.06]),
        'sell_spacing_range': bt_cfg.get('sell_spacing_range', [0.01, 0.06]),
        'multiplier_range': bt_cfg.get('amount_multiplier_range', [0.3, 3.0]),
        'initial_position_range': bt_cfg.get('initial_position_range', [0.15, 0.75]),
        'decay_range': bt_cfg.get('spacing_decay_range', [0.5, 2.0]),
        'grid_pool': grid_pool,
        'max_invest': max_invest,
    }


# ==================== 多目标惩罚函数 ====================

def calculate_composite_score(
    result: dict,
    buy_spacing_pct: float,
    sell_spacing_pct: float,
    max_grids: int,
    amount_multiplier: float,
    n_days: int,
    max_drawdown_limit: float = 0.12,
    trade_freq_threshold: int = 4,
    cost_ratio_limit: float = 0.03
) -> float:
    """
    多目标优化惩罚函数（7参数版）

    参数:
        result: backtest_grid_strategy 返回的字典
        buy_spacing_pct: 买入网格间距(小数)
        sell_spacing_pct: 卖出网格间距(小数)
        max_grids: 最大网格层数
        amount_multiplier: 加仓倍数
        n_days: 回测天数
        max_drawdown_limit: 最大回撤上限(默认12%)
        trade_freq_threshold: 月均交易次数上限阈值(默认4笔/月/股)
        cost_ratio_limit: 摩擦成本/初始本金上限(默认3%)
    """
    calmar = result.get('calmar_ratio', 0)
    max_dd = result.get('max_drawdown', 1)
    n_trades = result.get('n_trades', 0)
    annual_return = result.get('annual_return', 0)
    total_slippage = result.get('total_slippage_cost', 0)
    initial_cash = result.get('initial_cash', DEFAULT_INITIAL_CASH)

    if result.get('reason') == 'GRID_POOL_EXCEEDED':
        return -5.0

    if max_dd > max_drawdown_limit:
        return -999

    # 回撤软惩罚
    dd_ratio = max_dd / max_drawdown_limit
    dd_penalty = (dd_ratio - 0.5) * 0.5 if dd_ratio > 0.5 else 0.0

    # 频率惩罚
    months = max(1, n_days / 21)
    trades_per_month = n_trades / months
    trade_penalty = max(0.0, (trades_per_month - trade_freq_threshold) * 0.15)

    # 成本惩罚
    estimated_fees = abs(result.get('total_return', 0)) * initial_cash * 0.0007
    friction_ratio = (total_slippage + estimated_fees) / initial_cash
    cost_penalty = max(0.0, min((friction_ratio - cost_ratio_limit) * 5.0, 0.3))

    # 网格密度惩罚：使用平均间距
    avg_spacing = (buy_spacing_pct + sell_spacing_pct) / 2
    density_metric = max_grids / max(avg_spacing * 100, 0.1)
    if density_metric > 1.5:
        density_penalty = 0.3 * np.log10(density_metric / 1.5)
    else:
        density_penalty = 0.0

    # 金字塔激进惩罚：multiplier 偏离 1.0 过多时微罚
    mult_deviation = abs(amount_multiplier - 1.0)
    pyramid_penalty = 0.1 * mult_deviation if mult_deviation > 0.5 else 0.0

    composite = calmar - dd_penalty - trade_penalty - cost_penalty - density_penalty - pyramid_penalty

    if annual_return < 0:
        composite -= 1.5

    return composite


# ==================== 独立命令入口 ====================


def run_select(config: dict, force_select: bool = False) -> pd.DataFrame:
    """
    选股命令入口（严格本地模式）

    参数:
        config: 配置字典
        force_select: 是否强制重新选股

    返回:
        选股结果 DataFrame
    """
    logger.info("=" * 60)
    logger.info("选股模式：严格本地数据")
    logger.info("=" * 60)
    return run_multi_factor_selection(config, auto_update_config=True, force_select=force_select)


def run_backtest(config: dict) -> Dict:
    """
    回测命令入口：对选出的股票使用优化后的参数进行历史回测

    参数:
        config: 配置字典

    返回:
        回测结果字典
    """
    from data_layer.market_db import save_backtest_result, get_stock_data as get_db_data
    from utils.utils import load_state

    stocks = config.get('stocks', [])
    if not stocks:
        logger.error("配置文件中未指定股票列表，请先运行选股")
        return {}

    backtest_cfg = config.get('backtest', {})
    paths_cfg = config.get('paths', {})
    capital_cfg = config.get('capital', {})
    initial_cash = capital_cfg.get('total', int(DEFAULT_INITIAL_CASH))

    # 读取优化后的参数
    state = load_state()
    opt_history = state.get('optimization_history', {})

    # 默认网格参数（7参数格式）
    grid_cfg = config.get('grid', {})
    default_params = {
        'buy_spacing_pct': grid_cfg.get('base_spacing', 0.02),
        'sell_spacing_pct': grid_cfg.get('base_spacing', 0.02),
        'grid_amount': grid_cfg.get('grid_amount', 3000),
        'amount_multiplier': 1.0,
        'initial_position': grid_cfg.get('initial_position', 0.45),
        'max_grids': grid_cfg.get('max_grids', 5),
        'spacing_decay': 1.0,
    }

    results = []
    logger.info("=" * 60)
    logger.info("回测模式：历史数据回测")
    logger.info("=" * 60)
    logger.info(f"回测股票: {len(stocks)} 只, 初始资金: {initial_cash}")

    for code in stocks:
        logger.info(f"\n{'-'*60}")
        logger.info(f"回测: {code}")
        logger.info(f"{'-'*60}")

        # 读取历史数据（严格本地，直接从 SQLite 读取）
        df = get_db_data(code, data_dir=paths_cfg.get('data_dir', './data'))
        if df is None or df.empty or len(df) < 60:
            logger.warning(f"{code} 数据不足，跳过")
            continue

        df = clean_data(df)

        # 使用优化后的参数或默认参数（兼容新旧格式）
        params = opt_history.get(code, {}).get('final_params')
        if not params:
            params = default_params
        logger.info(f"参数: buy_sp={params.get('buy_spacing_pct', 0)*100:.2f}%, "
                    f"sell_sp={params.get('sell_spacing_pct', 0)*100:.2f}%, "
                    f"amount={params.get('grid_amount')}, mult={params.get('amount_multiplier', 1.0):.2f}, "
                    f"ip={params.get('initial_position', 0)*100:.0f}%, grids={params.get('max_grids')}, "
                    f"decay={params.get('spacing_decay', 1.0):.2f}")

        # 执行回测（7参数）
        result = backtest_grid_strategy(
            df,
            buy_spacing_pct=params.get('buy_spacing_pct', 0.02),
            sell_spacing_pct=params.get('sell_spacing_pct', params.get('buy_spacing_pct', 0.02)),
            grid_amount=params.get('grid_amount', 3000),
            amount_multiplier=params.get('amount_multiplier', 1.0),
            initial_position=params.get('initial_position', 0.45),
            max_grids=params.get('max_grids', 5),
            spacing_decay=params.get('spacing_decay', 1.0),
            commission_rate=backtest_cfg.get('commission_rate', 0.00015),
            stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
            slippage_rate=backtest_cfg.get('slippage_rate', 0.001),
            initial_cash=initial_cash
        )

        # 附加参数信息用于保存
        result['code'] = code
        result['params'] = params

        # 保存到 SQLite
        save_backtest_result(code, result, data_dir=paths_cfg.get('data_dir', './data'))

        logger.info(f"结果: 收益率={result['total_return']*100:.2f}%, "
                    f"最大回撤={result['max_drawdown']*100:.2f}%, "
                    f"Calmar={result['calmar_ratio']:.4f}, "
                    f"交易次数={result['n_trades']}")
        results.append(result)

    logger.info("=" * 60)
    logger.info("回测完成")
    logger.info("=" * 60)

    return {'stocks': stocks, 'results': results}


# ==================== Walk-Forward 完整流程 ====================

def execute_strategy(mode: str = None, config_path: str = "config.yaml",
                     force_select: bool = False):
    """
    策略执行入口

    参数:
        mode: 运行模式 ('select', 'optimize', 'backtest')
        config_path: 配置文件路径
        force_select: 是否强制重新选股（选股模式有效）
    """
    # 加载配置
    config = load_config(config_path)

    # 初始化数据获取模块配置（限流参数等）
    from data_layer.fetcher import init_fetcher_config
    init_fetcher_config(config)

    # 如果未指定模式，使用配置文件中的模式
    if mode is None:
        mode = config.get('mode', 'select')

    logger.info(f"\n当前运行模式：{mode.upper()}")

    try:
        if mode == 'select':
            result = run_select(config, force_select=force_select)
            return result
        elif mode == 'optimize':
            # 两阶段优化：贝叶斯优化 + WF微调
            result = run_two_phase_optimization(config)
            return result
        elif mode == 'backtest':
            result = run_backtest(config)
            return result
        else:
            logger.error(f"未知模式：{mode}")
            return None

    except Exception as e:
        logger.exception(f"策略执行失败：{str(e)}")
        raise


if __name__ == "__main__":
    # 测试运行
    logging.basicConfig(level=logging.INFO)
    
    # 简单测试选股
    config = load_config()
    config['mode'] = 'select'
    
    print("\n=== 测试选股模式 ===")
    result = execute_strategy('select')
    
    if result is not None:
        print(f"\n选股完成，找到 {len(result)} 只符合条件的股票")
