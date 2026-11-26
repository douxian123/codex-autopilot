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
#   ./install.sh --install-daemon     # 显式首次安装/重装 launchd 常驻守护（默认不动常驻）
#   ./install.sh --enable-local-guards # 显式允许改已装的本机 launchd / hooks（默认不碰）
#   ./install.sh --migrate-local-secrets # 显式迁移 HyAPI 本地密钥到 ~/.codex-proxy/secrets.env
#   ./install.sh --verify             # 装完立刻跑一遍验收（落地物/菜单栏应用/原生自检/总闸）
set -euo pipefail

NO_MARKET=0
NO_SEED=0
REFRESH=0
VERIFY=0
ROLLBACK=0
LIST_RELEASES=0
COMPANION=1
INSTALL_DAEMON=0
ENABLE_LOCAL_GUARDS=0
MIGRATE_LOCAL_SECRETS=0
PROJECT=""
TRUNK=""
TRUNK_SET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-marketplace) NO_MARKET=1; shift ;;
    --refresh-plugin) REFRESH=1; shift ;;
    --verify) VERIFY=1; shift ;;
    --no-seed) NO_SEED=1; shift ;;
    --no-companion) COMPANION=0; shift ;;
    --install-daemon) INSTALL_DAEMON=1; shift ;;
    --enable-local-guards) ENABLE_LOCAL_GUARDS=1; shift ;;
    --migrate-local-secrets) MIGRATE_LOCAL_SECRETS=1; shift ;;
    --rollback) ROLLBACK=1; shift ;;
    --list-releases) LIST_RELEASES=1; shift ;;
    --state-root) PROJECT="${2:?--state-root 需要一个目录}"; shift 2 ;;
    --trunk) TRUNK="${2:?--trunk 需要一个目录}"; TRUNK_SET=1; shift 2 ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) PROJECT="$1"; shift ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$REPO/plugin"
SOURCE_COMMIT=""
SOURCE_REPO_IS_GIT=0
if git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  SOURCE_REPO_IS_GIT=1
  dirty="$(git -C "$REPO" status --porcelain --untracked-files=normal)"
  if [ -n "$dirty" ] && [ "${CODEX_AUTOPILOT_ALLOW_DIRTY_SOURCE:-0}" != "1" ]; then
    echo "× 源码工作区有未提交改动，拒绝发布不可变版本。" >&2
    echo "  当前安装器会复制整个工作区，继续会把已提交代码和本地改动混进同一版本。" >&2
    echo "  先提交要发布的内容；隔离测试可显式设置 CODEX_AUTOPILOT_ALLOW_DIRTY_SOURCE=1。" >&2
    git -C "$REPO" status --short >&2
    exit 1
  fi
  SOURCE_COMMIT="$(git -C "$REPO" rev-parse HEAD)"
fi
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

# 本机集成属于显式 opt-in：公开 beta 默认不写 launchd、不改 hooks.json、
# 不迁移 provider 密钥。下面这段只由 --enable-local-guards、
# --install-daemon、--migrate-local-secrets 触发；默认安装只装插件主链。

# 本地密钥迁移单独授权：即使启用了本机 guards，也只有显式传
# --migrate-local-secrets 时才会碰 ~/.codex-proxy/secrets.env。
migrate_local_secrets() {
  [ "$MIGRATE_LOCAL_SECRETS" -eq 1 ] || return 0
  if [ -f "$DEST/ops/guards/hyapi_secrets.py" ]; then
    echo
    echo "迁移 HyAPI 本地密钥（只写 ~/.codex-proxy/secrets.env，不回显）…"
    if ! "$PY" "$DEST/ops/guards/hyapi_secrets.py" --migrate; then
      echo "× 本地密钥迁移失败；不继续改指 launchd。" >&2
      echo "  避免 plist 指向新脚本但任务因缺密钥立刻 SystemExit。" >&2
      exit 1
    fi
  fi
}

