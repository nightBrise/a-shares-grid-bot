"""
模拟盘交易模块 — 收盘后统一模拟模式
功能：每日收盘后运行，增量更新数据、生成信号、模拟执行、更新持仓
"""

import logging
from datetime import datetime
from typing import Dict, List

import pandas as pd

from broker_adapter.mock_adapter import MockAdapter
from data_layer.fetcher import incremental_update
from data_layer.market_db import (
    get_stock_data, get_latest_data_date, save_paper_account,
    get_paper_account, save_paper_daily_snapshot
)
from risk_management.circuit_breaker import create_risk_control_manager
from trading_core.strategy import generate_signals
from utils.utils import load_state

logger = logging.getLogger("grid_trading")


def is_trading_day(date: datetime) -> bool:
    """判断是否为交易日（简化：周一至周五，排除节假日需外部数据源）"""
    return date.weekday() < 5


def get_today_data(stocks: List[str], data_dir: str = "./data") -> Dict[str, Dict]:
    """
    获取当日收盘价数据

    返回:
        {code: {'close': price, 'open': price, 'high': price, 'low': price}, ...}
    """
    today = datetime.now().strftime('%Y-%m-%d')
    today_data = {}

    for code in stocks:
        df = get_stock_data(code, start_date=today, end_date=today, data_dir=data_dir)
        if df is not None and not df.empty:
            row = df.iloc[-1]
            today_data[code] = {
                'close': float(row['close']),
                'open': float(row['open']) if 'open' in row else float(row['close']),
                'high': float(row['high']) if 'high' in row else float(row['close']),
                'low': float(row['low']) if 'low' in row else float(row['close']),
            }
        else:
            logger.warning(f"{code} 无当日数据")

    return today_data


def update_data_to_today(stocks: List[str], data_dir: str = "./data") -> None:
    """
    检查并增量更新数据到今天收盘
    """
    logger.info("=" * 70)
    logger.info("检查并更新数据...")
    logger.info("=" * 70)

    today = datetime.now().strftime('%Y-%m-%d')
    updated_count = 0
    skipped_count = 0

    for code in stocks:
        latest_date = get_latest_data_date(code, data_dir)

        if latest_date is None:
            logger.info(f"{code} 无历史数据，执行全量更新...")
            incremental_update(code, data_dir=data_dir)
            updated_count += 1
        elif latest_date < today:
            logger.info(f"{code} 数据需要更新: {latest_date} -> {today}")
            incremental_update(code, data_dir=data_dir)
            updated_count += 1
        else:
            logger.debug(f"{code} 数据已是最新 ({latest_date})")
            skipped_count += 1

    logger.info(f"数据更新完成：{updated_count} 只更新，{skipped_count} 只已最新")


def is_triggered(signal: pd.Series, today_data: Dict) -> bool:
    """
    检查今日是否触发信号（用 High/Low 判断）

    买入信号：当日最低价 ≤ 预设买入价（盘中触及过）
    卖出信号：当日最高价 ≥ 预设卖出价（盘中触及过）
    """
    code = signal['code']
    day = today_data.get(code, {})
    if not day:
        return False

    low = day.get('low', day.get('close', 0))
    high = day.get('high', day.get('close', 0))

    if signal['direction'] == "buy":
        return low <= signal['price']
    else:  # sell
        return high >= signal['price']


def execute_paper_signal(signal, close_price: float, broker: MockAdapter,
                         risk_manager, account, positions) -> bool:
    """
    执行单个模拟信号

    返回:
        是否成功执行
    """
    code = signal['code']
    direction = signal['direction']
    quantity = int(signal['quantity'])

    # 二次风控确认
    if direction == "buy" and not risk_manager.should_allow_buy(code):
        logger.warning(f"风控拦截：{code} 买入被禁止")
        return False

    if direction == "buy":
        # 检查资金
        needed = close_price * quantity * (1 + broker.fee_rate)
        if account.cash < needed:
            logger.warning(f"资金不足：{code} 买入需要 {needed:,.2f}，可用 {account.cash:,.2f}")
            return False

        # 模拟买入
        result = broker.buy(code, close_price, quantity)
        if result.status == "filled":
            logger.info(f"模拟买入：{code} {quantity}股 @ {close_price:.2f}，费用 {result.fee:.2f}")
            return True
        else:
            logger.warning(f"买入失败：{code} {result.message}")
            return False

    else:  # sell
        # 检查可卖持仓（T+1）
        pos = None
        for p in positions:
            if p.code == code:
                pos = p
                break

        if pos is None or pos.available_quantity < quantity:
            logger.warning(f"可卖持仓不足：{code} 需要 {quantity}，可用 {pos.available_quantity if pos else 0}")
            return False

        # 模拟卖出
        result = broker.sell(code, close_price, quantity)
        if result.status == "filled":
            logger.info(f"模拟卖出：{code} {quantity}股 @ {close_price:.2f}，费用 {result.fee:.2f}，盈亏 {result.message}")
            return True
        else:
            logger.warning(f"卖出失败：{code} {result.message}")
            return False


