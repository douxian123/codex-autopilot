#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动挡的最小安全检查。

覆盖一个曾经真实发生的坏路径：convergence 分支把未解冲突保留在提交里，
它和 main 本身没有新的合并冲突，于是被误判成 CLEAN，守护进程可能直接把它推上 main。
这里用临时仓库锁住这道门禁：坏分支不能入队，也不能 land。
"""

import json
import os
import json
import subprocess
import sys
import tempfile
import time as _time
from pathlib import Path

OPS = Path(__file__).resolve().parent
KGCTL = OPS / "kgctl.py"


def _installed_version() -> str:
    import json
    candidates = (
        OPS.parent / "plugin" / ".codex-plugin" / "plugin.json",
        OPS.parent / ".codex-plugin" / "plugin.json",
    )
    for path in candidates:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["version"]
    raise AssertionError(f"找不到插件清单：{candidates[0]}")


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
        run([sys.executable, str(OPS / "test_truthctl_cross_worktree_ids.py")],
            cwd=str(OPS.parent), check=True)
        run([sys.executable, str(OPS / "test-ledger-terminal-state.py")],
            cwd=str(OPS.parent), check=True)
        run([sys.executable, str(OPS / "test-land-build-only-todo.py")],
            cwd=str(OPS.parent), check=True)
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
        (repo / "ops" / "tasks" / "conflict_burden.json").write_text(
            json.dumps({
                "main": git(repo, "rev-parse", "main").stdout.strip(),
                "branches": ["codex/dup"],
                "burden": {"codex/dup": 1},
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        port_dup = kgctl(repo, trunk, "port", "--auto", "--go", "--max-unlanded", "1")
        if "冲突缓存命中 1 条" not in port_dup.stdout:
            raise AssertionError(f"auto-port 没有复用冲突缓存:\n{port_dup.stdout}\n{port_dup.stderr}")
        if "选中 codex/dup" not in port_dup.stdout:
            raise AssertionError(f"auto-port 没选中缓存候选:\n{port_dup.stdout}\n{port_dup.stderr}")
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

        # 合法缓存被 skip 全部过滤后仍是缓存命中；这里若退回 scan()，真实项目会再次
        # 在自动派活前卡满 90 秒。
        port_cached_skip = kgctl(
            repo, trunk, "port", "--auto", "--go", "--max-unlanded", "1",
            extra_env={"KG_PORT_SKIP": "codex/dup"})
        if "冲突缓存命中 0 条" not in port_cached_skip.stdout:
            raise AssertionError(
                f"合法空缓存错误回退全量扫描:\n{port_cached_skip.stdout}\n"
                f"{port_cached_skip.stderr}")


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


def test_sync_trunk_truth_failclosed_non_truth():
    """锁住 tick/sync 的回归：主干工作树带非真相层改动时，真相同步必须放弃提交。

    旧实现只把 TRUTH_PATHS 显式 add，但如果同一棵树里已经有大量代码/测试改动，
    `git commit` 仍会把索引里那些非真相文件一起卷进 `chore(truth)`。这里构造
    「真相文件 + 代码文件同时脏」的共享检出场景，要求 sync_trunk_truth 返回 False、
    不产生 chore(truth) 提交、非真相文件继续留在工作区。
    """
    import importlib
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-truth-sync-failclosed-") as tmp:
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
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "PROJECT_STATUS.md").write_text("# status\n\nbase\n", encoding="utf-8")
        (repo / "ops" / "central.config.json").write_text(
            json.dumps({"build": {"cmd": "", "cwd": "."}}), encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        git(repo, "worktree", "add", "-q", "--detach", str(trunk), "main")

        # 共享检出同时存在真相层改动和代码改动。
        (trunk / "docs" / "PROJECT_STATUS.md").write_text("# status\n\nnew-truth\n", encoding="utf-8")
        (trunk / "backend" / "src" / "app.ts").parent.mkdir(parents=True, exist_ok=True)
        (trunk / "backend" / "src" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
        git(trunk, "add", "backend/src/app.ts")

        before = git(trunk, "rev-parse", "HEAD").stdout.strip()
        sys.path.insert(0, str(OPS))
        os.environ["KG_REPO"] = str(repo)
        os.environ["KG_TRUNK"] = str(trunk)
        os.environ.pop("KG_TICK_LIGHT", None)
        import kgctl
        importlib.reload(kgctl)
        ok = kgctl.sync_trunk_truth(trunk, "sync from tick")
        if ok:
            raise AssertionError("非真相层改动存在时 sync_trunk_truth 仍提交了")
        after = git(trunk, "rev-parse", "HEAD").stdout.strip()
        if after != before:
            raise AssertionError(f"sync_trunk_truth 产生了提交: {before} -> {after}")
        status = git(trunk, "status", "--porcelain").stdout
        if "backend/src/app.ts" not in status:
            raise AssertionError("非真相层改动被 sync_trunk_truth 吞掉或清掉")
        if "docs/PROJECT_STATUS.md" not in status:
            raise AssertionError("真相层改动被 sync_trunk_truth 吞掉或清掉")
    print("selftest ok: 工作树夹带代码改动时，真相同步 fail-closed 不吞文件、不产生 chore(truth) 提交。")


def test_publish_lock_and_origin_containment():
    """锁住并发发布故障：land 与真相推送不能同时持有发布权，成功必须由远端祖先证明。"""
    import importlib
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-publish-lock-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        repo.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "app.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "app.txt")
        git(repo, "commit", "-q", "-m", "init")
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        git(repo, "fetch", "-q", "origin", "main")

        sys.path.insert(0, str(OPS))
        os.environ["KG_REPO"] = str(repo)
        os.environ["KG_TRUNK"] = str(root / "unused-trunk")
        import kgctl
        importlib.reload(kgctl)
        real_lock = kgctl.LOCK
        real_push = kgctl._push_truth_state_locked
        kgctl.LOCK = root / "publish.lock"
        calls = []
        kgctl._push_truth_state_locked = lambda: calls.append("truth")
        try:
            assert kgctl._acquire_lock("land")
            kgctl.push_truth_state()
            if calls:
                raise AssertionError("land 持锁时真相推送仍进入了发布临界区")
            kgctl._release_lock()
            kgctl.push_truth_state()
            if calls != ["truth"]:
                raise AssertionError(f"无锁时真相推送没有执行: {calls}")
            if kgctl.LOCK.exists():
                raise AssertionError("真相推送结束后没有释放发布锁")

            (repo / "app.txt").write_text("landed\n", encoding="utf-8")
            git(repo, "commit", "-qam", "landed")
            landed = git(repo, "rev-parse", "HEAD").stdout.strip()
            if kgctl._origin_contains(landed):
                raise AssertionError("未推送的本地提交被误判为已在 origin/main")
            git(repo, "push", "-q", "origin", "main")
            git(repo, "fetch", "-q", "origin", "main")
            if not kgctl._origin_contains(landed):
                raise AssertionError("已推送提交没有被 origin/main 祖先检查确认")
        finally:
            kgctl._push_truth_state_locked = real_push
            kgctl.LOCK = real_lock

    print("selftest ok: land/真相推送发布锁互斥，且只有 origin/main 祖先校验通过才算落盘。")


def test_stale_lock_selfheals_and_never_deletes_others():
    """僵死锁必须自愈；且任何路径都不许删掉别人的锁。

    真实踩到的坑（2026-10-27）：`.git/kgctl-land.lock` 里写着 `71714 supersede`，
    而 71714 早就死了。僵死隔离机制本身是好的，但 supersede / converge / replan
    三条路径用的是 `LOCK.write_text(...)` + `finally: LOCK.unlink()`：
      * 「探活 → 写文件」之间没有原子性，两个进程可以同时通过探活；
      * `unlink()` 不核对 pid，会把**别人正持有的锁**删掉，落盘开始互相踩。
    这条门禁把三件事一起锁住：死锁自愈、活锁不被误删、隔离出来的证据不会无限堆。
    """
    import importlib
    import time as _t

    sys.path.insert(0, str(OPS))
    tmp = Path(tempfile.mkdtemp(prefix="kg-lock-"))
    # kgctl 导入时就会解析编排状态根并 sys.exit；必须在 reload 之前把 KG_REPO
    # 指到一个「看起来像状态根」的临时目录，否则本用例会吃上一个用例留下的
    # 已删除临时路径，报「找不到编排状态根」——那不是锁的问题，却会让门禁红。
    state = tmp / "state"
    (state / "ops" / "tasks").mkdir(parents=True)
    (state / "ops" / "truth").mkdir(parents=True)
    _saved_repo = os.environ.get("KG_REPO")
    os.environ["KG_REPO"] = str(state)
    os.environ.setdefault("KG_TRUNK", str(tmp / "unused-trunk"))
    import kgctl
    importlib.reload(kgctl)

    real = kgctl.LOCK
    kgctl.LOCK = tmp / "kgctl-land.lock"
    try:
        # 1) 一个确定已死的 pid：必须被判为不阻塞，并把锁挪成证据。
        dead_pid = 999999999
        kgctl.LOCK.write_text(f"{dead_pid} supersede\n", encoding="utf-8")
        assert kgctl._lock_blocked() is False, "持有者已死却被当成有人在做"
        assert not kgctl.LOCK.exists(), "僵死锁没有被隔离，仍会挡住整个舰队"
        assert list(tmp.glob("kgctl-land.lock.stale-*")), "隔离时没有留下证据文件"

        # 2) 自己的进程是活的：必须判为阻塞，且不许被别人顺手删掉。
        kgctl.LOCK.write_text(f"{os.getpid()} converge\n", encoding="utf-8")
        assert kgctl._lock_blocked() is True, "活锁必须挡住第二个进程"
        saved = kgctl.LOCK.read_text(encoding="utf-8")
        # 模拟「另一个进程」调用释放：把 getpid 换成别的值，它不该删除。
        import unittest.mock as _mock
        with _mock.patch.object(kgctl.os, "getpid", return_value=os.getpid() + 1):
            kgctl._release_lock()
        assert kgctl.LOCK.exists(), "非持有者把别人的锁删掉了——落盘会开始互相踩"
        assert kgctl.LOCK.read_text(encoding="utf-8") == saved

        # 3) 隔离证据不许无限堆：超过 keep 条就把旧的清掉。
        for i in range(12):
            f = tmp / f"kgctl-land.lock.stale-{1000 + i}"
            f.write_text("x\n", encoding="utf-8")
            _t.sleep(0.002)
        kgctl._prune_stale_locks(keep=5)
        left = sorted(tmp.glob("kgctl-land.lock.stale-*"))
        assert len(left) == 5, f"僵死锁证据没有按 keep 剪枝，还剩 {len(left)} 条"

        # 4) 可执行代码里只剩 _release_lock 一处 unlink（且它核对 pid）。
        #    文档字符串里提到 `finally: LOCK.unlink()` 是解释历史 bug，不算。
        import ast as _ast
        tree = _ast.parse((OPS / "kgctl.py").read_text(encoding="utf-8"))
        unlinks = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute) \
                    and node.func.attr == "unlink" \
                    and isinstance(node.func.value, _ast.Name) \
                    and node.func.value.id == "LOCK":
                unlinks.append(node.lineno)
        assert len(unlinks) == 1, (
            f"有 {len(unlinks)} 处不核对 pid 的 LOCK.unlink()（行 {unlinks}），"
            "应当只剩 _release_lock 里那一处")
    finally:
        kgctl.LOCK = real
        if _saved_repo is None:
            os.environ.pop("KG_REPO", None)
        else:
            os.environ["KG_REPO"] = _saved_repo
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("selftest ok: 僵死锁自愈、活锁不被误删、隔离证据按 keep 剪枝。")


def test_remote_benchmark_pollution_failclosed():
    """锁住稳定基线保护的运行入口：远端 origin/main 的 36 题报告被伪快照覆盖时，
    tick/intake 必须 fail-closed，不能把伪快照写回本地真实基线，也不能继续推送。
    """
    import hashlib as _hashlib
    REAL_HASH = "a9e242bff237e3836484776640ce751e9470948e976bc1f4c795401d6767050f"
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-benchmark-pollution-") as tmp:
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
        (repo / "ops" / "central.config.json").write_text(
            json.dumps({"build": {"cmd": "", "cwd": "."}}), encoding="utf-8")
        fixture = OPS / "selftest_fixtures" / "benchmark-contract-report.json"
        if not fixture.exists():
            raise AssertionError("真实 36 题基线 fixture 缺失")
        real_bytes = fixture.read_bytes()
        if _hashlib.sha256(real_bytes).hexdigest() != REAL_HASH:
            raise AssertionError("自测基线 fixture 与真实基线常量不一致")
        (repo / "ops" / "truth" / "benchmark-contract-report.json").write_bytes(real_bytes)
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")

        # 远端被覆盖成伪快照，同时本地仍保留真实字节。
        git(repo, "checkout", "-q", "-b", "codex/pollute", "main")
        (repo / "ops" / "truth" / "benchmark-contract-report.json").write_text(
            '{"itemCount": 1, "fabricated": true}\n', encoding="utf-8")
        git(repo, "add", "ops/truth/benchmark-contract-report.json")
        git(repo, "commit", "-q", "-m", "pollute remote baseline")
        git(repo, "checkout", "-q", "main")
        git(repo, "update-ref", "refs/heads/main", "codex/pollute")
        git(repo, "push", "-q", "origin", "main")
        git(repo, "update-ref", "refs/heads/main", "HEAD@{1}")

        before_local = (repo / "ops" / "truth" / "benchmark-contract-report.json").read_text(
            encoding="utf-8")
        before_main = git(repo, "rev-parse", "main").stdout.strip()
        tick = kgctl(repo, trunk, "tick", "--no-converge")
        out = tick.stdout + tick.stderr
        if tick.returncode == 0:
            raise AssertionError(f"远端污染时 tick 没有 fail-closed:\n{out}")
        if "远端保护证据本身已被污染" not in out:
            raise AssertionError(f"tick 没有报告远端污染:\n{out}")
        if "不传播伪快照" not in out:
            raise AssertionError(f"tick 没有说明不传播伪快照:\n{out}")
        after_local = (repo / "ops" / "truth" / "benchmark-contract-report.json").read_text(
            encoding="utf-8")
        if after_local != before_local:
            raise AssertionError("tick 覆盖了本地真实基线")
        if git(repo, "rev-parse", "main").stdout.strip() != before_main:
            raise AssertionError("tick 推进了 main 或本地 main 引用")
    print("selftest ok: 远端基准被污染时 tick fail-closed，本地真实基线不被动、不传播伪快照。")


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
        assert d["enabled"] is True and d["paused"] is False and d["auto_land"] is True
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

        control_state.set_keys(enabled=False, auto_exec=True)
        assert control_state.lifecycle_status() == "off"
        blocked = control_state.mutation_guard()
        assert blocked and blocked["code"] == "AUTOPILOT_DISABLED"
        disabled_cycle = central_daemon.run_cycle(dry=False, max_land=1)
        assert disabled_cycle.get("disabled") is True
        restored = control_state.set_keys(enabled=True)
        assert restored["enabled"] is True and restored["auto_exec"] is True
        assert control_state.mutation_guard() is None

        # control.json 损坏 -> 安全暂停 + error 可见。
        control_state.path().write_text("{ this is not json", encoding="utf-8")
        bad = control_state.load()
        assert bad["enabled"] is False and bad["paused"] is True and bad["error"], \
            "损坏的 control.json 没有安全暂停"

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


def test_installer():
    """版本化安装只切 marketplace 指针，不覆盖旧插件；回滚只还原自动挡入口。"""
    import json as _json

    install = OPS.parent / "install.sh"
    # 源码级锁：装完必须尝试把守护改指新版本。少了这一段，用户「装了新版」
    # 而 live 继续跑旧代码，且没有任何报错 —— 只能靠反复清盘碰运气。
    _install_src = install.read_text(encoding="utf-8")
    assert "--install" in _install_src and "NO_DAEMON_REPOINT" in _install_src, \
        "install.sh 缺少「装完把守护改指新版本」这一步"
    assert "daemon.plist" in _install_src, "install.sh 没有检查本机是否已装守护"
    installed_version = _installed_version()
    release_path = f"./plugins/codex-autopilot/releases/{installed_version}"
    with tempfile.TemporaryDirectory(prefix="autopilot-install-test-") as tmp:
        home = Path(tmp) / "home"
        release_dir = home / "plugins" / "codex-autopilot" / "releases" / installed_version
        old_plugin = home / "plugins" / "codex-autopilot"
        old_plugin.mkdir(parents=True)
        sentinel = old_plugin / "user-preserved.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        market = home / ".agents" / "plugins" / "marketplace.json"
        market.parent.mkdir(parents=True)
        market.write_text(_json.dumps({
            "name": "personal",
            "plugins": [
                {"name": "other-plugin", "source": {"source": "local", "path": "./plugins/other"}},
                {"name": "codex-autopilot",
                 "source": {"source": "local", "path": "./plugins/codex-autopilot"},
                 "policy": {"installation": "AVAILABLE"}},
            ],
        }), encoding="utf-8")

        env = os.environ.copy()
        env["HOME"] = str(home)
        # 这些用例在开发中的脏 worktree 里真实跑安装；发布溯源由
        # test_installer_requires_clean_source 单独锁住默认行为。
        env["CODEX_AUTOPILOT_ALLOW_DIRTY_SOURCE"] = "1"
        # --no-companion：菜单栏 .app 的编译单独测（test_companion_contract），
        # 不让一次安装测试既跑安装又跑 Swift 编译。
        command = ["bash", str(install), "--no-seed", "--no-companion", str(OPS.parent)]
        upgraded = run(command, env=env, check=False)
        if upgraded.returncode:
            raise AssertionError(f"版本化安装失败:\n{upgraded.stdout}\n{upgraded.stderr}")
        data = _json.loads(market.read_text(encoding="utf-8"))
        entry = next(p for p in data["plugins"] if p["name"] == "codex-autopilot")
        other = next(p for p in data["plugins"] if p["name"] == "other-plugin")
        assert entry["source"]["path"] == release_path
        assert other["source"]["path"] == "./plugins/other"
        assert sentinel.read_text(encoding="utf-8") == "keep\n"
        release = release_dir
        assert (release / "ops" / "central_mcp.py").is_file()
        # Companion 源码必须随版本目录一起装：以后能从这个目录重建菜单栏应用。
        assert (release / "companion" / "build.sh").is_file()
        assert (release / "companion" / "Info.plist").is_file()
        assert (release / "companion" / "Sources" / "CodexAutopilotCompanion"
                / "main.swift").is_file()

        duplicate = run(command, env=env, check=False)
        assert duplicate.returncode != 0 and "拒绝覆盖" in duplicate.stderr
        data_after_duplicate = _json.loads(market.read_text(encoding="utf-8"))
        entry_after_duplicate = next(
            p for p in data_after_duplicate["plugins"] if p["name"] == "codex-autopilot")
        assert entry_after_duplicate["source"]["path"] == entry["source"]["path"]

        # 故障回归：版本目录被手工移除后，用户会用同一版本重装恢复。
        # 这时 marketplace 已指向当前版本，不能把回滚指针也覆盖成当前版本。
        import shutil
        shutil.rmtree(release)
        reinstalled = run(command, env=env, check=False)
        if reinstalled.returncode:
            raise AssertionError(
                f"同版本恢复安装失败:\n{reinstalled.stdout}\n{reinstalled.stderr}")
        assert (release / "ops" / "central_mcp.py").is_file()
        state = _json.loads(
            (home / "plugins" / "codex-autopilot" / "install-state.json")
            .read_text(encoding="utf-8"))
        assert state["source"] == release_path
        assert state["previous_source"] == "./plugins/codex-autopilot", \
            "同版本恢复安装把回滚指针覆盖成了当前版本"

        rolled_back = run(["bash", str(install), "--rollback"], env=env, check=False)
        if rolled_back.returncode:
            raise AssertionError(f"回滚失败:\n{rolled_back.stdout}\n{rolled_back.stderr}")
        restored = _json.loads(market.read_text(encoding="utf-8"))
        restored_entry = next(p for p in restored["plugins"] if p["name"] == "codex-autopilot")
        assert restored_entry["source"]["path"] == "./plugins/codex-autopilot"
        assert any(p["name"] == "other-plugin" for p in restored["plugins"])
        assert sentinel.read_text(encoding="utf-8") == "keep\n"
        assert (release / "ops" / "central_mcp.py").is_file()

    print("selftest ok: 安装器使用不可变版本目录、拒绝覆盖同版本、"
          "同版本目录恢复重装仍保留上一版回滚指针。")


def test_installer_requires_clean_source():
    """不可变发布必须能追到唯一提交，不能把 dirty 工作区混进版本目录。"""
    import json as _json
    import shutil

    install = OPS.parent / "install.sh"
    installed_version = _installed_version()
    with tempfile.TemporaryDirectory(prefix="autopilot-source-proof-") as tmp:
        root = Path(tmp)
        source = root / "source"
        shutil.copytree(OPS.parent, source, ignore=shutil.ignore_patterns(".git"))
        source_install = source / "install.sh"
        run(["git", "init", "-q", "-b", "main", str(source)])
        run(["git", "-C", str(source), "config", "user.email", "selftest@example.invalid"])
        run(["git", "-C", str(source), "config", "user.name", "Autopilot Selftest"])
        run(["git", "-C", str(source), "add", "."])
        run(["git", "-C", str(source), "commit", "-qm", "fixture"])
        commit = run(["git", "-C", str(source), "rev-parse", "HEAD"],
                     check=False).stdout.strip()

        home = root / "home"
        home.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(home)
        clean = run(["bash", str(source_install), "--no-seed", "--no-marketplace",
                     "--no-companion", str(source)], env=env, check=False)
        if clean.returncode:
            raise AssertionError(f"干净安装失败:\n{clean.stdout}\n{clean.stderr}")
        release = home / "plugins" / "codex-autopilot" / "releases" / installed_version
        meta = _json.loads(
            (release / ".codex-autopilot-source.json").read_text(encoding="utf-8"))
        assert meta == {"version": installed_version, "commit": commit, "dirty": False}, \
            f"不可变版本没有记录准确来源提交：{meta}"

        home2 = root / "home-dirty"
        home2.mkdir()
        env["HOME"] = str(home2)
        (source / "UNPUBLISHED.py").write_text("# dirty\n", encoding="utf-8")
        dirty = run(["bash", str(source_install), "--no-seed", "--no-marketplace",
                     "--no-companion", str(source)], env=env, check=False)
        combined = dirty.stdout + dirty.stderr
        assert dirty.returncode != 0, f"脏工作区被误发布:\n{combined}"
        assert "拒绝发布不可变版本" in combined, combined
        dirty_release = (
            home2 / "plugins" / "codex-autopilot" / "releases" / installed_version)
        assert not dirty_release.exists(), "拒绝之后仍留下了不可变版本目录"

    print("selftest ok: 不可变安装记录唯一来源提交，并拒绝把 dirty 工作区混入发布。")


def test_installer_shell_portability():
    """安装脚本必须能在 macOS 自带 bash 3.2 + set -u 下跑完整条路径。

    bash 3.2 有个多字节解析缺陷：`$VAR` 紧跟全角字符时，那个字节会被算进变量名，
    set -u 下直接判 unbound 并中断安装。这类写法在中文提示里很容易踩到，所以既静态
    扫一遍，也真跑一次完整安装路径（覆盖 Companion 分支前后整段），防止只在非中文
    路径下才暴露。
    """
    import re

    installed_version = _installed_version()
    pattern = re.compile(rb"\$[A-Za-z_][A-Za-z0-9_]*")
    for path in sorted(OPS.parent.rglob("*.sh")):
        if ".git" in path.parts or ".build" in path.parts:
            continue
        blob = path.read_bytes()
        for m in pattern.finditer(blob):
            end = m.end()
            if end < len(blob) and blob[end] >= 0x80:
                line = blob[:m.start()].count(b"\n") + 1
                raise AssertionError(
                    f"{path.name}:{line} 用了 $VAR 紧跟多字节字符，bash 3.2 下会 unbound；"
                    f"改成 ${{VAR}} 写法")

    install = OPS.parent / "install.sh"
    with tempfile.TemporaryDirectory(prefix="autopilot-install-bash32-") as tmp:
        home = Path(tmp) / "home"
        home.mkdir(parents=True)
        # 用一个立刻失败的 swiftc 桩，只为让 Companion 分支走「编译失败降级」，
        # 覆盖它前后的整段提示/权限路径；真编译由 test_companion_contract 覆盖。
        fake_bin = Path(tmp) / "bin"
        fake_bin.mkdir()
        stub = fake_bin / "swiftc"
        stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["CODEX_AUTOPILOT_ALLOW_DIRTY_SOURCE"] = "1"
        result = run(["bash", str(install), "--no-seed", "--no-marketplace", str(OPS.parent)],
                     env=env, check=False)
        combined = result.stdout + result.stderr
        assert "unbound variable" not in combined, f"bash 3.2 变量解析崩了:\n{combined}"
        assert result.returncode == 0, f"完整安装路径没跑完:\n{combined}"
        assert (home / "plugins" / "codex-autopilot" / "releases" / installed_version
                / "ops" / "central_mcp.py").is_file()
        # 同一份安装不带 Companion 时也必须照样成功。
        result2 = run(["bash", str(install), "--no-seed", "--no-marketplace",
                       "--no-companion", str(OPS.parent)], env=env, check=False)
        # 版本目录已存在，第二次会因「拒绝覆盖」退出；只要不是崩在变量解析上就说明路径健康。
        assert "unbound variable" not in (result2.stdout + result2.stderr)

    print("selftest ok: 安装脚本在 macOS bash 3.2 + set -u 下整条路径跑完，没有多字节变量解析崩溃。")


def test_panel_first_screen():
    """面板首屏必须真的把控制层数据渲染出来，而不是只有对得上的元素 id。

    这条真执行面板脚本（最小 DOM），核对：绑定项目、总状态、正在做什么、
    原生直连结论、三个计数，以及「关掉总闸后是明确关闭态」。没有 node 时跳过。
    """
    import shutil

    if not shutil.which("node"):
        print("selftest skip: 没有 node，面板首屏逻辑检查跳过。")
        return
    script = OPS / "panel_logic_check.py"
    assert script.is_file(), "panel_logic_check.py 缺失"
    env = os.environ.copy()
    env.setdefault("KG_REPO", str(OPS.parent))
    result = run([sys.executable, str(script), "--quiet"],
                 cwd=str(OPS.parent), env=env, check=False)
    if result.returncode != 0:
        raise AssertionError(f"面板首屏逻辑检查未通过:\n{result.stdout}\n{result.stderr}")
    print("selftest ok: 面板首屏（含关闭态）每个值都来自真实控制层数据。")


def test_installed_copy_binding():
    """装出来的那份副本必须自己认得项目，并且 MCP 能应答。

    这条防的是最容易翻车、又最难查的一类问题：插件装是装上了，marketplace 也列出来了，
    但 Codex 拉起它时它不知道自己该管哪个项目，于是面板一片空白——用户只会觉得
    「装了没用」。这里真装一次到隔离 HOME，然后用装出来的副本、不带任何环境变量跑：
      1) kgctl status —— 能不能自己解析出项目；
      2) MCP 握手 —— initialize/tools/list 能不能应答。
    """
    import json as _json

    install = OPS.parent / "install.sh"
    installed_version = _installed_version()
    with tempfile.TemporaryDirectory(prefix="autopilot-installed-bind-") as tmp:
        home = Path(tmp) / "home"
        home.mkdir(parents=True)
        project = Path(tmp) / "project"
        # 状态根判据是 ops/tasks + ops/truth 两个目录都在；目标项目本身是个 git 仓库。
        (project / "ops" / "tasks").mkdir(parents=True)
        (project / "ops" / "truth").mkdir(parents=True)
        (project / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
        (project / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(project)], check=False)
        run(["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
             "add", "."], check=False)
        run(["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "init"], check=False)

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["CODEX_AUTOPILOT_ALLOW_DIRTY_SOURCE"] = "1"
        for key in ("KG_REPO", "KG_TRUNK", "KG_DAEMON_HOME"):
            env.pop(key, None)
        result = run(["bash", str(install), "--no-seed", "--no-marketplace",
                      "--no-companion", str(project)], env=env, check=False)
        assert result.returncode == 0, f"安装失败:\n{result.stdout}\n{result.stderr}"

        release = home / "plugins" / "codex-autopilot" / "releases" / installed_version
        binding = _json.loads((release / ".mcp.json").read_text(encoding="utf-8"))
        bound = binding["mcpServers"]["codex-autopilot"]["env"]["KG_REPO"]
        assert bound == str(project), f"插件没有把项目写进绑定：{bound}"

        # 1) 不带 KG_REPO：装出来的副本要能自己认出项目
        status = run([sys.executable, str(release / "ops" / "kgctl.py"), "status"],
                     env=env, cwd=str(release), check=False)
        assert status.returncode == 0, f"装出来的副本跑不起来:\n{status.stdout}\n{status.stderr}"
        assert str(project) in (status.stdout + status.stderr), \
            f"装出来的副本没有认出项目：\n{status.stdout}"

        # 2) MCP 握手：Codex 拉起它时要能应答
        proc = subprocess.Popen([sys.executable, str(release / "ops" / "central_mcp.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env,
                                cwd=str(release))
        try:
            def send(o):
                proc.stdin.write(_json.dumps(o, ensure_ascii=False) + "\n")
                proc.stdin.flush()

            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18"}})
            init = _json.loads(proc.stdout.readline())["result"]
            assert "resources" in init["capabilities"], "装出来的 MCP 没声明 resources"
            assert init["serverInfo"]["name"], "装出来的 MCP 没有名字"

            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = _json.loads(proc.stdout.readline())["result"]["tools"]
            names = {t["name"] for t in tools}
            for need in ("autopilot_status", "intake_request", "autopilot_control"):
                assert need in names, f"装出来的 MCP 少了工具 {need}"
        finally:
            proc.kill()
            proc.wait(timeout=10)

    print("selftest ok: 装出来的副本自带绑定、kgctl 认得出项目、MCP 能应答（不带任何环境变量）。")


def test_preview_check_contract():
    """预览验收脚本是用户唯一的一键验收入口，必须真检查、真报错。

    它把安装、落地物、市场指向、菜单栏应用、原生直连、总闸状态串成一次运行。
    这里守三件事：脚本存在且可执行、引用的路径就是真实安装布局、没装的时候
    如实报失败并用非零退出码（不能变成只会打印「全部通过」的摆设）。
    """
    script = OPS.parent / "preview-check.sh"
    assert script.is_file(), "preview-check.sh 缺失"
    assert os.access(script, os.X_OK), "preview-check.sh 不可执行"
    text = script.read_text(encoding="utf-8")
    for needle in ("plugin/.codex-plugin/plugin.json",
                   "plugins/codex-autopilot/releases",
                   "Applications/CodexAutopilotCompanion.app",
                   ".agents/plugins/marketplace.json",
                   "ops/codex_native.py probe",
                   "ops/companion_api.py status",
                   "CodexAutopilot/companion.json"):
        assert needle in text, f"preview-check.sh 少了 {needle}"
    assert "FAILS=$((FAILS + 1))" in text and 'exit "${FAILS}"' in text, \
        "验收脚本没有把失败项数当退出码"

    home = tempfile.mkdtemp(prefix="kg-preview-home-")
    try:
        env = os.environ.copy()
        env["HOME"] = home
        result = run(["bash", str(script), "--no-install", "--no-open"],
                     cwd=str(OPS.parent), env=env, check=False)
        combined = result.stdout + result.stderr
        assert "unbound variable" not in combined, combined
        assert "FAIL" in combined, f"空环境里什么都没报：\n{combined}"
        assert "全部通过" not in combined, "什么都没装却报全部通过"
        assert result.returncode > 0, "什么都没装却是零退出码"
    finally:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
    print("selftest ok: 预览验收脚本引用真实路径，未安装时如实报失败并非零退出。")


def test_daemon_project_binding():
    """仅把绑定到当前项目的 launchd 守护显示为当前项目正在运行。"""
    import importlib
    from unittest import mock

    sys.path.insert(0, str(OPS))
    import central_mcp
    importlib.reload(central_mcp)
    old_repo = os.environ.get("KG_REPO")
    os.environ["KG_REPO"] = "/tmp/autopilot-project-a"
    try:
        same_project = mock.Mock(
            returncode=0,
            stdout=("state = running\npid = 4321\n"
                    "KG_REPO => /tmp/autopilot-project-a\n"),
        )
        with mock.patch.object(central_mcp.subprocess, "run", return_value=same_project):
            live, pid, project = central_mcp._daemon_live()
            assert live and pid == 4321 and project == str(Path("/tmp/autopilot-project-a").resolve())

        other_project = mock.Mock(
            returncode=0,
            stdout=("state = running\npid = 8765\n"
                    "KG_REPO => /tmp/autopilot-project-b\n"),
        )
        with mock.patch.object(central_mcp.subprocess, "run", return_value=other_project):
            live, pid, project = central_mcp._daemon_live()
            assert live and pid == 8765 and project == str(Path("/tmp/autopilot-project-b").resolve())
            target = str(Path(os.environ["KG_REPO"]).resolve())
            assert project != target
    finally:
        if old_repo is None:
            os.environ.pop("KG_REPO", None)
        else:
            os.environ["KG_REPO"] = old_repo
    import central_daemon
    with mock.patch.dict(os.environ, {
        "KG_SHARED": "/tmp/autopilot-shared-code",
        "KG_REPO": "/tmp/autopilot-state-root",
        "KG_TRUNK": "/tmp/autopilot-trunk",
    }):
        plist = central_daemon.build_plist(15)
        assert plist["EnvironmentVariables"]["KG_SHARED"] == "/tmp/autopilot-shared-code", \
            "launchd 丢了共享检出，自包含状态根的门禁找不到 node_modules"
    print("selftest ok: launchd 状态通过 launchctl 查询，跨项目 daemon 不冒充当前项目；"
          "共享检出会传给启动后的门禁。")


def test_daemon_plist_repoints_on_upgrade():
    """升级之后守护必须改指新版本目录，否则 live 永远跑旧代码。

    真实踩到的坑（2026-10-27）：版本目录是不可变的，plist 里写死某一次安装的
    绝对路径；`install.sh` 只切 marketplace 指针、不重写 plist，于是装完
    preview.33 之后 launchd 仍按旧路径拉起 preview.32 的脚本，KeepAlive=True
    还一直把它拉回来。用户看到的是「明明发了新版，问题一点没变」，只能反复清盘。
    这里锁三件事：plist 指向的脚本可读出来、与当前这份不一致时判为漂移、
    `--install` 会把它改指当前这一份。
    """
    import importlib
    import plistlib as _plistlib

    sys.path.insert(0, str(OPS))
    import central_daemon
    importlib.reload(central_daemon)

    tmp = Path(tempfile.mkdtemp(prefix="kg-plist-drift-"))
    real_plist = central_daemon.PLIST_PATH
    central_daemon.PLIST_PATH = tmp / "com.codex-autopilot.daemon.plist"
    try:
        # 没装守护：不算漂移，不能反过来报警。
        drifted, running, here = central_daemon.plist_drift()
        assert drifted is False and running is None, (drifted, running)

        # 模拟一条旧 plist：指向另一个（旧）版本目录里的脚本。
        old_script = tmp / "releases" / "0.6.0-preview.32" / "ops" / "central_daemon.py"
        old_script.parent.mkdir(parents=True)
        old_script.write_text("# old\n", encoding="utf-8")
        central_daemon.PLIST_PATH.write_bytes(_plistlib.dumps({
            "Label": central_daemon.PLIST_LABEL,
            "ProgramArguments": ["/usr/bin/python3", str(old_script), "--loop"],
        }))
        assert central_daemon.plist_script_path() == str(old_script)
        drifted, running, here = central_daemon.plist_drift()
        assert drifted is True, "plist 指着旧版本却没判成漂移"
        assert running == str(old_script) and here.endswith("central_daemon.py")

        # --install 必须把 plist 改指当前这一份（这里 mock 掉 launchctl，
        # 门禁不真的去启停用户的守护）。
        from unittest import mock
        calls = []
        with mock.patch.object(central_daemon, "launchctl",
                              side_effect=lambda *a, **k: calls.append(a) or 0), \
             mock.patch.object(central_daemon, "wait_for_job_gone", return_value=True), \
             mock.patch.object(central_daemon, "log"):
            rc = central_daemon.install(15)
        assert rc == 0, "改指新版本的 install 失败"
        drifted, running, here = central_daemon.plist_drift()
        assert drifted is False, f"--install 之后 plist 仍指向旧脚本：{running}"
        assert central_daemon.plist_script_path() == str(
            central_daemon.HERE / "central_daemon.py")
        assert any("bootout" in " ".join(str(x) for x in c) for c in calls), \
            f"--install 没有先停旧实例：{calls}"
    finally:
        central_daemon.PLIST_PATH = real_plist
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("selftest ok: 升级后守护改指新版本目录，旧 plist 漂移可被 --status 看出。")


def test_daemon_control_action():
    """后台常驻开关必须真启停 launchd，而不是只改偏好位。

    面板上的「后台常驻」如果只写配置、不装守护，用户会以为关掉 Codex 还能推进，
    实际什么都没在跑。这里锁住三件事：总闸关掉时拒绝、开启/关闭真转发到
    `central_daemon.py --install/--uninstall`、安装失败不谎报已开启。
    """
    import importlib

    home = tempfile.mkdtemp(prefix="kg-daemon-ctl-")
    project = Path(tempfile.mkdtemp(prefix="kg-daemon-ctl-proj-"))
    (project / "ops" / "truth").mkdir(parents=True)
    (project / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
    old_home = os.environ.get("KG_DAEMON_HOME")
    old_repo = os.environ.get("KG_REPO")
    os.environ["KG_DAEMON_HOME"] = home
    os.environ["KG_REPO"] = str(project)
    try:
        sys.path.insert(0, str(OPS))
        import control_state
        import central_mcp
        importlib.reload(control_state)
        importlib.reload(central_mcp)

        calls = []
        real_run = central_mcp._run

        def fake_run(argv, extra_env=None, timeout=900):
            calls.append(list(argv))
            return "fake launchctl ok", 0

        central_mcp._run = fake_run

        # 总闸关掉后，连后台常驻开关都不许碰 launchd。
        control_state.set_keys(enabled=False, daemon_enabled=False)
        refused, rc = central_mcp.control_action("enable_daemon")
        assert rc == 2, f"总闸关掉后 enable_daemon 竟然返回 {rc}"
        assert "AUTOPILOT_DISABLED" in str(refused), refused
        assert calls == [], f"总闸关掉后仍去动了 launchd: {calls}"
        assert control_state.load()["daemon_enabled"] is False

        control_state.set_keys(enabled=True)

        payload, rc = central_mcp.control_action("enable_daemon")
        assert rc == 0, f"开启后台常驻失败: {payload}"
        assert calls and calls[-1][-1] == "--install", \
            f"开启后台常驻没真调 --install: {calls}"
        assert control_state.load()["daemon_enabled"] is True

        calls.clear()
        payload, rc = central_mcp.control_action("disable_daemon")
        assert rc == 0, f"关闭后台常驻失败: {payload}"
        assert calls and calls[-1][-1] == "--uninstall", \
            f"关闭后台常驻没真调 --uninstall: {calls}"
        assert control_state.load()["daemon_enabled"] is False

        # 真装不上时不能把偏好写成「已开启」。
        control_state.set_keys(daemon_enabled=False)

        def failing_run(argv, extra_env=None, timeout=900):
            return "launchctl bootstrap 失败", 1

        central_mcp._run = failing_run
        payload, rc = central_mcp.control_action("enable_daemon")
        assert rc == 1, f"launchd 安装失败却返回成功: {rc}"
        assert control_state.load()["daemon_enabled"] is False, \
            "launchd 安装失败却把偏好写成已开启"
    finally:
        # 打桩不还原会污染后面的测试：它们会拿到假的 launchctl 结果。
        central_mcp._run = real_run
        if old_home is None:
            os.environ.pop("KG_DAEMON_HOME", None)
        else:
            os.environ["KG_DAEMON_HOME"] = old_home
        if old_repo is None:
            os.environ.pop("KG_REPO", None)
        else:
            os.environ["KG_REPO"] = old_repo
        import shutil
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(project, ignore_errors=True)
    print("selftest ok: 后台常驻开关真启停 launchd，失败不谎报。")


def test_companion_contract():
    """菜单栏 Companion 只有一条通道：ops/companion_api.py。

    查三件事：打包元数据齐全（否则装出来是个没服务菜单的壳）、用户入口真的接上了
    （快捷键/截图/拖入/服务菜单，缺一个就是少一个入口）、总闸关掉后这个通道同样拒绝新需求。
    真编译 .app 需要 swiftc，默认跳过；设 AUTOPILOT_SELFTEST_COMPANION=1 会真编一次。
    """
    import importlib
    import json as _json
    import shutil

    companion = OPS.parent / "companion"
    build = companion / "build.sh"
    plist = companion / "Info.plist"
    source = companion / "Sources" / "CodexAutopilotCompanion" / "main.swift"
    assert build.is_file() and os.access(build, os.X_OK), "companion/build.sh 缺失或不可执行"
    assert plist.is_file() and source.is_file(), "Companion 打包元数据缺失"

    plist_text = plist.read_text(encoding="utf-8")
    for needle in ("LSUIElement", "sendToAutopilot",
                   "com.douxian.codex-autopilot.companion"):
        assert needle in plist_text, f"Info.plist 缺少 {needle}"

    source_text = source.read_text(encoding="utf-8")
    for needle in ("kVK_ANSI_A", "kVK_ANSI_S", "kVK_ANSI_D", "/usr/sbin/screencapture",
                   "registerForDraggedTypes", "servicesProvider", "companion_api",
                   # 附件必须看得见、撤得掉，不能退化成一个数字
                   "lastPathComponent", "xmark.circle.fill",
                   # 「打开 Codex」按 bundle id 激活，不去猜应用路径
                   "com.openai.codex", "openCodex",
                   # 主面板在 Codex 本体里：菜单栏只负责送回去，HTML 驾驶舱是备用
                   "openCodexPanel", "openCockpit", "onOpenCockpit",
                   # 仅在待办集合变化时发系统提醒，轮询不重复打扰
                   "UNUserNotificationCenter", "notifyIfNeeded",
                   "Codex 自动挡需要你处理", "lastAttentionFingerprint"):
        assert needle in source_text, f"Companion 源码缺少 {needle}"

    # 「完整面板」不许把浏览器里的驾驶舱当主面板直接打开。
    primary_panel = source_text.split("func openPanel()", 1)[1].split(
        "func openCockpit()", 1)[0]
    assert 'call(["panel"])' not in primary_panel, \
        "菜单栏「完整面板」又去开 HTML 驾驶舱了，主面板必须在 Codex 里"
    # Companion 不许自己造第二套任务账本或第二套状态文件。
    for forbidden in ("intake.jsonl", "ledger.jsonl", "control.json"):
        assert forbidden not in source_text, f"Companion 自己碰了 {forbidden}"

    api = OPS / "companion_api.py"
    home = tempfile.mkdtemp(prefix="kg-companion-home-")
    staged_repo = Path(tempfile.mkdtemp(prefix="kg-companion-repo-"))
    empty_repo = Path(tempfile.mkdtemp(prefix="kg-companion-empty-"))
    old_daemon_home = os.environ.get("KG_DAEMON_HOME")
    old_repo = os.environ.get("KG_REPO")
    os.environ["KG_DAEMON_HOME"] = home
    try:
        def api_run(*args, repo=None):
            env = os.environ.copy()
            if repo is not None:
                env["KG_REPO"] = str(repo)
            return run([sys.executable, str(api), *args],
                       cwd=str(OPS.parent), env=env, check=False)

        os.environ["KG_REPO"] = str(OPS.parent)
        status = _json.loads(api_run("status").stdout)
        assert status["ok"] is True and status["enabled"] is True
        assert status["lifecycle"] == "on"
        assert status["project"] == str(OPS.parent.resolve())
        for key in ("switches", "attention", "daemon", "recent_completed"):
            assert key in status, f"status 输出缺少 {key}"

        off = _json.loads(api_run("disable").stdout)
        assert off["enabled"] is False and off["lifecycle"] == "off"
        refused = api_run("intake", "总闸关掉后不该收需求")
        assert refused.returncode == 2 and "AUTOPILOT_DISABLED" in refused.stdout, \
            f"关掉总闸后 Companion 通道仍收需求: {refused.stdout}{refused.stderr}"
        back = _json.loads(api_run("enable").stdout)
        assert back["enabled"] is True and back["lifecycle"] == "on"

        # 附件与需求原话分开：--attach 进 files，不改写用户那句话。
        sys.path.insert(0, str(OPS))
        import companion_api
        importlib.reload(companion_api)
        text, files = companion_api._split_attachments(
            ["--attach", "/tmp/shot.png", "左边", "排版", "坏了", "-a", "/tmp/a.log"])
        assert text == "左边 排版 坏了", text
        assert files == ["/tmp/shot.png", "/tmp/a.log"], files
        assert companion_api._split_attachments(["--attach"]) == ("", [])

        # open status 指向真实状态文件；没有就如实报错，不编一个路径出来。
        (staged_repo / "docs").mkdir(parents=True)
        mainline = staged_repo / "docs" / "PRODUCT_MAINLINE.md"
        mainline.write_text("# 主线\n", encoding="utf-8")
        opened = _json.loads(api_run("open", "status", repo=staged_repo).stdout)
        # 状态根会被 resolve（macOS 的 /var 是指向 /private/var 的软链），按解析后的路径比。
        assert opened["ok"] is True and opened["path"] == str(mainline.resolve()), opened
        missing = api_run("open", "status", repo=empty_repo)
        assert missing.returncode == 1 and '"ok": false' in missing.stdout
        unknown = api_run("nonsense")
        assert unknown.returncode == 2 and "UNKNOWN_ACTION" in unknown.stdout
    finally:
        if old_daemon_home is None:
            os.environ.pop("KG_DAEMON_HOME", None)
        else:
            os.environ["KG_DAEMON_HOME"] = old_daemon_home
        if old_repo is None:
            os.environ.pop("KG_REPO", None)
        else:
            os.environ["KG_REPO"] = old_repo
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(staged_repo, ignore_errors=True)
        shutil.rmtree(empty_repo, ignore_errors=True)

    print("selftest ok: Companion 契约（打包元数据、快捷键/截图/拖入/服务入口、"
          "总闸关掉后同一通道拒收）。")

    if os.environ.get("AUTOPILOT_SELFTEST_COMPANION") == "1":
        built = run(["bash", str(build)], cwd=str(companion), check=False)
        assert built.returncode == 0, f"Companion 编译失败:\n{built.stdout}\n{built.stderr}"
        app = companion / "dist" / "CodexAutopilotCompanion.app"
        assert (app / "Contents" / "MacOS" / "CodexAutopilotCompanion").is_file()
        assert (app / "Contents" / "Info.plist").is_file()
        print("selftest ok: Companion 真编译出可安装的 .app。")


def test_panel_contract():
    """面板只通过 MCP 工具读写，而且它引用的每个元素都真实存在。

    这一条防的是「面板看着对、其实点不动」：id 对不上时按钮就是死的，
    HTML/JS 语法检查完全看不出来。同时也挡住「面板自己造第二套工具/开关」。
    """
    import re

    panel = OPS.parent / "ui" / "panel.html"
    if not panel.exists():
        panel = OPS.parent / "plugin" / "ui" / "panel.html"
    html = panel.read_text(encoding="utf-8")
    script = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", html))
    assert script.strip(), "面板里没有脚本，等于没接上控制层"

    dom_ids = set(re.findall(r'id="([^"]+)"', html))
    used_ids = set(re.findall(r'\bel\("([^"]+)"\)', script))
    created_ids = set(re.findall(r'\.id\s*=\s*"([^"]+)"', script))
    missing = sorted(used_ids - dom_ids - created_ids)
    assert not missing, f"面板引用了不存在的元素：{missing}"

    sys.path.insert(0, str(OPS))
    import central_mcp
    import control_state

    tool_names = {tool["name"] for tool in central_mcp.TOOLS}
    called = set(re.findall(r'callTool\("([^"]+)"', script))
    assert called <= tool_names, f"面板调了不存在的工具：{sorted(called - tool_names)}"

    switch_keys = set(re.findall(r'data-key="([^"]+)"', html))
    unknown = sorted(switch_keys - set(control_state.DEFAULTS))
    assert not unknown, f"面板有控制层不认识的开关：{unknown}"

    # 首屏必须能回答「当前绑定哪个项目」：面板元素、控制层字段、真实名字三边都在。
    assert "projectLine" in dom_ids, "面板首屏缺少绑定项目那一行"
    payload = central_mcp._status_payload(fast=True)
    project = payload.get("project")
    assert isinstance(project, dict), "控制层没有把绑定项目吐给面板"
    assert project.get("name"), "绑定项目没有名字，面板会显示未绑定"
    assert project.get("path"), "绑定项目没有路径，用户无法确认是哪一个"

    # 首屏必须按「项目 → 路线 → 工作项 → 验收」归口，而不是把所有台账混成一口锅。
    folders = payload.get("project_folders") or {}
    rows = folders.get("folders") or []
    assert rows, "控制层没有产出项目文件夹，面板无法按项目归类"
    folder = rows[0]
    assert folder.get("path") == project.get("path"), "项目文件夹没有绑定当前项目"
    assert folder.get("current_step"), "项目文件夹没有当前步骤"
    for key in ("doing_items", "todo_items", "human_acceptance", "counts"):
        assert key in folder, f"项目文件夹缺少 {key}"

    # 多项目不是把工作项堆在同一口锅：注册表另一个项目必须按自己的 path 单独出现。
    # 用临时状态根隔离测试，绝不写当前项目的真实注册表。
    with tempfile.TemporaryDirectory(prefix="autopilot-project-folder-") as tmp:
        state = Path(tmp) / "current"
        foreign = Path(tmp) / "foreign"
        (state / "ops" / "truth").mkdir(parents=True)
        (foreign / "ops" / "truth").mkdir(parents=True)
        import json as _json
        (state / "ops" / "truth" / "mainline.json").write_text(
            _json.dumps({"product": "当前产品"}, ensure_ascii=False), encoding="utf-8")
        (foreign / "ops" / "truth" / "mainline.json").write_text(
            _json.dumps({"product": "另一个产品"}, ensure_ascii=False), encoding="utf-8")
        (foreign / "ops" / "truth" / "ledger.jsonl").write_text(
            _json.dumps({"id": "L-9001", "title": "另一个项目的工作项", "status": "doing",
                         "source_thread_id": "thread-foreign"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        registry = state / "ops" / "truth" / "project_folders.json"
        registry.write_text(_json.dumps({"folders": [
            {"id": "foreign", "name": "另一个项目", "path": str(foreign),
             "active": False, "source_thread_id": "thread-foreign"}]}, ensure_ascii=False),
            encoding="utf-8")
        script = (
            "import json,sys\n"
            "sys.path.insert(0,%r)\n"
            "import central_mcp\n"
            "p=central_mcp._status_payload(fast=True)['project_folders']\n"
            "print(json.dumps(p,ensure_ascii=False))\n" % str(OPS))
        env = os.environ.copy()
        env["KG_REPO"] = str(state)
        probe = subprocess.run([sys.executable, "-c", script], cwd=str(OPS.parent), env=env,
                               capture_output=True, text=True)
        assert probe.returncode == 0, f"多项目状态探针失败：{probe.stderr[-500:]}"
        multi = _json.loads(probe.stdout.strip().splitlines()[-1])
        rows2 = multi.get("folders") or []
        assert len(rows2) >= 2, "注册表里的第二个项目没有出现在项目文件夹里"
        row = next((r for r in rows2
                    if Path(str(r.get("path") or "")).resolve() == foreign.resolve()), None)
        assert row is not None, "第二个项目没有按自己的 path 单独成组"
        assert row.get("readable") is True, "读得到的项目被标成不可读"
        assert row.get("doing_items") and row["doing_items"][0].get("id") == "L-9001", \
            "跨项目工作项没有按项目归属"
        assert row.get("source_thread_id") == "thread-foreign", "跨项目来源对话丢失"

    print(f"selftest ok: 面板元素（{len(used_ids)} 个）与工具（{len(called)} 个）"
          f"全部真实存在，项目文件夹按项目归口，开关项都在控制层里。")


def test_codex_native_bridge():
    """app-server 使用官方协议参数；历史数据库只读降级，不写宿主状态。"""
    import importlib
    import json as _json
    import sqlite3
    from unittest import mock

    sys.path.insert(0, str(OPS))
    import codex_native
    importlib.reload(codex_native)

    with tempfile.TemporaryDirectory(prefix="codex-fake-server-") as tmp:
        server = Path(tmp) / "fake_app_server.py"
        server.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    method, rid = req.get('method'), req.get('id')\n"
            "    if rid is None:\n"
            "        continue\n"
            "    if method == 'thread/start':\n"
            "        result = {'thread': {'id': 'thread-1', 'cwd': '/tmp'}}\n"
            "    elif method == 'turn/start':\n"
            "        result = {'turn': {'id': 'turn-1'}}\n"
            "    else:\n"
            "        result = {}\n"
            "    print(json.dumps({'id': rid, 'result': result}), flush=True)\n",
            encoding="utf-8",
        )
        proc, error = codex_native._open_session([sys.executable, str(server)])
        assert error is None and proc is not None
        try:
            created, error = codex_native._session_request(
                proc, "thread/start", {"cwd": "/tmp"})
            assert error is None and created["thread"]["id"] == "thread-1"
            moved, error = codex_native._session_request(
                proc, "thread/section/move",
                {"threadId": "thread-1", "sectionId": "section-1"})
            assert error is None and moved == {}
            started, error = codex_native._session_request(
                proc, "turn/start", {
                    "threadId": "thread-1",
                    "input": codex_native._turn_input("请实现并验证这个任务"),
                })
            assert error is None and started["turn"]["id"] == "turn-1"
            assert codex_native._turn_input("x") == [{"type": "text", "text": "x"}]
        finally:
            codex_native._close_session(proc)

    with mock.patch.object(codex_native, "_rpc", return_value=(None, "unavailable")):
        with tempfile.TemporaryDirectory(prefix="codex-history-") as tmp:
            db_path = Path(tmp) / "history.sqlite"
            with sqlite3.connect(db_path) as db:
                db.executescript("""
                    CREATE TABLE thread_turns (
                        thread_id TEXT, completed_at INTEGER, started_at INTEGER, status TEXT
                    );
                    CREATE TABLE thread_items (
                        thread_id TEXT, item_type TEXT, rollout_ordinal INTEGER, item_json TEXT
                    );
                    INSERT INTO thread_turns VALUES ('historic-thread', 20, 10, 'idle');
                    INSERT INTO thread_items VALUES (
                        'historic-thread', 'userMessage', 1, '{"text":"历史标题"}'
                    );
                """)
            old_db, codex_native.HISTORY_DB = codex_native.HISTORY_DB, db_path
            try:
                snapshot = codex_native.snapshot()
                assert snapshot["available"] is False
                assert snapshot["transport"] == "history-read-only"
                assert snapshot["threads"][0]["id"] == "historic-thread"
                with sqlite3.connect(db_path) as db:
                    assert db.execute("SELECT count(*) FROM thread_turns").fetchone()[0] == 1
            finally:
                codex_native.HISTORY_DB = old_db

    fake_proc = object()
    with mock.patch.object(codex_native, "_session", return_value=(fake_proc, None)) as session_mock, \
            mock.patch.object(codex_native, "_session_request", side_effect=[
                ({"thread": {"id": "thread-dispatch", "cwd": "/tmp"}}, None),
                ({}, None),
                ({"turn": {"id": "turn-dispatch"}}, None),
            ]) as request_mock, \
            mock.patch.object(codex_native, "_close_session") as close_mock:
        dispatched, error = codex_native.dispatch_task(
            "/tmp", "请实现并验证这个任务", "section-1")
        assert error is None
        assert dispatched["thread_id"] == "thread-dispatch"
        assert dispatched["turn_id"] == "turn-dispatch"
        session_mock.assert_called_once_with(host_only=True)
        assert [call.args[1] for call in request_mock.call_args_list] == [
            "thread/start", "thread/section/move", "turn/start"]
        assert "sectionId" not in request_mock.call_args_list[0].args[2]
        assert request_mock.call_args_list[1].args[2]["sectionId"] == "section-1"
        assert request_mock.call_args_list[2].args[2]["threadId"] == "thread-dispatch"
        close_mock.assert_called_once_with(fake_proc)

    with mock.patch.object(codex_native, "_session", return_value=(fake_proc, None)) as session_mock, \
            mock.patch.object(codex_native, "_session_request", side_effect=[
                ({"thread": {"id": "thread-dispatch"}}, None),
                ({"turn": {"id": "turn-follow-up"}}, None),
            ]) as request_mock, \
            mock.patch.object(codex_native, "_close_session"):
        continued, error = codex_native.follow_up("thread-dispatch", "继续处理并回报卡点")
        assert error is None and continued["turn"]["id"] == "turn-follow-up"
        assert [call.args[1] for call in request_mock.call_args_list] == [
            "thread/resume", "turn/start"]
        session_mock.assert_called_once_with(host_only=True)

    with mock.patch.object(codex_native, "_rpc", side_effect=[
        ({"thread": {"id": "thread-start", "cwd": "/tmp"}}, None),
        ({}, None),
    ]) as rpc_mock:
        started, error = codex_native.start_thread("/tmp", "section-1")
        assert error is None and started["section_id"] == "section-1"
        assert rpc_mock.call_args_list[0].args[1] == {
            "cwd": str(Path("/tmp").resolve())}
        assert rpc_mock.call_args_list[1].args[1] == {
            "threadId": "thread-start", "sectionId": "section-1"}

    with mock.patch.object(codex_native, "_session", return_value=(fake_proc, None)), \
            mock.patch.object(codex_native, "_session_request", side_effect=[
                ({"thread": {"id": "thread-orphan", "cwd": "/tmp"}}, None),
                (None, "turn/start rejected"),
            ]), mock.patch.object(codex_native, "_close_session"):
        partial, error = codex_native.dispatch_task("/tmp", "task")
        assert partial["thread_id"] == "thread-orphan" and partial["turn_id"] is None
        assert "thread 已创建" in error

    print("selftest ok: app-server 初始化顺序、原生窗口集合移动、turn/start 输入协议与只读历史降级通过。")


def test_native_socket_lstat():
    """socket 是 symlink 入口时也要能识别，不能把权限挡住的路径说成不存在。"""
    import importlib

    sys.path.insert(0, str(OPS))
    import codex_native
    importlib.reload(codex_native)

    real_socket = codex_native.SOCKET
    real_open = codex_native._open_session
    try:
        with tempfile.TemporaryDirectory(prefix="codex-native-socket-lstat-") as tmp:
            root = Path(tmp)
            codex_native._open_session = lambda command, timeout=30: (
                None, "Operation not permitted")

            # macOS 受限进程的真实形态：入口是 symlink，目标可被系统权限挡住。
            link = root / "codex-control.sock"
            link.symlink_to(root / "missing-target.sock")
            codex_native.SOCKET = link
            assert codex_native._socket_present(), "lstat 没识别 symlink 入口"
            state = codex_native.socket_state()
            assert state["socket_present"] is True, state
            result = codex_native.probe(timeout=1)
            assert result["socket_present"] is True, result
            assert result["connected"] is False, result
            assert "无法访问" in result["reason"], result
            assert "当前进程被系统权限挡住" in result["hint"], result

            # 普通文件与缺失路径仍要如实报“没有找到”，不能因放宽而谎报存在。
            plain = root / "plain.txt"
            plain.write_text("x", encoding="utf-8")
            codex_native.SOCKET = plain
            assert codex_native._socket_present() is False
            result = codex_native.probe(timeout=1)
            assert result["socket_present"] is False and "没有找到" in result["reason"], result

            codex_native.SOCKET = root / "absent.sock"
            assert codex_native._socket_present() is False
    finally:
        codex_native.SOCKET = real_socket
        codex_native._open_session = real_open
    print("selftest ok: socket symlink 入口按 lstat 识别，权限挡目标不再误报成不存在。")


FAKE_SECTION_SERVER = """
import json, os, sys
state_path = os.environ["FAKE_STATE"]
def load():
    try:
        with open(state_path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"sections": [], "moves": [], "creates": [], "threads": 0}
