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
import hashlib
import json
import os
import plistlib
import re
import shlex
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE_ROOT = HERE.parent
KGCTL = HERE / "kgctl.py"


from plugin_binding import apply_plugin_env  # noqa: E402

import central_config  # noqa: E402
import control_state  # noqa: E402
from ledger_state import latest_ledger_by_id  # noqa: E402
import workspace_guard  # noqa: E402

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

DEFAULT_INTERVAL_MIN = 15
DEFAULT_MAX_LAND = 3
LAND_TIMEOUT = 600
TICK_TIMEOUT = 1800
SUPERSEDE_TIMEOUT = 900
PORT_TIMEOUT = 90
# 没有候选的全量 port 扫描结果冷却多久；防止每 5 分钟周期都跑一次慢 git 扫描。
PORT_SCAN_COOLDOWN = 600
RESOLVE_TIMEOUT = 240
# 一个周期里最多尝试移植几条候选。每条候选要么落盘（走完整 build 门禁），要么撞
# 冲突被记进 port_blocked 后跳过；两种都会让下一轮候选集变小，所以循环会自然收敛。
PORT_MAX_ATTEMPTS = 6
# 一个周期最多拆几条冲突分支。每条先做确定性并集（只 jsonl）或留下一个可执行
# 任务书；拆不动 / 门禁没过记进 resolve_blocked，后续周期跳过，不再反复骚扰。
RESOLVE_MAX_ATTEMPTS = 1
# Disk pressure triggers a conservative lifecycle pass. The pass only removes
# merged worktrees and sufficiently old clean worktrees after a remote backup.
# It is low-frequency because scanning hundreds of Git worktrees is expensive.
REAP_MIN_INTERVAL_SECONDS = 6 * 60 * 60
REAP_DISK_PERCENT = 85
REAP_OLD_WORKTREE_DAYS = 2
# 自动实现（auto_exec）：把台账里还没做的待办派给真实 codex 会话，在独立 worktree
# 里并发实现 -> 串行门禁落盘 -> 收口。默认关闭；打开后按 max_workers 填满空闲槽位。
EXEC_DEFAULT_TIMEOUT = 3600
EXEC_MAX_PER_CYCLE = 1
EXEC_NO_CHANGE_RETRIES = 2
EXEC_DEFAULT_MAX_WORKERS = 10
# 基础设施抖动 vs 需求真做不出来：底层模型代理（127.0.0.1:15721）偶发 502 /
# Transport channel closed，会让 codex 进程以非 0 退出，但需求本身没问题。同一
# worktree 上指数退避重跑即可，默认 3 次；重试仍失败记 codex_infra_fail，与
# codex_exit_N 一眼区分开（L-0055 就是这么被误记成「做不出来」的）。
EXEC_INFRA_RETRIES = 3
EXEC_INFRA_BACKOFF = 20.0
# 只认这几种环境抖动特征；只看日志尾部若干字节，不把大日志整份读进来。
EXEC_INFRA_PATTERN = re.compile(
    r"502 Bad Gateway|Transport channel closed|ECONNRESET|connection refused"
    r"|429 Too Many Requests|exceeded retry limit, last status: 429",
    re.IGNORECASE)
EXEC_INFRA_TAIL_BYTES = 8192
# 模型通道预扣费/额度失败不是“需求做不出来”，也不适合在 fast 通道上反复退避。
# 检测到后再派一次慢通道（config 的 slow_model），仍失败才按基础设施失败保留现场。
EXEC_MODEL_FALLBACK_PATTERN = re.compile(
    r"403 Forbidden|insufficient balance|quota|额度|余额",
    re.IGNORECASE)
# 网关限流（429）是瞬时的：同一请求稍后重试即可。它必须和“额度耗尽”分开判，
# 否则一次限流就能把一批健康需求永久标成做不出来。
EXEC_THROTTLE_PATTERN = re.compile(
    r"429 Too Many Requests|exceeded retry limit, last status: 429",
    re.IGNORECASE)
# A gateway 429 is transient. Delay only that task before restarting it; this
# must never reduce the global worker limit or make the supervisor sleep while
# other workers could still be started/reaped.
EXEC_CHANNEL_RETRY_SECONDS = float(
    os.environ.get("KG_EXEC_CHANNEL_RETRY_SECONDS", "20"))

_stop = False


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
                                text=True, errors="replace", cwd=str(CODE_ROOT),
                                env=env, start_new_session=True)
    except OSError as exc:
        return 125, f"调用 kgctl 失败: {exc}"
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out = ""
        tail = (out or "").strip()
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


def _disk_percent(path: Path) -> float:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return 0.0
    return 100.0 * usage.used / usage.total if usage.total else 0.0


def maybe_reap(dry: bool = False) -> bool:
    """Run the conservative worktree lifecycle pass under disk pressure."""
    if os.environ.get("KG_AUTO_REAP", "1").lower() in ("0", "false", "off", "no"):
        return False
    state = load_state()
    now = time.time()
    last = float(state.get("last_reap_ts") or 0)
    if now - last < REAP_MIN_INTERVAL_SECONDS:
        return False
    if _disk_percent(RUNTIME) < REAP_DISK_PERCENT:
        return False
    if dry:
        log(f"[reap] disk {_disk_percent(RUNTIME):.1f}% exceeds threshold; dry-run")
        return True
    log(f"[reap] disk {_disk_percent(RUNTIME):.1f}% exceeds threshold; reaping")
    state["last_reap_ts"] = now
    save_state(state)
    rc, out = run_kgctl(
        ["reap", "--go", "--backup-unique", "--min-age-days",
         str(REAP_OLD_WORKTREE_DAYS)],
        timeout=TICK_TIMEOUT,
    )
    log_block("reap", rc, out)
    return rc == 0


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
    out = {"enabled": True, "max_files": 8, "max_lines": 1200}
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
    if isinstance(cfg, bool):
        out["enabled"] = cfg
    elif isinstance(cfg, dict):
        out["enabled"] = bool(cfg.get("enabled", True))
        for key in ("max_files", "max_lines"):
            try:
                out[key] = max(1, int(cfg.get(key, out[key])))
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

QUEUE_RE = re.compile(r"^\s*\d+\.\s+kgctl land (\S+).*files=(\d+)")
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
    files_by_branch = {}
    for line in out.splitlines():
        m = QUEUE_RE.match(line)
        if m:
            files_by_branch[m.group(1)] = int(m.group(2) or 0)
    summary = {"other": 0, "merged": 0, "conflict": 0, "unresolved": 0}
    m = SUMMARY_RE.search(out)
    if m:
        summary = {"other": int(m.group(1)), "merged": int(m.group(2)),
                   "conflict": int(m.group(3)), "unresolved": 0}
    m = UNRESOLVED_RE.search(out)
    if m:
        summary["unresolved"] = int(m.group(1))
    return branches, summary, files_by_branch


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
           "no_change_retries": EXEC_NO_CHANGE_RETRIES,
           "max_workers": EXEC_DEFAULT_MAX_WORKERS,
           "timeout": EXEC_DEFAULT_TIMEOUT, "sandbox": "workspace-write",
           "add_dirs": [],
           "model": None, "fast_model": None, "slow_model": None,
           "reasoning_effort": "medium",
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
            for key in ("max_per_cycle", "max_workers", "timeout"):
                try:
                    out[key] = max(1, int(cfg.get(key, out[key])))
                except (TypeError, ValueError):
                    pass
            try:
                out["retries"] = max(0, int(cfg.get("retries", out["retries"])))
            except (TypeError, ValueError):
                pass
            try:
                out["no_change_retries"] = max(0, int(
                    cfg.get("no_change_retries", out["no_change_retries"])))
            except (TypeError, ValueError):
                pass
            if cfg.get("sandbox"):
                out["sandbox"] = str(cfg["sandbox"])
            if isinstance(cfg.get("add_dirs"), list):
                out["add_dirs"] = [str(x) for x in cfg.get("add_dirs") if str(x).strip()]
            elif isinstance(cfg.get("add_dirs"), str):
                out["add_dirs"] = [x for x in cfg["add_dirs"].split(":") if x.strip()]
            if cfg.get("model"):
                out["model"] = str(cfg["model"])
            if cfg.get("fast_model"):
                out["fast_model"] = str(cfg["fast_model"])
            if cfg.get("slow_model"):
                out["slow_model"] = str(cfg["slow_model"])
            if cfg.get("reasoning_effort"):
                out["reasoning_effort"] = str(cfg["reasoning_effort"])
    # The launchd environment is the last-resort model override. Keeping it
    # here as well as in central.config.json makes an old installed config
    # unable to silently fall back to the general Codex model.
    if os.environ.get("KG_EXEC_FAST_MODEL"):
        out["fast_model"] = os.environ["KG_EXEC_FAST_MODEL"]
    if os.environ.get("KG_EXEC_MODEL"):
        out["slow_model"] = os.environ["KG_EXEC_MODEL"]
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


