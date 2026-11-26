#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：路线复核状态机只记异常/复测/恢复，不产生纠偏动作。

它锁住五条边界：
  1. 同一问题复测只记一次 still_attention，不每拍刷账；
  2. 判据齐全且复测 ok 时清除旧异常；
  3. unknown 保留旧异常，不冒充恢复；
  4. 多 worker 中一个恢复不会误伤另一个，也不会拿别人的 ok 替 unknown 宣布恢复；
  5. 复核账本里没有 kill / restart / config_change / write_task 等动作字段。

退出码 = 未通过项数。
"""

import json
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import route_review  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def rows(root):
    path = route_review.ledger_path(root)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def attention(headline="实际工作已经偏离 mandate 声明的范围"):
    return {"level": "attention", "headline": headline,
            "reasons": [headline], "unknown": [], "action": "propose_review"}


def ok():
    return {"level": "ok", "headline": "判据齐全，没有发现路线偏离或低效空转",
            "reasons": [], "unknown": [], "action": "none"}


def unknown(headline="路线与效能判据取不到：scope 外改动数取不到"):
    return {"level": "unknown", "headline": headline,
            "reasons": [], "unknown": [headline], "action": "none"}


def main():
    with tempfile.TemporaryDirectory(prefix="route-review-") as tmp:
        root = Path(tmp)
        key = "a|实际工作已经偏离 mandate 声明的范围"
        common = {"issue_keys": [key], "observed_owners": ["a"],
                  "owner_levels": {"a": "attention"}, "root": root}

        first = route_review.sync(attention(), "sig-a1", **common,
                                  now="2026-11-19 12:00:00")
        check("新异常进入 needs_review",
              first.get("written") is True
              and rows(root)[-1]["status"] == "needs_review", first)
        check("首次复核有一条账", len(rows(root)) == 1, rows(root))

        second = route_review.sync(attention(), "sig-a2", **common,
                                   now="2026-11-19 12:15:00")
        check("复测仍是 attention 进入 still_attention",
              second.get("written") is True
              and rows(root)[-1]["status"] == "still_attention", second)
        check("复测确认留第二条账", len(rows(root)) == 2, rows(root))

        third = route_review.sync(attention(), "sig-a3", **common,
                                  now="2026-11-19 12:30:00")
        check("同一问题第三次不刷账",
              third.get("written") is False and len(rows(root)) == 2, third)
        check("复核账本仍有一个待办",
              route_review.current(root)["count"] == 1, route_review.current(root))

        recovered = route_review.sync(
            ok(), "sig-ok", issue_keys=[], observed_owners=["a"],
            owner_levels={"a": "ok"}, root=root, now="2026-11-19 12:45:00")
        check("判据齐全复测 ok 后清除",
              recovered.get("written") is True
              and rows(root)[-1]["status"] == "cleared", recovered)
        check("清除后没有待复核项",
              route_review.current(root)["count"] == 0, route_review.current(root))

        reopened = route_review.sync(attention(), "sig-a4", **common,
                                     now="2026-11-19 13:00:00")
        check("同一问题恢复后再次出现仍是新异常",
              reopened.get("written") is True
              and rows(root)[-1]["status"] == "needs_review", reopened)

        before_unknown = len(rows(root))
        held = route_review.sync(
            unknown(), "sig-unknown", issue_keys=[],
            observed_owners=["a"], owner_levels={"a": "unknown"},
            root=root, now="2026-11-19 13:15:00")
        check("unknown 不写恢复事件",
              held.get("written") is False and len(rows(root)) == before_unknown, held)
        check("unknown 明说没复测成",
              held.get("review_status") == "unknown", held)
        check("unknown 保留旧待复核项",
              route_review.current(root)["count"] == 1, route_review.current(root))

        for row in rows(root):
            check("复核事件不生成动作", row.get("action") == "none", row)
            for forbidden in ("kill", "restart", "patch", "config_change", "write_task"):
                if forbidden in row:
                    failures.append(f"复核事件含 {forbidden}")
                    print(f"  FAIL 复核事件含 {forbidden}")
                    break

    with tempfile.TemporaryDirectory(prefix="route-review-multi-") as tmp:
        root = Path(tmp)
        a_key = "a|实际工作已经偏离 mandate 声明的范围"
        b_key = "b|50 分钟耗在与验收无关的卡点"
        route_review.sync(
            attention(), "sig-multi",
            issue_keys=[a_key, b_key], observed_owners=["a", "b"],
            owner_levels={"a": "attention", "b": "attention"}, root=root)
        route_review.sync(
            attention(), "sig-multi-2",
            issue_keys=[a_key], observed_owners=["a", "b"],
            owner_levels={"a": "attention", "b": "unknown"}, root=root)
        check("另一个 worker 判据缺失时不冒充恢复",
              route_review.current(root)["count"] == 2, route_review.current(root))

        route_review.sync(
            attention(), "sig-multi-3",
            issue_keys=[a_key], observed_owners=["a", "b"],
            owner_levels={"a": "attention", "b": "ok"}, root=root)
        current = route_review.current(root)
        check("另一个 worker 真恢复后只清它自己",
              current["count"] == 1
              and current["items"][0]["issue_key"] == a_key, current)

        # b 从活施工集合里消失时，它名下的旧异常也算恢复；
        # 这与「b 还在但判据 unknown」是两种不同事实。
        route_review.sync(
            attention(), "sig-multi-4",
            issue_keys=[a_key], observed_owners=["a"],
            owner_levels={"a": "attention"}, root=root)
        current = route_review.current(root)
        check("施工对象结束后清除它名下旧项",
              current["count"] == 1
              and current["items"][0]["issue_key"] == a_key, current)

    with tempfile.TemporaryDirectory(prefix="route-review-bad-") as tmp:
        root = Path(tmp)
        route_review.ledger_path(root).write_text("{not json\n", encoding="utf-8")
        broken = route_review.current(root)
        check("复核账本损坏报取不到",
              broken.get("status") == "error" and broken.get("count") is None,
              broken)
        check("取不到不说没有",
              "取不到" in str(broken.get("headline") or ""), broken)

    print("")
    if failures:
        print(f"route_review: {len(failures)} 项未通过")
    else:
        print("route_review: 全部通过")
    return len(failures)


if __name__ == "__main__":
    sys.exit(main())
