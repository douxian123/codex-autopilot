#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文管家自学习门禁：重复诉求只提名，不自动生效。"""

import importlib.util
import json
import os
import subprocess
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


def load(name, filename, env):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(name, OPS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def session(path, sid, texts, cwd):
    rows = [{"type": "session_meta", "payload": {
        "id": sid, "session_id": sid, "cwd": cwd,
        "runtime_workspace_roots": [cwd]}}]
    for i, text in enumerate(texts):
        rows.append({"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}})
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-context-learning-") as tmp:
        root = Path(tmp)
        repo = root / "repo"
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks").mkdir(parents=True)
        sessions = root / "sessions"
        sessions.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "https://example.invalid/test/context-learning.git"], check=True)
        worktree = root / "agent-worktree"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        subprocess.run(["git", "-C", str(worktree), "remote", "add", "origin",
                        "https://example.invalid/test/context-learning.git"], check=True)
        other_repo = root / "other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(other_repo)], check=True)
        subprocess.run(["git", "-C", str(other_repo), "remote", "add", "origin",
                        "https://example.invalid/test/other-project.git"], check=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "project_folders.json").write_text(json.dumps({
            "folders": [{
                "id": "context-learning-test",
                "name": "context-learning-test",
                "path": str(repo),
                "remote": "https://example.invalid/test/context-learning.git",
            }]}, ensure_ascii=False), encoding="utf-8")

        texts = [
            "面板不要出现任何与节点信息无关的信息，噪音要尽量少",
            "读者面遵循用户注意力机制，内部记账不要上屏",
            "这个面板太吵了，无关信息必须降噪",
        ]
        session(sessions / "a.jsonl", "thread-a", texts[:2], str(repo))
        session(sessions / "b.jsonl", "thread-b", texts[2:], str(repo))
        session(sessions / "worktree.jsonl", "thread-f", texts[:1], str(worktree))
        session(sessions / "other.jsonl", "thread-c", texts, str(other_repo))
        session(sessions / "promote-a.jsonl", "thread-d", [
            "所有改动只能做正向优化，绝对不允许破坏性修改或影响用户工作进程",
            "不要做负向修改，要非破坏性地推进",
        ], str(repo))
        session(sessions / "promote-b.jsonl", "thread-e", [
            "绝对不允许做破坏性修改，只能做优化",
        ], str(repo))
        session(sessions / "docs.jsonl", "thread-g", [
            "context-learning-test 的面板不要出现无关信息，必须遵循用户注意力机制",
            "万法归一入口应该把不同项目的输入路由到对应项目",
        ], "/tmp/project-discussion-docs")
        session(sessions / "other-docs.jsonl", "thread-h", [
            "context-learning-test 可以借鉴这个面板设计，但当前项目是别的产品",
            "这是别的项目，不要落进 context-learning-test 账本",
        ], "/tmp/other-project-discussion-docs")
        # 同一会话跨 rollout 重发同一句原话，只算一次提及。
        session(sessions / "a-rollout-2.jsonl", "thread-a", [texts[0]], str(repo))
        # harness / goal / delegation / 心跳的注入不是人的诉求，哪怕长得像用户消息。
        session(sessions / "noise.jsonl", "thread-noise", [
            '<external_goal_objective>闭环修好这个问题，提交到 main 才算证据</external_goal_objective>',
            '<codex_internal_context source="user_goal">面板必须降噪并遵循用户注意力机制</codex_internal_context>',
            '<hook_prompt hook_run_id="hook-1">万法归一入口应该把问题路由调度出去</hook_prompt>',
            '<heartbeat>thoughloom-29：I-xxxx 闭环进度检查</heartbeat>',
            'System message from the application, not from the user: 面板不要有噪音',
            '你是项目「thoughloom-29」的「司令台」，面板必须遵循注意力机制',
        ], str(repo))

        env = {"KG_REPO": str(repo), "KG_SESSION_ROOTS": str(sessions)}
        learning = load("context_learning_t", "context_learning.py", env)

        identity = learning._project_identity()
        check("同 remote 的隔离 worktree 归到本项目",
              learning._session_belongs({"cwd": str(worktree)}, identity), identity)
        check("不同 remote 的仓库不归本项目",
              not learning._session_belongs({"cwd": str(other_repo)}, identity), identity)
        check("文档型项目讨论归到本项目",
              learning._content_belongs(sessions / "docs.jsonl", identity), identity)
        check("显式别的项目不归本项目",
              not learning._content_belongs(sessions / "other-docs.jsonl", identity), identity)
        check("系统注入与委派正文不算用户原话",
              not list(learning._iter_user_texts(
                  learning._iter_jsonl_raw_lines(sessions / "noise.jsonl"))), identity)

        obs = learning.observe()
        check("历史会话可读", obs.get("status") == "ok", obs)
        themes = {t["key"]: t for t in obs.get("themes") or []}
        check("同主题达到阈值才提名", "reader_surface_noise" in themes, themes)
        row = themes.get("reader_surface_noise") or {}
        check("统计提及次数", row.get("mentions") == 5, row)
        check("统计跨会话数", row.get("threads") == 4, row)
        check("别人的项目不进账", all(x.get("thread") != "thread-c"
                                     for x in row.get("evidence") or []), row)
        check("文档型讨论窗能归到本项目", all(x.get("thread") != "thread-h"
                                       for x in row.get("evidence") or []), row)
        check("文档型讨论窗的主题证据可学习", any(x.get("thread") == "thread-g"
                                          for x in row.get("evidence") or []), row)
        check("跨 rollout 的同一句原话不重复计数", row.get("mentions") == 5, row)
        check("系统注入不进主题证据", all(x.get("thread") != "thread-noise"
                                    for x in row.get("evidence") or []), row)

        low = learning.observe(min_threads=5, min_mentions=3)
        check("阈值未达不提名", "themes" in low and not low.get("themes"), low)

        proposed = learning.propose()
        created = proposed.get("created") or []
        check("重复主题转成候选", len(created) == 2, proposed)
        constraints = repo / "ops" / "truth" / "constraints.jsonl"
        check("确认前不写生效约束", not constraints.exists())
        results = proposed.get("candidate_results") or []
        created_candidates = [x.get("created") or [] for x in results]
        pending = [row for group in created_candidates for row in group]
        check("候选进入统一闸", len(pending) == 2, results)
        cc = next(x for x in pending if x.get("source_topic") == "reader_surface_noise")
        check("候选带学习来源", cc.get("source") == "context_learning", cc)
        check("候选带原话证据", bool(cc.get("evidence")), cc)

        again = learning.propose()
        check("同一主题不重复提名", not again.get("created"), again)

        dc = learning.dismiss_learned(cc["id"], "暂时不用")
        check("dismiss 成功", dc.get("status") == "dismissed", dc)
        check("dismiss 后仍不写生效约束", not constraints.exists())
        again = learning.propose()
        check("dismiss 过的主题不再复发", not again.get("created"), again)

        # dismiss 过的主题不走晋升；另给一个未 dismiss 的学习候选验证确认出口。
        cid = next(x["id"] for x in pending
                   if x.get("source_topic") == "non_destructive_positive_only")
        pr = learning.promote_learned(cid, enforceable="none")
        check("人确认后晋升成功", pr.get("status") == "promoted", pr)
        check("晋升后写生效约束", constraints.exists())
        rows = [json.loads(x) for x in constraints.read_text(encoding="utf-8").splitlines()
                if x.strip()]
        check("生效约束带学习来源", rows[0].get("source") == "context_learning", rows)
        check("生效约束带学习来源标识", rows[0].get("source") == "context_learning", rows)
        sc = learning.selfcheck()
        check("逐条自检能读到学习约束", sc.get("status") == "ok" and sc.get("items"), sc)

        bad = root / "missing-sessions"
        bad_env = {"KG_REPO": str(repo), "KG_SESSION_ROOTS": str(bad)}
        missing = load("context_learning_missing_t", "context_learning.py", bad_env)
        res = missing.observe()
        check("源目录取不到报 unknown", res.get("status") == "unknown", res)

    print()
    if failures:
        print(f"context_learning 门禁：{len(failures)} 项未通过")
        for f in failures:
            print("  - " + f)
        return len(failures)
    print("context_learning 门禁：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
