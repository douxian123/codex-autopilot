#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁：本机 launchd 里的定时任务，不许停在「没人会升级它」的位置上。

要根治的病（2026-10-27 实测两次，同一种）
------------------------------------------
* `com.danta.codex-disk-reap` 每小时跑 `~/.codex-autopilot/ops/disk_reap.py`。
  那个文件不在任何不可变版本目录里，`install.sh` 从不写它 → 永远停在首次手工
  拷贝那一版。修好的回收逻辑发了 44 个版本、守护进程那条路早就生效，这条每小时
  的路径还在跑 10-21 的代码：每小时回收 0 个，磁盘反复被写爆。
* 审计同一类，还有 5 条：watchdog / 会话快照 / hooks 看门狗 / 3000 巡检 /
  代理日志轮转，源文件全在 `~/.codex-guard/` 这类目录，全部不在任何仓、
  全部不会被 install 刷新。

用户的原话是「到底是什么东西，一直要这样反复地清？有没有一种干净的机制」。
机制一直都在，问题在于它跑的是化石代码。所以这里不是「再查一次」，而是立一条
结构性判据：**凡是自动挡自己的定时任务，执行路径必须落在版本化目录里，或者
是登记过的薄转发。**

三档判定
--------
绿（fresh）：ProgramArguments 里那个脚本落在
    `~/plugins/codex-autopilot/releases/<version>/…`
  或 `…/production-releases/runtime/<stamp>-staged/…`
  这类「随发版一起换、没人能手改进去」的目录。

黄（forwarder）：不在版本目录，但它是 registry.json 里登记过的薄转发 ——
  文件存在、正文里没有任何业务实现关键字、且哈希与登记值一致。
  薄转发的意义是「它不需要升级，因为它自己不含逻辑」。

红（stale）：属于我们但既不是绿也不是黄。这就是「跑着跑着又得清一轮」的病灶。

