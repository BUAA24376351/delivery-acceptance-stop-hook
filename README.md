# Delivery Acceptance Stop Hook

一个面向 [Claude Code](https://code.claude.com/docs/en/hooks) 的 Stop Hook，
在代码被修改但未经验证（测试 / lint / 类型检查 / TODO 扫描）时阻止会话结束。

## 功能

- **自动拦截** —— Claude 完成一轮工作时自动触发，无需手动操作
- **两层变更检测** —— Git 状态（或 mtime 比对）+ 会话记录回退
- **验证识别** —— 自动在会话记录中查找 pytest、ruff、mypy 等验证命令的执行痕迹
- **重试支持** —— 首次拦截后，Claude 补上验证后可再次触发并放行
- **智能建议** —— 根据项目配置自动检测可用工具并给出验证建议

## 设计目标

防止 Claude 结束当前轮次（以及用户接受结果）时，修改过的文件缺少
测试 / lint / 类型检查验证。Hook 通过 `exit 2` 阻止结束，将指导信息
以 stderr 的形式反馈给 Claude 作为上下文，使其能够补上验证后重试。

## 工作原理

```
Claude 完成一轮 → Stop Hook 触发
                              │
                              ▼
              ┌── 第 1 步：检测文件变更（两层）
              │
              │  第 A 层 — 文件系统扫描
              │    ├─ Git 仓库 → git status --porcelain
              │    └─ 无 Git → os.walk + .file_state.json mtime 比对
              │
              │  第 B 层 — 会话记录回退（第 A 层无结果时触发）
              │    └─ 扫描会话记录末尾 2 MB，查找
              │       "type":"tool_use",...,"name":"write" / "edit"
              │
              ├── 无变更？→ exit 0（放行）
              │
              ├── 第 2 步：在会话记录中查找验证命令
              │    └─ 扫描末尾 2 MB，匹配测试/lint/类型检查标记
              │       （pytest、ruff、mypy、eslint、tsc、grep TODO …）
              │
              ├── 已验证？→ exit 0（放行）
              │
              └── 未验证？→ exit 2（拦截）
                   ├─ stderr → 用户可见 + 喂给 Claude 作为上下文
                   └─ Claude 可以修正后重试（stop_hook_active=true）
```

## 快速开始

```bash
git clone https://github.com/BUAA24376351/delivery-acceptance-stop-hook.git
cd Claude-Lab
cp .claude/settings.local.example.json .claude/settings.local.json
```

配置完成。确保已安装 Python 3，随后正常使用 Claude Code 即可。
首次触发 Stop 时会弹出权限确认，允许后 Hook 即生效。

## 安装

### 前置要求

- [Claude Code](https://code.claude.com/)（命令行工具或 VS Code 扩展）
- Python 3.7+
- （可选）Git —— 用于 Git 项目的变更检测

### 步骤

1. 将本项目克隆或复制到你的项目中：
   ```
   git clone https://github.com/BUAA24376351/delivery-acceptance-stop-hook.git
   ```
   或手动将 `.claude/hooks/delivery-acceptance.py` 放入项目对应位置。

2. 在 `.claude/settings.local.json` 中注册 Hook：
   ```json
   {
     "hooks": {
       "Stop": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "python .claude/hooks/delivery-acceptance.py"
             }
           ]
         }
       ]
     }
   }
   ```

   也可直接复制提供的示例配置：
   ```bash
   cp .claude/settings.local.example.json .claude/settings.local.json
   ```

3. （可选）将 `.claude/.file_state.json` 加入 `.gitignore`：
   ```
   .claude/.file_state.json
   ```

## 使用方法

安装完成后，Hook 会自动工作：

- 正常工作时无感 —— 无变更或已验证时安静放行
- 有变更未验证时，会在 VS Code 中显示拦截信息，Claude 会自动获得上下文并尝试补上验证
- 如需紧急跳过，可以在对话中说明原因（例如："本轮修改已完成，跳过交付验收"）

### 调试

如需查看 Hook 的运行日志，编辑 `delivery-acceptance.py` 将 `DEBUG = False` 改为 `DEBUG = True`，
Hook 每次触发时会向 `.claude/hook.log` 写入 JSONL 格式的详细日志。

## 配置

### 注册 Hook

仓库提供了开箱即用的示例配置 `.claude/settings.local.example.json`，
直接复制即可，无需手动编写：

```bash
cp .claude/settings.local.example.json .claude/settings.local.json
```

也可以手动在 `.claude/settings.local.json`（项目级）或
`~/.claude/settings.json`（用户级）中添加：

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python .claude/hooks/delivery-acceptance.py"
          }
        ]
      }
    ]
  }
}
```

### 涉及文件

| 路径 | 用途 |
|------|------|
| `.claude/hooks/delivery-acceptance.py` | Hook 脚本 |
| `.claude/.file_state.json` | 自动生成的 mtime 快照（非 Git 项目） |
| `.claude/settings.local.json` | Hook 注册配置 |

## 可识别的验证命令

| 类别 | 工具 |
|------|------|
| 测试框架 | pytest、npm test、go test、cargo test、jest、mocha、unittest、tox、nosetests |
| 代码检查 | ruff check、flake8、pylint、eslint、black --check |
| 类型检查 | mypy、pyright、tsc --noEmit |
| TODO 扫描 | grep/rg/ag/ack/find 配合 TODO/FIXME 关键词 |
| 基础验证 | `python -c`（内联语法/导入检查）、`node --check` |

## 项目目录结构

```
your-project/
├── .claude/
│   ├── hooks/
│   │   └── delivery-acceptance.py   # Stop Hook 脚本
│   ├── settings.local.json          # 本地 Hook 注册（手动创建）
│   └── settings.local.example.json  # 示例配置
├── .gitignore
└── README.md                        # 本文件
```

## 已知设计取舍

1. **会话记录仅读取末尾 2 MB，非完整文件。**
   — 极长会话中，如果修改发生在非常早期的轮次，可能超出这个窗口。
   此时文件系统扫描层（mtime 比对）仍然是主要检测手段，不会完全依赖
   会话记录回退。

2. **会话工作检测检测的是*尝试过的*工具调用，而非*成功*的。**
   — 被取消或失败的 Write 仍会出现在会话记录中。这是有意为之的
   假阳性倾向：不必要的拦截（用户可以说"跳过"）比静默漏检更安全。

3. **`.file_state.json` 每次运行都会更新。**
   — 快照在每次扫描后立即写回。这意味着同一会话内重试时，
   文件扫描层不会再检测到同一个变更；会话记录回退层会补上这个缺口。

4. **标记匹配是子串/正则匹配，而非结构化 JSONL 解析。**
   — 所有验证模式匹配原始转小写文本。这避免引入 JSONL 解析器，
   但理论上可能在极端情况下产生假匹配。实际测试中未发现此类案例。

## 维护笔记

- **扩展工具检测**（`detect_available_tools`）：如需支持更多生态
  （sbt、cmake、mix …）可新增配置文件检查项，使 Hook 的拦截信息
  给出更贴切的建议。
- **`.file_state.json` 增长**：在大型项目（超 10 万文件）中，
  `get_changed_files_scan` 的 `os.walk` 可能变慢。如有此问题，
  可限制遍历深度或改为仅 Git 检测。
- **Claude Code API 变更**：如果 Anthropic 修改了 stdin 负载的
  数据结构，Hook 的参数（`stop_hook_active`、`transcript_path`、
  `cwd`）需要相应更新。
- **假阳性反馈**：如果用户反馈"我没改文件但被拦截了"，请审查
  `SESSION_WORK_MARKERS` 和 `has_significant_changes` 是否过度匹配。

## License

Unlicense — 公有领域。详见 [LICENSE](LICENSE)。
