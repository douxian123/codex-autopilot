#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""central_daemon —— 自动挡的常驻执行器（安全子集 v1，stdlib only）。

为什么需要它
------------
`kgctl tick` / `kgctl land` 已经把「路由散需求 + 串行落盘」变成命令，但它们只在
有人敲、有会话开着的时候才跑。关掉 Codex，能干净合并的分支不会自己往前走，散落的
需求也不会自己归位 —— 每一步都还得人肉 check。常驻版解决的就是这一件事：没有会话
时也按固定节奏把**安全的工作**推进掉，只在真正需要人拍板时才打扰用户。

安全子集（硬编码，不可关）
  * 只 land `kgctl queue` 列出的 CLEAN 分支（与 main 无冲突，且树里没有残留
    conflict markers）。真正的语义冲突**不自动合**：冲突不丢给你逐条裁决，而是由
    `tick` 里的 `kgctl converge` 收拢成少数几个 convergence 任务（保留 markers），
    交给一个 agent 逐族解干净。
  * 归档与移植（`auto_port`，默认关闭）：`supersede` 只把「独有内容已全部在
    main」的分支标记为重复（只写台账、不动代码、不动源分支）；`port` 只把「只差
    1..N 个提交」的冲突分支 cherry-pick 到当前 main，任何文本冲突即整体 abort，
    成功也仍走同一道 build 门禁。两者都不猜语义、都不删数据；移植失败的分支记进
    `port_blocked`，之后不再重复尝试。要在项目里打开，写
    `ops/central.config.json` 的 `{"auto_port": {"enabled": true, "max_unlanded": 2}}`。
  * 冲突拆分（`auto_resolve`，默认开启）：每周期挑一条「冲突文件少、未落改动小」的
    冲突分支，在独立 worktree 里合并；append-only 的 `.jsonl` 冲突就地做行级并集，
    其余保留 marker 写成任务书（`ops/tasks/resolve/<slug>.md`），交给 agent 解开后走
    同一道 land 门禁。不改 main、不改源分支；拆不动 / 门禁没过的分支记进
    `resolve_blocked`，之后不再重复拆。要关就写
    `{"auto_resolve": {"enabled": false}}`，或用 `max_files` / `max_lines` 收紧闸门。
  * 每个分支仍走 `kgctl land` 的全部护栏：真实 build 门禁、origin/main 对齐、
    失败即丢弃、从不 force push。
  * 单实例锁 + 复用 kgctl 自己的 land 锁：不会和手动 land 抢。
  * 一次只 land 一个，落一个重算一次队列；每轮有上限，不让守护进程长时间持锁。
  * 通知去重：状态没变化就静默。

常驻 vs 非常驻
--------------
非常驻（原来的形态）：有会话时由 agent 触发 tick / land，关掉会话就停。
常驻（本进程）：launchd 守护，`--interval` 分钟一个周期，无人值守。

边界
----
本进程不做冲突的最终裁决（只把冲突收拢成族、不替谁决定对错）、不删数据、不改产品
方向、不发布。这些仍然要停下来问人。
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import plistlib
import re
import selectors
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE_ROOT = HERE.parent
KGCTL = HERE / "kgctl.py"


from plugin_binding import apply_plugin_env  # noqa: E402

import central_config  # noqa: E402
import control_state  # noqa: E402

# KG_REPO（账本）/ KG_TRUNK（落盘主干）优先取显式环境变量，其次读插件 .mcp.json。
apply_plugin_env()

RUNTIME = Path(os.environ.get("KG_DAEMON_HOME",
                              str(Path.home() / ".codex-autopilot"))).expanduser()
LOG = RUNTIME / "daemon.log"
STATE = RUNTIME / "state.json"
DAEMON_LOCK = RUNTIME / "daemon.lock"
NOTIFICATIONS = RUNTIME / "notifications.log"
MAX_LOG_BYTES = 5 * 1024 * 1024

PLIST_LABEL = os.environ.get("KG_DAEMON_LABEL", "com.codex-autopilot.daemon")
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"

DEFAULT_INTERVAL_MIN = 10
DEFAULT_MAX_LAND = 3
LAND_TIMEOUT = 1800
TICK_TIMEOUT = 1800
SUPERSEDE_TIMEOUT = 900
PORT_TIMEOUT = 1800
RESOLVE_TIMEOUT = 1800
# 一个周期里最多尝试移植几条候选。每条候选要么落盘（走完整 build 门禁），要么撞
# 冲突被记进 port_blocked 后跳过；两种都会让下一轮候选集变小，所以循环会自然收敛。
PORT_MAX_ATTEMPTS = 6
# 一个周期最多拆几条冲突分支。每条先做确定性并集（只 jsonl）或留下一个可执行
# 任务书；拆不动 / 门禁没过记进 resolve_blocked，后续周期跳过，不再反复骚扰。
RESOLVE_MAX_ATTEMPTS = 1
# 自动实现（auto_exec）：把台账里还没做的待办派给一个真实 codex 会话，在独立 worktree
# 里实现 -> 门禁 -> 落盘 -> 收口。默认关闭；打开后每周期最多派一条，避免一次开一堆会话。
EXEC_DEFAULT_TIMEOUT = 3600
EXEC_MAX_PER_CYCLE = 3
EXEC_MAX_WORKERS = 3
EXEC_CYCLE_BUDGET = 5400
CONVERGE_MAX_FILES = 2
CONVERGE_MAX_MARKERS = 24
# 基础设施抖动 vs 需求真做不出来：底层模型代理（127.0.0.1:15721）偶发 502 /
# Transport channel closed，会让 codex 进程以非 0 退出，但需求本身没问题。同一
# worktree 上指数退避重跑即可，默认 3 次；重试仍失败记 codex_infra_fail，与
# codex_exit_N 一眼区分开（L-0055 就是这么被误记成「做不出来」的）。
EXEC_INFRA_RETRIES = 3
EXEC_INFRA_BACKOFF = 20.0
# 只认这几种环境抖动特征；只看日志尾部若干字节，不把大日志整份读进来。
EXEC_INFRA_PATTERN = re.compile(
    r"502 Bad Gateway|Transport channel closed|ECONNRESET|connection refused",
    re.IGNORECASE)
EXEC_INFRA_TAIL_BYTES = 8192

_stop = False
_conv_pool = None
_conv_jobs: dict = {}
_conv_blocked: dict = {}
_resolve_future = None
_resolve_task_pool = None
_resolve_jobs: dict = {}
_external_resolve_jobs: dict = {}
_resolve_finish_jobs: dict = {}
_exec_jobs: dict = {}
_exec_pool = None
_land_gate = threading.Lock()


# ------------------------------------------------------------------ 基础设施

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _rotate_log() -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
            LOG.replace(LOG.with_suffix(".log.1"))
    except OSError:
        pass


def log(msg: str) -> None:
    """写日志。只有交互式终端才回显到 stdout —— launchd 的 StandardOutPath 指向
    同一个文件，若这里再 print 一次，每一行都会被写两遍（实测踩过）。"""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    _rotate_log()
    line = f"{_now()} {msg}"
    if sys.stdout.isatty():
        print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def log_block(title: str, rc: int, out: str) -> None:
    log(f"[{title}] rc={rc}")
    for line in out.splitlines():
        log("    " + line)


def run_kgctl(args, timeout=TICK_TIMEOUT, env_extra=None):
    """调用 kgctl，返回 (rc, 合并后的 stdout+stderr)。

    超时不能只说一句「超时」：旧实现在 TimeoutExpired 分支里**把子进程已经产出的输出
    整段丢掉**（subprocess.run 抛异常时不给 output），于是 2026-10-15 22:49 那次 tick
    挂满 1800s 之后，日志里只剩一行 `[tick] rc=124`，卡在哪一步无从判断。
    现在自己起进程：到点先收下已产出的行，再用 killpg 收掉整个进程组（只 kill 直接
    子进程会留下 git 孙进程，就是历史上那类「僵尸服务存活几小时」的形态）。
    """
    cmd = [sys.executable, str(KGCTL), *args]
    env = {**os.environ, **env_extra} if env_extra else None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=False, cwd=str(CODE_ROOT), env=env,
                                start_new_session=True)
    except OSError as exc:
        return 125, f"调用 kgctl 失败: {exc}"

    # 实时落日志：tick/land 可能跑几分钟，全部缓冲到退出才写会让
    # `daemon.log` 在关键阶段只剩一行 cycle start，现场无法判断卡在哪。
    # 读 stdout 必须可中断：阻塞 readline() 在子进程长时间无输出时会越过
    # deadline，导致 timeout 形同虚设。
    lines: list[str] = []
    deadline = time.time() + timeout
    timed_out = False
    sel = selectors.DefaultSelector()
    assert proc.stdout is not None
    sel.register(proc.stdout, selectors.EVENT_READ)
    pending = b""
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                break
            events = sel.select(min(remaining, 0.25))
            if events:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    continue
                pending += chunk
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace")
                    lines.append(line + "\n")
                    try:
                        with LOG.open("a", encoding="utf-8") as fh:
                            fh.write(f"{_now()}       {line}\n")
                    except OSError:
                        pass
            elif proc.poll() is not None:
                break
        if pending:
            line = pending.decode("utf-8", errors="replace")
            lines.append(line)
            try:
                with LOG.open("a", encoding="utf-8") as fh:
                    fh.write(f"{_now()}       {line}\n")
            except OSError:
                pass
        if not timed_out:
            proc.wait(timeout=max(1.0, deadline - time.time()))
            return proc.returncode, "".join(lines)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        sel.close()

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    tail = "".join(lines).strip()
    msg = f"kgctl {' '.join(args)} 超时（>{timeout}s，进程组已收掉）"
    if tail:
        msg += "\n超时前已产出的输出如下（最后一行即卡住的位置）：\n" + tail
    else:
        msg += "\n（超时前一行输出都没有——卡在第一个阶段之前，见 kgctl tick 的阶段编号）"
    return 124, msg


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def save_heartbeat(**fields) -> None:
    """在长周期里刷新可观测状态，不覆盖整轮结果。

    一个 `land` 可能跑几分钟。旧实现只在 cycle 末尾写 state，所以进程明明在干活，
    面板却一直显示上一轮时间。心跳只更新当前阶段和时间，不增加完成计数。
    """
    state = load_state()
    state["heartbeat_ts"] = time.time()
    state["heartbeat_iso"] = _now()
    state.update({k: v for k, v in fields.items() if v is not None})
    save_state(state)


def save_worker_state(convergence_done: list | None = None,
                      resolve_done: list | None = None,
                      exec_done: list | None = None) -> None:
    """记录后台 worker 的实时槽位，不改变周期的完成语义。

    这些任务运行在 daemon 的子线程里；只看 `current_stage=cycle_start` 无法区分
    “卡在 tick”与“三个 worker 正在各自 worktree 里干活”。状态只追加最近一次
    收割结果，active 列表从内存中的 future 推导，下一拍会自然收敛为空。
    """
    state = load_state()
    conv_cfg = convergence_exec_config()
    exec_cfg = auto_exec_config()
    conv_limit = max(1, int(conv_cfg.get("max_workers", EXEC_MAX_WORKERS)))
    exec_limit = max(1, int(exec_cfg.get("max_workers", EXEC_MAX_WORKERS)))
    exec_active = sorted(k for k, v in _exec_jobs.items()
                         if not v["future"].done())
    resolve_active = bool(_resolve_future is not None and
                          not _resolve_future.done())
    # State must survive a daemon restart. Re-scan real external `codex exec`
    # processes for open resolve worktrees before publishing active slots.
    try:
        _refresh_external_resolve_jobs()
    except NameError:
        pass
    resolve_task_active = sorted(set(_resolve_jobs) | set(_external_resolve_jobs))
    resolve_finish_active = sorted(_resolve_finish_jobs)
    state["finish_resolve_queue"] = resolve_finish_active
    state["workers"] = {
        "convergence_limit": conv_limit,
        "exec_limit": exec_limit,
        "convergence_active": sorted(_conv_jobs),
        "resolve_active": resolve_active,
        "resolve_started": (state.get("workers", {}).get("resolve_started")
                            if resolve_active else None),
        "resolve_task_active": resolve_task_active,
        "resolve_external_active": sorted(_external_resolve_jobs),
        "resolve_finish_active": resolve_finish_active,
        "exec_active": exec_active,
        "convergence_free": max(0, conv_limit - len(_conv_jobs) -
                                 (1 if _resolve_future is not None else 0)),
        "exec_free": max(0, exec_limit - len(exec_active)),
        "updated_ts": time.time(),
        "updated_iso": _now(),
    }
    if convergence_done is not None:
        state["workers"]["last_convergence"] = [
            {"id": r.get("id"), "outcome": r.get("outcome"),
             "commit": r.get("commit")} for r in convergence_done
        ]
    if resolve_done is not None:
        state["workers"]["last_resolve"] = resolve_done
    if exec_done is not None:
        state["workers"]["last_exec"] = [
            {"id": r.get("id"), "outcome": r.get("outcome"),
             "commit": r.get("commit")} for r in exec_done
        ]
    save_state(state)


