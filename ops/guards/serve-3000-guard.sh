#!/usr/bin/env bash
#
# 本机 3000/3001 常驻服务的守护脚本。
#
# 为什么需要它
# ------------
# 2026-10-25 那轮排查发现「时能打开、时打不开」有三层原因：
#   1. 服务曾经跑在 kg-serve-main，而那个目录会被自动挡/别的任务例行
#      `git fetch + checkout origin/main`（reflog 显示约每 40 分钟切一次），
#      每次切分支都触发 Vite 重载 + 后端重编译，那 10~40 秒里 3000 直接拒绝连接。
#      修法是把服务搬进不含 .git 的固定快照目录 kg-live-3000。
#   2. 服务进程本身可能死掉/卡死，而 pm2 的 autorestart 只在进程退出时生效，
#      进程活着但不监听端口时它不管。
#   3. 有人（人或 agent）可能把 pm2 又指回 kg-serve-main，于是第 1 条复活。
#
# 这个脚本只做三件事，且全部是可重复、非破坏的：
#   - 探活 3000 / 3001；
#   - 核对监听 3000 的进程 cwd 是不是 kg-live-3000；
#   - 核对 pm2 重启计数有没有异常增长（崩溃循环）。
# 只有当「服务确实不可用」时才重启，并把一行证据写进 ops/truth/stability-alerts.jsonl。
# 它不碰代码、不碰数据库、不切换分支。
#
# 用法
# ----
#   ./scripts/serve-3000-guard.sh              # 巡检一次，只在坏的时候动手
#   ./scripts/serve-3000-guard.sh --dry-run    # 只报告，不重启、不写台账
#
# 退出码：0 = 正常或已自愈；1 = 异常且需要人看。
#
# 定时：见 .serve-note.md 里的 launchd 段。

set -uo pipefail

FRONT_PORT="${KG_GUARD_FRONT_PORT:-3000}"
BACK_PORT="${KG_GUARD_BACK_PORT:-3001}"

# 常驻服务目录必须不含 .git；被自动挡切分支的目录不能拿来跑服务。
EXPECTED_ROOT="${KG_GUARD_EXPECTED_ROOT:-/Users/danta/.newmax/workspace/kg-live-3000}"

# 告警台账：与既有 stability-alerts.jsonl 同构，追加写。
ALERT_FILE="${KG_GUARD_ALERT_FILE:-/Users/danta/.newmax/workspace/kg-trunk/ops/truth/stability-alerts.jsonl}"

# 重启冷却：刚重启过就不要连环重启，否则会把「后端正在启动」误判成「死透」。
COOLDOWN_SECONDS="${KG_GUARD_COOLDOWN_SECONDS:-180}"
STATE_FILE="${KG_GUARD_STATE_FILE:-$HOME/.pm2/kg-serve-3000-guard.state}"

# 处置提示里要给出「照着就能跑」的绝对路径。
#
# 为什么不能用相对路径：本脚本由 launchd 拉起，工作目录不是仓库；而且它真正的
# 执行副本在 $HOME/.kg-guard/（plist 直接指向那里），那个目录里没有 scripts/。
# 2026-10-26 就发生过：脚本提示「跑 scripts/sync-live-3000.sh origin/main」，
# 用户/下一个 agent 照着跑直接 No such file or directory —— 自救指南本身是死的。
# 唯一可靠的锚点是脚本自己所在目录：同步脚本与它一起部署、一起更新。
GUARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_SCRIPT="$GUARD_DIR/sync-live-3000.sh"

# 同步脚本缺失时，提示要说明这件事本身，而不是给出一条跑不通的命令。
sync_hint() {
  if [[ -x "$SYNC_SCRIPT" ]]; then
    say "  $SYNC_SCRIPT $*"
  else
    say "  同步脚本不在 ${SYNC_SCRIPT}（部署副本与守护脚本应是同目录兄弟文件）。"
    say "  先重建它：cd /Users/danta/.newmax/workspace/knowledge-graph && git show origin/main:scripts/sync-live-3000.sh > \"$SYNC_SCRIPT\" && chmod +x \"$SYNC_SCRIPT\""
  fi
}

# 崩溃循环判定：单次巡检比上次多出这么多重启，就认定在循环里。
RESTART_JUMP_LIMIT="${KG_GUARD_RESTART_JUMP_LIMIT:-5}"

# 重启动作本身可替换，方便在不打扰真实服务的前提下测这条路径。
RESTART_CMD="${KG_GUARD_RESTART_CMD:-pm2 restart kg-backend kg-frontend}"

