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
KEEP=3    # 保留最近 3 个有效快照；更老的按文件级历史保护规则淘汰
MIN_FREE_GIB="${MIN_FREE_GIB:-20}"   # 工作盘低于此值时停止新建保护快照
LOCK="$GUARD/.snapshot.lock"

[ -e "$GUARD/OFF" ] && exit 0
[ -d "$SRC" ] || exit 0

mkdir -p "$SNAP" 2>/dev/null || exit 0

# 单实例运行，避免 launchd 与手动测试同时改写/淘汰快照。
if ! mkdir "$LOCK" 2>/dev/null; then
  if find "$LOCK" -prune -mmin +120 -print 2>/dev/null | grep -q .; then
    rm -r "$LOCK" 2>/dev/null || exit 0
    mkdir "$LOCK" 2>/dev/null || exit 0
  else
    echo "$(date '+%F %T') snapshot skipped: another run is active" >>"$LOG"
    exit 0
  fi
fi
trap 'rm -r "$LOCK" 2>/dev/null' EXIT

# 先淘汰“每个文件都已被当前 sessions 或更新快照完整包含”的旧目录。
# 删除、截断、改写过的内容都属于唯一历史，条件不满足时永久保留。
if [ -f "$SELF_DIR/snapshot_prune.py" ] && [ -x /usr/bin/python3 ]; then
  /usr/bin/python3 "$SELF_DIR/snapshot_prune.py" \
    --source "$SRC" --snapshots "$SNAP" --keep "$KEEP" >>"$LOG" 2>&1 || \
    echo "$(date '+%F %T') snapshot prune skipped (helper failed; no snapshots removed by shell)" >>"$LOG"
fi

free_gib=$(df -Pk "$GUARD" 2>/dev/null | awk 'NR==2 {printf "%.0f", $4 / 1048576}')
[ -n "${free_gib:-}" ] || free_gib=0
if [ "$free_gib" -lt "$MIN_FREE_GIB" ]; then
  echo "$(date '+%F %T') snapshot skipped: free=${free_gib}GiB threshold=${MIN_FREE_GIB}GiB" >>"$LOG"
  exit 0
fi

TS=$(date +%Y%m%d-%H%M%S)
FINAL="$SNAP/$TS"
PARTIAL="$SNAP/.${TS}.partial.$$"
if cp -cR "$SRC" "$PARTIAL" 2>>"$LOG" && mv "$PARTIAL" "$FINAL"; then
  echo "$(date '+%F %T') snapshot ok -> $FINAL $(find "$FINAL" -name '*.jsonl' | wc -l | tr -d ' ') files" >>"$LOG"
else
  echo "$(date '+%F %T') snapshot FAILED" >>"$LOG"
  rm -r "$PARTIAL" 2>/dev/null
fi

# 日志滚动，避免无限增长
if [ -f "$LOG" ] && [ "$(wc -l <"$LOG" | tr -d ' ')" -gt 2000 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi

exit 0
