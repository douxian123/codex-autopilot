# Codex 自动挡 · 全量上下文（唯一事实来源）

> 这份文件是**这个项目 + 相关对话**的唯一事实来源。opencode 和 codex 都改它，以它为准。
> 任何 agent 开工前先读它；改动产品行为、决策、进度、卡点后，**回写对应小节**，不要另起文档。
> 生成/核对时间：2026-10-15（当日现场核对，非转述）。
> 2026-10-24 复核：仓库升级到 `0.6.0-preview.18`，修复同版本目录被移除后重装会覆盖回滚指针的问题；`0.6.0-preview.17` 与更早版本保留可回滚。preview.17 含 10 路异步 worker 池、司令窗流式轮换，以及大仓库状态批量读取与按核并发扫描；461 条分支的状态根从 60 秒超时降到 15.182 秒退出 0。本地 app-server 控制 socket 通过纯 stdlib WebSocket 桥真机直连真实桌面宿主，`can_dispatch=true`。
> 2026-10-18 复核：源码 main 与 preview.11 运行时已修复状态根解析（无 KG_REPO 时 main 优先于 -trunk），真实桌面证据 A-0044/P-0041 已落盘 origin/main；A-0005 仍保留真人桌面验收。
> 2026-10-15 深夜 opencode 现场复核（见 12.7）：本机**不是沙箱**，已真跑安装并通过 5/6 验收；原「三条卡点」里的安装/网络两条已在本机解除，原生直连失败的根因当时**重新定位为 socket 选错（不是权限）**——第四节事实 4 已被推翻并更正。
> 仓库：`/Users/danta/Documents/Codex/codex-autopilot-native-autopilot`（分支 `main`）

---

## 一、30 秒版

**Codex 自动挡**是给 Codex 加的一层「工程控制面」：你平时打字的那个 Codex 输入框就是总输入框，零散需求进去，归类、落账、隔离工作区、跑验证、能合的自动合、收口、回写主线，全由它串起来。

- 形态：Codex 插件（原生风格面板） + macOS 菜单栏 Companion（状态锚点，不是第二套产品）
- 版本：源码/候选发布 `0.6.0-preview.21`；本机安装完成后应运行该版本，旧 `preview.20` 保留可回滚，阶段：内部可用
- 13 步产品路线：**7 步已完成、6 步进行中、0 步未开始**
- 验收：机器已自证项持续增加；当前 A-0005/A-0006/A-0007/A-0008 仍需真人/桌面操作
- 本机实测（2026-10-24）：候选版本 **`0.6.0-preview.21`** 包含共享 partial clone 禁止自动立即 prune 的保护；安装后旧 `preview.20` 保留可回滚。launchd 环境已确认带 `KG_SHARED=/Users/danta/.newmax/workspace/knowledge-graph`；原生直连真实桌面宿主 `connected=true`、`can_dispatch=true`。
- 自检：`python3 ops/selftest.py` → **24 组全过、退出码 0、约 58 秒**；再开真编译 + 真二进制两个开关 → **26 组全过**
- 自动实现（2026-10-17 补充）：`ops/central_daemon.py` 强制首步落盘并对“退出 0 但无改动”在同一 worktree 自动重试；真实 `codex exec` 已在隔离 fixture 完成 `L-0001` → `verify.txt` 内容 `hello-e2e` → 过门禁 → land → 收口。

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

### 2026-10-16 面板信息架构调整

