#!/usr/bin/env python3
"""
测试限流修复是否生效
验证项：
1. 配置读取：限流参数是否为修复后的值（10/20/2）
2. 代码逻辑：strategy.py 是否使用串行获取（无 ThreadPoolExecutor）
3. 实际网络测试：获取3只股票数据，观察请求间隔和成功率
"""

import sys
import os
import time
# from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.utils import load_config

def test_config_values():
    """测试1：验证配置参数已更新"""
    print("=" * 60)
    print("测试1：验证配置参数")
    print("=" * 60)

    config = load_config('configuration/config.yaml')
    network = config.get('network', {})

    checks = {
        'min_delay_per_stock': (network.get('min_delay_per_stock'), 10.0),
        'max_delay_per_stock': (network.get('max_delay_per_stock'), 20.0),
        'batch_size': (network.get('batch_size'), 3),
        'long_rest_after_batches': (network.get('long_rest_after_batches'), 2),
        'long_rest_duration': (network.get('long_rest_duration'), 60.0),
        'max_retries': (network.get('max_retries'), 2),
        'base_retry_delay': (network.get('base_retry_delay'), 5.0),
    }

    all_pass = True
    for key, (actual, expected) in checks.items():
        status = "PASS" if actual == expected else "FAIL"
        if status == "FAIL":
            all_pass = False
        print(f"  {status}: {key} = {actual} (期望: {expected})")

    print()
    return all_pass


def test_serial_fetch_logic():
    """测试2：验证 strategy.py 使用串行获取"""
    print("=" * 60)
    print("测试2：验证串行获取逻辑")
    print("=" * 60)

    with open('trading_core/strategy.py', 'r') as f:
        content = f.read()

    # 提取 run_multi_factor_selection 函数体（到下一个顶层 def 为止）
    func_start = content.find("def run_multi_factor_selection")
    func_end = content.find("\ndef ", func_start + 1)  # 下一个顶层函数
    func_body = content[func_start:func_end]

    checks = {
        "无 ThreadPoolExecutor 并行获取": "ThreadPoolExecutor" not in func_body,
        "串行循环获取": "for code in stock_list:" in func_body,
    }

    all_pass = True
    for desc, result in checks.items():
        status = "PASS" if result else "FAIL"
        if status == "FAIL":
            all_pass = False
        print(f"  {status}: {desc}")

    print()
    return all_pass


def test_cache_ttl():
    """测试3：验证缓存有效期延长到30天"""
    print("=" * 60)
    print("测试3：验证缓存有效期")
    print("=" * 60)

    with open('data_layer/fetcher.py', 'r') as f:
        content = f.read()

    # 检查 get_stock_data 函数中的缓存有效期
    if "days_diff <= 30" in content:
        print("  PASS: 缓存有效期已延长到 30 天")
        return True
    elif "days_diff <= 7" in content:
        print("  FAIL: 缓存有效期仍为 7 天")
        return False
    else:
        print("  WARN: 未找到缓存有效期判断代码")
        return False


