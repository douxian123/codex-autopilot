# Codex 自动挡 · 全量上下文（唯一事实来源）

> 这份文件是**这个项目 + 相关对话**的唯一事实来源。opencode 和 codex 都改它，以它为准。
> 任何 agent 开工前先读它；改动产品行为、决策、进度、卡点后，**回写对应小节**，不要另起文档。
> 生成/核对时间：2026-10-15（当日现场核对，非转述）。
> 仓库：`/Users/danta/Documents/Codex/codex-autopilot-native-autopilot`（分支 `main`）

---

## 一、30 秒版

**Codex 自动挡**是给 Codex 加的一层「工程控制面」：你平时打字的那个 Codex 输入框就是总输入框，零散需求进去，归类、落账、隔离工作区、跑验证、能合的自动合、收口、回写主线，全由它串起来。

- 形态：Codex 插件（原生风格面板） + macOS 菜单栏 Companion（状态锚点，不是第二套产品）
- 版本：`0.6.0-preview.1`，阶段：内部可用
- 13 步产品路线：**7 步已完成、6 步进行中、0 步未开始**，全部剩余工作都卡在「需要真实桌面环境/网络」这一条边界上
- 21 条验收：**17 条机器已自证**，4 条必须人在真机点
- 自检：`python3 ops/selftest.py` → **24 组全过、退出码 0、约 58 秒**；再开真编译 + 真二进制两个开关 → **26 组全过**

---

## 二、它是什么、为什么做

### 定位（用户原话提炼）

> 不是第二个 IDE，也不是独立项目管理工具，而是给 Codex 增加一层「工程控制面」，把零散输入变成可追踪、可验证、可收口的工程任务。

要补的是一句抱怨：Codex 本身很强，但**零散需求没有归口**。用户在浏览、写文档、看别人代码时冒出的问题，要么忘掉，要么得自己决定「放哪个对话、开哪个分支、派给谁」。

### 三层形态（形态决策，别再重开这个话题）

1. **入口层**：Codex 原生输入框、macOS 全局快捷键（⌥⌘S 截图派活）、系统服务菜单（选中文本/文件 →「发送到 Codex 自动挡」）、文件拖入菜单栏图标、截图拖入。所有入口最终写入**同一个 intake**，不允许各自维护任务逻辑。
2. **面板层**：Codex 原生风格侧栏面板，是主监控中心。首屏只回答：开没开、绑定哪个项目、正在处理什么、下一步、是否需要我介入、最近完成了什么。详细路线/证据/冲突收进二级。
3. **锚点层**：macOS 菜单栏 Companion。它只做状态锚点：状态图标、总开关、快速派活、当前任务、卡点摘要、打开 Codex 面板。

### 形态决策的理由（用户讨论过、已定）

用户考虑过三条路径：vibe island 式屏幕顶部灵动岛、Codex 原生生态里的桌宠、以及 staymusing 那种「没有形态、藏在右键里」。

**结论**：入口可以无处不在，但**面板必须存在**。用户原话：

> 我觉得还是需要有一个面板的，刚刚说像 musing 这种，它只是一个交互入口。没有形态的话，我就不知道它在哪里、怎么用。

所以：借鉴 musing 的「从当前上下文快速提交」，但面板不可省。桌宠人格动画、灵动岛视觉暂缓，它们只能做皮肤，不能承担工程语义。

### 明确不做

- 自建多 agent 框架（编排复用 Codex 会话 / worktree / skill）
- 第二套任务账本、第二套设置体系、独立任务看板
- 持续屏幕感知、后台自动截屏、隐式上传
- Electron 跨平台、正式签名公证分发
- 改动共享 checkout 里别人的在途工作

---

## 三、仓库与工程结构

仓库根：`/Users/danta/Documents/Codex/codex-autopilot-native-autopilot`

| 路径 | 职责 |
| --- | --- |
| `plugin/` | Codex 插件本体：`.codex-plugin/plugin.json`（清单）、`.mcp.json`、`ui/panel.html`（原生风格面板）、`skills/`（central-command 等） |
| `ops/` | 全部工程逻辑（Python，stdlib only）：控制面、编排、台账、自检 |
| `companion/` | macOS 菜单栏 App（Swift，`main.swift` 单文件 + `build.sh` 打 .app） |
| `install.sh` | 版本化安装／升级／回滚 |
| `preview-check.sh` | 一条命令验收（装 + 验，逐项 PASS/FAIL，退出码 = 失败项数） |
| `docs/` | 生成物与说明（见下） |