- 主面板首屏明确回答：当前状态、正在做什么、当前步骤，以及工作归属的项目与路线。
- 路线关系以「项目 → 产品路线/当前阶段 → 当前工作项 → 验收/收口」呈现；路线列表突出进行中和待处理工作项。
- 项目归属已落成 `ops/truth/project_folders.json` 注册表：每个项目文件夹带自己的仓库根、产品、路线和工作项；面板从各项目自己的 `ops/truth` 读取，绝不把别的项目工作项混进当前项目。当前注册表先登记本仓库一个项目，多项目不再是一次性概念。
- 项目文件夹和每条工作项都显示来源对话（`source_thread_id`）；没有登记来源就明确写「未标注来源」，不伪造对话 ID。项目账本读不到时保留文件夹并显示读取失败，不显示成 0 个工作项。
- 菜单栏 Companion 是轻量状态锚点：展示状态、项目、当前工作/步骤和待处理事项；保留派活、截图、附件、打开完整面板，诊断与路径类动作收进「更多」。
- 出现人工验收、需要拍板或异常时，主面板升级为明确的待处理状态；菜单栏显示警示图标并通过 macOS 通知中心提醒。同一批待办只提醒一次，待办清空后新一批重新提醒。
- 灵动岛与桌宠不承担路线管理和工程追踪；未来可做入口或轻量状态外观。工程状态需要稳定、可扫描、可验证的主面板。

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
4. **`~/.codex/ipc/ipc.sock` 不是 app-server 控制 socket**（2026-10-15 opencode 实测更正，推翻旧说法）。它是桌面端的进程间 IPC / IDE 上下文通道：帧带 4 字节长度前缀（在 `ChatGPT.app/Contents/Resources/app.asar` 里可见 `Buffer.allocUnsafe(4)` 与 `IDE context socket…`），对它直接发 app-server 的 JSONL 会被对端**立刻关闭**；用 `codex app-server proxy --sock ipc.sock`（本地 0.148 与桌面自带 0.162 都一样）报 `failed to relay data between stdio and socket`、退出码 1。实测三连：raw socket 连接秒通但 recv 得 `b''`（对端关闭）、proxy 报 relay 失败、`codex doctor` 挂住。
5. **真正的 app-server 控制 socket 路径是 `~/.codex/app-server-control/app-server-control.sock`**，由官方 standalone daemon 创建。**本机当前事实**：该 socket 已存在（`codex app-server daemon version` → running，`appServerVersion 0.163.0`）；**该 socket 使用 WebSocket 协议**。`ops/codex_native.py` 已新增纯标准库 RFC6455 客户端，默认优先直连这个真实桌面宿主；2026-10-17 实测 `probe` → `transport=codex-app-server-websocket`、`connected=true`、`can_dispatch=true`。之前「发 stdio JSONL 无响应」是因为协议选错，不是必须 ChatGPT 配对。
6. `codex` CLI 已随 standalone 安装升到 **`0.163.0`**；桌面端自带二进制版本可能仍与 CLI 不同，以实际 `codex --version` / `codex app-server daemon version` 为准，不要写死旧版本。
7. 还有 `~/.codex/ipc/codex-center-control-20260814b.sock`，是陈旧残留，连接报 `Connection refused`。
8. 用隔离状态目录（`CODEX_HOME=<临时目录>`）时，真实 `codex app-server` 能初始化并建出原生会话——所以桥接代码本身是通的（但那是**独立实例**，不等于桌面宿主）；本地 standalone daemon 与桌面端打开同一状态库 inode，协议一旦通过鉴权/配对，thread 可以互通。
9. 独立实例续轮必须走官方 `thread/resume` + `turn/start`：直接对新 app-server 会话里的旧 thread 开 `turn/start` 会报 `thread not found`，先 `thread/resume` 能恢复并真实返回新 turn id（2026-10-17 真机验证）。
10. 本地 app-server 控制 socket 是 WebSocket 升级协议。现在 `ops/codex_native.py` 已用纯 stdlib 实现握手/帧收发，并且默认优先走真实桌面宿主的控制 socket；临时独立 daemon 与默认真实宿主均完成 `probe`/`dispatch`/`follow` 真机验证。`remote-control` 的远端配对仍可能要求 ChatGPT 登录，但本地控制 socket 已验证可绕过，不再把两条路径混写。
11. 历史数据库 `~/.codex/thread_history_1.sqlite` 只读打开，永不写宿主状态。
12. 开关当前实况：总开关 on、未暂停、自动合入开、自动解冲突开、自动实现开、常驻开、**自动 port 关**。

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
| 进行中 | 自动实现并收口 | 引擎齐备、真实 auto_exec fixture 已跑通（需求→真实 codex→门禁→land→收口）；缺一条从用户真实入口进入的完整端到端 |
| 进行中 | 路线与人机验收可见 | 面板已展示路线与人工验收项 |
| 进行中 | 跨应用入口 | 四个入口都实现完，待真机逐个验证 |
| 进行中 | 菜单栏 Companion | 可编译可安装，待真桌面启动验证 |
| 进行中 | 原生 thread 直连派活 | 协议本身已对齐真实二进制；本地控制 socket 的 WebSocket 桥已真机直连真实桌面宿主，`probe can_dispatch=true`。剩真实端到端与人工验收。见第四节 5、第七节和第十三节 |
| 进行中 | 真实端到端演示与预览发布 | 定义见第九节；auto_exec 真实需求闭环已由隔离 fixture 自证，A-0008 仍剩用户入口与真人桌面目视 |

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

