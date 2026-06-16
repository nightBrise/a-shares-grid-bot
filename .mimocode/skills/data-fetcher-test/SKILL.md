---
name: data-fetcher-test
description: 数据获取层限流测试：验证多数据源轮询、增量更新、限流恢复机制是否正常工作
---

# 数据获取层测试技能

用于测试 data_layer/ 的多数据源轮询和限流恢复机制。在修改 fetcher.py 或数据源相关代码后执行。

## 执行流程

### 1. 基础连通性测试
```bash
python -c "
from data_layer.fetcher import get_stock_data
df = get_stock_data('sh.600000', enable_incremental=False)
print(f'Rows: {len(df)}, Cols: {list(df.columns)}')
print(df.tail(3))
"
```

### 2. 限流恢复测试
```bash
python -c "
from data_layer.fetcher import DataSourceManager
mgr = DataSourceManager()
# 测试单个数据源
result = mgr.try_source('baostock', 'sh.600000')
print(f'Source: baostock, Rows: {len(result) if result is not None else 0}')
"
```

### 3. 增量更新测试
```bash
python -c "
from data_layer.fetcher import get_stock_data
# 先获取基础数据
df1 = get_stock_data('sh.600000', enable_incremental=False)
print(f'Base rows: {len(df1)}')
# 再测试增量更新
df2 = get_stock_data('sh.600000', enable_incremental=True)
print(f'After incremental: {len(df2)}')
"
```

### 4. 批量下载监控
```bash
python -c "
import time
from data_layer.fetcher import get_stock_data

stocks = ['sh.600000', 'sh.600036', 'sz.000001']
for code in stocks:
    start = time.time()
    df = get_stock_data(code, enable_incremental=False)
    elapsed = time.time() - start
    print(f'{code}: {len(df)} rows, {elapsed:.1f}s')
    time.sleep(1)  # 避免触发限流
"
```

### 5. 限流状态检查
```bash
python -c "
from data_layer.fetcher import DataSourceManager
mgr = DataSourceManager()
for name, source in mgr.sources.items():
    print(f'{name}: healthy={source.healthy}, cooldown={source.cooldown_until}')
"
```

## 常见问题

| 问题 | 排查方向 |
|------|----------|
| 所有数据源返回 None | 检查网络连接，确认 API 端点未变更 |
| 频繁触发限流 | 增大 `min_delay_per_stock`，检查 `max_cooldown` 配置 |
| 增量更新数据异常 | 检查本地 parquet 缓存是否损坏 |
| 数据缺失列 | 检查各数据源返回格式是否一致 |

## 停止条件

- 所有数据源至少一个可用
- 限流后能自动恢复
- 增量更新正确追加数据
