#!/usr/bin/env python3
"""Install resource admission for pre- and post-tool lifecycle events."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE_HOOK = ROOT / "ops" / "guards" / "resource-admission.py"
SOURCE_FRAGMENT = ROOT / "ops" / "guards" / "resource-hooks.json"
EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")


def _is_our_entry(entry: Any) -> bool:
    return isinstance(entry, dict) and any(
        "resource-admission.py" in str(hook.get("command", ""))
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    )


def _installed_groups(home: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    raw = SOURCE_FRAGMENT.read_text(encoding="utf-8")
    # 模板里的 HOME 必须在安装时展开到 ~/.codex；展开成 ~/ 会让 hook 指向
    # ~/hooks/resource-admission.py，而文件实际在 ~/.codex/hooks/ 下。
    codex_home = (home or Path.home()) / ".codex"
    raw = raw.replace("__CODEX_HOOK_HOME__", str(codex_home))
    fragment = json.loads(raw)
    hooks = fragment.get("hooks")
    if not isinstance(hooks, dict):
        raise SystemExit("resource hook fragment is missing hooks")
    result: dict[str, list[dict[str, Any]]] = {}
    for event in EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list) or len(groups) != 1:
            raise SystemExit(f"resource hook fragment must contain one {event} group")
        result[event] = groups
    return result


def _load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {"hooks": {}}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"hooks.json is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise SystemExit("hooks.json is not a JSON object")
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit("hooks.json hooks field is not an object")
    return config


def main() -> int:
    if not SOURCE_HOOK.is_file() or not SOURCE_FRAGMENT.is_file():
        raise SystemExit("resource-admission source files are missing")
    home = Path.home()
    codex_home = home / ".codex"
    hooks_json = codex_home / "hooks.json"
    target_hook = codex_home / "hooks" / "resource-admission.py"
    source_groups = _installed_groups(home)
    config = _load_config(hooks_json)
    hooks = config["hooks"]

    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SystemExit(f"hooks.json {event} field is not an array")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    if hooks_json.exists():
        shutil.copy2(hooks_json, hooks_json.with_name(f"hooks.json.bak-{stamp}-resource-admission"))

    for event in EVENTS:
        groups = hooks[event]
        groups[:] = [entry for entry in groups if not _is_our_entry(entry)]
        groups.extend(source_groups[event])

    target_hook.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE_HOOK, target_hook)
    target_hook.chmod(0o755)

    temp = hooks_json.with_name(f".{hooks_json.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, hooks_json)
    print(f"installed resource admission hooks -> {target_hook}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
