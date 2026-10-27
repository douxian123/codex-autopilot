#!/bin/bash
# Codex hooks 挂载看门狗 (2026-10-14)
#
# 背景:hooks.json 被反复清空成 {"hooks":{}} 共 5 次(10-10/10-11×2/10-12/10-14),
# 每次都是某个会话按旧纪律「hooks 全关」照做 → Stop 熔断摘掉 → 所有对话裸奔空转。
# 弹药源(PROGRESS.md 75 处)已解除,但为防再次回归,加这道自动看门狗。
#
# 设计红线(用户要求,2026-09-29/10-07 重申):
#   · fail-open:自身任何异常都不影响任何服务,绝不阻断
#   · 一键关闭:存在 ~/.codex/hooks/DISABLED 或 CODEX_LOOP_GUARD=off → 本脚本立即不动作
#   · 只做「恢复挂载」这一件事,不碰 provider/模型/会话/其它文件
#   · 恢复前先备份当时的坏文件,留痕可回溯
#
# 部署:launchd com.danta.codex-hooks-guard,每 15 分钟一次

set -u
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

HOOKS_JSON="$HOME/.codex/hooks.json"
CANON="$HOME/.codex/hooks.json.bak-20261009-impl"   # 权威好版本(md5 一致的三份备份之一)
LOG="$HOME/.codex/hooks/hooks-guard.log"

log() { mkdir -p "$(dirname "$LOG")" 2>/dev/null; echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null || true; }

# ---- 一键关闭(用户明确要求) ----
if [ -e "$HOME/.codex/hooks/DISABLED" ]; then exit 0; fi
if [ "${CODEX_LOOP_GUARD:-}" = "off" ]; then exit 0; fi

# ---- 判定:文件存在、是有效 JSON、且 hooks 非空 ----
# ⚠️ 必须自包含判定:不能外包给 check-guard-mounted.sh —— 那个脚本用绝对 ~/.codex
# 路径,在隔离 HOME 下会误判真实文件(2026-10-14 验证时实测踩到)。
# 返回 0 = 挂载正常;1 = 需要恢复
need_restore=0
if [ ! -s "$HOOKS_JSON" ]; then
  need_restore=1
else
  /usr/bin/python3 - "$HOOKS_JSON" <<'PYEOF' >/dev/null 2>&1 || need_restore=1
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
except Exception:
    raise SystemExit(1)          # 解析失败 = 坏文件,需要恢复
h = d.get("hooks") if isinstance(d, dict) else None
raise SystemExit(0 if (isinstance(h, dict) and h) else 1)
PYEOF
fi

[ "$need_restore" = "1" ] || exit 0

# ---- 权威版本不在 → fail-open,绝不猜、绝不用别的东西顶上 ----
if [ ! -s "$CANON" ]; then
  log "未挂载但权威备份缺失,跳过(fail-open)"
  exit 0
fi

TS=$(date +%Y%m%d-%H%M%S)
cp "$HOOKS_JSON" "$HOME/.codex/hooks.json.bak-$TS-guard-restore" 2>/dev/null || true
if cp "$CANON" "$HOOKS_JSON" 2>/dev/null; then
  log "已恢复 hooks.json 挂载(旧件存 hooks.json.bak-$TS-guard-restore)"
  osascript -e 'display notification "Codex 循环熔断器被清空,已自动恢复挂载" with title "hooks-guard"' 2>/dev/null || true
else
  log "恢复失败(fail-open,不影响其它服务)"
fi
exit 0
