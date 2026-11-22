# 0.6.0-preview.126 发布与 live 换代验证报告

日期：2026-11-22

## 1. 结论

`0.6.0-preview.126` 的动态验收专项执行器已完成源码闭环、候选发布、本机 live 换代和现场门禁验证。

功能源码合并点是 `bbcc3832 Merge remote-tracking branch 'origin/main' into codex/release-126-dynamic-acceptance-20261122`；
该点已推送到候选分支和 `main`。随后本报告作为文档补丁另行提交，`main` 会再前进一步；
这是文档收口，不代表功能源码有新变更。本机 live 插件已在用户明确选择 A 后切换到 `0.6.0-preview.126`，
live release 的来源 commit 是 `857b45756870ea8b63d29ac165b6e8ed39c171bc`，旧版本 `0.6.0-preview.125` 保留可回滚。

口径边界：第 3、10、13 节记录的是当时的历史现场，其中出现过候选、`.125` live 和更早的 `main` 引用；
最终状态以第 14、15 节为准。由于不可变版本目录不允许覆盖同版本，live release 仍来自 `857b457...`，
而随后合入 `main` 的 4 个 land 修复只在源码里生效；要不要再发一个新的 live 版本需要用户拍板。

## 2. 候选分支

```text
CANDIDATE_PATH=/Users/danta/.newmax/workspace/codex-autopilot-release-126-20261122
CANDIDATE_BRANCH=codex/release-126-dynamic-acceptance-20261122
BASE=35c53856016fb72db46078d6e36ff4bc0b76adf9
VERSION_BUMP_COMMIT=02e557a4e7d348333a67f7ffe2a4d116b3e640fb
```

版本号改动：

```diff
-  "version": "0.6.0-preview.125",
+  "version": "0.6.0-preview.126",
```

## 3. 远端刷新状态

本轮尝试刷新远端失败，原文如下。这里记录为现场事实，不把它解释成版本漂移。

```text
fatal: unable to access 'https://github.com/douxian123/codex-autopilot.git/': Error in the HTTP2 framing layer
fatal: unable to access 'https://github.com/douxian123/codex-autopilot.git/': Failed to connect to github.com port 443 after 75007 ms: Couldn't connect to server
error: RPC failed; curl 56 Recv failure: Operation timed out
fatal: expected flush after ref listing
```

候选基于本地已验证的 `origin/main = 35c53856016fb72db46078d6e36ff4bc0b76adf9`。

## 4. 源码全量自检

命令：

```bash
cd /Users/danta/.newmax/workspace/codex-autopilot-release-126-20261122
python3 -u ops/selftest.py
```

结果：

```text
SELFTEST_EXIT=0
```

日志：

```text
/tmp/ca-release126-source-selftest-20261122.log
```

最后一行：

```text
selftest ok: 人判类收口停在 awaiting_human、人确认后才 done；机器可证的 bug 仍直达 done，自动派发闭环未被打断。
```

全量自检已经包含 `test-specialized-acceptance-executors.py`，不是只跑局部单测。

## 5. 隔离安装

安装使用全新 `HOME` 和独立 trunk，显式跳过 marketplace、Companion、daemon 改指。

```text
HOME_DIR=/private/tmp/ca-release126-home.RylBzU
TRUNK=/private/tmp/ca-release126-trunk-20261122-4947
RELEASE=/private/tmp/ca-release126-home.RylBzU/plugins/codex-autopilot/releases/0.6.0-preview.126
```

命令：

```bash
NO_DAEMON_REPOINT=1 HOME="$HOME_DIR" bash "$BASE/install.sh" \
  --state-root "$BASE" \
  --trunk "$TRUNK" \
  --no-marketplace \
  --no-seed \
  --no-companion
```

结果：

```text
INSTALL_EXIT=0
```

日志：

```text
/tmp/ca-release126-install.log
```

关键输出：

