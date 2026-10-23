#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kgctl —— 自动挡的唯一执行入口（stdlib only）

它存在的理由
------------
「一人一 worktree、不许在共享 checkout 提交、按文件声明归属、合并要串行」这些约定
往往早就写进了团队规范；但仓库里仍然会躺着几十个功能分支、几十个 worktree。
原因不是规范写得不好，而是**规范是文字，文字输给顺手**。

所以把最贵的四件事变成命令：

  0. 只有一个总输入框   -> `kgctl intake "…"`（记录+归类+刷真相+给下一步，一步到位）
  1. 输入不再分叉      -> `kgctl inbox` / `kgctl route`  （一个追加式账本，用户无需分类）
  2. 合并推理由系统决定 -> `kgctl queue` / `kgctl land <分支>`
  3. 冲突在写之前可见   -> `kgctl matrix` / `kgctl clash <文件...>`
  4. 冲突不推给人逐条   -> `kgctl converge [--go]`（收成少数 convergence 任务）
  5. 上下文不靠聊天记忆 -> `kgctl context <分支>`（版本化 Context Pack）
  6. 项目状态一眼可见   -> `kgctl dash`（docs/CONTROL_PLANE.html）/ `kgctl status`
  7. 工作区不靠人记忆   -> `kgctl reap --go` / `kgctl sync`
  8. 假冲突不推给人     -> `kgctl supersede --go`（内容已全在 main）/ `kgctl port --go`
                           （只差几个提交，移植到当前 main 后走同一道门禁）
  9. 真冲突拆成单任务   -> `kgctl resolve --auto --go`（每条冲突分支一个独立 worktree，
                            只自动并集 append-only JSONL，其余保留 marker 写任务书；
                           解完 `resolve --finish` 走同一道门禁后自动落盘）
 10. 「缺门禁证据」可自证 -> `kgctl queue --gates`（队列带门禁状态）/ `kgctl gate <分支>`
                            （在分支自己的工作树上真跑门禁，把 command/cwd/exit 写进验收台账，
                             不合并、不动 main；跑过之后该分支才敢被 land）

安全约定（硬编码，不可关）
  * `reap` 默认只预演，必须显式 `--go`。
  * `port` 默认只预演；cherry-pick 冲突即整体放弃并回滚，不留半成品分支。
  * `resolve` 默认只预演；只自动解 .jsonl，其余冲突保留 marker、绝不 ours/theirs；
    落盘走 `land` 的全部门禁，不过就留在工作树里。
  * `land` 任何一步失败都丢弃，main 与源分支都不动；从不 force push、从不改写源分支历史。
  * `matrix/queue/status/sync/clash/inbox 读` 全部只读。
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

MAIN = os.environ.get("KG_MAIN", "main")

# 编排状态（inbox / 台账 / 泳道 / 看板）只有一处权威目录，见 ops/state_root.py。
# 旧实现取「git 主工作树」当账本家，而 truthctl 取「调用所在 worktree」，于是
# 同一个项目能同时存在两份互不相认的账本。现在两个脚本共用同一个解析器。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from plugin_binding import apply_plugin_env  # noqa: E402
from state_root import main_worktree, looks_like_state_root, resolve_state_root  # noqa: E402
import central_config  # noqa: E402

# 自包含安装下，绑定的项目写在插件 .mcp.json 里；显式 KG_REPO 优先。
apply_plugin_env()
REPO = resolve_state_root()
if not looks_like_state_root(REPO):
    sys.exit(f"找不到编排状态根（{REPO}）；请设 KG_REPO 指到带 ops/tasks + ops/truth 的目录"
             "（自包含插件用 ./install.sh <项目> 初始化，不再要求项目里放运行时代码）")
# 项目相关的旋钮集中在 ops/central.config.json（env 可覆盖），见 ops/central_config.py。
CFG = central_config.load(REPO)
MAIN = CFG["main_branch"]
BRANCH_PREFIX = CFG["branch_prefix"]
# 共享检出（git 主工作树）：只在诊断输出和 node_modules 软链时读，不写。
SHARED = central_config.shared_dir(REPO, CFG)
# 代码入口以「被调用的 worktree」为准；REPO 只负责编排状态。
CODE_ROOT = Path(__file__).resolve().parents[1]
# 运行时代码目录：自包含安装下 = 插件目录里的 ops/，传统布局下 = 项目里的 ops/。
# land / supersede 里的真相写入必须用这份代码，而不是目标项目里可能过期的副本。
RUNTIME_OPS = Path(__file__).resolve().parent
OPS = REPO / "ops" / "tasks"
INBOX = OPS / "inbox.jsonl"
LANES = OPS / "lanes.json"
def _git_common_dir() -> Path:
    out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-common-dir"],
                         capture_output=True, text=True)
    p = Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() \
        else (REPO / ".git")
    if not p.is_absolute():
        p = (REPO / p).resolve()
    return p


# land 锁放进共享的 .git（所有 worktree 共用），而不是任何检出目录里：放在检出里
# 会被 `git status` 当成脏改动，而 land 要求主干工作树干净，于是命令把自己锁死。
LOCK = _git_common_dir() / "kgctl-land.lock"


def _lock_blocked() -> bool:
    """锁文件在 ≠ 有人在做：先探活持有者，僵死锁就地隔离（不删除）。

    land/resolve/supersede/converge 都把锁写在共享 .git 下，正常退出会 unlink；但进程
    被 kill -9（调用方超时把命令掐掉就会这样）会留下僵死锁，而 `LOCK.exists()` 只看文件
    在不在，于是**一条死锁会把整个舰队的落盘全堵住**（2026-10-14 实测堵了 25 分钟）。
    所以读锁里的 pid 用 os.kill(pid, 0) 探活；确认持有者已死就把锁挪到旁边留下证据。
    """
    if not LOCK.exists():
        return False
    pid = None
    try:
        pid = int(LOCK.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pid = None
    if pid is not None:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            pass
        except PermissionError:
            return True
    else:
        # 读不出 pid（空文件 / 写坏）：只有老到不可能还在写才当僵死锁。
        try:
            if time.time() - LOCK.stat().st_mtime < 60:
                return True
        except OSError:
            return True
    stale = LOCK.with_name(f"{LOCK.name}.stale-{pid if pid is not None else 'nopid'}")
    try:
        LOCK.replace(stale)
    except OSError:
        return True
    print(f"  [lock] 持有者已不在（pid={pid}），隔离僵死锁 -> {stale}")
    return False


def _acquire_lock(reason: str) -> bool:
    """原子取得共享发布锁；land 与真相推送只能有一个进入。"""
    if _lock_blocked():
        return False
    try:
        fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} {reason}\n")
    return True


def _release_lock() -> None:
    """只释放当前进程持有的锁，避免误删随后接手的另一把锁。"""
    try:
        pid = int(LOCK.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return
    if pid != os.getpid():
        return
    try:
        LOCK.unlink()
    except FileNotFoundError:
        pass


BOARD = REPO / "docs" / "CONTROL_BOARD.md"
DASHBOARD = REPO / "docs" / "CONTROL_PLANE.html"
CONTEXT_DIR = REPO / "ops" / "context"
CONVERGENCE = OPS / "convergence.json"
# 冲突任务的登记与任务书：JSON 记账 + 每任务一份 Markdown 交给 agent。都在 ops/tasks
# 之下，所以自动随真相层提交/推送，不靠聊天记忆。
RESOLVE = OPS / "resolve.json"
RESOLVE_DIR = OPS / "resolve"


def git(*args, check=False):
    # errors="replace"：仓库里混着图片等二进制，diff/cat-file 命中时不能抛解码异常
    p = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                       errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {p.stderr.strip()}")
    return p.stdout.strip()


def slugify(text: str, limit: int = 48) -> str:
    """把分支名 / 文件路径变成稳定的短 slug。"""
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", text or "")
    text = text.strip("-._") or "task"
    return text[:limit].strip("-._") or "task"


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def latest_by_id(rows: list) -> dict:
    """把 append-only 台账收敛成「每个 id 一条当前状态」。

    同一条记录被改写时会追加一行同 id 的新记录，而不是就地覆盖。只按原始行
    过滤状态，就会把已经关闭的 id 用最早那版 open 重新算成待办——2026-10-14
    实测：L-0012 / L-0023 / L-0026 / L-0029 在 ledger 里已是 done，驾驶舱和
    Context Pack 却仍把它们列为待办。凡是按「当前状态」取用的地方都要先过这一层。
    """
    out = {}
    for r in rows:
        if isinstance(r, dict) and r.get("id"):
            out[r["id"]] = r
    return out


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def codex_branches():
    out = git("for-each-ref", "--format=%(refname:short)", "refs/heads")
    return [b for b in out.split("\n") if b.startswith(BRANCH_PREFIX)]


def counts(branch):
    return (int(git("rev-list", "--count", f"{MAIN}..{branch}") or 0),
            int(git("rev-list", "--count", f"{branch}..{MAIN}") or 0))


def repo_of(branch):
    return git("rev-parse", "--show-toplevel") if False else REPO


def changed_files(branch):
    base = git("merge-base", MAIN, branch)
    return [f for f in git("diff", "--name-only", base, branch).split("\n") if f]


def conflict_files(branch):
    """git merge-tree 模拟：返回冲突文件列表（空 = 能干净合）。

    踩坑记录（别退回去）：
      * 冲突时 rc=1，**冲突路径只打印在 stdout 顶部**（第一行是 tree oid），
        不是在 stderr —— 先前的实现去 stderr 找，于是永远解析不到，退化成占位字符串。
      * `--name-only` 会把 Auto-merging/CONFLICT 进度行混进 stdout，
        所以要用不带 --name-only 的形式：按 `mode oid stage<TAB>path` 取 stage==3 的路径。
      * 解析不出任何路径时**不能**返回占位符当真值 —— 那会让"有冲突"的诊断变成假信号
        （实测：53 个分支全被标成抢同一个不存在的文件）。返回空并让调用方按 rc 判断。
    """
    p = subprocess.run(["git", "-C", str(REPO), "merge-tree", "--write-tree", MAIN, branch],
                       capture_output=True, text=True)
    if p.returncode == 0:
        return []
    names, seen = [], set()
    for line in p.stdout.split("\n")[1:]:          # 跳过 tree oid
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].split() and parts[0].split()[-1] == "3":
            path = parts[-1].strip()
            if path and path not in seen:
                names.append(path); seen.add(path)
    if names:
        return sorted(names)
    # 退化：从 CONFLICT 行里找（不同 git 版本措辞不同）
    return sorted(set(re.findall(r"CONFLICT[^:]*:\s*(?:Merge conflict in )?(\S+)", p.stdout)))


def unresolved_marker_files(branch):
    """分支树里已经提交进去的冲突标记文件。

    `conflict_files()` 只回答“分支和当前 main 能不能自动合”，回答不了“分支自己的树
    是否已经坏了”。convergence 分支会把未解合并保留在提交里，等 agent 继续处理；
    如果不单独拦，它会被当成 CLEAN，守护进程就能把一个满是冲突标记的树推上 main。
    两个行首标记同时出现才算，避免把文档里的单侧示例误判为坏树。
    """
    def grep(pattern):
        p = subprocess.run(
            ["git", "-C", str(REPO), "grep", "-l", "-I", "-E", pattern, branch, "--", "."],
            capture_output=True, text=True, errors="replace")
        if p.returncode not in (0, 1):
            return set()
        return {line.strip() for line in p.stdout.splitlines() if line.strip()}

    starts = grep(r"^<<<<<<< ")
    ends = grep(r"^>>>>>>> ")
    return sorted(starts & ends)


def branch_meta(branch: str) -> dict:
    """分支的提交者/时间/标题。给收拢计划做归属提示，不做自动裁决。"""
    return {
        "branch": branch,
        "subject": git("log", "-1", "--format=%s", branch)[:90],
        "author": git("log", "-1", "--format=%an <%ae>", branch),
        "ts": git("log", "-1", "--format=%cI", branch),
    }


def _is_convergence_branch(branch: str) -> bool:
    """codex/converge-* 是自动挡自己生成的收拢分支，不是用户的源工作分支。"""
    return bool(branch) and branch.startswith(f"{BRANCH_PREFIX}converge-")


def _is_resolve_branch(branch: str) -> bool:
    """codex/resolve-* 是自动挡为单条冲突分支准备的执行分支，不是用户的源分支。"""
    return bool(branch) and branch.startswith(f"{BRANCH_PREFIX}resolve-")


def _is_generated_branch(branch: str) -> bool:
    """自动挡生成的任务容器（converge / resolve），一律不算待裁决的源分支。"""
    return _is_convergence_branch(branch) or _is_resolve_branch(branch)


def conflict_groups(rows=None) -> list:
    """把冲突分支按「抢同一批文件」收成少数几个冲突族。

    一个文件被多条分支同时改，不代表每条都要单独问人；先把连通的分支收成
    一个 convergence 任务，用户/agent 面对的是「一族冲突」，不是几十个分支。
    """
    rows = rows if rows is not None else scan()
    # 收拢分支本身是派生产物，绝不能再当源分支喂回收拢。否则它一进入成员集，
    # 族规模就自我膨胀、id 被重算，旧记录变孤儿、worktree 永久半合并（实测：
    # codex/converge-055ddd5718 被当成源分支后，族成员 59→60，id 重算成
    # 39836fb2a8，055 卡死）。所以先把 codex/converge-* 从源集合里剔除。
    conflicts = [r for r in rows if r["kind"] == "CONFLICT"
                 and not _is_generated_branch(r["branch"])]
    parent = {r["branch"]: r["branch"] for r in conflicts}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_file = {}
    for r in conflicts:
        for f in r["conflicts"]:
            by_file.setdefault(f, []).append(r["branch"])
    for branches in by_file.values():
        for b in branches[1:]:
            union(branches[0], b)

    grouped = {}
    for r in conflicts:
        grouped.setdefault(find(r["branch"]), []).append(r)
    out = []
    for members in grouped.values():
        members = sorted(members, key=lambda r: r["branch"])
        branches = [r["branch"] for r in members]
        files = sorted({f for r in members for f in r["conflicts"]})
        digest = hashlib.sha1("\n".join(branches).encode("utf-8")).hexdigest()[:10]
        out.append({
            "id": digest,
            "branches": branches,
            "files": files,
            "rows": members,
            "members": [branch_meta(b) for b in branches],
        })
    out.sort(key=lambda g: (-len(g["branches"]), g["id"]))
    return out


def worktree_map():
    out = git("worktree", "list", "--porcelain")
    m, cur = {}, {}
    for line in out.split("\n"):
        if line.startswith("worktree "):
            cur = {"path": line.split(" ", 1)[1]}
        elif line.startswith("branch "):
            m[line.split(" ", 1)[1].replace("refs/heads/", "")] = cur["path"]
    return m


_BLOB_SHA_CACHE = {}
_BLOB_LINES_CACHE = {}


def _blob_sha(ref, path):
    key = (ref, path)
    if key not in _BLOB_SHA_CACHE:
        p = subprocess.run(["git", "-C", str(REPO), "rev-parse", f"{ref}:{path}"],
                           capture_output=True, text=True)
        _BLOB_SHA_CACHE[key] = p.stdout.strip() if p.returncode == 0 else ""
    return _BLOB_SHA_CACHE[key]


def _blob_lines(ref, path):
    """读某个 ref 下的文件行集合；二进制/非 UTF-8 用替换字符兜底，绝不抛异常。

    带缓存：几十个分支改的常常是同一批文件，缓存后整轮扫描从分钟级降到十几秒。
    """
    key = (ref, path)
    if key not in _BLOB_LINES_CACHE:
        p = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:{path}"],
                           capture_output=True, text=True, errors="replace")
        _BLOB_LINES_CACHE[key] = ({line.strip() for line in p.stdout.split("\n")}
                                  if p.returncode == 0 else set())
    return _BLOB_LINES_CACHE[key]


def already_superseded():
    """真相台账里已经标成 superseded 的泳道，避免重复归档。"""
    path = REPO / "ops" / "truth" / "progress.jsonl"
    done = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("status") == "superseded" and rec.get("lane"):
                done.add(rec["lane"])
    return done


def cached_conflict_branches():
    """最近一次冲突负担扫描留下的候选分支。

    None 表示缓存不存在或不可读；合法缓存即使筛完为空也返回空列表。调用方必须
    区分这两种情况，否则缓存里的候选全被跳过时会重新触发昂贵的全量 scan()。
    """
    path = OPS / "conflict_burden.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    branches = data.get("branches")
    if not isinstance(branches, list):
        return None
    return [b for b in branches
            if isinstance(b, str) and b.startswith(BRANCH_PREFIX)]


def branch_net_zero(branch):
    """这个分支相对它自己的 merge-base 的**独有内容**，是不是已经全部存在于 main。

    为什么需要它：`merge-tree` 一旦报告冲突，就分不出两种完全不同的情况——
      (a) 分支真的改了别人没改的东西（需要人裁决）；
      (b) 分支的内容早以别的路径进了 main，只是它的基线太旧，于是「冲突」。
    情况 (b) 在 100+ 个历史分支里占大头，把它们全推给用户裁决，就是让用户替
    系统记忆。这个函数把 (b) 判出来，交给 `kgctl supersede --go` 自动归档。

    判据（逐文件、相对该分支 merge-base）：
      * 分支版本 == main 版本            -> 该文件已并入；
      * main 根本没有这个文件            -> 分支独有新增，算「没并入」；
      * 否则取分支相对 base 的增/删行：
          - 每条新增行都能在 main 找到，且分支没有删掉 main 仍在的行 -> 已并入；
          - 否则 -> 没并入（保守判成 UNIQUE，宁可漏判，不可误归档有真改动的分支）。

    返回 (is_net_zero, notes)。只读。
    """
    base = git("merge-base", MAIN, branch)
    if not base:
        return False, ["无法求 merge-base"]
    files = [f for f in git("diff", "--name-only", base, branch).split("\n") if f]
    notes = []
    for f in files:
        bsha, msha = _blob_sha(branch, f), _blob_sha(MAIN, f)
        if bsha and msha and bsha == msha:
            continue
        if not msha:
            notes.append(f"{f}: main 没有该文件（分支独有新增）")
            continue
        diff = git("diff", "--unified=0", base, branch, "--", f)
        added, removed = [], []
        for ln in diff.split("\n"):
            if ln.startswith("+++") or ln.startswith("---"):
                continue
            if ln.startswith("+"):
                added.append(ln[1:].strip())
            elif ln.startswith("-"):
                removed.append(ln[1:].strip())
        added = [x for x in added if x]
        removed = [x for x in removed if x]
        main_lines = _blob_lines(MAIN, f)
        miss = [x for x in added if x not in main_lines]
        kept = [x for x in removed if x in main_lines]
        if miss or kept:
            notes.append(f"{f}: 新增未并入 {len(miss)} 行；分支已删但 main 仍在 {len(kept)} 行")
    return (not notes), notes


def unlanded_commits(branch):
    """分支上「main 里找不到等价改动」的提交（`git cherry` 的 `+` 行）。

    判据是 patch-id，不是 commit sha：同一个改动只要在 main 上以任何形式落过
    （cherry-pick / 手工重打 / squash），patch-id 就对得上，算已落。这正是
    「分支基线太旧、内容其实早进 main」这类假冲突的判别器——它不被基线新旧干扰，
    只看改动本身在不在 main。
    """
    out = git("cherry", MAIN, branch)
    return [parts[1] for line in out.split("\n")
            for parts in [line.split()]
            if len(parts) > 1 and line.startswith("+")]


# 自动移植的体量闸门。commit 数骗人的场景：一个分支只有 1 条未落提交，但那一条
# 是上千行的大改动，cherry-pick 到当前 main 必然文本冲突、白烧一轮 build 门禁。
# 按「未落改动相对 main 的真实规模和文件数」放行，才符合「只差一点点」的本意。
PORT_MAX_FILES = int(os.environ.get("KG_PORT_MAX_FILES", "15"))
PORT_MAX_LINES = int(os.environ.get("KG_PORT_MAX_LINES", "600"))

# 冲突任务执行器的闸门。它把一个「冲突文件数」和「未落改动行数」都比较小的冲突分支
# 拆成单个、可验收、可回退的任务；太大的先收拢（converge）或人工判断，避免一次把
# 几十个文件的语义冲突塞给一个 agent。
RESOLVE_MAX_FILES = int(os.environ.get("KG_RESOLVE_MAX_FILES", "8"))
RESOLVE_MAX_LINES = int(os.environ.get("KG_RESOLVE_MAX_LINES", "1200"))


def unlanded_diff_size(branch):
    """分支相对 merge-base(main, branch) 的真实改动规模 (files, changed_lines)。

    用 merge-base 三点差：只数「main 还没有的那部分」，不含分支自己落后的旧基线。
    与 `git cherry`（按 patch-id）互补——cherry 判「在不在」，这里判「有多大」。
    """
    mb = git("merge-base", MAIN, branch).strip()
    if not mb:
        return (0, 0)
    out = git("diff", "--numstat", mb, branch)
    files = 0
    lines = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        for tok in parts[:2]:
            if tok.isdigit():
                lines += int(tok)
    return (files, lines)


def conflict_burden(branch):
    """这条冲突分支真正要面对多少冲突文件。用 merge-tree 预演一次真实合并得到的
    冲突文件数——比 `scan` 的 nfiles（分支改了多少文件）更接近「要解多少」。
    0 表示其实能干净合并（scan 的 CONFLICT 可能只是索引态误判）。"""
    p = subprocess.run(["git", "-C", str(REPO), "merge-tree", "--write-tree",
                        "--name-only", MAIN, branch], capture_output=True, text=True)
    if p.returncode == 0:
        return 0
    files = []
    for line in p.stdout.splitlines()[1:]:
        if not line.strip():
            break
        files.append(line)
    return len(files)


