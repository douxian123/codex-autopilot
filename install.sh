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
PROJECT=""
TRUNK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-marketplace) NO_MARKET=1; shift ;;
    --refresh-plugin) REFRESH=1; shift ;;
    --no-seed) NO_SEED=1; shift ;;
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

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "找不到 python3，请先安装后再运行。" >&2
  exit 1
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

# 2) 装插件实体：plugin/ 的清单 + skill，加上本仓库的运行时 ops/。
mkdir -p "$MANIFEST_DIR" "$PLUGIN_ROOT"
DEST="$PLUGIN_ROOT/codex-autopilot"
if [ -L "$DEST" ] || [ -e "$DEST" ]; then rm -rf -- "$DEST"; fi
mkdir -p "$DEST/ops"
cp -R "$PLUGIN_DIR/." "$DEST/"
cp "$REPO"/ops/*.py "$DEST/ops/"
cp "$REPO/ops/central.config.example.json" "$DEST/ops/"

# 3) .mcp.json：运行时代码走插件自己的 ops/，状态写回目标项目。
"$PY" - "$DEST/.mcp.json" "$PY" "$PROJECT" "$TRUNK" <<'PYEOF'
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

echo "已安装插件到 $DEST"

# 4) 登记进个人 marketplace
if [ "$NO_MARKET" -eq 0 ]; then
  "$PY" - "$MARKET" <<'PYEOF'
import json, os, shutil, sys
path = sys.argv[1]
name = "codex-autopilot"
entry = {
    "name": name,
    "source": {"source": "local", "path": "./plugins/codex-autopilot"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity",
}
if os.path.exists(path):
    shutil.copyfile(path, path + ".bak-codex-autopilot")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
else:
    data = {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
plugins = data.setdefault("plugins", [])
plugins[:] = [p for p in plugins if p.get("name") != name]
plugins.append(entry)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print(f"已注册 {name} 到 {path}（原文件备份为 .bak-codex-autopilot）")
PYEOF
else
  echo "（跳过 marketplace：请手工把下面条目加进 $MARKET 的 plugins 数组）"
  echo '  { "name": "codex-autopilot", "source": { "source": "local", "path": "./plugins/codex-autopilot" }, "policy": { "installation": "AVAILABLE", "authentication": "ON_INSTALL" }, "category": "Productivity" }'
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
