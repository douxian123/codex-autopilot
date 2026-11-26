#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：上下文管家的「自动收录」必须不滥收，且只有人点头才生效。

背景（2026-11-18 实测）：用户要的上下文管家是「任何入口说过的预期自动收成约束」。
实际 `truthctl route` 只写泳道与问题台账，从不蒸馏约束；本项目
`constraints.jsonl` 是 0 条，只能人肉手写 —— 「自动整理、归档和调取」只剩后
半截。但直接拿正则往生效台账里灌，又会重演「一句话落错账本」的污染。

本门禁锁五件事：
  1. 明确的规范措辞（不许/必须/以后都/记住…）会被提名为候选；
  2. 描述现状的句子（实测/复现/曾达…）**不**被误收成规范；
  3. 还没路由的条目**不**参与（跟问题台账保持同一道闸）；
  4. 候选卡本身不生效：`constraints.jsonl` 在 promote 之前一个字都不写；
  5. promote 走 `truthctl constraints add` 真写进生效台账并带回 C-id；
     dismiss 则只改候选状态，生效台账仍然不写。

退出码 = 未通过项数。
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def load(env):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(
        "constraint_candidates_t", OPS / "constraint_candidates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


INBOX = [
    # 明确的规范措辞 + 文件线索 → 应提名
    {"ts": "2026-11-18 10:00:00",
     "text": "以后所有面板改动都不许再出现黑色文字",
     "files": ["plugin/ui/panel.html"], "kind": "visual", "routed": True,
     "lane": "design"},
    # 描述现状（实测/复现）→ 不许误收
    {"ts": "2026-11-18 10:01:00",
     "text": "磁盘曾达 99%（可用 1.6GB），实测已复现三遍",
     "files": [], "kind": "gate", "routed": True, "lane": "gate"},
    # 还没路由 → 不参与
    {"ts": "2026-11-18 10:02:00", "text": "这条必须等路由了再说",
     "files": [], "kind": "bug", "routed": False, "lane": None},
]


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-constraint-cand-") as tmp:
        repo = Path(tmp) / "repo"
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in INBOX) + "\n",
            encoding="utf-8")
        m = load({"KG_REPO": str(repo)})
        constraints = repo / "ops" / "truth" / "constraints.jsonl"

        res = m.propose()
        created = res.get("created") or []
        check("规范措辞被提名为候选", len(created) == 1, created)
        if created:
            c = created[0]
            check("候选带原文（不改写用户的话）",
                  c["text"] == "以后所有面板改动都不许再出现黑色文字", c["text"])
            check("建议 kind 是 taboo", c["kind"] == "taboo", c["kind"])
            check("建议 scope 来自文件线索",
                  c["scope"] == ["plugin/ui/panel.html"], c["scope"])
            check("候选带命中信号作为证据", c["signals"] == ["不许"], c["signals"])
            check("候选带范围分类（文件线索=模块）",
                  c.get("scope_class") == "module", c.get("scope_class"))
            check("强措辞候选优先级为高",
                  c.get("priority") == "high", c.get("priority"))
        check("描述现状的句子没有被误收",
              all("磁盘曾达" not in c["text"] for c in created))
        check("还没路由的条目不参与",
              all("必须等路由" not in c["text"] for c in created))
        check("提名阶段不写生效台账", not constraints.exists(), str(constraints))

        check("新规范词被识别（禁止）",
              ("禁止", "taboo") in m.signals("禁止删除数据"),
              m.signals("禁止删除数据"))
        check("新规范词被识别（应该）",
              ("应该", "expectation") in m.signals("应该先解释"),
              m.signals("应该先解释"))
        check("全局措辞分类为 global",
              m.classify_scope("全局必须保留命令原文") == ("global", []),
              m.classify_scope("全局必须保留命令原文"))
        check("流程措辞分类为 workflow（每次提交不是泛化全局）",
              m.classify_scope("每次提交必须跑门禁") == ("workflow", []),
              m.classify_scope("每次提交必须跑门禁"))
        check("文件线索分类为 module",
              m.classify_scope("这里别加隐式依赖", ["a.py"]) == ("module", ["a.py"]),
              m.classify_scope("这里别加隐式依赖", ["a.py"]))
        check("强措辞提高优先级，弱措辞保持普通",
              m.candidate_priority([("必须", "expectation")], "project") == "high"
              and m.candidate_priority([("应该", "expectation")], "project") == "normal")

        # 重复 propose 不重复出卡（同一句话只提一次）。
        again = m.propose()
        check("同一句话不重复提名", again.get("created") == [], again.get("created"))

        cid = created[0]["id"]
        # dismiss：只改候选状态，生效台账仍然不写。
        d = m.dismiss(cid, "这条其实是一次性要求")
        check("dismiss 成功", d.get("status") == "dismissed", d)
        check("dismiss 后生效台账仍然不写", not constraints.exists())
        check("dismiss 后不再挂在待确认里", m.pending() == [], m.pending())

        # 否掉的话不许下一拍又弹回来 —— 那就把「自动收录」变成了「反复烦人」。
        again = m.propose()
        check("dismiss 过的同句话不再重新提名",
              again.get("created") == [], again.get("created"))

        # 换一条新话（走它自己的候选 id）来验 promote 真写生效台账。
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text(
            (repo / "ops" / "tasks" / "inbox.jsonl").read_text(encoding="utf-8") +
            json.dumps({"ts": "2026-11-18 10:03:00",
                        "text": "落盘必须只在 main 上做，不许带 --force",
                        "files": ["ops/kgctl.py"], "kind": "arch",
                        "routed": True, "lane": "arch"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        third = m.propose()
        check("新话会被提名", len(third.get("created") or []) == 1, third.get("created"))
        cid = (third.get("created") or [{}])[0]["id"]
        pr = m.promote(cid, enforceable="none")
        check("promote 成功", pr.get("status") == "promoted", pr)
        check("promote 真写进了生效台账", constraints.exists())
        rows = []
        if constraints.exists():
            rows = [json.loads(l) for l in constraints.read_text(encoding="utf-8").splitlines()
                    if l.strip()]
        check("生效台账里有且只有一条约束", len(rows) == 1, rows)
        if rows:
            check("生效约束是用户原话", rows[0]["statement"] ==
                  "落盘必须只在 main 上做，不许带 --force", rows[0].get("statement"))
            check("生效约束带 scope", rows[0].get("scope") == ["ops/kgctl.py"],
                  rows[0].get("scope"))
            check("生效约束带 scope_class",
                  rows[0].get("scope_class") == "workflow",
                  rows[0].get("scope_class"))
        check("promote 带回 C-id", bool(pr.get("candidate", {}).get("constraint_id")), pr)
        check("候选确认记录保留 scope_class",
              pr.get("candidate", {}).get("scope_class") == "workflow",
              pr.get("candidate", {}).get("scope_class"))

    print()
    if failures:
        print(f"constraint_candidates 门禁：{len(failures)} 项未通过")
        for f in failures:
            print("  - " + f)
        return len(failures)
    print("constraint_candidates 门禁：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
