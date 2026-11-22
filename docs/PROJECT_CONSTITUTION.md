# 项目宪法（自动投影，勿手改）

> 生成时间 2026-11-22 11:56:46　·　由 `python3 ops/truthctl.py constitution` 从约束/候选/判断台账投影
> 只有人确认过的约束才生效；候选与驳回项保留原话供追溯，不自动参与执行。

生效 2 · 待确认 6 · 已确认待落账 0 · 已驳回 0 · 判断逻辑 13

## 一、生效约束

- **C-0001** [预期/acceptance|panel · 来源 user/有机器门禁] 验收管家必须先自验再打扰用户：没有可复核证据的项不得进入需要你验收；补图必须用户确认，且只追加附件不替用户收口。
  - 验证命令：`python3 ops/test-acceptance-selfcheck.py && python3 ops/test-acceptance-screenshot.py`
- **C-0002** [预期/pm|constraints|panel · 来源 user/有机器门禁] PM 管家只聚合并呈现待确认预期、让路和追问候选；候选不能自动发消息，不能未经确认直接升成生效约束，也不能替用户决定方向、删数据或发布。
  - 验证命令：`python3 ops/test-pm-briefing.py`

## 二、待确认候选（用户说过，但尚未生效）

- **CC-0005** [高优先/项目/预期] 用户只在一个入口表达，系统负责跨项目/跨窗口路由，不让用户自己选
  - 命中：context_learning、跨会话重复
  - 重复证据：出现 6 次 / 跨 5 个会话
- **CC-0006** [高优先/项目/预期] 验收和交付必须基于真实证据与端到端闭环，不用绿字、记录或可合并替代生效
  - 命中：context_learning、跨会话重复
  - 重复证据：出现 10 次 / 跨 4 个会话
- **CC-0001** [普通优先/项目/预期] 状态根解析把「停在旧分支的共享检出」当权威状态根：resolve_state_root 的候选顺序是 (main, trunk, current_worktree())，而 ops/tasks+ops/truth 已签入版本库、每个 worktree 都满足 looks_like_state_root，于是共享检出永远胜出。共享检出 ops/central.config.json 在其检出分支 1a6c56b4 上根本不存在，central_config.load 读不到就退回内置默认 build=npm run build(cwd=frontend)，本仓库没有 frontend，land 门禁必红。2026-11-10 实录：KG_REPO 指向含 config 的工作树时 land 能一路 merge→门禁 ok→落盘；默认解析到共享检出时门禁在 merge 之后必失败。需明确「配置从哪读（代码根 or 状态根）」，或让 config 缺失时 fail-closed 报错而不是静默退回 npm 默认值。
  - 命中：永远、默认
- **CC-0002** [普通优先/项目/预期] 【守护起不来·机械根因】install.sh 5.5 段的守护改指被条件锁死：if [ -f "$HOME/Library/LaunchAgents/com.codex-autopilot.daemon.plist" ] 才执行 KG_REPO=.. python3 $DEST/ops/central_daemon.py --install。也就是说：只有『已经装过守护』才会重写 plist 并重启；一旦 plist 缺失（本次实录：活的 com.codex-autopilot.daemon.plist 不存在，只有 .disabled-20261105/.bak-* 一堆），install.sh 静默跳过整段，于是每次安装都成功、守护永远不装、launchctl list 里没有这个 job。product_eval 判据 2『跑得起来』因此恒红（status=not_installed），而 control.json 仍显示 enabled=true/daemon_enabled=true —— 开关绿、进程无，正是本产品最痛的那类静默失效。对照：预置 plist 里 ProgramArguments 指向 releases/0.6.0-preview.50/ops/central_daemon.py，已是旧版本路径，说明这套 repoint 逻辑本身此前也没把它修到当前版本。修法方向（需人拍板）：install.sh 缺 plist 时也应提供一条显式『首次安装守护』路径（或用 --install-daemon 显式开关），而不是把常驻与否只挂在文件是否存在上；重装后必须回读 launchctl list 作为证据。
  - 命中：必须、永远、拍板
- **CC-0003** [普通优先/项目/预期] 【既存·门禁环境】在部署目录 ~/plugins/codex-autopilot/releases/0.6.0-preview.82 里直接跑 ops/selftest.py 会 exit=1，失败点是 test_installer -> install.sh 报『--state-root 必须是 git 仓库：.../releases/0.6.0-preview.82』（release 目录不是 git 仓）。A/B 对照：换回发版源 40fc0f39 的原版 selftest.py 同样 exit=1，证明与本次 Stop 修复无关，是既存环境问题；同一份 selftest.py 在专属 worktree（git 仓）里跑全绿。影响有限：install.sh --verify 走的是 preview-check.sh 而非 selftest.py。修法方向：test_installer 在非 git 目录里应显式 skip 或造一个 git 夹具，而不是让线上部署位置的总闸恒红。
  - 命中：必须
- **CC-0004** [普通优先/项目/预期] 【自治等级·observe 模式 + explicit/inferred 两级授权】现状是开关制（auto_exec on/off），要么全自动要么不动。要改成：新管家/pass 先跑 observe（只记录我本来会做什么 + 依据 + 影响面，不执行），人确认后再转 auto；把授权分 explicit（用户明确要求）与 inferred（系统推断），inferred 一律不得触发破坏性动作（合并/删除/发布/改方向）。pass_registry 已有 mode（observe/propose/auto）字段，缺的是默认 observe 起步 + 转正判据 + 面板上让人一眼看到 observe 期的建议这条链。
  - 命中：不得、一律、默认

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

## 六、未接线与缺口

- `CC-0005` 用户只在一个入口表达，系统负责跨项目/跨窗口路由，不让用户自己选 —— 高优先候选仍待人确认，尚未生效
- `CC-0006` 验收和交付必须基于真实证据与端到端闭环，不用绿字、记录或可合并替代生效 —— 高优先候选仍待人确认，尚未生效
- `书面文档` docs/PROJECT_GLOBAL_RULES.md —— 书面宪法文件不存在

## 七、书面宪法引用

### `docs/PROJECT_GLOBAL_RULES.md`

- （文件不存在；上面「未接线与缺口」已记一笔）
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
