#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small local control API for the macOS Companion.

The Companion is deliberately a thin client. All mutations still go through
the existing control state and kgctl entry points.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import control_state  # noqa: E402


def _repo():
    return Path(os.environ.get("KG_REPO") or OPS.parent).expanduser().resolve()


def _result(payload, code=0):
    print(json.dumps(payload, ensure_ascii=False))
    return code


def main(argv):
    action = argv[0] if argv else "status"
    if action == "status":
        cfg = control_state.load()
        return _result({
            "enabled": bool(cfg.get("enabled", True)),
            "lifecycle": control_state.lifecycle_status(cfg),
            "paused": bool(cfg.get("paused")),
            "switches": {
                key: bool(cfg.get(key))
                for key in ("auto_land", "auto_resolve", "auto_port",
                            "auto_exec", "daemon_enabled")
            },
            "error": cfg.get("error"),
        })
    if action in ("enable", "disable"):
        enabled = action == "enable"
        cfg = control_state.set_keys(enabled=enabled)
        control_state.append_event("companion", action=action)
        return _result({
            "enabled": bool(cfg.get("enabled")),
            "lifecycle": control_state.lifecycle_status(cfg),
        })
    if action == "intake":
        text = " ".join(argv[1:]).strip()
        if not text:
            return _result({"code": "INTAKE_TEXT_REQUIRED", "message": "需要输入需求"}, 2)
        guard = control_state.mutation_guard()
        if guard:
            return _result(guard, 2)
        env = os.environ.copy()
        env["KG_REPO"] = str(_repo())
        proc = subprocess.run(
            [sys.executable, str(OPS / "kgctl.py"), "intake", text],
            cwd=str(OPS.parent), env=env, capture_output=True, text=True,
        )
        return _result({
            "ok": proc.returncode == 0,
            "output": (proc.stdout + proc.stderr).strip(),
            "code": proc.returncode,
        }, proc.returncode)
    return _result({"code": "UNKNOWN_ACTION", "message": action}, 2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
