#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁：验收截图必须由人触发、只追加附件、失败时不写假路径。"""

import os
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import acceptance_screenshot  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def good_runner(target, timeout):
    target.write_bytes(b"fake-png")
    return None, str(target)


def cancel_runner(target, timeout):
    return "用户取消或截图失败；没有写入验收附件。", None


def no_file_runner(target, timeout):
    return None, str(target)


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-shot-") as tmp:
        home = Path(tmp)
        ok = acceptance_screenshot.capture("A-0030", home=home, runner=good_runner)
        check("用户触发后生成截图路径", "path" in ok and Path(ok["path"]).is_file(), ok)
        check("截图落在用户级目录，不进项目工作区",
              Path(home.resolve()) in Path(ok.get("path", "")).resolve().parents, ok)
        check("截图文件名带验收 id", "A-0030" in Path(ok.get("path", "")).name, ok)

        canceled = acceptance_screenshot.capture("A-0031", home=home, runner=cancel_runner)
        check("用户取消时不写假路径", "error" in canceled and "取消" in canceled["error"], canceled)
        check("用户取消时不创建图片",
              not list(acceptance_screenshot.screenshot_dir(home).glob("A-0031-*.png")), canceled)

        missing = acceptance_screenshot.capture("A-0032", home=home, runner=no_file_runner)
        check("截图工具没产出文件时如实报错", "error" in missing, missing)

        bad = acceptance_screenshot.capture("../../etc/passwd", home=home, runner=good_runner)
        check("非法验收 id 不能直接进路径", "error" in bad, bad)

    print()
    if failures:
        print(f"acceptance_screenshot 门禁：{len(failures)} 项未通过")
        for f in failures:
            print("  - " + f)
        return len(failures)
    print("acceptance_screenshot 门禁：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
