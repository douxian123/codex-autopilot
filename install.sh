#!/usr/bin/env bash
# Codex 自动挡 安装脚本（macOS / Linux）
#
# 作用：把插件（连同它自带的一份运行时代码）装进 Codex 个人插件市场，并把项目
# 状态根写进插件的 .mcp.json，让普通 Codex 输入框直接驱动这套编排。
#
# 自包含：运行时代码（ops/*.py）会复制进插件目录。目标项目里就算没有 ops/ 运行时
# 代码、或者那份代码已经过期，插件跑的都是自己这一份最新代码。目标项目只留账本
# （ops/tasks + ops/truth + ops/context + docs/）。
#
# 非破坏：只写下面三处
#   1. ~/plugins/codex-autopilot/（插件实体，重跑即覆盖）
#   2. ~/.agents/plugins/marketplace.json（先备份为 .bak-codex-autopilot）
#   3. 目标项目里缺失的状态文件（已存在的一律不动）
#
# 用法：
#   ./install.sh [项目目录]          # 默认本仓库自身；这个目录 = 状态根
#   ./install.sh --state-root <目录>  # 显式指定状态根（等价于位置参数）
#   ./install.sh --trunk <目录>       # 指定落盘用的主干 worktree，写进 KG_TRUNK
#   ./install.sh --no-seed            # 不在目标项目里补状态文件
#   ./install.sh --no-marketplace     # 只装插件，不动 marketplace.json
#   ./install.sh --refresh-plugin     # 装完后用 codex CLI 刷新已安装插件（否则桌面端可能仍用旧缓存）
set -euo pipefail

NO_MARKET=0
NO_SEED=0
REFRESH=0
ROLLBACK=0
LIST_RELEASES=0
PROJECT=""
TRUNK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-marketplace) NO_MARKET=1; shift ;;
    --refresh-plugin) REFRESH=1; shift ;;
    --no-seed) NO_SEED=1; shift ;;
    --rollback) ROLLBACK=1; shift ;;
    --list-releases) LIST_RELEASES=1; shift ;;
    --state-root) PROJECT="${2:?--state-root 需要一个目录}"; shift 2 ;;
    --trunk) TRUNK="${2:?--trunk 需要一个目录}"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) PROJECT="$1"; shift ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$REPO/plugin"
# Codex 的 personal marketplace 根是 $HOME：清单在 $HOME/.agents/plugins/marketplace.json，
# 而清单里的 "./plugins/<name>" 是相对根解析的，所以插件实体必须落在 $HOME/plugins/<name>。
# 这两个目录不是同一个地方，装错了会被 marketplace 列出、却 add 不进去。
MANIFEST_DIR="$HOME/.agents/plugins"
MARKET="$MANIFEST_DIR/marketplace.json"
PLUGIN_ROOT="$HOME/plugins"
AUTOPILOT_ROOT="$PLUGIN_ROOT/codex-autopilot"
RELEASE_ROOT="$AUTOPILOT_ROOT/releases"
INSTALL_STATE="$AUTOPILOT_ROOT/install-state.json"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "找不到 python3，请先安装后再运行。" >&2
  exit 1
fi

if [ "$LIST_RELEASES" -eq 1 ]; then
  if [ -d "$RELEASE_ROOT" ]; then
    find "$RELEASE_ROOT" -mindepth 1 -maxdepth 1 -type d ! -name '.staging-*' -print | sort
  else
    echo "尚无版本化安装。"
  fi
  exit 0
fi

if [ "$ROLLBACK" -eq 1 ]; then
  "$PY" - "$MARKET" "$INSTALL_STATE" <<'PYEOF'
import json, os, sys, tempfile
market, state_path = sys.argv[1:3]
try:
    state = json.load(open(state_path, encoding="utf-8"))
    previous = state.get("previous_source")
except (OSError, ValueError):
    state = None
if state is None or "previous_source" not in state:
    raise SystemExit("没有可回滚的 Codex 自动挡安装记录。")
try:
    data = json.load(open(market, encoding="utf-8"))
except (OSError, ValueError) as exc:
    raise SystemExit(f"无法读取 marketplace，未执行回滚：{exc}")
entry = next((p for p in data.get("plugins", []) if p.get("name") == "codex-autopilot"), None)
if not entry:
    raise SystemExit("marketplace 中找不到 Codex 自动挡，未执行回滚。")