def conflict_burden_map(rows=None):
    """{branch: 冲突文件数}，按 main 版本缓存。merge-tree 预演不便宜，一屏状态
    不该每次都重算——只在 main 前进或出现新分支时重算一次，其余读缓存。"""
    rows = rows if rows is not None else scan()
    cons = sorted(r["branch"] for r in rows if r["kind"] == "CONFLICT")
    key = git("rev-parse", MAIN)
    cache = OPS / "conflict_burden.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("main") == key and data.get("branches") == cons:
            return data.get("burden") or {}
    except (OSError, ValueError):
        pass
    burden = {b: conflict_burden(b) for b in cons}
    try:
        cache.write_text(json.dumps(
            {"main": key, "branches": cons, "burden": burden},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return burden


def branch_content_landed(branch):
    """分支想表达的内容，是不是已经全部在 main 里了。

    两条独立判据，命中任意一条都算已落（都只读、都不动任何分支）：
      * patch-id 等价：`git cherry` 里没有 `+` —— 每个提交都在 main 找到等价改动；
      * 逐文件净零：分支相对自身 base 的增删行都能在 main 找到（见 branch_net_zero）。

    第一条对「分支带着旧基线、文件内容整体落后于 main」更稳，第二条对「分支提交
    被重排或拆分」更稳。取并集意味着判据更宽，所以只用于归档（不产生任何代码改动、
    不改任何分支），不参与 land 的放行判断。
    """
    if not unlanded_commits(branch):
        return True, ["patch-id 等价：分支的每个提交都能在 main 找到等价改动"]
    return branch_net_zero(branch)


def cmd_supersede(args):
    """把「独有内容已全部在 main」的分支自动归档，不再当作冲突推给用户。

    默认只预演；`kgctl supersede --go` 才写进展台账并推送 main。
    只处理 ahead>0 的分支（ahead==0 的走 `kgctl reap`）。
    `--fast` 只跑 patch-id 判据（秒级），适合守护进程每个周期跑一遍；不带它则额外
    做逐文件比对（分钟级，能多捞出「提交被重排/拆分」的分支）。
    """
    go = "--go" in args
    fast = "--fast" in args
    if os.environ.get("KG_SUPERSEDE_LIGHT") == "1":
        # 自动周期只收口 exec 执行分支；不跑全量 scan()，否则 286 个分支逐个
        # merge-tree/merge-base 又会把一轮拖到超时。land 的 merge-tree 门禁不受影响。
        rows = [r for r in scan(light=True) if r["kind"] != "MERGED"]
    else:
        rows = [r for r in scan() if r["kind"] != "MERGED"]
    mode = "patch-id" if fast else "patch-id + 逐文件比对"
    print(f"扫描「分支独有内容是否已全部在 main」……（{mode}）\n")
    done = already_superseded()
    if done:
        print(f"（真相台账里已有 {len(done)} 条 superseded 记录，将跳过）\n")
    cands = []
    for r in sorted(rows, key=lambda x: (x["nfiles"], x["ahead"])):
        if r["branch"] in done:
            continue
        if fast:
            ok = not unlanded_commits(r["branch"])
            notes = ["patch-id 等价"] if ok else []
        else:
            ok, notes = branch_content_landed(r["branch"])
        if ok:
            cands.append(r)
    if not cands:
        print("没有可自动归档的分支：每个有改动的分支都还带着 main 里没有的内容。")
        return
    print(f"— 可自动归档（净贡献为零）共 {len(cands)} 个 —")
    for r in cands:
        print(f'   ahead={r["ahead"]:3d} files={r["nfiles"]:3d} conflicted={len(r["conflicts"]):3d} '
              f'{r["branch"]:46s} {r["subject"]}')
    if not go:
        print("\n（预演结束。加 --go 会逐个写进展台账并推送 main；不产生空合并提交。）")
        return
    if _lock_blocked():
        sys.exit(f"已有 land 在进行（{LOCK}）")
    OPS.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(f"{os.getpid()} supersede\n")
    try:
        _align_main()
        wt = _trunk_worktree()
        reset_trunk_head(wt)
        dirty = [line for line in trunk_status(wt) if not is_truth_path(line)]
        if dirty:
            print(f"× 主干工作树有非真相层改动，拒绝 supersede：{wt}")
            print("\n".join(dirty[:20])[:800])
            sys.exit(1)
        sync_trunk_truth(wt, "sync state before supersede")
        for r in cands:
            env = os.environ.copy()
            env.update({
                "KG_REPO": str(wt),
                "KG_TRUTH_LANE": r["branch"],
                "KG_TRUTH_COMMIT": git("rev-parse", r["branch"]),
                "KG_TRUTH_LANDED": "0",
                "KG_TRUTH_STATUS": "superseded",
                "KG_TRUTH_SOURCE": "kgctl supersede",
                "KG_TRUTH_SUMMARY": (
                    f'{r["branch"]} 相对自身基线的独有内容已逐文件确认全部存在于 main，'
                    "属重复分支，不产生空合并提交。"),
            })
            added = subprocess.run(
                [sys.executable, str(RUNTIME_OPS / "truthctl.py"), "add", "progress",
                 f'{r["branch"]} 已被 main 完全覆盖（重复分支）'],
                cwd=str(wt), env=env, capture_output=True, text=True)
            if added.returncode != 0:
                raise RuntimeError(f'写进展台账失败: {(added.stdout + added.stderr).strip()[:400]}')
            print(f'  归档 {r["branch"]}: {added.stdout.strip()}')
        subprocess.run(["git", "-C", str(wt), "add", "ops/truth/progress.jsonl"], check=True)
        committed = subprocess.run(
            ["git", "-C", str(wt), "commit", "-m",
             f'chore(truth): mark {len(cands)} branches superseded by main'],
            capture_output=True, text=True)
        if committed.returncode != 0:
            raise RuntimeError(f"提交进展台账失败: {committed.stderr.strip()[:400]}")
        pushed = _push_main_with_one_retry(wt)
        if pushed:
            print("  " + pushed.replace("\n", "\n  "))
        new = git("rev-parse", "HEAD")
        _align_main_ref_to_origin(new)
        print(f'  已归档 {len(cands)} 个重复分支并推送 origin/main @ {new[:8]}')
    finally:
        if LOCK.exists():
            LOCK.unlink()


# ---------------------------------------------------------------- 输入：唯一入口

def cmd_port(args):
    """把冲突分支的「未落提交」移植到当前 main，能过同一道门禁就直接落盘。

    为什么需要它：分支多起来以后，冲突队列真正的组成是两种——
      (a) 分支只差一两个提交没进 main，但基线太旧，于是 merge-tree 报冲突；
      (b) 分支真的和 main 改了同一处，需要人判断语义。
    (a) 不需要人。把那几条未落提交按序 cherry-pick 到当前 main 上，遇到冲突就停手
    放弃，跑通就交给 land 的同一道门禁。这样冲突队列只留下 (b)，用户要看的条目从
    几十条降到真正需要判断的那几条。

    安全约定：
      * 只在独立 worktree（`<state>-port-<slug>`）里操作，main 与源分支都不动；
      * cherry-pick 任一提交冲突就整体 --abort 并放弃，不猜、不用 ours/theirs；
      * 移植完成仍走 `kgctl land` 的完整门禁（merge-tree 预检 + 真实验证 +
        冲突标记扫描），门禁不过就丢弃，main 一动不动。
    默认只预演，`--go` 才真做。
    """
    if not args:
        sys.exit("用法: kgctl port <分支>|--auto [--max N] [--max-unlanded N] [--go]")
    go = "--go" in args
    auto = "--auto" in args
    limit = 4
    unlanded_max = 2
    for i, a in enumerate(args):
        if a == "--max" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                pass
        if a == "--max-unlanded" and i + 1 < len(args):
            try:
                unlanded_max = int(args[i + 1])
            except ValueError:
                pass
    if auto:
        # 自动挑一条「只差 1..unlanded_max 个提交」的冲突分支——这是最安全的一类：
        # 改动面小，cherry-pick 若语义撞车会直接文本冲突并整体放弃，不去猜。
        # KG_PORT_SKIP（冒号分隔的分支名）由守护进程传入：上一轮移植失败的分支本轮跳过，
        # 不重复烧一次 cherry-pick + build 门禁。
        skip = {b for b in os.environ.get("KG_PORT_SKIP", "").split(":") if b}
        done = already_superseded()
        cands = []
        oversize = 0
        cached_raw = cached_conflict_branches()
        use_cache = cached_raw is not None
        cached = [b for b in (cached_raw or []) if b not in done and b not in skip]
        rows = ([{"branch": b, "kind": "CONFLICT"} for b in cached]
                if use_cache else scan())
        if use_cache:
            print(f"auto-port：冲突缓存命中 {len(cached)} 条，跳过全量 merge-tree 扫描")
        for r in rows:
            if r["kind"] != "CONFLICT" or r["branch"] in done or r["branch"] in skip:
                continue
            n = len(unlanded_commits(r["branch"]))
            if 1 <= n <= unlanded_max:
                nf, nl = unlanded_diff_size(r["branch"])
                if nf > PORT_MAX_FILES or nl > PORT_MAX_LINES:
                    oversize += 1
                    continue
                cands.append((n, nf, nl, r["branch"]))
        if not cands:
            skipped = f"（已跳过 {len(skip)} 个暂缓分支）" if skip else ""
            extra = f"，另有 {oversize} 条提交数少但改动体量超闸门（需人工/收敛）" if oversize else ""
            print(f"auto-port：没有「只差 1–{unlanded_max} 个提交且体量可控」的冲突分支。{skipped}{extra}")
            return
        n, nf, nl, branch = sorted(cands)[0]
        print(f"auto-port：选中 {branch}（未落 {n} 提交，{nf} 文件，{nl} 行）")
    else:
        branch = args[0]
        if not branch.startswith(BRANCH_PREFIX):
            sys.exit(f"只允许 port {BRANCH_PREFIX}* 分支")
    plus = unlanded_commits(branch)
    if not plus:
        print(f"{branch} 没有未落提交（内容已全部在 main）：用 supersede --go 归档即可。")
        print(f"PORT_RESULT branch={branch} outcome=already")
        return
    nf, nl = unlanded_diff_size(branch)
    if nf > PORT_MAX_FILES or nl > PORT_MAX_LINES:
        print(f"{branch} 未落改动体量 {nf} 文件 / {nl} 行，超过自动移植闸门 "
              f"({PORT_MAX_FILES} 文件 / {PORT_MAX_LINES} 行)。")
        print("  这类「单提交上千行」cherry-pick 到当前 main 必然文本冲突，自动移植会白烧一轮门禁；")
        print("  需要时先 converge 或人工解冲突，再走 land。")
        print(f"PORT_RESULT branch={branch} outcome=too_big")
        return
    print(f"{branch} 未落提交 {len(plus)} 条（main 里没有等价改动）：")
    for sha in plus:
        print(f"  {sha[:10]} {git('log', '-1', '--format=%s', sha)}")
    if len(plus) > limit:
        print(f"超过 --max {limit}：改动面太大，自动移植容易把「并集」猜成「二选一」。")
        print(f"  需要时显式加 --max {len(plus)}，或先用 converge 让它和同族分支一起收口。")
        print(f"PORT_RESULT branch={branch} outcome=too_big")
        return
    if not go:
        print("预演结束。加 --go 会把这些提交移植到当前 main 上的一条新分支，"
              "跑同一道验证门禁，过了才 land。")
        return
    if _lock_blocked():
        sys.exit(f"已有 land/port/converge 在进行（{LOCK}）")
    _align_main()
    base = git("rev-parse", f"origin/{MAIN}") or git("rev-parse", MAIN)
    slug = slugify(branch[len(BRANCH_PREFIX):])
    new_branch = f"{BRANCH_PREFIX}port-{slug}"
    wt = REPO.parent / f"{REPO.name}-port-{slug}"
    if git("rev-parse", "--verify", new_branch):
        print(f"× {new_branch} 已存在（可能是上一次移植留下的）。先确认/清理它再重跑。")
        print(f"PORT_RESULT branch={branch} outcome=blocked")
        return
    if wt.exists():
        print(f"× {wt} 已存在但不是本命令的产出，跳过。")
        print(f"PORT_RESULT branch={branch} outcome=blocked")
        return
    add = subprocess.run(["git", "-C", str(REPO), "worktree", "add", "-b", new_branch,
                          str(wt), base], capture_output=True, text=True)
    if add.returncode != 0:
        sys.exit(f"建 worktree 失败: {(add.stdout + add.stderr).strip()[:400]}")
    _link_side_dirs(wt)
    picked = 0
    already = 0
    for sha in plus:
        res = subprocess.run(["git", "-C", str(wt), "cherry-pick", "-x", sha],
                             capture_output=True, text=True)
        if res.returncode != 0:
            blob = res.stdout + res.stderr
            # 分支提交可能已被 hand 落地、或本身就是 `index on main:` / `WIP on`
            # 这类备份提交，cherry-pick 后变成 empty —— 这不是语义冲突，别拿它污染
            # 冲突队列，也不能 abort 掉前面已经 pick 成功的提交。就地 skip 继续。
            if "previous cherry-pick is now empty" in blob or "nothing to commit" in blob:
                subprocess.run(["git", "-C", str(wt), "cherry-pick", "--skip"],
                               capture_output=True, text=True)
                already += 1
                print(f"  skip  {sha[:10]}（empty，等价改动已在 main）")
                continue
            subprocess.run(["git", "-C", str(wt), "cherry-pick", "--abort"],
                           capture_output=True, text=True)
            print(f"× 移植 {sha[:10]} 时冲突，已 abort："
                  f"{blob.strip()[:400]}")
            picked = -1
            break
        picked += 1
        print(f"  picked {sha[:10]} {git('log', '-1', '--format=%s', sha)}")
    if picked < 0:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(REPO), "branch", "-D", new_branch],
                       capture_output=True, text=True)
        print(f"  已回滚：{new_branch} 与它的 worktree 都不留，{branch} 原样不动。")
        print(f"PORT_RESULT branch={branch} outcome=conflict")
        return
    # 移植后树和 base 一致 = 这些改动其实已经在 main 里（只是 patch-id 不同）。
    # 这时不能交给 land：--no-ff 会产生一个空合并提交，污染历史。直接判重复。
    tree_diff = subprocess.run(["git", "-C", str(wt), "diff", "--stat", base, new_branch],
                               capture_output=True, text=True).stdout.strip()
    if not tree_diff:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(REPO), "branch", "-D", new_branch],
                       capture_output=True, text=True)
        if picked == 0 and already:
            print(f"  分支全部 {already} 条提交 cherry-pick 后为空：内容已在 main。")
        else:
            print(f"  移植后树与 {base[:8]} 完全一致：改动已在 main（patch-id 不同而已）。")
        print(f"  已回滚 {new_branch}；用 supersede --go 归档 {branch} 即可。")
        print(f"PORT_RESULT branch={branch} outcome=already")
        return
    print(f"→ 移植完成：{new_branch}（{picked} 提交，基于 {base[:8]}）。交给 land 跑门禁。")
    # 用子进程跑 land：门禁失败时 land 会 sys.exit(1)，必须由本函数接住，
    # 才能把移植分支与它的 worktree 一起回滚——不留无人认领的 codex/port-* 残留。
    res = subprocess.run([sys.executable, str(Path(__file__).resolve()), "land", new_branch],
                         capture_output=True, text=True)
    print((res.stdout or "").rstrip())
    if res.returncode != 0:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(REPO), "branch", "-D", new_branch],
                       capture_output=True, text=True)
        print(f"  land 未通过，已回滚 {new_branch} 与它的 worktree；{branch} 原样不动，main 未动。")
        print(f"PORT_RESULT branch={branch} outcome=failed")
        return
    print(f"✓ {new_branch} 已通过门禁并落盘。源分支 {branch} 未改动。")
    print(f"PORT_RESULT branch={branch} outcome=landed")

def load_lanes():
    """泳道的物理含义 = 它认领哪些文件（默认实现；把分支换成 PR 后仍成立）。"""
    if LANES.exists():
        return json.loads(LANES.read_text())
    return {"泳道": {}, "规则": []}


def cmd_inbox(args):
    """零散需求不需要用户分类：一律追加进同一账本，由自动挡负责路由。"""
    OPS.mkdir(parents=True, exist_ok=True)
    if not args:
        lines = INBOX.read_text().splitlines() if INBOX.exists() else []
        shown = 0
        for i, line in enumerate(lines, 1):
            try:
                it = json.loads(line)
            except Exception:
                continue
            flag = " " if it.get("routed") else "*"
            print(f"{flag} #{i:03d} [{it['ts'][5:16]}] {it.get('text','')[:110]}")
            shown += 1
        if shown == 0:
            print("inbox 为空（没有待路由项）。新需求直接：kgctl inbox \"…\"")
        return
    if args[0] in ('-h', '--help'):
        print('用法: kgctl inbox "用户原话"')
        return
    text = " ".join(args)
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text,
           "source": os.environ.get("KG_INBOX_SOURCE", "user"),
           "files": [f for f in os.environ.get("KG_INBOX_FILES", "").split(":") if f],
           "kind": os.environ.get("KG_INBOX_KIND", "unknown"),  # visual|ux|bug|arch|unknown
           "routed": False, "lane": None}
    with INBOX.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"已记入唯一入口 {rec['ts']}：{text[:80]}", flush=True)
    print("（无需分类；自动挡路由时按 files 落到既有泳道）", flush=True)



def cmd_intake(args):
    """一句话入口：记录原话 -> 归类 -> 刷新真相 -> 给出下一步。

    这是给 MCP / skill 用的「一次动作走完整条链」入口。`inbox` 仍保留纯追加语义，
    方便脚本或调试；普通对话应该走 `intake`，用户不该记两步命令。
    """
    if not args:
        sys.exit('用法: kgctl intake "用户原话"')
    if args[0] in ('-h', '--help'):
        print('用法: kgctl intake "用户原话"')
        return
    _restore_truth_keep_files_from_origin()
    cmd_inbox(args)
    sys.stdout.flush()
    print("\n[1/5] route inbox", flush=True)
    _truth("route", "--go")
    print("\n[2/5] refresh truth", flush=True)
    _truth("snapshot")
    print("\n[3/5] refresh board / dashboard", flush=True)
    cmd_board([])
    cmd_dash([])
    print("\n[4/5] commit truth state", flush=True)
    sync_trunk_truth(REPO, "sync from intake")
    push_truth_state()
    print("\n[5/5] what to do next", flush=True)
    _truth("next")

def cmd_route(args):
    """权威路由入口：与 `kgctl tick` 完全同一条实现。

    委托给 truthctl 的 route，读写同一份 inbox 与问题台账。旧的「按文件重叠给建议」
    挪到 `kgctl suggest`，避免「两个 route 各读各的文件、各说各话」再次出现。
    """
    return _truth("route", *args)


def cmd_suggest(args):
    """按文件重叠给「该并进哪个既有分支」的建议（只读，不落账）。"""
    lanes = load_lanes()
    if not INBOX.exists():
        print("inbox 为空"); return
    items = []
    for i, line in enumerate(INBOX.read_text().splitlines(), 1):
        try:
            it = json.loads(line)
        except Exception:
            continue
        if not it.get("routed"):
            it["_n"] = i
            items.append(it)
    if not items:
        print("没有待路由项"); return
    rows = [r for r in scan() if r["kind"] != "MERGED"]
    print(f"待路由 {len(items)} 项，按文件落在既有泳道：\n")
    for it in items:
        files = it.get("files") or []
        hits = []
        for r in rows:
            ov = set(files) & set(r["files"])
            if ov:
                hits.append((len(ov), r["branch"], sorted(ov)[:3]))
        hits.sort(reverse=True)
        if hits:
            print(f" #{it['_n']:03d} {it['text'][:70]}")
            for n, b, ov in hits[:3]:
                print(f"      -> {b}（{n} 文件重叠：{', '.join(ov)}）")
        else:
            print(f" #{it['_n']:03d} {it['text'][:70]}")
            print(f"      -> 无匹配泳道，建议开新泳道：kgctl new <名字>")
    print("\n（路由是给自动挡的建议；落地动作是 land 入队或起新泳道）")


# ---------------------------------------------------------------- 分诊

def scan(light=False):
    rows = []
    # 已裁决的源分支不再算「待裁决冲突」。resolve.json 里 landed/abandoned 的、
    # progress.jsonl 里 superseded 的，git 层还可能显示 ahead+conflict（它们本来就是
    # 没合进 main 的历史分支），只按 ahead/conflict 判定就会把它们反复捞回 C 组：
    # 队列数字虚高，守护进程每轮还重拆同一条已收口的分支。单列一档 SETTLED。
    settled = set()
    if not light:
        settled = _decided_resolve_branches() | already_superseded()
    for b in codex_branches():
        if light and not b.startswith("codex/exec-"):
            continue
        # 收拢分支是自动挡生成的任务容器，不是待裁决的用户分支。把它排除在一屏 /
        # matrix / queue 之外，否则它自己带 conflict markers 时会被当成“残留冲突
        # 标记”反复报出来，用户会误以为还有一条工作没收拾。
        if _is_generated_branch(b):
            continue
        ahead, behind = counts(b)
        ts = int(git("log", "-1", "--format=%ct", b) or 0)
        files = changed_files(b)
        markers = []
        if ahead == 0:
            kind, conf = "MERGED", []
        else:
            conf = conflict_files(b)
            markers = unresolved_marker_files(b) if not conf else []
            kind = "CONFLICT" if conf else ("UNRESOLVED" if markers else "CLEAN")
            if kind == "CONFLICT" and b in settled:
                kind = "SETTLED"
        rows.append(dict(branch=b, ahead=ahead, behind=behind,
                         age=round((time.time() - ts) / 86400, 1) if ts else -1,
                         subject=git("log", "-1", "--format=%s", b)[:70],
                         nfiles=len(files), files=files, kind=kind, conflicts=conf,
                         markers=markers))
    return rows


def cmd_matrix(args):
    rows = scan()
    json.dump(rows, (OPS / "matrix.json").open("w"), ensure_ascii=False, indent=1)
    g = {"MERGED": [], "CLEAN": [], "CONFLICT": [], "UNRESOLVED": [], "SETTLED": []}
    for r in rows:
        g[r["kind"]].append(r)
    print(f"仓库 {REPO}")
    print(f"主干 {MAIN} @ {git('log','-1','--format=%h %s',MAIN)[:60]}\n")
    print(f"codex 分支 {len(rows)} 个 ->  已合并 {len(g['MERGED'])} | "
          f"能干净合 {len(g['CLEAN'])} | 有冲突 {len(g['CONFLICT'])} | "
          f"残留冲突标记 {len(g['UNRESOLVED'])} | 已裁决 {len(g['SETTLED'])}\n")

    print("— A 已完全合并（可回收）—")
    for r in sorted(g["MERGED"], key=lambda x: -x["behind"]):
        print(f"   behind={r['behind']:4d} age={r['age']:5.1f}d  {r['branch']:44s} {r['subject']}")

    print("\n— B 无冲突·可入队 —")
    for r in sorted(g["CLEAN"], key=lambda x: (x["ahead"], -x["behind"])):
        print(f"   ahead={r['ahead']:3d} behind={r['behind']:4d} {r['nfiles']:3d}文件 "
              f"age={r['age']:4.1f}d  {r['branch']:44s} {r['subject']}")

    print("\n— C 有冲突·必须裁决（同族不可并行）—")
    byfile = {}
    for r in g["CONFLICT"]:
        for f in r["conflicts"]:
            byfile.setdefault(f, []).append(r["branch"])
    for f, bs in sorted(byfile.items(), key=lambda kv: -len(kv[1]))[:8]:
        print(f"   {f}")
        for b in bs[:6]:
            print(f"       <- {b}")
        if len(bs) > 6:
            print(f"       ... 共 {len(bs)} 个分支抢这个文件")
    print(f"\n（C 组 {len(g['CONFLICT'])} 个分支 / "
          f"{len({f for r in g['CONFLICT'] for f in r['conflicts']})} 个冲突文件）")

    print("\n— D 残留冲突标记·禁止落盘（先在 convergence worktree 解干净）—")
    for r in sorted(g["UNRESOLVED"], key=lambda x: (x["ahead"], x["branch"])):
        print(f"   {r['branch']:44s} {len(r['markers']):3d}文件 {r['subject']}")
        for f in r["markers"][:3]:
            print(f"       {f}")
        if len(r["markers"]) > 3:
            print(f"       ... 共 {len(r['markers'])} 个文件")
    print("明细：ops/tasks/matrix.json")

    if g["SETTLED"]:
        print("\n— E 已裁决·不再计入冲突队列（landed/abandoned/superseded）—")
        for r in sorted(g["SETTLED"], key=lambda x: (x["ahead"], x["branch"])):
            print(f"   ahead={r['ahead']:3d} behind={r['behind']:4d} "
                  f"{r['branch']:44s} {r['subject']}")


