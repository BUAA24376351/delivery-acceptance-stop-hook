#!/usr/bin/env python3
"""
Stop Hook 验收测试 (v2)
模拟 Claude Code 调用 Hook 的完整过程。
使用临时目录精确控制"是否有变更"。
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


HOOK_SCRIPT = Path(__file__).resolve().parent / ".claude" / "hooks" / "delivery-acceptance.py"
PROJECT_ROOT = str(Path(__file__).resolve().parent)


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


def create_transcript_with_commands(commands: list, include_write: bool = True) -> str:
    """Create a mock transcript JSONL containing specific Bash commands.

    Uses the **real** Claude Code transcript format where tool_use records
    are nested inside ``message.content[]`` (not the flat test format).

    Args:
        commands: List of Bash command strings to include in the transcript.
        include_write: If True, also includes a Write tool call so the
                       session-work fallback detects changes.

    Returns:
        Path to the temporary transcript file (caller should unlink).
    """
    import uuid as _uuid

    def _make_assistant_line(tool_uses):
        """Build one assistant-type JSONL line with nested tool_use entries."""
        content = []
        for tu_name, tu_input in tool_uses:
            content.append({
                "type": "tool_use",
                "id": "call_%s" % _uuid.uuid4().hex[:12],
                "name": tu_name,
                "input": tu_input,
            })
        rec = {
            "type": "assistant",
            "message": {
                "id": "msg_%s" % _uuid.uuid4().hex[:12],
                "type": "message",
                "role": "assistant",
                "model": "claude",
                "content": content,
            },
        }
        return json.dumps(rec, ensure_ascii=False)

    fd, path = tempfile.mkstemp(suffix=".jsonl", prefix="mock_transcript_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write('{"role":"user","content":"帮我改代码"}\n')
        f.write('{"role":"assistant","content":"好的"}\n')
        # Read tool call
        f.write(_make_assistant_line([
            ("Read", {"file_path": "app.py"}),
        ]) + "\n")
        # Write tool call (triggers session-work detection)
        if include_write:
            f.write(_make_assistant_line([
                ("Write", {"file_path": "app.py", "content": "..."}),
            ]) + "\n")
        # Bash commands — one per line (as in real transcripts)
        for cmd in commands:
            f.write(_make_assistant_line([
                ("Bash", {"command": cmd, "description": "run command"}),
            ]) + "\n")
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
    # 场景 7：python test_*.py 直接执行测试文件 → 应放行
    # 验证新分类器 Layer 2 的文件名模式匹配
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_pyfile_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        transcript = create_transcript_with_commands(
            ["python test_calculator.py", "ls -la"]
        )
        proc = simulate_stop(cwd=working_dir, transcript_path=transcript)
        print_scenario(7, "python test_*.py 测试文件执行 → 应放行", proc)
        analyze_path(7, proc,
                     "transcript 含 python test_calculator.py → Layer 2 test-file 匹配 → exit 0")
        os.unlink(transcript)

    # ═══════════════════════════════════════════════════════════════
    # 场景 8：uv run / poetry run / pipenv run → 应放行
    # 验证 runner prefix stripping + Layer 1 工具匹配
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_runner_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        transcript = create_transcript_with_commands([
            "uv run pytest -v",
            "ls -la",
            "echo done",
        ])
        proc = simulate_stop(cwd=working_dir, transcript_path=transcript)
        print_scenario(8, "uv run pytest → 应放行 (runner prefix strip)", proc)
        analyze_path(8, proc,
                     "transcript 含 uv run pytest → strip uv run → pytest 匹配 Layer 1 → exit 0")
        os.unlink(transcript)

    # ═══════════════════════════════════════════════════════════════
    # 场景 9：pnpm test / bun test → 应放行
    # 验证 Layer 4 包管理器测试脚本
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_pnpm_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        transcript = create_transcript_with_commands(["pnpm test"])
        proc = simulate_stop(cwd=working_dir, transcript_path=transcript)
        print_scenario(9, "pnpm test → 应放行 (Layer 4)", proc)
        analyze_path(9, proc,
                     "transcript 含 pnpm test → Layer 4 包管理器脚本匹配 → exit 0")
        os.unlink(transcript)

    # ═══════════════════════════════════════════════════════════════
    # 场景 10：python -m unittest discover → 应放行
    # 验证 Layer 2 的 -m <test_module> 模式匹配
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_unittest_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        transcript = create_transcript_with_commands(
            ["python -m unittest discover -s tests"]
        )
        proc = simulate_stop(cwd=working_dir, transcript_path=transcript)
        print_scenario(10, "python -m unittest discover → 应放行", proc)
        analyze_path(10, proc,
                     "transcript 含 python -m unittest → Layer 2 test module 匹配 → exit 0")
        os.unlink(transcript)

    # ═══════════════════════════════════════════════════════════════
    # 场景 11：npm install / git status / cat → 应拦截
    # 验证普通命令不会误判为验证 (安全性回归)
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_safety_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        transcript = create_transcript_with_commands([
            "npm install",
            "git status",
            "cat README.md",
        ])
        proc = simulate_stop(cwd=working_dir, transcript_path=transcript)
        print_scenario(11, "npm install / git status / cat → 应拦截 (安全性)", proc)
        analyze_path(11, proc,
                     "npm install → 不是 test/lint 脚本; git/cat → 不匹配任何层 → exit 2")
        os.unlink(transcript)

    # ═══════════════════════════════════════════════════════════════
    # 场景 12：python main.py (含 Write 但无 test 关键词) → 应拦截
    # 验证 "contest.py" / "latest.py" 等不会触发子串匹配
    # ═══════════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory(prefix="hook_test_nottest_") as working_dir:
        test_file = create_test_code_file(working_dir, "app.py")
        transcript = create_transcript_with_commands([
            "python main.py",
            "python contest.py",
            "python latest.py",
        ])
        proc = simulate_stop(cwd=working_dir, transcript_path=transcript)
        print_scenario(12, "python main.py / contest.py (无 test 词边界) → 应拦截", proc)
        analyze_path(12, proc,
                     "contest.py 含 'test' 但不是词边界 → 不匹配 Layer 2 → exit 2")
        os.unlink(transcript)

    # ═══════════════════════════════════════════════════════════════
    # 分类器单元测试
    # ═══════════════════════════════════════════════════════════════
    run_classifier_unit_tests()

    # ═══════════════════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════════════════
    print("=" * 62)
    print("  测试总结 (集成测试)")
    print("=" * 62)
    print(f"""
  {'场景 1: 无变更 → 放行':.<50} ...
  {'场景 2: 有变更+无验证 → 拦截':.<50} ...
  {'场景 3: 有变更+有验证(pytest) → 放行':.<50} ...
  {'场景 4: 仅 ls/echo → 拦截 (exit_code:0 bug 回归)':.<50} ...
  {'场景 5: 重试拦截 (语气加强)':.<50} ...
  {'场景 6: 重试放行':.<50} ...
  ── 新场景 ──
  {'场景 7: python test_*.py → 放行 (Layer 2)':.<50} ...
  {'场景 8: uv run pytest → 放行 (runner strip)':.<50} ...
  {'场景 9: pnpm test → 放行 (Layer 4)':.<50} ...
  {'场景 10: python -m unittest → 放行 (Layer 2)':.<50} ...
  {'场景 11: npm install/git/cat → 拦截 (安全)':.<50} ...
  {'场景 12: python main.py → 拦截 (非测试)':.<50} ...
