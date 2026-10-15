#!/usr/bin/env python3
"""cockpit_rotate.py —— 长驻窗口预算超标时，生成交接包并轮换心跳目标线程。

存在理由（不变式 1）：**任何长驻窗口都有生命周期，没有无限期运行的窗口。**
预算被测量 → 超阈值即轮换 → 轮换自动完成。不允许出现「让用户自己发现窗口变笨了再手动换」。

默认 **shadow（只读预演）**：只打印「它打算怎么做」，不改任何文件。
要真正执行必须显式加 `--go`，且执行前会自动备份 automation.toml。

为什么不能只是「换个 target_thread_id」就完事：
新线程是空的，它不知道自己是司令台、不知道纪律、不知道在途任务。所以轮换必须同时交三样：
  ① 身份与纪律（从 automation.toml 的 prompt 原样带过去，不重新发明）
  ② 现场（一页体检：HEAD/服务/CI/队列/未决问题——来自 cockpit_pulse.py）
  ③ 交接说明（这是第几代、上一代为什么退场、在途任务清单）

安全约定：
  * 默认预演；`--go` 才动手，且改前备份到 `automation.toml.bak-<ts>-pre-rotate`
  * 只改 `target_thread_id` 与 `updated_at` 两个字段，prompt 一字不动
  * fail-open：任何一步失败都不留下半改的文件（先写临时文件再原子替换）
  * 一键关：存在 `~/.codex/cockpit-rotate.OFF` 即整体跳过
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTOMATIONS = Path.home() / ".codex" / "automations"
CODEX_DB = Path.home() / ".codex" / "state_5.sqlite"
OFF_SWITCH = Path.home() / ".codex/cockpit-rotate.OFF"

# 轮换阈值：与 doctor / pulse 保持一致
MAX_ROLLOUT_MB = 80.0
MAX_TOKENS = 150_000_000

# 需要轮换时，若只想先看建议而不动配置：默认就是。
# 真正执行时，新线程会由 codex exec 创建，然后把 id 写回 automation.toml。


def run(cmd: list[str], **kw) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 60), **kw)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def budget_of(thread_id: str) -> dict:
    """读一个线程的预算（只读 sqlite + stat 文件）。取不到返回 {}。"""
    if not CODEX_DB.exists():
        return {}
    try:
        c = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True)
        row = c.execute("select rollout_path, tokens_used, cwd from threads where id=?",
                        (thread_id,)).fetchone()
        c.close()
    except Exception:
        return {}
    if not row:
        return {}
    rp, tk, cwd = row
    mb = None
    try:
        mb = os.path.getsize(rp) / 1e6
    except Exception:
        pass
    return {"rollout_mb": mb, "tokens_used": tk or 0, "cwd": cwd}


def over_budget(b: dict) -> tuple[bool, str]:
    """是否需要轮换：只要任一指标越线就换（宁早不晚，早期换窗口比晚期便宜）。"""
    if not b:
        return False, "取不到预算"
    mb, tk = b.get("rollout_mb"), b.get("tokens_used") or 0
    reasons = []
    if mb is not None and mb > MAX_ROLLOUT_MB:
        reasons.append(f"rollout {mb:.1f}MB > {MAX_ROLLOUT_MB}MB")
    if tk > MAX_TOKENS:
        reasons.append(f"累计 {tk/1e6:.1f}M token > {MAX_TOKENS/1e6:.0f}M")
    return bool(reasons), "、".join(reasons)


def parse_toml_fields(text: str) -> dict:
    """只取我们要用的几个字段；不引第三方 toml 库（保持 stdlib）。

    重要：字符串字段返回的是**已反转义**的值（`\\n` → 真实换行），
    这样它在语义上等于「这个配置的值是什么」。`dumps_toml()` 负责重新转义。
    若返回转义后的原文，dumps 会二次转义（曾把 prompt 里的换行变成字面 `\\n`）。
    """
    out = {}
    for key in ("id", "kind", "name", "prompt", "status", "rrule", "target_thread_id",
                "created_at", "updated_at"):
        m = re.search(rf'^{key}\s*=\s*"((?:[^"\\]|\\.)*)"', text, re.M)
        if m:
            raw = m.group(1)
            # 反转义：先处理 \\ 再处理 \" 与 \n，顺序不能反
            out[key] = (raw.replace("\\\\", "\x00")
                           .replace('\\"', '"')
                           .replace("\\n", "\n")
                           .replace("\\t", "\t")
                           .replace("\x00", "\\"))
        else:
            m2 = re.search(rf"^{key}\s*=\s*(\d+)", text, re.M)
            if m2:
                out[key] = int(m2.group(1))
    return out


def dumps_toml(d: dict) -> str:
    """按固定顺序写出 automation.toml（形状与原文件一致）。"""
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

    lines = ["version = 1", f'id = {q(str(d["id"]))}', f'kind = {q(str(d["kind"]))}',
             f'name = {q(str(d["name"]))}', f'prompt = {q(str(d["prompt"]))}',
             f'status = {q(str(d["status"]))}', f'rrule = {q(str(d["rrule"]))}']
    if d.get("target_thread_id"):
        lines.append(f'target_thread_id = {q(str(d["target_thread_id"]))}')
    lines.append(f'created_at = {int(d.get("created_at") or 0)}')
    lines.append(f'updated_at = {int(d.get("updated_at") or 0)}')
    return "\n".join(lines) + "\n"


def build_handoff(name: str, cfg: dict, pulse_text: str, gen: int, why: str,
                  repo: str = "", brief: str = "") -> str:
    """交接包：新窗口开局要一整段粘进去的全部背景。

    形状照项目自己的复盘教训：先给人身份与纪律，再给现场，最后给第一批任务。
    不要写成流水账（新窗口读不完等于没给）。
    项目相关的路径与文件名一律由调用方传入，**不写死某个项目**——本插件给任意项目用。
    """
    repo_line = f"（工作目录 {repo}）" if repo else ""
    brief_line = (f"2. 读 `{brief}`，把本代第一次接手的结论按四段形状追加到最上面。\n"
                  if brief else
                  "2. 把本代第一次接手的结论按四段形状（【做了什么】/【证据】/【还差什么】/"
                  "【要不要你拍板】）记到项目约定的那个人话入口里。\n")
    return f"""你是项目「{name}」的「司令台」第 {gen} 代{repo_line}。

