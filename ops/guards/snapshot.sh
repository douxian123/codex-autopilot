#!/bin/bash
# Codex 会话快照保护 —— 历史优先、fail-open、可一键关
# 起因：2026-10-13 20:38 一次共享 CODEX_HOME 的启动把 ~/.codex/sessions 5 个月历史清空。
# 原理：用 APFS clone(cp -cR)保留文件的写时复制版本。文件被删除、截断或改写后，
#       旧版本仍在快照里；未改动的块由文件系统共享，不按逻辑大小重复占盘。
# fail-open：任何失败只写日志、以 0 退出，绝不影响 codex 本身。
# 一键关：launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.danta.codex-guard.plist
#         或 touch ~/.codex-guard/OFF  （脚本见此文件立即退出）

set -u
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="$HOME/.codex-guard"
SNAP="$GUARD/snapshots"
SRC="$HOME/.codex/sessions"
LOG="$GUARD/guard.log"
KEEP=48   # 至少保留最近 48 份；更老的仅在内容被新文件完整包含时才淘汰

[ -e "$GUARD/OFF" ] && exit 0
[ -d "$SRC" ] || exit 0

mkdir -p "$SNAP" 2>/dev/null || exit 0

TS=$(date +%Y%m%d-%H%M%S)
DEST="$SNAP/$TS"
if cp -cR "$SRC" "$DEST" 2>>"$LOG"; then
  echo "$(date '+%F %T') snapshot ok -> $DEST $(find "$DEST" -name '*.jsonl' | wc -l | tr -d ' ') files" >>"$LOG"
else
  echo "$(date '+%F %T') snapshot FAILED" >>"$LOG"
  rm -rf "$DEST" 2>/dev/null
fi

# 只淘汰“每个文件都已被当前 sessions 或更新快照完整包含”的旧目录。
# 删除、截断、改写过的内容都属于唯一历史，条件不满足时永久保留。
if [ -f "$SELF_DIR/snapshot_prune.py" ] && [ -x /usr/bin/python3 ]; then
  /usr/bin/python3 "$SELF_DIR/snapshot_prune.py" \
    --source "$SRC" --snapshots "$SNAP" --keep "$KEEP" >>"$LOG" 2>&1 || \
    echo "$(date '+%F %T') snapshot prune skipped (helper failed; no snapshots removed by shell)" >>"$LOG"
fi

# 日志滚动，避免无限增长
if [ -f "$LOG" ] && [ "$(wc -l <"$LOG" | tr -d ' ')" -gt 2000 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi

exit 0