def _acceptance_by_lane() -> dict:
    """lane(=分支名) → 最新一条验收记录（按台账追加顺序取最后一条）。

    存在的理由：队列只报「有几个提交、落后多少」，不报「这个分支的门禁跑了没、过没过」。
    心跳拍因此只能说「队列没有附带门禁证据」而不敢 land —— 空拍的一半原因在这。
    """
    out = {}
    for r in read_jsonl(REPO / "ops" / "truth" / "acceptance.jsonl"):
        lane = r.get("lane")
        if lane:
            out[lane] = r
    return out


def cmd_queue(args):
    show_gates = "--gates" in (args or [])
    light = "--light" in (args or [])
    if light:
        rows = scan(light=True)
    else:
        rows = scan()
    clean = sorted([r for r in rows if r["kind"] == "CLEAN"],
                   key=lambda x: (x["ahead"], -x["behind"]))
    acc = _acceptance_by_lane() if show_gates else {}
    print("合并队列（一次一个，合完立刻重算）\n")
    for i, r in enumerate(clean, 1):
        print(f"  {i:2d}. kgctl land {r['branch']}   "
              f"（{r['ahead']} 提交 / {r['nfiles']} 文件 / 落后 {r['behind']}）")
        if not show_gates:
            continue
        a = acc.get(r["branch"])
        age = r.get("age")
        age_txt = f"{age} 天前" if isinstance(age, (int, float)) and age >= 0 else "取不到"
        if a:
            ev = (a.get("evidence") or "").replace("\n", " ")[:180]
            print(f"      门禁 {a.get('status')} @ {a.get('ts')} · {a.get('source')}：{ev}")
        else:
            print(f"      门禁 无记录 → 先跑 `python3 ops/kgctl.py gate {r['branch']}` 造证据，再 land")
        print(f"      最后提交 {age_txt}")
    if not clean:
        print("  队列为空")
    print(f"\n其余 {len(rows)-len(clean)} 个："
          f"{sum(1 for r in rows if r['kind']=='MERGED')} 个已合并待回收、"
          f"{sum(1 for r in rows if r['kind']=='CONFLICT')} 个有冲突需裁决。")
    unresolved = sum(1 for r in rows if r["kind"] == "UNRESOLVED")
    if unresolved:
        print(f"另有 {unresolved} 个分支残留冲突标记，已从队列排除。")


def cmd_gate(args):
    """kgctl gate <分支> [--keep] —— 在分支自己的工作树上真跑一遍验证门禁，并把结果写进验收台账。

    存在的理由：队列里的分支常常「没有门禁证据」，于是心跳拍只能报「缺证据」而不敢 land，
    一拍就这么空过去了。这个命令把「缺证据」变成「有证据」：不合并、不推送、不动 main、
    不动源分支；跑完把 name/cmd/cwd/exit 写进 ops/truth/acceptance.jsonl（lane=分支,
    source=kgctl gate），随后 `kgctl queue --gates` 就能看到它、`kgctl land` 直接可用。
    退出码：门禁全过 0，任一失败 1（便于看门狗与心跳拍直接判定）。
    """
    import tempfile
    if not args:
        sys.exit("用法: kgctl gate <分支> [--keep]")
    branch, keep = args[0], "--keep" in args
    if not git("rev-parse", "--verify", f"{branch}^{{commit}}"):
        sys.exit(f"× 没有这个分支：{branch}")
    wt_path = worktree_map().get(branch)
    tmp = None
    if wt_path:
        wt = Path(wt_path)
        print(f"用分支已有的工作树：{wt}")
    else:
        tmp = Path(tempfile.gettempdir()) / f"kg-gate-{slugify(branch, 24)}-{os.getpid()}"
        subprocess.run(["git", "-C", str(SHARED), "worktree", "remove", "--force", str(tmp)],
                       capture_output=True, text=True)
        add = subprocess.run(["git", "-C", str(SHARED), "worktree", "add", "--detach", str(tmp), branch],
                             capture_output=True, text=True, errors="replace")
        if add.returncode != 0:
            sys.exit(f"× 建门禁工作树失败：{(add.stdout or '') + (add.stderr or '')}".strip()[:400])
        wt = tmp
        print(f"门禁工作树（临时）：{wt}")
    try:
        _link_side_dirs(wt)
        ok, results, out = _run_verify_gates(wt)
        for r in results:
            print(f"  [{r.get('name')}] $ {r.get('cmd')} (cwd={r.get('cwd')}) -> exit "
                  f"{0 if r.get('ok') else 1}")
        if not results:
            print("  (项目没配验证门禁，按通过处理；请在 ops/central.config.json 的 verify 里补上)")
        sha = git("rev-parse", branch)
        env = dict(os.environ, KG_REPO=str(REPO), KG_TRUTH_LANE=branch, KG_TRUTH_COMMIT=sha,
                   KG_TRUTH_STATUS="verified" if ok else "todo",
                   KG_TRUTH_EVIDENCE=_verify_evidence(results),
                   KG_TRUTH_SOURCE="kgctl gate",
                   KG_TRUTH_SUMMARY=f"{branch} 的门禁预跑（未落盘）")
        p = subprocess.run([sys.executable, str(RUNTIME_OPS / "truthctl.py"), "add", "acceptance",
                            f"{branch} 门禁预跑（未落盘）"],
                           cwd=str(REPO), env=env, capture_output=True, text=True, errors="replace")
        if p.returncode == 0:
            print(f"  验收台账 {(p.stdout or '').strip()}")
        else:
            print(f"  × 写验收台账失败（门禁结果仍有效）：{((p.stdout or '') + (p.stderr or '')).strip()[:300]}")
        print(f"\n结论：门禁 {'ok → 可以直接 `kgctl land ' + branch + '`' if ok else '× 失败 → 先修再 land'}")
        if not ok:
            print(out[-1200:])
        return 0 if ok else 1
    finally:
        if tmp is not None and not keep:
            subprocess.run(["git", "-C", str(SHARED), "worktree", "remove", "--force", str(tmp)],
                           capture_output=True, text=True)


def cmd_clash(args):
    """写之前先问：这个文件现在有谁在改？"""
    if not args:
        sys.exit("用法: kgctl clash <文件> [文件...]")
    rows = scan()
    for f in args:
        holders = [r for r in rows if f in r["files"]]
        print(f"\n{f}")
        if not holders:
            print("   无人认领，可以动手")
        for r in sorted(holders, key=lambda x: -x["ahead"]):
            mark = "冲突" if f in r["conflicts"] else "可合"
            print(f"   [{mark}] {r['branch']:44s} ahead={r['ahead']:3d} {r['subject']}")



def _link_side_dirs(wt: Path) -> None:
    """给新建的收敛工作树软链 node_modules，避免重复安装。"""
    shared = central_config.shared_dir(REPO, CFG)
    for side in (CFG.get("link_dirs") or []):
        dst = wt / side / "node_modules"
        if dst.exists():
            continue
        for src in (shared / side / "node_modules", REPO / side / "node_modules"):
            if src.exists():
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.symlink_to(src, target_is_directory=True)
                except OSError:
                    pass
                break


def load_convergence() -> dict:
    if not CONVERGENCE.exists():
        return {"groups": []}
    try:
        data = json.loads(CONVERGENCE.read_text(encoding="utf-8"))
    except Exception:
        return {"groups": []}
    if not isinstance(data, dict):
        return {"groups": []}
    data.setdefault("groups", [])
    return data


def _write_convergence_record(record: dict) -> None:
    data = load_convergence()
    data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    groups = [g for g in data.get("groups", []) if g.get("id") != record.get("id")]
    groups.append(record)
    groups.sort(key=lambda g: g.get("created") or "", reverse=True)
    data["groups"] = groups
    write_json(CONVERGENCE, data)


def _merge_jsonl_conflict(wt: Path, rel: str) -> None:
    """冲突 JSONL 用行级并集保住两边记录，顺序保持 ours -> theirs。"""
    def stage(n):
        p = subprocess.run(["git", "-C", str(wt), "show", f":{n}:{rel}"],
                           capture_output=True, text=True, errors="replace")
        return p.stdout if p.returncode == 0 else ""
    seen, lines = set(), []
    for text in (stage(2), stage(3)):
        for line in text.splitlines():
            if line and line not in seen:
                lines.append(line)
                seen.add(line)
    write_text(wt / rel, "\n".join(lines) + ("\n" if lines else ""))


def _stage_unmerged(wt: Path, paths) -> None:
    """把未合并路径逐个落进索引，避免一个被忽略/已删除的路径让整批 git add 失败。

    单文件冲突时最容易踩的坑：某个未合并路径正好被 .gitignore 忽略（构建产物 / probe
    文件），`git add -- <全部>` 会因该路径返回非零 -> 整批失败 -> 合并卡在 MERGE_HEAD，
    而收敛分支已被登记，后续“已存在则跳过”，这个 worktree 就永久半合并。这里改成
    逐路径处理：文件存在用 add --force（越过 ignore 收口），文件被某侧删除用 rm --force。
    """
    for rel in paths:
        if (wt / rel).exists():
            r = subprocess.run(["git", "-C", str(wt), "add", "--force", "--", rel],
                               capture_output=True, text=True)
        else:
            r = subprocess.run(["git", "-C", str(wt), "rm", "--force", "--", rel],
                               capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"无法把未合并路径落索引：{rel}：{(r.stdout + r.stderr).strip()[:200]}")


