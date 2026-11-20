#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""总闸必须能在真正非 git 的 release 目录里跑完安装用例。

历史故障：test_installer 把 OPS.parent 同时当安装源和状态根。源码仓库里成立，
但 Codex 把插件快照到 ~/.codex/plugins/cache/... 后，OPS.parent 不是 git 仓，
安装器按生产规则正确拒绝，总闸却因此恒红。这里用真正剥离 .git 的版本副本
锁住正确夹具：测试自己造 git 源仓 + detached worktree，安装器对非 git 状态根
的拒绝必须保留。
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
ROOT = OPS.parent


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="selftest-release-dir-") as raw:
        base = Path(raw)
        release = base / "release"
        shutil.copytree(ROOT, release,
                        ignore=shutil.ignore_patterns(".git", ".build"))
        outer = run(["git", "-C", str(release), "rev-parse",
                     "--is-inside-work-tree"])
        if outer.returncode == 0 and outer.stdout.strip() == "true":
            print("fixture still resolves as a git work tree", file=sys.stderr)
            return 1

        spec = importlib.util.spec_from_file_location(
            "release_selftest", release / "ops" / "selftest.py")
        selftest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selftest)
        selftest.test_installer()

        home = base / "home"
        home.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(home)
        rejected = run([
            "bash", str(release / "install.sh"), "--no-seed", "--no-marketplace",
            "--no-companion", str(release),
        ], env=env)
        combined = rejected.stdout + rejected.stderr
        if rejected.returncode == 0 or "--state-root 必须是 git 仓库" not in combined:
            print("安装器放宽了非 git 状态根的生产校验：\n" + combined,
                  file=sys.stderr)
            return 1

    print("selftest release dir: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
