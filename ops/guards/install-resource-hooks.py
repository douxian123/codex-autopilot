#!/usr/bin/env python3
"""Install the resource-admission hook as an append-only hooks.json entry."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE_HOOK = ROOT / "ops" / "guards" / "resource-admission.py"
SOURCE_FRAGMENT = ROOT / "ops" / "guards" / "resource-hooks.json"
HOOKS_JSON = Path.home() / ".codex" / "hooks.json"
TARGET_HOOK = Path.home() / ".codex" / "hooks" / "resource-admission.py"


def _command_entry() -> dict:
    fragment = json.loads(SOURCE_FRAGMENT.read_text(encoding="utf-8"))
    return fragment["hooks"]["PreToolUse"][0]


def _is_our_entry(entry: dict) -> bool:
    return any(
        "resource-admission.py" in str(hook.get("command", ""))
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    )


def main() -> int:
    if not SOURCE_HOOK.is_file() or not SOURCE_FRAGMENT.is_file():
        raise SystemExit("resource-admission source files are missing")
    HOOKS_JSON.parent.mkdir(parents=True, exist_ok=True)
    TARGET_HOOK.parent.mkdir(parents=True, exist_ok=True)
    if TARGET_HOOK.exists():
        TARGET_HOOK.chmod(0o755)
    shutil.copy2(SOURCE_HOOK, TARGET_HOOK)
    TARGET_HOOK.chmod(0o755)

    try:
        config = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {"hooks": {}}
    if not isinstance(config, dict):
        raise SystemExit("hooks.json is not a JSON object")
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit("hooks.json hooks field is not an object")
    groups = hooks.setdefault("PreToolUse", [])
    if not isinstance(groups, list):
        raise SystemExit("hooks.json PreToolUse field is not an array")
    groups[:] = [entry for entry in groups if not isinstance(entry, dict) or not _is_our_entry(entry)]
    groups.append(_command_entry())

    stamp = time.strftime("%Y%m%d-%H%M%S")
    if HOOKS_JSON.exists():
        shutil.copy2(HOOKS_JSON, HOOKS_JSON.with_name(f"hooks.json.bak-{stamp}-resource-admission"))
    temp = HOOKS_JSON.with_name(f".{HOOKS_JSON.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, HOOKS_JSON)
    print(f"installed resource admission hook -> {TARGET_HOOK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
