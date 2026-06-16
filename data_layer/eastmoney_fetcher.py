"""
东方财富直接数据获取模块 - 作为 AkShare/Baostock/腾讯的备用数据源

东方财富API特点：
- 提供历史K线数据接口
- 无需登录，Cookie-free
- 但反爬虫较严格，需要合理的请求间隔和UA
"""

import logging
import time
from datetime import datetime

import pandas as pd

from data_layer.session_manager import get_session

logger = logging.getLogger("grid_trading")


def _code_to_eastmoney(code: str) -> tuple:
    """
    转换为东方财富格式
    000001.SZ -> (0, 000001)  # 深圳
    600000.SH -> (1, 600000)  # 上海
    """
    symbol, exchange = code.split('.')
    if exchange.upper() == 'SZ':
        market = 0
    elif exchange.upper() == 'SH':
        market = 1
    elif exchange.upper() == 'BJ':
        market = 0  # 北交所也走深圳
    else:
        market = 0
    return market, symbol


def fetch_from_eastmoney(code: str, start_date: str = None,
                         end_date: str = None, adjust: str = "qfq") -> pd.DataFrame:
    """
    从东方财富获取股票历史K线数据

    参数:
        code: 股票代码 (格式：000001.SZ)
        start_date: 开始日期 (YYYY-MM-DD 或 YYYYMMDD)
        end_date: 结束日期 (YYYY-MM-DD 或 YYYYMMDD)
        adjust: 复权类型 ('qfq': 前复权, 'hfq': 后复权, '': 不复权)

    返回:
        DataFrame 包含：date, open, close, high, low, volume, amount
    """
    market, symbol = _code_to_eastmoney(code)

    from utils.utils import fmt_date

    start_date = fmt_date(start_date)
    end_date = fmt_date(end_date) or datetime.now().strftime('%Y-%m-%d')

    # 复权映射
    adjust_map = {'qfq': '1', 'hfq': '2', '': '0'}
    adjust_flag = adjust_map.get(adjust, '1')

    # 东方财富K线API
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": f"{market}.{symbol}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",  # 101=日K
        "fqt": adjust_flag,
        "beg": start_date.replace('-', ''),
        "end": end_date.replace('-', ''),
        "lmt": "1000",  # 最大条数
        "_": str(int(time.time() * 1000)),
    }

    headers = {
        "Referer": "https://quote.eastmoney.com/",
    }

    try:
        resp = get_session().get(url, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        klines = data.get("data", {}).get("klines", [])
        if not klines:
            logger.warning(f"{code} 东方财富API返回空数据")
            return pd.DataFrame()

        # klines 格式: "日期,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率"
        rows = []
        for item in klines:
            parts = item.split(',')
            if len(parts) >= 6:
                rows.append({
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]) if len(parts) > 6 else 0.0,
                })

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])

        logger.info(f"✓ {code} 东方财富成功获取 {len(df)} 条记录")
        return df

    except Exception as e:
        logger.warning(f"✗ {code} 东方财富获取失败: {e}")
        return pd.DataFrame()