## 七、卡点清单（2026-10-15 深夜 opencode 复核后重写）

原来的「三条沙箱卡点」在本机（opencode 运行环境）**全部不成立**——本机可写 `~/Applications`、`~/Library/LaunchAgents`，网络与 DNS 都通。逐条走查后，真实卡点只剩下面这两条：

| 卡点 | 现象（原文） | 谁能解 | 解除后能验什么 |
| --- | --- | --- | --- |
| 原生直连已解除（2026-10-17） | 旧现象：`app-server-control.sock` 已存在，但 stdio 发 JSONL 无响应；旧定位是 remote-control 要求 ChatGPT 登录/配对。新证据：该 socket 是 WebSocket 协议；新增 stdlib 桥后 `probe` 返回 `can_dispatch=true`，`transport=codex-app-server-websocket` | 已由代码桥接解除 | 剩 A-0008 真实端到端与人工验收 |
| 真实模型回合跑不了（旧记录） | 受限进程网络出口封闭 | **本机已不成立**：`curl https://example.com` → 200、DNS 正常解析 | A-0008 端到端（隔离项目已过 7/9，见 12.8） |

**重要**：安装写入、网络出口、原生直连均已在 2026-10-17 确认解除；`probe` 已能在真实桌面宿主上返回 `can_dispatch=true`。剩余是 A-0005/A-0006/A-0007/A-0008 的人工/真实需求验收，不再停在「必须 ChatGPT 配对」。

### 2026-10-16 中央窗生命周期修正

- 中央司令窗 / 产品总监窗默认不再挂高频 heartbeat。原因是每拍工具输出都会永久进入该窗上下文，中央窗越用越臃肿，会退化成「用最短的话结束轮次」。
- heartbeat/automation 优先挂在专用监督窗；中央窗只接收四段结论、拍板请求和路线变化。
- 迁移动作必须同时改 `target_thread_id`，并在监督窗 prompt 里写清监控对象；不要让中央窗继续承担巡检。
- `cockpit_pulse.py` / `cockpit_rotate.py` 仍是正确的测量与轮换底座，本修正只改默认挂载位置。

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

**第 3 步：确认原生直连的真实结论**
```bash
python3 ops/codex_native.py probe
```
看到什么算过：会打印 `can_dispatch`。**当前本机实测 = `true`**：`transport=codex-app-server-websocket`、`connected=true`、`can_dispatch=true`。如果以后变成 `false`，输出里的 `reason`/`hint` 会写清为什么，不要直接回到 ChatGPT 配对旧结论。

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

这三条是**历史阶段**记录，不是当前事实。它们不是代码 bug，而是当时执行进程的权限/环境边界：