# 磁盘余量告警阈值（MB）。低于它，vite 写缓存、sqlite 写库会开始随机失败。
DISK_MIN_MB="${KG_GUARD_DISK_MIN_MB:-1024}"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
now_epoch() { date +%s; }

say() { printf '%s\n' "$*"; }

# 只取 HTTP 状态码；连不上时 curl 会给 000，正好等价于「不可用」。
# 探活要区分三件事，它们的处置完全不同：
#   ok        200，服务正常
#   slow      连上了但没在超时内答完（curl exit 28）
#   refused   连不上 / 端口没人监听（curl exit 7）
# 为什么必须区分：这台机器并行跑着十几个任务，实测负载能到 42、3000 的
# 首字节要 18.6 秒。把「慢」当成「死」去 pm2 restart，会在用户正在跑研究流
# 的时候把后端掐掉——那才是真正的「老打不开」。
PROBE_TIMEOUT="${KG_GUARD_PROBE_TIMEOUT:-15}"

probe() {
  local url="$1"
  local out code rc
  out="$(curl -s -o /dev/null -m "$PROBE_TIMEOUT" -w '%{http_code}' "$url" 2>/dev/null)"
  rc=$?
  code="${out:-000}"
  if [[ "$code" == "200" ]]; then
    printf 'ok %s' "$code"
  elif [[ "$rc" -eq 28 ]]; then
    printf 'slow %s' "$code"
  else
    printf 'refused %s' "$code"
  fi
}

listener_pid() {
  lsof -t -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -n 1
}

process_cwd() {
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

# `pm2 jlist` 在 pm2 守护进程版本落后于 CLI 时会先打几行
# "In-memory PM2 is out-of-date" 提示，再输出 JSON。直接喂给 jq 会解析失败，
# 所以从第一个 '[' 起截。一次巡检只真正调用一次，结果缓存在 PM2_JSON_CACHE 里。
# pm2 的绝对路径。launchd 里 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，
# 而本机 pm2 在 ~/.local/bin（launchd 下 command -v pm2 返回空）。
# 不解析它，pm2_json 会静默拿到空串，restarts 变 "?"，脚本仍报「结论：正常」——
# 那是假绿灯，比报警更糟（2026-10-26 实测踩到）。
resolve_pm2() {
  local c
  for c in \
    "${KG_GUARD_PM2:-}" \
    "/Users/danta/.local/bin/pm2" \
    "$HOME/.local/bin/pm2" \
    /opt/homebrew/bin/pm2 /usr/local/bin/pm2; do
    [[ -n "$c" && -x "$c" ]] && { printf '%s' "$c"; return 0; }
  done
  c="$(command -v pm2 2>/dev/null)" && [[ -n "$c" ]] && { printf '%s' "$c"; return 0; }
  # 最后试 nvm 安装目录
  c="$(ls -1 "$HOME"/.nvm/versions/node/*/bin/pm2 2>/dev/null | tail -n 1)"
  [[ -n "$c" && -x "$c" ]] && { printf '%s' "$c"; return 0; }
  return 1
}
PM2_BIN="$(resolve_pm2 || true)"

# pm2 是个 `#!/usr/bin/env node` 脚本，光有 pm2 路径没有 node 照样跑不起来
# （报 "env: node: No such file or directory"）。launchd 的 PATH 里没有 nvm 目录，
# 所以必须把 node 所在目录也补进去。
# 2026-10-26 实测：缺这一步时 pm2 jlist 一直返回空，脚本把 restarts 打成 "?"，
# 却仍然输出「结论：正常」——「查不到」被当成了「没问题」，这是最危险的假绿灯。
if [[ -n "$PM2_BIN" ]]; then
  pm2_node_bin="$(sed -n '1s|^#!.*/env ||p' "$PM2_BIN" 2>/dev/null)"
  node_dir=""
  if [[ -n "$pm2_node_bin" ]] && command -v "$pm2_node_bin" >/dev/null 2>&1; then
    node_dir="$(cd "$(dirname "$(command -v "$pm2_node_bin")")" && pwd)"
  fi
  if [[ -z "$node_dir" ]]; then
    # pm2 是通过 nvm 装的，node 与它同目录
    node_dir="$(dirname "$PM2_BIN")"
  fi
  [[ -n "$node_dir" ]] && PATH="$node_dir:$PATH"
fi

PM2_JSON_CACHE=""
pm2_json() {
  if [[ -z "$PM2_JSON_CACHE" ]]; then
    [[ -n "$PM2_BIN" ]] || return 0
    PM2_JSON_CACHE="$("$PM2_BIN" jlist 2>/dev/null | sed -n '/^\[/,$p')"
  fi
  printf '%s' "$PM2_JSON_CACHE"
}

pm2_restarts() {
  pm2_json | jq -r --arg n "$1" \
    '.[] | select(.name == $n) | .pm2_env.restart_time' 2>/dev/null | head -n 1
}

pm2_status() {
  pm2_json | jq -r --arg n "$1" \
    '.[] | select(.name == $n) | .pm2_env.status' 2>/dev/null | head -n 1
}

# pm2 记录的"它亲手拉起的那个进程"的 pid。
pm2_pid() {
  pm2_json | jq -r --arg n "$1" \
    '.[] | select(.name == $n) | .pid' 2>/dev/null | head -n 1
}

# 某个 pid 的全部后代（含自己）。
#
# 为什么要算这个：`tsx` 这类 CLI 会 fork 一层子进程再去监听端口，
# 而 pm2 只认它自己拉起的那一个。判断「端口是不是 pm2 管的」时，
# 不能要求 listener pid 恰好等于 pm2 pid —— 那会把 tsx 这种正常分层误报成故障；
# 但也不能只看端口通不通 —— 那正是本次事故里"孤儿占着端口、pm2 反复重启失败
# 却一切显示正常"的原因。正确判据是：listener 必须是 pm2 那个进程的后代。
proc_descendants() {
  local root="$1"
  [[ -z "$root" ]] && return 0
  local frontier="$root" next="" out="$root" pid
  local seen=" $root "
  # 深度上限 5 层：进程树不会被我们自己造得比这更深，设上限是为了防止
  # 异常情况下（pid 被复用、环状 ppid）在这里死循环。
  local depth=0
  while [[ -n "$frontier" && "$depth" -lt 5 ]]; do
    next=""
    for pid in $frontier; do
      local kids
      kids="$(pgrep -P "$pid" 2>/dev/null | tr '\n' ' ')"
      for k in $kids; do
        case "$seen" in *" $k "*) continue ;; esac
        seen="$seen$k "
        out="$out $k"
        next="$next $k"
      done
    done
    frontier="$next"
    depth=$((depth + 1))
  done
  printf '%s' "$out"
}