install_local_integrations() {
  local enable_all="${1:-0}"
  local daemon_label="${KG_DAEMON_LABEL:-com.codex-autopilot.daemon}"
  local daemon_plist="$HOME/Library/LaunchAgents/${daemon_label}.plist"
  local daemon_exists=0
  [ -f "$daemon_plist" ] && daemon_exists=1

  # 5.5) 守护改指/首次安装。只有显式授权才碰 plist 和 launchd。
  #
  #   --install-daemon：允许首次安装，也允许改指已装守护。
  #   --enable-local-guards：只维护已经存在的守护，绝不擅自新增常驻。
  if [ "${NO_DAEMON_REPOINT:-0}" != "1" ] && \
     { [ "$INSTALL_DAEMON" -eq 1 ] || { [ "$enable_all" -eq 1 ] && [ "$daemon_exists" -eq 1 ]; }; }; then
    PLIST="$daemon_plist"
    DAEMON_PLIST_EXISTS="$daemon_exists"
    if [ "$INSTALL_DAEMON" -eq 1 ] || [ "$DAEMON_PLIST_EXISTS" -eq 1 ]; then
      echo
      if [ "$DAEMON_PLIST_EXISTS" -eq 1 ]; then
        echo "守护改指新版本（否则 live 会继续跑上一版代码）…"
      else
        echo "按显式 --install-daemon 首次安装常驻守护…"
      fi
      set +e
      KG_REPO="$PROJECT" KG_TRUNK="$TRUNK" "$PY" "$DEST/ops/central_daemon.py" --install \
        >>"$HOME/.codex-autopilot/install-daemon.log" 2>&1
      daemon_rc=$?
      set -e
      if [ "$daemon_rc" -eq 0 ]; then
        if [ "$DAEMON_PLIST_EXISTS" -eq 1 ]; then
          echo "守护已切到 ${VERSION} 并重启。"
        else
          echo "守护已首次安装为 ${VERSION} 并启动。"
        fi
      else
        echo "× 守护改指失败（rc=${daemon_rc}），日志："
        echo "  $HOME/.codex-autopilot/install-daemon.log"
        echo "  手动重试：KG_REPO=\"$PROJECT\" KG_TRUNK=\"$TRUNK\" python3 \"$DEST/ops/central_daemon.py\" --install"
      fi
    else
      echo
      echo "本机没有守护 plist；按约定不替你启用常驻。"
      echo "需要常驻时用：./install.sh --install-daemon（会写 plist 并回读 launchd 判活）。"
    fi
  fi

  [ "$enable_all" -eq 1 ] || return 0

  # 5.6) 刷新「launchd 直接执行的入口脚本」（只在本机 guards 显式开启时）。
  ENTRY="$HOME/.codex-autopilot/ops/disk_reap.py"
  if [ -d "$HOME/.codex-autopilot/ops" ]; then
    cat >"$ENTRY.tmp-$$" <<'ENTRYEOF'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""launchd 入口：只做转发，绝不在这里实现任何逻辑。

这个文件由 install.sh 在显式启用本机 guards 时重写。它处在一个
「没有版本、也没人升级」的位置：launchd 直接执行它，而它既不在不可变版本
目录里，也不跟随 marketplace 指针。历史上它因此停在首次手工拷贝的版本上。
真正的实现在 `releases/<version>/ops/disk_reap.py`，那份会跟着发版一起更新。
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path


def current_ops() -> Path | None:
    """当前安装版本里的 ops/，与 disk_reap._resolve_ops 同一套判断顺序。"""
    home = Path.home()

    def usable(raw: str | None) -> Path | None:
        if not raw:
            return None
        cand = Path(raw).expanduser()
        if not cand.is_absolute():
            cand = home / raw.lstrip("./")
        ops = cand / "ops"
        return ops if (ops / "disk_reap.py").exists() else None

    state = home / "plugins/codex-autopilot/install-state.json"
    try:
        found = usable(str(json.loads(state.read_text(encoding="utf-8")).get("source") or ""))
        if found:
            return found
    except Exception:
        pass
    mkt = home / ".agents/plugins/marketplace.json"
    try:
        for plugin in (json.loads(mkt.read_text(encoding="utf-8")).get("plugins") or []):
            if isinstance(plugin, dict) and plugin.get("name") == "codex-autopilot":
                src = plugin.get("source")
                found = usable(src.get("path") if isinstance(src, dict) else src)
                if found:
                    return found
    except Exception:
        pass
    return None


def main() -> int:
    ops = current_ops()
    if ops is None:
        sys.stderr.write(
            "× 找不到当前安装版本里的 ops/disk_reap.py；"
            "先跑一次 ./install.sh 再启用这个定时任务。\n")
        return 1
    target = ops / "disk_reap.py"
    sys.argv = [str(target), *sys.argv[1:]]
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
ENTRYEOF
    chmod +x "$ENTRY.tmp-$$"
    mv "$ENTRY.tmp-$$" "$ENTRY"
    echo "已刷新定时回收入口（薄转发到 ${DEST}/ops/disk_reap.py）"
  fi

  # 5.7) launchd 定时任务改指。凭据迁移已拆到独立授权函数，不在这里做。
  if [ -f "$DEST/ops/guards/repoint_launchd.py" ]; then
    echo
    echo "定时任务改指当前版本（watchdog / 快照 / hooks / 3000 巡检 / 日志轮转）…"
    set +e
    "$PY" "$DEST/ops/guards/repoint_launchd.py" --install
    repoint_rc=$?
    set -e
    if [ "$repoint_rc" -ne 0 ]; then
      echo "× 定时任务改指未全部完成（rc=${repoint_rc}）。"
      echo "  只读复核：$PY $DEST/ops/guards/repoint_launchd.py --check"
    fi
  fi

  # 5.8) 主动刷新资源准入 hook。
  if [ -f "$DEST/ops/guards/install-resource-hooks.py" ]; then
    echo
    echo "刷新资源准入 hook（限制 tsc / Vite / Playwright 等重验证并发）…"
    if ! "$PY" "$DEST/ops/guards/install-resource-hooks.py"; then
      echo "× 资源准入 hook 安装失败。" >&2
      echo "  版本目录已保留；修复 Python/hooks.json 权限后重跑：" >&2
      echo "  $PY $DEST/ops/guards/install-resource-hooks.py" >&2
      exit 1
    fi
  fi
}