def save(data):
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    rid = req.get("id")
    if rid is None:
        continue
    method = req.get("method")
    params = req.get("params") or {}
    data = load()
    if method == "threadSection/list":
        if os.environ.get("FAKE_FAIL_SECTIONS"):
            print(json.dumps({"id": rid, "error": {"message": "section API unavailable"}}), flush=True)
            continue
        result = {"data": data["sections"]}
    elif method == "threadSection/create":
        name = params.get("name")
        data["creates"].append(name)
        section = {"id": "sec-%d" % (len(data["sections"]) + 1), "name": name}
        data["sections"].append(section)
        save(data)
        result = {"section": section}
    elif method == "thread/start":
        data["threads"] = data.get("threads", 0) + 1
        save(data)
        result = {"thread": {"id": "thread-%d" % data["threads"], "cwd": params.get("cwd")}}
    elif method == "thread/section/move":
        data["moves"].append({"threadId": params.get("threadId"), "sectionId": params.get("sectionId")})
        save(data)
        result = {}
    elif method == "turn/start":
        result = {"turn": {"id": "turn-1"}}
    else:
        result = {}
    print(json.dumps({"id": rid, "result": result}), flush=True)
"""

def test_native_section_grouping():
    """派活自动归入项目窗口集合：同名复用、不重复建；集合失败不拦派活但要如实记。

    用户要的是「窗口拉太多时自动给它们一个文件夹」。这里用真的 app-server 协议
    形状（threadSection/list + create + move）跑两遍，锁住「不是每次派活都新开一个」。
    """
    import importlib
    import json as _json

    sys.path.insert(0, str(OPS))
    import codex_native
    importlib.reload(codex_native)
    real_socket = codex_native.SOCKET
    codex_native.SOCKET = Path("/definitely/not/a/socket")

    with tempfile.TemporaryDirectory(prefix="codex-native-section-") as tmp:
        tmp_path = Path(tmp)
        server = tmp_path / "fake_app_server.py"
        server.write_text(FAKE_SECTION_SERVER, encoding="utf-8")
        state = tmp_path / "state.json"
        os.environ["FAKE_STATE"] = str(state)

        real_open = codex_native._open_session
        codex_native._open_session = lambda command, timeout=30: real_open(
            [sys.executable, str(server)], timeout=timeout)
        try:
            first, error = codex_native.dispatch_task(
                str(OPS.parent), "第一条派活", auto_section="Codex 自动挡 · 测试")
            assert error is None and first["thread_id"], first
            assert first["section_id"] == "sec-1", first
            assert first["section_note"] is None, first

            second, error = codex_native.dispatch_task(
                str(OPS.parent), "第二条派活", auto_section="Codex 自动挡 · 测试")
            assert error is None and second["section_id"] == "sec-1", second

            data = _json.loads(state.read_text(encoding="utf-8"))
            assert data["creates"] == ["Codex 自动挡 · 测试"], data["creates"]
            assert len(data["sections"]) == 1, data["sections"]
            assert [m["sectionId"] for m in data["moves"]] == ["sec-1", "sec-1"], data["moves"]

            # 集合 API 不可用时照常派活，但必须如实报出「没归组」，不能假装成功。
            os.environ["FAKE_FAIL_SECTIONS"] = "1"
            third, error = codex_native.dispatch_task(
                str(OPS.parent), "第三条派活", auto_section="Codex 自动挡 · 测试")
            assert error is None and third["thread_id"], third
            assert third["section_id"] is None, third
            assert third["section_note"] and "窗口集合" in third["section_note"], third
        finally:
            codex_native._open_session = real_open
            codex_native.SOCKET = real_socket
            os.environ.pop("FAKE_STATE", None)
            os.environ.pop("FAKE_FAIL_SECTIONS", None)

    print("selftest ok: 派活自动归入项目窗口集合、同名复用不重复建、集合失败不拦派活。")


def test_native_call_budget():
    """宿主「连得上但不回话」时，状态查询与开关不能被拖住。

    真实踩到的坑：桌面宿主 socket 可连、但不回包，于是
    `_route_summary -> snapshot()` 一路等到超时，用户点一下开关要卡半分钟。
    这里锁住三件事：给了预算就按时降级、候选服务不按个数翻倍等待、
    面板/开关那条快路根本不连宿主。
    """
    import importlib
    import io
    import os as _os
    import time as _time

    sys.path.insert(0, str(OPS))
    import codex_native
    importlib.reload(codex_native)
    real_socket = codex_native.SOCKET
    codex_native.SOCKET = Path("/definitely/not/a/socket")

    class FrozenProc:
        """活着但永远不说话（不发 EOF、不写 stdout）的假宿主进程。"""

        def __init__(self):
            read_fd, self._write_fd = _os.pipe()
            error_fd, self._error_write_fd = _os.pipe()
            self.stdout = _os.fdopen(read_fd, "r")
            self.stderr = _os.fdopen(error_fd, "r")
            self.stdin = io.StringIO()

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

        def close(self):
            self.stdout.close()
            self.stderr.close()
            _os.close(self._write_fd)
            _os.close(self._error_write_fd)

    frozen = FrozenProc()
    real_open = codex_native._open_session
    real_commands = codex_native._server_commands
    try:
        codex_native._open_session = lambda command, timeout=30: (frozen, None)
        started = _time.monotonic()
        state = codex_native.snapshot(timeout=1)
        elapsed = _time.monotonic() - started
        assert state["available"] is False, state
        assert state["reason"], "降级时必须如实说明原因，不能只说不可用"
        assert elapsed < 4, "snapshot 给了 1 秒预算却等了 %.1fs" % elapsed

        # 候选有多个时也不能按候选数翻倍：预算 1 秒就必须在 1 秒出头回来。
        attempts = []

        def slow_open(command, timeout=30):
            attempts.append(round(float(timeout), 2))
            _time.sleep(0.4)
            return None, "不回话"

        codex_native._open_session = slow_open
        codex_native._server_commands = (
            lambda include_isolated=True: [["a"], ["b"], ["c"]])
        started = _time.monotonic()
        _, error = codex_native._session(timeout=1)
        elapsed = _time.monotonic() - started
        assert error, "全都不回话时必须如实返回错误"
        assert len(attempts) < 3, "预算已经用完却还在试下一个候选：%s" % attempts
        assert attempts[1] < 0.9, "第二个候选没有拿到剩余预算：%s" % attempts
        assert elapsed < 2.5, "三个候选各等一次超时：%.1fs" % elapsed

        # 面板/开关那条快路给 0 预算：压根不连宿主，就没有「等它回话」这回事。
        connected = []
        codex_native._open_session = lambda command, timeout=30: (
            connected.append(command) or (None, "快路不该连宿主"))
        started = _time.monotonic()
        cached = codex_native.snapshot(timeout=0)
        elapsed = _time.monotonic() - started
        assert connected == [], "快路仍然去连了宿主：%s" % connected
        assert cached["available"] is False and cached["reason"], cached
        assert elapsed < 0.5, "快路不该等：%.1fs" % elapsed

        # 即使本机历史库巨大，快路也不许读它：历史兜底只在慢路降级时用。
        old_db = codex_native.HISTORY_DB
        codex_native.HISTORY_DB = Path("/definitely/not/a/small/history.sqlite")
        try:
            started = _time.monotonic()
            cached = codex_native.snapshot(timeout=0)
            elapsed = _time.monotonic() - started
            assert cached["threads"] == [], "快路读了历史兜底：%s" % cached["threads"]
            assert elapsed < 0.5, "快路被历史库拖住：%.1fs" % elapsed
        finally:
            codex_native.HISTORY_DB = old_db
    finally:
        codex_native._open_session = real_open
        codex_native._server_commands = real_commands
        codex_native.SOCKET = real_socket
        frozen.close()
    print("selftest ok: 宿主不回话时状态查询按预算降级，快路不连宿主、候选不翻倍。")


def test_real_codex_app_server():
    """真二进制协议验证：真实 codex app-server 能按我们的调用建出原生会话。

    默认跳过：换一台机器没装 codex CLI 时不该让主自检变红。设
    AUTOPILOT_SELFTEST_REAL_CODEX=1 真跑。它在一个可写的临时状态目录里跑，
    不碰用户的 ~/.codex，也不会创建用户桌面窗口，只证明协议真的对得上官方实现。
    """
    if os.environ.get("AUTOPILOT_SELFTEST_REAL_CODEX") != "1":
        print("selftest skip: 真二进制协议验证默认跳过"
              "（设 AUTOPILOT_SELFTEST_REAL_CODEX=1 开启）。")
        return

    import importlib
    import re as _re
    import shutil as _shutil

    cli = os.environ.get("CODEX_CLI", "codex")
    if not _shutil.which(cli):
        print("selftest skip: 没装 codex CLI，跳过真二进制协议验证。")
        return

    sys.path.insert(0, str(OPS))
    import codex_native

    with tempfile.TemporaryDirectory(prefix="codex-real-home-") as home:
        old_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = home
        try:
            importlib.reload(codex_native)
            proc, error = codex_native._open_session([cli, "app-server"])
            assert proc is not None, f"真实 codex app-server 起不来：{error}"
            try:
                started, error = codex_native._session_request(
                    proc, "thread/start", {"cwd": str(OPS.parent)})
                assert error is None, error
                thread_id = ((started or {}).get("thread") or {}).get("id")
                assert thread_id, started

                # 窗口集合真链路：创建 → 把会话移进去 → 列出来必须在。
                # 方法名或参数写错，这里会直接失败而不是在用户机器上才发现。
                created, error = codex_native._session_request(
                    proc, "threadSection/create", {"name": "Codex 自动挡 · 自检"})
                assert error is None, error
                section = (created or {}).get("section") or {}
                assert section.get("id"), created

                moved, move_error = codex_native._session_request(
                    proc, "thread/section/move",
                    {"threadId": thread_id, "sectionId": section["id"]})
                if move_error:
                    # 隔离状态目录里会话还没落盘时，服务器仍必须「认识」这个方法：
                    # 报的只能是找不到会话，绝不能是未知方法。
                    assert "unknown variant" not in move_error, move_error
                    assert "not found" in move_error.lower(), move_error

                listed, error = codex_native._session_request(
                    proc, "threadSection/list", {"limit": 100})
                assert error is None, error
                names = [s.get("name") for s in ((listed or {}).get("data") or [])]
                assert "Codex 自动挡 · 自检" in names, names

                # 项目里所有发往原生服务的方法名都必须真实存在。
                # 防的是「假服务器认识、真服务器不认」这种只在用户机器上炸的写法。
                source = (OPS / "codex_native.py").read_text(encoding="utf-8")
                used = set(_re.findall(r'_rpc\(\s*"([^"]+)"', source)) | set(
                    _re.findall(r'_session_request\(\s*proc,\s*"([^"]+)"', source))
                assert used, "没抓到任何原生方法名，检查本身失效了"
                _, unknown_error = codex_native._session_request(
                    proc, "___autopilot_unknown___", {})
                match = _re.search(r"expected one of (.+)$", unknown_error or "")
                accepted = set(_re.findall(r"`([^`]+)`", match.group(1))) if match else set()
                assert accepted, f"拿不到服务器接受的方法列表：{unknown_error}"
                unknown_methods = sorted(m for m in used if m not in accepted)
                assert not unknown_methods, \
                    f"这些原生方法名在真实服务上不存在：{unknown_methods}"
            finally:
                codex_native._close_session(proc)
        finally:
            if old_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old_home
            importlib.reload(codex_native)

    print("selftest ok: 真实 codex app-server 建出原生会话与窗口集合，"
          "并核对项目里所有原生方法名都真实存在。")


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

    assert central_config.verify_gates({"verify": [
        {"name": "build", "cmd": "npm run build", "cwd": ".", "always": True}]}) == [
        {"name": "build", "cmd": "npm run build", "cwd": ".", "always": True}]

    by_change = central_config.verify_gates_for_changes(
        {"verify": [{"name": "build", "cmd": "npm run build", "cwd": ".", "always": True}],
         "verify_by_change": {"enabled": True, "rules": [
             {"name": "benchmark", "paths": ["backend/test/benchmark-*"],
              "cmd": "npm run verify:benchmark-quality-gate", "cwd": "backend"}]}},
        ["backend/test/benchmark-zero-source-provenance.mjs"])
    assert [g["name"] for g in by_change] == ["build", "benchmark"], by_change
    fallback = central_config.verify_gates_for_changes(
        {"verify": [{"name": "build", "cmd": "npm run build", "cwd": "."}],
         "verify_by_change": {"enabled": True, "rules": [
             {"name": "frontend", "paths": ["frontend/**"], "cmd": "npm run build", "cwd": "frontend"}]}},
        ["backend/test/other.mjs"])
    assert [g["name"] for g in fallback] == ["build"], fallback
    assert central_config.is_network_unreachable(
        "fatal: unable to access ...: SSL connection timeout")
    assert central_config.is_network_unreachable(
        "fatal: unable to access ...: Empty reply from server")
    assert not central_config.is_network_unreachable(
        "fatal: authentication failed")

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


def test_prune_exec_blocked_landed_commit():
    """A commit already in main must close the ledger and clear stale blocking."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = base / "repo"
        repo.mkdir()
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "truth" / "ledger.jsonl").write_text(
            json.dumps({"id": "L-TEST", "type": "ledger", "status": "open",
                         "ts": "2026-01-01 00:00", "title": "landed"}) + "\n",
            encoding="utf-8")
        (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(repo, "add", "a.txt", "ops/truth/ledger.jsonl", "ops/truth/progress.jsonl")
        git(repo, "commit", "-q", "-m", "init")
        commit = git(repo, "rev-parse", "HEAD").stdout.strip()
        old_repo = os.environ.get("KG_REPO")
        os.environ["KG_REPO"] = str(repo)
        try:
            import central_daemon
            old_git = central_daemon._git
            central_daemon._git = lambda _repo, *args, **_kw: (
                run(["git", "-C", str(repo), *args]).returncode, "", "")
            blocked = {"L-TEST": {"reason": "land_error", "commit": commit, "ts": "now"}}
            stale = central_daemon._prune_exec_blocked(blocked)
            assert "L-TEST" in stale, blocked
            assert not blocked, blocked
            central_daemon._git = old_git
            latest = {r["id"]: r for r in central_daemon._ledger_latest().values()}
            assert latest["L-TEST"]["status"] == "done", latest
        finally:
            if old_repo is None:
                os.environ.pop("KG_REPO", None)
            else:
                os.environ["KG_REPO"] = old_repo
    print("selftest ok: 已在 main 的提交会自动收口并清掉陈旧 land_error。")


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

    def fake_codex_throttled(base, name):
        """第一次 429 留下半成品，第二次在同一 worktree 完成。"""
        path = base / name
        counter = base / f"{name}.count"
        path.write_text(
            "#!/bin/sh\n"
            "wt=\"\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-C\" ]; then wt=\"$2\"; shift 2; else shift; fi\n"
            "done\n"
            f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
            "n=$((n + 1))\n"
            f"echo $n > {counter}\n"
            "if [ \"$n\" = \"1\" ]; then\n"
            "  printf 'partial\\n' > \"$wt/partial.txt\"\n"
            "  echo 'ERROR: unexpected status 429 Too Many Requests'\n"
            "  exit 1\n"
            "fi\n"
            "printf 'complete\\n' >> \"$wt/partial.txt\"\n"
            "printf 'fake codex\\n' > \"$wt/fake.txt\"\n"
            "exit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path, counter

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
        # 宿主磁盘余量不属于这些用例的断言对象：真实低盘时它们会假失败。
        # 磁盘闸本身由 test_parallel_exec_slots 用显式 mock 单独锁住，
        # 这里只把普通派发路径固定在“余量充足”。
        central_daemon._disk_free_gb = lambda: 20.0
        # 每个 fixture 的 worktree 都叫 L-0001-item；上一个 fixture 的 worker
        # 可能还在退出过程中。模块级进程缓存若不重置，会把旧 worktree 误当成本
        # fixture 的活进程，跳过派发并直接清掉新分支。
        central_daemon._LIVE_EXEC_CACHE.update(ts=0.0, slugs=frozenset())
        return central_daemon

    def dispatch_until_done(daemon, blocked, workers=None, limit=100, pause=0.1):
        """Drive the asynchronous supervisor until it reports a finished item.

        Production intentionally returns after starting a slot and reaps it on
        a later cycle. Tests need the same two-phase lifecycle rather than the
        old assumption that one call blocks until landing.
        """
        import time as _time
        workers = workers if workers is not None else {}
        results = []
        for _ in range(limit):
            # dispatch_exec 返回 (results, workers)：只累积「收口结果」，
            # workers 由调用方持有并被本函数就地更新。
            _res, workers = daemon.dispatch_exec(False, blocked, workers)
            results.extend(_res)
            done = {"landed", "codex_fail_preserved", "codex_fail",
                    "landed_close_fail", "no_changes"}
            if any(r.get("outcome") in done for r in results):
                return results
            _time.sleep(pause)
        raise AssertionError(f"worker 在 {limit} 拍内没有完成: {results}, jobs={workers}")

    def ledger_latest(repo):
        recs = {}
        for line in (repo / "ops" / "truth" / "ledger.jsonl").read_text(
                encoding="utf-8").splitlines():
            if line.strip():
                r = _json.loads(line)
                recs[r["id"]] = r
        return recs

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-exec-") as tmp:
        base = Path(tmp)

        # --- 空分支等于 main 不等于需求已完成：必须重新派 worker，不能自动收口 ---
        # 真实事故：L-0012/L-0029 等九条仍 open 的需求，被“没有独有提交”的
        # 陈旧分支误判为已落盘。这里预造一条指向 main 的同名分支，断言派发后
        # 台账仍是 open，且真的启动了执行会话。
        repo_s, trunk_s, home_s = fixture(base / "stale-branch")
        daemon_s = load_daemon(
            repo_s, trunk_s, home_s, fake_codex(base, "fake-codex-stale-branch", 0))
        stale_item = {"id": "L-0001", "kind": "bug",
                      "title": "面板按钮点着没反应"}
        stale_cfg = daemon_s.auto_exec_config()
        stale_branch, _, _ = daemon_s._exec_target(stale_item, stale_cfg, repo_s)
        git(repo_s, "branch", stale_branch, "main")
        stale_blocked = {}
        stale_started, stale_workers = daemon_s.dispatch_exec(
            False, stale_blocked, {})
        stale_latest = ledger_latest(repo_s)
        if stale_latest["L-0001"]["status"] != "open":
            raise AssertionError(
                f"陈旧空分支被误收口: {stale_latest['L-0001']}")
        if "L-0001" not in stale_workers.get("exec_jobs", {}):
            raise AssertionError(
                f"陈旧空分支没有重派真实 worker: {stale_started} {stale_workers}")
        dispatch_until_done(daemon_s, stale_blocked, stale_workers)

        # --- 派发提示必须把“小任务第一步先落盘”写进硬规则，防止只读检查冒充完成 ---
        # L-0006 真机现场：codex 退出码 0、只跑 ls/find/git status 后停手，自动挡如实记
        # no_changes。只有提示词有这条约束还不够，但提示词缺了它，模型连被要求落盘都没有。
        repo_prompt, _, _ = fixture(base / "prompt")
        daemon_prompt = load_daemon(repo_prompt, base / "prompt-trunk",
                                    base / "prompt-home",
                                    fake_codex(base, "fake-prompt.sh", 0))
        prompt_text = daemon_prompt._exec_prompt({
            "id": "L-0001", "kind": "gate", "title": "写 verify.txt"})
        for needle in ("第一步就要在当前 worktree 产出改动",
                       "只读检查不算完成",
                       "无需改动"):
            if needle not in prompt_text:
                raise AssertionError(f"派发提示缺少强制落盘规则: {needle!r}\n{prompt_text}")

        # --- 成功路径：实现 -> 提交 -> 落盘 -> 收口，台账变 done，main 里有产物 ---
        repo, trunk, home = fixture(base / "ok")
        daemon = load_daemon(repo, trunk, home, fake_codex(base, "fake-ok.sh", 0))
        blocked = {}
        results = dispatch_until_done(daemon, blocked)
        if not results or results[-1].get("outcome") != "landed":
            raise AssertionError(f"成功路径没有落成 landed: {results}\nblocked={blocked}")
        if ledger_latest(repo)["L-0001"].get("status") != "done":
            raise AssertionError(f"落盘后台账没有收口: {ledger_latest(repo)}")
        if git(repo, "show", "main:fake.txt").stdout.strip() != "fake codex":
            raise AssertionError("main 里没有执行器写下的产物")
        if blocked:
            raise AssertionError(f"成功路径不应留下 blocked: {blocked}")
        if git(repo, "branch", "--list", "codex/exec-*").stdout.strip():
            raise AssertionError("成功路径应回收执行分支")

        # --- 失败路径：写了一半就崩，必须保留分支和 worktree，绝不能丢在途工作 ---
        repo_f, trunk_f, home_f = fixture(base / "fail")
        daemon_f = load_daemon(repo_f, trunk_f, home_f,
                               fake_codex(base, "fake-fail.sh", 7))
        blocked_f = {}
        results_f = dispatch_until_done(daemon_f, blocked_f)
        if results_f[-1].get("outcome") != "codex_fail_preserved":
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
        results_c = dispatch_until_done(daemon_c, blocked_c)
        if results_c[-1].get("outcome") != "landed_close_fail":
            raise AssertionError(f"收口失败被误报成成功: {results_c}")
        if blocked_c.get("L-0001", {}).get("reason") != "landed_close_fail":
            raise AssertionError(f"收口失败原因登记不正确: {blocked_c}")
        if not Path(blocked_c["L-0001"]["worktree"]).exists():
            raise AssertionError("收口失败删掉了现场，无法复核")
        if ledger_latest(repo_c)["L-0001"].get("status") == "done":
            raise AssertionError("收口失败却把台账标成 done")
        # 只看「收口结果」：dispatch_exec 返回 (results, workers)，
        # 元组本身永远为真，直接判真会把空结果误判成重复派发。
        again, _w = daemon_c.dispatch_exec(False, blocked_c)
        if again:
            raise AssertionError(f"收口失败后台账项被重复派发: {again}")

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
        results_p = dispatch_until_done(daemon_p, blocked_p)
        if not results_p or results_p[-1].get("outcome") != "landed":
            raise AssertionError(
                f"分支已有未落提交时没有直接收口: {results_p}\nblocked={blocked_p}")
        if git(repo_p, "show", "main:fake.txt").stdout.strip() != "preserved":
            raise AssertionError("直接收口没有把已有提交落进 main（疑似重复实现覆盖了成果）")
        if ledger_latest(repo_p)["L-0001"].get("status") != "done":
            raise AssertionError(f"直接收口后台账没有收口: {ledger_latest(repo_p)}")
        if git(repo_p, "branch", "--list", branch_p).stdout.strip():
            raise AssertionError("直接收口后执行分支没有回收")

        # --- worktree 已回收但分支还有未落提交：恢复槽不能永久占住并发空转 ---
        # 守护重启后从 state.json 还原 recover job，但上一轮 worker 的 worktree
        # 已被删除时，_hydrate_exec_jobs 应把该槽转成 detached 收口，而不是清掉后
        # 每轮重复 dispatch。锁定「分支未落 -> 直接收口」的自愈路径。
        repo_h, trunk_h, home_h = fixture(base / "hydrate-dead-recover")
        daemon_h = load_daemon(repo_h, trunk_h, home_h,
                               fake_codex(base, "fake-recover-done.sh", 0))
        slug_h = f"{daemon_h._exec_slug('L-0001')}-{daemon_h._exec_slug('面板按钮点着没反应')}"
        branch_h = f"codex/exec-{slug_h}"
        wt_h = Path(repo_h).parent / f"{Path(repo_h).name}-exec-{slug_h}"
        run(["git", "-C", str(repo_h), "worktree", "add", "-q", "-b",
             branch_h, str(wt_h)])
        (wt_h / "fake.txt").write_text("recover-preserved\n", encoding="utf-8")
        git(wt_h, "add", "fake.txt")
        git(wt_h, "commit", "-q", "-m", "exec(L-0001): 面板按钮点着没反应")
        run(["git", "-C", str(repo_h), "worktree", "remove", "--force", str(wt_h)])
        stale_jobs = {"exec_jobs": {"L-0001": {
            "id": "L-0001", "outcome": "ready",
            "item": daemon_h._exec_open_items().__next__(),
            "cfg": daemon_h.auto_exec_config(), "repo": str(repo_h),
            "branch": branch_h, "worktree": str(wt_h),
            "log": str(home_h / "exec" / "recover.log"),
            "model": "aibvv@deepseek-v4.1-flash", "recover": True,
            "result": {"id": "L-0001", "branch": branch_h,
                       "worktree": str(wt_h)}}}}
        hydrated = daemon_h._hydrate_exec_jobs(stale_jobs)
        if not hydrated["exec_jobs"]:
            raise AssertionError("recover 槽不应在分支仍有未落提交时被直接清掉")
        blocked_h = {}
        results_h = [daemon_h._reap_exec(
            list(hydrated["exec_jobs"].values())[0], blocked_h)]
        if results_h[-1].get("outcome") != "landed":
            raise AssertionError(
                f"recover 槽没有直接收口已提交成果: {results_h}\nblocked={blocked_h}")
        if git(repo_h, "show", "main:fake.txt").stdout.strip() != "recover-preserved":
            raise AssertionError("recover 槽没有把已提交成果落进 main")

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
        results_r = dispatch_until_done(daemon_r, blocked_r)
        if results_r[-1].get("outcome") != "landed":
            raise AssertionError(f"抖动后重试没有落成 landed: {results_r}\nblocked={blocked_r}")
        if blocked_r:
            raise AssertionError(f"抖动后重试成功不应留下 blocked: {blocked_r}")
        wts = [l for l in wt_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        if len(wts) != 2 or len(set(wts)) != 1:
            raise AssertionError(f"重试没有落在同一个 worktree 上: {wts}")
        if git(repo_r, "show", "main:fake.txt").stdout.strip() != "fake codex":
            raise AssertionError("重试成功后 main 里没有执行器的产物")
        slug_r = f"{daemon_r._exec_slug('L-0001')}-{daemon_r._exec_slug('面板按钮点着没反应')}"
        exec_log = (home_r / "exec" / f"{slug_r}.log").read_text(encoding="utf-8")
        if "502 Bad Gateway" not in exec_log or "重试 第 2/6 次" not in exec_log:
            raise AssertionError(f"重试没有保留第一次的现场证据: {exec_log[-600:]}")
        daemon_log = (home_r / "daemon.log").read_text(encoding="utf-8")
        if "第 2/6 次重试" not in daemon_log or str(home_r / "exec" / f"{slug_r}.log") not in daemon_log:
            raise AssertionError(f"daemon.log 没写明第几次重试 / 日志路径: {daemon_log[-800:]}")

        # --- 退出 0 但无改动：有限重试并要求真实落盘，不把只读检查当成完成 ---
        # L-0006/L-0001 真机现场：模型代理返回 0，但没有执行任何写命令，自动挡以前直接记
        # no_changes 并永久暂缓。修复后应在同一 worktree 重跑提示，第二次仍无改动才收口。
        repo_n, trunk_n, home_n = fixture(base / "no-change-retry",
                                         exec_cfg={"no_change_retries": 1})
        fake_nc = base / "fake-no-change.sh"
        fake_nc_count = base / "fake-no-change.count"
        fake_nc.write_text(
            "#!/bin/sh\n"
            "wt=\"\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-C\" ]; then wt=\"$2\"; shift 2; else shift; fi\n"
            "done\n"
            f"n=$(cat {fake_nc_count} 2>/dev/null || echo 0)\n"
            "n=$((n + 1))\n"
            f"echo $n > {fake_nc_count}\n"
            "if [ \"$n\" = \"1\" ]; then\n"
            "  exit 0\n"
            "fi\n"
            "printf 'retry wrote file\\n' > \"$wt/fake.txt\"\n"
            "exit 0\n", encoding="utf-8")
        fake_nc.chmod(0o755)
        daemon_n = load_daemon(repo_n, trunk_n, home_n, fake_nc)
        blocked_n = {}
        results_n = dispatch_until_done(daemon_n, blocked_n)
        if results_n[-1].get("outcome") != "landed" or results_n[-1].get("attempts") != 2:
            raise AssertionError(f"无改动重试没有最终落盘: {results_n}\nblocked={blocked_n}")
        if blocked_n:
            raise AssertionError(f"无改动重试成功后不应留下 blocked: {blocked_n}")
        if git(repo_n, "show", "main:fake.txt").stdout.strip() != "retry wrote file":
            raise AssertionError("无改动重试后 main 里没有第二次执行器产物")

        # --- 重试到上限仍抖动：reason 记 codex_infra_fail（与 codex_exit_1 区分）、现场保留、不重复派 ---
        repo_e, trunk_e, home_e = fixture(base / "infra-exhaust")
        daemon_e = load_daemon(repo_e, trunk_e, home_e,
                               fake_codex_infra(base, "fake-infra.sh"))
        daemon_e.EXEC_INFRA_BACKOFF = 0
        blocked_e = {}
        results_e = dispatch_until_done(daemon_e, blocked_e)
        if results_e[-1].get("outcome") != "codex_fail_preserved":
            raise AssertionError(f"抖动失败没有保留现场: {results_e}")
        if results_e[-1].get("attempts") != 4:
            raise AssertionError(f"抖动失败没有重试到默认上限（1+3）: {results_e}")
        if blocked_e.get("L-0001", {}).get("reason") != "codex_infra_fail":
            raise AssertionError(f"抖动失败原因没有与 codex_exit_N 区分: {blocked_e}")
        if "502 Bad Gateway" not in (blocked_e.get("L-0001", {}).get("detail") or ""):
            raise AssertionError(f"codex_infra_fail 没有留下抖动原文证据: {blocked_e}")
        if not Path(blocked_e["L-0001"]["worktree"]).exists():
            raise AssertionError("抖动失败丢掉了在途 worktree")
        again_e, _w = daemon_e.dispatch_exec(False, blocked_e)
        if again_e:
            raise AssertionError(f"codex_infra_fail 后被重复派发: {again_e}")

        # --- 429 是短期通道状态：不 sleeping 阻塞整条收口线程、不永久 codex_fail，
        # 退避到期后在原 worktree 重开会话并把半成品继续做完 ---
        repo_q, trunk_q, home_q = fixture(base / "infra-throttle")
        throttle_cmd, throttle_count = fake_codex_throttled(base, "fake-throttle.sh")
        daemon_q = load_daemon(repo_q, trunk_q, home_q, throttle_cmd)
        daemon_q.EXEC_CHANNEL_RETRY_SECONDS = 3600
        blocked_q, workers_q = {}, {}
        daemon_q.dispatch_exec(False, blocked_q, workers_q)
        results_q = []
        for _ in range(100):
            results_q, workers_q = daemon_q.dispatch_exec(False, blocked_q, workers_q)
            if results_q:
                break
            _time.sleep(0.1)
        if not results_q or results_q[-1].get("outcome") != "channel_unavailable":
            raise AssertionError(f"429 没被记成可恢复通道失败: {results_q}, blocked={blocked_q}")
        if int(throttle_count.read_text().strip()) != 1:
            raise AssertionError("429 在收口线程内被重复重试，阻塞了其他 worker")
        if blocked_q.get("L-0001", {}).get("reason") != "channel_unavailable":
            raise AssertionError(f"429 被误记为永久阻塞: {blocked_q}")
        if "codex_fail" in str(results_q[-1]):
            raise AssertionError(f"429 仍被记成 codex_fail: {results_q}")
        before_due, workers_q = daemon_q.dispatch_exec(False, blocked_q, workers_q)
        if before_due or workers_q.get("exec_jobs"):
            raise AssertionError(f"429 在退避到期前被提前重派: {before_due}, {workers_q}")

        blocked_q["L-0001"]["retry_after_ts"] = 0
        retry_started, workers_q = daemon_q.dispatch_exec(False, blocked_q, workers_q)
        retry_job = (workers_q.get("exec_jobs") or {}).get("L-0001")
        if retry_started or not retry_job:
            raise AssertionError(f"429 退避到期没有重开会话: {retry_started}, {workers_q}")
        if retry_job.get("recover"):
            raise AssertionError("429 重试被误当成 recover，会绕过 Codex 直接落半成品")
        retry_results = []
        for _ in range(100):
            retry_results, workers_q = daemon_q.dispatch_exec(False, blocked_q, workers_q)
            if retry_results:
                break
            _time.sleep(0.1)
        if not retry_results or retry_results[-1].get("outcome") != "landed":
            raise AssertionError(f"429 后同一 worktree 没有继续完成并落盘: {retry_results}")
        if int(throttle_count.read_text().strip()) != 2:
            raise AssertionError("429 后没有恰好重开一次 Codex 会话")
        partial_q = git(repo_q, "show", "main:partial.txt").stdout
        if "partial" not in partial_q or "complete" not in partial_q:
            raise AssertionError(f"429 重试丢了第一次半成品或没有继续完成: {partial_q!r}")
        daemon_q_log = (home_q / "daemon.log").read_text(encoding="utf-8")
        if "不记 codex_fail，不压低全局并发" not in daemon_q_log:
            raise AssertionError("daemon.log 没有记录 429 的可恢复语义")

        # --- retries 可通过项目配置收紧；普通失败（exit 7，无抖动文本）不受影响 ---
        repo_x, trunk_x, home_x = fixture(base / "infra-retries-1",
                                         exec_cfg={"retries": 0})
        daemon_x = load_daemon(repo_x, trunk_x, home_x,
                               fake_codex_infra(base, "fake-infra-x.sh"))
        daemon_x.EXEC_INFRA_BACKOFF = 0
        blocked_x = {}
        results_x = dispatch_until_done(daemon_x, blocked_x)
        if results_x[-1].get("attempts") != 1:
            raise AssertionError(f"retries=0 时仍重试了: {results_x}")
        if blocked_x.get("L-0001", {}).get("reason") != "codex_infra_fail":
            raise AssertionError(f"retries=0 的抖动失败原因不对: {blocked_x}")

        # --- 超时护栏不变：即使日志里有 502 文本，超时仍归 codex_exit_124 且不重试 ---
        repo_t, trunk_t, home_t = fixture(base / "infra-timeout",
                                         exec_cfg={"timeout": 1})
        daemon_t = load_daemon(repo_t, trunk_t, home_t,
                               fake_codex_hang(base, "fake-hang.sh"))
        daemon_t.EXEC_INFRA_BACKOFF = 0
        original_wait = daemon_t._reap_exec
        def reap_with_timeout(job, blocked):
            # The caller's poll() gate can see the fake process as still alive.
            # Drive the production timeout/reap branch directly for this probe.
            try:
                job["proc"].wait(timeout=job["cfg"]["timeout"])
            except daemon_t.subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(job["proc"].pid), 9)
                except OSError:
                    job["proc"].kill()
                job["proc"].wait()
                job["proc"].returncode = 124
                try:
                    job["fh"].close()
                except (AttributeError, OSError):
                    pass
                job.pop("fh", None)
                result = job["result"]
                result.update({"exit": 124, "attempts": 1, "outcome": "codex_exit_124"})
                daemon_t._preserve_exec(Path(job["repo"]), Path(job["worktree"]),
                                        job["branch"], job["id"], "codex_exit_124", blocked)
                return result
            return original_wait(job, blocked)
        daemon_t._reap_exec = reap_with_timeout
        blocked_t = {}
        workers_t = {}
        started_t, workers_t = daemon_t.dispatch_exec(False, blocked_t, workers_t)
        if started_t:
            raise AssertionError(f"超时 worker 不应在同拍返回结果: {started_t}")
        job_t = workers_t["exec_jobs"]["L-0001"]
        results_t = [daemon_t._reap_exec(job_t, blocked_t)]
        if results_t[-1].get("exit") != 124 or results_t[-1].get("attempts") != 1:
            raise AssertionError(f"超时没有仍归 124 / 或误重试了: {results_t}")
        if blocked_t.get("L-0001", {}).get("reason") != "codex_exit_124":
            raise AssertionError(f"超时原因登记不正确: {blocked_t}")


    print("selftest ok: 自动实现真跑并落盘、失败保留现场、收口失败不重复派、"
          "分支已有未落提交时直接收口、收口后清掉实现暂缓、"
          "502 日志判定为可重试并在同一 worktree 重跑（重试到上限记 codex_infra_fail）。")


def test_exec_model_balance_fallback():
    """403 欠费日志必须被识别为慢通道 fallback，且 fast/slow 路由可预测。"""
    import importlib
    import sys
    from pathlib import Path
    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import central_daemon
    importlib.reload(central_daemon)
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-model-fallback-") as tmp:
        probe = Path(tmp) / "403.log"
        probe.write_text(
            "ERROR: unexpected status 403 Forbidden: insufficient balance, url: "
            "http://127.0.0.1:15721/v1/responses\n", encoding="utf-8")
        if not central_daemon.classify_exec_model_fallback(probe):
            raise AssertionError("403 insufficient balance 没有被识别为模型通道额度失败")
        probe.write_text(
            "ERROR: unexpected status 403 Forbidden: insufficient balance\n"
            "===== auto_exec 模型通道额度失败，切换到 slow_model 重试 =====\n"
            "验证通过\n", encoding="utf-8")
        if central_daemon.classify_exec_model_fallback(probe):
            raise AssertionError("slow_model 重试后的旧 403 仍被误判为当前失败")
        probe.write_text(
            "ERROR: unexpected status 403 Forbidden: insufficient balance, url: "
            "http://127.0.0.1:15721/v1/responses\n", encoding="utf-8")
        job = {"model": "aibvv@deepseek-v4.1-flash"}
        cfg = {"fast_model": "aibvv@deepseek-v4.1-flash",
               "slow_model": "ok8888@deepseek-v4.1-flash"}
        if not central_daemon._should_fallback_to_slow_model(job, cfg, probe):
            raise AssertionError("fast 通道欠费后没有切到 slow_model")
        job["model"] = cfg["slow_model"]
        job["model_fallback_used"] = True
        if central_daemon._should_fallback_to_slow_model(job, cfg, probe):
            raise AssertionError("同一任务不应在 slow_model 上循环 fallback")
        quiet = Path(tmp) / "quiet.log"
        quiet.write_text("验证通过\n", encoding="utf-8")
        if central_daemon.classify_exec_model_fallback(quiet):
            raise AssertionError("普通日志被误判成模型通道额度失败")
        bug_item = {"kind": "bug"}
        gate_item = {"kind": "gate"}
        arch_item = {"kind": "arch"}
        if central_daemon._exec_model(bug_item, cfg) != cfg["fast_model"]:
            raise AssertionError("bug 泳道没有优先走 fast_model")
        if central_daemon._exec_model(gate_item, cfg) != cfg["fast_model"]:
            raise AssertionError("gate 泳道没有优先走 fast_model")
        if central_daemon._exec_model(arch_item, cfg) != cfg["slow_model"]:
            raise AssertionError("arch/其它泳道没有走 slow_model")
    print("selftest ok: 403 欠费即使退出码为 0 也切到 slow_model，且不会循环回退。")


def test_exec_model_fallback_relaunch_is_async():
    """A balance fallback must relaunch without blocking the daemon loop."""
    import importlib
    from unittest import mock
    import sys
    from pathlib import Path

    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import central_daemon
    importlib.reload(central_daemon)

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-async-fallback-") as tmp:
        base = Path(tmp)
        repo, wt = base / "repo", base / "worktree"
        repo.mkdir(); wt.mkdir()
        # 本用例验「fallback 重挂是异步的」，不验磁盘闸；宿主真实低盘会让它假失败。
        # 磁盘闸对重启的约束由 test_disk_hold_withholds_worker_restart 单独锁住。
        central_daemon._disk_free_gb = lambda: 20.0
        log_path = base / "worker.log"
        log_path.write_text(
            "ERROR: 403 Forbidden insufficient balance\n", encoding="utf-8")

        class FinishedProc:
            pid = 111
            returncode = 0

            @staticmethod
            def poll():
                return 0

            @staticmethod
            def wait(timeout=None):
                return 0

        class RunningProc:
            pid = 222
            returncode = None

            @staticmethod
            def poll():
                return None

            @staticmethod
            def wait(timeout=None):
                return 0

        rid = "L-9001"
        job = {
            "id": rid, "item": {"id": rid, "kind": "bug", "title": "test"},
            "cfg": {"timeout": 10, "retries": 0,
                    "fast_model": "aibvv@deepseek-v4.1-flash",
                    "slow_model": "ok8888@deepseek-v4.1-flash"},
            "repo": str(repo), "worktree": str(wt), "branch": "codex/test",
            "log": str(log_path), "model": "aibvv@deepseek-v4.1-flash",
            "proc": FinishedProc(), "pid": 111, "detached": True,
            "result": {"id": rid},
        }
        workers = {"exec_jobs": {rid: job}}
        with mock.patch.object(central_daemon, "_exec_command",
                               return_value=["fake-codex"]), \
             mock.patch.object(central_daemon.subprocess, "Popen",
                               return_value=RunningProc()):
            results = central_daemon._collect_exec(workers, {})

        assert results and results[0].get("outcome") == "relaunched", results
        assert rid in workers["exec_jobs"], workers
        assert job["pid"] == 222 and job["model"].startswith("ok8888@"), job
    print("selftest ok: 额度 fallback 异步重挂 worker，不阻塞其余并发槽位派发。")


def test_parallel_exec_slots():
    """Disk admission gates new workers without interrupting running workers.

    This locks the structural property the old synchronous dispatcher lacked:
    ``dispatch_exec`` starts up to max_workers independent worktrees without
    waiting for the first Codex process to exit. The landing lock remains a
    separate serial critical section. Both the three-slot and ten-slot paths
    verify probe failure, low-disk hold, hysteresis, and automatic resume.
    """
    import importlib
    import json as _json
    import time as _time

    def exercise(slots):
        with tempfile.TemporaryDirectory(prefix=f"codex-autopilot-parallel-{slots}-") as tmp:
            base = Path(tmp)
            repo, trunk, home = base / "state", base / "state-trunk", base / "home"
            repo.mkdir(); home.mkdir()
            (repo / "ops" / "tasks").mkdir(parents=True)
            (repo / "ops" / "truth").mkdir(parents=True)
            (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
            for name in ("progress", "decisions", "acceptance"):
                (repo / "ops" / "truth" / f"{name}.jsonl").write_text("", encoding="utf-8")
            ledger = []
            for i in range(1, slots + 1):
                ledger.append({"id": f"L-{i:04d}", "type": "ledger", "kind": "bug",
                               "title": f"并行槽位 {i}", "status": "open",
                               "ts": f"2026-01-01T00:00:{i:02d}+08:00"})
            (repo / "ops" / "truth" / "ledger.jsonl").write_text(
                "".join(_json.dumps(x, ensure_ascii=False) + "\n" for x in ledger),
                encoding="utf-8")
            (repo / "ops" / "central.config.json").write_text(_json.dumps({
                "build": {"cmd": "", "cwd": "."}, "verify": [],
                "auto_exec": {"enabled": True, "max_workers": slots,
                              "max_per_cycle": slots,
                              "fast_model": "aibvv@deepseek-v4.1-flash",
                              "slow_model": "ok8888@deepseek-v4.1-flash"}}), encoding="utf-8")
            run(["git", "init", "-q", "-b", "main", str(repo)])
            git(repo, "config", "user.email", "selftest@example.invalid")
            git(repo, "config", "user.name", "Autopilot Selftest")
            (repo / "app.txt").write_text("main\n", encoding="utf-8")
            git(repo, "add", "app.txt", "ops/central.config.json", "ops/truth/ledger.jsonl",
                "ops/truth/progress.jsonl", "ops/truth/decisions.jsonl",
                "ops/truth/acceptance.jsonl", "ops/tasks/inbox.jsonl")
            git(repo, "commit", "-q", "-m", "init")
            origin = base / "origin.git"
            run(["git", "init", "-q", "--bare", str(origin)])
            git(repo, "remote", "add", "origin", str(origin))
            git(repo, "push", "-q", "origin", "main")
            (home / "control.json").write_text(_json.dumps({
                "auto_exec": True, "paused": False}), encoding="utf-8")
            fake = base / "fake-parallel.sh"
            release = base / "parallel-release"
            fake.write_text(
                "#!/bin/sh\n"
                "wt=\"\"\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = \"-C\" ]; then wt=\"$2\"; shift 2; else shift; fi\n"
                "done\n"
                "printf 'started\n' > \"$wt/started.txt\"\n"
                f"while [ ! -f '{release}' ]; do sleep 0.02; done\n"
                "printf 'parallel\\n' > \"$wt/done.txt\"\n",
                encoding="utf-8")
            fake.chmod(0o755)
            os.environ.update(KG_REPO=str(repo), KG_TRUNK=str(trunk),
                              KG_DAEMON_HOME=str(home), KG_EXEC_CMD=str(fake))
            import control_state, central_config, central_daemon
            importlib.reload(control_state); importlib.reload(central_config)
            importlib.reload(central_daemon)
            central_daemon._disk_free_gb = lambda: None
            blocked, workers = {}, {}
            real_live_slugs = central_daemon._live_exec_slugs
            # 这个用例只验并发槽位。外部残留的自检进程可能刚好带同名
            # worktree，不能让它把本 fixture 的派发静默挡掉。
            central_daemon._live_exec_slugs = lambda: frozenset()
            try:
                started, workers = central_daemon.dispatch_exec(False, blocked, workers)
                assert started == [], started
                assert not workers.get("exec_jobs"), workers
                state = _json.loads((home / "state.json").read_text(encoding="utf-8"))
                assert state["exec_disk_held"] is True, state
                assert state["exec_disk_free_gb"] is None, state

                # The held state persists through the hysteresis band.
                central_daemon._disk_free_gb = lambda: 0.01
                _, workers = central_daemon.dispatch_exec(False, blocked, workers)
                assert not workers.get("exec_jobs"), workers
                central_daemon._disk_free_gb = lambda: 8.0
                _, workers = central_daemon.dispatch_exec(False, blocked, workers)
                assert not workers.get("exec_jobs"), workers

                # Crossing the resume threshold releases the configured slots.
                central_daemon._disk_free_gb = lambda: 10.01
                started, workers = central_daemon.dispatch_exec(False, blocked, workers)
                assert started == [], started
                jobs = workers["exec_jobs"]
                assert len(jobs) == slots, jobs
                assert len({j["worktree"] for j in jobs.values()}) == slots, jobs
                deadline = _time.time() + 10
                while (_time.time() < deadline and not all(
                        (Path(j["worktree"]) / "started.txt").exists()
                        for j in jobs.values())):
                    _time.sleep(0.02)
                assert all((Path(j["worktree"]) / "started.txt").exists()
                           for j in jobs.values()), jobs
                assert all(j["proc"].poll() is None for j in jobs.values()), jobs

                # A renewed low-space condition blocks only new workers and
                # leaves all currently running processes untouched.
                central_daemon._disk_free_gb = lambda: 0.01
                _, workers = central_daemon.dispatch_exec(False, blocked, workers)
                assert len(workers["exec_jobs"]) == slots, workers
                assert all(j["proc"].poll() is None
                           for j in workers["exec_jobs"].values()), workers
                release.touch()
                deadline = _time.time() + 15
                landed = []
                while _time.time() < deadline and len(landed) < slots:
                    results, workers = central_daemon.dispatch_exec(False, blocked, workers)
                    landed.extend(r for r in results if r.get("outcome") == "landed")
                    _time.sleep(0.1)
                assert len(landed) == slots, (slots, landed, blocked, workers)
                assert not workers["exec_jobs"], workers

            finally:
                central_daemon._live_exec_slugs = real_live_slugs
                release.touch()
                for job in workers.get("exec_jobs", {}).values():
                    proc = job.get("proc")
                    if proc is not None and proc.poll() is None:
                        proc.kill()

    exercise(3)
    exercise(10)
    print("selftest ok: 低盘阻断新 worker、保留在途任务，恢复到 10GB 后自动放行 3/10 槽位。")


def test_disk_hold_withholds_worker_restart():
    """A retry/relaunch is a new process and must obey the disk gate too.

    低盘时只拦首次派发、放过重试重启，等于让低盘现场自己把磁盘越写越满。
    这里锁住：重试重启也要过闸，被压住时不留 proc、不启 Popen、不烧重试预算。
    """
    import importlib
    import sys
    from unittest import mock

    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import central_daemon

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-disk-restart-") as tmp:
        home = Path(tmp)
        with mock.patch.dict(os.environ, {"KG_DAEMON_HOME": str(home)}, clear=False):
            importlib.reload(central_daemon)
            with mock.patch.object(central_daemon, "_disk_pressure", return_value=1), \
                 mock.patch.object(central_daemon, "_disk_free_gb", return_value=1.0):
                assert central_daemon._disk_holds_restart("L-1") is True
            with mock.patch.object(central_daemon, "_disk_pressure", return_value=0):
                assert central_daemon._disk_holds_restart("L-1") is False

            job = {"id": "L-1", "item": {"id": "L-1", "kind": "bug"},
                   "cfg": {"timeout": 5, "retries": 1, "no_change_retries": 1},
                   "repo": str(home), "worktree": str(home),
                   "branch": "codex/x", "log": str(home / "w.log"),
                   "model": "m", "attempts": 0,
                   "proc": central_daemon._finished_proc(1),
                   "result": {"id": "L-1"}}
            (home / "w.log").write_text("ERROR: unexpected status 500 boom\n",
                                        encoding="utf-8")
            with mock.patch.object(central_daemon, "_disk_pressure", return_value=1), \
                 mock.patch.object(central_daemon, "_disk_free_gb", return_value=1.0), \
                 mock.patch.object(central_daemon.subprocess, "Popen") as popen, \
                 mock.patch.object(central_daemon, "classify_exec_infra_failure",
                                   return_value=True):
                out = central_daemon._reap_exec(job, {})
                popen.assert_not_called()
                assert out.get("retry_reason") == "disk_held", out
                assert job.get("disk_held") is True, job
                assert "proc" not in job, job

    print("selftest ok: 低盘时重试/重派也不重启 worker，保留现场等空间恢复再续跑。")


def test_git_gc_never_prunes_shared_partial_clone():
    """Autopilot must never launch an immediate object prune.

    This repo is a partial clone shared by hundreds of worktrees. A successful
    ``git gc --prune=now`` can delete an object that another process has just
    written but not yet made reachable, and a concurrent lazy fetch can make the
    next command misreport a missing local commit as "already merged". Lock the
    safety property at the process boundary so a later cleanup refactor cannot
    silently reintroduce the destructive call.
    """
    import importlib
    import sys
    from unittest import mock

    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import central_daemon

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-no-gc-") as tmp:
        home = Path(tmp)
        with mock.patch.dict(os.environ, {"KG_DAEMON_HOME": str(home)}, clear=False):
            importlib.reload(central_daemon)
            with mock.patch.object(central_daemon.subprocess, "Popen") as popen:
                with mock.patch.object(central_daemon, "load_state", return_value={}):
                    with mock.patch.object(central_daemon, "save_state"):
                        central_daemon._git_gc_maintenance()
                popen.assert_not_called()

    print("selftest ok: 自动挡不会对共享 partial clone 执行 gc --prune=now。")


def test_exec_prompt_includes_patch_recovery():
    """Sub-workers must know how to recover from wrapped apply_patch failures."""
    import importlib
    import sys
    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import central_daemon
    importlib.reload(central_daemon)
    exec_prompt = central_daemon._exec_prompt({
        "id": "L-0001", "kind": "bug", "title": "test", "detail": "test"
    })
    resolve_prompt = central_daemon._resolve_exec_prompt({
        "id": "resolve:test", "kind": "resolve", "title": "test",
        "slug": "test", "files": ["frontend/src/App.tsx"],
    })
    for prompt in (exec_prompt, resolve_prompt):
        assert "first line of the patch" in prompt, prompt
        assert "count == 1" in prompt, prompt
    print("selftest ok: 普通与冲突 worker prompt 都包含 apply_patch 故障恢复规则。")


def test_git_scan_timeout_fails_closed_not_open():
    """A timed-out scan must block landing, never look like "no conflict".

    `conflict_files()` / `unresolved_marker_files()` 用「空列表 = 没冲突」表达
    可落盘。proc.run 的默认闸超时后返回 rc=124，如果这两个函数仍按「空」返回，
    一条根本算不完的分支就会被当成干净分支推上 main —— 这是最危险的一种假绿灯。
    这里锁住：rc=124 必须被翻译成「有阻塞」，而不是「没事」。

    在**独立子进程**里跑：selftest 主进程的 KG_REPO / kgctl 模块会被前序用例
    （test_verify_and_truth、test_publish_lock…）改成临时目录并留在那里，
    在本进程里 import kgctl 会拿到被污染的实例，断言就测不到真实代码。
    """
    import subprocess as _sp
    import sys

    ops = Path(__file__).resolve().parent
    script = r"""
import sys
sys.path.insert(0, sys.argv[1])
from unittest import mock
import kgctl

class Timeout124:
    returncode = 124
    stdout = ""
    stderr = "[proc] 命令超时"

with mock.patch.object(kgctl, "proc_run", return_value=Timeout124()):
    conf = kgctl.conflict_files("codex/some-branch")
    assert conf, "超时的 merge-tree 不能被当成『无冲突』"
    assert any("超时" in c for c in conf), conf
    markers = kgctl.unresolved_marker_files("codex/some-branch")
    assert markers, "超时的 git grep 不能被当成『无残留标记』"
    assert any("超时" in m for m in markers), markers

class GrepNoMatch:
    returncode = 1
    stdout = ""
    stderr = ""

with mock.patch.object(kgctl, "proc_run", return_value=GrepNoMatch()):
    assert kgctl.unresolved_marker_files("codex/clean-branch") == [], \
        "rc=1 是『没匹配到』，必须继续视为干净"
print("child-ok")
"""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("KG_")}
    probe = _sp.run([sys.executable, "-c", script, str(ops)],
                    capture_output=True, text=True, env=clean, cwd=str(ops.parent))
    assert "child-ok" in (probe.stdout or ""), (
        "kgctl 超时 fail-closed 断言失败：\n"
        + (probe.stdout or "")[-500:] + (probe.stderr or "")[-900:])
    print("selftest ok: git 扫描超时 fail-closed（阻塞落盘），rc=1 仍视为干净。")


def test_proc_run_never_hangs_on_inherited_pipe():
    """Lock the 2026-10-27 hang: a grandchild holding the output pipe must not block.

    tick 静默挂了 30+ 分钟、0.0% CPU：`subprocess.run(capture_output=True)` 用 pipe
    收输出，partial clone 下 git 的懒加载孙进程攥着写端不关，`communicate()` 就永远
    等不到 EOF。修法是「输出落临时文件 + 硬超时 + 超时 killpg 整组」。
    这里用真实的孙进程复刻病灶，锁住三条性质：
      1. 子进程秒退、孙进程活着时，proc.run 也必须秒回（不是等 EOF）；
      2. 超时必须 killpg 收掉整个进程组，不留存活孙进程；
      3. git 命令默认带 GIT_NO_LAZY_FETCH=1，杜绝懒加载派网络子进程。
    """
    import importlib
    import subprocess as _sp
    import sys
    import time
    from unittest import mock

    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import proc
    importlib.reload(proc)

    # 1) 孙进程攥着 stdout 不退出：旧实现会挂满 timeout，新实现必须立刻返回。
    t0 = time.time()
    r = proc.run(["sh", "-c", "(sleep 60) & echo child-done"], text=True)
    elapsed = time.time() - t0
    assert r.returncode == 0, r
    assert "child-done" in (r.stdout or ""), r.stdout
    assert elapsed < 10, f"孙进程攥着输出时 proc.run 不该等 EOF，实测 {elapsed:.1f}s"

    # 2) 超时收整个进程组，不留活着的孙进程。
    t0 = time.time()
    try:
        proc.run(["sh", "-c", "sleep 60 & echo $$; wait"],
                 text=True, timeout=1)
        raise AssertionError("显式 timeout 命中时必须抛 TimeoutExpired")
    except _sp.TimeoutExpired:
        pass
    assert time.time() - t0 < 15, "超时回收必须迅速"

    # 3) git 命令默认禁懒加载，并把提示类环境变量钉死。
    env = proc._git_env(None)
    assert env["GIT_NO_LAZY_FETCH"] == "1", env
    assert env["GIT_TERMINAL_PROMPT"] == "0", env
    assert proc._is_network_cmd(["git", "-C", "x", "fetch", "origin", "main"]) is True
    assert proc._is_network_cmd(["git", "-C", "x", "status"]) is False
    # 网络命令拿到更长预算，本地只读拿到 60s 级预算。
    assert proc.NET_TIMEOUT >= proc.DEFAULT_TIMEOUT

    # 4) 三个模块都必须把 subprocess.run 换成受控实现 —— 只要有一处漏掉，
    #    那条路径就仍然可以静默挂死整轮 tick。
    #    这里只读源码文本、不 import：selftest 进程里的 KG_REPO 会被前序用例改到
    #    临时目录，真 import kgctl 会在 import 期因找不到状态根 sys.exit。
    for mod_name in ("kgctl", "central_daemon", "truthctl"):
        src = (ops / f"{mod_name}.py").read_text(encoding="utf-8")
        assert "proc_run = _proc.run" in src, f"{mod_name}.py 没有接入受控执行器"
        assert "subprocess.run(" not in src, (
            f"{mod_name}.py 还有裸 subprocess.run（会绕过超时闸）："
            + str(src.count("subprocess.run(")))
        # 调用点必须真的走 proc_run，而不是只挂了个名字。
        assert src.count("proc_run(") >= 3, f"{mod_name}.py 的调用点没有改走 proc_run"

    print("selftest ok: 子进程执行器有硬超时、超时收整组、禁懒加载，且三模块无裸 subprocess.run。")


def test_run_kgctl_streams_progress_live():
    """kgctl 的输出必须边跑边进日志，不许攒到进程退出才吐。

    真实踩到的坑（2026-10-27 05:48 那一拍）：tick 实测只要 60 秒，但
    run_kgctl 用 `communicate()` 把 8 个阶段的输出全攒到退出才写日志，
    daemon.log 因此出现 7 分钟一片空白。用户看到的就是「它卡住了 /
    又得清一轮」，而实际上它一直在正常干活。这里锁三件事：
    阶段行在进程还在跑的时候就已经写进日志、超时仍收整组、返回值仍是完整输出。
    """
    import importlib
    import sys as _sys
    import tempfile
    from unittest import mock

    ops = Path(__file__).resolve().parent
    if str(ops) not in _sys.path:
        _sys.path.insert(0, str(ops))
    import central_daemon
    importlib.reload(central_daemon)

    seen_mid_run = []

    with tempfile.TemporaryDirectory(prefix="kg-stream-") as tmp:
        log_path = Path(tmp) / "daemon.log"

        def fake_log(msg):
            text = str(msg)
            # 记录「进程还活着时」日志里出现了什么
            if fake_proc.poll() is None:
                seen_mid_run.append(text)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")

        # 一个慢慢吐三行、每行间隔真实的子进程：攒到最后就一条都看不见。
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_proc.wait.return_value = 0
        lines = ["[1/8] route inbox", "[2/8] refresh project status", "[3/8] synthesize"]
        fake_proc.stdout = iter([l + "\n" for l in lines])

        with mock.patch.object(central_daemon, "log", side_effect=fake_log), \
             mock.patch.object(central_daemon.subprocess, "Popen", return_value=fake_proc):
            rc, out = central_daemon.run_kgctl(["tick", "--no-converge"],
                                              env_extra={"KG_TICK_LIGHT": "1"})

        assert rc == 0, rc
        assert out.count("[") >= 3, f"返回值丢了已产出的输出：{out!r}"
        joined = "".join(seen_mid_run)
        assert "[1/8]" in joined, f"阶段行没有在运行中就写进日志：{seen_mid_run}"
        assert "[3/8]" in joined, f"最后一行也是流式写出的：{seen_mid_run}"

    print("selftest ok: kgctl 阶段输出边跑边进日志（不再攒到退出才吐），返回值仍完整。")


def test_context_packs_are_cached_and_budgeted():
    """Context Pack 的 merge-tree 探测必须缓存 + 有时间预算。

    2026-10-27 实测：tick 阶段 6 给 20 支分支各做一次 merge-tree + 全树 marker
    grep，在 load 90+ 的机器上一支 10 秒，整段 4.5 分钟 —— 这正是「日志静默
    7 分钟」背后真正的耗时。缓存键是 branch@head@main，任何一边动了才重算。
    这里锁三件事：同样的输入不打第二次 merge-tree、head/main 变了会重算、
    超预算的分支被跳过并如实报告而不是悄悄消失。
    """
    import importlib
    import sys as _sys
    import tempfile
    from unittest import mock

    ops = Path(__file__).resolve().parent
    if str(ops) not in _sys.path:
        _sys.path.insert(0, str(ops))
    import kgctl

    tmp = Path(tempfile.mkdtemp(prefix="kg-ctx-cache-"))
    real_cache = kgctl.CONTEXT_CACHE
    kgctl.CONTEXT_CACHE = tmp / "ctx-cache.json"
    calls = []
    try:
        def fake_conflict_files(branch):
            calls.append(branch)
            return ["a.ts"]

        with mock.patch.object(kgctl, "conflict_files", side_effect=fake_conflict_files), \
             mock.patch.object(kgctl, "unresolved_marker_files", return_value=[]):
            first = kgctl._context_probes("codex/x", "h1", "m1")
            assert first["conflict_files"] == ["a.ts"], first
            assert calls == ["codex/x"], calls

            # 同样的 branch/head/main：不能再打一次 merge-tree。
            second = kgctl._context_probes("codex/x", "h1", "m1")
            assert second == first, (second, first)
            assert calls == ["codex/x"], f"缓存没命中，又打了一次：{calls}"

            # head 变了（分支有新提交）必须重算，不能拿旧答案糊过去。
            kgctl._context_probes("codex/x", "h2", "m1")
            assert calls == ["codex/x", "codex/x"], calls

            # main 前进同样必须重算：旧 main 下的「无冲突」不能当成结论。
            kgctl._context_probes("codex/x", "h2", "m2")
            assert len(calls) == 3, calls
    finally:
        kgctl.CONTEXT_CACHE = real_cache
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # 预算参数必须真的存在且可被环境变量调节（守护里靠它兜住一轮）。
    src = (ops / "kgctl.py").read_text(encoding="utf-8")
    assert "KG_CONTEXT_BUDGET" in src, "Context Pack 没有时间预算"
    assert "skipped.append" in src, "超预算的分支没有被如实报告"
    print("selftest ok: Context Pack 探测按 branch@head@main 缓存，且有可调时间预算。")


def test_cycle_has_hard_budget():
    """一整拍必须有硬预算，超了就不再开新步骤，并且留痕。

    单步各有时限不等于整拍有时限：tick 600s + land 600s + port + resolve 叠加，
    历史上就是「一拍吃掉半小时、日志一片空白」。这里锁三件事：
    超预算时 `_over_budget` 为真、`_run_cycle` 不再开新的 land 步骤、
    并且日志里明确写了掐在哪一步（静默是最贵的失败方式）。
    """
    import importlib
    import sys as _sys
    import tempfile
    from unittest import mock

    ops = Path(__file__).resolve().parent
    if str(ops) not in _sys.path:
        _sys.path.insert(0, str(ops))
    import central_daemon
    import time as _t

    logs = []

    # 预算未到：不拦。
    future = _t.monotonic() + 600
    assert central_daemon._over_budget(future, "任意步骤") is False
    # deadline=None（其它调用方）：表示不限，不能误拦。
    assert central_daemon._over_budget(None, "任意步骤") is False
    # 预算已过：拦，并且必须留痕。
    with mock.patch.object(central_daemon, "log", side_effect=logs.append):
        past = _t.monotonic() - 1
        assert central_daemon._over_budget(past, "下一轮 land") is True
    joined = " ".join(str(x) for x in logs)
    assert "下一轮 land" in joined, f"超预算没有写明掐在哪一步：{logs}"
    assert "预算" in joined, logs

    # 源码必须把 deadline 真的传进 _run_cycle，否则预算只是个摆设。
    src = (ops / "central_daemon.py").read_text(encoding="utf-8")
    assert "deadline=time.monotonic() + CYCLE_BUDGET" in src, \
        "_run_cycle 没有被传入整拍 deadline"
    assert "KG_CYCLE_BUDGET" in src, "整拍预算不可调"
    print("selftest ok: 整拍有硬预算，超预算不再开新步骤且留痕指明掐在哪。")


def test_worker_alive_treats_zombie_as_dead():
    """A defunct child must not occupy an auto_exec slot.

    os.kill(pid, 0) keeps succeeding for an unreaped zombie, so the daemon can
    mark a finished worker as still active and stop filling max_workers slots.
    The new _worker_alive reads the process state table and treats Z as dead.
    """
    import importlib
    import sys as _sys
    from unittest import mock

    ops = Path(__file__).resolve().parent
    if str(ops) not in _sys.path:
        _sys.path.insert(0, str(ops))
    import central_daemon
    importlib.reload(central_daemon)

    # Already-exited PID: os.kill(pid, 0) raises ProcessLookupError -> dead.
    assert not central_daemon._worker_alive(999999999), (
        "nonexistent pid must be treated as dead")

    # Zombie state: os.kill(pid, 0) succeeds but ps reports Z -> dead.
    zombie_pid = 424242
    with mock.patch("central_daemon.os.kill", return_value=None), \
         mock.patch("central_daemon.proc_run",
                    return_value=type("R", (), {"stdout": "Z+"})()):
        assert not central_daemon._worker_alive(zombie_pid), (
            "zombie state must be treated as dead")

    # Live state: os.kill succeeds and ps reports R+ -> alive.
    with mock.patch("central_daemon.os.kill", return_value=None), \
         mock.patch("central_daemon.proc_run",
                    return_value=type("R", (), {"stdout": "R+"})()):
        assert central_daemon._worker_alive(zombie_pid), (
            "live state must remain alive")

    central_daemon._worker_alive = lambda pid: pid == 111
    jobs = {
        "live": {"pid": 111},
        "dead": {"pid": 222},
        "recover": {"pid": 333, "recover": True},
    }
    assert central_daemon._active_exec_session_count(jobs) == 1, jobs
    print("selftest ok: 只有 PID 真活着的 worker 占并发槽；僵尸和待落盘槽位不占用。")


def test_dead_pid_never_fabricates_worker_success():
    """PID 已消失但 Popen 尚未返回状态时，不能把失败伪造成 exit 0。"""
    import importlib
    import subprocess
    import sys as _sys
    from unittest import mock

    ops = Path(__file__).resolve().parent
    if str(ops) not in _sys.path:
        _sys.path.insert(0, str(ops))
    import central_daemon
    importlib.reload(central_daemon)

    class StillPending:
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("codex", timeout)

    class ExitedWithFailure:
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.returncode = 7
            return 7

    pending = StillPending()
    with mock.patch("central_daemon._worker_alive", return_value=False), \
         mock.patch("central_daemon._reap_exec") as reap:
        job = {"id": "L-0001", "pid": 424242, "proc": pending}
        results = central_daemon._collect_exec({"exec_jobs": {"L-0001": job}}, {})
    assert results == [], results
    assert job["proc"] is pending, job
    reap.assert_not_called()

    failed = ExitedWithFailure()
    with mock.patch("central_daemon._worker_alive", return_value=False), \
         mock.patch("central_daemon._reap_exec",
                    return_value={"id": "L-0001", "outcome": "codex_fail_preserved"}) as reap:
        job = {"id": "L-0001", "pid": 424242, "proc": failed}
        central_daemon._collect_exec({"exec_jobs": {"L-0001": job}}, {})
    assert job["proc"].poll() == 7, job
    assert job["proc"].wait() == 7, job
    reap.assert_called_once()
    print("selftest ok: PID 消失时先取真实退出码，取不到就等待，绝不伪造成成功。")


def test_hydrate_drops_unverifiable_exec_slots():
    """Restart must not resurrect jobs that no live worktree can finish."""
    import importlib
    import json as _json
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-hydrate-") as tmp:
        base = Path(tmp)
        repo, trunk, home = base / "state", base / "state-trunk", base / "home"
        repo.mkdir(); home.mkdir(); (repo / "ops" / "truth").mkdir(parents=True)
        ledger = [{"id": "L-0001", "type": "ledger", "kind": "bug",
                   "title": "open but no worktree", "status": "open",
                   "ts": "2026-01-01T00:00:00+08:00"}]
        (repo / "ops" / "truth" / "ledger.jsonl").write_text(
            "".join(_json.dumps(x, ensure_ascii=False) + "\n" for x in ledger),
            encoding="utf-8")
        os.environ.update(KG_REPO=str(repo), KG_TRUNK=str(trunk),
                          KG_DAEMON_HOME=str(home))
        import control_state, central_config, central_daemon
        importlib.reload(control_state); importlib.reload(central_config)
        importlib.reload(central_daemon)
        workers = {"exec_jobs": {"L-0001": {
            "id": "L-0001", "outcome": "ready", "worktree": ""}}}
        hydrated = central_daemon._hydrate_exec_jobs(workers)
        assert hydrated["exec_jobs"] == {}, hydrated
    print("selftest ok: 无 worktree 的旧 worker 槽在重启时不会复活。")


def test_resolve_exec_parallel():
    """Resolve tasks are filled into the same parallel codex worker slots.

    kgctl resolve creates an isolated resolve worktree with markers. The daemon
    must reuse that worktree for a real paid worker, then commit and hand the
    branch back to kgctl resolve --finish instead of creating a second
    codex/exec-* worktree and losing the registered resolve task.
    """
    import importlib
    import json as _json
    import time as _time
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-resolve-exec-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        home = root / "home"
        repo.mkdir(); home.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for f in ("ledger", "progress", "decisions", "acceptance"):
            (repo / "ops" / "truth" / f"{f}.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
        (repo / "ops" / "central.config.json").write_text(_json.dumps({
            "build": {"cmd": "", "cwd": "."}, "verify": [],
            "auto_exec": {"enabled": True, "max_workers": 2,
                          "fast_model": "aibvv@deepseek-v4.1-flash",
                          "slow_model": "ok8888@deepseek-v4.1-flash"}}), encoding="utf-8")
        (home / "control.json").write_text(_json.dumps({
            "auto_exec": True, "paused": False}), encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "doc.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "doc.txt", "ops/central.config.json", "ops/truth/mainline.json",
            "ops/truth/ledger.jsonl", "ops/truth/progress.jsonl",
            "ops/truth/decisions.jsonl", "ops/truth/acceptance.jsonl",
            "ops/tasks/inbox.jsonl")
        git(repo, "commit", "-q", "-m", "init")
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        git(repo, "checkout", "-q", "-b", "codex/light", base)
        (repo / "doc.txt").write_text("light\n", encoding="utf-8")
        git(repo, "commit", "-qam", "codex/light")
        git(repo, "checkout", "-q", "main")
        (repo / "doc.txt").write_text("mainline\n", encoding="utf-8")
        git(repo, "commit", "-qam", "main advances")
        git(repo, "push", "-q", "origin", "main")

        go = kgctl(repo, trunk, "resolve", "--auto", "--go")
        if "outcome=needs_agent" not in (go.stdout + go.stderr):
            raise AssertionError(f"resolve 没建出待解任务:\n{go.stdout}\n{go.stderr}")
        wt = root / "state-resolve-light"
        if not wt.is_dir():
            raise AssertionError(f"resolve 没建出 worktree {wt}")

        fake = root / "fake-resolve.sh"
        fake.write_text(
            "#!/bin/sh\n"
            "wt=\"\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-C\" ]; then wt=\"$2\"; shift 2; else shift; fi\n"
            "done\n"
            "sed -i '' '/^<<<<<<< /d;/^=======$/d;/^>>>>>>> /d' \"$wt/doc.txt\"\n"
            "printf 'light\\nmainline\\n' > \"$wt/doc.txt\"\n"
            "exit 0\n", encoding="utf-8")
        fake.chmod(0o755)

        os.environ.update(KG_REPO=str(repo), KG_TRUNK=str(trunk),
                          KG_DAEMON_HOME=str(home), KG_EXEC_CMD=str(fake))
        import control_state, central_config, central_daemon
        importlib.reload(control_state); importlib.reload(central_config)
        importlib.reload(central_daemon)

        # 本用例只验证 resolve 派发，不验证磁盘闸；宿主真实低盘会让它假失败。
        central_daemon._disk_free_gb = lambda: 20.0

        original = (wt / "doc.txt").read_text(encoding="utf-8")
        (wt / "doc.txt").write_text("manual external edit\n", encoding="utf-8")
        if central_daemon._resolve_open_items():
            raise AssertionError("worktree 已有无冲突标记的外部改动时仍被当作可派任务")
        (wt / "doc.txt").write_text(original, encoding="utf-8")
        open_ids = [i["id"] for i in central_daemon._resolve_open_items()]
        if open_ids != ["resolve:light"]:
            raise AssertionError(f"仍有冲突标记且无 worker 时没有重新进入可派队列: {open_ids!r}")

        stale_workers = {"exec_jobs": {"resolve:light": {
            "id": "resolve:light",
            "item": central_daemon._resolve_task_item("light"),
            "cfg": central_daemon.auto_exec_config(),
            "repo": str(repo), "branch": "codex/resolve-light",
            "worktree": str(wt), "codex": ["codex"],
            "log": str(home / "exec" / "stale-resolve.log"),
            "pid": 999999, "detached": True, "proc": None,
            "result": {"id": "resolve:light"}}}}
        stale_results = central_daemon._collect_exec(stale_workers, {})
        if not stale_results or stale_workers["exec_jobs"]:
            raise AssertionError(
                f"重启后已死 resolve worker 仍占槽: {stale_results!r} {stale_workers!r}")

        blocked, workers = {}, {}
        # dispatch_exec 返回 (results, workers)：results 是本轮「已收口结果」，
        # 首次派发时还没有任何 worker 完成，所以必须为空；新起的 worker 在
        # workers["exec_jobs"] 里，由下面几行断言。断言意图没变，只是跟上签名。
        started, workers = central_daemon.dispatch_exec(False, blocked, workers)
        assert started == [], started
        jobs = workers["exec_jobs"]
        assert list(jobs) == ["resolve:light"], jobs
        job = jobs["resolve:light"]
        assert job["branch"] == "codex/resolve-light", job
        assert Path(job["worktree"]).resolve() == wt.resolve(), job
        assert job["model"] == "aibvv@deepseek-v4.1-flash", job
        assert job["proc"].poll() is None, job

        deadline = _time.time() + 15
        landed = []
        while _time.time() < deadline and not landed:
            results, workers = central_daemon.dispatch_exec(False, blocked, workers)
            landed.extend(r for r in results if r.get("outcome") == "landed")
            _time.sleep(0.1)
        assert landed, (results, blocked, workers)
        assert not workers["exec_jobs"], workers
        merged = git(repo, "show", "main:doc.txt").stdout
        if "light" not in merged or "mainline" not in merged:
            raise AssertionError(f"resolve-exec 落盘内容不对: {merged!r}")
    print("selftest ok: resolve 冲突任务进入同一并行 codex worker，复用 resolve worktree 并由 kgctl finish 串行落盘。")


def test_collect_exec_keeps_reaping_after_nonlanding_failure():
    """A failed resolve must not hide other completed workers in the same cycle."""
    import central_daemon

    class _DoneProc:
        def poll(self):
            return 0

    real_reap = central_daemon._reap_exec
    try:
        def fake_reap(job, blocked):
            return {"id": job["id"], "outcome": job["test_outcome"]}

        central_daemon._reap_exec = fake_reap
        workers = {"exec_jobs": {
            "r1": {"id": "r1", "test_outcome": "resolve_gate_fail", "proc": _DoneProc()},
            "r2": {"id": "r2", "test_outcome": "no_changes", "proc": _DoneProc()},
            "r3": {"id": "r3", "test_outcome": "landed", "proc": _DoneProc()},
        }}
        results = central_daemon._collect_exec(workers, {})
        assert [r["id"] for r in results] == ["r1", "r2", "r3"], results

        workers = {"exec_jobs": {
            "r1": {"id": "r1", "test_outcome": "resolve_gate_fail", "proc": _DoneProc()},
            "r2": {"id": "r2", "test_outcome": "land_conflict", "proc": _DoneProc()},
            "r3": {"id": "r3", "test_outcome": "landed", "proc": _DoneProc()},
        }}
        results = central_daemon._collect_exec(workers, {})
        assert [r["id"] for r in results] == ["r1", "r2"], results
    finally:
        central_daemon._reap_exec = real_reap
    print("selftest ok: resolve gate_fail 不阻塞同轮其他已完成 worker；真正 land/冲突仍保持串行边界。")


def test_resolve_gate_fail_auto_retry():
    """A resolve gate_fail must retry finish on the next cycle, not start codex again.

    A committed clean resolve worktree can hit gate_fail once because the remote
    moved between validation and push. The daemon previously wrote it into
    exec_blocked and never retried it, so an already-solved conflict stayed
    stuck forever. This locks the retry path: the next dispatch calls
    resolve --finish directly and removes the block on success.
    """
    import importlib
    import json as _json
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-resolve-retry-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        home = root / "home"
        repo.mkdir(); home.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for f in ("ledger", "progress", "decisions", "acceptance"):
            (repo / "ops" / "truth" / f"{f}.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
        (repo / "ops" / "central.config.json").write_text(_json.dumps({
            "build": {"cmd": "", "cwd": "."}, "verify": [],
            "auto_exec": {"enabled": True, "max_workers": 2,
                          "fast_model": "aibvv@deepseek-v4.1-flash",
                          "slow_model": "ok8888@deepseek-v4.1-flash"}}), encoding="utf-8")
        (home / "control.json").write_text(_json.dumps({
            "auto_exec": True, "paused": False}), encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "doc.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "doc.txt", "ops/central.config.json", "ops/truth/mainline.json",
            "ops/truth/ledger.jsonl", "ops/truth/progress.jsonl",
            "ops/truth/decisions.jsonl", "ops/truth/acceptance.jsonl",
            "ops/tasks/inbox.jsonl")
        git(repo, "commit", "-q", "-m", "init")
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        git(repo, "checkout", "-q", "-b", "codex/light", base)
        (repo / "doc.txt").write_text("light\n", encoding="utf-8")
        git(repo, "commit", "-qam", "codex/light")
        git(repo, "checkout", "-q", "main")
        (repo / "doc.txt").write_text("mainline\n", encoding="utf-8")
        git(repo, "commit", "-qam", "main advances")
        git(repo, "push", "-q", "origin", "main")

        go = kgctl(repo, trunk, "resolve", "--auto", "--go")
        if "outcome=needs_agent" not in (go.stdout + go.stderr):
            raise AssertionError(f"resolve 没建出待解任务:\n{go.stdout}\n{go.stderr}")
        wt = root / "state-resolve-light"
        if not wt.is_dir():
            raise AssertionError(f"resolve 没建出 worktree {wt}")

        (wt / "doc.txt").write_text("light\nmainline\n", encoding="utf-8")
        git(wt, "add", "doc.txt")
        git(wt, "commit", "-q", "-m", "resolve: keep both intents")
        commit = git(wt, "rev-parse", "HEAD").stdout.strip()

        rp = repo / "ops" / "tasks" / "resolve.json"
        data = _json.loads(rp.read_text(encoding="utf-8"))
        for rec in data.get("tasks") or []:
            if rec.get("slug") == "light":
                rec["status"] = "gate_fail"
                rec["updated"] = "2026-10-20T00:00:00+08:00"
        rp.write_text(_json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

        os.environ.update(KG_REPO=str(repo), KG_TRUNK=str(trunk),
                          KG_DAEMON_HOME=str(home), KG_EXEC_CMD="/bin/true")
        import control_state, central_config, central_daemon
        importlib.reload(control_state); importlib.reload(central_config)
        importlib.reload(central_daemon)

        blocked = {"resolve:light": {
            "reason": "resolve_gate_fail", "_id": "resolve:light",
            "branch": "codex/resolve-light", "worktree": str(wt),
            "commit": commit, "ts": "2026-10-20T00:00:00+08:00"}}
        workers = {}
        results, workers = central_daemon.dispatch_exec(False, blocked, workers)
        landed = [r for r in results if r.get("outcome") == "landed"]
        if not landed:
            raise AssertionError(f"gate_fail 没有下轮直接收口: {results} {blocked} {workers}")
        if "resolve:light" in blocked:
            raise AssertionError(f"落盘成功后仍保留 exec_blocked: {blocked}")
        if workers.get("exec_jobs"):
            raise AssertionError(f"重试 finish 不应新派 codex worker: {workers}")
        merged = git(repo, "show", "main:doc.txt").stdout
        if "light" not in merged or "mainline" not in merged:
            raise AssertionError(f"resolve 重试落盘内容不对: {merged!r}")
    print("selftest ok: resolve gate_fail 下一轮直接重试 finish 并落地，不重新派 codex。")


def test_resolve_gate_fail_not_retried_same_cycle():
    """A gate_fail written by _collect_exec must wait for the next cycle.

    Retrying resolve --finish in the same cycle can race a second resolve
    worker that was already dispatched for the same slug. The dispatcher
    should recognize a failure produced in this exact call and leave the task
    for the next cycle.
    """
    import importlib
    import json as _json
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-resolve-no-same-cycle-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        home = root / "home"
        repo.mkdir(); home.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for f in ("ledger", "progress", "decisions", "acceptance"):
            (repo / "ops" / "truth" / f"{f}.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
        (repo / "ops" / "central.config.json").write_text(_json.dumps({
            "build": {"cmd": "", "cwd": "."}, "verify": [],
            "auto_exec": {"enabled": True, "max_workers": 2,
                          "fast_model": "aibvv@deepseek-v4.1-flash",
                          "slow_model": "ok8888@deepseek-v4.1-flash"}}), encoding="utf-8")
        (home / "control.json").write_text(_json.dumps({
            "auto_exec": True, "paused": False}), encoding="utf-8")
        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "doc.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "doc.txt", "ops/central.config.json", "ops/truth/mainline.json",
            "ops/truth/ledger.jsonl", "ops/truth/progress.jsonl",
            "ops/truth/decisions.jsonl", "ops/truth/acceptance.jsonl",
            "ops/tasks/inbox.jsonl")
        git(repo, "commit", "-q", "-m", "init")
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)])
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        git(repo, "checkout", "-q", "-b", "codex/light", base)
        (repo / "doc.txt").write_text("light\n", encoding="utf-8")
        git(repo, "commit", "-qam", "codex/light")
        git(repo, "checkout", "-q", "main")
        (repo / "doc.txt").write_text("mainline\n", encoding="utf-8")
        git(repo, "commit", "-qam", "main advances")
        git(repo, "push", "-q", "origin", "main")

        go = kgctl(repo, trunk, "resolve", "--auto", "--go")
        if "outcome=needs_agent" not in (go.stdout + go.stderr):
            raise AssertionError(f"resolve 没建出待解任务:\n{go.stdout}\n{go.stderr}")
        wt = root / "state-resolve-light"
        if not wt.is_dir():
            raise AssertionError(f"resolve 没建出 worktree {wt}")
        (wt / "doc.txt").write_text("light\nmainline\n", encoding="utf-8")
        git(wt, "add", "doc.txt")
        git(wt, "commit", "-q", "-m", "resolve: keep both intents")
        commit = git(wt, "rev-parse", "HEAD").stdout.strip()

        rp = repo / "ops" / "tasks" / "resolve.json"
        data = _json.loads(rp.read_text(encoding="utf-8"))
        for rec in data.get("tasks") or []:
            if rec.get("slug") == "light":
                rec["status"] = "gate_fail"
                rec["updated"] = "2026-10-20T00:00:00+08:00"
        rp.write_text(_json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

        os.environ.update(KG_REPO=str(repo), KG_TRUNK=str(trunk),
                          KG_DAEMON_HOME=str(home), KG_EXEC_CMD="/bin/true")
        import control_state, central_config, central_daemon
        importlib.reload(control_state); importlib.reload(central_config)
        importlib.reload(central_daemon)

        blocked = {"resolve:light": {
            "reason": "resolve_gate_fail", "_id": "resolve:light",
            "branch": "codex/resolve-light", "worktree": str(wt),
            "commit": commit, "ts": "2026-10-20T00:00:00+08:00"}}
        workers = {}
        # Simulate _collect_exec having just produced this cycle's failure.
        # dispatch must see it in results and not immediately run finish again.
        original_collect = central_daemon._collect_exec
        central_daemon._collect_exec = lambda workers, blocked: [{
            "id": "resolve:light", "outcome": "resolve_gate_fail",
            "branch": "codex/resolve-light", "worktree": str(wt),
            "commit": commit}]
        try:
            results, workers = central_daemon.dispatch_exec(False, blocked, workers)
        finally:
            central_daemon._collect_exec = original_collect
        retried = [r for r in results if r.get("outcome") == "landed"]
        if retried:
            raise AssertionError(f"同一轮不应立即重试 finish: {results}")
        if workers.get("exec_jobs"):
            raise AssertionError(f"同一轮失败后不应再派 worker: {workers}")
    print("selftest ok: resolve gate_fail 当轮不立即重试，留到下一轮再 finish。")


def test_conflict_decision_hold_survives_existing_markers():
    """A human-decision hold must not be cleared by the same conflict markers."""
    import importlib
    from unittest import mock
    import sys
    from pathlib import Path

    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import central_daemon
    importlib.reload(central_daemon)

    rid = "resolve:stuck"
    item = {
        "id": rid, "kind": "resolve", "slug": "stuck", "title": "stuck",
        "repo": "/tmp", "worktree": "/tmp/stuck", "branch": "codex/stuck",
    }
    blocked = {rid: {"reason": "resolve_conflict_needs_decision",
                     "ts": "2099-01-01 00:00:00"}}
    cfg = {"enabled": True, "max_workers": 3, "max_per_cycle": 3}
    with mock.patch.object(central_daemon, "auto_exec_config", return_value=cfg), \
         mock.patch.object(central_daemon, "_codex_bin", return_value=["fake"]), \
         mock.patch.object(central_daemon, "_hydrate_exec_jobs", side_effect=lambda w: w), \
         mock.patch.object(central_daemon, "_collect_exec", return_value=[]), \
         mock.patch.object(central_daemon, "_exec_open_items", return_value=[item]), \
         mock.patch.object(central_daemon, "_live_exec_slugs", return_value=frozenset()), \
         mock.patch.object(central_daemon, "_resolve_auto_allowed", return_value=True), \
         mock.patch.object(central_daemon, "_resolve_task", return_value={"worktree": "/tmp/stuck"}), \
         mock.patch.object(central_daemon, "_resolve_worktree_has_markers", return_value=True), \
         mock.patch.object(central_daemon, "_retry_resolve_finish",
                           side_effect=AssertionError("人工停手期间不应重试 finish")):
        results, workers = central_daemon.dispatch_exec(False, blocked, {})

    assert results == [], results
    assert blocked[rid]["reason"] == "resolve_conflict_needs_decision", blocked
    assert workers.get("exec_jobs") == {}, workers
    print("selftest ok: 同一冲突标记不会绕过人工裁决停手，6h 内不再空转重试。")


def test_resolve_candidate_scan_skips_marker_grep():
    """Finding conflict candidates must not run marker greps on unrelated CLEAN branches."""
    import importlib
    from pathlib import Path
    from unittest import mock

    ops = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="kgctl-marker-scan-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        with mock.patch.dict(os.environ, {
            "KG_REPO": str(repo),
            "KG_TRUNK": str(trunk),
        }):
            spec = importlib.util.spec_from_file_location(
                "kgctl_marker_scan_selftest", ops / "kgctl.py")
            kgctl = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(kgctl)

            with mock.patch.object(kgctl, "codex_branches",
                                   return_value=["codex/exec-busy"]), \
                 mock.patch.object(kgctl, "counts", return_value=(2, 1)), \
                 mock.patch.object(kgctl, "git", return_value="1790000000"), \
                 mock.patch.object(kgctl, "changed_files", return_value=["backend/a.ts"]), \
                 mock.patch.object(kgctl, "conflict_files", return_value=["backend/a.ts"]), \
                 mock.patch.object(kgctl, "unresolved_marker_files",
                                   side_effect=AssertionError(
                                       "resolve 候选扫描不应做全树 marker grep")):
                rows = kgctl.scan(light=True, check_markers=False)

    assert len(rows) == 1 and rows[0]["kind"] == "CONFLICT", rows
    assert rows[0]["markers"] == [], rows
    print("selftest ok: resolve 候选扫描跳过无关的全树 marker grep，真落盘仍保留 marker 门禁。")


def test_scan_batches_branch_metadata():
    """The long-lived 461-branch checkout must not spawn per-branch count/log calls."""
    import importlib
    from pathlib import Path
    from unittest import mock

    ops = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="kgctl-batch-scan-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        with mock.patch.dict(os.environ, {
            "KG_REPO": str(repo),
            "KG_TRUNK": str(trunk),
        }):
            spec = importlib.util.spec_from_file_location(
                "kgctl_batch_scan_selftest", ops / "kgctl.py")
            kgctl = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(kgctl)

            index = {
                "codex/merged": {
                    "ahead": 0, "behind": 12, "ts": 1790000000,
                    "subject": "already landed",
                },
                "codex/pending": {
                    "ahead": 2, "behind": 3, "ts": 1790000100,
                    "subject": "pending work",
                },
            }
            with mock.patch.object(kgctl, "branch_index", return_value=index), \
                 mock.patch.object(
                     kgctl, "counts",
                     side_effect=AssertionError("batched metadata must not call counts")), \
                 mock.patch.object(
                     kgctl, "git",
                     side_effect=AssertionError("batched metadata must not call git log")), \
                 mock.patch.object(kgctl, "changed_files", return_value=["backend/a.ts"]), \
                 mock.patch.object(kgctl, "conflict_files", return_value=[]), \
                 mock.patch.object(kgctl, "unresolved_marker_files", return_value=[]):
                rows = kgctl.scan(check_markers=False)

    by_branch = {row["branch"]: row for row in rows}
    assert by_branch["codex/merged"]["kind"] == "MERGED", rows
    assert by_branch["codex/merged"]["files"] == [], rows
    assert by_branch["codex/pending"]["kind"] == "CLEAN", rows
    assert by_branch["codex/pending"]["files"] == ["backend/a.ts"], rows
    print("selftest ok: 大批量分支快照一次读取 ahead/behind，已合并分支不再白跑 diff。")


