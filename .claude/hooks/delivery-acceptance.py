#!/usr/bin/env python3
"""
Delivery Acceptance Stop Hook for Claude Code — v1.0.0

Triggers on Stop event. Checks whether:
1. Files were modified (via git status, or file-timestamp tracking for non-git,
   or transcript evidence of Write/Edit tool calls)
2. If yes, whether verification (test/lint/typecheck/TODO scan) was actually executed
3. If verification missing, blocks the stop and tells Claude to continue

Exit codes:
  0 → allow stop (no changes / verification done / guard active)
  2 → block stop (verification required — stderr fed to Claude as context)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple


# ── Debug switch ────────────────────────────────────────────────────────
# Set to True to enable detailed JSONL logging to .claude/hook.log.
# Each invocation appends one JSON line with timestamp, event type,
# decision, and full context (stdin, changed_files, verification status).
DEBUG = False


# ── Verification patterns that indicate actual tool execution ──────────
# These match against the raw transcript JSONL content (compact JSON format,
# no spaces after colons: "command":"pytest" not "command": "pytest").
# They target Bash tool commands (not chat text) for reliability.

BASH_CMD_MARKERS = [
    # Test frameworks
    '"command":"pytest',
    '"command":"npm test',
    '"command":"go test',
    '"command":"cargo test',
    '"command":"jest',
    '"command":"mocha',
    '"command":"unittest',
    '"command":"tox',
    '"command":"nosetests',
    '"command":"python -m pytest',
    '"command":"npx jest',
    # Lint
    '"command":"ruff check',
    '"command":"flake8',
    '"command":"pylint',
    '"command":"eslint',
    '"command":"black --check',
    # Typecheck
    '"command":"mypy',
    '"command":"pyright',
    '"command":"tsc --noEmit',
    '"command":"tsc --no-emit',
    '"command":"npx tsc',
    # Minimal / ad-hoc verification (matched by get_suggested_minimal_checks)
    '"command":"python -c',        # python -c "import ast; ast.parse(...)" etc.
    '"command":"node --check',     # node --check <file>
]
BASH_CMD_MARKERS_LOWER = [m.lower() for m in BASH_CMD_MARKERS]

TODO_MARKERS = [
    # Regex patterns matching actual Bash tool invocations, not conversation text
    # Uses re.search so .* matches any characters.
    r'"command":"grep.*todo',
    r'"command":"grep.*fixme',
    r'"command":"rg .*todo',
    r'"command":"rg .*fixme',
    r'"command":"find.*todo',
    r'"command":"ag .*todo',
    r'"command":"ack .*todo',
]
TODO_MARKERS_LOWER = [m.lower() for m in TODO_MARKERS]

# ── Session-work markers ────────────────────────────────────────────────
# Fallback when file-scan sees no mtime delta (baseline already synced).
# These match the real transcript JSONL format: "type":"tool_use",...,"name":"write"
# Note: .*? between type and name to skip the "id":"call_..." field.

SESSION_WORK_MARKERS = [
    r'"type":"tool_use".*?"name":"write"',
    r'"type":"tool_use".*?"name":"edit"',
    r'"type":"tool_use".*?"name":"notebookedit"',
]
SESSION_WORK_MARKERS_LOWER = [m.lower() for m in SESSION_WORK_MARKERS]


# ── Project tooling detection ──────────────────────────────────────────

def detect_available_tools(project_root: str) -> List[Tuple[str, str, str]]:
    """
    Detect what verification tooling the project has configured.
    Returns list of (category, tool_name, run_suggestion).
    """
    found: List[Tuple[str, str, str]] = []
    root = Path(project_root)

    # ── pyproject.toml (Python) ──
    pp = root / "pyproject.toml"
    if pp.exists():
        text = pp.read_text("utf-8", errors="replace")
        if "pytest" in text:
            found.append(("test", "pytest", "pytest"))
        if "ruff" in text:
            found.append(("lint", "ruff", "ruff check"))
        if "mypy" in text:
            found.append(("typecheck", "mypy", "mypy ."))
        if "black" in text:
            found.append(("lint", "black", "black --check ."))

    # ── pytest.ini / setup.cfg ──
    if (root / "pytest.ini").exists():
        found.append(("test", "pytest", "pytest"))
    scfg = root / "setup.cfg"
    if scfg.exists() and "pytest" in scfg.read_text("utf-8", errors="replace"):
        found.append(("test", "pytest", "pytest"))

    # ── tox.ini ──
    if (root / "tox.ini").exists():
        found.append(("test", "tox", "tox"))

    # ── requirements-dev.txt with pytest ──
    req_dev = root / "requirements-dev.txt"
    if req_dev.exists() and "pytest" in req_dev.read_text("utf-8", errors="replace"):
        found.append(("test", "pytest", "pytest (from requirements-dev.txt)"))

    # ── package.json (Node) ──
    pj = root / "package.json"
    if pj.exists():
        text = pj.read_text("utf-8", errors="replace")
        if '"test"' in text:
            found.append(("test", "npm test", "npm test"))
        if '"lint"' in text or 'eslint' in text:
            found.append(("lint", "eslint", "npx eslint ."))
        if 'jest' in text:
            found.append(("test", "jest", "npx jest"))
        if 'tsc' in text or 'TypeScript' in text:
            found.append(("typecheck", "tsc", "npx tsc --noEmit"))

    # ── tsconfig.json ──
    if (root / "tsconfig.json").exists():
        already = any(t[1] == "tsc" for t in found)
        if not already:
            found.append(("typecheck", "tsc", "npx tsc --noEmit"))

    # ── go.mod (Go) ──
    if (root / "go.mod").exists():
        found.append(("test", "go test", "go test ./..."))

    # ── Cargo.toml (Rust) ──
    if (root / "Cargo.toml").exists():
        found.append(("test", "cargo test", "cargo test"))

    # ── ruff.toml ──
    if (root / "ruff.toml").exists():
        found.append(("lint", "ruff", "ruff check"))

    # ── .eslintrc ──
    for rc in (".eslintrc", ".eslintrc.json", ".eslintrc.js", ".eslintrc.yaml"):
        if (root / rc).exists():
            if not any(t[1] == "eslint" for t in found):
                found.append(("lint", "eslint", "npx eslint ."))

    # ── Deduplicate by tool name (keep first occurrence) ──
    seen: set[str] = set()
    deduped: List[Tuple[str, str, str]] = []
    for category, tool, cmd in found:
        if tool not in seen:
            seen.add(tool)
            deduped.append((category, tool, cmd))
    return deduped


def get_suggested_minimal_checks(project_root: str) -> List[str]:
    """
    When no test/lint/typecheck tooling is found, suggest reasonable
    minimal verification based on file types in the changes.
    Uses early-exit scanning to avoid walking the entire tree.
    """
    suggestions: List[str] = []
    root = Path(project_root)

    def _has_file(pattern: str, exclude: Tuple[str, ...] = ("node_modules", ".git", "__pycache__")) -> bool:
        """Check if any file matching `pattern` exists, excluding certain dirs."""
        for p in root.rglob(pattern):
            if not any(part in p.parts for part in exclude):
                return True
        return False

    if _has_file("*.py"):
        suggestions.append("Python 语法检查: python -c \"import ast; ast.parse(open('FILE').read())\"")
        suggestions.append("Python 导入检查: python -c \"import <module_name>\"")

    if _has_file("*.js") or _has_file("*.jsx") or _has_file("*.ts") or _has_file("*.tsx"):
        suggestions.append("Node 语法检查: node --check <file>")

    suggestions.append(
        "TODO/FIXME 扫描: grep -rn \"TODO\\|FIXME\\|HACK\\|XXX\" "
        "--include=\"*.py\" --include=\"*.js\" --include=\"*.ts\" src/ 2>/dev/null"
    )
    suggestions.append("空运行: 确认代码能正常加载/解析")

    return suggestions


# ── File change detection ──────────────────────────────────────────────

EXCLUDE_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".pytest_cache",
    ".egg-info", "dist", "build", ".venv", "venv", "env",
    ".mypy_cache", ".ruff_cache", ".claude",
})

CODE_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css",
    ".scss", ".json", ".md", ".yaml", ".yml", ".toml",
    ".cfg", ".ini", ".sh", ".bat", ".ps1", ".go", ".rs",
    ".java", ".c", ".h", ".cpp", ".rb", ".php", ".swift",
    ".kt", ".gradle", ".sql", ".r", ".lua",
})


def is_git_repo(path: str) -> bool:
    """Check if the given path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5,
            cwd=path,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_changed_files_git(project_root: str) -> List[str]:
    """Get list of changed files via git status --porcelain."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            cwd=project_root,
        )
        if result.returncode == 0:
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            return lines
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        pass
    return []


def get_changed_files_scan(project_root: str, state_file: Path) -> List[str]:
    """Track file changes via modification timestamps for non-git projects.

    Stores file path → mtime mappings in a JSON state file and compares
    on each run to detect additions, modifications, and deletions.
    """
    root = Path(project_root)

    # Walk the project tree and collect code files
    current: dict[str, float] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place so os.walk skips them entirely
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]

        for f in filenames:
            ext = Path(f).suffix.lower()
            if ext in CODE_EXTENSIONS or f in {"Dockerfile", "Makefile", ".gitignore"}:
                fp = Path(dirpath) / f
                try:
                    rel = str(fp.relative_to(root).as_posix())
                    current[rel] = os.path.getmtime(fp)
                except (OSError, ValueError):
                    pass

    # Load previous state
    previous: dict[str, float] = {}
    if state_file.exists():
        try:
            previous = json.loads(state_file.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    changed: list[str] = []
    # Detect new or modified files (mtime delta > 0.001s)
    for path, mtime in current.items():
        prev_mtime = previous.get(path)
        if prev_mtime is None or abs(prev_mtime - mtime) > 0.001:
            changed.append(f" M {path}")
    # Detect deleted files
    for path in previous:
        if path not in current:
            changed.append(f" D {path}")

    # Save current state for next comparison
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False),
            "utf-8",
        )
    except OSError:
        pass

    return changed


def get_changed_files(project_root: str) -> Tuple[List[str], str]:
    """Get list of changed files.

    Returns (changed_file_list, method_description).
    Tries git status first; falls back to file-timestamp scanning.
    """
    if is_git_repo(project_root):
        files = get_changed_files_git(project_root)
        return files, "git"

    # Not a git repo — use file-timestamp tracking
    state_file = Path(project_root) / ".claude" / ".file_state.json"
    files = get_changed_files_scan(project_root, state_file)
    if files:
        return files, "file-scan (no git repo)"
    return files, "file-scan (no git repo, no changes)"


def has_significant_changes(changed_files: List[str]) -> bool:
    """
    Check whether any of the changed files are significant (not generated/lock files).
    Returns True if there are changes that need verification.
    """
    ignore_patterns = [
        ".git/", "node_modules/", "__pycache__/", ".pytest_cache/",
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
        ".coverage", "*.pyc", ".egg-info/", "dist/", "build/",
        ".mypy_cache/", ".ruff_cache/",
    ]
    for entry in changed_files:
        path_part = entry[2:].strip() if len(entry) > 2 else entry
        if not path_part:
            continue
        ignored = False
        for pat in ignore_patterns:
            if pat.endswith("/"):
                if pat.rstrip("/") in path_part.replace("\\", "/").split("/"):
                    ignored = True
                    break
            elif path_part.endswith(pat.lstrip("*")) or pat in path_part:
                ignored = True
                break
        if not ignored:
            return True
    return False


# ── Transcript analysis ───────────────────────────────────────────────

def _read_transcript_tail(transcript_path: str) -> str:
    """
    Read the last 2 MB (whole lines) of a transcript file, lowercased.

    NOTE: 2 MB tail is a deliberate engineering trade-off:
    - Covers the most recent ~5000+ tool calls / ~30+ turns of activity
    - Avoids loading multi-GB transcripts from long sessions into memory
    - For the session-work fallback, we only care about *recent* edits
    - For the verification check, recent tool calls are the relevant ones
    - Edge case: an edit in the very first turn of an extremely long session
      could fall outside the 2 MB window.  In that case the file-scan layer
      (mtime comparison) is still the primary detection mechanism, so we
      are not relying solely on this tail read.
    """
    try:
        file_size = os.path.getsize(transcript_path)
    except OSError:
        return ""
    read_size = min(file_size, 2 * 1024 * 1024)
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            if file_size > read_size:
                f.seek(file_size - read_size)
                f.readline()
            return f.read().lower()
    except Exception:
        return ""


def check_transcript_for_session_work(transcript_path: str) -> bool:
    """
    Check whether the current transcript contains file-modifying tool calls.
    Fallback when file-scan finds no mtime delta.

    NOTE: This detects *attempted* tool invocations (Write, Edit, etc.),
    not necessarily *successful* ones.  A failed or cancelled Write still
    appears in the transcript.  This is an intentional trade-off: false
    positives are safer (the user can say "skip" to bypass), while false
    negatives would silently skip verification.
    """
    content_lower = _read_transcript_tail(transcript_path)
    if not content_lower:
        return False
    for pat in SESSION_WORK_MARKERS_LOWER:
        if re.search(pat, content_lower):
            return True
    return False


def check_transcript_for_verification(transcript_path: str) -> Tuple[bool, str]:
    """
    Scan the transcript JSONL for actual verification tool executions.

    Uses streaming reads (bounded to 2 MB of tail content) to avoid
    loading large files into memory. Pre-lowered markers skip redundant
    .lower() calls in the hot loop.

    Returns (verified, evidence_description).
    """
    content_lower = _read_transcript_tail(transcript_path)
    if not content_lower:
        return False, ""

    # Check for Bash tool calls with verification commands
    for pattern in BASH_CMD_MARKERS_LOWER:
        idx = content_lower.find(pattern)
        if idx != -1:
            start = max(0, idx - 60)
            end = min(len(content_lower), idx + 80)
            snippet = content_lower[start:end].replace("\n", " ").strip()
            return True, snippet

    # Check for TODO/FIXME scans (regex because markers use .* patterns)
    for pattern in TODO_MARKERS_LOWER:
        if re.search(pattern, content_lower):
            return True, "TODO/FIXME 扫描已执行"

    return False, ""


# ── Main ──────────────────────────────────────────────────────────────

def _log_debug(event_type: str, **fields: object) -> None:
    """Append a JSONL entry to .claude/hook.log when DEBUG=True.

    Logs to {{cwd}}/.claude/hook.log.  Silently ignores errors so
    debug logging never affects Hook behaviour.
    """
    if not DEBUG:
        return
    try:
        log_path = Path.cwd() / ".claude" / "hook.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event": event_type,
            **fields,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> None:
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        _log_debug("PARSE_FAIL", action="ALLOW", reason="stdin parse error")
        sys.exit(0)

    stop_hook_active = str(input_data.get("stop_hook_active", "false")).lower()
    transcript_path = input_data.get("transcript_path", "") or ""
    project_root = input_data.get("cwd", "") or ""

    is_retry = stop_hook_active == "true"

    if not project_root or not os.path.isdir(project_root):
        _log_debug(
            "INVALID_CWD",
            action="ALLOW",
            reason="cwd not a directory",
            project_root=project_root,
        )
        sys.exit(0)

    # ── Step 1: Detect file changes ──
    # Two layers:
    #   a) File-scan (mtime delta from last Hook run, or git status)
    #   b) Transcript fallback (Write/Edit tool calls in session)
    changed, method = get_changed_files(project_root)
    has_real_changes = has_significant_changes(changed)

    if not has_real_changes and transcript_path and os.path.isfile(transcript_path):
        if check_transcript_for_session_work(transcript_path):
            has_real_changes = True
            method = "transcript (session Write/Edit detected)"

    if not has_real_changes:
        _log_debug(
            "NO_CHANGES",
            action="ALLOW",
            reason="no file changes detected",
            stop_hook_active=stop_hook_active,
            changed_files=changed,
            change_method=method,
        )
        sys.exit(0)

    # ── Step 2: Check transcript for verification evidence ──
    verified = False
    evidence = ""
    if transcript_path and os.path.isfile(transcript_path):
        verified, evidence = check_transcript_for_verification(transcript_path)

    # ── Step 3 (retry): after a previous block, block again ──
    if is_retry:
        if verified:
            _log_debug(
                "RETRY_ALLOW",
                action="ALLOW",
                reason="retry, verification now found",
                stop_hook_active=stop_hook_active,
                changed_files=changed,
                change_method=method,
                verified_files=evidence,
            )
            sys.exit(0)
        changed_again, method2 = get_changed_files(project_root)
        _log_debug(
            "RETRY_BLOCK",
            action="BLOCK",
            reason="retry, still unverified",
            stop_hook_active=stop_hook_active,
            changed_files=changed_again,
            change_method=method2,
        )
        change_desc = f"\n检测方法: {method2} | 修改文件数: {len(changed_again)}"
        print_block_message(
            project_root, changed_again,
            available_tools=None, is_retry=True, track_method=method2,
        )
        print(change_desc, file=sys.stderr)
        sys.exit(2)

    # ── First time: if verified, allow ──
    if verified:
        _log_debug(
            "ALLOW",
            action="ALLOW",
            reason="first-time, verification found",
            stop_hook_active=stop_hook_active,
            changed_files=changed,
            change_method=method,
            verified_files=evidence,
        )
        sys.exit(0)

    # ── Step 4: Detect available tooling ──
    available = detect_available_tools(project_root)

    _log_debug(
        "BLOCK",
        action="BLOCK",
        reason="first-time, no verification found",
        stop_hook_active=stop_hook_active,
        changed_files=changed,
        change_method=method,
    )

    # ── Step 5: Block with guidance ──
    print_block_message(
        project_root, changed,
        available_tools=available, is_retry=False, track_method=method,
    )
    sys.exit(2)


def print_block_message(
    project_root: str,
    changed_files: List[str],
    available_tools: List[Tuple[str, str, str]] | None,
    is_retry: bool,
    track_method: str = "",
) -> None:
    """Print block message to stderr — this becomes Claude's context."""
    lines: List[str] = []

    if is_retry:
        lines.append("⛔ 交付验收仍未通过（上次拦截后仍未完成验证）")
        lines.append("")
        lines.append("⚠️  注意：连续拦截约 8 次后 Claude 将自动结束本轮。")
        lines.append("")
    else:
        lines.append("⛔ 交付验收未通过")
        lines.append("")

    method_info = f" (检测方式: {track_method})" if track_method else ""
    lines.append(f"检测到文件修改 ({len(changed_files)} 个文件变动{method_info})：")
    for entry in changed_files[:5]:
        lines.append(f"  {entry}")
    if len(changed_files) > 5:
        lines.append(f"  ... 及另外 {len(changed_files) - 5} 个文件")
    lines.append("")

    if available_tools:
        lines.append("📋 项目中检测到以下可用验证工具：")
        for category, tool, cmd in available_tools:
            icon = {"test": "🧪", "lint": "🔍", "typecheck": "📐"}.get(category, "•")
            lines.append(f"  {icon} [{category}] {tool} → `{cmd}`")
        lines.append("")
        cats = set(t[0] for t in available_tools)
        missing = [c for c in ("test", "lint", "typecheck") if c not in cats]
        if missing:
            lines.append("⚠️  以下类别的验证工具未检测到配置：")
            name_map = {"test": "测试框架", "lint": "Lint 工具", "typecheck": "类型检查工具"}
            for m in missing:
                lines.append(f"  - {name_map.get(m, m)}")
            lines.append("")
    else:
        lines.append("📋 项目未检测到专用验证工具配置。")
        lines.append("")
        suggested = get_suggested_minimal_checks(project_root)
        if suggested:
            lines.append("建议进行以下基础验证：")
            for s in suggested:
                lines.append(f"  • {s}")
            lines.append("")

    lines.append("🔧 请继续完成至少一项验证后再结束本轮工作。")
    lines.append("   验证通过后尝试结束，本钩子会重新检查并放行。")
    lines.append("")
    lines.append("💡 如需跳过（仅限合理理由），请在对话中说明：")
    lines.append("   \"本轮修改已完成，跳过交付验收\" 并说明原因。")

    print("\n".join(lines), file=sys.stderr)


if __name__ == "__main__":
    main()
