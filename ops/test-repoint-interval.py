#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: install-time repoint must preserve registry-controlled intervals.

Changing only ProgramArguments left the disk reaper at its old hourly cadence
after every reinstall.  The registry interval is now part of the desired state,
so a stale plist is repaired and backed up before reload.
"""
import importlib.util
import json
import os
import plistlib
import tempfile
from pathlib import Path
from unittest import mock

OPS = Path(__file__).resolve().parent
HELPER = OPS / "guards/repoint_launchd.py"
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load_helper():
    spec = importlib.util.spec_from_file_location("repoint_interval_test", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = load_helper()
    with tempfile.TemporaryDirectory(prefix="repoint-interval-") as tmp:
        home = Path(tmp)
        release = home / "plugins/codex-autopilot/releases/9.9.9"
        script = release / "ops/disk_reap.py"
        script.parent.mkdir(parents=True)
        script.write_text("# reaper\n", encoding="utf-8")
        (home / "plugins/codex-autopilot/install-state.json").write_text(
            json.dumps({"source": "./plugins/codex-autopilot/releases/9.9.9"}),
            encoding="utf-8",
        )
        registry = home / "registry.json"
        registry.write_text(json.dumps({
            "entries": [{
                "label": "com.danta.codex-disk-reap",
                "script": "ops/disk_reap.py",
                "interval_s": 120,
                "class": "repoint",
            }],
        }), encoding="utf-8")
        plist_path = home / "Library/LaunchAgents/com.danta.codex-disk-reap.plist"
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(plistlib.dumps({
            "Label": "com.danta.codex-disk-reap",
            "ProgramArguments": ["/usr/bin/python3", str(script)],
            "StartInterval": 3600,
        }, sort_keys=False))

        env = {
            "KG_GUARD_HOME": str(home),
            "KG_GUARD_REGISTRY": str(registry),
        }
        with mock.patch.dict(os.environ, env):
            row = mod.plan(home)[0]
            check("路径正确但间隔过旧仍判 stale", row["state"] == "stale", row)
            check("registry 的 120 秒成为目标状态",
                  row["desired_interval"] == 120, row)
            with mock.patch.object(mod, "_reload", return_value=(True, "")):
                rc = mod.install(home, dry_run=False)
        check("安装期改指成功", rc == 0, rc)
        data = plistlib.loads(plist_path.read_bytes())
        check("实际 plist 已改成 120 秒",
              data.get("StartInterval") == 120, data.get("StartInterval"))
        backups = list(plist_path.parent.glob(plist_path.name + ".bak-*"))
        check("改写前保留了 plist 备份", len(backups) == 1, backups)

    if failures:
        print(f"\nrepoint interval unit: FAIL ({len(failures)})")
        raise SystemExit(1)
    print("\nrepoint interval unit: PASS")


if __name__ == "__main__":
    main()
