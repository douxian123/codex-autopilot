# Codex Autopilot

给 Codex 装一个总输入框。你把零散、即时的感受丢进去——「这里视觉不对」「这个流程别扭」「又冒出个 bug」——归类、排期、追踪、收口由它接手。你不用再想这条需求发哪个对话、开哪个分支。

## 它是什么

Codex 自动挡是一层装在本机的项目编排，把「管一个复杂项目」从人脑搬到一本账和一组守护进程上，由 Codex 自己推进。

它分两层，职责不混：

1. **插件层**（`~/plugins/codex-autopilot/`）：一个 skill + 一组 MCP 工具 + **一份自带的运行时代码**（纯 Python 标准库）。运行时代码跟着插件走，所以插件升级后不会出现「skill 说支持新工具、实际跑的还是项目里那份旧脚本」。目标项目里不需要放运行时代码。
2. **账本层**（你项目里的 `ops/tasks/`、`ops/truth/`、`ops/context/`、`docs/`）：唯一入口、问题台账、主线 / 决策 / 进展 / 验收。全是纯文本 + JSONL，可读、可 diff、可提交。

它不是新模型，不是新 IDE，也不改 Codex 二进制。用的全是 Codex 公开的扩展点（skill / MCP / plugin）。

它是 **Codex 的自动挡**。手动挡下，拆需求、分分支、盯合并、验收全靠你盯着；自动挡下这些交给编排层，只有改方向、删数据、发布这类事才停下来问你。

## 为什么做这个

今天用的 Agent 形态还很雏形，不是面向人交互的最优解。

模型侧，下一个突破普遍被说成 RSI——模型自己训练自己。工程侧对应的应该是 **自追踪、自运行**：AI 自己拆任务、自己分派、自己跟进、自己验收，把人的干预减到最少。

但现在的 coding agent（Codex 也一样）恰恰没做到。它要求你：自己把需求拆成一条条；自己决定每条发哪个对话、开哪个分支；自己盯每一条链路；每一步手动验收；再自己收拾散落在几十个对话和分支里的「左脚踩右脚」的合并。模型有能力处理这些，缺的是**把它们串起来、闭环跑通的那层工程**。

所以有一条第一性的要求：

> 不该让用户把需求发进各种各样的对话和分支，弄得支离破碎、左脚绊右脚。Codex 应该是一个**万法归一**的输入框，承接模糊需求，把它拆成最优解。

用户摸索时会不断冒出零散、即时的东西：视觉摸到的问题、体验和功能架构摸到的问题、bug 摸排查出的 bug。这些不该要求用户自己归类——归到哪个对话、新建哪个分支、派给哪个角色，应该由编排本身来做，给出统一的最优解，而不是让用户迁就那些分支。

顺着这个思路，一个复杂项目真正需要持续管的就四类东西：

- **产品主线**：PRD、讨论与决策进展。
- **问题清单**：bug、反馈、体验问题，自动归类、存档、追踪。
- **开发进展**：最近一次干了什么，离主线还差多少。
- **验收清单**：各模块 / 验收项的进度，以及需要你注意的项。

Codex Autopilot 就是把这四类收进一本账，让 Codex 自己推进。

## 它解决什么问题

一个人同时开几十个对话、几十个分支做复杂项目，效率不是被「写代码」吃掉的，是被这四件事吃掉的：

1. **输入碎**：零散需求不知道发哪，随手开个对话，做了左边记不住右边。
2. **分支海**：每条对话一个分支，谁跟谁能合、合了会不会互相覆盖，没人说得清。
3. **工作区脏**：共享检出里叠着十几个任务的半成品，谁都不敢提交。
4. **人肉验收**：每一步都要人 check，人成了瓶颈。

传统解法是写一沓规范（`AGENTS.md`、workflow 文档）。但规范不会自己动手：再多文字，也不会主动把一句「这里视觉坏了」记进台账、判断该进哪条泳道、在没有冲突时把它落进 `main`。这套东西把最贵的几个动作做成了命令和守护进程，而不是又一份文档。

## 怎么装

两步。在插件仓库里跑一次：

```bash
# 1) 装插件 + 绑定项目（不带参数则绑定本仓库自身）
./install.sh --refresh-plugin /path/to/your-project

#    可选：指定落盘用的主干 worktree；完全不动项目里已存在的文件用 --no-seed
./install.sh --state-root /path/to/your-project --trunk /path/to/your-project-trunk

# 2) 重启 Codex 桌面端 / CLI，打开 Plugins → Personal → 安装「Codex 自动挡」
```

`install.sh` 做三件事：

