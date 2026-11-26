#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：守护只绑一个项目，但路由必须能收下**所有已注册项目**的待路由。

为什么有这条（2026-11-18 实测）：
  守护是**全局单例**，route pass 只跑当前绑定的 `KG_REPO`。用户在别的项目窗口
  里提的需求会写进那个项目自己的 `inbox.jsonl`，却没有任何人在跑它的 route ——
  永久停在 `routed=false`：既不在看板上、也不进问题台账、也不报错。

  真机现场：`kgctl status` 显示「待路由 8」，守护每 15 分钟 `[1/8] route inbox`
  `rc=0`，而 `codex-autopilot` 账本里那 8 条躺了 7~20 天，`ledger.jsonl` 是 0 条。
  这不是判错归属，是**根本没人接管**。

这条门禁锁住：
  1) `--all` 会把别的已注册项目的待路由真的路由掉（写它自己的泳道 + 问题台账）；
  2) 走 `--all` 预演（不给 `--go`）时**只读不写**，别的项目账本一条不加；
  3) 不碰当前绑定项目的账本，也不把别的项目的失败算到绑定项目头上；
  4) `central_daemon._pass_route` 真的挂着 `--all`（否则守护那层又退回只扫一个项目）。

退出码 = 未通过项数。
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
ROOT = OPS.parent
fails = 0


def check(label, ok, detail=""):
    global fails
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))
        fails += 1


def make_state_root(base: Path, name: str) -> Path:
    root = base / name
    (root / "ops" / "tasks").mkdir(parents=True)
    (root / "ops" / "truth").mkdir(parents=True)
    for rel in ("ops/tasks/inbox.jsonl", "ops/truth/ledger.jsonl",
                "ops/truth/progress.jsonl", "ops/truth/decisions.jsonl",
                "ops/truth/acceptance.jsonl"):
        (root / rel).write_text("", encoding="utf-8")
    (root / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
    return root


def load_kgctl(project: Path, home: Path):
    """在隔离的环境里加载 kgctl：KG_REPO=绑定项目，注册表在临时 home。

    `KG_AUTOPILOT_HOME` 必须**在后续调用期间也保持**：`project_router` 是在每次
    调用时读它（registry_path() 是函数，不是 import 时常量）。只在这里设、退出
    就还原，会让 `_route_all_projects` 转身去读真实 `~/.codex-autopilot`。
    """
    os.environ["KG_REPO"] = str(project)
    os.environ["KG_AUTOPILOT_HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("kgctl_route_all", ROOT / "ops" / "kgctl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 整个测试期间保持隔离：注册表 / 待路由箱都落在临时 home，绝不碰真实的那份。
_OLD_ENV = {k: os.environ.get(k) for k in ("KG_REPO", "KG_AUTOPILOT_HOME")}


def write_registry(home: Path, entries):
    (home / "routing").mkdir(parents=True, exist_ok=True)
    (home / "projects.json").write_text(
        json.dumps({"projects": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


with tempfile.TemporaryDirectory(prefix="route-all-") as tmp:
    base = Path(tmp)
    home = base / "autopilot-home"
    home.mkdir()
    bound = make_state_root(base, "bound-project")
    other = make_state_root(base, "other-project")

    # 别的项目攒下 2 条待路由（含一条判得出 gate 泳道的）。
    (other / "ops" / "tasks" / "inbox.jsonl").write_text(
        json.dumps({"ts": "2026-11-18 10:00:00", "text": "门禁断言失败：npm run build 挂了",
                    "source": "user", "files": [], "kind": "unknown",
                    "routed": False, "lane": None}, ensure_ascii=False) + "\n" +
        json.dumps({"ts": "2026-11-18 10:01:00", "text": "面板按钮点不动，bug",
                    "source": "user", "files": [], "kind": "unknown",
                    "routed": False, "lane": None}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (bound / "ops" / "tasks" / "inbox.jsonl").write_text(
        json.dumps({"ts": "2026-11-18 09:00:00", "text": "绑定项目自己的待路由",
                    "source": "user", "files": [], "kind": "unknown",
                    "routed": False, "lane": None}, ensure_ascii=False) + "\n",
        encoding="utf-8")

    write_registry(home, [
        {"name": "bound-project", "root": str(bound), "clues": [], "aliases": []},
        {"name": "other-project", "root": str(other), "clues": [], "aliases": []},
    ])

    kg = load_kgctl(bound, home)

    def other_state():
        inbox = [json.loads(l) for l in
                 (other / "ops" / "tasks" / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        ledger = [json.loads(l) for l in
                  (other / "ops" / "truth" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
                  if l.strip()]
        return inbox, ledger

    # ---- 1) 预演（不给 --go）只读：别的项目账本一条不写 ----
    res = kg._route_all_projects(go=False)
    inbox, ledger = other_state()
    check("预演时发现了别的项目的待路由", len(res) == 1 and res[0]["pending"] == 2, f"{res}")
    check("预演不写别的项目的问题台账", ledger == [], f"{len(ledger)} 条")
    check("预演不回写别的项目的 inbox",
          all(not r.get("routed") for r in inbox), f"{inbox}")

    # ---- 2) 真跑：别的项目 2 条都落账 + 回写 routed ----
    res = kg._route_all_projects(go=True)
    inbox, ledger = other_state()
    check("真跑时 2 条都归了类", res and res[0]["ok"] and res[0]["pending"] == 2, f"{res}")
    check("别的项目问题台账新增 2 条", len(ledger) == 2, f"{len(ledger)} 条")
    check("别的项目 inbox 全部标 routed",
          all(r.get("routed") for r in inbox), f"{[r.get('routed') for r in inbox]}")
    check("落账带上了泳道", all(r.get("lane") for r in ledger),
          f"{[r.get('lane') for r in ledger]}")
    # 语义：门禁类那条必须进 gate 泳道，bug 那条必须进 bug 泳道（判据只有一份）。
    lanes = {r.get("lane") for r in ledger}
    check("泳道按既有判据分配（gate + bug）", lanes == {"gate", "bug"}, f"{lanes}")

    # ---- 3) 不碰绑定项目自己的 inbox ----
    bound_inbox = [json.loads(l) for l in
                   (bound / "ops" / "tasks" / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
                   if l.strip()]
    check("不动当前绑定项目的 inbox（交给它自己的 route）",
          len(bound_inbox) == 1 and not bound_inbox[0].get("routed"), f"{bound_inbox}")

    # ---- 4) 只有一个项目时不该报错、也不该重复劳动 ----
    write_registry(home, [{"name": "bound-project", "root": str(bound)}])
    check("注册表里只有绑定项目时不外扫", kg._registered_roots(exclude=bound) == [])

for _key, _val in _OLD_ENV.items():
    if _val is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _val

# ---- 5) 守护那层必须真的挂着 --all ----
daemon_src = (ROOT / "ops" / "central_daemon.py").read_text(encoding="utf-8")
start = daemon_src.index("def _pass_route(ctx: dict) -> dict:")
body = daemon_src[start:daemon_src.index("def _pass_exec", start)]
check("守护 route pass 调用 kgctl route --all --go", '"route", "--all", "--go"' in body,
      repr(body[-300:]))

print(f"\nroute-all-projects: {'PASS' if fails == 0 else str(fails) + ' 项未通过'}")
raise SystemExit(fails)