### ops/ 里几个关键文件

- `kgctl.py`：管「输入不分叉 + 合并串行」（intake、route、land、resolve、dash）
- `truthctl.py`：管「项目现在到底是什么状态」（mainline / product_state / decisions / judgments / ledger / progress / acceptance）
- `central_mcp.py`：MCP 工具层，面板和宿主都通过它读写
- `central_daemon.py`：常驻编排（launchd），负责自动合入、冲突拆解、自动实现
- `control_state.py`：唯一总开关与开关项（`enabled` / `paused` / `auto_land` / `auto_resolve` / `auto_port` / `auto_exec` / `daemon_enabled`）
- `codex_native.py`：Codex 官方 app-server 协议桥（建原生会话、归入窗口集合、follow-up、只读历史降级）
- `companion_api.py`：Companion 唯一通道，不许 Companion 自己造状态文件
- `selftest.py`：全量自检（24 组默认；两个环境开关可扩到 26 组）

### docs/ 里的文件

- `PRODUCT_MAINLINE.md`：产品主线（自动综合，勿手改）
- `PROJECT_STATUS.md`：项目状态一屏（自动生成，勿手改；已加入 .gitignore）
- `PREVIEW.md`：预览包说明（安装、验收、权限、升级、回滚、子对话归置）
- `CONTROL_PLANE.html`：HTML 驾驶舱（**备用视图**，不是主面板）

**注意**：`docs/PRODUCT_MAINLINE.md` 和 `docs/PROJECT_STATUS.md` 是 `truthctl` 的生成物。要改状态，改台账，不要改这两个文件。

---

## 四、锁定的技术事实（核对过，别再猜）

1. **Codex 桌面端本体 = `/Applications/ChatGPT.app`，bundle id `com.openai.codex`**（从 Info.plist 核实）。已注册 `codex://` URL scheme。
2. **官方 app-server 方法名**：移动会话进/出窗口集合是 `thread/section/move`（不是 `threadSection/move`，旧写法会被服务器判成 unknown variant）。列表/创建分别是 `threadSection/list`、`threadSection/create`。项目里 6 个原生调用点已逐条对真实服务器（144 个可接受方法）核对通过。
3. **会话归属窗口集合的字段是嵌套的** `thread.section.id`（旧代码按扁平 `sectionId` 读，面板会永远显示「暂无窗口」）。
4. **桌面宿主控制 socket**：`~/.codex/ipc/ipc.sock`。文件存在，但受限进程连接返回 `Operation not permitted`。
5. `codex` CLI 在 `/Users/danta/.local/bin/codex`，版本 `codex-cli 0.148.0-alpha.15`。
6. 用隔离状态目录（`CODEX_HOME=<临时目录>`）时，真实 `codex app-server` 能初始化并建出原生会话——所以桥接代码本身是通的。
7. 历史数据库 `~/.codex/thread_history_1.sqlite` 只读打开，永不写宿主状态。
8. 开关当前实况：总开关 on、未暂停、自动合入开、自动解冲突开、自动实现开、常驻开、**自动 port 关**。

---

## 五、产品路线：13 步到哪了

| 状态 | 步骤 | 说明 |
| --- | --- | --- |
| 已完成 | 唯一入口收需求 | 零散需求一个口进，不用选对话/分支/角色 |
| 已完成 | 自动归类并写进项目真相 | 问题/决策/进展/验收都有稳定台账 |
| 已完成 | 安全分支自动落盘与冲突收口 | 能合的自动合，真冲突拆任务 |
| 已完成 | 常驻自追踪 | 守护启停入口已实现并机器验证；真机常驻待安装验收 |
| 已完成 | 总开关与统一本机控制面 | 关掉回到 Codex 原生；所有入口共用一个控制通道 |
| 已完成 | Codex 原生风格侧栏面板 | 首屏数据流已机器验证；观感需人眼过一遍 |
| 已完成 | 版本化安装与回滚 | 不可变版本目录 + 当前指针，旧账本永不覆盖 |
| 进行中 | 自动实现并收口 | 引擎齐备并自检通过，缺一条真实需求跑完全程 |
| 进行中 | 路线与人机验收可见 | 面板已展示路线与人工验收项 |
| 进行中 | 跨应用入口 | 四个入口都实现完，待真机逐个验证 |
| 进行中 | 菜单栏 Companion | 可编译可安装，待真桌面启动验证 |
| 进行中 | 原生 thread 直连派活 | 协议对齐真实二进制并验证；派进用户桌面窗口需不受限环境 |
| 进行中 | 真实端到端演示与预览发布 | 定义见第九节，缺真实环境 |

