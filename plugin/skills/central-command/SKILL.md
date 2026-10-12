---
name: central-command
description: 中央司令台（Codex 自动挡）。当用户在同一项目里提出零散、即时、难以归类的需求（视觉问题、体验问题、bug、架构项、门禁红灯、多分支要不要合、该在哪个对话/分支继续），或问“现在最该做什么/项目到哪了/还有什么没做”时使用。把输入收进唯一入口、归到固定泳道、写进项目真相，并给出下一步；不要要求用户自己选对话、选分支、选角色。
---

# 中央司令台（Codex Autopilot）

## 一句话
用户只负责把模糊、零散、即时的感受丢进同一个输入框；归类、路由、排期、追踪、收口由你完成。

## 触发信号
- 用户对着界面说“这里不对 / 视觉坏了 / 体验别扭”，没有说进哪个分支。
- 用户报一个 bug 或门禁红灯，但没有指定泳道。
- 用户问“这个分支要不要合 / 等谁先合 / 现在到哪了 / 下一步做什么”。
- 用户一次抛出多条彼此无关的小需求。

## 硬规则
1. 绝不让用户选择：不改问“放哪个对话/开哪个分支/派给哪个角色”。
2. 不造分支海：固定泳道在 `ops/tasks/lanes.json`（默认 `design` / `bug` / `arch` / `gate`），
   一个 intake 只进一条；可用 `ops/central.config.json` 的 `lanes` 覆盖。
3. 先记账再动手：任何零散需求先 `inbox`，再 `tick`，再执行。
4. 只读 + 追加：归类、快照、看板都是加法；合并前必过门禁；不进共享 checkout 写文件。
5. 用户拍板只限三种：改产品方向、删数据、发布。其余自行路由执行。

## 两种调用方式
- 如果这个插件暴露了 MCP 工具，优先直接调用：`capture_request` / `sync_control_plane` /
  `next_actions` / `project_status` / `route_inbox` / `suggest_branch`。它们读写的就是下面这套 CLI。
- 否则走 CLI（状态根由 `ops/state_root.py` 统一解析，从任何 worktree 调都读同一份账本）：

```bash
# 1) 记录（用户原话即可，不要重写、不要分类）
KG_INBOX_KIND=visual KG_INBOX_FILES=src/index.css \
  python3 ops/kgctl.py inbox "左侧导航 rail 排版坏了，图标贴左缘被裁切"

# 2) 归类 + 刷新状态 + 给出下一步（一步完成）
python3 ops/kgctl.py tick

# 3) 看项目真相 / 当前待办
sed -n 1,80p docs/PROJECT_STATUS.md
python3 ops/kgctl.py next
```

## 执行路径
- `tick` 之后，按 `next` 给出的优先级挑一条，在独立 worktree 里做；不碰别人在途文件。
- 改完提交到该泳道分支；能干净合并、相关门禁通过就直接 `kgctl land <分支>`，不要把“能不能合”当问题回报给用户。
- 落盘后把进展写进真相层：`truthctl add progress "..."`（`KG_TRUTH_COMMIT=<sha>`、`KG_TRUTH_LANDED=1`）。
- 问题修完写一条状态变更：`truthctl add ledger` 或直接更新对应条目的 `status=done`。

## 边界
- 这个 skill 管“怎么把散乱输入收成一个有序系统”，不代替产品判断。
- 涉及产品方向、删数据、发布，必须停下来问用户，并给出“选 A / 选 B”的具体后果。
- 不重复造多 agent 框架：编排用现有 Codex 会话/worktree/skill；这里只做输入归一、真相沉淀、合并串行。