# 已经装过同一版本时，用户仍应能用同一个不可变版本打开本机能力；
# 否则为了开一个 opt-in 开关就得造一个新版本号，反而是负担。
if [ "$ENABLE_LOCAL_GUARDS" -eq 1 ] || [ "$INSTALL_DAEMON" -eq 1 ] || \
   [ "$MIGRATE_LOCAL_SECRETS" -eq 1 ]; then
  LOCAL_VERSION="$("$PY" - "$PLUGIN_DIR/.codex-plugin/plugin.json" <<'PYEOF'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])
PYEOF
)"
  case "$LOCAL_VERSION" in
    *[!A-Za-z0-9._+-]*|"") echo "× 插件版本号不合法：$LOCAL_VERSION" >&2; exit 1 ;;
  esac
  LOCAL_DEST="$RELEASE_ROOT/$LOCAL_VERSION"
  if [ -d "$LOCAL_DEST" ]; then
    DEST="$LOCAL_DEST"
    VERSION="$LOCAL_VERSION"
    read -r PROJECT TRUNK < <("$PY" - "$DEST/.mcp.json" <<'PYEOF'
import json, sys
try:
    env = json.load(open(sys.argv[1], encoding="utf-8"))["mcpServers"]["codex-autopilot"]["env"]
    print(str(env.get("KG_REPO") or ""), str(env.get("KG_TRUNK") or ""))
except Exception:
    print(" ", " ")
PYEOF
)
    PROJECT="${PROJECT:- }"
    TRUNK="${TRUNK:- }"
    [ "$PROJECT" = " " ] && PROJECT=""
    [ "$TRUNK" = " " ] && TRUNK=""
    migrate_local_secrets
    install_local_integrations "$ENABLE_LOCAL_GUARDS"
    echo "本机集成已按显式开关处理；未改动不可变版本目录。"
    exit 0
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
from pathlib import Path
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

