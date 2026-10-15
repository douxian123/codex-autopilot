#!/usr/bin/env bash
# 编译并打包 Codex 自动挡的 macOS 菜单栏 Companion。
#
# 为什么不直接用 `swift build`：
#   1. 本机可能只有 Command Line Tools（没装 Xcode），SwiftPM 自己会再套一层
#      沙箱，在受限环境里直接失败；
#   2. SwiftPM 还要写 ~/Library/Caches/org.swift.swiftpm，受限时同样失败。
# 所以这里直接调 swiftc 编这一个文件，过程可控，报错也直给。
#
# 一个真实踩过的坑（值得记住，否则会误判成「Swift 装坏了」）：
#   默认的 clang 模块缓存写在 ~/.cache/clang/ModuleCache。这个目录不可写时，
#   swiftc 会先报「缓存写不进去」，紧接着再报一句
#   “this SDK is not supported by the compiler”。后一句是假象。
#   因此本脚本把模块缓存钉在仓库内的 .build/module-cache，并把 SDK 选择做成兜底。
#
# 用法：
#   ./build.sh                 # 打包到 companion/dist/CodexAutopilotCompanion.app
#   ./build.sh --binary-only    # 只编译可执行文件，不打包成 .app
#   CODEX_AUTOPILOT_SDK=/path/to/MacOSX.sdk ./build.sh   # 指定 SDK
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$HERE/Sources/CodexAutopilotCompanion/main.swift"
BUILD="$HERE/.build/packaged"
DIST="$HERE/dist"
APP="$DIST/CodexAutopilotCompanion.app"
BIN="$BUILD/CodexAutopilotCompanion"
MODCACHE="$HERE/.build/module-cache"
BINARY_ONLY=0
[ "${1:-}" = "--binary-only" ] && BINARY_ONLY=1

if ! command -v swiftc >/dev/null 2>&1; then
  echo "× 找不到 swiftc。请先装 Xcode Command Line Tools：xcode-select --install" >&2
  exit 1
fi

mkdir -p "$BUILD" "$DIST" "$MODCACHE"

compile() {  # $1 = SDK 路径；空字符串表示用本机默认 SDK
  local sdk="$1"
  local args=(-O -parse-as-library
              -module-cache-path "$MODCACHE"
              -target arm64-apple-macos13.0
              -framework AppKit -framework SwiftUI -framework Carbon
              -o "$BIN" "$SOURCE")
  if [ -n "$sdk" ]; then
    swiftc -sdk "$sdk" "${args[@]}"
  else
    swiftc "${args[@]}"
  fi
}

if [ -n "${CODEX_AUTOPILOT_SDK:-}" ]; then
  echo "使用指定 SDK：$CODEX_AUTOPILOT_SDK"
  compile "$CODEX_AUTOPILOT_SDK"
elif compile ""; then
  echo "使用本机默认 SDK 编译成功"
else
  echo "默认 SDK 编译失败，逐个试本机其他 SDK…" >&2
  FOUND=0
  for sdk in /Library/Developer/CommandLineTools/SDKs/*.sdk \
             /Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/*.sdk; do
    [ -d "$sdk" ] || continue
    echo "  试 $(basename "$sdk")"
    if compile "$sdk"; then
      echo "用 $sdk 编译成功"
      FOUND=1
      break
    fi
  done
  if [ "$FOUND" -eq 0 ]; then
    echo "× 本机没有找到能用的 macOS SDK。" >&2
    echo "  装一次完整 Xcode，或指定：CODEX_AUTOPILOT_SDK=/path/to/MacOSX.sdk ./build.sh" >&2
    exit 1
  fi
fi
echo "已编译：$BIN"

if [ "$BINARY_ONLY" -eq 1 ]; then
  exit 0
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/CodexAutopilotCompanion"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"
# 本机自用：ad-hoc 签名。签名失败只影响首次启动的提示强度，不影响能不能跑。
if command -v codesign >/dev/null 2>&1; then
  if codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1; then
    echo "已 ad-hoc 签名"
  else
    echo "（ad-hoc 签名跳过：本机 codesign 不可用，不影响使用）"
  fi
fi
echo "已打包：$APP"
