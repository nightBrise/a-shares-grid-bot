"""
策略模块 - A 股网格交易系统 v4.0 (Lite 精简版)
功能：选股、参数优化、信号生成 (核心大脑)
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
import pandas as pd
import numpy as np

try:
    import optuna
except ImportError:
    raise ImportError("请安装 optuna: pip install optuna>=3.0.0")

from data import (
    get_stock_data, clean_data, calculate_atr, calculate_hurst_exponent,
    align_to_trading_day, get_next_trading_day, get_trade_calendar,
    pre_filter_stocks_fast, get_thread_rate_limiter
)
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from utils import (
    load_config, calculate_transaction_fee, validate_buy_quantity,
    check_limit_status, check_t1_rule, format_number, safe_divide
)

# 新模块：多因子选股 (可选)
try:
    from indicators import calculate_all_indicators, get_latest_indicators
    from screener import (
        AdvancedMultiFactorScreener,
        screen_universe_advanced,
    )
    NEW_SCREENER_AVAILABLE = True
except ImportError:
    NEW_SCREENER_AVAILABLE = False
    logger.warning("新多因子选股模块不可用，请检查 indicators.py 和 screener.py 是否存在")

# 新模块：动态网格引擎 (可选)
try:
    import grid_engine
    GRID_ENGINE_AVAILABLE = True
except ImportError:
    GRID_ENGINE_AVAILABLE = False
    logger.warning("动态网格引擎不可用，请检查 grid_engine.py 是否存在")

# 新模块：增强风控 (可选)
try:
    from risk import EnhancedRiskControl
    ENHANCED_RISK_AVAILABLE = True
except ImportError:
    ENHANCED_RISK_AVAILABLE = False
    logger.warning("增强风控模块不可用，请检查 risk.py 是否存在")


logger = logging.getLogger("grid_trading")


import sys


# ==================== 选股逻辑 ====================

def run_selection(config: dict, auto_update_config: bool = True) -> pd.DataFrame:
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
    # 默认使用多因子横截面打分选股器
    if NEW_SCREENER_AVAILABLE:
        logger.info("=" * 60)
        logger.info("使用多因子横截面打分选股器")
        logger.info("=" * 60)
        return run_multi_factor_selection(config, auto_update_config)

    logger.error("多因子选股模块不可用，请检查 indicators.py 和 screener.py")
    return pd.DataFrame()


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
        # 使用线程独立的限流器
        rate_limiter = get_thread_rate_limiter(cfg)
        rate_limiter.wait()

        # 网络不稳定时优先使用缓存，避免重试等待
        df = get_stock_data(code, data_dir=data_dir, force_full=force_full,
                           enable_incremental=False, use_cache=True, fallback_to_cache=True)
        return (code, df, "success")
    except Exception as e:
        logger.debug(f"{code} 获取失败: {e}")
        return (code, pd.DataFrame(), f"error: {e}")


def run_multi_factor_selection(config: dict, auto_update_config: bool = True) -> pd.DataFrame:
    """
    多因子横截面打分选股模式 (高级版)

    使用四因子正交化模型：
    - F1: Reversion_Speed (OU半衰期) - 均值回归速度
    - F2: Trend_Strength (ADX) - 趋势持续性
    - F3: Vol_Quality (波动率倒U型) - 波动质量
    - F4: Path_Memory (Variance Ratio) - 残差分形结构

    特性：
    - 横截面多元正交化（F4 对 F1、F2 回归取残差）
    - 双轨权重（ETF vs 股票）
    - 动态阈值（adaptive_quantile 模式）
    - 现金缓冲机制

    参数:
        config: 配置字典
        auto_update_config: 是否自动更新配置文件

    返回:
        符合条件的多因子得分 Top N 股票列表 DataFrame
    """
    if not NEW_SCREENER_AVAILABLE:
        logger.error("多因子选股模块不可用，请检查 indicators.py 和 screener.py")
        return pd.DataFrame()

    logger.info("=" * 60)
    logger.info("开始执行高级多因子横截面打分选股 (v2)...")
    logger.info("=" * 60)

    paths_cfg = config.get('paths', {})
    selection_cfg = config.get('selection', {})
    adv_cfg = config.get('advanced_screening', {})

    # 获取全市场股票列表
    from data import get_all_a_stocks, get_stock_data, get_stocks_basic_info_batch

    df_all_stocks = get_all_a_stocks()

    if df_all_stocks.empty:
        logger.error("无法获取全市场股票列表")
        return pd.DataFrame()

    # 初步过滤 (仅保留代码格式正确的)
    initial_count = len(df_all_stocks)
    df_all_stocks = df_all_stocks[df_all_stocks['code'].str.contains(r'\.(SH|SZ)$', regex=True, na=False)]
    logger.info(f"初步过滤后剩余 {len(df_all_stocks)} 只股票")

    # === 获取批量行情并按成交额排序 ===
    pre_filter_cfg = config.get('pre_filter', {})
    top_n_by_turnover = pre_filter_cfg.get('top_n_by_turnover', 200)

    df_spot = get_stocks_basic_info_batch()

    if not df_spot.empty:
        # 合并全市场列表和行情数据
        df_merged = df_all_stocks.merge(df_spot[['code', 'price', 'turnover', 'is_st']], on='code', how='left')

        # 按成交额降序排序，取 top N
        df_merged = df_merged.sort_values('turnover', ascending=False)
        top_n_list = df_merged['code'].head(top_n_by_turnover).tolist()
        logger.info(f"按成交额排序后取 Top {top_n_by_turnover} 只候选")

        # 预过滤：按用户配置的成交额/价格/ST 条件过滤
        if pre_filter_cfg.get('enabled', True):
            logger.info("执行预过滤阶段...")
            stock_list = pre_filter_stocks_fast(top_n_list, config)
        else:
            stock_list = top_n_list
    else:
        # Fallback：无法获取行情时，按代码顺序
        stock_list = df_all_stocks['code'].head(top_n_by_turnover).tolist()
        logger.warning(f"无法获取批量行情，使用代码顺序候选 {len(stock_list)} 只")

    if not stock_list:
        logger.error("预过滤后无候选股票，请检查过滤条件是否过严")
        return pd.DataFrame()

    logger.info(f"预过滤后候选 {len(stock_list)} 只股票")

    # 计算每只股票的因子指标
    stocks_factors = []
    success_count = 0
    fail_count = 0

    # 获取 Path Memory 参数
    pm_cfg = adv_cfg.get('path_memory', {})
    vr_min_periods = pm_cfg.get('min_periods', 120)
    min_periods_required = max(60, vr_min_periods)

    # === 并行获取数据阶段 ===
    max_workers = pre_filter_cfg.get('parallel_workers',
                                      config.get('small_capital', {}).get('parallel_workers', 3))
    logger.info(f"并行获取 {len(stock_list)} 只股票数据 (workers={max_workers})...")

    results_lock = threading.Lock()
    processed_count = [0]  # 使用列表以便在闭包中修改

    def process_result(code: str, df: pd.DataFrame, status: str):
        """处理单个股票的获取结果"""
        nonlocal success_count, fail_count

        with results_lock:
            processed_count[0] += 1
            pct = 100 * processed_count[0] // len(stock_list)
            if processed_count[0] % max(1, len(stock_list) // 10) == 0:
                logger.info(f"进度: {processed_count[0]}/{len(stock_list)} ({pct}%)")

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

            # ST 检查 (简化: 通过名称判断)
            is_st = False

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

    # 使用 ThreadPoolExecutor 并行获取数据
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_single_stock_data,
                          (code, paths_cfg['data_dir'], False, config)): code
            for code in stock_list
        }

        for future in as_completed(futures):
            code = futures[future]
            try:
                _, df, status = future.result()
                process_result(code, df, status)
            except Exception as e:
                logger.debug(f"{code} 处理异常: {e}")
                with results_lock:
                    fail_count += 1

    logger.info(f"指标计算完成: 成功 {success_count}, 失败 {fail_count}")

    if not stocks_factors:
        logger.error("没有获取到任何股票数据")
        return pd.DataFrame()

    # 使用高级多因子打分器筛选
    screener = AdvancedMultiFactorScreener(config)

    scoring_pool_size = selection_cfg.get('scoring_pool_size', 20)
    df_result = screener.screen(stocks_factors, asset_type="stock", top_n=scoring_pool_size)

    if df_result.empty:
        logger.warning("高级多因子筛选后无股票，尝试放宽阈值...")
        # 放宽条件重试
        alt_config = config.copy()
        alt_adv = alt_config.get('advanced_screening', {}).copy()
        alt_adv['quality_threshold'] = 0.55  # 降低阈值
        alt_config['advanced_screening'] = alt_adv
        screener = AdvancedMultiFactorScreener(alt_config)
        df_result = screener.screen(stocks_factors, asset_type="stock", top_n=scoring_pool_size)

    if df_result.empty:
        logger.error("高级多因子筛选无结果")
        return pd.DataFrame()

    # 添加更多信息列
    df_result['reason'] = df_result.apply(
        lambda row: f"总分:{row['total_score']:.4f} "
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

    # 保存选股结果
    output_dir = paths_cfg['output_dir']
    result_file = os.path.join(output_dir, "stock_selection_advanced.csv")
    df_result.to_csv(result_file, index=False, encoding='utf-8-sig')
    logger.info(f"\n高级多因子选股结果已保存到：{result_file}")

    # 自动更新配置文件
    if auto_update_config and len(df_result) > 0:
        update_config_with_selected_stocks(df_result, config)

    return df_result


def update_config_with_selected_stocks(df_selection: pd.DataFrame, config: dict):
    """
    将选股结果写入配置文件，并设置选股完成标记
    
    参数:
        df_selection: 选股结果 DataFrame
        config: 当前配置字典
    """
    import yaml
    
    # 获取前 N 只最佳股票 (默认前 5 只，用于实际交易)
    save_top_n = config.get('selection', {}).get('save_top_n', 5)
    selected_stocks = df_selection.head(save_top_n)['code'].tolist()

    logger.info(f"\n正在更新配置文件，保存前 {save_top_n} 只最佳股票到 stocks 列表...")
    
    # 读取原始配置文件
    config_path = "config.yaml"
    with open(config_path, 'r', encoding='utf-8') as f:
        config_lines = f.readlines()
    
    # 找到 stocks 部分并替换
    new_stocks_yaml = '\n'.join([f'  - "{code}"' for code in selected_stocks])
    
    # 同时更新 selection_status 标记
    today_str = datetime.now().strftime('%Y-%m-%d')
    status_yaml = (
        f"\n# === 选股状态标记 (自动生成，勿手动修改) ===\n"
        f"selection_status:\n"
        f"  completed: true\n"
        f"  last_selection_date: \"{today_str}\"\n"
        f"  selection_count: {len(selected_stocks)}\n"
        f"  version: \"v4.0\"\n"
    )
    
    # 定位 stocks 段落和 selection_status 段落
    new_lines = []
    i = 0
    stocks_updated = False
    status_updated = False
    
    while i < len(config_lines):
        line = config_lines[i]
        
        # 检测 stocks: 行
        if line.strip().startswith('stocks:') and not stocks_updated:
            new_lines.append(line)  # 保留 stocks: 行
            stocks_updated = True
            
            # 跳过原有的股票列表 (直到下一个顶级配置项)
            i += 1
            while i < len(config_lines):
                next_line = config_lines[i]
                # 如果是缩进的列表项，跳过
                if next_line.strip().startswith('- ') or (next_line.startswith('  ') and ':' not in next_line):
                    i += 1
                    continue
                # 如果是空行，跳过
                elif next_line.strip() == '':
                    i += 1
                    continue
                else:
                    # 遇到新的配置项，停止
                    break
            
            # 插入新的股票列表
            new_lines.append(new_stocks_yaml)
            new_lines.append('\n')
            continue
        
        # 检测 selection_status: 部分并更新
        elif line.strip().startswith('selection_status:') and not status_updated:
            # 跳过整个 selection_status 部分
            i += 1
            while i < len(config_lines):
                next_line = config_lines[i]
                # 如果是缩进的子项，跳过
                if next_line.startswith('  ') and ':' in next_line:
                    i += 1
                    continue
                # 如果是空行或顶级配置项，停止
                else:
                    break
            
            # 插入新的 status
            new_lines.append(status_yaml)
            new_lines.append('\n')
            status_updated = True
            continue
        
        new_lines.append(line)
        i += 1
    
    # 如果没有找到 selection_status，在文件末尾添加
    if not status_updated:
        new_lines.append(status_yaml)
        new_lines.append('\n')
    
    # 写回配置文件
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write(''.join(new_lines))
    
    logger.info(f"✓ 配置文件已更新：{config_path}")
    logger.info(f"  新股票池：{', '.join(selected_stocks[:5])}{'...' if len(selected_stocks) > 5 else ''}")
    logger.info(f"  选股日期：{today_str}")
    logger.info(f"  下次运行将直接使用这些股票，无需重新选股")
    logger.info(f"  如需重新选股，请使用 --force-select 参数")


# ==================== 网格回测引擎 ====================

def backtest_grid_strategy(df: pd.DataFrame, grid_spacing: float,
                           grid_amount: float, initial_position: float,
                           max_grids: int, commission_rate: float,
                           stamp_tax: float, slippage_rate: float = 0.001,
                           initial_cash: float = 1000000.0) -> Dict:
    """
    网格交易回测引擎（增强版 - 包含滑点）

    原理:
    - 将资金分成多份，在价格下跌时买入，上涨时卖出
    - 记录每次交易，计算最终收益
    - 考虑滑点成本（买入价上浮，卖出价下浮）

    参数:
        df: 历史行情数据 (包含 close 列)
        grid_spacing: 网格间距 (小数格式，如 0.02 表示 2%)
        grid_amount: 每格买入金额 (元)
        initial_position: 初始仓位 (小数格式，如 0.45 表示 45%)
        max_grids: 最大网格层数
        commission_rate: 佣金费率
        stamp_tax: 印花税率
        slippage_rate: 滑点比率（默认 0.1%，即千 1）
        initial_cash: 初始资金（默认 100 万，支持小资金优化）

    返回:
        回测结果字典 (包含收益率、最大回撤、卡尔玛比率等)
    """
    if df.empty or len(df) < 10:
        return {'calmar_ratio': 0, 'total_return': 0, 'max_drawdown': 1}

    # === 流动性硬约束检查 ===
    grid_pool = initial_cash * (1 - initial_position)
    max_grid_investment = grid_amount * max_grids
    if max_grid_investment > grid_pool * 0.95:
        return {
            'calmar_ratio': -999,
            'total_return': -1,
            'max_drawdown': 1,
            'reason': 'GRID_POOL_EXCEEDED',
            'grid_pool': grid_pool,
            'required': max_grid_investment,
        }

    prices = df['close'].values

    # 初始化状态
    cash = initial_cash  # 使用传入的初始资金
    position = 0  # 持仓股数
    avg_cost = 0  # 平均成本
    total_slippage_cost = 0.0  # 累计滑点成本

    # 初始建仓
    first_price = prices[0]
    initial_investment = cash * initial_position  # 小数格式（0.45 = 45%）
    position = int(initial_investment / first_price / 100) * 100  # 100 的整数倍
    
    # 应用滑点：买入成交价 = 理论价 × (1 + 滑点率)
    actual_buy_price = first_price * (1 + slippage_rate)
    cost = position * actual_buy_price
    fee = calculate_transaction_fee(
        cost, 'buy', commission_rate, stamp_tax
    )
    cash -= cost + fee
    avg_cost = actual_buy_price  # 使用实际成交价作为成本
    
    # 网格中心价
    center_price = first_price
    
    # 交易记录
    trades = []
    portfolio_values = []
    
    # 网格价格计算
    def get_grid_prices(center: float, spacing: float, n_grids: int) -> Tuple[List, List]:
        """计算网格的买入价和卖出价"""
        buy_prices = []
        sell_prices = []
        
        for i in range(1, n_grids + 1):
            buy_price = center * (1 - spacing / 100 * i)
            sell_price = center * (1 + spacing / 100 * i)
            buy_prices.append(buy_price)
            sell_prices.append(sell_price)
        
        return buy_prices, sell_prices
    
    # 逐日回测
    for i, price in enumerate(prices[1:], start=1):
        prev_price = prices[i - 1]
        
        # 检查是否突破网格范围，重新设定中心价
        if price > center_price * (1 + grid_spacing / 100 * max_grids):
            center_price = price
        elif price < center_price * (1 - grid_spacing / 100 * max_grids):
            center_price = price
        
        buy_prices, sell_prices = get_grid_prices(
            center_price, grid_spacing, max_grids
        )
        
        # 尝试买入 (价格下跌触发)
        for j, buy_price in enumerate(buy_prices):
            if price <= buy_price and cash > grid_amount * 1.1:
                # 可以买入
                buy_qty = validate_buy_quantity(int(grid_amount / buy_price))
                
                if buy_qty > 0 and cash >= buy_qty * buy_price * 1.01:
                    # 应用滑点：买入成交价 = 理论价 × (1 + 滑点率)
                    actual_buy_price = buy_price * (1 + slippage_rate)
                    cost = buy_qty * actual_buy_price
                    fee = calculate_transaction_fee(
                        cost, 'buy', commission_rate, stamp_tax
                    )
                    
                    if cash >= cost + fee:
                        # 更新持仓
                        total_cost = position * avg_cost + buy_qty * actual_buy_price
                        position += buy_qty
                        avg_cost = total_cost / position if position > 0 else 0
                        cash -= cost + fee
                        
                        # 记录滑点成本
                        slippage_cost = buy_qty * (actual_buy_price - buy_price)
                        total_slippage_cost += slippage_cost
                        
                        trades.append({
                            'day': i,
                            'type': 'buy',
                            'price': actual_buy_price,
                            'qty': buy_qty,
                            'cost': cost + fee,
                            'slippage': slippage_cost
                        })
                        break
        
        # 尝试卖出 (价格上涨触发)
        for j, sell_price in enumerate(sell_prices):
            if price >= sell_price and position > 0:
                # 可以卖出
                sell_qty = min(position, validate_buy_quantity(
                    int(grid_amount / sell_price)
                ))
                
                if sell_qty > 0:
                    # 应用滑点：卖出成交价 = 理论价 × (1 - 滑点率)
                    actual_sell_price = sell_price * (1 - slippage_rate)
                    revenue = sell_qty * actual_sell_price
                    fee = calculate_transaction_fee(
                        revenue, 'sell', commission_rate, stamp_tax
                    )
                    
                    # 更新持仓
                    position -= sell_qty
                    cash += revenue - fee
                    
                    # 记录滑点成本
                    slippage_cost = sell_qty * (sell_price - actual_sell_price)
                    total_slippage_cost += slippage_cost
                    
                    trades.append({
                        'day': i,
                        'type': 'sell',
                        'price': actual_sell_price,
                        'qty': sell_qty,
                        'revenue': revenue - fee,
                        'slippage': slippage_cost
                    })
                    break
        
        # 计算组合市值
        portfolio_value = cash + position * price
        portfolio_values.append(portfolio_value)
    
    # === 计算绩效指标 ===
    
    # 总收益率
    final_value = portfolio_values[-1] if portfolio_values else 0
    initial_value = 1000000.0
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
        'slippage_ratio': slippage_ratio
    }


# ==================== 贝叶斯优化 ====================

def run_optimization(config: dict) -> Dict:
    """
    优化模式：使用 Optuna 进行贝叶斯优化，寻找最佳网格参数

    优化目标:
    - 最大化复合分数（Calmar − 回撤惩罚 − 频率惩罚 − 成本惩罚 − 密度惩罚）
    - 回撤硬约束：max_drawdown ≤ 12%
    - 网格资金池约束：grid_amount × max_grids ≤ alloc × (1 − initial_position) × 95%

    参数:
        config: 配置字典

    返回:
        最佳参数字典
    """
    logger.info("=" * 60)
    logger.info("开始执行参数优化 (贝叶斯优化 + 多目标惩罚)...")
    logger.info("=" * 60)

    stocks = config.get('stocks', [])
    grid_cfg = config.get('grid', {})
    backtest_cfg = config.get('backtest', {})
    paths_cfg = config.get('paths', {})
    capital_cfg = config.get('capital', {})

    if not stocks:
        logger.error("配置文件中未指定股票列表")
        return {}

    # 资金分配逻辑（与 optimize_parameters_wf 一致）
    total_cash = capital_cfg.get('total', 100000)
    max_position_pct = capital_cfg.get('max_position_per_stock', 0.30)
    cash_reserve_ratio = capital_cfg.get('cash_reserve_ratio', 0.40)
    initial_position = capital_cfg.get('initial_position', 0.45)

    investable_cash = total_cash * (1 - cash_reserve_ratio)
    max_stocks_by_money = max(1, int(investable_cash / (total_cash * max_position_pct)))
    max_stocks = min(max_stocks_by_money, len(stocks))
    allocated_cash = investable_cash / max_stocks if max_stocks > 0 else investable_cash

    grid_pool = allocated_cash * (1 - initial_position)

    logger.info(f"资金配置: 总资金={total_cash}元, 每股分配={allocated_cash:.0f}元, "
                f"网格池={grid_pool:.0f}元 ({(1-initial_position)*100:.0f}%)")

    # 搜索空间（启动时动态裁剪，避免无效采样）
    search_space = build_adaptive_search_space(allocated_cash, initial_position)
    logger.info(f"搜索空间（已裁剪）: grid_amount={search_space['grid_amount_choices']}, "
                f"max_grids={search_space['max_grids_range']}, grid_pool={search_space['grid_pool']:.0f}元")

    # 准备数据
    all_results = []

    for code in stocks[:max_stocks]:
        logger.info(f"\n处理股票：{code}")

        # 获取历史数据（仅对选出的 top_n 股票执行增量更新）
        df = get_stock_data(code, data_dir=paths_cfg['data_dir'],
                           selected_stocks=stocks)

        if df.empty or len(df) < backtest_cfg.get('days', 250):
            logger.warning(f"{code} 数据不足，跳过")
            continue

        # 清洗数据
        df = clean_data(df)

        # 截取回测周期
        n_days = backtest_cfg.get('days', 250)
        df_backtest = df.tail(n_days)

        # 样本外验证：分割数据
        oos_ratio = backtest_cfg.get('oos_ratio', 0.3)
        split_idx = int(len(df_backtest) * (1 - oos_ratio))

        df_ins = df_backtest.iloc[:split_idx]  # 样本内
        df_oos = df_backtest.iloc[split_idx:]  # 样本外

        logger.info(f"样本内数据：{len(df_ins)}天，样本外数据：{len(df_oos)}天")

        # === 定义 Optuna 目标函数（多目标惩罚） ===
        def objective(trial):
            """Optuna 多目标优化目标函数"""
            grid_spacing = trial.suggest_float('grid_spacing',
                search_space['grid_spacing_range'][0],
                search_space['grid_spacing_range'][1], step=0.001)
            grid_amount = trial.suggest_categorical('grid_amount',
                search_space['grid_amount_choices'])
            ip = trial.suggest_float('initial_position',
                search_space['initial_position_range'][0],
                search_space['initial_position_range'][1], step=0.01)
            max_g = trial.suggest_int('max_grids',
                search_space['max_grids_range'][0],
                search_space['max_grids_range'][1])

            # 样本内回测
            result = backtest_grid_strategy(
                df_ins,
                grid_spacing=grid_spacing,
                grid_amount=grid_amount,
                initial_position=ip,
                max_grids=max_g,
                commission_rate=backtest_cfg.get('commission_rate', 0.00015),
                stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
                slippage_rate=backtest_cfg.get('slippage_rate', 0.001),
                initial_cash=allocated_cash
            )

            return calculate_composite_score(
                result=result,
                grid_spacing=grid_spacing,
                max_grids=max_g,
                n_days=len(df_ins)
            )

        # === 执行优化 ===
        n_trials = backtest_cfg.get('n_trials', 150)
        n_startup = backtest_cfg.get('n_startup_trials', 20)
        logger.info(f"开始 Optuna 优化，试验次数：{n_trials}")

        sampler = optuna.samplers.TPESampler(n_startup_trials=n_startup)
        pruner = optuna.pruners.MedianPruner()

        study = optuna.create_study(
            direction='maximize',
            study_name=f'{code}_grid_optimization',
            load_if_exists=False,
            sampler=sampler,
            pruner=pruner
        )

        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

        # 最佳参数
        best_params = study.best_params
        best_score = study.best_value

        logger.info(f"\n{code} 最佳参数:")
        logger.info(f"  网格间距：{best_params['grid_spacing']:.3f}%")
        logger.info(f"  每格金额：{best_params['grid_amount']}元")
        logger.info(f"  初始仓位：{best_params['initial_position']*100:.0f}%")
        logger.info(f"  最大网格：{best_params['max_grids']}层")
        logger.info(f"  复合分数：{best_score:.4f}")

        # 样本外验证
        result_oos = backtest_grid_strategy(
            df_oos,
            grid_spacing=best_params['grid_spacing'],
            grid_amount=best_params['grid_amount'],
            initial_position=best_params['initial_position'],
            max_grids=best_params['max_grids'],
            commission_rate=backtest_cfg.get('commission_rate', 0.00015),
            stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
            slippage_rate=backtest_cfg.get('slippage_rate', 0.001),
            initial_cash=allocated_cash
        )

        oos_score = calculate_composite_score(
            result=result_oos,
            grid_spacing=best_params['grid_spacing'],
            max_grids=best_params['max_grids'],
            n_days=len(df_oos)
        )

        logger.info(f"样本外验证 - 复合分数：{oos_score:.4f}, "
                    f"Calmar：{result_oos['calmar_ratio']:.4f}, "
                    f"回撤：{result_oos['max_drawdown']*100:.2f}%, "
                    f"交易次数：{result_oos['n_trades']}")

        all_results.append({
            'code': code,
            'best_params': best_params,
            'in_sample_score': best_score,
            'out_of_sample_score': oos_score,
            'out_of_sample_calmar': result_oos['calmar_ratio'],
            'out_of_sample_drawdown': result_oos['max_drawdown'],
            'out_of_sample_trades': result_oos['n_trades'],
            'out_of_sample_return': result_oos['total_return']
        })

    # 保存优化结果
    output_dir = paths_cfg['output_dir']
    report_file = os.path.join(output_dir, 'report.json')

    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump({
            'optimization_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'capital_allocation': {
                'total': total_cash,
                'allocated_per_stock': allocated_cash,
                'grid_pool_per_stock': grid_pool,
                'num_stocks': max_stocks
            },
            'results': all_results
        }, f, ensure_ascii=False, indent=2)

    logger.info(f"\n优化报告已保存到：{report_file}")

    # === 选择实盘交易股票：根据收益排序，选择理论收益最大的 n 只 ===
    # 根据资金量确定实盘股票数量
    trading_stocks_count = max(1, max_stocks)  # 默认全部参与，可根据资金调整
    df_results = pd.DataFrame(all_results)
    df_results = df_results.sort_values('out_of_sample_return', ascending=False)
    trading_stocks = df_results.head(trading_stocks_count)['code'].tolist()

    logger.info(f"\n实盘交易股票（按收益排序）:")
    for i, code in enumerate(trading_stocks):
        row = df_results[df_results['code'] == code].iloc[0]
        logger.info(f"  {i+1}. {code} - 样本外收益: {row['out_of_sample_return']*100:.2f}%")

    # 保存实盘股票到 config_state.json
    from utils import load_state, save_state
    state = load_state()
    state['trading_stocks'] = trading_stocks
    state['optimization_history'] = {
        r['code']: {
            'best_params': r['best_params'],
            'out_of_sample_return': r['out_of_sample_return'],
            'out_of_sample_calmar': r['out_of_sample_calmar'],
            'out_of_sample_drawdown': r['out_of_sample_drawdown']
        }
        for r in all_results
    }
    save_state(state)
    logger.info(f"实盘股票已保存到 config_state.json")

    return all_results


# ==================== 信号生成（增强版：动态参数调整 + 风控） ====================

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
    
    # 如果状态文件中没有，尝试从优化报告文件加载
    if not optimization_history:
        paths_cfg = config.get('paths', {})
        output_dir = paths_cfg.get('output_dir', './output')
        
        # 查找最新的优化报告
        import glob
        report_files = glob.glob(os.path.join(output_dir, 'wf_optimization_report_*.json'))
        
        if report_files:
            latest_report = max(report_files)
            logger.info(f"从优化报告加载历史数据：{latest_report}")
            
            try:
                with open(latest_report, 'r', encoding='utf-8') as f:
                    report_data = json.load(f)
                
                # 提取各股票的优化结果
                for result in report_data.get('results', []):
                    code = result.get('code')
                    if code:
                        optimization_history[code] = {
                            'optimization_date': report_data.get('optimization_date', ''),
                            'best_params': result.get('best_params', {}),
                            'ins_calmar': result.get('in_sample', {}).get('calmar_ratio', 0),
                            'oos_calmar': result.get('out_of_sample', {}).get('calmar_ratio', 0)
                        }
                
                logger.info(f"加载了 {len(optimization_history)} 只股票的历史优化数据")
                
            except Exception as e:
                logger.warning(f"读取优化报告失败：{str(e)}")
    
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
        logger.info(f"使用原始优化参数（波动率调整：禁用或参考值为 0）")
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
    risk_cfg = config.get('risk', {})
    paths_cfg = config.get('paths', {})
    
    # === 步骤 1: 加载历史优化数据（用于波动率对比） ===
    logger.info("\n加载历史优化数据...")
    opt_history = load_optimization_history(config)
    
    # === 步骤 2: 初始化风控管理器 ===
    from risk_control import RiskControlManager, create_risk_control_manager
    
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
    
    logger.info(f"\n账户状态:")
    logger.info(f"  总市值：{account_status.total_value:,.2f}")
    logger.info(f"  历史峰值：{account_status.peak_value:,.2f}")
    logger.info(f"  当前回撤：{account_status.drawdown*100:.2f}%")
    logger.info(f"  持仓数量：{len(account_status.positions)}")
    
    # === 步骤 4: 执行熔断检查 ===
    logger.info("\n执行实盘熔断风控检查...")
    circuit_breaker_state = risk_manager.check_circuit_breaker(account_status)
    
    if circuit_breaker_state.is_global_breaker:
        logger.error("🚨 全局熔断已触发！仅生成卖出信号，暂停所有买入")
    
    # === 步骤 4: 确定实盘交易股票 ===
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

    # === 步骤 5: 生成交易信号 ===
    signals = []
    version = get_version()  # 获取策略版本号

    for code in signal_stocks:
        logger.info(f"\n{'-'*60}")
        logger.info(f"处理股票：{code}")
        logger.info(f"{'-'*60}")
        
        # 检查是否允许买入该股
        allow_buy = risk_manager.should_allow_buy(code)

        if not allow_buy:
            logger.warning(f"⚠️  {code} 触发熔断（全局或个股），跳过买入信号生成")

        # 获取最新数据（仅对实盘交易股票执行增量更新获取实时数据）
        df = get_stock_data(code, data_dir=paths_cfg['data_dir'],
                            selected_stocks=signal_stocks)
        
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
        
        if code in opt_history:
            hist_data = opt_history[code]
            
            # 注意：历史优化报告中可能没有直接存储波动率
            # 这里需要从 In-Sample 数据重新计算，或使用优化时的最佳参数反推
            # 简化处理：使用优化报告中的 in_sample_calmar 作为参考指标
            logger.info(f"找到历史优化记录：{hist_data.get('optimization_date', 'N/A')}")
            
            # 如果有保存的波动率数据，直接使用
            reference_vol = hist_data.get('ins_volatility', 0.0)
            
            # 如果没有，使用当前波动率的一定比例作为近似（临时方案）
            if reference_vol <= 0:
                logger.info(f"历史记录中无波动率数据，使用当前波动率的 80% 作为参考")
                reference_vol = current_vol * 0.8
            
            # 获取优化得到的最佳网格间距
            best_params = hist_data.get('best_params', {})
            if 'grid_spacing' in best_params:
                base_spacing = best_params['grid_spacing']
                logger.info(f"使用优化最佳参数：grid_spacing={base_spacing}%")
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
        
        # ATR 动态调整
        atr_ratio = safe_divide(current_atr, current_price, 0.02)
        atr_adjusted_spacing = adjusted_spacing * (atr_ratio / 0.02) * grid_cfg.get('atr_coef', 1.5)
        atr_adjusted_spacing = max(1.0, min(atr_adjusted_spacing, 5.0))
        
        logger.info(f"ATR 调整后间距：{atr_adjusted_spacing:.2f}%")
        
        # === 涨跌停检查 ===
        limit_up, limit_down = check_limit_status(
            current_price, prev_close, 
            risk_cfg.get('limit_threshold', 9.8)
        )
        
        if limit_down:
            logger.warning(f"{code} 跌停，暂停生成买入信号")
        if limit_up:
            logger.warning(f"{code} 涨停，暂停生成卖出信号")
        
        # === 生成网格价格 ===
        grid_amount = grid_cfg.get('grid_amount', 10000)
        max_grids = grid_cfg.get('max_grids', 10)
        
        # 买入信号（价格低于中心价）
        if allow_buy and not limit_down:
            for i in range(1, max_grids + 1):
                buy_price = current_price * (1 - atr_adjusted_spacing / 100 * i)
                buy_qty = validate_buy_quantity(int(grid_amount / buy_price))
                
                if buy_qty > 0:
                    signals.append({
                        'code': code,
                        'direction': 'buy',
                        'price': round(buy_price, 2),
                        'quantity': buy_qty,
                        'amount': round(buy_qty * buy_price, 2),
                        'reason': f'网格第{i}层买入，间距{atr_adjusted_spacing:.1f}%',
                        'valid_date': get_next_trading_day(datetime.now()).strftime('%Y-%m-%d'),
                        'priority': i,
                        'strategy_version': version,
                        'param_source': param_source
                    })
        elif not allow_buy:
            logger.warning(f"{code} 因风控限制，跳过所有买入信号")
        elif limit_down:
            logger.warning(f"{code} 因跌停限制，跳过所有买入信号")
        
        # 卖出信号（价格高于中心价）
        # 注意：实际应用中需要检查可用持仓 (T+1 规则)
        available_position = state.get(f'{code}_position', 10000)  # 示例可用持仓
        
        for i in range(1, max_grids + 1):
            sell_price = current_price * (1 + atr_adjusted_spacing / 100 * i)
            sell_qty = validate_buy_quantity(int(grid_amount / sell_price))
            
            if sell_qty > 0 and not limit_up:
                # T+1 检查
                if check_t1_rule(sell_qty, available_position):
                    signals.append({
                        'code': code,
                        'direction': 'sell',
                        'price': round(sell_price, 2),
                        'quantity': sell_qty,
                        'amount': round(sell_qty * sell_price, 2),
                        'reason': f'网格第{i}层卖出，间距{atr_adjusted_spacing:.1f}%',
                        'valid_date': get_next_trading_day(datetime.now()).strftime('%Y-%m-%d'),
                        'priority': i,
                        'strategy_version': version,
                        'param_source': param_source
                    })
                else:
                    logger.debug(f"{code} 卖出信号因 T+1 规则被过滤")
    
    # === 步骤 6: 转换为 DataFrame 并排序 ===
    if not signals:
        logger.warning("未生成任何交易信号")
        return pd.DataFrame()
    
    df_signals = pd.DataFrame(signals)
    
    # 排序（按代码、优先级）
    df_signals = df_signals.sort_values(['code', 'priority', 'direction'])
    
    # === 步骤 7: 风控过滤（移除被禁止的买入信号） ===
    from risk_control import filter_signals_by_risk
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
        filtered_count = len(df_code[df_code.get('filtered', False) == True]) if 'filtered' in df_code.columns else 0
        
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

        # T - 3 个月（样本内结束/样本外开始）
        oos_start_raw = self.current_date - pd.DateOffset(months=3)
        self.oos_start = align_to_trading_day(oos_start_raw, direction='backward')

        # T - 1 年（样本内开始）
        ins_start_raw = self.current_date - pd.DateOffset(years=1)
        self.ins_start = align_to_trading_day(ins_start_raw, direction='backward')

        # T - 1.5 年（选股池构建开始）
        universe_start_raw = self.current_date - pd.DateOffset(years=1, months=6)
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
        logger.info(f"选股池构建期：{self.universe_start_str} 至 {self.oos_start_str} (T-1.5Y ~ T-3M)")
        logger.info(f"样本内优化期：{self.ins_start_str} 至 {self.oos_start_str} (T-1Y ~ T-3M)")
        logger.info(f"样本外验证期：{self.oos_start_str} 至 {self.current_date_str} (T-3M ~ T)")
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


def build_universe_with_wf(config: dict, wf_window: WalkForwardWindow) -> pd.DataFrame:
    """
    基于 Walk-Forward 窗口构建选股池
    
    数据范围：T - 1.5 年 至 T - 3 个月（不含）
    
    选股标准:
    1. Hurst 指数 < 0.5 (均值回归特性)
    2. 流动性充足 (日均成交额 > 阈值)
    3. 价格适中 (避免高价股和低价股)
    4. 波动率适中 (避免过度波动)
    
    参数:
        config: 配置字典
        wf_window: Walk-Forward 窗口管理器
    
    返回:
        符合条件的股票列表 DataFrame
    
    关键约束:
        - 禁止使用 T - 3 个月之后的任何数据
        - 防止未来信息泄露
        - 自动排除上市时间不足 1.5 年的股票
    """
    logger.info("=" * 70)
    logger.info("Walk-Forward 选股池构建")
    logger.info("=" * 70)
    
    # 获取选股池构建期日期范围
    universe_start, universe_end = wf_window.get_universe_period()
    universe_start_dt = datetime.strptime(universe_start, '%Y-%m-%d')
    universe_end_dt = datetime.strptime(universe_end, '%Y-%m-%d')
    
    # 计算所需的最小数据跨度
    required_data_span = (universe_end_dt - universe_start_dt).days
    required_years = required_data_span / 365.25
    
    logger.info(f"选股数据期间：{universe_start} 至 {universe_end}")
    logger.info(f"所需最小数据跨度：{required_data_span}天 ({required_years:.2f}年)")
    logger.info(f"注意：禁止使用 {universe_end} 之后的数据（防止前视偏差）")
    logger.info(f"注意：自动排除上市时间不足 {required_years:.2f}年的股票")
    logger.info("=" * 70)
    
    selection_cfg = config.get('selection', {})
    risk_cfg = config.get('risk', {})
    paths_cfg = config.get('paths', {})
    
    # 获取全市场股票列表
    from data import get_all_a_stocks, prepare_selection_data
    
    df_all_stocks = get_all_a_stocks()
    
    if df_all_stocks.empty:
        logger.error("无法获取全市场股票列表")
        return pd.DataFrame()
    
    # 初步过滤（仅保留代码格式正确的）
    initial_count = len(df_all_stocks)
    df_all_stocks = df_all_stocks[df_all_stocks['code'].str.contains(r'\.(SH|SZ)$', regex=True, na=False)]
    logger.info(f"初步过滤后剩余 {len(df_all_stocks)} 只股票 (共过滤 {initial_count - len(df_all_stocks)} 只)")
    
    # 限制处理数量
    max_stocks_to_process = selection_cfg.get('max_stocks_to_process', 200)
    stock_list = df_all_stocks['code'].head(max_stocks_to_process).tolist()
    logger.info(f"将处理最近的 {len(stock_list)} 只股票")
    
    # === 获取选股数据并计算指标 ===
    results = []
    success_count = 0
    fail_count = 0
    filtered_by_listing = 0  # 因上市时间不足被过滤的数量
    
    for i, code in enumerate(stock_list):
        try:
            logger.info(f"[{i+1}/{len(stock_list)}] 处理股票：{code}")
            
            # 获取历史数据
            df = get_stock_data(code, data_dir=paths_cfg['data_dir'])
            
            # 防御性检查 1: 无数据
            if df.empty:
                logger.warning(f"{code} 无数据，跳过")
                fail_count += 1
                continue
            
            # 防御性检查 2: 上市时间不足
            is_valid_listing, listing_reason = check_stock_listing_duration(
                code, df, required_years=required_years
            )
            
            if not is_valid_listing:
                logger.info(f"{code} 过滤：{listing_reason}")
                filtered_by_listing += 1
                continue
            
            logger.debug(f"{code} {listing_reason}")
            
            # === 关键：按 Walk-Forward 窗口切割数据 ===
            # 仅使用选股池构建期的数据 [T-1.5Y, T-3M)
            df_universe = wf_window.slice_dataframe_by_period(df, period='universe')
            
            # 防御性检查 3: 数据量不足
            if df_universe.empty:
                logger.warning(f"{code} 选股期数据为空，跳过")
                fail_count += 1
                continue
            
            min_required_records = selection_cfg.get('min_required_records', 60)
            if len(df_universe) < min_required_records:
                logger.warning(
                    f"{code} 选股期数据不足 (<{min_required_records}条，实际{len(df_universe)}条)，跳过"
                )
                fail_count += 1
                continue
            
            # 清洗数据
            df_universe = clean_data(df_universe)
            
            # 再次检查清洗后的数据量
            if df_universe.empty:
                logger.warning(f"{code} 数据清洗后为空，跳过")
                fail_count += 1
                continue
            
            # 计算指标（仅基于选股期数据）
            latest = df_universe.iloc[-1]
            
            # ATR
            df_universe['atr'] = calculate_atr(df_universe, selection_cfg.get('atr_period', 20))
            latest_atr = df_universe['atr'].iloc[-1]
            
            # 波动率
            df_universe['volatility'] = calculate_volatility(df_universe, 20)
            latest_vol = df_universe['volatility'].iloc[-1]
            
            # Hurst 指数（使用选股期最后 250 天，如果数据不足则使用全部）
            if len(df_universe) >= 250:
                price_series = df_universe['close'].tail(250)
            else:
                price_series = df_universe['close']
                logger.debug(f"{code} 数据不足 250 条，使用全部 {len(price_series)} 条计算 Hurst 指数")
            
            hurst = calculate_hurst_exponent(price_series)
            
            # 日均成交额（选股期最后 20 天）
            if len(df_universe) >= 20:
                avg_turnover = df_universe['amount'].tail(20).mean() / 10000  # 万元
            else:
                avg_turnover = df_universe['amount'].mean() / 10000
                logger.debug(f"{code} 数据不足 20 条，使用全部数据计算日均成交额")
            
            # 收集结果
            results.append({
                'code': code,
                'price': latest['close'],
                'atr': latest_atr if not np.isnan(latest_atr) else 0,
                'volatility': latest_vol if not np.isnan(latest_vol) else 0,
                'hurst': hurst,
                'avg_turnover': avg_turnover,
                'valid': True
            })
            
            success_count += 1
            
        except Exception as e:
            logger.error(f"处理 {code} 时发生异常：{str(e)}", exc_info=True)
            fail_count += 1
    
    # 转换为 DataFrame
    df_selection = pd.DataFrame(results)
    
    if df_selection.empty:
        logger.error("没有获取到任何股票数据")
        return pd.DataFrame()
    
    # === 输出统计信息 ===
    logger.info("\n" + "=" * 70)
    logger.info("选股池构建统计:")
    logger.info("=" * 70)
    logger.info(f"总计：{len(stock_list)} 只股票")
    logger.info(f"  成功入选：{success_count} 只")
    logger.info(f"  处理失败：{fail_count} 只")
    logger.info(f"  上市时间不足过滤：{filtered_by_listing} 只")
    logger.info(f"  成功率：{success_count/max(len(stock_list),1)*100:.1f}%")
    logger.info("=" * 70)
    
    # === 应用选股条件过滤 ===
    
    # 1. Hurst 指数过滤
    hurst_threshold = selection_cfg.get('hurst_threshold', 0.5)
    df_selection = df_selection[df_selection['hurst'] < hurst_threshold]
    logger.info(f"Hurst < {hurst_threshold}: 剩余 {len(df_selection)} 只股票")
    
    # 2. 流动性过滤
    min_turnover = risk_cfg.get('min_turnover', 5000)
    df_selection = df_selection[df_selection['avg_turnover'] >= min_turnover]
    logger.info(f"日均成交额 >= {min_turnover}万：剩余 {len(df_selection)} 只股票")
    
    # 3. 价格过滤
    min_price = selection_cfg.get('min_price', 5.0)
    max_price = selection_cfg.get('max_price', 500.0)
    df_selection = df_selection[
        (df_selection['price'] >= min_price) & 
        (df_selection['price'] <= max_price)
    ]
    logger.info(f"价格在 [{min_price}, {max_price}] 区间：剩余 {len(df_selection)} 只股票")
    
    # 4. 波动率过滤
    vol_threshold = selection_cfg.get('volatility_threshold', 0.8)
    df_selection = df_selection[df_selection['volatility'] < vol_threshold]
    logger.info(f"波动率 < {vol_threshold}: 剩余 {len(df_selection)} 只股票")
    
    # 排序（按 Hurst 指数）
    df_selection = df_selection.sort_values('hurst', ascending=True)
    df_selection['rank'] = range(1, len(df_selection) + 1)
    
    # 添加推荐理由
    df_selection['reason'] = df_selection.apply(
        lambda row: f"Hurst={row['hurst']:.3f}, ATR={row['atr']:.2f}, 成交额={row['avg_turnover']:.0f}万",
        axis=1
    )
    
    logger.info("\n" + "=" * 70)
    logger.info("选股池构建完成:")
    logger.info(f"总计：{len(stock_list)} 只，成功：{success_count}只，失败：{fail_count}只")
    logger.info(f"最终入选：{len(df_selection)} 只")
    logger.info("=" * 70)
    
    # 保存选股结果
    output_dir = paths_cfg['output_dir']
    os.makedirs(output_dir, exist_ok=True)
    result_file = os.path.join(output_dir, f"wf_stock_selection_{wf_window.current_date_str.replace('-', '')}.csv")
    df_selection.to_csv(result_file, index=False, encoding='utf-8-sig')
    logger.info(f"选股结果已保存到：{result_file}")
    
    # 打印前 10 只股票
    logger.info("\n选股池预览 (Top 10):")
    for _, row in df_selection.head(10).iterrows():
        logger.info(f"  {row['rank']}. {row['code']} | {row['reason']}")
    
    return df_selection


# ==================== 搜索空间动态裁剪 ====================

def build_adaptive_search_space(allocated_cash: float, init_pos: float) -> dict:
    """
    启动时动态裁剪搜索空间，避免 Optuna 采样无效组合

    核心约束: grid_amount × max_grids ≤ grid_pool × 0.95

    参数:
        allocated_cash: 单股分配资金（元）
        init_pos: 初始仓位比例（决定网格资金池大小）

    返回:
        裁剪后的搜索空间字典
    """
    # 实际网格资金池 = alloc × (1 - initial_position)
    grid_pool = allocated_cash * (1 - init_pos)
    # 95% 安全系数
    max_invest = grid_pool * 0.95

    # 基础每格金额下限
    amount_min = max(2000, allocated_cash * 0.03)

    # 离散候选值（按资金级别）
    if allocated_cash < 100000:
        raw_amounts = [2000, 3000, 4000, 5000]
        max_grids_raw = 6
    elif allocated_cash < 500000:
        raw_amounts = [5000, 8000, 10000, 15000]
        max_grids_raw = 9
    else:
        raw_amounts = [10000, 20000, 30000, 50000]
        max_grids_raw = 9

    # 动态裁剪：基于保守估计的 max_grids（取最小4层）过滤 grid_amount
    # 确保所有 valid_amounts 在 max_grids=4 时都满足约束
    conservative_grids = 4
    valid_amounts = [a for a in raw_amounts if a * conservative_grids <= max_invest]

    if not valid_amounts:
        # 降级：找不到满足4层的grid_amount，计算 amount_min 支持的最大层数
        valid_amounts = [amount_min]
        feasible_grids = int(max_invest / amount_min)
        max_grids = min(max_grids_raw, feasible_grids)
        # 确保 lower <= upper
        if max_grids < 4:
            max_grids_range = [max(1, max_grids), max_grids] if max_grids >= 1 else [1, 1]
        else:
            max_grids_range = [4, max_grids]
    else:
        # 基于最大有效金额计算 max_grids 上限（确保所有 valid_amounts 都满足约束）
        max_valid_amount = max(valid_amounts)
        feasible_grids = int(max_invest / max_valid_amount)
        max_grids = min(max_grids_raw, feasible_grids)
        # 修正：确保 max_grids 与所有 valid_amounts 兼容
        if max_valid_amount * 4 > max_invest:
            max_grids = min(max_grids, feasible_grids)
        max_grids_range = [4, max_grids] if max_grids >= 4 else [max(1, max_grids), max_grids]

    return {
        'grid_amount_choices': valid_amounts,
        'max_grids_range': max_grids_range,
        'grid_spacing_range': [0.015, 0.035],
        'initial_position_range': [0.40, 0.50],
        'grid_pool': grid_pool,
        'max_invest': max_invest,
    }


# ==================== 多目标惩罚函数 ====================

def calculate_composite_score(
    result: dict,
    grid_spacing: float,
    max_grids: int,
    n_days: int,
    max_drawdown_limit: float = 0.12,
    trade_freq_threshold: int = 4,
    cost_ratio_limit: float = 0.03
) -> float:
    """
    多目标优化惩罚函数（量纲对齐版）

    设计原则：
    1. 小资金(<10万)对交易频率极敏感——高频网格吞噬手续费本金
    2. 追求高Calmar但不能容忍高回撤——12%是小资金存活红线
    3. 密集网格(小间距+多层)在波动率跳变时易崩溃——需惩罚

    参数:
        result: backtest_grid_strategy 返回的字典
        grid_spacing: 网格间距(小数，如 0.02 表示 2%)
        max_grids: 最大网格层数
        n_days: 回测天数
        max_drawdown_limit: 最大回撤上限(默认12%)
        trade_freq_threshold: 月均交易次数上限阈值(默认4笔/月/股)
        cost_ratio_limit: 摩擦成本/初始本金上限(默认3%)

    返回:
        复合分数(越高越好)，返回 -999 表示最大回撤超限，-5.0 表示流动性越界
    """
    # 1. 提取关键指标
    calmar = result.get('calmar_ratio', 0)
    max_dd = result.get('max_drawdown', 1)
    n_trades = result.get('n_trades', 0)
    annual_return = result.get('annual_return', 0)
    total_slippage = result.get('total_slippage_cost', 0)
    initial_cash = result.get('initial_cash', 1000000.0)

    # 2. 硬约束：流动性越界 → 软惩罚（有限值避免污染 TPE 分布）
    if result.get('reason') == 'GRID_POOL_EXCEEDED':
        return -5.0

    # 3. 硬约束：最大回撤超过上限 → 无效
    if max_dd > max_drawdown_limit:
        return -999

    # 4. 回撤软惩罚：[0, 0.5] 区间无惩罚，[0.5, 1.0] 线性增长至 0.25
    #    量级：max 0.25，与 Calmar 量纲匹配
    dd_ratio = max_dd / max_drawdown_limit
    if dd_ratio > 0.5:
        dd_penalty = (dd_ratio - 0.5) * 0.5
    else:
        dd_penalty = 0.0

    # 5. 频率惩罚：月均交易次数超过阈值（4笔/月/股）开始惩罚
    #    量级：max ~0.4，避免淹没 Calmar 信号
    months = max(1, n_days / 21)
    trades_per_month = n_trades / months
    trade_penalty = max(0.0, (trades_per_month - trade_freq_threshold) * 0.15)

    # 6. 成本惩罚：使用 initial_cash 作分母，防除零奇点
    #    摩擦率 = (滑点 + 估算手续费) / 初始本金
    #    超过 3% 阈值开始惩罚，上限 0.3（避免淹没 Calmar 信号）
    estimated_fees = abs(result.get('total_return', 0)) * initial_cash * 0.0007
    friction_ratio = (total_slippage + estimated_fees) / initial_cash
    cost_penalty = max(0.0, min((friction_ratio - cost_ratio_limit) * 5.0, 0.3))

    # 7. 网格密度惩罚：对数平滑，量级控制在 [0, 0.4]
    #    density_metric = max_grids / (spacing * 100)
    #    例: 5层/2.0%间距 = 2.5；6层/1.5%间距 = 4.0
    #    >1.5 时开始惩罚，6层/1.5% 惩罚约 0.28
    density_metric = max_grids / max(grid_spacing * 100, 0.1)
    if density_metric > 1.5:
        density_penalty = 0.3 * np.log10(density_metric / 1.5)
    else:
        density_penalty = 0.0

    # 8. 复合分数 = Calmar - 各项惩罚
    composite = calmar - dd_penalty - trade_penalty - cost_penalty - density_penalty

    # 9. 年化收益为负 → 硬下移，保持梯度单调性
    if annual_return < 0:
        composite -= 1.5

    return composite


# ==================== Walk-Forward 参数优化 ====================

def optimize_parameters_wf(config: dict, wf_window: WalkForwardWindow,
                           stock_pool: List[str]) -> Dict:
    """
    基于 Walk-Forward 窗口的参数优化
    
    数据范围：
    - In-Sample: T - 1 年 至 T - 3 个月（用于优化）
    - Out-of-Sample: T - 3 个月 至 T（用于验证）
    
    优化算法：Optuna 贝叶斯超参数搜索
    
    搜索空间:
    - grid_spacing: 1.0% ~ 5.0%, 步长 0.1%
    - grid_amount: [5000, 10000, 20000, 50000]
    - initial_position: 30% ~ 70%, 步长 5%
    - max_grids: 5 ~ 15
    
    目标函数：最大化 Calmar Ratio (年化收益 / 最大回撤)
    
    参数:
        config: 配置字典
        wf_window: Walk-Forward 窗口管理器
        stock_pool: 候选股票池列表
    
    返回:
        优化结果字典（包含最佳参数和绩效指标）
    
    关键约束:
        - OOS 数据绝对不允许参与任何形式的训练或参数选择
    """
    logger.info("=" * 70)
    logger.info("Walk-Forward 参数优化")
    logger.info("=" * 70)
    
    # 获取日期范围
    ins_start, ins_end = wf_window.get_ins_sample_period()
    oos_start, oos_end = wf_window.get_oos_sample_period()
    
    logger.info(f"In-Sample 期间：{ins_start} 至 {ins_end} (T-1Y ~ T-3M)")
    logger.info(f"Out-of-Sample 期间：{oos_start} 至 {oos_end} (T-3M ~ T)")
    logger.info("=" * 70)

    grid_cfg = config.get('grid', {})
    backtest_cfg = config.get('backtest', {})
    paths_cfg = config.get('paths', {})
    capital_cfg = config.get('capital', {})

    # 从配置读取资金参数
    total_cash = capital_cfg.get('total', 100000)  # 默认10万
    max_position_pct = capital_cfg.get('max_position_per_stock', 0.30)
    cash_reserve_ratio = capital_cfg.get('cash_reserve_ratio', 0.40)
    initial_position = capital_cfg.get('initial_position', 0.45)

    # 计算每只股票分配的资金
    investable_cash = total_cash * (1 - cash_reserve_ratio)
    max_stocks_by_money = max(1, int(investable_cash / (total_cash * max_position_pct)))
    max_stocks = min(max_stocks_by_money, len(stock_pool))
    allocated_cash = investable_cash / max_stocks if max_stocks > 0 else investable_cash

    # 网格资金池
    grid_pool = allocated_cash * (1 - initial_position)

    logger.info(f"资金配置: 总资金={total_cash}元, 单股上限={max_position_pct*100}%, "
                f"现金保留={cash_reserve_ratio*100}%")
    logger.info(f"投入股票数: {max_stocks}, 每只分配: {allocated_cash:.0f}元, "
                f"网格资金池={grid_pool:.0f}元 ({(1-initial_position)*100:.0f}%)")

    # 计算搜索空间（启动时动态裁剪，避免无效采样）
    search_space = build_adaptive_search_space(allocated_cash, initial_position)
    logger.info(f"搜索空间（已裁剪）: grid_amount={search_space['grid_amount_choices']}, "
                f"max_grids={search_space['max_grids_range']}, "
                f"grid_pool={search_space['grid_pool']:.0f}元")

    if not stock_pool:
        logger.error("股票池为空，无法优化")
        return {}
    
    all_results = []
    
    # 限制优化股票数量
    max_optimize_stocks = backtest_cfg.get('max_optimize_stocks', 5)
    stocks_to_optimize = stock_pool[:max_optimize_stocks]
    
    logger.info(f"将对以下 {len(stocks_to_optimize)} 只股票进行参数优化:")
    for code in stocks_to_optimize:
        logger.info(f"  - {code}")
    logger.info("")
    
    for code in stocks_to_optimize:
        logger.info(f"\n{'='*70}")
        logger.info(f"处理股票：{code}")
        logger.info(f"{'='*70}")

        # 获取历史数据（仅对选股池中的股票执行增量更新）
        df = get_stock_data(code, data_dir=paths_cfg['data_dir'],
                            selected_stocks=stocks_to_optimize)
        
        if df.empty:
            logger.warning(f"{code} 无数据，跳过")
            continue
        
        # 清洗数据
        df = clean_data(df)
        
        # === 关键：按 Walk-Forward 窗口切割数据 ===
        # In-Sample 数据：用于参数优化 [T-1Y, T-3M)
        df_ins = wf_window.slice_dataframe_by_period(df, period='ins')
        
        # Out-of-Sample 数据：用于独立验证 [T-3M, T]
        df_oos = wf_window.slice_dataframe_by_period(df, period='oos')
        
        # 记录数据切片信息（注释要求）
        logger.info(f"数据切片完成:")
        logger.info(f"  In-Sample: {len(df_ins)}天 ({ins_start} 至 {ins_end})")
        logger.info(f"  Out-of-Sample: {len(df_oos)}天 ({oos_start} 至 {oos_end})")
        
        if df_ins.empty or len(df_ins) < 60:
            logger.warning(f"{code} In-Sample 数据不足，跳过")
            continue
        
        if df_oos.empty or len(df_oos) < 20:
            logger.warning(f"{code} Out-of-Sample 数据不足，跳过")
            continue
        
        # === 定义 Optuna 目标函数 ===
        def objective(trial):
            """
            Optuna 多目标优化目标函数

            搜索空间（根据资金自动计算）:
            - grid_spacing: {search_space['grid_spacing_range']}
            - grid_amount: {search_space['grid_amount_choices']}
            - initial_position: {search_space['initial_position_range']}
            - max_grids: {search_space['max_grids_range']}

            目标：最大化复合分数 = Calmar - 回撤惩罚 - 交易频率惩罚 - 成本率惩罚 - 网格密度惩罚
            """
            # 参数搜索空间（根据资金规模自动计算）
            grid_spacing = trial.suggest_float('grid_spacing',
                search_space['grid_spacing_range'][0],
                search_space['grid_spacing_range'][1], step=0.001)
            grid_amount = trial.suggest_categorical('grid_amount',
                search_space['grid_amount_choices'])
            initial_position = trial.suggest_float('initial_position',
                search_space['initial_position_range'][0],
                search_space['initial_position_range'][1], step=0.01)
            max_grids = trial.suggest_int('max_grids',
                search_space['max_grids_range'][0],
                search_space['max_grids_range'][1])

            # In-Sample 回测（仅使用样本内数据）
            result = backtest_grid_strategy(
                df_ins,  # 关键：仅使用 In-Sample 数据
                grid_spacing=grid_spacing,
                grid_amount=grid_amount,
                initial_position=initial_position,
                max_grids=max_grids,
                commission_rate=backtest_cfg.get('commission_rate', 0.00015),
                stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
                slippage_rate=backtest_cfg.get('slippage_rate', 0.001),
                initial_cash=allocated_cash  # 使用分配的资金
            )

            # 多目标惩罚复合分数
            return calculate_composite_score(
                result=result,
                grid_spacing=grid_spacing,
                max_grids=max_grids,
                n_days=len(df_ins),
                max_drawdown_limit=0.12,
                trade_freq_limit=0.05,
                cost_ratio_limit=0.40
            )
        
        # === 执行优化 ===
        n_trials = backtest_cfg.get('n_trials', 150)
        n_startup = backtest_cfg.get('n_startup_trials', 20)
        logger.info(f"开始 Optuna 优化，试验次数：{n_trials}")

        sampler = optuna.samplers.TPESampler(n_startup_trials=n_startup)
        pruner = optuna.pruners.MedianPruner()

        study = optuna.create_study(
            direction='maximize',
            study_name=f'{code}_wf_optimization_{wf_window.current_date_str}',
            load_if_exists=False,
            sampler=sampler,
            pruner=pruner
        )
        
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
        
        # 最佳参数
        best_params = study.best_params
        best_calmar_ins = study.best_value
        
        logger.info(f"\n{code} In-Sample 最佳参数:")
        logger.info(f"  网格间距：{best_params['grid_spacing']}%")
        logger.info(f"  每格金额：{best_params['grid_amount']}元")
        logger.info(f"  初始仓位：{best_params['initial_position']}%")
        logger.info(f"  最大网格：{best_params['max_grids']}层")
        logger.info(f"  In-Sample Calmar Ratio: {best_calmar_ins:.4f}")
        
        # === 样本外验证（关键：OOS 数据不参与优化） ===
        logger.info(f"\n开始 Out-of-Sample 验证...")
        
        result_oos = backtest_grid_strategy(
            df_oos,  # 关键：仅使用 OOS 数据
            grid_spacing=best_params['grid_spacing'],
            grid_amount=best_params['grid_amount'],
            initial_position=best_params['initial_position'],
            max_grids=best_params['max_grids'],
            commission_rate=backtest_cfg.get('commission_rate', 0.00015),
            stamp_tax=backtest_cfg.get('stamp_tax', 0.0005),
            slippage_rate=backtest_cfg.get('slippage_rate', 0.001)  # 新增：滑点参数
        )
        
        logger.info(f"\n{code} Out-of-Sample 绩效指标:")
        logger.info(f"  总收益率：{result_oos['total_return']*100:.2f}%")
        logger.info(f"  年化收益：{result_oos['annual_return']*100:.2f}%")
        logger.info(f"  最大回撤：{result_oos['max_drawdown']*100:.2f}%")
        logger.info(f"  夏普比率：{result_oos['sharpe_ratio']:.4f}")
        logger.info(f"  Calmar Ratio: {result_oos['calmar_ratio']:.4f}")
        logger.info(f"  交易次数：{result_oos['n_trades']}")
        
        # 保存结果
        all_results.append({
            'code': code,
            'window_date': wf_window.current_date_str,
            'best_params': best_params,
            'in_sample': {
                'calmar_ratio': best_calmar_ins,
                'trials': n_trials
            },
            'out_of_sample': {
                'total_return': result_oos['total_return'],
                'annual_return': result_oos['annual_return'],
                'max_drawdown': result_oos['max_drawdown'],
                'sharpe_ratio': result_oos['sharpe_ratio'],
                'calmar_ratio': result_oos['calmar_ratio'],
                'n_trades': result_oos['n_trades']
            }
        })
    
    # === 保存优化报告 ===
    output_dir = paths_cfg['output_dir']
    os.makedirs(output_dir, exist_ok=True)
    
    report_file = os.path.join(
        output_dir, 
        f'wf_optimization_report_{wf_window.current_date_str.replace("-", "")}.json'
    )
    
    report_data = {
        'optimization_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'window_config': {
            'current_date': wf_window.current_date_str,
            'universe_period': wf_window.get_universe_period(),
            'ins_sample_period': wf_window.get_ins_sample_period(),
            'oos_sample_period': wf_window.get_oos_sample_period()
        },
        'results': all_results
    }
    
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)

    logger.info(f"\n优化报告已保存到：{report_file}")

    # === 选择实盘交易股票：根据收益排序，选择理论收益最大的 n 只 ===
    trading_stocks_count = max(1, len(stock_pool))
    df_results = pd.DataFrame(all_results)
    df_results = df_results.sort_values('out_of_sample_return', ascending=False)
    wf_trading_stocks = df_results.head(trading_stocks_count)['code'].tolist()

    logger.info(f"\n实盘交易股票（按收益排序）:")
    for i, code in enumerate(wf_trading_stocks):
        row = df_results[df_results['code'] == code].iloc[0]
        logger.info(f"  {i+1}. {code} - 样本外收益: {row['out_of_sample_return']*100:.2f}%")

    # 保存实盘股票到 config_state.json
    from utils import load_state, save_state
    state = load_state()
    state['trading_stocks'] = wf_trading_stocks
    state['optimization_history'] = {
        r['code']: {
            'best_params': r.get('best_params', {}),
            'out_of_sample_return': r.get('out_of_sample_return', 0),
            'out_of_sample_calmar': r.get('out_of_sample_calmar', 0),
            'out_of_sample_drawdown': r.get('out_of_sample_drawdown', 0)
        }
        for r in all_results
    }
    save_state(state)
    logger.info(f"实盘股票已保存到 config_state.json")

    return all_results


# ==================== Walk-Forward 完整流程 ====================

def run_walk_forward_analysis(config: dict, current_date: datetime = None, 
                               rolling_period: str = None) -> Dict:
    """
    执行完整的 Walk-Forward 分析流程
    
    流程:
    1. 创建 Walk-Forward 窗口（当前日期 T）
    2. 构建选股池（使用 T-1.5Y 至 T-3M 数据）
    3. 参数优化（使用 T-1Y 至 T-3M 数据）
    4. 样本外验证（使用 T-3M 至 T 数据）
    5. 滚动窗口（如果指定 rolling_period）
    
    参数:
        config: 配置字典
        current_date: 当前日期（默认今天）
        rolling_period: 滚动周期（如 '1m' 表示每月滚动）
    
    返回:
        包含所有滚动窗口结果的字典
    """
    logger.info("=" * 70)
    logger.info("Walk-Forward 完整分析流程")
    logger.info("=" * 70)
    
    paths_cfg = config.get('paths', {})
    output_dir = paths_cfg['output_dir']
    os.makedirs(output_dir, exist_ok=True)
    
    # 存储所有滚动窗口的结果
    all_rolling_results = []
    
    # 初始化当前窗口
    wf_window = WalkForwardWindow(current_date)
    
    # 滚动次数（如果启用滚动）
    max_rolls = 12 if rolling_period else 1  # 默认最多滚动 12 次
    
    roll_count = 0
    
    while wf_window is not None:
        roll_count += 1
        logger.info(f"\n{'#'*70}")
        logger.info(f"# Walk-Forward 窗口 #{roll_count}")
        logger.info(f"# 当前日期：{wf_window.current_date_str}")
        logger.info(f"{'#'*70}\n")
        
        # === 步骤 1: 构建选股池 ===
        df_universe = build_universe_with_wf(config, wf_window)
        
        if df_universe.empty:
            logger.warning("选股池为空，停止分析")
            break
        
        # 获取选股池列表
        stock_pool = df_universe['code'].tolist()
        
        # === 步骤 2: 参数优化 ===
        optimization_results = optimize_parameters_wf(config, wf_window, stock_pool)
        
        if not optimization_results:
            logger.warning("优化结果为空，停止分析")
            break
        
        # === 步骤 3: 保存当前窗口结果 ===
        window_result = {
            'window_date': wf_window.current_date_str,
            'universe_count': len(stock_pool),
            'optimization_results': optimization_results
        }
        all_rolling_results.append(window_result)
        
        # === 步骤 4: 滚动窗口 ===
        if rolling_period and roll_count < max_rolls:
            logger.info(f"\n准备滚动窗口：{rolling_period}")
            wf_window = wf_window.roll_forward(rolling_period)
            
            # 检查是否超过当前实际日期
            if wf_window.current_date > datetime.now():
                logger.info("滚动窗口已超过当前日期，停止滚动")
                wf_window = None
        else:
            wf_window = None
    
    # === 保存汇总报告 ===
    summary_file = os.path.join(
        output_dir,
        f'wf_summary_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    )
    
    summary_data = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'rolling_period': rolling_period,
        'total_windows': len(all_rolling_results),
        'windows': all_rolling_results
    }
    
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(summary_data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"\n{'='*70}")
    logger.info("Walk-Forward 分析完成")
    logger.info(f"共执行 {len(all_rolling_results)} 个窗口")
    logger.info(f"汇总报告已保存到：{summary_file}")
    logger.info(f"{'='*70}")
    
    return summary_data


# ==================== 主调度函数 ====================

def execute_strategy(mode: str = None, config_path: str = "config.yaml"):
    """
    策略执行入口
    
    参数:
        mode: 运行模式 ('select', 'optimize', 'signal')
        config_path: 配置文件路径
    """
    # 加载配置
    config = load_config(config_path)
    
    # 如果未指定模式，使用配置文件中的模式
    if mode is None:
        mode = config.get('mode', 'select')
    
    logger.info(f"\n当前运行模式：{mode.upper()}")
    
    try:
        if mode == 'select':
            result = run_selection(config)
            return result
        elif mode == 'optimize':
            result = run_optimization(config)
            return result
        elif mode == 'signal':
            result = generate_signals(config)
            return result
        else:
            logger.error(f"未知模式：{mode}")
            return None
            
    except Exception as e:
        logger.exception(f"策略执行失败：{str(e)}")
        raise


class SignalStabilizer:
    """
    信号稳定性过滤器（无状态设计，调用方维护实例）。

    职责：
    - 维护 signal_history (maxlen=3)：每只股票最近3日得分
    - 维护 cooldown_tracker：剔除观察期追踪
    - 输出 trade_signals：T日生成 → T+1日9:30执行

    使用方式：
        stabilizer = SignalStabilizer()
        # T日:
        threshold = screener._get_threshold(scores)
        result = stabilizer.update(daily_signals_df, current_date, threshold)
        # T+1日9:30执行 result[result["action"]=="buy"]
    """

    def __init__(self, config: Optional[dict] = None):
        """
        初始化信号稳定性过滤器。

        Parameters:
            config: 配置字典（可选）
        """
        self.signal_stability_days = 3  # 连续3日达标
        self.cooldown_margin = 0.05     # 阈值下方5%以内触发冷却
        self.cooldown_days = 2           # 冷却期2日
        self.signal_history = {}          # {code: deque(maxlen=3) of scores}
        self.cooldown_tracker = {}       # {code: remaining_cooldown_days}

    def update(self, daily_signals: pd.DataFrame, current_date: str,
               daily_threshold: float) -> pd.DataFrame:
        """
        输入当日截面打分，输出可交易信号。

        Parameters:
            daily_signals: screener.screen() 输出的 DataFrame，含 code, total_score
            current_date: 当前日期字符串 "YYYY-MM-DD"
            daily_threshold: 当日动态阈值（来自 screener._get_threshold）

        Returns:
            pd.DataFrame: 含可交易信号的 DataFrame (columns: code, score, action, reason)
        """
        from collections import deque

        trade_signals = []
        for _, row in daily_signals.iterrows():
            code, score = row["code"], row["total_score"]

            # 1. 更新历史
            hist = self.signal_history.setdefault(code, deque(maxlen=3))
            hist.append(score)

            # 2. 冷却期递减
            if code in self.cooldown_tracker:
                self.cooldown_tracker[code] -= 1
                if self.cooldown_tracker[code] <= 0:
                    del self.cooldown_tracker[code]
                trade_signals.append({"code": code, "score": score, "action": "cooldown", "reason": "waiting"})
                continue

            # 3. 稳定性检查：连续3日均 >= daily_threshold
            if len(hist) < 3:
                trade_signals.append({"code": code, "score": score, "action": "watch", "reason": f"history={len(hist)}/3"})
                continue

            if min(hist) >= daily_threshold:
                trade_signals.append({"code": code, "score": score, "action": "buy", "reason": "stable_3d"})
                self.cooldown_tracker[code] = self.cooldown_days  # 触发后进入冷却
            else:
                trade_signals.append({"code": code, "score": score, "action": "skip", "reason": "below_threshold"})

        return pd.DataFrame(trade_signals)

    def prune_suspended(self, active_codes: List[str]):
        """
        停牌标的自动从历史缓存剔除。

        Parameters:
            active_codes: 当前在交易的有效代码列表
        """
        for code in list(self.signal_history.keys()):
            if code not in active_codes:
                del self.signal_history[code]
        for code in list(self.cooldown_tracker.keys()):
            if code not in active_codes:
                del self.cooldown_tracker[code]


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
