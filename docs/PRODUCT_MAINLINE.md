# 产品主线（自动综合，勿手改）

> 生成时间 2026-10-17 11:13:57　·　由 `python3 ops/truthctl.py synthesize` 从真相层综合
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
- ✓ 常驻自追踪：没有对话时也持续同步项目主线、决策、进展与卡点；守护启停入口已实现并机器验证，真机常驻需安装验收　（已完成）
- ✓ 总开关与统一本机控制面：一个总闸决定外挂是否工作；关掉就回到 Codex 原生状态，所有入口共用一个控制通道，不各自维护任务　（已完成）
- ✓ Codex 原生风格侧栏面板：首屏只回答：开没开、当前绑定哪个项目、正在处理什么、下一步、是否需要我介入、最近完成了什么；路线与证据收进二级（首屏数据流已机器验证，观感仍需人眼过一遍）　（已完成）
- ● 跨应用入口：在任何工作上下文都能把问题交进来：全局快捷键、截图、附件、系统服务菜单，全部落到同一个入口　（进行中）
- ● 菜单栏 Companion 状态锚点：永远知道自动挡在哪、开没开、有没有事需要我处理；它只做锚点，不复制第二套路线管理　（进行中）
- ✓ 版本化安装与回滚：升级只切版本指针，旧版本目录与旧账本一律保留，失败可一键回退　（已完成）
- ● 原生 thread 直连派活：用 Codex 官方 app-server 协议建原生对话、自动归入本项目的窗口集合、持续 follow-up；协议已用真实 codex 二进制验证（含窗口集合创建与移动方法名），派进用户桌面窗口需在不受限环境验收　（进行中）
- ● 真实端到端演示与预览发布：一条真实需求从入口走到收口，每一步留下命令、cwd、退出码、commit　（进行中）

## 现在是什么样

- **唯一入口**：零散需求（视觉/体验/bug/架构/门禁/多分支）说一次即进唯一入口，由系统路由到固定泳道，不需要用户选对话、选分支、选角色。
- **真相沉淀与真实验收**：主线/决策/问题/进展/验收/判断逻辑落进 ops/truth；验收证据必须是真实跑过的命令+cwd+退出码，不允许用结论代替证据。
- **问题闭环**：bug/视觉/体验/架构进台账后由 agent 推进：修复 -> 验证 -> close 并追加进展；回归用 reopen 重开。
- **产品主线综合**：每轮收口跑 synthesize，把决策/进展/问题/验收/产品状态综合成 docs/PRODUCT_MAINLINE.md；人在 mainline.json 只拍板方向。
- **合并编排**：每个任务独立 worktree；合并前用 git merge-tree 预演，门禁通过且无冲突时自动 land 并同步 main 引用，不把要不要合推回用户。
- **常驻守护与面板**：面板显示运行状态、当前处理的分支合并逻辑、开关与必要信息；支持暂停/自动合入/自动拆冲突/常驻。
- **视觉/设计方案提示词**：做视觉/设计前先当产品经理+产品设计师，尊重用户、保护注意力：先截图看现状再改，改完再截图对比，只允许正向改动，不能更丑、更乱、更难读。
- **主动注意提醒**：出现人工验收、需要拍板或异常时，主面板显示明确待处理状态，菜单栏切换警示图标并通过 macOS 通知中心提醒；同一批待办只提醒一次。

## 决策脉络

- Companion 打开 Codex 用 bundle id 而不是路径 —— Codex 桌面端本体是 /Applications/ChatGPT.app（已从 Info.plist 核实 CFBundleIdentifier=com.openai.codex）；按 bundle id 解析路径比写死 /Applications/ChatGPT.app 稳，名字换了也不会失效
- 北极星首问闭环率的唯一口径 —— 北极星一直是文案不是数字：被统计的是 landed/resolved/superseded 这类机器动作数，产品价值所在的比例没有分子分母、不会红，这是「做着做着达不到预期」的结构性根因。把它变成会红、可复现的数字，行为才会从「刷动作数」转向「真把需求做闭环」。

## 最近在推进

