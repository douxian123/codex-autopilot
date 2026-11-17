#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收截图附件：只在本机、用户点击后交互式框选，绝不自动截屏。

边界：
  * 只有面板上的人工验收按钮能触发；探针/守护/后台循环不得调用；
  * 截图写入用户级目录，不写进项目工作区；
  * 返回绝对路径，由 MCP 层追加到对应 A-id 的 screenshots；
  * 用户取消、非 macOS、工具缺失或空文件都如实报错，不写假路径。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCREENSHOT_BIN = Path("/usr/sbin/screencapture")
SAFE_ID = re.compile(r"^A-\d+$")


def screenshot_dir(home=None):
    base = Path(home or os.environ.get("KG_DAEMON_HOME")
                or (Path.home() / ".codex-autopilot")).expanduser()
    return base / "acceptance-screenshots"


def _default_capture(target, timeout):
    if sys.platform != "darwin":
        return "只有 macOS 支持面板触发的交互式截图。", None
    if not SCREENSHOT_BIN.is_file():
        return "找不到系统截图工具 /usr/sbin/screencapture。", None
    try:
        proc = subprocess.run([str(SCREENSHOT_BIN), "-i", "-x", str(target)],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "截图超时；没有写入验收附件。", None
    except OSError as exc:
        return "截图工具启动失败：%s" % exc, None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return ("用户取消或截图失败；没有写入验收附件。%s" % detail).strip(), None
    if not target.is_file() or target.stat().st_size == 0:
        return "用户取消或没有生成图片；没有写入验收附件。", None
    return None, str(target)


def capture(acceptance_id, home=None, runner=None, timeout=180):
    """交互式截一张图，返回 {"path": 绝对路径} 或 {"error": 人话原因}。

    runner 只为门禁注入；签名 (target: Path, timeout: int) -> (error|None, path|None)。
    """
    aid = str(acceptance_id or "").strip()
    if not SAFE_ID.match(aid):
        return {"error": "验收 id 不合法；只接受 A-数字。"}
    directory = screenshot_dir(home)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"error": "截图目录创建失败：%s" % exc}
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-%06d" % (time.time_ns() % 1_000_000)
    target = directory / ("%s-%s.png" % (aid, stamp))
    if target.exists():
        return {"error": "截图路径已存在；拒绝覆盖。"}
    error, path = (runner or _default_capture)(target, timeout)
    if error or not path:
        return {"error": str(error or "没有生成截图；没有写入验收附件。")}
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = directory / resolved
    try:
        resolved = resolved.resolve()
    except OSError:
        pass
    # runner 是测试注入点，但它不能绕过「真的有图片」这条事实。
    # 系统截图器异常时如果只返回路径、没落文件，也必须报失败，不能写假附件。
    if not resolved.is_file() or resolved.stat().st_size == 0:
        return {"error": "截图工具没有生成图片；没有写入验收附件。"}
    return {"path": str(resolved)}


if __name__ == "__main__":
    import json
    aid = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps(capture(aid), ensure_ascii=False))