def atomic_json(path, payload, prefix):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
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


current = state.get("source")
current_version = state.get("version") or ""
if current:
    marker = "./plugins/codex-autopilot/releases/"
    current_version = current.rsplit(marker, 1)[-1] if marker in current else ""
if previous:
    entry["source"] = {"source": "local", "path": previous}
    previous_version = previous.rsplit("./plugins/codex-autopilot/releases/", 1)[-1] \
        if "./plugins/codex-autopilot/releases/" in previous else ""
    # 回滚后的 install-state 必须继续保留一个更早的回滚点；否则第二次
    # --rollback 会原地打转，或者把不存在的版本当成上一代。
    older = state.get("older_source")
    if older == current and current_version:
        older = f"./plugins/codex-autopilot/releases/{current_version}"
    rolled_state = {
        "version": previous_version or state.get("version"),
        "source": previous,
        "previous_source": older,
        "source_commit": state.get("rollback_source_commit"),
    }
else:
    data["plugins"] = [p for p in data.get("plugins", [])
                       if p.get("name") != "codex-autopilot"]
    rolled_state = None

# marketplace 与 install-state 必须一起回滚；只改 marketplace 会让
# current_ops() 仍跟随旧 install-state 指向已回滚版本，形成“入口回去了、
# 运行代码没回去”的假回滚。先写临时文件，两个目标都准备好后再原子替换。
atomic_json(market, data, ".marketplace-codex-autopilot-")
if rolled_state is None:
    try:
        os.unlink(state_path)
    except FileNotFoundError:
        pass
else:
    atomic_json(state_path, rolled_state, ".install-state-")
if previous:
    print(f"已回滚 Codex 自动挡到 {previous}；install-state 与入口一致，其他 marketplace 项目保持不变，已安装版本目录均保留。")
else:
    print("已从 marketplace 移除本次新增的 Codex 自动挡入口和 install-state；其他项目和已安装版本目录均保留。")
PYEOF
  exit 0
fi

# 2026-11-22 preview.116 事故：从非 git 目录发版时 SOURCE_COMMIT 为空，安装器仍
# 写出 commit=null/dirty=true 的不可变版本，现场于是静默跑回旧 disk_reap。
# 发布根目录若是已带真实元数据的 release，可继承它的 commit；除此之外必须有 git HEAD。
# 放在 --list-releases / --rollback 之后，避免只读列表和回退被新门禁误伤。
if [ -z "$SOURCE_COMMIT" ]; then
  SOURCE_META="$REPO/.codex-autopilot-source.json"
  if [ -f "$SOURCE_META" ]; then
    SOURCE_COMMIT="$("$PY" - "$SOURCE_META" <<'PYEOF'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    commit = str(data.get("commit") or "").strip()
    if commit and not data.get("dirty"):
        print(commit)
except Exception:
    pass
PYEOF
)"
  fi
fi
if [ -z "$SOURCE_COMMIT" ]; then
  echo "× 找不到可追溯的唯一来源提交，拒绝发布不可变版本。" >&2
  echo "  $REPO 既不是干净 git 源码，也没有带真实 commit 的发布元数据。" >&2
  echo "  请从 git 仓库发布；不要用 commit=null 的 release 继续换代。" >&2
  exit 1
fi

[ -n "$PROJECT" ] || PROJECT="$REPO"
if [ ! -d "$PROJECT" ]; then
  echo "× 目标项目不存在：$PROJECT" >&2
  exit 1
