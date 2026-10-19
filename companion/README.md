# Codex 自动挡 · macOS 菜单栏 Companion

它是**状态锚点**，不是第二套面板：读和写都只走 `ops/companion_api.py` 这一条通道，
自己不另存任务账本，也不自己实现业务逻辑。

## 装

正常路径是跟着安装器走，不用单独装：

```bash
./install.sh          # 会编译并把 .app 装到 ~/Applications/CodexAutopilotCompanion.app
```

单独调试时：

```bash
cd companion
./build.sh            # 打包到 companion/dist/CodexAutopilotCompanion.app
./build.sh --binary-only   # 只编译可执行文件
```

`build.sh` 直接调 `swiftc`，不走 SwiftPM。原因写在脚本头部：SwiftPM 会写
`~/Library/Caches/org.swift.swiftpm` 并且自己再套一层沙箱，受限环境里两个都会失败；
另外默认的 clang 模块缓存不可写时会误报成「SDK not supported by the compiler」。
本脚本把模块缓存钉在仓库内，只有默认 SDK 真的编不过时才逐个兜底。

## 用

```bash
open ~/Applications/CodexAutopilotCompanion.app
```

- 菜单栏图标：开着是 `road.lanes`，关着是 `pause.circle`
- `⌥⌘A` 开合面板，`⌥⌘S` 截图派活，`⌥⌘D` 打开完整面板
- 把文件（截图、日志、文稿）拖到菜单栏图标上 → 变成附件
- 选中文本/文件 → 服务 → 「发送到 Codex 自动挡」
- 面板里：总开关、当前任务、需要你处理的事、一句话派活、完整面板

首次用「截图」时 macOS 可能要求授予屏幕录制权限（框选本身是你在操作，属于显式同意）；
「完整面板」只是用默认浏览器打开生成的驾驶舱 HTML，不额外读屏。

## 控制接口

`CODEX_AUTOPILOT_API` 可覆盖控制接口路径；否则读安装器写的
`~/Library/Application Support/CodexAutopilot/companion.json`。找不到就如实提示
“未配置控制接口”，不去猜别的路径。

```bash
python3 ../ops/companion_api.py status
python3 ../ops/companion_api.py enable | disable
python3 ../ops/companion_api.py intake --attach /tmp/shot.png "左边导航排版坏了"
python3 ../ops/companion_api.py panel
```
