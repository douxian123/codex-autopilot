#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""context_learning —— 上下文管家的跨会话自学习提案层（stdlib only）。

这个模块只做一件事：从**当前项目绑定**的历史会话与唯一入口里，找出用户反复
提及且能归纳为规范的主题，提出候选。它不直接写生效约束、不改优先级台账、
不自动追问用户；所有结果都停在提案层，等现有 constraint_candidates 的人确认闸。

判据故意保守：
  - 只统计真实 user 消息，不把 Agent 复述当用户需求；
  - 主题必须命中明确的规范信号，且至少跨 2 个会话出现；
  - 同一会话里的重复只证明强调强度，不冒充跨会话共识；
  - 缺历史会话源时返回 unknown，不把「没扫到」说成「没有重复主题」。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent

DEFAULT_MIN_THREADS = 2
DEFAULT_MIN_MENTIONS = 3
DEFAULT_MAX_USER_MESSAGES = 200
_OBSERVE_CACHE = {}

# 主题定义保持产品可读，不用 LLM 自由发挥。每个主题是一组语义接近的表达，
# 命中后保留用户原话作为证据；归纳语句只是给人确认的提案，不直接生效。
THEMES = [
    {
        "key": "reader_surface_noise",
        "title": "面向用户的面板/读者面只展示值得关注的信息，避免噪音和内部记账",
        "pattern": r"(面板|读者面|界面|UI).{0,40}(噪音|无关|干扰|垃圾|信息不过载|注意力)"
                   r"|(噪音|无关|干扰|垃圾).{0,40}(面板|读者面|界面|UI)"
                   r"|注意力(机制|预算)",
        "scope": ["ui/", "frontend/", "plugin/ui/"],
    },
    {
        "key": "non_destructive_positive_only",
        "title": "所有改动只做正向优化，禁止破坏性修改、负向工程和影响用户工作进程",
        "pattern": r"(破坏性修改|负向工程|负向修改|负向影响|不要再做负向|不要影响.{0,20}工作进程"
                   r"|不能.{0,20}破坏性|不要.{0,20}破坏性|只能做优化|正向修改)",
        "scope": [],
    },
    {
        "key": "single_entry_unified_routing",
        "title": "用户只在一个入口表达，系统负责跨项目/跨窗口路由，不让用户自己选",
        "pattern": r"(万法归一|唯一入口|一个输入框|同一个输入框|不要让我.{0,20}选"
                   r"|不应该让我.{0,30}对话|路由调度|派出去)",
        "scope": ["ops/central_daemon.py", "ops/project_router.py", "ops/central_mcp.py"],
    },
    {
        "key": "evidence_first_closure",
        "title": "验收和交付必须基于真实证据与端到端闭环，不用绿字、记录或可合并替代生效",
        "pattern": r"(真实验证|真截图|端到端|闭环|落 main|合入 main|证据|退出码|验收标准"
                   r"|不要.{0,20}自测通过|不能.{0,20}HTTP 200)",
        "scope": ["ops/kgctl.py", "ops/truthctl.py", "ops/central_mcp.py"],
    },
]


def _state_root() -> Path:
    repo = os.environ.get("KG_REPO") or ""
    return Path(repo).expanduser() if repo else REPO


def truth_dir() -> Path:
    return _state_root() / "ops" / "truth"


def patterns_path() -> Path:
    return truth_dir() / "context_patterns.jsonl"


def candidates_path() -> Path:
    return truth_dir() / "constraint_candidates.jsonl"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _text_hash(text: str) -> str:
    return hashlib.sha256(" ".join((text or "").split()).encode("utf-8")).hexdigest()[:16]


def _iter_jsonl_lines(path: Path):
    """逐行读会话文件：不把整文件解码成字符串列表，也不一次性 splitlines。"""
    with path.open("rb") as fh:
        for raw in fh:
            yield raw.decode("utf-8", errors="replace").strip()


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _latest(path: Path) -> dict:
    out = {}
    for row in _read_jsonl(path):
        rid = row.get("id")
        if rid:
            out[rid] = row
    return out