1. 把插件装到 `~/plugins/codex-autopilot/`，**连运行时代码一起复制**（`ops/*.py`）。
2. 把插件登记进 `~/.agents/plugins/marketplace.json`（原文件备份为 `.bak-codex-autopilot`）。
3. 在目标项目里补上缺失的账本文件（`ops/tasks/`、`ops/truth/`、`docs/`）。**已存在的一律不动**，不覆盖你已有的台账。

装完插件的 `.mcp.json` 里只有两句关键信息：运行时代码走插件自己的 `./ops/central_mcp.py`，状态根 `KG_REPO` 指向你的项目。于是插件是通用、可升级的一份，项目只负责存账——升级插件重跑一次 `install.sh`，项目里的旧文件不会拖累它。插件一次绑定一个项目，换项目重跑一次。

> 注意：Codex 的 personal marketplace 以 `$HOME` 为根，插件实体必须在 `~/plugins/<name>/`，清单在 `~/.agents/plugins/marketplace.json`，这是两个目录，别写混。

## 怎么用

装好之后，**那个普通的 Codex 输入框就是总输入框**。你只做一件事：把感受原样说出来。分类、路由、排期、追踪由它接手。

最省事就是对 Codex 直接说：

> 左边导航 rail 的排版坏了，图标贴到左缘，被裁掉了。

对话会把它记进唯一入口，归类、排期、追踪，然后告诉你现在最该动哪条。

### 怎么知道它真的在起作用

三个当场能验证的痕迹：

- 它回你的不是「好的」，而是「已记入唯一入口 + 落到哪条泳道 + 现在最该动哪条」。
- 项目里 `ops/tasks/inbox.jsonl` 多一条你的原话，`ops/truth/ledger.jsonl` 多一条带 `L-000n` 编号的台账项。
- `docs/PROJECT_STATUS.md`（快照）和 `docs/CONTROL_PLANE.html`（驾驶舱）跟着刷新：最近干了什么、还差什么、哪些分支能合、哪些在冲突。

### 什么时候开、什么时候关

- **非常驻（默认）**：插件在，skill / MCP 就随叫随到；会话结束就停，不占后台。
- **常驻（可选）**：装一个 launchd 守护，没开会话时也按周期推进安全子集。开是 `--install`，关是 `--uninstall`。
- **彻底关掉**：在 Plugins 页禁用 / 卸载插件即可。账本还留在项目里，随时能再装回来。

也可以让对话直接调 MCP 工具：`intake_request`（一句话走完整条链，首选）/ `sync_control_plane` / `next_actions` / `project_status` / `route_inbox` / `convergence_plan` / `generate_context` / `dashboard` / `capture_request` / `suggest_branch`。

想手敲命令：

```bash
# 运行时代码跟着插件走，项目里只留账本；手敲时用插件里的这一份
PLUGIN=~/plugins/codex-autopilot

# 一句话入口：记录 -> 归类 -> 刷新真相 -> 刷看板 -> 给下一步（一步完成）
python3 "$PLUGIN/ops/kgctl.py" intake "左侧导航 rail 排版坏了，图标贴左缘被裁切"

# 一次抛了多条、或想重扫全项目时，跑主循环
python3 "$PLUGIN/ops/kgctl.py" tick

# 现在最该做什么 / 项目到哪了 / 打开驾驶舱
python3 "$PLUGIN/ops/kgctl.py" next
python3 "$PLUGIN/ops/kgctl.py" status
python3 "$PLUGIN/ops/kgctl.py" dash --open
```

| 命令 | 作用 |
| --- | --- |
| `python3 "$PLUGIN/ops/kgctl.py" intake "…"` | 一句话走完整条链：记录 → 归类 → 刷新真相 → 刷看板 → 给下一步 |
| `python3 "$PLUGIN/ops/kgctl.py" inbox "…"` | 只把一条零散需求追加进唯一入口（不归类，脚本/调试用） |
| `python3 "$PLUGIN/ops/kgctl.py" tick` | 主循环：路由 inbox → 刷新真相 → 收拢冲突 → 刷看板/上下文 → 推真相层 |
| `python3 "$PLUGIN/ops/kgctl.py" converge [--go]` | 把几十个冲突分支收成少数几个 convergence 任务（默认预演） |
| `python3 "$PLUGIN/ops/kgctl.py" context <分支>` | 给分支写一份版本化 Context Pack（主线/决策/相关未决/DoD） |
| `python3 "$PLUGIN/ops/kgctl.py" dash [--open]` | 生成（可打开）驾驶舱 `docs/CONTROL_PLANE.html` |
| `python3 "$PLUGIN/ops/kgctl.py" next` | 只回答一个问题：现在最该动哪几条 |
| `python3 "$PLUGIN/ops/kgctl.py" status` | 一屏看清：待路由、分支可合/冲突、冲突收拢族、共享检出脏度 |
| `python3 "$PLUGIN/ops/kgctl.py" queue` / `matrix` | 盘点分支：哪些能干净合、哪些在抢同一个文件 |
| `python3 "$PLUGIN/ops/kgctl.py" land <分支>` | 串行落盘：合并 → 真实构建门禁 → 写进展 → 推 `main` |
| `python3 "$PLUGIN/ops/kgctl.py" reap --go` | 回收已合并的分支（默认只预演） |
| `python3 "$PLUGIN/ops/truthctl.py" ls` / `next` | 看台账 / 看优先级（同 `kgctl next`） |

