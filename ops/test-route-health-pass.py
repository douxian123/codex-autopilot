#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：路线/效能巡检已从查询时计算变成 daemon 每拍只读检测。

这条门禁锁住daemon接线的四个边界：
  1. `route-health` pass 必须存在，且只声明 auto + explicit + reversible；
  2. 摘要只含只读字段，动作只能是 none / propose_review；
  3. 状态签名不变时 append-only 账本不追加，避免每拍打扰用户；
  4. 卡点原始记录优先于面板投影，不能因为 ts 丢失而长期 unknown。
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import pass_registry  # noqa: E402
import route_health_runtime  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def main():
    # 1. 面板与 daemon 共用同一份运行时，不是两套判据。
    mcp = (OPS / "central_mcp.py").read_text(encoding="utf-8")
    daemon = (OPS / "central_daemon.py").read_text(encoding="utf-8")
    check("MCP 委托共享运行时", "route_health_runtime.summarize" in mcp)
    check("daemon 注册 route-health pass", 'Pass("route-health"' in daemon)
    check("巡检只从原始 exec_blocked 取卡点时间",
          '"exec_blocked": ctx.get("exec_blocked")' in daemon)
    check("巡检摘要写入运行态且不抹掉旧摘要",
          'if ctx.get("route_health") is not None:' in daemon
          and 'state["route_health"] = ctx["route_health"]' in daemon)

    route_block = daemon.split('Pass("route-health"', 1)[1].split("reg.register", 1)[0]
    for needle in ("level=MODE_AUTO", "authorization=AUTH_EXPLICIT",
                   "effect=EFFECT_REVERSIBLE"):
        check(f"route-health 声明 {needle}", needle in route_block, route_block)
    for forbidden in ("EFFECT_DESTRUCTIVE", "kill", "restart", "config_change",
                      "write_task"):
        check(f"route-health 不含 {forbidden}", forbidden not in route_block, route_block)

    # pass_registry 的通用门禁之外，再按 route-health 的真实声明锁一次：
    # --mode observe 时连 append-only 账本写入函数都不能被调用。
    calls = []
    reg = pass_registry.Registry()
    reg.register(pass_registry.Pass(
        "route-health", lambda: calls.append("write"),
        level=pass_registry.MODE_AUTO,
        authorization=pass_registry.AUTH_EXPLICIT,
        effect=pass_registry.EFFECT_REVERSIBLE))
    observed = reg.run(override=pass_registry.MODE_OBSERVE)
    check("observe 模式不执行巡检写入", calls == [], calls)
    check("observe 模式仍留下 pass 记录",
          len(observed) == 1 and observed[0].status == "ok", observed)

    # 2. 输出形状必须是只读结论。
    idle = route_health_runtime.summarize({"exec_now": None})
    check("空闲是 ok", idle["level"] == "ok", idle)
    check("空闲不生成提案", idle["action"] == "none", idle)
    allowed_keys = set(idle)
    check("输出没有可执行动作字段",
          allowed_keys == {"status", "level", "headline", "unknown", "reasons",
                           "action", "checked_at"}, sorted(allowed_keys))

    # 多个 worker 任一 attention 就抬 attention，不漏检前面的 worker。
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    high = route_health_runtime._sanitize({
        "level": "attention", "headline": "偏离 mandate", "reasons": ["scope 外改动 1 个"],
        "unknown": [], "action": "propose_review", "checked_at": now.isoformat(),
    })
    calm = route_health_runtime._sanitize({
        "level": "ok", "headline": "无异常", "reasons": [],
        "unknown": [], "action": "none", "checked_at": now.isoformat(),
    })
    orig = route_health_runtime.summarize
    try:
        route_health_runtime.summarize = lambda *a, **k: calm
        many = route_health_runtime.summarize_many(
            [{"exec_now": {"id": "a"}}, {"exec_now": {"id": "b"}}], now=now)
        check("多 worker 全正常为 ok", many["level"] == "ok", many)

        def mixed(payload, *a, **k):
            return high if str((payload.get("exec_now") or {}).get("id")) == "a" else calm

        route_health_runtime.summarize = mixed
        many = route_health_runtime.summarize_many(
            [{"exec_now": {"id": "a"}}, {"exec_now": {"id": "b"}}], now=now)
        check("任一 worker 异常抬 attention", many["level"] == "attention", many)
        check("多 worker 异常只提复核", many["action"] == "propose_review", many)
    finally:
        route_health_runtime.summarize = orig

    # 3. 原始卡点记录有时钟时，能算出真实卡点分钟数，不长期 unknown。
    ts = (now - timedelta(minutes=50)).timestamp()
    minutes = route_health_runtime._stall_minutes_from_blocked(
        {"exec": [{"id": "a", "reason": "worker_stuck", "ts": ts}]}, "a", now=now)
    check("原始卡点时间能算出分钟数", minutes == 50, minutes)
    benign = route_health_runtime._stall_minutes_from_blocked(
        {"exec": [{"id": "a", "reason": "overlap_hold", "ts": ts}]}, "a", now=now)
    check("调度性等待不算偏航", benign == 0, benign)
    stall50 = route_health_runtime._sanitize({
        "level": "attention",
        "headline": "50 分钟耗在与验收无关的卡点",
        "reasons": ["50 分钟耗在与验收无关的卡点"],
        "unknown": [],
        "action": "propose_review",
        "checked_at": now.isoformat(),
    })
    stall51 = route_health_runtime._sanitize({
        "level": "attention",
        "headline": "51 分钟耗在与验收无关的卡点",
        "reasons": ["51 分钟耗在与验收无关的卡点"],
        "unknown": [],
        "action": "propose_review",
        "checked_at": now.isoformat(),
    })
    check("卡点分钟增长不产生新签名",
          route_health_runtime.signature(stall50, "a")
          == route_health_runtime.signature(stall51, "a"))

    # 4. 账本只在签名变化时追加；这就是「不每拍打扰用户」的机器证据。
    import tempfile
    with tempfile.TemporaryDirectory(prefix="route-health-pass-") as tmp:
        runtime = Path(tmp)
        first = route_health_runtime.append_if_changed(runtime, high, exec_id="a", now=now)
        second = route_health_runtime.append_if_changed(runtime, high, exec_id="a", now=now)
        check("首次巡检追加一行", first["written"] is True, first)
        check("同状态不重复追加", second["written"] is False, second)
        # 必须拿 50 分钟那次巡检作基准；high 是另一条 headline，签名不同是正确行为。
        third = route_health_runtime.append_if_changed(
            runtime, stall51,
            prior={"signature": route_health_runtime.signature(stall50, "a")},
            exec_id="a", now=now + timedelta(minutes=1))
        check("卡点分钟增长不重复追加", third["written"] is False, third)
        rows = [json.loads(x) for x in route_health_runtime.ledger_path(runtime).read_text(
            encoding="utf-8").splitlines()]
        check("账本只有一行", len(rows) == 1, len(rows))
        check("账本记录只读动作", rows[0]["action"] == "propose_review", rows[0])
        for forbidden in ("kill", "restart", "patch", "config_change", "write_task"):
            check(f"账本不含 {forbidden}", forbidden not in rows[0], rows[0])

    print("")
    if failures:
        print(f"route_health_pass: {len(failures)} 项未通过")
    else:
        print("route_health_pass: 全部通过")
    return len(failures)


if __name__ == "__main__":
    sys.exit(main())