- [已落盘] @ 8c850036bb codex/verify-preview9-real-codex 验证门禁通过，已落盘 main @ 8c850036bb；改动 5 个文件：CONTEXT.md（+13/-15）、ops/codex_native.py（+177/-2）、ops/selftest.py（+6/-0）、ops/truth/closure_rate.json（+1/-1）、ops/truth/progress.jsonl（+7/-0）｜5 files changed, 204 insertions(+), 18 deletions(-)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0
- [在途] scratch=/tmp/ap-e2e-20261017-clean-1792202425-32217; KG_REPO=.../state; KG_TRUNK=.../state-trunk; KG_DAEMON_HOME=.../home; python3 ops/central_daemon.py --once -> exit 0; real codex exec created hello-ok.txt in state-exec-l-0001-hello-ok-txt; kgctl land pushed c0573b47->f6b0b66 to scratch origin/main; truthctl close L-0001 -> done
- [已落盘] @ 8de7ba3649 codex/release-preview10 验证门禁通过，已落盘 main @ 8de7ba3649；改动：本次合并未带进新内容（分支内容已在主干，属空合并）｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0｜对应验收项：A-0032
- [在途] real desktop host native dispatch succeeded via ops/codex_native.py dispatch; thread=01a147a9-b3ca-7412-80ff-2dc35cb816a1, turn=01a147a9-b4ef-7c43-949e-b5b83cb16773, section=01a147a9-b328-7c73-9d7d-0d84e3b11e9e; probe can_dispatch=true; section name Codex 自动挡真机验证; prompt requested no file changes
- [在途] 原生直连卡点已解除：2026-10-17 真机 probe 使用 app-server-control.sock WebSocket 直连真实桌面宿主，transport=codex-app-server-websocket、connected=true、can_dispatch=true；dispatch 成功创建 native thread/turn/section，thread_id=01a147a9-b3ca-7412-80ff-2dc35cb816a1。旧 remote-control ChatGPT 配对结论被推翻。
- [在途] preview-check.sh 已把真实安装/落地物/菜单栏应用/原生直连/总闸/面板首屏压成一条命令；2026-10-17 preview-check.sh --no-install 输出全部 PASS、exit 0。install.sh --verify 同一条命令可装完即验。
- [在途] 原生 app-server/thread、Projects/窗口集合、turn/start、follow 均已通过真实 codex 二进制与真实桌面宿主验证：AUTOPILOT_SELFTEST_REAL_CODEX=1 selftest 全过；2026-10-17 probe can_dispatch=true；dispatch 创建 native thread/turn/section。scheduled automations 仍属于后续外部生命周期能力，不再伪装成本轮已实现。
- [在途] 机器侧闭环已推进到底：preview.10 门禁全过（preview-check 0 failure、selftest、panel_logic_check、panel_render_check、codex_native probe can_dispatch=true）；A-0005/A-0006/A-0007/A-0008 仍为真人桌面验收，不可由机器自检替代。
- [在途] 该票属于 knowledge-graph 项目（真实来源 /Users/danta/.newmax/workspace/knowledge-graph/ops/truth/ledger.jsonl），所指文件在本仓库不存在，不应由自动挡账本持有；已在原项目继续闭环。
- [在途] 主动提醒能力已实现：Companion 有通知授权与 notifyIfNeeded 去重提醒、面板有人工验收/拍板/异常待处理态；A-0022 为真机人工验收，闭环边界见 A-0022。

## 关键验收证据（核对用，不替代 mainline.json）

- `A-0018` [P0] 全量自检 23 组通过（含真二进制开关 24 组）
  - 证据：python3 ops/selftest.py -> 23 组全过；AUTOPILOT_SELFTEST_REAL_CODEX=1 python3 ops/selftest.py -> 24 组全过（含真实 codex 二进制协议验证）
- `A-0019` [P0] 面板按官方嵌套字段显示窗口集合内的子对话
  - 证据：官方 Thread schema 里集合是嵌套 section.id（无扁平 sectionId）；panel_logic_check.py 新增真实形状用例，并做反向验证：把面板改回扁平字段后该用例失败（rc=1），还原后全过
- `A-0020` [P1] 菜单栏完整面板回到 Codex 本体，HTML 驾驶舱降为备用
  - 证据：Companion 契约自检新增断言：openCodexPanel/onOpenCockpit 存在，且主面板路径不含 call(["panel"])；AUTOPILOT_SELFTEST_COMPANION=1 真编译 .app 通过
- `A-0021` [P0] 原生方法名全量对真实服务器核对（6/6 存在）
  - 证据：AUTOPILOT_SELFTEST_REAL_CODEX=1：从真实 codex app-server 取接受方法列表（144 个），项目里 6 个原生调用方法全部存在；AUTOPILOT_SELFTEST_COMPANION=1 AUTOPILOT_SELFTEST_REAL_CODEX=1 python3 ops/selftest.py -> 25 组全过
- `A-0023` [P1] codex/panel-project-followup 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0026` [P1] 本机已安装 0.6.0-preview.6，marketplace/install-state/Codex 缓存全部指向 preview.6
  - 证据：bash install.sh --state-root /Users/danta/Documents/Codex/codex-autopilot-native-autopilot --no-seed --verify --refresh-plugin (cwd=/Users/danta/Documents/Codex/codex-autopilot-panel-project…
- `A-0028` [P1] codex/port-panel-route-closure-2 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0029` [P1] 本机已安装 0.6.0-preview.8，marketplace/install-state/Codex 缓存/Companion 配置全部指向 preview.8
  - 证据：bash install.sh --state-root /Users/danta/Documents/Codex/codex-autopilot-native-autopilot --no-seed --refresh-plugin (cwd=/Users/danta/Documents/Codex/codex-autopilot-panel-project-followup…
- `A-0031` [P1] codex/verify-preview9-real-codex 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0032` [P0] 真实需求 auto_exec 机器闭环：入口->归类->独立 worktree->真实 codex->门禁->land->收口
  - 证据：scratch=/tmp/ap-e2e-20261017-clean-1792202425-32217; KG_REPO=/Users/danta/Documents/Codex/2026-10-17/record-preview9/../../../../tmp/ap-e2e-20261017-clean-1792202425-32217/state; python3 ops…
- `A-0033` [P1] codex/release-preview10 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0036` [P0] preview.10 机器全量验收：自检/面板逻辑/真实渲染/原生直连/安装产物全部通过
  - 证据：preview-check.sh --no-install -> 0 failures (version=0.6.0-preview.10); python3 ops/selftest.py -> exit 0; python3 ops/panel_logic_check.py -> all pass; AUTOPILOT_CHROMIUM=... python3 ops/pa…

## 还差什么

- 台账里没有未决问题

## 需要你验收

- `A-0005` [P0] 菜单栏 Companion 在真实桌面启动后可用
  - 1) 终端跑 bash install.sh（会装到 ~/Applications）2) open ~/Applications/CodexAutopilotCompanion.app 3) 看菜单栏是否出现自动挡图标 4) 点图标：应看到总开关、当前任务、需要处理、派活入口 5) ⌥⌘D 应能打开完整面板
