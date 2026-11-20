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
  mainline.json      # 产品主线：阶段 / 北极星 / 当前焦点 / 产品路线 / 明确不做（单对象，人工维护）
  product_state.json # 产品状态：产品今天实际是怎么工作的（按模块，随代码同步）
  decisions.jsonl    # 决策台账：做过哪些决定、为什么、备选是什么（追加式）
  judgments.jsonl    # 判断逻辑台账：人为什么这么判断（每轮蒸馏回写，追加式）
  ledger.jsonl       # 问题台账：bug / 视觉 / 体验 / 架构问题（追加式，带状态与泳道）
  progress.jsonl     # 进展台账：每条泳道最近干了什么、是否落盘（追加式）
  acceptance.jsonl   # 验收台账：实现模块 / 验收项 / 状态 / 证据 / 人工复核步骤（追加式）

生成物：
  docs/PRODUCT_MAINLINE.md  # 由 truthctl synthesize 从以上台账综合出的产品主线
  docs/PROJECT_STATUS.md    # 由 truthctl snapshot 生成的一屏项目状态
```

## 通用字段（JSONL 每条）

| 字段 | 含义 | 必填 |
|---|---|---|
| `id` | 类型前缀 + 4 位序号，如 `D-0001` / `L-0003` / `P-0002` / `A-0001` / `J-0001` | 是（自动生成） |
| `ts` | 写入时间 `YYYY-MM-DD HH:MM:SS` | 是（自动生成） |
| `type` | `decision` / `judgment` / `ledger` / `progress` / `acceptance` | 是 |
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

### judgments.jsonl

额外字段：`statement`（判断逻辑本身）、`why`（为什么这么判断）、`scope`（适用范围）。
`status` ∈ `active` | `retired`。与 ledger 的区别：ledger 记「这个项目哪里有问题」，
judgment 记「以后遇到同类情况该怎么判断」——记的是判断逻辑，不是一次性细节，
这样下一次换一个地方也不用重新讲一遍。

### product_state.json（单对象，非追加）

它不是台账，而是「产品今天实际怎么工作」的一段可 diff 的事实。
结构：`{"updated": "YYYY-MM-DD", "modules": [{"area": "模块名", "behavior": "现在的行为",
"why": "为什么", "files": ["..."], "updated": "YYYY-MM-DD"}]}`。
改产品行为的会话应在同一轮把它更新进来，人验收时只看它的变化，不读实现。

### acceptance.jsonl

额外字段：`level` ∈ `P0` | `P1` | `P2`、`needs_human`（bool）、`steps`（人工复核步骤）、
`evidence`（可复核的验收证据）。
`status` ∈ `todo` | `wip` | `verified` | `blocked`。

**验收证据必须可复核**：`kgctl land` 落盘时会把每一道验证门禁的
`name: cmd (cwd=...) -> exit N` 写进 `evidence`，不允许写「通过构建」这类泛化文案。
没有配置验证门禁时，状态写 `todo` 并注明「未配置验证门禁」，不伪造 `verified`。
也就是说：`verified` 一定对应真实跑过的命令和退出码，而不是一句结论。

**人和机器不能互相冒充**：`needs_human=true` 表示审美、手感、真实产品里点一下这类
必须由人拍板的验收项；`steps` 写清楚人要怎么复核，面板会原样展示。机器能自证的项目
保持 `needs_human=false`，不得把机器跑过命令当成人工验收，也不得把人工验收写成
`verified` 直到人真的确认。`verified` 只表示对应类型的验收真的完成。

## 产品路线

`mainline.json` 的 `roadmap` 是产品预期的实现路线，不是任务清单。每一项写成：

```json
{"step": "唯一入口收需求", "expected": "用户只在一个输入框里提需求", "status": "done"}
```

`status` ∈ `done` | `doing` | `next` | `todo`。路线由人拍板；面板和
`truthctl synthesize` 只读它，并把「每一步到哪了」显示出来。路线下的具体工作、
未决问题和验收证据分别落在 `ledger.jsonl` / `progress.jsonl` / `acceptance.jsonl`，
不要把这些细节塞回 `roadmap`。

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
                      ├──> ops/truth/progress.jsonl（落盘即写进展）
                      └──> ops/truth/acceptance.jsonl（落盘即写真实验收证据）

每轮收口后：回看人这轮额外说了什么 ──> ops/truth/judgments.jsonl（蒸馏判断逻辑）
```

## 规矩

1. **只追加，不改历史**：JSONL 只 append。状态变化写新记录，`id` 不变。
2. **不猜**：不确定的字段留空或 `null`，不要用推测填空。
3. **一屏可读**：`python3 ops/kgctl.py truth` 生成的 `docs/PROJECT_STATUS.md` 是给人看的入口。
4. **主线是手写的**：`mainline.json` 由人维护（改方向属真决策），其余台账由会话写。
5. **验证证据不能编**：`acceptance.jsonl` 里的 `verified` 必须对应真实跑过的命令和退出码；
   没有验证门禁就写 `todo` 并说明，不允许用「通过构建」这类结论代替证据。
6. **每轮蒸馏**：收口后回看这轮人额外说了什么，把「为什么这么判断」写进 `judgments.jsonl`——
   只记判断逻辑，不记一次性细节，这样下次换个地方也不用重新讲一遍。
7. **问题一次提出就闭环**：bug/视觉/体验/架构进 `ledger.jsonl` 后，由 agent 推进修复 ->
   验证 -> `truthctl close <L-id> "<结论>"`；关闭要带 commit/证据并自动追加进展。回归了
   用 `truthctl open` 重开，不允许修完了还挂在 `open`，也不允许没验证就标 `done`。
8. **主线自动综合**：每轮收口跑 `truthctl synthesize`，把决策/进展/问题/验收/产品状态
   综合成 `docs/PRODUCT_MAINLINE.md`。人在 `mainline.json` 只拍板方向；零散讨论长成的主线
   由这份生成物承接，人只读这一页。
