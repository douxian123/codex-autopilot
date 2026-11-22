# 项目宪法（自动投影，勿手改）

> 生成时间 2026-11-22 17:51:12　·　由 `python3 ops/truthctl.py constitution` 从约束/候选/判断台账投影
> 只有人确认过的约束才生效；候选与驳回项保留原话供追溯，不自动参与执行。

生效 2 · 待确认 6 · 已确认待落账 0 · 已驳回 0 · 判断逻辑 16

## 一、生效约束

- **C-0001** [预期/acceptance|panel · 来源 user/有机器门禁] 验收管家必须先自验再打扰用户：没有可复核证据的项不得进入需要你验收；补图必须用户确认，且只追加附件不替用户收口。
  - 验证命令：`python3 ops/test-acceptance-selfcheck.py && python3 ops/test-acceptance-screenshot.py`
- **C-0002** [预期/pm|constraints|panel · 来源 user/有机器门禁] PM 管家只聚合并呈现待确认预期、让路和追问候选；候选不能自动发消息，不能未经确认直接升成生效约束，也不能替用户决定方向、删数据或发布。
  - 验证命令：`python3 ops/test-pm-briefing.py`

## 二、待确认候选（用户说过，但尚未生效）

- **CC-0001** [高优先/工作流程/预期] 自动挡在需要用户决策、人工验收或注意时，必须主动出现明确提示；同时回到产品路线，列出总体预期、剩余工作、卡点、未推进原因和下一步，不要只展示静态状态。
  - 命中：必须
- **CC-0002** [高优先/工作流程/禁忌] 为什么这么慢？现场要对齐 29 号前的工程闭环、19 层进度百分比、下一步与卡点；主线当前 Release Gates 的口径分叉要修，36 题严格质量 RED 和 50 题真人盲评不能靠放宽门禁解决。
  - 命中：不能
- **CC-0003** [高优先/工作流程/预期] openclaw 小生主动消息爱说空话：已修根因并验证。①7 个定时脚本（agent-{morning-brief,autodraw,evening-review,goodnight,midnight-goodnight,1am-check,2am-sleep}.sh）的 --message 是硬编码模板，20:00 那条「今天推进了什么/学到了什么/明天最重要的一件事」逐字来自脚本，不是模型自由发挥。已改为统一引用 ~/.openclaw/proactive-voice.md（新写的说话规则：必须先读上下文落到具体锚点，无锚点输出 HEARTBEAT_OK）。②小生的实际工作区是 workspace-main，但 MEMORY.md/HEARTBEAT.md/CURRENT_FOCUS.md/PRODUCTS.md 历史上只写在 workspace，他根本读不到自己的记忆，USER.md 也只有 1.2KB 空模板（真实档案 4.3KB 在 workspace）。已用真实文件同步（软链会让 read 工具报 path must be a regular file），并加 bin/sync-agent-workspace.sh 双向同步、7 个脚本每次先调用。③workspace/MEMORY.md 涨到 93925 行/96KB 其中 93822 行是纯空行，来源是 bin/xiaosheng_continuity_scheduler.py 的 sync_context_to_workspace() 里 after_section.rsplit('---',1)[1] 把空行反复接回，已修（strip + \n{3,} 折叠，跑 3 次幂等）并清理文件到 103 行/7KB。④workspace-main/AGENTS.md 原为 OpenClaw 英文默认模板，与 workspace/AGENTS.md 的小生人设冲突，已合并，并把「不输出 HEARTBEAT_OK」改成正确语义。验证：喂真实素材后运行，他输出「生长那条流程，阶段事件只能由后端 progress 帧推上来…谓语读不出来也是同一个根源…应该先把数据补对」，有具体判断无寒暄；无素材时正确输出 HEARTBEAT_OK。注意：workspace 仓库有 139 项他人未提交改动，未在其中提交。
  - 命中：必须、应该、默认、每次
