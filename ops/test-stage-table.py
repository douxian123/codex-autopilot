#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：工作项「走到哪一步」只能有一份判据，且优先级不许被改反。

为什么有这条：
  「现在什么状态」原本在 `central_mcp._status_payload()`、`plugin/ui/panel.html`
  的 JS、`kgctl doctor` 三处各写一遍。本项目已经因为这种重复踩过一次真实事故——
  CLI 报「未安装」而面板仍显示「后台常驻=开」。这条门禁锁住新收敛出来的
  `ops/stage_table.py`：谁要动优先级，必须先让这里的所有断言继续通过。

重点锁的不是「某个布尔对不对」，而是**几条容易悄悄改反的优先级**：
  1. 出错/关闭/暂停 要盖过一切，不能因为「还有分支可合」就显示成在干活
  2. 「正在实现」要盖过「有卡点」——活已经在跑了，先让人看到它在跑
  3. 「要你拍板」要盖过「等你验收」——不能把需要决策的事埋进验收清单背后
  4. 单看「有没有事要做」时，缺项等于不知道，**不许**凭空说「常驻未开」
  5. 台账里的自由状态词必须归一，认不出的按 todo（宁可低估，不许虚报完成）
"""

import importlib.util
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def load():
    spec = importlib.util.spec_from_file_location("stage_table", OPS / "stage_table.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stage_table"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    st = load()
    print("stage table 单点门禁")

    # ---- 面板级优先级：first-match-wins 不能改反 ----
    check("没事时是 idle", st.panel_stage({})["ui_state"] == "idle",
          st.panel_stage({}))
    check("出错盖过一切",
          st.panel_stage({"error": "x", "review_count": 5, "queued_count": 3})["stage"]
          == "error")
    check("关闭盖过「有事要你」",
          st.panel_stage({"enabled": False, "review_count": 5})["stage"] == "off")
    check("暂停盖过「有分支可合」",
          st.panel_stage({"paused": True, "queued_count": 3})["stage"] == "paused")
    check("正在实现盖过「有卡点」",
          st.panel_stage({"exec_running": True, "blocked_count": 2})["stage"]
          == "working/exec")
    check("持续运行异常盖过「需要你说一句」",
          st.panel_stage({"daemon_problem": True, "answer_count": 9})["stage"]
          == "daemon/problem")
    check("持续运行异常在服务别的项目时用人话",
          st.panel_stage({"daemon_problem": True, "daemon_other_project": True})["label"]
          == "当前项目待命")
    check("「要你拍板」盖过「等你验收」",
          st.panel_stage({"blocked_count": 1, "review_count": 9})["stage"]
          == "person/blocked")
    check("「等你验收」盖过「判不清要你答」",
          st.panel_stage({"review_count": 1, "answer_count": 9})["stage"]
          == "person/review")
    check("判不清归属要露面",
          st.panel_stage({"answer_count": 2})["stage"] == "person/answer")
    check("判不清归属用人话",
          "不知道归到哪个项目" in st.panel_stage({"answer_count": 2})["label"])
    check("常驻没接上要露面",
          st.panel_stage({"daemon_live": False})["stage"] == "session_only")
    check("在服务别的项目时文案不同",
          st.panel_stage({"daemon_live": False, "daemon_other_project": True})["label"]
          == st.CURRENT_PROJECT_IDLE)
    check("服务别的项目时完整结论仍单独保留",
          "这个项目暂时不需要你操作" in st.SESSION_ONLY_OTHER_PROJECT)
    check("在服务别的项目时不说后台队列黑话",
          "不会混入它的队列" not in st.SESSION_ONLY_OTHER_PROJECT)
    check("有分支可合 = working/queued",
          st.panel_stage({"queued_count": 1})["stage"] == "working/queued")

    # ---- 缺项 ≠ 有事：读不到不许凭空报「常驻未开」 ----
    check("daemon_live 缺项时不报「常驻未开」",
          st.panel_stage({})["stage"] != "session_only")
    check("daemon_live 明确 False 才报",
          st.panel_stage({"daemon_live": False})["stage"] == "session_only")
    check("永不抛异常（喂垃圾输入）",
          st.panel_stage({"blocked_count": "abc", "review_count": None})["stage"]
          in ("idle", "session_only", "working/queued"))

    # ---- 单工作项状态归一 ----
    for word in ("open", "todo", "next", "queued", "pending"):
        check(f"{word} → todo", st.norm_item_status(word) == "todo")
    for word in ("doing", "wip", "in_progress", "in-progress", "active"):
        check(f"{word} → doing", st.norm_item_status(word) == "doing")
    for word in ("blocked", "stuck", "failed"):
        check(f"{word} → stuck", st.norm_item_status(word) == "stuck")
    check("大小写不敏感", st.norm_item_status("IN_PROGRESS") == "doing")
    check("认不出的按 todo（宁可低估不虚报完成）",
          st.norm_item_status("随便什么") == "todo")
    check("空值按 todo", st.norm_item_status(None) == "todo")

    # 需要人拍板的验收项不能混进「进行中」。
    check("等人复核的验收项 = person/review",
          st.item_stage("doing", needs_human=True) == "person/review")
    check("已验过的验收项不再占 review",
          st.item_stage("verified", needs_human=True, is_verified=True) == "review")

    # ---- 四个计数格子 ----
    c = st.stage_counts(doing=2, todo=3, review=1, stuck=4, exec_running=True)
    check("正在实现要算进 doing", c["doing"] == 3, c)
    check("todo 原样", c["todo"] == 3, c)
    check("review 原样", c["review"] == 1, c)
    check("stuck 原样", c["stuck"] == 4, c)
    c0 = st.stage_counts()
    check("空输入四个格子都是 0（不是 None）",
          c0 == {"todo": 0, "doing": 0, "review": 0, "stuck": 0}, c0)

    print()
    if failures:
        print(f"结果：{len(failures)} 项未通过")
    else:
        print("selftest ok: 工作项 stage 判据已归一，面板/CLI 不再各算一套。")
    return len(failures)


if __name__ == "__main__":
    raise SystemExit(main())
