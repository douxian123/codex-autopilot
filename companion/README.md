# Codex 自动挡 macOS Companion

这是首个最小菜单栏 Companion。它不是第二套任务系统，只调用
`ops/companion_api.py`：

```bash
cd companion
swift build
swift run CodexAutopilotCompanion
```

运行时可用 `CODEX_AUTOPILOT_API=/absolute/path/to/ops/companion_api.py`
指定控制 API。菜单栏提供总开关、状态刷新和快速派活；完整路线、验收和冲突信息仍在 Codex 面板中。
