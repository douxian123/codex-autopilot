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
    check("签名临时副本默认宽限期为 2 分钟",
          mod.CODEX_SIGN_CLONE_GRACE_MIN == 2.0,
          mod.CODEX_SIGN_CLONE_GRACE_MIN)

    # 2) 默认在低盘压力下启用；高盘时不动任何历史。
    with tempfile.TemporaryDirectory(prefix="reap-zstd-") as tmp:
        root = Path(tmp)
        old = make_old_session(root)
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(root / ".codex")}), \
             mock.patch.object(mod, "free_gb", return_value=100.0):
            os.environ.pop("KG_SESSION_COMPACT", None)
            n, freed = mod.compact_session_logs()
        check("高盘时空闲时不动历史会话", n == 0 and freed == 0 and old.is_file(),
              (n, freed, old.exists()))

    # 3) 显式 opt-in + 精简 PATH + 低盘 → 才允许真归档且无损。
    with tempfile.TemporaryDirectory(prefix="reap-zstd-") as tmp:
        root = Path(tmp)
        old = make_old_session(root)
        with mock.patch.dict("os.environ",
                             {"CODEX_HOME": str(root / ".codex"),
                              "KG_SESSION_COMPACT": "1",
                              "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}), \
             mock.patch.object(mod, "free_gb", return_value=1.0), \
             mock.patch.object(mod, "SESSION_COMPACT_QUIET_H", 1.0), \
             mock.patch.object(mod, "SESSION_COMPACT_RECENT_WRITE_MIN", 0.0):
            n, freed = mod.compact_session_logs()
        archive = root / ".codex/session-archive/2026/01/01/old.jsonl.zst"
        check("精简 PATH 下真的归档了旧会话", n == 1 and freed > 0, (n, freed))
        check("sessions 里不残留 .zst，归档在独立目录",
              not old.exists()
              and not old.with_name(old.name + ".zst").exists()
              and archive.is_file())

    # 4) 反向：显式开启但真的没有 zstd 时，仍保留全部原文件且不留半截产物。
    with tempfile.TemporaryDirectory(prefix="reap-zstd-") as tmp:
        root = Path(tmp)
        old = make_old_session(root)
        with mock.patch.dict("os.environ",
                             {"CODEX_HOME": str(root / ".codex"),
                              "KG_SESSION_COMPACT": "1"}), \
             mock.patch.object(mod, "free_gb", return_value=1.0), \
             mock.patch.object(mod, "_zstd_path", return_value=None), \
             mock.patch.object(mod, "SESSION_COMPACT_QUIET_H", 1.0), \
             mock.patch.object(mod, "SESSION_COMPACT_RECENT_WRITE_MIN", 0.0):
            n, freed = mod.compact_session_logs()
        check("缺 zstd 时不删任何原文件", n == 0 and freed == 0 and old.is_file())
        check("缺 zstd 时不留半截 .zst / .tmp",
              not old.with_name(old.name + ".zst").exists()
              and not old.with_name(old.name + ".zst.tmp").exists())

    # 5) 文件被进程打开时绝不替换；压缩后、删除前再次打开也保守保留。
    with tempfile.TemporaryDirectory(prefix="reap-zstd-") as tmp:
        root = Path(tmp)
        old = make_old_session(root)
        with mock.patch.dict("os.environ",
                             {"CODEX_HOME": str(root / ".codex"),
                              "KG_SESSION_COMPACT": "1"}), \
             mock.patch.object(mod, "free_gb", return_value=1.0), \
             mock.patch.object(mod, "_path_is_open", return_value=True), \
             mock.patch.object(mod, "SESSION_COMPACT_QUIET_H", 1.0), \
             mock.patch.object(mod, "SESSION_COMPACT_RECENT_WRITE_MIN", 0.0):
            n, freed = mod.compact_session_logs()
        check("被打开时保留原文件且不创建归档", n == 0 and old.is_file()
              and not (root / ".codex/session-archive").exists(), (n, freed))

    with tempfile.TemporaryDirectory(prefix="reap-zstd-") as tmp:
        root = Path(tmp)
        old = make_old_session(root)
        with mock.patch.dict("os.environ",
                             {"CODEX_HOME": str(root / ".codex"),
                              "KG_SESSION_COMPACT": "1"}), \
             mock.patch.object(mod, "free_gb", return_value=1.0), \
             mock.patch.object(mod, "_path_is_open", side_effect=[False, True]), \
             mock.patch.object(mod, "SESSION_COMPACT_QUIET_H", 1.0), \
             mock.patch.object(mod, "SESSION_COMPACT_RECENT_WRITE_MIN", 0.0):
            n, freed = mod.compact_session_logs()
        archive_root = root / ".codex/session-archive"
        leftovers = list(archive_root.rglob("*.zst")) + list(archive_root.rglob("*.tmp")) if archive_root.exists() else []
        check("压缩后、删除前再次打开时不替换原文件",
              n == 0 and old.is_file() and not leftovers, (n, freed, leftovers))

    # 6) 默认低盘压力会自动启用；最近仍在写入的旧会话绝不进候选。
    with tempfile.TemporaryDirectory(prefix="reap-zstd-") as tmp:
        root = Path(tmp)
        old = make_old_session(root)
        old_ts = time.time() - 25 * 3600
        os.utime(old, (old_ts, old_ts))
        sessions = old.parent
        hot = sessions / "hot.jsonl"
        hot.write_bytes(b'{"type":"event","payload":"hot"}\n' * 900_000)
        # Keep hot much newer than old. The previous fixture gave both files
        # the same mtime, which cannot test a recency exclusion.
        hot_ts = time.time() - 3600
        os.utime(hot, (hot_ts, hot_ts))
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(root / ".codex")}), \
             mock.patch.object(mod, "free_gb", return_value=1.0), \
             mock.patch.object(mod, "SESSION_COMPACT_MAX_FILES", 1):
            os.environ.pop("KG_SESSION_COMPACT", None)
            n, freed = mod.compact_session_logs()
        check("低盘默认不自动迁移历史会话", n == 0 and freed == 0 and old.is_file(), (n, freed))
        with mock.patch.dict("os.environ",
                             {"CODEX_HOME": str(root / ".codex"),
                              "KG_SESSION_COMPACT": "1"}), \
             mock.patch.object(mod, "free_gb", return_value=1.0), \
             mock.patch.object(mod, "SESSION_COMPACT_MAX_FILES", 1):
            n, freed = mod.compact_session_logs()
        check("显式开启后每轮受预算限制",
              n == 1 and freed > 0 and not old.exists(), (n, freed))
        check("最近仍在写入的大会话保持原样", hot.is_file())

    if failures:
        print(f"\nreap zstd path unit: FAIL ({len(failures)})")
        raise SystemExit(1)
    print("\nreap zstd path unit: PASS")


if __name__ == "__main__":
    main()
