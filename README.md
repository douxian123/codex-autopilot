# Codex Autopilot · 中央司令台

> **一句话**：给 Codex 装一个「总输入框」。你只管把零散、即时的感受丢进去——
> 这里视觉不对、那个流程别扭、又冒出个 bug——归类、路由、排期、落盘、追踪由它接手。
> 你不需要再决定「这条需求发哪个对话、开哪个分支、派给哪个角色」。

可以把它理解成 **Codex 的自动挡化**：手动挡下，换挡（拆需求、分分支、盯合并、验收）
全靠你脚下和眼里；自动挡下，这些动作交给一层编排，你只在真正要拍板时被叫一下。

它**不是**新的模型、**不是**新的 IDE、也**不改** Codex 二进制。它是挂在 Codex 原生
入口（skill + MCP + 常驻调度）上的一层控制面。

---

## 它解决什么

一个人同时开几十个对话、几十个分支做一个复杂项目时，效率不是被「写代码」吃掉的，
是被这四件事吃掉的：

1. **输入碎**：零散需求不知道发哪，随手开个对话，做了左边记不住右边。
2. **分支海**：每条对话一个分支，谁跟谁能合、合了会不会互相覆盖，没人说得清。
3. **工作区脏**：共享检出里叠着十几个任务的半成品，谁都不敢提交。
4. **人肉验收**：每一步都要人 check，人成了整个系统的瓶颈。

传统解法是写一沓规范（AGENTS.md、workflow 文档）。但**文字输给顺手**：规范再多，
也不会自动把一句「这里视觉坏了」记进台账、自动判断该进哪条泳道、自动在没冲突时把它
落进 `main`。所以这套东西把最贵的动作做成了**命令和守护进程**，而不是又一份文档。

一句话的判据：**你不该把需求丢进各种对话框和分支，左脚绊右脚；Codex 应该是一个
万法归一的输入框。**

## 它是什么 / 不是什么

| | |
| --- | --- |
| 是 | 一个通用入口：任何项目的零散输入都先收进同一本账（`inbox`） |
| 是 | 一层编排：固定泳道归类 → 项目真相台账 → 串行落盘 → 常驻自跑 |
| 是 | 一个 Codex 插件：skill（触发）+ MCP（工具）+ launchd（常驻） |
| 不是 | 新的模型 / 新的 IDE / 新的多 agent 框架 |
| 不是 | 一个取代 Codex 的产品；它挂在 Codex 原生输入框上 |
| 不是 | 承诺全自动的黑盒：改方向、删数据、发布仍然要你拍板 |

## 怎么装

本仓库既是**模板**（`ops/` 是可复制的编排骨架），也是一个能直接跑的**实例**。
最短路径，三步：

```bash
# 1) 把编排骨架放进你的项目（或直接在本仓库里试）
cp -r ops /path/to/your-project/ops

# 2) 安装插件：把 MCP server 指向那个项目的 ops/，并登记进个人插件市场
./install.sh /path/to/your-project          # 不带参数则指向本仓库自身

# 3) 重启 Codex 桌面端 / CLI，打开 Plugins → Personal → 安装「Codex 自动挡」
```

> `install.sh` 做三件事：把 `plugin/` 装一份到 `~/.agents/plugins/codex-autopilot/`、
> 把其中的 `.mcp.json` 指到 `<项目>/ops/central_mcp.py`（绝对路径）、把插件登记进
> `~/.agents/plugins/marketplace.json`（原文件会备份）。它不改本仓库里的任何文件。
> 换项目 / 换目录时重跑一次。想手工做，用 `./install.sh --no-marketplace` 看提示。

装好之后，**那个普通的 Codex 输入框就是总输入框**。

## 怎么用

你只做一件事：**把感受原样说出来**。分类、路由、排期、追踪由司令台做。

```bash
# 记录一条零散需求（原话即可，不要自己分类）
python3 ops/kgctl.py inbox "左侧导航 rail 排版坏了，图标贴左缘被裁切"

# 归类 + 刷新项目真相 + 给出下一步（一步完成）
python3 ops/kgctl.py tick

# 现在最该做什么 / 项目到哪了
python3 ops/kgctl.py next
python3 ops/kgctl.py status
```

命令速查：

| 命令 | 作用 |
| --- | --- |
| `kgctl inbox "…"` | 把一条零散需求收进唯一入口（追加，不分类） |
| `kgctl tick` | 路由 inbox → 刷新真相快照 → 刷新看板 → 给出下一步 |
| `kgctl next` | 只回答一个问题：现在最该动哪几条 |
| `kgctl status` | 一屏看清：待路由、分支可合/冲突、共享检出脏度 |
| `kgctl queue` / `matrix` | 盘点分支：哪些能干净合、哪些在抢同一个文件 |
| `kgctl land <分支>` | 串行落盘：合并 → 真实构建门禁 → 写进展 → 推 `main` |
| `kgctl reap --go` | 回收已合并的分支（默认只预演） |
| `truthctl ls` / `truthctl next` | 看台账 / 看优先级（同 `kgctl next`） |

比记命令更省事的方式：装了插件后直接对 Codex 说，或让对话调 MCP 工具
`capture_request` / `sync_control_plane` / `next_actions` / `project_status`。

## 常驻 vs 非常驻