""")
    print("  （结果见上方各场景输出 + 下方分类器单元测试）")
    print("=" * 62)


# ═══════════════════════════════════════════════════════════════════
# 分类器单元测试 — 直接测试 is_verification_command()
# ═══════════════════════════════════════════════════════════════════

def run_classifier_unit_tests():
    """Directly test is_verification_command() against ~40 commands.

    Imports the function from the hook script via importlib so we test
    the actual production code, not a copy.
    """
    import importlib.util

    # Load the hook module
    spec = importlib.util.spec_from_file_location(
        "delivery_acceptance", str(HOOK_SCRIPT)
    )
    hook_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook_mod)
    is_verif = hook_mod.is_verification_command

    # (command, expected_verdict, description)
    cases = [
        # ── Layer 1: Known tools ──────────────────────────────────
        ("pytest", True, "pytest bare"),
        ("pytest -v --tb=short", True, "pytest with flags"),
        ("jest --coverage", True, "jest"),
        ("mocha --reporter spec", True, "mocha"),
        ("vitest run", True, "vitest"),
        ("cypress run", True, "cypress"),
        ("playwright test", True, "playwright"),
        ("ruff check .", True, "ruff check"),
        ("flake8 src/", True, "flake8"),
        ("eslint . --fix", True, "eslint"),
        ("mypy src/", True, "mypy"),
        ("pyright --outputjson", True, "pyright"),
        ("black --check .", True, "black --check"),
        ("prettier --check '**/*.ts'", True, "prettier --check"),
        ("go test ./...", True, "go test"),
        ("go vet ./...", True, "go vet"),
        ("go build ./cmd/...", True, "go build"),
        ("cargo test", True, "cargo test"),
        ("cargo clippy --all-targets", True, "cargo clippy"),
        ("cargo check", True, "cargo check"),
        ("cargo build --release", True, "cargo build"),
        ("deno test", True, "deno test"),
        ("deno lint", True, "deno lint"),
        ("deno check main.ts", True, "deno check"),
        ("shellcheck script.sh", True, "shellcheck"),
        ("make test", True, "make test"),
        ("make check", True, "make check"),
        ("ninja test", True, "ninja test"),
        ("tox", True, "tox"),
        ("nox", True, "nox"),
        ("nosetests", True, "nosetests"),
        ("tsc --noEmit", True, "tsc --noEmit"),
        ("tsc --no-emit", True, "tsc --no-emit"),

        # ── Layer 2: Test-file execution ──────────────────────────
        ("python test_calculator.py", True, "python test_*.py"),
        ("python3 test_app.py", True, "python3 test_*.py"),
        ("python calculator_test.py", True, "python *_test.py"),
        ("python tests/test_foo.py", True, "python tests/*.py (dir)"),
        ("py test_thing.py", True, "Windows py launcher"),
        ("python -m pytest -v", True, "python -m pytest"),
        ("python -m unittest discover", True, "python -m unittest"),
        ("python -m unittest test_module", True, "python -m unittest mod"),
        ("node test_app.js", True, "node test_*.js"),
        ("node app.test.js", True, "node *.test.js"),
        ("node app.spec.js", True, "node *.spec.js"),
        ("node __tests__/app.js", True, "node __tests__/*.js"),
        ("node -e 'require(\"./test\")'", True, "node -e (inline)"),

        # ── Layer 3: Inline verification ──────────────────────────
        ("python -c \"import ast; ast.parse(open('f').read())\"", True, "python -c"),
        ("python3 -c \"print('hello')\"", True, "python3 -c"),
        ("node --check app.js", True, "node --check"),
        ("ruby -c script.rb", True, "ruby -c"),
        ("perl -c script.pl", True, "perl -c"),

        # ── Layer 4: Package manager scripts ──────────────────────
        ("npm test", True, "npm test"),
        ("npm run test", True, "npm run test"),
        ("npm run test:unit", True, "npm run test:unit"),
        ("npm run lint", True, "npm run lint"),
        ("pnpm test", True, "pnpm test"),
        ("pnpm run test", True, "pnpm run test"),
        ("yarn test", True, "yarn test"),
        ("yarn run lint", True, "yarn run lint"),
        ("bun test", True, "bun test"),
        ("bun run test", True, "bun run test"),
        ("npm run typecheck", True, "npm run typecheck"),
        ("npm run verify", True, "npm run verify"),
        ("npm run ci", True, "npm run ci"),

        # ── Layer 5: TODO scans ───────────────────────────────────
        ("grep -r TODO src/", True, "grep TODO"),
        ("rg -i fixme .", True, "rg fixme"),
        ("find . -name '*.py' | xargs grep TODO", True, "find + TODO"),
        ("ag HACK src/", True, "ag HACK"),
        ("git grep -i xxx", True, "git grep xxx"),
        ("grep -rn 'FIXME\\|HACK' --include='*.py'", True, "grep FIXME|HACK"),

        # ── Runner stripping ─────────────────────────────────────
        ("uv run pytest", True, "uv run pytest"),
        ("poetry run pytest -v", True, "poetry run pytest"),
        ("pipenv run python -m pytest", True, "pipenv run python -m pytest"),
        ("npx jest --coverage", True, "npx jest"),
        ("npx vitest run", True, "npx vitest"),
        ("npx eslint .", True, "npx eslint"),
        ("npx tsc --noEmit", True, "npx tsc --noEmit"),
        ("pnpm exec jest", True, "pnpm exec jest"),

        # ── SAFETY: Must NOT match ────────────────────────────────
        ("ls -la", False, "ls"),
        ("echo 'hello world'", False, "echo"),
        ("pwd", False, "pwd"),
        ("cd /tmp", False, "cd"),
        ("mkdir -p foo/bar", False, "mkdir"),
        ("rm -rf node_modules", False, "rm"),
        ("cp file1 file2", False, "cp"),
        ("mv old new", False, "mv"),
        ("cat README.md", False, "cat"),
        ("head -20 file.txt", False, "head"),
        ("tail -f log.txt", False, "tail"),
        ("git status", False, "git status"),
        ("git diff --cached", False, "git diff"),
        ("git log --oneline", False, "git log"),
        ("npm install", False, "npm install"),
        ("npm run build", False, "npm run build"),
        ("npm run dev", False, "npm run dev"),
        ("npm run start", False, "npm run start"),
        ("pip install pytest", False, "pip install"),
        ("pip3 freeze", False, "pip3 freeze"),
        ("docker ps", False, "docker ps"),
        ("whoami", False, "whoami"),
        ("clear", False, "clear"),
        ("python main.py", False, "python main.py (no test)"),
        ("python server.py", False, "python server.py"),
        ("python contest.py", False, "contest (test substring)"),
        ("python latest.py", False, "latest (test substring)"),
        ("python testing.py", False, "testing (no boundary)"),
        ("node server.js", False, "node server.js"),
        ("node index.js", False, "node index.js"),
        ("npx create-react-app my-app", False, "npx non-test tool"),
        ("make build", False, "make build (not test/check)"),
    ]

    print("=" * 62)
    print("  分类器单元测试: is_verification_command()")
    print("=" * 62)

    passed = 0
    failed = 0
    for cmd, expected, desc in cases:
        ok, cat = is_verif(cmd)
        if ok == expected:
            passed += 1
        else:
            failed += 1
            verdict = "VERIFY" if ok else "NOT"
            exp_str = "VERIFY" if expected else "NOT"
            print(f"  FAIL: [{desc}] cmd={cmd!r}")
            print(f"        expected={exp_str}  got={verdict}  category={cat!r}")

    print(f"\n  结果: {passed} 通过, {failed} 失败 (共 {len(cases)} 项)")
    if failed > 0:
        print("  ⚠️  存在失败用例，请检查！")
    else:
        print("  ✅ 所有分类器单元测试通过")
    print("=" * 62)
    print()


if __name__ == "__main__":
    main()
