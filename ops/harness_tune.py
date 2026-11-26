#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""harness_tune —— 状态管家的「提卡 → 点头才改 → 备份 → 一键回滚」（stdlib only）。

为什么单独抽出来
----------------
`budget_gate.py` 已经每拍测量长驻窗口的上下文健康（峰值 input / 压缩频率 / 用量），
但它**只读**：越线就换代、熔断派活，从不改 harness 配置本身。于是
「压缩线该不该抬、工具输出上限该不该压」仍然要人肉判断、人肉改
`~/.codex/config.toml`、人肉记回滚点 —— 正是自动挡要消灭的那类精力分散。

这一层把它做成一条可回滚的窄通道：

  * `propose`  —— 只读测量 + 出一张卡（追加 `proposals.jsonl`），**绝不碰 config**。
  * `apply`    —— 必须显式 `--confirm`，并且同时满足四条硬约束：
                  1) 只允许白名单里的键（`TUNABLE`），其它键永远不动；
                  2) 新值必须落在安全区间，且相对旧值只做「正向」方向（`DIRECTION`）；
                  3) 先把 config.toml 按字节备份，再**只替换那一行**，原子写；
                  4) 提案时记下的 config 指纹必须仍然匹配 —— 中间有人手改过就作废，
                     绝不用一张旧提案覆盖人的编辑。
  * `rollback` —— 用备份**按字节**还原，还原后重新读回核对。

为什么默认不自动 apply
----------------------
改 harness 配置会影响用户接下来的所有会话，属于「改产品方向」量级的事，按本项目
红线必须由人点头。守护每拍只调 `propose`；`apply` 只从 CLI / MCP 显式触发。
本模块**从不重启、从不杀进程、从不写 config.toml 之外的文件**，所以它不可能影响
用户正在跑的工作进程。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# 可调键 -> (最小值, 最大值)。白名单之外一律拒绝，防止「顺手改个 model」这类事故。
TUNABLE = {
    "model_auto_compact_token_limit": (60_000, 400_000),
    "tool_output_token_limit": (2_000, 40_000),
}

# 每个键允许的「正向」方向。只允许往这个方向调，反方向一律拒绝。
DIRECTION = {
    "model_auto_compact_token_limit": "up",
    "tool_output_token_limit": "down",
}

# 触发用的阈值，和 budget_gate 的 over 线保持一致（单一口径，不另立第二套真相）。
COMPACT_STORM = 3          # 近 2 小时压缩 >= 3 次 = 压缩风暴
PEAK_INPUT_OVER = 165_000  # 单轮峰值 input 越线
TOOL_OUTPUT_LOOSE = 8_000  # 工具输出上限高于这个值才值得往下压
COMPACT_STEP = 1.25        # 抬压缩线的单步倍率
COMPACT_MIN_STEP = 20_000  # 单步至少抬这么多，避免爬得太慢

_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*=")


def runtime() -> Path:
    return Path(os.environ.get("KG_DAEMON_HOME")
                or str(Path.home() / ".codex-autopilot")).expanduser()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME")
                or str(Path.home() / ".codex")).expanduser()


def config_path() -> Path:
    return codex_home() / "config.toml"


def tune_dir() -> Path:
    return runtime() / "harness-tune"


def proposals_path() -> Path:
    return tune_dir() / "proposals.jsonl"


def backups_dir() -> Path:
    return tune_dir() / "backups"


def off_path() -> Path:
    return tune_dir() / "OFF"


def health_path() -> Path:
    return runtime() / "budget-gate.json"


