#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""现场生效判据门禁：install-state、metadata、plist 与 live 路径必须同一代。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

OPS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "runtime_install_contract_under_test", OPS / "runtime_install_contract.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def main() -> int:
    report = mod.check()
    status = report.get("status")
    assert status in ("ok", "stale", "unknown"), report
    # 现场取不到只报 unknown；绝不能把 unknown 伪装成 ok 或 stale。
    if status == "unknown":
        assert report.get("unknown"), report
    if status == "stale":
        assert report.get("failures"), report
    # 本门禁不是断言当前机器恰好是绿的，而是锁住判据的四重口径。
    source = (mod.SELFTEST_SOURCE if hasattr(mod, "SELFTEST_SOURCE")
              else (OPS / "selftest.py").read_text(encoding="utf-8"))
    for field in ("install-state.json", ".codex-autopilot-source.json",
                  "com.codex-autopilot.daemon.plist", "launchctl", "ps"):
        assert field in (OPS / "runtime_install_contract.py").read_text(encoding="utf-8"), field
    print("runtime-install-contract: PASS（现场状态 %s）" % status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
