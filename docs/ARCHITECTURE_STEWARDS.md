# 自动挡架构：一入口、两平面、七管家、一层全局健康

## 结论

用户提出的「六个管家」已经不够，但最优解不是继续横向加管家，而是把职责分成两个平面：

- **项目真相面**：上下文管家、PM 管家、验收管家。回答「要做什么、什么算成、是否值得让人看」。
- **执行面**：路由管家、实现管家、合并管家、状态管家。回答「派给谁、怎么改、怎么安全落盘、运行环境是否健康」。

在这两个平面之上再加一层**全局项目健康**，它不是第八个工序管家，不负责派活、改码或合分支；它只读所有平面已经算好的事实，回答「这个项目整体还健康吗」。

这个结构保留「万法归一」入口：用户仍然只在一个输入框里说话，不需要选项目、对话、分支或角色。内部由路由管家根据入口上下文和项目注册表判断归属；判不清的留在全局待路由箱，不硬塞进某个项目账本。

## 四层运行契约

两平面回答「谁负责什么」，四层回答「一个长期任务怎么活下来」。长期最优解必须同时满足这四层，不能只做其中一层：

| 层 | 责任 | 我们的对应物 | 当前缺口 |
|---|---|---|---|
| 关系层 | 一个持续对话/入口维护目标、优先级、需要人的时机 | 唯一输入框、`ops/tasks/inbox.jsonl`、面板三问、`needs_human` 的人工验收队列 | 还没有把跨项目长期目标做成稳定关系对象 |
| 规划层 | 把模糊输入编译成工作项、约束、排期、授权级别 | 上下文管家、PM 管家、`ops/truth`、`ops/exec_overlap.py`、`ops/acceptance_criteria.py`、`ops/acceptance_criteria_compiler.py`、observe/propose/auto 与 explicit/inferred 分级 | 候选确认与路由编译已落地；编译器只产出工单自证计划，不创建验收项，也不替代人工确认 |
| 执行层 | 隔离会话里并行研究/实现/验证，再做串行合流 | 路由/实现/合并/验收管家、独立 worktree、`kgctl land` 门禁、`ops/worker_lifecycle.py` 生命周期契约、`ops/exec_lifecycle.py` 生产接线 | 已接管真实派发；定时唤醒只写回入口，仍不直接开 worker |
| 运行层 | 收件箱、定时器、记忆、权限、会话、发件箱、审计，让任务可唤醒、可恢复、可送达 | `central_daemon`、`pass_registry`、`ops/wake_resume.py`、`worker_lifecycle`、`ops/exec_lifecycle.py`、truth 台账、全局待路由箱、`project_health` | 到期唤醒、显式恢复、资源/工具白名单与逐 pass 授权审计已落地；Outcomes 式独立评分仍缺 |

## Worker 生命周期契约

`ops/worker_lifecycle.py` 把后台 worker 的 `start/status/stop/event/handback/resume`
收敛为 append-only JSONL 事件账本：每次动作都带 seq、理由、授权来源和 generation，
当前状态由事件投影得出，不允许跳过中间态。重复 `start` 幂等，避免开出第二个 session；
`stop` / `interrupted` 可显式 `resume` 并升 generation；`handed_back` 是终态，
只能由路由层开新 worker。

每个 worker spec 还带 `allowed_resources` / `allowed_tools`：请求范围必须落在工单
声明内，越界在 `start/resume` 前抛 `AuthorizationError`，不会 `Popen`、不会落
running 账本，也不会占并发槽位。未声明 allowlist 时沿用旧请求范围，保证现有工单
平滑升级；控制层只把 `last_passes` 中已有的 `authorization` / `effect` 投影给
面板，工程详情折叠区逐条显示授权来源与副作用，首屏只保留摘要。

授权边界复用 `pass_registry`：`inferred + destructive` 直接拒绝，`destructive`
必须带 `authorization_reason`。账本损坏、事件 seq 断裂、非法状态转移都 fail-closed，
不能把「读不懂」当成「没有任务」。反向门禁 `ops/test-worker-lifecycle.py` 锁住这些失败路径。

`ops/exec_lifecycle.py` 已把这套契约接进 `central_daemon` 的真实派发路径：首次
派发、在途现场恢复、基础设施重试、模型降级重派和无改动重派都必须先过账本，
回收结果再回写终态。账本损坏或授权冲突时禁止 `Popen`，启动失败不占并发槽位，
终态之后重派生成新 worker，不能复用已结束的账本。反向门禁
`ops/test-exec-lifecycle.py` 锁住这些生产接线约束。

