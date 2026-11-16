# 开源对标调研：多 Agent 编排与「自动挡」

调研时间：2026-11-18
方法：公开检索（来源见文末）。**未逐个 clone 仓库逐行验证**，因此下面凡是从
检索摘要得到的结论都标「检索结论」；凡是本机实测的标「本机实测」。两者不要混读。

---

## 一句话结论

「多 harness 统一入口 + 任务路由 + 并行 worker + 实时面板」这一层，开源社区在
2026 年已经非常拥挤；DeepSeek Harness 的 Super-agent 原型与我们的**路由管家
（万法归一）几乎是同一套设计**。我们真正的差异化只剩三件事：

1. **落盘/合流的防丢失**（auto_land + 门禁 + 真相层并集）；
2. **约束真的进机器门禁**（不是一句写给人看的规范）；
3. **首屏三问的面板**（说得清「要不要我、在干对的事吗、替我做成了什么」）。

其余部分应该复用开源，不该自造。

---

## 1. DeepSeek Harness Super-agent：直接撞车

检索结论（多源一致）：

- 项目名 `dsh-superagent-prototype`，MIT，2026-10-31 公开，**自称研究原型，不是
  官方产品**；面向**单个** GitHub 仓库，可选接飞书。
- 架构是 **Router + Worker 双层**：Router 是常驻全局协调节点，统一接收多渠道
  事件（群消息、GitHub PR/issue），负责把活路由给 worker；Worker 隔离干活，
  写代码、跑测试、开 PR。
- 提出四个特征：**连续、高效、主动、持久**。
- **默认是「观察模式」**：只生成修复思路和补丁方案，标记为待接受，**不自动改
  代码**；确认稳定后才切到执行模式，自动提交 PR。

与我们的耦合与撞车：

| 我们的提法 | Super-agent 对应物 | 判定 |
|---|---|---|
| 路由管家（万法归一入口） | Router + Worker 双层 | **正面撞车**，概念完全重合 |
| 实现管家（何时该动手） | Worker（写码/跑测/开 PR） | 撞车 |
| 「不做破坏性修改」 | 默认观察模式、先方案后执行 | 同一种安全直觉 |
| 多入口混一个窗口也能定位 | 跨渠道事件统一收件 | 撞车 |
| 合并管家的防丢失落盘 | 无对等物（它只管开 PR，合不合是人的事） | **我们更前** |
| 约束进机器门禁 | 无对等物 | **我们更前** |

结论：**「入口归一 + 路由 + worker 实现」这块不要再自造**。我们该把力气放在
它没做的那两件事上：落盘防丢失、约束可验证。

---

## 2. 开源同类盘点（并行多 agent 编排）

检索结论，按许可证与定位：

| 项目 | 许可证 | 定位 | 与我们的关系 |
|---|---|---|---|
| Orca | MIT | 终端优先，同时协调 10~100 个 agent | 编排层撞车 |
| Paseo | Apache-2.0 | 自托管控制面，手机/桌面/网页监督 | 面板层撞车 |
| Emdash | Apache-2.0 | 25+ agent，各自独立 worktree，导入 Linear/GitHub/Jira | worktree 隔离撞车 |
| Synara | MIT | 并行 agent | 编排层撞车 |
| Superset | Elastic License 2.0 | macOS 多 agent（**非 MIT**，注意） | 许可证不兼容 |
| brat | MIT | append-only 事件日志、任务拆分、可配合并策略、Web 面板 | **合并策略最接近我们** |
| Claude Code Agentrooms | 开源 | @mention 路由 + 多 agent 工作区 | 路由撞车 |
| OpenCastle | 非商用免费 | Team Lead 只协调不写码 + 专家 agent | 撞车 |
| OpenCrew | MIT | Captain 协调、每个 agent 一个真实会话 | 撞车 |
| Agetor | 开源 | Kanban 控制面 + worktree 隔离（v0.0.1，早期） | 面板层撞车 |
| wshobson/agents | 开源 | Claude Code 多 agent 编排框架 | 撞车 |

（star 数在检索结果里出入很大，例如同一项目出现「887 星」与「3.5 万星」两种说法，
不采信具体数字。）

结论：**编排、worktree 隔离、看板面板这三层已经被开源吃干净了**；其中 `brat`
在「可配合并策略 + append-only 状态」上与我们最像，值得读它的实现而不是重写。

---

## 3. 我们真正值得自造的部分

1. **落盘/合流的最优路径判定**：把「防丢失、防覆盖、防破坏性修改」做成落盘前的
   硬门禁，而不是靠人盯。开源里没有一个把这件事当成核心卖点。
2. **约束进机器门禁**：用户说过一次的预期，直接变成落盘那一刻真跑的断言；写不出
   命令就老实标「无门禁」，不假装。这块目前没看到对等实现。
3. **首屏三问面板**：只回答「要不要我 / 在干对的事吗 / 替我做成了什么」。多数开源
   面板是「任务列表 + 日志流」，信息更全但注意力更散。

## 4. 面板形态（原生耦合还是独立）

本机实测：

- 面板是 **MCP UI resource**（`ui://autopilot/panel-v2.html`），由 Codex 宿主在
  iframe 里渲染，通过 `postMessage` 走 `tools/call`。也就是说：**它耦合在 Codex
  界面里，但走的是官方 UI 扩展点**，不是另起一个 Electron 应用。
- 另有 macOS 菜单栏 Companion，只是状态锚点（状态图标、总开关、当前任务、卡点），
  不承担路线管理和工程追踪。

这个形态是对的，不建议改。要补的是数据，不是外形。

---

## 来源

DeepSeek Super-agent：
- 「Super-agent(DeepSeek Harness)」https://wx.secv.cc/archives/82556
- 「Super Agent: What It Is And How It Works」https://1min.ai/deepseek-super-agent
- 「开源一个超级智能体，先只看不动手」https://m.sohu.com/coo/zmtq/1098345143_122944309
- 「DeepSeek Harness 上线多 Agent 组队」https://news.qq.com/rain/a/20261112A0BNSL00

开源同类：
- 「7 Best Open Source Conductor Alternatives in 2026」https://www.ossalternatives.org/alternatives-to/conductor
- 「4 Open-Source Orchestrators for Running AI Coding Agents in Parallel」https://revesery.com/@alex/4-open-source-orchestrators-for-running-ai-coding-agents-in-parallel-mtskf6wu
- 「Running AI Coding Agents in Parallel」(brat) https://www.neullabs.com/blog/multi-agent-orchestration-with-brat
- 「OpenCastle」https://www.opencastle.dev
- 「OpenCrew」https://techbytes.app/posts/opencrew-open-source-multi-agent-orchestration-on-top-of-claude-code
- 「Agetor」https://theagenttimes.com/agents/article/agetor-offers-agents-a-kanban-control-plane-built-for-parall-62538dae
