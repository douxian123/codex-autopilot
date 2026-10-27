#!/usr/bin/env bash
# 把 origin/main（或指定 commit）的源码快照同步到 3000/3001 常驻服务目录。
#
# 为什么需要这个脚本：
#   常驻目录不含 .git，所以「同步」这一步以前是手打的 rsync，谁都能顺手往里
#   rsync 自己的分支快照。2026-10-26 就发生过：另一个任务把它自己的分支
#   rsync 进来，常驻服务代码被整体换掉、修复文件被删，用户看到的是
#   「刚才还在的功能又没了」。
#
#   这里把「同步 + 落指纹 + 重启 + 验证」固化成一条命令，指纹（.live-commit
#   和 .live-manifest）供守护脚本 serve-3000-guard.sh 比对，覆盖就会被发现。
#
# 用法：
#   ./scripts/sync-live-3000.sh              # 同步 origin/main
#   ./scripts/sync-live-3000.sh <commit>     # 同步指定 commit
#   ./scripts/sync-live-3000.sh --check      # 只检查当前是否漂移，不动文件
set -uo pipefail

LIVE_ROOT="${KG_LIVE_ROOT:-/Users/danta/.newmax/workspace/kg-live-3000}"
REPO="${KG_REPO:-/Users/danta/.newmax/workspace/knowledge-graph}"
TARGET="${1:-origin/main}"
CHECK_ONLY=0
[[ "$TARGET" == "--check" ]] && CHECK_ONLY=1

say() { printf '%s\n' "$*"; }

