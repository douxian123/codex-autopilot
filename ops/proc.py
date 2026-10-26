#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ops/proc.py —— 受控子进程执行器（stdlib only）。

为什么需要它
------------
2026-10-27 实测：自动挡的 `tick` 连续挂了 30+ 分钟，`ps` 显示 0.0% CPU，
`sample` 栈帧停在 `subprocess.run(capture_output=True)` 内部的
`select_poll_poll` → `read`。原因不是死锁代码，而是**两个结构性缺陷叠加**：

1. **管道被孙进程继承。** `capture_output=True` 用 pipe 收输出；`git` 在
   partial clone（`remote.origin.promisor=true` / `filter=blob:none`）下遇到
   本地没落的 blob，会派一个**网络孙进程**去懒加载。git 本体退出了，孙进程
   还攥着 pipe 的写端，于是 `communicate()` 永远等不到 EOF —— 不是忙等，是
   零 CPU 的静默挂起。调用方没有 timeout，就一直挂着。

2. **git 调用全部没有 timeout。** 全仓 80+ 处 `subprocess.run(["git", ...])`
   都没传 timeout。任何一次慢（懒加载联网、仓库被别的任务锁住、磁盘抖动）
   都会无限期吃满一个心跳周期，而守护是串行的 —— 一拍卡死，整条舰队停摆。

修法
----
* **不用 pipe。** stdout/stderr 各自落临时文件，子进程退出后 `wait()` 立即返回，
  孙进程攥着文件描述符也无所谓——文件不是同步原语，不存在 EOF 等待。
* **硬超时 + 进程组回收。** 超时 `killpg(SIGKILL)`，连孙进程一起收，绝不留
  「活几小时的僵尸服务」。
* **禁懒加载。** 默认塞 `GIT_NO_LAZY_FETCH=1`：partial clone 里缺 blob 时
  宁可失败返回，也不许派网络子进程（这是挂起的源头）。
* **按命令分级超时。** 本地只读 60s；`fetch/push/clone/ls-remote` 这类网络操作
  300s；构建/门禁这类长任务由调用方显式指定。

用法与 `subprocess.run` 对齐，便于原地替换：
    from proc import run
    r = run(["git", "-C", repo, "status", "--porcelain"], text=True)
    r.returncode / r.stdout / r.stderr
