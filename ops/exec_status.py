#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exec_status —— 探测「自动实现（auto_exec）」此刻是不是真的在跑（stdlib only）。

为什么需要它
------------
auto_exec 派出的 `codex exec` 是同步执行的，最长可跑 3600s。这段时间里常驻进程
卡在 subprocess.run 里，`state.json` 完全不动——面板于是仍显示上一轮的
「没有能安全自动合入的分支」，让用户以为它没干活。这个模块从 daemon.log 的
`[exec] 派发 ... / [exec] ... 退出码=` 配对里读出「此刻正在实现哪条需求」，
再叠加进程表与日志新鲜度做交叉校验，供面板和驾驶舱共用。

契约
----
  * 只读，不写任何文件；任何探测失败都返回 None（宁可显示「空闲」，也不臆造在跑）。
  * 判定「在跑」需要两个信号同时成立：daemon.log 里最后一次派发还没有对应退出码，
    且该 exec 的工作区/日志仍新鲜（有活进程，或日志在近期被写过）。
"""

import json
import subprocess
import sys
import time
from pathlib import Path

try:
    import control_state
except Exception:  # 允许被当作脚本单独调试
    control_state = None

# 派发行的形状（central_daemon.log 写入）：
#   [exec] 派发 L-0012 -> codex/exec-l-0012-...（worktree=/…/kg-trunk-exec-…, codex=/…/codex）
# 退出行的形状：
#   [exec] L-0012 codex 退出码=0（日志 /…/exec/l-0012-….log）
_DISPATCH = "[exec] 派发 "
_EXIT_MARK = "codex 退出码="
# 日志超过这个时长没有被写过、又找不到活进程，就当作残影（例如守护被强杀）。
STALE_SECONDS = 900


def _home(home=None) -> Path:
    if home is not None:
        return Path(home).expanduser()
    if control_state is not None:
        return control_state.home()
    return Path.home() / ".codex-autopilot"


def _tail_lines(path: Path, limit: int = 4000):
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    return lines[-limit:] if len(lines) > limit else lines


def _codex_exec_alive(worktree: str):
    """工作区里是否有活的 `codex exec` 进程。三个值：True / False / None(说不准)。"""
    if not worktree:
        return None
    try:
        r = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                           text=True, errors="replace", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        if "codex" in line and " exec " in line and worktree in line:
            return True
    return False


def _split_dispatch(line: str):
    """从派发行里取出 (rid, branch, worktree)。取不到返回 None。"""
    body = line.split(_DISPATCH, 1)[1]
    if " -> " not in body:
        return None
    rid, rest = body.split(" -> ", 1)
    for sep in ("（", "("):
        if sep in rest:
            branch, meta = rest.split(sep, 1)
            break
    else:
        return None
    worktree = ""
    for tok in meta.replace(",", " ").split():
        if tok.startswith("worktree="):
            worktree = tok[len("worktree="):]
            break
    return rid.strip(), branch.strip(), worktree.strip()


def _ts_of(line: str):
    """日志行前缀是 `YYYY-MM-DD HH:MM:SS`，解析成 epoch 秒；取不到返回 None。"""
    head = line[:19]
    try:
        return time.mktime(time.strptime(head, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def running(home=None):
    """此刻正在跑的 auto_exec 任务；没有则返回 None。"""
    base = _home(home)
    lines = _tail_lines(base / "daemon.log.1") + _tail_lines(base / "daemon.log")
    disp_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if _DISPATCH in lines[i]:
            disp_idx = i
            break
    if disp_idx is None:
        return None
    for i in range(len(lines) - 1, disp_idx, -1):
        if _EXIT_MARK in lines[i]:
            return None  # 最后一次派发已经退出
    parsed = _split_dispatch(lines[disp_idx])
    if not parsed:
        return None
    rid, branch, worktree = parsed

    started_ts = _ts_of(lines[disp_idx])
    now = time.time()
    # 找日志文件：优先按工作区名反推 slug（<repo>-exec-<slug> -> <slug>.log）
    log_path = None
    if worktree:
        name = Path(worktree).name
        if "-exec-" in name:
            slug = name.split("-exec-", 1)[1]
            cand = base / "exec" / f"{slug}.log"
            if cand.exists():
                log_path = cand
    if log_path is None:
        try:
            logs = sorted((base / "exec").glob("*.log"), key=lambda p: p.stat().st_mtime)
        except OSError:
            logs = []
        if logs:
            candidate = logs[-1]
            if started_ts is None or candidate.stat().st_mtime >= started_ts - 10:
                log_path = candidate

    log_bytes = None
    log_mtime = None
    if log_path is not None:
        try:
            stt = log_path.stat()
            log_bytes = stt.st_size
            log_mtime = stt.st_mtime
        except OSError:
            log_path = None

    alive = _codex_exec_alive(worktree)
    fresh_ref = log_mtime if log_mtime is not None else started_ts
    fresh = (fresh_ref is not None) and (now - fresh_ref < STALE_SECONDS)
    # 残影判定：进程确已不在，且日志也停了 -> 当没在跑（守护可能被强杀过）。
    if alive is False and not fresh:
        return None

    elapsed = int(now - started_ts) if started_ts else None
    return {
        "id": rid,
        "branch": branch,
        "worktree": worktree,
        "started_at": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_ts))
                       if started_ts else None),
        "elapsed_sec": elapsed,
        "alive": alive,
        "log_path": str(log_path) if log_path else None,
        "log_bytes": log_bytes,
    }


def main():
    out = running()
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