- **CC-0004** [高优先/工作流程/预期] 打开自动挡面板自然语言触发不稳定：用户说“打开自动挡面板”后，Codex 会话只分析入口、没有调用 render_autopilot_panel，侧栏/内联面板都没有出现；需要在触发规范里明确写“必须调用 render_autopilot_panel 工具”，并给小白用户提供可复制指令
  - 命中：必须
- **CC-0005** [高优先/项目/预期] 用户只在一个入口表达，系统负责跨项目/跨窗口路由，不让用户自己选
  - 命中：context_learning、跨会话重复
  - 重复证据：出现 6 次 / 跨 5 个会话
- **CC-0006** [高优先/项目/预期] 验收和交付必须基于真实证据与端到端闭环，不用绿字、记录或可合并替代生效
  - 命中：context_learning、跨会话重复
  - 重复证据：出现 10 次 / 跨 4 个会话

## 三、已确认待落账

- （没有已确认但还没在生效约束里看到的候选）

## 四、已驳回（不自动复活）

- （没有已驳回条目）

## 五、判断逻辑

- **J-0001** [product-architecture] 自动挡架构按“谁能改变事实”分平面，不按愿望数量加管家；全局健康只读，PM 不自动追问，验收截图不替人收口。
  - 为什么：DeepSeek Super-agent 与开源方案已把 Router/Worker/隔离 worktree/面板做成熟；继续平铺角色只会增加注意力成本。我们的价值应集中在可验证真相、落盘防丢失和首屏注意力纪律，并把不能自动化的部分清楚交还用户。
- **J-0004** [arch] 结果代理分是产品结果的代理与审计入口，不是用户结果真值。
  - 为什么：结果代理分只读可核验的结果代理信号；首问闭环率取不到时整体 unknown，unknown 不计 0；机器命令绿灯只算运行证据，不冒充用户结果真值。
- **J-0005** [product-architecture] 跨项目长期目标必须显式登记和显式收口，聊天中的一句话不能直接升成生效目标；账本未知时如实显示 unknown，不能伪装成 0；关系细节只进高级区折叠展示，不占首屏注意力。
  - 为什么：关系层服务跨项目长期目标，自动推断会把聊天里的一次性表达变成未经确认的生效约束；显式登记、Fail-closed 读取和面板折叠展示才能同时保住连续性与用户注意力。
- **J-0006** [arch] 守护首次安装必须显式授权；安装命令成功不等于常驻生效，只有 launchctl 回读到 running+pid 才算成功。
  - 为什么：用户明确要求不影响正常工作进程，且不允许开关绿进程无；常驻属于改变后台行为，应显式授权。安装命令成功和守护真在跑是两件事，后者必须以 launchctl 回读为证。
- **J-0007** [arch] 守护脚本必须按 launchd 固定解释器做兼容门禁：snapshot_prune.py 用 typing.Optional/List/Dict/Tuple，并由 /usr/bin/python3 -m py_compile 锁定 Python 3.9 兼容。
  - 为什么：Python 3.9 是 launchd plist 固定的系统解释器；3.10+ 类型语法在编辑机可编译但在守护机上会 TypeError。守护脚本与其门禁必须用实际执行解释器验证，不能靠开发机默认 Python。
- **J-0009** [design] 面板首屏只回答要不要我、是否在干对的事、替我做成了什么；落点预览用一行浅灰提示，不新增卡片，判不清必须明说，默认落账也必须写出当前项目。
  - 为什么：首屏注意力有限；面板应是挡风玻璃而非管家工作台。先让落点可见，才能减少错账本和用户盯盘；只读预览的每一句话都必须和真实提交的落点一致。
- **J-0010** [gate] 面板新增能力必须同步进工具契约门禁和渲染状态门禁，不能只靠功能测试：功能合入不等于回归防线存在。
  - 为什么：功能合入只证明当时写对了，不证明以后不会被误删。本次发现面板已调用 route_preview，但 selftest 只断言 called <= tool_names，删掉预览仍会绿；panel_render_check 也只覆盖 resolved。规范的文字输给顺手，必须把工具调用、cwd 依据、DOM 元素和三种真实渲染文案都写成机器断言，才能让能力长期成立。
