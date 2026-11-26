#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：失效的 resolve worktree 登记不能永久挡住容器分支回收。

2026-11-25 实测：`codex/resolve-reap-identical-tracked-20261027` 的 worktree 目录
已经消失，但 `.git/worktrees/<name>` 登记仍在，`git branch -D` 报
`used by worktree`，于是 `kgctl resolve --auto --go` 每拍都卡在同一条容器分支。

本门禁用真实 Git worktree 夹具锁住四件事：
  * 裸 `git branch -D` 在这类登记下确实失败（夹具不是假现场）；
  * 受限 helper 只归档精确命中的 `codex/resolve-*` 失效登记，并留下证据；
  * 归档后容器分支能删除；
  * 真实 worktree 与非 resolve 分支的失效登记完全不动。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
KGCTL_PATH = OPS / "kgctl.py"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} 失败\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc


def seed_repo(path: Path) -> Path:
    (path / "ops" / "tasks").mkdir(parents=True)
    (path / "ops" / "truth").mkdir(parents=True)
    (path / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (path / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
    (path / "ops" / "central.config.json").write_text("{}\n", encoding="utf-8")
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Stale Worktree Test")
    (path / "doc.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "doc.txt")
    git(path, "commit", "-q", "-m", "init")
    return path


def load_kgctl(repo: Path):
    saved = {key: os.environ.get(key) for key in
             ("KG_REPO", "KG_TRUNK", "KG_MODEL_PROBE", "KG_TRUTH_ID_STATE_ROOT",
              "KG_BRANCH_PREFIX")}
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(repo)
    os.environ["KG_MODEL_PROBE"] = "off"
    os.environ["KG_BRANCH_PREFIX"] = "codex/"
    os.environ["KG_TRUTH_ID_STATE_ROOT"] = str(repo.parent / "truth-ids")
    try:
        spec = importlib.util.spec_from_file_location("kgctl_stale_worktree_test", KGCTL_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def make_stale(repo: Path, branch: str, path: Path) -> None:
    git(repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
    assert path.is_dir(), path
    shutil.rmtree(path)
    assert not path.exists(), path


def admin_dir(repo: Path, branch: str) -> Path:
    worktrees = repo / ".git" / "worktrees"
    for candidate in worktrees.iterdir():
        if not candidate.is_dir():
            continue
        head = (candidate / "HEAD").read_text(encoding="utf-8").strip()
        if head == f"ref: refs/heads/{branch}":
            return candidate
    raise AssertionError(f"找不到 {branch} 的 worktree 登记")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="kg-stale-resolve-registration-") as tmp:
        root = Path(tmp)
        repo = seed_repo(root / "state")
        kg = load_kgctl(repo)

        stale_branch = "codex/resolve-stale-registration"
        stale_path = root / "old-resolve-worktree"
        make_stale(repo, stale_branch, stale_path)
        registration = admin_dir(repo, stale_branch)

        before = git(repo, "branch", "-D", stale_branch, check=False)
        assert before.returncode != 0, "夹具没有复现 used by worktree，测试失去意义"
        assert "used by worktree" in before.stderr, before.stderr

        assert kg._prune_stale_worktree_registration(stale_branch) is True
        assert not registration.exists(), "失效登记应被移出 Git 的 worktrees 扫描目录"
        archived = list((repo / ".git" / "kgctl-stale-worktrees").iterdir())
        assert len(archived) == 1, archived
        assert (archived[0] / "HEAD").read_text(encoding="utf-8").strip() == (
            f"ref: refs/heads/{stale_branch}")

        gone = git(repo, "branch", "-D", stale_branch, check=False)
        assert gone.returncode == 0, gone.stderr
        assert git(repo, "rev-parse", "--verify", stale_branch, check=False).returncode != 0

        # 真实 worktree 的登记不能因为同名前缀就被误归档。
        live_branch = "codex/resolve-live-registration"
        live_path = root / "live-resolve-worktree"
        git(repo, "worktree", "add", "-q", "-b", live_branch, str(live_path), "main")
        live_registration = admin_dir(repo, live_branch)
        assert kg._prune_stale_worktree_registration(live_branch) is False
        assert live_registration.exists(), "真实 worktree 登记被误清理"
        assert live_path.is_dir(), "真实 worktree 目录被误删"

        # 用户分支即使留下失效登记，也不属于自动挡可清理的范围。
        user_branch = "codex/user-stale-registration"
        user_path = root / "old-user-worktree"
        make_stale(repo, user_branch, user_path)
        user_registration = admin_dir(repo, user_branch)
        assert kg._prune_stale_worktree_registration(user_branch) is False
        assert user_registration.exists(), "非 resolve 分支登记被误清理"
        blocked = git(repo, "branch", "-D", user_branch, check=False)
        assert blocked.returncode != 0, "非 resolve 分支的失效登记不应被绕过"

        # 覆盖真实调用入口，而不只是 helper 本身。
        integration = "codex/resolve-integration-registration"
        make_stale(repo, integration, root / "old-integration-worktree")
        assert kg._delete_resolve_container(integration, "测试残留容器分支") is None
        assert git(repo, "rev-parse", "--verify", integration, check=False).returncode != 0

    print("test-stale-resolve-worktree-registration: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