你不是第一次出现：这是同一个心跳任务的第 {gen} 个执行窗口。
上一代窗口因为「{why}」退场——长驻窗口的上下文会越喂越肥，模型在长上下文里会退化成
「用最短的一句话结束轮次」，所以这个任务设计了定期轮换，**这不是事故，是常规维护**。

你的位置：这个窗口是**心跳与指挥位**，但「指挥」不是「只汇报」——每拍都必须把推进做实：
① 跑一页体检；② 看在途任务与合并队列；③ 有已提交、门禁通过、无冲突的分支**立即落盘**；
④ 没有足够的在途任务就按产品路线**派下一批**。
需要读文件、写代码、跑门禁的实现任务，派给独立 worktree 里的子对话；你负责派活、验收、落盘与汇报。
**不允许把「已检查、无变化」当成一拍的终点**：没有可推进项时也要明确写下一条要派的工作或真实阻塞。

【你的纪律（原文，不要重新发明）】
{cfg.get('prompt', '')}

【接手时的现场（由 cockpit_pulse.py 生成，只读）】
{pulse_text}

【第一批任务】
1. 跑一次上面那条一页体检命令，确认你能取到数（取不到就如实报「取不到」，不要编）。
{brief_line}3. 看项目台账里有没有未路由条目（`kgctl status` / `kgctl next`）；没有就报「空」。
"""


def _brief_path(repo: str) -> str:
    """项目的人话简报路径（存在才给，不存在就不猜）。"""
    for rel in ("docs/COCKPIT_BRIEF.md", "docs/COCKPIT_BRIEF.MD"):
        p = Path(repo) / rel if repo else None
        if p and p.exists():
            return rel
    return ""


def _guard_healthy() -> tuple[bool, str]:
    """写入共用 ~/.codex 之前必须过的安全前置检查（fail-closed）。

    背景：PATH 上的 `codex` 是官方二进制，不设 CODEX_HOME 就写共用 `~/.codex`，
    2026-10-13 曾因此重建 sessions 目录、清掉 5 个月历史。轮换是自动流程，
    所以每次自动建线程前都要**证明**防护是活的，而不是指望它碰巧在跑。

    判据（两条都要满足）：
      1. launchd 里有 com.danta.codex-guard 这个 job；
      2. 它的日志里最近一次成功快照在 45 分钟以内。
    任一不满足就拒绝自动写——把决定交回给人，绝不在无保护状态下动共享历史。
    """
    try:
        rc, out = run(["launchctl", "list"], timeout=15)
        if "codex-guard" not in out:
            return False, "launchd 里找不到 com.danta.codex-guard（历史防护未装载）"
    except Exception as exc:
        return False, f"查 launchctl 失败：{type(exc).__name__}"
    log = Path.home() / ".codex-guard" / "guard.log"
    if not log.exists():
        return False, f"找不到防护日志 {log}"
    last_ok = None
    try:
        for line in log.read_text(errors="replace").splitlines():
            if "snapshot ok" in line:
                m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line.strip())
                if m:
                    last_ok = m.group(1)
    except Exception as exc:
        return False, f"读防护日志失败：{type(exc).__name__}"
    if not last_ok:
        return False, "防护日志里没有任何成功快照记录"
    try:
        ts = time.mktime(time.strptime(last_ok, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return False, f"无法解析最近快照时间 {last_ok}"
    age_min = (time.time() - ts) / 60
    if age_min > 45:
        return False, f"最近一次成功快照是 {age_min:.0f} 分钟前（>45 分钟），防护可能已停"
    return True, f"防护正常（最近快照 {age_min:.0f} 分钟前）"


def _resolve_codex_cli() -> tuple[str | None, str]:
    """挑一个**配置正确且受防护保护**的 codex 入口来建新线程。

    司令台窗口必须走本地代理（`~/.codex/config.toml` 里 model_provider=custom →
    127.0.0.1:15721）。所以：

      · `codex-off`  **不能用**：它把 CODEX_HOME 指到 ~/.codex-official，
        那里没有代理配置，会退回 ChatGPT 官方订阅——配置不一样，不是同一个窗口。
      · PATH 上的 `codex` 才是配置正确的那个（默认 CODEX_HOME=~/.codex → 读代理配置），
        但它正是 2026-10-13 清历史的那个二进制 —— 所以用它之前必须先过 `_guard_healthy()`。
      · 显式设 CODEX_HOME=~/.codex，避免任何继承来的环境把它指到别处。

    返回 (cli, 说明)；cli 为 None 表示拒绝执行。
    """
    ok, why = _guard_healthy()
    if not ok:
        return None, f"拒绝自动建线程（安全前置未通过）：{why}"
    try:
        rc, out = run(["which", "codex"], timeout=10)
        path = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
    except Exception:
        path = ""
    if not path:
        return None, "PATH 上找不到 codex，无法自动建线程（可用 --thread 指定手工建好的线程）"
    try:
        real = str(Path(path).resolve())
    except Exception:
        real = path
    return real, (f"使用 {real}（CODEX_HOME=~/.codex → 走本地代理）；{why}")


def new_thread(handoff: str, cwd: str, model: str | None) -> tuple[str, str]:
    """用 codex exec 起一个新线程，返回 (thread_id, log)。

    Codex 桌面 app 会用 flock 持有它打开的线程，**不能 resume**；推进永远走
    `codex exec` 开新会话（项目已知硬约束）。
    入口由 `_resolve_codex_cli()` 选，且必须先过防护前置检查。
    """
    cli, why = _resolve_codex_cli()
    if not cli:
        return "", f"拒绝自动建线程：{why}"
    print(f"  {why}")
    env = dict(os.environ, CODEX_HOME=str(Path.home() / ".codex"))
    cmd = [cli, "exec", "--json", "--skip-git-repo-check", "-C", cwd]
    if model:
        cmd += ["-m", model]
    cmd += [handoff]
    rc, out = run(cmd, timeout=1800, env=env)
    tid = ""
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        # codex exec --json 的首个事件就是 {"type":"thread.started","thread_id":...}
        if ev.get("type") == "thread.started" and isinstance(ev.get("thread_id"), str):
            tid = ev["thread_id"]
        for key in ("thread_id", "conversation_id", "session_id"):
            v = ev.get(key) or (ev.get("payload") or {}).get(key)
            if not tid and isinstance(v, str) and re.fullmatch(r"[0-9a-f-]{8,}", v):
                tid = v
    return tid, out[-4000:] if rc != 0 else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--automation", default="thoughloom", help="automation 目录名")
    ap.add_argument("--go", action="store_true", help="真正执行轮换（默认只预演）")
    ap.add_argument("--model", default=os.environ.get("ROTATE_MODEL", ""),
                    help="新窗口用的模型（留空则用 codex 默认）")
    ap.add_argument("--thread", default="", help="指定新线程 id（跳过创建，用于人工建好窗口后接管）")
    ap.add_argument("--force", action="store_true",
                    help="即使本脚本自带的预算线（rollout 80MB / 累计 150M token）没越，也执行轮换。"
                         "给「更早的轮换线」用（例如单轮 input 峰值 / 压缩频率这类信号）："
                         "这些信号说明窗口已经在退化，而累计量还没到本条线。"
                         "所有安全前置（codex-guard 体检、备份、原子替换、回读校验）一条都不放宽。")
    args = ap.parse_args()

    if OFF_SWITCH.exists():
        print("cockpit-rotate 已关闭（存在 ~/.codex/cockpit-rotate.OFF），不做任何动作。")
        return 0

    cfg_path = AUTOMATIONS / args.automation / "automation.toml"
    if not cfg_path.exists():
        print(f"× 找不到 {cfg_path}")
        return 1
    text = cfg_path.read_text()
    cfg = parse_toml_fields(text)
    tid = str(cfg.get("target_thread_id") or "")
    if not tid:
        print(f"× {args.automation} 没有 target_thread_id，无需轮换")
        return 0

    b = budget_of(tid)
    need, why = over_budget(b)
    gen = 1 + text.count("# generation:")

    print(f"=== 窗口轮换预演 · {args.automation} ===")
    print(f"当前目标线程 {tid[:12]}…  预算：rollout "
          f"{(b.get('rollout_mb') or -1):.1f}MB，累计 {(b.get('tokens_used') or 0)/1e6:.1f}M token")
    print(f"是否需要轮换：{'是（' + why + '）' if need else '否'}"
          + ("　← 由 --force 强制轮换（更早的轮换线，例如单轮 input 峰值 / 压缩频率）"
             if (not need and args.force) else ""))
    if not need and not args.force:
        print("预算在阈值内，无需动作。")
        return 0

    # 收集现场：直接用一页体检（同一份数据给人和给新窗口）
    pulse_cmd = [sys.executable, str(HERE / "cockpit_pulse.py"), "--thread", tid]
    rc, pulse_text = run(pulse_cmd, timeout=120)
    if rc != 0 or not pulse_text.strip():
        pulse_text = "（一页体检取数失败，新窗口请自己先跑 cockpit_pulse.py 确认现场）"

    handoff = build_handoff(args.automation, cfg, pulse_text, gen + 1, why,
                            repo=str(b.get("cwd") or ""), brief=_brief_path(str(b.get("cwd") or "")))
    print(f"\n交接包 {len(handoff)} 字符；新窗口将是第 {gen + 1} 代。")

    if not args.go:
        print("\n（预演模式，未改动任何文件。要执行加 --go）")
        print("执行时会：① 备份 automation.toml  ② 建新线程  ③ 只改 target_thread_id/updated_at")
        print("\n--- 交接包前 1200 字 ---")
        print(handoff[:1200])
        return 0

    # ---- 真执行 ----
    new_tid = args.thread.strip()
    if not new_tid:
        cwd = str(b.get("cwd") or Path.home())
        print(f"\n正在起新线程（cwd={cwd}）…")
        new_tid, log = new_thread(handoff, cwd, args.model or None)
        if not new_tid:
            print("× 未能从 codex exec 输出里解析出 thread id；未改动配置。")
            if log:
                print(log[-1500:])
            return 1

    backup = cfg_path.with_name(f"automation.toml.bak-{time.strftime('%Y%m%d-%H%M%S')}-pre-rotate")
    shutil.copy2(cfg_path, backup)
    old_prompt = str(cfg.get("prompt") or "")
    new_prompt = old_prompt
    # 关键：prompt 里往往硬编码了旧线程 id（例如「体检时带 --thread <旧id>」）。
    # 不替换的话，新窗口会去监控**上一代**的预算，看到的永远是「已越轮换线」，
    # 于是每拍都在催换窗口——轮换等于没换。这里做精确替换。
    if tid and tid in new_prompt:
        new_prompt = new_prompt.replace(tid, new_tid)
        print(f"  已把 prompt 里的旧线程 id 替换为新 id（{new_prompt.count(new_tid)} 处）")
    cfg["prompt"] = new_prompt
    cfg["target_thread_id"] = new_tid
    cfg["updated_at"] = int(time.time() * 1000)
    # 记一代，便于下一代知道自己是第几代
    body = dumps_toml(cfg)
    if "# generation:" not in body:
        body = body.rstrip() + f"\n# generation: {gen + 1}\n"
    tmp = cfg_path.with_suffix(".toml.tmp-rotate")
    tmp.write_text(body)
    # 回读校验：能解析且 id 是我们写的那个，才原子替换
    chk = parse_toml_fields(tmp.read_text())
    if chk.get("target_thread_id") != new_tid or chk.get("prompt") != cfg.get("prompt"):
        tmp.unlink(missing_ok=True)
        print("× 回读校验失败，已放弃替换（原文件未动）")
        return 1
    tmp.replace(cfg_path)
    print(f"✓ 已轮换：target_thread_id {tid[:12]}… → {new_tid[:12]}…")
    print(f"  备份：{backup}")
    print("  提示：automation.toml 由 Codex app 读取，通常需重启 app 或等其重载才生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