---

## 六、验收清单

### 已机器自证（17 条，机器可复现，不用人管）

安装器完整路径隔离跑通、Companion 产物可安装、总闸关闭后全部自动动作被拒且不删现场、预览验收一条命令跑完且如实报错、统一入口端到端（派单→归类→台账→主线→面板）、面板首屏每个值来自真实控制层、装出来的副本自带项目绑定、后台常驻开关真启停 launchd（失败不谎报）、派活自动归入项目窗口集合（同名复用不重复建）、原生协议与真实 codex 二进制对齐、面板按官方嵌套字段显示集合内会话、菜单栏完整面板回到 Codex 本体、原生方法名 6/6 对真实服务器存在，以及各代全量自检组数。

复现命令见第十节。

### 需要你拍板（4 条，人眼/手感，机器代替不了）

**A-0005 菜单栏 Companion 在真实桌面启动后可用**
1. 终端跑 `bash install.sh`
2. `open ~/Applications/CodexAutopilotCompanion.app`
3. 菜单栏应出现自动挡图标
4. 点图标：应看到总开关、当前任务、需要处理、派活入口
5. ⌥⌘D 应回到 Codex 里的完整面板

**A-0006 面板首屏在 Codex 里显示真实状态，关总闸后有明确关闭态**
1. 在 Codex 里打开自动挡面板
2. 首屏应显示：是否开启、当前项目、正在处理什么、下一步、需要你处理
3. 点「原生自检」，结果应如实（连不上就说连不上）
4. 关掉总闸，首屏应显示已关闭、Codex 已恢复原生状态

**A-0007 跨应用入口逐个可用**
1. 任意 app 里按 ⌥⌘S 截图并加一句话派活
2. 把文件拖到菜单栏图标上派活
3. 选中一段文本 → 服务 →「发送到 Codex 自动挡」
4. 每次派完，面板里都应出现对应的新任务（不是各存一份）

**A-0008 一条真实需求端到端走完并留证据**
判定标准见第九节。

---

## 七、卡点清单（3 个，全部是环境边界，不是代码问题）

| 卡点 | 现象（原文） | 谁能解除 | 解除后能验什么 |
| --- | --- | --- | --- |
| 安装写入被沙箱挡 | 沙箱进程写不了 `~/Applications`、`~/plugins`、`~/Library/LaunchAgents` | 用户在自己终端跑 `bash install.sh --verify` | A-0005、A-0006、A-0007 的前置条件 |
| 桌面宿主 socket 连不上 | `failed to connect to socket at /Users/danta/.codex/ipc/ipc.sock` / `Operation not permitted` | 在不受该限制的终端里跑 `python3 ops/codex_native.py probe` | 任务能否真派进桌面 Codex 窗口 |
| 真实模型回合跑不了 | 受限进程网络出口封闭（DNS 都解析不了） | 在用户正常网络环境跑端到端 | A-0008 |

**重要**：这三个都不是「代码有 bug」，而是「执行这个沙箱进程没有那些权限」。桥接代码本身已被证明是通的（隔离状态目录下真实 app-server 能建出原生会话）。

---

## 八、我该做什么（用户操作清单）

按顺序，一条一条来。每条都告诉你「看到什么算过」。

```bash
cd /Users/danta/Documents/Codex/codex-autopilot-native-autopilot
```

