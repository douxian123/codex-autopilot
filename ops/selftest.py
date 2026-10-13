#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动挡的最小安全检查。

覆盖一个曾经真实发生的坏路径：convergence 分支把未解冲突保留在提交里，
它和 main 本身没有新的合并冲突，于是被误判成 CLEAN，守护进程可能直接把它推上 main。
这里用临时仓库锁住这道门禁：坏分支不能入队，也不能 land。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
KGCTL = OPS / "kgctl.py"


def run(cmd, cwd=None, env=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(
            f"命令失败: {' '.join(map(str, cmd))}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    return p


def git(repo, *args):
    return run(["git", "-C", str(repo), *args])


def kgctl(repo, trunk, *args, extra_env=None):
    env = os.environ.copy()
    env["KG_REPO"] = str(repo)
    env["KG_TRUNK"] = str(trunk)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    return run([sys.executable, str(KGCTL), *args], cwd=str(OPS.parent), env=env, check=False)


def main():
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-selftest-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        repo.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "decisions.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "acceptance.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")

        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "app.txt").write_text("main\n", encoding="utf-8")
        git(repo, "add", "app.txt")
        git(repo, "commit", "-q", "-m", "init")
        # port --go 会 _align_main()，需要一个 origin/main 才能跑；给 fixture 配一个裸远端，
        # 否则测试本身会因为「没有 origin」而失败，测不到目标行为。
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")

        git(repo, "checkout", "-q", "-b", "codex/good")
        (repo / "good.txt").write_text("clean\n", encoding="utf-8")
        git(repo, "add", "good.txt")
        git(repo, "commit", "-q", "-m", "clean branch")

        git(repo, "checkout", "-q", "main")
        git(repo, "checkout", "-q", "-b", "codex/bad")
        (repo / "bad.txt").write_text(
            "<<<<<<< HEAD\nleft\n=======\nright\n>>>>>>> branch\n", encoding="utf-8")
        git(repo, "add", "bad.txt")
        git(repo, "commit", "-q", "-m", "unresolved marker branch")

        q = kgctl(repo, trunk, "queue")
        if "codex/good" not in q.stdout or "codex/bad" in q.stdout:
            raise AssertionError(f"queue 没有正确排除坏分支:\n{q.stdout}\n{q.stderr}")
        if "残留冲突标记" not in q.stdout:
            raise AssertionError(f"queue 没有报告残留冲突标记:\n{q.stdout}")

        land = kgctl(repo, trunk, "land", "codex/bad", "--dry")
        if land.returncode != 0 or "拒绝自动 land" not in land.stdout:
            raise AssertionError(f"land 没有拒绝坏分支:\n{land.stdout}\n{land.stderr}")

        good = kgctl(repo, trunk, "land", "codex/good", "--dry")
        if good.returncode != 0 or "无冲突" not in good.stdout:
            raise AssertionError(f"干净分支 dry-run 失败:\n{good.stdout}\n{good.stderr}")

        # 回归：分支提交的等价内容已在 main，git cherry 因 patch-id 不同仍报 +。
        # port --go 必须自决：无论判 already 还是 conflict，都不能留下 port 分支/worktree，
        # 也不能把源分支或 main 弄脏。锁住「失败即整体回滚」这条底线。
        git(repo, "checkout", "-q", "main")
        doc_first = git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "doc.txt").write_text("L1\nL2\n", encoding="utf-8")
        git(repo, "add", "doc.txt")
        git(repo, "commit", "-q", "-m", "main adds superset doc")
        # 从 doc.txt 存在之前的 main 拉分支，只加 main 已有内容的子集：patch-id 不同，
        # 但 cherry-pick 到当前 main 是空操作。
        git(repo, "checkout", "-q", "-b", "codex/dup", doc_first)
        (repo / "doc.txt").write_text("L1\n", encoding="utf-8")
        git(repo, "add", "doc.txt")
        git(repo, "commit", "-q", "-m", "backup: doc subset already on main")
        git(repo, "checkout", "-q", "main")
        git(repo, "push", "-q", "origin", "main")
        port_dup = kgctl(repo, trunk, "port", "codex/dup", "--go")
        if "未落提交 1 条" not in port_dup.stdout:
            raise AssertionError(f"fixture 没造出 patch-id 不匹配的未落提交:\n{port_dup.stdout}")
        if "PORT_RESULT" not in (port_dup.stdout + port_dup.stderr):
            raise AssertionError(f"port 没有给出可判定的 PORT_RESULT:\n{port_dup.stdout}\n{port_dup.stderr}")
        if "outcome=conflict" in (port_dup.stdout + port_dup.stderr) and "已回滚" not in port_dup.stdout:
            raise AssertionError(f"conflict 未整体回滚:\n{port_dup.stdout}")
        leftover = [q.name for q in root.iterdir()
                    if q.is_dir() and "-port-" in q.name]
        if leftover:
            raise AssertionError(f"already 判定未清理 port worktree: {leftover}")


        # 回归：冲突路径落在被 ignore 的目录里。目录整体被 .gitignore 命中、但文件是
        # tracked，此时 `git add -- <全部未合并>` 会因该路径返回非零。旧实现把它当致命
        # 错误抛出，把 worktree 留在 MERGE_HEAD 半合并；收敛分支已登记，之后每次 converge
        # 都“已存在则跳过”，worktree 永久半合并。这里锁住：converge 必须收敛完成，
        # 且不能留下任何 MERGE_HEAD，二次运行还要幂等。
        git(repo, "checkout", "-q", "main")
        (repo / ".gitignore").write_text("gen/\n", encoding="utf-8")
        (repo / "common.txt").write_text("x\n", encoding="utf-8")
        (repo / "gen").mkdir()
        (repo / "gen" / "f.json").write_text("v0\n", encoding="utf-8")
        git(repo, "add", ".gitignore", "common.txt")
        git(repo, "add", "-f", "gen/f.json")
        git(repo, "commit", "-q", "-m", "ignored-dir fixture on main")

        git(repo, "checkout", "-q", "-b", "codex/ig-a")
        (repo / "gen" / "f.json").write_text("vA\n", encoding="utf-8")
        (repo / "common.txt").write_text("A\n", encoding="utf-8")
        git(repo, "commit", "-qam", "A")

        git(repo, "checkout", "-q", "main")
        git(repo, "checkout", "-q", "-b", "codex/ig-b")
        (repo / "gen" / "f.json").write_text("vB\n", encoding="utf-8")
        (repo / "common.txt").write_text("B\n", encoding="utf-8")
        git(repo, "commit", "-qam", "B")

        # main 必须往前走，两支才会与 main 各自冲突（否则只是各自延长 main，合起来是干净的）。
        git(repo, "checkout", "-q", "main")
        (repo / "gen" / "f.json").write_text("vM\n", encoding="utf-8")
        (repo / "common.txt").write_text("M\n", encoding="utf-8")
        git(repo, "commit", "-qam", "main advances")

        for attempt in (1, 2):
            conv = kgctl(repo, trunk, "converge", "--go")
            if conv.returncode != 0:
                raise AssertionError(
                    f"converge 第 {attempt} 次失败:\n{conv.stdout}\n{conv.stderr}")
            if "没有冲突分支" in conv.stdout:
                raise AssertionError(f"fixture 没造出冲突分支:\n{conv.stdout}")
            converge_wts = [q for q in root.iterdir()
                            if q.is_dir() and "-converge-" in q.name]
            if not converge_wts:
                raise AssertionError(f"converge 没有建出收敛 worktree:\n{conv.stdout}")
            for wt in converge_wts:
                gd = run(["git", "-C", str(wt), "rev-parse", "--git-dir"]).stdout.strip()
                gdp = Path(gd) if os.path.isabs(gd) else (wt / gd)
                if (gdp / "MERGE_HEAD").exists():
                    raise AssertionError(
                        f"converge 第 {attempt} 次后仍残留半合并（MERGE_HEAD）: {wt}")

        # 回归：收拢分支不能被当成源分支再喂回收拢。
        # 触发条件是 main 前进：旧的收拢分支基于旧 main，main 一动它就重新与 main 冲突，
        # 于是被 CONFLICT 扫描当成源分支捞回来。成员集自我膨胀、id 漂移、旧 worktree 变
        # 孤儿、永久半合并。这一整套都锁在这里。
        import json as _json
        cj = repo / "ops" / "tasks" / "convergence.json"
        data = _json.loads(cj.read_text(encoding="utf-8"))
        if not data.get("groups"):
            raise AssertionError("converge 应登记至少一个收拢组")
        first_ids = {g["id"] for g in data["groups"]}
        before_wts = {q.name for q in root.iterdir() if q.is_dir() and "-converge-" in q.name}

        # main 前进，复现真实漂移：旧收拢分支重新与 main 冲突。
        git(repo, "checkout", "-q", "main")
        (repo / "common.txt").write_text("M2\n", encoding="utf-8")
        (repo / "gen" / "f.json").write_text("vM2\n", encoding="utf-8")
        git(repo, "commit", "-qam", "main advances again")

        conv = kgctl(repo, trunk, "converge", "--go")
        if conv.returncode != 0:
            raise AssertionError(f"漂移后 converge 失败:\n{conv.stdout}\n{conv.stderr}")
        if "没有冲突分支" in conv.stdout:
            raise AssertionError(f"漂移后 fixture 没造出冲突:\n{conv.stdout}")
        matrix_cmd = kgctl(repo, trunk, "matrix")
        if matrix_cmd.returncode != 0 or "codex/converge-" in matrix_cmd.stdout:
            raise AssertionError(f"收拢分支仍被计入 matrix / 一屏:\n{matrix_cmd.stdout}")
        queue_cmd = kgctl(repo, trunk, "queue")
        if queue_cmd.returncode != 0 or "codex/converge-" in queue_cmd.stdout:
            raise AssertionError(f"收拢分支仍被计入 queue:\n{queue_cmd.stdout}")

        after_wts = {q.name for q in root.iterdir() if q.is_dir() and "-converge-" in q.name}
        if after_wts != before_wts:
            raise AssertionError(
                f"漂移后凭空多出收拢 worktree：before={before_wts} after={after_wts}")
        data2 = _json.loads(cj.read_text(encoding="utf-8"))
        now_ids = {g["id"] for g in data2["groups"]}
        if now_ids != first_ids:
            raise AssertionError(f"漂移后收拢 id 未粘住：{first_ids} -> {now_ids}")
        for g in data2["groups"]:
            for src in g.get("branches", []):
                if "/converge-" in src:
                    raise AssertionError(f"收拢分支被当成源分支写入组：{src}")
        matrix = _json.loads((repo / "ops" / "tasks" / "matrix.json").read_text(encoding="utf-8"))
        if any("/converge-" in r.get("branch", "") for r in matrix):
            raise AssertionError("收拢分支仍被写进 matrix / 状态扫描")
        for wt in [q for q in root.iterdir() if q.is_dir() and "-converge-" in q.name]:
            gd = run(["git", "-C", str(wt), "rev-parse", "--git-dir"]).stdout.strip()
            gdp = Path(gd) if os.path.isabs(gd) else (wt / gd)
            if (gdp / "MERGE_HEAD").exists():
                raise AssertionError(f"漂移收敛后残留半合并: {wt}")

        # 回归：孤儿收拢记录（id 已不再被算出来）里卡着的半合并必须被清掉，不能永久残留。
        orphan_branch = "codex/converge-deadbeef00"
        owt = root / "state-converge-deadbeef00"
        git(repo, "branch", orphan_branch, "main")
        git(repo, "worktree", "add", "-q", str(owt), orphan_branch)
        run(["git", "-C", str(owt), "merge", "--no-commit", "--no-ff", "codex/ig-a"], check=False)
        ogd = run(["git", "-C", str(owt), "rev-parse", "--git-dir"]).stdout.strip()
        ogdp = Path(ogd) if os.path.isabs(ogd) else (owt / ogd)
        if not (ogdp / "MERGE_HEAD").exists():
            raise AssertionError("fixture 没造出孤儿半合并")
        data3 = _json.loads(cj.read_text(encoding="utf-8"))
        data3["groups"].append({
            "id": "deadbeef00", "branch": orphan_branch, "worktree": str(owt),
            "branches": ["codex/ig-a"], "files": ["common.txt"], "status": "needs_resolution",
        })
        cj.write_text(_json.dumps(data3, ensure_ascii=False), encoding="utf-8")
        kgctl(repo, trunk, "converge", "--go")
        if (ogdp / "MERGE_HEAD").exists():
            raise AssertionError("孤儿收拢的残留半合并没有被清掉")

    print("selftest ok: 干净分支可入队；带冲突标记的分支被 queue 排除且 land 拒绝。")
    print("selftest ok: 冲突路径落在被 ignore 目录时，converge 不残留半合并且可重跑。")
    print("selftest ok: 收拢分支不被当源分支喂回收拢；main 漂移后 id 粘住；孤儿收拢的半合并被清掉。")


