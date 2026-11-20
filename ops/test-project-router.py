#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：「万法归一」入口的**归属判定**不许猜。

为什么有这条（用户实测 2026-11-16）：
  插件 `.mcp.json` 写死了一个 `KG_REPO`，于是用户在 A 项目窗口里说 B 项目的事，
  记录照样落进 A 的账本。用户原话：「为什么我在别的窗口说别的东西都会落到另一个
  账本里…这不对呀」。修法是从「安装时写死」改成「每次投稿现算归属」。

这条门禁锁住的正是「现算」不能腐化成「猜」：
  1. 原话点名了一个项目 → 路由到它（最强证据）
  2. 原话同时点名了两个 → ambiguous，**不许**靠名字长度那 1 分差硬选一个
  3. 同一层证据咬得太近 → ambiguous（近似并列也不许猜）
  4. 判成 ambiguous 时，**任何项目账本都不许被写入**，只能进全局待路由箱
  5. 分层顺序：原话点名 > 文件路径 > cwd > 泛线索词。cwd 不能盖过用户点名
  6. 同一个状态根重复登记只留一条；`ensure_registered` 不许把用户显式起的好名字
     覆盖成目录名（冒烟实测踩到过：alpha 被写成 stateA）
  7. 过短的线索（单字）不触发匹配
  8. 项目身份是**仓库**而不是目录：同一个 git 仓库的 worktree / 落盘主干都算同一个
     项目。用户的真实工作方式就是「共享检出只读、在 worktree 里干活」——冒烟实测
     worktree 里说话会判不出归属，而 `<项目>-trunk` 会被重复登记成第二个项目。
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
KGCTL = OPS / "kgctl.py"
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load_router():
    spec = importlib.util.spec_from_file_location("project_router_under_test",
                                                  OPS / "project_router.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_state_root(base, name):
    root = base / name
    (root / "ops" / "tasks").mkdir(parents=True)
    (root / "ops" / "truth").mkdir(parents=True)
    (root / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    return root


def git_init(root):
    """把 state root 变成一个真 git 仓库：仓库身份判定要求 worktree 能跑通。"""
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@t"
    subprocess.run(["git", "init", "-q", str(root)], check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True, capture_output=True, env=env)
    return root


def run_kgctl(repo, trunk, *args, extra_env=None):
    env = os.environ.copy()
    env["KG_REPO"] = str(repo)
    env["KG_TRUNK"] = str(trunk)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    return subprocess.run([sys.executable, str(KGCTL), *args],
                          cwd=str(OPS.parent), env=env,
                          capture_output=True, text=True)


def main():
    base = Path(tempfile.mkdtemp(prefix="project-router-test-"))
    home = base / "home"
    home.mkdir()
    os.environ["KG_AUTOPILOT_HOME"] = str(home)
    os.environ.pop("KG_DAEMON_HOME", None)
    pr = load_router()

    try:
        alpha = make_state_root(base, "stateA")
        beta = make_state_root(base, "stateB")

        # ---- 1. 注册与幂等：同一个状态根只留一条，显式名字不被覆盖 ----
        pr.register("alpha", alpha, clues=["智能体"], path=home / "projects.json")
        pr.register("alpha", alpha, clues=["智能体", "对话编排"],
                    path=home / "projects.json")
        reg = pr.load_registry(home / "projects.json")
        check("同一状态根重复登记只留一条", len(reg) == 1, f"len={len(reg)}")
        check("重复登记的线索按并集累积，不重复不丢失",
              reg[0]["clues"] == ["智能体", "对话编排"], reg[0]["clues"])

        # ensure_registered 只知道目录名；不能拿它覆盖用户显式起的好名字。
        kept = pr.ensure_registered(alpha, path=home / "projects.json")
        check("ensure_registered 保留用户显式命名的项目名",
              kept["name"] == "alpha", kept["name"])
        # 目录名 stateA 绝不能出现在注册表里（那就是「候选：stateA」那个 bug）。
        names = [r["name"] for r in pr.load_registry(home / "projects.json")]
        check("目录名不会成为候选名", "stateA" not in names, names)

        pr.register("beta", beta, clues=["搜索面板"], path=home / "projects.json")
        registry = pr.load_registry(home / "projects.json")

        # ---- 2. 原话点名一个：resolved ----
        got = pr.match("beta项目的搜索面板要加缓存", [], registry)
        check("点名一个项目 → resolved",
              got["decision"] == "resolved" and got["project"] == "beta", got)
        check("resolved 时给出命中理由与层级",
              got["layer"] == "named" and got["reasons"], got)

        # ---- 3. 同时点名两个：ambiguous，不许靠分数差硬选 ----
        got = pr.match("alpha项目和beta项目都要改缓存", [], registry)
        check("同时点名两个项目 → ambiguous",
              got["decision"] == "ambiguous", got)
        cand = {c["name"] for c in got.get("candidates") or []}
        check("ambiguous 的候选必须把两个都列出来",
              cand == {"alpha", "beta"}, cand)

        # ---- 4. 近似并列：咬得太近也不许猜 ----
        near = [
            {"name": "aa", "root": "/x/aa", "clues": [], "aliases": []},
            {"name": "aab", "root": "/x/aab", "clues": [], "aliases": []},
        ]
        got = pr.match("刚才那个 aab 的事情", [], near)
        check("名字长度差带来的近似并列 → ambiguous",
              got["decision"] == "ambiguous", got)

        # ---- 5. 分层：点名必须压过 cwd ----
        got = pr.match("beta项目要改缓存", [], registry, cwd=str(alpha))
        check("原话点名压过 cwd（点名 beta，人在 alpha）",
              got["decision"] == "resolved" and got["project"] == "beta", got)
        # cwd 单独出现时仍然是有效证据。
        got = pr.match("那个面板要加缓存", [], registry, cwd=str(beta))
        check("只有 cwd 时也能定项目",
              got["decision"] == "resolved" and got["project"] == "beta", got)
        check("cwd 命中的层级标成 cwd", got["layer"] == "cwd", got)
        # 文件路径压过 cwd。
        got = pr.match("随便说点什么", [str(alpha / "ops" / "tasks" / "inbox.jsonl")],
                       registry, cwd=str(beta))
        check("文件路径压过 cwd",
              got["decision"] == "resolved" and got["project"] == "alpha", got)
        check("文件路径命中的层级标成 files", got["layer"] == "files", got)

        # ---- 6. 泛线索词是最弱的一层，且过短不触发 ----
        got = pr.match("智能体那块顺手看一下", [], registry)
        check("自定义线索词可以单独定位项目",
              got["decision"] == "resolved" and got["project"] == "alpha", got)
        check("线索词命中的层级标成 clue", got["layer"] == "clue", got)
        short = [{"name": "q", "root": "/x/q", "clues": ["啊"], "aliases": []}]
        got = pr.match("啊这个不太对", [], short)
        check("单字线索不触发匹配", got["decision"] == "none", got)

        # ---- 7. 谁都不像：none ----
        got = pr.match("把按钮再往左挪 8 像素", [], registry)
        check("没有线索时 → none", got["decision"] == "none", got)

        # ---- 8. 端到端：ambiguous 不许写进任何项目账本 ----
        before = {p: (p / "ops" / "tasks" / "inbox.jsonl").read_text(encoding="utf-8")
                  for p in (alpha, beta)}
        p = run_kgctl(alpha, base / "stateA-trunk", "inbox",
                      "alpha项目和beta项目都要改缓存")
        out = p.stdout + p.stderr
        after = {q: (q / "ops" / "tasks" / "inbox.jsonl").read_text(encoding="utf-8")
                 for q in (alpha, beta)}
        check("ambiguous 时两个项目账本都没被写入", before == after, out[:200])
        check("ambiguous 时明确告诉用户「没写进任何项目账本」",
              "没写进任何项目账本" in out, out[:200])
        routing = pr.routing_inbox_path()
        rows = [json.loads(l) for l in routing.read_text(encoding="utf-8").splitlines()
                if l.strip()] if routing.exists() else []
        check("ambiguous 的记录进了全局待路由箱", len(rows) == 1, rows)
        check("待路由记录标成 ambiguous 并带候选",
              rows and rows[0].get("status") == "ambiguous"
              and len(rows[0].get("candidates") or []) == 2, rows)

        # ---- 9. 端到端：点名一个项目 → 写进那个项目的账本，不写别的 ----
        p = run_kgctl(alpha, base / "stateA-trunk", "inbox", "beta项目要加缓存")
        out = p.stdout + p.stderr
        beta_lines = (beta / "ops" / "tasks" / "inbox.jsonl").read_text(
            encoding="utf-8").splitlines()
        check("点名 beta 时写进 beta 的账本", len(beta_lines) == 1, beta_lines)
        check("点名 beta 时不写进当前绑定的 alpha",
              (alpha / "ops" / "tasks" / "inbox.jsonl").read_text(
                  encoding="utf-8").strip() == "", out[:200])

        # ---- 10. 端到端：多项目下「谁都没提」不许回退到绑定项目 ----
        before_alpha = (alpha / "ops" / "tasks" / "inbox.jsonl").read_text(
            encoding="utf-8")
        p = run_kgctl(alpha, base / "stateA-trunk", "inbox", "把按钮往左挪 8 像素")
        out = p.stdout + p.stderr
        check("多项目下判不清 → 不许静默落回绑定账本",
              (alpha / "ops" / "tasks" / "inbox.jsonl").read_text(
                  encoding="utf-8") == before_alpha, out[:200])

        # ---- 11. 显式指派：KG_INBOX_PROJECT 解析不了就必须停 ----
        p = run_kgctl(alpha, base / "stateA-trunk", "inbox", "指派给一个不存在的项目",
                      extra_env={"KG_INBOX_PROJECT": "nosuch"})
        out = p.stdout + p.stderr
        check("指派了注册表里没有的项目 → 不许悄悄落回绑定账本",
              "没写进任何项目账本" in out, out[:200])
        p = run_kgctl(alpha, base / "stateA-trunk", "inbox", "指派给 beta",
                      extra_env={"KG_INBOX_PROJECT": "beta"})
        check("显式指派能生效",
              "beta" in (p.stdout + p.stderr), p.stdout[:200])

        # ---- 12. 项目身份是仓库：worktree / 落盘主干都算同一个项目 ----
        # 用户真实工作方式 = 共享主检出只读、在 worktree 里干活。早先按「目录包含」
        # 判归属时，worktree 是主检出的兄弟目录，判定永远不成立 → 掉进待路由箱；
        # 而 <项目>-trunk 自己看着像状态根 → 被重复登记成第二个项目。
        wtbase = base / "wt"
        wtbase.mkdir()
        repo = git_init(make_state_root(wtbase, "kg"))
        git_env = os.environ.copy()
        git_env["GIT_AUTHOR_NAME"] = git_env["GIT_COMMITTER_NAME"] = "t"
        git_env["GIT_AUTHOR_EMAIL"] = git_env["GIT_COMMITTER_EMAIL"] = "t@t"
        worktree = wtbase / "kg-worktree-1"
        trunk = wtbase / "kg-trunk"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q",
                        "-b", "fx", str(worktree)], check=True,
                       capture_output=True, env=git_env)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q",
                        "-b", "trunkbranch", str(trunk)], check=True,
                       capture_output=True, env=git_env)
        (trunk / "ops" / "tasks").mkdir(parents=True, exist_ok=True)
        (trunk / "ops" / "truth").mkdir(parents=True, exist_ok=True)

        # 只注册主检出；worktree 与 trunk 必须被认出属于同一个项目。
        pr.register("kg", repo, path=home / "projects.json")
        p = run_kgctl(repo, trunk, "inbox", "worktree 里发现一个 bug",
                      extra_env={"KG_INBOX_CWD": str(worktree)})
        out = p.stdout + p.stderr
        check("在 worktree 里说话 → 归到主项目，不掉进待路由箱",
              "属于「kg」" in out and "待路由箱" not in out, out[:300])
        check("worktree 的投稿写进主检出账本",
              len((repo / "ops" / "tasks" / "inbox.jsonl").read_text(
                  encoding="utf-8").splitlines()) == 1,
              (repo / "ops" / "tasks" / "inbox.jsonl").read_text(encoding="utf-8"))
        p = run_kgctl(repo, trunk, "inbox", "主干里的一句话",
                      extra_env={"KG_INBOX_CWD": str(trunk)})
        check("落盘主干说话也归到主项目（不被当成第二个项目）",
              "属于「kg」" in (p.stdout + p.stderr), (p.stdout + p.stderr)[:300])
        names = [r["name"] for r in pr.load_registry(home / "projects.json")]
        check("同一仓库的 worktree / trunk 不会各占一条注册项",
              names.count("kg") == 1
              and not any(n.startswith("kg-") for n in names), names)

        # ---- 13. 跨项目接力：归到别的项目后必须**当场归类**，不能只塞进 inbox ----
        # 早先 intake 遇到「属于别的项目」只把记录写进目标 inbox 就返回，指望「那个项目
        # 自己的守护」分类；但守护是全局单例、只认一个状态根，本机还停着。结果这条
        # 需求永远停在「待路由」，既不在任何看板上也没人分类 —— 路由只做了一半。
        relaybase = base / "relay"
        relaybase.mkdir()
        ra = git_init(make_state_root(relaybase, "gamma"))
        rb = git_init(make_state_root(relaybase, "delta"))
        pr.register("gamma", ra, path=home / "projects.json")
        pr.register("delta", rb, path=home / "projects.json")
        p = run_kgctl(ra, relaybase / "alpha-trunk", "intake",
                      "delta项目的登录页排版坏了")
        out = p.stdout + p.stderr
        beta_inbox = (rb / "ops" / "tasks" / "inbox.jsonl").read_text(
            encoding="utf-8").splitlines()
        beta_rows = [json.loads(l) for l in beta_inbox if l.strip()]
        check("归到别的项目时，目标 inbox 被标成已归类（routed=true）",
              len(beta_rows) == 1 and beta_rows[0].get("routed") is True, beta_rows)
        beta_ledger = (rb / "ops" / "truth" / "ledger.jsonl")
        led = [json.loads(l) for l in beta_ledger.read_text(encoding="utf-8").splitlines()
               if l.strip()] if beta_ledger.exists() else []
        check("归到别的项目时，目标问题台账真的新增了一条",
              len(led) == 1 and led[0].get("id"), led)
        check("跨项目接力不许把记录写进当前绑定项目的账本",
              not (ra / "ops" / "tasks" / "inbox.jsonl").read_text(
                  encoding="utf-8").strip(), out[:300])

        # ---- 14. 重名项目不许静默挑第一个 ----
        # 本机真实存在两个仓库都叫 codex-autopilot（一个在 ~/.newmax/workspace，
        # 一个在 ~/Documents/Codex）。显式指派时若取第一个命中，等于把活派去改另一个
        # 仓库 —— 必须停下来让调用方看清是哪一个。
        dupbase = base / "dup"
        dupbase.mkdir()
        d1 = git_init(make_state_root(dupbase, "same1"))
        d2 = git_init(make_state_root(dupbase, "same2"))
        pr.register("dup", d1, path=home / "projects.json")
        pr.register("dup", d2, path=home / "projects.json")
        why = []
        got = pr.resolve_hint("dup", pr.load_registry(home / "projects.json"),
                              detail=why)
        check("重名项目做显式指派 → 不返回任何一个（不许挑第一个）",
              got is None and why, (got, why))
        p = run_kgctl(ra, relaybase / "alpha-trunk", "inbox", "指派给重名项目",
                      extra_env={"KG_INBOX_PROJECT": "dup"})
        out = p.stdout + p.stderr
        check("重名项目做显式指派 → 停在待路由箱，不写任何项目账本",
              "没写进任何项目账本" in out, out[:300])

        # ---- 15. 落点预览：只读；默认落点明说；判不清不硬给项目 ----
        # 面板每敲一个字都会预演，所以预演既不能偷偷写注册表，也不能和真实提交
        # 说两套话。这里锁住三种真实分支：点名 → resolved、单项目无线索 → 明说
        # 默认落点、多项目无线索 → 停在「判不清」，和 cmd_inbox 的落账规则一致。
        one_project_registry = base / "preview-one-project.json"
        both_projects_registry = base / "preview-both-projects.json"
        alpha_rows = [dict(r) for r in pr.load_registry(home / "projects.json")
                      if r.get("name") == "alpha"]
        pr.save_registry(alpha_rows, one_project_registry)
        pr.save_registry(registry, both_projects_registry)
        reg_before = both_projects_registry.read_text(encoding="utf-8")
        alpha_before = (alpha / "ops" / "tasks" / "inbox.jsonl").read_text(
            encoding="utf-8")
        beta_before = (beta / "ops" / "tasks" / "inbox.jsonl").read_text(
            encoding="utf-8")

        def preview_json(proc):
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            return json.loads(lines[-1])

        p = run_kgctl(alpha, base / "stateA-trunk", "route-preview",
                      "beta项目要改缓存",
                      extra_env={"KG_PROJECTS_REGISTRY": str(both_projects_registry)})
        got = preview_json(p)
        check("预览点名 beta → 解析到 beta",
              got.get("decision") == "resolved" and got.get("project") == "beta", got)
        check("预览不写注册表",
              both_projects_registry.read_text(encoding="utf-8") == reg_before)
        check("预览不写任何项目账本",
              (alpha / "ops" / "tasks" / "inbox.jsonl").read_text(
                  encoding="utf-8") == alpha_before
              and (beta / "ops" / "tasks" / "inbox.jsonl").read_text(
                  encoding="utf-8") == beta_before)

        p = run_kgctl(alpha, base / "stateA-trunk", "route-preview",
                      "随口说一句",
                      extra_env={"KG_PROJECTS_REGISTRY": str(one_project_registry)})
        got = preview_json(p)
        check("单项目无线索 → 明说默认落到当前绑定项目",
              got.get("decision") == "none", got)
        check("单项目无线索 → 默认落点名与真实提交一致",
              got.get("project") == alpha.name, (got, alpha.name))
        check("单项目无线索 → 理由里明说默认落账",
              "默认" in "".join(got.get("reasons") or []), got)

        p = run_kgctl(alpha, base / "stateA-trunk", "route-preview",
                      "把按钮往左挪 8 像素",
                      extra_env={"KG_PROJECTS_REGISTRY": str(both_projects_registry)})
        got = preview_json(p)
        check("多项目无线索 → 停在判不清，不硬塞默认项目",
              got.get("decision") == "ambiguous" and not got.get("project"), got)
    finally:
        shutil.rmtree(base, ignore_errors=True)
        os.environ.pop("KG_AUTOPILOT_HOME", None)

    print()
    if failures:
        print(f"FAILED {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("selftest ok: 归属判定分层有序，说不清时绝不写进任何项目账本。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
