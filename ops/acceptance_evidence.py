#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把真实门禁结果绑定到逐项预验收字典。

这个模块不推断“某个大测试等于整个产品通过”。调用者必须显式提供标准编号
映射；命令成功只会使映射到该命令的条目获得证据，命令失败则追加失败证据。
所有记录都包含当前 commit、cwd、命令和退出码，便于面板与后续复核。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import acceptance_dictionary
import pre_acceptance


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _commit(root: Path) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=False)
        return result.stdout.strip() or ""
    except OSError:
        return ""


def _command_text(command: Iterable[str]) -> str:
    return " ".join(str(part) for part in command)


def record_result(root: Path, item_id: str, *, status: str, command: list[str],
                  returncode: int, stdout: str = "", stderr: str = "",
                  evidence_types: list[str] | None = None,
                  artifacts: list[str] | None = None,
                  reason: str = "") -> dict[str, Any]:
    """追加一条有 provenance 的标准结果。"""
    spec = acceptance_dictionary.BY_ID.get(item_id)
    if not spec:
        raise ValueError(f"unknown acceptance item: {item_id}")
    cmd = _command_text(command)
    result = "通过" if status == "pass" and returncode == 0 else "失败"
    # 只能记录调用者实际取得的证据。不能拿字典里的“要求”反填成“已取得”，
    # 否则一次命令成功会伪装成截图、浏览器和用户走查都完成。
    evidence = list(dict.fromkeys(evidence_types or ["command", "result", "version"]))
    row = {
        "check_id": spec["category"],
        "item_id": item_id,
        "status": "pass" if status == "pass" and returncode == 0 else "fail",
        "evidence_types": evidence,
        "version": _commit(root),
        "commands": [cmd],
        "artifacts": artifacts or [],
        "summary": f"{result}：{cmd}（exit {returncode}）",
        "reason": reason or (stderr.strip()[-500:] if returncode else ""),
        "cwd": str(root.resolve()),
        "exit_code": returncode,
        "stdout_tail": stdout.strip()[-500:],
        "stderr_tail": stderr.strip()[-500:],
        "updated_at": _now(),
    }
    path = root / "ops" / "truth" / "pre_acceptance.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def run_check(root: Path, item_ids: list[str], command: list[str], *,
              timeout: int = 300, evidence_types: list[str] | None = None,
              artifacts: list[str] | None = None) -> dict[str, Any]:
    """执行一次真实门禁，并把相同命令结果绑定到显式列出的标准。"""
    try:
        completed = subprocess.run(command, cwd=str(root), capture_output=True,
                                   text=True, check=False, timeout=timeout)
        returncode = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + "\ncommand timed out"
    status = "pass" if returncode == 0 else "fail"
    rows = [record_result(root, item_id, status=status, command=command,
                          returncode=returncode, stdout=stdout, stderr=stderr,
                          evidence_types=evidence_types,
                          artifacts=artifacts)
            for item_id in item_ids]
    return {"command": command, "returncode": returncode, "status": status,
            "items": rows, "stdout": stdout, "stderr": stderr}


SUITES = {
    # 这些命令只证明命令、结果和版本，不宣称视觉、浏览器或用户走查通过。
    "dictionary": (["ENG-003"],
                    [sys.executable, "ops/test-acceptance-dictionary.py"]),
    "panel_logic": (["ENG-003"],
                     [sys.executable, "ops/panel_logic_check.py", "--quiet"]),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记录真实门禁的逐项验收证据")
    parser.add_argument("--repo", default=os.environ.get("KG_REPO", "."))
    parser.add_argument("--suite", choices=sorted(SUITES))
    parser.add_argument("--item-id")
    parser.add_argument("--command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    root = Path(args.repo).expanduser().resolve()
    if args.suite:
        item_ids, command = SUITES[args.suite]
    elif args.item_id and args.command:
        item_ids, command = [args.item_id], args.command
    else:
        parser.error("需要 --suite，或同时提供 --item-id 和 --command")
    result = run_check(root, item_ids, command)
    print(json.dumps({"status": result["status"], "returncode": result["returncode"],
                      "items": [x["item_id"] for x in result["items"]]},
                     ensure_ascii=False))
    return result["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
