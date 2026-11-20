#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：关闭必须有可复核证据，回归必须重新打开，不能被自由文本糊过去。

验收闭环计划承诺「候选确认、机器门禁、人工复核、关闭与回归重开五条路径
全部可追溯」。前三条已有门禁；这条锁住剩余两条：
  1. 没有 evidence / commit 的 close 必须失败，台账仍是 open；
  2. 带命令 + cwd + 退出码的 close 才能翻 done，并写入证据/进展；
  3. commit 关单要留下可复核的 commit 引用；
  4. open 能把 done 翻回 open，保留原因和原证据；
  5. 不存在的 id 不能被关闭。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def read_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def latest(rows):
    out = {}
    for row in rows:
        if row.get("id"):
            out[row["id"]] = row
    return out


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-acceptance-close-") as tmp:
        repo = Path(tmp)
        truth = repo / "ops" / "truth"
        truth.mkdir(parents=True)
        write_jsonl(truth / "ledger.jsonl", [
            {"id": "L-0001", "type": "ledger", "status": "open",
             "title": "证据不足不许关单", "detail": "原始问题"},
            {"id": "L-0002", "type": "ledger", "status": "open",
             "title": "带 commit 关单", "detail": "原始问题"},
        ])
        write_jsonl(truth / "progress.jsonl", [])
        (truth / "mainline.json").write_text("{}\n", encoding="utf-8")

        base_env = {
            **os.environ,
            "KG_REPO": str(repo),
            "KG_TRUTH_ID_STATE_ROOT": str(repo / "truth-ids"),
            "KG_TRUTH_SOURCE": "test",
        }

        def truthctl(*args, extra=None):
            env = dict(base_env)
            if extra:
                env.update(extra)
            return subprocess.run(
                [sys.executable, str(OPS / "truthctl.py"), *args],
                cwd=str(REPO), env=env, capture_output=True, text=True)

        ledger_path = truth / "ledger.jsonl"
        progress_path = truth / "progress.jsonl"

        bad = truthctl("close", "L-0001", "口头说做完了")
        check("没有证据的 close 被拒绝", bad.returncode != 0,
              bad.stdout[-300:] + bad.stderr[-300:])
        check("拒绝关闭时台账仍是 open",
              latest(read_jsonl(ledger_path))["L-0001"]["status"] == "open")
        check("拒绝关闭时不写假进展", read_jsonl(progress_path) == [])

        evidence = "python3 ops/test-acceptance-criteria.py (cwd=.) -> exit 0"
        good = truthctl("close", "L-0001", "真门禁已跑",
                        extra={"KG_TRUTH_EVIDENCE": evidence})
        check("带真证据的 close 成功", good.returncode == 0,
              good.stdout[-300:] + good.stderr[-300:])
        row = latest(read_jsonl(ledger_path))["L-0001"]
        check("关单状态翻 done", row.get("status") == "done", row)
        check("命令 cwd 退出码原样进证据", row.get("evidence") == evidence, row)
        check("关单时间落账", bool(row.get("closed")), row)
        check("关单结论保留", "真门禁已跑" in (row.get("detail") or ""), row)
        progress = read_jsonl(progress_path)
        check("关闭同时追加进展", len(progress) == 1, progress)
        check("进展对应同一工作项", "L-0001" in (progress[0].get("title") or ""),
              progress)
        check("进展写明结论", progress[0].get("summary") == "真门禁已跑", progress)

        commit = "abcdef1234567890"
        by_commit = truthctl("close", "L-0002", "已合入 main",
                             extra={"KG_TRUTH_COMMIT": commit})
        check("带 commit 的 close 成功", by_commit.returncode == 0,
              by_commit.stdout[-300:] + by_commit.stderr[-300:])
        row2 = latest(read_jsonl(ledger_path))["L-0002"]
        check("commit 关单留下可复核引用",
              row2.get("status") == "done" and commit in (row2.get("evidence") or ""),
              row2)
        progress2 = read_jsonl(progress_path)
        check("commit 关单进展标记已落盘",
              len(progress2) == 2 and progress2[1].get("commit") == commit
              and progress2[1].get("landed") is True, progress2)

        reopen = truthctl("open", "L-0001", "回归重现，重新打开")
        check("回归重开成功", reopen.returncode == 0,
              reopen.stdout[-300:] + reopen.stderr[-300:])
        reopened = latest(read_jsonl(ledger_path))["L-0001"]
        check("重开后状态回 open", reopened.get("status") == "open", reopened)
        check("重开原因保留", "回归重现" in (reopened.get("detail") or ""), reopened)
        check("重开不抹掉原证据", reopened.get("evidence") == evidence, reopened)
        check("重开不追加虚假的关闭进展",
              len(read_jsonl(progress_path)) == 2, read_jsonl(progress_path))

        missing = truthctl("close", "L-9999", "不存在的工作项",
                           extra={"KG_TRUTH_EVIDENCE": evidence})
        check("不存在的工作项不能关闭", missing.returncode != 0,
              missing.stdout[-300:] + missing.stderr[-300:])

    print()
    if failures:
        print(f"acceptance_close_reopen 门禁：{len(failures)} 项未通过")
        for item in failures:
            print("  - " + item)
        return len(failures)
    print("acceptance_close_reopen 门禁：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