def test_replan():
    """replan 把一个真实的 needs_resolution 收拢组解散：标 replanned、删 worktree/分支，
    且后续 converge 不许把它重建。这是「过时大杂烩不再反复复活」的核心保证。"""
    import json as _json
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-replan-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        repo.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for f in ("ledger", "progress", "decisions", "acceptance"):
            (repo / "ops" / "truth" / f"{f}.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "doc.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "doc.txt")
        git(repo, "commit", "-q", "-m", "init")
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        # 两支抢同一个文件，各写不同内容，形成真实冲突族。
        for name, val in (("codex/rp-a", "A"), ("codex/rp-b", "B")):
            git(repo, "checkout", "-q", "-b", name, base)
            (repo / "doc.txt").write_text(val + "\n", encoding="utf-8")
            git(repo, "commit", "-qam", name)
        git(repo, "checkout", "-q", "main")
        (repo / "doc.txt").write_text("M\n", encoding="utf-8")
        git(repo, "commit", "-qam", "main advances")
        git(repo, "push", "-q", "origin", "main")

        conv = kgctl(repo, trunk, "converge", "--go")
        if "已收拢" not in conv.stdout:
            raise AssertionError(f"fixture 没造出可收拢的冲突组:\n{conv.stdout}")
        cj = repo / "ops" / "tasks" / "convergence.json"
        groups = _json.loads(cj.read_text(encoding="utf-8"))["groups"]
        grp = next(g for g in groups if g.get("status") == "needs_resolution")
        gid, gbr, gwt = grp["id"], grp["branch"], Path(grp["worktree"])

        rp = kgctl(repo, trunk, "replan", "--go")
        if rp.returncode != 0:
            raise AssertionError(f"replan --go 失败:\n{rp.stdout}\n{rp.stderr}")
        rec = next(g for g in _json.loads(cj.read_text(encoding="utf-8"))["groups"]
                   if g["id"] == gid)
        if rec.get("status") != "replanned":
            raise AssertionError(f"replan 未把组标为 replanned: {rec.get('status')}")
        if gwt.exists():
            raise AssertionError("replan 未清理超级合并 worktree")
        if run(["git", "-C", str(repo), "rev-parse", "--verify", gbr],
               check=False).returncode == 0:
            raise AssertionError("replan 未删除超级合并分支")

        # 关键：成员仍在冲突，converge 会重新算出同 id 的组，但必须跳过、不重建。
        conv2 = kgctl(repo, trunk, "converge", "--go")
        if "已 replan" not in conv2.stdout:
            raise AssertionError(f"converge 没有跳过已 replan 的组:\n{conv2.stdout}")
        if run(["git", "-C", str(repo), "rev-parse", "--verify", gbr],
               check=False).returncode == 0:
            raise AssertionError("已 replan 的组被 converge 重建了")
    print("selftest ok: replan 解散超级合并组后不复活，worktree/分支被清理。")


