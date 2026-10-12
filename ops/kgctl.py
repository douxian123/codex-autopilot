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

安全约定（硬编码，不可关）
  * `reap` 默认只预演，必须显式 `--go`。
  * `land` 任何一步失败都丢弃，main 与源分支都不动；从不 force push、从不改写源分支历史。
  * `matrix/queue/status/sync/clash/inbox 读` 全部只读。
"""

import hashlib
import json
import os
import re
import shutil
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
BOARD = REPO / "docs" / "CONTROL_BOARD.md"
DASHBOARD = REPO / "docs" / "CONTROL_PLANE.html"
CONTEXT_DIR = REPO / "ops" / "context"
CONVERGENCE = OPS / "convergence.json"


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


def branch_meta(branch: str) -> dict:
    """分支的提交者/时间/标题。给收拢计划做归属提示，不做自动裁决。"""
    return {
        "branch": branch,
        "subject": git("log", "-1", "--format=%s", branch)[:90],
        "author": git("log", "-1", "--format=%an <%ae>", branch),
        "ts": git("log", "-1", "--format=%cI", branch),
    }


def conflict_groups(rows=None) -> list:
    """把冲突分支按「抢同一批文件」收成少数几个冲突族。

    一个文件被多条分支同时改，不代表每条都要单独问人；先把连通的分支收成
    一个 convergence 任务，用户/agent 面对的是「一族冲突」，不是几十个分支。
    """
    rows = rows if rows is not None else scan()
    conflicts = [r for r in rows if r["kind"] == "CONFLICT"]
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


def cmd_supersede(args):
    """把「独有内容已全部在 main」的分支自动归档，不再当作冲突推给用户。

    默认只预演；`kgctl supersede --go` 才写进展台账并推送 main。
    只处理 ahead>0 的分支（ahead==0 的走 `kgctl reap`）。
    """
    go = "--go" in args
    rows = [r for r in scan() if r["kind"] != "MERGED"]
    print("扫描「分支独有内容是否已全部在 main」……（逐文件比对，几十秒到几分钟）\n")
    done = already_superseded()
    if done:
        print(f"（真相台账里已有 {len(done)} 条 superseded 记录，将跳过）\n")
    cands = []
    for r in sorted(rows, key=lambda x: (x["nfiles"], x["ahead"])):
        if r["branch"] in done:
            continue
        ok, notes = branch_net_zero(r["branch"])
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
    if LOCK.exists():
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
        subprocess.run(["git", "-C", str(REPO), "update-ref", f"refs/heads/{MAIN}", new], check=True)
        subprocess.run(["git", "-C", str(REPO), "fetch", "origin", MAIN],
                       capture_output=True, text=True)
        print(f'  已归档 {len(cands)} 个重复分支并推送 origin/main @ {new[:8]}')
    finally:
        if LOCK.exists():
            LOCK.unlink()


# ---------------------------------------------------------------- 输入：唯一入口

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

def scan():
    rows = []
    for b in codex_branches():
        ahead, behind = counts(b)
        ts = int(git("log", "-1", "--format=%ct", b) or 0)
        files = changed_files(b)
        if ahead == 0:
            kind, conf = "MERGED", []
        else:
            conf = conflict_files(b)
            kind = "CONFLICT" if conf else "CLEAN"
        rows.append(dict(branch=b, ahead=ahead, behind=behind,
                         age=round((time.time() - ts) / 86400, 1) if ts else -1,
                         subject=git("log", "-1", "--format=%s", b)[:70],
                         nfiles=len(files), files=files, kind=kind, conflicts=conf))
    return rows


def cmd_matrix(args):
    rows = scan()
    json.dump(rows, (OPS / "matrix.json").open("w"), ensure_ascii=False, indent=1)
    g = {"MERGED": [], "CLEAN": [], "CONFLICT": []}
    for r in rows:
        g[r["kind"]].append(r)
    print(f"仓库 {REPO}")
    print(f"主干 {MAIN} @ {git('log','-1','--format=%h %s',MAIN)[:60]}\n")
    print(f"codex 分支 {len(rows)} 个 ->  已合并 {len(g['MERGED'])} | "
          f"能干净合 {len(g['CLEAN'])} | 有冲突 {len(g['CONFLICT'])}\n")

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
    print("明细：ops/tasks/matrix.json")


def cmd_queue(args):
    rows = scan()
    clean = sorted([r for r in rows if r["kind"] == "CLEAN"],
                   key=lambda x: (x["ahead"], -x["behind"]))
    print("合并队列（一次一个，合完立刻重算）\n")
    for i, r in enumerate(clean, 1):
        print(f"  {i:2d}. kgctl land {r['branch']}   "
              f"（{r['ahead']} 提交 / {r['nfiles']} 文件 / 落后 {r['behind']}）")
    if not clean:
        print("  队列为空")
    print(f"\n其余 {len(rows)-len(clean)} 个："
          f"{sum(1 for r in rows if r['kind']=='MERGED')} 个已合并待回收、"
          f"{sum(1 for r in rows if r['kind']=='CONFLICT')} 个有冲突需裁决。")


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
    subprocess.run(["git", "-C", str(wt), "add", "--", *unresolved], check=True)
    commit = subprocess.run(
        ["git", "-C", str(wt), "commit", "-m",
         f"wip(converge): merge {branch} into {group_id} with conflict markers"],
        capture_output=True, text=True)
    if commit.returncode != 0:
        raise RuntimeError(f"提交冲突合并失败：{(commit.stdout + commit.stderr).strip()[:500]}")
    return {"branch": branch, "files": unresolved, "auto_resolved_jsonl": auto}


def _convergence_branch(group: dict) -> str:
    return f"{BRANCH_PREFIX}converge-{group['id']}"


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
    if not groups:
        print("没有冲突分支，无需收拢。")
        return
    records = {g.get("id"): g for g in load_convergence().get("groups", [])}
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
    if LOCK.exists():
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
            if existing.get("branch") == branch and git("rev-parse", "--verify", branch):
                print(f"  {branch} 已存在，跳过")
                continue
            wt = REPO.parent / f"{REPO.name}-converge-{g['id']}"
            if wt.exists():
                print(f"  × {wt} 已存在但不是已登记分支；跳过 {branch}")
                continue
            add = subprocess.run(
                ["git", "-C", str(REPO), "worktree", "add", "-b", branch, str(wt), base_ref],
                capture_output=True, text=True)
            if add.returncode != 0:
                print(f"  × 建 worktree 失败 {branch}: {(add.stdout + add.stderr).strip()[:300]}")
                continue
            _link_side_dirs(wt)
            merge_notes = []
            for src in g["branches"]:
                merged = subprocess.run(
                    ["git", "-C", str(wt), "merge", "--no-ff", src,
                     "-m", f"converge: merge {src} into {g['id']}"],
                    capture_output=True, text=True)
                if merged.returncode == 0:
                    merge_notes.append({"branch": src, "files": [],
                                        "auto_resolved_jsonl": []})
                    continue
                try:
                    note = _commit_conflicted_merge(wt, src, g["id"])
                except Exception as exc:
                    print(f"  × {branch} 合并 {src} 失败：{exc}")
                    merge_notes.append({"branch": src, "files": ["(commit failed)"],
                                        "auto_resolved_jsonl": []})
                    continue
                merge_notes.append(note)
            grep = subprocess.run(
                ["git", "-C", str(wt), "grep", "-l", "-E", "^(<<<<<<<|>>>>>>>)"],
                capture_output=True, text=True)
            unresolved_files = [x for x in grep.stdout.splitlines() if x.strip()]
            head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            record = {
                "id": g["id"], "branch": branch, "worktree": str(wt),
                "base": base_sha, "head": head, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "needs_resolution" if unresolved_files else "ready",
                "branches": g["branches"], "files": g["files"],
                "unresolved_files": unresolved_files, "merges": merge_notes,
            }
            _write_convergence_record(record)
            made.append(record)
            print(f"  已收拢 {len(g['branches'])} 分支 -> {branch}"
                  f"（待解冲突文件 {len(unresolved_files)}，worktree={wt}）")
        if made:
            print(f"\n已生成 {len(made)} 个 convergence 分支。下一步：对每个 needs_resolution "
                  "分支跑 `kgctl context <分支>`，再让一个 agent 在该 convergence worktree 里解冲突。")
    finally:
        if LOCK.exists():
            LOCK.unlink()


def _context_payload(branch: str) -> dict:
    ahead, behind = counts(branch)
    files = changed_files(branch)
    conf = conflict_files(branch)
    rows = read_jsonl(REPO / "ops" / "truth" / "ledger.jsonl")
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
        "mergeable": not conf,
        "conflict_files": conf,
        "changed_files": files,
        "mainline": json.loads((REPO / "ops" / "truth" / "mainline.json").read_text(encoding="utf-8"))
        if (REPO / "ops" / "truth" / "mainline.json").exists() else {},
        "active_decisions": [r for r in read_jsonl(REPO / "ops" / "truth" / "decisions.jsonl")
                             if r.get("status") == "active"],
        "open_issues_overlapping": overlap,
        "acceptance_todo": [r for r in read_jsonl(REPO / "ops" / "truth" / "acceptance.jsonl")
                            if r.get("status") in ("todo", "wip", "blocked")],
        "recent_progress": read_jsonl(REPO / "ops" / "truth" / "progress.jsonl")[-8:],
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
        if payload["convergence"]:
            lines += ["", "## 收拢任务"]
            lines += [f"- convergence: `{payload['convergence'].get('branch')}`",
                      f"- 待解冲突文件：{len(payload['convergence'].get('unresolved_files') or [])}"]
        write_text(CONTEXT_DIR / f"{name}.md", "\n".join(lines) + "\n")
        print(f"已写 Context Pack：ops/context/{name}.json")
    if branches:
        print(f"共 {len(branches)} 份；Markdown 在同目录。")


def cmd_dash(args):
    """生成一个可直接打开的驾驶舱 HTML；信息源仍是同一份台账和分支扫描。"""
    rows = scan()
    groups = conflict_groups(rows)
    led = read_jsonl(REPO / "ops" / "truth" / "ledger.jsonl")
    open_led = [r for r in led if r.get("status") in ("open", "doing", "todo")]
    open_led.sort(key=lambda r: ({"bug": 0, "arch": 1, "ux": 2, "visual": 3}.get(r.get("kind"), 9),
                                 r.get("ts", "")))
    prog = read_jsonl(REPO / "ops" / "truth" / "progress.jsonl")[-8:]
    inbox = read_jsonl(INBOX)
    pending = [x for x in inbox if not x.get("routed")]
    import html as _html

    def esc(x):
        return _html.escape(str(x or ""), quote=True)

    clean = [r for r in rows if r["kind"] == "CLEAN"]
    conflict = [r for r in rows if r["kind"] == "CONFLICT"]
    merged = [r for r in rows if r["kind"] == "MERGED"]
    html_rows = []
    for r in open_led[:20]:
        html_rows.append(
            f"<tr><td>{esc(r.get('id'))}</td><td>{esc(r.get('kind'))}</td>"
            f"<td>{esc(r.get('lane'))}</td><td>{esc(r.get('title'))}</td></tr>")
    group_rows = []
    for g in groups:
        group_rows.append(
            f"<tr><td><code>{esc(_convergence_branch(g))}</code></td>"
            f"<td>{len(g['branches'])}</td><td>{len(g['files'])}</td>"
            f"<td>{esc(', '.join(g['files'][:3]))}</td></tr>")
    branch_rows = []
    for r in sorted(clean + conflict, key=lambda x: (x["kind"], -x["ahead"]))[:30]:
        branch_rows.append(
            f"<tr><td>{esc(r['kind'])}</td><td><code>{esc(r['branch'])}</code></td>"
            f"<td>{r['ahead']}</td><td>{r['behind']}</td>"
            f"<td>{len(r['conflicts'])}</td><td>{esc(r['subject'])}</td></tr>")
    progress_rows = [
        f"<li><code>{esc(r.get('id'))}</code> {esc(r.get('summary') or r.get('title'))}</li>"
        for r in reversed(prog)]
    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Autopilot 驾驶舱</title>
<style>
:root{{color-scheme:light;--ink:#18212f;--muted:#667085;--line:#d7dce3;--bg:#f7f8fa;--accent:#0f766e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:24px}}h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:16px;margin:24px 0 8px}}
.meta{{color:var(--muted)}}table{{width:100%;border-collapse:collapse;background:white;border:1px solid var(--line)}}th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}}th{{background:#eef1f4;font-size:12px;color:#475467}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}}ul{{background:white;border:1px solid var(--line);margin:0;padding:12px 12px 12px 30px}}
.stats{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}}.stat{{background:white;border:1px solid var(--line);padding:12px}}.num{{font-size:22px;font-weight:650;color:var(--accent)}}
@media(max-width:760px){{.stats{{grid-template-columns:repeat(2,minmax(0,1fr))}}main{{padding:14px}}table{{display:block;overflow:auto}}}}
</style></head><body><main>
<h1>Codex Autopilot 驾驶舱</h1>
<div class="meta">生成 {esc(time.strftime('%Y-%m-%d %H:%M:%S'))} · 主干 <code>{esc(MAIN)}</code> @ <code>{esc(git('log','-1','--format=%h %s',MAIN))}</code></div>
<div class="stats">
<div class="stat"><div class="num">{len(pending)}</div><div>待路由输入</div></div>
<div class="stat"><div class="num">{len(clean)}</div><div>可落盘</div></div>
<div class="stat"><div class="num">{len(conflict)}</div><div>冲突分支</div></div>
<div class="stat"><div class="num">{len(groups)}</div><div>可收拢冲突族</div></div>
<div class="stat"><div class="num">{len(open_led)}</div><div>未决问题</div></div>
</div>
<h2>现在最该做什么</h2>
<table><thead><tr><th>ID</th><th>类型</th><th>泳道</th><th>问题</th></tr></thead><tbody>
{''.join(html_rows) or '<tr><td colspan="4">没有未决问题</td></tr>'}
</tbody></table>
<h2>冲突收拢</h2>
<table><thead><tr><th>Convergence 分支</th><th>源分支</th><th>冲突文件</th><th>示例</th></tr></thead><tbody>
{''.join(group_rows) or '<tr><td colspan="4">没有需要收拢的冲突</td></tr>'}
</tbody></table>
<h2>分支队列</h2>
<table><thead><tr><th>状态</th><th>分支</th><th>领先</th><th>落后</th><th>冲突</th><th>标题</th></tr></thead><tbody>
{''.join(branch_rows) or '<tr><td colspan="6">没有待处理分支</td></tr>'}
</tbody></table>
<h2>最近进展</h2><ul>{''.join(progress_rows) or '<li>暂无进展记录</li>'}</ul>
</main></body></html>"""
    write_text(DASHBOARD, doc)
    print(f"已写出 {DASHBOARD.relative_to(REPO)}（待路由 {len(pending)}，可落盘 {len(clean)}，"
          f"冲突 {len(conflict)}，收拢族 {len(groups)}，已合并 {len(merged)}）")
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


