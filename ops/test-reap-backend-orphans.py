#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：工作区里闲置的 dev server 要被回收，正在用的一条都不能碰。

为什么有这条（2026-11-12 用户在会话里的实测）：
  每小时跑的回收器日志恒为「孤儿后端 0 个」，可工作区里有一批
  `tsx watch` / `vite` 已经空转 7~8 小时。旧判据要求命令行含 `dist/index.js`
  **且** 内核 cwd 带 `-exec-`：这些进程 argv 里根本没有树路径
  （`node .../kg-x/backend/node_modules/.bin/tsx watch src/index.ts`），
  两条都匹配不上 —— 回收器不是「判断为不能杀」，而是**根本没看见**。

  更坏的是：`tsx watch` 不理会 SIGTERM。用户手写的 `kill 83627` 卡在
  `while kill -0 83627` 里 2 小时没返回，只发 TERM 的回收器会变成
  「日志报告已回收、进程还在」。

  这里把两件事一起钉死：
    1. 判据看得见（按内核 cwd 认树，不信 argv）；
    2. 判断方向永远偏保守（下面每一条「必须放过」的用例）。

  反向断言同样重要：一棵真正闲置、无人使用的树必须仍可回收，否则这条加固
  就变成「把所有 dev server 永久钉住」，磁盘照样爆 —— 那正是用户最初抱怨的症状。
