#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""control_state —— 自动挡的「用户级开关」（stdlib only）。

为什么单独抽出来
----------------
自动挡的开关有两个来源，必须分清：

  * 项目级（ops/central.config.json）：阈值、泳道、主干、验证门禁。
    这些随项目走，由项目自己决定。
  * 用户级（本文件的 ~/.codex-autopilot/control.json）：暂停 / 三个自动动作 /
    常驻开关。这些是「用户此刻想不想让它动手」，属于这台机器上的人，不属于项目。

分开的原因很直接：如果用户级开关也写进项目文件，那么用户在面板上按下的每一个
按钮都会变成一次 git 改动——面板反而成了新的脏工作区来源。用户级开关只落本机，
不进版本库，也不要求用户先提交。

契约
----
  * 文件损坏 / 读不出来 -> 安全暂停（paused=True）并记一条 error，宁可不动手，
    也不要在用户以为「已暂停」的时候继续合分支。
  * 原子写（临时文件 + os.replace），进程被打断不会留下半截 JSON。
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

DEFAULTS = {
    "paused": False,
    "auto_land": True,
    "auto_resolve": True,
    "auto_port": False,
    # 执行闭环：按台账里的未决问题，真的起一个 codex 会话去实现->验证->落盘。
    # 默认关闭。会花钱、会动代码，必须用户显式打开，且随时可暂停。
    "auto_exec": False,
    "daemon_enabled": True,
}

_BOOL_KEYS = tuple(DEFAULTS.keys())


def home() -> Path:
    return Path(os.environ.get("KG_DAEMON_HOME",
                               str(Path.home() / ".codex-autopilot"))).expanduser()


def path() -> Path:
    return home() / "control.json"


def events_path() -> Path:
    return home() / "events.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load() -> dict:
    """读用户级开关。缺省值补齐；文件损坏则安全暂停并带 error 字段。"""
    out = dict(DEFAULTS)
    out["error"] = None
    p = path()
    if not p.exists():
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        out["paused"] = True
        out["error"] = f"control.json 读不出来（{exc}）；已按「暂停」处理，避免误操作"
        return out
    if not isinstance(data, dict):
        out["paused"] = True
        out["error"] = "control.json 不是对象；已按「暂停」处理"
        return out
    for k in _BOOL_KEYS:
        if k in data and isinstance(data[k], bool):
            out[k] = data[k]
    out["updated_at"] = data.get("updated_at")
    return out


def save(cfg: dict) -> dict:
    """原子写用户级开关，回读一次确保落盘。"""
    clean = {k: bool(cfg.get(k, DEFAULTS[k])) for k in _BOOL_KEYS}
    clean["updated_at"] = _now()
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return load()


def set_keys(**kwargs) -> dict:
    """只改传入的键，其余保持当前值。未知键忽略。"""
    cfg = load()
    for k, v in kwargs.items():
        if k in _BOOL_KEYS and isinstance(v, bool):
            cfg[k] = v
    return save(cfg)


def is_paused() -> bool:
    return bool(load().get("paused"))


def append_event(kind: str, **fields) -> None:
    """给「发生了什么」留一条结构化流水，供面板时间线读取。

    只追加一行 JSON，读的时候倒序取尾部；不做任何聚合，避免成为第二份真相。
    """
    rec = {"ts": _now(), "kind": kind}
    for k, v in fields.items():
        if v is not None:
            rec[k] = v
    try:
        p = events_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 流水记不下不能拖垮真正的动作


def read_events(limit: int = 40) -> list:
    """倒序返回最近的流水记录。文件不存在或坏行就跳过，不抛。"""
    p = events_path()
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out
