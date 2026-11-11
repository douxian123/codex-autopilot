# Codex 自动挡进展简报

## 2026-11-11：显式锁定的工作树不再进入回收候选

这不是正常的工作树轮换，也不是任务做久了必然被清空。已复现的根因是回收器没有读取
Git 的显式 worktree lock：即使目录被人为锁定，旧回收流程仍可能把它算成候选。

修复已经进入 `main` 并安装为 `0.6.0-preview.59`。定时回收任务也已从旧版本切换到
`.59`。在磁盘可用空间低于阈值的真实预演里，结果是会回收 14 个签名临时副本、
0 个 worktree、8 个 `kg tmp`、0 个孤儿残留；被锁定的
`kg-source-provenance-audit-20261110` 在日志中零命中。

### 可核对证据

- 修复提交：`0d98bca99bf47a9c226ca3ce16ab062037aaa30d`
- 发布提交：`fdb7de9f3d666bbc4e1c21b34c76b51ad2aa3b31`
- `main` 与 `origin/main`：均为 `fdb7de9f3d666bbc4e1c21b34c76b51ad2aa3b31`
- 全量自检：`python3 ops/selftest.py`，退出码 `0`
- 安装版锁定门禁：`ops/test-reap-worktree-lock.py`，输出
  `reap worktree lock unit: PASS`，退出码 `0`
- 安装版低盘压力预演：`ops/disk_reap.py --dry-run`，退出码 `0`
- 预演日志：
  `/tmp/codex-autopilot-preview59-disk-reap-dry-run.log`

### 仍需注意

Codex 桌面端如果仍显示旧插件界面，需要在合适时重启桌面端以重新加载插件。定时回收
本身已经运行 `.59`，不依赖桌面端缓存刷新。入口新鲜度检查另有
`ai.danta.codex-proxy-watchdog` 这一条既有红灯，指向
`/Users/danta/.codex-proxy/proxy-watchdog.sh`，与本次 worktree lock 修复无关，未擅自改动。