def _align_main():
    """落盘前先把本地 main 引用对齐 origin/main，拒绝在分叉引用上继续。"""
    fetched = subprocess.run(["git", "-C", str(REPO), "fetch", "origin", MAIN],
                             capture_output=True, text=True)
    if fetched.returncode != 0:
        raise RuntimeError(f"fetch origin/{MAIN} 失败: {fetched.stderr.strip()}")
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


def _record_land_progress(wt: Path, branch: str, commit: str) -> None:
    """把真实落盘事件写进进展台账，并作为同一批 push 的一部分提交。"""
    env = os.environ.copy()
    env.update({
        "KG_REPO": str(wt),
        "KG_TRUTH_LANE": branch,
        "KG_TRUTH_COMMIT": commit,
        "KG_TRUTH_LANDED": "1",
        "KG_TRUTH_STATUS": "done",
        "KG_TRUTH_SUMMARY": f"{branch} 已通过构建门禁，并落盘 {MAIN} @ {commit[:10]}",
        "KG_TRUTH_SOURCE": "kgctl land",
    })
    added = subprocess.run(
        [sys.executable, str(RUNTIME_OPS / "truthctl.py"), "add", "progress",
         f"落盘 {branch}"],
        cwd=str(wt), env=env, capture_output=True, text=True)
    if added.returncode != 0:
        raise RuntimeError(f"写进展台账失败: {(added.stdout + added.stderr).strip()[:500]}")
    staged = subprocess.run(
        ["git", "-C", str(wt), "add", "ops/truth/progress.jsonl"],
        capture_output=True, text=True)
    if staged.returncode != 0:
        raise RuntimeError(f"暂存进展台账失败: {staged.stderr.strip()[:500]}")
    committed = subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", f"chore(truth): record landing {branch}"],
        capture_output=True, text=True)
    if committed.returncode != 0:
        raise RuntimeError(f"提交进展台账失败: {committed.stderr.strip()[:500]}")
    print(f"  进展台账 {added.stdout.strip()}")


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