## 常驻 vs 非常驻

- **非常驻（默认）**：有会话时由 skill / MCP 触发 `tick` / `land`。关掉会话就停。
- **常驻（可选）**：装一个 launchd 守护，没有会话时也按固定节奏推进**安全子集**。

```bash
PLUGIN=~/plugins/codex-autopilot
python3 "$PLUGIN/ops/central_daemon.py" --status     # 看守护 / 上一轮 / 队列
python3 "$PLUGIN/ops/central_daemon.py" --dry-run    # 只预测本轮会 land 哪些分支
python3 "$PLUGIN/ops/central_daemon.py" --once       # 跑一个周期就退出
python3 "$PLUGIN/ops/central_daemon.py" --install    # 装成常驻（默认每 15 分钟一轮）
python3 "$PLUGIN/ops/central_daemon.py" --uninstall  # 停用并删除守护
```

守护从插件目录的 `.mcp.json` 读绑定（`KG_REPO` / `KG_TRUNK`），不用再手动 export 环境变量。代价说清楚：它会真的在后台跑 `tick` / `land`，也就是会自己推进 `main`——走的是和手动一样的护栏，但确实不需要你在场。

常驻自动做三件事：路由唯一入口里的散需求、把与 `main` 无冲突的分支串行落盘、把冲突分支收拢成族（`converge`）。每个落盘分支仍走 `kgctl land` 的全部护栏（真实构建、`origin/main` 对齐、失败即丢弃）。**有冲突的分支不会自动合**，而是被收进少数几个 convergence 分支，交给一个 agent 逐族解，不再让你逐条回答「这 60 个分支要不要合」。通知是去重的，只有「有分支落盘 / 有分支卡住 / 新收拢出冲突族」时才打扰你。运行目录 `~/.codex-autopilot/`。

## 运行机制

一条需求从你嘴里到 `main` 上：

```text
            ┌────────────────────────────────────────────────────────────┐
 你说的话 → │  唯一入口  ops/tasks/inbox.jsonl （追加，不改历史，不分类）   │
            └───────────────┬────────────────────────────────────────────┘
                            │  kgctl tick  /  MCP sync_control_plane
                            v
            ┌────────────────────────────────────────────────────────────┐
            │  固定泳道归类   ops/tasks/lanes.json （design/bug/arch/gate） │
            └───────────────┬────────────────────────────────────────────┘
                            v
            ┌────────────────────────────────────────────────────────────┐
            │  项目真相  ops/truth/*.jsonl                                │
            │  mainline 主线 · decisions 决策 · ledger 问题 ·              │
            │  progress 进展 · acceptance 验收   →  docs/PROJECT_STATUS.md │
            └───────────────┬────────────────────────────────────────────┘
                            v
            ┌────────────────────────────────────────────────────────────┐
            │  串行落盘  kgctl land：merge-tree 预检 → 构建门禁 →          │
            │  写进展 → 推 main → update-ref 同步所有 worktree 的 main     │
            └───────────────┬────────────────────────────────────────────┘
                            v
            ┌────────────────────────────────────────────────────────────┐
            │  常驻（可选）  launchd 每 15 分钟：路由散需求 + land 无冲突   │
            │  分支；冲突分支不碰。状态没变化就静默。                      │
            └────────────────────────────────────────────────────────────┘
```

几个刻意的设计：

- **一本账、一处根**。状态根由 `ops/state_root.py` 统一解析（`KG_REPO` > 主干 worktree > 主工作树 > 当前 worktree），不会出现「这个 worktree 说待办 10 条、那个说没有」。
- **只追加，不改历史**。台账是 JSONL，状态变化写新记录；快照和看板都是自动生成物。
- **合并串行**。`land` 一次只推一个分支，用 `git merge-tree --write-tree` 在**写之前**先模拟合并；冲突就不动，能合才走构建门禁，构建失败就丢弃、`main` 一动不动。
- **永远不动别人的工作区**。推进 `main` 引用用 `git update-ref`，从不 `checkout` / `reset` / `stash` 任何人的在途改动。
- **项目相关的旋钮集中在一处**。主干分支名、分支前缀、落盘用的主干 worktree、构建门禁命令、泳道定义，全在 `ops/central.config.json`（环境变量可覆盖）；不配就用对「单仓 + `frontend` 子目录 + `npm run build`」开箱即用的默认值。

