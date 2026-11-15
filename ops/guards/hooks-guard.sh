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
#   · 只做两件事:恢复 hooks.json 挂载、补回丢失的 Stop 执行体;
#     不碰 provider/模型/会话/其它文件,也绝不覆盖已存在的文件
#   · 恢复前先备份当时的坏文件,留痕可回溯
#
# 部署:launchd com.danta.codex-hooks-guard,每 15 分钟一次

set -u
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

HOOKS_JSON="$HOME/.codex/hooks.json"
CANON="$HOME/.codex/hooks.json.bak-20261009-impl"   # 权威好版本(md5 一致的三份备份之一)
LOG="$HOME/.codex/hooks/hooks-guard.log"
RESOURCE_INSTALLER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install-resource-hooks.py"
# Stop 熔断的真正执行体。它住在非版本化路径、由 Codex/launchd 直接调用,
# 一旦被删或截断成空,hooks.json 看上去仍然「正常」,熔断其实已经死了 ——
# 表现就是「吐两句话就停」。所以看门狗连它一起守:在位就留一份 last-known-good,
# 缺失才从 last-good 恢复(只补空档,绝不覆盖已存在的文件)。
LOOP_GUARD_TARGET="$HOME/.codex/hooks/loop-guard.py"
LOOP_GUARD_LASTGOOD="$HOME/.codex/hooks/loop-guard.py.last-good"
# 第三层恢复源(2026-11-15 18:4x 补):随发版走的那一份。修前只有「现场 + last-good」
# 两层,两份同时坏掉(或都在别处被清掉)就没有任何恢复源,只能 fail-open 干瞪眼 ——
# 表现仍是「吐两句话就停」,而且要等人再手工拷。它就在本脚本旁边,跟着 release 更新,
# 所以把它当兜底:last-good 缺了才用它,且用之前先自证(编译 + 含 stop)。
LOOP_GUARD_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/loop-guard.py"

log() { mkdir -p "$(dirname "$LOG")" 2>/dev/null; echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null || true; }

# ---- 一键关闭(用户明确要求) ----
if [ -e "$HOME/.codex/hooks/DISABLED" ]; then exit 0; fi
if [ "${CODEX_LOOP_GUARD:-}" = "off" ]; then exit 0; fi

# ---- 先确保资源准入是追加式挂载,不依赖会过期的权威备份 ----
if [ -x /usr/bin/python3 ] && [ -f "$RESOURCE_INSTALLER" ]; then
  /usr/bin/python3 "$RESOURCE_INSTALLER" >>"$LOG" 2>&1 || \
    log "资源准入挂载失败(fail-open,不阻断 Codex)"
fi

# ---- 判定:文件存在、是有效 JSON、hooks 非空、且 Stop 熔断仍挂在 loop-guard.py 上 ----
# ⚠️ 必须自包含判定:不能外包给 check-guard-mounted.sh —— 那个脚本用绝对 ~/.codex
# 路径,在隔离 HOME 下会误判真实文件(2026-10-14 验证时实测踩到)。
#
# 🚨 2026-11-15 17:10 修一个结构性盲区:本脚本第 30~34 行会先跑
# install-resource-hooks.py,它只要 hooks 是 dict 就会往里 setdefault 并追加
# resource-admission 条目。于是若 hooks.json 被写成「丢了 Stop」的版本,
# installer 一跑 hooks 就变非空 → 旧判定(`isinstance(h,dict) and h`)直接 PASS
# → 第 56 行 exit 0 → Stop 永远不会被恢复。这正是脚本开头注释里那 5 次
# 「Stop 熔断被摘掉、所有对话裸奔空转」的形态,只是丢了 Stop 而非整体清空。
# 因此判定必须显式要求 Stop 存在、且确实指向 loop-guard.py。
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
if not (isinstance(h, dict) and h):
    raise SystemExit(1)          # hooks 为空 = 需要恢复
# Stop 必须存在,且至少有一个条目挂着 loop-guard.py stop。
# 只认 command 字符串里的 "loop-guard.py" + "stop",不猜其它形态。
ok = False
for group in h.get("Stop") or []:
    if not isinstance(group, dict):
        continue
    for hook in group.get("hooks") or []:
        if not isinstance(hook, dict):
            continue
        cmd = str(hook.get("command", ""))
        if "loop-guard.py" in cmd and "stop" in cmd:
            ok = True
            break
    if ok:
        break
raise SystemExit(0 if ok else 1)
PYEOF
fi

