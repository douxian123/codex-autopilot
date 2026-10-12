# Codex Autopilot · 插件

这是「一个复杂项目，只留一个总输入框」的产品化外壳。它把零散、即时、难以归类的
需求（视觉、体验、bug、架构、门禁红灯、多分支要不要合、下一步做什么）收进唯一入口，
由控制面统一归类、路由、追踪，并给出当前最该动的工作。

它**不是**新的 Codex，也**不改** Codex 二进制；它是装在 Codex 原生入口上的控制面。
完整说明见仓库根目录的 [`README.md`](../README.md)。

## 这个插件的入口在哪里

| 你在哪 | 怎么进 |
| --- | --- |
| ChatGPT 桌面版 / Codex | 打开 **Plugins** 标签，在 **Personal** 里找到「Codex 自动挡」，安装 |
| Codex CLI | 输入 `/plugins`，装上后**新开一个会话** |
| 任意对话内 | 直接说需求（skill 的 description 命中即触发），或输入 `$central-command` |
| 需要工具时 | 插件暴露 MCP 工具：`intake_request`（一句话走完整条链，首选）/ `sync_control_plane` / `next_actions` / `project_status` / `route_inbox` / `convergence_plan` / `generate_context` / `dashboard` / `capture_request` / `suggest_branch` |

装好之后，**那个普通的 Codex 输入框就是总输入框**：你不需要先想“这该发哪个对话、
开哪个分支、派给哪个角色”。把感受原样说出来，控制面负责把它变成有序工作。

## 组成

- `skills/central-command/` —— 行为层：怎么收输入、怎么触发、边界在哪。
- `ops/` —— 运行层：`install.sh` 从仓库复制进来的纯 Python 标准库代码
  （`kgctl.py` / `truthctl.py` / `central_mcp.py` / `central_daemon.py`）。它跟着插件走，
  项目里那份旧代码不会被调用。
- `.mcp.json` —— 工具层：把 `./ops/central_mcp.py` 这个 stdio MCP server 挂进来，
  并声明 `KG_REPO`（账本所在项目）。安装脚本会写好这两项。
- `.codex-plugin/plugin.json` —— 清单：声明上面几部分属于同一个插件。

## 安装

在本仓库根目录跑一次：

```bash
./install.sh
```

它会：把 `plugin/` **连同一份运行时代码**装到 `~/plugins/codex-autopilot/`；在目标项目里
只补缺失的账本文件（`ops/tasks/`、`ops/truth/`、`docs/`，已存在的一律不动）；把 `.mcp.json`
写成「运行时代码 = 插件自己的 `./ops/central_mcp.py`，状态根 = `KG_REPO=<目标项目>`」；
并把本插件登记进个人 marketplace（`~/.agents/plugins/marketplace.json`，原文件备份为
`.bak-codex-autopilot`）。它不改本仓库里的任何文件。

> 为什么是 `~/plugins/`：Codex 的 personal marketplace 以 `$HOME` 为根，清单在
> `$HOME/.agents/plugins/marketplace.json`，清单里的 `./plugins/<name>` 相对根解析。
> 插件实体若放错到 `~/.agents/plugins/<name>`，会被 marketplace 列出、却 `add` 不进去。

换目录 / 换机器时重跑一次即可。不想自动改 marketplace，用 `./install.sh --no-marketplace`。

## 能力边界（诚实版）

- **skill 负责“可发现 + 可触发”**：装了插件，入口就出现在 Plugins 页；对话里也会自动命中。
- **MCP 负责“可被调用”**：把 CLI 变成对话可直接调用的工具，减少记命令。
- **真正跑起来的是插件自带的 `ops/kgctl.py` + `ops/truthctl.py`**：状态根由 `ops/state_root.py`
  统一解析（`KG_REPO` > 主干 worktree > 主工作树 > 当前 worktree），保证全项目只有一份账本。
  项目里有没有 `ops/` 运行时代码都不影响；插件只把 `KG_REPO` 指向项目。
- 本机 stdio MCP **不是官方可移植形态**：官方插件市场的 MCP 要求 HTTPS 端点。本插件面向
  本机的 Codex（桌面 / CLI），用 compat 布局挂本机 stdio server。
- **长期常驻调度**（关掉对话也自己跑）由 `ops/central_daemon.py` 承担。它只做安全子集：
  路由 inbox + 自动 land 与 main 无冲突、且树里没有残留冲突标记的分支 + 把冲突分支
  收拢成族（`converge`）。冲突分支不会自动合；未解干净的 convergence 分支也不会被
  `queue` 列为可落盘，而是留在 convergence worktree 里交给 agent 逐族解。
