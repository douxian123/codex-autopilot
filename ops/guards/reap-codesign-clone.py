#!/usr/bin/env python3
"""回收 Chrome / 豆包 遗留的 code_sign_clone 临时克隆目录。

背景（2026-10-23 实测）：Chrome 每次签名更新会在
/var/folders/<xx>/<yy>/X/com.google.Chrome.code_sign_clone/ 下克隆一份
完整 .app（每个约 1.4GB），用完不删。当天累积 10 个 = 15GB，直接把磁盘
从 7.6GB 压到 3.5GB，触发守护的派活下限，看起来「跑不动」。

安全判据（任何一条不满足就不删）：
  1. 目录名必须匹配 code_sign_clone.*
  2. 必须有进程正引用它（lsof）就跳过 —— 那是正在用的那份
  3. 修改时间必须超过 GRACE_MIN 分钟
只删这些临时克隆本身，不碰任何项目文件、分支、worktree、用户数据。
"""
import os
import shutil
import subprocess
import sys
import time

GRACE_MIN = 20
TARGETS = [
    "com.google.Chrome.code_sign_clone",
    "com.bot.pc.doubao.browser.code_sign_clone",
]


def _xdirs():
    tmp = os.environ.get("TMPDIR", "/var/folders").rstrip("/")
    seen = set()
    for base in (os.path.dirname(tmp), tmp, "/var/folders"):
        cur = base
        for _ in range(4):
            cand = os.path.join(cur, "X")
            if os.path.isdir(cand) and cand not in seen:
                seen.add(cand)
                yield cand
            cur = os.path.dirname(cur)


def _in_use(path: str) -> bool:
    try:
        out = subprocess.run(["lsof", "+D", path],
                             capture_output=True, text=True, timeout=90).stdout
        return bool(out.strip())
    except Exception:
        return True          # 查不出来就当在用，宁可不删


def main() -> int:
    freed = 0
    removed = 0
    cutoff = time.time() - GRACE_MIN * 60
    for xdir in _xdirs():
        for name in TARGETS:
            root = os.path.join(xdir, name)
            if not os.path.isdir(root):
                continue
            for ent in sorted(os.listdir(root)):
                if not ent.startswith("code_sign_clone."):
                    continue
                p = os.path.join(root, ent)
                if not os.path.isdir(p):
                    continue
                try:
                    if os.path.getmtime(p) > cutoff:
                        continue
                except OSError:
                    continue
                if _in_use(p):
                    continue
                size = 0
                for r, _d, fs in os.walk(p):
                    for f in fs:
                        fp = os.path.join(r, f)
                        try:
                            size += os.lstat(fp).st_size
                        except OSError:
                            pass
                try:
                    shutil.rmtree(p)
                except Exception as exc:
                    print(f"skip {p}: {exc}")
                    continue
                removed += 1
                freed += size
                print(f"reaped {ent} {size / 1e9:.2f}GB")
    if removed:
        print(f"total reaped {removed} dirs, {freed / 1e9:.2f}GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