| 卡点 | 现象（原文） | 谁能解 |
| --- | --- | --- |
| 安装写入被沙箱挡 | 写不了 `~/Applications`、`~/plugins`、`~/Library/LaunchAgents` | 用户在自己终端跑 `bash install.sh --verify` |
| 桌面宿主 socket 连不上 | `failed to connect to socket at /Users/danta/.codex/ipc/ipc.sock` / `Operation not permitted` | 在不受限终端跑 `python3 ops/codex_native.py probe` |
| 真实模型回合跑不了 | 受限进程网络出口封闭（DNS 都解析不了） | 在正常网络环境跑端到端 |

2026-10-17 复核：安装写入、网络、socket 权限三条均已解除；同日原生直连再解除：`probe` 返回 `can_dispatch=true`、`transport=codex-app-server-websocket`。剩余验收见第七节/第十三节。

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

### 12.7 opencode 接手复核（2026-10-15 深夜，现场实测，非转述）

这一步是 opencode 在**本机真实环境**里跑出来的，用来核对「上一轮结论里哪些还成立」。命令与原文都留在这里，谁都能复核。

**（1）本机不是沙箱——原「三条边界」有两条当场证伪**

```bash
touch "$HOME/Applications/.ap-write-probe"        # → OK（旧的「写不了 ~/Applications」不成立）
touch "$HOME/Library/LaunchAgents/.ap-write-probe" # → OK（launchd 目录可写）
curl -sS -m 5 -o /dev/null -w "%{http_code}" https://example.com   # → 200（网络通）
python3 -c "import socket;print(socket.gethostbyname('example.com'))"  # → 解析成功（DNS 通）
```

所以「沙箱挡安装」「网络出口封闭」在本机都不存在。

**（2）真跑了安装 + 验收（此前一直没做成的第一步）**

```bash
bash install.sh --verify     # 退出码 1
```

结果：`5 PASS / 1 FAIL`。PASS：版本目录落地、菜单栏应用已安装/可执行/已签名、插件市场指向该版本、Companion 配置已写入、总闸统一控制通道可用、面板首屏数据流正常。FAIL（唯一）：

```
[4] 原生直连自检
  FAIL  原生直连不可派活（只读历史可用）
        原因：app-server 未在限定时间内返回方法结果（只能只读看历史，不能派活）
```

随后 `codex plugin add codex-autopilot@personal` 刷新缓存成功 → `codex plugin list` 显示 `codex-autopilot@personal  installed, enabled  0.6.0-preview.1  …/releases/0.6.0-preview.1`；`~/Applications/CodexAutopilotCompanion.app` 已在跑（进程在）。

**（3）原生直连失败的根因：socket 选错，不是权限**

在现场桌面端**正在运行**（`/Applications/ChatGPT.app`、其 app-server 子进程都在）的前提下，逐个排查：

```bash
# a) raw 连接：连得上，但一发 JSONL 就被对端关闭
python3 - <<'PY'  # connect ipc.sock, send initialize, recv → b''（对端立刻关闭）
...
PY

# b) 官方代理：报 relay 失败
codex app-server proxy --sock ~/.codex/ipc/ipc.sock
# → Error: failed to relay data between stdio and socket   (退出码 1)
# 桌面自带的 0.162 二进制同样失败

# c) 真正的控制 socket 不存在
ls ~/.codex/app-server-control/     # 只有 app-server-startup.lock，没有 *.sock
codex app-server daemon start       # → managed standalone Codex install not found
codex app-server daemon version     # → failed to connect to …/app-server-control.sock (No such file)
```

**结论**：`~/.codex/ipc/ipc.sock` 是桌面端的进程间 IPC / IDE 上下文通道（帧是 4 字节长度前缀，见 `app.asar` 里的 `IDE context socket…`），**不是 app-server 的 stdio 控制口**；对它发 app-server 协议，对端会直接关闭连接。真正的 app-server 控制 socket 在 `~/.codex/app-server-control/app-server-control.sock`，只有官方 standalone daemon 起来时才存在（本机没装）。**这跟沙箱权限无关**——opencode 用满权限、桌面在跑，依然连不上，因为压根连的是另一条 socket。

**（4）附带发现：Codex 侧在同一仓库并发工作**

