# 增量更新数据合并逻辑详解

## 问题回答

**问**: 增量更新时，如果本地 CSV 最后一行是 2023-01-01，新拉取数据从 2023-01-02 开始，如何保证没有重复或遗漏？

**答**: 通过以下三步保证：

```python
def append_new_data(df_existing: pd.DataFrame, df_new: pd.DataFrame, code: str) -> pd.DataFrame:
    """
    将新数据追加到现有数据，去重并排序
    
    核心机制:
    1. concat() 合并 - 新数据在后
    2. drop_duplicates(keep='last') - 保留新数据
    3. sort_values() 排序 - 按日期升序
    """
    
    # === 步骤 1: 合并两个 DataFrame ===
    df_combined = pd.concat([df_existing, df_new], ignore_index=True)
    # 结果：[旧数据..., 新数据...] 新数据在数组后面
    
    # === 步骤 2: 按日期去重，保留最新记录 ===
    df_combined = df_combined.drop_duplicates(subset=['date'], keep='last')
    # keep='last' 的含义：保留最后一次出现的记录
    # 由于 concat 时新数据在后，所以重复的日期会保留新数据的值
    
    # === 步骤 3: 按日期排序 ===
    df_combined = df_combined.sort_values('date').reset_index(drop=True)
    # 确保数据按时间顺序排列
    
    logger.info(f"{code} 数据合并完成：原有{len(df_existing)}条，新增{len(df_new)}条，合并后{len(df_combined)}条")
    
    return df_combined
```

## 三种场景验证

### 场景 1: 完美衔接 ✅

```
本地 CSV: 2022-12-20, ..., 2023-01-01 (10 条)
新数据：2023-01-02, ..., 2023-01-13 (10 条)

合并过程:
1. concat → [2022-12-20, ..., 2023-01-01, 2023-01-02, ..., 2023-01-13] (20 条)
2. drop_duplicates → 无重复，保持 20 条
3. sort_values → 已排序，无需调整

结果：20 条，日期连续，无重复无遗漏 ✓
```

### 场景 2: 数据重叠 ✅

```
本地 CSV: 2022-12-25, ..., 2023-01-05 (10 条)
新数据：2023-01-03, ..., 2023-01-14 (10 条)
        ↑ 重叠 3 天 (01-03, 01-04, 01-05)

合并过程:
1. concat → [旧 10 条 + 新 10 条] (20 条，含 3 个重复日期)
2. drop_duplicates(keep='last') → 移除 3 个重复的旧记录，保留新记录 (17 条)
3. sort_values → 按日期排序

结果：17 条 = 10 + 10 - 3，重叠日期使用新数据 ✓
```

### 场景 3: 数据间隔 ✅

```
本地 CSV: 2022-12-20, ..., 2023-01-01 (10 条)
新数据：2023-01-10, ..., 2023-01-21 (10 条)
        ↑ 间隔 8 天 (01-02 至 01-09 缺失)

合并过程:
1. concat → [2022-12-20, ..., 2023-01-01, 2023-01-10, ..., 2023-01-21] (20 条)
2. drop_duplicates → 无重复，保持 20 条
3. sort_values → 按日期排序

结果：20 条，检测到间隔 > 3 天
      → 调用 check_data_integrity() 检测缺失
      → 调用 backfill_missing_data() 补全数据
```

## 完整代码位置

文件：`/home/zhangny/rain/auto_grid_trading_system/data.py`

```python
# 第 518-548 行
def append_new_data(df_existing: pd.DataFrame, df_new: pd.DataFrame, 
                    code: str) -> pd.DataFrame:
    """将新数据追加到现有数据，去重并排序"""
    
    if df_existing.empty:
        return df_new
    
    if df_new.empty:
        return df_existing
    
    # 合并数据
    df_combined = pd.concat([df_existing, df_new], ignore_index=True)
    
    # 按日期去重（保留最新记录）
    df_combined = df_combined.drop_duplicates(subset=['date'], keep='last')
    
    # 按日期排序
    df_combined = df_combined.sort_values('date').reset_index(drop=True)
    
    logger.info(f"{code} 数据合并完成：原有{len(df_existing)}条，新增{len(df_new)}条，合并后{len(df_combined)}条")
    
    return df_combined
```

## 增量更新完整流程

文件：`data.py` 第 631-760 行

