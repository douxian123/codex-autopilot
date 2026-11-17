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

        err = raises(rel.ConflictError,
                     lambda: rel.update("G-9999", goal(source="user"), target))
        check("更新不存在的目标被拒绝", bool(err) and "不存在" in err, err)
        err = raises(rel.ConflictError,
                     lambda: rel.update("G-0001", goal(id="G-0002", source="user"), target))
        check("update 内容与 id 冲突被拒绝", bool(err), err)

        updated = rel.update("G-0001", goal(status="done", source="user"), target)
        check("update 保留 id 并递增 seq",
              updated["id"] == "G-0001" and updated["seq"] == 3, updated)
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