def daily_settlement(broker: MockAdapter, today_data: Dict[str, Dict]) -> None:
    """
    每日结算：更新持仓市值、计算回撤、释放 T+1
    """
    logger.info("=" * 70)
    logger.info("每日结算...")
    logger.info("=" * 70)

    # 1. 获取当前持仓和账户
    positions = broker.get_positions()
    account = broker.get_account()

    # 2. 更新持仓市值
    for pos in positions:
        quote = today_data.get(pos.code)
        if quote:
            pos.market_value = quote['close'] * pos.quantity

    # 3. 计算总资产
    total_value = account.cash + sum(p.market_value for p in positions)

    # 4. 计算回撤
    acc_dict = get_paper_account(broker.data_dir)
    peak_value = acc_dict['peak_value'] if acc_dict else total_value

    if total_value > peak_value:
        peak_value = total_value
        max_drawdown = 0.0
    else:
        max_drawdown = (peak_value - total_value) / peak_value if peak_value > 0 else 0.0

    # 5. 保存账户状态
    save_paper_account(
        cash=account.cash,
        total_value=total_value,
        peak_value=peak_value,
        max_drawdown=max_drawdown,
        data_dir=broker.data_dir
    )

    # 6. 释放 T+1 冻结持仓
    broker.release_t1_positions()

    # 7. 保存每日快照
    save_paper_daily_snapshot(
        date=datetime.now().strftime('%Y-%m-%d'),
        cash=account.cash,
        total_value=total_value,
        market_value=sum(p.market_value for p in positions),
        max_drawdown=max_drawdown,
        daily_pnl=total_value - (acc_dict['total_value'] if acc_dict else total_value),
        trade_count=0,  # 在交易记录中统计
        positions=[{
            'code': p.code,
            'quantity': p.quantity,
            'available': p.available_quantity,
            'avg_cost': p.avg_cost_price,
            'market_value': p.market_value
        } for p in positions],
        data_dir=broker.data_dir
    )

    logger.info(f"结算完成：现金 {account.cash:,.2f}，总市值 {total_value:,.2f}，回撤 {max_drawdown*100:.2f}%")


