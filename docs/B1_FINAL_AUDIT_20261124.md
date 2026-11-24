# B1 最终闭环核验（2026-11-24）

## 1. 结论与口径

本轮闭环的对象是已由用户选定的 **B1：本地主链小范围试用**。权威决策是
`ops/truth/decisions.jsonl` 的 `D-0005`：公开入口保持 `0.6.0-preview.140`，
不发布 `.146`、不做 live 换代、不切 provider/login；原生派活与跨设备留给 B2。

因此这里的“闭环”指：工程门禁、真相台账、公开 beta 入口文档、本机加载和多管家
责任视图已经形成可复核闭环。它不等于完整愿景已全部打穿，也不等于人工视觉/体验
项已经有人签收。

## 2. 全量 PRD 的实际形态

仓库里没有单一 `PRD.md`。全量 PRD 是以下契约包，缺一项就会把产品说小：

| 文件 | 在 PRD 里的角色 |
|---|---|
| `docs/PRODUCT_CONTRACT.md` | 产品定义、用户结果、最终架构、七类责任视图、PM 职责、执行标准、完成口径 |
| `docs/ARCHITECTURE_STEWARDS.md` | 一入口、两平面、四层运行契约与多管家架构推导 |
| `docs/PANEL_INTERACTION_DESIGN.md` | 面板首屏注意力顺序、信息层级与交互 |
| `docs/DESIGN.md` | 视觉与交互契约 |
| `docs/PRODUCT_VALUE.md` | 用户价值与机器/人工验收边界 |
| `docs/BETA_LOCAL_TRIAL.md` | B1 公开入口、安装、使用、API key 与订阅差异、已知边界 |
| `docs/experience/experience-contract.json`、`docs/experience/panel-renderer-contract.json` | 体验与渲染机器门禁 |
| `docs/steward_contract.json`、`ops/steward_contract.py` | 管家状态上屏契约 |
| `ops/truth/constraints.jsonl`、`ops/truth/decisions.jsonl` | 用户预期、返修意见、决策的生效真相 |
| `docs/PRODUCT_MAINLINE.md`、`docs/PROJECT_STATUS.md` | 从真相层综合出的当前产品状态 |

PRD 持续迭代由上下文管家和 PM 管家共同承担：前者收录候选，后者决定
adopt/adapt/defer/reject/ask_user；生效前必须先改 `docs/PRODUCT_CONTRACT.md`，
由 `ops/test-prd-evolution.py` 门禁检查。

## 3. 多管家架构怎么落到实现

“管家”是责任视图，不是七个常驻 Agent，也不是八个互相开会的模型。运行时仍由
Codex 会话、worktree、skill、MCP、daemon 和台账协作完成。

| 责任视图 | 当前实现落点 | B1 状态 |
|---|---|---|
| 上下文管家 | `capture_request`、`constraint_candidates_list/promote/dismiss`、`context_archive`、`truthctl constraints` | 生效；候选与驳回分离，不自动改写硬约束 |
| 路由管家 | `route_preview`、`route_inbox`、`routing_review_list/assign`、固定泳道与全局待路由箱 | 生效；判不清留全局箱，不猜项目 |
| PM 管家 | `pm_research_*`、`pm_product_decide`、`pm_execution_gate`、`product_state/update`、`distill_*`、PRD 迭代门禁 | 生效；持续质量责任与停止权已写成契约和门禁 |
| 实现管家 | `auto_exec` / 项目自主运行、`exec_lifecycle`、`worker_lifecycle`、隔离 worktree | 机制生效但默认关闭；面板显示 `exec off` |
| 合并管家 | `kgctl gate/land`、`resolve_*`、`convergence_plan`、append-only 真相层 | 生效；没过门禁不合并，冲突拆单任务 |
| 验收管家 | `register_acceptance`、`resolve_acceptance`、`capture_acceptance_screenshot`、人工验收队列 | 生效；机器自证与人判分离，代理不代签 |
| 状态管家 | `harness_tune_list/apply/rollback`、`route_health`、`project_health`、`outcome_score` | 生效；只读发现与受授权调整，不自动改方向 |
| 全局健康 | 面板只读聚合行，不派活、不改码、不合分支 | 生效；恰好 1 行，与 7 个管家并列展示 |