```text
已安装不可变版本 0.6.0-preview.126 到 /private/tmp/ca-release126-home.RylBzU/plugins/codex-autopilot/releases/0.6.0-preview.126
```

版本来源元数据：

```text
SOURCE_META_VERSION=0.6.0-preview.126
SOURCE_META_COMMIT=02e557a4e7d348333a67f7ffe2a4d116b3e640fb
SOURCE_META_DIRTY=False
```

## 6. 安装产物文件齐全性

以下文件均存在：

```text
FILE=YES ops/test-specialized-acceptance-executors.py
FILE=YES ops/dynamic_acceptance_scope.py
FILE=YES ops/pre_acceptance.py
FILE=YES ops/acceptance_dictionary.py
FILE=YES ops/test-pre-acceptance.py
FILE=YES ops/test-acceptance-dictionary.py
FILE=YES ops/test-dynamic-acceptance-scope.py
FILE=YES ops/test-generate-acceptance-scope-tool.py
FILE=YES ops/test-acceptance-evidence.py
FILE=YES docs/PRE_ACCEPTANCE_STANDARD.md
FILE=YES docs/PRODUCT_CONTRACT.md
FILE=YES docs/experience/panel-renderer-contract.json
FILE=YES docs/research/adoption-strategy.json
```

## 7. 安装目录内动态验收门禁

在隔离安装目录内运行：

```bash
python3 ops/test-specialized-acceptance-executors.py
python3 ops/test-pre-acceptance.py
python3 ops/test-acceptance-dictionary.py
python3 ops/test-dynamic-acceptance-scope.py
python3 ops/test-generate-acceptance-scope-tool.py
python3 ops/test-acceptance-evidence.py
python3 ops/test-gate-registration.py
python3 -m py_compile ops/*.py
```

结果：

```text
INSTALLED_GATES_EXIT=0
EXIT=0 py_compile
```

日志：

```text
/tmp/ca-release126-installed-gates-20261122.log
```

关键断言摘要：

```text
specialized-acceptance-executors: PASS
pre-acceptance: PASS
acceptance-dictionary: PASS
dynamic-acceptance-scope: PASS
generate-acceptance-scope-tool: PASS
acceptance-evidence: PASS
gate registration unit: PASS（92 条门禁全部已登记）
```

其中动态验收范围工具已经验证：

```text
PASS 默认只预览
PASS 预览不写项目文件
PASS confirm 后成功落盘
PASS 统一入口默认只返回验收范围预览
PASS 统一入口预览不覆盖已确认范围
PASS 统一入口显式确认后才落动态范围
```

## 8. 源码与安装产物哈希一致性

结果：

```text
HASH_ALL_EXIT=0
```

逐项一致：

```text
HASH=OK ops/test-specialized-acceptance-executors.py 0d72bafcde4980c4d319722d88a7f6dbba79918d25d625b141b065048aaeaa5e
HASH=OK ops/dynamic_acceptance_scope.py 93333fb41edad76115f9a908ac0f32707c203fd63834ea71d53dc8f81031091e
HASH=OK ops/pre_acceptance.py a35bc0889de76a9f01a5710ce27105806123a2cce7543a437bfdc351afea06fc
HASH=OK ops/acceptance_dictionary.py 9eb3bda9385ac35c709ada3bfc82707375b6e9708c135b5f066ed8cb8c1ac04d
HASH=OK ops/central_mcp.py b5b4d2f57e897adf3b8a648038705309cfafc1f4a69a701b6bdd6e27e299d0ae
HASH=OK ops/selftest.py f34276ba027b2da2ef2ed7949fc2678a68291a97219927e8a247fbca0f3a667e
HASH=OK docs/PRE_ACCEPTANCE_STANDARD.md c1d3ba9e2394325e394699a08b4d25d753f98fffcdaa34704b399f81a57ecd6f
HASH=OK docs/PRODUCT_CONTRACT.md dd53ed4867e9aa8d5659f428725bc9e18f535624cd49cd0d7a6166dcc2d65611
HASH=OK docs/experience/panel-renderer-contract.json 09bf0c10f4fa047f11ec87d62a16d6ddaf109f2389d9e60542e3239fd38e863b
HASH=OK docs/research/adoption-strategy.json f1bee1f7b592376662cb49195cb060638b3705f4e8f63e0b9f282986a257a0c2
```