- **非常驻（默认）**：有会话时，由 skill / MCP 触发 `tick` / `land`。关掉会话就停。
- **常驻（可选）**：装一个 launchd 守护，没有会话时也按固定节奏推进**安全子集**。

```bash
python3 ops/central_daemon.py --status     # 看守护 / 上一轮 / 队列
python3 ops/central_daemon.py --dry-run    # 只预测本轮会 land 哪些分支
python3 ops/central_daemon.py --once       # 跑一个周期就退出
python3 ops/central_daemon.py --install    # 装成常驻（默认每 15 分钟一轮）
python3 ops/central_daemon.py --uninstall  # 停用并删除守护
```

常驻只自动做两件事：路由唯一入口里的散需求，以及把**与 `main` 无冲突**的分支串行落盘。
每个分支仍走 `kgctl land` 的全部护栏（真实构建、`origin/main` 对齐、失败即丢弃）。
**有冲突的分支一律不动**——那是归属裁决，留给人。通知是去重的：只有「有分支落盘 /
有分支卡住 / 新增需裁决的冲突」时才打扰你。运行目录 `~/.codex-autopilot/`。

## 运行机制

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

- **一本账、一处根**。状态根由 `ops/state_root.py` 统一解析（`KG_REPO` > 主干 worktree >
  主工作树 > 当前 worktree）。不再出现「这个 worktree 说待办 10 条、那个说没有」。
- **只追加，不改历史**。台账是 JSONL，状态变化写新记录；快照和看板都是自动生成物。
- **合并是串行的**。`land` 一次只推一个分支，用 `git merge-tree --write-tree` 在**写之前**
  先模拟合并；冲突就不动，能合才走构建门禁，构建失败就丢弃、`main` 一动不动。
- **永远不动别人的工作区**。推进 `main` 引用用 `git update-ref`，从不 `checkout` / `reset` /
  `stash` 任何人的在途改动。
- **项目相关的旋钮集中在一处**。主干分支名、分支前缀、落盘用的主干 worktree、构建门禁
  命令、泳道定义，全在 `ops/central.config.json`（环境变量可覆盖）；不配就用对
  「单仓 + `frontend` 子目录 + `npm run build`」开箱即用的默认值。

## 复用了哪些轮子（致谢）

这套东西的价值不在「写了多少代码」，而在**没有重造轮子**。它站在这些已有能力上：

- **OpenAI Codex 的原生扩展点（核心复用）** —— 感谢 Codex 团队把这些做成公开可用：
  - **Skill**：`plugin/skills/central-command/SKILL.md` 负责「可发现 + 可触发」——你
    在任意对话里说出零散需求，skill 命中后自动走这套流程。
  - **MCP（stdio）**：`ops/central_mcp.py` 把 CLI 变成对话可直接调用的工具
    （`capture_request` / `sync_control_plane` / `next_actions` …）。
  - **Plugin**：`.codex-plugin/plugin.json` 把 skill 和 MCP 打包成一个可安装项。
  - **AGENTS.md / Automations**：作为「开工前先读契约」和长驻调度的落点。
- **Git** —— 没有它这套编排做不出来：`git worktree` 做任务隔离、`git merge-tree --write-tree`
  在写之前先判冲突、`git update-ref` 推进 `main` 引用而不动任何工作区、
  `git for-each-ref` / `rev-list` 做分支盘点。感谢 Git 社区。
- **macOS launchd** —— 常驻调度用系统自带能力，不引第三方守护框架。
- **Python 标准库** —— `ops/*.py` 全部零第三方依赖，`python3` 就能跑。
- **设计灵感** —— Lauren「poteto」的工程分享（把能靠人拦下的错误变成自动规则、
  「规则从硬到软排名」、「米其林厨房」）。这套东西里「固定泳道 / 门禁前置 /
  只在拍板时打扰你」直接来自她讲的问题。**仅设计借鉴，无代码复用**，特此致谢。

## 能力边界（诚实版）

- 它**不保证**全自动。改产品方向、删数据、发布，仍然要停下来问你，并给出「选 A 会怎样 /
  选 B 会怎样」。
- 常驻守护只做**安全子集**：路由散需求 + land 与 `main` 无冲突的分支。冲突分支不碰。
- 本机 stdio MCP 是面向**本机** Codex 的形态；官方插件市场的 MCP 要求 HTTPS 端点，
  所以这里用的是本机 compat 布局。
- 归类基于关键词 + 文件路径重叠 + 代码标识匹配，**是启发式，不是语义理解**。它可能
  判错泳道；判错不致命（只影响优先级和归属），你可以在 `ops/central.config.json` 里
  覆盖规则，或手工改台账。

## 目录

```text
ops/
  kgctl.py            唯一执行入口：输入归一 + 分支盘点 + 串行落盘
  truthctl.py         项目真相层唯一读写入口：主线/决策/问题/进展/验收
  central_mcp.py      把 CLI 暴露成 MCP 工具（stdio）
  central_daemon.py   常驻执行器（安全子集）
  state_root.py       状态根统一解析（避免多本账）
  central_config.py   项目相关旋钮：主干/前缀/worktree/构建门禁/泳道
  tasks/              inbox.jsonl（唯一入口）· lanes.json（固定泳道）
  truth/              mainline.json · decisions/ledger/progress/acceptance.jsonl
plugin/               Codex 插件：skill + MCP + 清单
install.sh            安装脚本
```

## License

MIT，见 [LICENSE](LICENSE)。