当前边界要说清：生产 worker 已经按上述生命周期契约运行；跨项目的 timer 唤醒、
显式跨工单恢复、资源/工具白名单和逐 pass 授权审计均已有可验证门禁，运行层比上一版更硬，
但仍不能把运行层整体说成「已最优」。

因此当前不是「最优已完成」，而是「架构收敛正确、运行层还在补」。最优路线不是继续加第八、第九个管家，而是把四层之间的契约写硬：谁可以唤醒谁、谁拥有写权限、什么证据允许合流、什么情况必须回到人这里。全局健康只读这四层的事实，不替任何一层做决定。

## 定时唤醒与显式跨工单恢复契约

`ops/wake_resume.py` 把 timer 唤醒和显式跨工单恢复收敛为 append-only 请求账本。
请求从 `scheduled` 进入 `firing`，再落 `delivered`、`failed` 或 `cancelled`；
每次投递都带幂等标记、理由、授权来源和 attempt，失败按退避重试且次数有上限，
送达成功后永不重复打扰唯一入口。账本 seq 断裂、非法跳转或 JSON 损坏时 fail-closed，
不能把「读不懂」当成「没有到期任务」。

安全边界是硬的：timer 只允许 `explicit` 授权，不接受 `inferred` 自动升权；它只能把
消息写回 `kgctl inbox`，不能启动 worker。daemon 的 `wake` pass 注册在 `route` 之前，
到期项在同一拍进入路由，随后仍由 `exec` pass 与 `worker_lifecycle` 决定是否派发。
跨工单恢复还必须声明 `resume_from`；旧 worker 处于 active、resumable 或 absent 时
一律拒绝，要求调用方回到 `worker_lifecycle.resume`，不能借 timer 绕过生命周期。

门禁 `ops/test-wake-resume.py` 锁住 inferred 拒绝、活跃旧 worker 拒绝、重复投递拒绝、
失败退避与上限、坏账本 fail-closed，以及 daemon 只写 inbox 不派 worker；已登记进
`selftest.py`。

## 责任边界

| 角色 | 拥有的判断 | 不拥有的权力 | 当前落点 |
|---|---|---|---|
| 上下文管家 | 从任意入口的预期、返修、决策、禁忌中提候选；按 `scope` 精准调取 | 不能未经用户确认就写生效约束 | `ops/constraint_candidates.py`、`ops/constraints.py`、面板候选卡 |
| PM 管家 | 理解预期、拆施工项、判断文件占地重复、规划不互相覆盖的路线、汇总追问候选 | 不能替用户决定产品方向、删数据或发布；不能自动发追问 | `ops/pm_briefing.py`、`ops/exec_overlap.py`、`ops/central_daemon.py` 的 `overlap_hold` |
| 验收管家 | 从已路由原话提名验收标准候选；先定义标准并自己跑门禁；有证据后才请用户看，并带上复核截图附件 | 不能把候选直接写成验收项或把没有证据的项伪装成「需要你验收」；截图不参与证据闸门 | `ops/acceptance_criteria.py`、`ops/central_mcp.py` 的 `_acceptance_board()`、`ops/truthctl.py` 的 `cmd_accept()` |
| 路由管家 | 从混合输入判断项目与泳道，把活派出去 | 不能把判不清的输入猜进某个项目 | `ops/project_router.py`、全局 routing inbox |
| 实现管家 | 按相关上下文实现，跑验证，提交到隔离 worktree | 不能在共享 checkout 上改别人文件 | `ops/central_daemon.py` 的派发与 `exec_status` |
| 合并管家 | 合并前自检、防丢失防覆盖、拆冲突、自动 land | 不能绕过门禁或把合并决策推回用户 | `ops/kgctl.py` 的 gate / land / resolve / port / supersede |
| 状态管家 | 监控 harness 压缩线、峰值 input、用量，提出正向调整卡 | 不能在工作进程忙时改配置，不能做负向调优 | `ops/budget_gate.py`、`ops/harness_tune.py` |
| 全局健康（层） | 聚合主线新鲜度、约束沉淀、无证据完成项、执行卡点、验收积压 | 不能重写台账、不能触发动作、不能把取不到说成正常 | `ops/project_health.py`、`autopilot_status.project_health` |

## PM 管家：重复施工项与最优路线

多个施工项可能互不干扰，也可能声明改同一批文件。worktree 隔离解决了「文件同时被写坏」，但不能解决「同一件事被做两遍」。