## 9. 面板与发布目录检查

在隔离安装目录内运行：

```bash
python3 ops/test-selftest-release-dir.py
python3 ops/panel_render_check.py
python3 ops/panel_logic_check.py
```

结果：

```text
PANEL_RELEASE_GATES_EXIT=0
```

日志：

```text
/tmp/ca-release126-panel-release-gates-20261122.log
```

关键输出：

```text
selftest release dir: PASS
Codex Autopilot 面板渲染检查
结果：全部通过
Codex 自动挡 面板首屏逻辑检查
结果：全部通过
```

面板首屏截图：

```text
/private/tmp/ca-release126-home.RylBzU/plugins/codex-autopilot/releases/0.6.0-preview.126/docs/panel-first-screen.png
```

渲染契约相关关键断言：

```text
PASS 体验契约投影到面板首屏
PASS 渲染契约投影到中间态面板
PASS 当前面板声明为 interim renderer
PASS 运行态提示写清不会自动重启
PASS 健康条取不到时不冒充正常
```

## 10. 本机 live 换代前状态（历史记录）

本节保留换代前的 `.125` 证据，不能当成最终现场。最终 live 状态见第 14 节。

本机插件列表：

```text
codex-autopilot@personal             installed, enabled  0.6.0-preview.125     /Users/danta/plugins/codex-autopilot/releases/0.6.0-preview.125
```

本机 install-state：

```json
{
  "version": "0.6.0-preview.125",
  "source": "./plugins/codex-autopilot/releases/0.6.0-preview.125",
  "previous_source": "./plugins/codex-autopilot/releases/0.6.0-preview.124",
  "source_commit": "2f1a0398bd2875cbf4bc21c243b62a09b93e1451"
}
```

本机运行态契约：

```text
LIVE_RUNTIME_CONTRACT_EXIT=0
```

```json
{
  "status": "ok",
  "version": "0.6.0-preview.125",
  "commit": "2f1a0398bd2875cbf4bc21c243b62a09b93e1451",
  "source": "./plugins/codex-autopilot/releases/0.6.0-preview.125",
  "plist_script": "/Users/danta/plugins/codex-autopilot/releases/0.6.0-preview.125/ops/central_daemon.py",
  "pid": "70368",
  "pid_script": "/Users/danta/plugins/codex-autopilot/releases/0.6.0-preview.125/ops/central_daemon.py",
  "unknown": [],
  "failures": []
}
```

live `.125` 没有专项执行器：

```text
FILE=NO /Users/danta/plugins/codex-autopilot/releases/0.6.0-preview.125/ops/test-specialized-acceptance-executors.py
```

因此当前不能宣称动态验收专项执行器已经在本机 live 生效。

## 11. 真实任务级验收范围调用

两组调用都在隔离安装的 `.126` 目录内运行，状态根是新建的临时 Git 项目；只写临时项目，没有写 live 现场。

### 11.1 `generate_acceptance_scope`

命令入口：

```text
/tmp/ca-release126-real-task-scope-20261122.log
```

实际结果：

```text
PREVIEW_RC= 0
PREVIEW_PERSISTED= False
PREVIEW_EXISTS_BEFORE_CONFIRM= False
CONFIRM_RC= 0
CONFIRM_PERSISTED= True
CONFIRM_EXISTS_AFTER= True
EXPECTED_VERSION= 591e1362128c8dcb74ca290f0e7422bbcd71b996
SELECTED_CHECKS= ["engineering", "evidence"]
UNMAPPED_RISKS= []
SCOPE_EXPECTED_VERSION= 591e1362128c8dcb74ca290f0e7422bbcd71b996
REAL_TASK_SCOPE=PASS
REAL_TASK_SCOPE_EXIT=0
```

