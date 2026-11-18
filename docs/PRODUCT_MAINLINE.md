# 产品主线（自动综合，勿手改）

> 生成时间 2026-11-18 13:25:06　·　由 `python3 ops/truthctl.py synthesize` 从真相层综合
> 产品方向以 `ops/truth/mainline.json` 为准（人拍板）；这一页是 Agent 从决策/进展/问题/验收里综合出来的现状。

## Codex 自动挡

给 Codex 装一个总输入框：零散需求进一个口，归类、落盘、合并、验证、问题收口由它自己串起来。

- 阶段：内部可用
- 北极星：用户提一次需求就闭环的比例
- 路径：输入归一：零散需求进唯一入口 → 真相沉淀：主线/决策/问题/进展/验收落盘 → 自运行：能合的自动合、能收口的自己收口 → 自追踪：产品主线自动综合
- 当前焦点：问题一次提出就闭环；产品讨论自动长成主线
- 本阶段不做：自建多 agent 框架（编排复用 Codex 会话 / worktree / skill）；改动共享 checkout 里别人的在途工作

## 产品路线（每一步到哪了）

- ✓ 唯一入口收需求：用户只在一个输入框里提零散问题，不用选对话、分支或角色　（已完成）
- ✓ 自动归类并写进项目真相：问题、决策、进展、验收都有稳定台账，跨会话不丢　（已完成）
- ✓ 安全分支自动落盘与冲突收口：能合的自动合，真冲突拆成任务，不再让用户逐条裁决　（已完成）
- ● 自动实现并收口：用户提一次需求后，系统自己实现、验证、落盘、关闭台账　（进行中）
- ● 路线与人机验收可见：面板说清产品路线、当前工作、还差什么，以及哪些需要人验收　（进行中）
- ● 常驻自追踪：没有对话时也持续同步项目主线、决策、进展与卡点　（下一步）

## 现在是什么样

- **唯一入口**：零散需求（视觉/体验/bug/架构/门禁/多分支）说一次即进唯一入口，由系统路由到固定泳道，不需要用户选对话、选分支、选角色。
- **真相沉淀与真实验收**：主线/决策/问题/进展/验收/判断逻辑落进 ops/truth；验收证据必须是真实跑过的命令+cwd+退出码，不允许用结论代替证据。
- **问题闭环**：bug/视觉/体验/架构进台账后由 agent 推进：修复 -> 验证 -> close 并追加进展；回归用 reopen 重开。
- **产品主线综合**：每轮收口跑 synthesize，把决策/进展/问题/验收/产品状态综合成 docs/PRODUCT_MAINLINE.md；人在 mainline.json 只拍板方向。
- **合并编排**：每个任务独立 worktree；合并前用 git merge-tree 预演，门禁通过且无冲突时自动 land 并同步 main 引用，不把要不要合推回用户。
- **常驻守护与面板**：面板显示运行状态、当前处理的分支合并逻辑、开关与必要信息；支持暂停/自动合入/自动拆冲突/常驻。
- **视觉/设计方案提示词**：做视觉/设计前先当产品经理+产品设计师，尊重用户、保护注意力：先截图看现状再改，改完再截图对比，只允许正向改动，不能更丑、更乱、更难读。
- **两平面架构**：用户仍从唯一输入框提需求；项目真相面负责预期、验收和是否值得打扰人，执行面负责路由、实现、合并与 harness 状态；全局健康只读聚合事实，不派活、不改配置、不替任何平面做决定。
- **PM 管家**：PM 管家只读聚合待确认预期、已生效约束、主线阶段、让路工作项和追问候选；文件占地重复时让新项排队且保留未知，不自动发追问，不替用户决定方向、删数据或发布。
- **验收管家**：人工验收项先由机器跑真实验证，只有带可复核命令、cwd、退出码的项才进入“需要你验收”；用户点击后才会调系统截图器补复核附件，补图只追加附件，不替用户收口。
- **全局项目健康**：全局健康从真相层、执行层和验收层已有事实聚合为 attention/unknown/off/ok 四态；取不到就说取不到，正常排队不报警，不新建分数台账，也不自动触发修复。
- **关系层**：跨项目长期目标通过 truthctl goal 显式登记，支持 summary/list/create/update/link/close；可关联工作项、验收项、来源窗口和收口证据，并在高级面板折叠区展示。聊天里的一句话不会自动升成生效目标；账本取不到显示 unknown。
- **守护首次安装**：install.sh 默认不替用户启用常驻；只有 --install-daemon 才首次安装守护。central_daemon.py --install 在 bootstrap 后回读 launchd，只有 running 且有 pid 才返回成功，否则不写常驻开启。
- **快照清理兼容**：snapshot_prune.py 使用 typing.Optional/List/Dict/Tuple，守护脚本固定用 /usr/bin/python3 3.9.6 执行；test-snapshot-clone-prune.py 每次门禁用 /usr/bin/python3 -m py_compile 回编译，旧版 Path | None 语法回归会直接红灯。

