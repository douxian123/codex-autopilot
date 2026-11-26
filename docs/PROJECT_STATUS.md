# 项目状态（自动生成，勿手改）

> 生成时间 2026-11-27 05:52:50　·　由 `python3 ops/truthctl.py` 从 `ops/truth/` 生成　·　改状态请改台账，不要改本文件。

## 一、产品主线

**Codex 自动挡** — 给 Codex 装一个总输入框：零散需求进一个口，归类、落盘、合并、验证、问题收口由它自己串起来。

- 阶段：内部可用
- 北极星：用户提一次需求就闭环的比例
- 路径：输入归一：零散需求进唯一入口 → 真相沉淀：主线/决策/问题/进展/验收落盘 → 自运行：能合的自动合、能收口的自己收口 → 自追踪：产品主线自动综合
- 当前焦点：问题一次提出就闭环；产品讨论自动长成主线；机器闭环与真实桌面人工验收分界明确
- 明确不做：自建多 agent 框架（编排复用 Codex 会话 / worktree / skill）；改动共享 checkout 里别人的在途工作
- 最后更新：2026-10-18

**产品路线（每一步到哪了）**
- ✓ 唯一入口收需求：用户只在一个输入框里提零散问题，不用选对话、分支或角色　（已完成）
- ✓ 自动归类并写进项目真相：问题、决策、进展、验收都有稳定台账，跨会话不丢　（已完成）
- ✓ 安全分支自动落盘与冲突收口：能合的自动合，真冲突拆成任务，不再让用户逐条裁决　（已完成）
- ● 自动实现并收口：用户提一次需求后，系统自己实现、验证、落盘、关闭台账　（进行中）
- ● 路线与人机验收可见：面板说清产品路线、当前工作、还差什么，以及哪些需要人验收　（进行中）
- ✓ 常驻自追踪：没有对话时也持续同步项目主线、决策、进展与卡点；守护启停入口已实现并机器验证，真机常驻需安装验收　（已完成）
- ✓ 总开关与统一本机控制面：一个总闸决定外挂是否工作；关掉就回到 Codex 原生状态，所有入口共用一个控制通道，不各自维护任务　（已完成）
- ✓ Codex 原生风格侧栏面板：首屏只回答：开没开、当前绑定哪个项目、正在处理什么、下一步、是否需要我介入、最近完成了什么；路线与证据收进二级（首屏数据流已机器验证，观感仍需人眼过一遍）　（已完成）
- ● 跨应用入口：在任何工作上下文都能把问题交进来：全局快捷键、截图、附件、系统服务菜单，全部落到同一个入口　（进行中）
- ● 菜单栏 Companion 状态锚点：永远知道自动挡在哪、开没开、有没有事需要我处理；它只做锚点，不复制第二套路线管理　（进行中）
- ✓ 版本化安装与回滚：升级只切版本指针，旧版本目录与旧账本一律保留，失败可一键回退　（已完成）
- ● 原生 thread 直连派活：用 Codex 官方 app-server 协议建原生对话、自动归入本项目的窗口集合、持续 follow-up；协议已用真实 codex 二进制验证（含窗口集合创建与移动方法名），派进用户桌面窗口需在不受限环境验收　（进行中）
- ● 真实端到端演示与预览发布：一条真实需求从入口走到收口，每一步留下命令、cwd、退出码、commit　（进行中）

## 二、需要你注意

**待修的问题（bug / 架构）**
- `L-0020` 按你推荐的来，这些肯定不是我发现、修好，然后我自己能用就好了。肯定是它作为一个完整的、能够对外发表的、能让普通人也用得明白的顺畅产品为标准。

**等你确认（机器已做完，好不好只有你能判）**
- `L-0019` 打开自动挡面板自然语言触发不稳定：用户说“打开自动挡面板”后，Codex 会话只分析入口、没有调用 render_autopilot_panel

## 三、问题台账

**gate（1 待处理）**
- `L-0020` 按你推荐的来，这些肯定不是我发现、修好，然后我自己能用就好了。肯定是它作为一个完整的、能够对外发表的、能让普通人也用得明白的顺畅产品为标准。 → `gate`

