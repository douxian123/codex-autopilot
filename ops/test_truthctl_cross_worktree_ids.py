#!/usr/bin/env python3
"""回归：独立 clone 共用项目真相时，编号分配不能从同一低水位重来。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
TRUTHCTL = OPS / "truthctl.py"


def run(cmd, cwd, env):
    result = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(
            f"命令失败: {' '.join(map(str, cmd))}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def make_clone(origin, dest):
    run(["git", "clone", "-q", str(origin), str(dest)], dest.parent, os.environ.copy())
    (dest / "ops" / "tasks").mkdir(parents=True)
    (dest / "ops" / "truth").mkdir(parents=True)
    (dest / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    for name in ("decision", "ledger", "progress", "acceptance", "judgment"):
        (dest / "ops" / "truth" / f"{name}s.jsonl").write_text("", encoding="utf-8")
    return dest


def add_progress(repo, state_dir, title, env_extra=None):
    env = os.environ.copy()
    env.update({
        "KG_REPO": str(repo),
        "KG_TRUTH_ID_STATE_DIR": str(state_dir),
        "KG_TRUTH_PROJECT_ID": "cross-worktree-regression",
    })
    if env_extra:
        env.update(env_extra)
    return run(
        [sys.executable, str(TRUTHCTL), "add", "progress", title],
        OPS.parent,
        env,
    )


def ids_from(path):
    return [
        json.loads(line)["id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main():
    with tempfile.TemporaryDirectory(prefix="truthctl-cross-worktree-") as tmp:
        root = Path(tmp)
        origin = root / "origin.git"
        run(["git", "init", "-q", "--bare", str(origin)], root, os.environ.copy())

        seed = make_clone(origin, root / "seed")
        run(["git", "-C", str(seed), "config", "user.email", "test@example.invalid"],
            root, os.environ.copy())
        run(["git", "-C", str(seed), "config", "user.name", "Cross Worktree Test"],
            root, os.environ.copy())
        (seed / "README").write_text("fixture\n", encoding="utf-8")
        run(["git", "-C", str(seed), "add", "README"], root, os.environ.copy())
        run(["git", "-C", str(seed), "commit", "-q", "-m", "fixture"],
            root, os.environ.copy())
        run(["git", "-C", str(seed), "push", "-q", "origin", "HEAD:main"],
            root, os.environ.copy())

        first = make_clone(origin, root / "first")
        second = make_clone(origin, root / "second")
        state_dir = root / "id-state"

        add_progress(first, state_dir, "first")
        add_progress(second, state_dir, "second")

        assert ids_from(first / "ops" / "truth" / "progress.jsonl") == ["P-0001"]
        assert ids_from(second / "ops" / "truth" / "progress.jsonl") == ["P-0002"]

        state = json.loads(
            (state_dir / "truth-id-sequences.json").read_text(encoding="utf-8"))
        assert state["progress"] == 2, state


if __name__ == "__main__":
    main()
    print("truthctl cross-worktree id regression: PASS")