def test_resolve_orphan_container_is_adopted_not_blocked_forever():
    """resolve 容器分支的遗留态必须能自愈，不能永远报同一个 blocked。

    真实故障（2026-10-27 07:24 与 07:38 连续两拍）：`resolve --auto --go` 挑中一条
    冲突分支后打印「codex/resolve-<slug> 已存在（可能上次留下的任务）」并 blocked。
    可这条路的两个出口（--finish / --abandon）都只认 needs_agent 记录；worktree 被回收
    或登记丢失之后，两条路都进不去，于是每拍都挑同一条、报同一个 blocked——冲突队列
    的数字永远不减，守护看着在跑、实际原地踏步。

    这条门禁钉住三条：
      1) worktree 还在 -> 接管成 needs_agent，不再 blocked；
      2) worktree 不在、树里还是原始 marker -> 撤掉容器分支重拆；
      3) worktree 不在、但树里已经没有 marker（有人真解过）-> 绝不自动删，交给 land。
    """
    import importlib.util
    from pathlib import Path
    from unittest import mock

    ops = Path(__file__).resolve().parent

    def _load(tmp):
        root = Path(tmp)
        repo = root / "state"
        (repo / "ops" / "tasks").mkdir(parents=True, exist_ok=True)
        (repo / "ops" / "truth").mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {"KG_REPO": str(repo),
                                          "KG_TRUNK": str(root / "trunk")}):
            spec = importlib.util.spec_from_file_location(
                "kgctl_resolve_orphan_selftest", ops / "kgctl.py")
            kgctl = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(kgctl)
        return root, repo, kgctl

    printed = []

    def run_case(*, worktree_exists, markers, existing_record):
        with tempfile.TemporaryDirectory(prefix="kgctl-resolve-orphan-") as tmp:
            root, repo, kgctl = _load(tmp)
            kgctl.RESOLVE = repo / "ops" / "tasks" / "resolve.json"
            kgctl.RESOLVE_DIR = repo / "ops" / "tasks" / "resolve"
            wt = root / "state-resolve-hover-power-remove-feedback"
            if worktree_exists:
                wt.mkdir()
            written, deleted = [], []

            def fake_write(rec):
                written.append(rec)

            def fake_proc(cmd, **kw):
                class R:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                if cmd[:2] == ["git", "-C"] and "branch" in cmd and "-D" in cmd:
                    deleted.append(cmd[-1])
                if cmd[:2] == ["git", "-C"] and "diff" in cmd and "--diff-filter=U" in cmd:
                    R.stdout = "frontend/src/components/NodeActionTray.tsx\n"
                if cmd[0] == "git" and "rev-parse" in cmd and "--verify" in cmd:
                    R.returncode = 0
                    R.stdout = "b75c7dd3dc\n"
                return R()

            printed.clear()
            with mock.patch.object(kgctl, "PROC_PRINT", lambda *a, **k: printed.append(" ".join(map(str, a))), create=True), \
                 mock.patch.object(kgctl, "_write_resolve_record", fake_write), \
                 mock.patch.object(kgctl, "_write_resolve_task_md", lambda rec: written.append(rec)), \
                 mock.patch.object(kgctl, "_find_resolve_task", side_effect=lambda t: existing_record), \
                 mock.patch.object(kgctl, "_lock_blocked", return_value=False), \
                 mock.patch.object(kgctl, "_align_main", lambda: None), \
                 mock.patch.object(kgctl, "git",
                                   side_effect=lambda *a, **k: "b75c7dd3dc" if "rev-parse" in a else ""), \
                 mock.patch.object(kgctl, "conflict_files", return_value=["a.ts"]), \
                 mock.patch.object(kgctl, "conflict_burden", return_value=1), \
                 mock.patch.object(kgctl, "unlanded_diff_size", return_value=(1, 10)), \
                 mock.patch.object(kgctl, "unresolved_marker_files", return_value=markers), \
                 mock.patch.object(kgctl, "unlanded_commits", return_value=[]), \
                 mock.patch.object(kgctl, "_resolve_worktree_path", return_value=wt), \
                 mock.patch.object(kgctl, "proc_run", side_effect=fake_proc), \
                 mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
                kgctl._resolve_prepare(["codex/hover-power-remove-feedback", "--go",
                                        "--max-files", "8", "--max-lines", "1200"])
            return written, deleted, list(printed)

    # 1) worktree 还在：接管成 needs_agent。
    written, deleted, out = run_case(worktree_exists=True, markers=["a.ts"], existing_record=None)
    assert written and written[0]["status"] == "needs_agent", written
    assert written[0].get("adopted") is True, written
    assert deleted == [], f"不该删任何分支：{deleted}"

    # 2) worktree 不在、树里还是 marker：撤掉容器分支重拆。
    written, deleted, out = run_case(worktree_exists=False, markers=["a.ts"], existing_record=None)
    assert deleted == ["codex/resolve-hover-power-remove-feedback"], deleted

    # 3) worktree 不在、但已经没有 marker：绝不自动删。
    written, deleted, out = run_case(worktree_exists=False, markers=[], existing_record=None)
    assert deleted == [], f"有人真解过的容器分支被自动删掉了：{deleted}"
    assert any("没有冲突标记" in line for line in out), out

    # 4) 已有 open 记录：仍然 blocked（交给 --finish/--abandon，不抢）。
    written, deleted, out = run_case(worktree_exists=True, markers=["a.ts"],
                                     existing_record={"slug": "hover-power-remove-feedback",
                                                      "status": "needs_agent"})
    assert not written, f"已有任务时不该重复登记：{written}"
    assert any("outcome=blocked" in line for line in out), out

    print("selftest ok: resolve 遗留容器分支会被接管/重拆，已解过的绝不自动删，有 open 记录不抢。")


