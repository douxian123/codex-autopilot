#!/usr/bin/env bash
# Codex Autopilot 安装脚本（macOS / Linux）
#
# 作用：把本仓库的插件安装进 Codex 个人插件市场，并把 MCP server 指到你那个项目的 ops/。
# 非破坏：只写 ~/.agents/plugins/codex-autopilot/（本脚本自己的安装目录）和给
# marketplace.json 加一条（先备份）；不改本仓库里的任何文件。
#
# 用法：
#   ./install.sh [项目目录]        # 默认指向本仓库自身
#   ./install.sh --no-marketplace  # 只装插件，不动 marketplace.json
set -euo pipefail

NO_MARKET=0
PROJECT=""
for arg in "$@"; do
  case "$arg" in
    --no-marketplace) NO_MARKET=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) PROJECT="$arg" ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$REPO/plugin"
AGENTS_DIR="$HOME/.agents/plugins"
MARKET="$AGENTS_DIR/marketplace.json"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "找不到 python3，请先安装后再运行。" >&2
  exit 1
fi

[ -n "$PROJECT" ] || PROJECT="$REPO"
PROJECT="$(cd "$PROJECT" && pwd)"

if [ ! -f "$PROJECT/ops/kgctl.py" ]; then
  echo "× 找不到 $PROJECT/ops/kgctl.py —— 这个目录还没有放进 ops/。" >&2
  echo "  先执行：cp -r \"$REPO/ops\" \"$PROJECT/ops\"" >&2
  exit 1
fi

mkdir -p "$AGENTS_DIR"

DEST="$AGENTS_DIR/codex-autopilot"
# 装一份干净副本，避免安装动作把 clone 弄脏；重跑即覆盖更新。
if [ -L "$DEST" ] || [ -e "$DEST" ]; then rm -rf -- "$DEST"; fi
mkdir -p "$DEST"
cp -R "$PLUGIN_DIR/." "$DEST/"

# 1) MCP server 指向 <项目>/ops/central_mcp.py（绝对路径；换项目重跑本脚本）
"$PY" - "$DEST/.mcp.json" "$PROJECT/ops/central_mcp.py" <<'PYEOF'
import json, sys
path, script = sys.argv[1], sys.argv[2]
data = {"mcpServers": {"codex-autopilot": {"command": "python3", "args": [script]}}}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print(f"已写 {path} -> {script}")
PYEOF

echo "已安装插件到 $DEST"

# 3) 登记进个人 marketplace
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

echo
echo "完成。下一步："
echo "  1) 重启 Codex 桌面端 / CLI"
echo "  2) 打开 Plugins → Personal → 安装「Codex 自动挡」"
echo "  3) 在项目里跑：python3 ops/kgctl.py status"
