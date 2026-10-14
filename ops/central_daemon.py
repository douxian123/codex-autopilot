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
EXEC_MAX_PER_CYCLE = 1
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
            for key in ("max_per_cycle", "timeout"):
                try:
                    out[key] = max(1, int(cfg.get(key, out[key])))
                except (TypeError, ValueError):
                    pass
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
    return opens[:limit]


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
        log(f"[exec] {rid} 没有产生改动，记为无变化")
        blocked[rid] = {"reason": "no_changes", "ts": _now()}
        _cleanup_exec(repo, wt, branch)
        result["outcome"] = "no_changes"
        return result

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


def dispatch_exec(dry: bool, blocked: dict) -> list:
    """每周期派最多 max_per_cycle 条待办。返回本轮结果列表。"""
    cfg = auto_exec_config()
    if not cfg["enabled"]:
        return []
    codex = _codex_bin()
    if not codex:
        log("[exec] auto_exec 已开，但找不到 codex 可执行文件，跳过")
        return []
    results = []
    for item in _ledger_open_items():
        rid = item.get("id")
        if rid in blocked:
            continue
        if _looks_like_command(item.get("title")) or _looks_like_command(item.get("detail")):
            log(f"[exec] {rid} 输入像命令残片（{(item.get('title') or '')[:24]!r}），"
                f"不派给执行器")
            blocked[rid] = {"reason": "looks_like_command",
                            "title": (item.get("title") or "")[:60], "ts": _now()}
            continue
        if dry:
            log(f"[dry] 会派发 {rid}（{item.get('kind')}）{item.get('title')}")
            break
        results.append(_dispatch_one(item, cfg, blocked))
        if len(results) >= cfg["max_per_cycle"]:
            break
    return results


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
    if control_state.is_paused():
        log("[pause] 周期开始前被暂停：只刷新状态，不做路由/落盘")
        return {"ts": time.time(), "iso": _now(), "dry": dry,
                "paused": True, "summary": {}, "landed": [], "failures": []}
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
                                  env_extra={"KG_RESOLVE_SKIP": skip})
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
        exec_results = dispatch_exec(False, exec_blocked)
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