```python
def incremental_update(code: str, data_dir: str = "./data", 
                       adjust: str = "qfq", force_full: bool = False) -> pd.DataFrame:
    """
    执行增量更新逻辑
    
    关键步骤:
    1. 读取元数据，获取最后更新日期 last_update_date
    2. 计算增量起始日期：incremental_start = last_update_date + 1 天
    3. 获取增量数据：[incremental_start, today]
    4. 调用 append_new_data() 合并
    5. 检查完整性：check_data_integrity()
    6. 必要时补全：backfill_missing_data()
    7. 更新元数据
    """
    
    # 步骤 1: 读取本地元数据
    stock_meta = get_stock_metadata(code, data_dir)
    df_existing = load_from_cache(cache_path)
    
    # 步骤 2: 计算增量日期范围
    last_update_str = stock_meta.get('last_update_date', '')
    last_update_date = datetime.strptime(last_update_str, '%Y-%m-%d')
    
    # 关键：从最后更新日期的次日开始
    incremental_start = (last_update_date + timedelta(days=1)).strftime('%Y%m%d')
    
    # 步骤 3: 获取增量数据
    df_incremental = fetch_incremental_data(code, incremental_start, today_str)
    
    # 步骤 4: 合并数据（调用 append_new_data）
    df_updated = append_new_data(df_existing, df_incremental, code)
    
    # 步骤 5: 检查完整性
    is_complete, missing = check_data_integrity(df_updated, code)
    
    # 步骤 6: 如有缺失，自动补全
    if not is_complete and missing:
        backfill_missing_data(code, missing, data_dir, adjust)
    
    # 步骤 7: 更新元数据
    last_date = df_updated['date'].max().strftime('%Y-%m-%d')
    update_stock_metadata(code, last_date, len(df_updated), data_dir)
    
    return df_updated
```

## 为什么不会重复？

```python
# 示例：本地有 2023-01-03，新数据也有 2023-01-03

df_existing = DataFrame([
    {'date': '2023-01-02', 'close': 100.0},
    {'date': '2023-01-03', 'close': 101.0},  # ← 旧值
])

df_new = DataFrame([
    {'date': '2023-01-03', 'close': 101.5},  # ← 新值
    {'date': '2023-01-04', 'close': 102.0},
])

# Step 1: concat
df_combined = pd.concat([df_existing, df_new])
# 结果:
# index | date       | close
# 0     | 2023-01-02 | 100.0
# 1     | 2023-01-03 | 101.0  ← 旧值在前
# 0     | 2023-01-03 | 101.5  ← 新值在后
# 1     | 2023-01-04 | 102.0

# Step 2: drop_duplicates(subset=['date'], keep='last')
# keep='last' 保留索引大的记录（即新值）
df_deduped = df_combined.drop_duplicates(subset=['date'], keep='last')
# 结果:
# index | date       | close
# 0     | 2023-01-02 | 100.0
# 1     | 2023-01-03 | 101.5  ← 保留新值 ✓
# 1     | 2023-01-04 | 102.0

# Step 3: sort_values('date')
df_final = df_deduped.sort_values('date').reset_index(drop=True)
# 最终结果:
# index | date       | close
# 0     | 2023-01-02 | 100.0
# 1     | 2023-01-03 | 101.5  ← 新值
# 2     | 2023-01-04 | 102.0
```

## 为什么不会遗漏？

1. **增量起始日期计算准确**:
   ```python
   incremental_start = (last_update_date + timedelta(days=1)).strftime('%Y%m%d')
   # 例：last_update_date = '2023-01-01'
   # → incremental_start = '2023-01-02'
   # → 从 2023-01-02 开始请求，不会遗漏 2023-01-02
   ```

2. **数据源接口保证连续性**:
   ```python
   df_incremental = fetch_incremental_data(
       code, 
       start_date='20230102',  # 包含此日期
       end_date='20230115',    # 包含此日期
   )
   # 数据源返回 [20230102, 20230103, ..., 20230115] 闭区间
   ```

3. **完整性检查检测遗漏**:
   ```python
   is_complete, missing = check_data_integrity(df_updated, code)
   
   def check_data_integrity(df, code):
       """检查相邻日期间隔，识别缺失的交易日"""
       dates = pd.to_datetime(df.sort_values('date')['date'])
       gaps = dates.diff()[dates.diff() > pd.Timedelta(days=3)]
       
       if len(gaps) > 0:
           # 发现间隔，生成缺失日期列表
           missing = generate_missing_dates(gaps)
           return False, missing
       
       return True, []
   ```

4. **自动补全机制**:
   ```python
   if not is_complete and missing:
       backfill_missing_data(code, missing, data_dir, adjust)
       # 重新请求缺失日期的数据
   ```

## 总结

✅ **无重复**: `drop_duplicates(keep='last')` 自动去重，保留新数据  
✅ **无遗漏**: `incremental_start = last_update_date + 1` 精确衔接  
✅ **自动补全**: `check_data_integrity()` + `backfill_missing_data()` 检测并补全缺失  
✅ **日志追踪**: 详细记录合并前后的数据条数变化