复核期间，`main` 上多出一个提交 `6866f00 docs(context): 全量上下文落盘（CONTEXT.md/AGENTS.md）+ 宿主不回话时控制面按预算降级`（作者 douxian123，23:02:55），把 CONTEXT.md/AGENTS.md + 上一轮的 `ops/codex_native.py`/`central_mcp.py`/`selftest.py` 收了进来。同时本机有常驻在跑：`central_daemon.py --loop`、`kgctl.py land codex/agent-sync-ledger`，以及自动挡把一条需求 `L-0080` 派给 opencode 执行（`opencode run --agent 司令`）。→ **本仓库此刻有并发写者**，动手前先 `git status`，只做加法、别覆盖别人的在途改动。

**（5）开放决策（需要用户拍板）**

原生 thread 直连派活有两条路，互斥：

- **路线 A（装 daemon）**：`curl -fsSL https://chatgpt.com/codex/install.sh | sh` 装官方 standalone Codex，再 `codex remote-control start` 起带控制 socket 的 daemon。风险/未知：该 daemon 与桌面端是否共用同一份 thread 状态、子对话能否出现在你桌面窗口集合里，**尚未验证**。
- **路线 B（改产品路线）**：承认「派进桌面窗口」在当前桌面版不可达，原生直连降级为「独立 app-server 建会话 + 只读历史」，派活仍走「台账 + worktree + 门禁」主链路（这条已机器自证）。代价：子对话不再自动出现在桌面 Codex 里。

**下一轮的优先顺序**：① 拿到 `install.sh --verify` 输出逐条关 A-0005/A-0006/A-0007（本机已跑，5/6 PASS，A-0005 前置已满足）；② 用户拍板原生直连走 A 还是 B；③ 陪跑 A-0008 全程留证；④ 收口 `0.6.0-preview.1` → 正式预览包；⑤ 推 `main` 到 `origin`（本机网络已确认可用）。

### 12.8 三线并行结果（2026-10-16 中午，用户拍板路线 A 后；历史过程，不是当前结论）

用户说「A 可以并行推」，于是三条线同时开跑。三条线的 agent 都已返回，下面是结论（证据文件在 `/tmp`，命令原文都在里面）。

**线 1 · 路线 A（原生派活）：部分通，没全通**

- 官方 standalone 装上了：`codex` 从 `0.148.0-alpha.15` 升到 **`0.163.0`**（`curl -fsSL https://chatgpt.com/codex/install.sh | sh`，无 sudo、无交互，退出码 0）。
- daemon 起来了：`codex app-server daemon version` → `running`，`appServerVersion 0.163.0`；**`~/.codex/app-server-control/app-server-control.sock` 已出现**（之前只有 lock 文件）。注意 `codex remote-control start` 本身退出码 1（`connection is errored`），但 daemon 本体健康。
- 但派活还是不通：当时 `probe` 依然 `can_dispatch=false`。当时代码只认 `ipc.sock`（后续已改成默认探测 `app-server-control.sock`）；当时对着新控制 socket 发 JSONL/长度前缀/proxy/HTTP 四种载荷全部无响应（proxy 报 `Broken pipe`）。后续当时定位为配对/鉴权硬闸；2026-10-17 用 WebSocket 协议直连同一控制 socket 后 `probe` 返回 `can_dispatch=true`，旧结论已被新证据推翻。
- 附带好消息：daemon 和桌面端打开的是**同一 inode** 的状态库（`state_5.sqlite`、`thread_history_1.sqlite`，1251 个 thread），文件层是同一份——协议一旦走通，线程就是互通的。
- 仓库当时零修改（只给了改法，没动手）：`ops/codex_native.py` 把控制 socket 放优先、`ipc.sock` 回落；该改动后续已落盘，但完成它仍差 ChatGPT 登录/配对这一关。

**线 2 · A-0008 留证：9 条里过了 7 条**