结论：后端任务只选 `engineering + evidence`，没有把前端、安全、AI 输出门禁误选进来；预览不写范围，确认后写入的 `expected_version` 与临时项目 HEAD 一致。

### 11.2 `intake_request`

命令入口：

```text
/tmp/ca-release126-real-task-intake-20261122.log
```

实际结果：

```text
INTAKE_PREVIEW_RC= 0
INTAKE_PREVIEW_TEXT= 需求已进入统一入口；动态验收范围仅生成预览，尚未作为本次活动门禁。
INTAKE_SCOPE_PERSISTED= False
INTAKE_SCOPE_EXISTS_BEFORE_CONFIRM= False
EXPERIENCE_REQUIRED= True
INBOX_EXISTS_AFTER_PREVIEW= True
INBOX_LINES_AFTER_PREVIEW= 1
INTAKE_CONFIRM_RC= 0
INTAKE_SCOPE_PERSISTED_AFTER= True
INTAKE_SCOPE_EXISTS_AFTER= True
SELECTED_CHECKS= ["alignment", "comprehension", "engineering", "evidence", "frontend"]
EXPECTED_VERSION= dacfb3b132736cc0da27ed978ce9078d423a0d27
REAL_TASK_INTAKE=PASS
REAL_TASK_INTAKE_EXIT=0
```

结论：统一入口默认把需求写进唯一入口，但动态验收范围只是预览；显式 `confirm_scope=true` 后才把范围写入目标项目。视觉交互任务触发 `experience_contract.required=true`，选中前端和理解性验收。这里“预览”特指验收范围，不表示 `intake_request` 这个提交动作本身无副作用。

首次夹具曾把 `[]` 当作单行写进 JSONL，导致 `AttributeError: 'list' object has no attribute 'get'`；这是临时夹具错误，不是产品错误。改用空 JSONL 后复跑通过。

## 12. 还差什么

已完成：源码合并与推送、live 换代、marketplace / 插件缓存 / Companion / daemon / 定时任务改指、live 现场机器门禁。

未完成或未验证：

1. 新开 Codex 会话后的用户肉眼验收：确认侧栏 / 对话里的自动挡面板能打开，首屏绑定项目显示为 `/Users/danta/Documents/Codex/codex-autopilot-native-autopilot`，并出现“交付前自检”区块。当前会话已加载的 MCP 工具不保证热更新，机器不能代签。
2. live release 来源是 `857b457...`；`main` 后续 4 个 land 修复（`85e992e9`、`fdbb279a`、`cb5832ef`、`36a265d6`）已在源码和远端 `main`，但没有进入已安装的不可变 `.126` release。要不要发新的 live 版本，需要用户拍板。
3. 最终面板 redesign 未定稿。当前通过的是 `panel renderer contract` 下的 interim renderer，不能宣称最终视觉版上线。

## 13. 远端闭环（第一次发布推送，历史记录）

候选分支推送成功后，已确认 `origin/main` 未漂移且候选是其快进后继，随后把验证过的候选推入主线：

```text
To https://github.com/douxian123/codex-autopilot.git
   35c53856..475c5377  HEAD -> main
PUSH_MAIN_EXIT=0
```

当时回读结果：

```text
REMOTE_MAIN=475c537783c2bd525a8298a917d6305a04cd5feb	refs/heads/main
ORIGIN_MAIN=475c537783c2bd525a8298a917d6305a04cd5feb
LOG_ORIGIN_MAIN=475c5377 docs(release): add real task acceptance scope evidence
```

这记录了 `0.6.0-preview.126` 的版本号和当时报告进入远端 `main` 的第一次推送。后续远端继续前进到 `36a265d6`，
最终闭环见第 15 节；本机 live 切换证据见第 14 节。

## 14. live 换代现场证据

用户明确选择 A 后，执行了真实 live 换代：

