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
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE_ROOT = HERE.parent
KGCTL = HERE / "kgctl.py"


from plugin_binding import apply_plugin_env  # noqa: E402

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
LAND_TIMEOUT = 1800
TICK_TIMEOUT = 1800
SUPERSEDE_TIMEOUT = 900
PORT_TIMEOUT = 1800
# 一个周期里最多尝试移植几条候选。每条候选要么落盘（走完整 build 门禁），要么撞
# 冲突被记进 port_blocked 后跳过；两种都会让下一轮候选集变小，所以循环会自然收敛。
PORT_MAX_ATTEMPTS = 6

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
    """调用 kgctl，返回 (rc, 合并后的 stdout+stderr)。"""
    cmd = [sys.executable, str(KGCTL), *args]
    env = {**os.environ, **env_extra} if env_extra else None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           cwd=str(CODE_ROOT), timeout=timeout, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"kgctl {' '.join(args)} 超时（>{timeout}s）"
    except OSError as exc:
        return 125, f"调用 kgctl 失败: {exc}"


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
        return out
    try:
        data = json.loads((repo / "ops" / "central.config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
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
    if "× 失败 → 丢弃" in out or "build × 失败" in out or "构建门禁 × 失败" in out:
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


def run_cycle(dry: bool, max_land: int) -> dict:
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
    rc, out = run_kgctl(["tick"])
    log_block("tick", rc, out)
    convergence = parse_convergence(out)
    log(f"[converge] 可收拢族 {convergence['families']}，本轮新建 {convergence['made']}")

    # 归档「内容已全部在 main」的重复分支。这一步只写台账（加法），不动任何代码，
    # 也不动任何源分支，所以放进守护的安全子集。用 --fast（patch-id）保证秒级返回。
    superseded = 0
    src, sout = run_kgctl(["supersede", "--go", "--fast"], timeout=SUPERSEDE_TIMEOUT)
    log_block("supersede", src, sout)
    m = SUPERSEDE_DONE_RE.search(sout)
    if m:
        superseded = int(m.group(1))

    landed, failures = [], []
    if rc != 0 and not out.strip():
        failures.append(("(tick)", "tick_error"))

    summary = {"other": 0, "merged": 0, "conflict": 0, "unresolved": 0}
    attempts = 0
    while attempts < max_land:
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
        if dry:
            log(f"[dry] 本轮会 land: {target}（队列 {len(clean)} 个）")
            break
        lrc, lout = run_kgctl(["land", target], timeout=LAND_TIMEOUT)
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

    # 落过盘就再刷一次控制面，让看板/快照反映新 main。
    if (landed or ported) and not dry:
        rc2, out2 = run_kgctl(["tick"])
        log_block("tick(post-land)", rc2, out2)

    result = {"ts": time.time(), "iso": _now(), "dry": dry,
              "summary": summary, "landed": landed, "failures": failures,
              "superseded": superseded,
              "ported": ported,
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
    state["port_blocked"] = port_blocked
    save_state(state)

    log(f"--- cycle done: landed={len(landed)} failures={len(failures)} "
        f"conflict={summary['conflict']} converge_families={convergence['families']} ---")
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
    }
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
        print(f"累计自动落盘   {state.get('total_landed', 0)} 个分支")
        print(f"累计归档重复   {state.get('total_superseded', 0)} 个分支")
        print(f"累计自动移植   {state.get('total_ported', 0)} 个分支")
        blocked = state.get("port_blocked") or {}
        if blocked:
            print(f"移植暂缓       {len(blocked)} 个分支（冲突/门禁不过，等 converge）")
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
            if _stop:
                break
            sleep_for = max(5, interval_min * 60 - (time.time() - started))
            log(f"下一轮 {sleep_for/60:.1f} 分钟后")
            slept = 0.0
            while slept < sleep_for and not _stop:
                time.sleep(min(5.0, sleep_for - slept))
                slept += 5.0
    finally:
        release_lock()
        log("常驻退出")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="自动挡常驻执行器（安全子集：只自动合无冲突分支）")
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