def test_resolve():
    """resolve 把一条真冲突分支拆成独立、可验收的单任务。

    锁住四件事：
      * 预演不建树；`--go` 才在 origin/main 上开 resolve worktree，把源分支合进来，
        非确定性冲突保留 marker 交给 agent，main 与源分支都不动；
      * `--finish` 走的就是 land 的同一道门禁：解干净才落盘并回收 worktree；
      * 拆出来的容器分支（codex/resolve-*）不许被 scan/matrix/queue 当成源分支回收；
      * append-only 的 .jsonl 冲突走确定性行级并集，直接落盘、两端记录都保住。
    """
    import json as _json
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-resolve-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        repo.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for f in ("ledger", "progress", "decisions", "acceptance"):
            (repo / "ops" / "truth" / f"{f}.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
        # 编排逻辑的测试不测前端构建：关掉门禁，否则 land 会去找不存在的 frontend。
        (repo / "ops" / "central.config.json").write_text(
            _json.dumps({"build": {"cmd": "", "cwd": "."}}), encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "doc.txt").write_text("base\n", encoding="utf-8")
        (repo / "data.jsonl").write_text("base\n", encoding="utf-8")
        git(repo, "add", "doc.txt", "data.jsonl")
        git(repo, "commit", "-q", "-m", "init")
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        # 一条抢 doc.txt 的真冲突分支：改同一文件的不同内容，语义冲突必须人/agent 解。
        git(repo, "checkout", "-q", "-b", "codex/light", base)
        (repo / "doc.txt").write_text("light\n", encoding="utf-8")
        git(repo, "commit", "-qam", "codex/light")
        git(repo, "checkout", "-q", "main")
        (repo / "doc.txt").write_text("mainline\n", encoding="utf-8")
        git(repo, "commit", "-qam", "main advances")
        git(repo, "push", "-q", "origin", "main")

        main_before = git(repo, "rev-parse", "main").stdout.strip()
        light_before = git(repo, "rev-parse", "codex/light").stdout.strip()

        plan = kgctl(repo, trunk, "resolve", "--auto")
        if plan.returncode != 0 or "--go" not in plan.stdout:
            raise AssertionError(f"resolve 预演没给出 --go 提示:\n{plan.stdout}\n{plan.stderr}")
        if any(q.is_dir() and "-resolve-" in q.name for q in root.iterdir()):
            raise AssertionError("resolve 预演就建了 worktree")

        go = kgctl(repo, trunk, "resolve", "--auto", "--go")
        if "outcome=needs_agent" not in (go.stdout + go.stderr):
            raise AssertionError(f"resolve --go 没给出 needs_agent:\n{go.stdout}\n{go.stderr}")
        wt = root / "state-resolve-light"
        if not wt.is_dir():
            raise AssertionError(f"resolve 没有建出 worktree {wt}:\n{go.stdout}")
        if git(repo, "rev-parse", "main").stdout.strip() != main_before:
            raise AssertionError("resolve 把 main 弄动了")
        if git(repo, "rev-parse", "codex/light").stdout.strip() != light_before:
            raise AssertionError("resolve 把源分支弄动了")
        rec = next(t for t in _json.loads((repo / "ops" / "tasks" / "resolve.json").read_text(
            encoding="utf-8"))["tasks"] if t["slug"] == "light")
        if rec.get("status") != "needs_agent" or rec.get("branch") != "codex/light":
            raise AssertionError(f"resolve 任务登记不对: {rec}")

        # 容器分支不许被当成源分支回收：matrix / queue / 状态扫描都不该出现它。
        matrix = kgctl(repo, trunk, "matrix")
        if "codex/resolve-" in matrix.stdout:
            raise AssertionError(f"matrix 把 resolve 容器当源分支:\n{matrix.stdout}")
        queue = kgctl(repo, trunk, "queue")
        if "codex/resolve-" in queue.stdout:
            raise AssertionError(f"queue 把 resolve 容器当源分支:\n{queue.stdout}")
        rows = _json.loads((repo / "ops" / "tasks" / "matrix.json").read_text(encoding="utf-8"))
        if any("/resolve-" in r.get("branch", "") for r in rows):
            raise AssertionError("resolve 容器被写进 matrix / 状态扫描")

        # 已在解的源分支不会被重复拆任务（此时没有别的候选）。
        again = kgctl(repo, trunk, "resolve", "--auto", "--go")
        if "outcome=needs_agent" in (again.stdout + again.stderr):
            raise AssertionError(f"resolve 重复拆了同一源分支:\n{again.stdout}")

        # agent 解掉 marker、显式提交，再 finish。
        (wt / "doc.txt").write_text("light\nmainline\n", encoding="utf-8")
        git(wt, "add", "doc.txt")
        git(wt, "commit", "-q", "-m", "resolve: keep both intents in doc.txt")

        finish = kgctl(repo, trunk, "resolve", "--finish", "light", "--go")
        if "outcome=landed" not in (finish.stdout + finish.stderr):
            raise AssertionError(f"resolve --finish 没有落盘:\n{finish.stdout}\n{finish.stderr}")
        if git(repo, "rev-parse", "main").stdout.strip() == main_before:
            raise AssertionError("resolve --finish 没有推进 main")
        if wt.exists():
            raise AssertionError("resolve --finish 没有回收 worktree")
        if git(repo, "rev-parse", "codex/light").stdout.strip() != light_before:
            raise AssertionError("落盘动了源分支")
        landed_doc = git(repo, "show", "main:doc.txt").stdout
        if "light" not in landed_doc or "mainline" not in landed_doc:
            raise AssertionError(f"落盘内容不对: {landed_doc!r}")
        rec = next(t for t in _json.loads((repo / "ops" / "tasks" / "resolve.json").read_text(
            encoding="utf-8"))["tasks"] if t["slug"] == "light")
        if rec.get("status") != "landed":
            raise AssertionError(f"落盘后任务未标 landed: {rec.get('status')}")

        # 历史残留：同一源分支早期留下的 needs_agent 与后面的 landed 并存。已落盘的分支
        # 不能再被当成开放任务、也不能被自动重拆。
        data = _json.loads((repo / "ops" / "tasks" / "resolve.json").read_text(encoding="utf-8"))
        data["tasks"].append({"slug": "light-stale", "branch": "codex/light",
                              "resolve_branch": "codex/resolve-light", "worktree": str(wt),
                              "status": "needs_agent", "updated": "2000-01-01 00:00:00"})
        (repo / "ops" / "tasks" / "resolve.json").write_text(
            _json.dumps(data, ensure_ascii=False), encoding="utf-8")
        listed = kgctl(repo, trunk, "resolve", "--list")
        if "light-stale" in listed.stdout or "[needs_agent] light " in listed.stdout:
            raise AssertionError(f"已落盘分支仍被列成开放任务:\n{listed.stdout}")
        # codex/light 已进 main（不再是冲突分支），landed 记录应直接挡住重拆。
        landed_again = kgctl(repo, trunk, "resolve", "codex/light", "--go")
        if "已经 resolve 落盘过" not in landed_again.stdout:
            raise AssertionError(f"landed 的源分支没被挡住重拆:\n{landed_again.stdout}")

        # --abandon：清容器、源分支原样不动。用一次性 worktree 造分支，避免动状态根检出
        # （land 用 update-ref 推进 main，状态根工作区会与 HEAD 脱节，不该再 checkout）。
        mkw = root / "mk-second"
        git(repo, "worktree", "add", "-q", "-b", "codex/second", str(mkw), base)
        (mkw / "doc.txt").write_text("second\n", encoding="utf-8")
        git(mkw, "add", "doc.txt")
        git(mkw, "commit", "-q", "-m", "codex/second")
        git(repo, "worktree", "remove", "--force", str(mkw))
        go2 = kgctl(repo, trunk, "resolve", "codex/second", "--go")
        if "outcome=needs_agent" not in (go2.stdout + go2.stderr):
            raise AssertionError(f"resolve codex/second 没建出任务:\n{go2.stdout}\n{go2.stderr}")
        wt2 = root / "state-resolve-second"
        if not wt2.is_dir():
            raise AssertionError("第二条 resolve 没建出 worktree")
        second_before = git(repo, "rev-parse", "codex/second").stdout.strip()
        ab = kgctl(repo, trunk, "resolve", "--abandon", "second", "--go")
        if ab.returncode != 0 or "已放弃" not in ab.stdout:
            raise AssertionError(f"resolve --abandon 失败:\n{ab.stdout}\n{ab.stderr}")
        if wt2.exists():
            raise AssertionError("resolve --abandon 没清 worktree")
        if run(["git", "-C", str(repo), "rev-parse", "--verify", "codex/resolve-second"],
               check=False).returncode == 0:
            raise AssertionError("resolve --abandon 没删容器分支")
        if git(repo, "rev-parse", "codex/second").stdout.strip() != second_before:
            raise AssertionError("resolve --abandon 动了源分支")

        # append-only 的 .jsonl 冲突：确定性行级并集，直接落盘、两端记录都保住。
        mka = root / "mk-append"
        git(repo, "worktree", "add", "-q", "-b", "codex/append", str(mka), "main")
        (mka / "data.jsonl").write_text("base\nappend\n", encoding="utf-8")
        git(mka, "add", "data.jsonl")
        git(mka, "commit", "-q", "-m", "codex/append")
        git(repo, "worktree", "remove", "--force", str(mka))
        # 让 main 也改 data.jsonl，才构成真冲突（否则 append 只是快进）。
        mkm = root / "mk-main"
        git(repo, "worktree", "add", "-q", "--detach", str(mkm), "main")
        (mkm / "data.jsonl").write_text("base\nmainline\n", encoding="utf-8")
        git(mkm, "add", "data.jsonl")
        git(mkm, "commit", "-q", "-m", "main edits data.jsonl")
        new_main = git(mkm, "rev-parse", "HEAD").stdout.strip()
        git(repo, "worktree", "remove", "--force", str(mkm))
        git(repo, "update-ref", "refs/heads/main", new_main)
        git(repo, "push", "-q", "origin", "main")
        ap = kgctl(repo, trunk, "resolve", "codex/append", "--go")
        if "outcome=landed" not in (ap.stdout + ap.stderr):
            raise AssertionError(f"append-only JSONL 没自动并集落盘:\n{ap.stdout}\n{ap.stderr}")
        merged = git(repo, "show", "main:data.jsonl").stdout
        if "append" not in merged or "mainline" not in merged:
            raise AssertionError(f"JSONL 行级并集丢了记录: {merged!r}")

        # KG_RESOLVE_SKIP：守护进程用它跳过「拆不动 / 门禁没过」的分支，不许反复拆同一条。
        mks = root / "mk-skip"
        git(repo, "worktree", "add", "-q", "-b", "codex/skipme", str(mks), base)
        (mks / "doc.txt").write_text("skipme\n", encoding="utf-8")
        git(mks, "add", "doc.txt")
        git(mks, "commit", "-q", "-m", "codex/skipme")
        git(repo, "worktree", "remove", "--force", str(mks))
        skipped = kgctl(repo, trunk, "resolve", "--auto", "--go",
                        extra_env={"KG_RESOLVE_SKIP": "codex/skipme"})
        if "RESOLVE_RESULT branch=codex/skipme" in (skipped.stdout + skipped.stderr):
            raise AssertionError(f"KG_RESOLVE_SKIP 没跳过指定分支:\n{skipped.stdout}")
        if (root / "state-resolve-skipme").exists():
            raise AssertionError("KG_RESOLVE_SKIP 还是建了 worktree")

    print("selftest ok: resolve 拆冲突为单任务、保留 marker、走门禁落盘并回收；容器不被当源分支。")
    print("selftest ok: resolve --abandon 清容器且源分支不动；append-only JSONL 确定性并集落盘。")
    print("selftest ok: KG_RESOLVE_SKIP 让守护进程跳过已判定的冲突分支，不重复拆。")


def test_reset_trunk_truth_conflict():
    """锁住 reset_trunk_head 的回归：失败 land 在主干工作树留下「纯真相层」提交后，
    origin/main 又前进了同一批真相文件（别的 land 更新了 PROJECT_STATUS / inbox.jsonl）。

    旧实现直接在主干工作树 `git merge origin/main`，一冲突就 `sys.exit(1)`：于是这一次
    落盘、以及之后每一次落盘，都被上一次失败留下的真相提交永久锁死。这里要求下一次
    land 必须能把真相层并到 origin/main 之上（JSONL 行级并集、生成视图以远端为底），
    并正常推进 main——只要出现「已放弃」或没有「main 推进」就算回归。
    """
    import json as _json
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-trunk-reset-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        repo.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "docs").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for f in ("ledger", "progress", "decisions", "acceptance"):
            (repo / "ops" / "truth" / f"{f}.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
        (repo / "docs" / "PROJECT_STATUS.md").write_text("# status\n\nbase\n", encoding="utf-8")
        # 编排逻辑不测前端构建：关掉门禁，否则 land 会去找不存在的 frontend。
        (repo / "ops" / "central.config.json").write_text(
            _json.dumps({"build": {"cmd": "", "cwd": "."}}), encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        # 主干工作树留下一条「纯真相层」提交，模拟上一次 land 失败的残留。
        git(repo, "worktree", "add", "-q", "--detach", str(trunk), "main")
        (trunk / "docs" / "PROJECT_STATUS.md").write_text("# status\n\ntrunk-residue\n",
                                                          encoding="utf-8")
        (trunk / "ops" / "tasks" / "inbox.jsonl").write_text(
            '{"id":"trunk-residue"}\n', encoding="utf-8")
        git(trunk, "add", ".")
        git(trunk, "commit", "-q", "-m", "chore(truth): failed land residue")

        # origin/main 前进：改了同一批真相文件的不同内容，构成真相层冲突。
        (repo / "docs" / "PROJECT_STATUS.md").write_text("# status\n\norigin\n", encoding="utf-8")
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text(
            '{"id":"origin-line"}\n', encoding="utf-8")
        git(repo, "commit", "-qam", "advance origin truth")
        git(repo, "push", "-q", "origin", "main")

        # 一条只剩干净代码、和真相层无关的分支等着落盘。
        mkc = root / "mk-clean"
        git(repo, "worktree", "add", "-q", "-b", "codex/clean", str(mkc), base)
        (mkc / "clean.txt").write_text("clean\n", encoding="utf-8")
        git(mkc, "add", "clean.txt")
        git(mkc, "commit", "-q", "-m", "codex/clean")
        git(repo, "worktree", "remove", "--force", str(mkc))

        main_before = git(repo, "rev-parse", "main").stdout.strip()
        landed = kgctl(repo, trunk, "land", "codex/clean")
        out = landed.stdout + landed.stderr
        if landed.returncode != 0 or "已放弃" in out:
            raise AssertionError(f"真相层残留把后续 land 锁死了:\n{out}")
        if "main 推进" not in out:
            raise AssertionError(f"land 没有推进 main:\n{out}")
        if git(repo, "rev-parse", "main").stdout.strip() == main_before:
            raise AssertionError("land 报成功但 main 引用没动")
        if git(repo, "show", "main:clean.txt").stdout.strip() != "clean":
            raise AssertionError("干净分支的代码没进 main")
        status_md = git(repo, "show", "main:docs/PROJECT_STATUS.md").stdout
        if "trunk-residue" in status_md:
            raise AssertionError(f"真相层残留没被远端覆盖: {status_md!r}")

    print("selftest ok: 失败 land 留下的真相层残留不再锁死后续落盘，JSONL 并集、main 正常推进。")


if __name__ == "__main__":
    main()
    test_replan()
    test_resolve()
    test_reset_trunk_truth_conflict()