def test_resolve_candidate_caches_branch_sizes():
    """`resolve --auto` 不许对每条冲突分支都重跑 merge-base + diff。

    真实故障（2026-10-27 07:12）：守护每拍的 resolve 步骤报 rc=124 超时、整拍空转。
    根因不是死锁，是体量闸门的顺序——84 条冲突分支，每条先跑一次
    `merge-base` + `diff --numstat`（本机 0.4s/支，全量 33s），才轮到"冲突文件
    超闸门就跳过"这种几乎免费的判断。顺序颠倒后同样的候选结论，从 56s 降到 10s。
    这条门禁把两件事钉住：预算先筛后量，且 head/main 没变时不重算。
    """
    import importlib.util
    from pathlib import Path
    from unittest import mock

    ops = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="kgctl-resolve-cache-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"KG_REPO": str(repo),
                                          "KG_TRUNK": str(root / "trunk")}):
            spec = importlib.util.spec_from_file_location(
                "kgctl_resolve_cache_selftest", ops / "kgctl.py")
            kgctl = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(kgctl)

            kgctl.DIFF_CACHE = root / "diff_probe_cache.json"
            rows = [
                # 冲突文件 20 个 > 闸门 8：必须在量 diff 之前就被筛掉。
                {"branch": "codex/too-many-files", "kind": "CONFLICT", "conflicts": [f"f{i}" for i in range(20)]},
                {"branch": "codex/ok", "kind": "CONFLICT", "conflicts": ["a.ts"]},
                {"branch": "codex/clean", "kind": "CLEAN", "conflicts": []},
            ]
            calls = []

            def fake_size(branch):
                calls.append(branch)
                return (1, 40)

            with mock.patch.object(kgctl, "scan", return_value=rows),                  mock.patch.object(kgctl, "_open_resolve_sources", return_value=set()),                  mock.patch.object(kgctl, "_decided_resolve_branches", return_value=set()),                  mock.patch.object(kgctl, "_is_generated_branch", return_value=False),                  mock.patch.object(kgctl, "conflict_burden_map",
                                   return_value={"codex/too-many-files": 20, "codex/ok": 1}),                  mock.patch.object(kgctl, "git",
                                   side_effect=lambda *a, **k: "deadbeef" if a[:2] == ("rev-parse", "--verify") or a[:2] == ("rev-parse", "main") else ""),                  mock.patch.object(kgctl, "unlanded_diff_size", side_effect=fake_size):
                cands, oversize = kgctl._pick_resolve_candidate(8, 1200)
                assert calls == ["codex/ok"], (
                    f"体量闸门顺序错了：给 {calls} 都量了 diff，超闸门的分支本该先被跳过")
                assert [c[3] for c in cands] == ["codex/ok"], cands
                assert oversize == [], oversize

                # 第二次：head/main 未变，必须命中缓存、不再跑 diff。
                calls.clear()
                kgctl._pick_resolve_candidate(8, 1200)
                assert calls == [], f"缓存没生效，又量了一遍：{calls}"

            # 缓存是纯缓存，不许无限长。
            cache = {f"b{i}@h@m": {"files": 1, "lines": 1} for i in range(900)}
            kgctl._diff_cache_save(cache)
            assert len(cache) <= 600, f"diff 缓存没有剪枝，还有 {len(cache)} 条"
    print("selftest ok: resolve 候选先过体量闸门再量 diff，且分支规模走缓存不重算。")


