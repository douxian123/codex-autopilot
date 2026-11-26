#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：路线与效能健康只读、诚实、只提复核，不自动纠偏。

用户新增的诉求是：实时发现路线跑偏、是否耗在与预期无关的卡点、效用比是否最优。
这类判断天然容易越过边界变成「自动改路线 / 杀进程 / 改 harness 配置」，所以门禁
锁住四件事：
  1. 判据缺失 -> unknown，绝不当成 ok；
  2. scope 偏离、无关卡点、重试空转都能抬 attention；
  3. 输出只有 propose_review，没有可执行动作；
  4. 正常状态不能因为正常重试计数或 overlap 排队而误报。

退出码 = 未通过项数。
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import route_health  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def healthy(**over):
    base = {
        "mandate_scope_ok": True,
        "off_scope_changes": 0,
        "unrelated_stall_minutes": 0,
        "retry_count": 0,
        "progress_events": 1,
    }
    base.update(over)
    return base


def main():
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)

    blank = route_health.evaluate({}, now=now)
    check("判据全缺时 unknown", blank["level"] == "unknown", blank)
    check("unknown 不写 action", blank["action"] == "none", blank)
    check("unknown 不冒充正常", "取不到" in blank["headline"], blank["headline"])

    ok = route_health.evaluate(healthy(), now=now)
    check("判据齐全且无异常为 ok", ok["level"] == "ok", ok)
    check("ok 不生成复核提案", ok["action"] == "none", ok)

    idle = route_health.evaluate({"idle": True}, now=now)
    check("没有在跑的实现时是明确的空闲 ok", idle["level"] == "ok", idle)
    check("空闲状态不生成复核提案", idle["action"] == "none", idle)

    drift = route_health.evaluate(healthy(mandate_scope_ok=False,
                                           off_scope_changes=1), now=now)
    check("scope 偏离抬 attention", drift["level"] == "attention", drift)
    check("偏离只提复核不改路线", drift["action"] == "propose_review", drift)

    stall = route_health.evaluate(healthy(unrelated_stall_minutes=60), now=now)
    check("无关卡点超时抬 attention", stall["level"] == "attention", stall)
    check("卡点说清分钟数",
          "60 分钟" in stall["headline"], stall["headline"])

    spin = route_health.evaluate(healthy(retry_count=4, progress_events=0), now=now)
    check("重试空转抬 attention", spin["level"] == "attention", spin)
    check("重试空转说清无推进",
          "有效推进为 0" in " ".join(spin["reasons"]), spin["reasons"])

    # 只低于阈值不能抬成 attention：避免正常重试变成狼来了。
    normal_retry = route_health.evaluate(healthy(retry_count=2), now=now)
    check("阈值内重试不报警", normal_retry["level"] == "ok", normal_retry)

    # 检测器本身绝不允许出现写动作字段；这是状态管家不能越权的机器证据。
    for result in (blank, ok, drift, stall, spin):
        if any(k in result for k in ("kill", "restart", "write", "patch", "config_change")):
            failures.append("输出含越权动作字段")
            print("  FAIL 输出含越权动作字段")
            break
    else:
        print("  ok   输出不含杀进程/改配置/写台账动作")

    print("")
    if failures:
        print(f"route_health: {len(failures)} 项未通过")
    else:
        print("route_health: 全部通过")
    return len(failures)


if __name__ == "__main__":
    sys.exit(main())
