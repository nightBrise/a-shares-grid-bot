---
name: dashboard-dev
description: Gradio dashboard 开发调试循环：编辑 → 语法检查 → 重启 → 浏览器验证 → 修复问题
---

# Dashboard 开发调试技能

用于 Gradio + Plotly 仪表板的迭代开发。每次修改 dashboard.py 后执行此流程，避免反复手动调试。

## 执行流程

### 1. 语法检查
```bash
python -m py_compile dashboard.py && echo "SYNTAX OK"
```
失败则立即修复语法错误，不进入下一步。

### 2. 重启 Dashboard
```bash
./dashboard_ctl.sh restart 2>&1
```
等待服务启动：
```bash
sleep 3 && curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:7860 && echo " OK"
```
如果返回非 200，检查进程状态：
```bash
./dashboard_ctl.sh status 2>&1
```

### 3. 验证
- 本地访问 `http://127.0.0.1:7860`
- 检查下拉框、图表、时间范围过滤是否正常
- 确认 x 轴范围正确（不应出现 1960/1970 年）
- 确认标签不重叠

### 4. 常见问题排查

| 问题 | 排查方向 |
|------|----------|
| 图表 x 轴从 1960 年开始 | 检查 DataFrame 日期列是否为 datetime 类型，是否有 NaN/NaT |
| 标签重叠 | 增大子图间距 `update_layout(margin=dict(b=...))` |
| 下拉框无中文名 | 检查 stock_name 映射是否正确加载 |
| 图表消失 | 检查 Gradio 版本兼容性，Plotly fig 是否为 None |
| 进程未启动 | `ps aux | grep dashboard` 检查端口占用 |

### 5. 公网分享（可选）
```bash
./dashboard_ctl.sh share
```

## 停止条件

- 语法检查通过
- Dashboard 可访问且图表正常显示
- 用户确认视觉效果正确
