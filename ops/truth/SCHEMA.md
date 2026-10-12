# 项目真相（Project Truth）

> 这份目录回答一个系统性问题：**这个项目现在到底是什么状态，谁定的，做到哪了，还差什么，需要人拍板什么。**
>
> 它不是又一份规范，也不是又一份会议纪要。它是**唯一事实来源**（single source of truth），
> 且全部是机器可读 + 人可读 + 可 diff 的文件。任何会话、任何 agent、任何人在动手前先读它，
> 收工时按同一套格式写回它。`ops/truthctl.py` 负责读写与生成一屏快照，统一入口是 `python3 ops/kgctl.py tick`。

## 为什么要它（一句话）

散落在 40+ 个对话、70+ 个分支里的「产品主线 / 决策 / bug 台账 / 进展 / 验收」，
只要不落到这里，就会随对话结束而消失；一旦落到这里，它就**跨会话存活**，
路由、合并、升级用户决策才有共同依据。

## 目录

```text
ops/truth/
  mainline.json      # 产品主线：阶段 / 北极星 / 当前焦点 / 明确不做（单对象，人工维护）
  decisions.jsonl    # 决策台账：做过哪些决定、为什么、备选是什么（追加式）
  ledger.jsonl       # 问题台账：bug / 视觉 / 体验 / 架构问题（追加式，带状态与泳道）
  progress.jsonl     # 进展台账：每条泳道最近干了什么、是否落盘（追加式）
  acceptance.jsonl   # 验收台账：实现模块 / 验收项 / 状态 / 证据（追加式）
```

## 通用字段（JSONL 每条）

| 字段 | 含义 | 必填 |
|---|---|---|
| `id` | 类型前缀 + 4 位序号，如 `D-0001` / `L-0003` / `P-0002` / `A-0001` | 是（自动生成） |
| `ts` | 写入时间 `YYYY-MM-DD HH:MM:SS` | 是（自动生成） |
| `type` | `decision` / `ledger` / `progress` / `acceptance` | 是 |
| `title` | 一句话说清这条是什么 | 是 |
| `status` | 见各类型约定 | 是 |
| `lane` | 归属泳道（分支名或 PR），`null` 表示未归属 | 否 |
| `files` | 相关文件路径数组 | 否 |
| `source` | 来源：`user` / `agent` / 会话或 issue 标识 | 否 |

### decisions.jsonl

额外字段：`decision`（结论）、`why`（理由）、`alternatives`（被否的备选）。
`status` ∈ `active` | `superseded`。

### ledger.jsonl

额外字段：`kind` ∈ `visual` | `ux` | `bug` | `arch`、`detail`（复现/根因/证据）。
`status` ∈ `open` | `doing` | `done` | `wontfix`。

### progress.jsonl

额外字段：`summary`、`commit`（落盘后的 sha）、`landed`（bool）。

### acceptance.jsonl

额外字段：`level` ∈ `P0` | `P1` | `P2`、`evidence`（可执行的验收证据/命令）。
`status` ∈ `todo` | `wip` | `verified` | `blocked`。

## 与 inbox / 泳道 / 合并队列的关系

```text
用户零散输入 ──> ops/tasks/inbox.jsonl（原始入口，不分类、不丢）
                      │  truthctl route --go
                      v
                 ops/truth/ledger.jsonl（已归类、带状态与泳道）
                      │  kgctl route / kgctl queue
                      v
              泳道（分支 ≡ PR）──> kgctl land ──> main
                      │
                      └──> ops/truth/progress.jsonl（落盘即写进展）
```

## 规矩

1. **只追加，不改历史**：JSONL 只 append。状态变化写新记录，`id` 不变。
2. **不猜**：不确定的字段留空或 `null`，不要用推测填空。
3. **一屏可读**：`python3 ops/kgctl.py truth` 生成的 `docs/PROJECT_STATUS.md` 是给人看的入口。
4. **主线是手写的**：`mainline.json` 由人维护（改方向属真决策），其余台账由会话写。
