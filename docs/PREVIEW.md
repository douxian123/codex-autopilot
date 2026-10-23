# Codex 自动挡 · 预览包说明

版本：`0.6.0-preview.18`
形态：Codex 插件 + macOS 菜单栏 Companion（一个状态锚点，不是第二套产品）

本版修复同版本目录被移除后重装会覆盖回滚指针的问题：恢复安装仍保留上一版，`--rollback` 不会原地回到当前版本。本版也保留 preview.17 的大仓库状态扫描优化：批量 Git 元数据读取并按本机核心数并发检查独立分支，只读、完整、不改落盘并发；461 条分支的状态根实测从 60 秒超时降到 15.182 秒退出 0。

## 一条命令验收

装和验可以合成一步：

```bash
bash install.sh --verify
```

装完立刻跑同一份验收报告。只想验收、不动安装时用下面这条：

```bash
bash preview-check.sh
```

它会按顺序做完并逐项打印 PASS/FAIL：装到不可变版本目录 → 校验版本目录、菜单栏应用（存在/可执行/已签名）、插件市场指向、Companion 配置 → 打开菜单栏应用 → 真跑一次原生直连自检 → 读总闸状态。退出码等于失败项数；哪一步没成、为什么没成都直接写出来，不会把没跑成的说成通过。

只想自检、不动安装：`bash preview-check.sh --no-install`；无图形界面时：`--no-open`。
加上 `--with-selftest` 会连全量自检一起跑（默认不带：自检里有一条会回头跑本脚本，带上会互相调用）。

面板本身还有两条独立检查：

```bash
python3 ops/panel_logic_check.py    # 真跑面板脚本：首屏每个值是否都来自真实状态（含关闭态）
python3 ops/panel_render_check.py   # 真浏览器渲染真面板：断言首屏文字并存一张截图
```

第一条不依赖浏览器，随时能跑；第二条需要本机有 Chrome/Chromium（`AUTOPILOT_CHROMIUM` 可指定），截图落在 `docs/panel-first-screen.png`。

原生直连协议还能对着真实 Codex 二进制验一遍（默认跳过，不拖慢主自检）：

```bash
AUTOPILOT_SELFTEST_REAL_CODEX=1 python3 ops/selftest.py
```

它在一个临时状态目录里跑真实的 `codex app-server`：建原生会话 → 建窗口集合 → 列出来必须在，并确认归组调用的是服务器认识的方法名。这条只证明协议和官方实现对得上；任务真正出现在你桌面 Codex 窗口里，仍以 `preview-check.sh` 的原生自检为准。

## 子对话怎么归置

自动挡派活时会在 Codex 里建一个按项目命名的窗口集合（例如「Codex 自动挡 · 某个项目」），新建的子对话自动归进去；同名集合复用，不重复创建。集合由 Codex 宿主维护，自动挡不写宿主的数据库。集合建不了时派活照常进行，但会如实记下「没归组」。

## 装什么

一条命令（在仓库根目录）：

```bash
bash install.sh
```

它会做四件事，全部可回退：

1. 把这一版装进一个不可变版本目录：`~/plugins/codex-autopilot/releases/0.6.0-preview.18/`
2. 把插件市场清单里的一行指向这个版本目录（原子替换，只动自动挡这一条）
3. 编译菜单栏 Companion 到 `~/Applications/CodexAutopilotCompanion.app`
4. 写一份 Companion 配置到 `~/Library/Application Support/CodexAutopilot/companion.json`

旧版本目录、旧账本一律不动。

## 首次启动检查

装完按顺序确认三件事：

```bash
open ~/Applications/CodexAutopilotCompanion.app
```

1. 屏幕右上角菜单栏出现自动挡图标，位置在电池/时钟左侧；图标是 macOS 系统符号，不是 Dock 图标：总闸开启时通常为路线符号 `road.lanes`，总闸关闭时为暂停圆 `pause.circle`，有待办时为警示圆 `exclamationmark.circle.fill`
2. 点图标能看到：总开关、当前任务、需要处理的事、派活入口
3. `⌥⌘D` 回到 Codex 里的完整面板（HTML 驾驶舱只在菜单里作备用视图）；面板首屏应显示「是否开启 / 当前项目 / 正在处理什么 / 下一步 / 需要你处理」

## 权限说明

- **屏幕录制**：只有你主动按 `⌥⌘S` 或选「截图派活」时，才会调系统自带的 `screencapture` 框选一次，第一次用 macOS 会问你要「屏幕录制」授权。自动挡不会后台持续截屏，也不会自动读取你的屏幕。
- **不需要辅助功能权限**：全局快捷键走系统 Carbon 注册，不需要「辅助功能」授权。
- **不需要网络权限**：判定、归类、落盘全在本机，不隐式上传任何内容。
- **服务菜单**：「发送到 Codex 自动挡」出现在支持 macOS 服务的应用里；不是所有第三方应用都会显示，所以快捷键才是确定性兜底。

## 升级

同一个版本号不能覆盖重装（安装器会拒绝）。换个版本号重跑即可：

```bash
bash install.sh
```

新版本进新的版本目录，市场清单指向新目录，旧目录保留。想刷掉桌面端的插件缓存，加 `--refresh-plugin`。

## 回滚

```bash
bash install.sh --list-releases   # 看装了哪些版本
bash install.sh --rollback        # 把市场清单指回上一版
```

回滚只动「清单指向哪一版」，不删任何版本目录、不删任何账本。

## 明确不做（这一版）

- 不后台持续截屏、不自动感知屏幕
- 不默认自动实现；自动合入只对门禁通过且无冲突的分支
- 不做桌宠人格动画、不做跨平台 Electron
- 不另起一套任务系统：菜单栏和面板都只读同一条本机控制通道
