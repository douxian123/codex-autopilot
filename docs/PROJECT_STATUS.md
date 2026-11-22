# 项目状态（自动生成，勿手改）

> 生成时间 2026-11-22 18:08:59　·　由 `python3 ops/truthctl.py` 从 `ops/truth/` 生成　·　改状态请改台账，不要改本文件。

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
- `L-0007` 【launchd 陈旧项·非 Stop 复发链】freshness 门禁真机实测 4 条 stale：ai.danta.codex-proxy-watchdog

**等你确认（机器已做完，好不好只有你能判）**
- `L-0019` 打开自动挡面板自然语言触发不稳定：用户说“打开自动挡面板”后，Codex 会话只分析入口、没有调用 render_autopilot_panel

## 三、问题台账

**gate（1 待处理）**
- `L-0007` 【launchd 陈旧项·非 Stop 复发链】freshness 门禁真机实测 4 条 stale：ai.danta.codex-proxy-watchdog → `gate`

（历史已关闭 14 条）

## 四、进展

- `P-0200` [已落盘] @ `2f1a0398bd` 网络恢复后单次核验并推送：curl -sS -o /dev/null -w "%{http_code}\n" --connect-timeout 5 --max-time 10 https://github.com -> 200, CURL_EXIT=0；git fetch origin main -> FETCH_EXIT=0, LOCAL=2f1a0398bd2875cbf4bc21c243b62a09b93e1451, ORIGIN_MAIN=60aa31beb80fb010777ba562ae7cf92d1fd3babd, ANCESTOR_EXIT=0；git push origin codex/release-124-truthctl-runtime-20261122:main -> 60aa31be..2f1a0398, PUSH_EXIT=0；git ls-remote origin refs/heads/main -> 2f1a0398bd2875cbf4bc21c243b62a09b93e1451 refs/heads/main, LS_REMOTE_EXIT=0。本机现场：install-state 0.6.0-preview.125, source_commit=2f1a0398bd2875cbf4bc21c243b62a09b93e1451；daemon PID 70368；runtime-install-contract status ok, unknown=[], failures=[]。
- `P-0201` [已落盘] @ `780021fb69` codex/release-124-truthctl-runtime-20261122 验证门禁通过，已落盘 main @ 780021fb69；改动 11 个文件：docs/PRE_ACCEPTANCE_STANDARD.md（+27/-6）、docs/PRODUCT_CONTRACT.md（+1/-1）、ops/acceptance_dictionary.py（+46/-9）、ops/central_mcp.py（+19/-0）、ops/dynamic_acceptance_scope.py（+21/-8），另有 6 个文件｜11 files changed, 276 insertions(+), 40 deletions(-)｜门禁：experience-contract exit 0；selftest exit 0；panel-logic exit 0；panel-render exit 0
- `P-0203` [已落盘] @ `c31ee080b3` cwd=/Users/danta/.newmax/workspace/ca-truthctl-runtime-precedence-20261122-trunk-trunk; python3 ops/test-truth-sync-ignored-views.py -> EXIT=0; python3 ops/test-specialized-acceptance-executors.py -> EXIT=0; python3 ops/test-pre-acceptance.py -> EXIT=0; python3 ops/test-acceptance-dictionary.py -> EXIT=0; python3 ops/test-dynamic-acceptance-scope.py -> EXIT=0; python3 ops/test-generate-acceptance-scope-tool.py -> EXIT=0; python3 ops/test-acceptance-evidence.py -> EXIT=0; python3 ops/test-gate-registration.py -> EXIT=0; python3 -m py_compile ... -> PY_COMPILE_EXIT=0; python3 -u ops/selftest.py -> SELFTEST_EXIT=0; git push origin HEAD:refs/heads/main -> PUSH_EXIT=0; git ls-remote origin refs/heads/main -> c31ee080b3efb2202acd62b6af53aef29b8f3ef3
- `P-0204` [已落盘] @ `c31ee080b3` 动态验收专项执行器已进入 origin/main；专项和最小充分范围门禁通过；现场安装版本暂未换代，未宣称用户现场已生效
- `P-0206` [在途] 机器侧已修并换代到 live .128：setup/MCP 触发文案已落盘，trigger/gate/panel 渲染门禁与 MCP 实调全 PASS；宿主面板视觉与真实打开需用户新开会话确认
- `P-0207` [已落盘] @ `a66902bcb0` codex/resolve-land-clean-worktree-gate-20261122 验证门禁通过，已落盘 main @ a66902bcb0；改动 1 个文件：CONTEXT.md（+8/-0）｜1 file changed, 8 insertions(+)｜门禁：experience-contract exit 0；selftest exit 0；panel-logic exit 0；panel-render exit 0
- `P-0208` [在途] @ `300ea39314` codex/disk-admission-fix-20261105 相对自身基线的独有内容已逐文件确认全部存在于 main，属重复分支，不产生空合并提交。
- `P-0209` [在途] @ `07b17402d8` codex/disk-reap-into-repo 相对自身基线的独有内容已逐文件确认全部存在于 main，属重复分支，不产生空合并提交。

