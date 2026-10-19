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
# 非破坏：只写下面几处，旧版本目录和旧账本一律保留
#   1. ~/plugins/codex-autopilot/releases/<版本>/（每个版本一个不可变目录）
#   2. ~/.agents/plugins/marketplace.json（原子替换，只改自动挡这一条）
#   3. 目标项目里缺失的状态文件（已存在的一律不动）
#   4. ~/Applications/CodexAutopilotCompanion.app（可随时从版本目录重建）
#   5. ~/Library/Application Support/CodexAutopilot/companion.json（Companion 配置）
#
# 用法：
#   ./install.sh [项目目录]          # 默认本仓库自身；这个目录 = 状态根
#   ./install.sh --state-root <目录>  # 显式指定状态根（等价于位置参数）
#   ./install.sh --trunk <目录>       # 指定落盘用的主干 worktree，写进 KG_TRUNK
#   ./install.sh --no-seed            # 不在目标项目里补状态文件
#   ./install.sh --no-marketplace     # 只装插件，不动 marketplace.json
#   ./install.sh --no-companion       # 不编译/安装 macOS 菜单栏 Companion
#   ./install.sh --refresh-plugin     # 装完后用 codex CLI 刷新已安装插件（否则桌面端可能仍用旧缓存）
#   ./install.sh --verify             # 装完立刻跑一遍验收（落地物/菜单栏应用/原生自检/总闸）
set -euo pipefail

NO_MARKET=0
NO_SEED=0
REFRESH=0
VERIFY=0
ROLLBACK=0
LIST_RELEASES=0
COMPANION=1
PROJECT=""
TRUNK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-marketplace) NO_MARKET=1; shift ;;
    --refresh-plugin) REFRESH=1; shift ;;
    --verify) VERIFY=1; shift ;;
    --no-seed) NO_SEED=1; shift ;;
    --no-companion) COMPANION=0; shift ;;
    --rollback) ROLLBACK=1; shift ;;
    --list-releases) LIST_RELEASES=1; shift ;;
    --state-root) PROJECT="${2:?--state-root 需要一个目录}"; shift 2 ;;
    --trunk) TRUNK="${2:?--trunk 需要一个目录}"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) PROJECT="$1"; shift ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$REPO/plugin"
# 源码仓库里插件本体在 plugin/，已发布的版本目录直接以自身为插件根。
# 两者都允许，发布目录因此也能完整跑安装/自测，不再依赖源码仓库布局。
if [ -d "$REPO/plugin/.codex-plugin" ]; then
  PLUGIN_DIR="$REPO/plugin"
else
  PLUGIN_DIR="$REPO"
fi
# Codex 的 personal marketplace 根是 ${HOME}：清单在 $HOME/.agents/plugins/marketplace.json，
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
# 重装时**沿用已有的解释器路径**，不要静默换成另一个 python3。
# 为什么：`command -v python3` 的结果取决于 PATH；从不同 shell 跑 install.sh 会得到不同
# 解释器（实测在 /opt/homebrew/bin/python3 与 /usr/bin/python3 之间来回跳）。那会让一次
# 「只刷新代码」的重装顺手改写一份本来能用的配置——属于「静默改变别人的东西」。
EXISTING_MCP="$PLUGIN_ROOT/codex-autopilot/.mcp.json"
if [ -f "$EXISTING_MCP" ]; then
  PREV_PY="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["mcpServers"]["codex-autopilot"]["command"])' "$EXISTING_MCP" 2>/dev/null || true)"
  if [ -n "$PREV_PY" ] && [ -x "$PREV_PY" ]; then
    PY="$PREV_PY"
  fi
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
  echo "× --trunk 不能等于 --state-root（${PROJECT}）。" >&2
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
  echo "  已有发布不可变；请提升插件清单里的版本号后再安装。" >&2
  exit 1