if previous:
    entry["source"] = {"source": "local", "path": previous}
else:
    data["plugins"] = [p for p in data.get("plugins", [])
                       if p.get("name") != "codex-autopilot"]
fd, tmp = tempfile.mkstemp(prefix=".marketplace-codex-autopilot-", dir=os.path.dirname(market))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, market)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
if previous:
    print(f"已回滚 Codex 自动挡入口到 {previous}；其他 marketplace 项目保持不变，已安装版本目录均保留。")
else:
    print("已从 marketplace 移除本次新增的 Codex 自动挡入口；其他项目和已安装版本目录均保留。")
PYEOF
  exit 0
fi

[ -n "$PROJECT" ] || PROJECT="$REPO"
if [ ! -d "$PROJECT" ]; then
  echo "× 目标项目不存在：$PROJECT" >&2
  exit 1
fi
PROJECT="$(cd "$PROJECT" && pwd)"
# 落盘主干 = 一个独立的 worktree；缺省用同级的 <项目名>-trunk（kgctl 首次落盘时自建）。
# 它绝不能等于状态根本身：那样 land 的 merge/reset 会直接改写 ops/tasks、ops/truth 账本。
if [ -z "$TRUNK" ]; then
  TRUNK="$PROJECT-trunk"
fi
if [ -d "$TRUNK" ]; then
  TRUNK="$(cd "$TRUNK" && pwd)"
fi
if [ "$TRUNK" = "$PROJECT" ]; then
  echo "× --trunk 不能等于 --state-root（$PROJECT）。" >&2
  echo "  落盘主干必须是独立 worktree；指到账本目录会在 merge/reset 时直接改写它。" >&2
  echo "  留空即用默认的 ${PROJECT}-trunk。" >&2
  exit 1
fi