def _build_frontend(wt: Path) -> tuple:
    """落盘前的构建门禁。命令与工作目录由 ops/central.config.json 的 build 决定。"""
    build = CFG.get("build") or {}
    cmd = (build.get("cmd") or "").strip()
    if not cmd:
        return True, "(未配置构建门禁，跳过)"
    cwd = wt / (build.get("cwd") or ".")
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr)


def _push_main_with_one_retry(wt: Path) -> str:
    """推送 trunk HEAD 到 main；若远端前进则同步一次、重验 build 后重试一次。"""
    last = ""
    for attempt in range(2):
        pushed = subprocess.run(["git", "-C", str(wt), "push", "origin", "HEAD:main"],
                                capture_output=True, text=True)
        if pushed.returncode == 0:
            return (pushed.stdout + pushed.stderr).strip()
        last = (pushed.stdout + pushed.stderr).strip()
        if attempt:
            break
        print("× push 被拒，远端可能前进；自动同步 origin/main 后重试一次")
        fetched = subprocess.run(["git", "-C", str(wt), "fetch", "origin", MAIN],
                                 capture_output=True, text=True)
        if fetched.returncode != 0:
            raise RuntimeError(f"push 后 fetch origin/{MAIN} 失败: {fetched.stderr.strip()[:500]}")
        synced = subprocess.run(
            ["git", "-C", str(wt), "merge", "--no-ff", f"origin/{MAIN}",
             "-m", "train: sync origin/main before landing"],
            capture_output=True, text=True)
        if synced.returncode != 0:
            subprocess.run(["git", "-C", str(wt), "merge", "--abort"],
                           capture_output=True, text=True)
            raise RuntimeError(
                f"远端 main 前进且自动同步出现冲突，未推送：{(synced.stdout + synced.stderr).strip()[:500]}")
        ok, out = _build_frontend(wt)
        if not ok:
            raise RuntimeError(f"同步远端 main 后 build 失败，未推送：{out[-800:]}")
    raise RuntimeError(f"push origin HEAD:main 失败：{last[:800]}")


