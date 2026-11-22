#!/usr/bin/env python3
"""Prove kgctl prefers project truthctl over the bundled runtime copy."""

import importlib.util
import os
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_kgctl(project: Path):
    old = os.environ.get("KG_REPO")
    os.environ["KG_REPO"] = str(project)
    try:
        spec = importlib.util.spec_from_file_location("kgctl_under_test", ROOT / "ops" / "kgctl.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            os.environ.pop("KG_REPO", None)
        else:
            os.environ["KG_REPO"] = old


with tempfile.TemporaryDirectory(prefix="runtime-truthctl-") as tmp:
    project = Path(tmp) / "project"
    project_truthctl = project / "ops" / "truthctl.py"
    project_truthctl.parent.mkdir(parents=True)
    (project / "ops" / "tasks").mkdir()
    (project / "ops" / "truth").mkdir()
    project_truthctl.write_text("# project copy\n", encoding="utf-8")

    kgctl = load_kgctl(project)
    # 正向：项目自带一份时，必须用项目那份，绝不能被插件运行时那份盖过去。
    assert kgctl._truthctl_path().resolve() == project_truthctl.resolve(), kgctl._truthctl_path()
    assert kgctl._truthctl_path(project).resolve() == project_truthctl.resolve()

    # 反向（此前缺失，正是它让「回退」和「绕过」分不清）：
    #
    #   a) 不指定 root 时，候选顺序是 REPO -> CODE_ROOT -> trunk -> RUNTIME_OPS。
    #      项目里没有 ops/truthctl.py 时要落到插件运行时那一份，而不是抛错 ——
    #      自包含安装下项目里本来就不带 ops/ 运行时代码。
    #   b) 显式指定 root 时，只认那一个 root：缺了就要 fail-closed 抛错，绝不
    #      偷偷改用运行时那份（那会把「用户的账本」写到插件自带的代码上）。
    runtime_copy = (ROOT / "ops" / "truthctl.py").resolve()
    assert runtime_copy.is_file(), runtime_copy
    bare = Path(tmp) / "bare-project"
    (bare / "ops" / "tasks").mkdir(parents=True)
    (bare / "ops" / "truth").mkdir()
    kgctl_bare = load_kgctl(bare)
    assert kgctl_bare._truthctl_path().resolve() == runtime_copy, kgctl_bare._truthctl_path()
    try:
        kgctl_bare._truthctl_path(bare)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("显式 root 缺 truthctl 时必须 fail-closed，而不是回退到运行时副本")

    # synthesize 在本版还会刷新项目宪法页。项目自带旧 truthctl 没有 constitution
    # 子命令时，kgctl 必须改用有该能力的运行时副本，否则 tick 每拍绿灯但页面永远不生成。
    stale = Path(tmp) / "stale-project"
    stale_truthctl = stale / "ops" / "truthctl.py"
    stale_truthctl.parent.mkdir(parents=True)
    (stale / "ops" / "tasks").mkdir()
    (stale / "ops" / "truth").mkdir()
    stale_truthctl.write_text("CMDS = {'synthesize': 1}\n", encoding="utf-8")
    kgctl_stale = load_kgctl(stale)
    assert kgctl_stale._truthctl_path().resolve() == stale_truthctl.resolve()
    assert kgctl_stale._truthctl_path(required_command="constitution").resolve() == runtime_copy.resolve()

    capable = Path(tmp) / "capable-project"
    capable_truthctl = capable / "ops" / "truthctl.py"
    capable_truthctl.parent.mkdir(parents=True)
    (capable / "ops" / "tasks").mkdir()
    (capable / "ops" / "truth").mkdir()
    capable_truthctl.write_text("CMDS = {'synthesize': 1, 'constitution': 1}\n", encoding="utf-8")
    kgctl_capable = load_kgctl(capable)
    assert kgctl_capable._truthctl_path(required_command="constitution").resolve() == capable_truthctl.resolve()

    source = (ROOT / "ops" / "kgctl.py").read_text(encoding="utf-8")

    # 源码层的判据从「不许出现某段文本」改成「出现时只能是回退分支」。
    #
    # 原来写的是 `'RUNTIME_OPS / "truthctl.py"' not in source`，在 2026-10-25 当时
    # 成立；但 2026-11-16/17 两次修复（跨项目路由、全局单例守护）**合法地**加了
    # 「项目没带 truthctl 就回退运行时」的分支，这段文本于是必然出现，断言恒红，
    # 而真实行为是对的。判据过宽 → 只能靠放宽来变绿，等于把回归藏起来。
    #
    # 换成抓真正的不变量：每一处 `RUNTIME_OPS / "truthctl.py"` 都必须长成
    # `... if <own>.is_file() else (RUNTIME_OPS / "truthctl.py")` 这种回退形态，
    # 也就是只在「项目那份不存在」时才可达。将来谁把运行时那份提到首选位置，
    # 这里就会红。
    fallback = 'else (RUNTIME_OPS / "truthctl.py")'
    occurrences = source.count('RUNTIME_OPS / "truthctl.py"')
    assert occurrences >= 1, "运行时 truthctl 的回退分支不该被整体删掉"
    assert source.count(fallback) == occurrences, (
        "RUNTIME_OPS/truthctl.py 只能作为 own.is_file() 为假时的回退出现，"
        f"实际 {source.count(fallback)}/{occurrences} 处是回退形态"
    )
    # 「项目自带优先」的入口本身也要保持：不得直接拼死运行时路径当作首选。
    assert 'own if own.is_file() else' in source
    assert 'wt / "ops" / "truthctl.py"' not in source
    assert '_TRUTH_SUBCOMMAND_CAPABILITIES.get(sub)' in source, (
        "synthesize/constitution 必须走能力探测，不能只改 helper 却忘记接线")

print("runtime truthctl selection: PASS (project copy wins when capable; runtime copy supplies missing commands)")