- 在全新 scratch 项目里跑完全程：intake→归类（`L-0001`，`gate` 泳道）→独立 worktree→门禁（修前 FAIL、修后三次 0）→自动合入→台账关闭→主线综合，全程命令+cwd+退出码+commit。证据：`/tmp/ap-e2e-evidence.md`。
- 没过的 2 条（第 2、8 条）都是“要在真实桌面面板/菜单栏亲眼看到”，缺桌面环境——机器侧已尽。
- **两起越界事故（已修好，如实披露）**：① agent 一条 `--help` 探测误落到本仓库账本并推了 `L-0005`（`1ee314b`）→ 已删该两行并推 `03f197a` 修复；② agent 没设 `KG_TRUNK`，把本仓库的 trunk 目录征用成了 scratch 的落盘树 → 已删掉被污染的目录，按项目自带逻辑重建了干净 trunk worktree（detached HEAD，主线一致）。

**线 3 · A-0005/6/7 机器确认：9 项 PASS**

- `preview-check.sh --no-install`：版本目录、菜单栏应用（已装/可执行/已签名）、市场指向、配置、总闸、面板首屏数据流——9 项 PASS；唯一 FAIL 仍是原生直连（和线 1 一致）。
- Companion 进程在跑，控制通道能读到真实状态；快捷键（⌥⌘A/S/D）、服务菜单、拖入三项代码都在；`panel_logic_check.py` 22 项全过。清单：`/tmp/ap-accept-checklist.md`。
- 剩下**必须人来**的：菜单栏图标亲眼确认、点图标看面板、⌥⌘D/⌥⌘S/拖文件/服务菜单逐个真按一遍、面板首屏观感、关总闸看关闭态（值都已机器证，只差人眼人手）。

**版本对齐**：仓库与已安装都是 **`0.6.0-preview.3`**（preview.1/2/3 三个版本目录都在，当前指向 preview.3）；`codex` 已升 0.163；自检 24 组绿；`main` 已推平（0 ahead / 0 behind）。

### 12.9 面板项目归口落盘（2026-10-16 下午）

- 已落盘并推送 `origin/main`：`codex/project-folders-multiproject`，主提交 `5564d8b` / 落盘台账 `3941506`。
- 能力：新增 `ops/truth/project_folders.json` 项目注册表；状态服务按注册表读取每个项目自己的 `ops/truth`；面板的项目文件夹显示项目、路径、产品、当前步骤、来源对话、进行/待办/验收计数，以及最多 4 条工作项和来源对话。
- 诚实边界：只登记了本仓库一个真实项目，没有伪造第二个项目；跨项目账本读不到时显示读取失败原因，不显示成 0。当前项目的来源对话是 `01a14331-6d8b-70c0-aba2-fc214034b9c5`。
- 机器门禁：`python3 ops/selftest.py` → 25 组全过、退出码 0；`python3 ops/panel_logic_check.py` → 24 项全过、退出码 0；`AUTOPILOT_CHROMIUM='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' python3 ops/panel_render_check.py --no-shot` → 10 项全过、退出码 0。三条都写进 `A-0024` 验收证据。

---

### 12.10 自动 port 复用冲突缓存（2026-10-24）

- 现象：守护进程每轮执行 `kgctl port --auto --go`，旧实现会对全部历史分支重跑
  `merge-tree`，在真实状态根连续 90 秒无输出后被强制终止，冲突里“只差一两个提交”的
  分支因此无法进入自动移植。
- 修复：`kgctl port --auto` 优先读取 tick 已维护的
  `ops/tasks/conflict_burden.json` 冲突清单；命中时只对候选做 `git cherry` 和改动体量
  检查，缓存缺失才回退完整 `scan()`。cherry-pick、land 门禁、失败整体回滚均不变。
- 证据：`python3 -u ops/selftest.py` 退出码 0，并新增断言锁定“冲突缓存命中且选中
  缓存分支”；真实 ThoughLoom 状态根只读预演命中 45 条冲突分支，`41.9s` 选出
  `codex/ci-red-registration-and-identity-lead-predicate`，不再超时。