def _git(repo, *args, timeout=300):
    """跑一条 git 命令，返回 (rc, stdout, stderr)。"""
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _worktree_for_branch(repo, branch: str):
    """返回某个分支当前登记的 worktree 路径；没有则 None。

    分支可能挂在任何 worktree 上（老版本留下的是 kg-trunk-exec-*，新版本是
    kg-<状态根>-exec-*）。只按命名规则推算会漏掉老目录里的在途成果。
    """
    rc, out, _ = _git(repo, "worktree", "list", "--porcelain", timeout=30)
    if rc != 0:
        return None
    def _norm(ref: str) -> str:
        ref = (ref or "").strip()
        for pfx in ("refs/heads/", "refs/"):
            if ref.startswith(pfx):
                return ref[len(pfx):]
        return ref
    want = _norm(branch)
    current = None
    for line in (out or "").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):].strip()
        elif line.startswith("branch ") and current:
            # `git worktree list --porcelain` 给的是 refs/heads/<name>，
            # 直接和 codex/<name> 比永远不会相等 —— 老目录里的在途成果因此
            # 全被判成「不存在」，只能等 6 小时派发上限过期。
            if _norm(line[len("branch "):]) == want:
                return Path(current)
    return None


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
    """台账里每个 id 按时间收敛后的当前记录。目录/文件不可读时返回 None。

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
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return latest_ledger_by_id(rows)


def _ledger_open_items(limit=20):
    """台账里还没关闭、也还没派过的待办。排序：bug / gate 优先，其次按时间。"""
    latest = _ledger_latest() or {}
    opens = [r for r in latest.values() if r.get("status") in ("open", "doing")]
    opens.sort(key=lambda r: (0 if r.get("kind") in ("bug", "gate") else 1, r.get("ts", "")))
    return opens[:limit]


def _resolve_tasks_path(repo=None):
    repo = repo or _state_repo()
    if not repo:
        return None
    return Path(repo) / "ops" / "tasks" / "resolve.json"


def _resolve_task(slug):
    p = _resolve_tasks_path()
    if not p or not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for rec in data.get("tasks") or []:
        if rec.get("slug") == slug:
            return rec
    return None


def _set_resolve_status(repo, slug, status):
    p = _resolve_tasks_path(repo)
    if not p or not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for rec in data.get("tasks") or []:
        if rec.get("slug") == slug:
            rec["status"] = status
            rec["updated"] = _now()
            break
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def _resolve_worktree_dirty(wt: Path) -> bool:
    if not wt or not wt.exists():
        return True
    try:
        out = subprocess.run(
            ["git", "-C", str(wt), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return bool((out.stdout or "").strip())


def _resolve_worktree_clean_for_finish(item: dict) -> bool:
    """True when a resolve row has committed work that only needs the finish gate.

    A resolve worker may commit the resolved files and leave the worktree
    clean while the ledger row is still needs_agent/gate_fail. Starting a new
    Codex session then redoes the same conflict; the finish path reuses the
    committed branch and goes through the normal land gate instead.
    """
    if not item or item.get("kind") != "resolve":
        return False
    repo = _state_repo()
    if not repo:
        return False
    rec = _resolve_task(item.get("slug") or "")
    if not rec:
        return False
    if _resolve_worktree_has_markers(rec):
        return False
    wt = Path(rec.get("worktree") or "")
    branch = rec.get("resolve_branch") or ""
    if not wt.exists() or not branch:
        return False
    if _resolve_worktree_dirty(wt) or _resolve_worktree_owned(wt):
        return False
    main_branch = (central_config.load(repo) or {}).get("main_branch", "main")
    return _branch_pending_commits(repo, branch, main_branch)


def _resolve_worktree_owned(wt: Path) -> bool:
    """Has an external agent process already opened this resolve worktree?"""
    if not wt or not wt.exists():
        return True
    target = str(wt.resolve())
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return True
    for line in (out.stdout or "").splitlines():
        if target in line:
            return True
    return False


def _resolve_auto_allowed(item: dict) -> bool:
    """Resolve tasks explicitly awaiting a product decision must stay untouched."""
    slug = item.get("slug") or ""
    rec = _resolve_task(slug) if slug else None
    if not rec:
        return True
    reason = rec.get("blocked_reason") or ""
    return ("do NOT auto-resolve" not in reason
            and "awaiting product decision" not in reason)


def _resolve_task_item(slug):
    rec = _resolve_task(slug)
    if not rec or rec.get("status") not in ("needs_agent", "gate_fail"):
        return None
    wt = rec.get("worktree") or ""
    if not wt or not Path(wt).exists():
        return None
    files = rec.get("unresolved_files") or []
    return {
        "id": f"resolve:{slug}",
        "kind": "resolve",
        "slug": slug,
        "title": f"解决冲突任务 {slug}",
        "detail": (
            f"源分支：{rec.get("branch")}；执行分支：{rec.get("resolve_branch")}；"
            f"工作树：{wt}；待解文件：{chr(44).join(files) or "无"}。保留冲突两侧的真实意图，不要机械二选一。"
        ),
        "files": files,
        "resolve_branch": rec.get("resolve_branch"),
        "worktree": wt,
        "branch": rec.get("resolve_branch"),
        "repo": str(_state_repo() or ""),
    }


def _resolve_open_items(limit=10):
    repo = _state_repo()
    if not repo:
        return []
    p = _resolve_tasks_path(repo)
    if not p or not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    settled = {t.get("slug") for t in data.get("tasks") or []
               if t.get("status") in ("landed", "abandoned")}
    settled |= {t.get("branch") for t in data.get("tasks") or []
                if t.get("status") in ("landed", "abandoned")}
    out = []
    for rec in data.get("tasks") or []:
        if rec.get("status") not in ("needs_agent", "gate_fail"):
            continue
        wt = Path(rec.get("worktree") or "")
        if not wt.exists():
            continue
        if _resolve_worktree_owned(wt):
            continue
        # A merge conflict leaves the resolve worktree dirty by definition.
        # If the previous worker died, treating every dirty worktree as owned
        # strands the task forever even though its conflict markers are still
        # present. Re-dispatch marker-bearing work, but leave dirty tasks whose
        # markers are gone to the finish gate so we do not overwrite a solved
        # worktree with another model run.
        if _resolve_worktree_dirty(wt) and not _resolve_worktree_has_markers(rec):
            continue
        if rec.get("slug") in settled or rec.get("branch") in settled:
            continue
        item = _resolve_task_item(rec.get("slug"))
        if item:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _resolve_ready_items(limit=10):
    """Resolve worktrees with markers removed are ready to finish.

    Codex resolve workers are told to leave changes uncommitted; the daemon
    normally skips dirty worktrees, so those finished works never enter the
    finish gate. Re-admit dirty worktrees whose unresolved files no longer
    contain markers, and let the normal commit + resolve finish path land them.
    """
    repo = _state_repo()
    if not repo:
        return []
    p = _resolve_tasks_path(repo)
    if not p or not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    settled = {t.get("slug") for t in data.get("tasks") or []
               if t.get("status") in ("landed", "abandoned")}
    settled |= {t.get("branch") for t in data.get("tasks") or []
                if t.get("status") in ("landed", "abandoned")}
    for rec in data.get("tasks") or []:
        if rec.get("status") not in ("needs_agent", "gate_fail"):
            continue
        if rec.get("slug") in settled or rec.get("branch") in settled:
            continue
        wt = Path(rec.get("worktree") or "")
        if not wt.exists():
            continue
        if _resolve_worktree_owned(wt):
            continue
        files = rec.get("unresolved_files") or []
        if any(_has_conflict_markers(wt, f) for f in files):
            continue
        if not files:
            continue
        unresolved = subprocess.run(
            ["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
            capture_output=True, text=True, timeout=15)
        if (unresolved.stdout or "").strip():
            continue
        item = _resolve_task_item(rec.get("slug"))
        if item:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _has_conflict_markers(wt: Path, relative_path: str) -> bool:
    if not wt or not relative_path:
        return False
    path = Path(wt) / relative_path
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return any(marker in text for marker in ("<<<<<<< ", ">>>>>>> ", "======="))


def _resolve_worktree_has_markers(rec: dict) -> bool:
    """True when a resolve task's worktree still contains conflict markers.

    Markers may be committed as a WIP step before an agent resolves them, so
    runnable state is not the same as resolved state.
    """
    if not rec:
        return False
    wt = Path(rec.get("worktree") or "")
    files = rec.get("unresolved_files") or []
    return any(_has_conflict_markers(wt, f) for f in files)


def _exec_open_items(limit=20):
    seen = set()
    for item in _ledger_open_items(limit):
        seen.add(item.get("id"))
        yield item
    for item in _resolve_open_items(limit):
        if item.get("id") not in seen:
            seen.add(item["id"])
            yield item
    for item in _resolve_ready_items(limit):
        if item.get("id") not in seen:
            seen.add(item["id"])
            yield item


def _main_contains(commit: str) -> bool:
    """Read-only ancestry check used to detect false 'land failed' records."""
    repo = _state_repo()
    if not repo:
        return False
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, "main"],
            capture_output=True, text=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


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
    # Resolve tasks live in resolve_tasks.json, not always in the ordinary
    # ledger while they are gate_fail/needs_agent. Treating them as stale would
    # erase a 6h human-decision hold on every daemon restart.
    try:
        open_ids |= {
            f"resolve:{rec.get('slug')}" for rec in _resolve_records_all()
            if rec.get("slug") and rec.get("status") in ("needs_agent", "gate_fail")
        }
    except Exception as exc:
        log(f"[exec] 读取未决 resolve 任务失败（保留现有暂缓）：{exc!r}")
    stale = [k for k in list(blocked) if k not in open_ids]
    for k in stale:
        blocked.pop(k, None)
    # 派发计数在窗口内回落后，旧 retry_cap 不再代表真实阻塞。之前计数被人为
    # 清掉（或 6 小时自然过期）后 blocked 仍会从旧 state 写回，导致任务永远
    # 不再派发。这里让 blocked 跟随 exec_attempts.json 的真实计数自愈。
    try:
        attempts_path = RUNTIME / "exec_attempts.json"
        if attempts_path.exists():
            attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
            now = time.time()
            for rid, rec in list(blocked.items()):
                if not isinstance(rec, dict) or rec.get("reason") != "exec_retry_cap":
                    continue
                rec_ts = (attempts.get(rid) or [])
                window = [t for t in rec_ts if now - float(t) < EXEC_REPEAT_WINDOW]
                if len(window) < EXEC_REPEAT_CAP:
                    blocked.pop(rid, None)
                    stale.append(rid)
                    log(f"[exec] {rid} 派发计数已回落（{len(window)}/{EXEC_REPEAT_CAP}），"
                        f"清掉旧的自动重派上限")
    except Exception as exc:
        log(f"[exec] 清理旧 retry_cap 失败（忽略）：{exc!r}")
    # 已经留下在途成果的需求不需要新会话，只走收口门禁。旧版本会把这类
    # 「会话跑完了、但收口时守护崩了」的情况也算成一次失败派发，于是
    # 做完的活被 exec_retry_cap 永久锁住。这里按真实现场自愈放行。
    try:
        _repo = _state_repo()
        if _repo:
            _cfg = auto_exec_config()
            for rid, rec in list(blocked.items()):
                if not isinstance(rec, dict):
                    continue
                if rec.get("reason") not in ("exec_retry_cap", "worktree_fail",
                                             "worktree_preserved", "start_fail"):
                    continue
                _item = (latest.get(rid) or {})
                if not _item:
                    continue
                if _exec_has_inflight_work(_item, _cfg, _repo):
                    blocked.pop(rid, None)
                    stale.append(rid)
                    log(f"[exec] {rid} 已有在途成果，清掉旧的派发上限/建现场失败，走收口门禁")
    except Exception as exc:
        log(f"[exec] 在途成果放行失败（忽略）：{exc!r}")
    # land_locked / land_error 是瞬时状态（前一条落盘还在跑、或 fetch 抖了一下）。
    # 它们不代表「这活做不成」，留在 blocked 里会让已完成的分支永远不再进落盘。
    try:
        _repo2 = _state_repo()
        if _repo2:
            for rid, rec in list(blocked.items()):
                if not isinstance(rec, dict):
                    continue
                if str(rec.get("reason") or "") not in ("land_locked", "land_error",
                                                        "no_changes"):
                    continue
                _reason = str(rec.get("reason") or "")
                if _reason == "no_changes":
                    blocked.pop(rid, None)
                    stale.append(rid)
                    log(f"[exec] {rid} 上一拍没有产出（{_reason}），放行重试")
                    continue
                _b = str(rec.get("branch") or "")
                if _b and _branch_pending_commits(_repo2, _b, "main"):
                    blocked.pop(rid, None)
                    stale.append(rid)
                    log(f"[exec] {rid} 先前的落盘锁/网络抖动已过，分支仍有未落提交，放行重试")
    except Exception as exc:
        log(f"[exec] 清理瞬时落盘阻塞失败（忽略）：{exc!r}")
    # 「同一组冲突需人工裁决」的停手有期限：到期放行一次，免得改完代码也永远不动。
    now_ts = time.time()
    for rid, rec in list(blocked.items()):
        if not isinstance(rec, dict) or rec.get("reason") != "resolve_conflict_needs_decision":
            continue
        try:
            ts = time.mktime(time.strptime(str(rec.get("ts") or ""), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        if now_ts - ts >= RESOLVE_CONFLICT_BLOCK_H * 3600:
            blocked.pop(rid, None)
            log(f"[safety] {rid} 冲突停手已满 {RESOLVE_CONFLICT_BLOCK_H}h，允许再试一次")
    for rid, rec in list(blocked.items()):
        if not isinstance(rec, dict) or rec.get("reason") != "worktree_preserved":
            continue
        wt = str(rec.get("worktree") or "").strip()
        if wt and not Path(wt).exists():
            blocked.pop(rid, None)
            stale.append(rid)
            log(f"[exec] {rid} worktree gone, branch kept, clearing stale block")
    # A previous land may have reached main while close/network failed. Once the
    # commit is an ancestor of main, the work is landed and the stale block would
    # otherwise suppress that item forever.
    repo = _state_repo()
    if repo:
        for rid, rec in list(blocked.items()):
            if rid not in open_ids or not isinstance(rec, dict):
                continue
            commit = str(rec.get("commit") or "").strip()
            if not commit:
                continue
            rc, _, _ = _git(repo, "merge-base", "--is-ancestor", commit, "main")
            if rc != 0:
                continue
            close_rc, _ = _run_truthctl(
                ["close", rid, "auto-close: commit already in main"],
                extra_env={"KG_TRUTH_COMMIT": commit})
            if close_rc == 0:
                blocked.pop(rid, None)
                stale.append(rid)
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


def _resolve_exec_prompt(item: dict) -> str:
    slug = item.get("slug") or ""
    files = item.get("files") or []
    lines = [
        "你是 Codex 自动挡的冲突执行者。下面是一条已经拆好的冲突任务，请在当前 worktree 里把它解干净。",
        "",
        f"冲突任务 [{item.get('id')}]：{item.get('title') or ''}",
        "",
        "目标：保留冲突两侧的真实意图，不要机械二选一；除非能证明一侧能力已被另一侧同义替代，否则不删任何一方独有语义。",
    ]
    if item.get("detail"):
        lines += ["", "任务说明：", str(item["detail"])]
    if files:
        lines += ["", "待解文件：" + "、".join(str(f) for f in files)]
    lines += [
        "",
        "硬规则（必须遵守）：",
        "- 只改当前 worktree 里自己的文件；不 `git checkout .` / `git reset --hard` / `git stash` / `git add -A`。",
        "- 不要 `git commit`、不要 `git push`、不要合并分支：把改动留在工作区即可，落盘由自动挡统一走门禁。",
        "- 如果 `apply_patch` 报 `The first line of the patch must be '*** Begin Patch'`，不要反复重试；改用 `apply_patch` heredoc 的标准文本形式，或做带 `count == 1` 断言的精确字符串替换，然后立刻 `git diff --check` 与相关编译验证。",
        "- 解完 marker 后跑与改动相关的验证（如 `npx tsc --noEmit`、`npm run build`、相关 `verify:*` 脚本）；",
        "  失败就继续修，直到通过或有明确的外部阻塞。",
        "- 做完用一句话总结：改了哪些文件、跑了什么验证、结果如何。",
    ]
    return "\n".join(lines)


def _exec_prompt(item: dict) -> str:
    if item.get("kind") == "resolve":
        return _resolve_exec_prompt(item)
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
        "- 对直接、可验证的小任务（写文件、修文本、补配置等），第一步就要在当前 worktree 产出改动：",
        "  先执行实际命令（如 `printf '...' > 文件`、修改文本、补配置），再用命令自证；",
        "  只读检查不算完成，不能以“先看看 / 我先确认 / 发现已有”结束。",
        "- 如果该需求确实无需改动（例如功能已存在），必须用一条命令或读取结果证明现状满足需求，",
        "  并在总结里写清“无需改动 + 证明命令 + 现状”，否则一律按未完成处理。",
        "- 如果 `apply_patch` 报 `The first line of the patch must be '*** Begin Patch'`，不要反复重试；改用 `apply_patch` heredoc 的标准文本形式，或做带 `count == 1` 断言的精确字符串替换，然后立刻 `git diff --check` 与相关编译验证。",
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


def _nested_exec_worktrees(wt: Path) -> list:
    if not wt.is_dir():
        return []
    out = []
    try:
        for child in sorted(wt.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not (child / ".git").is_dir():
                continue
            rc, changed, _ = _git(child, "status", "--porcelain")
            if rc == 0 and changed.strip():
                out.append(str(child))
    except OSError:
        pass
    return out


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


def classify_exec_infra_failure(log_path) -> bool:
    """日志尾部命中基础设施抖动特征 -> 这次失败可重试，不是需求做不出来。

    底层模型代理（127.0.0.1:15721）偶发 502 Bad Gateway / Transport channel
    closed / ECONNRESET / connection refused 会让 codex 进程以非 0 退出，但需求
    本身没问题（L-0055 实测：代理 16:00:52 已恢复 200）。这类失败在同一 worktree 上
    重跑就能过，不该被记成 codex_exit_1 永久暂缓。

    只看日志尾部，宁可漏判（当普通失败处理）也不整份读大日志。
    """
    return bool(EXEC_INFRA_PATTERN.search(_exec_log_tail(log_path)))


def classify_exec_model_fallback(log_path) -> bool:
    """日志尾部是否命中预扣费 / 额度失败文案。"""
    text = _exec_log_tail(log_path)
    marker = "===== auto_exec 模型通道额度失败"
    marker_at = text.rfind(marker)
    if marker_at >= 0:
        text = text[marker_at + len(marker):]
    return bool(EXEC_MODEL_FALLBACK_PATTERN.search(text))


def _should_fallback_to_slow_model(job: dict, cfg: dict, log_path: Path) -> bool:
    """Return whether a provider-balance failure should retry on slow_model."""
    return bool(
        not job.get("model_fallback_used")
        and cfg.get("slow_model")
        and job.get("model") != cfg["slow_model"]
        and classify_exec_model_fallback(log_path)
    )


def classify_exec_throttled(log_path) -> bool:
    """日志尾部是否命中网关限流（429）。

    限流和额度耗尽不同：额度耗尽要换通道，限流只要等一会儿重试。
    两者都不能算“这条需求做不出来”，否则健康任务会被限流瞬间封死。
    """
    return bool(EXEC_THROTTLE_PATTERN.search(_exec_log_tail(log_path)))


def _channel_retry_due(rec: dict) -> bool:
    """Return whether a transient channel block may restart its worker."""
    try:
        retry_at = float((rec or {}).get("retry_after_ts") or 0)
    except (TypeError, ValueError):
        retry_at = 0
    return time.time() >= retry_at


def _defer_exec_channel_failure(
        rid: str, blocked: dict, *, branch: str = "", worktree: str = "",
        error: str = "channel_unavailable", detail: str = "") -> dict:
    """Record a provider/channel failure as resumable, not as a dead task."""
    release_attempt_slot(rid)
    rec = {
        "reason": "channel_unavailable",
        "error": error,
        "branch": branch,
        "worktree": worktree,
        "retry_after_ts": time.time() + max(0.0, EXEC_CHANNEL_RETRY_SECONDS),
        "restart_worker": True,
        "detail": (detail or "")[:400],
        "ts": _now(),
    }
    blocked[rid] = rec
    log(f"[exec] {rid} 模型通道暂时不可用（{error}），"
        f"{max(0.0, EXEC_CHANNEL_RETRY_SECONDS):.0f}s 后在同一现场重试；"
        "不记 codex_fail，不压低全局并发")
    return rec


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


def _exec_model(item: dict, cfg: dict) -> str | None:
    """Pick a predictable model per task: quick bug/gate work uses the fast lane.

    The old configuration only had one model and left it empty; the launchd
    process therefore fell back to the general Codex model. That made worker
    behaviour impossible to reason about and caused the wrong latency budget.
    """
    if cfg.get("model"):
        return str(cfg["model"])
    # 29 号离线批次把「文档、审计、只读盘点」这类不赶交互的任务放到慢通道，
    # 避免把 aibvv 快速额度烧在低时敏任务上。命中关键词才走慢卡，否则保持
    # 原有行为：bug/gate/resolve 走 fast_model，其它走 slow_model。
    title = str(item.get("title") or "")
    slug = str(item.get("slug") or item.get("id") or "")
    hay = f"{title}\n{slug}".lower()
    offline_markers = (
        "29号离线", "离线可并行", "只读", "审计", "盘点", "草案",
        "官网结构", "marketing", "docs/29-oct", "source 失败账本",
        "失败账本", "统计", "不依赖", "不发布",
    )
    if any(m in hay for m in offline_markers) and cfg.get("slow_model"):
        return str(cfg["slow_model"])
    # visual 也是「要跟用户来回几轮」的活：界面改一版就被退回重改一次，
    # 卡在慢通道上时每一轮都要重新排队，比 bug 类更吃交互延迟。
    # 2026-10-23 实测 L-0030 / L-0031 两条移动端视觉任务被分到慢卡，
    # 用户当场看到的现象就是「UI 改动半天没反应」。
    # ux 与 visual 是同一类「界面体验」活，都要跟用户来回改稿，一并走快卡。
    fast_kinds = ("bug", "gate", "resolve", "visual", "ux")
    if (item.get("kind") or "") in fast_kinds and cfg.get("fast_model"):
        return str(cfg["fast_model"])
    return str(cfg.get("slow_model") or cfg.get("fast_model") or "") or None


def _prepare_resolve_exec(item: dict, cfg: dict, blocked: dict) -> dict:
    """Prepare a Codex worker inside the resolve worktree already registered by kgctl."""
    slug = item.get("slug") or ""
    rec = _resolve_task(slug)
    if not rec:
        blocked[item["id"]] = {"reason": "resolve_missing", "ts": _now()}
        return {"id": item["id"], "outcome": "resolve_missing"}
    wt = Path(rec.get("worktree") or "")
    branch = rec.get("resolve_branch") or ""
    if not wt.exists() or not branch:
        blocked[item["id"]] = {"reason": "resolve_missing", "ts": _now()}
        return {"id": item["id"], "outcome": "resolve_missing"}
    codex = _codex_bin()
    if not codex:
        blocked[item["id"]] = {"reason": "no_codex", "ts": _now()}
        return {"id": item["id"], "outcome": "no_codex"}
    log_path = _exec_dir() / f"resolve-{_exec_slug(slug)}.log"
    model = _exec_model(item, cfg)
    return {"id": item["id"], "outcome": "ready", "item": item, "cfg": cfg,
            "repo": str(_state_repo() or ""), "branch": branch, "worktree": str(wt),
            "codex": codex, "log": str(log_path), "model": model,
            "started_at": _now(), "recover": False,
            "result": {"id": item["id"], "branch": branch, "worktree": str(wt)}}


def _exec_target(item: dict, cfg: dict, repo) -> tuple:
    """一条需求对应的 (branch, worktree, slug)。

    _prepare_exec 建现场、派发限额判断「是不是已有在途成果」都用这一套命名；
    两边各写一份会让限额闸门看到的工作区和真实要用的对不上。
    """
    rid = item.get("id") or "?"
    prefix = cfg.get("branch_prefix", "codex/")
    slug = f"{_exec_slug(rid)}-{_exec_slug(item.get('title') or '')}"
    branch = f"{prefix}exec-{slug}"
    wt = Path(repo).parent / f"{Path(repo).name}-exec-{slug}"
    return branch, wt, slug


def _exec_has_inflight_work(item: dict, cfg: dict, repo) -> bool:
    """这条需求是否已经有在途成果（真分支 + 未提交改动或未落提交）。

    只读判断，不动任何文件。用途：已在途的成果只需走收口门禁，不会再拉起
    新的 Codex 会话，因此不该被「同一需求派了太多次」的上限挡住。
    """
    if not repo:
        return False
    try:
        conf = central_config.load(repo)
        main_branch = conf.get("main_branch", "main")
        _branch, wt, _slug = _exec_target(item, cfg, repo)
        real_wt = wt if wt.exists() else (_worktree_for_branch(repo, _branch) or wt)
        if real_wt.exists() and _changed_paths(real_wt):
            return True
        if _branch_pending_commits(repo, _branch, main_branch):
            return True
    except Exception:
        return False
    return False


def _prepare_exec(item: dict, cfg: dict, blocked: dict,
                  force_session: bool = False) -> dict:
    """Create the isolated worktree for one worker without waiting for it.

    Returning a job description separates worker startup from worker
    completion. The daemon can fill several slots in one cycle while keeping
    a single, separate ``land`` critical section.
    """
    rid = item.get("id") or "?"
    repo = _state_repo()
    if not repo:
        return {"id": rid, "outcome": "no_repo"}
    conf = central_config.load(repo)
    main_branch = conf.get("main_branch", "main")
    prefix = conf.get("branch_prefix", "codex/")
    branch, wt, slug = _exec_target(item, conf, repo)
    # 旧实现一见 worktree / 分支已存在就永久记 leftover，且自己从不清理残留，于是任何
    # 首次收口没走完的需求都会永远卡住（L-0029/L-0030 反复 leftover 的结构根因）。
    # 改为非破坏性自愈：有在途改动、或分支上已有未落提交，都接着走同一道门禁收口；
    # 只有真正没有任何成果（无未提交改动且分支不领先 main）的残留才清掉重建。
    # 绝不丢未提交改动，也绝不删掉已经提交、只是没走完落盘的那笔提交。
    existing = _git(repo, "rev-parse", "--verify", branch)[0] == 0 or wt.exists()
    # 分支可能还登记在别的路径（例如老版本只把旧的 kg-trunk-* worktree 留了下来）。
    # 直接沿用登记在案的现场，而不是用新路径重建/删除；在途改动和未落提交都不丢。
    existing_wt = wt if wt.exists() else (_worktree_for_branch(repo, branch) or wt)
    existing |= existing_wt.exists()
    # A 429/provider failure preserves the worktree but invalidates the
    # session. Restart Codex in that same worktree so partial files are visible
    # to the next worker, while recover=True remains reserved for work that is
    # already delivered and only needs landing.
    if force_session and _git(repo, "rev-parse", "--verify", branch)[0] == 0:
        if not existing_wt.exists():
            _git(repo, "worktree", "prune")
            _rc, _out, _err = _git(repo, "worktree", "add", str(existing_wt), branch)
            if _rc != 0:
                blocked[rid] = {"reason": "worktree_preserved",
                                "worktree": str(existing_wt), "branch": branch,
                                "ts": _now()}
                log(f"[exec] {rid} 限流后重挂 worktree 失败：{_err[:200]}")
                return {"id": rid, "outcome": "worktree_preserved",
                        "branch": branch, "worktree": str(existing_wt)}
            _link_side_dirs(existing_wt, conf)
        codex = _codex_bin()
        if not codex:
            blocked[rid] = {"reason": "no_codex", "ts": _now()}
            return {"id": rid, "outcome": "no_codex"}
        log(f"[exec] {rid} 通道退避结束，沿用原 worktree 重开会话（不按 recover 直接落盘）")
        return {"id": rid, "outcome": "ready", "item": item, "cfg": cfg,
                "repo": str(repo), "branch": branch, "worktree": str(existing_wt),
                "codex": codex, "log": str(_exec_dir() / f"{slug}.log"),
                "model": _exec_model(item, cfg), "started_at": _now(),
                "recover": False,
                "result": {"id": rid, "branch": branch,
                           "worktree": str(existing_wt)}}
    # If a leftover was cleaned and recreated, it is no longer an in-flight
    # branch and must get a fresh worker. Returning the stale `existing` flag
    # here made every rebuilt slot start as recover=True, so no codex session
    # was ever spawned and the pool spun without doing work.
    changed_in_flight = _changed_paths(existing_wt) if existing_wt.exists() else []
    if existing and (changed_in_flight or _branch_pending_commits(repo, branch, main_branch)):
        return {"id": rid, "outcome": "ready", "item": item, "cfg": cfg,
                "repo": str(repo), "branch": branch, "worktree": str(existing_wt),
                "log": str(_exec_dir() / f"{slug}.log"), "recover": True,
                "result": {"id": rid, "branch": branch, "worktree": str(existing_wt)}}
    if existing:
        if not _knob("reap_worktrees", True):
            if not existing_wt.exists():
                # The branch survives but its worktree directory is gone
                # (collected by an older cycle). Re-attach a worktree to the
                # same branch instead of blocking dispatch forever on a
                # phantom path; nothing is deleted.
                _git(repo, "worktree", "prune")
                _rc, _out, _err = _git(repo, "worktree", "add", str(existing_wt), branch)
                if _rc == 0:
                    log(f"[exec] {rid} 复用保留分支 {branch}（原 worktree 已不存在）")
                    _link_side_dirs(existing_wt, conf)
                    _codex2 = _codex_bin()
                    if not _codex2:
                        blocked[rid] = {"reason": "no_codex", "ts": _now()}
                        return {"id": rid, "outcome": "no_codex"}
                    return {"id": rid, "outcome": "ready", "item": item,
                            "cfg": cfg, "repo": str(repo), "branch": branch,
                            "worktree": str(existing_wt), "codex": _codex2,
                            "log": str(_exec_dir() / f"{slug}.log"),
                            "model": _exec_model(item, cfg),
                            "started_at": _now(), "recover": False,
                            "result": {"id": rid, "branch": branch,
                                       "worktree": str(existing_wt)}}
                else:
                    blocked[rid] = {"reason": "worktree_preserved",
                                    "worktree": str(existing_wt), "branch": branch,
                                    "ts": _now()}
                    log(f"[exec] {rid} 复用分支建 worktree 失败：{_err[:200]}")
                    return {"id": rid, "outcome": "worktree_preserved",
                            "branch": branch, "worktree": str(existing_wt)}
            blocked[rid] = {
                "reason": "worktree_preserved",
                "worktree": str(existing_wt),
                "branch": branch,
                "ts": _now(),
            }
            log(f"[exec] {rid} 保留现有 worktree/分支（reap_worktrees=false），不删除、不重建")
            return {"id": rid, "outcome": "worktree_preserved",
                    "branch": branch, "worktree": str(existing_wt)}
        else:
            _git(repo, "worktree", "remove", "--force", str(existing_wt))
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

    log_path = _exec_dir() / f"{slug}.log"
    model = _exec_model(item, cfg)
    return {"id": rid, "outcome": "ready", "item": item, "cfg": cfg,
            "repo": str(repo), "branch": branch, "worktree": str(wt),
            "codex": codex, "log": str(log_path), "model": model,
            "started_at": _now(), "recover": False,
            "result": {"id": rid, "branch": branch, "worktree": str(wt)}}


def _exec_command(job: dict) -> list:
    """Build the Codex CLI command for one prepared job."""
    cfg = job["cfg"]
    wt = Path(job["worktree"])
    cmd = [*job["codex"], "exec", "-C", str(wt), "-s", cfg["sandbox"],
           "--skip-git-repo-check"]
    for d in cfg.get("add_dirs") or []:
        cmd += ["--add-dir", str(d)]
    if job.get("model"):
        cmd += ["-m", job["model"]]
    effort = str(cfg.get("reasoning_effort") or "medium")
    cmd += ["-c", f'model_reasoning_effort="{effort}"']
    prompt = _exec_prompt(job["item"])
    if job.get("no_change_retry"):
        prompt += ("\n\n重要：上一次自动执行没有产生任何工作区改动，不能作为完成。"
                   "现在必须重新执行能留下改动的命令（写文件、修文本、补配置等），"
                   "并在命令后立刻自证。不要再做一轮只读检查。")
    cmd += [prompt]
    return cmd


def _start_exec(job: dict) -> dict | None:
    """Start one prepared Codex worker without waiting for it."""
    if job.get("recover"):
        # No new session is needed; make the existing branch immediately
        # reapable so the same supervisor performs add/commit/land.
        job["proc"] = _finished_proc()
        return job
    wt = Path(job["worktree"])
    log_path = Path(job["log"])
    cmd = _exec_command(job)
    log(f"[exec] 派发 {job['id']} -> {job['branch']}（worktree={wt}, "
        f"codex={job['codex'][0]}, model={job.get('model') or 'default'}）")
    try:
        fh = log_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, cwd=str(wt),
                                start_new_session=True)
    except OSError as exc:
        log(f"[exec] 无法启动 codex: {exc}")
        job["start_error"] = str(exc)
        return None
    job.pop("start_error", None)
    job["proc"] = proc
    job["fh"] = fh
    job["pid"] = proc.pid
    return job


def _reap_exec(job: dict, blocked: dict) -> dict:
    """Wait for one worker; implementation is concurrent, landing is not."""
    rid = job["id"]
    item, cfg = job["item"], job["cfg"]
    repo, wt, branch = Path(job["repo"]), Path(job["worktree"]), job["branch"]
    log_path = Path(job["log"])
    if job.get("recover"):
        log(f"[exec] {rid} 已有在途 worktree -> 跳过新会话，接着收口")
        if item.get("kind") == "resolve":
            return _finish_resolve_exec(
                item, cfg, repo, wt, branch, rid,
                {"id": rid, "branch": branch, "worktree": str(wt)}, blocked)
        return _finish_exec(item, cfg, repo, wt, branch, rid,
                            {"id": rid, "branch": branch, "worktree": str(wt)}, blocked,
                            log_path)
    retries = max(0, int(cfg.get("retries", EXEC_INFRA_RETRIES)))
    no_change_retries = max(0, int(cfg.get("no_change_retries",
                                           EXEC_NO_CHANGE_RETRIES)))
    total, attempt, code, infra = (retries + no_change_retries + 1,
                                   int(job.get("attempts") or 0), 0, False)
    result = job["result"]
    while True:
        attempt += 1
        if attempt > 1:
            if job.get("model_fallback_used"):
                retry_note = ("模型通道欠费/额度失败，切到慢通道重试"
                              f"（model={job.get('model') or 'default'}）")
            else:
                retry_note = f"第 {attempt}/{total} 次重试"
            log(f"[exec] {rid} {retry_note}"
                f"（同一 worktree={wt}，日志 {log_path}）")
            try:
                fh = log_path.open("a", encoding="utf-8")
                if job.get("model_fallback_used"):
                    fh.write(f"\n===== auto_exec 模型通道额度失败，"
                             f"切换到 slow_model 重试（{_now()}，"
                             f"model={job.get('model') or 'default'}）=====\n")
                elif job.get("no_change_retry"):
                    fh.write(f"\n===== auto_exec 无改动重试 {attempt}/{total} 次"
                             f"（{_now()}，上一次退出码={code}）=====\n")
                else:
                    fh.write(f"\n===== auto_exec 重试 第 {attempt}/{total} 次"
                             f"（{_now()}，上一次退出码={code}，"
                             f"判定为基础设施抖动）=====\n")
                fh.flush()
                proc = subprocess.Popen(
                    _exec_command(job), stdin=subprocess.DEVNULL, stdout=fh,
                    stderr=subprocess.STDOUT, cwd=str(wt), start_new_session=True)
            except OSError as exc:
                log(f"[exec] 无法启动 codex: {exc}")
                code = 125
                break
            job["proc"], job["fh"] = proc, fh
        try:
            code = job["proc"].wait(timeout=cfg["timeout"])
        except subprocess.TimeoutExpired:
            # Reap the whole worker process group. Leaving the child alive
            # would occupy a slot forever and duplicate work on the next poll.
            try:
                os.killpg(os.getpgid(job["proc"].pid), signal.SIGKILL)
            except OSError:
                job["proc"].kill()
            try:
                job["proc"].wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            code = 124
            # Popen.poll() gates _reap_exec. A killed process may not have a
            # returncode yet on every platform, so synchronize it explicitly.
            job["proc"].returncode = 124
        finally:
            try:
                fh = job.get("fh")
                if fh is not None:
                    fh.close()
            except (AttributeError, OSError):
                pass
            job.pop("fh", None)
        result["exit"] = code
        log(f"[exec] {rid} codex 退出码={code}（第 {attempt}/{total} 次，日志 {log_path}）")
        # Some CLI/provider combinations print a 403 balance error and still
        # exit 0. Provider failure must win over the process exit code.
        if _should_fallback_to_slow_model(job, cfg, log_path):
            job["model"] = str(cfg["slow_model"])
            job["model_fallback_used"] = True
            detail_tail = _exec_log_tail(log_path, limit=400).strip()
            log(f"[exec] {rid} 判定为模型通道额度失败，切到 slow_model="
                f"{job['model']} 重跑；日志尾部: {detail_tail[-120:]}")
            # Relaunch and return immediately. Waiting here would stop the
            # daemon loop from filling the remaining configured slots.
            try:
                fh = log_path.open("a", encoding="utf-8")
                fh.write(
                    "\n===== auto_exec 模型通道额度失败，切换到 slow_model 重试"
                    f"（{_now()}，model={job.get('model') or 'default'}）=====\n")
                fh.flush()
                proc = subprocess.Popen(
                    _exec_command(job), stdin=subprocess.DEVNULL, stdout=fh,
                    stderr=subprocess.STDOUT, cwd=str(wt), start_new_session=True)
            except OSError as exc:
                job["start_error"] = str(exc)
                code = 125
                break
            job["proc"], job["fh"], job["pid"] = proc, fh, proc.pid
            job["detached"] = False
            job["relaunched_at"] = _now()
            job["attempts"] = attempt
            result.update({"attempts": attempt, "outcome": "relaunched",
                           "model": job.get("model")})
            return result
        if code == 0:
            if (job.get("model_fallback_used")
                    and classify_exec_model_fallback(log_path)):
                code = 1
                infra = True
            elif not _changed_paths(wt):
                tries = int(job.get("no_change_tries") or 0)
                if tries < no_change_retries:
                    job["no_change_tries"] = tries + 1
                    job["no_change_retry"] = True
                    job["attempts"] = attempt
                    log(f"[exec] {rid} 退出 0 但无改动，异步重派 "
                        f"{tries + 1}/{no_change_retries}（工作区和槽位都保留）")
                    try:
                        fh = log_path.open("a", encoding="utf-8")
                        fh.write(f"\n===== auto_exec 退出 0 但无改动，异步重派 "
                                 f"{tries + 1}/{no_change_retries}（{_now()}）=====\n")
                        fh.flush()
                        proc = subprocess.Popen(
                            _exec_command(job), stdin=subprocess.DEVNULL, stdout=fh,
                            stderr=subprocess.STDOUT, cwd=str(wt), start_new_session=True)
                    except OSError as exc:
                        job["start_error"] = str(exc)
                        code = 125
                    else:
                        job["proc"], job["fh"], job["pid"] = proc, fh, proc.pid
                        job["relaunched_at"] = _now()
                        result.update({"attempts": attempt, "outcome": "relaunched",
                                       "retry_reason": "no_changes"})
                        return result
            if code == 0:
                break
        # Do not burn the supervisor's single reaping thread on repeated 429
        # sleeps. Defer this task briefly and keep every other slot moving.
        if classify_exec_throttled(log_path):
            break
        # Timeouts and startup failures are not retried. Only a transport
        # failure with a matching log tail gets another attempt.
        infra = code not in (124, 125) and classify_exec_infra_failure(log_path)
        if (not infra and job.get("model_fallback_used")
                and classify_exec_model_fallback(log_path)):
            infra = True
        if not infra or attempt > retries:
            break
        delay = EXEC_INFRA_BACKOFF * (2 ** (attempt - 1))
        log(f"[exec] {rid} 判定为基础设施抖动（退出码={code}），{delay:.0f}s 后重试")
        time.sleep(delay)
    result["attempts"] = attempt
    job["attempts"] = attempt
    if job.get("start_error"):
        # Fail at spawn time, not by waiting a full worker timeout later.
        code = 125
    if code != 0:
        if classify_exec_throttled(log_path):
            _defer_exec_channel_failure(
                rid, blocked, branch=branch, worktree=str(wt), error="throttled",
                detail=_exec_log_tail(log_path, limit=400))
            result.update({"outcome": "channel_unavailable",
                           "retry_next_cycle": True})
            return result
        reason = "codex_infra_fail" if infra else f"codex_exit_{code}"
        detail = ""
        if infra:
            detail = (f"基础设施抖动，同一 worktree 重试 {attempt - 1} 次仍失败；"
                      f"日志尾部: {_exec_log_tail(log_path)[-200:].strip()}")
        if item.get("kind") == "resolve":
            preserved = _preserve_exec(repo, wt, branch, rid, reason, blocked, detail=detail)
            if preserved.get("preserve"):
                result.update({"outcome": "codex_fail_preserved",
                               "commit": preserved.get("commit"),
                               "worktree": preserved.get("worktree")})
            else:
                blocked[rid] = {"reason": reason, "ts": _now()}
                result["outcome"] = "codex_fail"
            return result
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
    result["ready_to_finish"] = True
    result["outcome"] = "worker_done"
    return result


def _finish_exec(item, cfg, repo, wt, branch, rid, result, blocked,
                 log_path) -> dict:
    """codex 已实现（或从在途现场恢复）之后的公共收尾：add -> commit -> land -> close。"""
    conf = central_config.load(repo)
    main_branch = conf.get("main_branch", "main")
    # 把 codex 留下的改动显式提交（不 git add -A；只加 status 里列出的、非忽略的路径）。
    changed = _changed_paths(wt)
    pending = _branch_pending_commits(repo, branch, main_branch)
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
    elif pending:
        # 上一轮已经把改动提交进分支、只是没走完落盘（例如守护在 land 前退出）。
        # 重复实现会覆盖既定成果；直接把这笔提交交给同一道门禁收口。
        _, commit, _ = _git(repo, "rev-parse", branch)
        result["commit"] = commit or result.get("commit")
        log(f"[exec] {rid} worktree 干净但分支已有未落提交 -> 直接收口 {(commit or '')[:12]}")
    else:
        nested = _nested_exec_worktrees(wt)
        if nested:
            log(f"[exec] {rid} 外层 worktree 无改动，但嵌套 clone 有在途改动："
                f"{nested}；保留现场，不按 no_changes 清理")
            blocked[rid] = {"reason": "nested_clone_preserved", "branch": branch,
                            "worktree": str(wt), "nested": nested, "ts": _now()}
            result["outcome"] = "nested_clone_preserved"
            return result
        # 「没有改动」有两种完全不同的成因：真的做完了不需要改，和会话被
        # 额度耗尽/限流中途掐断（日志里有 403 insufficient balance、429），
        # 后者是通道问题、不是需求做不出来。把它记成 no_changes 会永久封死
        # 这条任务；这里先认通道失败，并按「不是这条任务的错」处理。
        channel_error = ""
        if classify_exec_throttled(log_path):
            channel_error = "throttled"
        elif classify_exec_model_fallback(log_path):
            channel_error = "provider_unavailable"
        if channel_error:
            _defer_exec_channel_failure(
                rid, blocked, branch=branch, worktree=str(wt),
                error=channel_error, detail=_exec_log_tail(log_path, limit=400))
            result.update({"outcome": "channel_unavailable",
                           "retry_next_cycle": True})
            return result
        log(f"[exec] {rid} 没有产生改动，记为无变化")
        # A worker may have no pending changes because its target commit was
        # already landed by an earlier pass. Keep the ledger accurate in that
        # case instead of leaving an open no_changes row forever.
        commit = str(item.get("commit") or "").strip()
        if commit:
            ancestor_rc, _, _ = _git(repo, "merge-base", "--is-ancestor",
                                     commit, main_branch)
            if ancestor_rc == 0:
                _run_truthctl(["close", rid, "auto-close: commit already in main"],
                              extra_env={"KG_TRUTH_COMMIT": commit})
        # 不写进 blocked：no_changes 只是「这一拍没做出东西」，不是「这活做不成」。
        # 永久阻塞会让额度抖动/超时中断过的任务再也不被重派。
        blocked.pop(rid, None)
        _cleanup_exec(repo, wt, branch)
        result["outcome"] = "no_changes"
        return result

    lrc, lout = run_kgctl(["land", branch], timeout=LAND_TIMEOUT)
    kind = classify_land(lout, lrc)
    if kind == "error":
        # fetch/push 的 TLS 或网络抖动会把整条 land 记成 error（常见为 rc=1 的
        # fetch origin/main 失败，少见于 124 超时）；真实冲突/门禁失败会被归成
        # conflict/gate_fail/merge_fail 等其他 kind。error 重试一次即可，不用把
        # 可落盘分支长期留在 exec_blocked。
        log(f"[exec] {rid} land error（可能是网络抖动），串行重试一次")
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
    # 锁竞争不是失败：另一条 land 正在跑，等它结束再试就行。
    # 把它记成永久的 land_locked 阻塞，等于把「排在我前面的人还没完」
    # 判成「这活做不成」，任务会一直躺到人工干预。
    if kind == "locked":
        result["outcome"] = "land_locked"
        result["retry_next_cycle"] = True
        log(f"[exec] {rid} 落盘锁被占用，本轮让路，下一拍重试（不算阻塞）")
        return result
    # 执行者已经交付了提交，任何落盘失败都保留 worktree 和分支，避免丢掉在途工作。
    blocked[rid] = {"reason": f"land_{kind}", "branch": branch,
                    "worktree": str(wt), "commit": commit, "ts": _now()}
    result["outcome"] = f"land_{kind}"
    return result


def _cleanup_exec(repo, wt: Path, branch: str) -> None:
    assessment = workspace_guard.worktree_removable(
        Path(repo), wt, branch, "main", commands="", backup_unique=False)
    if assessment.get("reason") == "missing":
        # 工作区已经不在了（上一轮收尾/压力回收把它删了），但分支还在。
        # 内容已全部进 main 时应当把分支一起清掉，否则每落一条就在本地留一个
        # 永远没人删的 codex/exec-* 分支。分支未进 main 时一律保留。
        rc, ahead, _ = _git(repo, "rev-list", "--count", f"main..{branch}")
        if rc == 0 and ahead.strip() == "0":
            _git(repo, "branch", "-D", branch)
            log(f"[exec] 工作区已不在，分支内容全在 main，顺手清掉 {branch}")
        return
    if assessment.get("ok"):
        ok, detail = workspace_guard.remove_worktree(Path(repo), wt)
        if not ok:
            log(f"[exec] worktree cleanup skipped: {detail}")
    else:
        # Keep live/dirty worktrees. Their branch is still available for a
        # later landing pass; deleting it here would lose the recovery path.
        log(f"[exec] worktree cleanup skipped ({assessment.get('reason')}): {wt}")
        return
    _git(repo, "branch", "-D", branch)


def _should_retry_resolve_finish(item: dict, rec: dict | None) -> bool:
    """Resolve finish may fail once for a remote push race; retry without a new worker."""
    return bool(
        item
        and item.get("kind") == "resolve"
        and rec
        and rec.get("reason") == "resolve_gate_fail")


def _retry_resolve_finish(item, cfg, repo, wt, branch, rid, blocked) -> dict | None:
    """Retry a committed resolve finish that previously hit gate_fail.

    The worktree is kept by resolve registrations, and the branch can be clean
    with unlanded commits after a successful Codex resolution. Retrying the
    finish uses the same land gate as a normal finish and avoids burning a new
    Codex session on already-resolved conflict work. The existing block is
    replaced on failure so a genuinely stuck gate stays visible.
    """
    slug = item.get("slug") or ""
    rec = _resolve_task(slug)
    if not rec:
        return None
    branch = rec.get("resolve_branch") or branch
    wt = Path(rec.get("worktree") or wt)
    main_branch = (central_config.load(repo) or {}).get("main_branch", "main")
    if not wt.exists() or _resolve_worktree_dirty(wt):
        return None
    _, commit, _ = _git(repo, "rev-parse", branch)
    if not _branch_pending_commits(repo, branch, main_branch):
        return None
    result = {"id": rid, "branch": branch, "worktree": str(wt), "commit": commit}
    rrc, rout = run_kgctl(["resolve", "--finish", slug, "--go"], timeout=LAND_TIMEOUT)
    log_block(f"resolve-exec finish retry {slug}", rrc, rout)
    result["resolve_finish"] = rrc
    if "outcome=landed" in rout:
        _set_resolve_status(repo, slug, "landed")
        result["outcome"] = "landed"
        blocked.pop(rid, None)
        _clear_conflict_note(branch)
        return result
    blocked[rid] = _escalate_conflict_block(
        {"reason": "resolve_gate_fail", "_id": rid, "branch": branch,
         "worktree": str(wt), "commit": commit, "ts": _now()}, branch, rout)
    result["outcome"] = "resolve_gate_fail"
    return result


def _finish_resolve_exec(item, cfg, repo, wt, branch, rid, result, blocked) -> dict:
    """codex 解完冲突后的公共收尾：显式提交，再交给 kgctl resolve --finish 走门禁。

    resolve worktree 和 resolve 分支由 kgctl 登记并回收；这里不能像普通 exec 一样
    在 land 前删除 resolve worktree，否则 finish 会认为现场丢失。
    """
    changed = _changed_paths(wt)
    commit = None
    if changed:
        add_rc, add_err = _git_add_retry(wt, changed)
        if add_rc != 0:
            log(f"[resolve-exec] {rid} git add 失败：{(add_err or '')[:300]}")
            return _preserve_exec(repo, wt, branch, rid, "add_fail", blocked, detail=add_err)
        msg = f"resolve-exec({item.get('slug')}): keep both intents"
        if _git(wt, "commit", "-m", msg)[0] != 0:
            return _preserve_exec(repo, wt, branch, rid, "commit_fail", blocked)
        _, commit, _ = _git(wt, "rev-parse", "HEAD")
        result["commit"] = commit
    else:
        # A resolved worktree may already be committed by the Codex worker while
        # leaving the worktree clean. Treat that as delivered work and send it
        # through the same resolve landing gate instead of dropping the slot.
        conf = central_config.load(repo)
        main_branch = conf.get("main_branch", "main")
        _, commit, _ = _git(repo, "rev-parse", branch)
        ancestor_rc, _, _ = _git(repo, "merge-base", "--is-ancestor",
                                 commit, main_branch)
        if _branch_pending_commits(repo, branch, main_branch) or ancestor_rc == 0:
            result["commit"] = commit or result.get("commit")
            log(f"[resolve-exec] {rid} worktree 干净且分支已有未落提交/已进 main "
                f"-> 直接收口 {(commit or '')[:12]}")
        else:
            resolve_log = _exec_dir() / f"resolve-{_exec_slug(item.get('slug') or '')}.log"
            channel_error = ""
            if classify_exec_throttled(resolve_log):
                channel_error = "throttled"
            elif classify_exec_model_fallback(resolve_log):
                channel_error = "provider_unavailable"
            if channel_error:
                _defer_exec_channel_failure(
                    rid, blocked, branch=branch, worktree=str(wt),
                    error=channel_error, detail=_exec_log_tail(resolve_log, limit=400))
                result.update({"outcome": "channel_unavailable",
                               "retry_next_cycle": True})
                return result
            log(f"[resolve-exec] {rid} 没有产生改动，不记永久阻塞（下一拍可重试）")
            blocked.pop(rid, None)
            result["outcome"] = "no_changes"
            return result

    slug = item.get("slug") or ""
    rrc, rout = run_kgctl(["resolve", "--finish", slug, "--go"], timeout=LAND_TIMEOUT)
    log_block(f"resolve-exec finish {slug}", rrc, rout)
    result["resolve_finish"] = rrc
    if "outcome=landed" in rout:
        _set_resolve_status(repo, slug, "landed")
        result["outcome"] = "landed"
        _clear_conflict_note(branch)
        return result
    blocked[rid] = _escalate_conflict_block(
        {"reason": "resolve_gate_fail", "_id": rid, "branch": branch,
         "worktree": str(wt), "commit": commit, "ts": _now()}, branch, rout)
    result["outcome"] = "resolve_gate_fail"
    return result


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


def _finished_proc(returncode: int = 0) -> "_Finished":
    """一个已结束进程的最小替身，保留真实退出码。

    守护重启后无法 wait() 上一轮的进程；收口路径只需要「进程已结束」这个
    事实，不该让 _reap_exec 对壳对象调用 subprocess.TimeoutExpired 语义再
    等一整轮 timeout（比如 3600s），否则整个 worker 池会空转。
    """
    obj = type("_Finished", (), {
        "poll": lambda self: returncode,
        "wait": lambda self, timeout=None: returncode,
        "returncode": returncode,
    })()
    return obj


def _reap_exited_child(proc, timeout: float = 0.2):
    """取回一个已退出子进程的真实退出码；状态还没就绪时返回 None。

    macOS 上进程可能已经从进程表消失，而 Popen.poll() 还没拿到状态。
    这里短暂 wait 一次，绝不把这种未知状态替换成成功码。
    """
    try:
        code = proc.poll()
        if code is not None:
            return int(code)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    try:
        return None if code is None else int(code)
    except (TypeError, ValueError):
        return None


def _worker_alive(pid) -> bool:
    # os.kill(pid, 0) 对已退出的子进程可能仍返回“存在”（父进程还没 wait 的
    # 僵尸态）。僵尸只占槽位、不会再产出任何结果，必须视为已完成，否则
    # max_workers=3 会被几个 defunct worker 占满，后续任务一直派不出去。
    try:
        pid_int = int(pid)
        os.kill(pid_int, 0)
    except (OSError, TypeError, ValueError):
        return False
    try:
        with open(f"/proc/{pid_int}/stat", "r", encoding="utf-8") as fh:
            state = fh.read().split()[2]
        if state == "Z":
            return False
    except (OSError, ValueError, IndexError):
        # macOS 没有 /proc：回退到进程表探测，僵尸态由父进程 wait 回收。
        try:
            out = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid_int)],
                capture_output=True, text=True, timeout=5)
            stat = (out.stdout or "").strip()
            if stat.startswith("Z"):
                return False
        except (OSError, subprocess.TimeoutExpired):
            pass
    return True


def _resolve_records_all():
    repo = _state_repo()
    if not repo:
        return []
    p = _resolve_tasks_path(repo)
    if not p or not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("tasks") or []


def _adopt_orphan_resolve_workers(workers: dict) -> dict:
    """Adopt a resolve worktree that has a live codex exec from a prior daemon.

    The persisted slot can be overwritten by an older daemon during restart, so
    rebuilding only from state.json is not enough. Match the open resolve worktree
    against live codex exec processes; this is cheap and only touches worktrees
    that were already registered by kgctl resolve.
    """
    jobs = workers.setdefault("exec_jobs", {})
    if not _codex_bin():
        return workers
    try:
        procs = subprocess.run(
            ["ps", "-axo", "pid=,command="], capture_output=True,
            text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return workers
    for rec in _resolve_records_all():
        if rec.get("status") not in ("needs_agent", "gate_fail"):
            continue
        item = _resolve_task_item(rec.get("slug") or "")
        if not item:
            continue
        rid = item.get("id") or ""
        if not rid or rid in jobs:
            continue
        wt = str(Path(item.get("worktree") or "").resolve())
        if not wt:
            continue
        pid = None
        for line in procs.splitlines():
            if wt in line and "codex" in line:
                fields = line.strip().split(None, 1)
                if fields and fields[0].isdigit():
                    pid = int(fields[0])
                    break
        if not pid or not _worker_alive(pid):
            continue
        jobs[rid] = {
            "id": rid, "outcome": "ready", "item": item,
            "cfg": auto_exec_config(), "codex": _codex_bin(),
            "repo": str(_state_repo() or ""), "branch": item.get("branch") or "",
            "worktree": str(item.get("worktree") or ""), "log": str(_exec_dir() / f"resolve-{_exec_slug(item.get('slug') or rid)}.log"),
            "model": _exec_model(item, auto_exec_config()),
            "started_at": _now(), "recover": False,
            "pid": pid, "detached": True,
            "result": {"id": rid, "branch": item.get("branch") or "",
                       "worktree": str(item.get("worktree") or "")},
        }
    return workers


def _adopt_orphan_exec_workers(workers: dict, open_items: dict) -> dict:
    """Reclaim live regular workers missing from persisted state."""
    jobs = workers.setdefault("exec_jobs", {})
    if not _codex_bin():
        return workers
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return workers
    live = {}
    for line in out.splitlines():
        if "codex exec -C " not in line:
            continue
        pid_match = re.match(r"\s*(\d+)\s+", line)
        wt_match = re.search(r"codex exec -C\s+(\S+)", line)
        if not pid_match or not wt_match:
            continue
        pid = int(pid_match.group(1))
        wt = str(Path(wt_match.group(1)).resolve())
        if _worker_alive(pid) and Path(wt).exists():
            live[wt] = pid
    for rid, item in open_items.items():
        if not rid or rid in jobs:
            continue
        # 用分支名精确比对，不要用「-l-0114- 是不是子串」这种前缀匹配：
        # L-0160 的标题里带着 "L-0114"，它的 worktree 名
        # （…-exec-l-0160-l-0114-tavily-432-…）会把 L-0114 这条需求错认到自己身上，
        # 于是两个台账行抢同一个现场，真正的 L-0114 反而没人管。
        _want_branch, _want_wt, _want_slug = _exec_target(item, auto_exec_config(),
                                                         _state_repo())
        for wt, pid in live.items():
            branch_rc, branch, _ = _git(Path(wt), "branch", "--show-current")
            if branch_rc != 0 or not branch:
                continue
            if branch != _want_branch and Path(wt).name != Path(_want_wt).name:
                continue
            slug = _want_slug
            cfg = auto_exec_config()
            jobs[rid] = {
                "id": rid, "outcome": "ready", "item": item,
                "cfg": cfg, "codex": _codex_bin(),
                "repo": str(_state_repo() or ""), "branch": branch,
                "worktree": wt, "log": str(_exec_dir() / f"{slug}.log"),
                "model": _exec_model(item, cfg), "started_at": _now(),
                "recover": False, "pid": pid, "detached": True,
                "result": {"id": rid, "branch": branch, "worktree": wt},
            }
            log(f"[exec] 重新认领仍在运行的 worker {rid} "
                f"(pid={pid}, worktree={wt})")
            break
    return workers


def _hydrate_exec_jobs(workers: dict) -> dict:
    """Restore live worker jobs after daemon restart without starting new sessions."""
    workers = _adopt_orphan_resolve_workers(workers)
    jobs = workers.setdefault("exec_jobs", {})
    open_items = {item.get("id"): item for item in _exec_open_items(100)}
    workers = _adopt_orphan_exec_workers(workers, open_items)
    jobs = workers.setdefault("exec_jobs", {})
    for rid, job in list(jobs.items()):
        if job.get("proc") is not None:
            continue
        persisted_worktree = str(job.get("worktree") or "").strip()
        if persisted_worktree and not Path(persisted_worktree).exists():
            branch = str(job.get("branch") or "").strip()
            if job.get("recover") and branch and _branch_pending_commits(
                    Path(job.get("repo") or ""), branch, "main"):
                # Recover only needs the branch: the worker worktree was
                # collected by a previous cycle, but committed work is pending.
                job.pop("proc", None)
                job["detached"] = True
            else:
                jobs.pop(rid, None)
                continue
        if not persisted_worktree:
            # Jobs without a worktree cannot be reaped or verified safely.
            # Reconstruct from the ledger only through normal dispatch, not by
            # resurrecting a stale active slot.
            jobs.pop(rid, None)
            continue
        # A previous cycle may already have landed this branch while the state
        # slot was still persisted (e.g. a manual/earlier land won the race, or
        # the ledger was closed after the branch reached main). Restoring such a
        # slot would make the daemon re-run commit/land on already-landed work,
        # or resurrect a stale recover job and burn a fresh Codex session.
        branch = str(job.get("branch") or "").strip()
        # 会话还在跑的时候，分支上当然还没有提交 —— 这不叫「已落盘」，更不叫
        # 「过期槽位」。以前这里只看到 pending==0 就把在跑的 worker 从池里摘掉，
        # 于是它的收口再也没人负责：进程跑完、产物躺在 worktree 里没人 commit，
        # 台账一直 open，机器看上去「在跑」其实进度全丢。
        if job.get("pid") and _worker_alive(job.get("pid")):
            job["proc"] = None
            job["detached"] = True
            continue
        if branch and not _branch_pending_commits(Path(job.get("repo") or ""), branch, "main"):
            wt_path = Path(job.get("worktree") or "")
            if (wt_path.exists()
                    and (_changed_paths(wt_path) or _nested_exec_worktrees(wt_path))):
                # In-flight worker evidence was left untracked while the branch
                # has no pending commits. Mark the existing slot ready so the
                # normal commit/land path owns it; a recover job already owns
                # that worktree and must not be silently dropped. Do not continue
                # here: hydrate must still reconstruct job["item"] below.
                job["proc"] = _finished_proc()
                job["detached"] = True
            else:
                jobs.pop(rid, None)
                continue
        if job.get("item") is None:
            item = open_items.get(rid)
            if item is None and str(rid).startswith("resolve:"):
                # Resolve worktrees are intentionally skipped by _resolve_open_items
                # while dirty or owned, so a persisted worker could not be restored
                # from the ledger after a daemon restart. Reconstruct it from the
                # resolve record instead; the live PID is reaped when it exits.
                item = _resolve_task_item(str(rid).split(":", 1)[1])
            if item is None:
                # Persisted job whose ledger row is gone cannot be reaped safely.
                # Keep the slot metadata out of active jobs instead of letting
                # _reap_exec raise KeyError('item') every cycle.
                job["partial"] = True
                jobs.pop(rid, None)
                continue
            job["item"] = item
        ledger_status = str((job.get("item") or {}).get("status") or "")
        if ledger_status in ("done", "closed", "cancelled", "superseded"):
            jobs.pop(rid, None)
            continue
        # 台账里这条已经 done/closed 时，job["item"] 可能是上一轮缓存下来的旧
        # 快照（status 还是 open）。以台账最新状态为准，否则收口完的槽位会一直
        # 占着并发名额：实测 4 个槽位被 3 个「已 done」的僵尸占住，
        # 真正要跑模型的位置只剩 1 个。
        try:
            _lts = open_items.get(rid)
            if _lts is not None and str(_lts.get("status") or "") in (
                    "done", "closed", "cancelled", "superseded"):
                jobs.pop(rid, None)
                continue
        except Exception:
            pass
        try:
            _live_status = (_ledger_latest() or {}).get(rid, {}).get("status")
            if str(_live_status or "") in ("done", "closed", "cancelled",
                                           "superseded"):
                jobs.pop(rid, None)
                continue
        except Exception:
            pass
        if not job.get("cfg"):
            job["cfg"] = auto_exec_config()
        if job.get("codex") is None:
            job["codex"] = _codex_bin()
        if job.get("recover"):
            # A recover job with no live session, no pending commits AND a clean
            # worktree is a stale slot: the branch is already at main and there
            # is nothing to commit/land. Drop it so dispatch can rebuild.
            # 但 worktree 里有未提交改动时绝不能丢：那正是要收口的东西。
            # 以前这里只判断 pending==0，于是「会话已结束、改动还在工作区」的
            # 槽位每轮被清掉、下一轮又被重新建成 recover，永远轮不到
            # _collect_exec 去 commit+land —— L-0144 就这样原地转了半小时。
            _rec_wt = Path(job.get("worktree") or "")
            _rec_dirty = _changed_paths(_rec_wt) if _rec_wt.exists() else []
            if (not _worker_alive(job.get("pid"))
                    and not _branch_pending_commits(
                        Path(job.get("repo") or ""), str(job.get("branch") or ""), "main")
                    and not _rec_dirty):
                jobs.pop(rid, None)
            continue
        if job.get("pid") and _worker_alive(job["pid"]):
            # A restarted daemon cannot wait() an unrelated process. Preserve
            # the slot and finish it once the PID disappears.
            job["proc"] = None
            job["detached"] = True
        elif job.get("pid"):
            job["detached"] = True
    return workers


def _collect_exec(workers: dict, blocked: dict) -> list:
    """Reap live workers one at a time; landing remains globally serial."""
    jobs = workers.setdefault("exec_jobs", {})
    results = []
    for rid, job in list(jobs.items()):
        proc = job.get("proc")
        # 进程早就退出了，但 proc 对象还是上一轮 Popen 的句柄（重启后则是
        # None）。先按 PID 活着与否对齐一次，避免对已死的进程反复 wait 或
        # 反过来把死进程当在跑。
        if proc is not None and job.get("pid"):
            try:
                if proc.poll() is None and not _worker_alive(job.get("pid")):
                    code = _reap_exited_child(proc)
                    if code is None:
                        # Popen has not returned a status yet. Wait for the
                        # real exit code instead of fabricating success.
                        continue
                    job["proc"] = _finished_proc(code)
                    proc = job["proc"]
            except Exception:
                pass
        if job.get("recover"):
            # 恢复型槽位不需要新会话，也不会有 proc。显式标成已结束，
            # 让同一轮就走 add/commit/land；否则它永远停在「等待」。
            if proc is None:
                job["proc"] = _finished_proc()
        elif proc is None and job.get("detached") and _worker_alive(job.get("pid")):
            continue
        elif proc is None and job.get("detached"):
            # A restarted daemon cannot wait() the original process. Treat a
            # dead detached worker as finished; _reap_exec then reads the
            # persisted log and runs the normal commit/land/close path.
            job["proc"] = _finished_proc()
        elif proc is None:
            # 没有 proc 但工作区还在：可能是守护重启后遗留的槽位。
            # 直接按「已结束」收口，不要记成 start_fail —— 那会把一条
            # 有真实产物的需求永久踢出队列。
            if Path(str(job.get("worktree") or "")).exists():
                job["proc"] = _finished_proc()
                job["detached"] = True
            else:
                blocked[rid] = {"reason": "start_fail", "ts": _now()}
                jobs.pop(rid, None)
                results.append({"id": rid, "outcome": "start_fail"})
                continue
        elif proc.poll() is None:
            continue
        result = _reap_exec(job, blocked)
        if result.get("outcome") == "relaunched":
            results.append(result)
            continue
        jobs.pop(rid, None)
        if result.get("outcome") == "worker_done":
            item = job["item"]
            if item.get("kind") == "resolve":
                result = _finish_resolve_exec(
                    item, job["cfg"], Path(job["repo"]), Path(job["worktree"]),
                    job["branch"], rid, result, blocked)
            else:
                result = _finish_exec(item, job["cfg"],
                                      Path(job["repo"]), Path(job["worktree"]),
                                      job["branch"], rid, result, blocked,
                                      Path(job["log"]))
        results.append(result)
        # One land per cycle; another completed worker is collected next
        # cycle. This keeps the critical section serial and auditable.
        if result.get("outcome") in ("landed", "land_locked", "land_error",
                                     "land_conflict", "land_gate_fail",
                                     "resolve_gate_fail", "no_changes",
                                     "codex_fail", "codex_fail_preserved"):
            # 串行落盘：提交/推送这一小段落只能一个接一个。但「一个落完就
            # 结束本轮」会把另一个已经跑完的 worker 压到下一轮（最长 15 分钟）。
            # 这里把串行收窄到「每次只做一件落盘」，做完继续收下一个。
            if result.get("outcome") == "landed":
                continue
            # 失败/无变化没有改变 main，也不持有落盘锁；继续收下一个已经
            # 结束的 worker，避免一条 resolve_gate_fail 把同轮其他完成项
            # 推迟到下一拍。真正 land 的分支仍在上面的 landed 分支里逐条
            # 串行处理。
            if result.get("outcome") in ("resolve_gate_fail", "no_changes",
                                         "codex_fail", "codex_fail_preserved"):
                continue
            break
    return results


EXEC_REPEAT_CAP = 3            # 同一需求在窗口内最多派 3 次
EXEC_REPEAT_WINDOW = 6 * 3600  # 6 小时

_LIVE_EXEC_CACHE = {"ts": 0.0, "slugs": frozenset()}


def _live_exec_slugs(max_age: float = 20.0) -> frozenset:
    """正在跑的 `codex exec -C <worktree>` 的工作区名集合。

    用途：守护进程重启后，上一轮拉起的 codex exec 会变成孤儿（不在 exec_jobs
    里），dispatch 会以为槽位空着又派一次 —— 同一需求在工作区里叠加多个
    codex exec，每个都拉起后端进程，最后把机器 CPU/磁盘吃满。
    fail-open：任何异常都返回空集，最多多派一次，绝不会卡住派发。
    """
    now = time.time()
    if now - _LIVE_EXEC_CACHE["ts"] < max_age:
        return _LIVE_EXEC_CACHE["slugs"]
    slugs = set()
    try:
        out = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines():
            if "codex" not in line or " exec" not in line:
                continue
            m = re.search(r"-C\s+(\S+)", line)
            if m:
                slugs.add(Path(m.group(1)).name)
    except Exception as exc:  # fail-open
        log(f"[exec] 扫描在跑的 codex exec 失败（忽略）：{exc}")
        return frozenset()
    _LIVE_EXEC_CACHE.update(ts=now, slugs=frozenset(slugs))
    return _LIVE_EXEC_CACHE["slugs"]


def _exec_attempt_gate(rid: str) -> dict | None:
    """记录一次派发；窗口内超上限就返回 blocked 记录，停止自动重派。

    现象：某个需求反复失败但台账行一直 open，每轮都重新拉起 codex exec，
    一天下来把磁盘/CPU 灌满。上限到顶后交回人处理（面板 blocked 可见）。
    fail-open：读写失败就当没有上限。
    """
    """记录一次派发；窗口内超上限就返回 blocked 记录，停止自动重派。

    现象：某个需求反复失败但台账行一直 open，每轮都重新拉起 codex exec，
    一天下来把磁盘/CPU 灌满。上限到顶后交回人处理（面板 blocked 可见）。
    fail-open：读写失败就当没有上限。
    """
    try:
        path = RUNTIME / "exec_attempts.json"
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        now = time.time()
        rec = [t for t in (data.get(rid) or []) if now - float(t) < EXEC_REPEAT_WINDOW]
        if len(rec) >= EXEC_REPEAT_CAP:
            data[rid] = rec
            return {"reason": "exec_retry_cap",
                    "title": f"{rid} 窗口内已派 {len(rec)} 次仍未收口，暂停自动重派",
                    "ts": _now()}
        rec.append(now)
        data[rid] = rec
        path.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as exc:  # fail-open
        log(f"[exec] 派发计数失败（忽略）：{exc}")
        return None
    return None


def release_attempt_slot(rid: str) -> bool:
    """Give back one dispatch slot when the attempt died of throttling.

    A 429 from the model gateway is not evidence that the task is impossible:
    the session never got to do any work. Charging those attempts against the
    repeat cap strands healthy tasks behind a transient rate limit, which is
    exactly how thirteen items went quietly un-dispatched.
    """
    try:
        path = RUNTIME / "exec_attempts.json"
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return False
        except Exception:
            return False
        rec = list(data.get(rid) or [])
        if not rec:
            return False
        rec.pop()
        if rec:
            data[rid] = rec
        else:
            data.pop(rid, None)
        path.write_text(json.dumps(data, ensure_ascii=False))
        return True
    except Exception as exc:  # fail-open
        log(f"[exec] 归还派发名额失败（忽略）：{exc}")
        return False


# ── 防跑飞安全闸（2026-10-22：重复派发 + 无限重试把整机 CPU/磁盘耗尽）──────────
# 教训固化成代码，三条纪律：
#   1) 失败必须退避且有上限：同一件事不许无限重试（查重 + 计数封顶）；
#   2) 「需要人裁决」的信号不许进自动重试队列（同一组冲突反复撞就停手）；
#   3) 一切自动清理/限流 fail-open 且可一键关闭：safety.json 或 KG_* 环境变量。
# 关闭方式：~/.codex-autopilot/safety.json 写 {"reap_worktrees": false} 等，
# 或 export KG_REAP_WORKTREES=0 / KG_REAP_BACKENDS=0。

RESOLVE_CONFLICT_CAP = 2       # 同一组冲突文件连续撞 N 次 → 停手等人裁决
RESOLVE_CONFLICT_BLOCK_H = 6   # 停手时长（小时），到期自动允许再试一次
REAP_MIN_IDLE_H = float(os.environ.get("KG_REAP_MIN_IDLE_H", "3"))
REAP_IDLE_PRESSURE_H = float(os.environ.get("KG_REAP_IDLE_PRESSURE_H", "0.25"))
# 每轮 worktree 回收的时间预算：回收不能把派活饿死。
REAP_TIME_BUDGET_S = float(os.environ.get("KG_REAP_TIME_BUDGET_S", "25"))
                               # worktree 至少闲置这么久才回收（按目录 mtime 与分支最后提交取较新者）
REAP_MAX_PER_ROUND = int(os.environ.get("KG_REAP_MAX_PER_ROUND", "5"))
# 空间自救阈值：可用低于它就回收零损失现场 + git gc（**不降低并发**）。
REAP_PRESSURE_FREE_GB = float(os.environ.get("KG_REAP_PRESSURE_FREE_GB", "12"))


def _safety_path() -> Path:
    return RUNTIME / "safety.json"


def _knob(name: str, default: bool = True) -> bool:
    """一键开关。safety.json 的布尔值优先于环境变量，任何异常都返回 default。"""
    try:
        env = os.environ.get("KG_" + name.upper())
        if env is not None:
            return env.strip().lower() not in ("0", "off", "false", "no")
        path = _safety_path()
        if path.exists():
            val = json.loads(path.read_text()).get(name)
            if isinstance(val, bool):
                return val
    except Exception:
        pass
    return default


def _workspace_root() -> Path:
    return Path(os.environ.get("KG_WORKSPACE_ROOT")
                or (Path.home() / ".newmax" / "workspace"))


def _conflict_files(out: str) -> list:
    """从 kgctl resolve --finish 的输出里扒出冲突文件名（用于认「同一组冲突」）。"""
    files = []
    try:
        lines = (out or "").splitlines()
        for i, line in enumerate(lines):
            if "个冲突文件" not in line:
                continue
            for nxt in lines[i + 1:i + 12]:
                s = nxt.strip()
                if not s or s.startswith(("×", "→", "这是", "在该工作树", "RESOLVE_RESULT")):
                    break
                if re.fullmatch(r"[\w./@+-]+\.\w{1,6}", s):
                    files.append(s)
    except Exception:
        pass
    return files[:12]


def _conflict_path() -> Path:
    return RUNTIME / "resolve_conflicts.json"


def _escalate_conflict_block(block: dict, branch: str, out: str) -> dict:
    """同一组冲突连续撞 → 把 block 升级成「需人工裁决」，不再自动重试。

    2026-10-22 实录：stability-evidence-benchmark-path-fix 每 5 分钟撞同一个
    SeedInputCard.tsx，一天 51 次 gate_fail，日志自己写着「需要归属裁决」却照样重试。
    fail-open：任何异常都保留原来的 resolve_gate_fail 语义。
    """
    try:
        files = _conflict_files(out)
        path = _conflict_path()
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        rec = data.get(branch) or {}
        sig = ",".join(sorted(files)) if files else str(rec.get("sig") or "")
        count = int(rec.get("count") or 0) + 1 if (sig and rec.get("sig") == sig) else 1
        data[branch] = {"sig": sig, "files": files, "count": count, "ts": time.time()}
        path.write_text(json.dumps(data, ensure_ascii=False)[:200000])
        block["conflict_files"] = files
        block["conflict_repeat"] = count
        if files and count >= RESOLVE_CONFLICT_CAP:
            shown = "、".join(files[:3])
            block["reason"] = "resolve_conflict_needs_decision"
            block["title"] = (f"{branch} 同一组冲突连续 {count} 次（{shown}），"
                              f"需人工裁决后再放行")
            log(f"[safety] {branch} 同一组冲突连续 {count} 次（{shown}），"
                f"{RESOLVE_CONFLICT_BLOCK_H}h 内不再自动重试")
    except Exception as exc:  # fail-open
        log(f"[safety] 冲突计数失败（忽略）：{exc}")
    return block


def _clear_conflict_note(branch: str) -> None:
    """落盘成功后清掉冲突计数，下次真冲突重新从 1 开始。"""
    try:
        path = _conflict_path()
        if not path.exists():
            return
        data = json.loads(path.read_text())
        if isinstance(data, dict) and branch in data:
            data.pop(branch, None)
            path.write_text(json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


def _disk_pressure() -> int:
    """磁盘偏紧信号。永远返回 0：**不限制并发**。

    历史：这里曾经返回 1，把并发压到 1 个会话。磁盘余量后来又以资源闸形式
    留在派活路径上，仍会在空间偏紧时把配置的并发槽位挡成 0。并发上限只由
    auto_exec.max_workers 决定，磁盘不再参与派活限流。

    磁盘偏紧时要做的不是少干活，而是把空间找回来：压力回收 + git gc
    仍然照常由 `_reap_dead_worktrees()` / `_git_gc_maintenance()` 执行。
    """
    return 0


def _disk_free_gb() -> float | None:
    """当前可用 GB（df 口径）。仅用于日志与空间自救判断，不参与限流。"""
    try:
        out = subprocess.run(["df", "-k", "/System/Volumes/Data"],
                             capture_output=True, text=True, timeout=8).stdout
        return int(out.strip().splitlines()[-1].split()[3]) / 1024 / 1024
    except Exception:
        return None


def _reap_backend_orphans() -> int:
    """杀掉挂在「已无 codex exec 的 exec worktree」上的 node 后端。

    2026-10-22 实录：两个 `node dist/index.js` 各自 100% CPU 烧了 6 小时，
    父进程早死、worktree 没人管，直到把整机拖到 load 11。
    只认 cwd 在 *-exec-* worktree 里的 node，绝不碰用户在别的目录起的服务。
    fail-open：任何异常只记日志。
    """
    if not _knob("reap_backends", True):
        return 0
    killed = 0
    try:
        live = _live_exec_slugs()
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines():
            if "dist/index.js" not in line:
                continue
            pid = line.strip().split(None, 1)[0]
            if not pid.isdigit():
                continue
            cwd = ""
            try:
                cw = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                                    capture_output=True, text=True, timeout=8).stdout
                cwd = next((l[1:] for l in cw.splitlines() if l.startswith("n")), "")
            except Exception:
                continue
            if "-exec-" not in cwd:
                continue
            wt_name = Path(cwd).parts[-2] if cwd.endswith("backend") else Path(cwd).name
            if any(f"-{wt_name}-" in f"-{s}-" for s in live):
                continue  # 还有 exec 在跑，不能杀
            try:
                os.kill(int(pid), signal.SIGTERM)
                killed += 1
                log(f"[safety] 回收孤儿后端 pid={pid}（worktree 已无 exec）：{cwd}")
            except Exception as exc:
                log(f"[safety] 回收孤儿后端 pid={pid} 失败：{exc}")
    except Exception as exc:  # fail-open
        log(f"[safety] 孤儿后端扫描失败（忽略）：{exc}")
    return killed


def _wt_last_activity(wt: Path) -> float:
    """worktree 最后一次有效活动的秒级时间：目录 mtime 与分支最后提交取较新者。

    只看目录 mtime 会误判（任何顶层增删都会刷新它），只看提交时间又会漏掉
    「刚建好还没提交」的新 worktree —— 两者取 max 最接近「真的没人动了」。
    """
    ts = 0.0
    try:
        ts = wt.stat().st_mtime
    except Exception:
        pass
    try:
        rc, out, _ = _git(wt, "log", "-1", "--format=%ct")
        if rc == 0 and out.strip().isdigit():
            ts = max(ts, float(out.strip()))
    except Exception:
        pass
    return ts


def _worktree_repo(wt: Path) -> Path | None:
    """worktree → 它所属的主仓路径。不是 worktree（或读不到）就返回 None。"""
    try:
        if not (wt / ".git").exists() or (wt / ".git").is_dir():
            return None  # 主仓自己 / 非 git 目录，绝不动
        rc, out, _ = _git(wt, "rev-parse", "--git-common-dir")
        if rc != 0 or not out.strip():
            return None
        common = Path(out.strip())
        if not common.is_absolute():
            common = (wt / common).resolve()
        return common.parent if common.name == ".git" else common
    except Exception:
        return None


def _git_gc_maintenance() -> None:
    """Retire the automatic ``gc --prune=now`` maintenance path.

    This repository is a partial clone shared by hundreds of worktrees. Pruning
    unreachable objects while other processes lazily fetch can delete freshly
    written commits or the blobs they need, and the next command misreads the
    damaged ref as "already merged". Git's ordinary auto-gc policy (without an
    explicit immediate prune) remains available to the user, but autopilot must
    not destructively prune objects on its own.
    """
    log("[safety] automatic git gc --prune=now is disabled; no objects were pruned")


def _reap_dead_worktrees() -> int:
    """回收闲置且安全的 worktree（无改动、分支无未落提交、没人在用）。

    2026-10-22 实录：autopilot 累计留下 336 个 worktree / 24.5GB（一天新增 22 个），
    从不回收，磁盘只剩 685MB。判据只看「安全」：分支已并入 main（内容都在 main）、
    工作区无未提交改动、没有活着的 codex exec、闲置够久。
    排除：主仓本身、resolver 登记的现场、名字带 resolve 的树。
    fail-open + 每轮上限，宁可少回收不可误删。
    """
    # 默认关闭（用户明确要求「不清理」）。但磁盘逼近硬闸时，守护要能自己活下来：
    # 只回收「零损失」的现场 —— 分支无未落提交、工作区无未提交改动、无活着的会话、
    # 闲置够久，且**只删工作目录、保留分支**。任何一条不满足就不动。
    # 用 KG_REAP_ON_DISK_PRESSURE=0 / safety.json 关掉这一层。
    _pressure_reap = False
    if not _knob("reap_worktrees", True):
        if not _knob("reap_on_disk_pressure", True):
            return 0
        _free = _disk_free_gb()
        if _free is not None and _free >= REAP_PRESSURE_FREE_GB:
            return 0          # 空间够用就不动任何现场
        _pressure_reap = True
        log(f"[safety] 磁盘可用 {(_free if _free is not None else -1):.2f}GB "
            f"< {REAP_PRESSURE_FREE_GB}GB：对零损失现场做回收（只删目录，分支保留）")
    reaped = 0
    try:
        root = _workspace_root()
        if not root.exists():
            return 0
        live = _live_exec_slugs()
        # 磁盘偏紧时放宽「闲置多久算可回收」：默认 3 小时是为了不打扰正在
        # 进行的活；但今天刚收口完的现场只闲置 2 小时，卡在门槛上就一个都
        # 回收不了，磁盘照样往下走。压力模式下用 30 分钟 —— 安全判据
        # （无未提交改动、分支无未落提交、无活着的会话）一条都没放松。
        idle_h = REAP_IDLE_PRESSURE_H if _pressure_reap else REAP_MIN_IDLE_H
        cutoff = time.time() - idle_h * 3600
        # 压力模式每轮多回收一些：一轮只放 5 个时，磁盘下滑速度可能比回收
        # 速度快（每轮 45s，每个现场 ~200MB），追不上。
        max_round = REAP_MAX_PER_ROUND * 8 if _pressure_reap else REAP_MAX_PER_ROUND
        # 时间预算：回收本身要扫 282 个现场、每个还跑几次 git，压力模式下
        # 一轮能占掉一分多钟（实测 19:53:47 单个回收就花 78s）。它排在派活
        # 之前同步执行，于是 worker 掉下去后整轮都在等它，补派被拖住。
        # 超过预算就收工，剩下的留给下一轮 —— 回收是渐进目标，派活不能被它饿死。
        deadline = time.time() + REAP_TIME_BUDGET_S
        for d in sorted(root.iterdir()):
            if reaped >= max_round:
                break
            if time.time() > deadline:
                log(f"[safety] worktree 回收达时间预算 {REAP_TIME_BUDGET_S:.0f}s"
                    f"（已回收 {reaped} 个），余下留给下一轮，先回去派活")
                break
            if not d.is_dir():
                continue
            if "resolve" in d.name:
                continue
            if any(f"-{d.name}-" in f"-{s}-" for s in live):
                continue
            repo = _worktree_repo(d)
            if repo is None:
                continue
            if _wt_last_activity(d) > cutoff:
                continue
            brc, branch, _ = _git(d, "rev-parse", "--abbrev-ref", "HEAD")
            if brc != 0 or not branch or branch in ("HEAD", "main"):
                continue
            assessment = workspace_guard.worktree_removable(
                repo, d, branch, "main", commands="", backup_unique=False)
            if not assessment.get("ok"):
                continue
            ok, detail = workspace_guard.remove_worktree(repo, d)
            if ok:
                reaped += 1
                log(f"[safety] 回收闲置 worktree {d.name}"
                    f"（分支 {branch} 无未落提交；分支本身保留）")
                if _pressure_reap:
                    # 只回收目录，分支一律保留：重装/回溯时仍能从分支找回全部内容。
                    log(f"[safety] 保留分支 {branch}（压力回收不删分支）")
            else:
                log(f"[safety] 回收 worktree 跳过 {d.name}：{detail}")
    except Exception as exc:  # fail-open
        log(f"[safety] worktree 回收失败（忽略）：{exc}")
    return reaped


def _active_exec_session_count(jobs: dict) -> int:
    """Count only model sessions with a live PID.

    A finished worker can still own a slot briefly while its worktree is being
    committed and landed. That cleanup work must keep the item reserved against
    duplicate dispatch, but it must not consume a model concurrency slot.
    """
    return sum(1 for job in jobs.values()
               if not job.get("recover") and _worker_alive(job.get("pid")))


def dispatch_exec(dry: bool, blocked: dict, workers: dict | None = None):
    """Fill up to max_workers slots; completed workers still land serially."""
    cfg = auto_exec_config()
    if not cfg["enabled"]:
        return [], workers or {}
    codex = _codex_bin()
    if not codex:
        log("[exec] auto_exec 已开，但找不到 codex 可执行文件，跳过")
        return [], workers or {}
    workers = _hydrate_exec_jobs(workers if workers is not None else {})
    jobs = workers.setdefault("exec_jobs", {})
    # 先按台账真实状态清掉已完成槽位。hydrate 只在启动时跑一次，而一轮里
    # 可能有多个槽位刚收口完；不在这里清，它们会一直占着并发名额。
    try:
        _latest = _ledger_latest() or {}
        for _rid in list(jobs):
            _st = str((_latest.get(_rid) or {}).get("status") or "")
            if _st in ("done", "closed", "cancelled", "superseded"):
                jobs.pop(_rid, None)
    except Exception:
        pass
    # 只差落盘的 recover 槽位不耗模型，先把它们收口可以立刻腾出「需要会话」
    # 的名额，也不会和正在跑的 worker 争资源。
    results = _collect_exec(workers, blocked)
    # 并发槽位只看配置，不看磁盘：空间偏紧时在派活后台回收，不挡派活。
    limit = max(1, int(cfg.get("max_workers", EXEC_DEFAULT_MAX_WORKERS)))
    # A completed worker frees one slot immediately. Keep filling in the same
    # cycle instead of idling until the next 10-minute heartbeat.
    active_ids = {rid for rid, job in jobs.items() if not job.get("recover")}
    # If _collect_exec just wrote a resolve gate_fail into blocked, it must not
    # be retried until the next cycle. Retrying inside the same cycle can race
    # another resolve worker that has already been dispatched for the same slug.
    failed_this_cycle = {r.get("id") for r in results
                         if r.get("outcome") == "resolve_gate_fail"}
    force_session_ids = set()
    open_items = list(_exec_open_items())

    def _session_slots() -> int:
        # 只数「真在跑/真要用模型」的槽位；recover 只是等落盘，不占算力名额。
        return _active_exec_session_count(jobs)

    # 守护重启会 hydrate 回一批仍在跑的 worker。若上一轮已经跑到上限，
    # 这里必须直接停手 —— 只在「新派一个之后」才检查上限，会让每次重启
    # 都多塞一个进池子，实测累积到 7 个 codex 会话同时跑，把机器压死。
    if _session_slots() >= limit:
        log(f"[exec] 已有 {_session_slots()} 个会话在跑（上限 {limit}），本轮不派新活")
        return results, workers
    for item in sorted(open_items,
                       key=lambda it: (it.get("id") in blocked, it.get("ts", ""))):
        rid = item.get("id")
        if rid in jobs or rid in active_ids:
            continue
        if _session_slots() >= limit:
            break
        # 防重复派发：同一工作区已经有 codex exec 在跑（多半是重启后遗留的孤儿），
        # 再派一个只会叠加进程把机器拖死。
        try:
            _rid_slug = _exec_slug(rid)
            if any(f"-{_rid_slug}-" in f"-{_s}-" for _s in _live_exec_slugs()):
                log(f"[exec] {rid} 已有 codex exec 在该工作区运行，本轮不重复派发")
                continue
        except Exception:
            pass
        if (item.get("kind") == "resolve"
                and not _resolve_auto_allowed(item)):
            blocked[rid] = {"reason": "needs_product_decision",
                            "title": (item.get("title") or "")[:60],
                            "ts": _now()}
            continue
        if rid in blocked and item.get("kind") == "resolve":
            rec = _resolve_task(item.get("slug") or "")
            # Existing markers are the reason the conflict is still blocked;
            # they are not evidence that a human-decision hold has cleared.
            # Clearing here made the 6h stop-repeat gate retry every cycle.
            if (blocked[rid].get("reason") not in (
                    "resolve_conflict_needs_decision", "channel_unavailable")
                    and _resolve_worktree_has_markers(rec)):
                blocked.pop(rid, None)
        if rid in blocked:
            if blocked[rid].get("reason") == "channel_unavailable":
                if not _channel_retry_due(blocked[rid]):
                    continue
                blocked.pop(rid, None)
                force_session_ids.add(rid)
                log(f"[exec] {rid} 通道退避到期，重开模型会话")
            else:
                if (_should_retry_resolve_finish(item, blocked[rid])
                        and rid not in failed_this_cycle):
                    # Previous cycle left a committed resolve branch at gate_fail.
                    # Do not start a new Codex session; finish the same resolved
                    # branch through the land gate now.
                    if item.get("kind") == "resolve":
                        retried = _retry_resolve_finish(
                            item, cfg, Path(item.get("repo") or ""),
                            Path(item.get("worktree") or ""), item.get("branch") or "",
                            rid, blocked)
                        if retried:
                            results.append(retried)
                continue
        if _looks_like_command(item.get("title")) or _looks_like_command(item.get("detail")):
            log(f"[exec] {rid} 输入像命令残片（{(item.get('title') or '')[:24]!r}），"
                f"不派给执行器")
            blocked[rid] = {"reason": "looks_like_command",
                            "title": (item.get("title") or "")[:60], "ts": _now()}
            continue
        # A resolve branch may already carry a committed resolution from an
        # earlier worker while the ledger row is still needs_agent/gate_fail.
        # Dispatch would otherwise start a second Codex session to redo the
        # same conflict. Reuse the finish path for committed work instead of
        # burning a fresh worker on it.
        if (item.get("kind") == "resolve"
                and _resolve_worktree_clean_for_finish(item)):
            retried = _retry_resolve_finish(
                item, cfg, Path(item.get("repo") or ""),
                Path(item.get("worktree") or ""), item.get("branch") or "",
                rid, blocked)
            if retried:
                results.append(retried)
            continue
        if dry:
            log(f"[dry] 会派发 {rid}（{item.get('kind')}）{item.get('title')}")
            break
        # 派发上限的目的是「不要让同一件事反复拉起新会话把机器灌满」。
        # 已经有在途成果的（worktree 里留着改动、或分支上有未落提交）不需要
        # 新会话，只走收口门禁；把它们算进上限等于让旧 bug 留下的
        # exec_retry_cap 伪失败永久锁死已经做完的活。
        if item.get("kind") != "resolve":
            try:
                # “分支已无独有提交”只能证明准备的基线已在 main，不能证明这条
                # 需求被修过。若分支只是从旧 main 创建且 worker 没留下提交，
                # 直接 close 会把真实 open 项永久误关。只有台账明确记录了某个
                # commit、且该 commit 已在 main 时，才允许免会话自动收口。
                _commit = str(item.get("commit") or "").strip()
                if _commit and _main_contains(_commit):
                    _rc_c, _out_c = _run_truthctl(
                        ["close", rid, "auto-close: commit already in main"],
                        extra_env={"KG_TRUTH_COMMIT": _commit})
                    log(f"[exec] {rid} 台账 commit 已在 main -> 自动收口 "
                        f"rc={_rc_c}")
                    if _rc_c == 0:
                        continue
            except Exception as _exc:
                log(f"[exec] {rid} 自动收口判断失败（忽略）：{_exc!r}")
        if (item.get("kind") != "resolve"
                and not _exec_has_inflight_work(item, cfg, _state_repo())):
            _cap = _exec_attempt_gate(rid)
            if _cap:
                blocked[rid] = _cap
                log(f"[exec] {rid} 已达自动派发上限（{_cap['reason']}），交回人工处理")
                continue
        if item.get("kind") == "resolve":
            job = _prepare_resolve_exec(item, cfg, blocked)
        else:
            job = _prepare_exec(item, cfg, blocked,
                                force_session=rid in force_session_ids)
        if job.get("outcome") != "ready":
            results.append(job)
            continue
        _start_exec(job)
        jobs[rid] = job
        # 槽位只统计「需要跑 codex 会话」的工作。recover 槽位不需要模型
        # （只是等落盘门禁按序收口），把它们算进并发上限等于用一条不需要
        # 算力的队列项去挤占真正要跑模型的位置 —— 实测 4 个槽位里 3 个是
        # recover，机器上只剩 1 个 codex 在跑，看起来「没在并行」就是这个原因。
        if _session_slots() > limit:
            break
    return results, workers


def _exec_dir() -> Path:
    d = RUNTIME / "exec"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_cycle(dry: bool, max_land: int) -> dict:
    """用户可见的一轮：先过「暂停」闸门，再调用真正的周期实现。

    暂停语义（面板「暂停」按钮就是写这个开关）：
      * 暂停时**不启动任何新周期** —— 不 tick、不 land、不 resolve。
      * 正在跑的周期允许安全收尾，不会被从中间砍断（land 自带完整门禁）。
      * 因此这里是最外层也最可靠的闸门；`_run_cycle` 里的逐子系统闸门是第二道保险，
        防止别的调用方绕过本函数直接进内部实现。
    """
    if not control_state.is_enabled():
        log("[disabled] 自动挡已关闭或控制文件异常：本轮不启动任何新工作")
        return {"ts": time.time(), "iso": _now(), "dry": dry,
                "disabled": True, "summary": {}, "landed": [], "failures": []}
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
        rc, qout = run_kgctl(["queue", "--light"])
        clean, summary, files_by_branch = parse_queue(qout)
        log_block("queue(dry)", rc, qout)
        log(f"[dry] 本轮会 land {min(len(clean), max_land)} / {len(clean)} 个: "
            f"{clean[:max_land]}")
        return {"ts": time.time(), "iso": _now(), "dry": True,
                "summary": summary, "landed": [], "failures": []}

    log("--- cycle start (live) ---")
    if not control_state.is_enabled():
        log("[disabled] 周期开始前自动挡已关闭：不做路由/落盘")
        return {"ts": time.time(), "iso": _now(), "dry": dry,
                "disabled": True, "summary": {}, "landed": [], "failures": []}
    if control_state.is_paused():
        log("[pause] 周期开始前被暂停：只刷新状态，不做路由/落盘")
        return {"ts": time.time(), "iso": _now(), "dry": dry,
                "paused": True, "summary": {}, "landed": [], "failures": []}
    # 安全回收放到派活之后（见下方 dispatch 后的同名块）：回收要扫几百个现场、
    # 每个还跑几次 git，曾把派活饿死一整个周期。
    # Route new intake before filling worker slots. dispatch_exec only sees
    # rows that already exist in the ledger; routing after dispatch makes a
    # freshly captured task wait a full cycle while all slots sit idle.
    route_rc, route_out = run_kgctl(["route", "--go"], timeout=TICK_TIMEOUT)
    log_block("route(pre-exec)", route_rc, route_out)
    exec_results, exec_landed = [], []
    exec_blocked = dict(load_state().get("exec_blocked") or {})
    for _stale in _prune_exec_blocked(exec_blocked):
        log(f"[exec] 台账已收口，清掉实现暂缓：{_stale}")
    exec_cfg = auto_exec_config()
    exec_workers = dict(load_state().get("exec_workers") or {})
    if dry:
        dispatch_exec(True, exec_blocked, exec_workers)
    elif exec_cfg["enabled"]:
        exec_results, exec_workers = dispatch_exec(False, exec_blocked, exec_workers)
        exec_landed = [r for r in exec_results if r.get("outcome") == "landed"]
    else:
        log("[exec] auto_exec 未开启（面板开关 / control.json），跳过自动实现")

    # 空间自救：清孤儿后端 + 零损失 worktree + git gc（全部 fail-open，可一键关）。
    # 放在派活之后是硬要求：这一步要扫几百个现场，跑在派活前面时会挤掉一整轮派发。
    try:
        _reap_backend_orphans()
        _reap_dead_worktrees()
        _free = _disk_free_gb()
        if _free is None or _free < REAP_PRESSURE_FREE_GB:
            _git_gc_maintenance()
    except Exception as exc:
        log(f"[safety] 回收阶段异常（忽略，继续本轮）：{exc}")

    # Persist the pruned blocked set before the first network refresh. The later
    # save only refreshes worker metadata; otherwise a closed item can be
    # restored from stale state before tick completes and occupy an exec slot.
    try:
        _early = load_state()
        _early["exec_blocked"] = exec_blocked
        _early["exec_workers"] = {
            "exec_jobs": {
                rid: {k: v for k, v in job.items()
                      if k not in ("proc", "fh", "item", "cfg", "codex")}
                for rid, job in (exec_workers.get("exec_jobs") or {}).items()
                if not job.get("partial")
            }
        }
        save_state(_early)
    except Exception as exc:
        log(f"[exec] 工作槽状态提前落盘失败（继续）：{exc!r}")

    # Refresh truth after local work has already started. A slow/unreachable
    # remote must not hold worker dispatch behind the network timeout.
    rc, out = run_kgctl(["tick", "--no-converge"], env_extra={"KG_TICK_LIGHT": "1"})
    log_block("tick", rc, out)
    convergence = parse_convergence(out)
    log(f"[converge] 可收拢族 {convergence['families']}，本轮新建 {convergence['made']}")

    # 归档「内容已全部在 main」的重复分支。这一步只写台账（加法），不动任何代码，
    # 也不动任何源分支，所以放进守护的安全子集。用 --fast（patch-id）保证秒级返回。
    superseded = 0
    src, sout = run_kgctl(["supersede", "--go", "--fast"], timeout=SUPERSEDE_TIMEOUT,
                              env_extra={"KG_SUPERSEDE_LIGHT": "1"})
    log_block("supersede", src, sout)
    m = SUPERSEDE_DONE_RE.search(sout)
    if m:
        superseded = int(m.group(1))

    landed, failures = [], []
    if rc == 124:
        # 超时必须算失败：旧实现里 tick 超时的 out 是一条说明文案（非空），于是既不进
        # failures 也不报警——30 分钟的黑洞在面板上显示成 last_failures=0。
        failures.append(("(tick)", "tick_timeout"))
    elif rc != 0 and not out.strip():
        failures.append(("(tick)", "tick_error"))

    auto_land = control_state.load().get("auto_land", True)
    summary = {"other": 0, "merged": 0, "conflict": 0, "unresolved": 0}
    land_blocked = dict(load_state().get("land_blocked") or {})
    attempts = 0
    while auto_land and attempts < max_land:
        rc, qout = run_kgctl(["queue", "--light"])
        clean, summary, files_by_branch = parse_queue(qout)
        if rc != 0:
            log_block("queue", rc, qout)
            failures.append(("(queue)", "queue_error"))
            break
        clean = [b for b in clean if b not in land_blocked and files_by_branch.get(b, 0) > 0]
        if not clean:
            log(f"[queue] 可落盘 0 个（merged={summary['merged']} "
                f"conflict={summary['conflict']}）")
            break
        target = clean[0]
        attempts += 1
        if dry:
            log(f"[dry] 本轮会 land: {target}（队列 {len(clean)} 个）")
            break
        lrc, lout = run_kgctl(["land", target], timeout=LAND_TIMEOUT)
        kind = classify_land(lout, lrc)
        log_block(f"land {target} -> {kind}", lrc, lout)
        if kind == "landed":
            landed.append(target)
            land_blocked.pop(target, None)
            continue
        if kind == "already":
            land_blocked.pop(target, None)
            continue
        if kind in ("locked", "noop"):
            break
        failures.append((target, kind))
        if kind in ("merge_fail", "gate_fail", "conflict", "unresolved_markers"):
            land_blocked[target] = {"reason": kind, "ts": _now()}
            log(f"[land] {target} 记进 land_blocked（{kind}），后续周期跳过，"
                f"避免反复占用自动落盘")
        break

    # 自动移植「只差几个提交」的冲突分支：cherry-pick 到当前 main，过门禁才落盘，
    # 冲突即整体放弃。默认关闭，由状态根 ops/central.config.json 的 auto_port 打开。
    # 一周期内把所有候选试一遍：成功的落盘并继续，冲突的进 port_blocked 后跳过，
    # 于是循环不断收窄，直到没有候选或达到上限。试过且不行的分支后续周期不再重试。
    ported = []
    port_blocked = dict(load_state().get("port_blocked") or {})
    port_cfg = auto_port_config()
    if not dry and port_cfg["enabled"] and not (rc != 0 and not out.strip()):
        port_state = load_state()
        port_scan_ts = float(port_state.get("last_port_scan_ts") or 0)
        port_scan_hit = bool(port_state.get("last_port_scan_hit"))
        now = time.time()
        if not port_scan_hit and now - port_scan_ts < PORT_SCAN_COOLDOWN:
            log(f"[port] 上一轮无候选扫描在 {PORT_SCAN_COOLDOWN}s 冷却期内，跳过本轮全量扫描")
        else:
            for _ in range(PORT_MAX_ATTEMPTS):
                prc, pout = run_kgctl(["port", "--auto", "--go",
                                       "--max-unlanded", str(port_cfg["max_unlanded"])],
                                      timeout=PORT_TIMEOUT,
                                      env_extra={"KG_PORT_SKIP": ":".join(port_blocked)})
                log_block("port(auto)", prc, pout)
                pm = PORT_RESULT_RE.search(pout)
                if not pm:
                    # 没有候选 / 扫描超时：把扫描时间记回 state，下一轮不重扫同一批
                    # 无候选分支，也不让超时后的下一次循环再白等几分钟。
                    port_state = load_state()
                    port_state["last_port_scan_ts"] = str(int(time.time()))
                    port_state["last_port_scan_hit"] = False
                    save_state(port_state)
                    if prc == 124:
                        log(f"[port] 扫描超时后进入 {PORT_SCAN_COOLDOWN}s 冷却，避免下一轮重复全量扫描")
                    break
                port_state["last_port_scan_hit"] = True
                save_state(port_state)
                pb, po = pm.group(1), pm.group(2)
                if po == "landed":
                    ported.append(pb)
                    port_blocked.pop(pb, None)
                    continue  # main 已前进，下一轮重新挑
                port_blocked[pb] = {"reason": po, "ts": _now()}
        # 移植推进了 main，可能让别的分支的改动「现在全部在 main 了」——再扫一次归档。
        if ported:
            src2, sout2 = run_kgctl(["supersede", "--go", "--fast"], timeout=SUPERSEDE_TIMEOUT,
                                       env_extra={"KG_SUPERSEDE_LIGHT": "1"})
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
    if not dry and resolve_cfg["enabled"] and not (rc != 0 and not out.strip()):
        for _ in range(RESOLVE_MAX_ATTEMPTS):
            skip = ":".join(b for b in resolve_blocked if b)
            rrc, rout = run_kgctl(["resolve", "--auto", "--go",
                                   "--max-files", str(resolve_cfg["max_files"]),
                                   "--max-lines", str(resolve_cfg["max_lines"])],
                                  timeout=RESOLVE_TIMEOUT,
                                  env_extra={"KG_RESOLVE_SKIP": skip,
                                             "KG_RESOLVE_LIGHT": "1"})
            log_block("resolve(auto)", rrc, rout)
            rm = RESOLVE_RESULT_RE.search(rout)
            if not rm:
                break  # 没有候选了
            rb, ro = rm.group(1), rm.group(2)
            if ro == "landed":
                resolve_landed_any = True
            elif ro == "needs_agent":
                resolved_tasks.append(rb)
            elif ro in ("too_big", "blocked", "clean"):
                resolve_blocked[rb] = {"reason": ro, "ts": _now()}
            elif ro == "gate_fail":
                resolve_gate_fail.append(rb)
                resolve_blocked[rb] = {"reason": ro, "ts": _now()}
            # 每条结果都让候选集变小（落盘 / 占用 / 跳过），继续拆下一条。
            if resolve_landed_any:
                break  # 落盘推进了 main，下一周期基于新 main 重挑
    elif not resolve_cfg["enabled"]:
        log("[resolve] auto_resolve 未开启（ops/central.config.json），跳过冲突拆分")

    # 落过盘就再刷一次控制面，让看板/快照反映新 main。
    if (landed or ported or resolve_landed_any or exec_landed) and not dry:
        rc2, out2 = run_kgctl(["tick", "--no-converge"], env_extra={"KG_TICK_LIGHT": "1"})
        log_block("tick(post-land)", rc2, out2)

    result = {"ts": time.time(), "iso": _now(), "dry": dry,
              "summary": summary, "landed": landed, "failures": failures,
              "superseded": superseded,
              "ported": ported,
              "resolved_tasks": resolved_tasks,
              "resolve_gate_fail": resolve_gate_fail,
              "exec_results": exec_results,
              "convergence": convergence}

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
    exec_failed = [r for r in exec_results
                   if r.get("outcome") not in ("landed", "channel_unavailable")]
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
    state["land_blocked"] = land_blocked
    state["resolve_blocked"] = resolve_blocked
    state["port_blocked"] = port_blocked
    state["exec_blocked"] = exec_blocked
    # Persist only JSON-safe worker metadata. Popen handles and file handles
    # are process-local and must never leak into state.json.
    state["exec_workers"] = {
        "exec_jobs": {
            rid: {k: v for k, v in job.items()
                  if k not in ("proc", "fh", "item", "cfg", "codex")}
            for rid, job in (exec_workers.get("exec_jobs") or {}).items()
            if not job.get("partial")
        }
    }
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
    if os.environ.get("KG_EXEC_FAST_MODEL"):
        env["KG_EXEC_FAST_MODEL"] = os.environ["KG_EXEC_FAST_MODEL"]
    if os.environ.get("KG_EXEC_MODEL"):
        env["KG_EXEC_MODEL"] = os.environ["KG_EXEC_MODEL"]
    # 自包含状态根不是代码主工作树，不能靠默认值找到前端/后端 node_modules。
    # 把共享检出显式带进 launchd，kgctl gate / auto_exec 才能真的跑门禁。
    if os.environ.get("KG_SHARED"):
        env["KG_SHARED"] = str(Path(os.environ["KG_SHARED"]).expanduser())
    # KG_REPO 是账本所在；自包含安装下它和 KG_TRUNK 不一定同一个目录。
    repo_env = os.environ.get("KG_REPO")
    trunk_env = os.environ.get("KG_TRUNK")
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
        "ThrottleInterval": 60,
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
            and t.get("slug") not in settled and t.get("branch") not in settled]


def show_status() -> int:
    state = load_state()
    workers = _hydrate_exec_jobs(dict(state.get("exec_workers") or {}))
    state["exec_workers"] = workers
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


ACTIVE_BREATH_S = float(os.environ.get("KG_ACTIVE_BREATH_S", "45"))


def _exec_pool_busy() -> bool:
    """这一拍还要不要快点回来？只读判断，任何异常都当作不忙。

    两个来源都算「忙」：
      1) 进程表里真有 codex exec 在跑；
      2) 状态里还挂着 worker 槽位 —— 哪怕进程已经没了，也必须尽快回来
         收口/回收。只看进程表会漏掉这一种：worker 退出后槽位还在，
         但守护以为「没事干」于是睡满 15 分钟，槽位一直占着并发名额，
         整池子看上去满员、实际一个会话都没有（实测空转过一次）。
    """
    try:
        st = load_state()
        jobs = ((st.get("exec_workers") or {}).get("exec_jobs") or {})
        if jobs:
            return True
    except Exception:
        pass
    try:
        out = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines():
            if "codex" in line and " exec " in line and "-C" in line:
                return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    return False


def loop(interval_min: int, max_land: int, dry: bool) -> int:
    if not acquire_lock():
        log(f"已有实例在跑（{DAEMON_LOCK}），本次退出")
        return 1
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
            # 自适应心跳：worker 一跑完就必须尽快收口，等满 15 分钟等于把
            # 一个已完成的结果压在槽位里发呆。有 vivo worker 或刚派过新活时
            # 用短呼吸，只有真正空闲时才退回完整周期。
            busy = _exec_pool_busy()
            sleep_for = max(5, interval_min * 60 - (time.time() - started))
            if busy and not was_paused:
                sleep_for = min(sleep_for, ACTIVE_BREATH_S)
                log(f"[beat] 有 worker 在跑，{sleep_for:.0f}s 后回来收口")
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