fi
STAGE="$RELEASE_ROOT/.staging-$VERSION-$$"
mkdir -p "$STAGE/ops"
trap 'if [ -n "${STAGE:-}" ] && [ -d "$STAGE" ]; then rm -rf -- "$STAGE"; fi' EXIT
cp -R "$PLUGIN_DIR/." "$STAGE/"
cp "$REPO/install.sh" "$STAGE/install.sh"
chmod +x "$STAGE/install.sh"
cp "$REPO"/ops/*.py "$STAGE/ops/"
# 运行时可能带非 .py 的配套文件（模板/示例配置）。一并复制，避免安装后缺文件。
cp "$REPO"/ops/*.md "$STAGE/ops/" 2>/dev/null || true
cp "$REPO"/ops/*.json "$STAGE/ops/" 2>/dev/null || true
cp "$REPO/preview-check.sh" "$STAGE/preview-check.sh"
chmod +x "$STAGE/preview-check.sh"
# 自测需要真实基线 fixture；嵌套目录不能靠通配符漏掉。
if [ -d "$REPO/ops/selftest_fixtures" ]; then
  cp -R "$REPO/ops/selftest_fixtures" "$STAGE/ops/"
fi
# Companion 源码随版本目录一起装：以后任何一次升级都能从版本目录重建菜单栏应用，
# 不依赖原始仓库还在不在。
mkdir -p "$STAGE/companion"
cp -R "$REPO/companion/Sources" "$STAGE/companion/"
for f in build.sh Info.plist Package.swift README.md; do
  [ -f "$REPO/companion/$f" ] && cp "$REPO/companion/$f" "$STAGE/companion/$f"
done

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

# 5) 菜单栏 Companion：编译成 .app 放进 ~/Applications。
#    它只是状态锚点，只读 ops/companion_api.py 这一条通道；编译不了也不影响插件本体。
#    构建在工作目录里做（不污染不可变版本目录），app 本身随时可重建。
if [ "$COMPANION" -eq 1 ]; then
  echo
  if ! command -v swiftc >/dev/null 2>&1; then
    echo "本机没有 swiftc，跳过菜单栏 Companion。"
    echo "  装好 Xcode Command Line Tools 后手动补："
    echo "    bash $DEST/companion/build.sh"
    echo "  然后把 $DEST/companion/dist/CodexAutopilotCompanion.app 拖进「应用程序」"
  else
    BUILD_ROOT="$AUTOPILOT_ROOT/build"
    LOG_DIR="$AUTOPILOT_ROOT/logs"
    WORK="$BUILD_ROOT/companion-$VERSION"
    LOG="$LOG_DIR/companion-$VERSION.log"
    mkdir -p "$BUILD_ROOT" "$LOG_DIR"
    rm -rf -- "$WORK"
    mkdir -p "$WORK"
    cp -R "$DEST/companion/." "$WORK/"
    echo "编译菜单栏 Companion（日志：${LOG}）…"
    if bash "$WORK/build.sh" >"$LOG" 2>&1 && [ -d "$WORK/dist/CodexAutopilotCompanion.app" ]; then
      APP_DST="$HOME/Applications/CodexAutopilotCompanion.app"
      APP_STAGE="$HOME/Applications/.CodexAutopilotCompanion.staging-$$.app"
      mkdir -p "$HOME/Applications"
      rm -rf -- "$APP_STAGE"
      cp -R "$WORK/dist/CodexAutopilotCompanion.app" "$APP_STAGE"
      rm -rf -- "$APP_DST"
      mv "$APP_STAGE" "$APP_DST"
      echo "已安装菜单栏 Companion：$APP_DST"
      echo "  启动：open \"$APP_DST\""
      echo "  快捷键：⌥⌘A 开合面板 · ⌥⌘S 截图派活 · ⌥⌘D 打开完整面板"
      echo "  截图拖到菜单栏图标上，或者在「服务」菜单里选「发送到 Codex 自动挡」也可以派活"
    else
      echo "× Companion 编译失败，插件本体已装好、不受影响。"
      echo "  失败原因见：$LOG"
      echo "  手动重试：bash $DEST/companion/build.sh"
    fi
  fi
fi

# 6) 可选：用官方 CLI 刷新已安装插件，避免桌面端继续用旧缓存。
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
echo "  3) 菜单栏：open \"$HOME/Applications/CodexAutopilotCompanion.app\"（⌥⌘A 面板 · ⌥⌘S 截图派活）"
echo "  4) 想手敲命令看状态：KG_REPO=$PROJECT python3 $DEST/ops/kgctl.py status"

# 装完顺手验收：装是装上了、还是装上了能用，是两件事。
if [ "$VERIFY" -eq 1 ]; then
  echo
  echo "跑一遍验收（安装本身已完成；下面报 FAIL 的是还需要处理的部分）…"
  set +e
  bash "$REPO/preview-check.sh" --no-install
  verify_code=$?
  set -e
  exit "$verify_code"
fi
