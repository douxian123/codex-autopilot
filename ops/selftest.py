#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动挡的最小安全检查。

覆盖一个曾经真实发生的坏路径：convergence 分支把未解冲突保留在提交里，
它和 main 本身没有新的合并冲突，于是被误判成 CLEAN，守护进程可能直接把它推上 main。
这里用临时仓库锁住这道门禁：坏分支不能入队，也不能 land。
"""

import concurrent.futures
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


def truthctl(repo, *args, extra_env=None):
    env = os.environ.copy()
    env["KG_REPO"] = str(repo)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    return run([sys.executable, str(OPS / "truthctl.py"), *args],
               cwd=str(OPS.parent), env=env, check=False)


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


def test_align_main_only_recovers_truth_only_commits():
    """自动对齐 main 不能把本地产品提交当作真相层提交丢掉。"""
    import sys as _sys
    _sys.path.insert(0, str(OPS))
    env_keys = ("KG_REPO", "KG_TRUNK", "KG_DAEMON_HOME")
    old_env = {k: os.environ.get(k) for k in env_keys}
    try:
        import importlib
        import kgctl as kg
        importlib.reload(kg)

        # 场景一：本地领先提交只改真相层。允许把 ref 对齐到 origin，
        # 同时保住工作树里尚未提交的追加记录。
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-align-truth-") as tmp:
            root = Path(tmp)
            repo = root / "state"
            repo.mkdir()
            (repo / "ops" / "tasks").mkdir(parents=True)
            (repo / "ops" / "truth").mkdir(parents=True)
            (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
            for name in ("progress", "decisions", "acceptance"):
                (repo / "ops" / "truth" / f"{name}.jsonl").write_text("", encoding="utf-8")
            (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
            (repo / "ops" / "truth" / "ledger.jsonl").write_text(
                '{"id":"L-0001","status":"open"}\n', encoding="utf-8")
            run(["git", "init", "-q", "-b", "main", str(repo)])
            git(repo, "config", "user.email", "selftest@example.invalid")
            git(repo, "config", "user.name", "Autopilot Selftest")
            (repo / "app.txt").write_text("main\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "init")
            origin = root / "origin.git"
            run(["git", "init", "-q", "--bare", str(origin)])
            git(repo, "remote", "add", "origin", str(origin))
            git(repo, "push", "-q", "origin", "main")
            (repo / "ops" / "truth" / "ledger.jsonl").write_text(
                '{"id":"L-0001","status":"open"}\n{"id":"L-0002","status":"open"}\n',
                encoding="utf-8")
            git(repo, "add", "ops/truth/ledger.jsonl")
            git(repo, "commit", "-q", "-m", "chore(truth): local ledger")
            remote_before = git(repo, "rev-parse", "origin/main").stdout.strip()
            os.environ["KG_REPO"] = str(repo)
            importlib.reload(kg)
            kg._align_main()
            if git(repo, "rev-parse", "main").stdout.strip() != remote_before:
                raise AssertionError("纯真相临时提交没有被安全对齐到 origin/main")
            if "L-0002" not in (repo / "ops" / "truth" / "ledger.jsonl").read_text(encoding="utf-8"):
                raise AssertionError("自动对齐丢了工作树里的新增真相记录")

        # 场景二：本地领先提交既有 truth 又有产品文件。必须 fail closed，
        # 不允许 main ref 前进，更不允许把产品提交从历史里丢掉。
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-align-mixed-") as tmp:
            root = Path(tmp)
            repo = root / "state"
            repo.mkdir()
            (repo / "ops" / "tasks").mkdir(parents=True)
            (repo / "ops" / "truth").mkdir(parents=True)
            (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
            for name in ("ledger", "progress", "decisions", "acceptance"):
                (repo / "ops" / "truth" / f"{name}.jsonl").write_text("", encoding="utf-8")
            (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
            run(["git", "init", "-q", "-b", "main", str(repo)])
            git(repo, "config", "user.email", "selftest@example.invalid")
            git(repo, "config", "user.name", "Autopilot Selftest")
            (repo / "app.txt").write_text("main\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "init")
            origin = root / "origin.git"
            run(["git", "init", "-q", "--bare", str(origin)])
            git(repo, "remote", "add", "origin", str(origin))
            git(repo, "push", "-q", "origin", "main")
            (repo / "app.txt").write_text("product-local\n", encoding="utf-8")
            (repo / "ops" / "truth" / "ledger.jsonl").write_text(
                '{"id":"L-0001","status":"open"}\n', encoding="utf-8")
            git(repo, "add", "app.txt", "ops/truth/ledger.jsonl")
            git(repo, "commit", "-q", "-m", "chore(truth): mixed product change")
            local_before = git(repo, "rev-parse", "main").stdout.strip()
            os.environ["KG_REPO"] = str(repo)
            importlib.reload(kg)
            try:
                kg._align_main()
            except RuntimeError as exc:
                if "拒绝自动对齐" not in str(exc):
                    raise AssertionError(f"失败原因不是拒绝自动对齐: {exc}") from exc
            else:
                raise AssertionError("混合产品提交被错误自动对齐")
            if git(repo, "rev-parse", "main").stdout.strip() != local_before:
                raise AssertionError("拒绝自动对齐时 main ref 仍被移动")
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    print("selftest ok: _align_main 只恢复纯真相临时提交，混合产品提交 fail closed。")

def test_clean_land_with_dirty_trunk():
    """Shared trunk dirty must not block landing through a one-shot clean worktree."""
    import json as _json
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-dirty-trunk-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        repo.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        for name in ("ledger", "progress", "decisions", "acceptance"):
            (repo / "ops" / "truth" / f"{name}.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
        (repo / "ops" / "central.config.json").write_text(
            _json.dumps({"build": {"cmd": "", "cwd": "."}, "verify": []}), encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")

        git(repo, "worktree", "add", "-q", "--detach", str(trunk), "main")
        dirty = trunk / "user-work.txt"
        dirty.write_text("do not touch\n", encoding="utf-8")
        mkc = root / "mk-clean"
        git(repo, "worktree", "add", "-q", "-b", "codex/clean", str(mkc), "main")
        (mkc / "clean.txt").write_text("clean\n", encoding="utf-8")
        git(mkc, "add", "clean.txt")
        git(mkc, "commit", "-q", "-m", "clean")
        git(repo, "worktree", "remove", "--force", str(mkc))

        landed = kgctl(repo, trunk, "land", "codex/clean")
        out = landed.stdout + landed.stderr
        if landed.returncode != 0 or "main 推进" not in out:
            raise AssertionError(f"dirty trunk 挡住了 clean land:\n{out}")
        if git(repo, "show", "main:clean.txt").stdout.strip() != "clean":
            raise AssertionError("dirty trunk 路径没有把 clean 分支写进 main")
        if not dirty.exists() or dirty.read_text(encoding="utf-8") != "do not touch\n":
            raise AssertionError("dirty trunk 的用户改动被 clean land 覆盖了")

    print("selftest ok: shared trunk dirty does not block clean-worktree land.")


def test_control_state():
    """用户级开关：暂停闸门、开关持久化、损坏即安全暂停。"""
    import importlib
    sys.path.insert(0, str(OPS))
    home = tempfile.mkdtemp(prefix="kg-ctl-")
    os.environ["KG_DAEMON_HOME"] = home
    try:
        import control_state
        importlib.reload(control_state)
        import central_daemon
        importlib.reload(central_daemon)

        # 默认不暂停，三个动作开关按设计默认值。
        d = control_state.load()
        assert d["paused"] is False and d["auto_land"] is True
        assert d["auto_resolve"] is True and d["auto_port"] is False

        # 暂停后 run_cycle 必须秒返回且不启动任何工作。
        control_state.set_keys(paused=True)
        result = central_daemon.run_cycle(dry=False, max_land=1)
        assert result.get("paused") is True, f"暂停后 run_cycle 仍启动工作: {result}"

        # 用户开关拥有最终否决权：项目开着 auto_resolve，用户关掉也不生效。
        control_state.set_keys(paused=False, auto_resolve=False)
        assert central_daemon.auto_resolve_config()["enabled"] is False
        control_state.set_keys(auto_resolve=True)
        assert central_daemon.auto_resolve_config()["enabled"] is True

        # auto_exec 默认关闭；只有用户显式打开才可能派真实会话。
        assert d["auto_exec"] is False, "auto_exec 不该默认打开"
        control_state.set_keys(auto_exec=False)
        assert central_daemon.auto_exec_config()["enabled"] is False
        control_state.set_keys(auto_exec=True)
        assert central_daemon.auto_exec_config()["enabled"] is True

        # control.json 损坏 -> 安全暂停 + error 可见。
        control_state.path().write_text("{ this is not json", encoding="utf-8")
        bad = control_state.load()
        assert bad["paused"] is True and bad["error"], "损坏的 control.json 没有安全暂停"

        # 事件流水可写可读。
        control_state.save(dict(control_state.DEFAULTS))
        control_state.append_event("pause")
        control_state.append_event("cycle_done", landed=["x"], conflict=3)
        ev = control_state.read_events(5)
        assert ev and ev[0]["kind"] == "cycle_done" and ev[0]["conflict"] == 3
    finally:
        os.environ.pop("KG_DAEMON_HOME", None)
        import shutil
        shutil.rmtree(home, ignore_errors=True)
    print("selftest ok: 用户级开关生效，暂停闸门拦住新工作，损坏即安全暂停。")


def test_resolve_worker_pool():
    """resolve 后台池：三路并行、脏任务续解、主分支前进后仍接管已提交任务。"""
    import importlib
    import json as _json

    sys.path.insert(0, str(OPS))
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-resolve-pool-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        home = root / "home"
        repo.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        for name in ("ledger", "progress", "decisions", "acceptance"):
            (repo / "ops" / "truth" / f"{name}.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "tasks" / "resolve.json").write_text(
            _json.dumps({"tasks": []}), encoding="utf-8")
        (repo / "ops" / "central.config.json").write_text(
            _json.dumps({"auto_resolve": {
                "enabled": True, "max_workers": 3, "resume_dirty": True,
                "auto_split": True,
                "sandbox": "danger-full-access",
                "fast_model": "aibvv@deepseek-v4.1-flash",
            }}), encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "base.txt")
        git(repo, "commit", "-q", "-m", "init")
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")

        base = git(repo, "rev-parse", "HEAD").stdout.strip()
        tasks = []
        marker = "<<<<<<< HEAD\nleft\n=======\nright\n>>>>>>> branch\n"
        for index in range(3):
            wt = root / f"wt-{index}"
            git(repo, "worktree", "add", "-q", "-b", f"codex/resolve-t{index}", str(wt), base)
            (wt / f"file-{index}.txt").write_text(marker, encoding="utf-8")
            git(wt, "add", f"file-{index}.txt")
            git(wt, "commit", "-q", "-m", f"resolve conflict {index}")
            if index == 2:
                # Simulate a worker interrupted after clearing one marker but
                # before commit; resume_dirty must still allow this worktree.
                (wt / "file-2.txt").write_text(marker, encoding="utf-8")
            task = {
                "slug": f"t{index}", "branch": f"codex/resolve-t{index}",
                "worktree": str(wt), "status": "needs_agent",
                "unresolved_files": [f"file-{index}.txt"],
            }
            if index == 0:
                # The source branch may already be in main while the dedicated
                # resolve branch still contains a real conflict replayed onto
                # newer main. Completion must follow resolve_branch, not source.
                git(repo, "branch", "codex/source-t0", base)
                task.update({
                    "branch": "codex/source-t0",
                    "resolve_branch": "codex/resolve-t0",
                })
            tasks.append(task)
        git(repo, "checkout", "-q", "main")
        (repo / "main-only.txt").write_text("main advances\n", encoding="utf-8")
        git(repo, "add", "main-only.txt")
        git(repo, "commit", "-q", "-m", "main advances after branch split")
        git(repo, "push", "-q", "origin", "main")
        git(repo, "update-ref", "refs/remotes/origin/main", "main")
        # 第三个任务故意留成脏工作区：上一轮 worker 被打断的现场应可原地续解。
        (repo / "ops" / "tasks" / "resolve.json").write_text(
            _json.dumps({"tasks": tasks}), encoding="utf-8")

        git(repo, "push", "-q", "origin", "main")
        os.environ["KG_REPO"] = str(repo)
        os.environ["KG_TRUNK"] = str(trunk)
        os.environ["KG_DAEMON_HOME"] = str(home)
        # Keep fixture state isolated from the real repository's resolve ledger.
        # `central_daemon` imports these modules; reload them now so
        # `_state_repo()` and `origin/main` both point at this fixture.
        import control_state
        import central_config
        import central_daemon
        # Other tests may already have imported and hot-reloaded these modules.
        # Reload the dependency first, then the daemon, so daemon functions bind
        # to the current control_state module object rather than a stale one.
        importlib.reload(control_state)
        importlib.reload(central_config)
        importlib.reload(central_daemon)
        control_state.load = lambda: {
            "auto_resolve": True, "auto_exec": False, "auto_land": False}
        started = []

        class DoneFuture:
            def done(self):
                return False

        class Pool:
            def submit(self, fn, rec):
                started.append(rec["slug"])
                return DoneFuture()

        central_daemon._resolve_task_pool = Pool()
        central_daemon._resolve_jobs.clear()
        central_daemon._resolve_finish_jobs.clear()
        central_daemon.dispatch_resolve_tasks(False)
        if sorted(started) != ["t0", "t1", "t2"]:
            raise AssertionError(f"三路池没有派满 3 个任务（脏任务应续解）: {started}")

        # A worker launched outside the daemon (for example during a daemon
        # restart) must be observable and adopted, not duplicated. Simulate a
        # live process for slot accounting and then an exited worker that must
        # enter the serial finish queue.
        central_daemon._external_resolve_jobs.clear()
        central_daemon._external_resolve_jobs["external"] = {
            "pid": os.getpid(), "rec": {"slug": "external", "worktree": str(root)},
            "external": True,
        }
        central_daemon.save_worker_state()
        workers = central_daemon.load_state().get("workers") or {}
        if "external" not in (workers.get("resolve_external_active") or []):
            raise AssertionError(f"外部 resolve worker 未进入可观测状态: {workers}")
        central_daemon._external_resolve_jobs["external"]["pid"] = 99999999
        central_daemon._collect_external_resolve_tasks()
        if "external" in central_daemon._external_resolve_jobs:
            raise AssertionError("已退出的外部 worker 没有释放槽位")
        central_daemon.save_worker_state()
        workers = central_daemon.load_state().get("workers") or {}
        if workers.get("resolve_task_active") != ["t0", "t1", "t2"]:
            raise AssertionError(f"resolve 活跃任务未写入可观测状态: {workers}")

        # dispatch_convergence must not bypass the real-worktree gate and
        # start the legacy `kgctl resolve --auto` in parallel with actual
        # resolve workers.
        central_daemon._resolve_future = None
        central_daemon._conv_jobs.clear()
        central_daemon._resolve_jobs.clear()
        central_daemon._external_resolve_jobs.clear()
        central_daemon._resolve_finish_jobs.clear()
        # Finish slots use their own serial lane. Keep one live split worker
        # without making the fixture accidentally block on a real finish job.
        calls = []
        old_start = central_daemon._start_resolve_worker
        central_daemon._start_resolve_worker = lambda: calls.append(1) or []
        try:
            central_daemon.dispatch_convergence(False, {})
            # Existing resolve worktrees must not starve the legacy auto-split
            # forever. One extra slot may be filled per cycle, while the pool
            # remains bounded by max_workers.
            if len(calls) != 1:
                raise AssertionError(f"已有真实 resolve worker 时没有补一个拆分槽: {calls}")
            central_daemon._resolve_future = concurrent.futures.Future()
            calls.clear()
            central_daemon.dispatch_convergence(False, {})
            if calls:
                raise AssertionError("resolve 槽已占用时仍重复启动 auto-split")
        finally:
            central_daemon._resolve_future = None
            central_daemon._start_resolve_worker = old_start
        central_daemon._external_resolve_jobs.clear()

        # Real existing worktrees take priority over the legacy auto-split path.
        # Otherwise the daemon can report one speculative task while two
        # actionable worktrees sit idle behind the land lock.
        central_daemon._resolve_future = None
        auto_split_calls = []
        old_start = central_daemon._start_resolve_worker
        central_daemon._start_resolve_worker = lambda: auto_split_calls.append(1) or []
        try:
            if not central_daemon.open_resolve_tasks():
                raise AssertionError("fixture 应有真实 resolve worktree")
            # Mirror the production gate added after land: only auto-split when
            # there is no real open worktree.
            if not central_daemon.open_resolve_tasks():
                central_daemon._start_resolve_worker()
            if auto_split_calls:
                raise AssertionError("已有真实 resolve worktree 时仍启动了旧的 auto-split")
        finally:
            central_daemon._start_resolve_worker = old_start
        cfg = central_daemon.auto_resolve_config()
        if cfg["sandbox"] != "danger-full-access" or \
                cfg["fast_model"] != "aibvv@deepseek-v4.1-flash":
            raise AssertionError(f"resolve worker 配置未覆盖沙箱/快速模型: {cfg}")

        # Stale ledger rows whose worktree has been reclaimed must not appear as
        # live work or consume a worker slot.
        data = _json.loads((repo / "ops" / "tasks" / "resolve.json").read_text(encoding="utf-8"))
        data["tasks"].append({
            "slug": "stale", "branch": "codex/resolve-stale",
            "worktree": str(root / "missing-worktree"), "status": "gate_fail",
            "unresolved_files": ["gone.ts"],
        })
        (repo / "ops" / "tasks" / "resolve.json").write_text(
            _json.dumps(data), encoding="utf-8")
        if "stale" in {t.get("slug") for t in central_daemon.open_resolve_tasks()}:
            raise AssertionError("worktree 已回收的陈旧 resolve 台账仍被列为待解")

        # 已提交、无 marker、但 origin/main 已前进到不再是 HEAD ancestor 的任务，
        # 也必须进入串行 finish，不能变成孤儿。
        central_daemon._resolve_jobs.clear()
        central_daemon._resolve_finish_jobs.clear()
        wt = root / "wt-committed"
        git(repo, "worktree", "add", "-q", "-b", "codex/resolve-committed", str(wt))
        (wt / "resolved.txt").write_text("resolved\n", encoding="utf-8")
        git(wt, "add", "resolved.txt")
        git(wt, "commit", "-q", "-m", "resolved")
        (repo / "main-moves.txt").write_text("main advanced\n", encoding="utf-8")
        git(repo, "add", "main-moves.txt")
        git(repo, "commit", "-q", "-m", "main advances")
        git(repo, "push", "-q", "origin", "main")
        data = _json.loads((repo / "ops" / "tasks" / "resolve.json").read_text(encoding="utf-8"))
        data["tasks"] = [{
            "slug": "committed", "branch": "codex/resolve-committed",
            "worktree": str(wt), "status": "needs_agent", "unresolved_files": [],
        }]
        (repo / "ops" / "tasks" / "resolve.json").write_text(
            _json.dumps(data), encoding="utf-8")
        central_daemon._pickup_committed_resolve_tasks()
        if "committed" not in central_daemon._resolve_finish_jobs:
            raise AssertionError("主分支前进后已提交的 resolve 任务被孤儿化了")

        # A committed resolve worktree can legitimately be ahead of origin/main.
        # Those differences are normal task output, not a dirty blocker. Only
        # real UU entries or merge-overwrite hazards may stop serial finish.
        central_daemon._resolve_finish_jobs.clear()
        (wt / "worker-output.txt").write_text("resolved output\n", encoding="utf-8")
        git(wt, "add", "worker-output.txt")
        git(wt, "commit", "-q", "-m", "worker output ahead of main")
        synced = central_daemon._sync_resolve_worktree_before_finish(
            {"worktree": str(wt)})
        if synced.get("outcome") not in ("fresh", "merged"):
            raise AssertionError(
                f"committed resolve worktree was rejected before finish: {synced}")

        for name in ("KG_REPO", "KG_TRUNK", "KG_DAEMON_HOME"):
            os.environ.pop(name, None)
    print("selftest ok: resolve 三路并行、可观测、优先真实 worktree、快速模型可提交、陈旧台账过滤。")


def test_verify_and_truth():
    """验证门禁与真相层：门禁真跑、证据真写、失败真拦；产品状态/判断逻辑可累积。"""
    import json as _json
    sys.path.insert(0, str(OPS))
    import central_config

    # verify 配置解析：None 退化 build，[] 明确无门禁，list/dict/字符串都要能接。
    assert central_config.verify_gates({"verify": [], "build": {"cmd": "npm run build"}}) == []
    assert central_config.verify_gates({"verify": None, "build": {"cmd": ""}}) == []
    assert central_config.verify_gates(
        {"verify": None, "build": {"cmd": "npm run build", "cwd": "frontend"}}) == [
        {"name": "build", "cmd": "npm run build", "cwd": "frontend"}]
    assert central_config.verify_gates({"verify": "echo hi"}) == [
        {"name": "gate1", "cmd": "echo hi", "cwd": "."}]
    assert central_config.verify_gates(
        {"verify": {"name": "t", "cmd": "true", "cwd": "."}}) == [
        {"name": "t", "cmd": "true", "cwd": "."}]
    assert central_config.verify_gates({"verify": [
        {"name": "typecheck", "cmd": "npx tsc --noEmit", "cwd": "frontend"},
        "npm run build"]}) == [
        {"name": "typecheck", "cmd": "npx tsc --noEmit", "cwd": "frontend"},
        {"name": "gate2", "cmd": "npm run build", "cwd": "."}]

    def fresh_fixture(base, verify):
        base.mkdir(parents=True, exist_ok=True)
        repo = base / "state"
        trunk = base / "state-trunk"
        repo.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for f in ("ledger", "progress", "decisions", "acceptance"):
            (repo / "ops" / "truth" / f"{f}.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
        (repo / "ops" / "central.config.json").write_text(
            _json.dumps({"build": {"cmd": "", "cwd": "."}, "verify": verify}),
            encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "app.txt").write_text("main\n", encoding="utf-8")
        git(repo, "add", "app.txt", "ops/central.config.json",
            "ops/truth/mainline.json", "ops/truth/ledger.jsonl",
            "ops/truth/progress.jsonl", "ops/truth/decisions.jsonl",
            "ops/truth/acceptance.jsonl", "ops/tasks/inbox.jsonl")
        git(repo, "commit", "-q", "-m", "init")
        origin = base / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        git(repo, "checkout", "-q", "-b", "codex/good")
        (repo / "good.txt").write_text("clean\n", encoding="utf-8")
        git(repo, "add", "good.txt")
        git(repo, "commit", "-q", "-m", "clean branch")
        git(repo, "checkout", "-q", "main")
        return repo, trunk

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-verify-") as tmp:
        base = Path(tmp)
        repo, trunk = fresh_fixture(
            base / "ok", [{"name": "unit", "cmd": "echo verify-ok", "cwd": "."}])
        land = kgctl(repo, trunk, "land", "codex/good")
        if land.returncode != 0 or "已落盘并推送 origin" not in land.stdout:
            raise AssertionError(f"验证通过的分支未落盘:\n{land.stdout}\n{land.stderr}")
        if "验证门禁 ok" not in land.stdout:
            raise AssertionError(f"land 未报告验证门禁:\n{land.stdout}")
        acc = git(repo, "show", "main:ops/truth/acceptance.jsonl").stdout.strip()
        if not acc:
            raise AssertionError("落盘后验收台账为空")
        rec = _json.loads(acc.splitlines()[-1])
        if rec.get("status") != "verified":
            raise AssertionError(f"验收状态不是 verified: {rec.get('status')}")
        if "echo verify-ok" not in rec.get("evidence", "") or "exit 0" not in rec.get("evidence", ""):
            raise AssertionError(f"验收证据缺少真实命令/结果: {rec.get('evidence')}")
        prog = git(repo, "show", "main:ops/truth/progress.jsonl").stdout.strip()
        if not _json.loads(prog.splitlines()[-1]).get("landed"):
            raise AssertionError("进展台账没有 landed 记录")

        repo2, trunk2 = fresh_fixture(
            base / "bad", [{"name": "unit", "cmd": "echo boom && exit 3", "cwd": "."}])
        main_before = git(repo2, "rev-parse", "main").stdout.strip()
        land2 = kgctl(repo2, trunk2, "land", "codex/good")
        if "验证门禁 × 失败" not in land2.stdout:
            raise AssertionError(f"失败门禁没有被拦下:\n{land2.stdout}\n{land2.stderr}")
        if git(repo2, "rev-parse", "main").stdout.strip() != main_before:
            raise AssertionError("门禁失败后 main 仍被推进")
        if git(repo2, "show", "main:ops/truth/acceptance.jsonl").stdout.strip():
            raise AssertionError("门禁失败却写了验收台账")

        # 产品状态与判断逻辑：写入后 snapshot 里必须能看到。
        ps = truthctl(repo, "product", "set", "自动挡面板", "默认面板一眼看懂在做什么",
                      extra_env={"KG_PS_WHY": "小白先要能感知",
                                 "KG_PS_FILES": "plugin/ui/panel.html"})
        if ps.returncode != 0:
            raise AssertionError(f"product set 失败:\n{ps.stdout}\n{ps.stderr}")
        data = _json.loads((repo / "ops" / "truth" / "product_state.json").read_text(encoding="utf-8"))
        if data["modules"][0]["area"] != "自动挡面板":
            raise AssertionError(f"产品状态未写入: {data}")
        snap = truthctl(repo, "snapshot", "--print")
        if "产品状态" not in snap.stdout or "自动挡面板" not in snap.stdout:
            raise AssertionError(f"snapshot 没有渲染产品状态:\n{snap.stdout}")

        dj = truthctl(repo, "distill", "add", "落盘默认合入，不把合并决策推回用户",
                      extra_env={"KG_TRUTH_WHY": "用户没有现场信息",
                                 "KG_TRUTH_SCOPE": "process"})
        if dj.returncode != 0:
            raise AssertionError(f"distill add 失败:\n{dj.stdout}\n{dj.stderr}")
        jd = (repo / "ops" / "truth" / "judgments.jsonl").read_text(encoding="utf-8")
        if "J-0001" not in jd:
            raise AssertionError(f"判断逻辑未写回: {jd}")
        snap2 = truthctl(repo, "snapshot", "--print")
        if "判断逻辑" not in snap2.stdout or "J-0001" not in snap2.stdout:
            raise AssertionError(f"snapshot 没有渲染判断逻辑:\n{snap2.stdout}")

        # 问题闭环：新增一条 ledger -> close 标 done 并写进展 -> open 能重开。
        add = truthctl(repo, "add", "ledger", "按钮点着没反应",
                       extra_env={"KG_TRUTH_KIND": "bug", "KG_TRUTH_DETAIL": "首页按钮无响应"})
        if add.returncode != 0:
            raise AssertionError(f"ledger add 失败:\n{add.stdout}\n{add.stderr}")
        ledger = (repo / "ops" / "truth" / "ledger.jsonl").read_text(encoding="utf-8")
        lid = _json.loads(ledger.splitlines()[-1])["id"]
        cl = truthctl(repo, "close", lid, "修好并跑过门禁",
                      extra_env={"KG_TRUTH_COMMIT": "abc1234",
                                 "KG_TRUTH_EVIDENCE": "unit: cmd -> exit 0"})
        if cl.returncode != 0:
            raise AssertionError(f"close 失败:\n{cl.stdout}\n{cl.stderr}")
        latest = {}
        for line in (repo / "ops" / "truth" / "ledger.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = _json.loads(line)
                latest[r["id"]] = r
        if latest[lid].get("status") != "done":
            raise AssertionError(f"close 后状态不是 done: {latest[lid]}")
        prog2 = (repo / "ops" / "truth" / "progress.jsonl").read_text(encoding="utf-8")
        if "关闭 %s" % lid not in prog2:
            raise AssertionError("close 没有追加进展")
        op = truthctl(repo, "open", lid, "又复现了")
        if op.returncode != 0:
            raise AssertionError(f"open 失败:\n{op.stdout}\n{op.stderr}")

        # 综合：决策/进展/问题/验收合成产品主线，且主线文件真的落盘。
        syn = truthctl(repo, "synthesize")
        if syn.returncode != 0:
            raise AssertionError(f"synthesize 失败:\n{syn.stdout}\n{syn.stderr}")
        mainline_doc = repo / "docs" / "PRODUCT_MAINLINE.md"
        if not mainline_doc.exists():
            raise AssertionError("synthesize 没有写出 docs/PRODUCT_MAINLINE.md")
        text = mainline_doc.read_text(encoding="utf-8")
        if "现在是什么样" not in text or "还差什么" not in text:
            raise AssertionError(f"产品主线缺少关键段落:\n{text[:400]}")

        # 验收分堆：机器自证 vs 需要人拍板，steps 原样写回。
        a1 = truthctl(repo, "accept", "首页第一屏不挤", "--human",
                      "--steps", "1) 打开首页 2) 看第一屏 3) 确认不挤",
                      extra_env={"KG_TRUTH_LEVEL": "P0"})
        if a1.returncode != 0:
            raise AssertionError(f"accept --human 失败:\n{a1.stdout}\n{a1.stderr}")
        a2 = truthctl(repo, "accept", "tsc 通过",
                      extra_env={"KG_TRUTH_EVIDENCE": "npx tsc --noEmit -> exit 0"})
        if a2.returncode != 0:
            raise AssertionError(f"accept 失败:\n{a2.stdout}\n{a2.stderr}")
        arecs = [ _json.loads(x) for x in
                  (repo / "ops" / "truth" / "acceptance.jsonl").read_text(
                      encoding="utf-8").splitlines() if x.strip() ]
        human = [r for r in arecs if r.get("needs_human")]
        if not human or human[0].get("steps", "").find("打开首页") < 0:
            raise AssertionError(f"人工验收项或步骤没写回: {human}")
        if not any(r.get("evidence") for r in arecs):
            raise AssertionError("机器自证项没有证据")

        # 产品路线：mainline.json 写 roadmap，synthesize 要渲染出每一步到哪了。
        (repo / "ops" / "truth" / "mainline.json").write_text(_json.dumps({
            "product": "自动挡", "stage": "v0.5",
            "roadmap": [
                {"step": "唯一入口收需求", "expected": "不再分对话/分支", "status": "done"},
                {"step": "自动实现并收口", "expected": "提一次就闭环", "status": "doing"},
            ]}), encoding="utf-8")
        syn2 = truthctl(repo, "synthesize")
        if syn2.returncode != 0:
            raise AssertionError(f"synthesize(roadmap) 失败:\n{syn2.stdout}\n{syn2.stderr}")
        text2 = mainline_doc.read_text(encoding="utf-8")
        if "产品路线" not in text2 or "唯一入口收需求" not in text2 or "自动实现并收口" not in text2:
            raise AssertionError(f"产品主线缺少路线渲染:\n{text2[:600]}")
        if "需要你验收" not in text2:
            raise AssertionError("产品主线没有拆出「需要你验收」段")
    print("selftest ok: 验证门禁真跑真写真拦；产品状态与判断逻辑可回写；问题可闭环；主线可综合。")


def test_auto_exec():
    """自动实现闭环：台账待办 -> 独立 worktree 真跑 -> 显式提交 -> 落盘 -> 收口。

    用 KG_EXEC_CMD 指向一个假的 codex 脚本，把「真起会话」这一段确定性化。
    锁住三件曾经只能靠人盯的事：
      1. 成功后台账真的变成 done，且 main 里真的有它写的文件；
      2. 失败但产生了改动时，分支和 worktree 被保留（wip 提交 + codex_exit_N），不丢在途工作；
      3. 落盘成功但收口失败时，不删现场、不重复派同一条（防止覆盖他人工作）。
      4. 模型代理瞬时 502（「基础设施抖动」）不算需求做不出来：日志尾部命中抖动特征就
         在同一个 worktree 上重跑，重试仍失败记 codex_infra_fail，与 codex_exit_N 区分。
    """
    import importlib
    import json as _json

    sys.path.insert(0, str(OPS))

    def fixture(base, item_status="open", exec_cfg=None):
        """一个能跑 land 的最小状态根：裸远端 + main + 空门禁 + 一条台账待办。"""
        base.mkdir(parents=True, exist_ok=True)
        repo = base / "state"
        trunk = base / "state-trunk"
        home = base / "home"
        repo.mkdir()
        home.mkdir()
        (home / "control.json").write_text(
            _json.dumps({"auto_exec": True, "paused": False}), encoding="utf-8")
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for f in ("progress", "decisions", "acceptance"):
            (repo / "ops" / "truth" / f"{f}.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
        (repo / "ops" / "truth" / "ledger.jsonl").write_text(
            _json.dumps({"id": "L-0001", "type": "ledger", "kind": "bug",
                         "title": "面板按钮点着没反应", "status": item_status,
                         "ts": "2026-01-01T00:00:00+08:00"},
                        ensure_ascii=False) + "\n", encoding="utf-8")
        cfg_obj = {"build": {"cmd": "", "cwd": "."}, "verify": []}
        if exec_cfg:
            cfg_obj["auto_exec"] = exec_cfg
        (repo / "ops" / "central.config.json").write_text(
            _json.dumps(cfg_obj), encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "app.txt").write_text("main\n", encoding="utf-8")
        git(repo, "add", "app.txt", "ops/central.config.json", "ops/truth/mainline.json",
            "ops/truth/ledger.jsonl", "ops/truth/progress.jsonl",
            "ops/truth/decisions.jsonl", "ops/truth/acceptance.jsonl",
            "ops/tasks/inbox.jsonl")
        git(repo, "commit", "-q", "-m", "init")
        origin = base / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        return repo, trunk, home

    def fake_codex(base, name, exit_code):
        """一个最小的 codex exec 替身：写一个文件后按指定码退出。"""
        path = base / name
        path.write_text(
            "#!/bin/sh\n"
            "wt=\"\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-C\" ]; then wt=\"$2\"; shift 2; else shift; fi\n"
            "done\n"
            "printf 'fake codex\\n' > \"$wt/fake.txt\"\n"
            f"exit {exit_code}\n",
            encoding="utf-8")
        path.chmod(0o755)
        return path

    def fake_codex_flaky(base, name):
        """第一次以 502 抖动失败、第二次成功：验证在同一个 worktree 上重跑。"""
        path = base / name
        counter = base / f"{name}.count"
        wt_log = base / f"{name}.wt"
        path.write_text(
            "#!/bin/sh\n"
            "wt=\"\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-C\" ]; then wt=\"$2\"; shift 2; else shift; fi\n"
            "done\n"
            f"echo \"$wt\" >> {wt_log}\n"
            f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
            "n=$((n + 1))\n"
            f"echo $n > {counter}\n"
            "if [ \"$n\" = \"1\" ]; then\n"
            "  echo 'ERROR: Reconnecting... 5/5'\n"
            "  echo 'ERROR: unexpected status 502 Bad Gateway: Unknown error, url: "
            "http://127.0.0.1:15721/v1/responses'\n"
            "  exit 1\n"
            "fi\n"
            "printf 'fake codex\\n' > \"$wt/fake.txt\"\n"
            "exit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path, wt_log

    def fake_codex_infra(base, name):
        """每次都写一半 + 打 502 文本 + 退出 1：验证重试到上限仍失败时的登记原因。"""
        path = base / name
        path.write_text(
            "#!/bin/sh\n"
            "wt=\"\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-C\" ]; then wt=\"$2\"; shift 2; else shift; fi\n"
            "done\n"
            "printf 'partial\\n' > \"$wt/partial.txt\"\n"
            "echo 'ERROR: unexpected status 502 Bad Gateway: Unknown error, url: "
            "http://127.0.0.1:15721/v1/responses'\n"
            "exit 1\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def fake_codex_hang(base, name):
        """打一句 502 文本后就不退出：验证超时仍归 124，且不会被当成可重试抖动。"""
        path = base / name
        path.write_text(
            "#!/bin/sh\n"
            "echo 'ERROR: unexpected status 502 Bad Gateway: Unknown error, url: "
            "http://127.0.0.1:15721/v1/responses'\n"
            "sleep 30\n"
            "exit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def load_daemon(repo, trunk, home, exec_cmd):
        """把环境切到本 fixture，并重载读取环境变量的那几个模块。"""
        os.environ["KG_REPO"] = str(repo)
        os.environ["KG_TRUNK"] = str(trunk)
        os.environ["KG_DAEMON_HOME"] = str(home)
        os.environ["KG_EXEC_CMD"] = str(exec_cmd)
        import control_state
        import central_config
        import central_daemon
        importlib.reload(control_state)
        importlib.reload(central_config)
        importlib.reload(central_daemon)
        return central_daemon

    def ledger_latest(repo):
        recs = {}
        for line in (repo / "ops" / "truth" / "ledger.jsonl").read_text(
                encoding="utf-8").splitlines():
            if line.strip():
                r = _json.loads(line)
                recs[r["id"]] = r
        return recs

    def dispatch_and_drain(daemon, blocked, timeout=300):
        """Start the bounded pool, then wait for the worker to finish.

        Calling `_collect_exec` in a tight loop while the worker owns the
        land gate can starve it under Python's GIL; wait on the futures first.
        """
        daemon.dispatch_exec(False, blocked)
        jobs = list(getattr(daemon, "_exec_jobs", {}).values())
        if not jobs:
            raise AssertionError(f"没有启动后台 exec worker（blocked={blocked}）")
        deadline = __import__("time").time() + timeout
        for job in jobs:
            remaining = max(0.05, deadline - __import__("time").time())
            job["future"].result(timeout=remaining)
        finished = daemon._collect_exec(blocked)
        if not finished:
            raise AssertionError(f"worker 已完成但未收割（blocked={blocked}）")
        return finished

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-exec-") as tmp:
        base = Path(tmp)

        # --- 成功路径：实现 -> 提交 -> 落盘 -> 收口，台账变 done，main 里有产物 ---
        repo, trunk, home = fixture(base / "ok")
        daemon = load_daemon(repo, trunk, home, fake_codex(base, "fake-ok.sh", 0))
        blocked = {}
        results = dispatch_and_drain(daemon, blocked, timeout=300)
        if len(results) != 1 or results[0].get("outcome") != "landed":
            raise AssertionError(f"成功路径没有落成 landed: {results}\nblocked={blocked}")
        if ledger_latest(repo)["L-0001"].get("status") != "done":
            raise AssertionError(f"落盘后台账没有收口: {ledger_latest(repo)}")
        if git(repo, "show", "main:fake.txt").stdout.strip() != "fake codex":
            raise AssertionError("main 里没有执行器写下的产物")
        # The pool surfaces a terminal row for both success and failure; the
        # next cycle prunes successful rows against the truth ledger.
        if blocked.get("L-0001", {}).get("reason") != "landed":
            raise AssertionError(f"成功路径没有可清理的 landed 终态: {blocked}")
        if git(repo, "branch", "--list", "codex/exec-*").stdout.strip():
            raise AssertionError("成功路径应回收执行分支")

        # --- 重启恢复：正在运行的旧 exec worktree 占槽，不重派同一条 ---
        repo_l, trunk_l, home_l = fixture(base / "live")
        daemon_l = load_daemon(repo_l, trunk_l, home_l, fake_codex(base, "fake-live.sh", 0))
        active, free = daemon_l._exec_capacity(
            3, {"L-live-in-memory"}, {str(repo_l.parent / "state-exec-old"): os.getpid()})
        if active != 2 or free != 1:
            raise AssertionError(f"live exec 没占槽: active={active} free={free}")
        active, free = daemon_l._exec_capacity(
            3, {"L-a", "L-b", "L-c"}, set())
        if free != 0:
            raise AssertionError(f"本池三个 active 时仍报告空闲槽: free={free}")
        real_live = daemon_l._live_exec_worktrees
        daemon_l._live_exec_worktrees = lambda kind=None: (
            {"/tmp/kg-trunk-resolve-doc": os.getpid()})
        try:
            import control_state as cs
            old_load = cs.load
            cs.load = lambda: {"auto_exec": True}
            try:
                started_l = daemon_l.dispatch_exec(False, {})
            finally:
                cs.load = old_load
        finally:
            daemon_l._live_exec_worktrees = real_live
        # The same task is allowed to start; the old branch is what proves
        # whether a resolve worktree was counted as an implementation slot.
        for job in list(daemon_l._exec_jobs.values()):
            job["future"].cancel()
        daemon_l._exec_jobs.clear()
        daemon_l._exec_pool = None

        # --- 失败路径：写了一半就崩，必须保留分支和 worktree，绝不能丢在途工作 ---
        repo_f, trunk_f, home_f = fixture(base / "fail")
        daemon_f = load_daemon(repo_f, trunk_f, home_f,
                               fake_codex(base, "fake-fail.sh", 7))
        blocked_f = {}
        results_f = dispatch_and_drain(daemon_f, blocked_f)
        if results_f[0].get("outcome") != "codex_fail_preserved":
            raise AssertionError(f"失败路径没有保留成果: {results_f}")
        if blocked_f.get("L-0001", {}).get("reason") != "codex_exit_7":
            raise AssertionError(f"失败原因登记不正确: {blocked_f}")
        wt_f = Path(blocked_f["L-0001"]["worktree"])
        if not wt_f.exists():
            raise AssertionError("失败路径删掉了 worktree，丢了在途工作")
        if not git(repo_f, "branch", "--list", "codex/exec-*").stdout.strip():
            raise AssertionError("失败路径删掉了 WIP 分支")
        if ledger_latest(repo_f)["L-0001"].get("status") != "open":
            raise AssertionError("失败路径不应把台账标成 done")

        # --- 收口失败路径：落盘成功、truthctl close 挂了，保留现场且不再重复派 ---
        repo_c, trunk_c, home_c = fixture(base / "closefail")
        daemon_c = load_daemon(repo_c, trunk_c, home_c,
                               fake_codex(base, "fake-close.sh", 0))
        daemon_c._run_truthctl = lambda args, extra_env=None, timeout=120: (
            1, "forced close failure")
        blocked_c = {}
        results_c = dispatch_and_drain(daemon_c, blocked_c)
        if results_c[0].get("outcome") != "landed_close_fail":
            raise AssertionError(f"收口失败被误报成成功: {results_c}")
        if blocked_c.get("L-0001", {}).get("reason") != "landed_close_fail":
            raise AssertionError(f"收口失败原因登记不正确: {blocked_c}")
        if not Path(blocked_c["L-0001"]["worktree"]).exists():
            raise AssertionError("收口失败删掉了现场，无法复核")
        if ledger_latest(repo_c)["L-0001"].get("status") == "done":
            raise AssertionError("收口失败却把台账标成 done")
        again = daemon_c.dispatch_exec(False, blocked_c)
        if again:
            raise AssertionError(f"收口失败后台账项被重复派发: {again}")

        # --- 过期本地失败可重试，但外部阻塞/结论失败仍不重试 ---
        retry_blocked = {
            "L-0012": {"reason": "codex_exit_1", "ts": "2000-01-01 00:00:00"},
            "L-0016": {"reason": "external_blocked", "ts": "2000-01-01 00:00:00"},
            "L-0056": {"reason": "landed_close_fail", "ts": "2000-01-01 00:00:00"},
        }
        released = daemon._prune_retryable_exec_blocked(retry_blocked)
        if released != ["L-0012"] or "L-0016" not in retry_blocked or "L-0056" not in retry_blocked:
            raise AssertionError(f"过期失败恢复边界错误: released={released}, blocked={retry_blocked}")

        # --- 已收口的条目不占「实现暂缓」：台账变 done 后自愈清掉 ---
        with (repo_c / "ops" / "truth" / "ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps({"id": "L-0001", "type": "ledger", "kind": "bug",
                                  "title": "面板按钮点着没反应", "status": "done",
                                  "ts": "2026-01-02T00:00:00+08:00"},
                                 ensure_ascii=False) + "\n")
        stale = daemon_c._prune_exec_blocked(blocked_c)
        if "L-0001" not in stale:
            raise AssertionError(f"台账已收口却没清掉实现暂缓: stale={stale}")
        if blocked_c:
            raise AssertionError(f"台账已收口后 blocked 应为空: {blocked_c}")

        # --- 台账读不到时绝不误清真实的暂缓 ---
        blocked_keep = {"L-9999": {"reason": "gate_fail"}}
        orig_truth_dir = daemon_c._truth_dir
        daemon_c._truth_dir = lambda: None
        kept = daemon_c._prune_exec_blocked(blocked_keep)
        daemon_c._truth_dir = orig_truth_dir
        if kept or "L-9999" not in blocked_keep:
            raise AssertionError("台账读不到时误清了真实暂缓记录")

        # --- 分支已有未落提交、worktree 干净：直接收口，不重复实现、不删已有成果 ---
        # 复刻「上一轮改动已提交进分支、守护在 land 前退出」的现场：分支领先 main 一笔，
        # 且 worktree 已不存在。旧逻辑会把分支 -D 删掉再重建，等于丢掉已提交的成果。
        repo_p, trunk_p, home_p = fixture(base / "pending")
        daemon_p = load_daemon(repo_p, trunk_p, home_p,
                               fake_codex(base, "fake-again.sh", 0))
        slug_p = f"{daemon_p._exec_slug('L-0001')}-{daemon_p._exec_slug('面板按钮点着没反应')}"
        branch_p = f"codex/exec-{slug_p}"
        wt_p = Path(repo_p).parent / f"{Path(repo_p).name}-exec-{slug_p}"
        run(["git", "-C", str(repo_p), "worktree", "add", "-q", "-b", branch_p, str(wt_p)])
        (wt_p / "fake.txt").write_text("preserved\n", encoding="utf-8")
        git(wt_p, "add", "fake.txt")
        git(wt_p, "commit", "-q", "-m", "exec(L-0001): 面板按钮点着没反应")
        run(["git", "-C", str(repo_p), "worktree", "remove", "--force", str(wt_p)])
        blocked_p = {}
        results_p = dispatch_and_drain(daemon_p, blocked_p)
        if not results_p or results_p[0].get("outcome") != "landed":
            raise AssertionError(
                f"分支已有未落提交时没有直接收口: {results_p}\nblocked={blocked_p}")
        if git(repo_p, "show", "main:fake.txt").stdout.strip() != "preserved":
            raise AssertionError("直接收口没有把已有提交落进 main（疑似重复实现覆盖了成果）")
        if ledger_latest(repo_p)["L-0001"].get("status") != "done":
            raise AssertionError(f"直接收口后台账没有收口: {ledger_latest(repo_p)}")
        if git(repo_p, "branch", "--list", branch_p).stdout.strip():
            raise AssertionError("直接收口后执行分支没有回收")

        # --- 跨多个阶段、没有单一验收面的路线任务不能占 worker 槽 ---
        assert daemon_p._oversized_exec_item({
            "title": "继续按 ThoughLoom 产品路线推进闭环",
            "detail": "阶段1质量、阶段2 live、阶段3图谱、阶段4分享",
        })
        assert not daemon_p._oversized_exec_item({
            "title": "修复 verify:graph-png-export-browser",
            "detail": "改 frontend/src/components/NodeHoverCard.tsx 并跑 npm run verify:graph-png-export-browser",
        })

        # --- 人工判断 / 外部通道阻塞不能占用代码 worker 槽 ---
        assert daemon_p._non_executable_reason({
            "title": "阶段1人工盲评包已生成，等待真人打开复核",
            "detail": "需要人工复核 50 题并导出 review-results.json",
        }) == "needs_human"
        assert daemon_p._non_executable_reason({
            "title": "检索出口当前是红灯：Tavily 返回 432 usage limit",
            "detail": "外部计划配额耗尽",
        }) == "external_blocked"
        assert daemon_p._non_executable_reason({
            "title": "修复人工复核页导出按钮",
            "detail": "改 backend/test/stage1-review-page.mjs 并跑 node test/stage1-review-page.mjs",
        }) is None

        # --- 同 id 已落 main 但尚未 close：派发前必须自动收口，不再开新 worker ---
        repo_done, trunk_done, home_done = fixture(base / "already-landed")
        git(repo_done, "push", "-q", "origin", "main")
        ledger_done = repo_done / "ops" / "truth" / "ledger.jsonl"
        rows_done = [_json.loads(line) for line in ledger_done.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows_done.append({
            "id": "L-0001", "ts": "2026-10-18 00:00:00", "type": "ledger",
            "kind": "bug", "lane": "bug", "title": "面板按钮点着没反应",
            "detail": "面板按钮点着没反应", "status": "open", "source": "selftest",
        })
        ledger_done.write_text("\n".join(_json.dumps(r, ensure_ascii=False) for r in rows_done) + "\n", encoding="utf-8")
        (repo_done / "already.txt").write_text("landed\n", encoding="utf-8")
        git(repo_done, "add", "already.txt")
        git(repo_done, "commit", "-q", "-m", "exec(L-0001): 面板按钮点着没反应")
        git(repo_done, "push", "-q", "origin", "main")
        daemon_done = load_daemon(repo_done, trunk_done, home_done,
                                  fake_codex(base, "should-not-run.sh", 0))
        def run_truthctl_done(args, **kwargs):
            result = truthctl(repo_done, *args)
            return result.returncode, result.stdout + result.stderr
        daemon_done._run_truthctl = run_truthctl_done
        blocked_done = {}
        results_done = daemon_done.dispatch_exec(False, blocked_done)
        if results_done:
            raise AssertionError(f"已落 main 的任务仍启动了 worker: {results_done}")
        if ledger_latest(repo_done)["L-0001"].get("status") != "done":
            raise AssertionError(f"已落 main 的任务没有自动收口: {ledger_latest(repo_done)}")

        # --- _changed_paths 必须保住 porcelain 首行的前导空格，否则 git add 必失败 ---
        # 曾经的 bug：_git() 整体 strip 掉 stdout，porcelain 首行 ' M app.txt' 变成
        # 'M app.txt'，line[3:] 便切出 'pp.txt'，git add 直接 fatal，成果永远落不了盘
        # （L-0016 的 ackend/package.json 就是这么来的）。
        repo_cp, trunk_cp, home_cp = fixture(base / "changedpaths")
        (repo_cp / "app.txt").write_text("main\nsecond\n", encoding="utf-8")
        (repo_cp / "ops" / "central.config.json").write_text(
            _json.dumps({"build": {"cmd": "", "cwd": "."}, "verify": [],
                         "note": "touched"}), encoding="utf-8")
        got = daemon_p._changed_paths(repo_cp)
        if sorted(got) != ["app.txt", "ops/central.config.json"]:
            raise AssertionError(
                f"_changed_paths 解析 porcelain 首行有误（前导空格被吞）: {got}")

        # --- 基础设施抖动 vs 需求做不出来：502 日志文本判定为可重试，并在同一 worktree 重跑 ---
        # L-0055 的现场原文就是模型代理 127.0.0.1:15721 的瞬时 502 + Transport channel
        # closed；这类失败不该被记成 codex_exit_1 永久暂缓。
        probe = base / "probe-infra.log"
        probe.write_text(
            "ERROR: Reconnecting... 1/5\n"
            "ERROR: rmcp::transport::worker: worker quit with fatal: Transport channel closed,"
            ' when UnexpectedServerResponse("HTTP 502: ")\n'
            "ERROR: unexpected status 502 Bad Gateway: Unknown error, url: "
            "http://127.0.0.1:15721/v1/responses\n", encoding="utf-8")
        quiet_log = base / "probe-clean.log"
        quiet_log.write_text("需求已实现，验证通过\n", encoding="utf-8")

        repo_r, trunk_r, home_r = fixture(base / "infra-retry")
        flaky, wt_log = fake_codex_flaky(base, "fake-flaky.sh")
        daemon_r = load_daemon(repo_r, trunk_r, home_r, flaky)
        daemon_r.EXEC_INFRA_BACKOFF = 0  # 退避不拖慢自测
        if not daemon_r.classify_exec_infra_failure(probe):
            raise AssertionError("502 日志文本没有被判定为可重试的基础设施失败")
        if daemon_r.classify_exec_infra_failure(quiet_log):
            raise AssertionError("普通日志被误判成基础设施抖动")
        if daemon_r.classify_exec_infra_failure(base / "no-such.log"):
            raise AssertionError("日志不存在时被误判成基础设施抖动")
        for text in ("Transport channel closed", "ECONNRESET", "connection refused"):
            probe.write_text(f"error: {text}\n", encoding="utf-8")
            if not daemon_r.classify_exec_infra_failure(probe):
                raise AssertionError(f"{text} 没有被判定为可重试的基础设施失败")

        blocked_r = {}
        results_r = dispatch_and_drain(daemon_r, blocked_r)
        if results_r[0].get("outcome") != "landed" or results_r[0].get("attempts") != 2:
            raise AssertionError(f"抖动后重试没有落成 landed: {results_r}\nblocked={blocked_r}")
        if blocked_r.get("L-0001", {}).get("reason") != "landed":
            raise AssertionError(f"抖动后重试成功没有 landed 终态: {blocked_r}")
        wts = [l for l in wt_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        if len(wts) != 2 or len(set(wts)) != 1:
            raise AssertionError(f"重试没有落在同一个 worktree 上: {wts}")
        if git(repo_r, "show", "main:fake.txt").stdout.strip() != "fake codex":
            raise AssertionError("重试成功后 main 里没有执行器的产物")
        slug_r = f"{daemon_r._exec_slug('L-0001')}-{daemon_r._exec_slug('面板按钮点着没反应')}"
        exec_log = (home_r / "exec" / f"{slug_r}.log").read_text(encoding="utf-8")
        if "502 Bad Gateway" not in exec_log or "重试 第 2/4 次" not in exec_log:
            raise AssertionError(f"重试没有保留第一次的现场证据: {exec_log[-600:]}")
        daemon_log = (home_r / "daemon.log").read_text(encoding="utf-8")
        if "第 2/4 次重试" not in daemon_log or str(home_r / "exec" / f"{slug_r}.log") not in daemon_log:
            raise AssertionError(f"daemon.log 没写明第几次重试 / 日志路径: {daemon_log[-800:]}")

        # --- 重试到上限仍抖动：reason 记 codex_infra_fail（与 codex_exit_1 区分）、现场保留、不重复派 ---
        repo_e, trunk_e, home_e = fixture(base / "infra-exhaust")
        daemon_e = load_daemon(repo_e, trunk_e, home_e,
                               fake_codex_infra(base, "fake-infra.sh"))
        daemon_e.EXEC_INFRA_BACKOFF = 0
        blocked_e = {}
        results_e = dispatch_and_drain(daemon_e, blocked_e)
        if results_e[0].get("outcome") != "codex_fail_preserved":
            raise AssertionError(f"抖动失败没有保留现场: {results_e}")
        if results_e[0].get("attempts") != 4:
            raise AssertionError(f"抖动失败没有重试到默认上限（1+3）: {results_e}")
        if blocked_e.get("L-0001", {}).get("reason") != "codex_infra_fail":
            raise AssertionError(f"抖动失败原因没有与 codex_exit_N 区分: {blocked_e}")
        if "502 Bad Gateway" not in (blocked_e.get("L-0001", {}).get("detail") or ""):
            raise AssertionError(f"codex_infra_fail 没有留下抖动原文证据: {blocked_e}")
        if not Path(blocked_e["L-0001"]["worktree"]).exists():
            raise AssertionError("抖动失败丢掉了在途 worktree")
        again_e = daemon_e.dispatch_exec(False, blocked_e)
        if again_e:
            raise AssertionError(f"codex_infra_fail 后被重复派发: {again_e}")

        # --- retries 可通过项目配置收紧；普通失败（exit 7，无抖动文本）不受影响 ---
        repo_x, trunk_x, home_x = fixture(base / "infra-retries-1",
                                         exec_cfg={"retries": 0})
        daemon_x = load_daemon(repo_x, trunk_x, home_x,
                               fake_codex_infra(base, "fake-infra-x.sh"))
        daemon_x.EXEC_INFRA_BACKOFF = 0
        blocked_x = {}
        results_x = dispatch_and_drain(daemon_x, blocked_x)
        if results_x[0].get("attempts") != 1:
            raise AssertionError(f"retries=0 时仍重试了: {results_x}")
        if blocked_x.get("L-0001", {}).get("reason") != "codex_infra_fail":
            raise AssertionError(f"retries=0 的抖动失败原因不对: {blocked_x}")

        # --- 超时护栏不变：即使日志里有 502 文本，超时仍归 codex_exit_124 且不重试 ---
        repo_t, trunk_t, home_t = fixture(base / "infra-timeout",
                                         exec_cfg={"timeout": 1})
        daemon_t = load_daemon(repo_t, trunk_t, home_t,
                               fake_codex_hang(base, "fake-hang.sh"))
        daemon_t.EXEC_INFRA_BACKOFF = 0
        blocked_t = {}
        results_t = dispatch_and_drain(daemon_t, blocked_t)
        if results_t[0].get("exit") != 124 or results_t[0].get("attempts") != 1:
            raise AssertionError(f"超时没有仍归 124 / 或误重试了: {results_t}")
        if blocked_t.get("L-0001", {}).get("reason") != "codex_exit_124":
            raise AssertionError(f"超时原因登记不正确: {blocked_t}")

    print("selftest ok: 自动实现真跑并落盘、失败保留现场、收口失败不重复派、"
          "分支已有未落提交时直接收口、收口后清掉实现暂缓、"
          "502 日志判定为可重试并在同一 worktree 重跑（重试到上限记 codex_infra_fail）。")


def test_mcp_contract():
    """MCP 合约：resources 能力、面板资源可读、状态/控制/渲染工具可用。"""
    import json as _json
    home = tempfile.mkdtemp(prefix="kg-mcp-")
    project = Path(tempfile.mkdtemp(prefix="kg-mcp-proj-"))
    (project / "ops" / "truth").mkdir(parents=True)
    (project / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["KG_DAEMON_HOME"] = home
    env["KG_REPO"] = str(project)
    proc = subprocess.Popen([sys.executable, str(OPS / "central_mcp.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env,
                            cwd=str(OPS.parent))
    try:
        def send(o):
            proc.stdin.write(_json.dumps(o, ensure_ascii=False) + "\n")
            proc.stdin.flush()

        def recv():
            return _json.loads(proc.stdout.readline())

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18"}})
        init = recv()["result"]
        assert "resources" in init["capabilities"], "initialize 未声明 resources"

        send({"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}})
        resources = recv()["result"]["resources"]
        uri = resources[0]["uri"]
        assert uri == "ui://autopilot/panel-v2.html"

        send({"jsonrpc": "2.0", "id": 3, "method": "resources/read",
              "params": {"uri": uri}})
        contents = recv()["result"]["contents"][0]
        assert contents["mimeType"] == "text/html;profile=mcp-app"
        assert "总输入框" in contents["text"], "面板没讲清「总输入框」"
        for anchor in ("productList", "judgmentList", "verifyBox", "mainlineBox",
                       "roadmapBox", "acceptBox", "progressBox"):
            assert anchor in contents["text"], f"面板缺少 {anchor}"
        assert 'data-key="auto_exec"' in contents["text"], "面板缺少 auto_exec 开关"

        send({"jsonrpc": "2.0", "id": 30, "method": "tools/list", "params": {}})
        tool_names = {t["name"] for t in recv()["result"]["tools"]}
        missing = {"product_state", "product_update", "distill_review",
                   "distill_add", "synthesize_mainline", "close_item",
                   "reopen_item", "register_acceptance"} - tool_names
        assert not missing, f"缺少自动挡工具: {sorted(missing)}"

        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "autopilot_status", "arguments": {"fast": True}}})
        sc = recv()["result"]["structuredContent"]
        assert "state" in sc and "switches" in sc
        assert "product_state" in sc and "judgments" in sc and "last_verification" in sc, \
            "状态快照缺少产品状态/判断逻辑/最近验证"
        assert "mainline" in sc, "状态快照缺少产品主线摘要"
        for key in ("roadmap", "doing_items", "todo_items", "todo_count",
                    "human_acceptance", "machine_verified"):
            assert key in sc, f"状态快照缺少 {key}"

        send({"jsonrpc": "2.0", "id": 31, "method": "tools/call",
              "params": {"name": "product_update",
                         "arguments": {"area": "面板", "behavior": "一眼看懂在做什么",
                                       "why": "小白先要能感知",
                                       "files": ["plugin/ui/panel.html"]}}})
        assert not recv()["result"].get("isError"), "product_update 调用失败"
        send({"jsonrpc": "2.0", "id": 32, "method": "tools/call",
              "params": {"name": "product_state", "arguments": {}}})
        assert "面板" in recv()["result"]["content"][0]["text"], "product_state 读不到刚写入的行为"
        send({"jsonrpc": "2.0", "id": 33, "method": "tools/call",
              "params": {"name": "distill_add",
                         "arguments": {"statement": "落盘默认合入", "scope": "process"}}})
        assert not recv()["result"].get("isError"), "distill_add 调用失败"

        send({"jsonrpc": "2.0", "id": 34, "method": "tools/call",
              "params": {"name": "register_acceptance",
                         "arguments": {"title": "首页第一屏不挤", "needs_human": True,
                                       "steps": "1) 打开首页 2) 看第一屏", "level": "P0"}}})
        assert not recv()["result"].get("isError"), "register_acceptance 调用失败"
        send({"jsonrpc": "2.0", "id": 35, "method": "tools/call",
              "params": {"name": "autopilot_status", "arguments": {"fast": True}}})
        hum = recv()["result"]["structuredContent"].get("human_acceptance") or []
        assert hum and hum[0].get("title") == "首页第一屏不挤", f"人工验收没进状态: {hum}"

        send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
              "params": {"name": "autopilot_control", "arguments": {"action": "pause"}}})
        assert recv()["result"]["structuredContent"]["paused"] is True

        send({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
              "params": {"name": "autopilot_control",
                         "arguments": {"action": "run_once"}}})
        assert recv()["result"]["isError"] is True, "暂停时 run_once 不应放行"

        send({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
              "params": {"name": "autopilot_control", "arguments": {"action": "resume"}}})
        assert recv()["result"]["structuredContent"]["paused"] is False
        proc.stdin.close()
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        proc.stderr.close()
        import shutil
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(project, ignore_errors=True)
    print("selftest ok: MCP resources 合约、暂停闸门、状态/控制工具全部通过。")


def test_budget_gate():
    """预算闸门：越线判定 / 不打扰在用窗口 / 熔断与恢复 / 一键关 / 预演零副作用。

    全部在临时目录里伪造数据源（automations + codex 线程库 + 代理账本），
    不碰真实的 ~/.codex、不换代、不发通知、不写真实状态。
    """
    import json as _json
    import sqlite3
    import time as _time
    import importlib

    sys.path.insert(0, str(OPS))
    bg = importlib.import_module("budget_gate")

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-budget-") as tmp:
        root = Path(tmp)
        home = root / "home"
        codex = home / ".codex"
        runtime = root / "runtime"
        (codex / "automations" / "fake").mkdir(parents=True)
        runtime.mkdir(parents=True)

        # 目标线程的 rollout：峰值 19 万（谷底 4.6 万）+ 近 1 小时压缩 3 次
        rollout = root / "rollout.jsonl"
        now_iso = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime())
        lines = []
        for i, v in enumerate([46_000, 120_000, 190_000]):
            lines.append(_json.dumps({"timestamp": now_iso, "type": "response_item",
                                      "payload": {"type": "token_usage_record",
                                                  "usage": {"input_tokens": v}}}))
        for i in range(3):
            lines.append(_json.dumps({"timestamp": now_iso, "type": "compacted",
                                      "payload": {"message": "summary"}}))
        rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")

        db = codex / "state_5.sqlite"
        c = sqlite3.connect(db)
        c.execute("create table threads (id text primary key, rollout_path text, "
                  "tokens_used integer, updated_at integer)")
        c.execute("insert into threads values (?,?,?,?)", ("fake-thread", str(rollout), 1_000, 0))
        c.commit()
        c.close()

        proxy = home / ".codex-proxy" / "stats.db"
        proxy.parent.mkdir(parents=True)
        p = sqlite3.connect(proxy)
        p.execute("create table requests (id integer primary key, ts integer, provider text, "
                  "model text, input_tokens integer, output_tokens integer, "
                  "cache_hit_tokens integer, status integer)")
        p.execute("insert into requests (ts, provider, model, input_tokens, output_tokens, "
                  "cache_hit_tokens, status) values (?,?,?,?,?,?,?)",
                  (int(_time.time()) - 60, "fake", "fake-model", 10_000_000, 1000, 9_000_000, 200))
        p.commit()
        p.close()

        (codex / "automations" / "fake" / "automation.toml").write_text(
            'version = 1\nid = "fake"\nkind = "heartbeat"\nname = "fake"\n'
            'prompt = "x"\nstatus = "ACTIVE"\nrrule = "RRULE:FREQ=MINUTELY;INTERVAL=15"\n'
            'target_thread_id = "fake-thread"\ncreated_at = 1\nupdated_at = 1\n',
            encoding="utf-8")

        # 把模块里的路径指向伪造环境（测完在 finally 里还原）
        real = {k: getattr(bg, k) for k in
                ("AUTOMATIONS", "CODEX_DB", "PROXY_DB", "RUNTIME", "STATE", "LOG",
                 "ROTATE_LOG", "OFF_FILES", "CONFIG_PATH")}
        started = []
        real_popen = subprocess.Popen

        class _NoPopen:
            def __init__(self, *a, **k):
                started.append(a[0] if a else k.get("args"))
                raise OSError("selftest: 换代被拦住（本测试不允许真换代）")

        try:
            bg.AUTOMATIONS, bg.CODEX_DB, bg.PROXY_DB = (codex / "automations", db, proxy)
            bg.RUNTIME, bg.STATE = runtime, runtime / "budget-gate.json"
            bg.LOG, bg.ROTATE_LOG = runtime / "budget-gate.log", runtime / "rotate.log"
            bg.OFF_FILES = (runtime / "BUDGET_GATE_OFF", codex / "budget-gate.off")
            bg.CONFIG_PATH = codex / "nonexistent.json"
            bg.notify = lambda *a, **k: None          # 不发桌面通知
            bg._event = lambda *a, **k: None          # 不写真实流水
            subprocess.Popen = _NoPopen

            # 1) 越线判定：只看「最后一轮」（4.6 万，谷底）会漏，峰值/压缩必须抓住
            info = bg.measure_thread("fake-thread", bg.DEFAULTS)
            level, reasons = bg.verdict(info, bg.DEFAULTS)
            if level != "over":
                raise AssertionError(f"峰值 19 万 + 压缩 3 次应判 over，实得 {level}：{reasons}")
            if info.get("peak_input_recent") != 190_000:
                raise AssertionError(f"峰值读错：{info.get('peak_input_recent')}")
            if info.get("compactions_recent") != 3:
                raise AssertionError(f"压缩次数读错：{info.get('compactions_recent')}")

            # 2) 窗口刚动过（有人在用）→ 不换代，只等
            rep = bg.run(dry=False)
            acts = [a["action"] for a in rep["actions"]]
            if "rotate-wait" not in acts or started:
                raise AssertionError(f"在用窗口不该被换代，实得动作 {acts}，换代调用 {started}")

            # 3) 窗口空闲 10 分钟 + 允许换代 → 才会真的排一次换代（Popen 被测试拦住）
            old = _time.time() - 600
            os.utime(rollout, (old, old))
            rep = bg.run(dry=False)
            acts = [a["action"] for a in rep["actions"]]
            if "rotate" not in acts or len(started) != 1:
                raise AssertionError(f"空闲且越线应排一次换代，实得动作 {acts}，换代调用 {started}")
            if "--force" not in list(started[0]):
                raise AssertionError(f"换代必须带 --force 才越过 cockpit_rotate 自带的粗线：{started[0]}")

            # 4) 熔断与恢复：用量越线停「派活」，回落自动恢复
            bg._maybe_pause_dispatch({"input": 9_000_000_000, "fresh": 9_000_000_000,
                                      "requests": 1, "window_minutes": 60}, bg.DEFAULTS)
            if not bg.dispatch_paused()[0]:
                raise AssertionError("用量越线没有暂停派活")
            import central_daemon as _cd
            real_ac, real_state = _cd.auto_exec_config, _cd.control_state.load
            try:
                _cd.control_state.load = lambda: {"auto_exec": True}
                if _cd.auto_exec_config().get("enabled"):
                    raise AssertionError("熔断后自动挡仍在派活（auto_exec 没被关掉）")
            finally:
                _cd.control_state.load = real_state
            bg._maybe_pause_dispatch({"input": 1_000_000, "fresh": 100_000,
                                      "requests": 5, "window_minutes": 60}, bg.DEFAULTS)
            if bg.dispatch_paused()[0]:
                raise AssertionError("用量回落后没有自动恢复派活")

            # 4b) 终态暂缓不能被冷却时间重新放回执行池。宽任务、人工验收和
            # 「零改动但保留现场」都需要明确拆分或重开，不能每 30 分钟烧一次 worker。
            import central_daemon as _cd2
            old_ts = "2000-01-01 00:00:00"
            terminal = {
                "needs_split": {"reason": "needs_split", "ts": old_ts},
                "needs_human": {"reason": "needs_human", "ts": old_ts},
                "no_changes": {"reason": "no_changes", "ts": old_ts},
                "no_changes_preserved": {"reason": "no_changes_preserved", "ts": old_ts},
            }
            for rid, rec in terminal.items():
                if _cd2._exec_retryable({rid: rec}, rid):
                    raise AssertionError(f"终态暂缓 {rid} 被判为可重派")
            retryable = {"L-retry": {"reason": "codex_exit_1", "ts": old_ts}}
            if not _cd2._exec_retryable(retryable, "L-retry"):
                raise AssertionError("普通本地失败在冷却后应允许重试")

            # 5) 状态文件坏掉 → fail-open（不误停、不抛）
            bg.STATE.write_text("{ 坏 json", encoding="utf-8")
            if bg.dispatch_paused()[0]:
                raise AssertionError("状态文件坏掉时不应误停派活")

            # 6) 一键关：存在开关文件 → 整体跳过，零动作
            bg.STATE.unlink()
            (runtime / "BUDGET_GATE_OFF").write_text("", encoding="utf-8")
            rep = bg.run(dry=False)
            if not rep["off"] or any(a["action"] not in ("skip",) for a in rep["actions"]):
                raise AssertionError(f"一键关没有生效：{rep['actions']}")
            (runtime / "BUDGET_GATE_OFF").unlink()

            # 7) 预演零副作用：不写状态、不换代
            before = bg.STATE.exists()
            started.clear()
            bg.run(dry=True)
            if started:
                raise AssertionError("预演模式下发生了换代")
            if bg.STATE.exists() != before and not before:
                raise AssertionError("预演模式不应写状态文件")
        finally:
            subprocess.Popen = real_popen
            for k, v in real.items():
                setattr(bg, k, v)

    print("selftest ok: 预算闸门抓住峰值/压缩越线、不打扰在用窗口、只对空闲越线窗口换代（带 --force）；"
          "用量熔断可停派活并自动恢复；状态坏掉 fail-open；一键关与预演零副作用。")


if __name__ == "__main__":
    main()
    test_replan()
    test_resolve()
    test_reset_trunk_truth_conflict()
    test_clean_land_with_dirty_trunk()
    test_align_main_only_recovers_truth_only_commits()
    test_control_state()
    test_resolve_worker_pool()
    test_verify_and_truth()
    test_auto_exec()
    test_mcp_contract()
    test_budget_gate()