**ux（1 待处理）**
- `L-0021` 这个面板现在真的能够正常运行在运作吗？怎么全都是报红点点报错还是什么东西？而且这个面板现在的信息承载量怎么少了这么多？连最基础的施工地图、整体进度、项目文件夹这 → `design`

（历史已关闭 16 条）

## 四、进展

- `P-0259` [已落盘] @ `39d49bd5cd` codex/resolve-truth-sync-covered-20261125 验证门禁通过，已落盘 main @ 39d49bd5cd；改动 2 个文件：ops/truth/acceptance.jsonl（+1/-0）、ops/truth/progress.jsonl（+1/-0）｜2 files changed, 2 insertions(+)｜门禁：experience-contract exit 0；selftest exit 0；panel-logic exit 0；panel-render exit 0
- `P-0260` [已归档] @ `9845fb9e97` codex/public-beta-docs-closure-20261124 相对自身基线的独有内容已逐文件确认全部存在于 main，属重复分支，不产生空合并提交。
- `P-0261` [已归档] @ `300ea39314` codex/disk-admission-fix-20261105 相对自身基线的独有内容已逐文件确认全部存在于 main，属重复分支，不产生空合并提交。
- `P-0262` [已归档] @ `07b17402d8` codex/disk-reap-into-repo 相对自身基线的独有内容已逐文件确认全部存在于 main，属重复分支，不产生空合并提交。
- `P-0263` [已落盘] @ `73da84b973` codex/fix-stale-resolve-worktree-20261125 验证门禁通过，已落盘 main @ 73da84b973；改动 3 个文件：ops/kgctl.py（+80/-9）、ops/selftest.py（+1/-0）、ops/test-stale-resolve-worktree-registration.py（+151/-0）｜3 files changed, 232 insertions(+), 9 deletions(-)｜门禁：experience-contract exit 0；selftest exit 0；panel-logic exit 0；panel-render exit 0
- `P-0264` [已落盘] @ `8c20327973` codex/resolve-reap-identical-tracked-20261027 验证门禁通过，已落盘 main @ 8c20327973；改动：本次合并未带进新内容（分支内容已在主干，属空合并）｜门禁：experience-contract exit 0；selftest exit 0；panel-logic exit 0；panel-render exit 0
- `P-0265` [已落盘] @ `4c5d6d2b8b` codex/resolve-truth-baseline-20261125 验证门禁通过，已落盘 main @ 4c5d6d2b8b；改动 3 个文件：ops/kgctl.py（+43/-3）、ops/selftest.py（+1/-0）、ops/test-resolve-truth-baseline.py（+177/-0）｜3 files changed, 221 insertions(+), 3 deletions(-)｜门禁：experience-contract exit 0；selftest exit 0；panel-logic exit 0；panel-render exit 0｜对应验收项：A-0257
- `P-0266` [已落盘] @ `967801da6f` codex/land-resolve-task-status-20261126 验证门禁通过，已落盘 main @ 967801da6f；改动 2 个文件：ops/kgctl.py（+63/-2）、ops/test-resolve-truth-baseline.py（+33/-0）｜2 files changed, 96 insertions(+), 2 deletions(-)｜门禁：experience-contract exit 0；selftest exit 0；panel-logic exit 0；panel-render exit 0｜对应验收项：A-0259

## 五、验收进度

**需要你验收（8）**
- `A-0022` [P0] 需要用户决策/验收时主动提醒并说明下一步
  - 1) 在正常 macOS 终端安装并打开 Companion 2) 制造或等待一项人工验收/需要拍板事项 3) 确认菜单栏出现警示图标和 macOS 通知 4) 打开 Codex 面板确认写清具体动作 5) 清掉事项后确认不再重复提醒
- `A-0044` [P0] preview.11 真实桌面宿主原生派活与 Companion 进程就绪（机器自证，人工验收仍待确认）
  - 1) 打开菜单栏 Companion 2) 看到自动挡图标 3) 点开看到总开关/当前任务/派活入口 4) ⌥⌘D 回到 Codex 完整面板
