#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：跨项目长期目标账本只接受显式写入，且损坏必须 fail-closed。

关系层此前只有聊天里的目标和面板三问，没有稳定对象。这个门禁锁住：
  1. 文件不存在 -> unknown，不能说成 0 条；
  2. 空账本才是 0 条；
  3. inferred 来源、缺 reason 的 needs_human 一律拒绝；
  4. create/update 保持 seq 与 id 稳定，投影不增加新目标；
  5. JSON 损坏、seq 断裂、重复创建、未知字段 -> unknown 且拒绝继续写。

退出码 = 未通过项数。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import relationship_ledger as rel  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def raises(exc_type, fn):
    try:
        fn()
    except exc_type as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return f"错误类型不对：{exc!r}"
    return None


def goal(**over):
    row = {
        "title": "把自动挡做成长期可信的个人助理",
        "priority": "normal",
        "projects": ["codex-autopilot", "personal-memory"],
        "status": "active",
        "needs_human": False,
        "reason": "",
        "north_star": "用户只说一次，系统跨项目记住并推进",
        "source_thread": "thread-relationship-20261118",
        "linked_ledger": ["L-0076"],
        "linked_acceptance": ["A-0069"],
        "last_touched": "2026-11-18T06:00:00Z",
        "source": "user",
    }
    row.update(over)
    return row