fi
PROJECT="$(cd "$PROJECT" && pwd)"
# 状态根必须就是仓库根。只在任意 git worktree 或裸目录里写 .mcp.json，之后能从
# 子目录/软链绑定到错误路径；而自动挡又不会因此报错，只会安静地空转。
if ! git -C "$PROJECT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "× --state-root 必须是 git 仓库：$PROJECT" >&2
  echo "  自动挡需要分支与 worktree 才能落盘；普通目录只能存文件，不能闭环。" >&2
  exit 1
fi
PROJECT_TOP="$("$PY" - "$PROJECT" <<'PYEOF'
import os, sys
print(os.path.realpath(sys.argv[1]))
PYEOF
)"
PROJECT_REAL="$("$PY" - "$PROJECT" <<'PYEOF'
import os, sys
print(os.path.realpath(sys.argv[1]))
PYEOF
)"
GIT_TOP="$(git -C "$PROJECT" rev-parse --show-toplevel 2>/dev/null || true)"
GIT_TOP_REAL="$("$PY" - "$GIT_TOP" <<'PYEOF'
import os, sys
print(os.path.realpath(sys.argv[1]))
PYEOF
)"
if [ "$GIT_TOP_REAL" != "$PROJECT_REAL" ]; then
  echo "× --state-root 必须是仓库根，而不是子目录：$PROJECT" >&2
  echo "  实际仓库根：${GIT_TOP:-未知}" >&2
  exit 1
fi
# 落盘主干 = 一个独立的 worktree；缺省用同级的 <项目名>-trunk（kgctl 首次落盘时自建）。
# 它绝不能等于状态根本身：那样 land 的 merge/reset 会直接改写 ops/tasks、ops/truth 账本。
# 但绝不能把不存在的路径写进 KG_TRUNK / release 的 central.config.json：
# 2026-11-23 现场实测，<状态根>-trunk 不存在，却写进配置后 product_eval 的
# consistent 判据永久失败，用户只看到「装上了一版跑不起来」。
TRUNK="$("$PY" - "$REPO" "$PROJECT" "$TRUNK" "$TRUNK_SET" "$SOURCE_REPO_IS_GIT" "$SOURCE_COMMIT" <<'PYEOF'
import os
import subprocess
import sys

repo, project, trunk, explicit, source_is_git, source_commit = sys.argv[1:7]
explicit = explicit == "1"


def die(message):
    print("× " + message, file=sys.stderr)
    print("  落盘主干必须是真实存在、属于插件源码同一仓库的独立 git worktree。", file=sys.stderr)
    print("  可用 --trunk <已有 worktree> 显式指定；留空则使用同级的 "
          + project + "-trunk。", file=sys.stderr)
    raise SystemExit(1)


def git(git_dir, *args):
    proc = subprocess.run(["git", "-C", git_dir, *args],
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "").strip()


def resolve(path, base):
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(base, path))


if not trunk:
    trunk = project + "-trunk"
if not os.path.isdir(trunk):
    if explicit:
        die("--trunk 指向的目录不存在：" + trunk)
    # 默认 trunk 允许尚未创建：kgctl 首次落盘会自建 worktree。此时不要写死
    # 一个不存在的路径，否则 product_eval 会拿它做祖先比较而误红。
    print("")
    raise SystemExit(0)

trunk = os.path.abspath(trunk)
if os.path.realpath(trunk) == os.path.realpath(project):
    die("--trunk 不能等于 --state-root：" + project)

rc, inside = git(trunk, "rev-parse", "--is-inside-work-tree")
if rc != 0 or inside != "true":
    die("落盘主干不是 git worktree：" + trunk)
rc, top = git(trunk, "rev-parse", "--show-toplevel")
if rc != 0 or not top or resolve(top, trunk) != os.path.realpath(trunk):
    die("落盘主干必须是仓库根，不是子目录：" + trunk)

