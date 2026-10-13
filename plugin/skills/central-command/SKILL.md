---
name: central-command
description: Codex 自动挡。当用户在同一项目里提出零散、即时、难以归类的需求（视觉问题、体验问题、bug、架构项、门禁红灯、多分支要不要合、该在哪个对话/分支继续），或问“现在最该做什么/项目到哪了/还有什么没做”时使用。把输入收进唯一入口、归到固定泳道、写进项目真相，并给出下一步；不要要求用户自己选对话、选分支、选角色。
---

# Codex Autopilot（自动挡）

## 一句话
用户只负责把模糊、零散、即时的感受丢进同一个输入框；归类、路由、排期、追踪、收口由你完成。

## 触发信号
- 用户对着界面说“这里不对 / 视觉坏了 / 体验别扭”，没有说进哪个分支。
- 用户报一个 bug 或门禁红灯，但没有指定泳道。
- 用户问“这个分支要不要合 / 等谁先合 / 现在到哪了 / 下一步做什么”。
- 用户一次抛出多条彼此无关的小需求。

## 硬规则
1. 绝不让用户选择：不改问“放哪个对话/开哪个分支/派给哪个角色”。
2. 不造分支海：固定泳道在 `ops/tasks/lanes.json`（默认 `design` / `bug` / `arch` / `gate`），
   一个 intake 只进一条；可用 `ops/central.config.json` 的 `lanes` 覆盖。
3. 先记账再动手：任何零散需求先 `inbox`，再 `tick`，再执行。
4. 只读 + 追加：归类、快照、看板都是加法；合并前必过门禁；不进共享 checkout 写文件。
5. 用户拍板只限三种：改产品方向、删数据、发布。其余自行路由执行。
6. 落盘自带验证：合入走真实验证门禁，命令 + cwd + 退出码写进验收台账；没配门禁就标 `todo`，不伪造 `verified`。
7. 每轮蒸馏：收口后回看这轮人额外说了什么，把「为什么这么判断」写回判断逻辑台账；只记判断逻辑，不记一次性细节。
8. 产品讨论自动成主线：每轮收口 `truthctl synthesize`，把决策/进展/问题/验收综合成 `docs/PRODUCT_MAINLINE.md`。人只读这一页，就知道现状、最近在推什么、还差什么；不用人肉把零散对话归类成 PRD。
9. 问题一次提出就闭环：bug/视觉/体验/架构收进台账后，由你主动推进修复 -> 验证 -> `truthctl close <L-id> "<结论>"`。关闭要带 commit/证据；不许修完了还挂在 `open`，也不许没验证就标 `done`。

## 做产品视觉/设计时的硬规则
改任何界面/视觉/体验之前，先把自己当这个产品的产品经理 + 产品设计师，按下面判断再动手：
- 尊重用户，保护注意力：显示在面板上的每一条信息都必须值得用户看，是最核心、最有利于理解的信息。层级、优先级、视觉秩序是第一位。
- 一眼看懂：用户扫一眼就知道「这是什么、现在怎么样、要我做什么」。用最浓缩、最克制、最有秩序的篇幅，把必要信息优雅地塞进用户脑子，不堆废信息。
- 先截图再改：动手改视觉前先跑起来截图，判断现状是否符合预期和设计规范。改完再截一次对比。
- 只允许正向改动：明显更清晰、更符合注意力秩序才改；不能比现在更丑、更乱、更难读。宁可不动，也不做负向改动。
- 拿不准就先不改，把判断写成一条 `truthctl distill add "<判断>"`（`KG_TRUTH_SCOPE=design`），下次不用重复讲。

## 两种调用方式
- 有 MCP 工具时优先直接调用（读写的就是下面这套 CLI）：
  `intake_request`（一句话收进唯一入口并走完整条链，首选）、`sync_control_plane`（重扫主循环）、
  `next_actions`、`project_status`、`route_inbox`、`resolve_list`、`resolve_plan`、
  `resolve_start`、`resolve_finish`、`convergence_plan`（冲突收拢）、
  `generate_context`、`dashboard`、`capture_request`（只追加）、`suggest_branch`、
  `product_state` / `product_update`（产品状态）、`distill_review` / `distill_add`（判断逻辑蒸馏）、
  `synthesize_mainline`（综合成产品主线）、`close_item` / `reopen_item`（问题收口）。
- 否则走 CLI。运行时代码在插件里（自包含安装：`~/plugins/codex-autopilot/ops/`），
  项目里只留账本；手敲时先设 `KG_REPO` 指到项目（MCP 调用时插件已自动注入）：

