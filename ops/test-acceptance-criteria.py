#!/usr/bin/env python3
# -*- coding: utf-8 -*-
<<<<<<< HEAD
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
=======
"""回归：模糊需求必须在路由时编译成可判定验收标准，并被执行/面板消费。

它锁住四件事：
  1) 编译器是确定性的，机器门禁落到 name + cmd + cwd + exit 0；
  2) 视觉/交互需求必须带人工复核的通过条件，不能只写「看一眼」；
  3) truthctl route --go 把标准写进 ledger，归并时不覆盖已有标准；
  4) 执行 prompt 与面板工单表都只读这份标准，不各自重新解释「做完」。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent
sys.path.insert(0, str(OPS))

import acceptance_criteria as ac  # noqa: E402

FAILS = []


def check(label, ok, detail=""):
    if ok:
        print(f"  ok   {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def make_state_root(base):
    root = base / "project"
    (root / "ops" / "tasks").mkdir(parents=True)
    (root / "ops" / "truth").mkdir(parents=True)
    for rel in ("ops/tasks/inbox.jsonl", "ops/truth/ledger.jsonl",
                "ops/truth/progress.jsonl", "ops/truth/decisions.jsonl",
                "ops/truth/acceptance.jsonl"):
        (root / rel).write_text("", encoding="utf-8")
    (root / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
    return root


def main():
    # ---- 1) 纯函数编译：命令可跑、cwd 明确、通过条件是 exit 0 ----
    cfg = {"verify": [{"name": "unit", "cmd": "python3 -m unittest discover", "cwd": "."}]}
    criteria = ac.compile_criteria(
        text="补一个门禁", kind="gate", files=["ops/acceptance_criteria.py"],
        lane="gate", cfg=cfg)
    machine = criteria["machine_checks"]
    check("编译状态是 ok", criteria["status"] == "ok", json.dumps(criteria, ensure_ascii=False))
    check("机器门禁来自项目配置",
          any(g.get("name") == "unit" and g.get("cmd") == "python3 -m unittest discover"
              for g in machine), json.dumps(machine, ensure_ascii=False))
    check("Python 文件带静态编译门禁",
          any(g.get("name") == "py-compile" and "py_compile" in g.get("cmd", "")
              for g in machine), json.dumps(machine, ensure_ascii=False))
    check("每条机器门禁都可判定",
          bool(machine) and all(g.get("cmd") and g.get("cwd")
                                and g.get("pass_condition") == "exit 0" for g in machine),
          json.dumps(machine, ensure_ascii=False))
    check("机器门禁不是泛化文案",
          all("验证一下" not in g.get("cmd", "") and "跑测试" not in g.get("cmd", "")
              for g in machine))

    # ---- 2) 视觉/交互：人工复核必须带通过条件 ----
    visual = ac.compile_criteria(
        text="修一下面板按钮位置的视觉问题", kind="visual",
        files=["plugin/ui/panel.html"], cfg={})
    check("视觉需求标记需要人看", visual["needs_human"] is True)
    check("人工步骤带通过条件",
          bool(visual["human_checks"])
          and all(h.get("step") and h.get("pass_condition")
                  for h in visual["human_checks"]),
          json.dumps(visual["human_checks"], ensure_ascii=False))
    check("界面文件带面板机器门禁",
          any(g.get("name") == "panel-render" for g in visual["machine_checks"]),
          json.dumps(visual["machine_checks"], ensure_ascii=False))

    # ---- 3) 推不出条件时如实说 unknown ----
    unknown = ac.compile_criteria(text="随便看看", kind="", files=[], cfg={})
    check("无证据时状态是 unknown", unknown["status"] == "unknown",
          json.dumps(unknown, ensure_ascii=False))

    # ---- 4) 消费者投影 ----
    prompt = "\n".join(ac.render_for_prompt(criteria))
    check("执行 prompt 带真实命令", "python3 -m unittest discover" in prompt)
    check("执行 prompt 要求写退出码", "exit 0" in prompt and "退出码" in prompt)
    label = ac.summary_label(criteria)
    check("面板摘要只报条数", label.startswith("验收标准：机器 ")
          and "/ 人工 " in label and "py_compile" not in label, label)

    daemon_src = (OPS / "central_daemon.py").read_text(encoding="utf-8")
    mcp_src = (OPS / "central_mcp.py").read_text(encoding="utf-8")
    panel_src = (OPS.parent / "plugin" / "ui" / "panel.html").read_text(encoding="utf-8")
    check("执行者只读台账里的验收标准",
          'acceptance_criteria.render_for_prompt(item.get("acceptance_criteria"))'
          in daemon_src)
    check("控制层把验收标准投影到工单表",
          'acceptance_criteria.summary_label(' in mcp_src)
    check("面板显示验收标准摘要", "r.acceptance" in panel_src)

    # ---- 5) route --go 真落账 ----
    with tempfile.TemporaryDirectory(prefix="acceptance-criteria-") as raw:
        base = Path(raw)
        root = make_state_root(base)
        env = {**os.environ, "KG_REPO": str(root),
               "KG_AUTOPILOT_HOME": str(base / "autopilot-home"),
               "KG_TRUTH_ID_STATE_ROOT": str(base / "truth-ids")}

        def truthctl(*args):
            return subprocess.run(
                [sys.executable, str(OPS / "truthctl.py"), *args],
                cwd=str(REPO), env=env, capture_output=True, text=True)

        inbox_path = root / "ops" / "tasks" / "inbox.jsonl"
        inbox_path.write_text(json.dumps({
            "ts": "2026-11-18 12:00:00", "text": "修面板按钮位置的视觉问题",
            "source": "user", "files": ["plugin/ui/panel.html"],
            "kind": "unknown", "routed": False, "lane": None,
        }, ensure_ascii=False) + "\n", encoding="utf-8")

        preview = truthctl("route")
        check("route 预演成功", preview.returncode == 0, preview.stderr[-300:])
        check("route 预演显示验收标准", "验收标准：" in preview.stdout, preview.stdout[-500:])
        check("预演不落账", read_jsonl(root / "ops" / "truth" / "ledger.jsonl") == [])

        landed = truthctl("route", "--go")
        check("route --go 成功", landed.returncode == 0, landed.stderr[-300:])
        rows = read_jsonl(root / "ops" / "truth" / "ledger.jsonl")
        check("route --go 新增一条台账", len(rows) == 1,
              json.dumps(rows, ensure_ascii=False))
        rec = rows[0] if rows else {}
        stored = rec.get("acceptance_criteria") or {}
        check("台账带可判定验收标准", stored.get("status") in ("ok", "unknown"),
              json.dumps(stored, ensure_ascii=False))
        check("台账标准含机器门禁", bool(stored.get("machine_checks")),
              json.dumps(stored, ensure_ascii=False))
        check("台账标准含人工复核", bool(stored.get("human_checks")),
              json.dumps(stored, ensure_ascii=False))

        # 归并只补缺失字段，不能拿新输入覆盖既有标准。
        existing = json.dumps({**rec, "acceptance_criteria": {"status": "existing",
                                                              "machine_checks": [],
                                                              "human_checks": []}},
                              ensure_ascii=False)
        (root / "ops" / "truth" / "ledger.jsonl").write_text(existing + "\n",
                                                             encoding="utf-8")
        with inbox_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": "2026-11-18 12:05:00", "text": "修面板按钮位置的视觉问题",
                "source": "user", "files": ["plugin/ui/panel.html"],
                "kind": "unknown", "routed": False, "lane": None,
            }, ensure_ascii=False) + "\n")
        merged = truthctl("route", "--go")
        check("归并 route --go 成功", merged.returncode == 0, merged.stderr[-300:])
        after = read_jsonl(root / "ops" / "truth" / "ledger.jsonl")
        check("归并保留既有验收标准",
              len(after) == 1 and after[0].get("acceptance_criteria", {}).get("status")
              == "existing", json.dumps(after, ensure_ascii=False))

    return len(FAILS)


if __name__ == "__main__":
    code = main()
    print(f"\nacceptance-criteria: {'PASS' if code == 0 else str(code) + ' 项未通过'}")
    raise SystemExit(code)
>>>>>>> codex/acceptance-criteria-compiler-20261118
