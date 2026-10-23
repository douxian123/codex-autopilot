# 自动挡安全闸

## 闸门

| 闸门 | 函数 | 作用 | 关闭方式 |
|---|---|---|---|
| 防重复派发 | `_live_exec_slugs()` | 同一工作区已有 `codex exec` 时不派第二个 | 无常驻开关，只在日志留痕 |
| 派发上限 | `_exec_attempt_gate()` | 6h 窗口内同一需求最多派 3 次，超限进 blocked | 调 `EXEC_REPEAT_CAP` |
| 冲突停手 | `_escalate_conflict_block()` | 同一组冲突文件连续撞 2 次后停手等待裁决 | 面板 blocked 人工放行 |
| 现场回收 | `_reap_backend_orphans()` / `_reap_dead_worktrees()` | 回收孤儿后端和满足严格条件的闲置现场 | `KG_REAP_BACKENDS=0` / `KG_REAP_WORKTREES=0` |

## 并发边界

派发槽位只由 `ops/central.config.json` 的 `auto_exec.max_workers` 决定。
磁盘余量只触发后台回收，不参与派活限流，不会把并发槽位压成 0。

## 设计纪律

1. 有副作用的循环必须幂等：重启后先认领在跑的，再决定是否新开。
2. 失败必须退避、有上限且可见。
3. 需要人裁决的信号不得进入自动重试队列。
4. 谁创建谁回收；回收条件从严。
5. 每个闸门 fail-open，并留下可核对日志。

## 自测

```bash
cd /Users/danta/.newmax/workspace/codex-autopilot
python3 -m py_compile ops/central_daemon.py ops/selftest.py
python3 ops/selftest.py
```