def write_rows(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def main():
    old_env = os.environ.get("KG_RELATIONSHIP_GOALS_PATH")
    with tempfile.TemporaryDirectory(prefix="autopilot-relationship-") as tmp:
        target = Path(tmp) / "goals.jsonl"
        os.environ["KG_RELATIONSHIP_GOALS_PATH"] = str(target)

        missing = rel.summary()
        check("账本不存在时 unknown", missing["status"] == "unknown", missing)
        check("账本不存在不说成 0 条",
              missing["count"] is None and missing["needs_human_count"] is None,
              missing)

        target.write_text("", encoding="utf-8")
        empty = rel.summary()
        check("空账本才是 0 条目标",
              empty["status"] == "ok" and empty["count"] == 0, empty)

        created = rel.create(goal(source="user"))
        check("显式 user 来源可写入",
              created["id"] == "G-0001" and created["seq"] == 1,
              created)
        check("目标带跨项目字段",
              created["projects"] == ["codex-autopilot", "personal-memory"],
              created)
        check("目标带长期目标关联字段",
              created["north_star"] == "用户只说一次，系统跨项目记住并推进"
              and created["source_thread"] == "thread-relationship-20261118"
              and created["linked_ledger"] == ["L-0076"]
              and created["linked_acceptance"] == ["A-0069"]
              and created["last_touched"] == "2026-11-18T06:00:00Z",
              created)

        err = raises(rel.AuthorizationError,
                     lambda: rel.create(goal(source="inferred"), target))
        check("推断来源被拒绝", bool(err) and "显式" in err, err)
        err = raises(rel.RelationshipError,
                     lambda: rel.create(goal(needs_human=True, reason=""), target))
        check("需要人的目标必须写原因", bool(err) and "reason" in err, err)
        err = raises(rel.RelationshipError,
                     lambda: rel.create(goal(priority="urgent!"), target))
        check("优先级非法被拒绝", bool(err), err)

        second = rel.create(goal(
            title="把验收候选编译成真门禁",
            priority="high",
            projects=["codex-autopilot"],
            needs_human=True,
            reason="机器门禁能跑完才请人复核",
            source="operator",
        ), target)
        check("operator 显式来源可写入", second["id"] == "G-0002", second)
        summary = rel.summary()
        check("摘要按优先级排序", summary["items"][0]["id"] == "G-0002", summary)
        check("需要人的目标抬成 attention",
              summary["level"] == "attention" and summary["needs_human_count"] == 1,
              summary)
        check("需要人的原因进摘要",
              any("机器门禁" in (r.get("reason") or "") for r in summary["items"]),
              summary)
        check("关联字段进只读摘要",
              all(key in summary["items"][0]
                  for key in ("north_star", "source_thread", "linked_ledger",
                              "linked_acceptance", "last_touched")),
              summary["items"][0])

        linked = rel.link(
            "G-0002", ledger=["L-0099"], acceptance=["A-0071"],
            thread="thread-linked", source="operator", path=target)
        check("link 追加工作项与验收关联",
              "L-0099" in linked["linked_ledger"]
              and "A-0071" in linked["linked_acceptance"]
              and linked["source_thread"] == "thread-linked", linked)
        check("link 不覆盖已有来源", linked["source"] == "operator", linked)

        closed = rel.close(
            "G-0002", "已按门禁和人工复核收口",
            source="operator", evidence="test-relationship-ledger.py", path=target)
        check("close 写入收口证据与状态",
              closed["status"] == "done" and closed["needs_human"] is False
              and closed["evidence"] == "test-relationship-ledger.py"
              and "已按门禁和人工复核收口" in closed["reason"], closed)
        check("close 不丢北极星和关联",
              closed["north_star"] and "L-0099" in closed["linked_ledger"], closed)

        err = raises(rel.ConflictError,
                     lambda: rel.update("G-9999", goal(source="user"), target))
        check("更新不存在的目标被拒绝", bool(err) and "不存在" in err, err)
        err = raises(rel.ConflictError,
                     lambda: rel.update("G-0001", goal(id="G-0002", source="user"), target))
        check("update 内容与 id 冲突被拒绝", bool(err), err)

        seq_before = len(target.read_text(encoding="utf-8").splitlines())
        updated = rel.update("G-0001", goal(status="done", source="user"), target)
        check("update 保留 id 并递增 seq",
              updated["id"] == "G-0001" and updated["seq"] == seq_before + 1, updated)
        after = rel.summary()
        check("update 不新增目标", after["count"] == 2, after)
        check("update 后状态收口",
              after["items"][1]["status"] == "done"
              or after["items"][0]["status"] == "done", after)

        before_read = target.read_bytes()
        rel.summary()
        check("只读摘要不写账本", target.read_bytes() == before_read)

        good_text = target.read_text(encoding="utf-8")
        target.write_text(good_text + '{"broken":\n', encoding="utf-8")
        broken = rel.summary()
        check("JSON 损坏时 unknown", broken["status"] == "unknown", broken)
        err = raises(rel.LedgerCorrupt,
                     lambda: rel.create(goal(source="user"), target))
        check("损坏账本拒绝继续写", bool(err), err)

        target.write_text(good_text, encoding="utf-8")
        rows = [json.loads(line) for line in good_text.splitlines() if line.strip()]
        rows[-1]["seq"] = 99
        write_rows(target, rows)
        check("seq 断裂时 unknown", rel.summary()["status"] == "unknown",
              rel.summary())

        target.write_text(good_text, encoding="utf-8")
        rows = [json.loads(line) for line in good_text.splitlines() if line.strip()]
        rows.append({**rows[0], "seq": len(rows) + 1, "updated_at": "2026-11-18T00:00:00Z"})
        write_rows(target, rows)
        check("重复创建同一目标时 unknown", rel.summary()["status"] == "unknown",
              rel.summary())

        target.write_text(good_text, encoding="utf-8")
        rows = [json.loads(line) for line in good_text.splitlines() if line.strip()]
        rows[-1]["auto_dispatch"] = True
        write_rows(target, rows)
        unknown = rel.summary()
        check("未知字段时 unknown", unknown["status"] == "unknown", unknown)
        check("损坏不伪装成正常", "取不到" in unknown["headline"], unknown)

        # 旧事件没有新增关联字段：必须能投影，且不能把旧账本说成损坏。
        legacy_path = Path(tmp) / "legacy.jsonl"
        legacy = {
            "type": "goal.created", "id": "G-0001", "seq": 1,
            "ts": "2026-11-18T00:00:00Z", "updated_at": "2026-11-18T00:00:00Z",
            "title": "旧版长期目标", "priority": "normal",
            "projects": ["codex-autopilot"], "status": "active",
            "needs_human": False, "reason": "", "source": "user",
        }
        legacy_path.write_text(json.dumps(legacy, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        legacy_summary = rel.summary(legacy_path)
        check("旧账本没有新字段仍可读",
              legacy_summary["status"] == "ok" and legacy_summary["count"] == 1,
              legacy_summary)
        check("旧账本缺失关联字段如实留空",
              legacy_summary["items"][0]["linked_ledger"] == []
              and legacy_summary["items"][0]["last_touched"] is None,
              legacy_summary["items"][0])

        # 命令入口也必须闭环：create -> link -> close -> list 是用户真正会走的路径。
        cli_path = Path(tmp) / "cli-goals.jsonl"
        cli_env = {**os.environ, "KG_RELATIONSHIP_GOALS_PATH": str(cli_path)}

        def truthctl_goal(*cli_args):
            return subprocess.run(
                [sys.executable, str(OPS / "truthctl.py"), "goal", *cli_args],
                cwd=str(OPS.parent), env=cli_env, capture_output=True, text=True)

        created_cli = truthctl_goal(
            "create", "--title", "CLI 长期目标", "--priority", "normal",
            "--projects", "codex-autopilot,personal-memory", "--status", "active",
            "--north-star", "用户只说一次，跨项目长期记住", "--source", "user")
        check("truthctl goal create 可登记显式目标",
              created_cli.returncode == 0 and "G-0001" in created_cli.stdout,
              (created_cli.returncode, created_cli.stdout, created_cli.stderr))

        linked_cli = truthctl_goal(
            "link", "G-0001", "--ledger", "L-0100", "--acceptance", "A-0100",
            "--thread", "thread-cli", "--source", "user")
        check("truthctl goal link 可追加关联",
              linked_cli.returncode == 0
              and "L-0100" in linked_cli.stdout
              and "A-0100" in linked_cli.stdout,
              (linked_cli.returncode, linked_cli.stdout, linked_cli.stderr))

        closed_cli = truthctl_goal(
            "close", "G-0001", "已由 CLI 收口", "--evidence", "cli-regression",
            "--source", "user")
        check("truthctl goal close 必须带结论和证据",
              closed_cli.returncode == 0
              and '"status": "done"' in closed_cli.stdout
              and "cli-regression" in closed_cli.stdout,
              (closed_cli.returncode, closed_cli.stdout, closed_cli.stderr))

        listed_cli = truthctl_goal("list")
        check("truthctl goal list 读回收口后的目标",
              listed_cli.returncode == 0
              and "G-0001" in listed_cli.stdout
              and "L-0100" in listed_cli.stdout
              and "cli-regression" in listed_cli.stdout,
              (listed_cli.returncode, listed_cli.stdout, listed_cli.stderr))

    if old_env is None:
        os.environ.pop("KG_RELATIONSHIP_GOALS_PATH", None)
    else:
        os.environ["KG_RELATIONSHIP_GOALS_PATH"] = old_env

    print()
    if failures:
        print(f"relationship_ledger 门禁：{len(failures)} 项未通过")
        return len(failures)
    print("relationship_ledger 门禁：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