def _commit_conflicted_merge(wt: Path, branch: str, group_id: str) -> dict:
    unresolved = subprocess.run(
        ["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True).stdout.splitlines()
    if not unresolved:
        return {"branch": branch, "files": [], "auto_resolved_jsonl": []}
    auto = []
    for rel in unresolved:
        if rel.endswith(".jsonl"):
            _merge_jsonl_conflict(wt, rel)
            auto.append(rel)
    _stage_unmerged(wt, unresolved)
    commit = subprocess.run(
        ["git", "-C", str(wt), "commit", "-m",
         f"wip(converge): merge {branch} into {group_id} with conflict markers"],
        capture_output=True, text=True)
    if commit.returncode != 0:
        raise RuntimeError(f"提交冲突合并失败：{(commit.stdout + commit.stderr).strip()[:500]}")
    return {"branch": branch, "files": unresolved, "auto_resolved_jsonl": auto}


def _convergence_branch(group: dict) -> str:
    return f"{BRANCH_PREFIX}converge-{group['id']}"


def _branch_contained(wt: Path, src: str) -> bool:
    """src 是否已在 wt 当前 HEAD 的历史里（已合入，或 base 本来就有）。"""
    return subprocess.run(
        ["git", "-C", str(wt), "merge-base", "--is-ancestor", src, "HEAD"],
        capture_output=True, text=True).returncode == 0


def _abort_stale_merge(wt: Path) -> bool:
    """worktree 若停在半合并（MERGE_HEAD 存在），abort 回上一个已提交状态。

    已完成并提交的 merge 都在提交历史里，abort 只丢未提交的中间索引。不清掉的话，
    下一次 converge 会因为“分支已存在”而跳过，这个 worktree 就永久半合并。
    """
    out = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-dir"],
                         capture_output=True, text=True)
    gd = out.stdout.strip()
    if out.returncode != 0 or not gd:
        return False
    gd_path = Path(gd) if os.path.isabs(gd) else (wt / gd)
    if not (gd_path / "MERGE_HEAD").exists():
        return False
    r = subprocess.run(["git", "-C", str(wt), "merge", "--abort"],
                       capture_output=True, text=True)
    return r.returncode == 0


def _adopt_stable_ids(groups, records) -> None:
    """把当前冲突族的 id 粘到既有收拢记录上，别让成员集一漂移就换 id。

    族 id 原本是「成员集 digest」，成员集一变（main 前进、分支新加入/转干净）就
    重算，旧记录连带它的 worktree 变成没人认领的孤儿。这里做粘性匹配：一份既有
    记录只要和当前族有足够重叠（≥ 半数），就沿用它已登记的 id / 收拢分支 / worktree。
    """
    taken = set()
    recs = [r for r in records.values() if r.get("id")]
    for g in groups:
        members = set(g["branches"])
        best, best_key = None, None
        for rec in recs:
            if rec["id"] in taken:
                continue
            rb = set(rec.get("branches") or [])
            ov = len(members & rb)
            if not ov:
                continue
            ratio = ov / max(1, min(len(members), len(rb)))
            if ratio < 0.5:
                continue
            # 同一族被两份记录覆盖时，用「重合度 -> 已完成的合并数」取舍，尽量接着
            # 已经合过大半的那份 worktree 往下走，而不是从只合了几条的那份重来。
            key = (ov, round(ratio, 4), len(rec.get("merges") or []))
            if best_key is None or key > best_key:
                best, best_key = rec, key
        if best is not None:
            g["id"] = best["id"]
            taken.add(best["id"])


def _clean_orphan_convergences(records, used_ids, made) -> list:
    """清理不再对应任何当前冲突族的收拢记录。

    成员集一变，旧 id 就不再被算出来，旧记录连同它的 worktree 变成孤儿——里面若卡着
    半合并就永久残留（实测就是 055ddd5718）。这里只做安全的收尾：清掉残留半合并；
    若它的源分支都已被别的收拢分支或 main 接走，就把记录标成 superseded 指向接管者。
    绝不删源分支、绝不动 main。
    """
    archived = []
    for rid, rec in records.items():
        if rid in used_ids or not rec.get("branch"):
            continue
        if not git("rev-parse", "--verify", rec["branch"]):
            continue
        wt = Path(rec.get("worktree") or (REPO.parent / f"{REPO.name}-converge-{rid}"))
        if wt.exists() and _abort_stale_merge(wt):
            print(f"  ！孤儿收拢 {rec['branch']} 清掉了残留半合并")
        # 判断「内容有没有被接管」时忽略派生的收拢分支，只看真实源分支。
        srcs = [s for s in (rec.get("branches") or [])
                if not _is_convergence_branch(s) and git("rev-parse", "--verify", s)]
        if not srcs:
            continue
        holder = None
        for mrec in made:
            mwt = Path(mrec["worktree"])
            if mwt.exists() and all(_branch_contained(mwt, s) for s in srcs):
                holder = mrec["id"]
                break
        if holder is None and all(counts(s)[0] == 0 for s in srcs):
            holder = "main"
        if holder:
            rec = dict(rec)
            rec["status"] = "superseded"
            rec["superseded_by"] = holder
            rec["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _write_convergence_record(rec)
            archived.append(rec["branch"])
            print(f"  已归档孤儿收拢 {rec['branch']}（内容已由 {holder} 接管）")
    return archived


def cmd_replan(args):
    """把过时的 convergence 大组按当前 main 重盘，解散成逐支可执行的工作队列。

    背景：convergence 组是在某个历史 main 上收拢几十个冲突分支生成的超级合并。
    之后 main 不断前进，这个组会和 main 越差越远——一个 218 文件 / 7000+ 冲突标记
    的 worktree，人根本啃不动。但它的成员分支单独相对当前 main 时，很多只差几条提交，
    甚至内容早已落盘。真正的最优解不是解超级合并，而是把它解散，让每支回到
    「supersede / auto-port / land」这套现有流水线里逐支处理。

    只读默认：打印每支在「当前 main」下的分类。
    `--go`：把组标成 replanned（不再出现在 matrix/看板），物理删掉它的 worktree 和
    分支，成员分支重新回到扫描队列，由后续 supersede / port 自动收敛。
    """
    go = "--go" in args
    groups = load_convergence().get("groups", [])
    pending = [g for g in groups
               if g.get("status") not in ("superseded", "replanned")
               and g.get("branches")]
    if not pending:
        print("没有可重盘的 convergence 组（都已被接管或已重盘）。")
        return
    if go and _lock_blocked():
        sys.exit(f"已有 land/port/converge/replan 在进行（{LOCK}）")
    if go:
        LOCK.write_text(str(os.getpid()))
    try:
        _replan_groups(pending, go)
    finally:
        if go and LOCK.exists():
            LOCK.unlink()


def _replan_groups(pending, go):
    changed = 0
    for g in pending:
        members = [b for b in g["branches"] if git("rev-parse", "--verify", b)]
        already, port, manual = [], [], []
        for b in members:
            n = len(unlanded_commits(b))
            if n == 0:
                already.append(b)
                continue
            nf, nl = unlanded_diff_size(b)
            if n <= 2 and nf <= PORT_MAX_FILES and nl <= PORT_MAX_LINES:
                port.append((n, nf, nl, b))
            else:
                manual.append((n, nf, nl, b))
        print(f"\n组 {g['id']} ({g.get('branch')}) 状态 {g.get('status')}，成员 {len(members)}：")
        print(f"  已落盘可归档 : {len(already)}")
        print(f"  小而可控可移植: {len(port)}")
        print(f"  需人工/收敛  : {len(manual)}")
        if g.get("status") == "needs_resolution":
            print("  → 建议 replan --go：解散超级合并，成员回到逐支流水线。")
        if not go:
            continue
        # 解散：记录标 replanned，删掉 worktree/分支；成员本来就是分支，回到扫描集。
        rec = dict(g)
        rec["status"] = "replanned"
        rec["replanned"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rec["replan_report"] = {
            "members": len(members), "already": len(already),
            "auto_port": len(port), "manual": len(manual),
        }
        _write_convergence_record(rec)
        wt = rec.get("worktree")
        branch = rec.get("branch")
        if wt and Path(wt).exists():
            subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", wt],
                           capture_output=True, text=True)
        if branch and git("rev-parse", "--verify", branch):
            subprocess.run(["git", "-C", str(REPO), "branch", "-D", branch],
                           capture_output=True, text=True)
        print(f"  已解散 {g['id']}：worktree/分支已清理，{len(members)} 个成员回到逐支流水线。")
        changed += 1
    if go and changed:
        print(f"\n共重盘 {changed} 个组。下一轮 supersede / port 会逐支处理成员分支。")
    elif not go:
        print("\n（预演。加 --go 解散这些过时组。）")


def cmd_converge(args):
    """把几十个冲突分支收成少数几个 convergence 任务。

    这一步只新建 WIP 分支，不动 main、不改源分支。冲突内容会原样进入 convergence
    分支（保留 conflict markers；JSONL 走行级并集），随后一个 agent 只需要处理少数
    convergence 分支，而不是逐个回答“这 60 个分支要不要合”。
    """
    go = "--go" in args
    limit = None
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                pass
    rows = scan()
    groups = conflict_groups(rows)
    if limit:
        groups = groups[:limit]
    records = {g.get("id"): g for g in load_convergence().get("groups", [])}
    _adopt_stable_ids(groups, records)
    # 已 replan 过的组不允许再被 tick 自动重建：否则解散过的超级合并每轮复活，
    # 成员分支永远回不到逐支流水线。要让它们重新收拢，得先手动清掉记录。
    dissolved = [g for g in groups
                 if (records.get(g.get("id")) or {}).get("status") == "replanned"]
    if dissolved:
        groups = [g for g in groups if g not in dissolved]
        print(f"跳过 {len(dissolved)} 个已 replan 的组（不重建超级合并）。")
    if not groups:
        print("没有冲突分支，无需收拢。")
        if go:
            _clean_orphan_convergences(records, set(), [])
        return
    print(f"冲突分支 {sum(len(g['branches']) for g in groups)} 个，可收成 "
          f"{len(groups)} 个 convergence 任务：\n")
    for g in groups:
        branch = _convergence_branch(g)
        rec = records.get(g["id"]) or {}
        mark = "已收拢" if rec.get("branch") == branch else "待收拢"
        print(f"  [{mark}] {branch}")
        print(f"      {len(g['branches'])} 分支 / {len(g['files'])} 冲突文件："
              f"{', '.join(g['files'][:4])}{' ...' if len(g['files']) > 4 else ''}")
        for b in g["branches"][:6]:
            print(f"      <- {b}")
        if len(g["branches"]) > 6:
            print(f"      ... 共 {len(g['branches'])} 条")
    if not go:
        print("\n预演结束。加 --go 会新建 convergence 分支（不改 main、不改源分支）。")
        return
    if _lock_blocked():
        sys.exit(f"已有 land/supersede/converge 在进行（{LOCK}）")
    LOCK.write_text(f"{os.getpid()} converge\n")
    made = []
    try:
        fetch = subprocess.run(["git", "-C", str(REPO), "fetch", "origin", MAIN],
                               capture_output=True, text=True)
        base_ref = f"origin/{MAIN}" if fetch.returncode == 0 else MAIN
        base_sha = git("rev-parse", base_ref)
        for g in groups:
            branch = _convergence_branch(g)
            existing = records.get(g["id"]) or {}
            branch_exists = bool(git("rev-parse", "--verify", branch))
            wt = REPO.parent / f"{REPO.name}-converge-{g['id']}"
            resume = branch_exists and existing.get("branch") == branch and wt.exists()
            if resume:
                # 断点续跑：先清掉可能残留的半合并（已提交的 merge 在历史里，不会丢），
                # 再只补合尚未进入 HEAD 的源分支；已合入的靠 is-ancestor 跳过。
                aborted = _abort_stale_merge(wt)
                print(f"  {branch} 已存在，续跑未合入的源分支"
                      + ("（清掉了残留半合并）" if aborted else ""))
            elif branch_exists:
                print(f"  {branch} 已存在但未登记或 worktree 缺失，跳过")
                continue
            elif wt.exists():
                print(f"  × {wt} 已存在但不是已登记分支；跳过 {branch}")
                continue
            else:
                add = subprocess.run(
                    ["git", "-C", str(REPO), "worktree", "add", "-b", branch, str(wt), base_ref],
                    capture_output=True, text=True)
                if add.returncode != 0:
                    print(f"  × 建 worktree 失败 {branch}: {(add.stdout + add.stderr).strip()[:300]}")
                    continue
                _link_side_dirs(wt)
            merge_notes = list(existing.get("merges") or [])
            merged_now = 0
            for src in g["branches"]:
                if _branch_contained(wt, src):
                    continue
                merged = subprocess.run(
                    ["git", "-C", str(wt), "merge", "--no-ff", src,
                     "-m", f"converge: merge {src} into {g['id']}"],
                    capture_output=True, text=True)
                if merged.returncode == 0:
                    merge_notes.append({"branch": src, "files": [],
                                        "auto_resolved_jsonl": []})
                    merged_now += 1
                    continue
                try:
                    note = _commit_conflicted_merge(wt, src, g["id"])
                except Exception as exc:
                    subprocess.run(["git", "-C", str(wt), "merge", "--abort"],
                                   capture_output=True, text=True)
                    print(f"  × {branch} 合并 {src} 失败：{exc}，已 merge --abort 回到干净状态")
                    merge_notes.append({"branch": src, "files": ["(commit failed)"],
                                        "auto_resolved_jsonl": []})
                    continue
                merge_notes.append(note)
                merged_now += 1
            # 半合并兜底：任何路径异常都不许把 MERGE_HEAD 留在 worktree 里，
            # 否则下一次 converge 会误判“已存在，跳过”，worktree 永久半合并。
            if _abort_stale_merge(wt):
                print(f"  ！{branch} 发现并清掉了残留半合并（未提交的中间索引已丢弃）")
            grep = subprocess.run(
                ["git", "-C", str(wt), "grep", "-l", "-E", "^(<<<<<<<|>>>>>>>)"],
                capture_output=True, text=True)
            unresolved_files = [x for x in grep.stdout.splitlines() if x.strip()]
            head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            record = {
                "id": g["id"], "branch": branch, "worktree": str(wt),
                "base": existing.get("base") or base_sha, "head": head,
                "created": existing.get("created") or time.strftime("%Y-%m-%d %H:%M:%S"),
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "needs_resolution" if unresolved_files else "ready",
                "branches": g["branches"], "files": g["files"],
                "unresolved_files": unresolved_files, "merges": merge_notes,
            }
            _write_convergence_record(record)
            made.append(record)
            print(f"  已收拢 {len(g['branches'])} 分支 -> {branch}"
                  f"（补合 {merged_now} 条，待解冲突文件 {len(unresolved_files)}，worktree={wt}）")
        if made:
            print(f"\n已生成 {len(made)} 个 convergence 分支。下一步：对每个 needs_resolution "
                  "分支跑 `kgctl context <分支>`，再让一个 agent 在该 convergence worktree 里解冲突。")
        # 孤儿收拢：既有记录已不再对应任何当前族，先清掉它 worktree 里可能卡着的半合并，
        # 内容被别处接走就归档，绝不留下永久半合并。必须等所有当前族都处理完再判断，
        # 否则多族收拢时，尚未轮到的既有记录会被误判成孤儿。
        _clean_orphan_convergences(records, {g["id"] for g in groups}, made)
    finally:
        if LOCK.exists():
            LOCK.unlink()


def _context_payload(branch: str) -> dict:
    ahead, behind = counts(branch)
    files = changed_files(branch)
    conf = conflict_files(branch)
    markers = unresolved_marker_files(branch) if not conf else []
    rows = latest_by_id(read_jsonl(REPO / "ops" / "truth" / "ledger.jsonl")).values()
    open_rows = [r for r in rows if r.get("status") in ("open", "doing", "todo")]
    overlap = []
    for r in open_rows:
        hit = sorted(set(files) & set(r.get("files") or []))
        if hit:
            overlap.append({"id": r.get("id"), "kind": r.get("kind"),
                            "lane": r.get("lane"), "title": r.get("title"),
                            "files": hit})
    payload = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "branch": branch,
        "head": git("rev-parse", branch),
        "worktree": worktree_map().get(branch, ""),
        "main": MAIN,
        "main_head": git("rev-parse", MAIN),
        "ahead": ahead,
        "behind": behind,
        "mergeable": not conf and not markers,
        "conflict_files": conf,
        "unresolved_marker_files": markers,
        "changed_files": files,
        "mainline": json.loads((REPO / "ops" / "truth" / "mainline.json").read_text(encoding="utf-8"))
        if (REPO / "ops" / "truth" / "mainline.json").exists() else {},
        "active_decisions": [r for r in latest_by_id(read_jsonl(REPO / "ops" / "truth" / "decisions.jsonl")).values()
                             if r.get("status") == "active"],
        "open_issues_overlapping": overlap,
        "acceptance_todo": [r for r in latest_by_id(read_jsonl(REPO / "ops" / "truth" / "acceptance.jsonl")).values()
                            if r.get("status") in ("todo", "wip", "blocked")],
        "recent_progress": sorted(latest_by_id(read_jsonl(REPO / "ops" / "truth" / "progress.jsonl")).values(),
                                  key=lambda r: r.get("ts", ""))[-8:],
        "convergence": next((g for g in load_convergence().get("groups", [])
                             if g.get("branch") == branch), None),
    }
    payload["hash"] = hashlib.sha1(json.dumps(
        payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return payload


def cmd_context(args):
    """为一条分支生成版本化 Context Pack：主线/决策/已知问题/归属/验收/DoD。"""
    if not args or args[0] in ("-h", "--help"):
        print("用法: kgctl context <分支> | kgctl context --all")
        return
    branches = []
    if args[0] == "--all":
        branches = [r["branch"] for r in scan() if r["kind"] != "MERGED"]
    else:
        branches = [a for a in args if not a.startswith("--")]
    for branch in branches:
        if not git("rev-parse", "--verify", branch):
            print(f"  × 找不到分支 {branch}")
            continue
        payload = _context_payload(branch)
        name = slugify(branch.replace("/", "-"), 80)
        write_json(CONTEXT_DIR / f"{name}.json", payload)
        lines = [
            f"# Context Pack: {branch}", "",
            f"- hash: `{payload['hash']}`",
            f"- commit: `{payload['head']}`",
            f"- worktree: `{payload['worktree'] or '(none)'}`",
            f"- main: `{payload['main_head']}`",
            f"- mergeable: `{payload['mergeable']}`",
            f"- changed files: {len(payload['changed_files'])}",
            "",
            "## 产品主线",
            f"- 产品：{payload['mainline'].get('product', '')}",
            f"- 北极星：{payload['mainline'].get('north_star', '')}",
            f"- 当前焦点：{'；'.join(payload['mainline'].get('current_focus') or [])}",
            "",
            "## 必须遵守的决策",
        ]
        lines += [f"- `{d.get('id')}` {d.get('title')}" for d in payload["active_decisions"]] or ["- （无）"]
        lines += ["", "## 相关未决问题"]
        lines += [f"- `{x['id']}` {x['title']}（命中 {', '.join(x['files'])}）"
                  for x in payload["open_issues_overlapping"]] or ["- （无文件重叠）"]
        lines += ["", "## 验收 / DoD"]
        lines += [f"- [{x.get('status')}] {x.get('level','')} {x.get('title','')}"
                  for x in payload["acceptance_todo"]] or ["- （无）"]
        if payload["conflict_files"]:
            lines += ["", "## 冲突文件"]
            lines += [f"- `{f}`" for f in payload["conflict_files"]]
        if payload["unresolved_marker_files"]:
            lines += ["", "## 残留冲突标记（禁止落盘）"]
            lines += [f"- `{f}`" for f in payload["unresolved_marker_files"][:20]]
        if payload["convergence"]:
            lines += ["", "## 收拢任务"]
            lines += [f"- convergence: `{payload['convergence'].get('branch')}`",
                      f"- 待解冲突文件：{len(payload['convergence'].get('unresolved_files') or [])}"]
        write_text(CONTEXT_DIR / f"{name}.md", "\n".join(lines) + "\n")
        print(f"已写 Context Pack：ops/context/{name}.json")
    if branches:
        print(f"共 {len(branches)} 份；Markdown 在同目录。")


def cmd_dash(args):
    """生成可直接打开的驾驶舱 HTML。

    这是「自动挡」对普通用户的窗口，不是运维控制台：第一屏先说清它是什么、怎么用、
    现在替你做了什么；分支/合并这些工程细节收进默认折叠的「高级视图」，不想看就不用看。
    """
    import html as _html

    rows = scan()
    groups = conflict_groups(rows)
    led = latest_by_id(read_jsonl(REPO / "ops" / "truth" / "ledger.jsonl")).values()
    open_led = [r for r in led if r.get("status") in ("open", "doing", "todo")]
    open_led.sort(key=lambda r: ({"bug": 0, "arch": 1, "ux": 2, "visual": 3}.get(r.get("kind"), 9),
                                 r.get("ts", "")))
    prog = sorted(latest_by_id(read_jsonl(REPO / "ops" / "truth" / "progress.jsonl")).values(),
                  key=lambda r: r.get("ts", ""))[-8:]
    inbox = read_jsonl(INBOX)
    pending = [x for x in inbox if not x.get("routed")]

    def esc(x):
        return _html.escape(str(x or ""), quote=True)

    def jfile(name):
        try:
            return json.loads((REPO / "ops" / "truth" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def latest(records):
        out = {}
        for rec in records:
            if isinstance(rec, dict) and rec.get("id"):
                out[rec["id"]] = rec
        return out

    try:
        import control_state
        ctrl = control_state.load()
    except Exception:
        ctrl = {}

    ml = jfile("mainline.json")
    road = []
    for it in (ml.get("roadmap") or []):
        if isinstance(it, str):
            road.append({"step": it, "expected": "", "status": "todo"})
        elif isinstance(it, dict):
            road.append({"step": it.get("step", ""), "expected": it.get("expected", ""),
                         "status": it.get("status") or "todo"})
    ps = jfile("product_state.json")
    modules = ps.get("modules") if isinstance(ps.get("modules"), list) else []
    acc = latest(read_jsonl(REPO / "ops" / "truth" / "acceptance.jsonl"))
    human = [r for r in acc.values()
             if r.get("needs_human") and r.get("status") in ("todo", "wip", "blocked")]
    machine = [r for r in acc.values()
               if r.get("status") == "verified" and not r.get("needs_human")]

    clean = [r for r in rows if r["kind"] == "CLEAN"]
    conflict = [r for r in rows if r["kind"] == "CONFLICT"]
    unresolved = [r for r in rows if r["kind"] == "UNRESOLVED"]
    merged = [r for r in rows if r["kind"] == "MERGED"]

    try:
        import exec_status
        exec_now = exec_status.running()
    except Exception:
        exec_now = None
    if exec_now:
        _rec = latest(led).get(exec_now.get("id")) or {}
        exec_now["title"] = _rec.get("title") or exec_now.get("title") or ""

    paused = bool(ctrl.get("paused"))
    state_txt = "已暂停" if paused else "运行中"
    state_cls = "paused" if paused else "live"
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S")
    head = git("log", "-1", "--format=%h %s", MAIN)
    if exec_now:
        _m = int((exec_now.get("elapsed_sec") or 0) // 60)
        _ti = exec_now.get("title") or ""
        _t = f"（{_ti}）" if _ti else ""
        exec_html = (
            '<section class="card execnow"><h2>正在自动实现</h2>'
            f'<p><b>{esc(exec_now.get("id"))}</b>{esc(_t)} · 已跑 {_m} 分钟</p>'
            '<div class="note">它在一个独立工作区里改代码，自测通过后自动合入 main；'
            '这一条跑完，下一条会接着来。</div></section>')
    else:
        exec_html = ""

    KIND_LABEL = {"bug": "bug", "gate": "门禁", "visual": "视觉", "ux": "体验",
                  "arch": "架构", "idea": "想法", "reprocess": "待重排"}
    ROAD_LABEL = {"done": "已完成", "doing": "进行中", "next": "下一步", "todo": "未开始"}
    ROAD_MARK = {"done": "✓", "doing": "●", "next": "●", "todo": "○"}

    def kind_tag(kind):
        warn = " warn" if kind in ("bug", "gate") else ""
        return f'<span class="tag{warn}">{esc(KIND_LABEL.get(kind, kind or "item"))}</span>'

    # 产品路线：有实现级 roadmap 就逐步显示状态，否则如实列出主线声明的阶段。
    if road:
        items = []
        for r in road:
            ex = f'<div class="ex">达到：{esc(r["expected"])}</div>' if r["expected"] else ""
            items.append(
                f'<li class="road-{esc(r["status"])}"><span class="mk">'
                f'{ROAD_MARK.get(r["status"], "○")}</span><div><div class="sb">'
                f'<span class="nm">{esc(r["step"])}</span>'
                f'<span class="st">{esc(ROAD_LABEL.get(r["status"], r["status"]))}</span></div>'
                f'{ex}</div></li>')
        road_html = ('<ul class="road">' + "".join(items) + "</ul>"
                     '<div class="note">这条路线走完，就达到产品预期；每一步到哪了都从项目真相里读出来。</div>')
    elif ml.get("path"):
        items = []
        for p in ml.get("path"):
            step = p if isinstance(p, str) else p.get("step", "")
            items.append('<li class="road-todo"><span class="mk">○</span><div><div class="sb">'
                         f'<span class="nm">{esc(step)}</span></div></div></li>')
        stage = f'<div class="note">当前阶段：{esc(ml.get("stage"))}</div>' if ml.get("stage") else ""
        road_html = (stage + '<ul class="road">' + "".join(items) + "</ul>"
                     '<div class="note">这是主线声明的产品阶段；等自动挡把它拆成带状态的实现路线后，'
                     '每一步到哪了都会显示在这里。</div>')
    else:
        road_html = '<div class="empty">还没拆出产品路线。</div>'

    # 需要你验收：只有必须人拍板的才出现，附复核步骤。
    if human:
        items = []
        for a in human:
            steps = (f'<div class="accsteps">{esc(a.get("steps"))}</div>' if a.get("steps")
                     else '<div class="ex">（还没写明复核步骤）</div>')
            items.append(f'<li><div class="t"><span class="lv">{esc(a.get("level") or "P1")}</span>'
                         f'<span class="grow">{esc(a.get("title"))}</span></div>{steps}</li>')
        human_html = '<ul class="acc">' + "".join(items) + "</ul>"
    else:
        human_html = ('<div class="empty">现在没有需要你亲自验收的项。功能改完后，'
                      '只有必须人看（审美、手感、在真实产品里点一下）的才会出现在这里。</div>')
    if machine:
        items = "".join(f'<li><span class="tag ok">✓</span>'
                        f'<span class="grow">{esc(a.get("title"))}</span></li>' for a in machine)
        machine_html = (f'<div class="note">机器已自证 · 不需要你动手（{len(machine)} 项）</div>'
                        f'<ul class="list">{items}</ul>')
    else:
        machine_html = '<div class="note">机器已自证 · 不需要你动手</div>'

    # 当前在做什么 / 还差什么
    doing = [r for r in open_led if r.get("status") == "doing"]
    todo = [r for r in open_led if r.get("status") != "doing"]

    def item_ul(items):
        return "".join(
            f'<li>{kind_tag(r.get("kind"))}<span class="grow">{esc(r.get("title"))}</span>'
            f'<span class="when">{esc(r.get("id"))}</span></li>' for r in items)

    if doing or todo:
        parts = []
        if doing:
            parts.append('<div class="subhead">正在做</div><ul class="list">' + item_ul(doing) + "</ul>")
        if todo:
            more = f"（共 {len(todo)} 条，显示前 {min(len(todo), 20)}）" if len(todo) > 20 else ""
            parts.append(f'<div class="subhead">还差这些{more}</div>'
                         '<ul class="list">' + item_ul(todo[:20]) + "</ul>")
        parts.append('<div class="note">修完、验完的条目会自动从「还差这些」里消失，不用你手动划掉。</div>')
        progress_html = "".join(parts)
    else:
        progress_html = ('<div class="empty">当前没有未决的工作项。你提的需求会先进问题台账，'
                         '再从这里被推进到收口。</div>')

    # 产品主线
    focus = ml.get("current_focus") or []
    focus_html = "".join(f"<li>{esc(f)}</li>" for f in focus) if focus else "<li>（未声明）</li>"
    mainline_html = (
        f'<div class="row"><span class="k">产品</span><span>{esc(ml.get("product"))}</span></div>'
        f'<div class="row"><span class="k">阶段</span><span>{esc(ml.get("stage"))}</span></div>'
        f'<div class="row"><span class="k">北极星</span><span>{esc(ml.get("north_star"))}</span></div>'
        '<div class="subhead">当前焦点</div><ul class="list plain">' + focus_html + "</ul>")

    # 产品状态：它现在为你做什么（按模块读 product_state.json）
    if modules:
        items = []
        for m in modules[:12]:
            items.append(f'<li><span class="tag">{esc(m.get("area") or "模块")}</span>'
                         f'<span class="grow">{esc(m.get("behavior"))}</span>'
                         f'<span class="when">{esc(m.get("updated"))}</span></li>')
        meta_bits = []
        if ps.get("count"):
            meta_bits.append(f"共 {ps['count']} 项")
        if ps.get("updated"):
            meta_bits.append(f"最后更新 {ps['updated']}")
        meta_bits.append("人验收只看这里的变化")
        product_html = ('<ul class="list">' + "".join(items) + "</ul>"
                        f'<div class="note">{" · ".join(meta_bits)}</div>')
    else:
        product_html = ('<div class="empty">还没有记录。改动落盘时会把产品行为写进这里，'
                        '让你不用读代码就知道它现在是什么样。</div>')

    # ---- 高级视图：运维表（保持原有信息，一字不少） ----
    ops_need = "".join(
        f"<tr><td>{esc(r.get('id'))}</td><td>{esc(r.get('kind'))}</td>"
        f"<td>{esc(r.get('lane'))}</td><td>{esc(r.get('title'))}</td></tr>"
        for r in open_led[:20]) or '<tr><td colspan="4">没有未决问题</td></tr>'
    ops_group = "".join(
        f"<tr><td><code>{esc(_convergence_branch(g))}</code></td>"
        f"<td>{len(g['branches'])}</td><td>{len(g['files'])}</td>"
        f"<td>{esc(', '.join(g['files'][:3]))}</td></tr>"
        for g in groups) or '<tr><td colspan="4">没有需要收拢的冲突</td></tr>'
    ops_branch = "".join(
        f"<tr><td>{esc(r['kind'])}</td><td><code>{esc(r['branch'])}</code></td>"
        f"<td>{r['ahead']}</td><td>{r['behind']}</td>"
        f"<td>{len(r['conflicts'])}</td><td>{esc(r['subject'])}</td></tr>"
        for r in sorted(clean + conflict + unresolved, key=lambda x: (x["kind"], -x["ahead"]))[:30]
    ) or '<tr><td colspan="6">没有待处理分支</td></tr>'
    ops_prog = "".join(
        f"<li><code>{esc(r.get('id'))}</code> {esc(r.get('summary') or r.get('title'))}</li>"
        for r in reversed(prog)) or "<li>暂无进展记录</li>"

    css = """
:root{--ink:#1b2330;--ink2:#5b6472;--ink3:#8a93a1;--line:#e4e8ef;--bg:#f6f7f9;--card:#fff;--accent:#0f766e;--accent-soft:#e6f2f0;--warn:#b42318;--warn-soft:#fef3f2}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}
main{max-width:900px;margin:0 auto;padding:30px 20px 64px}
.top{display:flex;align-items:center;gap:12px;margin-bottom:6px}
.brand{width:34px;height:34px;border-radius:9px;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:16px;flex:none}
.grow{flex:1}
h1{font-size:22px;margin:0;letter-spacing:0}
h2{font-size:15.5px;margin:0 0 12px;letter-spacing:0}
h3{font-size:13px;margin:18px 0 8px;color:var(--ink2)}
.meta{color:var(--ink3);font-size:12.5px;margin-top:3px}
.meta code{background:#eef1f5;padding:1px 5px;border-radius:5px;color:var(--ink2)}
.lede{color:var(--ink2);font-size:14.5px;margin:10px 0 18px;max-width:690px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:13px}
.card p{margin:0 0 12px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:2px 9px;border-radius:999px;vertical-align:middle}
.pill.live{background:var(--accent-soft);color:var(--accent)}
.pill.paused{background:var(--warn-soft);color:var(--warn)}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.steps{list-style:none;padding:0;margin:0}
.steps li{display:flex;gap:11px;padding:6px 0;align-items:flex-start}
.stepn{flex:none;width:22px;height:22px;border-radius:50%;background:var(--accent-soft);color:var(--accent);font-size:12.5px;font-weight:600;display:flex;align-items:center;justify-content:center;margin-top:1px}
.callout{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-top:10px}
.quote{color:var(--ink);font-size:14px}
.hint{color:var(--ink3);font-size:12.5px;margin-top:6px}
.counters{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.metric{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.metric .n{font-size:24px;font-weight:650;color:var(--accent);line-height:1.2}
.metric .l{font-size:12.5px;color:var(--ink2)}
.note{color:var(--ink3);font-size:12.5px;margin-top:10px}
.execnow{background:var(--accent-soft);border-color:#bfe0db}
.execnow h2{margin-bottom:6px;color:var(--accent)}
.empty{color:var(--ink2);font-size:13.5px;background:var(--bg);border:1px dashed var(--line);border-radius:10px;padding:12px 14px}
.road{list-style:none;padding:0;margin:0}
.road li{display:flex;gap:10px;padding:7px 0;border-bottom:1px solid var(--line)}
.road li:last-child{border-bottom:none}
.mk{flex:none;width:18px;text-align:center;color:var(--accent);font-size:13px;margin-top:2px}
.road-todo .mk{color:var(--ink3)}
.sb{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.nm{font-weight:550}
.st{font-size:11.5px;color:var(--ink3)}
.road-done .nm{color:var(--ink2)}
.ex{color:var(--ink2);font-size:12.5px;margin-top:2px}
.list{list-style:none;padding:0;margin:0}
.list li{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--line)}
.list li:last-child{border-bottom:none}
.list.plain li{border-bottom:none;padding:4px 0;color:var(--ink2);display:list-item;list-style:disc;margin-left:18px}
.tag{flex:none;font-size:11.5px;background:#eef1f5;color:var(--ink2);padding:1px 8px;border-radius:999px}
.tag.warn{background:var(--warn-soft);color:var(--warn)}
.tag.ok{background:var(--accent-soft);color:var(--accent)}
.when{color:var(--ink3);font-size:11.5px;white-space:nowrap}
.subhead{font-size:12.5px;font-weight:600;color:var(--ink2);margin:14px 0 4px}
.subhead:first-child{margin-top:0}
.acc{list-style:none;padding:0;margin:0}
.acc li{padding:9px 0;border-bottom:1px solid var(--line)}
.acc li:last-child{border-bottom:none}
.acc .t{display:flex;gap:8px;align-items:baseline}
.lv{flex:none;font-size:11px;background:var(--accent-soft);color:var(--accent);padding:1px 7px;border-radius:5px}
.accsteps{color:var(--ink2);font-size:12.5px;margin-top:4px;white-space:pre-line}
.row{display:flex;gap:12px;padding:3px 0}
.row .k{flex:none;width:64px;color:var(--ink3);font-size:12.5px}
.adv{margin-top:6px;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.adv>summary{cursor:pointer;padding:14px 20px;font-size:13.5px;font-weight:600;color:var(--ink2);list-style:none}
.adv>summary::-webkit-details-marker{display:none}
.adv>summary:before{content:"\\25B8  "}
.adv[open]>summary:before{content:"\\25BE  "}
.advbody{padding:0 20px 18px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:6px}
.stat{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:10px 12px}
.stat .num{font-size:20px;font-weight:650;color:var(--accent)}
.stat .lab{font-size:12px;color:var(--ink2)}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:9px;overflow:hidden;font-size:12.5px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:#eef1f4;font-size:11.5px;color:#475467;font-weight:600}
tr:last-child td{border-bottom:none}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.foot{color:var(--ink3);font-size:12.5px;text-align:center;margin-top:20px}
@media(max-width:640px){.counters,.stats{grid-template-columns:repeat(2,1fr)}main{padding:20px 14px 48px}table{display:block;overflow:auto}}
"""
    body = f"""<main>
<div class="top">
  <div class="brand">自</div>
  <div class="grow">
    <h1>Codex 自动挡</h1>
    <div class="meta">生成 {ts_str} · 主干 <code>{esc(MAIN)}</code> @ <code>{esc(head)}</code>
      · <span class="pill {state_cls}"><span class="dot"></span>{state_txt}</span></div>
  </div>
</div>
<p class="lede">它接管你零散的需求——归类、记录、追踪，把和主线不冲突、检查通过的改动自己合好；你只在必须拍板时出现。</p>

{exec_html}

<section class="card">
  <h2>怎么用</h2>
  <p>你平时打字的那个 <b>Codex 输入框，就是总输入框</b>。不用新建对话，也不用挑分支、挑角色。</p>
  <ol class="steps">
    <li><span class="stepn">1</span><div>把看到的零散问题<b>原样说出来</b>就行——视觉不对、体验别扭、冒出一个 bug，都可以一起丢过来。</div></li>
    <li><span class="stepn">2</span><div>自动挡负责<b>归类、记录、追踪</b>，并把和主线不冲突、检查通过的改动<b>自己合好</b>。</div></li>
    <li><span class="stepn">3</span><div>只有<b>改产品方向、删数据、发布</b>这类要拍板的事，它才会停下来问你。</div></li>
  </ol>
  <div class="callout">
    <div class="quote">“左边导航排版坏了，顺便这个按钮点着没反应，另外首页有点慢。”</div>
    <div class="hint">就这样一句话全发出去，不用分类，也不用想它该进哪个对话或分支。</div>
  </div>
</section>

<section class="card">
  <h2>它替你做了这些</h2>
  <div class="counters">
    <div class="metric"><div class="n">{len(latest(led))}</div><div class="l">收到的零散反馈</div></div>
    <div class="metric"><div class="n">{len(merged)}</div><div class="l">自动合入的分支</div></div>
    <div class="metric"><div class="n">{len(open_led)}</div><div class="l">未决问题</div></div>
  </div>
  <div class="note">没有让新手去选分支、等合并、一个个点验收——这些由它自己串起来。</div>
</section>

<section class="card"><h2>产品路线 · 现在走到哪了</h2>{road_html}</section>
<section class="card"><h2>需要你验收</h2>{human_html}{machine_html}</section>
<section class="card"><h2>当前在做什么 · 还差什么</h2>{progress_html}</section>
<section class="card"><h2>产品主线</h2>{mainline_html}</section>
<section class="card"><h2>产品状态 · 它现在为你做什么</h2>{product_html}</section>

<details class="adv">
  <summary>高级视图 · 分支与合并（运维）</summary>
  <div class="advbody">
    <div class="stats">
      <div class="stat"><div class="num">{len(pending)}</div><div class="lab">待路由输入</div></div>
      <div class="stat"><div class="num">{len(clean)}</div><div class="lab">可落盘</div></div>
      <div class="stat"><div class="num">{len(conflict)}</div><div class="lab">冲突分支</div></div>
      <div class="stat"><div class="num">{len(unresolved)}</div><div class="lab">残留冲突标记</div></div>
      <div class="stat"><div class="num">{len(groups)}</div><div class="lab">可收拢冲突族</div></div>
      <div class="stat"><div class="num">{len(open_led)}</div><div class="lab">未决问题</div></div>
    </div>
    <h3>现在最该做什么</h3>
    <table><thead><tr><th>ID</th><th>类型</th><th>泳道</th><th>问题</th></tr></thead><tbody>{ops_need}</tbody></table>
    <h3>冲突收拢</h3>
    <table><thead><tr><th>Convergence 分支</th><th>源分支</th><th>冲突文件</th><th>示例</th></tr></thead><tbody>{ops_group}</tbody></table>
    <h3>分支队列</h3>
    <table><thead><tr><th>状态</th><th>分支</th><th>领先</th><th>落后</th><th>冲突</th><th>标题</th></tr></thead><tbody>{ops_branch}</tbody></table>
    <h3>最近进展</h3><ul class="list plain">{ops_prog}</ul>
  </div>
</details>

<div class="foot">Codex 自动挡 · 平时无感运行，需要时这里能看、能停、能改。</div>
</main>"""
    doc = ('<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">\n'
           '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
           '<title>Codex 自动挡 · 驾驶舱</title>\n<style>\n' + css + '\n</style></head><body>\n'
           + body + '\n</body></html>')
    write_text(DASHBOARD, doc)
    print(f"已写出 {DASHBOARD.relative_to(REPO)}（用户视图：反馈 {len(latest(led))}、"
          f"已合入 {len(merged)}、未决 {len(open_led)}；运维：待路由 {len(pending)}、"
          f"可落盘 {len(clean)}、冲突 {len(conflict)}、收拢族 {len(groups)}）")
    if "--open" in args and sys.platform == "darwin":
        subprocess.run(["open", str(DASHBOARD)], check=False)

def cmd_reap(args):
    go = "--go" in args
    rows = scan()
    merged = [r for r in rows if r["kind"] == "MERGED"]
    wts = worktree_map()
    print("回收预演（ahead=0 的分支 + 它们独占的 worktree）")
    for r in sorted(merged, key=lambda x: -x["behind"]):
        wt = wts.get(r["branch"], "")
        own = wt and Path(wt) != REPO
        dirty = ""
        if own:
            st = subprocess.run(["git", "-C", wt, "status", "--porcelain"],
                                capture_output=True, text=True).stdout.strip()
            dirty = f"  脏={len(st.splitlines())} 项，只删分支、不删 worktree" if st else ""
        print(f"  {'分支+worktree' if own else '仅分支'}  {r['branch']:44s} {wt}{dirty}")
    print(f"\n共 {len(merged)} 项。{'【已执行】' if go else '默认只预演；确认后加 --go'}")
    if not go:
        return
    for r in merged:
        wt = wts.get(r["branch"], "")
        if wt and Path(wt) != REPO:
            st = subprocess.run(["git", "-C", wt, "status", "--porcelain"],
                                capture_output=True, text=True).stdout.strip()
            if not st:
                subprocess.run(["git", "-C", str(REPO), "worktree", "remove", wt],
                               capture_output=True, text=True)
        subprocess.run(["git", "-C", str(REPO), "branch", "-D", r["branch"]],
                       capture_output=True, text=True)
    print("已回收。分支只是指针，成果在 main 里。")


# ---------------------------------------------------------------- 落盘：merge train

def _trunk_worktree():
    """落盘必须在**独立主干工作树**里做。

    不能直接在 REPO 里 merge：`main` 通常正被另一个 worktree 检出（它可能正是 main 的常驻检出点），在那里 `git checkout main` 会静默失败，merge 落到当前分支上，
    看起来"合了"而 main 一动不动 —— 实测踩过。train 因此自带工作树，并用 update-ref 推进 main。
    """
    wt = central_config.trunk_dir(REPO, CFG)
    if wt.resolve() == REPO.resolve():
        # KG_TRUNK 被指到了状态根本身：落盘会直接在这个目录里 merge / reset，而账本
        # （ops/tasks、ops/truth）就在这儿。一次 reset 就能把账本打回 main 的旧版本
        # ——实测正是这么丢掉了刚路由好的 7 条 inbox。落盘主干必须是独立 worktree，
        # 所以这里强制改用默认的 `<项目名>-trunk`。
        fallback = REPO.parent / (REPO.name + "-trunk")
        print(f"  !KG_TRUNK 指向状态根本身（{REPO}），已改用独立主干工作树：{fallback}")
        wt = fallback
    if not (wt / ".git").exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(wt), MAIN],
                       capture_output=True, text=True)
    shared = central_config.shared_dir(REPO, CFG)
    for side in (CFG.get("link_dirs") or []):
        dst = wt / side / "node_modules"
        if dst.exists():
            continue
        for src in (shared / side / "node_modules", REPO / side / "node_modules"):
            if src.exists():
                try:
                    dst.symlink_to(src, target_is_directory=True)
                except OSError:
                    pass
                break
    return wt


def _fetch_origin_main(attempts: int = 5, delay: float = 3.0) -> bool:
    """fetch origin/main，带重试。

    原来的 _align_main() 只 fetch 一次：git 代理（http.https://github.com.proxy）
    抖一下就让整轮 land 抛 RuntimeError 并计入 last_failures，而分支其实早已落盘
    ——是「假失败」。实测 daemon.log 里就有 Failed to connect to 127.0.0.1 port 9674
    导致的误判。网络抖动是暂态，重试能消掉绝大多数；真失败（认证/仓库不存在）
    重试几次后照样抛出，错误信息原样保留。
    """
    last = ""
    for attempt in range(attempts):
        fetched = subprocess.run(["git", "-C", str(REPO), "fetch", "origin", MAIN],
                                 capture_output=True, text=True, timeout=120)
        if fetched.returncode == 0:
            if attempt:
                print(f"  fetch origin/{MAIN} 第 {attempt + 1} 次成功（前 {attempt} 次为暂态失败）")
            return True
        last = ((fetched.stdout or "") + (fetched.stderr or "")).strip()
        if attempt < attempts - 1:
            print(f"  ! fetch origin/{MAIN} 第 {attempt + 1} 次失败，{delay:g}s 后重试："
                  f"{last[:200]}")
            time.sleep(delay)
    classifier = getattr(central_config, "is_network_unreachable", None)
    if callable(classifier) and classifier(last):
        print(f"  origin 网络不可达，保留本地 {MAIN} 并继续：{last[:240]}")
        return False
    raise RuntimeError(f"fetch origin/{MAIN} 失败（重试 {attempts} 次后仍失败）: {last}")


def _align_main():
    """落盘前先把本地 main 引用对齐 origin/main，拒绝在分叉引用上继续。"""
    if not _fetch_origin_main():
        return
    local = git("rev-parse", MAIN)
    remote = git("rev-parse", f"origin/{MAIN}")
    if local == remote:
        return
    ff = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor",
                         local, remote], capture_output=True, text=True)
    if ff.returncode != 0:
        raise RuntimeError(
            f"本地 {MAIN} 与 origin/{MAIN} 已分叉：local={local[:10]} remote={remote[:10]}；"
            "先人工合并/推送本地提交，再执行 land")
    subprocess.run(["git", "-C", str(REPO), "update-ref", f"refs/heads/{MAIN}",
                    remote, local], check=True)
    print(f"  {MAIN} 本地引用 {local[:8]} -> {remote[:8]}（同步 origin）")


def _verify_evidence(verify_results: list) -> str:
    """把每一道验证门禁的「真实命令 + cwd + 结果」写成可复现的验收证据。

    不接受「通过构建」这类泛化文案：没有命令名、工作目录和退出结果，证据就不可复核。
    """
    if not verify_results:
        return ("未配置验证门禁：本次只做了合并与推送，没有可复现的验证命令。"
                "请在 ops/central.config.json 的 verify 里补上。")
    lines = []
    for r in verify_results:
        code = "0" if r.get("ok") else "非0"
        lines.append(f"{r.get('name')}: {r.get('cmd')} (cwd={r.get('cwd')}) -> exit {code}")
    return "\n".join(lines)


def _land_manifest(wt: Path, branch: str, merge_commit: str, pre_main: str) -> dict:
    """落盘清单：这次 land 到底把什么带进了 main。

    为什么要它：用户看到的落盘记录原本只有一句「已通过真实 npm run build，并落盘 main @ xxx」，
    `files: []` 空着。于是「走几步就说合并了、不知道他干了什么、也不知道做完没有」
    ——用户不是错觉，是台账里确实没有这三件验收对象：

      ① 改了什么   -> git diff --stat：文件清单 + 增删行数
      ② 过了什么   -> 每条验证门禁的 name/cmd/cwd/exit
      ③ 推进到哪   -> 这次改动落在哪条泳道、有没有对应的验收项

    只读、幂等：任何一步取不到就留空，绝不让落盘因诊断而失败。
    """
    out: dict = {"branch": branch, "merge_commit": merge_commit,
                 "changed_files": [], "total_stat": "", "gates": [],
                 "acceptance": [], "lane": branch, "probe_failed": False}
    # ① 变更清单：合并提交相对它的第一个父提交（= 合并前的 main）
    # 注意 git() 已内置 `-C REPO`，不能重复传 -C；这里直接用 subprocess 指到工作树。
    # probe_failed 与「空 diff」必须分开：空 diff 是「本次没带进内容」（合法且常见，
    # 例如分支内容已通过别的路径进了主干），取数失败是「不知道」——两者不能同话。
    try:
        base = f"{merge_commit}^1"
        numstat = subprocess.run(
            ["git", "-C", str(wt), "diff", "--numstat", base, merge_commit],
            capture_output=True, text=True, errors="replace")
        stat = subprocess.run(
            ["git", "-C", str(wt), "diff", "--stat", base, merge_commit],
            capture_output=True, text=True, errors="replace")
        if numstat.returncode != 0 or stat.returncode != 0:
            out["probe_failed"] = True
        else:
            files = []
            for line in (numstat.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    add, dele, path = parts
                    files.append({"path": path, "added": add, "deleted": dele})
            out["changed_files"] = files[:60]
            out["total_stat"] = (stat.stdout or "").strip().splitlines()[-1].strip() \
                if (stat.stdout or "").strip() else ""
    except Exception:
        out["probe_failed"] = True
    # ③ 泳道对应的验收项（本次 land 前已登记的，用于把「改动」挂到「验收」上）
    try:
        rows = read_jsonl(REPO / "ops" / "truth" / "acceptance.jsonl")
        latest = latest_by_id(rows)
        for rec in latest.values():
            if rec.get("lane") == branch:
                out["acceptance"].append({"id": rec.get("id"), "title": rec.get("title"),
                                          "status": rec.get("status")})
    except Exception:
        pass
    return out


def _manifest_text(m: dict, gates: list) -> str:
    """把落盘清单压成给人读的几行（写进 progress 的 summary，也是终端输出）。

    规则：不带裸标识符，先给人话，再给可核对证据。

    关键：必须分清三种情况，不能都说成「未取到」——
      · 真的带进了改动        → 报文件数与清单
      · 合并是空 diff（分支内容早已在主干） → 明说「没带进新内容」
      · 取数失败              → 明确标 `probe_failed`，别让读者以为是「没有」
    后两者混为一谈就是「把'没有'和'不知道'说成同一句」。
    """
    L = []
    files = m.get("changed_files") or []
    probe_failed = bool(m.get("probe_failed"))
    if probe_failed:
        L.append("改动：清单取数失败（不是「没有改动」，请自行 git show 核对）")
    elif files:
        shown = "、".join(f"{f['path']}（+{f['added']}/-{f['deleted']}）" for f in files[:5])
        more = f"，另有 {len(files) - 5} 个文件" if len(files) > 5 else ""
        L.append(f"改动 {len(files)} 个文件：{shown}{more}")
    else:
        L.append("改动：本次合并未带进新内容（分支内容已在主干，属空合并）")
    if m.get("total_stat"):
        L.append(m["total_stat"])
    if gates:
        gs = "；".join(f"{g.get('name')} exit {0 if g.get('ok') else '非0'}" for g in gates)
        L.append(f"门禁：{gs}")
    if m.get("acceptance"):
        a = "、".join(str(x.get("id")) for x in m["acceptance"][:4])
        L.append(f"对应验收项：{a}")
    return "｜".join(L)


def _record_land_progress(wt: Path, branch: str, commit: str, verify_results: list,
                          manifest: dict = None, pre_main: str = "") -> None:
    """把真实落盘事件和真实验收证据写进台账，并作为同一批 push 的一部分提交。

    写三类东西，缺一不可（用户的验收对象）：
      · progress   —— 这次落盘「做了什么」：变更清单 + 门禁 + 阶段位置
      · acceptance —— 「怎么证明」：逐条门禁的命令/cwd/退出码
      · 变更文件清单 —— 写进 progress 的 files 字段，供面板与 synthesize 直接引用
    """
    manifest = manifest or {}
    verified = bool(verify_results) and all(r.get("ok") for r in verify_results)
    gate_note = "验证门禁通过" if verified else "未通过验证门禁"
    human = _manifest_text(manifest, verify_results)
    changed = [f.get("path") for f in (manifest.get("changed_files") or []) if f.get("path")]
    stage_note = _roadmap_touch(manifest, changed)
    env = os.environ.copy()
    env.update({
        "KG_REPO": str(wt),
        "KG_TRUTH_LANE": branch,
        "KG_TRUTH_COMMIT": commit,
        "KG_TRUTH_LANDED": "1",
        "KG_TRUTH_STATUS": "done",
        "KG_TRUTH_FILES": ":".join(changed[:40]),
        "KG_TRUTH_SUMMARY": f"{branch} {gate_note}，已落盘 {MAIN} @ {commit[:10]}；{human}"
                            + (f"；{stage_note}" if stage_note else ""),
        "KG_TRUTH_SOURCE": "kgctl land",
    })

    added = subprocess.run(
        [sys.executable, str(RUNTIME_OPS / "truthctl.py"), "add", "progress",
         f"落盘 {branch}"],
        cwd=str(wt), env=env, capture_output=True, text=True)
    if added.returncode != 0:
        raise RuntimeError(f"写进展台账失败: {(added.stdout + added.stderr).strip()[:500]}")

    acc_env = os.environ.copy()
    acc_env.update({
        "KG_REPO": str(wt),
        "KG_TRUTH_LANE": branch,
        "KG_TRUTH_COMMIT": commit,
        "KG_TRUTH_STATUS": "verified" if verified else "todo",
        "KG_TRUTH_EVIDENCE": _verify_evidence(verify_results),
        "KG_TRUTH_SOURCE": "kgctl land",
        "KG_TRUTH_SUMMARY": f"{branch} 的验证门禁结果",
    })
    accepted = subprocess.run(
        [sys.executable, str(RUNTIME_OPS / "truthctl.py"), "add", "acceptance",
         f"{branch} 落盘验收"],
        cwd=str(wt), env=acc_env, capture_output=True, text=True)
    if accepted.returncode != 0:
        raise RuntimeError(f"写验收台账失败: {(accepted.stdout + accepted.stderr).strip()[:500]}")

    staged = subprocess.run(
        ["git", "-C", str(wt), "add", "ops/truth/progress.jsonl",
         "ops/truth/acceptance.jsonl"],
        capture_output=True, text=True)
    if staged.returncode != 0:
        raise RuntimeError(f"暂存真相信台账失败: {staged.stderr.strip()[:500]}")
    committed = subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", f"chore(truth): record landing {branch}"],
        capture_output=True, text=True)
    if committed.returncode != 0:
        raise RuntimeError(f"提交真相台账失败: {committed.stderr.strip()[:500]}")
    print(f"  进展台账 {added.stdout.strip()}")
    print(f"  验收台账 {accepted.stdout.strip()}")
    print(f"  本次落盘：{human}")


def _roadmap_touch(manifest: dict, changed: list) -> str:
    """阶段位置：这次落盘的改动碰到产品路线的哪一步。

    判据只取**可核对且足够特异**的东西，避免把 stage 5「入口适配器：Codex 与 Obsidian」
    这种通用词命中（分支名里到处都是 codex/）。顺序：

      1. `ops/truth/mainline.json` 的 roadmap 每步若能给出 `match` 关键词表，就用它；
      2. 否则用**泳道的业务名**（去掉 `codex/` 前缀后的 slug）与每步的 step/expected/
         deliverables 做词交集，且要求命中词长度 >= 5、不是通用词。
      3. 都不命中就返回空串——宁可不说，也不编一句「已推进阶段 1」。
    """
    try:
        ml = json.loads((REPO / "ops" / "truth" / "mainline.json").read_text())
    except Exception:
        return ""

    # 通用词黑名单：这些词在任何一步都会出现，不能作为命中依据。
    GENERIC = {"codex", "stage", "web", "test", "index", "main", "frontend", "backend",
               "src", "docs", "json", "ts", "css", "app", "code", "the", "and"}
    lane = (manifest.get("lane") or "")
    slug = re.sub(r"^codex/", "", lane).lower()
    tokens = {t for t in re.split(r"[^a-z0-9]+", slug + " " + " ".join(changed).lower())
              if len(t) >= 5 and t not in GENERIC}
    if not tokens:
        return ""
    for step in (ml.get("roadmap") or []):
        if not isinstance(step, dict):
            continue
        # ① 显式关键词表（若人在 mainline.json 里给了 match）
        explicit = [str(x).lower() for x in (step.get("match") or [])]
        if explicit and any(e in slug for e in explicit):
            return f"对应产品路线「{step.get('step')}」（当前标 {step.get('status', '?')}）"
        # ② 词交集
        text = " ".join([str(step.get("step", "")), str(step.get("expected", "")),
                         " ".join(step.get("deliverables") or [])]).lower()
        for t in tokens:
            if t in text:
                return (f"对应产品路线「{step.get('step')}」"
                        f"（当前标 {step.get('status', '?')}）")
    return ""


def _mirror_truth_into_trunk(wt: Path) -> None:
    """把状态根的账本镜像进落盘用的主干工作树。

    自包含安装下，账本在项目目录的 `ops/truth` 里，而主干工作树是从 `main` 检出的；
    若 `ops/truth` 没进过版本库，trunk 里连目录都不存在，写进展台账会直接崩。这里只做
    文件级拷贝（加法），让主干工作树拥有同一份账本，后续 `sync_trunk_truth` 才能把它
    作为真相层提交，与代码一起推回 `main`。
    """
    for rel in TRUTH_PATHS:
        src, dst = REPO / rel, wt / rel
        if not src.exists():
            continue
        if src.resolve() == dst.resolve():
            # 状态根本身就是落盘主干时（配置错误），源与目标同一个文件；
            # 直接跳过，绝不 shutil.copy2 到自己。
            continue
        if src.is_dir():
            for item in src.rglob("*"):
                if not item.is_file():
                    continue
                target = dst / item.relative_to(src)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _run_verify_gates(wt: Path, pre: str = "") -> tuple:
    """落盘前的验证门禁：逐条真实执行，返回 (ok, results, output)。

    门禁清单来自 ops/central.config.json 的 verify（没配就退化成 build 那一条）。
    verify_by_change 可按本次 merge 的实际 diff 追加相关门禁；没命中规则时仍保留
    build 作为真实验证底线。工作目录不存在时按失败处理，不抛未捕获异常。
    """
    changed = []
    try:
        base = pre or "HEAD"
        p = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", base, "HEAD"],
                           capture_output=True, text=True, errors="replace")
        if p.returncode == 0:
            changed = [line for line in (p.stdout or "").splitlines() if line.strip()]
    except Exception:
        changed = []
    selector = getattr(central_config, "verify_gates_for_changes", None)
    # Runtime copies can briefly lag the staged source copy. Falling back to the
    # stable gate list keeps landing possible while the change selector is absent.
    gates = (selector(CFG, changed) if callable(selector)
             else central_config.verify_gates(CFG))
    if not gates:
        return True, [], "(未配置验证门禁，跳过)"
    results, chunks, all_ok = [], [], True
    for g in gates:
        rel = g.get("cwd") or "."
        cwd = wt / rel
        if not cwd.exists():
            results.append({"name": g.get("name"), "cmd": g.get("cmd"), "cwd": rel,
                            "ok": False, "output": f"工作目录不存在：{cwd}"})
            chunks.append(f"[{g.get('name')}] cwd 不存在：{cwd}")
            all_ok = False
            continue
        p = subprocess.run(g["cmd"], shell=True, cwd=cwd, capture_output=True, text=True)
        out = (p.stdout or "") + (p.stderr or "")
        results.append({"name": g.get("name"), "cmd": g.get("cmd"), "cwd": rel,
                        "ok": p.returncode == 0, "output": out[-4000:]})
        chunks.append(f"[{g.get('name')}] $ {g.get('cmd')} (cwd={rel}) "
                      f"-> exit {p.returncode}\n{out[-1500:]}")
        if p.returncode != 0:
            all_ok = False
    return all_ok, results, "\n".join(chunks)


def _push_main_with_one_retry(wt: Path) -> str:
    """推送 trunk HEAD 到 main；若远端前进则同步并重跑验证后重试，最多 5 次。

    并发 daemon 会在 land 的 fetch 与 push 之间把 main 推前，第一次 push 收到
    ``(fetch first)`` 并不代表失败：同步 origin/main 后重试通常就成功。早前只
    重试一次，实测同一个分支会先 landed rc=0、几拍后又因第二次被并发 push 顶掉
    而抛 RuntimeError（daemon.log 21:48 source-coverage-map），把已落盘记成
    last_failures。真正的空合并没有新内容要带，若同步后远端已包含 HEAD，等同
    已落盘，不再重跑验证。
    """
    last = ""
    for attempt in range(5):
        pushed = subprocess.run(["git", "-C", str(wt), "push", "origin", "HEAD:main"],
                                capture_output=True, text=True)
        if pushed.returncode == 0:
            return (pushed.stdout + pushed.stderr).strip()
        last = (pushed.stdout + pushed.stderr).strip()
        if attempt >= 4:
            break
        print(f"× push 被拒（第 {attempt + 1} 次），远端可能前进；同步 origin/main 后重试")
        fetched = subprocess.run(["git", "-C", str(wt), "fetch", "origin", MAIN],
                                 capture_output=True, text=True)
        if fetched.returncode != 0:
            raise RuntimeError(f"push 后 fetch origin/{MAIN} 失败: {fetched.stderr.strip()[:500]}")
        remote = git("rev-parse", f"origin/{MAIN}")
        if remote == git("rev-parse", "HEAD"):
            print("  远端 origin/main 已包含 trunk HEAD；空合并等同已落盘")
            return ("origin/main 已包含 HEAD（空合并，无需重复推送）")
        synced = subprocess.run(
            ["git", "-C", str(wt), "merge", "--no-ff", f"origin/{MAIN}",
             "-m", "train: sync origin/main before landing"],
            capture_output=True, text=True)
        if synced.returncode != 0:
            subprocess.run(["git", "-C", str(wt), "merge", "--abort"],
                           capture_output=True, text=True)
            raise RuntimeError(
                f"远端 main 前进且自动同步出现冲突，未推送：{(synced.stdout + synced.stderr).strip()[:500]}")
        ok, _results, out = _run_verify_gates(wt, "")
        if not ok:
            raise RuntimeError(f"同步远端 main 后验证门禁失败，未推送：{out[-800:]}")
    raise RuntimeError(f"push origin HEAD:main 失败：{last[:800]}")


def _align_main_ref_to_origin(new: str = "") -> str:
    """把本地 main 引用对齐到 origin/main（push 成功后调用）。

    并发下绝不能用 CAS 从抓取时的旧 `pre` 推进 main：daemon 的 tick / supersede /
    land 都会推 main，任何一次 CAS 都可能撞上「cannot lock ref 'refs/heads/main':
    is at X but expected Y」，把一次已经 push 成功的落盘误判成失败（实测在 kg-trunk
    反复出现）。push 成功后 origin/main 才是真相，本地引用只需对齐它。
    """
    subprocess.run(["git", "-C", str(REPO), "fetch", "origin", MAIN],
                   capture_output=True, text=True)
    remote = git("rev-parse", f"origin/{MAIN}") or new
    local = git("rev-parse", MAIN)
    if local == remote or not remote:
        return local
    anc = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor",
                          local, remote], capture_output=True, text=True)
    if anc.returncode == 0:
        subprocess.run(["git", "-C", str(REPO), "update-ref",
                        f"refs/heads/{MAIN}", remote], check=False)
        return remote
    # 本地领先或分叉：保留本地引用，绝不因为一次引用竞争回退或丢提交。
    return local