if source_is_git == "1":
    rc, common = git(repo, "rev-parse", "--git-common-dir")
    if rc != 0 or not common:
        die("取不到插件源码仓库身份：" + repo)
    common = resolve(common, repo)
    rc, trunk_common = git(trunk, "rev-parse", "--git-common-dir")
    if rc != 0 or not trunk_common or resolve(trunk_common, trunk) != common:
        die("落盘主干属于另一个仓库：" + trunk)
else:
    # 带真实来源元数据的 release 换代：目录本身不是 git worktree，
    # 用来源提交是不是主干祖先来证明同源，避免把正常换代误判成跨仓库。
    if not source_commit:
        die("取不到插件源码仓库身份：" + repo)
    rc, _ = git(trunk, "merge-base", "--is-ancestor", source_commit, "HEAD")
    if rc != 0:
        die("落盘主干不包含来源提交：" + source_commit[:12] + " -> " + trunk)

print(trunk)
PYEOF
)" || exit 1

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

# 1b) 把项目登记进多项目注册表。
# 为什么要有这一步：`.mcp.json` 一次只绑定一个 KG_REPO，于是「在别的项目窗口里
# 说话，记录却落进这一个账本」——2026-11-16 用户明确指出的 bug。注册表让入口在
# 投稿时能认出不止一个项目；绑定仍保留作默认项，但不再是唯一候选。
"$PY" - "$REPO" "$PROJECT" <<'PYEOF'
import importlib.util, sys
from pathlib import Path
repo, project = sys.argv[1], sys.argv[2]
ops = Path(repo) / "ops"
sys.path.insert(0, str(ops))
spec = importlib.util.spec_from_file_location("project_router", ops / "project_router.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["project_router"] = mod
try:
    spec.loader.exec_module(mod)
    row = mod.ensure_registered(project)
    if row is None:
        print(f"  （{project} 还不是状态根，先跑 install 的 seed 步骤再登记）")
    else:
        print(f"  已登记项目「{row['name']}」→ {row['root']}")
        print(f"  注册表 {mod.registry_path()}")
        print(f"  再加一个项目：KG_REPO=<该项目> python3 <插件>/ops/kgctl.py projects register <名字> <root>")
except Exception as exc:  # 登记失败不该让安装失败：绑定本身仍然是旧的可用路径
    print(f"  （项目登记跳过：{type(exc).__name__}: {exc}）")
PYEOF

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
# 安装件自检要读源码根的 docs/*.json 契约（七管家职责、施工闭环计划等），
# 也要读管家架构约束文档；不把项目生成的 docs 视图塞进不可变发布目录。
if [ -d "$REPO/docs" ]; then
  mkdir -p "$STAGE/docs"
  cp "$REPO"/docs/*.json "$STAGE/docs/" 2>/dev/null || true
  if [ -f "$REPO/docs/ARCHITECTURE_STEWARDS.md" ]; then
    cp "$REPO/docs/ARCHITECTURE_STEWARDS.md" "$STAGE/docs/ARCHITECTURE_STEWARDS.md"
  fi
  # 动态验收标准、体验契约与研究采用决策是安装后门禁/面板要读的现行规范，必须随 release 发布。
  for doc in DYNAMIC_ACCEPTANCE_SCOPE.md PRE_ACCEPTANCE_STANDARD.md \
             PRODUCT_CONTRACT.md OPENSOURCE_RESEARCH.md PANEL_INTERACTION_DESIGN.md \
             PROJECT_GLOBAL_RULES.md; do
    if [ -f "$REPO/docs/$doc" ]; then
      cp "$REPO/docs/$doc" "$STAGE/docs/$doc"
    fi
  done
  if [ -d "$REPO/docs/experience" ]; then
    cp -R "$REPO/docs/experience" "$STAGE/docs/"
  fi
  if [ -d "$REPO/docs/research" ]; then
    cp -R "$REPO/docs/research" "$STAGE/docs/"
  fi
fi
# 本机 launchd 定时任务的源文件（看门狗 / 快照 / hooks 看门狗 / 3000 巡检 /
# 日志轮转）。它们以前散落在 ~/.codex-guard/ 这类「没有版本、没人升级」的位置，
# install 永远写不到，于是发版之后它们还在跑几周前的代码。搬进版本目录后，
# 下面 5.7 段会把 plist 改指到这里。
if [ -d "$REPO/ops/guards" ]; then
  cp -R "$REPO/ops/guards" "$STAGE/ops/"
  chmod +x "$STAGE"/ops/guards/*.sh "$STAGE"/ops/guards/*.py 2>/dev/null || true
fi
cp "$REPO/preview-check.sh" "$STAGE/preview-check.sh"
chmod +x "$STAGE/preview-check.sh"
"$PY" - "$STAGE/.codex-autopilot-source.json" "$VERSION" "$SOURCE_COMMIT" <<'PYEOF'
import json, os, sys, tempfile
path, version, commit = sys.argv[1:4]
data = {"version": version, "commit": commit or None, "dirty": not bool(commit)}
fd, tmp = tempfile.mkstemp(prefix=".source-meta-", dir=os.path.dirname(path))
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
PYEOF
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
import json, os, sys, tempfile
from pathlib import Path
path, python, project, trunk = sys.argv[1:5]
env = {"KG_REPO": project}
if trunk:
    env["KG_TRUNK"] = trunk
data = {"mcpServers": {"codex-autopilot": {
    "command": python,
    "args": ["./ops/central_mcp.py"],
    "cwd": ".",
    "env": env,
    # MCP Apps tools must remain direct functions. The host's deferred bridge
    # emits plain function_call items and cannot produce native mcpToolCall UI.
    "omit_tools_from": ["deferred"],
}}}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
# 版本自带的 central.config.json 必须反映本次安装的 trunk 解析结果。
# 只写「显式 --trunk」不够：新 HOME 没有默认 trunk 时，旧 release 复制过来的
# 陈旧 trunk_dir 会留在配置里，product_eval 判据 5 拿它做祖先比较而永久误红。
ops_config = Path(path).parent / "ops" / "central.config.json"
if ops_config.is_file():
    with ops_config.open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["trunk_dir"] = trunk or None
    fd, tmp = tempfile.mkstemp(prefix=".central-config-", dir=str(ops_config.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(ops_config))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
print(f"已写 {path}")
print(f"  运行时代码 = {path.rsplit('/', 1)[0]}/ops/central_mcp.py")
print(f"  状态根     = {project}")
if trunk:
    print(f"  落盘主干   = {trunk}")
    if ops_config.is_file():
        print(f"  发布评估主干配置 = {ops_config}")
elif ops_config.is_file():
    print("  落盘主干   = 未解析；已清空版本内陈旧 trunk_dir 配置")
PYEOF
if ! "$PY" -m py_compile "$STAGE"/ops/*.py; then
  echo "× 新版本 Python 预检失败，当前安装入口未更改。" >&2
  exit 1
fi
# 安装后的 release 根目录既是插件根，也是门禁执行根。契约、采用策略与两份校验器
# 缺任何一项，面板都会失去可核验的真相来源；在移动 staging 之前硬失败。
for required in \
  docs/PROJECT_GLOBAL_RULES.md \
  docs/experience/panel-renderer-contract.json \
  docs/research/adoption-strategy.json \
  ops/panel_renderer_contract.py \
  ops/research_adoption.py \
  ops/test-panel-renderer-contract.py \
  ops/test-research-adoption.py; do
  if [ ! -f "$STAGE/$required" ]; then
    echo "× release 包缺少必需契约文件：$required" >&2
    exit 1
  fi
done
# 绑定自检：从即将发布的这份副本按 .mcp.json 解析状态根，必须证明真的落账。
# 这个检查放在移动版本目录之前，失败只删除 staging，不影响现有安装。
if ! "$PY" - "$STAGE/ops/plugin_binding.py" <<'PYEOF'
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("binding_check", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
# 这里验证的是刚写好的 .mcp.json 绑定，不是调用 install.sh 的 shell 里
# 可能残留的 KG_REPO/KG_TRUNK。继承它们会把安装自检误判成项目漂移。
os.environ.pop("KG_REPO", None)
os.environ.pop("KG_TRUNK", None)
# 安装夹具可能故意带未提交项来验证来源；这里只校验刚写好的 .mcp.json 绑定，
# 不做「当前会话 cwd 指向另一个干净状态根」的用户侧漂移检测。
os.environ["CODEX_AUTOPILOT_SKIP_WORKTREE_DRIFT"] = "1"
h = mod.binding_health()
if h.get("status") == "broken":
    print("× 安装绑定自检失败：", file=sys.stderr)
    for p in h.get("problems") or ["原因未知"]:
        print("  - " + p, file=sys.stderr)
    sys.exit(1)
print("安装绑定自检通过：" + str(h.get("repo") or "传统布局"))
PYEOF
then
  echo "× 新版本绑定预检失败，当前安装入口未更改。" >&2
  exit 1
fi
mv "$STAGE" "$DEST"
STAGE=""
echo "已安装不可变版本 $VERSION 到 $DEST"

# 4) 登记进个人 marketplace
if [ "$NO_MARKET" -eq 0 ]; then
  "$PY" - "$MARKET" "$INSTALL_STATE" "$VERSION" "$SOURCE_COMMIT" <<'PYEOF'
import json, os, shutil, sys, tempfile
from pathlib import Path
path, state_path, version, source_commit = sys.argv[1:5]
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
existing_path = ((existing or {}).get("source") or {}).get("path")
try:
    with open(state_path, encoding="utf-8") as fh:
        install_state = json.load(fh)
except (OSError, ValueError):
    install_state = {}
# 同版本目录被移除后重装时，marketplace 已经指向当前 source。此时若把它
# 直接写成 previous_source，回滚会原地回到同一版本，真正的上一版就丢了。
if (install_state.get("version") == version
        and install_state.get("source") == source_path
        and "previous_source" in install_state):
    previous = install_state.get("previous_source")
else:
    previous = existing_path
if previous == source_path:
    previous = None
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
# 给 rollback 留下两层指针：previous_source 是紧邻上一代，older_source 是再上一代。
# 回滚后把 older_source 提升为 previous_source，才能连续回退而不是原地打转。
rollback_source_commit = None
if previous:
    marker = "./plugins/codex-autopilot/releases/"
    previous_version = previous.rsplit(marker, 1)[-1] if marker in previous else ""
    previous_meta = Path(os.path.expanduser("~")) / "plugins" / "codex-autopilot" / "releases" / previous_version / ".codex-autopilot-source.json"
    try:
        previous_data = json.loads(previous_meta.read_text(encoding="utf-8"))
        if previous_data.get("commit") and not previous_data.get("dirty"):
            rollback_source_commit = previous_data["commit"]
    except (OSError, ValueError):
        pass
state = {"version": version, "source": source_path, "previous_source": previous,
         "older_source": install_state.get("previous_source"),
         "rollback_source_commit": rollback_source_commit,
         "source_commit": source_commit or None}
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

migrate_local_secrets
install_local_integrations "$ENABLE_LOCAL_GUARDS"
if [ "$ENABLE_LOCAL_GUARDS" -eq 0 ] && [ "$INSTALL_DAEMON" -eq 0 ] && \
   [ "$MIGRATE_LOCAL_SECRETS" -eq 0 ]; then
  echo
  echo "本机集成按默认策略跳过：未改 launchd、hooks.json、本地密钥或 provider。"
  echo "只启用已装版本的本机集成：./install.sh --enable-local-guards"
  echo "首次安装常驻守护：./install.sh --install-daemon"
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