def test_zero_file_branches_do_not_occupy_land_queue():
    """0 文件空壳必须避开 land 队列，并在 supersede --fast 中直接归档。"""
    import contextlib
    import importlib
    import io
    from pathlib import Path
    from unittest import mock

    ops = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="kgctl-zero-file-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {
            "KG_REPO": str(repo),
            "KG_TRUNK": str(trunk),
        }):
            spec = importlib.util.spec_from_file_location(
                "kgctl_zero_file_selftest", ops / "kgctl.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            rows = [
                {"branch": "codex/empty", "ahead": 40, "behind": 500,
                 "nfiles": 0, "age": 10, "subject": "net-zero history",
                 "kind": "CLEAN", "files": [], "conflicts": [], "markers": []},
                {"branch": "codex/real", "ahead": 1, "behind": 2,
                 "nfiles": 1, "age": 1, "subject": "real change",
                 "kind": "CLEAN", "files": ["app.ts"], "conflicts": [], "markers": []},
            ]
            with mock.patch.object(module, "scan", return_value=rows):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    module.cmd_queue([])
            queue_text = output.getvalue()
            assert "kgctl land codex/real" in queue_text, queue_text
            assert "kgctl land codex/empty" not in queue_text, queue_text
            assert "1 个 0 文件空分支" in queue_text, queue_text

            checked = []
            def cherry(branch):
                checked.append(branch)
                return ["deadbeef"]

            with mock.patch.object(module, "scan", return_value=rows), \
                 mock.patch.object(module, "already_superseded", return_value=set()), \
                 mock.patch.object(module, "unlanded_commits", side_effect=cherry):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    module.cmd_supersede(["--fast"])
            supersede_text = output.getvalue()
            assert "codex/empty" in supersede_text, supersede_text
            assert "codex/real" not in supersede_text.split("可自动归档", 1)[1], supersede_text
            assert checked == ["codex/real"], checked

            with mock.patch.object(module, "changed_files", return_value=[]), \
                 mock.patch.object(
                     module, "unlanded_commits",
                     side_effect=AssertionError("0 文件分支不应调用 git cherry")):
                ok, notes = module.branch_content_landed("codex/empty")
            assert ok is True and notes == ["净零文件：分支相对自身基线没有文件级改动"], notes

    print("selftest ok: 0 文件空壳不占 land 队列，supersede 不再为它们跑 git cherry。")


