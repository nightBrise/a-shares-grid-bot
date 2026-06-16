#!/usr/bin/env python3
"""
增量更新数据合并逻辑测试脚本（独立版本）

直接测试 append_new_data() 函数的核心逻辑，不依赖外部数据源
"""

import pandas as pd
import numpy as np
# # from datetime import datetime, timedelta
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


def append_new_data(df_existing: pd.DataFrame, df_new: pd.DataFrame, code: str) -> pd.DataFrame:
    """
    将新数据追加到现有数据，去重并排序
    
    核心逻辑:
    1. pd.concat() 合并两个 DataFrame
    2. drop_duplicates(subset=['date'], keep='last') 去重，保留新数据
    3. sort_values('date') 按日期排序
    4. reset_index(drop=True) 重置索引
    """
    if df_existing.empty:
        return df_new
    
    if df_new.empty:
        return df_existing
    
    # === 关键步骤 1: 合并数据 ===
    df_combined = pd.concat([df_existing, df_new], ignore_index=True)
    
    # === 关键步骤 2: 按日期去重（保留最新记录） ===
    # keep='last' 的含义：保留最后一次出现的记录
    # 由于 pd.concat([旧，新])，新数据在后，所以保留新数据
    df_combined = df_combined.drop_duplicates(subset=['date'], keep='last')
    
    # === 关键步骤 3: 按日期排序 ===
    df_combined = df_combined.sort_values('date').reset_index(drop=True)
    
    logger.info(f"{code} 数据合并完成：原有{len(df_existing)}条，新增{len(df_new)}条，合并后{len(df_combined)}条")
    
    return df_combined


def generate_mock_data(start_date: str, days: int, base_price: float = 100.0, seed: int = 42) -> pd.DataFrame:
    """生成模拟股票数据"""
    dates = pd.date_range(start=start_date, periods=days, freq='B')
    
    np.random.seed(seed)
    closes = base_price + np.cumsum(np.random.randn(days) * 2)
    
    df = pd.DataFrame({
        'date': dates.strftime('%Y-%m-%d'),
        'open': closes + np.random.randn(days) * 0.5,
        'high': closes + np.abs(np.random.randn(days)),
        'low': closes - np.abs(np.random.randn(days)),
        'close': closes,
        'volume': np.random.randint(1000, 10000, days),
    })
    
    return df


def test_perfect_handover():
    """场景 1: 完美衔接"""
    print("\n" + "="*70)
    print("场景 1: 完美衔接测试")
    print("="*70)
    print("本地 CSV 最后一条：2023-01-01")
    print("新拉取数据开始：2023-01-02")
    print("预期：无缝衔接，无重复，无遗漏\n")
    
    # 现有数据：2022-12-20 至 2023-01-01 (10 个工作日)
    df_existing = generate_mock_data('2022-12-20', 10, base_price=100.0)
    print(f"现有数据：{df_existing['date'].iloc[0]} 至 {df_existing['date'].iloc[-1]} ({len(df_existing)}条)")
    
    # 新数据：2023-01-02 至 2023-01-13 (10 个工作日)
    df_new = generate_mock_data('2023-01-02', 10, base_price=105.0, seed=43)
    print(f"新数据：{df_new['date'].iloc[0]} 至 {df_new['date'].iloc[-1]} ({len(df_new)}条)")
    
    # 执行合并
    df_combined = append_new_data(df_existing, df_new, "TEST.SH")
    
    # 验证
    print("\n合并结果:")
    print(f"  总条数：{len(df_combined)}")
    print(f"  日期范围：{df_combined['date'].iloc[0]} 至 {df_combined['date'].iloc[-1]}")
    
    # 检查重复
    duplicates = df_combined.duplicated(subset=['date']).sum()
    print(f"  重复记录：{duplicates}")
    
    # 检查间隔
    dates = pd.to_datetime(df_combined['date'])
    gaps = dates.diff().dt.days
    large_gaps = gaps[gaps > 3]
    
    if len(large_gaps) == 0:
        print("  日期间隔：✓ 连续无间隔")
    else:
        print(f"  日期间隔：⚠️ 发现 {len(large_gaps)} 个间隔>3 天")
    
    # 显示连接处
    mid_idx = len(df_existing) - 1
    print("\n连接处详情:")
    print(df_combined.loc[mid_idx-2:mid_idx+2, ['date', 'close']].to_string(index=False))
    
    # 断言
    assert duplicates == 0, "发现重复！"
    assert len(df_combined) == len(df_existing) + len(df_new), "条数不匹配！"
    
    print("\n✓ 测试通过：完美衔接，无重复无遗漏")
    return True


