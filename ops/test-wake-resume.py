#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：timer 唤醒与跨工单恢复必须可审计、幂等、fail-closed。

锁住的是失败路径，不是「文件存在」：
  * inferred 授权不能排唤醒；
  * 旧 worker 还活着时不能伪造跨工单恢复；
  * 到期投递成功后重复 fire 不再打扰唯一入口；
  * 投递失败有退避和次数上限；
  * 坏账本不能当成「没有到期任务」；
  * daemon 的 wake pass 只写 inbox，不启动 worker、不直接派发。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"{label} {detail}")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def base_spec(**overrides) -> dict:
    spec = {
        "kind": "wake",
        "ticket_id": "L-9001",
        "due_at": 100.0,
        "text": "到点继续处理这个工单",
        "reason": "用户设置了提醒",
        "authorization": "explicit",
    }
    spec.update(overrides)
    return spec


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wake-resume-test-") as raw:
        tmp = Path(raw)
        runtime = tmp / "runtime"
        wake_root = tmp / "wake"
        life_root = tmp / "lifecycle"
        for path in (runtime, wake_root, life_root):
            path.mkdir(parents=True, exist_ok=True)
        os.environ["KG_DAEMON_HOME"] = str(runtime)
        os.environ["KG_WAKE_RESUME_ROOT"] = str(wake_root)
        os.environ["KG_WORKER_LIFECYCLE_ROOT"] = str(life_root)
        os.environ["KG_WAKE_RETRY_SECONDS"] = "7"
        os.environ["KG_WAKE_MAX_ATTEMPTS"] = "2"

        wl = load_module("worker_lifecycle_under_wake_test",
                         OPS / "worker_lifecycle.py")
        wake = load_module("wake_resume_under_test", OPS / "wake_resume.py")
        daemon = load_module("central_daemon_under_wake_test",
                             OPS / "central_daemon.py")
        service = wake.WakeResume(runtime)

        try:
            wake.normalize_spec(base_spec(authorization="inferred"))
            raise AssertionError("inferred 授权不应通过")
        except wake.AuthorizationError:
            check("inferred 授权被拒绝", True)

        # 旧 worker 还在 running 时，跨工单恢复必须走 lifecycle.resume。
        workers = wl.WorkerLifecycle(life_root)
        workers.start({
            "worker_id": "exec-L-OLD",
            "ticket_id": "L-OLD",
            "lane": "arch",
            "command": ["/bin/true"],
            "resources": [],
            "tools": [],
            "authorization": "explicit",
            "effect": "reversible",
            "authorization_reason": "test",
        }, launch=lambda: 4242)
        try:
            service.schedule(base_spec(
                kind="resume", ticket_id="L-NEW", resume_from="L-OLD",
                lifecycle_worker_id="exec-L-OLD", due_at=1.0))
            raise AssertionError("活跃 worker 不应允许跨工单恢复")
        except wake.ConflictError:
            check("活跃旧 worker 的恢复被拒绝", True)

        projection = service.schedule(base_spec(request_id="wake-once"))
        check("首次排程为 scheduled", projection["state"] == "scheduled",
              str(projection))
        repeated = service.schedule(base_spec(request_id="wake-once"))
        check("同 spec 重复排程幂等",
              repeated["event_count"] == projection["event_count"],
              f"{projection['event_count']} -> {repeated['event_count']}")

        not_due = service.due(now=99.0)
        check("未到点不唤醒", not_due == [], str(not_due))
        due = service.due(now=100.0)
        check("到点可见", [p["request_id"] for p in due] == ["wake-once"],
              str(due))

        delivered: list[str] = []

        def deliver(spec, _current):
            delivered.append(spec["request_id"])
            return "inbox#1"

        fired = service.fire("wake-once", deliver, now=100.0)
        check("投递后 delivered", fired["state"] == "delivered", str(fired))
        check("只投递一次", delivered == ["wake-once"], str(delivered))
        try:
            service.fire("wake-once", deliver, now=200.0)
            raise AssertionError("已送达的请求不应再次触发")
        except wake.ConflictError:
            check("重复 fire 被拒绝", True)
        check("重复 fire 没有再次投递", delivered == ["wake-once"],
              str(delivered))

        service.schedule(base_spec(request_id="wake-retry"))
        attempts: list[int] = []

        def broken_deliver(_spec, _current):
            attempts.append(1)
            raise RuntimeError("inbox unavailable")

        failed = service.fire("wake-retry", broken_deliver, now=100.0)
        check("失败落到 failed", failed["state"] == "failed", str(failed))
        check("失败写退避时间", failed["next_retry_at"] == 107.0,
              str(failed))
        check("失败未到点不重试", service.due(now=106.9) == [])
        check("退避到点可重试",
              [p["request_id"] for p in service.due(now=107.0)]
              == ["wake-retry"])
        service.fire("wake-retry", broken_deliver, now=107.0)
        check("超过上限不再重试", service.due(now=10_000.0) == [])

        bad = wake_root / "wake-bad.jsonl"
        bad.write_text("{not-json}\n", encoding="utf-8")
        try:
            service.due(now=10_000.0)
            raise AssertionError("坏账本不能静默当空")
        except wake.LedgerCorrupt:
            check("坏账本 fail-closed", True)
        # 坏账本夹具只验证 fail-closed；清掉后才能继续验证 daemon pass，
        # 否则 daemon 会因账本损坏正确地拒绝扫描，测试红灯不是实现问题。
        bad.unlink()

        source = (OPS / "central_daemon.py").read_text(encoding="utf-8")
        check("wake pass 在 route 之前注册",
              source.index('Pass("wake"') < source.index('Pass("route"'))
        check("wake pass 只走 inbox 投递",
              "wake_resume.deliver_to_inbox" in source)
        check("wake pass 不直接派 worker",
              "_pass_wake" in source and "_pass_exec" in source
              and "_pass_wake(ctx)" not in source.split("def _pass_exec")[0])

        # daemon 用真实时钟扫到期项；单独排一条必然到期的请求。
        service.schedule(base_spec(request_id="wake-pass", due_at=0.0))
        delivered_inbox: list[str] = []
        daemon._deliver_wake_to_inbox = (
            lambda text, spec: delivered_inbox.append(spec["request_id"])
            or "inbox#test")
        context = {"dry": False, "wake": {}}
        result = daemon._pass_wake(context)
        check("wake pass 返回投递结果",
              result.get("fired") == 1, str(result))
        check("wake pass 只写唯一入口",
              delivered_inbox == ["wake-pass"], str(delivered_inbox))

    print("wake resume: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
