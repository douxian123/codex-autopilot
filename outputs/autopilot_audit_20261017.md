# Codex 自动挡落位审计（2026-10-17）

## 当前基线
origin/main = bfda13a
已安装版本 = 0.6.0-preview.10

## 机器侧已自证
- selftest：exit 0，全部组 ok（含新增 socket 入口 lstat 用例）
- 闭环率：30.8%（4/13），exit 0
- 分支：`codex/native-socket-lstat-v2`（f239795）
- 面板逻辑：全部通过
- 安装落地产物：6 项 PASS

## 仍失败/需人验收
- 菜单栏应用启动 FAIL（open ~/Applications/CodexAutopilotCompanion.app 后无图标）
- 原生直连不可派活 FAIL（`~/.codex/app-server-control/app-server-control.sock` 入口存在，但受限进程访问目标被 `Operation not permitted` 挡住；不是“socket 不存在”）
- A-0005/A-0006/A-0007/A-0008/A-0022 仍是真人桌面验收项

## 本轮修复
- `ops/codex_native.py` 改为 lstat 判断 socket 入口；symlink 被权限挡目标时，probe 如实报“socket 存在但当前进程无法访问”，不再误报“没有找到”。
- `ops/selftest.py` 新增 symlink/lstat 兼容用例；全部组自检通过。

## 结论
机器能力已落位；剩余是真实桌面环境验收，不能靠改代码冒充通过。
