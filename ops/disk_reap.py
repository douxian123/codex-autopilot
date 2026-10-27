#!/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/bin/python3.14
"""磁盘/进程自动止血（每小时一次，独立于自动挡是否暂停）。

为什么单独做：2026-10-22 磁盘从 8.8G 被吃到 108Mi，导致所有 provider 一起断流
（CLIENT-GONE / BrokenPipe / stream disconnected），和线路完全无关。
自动挡暂停后它内部的回收器不再运行 → 磁盘必然会再被吃满。这个脚本专职干两件事：

  1. 回收「分支已并入 main + 工作区干净 + 没人在用 + 闲置够久」的 worktree；
  2. 杀掉挂在已无 codex exec 的 worktree 上的 node 后端（100% CPU 僵尸）；
  3. 清理 kg 流水线遗留在 /private/tmp 的临时目录（闲置够久、无进程占用、
     不碰仍登记且带未落提交的 git worktree）。

纪律：fail-open（任何异常只记一行日志、退出码 0）、可一键关（KG_REAP=0）、
只删安全对象（有未提交改动/未落提交的一律跳过）。用法：--dry-run 只打印不执行。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

def _resolve_ops() -> Path:
    """Locate the runtime ops/ that is ACTUALLY INSTALLED right now.

    Why this is not a constant any more (2026-10-27 事故):
      这里原本硬编码 /Users/danta/plugins/codex-autopilot/ops，那是插件装好时
      留下的一份 ops/ 副本，它在 2026-10-21 就冻结了。install.sh 每次发版只会写
      releases/<version>/ops/，从不更新那份副本。结果：修好的 workspace_guard
      发布了 44 个版本，这个每小时跑的回收器一直在跑 10-21 的旧代码，回收判据
      永远卡在「dirty」，每小时回收 0 个，磁盘反复被写爆、人只能反复手工清。
      用户问「有没有一种干净机制能长期稳定跑」——机制有，但它跑的是化石代码。

    现在改为跟随 install-state.json 记录的当前版本；读不到再退回 marketplace
    登记的路径；再不行才用旧的固定副本（仍然可用，只是不会自动升级）。
    """
    def _abs(raw: str) -> Path | None:
        """marketplace/install-state 里的路径是相对 ~ 的，例如
        `./plugins/codex-autopilot/releases/0.6.0-preview.44`。"""
        if not raw:
            return None
        cand = Path(raw).expanduser()
        if not cand.is_absolute():
            cand = Path.home() / raw.lstrip("./")
        return cand

    def _ok(ops: Path | None) -> bool:
        return bool(ops) and (ops / "central_daemon.py").exists()

    state = Path.home() / "plugins/codex-autopilot/install-state.json"
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
        cand = _abs(str(data.get("source") or ""))
        if _ok(cand / "ops" if cand else None):
            return (cand / "ops")
    except Exception:
        pass
    mkt = Path.home() / ".agents/plugins/marketplace.json"
    try:
        data = json.loads(mkt.read_text(encoding="utf-8"))
        for plugin in (data.get("plugins") or []):
            if not isinstance(plugin, dict) or plugin.get("name") != "codex-autopilot":
                continue
            src = plugin.get("source")
            raw = src.get("path") if isinstance(src, dict) else src
            cand = _abs(str(raw or ""))
            if _ok(cand / "ops" if cand else None):
                return (cand / "ops")
    except Exception:
        pass
    return Path("/Users/danta/plugins/codex-autopilot/ops")


OPS = _resolve_ops()
LOG = Path("/Users/danta/.codex-autopilot/disk-reap.log")
DRY = "--dry-run" in sys.argv

# 只清 kg 自动挡/门禁自己生成的前缀，绝不碰用户的 /private/tmp。
KG_TMP_RE = re.compile(r"^kg(?:-|_|\.)")
# 闲置多久算可清：默认 6 小时，避免清掉正在跑的现场；压力可收紧。
KG_TMP_AGE_H = float(os.environ.get("KG_TMP_AGE_H", "6"))
# 每小时独立回收的力度，与 plist 注入一致；直接运行也能验证。
REAP_MAX_PER_ROUND = int(os.environ.get("KG_REAP_MAX_PER_ROUND", "40"))
REAP_TIME_BUDGET_S = float(os.environ.get("KG_REAP_TIME_BUDGET_S", "60"))
REAP_MIN_IDLE_H = float(os.environ.get("KG_REAP_MIN_IDLE_H", "1"))


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        if LOG.exists() and LOG.stat().st_size > 2_000_000:
            LOG.write_text("")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def free_gb():
    try:
        st = os.statvfs("/System/Volumes/Data")
        return st.f_bavail * st.f_frsize / 1e9
    except Exception:
        return -1.0


def _process_lines() -> str:
    try:
        out = subprocess.run(["ps", "ax", "-o", "command="],
                             capture_output=True, text=True, timeout=10)
    except Exception:
        return ""
    return (out.stdout or "").lower() if out.returncode == 0 else ""


def _referenced(path: Path, procs: str) -> bool:
    return str(path).rstrip("/").lower() in procs


def _git_common_dir(path: Path):
    """Return the common .git dir for a linked worktree, or None."""
    dotgit = path / ".git"
    if not dotgit.is_file():
        return None
    try:
        line = dotgit.read_text(errors="replace").strip()
        if not line.startswith("gitdir:"):
            return None
        gitdir = Path(line.split(":", 1)[1].strip().strip('"')).resolve()
    except Exception:
        return None
    repo = gitdir
    while repo.name != ".git" and repo.parent != repo:
        repo = repo.parent
    if repo.name == ".git" and (repo / "worktrees").exists():
        return repo
    return None


def _registered_worktree(path: Path) -> bool:
    common = _git_common_dir(path)
    if common is None:
        return False
    try:
        wt_name = Path((path / ".git").read_text(errors="replace")
                       .split(":", 1)[1].strip().strip('"')).name
    except Exception:
        return False
    return (common / "worktrees" / wt_name).exists()


def _remove_one_kg_tmp(path: Path, procs: str, guard=None) -> bool:
    """Remove one stale kg temp object when nothing references it."""
    if _referenced(path, procs):
        return False
    if path.is_dir():
        if _registered_worktree(path):
            if guard is None:
                return False
            try:
                repo = _git_common_dir(path).parent
                branch = ""
                brc = subprocess.run(
                    ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True).stdout.strip()
                if brc and brc not in ("HEAD", "main"):
                    branch = brc
                assessment = guard.worktree_removable(
                    repo, path, branch, "main", commands=procs,
                    backup_unique=False)
                if not assessment.get("ok"):
                    return False
                ok, _ = guard.remove_worktree(repo, path)
                return ok
            except Exception:
                return False
        if (path / ".git").is_dir():
            # 独立 clone：只有无未提交改动、无未推送分支且无人占用才删。
            try:
                if subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                                  capture_output=True, text=True,
                                  timeout=30).stdout.strip():
                    return False
                head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                                      capture_output=True, text=True,
                                      timeout=30).stdout.strip()
                if not head:
                    return False
                merged = subprocess.run(
                    ["git", "-C", str(path), "branch", "-r", "--contains", head],
                    capture_output=True, text=True, timeout=30).stdout
                if "origin/main" not in merged:
                    return False
                if subprocess.run(["git", "-C", str(path), "rev-list", "--count",
                                   "HEAD", "--not", "--remotes"],
                                  capture_output=True, text=True,
                                  timeout=30).stdout.strip() not in ("", "0"):
                    return False
            except Exception:
                return False
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError:
        return False


def _reap_kg_tmp(dry: bool = False, guard=None) -> int:
    """Remove kg-generated /private/tmp objects idle past KG_TMP_AGE_H."""
    if os.environ.get("KG_TMP_REAP", "1").strip().lower() in ("0", "off", "false"):
        return 0
    tmp = Path("/private/tmp")
    if not tmp.is_dir():
        return 0
    procs = _process_lines()
    cutoff = time.time() - KG_TMP_AGE_H * 3600
    removed = 0
    for child in sorted(tmp.iterdir()):
        if not KG_TMP_RE.match(child.name):
            continue
        try:
            age = child.stat().st_mtime
        except OSError:
            continue
        if age > cutoff:
            continue
        if dry:
            # 预演必须只读：只统计“满足条件、真跑时会删”的对象。
            if not _referenced(child, procs):
                removed += 1
            continue
        if _remove_one_kg_tmp(child, procs, guard=guard):
            removed += 1
    return removed


def main():
    if os.environ.get("KG_REAP", "1").strip().lower() in ("0", "off", "false"):
        log("KG_REAP=0，跳过")
        return 0
    sys.path.insert(0, str(OPS))
    try:
        import central_daemon as d
    except Exception as exc:
        log(f"导入 central_daemon 失败（忽略）：{type(exc).__name__}: {exc}")
        return 0
    d.REAP_MAX_PER_ROUND = REAP_MAX_PER_ROUND
    d.REAP_TIME_BUDGET_S = REAP_TIME_BUDGET_S
    d.REAP_MIN_IDLE_H = REAP_MIN_IDLE_H

    before = free_gb()
    try:
        if DRY:
            picked = []
            orig = d.workspace_guard.remove_worktree
            d.workspace_guard.remove_worktree = lambda repo, wt: (picked.append(wt.name) or (True, "dry"))
            try:
                n = d._reap_dead_worktrees()
            finally:
                d.workspace_guard.remove_worktree = orig
            t = _reap_kg_tmp(dry=True, guard=d.workspace_guard)
            log(f"dry-run: 会回收 {n} 个 worktree / {t} 个 kg tmp -> {picked}")
        else:
            n = d._reap_dead_worktrees()
            k = d._reap_backend_orphans()
            t = _reap_kg_tmp(guard=d.workspace_guard)
            after = free_gb()
            log(f"回收 worktree {n} 个 / 孤儿后端 {k} 个 / kg tmp {t} 个；"
                f"磁盘 {before:.1f}G → {after:.1f}G")
    except Exception as exc:
        log(f"回收异常（忽略）：{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
