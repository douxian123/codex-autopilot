# Codex Autopilot B1 本地主链试用说明

公开 beta：`0.6.0-preview.140`  
源码发布候选：`0.6.0-preview.147`（已合入 `origin/main`，尚未打公开 tag/prerelease）

## 从哪里拿这个 beta

普通用户现在应从 GitHub 上真实存在的
[`v0.6.0-preview.140` prerelease](https://github.com/douxian123/codex-autopilot/releases/tag/v0.6.0-preview.140)
获取公开 beta。它是源码 tag，不要求装未知二进制；从 tag 检出后运行仓库自带的安装脚本即可：

```bash
git clone --branch v0.6.0-preview.140 --depth 1 https://github.com/douxian123/codex-autopilot.git
cd codex-autopilot
./install.sh --refresh-plugin /absolute/path/to/your-project
```

如果 `git clone` 提示 annotated tag 的 warning，这是正常提示，不表示安装失败。
安装脚本把运行时代码复制进插件自己的版本目录，不会要求你的项目先装一份运行时代码。

`.146` 修复了旧 MCP 会话在宿主升级后继续读旧账本的问题，并已通过隔离安装和对外发布评估；
但它尚未打公开 tag、也没有公开 prerelease 页面。不要把源码候选当成现在可下载的版本，
也不要把“源码已合并”写成“本机已生效”。本页在 `.146` 真正发布前始终按上面的 `.140` 入口安装。

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

### 怎么确认多管家真的加载了

面板第一屏的「系统在干对的事吗」下面，应有一条紧凑的管家状态条。正常能看到 8 个名字：`上下文`、`路由`、`合并`、`状态`、`实现`、`项目`、`验收`、`全局健康`。这不是要你选 8 个机器人，也不需要分别打开它们；它们是同一套自动挡链路的 7 个责任视图，加 1 条只读健康摘要。

点任意一个管家名，应展开它的当前状态、最近完成项和历史记录。正常现象包括：`上下文` 显示若干条候选等你确认，`验收` 显示若干条验收候选等你确认，`实现管家：自动实现已关`，`全局健康` 显示 `attention` / `ok` / `unknown` / `off` 之一。看到 `attention` 时，按第一屏「需要你决定什么」里的下一步处理；看到候选时，不要自己批量 promote/dismiss，只有你明确说“这条确认/不算”才处理。

看不到管家条、只有空白、或提示绑定失效，先按旧会话处理：完全退出并重新打开 Codex，在新会话复验。不要把浏览器驾驶舱或 HTML 页面当成这里的多管家面板。

版本和绑定关系可另外核对：

```bash
cat ~/plugins/codex-autopilot/install-state.json
codex plugin list --json
```

从公开 `.140` 入口安装后，两者应指向 `0.6.0-preview.140`。`install-state.json` 还应带有本次源码 commit。旧版本目录保留，不覆盖历史版本。

如果任一工具或面板显示 `AUTOPILOT_BINDING_BROKEN`、`binding_broken`、“Codex 还在跑升级前的旧 MCP 进程”或“正在运行的插件代码目录已被删除”，先按旧会话处理：完全退出并重新打开 Codex，再重新打开面板。`.146` 候选已加入旧进程 fail-closed，只保留 `autopilot_status` 和 `render_autopilot_panel` 返回诊断与恢复动作，因此不再把旧项目的 `resolve_list`、自治队列或产品状态冒充当前真相。公开 `.140` 用户也应按这个顺序重启后再验。重跑 `install.sh` 不会替换已经在跑的进程；只有重启后仍显示绑定失效，才需要重跑安装。

如果显示 `AUTOPILOT_BINDING_DRIFT`、`binding_drift`、“插件仍绑定旧脏状态根”或“继续提需求会写进旧账本”，说明插件 `.mcp.json` 还绑着旧 worktree，而当前会话已经在同一个仓库的另一个状态根里。此时 `intake_request` / `capture_request` 会先拦下写入，不会再把你的需求记进旧项目账本。按工具返回的路径执行：

```bash
./install.sh <工具提示的当前状态根>
```

然后完全退出并重新打开 Codex，再打开面板复核。只设 `KG_REPO` 环境变量不会改插件 `.mcp.json`，也不会让已经启动的 MCP 进程换绑定；这一步必须重新绑定并重开宿主。自动挡不会替你改 live 绑定、重启 daemon 或切 provider。

## 回滚

```bash
./install.sh --list-releases
./install.sh --rollback
```

回滚只还原自动挡的 marketplace 入口；项目账本与旧版本目录不会被删除。若已经显式启用过本机 guards/daemon，要按当时的 plist 状态另行评估，B1 默认安装不替你撤销这些显式选择。

## API key 与 ChatGPT 订阅有什么区别

- API key：按量计费，适合本地 CLI、IDE、SDK 等开发工作流。你目前只用 API key，B1 本地主链不需要切换登录方式。
- ChatGPT 订阅：使用 ChatGPT 账号登录 Codex，走 ChatGPT plan 的 included usage / credits 和 workspace 权限。Codex cloud、远端/跨设备连接依赖云侧能力，API key 不包含这些功能。Free / Go 的 Codex 权益受 rollout 和 plan 限制；Beta 用户更适合 Plus / Pro / Business。
- Remote Connection / Control this Mac：需要同一 ChatGPT 账号或 workspace、目标 Mac 在线，可能经过 MFA / SSO / passkey，管理员也可能要求先开启 Remote Control access。设置入口是 `Settings > Connections > Control this Mac or PC`，通过 QR 配对。

本次 beta 不会替你执行 `codex login` 浏览器登录、ChatGPT 配对、provider 切换、桌面端重启或守护重启。先用你当前已有的登录方式跑通本地主链；B1 不要求切换登录，也不承诺远端/跨设备。

## 已知边界

- 隔离 HOME 安装验收与全量门禁已完成；尚未承诺原生子对话派活在真实桌面窗口稳定出现。
- 跨机移植包默认不含 `~/.codex/auth.json`，但 provider 配置仍属敏感信息；一次性令牌只防裸路径访问，仍然使用明文 HTTP，只在可信 WiFi 使用。
- 回归过程中若宿主仍加载旧会话的 `.136` 等旧插件，属于会话注入问题；用新会话或重启宿主验证，不要拿旧会话结果代表 B1。
