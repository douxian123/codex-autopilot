#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Liveness guard: a worktree is not abandoned while something works inside it.

Why this module exists (measured 2026-11-10):
  The hourly reaper decided a worktree was reclaimable from git state alone --
  branch already merged into main, `git status` clean -- plus a directory-mtime
  idle gate. That is blind to the one case that actually matters: a desktop
  session whose working directory *is* the worktree. Such a session never shows
  up as `codex exec -C <tree>`, and read-only analysis never bumps the directory
  mtime, so a tree under active use read exactly like an abandoned one.
  On 2026-11-10 the daemon deleted 18 such trees in a single day; only the
  surviving branch pointers made the work recoverable at all. The user's own
  report of the symptom is in the knowledge-graph inbox at 04:36.

Two independent signals, because each one alone has a blind spot:
  * cwd of a live process -- a dev server, gate run, or shell that has the tree
    as its working directory. Catches activity whose command line never names
    the tree.
  * a recently written Codex transcript whose recorded cwd is inside the tree --
    catches a read-only session between commands, when no long-lived process is
    holding the tree as its cwd.

Failure direction is deliberate. Every probe degrades to "no evidence" when its
tool is unavailable, and this module is never the thing that authorises a
delete: callers treat a non-empty answer as a veto. A missing `lsof` therefore
falls back to the old behaviour instead of becoming an accidental delete
licence.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

# How long a silent transcript keeps protecting its tree. Long enough to cover a
# session that is thinking between turns, short enough that a tree is not pinned
# forever by a run that ended days ago.
LIVE_SESSION_MAX_AGE_S = float(
    os.environ.get("KG_LIVE_SESSION_MAX_AGE_S", str(3 * 3600)))
# The reaper walks dozens of candidates per round, so each probe is cached for
# this long rather than forked once per candidate.
LIVE_PROBE_TTL_S = float(os.environ.get("KG_LIVE_PROBE_TTL_S", "30"))
SESSION_PROBE_BYTES = 65536

_LIVE_CWD_CACHE: dict = {"ts": 0.0, "pids": {}}
_SESSION_CACHE: dict = {"ts": 0.0, "paths": {}}

_CWD_FIELD_RE = re.compile(r'"cwd"\s*:\s*"([^"]+)"')
_ROOTS_FIELD_RE = re.compile(r'"runtime_workspace_roots"\s*:\s*\[([^\]]*)\]')
_JSON_STRING_RE = re.compile(r'"([^"]+)"')


def session_roots() -> list[Path]:
    """Where Codex keeps rollout transcripts, in probe order."""
    env = os.environ.get("KG_SESSION_ROOTS")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    home = Path.home() / ".codex"
    return [home / "sessions", home / "archived_sessions"]


def reset_caches() -> None:
    """Drop both probe caches.

    Tests call this so a stale snapshot cannot mask a freshly started process.
    """
    _LIVE_CWD_CACHE.update(ts=0.0, pids={})
    _SESSION_CACHE.update(ts=0.0, paths={})


def _live_cwd_pids(max_age: float = LIVE_PROBE_TTL_S) -> dict:
    """{pid: cwd} for every process that has one, from a single `lsof` call."""
    now = time.time()
    if now - float(_LIVE_CWD_CACHE["ts"]) < max_age and _LIVE_CWD_CACHE["pids"]:
        return dict(_LIVE_CWD_CACHE["pids"])
    pids: dict = {}
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn"],
                             capture_output=True, text=True, errors="replace",
                             timeout=30).stdout
        cur = ""
        for line in out.splitlines():
            if not line:
                continue
            tag, val = line[0], line[1:]
            if tag == "p":
                cur = val
            elif tag == "n" and cur:
                pids[cur] = val
    except (OSError, subprocess.SubprocessError):
        pids = {}
    _LIVE_CWD_CACHE.update(ts=now, pids=pids)
    return dict(pids)


def _normalize_recorded_path(raw) -> str:
    """Turn a transcript-recorded path into a filesystem path.

    Codex transcripts record the desktop session cwd as a `file://` URL, not as
    a bare path: measured 2026-11-11, 9 of 30 live-session records were in that
    form (`file:///Users/danta/.newmax/workspace/knowledge-graph`). A bare
    string comparison against the tree path therefore matched none of them, so
    roughly a third of the session evidence arrived pre-blinded and those trees
    still read as abandoned. Percent-escapes are decoded for the same reason:
    repository names with CJK characters are stored fully escaped
    (`file:///.../kg-%E4%BE%A7%E6%A0%8F`).
    """
    text = str(raw or "").strip()
    if text.startswith("file://"):
        text = text[len("file://"):]
    try:
        return urllib.parse.unquote(text)
    except Exception:
        return text


