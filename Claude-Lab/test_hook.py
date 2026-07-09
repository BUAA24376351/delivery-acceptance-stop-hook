#!/usr/bin/env python3
"""
Stop Hook 验收测试 (v2)
模拟 Claude Code 调用 Hook 的完整过程。
使用临时目录精确控制"是否有变更"。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


HOOK_SCRIPT = Path("D:/Claude-Lab/.claude/hooks/delivery-acceptance.py")
PROJECT_ROOT = "D:/Claude-Lab"


def simulate_stop(cwd: str, transcript_path: str, is_retry: bool = False) -> subprocess.CompletedProcess:
    """
    模拟 Claude Code 触发 Stop 事件。

    Claude Code 引擎的行为：
    1. 构建 stdin JSON → {stop_hook_active, transcript_path, cwd}
    2. fork 子进程 → python .claude/hooks/delivery-acceptance.py
    3. 通过 pipe 把 stdin JSON (bytes) 写入子进程
    4. 读 exit code：
       0 → 放行
       2 → 拦截，把 stderr bytes 喂给 Claude 作为上下文
    """
    payload = {
        "stop_hook_active": is_retry,
        "cwd": cwd,
        "transcript_path": transcript_path or "",
    }

    # bytes 输入模拟实际 pipe 传输
    proc = subprocess.run(
        ["python", str(HOOK_SCRIPT)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=10,
    )
    return proc


def create_mock_transcript(contains_verification: bool) -> str:
    """创建模拟 transcript JSONL，模拟真实的工具调用日志格式。"""
    fd, path = tempfile.mkstemp(suffix=".jsonl", prefix="mock_transcript_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        # 模拟对话
        f.write('{"role":"user","content":"帮我改一下代码"}\n')
        f.write('{"role":"assistant","content":"让我看看"}\n')
        # 模拟文件读取
        f.write(
            '{"type":"tool_use","id":"call_00_mock","name":"Read",'
            '"input":{"file_path":"test_app.py"}}\n'
        )
        # 模拟非验证命令（ls、echo 等）
        f.write(
            '{"type":"tool_use","id":"call_00_mock","name":"Bash",'
            '"input":{"command":"ls -la"}}\n'
        )
        # 模拟文件写入
        f.write(
            '{"type":"tool_use","id":"call_00_mock","name":"Write",'
            '"input":{"file_path":"test_app.py","content":"..."}}\n'
        )

        if contains_verification:
            # 模拟运行测试
            f.write(
                '{"type":"tool_use","id":"call_00_mock","name":"Bash",'
                '"input":{"command":"pytest -v","description":"run tests"}}\n'
            )

        f.write('{"role":"user","content":"改完了"}\n')

    return path


def create_test_code_file(dirpath: str, name: str = "app.py") -> str:
    """在给定目录下创建一个简单的测试代码文件。"""
    content = '''
def greet(name: str) -> str:
    return f"Hello, {name}!"


def add(a: int, b: int) -> int:
    return a + b


if __name__ == "__main__":
    print(greet("World"))
'''
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.strip())
    return path


def print_scenario(num: int, title: str, proc: subprocess.CompletedProcess):
    """格式化输出场景结果。"""
    print("=" * 62)
    print(f"  场景 {num}: {title}")
    print("=" * 62)

    action = "✅ 放行 (exit 0)" if proc.returncode == 0 else "⛔ 拦截 (exit 2)"
    print(f"  结果: {action}")
    print()

    if proc.stderr:
        stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
        if stderr_text:
            print("  ── stderr 输出（给 Claude 的拦截信息）──")
            for line in stderr_text.split("\n"):
                print(f"  | {line}")
            print("  ─────────────────────────────────────")
        else:
            print("  (stderr 为空)")

    print()


def analyze_path(num: int, proc: subprocess.CompletedProcess, desc: str):
    """分析 Hook 的执行路径。"""
    if proc.returncode == 0:
        if not proc.stderr:
            reason = "无变更 → 直接放行"
        else:
            reason = "有变更但验证已通过 → 放行"
    else:
        reason = "有变更且无验证记录 → 拦截"
    print(f"  → {desc}: {reason}")


def main():
    print("=" * 62)
    print("  Stop Hook 验收测试")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  项目: {PROJECT_ROOT}")
    print(f"  Git 仓库: {os.path.isdir(os.path.join(PROJECT_ROOT, '.git'))}")
    print(f"  Hook 脚本: {HOOK_SCRIPT}")
    print(f"  Hook 存在: {HOOK_SCRIPT.exists()}")
    print("=" * 62)
    print()

    # ═══════════════════════════════════════════════════════════════
    # 场景 1：基线 — 无代码变更
    # 用空目录模拟"啥也没改"的会话
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_empty_") as empty_dir:
        proc = simulate_stop(cwd=empty_dir, transcript_path="")
        print_scenario(1, "基线测试：无代码变更（空目录）", proc)
        analyze_path(1, proc, "空目录 → 文件扫描无结果 → 无变更 → exit 0")

    # ═══════════════════════════════════════════════════════════════
    # 场景 2：有代码变更，无验证记录 → 应拦截
    # 模拟：Claude 改了一个文件，没跑测试就想结束
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_change_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        proc = simulate_stop(cwd=working_dir, transcript_path="")
        print_scenario(2, "有变更 + 无验证 → 应拦截", proc)
        analyze_path(2, proc, "新建 app.py + 空 transcript → 变更检测命中 → 无验证记录 → exit 2")

    # ═══════════════════════════════════════════════════════════════
    # 场景 3：有代码变更，有 pytest 验证记录 → 应放行
    # 模拟：Claude 改完文件后跑了 pytest，然后结束
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_verified_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        transcript = create_mock_transcript(contains_verification=True)
        proc = simulate_stop(cwd=working_dir, transcript_path=transcript)
        print_scenario(3, "有变更 + transcript 含 pytest → 应放行", proc)
        analyze_path(3, proc, "新建 app.py + transcipt 有 pytest → 变更检测命中 → 验证匹配 → exit 0")
        os.unlink(transcript)

    # ═══════════════════════════════════════════════════════════════
    # 场景 4：有变更，transcript 有但只有 ls/echo → 应拦截
    # 证明：exit_code:0 不再导致误放行
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_no_verify_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        transcript = create_mock_transcript(contains_verification=False)
        proc = simulate_stop(cwd=working_dir, transcript_path=transcript)
        print_scenario(4, "有变更 + transcript 只有 ls/echo → 应拦截 (回归测试 exit_code:0 bug)", proc)
        analyze_path(4, proc, "transcript 有 ls 但无 pytest → 验证标记不匹配 → exit 2")
        os.unlink(transcript)

    # ═══════════════════════════════════════════════════════════════
    # 场景 5：重试拦截 — stop_hook_active=true，仍无验证
    # 模拟：第一次被拦后，Claude 仍然没跑验证就再次试图结束
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_retry_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        transcript = create_mock_transcript(contains_verification=False)
        proc = simulate_stop(cwd=working_dir, transcript_path=transcript, is_retry=True)
        print_scenario(5, "重试拦截 (stop_hook_active=true, 仍无验证)", proc)
        analyze_path(5, proc, "is_retry=true + 验证未补上 → 拦截（语气更强 + 含 8 次上限警告）")
        # 检查拦截信息是否包含"仍未通过"
        stderr = proc.stderr.decode("utf-8", errors="replace")
        if "仍未通过" in stderr:
            print("  ✓ 拦截信息包含 '仍未通过' 重试语气")
        if "8 次" in stderr:
            print("  ✓ 包含 8 次上限警告")
        os.unlink(transcript)

    # ═══════════════════════════════════════════════════════════════
    # 场景 6：重试放行 — 第一次被拦后补上了验证
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_retry_pass_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        transcript = create_mock_transcript(contains_verification=True)
        proc = simulate_stop(cwd=working_dir, transcript_path=transcript, is_retry=True)
        print_scenario(6, "重试放行 (stop_hook_active=true, 验证已补上)", proc)
        analyze_path(6, proc, "is_retry=true + 验证已补上 → exit 0 放行")
        os.unlink(transcript)

    # ═══════════════════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════════════════
    print("=" * 62)
    print("  测试总结")
    print("=" * 62)
    print(f"""
  {'场景 1: 无变更 → 放行':.<45} ...
  {'场景 2: 有变更+无验证 → 拦截':.<45} ...
  {'场景 3: 有变更+有验证 → 放行':.<45} ...
  {'场景 4: 仅 ls/echo → 拦截 (exit_code:0 bug 回归)':.<45} ...
  {'场景 5: 重试拦截 (语气加强)':.<45} ...
  {'场景 6: 重试放行':.<45} ...
""")
    print("  （结果见上方各场景输出）")
    print("=" * 62)


if __name__ == "__main__":
    main()
