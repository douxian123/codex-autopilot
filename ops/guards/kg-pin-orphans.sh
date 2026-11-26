#!/usr/bin/env bash
#
# 把所有「任何 ref 都够不到」的提交钉成 preserve/orphan/<sha>，并把本地已有的
# 全部 preserve/* 救援 ref 对齐到远端（本地有、远端缺的补推）。
#
# 为什么需要它
# ------------
# 本仓库里有多条自动/半自动路径会短暂生成 stash 形态的提交对
# （`WIP on <branch>: ...` + `index on <branch>: ...`）。这些提交一旦被 drop，
# 就不再被任何 ref 引用，只剩对象库里的游离对象。`git gc` 按默认
# gc.pruneExpire（两周）会把它们静默删掉——那里面可能是别人尚未提交的真实改动。
#
# 判据是权威差集：全部本地 commit 对象 − 全部 ref 可达 commit。
# 刻意不使用 `git fsck --unreachable`：本仓库是 shallow + blob:none 部分克隆，
# fsck 既误报又漏报（与 preserve-sweep.sh 的结论一致）。
#
# 为什么「建 ref」和「推远端」必须分开
# ------------------------------------
# 本脚本早先的写法是：新建本地 ref 时顺手 push，而本地已存在的 ref 直接
# `continue` 跳过。这留下一个静默缺口——一旦某条 ref 的 push 失败（网络抖动、
# 远端瞬时拒绝），本地 ref 已经建成；下一次运行时该提交因为「本地 ref 可达」
# 已不再属于孤儿集合，于是永远不再进入那个分支，那条 ref 就永久停在「只在本地、
# 远端没有」的状态，脚本还照样报成功。2026-11-17 实测抓到这个状态：
# 远端缺 4 条、本地缺 0 条，而守护日志显示「本轮新建 0 条，推送失败 0 条」。
#
# 因此本脚本分成两趟：
#   1) 建本地 ref：只把「本轮新发现、本地还没有」的孤儿钉住；
#   2) 对齐远端：扫描**全部**本地 preserve/* ref，凡远端没有的一律补推。
# 第 2 趟是幂等的，可以无限重跑；push 失败不再永久卡死，下次运行自动续上。
#
# 为什么第 2 趟是 preserve/* 而不是只 preserve/orphan
# ----------------------------------------------------
# 第 1 趟只钉 preserve/orphan/<sha>，但仓库里还有别的救援路径也会在本地建
# preserve/* ref（preserve/dangling-*、preserve/sweep-*、preserve/inflight/*、
# preserve/reaped/*、preserve/local-*、preserve/stash-backup-* 等）。上一版第 2 趟
# 只对齐 preserve/orphan 一个前缀，这些前缀的「本地有、远端缺」没人补。2026-11-17
# 实测：本地 preserve/* 中远端缺的只有 1 条，恰是 preserve/dangling-20261116-0606/100；
# 它本身无丢失风险（同一对象已由 preserve/orphan/* 在远端可达），但暴露了前缀级缺口。
# 第 2 趟因此泛化为「本地 preserve/* 集合 − 远端 preserve/* 集合」，一次覆盖所有
# 救援前缀，不再逐前缀补丁。
#
# 安全边界
# --------
# - 新建 ref 只限 refs/heads/preserve/orphan/<sha>，永不覆盖、永不带 --force。
# - 第 2 趟只 push「远端确实缺失」的 preserve/* ref，且逐个同名 ref 补推。
# - 不 checkout / reset / stash / clean / gc，工作区一个字节不动。
# - 不删任何 ref，不改 main。
#
# 用法
# ----
#   ./scripts/pin-orphan-commits.sh              # 钉住本地并补齐远端
#   ./scripts/pin-orphan-commits.sh --dry-run    # 只打印将钉什么、将补推什么
#   ./scripts/pin-orphan-commits.sh --no-push    # 只建本地 ref，不碰远端

set -euo pipefail

DRY_RUN=0
NO_PUSH=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-push) NO_PUSH=1 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

