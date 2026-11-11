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

CODEX_SIGN_CLONE_TARGETS = (
    "com.google.Chrome.code_sign_clone",
    "com.bot.pc.doubao.browser.code_sign_clone",
)
CODEX_SIGN_CLONE_GRACE_MIN = float(os.environ.get("KG_CODESIGN_CLONE_GRACE_MIN", "20"))

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

    here = Path(__file__).resolve().parent
    if _ok(here):
        return here

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
    # The executable own versioned ops directory is the only safe fallback.
    # The old non-versioned copy is commonly absent after plugin migration;
    # returning it made this hourly guard silently stop working.
    return here


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
# 剥离重复副本的闲置门槛：默认 24h，避免动到正在进行的现场。
STRIP_IDLE_H = float(os.environ.get("KG_STRIP_IDLE_H", "24"))
# 旧会话原始 JSONL 的压缩门槛与预算。只压缩“未被进程引用”的旧文件，
# 压缩后先做完整性校验，校验失败绝不删除原文件。
SESSION_COMPACT_MIN_AGE_H = float(os.environ.get("KG_SESSION_COMPACT_MIN_AGE_H", "6"))
SESSION_COMPACT_MIN_MB = float(os.environ.get("KG_SESSION_COMPACT_MIN_MB", "20"))
SESSION_COMPACT_MAX_FILES = int(os.environ.get("KG_SESSION_COMPACT_MAX_FILES", "8"))
SESSION_COMPACT_UNDER_FREE_GB = float(
    os.environ.get("KG_SESSION_COMPACT_UNDER_FREE_GB", "15"))


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


def _zstd_path() -> str | None:
    """Find the zstd binary even under launchd's minimal PATH.

    launchd starts this reaper with PATH=/usr/bin:/bin:/usr/sbin:/sbin, which
    does not include /opt/homebrew/bin -- the only place zstd is installed on
    this machine. Measured 2026-11-11: every hourly run logged
    `FileNotFoundError: 'zstd'` once per candidate file and compressed nothing,
    so the disk-pressure path it was written for silently did nothing while the
    disk stayed full -- and a full disk is exactly what keeps the reaper in its
    15-minute idle mode. Resolve the binary explicitly instead of trusting PATH.
    """
    found = shutil.which("zstd")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/zstd", "/usr/local/bin/zstd", "/usr/bin/zstd"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def compact_session_logs(dry: bool = False) -> tuple[int, int]:
    """Compress old Codex JSONL transcripts with an integrity check.

    Session history is valuable user context, so this path never removes a
    transcript before the compressed archive has been decompressed and compared
    byte-for-byte with the source.
    """
    if os.environ.get("KG_SESSION_COMPACT", "1").strip().lower() in ("0", "off", "false"):
        return 0, 0
    if free_gb() >= SESSION_COMPACT_UNDER_FREE_GB:
        return 0, 0
    home = Path.home()
    codex_home = Path(os.getenv("CODEX_HOME", str(home / ".codex"))).expanduser()
    sessions = codex_home / "sessions"
    if not sessions.is_dir() and codex_home.name == ".codex":
        sessions = home / ".codex/sessions"
    if not sessions.is_dir():
        return 0, 0
    cutoff = time.time() - SESSION_COMPACT_MIN_AGE_H * 3600
    min_bytes = int(SESSION_COMPACT_MIN_MB * 1024 * 1024)
    procs = _process_lines()
    candidates = []
    for path in sessions.rglob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size < min_bytes or st.st_mtime > cutoff:
            continue
        if _referenced(path, procs) or _referenced(path.with_name(path.name + ".zst"), procs):
            continue
        candidates.append((st.st_mtime, st.st_size, path))
    candidates.sort(reverse=True)
    zstd = None
    if not dry:
        zstd = _zstd_path()
        if not zstd:
            log("找不到 zstd，跳过旧会话压缩（原文件全部保留）")
            return 0, 0
    compressed = 0
    freed = 0
    for _mtime, size, path in candidates[:SESSION_COMPACT_MAX_FILES]:
        archive = path.with_name(path.name + ".zst")
        if archive.exists():
            continue
        if dry:
            compressed += 1
            freed += size
            continue
        tmp_archive = path.with_name(path.name + ".zst.tmp")
        try:
            proc = subprocess.run(
                [zstd, "-19", "-f", "-o", str(tmp_archive), str(path)],
                capture_output=True, text=True, timeout=600)
            if proc.returncode != 0 or not tmp_archive.is_file():
                log(f"会话压缩失败，保留原文件：{path} rc={proc.returncode} {proc.stderr[:200]}")
                continue
            check = subprocess.run(
                [zstd, "-dc", str(tmp_archive)],
                capture_output=True, timeout=600)
            if check.returncode != 0 or check.stdout != path.read_bytes():
                log(f"会话压缩校验失败，保留原文件：{path}")
                continue
            tmp_archive.replace(archive)
            path.unlink()
            compressed += 1
            freed += max(0, size - archive.stat().st_size)
            log(f"压缩旧会话 {path.name}: {size} -> {archive.stat().st_size} bytes")
        except Exception as exc:
            log(f"会话压缩异常，保留原文件 {path}: {type(exc).__name__}: {exc}")
        finally:
            try:
                if tmp_archive.exists():
                    tmp_archive.unlink()
            except OSError:
                pass
    return compressed, freed


