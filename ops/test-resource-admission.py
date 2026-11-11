#!/usr/bin/env python3
"""Regression tests for the narrow resource-admission hook."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "ops" / "guards" / "resource-admission.py"
HOOKS = ROOT / "ops" / "guards" / "resource-hooks.json"


def run_hook(command: str, state_dir: Path, slots: int = 2, pid: int | None = None):
    env = os.environ.copy()
    env["CODEX_RESOURCE_ADMISSION_STATE_DIR"] = str(state_dir)
    env["CODEX_RESOURCE_ADMISSION_SLOTS"] = str(slots)
    env["CODEX_RESOURCE_ADMISSION_MIN_FREE_GIB"] = "0"
    if pid is not None:
        env["CODEX_RESOURCE_ADMISSION_PID"] = str(pid)
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "exec_command",
        "tool_input": {"command": command},
        "session_id": "test-session",
        "cwd": str(ROOT),
    }
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("  ok   " if condition else "  FAIL ") + name + (f" ({detail})" if detail else ""))
    if not condition:
        raise SystemExit(1)


def main() -> None:
    check("资源准入 hook 独立存在", GUARD.is_file())
    check("hooks 片段只含追加式 PreToolUse", HOOKS.is_file())
    hook_config = json.loads(HOOKS.read_text(encoding="utf-8"))
    matches = hook_config["hooks"]["PreToolUse"]
    check("matcher 覆盖 exec_command",
          any("exec_command" in str(group.get("matcher", "")) for group in matches))
    check("hook 命令指向资源准入脚本",
          any("resource-admission.py" in hook["command"]
              for group in matches for hook in group.get("hooks", [])))

    with tempfile.TemporaryDirectory(prefix="resource-admission-test-") as raw:
        state = Path(raw)
        normal = run_hook("rg resource-admission ops", state)
        check("普通只读命令不被拦截",
              normal.returncode == 0 and not normal.stdout.strip(), normal.stderr)
        read_tsc = run_hook("rg 'tsc --noEmit' docs", state)
        check("搜索 tsc 字样不被误拦",
              read_tsc.returncode == 0 and not read_tsc.stdout.strip(), read_tsc.stderr)

        first = run_hook("npx tsc --noEmit", state, slots=2, pid=os.getpid())
        check("第一个重验证放行", not first.stdout.strip(), first.stdout + first.stderr)
        second = run_hook("npx playwright test", state, slots=2, pid=os.getpid())
        check("第二个重验证放行", not second.stdout.strip(), second.stdout + second.stderr)
        third = run_hook("npm exec vite build", state, slots=2, pid=os.getpid())
        check("超出租约时明确拒绝",
              third.returncode == 0 and "资源准入" in third.stdout,
              third.stdout + third.stderr)
        check("拒绝并不终止已有命令", "不会被终止" in third.stdout, third.stdout)

        leases_path = state / "leases.json"
        raw_state = json.loads(leases_path.read_text(encoding="utf-8"))
        raw_state["leases"][0]["pid"] = 99999999
        raw_state["leases"][0]["pgid"] = 0
        leases_path.write_text(json.dumps(raw_state), encoding="utf-8")
        recovered = run_hook("npx tsc --noEmit", state, slots=2, pid=os.getpid())
        check("异常退出的租约被回收",
              not recovered.stdout.strip(), recovered.stdout + recovered.stderr)

        disabled = Path.home() / ".codex" / "hooks" / "RESOURCE_ADMISSION_DISABLED"
        if not disabled.exists():
            try:
                disabled.write_text("test\n", encoding="utf-8")
                off = run_hook("npx tsc --noEmit", state, slots=1, pid=os.getpid())
                check("一键禁用后 fail-open",
                      not off.stdout.strip(), off.stdout + off.stderr)
            finally:
                disabled.unlink(missing_ok=True)

    print("\nresource admission unit: PASS")


if __name__ == "__main__":
    main()
