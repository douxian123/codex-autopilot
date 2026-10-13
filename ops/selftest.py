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


def kgctl(repo, trunk, *args):
    env = os.environ.copy()
    env["KG_REPO"] = str(repo)
    env["KG_TRUNK"] = str(trunk)
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


if __name__ == "__main__":
    main()
