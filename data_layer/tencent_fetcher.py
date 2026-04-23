"""
腾讯财经数据获取模块 - 作为 AkShare/Baostock 的备用数据源
"""

import json
import logging
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

logger = logging.getLogger("grid_trading")


def _code_to_tencent(code: str) -> str:
    """转换为腾讯格式: 000001.SZ -> sz000001"""
    symbol, exchange = code.split('.')
    return f"{exchange.lower()}{symbol}"


def fetch_from_tencent(code: str, start_date: str = None,
                       end_date: str = None, adjust: str = "qfq") -> pd.DataFrame:
    """
    从腾讯财经获取股票历史K线数据

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
        start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
    if not end_date:
        end_date = datetime.now().strftime('%Y-%m-%d')

    # 转换日期格式
    def fmt_date(d):
        if len(d) == 8 and d.isdigit():
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return d

    start_date = fmt_date(start_date)
    end_date = fmt_date(end_date)

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {
        "param": f"{tencent_code},day,{start_date},{end_date},500,{adjust}"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        stock_data = data.get("data", {}).get(tencent_code, {})
        klines = stock_data.get(f"{adjust}day", []) or stock_data.get("day", [])

        if not klines:
            logger.warning(f"{code} 腾讯API返回空数据")
            return pd.DataFrame()

        # 解析数据: [date, open, close, high, low, volume]
        rows = []
        for item in klines:
            if len(item) >= 6:
                rows.append({
                    "date": item[0],
                    "open": float(item[1]),
                    "close": float(item[2]),
                    "high": float(item[3]),
                    "low": float(item[4]),
                    "volume": float(item[5]),
                })

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])

        logger.info(f"✓ {code} 腾讯API成功获取 {len(df)} 条记录")
        return df

    except Exception as e:
        logger.error(f"✗ {code} 腾讯API获取失败: {e}")
        return pd.DataFrame()


def update_stocks_data(codes: list, data_dir: str = "./data") -> dict:
    """
    批量更新股票数据

    返回:
        {code: success_bool}
    """
    import os
    from data_layer.fetcher import save_quarter_history, update_stock_metadata, get_cache_path, save_to_cache

    results = {}
    today_str = datetime.now().strftime('%Y-%m-%d')

    for code in codes:
        try:
            # 获取近30天数据（增量更新）
            start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            df = fetch_from_tencent(code, start_date=start, end_date=today_str)

            if df.empty:
                results[code] = False
                continue

            # 加载现有缓存
            cache_path = get_cache_path(code, data_dir)
            if os.path.exists(cache_path):
                df_existing = pd.read_parquet(cache_path)
                # 合并去重
                df_combined = pd.concat([df_existing, df], ignore_index=True)
                df_combined = df_combined.drop_duplicates(subset=['date'], keep='last')
                df_combined = df_combined.sort_values('date').reset_index(drop=True)
            else:
                df_combined = df

            # 保存缓存
            save_to_cache(df_combined, cache_path)

            # 保存到季度文件
            df_with_code = df.copy()
            df_with_code['code'] = code
            df_with_code['amount'] = 0  # 腾讯API不提供成交额
            save_quarter_history(df_with_code, data_dir)

            # 更新元数据
            last_date = df_combined['date'].max().strftime('%Y-%m-%d')
            update_stock_metadata(code, last_date, len(df_combined), data_dir,
                                 update_mode='tencent', source='tencent')

            results[code] = True
            logger.info(f"{code} 更新完成: 最新日期 {last_date}, 共 {len(df_combined)} 条")

            time.sleep(0.5)  # 避免请求过快

        except Exception as e:
            logger.error(f"{code} 更新失败: {e}")
            results[code] = False

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 测试
    df = fetch_from_tencent("000001.SZ", start_date="2026-04-01", end_date="2026-04-23")
    print(df.tail())
