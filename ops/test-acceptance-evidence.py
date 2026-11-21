#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据采集器回归：只绑定显式标准，并保留 provenance。"""

import json
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import acceptance_evidence
import pre_acceptance


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  PASS", label)


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-evidence-") as tmp:
        root = Path(tmp)
        (root / ".git").mkdir()
        result = acceptance_evidence.run_check(
            root, ["FE-012"], [sys.executable, "-c", "print('real gate')"])
        row = result["items"][0]
        check("真实命令通过才记录 pass", row["status"] == "pass")
        check("记录当前 cwd", row["cwd"] == str(root.resolve()))
        check("记录退出码", row["exit_code"] == 0)
        check("记录标准编号", row["item_id"] == "FE-012")
        check("不会伪造截图和浏览器证据", row["evidence_types"] == ["command", "result", "version"])
        check("无 git commit 时不伪造版本", row["version"] == "")
        check("没有版本不能被判定为 pass", pre_acceptance.evaluate(root)["status"] == "unknown")
        failed = acceptance_evidence.run_check(
            root, ["FE-012"], [sys.executable, "-c", "raise SystemExit(7)"])
        check("失败命令追加 fail", failed["items"][0]["status"] == "fail")
        rows = [json.loads(x) for x in (root / "ops" / "truth" / "pre_acceptance.jsonl").read_text().splitlines()]
        check("失败证据没有覆盖历史", len(rows) == 2)
        check("未知状态仍阻断用户", pre_acceptance.evaluate(root)["notify_user"] is False)
    print("acceptance-evidence: PASS")


if __name__ == "__main__":
    main()