当前判据是**文件占地**：每条需求可声明 `files`；派发新会话前，把它与「正在跑会话的条目」和「已有在途成果的条目」比对。路径先归一化，命中同一文件就记 `overlap_hold`，让新条目排队；前一条落地后每拍重算并自动放行。

两个重要边界：

1. 排队不占并发槽位，也不计失败次数；`overlap_hold` 在面板里归到 `exec_waiting`，不进 `stuck_n`。
2. 没有 `files` 线索时不做判断，也不假装「不重叠」。这如实保留未知，后续应由 PM 管家在需求拆解阶段补足文件声明。

PM 视角新增一层只读简报：把待确认预期、验收标准候选、已生效约束、主线阶段、让路工作项（等谁、撞哪些文件）和长时间无进展的追问候选聚到一处。它的边界很硬：只读台账、不派活、不自动发追问；台账读不到写 `unknown`，不写 `0` 冒充正常。

## 验收管家：先自验，再打扰用户

前半截是「什么算成」：`ops/acceptance_criteria.py` 从**已路由**的入口原话里识别明确验收信号（必须能、看到、跑通、测试通过、门禁等），提名可判定标准候选，并附原话、命中信号、建议复核步骤、是否建议机器门禁。候选只进 `acceptance_criteria_candidates.jsonl`，不写 `acceptance.jsonl`；重复原话和 `dismiss` 过的原话不再弹。人明确 `confirm=true` 后才调用 `truthctl accept` 登记 A-id，原话 hash 一并落进验收台账，候选文件丢失也不会让同一句话复发。

规划层另有一条独立的确定性编译链，和候选确认互补：`ops/truthctl.py route` 在预演与落账时用 `ops/acceptance_criteria_compiler.py` 把已路由工作项编译成 `acceptance_plan`，写进 `ledger.jsonl`。它只从项目 `verify` 配置和文件线索推出能真跑的机器门禁（命令、cwd、`exit 0`）与必须人看的复核步骤；推不出可判定条件就如实写 `unknown`。`central_daemon._exec_prompt()` 读这份计划，要求执行者逐条写出命令、cwd 和退出码；`central_mcp._work_rows()` 与工程详情工单表只显示 `验收标准：机器 N 条 / 人工 M 条`，不把命令堆到首屏。编译器不创建 A-id、不写 `evidence`，也不把人工项伪装成机器 verdict。

确认候选只是登记验收项，**不是验收通过**：人工项带复核步骤但没有 `evidence`，机器项也只建议真门禁。两者都不能被自动标成 `verified`，也不能直接进「需要你验收」。

人工验收项只有 `needs_human=true` 还不够。若没有 `evidence`，它进入「待自验」，不进入「需要你验收」。`evidence` 必须是可复核的命令、cwd 和退出码；验收管家自己先把机器能跑的部分跑完，再把确实需要人的部分截图提醒用户。

这是一种顺序约束，不是礼貌措辞：用户不能被当成第一道测试。

`register_acceptance` / `resolve_acceptance` 现在支持 `screenshots` 数组，写入 append-only 验收台账并原样上屏为「复核截图」。它是人工复核附件，不参与 `evidence` 闸门：没有真实验证的项即使附了截图，也仍留在「待自验」。

## 全局项目健康

`ops/project_health.py` 只读已有事实，不新建分数台账：

- **真相层**：`mainline.updated` 超过 30 天、`constraints.jsonl` 为 0、完成项没证据，抬为 `attention`。
- **执行层**：真卡住的实现、落盘门禁失败抬为 `attention`；`overlap_hold` 只作为信号展示，因为它是正常排队，不是病。
- **验收层**：待自验项在自动实现关闭时没人会跑，抬为 `attention`；已自验的人工项积压超过 3 条抬为 `attention`。

读取失败一律 `unknown`，标题说「取不到」，不输出 0% 或「正常」。`overlap_hold` 是调度信号，不抬整体告警，避免正常调度把首屏变成狼来了。

## 面板

面板原生耦合在 Codex 界面里，通过 MCP UI resource `ui://autopilot/panel-v2.html` 渲染，不另起 Electron 应用。首屏继续只回答三问：

1. 要不要我处理。
2. 是不是在干对的事。
3. 替我做成了什么。

管家状态位于第二问下方，一行一个角色，顺序由后台按 `attention / unknown / off / ok` 排好；面板只照抄，不重排。最后一行是「全局健康」，用的是同一套四态点阵，但语义是项目级结论，不是第八个施工管家。