def lock_path() -> Path:
    return tune_dir() / "apply.lock"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{_now()}] {msg}\n"
    try:
        d = runtime()
        d.mkdir(parents=True, exist_ok=True)
        with (d / "harness-tune.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def off() -> tuple:
    try:
        if off_path().exists():
            return True, f"存在开关文件 {off_path()}"
    except OSError:
        pass
    if str(os.environ.get("KG_HARNESS_TUNE", "")).strip().lower() in {"off", "0", "false", "no"}:
        return True, "环境变量 KG_HARNESS_TUNE=off"
    return False, ""


def read_config_bytes():
    try:
        return config_path().read_bytes()
    except OSError:
        return None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def top_level_values(text: str) -> dict:
    """取**顶层**键值对（遇到第一个 `[section]` 就停）。

    只认顶层是必须的：`[mcp_servers.foo]` 下面完全可能有同名键，按全文 grep 会串台。
    """
    out = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if not stripped or stripped.startswith("#"):
            continue
        m = _KEY_RE.match(line)
        if m:
            out[m.group(1)] = line.split("=", 1)[1].strip()
    return out


def parse_int(raw):
    try:
        return int(str(raw).strip().strip('"'))
    except (TypeError, ValueError):
        return None


def worst_health() -> dict:
    """读出体检结论里最差的一条长驻窗口 + 用量。取不到就如实标 unknown。"""
    try:
        data = json.loads(health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "unknown", "reason": "还没有体检记录（守护下一拍会写）"}
    if not isinstance(data, dict):
        return {"status": "unknown", "reason": "体检记录格式不对"}
    report = data.get("last_report") or {}
    rows = [r for r in (report.get("threads") or []) if isinstance(r, dict)]
    if not rows:
        return {"status": "no_data", "reason": "体检还没测到长驻窗口"}
    order = {"ok": 0, "warn": 1, "over": 2}
    worst = max(rows, key=lambda r: order.get(r.get("level"), -1))
    return {"status": "ok", "worst": worst, "usage": report.get("usage") or {},
            "updated": data.get("last_run")}


def rules(values: dict, health: dict) -> list:
    """把体检结论翻译成提案。没有证据就不出卡 —— 宁可不提，也不猜。"""
    if health.get("status") != "ok":
        return []
    worst = health.get("worst") or {}
    out = []

    compact = parse_int(values.get("model_auto_compact_token_limit"))
    compactions = worst.get("compactions_recent") or 0
    if compact is not None and isinstance(compactions, int) and compactions >= COMPACT_STORM:
        lo, hi = TUNABLE["model_auto_compact_token_limit"]
        new = min(hi, max(int(compact * COMPACT_STEP), compact + COMPACT_MIN_STEP))
        if new > compact:
            out.append({
                "key": "model_auto_compact_token_limit",
                "old": compact,
                "new": new,
                "why": (f"{worst.get('automation') or '长驻窗口'} 近 2 小时压缩 "
                        f"{compactions} 次（风暴线 {COMPACT_STORM}）；抬高压缩线减少压缩频率"),
                "evidence": {
                    "compactions_recent": compactions,
                    "peak_input_recent": worst.get("peak_input_recent"),
                    "last_compaction_minutes": worst.get("last_compaction_minutes"),
                },
            })

    tool = parse_int(values.get("tool_output_token_limit"))
    peak = worst.get("peak_input_recent") or 0
    if (tool is not None and tool > TOOL_OUTPUT_LOOSE and peak >= PEAK_INPUT_OVER
            and compactions < COMPACT_STORM):
        lo, hi = TUNABLE["tool_output_token_limit"]
        new = max(lo, int(tool * 0.5))
        if new < tool:
            out.append({
                "key": "tool_output_token_limit",
                "old": tool,
                "new": new,
                "why": (f"单轮峰值 input {peak}（越线 {PEAK_INPUT_OVER}）而压缩不频繁；"
                        f"压低单条工具输出上限，减少每轮无谓上下文"),
                "evidence": {
                    "peak_input_recent": peak,
                    "compactions_recent": compactions,
                    "tool_output_token_limit": tool,
                },
            })
    return out


def allowed_change(key: str, old, new) -> tuple:
    """白名单 + 区间 + 方向，三重校验。任何一条不过就拒绝，并说清是哪条。"""
    if key not in TUNABLE:
        return False, f"键 {key} 不在可调白名单里"
    if old is None or new is None:
        return False, "旧值或新值读不成整数"
    lo, hi = TUNABLE[key]
    if not (lo <= new <= hi):
        return False, f"新值 {new} 落在安全区间 [{lo}, {hi}] 之外"
    direction = DIRECTION[key]
    if direction == "up" and new <= old:
        return False, f"{key} 只允许抬高（{old} → {new} 是反向）"
    if direction == "down" and new >= old:
        return False, f"{key} 只允许压低（{old} → {new} 是反向）"
    return True, ""


def read_rows() -> list:
    p = proposals_path()
    if not p.exists():
        return []
    out = []
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return []
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


def latest_by_id() -> dict:
    latest = {}
    for row in read_rows():
        rid = row.get("id")
        if rid:
            latest[rid] = row
    return latest


def _next_id() -> str:
    nums = []
    for rid in latest_by_id():
        m = re.match(r"^T-(\d+)$", str(rid))
        if m:
            nums.append(int(m.group(1)))
    return "T-%04d" % ((max(nums) + 1) if nums else 1)


def _append(row: dict) -> None:
    d = tune_dir()
    d.mkdir(parents=True, exist_ok=True)
    with proposals_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def replace_top_level_value(text: str, key: str, new_value) -> tuple:
    """把**顶层** `key = ...` 那一行换成新值；只换第一处，其它行原样保留。"""
    lines = text.splitlines(keepends=True)
    in_section = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = True
            continue
        if in_section:
            continue
        m = _KEY_RE.match(line)
        if m and m.group(1) == key:
            eol = "\n" if line.endswith("\n") else ""
            lines[i] = f"{key} = {new_value}{eol}"
            return "".join(lines), True
    return text, False


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _acquire_lock() -> bool:
    d = tune_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path()), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def _release_lock() -> None:
    try:
        lock_path().unlink()
    except OSError:
        pass