```bash
KGCTL=~/plugins/codex-autopilot/ops/kgctl.py
export KG_REPO=/path/to/your-project

# 一句话入口：记录 -> 归类 -> 刷新真相 -> 刷看板 -> 给下一步（一步完成）
python3 "$KGCTL" intake "左侧导航 rail 排版坏了，图标贴左缘被裁切"

# 一次抛了多条、或想重扫全项目时，跑主循环
python3 "$KGCTL" tick

# 看项目真相 / 当前待办 / 驾驶舱
sed -n 1,80p "$KG_REPO/docs/PROJECT_STATUS.md"
python3 "$KGCTL" next
python3 "$KGCTL" dash --open            # 生成并打开 docs/CONTROL_PLANE.html
```

## 分支很多、到底谁先合
冲突分支不要逐条问用户。默认把每条轻冲突拆成**一个可执行任务**，逐条收口；量大且分散时再收拢成族：

```bash
python3 "$KGCTL" supersede --fast      # 归档「内容已全部在 main」的重复分支（不动代码）
python3 "$KGCTL" port --auto --go      # 把「只差 1–2 个提交」的冲突分支移植到 main，冲突即整体放弃
python3 "$KGCTL" resolve --auto        # 预演：挑一条轻冲突，说明几个文件、多大
python3 "$KGCTL" resolve --auto --go   # 建独立 worktree：JSONL 行级并集，其余留 marker + 任务书
python3 "$KGCTL" resolve --list        # 看进行中的冲突任务（slug / worktree / 待解文件）
# agent 在任务 worktree 里解 marker、显式 add + commit 后：
python3 "$KGCTL" resolve --finish <slug> --go   # 走 land 同一道门禁落盘并回收
python3 "$KGCTL" converge --go         # 冲突量大且分散时，收成少数几个 convergence 任务
python3 "$KGCTL" replan --go           # 过时的超级合并按当前 main 重盘：解散成逐支流水线
python3 "$KGCTL" context <分支>    # 给该分支写一份版本化 Context Pack
```

先用 `supersede` 摘掉「内容其实早进 main」的假冲突（patch-id 判据，只写台账、不动代码、
不动源分支），再用 `port` 把「只差几个提交」的分支 cherry-pick 到当前 main（冲突即
`--abort` 整体放弃，成功也走同一道验证门禁）。剩下的才是真需要语义判断的冲突：默认
`resolve` 把它们逐条拆成独立任务——append-only JSONL 走行级并集，其余保留 marker 写任务书，
解完走同一道门禁，一条冲突不再需要一个对话；量大时才用 `converge` 收成族。`tick` 已自动
包含收拢与归档。

## 执行路径
- `intake` / `tick` 之后，按 `next` 给出的优先级挑一条，在独立 worktree 里做；不碰别人在途文件。
- 改完提交到该泳道分支；能干净合并、相关门禁通过就直接 `kgctl land <分支>`，不要把“能不能合”当问题回报给用户。
- 改到产品行为的同一轮，把它写进产品状态：`truthctl product set "<模块>" "<行为>"`（`KG_PS_WHY` / `KG_PS_FILES`）；人验收只看它相对主线的变化，不读实现。
- 分支冲突先别急着问用户：`supersede` 归档已落内容，`port` 自动移植只差几个提交的分支；剩下真语义冲突出现在 `resolve --list` 里，逐条解完后 `resolve --finish` 收口。
- 接手一条陌生分支前先 `kgctl context <分支>`，读它写出的主线/决策/相关未决问题/DoD，而不是靠聊天记忆。
- 落盘后把进展写进真相层：`truthctl add progress "..."`（`KG_TRUTH_COMMIT=<sha>`、`KG_TRUTH_LANDED=1`）；`land` 自身会把每道验证门禁的 `name: cmd (cwd=...) -> exit N` 写进验收台账，不写「通过构建」这类结论。
- 问题修完写一条状态变更：`truthctl add ledger` 或直接更新对应条目的 `status=done`。
- 一轮收口后蒸馏一次：`truthctl distill` 列出这轮人额外说了什么，用 `truthctl distill add "<判断逻辑>"`（`KG_TRUTH_WHY` / `KG_TRUTH_SCOPE`）把「为什么这么判断」写回，下次不用重复说。
- 问题修完、验收过就收口：`truthctl close <L-id> "<结论>"`（`KG_TRUTH_COMMIT`、`KG_TRUTH_EVIDENCE` 可选），把台账从 `open` 翻到 `done` 并追加进展；回归了用 `truthctl open <L-id> "<原因>"`。
- 每轮收口综合一次：`truthctl synthesize` 把决策/进展/问题/验收写成 `docs/PRODUCT_MAINLINE.md`，产品讨论自动长成主线，人只读这一页。

## 边界
- 这个 skill 管“怎么把散乱输入收成一个有序系统”，不代替产品判断。
- 涉及产品方向、删数据、发布，必须停下来问用户，并给出“选 A / 选 B”的具体后果。
- 不重复造多 agent 框架：编排用现有 Codex 会话/worktree/skill；这里只做输入归一、真相沉淀、合并串行。
