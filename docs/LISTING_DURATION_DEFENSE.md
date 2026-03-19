# 新股防御性代码实现

## 问题

**问**: 如果某只股票上市时间不足 1.5 年，程序会报错还是自动排除？

**答**: 已添加完整的防御性代码，**自动排除**上市时间不足的股票，不会报错。

## 解决方案

### 新增函数：`check_stock_listing_duration()`

文件：[`strategy.py`](file:///home/zhangny/rain/auto_grid_trading_system/strategy.py) 第 1236-1287 行

```python
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
    
    # 获取最早日期（上市日期）
    earliest_date = df['date'].min()
    
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
    
    return True, f"上市时间充足 ({years_since_listing:.2f}年)"
```

### 集成到选股流程

在 `build_universe_with_wf()` 函数中（第 1302-1317 行）：

```python
for i, code in enumerate(stock_list):
    try:
        # 获取历史数据
        df = get_stock_data(code, data_dir=paths_cfg['data_dir'])
        
        # 防御性检查 1: 无数据
        if df.empty:
            logger.warning(f"{code} 无数据，跳过")
            fail_count += 1
            continue
        
        # 防御性检查 2: 上市时间不足 ← 新增
        is_valid_listing, listing_reason = check_stock_listing_duration(
            code, df, required_years=required_years
        )
        
        if not is_valid_listing:
            logger.info(f"{code} 过滤：{listing_reason}")
            filtered_by_listing += 1  # 统计过滤数量
            continue
        
        # ... 后续处理
```

## 四层防御机制

### 防御 1: 无数据检查

```python
if df.empty:
    logger.warning(f"{code} 无数据，跳过")
    continue
```

### 防御 2: 上市时间检查 ✅ 新增

```python
is_valid_listing, listing_reason = check_stock_listing_duration(
    code, df, required_years=required_years
)

if not is_valid_listing:
    logger.info(f"{code} 过滤：{listing_reason}")
    filtered_by_listing += 1
    continue
```

### 防御 3: 数据量检查

```python
if df_universe.empty or len(df_universe) < min_required_records:
    logger.warning(f"{code} 选股期数据不足 (<{min_required_records}条)，跳过")
    continue
```

### 防御 4: 异常捕获

```python
except Exception as e:
    logger.error(f"处理 {code} 时发生异常：{str(e)}", exc_info=True)
    fail_count += 1
```

## 日志输出示例

```
======================================================================
Walk-Forward 选股池构建
======================================================================
选股数据期间：2024-09-18 至 2025-12-18
所需最小数据跨度：456 天 (1.25 年)
注意：禁止使用 2025-12-18 之后的数据（防止前视偏差）
注意：自动排除上市时间不足 1.25 年的股票
======================================================================

[1/200] 处理股票：688XXX.SH
688XXX.SH 过滤：上市时间不足 1.25 年：上市日期=2025-06-01, 至今=0.79 年 (需≥1.25 年)

[2/200] 处理股票：600519.SH
600519.SH 上市时间充足 (15.23 年)

...

======================================================================
选股池构建统计:
======================================================================
总计：200 只股票
  成功入选：156 只
  处理失败：12 只
  上市时间不足过滤：32 只  ← 新增统计
  成功率：78.0%
======================================================================
```

## 测试验证

运行测试脚本：

```bash
cd /home/zhangny/rain/auto_grid_trading_system
python test_listing_simple.py
```

测试结果：

```
【测试 1】老股票（上市 3 年）
结果：✓ 通过
原因：上市时间充足 (3.00 年)

【测试 2】新股（上市 1 年）
结果：✗ 过滤
原因：上市时间不足 1.5 年：上市日期=2025-03-18, 至今=1.00 年 (需≥1.5 年)

【测试 3】边界情况（上市 1.5 年）
结果：✓ 通过
原因：上市时间充足 (1.50 年)

【测试 4】空 DataFrame
结果：✗ 过滤
原因：无历史数据

✓ 所有测试通过
```

## 配置参数

在 `config.yaml` 中可以调整最低年限要求：

```yaml
selection:
  # Walk-Forward 窗口自动计算所需年限
  # 默认为 T-1.5 年 至 T-3 个月，即 1.25 年
  # 如需修改，可调整 backtest 配置
  min_required_records: 60  # 最小数据条数
```

## 为什么需要 1.5 年数据？

Walk-Forward 分析的时间窗口定义：

```
当前日期：T

选股池构建期：T - 1.5 年 至 T - 3 个月
              ↑___________________↑
                   需要 1.25 年的历史数据

样本内优化期：T - 1 年 至 T - 3 个月
             ↑_______________↑
                 需要 0.75 年的历史数据
```

如果股票上市时间不足 1.5 年：
1. **无法提供足够的选股期数据** → Hurst 指数计算不准确
2. **无法进行有效的历史回测** → 参数优化缺乏依据
3. **可能导致数组越界错误** → `df.iloc[-250]` 在数据不足 250 条时会报错

因此，**自动过滤**是必要的防御措施。

## 相关改进

同时改进了以下防御性代码：

### 1. Hurst 指数计算的数据长度检查

```python
# 原代码
price_series = latest['close'] if len(df_universe) < 250 else df_universe['close'].tail(250)

# 改进后
if len(df_universe) >= 250:
    price_series = df_universe['close'].tail(250)
else:
    price_series = df_universe['close']
    logger.debug(f"{code} 数据不足 250 条，使用全部 {len(price_series)} 条计算 Hurst 指数")
```

### 2. 日均成交额计算的数据长度检查

```python
# 原代码
avg_turnover = df_universe['amount'].tail(20).mean() / 10000

# 改进后
if len(df_universe) >= 20:
    avg_turnover = df_universe['amount'].tail(20).mean() / 10000
else:
    avg_turnover = df_universe['amount'].mean() / 10000
    logger.debug(f"{code} 数据不足 20 条，使用全部数据计算日均成交额")
```

### 3. 增强的统计信息

```python
logger.info(f"总计：{len(stock_list)} 只股票")
logger.info(f"  成功入选：{success_count} 只")
logger.info(f"  处理失败：{fail_count} 只")
logger.info(f"  上市时间不足过滤：{filtered_by_listing} 只")  # 新增
logger.info(f"  成功率：{success_count/max(len(stock_list),1)*100:.1f}%")
```

## 总结

✅ **不会报错** - 所有边界情况都有适当的检查和日志  
✅ **自动排除** - 上市时间不足的股票会被自动过滤  
✅ **详细日志** - 记录每只股票的过滤原因和统计数据  
✅ **多层防御** - 4 层检查确保系统健壮性  
✅ **可配置** - 支持自定义最低年限要求  
