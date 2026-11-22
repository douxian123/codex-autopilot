# 0.6.0-preview.126 候选发布验证报告

日期：2026-11-22

## 1. 结论

`0.6.0-preview.126` 是动态验收专项执行器的候选发布版本。它基于 `origin/main = 35c53856016fb72db46078d6e36ff4bc0b76adf9`，
在候选分支 `codex/release-126-dynamic-acceptance-20261122` 上只增加了版本号 bump。

本机 live 插件仍然是 `0.6.0-preview.125`，没有切换。切换 live 属于发布动作，未在本轮执行。

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

## 10. 本机 live 现场未改动

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

候选包已经完成，未做 live 切换。live 切换会改写 marketplace、刷新 Codex 插件缓存、重建 Companion，并可能重载 daemon，属于发布动作，需要用户明确拍板。

用户选择 A：`0.6.0-preview.126` 切换为 live。好处是当前会话之后可以使用新动态验收专项执行器；代价是会改动本机插件入口和后台进程，当前会话已加载的工具未必立刻更新，可能需要新开 Codex 会话确认。

用户选择 B：保持 `0.6.0-preview.125`。好处是当前现场完全不动；代价是动态验收专项执行器只在候选包和仓库里可追溯，本机 live 仍然不生效。
