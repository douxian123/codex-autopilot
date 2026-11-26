# Changelog

## 0.6.0-preview.140 (2026-11-23)

B1 本地主链公开 beta 补丁版。

- 修复 Codex 升级后旧 MCP 进程仍在跑时的面板误导：原因、当前状态和恢复步骤现在会明确说“完全退出并重新打开 Codex”，不再先让用户重跑 `install.sh`。重跑安装不会替换已经启动的进程。
- 真正的绑定失效（缺 `KG_REPO`、状态根不是项目账本等）仍保持原有重装指引，两种故障不再混为一谈。
- `binding_health()` 现在把陈旧运行代码标记传给面板；新增 `ops/test-binding-recovery-action.py` 并登记进总闸，防止这类文案回归。

## 0.6.0-preview.139 (2026-11-23)

B1 本地主链小范围试用准备。

- 默认安装只装插件主链：不首次安装 launchd 常驻、不维护已装的本机 guards / hooks、不迁移本地密钥、不切换 provider。安装结束会明确打印跳过范围。
- 本机能力改为逐项 opt-in：`--enable-local-guards` 只维护已存在的安装；`--install-daemon` 是唯一允许首次安装常驻的开关；`--migrate-local-secrets` 是唯一允许迁移 HyAPI 本地密钥的开关。
- 跨机移植默认排除 `~/.codex/auth.json`，新增 `--include-auth` 显式开关；一次性令牌与明文 HTTP 的风险提示保留。
- 修复资源准入 hook 安装路径回归：配置模板不再写死 `/Users/danta`，安装器会把 `__CODEX_HOOK_HOME__` 展开到 `$HOME/.codex/hooks/resource-admission.py`，并补了源码与安装后路径回归。
- `disk_reap.py` 和 watchdog 的日志、label、plist 路径支持环境变量覆盖，便于隔离 HOME 验收，不碰本机真实 LaunchAgents。
- 新增 `docs/BETA_LOCAL_TRIAL.md`，覆盖安装、验证、回滚、API key 与 ChatGPT 订阅差异，以及 B1 的能力边界。

更早版本的历史见 Git 提交记录。