pm2_cwd() {
  pm2_json | jq -r --arg n "$1" \
    '.[] | select(.name == $n) | .pm2_env.pm_cwd' 2>/dev/null | head -n 1
}

# 常驻目录的源码指纹是否还等于 .live-commit 记录的那次同步。
# 同步脚本会把 <sha>:.live-manifest 落到目录里；此处只比对「当前磁盘的源码清单」
# 与那份清单是否一致。不一致 = 被别的分支快照覆盖过。
live_source_drift() {
  # 比对关键入口文件的指纹。用「关键文件」而不是全树，是因为全树 2853 个文件
  # 算一遍要 30s，而本脚本每 60s 跑一次 —— 那种实现自己就会把机器拖慢。
  # 关键文件覆盖了「服务跑起来实际加载的那几处」：前端入口、历史同步队列、
  # 后端入口与主要 service。别的分支做过 rsync 覆盖，几乎必然改到其中至少一个。
  local root="${1:-$EXPECTED_ROOT}"
  local keyfile="$root/.live-keyfiles"
  [[ -f "$keyfile" ]] || return 1   # 没有指纹就无法判断，不误报

  # 指纹是哪次同步取的。与 .live-commit 不同 = 之后又正常同步过、只是指纹旧了。
  # 这种情况不是「被覆盖」，返回「无漂移」，避免误报（2026-10-26 实测误报过一次）。
  local baseline live_commit
  baseline="$(sed -n '1s/^# commit //p' "$keyfile")"
  live_commit="$(cat "$root/.live-commit" 2>/dev/null)"
  if [[ -n "$baseline" && -n "$live_commit" && "$baseline" != "$live_commit" ]]; then
    return 1
  fi
  local line path want have bad=0
  while IFS=' ' read -r want path; do
    [[ -z "${path:-}" ]] && continue
    [[ "$want" == \#* ]] && continue
    if [[ ! -f "$root/$path" ]]; then
      bad=1
      continue
    fi
    have="$(shasum "$root/$path" 2>/dev/null | awk '{print $1}')"
    [[ "$have" == "$want" ]] || bad=1
  done < "$keyfile"
  # 返回值语义：0 = 检测到漂移（像 grep 一样，非零表示「没命中」）。
  # 写成 return "$bad" 会把「没漂移」返回成 0，调用方反而当成有问题。
  [[ "$bad" -eq 1 ]]
}

# 上次重启时间（epoch）。没有记录时返回 0，表示「可以立刻重启」。
last_restart_epoch() {
  [[ -f "$STATE_FILE" ]] || { echo 0; return; }
  sed -n 's/^last_restart_epoch=//p' "$STATE_FILE" | head -n 1
}

# 上次巡检看到的重启计数。没有记录时返回空。
last_restart_count() {
  [[ -f "$STATE_FILE" ]] || return 0
  sed -n 's/^backend_restarts=//p' "$STATE_FILE" | head -n 1
}

save_state() {
  local restarts="$1" restarted="$2"
  [[ "$DRY_RUN" -eq 1 ]] && return 0
  mkdir -p "$(dirname "$STATE_FILE")"
  cat >"$STATE_FILE" <<EOF
backend_restarts=$restarts
last_restart_epoch=$restarted
updated=$(now_iso)
EOF
}

write_alert() {
  local code="$1" message="$2" command="$3" failures="$4"
  [[ "$DRY_RUN" -eq 1 ]] && { say "[dry-run] 本应写入告警 $code: $message"; return 0; }
  mkdir -p "$(dirname "$ALERT_FILE")"
  jq -nc \
    --arg alertId "serve-3000-guard-$(now_epoch)" \
    --arg createdAt "$(now_iso)" \
    --arg code "$code" \
    --arg message "$message" \
    --arg command "$command" \
    --arg baseUrl "http://localhost:$FRONT_PORT/" \
    --arg failures "$failures" \
    '{alertId:$alertId, createdAt:$createdAt, level:"error", code:$code, message:$message,
      command:$command, evidence:[], checkedAt:$createdAt, baseUrl:$baseUrl,
      failures:[$failures]}' >>"$ALERT_FILE"
}

restart_services() {
  local reason="$1"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] 本应重启 kg-backend kg-frontend（原因：${reason}）"
    return 0
  fi
  say "==> 重启 kg-backend kg-frontend（原因：${reason}）"
  # 重启命令的失败**不许吞掉**。
  #
  # 2026-10-27 实测：这里原来是 `eval "$RESTART_CMD" >/dev/null 2>&1 || true`。
  # 于是 pm2 restart 真失败（pm2 不在 PATH、EADDRINUSE、被 SIGKILL 打断）时，
  # 脚本什么都看不到：它照样等 30 秒，然后打印「重启后仍未恢复」，把
  # 「命令根本没跑起来」和「跑了但服务没起来」混成同一句话。
  # 巡检日志里 19:2x 那两轮就是这个形态（restarts 46→47→48 一路涨却始终
  # refused），最后靠人手动 pm2 restart 才恢复——这正是用户说的
  # 「老是跑着跑着又要清一轮」。
  local restart_out restart_rc
  restart_out="$(eval "$RESTART_CMD" 2>&1)"
  restart_rc=$?
  if [[ "$restart_rc" -ne 0 ]]; then
    say "==> 重启命令失败（exit=${restart_rc}）：$RESTART_CMD"
    # 只回显前 5 行：pm2 的报错头部就够定位，全量会把每 60s 的日志灌满。
    printf '%s\n' "$restart_out" | sed -n '1,5p' | sed 's/^/    /'
  fi
  save_state "$(pm2_restarts kg-backend)" "$(now_epoch)"
  # 给服务一点启动时间再判定，避免把「正在启动」报成新一轮故障。
  #
  # 等待窗口从 30s 加到 90s：2026-10-27 高负载下实测后端从 restart 到真正
  # listen 需要 20~25s，加上 pm2 先停旧进程的 kill_timeout(8s)，30s 窗口会
  # 刚好卡在边界上，把「正在启动」报成「重启后仍未恢复」并发告警。
  local budget="${KG_GUARD_RECOVERY_BUDGET:-90}"
  local waited=0
  while [[ "$waited" -lt "$budget" ]]; do
    sleep 3
    waited=$((waited + 3))
    if [[ "$(probe "http://127.0.0.1:$BACK_PORT/api/health")" == "ok 200" ]] \
      && [[ "$(probe "http://127.0.0.1:$FRONT_PORT/")" == "ok 200" ]]; then
      say "==> 已在 ${waited}s 内恢复"
      return 0
    fi
  done
  say "==> 重启后 ${budget}s 内仍未恢复（重启命令 exit=${restart_rc}）"
  # 失败时把冷却起点清零。
  #
  # 原来无论成败都写 now_epoch，于是一次没救活的重启会让接下来 180s 的巡检
  # 全部走「跳过重启：仍在冷却期内」——日志看起来每 60s 都在跑，实际上一分
  # 半钟里什么都没做。2026-10-27 19:2x 的形态就是这样：重启失败 -> 冷却 ->
  # 再失败 -> 再冷却，用户侧感知到的就是「卡住好几分钟，然后要清一轮」。
  # 没救活就不该占着冷却窗口，下一拍必须允许再试。
  save_state "$(pm2_restarts kg-backend)" 0
  if [[ "$restart_rc" -ne 0 ]]; then
    say "    先解决上面那条命令失败；否则下一轮巡检只会重复同一次失败。"
  else
    say "    命令本身成功但服务没起来：看 pm2 logs kg-backend --err（多半是启动期崩溃）。"
  fi
  return 1
}

problems=()

# --- 1. 探活 ---------------------------------------------------------------
# probe 输出形如 "ok 200" / "slow 000" / "refused 000"。
front_state="$(probe "http://127.0.0.1:$FRONT_PORT/")"
back_state="$(probe "http://127.0.0.1:$BACK_PORT/api/health")"
front_verdict="${front_state%% *}"
back_verdict="${back_state%% *}"
say "前端 $FRONT_PORT -> ${front_state}"
say "后端 $BACK_PORT -> ${back_state}"

# 只有「连不上」才算可用性故障。「慢」只报告，不重启 —— 重启解决不了负载，
# 只会把正在进行的请求打断。
[[ "$front_verdict" == "refused" ]] && problems+=("前端 / 完全连不上（${front_state}）")
[[ "$back_verdict" == "refused" ]] && problems+=("后端 /api/health 完全连不上（${back_state}）")
[[ "$front_verdict" == "slow" ]] && say "注意：前端有响应但 ${PROBE_TIMEOUT}s 内没答完（系统负载高，不是故障）"
[[ "$back_verdict" == "slow" ]] && say "注意：后端有响应但 ${PROBE_TIMEOUT}s 内没答完（系统负载高，不是故障）"

# --- 2. 服务目录必须是固定快照，不能是被切分支的 worktree -------------------
front_pid="$(listener_pid "$FRONT_PORT")"
if [[ -n "$front_pid" ]]; then
  front_cwd="$(process_cwd "$front_pid")"
  say "监听 $FRONT_PORT 的进程 $front_pid cwd = $front_cwd"
  case "$front_cwd" in
    "$EXPECTED_ROOT"/*) ;;
    *) problems+=("监听 $FRONT_PORT 的进程跑在 ${front_cwd}，不在 ${EXPECTED_ROOT}（会被别人切分支打断）") ;;
  esac
else
  problems+=("$FRONT_PORT 上没有监听进程")
fi

# --- 2.5 端口必须由 pm2 管得住的那个进程在监听 -----------------------------
# 这是 2026-10-26 那轮"时能打开时打不开"的**真正主因**，之前一直没被发现。
#
# 故障形态（实测证据）：
#   pm2 记录的 kg-backend pid = 72413，它 restart 了 56 次；
#   而真正监听 3001 的是一个 PPID=1 的孤儿进程 223，它的 pm_pid_path 仍写着
#   ~/.pm2/pids/kg-backend-0.pid —— 也就是说它是 kg-backend 的 tsx 孙进程，
#   包装器死后被 launchd 收养，从此没人管。
#   新实例每次启动都报 `listen EADDRINUSE: address already in use :::3001`
#   （日志里 13 次），pm2 于是不停重启，用户每次看到的都是同一份旧代码。
#
# 只探活端口发现不了这个 —— 孤儿进程答得挺好，200 且很快。所以必须比进程关系：
#   端口 listener 必须落在 pm2 那个 pid 的后代集合里。
# 允许"后代"而不是"相等"，是因为 tsx 这类加载器合法地会多一层子进程
# （改 ecosystem 去掉这层是另一件事，不能靠这条检查兜底）。
say_pm2_ownership() {
  local port="$1" app="$2"
  local lpid root_pid tree
  lpid="$(listener_pid "$port")"
  root_pid="$(pm2_pid "$app")"
  [[ -z "$lpid" ]] && return 0        # 端口没人听，上面已经记过问题了
  # pm2 停着/读不到时 pid 可能是 0 或空，此时无从判断，交给"读不到 pm2 状态"那条
  if [[ -z "$root_pid" || ! "$root_pid" =~ ^[1-9][0-9]*$ ]]; then
    return 0
  fi
  tree="$(proc_descendants "$root_pid")"
  case " $tree " in
    *" $lpid "*) return 0 ;;
  esac
  problems+=("$port 由进程 ${lpid} 监听，但它不是 pm2 ${app}（pid=${root_pid}）的后代——这是没人管的孤儿进程占着端口，pm2 只会反复重启失败")
  return 1
}

say_pm2_ownership "$FRONT_PORT" kg-frontend || true
say_pm2_ownership "$BACK_PORT" kg-backend || true

for app in kg-backend kg-frontend; do
  app_cwd="$(pm2_cwd "$app")"
  case "$app_cwd" in
    "$EXPECTED_ROOT"/*) ;;
    "") ;;
    *) problems+=("pm2 $app 的 cwd 是 ${app_cwd}，已偏离 $EXPECTED_ROOT") ;;
  esac
done

# --- 3. 崩溃循环 -----------------------------------------------------------
cur_restarts="$(pm2_restarts kg-backend)"
prev_restarts="$(last_restart_count)"
# 首次运行没有上次记录（state 文件刚建、或字段写的是 0 初值），
# 此时 cur=31 会被算成「一次跳了 31 次」并误报崩溃循环。
# 只比两次真实记录之间的差：没有上一次记录就不判断。
if [[ -n "$cur_restarts" && -n "$prev_restarts" ]] && [[ "$cur_restarts" =~ ^[0-9]+$ ]] \
  && [[ "$prev_restarts" =~ ^[0-9]+$ ]] && [[ -f "$STATE_FILE" ]] \
  && [[ "$prev_restarts" != "0" ]]; then
  delta=$((cur_restarts - prev_restarts))
  if [[ "$delta" -ge "$RESTART_JUMP_LIMIT" ]]; then
    problems+=("kg-backend 两次巡检之间重启了 $delta 次，疑似崩溃循环")
  fi
fi
say "kg-backend status=$(pm2_status kg-backend) restarts=${cur_restarts:-?}（上次巡检 ${prev_restarts:-无记录}）"

# --- 3.5 常驻目录是否被别的任务覆盖 -------------------------------------
# 2026-10-26 实测：另一个并行任务把 /tmp 里自己的分支快照 rsync 进
# kg-live-3000，把这里的服务代码整体换成了它的分支，我的修复文件被删掉。
# 症状对用户是「刚才还在的功能又没了」，和「服务挂了」看起来一样。
# 这里比对关键入口文件指纹：.live-keyfiles 是同步时落下的，对不上就是被覆盖。
# 只比对 8 个入口文件而不是全树 —— 全树 2853 个文件算一遍要 30s，而本脚本
# 每 60s 跑一次，那种实现自己就会把机器拖慢。
drift=0
disk_low=0
if [[ "${#problems[@]}" -eq 0 ]] && live_source_drift; then
  drift=1
  problems+=("$EXPECTED_ROOT 的源码已偏离 .live-commit=$(cat "$EXPECTED_ROOT/.live-commit" 2>/dev/null)：被别的任务的分支快照覆盖了")
fi

# --- 3.55 常驻目录是否落后于 origin/main -------------------------------
# 前一条 3.5 只回答「有没有被别人覆盖」，回答不了更常见的那件事：
# **它压根没同步过最新主线**。
#
# 2026-10-27 实测就是这种形态：常驻目录 .live-commit=ed66200cba，而
# origin/main 已经到了 7c2483056c，中间隔着 11 个提交，其中 stream.ts +199 行、
# aiService.ts +208 行 —— 用户打开侧栏用的还是旧实现，然后反馈「改了怎么没反应」。
# 端口 200、巡检「结论：正常」，两条都没错，因为它们量的不是这件事。
#
# 这里只做**读数**，不自动同步：同步要重启服务、会打断正在跑的 SSE 流，
# 那是人的决定（也可能是 next 定时任务的事），守护脚本不该擅自做。
# 所以它只加一行「落后 N 个提交」，让「服务健康但代码是旧的」这件事
# 在日志里有一句话，而不是要靠人想起来去比对。
LIVE_REPO="${KG_GUARD_REPO:-/Users/danta/.newmax/workspace/knowledge-graph}"
live_lag=""
if [[ -f "$EXPECTED_ROOT/.live-commit" ]] && [[ -d "$LIVE_REPO/.git" ]]; then
  live_sha="$(cat "$EXPECTED_ROOT/.live-commit" 2>/dev/null)"
  if [[ -n "$live_sha" ]]; then
    if main_sha="$(git -C "$LIVE_REPO" rev-parse origin/main 2>/dev/null)" && [[ -n "$main_sha" ]]; then
      if [[ "$live_sha" != "$main_sha" ]]; then
        if lag="$(git -C "$LIVE_REPO" rev-list --count "$live_sha..$main_sha" 2>/dev/null)" \
          && [[ "$lag" =~ ^[0-9]+$ ]] && [[ "$lag" -gt 0 ]]; then
          live_lag="$lag"
          say "常驻目录落后 origin/main ${lag} 个提交（.live-commit=${live_sha:0:10} / origin/main=${main_sha:0:10}）"
        fi
      else
        say "常驻目录已是最新 origin/main=${main_sha:0:10}"
      fi
    fi
  fi
fi

# --- 3.6 磁盘余量 -------------------------------------------------------
# 磁盘写满时 vite 写不了缓存、sqlite 写不了库，表现是「时能打开时打不开」。
# 2026-10-26 实测本机可用空间只剩 306MB。
avail_kb="$(df -Pk "$EXPECTED_ROOT" 2>/dev/null | awk 'NR==2{print $4}')"
if [[ "$avail_kb" =~ ^[0-9]+$ ]]; then
  avail_mb=$((avail_kb / 1024))
  say "磁盘可用 = ${avail_mb}MB（告警阈值 ${DISK_MIN_MB}MB）"
  if [[ "$avail_mb" -lt "$DISK_MIN_MB" ]]; then
    disk_low=1
    problems+=("磁盘可用只剩 ${avail_mb}MB，低于 ${DISK_MIN_MB}MB：写缓存/写库会随机失败")
  fi
fi

# --- 4. 处置 ---------------------------------------------------------------
# 读不到 pm2 就无法判断崩溃循环与进程存活。此时报「正常」是假绿灯，
# 按「无法确认 = 有问题」处理，并且不给重启建议（盲目重启没用）。
# 注意：pm2 在找不到进程表时会吐 `[]`，那不是空串，`-z` 判断不出来。
# 要守的是「kg-backend 这条记录在不在」，所以查 status 是否为空。
# 2026-10-26 实测：HOME 指到空目录时 pm2 返回 []，旧判断照样报「结论：正常」。
if [[ -z "$PM2_BIN" || -z "$(pm2_status kg-backend)" ]]; then
  problems+=("读不到 kg-backend 的 pm2 状态（pm2 或 node 不在 PATH、或进程表为空），无法确认服务是否在线")
fi

if [[ "${#problems[@]}" -eq 0 ]]; then
  save_state "${cur_restarts:-0}" "$(last_restart_epoch)"
  # 「服务活着」和「跑的是最新代码」是两件事，别用一句话盖住。
  # 落后不算故障（不告警、不重启），但必须在结论里说出来，否则
  # 「结论：正常」会被读成「一切都没问题」——而用户看到的界面其实是旧的。
  if [[ -n "$live_lag" ]]; then
    say "结论：服务在线，但代码落后主线 ${live_lag} 个提交（跑的是 .live-commit=$(cat "$EXPECTED_ROOT/.live-commit" 2>/dev/null | cut -c1-10)）"
    sync_hint origin/main
    say "  上面这条会重启服务（打断正在跑的流），建议没人在用时再跑。"
  else
    say "结论：正常"
  fi
  exit 0
fi

say "发现问题 ${#problems[@]} 项："
for p in "${problems[@]}"; do say "  - $p"; done

joined="$(printf '%s；' "${problems[@]}")"
write_alert "serve_3000_unavailable" \
  "本机 3000/3001 常驻服务巡检失败：$joined" \
  "$0" "$joined"

# 只有「端口真的不通 / 进程不在了」才重启；纯粹目录偏航不重启，
# 因为重启解决不了它，得改 pm2 配置 —— 那种情况必须人看。
need_restart=0
[[ "$front_verdict" == "refused" || "$back_verdict" == "refused" ]] && need_restart=1

# 孤儿占端口时先清孤儿，再重启。
#
# 顺序不能反：端口被孤儿占着的时候，pm2 restart 拉起来的新实例必然
# `listen EADDRINUSE` 失败，重启只是把 restart 计数往上堆（本机实测堆到 56 次
# 仍在失败）。先让端口空出来，重启才有意义。
#
# 清理必须精准，不能 pkill 一片：只杀「pid 等于监听该端口的那个进程，且它不在
# pm2 后代集合里」的单个进程。别的任务也在用 3xxx 端口，杀错就是删别人的工作。
reap_orphan_on_port() {
  local port="$1" app="$2"
  local lpid root_pid tree
  lpid="$(listener_pid "$port")"
  [[ -z "$lpid" ]] && { say "端口 $port 无人监听，无需清理"; return 0; }
  root_pid="$(pm2_pid "$app")"
  if [[ -n "$root_pid" && "$root_pid" =~ ^[1-9][0-9]*$ ]]; then
    tree="$(proc_descendants "$root_pid")"
    case " $tree " in
      *" $lpid "*) say "端口 $port 的监听者 ${lpid} 属于 pm2 ${app}，无需清理"; return 0 ;;
    esac
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] 本应清理占着 $port 的孤儿进程 ${lpid}（不属于 pm2 ${app}）"
    return 0
  fi
  say "==> 清理占着 $port 的孤儿进程 ${lpid}（不属于 pm2 ${app}，pm2 管不住它）"
  kill -TERM "$lpid" 2>/dev/null || true
  local waited=0
  while [[ "$waited" -lt 10 ]]; do
    sleep 1
    waited=$((waited + 1))
    [[ -z "$(listener_pid "$port")" ]] && { say "端口 $port 已空出（${waited}s）"; return 0; }
  done
  # 只对"就是我们确认过的那个 pid"补一次 KILL，不用通配。
  if [[ "$(listener_pid "$port")" == "$lpid" ]]; then
    say "==> 进程 ${lpid} 未响应 SIGTERM，发 SIGKILL"
    kill -KILL "$lpid" 2>/dev/null || true
    sleep 2
  fi
  [[ -z "$(listener_pid "$port")" ]] && return 0
  say "==> 端口 $port 仍被占用，清理失败"
  return 1
}

orphan_clean_needed=0
for pair in "$FRONT_PORT:kg-frontend" "$BACK_PORT:kg-backend"; do
  port="${pair%%:*}"; app="${pair##*:}"
  lpid="$(listener_pid "$port")"
  [[ -z "$lpid" ]] && continue
  root_pid="$(pm2_pid "$app")"
  [[ -z "$root_pid" || ! "$root_pid" =~ ^[1-9][0-9]*$ ]] && continue
  tree="$(proc_descendants "$root_pid")"
  case " $tree " in *" $lpid "*) ;; *) orphan_clean_needed=1 ;; esac
done

if [[ "$orphan_clean_needed" -eq 1 ]]; then
  reclaimed=0
  [[ "$DRY_RUN" -eq 0 ]] && reclaimed=1
  reap_orphan_on_port "$FRONT_PORT" kg-frontend || true
  reap_orphan_on_port "$BACK_PORT" kg-backend || true
  # 端口清干净之后，必须重启才能让 pm2 把成功的实例接管回来。
  if [[ "$DRY_RUN" -eq 0 ]]; then
    if restart_services "清理孤儿进程后重新接管端口"; then
      exit 0
    fi
    exit 1
  fi
fi

if [[ "$need_restart" -eq 1 ]]; then
  since=$(( $(now_epoch) - $(last_restart_epoch) ))
  if [[ "$since" -lt "$COOLDOWN_SECONDS" ]]; then
    say "跳过重启：${since}s 前刚重启过，仍在 ${COOLDOWN_SECONDS}s 冷却期内"
    exit 1
  fi
  if restart_services "$joined"; then
    exit 0
  fi
  exit 1
fi

# 不同问题给不同的下一步。原来这里一律说「改 pm2 配置」，但磁盘满、
# 源码被覆盖这两类改 pm2 配置根本治不了，照着做只会白忙。
if [[ "$disk_low" -eq 1 ]]; then
  say "未重启：磁盘余量不足重启治不了。先清空间：pm2 flush、清 ~/Library/Caches 与 ~/Downloads，"
  say "  或先确认不是叠加了别的问题："
  sync_hint --check
fi
if [[ "$drift" -eq 1 ]]; then
  say "未重启：源码已被别的任务的分支快照覆盖，重启只会把它再加载一遍。"
  say "  复位方式："
  sync_hint origin/main
  say "  （把服务代码恢复到 .live-commit 记录的那份快照，并重新落指纹）"
fi
if [[ -z "$PM2_BIN" || -z "$(pm2_status kg-backend)" ]]; then
  say "未重启：读不到 pm2 状态，盲目重启没有意义。先确认 pm2 与 node 对 launchd 可见："
  say "  /Users/danta/.local/bin/pm2 jlist | head    （若报 env: node，则 node 不在 PATH）"
fi
if [[ "$disk_low" -eq 0 && "$drift" -eq 0 ]] \
   && [[ -n "$PM2_BIN" && -n "$(pm2_status kg-backend)" ]]; then
  say "未重启：这类问题重启解决不了，需要改 pm2 配置（把 cwd 指回 ${EXPECTED_ROOT}）"
fi
exit 1