# 真相层与生成视图：它们本来就该落在主干工作树上，所以不算「污染」。land /
# supersede 先提交它们再落盘，而不是因为「主干有未提交改动」直接拒绝执行——
# 后者曾经让每一次 `kgctl tick` 都顺手把 land 锁死。
TRUTH_PATHS = ("ops/truth", "ops/tasks", "ops/context", "docs/PROJECT_STATUS.md",
               "docs/CONTROL_BOARD.md", "docs/CONTROL_PLANE.html")
TRUTH_COMMIT_RE = re.compile(r"^(chore\(truth\)|train: (sync|merge))")


def trunk_status(wt: Path):
    # 只看已跟踪改动：未跟踪文件（如 ops/__pycache__/）不影响合并，若真要覆盖它们
    # `git merge` 自己会失败并被 land 安全丢弃，不该在这里当成「污染」挡住落盘。
    out = subprocess.run(["git", "-C", str(wt), "status", "--porcelain",
                          "--untracked-files=no"],
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def is_truth_path(line: str) -> bool:
    """judge whether a `git status --porcelain` line belongs to the truth layer."""
    body = line[3:].strip()
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
    if trunk_head_is_truth_only(wt):
        merged = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", f"origin/{MAIN}",
                                 "-m", "train: sync origin/main over truth state"],
                                capture_output=True, text=True)
        if merged.returncode != 0:
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
        if rel.endswith(".jsonl"):
            write_text(wt / rel, _union_jsonl_text(ours, theirs))
        elif rel.endswith(".json"):
            try:
                merged = _merge_json_value(json.loads(theirs), json.loads(ours))
                write_json(wt / rel, merged)
            except Exception:
                write_text(wt / rel, theirs)
        else:
            write_text(wt / rel, theirs or ours)
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
    merged = subprocess.run(
        ["git", "-C", str(REPO), "merge", "--no-ff", f"origin/{MAIN}",
         "-m", "train: merge origin/main into truth state"],
        capture_output=True, text=True)
    if merged.returncode == 0:
        return True
    if _resolve_truth_merge(REPO):
        return True
    return False


