#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：旧会话压缩必须在 launchd 的精简 PATH 下也能用。

为什么有这条（用户实测 2026-11-11）：
  `com.danta.codex-disk-reap` 由 launchd 拉起，环境里 PATH 只有
  /usr/bin:/bin:/usr/sbin:/sbin，而本机 zstd 只装在 /opt/homebrew/bin。
  于是 `compact_session_logs` 每小时对每个候选文件都抛一次
  `FileNotFoundError: 'zstd'`，一个字节都没压过 —— 它明明是磁盘压力下
  的止血手段，却长期空转，磁盘反而一直停在压力模式。

  反向断言同样重要：真找不到 zstd 时必须「保留全部原文件并只报一次」，
  绝不能在缺工具时误删任何会话历史。
"""
import importlib.util
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load_disk_reap(log_path: Path | None = None):
    spec = importlib.util.spec_from_file_location("disk_reap_zstd", OPS / "disk_reap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 本门禁会真的跑 compact_session_logs，而它的 log() 写模块级全局 LOG。
    # 不重定向的话，「压缩旧会话 old.jsonl: ...」会落进真实运行日志，把一条
    # 合成夹具的压缩记录伪装成生产事件（实测 2026-11-11 反复出现）。
    if log_path is not None:
        mod.LOG = log_path
    return mod


def make_old_session(root: Path) -> Path:
    sessions = root / ".codex/sessions/2026/01/01"
    sessions.mkdir(parents=True)
    old = sessions / "old.jsonl"
    old.write_bytes(b'{"type":"event","payload":"keep me"}\n' * 900_000)
    old.touch()
    ts = old.stat().st_mtime
    os.utime(old, (ts - 7 * 3600, ts - 7 * 3600))
    return old


def main():
    log_sink = Path(tempfile.mkdtemp(prefix="reap-zstd-log-")) / "disk-reap.log"
    mod = load_disk_reap(log_sink)

    # 1) 精简 PATH 下仍能定位 zstd：证明不再依赖 PATH。
    with mock.patch.dict("os.environ", {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, clear=False):
        found = mod._zstd_path()
    check("精简 PATH 下仍能定位 zstd", bool(found) and os.access(found, os.X_OK), found)

    # 2) 精简 PATH + 低盘 → 真压缩且无损。
    with tempfile.TemporaryDirectory(prefix="reap-zstd-") as tmp:
        root = Path(tmp)
        old = make_old_session(root)
        with mock.patch.dict("os.environ",
                             {"CODEX_HOME": str(root / ".codex"),
                              "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}), \
             mock.patch.object(mod, "free_gb", return_value=1.0):
            n, freed = mod.compact_session_logs()
        check("精简 PATH 下真的压掉了旧会话", n == 1 and freed > 0, (n, freed))
        check("原文件已替换为 .zst", (not old.exists())
              and old.with_name(old.name + ".zst").is_file())

    # 3) 反向：真的没有 zstd 时，保留全部原文件且不留半截产物。
    with tempfile.TemporaryDirectory(prefix="reap-zstd-") as tmp:
        root = Path(tmp)
        old = make_old_session(root)
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(root / ".codex")}), \
             mock.patch.object(mod, "free_gb", return_value=1.0), \
             mock.patch.object(mod, "_zstd_path", return_value=None):
            n, freed = mod.compact_session_logs()
        check("缺 zstd 时不删任何原文件", n == 0 and freed == 0 and old.is_file())
        check("缺 zstd 时不留半截 .zst / .tmp",
              not old.with_name(old.name + ".zst").exists()
              and not old.with_name(old.name + ".zst.tmp").exists())

    if failures:
        print(f"\nreap zstd path unit: FAIL ({len(failures)})")
        raise SystemExit(1)
    print("\nreap zstd path unit: PASS")


if __name__ == "__main__":
    main()
