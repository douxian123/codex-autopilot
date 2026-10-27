#!/bin/bash
# Codex 会话快照保护 —— 只增不减、fail-open、可一键关
# 起因：2026-10-13 20:38 一次共享 CODEX_HOME 的启动把 ~/.codex/sessions 5 个月历史清空。
# 原理：用硬链接(cp -al)快照 sessions 目录。文件被删除时 inode 仍在快照里 → 内容可救。
#       硬链接几乎不占空间（只增加目录项）。
# fail-open：任何失败只写日志、以 0 退出，绝不影响 codex 本身。
# 一键关：launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.danta.codex-guard.plist
#         或 touch ~/.codex-guard/OFF  （脚本见此文件立即退出）

set -u
GUARD="$HOME/.codex-guard"
SNAP="$GUARD/snapshots"
SRC="$HOME/.codex/sessions"
LOG="$GUARD/guard.log"
KEEP=48   # 保留份数；每 30 分钟一次 ≈ 24 小时

[ -e "$GUARD/OFF" ] && exit 0
[ -d "$SRC" ] || exit 0

mkdir -p "$SNAP" 2>/dev/null || exit 0

TS=$(date +%Y%m%d-%H%M%S)
DEST="$SNAP/$TS"
if cp -al "$SRC" "$DEST" 2>>"$LOG"; then
  echo "$(date '+%F %T') snapshot ok -> $DEST $(find "$DEST" -name '*.jsonl' | wc -l | tr -d ' ') files" >>"$LOG"
else
  echo "$(date '+%F %T') snapshot FAILED" >>"$LOG"
  rm -rf "$DEST" 2>/dev/null
fi

# 清理超量快照（只删自己建的快照目录）
ls -1dt "$SNAP"/*/ 2>/dev/null | tail -n +$((KEEP+1)) | while read -r d; do
  case "$d" in "$SNAP"/*) rm -rf "$d" 2>/dev/null;; esac
done

# 日志滚动，避免无限增长
if [ -f "$LOG" ] && [ "$(wc -l <"$LOG" | tr -d ' ')" -gt 2000 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi

exit 0
