# 自动挡安全闸

## 闸门

| 闸门 | 函数 | 作用 | 关闭方式 |
|---|---|---|---|
| 防重复派发 | `_live_exec_slugs()` | 同一工作区已有 `codex exec` 时不派第二个 | 无常驻开关，只在日志留痕 |
| 派发上限 | `_exec_attempt_gate()` | 6h 窗口内同一需求最多派 3 次，超限进 blocked | 调 `EXEC_REPEAT_CAP` |
| 冲突停手 | `_escalate_conflict_block()` | 同一组冲突文件连续撞 2 次后停手等待裁决 | 面板 blocked 人工放行 |
| 磁盘准入 | `_disk_pressure()` | 可用空间低于 6GB 时只停新 worker；恢复到 10GB 才放行 | `KG_EXEC_DISK_STOP_FREE_GB` / `KG_EXEC_DISK_RESUME_FREE_GB` |
| 现场回收 | `_reap_backend_orphans()` / `_reap_dead_worktrees()` | 回收孤儿后端和满足严格条件的闲置现场 | `KG_REAP_BACKENDS=0` / `KG_REAP_WORKTREES=0` |
| 子进程执行 | `ops/proc.py` 的 `proc_run()` | 所有 git / 子进程调用统一走受控执行器：输出落临时文件（不用管道，孙进程攥不住 EOF）、硬超时 + 超时 killpg 收整组、`GIT_NO_LAZY_FETCH=1` 禁 partial clone 懒加载派网络子进程 | `KG_EXEC_TIMEOUT_LOCAL_GIT` / `KG_EXEC_TIMEOUT_NET_GIT` 调预算 |

## 子进程为什么必须走 `proc.py`（2026-10-27 实锤）

共享检出是 partial clone（`remote.origin.promisor=true`、`blob:none`）。旧代码用
`subprocess.run(capture_output=True)`：git 遇到本地缺失 blob 会派**网络孙进程**去懒加载，
git 本体退出后孙进程仍攥着管道写端，`communicate()` 永远等不到 EOF。实测一次 `tick`
静默挂起 30+ 分钟（`sample` 栈帧停在 `select_poll_poll → read`，13 分钟只烧 0.42s CPU）。

因此三个模块（`kgctl` / `central_daemon` / `truthctl`）里 135 处 git 调用全部改成
`proc_run()`，并由门禁 `test_proc_run_never_hangs_on_inherited_pipe` 断言三模块内
不存在裸 `subprocess.run`。扫描类调用超时（rc=124）一律 **fail-closed**：
视作「可能仍有冲突」，宁可阻塞落盘也不放行半成品。

同类坑：`codex_native.snapshot()` 的历史兜底查询曾绕开调用方预算，
实测 1 秒预算等了 4.9 秒（5.0GB 的 `thread_history_1.sqlite`，一条 GROUP BY 3.25 秒）。
现在 `_history_snapshot(budget=)` 放 daemon 线程跑，超预算返回旧缓存。

## 并发边界

并发上限由 `ops/central.config.json` 的 `auto_exec.max_workers` 决定；磁盘准入
决定是否允许启动新 worker。低于停止线时不启动新任务，但已运行任务继续收尾；
达到恢复线后自动放行，避免反复清盘和边界抖动。

## 设计纪律

1. 有副作用的循环必须幂等：重启后先认领在跑的，再决定是否新开。
2. 失败必须退避、有上限且可见。
3. 需要人裁决的信号不得进入自动重试队列。
4. 谁创建谁回收；回收条件从严。
5. 每个闸门留下可核对日志；磁盘余量读取失败时必须 fail-closed。

## 自测

```bash
cd /Users/danta/.newmax/workspace/codex-autopilot
python3 -m py_compile ops/central_daemon.py ops/selftest.py
python3 ops/selftest.py
```