def run_paper_trading(config: dict, reset: bool = False) -> bool:
    """
    运行模拟盘（收盘后统一模拟）

    参数:
        config: 配置字典
        reset: 是否重置持仓和资金

    返回:
        是否成功
    """
    logger.info("=" * 70)
    logger.info("启动模拟盘交易")
    logger.info("=" * 70)

    # 获取配置 - 优先使用 trading_stocks（经优化筛选的股票）
    state = load_state()
    stocks = state.get('trading_stocks', [])
    if not stocks:
        # 没有 trading_stocks 时使用 optimized_stocks
        stocks = state.get('optimized_stocks', [])
    if not stocks:
        # 最后回退到 config 中的 stocks
        stocks = config.get('stocks', [])
    if not stocks:
        logger.error("未配置交易股票，请先运行选股和优化")
        return False

    # 根据资金和风控限制，筛选进入模拟盘的股票
    max_positions = config.get('risk', {}).get('max_positions', 5)
    if len(stocks) > max_positions:
        logger.info(f"股票数量 {len(stocks)} 超过 max_positions {max_positions}，按优化收益排序取前 {max_positions} 只")
        opt_history = state.get('optimization_history', {})
        stocks_with_return = [
            (code, opt_history.get(code, {}).get('final_return', 0))
            for code in stocks
        ]
        stocks_with_return.sort(key=lambda x: x[1], reverse=True)
        stocks = [code for code, _ in stocks_with_return[:max_positions]]
        logger.info(f"筛选后进入模拟盘: {stocks}")

    data_dir = config.get('paths', {}).get('data_dir', './data')

    # 初始化模拟盘适配器
    broker = MockAdapter(config)
    broker.connect(config)

    # 重置账户
    if reset:
        initial_cash = config.get('paper_trading', {}).get('initial_cash', 1000000)
        from data_layer.market_db import init_paper_tables
        init_paper_tables(data_dir)
        from data_layer.market_db import _get_db_path
        import sqlite3
        db_path = _get_db_path(data_dir)
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM paper_positions")
            conn.execute("DELETE FROM paper_trades")
            conn.commit()
        save_paper_account(
            cash=initial_cash,
            total_value=initial_cash,
            peak_value=initial_cash,
            max_drawdown=0.0,
            data_dir=data_dir
        )

    # 数据准备：检查并增量更新数据到今天收盘
    update_data_to_today(stocks, data_dir)

    # 同步模拟盘账户状态到 config_state.json（供 generate_signals 风控使用）
    paper_account = broker.get_account()
    paper_positions = broker.get_positions()
    state = load_state()
    state['cash'] = paper_account.cash
    state['peak_value'] = max(paper_account.total_value, config.get('paper_trading', {}).get('initial_cash', 1000000))
    positions_dict = {}
    for p in paper_positions:
        entry = {'code': p.code, 'cost_price': p.avg_cost_price, 'quantity': p.quantity}
        positions_dict[p.code] = [entry]
        state[f'{p.code}_available'] = p.available_quantity
    state['positions'] = positions_dict
    from utils.utils import save_state
    save_state(state)

    # 生成信号（复用现有 generate_signals()）
    logger.info("=" * 70)
    logger.info("生成交易信号...")
    logger.info("=" * 70)

    try:
        signals_df = generate_signals(config)
    except Exception as e:
        logger.exception(f"信号生成失败：{e}")
        return False

    if signals_df is None or signals_df.empty:
        logger.warning("今日无交易信号")
        return True

    logger.info(f"生成 {len(signals_df)} 条信号")

    # 3. 获取当日收盘价
    today_data = get_today_data(stocks, data_dir)
    if not today_data:
        logger.error("无法获取当日行情数据，模拟盘终止")
        return False

    # 4. 加载虚拟持仓和账户
    positions = broker.get_positions()
    account = broker.get_account()

    # 5. 运行风控检查
    logger.info("=" * 70)
    logger.info("运行风控检查...")
    logger.info("=" * 70)

    risk_manager = create_risk_control_manager(config)

    # 构建账户状态用于风控检查
    positions_with_price = []
    for pos in positions:
        quote = today_data.get(pos.code)
        if quote:
            positions_with_price.append({
                'code': pos.code,
                'cost_price': pos.avg_cost_price,
                'current_price': quote['close'],
                'quantity': pos.quantity
            })

    account_status = risk_manager.get_account_status(
        positions_with_price,
        cash=account.cash
    )
    circuit_breaker_state = risk_manager.check_circuit_breaker(account_status)

    if circuit_breaker_state.is_global_breaker:
        logger.error("🚨 全局熔断已触发！暂停所有买入")

    # 6. 模拟执行信号
    logger.info("=" * 70)
    logger.info("模拟执行信号...")
    logger.info("=" * 70)

    executed_count = 0
    for _, signal in signals_df.iterrows():
        code = signal['code']
        today_day = today_data.get(code, {})
        if not today_day:
            continue

        # 检查 High/Low 是否触发信号
        if is_triggered(signal, today_data):
            # 成交价：用网格价格（而非当日收盘价）
            exec_price = signal['price']
            success = execute_paper_signal(
                signal, exec_price, broker, risk_manager, account, positions
            )
            if success:
                executed_count += 1
                # 重新加载持仓和账户（因为已更新）
                positions = broker.get_positions()
                account = broker.get_account()

    logger.info(f"信号执行完成：{executed_count}/{len(signals_df)} 条成交")

    # 7. 每日结算
    daily_settlement(broker, today_data)

    # 8. 生成日报
    generate_paper_report(broker, today_data)

    logger.info("=" * 70)
    logger.info("模拟盘执行完成")
    logger.info("=" * 70)

    return True


def generate_paper_report(broker: MockAdapter, today_data: Dict[str, Dict]) -> None:
    """
    生成模拟盘日报
    """
    account = broker.get_account()
    positions = broker.get_positions()

    logger.info("=" * 70)
    logger.info("模拟盘日报")
    logger.info("=" * 70)
    logger.info(f"日期：{datetime.now().strftime('%Y-%m-%d')}")
    logger.info(f"现金：{account.cash:,.2f}")
    logger.info(f"总市值：{account.total_value:,.2f}")
    logger.info(f"持仓市值：{account.market_value:,.2f}")
    logger.info(f"持仓数量：{len(positions)}")

    if positions:
        logger.info("\n持仓明细：")
        for pos in positions:
            quote = today_data.get(pos.code, {})
            current_price = quote.get('close', pos.current_price)
            pnl = (current_price - pos.avg_cost_price) * pos.quantity
            pnl_pct = (current_price / pos.avg_cost_price - 1) * 100 if pos.avg_cost_price > 0 else 0
            logger.info(f"  {pos.code}: {pos.quantity}股 (可卖{pos.available_quantity}) "
                       f"成本{pos.avg_cost_price:.2f} 现价{current_price:.2f} "
                       f"盈亏{pnl:,.2f} ({pnl_pct:+.2f}%)")

    logger.info("=" * 70)


def get_paper_status(config: dict) -> Dict:
    """
    获取模拟盘状态
    """
    broker = MockAdapter(config)
    broker.connect(config)

    account = broker.get_account()
    positions = broker.get_positions()

    return {
        'cash': account.cash,
        'total_value': account.total_value,
        'market_value': account.market_value,
        'position_count': len(positions),
        'positions': [
            {
                'code': p.code,
                'quantity': p.quantity,
                'available': p.available_quantity,
                'avg_cost': p.avg_cost_price,
                'market_value': p.market_value
            }
            for p in positions
        ]
    }
