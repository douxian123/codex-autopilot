#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：常驻自追踪只在真相内容变化时重写产品主线。

自动挡每拍都会 `kgctl tick` -> `truthctl synthesize`。旧实现每次都写新的生成时间，
于是「什么都没发生」也会让 docs/PRODUCT_MAINLINE.md 变脏，并把假进展写进真相。
这条门禁同时锁住三件事：
  1. `--if-changed` 在内容不变时不改文件；
  2. 真相真的变化后仍会刷新内容；
  3. kgctl tick 与守护 tick pass 确实走在这条链路上。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ok   " if ok else "  FAIL ") + label + (f" ({detail})" if detail else ""))
    return ok


def truth_env(root: Path) -> dict:
    return {
        **os.environ,
        "KG_REPO": str(root),
        "KG_TRUTH_ID_STATE_ROOT": str(root / "truth-ids"),
        "KG_MODEL_PROBE": "off",
    }


def run_truthctl(root: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(OPS / "truthctl.py"), *args],
        cwd=str(root), env=truth_env(root), capture_output=True, text=True,
    )


def build_repo(root: Path) -> Path:
    (root / "ops" / "truth").mkdir(parents=True)
    (root / "ops" / "tasks").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (root / "ops" / "truth" / "product_state.json").write_text(
        json.dumps({"updated": "2026-11-19", "modules": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    for name in ("ledger", "progress", "decisions", "acceptance",
                 "judgments", "constraints"):
        (root / "ops" / "truth" / f"{name}.jsonl").write_text("", encoding="utf-8")
    (root / "ops" / "truth" / "mainline.json").write_text(
        json.dumps({
            "product": "Codex 自动挡",
            "one_liner": "一句话入口",
            "stage": "内部可用",
            "north_star": "一次提出就闭环",
            "path": ["输入归一", "真相沉淀"],
            "current_focus": ["常驻自追踪"],
            "not_doing": [],
            "roadmap": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return root / "docs" / "PRODUCT_MAINLINE.md"


def call_has_args(node: ast.AST, func_name: str, args: tuple[str, ...]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = ""
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    if name != func_name:
        return False
    actual = []
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            actual.append(arg.value)
        else:
            return False
    return tuple(actual) == args


def call_has_string_list_arg(node: ast.AST, func_name: str,
                             args: tuple[str, ...]) -> bool:
    """识别 `run_kgctl(["tick", ...], env_extra=...)` 这类首参数为列表的调用。"""
    if not isinstance(node, ast.Call):
        return False
    name = ""
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    if name != func_name or not node.args:
        return False
    first = node.args[0]
    if not isinstance(first, ast.List):
        return False
    actual = []
    for item in first.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return False
        actual.append(item.value)
    return tuple(actual) == args


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory(prefix="selftrack-mainline-gate-") as tmp:
        root = Path(tmp)
        mainline = build_repo(root)

        first = run_truthctl(root, "synthesize", "--if-changed")
        ok &= check("首次综合成功", first.returncode == 0,
                    (first.stdout + first.stderr)[-300:])
        first_text = mainline.read_text(encoding="utf-8") if mainline.exists() else ""
        ok &= check("首次综合写出主线", "常驻自追踪" in first_text)

        second = run_truthctl(root, "synthesize", "--if-changed")
        second_text = mainline.read_text(encoding="utf-8")
        ok &= check("无变化时命令成功", second.returncode == 0,
                    (second.stdout + second.stderr)[-300:])
        ok &= check("无变化时文件逐字不变", second_text == first_text)
        ok &= check("无变化时输出说明原因", "内容未变化" in second.stdout)

        ml_path = root / "ops" / "truth" / "mainline.json"
        ml = json.loads(ml_path.read_text(encoding="utf-8"))
        ml["current_focus"] = ["常驻自追踪已落盘"]
        ml_path.write_text(json.dumps(ml, ensure_ascii=False), encoding="utf-8")

        third = run_truthctl(root, "synthesize", "--if-changed")
        third_text = mainline.read_text(encoding="utf-8")
        ok &= check("真相变化后命令成功", third.returncode == 0,
                    (third.stdout + third.stderr)[-300:])
        ok &= check("真相变化后刷新内容",
                    "常驻自追踪已落盘" in third_text and third_text != first_text)

    kgctl_source = (OPS / "kgctl.py").read_text(encoding="utf-8")
    kgctl_calls = [n for n in ast.walk(ast.parse(kgctl_source))
                   if call_has_args(n, "_truth", ("synthesize", "--if-changed"))]
    ok &= check("kgctl tick 使用 --if-changed", bool(kgctl_calls))

    daemon_source = (OPS / "central_daemon.py").read_text(encoding="utf-8")
    daemon_calls = [n for n in ast.walk(ast.parse(daemon_source))
                    if call_has_string_list_arg(n, "run_kgctl",
                                                ("tick", "--no-converge"))]
    ok &= check("守护 tick pass 调用 kgctl tick", bool(daemon_calls))

    print("\n结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