**第 1 步：装 + 立刻验收**（这一条覆盖原来要手工分步做的全部）
```bash
bash install.sh --verify
```
看到什么算过：末尾打印逐项 PASS，退出码 0。如果仍打印若干 FAIL，把输出贴回来——它设计成「哪一步没成、为什么没成」都直接写出来，不会把没跑成的说成通过。

**第 2 步：打开菜单栏应用，确认图标出现**
```bash
open ~/Applications/CodexAutopilotCompanion.app
```
看到什么算过：菜单栏出现图标（不是 Dock 图标）。点它应弹出小面板，里面有总开关、当前任务、需要处理、派活的输入框、「截图」「附件」「打开 Codex」「完整面板」按钮。

**第 3 步：确认原生直连的真实结论**（必须在不带沙箱限制的终端里跑）
```bash
python3 ops/codex_native.py probe
```
看到什么算过：会打印 `can_dispatch`。`true` 说明能把任务派进你桌面的 Codex 窗口；`false` 会如实说明原因（比如 Codex 没启动）。

**第 4 步：按 ⌥⌘D 回到 Codex 里的完整面板**
看到什么算过：Codex 里显示自动挡面板，首屏有「是否开启 / 当前项目 / 正在处理什么 / 下一步 / 需要你处理」。关掉总开关，首屏应变成明确的关闭态。

**第 5 步：端到端一条真实需求**（判定标准见第九节）

**第 6 步（可选）：全量自检，含真编译 Companion 与真二进制协议**
```bash
AUTOPILOT_SELFTEST_COMPANION=1 AUTOPILOT_SELFTEST_REAL_CODEX=1 python3 ops/selftest.py
```
看到什么算过：打印 26 组 `selftest ok`，退出码 0。

---

## 九、怎么算「端到端真的走完一遍」

这是 A-0008 的判定标准，9 条全中才算过。别用「自测通过」替代。

1. **入口**：用任一个真实入口提一条真实小需求（建议 ⌥⌘S 截图 + 一句话，最贴近真实场景）。
2. **进箱**：面板或菜单栏出现这条任务，状态从「已接收」开始变。
3. **归类落账**：它被自动归进某个泳道，问题台账里出现稳定 id（形如 L-00xx）。
4. **隔离执行**：系统在独立 worktree 里干活，不碰你正在用的检出。
5. **真验证**：验证门禁真实跑过，留下命令、cwd、退出码。
6. **真合并**：门禁通过且无冲突时自动合入 main；有冲突时如实报出卡在哪，而不是假装完成。
7. **收口回写**：台账关闭、进展落盘、产品主线更新。
8. **可视**：面板「最近完成」出现这条，菜单栏状态与面板一致。
9. **省心**：全程你没有手动切对话、选分支、派给谁。

当前进度：第 2、3、7、8 条机器已自证（A-0010）；第 4、5、6 条引擎齐备并通过自检，缺一条真实需求跑全程；第 1、9 条依赖你的环境。

---

## 十、工程纪律（改代码必守）

1. **非破坏、正向**：不 revert 别人的在途改动，不删来源不明的产物，不用 `git add -A` / `git checkout .` / `git stash` / `git reset --hard`。
2. **提交是加法**：到可验收阶段就提交，WIP 也提交。收尾必须留「分支名 + 能否干净合并」。
   干净判定：`git merge-tree --write-tree --name-only main <分支>` 空输出 = 能干净合。
3. **不许把门禁改成能过**：不放宽断言、不为了让红变绿而删测试。
4. **真实验证不是结论**：贴命令、cwd、退出码原文，不贴转述。
5. **改完必跑自检**：`python3 ops/selftest.py`。动了 Companion 或原生协议时加对应开关。
6. **防空转**：同一命令/同一失败连续 3 次就停，贴原始错误并说明已试过的差异。
7. **回写上下文**：改完产品行为或决策，更新本文件对应小节；产品行为变化用 `truthctl product set`，判断逻辑用 `truthctl distill add`。
8. 开关语义：`enabled` 是总闸；关掉后所有 mutation 被拒且不删现场。总闸只管外挂是否工作，不隐式改细分开关。

---

## 十一、汇报规范（用户明确要求，别让他再提一遍）

