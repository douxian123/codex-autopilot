# 动态验收范围接入规范

自动挡维护一套完整的产品验收标准库，但一次任务不直接执行全量标准。任务开始时，
PM 管家根据产品形态、交付目标、任务文本、改动文件和显式风险标签生成
`ops/truth/acceptance_scope.json`，其中 `selected_checks` 是本次最小充分验收包。

## 运行规则

1. 生成阶段只预览，不执行门禁，也不写账本。
2. 只有项目归属明确且显式 `confirm=true` 才能落盘范围文件。
3. `pre_acceptance.py` 有范围文件时只消费 `selected_checks`；没有范围文件时兼容旧的全量逐项门禁。
4. 范围文件损坏、缺少 `selected_checks` 或包含未知检查项时直接 `blocked`，不得静默回退。
5. `unmapped_risks` 代表当前没有对应专项执行器；即使已选检查通过，也必须 `blocked`。
6. 只有本次适用标准全部逐条 `pass` 且没有未映射风险时，才允许 `notify_user=true`。
7. `N/A` 只能由明确不适用的理由支持，不能作为通过的替代品。

## MCP 入口

工具名：`generate_acceptance_scope`。

默认调用只返回预览。确认落盘时传入 `confirm=true`，并传入 `project` 或可解析的 `cwd`。
安全、权限、发布、性能和 AI 输出风险目前会被显式标为 `unmapped_risks`，这表示需要
补充专项门禁或完成风险处理，不能把它们包装成已验收。