def push_truth_state() -> None:
    """把主干工作树上「只有真相层提交」的提交推回 origin/main。

    远端前进不是终止条件：真相层是追加式 JSONL，先合并远端再推送；只有非真相文件冲突
    才放弃。这样「本地 inbox / 台账永远推不上 main」不会再成为暗坑。
    """
    fetched = subprocess.run(["git", "-C", str(REPO), "fetch", "origin", MAIN],
                             capture_output=True, text=True)
    if fetched.returncode != 0:
        print(f"  fetch origin/{MAIN} 失败：{fetched.stderr.strip()[:200]}")
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
    pushed = subprocess.run(["git", "-C", str(REPO), "push", "origin", f"HEAD:{MAIN}"],
                            capture_output=True, text=True)
    if pushed.returncode != 0:
        print(f"  真相层推送失败，{ahead} 条提交留在本地：{(pushed.stdout + pushed.stderr).strip()[:200]}")
        return
    subprocess.run(["git", "-C", str(REPO), "update-ref", f"refs/heads/{MAIN}", "HEAD"],
                   check=False)
    print(f"  真相层 {ahead} 条提交已推送 origin/{MAIN}")


def cmd_land(args):
    if not args:
        sys.exit("用法: kgctl land <分支> [--dry]")
    branch, dry = args[0], "--dry" in args
    if not branch.startswith(BRANCH_PREFIX):
        sys.exit(f"只允许 land {BRANCH_PREFIX}* 分支")
    if LOCK.exists():
        sys.exit(f"已有 land 在进行（{LOCK}）")

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

    print(f"→ {branch}: {ahead} 提交、落后 {behind}、无冲突")
    print("  步骤：主干工作树 merge → 真实 build → 写进展台账 → push origin/main → 同步 main")
    if dry:
        print("  （--dry：到此为止）")
        return

    OPS.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(f"{os.getpid()} {branch}\n")
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
            subprocess.run(["git", "-C", str(wt), "merge", "--abort"], capture_output=True, text=True)
            print("× 合并失败，已丢弃：", m.stderr.strip()[:300])
            sys.exit(1)
        print("  merge ok（main 未动）")
        gate_ok, gate_out = _build_frontend(wt)
        print(f"  build 门禁 {'ok' if gate_ok else '× 失败 → 丢弃，main 不动'}")
        if not gate_ok:
            print(gate_out[-800:])
            print(f"  main 仍是 {pre[:10]}，源分支 {branch} 未受影响")
            sys.exit(1)
        merge_commit = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                                      capture_output=True, text=True).stdout.strip()
        _record_land_progress(wt, branch, merge_commit)
        pushed = _push_main_with_one_retry(wt)
        if pushed:
            print("  " + pushed.replace("\n", "\n  "))
        new = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", str(REPO), "update-ref", f"refs/heads/{MAIN}", new, pre],
                       check=True)
        subprocess.run(["git", "-C", str(REPO), "fetch", "origin", MAIN],
                       capture_output=True, text=True)
        print(f"  main 推进 {pre[:8]} → {new[:8]}（{branch} 已落盘并推送 origin）")
    finally:
        if LOCK.exists():
            LOCK.unlink()


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
    print(f"  └ 有冲突需裁决 {sum(1 for r in rows if r['kind']=='CONFLICT')}")
    groups = conflict_groups(rows)
    print(f"冲突收拢族     {len(groups)} 个（可把冲突分支收成 convergence 任务）")
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