"""
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import live_guard  # noqa: E402
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, OPS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def spawn_term_ignorer(cwd: Path) -> subprocess.Popen:
    """一个忽略 SIGTERM 的长命进程，cwd 落在 cwd 里（模仿 tsx watch）。"""
    return subprocess.Popen(
        [sys.executable, "-c",
         "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
         " time.sleep(300)"],
        cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def alive(pid) -> bool:
    """僵尸不算活着：SIGKILL 之后进程会短暂留在 ps 里等父进程收尸。"""
    out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "stat="],
                         capture_output=True, text=True).stdout.strip()
    if not out:
        return False
    return not out.startswith("Z")


def main():
    cd = load("cd_orphan", "central_daemon.py")

    base = Path(tempfile.mkdtemp(prefix="reap-orphan-"))
    runtime = base / "runtime"
    runtime.mkdir()
    cd.RUNTIME = runtime
    cd.LOG = runtime / "daemon.log"
    root = base / "workspace"
    root.mkdir()
    os.environ["KG_WORKSPACE_ROOT"] = str(root)
    os.environ["KG_REAP_BACKENDS"] = "1"
    cd.BACKEND_MIN_IDLE_H = 3.0
    cd.BACKEND_MAX_PER_ROUND = 40

    # 判活打桩：默认「无人使用」，各用例按需覆盖。
    live_guard.reset_caches()
    orig_sessions = live_guard.sessions_working_in
    orig_live = live_guard.live_processes_in
    orig_servers = cd._workspace_dev_servers
    orig_exec = cd._live_exec_slugs
    orig_last_activity = cd._wt_last_activity
    live_guard.sessions_working_in = lambda wt: []
    live_guard.live_processes_in = lambda wt: []
    cd._live_exec_slugs = lambda *a, **k: frozenset()
    # 树「不热」：让闲置闸放行，专测判活部分。
    cd._wt_last_activity = lambda wt: 0.0

    def serve(tree_name, pids):
        tree = root / tree_name
        tree.mkdir(exist_ok=True)
        return [{"pid": str(p), "cwd": str(tree), "wt": tree, "cmd": "tsx watch"}
                for p in pids]

    procs = []
    try:
        # ---- 1. 闲置 + 无会话 + 无连接：整族必须被回收 ----
        t = root / "kg-idle"
        t.mkdir()
        victim = spawn_term_ignorer(t)
        procs.append(victim)
        time.sleep(0.4)
        cd._workspace_dev_servers = lambda: serve("kg-idle", [victim.pid])
        n = cd._reap_backend_orphans()
        time.sleep(0.3)
        check("闲置 dev server 被回收", not alive(victim.pid), f"killed={n}")
        check("回收计数为 1", n == 1, f"killed={n}")

        # ---- 2. SIGTERM 杀不动时要用 SIGKILL 兜底 ----
        t2 = root / "kg-stubborn"
        t2.mkdir()
        stubborn = spawn_term_ignorer(t2)
        procs.append(stubborn)
        time.sleep(0.4)
        ok = cd._terminate_pid(stubborn.pid, grace_s=1.0)
        check("忽略 SIGTERM 的进程仍被收掉", ok and not alive(stubborn.pid), f"ok={ok}")

        # ---- 3. 有活跃会话（只读会话也算）-> 放过 ----
        t3 = root / "kg-live-session"
        t3.mkdir()
        keep3 = spawn_term_ignorer(t3)
        procs.append(keep3)
        time.sleep(0.4)
        cd._workspace_dev_servers = lambda: serve("kg-live-session", [keep3.pid])
        live_guard.sessions_working_in = (
            lambda wt: ["file:///.../kg-live-session"] if wt.name == "kg-live-session" else [])
        n = cd._reap_backend_orphans()
        check("有活跃会话的树不回收", alive(keep3.pid) and n == 0, f"killed={n}")
        live_guard.sessions_working_in = lambda wt: []

        # ---- 4. 有人在连（浏览器 / HMR websocket）-> 放过 ----
        t4 = root / "kg-connected"
        t4.mkdir()
        keep4 = spawn_term_ignorer(t4)
        procs.append(keep4)
        time.sleep(0.4)
        cd._workspace_dev_servers = lambda: serve("kg-connected", [keep4.pid])
        orig_proc_run = cd.proc_run

        def fake_proc_run(cmd, **kw):
            # cmd 是参数列表，不是字符串：写成 `"ESTABLISHED" in cmd` 会变成
            # 列表成员判断，永远为假，这条用例就会假装通过。
            if any("ESTABLISHED" in str(c) for c in cmd):
                class R:
                    returncode = 0
                    stdout = ("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
                              "node 1 danta 20u IPv4 x 0t0 TCP 127.0.0.1:1->127.0.0.1:2\n")
                    stderr = ""
                return R()
            return orig_proc_run(cmd, **kw)

        cd.proc_run = fake_proc_run
        n = cd._reap_backend_orphans()
        check("有人在连的 dev server 不回收", alive(keep4.pid) and n == 0, f"killed={n}")
        cd.proc_run = orig_proc_run

        # ---- 5. 树里还有 dev server 家族之外的活进程 -> 放过 ----
        t5 = root / "kg-stranger"
        t5.mkdir()
        keep5 = spawn_term_ignorer(t5)
        procs.append(keep5)
        time.sleep(0.4)
        cd._workspace_dev_servers = lambda: serve("kg-stranger", [keep5.pid])
        live_guard.live_processes_in = (
            lambda wt: [str(keep5.pid), "999999"] if wt.name == "kg-stranger" else [])
        n = cd._reap_backend_orphans()
        check("树里有陌生活进程时不回收", alive(keep5.pid) and n == 0, f"killed={n}")
        live_guard.live_processes_in = lambda wt: []

        # ---- 6. dev server 自己不能否决自己（否则回收器又变回空转）----
        t6 = root / "kg-selfveto"
        t6.mkdir()
        selfv = spawn_term_ignorer(t6)
        procs.append(selfv)
        time.sleep(0.4)
        cd._workspace_dev_servers = lambda: serve("kg-selfveto", [selfv.pid])
        # 内核 cwd 里只有它自己 —— 家族展开后不应再算「陌生人」。
        live_guard.live_processes_in = lambda wt: [str(selfv.pid)]
        n = cd._reap_backend_orphans()
        time.sleep(0.3)
        check("dev server 不否决自己", not alive(selfv.pid), f"killed={n}")
        live_guard.live_processes_in = lambda wt: []

        # ---- 7. 保护前缀（kg-live-*：用户实际在跑的那份）-> 放过 ----
        t7 = root / "kg-live-3000"
        t7.mkdir()
        keep7 = spawn_term_ignorer(t7)
        procs.append(keep7)
        time.sleep(0.4)
        cd._workspace_dev_servers = lambda: serve("kg-live-3000", [keep7.pid])
        n = cd._reap_backend_orphans()
        check("受保护的 kg-live-* 不回收", alive(keep7.pid) and n == 0, f"killed={n}")

        # ---- 8. 树上有 codex exec -> 放过（它自己会拉起后端）----
        t8 = root / "kg-exec-tree"
        t8.mkdir()
        keep8 = spawn_term_ignorer(t8)
        procs.append(keep8)
        time.sleep(0.4)
        cd._workspace_dev_servers = lambda: serve("kg-exec-tree", [keep8.pid])
        cd._live_exec_slugs = lambda *a, **k: frozenset({"kg-exec-tree"})
        n = cd._reap_backend_orphans()
        check("挂着 codex exec 的树不回收", alive(keep8.pid) and n == 0, f"killed={n}")
        cd._live_exec_slugs = lambda *a, **k: frozenset()

        # ---- 9. 树还很热（刚动过）-> 放过 ----
        t9 = root / "kg-hot"
        t9.mkdir()
        keep9 = spawn_term_ignorer(t9)
        procs.append(keep9)
        time.sleep(0.4)
        cd._workspace_dev_servers = lambda: serve("kg-hot", [keep9.pid])
        cd._wt_last_activity = lambda wt: time.time()  # 就是现在动过
        n = cd._reap_backend_orphans()
        check("刚动过的树不回收", alive(keep9.pid) and n == 0, f"killed={n}")
        cd._wt_last_activity = lambda wt: 0.0

        # ---- 10. 总开关关掉 -> 一个都不动 ----
        cd._knob = lambda name, default=True: False if name == "reap_backends" else default
        cd._workspace_dev_servers = lambda: serve("kg-idle", [victim.pid])
        check("KG_REAP_BACKENDS=0 时恒为 0", cd._reap_backend_orphans() == 0)

        # ---- 11. 按内核 cwd 认树：只看 workspace 下的 kg-* ----
        outside = base / "outside-project"
        outside.mkdir()
        (outside / "backend").mkdir()
        bad = spawn_term_ignorer(outside / "backend")
        procs.append(bad)
        time.sleep(0.4)
        cd._workspace_dev_servers = lambda: [
            it for it in orig_servers()
            if str(it["wt"]).startswith(str(root))
        ]
        found = {it["wt"].name for it in cd._workspace_dev_servers()}
        check("工作区外的 dev server 不进候选", "outside-project" not in found,
              f"found={sorted(found)}")
    finally:
        live_guard.sessions_working_in = orig_sessions
        live_guard.live_processes_in = orig_live
        cd._workspace_dev_servers = orig_servers
        cd._live_exec_slugs = orig_exec
        cd._wt_last_activity = orig_last_activity
        for p in procs:
            try:
                os.kill(p.pid, signal.SIGKILL)
            except Exception:
                pass

    if failures:
        print(f"\n{len(failures)} 项失败：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nbackend orphan reap: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