def test_prune_keeps_open_resolve_hold():
    """Restart cleanup must not erase a hold for a gate_fail resolve task."""
    import importlib
    from unittest import mock
    import sys
    from pathlib import Path

    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import central_daemon
    importlib.reload(central_daemon)

    rid = "resolve:stuck"
    blocked = {rid: {"reason": "resolve_conflict_needs_decision",
                     "ts": "2099-01-01 00:00:00"}}
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-hold-prune-") as tmp, \
         mock.patch.object(central_daemon, "RUNTIME", Path(tmp)), \
         mock.patch.object(central_daemon, "_ledger_latest", return_value={}), \
         mock.patch.object(central_daemon, "_resolve_records_all",
                           return_value=[{"slug": "stuck", "status": "gate_fail"}]), \
         mock.patch.object(central_daemon, "_state_repo", return_value=None):
        stale = central_daemon._prune_exec_blocked(blocked)

    assert rid not in stale, stale
    assert rid in blocked, blocked
    print("selftest ok: 重启清理不会把仍 gate_fail 的 resolve 停手记录误删。")


def test_pressure_reap_engages_even_when_regular_reaping_is_on():
    """磁盘紧张时必须走压力节奏，哪怕常规回收本来就是开着的。

    2026-10-27 根因：_pressure_reap 只在「常规回收被关掉」的分支里才置 True。
    safety.json 默认 reap_worktrees=true，于是压力分支整个被跳过 —— 一直用
    常规 25s 预算 / 600s 间隔，日志反复「达时间预算 25s（已回收 0 个）」，
    回收永远追不上派工写入，磁盘周期性被写爆。
    """
    import importlib
    import sys
    from unittest import mock

    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import central_daemon
    importlib.reload(central_daemon)

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-pressure-on-") as tmp:
        home = Path(tmp)
        ws = home / "ws"
        ws.mkdir()
        saved = {}

        def fake_save(book):
            saved.clear()
            saved.update(book)

        with mock.patch.object(central_daemon, "RUNTIME", home), \
             mock.patch.object(central_daemon, "_workspace_root", return_value=ws), \
             mock.patch.object(central_daemon, "_knob", return_value=True), \
             mock.patch.object(central_daemon, "_disk_free_gb", return_value=5.0), \
             mock.patch.object(central_daemon, "_live_exec_slugs", return_value=[]), \
             mock.patch.object(central_daemon, "_load_reap_state", return_value={}), \
             mock.patch.object(central_daemon, "_save_reap_state", side_effect=fake_save):
            central_daemon._reap_dead_worktrees()
            # 压力模式必须真的进入扫描并写状态（常规节奏在节流后才会走到这里）
            assert saved.get("last_ts"), saved

        # 低盘 + 常规回收开着 -> 仍走压力节奏与压力预算
        src = __import__("inspect").getsource(central_daemon._reap_dead_worktrees)
        assert "_disk_tight" in src, "紧张度必须独立判断，不能只在常规回收关闭时才成立"
        assert "REAP_INTERVAL_PRESSURE_S if _pressure_reap" in src
        assert "REAP_TIME_BUDGET_PRESSURE_S if _pressure_reap" in src

    print("selftest ok: 盘紧时即使常规回收开着，也走压力节奏与压力预算。")