工程详情里新增「PM 简报」折叠区：待确认预期、验收标准候选、已生效约束、让路队列、追问候选都在那里，首屏不增噪音。追问候选只展示建议提示，不会自动向任何 Codex 窗口发消息。验收标准候选在「需要你决策」区提供「确认成验收项 / 不算」两条出口，文案明确说确认只登记验收项、不写证据；首屏不因此新增噪音。

## 与 DeepSeek Super-agent 的关系

详细调研见 `docs/OPENSOURCE_RESEARCH.md`。结论：Router + Worker + 隔离 worktree + 面板这一层与 DeepSeek Harness 的 Super-agent 原型正面撞车，继续自造没有价值。

我们的差异化保留在三件事：

1. 落盘合流的防丢失门禁，以及 append-only 真相层的并集与收口；
2. 用户约束不只写给人看，还能进机器门禁；
3. 首屏三问的注意力纪律，以及这层可计算、可上屏的全局健康。

因此路线不是「再造一个多 agent 框架」，而是把编排复用给成熟方案，在 Codex 现有会话、worktree、skill 和 MCP UI 扩展点上，把项目真相、验收顺序和健康信号做穿。

## 事实边界

外部资料必须分层写，不能把营销转述当成上游事实：

- Claude 官方已核实：Projects 是一个持续对话，Claude 为每项工作建立 thread，Overview 汇总已完成、待评审和等待回答的 thread；Claude Code slash commands 文档包含 `/btw` 旁问、`/background` 脱离当前终端继续运行、`/fork` 复制会话到后台；scheduled tasks 文档给出会话内轮询与提醒。我们的 `needs_human` 队列与「等待回答」是同类产品语义，不是 Claude 的功能别名。来源：`https://code.claude.com/docs/en/claude-projects.md`、`https://code.claude.com/docs/en/commands.md`、`https://code.claude.com/docs/en/agent-view.md`、`https://code.claude.com/docs/en/scheduled-tasks.md`、`https://code.claude.com/docs/en/sub-agents.md`。
- DeepSeek 官方原型文档已核实：README 自述是研究原型、不是官方产品；授权文档定义 `explicit/inferred`，执行文档明确 host 没有 merge-specific decision logic、auto-merge 由 Delivery requirements 约束，面板文档说明优先展示需要人的工作。来源：`https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/README.md`、`docs/002-authorization.md`、`docs/003-execution.md`、`docs/005-panel.md`。
- DSH 本机可核实：`~/.dsh/profiles/web/cordis.yml` 明确写着 profile 由 package bundle 与 patch 组合；本机 session 状态包含 `inbox`、`subagentCatalog`、`subagent` 等键。这支持「插件化 + 收件箱 + subagent」的运行层判断，但不等于验证了 Super-agent 原型的所有指标。
- 小红书线索里的 `Grok Bot 1.0`、多 Bot 群聊、自动优化 Skill、p50/p95、缓存读取倍数和订阅直通比例，目前没有在官方原文中逐条核实，只作为产品方向信号，不写成已发布事实。
- proto 与产品要分开：DeepSeek Super-agent 原型属于研究原型；Claude 的 projects / background / fork 是已上线功能。两者不能混成一句「大家都已支持 Super Agent」。

## 已知边界

- PM 管家的预期打磨已上屏为简报，但候选升成生效约束仍需人确认；这是有意的，避免一句自然语言直接进机器门禁全局污染。
- 验收标准编译器只按项目配置和文件线索产出工单自证计划；推不出时保持 `unknown`，且不替代候选确认、人工验收或真跑门禁的证据。
- 进度追问已能生成候选，但不会自动发消息；是否追问仍由人决定。
- 全局健康目前是面板可见的只读结论，还没有在守护里自动触发修复；这是有意的，避免越过用户授权改配置或动工作进程。
- 截图附件已能写入、透传、上屏，但系统不会自动截屏；真实桌面验收动作仍需用户或执行者提供截图路径，面板负责把附件展示出来。
- `pass_registry` 已实现 `explicit/inferred` 两级授权和 `inferred + destructive` 禁行；worker spec 的 `allowed_resources` / `allowed_tools` 在 `Popen` 前 fail-closed，面板工程详情逐条显示本轮 pass 的模式、授权来源、副作用和状态。旧对照稿曾写「两级授权未落地」，那是对旧基线的判断，不适用于当前代码。
