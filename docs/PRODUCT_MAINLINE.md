# 产品主线（自动综合，勿手改）

> 生成时间 2026-10-17 06:57:17　·　由 `python3 ops/truthctl.py synthesize` 从真相层综合
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

- [在途] 菜单栏 Companion 的附件从「只报数量」改成逐条可见（回形针图标 + 文件名 + 悬停看全路径）并可单独移除，契约测试锁住不许退回成数字；真编译通过（arm64 macOS13）。
- [在途] install.sh 新增 --verify：装完立刻跑同一份验收报告（落地物/菜单栏应用/原生自检/总闸/面板首屏逻辑），退出码等于失败项数。用户不用记两条命令。
- [在途] 派活自动归入项目窗口集合；按官方 schema 修正 thread/section/move 与嵌套 section.id；新增真二进制协议自检
- [已落盘] @ c002d8269c codex/panel-project-followup 验证门禁通过，已落盘 main @ c002d8269c；改动 6 个文件：ops/central.config.json（+41/-0）、ops/central_mcp.py（+62/-0）、ops/panel_logic_check.py（+11/-5）、ops/panel_render_check.py（+54/-14）、ops/selftest.py（+11/-1），另有 1 个文件｜6 files changed, 278 insertions(+), 76 deletions(-)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0
- [在途] 主 checkout 脏改动审计：判定为旧面板/旧版本回退，未暂存 kgctl 是把已合入的 5 次重试改回 2 次，无合理新进展需要合并；主 checkout 不应直接安装或合入
- [在途] 真机核对：control socket 已存在（app-server daemon 0.163 remoteControlEnabled=true），但对 app-server-control.sock 的 stdio initialize 仍超时；remote-control pair 报 enrollment 未完成；卡点是官方 remote-control enrollment/配对，不是 socket 路径或权限。
- [在途] remote-control enrollment 未完成根因进一步定位：CLI 当前 login status = API key，而 daemon 字符串明确 remote control requires ChatGPT authentication；API key auth is not supported。需要 ChatGPT 账号身份 + remote-control pair 才能握手。
- [在途] 本机已安装 0.6.0-preview.7：最新面板三问/项目文件夹/北星闭环率能力已随插件缓存与 Companion 配置生效
- [在途] preview.7 已安装落盘：面板三问/项目文件夹/北星闭环率/菜单栏新版全部进入本机，唯一剩余阻塞是 remote-control ChatGPT 登录/配对
- [在途] 原生探测升级：socket 存在但不可派活时，直接报 remote-control ChatGPT 登录/配对硬闸，面板/MCP/Companion 可读 blocked_by=remote-control-chatgpt-pairing

## 关键验收证据（核对用，不替代 mainline.json）

- `A-0012` [P0] 面板首屏（含关闭态）每个值都来自真实控制层数据
  - 证据：python3 ops/panel_logic_check.py -> 14 项全过：真执行面板脚本，核对绑定项目（名+路径+悬停）、总状态、正在做什么、原生直连结论、收到/已合入/待你验收三个计数、最近完成，以及关掉总闸后的明确关闭态
- `A-0013` [P0] 装出来的副本自带绑定：kgctl 认得出项目、MCP 能应答（不带任何环境变量）
  - 证据：隔离 HOME 里 bash install.sh --no-seed --no-companion <项目> -> 取装出来的 releases/0.6.0-preview.1 副本，清空 KG_REPO/KG_TRUNK 后：kgctl status 自己解析出项目；MCP initialize + tools/list 正常应答
- `A-0015` [P0] 全量自检 22 组通过（含后台常驻开关真启停，机器自证）
  - 证据：python3 ops/selftest.py -> 22 组全过（退出码 0）；新增断言：总闸关掉时 enable_daemon 被拒、开启/关闭真调 central_daemon --install/--uninstall、安装失败不写 daemon_enabled=true；本条覆盖 A-0014 登记时的默认 todo 状态
- `A-0016` [P0] 派活自动归入项目窗口集合：同名复用、不重复创建、失败不拦派活
  - 证据：python3 ops/selftest.py -> 23 组全过；新增断言：第一次派活自动建窗口集合、第二次复用同一集合不重复建、集合 API 不可用时照常派活但如实返回 section_note
- `A-0017` [P0] 原生协议与真实 codex 二进制对齐：修正窗口集合移动方法名并真机验证
  - 证据：对 codex app-server 0.148.0-alpha.15 生成的官方 JSON schema：threadSection/move 不存在，正确方法名是 thread/section/move；实机验证旧名返回 unknown variant、新名被识别；AUTOPILOT_SELFTEST_REAL_CODEX=1 用真实 codex 二进制建出原生会话与窗口集…
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
- `A-0029` [P1] 本机已安装 0.6.0-preview.8，marketplace/install-state/Codex 缓存/Companion 配置全部指向 preview.8
  - 证据：bash install.sh --state-root /Users/danta/Documents/Codex/codex-autopilot-native-autopilot --no-seed --refresh-plugin (cwd=/Users/danta/Documents/Codex/codex-autopilot-panel-project-followup…

## 还差什么

**bug（4 待处理）**
- `L-0001` 复用 Codex 原生 app-server/thread、Projects/窗口集合与 scheduled automations，形成主司令窗到子对话窗的真
- `L-0003` 对话面板线上仍频繁显示“模型暂时没连上/已停止”，用户无法正常问答；同时右侧面板首卡、失败提示、17 个引用芯片、重复历史气泡混在一起，信息过多、没有轻重。需要
- `L-0004` 自动挡在需要用户决策、人工验收或注意时，必须主动出现明确提示；同时回到产品路线，列出总体预期、剩余工作、卡点、未推进原因和下一步，不要只展示静态状态。
- `L-0005` 原生直连仍卡在官方 remote-control ChatGPT enrollment/配对；安装、面板、菜单栏其余验收可继续机器自证

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

## 进度概览

- 累计关闭问题：1 条
- 累计落盘：2 条
- 未决问题：4 条
- 已验收：19 条

