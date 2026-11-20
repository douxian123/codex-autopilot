#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：真实 auto_exec 派发必须经过 worker 生命周期账本。

锁住的不是「适配层文件存在」，而是失败路径：
  * 坏账本 / 状态冲突时不能 Popen，也不能占并发槽位；
  * 启动成功后账本要记下 PID，回收结果要回写终态；
  * handed_back / completed 之后重派必须换新 worker，不能复用终态；
  * 恢复在途现场不伪装成新 session，但要留下可审计外部引用。
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


class FakePopen:
    def __init__(self, cmd, **_kwargs):
        self.cmd = list(cmd)
        self.pid = 42000 + len(SPAWN_CALLS)
        self.returncode = None
        SPAWN_CALLS.append(self.cmd)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


SPAWN_CALLS: list[list[str]] = []


def make_job(tmp: Path, rid: str, **overrides) -> dict:
    wt = tmp / f"wt-{rid}"
    wt.mkdir(parents=True, exist_ok=True)
    job = {
        "id": rid,
        "item": {
            "id": rid,
            "kind": "bug",
            "lane": "bug",
            "title": "生命周期接线回归",
            "files": ["app.txt"],
        },
        "cfg": {
            "timeout": 1,
            "sandbox": "workspace-write",
            "add_dirs": [],
            "reasoning_effort": "medium",
            "retries": 0,
            "no_change_retries": 0,
        },
        "repo": str(tmp),
        "branch": f"codex/exec-{rid.lower()}",
        "worktree": str(wt),
        "codex": ["/bin/true"],
        "log": str(tmp / f"{rid}.log"),
        "model": None,
        "started_at": "2026-11-18T00:00:00+0800",
        "recover": False,
        "result": {"id": rid},
    }
    job.update(overrides)
    return job


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="exec-lifecycle-test-") as raw:
        tmp = Path(raw)
        home = tmp / "home"
        repo = tmp / "repo"
        trunk = tmp / "trunk"
        life = tmp / "lifecycle"
        for path in (home, repo, trunk, life):
            path.mkdir(parents=True, exist_ok=True)

        os.environ["KG_DAEMON_HOME"] = str(home)
        os.environ["KG_REPO"] = str(repo)
        os.environ["KG_TRUNK"] = str(trunk)
        os.environ["KG_EXEC_CMD"] = "/bin/true"
        os.environ["KG_WORKER_LIFECYCLE_ROOT"] = str(life)

        worker_lifecycle = load_module(
            "worker_lifecycle", OPS / "worker_lifecycle.py")
        exec_lifecycle = load_module(
            "exec_lifecycle", OPS / "exec_lifecycle.py")
        daemon = load_module(
            "central_daemon_under_test", OPS / "central_daemon.py")

        original_popen = daemon.subprocess.Popen
        original_command = daemon._exec_command
        daemon.subprocess.Popen = FakePopen
        daemon._exec_command = lambda job: ["fake-codex"]
        service = worker_lifecycle.WorkerLifecycle(life)

        try:
            # 1. 正常派发：账本必须记下真实 PID，job 才能拿到 proc/slot。
            job = make_job(tmp, "L-9001")
            started = daemon._start_exec(job)
            check("真实派发返回 job", started is not None, str(job))
            check("Popen 只调用一次", len(SPAWN_CALLS) == 1, str(SPAWN_CALLS))
            state = service.status("exec-L-9001")
            check("账本进入 running", state["state"] == "running", str(state))
            check("账本记录外部 PID", state["external_ref"] == 42000, str(state))

            # 2. 回收结果必须落终态；终态后重派必须换 worker id。
            projection, err = exec_lifecycle.report(
                job, {"outcome": "landed", "exit": 0}, runtime=home)
            check("回收上报没有错误", err is None, str(err))
            check("回收后账本 completed",
                  projection["state"] == "completed", str(projection))
            count_after_report = projection["event_count"]
            repeated, repeated_err = exec_lifecycle.report(
                job, {"outcome": "landed", "exit": 0}, runtime=home)
            check("重复上报没有错误", repeated_err is None, str(repeated_err))
            check("重复上报不污染账本",
                  repeated and repeated["event_count"] == count_after_report,
                  f"{count_after_report} -> {repeated and repeated['event_count']}")
            first_worker = job["lifecycle_worker_id"]
            check("重派前 worker id 已落账", bool(first_worker), str(job))
            daemon._start_exec(job)
            second_worker = job["lifecycle_worker_id"]
            check("终态重派换新 worker", second_worker != first_worker,
                  f"{first_worker} -> {second_worker}")
            new_state = service.status(second_worker)
            check("新 worker 是独立 generation=1",
                  new_state["state"] == "running"
                  and new_state["generation"] == 1, str(new_state))

            # 3. 通道不可用交回路由：handed_back 是终态，不许继续硬做。
            handback_job = make_job(tmp, "L-HB")
            daemon._start_exec(handback_job)
            handed, err = exec_lifecycle.report(
                handback_job, {"outcome": "channel_unavailable"}, runtime=home)
            check("handback 上报没有错误", err is None, str(err))
            check("通道不可用落到 handed_back",
                  handed["state"] == "handed_back", str(handed))
            check("handback 指向 route",
                  handed["handback_target"] == "route", str(handed))

            # 4. 在途现场恢复：不新开 session，但必须留下 recovered 外ref。
            recover_job = make_job(tmp, "L-REC", recover=True)
            recovered = daemon._start_exec(recover_job)
            check("恢复现场返回 job", recovered is not None, str(recover_job))
            recover_state = service.status(recover_job["lifecycle_worker_id"])
            check("恢复现场账本 running",
                  recover_state["state"] == "running", str(recover_state))
            check("恢复现场外ref 可审计",
                  str(recover_state["external_ref"]).startswith("recovered:"),
                  str(recover_state))

            # 5. 坏账本 fail-closed：绝不 Popen，绝不悄悄当 absent。
            bad_job = make_job(tmp, "L-BAD")
            bad_path = life / "worker-exec-L-BAD.jsonl"
            bad_path.write_text("{not-json}\n", encoding="utf-8")
            before = len(SPAWN_CALLS)
            bad_started = daemon._start_exec(bad_job)
            check("坏账本拒绝启动", bad_started is None, str(bad_job))
            check("坏账本不启动进程", len(SPAWN_CALLS) == before,
                  str(SPAWN_CALLS[before:]))
            check("坏账本错误可见",
                  "LedgerCorrupt" in str(bad_job.get("start_error") or ""),
                  str(bad_job.get("start_error")))

            # 5b. 工单 allowlist 之外什么都不启动，也不占槽、不落 running。
            deny_job = make_job(tmp, "L-DENY")
            deny_job["item"]["allowed_resources"] = ["app.txt"]
            deny_job["item"]["allowed_tools"] = ["codex_exec"]
            deny_job["lifecycle_requested_resources"] = ["app.txt", "secret.txt"]
            before_deny = len(SPAWN_CALLS)
            deny_started = daemon._start_exec(deny_job)
            check("越权资源拒绝启动", deny_started is None, str(deny_job))
            check("越权资源不启动进程", len(SPAWN_CALLS) == before_deny,
                  str(SPAWN_CALLS[before_deny:]))
            check("越权资源错误可见",
                  "AuthorizationError" in str(deny_job.get("start_error") or ""),
                  str(deny_job.get("start_error")))
            deny_state = service.status(
                exec_lifecycle.safe_worker_id("L-DENY"))
            check("越权资源不落 running 账本",
                  deny_state["state"] == "absent", str(deny_state))

            deny_tool_job = make_job(tmp, "L-DENY-TOOL")
            deny_tool_job["item"]["allowed_tools"] = ["codex_exec"]
            deny_tool_job["lifecycle_requested_tools"] = ["shell"]
            before_tool_deny = len(SPAWN_CALLS)
            deny_tool_started = daemon._start_exec(deny_tool_job)
            check("越权工具拒绝启动", deny_tool_started is None,
                  str(deny_tool_job))
            check("越权工具不启动进程", len(SPAWN_CALLS) == before_tool_deny,
                  str(SPAWN_CALLS[before_tool_deny:]))

            # 6. dispatch 层不能把失败启动塞回并发槽位。
            source = (OPS / "central_daemon.py").read_text(encoding="utf-8")
            check("dispatch 接住空启动", "started = _start_exec(job)" in source
                  and "if started is None:" in source
                  and "jobs[rid] = started" in source)

            # 7. 适配层时序：start.requested 必须先落盘，才允许 launch。
            timing_job = make_job(tmp, "L-TIMING")
            timing_calls: list[str] = []

            def timing_launch() -> str:
                ledger = life / (
                    f"worker-{exec_lifecycle.safe_worker_id('L-TIMING')}.jsonl")
                raw = ledger.read_text(encoding="utf-8")
                check("launch 前已有 start.requested",
                      "start.requested" in raw, raw[:200])
                timing_calls.append("launch")
                return "pid-timing"

            timing_projection, timing_err = exec_lifecycle.start_or_resume(
                timing_job, timing_launch, runtime=home)
            check("适配层派发无错误", timing_err is None, str(timing_err))
            check("适配层 launch 恰好一次", timing_calls == ["launch"],
                  str(timing_calls))
            check("适配层账本 running",
                  timing_projection and timing_projection["state"] == "running",
                  str(timing_projection))
        finally:
            daemon.subprocess.Popen = original_popen
            daemon._exec_command = original_command
            for job in globals().get("JOBS_TO_CLOSE", []):
                fh = job.get("fh")
                if fh is not None:
                    try:
                        fh.close()
                    except OSError:
                        pass

    print("exec lifecycle: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