## 五、验收进度

**需要你验收（2）**
- `A-0022` [P0] 需要用户决策/验收时主动提醒并说明下一步
  - 1) 在正常 macOS 终端安装并打开 Companion 2) 制造或等待一项人工验收/需要拍板事项 3) 确认菜单栏出现警示图标和 macOS 通知 4) 打开 Codex 面板确认写清具体动作 5) 清掉事项后确认不再重复提醒
- `A-0044` [P0] preview.11 真实桌面宿主原生派活与 Companion 进程就绪（机器自证，人工验收仍待确认）
  - 1) 打开菜单栏 Companion 2) 看到自动挡图标 3) 点开看到总开关/当前任务/派活入口 4) ⌥⌘D 回到 Codex 完整面板

**已机器验收（136）**
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

## 六、决策记录

- `D-0001` 自动挡架构收敛为两平面七管家与一层全局健康
- `D-0003` 受控换代边界与 MCP 重载策略
- `D-0002` 北极星首问闭环率的唯一口径

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

## 八、判断逻辑（同一个判断，人只做一次）

- `J-0010` [gate] 面板新增能力必须同步进工具契约门禁和渲染状态门禁，不能只靠功能测试：功能合入不等于回归防线存在。
- `J-0011` [process] 修复完成后必须同步翻 ledger 状态，否则闭环率会把已修复问题算成未闭环。
- `J-0012` [arch] 常驻自追踪必须幂等：真相内容没变时不重写生成视图和生成时间。
- `J-0013` [architecture] 管家是责任视图而非人类岗位表；运行时按 execute/control/evaluate 分工，状态管家的路线效用职责只做只读检测与授权后的复核闭环，不声称全局最优。
- `J-0014` [arch] 路线与效用健康不是新增管家或自动纠偏器，而是 evaluate 层的只读责任视图；最优是可证伪闭环，目标是目标可核对、偏离可发现、纠偏有边界、纠偏后复测。
- `J-0016` [process] CLI 写命令的帮助探测不得用 unsupported flag 试探；未知参数可能被子命令当成标题或正文并写入账本，先读总帮助或源码，确认支持后再调用。
- `J-0019` [workflow] 源码合并不等于现场生效：换代必须回读 install-state、release 元数据、daemon plist 和 live PID 四重一致；取不到写 unknown，不写 0 或正常。
- `J-0001` [product-architecture] 自动挡架构按“谁能改变事实”分平面，不按愿望数量加管家；全局健康只读，PM 不自动追问，验收截图不替人收口。
- `J-0002` [process] 原生直连派活的最后一道闸是官方 remote-control 的 ChatGPT 登录/配对，不是 socket 路径或协议；不能用 API key 冒充 ChatGPT 身份，也不应把独立 app-server 会话包装成桌面宿主实时窗口。
- `J-0003` [product] 机器闭环和人工验收不能混为一谈
- `J-0017` [process] 源码合并不等于现场生效：换代必须回读 install-state、release 元数据、daemon plist 和 live PID 四重一致；取不到写 unknown，不写 0 或正常。 --scope workflow --why 2026-11-22 宪法页修复在源码和手工 tick 已通过，但当前安装版 .121 仍不含修复；只有受控换代并回读现场才能证明用户现场生效。
- `J-0018` [workflow] 源码合并不等于现场生效：换代必须回读四重现场证据