- **J-0011** [process] 修复完成后必须同步翻 ledger 状态，否则闭环率会把已修复问题算成未闭环。
  - 为什么：修复有证据但状态没翻，会让闭环率把已修复问题继续算成未闭环；状态翻转要和证据、进展记录同轮落盘。
- **J-0012** [arch] 常驻自追踪必须幂等：真相内容没变时不重写生成视图和生成时间。
  - 为什么：每拍自动综合若把生成时间当内容变化，会产生假进展、无意义脏文件和反复冲突；内容指纹必须排除时间戳，只有事实变化才允许改生成视图。
- **J-0013** [architecture] 管家是责任视图而非人类岗位表；运行时按 execute/control/evaluate 分工，状态管家的路线效用职责只做只读检测与授权后的复核闭环，不声称全局最优。
  - 为什么：按人类岗位切 Agent 会把组织协同开销搬进运行时；自动挡应复用 Codex session/worktree/skill/MCP/daemon，只把责任做成可解释、可验证的边界。最优不是全局可判定的口号，而是目标可核对、偏离可发现、纠偏有边界、纠偏后复测的闭环；判据不足必须 unknown。
- **J-0014** [arch] 路线与效用健康不是新增管家或自动纠偏器，而是 evaluate 层的只读责任视图；最优是可证伪闭环，目标是目标可核对、偏离可发现、纠偏有边界、纠偏后复测。
  - 为什么：用户明确质疑按人类职能拆管家的合理性；运行时应按 execute/control/evaluate 组织责任，路线与效用健康只做只读发现、append-only 留账和 propose_review，不能自动纠偏，也不能把 unknown 冒充正常。
- **J-0016** [process] CLI 写命令的帮助探测不得用 unsupported flag 试探；未知参数可能被子命令当成标题或正文并写入账本，先读总帮助或源码，确认支持后再调用。
  - 为什么：2026-11-20 现场误触：truthctl accept 不支持 --help，直接探测把帮助请求登记成验收项 A-0138；帮助/参数探测必须先读 --help 总览或源码，不能把未知 flag 当真实业务输入提交给写命令。
- **J-0019** [workflow] 源码合并不等于现场生效：换代必须回读 install-state、release 元数据、daemon plist 和 live PID 四重一致；取不到写 unknown，不写 0 或正常。
  - 为什么：2026-11-22 宪法页修复在源码和手工 tick 已通过，但当前安装版 .121 仍不含修复；只有受控换代并回读现场才能证明用户现场生效。
- **J-0002** [process] 原生直连派活的最后一道闸是官方 remote-control 的 ChatGPT 登录/配对，不是 socket 路径或协议；不能用 API key 冒充 ChatGPT 身份，也不应把独立 app-server 会话包装成桌面宿主实时窗口。
- **J-0003** [product] 机器闭环和人工验收不能混为一谈
- **J-0018** [workflow] 源码合并不等于现场生效：换代必须回读四重现场证据
  - 为什么：2026-11-22 宪法页修复在源码和手工 tick 已通过，但当前安装版 .121 仍不含修复；只有受控换代并回读现场才能证明用户现场生效。

## 六、未接线与缺口