1. 一律中文。代码、路径、命令、报错原文保持原样。
2. 回复用四段结构：
   - ① 你做了什么、路线与整体工程是什么样
   - ② 已完成哪些、哪些可验收、还差什么、需要用户关注什么
   - ③ 用户在哪里能摸到这些改动
   - ④ 为什么现在可以停
3. **不要在回复里报变量名**。文件里可以写清楚，对话里不要。
4. 不要用「这个分支要不要合」收尾——自己判断并给出结论。
5. 不要停在半路。「做完」就是做完；卡住就贴原始错误 + 已试差异 + 明确需要用户做什么。
6. 报进度必须有新增内容（新证据、新结论、新动作、明确阻塞），不重复播报状态。

---

## 十二、这个对话的脉络（从哪来、在纠结什么）

### 12.1 对话身份与体量（2026-10-15 22:55 现场核对）

| 项 | 值 |
| --- | --- |
| 会话 | Codex 桌面版线程 `01a1386b-8f4a-7620-ade7-0240decda13b`，标题「接手 Codex 自动挡项目并查看进度」 |
| 跨度 | 2026-10-15 01:57 → 22:48（本地），22 个工作回合 |
| 用户输入 | 26 条（其中 8 条是 goal 自动续跑，同一句「那就验收…为什么老是在这停住啊？」） |
| 工具调用 | 907 次 |
| 原始记录（任何人可复核） | `~/.codex/sessions/2026/10/15/rollout-2026-10-15T01-56-51-01a1386b-8f4a-7620-ade7-0240decda13b_01a13b8f-5362-7e31-920b-b893ad658798.jsonl`（约 20MB） |
| 对话侧工作目录 | `/Users/danta/Documents/Codex/2026-10-14/codex-svgcodex-autopilot-mdsvg-15-6`（Codex 桌面版的会话目录）；**真实改动全部落在本仓库** |

### 12.2 用户诉求按时间演进（原话，本地时间）

1. **01:57** 再梳理这件事：「查找预期信息 / 盘点现有进度 / 规划最优解」，并给出取向——「我感觉还是尽可能地还原 Codex 原生面板的形态」。
2. **02:03** 定位成型：应该更像**一个 Codex 外挂 + 一个总开关**（开=外挂开启，关=Codex 原生），尽可能像 Codex 原生 UI，可以常驻 Mac 顶部栏；「你是世界顶级的产品经理和产品设计师…最好要在这两天能够把它做好，并发布出去」。
3. **11:12** 「执行方案」。
4. **11:19** 抛出形态三选一：① vibe island 式屏幕顶端灵动岛（鼠标上移看状态；截图碎片需求直接派给总司令窗）；② 桌宠；③ staymusing 那种「没有形态、藏在右键里」。
5. **11:33 → 11:42** 让 agent 以顶级 PM/设计师身份定「最优交互实现」。
6. **11:48（关键锚点）** 「我觉得还是需要有一个面板的，刚刚说像 musing 这种，它只是一个交互入口…没有形态的话，我就不知道它在哪里、怎么用」。→ **面板不可省**，这是形态决策的定音。
7. **11:51** 「PLEASE IMPLEMENT THIS PLAN」：产品路线与工程补足计划（定位 = 给 Codex 加一层工程控制面；覆盖需求捕捉→归类→真相→路由→worktree 隔离→执行调度→验证门禁→合并收口→人机验收→监控→卡点→关闭恢复）。
8. **12:21** 「那就去做完呀。」
9. **15:23（规范）** 定下汇报规范：四段结构（做了什么与路线 / 已完成与验收 / 我在哪里能摸到 / 为什么可以停），**不要在对话里报变量名**，「把这个记到你的规范里」。
10. **15:32** 「所以，这个需求做完了吗？真正落地了吗？…去看一下所有工程项的现有进度到哪里了？还差什么工作？我要怎么验收？」
11. **15:42 → 18:08（8 次 goal 续跑）** 「那就验收，你不是很清楚吗？按照这个产品路线去把它打通做完呀，**为什么老是在这停住啊**？」——这是整段对话最强的诉求：**不要停在半路**。
12. **19:13** 「卡点是什么？你要我怎么操作？在终端里跑什么东西？你让我操作的清单是什么？验收的清单是什么？怎么算端到端真的走完一遍？」
13. **22:28（本轮）** 加全局 AGENTS.md 约定，并要求：把项目 + 对话的全量上下文归总到**一个本地文件**，作为 opencode 与 codex 共享的唯一事实来源，不用再手工搬上下文。
14. **22:38 / 22:48** 「好了吗？怎么这么慢啊？」「好了吗？我真是晕了。」