- `A-0190` [P1] Codex Autopilot 面板人工验收：注意力顺序
  - 人看首屏默认截图，确认当前项目、进度、责任状态、实施进度的阅读顺序符合预期
- `A-0191` [P1] Codex Autopilot 面板人工验收：信息密度
  - 人看首屏默认截图，确认信息不过密、不过空，无无名圆点、无橙色提醒块、无重复折叠入口
- `A-0192` [P1] Codex Autopilot 面板人工验收：任务完成感
  - 人看首屏默认截图，确认当前目标、总体进度、最近完成和当前步骤能支撑判断任务是否在推进
- `A-0187` [P1] Codex Autopilot 面板人工验收：注意力顺序
  - 人看首屏默认截图，确认当前项目、进度、责任状态、实施进度的阅读顺序符合预期
- `A-0188` [P1] Codex Autopilot 面板人工验收：信息密度
  - 人看首屏默认截图，确认信息不过密、不过空，无无名圆点、无橙色提醒块、无重复折叠入口
- `A-0189` [P1] Codex Autopilot 面板人工验收：任务完成感
  - 人看首屏默认截图，确认当前目标、总体进度、最近完成和当前步骤能支撑判断任务是否在推进

**已机器验收（182）**
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
- `A-0170` [P1] codex/experience-evidence-gate-20261121 落盘验收
- `A-0172` [P1] codex/context-projection-stale-wt-20261122 门禁预跑（未落盘）
- `A-0174` [P1] codex/context-projection-stale-wt-20261122 门禁预跑（未落盘）
- `A-0173` [P1] codex/codesign-clone-reap-resilience-20261122 落盘验收
- `A-0175` [P1] codex/context-projection-stale-wt-20261122 落盘验收
- `A-0176` [P1] codex/context-projection-stale-wt-20261122 门禁预跑（未落盘）
- `A-0177` [P1] codex/context-projection-stale-wt-20261122 落盘验收
- `A-0178` [P1] codex/installer-source-provenance-20261122 落盘验收
- `A-0179` [P1] codex/disk-gate-immediate-reap-20261122 落盘验收
- `A-0180` [P1] codex/truthctl-runtime-precedence-20261122 门禁预跑（未落盘）
- `A-0181` [P1] codex/truthctl-runtime-precedence-20261122 落盘验收
- `A-0182` [P1] codex/release-124-truthctl-runtime-20261122 落盘验收
- `A-0183` [P1] 动态验收专项执行器远端合入门禁验收
- `A-0011` [P0] 全量自检 20 组通过（覆盖 A-0002 的组数）
- `A-0012` [P0] 面板首屏（含关闭态）每个值都来自真实控制层数据
- `A-0013` [P0] 装出来的副本自带绑定：kgctl 认得出项目、MCP 能应答（不带任何环境变量）
- `A-0014` [P0] 全量自检 22 组通过（含后台常驻开关真启停）
- `A-0015` [P0] 全量自检 22 组通过（含后台常驻开关真启停，机器自证）
- `A-0016` [P0] 派活自动归入项目窗口集合：同名复用、不重复创建、失败不拦派活
- `A-0017` [P0] 原生协议与真实 codex 二进制对齐：修正窗口集合移动方法名并真机验证
- `A-0018` [P0] 全量自检 23 组通过（含真二进制开关 24 组）
- `A-0019` [P0] 面板按官方嵌套字段显示窗口集合内的子对话
- `A-0020` [P1] 菜单栏完整面板回到 Codex 本体，HTML 驾驶舱降为备用
- `A-0021` [P0] 原生方法名全量对真实服务器核对（6/6 存在）
- `A-0023` [P1] codex/panel-project-followup 落盘验收
- `A-0024` [P0] 北极星首问闭环率会算、算不出就红、且首屏可见
- `A-0026` [P1] 本机已安装 0.6.0-preview.6，marketplace/install-state/Codex 缓存全部指向 preview.6
- `A-0029` [P1] 本机已安装 0.6.0-preview.8，marketplace/install-state/Codex 缓存/Companion 配置全部指向 preview.8
- `A-0028` [P1] codex/port-panel-route-closure-2 落盘验收
- `A-0030` [P1] preview.9 发布树机器验收：Companion 真编译 + 真实 codex 协议
- `A-0031` [P1] codex/verify-preview9-real-codex 落盘验收
- `A-0032` [P0] 真实需求 auto_exec 机器闭环：入口->归类->独立 worktree->真实 codex->门禁->land->收口
- `A-0033` [P1] codex/release-preview10 落盘验收
- `A-0034` [P0] 真实桌面宿主原生派活：dispatch 建 native thread/turn/section
- `A-0036` [P0] preview.10 机器全量验收：自检/面板逻辑/真实渲染/原生直连/安装产物全部通过
- `A-0037` [P1] Companion 真机运行与环境入口就绪（进程、服务菜单、通知、截图快捷键、拖拽入口均有真实注册）
- `A-0038` [P1] Companion 真机运行与环境入口就绪（机器自证已验）
- `A-0039` [P1] codex/release-preview10 落盘验收
- `A-0041` [P0] preview.11 原生直连与全量预览验收通过
- `A-0042` [P0] preview.11 真实桌面宿主原生派活（thread/turn/section）通过
- `A-0043` [P1] codex/native-socket-lstat-v7 落盘验收
- `A-0045` [P1] 真实入口端到端机器闭环：Companion 派需求->归类->台账->面板逻辑检查->收口
- `A-0046` [P1] Companion 作为 UIElement 菜单栏应用在系统注册且驾驶舱可打开（机器自证）
- `A-0184` [P1] codex/resolve-land-clean-worktree-gate-20261122 落盘验收
- `A-0185` [P1] codex/panel-native-surface-20261123 门禁预跑（未落盘）
- `A-0186` [P1] codex/panel-native-surface-20261123 门禁预跑（未落盘）
- `A-0197` [P1] codex/route-ownership-contract-20261123 落盘验收
- `A-0198` [P1] 面板首屏四项恢复的机器门禁验收
- `A-0201` [P1] codex/rollback-install-state-20261124 落盘验收
- `A-0206` [P1] codex/release-doc-truth-alignment-20261124 落盘验收
- `A-0207` [P1] codex/project-health-mainline-freshness-20261124 落盘验收
- `A-0208` [P1] codex/acceptance-id-collision-20261124 落盘验收
- `A-0209` [P1] codex/experience-evidence-focus-measure-20261124 落盘验收
- `A-0210` [P1] codex/fix-superseded-queue-20261124 落盘验收
- `A-0212` [P1] codex/fix-converge-orphan-adopt-20261124 落盘验收
- `A-0214` [P1] codex/truth-closure-audit-20261124 门禁预跑（未落盘）
- `A-0215` [P1] codex/truth-closure-audit-20261124 落盘验收
- `A-0216` [P1] codex/fix-stale-resolve-abandon-20261124 落盘验收
- `A-0219` [P1] codex/fix-gate-experience-base-20261124 落盘验收
- `A-0221` [P1] codex/truth-gate-false-green-correction-20261124 落盘验收
- `A-0230` [P1] codex/port-identity-guard-20261124 门禁预跑（未落盘）
- `A-0231` [P1] codex/port-identity-guard-20261124 落盘验收
- `A-0202` [P1] 面板秘书简报与工具一致性机器验收
- `A-0203` [P1] codex/fix-resolve-stale-adopt-guard-20261124 门禁预跑（未落盘）
- `A-0204` [P1] codex/fix-resolve-stale-adopt-guard-20261124 门禁预跑（未落盘）
- `A-0232` [P1] codex/fix-resolve-stale-adopt-guard-20261124 落盘验收
- `A-0233` [P1] codex/public-beta-docs-closure-20261124 门禁预跑（未落盘）
- `A-0234` [P1] codex/public-beta-docs-closure-20261124 落盘验收
- `A-0235` [P1] codex/resolve-panel-design-restore-20261124 门禁预跑（未落盘）
- `A-0237` [P1] codex/experience-gate-target-runner-20261124 落盘验收
- `A-0240` [P1] codex/acceptance-id-worktrees-20261124 门禁预跑（未落盘）
- `A-0241` [P1] codex/acceptance-id-worktrees-20261124 落盘验收
- `A-0243` [P1] codex/b1-final-closure-20261124 门禁预跑（未落盘）
- `A-0244` [P1] codex/b1-final-closure-20261124 落盘验收
- `A-0245` [P1] codex/final-progress-closure-20261124 落盘验收
- `A-0246` [P1] 绑定漂移 fail-closed 机器验收
- `A-0247` [P1] codex/truth-sync-clean-20261124 落盘验收
- `A-0248` [P1] codex/synthesize-status-gate-20261125 落盘验收
- `A-0249` [P1] codex/truth-sync-clean-20261124 落盘验收
- `A-0250` [P1] codex/session-compact-danger-switch-20261125 落盘验收
- `A-0251` [P1] codex/fix-truth-sync-stale-checkout-20261125 落盘验收
- `A-0252` [P1] codex/fix-truth-push-stale-head-20261125 落盘验收
- `A-0253` [P1] codex/fix-headless-chrome-browser-name-20261125 落盘验收
- `A-0254` [P1] codex/resolve-truth-sync-covered-20261125 落盘验收
- `A-0255` [P1] codex/fix-stale-resolve-worktree-20261125 落盘验收
- `A-0256` [P1] codex/resolve-reap-identical-tracked-20261027 落盘验收
- `A-0257` [P1] codex/resolve-truth-baseline-20261125 门禁预跑（未落盘）
- `A-0258` [P1] codex/resolve-truth-baseline-20261125 落盘验收
- `A-0259` [P1] codex/land-resolve-task-status-20261126 门禁预跑（未落盘）
- `A-0260` [P1] codex/land-resolve-task-status-20261126 落盘验收

