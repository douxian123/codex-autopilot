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
    assert kgctl._truthctl_path().resolve() == project_truthctl.resolve(), kgctl._truthctl_path()
    assert kgctl._truthctl_path(project).resolve() == project_truthctl.resolve()

    source = (ROOT / "ops" / "kgctl.py").read_text(encoding="utf-8")
    assert 'RUNTIME_OPS / "truthctl.py"' not in source
    assert 'wt / "ops" / "truthctl.py"' not in source

print("runtime truthctl selection: PASS (project copy wins; no direct runtime bypass)")
