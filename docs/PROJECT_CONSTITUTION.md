# 项目宪法（自动投影，勿手改）

> 生成时间 2026-11-22 15:00:45　·　由 `python3 ops/truthctl.py constitution` 从约束/候选/判断台账投影
> 只有人确认过的约束才生效；候选与驳回项保留原话供追溯，不自动参与执行。

生效 2 · 待确认 4 · 已确认待落账 0 · 已驳回 0 · 判断逻辑 4

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

## 三、已确认待落账

- （没有已确认但还没在生效约束里看到的候选）

## 四、已驳回（不自动复活）

- （没有已驳回条目）

## 五、判断逻辑

- **J-0001** [product] 需要用户行动的状态必须同时在主面板和菜单栏显性出现：面板说明要做什么，菜单栏负责把注意力拉回来；同一批待办去重提醒。
  - 为什么：决策、人工验收和异常都意味着自动挡不能继续替用户推进；必须让用户看见并知道下一步，且提醒不能因轮询重复轰炸。
- **J-0002** [process] 原生直连派活的最后一道闸是官方 remote-control 的 ChatGPT 登录/配对，不是 socket 路径或协议；不能用 API key 冒充 ChatGPT 身份，也不应把独立 app-server 会话包装成桌面宿主实时窗口。
- **J-0003** [product] 机器闭环和人工验收不能混为一谈
- **J-0018** [workflow] 源码合并不等于现场生效：换代必须回读四重现场证据
  - 为什么：2026-11-22 宪法页修复在源码和手工 tick 已通过，但当前安装版 .121 仍不含修复；只有受控换代并回读现场才能证明用户现场生效。

## 六、未接线与缺口

- `CC-0001` 自动挡在需要用户决策、人工验收或注意时，必须主动出现明确提示；同时回到产品路线，列出总体预期、剩余工作、卡点、未推进原因和下一步，不要只展示静态状态。 —— 高优先候选仍待人确认，尚未生效
- `CC-0002` 为什么这么慢？现场要对齐 29 号前的工程闭环、19 层进度百分比、下一步与卡点；主线当前 Release Gates 的口径分叉要修，36 题严格质量 RED 和 50 题真人盲评不能靠放宽门禁解决。 —— 高优先候选仍待人确认，尚未生效
- `CC-0003` openclaw 小生主动消息爱说空话：已修根因并验证。①7 个定时脚本（agent-{morning-brief,autodraw,evening-review,goodnight,midnight-goodnight,1am-check,2am-sleep}.sh）的 --message 是硬编码模板，20:00 那条「今天推进了什么/学到了什么/明天最重要的一件事」逐字来自脚本，不是模型自由发挥。已改为统一引用 ~/.openclaw/proactive-voice.md（新写的说话规则：必须先读上下文落到具体锚点，无锚点输出 HEARTBEAT_OK）。②小生的实际工作区是 workspace-main，但 MEMORY.md/HEARTBEAT.md/CURRENT_FOCUS.md/PRODUCTS.md 历史上只写在 workspace，他根本读不到自己的记忆，USER.md 也只有 1.2KB 空模板（真实档案 4.3KB 在 workspace）。已用真实文件同步（软链会让 read 工具报 path must be a regular file），并加 bin/sync-agent-workspace.sh 双向同步、7 个脚本每次先调用。③workspace/MEMORY.md 涨到 93925 行/96KB 其中 93822 行是纯空行，来源是 bin/xiaosheng_continuity_scheduler.py 的 sync_context_to_workspace() 里 after_section.rsplit('---',1)[1] 把空行反复接回，已修（strip + \n{3,} 折叠，跑 3 次幂等）并清理文件到 103 行/7KB。④workspace-main/AGENTS.md 原为 OpenClaw 英文默认模板，与 workspace/AGENTS.md 的小生人设冲突，已合并，并把「不输出 HEARTBEAT_OK」改成正确语义。验证：喂真实素材后运行，他输出「生长那条流程，阶段事件只能由后端 progress 帧推上来…谓语读不出来也是同一个根源…应该先把数据补对」，有具体判断无寒暄；无素材时正确输出 HEARTBEAT_OK。注意：workspace 仓库有 139 项他人未提交改动，未在其中提交。 —— 高优先候选仍待人确认，尚未生效
- `CC-0004` 打开自动挡面板自然语言触发不稳定：用户说“打开自动挡面板”后，Codex 会话只分析入口、没有调用 render_autopilot_panel，侧栏/内联面板都没有出现；需要在触发规范里明确写“必须调用 render_autopilot_panel 工具”，并给小白用户提供可复制指令 —— 高优先候选仍待人确认，尚未生效
- `书面文档` docs/PROJECT_GLOBAL_RULES.md —— 书面宪法文件不存在
- `书面文档` docs/experience/GENERAL_EXPERIENCE_CONSTITUTION.md —— 书面宪法文件不存在

## 七、书面宪法引用

### `docs/PROJECT_GLOBAL_RULES.md`

- （文件不存在；上面「未接线与缺口」已记一笔）
### `docs/experience/GENERAL_EXPERIENCE_CONSTITUTION.md`

- （文件不存在；上面「未接线与缺口」已记一笔）
