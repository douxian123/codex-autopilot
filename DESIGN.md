# Design

## Source of truth

Status: Active. Date: 2026-11-22.

Surfaces: Codex 插件面板（`plugin/ui/panel.html`）、MCP UI resource、Companion 状态锚点。

Evidence reviewed: `docs/PANEL_INTERACTION_DESIGN.md`、`plugin/ui/panel.html`、插件元数据、已发布 `0.6.0-preview.121` 面板、`@openai/apps-sdk-ui` 调研结论（`0.2.2`，MIT）、`docs/OPENSOURCE_RESEARCH.md`、用户关于原生风格与注意力秩序的历史规范。当前 `panel.html` 仍是 reference implementation，不是最终像素稿；真实宿主截图验收尚未完成。

## Brand

Codex Autopilot 是一个安静的项目交付控制面。公开名称固定为 `Codex Autopilot`；“自动挡”仅是内部称呼，不进入用户可见界面。气质是克制、可信、工具性：不喧哗、不拟人化、不用营销语言。

Trust signals: 原生容器质感、可追溯证据、明确的人工决策边界、缺失数据如实说“取不到”。

Avoid: 深色大底、渐变徽章、装饰线堆叠、彩色胶囊阵列、内部 ID / PID / 路径 / 原始命令、无人解释的口号（如“万法归一”“兜底入口”“后台常驻”）。

## Product goals

- 用户不看日志就能知道：需要自己决定什么、系统正在做什么、真正闭环了什么。
- 多项目并行时，信息按项目归属隔离；项目内信息只出现在项目卡内，全局信息放在项目卡外。
- 所有职责都有可见入口，但管家是内部实现，不作为用户导航结构。
- 项目目标、模型路由、归档、验收证据在需要时可查，不抢首屏注意力。

Non-goals: 另起独立工作台、复制 Codex 私有源码、把内部工程读数搬上首屏、用视觉装饰替代信息层级。

Success signals: 5 秒内回答三问；首屏无警告态时不出现警告色；任何“需要你”条目都有下一步动作；数据缺失不冒充 `0` 或 `正常`。

## Personas and jobs

Primary persona: 同时推进多个 Codex 项目的开发者或产品负责人。Jobs: 快速判断哪个项目需要自己介入；确认系统在正确目标上推进；查看真实收口与证据；必要时修改项目目标、模型路由或授权。

Context: Codex 桌面端侧栏面板，窄容器、高频扫视、低容忍噪音。

## Information architecture

Attention order: 需要你的决定 > 当前状态与当前目标 > 真实闭环 > 项目内工程详情 > 全局配置与归档。

项目卡是主要容器，采用面性卡片而非线性任务列表。折叠态保留：项目名、状态、当前阶段、当前步骤、整体进度、当前目标。展开态才显示工程项、验收、档案、证据。项目目标可自动生成，也可用户修改；清空后恢复自动摘要。

模型路由属于全局配置，放在项目卡外，按“快速处理 / 标准施工 / 深度决策”表达。全局待路由箱同样放在项目卡外。上下文档案、全量 PRD、决策待确认项、验收清单按项目归属收纳。

## Design principles

- 首屏只放用户值得注意的信息；可观测性从“默认展示”改为“需要时可查”。
- 装饰线只在区分真实层级时使用；能用留白、字重、底色分区表达时，不新增边框。
- 管家状态压缩成一条健康摘要，明细进入工程详情。
- 按钮最多两个主操作；未知状态用中性色和“取不到”，不用绿色伪装正常。
- 用户可见文案必须是人话；内部术语、命令、ID、路径只进 `title` 或折叠证据。

## Visual language

Color: 继承宿主 token，顺序为 `@openai/apps-sdk-ui` 语义 token > 旧宿主 CSS 变量 > 浏览器兜底。品牌强调色使用 OpenAI 绿，只出现在状态、主按钮、进度与选中态。`#10A37F` 仅作为插件元数据品牌色，不扩散成整页主题。

Typography: 继承系统字体；不做 viewport 字号缩放；标题 16/19px，正文 12.5–13px，辅助文字 11–11.5px；letter-spacing 保持 0。

Spacing: 4px 基准，面板内边距 16–18px，区块间距 11–14px，卡片内边距 14–18px。

Shape/elevation: 卡片圆角不超过 8px；阴影只用于轻浮起，不叠加发光。避免 linear-gradient 徽章。

Motion: 仅状态更新使用短过渡；加载动画轻且不闪烁；尊重 reduced motion。

Imagery/iconography: 使用熟悉的线性图标，避免手绘 SVG 装饰；图标只表达动作或状态，不承载品牌叙事。

## Components

现有面板组件：header、三问 section、project card、alert、switch row、details fold、list、badge、progress、toast。新实现优先复用 `@openai/apps-sdk-ui` 的 `Button`、`Badge`、`Alert`、`Avatar`、`Input`、`Textarea`、`Select`、`Switch`、`Slider`、`Tooltip`、`Popover`、`Menu`、`Markdown`、`CodeBlock`、`Icon`，不另造平行设计系统。

Token ownership: 颜色与圆角由宿主 CSS 变量拥有；组件局部尺寸由组件自身拥有；产品语义（状态、阶段、路由等级）由面板数据契约拥有。

## Accessibility

Target: WCAG 2.2 AA。所有折叠区用原生 `details/summary`；开关有 `aria-label` 与 `aria-pressed`；图标按钮有 `title` / `aria-label`；长文本换行，不覆盖相邻内容；键盘可完成确认、暂停、展开、编辑目标；reduced motion 下去掉非必要动画。

## Responsive behavior

侧栏窄宽度默认单列；项目卡可折叠；正文使用 `overflow-wrap:anywhere`；固定尺寸控件（开关、图标按钮、进度环）不因动态文案改变尺寸；hover-only 信息必须有可访问替代。

## Interaction states

Loading: “正在读取状态…”，不显示旧数据伪装成当前值。

Empty: 明确说“还没有记录”或“无需你处理”，不放警告色。

Error/unknown: 写“取不到”，给出下一步；“已连通/正常”只在证据存在时出现。

Success: 用“需求 -> 结果”的自然语言展示真实收口；证据留在折叠区。

Disabled/off: 用中性灰，不用警告色；关闭状态明确说明 Codex 原生功能不受影响。

## Content voice

中文、短句、结论先行。用户可见文案使用业务语言；不出现“万法归一”“兜底入口”“后台常驻”“有东西需要我吗？”等内部或不明含义表达。状态动词统一为“需要你的决定 / 正在推进 / 已闭环 / 取不到”。项目标题与线程标题遵循既有 `[AP][project][lane]` 契约，但只作为追溯元数据显示。

## Implementation constraints

当前实现是单文件 HTML + MCP JSON-RPC，先消费宿主 token；若迁移到 React，锁定 React 18/19，并按官方顺序引入 Tailwind 与 `@openai/apps-sdk-ui/css`。不得把当前 DOM 或 CSS 固化为“原生 Codex 组件”。每次 UI 改动必须同步更新 `ops/panel_logic_check.py` 与 `ops/panel_render_check.py`，并在真实宿主截图验收前明确标注未验证。

## Open questions

- [ ] 真实 Codex 桌面端截图对比尚未完成；owner: 当前产品负责人；impact: 不能宣称最终视觉验收通过。
- [ ] `panel.html` 是否迁移到 `@openai/apps-sdk-ui` React 组件层；owner: 当前实现负责人；impact: 影响组件复用深度与包体积。
- [ ] 插件市场描述是否全部改为英文品牌语气；owner: 当前产品负责人；impact: 影响公开商店首屏认知。