def propose() -> dict:
    """只读测量 + 出卡。绝不改 config。"""
    is_off, why = off()
    if is_off:
        return {"status": "off", "reason": why, "created": []}
    raw = read_config_bytes()
    if raw is None:
        return {"status": "unknown", "reason": f"读不到 {config_path()}，不改任何东西",
                "created": []}
    text = raw.decode("utf-8", errors="replace")
    values = top_level_values(text)
    health = worst_health()
    if health.get("status") != "ok":
        return {"status": health.get("status"), "reason": health.get("reason"),
                "created": []}

    live = latest_by_id()
    fingerprint = sha256(raw)
    created = []
    for cand in rules(values, health):
        if cand["old"] != parse_int(values.get(cand["key"])):
            continue
        ok, why = allowed_change(cand["key"], cand["old"], cand["new"])
        if not ok:
            log(f"x 规则产出的提案被拒：{why}")
            continue
        dup = [r for r in live.values()
               if r.get("status") == "proposed" and r.get("key") == cand["key"]
               and r.get("new") == cand["new"] and r.get("config_sha256") == fingerprint]
        if dup:
            log(f"= 已有一张等价的待办提案（{dup[0]['id']}），不重复出卡")
            continue
        row = {
            "id": _next_id(), "ts": _now(), "type": "harness_tune",
            "status": "proposed", "key": cand["key"],
            "old": cand["old"], "new": cand["new"], "why": cand["why"],
            "evidence": cand["evidence"], "config": str(config_path()),
            "config_sha256": fingerprint,
            "rollback_hint": "harness_tune.py rollback <id>",
        }
        _append(row)
        live[row["id"]] = row
        created.append(row)
        log(f"+ 提案 {row['id']}：{cand['key']} {cand['old']} → {cand['new']}（{cand['why']}）")
    return {"status": "ok", "created": created,
            "pending": [r for r in latest_by_id().values() if r.get("status") == "proposed"],
            "updated": health.get("updated")}


