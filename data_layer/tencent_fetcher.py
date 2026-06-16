"""
腾讯财经数据获取模块 - 作为 AkShare/Baostock 的备用数据源
"""

import logging
import time
from datetime import datetime

import pandas as pd

from data_layer.session_manager import get_session

logger = logging.getLogger("grid_trading")


def _code_to_tencent(code: str) -> str:
    """转换为腾讯格式: 000001.SZ -> sz000001"""
    symbol, exchange = code.split('.')
    return f"{exchange.lower()}{symbol}"


def fetch_from_tencent(code: str, start_date: str = None,
                       end_date: str = None, adjust: str = "qfq") -> pd.DataFrame:
    """
    从腾讯财经获取股票历史K线数据（支持多段请求覆盖完整历史）

    参数:
        code: 股票代码 (格式：000001.SZ)
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)
        adjust: 复权类型 ('qfq': 前复权)

    返回:
        DataFrame 包含：date, open, close, high, low, volume
    """
    tencent_code = _code_to_tencent(code)

    if not start_date:
        start_date = '2020-01-01'
    if not end_date:
        end_date = datetime.now().strftime('%Y-%m-%d')

    from utils.utils import fmt_date

    start_date = fmt_date(start_date)
    end_date = fmt_date(end_date)

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    all_rows = []
    current_start = start_date
    max_segments = 5  # 最多分段请求次数，防止无限循环

    for segment in range(max_segments):
        params = {
            "param": f"{tencent_code},day,{current_start},{end_date},1000,{adjust}"
        }
        try:
            resp = get_session().get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            stock_data = data.get("data", {}).get(tencent_code, {})
            klines = stock_data.get(f"{adjust}day", []) or stock_data.get("day", [])

            if not klines:
                break

            for item in klines:
                if len(item) >= 6:
                    all_rows.append({
                        "date": item[0],
                        "open": float(item[1]),
                        "close": float(item[2]),
                        "high": float(item[3]),
                        "low": float(item[4]),
                        "volume": float(item[5]),
                    })

            # 检查是否覆盖了完整的起始日期
            earliest_date = klines[0][0]
            if earliest_date <= current_start:
                # 已覆盖到请求的起始日期
                break

            # 还有更早的数据，继续请求
            # 将 end_date 设为 earliest_date 前一天
            from datetime import timedelta
            earliest_dt = datetime.strptime(earliest_date, '%Y-%m-%d')
            next_end = (earliest_dt - timedelta(days=1)).strftime('%Y-%m-%d')
            if next_end < start_date:
                next_end = start_date
            current_start = start_date
            end_date = next_end
            time.sleep(0.3)  # 分段请求间短暂延迟

        except Exception as e:
            logger.error(f"✗ {code} 腾讯API第{segment+1}段获取失败: {e}")
            break

    if not all_rows:
        logger.warning(f"{code} 腾讯API返回空数据")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

    logger.info(f"✓ {code} 腾讯API成功获取 {len(df)} 条记录 (共{segment+1}段)")
    return df