---

### 12.11 恢复 10 路异步 worker 池（2026-10-24）

- 现象：`max_workers=10` 虽写在状态根配置里，但当前 `main` 的守护进程根本不读取这个键；
  自动实现仍按单条阻塞执行，端口全量扫描又会先超时，形成“窗口不多但活推不动”的假象。
- 修复：合并已有的 worker 槽位生命周期实现，按 `max_workers` 填满空闲槽；实现并发、
  门禁与 `kgctl land` 仍严格串行。保留本轮新增的“退出 0 但无改动”异步重派，不再用
  阻塞式重试占住其他槽位。缺省并发从 3 提升到 10，磁盘压力不会再下调并发。
- 证据：`python3 -u ops/selftest.py` 退出码 0；覆盖 3 槽真实并行启动、先完成后串行
  落盘、429 不阻塞其他 worker、僵尸 PID 释放槽位、无改动重派后落盘。

### 12.12 0 文件空壳不再拖住并发布队列（2026-10-24）

- 现象：真实状态根在磁盘只剩约 `400Mi` 时，`kgctl supersede` 对 0 文件旧分支仍逐个执行
  `git cherry`，单条扫描进入分钟级；这些分支既没有可 land 的产品改动，又长期显示在
  “可入队落盘”里，使自动挡看起来有大量候选却实际无可执行工作。
- 处置：先用仓库自带 `kgctl reap --go` 回收 186 个 `ahead=0` 已合并分支指针；脏
  worktree 保持原样。磁盘从约 `400Mi` 恢复到约 `4.0Gi`。
- 修复：`branch_content_landed()` 先判“相对自身基线 0 文件”，再决定是否调用
  `git cherry`；`supersede --fast` 直接归档 0 文件空壳；`queue`、`matrix`、`status`
  与 tick 上下文生成不再把空壳算作可落盘候选。
- 证据：新增自检“0 文件空壳不占 land 队列，supersede 不再为它们跑 git cherry”；
  `python3 ops/selftest.py` 首次命中已知 `test_auto_exec` 抖动，单测复跑 exit 0，
  完整复跑 exit 0；`panel_logic_check.py` 与 `panel_render_check.py --no-shot` 均 exit 0。
  真实状态根 `kgctl queue --light` 不再列出 0 文件分支，并单独报告 1 个待归档空壳。

---

## 十三、下一步（谁该做什么）

**用户（只有这几件，别的不用管）**：
1. 人工验收 A-0005/A-0006/A-0007（机器已证 9 项，剩的全是人眼人手：菜单栏图标、点图标、⌥⌘D/⌥⌘S/拖文件/服务菜单、面板观感、关总闸——具体步骤见第六节， checklist 在 `/tmp/ap-accept-checklist.md`）。
2. A-0008 差的 2 条（面板“最近完成”+菜单栏一致性）需在真实桌面补目视。
3. 原生派活已经解除旧闸：真机 `probe can_dispatch=true`，`transport=codex-app-server-websocket`。不再需要用户切换 ChatGPT 登录来做这一步。

**agent（下一步可做的）**：
- 把更多真实项目登记进 `ops/truth/project_folders.json`（每个项目自带 `ops/truth` 与来源对话），再用面板确认各自路线和工作项不串项目；目前只登记了本仓库。
- 已确认控制 socket 存在并用 WebSocket 桥直连真实桌面宿主：`probe can_dispatch=true`。下一步是真实需求端到端与人工验收，具体见第六/九节。
- 独立 app-server 链路已补 `thread/resume` 续轮并真机通过；本地控制 socket 的 WebSocket 桥已把“原生派活闭环”从独立实例推进到真实桌面宿主：`probe can_dispatch=true`。
- 人工验收结果回来后，逐条关闭 A-0005 / A-0006 / A-0007（改台账，不改生成物）。
- 收口预览发布（`0.6.0-preview.16` → 正式预览包）。
- `main` 已推平；有新提交继续保持推平。

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