## 六、决策记录

- `D-0001` 自动挡架构收敛为两平面七管家与一层全局健康
- `D-0003` 受控换代边界与 MCP 重载策略
- `D-0002` 北极星首问闭环率的唯一口径
- `D-0005` B1 路线已定：公开 beta 保持 .140

## 七、产品状态（产品今天是怎么工作的）

- **唯一入口**：零散需求（视觉/体验/bug/架构/门禁/多分支）说一次即进唯一入口，由系统路由到固定泳道，不需要用户选对话、选分支、选角色。
  - 为什么：真实工作思考和想法是零碎的、即时的；让用户自己挑对话/分支/角色就是把结构性工作推回给用户
- **真相沉淀与真实验收**：主线/决策/问题/进展/验收/判断逻辑落进 ops/truth；验收证据必须是真实跑过的命令+cwd+退出码，不允许用结论代替证据。
  - 为什么：跨会话存活是路由与合并的共同依据；验证只认真实执行过的命令，假绿灯比红灯更危险
- **问题闭环**：bug/视觉/体验/架构进台账后由 agent 推进：修复 -> 验证 -> close 并追加进展；回归用 reopen 重开。
  - 为什么：用户提一次就应闭环；挂在 open 或没验证就标 done 都会让台账失真
- **产品主线综合**：每轮收口跑 synthesize，把决策/进展/问题/验收/产品状态综合成 docs/PRODUCT_MAINLINE.md；人在 mainline.json 只拍板方向。
  - 为什么：零散讨论应自动长成产品主线，人只读一页，不读实现