def apply(pid: str, confirm: bool = False) -> dict:
    """应用一张卡。没有 --confirm 一律不动手。"""
    rows = latest_by_id()
    row = rows.get(pid)
    if not row:
        return {"status": "error", "reason": f"没有这张卡：{pid}"}
    if row.get("status") != "proposed":
        return {"status": "error", "reason": f"{pid} 当前状态是 {row.get('status')}，不是待办"}
    if not confirm:
        return {"status": "needs_confirm",
                "reason": "这是改 harness 配置，需要你明确点头：--confirm",
                "proposal": row}

    ok, why = allowed_change(row.get("key"), parse_int(row.get("old")), parse_int(row.get("new")))
    if not ok:
        return {"status": "error", "reason": f"提案没通过安全校验：{why}"}

    raw = read_config_bytes()
    if raw is None:
        return {"status": "error", "reason": f"读不到 {config_path()}，不改任何东西"}
    if sha256(raw) != row.get("config_sha256"):
        stale = dict(row)
        stale["status"] = "stale"
        stale["stale_ts"] = _now()
        stale["stale_reason"] = "config.toml 在出卡之后被人改过，这张卡作废（绝不覆盖人的编辑）"
        _append(stale)
        return {"status": "stale", "reason": stale["stale_reason"]}

    if not _acquire_lock():
        return {"status": "busy", "reason": "另一次 apply 正在跑，本次不动手"}
    try:
        text = raw.decode("utf-8", errors="replace")
        new_text, found = replace_top_level_value(text, row["key"], row["new"])
        if not found:
            return {"status": "error",
                    "reason": f"顶层找不到 {row['key']}，拒绝新增键（只改既有键）"}
        backups_dir().mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backups_dir() / f"config.toml.{stamp}.bak"
        backup.write_bytes(raw)
        if sha256(backup.read_bytes()) != sha256(raw):
            return {"status": "error", "reason": "备份回读不一致，放弃写入"}
        _atomic_write_bytes(config_path(), new_text.encode("utf-8"))
        after = read_config_bytes() or b""
        got = parse_int(top_level_values(after.decode("utf-8", errors="replace")).get(row["key"]))
        if got != parse_int(row["new"]):
            _atomic_write_bytes(config_path(), backup.read_bytes())
            return {"status": "error",
                    "reason": f"写入后回读是 {got}，不是 {row['new']}；已按备份还原"}
        done = dict(row)
        done.update({"status": "applied", "applied_ts": _now(),
                     "backup": str(backup), "before": row["old"], "after": row["new"],
                     "rollback": f"harness_tune.py rollback {row['id']}"})
        _append(done)
        log(f"OK {row['id']} 已应用：{row['key']} {row['old']} → {row['new']}；备份 {backup}")
        return {"status": "applied", "proposal": done, "backup": str(backup)}
    finally:
        _release_lock()


def rollback(pid: str) -> dict:
    """按字节还原备份。"""
    rows = latest_by_id()
    row = rows.get(pid)
    if not row:
        return {"status": "error", "reason": f"没有这张卡：{pid}"}
    if row.get("status") != "applied":
        return {"status": "error", "reason": f"{pid} 当前状态是 {row.get('status')}，没得回滚"}
    backup = Path(str(row.get("backup") or ""))
    if not backup.exists():
        return {"status": "error", "reason": f"备份不见了：{backup}，拒绝动 config"}
    if not _acquire_lock():
        return {"status": "busy", "reason": "另一次 apply/rollback 正在跑，本次不动手"}
    try:
        want = backup.read_bytes()
        _atomic_write_bytes(config_path(), want)
        got = read_config_bytes()
        if got != want:
            return {"status": "error", "reason": "还原后回读不一致，请查看备份文件"}
        done = dict(row)
        done.update({"status": "rolled_back", "rolled_back_ts": _now(),
                     "restored_sha256": sha256(want)})
        _append(done)
        log(f"<- {pid} 已回滚到 {backup}")
        return {"status": "rolled_back", "proposal": done}
    finally:
        _release_lock()