def test_pressure_reap_gets_its_own_time_budget():
    """压力回收必须有自己的时间预算，否则永远追不上派工写入。

    2026-10-27 实测：守护日志连续出现「worktree 回收达时间预算 25s（已回收 0 个）」。
    切片 40 个现场、每个还要跑 4~6 次 git 子进程，25s 常常连一个都删不掉；而
    每个 worker 现场 ~200MB，磁盘就这样一路滑到 5GB 告急、反复把人卡死。
    常规轮必须保留短预算（别把派活拖住），压力轮必须给足。
    """
    import sys

    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import central_daemon

    assert central_daemon.REAP_TIME_BUDGET_PRESSURE_S > (
        central_daemon.REAP_TIME_BUDGET_S
    ), (central_daemon.REAP_TIME_BUDGET_PRESSURE_S,
        central_daemon.REAP_TIME_BUDGET_S)
    assert central_daemon.REAP_TIME_BUDGET_PRESSURE_S >= 60, (
        "压力预算必须覆盖一整片现场的实际扫描与删除成本"
    )
    import inspect
    src = inspect.getsource(central_daemon._reap_dead_worktrees)
    assert "REAP_TIME_BUDGET_PRESSURE_S if _pressure_reap" in src, src[:400]
    print("selftest ok: 压力回收有独立且更大的时间预算，常规轮仍用短预算。")