## 决策脉络

- 自动挡架构收敛为两平面七管家与一层全局健康 —— 继续横向加管家会把职责变成角色堆叠，无法说清谁拥有事实、谁执行、谁能改状态。两平面先分真相与执行，再以四层契约处理关系、规划、执行、运行，能保留万法归一入口，同时把写权限和人工介入边界写硬。

## 最近在推进

- [在途] 确认修复已在 origin/main：snapshot_prune.py 使用 typing.Optional/List/Dict/Tuple，不再依赖 Python 3.10 联合类型语法；系统 Python 3.9.6 真导入与快照清理门禁均通过，并补上旧 Path | None 语法的阴性对照，证明门禁能抓回原故障。
- [已落盘] @ b6e94f44f1 补记原 P-0079（被本轮真相镜像覆盖）：codex/daemon-first-install-20261118 验证门禁通过，已落盘 main @ b6e94f44f1；改动 3 个文件：install.sh（+24/-5）、ops/central_daemon.py（+26/-1）、ops/selftest.py（+115/-0）｜3 files changed, 165 insertions(+), 6 deletions(-)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0
- [已落盘] @ a8d2a698ad codex/truth-restore-land-record-20261118 验证门禁通过，已落盘 main @ a8d2a698ad；改动：本次合并未带进新内容（分支内容已在主干，属空合并）｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0
- [已落盘] @ d367c509e8 codex/jsonl-truth-no-overwrite-20261118 验证门禁通过，已落盘 main @ d367c509e8；改动 3 个文件：ops/kgctl.py（+52/-9）、ops/selftest.py（+1/-0）、ops/test-mirror-truth-jsonl-union.py（+106/-0）｜3 files changed, 159 insertions(+), 9 deletions(-)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0
- [已落盘] @ 5caaabb7 已修复 JSONL 真相镜像覆盖：_mirror_truth_into_trunk 对 ops/truth 与 ops/tasks 下的 JSONL 做行级并集，同 id 不同 ts 按时间排序保留最新记录；新增 test-mirror-truth-jsonl-union 门禁并登记 selftest。提交 5caaabb7 已合入 origin/main，land 的全量 selftest/panel-logic/panel-render 门禁通过，P-0084/A-0073 已记录落盘验收。状态根解析陈旧导致的相邻问题继续由 L-0003 跟踪。
- [已落盘] @ 138a8b7662 codex/state-root-freshness-20261118 验证门禁通过，已落盘 main @ 138a8b7662；改动 3 个文件：ops/selftest.py（+1/-0）、ops/state_root.py（+42/-1）、ops/test-state-root-freshness.py（+139/-0）｜3 files changed, 182 insertions(+), 1 deletion(-)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0
- [已落盘] @ 1358b850 已修复默认状态根选择：resolve_state_root 保留 KG_REPO 最高优先级；无显式覆盖时按可用 ops/central.config.json 优先、同条件按 HEAD commit 时间取最新，配置损坏按不可用处理，避免停在旧分支的共享检出压过活的主干。新增 test-state-root-freshness 门禁并在旧逻辑上复现 3 条失败，修复后 4 条全绿；提交 1358b850 已随 codex/state-root-freshness-20261118 落盘 origin/main，全量 selftest、panel-logic、panel-render 均通过，P-0086/A-0074 已记录验收。
- [已落盘] @ 8cedc648 发布一致性判据5已补远端主干lineage门禁：install-state.source_commit 必须同时是本地主干和 refs/remotes/origin/main 的祖先；新增阴性对照证明只在本地HEAD可见必须红，正常发布不误红。门禁 exit 0，落盘 commit 8cedc648。
- [已落盘] @ 2aee420d3f codex/truth-sync-union-20261118 验证门禁通过，已落盘 main @ 2aee420d3f；改动 2 个文件：ops/kgctl.py（+22/-0）、ops/test-mirror-truth-jsonl-union.py（+58/-0）｜2 files changed, 80 insertions(+)｜门禁：selftest exit 0；panel-logic exit 0；panel-render exit 0｜对应验收项：A-0007
- [已落盘] @ b07f8317 已修复 sync_trunk_truth 暂存前 JSONL 与 HEAD 行级并集，新增 test-mirror-truth-jsonl-union 阴性对照；提交 d61f0194 已随 b07f8317 落盘 origin/main，插件运行时已同步同一哈希 4782a6eb；本提交恢复被旧状态根覆盖的 JSONL 历史。

