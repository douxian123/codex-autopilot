#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：模糊预期先编译成可判定验收标准候选，确认前不进验收台账。

此前验收链只有 `acceptance.jsonl` 登记和「先自验再请人看」闸门，缺了前半截：
用户说「做完要能看到 X / 不卡住 / 跑通门禁」，系统不会把它变成可判定标准。
本门禁锁住：
  1. 已有明确验收信号的已路由原话会提名候选，并附上人话复核步骤；
  2. 描述现状的句子不误收，未路由原话不参与；
  3. 提名阶段不写验收台账；重复原话和 dismiss 过的原话不再弹；
  4. 没有 confirm 不能 promote；确认后才生成 A-id 验收项，且不写假证据；
  5. PM 简报能只读聚合候选，保持 unknown/0 语义不混。

退出码 = 未通过项数。
"""

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def load(name, filename, env):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(name, OPS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


INBOX = [
    {"ts": "2026-11-18 11:00:00", "text": "改成新面板后必须能看到验收截图，点了不卡住",
     "files": ["plugin/ui/panel.html"], "kind": "visual", "routed": True, "lane": "design"},
    {"ts": "2026-11-18 11:01:00", "text": "这项落地前必须真跑门禁，测试通过才算过",
     "files": ["ops/kgctl.py"], "kind": "gate", "routed": True, "lane": "gate"},
    {"ts": "2026-11-18 11:02:00", "text": "实测曾达 99%，复现报错原文如下",
     "files": [], "kind": "bug", "routed": True, "lane": "bug"},
    {"ts": "2026-11-18 11:03:00", "text": "页面点一下要能看到结果，这条还没路由",
     "files": [], "kind": "ux", "routed": False, "lane": None},
]


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-acceptance-criteria-") as tmp:
        repo = Path(tmp) / "repo"
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in INBOX) + "\n",
            encoding="utf-8")
        for name in ("constraints.jsonl", "mainline.json"):
            (repo / "ops" / "truth" / name).write_text("[]\n", encoding="utf-8")
        (repo / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")

        m = load("acceptance_criteria_t", "acceptance_criteria.py", {"KG_REPO": str(repo)})
        acceptance = repo / "ops" / "truth" / "acceptance.jsonl"
        res = m.propose()
        created = res.get("created") or []
        check("明确预期被提名为候选", len(created) == 2, created)
        check("候选保留原话",
              any("验收截图" in c.get("text", "") for c in created)
              and any("测试通过" in c.get("text", "") for c in created), created)
        check("人工项建议人看步骤",
              any(c.get("needs_human") and "原话" in c.get("steps", "") for c in created), created)
        check("门禁项建议机器自证",
              any(c.get("machine_gate_suggested") and not c.get("needs_human")
                  for c in created), created)
        check("描述现状的句子没有被误收",
              all("实测曾达" not in c.get("text", "") for c in created))
        check("还没路由的条目不参与",
              all("还没路由" not in c.get("text", "") for c in created))
        check("提名阶段不写验收台账", not acceptance.exists(), str(acceptance))

        again = m.propose()
        check("同一句话不重复提名", again.get("created") == [], again.get("created"))

        cid = created[0]["id"]
        denied = m.promote(cid, confirm=False)
        check("没有 confirm 不能 promote", denied.get("status") == "error", denied)
        check("拒绝 promote 后仍不写验收台账", not acceptance.exists())

        promoted = m.promote(cid, confirm=True)
        check("确认后才登记验收项", promoted.get("status") == "promoted", promoted)
        check("promote 带回 A-id",
              bool((promoted.get("candidate") or {}).get("acceptance_id")), promoted)
        rows = [json.loads(line) for line in acceptance.read_text(encoding="utf-8").splitlines()
                if line.strip()] if acceptance.exists() else []
        check("验收台账里登记了这条标准", len(rows) == 1, rows)
        if rows:
            check("新验收项没有伪造证据", not str(rows[0].get("evidence") or "").strip(), rows[0])
            check("人工项有复核步骤", bool(str(rows[0].get("steps") or "").strip()), rows[0])
            check("人工项不会自动进需要你验收", rows[0].get("needs_human") is True, rows[0])
            check("验收项带回原话 hash（长句去重不靠截断标题）",
                  rows[0].get("source_hash") == created[0].get("source_hash"), rows[0])

        second = created[1]["id"]
        dismissed = m.dismiss(second, "这条不是验收标准")
        check("dismiss 成功", dismissed.get("status") == "dismissed", dismissed)
        check("dismiss 后候选不再待确认",
              all(r["id"] != second for r in m.pending()), m.pending())
        m.propose()
        check("dismiss 过的同句话不再重新提名",
              all(r["id"] != second for r in m.pending()), m.pending())

        # 候选文件被清掉也不能让已确认的同一句原话再次弹出；验收台账本身就是真相。
        cand_path = repo / "ops" / "truth" / "acceptance_criteria_candidates.jsonl"
        kept = [r for r in
                (json.loads(line) for line in cand_path.read_text(encoding="utf-8").splitlines()
                 if line.strip())
                if r.get("id") != cid]
        cand_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
            encoding="utf-8")
        resurrected = m.propose().get("created") or []
        check("候选文件丢失后已确认原话不复发",
              all("验收截图" not in r.get("text", "") for r in resurrected), resurrected)

        pm = load("pm_briefing_t", "pm_briefing.py", {"KG_REPO": str(repo)})
        briefing = pm.compute(repo, now=datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc))
        acc = briefing.get("acceptance_criteria") or {}
        check("PM 简报聚合验收标准候选", acc.get("status") == "ok", acc)
        check("PM 简报候选计数只读", acc.get("pending_count") == 0, acc)

    print()
    if failures:
        print(f"acceptance_criteria 门禁：{len(failures)} 项未通过")
        for f in failures:
            print("  - " + f)
        return len(failures)
    print("acceptance_criteria 门禁：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