def test_overlap():
    """场景 2: 数据重叠"""
    print("\n" + "="*70)
    print("场景 2: 数据重叠测试")
    print("="*70)
    print("本地 CSV 最后一条：2023-01-05")
    print("新拉取数据开始：2023-01-03 (重叠 3 天)")
    print("预期：自动去重，保留新数据\n")
    
    # 现有数据：2022-12-25 至 2023-01-05
    df_existing = generate_mock_data('2022-12-25', 10, base_price=100.0)
    print(f"现有数据：{df_existing['date'].iloc[0]} 至 {df_existing['date'].iloc[-1]} ({len(df_existing)}条)")
    
    # 新数据：2023-01-03 至 2023-01-14 (重叠 3 天：01-03, 01-04, 01-05)
    df_new = generate_mock_data('2023-01-03', 10, base_price=105.0, seed=43)
    print(f"新数据：{df_new['date'].iloc[0]} 至 {df_new['date'].iloc[-1]} ({len(df_new)}条)")
    
    # 找出重叠日期
    overlap = set(df_existing['date']) & set(df_new['date'])
    print(f"重叠日期：{sorted(overlap)}")
    
    # 合并
    df_combined = append_new_data(df_existing, df_new, "TEST.SH")
    
    print("\n合并结果:")
    print(f"  理论条数：{len(df_existing) + len(df_new) - len(overlap)}")
    print(f"  实际条数：{len(df_combined)}")
    
    duplicates = df_combined.duplicated(subset=['date']).sum()
    print(f"  重复记录：{duplicates}")
    
    # 验证重叠日期使用新数据
    print("\n重叠日期数据验证:")
    for date in sorted(overlap):
        old_close = df_existing[df_existing['date'] == date]['close'].values[0]
        new_close = df_new[df_new['date'] == date]['close'].values[0]
        combined_close = df_combined[df_combined['date'] == date]['close'].values[0]
        
        source = "新数据 ✓" if abs(combined_close - new_close) < 0.01 else "旧数据 ✗"
        print(f"  {date}: 旧={old_close:.2f}, 新={new_close:.2f}, 合并={combined_close:.2f} → {source}")
    
    assert duplicates == 0, "去重失败！"
    assert len(df_combined) == len(df_existing) + len(df_new) - len(overlap), "条数错误！"
    
    print("\n✓ 测试通过：自动去重，保留新数据")
    return True


def test_gap():
    """场景 3: 数据间隔"""
    print("\n" + "="*70)
    print("场景 3: 数据间隔测试")
    print("="*70)
    print("本地 CSV 最后一条：2023-01-01")
    print("新拉取数据开始：2023-01-10 (间隔 8 天)")
    print("预期：正确合并，检测到间隔\n")
    
    # 现有数据：2022-12-20 至 2023-01-01
    df_existing = generate_mock_data('2022-12-20', 10, base_price=100.0)
    print(f"现有数据：{df_existing['date'].iloc[0]} 至 {df_existing['date'].iloc[-1]} ({len(df_existing)}条)")
    
    # 新数据：2023-01-10 至 2023-01-21
    df_new = generate_mock_data('2023-01-10', 10, base_price=105.0, seed=43)
    print(f"新数据：{df_new['date'].iloc[0]} 至 {df_new['date'].iloc[-1]} ({len(df_new)}条)")
    
    # 合并
    df_combined = append_new_data(df_existing, df_new, "TEST.SH")
    
    print("\n合并结果:")
    print(f"  总条数：{len(df_combined)}")
    
    # 检测间隔
    dates = pd.to_datetime(df_combined['date'])
    gaps = dates.diff().dt.days
    large_gaps = gaps[gaps > 3]
    
    if len(large_gaps) > 0:
        print(f"  ⚠️  检测到 {len(large_gaps)} 个间隔>3 天:")
        for idx in large_gaps.index:
            prev_date = dates.iloc[idx-1].strftime('%Y-%m-%d')
            curr_date = dates.iloc[idx].strftime('%Y-%m-%d')
            gap_days = large_gaps.iloc[idx]
            print(f"     - {prev_date} → {curr_date}: 间隔 {gap_days} 天")
        print("  → 需调用 check_data_integrity() 检测并补全缺失日期")
    else:
        print("  ✓ 日期连续")
    
    assert len(df_combined) == len(df_existing) + len(df_new), "条数不匹配！"
    
    print("\n✓ 测试通过：间隔检测正常")
    return True


def main():
    print("="*70)
    print("增量更新数据合并逻辑测试")
    print("="*70)
    
    all_passed = True
    
    try:
        all_passed &= test_perfect_handover()
        all_passed &= test_overlap()
        all_passed &= test_gap()
        
        print("\n" + "="*70)
        print("测试总结")
        print("="*70)
        print("✓ 所有测试通过\n")
        
        print("关键机制:")
        print("  1. concat([df_existing, df_new]) - 新数据在后")
        print("  2. drop_duplicates(subset=['date'], keep='last') - 保留新数据")
        print("  3. sort_values('date') - 按日期排序")
        print("  4. reset_index(drop=True) - 重置索引\n")
        
        print("场景验证:")
        print("  ✓ 完美衔接：无重复、无遗漏")
        print("  ✓ 数据重叠：自动去重、保留新值")
        print("  ✓ 数据间隔：正确合并、检测缺失\n")
        
    except Exception as e:
        print(f"\n✗ 测试失败：{e}")
        import traceback
        traceback.print_exc()
        all_passed = False
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
