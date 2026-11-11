#!/usr/bin/env python3
"""Bound expensive verification commands without blocking normal work.

PreToolUse takes a lease keyed by the harness call id. PostToolUse and
PostToolUseFailure release it. Existing commands are never killed. Every
unexpected condition fails open so this guard cannot block normal work.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator


STATE_DIR = Path(
    os.environ.get(
        "CODEX_RESOURCE_ADMISSION_STATE_DIR",
        str(Path.home() / ".codex" / "hooks" / "resource-admission"),
    )
)
DISABLED_FILE = Path.home() / ".codex" / "hooks" / "RESOURCE_ADMISSION_DISABLED"
MAX_LEASES = max(1, int(os.environ.get("CODEX_RESOURCE_ADMISSION_SLOTS", "2")))
MIN_FREE_GIB = float(os.environ.get("CODEX_RESOURCE_ADMISSION_MIN_FREE_GIB", "8"))
LEASE_MAX_AGE_SECONDS = int(
    os.environ.get("CODEX_RESOURCE_ADMISSION_MAX_AGE_SECONDS", str(6 * 60 * 60))
)
TEST_PID_ENV = "CODEX_RESOURCE_ADMISSION_PID"
PRE_EVENTS = {"PreToolUse"}
POST_EVENTS = {"PostToolUse", "PostToolUseFailure"}

COMMAND_SEPARATORS = r"(?:^|[;&|()\n])\s*"
HEAVY_PATTERNS = (
    re.compile(COMMAND_SEPARATORS + r"(?:npx|npm\s+exec|pnpm\s+exec|yarn\s+exec)\s+(?:--\s+)?(?:tsc|tsgo)(?:\s|$)"),
    re.compile(COMMAND_SEPARATORS + r"(?:npx|npm\s+exec|pnpm\s+exec|yarn\s+exec)\s+(?:--\s+)?vite\s+(?:build|preview)(?:\s|$)"),
    re.compile(COMMAND_SEPARATORS + r"(?:npx|npm\s+exec|pnpm\s+exec|yarn\s+exec)\s+(?:--\s+)?playwright(?:\s|$)"),
    re.compile(COMMAND_SEPARATORS + r"(?:npx|npm\s+exec|pnpm\s+exec|yarn\s+exec)\s+(?:--\s+)?vitest(?:\s|$)"),
    re.compile(COMMAND_SEPARATORS + r"(?:\./)?node_modules/\.bin/(?:tsc|tsgo|vite|playwright|vitest)(?:\s|$)"),
    re.compile(COMMAND_SEPARATORS + r"(?:tsc|tsgo)(?:\s|$)"),
    re.compile(COMMAND_SEPARATORS + r"vite\s+(?:build|preview)(?:\s|$)"),
    re.compile(COMMAND_SEPARATORS + r"(?:playwright|vitest)(?:\s|$)"),
    re.compile(COMMAND_SEPARATORS + r"(?:npx|npm\s+exec|pnpm\s+exec|yarn\s+exec)\s+(?:--\s+)?tsx\S*\s+.*scripts/verify\S*"),
    re.compile(COMMAND_SEPARATORS + r"(?:npx|npm\s+exec|pnpm\s+exec|yarn\s+exec)\s+(?:--\s+)?tsx\S*\s+.*verify-\S*(?:browser|playwright)\S*"),
    re.compile(COMMAND_SEPARATORS + r"[^\n]*(?:scripts/verify\S*(?:browser|playwright)\S*|verify-\S*(?:browser|playwright)\S*)"),
)


def _emit_deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )


def _command_text(event: dict[str, Any]) -> str:
    tool_input = event.get("tool_input")
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command")
    return command if isinstance(command, str) else ""


def _tool_name(event: dict[str, Any]) -> str:
    value = event.get("tool_name")
    return value if isinstance(value, str) else ""


def _is_heavy(command: str) -> bool:
    if not command.strip():
        return False
    return any(pattern.search(command) for pattern in HEAVY_PATTERNS)


def _free_gib(path: Path = Path.home()) -> float:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize / (1024**3)


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _owner_alive(owner: dict[str, Any]) -> bool:
    pgid = int(owner.get("pgid") or 0)
    if pgid > 1:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    pid = int(owner.get("pid") or 0)
    if pid > 1:
        return _pid_alive(pid)
    return True


def _state_path() -> Path:
    return STATE_DIR / "leases.json"


@contextlib.contextmanager
def _locked_state() -> Iterator[dict[str, Any]]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / "leases.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        path = _state_path()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        leases = state.get("leases")
        if not isinstance(leases, list):
            leases = []
        state["leases"] = [lease for lease in leases if isinstance(lease, dict)]
        try:
            yield state
        finally:
            temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(temp, path)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _prune(state: dict[str, Any]) -> None:
    now = time.time()
    state["leases"] = [
        lease
        for lease in state["leases"]
        if now - float(lease.get("created_at") or 0) <= LEASE_MAX_AGE_SECONDS
        and _owner_alive(lease)
    ]


def _event_name(event: dict[str, Any]) -> str:
    return str(event.get("hook_event_name") or "")


def _call_id(event: dict[str, Any]) -> str:
    for key in ("tool_use_id", "toolUseId", "call_id", "callId"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("tool_use_id", "toolUseId", "call_id", "callId"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _lease_id(event: dict[str, Any]) -> str:
    call_id = _call_id(event)
    session_id = str(event.get("session_id") or "unknown")
    return f"{session_id}:{call_id}" if call_id else ""


def _deny_reason(state: dict[str, Any], free_gib: float) -> str:
    if free_gib < MIN_FREE_GIB:
        return (
            f"[资源准入] 目前只剩 {free_gib:.1f} GiB，新的重验证已延后。"
            "已有命令不会被终止；请先释放空间，或完成当前工作后再运行验证。"
        )
    return (
        f"[资源准入] 已有 {len(state['leases'])}/{MAX_LEASES} 个重验证在运行，"
        "为避免再次占满内存和磁盘，新的验证已延后。"
        "已有命令不会被终止；等待其中一个退出后重试。"
    )


def _release(state: dict[str, Any], lease_id: str) -> None:
    if not lease_id:
        return
    state["leases"] = [lease for lease in state["leases"] if lease.get("id") != lease_id]


def _admit_pre(event: dict[str, Any]) -> None:
    command = _command_text(event)
    if not _is_heavy(command):
        return
    lease_id = _lease_id(event)
    if not lease_id:
        return
    free_gib = _free_gib()
    with _locked_state() as state:
        _prune(state)
        if any(lease.get("id") == lease_id for lease in state["leases"]):
            return
        if len(state["leases"]) >= MAX_LEASES or free_gib < MIN_FREE_GIB:
            _emit_deny(_deny_reason(state, free_gib))
            return
        test_pid = int(os.environ.get(TEST_PID_ENV, "0") or 0)
        state["leases"].append(
            {
                "id": lease_id,
                "session_id": str(event.get("session_id") or "unknown"),
                "created_at": time.time(),
                "cwd": str(event.get("cwd") or os.getcwd()),
                "pid": test_pid if test_pid > 1 else 0,
                "pgid": 0,
            }
        )


def _release_post(event: dict[str, Any]) -> None:
    lease_id = _lease_id(event)
    if not lease_id:
        return
    with _locked_state() as state:
        _release(state, lease_id)


def main() -> int:
    if DISABLED_FILE.exists():
        return 0
    if os.environ.get("CODEX_RESOURCE_ADMISSION", "on").lower() == "off":
        return 0
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict) or event.get("tool_name") not in {"exec_command", "Bash"}:
            return 0
        name = _event_name(event)
        if name in PRE_EVENTS:
            _admit_pre(event)
        elif name in POST_EVENTS:
            _release_post(event)
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