### 12.3 脉络摘要（首版七条，保留）

1. 起点：用户带着一份《codex-autopilot-交接提示词》进来，要求接手这个项目并「看进度」。
2. 第一次推进：确认项目独立于 ThoughLoom；目标是 Codex 自动挡本身；要求干净合并、不破坏已有进度。
3. 产品形态讨论：用户反复推敲「外挂应该长什么样」——灵动岛 / 桌宠 / 无形态右键入口三条路。最终定成三层形态（入口 + 面板 + 菜单栏锚点），并强调**面板必须存在**。
4. 计划落地：用户给出完整的产品路线与工程补足计划（统一控制面、原生风格面板、跨应用入口、菜单栏 Companion、版本化安装发布），要求按此实现。
5. 反复出现的痛点：**用户不想看到「停在半路」**。原话：「那就验收，你不是很清楚吗？按照这个产品路线去把它打通做完呀，为什么老是在这停住啊？」所以每轮都要有真实推进，卡住必须精确说明卡在哪、谁能解、怎么解。
6. 用户还要求：围绕 Codex 已有生态做最小、最优雅的耦合，优先复用而不是重造；主司令窗派活、子对话跟进监控、窗口集合归类、scheduled automations 做主动跟进。
7. 本轮：用户要求把项目 + 对话的全量上下文归总到一个本地文件，作为 opencode 与 codex 共享的唯一事实来源，避免手工搬上下文。

### 用户最在意的四件事

1. 用户能在任何工作上下文快速把问题交进来。
2. 用户能在一个明确面板里找到任务和真实进度。
3. 自动挡不会悄悄做用户不知道的事。
4. 任务最终有真实验证、明确证据、可追踪的收口。

### 12.4 反复卡住的三条边界（用户不满的真实来源）

这三条都不是代码 bug，而是**执行它的那个进程没有那些权限**（详见第七节）：

| 卡点 | 现象（原文） | 谁能解 |
| --- | --- | --- |
| 安装写入被沙箱挡 | 写不了 `~/Applications`、`~/plugins`、`~/Library/LaunchAgents` | 用户在自己终端跑 `bash install.sh --verify` |
| 桌面宿主 socket 连不上 | `failed to connect to socket at /Users/danta/.codex/ipc/ipc.sock` / `Operation not permitted` | 在不受限终端跑 `python3 ops/codex_native.py probe` |
| 真实模型回合跑不了 | 受限进程网络出口封闭（DNS 都解析不了） | 在正常网络环境跑端到端 |

### 12.5 上一轮的卡点已修（2026-10-15 现场核对）

那次「跑了几十分钟什么也不打印」是两个原因叠加，都已查清并修掉：

1. **输出被块缓冲**（现象层）：`python3 ops/selftest.py > 文件` 时不满 8KB 不落盘，看起来像死机。→ 一律用 `python3 -u`，别把输出重定向成文件再看。
2. **真卡点（真 bug，产品级）**：桌面宿主控制 socket 的行为从「立刻报 `Operation not permitted`」变成「连得上但不回话」，于是 `_route_summary → codex_native.snapshot()` 一路等到超时。`central_mcp.control_action` 的返回值里带状态快照，所以**点一次开关要等几十秒**。定位证据是 `faulthandler` 打出的真实栈：

   ```
   .../ops/codex_native.py line 65 in _read_response
   .../ops/codex_native.py line 315 in snapshot
   .../ops/central_mcp.py line 873 in _route_summary
   .../ops/central_mcp.py line 1163 in _status_payload
   .../ops/central_mcp.py line 1232 in _with_status
   .../ops/central_mcp.py line 1291 in control_action
   .../ops/selftest.py line 998 in test_daemon_control_action
   ```

   修法：给「查宿主」这条路加时间预算（默认 6 秒，可用 `CODEX_AUTOPILOT_NATIVE_TIMEOUT` 调），候选服务共享同一份预算而不是按个数翻倍；**面板/开关那条快路压根不连宿主**（只读缓存状态 + 只读历史库）。顺手修了自检里打桩不还原造成的污染，并补了一组自检把这个行为锁住。