# 1) 初始化目标项目的状态层：只补缺失文件，绝不覆盖已有账本。
if [ "$NO_SEED" -eq 0 ]; then
  mkdir -p "$PROJECT/ops/tasks" "$PROJECT/ops/truth" "$PROJECT/docs"
  seed_file() {  # $1 = 源，$2 = 目标
    [ -e "$2" ] && return 0
    mkdir -p "$(dirname "$2")"
    cp "$1" "$2"
    echo "  补状态文件 $2"
  }
  [ -e "$PROJECT/ops/tasks/inbox.jsonl" ] || : > "$PROJECT/ops/tasks/inbox.jsonl"
  seed_file "$REPO/ops/tasks/lanes.json" "$PROJECT/ops/tasks/lanes.json"
  for f in "$REPO"/ops/truth/*; do
    [ -e "$f" ] || continue
    seed_file "$f" "$PROJECT/ops/truth/$(basename "$f")"
  done
fi

# 2) 先校验，再把不可变版本装进独立目录；绝不删除或覆盖旧安装。
mkdir -p "$MANIFEST_DIR" "$PLUGIN_ROOT" "$RELEASE_ROOT"
VERSION="$("$PY" - "$PLUGIN_DIR/.codex-plugin/plugin.json" <<'PYEOF'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])
PYEOF
)"
case "$VERSION" in
  *[!A-Za-z0-9._+-]*|"") echo "× 插件版本号不合法：$VERSION" >&2; exit 1 ;;
esac
DEST="$RELEASE_ROOT/$VERSION"
if [ -e "$DEST" ]; then
  echo "× 版本目录已存在，拒绝覆盖：$DEST" >&2
  echo "  已有发布不可变；请提升 plugin/.codex-plugin/plugin.json 的版本号后再安装。" >&2
  exit 1
fi
STAGE="$RELEASE_ROOT/.staging-$VERSION-$$"
mkdir -p "$STAGE/ops"
trap 'if [ -n "${STAGE:-}" ] && [ -d "$STAGE" ]; then rm -rf -- "$STAGE"; fi' EXIT
cp -R "$PLUGIN_DIR/." "$STAGE/"
cp "$REPO"/ops/*.py "$STAGE/ops/"
cp "$REPO/ops/central.config.example.json" "$STAGE/ops/"

# 3) .mcp.json：运行时代码走插件自己的 ops/，状态写回目标项目。
"$PY" - "$STAGE/.mcp.json" "$PY" "$PROJECT" "$TRUNK" <<'PYEOF'
import json, sys
path, python, project, trunk = sys.argv[1:5]
env = {"KG_REPO": project}
if trunk:
    env["KG_TRUNK"] = trunk
data = {"mcpServers": {"codex-autopilot": {
    "command": python,
    "args": ["./ops/central_mcp.py"],
    "cwd": ".",
    "env": env,
}}}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print(f"已写 {path}")
print(f"  运行时代码 = {path.rsplit('/', 1)[0]}/ops/central_mcp.py")
print(f"  状态根     = {project}")
if trunk:
    print(f"  落盘主干   = {trunk}")
PYEOF
if ! "$PY" -m py_compile "$STAGE"/ops/*.py; then
  echo "× 新版本 Python 预检失败，当前安装入口未更改。" >&2
  exit 1
fi
mv "$STAGE" "$DEST"
STAGE=""
echo "已安装不可变版本 $VERSION 到 $DEST"

# 4) 登记进个人 marketplace
if [ "$NO_MARKET" -eq 0 ]; then
  "$PY" - "$MARKET" "$INSTALL_STATE" "$VERSION" <<'PYEOF'
import json, os, shutil, sys, tempfile
path, state_path, version = sys.argv[1:4]
name = "codex-autopilot"
source_path = f"./plugins/codex-autopilot/releases/{version}"
entry = {
    "name": name,
    "source": {"source": "local", "path": source_path},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity",
}
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
else:
    data = {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
plugins = data.setdefault("plugins", [])
existing = next((p for p in plugins if p.get("name") == name), None)
previous = ((existing or {}).get("source") or {}).get("path")
if existing:
    existing.update(entry)
else:
    plugins.append(entry)
directory = os.path.dirname(path)
os.makedirs(directory, exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".marketplace-codex-autopilot-", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
state = {"version": version, "source": source_path, "previous_source": previous}
fd, tmp = tempfile.mkstemp(prefix=".install-state-", dir=os.path.dirname(state_path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, state_path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print(f"已注册 {name} 到 {path}，当前版本路径：{source_path}")
if previous:
    print(f"原版本仍完整保留，可用 ./install.sh --rollback 回退到：{previous}")
PYEOF
  mkdir -p "$HOME/Library/Application Support/CodexAutopilot"
  "$PY" - "$HOME/Library/Application Support/CodexAutopilot/companion.json" "$DEST/ops/companion_api.py" "$PROJECT" <<'PYEOF'
import json, os, sys, tempfile
path, api, project = sys.argv[1:4]
data = {"api": api, "project": project}
fd, tmp = tempfile.mkstemp(prefix=".companion-", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print(f"Companion 控制接口配置：{path}")
PYEOF
else
  echo "（跳过 marketplace：请手工把下面条目加进 $MARKET 的 plugins 数组）"
  echo "  新版本目录：$DEST"
  echo '  marketplace 入口将指向该不可变版本目录；可使用 ./install.sh --rollback 回退。'
fi

# 5) 可选：用官方 CLI 刷新已安装插件，避免桌面端继续用旧缓存。
#    Codex 会把插件快照到 ~/.codex/plugins/cache/personal/<name>/<version>/；
#    只改 marketplace 文件和插件目录，缓存不会自动更新。
if [ "$REFRESH" -eq 1 ]; then
  if command -v codex >/dev/null 2>&1; then
    echo
    echo "刷新 Codex 插件缓存（codex plugin add codex-autopilot@personal）…"
    if codex plugin add codex-autopilot@personal >/tmp/codex-autopilot-plugin-add.log 2>&1; then
      cat /tmp/codex-autopilot-plugin-add.log
      echo "插件缓存已刷新。"
    else
      echo "× 刷新失败，输出如下；可稍后手动重试："
      cat /tmp/codex-autopilot-plugin-add.log 2>/dev/null
      echo "  codex plugin add codex-autopilot@personal"
    fi
  else
    echo "找不到 codex CLI，跳过刷新；请在桌面端 Plugins 里重装一次。"
  fi
fi

echo
echo "完成。下一步："
echo "  1) 重启 Codex 桌面端 / CLI，在 Plugins → Personal 里安装「Codex 自动挡」"
echo "  2) 在项目里直接说话即可，例如：左边导航排版坏了，帮我记下来并安排修复"
echo "  3) 想手敲命令看状态：KG_REPO=$PROJECT python3 $DEST/ops/kgctl.py status"