PREFIX="refs/heads/preserve/orphan"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/kg-pin-orphans.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

export GIT_NO_LAZY_FETCH=1

git cat-file --batch-all-objects --batch-check='%(objectname) %(objecttype)' 2>/dev/null \
  | awk '$2=="commit"{print $1}' | sort -u > "$TMP/all"
git rev-list --all 2>/dev/null | sort -u > "$TMP/reach"
comm -23 "$TMP/all" "$TMP/reach" > "$TMP/orphan"

total="$(wc -l < "$TMP/orphan" | tr -d ' ')"
echo "== 仓库: $REPO_ROOT"
echo "== 无任何 ref 可达的提交: $total"
[[ "$DRY_RUN" == "1" ]] && echo "== 模式: dry-run（不建 ref、不推送）"
[[ "$NO_PUSH" == "1" ]] && echo "== 模式: --no-push（只建本地 ref，不碰远端）"

created=0
while IFS= read -r sha; do
  [[ -z "$sha" ]] && continue
  target="$PREFIX/$sha"
  if git show-ref --verify --quiet "$target"; then
    continue
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "WOULD pin ${sha:0:12}"
    created=$((created + 1))
    continue
  fi
  git update-ref "$target" "$sha"
  created=$((created + 1))
  echo "PIN ${sha:0:12}（本地）"
done < "$TMP/orphan"

echo "== 本轮新建本地 ref $created 条"

git rev-list --all 2>/dev/null | sort -u > "$TMP/reach-after"
left="$(comm -23 "$TMP/all" "$TMP/reach-after" | wc -l | tr -d ' ')"
echo "== 钉完后仍无 ref 可达: $left"

# ---- 第二趟：把本地已有的全部 preserve/* ref 对齐到远端 ----
#
# 这一趟是幂等重试点。它不看「本轮新建了什么」，而是看「本地集合 − 远端集合」，
# 因此上一轮 push 失败的 ref 会在这一轮、以及之后每一轮被重试，直到成功。
push_failed=0
if [[ "$NO_PUSH" == "1" ]]; then
  echo "== 跳过远端对齐（--no-push）"
else
  # 本地集合与远端集合都取完整 ref 名，同一坐标系才能做差集（不再裁剪前缀）。
  git for-each-ref --format='%(refname)' 'refs/heads/preserve/' 2>/dev/null | sort -u > "$TMP/local-refs"
  # 网络失败时 ls-remote 返回非 0、输出可能为空；空集合绝不能被当成「远端全缺」，
  # 否则会退化成 3800+ 条全量推送。因此先判退出码，失败就整体跳过本趟。
  if git ls-remote --heads origin 'refs/heads/preserve/*' > "$TMP/remote-raw" 2>/dev/null; then
    awk '{print $2}' "$TMP/remote-raw" | sort -u > "$TMP/remote-refs"
    comm -23 "$TMP/local-refs" "$TMP/remote-refs" > "$TMP/missing-remote"
    missing="$(wc -l < "$TMP/missing-remote" | tr -d ' ')"
    local_total="$(wc -l < "$TMP/local-refs" | tr -d ' ')"
    echo "== 本地 preserve/* $local_total 条，远端缺 $missing 条"
    while IFS= read -r ref; do
      [[ -z "$ref" ]] && continue
      if [[ "$DRY_RUN" == "1" ]]; then
        echo "WOULD push ${ref#refs/heads/preserve/}"
        continue
      fi
      if git push origin "$ref:$ref" >/dev/null 2>&1; then
        echo "PUSH ${ref#refs/heads/preserve/} -> 远端"
      else
        echo "FAIL 补推 ${ref#refs/heads/preserve/} 失败；本地 ref 在，下次运行会续推"
        push_failed=$((push_failed + 1))
      fi
    done < "$TMP/missing-remote"
    echo "== 远端对齐补推失败 $push_failed 条"
  else
    echo "WARN 远端对齐跳过：git ls-remote 失败（网络或远端不可达）"
    push_failed=1
  fi
fi

[[ "$left" == "0" && "$push_failed" == "0" ]]
