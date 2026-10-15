#!/usr/bin/env bash
# Codex 自动挡 · 预览包一条命令验收
#
# 它把「装好 -> 菜单栏应用起来 -> 原生直连自检 -> 总闸状态」串成一次运行，
# 每项打印 PASS/FAIL 和真实路径，退出码 = 失败项数。装不了/连不上都如实报，
# 不把没跑成的说成通过。
#
# 用法：
#   bash preview-check.sh              # 装（若未装）+ 打开菜单栏应用 + 全量自检
#   bash preview-check.sh --no-open    # 不自动打开应用（远程/无 GUI 时用）
#   bash preview-check.sh --no-install # 只自检，不动安装
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPEN_APP=1
DO_INSTALL=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-open) OPEN_APP=0; shift ;;
    --no-install) DO_INSTALL=0; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$REPO/plugin/.codex-plugin/plugin.json" | head -1)"
[ -n "${VERSION}" ] || { echo "读不到插件版本，仓库不完整？" >&2; exit 2; }

RELEASE_DIR="${HOME}/plugins/codex-autopilot/releases/${VERSION}"
MARKET="${HOME}/.agents/plugins/marketplace.json"
APP="${HOME}/Applications/CodexAutopilotCompanion.app"
CONF="${HOME}/Library/Application Support/CodexAutopilot/companion.json"

FAILS=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
info() { printf '        %s\n' "$1"; }
head_() { printf '\n[%s] %s\n' "$1" "$2"; }

echo "Codex 自动挡 预览包验收  version=${VERSION}"
echo "仓库：${REPO}"

head_ 1 "安装"
if [ "${DO_INSTALL}" -eq 1 ]; then
  if [ -f "${RELEASE_DIR}/ops/central_mcp.py" ]; then
    pass "同版本已安装，跳过（避免覆盖不可变版本）"
    info "版本目录：${RELEASE_DIR}"
  else
    if bash "$REPO/install.sh" --no-seed; then
      pass "安装完成"
    else
      fail "安装失败（看上面输出）"
    fi
  fi
else
  info "按 --no-install 跳过安装"
fi

head_ 2 "落地产物"
if [ -f "${RELEASE_DIR}/ops/central_mcp.py" ]; then
  pass "版本目录已落地"
else
  fail "版本目录缺失：${RELEASE_DIR}"
fi

if [ -f "${APP}/Contents/MacOS/CodexAutopilotCompanion" ]; then
  pass "菜单栏应用已安装"
  info "${APP}"
  if [ -x "${APP}/Contents/MacOS/CodexAutopilotCompanion" ]; then
    pass "应用可执行"
  else
    fail "应用不可执行"
  fi
  if codesign -dv "${APP}" >/dev/null 2>&1; then
    pass "应用已签名"
  else
    fail "应用未签名（可能被 Gatekeeper 拦）"
  fi
else
  fail "菜单栏应用缺失：${APP}"
  info "如果本机没有 swiftc：装 Xcode Command Line Tools 后跑 bash install.sh"
fi

if [ -f "${MARKET}" ]; then
  if python3 - "${MARKET}" "${VERSION}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
want = f"./plugins/codex-autopilot/releases/{sys.argv[2]}"
for p in data.get("plugins", []):
    if p.get("name") == "codex-autopilot":
        sys.exit(0 if p.get("source", {}).get("path") == want else 1)
sys.exit(1)
PY
  then
    pass "插件市场指向该版本"
  else
    fail "插件市场未指向该版本（Codex 里可能还是旧版）"
  fi
else
  fail "找不到插件市场清单：${MARKET}"
fi

if [ -f "${CONF}" ]; then
  pass "Companion 配置已写入"
else
  fail "Companion 配置缺失：${CONF}"
fi

head_ 3 "菜单栏应用"
if [ "${OPEN_APP}" -eq 1 ]; then
  if open "${APP}" 2>/dev/null; then
    pass "已请求启动菜单栏应用"
    info "菜单栏应出现自动挡图标；⌥⌘D 打开完整面板，⌥⌘S 截图派活"
  else
    fail "启动失败（在真实桌面会话里重试：open \"${APP}\"）"
  fi
else
  info "按 --no-open 跳过启动"
fi

head_ 4 "原生直连自检"
PROBE="$(cd "$REPO" && python3 ops/codex_native.py probe 2>/dev/null)"
PROBE_LINES="$(printf '%s' "${PROBE}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); print(""); raise SystemExit(0)
r = d.get("result") if isinstance(d.get("result"), dict) else d
def flat(key):
    return (r.get(key) or "").replace("\n", " ").replace("\r", " ")[:200]
print(flat("reason")); print(flat("hint"))
' 2>/dev/null)"
PROBE_REASON="$(printf '%s' "${PROBE_LINES}" | sed -n '1p')"
PROBE_HINT="$(printf '%s' "${PROBE_LINES}" | sed -n '2p')"
if printf '%s' "${PROBE}" | grep -q '"can_dispatch": true'; then
  pass "原生直连已连通：可以把任务派进 Codex 窗口"
else
  fail "原生直连不可派活（只读历史可用）"
  [ -n "${PROBE_REASON}" ] && info "原因：${PROBE_REASON}"
  [ -n "${PROBE_HINT}" ] && info "提示：${PROBE_HINT}"
fi

head_ 5 "总闸"
STATUS="$(cd "$REPO" && python3 ops/companion_api.py status 2>/dev/null)"
if printf '%s' "${STATUS}" | grep -q '"ok": true'; then
  LIFECYCLE="$(printf '%s' "${STATUS}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("lifecycle"))' 2>/dev/null)"
  pass "统一控制通道可用（总闸状态：${LIFECYCLE}）"
else
  fail "统一控制通道不可用"
fi

printf '\n---------------- 结果 ----------------\n'
if [ "${FAILS}" -eq 0 ]; then
  echo "全部通过（0 项失败）。"
else
  echo "失败 ${FAILS} 项（见上面 FAIL 行）。"
fi
echo "完整说明：${REPO}/docs/PREVIEW.md"
exit "${FAILS}"
