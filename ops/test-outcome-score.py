#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：结果代理分只读、独立，且绝不拿命令绿灯冒充用户结果。

本门禁锁住六条边界：
  1. 台账缺失 -> unknown，不按 0 分算；
  2. 首问闭环、完成项证据、人工验收边界都是必需判据，不能被别的项替补；
  3. 有真实样本时能算出分数，机器项不计入人工验收；
  4. 标 verified 却没有证据的人工项必须变红；
  5. 全局健康和显式目标取不到仍可报分，但必须标出未覆盖，不静默假装全覆盖；
  6. compute 只读，不写真相台账，也不写关系目标账本。

退出码 = 未通过项数。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import outcome_score  # noqa: E402
import relationship_ledger  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def write_json(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def component(res, key):
    for row in res.get("components") or []:
        if row.get("key") == key:
            return row
    return {}


def seed(repo):
    truth = repo / "ops" / "truth"
    write_json(truth / "mainline.json", {
        "stage": "架构闭环",
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    })
    write_jsonl(truth / "constraints.jsonl", [{
        "id": "C-0001", "ts": "2026-11-18 00:00:00", "type": "constraint",
        "text": "取不到不能说成 0", "status": "active",
    }])
    write_jsonl(truth / "ledger.jsonl", [
        {"id": "L-0001", "ts": "2026-11-18 00:00:01", "type": "ledger",
         "status": "done", "evidence": "python3 ops/test-a.py -> exit 0"},
        {"id": "L-0002", "ts": "2026-11-18 00:00:02", "type": "ledger",
         "status": "done", "evidence": "python3 ops/test-b.py -> exit 0"},
        {"id": "L-0003", "ts": "2026-11-18 00:00:03", "type": "ledger",
         "status": "done", "evidence": "python3 ops/test-c.py -> exit 0"},
        {"id": "L-0004", "ts": "2026-11-18 00:00:04", "type": "ledger",
         "status": "done", "evidence": "python3 ops/test-d.py -> exit 0"},
    ])
    write_jsonl(truth / "acceptance.jsonl", [
        {"id": "A-0001", "ts": "2026-11-18 00:00:05", "type": "acceptance",
         "needs_human": True, "status": "verified",
         "evidence": "用户确认：面板首屏可用"},
        {"id": "A-0002", "ts": "2026-11-18 00:00:06", "type": "acceptance",
         "needs_human": False, "status": "verified",
         "evidence": "python3 ops/selftest.py -> exit 0"},
    ])


def main():
    old_rel = os.environ.get("KG_RELATIONSHIP_GOALS_PATH")
    with tempfile.TemporaryDirectory(prefix="autopilot-outcome-") as tmp:
        base = Path(tmp)
        repo = base / "repo"
        goals_path = base / "goals.jsonl"
        os.environ["KG_RELATIONSHIP_GOALS_PATH"] = str(goals_path)

        missing = outcome_score.compute(repo)
        check("台账缺失时整体 unknown", missing["status"] == "unknown", missing)
        check("台账缺失时不报 0 分", missing["score"] is None, missing)
        check("首问闭环缺失时明确点名北极星",
              "北极星" in missing["headline"]
              and component(missing, "closure")["status"] == "unknown",
              missing["headline"])
        check("必需判据取不到时不按覆盖率放行",
              "北极星" in missing["headline"]
              and "首问闭环" in (missing["missing"] or []),
              (missing["headline"], missing["missing"]))

        seed(repo)
        with_goals = relationship_ledger.create({
            "title": "把自动挡做成长期可信的个人助理",
            "priority": "normal", "projects": ["codex-autopilot"],
            "status": "active", "needs_human": False, "reason": "",
            "source": "user",
        })
        check("测试夹具写入显式目标", with_goals["id"] == "G-0001", with_goals)

        before = {str(p): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        before_goals = goals_path.read_bytes()
        good = outcome_score.compute(repo)
        after = {str(p): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        check("有真实样本时能算出结果代理分",
              good["status"] == "ok" and isinstance(good["score"], int), good)
        check("机器项不计入人工验收",
              "1/1 项由人收口" in component(good, "human")["detail"],
              component(good, "human"))
        check("结果代理分声明不是用户结果真值",
              "机器命令通过不等于用户结果变好" in good["note"], good["note"])
        check("显式目标是弱代理",
              "弱代理" in component(good, "goals")["detail"],
              component(good, "goals"))
        check("compute 不写任何台账", before == after and before_goals == goals_path.read_bytes())

        # 命令全绿但没有用户结果证据：首问闭环无分母，整体必须 unknown。
        no_user_result = base / "no-user-result"
        seed(no_user_result)
        (no_user_result / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
        green = outcome_score.compute(no_user_result)
        check("命令绿灯不等于用户结果变好",
              green["status"] == "unknown" and green["score"] is None, green)
        check("无样本时明确说不能按 0 分算",
              "不能按 0% 计分" in component(green, "closure")["detail"]
              or "首问闭环" in green["headline"],
              (green["headline"], component(green, "closure")))

        # 人工验收标 verified 却没有证据，不能拿机器绿灯替人收口。
        fake_human = base / "fake-human"
        seed(fake_human)
        fake_acc = fake_human / "ops" / "truth" / "acceptance.jsonl"
        rows = [json.loads(l) for l in fake_acc.read_text(encoding="utf-8").splitlines() if l.strip()]
        rows[0]["evidence"] = ""
        write_jsonl(fake_acc, rows)
        bad = outcome_score.compute(fake_human)
        bad_human = component(bad, "human")
        check("人工项无证据时 humane 判据变红",
              bad_human["status"] == "ok" and bad_human["score"] < 100
              and any("verified 却无证据" in s for s in bad_human["shortfall"]),
              bad_human)

        # 可选判据缺失仍可报分，但必须列在未覆盖里，不能静默当满分。
        # 只破坏 project_health 的约束台账；保留 closure_rate 依赖的 ledger，
        # 这样测的是「可选判据未覆盖」，不是把北极星也一起弄丢。
        repo_no_health = base / "no-health"
        seed(repo_no_health)
        (repo_no_health / "ops" / "truth" / "constraints.jsonl").unlink()
        optional_missing = outcome_score.compute(repo_no_health)
        check("全局健康取不到时不冒充满分",
              optional_missing["status"] == "ok"
              and component(optional_missing, "health")["status"] == "unknown"
              and "全局健康" in (optional_missing["missing"] or []),
              optional_missing)

        # CLI 失败路径必须非零退出，防止“算不出来还返回 0”的假绿灯。
        import subprocess
        cli = subprocess.run(
            [sys.executable, str(OPS / "outcome_score.py"), "--repo", str(base / "absent")],
            capture_output=True, text=True, timeout=20)
        check("CLI 取不到时非零退出", cli.returncode == 2,
              (cli.returncode, cli.stdout, cli.stderr))

    if old_rel is None:
        os.environ.pop("KG_RELATIONSHIP_GOALS_PATH", None)
    else:
        os.environ["KG_RELATIONSHIP_GOALS_PATH"] = old_rel

    print()
    if failures:
        print(f"outcome_score 门禁：{len(failures)} 项未通过")
        return len(failures)
    print("outcome_score 门禁：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
