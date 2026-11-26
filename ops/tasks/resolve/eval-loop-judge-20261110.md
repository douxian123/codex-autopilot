# 冲突任务：eval-loop-judge-20261110

- 源分支：`codex/eval-loop-judge-20261110`
- 执行分支：`codex/resolve-eval-loop-judge-20261110`
- 工作树：`/Users/danta/.newmax/workspace/codex-autopilot-resolve-eval-loop-judge-20261110`
- 基于：`02ad0ab72ec3`（origin/main）
- 状态：`needs_agent`

## 要解的冲突文件（保留的 marker）
- `docs/PRODUCT_MAINLINE.md`
- `ops/product_eval.py`
- `ops/selftest.py`
- `ops/truth/product_state.json`

## 怎么做
1. 进 `/Users/danta/.newmax/workspace/codex-autopilot-resolve-eval-loop-judge-20261110`，逐个打开上面列出的文件，把 `<<<<<<<` / `=======` / `>>>>>>>` 改成正确的最终代码——想清楚两边意图，不是机械二选一。
2. 显式 `git add <文件>`（不要 `-A`），`git commit -m "resolve: ..."`。
3. `python3 ops/kgctl.py resolve --finish eval-loop-judge-20261110 --go`：无 marker 且过了 build 门禁才会落盘。

## 硬约束
- 不 `git add -A`、不 `checkout .`、不 `stash`、不 `reset --hard`；只动本工作树里自己的文件。
- 不用 `--ours`/`--theirs` 强行盖掉一侧；两边意图都要保住，或明确取舍并写进提交信息。
- main 与源分支都不动；落盘统一交给 `resolve --finish`。