def _origin_contains(commit: str) -> bool:
    """确认发布提交已经进入 origin/main，而不是只存在于本地发布工作树。"""
    contained = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", commit,
         f"origin/{MAIN}"],
        capture_output=True, text=True)
    return contained.returncode == 0


# 真相层与生成视图：它们本来就该落在主干工作树上，所以不算「污染」。land /
# supersede 先提交它们再落盘，而不是因为「主干有未提交改动」直接拒绝执行——
# 后者曾经让每一次 `kgctl tick` 都顺手把 land 锁死。
# 心跳窗口每拍都要写 docs/COCKPIT_BRIEF.md 与 docs/AGENT_SYNC.md。这两份文件不在
# TRUTH_PATHS 里时，tick 的 sync_trunk_truth 不会提交它们——心跳每写一次，共享检出就
# 多一份无人认领的在途改动（正是 PARALLEL_WORKFLOW「禁止制造在途改动」要禁止的形态）。
TRUTH_PATHS = ("ops/truth", "ops/tasks", "ops/context", "docs/PROJECT_STATUS.md",
               "docs/CONTROL_BOARD.md", "docs/CONTROL_PLANE.html",
               "docs/PRODUCT_MAINLINE.md", "docs/COCKPIT_BRIEF.md",
               "docs/AGENT_SYNC.md")
