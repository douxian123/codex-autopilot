# 产品主线（自动综合，勿手改）

> 生成时间 2026-11-21 08:41:15　·　由 `python3 ops/truthctl.py synthesize` 从真相层综合
> 产品方向以 `ops/truth/mainline.json` 为准（人拍板）；这一页是 Agent 从决策/进展/问题/验收里综合出来的现状。

## Codex 自动挡

给 Codex 装一个总输入框：零散需求进一个口，归类、落盘、合并、验证、问题收口由它自己串起来。

- 阶段：内部可用
- 北极星：用户提一次需求就闭环的比例
- 路径：输入归一：零散需求进唯一入口 → 真相沉淀：主线/决策/问题/进展/验收落盘 → 自运行：能合的自动合、能收口的自己收口 → 自追踪：产品主线自动综合
- 当前焦点：问题一次提出就闭环；产品讨论自动长成主线
- 本阶段不做：自建多 agent 框架（编排复用 Codex 会话 / worktree / skill）；改动共享 checkout 里别人的在途工作

## 产品路线（每一步到哪了）

- ✓ 唯一入口收需求：用户只在一个输入框里提零散问题，不用选对话、分支或角色　（已完成）
- ✓ 自动归类并写进项目真相：问题、决策、进展、验收都有稳定台账，跨会话不丢　（已完成）
- ✓ 安全分支自动落盘与冲突收口：能合的自动合，真冲突拆成任务，不再让用户逐条裁决　（已完成）
- ● 自动实现并收口：用户提一次需求后，系统自己实现、验证、落盘、关闭台账　（进行中）
- ● 路线与人机验收可见：面板说清产品路线、当前工作、还差什么，以及哪些需要人验收　（进行中）
- ● 常驻自追踪：没有对话时也持续同步项目主线、决策、进展与卡点　（下一步）

## 现在是什么样

- **唯一入口**：零散需求（视觉/体验/bug/架构/门禁/多分支）说一次即进唯一入口，由系统路由到固定泳道，不需要用户选对话、选分支、选角色。
- **真相沉淀与真实验收**：主线/决策/问题/进展/验收/判断逻辑落进 ops/truth；验收证据必须是真实跑过的命令+cwd+退出码，不允许用结论代替证据。
- **问题闭环**：bug/视觉/体验/架构进台账后由 agent 推进：修复 -> 验证 -> close 并追加进展；回归用 reopen 重开。
- **产品主线综合**：每轮收口跑 synthesize，把决策/进展/问题/验收/产品状态综合成 docs/PRODUCT_MAINLINE.md；人在 mainline.json 只拍板方向。
- **合并编排**：每个任务独立 worktree；合并前用 git merge-tree 预演，门禁通过且无冲突时自动 land 并同步 main 引用，不把要不要合推回用户。
- **常驻守护与面板**：面板显示运行状态、当前处理的分支合并逻辑、开关与必要信息；支持暂停/自动合入/自动拆冲突/常驻。
- **视觉/设计方案提示词**：做视觉/设计前先当产品经理+产品设计师，尊重用户、保护注意力：先截图看现状再改，改完再截图对比，只允许正向改动，不能更丑、更乱、更难读。
- **两平面架构**：用户仍从唯一输入框提需求；项目真相面负责预期、验收和是否值得打扰人，执行面负责路由、实现、合并与 harness 状态；全局健康只读聚合事实，不派活、不改配置、不替任何平面做决定。
- **PM 管家**：PM 管家只读聚合待确认预期、已生效约束、主线阶段、让路工作项和追问候选；文件占地重复时让新项排队且保留未知，不自动发追问，不替用户决定方向、删数据或发布。
- **验收管家**：人工验收项先由机器跑真实验证，只有带可复核命令、cwd、退出码的项才进入“需要你验收”；用户点击后才会调系统截图器补复核附件，补图只追加附件，不替用户收口。
- **全局项目健康**：全局健康从真相层、执行层和验收层已有事实聚合为 attention/unknown/off/ok 四态；取不到就说取不到，正常排队不报警，不新建分数台账，也不自动触发修复。
- **关系层**：跨项目长期目标通过 truthctl goal 显式登记，支持 summary/list/create/update/link/close；可关联工作项、验收项、来源窗口和收口证据，并在高级面板折叠区展示。聊天里的一句话不会自动升成生效目标；账本取不到显示 unknown。
- **守护首次安装**：install.sh 默认不替用户启用常驻；只有 --install-daemon 才首次安装守护。central_daemon.py --install 在 bootstrap 后回读 launchd，只有 running 且有 pid 才返回成功，否则不写常驻开启。
- **快照清理兼容**：snapshot_prune.py 使用 typing.Optional/List/Dict/Tuple，守护脚本固定用 /usr/bin/python3 3.9.6 执行；test-snapshot-clone-prune.py 每次门禁用 /usr/bin/python3 -m py_compile 回编译，旧版 Path | None 语法回归会直接红灯。

