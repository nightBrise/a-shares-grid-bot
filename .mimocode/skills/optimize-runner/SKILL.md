---
name: optimize-runner
description: 两阶段网格参数优化完整流程：清理旧进程 → 运行优化 → 监控进度 → 生成参数报告并分析
---

# 两阶段参数优化技能

用于执行完整的网格参数优化工作流（贝叶斯优化 + WF 微调）。这是本项目最频繁重复的运维操作。

## 前置条件

- 已通过 `--mode select` 完成选股，`config.yaml` 中 `stocks` 列表不为空
- 数据层正常（可用 `data-fetcher-test` 技能验证）
- 当前在项目根目录 `/home/zhangny/rain/auto_grid_trading_system`

## 执行流程

### 1. 清理残留进程

优化任务残留进程会占用数据和端口，必须在启动新优化前清理：

```bash
# 终止所有 python main.py --mode optimize 进程
ps aux | grep "python main.py.*optimize" | grep -v grep | awk '{print $2}' | xargs -r kill 2>/dev/null
sleep 1
# 确认已清理
ps aux | grep "python main.py.*optimize" | grep -v grep || echo "已清理"
```

### 2. 检查配置

确认优化配置正确，特别关注 `parallel_optimization.enabled` 和 stocks 列表：

```bash
grep -E "(parallel_optimization|stocks:)" configuration/config.yaml | head -10
```

如果 stocks 为空，先运行选股模式：

```bash
python main.py --mode select 2>&1 | tail -30
```

### 3. 运行优化

```bash
# 前台运行（适合短时调试）
python main.py --mode optimize 2>&1 | tail -100

# 或后台运行（适合完整优化，耗时较久）
python main.py --mode optimize > output/optimize_$(date +%Y%m%d_%H%M%S).log 2>&1 &
echo "PID: $!"
```

### 4. 监控进度

```bash
# 检查进程是否在运行
ps aux | grep "python main.py.*optimize" | grep -v grep

# 查看实时日志输出
tail -50 output/optimize_*.log 2>/dev/null

# 检查是否有中间结果写入
ls -la output/report.json 2>/dev/null
```

### 5. 生成参数说明报告

优化完成后，输出 JSON 报告和 Markdown 格式参数说明：

```bash
# 检查 JSON 报告是否存在
ls -la output/report.json && echo "JSON 报告已生成"
```

生成参数解释报告（报告路径：`output/优化参数报告_{date}.md`）：

读取 `output/report.json`，逐只股票分析：
- Phase 1 分数：验证不是 `-999`（-999 表示数据不足导致优化失败）
- Phase 2 分数：应高于 Phase 1
- 网格参数（spacing, k_up, k_down 等）的实际含义和对该股票的适配性
- 交易频率预期（基于网格层数和波动率估算）

### 6. 验证结果

**检查要点（详细）**：

| 检查项 | 正常表现 | 异常排查方向 |
|--------|----------|-------------|
| Phase 1 分数 | 非 -999，合理正数 | 检查历史数据是否充足（需 > 250 个交易日） |
| Phase 2 分数 | 高于 Phase 1，为正 | WF 时间窗口过短？数据缺失？ |
| 网格间距 | 股票 ATR 的合理比例 | 间距 = 0 或极端值 → 优化未收敛 |
| 波 k_up/down | 1.0 ~ 3.0 之间 | 极端值 → 波动率估算异常 |
| 优化时长 | 单股票 2-10 分钟 | 超过 30 分钟 → 检查数据获取是否卡住 |

```bash
# 快速查看关键指标
python -c "
import json
r = json.load(open('output/report.json'))
for s in r.get('stocks', []):
    p1 = s.get('phase1_score', 'N/A')
    p2 = s.get('phase2_score', 'N/A')
    print(f\"{s['code']}: P1={p1} P2={p2}\")
"
```

## 停止条件

- 所有股票 Phase 2 优化完成且分数正常
- 优化参数报告已生成并可读
- 网格参数在合理范围内
- 用户确认参数合理，可以用于回测或实盘

## 常见问题

| 问题 | 处理方式 |
|------|----------|
| Python 找不到模块 | 检查 conda env: `conda activate rain` 或 `pip install -r requirements.txt` |
| 优化卡在"数据获取" | 网络问题，使用 `data-fetcher-test` 技能诊断 |
| report.json 为空或不存在 | 优化未运行完成；检查后台进程是否被 kill |
| 某只股票优化失败 | 单独用 `python -c "from trading_core.strategy import run_two_phase_optimization; ..."` 调试 |