- `CC-0001` 自动挡在需要用户决策、人工验收或注意时，必须主动出现明确提示；同时回到产品路线，列出总体预期、剩余工作、卡点、未推进原因和下一步，不要只展示静态状态。 —— 高优先候选仍待人确认，尚未生效
- `CC-0002` 为什么这么慢？现场要对齐 29 号前的工程闭环、19 层进度百分比、下一步与卡点；主线当前 Release Gates 的口径分叉要修，36 题严格质量 RED 和 50 题真人盲评不能靠放宽门禁解决。 —— 高优先候选仍待人确认，尚未生效
- `CC-0003` openclaw 小生主动消息爱说空话：已修根因并验证。①7 个定时脚本（agent-{morning-brief,autodraw,evening-review,goodnight,midnight-goodnight,1am-check,2am-sleep}.sh）的 --message 是硬编码模板，20:00 那条「今天推进了什么/学到了什么/明天最重要的一件事」逐字来自脚本，不是模型自由发挥。已改为统一引用 ~/.openclaw/proactive-voice.md（新写的说话规则：必须先读上下文落到具体锚点，无锚点输出 HEARTBEAT_OK）。②小生的实际工作区是 workspace-main，但 MEMORY.md/HEARTBEAT.md/CURRENT_FOCUS.md/PRODUCTS.md 历史上只写在 workspace，他根本读不到自己的记忆，USER.md 也只有 1.2KB 空模板（真实档案 4.3KB 在 workspace）。已用真实文件同步（软链会让 read 工具报 path must be a regular file），并加 bin/sync-agent-workspace.sh 双向同步、7 个脚本每次先调用。③workspace/MEMORY.md 涨到 93925 行/96KB 其中 93822 行是纯空行，来源是 bin/xiaosheng_continuity_scheduler.py 的 sync_context_to_workspace() 里 after_section.rsplit('---',1)[1] 把空行反复接回，已修（strip + \n{3,} 折叠，跑 3 次幂等）并清理文件到 103 行/7KB。④workspace-main/AGENTS.md 原为 OpenClaw 英文默认模板，与 workspace/AGENTS.md 的小生人设冲突，已合并，并把「不输出 HEARTBEAT_OK」改成正确语义。验证：喂真实素材后运行，他输出「生长那条流程，阶段事件只能由后端 progress 帧推上来…谓语读不出来也是同一个根源…应该先把数据补对」，有具体判断无寒暄；无素材时正确输出 HEARTBEAT_OK。注意：workspace 仓库有 139 项他人未提交改动，未在其中提交。 —— 高优先候选仍待人确认，尚未生效
- `CC-0004` 打开自动挡面板自然语言触发不稳定：用户说“打开自动挡面板”后，Codex 会话只分析入口、没有调用 render_autopilot_panel，侧栏/内联面板都没有出现；需要在触发规范里明确写“必须调用 render_autopilot_panel 工具”，并给小白用户提供可复制指令 —— 高优先候选仍待人确认，尚未生效
- `CC-0005` 用户只在一个入口表达，系统负责跨项目/跨窗口路由，不让用户自己选 —— 高优先候选仍待人确认，尚未生效
- `CC-0006` 验收和交付必须基于真实证据与端到端闭环，不用绿字、记录或可合并替代生效 —— 高优先候选仍待人确认，尚未生效

## 七、书面宪法引用

### `docs/PROJECT_GLOBAL_RULES.md`