# ---- 顺带守住 Stop 熔断的执行体(2026-11-15 18:xx 补的结构性口子) ----
# hooks.json 只是「挂载表」,真正拦停的是 loop-guard.py 这个文件。两者分离在
# 两个位置,看门狗此前只守前者:一旦 loop-guard.py 被删或截断成空,hooks.json
# 仍是完整 5 段、Stop 仍指向它 —— 判定全绿,熔断其实已经死了,表现就是
# 「吐两句话就停」。所以这里给它留一份 last-known-good:在位且能编译就刷新备份,
# 缺失/坏掉才从备份补回。只补空档,绝不覆盖已经存在的文件。
LOOP_GUARD_OK=0
if [ -s "$LOOP_GUARD_TARGET" ]; then
  if /usr/bin/python3 - "$LOOP_GUARD_TARGET" <<'PYEOF' >/dev/null 2>&1
import sys
src = open(sys.argv[1], encoding="utf-8").read()
compile(src, sys.argv[1], "exec")          # 语法坏掉 = 不算在位
if "stop" not in src:                      # 明显不是本脚本 = 不算在位
    raise SystemExit(1)
PYEOF
  then
    LOOP_GUARD_OK=1
    # 只在内容真的变了才重写备份,避免每 15 分钟无谓刷新 mtime。
    if ! cmp -s "$LOOP_GUARD_TARGET" "$LOOP_GUARD_LASTGOOD"; then
      cp "$LOOP_GUARD_TARGET" "$LOOP_GUARD_LASTGOOD" 2>/dev/null || true
    fi
  fi
fi
if [ "$LOOP_GUARD_OK" = "0" ]; then
  # 恢复源按可靠性排序:last-good(现场最后一份已知好件)→ 版本目录(随发版)。
  # 每个候选都必须先自证,绝不照坏件恢复。
  LOOP_GUARD_FIXED=0
  for CAND in "$LOOP_GUARD_LASTGOOD" "$LOOP_GUARD_SOURCE"; do
    [ -s "$CAND" ] || continue
    if ! /usr/bin/python3 - "$CAND" <<'PYEOF' >/dev/null 2>&1
import sys
src = open(sys.argv[1], encoding="utf-8").read()
compile(src, sys.argv[1], "exec")
if "stop" not in src:
    raise SystemExit(1)
PYEOF
    then
      continue
    fi
    if cp "$CAND" "$LOOP_GUARD_TARGET" 2>/dev/null; then
      chmod +x "$LOOP_GUARD_TARGET" 2>/dev/null || true
      log "已从 $CAND 补回 loop-guard.py 执行体"
      osascript -e 'display notification "Stop 熔断执行体缺失,已自动补回" with title "hooks-guard"' 2>/dev/null || true
      LOOP_GUARD_FIXED=1
      break
    fi
  done
  if [ "$LOOP_GUARD_FIXED" = "0" ]; then
    log "loop-guard.py 缺失且无可用恢复源(last-good/版本目录均不可用),跳过(fail-open,绝不猜内容)"
  fi
fi

[ "$need_restore" = "1" ] || exit 0

# ---- 权威版本不在 → fail-open,绝不猜、绝不用别的东西顶上 ----
if [ ! -s "$CANON" ]; then
  log "未挂载但权威备份缺失,跳过(fail-open)"
  exit 0
fi

# 🚨 2026-11-15 18:2x 补:权威备份自身也必须先自证。
# 修前只判 CANON「非空」,于是若 CANON 自己是被写坏的那一版(丢了 Stop),
# 守卫照样把坏文件盖回去,还记一条「已恢复挂载」—— 表面成功,熔断实际没回来,
# 而且要等下一次判定才可能再修,等于把复发通道留在恢复路径上。
# 所以拿同一套判据先验 CANON:不达标就拒绝恢复并明确记日志,绝不照坏件恢复。
if ! /usr/bin/python3 - "$CANON" <<'PYEOF' >/dev/null 2>&1
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
except Exception:
    raise SystemExit(1)
h = d.get("hooks") if isinstance(d, dict) else None
if not (isinstance(h, dict) and h):
    raise SystemExit(1)
ok = False
for group in h.get("Stop") or []:
    if not isinstance(group, dict):
        continue
    for hook in group.get("hooks") or []:
        if not isinstance(hook, dict):
            continue
        cmd = str(hook.get("command", ""))
        if "loop-guard.py" in cmd and "stop" in cmd:
            ok = True
            break
    if ok:
        break
raise SystemExit(0 if ok else 1)
PYEOF
then
  log "权威备份本身不含 Stop 熔断,拒绝照它恢复(fail-open,等待人工修 CANON)"
  osascript -e 'display notification "权威备份已损坏,未自动恢复,请人工检查 hooks.json" with title "hooks-guard"' 2>/dev/null || true
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