def test_reap_scan_is_sliced_and_throttled():
    """回收不能再「每拍全扫」：那是它自己制造负载和反复爆盘的根因。

    实测 2026-10-27：242 个 worktree，每个还要跑 4~6 次 git 子进程；守护每 15 秒
    全扫一遍，单独就把 load 顶到 191，机器越跑越卡，人只能反复清一轮。
    这里锁住三条结构属性：
      1. 一次只扫一片（KG_REAP_SCAN_SLICE），游标逐轮前进，最终全部扫到；
      2. 两次扫描之间有节流，不会每拍都动；
      3. 回收账本单独文件，不覆盖 state.json 里派发阶段刚写的磁盘闸状态。
    """
    import importlib
    from unittest import mock
    import sys
    from pathlib import Path

    ops = Path(__file__).resolve().parent
    if str(ops) not in sys.path:
        sys.path.insert(0, str(ops))
    import central_daemon
    importlib.reload(central_daemon)

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-reap-slice-") as tmp:
        base = Path(tmp)
        home = base / "home"
        workspace = base / "ws"
        home.mkdir(); workspace.mkdir()
        # 造 60 个假现场，远多于单次切片；mtime 拨到 5 小时前，
        # 否则会被「刚动过」的廉价预筛正确跳过。
        old_ts = _time.time() - 5 * 3600
        for i in range(60):
            d = workspace / f"kg-fake-{i:03d}"
            d.mkdir()
            os.utime(d, (old_ts, old_ts))

        calls = []

        def fake_worktree_repo(wt):
            calls.append(wt.name)
            return None          # 不是 worktree -> 跳过，不实际删除任何东西

        with mock.patch.object(central_daemon, "RUNTIME", home), \
             mock.patch.object(central_daemon, "REAP_STATE", home / "reap_state.json"), \
             mock.patch.dict(os.environ, {
                 "KG_WORKSPACE_ROOT": str(workspace),
                 "KG_REAP_SCAN_SLICE": "7",
                 "KG_REAP_MIN_INTERVAL_S": "600",
                 "KG_REAP_BACKENDS": "0"}, clear=False), \
             mock.patch.object(central_daemon, "_live_exec_slugs",
                               return_value=frozenset()), \
             mock.patch.object(central_daemon, "_disk_free_gb",
                               return_value=20.0), \
             mock.patch.object(central_daemon, "_knob",
                               side_effect=lambda n, d=True: n == "reap_worktrees"), \
             mock.patch.object(central_daemon.workspace_guard,
                               "worktree_removable", return_value={"ok": False}), \
             mock.patch.object(central_daemon, "_worktree_repo",
                               side_effect=fake_worktree_repo):
            importlib.reload(central_daemon)
            central_daemon.REAP_STATE = home / "reap_state.json"
            central_daemon.REAP_SCAN_SLICE = 7
            central_daemon.REAP_MIN_INTERVAL_S = 600.0
            central_daemon._knob = lambda n, d=True: n == "reap_worktrees"
            central_daemon._worktree_repo = fake_worktree_repo
            central_daemon._live_exec_slugs = lambda *a, **k: frozenset()
            central_daemon._disk_free_gb = lambda: 20.0

            first = central_daemon._reap_dead_worktrees()
            assert first == 0, first
            first_n = len(calls)
            assert first_n == 7, f"单次扫描没有按切片限制（扫了 {first_n} 个）"

            # 节流：立刻再跑一次必须直接返回，不再扫
            calls.clear()
            central_daemon._reap_dead_worktrees()
            assert calls == [], f"节流失效，立刻又扫了 {calls}"

            # 把时间戳拨回过去，模拟下一次窗口：游标必须前进到新的一片
            import json as _json
            book = _json.loads((home / "reap_state.json").read_text())
            assert book["last_scan_n"] == 7, book
            assert book["last_scan_total"] == 60, book
            book["last_ts"] = 0
            (home / "reap_state.json").write_text(_json.dumps(book))
            calls.clear()
            central_daemon._reap_dead_worktrees()
            assert len(calls) == 7, f"第二次扫描也没按切片: {calls}"

            # 回收账本不能写进 state.json（否则覆盖磁盘闸状态）
            state_path = home / "state.json"
            if state_path.exists():
                state = _json.loads(state_path.read_text())
                assert "reap_cursor" not in state, state
                assert "reap_last_ts" not in state, state

    print("selftest ok: 回收按游标切片 + 节流，账本独立，不再每拍全扫拖垮机器。")


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
                   "reopen_item", "register_acceptance",
                   "codex_native_dispatch_task", "codex_native_follow_up"} - tool_names
        assert not missing, f"缺少自动挡工具: {sorted(missing)}"

        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "autopilot_status", "arguments": {"fast": True}}})
        sc = recv()["result"]["structuredContent"]
        assert "state" in sc and "switches" in sc
        assert "product_state" in sc and "judgments" in sc and "last_verification" in sc, \
            "状态快照缺少产品状态/判断逻辑/最近验证"
        assert "mainline" in sc, "状态快照缺少产品主线摘要"
        assert "route" in sc and sc["route"]["commander"]["label"] == "主司令窗", \
            "状态快照缺少主司令窗路线"
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
        send({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
              "params": {"name": "autopilot_control", "arguments": {"action": "disable"}}})
        off = recv()["result"]["structuredContent"]
        assert off["enabled"] is False and off["lifecycle"] == "off"
        send({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
              "params": {"name": "intake_request",
                         "arguments": {"text": "must reject while off"}}})
        refused = recv()["result"]
        assert refused["isError"] is True
        assert "AUTOPILOT_DISABLED" in _json.dumps(refused, ensure_ascii=False)
        send({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
              "params": {"name": "autopilot_control", "arguments": {"action": "enable"}}})
        on = recv()["result"]["structuredContent"]
        assert on["enabled"] is True and on["lifecycle"] == "on"
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


def test_closure_rate():
    """北极星首问闭环率：必须算得出真数，算不出必须红，不许冒充 0%。"""
    import closure_rate
    import json as _json
    with tempfile.TemporaryDirectory(prefix="autopilot-closure-rate-") as tmp:
        truth = Path(tmp) / "ops" / "truth"
        truth.mkdir(parents=True)
        ledger = truth / "ledger.jsonl"

        def write(rows):
            ledger.write_text(
                "\n".join(_json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8")

        # 3 条：1 条带证据闭环、1 条仍 open、1 条标 done 却没有证据。
        write([
            {"id": "L-0001", "type": "ledger", "status": "done",
             "evidence": "python3 ops/selftest.py -> exit 0"},
            {"id": "L-0002", "type": "ledger", "status": "open"},
            {"id": "L-0003", "type": "ledger", "status": "done",
             "evidence": "   "},
        ])
        res = closure_rate.compute(tmp)
        assert res["status"] == "ok", res
        assert res["denominator"] == 3 and res["numerator"] == 1, res
        assert res["rate_pct"] == 33.3, res
        assert res["done_without_evidence"] == ["L-0003"], \
            "标 done 却没有证据的必须按未闭环并单独报出来"
        assert "33.3%" in closure_rate.human_line(res), closure_rate.human_line(res)

        # 分母为 0：必须是 no_data +「取不到」，绝不能算成 0% 或「正常」。
        write([])
        res = closure_rate.compute(tmp)
        assert res["status"] == "no_data" and res["rate_pct"] is None, res
        assert "取不到" in closure_rate.human_line(res), closure_rate.human_line(res)

        # 台账损坏：unreadable 行会让比例失真，必须 fail-closed 成 unknown。
        ledger.write_text("{\"broken\":\n", encoding="utf-8")
        res = closure_rate.compute(tmp)
        assert res["status"] == "unknown" and res["rate_pct"] is None, res
        assert "取不到" in closure_rate.human_line(res), closure_rate.human_line(res)

        # 台账缺失：同样是 unknown，不能静默成「没有问题」。
        ledger.unlink()
        res = closure_rate.compute(tmp)
        assert res["status"] == "unknown", res

        # 门禁退出码：算得出=0，算不出=2（先放回一条正常台账，再删掉验红）。
        write([{"id": "L-0009", "type": "ledger", "status": "done",
                "evidence": "cmd -> exit 0"}])
        assert closure_rate.main(["--repo", tmp]) == 0
        ledger.unlink()
        assert closure_rate.main(["--repo", tmp]) == 2

    # 面板与状态文本必须带上这个字段，否则首屏看不到北极星。
    import central_mcp
    payload = central_mcp._status_payload(fast=True)
    assert "closure_rate" in payload, "状态快照缺少 closure_rate"
    line = central_mcp._closure_rate_line(payload.get("closure_rate"))
    assert "首问闭环率" in line, line
    print("selftest ok: 北极星首问闭环率三态、fail-closed 退出码与面板接线全部通过。")


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


def test_rotation_streaming():
    """轮换只等 thread.started，不等首次长回复结束。"""
    import importlib
    import signal

    sys.path.insert(0, str(OPS))
    cr = importlib.import_module("cockpit_rotate")

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-rotate-") as tmp:
        root = Path(tmp)
        fake = root / "codex"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, time\n"
            "print(json.dumps({'type':'thread.started',"
            "'thread_id':'01a12345-6789-7abc-8def-0123456789ab','pid':os.getpid()}),"
            " flush=True)\n"
            "time.sleep(5)\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        real_cli, real_runs = cr._resolve_codex_cli, cr.ROTATE_RUNS
        cr._resolve_codex_cli = lambda: (str(fake), "fake codex")
        cr.ROTATE_RUNS = root / "rotate-runs"
        try:
            started = _time.monotonic()
            tid, log_path = cr.new_thread("handoff", str(root), None)
            elapsed = _time.monotonic() - started
            assert tid == "01a12345-6789-7abc-8def-0123456789ab", (tid, log_path)
            assert elapsed < 2.0, f"轮换等待了 {elapsed:.2f}s，仍在等首次回复"
            log = Path(log_path).read_text(encoding="utf-8")
            row = next(line for line in log.splitlines() if '"thread.started"' in line)
            child_pid = json.loads(row)["pid"]
            try:
                os.killpg(child_pid, signal.SIGTERM)
            except OSError:
                pass
        finally:
            cr._resolve_codex_cli, cr.ROTATE_RUNS = real_cli, real_runs

    print("selftest ok: 窗口轮换在 thread.started 后立即返回，不受首次长回复阻塞。")


if __name__ == "__main__":
    main()
    test_replan()
    test_resolve()
    test_resolve_exec_parallel()
    test_collect_exec_keeps_reaping_after_nonlanding_failure()
    test_resolve_gate_fail_auto_retry()
    test_resolve_gate_fail_not_retried_same_cycle()
    test_resolve_candidate_scan_skips_marker_grep()
    test_scan_batches_branch_metadata()
    test_resolve_candidate_caches_branch_sizes()
    test_resolve_orphan_container_is_adopted_not_blocked_forever()
    test_zero_file_branches_do_not_occupy_land_queue()
    test_conflict_decision_hold_survives_existing_markers()
    test_prune_keeps_open_resolve_hold()
    test_reset_trunk_truth_conflict()
    test_sync_trunk_truth_failclosed_non_truth()
    test_publish_lock_and_origin_containment()
    test_stale_lock_selfheals_and_never_deletes_others()
    test_remote_benchmark_pollution_failclosed()
    test_control_state()
    test_installer()
    test_installer_requires_clean_source()
    test_installer_shell_portability()
    test_daemon_project_binding()
    test_daemon_plist_repoints_on_upgrade()
    test_daemon_control_action()
    test_companion_contract()
    test_panel_contract()
    test_preview_check_contract()
    test_installed_copy_binding()
    test_panel_first_screen()
    test_codex_native_bridge()
    test_native_socket_lstat()
    test_native_section_grouping()
    test_native_call_budget()
    test_real_codex_app_server()
    test_verify_and_truth()
    test_prune_exec_blocked_landed_commit()
    test_auto_exec()
    test_exec_model_balance_fallback()
    test_exec_model_fallback_relaunch_is_async()
    test_parallel_exec_slots()
    test_disk_hold_withholds_worker_restart()
    test_git_gc_never_prunes_shared_partial_clone()
    test_proc_run_never_hangs_on_inherited_pipe()
    test_run_kgctl_streams_progress_live()
    test_context_packs_are_cached_and_budgeted()
    test_cycle_has_hard_budget()
    test_git_scan_timeout_fails_closed_not_open()
    test_exec_prompt_includes_patch_recovery()
    test_worker_alive_treats_zombie_as_dead()
    test_dead_pid_never_fabricates_worker_success()
    test_hydrate_drops_unverifiable_exec_slots()
    test_pressure_reap_gets_its_own_time_budget()
    test_pressure_reap_engages_even_when_regular_reaping_is_on()
    test_reap_scan_is_sliced_and_throttled()
    test_mcp_contract()
    test_closure_rate()
    test_budget_gate()
    test_rotation_streaming()
