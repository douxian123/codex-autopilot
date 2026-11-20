#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：worker 生命周期必须是可唤醒、可恢复、可审计的硬契约。

这里锁住的不是「函数存在」，而是失败路径：
  * 重复 start 不能开出第二个 session；
  * stop / handback / resume 必须遵守状态机；
  * inferred + destructive 不能启动 worker；
  * 账本损坏必须报错，绝不能投影成 absent；
  * 内部事件不能从外部伪造；
  * CLI 真能落账并返回状态。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
MODULE = OPS / "worker_lifecycle.py"


def load_module():
    spec = importlib.util.spec_from_file_location("worker_lifecycle_under_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"{label} {detail}")


def raises(label: str, exc_type, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        print(f"  ok   {label} ({type(exc).__name__}: {exc})")
        return
    raise AssertionError(f"{label} 没有抛出 {exc_type.__name__}")


def sample(worker_id: str = "w1", **overrides) -> dict:
    spec = {
        "worker_id": worker_id,
        "ticket_id": "L-0001",
        "lane": "arch",
        "worktree": "/tmp/wt-w1",
        "command": ["python3", "-m", "pytest"],
        "resources": ["ops/central_daemon.py"],
        "tools": ["read", "write"],
        "authorization": "explicit",
        "effect": "reversible",
        "authorization_reason": "用户已打开自动实现",
        "expected_evidence": "python3 ops/selftest.py exit 0",
    }
    spec.update(overrides)
    return spec


def main() -> int:
    mod = load_module()
    with tempfile.TemporaryDirectory(prefix="worker-lifecycle-test-") as tmp:
        root = Path(tmp)
        service = mod.WorkerLifecycle(root)

        # 1. 正常生命周期：start -> running -> progress -> stop -> resumed -> handback
        started = service.start(sample(), launch=lambda: "session-1")
        check("start 落到 running", started["state"] == "running", started["state"])
        check("start 生成 generation=1", started["generation"] == 1, str(started))
        check("start 记录外部引用", started["external_ref"] == "session-1")

        idem = service.start(sample(), launch=lambda: "session-2")
        check("重复 start 幂等", idem["state"] == "running" and idem["event_count"] == 2,
              str(idem))
        check("重复 start 不换外部引用", idem["external_ref"] == "session-1")

        progressed = service.append("w1", "progress", {"note": "研究中"})
        check("running 可记 progress", progressed["event_count"] == 3)

        stopping = service.stop("w1", "用户改了需求")
        check("stop 进入 stopping", stopping["state"] == "stopping", stopping["state"])
        stopped = service.append("w1", "worker.stopped", {"reason": "已收尾"})
        check("stopped 是停态", stopped["state"] == "stopped", stopped["state"])

        resumed = service.resume("w1", "新需求已确认", launch=lambda: "session-2")
        check("resume 回到 running", resumed["state"] == "running", resumed["state"])
        check("resume 升 generation", resumed["generation"] == 2, str(resumed))
        check("resume 记录原因", resumed["resume_reason"] == "新需求已确认")
        check("resume 换外部引用", resumed["external_ref"] == "session-2")

        handed = service.handback("w1", "超出本工单范围", target="route")
        check("handback 是终态", handed["state"] == "handed_back", handed["state"])
        check("handback 记录目标", handed["handback_target"] == "route")
        raises("终态不能 resume", mod.ConflictError, service.resume, "w1", "再来")
        raises("终态不能 start", mod.ConflictError, service.start, sample())

        # 2. 启动失败必须落账，不能留下 starting 黑洞
        def boom():
            raise RuntimeError("spawn failed")

        failed = service.start(sample("w2"), launch=boom)
        check("启动失败落 failed", failed["state"] == "failed", failed["state"])
        check("启动失败保留原因", "spawn failed" in failed["error"], failed["error"])
        raises("failed 不能记 progress", mod.ConflictError,
               service.append, "w2", "progress", {})

        # 3. 授权禁行：inferred + destructive 与无理由 destructive 都拒绝
        raises("inferred+destructive 禁行", mod.AuthorizationError,
               service.start, sample("w3", authorization="inferred",
                                     effect="destructive",
                                     authorization_reason=""))
        raises("destructive 无理由禁行", mod.AuthorizationError,
               service.start, sample("w4", effect="destructive",
                                     authorization_reason=""))
        explicit_destructive = service.start(
            sample("w5", effect="destructive",
                   authorization_reason="用户明确授权删除临时现场"))
        check("explicit+destructive 有理由可执行",
              explicit_destructive["state"] == "running")

        # 3b. 资源 / 工具白名单：工单声明之外的请求必须在 Popen 之前禁行。
        raises("越权 resource 禁行", mod.AuthorizationError,
               service.start, sample("w6", allowed_resources=["ops/a.txt"],
                                     resources=["ops/secret.txt"]))
        raises("越权 tool 禁行", mod.AuthorizationError,
               service.start, sample("w7", allowed_tools=["read"],
                                     tools=["write"]))
        scoped = service.start(sample("w8", allowed_resources=["ops/a.txt"],
                                      resources=["ops/a.txt"],
                                      allowed_tools=["read"], tools=["read"]))
        check("白名单内请求可执行", scoped["state"] == "running",
              scoped["state"])

        # 4. 状态机与账本完整性
        raises("非法 worker_id", mod.LifecycleError,
               service.start, sample("../escape"))
        raises("空 reason 不能 stop", mod.LifecycleError,
               service.stop, "w1", "  ")
        raises("空 reason 不能 handback", mod.LifecycleError,
               service.handback, "w1", "  ")
        raises("空 reason 不能 resume", mod.LifecycleError,
               service.resume, "w1", "  ")
        raises("内部事件不能伪造", mod.LifecycleError,
               service.append, "w1", "worker.started", {"ref": "fake"})
        raises("未知事件拒绝", mod.LifecycleError,
               service.append, "w1", "mystery.event", {})
        raises("expected_seq 冲突", mod.ConflictError,
               service.append, "w1", "progress", {}, expected_seq=999)

        corrupt = root / "worker-bad.jsonl"
        corrupt.write_text("{not-json}\n", encoding="utf-8")
        raises("坏账本 fail-closed", mod.LedgerCorrupt, service.status, "bad")
        listed = {row["worker_id"]: row for row in service.list_workers()}
        check("坏账本列为 unknown 而不是 absent",
              listed["bad"]["state"] == "unknown", str(listed["bad"]))

        # 5. CLI 真能落账
        cli_root = root / "cli"
        env = {**os.environ, "KG_WORKER_LIFECYCLE_ROOT": str(cli_root),
               "PYTHONPATH": str(OPS)}
        cli_spec = sample("cli-1")
        start_out = subprocess.run(
            [sys.executable, str(MODULE), "start", "--spec-json",
             json.dumps(cli_spec, ensure_ascii=False)],
            cwd=str(OPS), env=env, capture_output=True, text=True)
        check("CLI start exit 0", start_out.returncode == 0,
              start_out.stderr.strip())
        status_out = subprocess.run(
            [sys.executable, str(MODULE), "status", "--worker-id", "cli-1"],
            cwd=str(OPS), env=env, capture_output=True, text=True)
        check("CLI status exit 0", status_out.returncode == 0,
              status_out.stderr.strip())
        payload = json.loads(status_out.stdout)
        check("CLI status 读到 running", payload["state"] == "running", str(payload))
        denied = subprocess.run(
            [sys.executable, str(MODULE), "start", "--spec-json",
             json.dumps(sample("cli-2", authorization="inferred",
                               effect="destructive"), ensure_ascii=False)],
            cwd=str(OPS), env=env, capture_output=True, text=True)
        check("CLI 授权禁行返回非零", denied.returncode != 0,
              denied.stdout.strip())

    print("worker lifecycle: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
