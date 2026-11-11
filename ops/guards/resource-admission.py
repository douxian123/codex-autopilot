#!/usr/bin/env python3
"""Bound expensive verification commands without blocking normal work.

Only explicit compiler, bundler, and browser verification commands need a
lease. Existing commands are never killed. Any failure is fail-open.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
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

HEAVY_PATTERNS = (
    re.compile(r"(^|[;&|()\s])(?:npx\s+|pnpm\s+exec\s+|yarn\s+)?(?:tsc|tsgo)(?:\s|$)"),
    re.compile(r"(^|[;&|()\s])(?:npx\s+|pnpm\s+exec\s+|yarn\s+)?vite\s+(?:build|preview)(?:\s|$)"),
    re.compile(r"(^|[;&|()\s])(?:npx\s+|pnpm\s+exec\s+|yarn\s+)?playwright(?:\s|$)"),
    re.compile(r"(^|[;&|()\s])(?:npx\s+|pnpm\s+exec\s+|yarn\s+)?vitest(?:\s|$)"),
    re.compile(r"(^|[;&|()\s])(?:npx\s+|pnpm\s+exec\s+|yarn\s+)?tsx(?:\s+\S+)*\s+scripts/verify\S*"),
    re.compile(r"(^|[;&|()\s])scripts/verify\S*(?:browser|playwright)\S*"),
    re.compile(r"(^|[;&|()\s])verify-\S*(?:browser|playwright)\S*"),
)

EXEMPT_PATTERNS = (
    re.compile(r"(^|[;&|()\s])(?:ls|rg|grep|find|cat|sed|head|tail|df|du|ps|git\s+(?:status|log|diff|show|branch))(?:\s|$)"),
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


def _is_heavy(command: str) -> bool:
    if not command.strip():
        return False
    if any(pattern.search(command) for pattern in EXEMPT_PATTERNS):
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
    return _pid_alive(int(owner.get("pid") or 0))


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


def _cwd_from_event(event: dict[str, Any]) -> str:
    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return os.getcwd()


def _owner_from_event(event: dict[str, Any]) -> dict[str, Any]:
    pid = int(os.environ.get(TEST_PID_ENV, "0") or 0)
    pgid = 0
    if pid > 1:
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            pgid = 0
    return {
        "pid": pid,
        "pgid": pgid,
        "session_id": str(event.get("session_id") or "unknown"),
        "cwd": _cwd_from_event(event),
    }


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


def main() -> int:
    if DISABLED_FILE.exists():
        return 0
    if os.environ.get("CODEX_RESOURCE_ADMISSION", "on").lower() == "off":
        return 0
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict) or event.get("tool_name") not in {"exec_command", "Bash"}:
            return 0
        command = _command_text(event)
        if not _is_heavy(command):
            return 0
        owner = _owner_from_event(event)
        free_gib = _free_gib()
        with _locked_state() as state:
            _prune(state)
            if len(state["leases"]) >= MAX_LEASES or free_gib < MIN_FREE_GIB:
                _emit_deny(_deny_reason(state, free_gib))
                return 0
            if owner["pid"] > 1:
                owner["created_at"] = time.time()
                state["leases"].append(owner)
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