`ops/panel_logic_check.py` 锁住“管家键集合正好是七个”“全局健康恰好 1 行”，
并逐项检查 `context / route / merge / tune / exec / pm / accept / health` 上屏。
`ops/panel_render_check.py` 锁住真实面板渲染，不只是后端数据存在。

## 4. 普通用户怎么真正用上

1. 从公开存在的 `v0.6.0-preview.140` prerelease 取源码，按
   `docs/BETA_LOCAL_TRIAL.md` 运行 `./install.sh --refresh-plugin /绝对路径/你的项目`。
2. 完全退出并重开 Codex 会话，让 MCP 工具重新注入。
3. 在原生输入框说：`请调用 render_autopilot_panel 工具打开 Codex Autopilot 面板。`
4. 看到 MCP App / 内联卡片 / 侧栏面板出现，第一屏显示“需要你决定什么 / 系统正在做什么 /
   真正闭环了什么”，并且在“系统在干对的事吗”下面看到 7 个管家加 1 条全局健康，
   才算加载成功。
5. 继续在原来的 Codex 输入框提需求；不需要选项目、对话、分支或角色。需要人拍板的
   产品方向、删数据、发布和人工验收会进入“需要你决定什么”。

API key 登录不需要为 B1 切换。ChatGPT 订阅的差异在 `docs/BETA_LOCAL_TRIAL.md` 已写清：
订阅走 ChatGPT 账号和 plan 权益，Codex cloud / Remote Control / 跨设备依赖云侧能力，
API key 不包含这些功能；这些属于 B2。

## 5. 当前证据

- 源码决策：`D-0005`，lane 为 `codex/b1-final-closure-20261124`。
- 分支与落盘：分支 `codex/b1-final-closure-20261124`；落盘验收 `A-0244`，
  进展 `P-0243`；审计基准的 `origin/main` 顶端为 `7a42c137`。
- 本轮补齐：审计发现 `P-0242` 仍停在 `doing`，但同分支已有 `P-0243` 落盘和
  `A-0244` 验收；现已按追加方式停用过期状态，审计结论落进本页并回写真相层。
- 门禁原文：

```text
[experience-contract] $ committed contract validation (cwd=.) -> exit 0
[selftest] $ python3 ops/selftest.py (cwd=.) -> exit 0
[panel-logic] $ python3 ops/panel_logic_check.py (cwd=.) -> exit 0
[panel-render] $ python3 ops/panel_render_check.py --no-shot (cwd=.) -> exit 0
```

- 本机安装态：

```json
{
  "version": "0.6.0-preview.140",
  "source": "./plugins/codex-autopilot/releases/0.6.0-preview.140",
  "previous_source": "./plugins/codex-autopilot/releases/0.6.0-preview.145",
  "source_commit": "02a7c6a5293eb4838d0ef05029e173d508c82ca7"
}
```

- 公开 tag 只有 `v0.6.0-preview.139` 与 `v0.6.0-preview.140`，没有 `.146`。
- 本轮原生面板工具返回 `ok:true`；多管家行可见，`exec` 为 `off`，native 为
  `connected=false / can_dispatch=false / blocked_by=remote-control-chatgpt-pairing`。

## 6. 仍未闭合的部分

- 人工视觉/体验项 `A-0187` 至 `A-0192`、`A-0022`、`A-0044` 仍是 `todo`，
  必须由用户在真实宿主里看过并明确说“可以”才能签收；代理不代签。
- `L-0021` 仍是 `open`（ux），记录用户对面板信息承载的原始返修；机器侧已有恢复证据，
  但最后一眼仍需人看。
- `L-0020` 仍是 `open`（gate），它要求达到可对外发表的顺畅产品标准。B1 只闭合了
  本地主链和公开 beta 入口，不宣称完整愿景或全量公开 Beta 四层全部通过。
- native 派活和跨设备未闭合；B2 需要真实 ChatGPT 订阅账号、Remote Control pairing、
  `connected=true / can_dispatch=true`，且属于新的发布/方向级事项。

## 7. 最终判断

按用户已选的 B1 口径，**工程链、公开入口、本机主链、多管家责任视图和使用说明已经闭环**。
把 B2、人工签收和完整公开 Beta 四层标准也算进同一句“全部完成”是不诚实的；它们仍按
上面的未闭合项独立记账。