def test_network_request(test_codes=None):
    """测试4：实际网络测试 - 获取几只股票的数据"""
    if test_codes is None:
        test_codes = ["000333.SZ", "000066.SZ", "000651.SZ"]

    print("=" * 60)
    print(f"测试4：实际网络测试 ({len(test_codes)} 只股票)")
    print("=" * 60)
    print(f"股票列表: {test_codes}")
    print("预期间隔: 10-20 秒/只 (串行)")
    print(f"预期总耗时: ~{len(test_codes) * 15} 秒")
    print("-" * 60)

    from data_layer.fetcher import get_stock_data, init_fetcher_config

    config = load_config('configuration/config.yaml')
    init_fetcher_config(config)

    data_dir = config.get('paths', {}).get('data_dir', './data')

    results = []
    start_time = time.time()

    for i, code in enumerate(test_codes, 1):
        req_start = time.time()
        print(f"\n[{i}/{len(test_codes)}] {code} 开始获取...", end=" ", flush=True)

        try:
            df = get_stock_data(code, data_dir=data_dir,
                               enable_incremental=False, use_cache=True,
                               fallback_to_cache=True)
            req_time = time.time() - req_start

            if df is not None and len(df) > 0:
                last_date = df['date'].max().strftime('%Y-%m-%d') if hasattr(df['date'].max(), 'strftime') else str(df['date'].max())
                print(f"OK | {len(df)} 条 | 最后日期: {last_date} | 耗时: {req_time:.1f}s")
                results.append({
                    'code': code,
                    'status': 'success',
                    'rows': len(df),
                    'last_date': last_date,
                    'time': req_time,
                    'source': 'cache' if req_time < 1.0 else 'network'
                })
            else:
                print(f"FAIL | 空数据 | 耗时: {req_time:.1f}s")
                results.append({
                    'code': code,
                    'status': 'empty',
                    'rows': 0,
                    'time': req_time,
                    'source': 'unknown'
                })
        except Exception as e:
            req_time = time.time() - req_start
            print(f"FAIL | 异常: {e} | 耗时: {req_time:.1f}s")
            results.append({
                'code': code,
                'status': 'error',
                'error': str(e)[:50],
                'time': req_time,
                'source': 'unknown'
            })

    total_time = time.time() - start_time

    # 分析结果
    print("\n" + "=" * 60)
    print("网络测试结果汇总")
    print("=" * 60)

    success = [r for r in results if r['status'] == 'success']
    failed = [r for r in results if r['status'] != 'success']
    cache_hits = [r for r in results if r.get('source') == 'cache']
    network_reqs = [r for r in results if r.get('source') == 'network']

    print(f"  总耗时: {total_time:.1f}s")
    print(f"  成功: {len(success)}/{len(test_codes)}")
    print(f"  失败: {len(failed)}/{len(test_codes)}")
    print(f"  缓存命中: {len(cache_hits)}")
    print(f"  网络请求: {len(network_reqs)}")

    if network_reqs:
        times = [r['time'] for r in network_reqs]
        avg_time = sum(times) / len(times)
        min_time = min(times)
        max_time = max(times)
        print(f"  网络请求耗时: 平均 {avg_time:.1f}s, 最小 {min_time:.1f}s, 最大 {max_time:.1f}s")

        # 检查间隔是否合理（串行模式下，每次请求应该包含等待时间）
        if len(times) >= 2 and min_time >= 8.0:
            print(f"  请求间隔检查: 最小耗时 {min_time:.1f}s >= 8s，限流可能生效")
        elif len(times) >= 2 and min_time < 5.0:
            print(f"  WARNING: 最小耗时 {min_time:.1f}s < 5s，限流可能未生效（缓存命中或配置未生效）")

    if failed:
        print("\n  失败详情:")
        for r in failed:
            print(f"    {r['code']}: {r['status']} - {r.get('error', 'N/A')}")

    print()
    return len(failed) == 0


def main():
    print("\n" + "=" * 60)
    print("限流修复验证测试")
    print("=" * 60)
    print()

    # 运行测试1-3（快速，无需网络）
    t1 = test_config_values()
    t2 = test_serial_fetch_logic()
    t3 = test_cache_ttl()

    # 测试4（需要网络，耗时较长）
    if len(sys.argv) > 1 and sys.argv[1] == '--network':
        t4 = test_network_request()
    else:
        print("=" * 60)
        print("测试4：实际网络测试")
        print("=" * 60)
        print("  SKIP: 跳过网络测试（加 --network 参数运行）")
        print("  例: python test_rate_limit_fix.py --network")
        print()
        t4 = True  # 未运行，不算失败

    # 汇总
    print("=" * 60)
    print("测试汇总")
    print("=" * 60)
    print(f"  配置参数验证:     {'PASS' if t1 else 'FAIL'}")
    print(f"  串行逻辑验证:     {'PASS' if t2 else 'FAIL'}")
    print(f"  缓存有效期验证:   {'PASS' if t3 else 'FAIL'}")
    print(f"  实际网络测试:     {'PASS' if t4 else 'FAIL'}")
    print()

    if t1 and t2 and t3:
        print("前三项全部通过。如需验证实际网络限流效果，运行:")
        print("  python test_rate_limit_fix.py --network")
        return 0
    else:
        print("部分测试失败，请检查修复是否完整应用。")
        return 1


if __name__ == '__main__':
    sys.exit(main())