def human() -> str:
    pending = [r for r in latest_by_id().values() if r.get("status") == "proposed"]
    if not pending:
        return "状态管家：当前没有待你点头的 harness 调整。"
    out = [f"状态管家：{len(pending)} 张待你点头的调整卡", ""]
    for r in sorted(pending, key=lambda x: x["id"]):
        out.append(f"  {r['id']}  {r['key']}: {r['old']} → {r['new']}")
        out.append(f"        为什么：{r['why']}")
        out.append(f"        点头：python3 ops/harness_tune.py apply {r['id']} --confirm")
        out.append("        不同意：不用管它，config 一个字都不会变")
    return "\n".join(out)


def pending() -> list:
    """给面板/工具层用：当前待你点头的卡（只读）。"""
    return [r for r in latest_by_id().values() if r.get("status") == "proposed"]


def daemon_hook(dry: bool = False, log_fn=None) -> dict:
    """守护每拍调这里。**只出提案，绝不改 config**。异常由调用方吞掉（fail-open）。"""
    res = propose()
    if log_fn:
        created = res.get("created") or []
        if created:
            log_fn(f"[tune] 出了 {len(created)} 张待点头的 harness 调整卡："
                   + "、".join(f"{c['id']} {c['key']} {c['old']}→{c['new']}" for c in created))
        elif res.get("status") in {"unknown", "no_data"}:
            log_fn(f"[tune] 没有体检证据，不出卡（{res.get('reason')}）")
    return res


def _selftest() -> int:
    ok = True

    def chk(cond, msg):
        nonlocal ok
        print(("  OK " if cond else "  FAIL ") + msg)
        ok = ok and bool(cond)

    text = ("a = 1\nmodel_auto_compact_token_limit = 160000\n"
            "[s]\nmodel_auto_compact_token_limit = 1\n")
    new, found = replace_top_level_value(text, "model_auto_compact_token_limit", 200000)
    chk(found, "顶层键能被替换")
    chk("model_auto_compact_token_limit = 200000" in new, "新值写进去了")
    chk(new.endswith("[s]\nmodel_auto_compact_token_limit = 1\n"),
        "section 里的同名键一个字都没动")
    chk(top_level_values(text).get("a") == "1", "顶层取值正常")
    # 关键：顶层取到的是 160000，不是 [s] 里的 1 —— 证明取值在 section 处就停了。
    chk(top_level_values(text).get("model_auto_compact_token_limit") == "160000",
        "section 里的同名键不覆盖顶层取值")
    chk(allowed_change("model_auto_compact_token_limit", 160000, 200000)[0], "抬高压缩线放行")
    chk(not allowed_change("model_auto_compact_token_limit", 200000, 160000)[0], "压低压缩线拒绝")
    chk(not allowed_change("tool_output_token_limit", 6000, 9000)[0], "抬高工具上限拒绝")
    chk(allowed_change("tool_output_token_limit", 12000, 6000)[0], "压低工具上限放行")
    chk(not allowed_change("model", 1, 2)[0], "白名单外的键拒绝")
    chk(not allowed_change("tool_output_token_limit", 12000, 100)[0], "越出安全区间拒绝")
    print("自检", "通过" if ok else "失败")
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="状态管家：harness 配置的提卡 / 点头应用 / 回滚")
    ap.add_argument("cmd", nargs="?", default="list",
                    choices=["propose", "list", "apply", "rollback"])
    ap.add_argument("id", nargs="?", default="")
    ap.add_argument("--confirm", action="store_true", help="应用一张卡时必须显式给")
    ap.add_argument("--json", action="store_true", help="打印结构化结果")
    ap.add_argument("--selftest", action="store_true", help="纯内存自检（不碰任何文件）")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.cmd == "propose":
        res = propose()
    elif args.cmd == "apply":
        if not args.id:
            print("用法: harness_tune.py apply <T-id> --confirm")
            return 2
        res = apply(args.id, confirm=args.confirm)
    elif args.cmd == "rollback":
        if not args.id:
            print("用法: harness_tune.py rollback <T-id>")
            return 2
        res = rollback(args.id)
    else:
        res = {"status": "ok"}

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(human())
        if res.get("status") not in {None, "ok"}:
            print()
            print(f"[{res.get('status')}] {res.get('reason') or res.get('status')}")
    return 2 if res.get("status") == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
