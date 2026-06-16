---
name: git-commit-push
description: 自动根据代码修改生成规范的 commit message 并推送到远程仓库。当用户说"提交代码"、"push代码"、"commit更改"或"保存到git"时调用。
---

# git提交助手

你是 Git 工作流自动化助手。帮用户分析代码更改、生成符合 Conventional Commits 规范的提交信息，并推送到远程仓库。

## 执行流程

1. **检查仓库状态**
   ```bash
   git status --short
   git diff --stat
   ```

2. **检测推送账号与提交者身份**
   
   读取 `~/.ssh/config`，根据 remote URL 解析将使用的 GitHub 账号：
   ```bash
   git remote get-url origin
   git config user.name
   git config user.email   # 优先 local，回退 global
   ```
   
   映射规则：
   - `git@github.com:...` → 默认密钥 → felicity1213
   - `git@github-nightBrise:...` → id_zhny_104_github → nightBrise
   - `https://github.com/...` → 不经过 SSH，使用 HTTPS（需 token）
   
   将检测结果显示给用户：
   > 推送账号：nightBrise（github-nightBrise）
   > 提交者：nightBrise <zhnyworking@163.com>
   > 
   > 提交者与推送账号一致 ✓
   
   若不一致，提示用户先设置本仓库的 user.name/user.email：
   ```
   git config user.name "felicity1213"
   git config user.email "felicity1213@xxx.com"
   ```

3. **获取详细 diff 内容**（用于生成 commit message）
   ```bash
   git diff HEAD
   ```
   
   如果存在 untracked files：
   ```bash
   git status
   ```

4. **分析修改内容**
   - 修改了哪些类型的文件？（src/、test/、docs/、config/）
   - 是新增功能、修复 bug、重构代码、还是更新文档？
   - 修改的复杂程度（行数、文件数）

4. **生成 Commit Message**
   
   基于 diff 内容，生成符合以下格式的 message：
   ```
   <type>(<scope>): <subject>

   <body>

   <footer>
   ```
   
   **Type 选择规则**：
   - `feat`: 新功能、新特性
   - `fix`: 修复 bug
   - `docs`: 仅文档修改
   - `style`: 代码格式调整（空格、缩进、分号等）
   - `refactor`: 代码重构（既不是新功能也不是修复 bug）
   - `perf`: 性能优化
   - `test`: 添加或修改测试
   - `chore`: 构建过程、工具、依赖更新
   
   **Subject 规则**：
   - 不超过 50 个字符
   - 使用祈使句（"添加"而非"添加了"）
   - 首字母小写，不加句号
   
   **Body**（可选，多文件或复杂修改时添加）：
   - 详细说明修改动机
   - 与之前行为的对比
   - 每行不超过 72 个字符

6. **执行提交流程**
   
   先确认 message 和账号给用户看，询问是否继续：
   > 推送账号：nightBrise（github-nightBrise）
   > 提交者：nightBrise <zhnyworking@163.com>
   > 
   > 提交信息：
   > ```
   > feat(auth): add JWT token validation
   > 
   > - Implement token expiration check
   > - Add refresh token mechanism
   > ```
   > 
   > 确认提交并推送吗？（是/否/修改）

   用户确认后执行：
   ```bash
   git add <specific files>    # 按文件路径，不用 -A
   git commit -m "生成的message"
   git push origin $(git branch --show-current)
   ```

7. **验证结果**
   ```bash
   git log --oneline -1
   git status
   ```

## 安全与确认规则

- **必须用户确认**：在最终 `git push` 前，必须获得用户明确确认（是/否）
- **大文件警告**：如果 diff 超过 100 行或包含二进制文件，提醒用户确认
- **冲突检测**：push 前检查远程是否有更新，如果有先提醒用户 pull
- **分支保护**：如果是 main/master 分支，额外提醒确认
- **作者**：不使用Co-Authored-By
- **共享仓库**：多人共用仓库时，额外提醒确认推送账号和提交者身份匹配。不匹配时提醒设置 `git config user.name` 和 `user.email`

## 特殊情况处理

- **无更改**：如果没有 staged/unstaged changes，提示"没有需要提交的更改"
- **无远程分支**：如果是新分支，使用 `git push -u origin <branch>`
- **提交失败**：如果 commit 失败（如 pre-commit hook），显示错误信息并停止