def _live_exec_worktrees(kind: str | None = None) -> dict:
    """Return worktree path -> pid for live codex exec processes.

    ``kind='exec'`` only counts implementation workers. Resolve/convergence
    workers live in their own bounded lanes and must not consume those slots.
    """
    try:
        p = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    repo = os.environ.get("KG_REPO")
    token_repo = Path(repo).expanduser().resolve() if repo else None
    for line in (p.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or "codex" not in parts[1] or "exec" not in parts[1]:
            continue
        # Test fixtures run the fake executor with the same `exec -C <wt>` CLI.
        # Do not let a live production exec process occupy fixture slots.
        if token_repo and str(token_repo) not in parts[1]:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        try:
            tokens = shlex.split(parts[1])
        except ValueError:
            tokens = parts[1].split()
        for i, token in enumerate(tokens[:-1]):
            if token in ("-C", "--cd"):
                candidate = Path(tokens[i + 1]).expanduser().resolve()
                if kind == "exec" and "-exec-" not in candidate.name:
                    break
                out[str(candidate)] = pid
                break
    return out


def _state_repo():
    """守护要读哪个项目的 ops/central.config.json：显式 KG_REPO 优先，其次插件 .mcp.json。"""
    repo = os.environ.get("KG_REPO")
    if repo:
        return Path(repo).expanduser()
    try:
        data = json.loads((HERE.parent / ".mcp.json").read_text(encoding="utf-8"))
        env = (data.get("mcpServers") or {}).get("codex-autopilot", {}).get("env") or {}
        if env.get("KG_REPO"):
            return Path(env["KG_REPO"]).expanduser()
    except (OSError, ValueError):
        pass
    return None


def auto_port_config() -> dict:
    """状态根 ops/central.config.json 的 auto_port 段。

    默认**关闭**：把「只差几个提交」的冲突分支自动 cherry-pick 到 main 属于契约扩展，
    必须由项目在配置里显式打开。打开后一个周期把候选逐条试一遍（上限见
    PORT_MAX_ATTEMPTS），任何文本冲突即整体放弃并记进 port_blocked。
    """
    out = {"enabled": False, "max_unlanded": 2}
    repo = _state_repo()
    if not repo:
        out["enabled"] = bool(control_state.load().get("auto_port", False))
        return out
    ctrl_port = control_state.load().get("auto_port", False)
    try:
        data = json.loads((repo / "ops" / "central.config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        out["enabled"] = bool(ctrl_port)
        return out
    cfg = data.get("auto_port")
    if isinstance(cfg, bool):
        out["enabled"] = cfg
    elif isinstance(cfg, dict):
        out["enabled"] = bool(cfg.get("enabled", False))
        try:
            out["max_unlanded"] = max(1, int(cfg.get("max_unlanded", 2)))
        except (TypeError, ValueError):
            pass
    # 用户级开关拥有最终否决权：面板上关掉 auto_port，项目配置打开也不生效。
    if not control_state.load().get("auto_port", False):
        out["enabled"] = False
    return out


def auto_resolve_config() -> dict:
    """状态根 ops/central.config.json 的 auto_resolve 段。

    默认**开启**：冲突分支不再推给用户逐条裁决，而是每周期挑一条「文件少、未落
    改动小」的，拆成确定性并集或一个可执行任务书。它只在独立 worktree 里动，
    不改 main、不改源分支，落盘仍走完整门禁。项目可用
    `{"auto_resolve": {"enabled": false}}` 关掉，或调 max_files / max_lines 收紧闸门。
    """
    out = {"enabled": True, "max_files": 8, "max_lines": 1200,
           "max_workers": 3, "resume_dirty": True, "auto_split": True,
           "sandbox": "danger-full-access", "fast_model": None,
           "timeout": EXEC_DEFAULT_TIMEOUT}
    repo = _state_repo()
    if not repo:
        out["enabled"] = bool(control_state.load().get("auto_resolve", True))
        return out
    try:
        data = json.loads((repo / "ops" / "central.config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        out["enabled"] = bool(control_state.load().get("auto_resolve", True))
        return out
    cfg = data.get("auto_resolve")
    exec_cfg = data.get("auto_exec")
    if isinstance(exec_cfg, dict):
        # Keep one source of truth for the fast provider and execution budget
        # unless this worker class explicitly overrides them.
        if out.get("fast_model") is None:
            out["fast_model"] = exec_cfg.get("fast_model")
        try:
            out["timeout"] = max(1, int(exec_cfg.get("timeout", out["timeout"])))
        except (TypeError, ValueError):
            pass
    if isinstance(cfg, bool):
        out["enabled"] = cfg
    elif isinstance(cfg, dict):
        out["enabled"] = bool(cfg.get("enabled", True))
        for key in ("max_files", "max_lines", "max_workers"):
            try:
                out[key] = max(1, int(cfg.get(key, out[key])))
            except (TypeError, ValueError):
                pass
        out["resume_dirty"] = bool(cfg.get("resume_dirty", out["resume_dirty"]))
        out["auto_split"] = bool(cfg.get("auto_split", out["auto_split"]))
        if cfg.get("sandbox"):
            out["sandbox"] = str(cfg["sandbox"])
        if cfg.get("fast_model"):
            out["fast_model"] = str(cfg["fast_model"])
        try:
            out["timeout"] = max(1, int(cfg.get("timeout", out["timeout"])))
        except (TypeError, ValueError):
            pass
    # 用户级开关拥有最终否决权：面板上关掉 auto_resolve，项目配置打开也不生效。
    if not control_state.load().get("auto_resolve", True):
        out["enabled"] = False
    return out


# ------------------------------------------------------------------ 通知

def _osa_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') \
                   .replace("\n", " ").replace("\r", " ") + '"'


def notify(title: str, body: str) -> None:
    """一条通知，两个落点：始终写 notifications.log，桌面弹一个（可关）。"""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        with NOTIFICATIONS.open("a", encoding="utf-8") as fh:
            fh.write(f"{_now()} {title} | {body}\n")
    except OSError:
        pass
    log(f"NOTIFY {title} | {body}")
    custom = os.environ.get("KG_DAEMON_NOTIFY_CMD")
    if custom:
        try:
            subprocess.run(shlex.split(custom) + [title, body],
                           capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    if sys.platform == "darwin" and not os.environ.get("KG_DAEMON_SILENT"):
        try:
            subprocess.run(["osascript", "-e",
                            f"display notification {_osa_string(body)} "
                            f"with title {_osa_string(title)}"],
                           capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _sig(payload) -> str:
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False,
                                   sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ 落盘编排

QUEUE_RE = re.compile(r"^\s*\d+\.\s+kgctl land (\S+)")
SUMMARY_RE = re.compile(r"其余 (\d+) 个：(\d+) 个已合并待回收、(\d+) 个有冲突需裁决")
UNRESOLVED_RE = re.compile(r"另有 (\d+) 个分支残留冲突标记")
CONVERGE_PLAN_RE = re.compile(r"可收成 (\d+) 个 convergence 任务")
CONVERGE_MADE_RE = re.compile(r"已生成 (\d+) 个 convergence 分支")
SUPERSEDE_DONE_RE = re.compile(r"已归档 (\d+) 个重复分支")
PORT_RESULT_RE = re.compile(r"PORT_RESULT branch=(\S+) outcome=(\w+)")
RESOLVE_RESULT_RE = re.compile(r"RESOLVE_RESULT branch=(\S+) outcome=(\w+)")


def parse_queue(out: str):
    """从 `kgctl queue` 输出里取可落盘分支列表和三类计数。"""
    branches = []
    for line in out.splitlines():
        m = QUEUE_RE.match(line)
        if m:
            branches.append(m.group(1))
    summary = {"other": 0, "merged": 0, "conflict": 0, "unresolved": 0}
    m = SUMMARY_RE.search(out)
    if m:
        summary = {"other": int(m.group(1)), "merged": int(m.group(2)),
                   "conflict": int(m.group(3)), "unresolved": 0}
    m = UNRESOLVED_RE.search(out)
    if m:
        summary["unresolved"] = int(m.group(1))
    return branches, summary


def parse_convergence(tick_out: str) -> dict:
    """从 `kgctl tick` 输出里取冲突收拢情况：可收拢族数 / 本轮新建族数。"""
    plan = CONVERGE_PLAN_RE.search(tick_out or "")
    made = CONVERGE_MADE_RE.search(tick_out or "")
    return {"families": int(plan.group(1)) if plan else 0,
            "made": int(made.group(1)) if made else 0}


def classify_land(out: str, rc: int) -> str:
    """把 `kgctl land` 的输出归类。cmd_land 多数分支是 print+return（rc=0），
    所以必须按文案判，不能只看 rc。"""
    if "已落盘并推送 origin" in out:
        return "landed"
    if "已完全合并" in out:
        return "already"
    if "已有 land 在进行" in out:
        return "locked"
    if "拒绝自动 land" in out:
        if "残留" in out or "冲突标记" in out:
            return "unresolved_markers"
        return "conflict"
    if ("× 失败 → 丢弃" in out or "build × 失败" in out
            or "构建门禁 × 失败" in out or "验证门禁 × 失败" in out):
        return "gate_fail"
    if "合并失败，已丢弃" in out:
        return "merge_fail"
    if "主干工作树有非真相层改动" in out:
        return "trunk_dirty"
    if "与 origin/已分叉" in out or "已分叉" in out:
        return "diverged"
    if rc != 0:
        return "error"
    return "noop"


# ------------------------------------------------------------------ 自动实现（auto_exec）

def auto_exec_config() -> dict:
    """状态根 ops/central.config.json 的 auto_exec 段。

    默认「代码层开启、用户层关闭」——真正开关是面板 / `control.json` 里的 `auto_exec`，
    默认 False。派真实 codex 会话改代码比自动合分支风险高，所以必须由用户显式打开；
    项目配置可以关掉、收紧沙箱或换模型，但用户开关拥有最终否决权。

    `retries` 是「基础设施抖动」在同一 worktree 上的重跑上限（默认 3 次，指数退避），
    与「需求真做不出来」区分开；只在日志尾部命中抖动特征时才生效。
    """
    out = {"enabled": True, "max_per_cycle": EXEC_MAX_PER_CYCLE,
           "max_workers": EXEC_MAX_WORKERS, "cycle_budget": EXEC_CYCLE_BUDGET,
           "timeout": EXEC_DEFAULT_TIMEOUT, "sandbox": "workspace-write", "model": None,
           "retries": EXEC_INFRA_RETRIES}
    repo = _state_repo()
    if repo:
        try:
            data = json.loads((repo / "ops" / "central.config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        cfg = data.get("auto_exec")
        if isinstance(cfg, bool):
            out["enabled"] = cfg
        elif isinstance(cfg, dict):
            out["enabled"] = bool(cfg.get("enabled", True))
            for key in ("max_per_cycle", "max_workers", "cycle_budget", "timeout"):
                try:
                    out[key] = max(1, int(cfg.get(key, out[key])))
                except (TypeError, ValueError):
                    pass
            # 并发数不能超过每轮派发数；否则配置写错也不会偷偷开更多会话。
            out["max_workers"] = max(1, min(out["max_workers"], out["max_per_cycle"]))
            try:
                out["retries"] = max(0, int(cfg.get("retries", out["retries"])))
            except (TypeError, ValueError):
                pass
            if cfg.get("sandbox"):
                out["sandbox"] = str(cfg["sandbox"])
            if cfg.get("model"):
                out["model"] = str(cfg["model"])
    # 用户级开关拥有最终否决权（默认关）。
    if not control_state.load().get("auto_exec", False):
        out["enabled"] = False
    # 预算熔断（fail-open）：用量越过硬上限时只停「派活」（auto_exec），
    # 不停 tick / land / resolve —— 不丢在途工作，降回来自动恢复。
    # 任何异常都当作「没有这道闸门」，不影响原行为。
    try:
        import budget_gate
        paused, why = budget_gate.dispatch_paused()
        if paused:
            out["enabled"] = False
            out["_paused_by_budget"] = why
    except Exception:
        pass
    return out


def convergence_exec_config() -> dict:
    """convergence 任务的独立派发开关。

    这一层只负责已有 convergence worktree 里的 marker 收口；建族仍由 kgctl converge
    完成。默认只派「单文件/少量 marker」的族，大族继续留给 replan 或人工判断。
    """
    out = {"enabled": True, "max_files": CONVERGE_MAX_FILES,
           "max_markers": CONVERGE_MAX_MARKERS, "max_workers": EXEC_MAX_WORKERS}
    repo = _state_repo()
    if not repo:
        return out
    try:
        data = json.loads((repo / "ops" / "central.config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    cfg = data.get("convergence_exec")
    if isinstance(cfg, bool):
        out["enabled"] = cfg
    elif isinstance(cfg, dict):
        out["enabled"] = bool(cfg.get("enabled", True))
        for key in ("max_files", "max_markers", "max_workers"):
            try:
                out[key] = max(1, int(cfg.get(key, out[key])))
            except (TypeError, ValueError):
                pass
    return out


def _git(repo, *args, timeout=300):
    """跑一条 git 命令，返回 (rc, stdout, stderr)。"""
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _exec_slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (slug or "item")[:limit].strip("-") or "item"


def _codex_bin():
    """找到 codex 可执行文件。KG_EXEC_CMD 可整体覆盖（如带 wrapper 的场景）。"""
    custom = os.environ.get("KG_EXEC_CMD")
    if custom:
        return shlex.split(custom)
    found = shutil.which("codex")
    if found:
        return [found]
    for cand in (Path.home() / ".local/bin/codex",
                 Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")):
        if cand.exists():
            return [str(cand)]
    return None


def _truth_dir():
    repo = _state_repo()
    return (Path(repo) / "ops" / "truth") if repo else None


def _ledger_latest():
    """台账里每个 id 的最后一条记录。目录/文件不可读时返回 None。

    返回 None 与返回 {} 语义不同：None 表示「读不到真相」，调用方不应据此清理
    任何暂缓记录；{} 表示「台账确实为空」。
    """
    tdir = _truth_dir()
    if not tdir:
        return None
    try:
        raw = (tdir / "ledger.jsonl").read_text(encoding="utf-8")
    except OSError:
        return None
    latest = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("id"):
            latest[rec["id"]] = rec
    return latest


def _ledger_open_items(limit=20):
    """台账里还没关闭、也还没派过的待办。排序：bug / gate 优先，其次按时间。"""
    latest = _ledger_latest() or {}
    opens = [r for r in latest.values() if r.get("status") in ("open", "doing")]
    opens.sort(key=lambda r: (0 if r.get("kind") in ("bug", "gate") else 1, r.get("ts", "")))
    # 同一个标题只派一次；并行池里重复条目会抢同一片文件，吞吐反而下降。
    seen_titles = set()
    unique = []
    for r in opens:
        title = (r.get("title") or "").strip()
        if title and title in seen_titles:
            continue
        if title:
            seen_titles.add(title)
        unique.append(r)
    return unique[:limit]


def _oversized_exec_item(item: dict) -> bool:
    """Reject roadmap-sized items that cannot produce one verifiable change."""
    text = f"{item.get('title') or ''}\n{item.get('detail') or ''}".lower()
    phase_marks = sum(text.count(x) for x in ("阶段1", "阶段2", "阶段3", "阶段4"))
    broad = any(x in text for x in (
        "继续按", "全路线", "推进闭环", "继续推进", "继续把阶段",
    ))
    concrete = any(x in text for x in (
        "verify:", "npm run", "npx ", "node test/", "python3 ",
        ".ts", ".mjs", ".py", "/",
    ))
    return phase_marks >= 2 or (broad and not concrete)


def _non_executable_reason(item: dict) -> str | None:
    """Return why an item cannot be completed by a code worker."""
    title = (item.get("title") or "").strip().lower()
    detail = (item.get("detail") or "").strip().lower()
    text = title + "\n" + detail
    human_marks = (
        "人工盲评", "真人盲评", "人工复核", "真人复核", "人工验收",
        "需要人判断", "请用户打开", "等待带指纹",
    )
    external_marks = (
        "tavily 返回 432", "usage limit", "欠费", "余额不足",
        "生产 404", "stripe 未配置", "bing 反爬",
    )
    concrete = any(x in text for x in (
        "verify:", "npm run", "npx ", "node test/", "python3 ",
        ".ts", ".mjs", ".py",
    ))
    if any(x in text for x in human_marks) and not concrete:
        return "needs_human"
    if any(x in text for x in external_marks):
        return "external_blocked"
    return None


def _landed_exec_commit(repo, rid: str) -> str | None:
    """Return a committed ``exec(<id>)`` that is already an ancestor of main."""
    if not repo or not rid:
        return None
    main_ref = "origin/main"
    if _git(repo, "rev-parse", "--verify", main_ref)[0] != 0:
        main_ref = "main"
    prefix = "exec(" + rid + ")"
    rc, out, _ = _git(repo, "log", "--all", "--format=%H %s",
                      "--grep", f"^{prefix}", "-n", "50")
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        sha, subject = parts
        if not (subject or "").startswith(prefix):
            continue
        if _git(repo, "merge-base", "--is-ancestor", sha, main_ref)[0] == 0:
            return sha
    return None


def _prune_exec_blocked(blocked: dict) -> list:
    """丢掉已经不在台账 open/doing 里的「实现暂缓」。

    一条需求被实现并收口（done/superseded）后，它的 blocked 记录就不再代表任何
    真实阻塞；留着只会让面板一直显示一条不存在的暂缓。这里按台账真实状态自愈。
    台账读不到（None）时直接返回空，绝不误清真实的暂缓记录。
    """
    if not blocked:
        return []
    latest = _ledger_latest()
    if latest is None:
        return []
    open_ids = {i for i, r in latest.items() if r.get("status") in ("open", "doing")}
    stale = [k for k in list(blocked) if k not in open_ids]
    for k in stale:
        blocked.pop(k, None)
    return stale


def _run_truthctl(args, extra_env=None, timeout=120):
    cmd = [sys.executable, str(HERE / "truthctl.py"), *args]
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           cwd=str(CODE_ROOT), timeout=timeout, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"调用 truthctl 失败/超时: {exc}"


def _link_side_dirs(wt: Path, cfg: dict) -> None:
    """给执行 worktree 软链 node_modules，省掉重复安装。"""
    repo = _state_repo()
    shared = None
    if cfg.get("shared_dir"):
        shared = Path(cfg["shared_dir"]).expanduser()
    elif repo:
        try:
            main_wt = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                                     capture_output=True, text=True, timeout=30)
            for line in (main_wt.stdout or "").splitlines():
                if line.startswith("worktree "):
                    shared = Path(line[len("worktree "):].strip())
                    break
        except (OSError, subprocess.TimeoutExpired):
            shared = None
    for side in (cfg.get("link_dirs") or ["frontend", "backend"]):
        dst = wt / side / "node_modules"
        if dst.exists():
            continue
        for src in ([shared / side / "node_modules"] if shared else []) + \
                   ([repo / side / "node_modules"] if repo else []):
            if src and src.exists():
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.symlink_to(src, target_is_directory=True)
                except OSError:
                    pass
                break


def _exec_prompt(item: dict) -> str:
    files = item.get("files") or []
    lines = [
        "你是 Codex 自动挡的执行者。下面是一条用户抛出的零散需求，已经在唯一入口归好类、",
        "排好序。请在这个工作区里把它实现掉，并自证做完。",
        "",
        f"需求 [{item.get('id')}]（{item.get('kind') or 'item'}）：{item.get('title') or ''}",
    ]
    if item.get("detail"):
        lines += ["", "补充说明：", str(item["detail"])]
    if files:
        lines += ["", "相关文件线索：" + "、".join(str(f) for f in files)]
    lines += [
        "",
        "硬规则（必须遵守）：",
        "- 只做非破坏性、正向的改动：不要 `git checkout .` / `git reset --hard` / `git stash` / `git add -A`。",
        "- 目标只有两个：(1) 击穿产品预期——前后端真正闭环生效；(2) 保证现有体验——功能、",
        "  性能不能因为这次改动变差或更难用。不达标的实现不如不做。",
        "- 动手前先读仓库里的 AGENTS.md 和相关上下文（如 AI_SESSION_CONTEXT.md、产品决策文件）。",
        "- 改完跑与改动相关的验证（如 `npx tsc --noEmit`、`npm run build`、相关 `verify:*` 脚本）；",
        "  失败就继续修，直到通过或有明确的外部阻塞。",
        "- 不要 `git commit`、不要 `git push`、不要合并分支：把改动留在工作区即可，落盘由自动挡统一走门禁。",
        "- 做完用一句话总结：改了什么、跑了什么验证、结果如何。",
    ]
    return "\n".join(lines)


def _changed_paths(wt: Path) -> list:
    """列出 worktree 里非忽略的改动路径，供显式 git add，绝不使用 git add -A。

    这里刻意不走 _git()：_git 会对 stdout 整体 strip，把 porcelain 首行的前导空格
    （X/Y 状态占位）吃掉，于是 line[3:] 对第一行少切一个字符——backend/package.json
    解析成 ackend/package.json，git add 必然 fatal，现场只能落进 add_fail 永不收口。
    L-0016 的 ackend/package.json、早期 L-0029 的 ackend/test/… 与 L-0030 的
    rontend/scripts/… 都是同一个 bug。所以这里保留原始 stdout，只按行切。
    """
    try:
        r = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                           capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    changed = []
    for line in (r.stdout or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:          # 重命名：两边都要 add
            for part in path.split(" -> "):
                changed.append(part.strip())
        else:
            changed.append(path)
    return changed


def _git_add_retry(wt: Path, paths: list, attempts: int = 3, delay: float = 2.0):
    """显式 git add；对 index.lock / 瞬时锁竞争重试几次，并返回最后一次 stderr。

    2026-10-13 出现过一次「codex 退出码=0，git add 却失败」的落盘丢弃：
    当时 add_fail 没留下任何 stderr，事后无法诊断（重跑又正常）。这里加上
    重试 + 保留 stderr：既扛住瞬时锁竞争，也让下次真失败有据可查。
    """
    err = ""
    for i in range(attempts):
        rc, _, err = _git(wt, "add", "--", *paths)
        if rc == 0:
            return 0, ""
        if i < attempts - 1:
            log(f"[exec] git add 第 {i+1} 次失败（{(err or '')[:160]}），{delay:.0f}s 后重试")
            time.sleep(delay)
    return 1, err


def _branch_pending_commits(repo, branch: str, main_branch: str) -> int:
    """分支上还没进 main 的提交数；分支不存在或读不到一律按 0 算。"""
    rc, out, _ = _git(repo, "rev-list", "--count", f"{main_branch}..{branch}")
    if rc != 0:
        return 0
    try:
        return int((out or "0").strip() or 0)
    except ValueError:
        return 0


def _exec_log_tail(log_path, limit: int = EXEC_INFRA_TAIL_BYTES) -> str:
    """只读执行日志尾部若干字节，用于判断失败是不是基础设施抖动。"""
    try:
        with Path(log_path).open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _resolve_prompt(rec: dict) -> str:
    files = "\n".join(f"- {p}" for p in (rec.get("unresolved_files") or []))
    return (
        "【任务】在当前 resolve worktree 中解决下列冲突 marker。\n"
        f"{files}\n"
        "【期望输出】读取完整冲突上下文，结合两侧意图保留正确最终实现；"
        "显式 git add 目标文件并 git commit，报告 commit sha、修改文件和验证命令/退出码。\n"
        "【证据】rg 冲突 marker、tsc/build 或相关门禁的真实退出码。\n"
        "【禁止】不要修改其他 worktree；不要 git add -A/stash/reset/checkout .；"
        "不要 push/merge；不要放宽或删除门禁断言。只读必要的冲突段和相邻代码。"
    )


def _has_conflict_markers(text: str) -> bool:
    """Require an actual conflict block, not a normal Markdown `=======` divider."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("<<<<<<<"):
            continue
        for j in range(i + 1, len(lines)):
            if lines[j].startswith("======="):
                for k in range(j + 1, len(lines)):
                    if lines[k].startswith(">>>>>>>"):
                        return True
                break
    return False


def _resolve_task_candidates(limit: int) -> list:
    """选已有 resolve worktree 中有真实 marker 的轻任务。

    默认优先干净 worktree。任务专属 worktree 被上一轮 worker 打断时可能留下
    未提交改动；配置 `resume_dirty` 打开后，只要 unresolved_files 里仍有真实
    conflict block，就允许同一个 worker 在该 worktree 原地续解。这里只检查
    自己的任务文件，不碰其他 worktree，也不清理任何改动。
    """
    out = []
    cfg = auto_resolve_config()
    resume_dirty = bool(cfg.get("resume_dirty", True))
    # Candidate selection is based on the on-disk worktree, not the mutable
    # status label. A previous finish attempt can leave `gate_fail` behind even
    # though worktree still contains a real conflict block; that task must be
    # resumable. Source branches that already landed are filtered below.
    tasks = []
    repo = _state_repo()
    if repo:
        try:
            data = json.loads((repo / "ops" / "tasks" / "resolve.json").read_text(encoding="utf-8"))
            tasks = [
                t for t in (data.get("tasks") or [])
                if t.get("status") not in ("landed", "abandoned")
            ]
        except (OSError, ValueError):
            tasks = []
    for rec in tasks:
        wt = Path(rec.get("worktree") or "")
        if not wt.is_dir():
            continue
        rc, dirty, _ = _git(wt, "status", "--porcelain")
        if rc != 0 or (dirty and not resume_dirty):
            continue
        # The source branch records where the conflict came from. Once that
        # source lands, the resolve branch can still hold unique conflict work
        # (especially after replaying a newer main). Candidate completion must
        # therefore be based on the resolve branch when one exists.
        done_ref = str(rec.get("resolve_branch") or rec.get("branch") or "")
        if repo and done_ref:
            # Only skip when we can positively prove the resolve branch is
            # already contained in origin/main. `--is-ancestor` uses exit 1 for
            # "not an ancestor", which is a normal answer, not an error.
            resolved_rc, _, _ = _git(repo, "rev-parse", "--verify", "--quiet",
                                     "origin/main")
            if resolved_rc == 0:
                probe_rc, _, _ = _git(repo, "merge-base", "--is-ancestor",
                                      done_ref, "origin/main")
                if probe_rc == 0:
                    continue
        _, unmerged, _ = _git(
            wt, "diff", "--name-only", "--diff-filter=U")
        if unmerged:
            # Unmerged index entries are authoritative. `unresolved_files` can
            # be stale after a refresh/main-resync; never let that stale list
            # hide the real UU path from the next worker.
            marker_files = [line for line in unmerged.splitlines() if line.strip()]
        else:
            marker_files = []
        for rel in rec.get("unresolved_files") or []:
            try:
                text = (wt / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                marker_files.append(rel)
                continue
            if _has_conflict_markers(text):
                marker_files.append(rel)
        marker_files = list(dict.fromkeys(marker_files))
        if not marker_files:
            continue
        out.append({**rec, "marker_files": marker_files, "dirty_resume": bool(dirty)})
        if len(out) >= limit:
            break
    return out


def _external_resolve_worker_rec(rec: dict) -> dict | None:
    """Find a live external `codex exec` already working this resolve task.

    macOS `ps` may hide full argv from a non-root child even with `-ww`, while
    `lsof -d cwd` remains readable for our own processes. Scan codex processes
    and match their working directory instead of relying on full command text.
    """
    wt = Path(rec.get("worktree") or "").resolve()
    if not wt.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (proc.stdout or "").splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        pid_text, command = fields
        if "codex" not in command or "exec" not in command:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            cwd_proc = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        cwd = ""
        for candidate in (cwd_proc.stdout or "").splitlines():
            if candidate.startswith("n"):
                cwd = candidate[1:]
                break
        if cwd and Path(cwd).resolve() == wt:
            return {"pid": pid, "rec": rec, "started": time.time(), "external": True}
    return None

def _refresh_external_resolve_jobs() -> None:
    """Refresh adoptable external workers and release exited slots."""
    for slug, job in list(_external_resolve_jobs.items()):
        try:
            os.kill(int(job.get("pid")), 0)
        except (OSError, TypeError, ValueError):
            _external_resolve_jobs.pop(slug, None)
    for rec in open_resolve_tasks():
        slug = rec.get("slug")
        if not slug or slug in _resolve_jobs or slug in _external_resolve_jobs:
            continue
        job = _external_resolve_worker_rec(rec)
        if job:
            _external_resolve_jobs[slug] = job
            log(f"[resolve-task] 接管外部 worker {slug}（pid={job['pid']}）")


def _collect_external_resolve_tasks() -> list:
    """Adopt finished external workers through the normal serial finish path."""
    done = []
    for slug, job in list(_external_resolve_jobs.items()):
        rec = job.get("rec") or {}
        wt = Path(rec.get("worktree") or "")
        try:
            os.kill(int(job.get("pid")), 0)
            continue
        except (OSError, TypeError, ValueError):
            pass
        _external_resolve_jobs.pop(slug, None)
        if not wt.is_dir():
            continue
        remaining = []
        for rel in rec.get("unresolved_files") or []:
            try:
                text = (wt / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                remaining.append(rel)
                continue
            if _has_conflict_markers(text):
                remaining.append(rel)
        changed = _changed_paths(wt)
        commit = None
        if not remaining and changed:
            add_rc, _ = _git_add_retry(wt, changed)
            if add_rc == 0 and _git(wt, "commit", "-m", f"resolve: {slug}")[0] == 0:
                _, commit, _ = _git(wt, "rev-parse", "HEAD")
        dirty = _changed_paths(wt)
        outcome = "done" if not remaining and not dirty else (
            "markers_remain" if remaining else "dirty_worktree")
        result = {"kind": "resolve_task", "slug": slug, "worktree": str(wt),
                  "commit": commit, "outcome": outcome}
        done.append(result)
        if outcome == "done":
            _resolve_finish_jobs[slug] = {"rec": rec, "finish_blocked": False}
            log(f"[resolve-task] 外部 worker {slug} 已提交，等待串行 finish")
        else:
            _resolve_task_update(slug, status="needs_agent",
                                 unresolved_files=remaining or rec.get("unresolved_files") or [],
                                 worker_error={"reason": outcome, "ts": _now()})
    if done:
        save_worker_state()
    return done


def _resolve_task_update(slug: str, **fields) -> None:
    repo = _state_repo()
    if not repo:
        return
    path = repo / "ops" / "tasks" / "resolve.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rec = next((t for t in data.get("tasks", []) if t.get("slug") == slug), None)
        if not rec:
            return
        rec.update(fields)
        rec["updated"] = _now()
        data["updated"] = _now()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError, TypeError) as exc:
        log(f"[resolve-task] 写回台账失败 {slug}: {exc!r}")


def _dispatch_resolve_task_one(rec: dict) -> dict:
    slug = rec.get("slug") or rec.get("branch") or "?"
    wt = Path(rec["worktree"])
    codex = _codex_bin()
    if not codex:
        return {"kind": "resolve_task", "slug": slug, "outcome": "no_codex"}
    cfg = auto_resolve_config()
    cmd = [*codex, "exec", "-C", str(wt), "-s", cfg.get("sandbox", "danger-full-access"),
           "--skip-git-repo-check"]
    fast = os.environ.get("KG_EXEC_FAST_MODEL") or cfg.get("fast_model")
    if fast:
        cmd += ["-m", str(fast)]
    cmd += [_resolve_prompt(rec)]
    log_path = _exec_dir() / f"resolve-task-{hashlib.sha1(slug.encode('utf-8')).hexdigest()[:10]}.log"
    log(f"[resolve-task] 派发 {slug}（{len(rec.get('marker_files') or [])} 文件，worktree={wt}）")
    try:
        with log_path.open("w", encoding="utf-8") as fh:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=fh,
                               stderr=subprocess.STDOUT, cwd=str(wt),
                               timeout=cfg["timeout"])
        code = r.returncode
    except subprocess.TimeoutExpired:
        code = 124
    except OSError as exc:
        log(f"[resolve-task] 无法启动 codex: {exc}")
        code = 125
    remaining = []
    for rel in rec.get("marker_files") or []:
        try:
            text = (wt / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            remaining.append(rel)
            continue
        if _has_conflict_markers(text):
            remaining.append(rel)
    changed = _changed_paths(wt)
    commit = None
    if not remaining and changed:
        add_rc, add_err = _git_add_retry(wt, changed)
        if add_rc == 0:
            git_rc, _, git_err = _git(wt, "commit", "-m", f"resolve: {slug}")
            if git_rc == 0:
                _, commit, _ = _git(wt, "rev-parse", "HEAD")
            else:
                add_err = git_err
        if not commit:
            _resolve_task_update(slug, status="gate_fail",
                                 worker_error={"reason": "commit_fail", "detail": (add_err or "")[:300]})
            return {"kind": "resolve_task", "slug": slug,
                    "outcome": "commit_fail", "worktree": str(wt)}
    if remaining:
        outcome = f"codex_exit_{code}" if code != 0 else "markers_remain"
    else:
        dirty = _changed_paths(wt)
        if dirty:
            outcome = "dirty_worktree"
        else:
            outcome = "done"
    result = {"kind": "resolve_task", "slug": slug, "worktree": str(wt),
              "commit": commit, "outcome": outcome}
    if outcome != "done":
        _resolve_task_update(slug, status="gate_fail",
                             unresolved_files=remaining or rec.get("unresolved_files") or [],
                             worker_error={"reason": outcome, "commit": commit, "ts": _now()})
    log(f"[resolve-task] {slug} 退出码={code}，commit={(commit or '')[:12]}，结论={outcome}")
    return result


def _collect_resolve_tasks() -> list:
    done = []
    for slug, job in list(_resolve_jobs.items()):
        fut = job["future"]
        if not fut.done():
            continue
        try:
            result = fut.result()
        except Exception as exc:
            result = {"kind": "resolve_task", "slug": slug,
                      "outcome": "worker_exception", "detail": repr(exc)[:400]}
        _resolve_jobs.pop(slug, None)
        done.append(result)
        if result.get("outcome") == "done":
            job["finish_blocked"] = False
            _resolve_finish_jobs[slug] = job
            log(f"[resolve-task] {slug} 已提交，等待串行 resolve --finish")
        else:
            log(f"[resolve-task] {slug} 未完成：{result.get('outcome')}")
    return done


def _pickup_committed_resolve_tasks() -> None:
    """Adopt resolve worktrees that already contain a clean commit but no marker.

    This covers a daemon restart between the worker commit and serial finish. The
    task is only adopted when the worktree is clean, its HEAD differs from
    origin/main, and no real conflict block remains. Do not require current main
    to already be an ancestor of HEAD: main moves while workers run, and an old
    resolve commit may need to be replayed. `resolve --finish` / land owns the
    mergeability check.
    """
    repo = _state_repo()
    if not repo:
        return
    if not _external_resolve_jobs:
        _refresh_external_resolve_jobs()
    for rec in open_resolve_tasks():
        slug = rec.get("slug")
        if not slug or slug in _resolve_finish_jobs:
            continue
        # Never move a still-running worker into the serial finish queue. A
        # clean target-file commit can exist while the worker is still
        # validating or resolving adjacent paths.
        if slug in _external_resolve_jobs or slug in _resolve_jobs:
            continue
        wt = Path(rec.get("worktree") or "")
        if not wt.is_dir():
            continue
        # A resolve worktree based on older main naturally shows many modified
        # paths after conflicts are resolved; only real unmerged index entries
        # block adoption. The refreshed merge below replays latest origin/main.
        rc, unmerged, _ = _git(wt, "diff", "--name-only", "--diff-filter=U")
        if rc != 0 or unmerged:
            continue
        _, head, _ = _git(wt, "rev-parse", "HEAD")
        if not head:
            continue
        main_rc, main, _ = _git(repo, "rev-parse", "origin/main")
        if main_rc != 0 or not main or head == main:
            continue
        marker = False
        for rel in rec.get("unresolved_files") or []:
            try:
                text = (wt / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                marker = True
                break
            if _has_conflict_markers(text):
                marker = True
                break
        if marker:
            continue
        _resolve_finish_jobs[slug] = {"rec": rec, "finish_blocked": False}
        log(f"[resolve-task] 接管已提交待落盘 {slug}（{head[:12]}）")


def dispatch_resolve_tasks(dry: bool = False) -> list:
    """用独立有界池推进已有 resolve worktree；落盘仍由主周期串行执行。"""
    global _resolve_task_pool
    _refresh_external_resolve_jobs()
    done = _collect_resolve_tasks() + _collect_external_resolve_tasks()
    # A clean committed worktree may be waiting to enter the serial finish
    # queue. Adopt it before looking for marker work so it cannot sit idle
    # behind a legacy auto-split worker.
    _pickup_committed_resolve_tasks()
    if dry:
        return done
    cfg = auto_resolve_config()
    if not cfg.get("enabled"):
        return done
    limit = max(1, int(cfg.get("max_workers", 3)))
    free = (limit - len(_resolve_jobs) - len(_external_resolve_jobs)
            - len(_resolve_finish_jobs))
    if free <= 0:
        return done
    busy = set(_resolve_jobs) | set(_external_resolve_jobs) | set(_resolve_finish_jobs)
    candidates = [r for r in _resolve_task_candidates(free + len(busy))
                  if r.get("slug") not in busy][:free]
    if not candidates:
        return done
    if _resolve_task_pool is None:
        _resolve_task_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=limit, thread_name_prefix="kg-resolve-task")
    for rec in candidates:
        slug = rec["slug"]
        _resolve_jobs[slug] = {"future": _resolve_task_pool.submit(
            _dispatch_resolve_task_one, rec), "rec": rec, "started": time.time()}
        log(f"[resolve-task] 启动后台 worker {slug}（活跃 {len(_resolve_jobs)}/{limit}）")
    save_worker_state()
    return done


def _sync_resolve_worktree_before_finish(rec: dict) -> dict:
    """Replay the newest origin/main into a committed resolve worktree.

    A worker can finish while another task lands and moves main. Finishing on
    the old base then fails the same conflict gate that the worker just cleared.
    Refresh the dedicated worktree first: a clean merge can continue to serial
    land, while a real conflict is preserved as markers for the same worker.
    """
    repo = _state_repo()
    wt = Path(rec.get("worktree") or "")
    if not repo or not wt.is_dir():
        return {"outcome": "blocked", "detail": "state repo or worktree missing"}

    last = ""
    for attempt in range(3):
        rc, _, err = _git(repo, "fetch", "origin", "main", timeout=120)
        if rc == 0:
            break
        last = err
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    else:
        return {"outcome": "fetch_fail", "detail": last[:400]}

    rc, origin_main, err = _git(repo, "rev-parse", "origin/main")
    if rc != 0 or not origin_main:
        return {"outcome": "fetch_fail", "detail": (err or "origin/main missing")[:400]}

    # A resolve worktree is based on an older main and legitimately has many
    # staged differences. Those are not blockers by themselves; checking all
    # of `status` here rejected every committed resolve worker as "dirty".
    rc, unmerged, err = _git(wt, "diff", "--name-only", "--diff-filter=U")
    if rc != 0:
        return {"outcome": "blocked", "detail": (err or "git status failed")[:400]}
    if unmerged:
        files = [line for line in unmerged.splitlines() if line.strip()]
        return {"outcome": "conflict", "files": files,
                "detail": "unmerged entries remain"}

    rc, _, _ = _git(wt, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    if rc == 0:
        return {"outcome": "fresh", "origin_main": origin_main}

    rc, out, err = _git(wt, "merge", "--no-edit", "origin/main", timeout=300)
    if rc == 0:
        _, head, _ = _git(wt, "rev-parse", "HEAD")
        return {"outcome": "merged", "origin_main": origin_main, "head": head,
                "detail": out[:400]}

    _, unmerged, _ = _git(wt, "diff", "--name-only", "--diff-filter=U")
    files = [line for line in unmerged.splitlines() if line.strip()]
    if files:
        return {"outcome": "conflict", "origin_main": origin_main, "files": files,
                "detail": err[:400]}

    # A non-conflict merge failure must not leave the worktree half-merged.
    _git(wt, "merge", "--abort")
    if "would be overwritten by merge" in (err or out):
        return {"outcome": "dirty", "origin_main": origin_main,
                "detail": (err or out)[:400]}
    return {"outcome": "merge_fail", "origin_main": origin_main,
            "detail": (err or out)[:400]}


def finish_resolve_tasks() -> list:
    """Refresh latest main, then serially finish resolved branches.

    Serial landing remains the only writer. The refresh is scoped to each
    resolve worktree and never changes main or a source branch.
    """
    done = []
    for slug, job in list(_resolve_finish_jobs.items()):
        with _land_gate:
            rec = job.get("rec") or {}
            refreshed = _sync_resolve_worktree_before_finish(rec)
            sync_outcome = refreshed.get("outcome")
            if sync_outcome == "conflict":
                rc, out = 0, (f"× {slug} 刷新 origin/main 时出现真实冲突，"
                              f"已保留 marker 交回 worker：\n  "
                              + "\n  ".join(refreshed.get("files") or []))
                kind = "needs_agent"
            elif sync_outcome not in ("fresh", "merged"):
                rc, out = 1, (f"× {slug} 落盘前刷新 origin/main 失败："
                              f"{sync_outcome} {refreshed.get('detail', '')}")
                kind = "gate_fail"
            else:
                rc, out = run_kgctl(["resolve", "--finish", slug, "--go"],
                                    timeout=LAND_TIMEOUT)
                kind = "landed" if "outcome=landed" in (out or "") else "gate_fail"
                if sync_outcome == "merged":
                    out = (f"→ {slug} 已先合入最新 origin/main "
                           f"{(refreshed.get('origin_main') or '')[:10]}，再串行落盘。\n"
                           + (out or ""))
        log_block(f"resolve-task finish {slug} -> {kind}", rc, out)
        result = {"kind": "resolve_task", "slug": slug, "outcome": kind, "rc": rc,
                  "sync": sync_outcome}
        _resolve_finish_jobs.pop(slug, None)
        if kind == "needs_agent":
            _resolve_task_update(slug, status="needs_agent",
                                 unresolved_files=refreshed.get("files") or [],
                                 worker_error={"reason": "main_sync_conflict",
                                               "ts": _now()})
        elif kind != "landed":
            _resolve_task_update(slug, status="gate_fail",
                                 worker_error={"reason": kind, "rc": rc,
                                               "sync": sync_outcome,
                                               "detail": refreshed.get("detail", ""),
                                               "ts": _now()})
        done.append(result)
    if done:
        save_worker_state()
    return done


def classify_exec_infra_failure(log_path) -> bool:
    """日志尾部命中基础设施抖动特征 -> 这次失败可重试，不是需求做不出来。

    底层模型代理（127.0.0.1:15721）偶发 502 Bad Gateway / Transport channel
    closed / ECONNRESET / connection refused 会让 codex 进程以非 0 退出，但需求
    本身没问题（L-0055 实测：代理 16:00:52 已恢复 200）。这类失败在同一 worktree 上
    重跑就能过，不该被记成 codex_exit_1 永久暂缓。

    只看日志尾部，宁可漏判（当普通失败处理）也不整份读大日志。
    """
    return bool(EXEC_INFRA_PATTERN.search(_exec_log_tail(log_path)))


def _preserve_exec(repo, wt: Path, branch: str, rid: str, reason: str, blocked: dict,
                   detail: str = "") -> dict:
    """保留一个执行 worktree：有改动就提交成 WIP，绝不因为失败删掉在途工作。"""
    changed = _changed_paths(wt)
    commit = None
    if changed:
        # add/commit 失败也必须保留 worktree；工作区里的改动本身就是最后的现场。
        if _git_add_retry(wt, changed)[0] == 0:
            git_rc, _, _ = _git(
                wt, "commit", "-m", f"wip({rid}): {reason}, partial work preserved")
            if git_rc == 0:
                _, commit, _ = _git(wt, "rev-parse", "HEAD")
    blocked[rid] = {"reason": reason, "branch": branch, "worktree": str(wt),
                    "commit": commit, "preserve": bool(changed),
                    "detail": (detail or "")[:400], "ts": _now()}
    return {"id": rid, "outcome": reason, "branch": branch,
            "worktree": str(wt), "commit": commit, "preserve": bool(changed)}


def _dispatch_one(item: dict, cfg: dict, blocked: dict) -> dict:
    """把一条台账待办派给一个真实 codex 会话：独立 worktree 实现 -> 门禁落盘 -> 收口。"""
    rid = item.get("id") or "?"
    repo = _state_repo()
    if not repo:
        return {"id": rid, "outcome": "no_repo"}
    conf = central_config.load(repo)
    main_branch = conf.get("main_branch", "main")
    prefix = conf.get("branch_prefix", "codex/")
    slug = f"{_exec_slug(rid)}-{_exec_slug(item.get('title') or '')}"
    branch = f"{prefix}exec-{slug}"
    wt = Path(repo).parent / f"{Path(repo).name}-exec-{slug}"
    # 旧实现一见 worktree / 分支已存在就永久记 leftover，且自己从不清理残留，于是任何
    # 首次收口没走完的需求都会永远卡住（L-0029/L-0030 反复 leftover 的结构根因）。
    # 改为非破坏性自愈：有在途改动、或分支上已有未落提交，都接着走同一道门禁收口；
    # 只有真正没有任何成果（无未提交改动且分支不领先 main）的残留才清掉重建。
    # 绝不丢未提交改动，也绝不删掉已经提交、只是没走完落盘的那笔提交。
    existing = _git(repo, "rev-parse", "--verify", branch)[0] == 0 or wt.exists()
    changed_in_flight = _changed_paths(wt) if wt.exists() else []
    if existing and changed_in_flight:
        log(f"[exec] {rid} 已有在途 worktree -> 跳过新会话，接着收口")
        return _finish_exec(item, cfg, repo, wt, branch, rid,
                            {"id": rid, "branch": branch, "worktree": str(wt)}, blocked)
    if existing and _branch_pending_commits(repo, branch, main_branch):
        log(f"[exec] {rid} worktree 干净但分支已有未落提交 -> 跳过新会话，直接收口")
        return _finish_exec(item, cfg, repo, wt, branch, rid,
                            {"id": rid, "branch": branch, "worktree": str(wt)}, blocked)
    if existing:
        _git(repo, "worktree", "remove", "--force", str(wt))
        if _git(repo, "rev-parse", "--verify", branch)[0] == 0:
            _git(repo, "branch", "-D", branch)
        log(f"[exec] {rid} 残留 worktree/分支已清掉（无未提交改动、分支无未落提交），重建")
    _git(repo, "fetch", "origin", main_branch)
    _, base, _ = _git(repo, "rev-parse", f"origin/{main_branch}")
    if not base:
        _, base, _ = _git(repo, "rev-parse", main_branch)
    rc, out, err = _git(repo, "worktree", "add", "-b", branch, str(wt), base)
    if rc != 0:
        log(f"[exec] 建 worktree 失败 {rid}: {err[:300]}")
        blocked[rid] = {"reason": "worktree_fail", "ts": _now()}
        return {"id": rid, "outcome": "worktree_fail"}
    _link_side_dirs(wt, conf)

    codex = _codex_bin()
    result = {"id": rid, "branch": branch, "worktree": str(wt)}
    if not codex:
        log("[exec] 找不到 codex 可执行文件（设 KG_EXEC_CMD 或把 codex 放进 PATH）")
        blocked[rid] = {"reason": "no_codex", "ts": _now()}
        _git(repo, "worktree", "remove", "--force", str(wt))
        _git(repo, "branch", "-D", branch)
        return {"id": rid, "outcome": "no_codex"}

    cmd = [*codex, "exec", "-C", str(wt), "-s", cfg["sandbox"],
           "--skip-git-repo-check"]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    cmd += [ _exec_prompt(item) ]
    log_path = _exec_dir() / f"{slug}.log"
    retries = max(0, int(cfg.get("retries", EXEC_INFRA_RETRIES)))
    total = retries + 1
    code, attempt, infra = 0, 0, False
    while True:
        attempt += 1
        first = attempt == 1
        if first:
            log(f"[exec] 派发 {rid} -> {branch}（worktree={wt}, codex={codex[0]}）")
        else:
            log(f"[exec] {rid} 第 {attempt}/{total} 次重试"
                f"（同一 worktree={wt}，日志 {log_path}）")
        try:
            with log_path.open("w" if first else "a", encoding="utf-8") as fh:
                if not first:
                    fh.write(f"\n===== auto_exec 重试 第 {attempt}/{total} 次（{_now()}，"
                             f"上一次退出码={code}，判定为基础设施抖动）=====\n")
                    fh.flush()
                r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=fh,
                                   stderr=subprocess.STDOUT, cwd=str(wt),
                                   timeout=cfg["timeout"])
            code = r.returncode
        except subprocess.TimeoutExpired:
            code = 124
        except OSError as exc:
            log(f"[exec] 无法启动 codex: {exc}")
            code = 125
        result["exit"] = code
        log(f"[exec] {rid} codex 退出码={code}（第 {attempt}/{total} 次，日志 {log_path}）")
        if code == 0:
            break
        # 超时(124) 与启动失败(125) 不重试：超时已经吃掉一整个 cfg["timeout"]，再乘 N 会把
        # 一个周期拖垮；启动失败重跑也没有新证据。只有日志尾部命中抖动特征才重跑。
        infra = code not in (124, 125) and classify_exec_infra_failure(log_path)
        if not infra or attempt > retries:
            break
        delay = EXEC_INFRA_BACKOFF * (2 ** (attempt - 1))
        log(f"[exec] {rid} 判定为基础设施抖动（退出码={code}），{delay:.0f}s 后重试")
        time.sleep(delay)
    result["attempts"] = attempt
    if code != 0:
        if infra:
            # 与 codex_exit_N 区分：一眼看出是环境抖动，不是需求做不出来。
            reason = "codex_infra_fail"
            detail = (f"基础设施抖动，同一 worktree 重试 {attempt - 1} 次仍失败；"
                      f"日志尾部: {_exec_log_tail(log_path)[-200:].strip()}")
        else:
            reason = f"codex_exit_{code}"
            detail = ""
        preserved = _preserve_exec(repo, wt, branch, rid, reason, blocked, detail=detail)
        if preserved.get("preserve"):
            result.update({"outcome": "codex_fail_preserved",
                           "commit": preserved.get("commit"),
                           "worktree": preserved.get("worktree")})
        else:
            blocked[rid] = {"reason": reason, "ts": _now()}
            _cleanup_exec(repo, wt, branch)
            result["outcome"] = "codex_fail"
        return result
    return _finish_exec(item, cfg, repo, wt, branch, rid, result, blocked)


def _finish_exec(item, cfg, repo, wt, branch, rid, result, blocked) -> dict:
    """codex 已实现（或从在途现场恢复）之后的公共收尾：add -> commit -> land -> close。"""
    conf = central_config.load(repo)
    main_branch = conf.get("main_branch", "main")
    # 把 codex 留下的改动显式提交（不 git add -A；只加 status 里列出的、非忽略的路径）。
    changed = _changed_paths(wt)
    if changed:
        add_rc, add_err = _git_add_retry(wt, changed)
        if add_rc != 0:
            log(f"[exec] {rid} git add 失败，重试后仍失败：{(add_err or '')[:300]}")
            return _preserve_exec(repo, wt, branch, rid, "add_fail", blocked, detail=add_err)
        title = (item.get("title") or "").strip().replace("\n", " ")[:60]
        msg = f"exec({item.get('id')}): {title}".rstrip()
        if _git(wt, "commit", "-m", msg)[0] != 0:
            return _preserve_exec(repo, wt, branch, rid, "commit_fail", blocked)
        _, commit, _ = _git(wt, "rev-parse", "HEAD")
        result["commit"] = commit
    elif _branch_pending_commits(repo, branch, main_branch):
        # 上一轮已经把改动提交进分支、只是没走完落盘（例如守护在 land 前退出）。
        # 重复实现会覆盖既定成果；直接把这笔提交交给同一道门禁收口。
        _, commit, _ = _git(repo, "rev-parse", branch)
        result["commit"] = commit or result.get("commit")
        log(f"[exec] {rid} worktree 干净但分支已有未落提交 -> 直接收口 {(commit or '')[:12]}")
    else:
        # Zero exit with no diff is not delivery. Keep the branch/worktree as
        # a diagnostic scene instead of silently retrying the same request.
        log(f"[exec] {rid} codex 成功退出但没有产生改动，保留现场等待拆分/复核")
        blocked[rid] = {"reason": "no_changes_preserved", "branch": branch,
                        "worktree": str(wt), "commit": result.get("commit"),
                        "ts": _now()}
        result["outcome"] = "no_changes_preserved"
        return result

    with _land_gate:
        lrc, lout = run_kgctl(["land", branch], timeout=LAND_TIMEOUT)
    kind = classify_land(lout, lrc)
    log_block(f"exec land {branch} -> {kind}", lrc, lout)
    result["land"] = kind
    if kind == "landed":
        evidence = lout.strip()[-800:]
        trc, tout = _run_truthctl(["close", rid, "auto-exec 已实现并落盘"],
                                  extra_env={"KG_TRUTH_COMMIT": commit,
                                             "KG_TRUTH_EVIDENCE": evidence})
        log(f"[exec] close {rid} rc={trc}: {tout.strip()[:200]}")
        if trc != 0:
            # 代码已经在 main，但台账没收口。此时删掉 worktree/分支再派一次，等于
            # 重复实现同一条并可能覆盖他人工作；保留现场并登记，等待人工处理。
            blocked[rid] = {"reason": "landed_close_fail", "branch": branch,
                            "worktree": str(wt), "commit": commit,
                            "close_rc": trc, "ts": _now()}
            result["outcome"] = "landed_close_fail"
            return result
        _cleanup_exec(repo, wt, branch)
        result["outcome"] = "landed"
        return result
    # 执行者已经交付了提交，任何落盘失败都保留 worktree 和分支，避免丢掉在途工作。
    blocked[rid] = {"reason": f"land_{kind}", "branch": branch,
                    "worktree": str(wt), "commit": commit, "ts": _now()}
    result["outcome"] = f"land_{kind}"
    return result


def _cleanup_exec(repo, wt: Path, branch: str) -> None:
    _git(repo, "worktree", "remove", "--force", str(wt))
    _git(repo, "branch", "-D", branch)


def _looks_like_command(text: str) -> bool:
    """判断一串输入是不是「终端命令尾巴」而非一条产品需求。

    用户把 `kgctl ... --help` / `... --list` / 纯 `list` 这类命令残片误投进唯一入口时，
    它们会落到默认泳道（bug）并最终被 auto_exec 当成需求派给一个真实会话去「实现」——
    既烧会话又永不收口（L-0035/L-0037 就是这么来的）。这里挡的是明显不是需求的短命令残片：
    只识别单行、短（<=16 字符）、无空格，或纯 `--flag` 形态，或命令名后紧跟 `--flag`。
    宁可漏判成需求，也不误吞真实需求——所以命中条件收得很窄。
    """
    t = (text or "").strip()
    if not t or "\n" in t or len(t) > 16:
        return False
    if t.startswith("-"):
        return bool(re.fullmatch(r"-{1,2}[A-Za-z][\w-]*", t))
    if re.fullmatch(r"[A-Za-z][\w-]*", t):
        return True
    if re.fullmatch(r"[A-Za-z][\w-]*\s+-{1,2}[A-Za-z][\w-]*", t):
        return True
    return False


def _exec_retryable(blocked: dict, rid: str) -> bool:
    """Return true when an old blocked row is stale enough to retry.

    Terminal/user decisions and externally blocked work stay blocked. A local
    worker failure, timeout, land conflict, or no-change result is a snapshot,
    not a permanent verdict: the repository and configured providers move. A
    long cooldown prevents tight retry loops while allowing the scheduler to
    make progress across the 29th deadline.
    """
    rec = blocked.get(rid) or {}
    reason = str(rec.get("reason") or "")
    if not reason:
        return False
    # These need a human decision, a narrower task, or a deliberate re-open.
    # Automatically releasing them after a cooldown only burns another worker
    # slot on the same unexecutable input.
    if reason in (
        "external_blocked",
        "looks_like_command",
        "no_codex",
        "needs_split",
        "needs_human",
        # Legacy rows written before the state was renamed still mean the same
        # thing: the worker exited 0 without producing a diff.
        "no_changes",
        "no_changes_preserved",
    ):
        return False
    if reason.startswith("landed_close_fail"):
        return False
    try:
        ts = time.mktime(time.strptime(str(rec.get("ts") or ""), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return True
    cooldown = max(0, int(os.environ.get("KG_EXEC_RETRY_COOLDOWN_SEC", "1800")))
    return time.time() - ts >= cooldown


def _prune_retryable_exec_blocked(blocked: dict, log_fn=log) -> list:
    """Release stale local-failure rows before candidate selection.

    `_collect_exec` records a terminal row for successful workers too; those
    are pruned against the truth ledger by `_prune_exec_blocked`. This helper
    only handles the other side: failures that deserve another bounded run.
    """
    released = []
    for rid in list(blocked):
        if _exec_retryable(blocked, rid):
            rec = blocked.pop(rid)
            released.append(rid)
            log_fn(f"[exec] 释放过期暂缓 {rid}（{rec.get('reason')}，{rec.get('ts')}）")
    return released


def _exec_capacity(limit: int, active_ids, live_worktrees) -> tuple[int, int]:
    """Return (active, free) with externally live exec work counted."""
    active = len(set(active_ids)) + len(set(live_worktrees))
    return active, max(0, int(limit) - active)


def dispatch_exec(dry: bool, blocked: dict) -> list:
    """每周期用有界并发池派最多 max_per_cycle 条待办。返回本轮新完成结果。

    并行只发生在隔离 worktree 里的实现阶段；每个 worker 最后都会调用同一个
    `kgctl land`，由共享 land 锁串行化。于是吞吐量来自「同时写/测」，落盘仍然
    一次一个，避免主分支和门禁互相踩。

    实现 worker 必须跨周期存活：若在本周期同步等待 1 小时的会话，tick/land 与
    下一轮派发都会被同一个主线程冻结。这里只负责启动和收割；真正收口由
    `_collect_exec()` 在后续周期完成。
    """
    global _exec_pool
    cfg = auto_exec_config()
    if not cfg["enabled"]:
        return []
    finished = _collect_exec(blocked)
    codex = _codex_bin()
    if not codex:
        log("[exec] auto_exec 已开，但找不到 codex 可执行文件，跳过")
        return finished
    limit = max(1, int(cfg.get("max_workers", 1)))
    active_ids = {rid for rid, v in _exec_jobs.items() if not v["future"].done()}
    # A daemon reload loses futures but not the child process. Count live
    # codex exec worktrees outside our own pool so a restart cannot double-book
    # the same implementation slot. Resolve/convergence workers have their own
    # capacity and must not consume an exec slot.
    live_worktrees = _live_exec_worktrees(kind="exec")
    # Only implementation work consumes implementation slots. Resolve and
    # convergence workers have their own bounded pools; counting their
    # worktrees here would make exec look idle while all workers are busy.
    external = set(live_worktrees)
    active, free = _exec_capacity(limit, active_ids, external)
    if external:
        log(f"[exec] 检测到 {len(external)} 个重载前仍在运行的 exec worktree，按占用槽位处理")
    if free <= 0:
        return finished
    released = [] if dry else _prune_retryable_exec_blocked(blocked)
    candidates = []
    for item in _ledger_open_items():
        rid = item.get("id")
        if rid in blocked:
            continue
        landed = _landed_exec_commit(_state_repo(), rid)
        if landed:
            log(f"[exec] {rid} 已有落盘提交 {landed[:12]}，自动收口，不再重复派发")
            if not dry:
                close_rc, close_out = _run_truthctl([
                    "close", rid, f"exec({rid}) {landed[:12]} 已在 main，自动收口"])
                log(f"[exec] {rid} 自动 close rc={close_rc} {close_out.strip()[:300]}")
                blocked.pop(rid, None)
            continue
        if _looks_like_command(item.get("title")) or _looks_like_command(item.get("detail")):
            log(f"[exec] {rid} 输入像命令残片（{(item.get('title') or '')[:24]!r}），"
                f"不派给执行器")
            blocked[rid] = {"reason": "looks_like_command",
                            "title": (item.get("title") or "")[:60], "ts": _now()}
            continue
        if _oversized_exec_item(item):
            log(f"[exec] {rid} 任务范围过大（跨多个阶段且无单一验收面），"
                "先拆成单个可验证交付，不占用 worker 槽")
            blocked[rid] = {"reason": "needs_split",
                            "title": (item.get("title") or "")[:60], "ts": _now()}
            continue
        non_executable = _non_executable_reason(item)
        if non_executable:
            log(f"[exec] {rid} 不能由代码 worker 完成（{non_executable}），"
                "保留台账等待人工或外部条件恢复")
            blocked[rid] = {"reason": non_executable,
                            "title": (item.get("title") or "")[:60], "ts": _now()}
            continue
        if dry:
            log(f"[dry] 会派发 {rid}（{item.get('kind')}）{item.get('title')}")
            candidates.append(item)
            break
        candidates.append(item)
        if len(candidates) >= min(cfg["max_per_cycle"], free):
            break
    if dry or not candidates:
        return finished

    if _exec_pool is None:
        _exec_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=limit, thread_name_prefix="kg-exec")
    log(f"[exec] 启动后台实现 worker（候选 {len(candidates)}，活跃 {active}/{limit}）")
    for item in candidates:
        rid = item.get("id") or "?"
        if rid in _exec_jobs and not _exec_jobs[rid]["future"].done():
            continue
        local_blocked: dict = {}
        fut = _exec_pool.submit(_dispatch_one, item, cfg, local_blocked)
        _exec_jobs[rid] = {"future": fut, "item": item,
                           "blocked": local_blocked, "started": time.time()}
        active += 1
        log(f"[exec] 派发 {rid}（后台槽位 {active}/{limit}）")
    save_worker_state(exec_done=finished)
    return finished


def _collect_exec(blocked: dict | None = None) -> list:
    """收割已经完成的实现 worker；不阻塞主 daemon 周期。"""
    finished = []
    for rid, job in list(_exec_jobs.items()):
        fut = job["future"]
        if not fut.done():
            continue
        try:
            result = fut.result()
        except Exception as exc:
            result = {"id": rid, "outcome": "worker_exception",
                      "detail": repr(exc)[:400]}
            log(f"[exec] worker {rid} 异常（已隔离）：{exc!r}")
        local = job.get("blocked") or {}
        if result.get("outcome") and result.get("id"):
            local.setdefault(result["id"], {
                "reason": result.get("outcome"),
                "branch": result.get("branch"),
                "worktree": result.get("worktree"),
                "commit": result.get("commit"),
                "ts": _now(),
            })
        if blocked is not None:
            blocked.update(local)
        _exec_jobs.pop(rid, None)
        finished.append(result)
        log(f"[exec] {rid} 后台完成：{result.get('outcome')}")
    if finished:
        save_worker_state(exec_done=finished)
    return finished


def _exec_dir() -> Path:
    d = RUNTIME / "exec"
    d.mkdir(parents=True, exist_ok=True)
    return d



def convergence_candidates(blocked: dict, limit: int = EXEC_MAX_WORKERS) -> list:
    """只挑可直接交给独立 worker 的 convergence 任务。

    条件：仍有 needs_resolution、worktree 干净且已存在、冲突文件数和 marker 数
    都在闸门内。worker 只负责解 marker 和提交；land 仍由 daemon 串行执行。
    """
    repo = _state_repo()
    if not repo:
        return []
    cfg = convergence_exec_config()
    if not cfg["enabled"]:
        return []
    try:
        data = json.loads((repo / "ops" / "tasks" / "convergence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log("[convergence] 读不到 convergence.json，本轮跳过")
        return []
    out = []
    for rec in data.get("groups") or []:
        rid = rec.get("id") or rec.get("branch")
        if not rid or rec.get("status") != "needs_resolution":
            continue
        files = rec.get("unresolved_files") or []
        if not files or len(files) > cfg["max_files"]:
            continue
        wt = Path(rec.get("worktree") or "")
        if not wt.is_dir():
            continue
        rc, dirty, _err = _git(wt, "status", "--porcelain")
        if rc != 0 or dirty:
            continue
        marker_total = 0
        for rel in files:
            try:
                text = (wt / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                marker_total = cfg["max_markers"] + 1
                break
            marker_total += sum(text.count(tok) for tok in ("<<<<<<<", "=======", ">>>>>>>"))
        if marker_total <= 0 or marker_total > cfg["max_markers"]:
            continue
        out.append({**rec, "marker_total": marker_total})
        if len(out) >= limit:
            break
    return out


def _convergence_prompt(rec: dict) -> str:
    files = "\n".join(f"- {p}" for p in rec.get("unresolved_files") or [])
    return (
        "【任务】在当前 worktree 中解决下列冲突 marker。先读取完整冲突上下文，"
        "结合 HEAD 与两侧分支意图做最终实现；不要机械删断言，不要掩盖回归。\n"
        f"{files}\n"
        "【期望输出】只修改上述文件，显式 git add 这些文件并提交；最后报告 commit sha、"
        "修改文件和验证命令/退出码。\n"
        "【证据】报告 rg marker、tsc/build 或相关门禁的真实退出码。\n"
        "【禁止】不要修改其他 worktree；不要 git add -A/stash/reset/checkout .；"
        "不要 push/merge/land。不要遍历全仓历史；只读当前文件冲突段和必要的相邻上下文。"
        "若 apply_patch 因格式被拒，改用 perl -0pi 或先把标准 patch 写到 /tmp/*.patch 再 git apply；"
        "不要反复重发同一种失败格式。"
    )


def _dispatch_convergence_one(rec: dict, blocked: dict) -> dict:
    rid = rec.get("id") or rec.get("branch") or "?"
    wt = Path(rec["worktree"])
    codex = _codex_bin()
    if not codex:
        return {"id": rid, "outcome": "no_codex"}
    log_path = _exec_dir() / f"converge-{rid}.log"
    cmd = [*codex, "exec", "-C", str(wt), "-s", "workspace-write",
           "--skip-git-repo-check"]
    cfg = auto_exec_config()
    fast = os.environ.get("KG_EXEC_FAST_MODEL") or cfg.get("model")
    if fast:
        cmd += ["-m", str(fast)]
    cmd += [_convergence_prompt(rec)]
    log(f"[convergence] 派发 {rid}（{len(rec.get('unresolved_files') or [])} 文件 / "
        f"{rec.get('marker_total')} markers，worktree={wt}）")
    try:
        with log_path.open("w", encoding="utf-8") as fh:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=fh,
                               stderr=subprocess.STDOUT, cwd=str(wt),
                               timeout=EXEC_DEFAULT_TIMEOUT)
        code = r.returncode
    except subprocess.TimeoutExpired:
        code = 124
    except OSError as exc:
        code = 125
        log(f"[convergence] 无法启动 codex: {exc}")
    _, commit, _ = _git(wt, "rev-parse", "HEAD")
    dirty = _changed_paths(wt)
    marker_files = []
    for rel in rec.get("unresolved_files") or []:
        try:
            text = (wt / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            marker_files.append(rel)
            continue
        if _has_conflict_markers(text):
            marker_files.append(rel)
    if code != 0:
        outcome = f"codex_exit_{code}"
    elif dirty:
        outcome = "dirty_worktree"
    elif marker_files:
        outcome = "markers_remain"
    else:
        outcome = "done"
    result = {"id": rid, "branch": rec.get("branch"), "worktree": str(wt),
              "commit": commit, "dirty": dirty, "marker_files": marker_files,
              "outcome": outcome}
    if outcome != "done":
        blocked[rid] = {"reason": outcome, "branch": rec.get("branch"),
                        "worktree": str(wt), "commit": commit,
                        "marker_files": marker_files[:8], "ts": _now()}
    log(f"[convergence] {rid} 退出码={code}，commit={commit[:12]}，残留改动={len(dirty)}，"
        f"残留marker文件={len(marker_files)}，结论={outcome}")
    return result


def _convergence_update(rid: str, **fields) -> None:
    """把 worker 的真实结局写回 convergence 台账，供 tick/queue/land 接手。"""
    repo = _state_repo()
    if not repo:
        return
    path = repo / "ops" / "tasks" / "convergence.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        groups = data.get("groups") or []
        record = next((g for g in groups if g.get("id") == rid), None)
        if not record:
            return
        record.update(fields)
        record["updated"] = _now()
        data["updated"] = _now()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError, TypeError) as exc:
        log(f"[convergence] 写回台账失败 {rid}: {exc!r}")


def _convergence_collect() -> list:
    """收割已完成 worker，刷新台账；worker 不阻塞下一轮 tick/land。"""
    done = []
    for rid, item in list(_conv_jobs.items()):
        fut, rec = item["future"], item["rec"]
        if not fut.done():
            continue
        try:
            result = fut.result()
        except Exception as exc:
            result = {"id": rid, "outcome": "worker_exception", "detail": repr(exc)[:400]}
        outcome = result.get("outcome") or "unknown"
        if outcome == "done":
            _convergence_update(rid, status="ready", unresolved_files=[],
                                head=result.get("commit", rec.get("head", "")))
            log(f"[convergence] {rid} 已完成并标 ready，等待串行 land")
        else:
            markers = result.get("marker_files") or []
            _convergence_update(rid, status="needs_resolution",
                                unresolved_files=markers or rec.get("unresolved_files") or [],
                                worker_error={"reason": outcome,
                                              "commit": result.get("commit", ""),
                                              "ts": _now()})
            # 只抑制本拍内重复派发；下一拍读台账与 worktree 真状态重新判断。
            _conv_blocked.pop(rid, None)
            log(f"[convergence] {rid} 未完成：{outcome}，保持 needs_resolution")
        _conv_jobs.pop(rid, None)
        done.append(result)
    if done:
        state = load_state()
        state["convergence_active"] = sorted(_conv_jobs)
        save_state(state)
    return done


def _collect_resolve_worker() -> list:
    """收割后台 `resolve --auto --go`，把日志和结构化结果交给本轮台账。"""
    global _resolve_future
    if _resolve_future is None or not _resolve_future.done():
        return []
    try:
        rc, out = _resolve_future.result()
    except Exception as exc:
        rc, out = 1, f"resolve worker 异常: {exc!r}"
    log_block("resolve(auto/background)", rc, out)
    _resolve_future = None
    save_heartbeat(current_stage="resolve_done")
    parsed = [{"branch": b, "outcome": o}
              for b, o in RESOLVE_RESULT_RE.findall(out or "")]
    result = {"kind": "resolve", "rc": rc, "results": parsed}
    save_worker_state(resolve_done=[result])
    return [result]


def _start_resolve_worker() -> list:
    """没有空闲 convergence 槽位时，用同一线程池跑一条 resolve --auto。"""
    global _resolve_future, _conv_pool
    if _resolve_future is not None:
        return []
    cfg = auto_resolve_config()
    if not cfg.get("enabled"):
        return []
    if _conv_pool is None:
        _conv_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, convergence_exec_config().get("max_workers", EXEC_MAX_WORKERS)),
            thread_name_prefix="kg-resolve")
    args = ["resolve", "--auto", "--go", "--max-files", str(cfg["max_files"]),
            "--max-lines", str(cfg["max_lines"])]
    log("[resolve] 启动后台 worker（复用现有 kgctl resolve --auto）")
    save_heartbeat(current_stage="resolve_worker")
    _resolve_future = _conv_pool.submit(run_kgctl, args, RESOLVE_TIMEOUT)
    return [{"kind": "resolve", "started": True}]


def dispatch_convergence(dry: bool, blocked: dict = None) -> list:
    """启动有界并行 worker；每拍先收割，返回**本拍新完成**的结果。"""
    global _conv_pool
    finished = _convergence_collect() + _collect_resolve_worker()
    cfg = convergence_exec_config()
    if not cfg["enabled"]:
        return finished
    limit = max(1, int(cfg.get("max_workers", EXEC_MAX_WORKERS)))
    free = limit - len(_conv_jobs) - (1 if _resolve_future is not None else 0)
    try:
        _refresh_external_resolve_jobs()
    except NameError:
        pass
    has_real_resolve = bool(open_resolve_tasks() or _resolve_jobs
                            or _external_resolve_jobs or _resolve_finish_jobs)
    merge_blocked = {**_conv_blocked, **(blocked or {})}
    candidates = convergence_candidates(merge_blocked, limit=max(1, free)) if free > 0 else []
    if dry:
        for rec in candidates:
            log(f"[dry] 会派 convergence {rec.get('id')}（{rec.get('marker_total')} markers）")
        return finished
    if _conv_pool is None:
        _conv_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=limit, thread_name_prefix="kg-converge")
    for rec in candidates:
        rid = rec.get("id") or rec.get("branch")
        if rid in _conv_jobs:
            continue
        fut = _conv_pool.submit(_dispatch_convergence_one, rec, _conv_blocked)
        _conv_jobs[rid] = {"future": fut, "rec": rec, "started": time.time()}
        log(f"[convergence] 启动后台 worker {rid}（活跃 {len(_conv_jobs)}/{limit}）")
    if candidates or _conv_jobs:
        state = load_state()
        state["convergence_active"] = sorted(_conv_jobs)
        save_state(state)
    # convergence 与 resolve 共用有界池。每轮最多派一条 resolve，但只要基础槽位够，
    # 就给 convergence 留一个位置：否则大族持续有新 worker 时，轻冲突会长期饿死。
    conv_used = len(_conv_jobs)
    resolve_used = 1 if _resolve_future is not None else 0
    # A live worker on an existing worktree and a splitter for a new source are
    # separate slots. A splitter may only run when no other splitter is live;
    # this keeps the pool bounded while allowing idle resolve capacity to help
    # with new conflicts instead of waiting behind one long resolve task.
    existing_resolve_slots = len(_resolve_jobs) + len(_external_resolve_jobs)
    finish_slots = len(_resolve_finish_jobs)
    split_slots = 1 if _resolve_future is not None else 0
    available = limit - conv_used - existing_resolve_slots - finish_slots - split_slots
    can_split = (split_slots == 0 and available >= 1
                 and auto_resolve_config().get("auto_split")
                 and not auto_resolve_config().get("disable_auto_split"))
    if can_split:
        finished += _start_resolve_worker()
    return finished


def run_cycle(dry: bool, max_land: int) -> dict:
    """用户可见的一轮：先过「暂停」闸门，再调用真正的周期实现。

    暂停语义（面板「暂停」按钮就是写这个开关）：
      * 暂停时**不启动任何新周期** —— 不 tick、不 land、不 resolve。
      * 正在跑的周期允许安全收尾，不会被从中间砍断（land 自带完整门禁）。
      * 因此这里是最外层也最可靠的闸门；`_run_cycle` 里的逐子系统闸门是第二道保险，
        防止别的调用方绕过本函数直接进内部实现。
    """
    if control_state.is_paused():
        log("[pause] 已暂停：本轮不启动任何新工作（面板可恢复）")
        return {"ts": time.time(), "iso": _now(), "dry": dry,
                "paused": True, "summary": {}, "landed": [], "failures": []}
    return _run_cycle(dry, max_land)


def _run_cycle(dry: bool, max_land: int) -> dict:
    """一个周期：刷新控制面 -> 串行落盘安全子集 -> 按需通知。返回本轮结果。"""
    if dry:
        # 真预演：只问队列，不 tick（tick 会提交并推送真相层）、不 land、不通知。
        log("--- dry-run: 只预测本轮会 land 哪些分支 ---")
        rc, qout = run_kgctl(["queue"])
        clean, summary = parse_queue(qout)
        log_block("queue(dry)", rc, qout)
        log(f"[dry] 本轮会 land {min(len(clean), max_land)} / {len(clean)} 个: "
            f"{clean[:max_land]}")
        return {"ts": time.time(), "iso": _now(), "dry": True,
                "summary": summary, "landed": [], "failures": []}

    log("--- cycle start (live) ---")
    save_heartbeat(current_stage="cycle_start")
    if control_state.is_paused():
        log("[pause] 周期开始前被暂停：只刷新状态，不做路由/落盘")
        return {"ts": time.time(), "iso": _now(), "dry": dry,
                "paused": True, "summary": {}, "landed": [], "failures": []}
    rc, out = run_kgctl(["tick"])
    save_heartbeat(current_stage="tick_done")
    log_block("tick", rc, out)
    convergence = parse_convergence(out)
    log(f"[converge] 可收拢族 {convergence['families']}，本轮新建 {convergence['made']}")

    # 归档「内容已全部在 main」的重复分支。这一步只写台账（加法），不动任何代码，
    # 也不动任何源分支，所以放进守护的安全子集。用 --fast（patch-id + tree）保证秒级返回。
    superseded = 0
    src, sout = run_kgctl(["supersede", "--go", "--fast"], timeout=SUPERSEDE_TIMEOUT)
    log_block("supersede", src, sout)
    m = SUPERSEDE_DONE_RE.search(sout)
    if m:
        superseded = int(m.group(1))

    landed, failures = [], []
    convergence_results = []
    resolve_task_results = []
    resolve_finish_results = []
    if rc == 124:
        # 超时必须算失败：旧实现里 tick 超时的 out 是一条说明文案（非空），于是既不进
        # failures 也不报警——30 分钟的黑洞在面板上显示成 last_failures=0。
        failures.append(("(tick)", "tick_timeout"))
    elif rc != 0 and not out.strip():
        failures.append(("(tick)", "tick_error"))

    auto_land = control_state.load().get("auto_land", True)
    summary = {"other": 0, "merged": 0, "conflict": 0, "unresolved": 0}
    attempts = 0
    while auto_land and attempts < max_land:
        rc, qout = run_kgctl(["queue"])
        clean, summary = parse_queue(qout)
        if rc != 0:
            log_block("queue", rc, qout)
            failures.append(("(queue)", "queue_error"))
            break
        if not clean:
            log(f"[queue] 可落盘 0 个（merged={summary['merged']} "
                f"conflict={summary['conflict']}）")
            break
        target = clean[0]
        attempts += 1
        save_heartbeat(current_stage=f"landing:{target}")
        if dry:
            log(f"[dry] 本轮会 land: {target}（队列 {len(clean)} 个）")
            break
        with _land_gate:
            lrc, lout = run_kgctl(["land", target], timeout=LAND_TIMEOUT)
        save_heartbeat(current_stage=f"land_done:{target}", current_land=target)
        kind = classify_land(lout, lrc)
        log_block(f"land {target} -> {kind}", lrc, lout)
        if kind == "landed":
            landed.append(target)
            continue
        if kind == "already":
            continue
        if kind in ("locked", "noop"):
            break
        failures.append((target, kind))
        break

    # Land is the single-writer contract, not a reason to serialize every
    # worker behind an old conflict-splitting tick. Drain committed resolve
    # worktrees first, then fill the bounded worker pool. Code work stays
    # parallel while every write to main still passes through _land_gate.
    # First adopt committed no-marker worktrees into the finish queue. Then
    # drain that queue serially. Only after that fill the worker pool, so an
    # empty finish queue cannot be mistaken for no work.
    _pickup_committed_resolve_tasks()
    resolve_finish_results = finish_resolve_tasks()
    convergence_results = dispatch_convergence(dry, {})
    resolve_task_results = dispatch_resolve_tasks(dry)
    save_worker_state()
    if convergence_results:
        log(f"[convergence] 本轮完成 {len(convergence_results)} 路，等待串行 land")
    else:
        log("[convergence] 本轮没有可派任务的干净 convergence worktree")
    save_heartbeat(current_stage="convergence_done")

    # 自动移植「只差几个提交」的冲突分支：cherry-pick 到当前 main，过门禁才落盘，
    # 冲突即整体放弃。默认关闭，由状态根 ops/central.config.json 的 auto_port 打开。
    # 一周期内把所有候选试一遍：成功的落盘并继续，冲突的进 port_blocked 后跳过，
    # 于是循环不断收窄，直到没有候选或达到上限。试过且不行的分支后续周期不再重试。
    ported = []
    port_blocked = dict(load_state().get("port_blocked") or {})
    port_cfg = auto_port_config()
    if not dry and port_cfg["enabled"] and not (rc != 0 and not out.strip()):
        for _ in range(PORT_MAX_ATTEMPTS):
            prc, pout = run_kgctl(["port", "--auto", "--go",
                                   "--max-unlanded", str(port_cfg["max_unlanded"])],
                                  timeout=PORT_TIMEOUT,
                                  env_extra={"KG_PORT_SKIP": ":".join(port_blocked)})
            log_block("port(auto)", prc, pout)
            pm = PORT_RESULT_RE.search(pout)
            if not pm:
                break  # 没有候选了
            pb, po = pm.group(1), pm.group(2)
            if po == "landed":
                ported.append(pb)
                port_blocked.pop(pb, None)
                continue  # main 已前进，下一轮重新挑
            port_blocked[pb] = {"reason": po, "ts": _now()}
        # 移植推进了 main，可能让别的分支的改动「现在全部在 main 了」——再扫一次归档。
        if ported:
            src2, sout2 = run_kgctl(["supersede", "--go", "--fast"], timeout=SUPERSEDE_TIMEOUT)
            log_block("supersede(post-port)", src2, sout2)
            m2 = SUPERSEDE_DONE_RE.search(sout2)
            if m2:
                superseded += int(m2.group(1))
    elif not port_cfg["enabled"]:
        log("[port] auto_port 未开启（ops/central.config.json），跳过自动移植")

    # 冲突分支不再推给用户逐条裁决：每周期把一条「文件少、未落改动小」的冲突拆成
    # 独立可执行单元。能被确定性解掉的（append-only jsonl 行级并集）直接走门禁落盘；
    # 其余保留 marker，写出一份任务书 + 独立 worktree，交给 agent 按 slug 收口。
    # 拆不动 / 门禁没过的分支记进 resolve_blocked，后续周期跳过，不反复拆同一条。
    resolved_tasks, resolve_gate_fail = [], []
    resolve_blocked = dict(load_state().get("resolve_blocked") or {})
    resolve_cfg = auto_resolve_config()
    resolve_landed_any = False
    for rr in convergence_results:
        if rr.get("kind") != "resolve":
            continue
        for item in rr.get("results") or []:
            rb, ro = item.get("branch"), item.get("outcome")
            if not rb:
                continue
            if ro == "landed":
                resolve_landed_any = True
            elif ro == "needs_agent":
                resolved_tasks.append(rb)
            elif ro in ("too_big", "blocked", "clean"):
                resolve_blocked[rb] = {"reason": ro, "ts": _now()}
            elif ro == "gate_fail":
                resolve_gate_fail.append(rb)
                resolve_blocked[rb] = {"reason": ro, "ts": _now()}
    if not dry and resolve_cfg["enabled"] and not (rc != 0 and not out.strip()):
        # Existing resolve worktrees are the priority. Only ask kgctl to split
        # a new source branch when the worker pool has no real worktree left.
        if (not open_resolve_tasks()
                and not _resolve_jobs and not _resolve_finish_jobs
                and resolve_cfg.get("auto_split")
                and not resolve_cfg.get("disable_auto_split")):
            started = _start_resolve_worker()
            if started:
                log("[resolve] 后台拆分已启动，下一周期收割")
    elif not resolve_cfg["enabled"]:
        log("[resolve] auto_resolve 未开启（ops/central.config.json），跳过冲突拆分")

    # 自动实现：把台账里还没做的待办派给一个真实 codex 会话，在独立 worktree 里
    # 实现 -> 门禁 -> 落盘 -> 收口。默认关闭（面板「自动实现需求」开关 / control.json）。
    exec_results, exec_landed = [], []
    exec_blocked = dict(load_state().get("exec_blocked") or {})
    # 已收口的需求不该继续挂在「实现暂缓」名单里（代码/台账都已落定）。
    for _stale in _prune_exec_blocked(exec_blocked):
        log(f"[exec] 台账已收口，清掉实现暂缓：{_stale}")
    exec_cfg = auto_exec_config()
    if dry:
        dispatch_exec(True, exec_blocked)
    elif exec_cfg["enabled"] and not (rc != 0 and not out.strip()):
        exec_results = _collect_exec(exec_blocked) + dispatch_exec(False, exec_blocked)
        save_worker_state()
        exec_landed = [r for r in exec_results if r.get("outcome") == "landed"]
    elif not exec_cfg["enabled"]:
        log("[exec] auto_exec 未开启（面板开关 / control.json），跳过自动实现")

    # 落过盘就再刷一次控制面，让看板/快照反映新 main。
    if (landed or ported or resolve_landed_any or exec_landed) and not dry:
        rc2, out2 = run_kgctl(["tick"])
        log_block("tick(post-land)", rc2, out2)

    result = {"ts": time.time(), "iso": _now(), "dry": dry,
              "summary": summary, "landed": landed, "failures": failures,
              "superseded": superseded,
              "ported": ported,
              "resolved_tasks": resolved_tasks,
              "resolve_gate_fail": resolve_gate_fail,
              "exec_results": exec_results,
              "convergence": convergence,
              "convergence_results": convergence_results,
              "resolve_task_results": resolve_task_results,
              "resolve_finish_results": resolve_finish_results}

    # ---- 通知：只在「完成 / 失败 / 新收拢出族」时出声，且去重 ----
    state = load_state()
    prev = state.get("last_summary") or {}
    prev_conflict = prev.get("conflict")
    prev_families = int(state.get("convergence_families", 0))

    if landed:
        notify("自动挡：已自动落盘",
               f"自动合入 {len(landed)} 个分支：{', '.join(landed[:3])}"
               + (" 等" if len(landed) > 3 else ""))
    if superseded:
        notify("自动挡：已归档重复分支",
               f"{superseded} 个分支的改动已全部在 main，已归档，不再占用冲突队列")
    if ported:
        notify("自动挡：已自动移植落盘",
               f"{len(ported)} 个「只差几个提交」的冲突分支已移植并落盘："
               f"{', '.join(ported[:3])}")
    if resolve_landed_any:
        notify("自动挡：冲突已自动解掉并落盘",
               "一条冲突分支的确定性部分已并集、过了门禁并合进 main")
    if resolved_tasks:
        notify("自动挡：冲突已拆成可执行任务",
               f"{len(resolved_tasks)} 条冲突分支已拆成独立任务书（保留冲突文件），"
               "agent 按任务书解开后走同一道门禁落盘，不用你逐条裁决")
    if resolve_gate_fail:
        notify("自动挡：冲突任务门禁没过",
               f"{', '.join(resolve_gate_fail[:3])} 留在独立 worktree 里，"
               "已登记 gate_fail，修到过门禁再收口")
    if exec_landed:
        notify("自动挡：已自己做完并落盘",
               f"{len(exec_landed)} 条需求由独立会话实现、过门禁并合进 main："
               f"{', '.join(str(r.get('id', '')) for r in exec_landed[:3])}")
    exec_failed = [r for r in exec_results if r.get("outcome") != "landed"]
    if exec_failed:
        r0 = exec_failed[0]
        notify("自动挡：自动实现没走完",
               f"{r0.get('id')} 卡在 {r0.get('outcome')}，已记进 exec_blocked，不再重复派"
               "（修好对应条件后可重开该条目）")
    fail_sig = _sig(failures) if failures else None
    if failures and fail_sig != state.get("last_failure_sig"):
        target, kind = failures[0]
        notify("自动挡：有分支没落成",
               f"{target} 卡在 {kind}，需要裁决或改门禁（其余 "
               f"{max(len(failures) - 1, 0)} 项见 daemon.log）")
    # 冲突不再推给用户逐条裁决：自动收拢成族，只在「新收拢出族」时告知一次。
    if convergence["made"] > 0 and convergence["families"] != prev_families:
        notify("自动挡：冲突已收拢成族",
               f"冲突分支已收成 {convergence['families']} 个 convergence 任务"
               f"（本轮新建 {convergence['made']} 个），agent 逐族解，不用你逐条裁决")

    state["last_summary"] = summary
    state["last_cycle_ts"] = result["ts"]
    state["last_cycle_iso"] = result["iso"]
    state["last_landed"] = landed or state.get("last_landed", [])
    state["last_failures"] = failures
    state["last_failure_sig"] = fail_sig
    state["convergence_families"] = convergence["families"]
    state["total_landed"] = int(state.get("total_landed", 0)) + len(landed)
    state["total_superseded"] = int(state.get("total_superseded", 0)) + superseded
    state["total_ported"] = int(state.get("total_ported", 0)) + len(ported)
    state["total_resolved"] = int(state.get("total_resolved", 0)) + (
        1 if resolve_landed_any else 0) + len(resolved_tasks)
    state["last_resolved_tasks"] = resolved_tasks or state.get("last_resolved_tasks", [])
    state["last_convergence"] = ([{"id": r.get("id"), "outcome": r.get("outcome")}
                                  for r in convergence_results]
                                 or state.get("last_convergence", []))
    state["last_resolve_tasks"] = ([{"slug": r.get("slug"), "outcome": r.get("outcome")}
                                    for r in resolve_task_results]
                                   or state.get("last_resolve_tasks", []))
    state["last_resolve_finish"] = ([{"slug": r.get("slug"), "outcome": r.get("outcome")}
                                     for r in resolve_finish_results]
                                    or state.get("last_resolve_finish", []))
    state["resolve_blocked"] = resolve_blocked
    state["port_blocked"] = port_blocked
    state["exec_blocked"] = exec_blocked
    state["last_exec"] = ([{"id": r.get("id"), "outcome": r.get("outcome")}
                           for r in exec_results] or state.get("last_exec", []))
    state["total_exec"] = int(state.get("total_exec", 0)) + len(exec_landed)
    save_state(state)

    log(f"--- cycle done: landed={len(landed)} failures={len(failures)} "
        f"conflict={summary['conflict']} converge_families={convergence['families']} ---")
    control_state.append_event(
        "cycle_done",
        landed=list(landed),
        ported=list(ported),
        superseded=superseded,
        resolved_tasks=list(resolved_tasks),
        resolve_gate_fail=list(resolve_gate_fail),
        exec=[{"id": r.get("id"), "outcome": r.get("outcome")} for r in exec_results],
        failures=[{"target": t, "kind": k} for t, k in failures],
        conflict=summary.get("conflict"),
        merged=summary.get("merged"),
    )
    return result


# ------------------------------------------------------------------ 单实例

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock_owner_alive() -> bool:
    """读锁文件，看里面那个 pid 是否还活着。锁不存在 / 内容坏都返回 False。"""
    try:
        pid = int(DAEMON_LOCK.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return False
    return pid > 0 and pid != os.getpid() and _pid_alive(pid)


def acquire_lock() -> bool:
    """单实例锁。

    原子性靠 O_CREAT|O_EXCL —— 谁先创建成功谁持锁，杜绝 bootstrap + kickstart
    同时拉起两个进程时的 check-then-act 竞态（旧版 read-then-write 会双开）。
    锁里 pid 已死才清陈旧锁；活锁直接让位。
    """
    RUNTIME.mkdir(parents=True, exist_ok=True)
    payload = f"{os.getpid()} {_now()}\n"
    for _ in range(3):
        try:
            fd = os.open(DAEMON_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if _lock_owner_alive():
                return False
            try:
                DAEMON_LOCK.unlink()  # 陈旧锁：持锁进程已不在
            except OSError:
                return False
            continue
        except OSError:
            return False
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    return False


def heartbeat_lock() -> None:
    """刷新锁文件时间戳，让存活中的守护不会被当成陈旧锁。只在自己持锁时写。"""
    try:
        content = DAEMON_LOCK.read_text(encoding="utf-8")
        if content.split()[0] == str(os.getpid()):
            DAEMON_LOCK.write_text(f"{os.getpid()} {_now()}\n", encoding="utf-8")
    except (OSError, IndexError, ValueError):
        pass


def release_lock() -> None:
    try:
        if DAEMON_LOCK.exists():
            content = DAEMON_LOCK.read_text(encoding="utf-8")
            if content.split()[0] == str(os.getpid()):
                DAEMON_LOCK.unlink()
    except (OSError, IndexError, ValueError):
        pass


# ------------------------------------------------------------------ launchd

def _python_for_plist() -> str:
    return sys.executable or "/usr/bin/python3"


def build_plist(interval_min: int) -> dict:
    # 守护永远跑插件目录里的这份脚本。KG_TRUNK 是落盘用的主干 worktree，它的 ops/
    # 可能是旧副本；如果 plist 指向 `$KG_TRUNK/ops/central_daemon.py`，插件升级后
    # 守护又会跑旧代码 —— 这正是自包含安装要消掉的问题。KG_TRUNK 只作为运行时旋钮
    # 传给脚本，不用来定位运行时。
    #
    # 关键约束：落盘主干不能等于状态根（KG_REPO）。KG_TRUNK 指到账本目录时，land 的
    # merge/reset 会直接改写 ops/tasks、ops/truth，实测因此丢过路由结果。所以
    # KG_TRUNK 缺省、或恰好等于 KG_REPO，都退回独立的 `<KG_REPO>-trunk`。
    script = HERE / "central_daemon.py"
    env = {
        "KG_DAEMON_HOME": str(RUNTIME),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        # launchd 起的进程看不到交互 shell 里的 no_proxy，而 macOS 系统级 HTTP 代理
        # 开着时，codex 的 reqwest 会把打向本地模型网关的请求也送进代理，代理对
        # /v1/responses 回 502 —— 表现为每条 auto_exec 都 codex_exit_1。
        # 2026-10-14 实测：最小环境不加这两个变量 -> 502；加上 -> 正常返回。
        # 本地回环永远不该走代理，所以在这里硬钉住。
        "no_proxy": "127.0.0.1,localhost,::1",
        "NO_PROXY": "127.0.0.1,localhost,::1",
    }
    # KG_REPO 是账本所在；自包含安装下它和 KG_TRUNK 不一定同一个目录。
    # 运行时的模型分工由项目配置提供：慢卡长任务用 auto_exec.model，
    # 快速收敛用 auto_exec.fast_model。未配置时不写环境变量，保持旧行为。
    repo_env = os.environ.get("KG_REPO")
    trunk_env = os.environ.get("KG_TRUNK")
    try:
        repo_path = Path(repo_env).expanduser() if repo_env else _state_repo()
        cfg_data = json.loads((repo_path / "ops" / "central.config.json").read_text(encoding="utf-8"))
        acfg = cfg_data.get("auto_exec") or {}
        if isinstance(acfg, dict):
            if acfg.get("fast_model"):
                env["KG_EXEC_FAST_MODEL"] = str(acfg["fast_model"])
            if acfg.get("model"):
                env["KG_EXEC_MODEL"] = str(acfg["model"])
    except (OSError, ValueError, TypeError):
        pass
    if repo_env:
        repo_path = Path(repo_env).expanduser()
        env["KG_REPO"] = str(repo_path)
        trunk = Path(trunk_env).expanduser() if trunk_env else None
        if trunk is None or trunk.resolve() == repo_path.resolve():
            trunk = repo_path.parent / (repo_path.name + "-trunk")
        env["KG_TRUNK"] = str(trunk)
    elif trunk_env:
        env["KG_TRUNK"] = str(Path(trunk_env).expanduser())
    return {
        "Label": PLIST_LABEL,
        "ProgramArguments": [_python_for_plist(), str(script),
                             "--loop", "--interval", str(interval_min),
                             "--max-land", str(DEFAULT_MAX_LAND)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "Nice": int(os.environ.get("KG_DAEMON_NICE", "10")),
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": env,
    }


def launchctl(*args, capture: bool = False) -> int:
    """跑 launchctl。

    默认不 capture：`bootstrap` / `kickstart` 会拉起一个长期运行的 job，
    若用 capture_output，job 继承的管道写端不关，subprocess.run 会一直挂住
    （实测 --install 卡死）。查询类调用才显式 capture=True。
    """
    std = subprocess.PIPE if capture else subprocess.DEVNULL
    try:
        return subprocess.run(["launchctl", *args], stdin=subprocess.DEVNULL,
                              stdout=std, stderr=std, text=capture).returncode
    except OSError:
        return 127


def _launchctl_show(uid: int) -> str:
    try:
        p = subprocess.run(["launchctl", "print", f"gui/{uid}/{PLIST_LABEL}"],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout or ""


def _job_pid(uid: int):
    """查 launchd 里本 job 的 pid；没有就返回 None。"""
    m = re.search(r"pid = (\d+)", _launchctl_show(uid))
    return int(m.group(1)) if m else None


def wait_for_job_gone(uid: int, timeout: float = 30.0) -> bool:
    """bootout 是异步的：等旧实例真正退干净再 bootstrap，否则新实例抢不到锁会退出。

    旧进程收到 SIGTERM 后要跑完当前周期才退；最多等 timeout 秒。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _job_pid(uid) is None:
            return True
        time.sleep(1.0)
    return _job_pid(uid) is None


def install(interval_min: int) -> int:
    if sys.platform != "darwin":
        log("install 只支持 macOS（launchd）。其它平台请用 `--loop` 自己托管。")
        return 1
    trunk = Path(os.environ.get("KG_TRUNK", str(CODE_ROOT))).expanduser()
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PLIST_PATH.open("wb") as fh:
        plistlib.dump(build_plist(interval_min), fh)
    log(f"已写 {PLIST_PATH}")
    uid = os.getuid()
    launchctl("bootout", f"gui/{uid}/{PLIST_LABEL}")
    if not wait_for_job_gone(uid):
        log("× 旧实例在 30s 内没退干净；稍后重试 --install")
        return 1
    # bootstrap 带 RunAtLoad，会直接把进程拉起；不要用 kickstart：
    # kickstart -k 要等常驻进程退出才返回，会一直挂住（实测）。
    rc = launchctl("bootstrap", f"gui/{uid}", str(PLIST_PATH))
    if rc != 0:
        time.sleep(2.0)
        rc = launchctl("bootstrap", f"gui/{uid}", str(PLIST_PATH))
    if rc != 0:
        log("× launchctl bootstrap 失败；请检查上面的 plist 和 python 路径")
        return 1
    launchctl("enable", f"gui/{uid}/{PLIST_LABEL}")
    log(f"已启用 {PLIST_LABEL}（每 {interval_min} 分钟一轮，nice={os.environ.get('KG_DAEMON_NICE', '10')}）")
    return 0


def uninstall() -> int:
    uid = os.getuid()
    launchctl("bootout", f"gui/{uid}/{PLIST_LABEL}")
    wait_for_job_gone(uid)
    try:
        PLIST_PATH.unlink()
        log(f"已删除 {PLIST_PATH}")
    except OSError:
        pass
    release_lock()
    log(f"已停用 {PLIST_LABEL}")
    return 0


# ------------------------------------------------------------------ 状态

def open_resolve_tasks():
    """从状态根的 ops/tasks/resolve.json 读出**还没落地**的冲突任务。

    以前这里显示的是守护进程上一轮拆过谁（`state.last_resolved_tasks`），它一旦写进去
    就永不清空：一条冲突任务已经 `landed` 了，`--status` 还会一直把它列成「冲突任务」，
    看着像还有活没干完——这正是"状态信号不可信"的来源。改成实时读任务账本，只列
    status 还在进行中的（needs_agent / gate_fail 等），落盘或放弃的不再出现。
    """
    repo = _state_repo()
    if not repo:
        return []
    try:
        data = json.loads((repo / "ops" / "tasks" / "resolve.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tasks = data.get("tasks") or []
    settled = {t.get("slug") for t in tasks if t.get("status") in ("landed", "abandoned")}
    settled |= {t.get("branch") for t in tasks if t.get("status") in ("landed", "abandoned")}
    return [t for t in tasks
            if t.get("status") in ("needs_agent", "gate_fail")
            and Path(t.get("worktree") or "").is_dir()
            and t.get("slug") not in settled and t.get("branch") not in settled]


def show_status() -> int:
    state = load_state()
    uid = os.getuid()
    loaded = launchctl("print", f"gui/{uid}/{PLIST_LABEL}", capture=True) == 0
    lock = DAEMON_LOCK.read_text().strip() if DAEMON_LOCK.exists() else "(无)"
    print("=== 自动挡常驻（KgCentralDaemon）===")
    print(f"守护状态       {'launchd 已加载' if loaded else '未加载'}"
          f"{'' if loaded else '（用 --install 打开）'}")
    print(f"实例锁         {lock}")
    print(f"运行目录       {RUNTIME}")
    print(f"日志           {LOG}")
    print(f"通知流水       {NOTIFICATIONS}")
    if state:
        print(f"上一轮         {state.get('last_cycle_iso', '?')}"
              f"{'（dry-run）' if state.get('dry') else ''}")
        print(f"当前心跳       {state.get('heartbeat_iso', '未记录')} / {state.get('current_stage', '?')}")
        print(f"累计自动落盘   {state.get('total_landed', 0)} 个分支")
        print(f"累计归档重复   {state.get('total_superseded', 0)} 个分支")
        print(f"累计自动移植   {state.get('total_ported', 0)} 个分支")
        print(f"累计冲突拆分   {state.get('total_resolved', 0)} 条（含确定性并集落盘）")
        print(f"累计自动实现   {state.get('total_exec', 0)} 条需求（独立会话实现并落盘）")
        blocked = state.get("port_blocked") or {}
        if blocked:
            print(f"移植暂缓       {len(blocked)} 个分支（冲突/门禁不过，等 converge）")
        rblocked = state.get("resolve_blocked") or {}
        if rblocked:
            print(f"冲突暂缓       {len(rblocked)} 个分支（拆不动/门禁不过，已跳过）")
        eblocked = state.get("exec_blocked") or {}
        if eblocked:
            print(f"实现暂缓       {len(eblocked)} 条（无改动/门禁不过，已跳过，不再重复派）")
        workers = state.get("workers") or {}
        if workers:
            conv_active = workers.get("convergence_active") or []
            exec_active = workers.get("exec_active") or []
            resolve_active = bool(workers.get("resolve_active"))
            resolve_tasks = workers.get("resolve_task_active") or []
            resolve_finish = workers.get("resolve_finish_active") or []
            resolve_external = workers.get("resolve_external_active") or []
            resolve_label = "空闲"
            if resolve_active or resolve_tasks or resolve_finish:
                resolve_label = (f"运行中 {len(resolve_tasks) + len(resolve_finish)}"
                                 if resolve_tasks or resolve_finish else "拆冲突中")
            print(f"后台并行       convergence {len(conv_active)}/"
                  f"{workers.get('convergence_limit', '?')} · "
                  f"exec {len(exec_active)}/{workers.get('exec_limit', '?')} · "
                  f"resolve {resolve_label}")
            if resolve_tasks:
                print(f"  resolve任务  {', '.join(resolve_tasks)}")
            if resolve_external:
                print(f"  resolve外部  {', '.join(resolve_external)}")
            if resolve_finish:
                print(f"  resolve落盘  {', '.join(resolve_finish)}")
            finish_queue = state.get("finish_resolve_queue") or []
            if finish_queue:
                print(f"  resolve待收  {', '.join(finish_queue)}")
            if conv_active:
                print(f"  convergence  {', '.join(conv_active)}")
            if exec_active:
                print(f"  exec         {', '.join(exec_active)}")
            last_conv = workers.get("last_convergence") or []
            last_resolve = workers.get("last_resolve") or []
            last_exec = workers.get("last_exec") or []
            last_rt = state.get("last_resolve_tasks") or []
            last_rf = state.get("last_resolve_finish") or []
            if last_conv:
                print("  最近收敛     " + ", ".join(
                    f"{r.get('id') or '?'}:{r.get('outcome') or '?'}"
                    for r in last_conv[-3:]))
            if last_resolve:
                rr = last_resolve[-1]
                results = rr.get("results") or []
                detail = (", ".join(f"{x.get('branch')}:{x.get('outcome')}"
                                    for x in results[:3])
                          if results else "无结构化结果")
                print(f"  最近 resolve rc={rr.get('rc')}  {detail}")
            if last_exec:
                print("  最近实现     " + ", ".join(
                    f"{r.get('id') or '?'}:{r.get('outcome') or '?'}"
                    for r in last_exec[-3:]))
            if last_rt:
                print("  最近解冲突   " + ", ".join(
                    f"{r.get('slug') or '?'}:{r.get('outcome') or '?'}"
                    for r in last_rt[-3:]))
            if last_rf:
                print("  最近冲突落盘 " + ", ".join(
                    f"{r.get('slug') or '?'}:{r.get('outcome') or '?'}"
                    for r in last_rf[-3:]))
            print(f"  更新于       {workers.get('updated_iso', '?')}")
        rtasks = open_resolve_tasks()
        if rtasks:
            shown = ", ".join(f"{t.get('slug')} [{t.get('status')}]" for t in rtasks[:5])
            print(f"冲突任务       {shown}")
        last = state.get("last_landed") or []
        if last:
            print(f"最近落盘       {', '.join(last[:5])}")
        fails = state.get("last_failures") or []
        if fails:
            print(f"上轮失败       {fails}")
        s = state.get("last_summary") or {}
        if s:
            print(f"队列           merged={s.get('merged')} "
                  f"clean={s.get('clean')} conflict={s.get('conflict')}")
    else:
        print("上一轮         （还没跑过；用 --once 试跑一次）")
    return 0


# ------------------------------------------------------------------ 主循环

def _sigint(_s, _f):
    global _stop
    _stop = True
    log("收到停止信号，本轮结束后退出")


def loop(interval_min: int, max_land: int, dry: bool) -> int:
    if not acquire_lock():
        log(f"已有实例在跑（{DAEMON_LOCK}），本次退出")
        return 1
    # 上一进程可能崩溃或被 launchd 重启。不要假装子进程已经结束：明确的 codex
    # 子进程仍可能在 worktree 里写文件；此时重新派同一条会让两个 worker 抢同一分支。
    # 新实例先观察真实进程，把现场登记成外部活跃，再让各派发器跳过它。
    try:
        state = load_state()
        live = _live_exec_worktrees()
        state["workers"] = {
            "convergence_limit": max(1, int(convergence_exec_config().get(
                "max_workers", EXEC_MAX_WORKERS))),
            "exec_limit": max(1, int(auto_exec_config().get(
                "max_workers", EXEC_MAX_WORKERS))),
            "convergence_active": [],
            "resolve_active": False,
            "exec_active": [],
            "exec_external_active": [],
            "recovery_external": [
                {"worktree": wt, "pid": pid} for wt, pid in sorted(live.items())
            ],
            "updated_ts": time.time(),
            "updated_iso": _now(),
        }
        save_state(state)
    except Exception as exc:
        log(f"[worker-state] 初始化清理跳过：{exc!r}")
    signal.signal(signal.SIGTERM, _sigint)
    signal.signal(signal.SIGINT, _sigint)
    try:
        try:
            os.nice(int(os.environ.get("KG_DAEMON_NICE", "10")))
        except (OSError, ValueError):
            pass
        log(f"常驻启动 pid={os.getpid()} interval={interval_min}min "
            f"max_land/cycle={max_land}")
        while not _stop:
            heartbeat_lock()
            started = time.time()
            try:
                run_cycle(dry, max_land)
            except Exception as exc:  # 单轮失败不能让守护退出
                log(f"× 本轮异常（已吞掉，下轮继续）：{exc!r}")
            # 预算闸门（fail-open）：测量长驻窗口预算、按需自动换代、按需暂停派活。
            # 放在本轮 land/resolve 之后 —— 既有工作先收尾，闸门不插队。
            # 任何异常都当作「没有这个钩子」，绝不影响自动挡本体。
            try:
                import budget_gate
                budget_gate.daemon_hook(dry=dry, log_fn=log)
            except Exception as exc:
                log(f"[budget] 闸门跳过（fail-open）：{exc!r}")
            # 空转记分（fail-open）：给每一拍分类，连续空转就点名并把记分写进下一拍的 prompt。
            # 与预算闸门同族：只读判定、动作显式、一键关、异常一律吞掉。
            try:
                import beat_audit
                beat_audit.daemon_hook(dry=dry, log_fn=log)
            except Exception as exc:
                log(f"[beat] 记分跳过（fail-open）：{exc!r}")
            if _stop:
                break
            was_paused = control_state.is_paused()
            sleep_for = max(5, interval_min * 60 - (time.time() - started))
            log(f"下一轮 {sleep_for/60:.1f} 分钟后"
                f"{'（当前暂停中）' if was_paused else ''}")
            slept = 0.0
            while slept < sleep_for and not _stop:
                time.sleep(min(5.0, sleep_for - slept))
                slept += 5.0
                # 面板里恢复运行后，最多 5 秒就重新进入下一轮，不必等满整个周期。
                if was_paused and not control_state.is_paused():
                    log("[pause] 已恢复运行，提前进入下一轮")
                    break
    finally:
        if _conv_pool is not None:
            _conv_pool.shutdown(wait=False)
        if _exec_pool is not None:
            _exec_pool.shutdown(wait=False)
        if _resolve_task_pool is not None:
            _resolve_task_pool.shutdown(wait=False)
        release_lock()
        log("常驻退出")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="自动挡常驻执行器（安全子集：合无冲突分支 + 移植小改动 + 拆冲突为任务）")
    ap.add_argument("--once", action="store_true", help="跑一个周期就退出")
    ap.add_argument("--loop", action="store_true", help="常驻，按 --interval 循环")
    ap.add_argument("--dry-run", action="store_true", help="只预演，不真的 land")
    ap.add_argument("--interval", type=int, default=int(
        os.environ.get("KG_DAEMON_INTERVAL", DEFAULT_INTERVAL_MIN)),
        help=f"常驻周期，分钟（默认 {DEFAULT_INTERVAL_MIN}）")
    ap.add_argument("--max-land", type=int, default=int(
        os.environ.get("KG_DAEMON_MAX_LAND", DEFAULT_MAX_LAND)),
        help=f"每轮最多 land 几个（默认 {DEFAULT_MAX_LAND}）")
    ap.add_argument("--status", action="store_true", help="看守护 / 队列状态")
    ap.add_argument("--install", action="store_true", help="装成 launchd 守护并启用")
    ap.add_argument("--uninstall", action="store_true", help="停用并删除 launchd 守护")
    args = ap.parse_args()

    if args.install:
        return install(args.interval)
    if args.uninstall:
        return uninstall()
    if args.status:
        return show_status()
    if args.loop:
        return loop(args.interval, args.max_land, args.dry_run)
    # 默认（含 --once）：跑一个周期。
    run_cycle(args.dry_run, args.max_land)
    return 0


if __name__ == "__main__":
    sys.exit(main())