manifest_of() {
  # 对一棵源码树算「文件 -> sha1」清单；排除 .git / node_modules / 数据库 / 构建产物。
  local root="$1"
  ( cd "$root" && find . -type f \
      -not -path './.git/*' -not -path './node_modules/*' \
      -not -path './backend/node_modules/*' -not -path './frontend/node_modules/*' \
      -not -path './backend/data/*' -not -path './frontend/dist/*' \
      -not -name '*.log' -not -name '.DS_Store' \
      -not -name '.live-manifest' -not -name '.live-commit' \
      | sort | while read -r f; do printf '%s %s\n' "$(shasum "$f" | awk '{print $1}')" "$f"; done )
}

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  if [[ ! -f "$LIVE_ROOT/.live-keyfiles" ]]; then
    say "没有 .live-keyfiles，无法判断（先用本脚本同步一次）"
    exit 2
  fi
  KEYCHECK_OK=1
  while IFS=' ' read -r want path; do
    [[ -z "${path:-}" ]] && continue
    [[ "$want" == \#* ]] && continue   # 首行是基线 commit，不是文件指纹
    have="$(shasum "$LIVE_ROOT/$path" 2>/dev/null | awk '{print $1}')"
    [[ "$have" == "$want" ]] || KEYCHECK_OK=0
  done < "$LIVE_ROOT/.live-keyfiles"
  if [[ "$KEYCHECK_OK" -eq 1 ]]; then
    say "未漂移：常驻目录仍等于 .live-commit=$(cat "$LIVE_ROOT/.live-commit" 2>/dev/null)"
    exit 0
  fi
  say "已漂移：常驻目录源码与 .live-commit=$(cat "$LIVE_ROOT/.live-commit" 2>/dev/null) 不一致"
  say "（很可能是别的任务把自己的分支快照 rsync 进来了）"
  exit 1
fi

SHA="$(git -C "$REPO" rev-parse "$TARGET" 2>/dev/null)" || { say "取不到 $TARGET 的 commit"; exit 2; }
say "目标 commit = $SHA ($(git -C "$REPO" log -1 --format='%s' "$SHA"))"

TMP="$(mktemp -d "/tmp/kg-live-sync.XXXXXX")" || exit 2
trap 'find "$TMP" -depth -type f -delete 2>/dev/null; find "$TMP" -depth -type d -empty -delete 2>/dev/null' EXIT

git -C "$REPO" archive "$SHA" | tar -x -C "$TMP" || { say "导出 $SHA 失败"; exit 2; }
say "已导出 $(find "$TMP" -type f | wc -l | tr -d ' ') 个文件"

# 不删任何东西：只覆盖同名源码文件。`.serve-note.md`、软链的 data 目录、
# 后端 .env 都不会被碰。
rsync -a --exclude='.git' "$TMP"/ "$LIVE_ROOT"/ || { say "rsync 失败"; exit 2; }

printf '%s' "$SHA" > "$LIVE_ROOT/.live-commit"
manifest_of "$LIVE_ROOT" > "$LIVE_ROOT/.live-manifest"

# 关键入口指纹：守护脚本每 60s 只比对这几个，避免全树 2853 个文件算 30s。
# 选它们的理由：前端主入口与服务层、后端启动入口与核心 service，任何一次
# 「别的分支 rsync 覆盖」都几乎必然改到其中至少一个。
KEYFILES=(
  frontend/src/App.tsx
  frontend/src/main.tsx
  frontend/src/services/librarySyncQueue.ts
  frontend/src/types/graph.ts
  backend/src/index.ts
  backend/src/services/materialUnderstandingService.ts
  backend/package.json
  frontend/package.json
)
# 第一行记下这份指纹是在哪个 commit 上取的。守护脚本靠它区分两件完全不同的事：
#   - .live-commit 与基线一致、但文件对不上 → 目录被别的分支快照覆盖了（要告警）
#   - .live-commit 与基线不一致           → 之后又正常同步过一次，指纹只是旧了（不该告警）
# 没有这行基线，第二种情况会被误报成「被覆盖」。
printf '# commit %s\n' "$SHA" > "$LIVE_ROOT/.live-keyfiles"
for f in "${KEYFILES[@]}"; do
  [[ -f "$LIVE_ROOT/$f" ]] || continue
  printf '%s %s\n' "$(shasum "$LIVE_ROOT/$f" | awk '{print $1}')" "$f" >> "$LIVE_ROOT/.live-keyfiles"
done
say "关键文件指纹 $(wc -l < "$LIVE_ROOT/.live-keyfiles" | tr -d ' ') 条：.live-keyfiles"
say "指纹已落盘：.live-commit=$(cat "$LIVE_ROOT/.live-commit")"

if [[ "${KG_SYNC_NO_RESTART:-0}" == "1" ]]; then
  say "跳过重启（KG_SYNC_NO_RESTART=1）"
  exit 0
fi

# pm2 是 #!/usr/bin/env node 脚本，且本机装在 ~/.local/bin、node 在 nvm 目录。
# 在非登录 shell 里 PATH 可能都没有这两处，直接 `pm2 restart` 会失败。
# 先把它们补进来（与 serve-3000-guard.sh 的 resolve_pm2 同思路）。
PM2_BIN=""
for c in "${KG_PM2_BIN:-}" "$HOME/.local/bin/pm2" /Users/danta/.local/bin/pm2 \
         /opt/homebrew/bin/pm2 /usr/local/bin/pm2; do
  [[ -n "$c" && -x "$c" ]] && { PM2_BIN="$c"; break; }
done
[[ -z "$PM2_BIN" ]] && PM2_BIN="$(command -v pm2 2>/dev/null || true)"
if [[ -n "$PM2_BIN" ]]; then
  PATH="$(dirname "$PM2_BIN"):$PATH"
fi

# 占着端口、却不属于 pm2 记录进程的孤儿。
#
# 为什么同步脚本也要管这个：`pm2 restart` 只会重载进程定义，**不会**把已经
# 跑到外面的孙进程收回来。而 tsx 那套写法（`tsx watch src/index.ts`）恰好会
# fork 出这样一层：pm2 杀包装器，孙进程被 launchd 收养、继续占着 3001。
# 于是「同步了最新代码 + 重启成功」之后，端口仍由旧孤儿在服务，
# 用户看到的还是「改完没变化」。（2026-10-26 实测：孤儿 223 占着 3001，
# pm2 的 kg-backend 重启了 56 次全部 EADDRINUSE 失败。）
#
# 这里只杀「pid 等于该端口 listener、且不在 pm2 该应用后代集合里」的那一个进程，
# 不用 pkill -f 之类通配，避免误伤这台机器上其他任务的 3xxx 服务。
pm2_pid_of() {
  "$PM2_BIN" jlist 2>/dev/null | sed -n '/^\[/,$p' \
    | python3 -c 'import sys,json;n=sys.argv[1]
d=json.load(sys.stdin)
for p in d:
    if p.get("name")==n:
        print(p.get("pid") or "");break' "$1" 2>/dev/null | head -n 1
}

is_descendant_of() {
  local target="$1" root="$2" frontier="$2" next pid k depth=0 seen=" $2 "
  [[ -z "$root" ]] && return 1
  # target 就是 root 本身时，它就是 pm2 亲手拉起的那一个，当然「属于它」。
  #
  # 2026-10-27 实测踩到，这条 bug 让每一次同步都把自己的服务杀掉：
  # ecosystem.config.cjs 改用 `node --import tsx src/index.ts` 之后 tsx 不再
  # fork 一层，pm2 持有的 pid 就是真正监听端口的那个。而下面这个循环只从
  # 子进程开始找 target，root 自己永远不在候选里，于是 root==listener 被判成
  # 「不是后代 = 孤儿」→ kill -TERM 掉健康进程 → 紧接着 startOrReload 再拉起。
  # 用户看到的表象是「同步一次服务就抖一下」，pm2 restart 计数一路涨（本机
  # 实测 kg-frontend 83 次 / kg-backend 52 次）。同步日志里那两行
  # `清理占着 3000 的孤儿进程 60587（不属于 pm2 kg-frontend，pid=60587）`
  # 就是它——自己杀自己。
  [[ "$target" == "$root" ]] && return 0
  while [[ -n "$frontier" && "$depth" -lt 5 ]]; do
    next=""
    for pid in $frontier; do
      for k in $(pgrep -P "$pid" 2>/dev/null); do
        [[ "$k" == "$target" ]] && return 0
        case "$seen" in *" $k "*) continue ;; esac
        seen="$seen$k "
        next="$next $k"
      done
    done
    frontier="$next"
    depth=$((depth + 1))
  done
  return 1
}

reap_orphan_on() {
  local port="$1" app="$2" lpid root_pid
  lpid="$(lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n 1)"
  [[ -z "$lpid" ]] && return 0
  root_pid="$(pm2_pid_of "$app")"
  [[ -z "$root_pid" ]] && return 0
  is_descendant_of "$lpid" "$root_pid" && return 0
  say "清理占着 $port 的孤儿进程 ${lpid}（不属于 pm2 ${app}，pid=${root_pid}）"
  kill -TERM "$lpid" 2>/dev/null || true
  local i=0
  while [[ "$i" -lt 10 ]]; do
    sleep 1; i=$((i + 1))
    [[ -z "$(lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null)" ]] && return 0
  done
  kill -KILL "$lpid" 2>/dev/null || true
  sleep 2
  return 0
}

if [[ -z "$PM2_BIN" ]]; then
  say "找不到 pm2（PM2_BIN 为空），跳过重启"
  exit 1
fi

reap_orphan_on 3000 kg-frontend
reap_orphan_on 3001 kg-backend

# 用 startOrReload 而不是 restart：只有前者会重新读 ecosystem.config.cjs。
# 这是必须的 —— 进程定义本身也会演进。2026-10-26 把 kg-backend 从
# `tsx watch src/index.ts` 改成 `node --import tsx src/index.ts`（去掉会把
# 孙进程留成孤儿的那层 fork），如果这里仍是 `pm2 restart`，新定义永远不会生效，
# 修好的配置形同虚设，而每次同步还会打印「已重启」——又一次假绿灯。
# startOrReload 对「定义没变」的情况等价于 reload，因此可以无条件使用。
# cwd 必须在 LIVE_ROOT 里：ecosystem 的 `cwd: './backend'` 是相对路径。
ECOSYSTEM="$LIVE_ROOT/ecosystem.config.cjs"
if [[ -f "$ECOSYSTEM" ]]; then
  ( cd "$LIVE_ROOT" && "$PM2_BIN" startOrReload "$ECOSYSTEM" --update-env >/dev/null 2>&1 ) \
    || { say "pm2 startOrReload 失败（${ECOSYSTEM}）"; exit 1; }
  say "已按 ecosystem.config.cjs 重载 kg-backend / kg-frontend，等待就绪…"
else
  say "常驻目录里没有 ecosystem.config.cjs，退回 pm2 restart（进程定义不会被更新）"
  "$PM2_BIN" restart kg-backend kg-frontend >/dev/null 2>&1 || { say "pm2 重启失败（PM2_BIN=${PM2_BIN:-未找到}）"; exit 1; }
  say "已重启 kg-backend / kg-frontend，等待就绪…"
fi

for i in $(seq 1 30); do
  sleep 2
  f="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:3000/ 2>/dev/null)"
  b="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:3001/api/health 2>/dev/null)"
  if [[ "$f" == "200" && "$b" == "200" ]]; then
    say "就绪：3000=200 3001=200（用时 $((i*2))s）"
    exit 0
  fi
done

say "超时未就绪：3000=${f:-000} 3001=${b:-000}"
exit 1