> # 项目全局规则
>
> 这是用户反复强调的长期诉求压缩入口。进入任何项目的实质工作前，如果项目根目录存在本文件，必须先读完并遵守；禁止让用户重复提这里已经写明的要求。
>
> 本文件是书面宪法源，不是第二套真相：生效状态仍以 `ops/truth/constraints.jsonl` 为准，候选与驳回仍以 `ops/truth/constraint_candidates.jsonl` 为准。用户说过的返修诉求或明确规则，无论针对全项目、全局还是工作流程，都必须先进入候选区，保留原话、来源、范围、优先级和证据；只有人工确认后才写入生效约束并参与门禁。
>
> ## 位置与推进
>
> 1. 位置确认一个任务只做一次：已经确认过自己在专属 worktree，后续轮次不许再把 `git rev-parse` / `git status` 当整轮工作，更不许只复述「还在专属 worktree 里」而不推进实质任务。
> 2. 确认命令必须和实质工作合在同一轮发出；任何一轮的输出都必须包含除自检之外的真实进展，读码、改码、跑验证至少其一。
> 3. 顶层是共享 checkout（`knowledge-graph`）且脏文件几十上百时，先 `./scripts/new-task.sh <任务名>` 到自己的 worktree 干活。
>
> ## 已有改动
>
> 4. 禁止「先改后搬」：已经改了的先 `git diff -- <你的文件>`，全是自己的就按整个文件搬；混着别人的就停下说明归属、等用户裁决，不要逐 hunk 硬搬。
>
> ## 读码纪律
>
> 5. 读代码先查 `docs/CODE_MAP.md` 拿行号，再 `sed -n` 只读 30–50 行；大文件（如 `aiService.ts` 2.5 万行）不整读、不反复扫同一文件。
>
> ## 长任务收尾
>
> 6. 只有当这一步任务真的做完、或确实需要用户确认才能继续时，才允许结束回合。
> 7. 结束回合前必须留下正文结论，说清做了什么、证据是什么、还差什么；只跑工具不写正文，等同于「跑着跑着停了」。
> 8. 本轮已经连续三次以上工具调用、且还有明确的下一步（读代码、改文件、跑验证），必须继续调用工具推进，不得用「我先汇报一下」「接下来我会」这类一句话总结结束回合。
> 9. 只读调查（查分支、看日志、curl 健康检查）本身就是正常推进；只要还有明确的下一步，就继续做完再结束回合。
> 10. 短问答、单次翻译、单条命令不受本节约束，直接正常回答即可。
>
> ## 机器校验
>
> - `python3 ops/test-project-constitution.py` 验证本文件被 `ops/project_constitution.py` 投影到 `docs/PROJECT_CONSTITUTION.md`，且「书面宪法文件不存在」缺口消失。
> - `python3 ops/selftest.py` 覆盖自动挡总门禁；新增或修改本文件的同一轮必须真实运行相关门禁并记录命令、cwd 与退出码。

### `docs/experience/GENERAL_EXPERIENCE_CONSTITUTION.md`

> # 通用体验宪法
>
> 这是 Codex 自动挡引用的跨产品体验原则摘要，不是自动挡的品牌方案，也不规定统一的颜色、字号、字体、圆角、栅格或间距。
>
> ## 层级
>
> 1. **正式标准**：以 WCAG 等具有明确适用范围和可测试成功条件的标准为准，作为适用产品的合规底线。
> 2. **行业共识**：从 GOV.UK、USWDS、Carbon、Primer、Fluent、Material 等成熟体系交叉提炼，用于设计判断，不冒充统一数值标准。
> 3. **项目决策**：颜色、字号、字体、栅格、间距、圆角、密度、动效和组件尺寸由项目根据用户、内容、平台和任务风险定义，并通过真实场景验证。
>
> ## 自动挡必须保留的判断
>
> - 语义先于样式，任务先于装饰。
> - 相关信息靠近，不相关信息分开；信息层级应能被扫描和解释。
> - 默认路径保持简单，低频复杂性渐进暴露。
> - 同一概念、操作和状态保持一致；差异只表达真实差异。
> - 默认、加载、空、无结果、错误、成功、禁用和权限限制等适用状态必须可见、可理解、可恢复。
> - 关键状态不能只靠颜色表达。
> - 响应式是内容和操作的重排，不是把整页机械缩小。
> - 可访问性进入组件、浏览器验证和上线验收，而不是交付后的附加检查。
> - 项目例外必须有原因、影响范围和验证证据。
>
> ## 不得伪造的结论
>
> - 不能把 8px、14px、16px、48px 或某个圆角值说成所有产品的行业标准。
> - 不能把“用了 Material、Carbon、Fluent 或 GOV.UK”说成体验质量已通过。
> - 不能把截图生成成功说成信息架构、任务完成或审美已经通过。
> - 不能用模型自评替代需要人判断的注意力顺序、信息负担和整体体验。
>
> ## 来源
>
> - W3C WCAG 2.2：https://www.w3.org/TR/WCAG22/
> - GOV.UK Design System：https://design-system.service.gov.uk/
> - U.S. Web Design System：https://designsystem.digital.gov/
> - Carbon Design System：https://carbondesignsystem.com/
> - Primer：https://primer.style/
> - Fluent 2：https://fluent2.microsoft.design/
> - Material Design 3：https://m3.material.io/
