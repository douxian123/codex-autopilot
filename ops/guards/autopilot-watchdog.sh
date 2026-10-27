#!/bin/bash
# 守护存活看门狗：launchd job 不在就重新 bootstrap。
# 只做一件事，失败不重试（交给 launchd 的 StartInterval）。
#
# 2026-10-27：这个脚本从 ~/.codex-guard/ 搬进版本目录 ops/guards/。
# 原因见 ops/guards/registry.json 的 _why：放在没有版本的位置上，它就是化石，
# 修好、发版、守护进程生效，但它自己永远不会被升级。搬进版本目录后，
# install.sh 每次安装会把 plist 改指到 releases/<version>/ops/guards/。
#
# 搬家带来的唯一真实副作用：下面 reap-codesign-clone.py 原来是绝对路径写死的
# ~/.codex-guard/reap-codesign-clone.py，搬走之后就找不到兄弟脚本了。所以改成
# 「先找自己所在目录，找不到再退回旧位置」——两段位置都能跑。
#
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG=/Users/danta/.codex-autopilot/watchdog.log
LABEL=com.codex-autopilot.daemon
PLIST=/Users/danta/Library/LaunchAgents/com.codex-autopilot.daemon.plist
UID_=$(id -u)

# 每次心跳先回收 Chrome/豆包 遗留的签名临时克隆（每个约 1.4GB，用完不删）。
# 2026-10-23 实测一天堆了 15GB，把可用空间从 7.6GB 压到 3.5GB，
# 触发守护的 4GB 派活下限，表现就是「机器跑不动」。
# 只删这些临时目录，不碰任何项目文件/分支/worktree；进程在用或太新的一律跳过。
REAP_CLONE="$SELF_DIR/reap-codesign-clone.py"
[ -f "$REAP_CLONE" ] || REAP_CLONE="/Users/danta/.codex-guard/reap-codesign-clone.py"
if [ -f "$REAP_CLONE" ]; then
  python3 "$REAP_CLONE" >> "$LOG" 2>&1
else
  # 找不到就记一行，不静默：否则「每次心跳都顺手回收」变成一句谎话。
  echo "$(date '+%F %T') 找不到 reap-codesign-clone.py（已查 $SELF_DIR 与旧位置），跳过该步" >> "$LOG"
fi

if launchctl print "gui/$UID_/$LABEL" >/dev/null 2>&1; then
  if launchctl print "gui/$UID_/$LABEL" 2>/dev/null | grep -q "pid = "; then
    echo "$(date '+%F %T') alive" >> "$LOG"
    exit 0
  fi
  echo "$(date '+%F %T') job loaded but no pid -> kickstart" >> "$LOG"
  launchctl kickstart -k "gui/$UID_/$LABEL" >> "$LOG" 2>&1
  exit 0
fi
echo "$(date '+%F %T') job missing -> bootstrap" >> "$LOG"
launchctl bootstrap "gui/$UID_" "$PLIST" >> "$LOG" 2>&1
launchctl enable "gui/$UID_/$LABEL" >> "$LOG" 2>&1
exit 0
