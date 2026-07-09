# Delivery Acceptance Stop Hook — v1.0.0

一个面向 [Claude Code](https://code.claude.com/docs/en/hooks) 的 Stop Hook，
用于在代码被修改但未经验证时阻止会话结束。

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

## 配置

### 注册 Hook

在 `.claude/settings.local.json`（项目级）或 `~/.claude/settings.json`
（用户级）中添加：

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
| **测试框架** | pytest、npm test、go test、cargo test、jest、mocha、unittest、tox、nosetests |
| **代码检查** | ruff check、flake8、pylint、eslint、black --check |
| **类型检查** | mypy、pyright、tsc --noEmit |
| **TODO 扫描** | grep/rg/ag/ack/find 配合 TODO/FIXME 关键词 |
| **基础验证** | `python -c`（内联语法/导入检查）、`node --check` |

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

## 许可

Unlicense — 公有领域。