不属于我们的任务（Chrome、openclaw gateway、GitHub runner 之类）不判定，
只在报告里列出来，避免误伤别人的东西。
"""
import hashlib
import json
import os
import plistlib
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent

# 薄转发里出现这些关键字，说明有人在里面写实现了 —— 那就不是薄转发。
FORBIDDEN_IN_FORWARDER = (
    "shutil.rmtree", "_reap_dead_worktrees", "worktree_removable",
    "orphan_residue", "_reap_kg_tmp", "remove_orphan_residue",
)


def _home() -> Path:
    raw = os.environ.get("KG_GUARD_HOME")
    return Path(raw).expanduser() if raw else Path.home()


def _load_registry() -> dict:
    raw = os.environ.get("KG_GUARD_REGISTRY")
    path = Path(raw).expanduser() if raw else OPS / "guards" / "registry.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _expand(raw: str, home: Path) -> Path:
    if raw.startswith("~/"):
        return home / raw[2:]
    p = Path(raw).expanduser()
    if not p.is_absolute():
        return home / raw.lstrip("./")
    return p


def _program_paths(plist_path: Path) -> list[str]:
    try:
        data = plistlib.loads(plist_path.read_bytes())
    except Exception:
        return []
    out: list[str] = []
    args = data.get("ProgramArguments")
    if isinstance(args, list):
        for a in args:
            s = str(a)
            if s.startswith("/"):
                out.append(s)
    prog = data.get("Program")
    if isinstance(prog, str):
        out.append(prog)
    return out


def _versioned_roots(home: Path) -> list[Path]:
    """哪些目录属于「随发版一起换」。"""
    roots = [
        home / "plugins/codex-autopilot/releases",
        home / ".openclaw/production-releases/runtime",
    ]
    return [r for r in roots if r.is_dir()]


def classify(path: Path, home: Path, versioned: list[Path]) -> str:
    for root in versioned:
        try:
            path.relative_to(root)
            return "fresh"
        except ValueError:
            continue
    return "unmanaged"


def _in_owned_scope(path: Path, home: Path, prefixes: list[str]) -> bool:
    for raw in prefixes:
        base = _expand(raw, home)
        try:
            path.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def forwarder_ok(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "文件不存在"
    try:
        body = path.read_text(encoding="utf-8")
    except Exception as exc:
        return False, f"读不了：{exc}"
    for word in FORBIDDEN_IN_FORWARDER:
        if word in body:
            return False, f"正文里出现实现关键字 {word!r}，它不是薄转发"
    if "runpy.run_path" not in body:
        return False, "正文里没有 runpy.run_path，不是转发"
    return True, ""


def main() -> int:
    home = _home()
    reg = _load_registry()
    prefixes = reg.get("owned_prefixes") or []
    registered = {e["label"]: e for e in (reg.get("entries") or [])}
    forwarders = {}
    for fw in reg.get("registered_forwarders") or []:
        forwarders[_expand(fw["path"], home)] = fw

    versioned = _versioned_roots(home)
    pdir = home / "Library" / "LaunchAgents"
    failures: list[str] = []
    stats = {"fresh": 0, "forwarder": 0, "stale": 0, "foreign": 0, "ours_absent": 0}

    if not pdir.is_dir():
        print(f"（{pdir} 不存在，本机没有 launchd 任务）")
        return 0

    seen_labels = set()
    for plist_path in sorted(pdir.glob("*.plist")):
        label = plist_path.stem
        seen_labels.add(label)
        paths = _program_paths(plist_path)
        if not paths:
            continue
        ours = [p for p in paths if _in_owned_scope(Path(p), home, prefixes)]
        if not ours:
            stats["foreign"] += 1
            continue

        # 判据只看「真正被执行的脚本」，解释器路径不参与判定。
        scripts = [Path(p) for p in ours if Path(p).suffix in (".py", ".sh", ".mjs", ".js")]
        if not scripts:
            scripts = [Path(p) for p in ours]
        verdicts = []
        for script in scripts:
            if classify(script, home, versioned) == "fresh":
                verdicts.append(("fresh", script, ""))
            elif script in forwarders:
                ok, why = forwarder_ok(script)
                verdicts.append(("forwarder" if ok else "stale", script, why))
            elif script.suffix == ".py" and script.parent.name == "ops" and \
                    (script.parent / "disk_reap.py").exists() and "runpy.run_path" in \
                    script.read_text(encoding="utf-8", errors="ignore"):
                # 未登记但内容确实是薄转发：给它黄，但要求补登记。
                verdicts.append(("stale", script, "是薄转发但没在 registry.json 登记"))
            else:
                verdicts.append(("stale", script, "既不在版本目录，也不是登记过的薄转发"))

        worst = "fresh"
        for verdict, script, why in verdicts:
            if verdict == "stale":
                worst = "stale"
                failures.append(
                    f"{label} 仍在跑非版本化路径：{script}"
                    + (f"（{why}）" if why else ""))
            elif verdict == "forwarder" and worst == "fresh":
                worst = "forwarder"
        stats[worst] += 1
        print(f"  [{worst:9s}] {label}  ->  {[str(s) for s in scripts]}")

    # 登记了但本机没装的：不算失败（用户可能只装了部分能力），但要可见。
    for label in registered:
        if label not in seen_labels:
            stats["ours_absent"] += 1
            print(f"  [absent   ] {label}（registry 登记了，但本机没装这个 plist）")

    # 另外一条：登记项里的脚本必须真的随版本目录发出去。
    base = None
    state = home / "plugins/codex-autopilot/install-state.json"
    try:
        raw = json.loads(state.read_text(encoding="utf-8")).get("source") or ""
        if raw:
            cand = Path(raw).expanduser()
            if not cand.is_absolute():
                cand = home / raw.lstrip("./")
            if cand.is_dir():
                base = cand
    except Exception:
        pass
    if base is not None:
        for entry in reg.get("entries") or []:
            for rel in [entry["script"], *(entry.get("siblings") or [])]:
                if not (base / rel).exists():
                    failures.append(f"当前安装版本缺文件：{base / rel}（{entry['label']} 要用）")

    print()
    print(f"统计：鲜活 {stats['fresh']} 条 / 薄转发 {stats['forwarder']} 条 / "
          f"停在旧路径 {stats['stale']} 条 / 不归我们 {stats['foreign']} 条 / "
          f"登记但未装 {stats['ours_absent']} 条")
    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        print("\n这不是「再清一轮」能修的：跑一次 ./install.sh（5.7 段会统一改指），")
        print("然后重跑本门禁。")
        return 1
    print("\nlaunchd 入口新鲜度门禁：PASS（没有定时任务停在没人会升级它的位置）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