def _next_id(rows: list) -> str:
    nums = []
    for rid in {r.get("id") for r in rows}:
        m = re.match(r"^CP-(\d+)$", str(rid))
        if m:
            nums.append(int(m.group(1)))
    return "CP-%04d" % ((max(nums) + 1) if nums else 1)


def _project_key() -> str:
    repo = _state_root().resolve()
    return repo.name or str(repo)


def _normalized_remote(value: str) -> str:
    text = (value or "").strip()
    m = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", text)
    if m and "://" not in text:
        text = f"https://{m.group(1)}/{m.group(2)}"
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    return text.rstrip("/").removesuffix(".git").lower()


@lru_cache(maxsize=64)
def _git_worktrees(repo: str) -> tuple:
    """当前项目的全部 worktree:会话开在哪个隔离目录都能认回来。"""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            return ()
        return tuple(
            str(Path(line.strip()[len("worktree "):]).expanduser().resolve())
            for line in proc.stdout.splitlines()
            if line.startswith("worktree ")
        )
    except (OSError, subprocess.SubprocessError):
        return ()


@lru_cache(maxsize=4096)
def _remote_for_path(value: str) -> str:
    """取路径所在 Git 仓库的 origin，取不到返回空串，不猜项目归属。"""
    try:
        top = _git_toplevel_fast(value)
        if not top:
            return ""
        proc = subprocess.run(
            ["git", "-C", top, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            return ""
        return _normalized_remote(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_toplevel_fast(value: str) -> str:
    """先用目录结构判断是不是 Git 仓库，避免给每个文档目录都起 git 子进程。"""
    try:
        raw = Path(value).expanduser().resolve()
        if not raw.exists():
            return ""
        base = raw if raw.is_dir() else raw.parent
        for candidate in (base, *base.parents):
            if (candidate / ".git").exists():
                return str(candidate)
    except OSError:
        return ""
    return ""


def _project_identity() -> dict:
    """项目身份：用稳定 remote 与注册路径识别同一项目的所有 worktree。"""
    repo = _state_root().resolve()
    identity = {"names": {repo.name}, "paths": [str(repo)], "remotes": set(),
                "project_key": _project_key(), "other_paths": []}
    for path in _git_worktrees(str(repo)):
        if path not in identity["paths"]:
            identity["paths"].append(path)
    try:
        proc = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=5)
        remote = _normalized_remote(proc.stdout)
        if proc.returncode == 0 and remote:
            identity["remotes"].add(remote)
    except (OSError, subprocess.SubprocessError):
        pass
    registry = truth_dir() / "project_folders.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for row in data.get("folders") or []:
        if not isinstance(row, dict):
            continue
        row_remote = _normalized_remote(str(row.get("remote") or row.get("git_remote") or ""))
        belongs = (identity["project_key"] in {str(row.get("id") or ""), str(row.get("name") or "")}
                   or bool(row_remote and row_remote in identity["remotes"]))
        if not belongs and row.get("path"):
            other = str(Path(str(row["path"])).resolve())
            if other not in identity["other_paths"]:
                identity["other_paths"].append(other)
        if not belongs:
            continue
        for key in ("id", "name"):
            if row.get(key):
                identity["names"].add(str(row[key]))
        if row.get("path"):
            identity["paths"].append(str(Path(str(row["path"])).resolve()))
    return identity


def _session_roots() -> list:
    roots = []
    env = os.environ.get("KG_SESSION_ROOTS")
    if env:
        roots.extend(Path(x).expanduser() for x in env.split(":") if x.strip())
    else:
        roots.append(Path.home() / ".codex" / "sessions")
    seen, out = set(), []
    for root in roots:
        key = str(root.resolve())
        if key not in seen and root.exists():
            seen.add(key)
            out.append(root)
    return out


def _session_belongs(meta: dict, identity: dict, path: Path = None,
                     probe_remote: bool = True) -> bool:
    """路径能判就先判；只有路径判不准时才探测 Git remote。"""
    candidates = [str(meta.get("cwd") or "")]
    candidates.extend(str(Path(str(x)).resolve())
                      for x in (meta.get("runtime_workspace_roots") or []))
    if path is not None:
        candidates.append(str(path))
    resolved_values = []
    for value in candidates:
        if not value:
            continue
        resolved = str(Path(value).expanduser().resolve())
        parts = {part.lower() for part in Path(resolved).parts}
        if any(str(name).lower() in parts
               for name in identity["names"] if len(str(name)) >= 4):
            return True
        for base in identity["paths"]:
            if len(base) > 4 and (resolved == base or resolved.startswith(base + "/")):
                return True
        for base in identity.get("other_paths") or []:
            if len(base) > 4 and (resolved == base or resolved.startswith(base + "/")):
                return False
        resolved_values.append(resolved)
    if not probe_remote:
        return False
    # 已知路径先判完；只剩陌生路径才做 git remote 探测，避免每会话两三次子进程。
    for resolved in resolved_values:
        remote = _remote_for_path(resolved)
        if remote and identity["remotes"]:
            if remote in identity["remotes"]:
                return True
            # 明确是另一个 Git 仓库，就不再用目录名猜归属。
            continue
    return False


def _normalize_alias(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "")).lower()
    return text.replace("挡", "档").replace("-", "").replace("_", "")


def _alias_terms(identity: dict) -> set:
    """能在原话里稳定指向本项目的词，不收「自动挡」这种泛称。"""
    terms = set()
    values = set(identity.get("names") or set())
    if identity.get("project_key"):
        values.add(str(identity["project_key"]))
    for value in values:
        alias = _normalize_alias(value)
        if len(alias) >= 6:
            terms.add(alias)
    for remote in identity.get("remotes") or set():
        alias = _normalize_alias(remote)
        if len(alias) >= 8:
            terms.add(alias)
    return terms


def _iter_jsonl_raw_lines(path: Path):
    """流式读 JSONL 原始行；只解码候选行，避免整段历史会话进内存。"""
    try:
        with path.open("rb") as fh:
            for raw in fh:
                line = raw.strip()
                if line:
                    yield line
    except OSError:
        return


def _decode_line(line):
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
    try:
        row = json.loads(text)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _iter_user_texts(lines, max_messages=None):
    """从原始行里取真实用户原话；max_messages 只限制非噪声原话条数。"""
    count = 0
    for line in lines:
        if isinstance(line, bytes):
            if b'"role": "user"' not in line and b'"role":"user"' not in line:
                continue
        elif '"role": "user"' not in str(line) and '"role":"user"' not in str(line):
            continue
        row = _decode_line(line)
        if row is None:
            continue
        payload = row.get("payload") or {}
        if payload.get("type") != "message" or payload.get("role") != "user":
            continue
        for part in payload.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "input_text":
                continue
            text = " ".join(str(part.get("text") or "").split())
            if not text or _noise(text):
                continue
            yield text
            count += 1
            if max_messages is not None and count >= max_messages:
                return


def _content_belongs_texts(texts, identity: dict) -> bool:
    """只认真实用户原话里明确的项目名；前 3 条足够判定归属。"""
    aliases = _alias_terms(identity)
    if not aliases:
        return False
    negative_phrases = ("当前项目是别的", "当前项目不是", "这是别的项目",
                        "别的项目", "其他项目", "其它项目")
    seen_user_messages = 0
    for text in texts:
        seen_user_messages += 1
        normalized = _normalize_alias(text)
        named = any(alias in normalized for alias in aliases)
        if named and any(phrase in text for phrase in negative_phrases):
            return False
        if named:
            return True
        if seen_user_messages >= 3:
            return False
    return False


def _content_belongs_lines(lines, identity: dict) -> bool:
    return _content_belongs_texts(_iter_user_texts(lines, max_messages=3), identity)


def _content_belongs(path: Path, identity: dict) -> bool:
    return _content_belongs_lines(_iter_jsonl_raw_lines(path), identity)


def _noise(text: str) -> bool:
    """只统计人说的话；委派正文、系统注入和 harness 噪声不算需求。"""
    stripped = " ".join(str(text or "").split())
    if not stripped:
        return True
    markers = (
        "# AGENTS.md instructions",
        "Files mentioned by the user",
        "<in-app-browser-context",
        "<codex_internal_context",
        "<external_goal_objective",
        "<user_goal>",
        "<goal>",
        "<hook_prompt",
        "<heartbeat>",
        "<automation_id>",
        "System message from the application",
        "User set the goal",
        "User set goal status",
        "active thread goal objective",
        "<!-- LIVE-RULES",
        "<turn_aborted>",
        "ECC_AGENT_OK:",
        "【任务】",
    )
    if any(marker in stripped for marker in markers):
        return True
    head = stripped[:240]
    return (head.startswith("你是项目")
            or head.startswith("你是 ")
            or stripped == "继续")


def _meta_from_lines(lines) -> dict:
    for line in lines:
        if isinstance(line, bytes):
            marker_ok = (b'"type":"session_meta"' in line
                         or b'"type": "session_meta"' in line)
        else:
            marker_ok = ('"type":"session_meta"' in line
                         or '"type": "session_meta"' in line)
        if not marker_ok:
            continue
        row = _decode_line(line)
        if row is None:
            continue
        if row.get("type") == "session_meta":
            return row.get("payload") or {}
    return {}


def _meta_by_file(path: Path) -> dict:
    return _meta_from_lines(_iter_jsonl_raw_lines(path))


def _iter_session_messages() -> dict:  # type: ignore[no-redef]
    """按会话聚合命中主题的用户原话；只统计 user message。"""
    identity = _project_identity()
    aliases = _alias_terms(identity)
    cache_key = (str(_state_root().resolve()),
                 tuple(sorted(str(Path(x).resolve()) for x in _session_roots())))
    cached = _OBSERVE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    by_theme = {t["key"]: {} for t in THEMES}
    scanned = 0
    seen_hashes = set()
    for root in _session_roots():
        for path in root.rglob("*.jsonl"):
            scanned += 1
            meta = _meta_from_lines(_iter_jsonl_raw_lines(path))
            belongs = _session_belongs(meta, identity, path)
            if not belongs and aliases and _content_belongs(path, identity):
                belongs = True
            if not belongs:
                continue
            session_id = str(meta.get("id") or path.stem)
            for text in _iter_user_texts(_iter_jsonl_raw_lines(path),
                                         max_messages=DEFAULT_MAX_USER_MESSAGES):
                text_hash = _text_hash(text)
                for theme in THEMES:
                    if not re.search(theme["pattern"], text, re.IGNORECASE):
                        continue
                    dedup_key = (session_id, theme["key"], text_hash)
                    if dedup_key in seen_hashes:
                        continue
                    seen_hashes.add(dedup_key)
                    by_theme[theme["key"]].setdefault(session_id, []).append(text)
    result = {"themes": by_theme, "scanned_sessions": scanned}
    _OBSERVE_CACHE[cache_key] = result
    return result


def _inbox_signals() -> dict:
    """已路由入口也算证据；它与历史会话合并时会按会话数去重。"""
    inbox = _state_root() / "ops" / "tasks" / "inbox.jsonl"
    by_theme = {t["key"]: {} for t in THEMES}
    for row in _read_jsonl(inbox):
        if not row.get("routed"):
            continue
        text = " ".join(str(row.get("text") or "").split())
        if not text:
            continue
        session = str(row.get("thread_id") or row.get("source_thread") or "inbox")
        for theme in THEMES:
            if re.search(theme["pattern"], text, re.IGNORECASE):
                by_theme[theme["key"]].setdefault(session, []).append(text)
    return by_theme


def observe(min_threads=None, min_mentions=None) -> dict:
    """扫描历史与入口，返回高频主题；不写任何文件。"""
    min_threads = int(min_threads or os.environ.get("KG_LEARN_MIN_THREADS")
                      or DEFAULT_MIN_THREADS)
    min_mentions = int(min_mentions or os.environ.get("KG_LEARN_MIN_MENTIONS")
                       or DEFAULT_MIN_MENTIONS)
    scan = _iter_session_messages()
    inbox = _inbox_signals()
    if not scan.get("scanned_sessions"):
        return {"status": "unknown", "reason": "历史会话源取不到，不把没扫到当成没有重复主题",
                "themes": []}

    found = []
    for theme in THEMES:
        sessions = dict(scan["themes"][theme["key"]])
        for sid, texts in inbox[theme["key"]].items():
            sessions.setdefault(sid, []).extend(texts)
        mentions = sum(len(v) for v in sessions.values())
        threads = len(sessions)
        if threads < min_threads or mentions < min_mentions:
            continue
        evidence = []
        for sid, texts in sessions.items():
            for text in texts[:2]:
                evidence.append({"thread": sid, "text": text[:240],
                                 "hash": _text_hash(text)})
        priority = "high" if threads >= 4 or mentions >= 8 else "normal"
        found.append({
            "key": theme["key"], "title": theme["title"], "scope": theme["scope"],
            "mentions": mentions, "threads": threads, "priority": priority,
            "min_threads": min_threads, "min_mentions": min_mentions,
            "evidence": evidence[:8],
        })
    found.sort(key=lambda r: (r["priority"] != "high", -r["threads"], -r["mentions"]))
    return {"status": "ok", "themes": found,
            "scanned_sessions": scan["scanned_sessions"],
            "reason": "只统计当前项目绑定会话与已路由入口的真实用户原话"}


def propose(min_threads=None, min_mentions=None) -> dict:
    """把达到阈值的主题转成 constraint 候选卡；确认闸仍由 constraint_candidates 管。"""
    report = observe(min_threads, min_mentions)
    if report.get("status") != "ok":
        return report
    # 同一进程里已加载的 constraint_candidates 也用本模块的文件根，避免测试里
    # 环境顺序造成两个模块各读各的 KG_REPO。
    import constraint_candidates
    constraint_candidates.OPS = OPS
    constraint_candidates.truth_dir = truth_dir
    constraint_candidates.candidates_path = candidates_path
    path = patterns_path()
    latest = _latest(path)
    rows = _read_jsonl(path)
    created = []
    for theme in report["themes"]:
        rid = "CP-%s" % theme["key"]
        existing = latest.get(rid)
        sig = (theme["mentions"], theme["threads"], theme["priority"])
        if existing and (existing.get("status") in {"proposed", "promoted", "dismissed"}
                         or (existing.get("mentions"), existing.get("threads"),
                             existing.get("priority")) == sig):
            continue
        row = {"id": rid, "ts": _now(), "type": "context_pattern", "status": "proposed",
               **theme}
        _append(path, row)
        rows.append(row)
        created.append(row)
    # 交给 constraint_candidates 生成候选卡；它仍只写 candidate，不写生效约束。
    imported = []
    try:
        import constraint_candidates
        for row in created:
            evidence = row.get("evidence") or []
            sample = evidence[0]["text"] if evidence else row["title"]
            res = constraint_candidates.propose_learned(
                key=row["key"], statement=row["title"], scope=row.get("scope") or [],
                evidence=evidence, priority=row.get("priority") or "normal",
                mentions=row["mentions"], threads=row["threads"], sample=sample)
            imported.append(res)
    except Exception as exc:
        return {**report, "status": "error", "reason": f"主题写进台账但候选接线失败：{exc!r}",
                "created": created, "candidate_results": imported}
    return {**report, "created": created, "candidate_results": imported}


def daemon_hook(dry: bool = False, log_fn=None) -> dict:
    """守护每拍的 fail-open 学习钩子；只提候选，绝不自动写生效约束。"""
    if dry:
        return {"status": "dry", "reason": "预演不扫描、不写候选"}
    res = propose()
    if log_fn and res.get("created"):
        log_fn(f"[context-learning] 提名 {len(res['created'])} 条重复诉求："
               + "、".join(str(c.get("id")) for c in res["created"]))
    return res


def dismiss_learned(cid: str, reason: str = "") -> dict:
    """学习候选的「不算」出口；与 intake 候选共用 dismiss 状态。"""
    import constraint_candidates
    return constraint_candidates.dismiss(cid, reason)


def promote_learned(cid: str, enforceable: str = "none", cmd: str = "") -> dict:
    """人点头后走统一候选闸；source_pattern 写进生效约束供逐条自检。"""
    import constraint_candidates
    return constraint_candidates.promote(
        cid, enforceable=enforceable, cmd=cmd,
        env_extra={"KG_TRUTH_SOURCE": "context_learning"})


def _selfcheck_identity_fixture() -> dict:
    return _project_identity()


def selfcheck() -> dict:
    """逐条检查生效学习约束：只有 enforceable=gate 且真有 cmd 才算已落实。"""
    try:
        import constraints
    except Exception as exc:
        return {"status": "unknown", "reason": f"约束读取器取不到：{exc!r}", "items": []}
    path = truth_dir() / "constraints.jsonl"
    if not path.exists():
        return {"status": "unknown", "reason": "生效约束台账取不到，不写 0 条", "items": []}
    items = []
    for row in constraints.active_constraints(path):
        if row.get("source") != "context_learning":
            continue
        enforceable = str(row.get("enforceable") or "none")
        cmd = str(row.get("cmd") or "").strip()
        items.append({
            "id": row.get("id"), "statement": row.get("statement"),
            "enforceable": enforceable,
            "status": "ok" if enforceable != "gate" or cmd else "unwired",
            "detail": ("门禁已接：%s" % cmd if cmd else
                       "这条标了 gate 但没有命令，不能算已落实"),
        })
    return {"status": "ok", "items": items,
            "reason": "逐条检查学习来源的生效约束是否有机器兜底；none/checklist 如实显示"}


def human(report=None) -> str:
    report = report or observe()
    if report.get("status") != "ok":
        return "上下文管家自学习：%s" % (report.get("reason") or "取不到")
    if not report.get("themes"):
        return "上下文管家自学习：已扫 %s 个会话，没有达到重复阈值的规范主题。" % report.get(
            "scanned_sessions")
    out = ["上下文管家自学习：%s 个主题达到重复阈值（只提名，不自动生效）" % len(report["themes"])]
    for r in report["themes"]:
        out.append("  [%s] %s · %s 次 / %s 个会话 · %s" % (
            r["priority"], r["title"], r["mentions"], r["threads"], r["key"]))
        sample = (r.get("evidence") or [{}])[0].get("text") or ""
        if sample:
            out.append("        原话：%s" % sample[:160])
    return "\n".join(out)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="上下文管家：跨会话自学习提案与自检")
    ap.add_argument("cmd", nargs="?", default="observe",
                    choices=["observe", "propose", "selfcheck", "promote"])
    ap.add_argument("id", nargs="?", default="")
    ap.add_argument("--min-threads", type=int)
    ap.add_argument("--min-mentions", type=int)
    ap.add_argument("--enforceable", default="none")
    ap.add_argument("--cmd")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.cmd == "observe":
        res = observe(args.min_threads, args.min_mentions)
    elif args.cmd == "propose":
        res = propose(args.min_threads, args.min_mentions)
    elif args.cmd == "promote":
        res = promote_learned(args.id, args.enforceable, args.cmd or "")
    else:
        res = selfcheck()
    print(json.dumps(res, ensure_ascii=False, indent=1) if args.json else human(res))
    return 0 if res.get("status") in {"ok", "unknown"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
