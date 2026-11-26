#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：`kgctl` 转发子命令时，`truthctl` 的输出必须原样透给用户。

为什么有这条（2026-11-17 实测）：
  「万法归一」入口的唯一价值就是「用户只看一个地方」。`kgctl` 把 route / snapshot /
  synthesize / next 都转发给 `truthctl`，但转发时用 `proc_run` 捕获了子进程输出后
  **整段丢弃**，只把退出码带回来。后果实测是：

    $ kgctl route   -> 0 字节
    $ truthctl route-> 1460 字节（同一个 inbox、同一个 KG_REPO）
    $ kgctl next    -> 0 字节
    $ truthctl next -> 127 字节

  也就是说守护每 15 分钟返回码 0、日志里 `[route(pre-exec)] rc=0`，而用户从总入口
  什么都看不到——自动挡静默空转，正是这个项目最难发现的那类假绿灯。

这条门禁锁住三件事：
  1) stdout 里的内容真的出现在 `kgctl` 的 stdout（不再被吞）；
  2) stderr 同样透传（否则「判不清就停」这类提示会消失）；
  3) 子进程的退出码原样返回（透传输出不能顺手把失败改成成功）。

退出码 = 未通过项数。
"""

import contextlib
import importlib.util
import io
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
fails = 0


def check(label, ok, detail=""):
    global fails
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))
        fails += 1


def load_kgctl(project: Path):
    old = os.environ.get("KG_REPO")
    os.environ["KG_REPO"] = str(project)
    try:
        spec = importlib.util.spec_from_file_location(
            "kgctl_truth_output", ROOT / "ops" / "kgctl.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            os.environ.pop("KG_REPO", None)
        else:
            os.environ["KG_REPO"] = old


with tempfile.TemporaryDirectory(prefix="kgctl-truth-output-") as tmp:
    project = Path(tmp) / "project"
    (project / "ops" / "tasks").mkdir(parents=True)
    (project / "ops" / "truth").mkdir()
    # 假 truthctl：同时用可有可无的 stdout / stderr 和退出码，检验三样都原样透传。
    (project / "ops" / "truthctl.py").write_text(
        "import sys\n"
        "print('STDOUT-MARKER-路由计划')\n"
        "print('STDERR-MARKER-判不清就停', file=sys.stderr)\n"
        "sys.exit(7)\n",
        encoding="utf-8")

    kg = load_kgctl(project)
    check("_truthctl_path 认到项目自带的 truthctl",
          kg._truthctl_path().resolve() == (project / "ops" / "truthctl.py").resolve())

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = kg._truth("route", "--go")
    captured_out, captured_err = out.getvalue(), err.getvalue()

    check("子进程 stdout 透传到 kgctl 的 stdout",
          "STDOUT-MARKER" in captured_out, repr(captured_out[:120]))
    check("子进程 stderr 透传到 kgctl 的 stderr",
          "STDERR-MARKER" in captured_err, repr(captured_err[:120]))
    check("退出码原样返回（透传不把失败改成成功）", rc == 7, repr(rc))

    # 反向：源码里不许再出现「调用 proc_run 后只取 returncode」的转发形态。
    source = (ROOT / "ops" / "kgctl.py").read_text(encoding="utf-8")
    start = source.index("def _truth(sub, *extra):")
    end = source.index("def _relay_route_to_project", start)
    body = source[start:end]
    check("转发体里确实写了 stdout", "sys.stdout.write" in body)
    check("转发体里确实写了 stderr", "sys.stderr.write" in body)

print(f"\nkgctl truth output: {'PASS' if fails == 0 else str(fails) + ' 项未通过'}")
raise SystemExit(fails)