**证据**：`python3 -u ops/selftest.py` → `EXIT 0`、用时 57.6 秒、24 组 `selftest ok`（新增一组「宿主不回话时状态查询按预算降级，快路不连宿主、候选不翻倍」）。`python3 ops/codex_native.py probe` 不再对着黑屏干等：先往 stderr 打一行「正在连接…最多等 N 秒」，再如实输出 JSON，连不上就是非零退出码。

**工作区现状**：`CONTEXT.md`、`AGENTS.md` 为本轮新增；`ops/codex_native.py`、`ops/central_mcp.py`、`ops/selftest.py` 为本轮修复改动。`main` 领先 `origin/main` **20 个提交**（`merge-tree` 干净，缺网络）。

### 12.6 接手者从这里继续（命令）

```bash
cd /Users/danta/Documents/Codex/codex-autopilot-native-autopilot

# 想看进度就看得到（-u 关缓冲，别再重定向成文件）
python3 -u ops/selftest.py 2>&1 | tee /tmp/selftest.log

# 只跑卡住的那一组（原生窗口集合归组）
python3 -c "import sys; sys.path.insert(0,'ops'); import selftest; selftest.test_native_section_grouping()"

# 环境边界本身（要在不受沙箱限制的终端里跑）
python3 ops/codex_native.py probe
```

**下一轮的优先顺序**：① 拿到 `install.sh --verify` 输出逐条关 A-0005/A-0006/A-0007；② 陪跑 A-0008 全程留证；③ 收口 `0.6.0-preview.1` → 正式预览包；④ 推 `main` 到 `origin`。

---

## 十三、下一步（谁该做什么）

**用户（只有这几件，别的不用管）**：
跑第八节第 1 步（`bash install.sh --verify`），把输出贴回来。然后按第 2–6 步，把 4 条人工验收逐条过掉。

**agent（下一步可做的）**：
- 拿到用户安装输出后，逐条关闭 A-0005 / A-0006 / A-0007
- 陪用户跑 A-0008，全程记录命令、cwd、退出码、commit
- 真实端到端通过后，收口预览发布（`0.6.0-preview.1` → 正式预览包）
- 把 `main` 推到 `origin`（当前领先 20 个提交，`merge-tree` 干净，需要网络）

---

## 十四、快速命令索引

```bash
cd /Users/danta/Documents/Codex/codex-autopilot-native-autopilot

# 装 + 立刻验收（用户主要入口）
bash install.sh --verify

# 只验收，不动安装
bash preview-check.sh

# 全量自检（默认 24 组，约 58 秒）
python3 ops/selftest.py

# 全量自检 + 真编译 Companion + 真二进制协议（26 组）
AUTOPILOT_SELFTEST_COMPANION=1 AUTOPILOT_SELFTEST_REAL_CODEX=1 python3 ops/selftest.py

# 面板首屏数据流（不依赖浏览器）
python3 ops/panel_logic_check.py

# 原生直连真实结论（要在不受沙箱限制的终端里跑）
python3 ops/codex_native.py probe

# 真相与台账
python3 ops/truthctl.py                       # 一屏真相 + 写 docs/PROJECT_STATUS.md
python3 ops/truthctl.py ls acceptance          # 验收清单
python3 ops/truthctl.py synthesize             # 综合产品主线

# 常驻守护
python3 ops/central_daemon.py --status
python3 ops/central_daemon.py --install | --uninstall
```

---

## 十五、更新这份文件的规则

1. **它说了算**。和 README、聊天记录、旧交接文档冲突时，以本文件为准（然后修另一处）。
2. 改产品行为：更新第二节（定位/形态）或第五节（路线）。
3. 改状态：优先改台账（`truthctl`），第五节/第六节按台账同步。
4. 卡点解除或新增：更新第七节，写清现象原文与谁能解。
5. 每轮收口：更新第十三节「下一步」，让接手的人知道从哪继续。
6. 不要在别处复制粘贴上下文；需要引用就让对方读这个文件。