def _code_sign_clone_xdirs():
    """Yield temporary X directories where macOS app signing clones live."""
    tmp = os.environ.get("TMPDIR", "/var/folders").rstrip("/")
    seen = set()
    for base in (os.path.dirname(tmp), tmp, "/var/folders"):
        cur = base
        for _ in range(4):
            cand = os.path.join(cur, "X")
            if os.path.isdir(cand) and cand not in seen:
                seen.add(cand)
                yield cand
            cur = os.path.dirname(cur)


def _path_in_use(path: str) -> bool:
    """Return True when an open process references this clone path.

    Do not use `lsof +D` for one clone at a time: macOS may report files in
    sibling clones under the same parent as well, which made stale and active
    Chrome copies look identical. Ask open files explicitly, then keep the
    directory when the probe itself fails.
    """
    try:
        out = subprocess.run(
            ["lsof", "-n", "-F", "n"],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if out.returncode not in (0, 1):
            return True
        needle = path.rstrip("/") + "/"
        return any(line.startswith("n") and needle in line[1:] for line in out.stdout.splitlines())
    except Exception:
        return True


def _tree_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def reap_code_sign_clones(dry: bool = False) -> tuple[int, int]:
    """Remove stale macOS app-signing clones that are no longer in use."""
    removed = 0
    freed = 0
    cutoff = time.time() - CODEX_SIGN_CLONE_GRACE_MIN * 60
    for xdir in _code_sign_clone_xdirs():
        for parent_name in CODEX_SIGN_CLONE_TARGETS:
            root = os.path.join(xdir, parent_name)
            if not os.path.isdir(root):
                continue
            try:
                entries = sorted(os.listdir(root))
            except OSError:
                continue
            for ent in entries:
                if not ent.startswith("code_sign_clone."):
                    continue
                path = os.path.join(root, ent)
                if not os.path.isdir(path) or os.path.islink(path):
                    continue
                try:
                    if os.path.getmtime(path) > cutoff:
                        continue
                except OSError:
                    continue
                if _path_in_use(path):
                    log(f"签名临时副本仍在使用，跳过：{path}")
                    continue
                size = _tree_size(path)
                if dry:
                    log(f"dry-run: 会回收签名临时副本 {path}（{size / 1e9:.2f}GB）")
                    removed += 1
                    freed += size
                    continue
                try:
                    shutil.rmtree(path)
                except Exception as exc:
                    log(f"签名临时副本回收失败 {path}: {type(exc).__name__}: {exc}")
                    continue
                removed += 1
                freed += size
                log(f"回收签名临时副本 {path}（{size / 1e9:.2f}GB）")
    return removed, freed


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
                # 判活用 "all"（2026-11-10）：这里删的是 /private/tmp 下已登记、
                # 已并入 main 的 kg-* worktree。只认内核 cwd 会漏掉「一个只读会话
                # 正把这棵树当工作目录」——它的 argv 里没有树路径，只读活动也不
                # 刷新目录 mtime，于是「正在用」和「已废弃」读起来一模一样。
                assessment = guard.worktree_removable(
                    repo, path, branch, "main", commands=procs,
                    backup_unique=False, accept_stripped_regenerable=True,
                    live_guard_mode="all")
                if not assessment.get("ok"):
                    return False
                ok, _ = guard.remove_worktree(repo, path, force_regenerable=True)
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


def reap_regenerable_duplicates(dry: bool = False, guard=None,
                                idle_h: float | None = None) -> tuple[int, int]:
    """剥离闲置 worktree 里与 HEAD 逐字节相同的可重建副本。

    为什么单开一条通道（2026-11-09 实测）：`backend/evidence` + `evidence` 被
    git 跟踪，每个 worktree 都物化一份 ~235MB，本机 86 棵树合计约 15.6GB 完全
    重复。旧逻辑只会 `git checkout --` 把副本「还原」回来，从不真正释放 —— 所以
    磁盘每隔几天就被填满，人只能反复手工清。

    这条路与「回收整棵树」解耦：一棵树可能因为主人在别处还有真实改动而长期
    留在盘上（本来就该留），但它那份 235MB 的重复缓存没有任何理由跟着留。
    判据一条没放松 —— 只动「与 HEAD 逐字节相同」的副本：
      * 可再生产物之外/之内出现任何未跟踪文件或真实改动 -> 整棵树原地不动；
      * 有活进程引用 -> 跳过；
      * 闲置不足 idle_h -> 跳过；
      * 受保护名字（主仓、kg-trunk、kg-live-*）-> 跳过。
    释放的字节全部可从 git 还原（`git checkout -- <dir>`），零信息损失。
    """
    if guard is None:
        return 0, 0
    if os.environ.get("KG_STRIP_DUPLICATES", "1").strip().lower() in ("0", "off", "false"):
        return 0, 0
    root = Path(os.environ.get("KG_WORKSPACE_ROOT")
                or (Path.home() / ".newmax/workspace"))
    if not root.is_dir():
        return 0, 0
    procs = _process_lines()
    hours = REAP_MIN_IDLE_H if idle_h is None else idle_h
    cutoff = time.time() - hours * 3600
    stripped_n = 0
    freed = 0
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.is_symlink() or not d.name.startswith("kg-"):
            continue
        if not (d / ".git").exists():
            continue
        if d.name in guard.PROTECTED_NAMES or d.name.startswith(guard.PROTECTED_PREFIXES):
            continue
        try:
            if d.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        if guard.process_reference(d, procs):
            continue
        # 判活补充：这里删的是「与 HEAD 逐字节相同的可再生产物」，内容可从 git
        # 还原，损失远小于整棵回收；但一个正在读这些证据的会话仍然会被搅乱。
        # 命令行扫描挡不住桌面会话（它的 argv 里没有树路径），所以再问一次内核
        # cwd。命中就跳过这一棵，别的树照常剥离。
        if guard.live_guard_blocker(d, "all"):
            continue
        state = guard.regenerable_dirs_state(d)
        if not any(v == "pristine" for v in state.values()):
            continue
        if guard.dirty_outside_regenerable(d):
            continue
        if dry:
            approx = 0
            for rel, how in state.items():
                if how != "pristine":
                    continue
                try:
                    approx += sum(f.stat().st_size
                                  for f in (d / rel).rglob("*") if f.is_file())
                except OSError:
                    pass
            if approx:
                stripped_n += 1
                freed += approx
            continue
        got = guard.strip_regenerable_tracked(d)
        if got:
            stripped_n += 1
            freed += sum(got.values())
            log(f"剥离可重建重复副本 {d.name}：{', '.join(got)}"
                f"（{sum(got.values()) / 1e9:.2f}GB，可从 HEAD 还原）")
    return stripped_n, freed


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


def _dry_orphan_residue(d) -> int:
    """预演：只数会被删的孤儿残留，不删。"""
    import workspace_guard as g
    root = d._workspace_root()
    if not root.exists():
        return 0
    n = 0
    for child in sorted(root.iterdir()):
        try:
            if not child.is_dir() or child.is_symlink() or "resolve" in child.name:
                continue
        except OSError:
            continue
        if g.orphan_residue(child, d.ORPHAN_MIN_IDLE_H).get("ok"):
            n += 1
    return n


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
    clone_n, clone_freed = reap_code_sign_clones(dry=DRY)
    session_n, session_freed = compact_session_logs(dry=DRY)
    try:
        if DRY:
            # 演练的候选清单由 _reap_dead_worktrees 自己回填，调用方不再猴补
            # remove_worktree —— 那条猴补一旦漏掉，演练就会真删现场。
            picked = []
            n = d._reap_dead_worktrees(dry=True, candidates=picked)
            t = _reap_kg_tmp(dry=True, guard=d.workspace_guard)
            o = _dry_orphan_residue(d)
            sn, sf = reap_regenerable_duplicates(
                dry=True, guard=d.workspace_guard, idle_h=STRIP_IDLE_H)
            log(f"dry-run: 会回收 {clone_n} 个签名临时副本 / {n} 个 worktree / "
                f"{t} 个 kg tmp / {o} 个孤儿残留；会压缩 {session_n} 个旧会话"
                f"（{session_freed / 1e9:.2f}GB）；会剥离 {sn} 棵树的重复副本"
                f"（{sf / 1e9:.2f}GB）-> {picked}")
        else:
            n = d._reap_dead_worktrees(dry=False)
            k = d._reap_backend_orphans()
            t = _reap_kg_tmp(guard=d.workspace_guard)
            # 顺序有讲究：必须在 worktree 回收之后。前者刚把树删掉的现场不会
            # 变成孤儿；只有「删到一半失败」的现场才会落成孤儿，正是这一条接住它。
            o = d._reap_orphan_residue()
            # 顺序有讲究：剥离放在 worktree 回收之后。刚被整棵回收的树不必再
            # 剥离；而回收失败的树正好在这里把它那份重复副本清掉。
            sn, sf = reap_regenerable_duplicates(
                guard=d.workspace_guard, idle_h=STRIP_IDLE_H)
            after = free_gb()
            log(f"回收签名临时副本 {clone_n} 个（{clone_freed / 1e9:.2f}GB）/ worktree {n} 个 / "
                f"孤儿后端 {k} 个 / kg tmp {t} 个 / 孤儿残留目录 {o} 个 / "
                f"压缩旧会话 {session_n} 个（{session_freed / 1e9:.2f}GB）/ "
                f"剥离重复副本 {sn} 棵树（{sf / 1e9:.2f}GB）；"
                f"磁盘 {before:.1f}G → {after:.1f}G")
    except Exception as exc:
        log(f"回收异常（忽略）：{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