## 决策脉络

- 自动挡架构收敛为两平面七管家与一层全局健康 —— 继续横向加管家会把职责变成角色堆叠，无法说清谁拥有事实、谁执行、谁能改状态。两平面先分真相与执行，再以四层契约处理关系、规划、执行、运行，能保留万法归一入口，同时把写权限和人工介入边界写硬。
- 受控换代边界与 MCP 重载策略 —— 用户授权 A 是换代并重载自动挡自身运行面，不是授权扩大到宿主全局服务。必须把发布一致性、现场生效和会话内工具未热加载分开记账。

## 最近在推进

- [已落盘] @ fe7ae040c0 codex/pm-research-decision-20261120 验证门禁通过，已落盘 main @ fe7ae040c0；改动 13 个文件：README.md（+1/-0）、ops/central_mcp.py（+147/-0）、ops/kgctl.py（+18/-0）、ops/panel_logic_check.py（+7/-0）、ops/panel_render_check.py（+7/-0），另有 8 个文件｜13 files changed, 1099 insertions(+), 11 deletions(-)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0｜对应验收项：A-0152
- [已落盘] @ b827dba849 codex/cross-entry-route-control-20261120 验证门禁通过，已落盘 main @ b827dba849；改动 6 个文件：ops/central_mcp.py（+139/-6）、ops/kgctl.py（+39/-2）、ops/panel_logic_check.py（+44/-0）、ops/test-route-inbox.py（+60/-1）、ops/truth/acceptance.jsonl（+1/-0），另有 1 个文件｜6 files changed, 390 insertions(+), 9 deletions(-)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0｜对应验收项：A-0155、A-0156
- [已落盘] @ 1db2bc5563 codex/resolve-guard-release-version-20261119 验证门禁通过，已落盘 main @ 1db2bc5563；改动 2 个文件：ops/truth/acceptance.jsonl（+2/-0）、ops/truth/progress.jsonl（+1/-0）｜2 files changed, 3 insertions(+)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0
- [已落盘] @ 543f005f37 codex/resolve-ledger-normalization-20261120 验证门禁通过，已落盘 main @ 543f005f37；改动 1 个文件：ops/tasks/resolve.json（+51/-4）｜1 file changed, 51 insertions(+), 4 deletions(-)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0
- [在途] 0.6.0-preview.105 受控换代完成：marketplace、install-state、Companion、Codex 插件缓存、自动挡守护均指向 .105；守护 live pid 由旧版切到新版；产品评估 5/5 通过，阴性对照 8/8 变红。
- [已落盘] @ 3595c70326 codex/resolve-dedupe-load-20261120 验证门禁通过，已落盘 main @ 3595c70326；改动 2 个文件：ops/kgctl.py（+47/-1）、ops/selftest.py（+6/-1）｜2 files changed, 53 insertions(+), 2 deletions(-)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0
- [在途] 端到端闭环施工地图九阶段已按门禁闭环并推送 main
- [在途] 端到端闭环施工地图九阶段已按门禁闭环并推送 main
- [在途] launchd guard 随版本发布完整性门禁已覆盖并全量通过
- [已落盘] @ 51a8d2a7ff codex/project-autonomy-main-port-20261121 验证门禁通过，已落盘 main @ 51a8d2a7ff；改动 7 个文件：README.md（+10/-0）、ops/central_daemon.py（+97/-4）、ops/central_mcp.py（+79/-0）、ops/project_autonomy.py（+232/-0）、ops/selftest.py（+109/-1），另有 2 个文件｜7 files changed, 608 insertions(+), 6 deletions(-)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0｜对应验收项：A-0163、A-0164

## 关键验收证据（核对用，不替代 mainline.json）

- `A-0152` [P1] codex/pm-research-decision-20261120 门禁预跑（未落盘）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0153` [P1] codex/resolve-autopilot-truth-sync-failclosed 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0154` [P1] codex/pm-research-decision-20261120 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0155` [P1] codex/cross-entry-route-control-20261120 门禁预跑（未落盘）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0156` [P1] codex/cross-entry-route-control-20261120 门禁预跑（未落盘）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0157` [P1] codex/cross-entry-route-control-20261120 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0158` [P1] codex/resolve-guard-release-version-20261119 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0159` [P1] codex/resolve-ledger-normalization-20261120 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0161` [P1] 0.6.0-preview.105 换代验证
  - 证据：python3 ops/test-pm-research-decision.py -> 19/19 PASS；python3 ops/product_eval.py -> 可以对外（5/5 通过），阴性对照 8/8；plist/launchctl 指向 /Users/danta/plugins/codex-autopilot/releases/0.6.0-preview.105…
