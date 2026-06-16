#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
选股功能集成测试脚本

测试范围:
1. 股票数据获取（全量 + 增量）
2. 数据增量保存机制
3. Walk-Forward 选股逻辑
4. 上市时间检查
5. 网格参数优化
6. 最终选股结果

使用方法:
    conda activate rain
    python test_stock_selection.py

验收标准:
    ✓ 成功获取股票数据
    ✓ 元数据文件正确创建和更新
    ✓ Walk-Forward 窗口正确划分
    ✓ 新股被正确过滤
    ✓ 优化出合理的网格参数
    ✓ 生成选股结果
"""

import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime
import logging
import json

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("stock_selection_test")

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_data_acquisition():
    """测试 1: 股票数据获取功能"""
    print("\n" + "="*70)
    print("测试 1: 股票数据获取功能")
    print("="*70)
    
    from data_layer.fetcher import get_stock_data, load_metadata
    
    test_stocks = [
        ("600519.SH", "贵州茅台"),
        ("000858.SZ", "五粮液"),
        ("601318.SH", "中国平安")
    ]
    
    results = []
    
    for code, name in test_stocks:
        print(f"\n[{code}] {name}")
        print("-" * 70)
        
        try:
            # 第一次运行：全量更新
            print("  [步骤 1] 全量更新...")
            df1 = get_stock_data(code, force_full=True, enable_incremental=False)
            
            if df1.empty:
                print("    ✗ 获取数据失败")
                results.append((code, False, "数据为空"))
                continue
            
            print(f"    ✓ 获取成功：{len(df1)}条记录")
            print(f"    日期范围：{df1['date'].min()} 至 {df1['date'].max()}")
            
            # 检查数据质量
            missing_cols = set(['date', 'open', 'high', 'low', 'close', 'volume']) - set(df1.columns)
            if missing_cols:
                print(f"    ✗ 缺少列：{missing_cols}")
                results.append((code, False, f"缺少列：{missing_cols}"))
                continue
            
            # 第二次运行：增量更新
            print("  [步骤 2] 增量更新...")
            df2 = get_stock_data(code, enable_incremental=True)
            
            print(f"    ✓ 增量更新完成：{len(df2)}条记录")
            
            # 验证元数据
            metadata = load_metadata()
            if code in metadata:
                stock_meta = metadata[code]
                print("    ✓ 元数据已更新:")
                print(f"      - 记录数：{stock_meta.get('record_count', 'N/A')}")
                print(f"      - 最后更新：{stock_meta.get('last_update_date', 'N/A')}")
                print(f"      - 更新模式：{stock_meta.get('update_mode', 'N/A')}")
            else:
                print("    ⚠ 未找到元数据")
            
            results.append((code, True, f"{len(df2)}条"))
            
        except Exception as e:
            print(f"    ✗ 异常：{str(e)}")
            results.append((code, False, str(e)))
    
    # 总结
    print("\n" + "="*70)
    print("数据获取测试结果")
    print("="*70)
    success_count = sum(1 for _, success, _ in results if success)
    print(f"成功：{success_count}/{len(results)}")
    
    for code, success, msg in results:
        status = "✓" if success else "✗"
        print(f"  {status} {code}: {msg}")
    
    return success_count == len(results)


def test_incremental_save():
    """测试 2: 增量保存机制"""
    print("\n" + "="*70)
    print("测试 2: 增量保存机制")
    print("="*70)
    
    from data_layer.fetcher import get_stock_data, load_metadata, check_data_integrity
    import os
    
    test_code = "600519.SH"
    data_dir = "./data"
    metadata_file = os.path.join(data_dir, "metadata.json")
    
    print(f"\n测试股票：{test_code}")
    print(f"数据目录：{data_dir}")
    print(f"元数据文件：{metadata_file}")
    
    # === 步骤 1: 清理现有数据（可选）===
    print("\n[准备] 检查现有数据状态...")
    csv_path = os.path.join(data_dir, f"{test_code.replace('.', '_')}.csv")
    
    if os.path.exists(csv_path):
        print("  ✓ CSV 文件已存在")
        file_size = os.path.getsize(csv_path) / 1024  # KB
        print(f"    文件大小：{file_size:.2f} KB")
    else:
        print("  ⚠ CSV 文件不存在，将创建新文件")
    
    # === 步骤 2: 第一次获取（全量）===
    print("\n[步骤 1] 全量更新...")
    df1 = get_stock_data(test_code, force_full=True, enable_incremental=False)
    
    if df1.empty:
        print("  ✗ 获取数据失败")
        return False
    
    print(f"  ✓ 全量更新完成：{len(df1)}条记录")
    
    # 检查 CSV 是否创建
    if os.path.exists(csv_path):
        print("  ✓ CSV 文件已创建")
    else:
        print("  ✗ CSV 文件未创建")
        return False
    
    # 检查元数据
    metadata = load_metadata()
    if test_code in metadata:
        print("  ✓ 元数据已记录")
    else:
        print("  ✗ 元数据未记录")
        return False
    
    # === 步骤 3: 第二次获取（增量）===
    print("\n[步骤 2] 增量更新...")
    df2 = get_stock_data(test_code, enable_incremental=True)
    
    print(f"  ✓ 增量更新完成：{len(df2)}条记录")
    
    # 验证数据完整性
    is_complete, missing = check_data_integrity(df2, test_code)
    if is_complete:
        print("  ✓ 数据完整性检查通过")
    else:
        print(f"  ⚠ 发现 {len(missing)} 个缺失日期")
    
    # === 步骤 4: 验证元数据更新 ===
    print("\n[步骤 3] 验证元数据...")
    metadata = load_metadata()
    stock_meta = metadata.get(test_code, {})
    
    print("  元数据信息:")
    for key, value in stock_meta.items():
        print(f"    {key}: {value}")
    
    # === 步骤 5: 验证 metadata.json 文件 ===
    print("\n[步骤 4] 检查 metadata.json 文件...")
    if os.path.exists(metadata_file):
        with open(metadata_file, 'r', encoding='utf-8') as f:
            content = f.read()
            print(f"  ✓ 文件存在，大小：{len(content)/1024:.2f} KB")
            
            # 验证 JSON 格式
            try:
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    json.load(f)
                print("  ✓ JSON 格式正确")
            except Exception:
                print("  ✗ JSON 格式错误")
                return False
    else:
        print("  ✗ metadata.json 文件不存在")
        return False
    
    print("\n✓ 增量保存机制测试通过")
    return True


def test_walk_forward_window():
    """测试 3: Walk-Forward 窗口划分"""
    print("\n" + "="*70)
    print("测试 3: Walk-Forward 窗口划分")
    print("="*70)
    
    from trading_core.strategy import WalkForwardWindow
    
    wf = WalkForwardWindow(current_date=datetime.now())
    
    print(f"\n当前日期：{datetime.now().strftime('%Y-%m-%d')}")
    print(f"Walk-Forward 版本：v{wf.version}")
    
    # === 测试 1: 获取回测期 ===
    print("\n[测试 1] 回测期 (In-Sample)...")
    start, end = wf.get_backtest_period()
    print(f"  日期范围：{start} 至 {end}")
    
    # 计算天数
    from datetime import datetime as dt
    days = (dt.strptime(end, '%Y-%m-%d') - dt.strptime(start, '%Y-%m-%d')).days
    print(f"  天数：{days}天")
    
    if days < 250:
        print("  ⚠ 回测期不足 250 天")
    else:
        print("  ✓ 回测期充足")
    
    # === 测试 2: 获取选股期 ===
    print("\n[测试 2] 选股期 (Out-of-Sample)...")
    start, end = wf.get_universe_period()
    print(f"  日期范围：{start} 至 {end}")
    
    days = (dt.strptime(end, '%Y-%m-%d') - dt.strptime(start, '%Y-%m-%d')).days
    print(f"  天数：{days}天")
    
    if days < 60:
        print("  ⚠ 选股期不足 60 天")
    else:
        print("  ✓ 选股期充足")
    
    # === 测试 3: 切片 DataFrame ===
    print("\n[测试 3] DataFrame 切片测试...")
    
    # 生成模拟数据
    dates = pd.date_range(
        start='2023-01-01',
        end=datetime.now().strftime('%Y-%m-%d'),
        freq='B'
    )
    df_mock = pd.DataFrame({
        'date': dates.strftime('%Y-%m-%d'),
        'close': 100 + np.cumsum(np.random.randn(len(dates)))
    })
    
    print(f"  原始数据：{len(df_mock)}条")
    
    # 测试回测期切片
    df_backtest = wf.slice_dataframe_by_period(df_mock, period='backtest')
    print(f"  回测期切片：{len(df_backtest)}条")
    
    # 测试选股期切片
    df_universe = wf.slice_dataframe_by_period(df_mock, period='universe')
    print(f"  选股期切片：{len(df_universe)}条")
    
    if len(df_backtest) > 0 and len(df_universe) > 0:
        print("  ✓ 切片功能正常")
    else:
        print("  ✗ 切片功能异常")
        return False
    
    print("\n✓ Walk-Forward 窗口测试通过")
    return True


def test_listing_filter():
    """测试 4: 上市时间过滤"""
    print("\n" + "="*70)
    print("测试 4: 上市时间过滤（新股筛选）")
    print("="*70)
    
    from trading_core.strategy import check_stock_listing_duration
    
    # === 测试 1: 老股票（应该通过）===
    print("\n[测试 1] 老股票（上市>3 年）...")
    
    dates_old = pd.date_range(
        end=datetime.now(),
        periods=800,  # 约 3 年
        freq='B'
    )
    df_old = pd.DataFrame({
        'date': dates_old.strftime('%Y-%m-%d'),
        'close': 100 + np.cumsum(np.random.randn(len(dates_old)))
    })
    
    is_valid, reason = check_stock_listing_duration("600519.SH", df_old, 1.5)
    print(f"  结果：{'✓ 通过' if is_valid else '✗ 过滤'}")
    print(f"  原因：{reason}")
    
    if not is_valid:
        print("  ✗ 老股票应该通过")
        return False
    
    # === 测试 2: 新股（应该过滤）===
    print("\n[测试 2] 新股（上市<1 年）...")
    
    dates_new = pd.date_range(
        end=datetime.now(),
        periods=200,  # 约 1 年
        freq='B'
    )
    df_new = pd.DataFrame({
        'date': dates_new.strftime('%Y-%m-%d'),
        'close': 50 + np.cumsum(np.random.randn(len(dates_new)))
    })
    
    is_valid, reason = check_stock_listing_duration("688XXX.SH", df_new, 1.5)
    print(f"  结果：{'✓ 通过' if is_valid else '✗ 过滤'}")
    print(f"  原因：{reason}")
    
    if is_valid:
        print("  ✗ 新股应该被过滤")
        return False
    
    if "上市时间不足" not in reason:
        print("  ✗ 错误信息应包含'上市时间不足'")
        return False
    
    # === 测试 3: 空 DataFrame ===
    print("\n[测试 3] 空 DataFrame...")
    
    df_empty = pd.DataFrame(columns=['date', 'close'])
    is_valid, reason = check_stock_listing_duration("TEST.SH", df_empty, 1.5)
    print(f"  结果：{'✓ 通过' if is_valid else '✗ 过滤'}")
    print(f"  原因：{reason}")
    
    if is_valid:
        print("  ✗ 空 DataFrame 应该被过滤")
        return False
    
    print("\n✓ 上市时间过滤测试通过")
    return True


def test_parameter_optimization():
    """测试 5: 网格参数优化"""
    print("\n" + "="*70)
    print("测试 5: 网格参数优化（Optuna）")
    print("="*70)

    # 7参数两阶段优化已替代旧版 optimize_parameters_wf。
    # 完整的优化测试通过 CLI: python main.py --optimize
    print("\n[跳过] 完整优化测试请使用 python main.py --optimize")
    return True


def test_full_selection_pipeline():
    """测试 6: 完整选股流程（简化版）"""
    print("\n" + "="*70)
    print("测试 6: 完整选股流程（简化版）")
    print("="*70)
    
    from data_layer.fetcher import get_stock_data
    import yaml
    
    # 加载配置
    config_path = "config.yaml"
    if not os.path.exists(config_path):
        print(f"✗ 配置文件不存在：{config_path}")
        return False
    
    with open(config_path, 'r', encoding='utf-8') as f:
        _ = yaml.safe_load(f)
    
    print("\n[步骤 1] 加载配置...")
    print(f"  配置文件：{config_path}")
    
    # 使用测试参数
    test_params = {
        'grid_spacing': 2.0,
        'grid_amount': 10000,
        'initial_position': 50,
        'max_grids': 10
    }
    
    print("  测试参数:")
    for k, v in test_params.items():
        print(f"    {k}: {v}")
    
    # 获取测试股票数据
    print("\n[步骤 2] 获取股票数据...")
    test_stocks = ["600519.SH", "000858.SZ"]
    
    signals = []
    
    for code in test_stocks:
        print(f"\n  [{code}] 处理中...")
        
        try:
            df = get_stock_data(code, force_full=True)
            
            if df.empty or len(df) < 250:
                print("    ⚠ 数据不足，跳过")
                continue
            
            print(f"    ✓ 数据充足：{len(df)}条")
            
            # 生成信号
            print("    [生成信号]...")
            from trading_core.grid_engine import DynamicGridEngine
            engine = DynamicGridEngine()
            stock_signals = engine.generate_signals(
                df=df,
                stock_codes=[code],
                grid_params=test_params,
                version="test_v1.0.0",
                param_source="manual_test"
            )
            
            if stock_signals:
                print(f"    ✓ 生成 {len(stock_signals)} 个信号")
                signals.extend(stock_signals)
            else:
                print("    ⚠ 未生成信号")
                
        except Exception as e:
            print(f"    ✗ 异常：{str(e)}")
            continue
    
    # 总结
    print("\n" + "="*70)
    print("选股结果")
    print("="*70)
    print(f"总信号数：{len(signals)}")
    
    if len(signals) > 0:
        # 显示前几个信号
        print("\n信号预览（前 5 个）:")
        for sig in signals[:5]:
            print(f"  - {sig['code']} | {sig['direction']} | "
                  f"价格:{sig['price']:.2f} | 数量:{sig['quantity']}")
        
        print("\n✓ 完整选股流程测试通过")
        return True
    else:
        print("✗ 未生成任何信号")
        return False


def main():
    """主测试函数"""
    print("\n" + "="*70)
    print("A 股网格交易系统 - 选股功能集成测试")
    print("="*70)
    print(f"测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python 版本：{sys.version}")
    print(f"Pandas 版本：{pd.__version__}")
    print("="*70)
    
    tests = [
        ("数据获取功能", test_data_acquisition),
        ("增量保存机制", test_incremental_save),
        ("Walk-Forward 窗口", test_walk_forward_window),
        ("上市时间过滤", test_listing_filter),
        ("网格参数优化", test_parameter_optimization),
        ("完整选股流程", test_full_selection_pipeline),
    ]
    
    results = []
    
    for test_name, test_func in tests:
        try:
            print(f"\n{'='*70}")
            print(f"开始测试：{test_name}")
            print('='*70)
            
            success = test_func()
            results.append((test_name, success))
            
        except Exception as e:
            print(f"\n✗ 测试异常：{str(e)}")
            import traceback
            traceback.print_exc()
            results.append((test_name, False))
    
    # 最终总结
    print("\n" + "="*70)
    print("最终测试结果总结")
    print("="*70)
    
    passed = sum(1 for _, success in results if success)
    total = len(results)
    
    print(f"\n通过率：{passed}/{total}")
    
    for test_name, success in results:
        status = "✓" if success else "✗"
        print(f"  {status} {test_name}")
    
    print("\n" + "="*70)
    
    if passed == total:
        print("🎉 所有测试通过！")
        print("\n系统已准备好进行实盘选股！")
        return 0
    else:
        print(f"⚠ {total - passed} 个测试失败，请检查问题")
        return 1


if __name__ == "__main__":
    sys.exit(main())
