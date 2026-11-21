#!/usr/bin/env python3
"""Regression tests for per-call project routing."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from request_context import RequestContextError, resolve_project


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "project"
        (root / "ops/tasks").mkdir(parents=True)
        (root / "ops/truth").mkdir(parents=True)

        assert resolve_project(project=str(root)) == root.resolve()
        assert resolve_project(cwd=str(root / "ops")) == root.resolve()

        try:
            resolve_project(project=str(Path(td) / "missing"))
        except RequestContextError:
            pass
        else:
            raise AssertionError("invalid project accepted")

        try:
            resolve_project(cwd=str(Path(td) / "not-a-project"))
        except RequestContextError as exc:
            assert "未写入任何项目账本" in str(exc)
        else:
            raise AssertionError("invalid cwd accepted")

    print("request context routing: PASS")


if __name__ == "__main__":
    main()
