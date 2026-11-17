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
| 规划层 | 把模糊输入编译成工作项、约束、排期、授权级别 | 上下文管家、PM 管家、`ops/truth`、`ops/exec_overlap.py`、observe/propose/auto 与 explicit/inferred 分级 | 模糊需求自动生成可判定验收标准仍未完成 |
| 执行层 | 隔离会话里并行研究/实现/验证，再做串行合流 | 路由/实现/合并/验收管家、独立 worktree、`kgctl land` 门禁 | 后台 worker 的显式 `start/status/stop/event` 契约还没成形 |
| 运行层 | 收件箱、定时器、记忆、权限、会话、发件箱、审计，让任务可唤醒、可恢复、可送达 | `central_daemon`、`pass_registry`、truth 台账、全局待路由箱、`project_health` | 定时器和显式恢复协议仍弱，当前主要靠守护轮询与台账快照 |

因此当前不是「最优已完成」，而是「架构收敛正确、运行层还在补」。最优路线不是继续加第八、第九个管家，而是把四层之间的契约写硬：谁可以唤醒谁、谁拥有写权限、什么证据允许合流、什么情况必须回到人这里。全局健康只读这四层的事实，不替任何一层做决定。

## 责任边界

| 角色 | 拥有的判断 | 不拥有的权力 | 当前落点 |
|---|---|---|---|
| 上下文管家 | 从任意入口的预期、返修、决策、禁忌中提候选；按 `scope` 精准调取 | 不能未经用户确认就写生效约束 | `ops/constraint_candidates.py`、`ops/constraints.py`、面板候选卡 |
| PM 管家 | 理解预期、拆施工项、判断文件占地重复、规划不互相覆盖的路线 | 不能替用户决定产品方向、删数据或发布 | `ops/exec_overlap.py`、`ops/central_daemon.py` 的 `overlap_hold` |
| 验收管家 | 先定义验收标准并自己跑门禁；有证据后才请用户看 | 不能把没有证据的项伪装成「需要你验收」 | `ops/central_mcp.py` 的 `_acceptance_board()` |
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

## 验收管家：先自验，再打扰用户

人工验收项只有 `needs_human=true` 还不够。若没有 `evidence`，它进入「待自验」，不进入「需要你验收」。`evidence` 必须是可复核的命令、cwd 和退出码；验收管家自己先把机器能跑的部分跑完，再把确实需要人的部分截图提醒用户。

这是一种顺序约束，不是礼貌措辞：用户不能被当成第一道测试。

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

- PM 管家目前能按声明文件挡重复施工，但「预期持续打磨」仍主要依赖上下文候选卡和用户确认，还没有自动把模糊需求演化成稳定验收标准。
- 全局健康目前是面板可见的只读结论，还没有在守护里自动触发修复；这是有意的，避免越过用户授权改配置或动工作进程。
- 截图提醒用户验收需要真实桌面验收动作，当前由面板验收项的收口出口承载，尚未做成自动截图附件。
- `pass_registry` 已实现 `explicit/inferred` 两级授权和 `inferred + destructive` 禁行；但每个真实 pass 的授权来源声明、面板对这两种来源的可读呈现，还没有做完整审计。旧对照稿曾写「两级授权未落地」，那是对旧基线的判断，不适用于当前代码。