## 冲突怎么收口

这是它和「一沓规范」最不一样的地方。分支多起来之后，最容易把人反复打断的不是写代码，是「这 60 个分支谁跟谁冲突、谁先合」。自动挡不把这个问题丢回给你：

- `kgctl converge` 按「抢同一批文件」把冲突分支收成**少数几个 convergence 任务**（一个连通族 = 一个任务），而不是几十条待裁决项。
- 每个 convergence 任务建一条 `codex/converge-<hash>` 分支和独立 worktree，把源分支相继并进去；冲突**原样保留**成 marker（JSONL 另做行级并集），`main` 和源分支都不动。
- 于是交给一个 agent 的是「去解这一族的冲突」，不是「你告诉我该合哪个」。`kgctl tick` 已自动包含这一步。

## 参考了什么

**同类开源做法（设计参考，无代码复用）** —— 做架构调研时，这类问题在开源里已经攒出一套共识：每个任务一个独立 worktree 隔离、输入先进收件箱再分诊、spec 先于执行、合并走串行队列、issue/task 图当唯一事实来源。代表项目：`max-sixty/worktrunk`、`automazeio/ccpm`、`smtg-ai/claude-squad`、`BloopAI/vibe-kanban`、`github/spec-kit`、`aviator-co/av`。

它们各解决一块（worktree 隔离 / 看板 / spec / merge queue），没有一个是「单入口 → 分类 → 路由 → 隔离执行 → 串行收口 → 项目真相」的完整总装。

为什么没直接拿它们当底座：这些项目都是各自独立的产品面（Web 应用、带自己数据库的服务、或要求你换一套 CLI 工作流），而这里要的是一层长在 Codex 原生输入框里的薄编排。嵌一个外部运行时进来，成本和摩擦都比复用自己的标准库大。所以这里复用它们的**做法**（每个任务一个 worktree、输入先进收件箱再分诊、spec 先于执行、合并走串行队列），不复用它们的代码。

**观点参考：Lauren「poteto」的工程分享（P-Stack）** —— 这套设计的两个判断直接来自她：

- 能靠人拦下的错误，就该写成自动规则。对应这里的门禁前置、冲突分支不自动合、只在拍板时打扰你。
- 规则从硬到软排序：改代码结构 > 自动检查 > 写规则 > 靠人检查。对应这里用「固定泳道」代替再写一份规范。

## 能力边界（诚实版）

- 它**不保证**全自动。改产品方向、删数据、发布仍然要停下来问你，并给出「选 A 会怎样 / 选 B 会怎样」。
- 常驻守护只做**安全子集**：路由散需求 + land 与 `main` 无冲突的分支。冲突分支不碰。
- 本机 stdio MCP 是面向**本机** Codex 的形态；官方插件市场的 MCP 要求 HTTPS 端点，所以这里用的是本机 compat 布局。
- 归类基于关键词 + 文件路径重叠 + 代码标识匹配，**是启发式，不是语义理解**。它可能判错泳道；判错不致命（只影响优先级和归属），你可以在 `ops/central.config.json` 里覆盖规则，或手工改台账。
- 冲突收拢（`converge`）只做**机械合并**：建分支、并入源分支、保留冲突 marker。真正解冲突仍需一个 agent 在 convergence worktree 里做，它不假装能替你判断「谁对」。

## 目录

```text
运行时（跟着插件走，安装时复制到 ~/plugins/codex-autopilot/ops/）
  kgctl.py            唯一执行入口：输入归一 + 分支盘点 + 串行落盘
  truthctl.py         项目真相层唯一读写入口：主线/决策/问题/进展/验收
  central_mcp.py      把 CLI 暴露成 MCP 工具（stdio）
  central_daemon.py   常驻执行器（安全子集）
  state_root.py       状态根统一解析（避免多本账）
  central_config.py   项目相关旋钮：主干/前缀/worktree/构建门禁/泳道

账本（留在你项目里，可提交、可 diff）
  ops/tasks/          inbox.jsonl（唯一入口）· lanes.json（固定泳道）· convergence.json（收拢记录）
  ops/truth/          mainline.json · decisions/ledger/progress/acceptance.jsonl
  ops/context/        每分支的 Context Pack（.json / .md）
  docs/CONTROL_PLANE.html  驾驶舱（kgctl dash 生成）

plugin/               Codex 插件：skill + MCP 清单（运行时 ops/ 安装时复制进来）
install.sh            安装脚本
```

## License

MIT，见 [LICENSE](LICENSE)。