```bash
cd /Users/danta/.newmax/workspace/codex-autopilot-release-126-20261122
bash ./install.sh \
  --state-root /Users/danta/Documents/Codex/codex-autopilot-native-autopilot \
  --trunk /Users/danta/Documents/Codex/codex-autopilot-native-autopilot-trunk \
  --refresh-plugin
```

结果：

```text
LIVE_INSTALL_EXIT=0
```

关键安装输出：

```text
已安装不可变版本 0.6.0-preview.126 到 /Users/danta/plugins/codex-autopilot/releases/0.6.0-preview.126
已注册 codex-autopilot 到 /Users/danta/.agents/plugins/marketplace.json，当前版本路径：./plugins/codex-autopilot/releases/0.6.0-preview.126
原版本仍完整保留，可用 ./install.sh --rollback 回退到：./plugins/codex-autopilot/releases/0.6.0-preview.125
已安装菜单栏 Companion：/Users/danta/Applications/CodexAutopilotCompanion.app
守护已切到 0.6.0-preview.126 并重启。
定时任务改指完成：10 条已更新。
插件缓存已刷新。
```

live 运行态契约：

```text
LIVE126_RUNTIME_CONTRACT_EXIT=0
```

```json
{
  "status": "ok",
  "version": "0.6.0-preview.126",
  "commit": "857b45756870ea8b63d29ac165b6e8ed39c171bc",
  "source": "./plugins/codex-autopilot/releases/0.6.0-preview.126",
  "plist_script": "/Users/danta/plugins/codex-autopilot/releases/0.6.0-preview.126/ops/central_daemon.py",
  "pid": "85211",
  "pid_script": "/Users/danta/plugins/codex-autopilot/releases/0.6.0-preview.126/ops/central_daemon.py",
  "unknown": [],
  "failures": []
}
```

Codex 插件列表和缓存：

```text
codex-autopilot@personal installed, enabled 0.6.0-preview.126 /Users/danta/plugins/codex-autopilot/releases/0.6.0-preview.126
cache version = 0.6.0-preview.126
FILE=YES /Users/danta/.codex/plugins/cache/personal/codex-autopilot/0.6.0-preview.126/ui/panel.html
FILE=YES /Users/danta/.codex/plugins/cache/personal/codex-autopilot/0.6.0-preview.126/ops/test-specialized-acceptance-executors.py
```

live 目录内复跑专项门禁：

```text
specialized-acceptance-executors: PASS
pre-acceptance: PASS
acceptance-dictionary: PASS
dynamic-acceptance-scope: PASS
generate-acceptance-scope-tool: PASS
acceptance-evidence: PASS
gate registration unit: PASS（92 条门禁全部已登记）
EXIT=0 py_compile
LIVE126_GATES_EXIT=0
```

live 面板检查：

```text
体验契约投影到面板首屏
PASS  渲染契约投影到中间态面板
PASS  当前面板声明为 interim renderer
PASS  运行态提示写清不会自动重启
PASS  健康条取不到时不冒充正常
结果：全部通过
EXIT=0
LIVE126_PANEL_TAIL_EXIT=0
```

命令行状态回读：

```text
安装绑定   OK（0.6.0-preview.126 · 状态根 /Users/danta/Documents/Codex/codex-autopilot-native-autopilot）
常驻守护       OK（pid 85211，绑定 /Users/danta/Documents/Codex/codex-autopilot-native-autopilot）
```

结论：本机现场已经吃到 `.126`，动态验收专项执行器在 live 目录中存在并通过门禁。最终仍需新开 Codex 会话做一次用户肉眼验收，因为当前会话已加载的 MCP 工具不保证热更新。

## 15. 合并后复验与功能源码远端闭环

live 换代后，`origin/main` 又前进了 4 个 land 修复提交。候选分支已把它们合并进本地 `bbcc3832`，合并无冲突。
在合并后的 `bbcc3832` 上复跑验证：

