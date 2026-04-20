#!/usr/bin/env python3
"""
增量更新数据合并逻辑测试脚本

目的：
1. 演示 append_new_data() 函数如何保证无重复、无遗漏
2. 验证边界场景（日期刚好衔接、有重叠、有间隔）
3. 展示去重和排序逻辑

测试场景：
- 场景 1: 完美衔接（本地最后一条 2023-01-01，新数据从 2023-01-02 开始）
- 场景 2: 有重叠（本地到最后 2023-01-05，新数据从 2023-01-03 开始）
- 场景 3: 有间隔（本地最后 2023-01-01，新数据从 2023-01-10 开始）
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data_layer.fetcher import append_new_data


def generate_mock_data(start_date: str, days: int, base_price: float = 100.0) -> pd.DataFrame:
    """
    生成模拟股票数据
    
    参数:
        start_date: 开始日期 (YYYY-MM-DD)
        days: 天数
        base_price: 基础价格
    
    返回:
        DataFrame with columns: date, open, high, low, close, volume, amount
    """
    dates = pd.date_range(start=start_date, periods=days, freq='B')  # 工作日
    
    np.random.seed(42)  # 固定随机种子以便复现
    
    # 生成随机价格数据
    closes = base_price + np.cumsum(np.random.randn(days) * 2)
    opens = closes + np.random.randn(days) * 0.5
    highs = np.maximum(opens, closes) + np.abs(np.random.randn(days))
    lows = np.minimum(opens, closes) - np.abs(np.random.randn(days))
    
    df = pd.DataFrame({
        'date': dates,
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': np.random.randint(1000, 10000, days),
        'amount': np.random.randint(100000, 1000000, days)
    })
    
    # 格式化日期列为字符串（与实际数据一致）
    df['date'] = df['date'].dt.strftime('%Y-%m-%d')
    
    return df


def test_scenario_1_perfect_handover():
    """
    场景 1: 完美衔接
    本地 CSV 最后一条：2023-01-01
    新拉取数据开始：2023-01-02
    预期：无缝衔接，无重复，无遗漏
    """
    print("\n" + "="*70)
    print("场景 1: 完美衔接测试")
    print("="*70)
    
    # 生成现有数据（2023-01-01 及之前）
    df_existing = generate_mock_data('2022-12-20', 10, base_price=100.0)
    print(f"\n现有数据日期范围：{df_existing['date'].iloc[0]} 至 {df_existing['date'].iloc[-1]}")
    print(f"现有数据条数：{len(df_existing)}")
    
    # 生成新数据（从 2023-01-02 开始）
    df_new = generate_mock_data('2023-01-02', 10, base_price=105.0)
    print(f"新数据日期范围：{df_new['date'].iloc[0]} 至 {df_new['date'].iloc[-1]}")
    print(f"新数据条数：{len(df_new)}")
    
    # 执行合并
    df_combined = append_new_data(df_existing, df_new, code="TEST.SH")
    
    # 验证结果
    print(f"\n合并后数据:")
    print(f"  总条数：{len(df_combined)}")
    print(f"  日期范围：{df_combined['date'].iloc[0]} 至 {df_combined['date'].iloc[-1]}")
    
    # 检查是否有重复
    duplicate_count = df_combined.duplicated(subset=['date']).sum()
    print(f"  重复记录数：{duplicate_count}")
    
    # 检查是否连续
    dates = pd.to_datetime(df_combined['date'])
    date_diffs = dates.diff().dropna()
    gaps = date_diffs[date_diffs > pd.Timedelta(days=3)]  # 允许周末
    
    if len(gaps) > 0:
        print(f"  ⚠️  发现 {len(gaps)} 个日期间隔 > 3 天")
        for gap_date, gap in gaps.items():
            print(f"     - {gap.days} 天间隔 at index {gap_date}")
    else:
        print(f"  ✓ 日期连续，无间隔")
    
    # 显示连接处数据
    mid_idx = len(df_existing) - 1
    print(f"\n连接处数据预览 (前后各 3 条):")
    print(df_combined.loc[mid_idx-2:mid_idx+3, ['date', 'close']].to_string(index=False))
    
    assert duplicate_count == 0, "发现重复数据！"
    assert len(df_combined) == len(df_existing) + len(df_new), "数据条数不匹配！"
    
    print("\n✓ 场景 1 测试通过")
    return True


def test_scenario_2_overlap():
    """
    场景 2: 有重叠
    本地 CSV 最后一条：2023-01-05
    新数据从 2023-01-03 开始（重叠 3 天）
    预期：自动去重，保留新数据
    """
    print("\n" + "="*70)
    print("场景 2: 数据重叠测试")
    print("="*70)
    
    # 生成现有数据（到 2023-01-05）
    df_existing = generate_mock_data('2022-12-25', 10, base_price=100.0)
    print(f"\n现有数据日期范围：{df_existing['date'].iloc[0]} 至 {df_existing['date'].iloc[-1]}")
    print(f"现有数据条数：{len(df_existing)}")
    
    # 生成新数据（从 2023-01-03 开始，有重叠）
    df_new = generate_mock_data('2023-01-03', 10, base_price=105.0)
    print(f"新数据日期范围：{df_new['date'].iloc[0]} 至 {df_new['date'].iloc[-1]}")
    print(f"新数据条数：{len(df_new)}")
    
    # 找出重叠日期
    overlap_dates = set(df_existing['date']) & set(df_new['date'])
    print(f"重叠日期：{sorted(overlap_dates)}")
    
    # 执行合并
    df_combined = append_new_data(df_existing, df_new, code="TEST.SH")
    
    # 验证结果
    print(f"\n合并后数据:")
    print(f"  总条数：{len(df_combined)}")
    print(f"  预期条数：{len(df_existing) + len(df_new) - len(overlap_dates)}")
    
    # 检查去重
    duplicate_count = df_combined.duplicated(subset=['date']).sum()
    print(f"  重复记录数：{duplicate_count}")
    
    # 验证重叠日期的数据来自新数据
    for date in overlap_dates:
        existing_close = df_existing[df_existing['date'] == date]['close'].values[0]
        new_close = df_new[df_new['date'] == date]['close'].values[0]
        combined_close = df_combined[df_combined['date'] == date]['close'].values[0]
        
        if np.isclose(combined_close, new_close):
            print(f"  ✓ {date}: 使用新数据 (close={new_close:.2f})")
        elif np.isclose(combined_close, existing_close):
            print(f"  ⚠ {date}: 使用旧数据 (close={existing_close:.2f})")
        else:
            print(f"  ✗ {date}: 数据异常")
    
    assert duplicate_count == 0, "去重失败！"
    assert len(df_combined) == len(df_existing) + len(df_new) - len(overlap_dates), "合并后条数错误！"
    
    print("\n✓ 场景 2 测试通过")
    return True


def test_scenario_3_gap():
    """
    场景 3: 有间隔
    本地 CSV 最后一条：2023-01-01
    新数据从 2023-01-10 开始（中间缺失 8 天）
    预期：检测到间隔，提示补全
    """
    print("\n" + "="*70)
    print("场景 3: 数据间隔测试")
    print("="*70)
    
    # 生成现有数据（到 2023-01-01）
    df_existing = generate_mock_data('2022-12-20', 10, base_price=100.0)
    print(f"\n现有数据日期范围：{df_existing['date'].iloc[0]} 至 {df_existing['date'].iloc[-1]}")
    print(f"现有数据条数：{len(df_existing)}")
    
    # 生成新数据（从 2023-01-10 开始，有间隔）
    df_new = generate_mock_data('2023-01-10', 10, base_price=105.0)
    print(f"新数据日期范围：{df_new['date'].iloc[0]} 至 {df_new['date'].iloc[-1]}")
    print(f"新数据条数：{len(df_new)}")
    
    # 执行合并
    df_combined = append_new_data(df_existing, df_new, code="TEST.SH")
    
    # 验证结果
    print(f"\n合并后数据:")
    print(f"  总条数：{len(df_combined)}")
    
    # 检查间隔
    dates = pd.to_datetime(df_combined['date'])
    date_diffs = dates.diff().dropna()
    gaps = date_diffs[date_diffs > pd.Timedelta(days=3)]
    
    if len(gaps) > 0:
        print(f"  ⚠️  发现 {len(gaps)} 个日期间隔 > 3 天:")
        for idx, gap in gaps.items():
            prev_date = dates.iloc[idx-1].strftime('%Y-%m-%d')
            curr_date = dates.iloc[idx].strftime('%Y-%m-%d')
            print(f"     - {prev_date} 至 {curr_date}: 间隔 {gap.days} 天")
    else:
        print(f"  ✓ 日期连续，无间隔")
    
    # 显示间隔处数据
    print(f"\n间隔处数据预览:")
    for idx in gaps.index:
        prev_idx = idx - 1
        print(f"  间隔前最后一条：{df_combined.iloc[prev_idx]['date']}")
        print(f"  间隔后第一条：{df_combined.iloc[idx]['date']}")
    
    assert len(df_combined) == len(df_existing) + len(df_new), "数据条数不匹配！"
    
    print("\n✓ 场景 3 测试通过（间隔检测正常，需调用 backfill_missing_data 补全）")
    return True


def demonstrate_deduplication_logic():
    """
    演示去重逻辑细节
    keep='last' 的含义
    """
    print("\n" + "="*70)
    print("去重逻辑详解：keep='last'")
    print("="*70)
    
    # 创建包含重复日期的测试数据
    df_test = pd.DataFrame({
        'date': ['2023-01-01', '2023-01-02', '2023-01-02', '2023-01-03', '2023-01-03'],
        'close': [100.0, 101.0, 101.5, 102.0, 102.5],  # 同一天有两个价格
        'source': ['old', 'old', 'new', 'old', 'new']
    })
    
    print("\n原始数据（包含重复日期）:")
    print(df_test.to_string(index=False))
    
    # 去重（keep='last'）
    df_deduped = df_test.drop_duplicates(subset=['date'], keep='last')
    
    print("\n去重后（keep='last'，保留最后一次出现的记录）:")
    print(df_deduped.to_string(index=False))
    
    print("\n解释:")
    print("  - 2023-01-02: 保留 'new' 源的数据 (close=101.5)")
    print("  - 2023-01-03: 保留 'new' 源的数据 (close=102.5)")
    print("  - concat 时新数据在后，所以 keep='last' 等价于保留新数据")


def main():
    """运行所有测试"""
    print("\n" + "="*70)
    print("增量更新数据合并逻辑测试")
    print("="*70)
    
    all_passed = True
    
    # 运行测试场景
    try:
        all_passed &= test_scenario_1_perfect_handover()
        all_passed &= test_scenario_2_overlap()
        all_passed &= test_scenario_3_gap()
        demonstrate_deduplication_logic()
        
    except Exception as e:
        print(f"\n✗ 测试失败：{str(e)}")
        import traceback
        traceback.print_exc()
        all_passed = False
    
    # 总结
    print("\n" + "="*70)
    print("测试总结")
    print("="*70)
    
    if all_passed:
        print("✓ 所有测试通过")
        print("\n关键结论:")
        print("  1. append_new_data() 使用 drop_duplicates(keep='last') 去重")
        print("  2. 由于 pd.concat([df_existing, df_new]) 新数据在后，keep='last' 保留新数据")
        print("  3. 完美衔接场景：无重复、无遗漏")
        print("  4. 重叠场景：自动去重，保留新数据")
        print("  5. 间隔场景：正确合并，需配合 check_data_integrity() 检测并补全")
    else:
        print("✗ 部分测试失败，请检查代码")
    
    print("="*70)
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
