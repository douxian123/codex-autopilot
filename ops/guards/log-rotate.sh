#!/bin/bash
# proxy.log 轮转(fail-open,幂等,绝不重启代理)
#
# 为什么需要(2026-10-11 实锤):
#   proxy.log 无轮转,涨到 186MB / 340MB 每小时 → Data 卷只剩 4GB →
#   Python 写日志 flush 失败 → **代理进程静默猝死** →
#   health-guard 每 3 分钟拉起一次 → Codex 报
#   "Connection failed: error sending request"(3 个会话各重试 10~14 次)。
#   根因不是网络,是磁盘。这个脚本把日志压到可长期存活的大小。
#
# 用法:cron/launchd 每 10 分钟跑一次,或手工执行。
#   MAX_BYTES  超过就轮转(默认 32MB)
#   KEEP       保留最近几个归档(默认 5,更早的删掉)
# 注意:用截断(: >)而不是 mv —— mv 后正在写的老句柄会继续写旧 inode,
#       磁盘不会释放(实测白干一次)。截断让句柄指向同一个 inode,立即回收。
set -u
LOG="$HOME/.codex-proxy/proxy.log"
ARCHIVE="$HOME/.codex-proxy/log-archive"
MAX_BYTES=${MAX_BYTES:-33554432}
KEEP=${KEEP:-5}

[ -f "$LOG" ] || exit 0
SIZE=$(stat -f%z "$LOG" 2>/dev/null || echo 0)
[ "$SIZE" -gt "$MAX_BYTES" ] || exit 0

mkdir -p "$ARCHIVE" || exit 0
TS=$(date +%Y%m%d-%H%M%S)
cp "$LOG" "$ARCHIVE/proxy.log.$TS" 2>/dev/null || exit 0
: > "$LOG"                                     # 截断,老句柄继续有效
gzip -9 "$ARCHIVE/proxy.log.$TS" 2>/dev/null    # 失败就留着裸文件,不删

# 清理超额归档(只删自己生成的)
ls -1t "$ARCHIVE"/proxy.log.*.gz 2>/dev/null | tail -n +$((KEEP+1)) | while read -r f; do rm -f "$f"; done
echo "$(date '+%Y-%m-%d %H:%M:%S') rotated ${SIZE} bytes -> $ARCHIVE/proxy.log.$TS.gz"