- **合并编排**：每个任务独立 worktree；合并前用 git merge-tree 预演，门禁通过且无冲突时自动 land 并同步 main 引用，不把要不要合推回用户。
  - 为什么：并行任务共享同一 checkout 会互相污染，移动靶最难查；能干净合并就不该把决策推回给用户
- **常驻守护与面板**：面板显示运行状态、当前处理的分支合并逻辑、开关与必要信息；支持暂停/自动合入/自动拆冲突/常驻。
  - 为什么：无感的外挂没人敢信；用户要能随时看到它开着没、在做什么、以及随时停
- **视觉/设计方案提示词**：做视觉/设计前先当产品经理+产品设计师，尊重用户、保护注意力：先截图看现状再改，改完再截图对比，只允许正向改动，不能更丑、更乱、更难读。
  - 为什么：视觉改动最容易越改越丑；把设计要求写进 harness 才能让每次改动都守住注意力秩序
- **主动注意提醒**：出现人工验收、需要拍板或异常时，主面板显示明确待处理状态，菜单栏切换警示图标并通过 macOS 通知中心提醒；同一批待办只提醒一次。
- **自动挡合并队列与真相投影**：已归档（superseded）的分支不再出现在落盘队列或一屏在途项，队列会单列已裁决/已归档数量；真相快照把这类进展显示为「已归档」而不是「在途」。
  - 为什么：修复队列把已归档但无冲突的历史分支反复当成可落盘在途的假信号。