- `A-0006` [P0] 面板首屏在 Codex 里显示真实状态，关总闸后有明确关闭态
  - 1) 在 Codex 里打开自动挡面板 2) 首屏应显示：是否开启、当前项目、正在处理什么、下一步、需要你处理 3) 点「原生自检」，结果应如实（连不上就说连不上）4) 关掉总闸，首屏应显示已关闭、Codex 已恢复原生状态
- `A-0007` [P0] 跨应用入口逐个可用（快捷键/截图/附件/服务菜单）
  - 1) 在任意 app 里按 ⌥⌘S 截图并加一句话派活 2) 把文件拖到菜单栏图标上派活 3) 选中一段文本 → 服务 → 发送到 Codex 自动挡 4) 每次派完，面板里都应出现对应的新任务，而不是各存一份
- `A-0008` [P0] 一条真实需求端到端走完并留证据
  - 1) 用任意入口提一条真实小需求 2) 观察它自动归类、面板出现阶段变化 3) 每个阶段面板与菜单栏状态一致 4) 结束时能看到真实验证命令与收口结果，而不是只有一句完成
- `A-0022` [P0] 需要用户决策/验收时主动提醒并说明下一步
  - 1) 在正常 macOS 终端安装并打开 Companion 2) 制造或等待一项人工验收/需要拍板事项 3) 确认菜单栏出现警示图标和 macOS 通知 4) 打开 Codex 面板确认写清具体动作 5) 清掉事项后确认不再重复提醒

## 已机器验收

- `A-0001` [P0] 安装器完整路径（含菜单栏 Companion 真编译）在隔离 HOME 里真跑通
- `A-0002` [P0] 全量自检 19 组通过
- `A-0003` [P1] 菜单栏 Companion 产物是可安装的 .app（arm64、可执行、已签名、带服务菜单）
- `A-0004` [P0] 总开关关掉后所有自动动作被拒绝，且不删除任何现场
- `A-0009` [P0] 预览包验收可用一条命令跑完并如实报错
- `A-0010` [P0] 统一入口真实端到端：派单→自动归类→台账→主线→面板
- `A-0011` [P0] 全量自检 20 组通过（覆盖 A-0002 的组数）
- `A-0012` [P0] 面板首屏（含关闭态）每个值都来自真实控制层数据
- `A-0013` [P0] 装出来的副本自带绑定：kgctl 认得出项目、MCP 能应答（不带任何环境变量）
- `A-0015` [P0] 全量自检 22 组通过（含后台常驻开关真启停，机器自证）
- `A-0016` [P0] 派活自动归入项目窗口集合：同名复用、不重复创建、失败不拦派活
- `A-0017` [P0] 原生协议与真实 codex 二进制对齐：修正窗口集合移动方法名并真机验证
- `A-0018` [P0] 全量自检 23 组通过（含真二进制开关 24 组）
- `A-0019` [P0] 面板按官方嵌套字段显示窗口集合内的子对话
- `A-0020` [P1] 菜单栏完整面板回到 Codex 本体，HTML 驾驶舱降为备用
- `A-0021` [P0] 原生方法名全量对真实服务器核对（6/6 存在）
- `A-0023` [P1] codex/panel-project-followup 落盘验收
- `A-0026` [P1] 本机已安装 0.6.0-preview.6，marketplace/install-state/Codex 缓存全部指向 preview.6
- `A-0029` [P1] 本机已安装 0.6.0-preview.8，marketplace/install-state/Codex 缓存/Companion 配置全部指向 preview.8
- `A-0028` [P1] codex/port-panel-route-closure-2 落盘验收
- `A-0031` [P1] codex/verify-preview9-real-codex 落盘验收
- `A-0032` [P0] 真实需求 auto_exec 机器闭环：入口->归类->独立 worktree->真实 codex->门禁->land->收口
- `A-0033` [P1] codex/release-preview10 落盘验收
- `A-0036` [P0] preview.10 机器全量验收：自检/面板逻辑/真实渲染/原生直连/安装产物全部通过

## 进度概览

- 累计关闭问题：5 条
- 累计落盘：7 条
- 未决问题：0 条
- 已验收：24 条