"""

import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# 本地 git 只读操作的硬上限。
DEFAULT_TIMEOUT = _f("KG_PROC_TIMEOUT", 60.0)
# 非 git 命令的默认上限：门禁脚本 / land 递归 / npm build 本来就慢。
DEFAULT_NON_GIT_TIMEOUT = _f("KG_PROC_NON_GIT_TIMEOUT", 1800.0)
# 网络型 git 操作（fetch/push/clone/ls-remote/remote update）的硬上限。
NET_TIMEOUT = _f("KG_PROC_NET_TIMEOUT", 300.0)
# 兜底总上限：任何没被识别出来的命令也不会超过它。
MAX_TIMEOUT = _f("KG_PROC_MAX_TIMEOUT", 1800.0)

NET_VERBS = {"fetch", "push", "clone", "ls-remote", "pull", "submodule"}
# 这些动词在 `git -C x <verb>` 里可能出现在第 1 位或更后（如 `remote update`）。
NET_VERBS_ANY = NET_VERBS | {"update"}


@dataclass
class Result:
    returncode: int
    stdout: str | bytes
    stderr: str | bytes

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _is_network_cmd(cmd) -> bool:
    if isinstance(cmd, str):
        return any(f" {v}" in cmd or cmd.startswith(v) for v in NET_VERBS_ANY)
    if not cmd:
        return False
    base = Path(str(cmd[0])).name
    if base not in ("git", "git.exe"):
        return False
    # 跳过 `-C <path>` / `--git-dir=…` 之类的选项，找真正的动词。
    skip_next = False
    for tok in cmd[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            skip_next = True
            continue
        if str(tok).startswith("-"):
            continue
        return str(tok) in NET_VERBS_ANY
    return False


def _is_git_cmd(cmd) -> bool:
    """是不是一条 git 命令（不区分动词）。"""
    if isinstance(cmd, str):
        parts = cmd.split()
        return bool(parts) and Path(parts[0]).name in ("git", "git.exe")
    return bool(cmd) and Path(str(cmd[0])).name in ("git", "git.exe")


def _git_env(env) -> dict:
    """git 专用环境：禁懒加载、禁交互提示、禁 SSH 阻塞。

    这是本次挂起的直接成因：partial clone 下 `git show`/`rev-parse` 命中缺失
    blob 时会派 `git fetch` 孙进程联网。`GIT_NO_LAZY_FETCH=1` 让它立刻失败返回。
    """
    out = dict(os.environ if env is None else env)
    out.setdefault("GIT_TERMINAL_PROMPT", "0")
    out.setdefault("GIT_ASKPASS", "/usr/bin/true")
    out.setdefault("SSH_ASKPASS", "/usr/bin/true")
    out.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=5")
    out.setdefault("GIT_NO_LAZY_FETCH", "1")
    # 别让 gc 在后台抢锁：autopilot 只做只读探查。
    out.setdefault("GIT_OPTIONAL_LOCKS", "0")
    return out


def run(cmd, *, timeout=None, capture_output=True, text=False, errors="replace",
        check=False, cwd=None, env=None, shell=False, input=None,
        stdin=None, stdout=None, stderr=None, **_ignored):
    """`subprocess.run` 的受限替身：无 pipe、有硬超时、超时收整个进程组。

    与 `subprocess.run` 的差异（都是有意的）：
      * **永远不返回 `None` 的 stdout/stderr**：未捕获时给空串/空字节。
      * `capture_output=False` 时丢弃输出而不是继承终端 —— 守护进程里转发到
        stdout 会和日志格式打架。
      * 超时不会抛 `TimeoutExpired`，而是返回 `returncode=124` 并把已产出的
        输出带回来，调用方据此判断「卡在哪一步」而不是只看到一句「超时」。
    """
    explicit_timeout = timeout is not None
    if timeout is None:
        if _is_network_cmd(cmd):
            timeout = NET_TIMEOUT
        elif _is_git_cmd(cmd):
            timeout = DEFAULT_TIMEOUT
        else:
            # 非 git 的命令（门禁脚本、land 递归、npm build、playwright）本来就
            # 可能跑几分钟。给足预算，别为了防 git 挂起反而把正常长任务掐死。
            timeout = DEFAULT_NON_GIT_TIMEOUT
    timeout = min(float(timeout), MAX_TIMEOUT)

    use_env = env if env is not None else os.environ
    is_git = _is_git_cmd(cmd)
    if is_git:
        use_env = _git_env(env)

    # 关键：输出落文件而不是 pipe。孙进程攥着 fd 也换不来 EOF 等待。
    out_f = tempfile.TemporaryFile()
    err_f = tempfile.TemporaryFile()
    # 兼容 subprocess.run 的 stdout=/stderr=/stdin= 写法（launchctl 那类调用在用）。
    # 传了 DEVNULL 就真的丢弃；传了 PIPE 或未传则照旧收进临时文件。
    if stdout is subprocess.DEVNULL or stderr is subprocess.DEVNULL:
        if stdout is subprocess.DEVNULL:
            capture_out_eff = False
        else:
            capture_out_eff = capture_output
        if stderr is subprocess.DEVNULL:
            capture_err_eff = False
        else:
            capture_err_eff = capture_output
    else:
        capture_out_eff = capture_err_eff = capture_output
    if stdin is subprocess.PIPE:
        stdin_arg = subprocess.PIPE
    elif input is not None:
        stdin_arg = subprocess.PIPE
    else:
        stdin_arg = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            cmd, stdin=stdin_arg,
            stdout=out_f if capture_out_eff else subprocess.DEVNULL,
            stderr=err_f if capture_err_eff else subprocess.DEVNULL,
            cwd=cwd, env=use_env, shell=shell,
            start_new_session=True,           # 自成进程组，超时能整组收回
        )
    except OSError as exc:
        # 与 subprocess.run 一致：起不来就抛 OSError，让调用方既有的
        # `except (OSError, subprocess.TimeoutExpired)` 分支继续生效。
        out_f.close()
        err_f.close()
        raise

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    out_f.seek(0)
    err_f.seek(0)
    raw_out = out_f.read() if capture_out_eff else b""
    raw_err = err_f.read() if capture_err_eff else b""
    out_f.close()
    err_f.close()

    if text:
        raw_out = (raw_out or b"").decode("utf-8", errors or "replace")
        raw_err = (raw_err or b"").decode("utf-8", errors or "replace")

    if timed_out:
        note = f"\n[proc] 命令超时（>{timeout:.0f}s），已收掉整个进程组：{cmd!r}"
        if text:
            raw_err = (raw_err or "") + note
        else:
            raw_err = (raw_err or b"") + note.encode("utf-8")
        # 两种语义，按调用方是否显式给了 timeout 区分：
        #  * 显式给了 timeout -> 抛 TimeoutExpired，和 subprocess.run 完全一致，
        #    既有 `except subprocess.TimeoutExpired` 分支不需要改一行。
        #  * 没给（走默认闸）-> 返回 rc=124 并把输出带回来。这类调用点几乎都是
        #    `if p.returncode != 0: return None/[]`，超时降级成「读不到」比让整轮
        #    tick 抛异常中断（真相层就不推送了）安全得多。
        if explicit_timeout:
            raise subprocess.TimeoutExpired(
                cmd, timeout, output=raw_out, stderr=raw_err)
        return Result(124, raw_out, raw_err)

    code = proc.returncode
    if check and code != 0:
        # 与 subprocess.run(check=True) 保持一致，免得调用方的 except 分支失效。
        raise subprocess.CalledProcessError(code, cmd, raw_out, raw_err)
    return Result(code, raw_out, raw_err)


def out(cmd, **kw) -> str:
    """便利封装：跑一条命令，返回 strip 过的 stdout。失败返回空串。"""
    r = run(cmd, text=True, **kw)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def which_git() -> str:
    return shutil.which("git") or "git"