## 关键验收证据（核对用，不替代 mainline.json）

- `A-0002` [P1] codex/pm-truth-closure-20261118 门禁预跑（未落盘）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0003` [P1] codex/pm-truth-closure-20261118 门禁预跑（未落盘）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0004` [P1] codex/truth-close-pm-truth-closure-20261118 门禁预跑（未落盘）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0005` [P1] codex/truth-close-pm-truth-closure-20261118 门禁预跑（未落盘）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0071` [P1] 补记 codex/daemon-first-install-20261118 落盘验收（原 A-0006 被覆盖）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0072` [P1] codex/truth-restore-land-record-20261118 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0073` [P1] codex/jsonl-truth-no-overwrite-20261118 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0074` [P1] codex/state-root-freshness-20261118 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0006` [P1] codex/release-lineage-gate-20261118 门禁预跑（未落盘）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0007` [P1] codex/truth-sync-union-20261118 门禁预跑（未落盘）
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0
- `A-0008` [P1] codex/truth-sync-union-20261118 落盘验收
  - 证据：selftest: python3 ops/selftest.py (cwd=.) -> exit 0

## 还差什么

**gate（3 待处理）**
- `L-0001` 磁盘曾达 99%（可用 1.6GB）。/tmp 有 228 个 autopilot 临时目录约 3.87GB，其中 kg-gate-* 由 kgctl gate
- `L-0007` 【launchd 陈旧项·非 Stop 复发链】freshness 门禁真机实测 4 条 stale：ai.danta.codex-proxy-watchdog
- `L-0008` 【既存·门禁环境】在部署目录 ~/plugins/codex-autopilot/releases/0.6.0-preview.82 里直接跑 ops/self

## 需要你验收

- （当前没有需要你人工拍板的验收项）

## 已机器验收

- `A-0002` [P1] codex/pm-truth-closure-20261118 门禁预跑（未落盘）
- `A-0003` [P1] codex/pm-truth-closure-20261118 门禁预跑（未落盘）
- `A-0004` [P1] codex/truth-close-pm-truth-closure-20261118 门禁预跑（未落盘）
- `A-0005` [P1] codex/truth-close-pm-truth-closure-20261118 门禁预跑（未落盘）
- `A-0006` [P1] codex/release-lineage-gate-20261118 门禁预跑（未落盘）
- `A-0007` [P1] codex/truth-sync-union-20261118 门禁预跑（未落盘）
- `A-0008` [P1] codex/truth-sync-union-20261118 落盘验收
- `A-0071` [P1] 补记 codex/daemon-first-install-20261118 落盘验收（原 A-0006 被覆盖）
- `A-0072` [P1] codex/truth-restore-land-record-20261118 落盘验收
- `A-0073` [P1] codex/jsonl-truth-no-overwrite-20261118 落盘验收
- `A-0074` [P1] codex/state-root-freshness-20261118 落盘验收

## 进度概览

- 累计关闭问题：7 条
- 累计落盘：10 条
- 未决问题：3 条
- 已验收：11 条