- **绑定漂移**：插件绑定旧脏 worktree、当前会话在同仓库干净状态根时，binding_health 报漂移；capture_request、intake_request、route_preview 在写入前返回 AUTOPILOT_BINDING_DRIFT，并提示重跑 install.sh <当前状态根> 后重开 Codex；干净多 worktree 和安装自检不误报。
  - 为什么：2026-11-24 现场发现插件 .mcp.json 绑定旧脏 worktree 时，当前干净状态根的需求会被写进旧账本，面板却显示正常。
- **验收结论口径**：当前知识图谱走查修复分支已提交推送但尚未合入 main；真实浏览器复放和 30 条全量重跑未完成，因此不能宣称符合预期或全产品闭环生效。
  - 为什么：分支 codex/user-audit-quality-closure-r3-20261124 commit 2dabbeb4e6 已推送，main 仍是 fcd634cf99；资源准入拦截浏览器重验证。

## 八、判断逻辑（同一个判断，人只做一次）

- `J-0003` [product] 机器闭环和人工验收不能混为一谈
- `J-0029` [process] 用户明确要求中心窗口作为产品总监/调度者时，应主动派活并监视其他窗口，而不是在一个窗口死磕；派活后中心窗口仍需负责集成、门禁、合并和四段式收口。
- `J-0017` [process] 源码合并不等于现场生效：换代必须回读 install-state、release 元数据、daemon plist 和 live PID 四重一致；取不到写 unknown，不写 0 或正常。 --scope workflow --why 2026-11-22 宪法页修复在源码和手工 tick 已通过，但当前安装版 .121 仍不含修复；只有受控换代并回读现场才能证明用户现场生效。
- `J-0018` [workflow] 源码合并不等于现场生效：换代必须回读四重现场证据
- `J-0020` [process] 诊断「部分 Codex 对话持续报 upstream_stream_cut」时，先按精确错误对象匹配 emitter，再用 proxy.log 的 ROUTE 行按 thread_id 核对实际模型线路；同一时刻其他线程正常不能排除单线程绑定坏线路。
- `J-0023` [process] 验收问题必须先证明当前源码和当前 main 的基线，再区分本轮回归、门禁字面量漂移和主线既有红灯；冲击性措辞只作为缺陷线索，不作为已修复或已闭环的证据。
- `J-0024` [product] 真实用户走查的完成判定必须按题同时看终态、首值、内容相关性、移动端可见性和刷新后状态；done 帧出现但只有一张无证据残句，或 30 秒后出现大量跑题来源，都不能算符合预期。
- `J-0021` [design] 原生窄面板压缩时，项目卡（含整体进度和一句汇报简语）、施工地图、项目文件夹、管家健康是信息承载底线；后台候选与历史冲突不能用橙色报错语义表达。
- `J-0025` [process] 公开 beta 的用户可见文档只有在对应 tag/prerelease 或公开文档站更新后才算交付；源码 main 落盘只能证明工程闭环，不能写成公开用户现场已经看到。
- `J-0026` [process] 落盘门禁必须从正在验证的目标树加载门禁模块，不能从当前已安装 runner 的 ops 目录加载；否则目标树已修好的证据会被旧 runner 否定，形成机器假红。
- `J-0027` [process] 当旧 runner 与当前目标树证据冲突时，以目标树可复现的机器证据为准；机器全绿但人工体验未签收时停在人工闸口，不代签也不重复跑同一失败命令。
- `J-0028` [process] 陈旧机器验收要看最新证据：被后续 verified/landed/superseded 或目标树复核覆盖的旧 todo 转 void，不删除历史；人工验收项保持 todo 等待真人拍板。
