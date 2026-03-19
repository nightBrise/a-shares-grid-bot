#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
增量数据更新引擎测试脚本
用于验证 Data Incremental ETL 功能是否正常工作

使用方法:
    python test_incremental_update.py

验收标准:
    - 第一次运行：显示"全量更新"，拉取近 1 年数据
    - 第二次运行：显示"Incremental update: X days data fetched"
    - 检查 data/metadata.json 文件，确认元数据已记录
    - 检查日志中是否有数据完整性检查结果
"""

import os
import sys
import logging
from datetime import datetime, timedelta

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data import (
    get_stock_data, 
    load_metadata, 
    check_data_integrity,
    METADATA_FILE
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("incremental_test")


def test_incremental_update():
    """测试增量更新引擎"""
    
    test_code = "600519.SH"  # 贵州茅台
    data_dir = "./data"
    
    print("\n" + "="*70)
    print("增量数据更新引擎测试")
    print("="*70)
    print(f"测试股票：{test_code}")
    print(f"数据目录：{data_dir}")
    print(f"元数据文件：{os.path.join(data_dir, METADATA_FILE)}")
    print("="*70)
    
    # === 步骤 1: 检查当前元数据状态 ===
    print("\n[步骤 1] 检查当前元数据状态...")
    metadata = load_metadata(data_dir)
    
    if test_code in metadata:
        stock_meta = metadata[test_code]
        print(f"  ✓ 已存在元数据:")
        print(f"    - 最后更新日期：{stock_meta.get('last_update_date', 'N/A')}")
        print(f"    - 记录数：{stock_meta.get('record_count', 'N/A')}")
        print(f"    - 更新模式：{stock_meta.get('update_mode', 'N/A')}")
        print(f"    - 更新时间：{stock_meta.get('update_time', 'N/A')}")
    else:
        print(f"  ⚠ 未找到元数据，首次运行将执行全量更新")
    
    # === 步骤 2: 第一次运行（强制全量更新）===
    print("\n[步骤 2] 第一次运行：强制全量更新...")
    print("-" * 70)
    
    df1 = get_stock_data(
        test_code, 
        data_dir=data_dir,
        force_full=True,  # 强制全量
        enable_incremental=False  # 禁用增量
    )
    
    if df1.empty:
        print(f"\n✗ 获取数据失败，无法继续测试")
        return False
    
    print(f"\n✓ 全量更新完成:")
    print(f"  - 数据形状：{df1.shape}")
    print(f"  - 日期范围：{df1['date'].min().strftime('%Y-%m-%d')} 至 {df1['date'].max().strftime('%Y-%m-%d')}")
    
    # 检查元数据是否已更新
    metadata = load_metadata(data_dir)
    if test_code in metadata:
        print(f"  - 元数据已更新 ✓")
    
    # === 步骤 3: 第二次运行（增量更新）===
    print("\n[步骤 3] 第二次运行：增量更新（关键测试）...")
    print("-" * 70)
    print("预期日志应显示：'Incremental update: X days data fetched for 600519.SH'")
    print("-" * 70)
    
    df2 = get_stock_data(
        test_code,
        data_dir=data_dir,
        enable_incremental=True  # 启用增量
    )
    
    if df2.empty:
        print(f"\n✗ 增量更新失败")
        return False
    
    print(f"\n✓ 增量更新完成:")
    print(f"  - 数据形状：{df2.shape}")
    print(f"  - 日期范围：{df2['date'].min().strftime('%Y-%m-%d')} 至 {df2['date'].max().strftime('%Y-%m-%d')}")
    
    # === 步骤 4: 验证数据完整性 ===
    print("\n[步骤 4] 数据完整性检查...")
    
    is_complete, missing_dates = check_data_integrity(df2, test_code)
    
    if is_complete:
        print(f"  ✓ 数据完整性检查通过")
    else:
        print(f"  ⚠ 发现 {len(missing_dates)} 个缺失日期:")
        if len(missing_dates) <= 10:
            for date in missing_dates:
                print(f"    - {date}")
        else:
            for date in missing_dates[:10]:
                print(f"    - {date}")
            print(f"    ... 还有 {len(missing_dates) - 10} 个日期")
    
    # === 步骤 5: 显示最终元数据 ===
    print("\n[步骤 5] 最终元数据状态...")
    
    metadata = load_metadata(data_dir)
    if test_code in metadata:
        stock_meta = metadata[test_code]
        print(f"  元数据信息:")
        for key, value in stock_meta.items():
            print(f"    {key}: {value}")
    
    # === 测试结果总结 ===
    print("\n" + "="*70)
    print("测试结果总结")
    print("="*70)
    
    # 检查日志输出是否包含预期的增量更新消息
    print("\n请检查上方日志输出，确认是否包含以下内容:")
    print("  ✓ 'Incremental update: X days data fetched for 600519.SH'")
    print("  ✓ 第二次运行时仅请求少量天数数据（而非全量 365 天）")
    print("  ✓ 元数据文件 data/metadata.json 已创建并更新")
    
    print("\n" + "="*70)
    print("测试完成！")
    print("="*70)
    
    return True


def test_backfill():
    """测试数据补全功能"""
    
    from data import backfill_missing_data, check_data_integrity
    
    test_code = "000858.SZ"  # 五粮液
    data_dir = "./data"
    
    print("\n" + "="*70)
    print("数据补全功能测试")
    print("="*70)
    
    # 获取数据
    df = get_stock_data(test_code, data_dir=data_dir, force_full=True)
    
    if df.empty:
        print(f"\n✗ 获取数据失败")
        return False
    
    # 检查完整性
    is_complete, missing = check_data_integrity(df, test_code)
    
    print(f"\n数据完整性检查结果:")
    print(f"  - 数据条数：{len(df)}")
    print(f"  - 是否完整：{'是' if is_complete else '否'}")
    print(f"  - 缺失日期数：{len(missing)}")
    
    if not is_complete and missing:
        print(f"\n开始自动补全缺失数据...")
        success = backfill_missing_data(test_code, missing, data_dir)
        
        if success:
            print(f"✓ 数据补全成功")
            
            # 重新检查
            df_updated = get_stock_data(test_code, data_dir=data_dir)
            is_complete_new, missing_new = check_data_integrity(df_updated, test_code)
            
            print(f"\n补全后检查结果:")
            print(f"  - 新数据条数：{len(df_updated)}")
            print(f"  - 是否完整：{'是' if is_complete_new else '否'}")
            print(f"  - 剩余缺失日期数：{len(missing_new)}")
        else:
            print(f"✗ 数据补全失败")
    
    print("\n" + "="*70)
    print("数据补全功能测试完成")
    print("="*70)
    
    return True


if __name__ == "__main__":
    # 运行主测试
    success = test_incremental_update()
    
    # 可选：运行数据补全测试
    # test_backfill()
    
    sys.exit(0 if success else 1)