TRUTH_COMMIT_RE = re.compile(r"^(chore\(truth\)|train: (sync|merge))")

# 产品真相证据：这些文件由真实质量/运维命令生成，不能由旧真相同步或冲突合并覆盖。
TRUTH_KEEP_IF_BASELINE_MISSING = ("ops/truth/benchmark-contract-report.json",
                                  "ops/truth/stability-alerts.jsonl",
                                  "ops/truth/backup-restore-receipt.json")

# 36 题真实评分基线（2026-10-20 已恢复并验证）:
#   itemCount=36, averageScore=78.1, verifiedCount=27,
#   fabricatedCount=2, fabricatedSources=4, missingProvenanceCount=7,
#   claimUpgradeViolations=0, terminalSubstanceFailedItems=v1-action-04/v1-action-06
TRUTH_BENCHMARK_REAL_HASH = "a9e242bff237e3836484776640ce751e9470948e976bc1f4c795401d6767050f"


def trunk_status(wt: Path):
    # 只看已跟踪改动：未跟踪文件（如 ops/__pycache__/）不影响合并，若真要覆盖它们
    # `git merge` 自己会失败并被 land 安全丢弃，不该在这里当成「污染」挡住落盘。
    out = subprocess.run(["git", "-C", str(wt), "status", "--porcelain",
                          "--untracked-files=no"],
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def is_truth_path(line: str) -> bool:
    """judge whether a `git status --porcelain` line belongs to the truth layer."""
    body = line[3:].strip() if len(line) > 3 else line.strip()
    if " -> " in body:
        body = body.split(" -> ", 1)[1]
    body = body.strip('"')
    return any(body == t or body.startswith(t.rstrip("/") + "/") for t in TRUTH_PATHS)


def trunk_truth_commits(wt: Path, base: str = None):
    base = base or MAIN
    out = git("-C", str(wt), "log", "--format=%s", f"{base}..HEAD")
    return [line for line in out.split("\n") if line.strip()]


def trunk_head_is_truth_only(wt: Path, base: str = None) -> bool:
    """HEAD 相对 base 是否只动了真相层文件。

    判据必须是「改了什么文件」，不能只看提交信息：`train: merge <分支>` 这类合并提交
    的消息会命中 TRUTH_COMMIT_RE，但它可能把整整一个代码分支并进真相层——一旦混入代码
    提交，真相层就永远推不上去（实测：分支里几十条 fix() 提交混进来后 push 被永久拦死）。
    改成三向 diff 之后，只有「HEAD 侧相对 base 的净改动全是真相层文件」才放行：合并提交
    携带的代码会被正确拦下，纯真相层的 train 合并依旧放行。
    """
    base = base or MAIN
    out = git("-C", str(wt), "diff", "--name-only", f"{base}...HEAD")
    files = [f.strip().strip('"') for f in out.split("\n") if f.strip()]
    if not files:
        return False
    return all(any(f == t or f.startswith(t.rstrip("/") + "/") for t in TRUTH_PATHS)
               for f in files)


def _hash_blob(ref: str, rel: str) -> str:
    """取 ref 下文件的 Git blob SHA-1；文件不存在时返回空串。"""
    p = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "--quiet",
                        f"{ref}:{rel}"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def _sha256_file(path: Path) -> str:
    """取文件内容 SHA-256，与 TRUTH_BENCHMARK_REAL_HASH 固定常量同口径。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _remote_benchmark_sha() -> str:
    """取 origin/{MAIN} 下 36 题基线的 SHA-256；文件不存在时返回空串。"""
    rel = "ops/truth/benchmark-contract-report.json"
    if not _hash_blob(f"origin/{MAIN}", rel):
        return ""
    blob = subprocess.run(
        ["git", "-C", str(REPO), "show", f"origin/{MAIN}:{rel}"],
        capture_output=True,
        text=True,
    ).stdout
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ensure_truth_baselines(wt: Path = None) -> bool:
    """防止真相层把真实评分证据覆盖成旧/伪快照。

    当前唯一带真实运行上下文且保持 RED 的 36 题报告是 ops/truth/
    benchmark-contract-report.json。tick 同步会先恢复保护文件；这里再用固定哈希
    复核它没有被旧状态覆盖。文件缺失时保持旧行为（自包含插件可用于任何项目），
    哈希不符则 fail-closed，不让错误版本继续被后续合并/提交当成事实。
    """
    base = wt or REPO
    rel = "ops/truth/benchmark-contract-report.json"
    target = base / rel
    if not target.exists():
        return True
    local_sha = _sha256_file(target)
    if local_sha == TRUTH_BENCHMARK_REAL_HASH:
        remote_sha = _remote_benchmark_sha()
        if remote_sha and remote_sha != TRUTH_BENCHMARK_REAL_HASH:
            print(
                f"  × 远端保护证据本身已被污染：{rel} "
                f"remote={remote_sha[:10]} != {TRUTH_BENCHMARK_REAL_HASH[:10]}"
            )
            return False
        return True
    remote_sha = _remote_benchmark_sha()
    if remote_sha and remote_sha != TRUTH_BENCHMARK_REAL_HASH:
        print(
            f"  × 稳定性基准被覆盖且远端也不含真实基线：{rel} "
            f"local={local_sha[:10]} remote={remote_sha[:10]}"
        )
        return False
    if remote_sha == TRUTH_BENCHMARK_REAL_HASH:
        print(f"  基准内容被覆盖（{local_sha[:10]} != {TRUTH_BENCHMARK_REAL_HASH[:10]}），从 origin/{MAIN} 恢复真实 36 题报告")
        blob = subprocess.run(["git", "-C", str(base), "show", f"origin/{MAIN}:{rel}"],
                              capture_output=True, text=True)
        if blob.returncode != 0 or not blob.stdout:
            print(f"  × 恢复失败：origin/{MAIN} 的 {rel} 不可读")
            return False
        target.write_text(blob.stdout, encoding="utf-8")
        return True
    print(
        f"  × 稳定性基准被覆盖且远端也不含真实基线：{rel} "
        f"local={local_sha[:10]} remote={remote_sha[:10] or '(missing)'}"
    )
    return False


def _snapshot_truth_files(wt: Path) -> dict:
    """把主干工作树上真相层文件的内容读进内存（路径 -> bytes）。

    用来在「把 HEAD 切回 main」之前兜底：checkout 会用 main 的版本覆盖工作树里的
    ops/truth、ops/tasks 等文件，若不先抓一份，路由结果、台账追加这些只存在于本地
    的真相改动就永久丢了（实测：一次 `checkout --detach main` 把刚路由好的 inbox 打回
    未路由）。只读，不改任何文件。
    """
    snap = {}
    for rel in TRUTH_PATHS:
        root = wt / rel
        if root.is_dir():
            for item in root.rglob("*"):
                if item.is_file():
                    snap[str(item.relative_to(wt))] = item.read_bytes()
        elif root.is_file():
            snap[rel] = root.read_bytes()
    return snap


def _restore_truth_files(wt: Path, snap: dict) -> None:
    """把 `_snapshot_truth_files` 抓到的真相文件写回工作树（纯加法，只写真相路径）。"""
    for rel, data in snap.items():
        dst = wt / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)


def reset_trunk_head(wt: Path) -> None:
    """让主干工作树的 HEAD 对齐 main，但绝不丢真相层。

    失败的 land 会在主干工作树留下一个 merge 提交（main 未动），必须清掉，否则下一次
    land 会把上一次失败的合并一起推上 main。真相层状态（ops/truth、ops/tasks、
    docs/PROJECT_STATUS.md 等）是有价值的本地状态，必须保留。

    两种情形分开处理：
      * HEAD 相对 main 只动了真相层文件 -> 直接把 origin/main 合到真相提交之上，提交
        与文件都保留。
      * HEAD 夹带了非真相内容（例如一次没推成的 `train: merge <分支>`）-> 切回 main，
        但在切换前先把真相文件抓进内存，切完再写回，保证路由结果、台账追加一条不丢。
        被丢弃的代码来自仍存在的源分支，可随时重放；真相层不能丢，因为它没有别处副本。
    """
    if not _ensure_truth_baselines(wt):
        print("  × 稳定性基准保护失败，拒绝重置主干工作树")
        sys.exit(1)
    if trunk_head_is_truth_only(wt):
        merged = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", f"origin/{MAIN}",
                                 "-m", "train: sync origin/main over truth state"],
                                capture_output=True, text=True)
        if merged.returncode != 0:
            # 冲突通常全部落在真相层：生成视图（CONTROL_BOARD/CONTROL_PLANE/
            # PROJECT_STATUS）与追加式台账。这些正是 _resolve_truth_merge 会确定性
            # 合并的东西——生成视图以远端为底、JSONL 做行级并集、JSON 递归并集。
            # 不在这里复用，就会把上一次失败 land 留下的真相提交当死结，永久锁死落盘。
            if _resolve_truth_merge(wt):
                print(f"  保留主干工作树上 {len(trunk_truth_commits(wt))} 条真相提交"
                      f"并同步 origin/{MAIN}")
                return
            subprocess.run(["git", "-C", str(wt), "merge", "--abort"],
                           capture_output=True, text=True)
            print("× 真相层提交与 origin/main 合并冲突，已放弃：", merged.stderr.strip()[:300])
            sys.exit(1)
        print(f"  保留主干工作树上 {len(trunk_truth_commits(wt))} 条真相提交并同步 origin/{MAIN}")
        return
    snap = _snapshot_truth_files(wt)
    ok = subprocess.run(["git", "-C", str(wt), "checkout", "--detach", MAIN],
                        capture_output=True, text=True)
    if ok.returncode != 0:
        print("× 主干工作树切不到 main：", ok.stderr.strip()[:200])
        sys.exit(1)
    _restore_truth_files(wt, snap)
    if snap:
        print(f"  已把 {len(snap)} 个真相层文件从切换前状态写回（路由/台账不丢）")


def sync_trunk_truth(wt: Path, reason: str) -> bool:
    """把主干工作树上真相层 / 生成视图的改动提交掉（只显式列出这些路径）。"""
    pending = [line for line in trunk_status(wt) if is_truth_path(line)]
    if not pending:
        return False
    dirty = [line for line in trunk_status(wt) if not is_truth_path(line)]
    if dirty:
        # 共享检出里可能有并行任务在写代码/产品文件。只提交真相层会把它们吞进
        # `chore(truth)`，并让本地 main 与 origin/main 分叉，随后 land 永久卡死。
        # 这里必须和 land / supersede 一样 fail-closed：先撤回已暂存的真相层，
        # 不留下半暂存状态；等真正只有真相层改动时再同步。
        subprocess.run(["git", "-C", str(wt), "reset", "-q", "--", *TRUTH_PATHS],
                       capture_output=True, text=True)
        print(f"  主干工作树有非真相层改动，拒绝同步真相层：{wt}")
        for line in dirty[:20]:
            print("  " + line)
        return False
    if not _ensure_truth_baselines(wt):
        print("  × 稳定性基准保护失败，拒绝提交真相层")
        return False
    for rel in TRUTH_PATHS:
        if (wt / rel).exists():
            subprocess.run(["git", "-C", str(wt), "add", "--", rel],
                           capture_output=True, text=True)
    staged = subprocess.run(["git", "-C", str(wt), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True).stdout.strip()
    if not staged:
        return False
    committed = subprocess.run(["git", "-C", str(wt), "commit", "-m", f"chore(truth): {reason}"],
                               capture_output=True, text=True)
    if committed.returncode != 0:
        print("  " + (committed.stdout + committed.stderr).strip()[:200])
        return False
    print(f"  真相层 {len(staged.splitlines())} 个文件已提交（chore(truth): {reason}）")
    return True


def _stage_blob(wt: Path, stage: int, rel: str) -> str:
    p = subprocess.run(["git", "-C", str(wt), "show", f":{stage}:{rel}"],
                       capture_output=True, text=True, errors="replace")
    return p.stdout if p.returncode == 0 else ""


def _union_jsonl_text(a: str, b: str) -> str:
    seen, lines = set(), []
    for text in (a, b):
        for line in text.splitlines():
            if line and line not in seen:
                seen.add(line)
                lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def _merge_json_value(a, b):
    """真相 JSON 合并：字典递归，列表去重保序；冲突键以远端为底、本地覆盖。"""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge_json_value(out.get(k), v) if k in out else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        out, seen = [], set()
        for item in a + b:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out
    return b if b is not None else a


def _resolve_truth_merge(wt: Path) -> bool:
    """只在冲突文件全部属于真相层时自动解；任何代码冲突立即放弃。"""
    unmerged = subprocess.run(
        ["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True).stdout.splitlines()
    if not unmerged:
        return False
    non_truth = [rel for rel in unmerged if not is_truth_path(f" M {rel}")]
    if non_truth:
        subprocess.run(["git", "-C", str(wt), "merge", "--abort"], capture_output=True, text=True)
        print(f"  真相层同步遇到非真相文件冲突，已放弃：{', '.join(non_truth[:5])}")
        return False
    for rel in unmerged:
        ours, theirs = _stage_blob(wt, 2, rel), _stage_blob(wt, 3, rel)
        target = wt / rel
        if rel.endswith(".jsonl"):
            target.write_text(_union_jsonl_text(ours, theirs), encoding="utf-8")
        elif rel.endswith(".json"):
            if rel in TRUTH_KEEP_IF_BASELINE_MISSING:
                # Product evidence snapshots are generated by real repo commands;
                # truth tick must not overwrite newly landed evidence with stale state.
                # stage3 may itself be a polluted truth snapshot, so verify the
                # pinned byte hash before allowing this conflict resolution to land.
                target.write_text(theirs or ours, encoding="utf-8")
                if rel == "ops/truth/benchmark-contract-report.json" and _sha256_file(target) != TRUTH_BENCHMARK_REAL_HASH:
                    print("  × 稳定性基准在真相冲突中被污染，拒绝该次同步")
                    subprocess.run(["git", "-C", str(wt), "merge", "--abort"],
                                   capture_output=True, text=True)
                    return False
            else:
                try:
                    merged = _merge_json_value(json.loads(theirs), json.loads(ours))
                    write_json(target, merged)
                except Exception:
                    target.write_text(theirs or ours, encoding="utf-8")
        else:
            target.write_text(theirs or ours, encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "--", rel], check=True)
    committed = subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "chore(truth): merge origin/main into truth state"],
        capture_output=True, text=True)
    if committed.returncode != 0:
        print(f"  真相层合并提交失败：{(committed.stdout + committed.stderr).strip()[:300]}")
        return False
    print("  真相层与 origin/main 已自动合并（JSONL 并集，生成视图以远端为底）")
    return True


def _merge_truth_from_origin() -> bool:
    if not _ensure_truth_baselines(REPO):
        print("  × 稳定性基准保护失败，拒绝合并 origin/main")
        return False
    rc, _out = _git_bounded(["merge", "--no-ff", f"origin/{MAIN}",
                             "-m", "train: merge origin/main into truth state"], 180)
    if rc == 0:
        return True
    if _resolve_truth_merge(REPO):
        return True
    return False


def _git_bounded(args, timeout: int, label: str = ""):
    """有界的 git 调用：超时就杀，返回 (rc, 合并后的输出)，绝不无限期挂着。

    事故（2026-10-15 22:19 那一轮）：tick 挂满 1800s 被守护进程杀掉，日志里只剩
    `[tick] rc=124`，连卡在哪一步都看不出。tick 的网络步骤当时用的是没有 timeout
    的 subprocess.run —— 只要 TCP 卡住，整轮就是黑洞：真相层不提交、不推送，
    共享检出也就停在上一个 commit 上（实测落后 origin/main 20 个 commit）。
    这里给每一步一个上限，把「无限期挂着」变成「一条可见的失败」。
    """
    try:
        p = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                           text=True, errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"git {' '.join(args)} 超时（>{timeout}s）"
    except OSError as exc:  # 解释器/可执行文件问题，也要有明确结果而不是抛穿 tick
        return 125, f"git {' '.join(args)} 起不来：{exc}"


def _push_truth_state_locked() -> None:
    """把主干工作树上「只有真相层提交」的提交推回 origin/main。

    远端前进不是终止条件：真相层是追加式 JSONL，先合并远端再推送；只有非真相文件冲突
    才放弃。这样「本地 inbox / 台账永远推不上 main」不会再成为暗坑。
    """
    frc, fout = _git_bounded(["fetch", "origin", MAIN], 120)
    if frc != 0:
        print(f"  fetch origin/{MAIN} 失败：{fout.strip()[:200]}")
        return
    if not _ensure_truth_baselines(REPO):
        print("  × 稳定性基准保护失败，拒绝推送真相层")
        return
    ahead = int(git("rev-list", "--count", f"origin/{MAIN}..HEAD") or 0)
    if ahead == 0:
        return
    if not trunk_head_is_truth_only(REPO, f"origin/{MAIN}"):
        print(f"  主干工作树有 {ahead} 条提交但不是纯真相层提交，跳过自动推送（交给 land）")
        return
    ff = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor",
                         f"origin/{MAIN}", "HEAD"], capture_output=True, text=True)
    if ff.returncode != 0:
        if not _merge_truth_from_origin():
            return
        ahead = int(git("rev-list", "--count", f"origin/{MAIN}..HEAD") or 0)
    prc, pout = _git_bounded(["push", "origin", f"HEAD:{MAIN}"], 180)
    if prc != 0:
        print(f"  真相层推送失败，{ahead} 条提交留在本地：{pout.strip()[:200]}")
        return
    subprocess.run(["git", "-C", str(REPO), "update-ref", f"refs/heads/{MAIN}", "HEAD"],
                   check=False)
    print(f"  真相层 {ahead} 条提交已推送 origin/{MAIN}")


def push_truth_state() -> None:
    """真相推送与 land 共用发布锁，模型 worker 并发不受影响。"""
    if not _acquire_lock("truth-push"):
        print("  已有 land/发布在推进，真相层推送推迟到下一拍")
        return
    try:
        _push_truth_state_locked()
    finally:
        _release_lock()


def load_resolve() -> dict:
    if not RESOLVE.exists():
        return {"tasks": []}
    try:
        data = json.loads(RESOLVE.read_text(encoding="utf-8"))
    except Exception:
        return {"tasks": []}
    if not isinstance(data, dict):
        return {"tasks": []}
    data.setdefault("tasks", [])
    return data


def _write_resolve_record(rec: dict) -> None:
    data = load_resolve()
    data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # 一条源分支只保留一条当前记录：按 slug 或源分支去重。否则早期留下的
    # needs_agent 会和后面的 landed 并存，--list 把已收口的分支继续报成待解。
    key = rec.get("slug")
    branch = rec.get("branch")
    tasks = [t for t in data.get("tasks", [])
             if t.get("slug") != key and (not branch or t.get("branch") != branch)]
    tasks.append(rec)
    tasks.sort(key=lambda t: t.get("updated") or "", reverse=True)
    data["tasks"] = tasks
    write_json(RESOLVE, data)


def _landed_resolve_branches() -> set:
    """已经 resolve 落盘过的源分支集合。用来防止同一条冲突被重复拆。"""
    return {t.get("branch") for t in load_resolve().get("tasks", [])
            if t.get("status") == "landed" and t.get("branch")}


def _open_resolve_sources() -> set:
    """仍在进行（未落盘、未放弃）的冲突任务对应的源分支集合。"""
    return {t.get("branch") for t in load_resolve().get("tasks", [])
            if t.get("status") in ("needs_agent", "gate_fail") and t.get("branch")}


def _decided_resolve_branches() -> set:
    """已经给出结论（落盘 landed 或明确放弃 abandoned）的源分支集合。

    `abandoned` 也落进这个集合，是为了让「放弃」这个决定留痕：否则守护进程下一轮
    又把它当成候选捡回来重拆，`resolve --list` 里会反复冒出同一条已裁决的分支。
    人工想重拆时仍然可以显式 `kgctl resolve codex/<分支> --go`，自动拆解不再自动回头。
    """
    return {t.get("branch") for t in load_resolve().get("tasks", [])
            if t.get("status") in ("landed", "abandoned") and t.get("branch")}


def _open_resolve_tasks() -> list:
    """还没收口的冲突任务（needs_agent / gate_fail），已收口的旧记录不算。

    早期留下的 needs_agent 记录会和后来的 landed/abandoned 并存，不去重就会把
    已经收口的分支一直报成「待解」——`--status` 与 `--list` 必须用同一套口径。
    """
    tasks = load_resolve().get("tasks", [])
    settled = {t.get("slug") for t in tasks if t.get("status") in ("landed", "abandoned")}
    settled |= {t.get("branch") for t in tasks if t.get("status") in ("landed", "abandoned")}
    return [t for t in tasks
            if t.get("status") in ("needs_agent", "gate_fail")
            and t.get("slug") not in settled and t.get("branch") not in settled]


def _find_resolve_task(target: str):
    tasks = load_resolve().get("tasks", [])
    settled = {t.get("slug") for t in tasks if t.get("status") in ("landed", "abandoned")}
    settled |= {t.get("branch") for t in tasks if t.get("status") in ("landed", "abandoned")}
    for t in tasks:
        if t.get("status") not in ("needs_agent", "gate_fail"):
            continue
        if t.get("slug") in settled or t.get("branch") in settled:
            continue
        if target in (t.get("slug"), t.get("branch"), t.get("resolve_branch")):
            return t
    return None


def _resolve_slug(branch: str) -> str:
    return slugify(branch[len(BRANCH_PREFIX):])


def _resolve_worktree_path(slug: str) -> Path:
    return REPO.parent / f"{REPO.name}-resolve-{slug}"


def _resolve_branch_name(slug: str) -> str:
    return f"{BRANCH_PREFIX}resolve-{slug}"


def _pick_resolve_candidate(max_files: int, max_lines: int):
    """挑一条「冲突文件少、未落改动也小」的冲突分支。返回 (候选, 超体量的)。"""
    rows = scan()
    open_src = _open_resolve_sources()
    decided_src = _decided_resolve_branches()
    # 守护进程把「拆不动 / 门禁没过」的分支名放进 KG_RESOLVE_SKIP，避免每个周期
    # 反复拆同一条。手动执行不带该环境变量，行为不变。
    skip = {b for b in os.environ.get("KG_RESOLVE_SKIP", "").split(":") if b}
    bmap = conflict_burden_map(rows)
    cands, oversize = [], []
    for r in rows:
        if r["kind"] != "CONFLICT" or _is_generated_branch(r["branch"]):
            continue
        if r["branch"] in open_src or r["branch"] in decided_src or r["branch"] in skip:
            continue
        burden = bmap.get(r["branch"], len(r["conflicts"]))
        nf, nl = unlanded_diff_size(r["branch"])
        if burden > max_files:
            continue
        row = (burden, nl, nf, r["branch"])
        (cands if nl <= max_lines else oversize).append(row)
    cands.sort()
    oversize.sort()
    return cands, oversize


def _write_resolve_task_md(rec: dict) -> None:
    RESOLVE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# 冲突任务：{rec['slug']}", "",
        f"- 源分支：`{rec['branch']}`",
        f"- 执行分支：`{rec['resolve_branch']}`",
        f"- 工作树：`{rec['worktree']}`",
        f"- 基于：`{rec['base_main'][:12]}`（origin/{MAIN}）",
        f"- 状态：`{rec.get('status')}`",
        "",
        "## 要解的冲突文件（保留的 marker）",
    ]
    uf = rec.get("unresolved_files") or []
    lines += [f"- `{f}`" for f in uf] or ["- （无）"]
    if rec.get("auto_resolved_jsonl"):
        lines += ["", "## 已自动并集的 JSONL（行级并集）",
                  *[f"- `{f}`" for f in rec["auto_resolved_jsonl"]]]
    lines += [
        "", "## 怎么做",
        f"1. 进 `{rec['worktree']}`，逐个打开上面列出的文件，把 `<<<<<<<` / `=======` / "
        "`>>>>>>>` 改成正确的最终代码——想清楚两边意图，不是机械二选一。",
        "2. 显式 `git add <文件>`（不要 `-A`），`git commit -m \"resolve: ...\"`。",
        f"3. `python3 ops/kgctl.py resolve --finish {rec['slug']} --go`：无 marker 且过了 "
        "build 门禁才会落盘。",
        "",
        "## 硬约束",
        "- 不 `git add -A`、不 `checkout .`、不 `stash`、不 `reset --hard`；只动本工作树里自己的文件。",
        "- 不用 `--ours`/`--theirs` 强行盖掉一侧；两边意图都要保住，或明确取舍并写进提交信息。",
        "- main 与源分支都不动；落盘统一交给 `resolve --finish`。",
    ]
    write_text(RESOLVE_DIR / f"{rec['slug']}.md", "\n".join(lines) + "\n")


def _resolve_land(rec: dict, wt: Path) -> None:
    """把 resolve 分支交给 land 走完整门禁；过了落盘并回收 worktree，不过则留作任务。"""
    resolve_branch = rec["resolve_branch"]
    res = subprocess.run([sys.executable, str(Path(__file__).resolve()), "land", resolve_branch],
                         capture_output=True, text=True)
    out = (res.stdout or "").rstrip()
    print(out)
    rec["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # land 在写门禁之前可能因分支已被合入而短路（"已完全合并，无需 land"）。那同样是
    # 「内容已进 main」的收口信号，不能因为少了落盘字样就把已落盘的分支退回 gate_fail。
    already_merged = "已完全合并" in out
    if res.returncode == 0 and ("已落盘并推送 origin" in out or already_merged):
        rec["status"] = "landed"
        _write_resolve_record(rec)
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, text=True)
        if already_merged:
            print(f"✓ {resolve_branch} 内容已全在 {MAIN}（无未落提交）；标记 landed，worktree 已回收。")
        else:
            print(f"✓ {resolve_branch} 已通过门禁并落盘；worktree 已回收，源分支 {rec['branch']} 未改动。")
        print(f"RESOLVE_RESULT branch={rec['branch']} outcome=landed")
        return
    rec["status"] = "gate_fail"
    rec["gate_output"] = out[-1500:]
    _write_resolve_record(rec)
    _write_resolve_task_md(rec)
    print(f"× 门禁未通过：{resolve_branch} 留在 {wt}，已登记任务 {rec['slug']}（gate_fail）。")
    print(f"  在该工作树修到过门禁，再 kgctl resolve --finish {rec['slug']} --go。")
    print(f"RESOLVE_RESULT branch={rec['branch']} outcome=gate_fail")


def _resolve_prepare(args) -> None:
    go = "--go" in args
    auto = "--auto" in args
    max_files, max_lines = RESOLVE_MAX_FILES, RESOLVE_MAX_LINES
    for i, a in enumerate(args):
        if a == "--max-files" and i + 1 < len(args):
            try:
                max_files = int(args[i + 1])
            except ValueError:
                pass
        if a == "--max-lines" and i + 1 < len(args):
            try:
                max_lines = int(args[i + 1])
            except ValueError:
                pass
    branch = None if auto else next((a for a in args if not a.startswith("--")), None)
    if branch and not branch.startswith(BRANCH_PREFIX):
        sys.exit(f"只允许 resolve {BRANCH_PREFIX}* 分支")
    oversize = []
    if branch:
        if not git("rev-parse", "--verify", branch):
            sys.exit(f"找不到分支 {branch}")
        if branch in _landed_resolve_branches():
            print(f"{branch} 已经 resolve 落盘过（见 ops/tasks/resolve.json），不重复拆。")
            return
        burden = conflict_burden(branch)
        nf, nl = unlanded_diff_size(branch)
        if burden == 0:
            print(f"{branch} 其实能干净合并（merge-tree 无冲突文件）："
                  f"用 kgctl land {branch} 即可，不需要解冲突。")
            print(f"RESOLVE_RESULT branch={branch} outcome=clean")
            return
    else:
        cands, oversize = _pick_resolve_candidate(max_files, max_lines)
        if not cands:
            extra = f"，另有 {len(oversize)} 条冲突文件少但改动行数超闸门" if oversize else ""
            print(f"没有「冲突文件 <= {max_files} 且未落行数 <= {max_lines}」的冲突分支可拆{extra}。")
            print("  这些分支先 kgctl converge 收拢，或人工判断；不要一次把几十个文件塞给一个 agent。")
            return
        burden, nl, nf, branch = cands[0]
    conf = conflict_files(branch)
    print(f"→ {branch}：冲突文件 {len(conf)} 个（闸门 {max_files}），未落改动 {nl} 行（闸门 {max_lines}）")
    for f in conf[:12]:
        print(f"    {f}")
    if len(conf) > 12:
        print(f"    ... 共 {len(conf)} 个")
    if nl > max_lines:
        print(f"× 未落改动 {nl} 行超过闸门 {max_lines}：先收拢或人工判断，不自动拆。")
        print(f"RESOLVE_RESULT branch={branch} outcome=too_big")
        return
    if not go:
        print("预演结束。加 --go 会在 origin/main 上开一个 resolve worktree，把该分支合进来："
              "append-only JSONL 冲突自动行级并集，其余保留 marker 交给 agent；解完用 "
              "kgctl resolve --finish <slug> --go 走同一道门禁后落盘。")
        return
    if _lock_blocked():
        sys.exit(f"已有 land/port/converge/resolve 在进行（{LOCK}）")
    slug = _resolve_slug(branch)
    resolve_branch = _resolve_branch_name(slug)
    wt = _resolve_worktree_path(slug)
    if git("rev-parse", "--verify", resolve_branch):
        print(f"× {resolve_branch} 已存在（可能上次留下的任务）。先 "
              f"resolve --finish/--abandon 收口再重跑。")
        print(f"RESOLVE_RESULT branch={branch} outcome=blocked")
        return
    if wt.exists():
        print(f"× {wt} 已存在但不是本命令登记的任务，跳过。")
        print(f"RESOLVE_RESULT branch={branch} outcome=blocked")
        return
    _align_main()
    base = git("rev-parse", f"origin/{MAIN}") or git("rev-parse", MAIN)
    add = subprocess.run(["git", "-C", str(REPO), "worktree", "add", "-b", resolve_branch,
                          str(wt), base], capture_output=True, text=True)
    if add.returncode != 0:
        sys.exit(f"建 worktree 失败：{(add.stdout + add.stderr).strip()[:400]}")
    _link_side_dirs(wt)
    rec = {
        "slug": slug, "branch": branch, "resolve_branch": resolve_branch, "worktree": str(wt),
        "base_main": base, "source_head": git("rev-parse", branch),
        "source_commits": unlanded_commits(branch),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "conflict_files": conf, "unresolved_files": [], "auto_resolved_jsonl": [],
        "status": "preparing",
    }
    merged = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", branch,
                             "-m", f"resolve: merge {branch}"], capture_output=True, text=True)
    if merged.returncode == 0:
        print("  merge ok：源分支能直接合进 main，进入落盘门禁。")
        return _resolve_land(rec, wt)
    unresolved = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
                                capture_output=True, text=True).stdout.splitlines()
    auto = []
    for rel in unresolved:
        if rel.endswith(".jsonl"):
            _merge_jsonl_conflict(wt, rel)
            auto.append(rel)
    _stage_unmerged(wt, unresolved)
    commit = subprocess.run(["git", "-C", str(wt), "commit", "-m",
                             f"wip(resolve): merge {branch} into {resolve_branch}"
                             " (conflict markers kept)"], capture_output=True, text=True)
    if commit.returncode != 0:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(REPO), "branch", "-D", resolve_branch],
                       capture_output=True, text=True)
        print(f"× 提交冲突合并失败，已回滚 {resolve_branch}："
              f"{(commit.stdout + commit.stderr).strip()[:300]}")
        print(f"RESOLVE_RESULT branch={branch} outcome=blocked")
        return
    remaining = [r for r in unresolved if r not in auto]
    rec.update({"conflict_files": unresolved, "unresolved_files": remaining,
                "auto_resolved_jsonl": auto})
    if remaining:
        rec["status"] = "needs_agent"
        _write_resolve_record(rec)
        _write_resolve_task_md(rec)
        print(f"  自动并集 {len(auto)} 个 JSONL；保留 {len(remaining)} 个 marker 待解。")
        print(f"  已登记任务 {slug}：worktree={wt}")
        print(f"  解完 marker 后跑：kgctl resolve --finish {slug} --go")
        print(f"RESOLVE_RESULT branch={branch} outcome=needs_agent")
        return
    print(f"  全部冲突已确定性解掉（{len(auto)} 个 JSONL 行级并集），进入落盘门禁。")
    return _resolve_land(rec, wt)


def _resolve_finish(target: str, go: bool) -> None:
    rec = _find_resolve_task(target)
    if not rec:
        sys.exit(f"找不到进行中的 resolve 任务：{target}（用 kgctl resolve --list 看）")
    wt = Path(rec["worktree"])
    resolve_branch = rec["resolve_branch"]
    if not wt.exists():
        sys.exit(f"任务 {rec['slug']} 的 worktree 不存在：{wt}")
    unresolved = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
                                capture_output=True, text=True).stdout.splitlines()
    if unresolved:
        print(f"× {len(unresolved)} 个文件仍是未合并状态，先解干净：{', '.join(unresolved[:6])}")
        return
    grep = subprocess.run(["git", "-C", str(wt), "grep", "-l", "-E", "^(<<<<<<<|>>>>>>>)"],
                          capture_output=True, text=True)
    markers = [x for x in grep.stdout.splitlines() if x.strip()]
    if markers:
        print(f"× 树里仍有 {len(markers)} 个文件带冲突标记，先解干净："
              f"{', '.join(m.strip() for m in markers[:6])}")
        return
    st = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                        capture_output=True, text=True).stdout.strip()
    if st:
        print(f"× worktree 还有未提交改动，先显式 add + commit：\n{st[:800]}")
        return
    print(f"→ {rec['slug']}（{resolve_branch}）已解干净，进入落盘门禁。")
    if not go:
        print("  加 --go 走 land 门禁并落盘。")
        return
    return _resolve_land(rec, wt)


def _resolve_abandon(target: str, go: bool, reason: str = "") -> None:
    rec = _find_resolve_task(target)
    if not rec:
        # 没有 open 任务也要能留痕放弃。冲突队列里真正解不动的是「超体量」分支：
        # 它们在 `resolve --go` 的闸门处（未落改动 > RESOLVE_MAX_LINES）就被拒绝，
        # 永远拿不到一条 open 记录，而「这条不该合」依然是必须写下来的决定——否则
        # 队列每轮都把同一条重新报出来，用户看到的是一个永远减不下去的数字。
        # 只接受完整 codex/ 分支名，并拒绝「已合并」「与 main 无冲突」这两种本该走
        # land/reap 的分支，避免把能落地的工作误标成放弃。
        branch = target if target.startswith(BRANCH_PREFIX) else ""
        if not branch:
            sys.exit(f"找不到进行中的 resolve 任务：{target}（用 kgctl resolve --list 看）")
        ahead, _behind = counts(branch)
        if ahead == 0:
            sys.exit(f"{branch} 已完全合并，无需 abandon（可用 kgctl reap --go 回收）。")
        conf = conflict_files(branch)
        if not conf:
            sys.exit(f"{branch} 与 {MAIN} 无冲突文件：应走 kgctl land {branch}，不要 abandon。")
        if not go:
            print(f"{branch}：没有进行中的 resolve 任务（属于「有冲突·未拆解」）。")
            print("  加 --go 会写一条 abandoned 决定：源分支原样不动，只是不再计入冲突队列；")
            print(f"  以后想重拆仍可显式 kgctl resolve {branch} --go。")
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        rec = {
            "slug": _resolve_slug(branch),
            "branch": branch,
            "base_main": git("rev-parse", MAIN),
            "source_head": git("rev-parse", branch),
            "source_commits": [c for c in git("rev-list", f"{MAIN}..{branch}").splitlines() if c][:50],
            "conflict_files": conf,
            "unresolved_files": conf,
            "auto_resolved_jsonl": [],
            "created": stamp,
            "updated": stamp,
            "status": "abandoned",
        }
        if reason:
            rec["reason"] = reason
        _write_resolve_record(rec)
        print(f"已记录放弃决定（未合并）：{branch}，源分支保持不变，不再计入冲突队列。")
        if reason:
            print(f"  理由：{reason}")
        print(f"RESOLVE_RESULT branch={branch} outcome=abandoned")
        return
    wt = Path(rec["worktree"])
    resolve_branch = rec["resolve_branch"]
    print(f"放弃任务 {rec['slug']}：删 {resolve_branch} 与 {wt}，源分支 {rec['branch']} 原样不动。")
    if not go:
        print("  加 --go 执行。")
        return
    if wt.exists():
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, text=True)
    subprocess.run(["git", "-C", str(REPO), "branch", "-D", resolve_branch],
                   capture_output=True, text=True)
    rec["status"] = "abandoned"
    rec["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if reason:
        rec["reason"] = reason
    _write_resolve_record(rec)
    print("已放弃。")


def _resolve_list() -> None:
    tasks = load_resolve().get("tasks", [])
    open_tasks = _open_resolve_tasks()
    done = [t for t in tasks if t.get("status") == "landed"]
    print("=== 冲突执行任务 ===")
    if not open_tasks:
        print("  没有进行中的任务。用 kgctl resolve --auto --go 把一条冲突分支拆成单个任务。")
    for t in open_tasks:
        uf = t.get("unresolved_files") or []
        print(f"  [{t.get('status')}] {t.get('slug')}  <- {t.get('branch')}")
        print(f"       worktree: {t.get('worktree')}")
        print(f"       待解 {len(uf)} 个：{', '.join(uf[:5])}{' ...' if len(uf) > 5 else ''}")
    if done:
        print(f"  （已落盘 {len(done)} 个：{', '.join(t.get('slug', '') for t in done[:6])}）")


def cmd_resolve(args) -> None:
    """把一条轻冲突分支拆成「单个可执行任务」：直接落盘，或留一个待解工作树给 agent。

    冲突队列真正的堵点不是「要不要合」，而是 merge-tree 报冲突之后没有下一步——要么
    人肉开几十个分支，要么全塞进一个超级收拢。本命令给每条冲突分支一个独立、可验收、
    可回退的执行单元：在 origin/main 上开一个 resolve worktree，把源分支合进来；能被
    确定性解掉的冲突（append-only 的 .jsonl 行级并集）就地解掉，剩下的原样保留 marker
    写成任务书交给 agent。解完用 `kgctl resolve --finish <slug>` 走 land 的同一道门禁。

    安全约定：
      * 只在独立 worktree（`<state>-resolve-<slug>`）里操作，main 与源分支都不动；
      * 只自动解 append-only 的 .jsonl（行级并集），其余冲突一律保留 marker，不猜；
      * 落盘仍走 `kgctl land` 的完整门禁；不过就留在工作树里，绝不半成品进 main。
    """
    if not args or args[0] in ("-h", "--help"):
        print("用法: kgctl resolve --auto [--max-files N] [--max-lines N] [--go]")
        print("      kgctl resolve <分支> [--go]")
        print("      kgctl resolve --list")
        print("      kgctl resolve --finish <slug|分支> [--go]")
        print("      kgctl resolve --abandon <slug|分支> [--reason 理由] [--go]")
        return
    if "--list" in args:
        return _resolve_list()
    if "--finish" in args:
        i = args.index("--finish")
        target = args[i + 1] if i + 1 < len(args) else ""
        if not target:
            sys.exit("用法: kgctl resolve --finish <slug|分支> [--go]")
        return _resolve_finish(target, "--go" in args)
    if "--abandon" in args:
        i = args.index("--abandon")
        target = args[i + 1] if i + 1 < len(args) else ""
        if not target:
            sys.exit("用法: kgctl resolve --abandon <slug|分支> [--reason 理由] [--go]")
        reason = ""
        if "--reason" in args:
            j = args.index("--reason")
            reason = args[j + 1] if j + 1 < len(args) else ""
        return _resolve_abandon(target, "--go" in args, reason)
    return _resolve_prepare(args)


def cmd_land(args):
    if not args:
        sys.exit("用法: kgctl land <分支> [--dry]")
    branch, dry = args[0], "--dry" in args
    if not branch.startswith(BRANCH_PREFIX):
        sys.exit(f"只允许 land {BRANCH_PREFIX}* 分支")

    ahead, behind = counts(branch)
    if ahead == 0:
        print(f"{branch} 已完全合并，无需 land（可 kgctl reap --go）")
        return
    conf = conflict_files(branch)
    if conf:
        print(f"× {branch} 与 {MAIN} 有 {len(conf)} 个冲突文件，拒绝自动 land：")
        for f in conf[:10]:
            print(f"    {f}")
        print("  这是需要归属裁决的信号（kgctl matrix 的 C 组）。")
        return
    markers = unresolved_marker_files(branch)
    if markers:
        print(f"× {branch} 的树里残留 {len(markers)} 个冲突标记文件，拒绝自动 land：")
        for f in markers[:10]:
            print(f"    {f}")
        print("  先在 convergence worktree 解干净并提交，再重新排队。")
        return

    print(f"→ {branch}: {ahead} 提交、落后 {behind}、无冲突")
    print("  步骤：主干工作树 merge → 验证门禁 → 写进展/验收台账 → push origin/main → 同步 main")
    if dry:
        print("  （--dry：到此为止）")
        return

    if not _acquire_lock("land"):
        sys.exit(f"已有 land 在进行（{LOCK}）")
    OPS.mkdir(parents=True, exist_ok=True)
    try:
        _align_main()
        pre = git("rev-parse", MAIN)
        wt = _trunk_worktree()
        reset_trunk_head(wt)
        _mirror_truth_into_trunk(wt)
        dirty = [line for line in trunk_status(wt) if not is_truth_path(line)]
        if dirty:
            print(f"× 主干工作树有非真相层改动，拒绝 land：{wt}")
            print("\n".join(dirty[:20])[:800])
            sys.exit(1)
        sync_trunk_truth(wt, "sync state before landing")
        m = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", branch,
                            "-m", f"train: merge {branch}"], capture_output=True, text=True)
        if m.returncode != 0:
            if not _resolve_truth_merge(wt):
                subprocess.run(["git", "-C", str(wt), "merge", "--abort"],
                               capture_output=True, text=True)
                print("× 合并失败，已丢弃：", m.stderr.strip()[:300])
                sys.exit(1)
            print("  合并的冲突全部属于真相层，已按台账规则自动解后继续")
        print("  merge ok（main 未动）")
        gate_ok, gate_results, gate_out = _run_verify_gates(wt, pre)
        print(f"  验证门禁 {'ok' if gate_ok else '× 失败 → 丢弃，main 不动'}")
        if not gate_ok:
            print(gate_out[-800:])
            print(f"  main 仍是 {pre[:10]}，源分支 {branch} 未受影响")
            sys.exit(1)
        merge_commit = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                                      capture_output=True, text=True).stdout.strip()
        manifest = _land_manifest(wt, branch, merge_commit, pre)
        _record_land_progress(wt, branch, merge_commit, gate_results, manifest, pre)
        pushed = _push_main_with_one_retry(wt)
        if pushed:
            print("  " + pushed.replace("\n", "\n  "))
        new = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        # 并发下不用 CAS 从旧 pre 推进（会撞 cannot lock ref）；push 已成功，以
        # origin/main 为准对齐本地引用，避免把已落盘误判成失败。
        aligned = _align_main_ref_to_origin(new)
        if not _origin_contains(new):
            raise RuntimeError(
                f"push 返回成功，但 origin/{MAIN} 不包含落盘提交 {new[:10]}；"
                "拒绝输出已落盘，请检查是否有并发强推")
        print(f"  main 推进 {pre[:8]} → {aligned[:8]}"
              f"（{branch} 已落盘并推送 origin，且已确认包含于 origin/{MAIN}）")
    finally:
        _release_lock()


def cmd_sync(args):
    branch = args[0] if args else None
    wts = worktree_map()
    print("前提：先让持有该 worktree 的会话停下；本命令不改运行中的会话，只打动作清单。")
    if not branch:
        stale = [b for b in codex_branches() if counts(b)[1] > 50]
        print(f"落后 main > 50 的分支共 {len(stale)} 个（前 12）：")
        for b in sorted(stale, key=lambda x: -counts(x)[1])[:12]:
            print(f"   behind={counts(b)[1]:4d}  {b:44s} wt={wts.get(b,'')}")
        print("\n用法：kgctl sync <分支>")
        return
    wt = wts.get(branch)
    if not wt:
        sys.exit(f"{branch} 没有 worktree")
    print(f"\n在 {wt} 里依次执行：")
    for cmd in ["git fetch origin",
                "git merge --no-edit origin/main   # 或 git rebase origin/main",
                ((CFG.get("build") or {}).get("cmd") or "npm run build"),
                f"cd {REPO} && python3 ops/kgctl.py land {branch}"]:
        print(f"  {cmd}")


# ---------------------------------------------------------------- 一屏 / 看板

def cmd_status(args):
    rows = scan()
    unrouted = total = 0
    if INBOX.exists():
        for line in INBOX.open():
            total += 1
            try:
                if not json.loads(line).get("routed"):
                    unrouted += 1
            except Exception:
                pass
    print("=== 自动挡一屏 ===")
    print(f"状态根         {REPO}")
    print(f"共享检出       {SHARED}（只读诊断，不当账本）")
    print(f"主干           {MAIN} @ {git('log','-1','--format=%h %s',MAIN)[:56]}")
    print(f"唯一入口       待路由 {unrouted} / 累计 {total} 条")
    print(f"codex 分支     {len(rows)}")
    print(f"  ├ 已合并可回收 {sum(1 for r in rows if r['kind']=='MERGED')}")
    print(f"  ├ 可入队落盘   {sum(1 for r in rows if r['kind']=='CLEAN')}")
    print(f"  ├ 有冲突需裁决 {sum(1 for r in rows if r['kind']=='CONFLICT')}")
    print(f"  └ 残留冲突标记 {sum(1 for r in rows if r['kind']=='UNRESOLVED')}")
    groups = conflict_groups(rows)
    print(f"冲突收拢族     {len(groups)} 个（可把冲突分支收成 convergence 任务）")
    conflicts = [r for r in rows if r["kind"] == "CONFLICT"]
    if conflicts:
        bmap = conflict_burden_map(rows)
        burden = sorted((bmap.get(r["branch"], r["nfiles"]), r["branch"]) for r in conflicts)
        light = [b for n, b in burden if n <= 8]
        print(f"冲突可解度     {len(light)}/{len(conflicts)} 支只需解 <=8 个冲突文件"
              f"（最轻 {burden[0][0]} 个：{burden[0][1]}）")
    open_tasks = _open_resolve_tasks()
    if open_tasks:
        print(f"冲突执行任务   {len(open_tasks)} 个待解（kgctl resolve --list）")
    wts = worktree_map()
    behind50 = [b for b in codex_branches() if counts(b)[1] > 50]
    print(f"worktree       {len(wts)} 个（其中落后 main >50 的分支 {len(behind50)} 条）")
    shared_dirty = [l for l in subprocess.run(
        ["git", "-C", str(SHARED), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True).stdout.split("\n") if l.strip()]
    print(f"共享检出脏度   {len(shared_dirty)} 项（已跟踪）")
    pending_truth = int(git("rev-list", "--count", f"origin/{MAIN}..HEAD") or 0)
    print(f"真相层待推送   {pending_truth} 条提交")
    print(f"驾驶舱         {DASHBOARD}（kgctl dash --open）")


def cmd_board(args):
    rows = scan()
    lines = ["# 控制台（自动生成，勿手改）", "",
             f"生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"主干 `{MAIN}` @ `{git('log','-1','--format=%h %s',MAIN)}`", "",
             "| 状态 | 分支 | 领先 | 落后 | 冲突文件 | 标题 |", "|---|---|---:|---:|---:|---|"]
    for r in sorted(rows, key=lambda x: (x["kind"], -x["ahead"])):
        lines.append(f"| {r['kind']} | `{r['branch']}` | {r['ahead']} | {r['behind']} | "
                     f"{len(r['conflicts'])} | {r['subject']} |")
    BOARD.write_text("\n".join(lines) + "\n")
    print(f"已写出 {BOARD.relative_to(REPO)}（{len(rows)} 行）")


def _truthctl_path() -> Path:
    """找到可执行的 truthctl.py。

    自包含安装下，运行时代码只存在于插件目录，目标项目里没有 `ops/truthctl.py`；
    所以先看本次调用所在的 ops/（即插件自己的运行时），再回退到主干/共享检出。
    """
    roots = [RUNTIME_OPS, CODE_ROOT, central_config.trunk_dir(REPO, CFG), REPO]
    for root in roots:
        candidate = root / "ops" / "truthctl.py"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("找不到 ops/truthctl.py；请先同步 main 或设置 KG_TRUNK")


def _truth(sub, *extra):
    """Delegate a truth-layer subcommand through the same entrypoint.

    KG_REPO 必须显式传给子进程：truthctl 若自己再解析一次状态根，就可能解析到另一个
    worktree，「kgctl 说 10 条待路由、tick 说没有待路由」的分叉又会回来。
    """
    path = _truthctl_path()
    env = os.environ.copy()
    env["KG_REPO"] = str(REPO)
    rc = subprocess.run([sys.executable, str(path), sub, *extra],
                        cwd=str(path.parent.parent), env=env)
    return rc.returncode


def cmd_next(args):
    _truth("next")


def _restore_truth_keep_files_from_origin() -> None:
    """tick/intake 前恢复必须保留的真相证据，并拒绝远端伪快照。

    远端确实不存在时不伪造文件；远端如果本身已被污染成伪快照，也不能把它写回
    本地，必须停止并保留真实字节。
    """
    fetched = subprocess.run(["git", "-C", str(REPO), "fetch", "origin", MAIN],
                             capture_output=True, text=True)
    if fetched.returncode != 0:
        print(f"  保护证据恢复失败：fetch origin/{MAIN} 失败")
        return
    rel = "ops/truth/benchmark-contract-report.json"
    remote_sha = _remote_benchmark_sha()
    if remote_sha and remote_sha != TRUTH_BENCHMARK_REAL_HASH:
        print(
            f"  × 远端保护证据本身已被污染：{rel} "
            f"remote={remote_sha[:10]} != {TRUTH_BENCHMARK_REAL_HASH[:10]}"
        )
        if _ensure_truth_baselines(REPO):
            print("  已保留本地真实 36 题基线并停止 tick/intake/truth-replay")
        sys.exit("× 稳定性基准远端污染，拒绝 tick/intake/truth-replay，不传播伪快照")
    restored = []
    for rel in TRUTH_KEEP_IF_BASELINE_MISSING:
        blob = subprocess.run(["git", "-C", str(REPO), "show", f"origin/{MAIN}:{rel}"],
                              capture_output=True, text=True)
        if blob.returncode != 0 or not blob.stdout:
            continue
        target = REPO / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(blob.stdout, encoding="utf-8")
        restored.append(rel)
    if restored:
        print(f"  已从 origin/{MAIN} 恢复 {len(restored)} 个保护证据文件")
    if not _ensure_truth_baselines(REPO):
        sys.exit("× 稳定性基准保护失败，停止 tick/intake/truth-replay，不推送可能被污染的真相层")


def _stage(n: int, label: str) -> None:
    """打印阶段号 + 开始时间。

    tick 被上层超时杀掉时（实测 2026-10-15 22:19 那一轮挂了 1800s 被杀），日志里最后
    一个时间戳就是卡住的位置；旧实现八个阶段共用固定文案，超时后完全无法定位。
    """
    print("\n[%d/8] %s  (%s)" % (n, label, time.strftime("%H:%M:%S")), flush=True)


def cmd_tick(args):
    """自动挡主循环：输入归一 -> 真相刷新 -> 冲突收拢 -> 看板/上下文 -> 落盘状态。"""
    _restore_truth_keep_files_from_origin()
    _stage(1, "route inbox")
    _truth("route", "--go")
    _stage(2, "refresh project status")
    _truth("snapshot")
    _stage(3, "synthesize product mainline")
    _truth("synthesize")

    if os.environ.get("KG_TICK_LIGHT") == "1":
        rows = []
        print("  (light tick: 跳过全量分支 merge-tree 扫描)")
    else:
        rows = scan()
        print("  merged=%d clean=%d conflict=%d unresolved=%d" % (
            sum(1 for r in rows if r["kind"] == "MERGED"),
            sum(1 for r in rows if r["kind"] == "CLEAN"),
            sum(1 for r in rows if r["kind"] == "CONFLICT"),
            sum(1 for r in rows if r["kind"] == "UNRESOLVED")))

    _stage(4, "consolidate conflict families")
    if "--no-converge" not in args:
        cmd_converge(["--go", "--limit", "8"])
    else:
        print("  （--no-converge：跳过）")

    _stage(5, "refresh board + dashboard")
    if os.environ.get("KG_TICK_LIGHT") == "1":
        print("  (light tick: 跳过 CONTROL_BOARD/CONTROL_PLANE 渲染)")
    else:
        cmd_board([])
        cmd_dash([])

    _stage(6, "write context packs for next tasks")
    if os.environ.get("KG_TICK_LIGHT") != "1":
        rows = scan()
    branches = [r["branch"] for r in rows if r["kind"] == "CLEAN"][:20]
    branches += [g.get("branch") for g in load_convergence().get("groups", [])[:20]
                 if g.get("branch")]
    if branches:
        cmd_context(branches)
    else:
        print("  没有待执行分支，跳过")

    _stage(7, "commit + push truth state")
    sync_trunk_truth(REPO, "sync from tick")
    push_truth_state()
    _stage(8, "what to do next")
    _truth("next")


def cmd_doctor(args):
    """体检：把「会悄悄变坏」的东西查出来，而不是等人肉感觉到不对劲。

    三项，全部只读、fail-open（任何一项取不到就打印「取不到」，绝不让体检本身报错）：

      ① automation 漂移 —— 心跳类 config 里 prompt 写的频次与 rrule 是否一致。
         事故：prompt 写「每 30 分钟一拍」，rrule 写 INTERVAL=10，模型拿这个矛盾给自己开脱。
      ② 真相层矛盾   —— 已 verified 的验收项，其关键词是否还出现在方向文件的
         「未跑/未验证/待办」表述里。事故：A-0085 已 exit 0，mainline.json 仍写「网页 live 未跑」。
      ③ 长驻窗口预算 —— 每个挂 automation 的线程的 rollout 体量 / 累计 token /
         最近一轮 input 是否已进入「越长越懒」区间。
    """
    problems: list[str] = []
    INFOS: list[str] = []

    # ① automation 漂移
    auto_root = Path.home() / ".codex" / "automations"
    if not auto_root.exists():
        INFOS.append("① automation：取不到 ~/.codex/automations（跳过）")
    else:
        checked = 0
        for cfg in sorted(auto_root.glob("*/automation.toml")):
            try:
                text = cfg.read_text()
            except Exception:
                continue
            checked += 1
            rrule = ""
            m = re.search(r'^rrule\s*=\s*"([^"]+)"', text, re.M)
            if m:
                rrule = m.group(1)
            mins = None
            mm = re.search(r"INTERVAL=(\d+)", rrule)
            if mm and "MINUTELY" in rrule:
                mins = int(mm.group(1))
            elif "DAILY" in rrule:
                mins = -1  # 日级，不做分钟比对
            # prompt 里声明的「每 N 分钟一拍」
            pm = re.search(r"每\s*(\d+)\s*分钟", text)
            if mins and mins > 0 and pm and int(pm.group(1)) != mins:
                problems.append(
                    f"① {cfg.parent.name}：prompt 写「每 {pm.group(1)} 分钟」，"
                    f"rrule 是 {mins} 分钟 —— 文字与字段矛盾，模型会拿它给自己开脱")
        INFOS.append(f"① automation：检查 {checked} 个心跳配置，"
                     f"漂移 {sum(1 for p in problems if p.startswith('①'))} 处")

    # ② 真相层矛盾：verified 的验收项 vs 方向文件里的否定表述
    acc_path = REPO / "ops" / "truth" / "acceptance.jsonl"
    ml_path = REPO / "ops" / "truth" / "mainline.json"
    try:
        acc = list(latest_by_id(read_jsonl(acc_path)).values())
        ml_text = ml_path.read_text()
    except Exception:
        acc, ml_text = [], ""
    if not acc or not ml_text:
        INFOS.append("② 真相层：取不到验收台账或方向文件（跳过）")
    else:
        NEG = ("未跑", "未验证", "未实测", "unverified", "待跑", "尚未")
        conflicts = 0
        verified = [r for r in acc if r.get("status") == "verified"]
        for rec in verified:
            lane = str(rec.get("lane") or "")
            slug = re.sub(r"^codex/", "", lane).lower()
            key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", slug)
            if len(key) < 6:
                continue
            # 该验收项的关键词是否出现在方向文件的否定句里
            for sent in re.split(r"[。；\n]", ml_text):
                if any(n in sent for n in NEG) and key[:12] in re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", sent.lower()):
                    problems.append(
                        f"② {rec.get('id')} 已 verified（lane={lane}），但方向文件里仍有否定表述："
                        f"「{sent.strip()[:70]}」")
                    conflicts += 1
                    break
        INFOS.append(f"② 真相层：{len(verified)} 条已机器验收，疑似矛盾 {conflicts} 条")

    # ③ 长驻窗口预算
    db = Path.home() / ".codex" / "state_5.sqlite"
    if not auto_root.exists() or not db.exists():
        INFOS.append("③ 窗口预算：取不到 automations 或 codex 状态库（跳过）")
    else:
        targets = {}
        for cfg in sorted(auto_root.glob("*/automation.toml")):
            try:
                t = cfg.read_text()
            except Exception:
                continue
            tm = re.search(r'^target_thread_id\s*=\s*"([^"]+)"', t, re.M)
            if tm:
                targets[cfg.parent.name] = tm.group(1)
        probed = 0
        for name, tid in targets.items():
            # 关键：这里**不能**用 except: continue 静默跳过。
            # 探针自己坏掉时若静默变成「没发现问题」，就是全盘假阴性
            # （本节第一版正是如此：sqlite3 未导入 → NameError 被吞 → 体检报「一切正常」）。
            try:
                c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                row = c.execute("select rollout_path, tokens_used from threads where id=?",
                                (tid,)).fetchone()
                c.close()
            except Exception as exc:
                problems.append(f"③ {name}：窗口预算探针失败（{type(exc).__name__}: {exc}）"
                                "——这说明体检工具自己坏了，不是窗口没问题")
                continue
            probed += 1
            if not row:
                INFOS.append(f"③ {name}：线程 {tid[:8]} 在状态库里找不到（可能已归档）")
                continue
            rp, tk = row
            mb = None
            try:
                mb = round(os.path.getsize(rp) / 1e6, 1)
            except Exception:
                pass
            badge = ""
            if mb and mb > 80:
                badge = " ⚠️ 体量已大"
            if (tk or 0) > 150_000_000:
                badge += " ⚠️ 累计 token 已高"
            INFOS.append(f"③ {name}：rollout {mb}MB，累计 {round((tk or 0)/1e6,1)}M token{badge}")
            if badge:
                problems.append(f"③ {name}：窗口 {tid[:8]} 已进入退化区间（{mb}MB / "
                                f"{round((tk or 0)/1e6,1)}M token）→ 建议换新窗口并改 target_thread_id")
        if probed == 0 and targets:
            problems.append("③ 窗口预算：一个线程都没探测成功——探针失效，别把「无输出」当正常")

    print("=== 自动挡体检 ===")
    for line in INFOS:
        print("  " + line)
    print()
    skipped = sum(1 for line in INFOS if "跳过" in line)
    if not INFOS:
        print("× 三项全部无法执行——不是「没问题」，是体检没跑起来。请检查路径与依赖。")
        return 1
    if skipped == len(INFOS):
        print(f"× {skipped} 项全部跳过（路径或依赖取不到）——这不是「没问题」，"
              "是体检没跑起来。别把它当成绿灯。")
        return 1
    if problems:
        print(f"发现 {len(problems)} 个需要处理的项：")
        for p in problems:
            print("  × " + p)
        print("\n（体检只读。轮换用 ops/cockpit_rotate.py；委派用 ops/DELEGATION_TEMPLATE.md）")
    else:
        note = f"（其中 {skipped} 项因取不到而跳过，结论不完整）" if skipped else ""
        print("没有发现问题：automation 无漂移、真相层无矛盾、窗口预算正常。" + note)
    return 1 if problems else 0


CMDS = {"inbox": cmd_inbox, "intake": cmd_intake, "route": cmd_route, "suggest": cmd_suggest,
        "matrix": cmd_matrix, "queue": cmd_queue, "gate": cmd_gate, "converge": cmd_converge,
        "replan": cmd_replan,
        "context": cmd_context, "dash": cmd_dash,
        "clash": cmd_clash, "reap": cmd_reap, "land": cmd_land, "supersede": cmd_supersede,
        "port": cmd_port, "resolve": cmd_resolve, "sync": cmd_sync,
        "doctor": cmd_doctor,
        "status": cmd_status, "board": cmd_board, "next": cmd_next, "tick": cmd_tick}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__)
        print("命令：" + " | ".join(CMDS))
        sys.exit(1)
    # doctor 用退出码报告「有待处理项」（便于 cron / 看门狗据此报警）：
    # 只有显式返回 int 才作为进程退出码；返回 None 一律当 0。
    # 注意：命令内部的用法提示（sys.exit("用法: ...")）本来就会以非 0 退出，
    # 这是原有行为，不在这里改写。
    _rc = CMDS[sys.argv[1]](sys.argv[2:])
    sys.exit(_rc if isinstance(_rc, int) else 0)
