#!/usr/bin/env python3
"""Regression tests for the narrow resource-admission hook."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "ops" / "guards" / "resource-admission.py"
HOOKS = ROOT / "ops" / "guards" / "resource-hooks.json"


def run_hook(
    event_name: str,
    command: str,
    state_dir: Path,
    call_id: str,
    slots: int = 2,
    pid: int = 0,
):
    env = os.environ.copy()
    env["CODEX_RESOURCE_ADMISSION_STATE_DIR"] = str(state_dir)
    env["CODEX_RESOURCE_ADMISSION_SLOTS"] = str(slots)
    env["CODEX_RESOURCE_ADMISSION_MIN_FREE_GIB"] = "0"
    env["CODEX_RESOURCE_ADMISSION_MIN_FREE_MEM_GIB"] = "0"
    env["CODEX_RESOURCE_ADMISSION_MAX_COMPRESSOR_GIB"] = "999"
    env["CODEX_RESOURCE_ADMISSION_MAX_SWAP_USED_GIB"] = "999"
    if pid > 1:
        env["CODEX_RESOURCE_ADMISSION_PID"] = str(pid)
    else:
        env.pop("CODEX_RESOURCE_ADMISSION_PID", None)
    event = {
        "hook_event_name": event_name,
        "tool_name": "exec_command",
        "tool_input": {"command": command},
        "tool_use_id": call_id,
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


def leases(state: Path) -> list[dict]:
    path = state / "leases.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("leases", [])


def main() -> None:
    check("资源准入 hook 独立存在", GUARD.is_file())
    check("hooks 片段存在", HOOKS.is_file())
    hook_config = json.loads(HOOKS.read_text(encoding="utf-8"))
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        groups = hook_config["hooks"][event]
        check(f"{event} matcher 覆盖 exec_command",
              any("exec_command" in str(group.get("matcher", "")) for group in groups))
        check(f"{event} 指向资源准入脚本",
              any("resource-admission.py" in hook["command"]
                  for group in groups for hook in group.get("hooks", [])))

    with tempfile.TemporaryDirectory(prefix="verification-lock-test-") as raw:
        root = Path(raw)
        state = root / "state"
        trunk = root / "trunk"
        (state / "ops" / "tasks").mkdir(parents=True)
        (state / "ops" / "truth").mkdir(parents=True)
        (state / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for name in ("ledger", "progress", "decisions", "acceptance"):
            (state / "ops" / "truth" / f"{name}.jsonl").write_text("", encoding="utf-8")
        (state / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
        marker = root / "entered"
        probe = root / "probe.py"
        probe.write_text(
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text(str(time.time()))\n"
            "time.sleep(1.5)\n",
            encoding="utf-8",
        )
        command = f"{sys.executable} {probe} {marker}"
        config = {
            "build": {"name": "probe", "cmd": command, "cwd": "."},
            "verify": [{"name": "probe", "cmd": command, "cwd": "."}],
            "shared_dir": str(state),
        }
        (state / "ops" / "central.config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(state)], check=True
        )
        subprocess.run(
            ["git", "-C", str(state), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(state), "config", "user.name", "Test"],
            check=True,
        )
        (state / "README").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(state), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(state), "commit", "-qm", "fixture"], check=True
        )
        trunk.mkdir()
        env = os.environ.copy()
        env["KG_REPO"] = str(state)
        env["KG_MAIN"] = "main"
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(ROOT / 'ops')!r}); "
            "import kgctl; "
            f"ok, results, out = kgctl._run_verify_gates(kgctl.Path({str(state)!r})); "
            "print(ok, results, out)"
        )
        first = subprocess.Popen(
            [sys.executable, "-c", code], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 4
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.02)
        check("第一路已进入重门禁", marker.exists())
        started = time.time()
        second = subprocess.run(
            [sys.executable, "-c", code], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=12,
        )
        first_out, first_err = first.communicate(timeout=12)
        check("第一路门禁成功", first.returncode == 0 and "True" in first_out,
              first_out + first_err)
        check("第二路等待后成功", second.returncode == 0 and "True" in second.stdout,
              second.stdout + second.stderr)
        check("两路未同时执行", time.time() - started >= 1.2,
              f"elapsed={time.time() - started:.2f}s")

    with tempfile.TemporaryDirectory(prefix="resource-admission-test-") as raw:
        state = Path(raw)
        (Path.home() / ".codex" / "hooks" / "RESOURCE_ADMISSION_DISABLED").unlink(missing_ok=True)
        for command in (
            "rg resource-admission ops",
            "ls -la",
            "git status --short",
            "rg 'tsc --noEmit' docs",
            "python3 -m py_compile ops/guards/resource-admission.py",
            "python3 ops/test-resource-admission.py",
        ):
            result = run_hook("PreToolUse", command, state, f"read-{command}")
            check(f"只读命令不占槽: {command}",
                  result.returncode == 0 and not result.stdout.strip(), result.stderr)
        check("只读命令后没有租约", leases(state) == [])

        first = run_hook("PreToolUse", "npx tsc --noEmit", state, "call-1", slots=2)
        check("第一个重验证放行", not first.stdout.strip(), first.stdout + first.stderr)
        second = run_hook("PreToolUse", "npx playwright test", state, "call-2", slots=2)
        check("第二个重验证放行", not second.stdout.strip(), second.stdout + second.stderr)
        third = run_hook("PreToolUse", "npm exec vite build", state, "call-3", slots=2)
        check("超出租约时明确拒绝",
              third.returncode == 0 and "资源准入" in third.stdout,
              third.stdout + third.stderr)
        check("拒绝不占槽",
              [item["id"] for item in leases(state)] == ["test-session:call-1", "test-session:call-2"])

        released = run_hook("PostToolUse", "npx tsc --noEmit", state, "call-1", slots=2)
        check("PostToolUse 释放租约", not released.stdout.strip(), released.stderr)
        after_release = run_hook("PreToolUse", "npm exec vite build", state, "call-4", slots=2)
        check("释放后下一次放行", not after_release.stdout.strip(), after_release.stdout + after_release.stderr)

        pressure_probe = state / "vm_stat"
        pressure_probe.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 'Mach Virtual Memory Statistics: (page size of 16384 bytes)'\n"
            "printf '%s\\n' 'Pages occupied by compressor: 393216.'\n",
            encoding="utf-8",
        )
        pressure_probe.chmod(0o755)
        pressure_env = os.environ.copy()
        pressure_env.update({
            "CODEX_RESOURCE_ADMISSION_STATE_DIR": str(state),
            "CODEX_RESOURCE_ADMISSION_MIN_FREE_GIB": "0",
            "CODEX_RESOURCE_ADMISSION_MIN_FREE_MEM_GIB": "0",
            "CODEX_RESOURCE_ADMISSION_MAX_COMPRESSOR_GIB": "4",
            "CODEX_RESOURCE_ADMISSION_MAX_SWAP_USED_GIB": "999",
            "CODEX_RESOURCE_ADMISSION_SLOTS": "2",
            "CODEX_RESOURCE_ADMISSION_MEMORY_PROBE": str(pressure_probe),
        })
        pressure_event = {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec_command",
            "tool_input": {"command": "npx tsc --noEmit"},
            "tool_use_id": "call-pressure",
            "session_id": "test-session",
            "cwd": str(ROOT),
        }
        # Exercise memory pressure independently, then restore the two leases
        # used by the following failure-release assertion.
        leases_path = state / "leases.json"
        raw_state = json.loads(leases_path.read_text(encoding="utf-8"))
        raw_state["leases"] = []
        leases_path.write_text(json.dumps(raw_state), encoding="utf-8")
        pressure_result = subprocess.run(
            [sys.executable, str(GUARD)],
            input=json.dumps(pressure_event),
            text=True,
            capture_output=True,
            env=pressure_env,
            check=False,
        )
        check("高内存压缩时延后重验证",
              "内存压缩" in pressure_result.stdout,
              pressure_result.stdout + pressure_result.stderr)
        check("高内存压力拒绝不占槽",
              all(item["id"] != "test-session:call-pressure" for item in leases(state)))
        leases_path.write_text(
            json.dumps({"leases": [
                {"id": "test-session:call-1", "created_at": time.time(), "pid": 0},
                {"id": "test-session:call-2", "created_at": time.time(), "pid": 0},
            ]}),
            encoding="utf-8",
        )

        failed = run_hook("PostToolUseFailure", "npx playwright test", state, "call-2", slots=2)
        check("失败结束也释放租约", not failed.stdout.strip(), failed.stderr)
        check("失败释放后只保留新租约",
              [item["id"] for item in leases(state)] == ["test-session:call-1"])

        raw_state = json.loads(leases_path.read_text(encoding="utf-8"))
        raw_state["leases"][0]["created_at"] = time.time() - 7 * 60 * 60
        leases_path.write_text(json.dumps(raw_state), encoding="utf-8")
        recovered = run_hook("PreToolUse", "npx tsc --noEmit", state, "call-5", slots=2)
        check("异常退出的租约可过期回收",
              not recovered.stdout.strip(), recovered.stdout + recovered.stderr)

        no_call_id = {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec_command",
            "tool_input": {"command": "npx tsc --noEmit"},
            "session_id": "test-session",
            "cwd": str(ROOT),
        }
        env = os.environ.copy()
        env["CODEX_RESOURCE_ADMISSION_STATE_DIR"] = str(state)
        env["CODEX_RESOURCE_ADMISSION_MIN_FREE_GIB"] = "0"
        result = subprocess.run(
            [sys.executable, str(GUARD)],
            input=json.dumps(no_call_id),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        check("没有调用 ID 时 fail-open", not result.stdout.strip(), result.stderr)

        disabled = Path.home() / ".codex" / "hooks" / "RESOURCE_ADMISSION_DISABLED"
        if not disabled.exists():
            try:
                disabled.write_text("test\n", encoding="utf-8")
                off = run_hook("PreToolUse", "npx tsc --noEmit", state, "call-off", slots=1)
                check("一键禁用后 fail-open",
                      not off.stdout.strip(), off.stdout + off.stderr)
            finally:
                disabled.unlink(missing_ok=True)

    print("\nresource admission unit: PASS")


if __name__ == "__main__":
    main()