- `A-0162` [P1] codex/resolve-dedupe-load-20261120 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0164` [P1] codex/project-autonomy-main-port-20261121 门禁预跑（未落盘）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0165` [P1] codex/project-autonomy-main-port-20261121 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0

## 还差什么

**gate（1 待处理）**
- `L-0007` 【launchd 陈旧项·非 Stop 复发链】freshness 门禁真机实测 4 条 stale：ai.danta.codex-proxy-watchdog

## 需要你验收

- （当前没有需要你人工拍板的验收项）

## 已机器验收

- `A-0002` [P1] codex/pm-truth-closure-20261118 门禁预跑（未落盘）
- `A-0003` [P1] codex/pm-truth-closure-20261118 门禁预跑（未落盘）
- `A-0004` [P1] codex/truth-close-pm-truth-closure-20261118 门禁预跑（未落盘）
- `A-0005` [P1] codex/truth-close-pm-truth-closure-20261118 门禁预跑（未落盘）
- `A-0006` [P1] codex/release-lineage-gate-20261118 门禁预跑（未落盘）
- `A-0007` [P1] codex/truth-sync-union-20261118 门禁预跑（未落盘）
- `A-0008` [P1] codex/truth-restore-union-20261118 门禁预跑（未落盘）
- `A-0009` [P1] codex/gate-selftest-release-dir-20261118 门禁预跑（未落盘）
- `A-0139` [P1] codex/resolve-autopilot-parallel-runtime-fix-20261019 落盘验收
- `A-0140` [P1] codex/cli-help-guard-20261120 门禁预跑（未落盘）
- `A-0141` [P1] codex/resolve-autopilot-exec-recover-min-20261020 落盘验收
- `A-0071` [P1] 补记 codex/daemon-first-install-20261118 落盘验收（原 A-0006 被覆盖）
- `A-0072` [P1] codex/truth-restore-land-record-20261118 落盘验收
- `A-0073` [P1] codex/jsonl-truth-no-overwrite-20261118 落盘验收
- `A-0074` [P1] codex/state-root-freshness-20261118 落盘验收
- `A-0075` [P1] codex/truth-restore-union-20261118 落盘验收
- `A-0076` [P1] codex/gate-selftest-release-dir-20261118 落盘验收
- `A-0077` [P1] codex/tmp-owner-cleanup-20261118 门禁预跑（未落盘）
- `A-0078` [P1] codex/tmp-owner-cleanup-20261118 落盘验收
- `A-0081` [P1] codex/tmp-owner-cleanup-20261118 门禁预跑（未落盘）
- `A-0082` [P1] codex/tmp-owner-cleanup-20261118 落盘验收
- `A-0083` [P1] codex/panel-windshield-20261119 落盘验收
- `A-0084` [P1] codex/guard-release-version-20261119 门禁预跑（未落盘）
- `A-0088` [P1] codex/guard-release-version-20261119 门禁预跑（未落盘）
- `A-0085` [P1] codex/guard-release-version-20261119 落盘验收
- `A-0086` [P1] codex/guard-release-version-20261119 落盘验收
- `A-0087` [P1] codex/panel-route-contract-20261119 落盘验收
- `A-0089` [P1] codex/guard-release-version-20261119 门禁预跑（未落盘）
- `A-0090` [P1] codex/truth-close-open-ledger-20261119 落盘验收
- `A-0091` [P1] codex/guard-release-version-20261119 门禁预跑（未落盘）
- `A-0092` [P1] codex/guard-release-version-20261119 落盘验收
- `A-0094` [P1] codex/selftrack-mainline-20261119 落盘验收
- `A-0095` [P1] codex/panel-interaction-design-20261119 落盘验收
- `A-0096` [P1] codex/panel-compact-health-20261119 落盘验收
- `A-0097` [P1] codex/route-health-20261119 落盘验收
- `A-0098` [P1] codex/panel-attention-funnel-20261119 落盘验收
- `A-0099` [P1] codex/context-self-learning-20261119 落盘验收
- `A-0100` [P1] codex/panel-attention-hierarchy-20261119 落盘验收
- `A-0101` [P1] codex/panel-human-copy-20261119 落盘验收
- `A-0102` [P1] codex/authorized-followup-20261119 落盘验收
- `A-0103` [P1] codex/context-self-learning-20261119 落盘验收
- `A-0104` [P1] codex/panel-attention-final-copy-20261119 落盘验收
- `A-0105` [P1] codex/preview103-live-release-truth-20261119 落盘验收
- `A-0107` [P1] codex/authorized-followup-20261119 落盘验收
- `A-0108` [P1] codex/arch-boundaries-port-20261119 落盘验收
- `A-0109` [P1] codex/steward-contract-20261119 落盘验收
- `A-0110` [P1] codex/context-learning-remote-identity-20261119 落盘验收
- `A-0111` [P1] codex/context-learning-noise-guard-20261119 落盘验收
- `A-0112` [P1] codex/release-selftest-fixture-20261119 落盘验收
- `A-0113` [P1] codex/route-review-20261119 落盘验收
- `A-0114` [P1] codex/release-docs-fixture-20261119 落盘验收
- `A-0115` [P1] codex/runtime-docs-assets-20261119 落盘验收
- `A-0116` [P1] codex/cross-project-constraint-guard-20261119 落盘验收
- `A-0117` [P1] codex/panel-experience-design-20261119 落盘验收
- `A-0118` [P1] codex/context-archive-20261119 落盘验收
- `A-0119` [P1] codex/install-clean-source-fixture-20261119 落盘验收
- `A-0120` [P1] codex/resolve-route-review-20261119 落盘验收
- `A-0121` [P1] codex/runtime-freshness-20261119b 落盘验收
- `A-0123` [P1] codex/pm-closure-loop-20261119 落盘验收
- `A-0124` [P1] codex/native-panel-design-contract-20261119 落盘验收
- `A-0125` [P1] codex/acceptance-loop-20261119 落盘验收
- `A-0126` [P1] codex/native-panel-truth-close-20261119 落盘验收
- `A-0127` [P1] codex/release-contract-final-20261119b 落盘验收
- `A-0128` [P1] codex/runtime-freshness-20261119c 落盘验收
- `A-0129` [P1] codex/release-preview-104-20261119 落盘验收
- `A-0130` [P1] codex/runtime-loop-20261119 落盘验收
- `A-0131` [P1] codex/context-release-104-20261120 落盘验收
- `A-0132` [P1] codex/release-contract-layout-20261119 落盘验收
- `A-0133` [P1] codex/resolve-runtime-freshness-20261119b 落盘验收
- `A-0122` [P1] codex/release-contract-layout-20261119 落盘验收
- `A-0134` [P1] codex/resolve-install-architecture-doc-20261119 落盘验收
- `A-0135` [P1] codex/global-routing-review-20261120 落盘验收
- `A-0136` [P1] codex/resolve-snapshot-budget-archive-20261113 落盘验收
- `A-0137` [P1] codex/resolve-autopilot-close-gate-20261026 落盘验收
- `A-0142` [P1] codex/resolve-autopilot-resolve-stderr 落盘验收
- `A-0147` [P1] codex/resolve-autopilot-routing-context-20261119 落盘验收
- `A-0148` [P1] codex/resolve-eval-loop-judge-20261110 落盘验收
- `A-0149` [P1] codex/resolve-resource-admission-verify-narrowing-20261115 落盘验收
- `A-0151` [P1] codex/resolve-reap-branch-preserve-20261111 落盘验收
- `A-0155` [P1] codex/cross-entry-route-control-20261120 门禁预跑（未落盘）
- `A-0093` [P1] codex/guard-release-version-20261119 门禁预跑（未落盘）
- `A-0153` [P1] codex/resolve-autopilot-truth-sync-failclosed 落盘验收
- `A-0152` [P1] codex/pm-research-decision-20261120 门禁预跑（未落盘）
- `A-0154` [P1] codex/pm-research-decision-20261120 落盘验收
- `A-0156` [P1] codex/cross-entry-route-control-20261120 门禁预跑（未落盘）
- `A-0157` [P1] codex/cross-entry-route-control-20261120 落盘验收
- `A-0158` [P1] codex/resolve-guard-release-version-20261119 落盘验收
- `A-0159` [P1] codex/resolve-ledger-normalization-20261120 落盘验收
- `A-0161` [P1] 0.6.0-preview.105 换代验证
- `A-0162` [P1] codex/resolve-dedupe-load-20261120 落盘验收
- `A-0164` [P1] codex/project-autonomy-main-port-20261121 门禁预跑（未落盘）
- `A-0165` [P1] codex/project-autonomy-main-port-20261121 落盘验收

## 进度概览

- 累计关闭问题：13 条
- 累计落盘：92 条
- 未决问题：1 条
- 已验收：92 条