```bash
cd /Users/danta/.newmax/workspace/codex-autopilot-release-126-20261122
python3 ops/test-land-dirty-trunk-clean-worktree.py
python3 ops/test-trunk-is-same-repo.py
python3 -m py_compile ops/*.py
python3 -u ops/selftest.py
```

结果：

```text
land dirty trunk clean worktree unit: PASS
LAND_DIRTY_TRUNK_EXIT=0
land trunk is same repo unit: PASS
TRUNK_SAME_REPO_EXIT=0
PY_COMPILE_EXIT=0
MERGED_SELFTEST_EXIT=0
selftest ok: 人判类收口停在 awaiting_human、人确认后才 done；机器可证的 bug 仍直达 done，自动派发闭环未被打断。
```

日志：

```text
/tmp/ca-release126-land-dirty-trunk-20261122.log
/tmp/ca-release126-trunk-same-repo-20261122.log
/tmp/ca-release126-py-compile-merged-20261122.log
/tmp/ca-release126-selftest-merged-20261122.log
```

推送前远端引用：

```text
49c75a73fc346862c24d1ff759644b6f12230eda	refs/heads/codex/release-126-dynamic-acceptance-20261122
36a265d603d811b077f021689dd70c384bb2ee73	refs/heads/main
```

功能合并点推送结果（本报告提交前）：

```text
To https://github.com/douxian123/codex-autopilot.git
   49c75a73..bbcc3832  HEAD -> codex/release-126-dynamic-acceptance-20261122
PUSH_CANDIDATE_EXIT=0
To https://github.com/douxian123/codex-autopilot.git
   36a265d6..bbcc3832  HEAD -> main
PUSH_MAIN_EXIT=0
```

边界说明：以下推送结果记录功能合并点 `bbcc3832` 进入远端；随后本报告的文档补丁会再次推进 `main`，
最终远端引用以推送后回读为准，功能源码仍停在 `bbcc3832`。本机 live release 仍是 `857b457...` 来源的不可变 `.126`。
两者版本号相同但源码 commit 不同，不能把“源码已合入”说成“live 现场也已更新到该 commit”。

## 16. 用户怎么用上并验收

最短使用路径：

1. 新开一个 Codex 会话。当前已加载 MCP 的会话不保证热更新到新插件版本。
2. 在 Codex 里打开 Plugins → Personal，确认 `Codex 自动挡` 显示为 `0.6.0-preview.126`。
3. 在侧栏或对话里说“打开自动挡面板”。看到面板首屏后，确认绑定项目是 `/Users/danta/Documents/Codex/codex-autopilot-native-autopilot`。
4. 首屏应能看到安装绑定 `OK`、常驻守护 `OK`、唯一入口、分支/冲突/落盘统计。高级区应能看到 PM 简报、验收标准、PRD 迭代、上下文档案和管家状态。
5. 如果要发需求，直接在总输入框说；默认先路由，验收范围只预览。看到范围后再显式确认，才会把动态验收范围写进项目账本。

命令行快速验收：

```bash
cat /Users/danta/plugins/codex-autopilot/install-state.json
KG_REPO=/Users/danta/Documents/Codex/codex-autopilot-native-autopilot \
KG_TRUNK=/Users/danta/Documents/Codex/codex-autopilot-native-autopilot-trunk \
python3 /Users/danta/plugins/codex-autopilot/releases/0.6.0-preview.126/ops/kgctl.py status
```

看到以下组合算机器侧正常：

```text
"version": "0.6.0-preview.126"
安装绑定   OK（0.6.0-preview.126 · 状态根 /Users/danta/Documents/Codex/codex-autopilot-native-autopilot）
常驻守护       OK（pid ...，绑定 /Users/danta/Documents/Codex/codex-autopilot-native-autopilot）
```

人工侧必须你自己看一眼：新会话里面板能否打开、首屏是否来自真实状态、交互是否符合当前 interim renderer 的视觉预期。
