# Delivery Acceptance Stop Hook

一个面向 [Claude Code](https://code.claude.com/docs/en/hooks) 的 Stop Hook，
在代码被修改但未经验证（测试 / lint / 类型检查 / TODO 扫描）时阻止会话结束。

## 功能

- **自动拦截** —— Claude 完成一轮工作时自动触发，无需手动操作
- **两层变更检测** —— Git 状态（或 mtime 比对）+ 会话记录回退
- **验证识别** —— 自动在会话记录中查找 pytest、ruff、mypy 等验证命令的执行痕迹
- **重试支持** —— 首次拦截后，Claude 补上验证后可再次触发并放行
- **智能建议** —— 根据项目配置自动检测可用工具并给出验证建议

## 工作原理

```
Claude 完成一轮工作 → Stop Hook 触发
                              │
                              ▼
              检测文件是否有变更
              ├─ 无变更 → ✅ 放行
              └─ 有变更 → 检查是否执行了验证
                   ├─ 已验证 → ✅ 放行
                   └─ 未验证 → ⛔ 拦截，给出指导
```

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

   也可参见提供的示例配置 `.claude/settings.local.example.json`。

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
│   │   ├── delivery-acceptance.py   # Stop Hook 脚本
│   │   └── README.md                # Hook 详细文档
│   ├── settings.local.json          # 本地 Hook 注册（手动创建）
│   └── settings.local.example.json  # 示例配置
├── .gitignore
└── README.md                        # 本文件
```

## 设计文档

详细的原理、配置说明和已知取舍请参见 [hooks/README.md](.claude/hooks/README.md)。

## License

Unlicense — 公有领域。详见 [LICENSE](LICENSE)。
