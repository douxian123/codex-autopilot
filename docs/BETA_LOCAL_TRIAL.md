# Codex Autopilot B1 本地主链试用说明

版本：`0.6.0-preview.139`

## 这份 beta 准备交付什么

B1 只覆盖**本地主链**：在 Codex 里继续用原来的输入框提需求，由插件完成记录、路由、验证、产品状态与面板呈现。公开安装默认只装这条主链，不替用户接管机器。

多管家职责在现有实现里分别落在：输入框与意图入口（`central-command`）、安装入门（`setup`）、上下文/问题/验收/状态/产品/自动挡状态等管家工具、落盘门禁与真相台账。当前 beta 阶段可实际使用的是本地工具与主链闭环；原生派活和跨设备远端连接不作为 B1 承诺。

## 默认安全边界

默认 `./install.sh` 只做插件主链安装：插件版本目录、marketplace 登记、项目缺失账本、可选菜单栏 Companion。它不会：

- 首次安装 launchd 常驻守护
- 改写已有的 launchd plist 或 `~/.codex/hooks.json`
- 迁移 HyAPI 本地密钥
- 改 provider、`~/.codex/auth.json` 或你的 Codex 登录方式

安装结束会打印 `本机集成按默认策略跳过：未改 launchd、hooks.json、本地密钥或 provider。`

需要本机能力时逐项显式开启：

```bash
./install.sh --enable-local-guards      # 只维护已存在的本机 launchd / hooks，不首次安装常驻
./install.sh --install-daemon           # 唯一允许首次安装常驻守护的开关，会写 plist 并回读 launchd
./install.sh --migrate-local-secrets    # 唯一允许迁移 HyAPI 本地密钥的开关
```

`--enable-local-guards` 与 `--install-daemon` 可以一起用；没有显式开关时，即使装了新插件版本，本机常驻也不会被悄悄改写。

## 安装与现场验证

在插件仓库根目录：

```bash
./install.sh --refresh-plugin --verify /absolute/path/to/your-project
```

完全退出并重新打开正在使用的 Codex 会话/桌面端，让 MCP 工具重新注入。然后在新会话里说：

```text
请调用 render_autopilot_panel 工具打开 Codex Autopilot 面板。
```

看到 MCP App / 内联卡片 / 侧栏面板出现，且第一屏显示「需要你决定什么 / 系统正在做什么 / 真正闭环了什么」，才算面板真的加载。当前会话如果只返回 `unsupported call: mcp__codex_autopilot__render_autopilot_panel`，不要改开 HTML 驾驶舱冒充；新开会话或重启宿主后再验。

版本和绑定关系可另外核对：

```bash
cat ~/plugins/codex-autopilot/install-state.json
codex plugin list --json
```

两者应指向 `0.6.0-preview.139`。`install-state.json` 还应带有本次源码 commit。旧版本目录保留，不覆盖历史版本。

## 回滚

```bash
./install.sh --list-releases
./install.sh --rollback
```

回滚只还原自动挡的 marketplace 入口；项目账本与旧版本目录不会被删除。若已经显式启用过本机 guards/daemon，要按当时的 plist 状态另行评估，B1 默认安装不替你撤销这些显式选择。

## API key 与 ChatGPT 订阅有什么区别

- API key：按量计费，适合本地 CLI、IDE、SDK 等开发工作流。你目前只用 API key，B1 本地主链不需要切换登录方式。
- ChatGPT 订阅：使用 ChatGPT 账号登录 Codex。Codex cloud、远端/跨设备连接依赖云侧能力，API key 不包含这些功能。
- Remote Connection / Control this Mac：需要同一 ChatGPT 账号或 workspace、目标 Mac 在线，可能经过 MFA / SSO / passkey，管理员也可能要求先开启 Remote Control access。设置入口是 `Settings > Connections > Control this Mac or PC`，通过 QR 配对。

本次 beta 不会替你执行 `codex login --device-auth`、ChatGPT 配对、provider 切换、桌面端重启或守护重启。先跑通 API key 下的本地主链；远端/跨设备是后续独立验收项。

## 已知边界

- 公开试用前仍需完成隔离 HOME 安装验收与门禁；尚未承诺原生子对话派活在真实桌面窗口稳定出现。
- 跨机移植包默认不含 `~/.codex/auth.json`，但 provider 配置仍属敏感信息；一次性令牌只防裸路径访问，仍然使用明文 HTTP，只在可信 WiFi 使用。
- 回归过程中若宿主仍加载旧会话的 `.136` 等旧插件，属于会话注入问题；用新会话或重启宿主验证，不要拿旧会话结果代表 B1。