def cmd_tick(args):
    """自动挡主循环：输入归一 -> 真相刷新 -> 冲突收拢 -> 看板/上下文 -> 落盘状态。"""
    print("[1/7] route inbox", flush=True)
    _truth("route", "--go")
    print("\n[2/7] refresh project status", flush=True)
    _truth("snapshot")

    rows = scan()
    print("  merged=%d clean=%d conflict=%d" % (
        sum(1 for r in rows if r["kind"] == "MERGED"),
        sum(1 for r in rows if r["kind"] == "CLEAN"),
        sum(1 for r in rows if r["kind"] == "CONFLICT")))

    print("\n[3/7] consolidate conflict families", flush=True)
    if "--no-converge" not in args:
        cmd_converge(["--go", "--limit", "8"])
    else:
        print("  （--no-converge：跳过）")

    print("\n[4/7] refresh board + dashboard", flush=True)
    cmd_board([])
    cmd_dash([])

    print("\n[5/7] write context packs for next tasks", flush=True)
    rows = scan()
    branches = [r["branch"] for r in rows if r["kind"] == "CLEAN"][:20]
    branches += [g.get("branch") for g in load_convergence().get("groups", [])[:20]
                 if g.get("branch")]
    if branches:
        cmd_context(branches)
    else:
        print("  没有待执行分支，跳过")

    print("\n[6/7] commit + push truth state", flush=True)
    sync_trunk_truth(REPO, "sync from tick")
    push_truth_state()
    print("\n[7/7] what to do next", flush=True)
    _truth("next")


CMDS = {"inbox": cmd_inbox, "intake": cmd_intake, "route": cmd_route, "suggest": cmd_suggest,
        "matrix": cmd_matrix, "queue": cmd_queue, "converge": cmd_converge,
        "context": cmd_context, "dash": cmd_dash,
        "clash": cmd_clash, "reap": cmd_reap, "land": cmd_land, "supersede": cmd_supersede,
        "sync": cmd_sync,
        "status": cmd_status, "board": cmd_board, "next": cmd_next, "tick": cmd_tick}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__)
        print("命令：" + " | ".join(CMDS))
        sys.exit(1)
    CMDS[sys.argv[1]](sys.argv[2:])
