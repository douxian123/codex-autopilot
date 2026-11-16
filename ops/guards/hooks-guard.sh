#!/bin/bash
# Codex hooks 挂载看门狗 (2026-10-14)
#
# 背景:hooks.json 被反复清空成 {"hooks":{}} 共 5 次(10-10/10-11×2/10-12/10-14),
# 每次都是某个会话按旧纪律「hooks 全关」照做 → 所有对话裸奔空转。
# 弹药源(PROGRESS.md 75 处)已解除,但为防再次回归,加这道自动看门狗。
#
# 🚨 2026-11-16 政策变更:看门狗**不再**要求/恢复 Stop 事件。
# Stop 的 decision:block 会被 Codex 客户端落成 role=user 的 HookPrompt
# (桌面端渲染成「钩子反馈」卡片),把对话刷爆;`suppressOutput` 只压 stdout,
# 挡不住这条落盘。且在 Plan Mode 会话里该注入会让模型回「我不能改」直接停住,
# 等于制造「跑两句就停」。摘掉后改为守护另外三条 loop-guard 挂载
# (PreToolUse / PostToolUse / UserPromptSubmit) —— 覆盖面比只盯 Stop 更广。
# 回滚:把 hooks.json + CANON 的 Stop 段加回,并恢复本文件的历史判据
# (备份见同目录 hooks-guard.sh.bak-20261116-* )。
#
# 设计红线(用户要求,2026-09-29/10-07 重申):
#   · fail-open:自身任何异常都不影响任何服务,绝不阻断
#   · 一键关闭:存在 ~/.codex/hooks/DISABLED 或 CODEX_LOOP_GUARD=off → 本脚本立即不动作
#   · 只做两件事:恢复 hooks.json 挂载(三条 loop-guard 挂载齐)、补回丢失的执行体;
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

# ---- 判定:文件存在、合法 JSON、且 loop-guard 的三条真实挂载仍在位 ----
# ⚠️ 必须自包含判定:不能外包给 check-guard-mounted.sh —— 那个脚本用绝对 ~/.codex
# 路径,在隔离 HOME 下会误判真实文件(2026-10-14 验证时实测踩到)。
#
# 🚨 2026-11-16 政策变更:Stop 事件已**主动摘除**,本判定不再要求/恢复它。
# 原因:Stop 的 decision:block 会被 Codex 客户端落成 role=user 的 HookPrompt
# (桌面端渲染成「钩子反馈」卡片),把对话刷爆;实测 `suppressOutput` 只压 stdout,
# 挡不住这条落盘。且在 Plan Mode 会话里该注入会让模型回「我不能改」直接停住,
# 等于制造「跑两句就停」。
#
# ⚠️ 摘掉 Stop 后**不能**把判据退化成「hooks 非空」:本脚本第 30~34 行会先跑
# install-resource-hooks.py,它只要 hooks 是 dict 就往里追加 resource-admission
# 条目 → 即使 hooks.json 被清成 `{"hooks":{}}` 或只剩半截,installer 一跑 hooks
# 就变非空 → 「非空」判据恒真 → 看门狗永远绿。所以判据是:
# PreToolUse / PostToolUse / UserPromptSubmit 三条 loop-guard 挂载**都必须**在,
# 少任何一条就恢复。这比旧的「Stop 在不在」覆盖面更广。
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
# loop-guard 的非 Stop 挂载必须齐;Stop 已按 2026-11-16 政策主动摘除,不作要求。
for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit"):
    found = False
    for group in h.get(event) or []:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            if "loop-guard.py" in str(hook.get("command", "")):
                found = True
                break
        if found:
            break
    if not found:
        raise SystemExit(1)      # 缺 loop-guard 挂载 = 需要恢复
raise SystemExit(0)
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

# ---- 2026-11-16 政策不变量:现场不许再有 Stop 事件 ----
# 「不再要求 Stop」只保证看门狗**不会**把它装回来;它挡不住别人装:
# 任何安装脚本、旧版恢复链路、或人工编辑往 hooks.json 里写回 Stop,
# 「钩子反馈」卡片就会立刻复发 —— 这正是用户反复说「修了两天还没好」的形态。
# 所以这里把「Stop 不在现场」升级成不变量:只在确实存在 Stop 时才动文件,
# 且**只删 Stop 这一个 key**,其余挂载原样保留;写前备份、临时文件原子替换。
# 返回值:0=已摘除 1=本来就没有(什么都不做) 2=文件不可用(不碰,fail-open)。
if [ "$need_restore" = "0" ]; then
  # 不用 set +e/-e 包裹:本脚本只有 set -u,凭空加 errexit 会改变后面恢复路径
  # 的退出语义。用 || 捕获返回码即可。
  stop_unmount_rc=0
  /usr/bin/python3 - "$HOOKS_JSON" "$HOME" >/dev/null 2>&1 <<'PYEOF' || stop_unmount_rc=$?
import json, os, shutil, sys, tempfile, time

path, home = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
except Exception:
    raise SystemExit(2)          # 读不了就别动,交给上面的恢复判据处理
if not isinstance(d, dict):
    raise SystemExit(2)
h = d.get("hooks")
if not (isinstance(h, dict) and "Stop" in h):
    raise SystemExit(1)          # 本来就没有 Stop

stamp = time.strftime("%Y%m%d-%H%M%S")
shutil.copy2(path, os.path.join(home, ".codex", f"hooks.json.bak-{stamp}-stop-unmount"))
h.pop("Stop", None)
fd, tmp = tempfile.mkstemp(prefix=".hooks-json-", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)        # 原子替换:并发写入不会看到半截文件
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise SystemExit(2)
raise SystemExit(0)
PYEOF
  if [ "$stop_unmount_rc" = "0" ]; then
    log "现场 hooks.json 又出现了 Stop 事件,已主动摘除(旧件存 hooks.json.bak-*-stop-unmount)"
    osascript -e 'display notification "已摘除回归的 Stop 钩子(它会刷满钩子反馈卡片)" with title "hooks-guard"' 2>/dev/null || true
  fi
fi

[ "$need_restore" = "1" ] || exit 0

# ---- 权威版本不在 → fail-open,绝不猜、绝不用别的东西顶上 ----
if [ ! -s "$CANON" ]; then
  log "未挂载但权威备份缺失,跳过(fail-open)"
  exit 0
fi

# 🚨 2026-11-16:Stop 事件已主动摘除,但恢复判据**只换要求、不放松强度**。
# 原判据要求 CANON 含 Stop;Stop 摘除后改成要求 CANON 含另外三条 loop-guard
# 挂载(PreToolUse / PostToolUse / UserPromptSubmit)。这样既不会因缺 Stop 而
# 拒绝恢复(永远 fail-open),也不会照一份「只剩空壳」的备份覆盖现场。
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
for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit"):
    found = False
    for group in h.get(event) or []:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            if "loop-guard.py" in str(hook.get("command", "")):
                found = True
                break
        if found:
            break
    if not found:
        raise SystemExit(1)
raise SystemExit(0)
PYEOF
then
  log "权威备份缺 loop-guard 挂载或不是合法 JSON,拒绝照它恢复(fail-open,等待人工修 CANON)"
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
