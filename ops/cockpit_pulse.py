#!/usr/bin/env python3
"""cockpit_pulse.py —— 司令台一页心跳（只读 / 幂等 / fail-open）

存在理由：心跳窗口每拍都让模型自己跑 7 条 git/curl/gh/kgctl 命令再拼汇报，
代价是「每拍 7 次工具调用 + 7 段工具输出」永久写进该窗口的上下文。
窗口因此越喂越肥（实测：24 小时内单轮 input 从 ~90k 涨到 ~150k，
rollout 140MB / 累计 3.15 亿 token），而模型在长上下文里会退化成
「只挑一句最短的话结束轮次」。

本脚本把同一批硬检查压成**一条命令、一页输出**：
模型只需要读这一页、判断有无变化、汇报。检查本身不再进上下文。

设计约束（与项目一致）：
  * 全部只读：不写仓库、不改状态、不落盘。
  * fail-open：任何一节取不到就打印「（取不到）」，绝不中断、绝不报错退出。
  * 一页制：输出 <= 约 1.5KB，供人/模型直接引用。
  * 无第三方依赖：只 use stdlib。

用法：
  python3 cockpit_pulse.py                      # 用默认路径
  python3 cockpit_pulse.py --repo <主干 checkout>
  python3 cockpit_pulse.py --thread <线程id> --json   # 机器可读（给上游程序用）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

def _default_repo() -> str:
    """默认项目根：优先环境变量，其次由 KG_REPO 推导，最后回退当前目录。

    不要再写死某个项目的绝对路径——本插件是给任意项目用的，
    硬编码路径会让它在别人机器上「体检一个不存在的项目」并报出一堆假问题。
    """
    for key in ("KG_TRUNK", "KG_REPO"):
        v = os.environ.get(key)
        if v:
            return v
    # 从当前工作目录向上找一个「像状态根」的目录（有 ops/truth 或 .git）
    try:
        cur = Path.cwd()
        for cand in [cur, *cur.parents]:
            if (cand / "ops" / "truth").is_dir() or (cand / ".git").exists():
                return str(cand)
    except Exception:
        pass
    return str(Path.cwd())


DEFAULT_REPO = _default_repo()
STATE = Path.home() / ".codex-autopilot" / "state.json"
CODEX_DB = Path.home() / ".codex" / "state_5.sqlite"
# 爆炸半径开关：存在该文件则整个脚本只打印一行提示后退出（一键关）。
OFF_SWITCH = Path.home() / ".codex/cockpit-pulse.OFF"


def run(cmd: list[str], cwd: str | None = None, timeout: int = 8) -> tuple[int, str]:
    """执行只读命令；任何异常都折成 (非0, 空串)，调用方自行降级。"""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except Exception:
        return 1, ""


def git(repo: str, *args: str, timeout: int = 8) -> str:
    rc, out = run(["git", "-C", repo, *args], timeout=timeout)
    return out if rc == 0 else ""


def http_code(url: str, timeout: int = 4) -> str:
    """探一次；只有拿到非 200 才用更长超时复测一次。

    为什么需要复测：这套服务会在真实工作负载下短暂变慢（实测后端处理
    POST /api/understanding 3888ms、POST /mcp 6829ms，health 直跑 curl 也有过
    14s 才回 200）。固定 4s 单次探测会把「慢」记成「000」，于是司令台每拍都
    误报服务红灯，而同一刻直跑 curl --max-time 30 其实是 200（见 L-0080）。
    快探 + 慢复测既保住「真挂了」的判定（复测仍非 200 才算挂），也不会因为
    一次慢就直接判红。
    """
    rc, out = run(
        ["curl", "-sS", "-o", "/dev/null", "-m", str(timeout), "-w", "%{http_code}", url],
        timeout=timeout + 2,
    )
    first = out or "ERR"
    if first == "200":
        return "200"
    # 慢复测：给它更长的时间窗，把「健康但慢」和「真的不可用」分开。
    rc2, out2 = run(
        ["curl", "-sS", "-o", "/dev/null", "-m", "20", "-w", "%{http_code}", url],
        timeout=22,
    )
    slow = out2 or "ERR"
    if slow == "200":
        return "200"  # 复测通过按健康处理，避免把慢误报成不可用
    return slow if slow != "ERR" else first


def daemon_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def ci_runs(repo: str, limit: int = 3) -> list[dict]:
    rc, out = run(
        ["gh", "run", "list", "--limit", str(limit),
         "--json", "databaseId,headSha,status,conclusion,createdAt"],
        cwd=repo, timeout=12,
    )
    if rc != 0 or not out:
        return []
    try:
        return json.loads(out)
    except Exception:
        return []


def merge_queue(repo: str, ops: str, timeout: int = 12) -> str:
    """只读取合并队列（一次一个，合完重算）。取不到就空。"""
    kgctl = Path(ops) / "kgctl.py"
    if not kgctl.exists():
        return ""
    env = dict(os.environ, KG_REPO=repo)
    try:
        p = subprocess.run([sys.executable, str(kgctl), "queue"], env=env,
                           capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "").strip()
    except Exception:
        return ""


def thread_budget(thread_id: str | None) -> dict:
    """预算自检：目标线程的 rollout 体量 + 累计 token + 最近一轮 input。

    这就是「窗口会不会越喂越肥」的可观测量。取不到就返回空 dict。
    """
    if not thread_id or not CODEX_DB.exists():
        return {}
    try:
        c = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True)
        row = c.execute(
            "select rollout_path, tokens_used, updated_at from threads where id=?",
            (thread_id,),
        ).fetchone()
        c.close()
    except Exception:
        return {}
    if not row:
        return {}
    rollout_path, tokens_used, updated_at = row
    info: dict = {"thread_id": thread_id, "rollout_mb": None,
                  "tokens_used": tokens_used, "last_turn_input": None}
    try:
        info["rollout_mb"] = round(os.path.getsize(rollout_path) / 1e6, 1)
    except Exception:
        pass
    # 最近一轮 input：读文件尾部若干行（不整读，避免大文件开销）
    try:
        with open(rollout_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 400_000))
            tail = fh.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            if '"token_usage_record"' in line:
                try:
                    info["last_turn_input"] = json.loads(line)["payload"]["usage"]["input_tokens"]
                except Exception:
                    pass
                break
    except Exception:
        pass
    return info


def resolve_automation_thread(name: str) -> str:
    """从 automation.toml 里读目标线程 id。

    为什么要这个入口：如果 prompt 里硬编码线程 id（「体检时带 --thread <某个id>」），
    那么**一旦轮换，新窗口就会去监控上一代的预算**——它每拍都会看到「已越轮换线」，
    于是永远在催换窗口，轮换等于没换。
    改成让体检自己按名字解析，prompt 里就没有会过期的 id，轮换也不必改 prompt。
    """
    cfg = Path.home() / ".codex" / "automations" / name / "automation.toml"
    if not cfg.exists():
        return ""
    try:
        m = re.search(r'^target_thread_id\s*=\s*"([^"]+)"', cfg.read_text(), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""



def build(args) -> dict:
    repo = args.repo
    t0 = time.time()
    head = git(repo, "rev-parse", "--short", "HEAD")
    main = git(repo, "rev-parse", "--short", "origin/main")
    behind = git(repo, "rev-list", "--count", f"{head}..{main}") if head and main else ""
    behind = behind if re.fullmatch(r"\d+", behind or "") else "?"
    recent = git(repo, "log", "--oneline", "-3")
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no")
    st = daemon_state()
    summary = st.get("last_summary") or {}
    fails = st.get("last_failures") or []
    return {
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo": repo,
        "git": {
            "head": head, "origin_main": main, "behind": behind,
            "in_sync": bool(head) and head == main,
            "recent": recent.splitlines(),
            "dirty_tracked": len([l for l in dirty.splitlines() if l.strip()]),
        },
        "services": {"frontend_3000": http_code("http://localhost:3000"),
                     "api_3001": http_code("http://localhost:3001/api/health")},
        "daemon": {
            "last_cycle": st.get("last_cycle_iso", ""),
            "merged": summary.get("merged"), "conflict": summary.get("conflict"),
            "unresolved": summary.get("unresolved"), "failures": len(fails),
            "total_landed": st.get("total_landed"), "total_resolved": st.get("total_resolved"),
            "blocked": {k: len(st.get(k) or {}) for k in
                        ("exec_blocked", "resolve_blocked", "port_blocked")},
        },
        "ci": [
            {"id": r.get("databaseId"), "sha": (r.get("headSha") or "")[:9],
             "status": r.get("status"), "conclusion": r.get("conclusion") or "-",
             "at": (r.get("createdAt") or "")[11:16]}
            for r in ci_runs(repo)
        ],
        "queue": merge_queue(repo, args.ops),
        "budget": thread_budget(args.thread),
        "budget_missing_reason": getattr(args, "budget_missing_reason", ""),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def render(d: dict) -> str:
    g, s, dm, b = d["git"], d["services"], d["daemon"], d["budget"]
    L = [f"# 司令台一页 · {d['now']}"]

    sync = "✅ 一致" if g["in_sync"] else f"❌ 落后 origin/main {g['behind']} 个 commit"
    L.append(f"\n【用户可见性】HEAD `{g['head']}` vs origin/main `{g['origin_main']}` → {sync}"
             f"；共享检出已跟踪脏文件 {g['dirty_tracked']} 项")
    ok = all(v == "200" for v in s.values())
    L.append(f"【服务】:3000={s['frontend_3000']}　:3001/api/health={s['api_3001']}"
             f" → {'✅ 全 200' if ok else '⚠️ 有非 200'}")
    L.append("【最近提交】" + " ｜ ".join(g["recent"] or ["（取不到）"]))

    L.append(f"\n【自动挡】last_cycle={dm['last_cycle'] or '（取不到）'}"
             f"　merged={dm['merged']}　conflict={dm['conflict']}　unresolved={dm['unresolved']}"
             f"　last_failures={dm['failures']}"
             f"　累计 land={dm['total_landed']} / resolve={dm['total_resolved']}")
    blocked = {k: v for k, v in (dm["blocked"] or {}).items() if v}
    L.append("　blocked：" + ("、".join(f"{k}={v}" for k, v in blocked.items()) if blocked else "无"))

    if d["ci"]:
        L.append("【CI 最新 3 条】" + " ｜ ".join(
            f"{r['id']} {r['sha']} {r['status']}/{r['conclusion']}@{r['at']}" for r in d["ci"]))
    else:
        L.append("【CI】取不到（网络或 gh 未登录，别当成绿灯）")

    if d["queue"]:
        L.append("【合并队列】\n" + d["queue"])
    else:
        L.append("【合并队列】空 / 取不到")

    # 体检：automation 漂移 / 真相层矛盾 —— 这两类问题只会在后台悄悄积累，
    # 放进每拍的一页里，坏之前就能看见。整段 fail-open（doctor 自己坏了不影响心跳）。
    if not OFF_SWITCH.exists() and os.environ.get("PULSE_SKIP_DOCTOR") != "1":
        try:
            kgctl = Path(__file__).resolve().parent / "kgctl.py"
            if kgctl.exists():
                env = dict(os.environ, KG_REPO=d["repo"])
                p = subprocess.run([sys.executable, str(kgctl), "doctor"],
                                   capture_output=True, text=True, timeout=120, env=env)
                probs = [ln.strip()[2:].strip() for ln in (p.stdout or "").splitlines()
                         if ln.strip().startswith("×")]
                # 预算那一条已在上面单独报过，避免重复
                probs = [x for x in probs if not x.startswith("③")]
                if probs:
                    L.append("【体检发现】")
                    for x in probs[:4]:
                        L.append("  × " + x)
                else:
                    L.append("【体检】无漂移、无矛盾")
            else:
                L.append("【体检】kgctl.py 不在同目录，跳过")
        except Exception as exc:
            L.append(f"【体检】取不到（{type(exc).__name__}）——别把这当成「没问题」")

    if b:
        L.append(f"\n【本窗口预算自检】rollout {b.get('rollout_mb')}MB"
                 f"　累计 {round((b.get('tokens_used') or 0)/1e6, 1)}M token"
                 f"　最近一轮 input {b.get('last_turn_input')}"
                 + ("  ⚠️ 最近一轮 input >120k，本窗口已进入「越长越懒」区间，建议换新窗口接手"
                    if (b.get("last_turn_input") or 0) > 120_000 else ""))
        # 预算越线时直接给出「下一步该敲哪条命令」，不让人去记。
        mb = b.get("rollout_mb") or 0
        tk = b.get("tokens_used") or 0
        if mb > 80 or tk > 150_000_000:
            L.append("　→ 已越轮换线：`python3 "
                     + str(Path(__file__).resolve().parent / "cockpit_rotate.py")
                     + " --automation <名字>`（默认只预演，加 --go 才执行）")
    else:
        # 关键：解析不到线程时**绝不能静默省略**这一段。
        # 少一段的输出与「窗口很健康」长得一模一样，读者会以为没事。
        # 「没有」和「不知道」必须是两句不同的话。
        why = d.get("budget_missing_reason") or "未指定线程（用 --automation <名字> 或 --thread <id>）"
        L.append(f"\n【本窗口预算自检】取不到：{why}"
                 "　—— 这不代表窗口健康，只代表这一项没测。")
    L.append(f"\n（一页生成耗时 {d['elapsed_ms']}ms，全部只读）")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--automation", default="",
                    help="按 automation 名字自动解析目标线程（推荐，轮换后无需改 prompt）")
    ap.add_argument("--repo", default=os.environ.get("KG_TRUNK", DEFAULT_REPO))
    ap.add_argument("--ops", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--thread", default=os.environ.get("PULSE_THREAD", ""))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # 一键关：文件即开关，第一行就能看见（fail-open 的对称操作）
    if OFF_SWITCH.exists():
        print("cockpit-pulse 已关闭（存在 ~/.codex/cockpit-pulse.OFF），请照常人工验收。")
        return 0

    # 线程 id 的解析优先级：--automation 名字 > --thread / 环境变量。
    # 用名字解析是为了让 prompt 里不必硬编码 id（否则轮换后新窗口会监控上一代）。
    if args.automation and not args.thread:
        resolved = resolve_automation_thread(args.automation)
        if resolved:
            args.thread = resolved
        else:
            args.budget_missing_reason = (
                f"按名字 {args.automation} 解析不到目标线程"
                "（automation.toml 不存在，或刚轮换过而 app 尚未重载）")

    try:
        d = build(args)
    except Exception as exc:  # 任何异常都不让心跳挂掉
        print(f"（一页心跳取数失败：{type(exc).__name__}: {exc}；请按老办法手工核对）")
        return 0

    print(json.dumps(d, ensure_ascii=False, indent=2) if args.json else render(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