def path_within(child, parent: Path) -> bool:
    """True when `child` is `parent` itself or sits below it (boundary aware).

    The boundary matters: `/w/kg-tree` must not be matched by a process sitting
    in `/w/kg-tree-extra`, which is exactly the kind of prefix bug that would
    protect the wrong tree.
    """
    if not child:
        return False
    # Resolve BOTH sides. On macOS a tree reached as `/var/...` and the same
    # tree reported by lsof or a transcript as `/private/var/...` are the same
    # directory but different strings; comparing a resolved base against an
    # unresolved child silently misses a genuine match, and a missed match here
    # means "not live" -> deletion. The boundary check is kept so that
    # `/w/kg-tree` is still not matched by `/w/kg-tree-extra`.
    base = os.path.realpath(str(parent)).rstrip("/")
    target = os.path.realpath(_normalize_recorded_path(child)).rstrip("/")
    if not target:
        return False
    return target == base or target.startswith(base + os.sep)


def cwd_map() -> dict:
    """{pid: cwd} 的公开视图，来自一次 `lsof`（内部有 30s 缓存）。

    给「按内核 cwd 找进程」的调用方用：回收器要认出工作区里的 dev server，而
    它们的命令行长什么样完全不可靠（`tsx watch` 的 argv 里没有树路径）。
    探针不可用时返回空字典 —— 与模块其它探针一致，空结果只意味着「没证据」，
    永远不会被当成删除许可。
    """
    try:
        return _live_cwd_pids()
    except Exception:
        return {}


def live_processes_in(worktree: Path) -> list:
    """PIDs whose current working directory is inside `worktree`."""
    cwd_by_pid = _live_cwd_pids()
    if not cwd_by_pid:
        return []
    return sorted(pid for pid, cwd in cwd_by_pid.items()
                  if path_within(cwd, worktree))


def _session_recorded_paths(transcript: Path) -> set:
    """Paths a transcript names as its cwd / workspace roots.

    Reads head and tail only: `session_meta` is the first line and the newest
    `turn_context` sits at the end, so a multi-megabyte transcript costs two
    bounded reads instead of a full parse.
    """
    try:
        size = transcript.stat().st_size
    except OSError:
        return set()
    spans = [(0, min(size, SESSION_PROBE_BYTES))]
    if size > SESSION_PROBE_BYTES:
        spans.append((size - SESSION_PROBE_BYTES, size))
    chunks = []
    try:
        with transcript.open("rb") as fh:
            for start, end in spans:
                fh.seek(start)
                chunks.append(
                    (fh.read(end - start) or b"").decode("utf-8", "replace"))
    except OSError:
        return set()
    blob = "\n".join(chunks)
    paths = set(_CWD_FIELD_RE.findall(blob))
    for group in _ROOTS_FIELD_RE.findall(blob):
        paths.update(_JSON_STRING_RE.findall(group))
    return paths


def active_session_paths(max_age_s: float = LIVE_SESSION_MAX_AGE_S,
                         roots: list | None = None) -> dict:
    """{path recorded as a session cwd: newest transcript mtime}.

    Only transcripts written within `max_age_s` count, so a session that merely
    once ran in a tree stops protecting it once it goes quiet -- this tracks live
    work, not history.
    """
    now = time.time()
    if (now - float(_SESSION_CACHE["ts"]) < LIVE_PROBE_TTL_S
            and _SESSION_CACHE["paths"]):
        return dict(_SESSION_CACHE["paths"])
    cutoff = now - max_age_s
    found: dict = {}
    for root in (roots if roots is not None else session_roots()):
        root = Path(root)
        if not root.exists():
            continue
        for transcript in root.rglob("*.jsonl"):
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            for path in _session_recorded_paths(transcript):
                found[path] = max(found.get(path, 0.0), mtime)
    _SESSION_CACHE.update(ts=now, paths=found)
    return dict(found)


def sessions_working_in(worktree: Path) -> list:
    """Recorded cwds of recently active sessions that sit inside `worktree`."""
    return sorted(p for p in active_session_paths()
                  if path_within(p, worktree))


def live_blocker(worktree: Path) -> str:
    """Reason string when something is still using `worktree`, else ""."""
    if isinstance(worktree, str):
        worktree = Path(worktree)
    try:
        pids = live_processes_in(worktree)
    except Exception:
        pids = []
    if pids:
        return f"cwd: pid {' '.join(pids[:6])}"
    try:
        sessions = sessions_working_in(worktree)
    except Exception:
        sessions = []
    if sessions:
        return f"active-session: {sessions[0][:160]}"
    return ""
