#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex 自动挡的本机控制接口：Companion、脚本、以后别的入口都只走这一条通道。

为什么要有它
------------
菜单栏 Companion、面板、MCP、脚本如果各自去读 control.json、各自去写账本，
迟早会长出第二套真相（这个项目已经被这种事坑过一次）。所以把它们收成一层很薄的
接口：只做「读状态 + 四个动作」，业务逻辑一律不在这里实现。

动作：
  status                          读当前状态（开关、项目、当前任务、需要你处理的事）
  enable / disable                总闸（等价于关掉/打开整个外挂）
  intake [--attach 文件]... 文本    把一条需求送进唯一入口
  panel                           生成完整驾驶舱并打开
  open status                     返回项目状态文件路径（给 Companion 揭示用）
  settings [key=value]...         读/改自动动作开关
  events [n]                      最近的事件流水
  native-probe                    真跑一次原生直连自检并记下结果
"""

import json
import os
import subprocess
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import control_state  # noqa: E402
from plugin_binding import apply_plugin_env  # noqa: E402

apply_plugin_env()

CONFIG = Path.home() / "Library" / "Application Support" / "CodexAutopilot" / "companion.json"
STATUS_FILES = ("docs/PRODUCT_MAINLINE.md", "docs/CONTROL_PLANE.html", "ops/truth/mainline.json")


def _companion_config():
    """安装器写下的配置：控制接口路径 + 它服务的项目。读不到就按缺省走。"""
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _repo():
    configured = _companion_config().get("project")
    return Path(os.environ.get("KG_REPO") or configured or OPS.parent).expanduser().resolve()


def _result(payload, code=0):
    print(json.dumps(payload, ensure_ascii=False))
    return code


def _split_attachments(argv):
    """拆出 --attach <路径>，剩下的拼成需求原话（原话不动，不改写、不归类）。"""
    files, words, index = [], [], 0
    while index < len(argv):
        token = argv[index]
        if token in ("--attach", "-a"):
            if index + 1 < len(argv):
                files.append(str(Path(argv[index + 1]).expanduser()))
                index += 2
            else:
                index += 1
            continue
        words.append(token)
        index += 1
    return " ".join(words).strip(), files


def _run_kgctl(args, repo, extra_env=None):
    env = os.environ.copy()
    env["KG_REPO"] = str(repo)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(OPS / "kgctl.py"), *args],
                          cwd=str(repo), env=env, capture_output=True, text=True)


def _status():
    from central_mcp import _status_payload

    import codex_native

    cfg = control_state.load()
    snapshot = _status_payload(fast=True)
    active = (snapshot.get("doing_items") or [])[:3]
    attention = (
        (snapshot.get("human_acceptance") or [])
        + (snapshot.get("blocked", {}).get("failures") or [])
        + (snapshot.get("blocked", {}).get("resolve") or [])
        + (snapshot.get("blocked", {}).get("exec") or [])
    )[:5]
    return _result({
        "ok": True,
        "enabled": bool(cfg.get("enabled", True)),
        "lifecycle": control_state.lifecycle_status(cfg),
        "paused": bool(cfg.get("paused")),
        "switches": {
            key: bool(cfg.get(key))
            for key in ("auto_land", "auto_resolve", "auto_port",
                        "auto_exec", "daemon_enabled")
        },
        "error": cfg.get("error"),
        "project": str(_repo()),
        "current_task": (
            {
                "title": (snapshot.get("exec_now") or {}).get("title")
                or (active[0].get("title") if active else None),
                "stage": "自动实现中" if snapshot.get("exec_now") else (
                    "处理中" if active else "空闲"
                ),
                "detail": snapshot.get("doing"),
            }
            if snapshot.get("exec_now") or active
            else None
        ),
        "attention": [
            item.get("title") or item.get("target") or item.get("branch")
            or item.get("id") or "需要处理"
            for item in attention
        ],
        "recent_completed": snapshot.get("recent_completed") or [],
        "daemon": snapshot.get("daemon") or {},
        # 原生直连只报「已验证据」：socket 在不在是即时事实，能不能派活得看自检结果。
        "native": dict(codex_native.socket_state(),
                       last_probe=codex_native.last_probe()),
    })


def _panel(repo):
    """完整面板 = 浏览器里的驾驶舱。生成不了就如实说，不假装打开成功。"""
    proc = _run_kgctl(["dash"], repo)
    dashboard = repo / "docs" / "CONTROL_PLANE.html"
    if not dashboard.is_file():
        tail = (proc.stdout + proc.stderr).strip()[-400:]
        return _result({"ok": False, "message": tail or "驾驶舱未生成"}, 1)
    opened = False
    if sys.platform == "darwin":
        opened = subprocess.run(["open", str(dashboard)], check=False).returncode == 0
    return _result({"ok": True, "path": str(dashboard), "opened": opened})


def main(argv):
    action = argv[0] if argv else "status"
    repo = _repo()

    if action == "status":
        return _status()

    if action in ("enable", "disable"):
        config = control_state.set_keys(enabled=(action == "enable"))
        control_state.append_event("companion", action=action)
        return _result({
            "ok": True,
            "enabled": bool(config.get("enabled")),
            "lifecycle": control_state.lifecycle_status(config),
        })

    if action == "intake":
        text, files = _split_attachments(argv[1:])
        if not text and not files:
            return _result({"code": "INTAKE_TEXT_REQUIRED",
                            "message": "需要输入需求，或至少附一个文件"}, 2)
        guard = control_state.mutation_guard()
        if guard:
            return _result(guard, 2)
        proc = _run_kgctl(["intake", text or "（见附件）"], repo,
                          {"KG_INBOX_FILES": ":".join(files)} if files else None)
        return _result({
            "ok": proc.returncode == 0,
            "output": (proc.stdout + proc.stderr).strip(),
            "attachments": files,
            "code": proc.returncode,
        }, proc.returncode)

    if action == "panel":
        return _panel(repo)

    if action == "native-probe":
        import codex_native

        result = codex_native.probe()
        codex_native.record_probe(result)
        return _result({"ok": True, "native": result},
                       0 if result.get("connected") else 1)

    if action == "settings":
        updates = {}
        for token in argv[1:]:
            if "=" not in token:
                continue
            key, raw = token.split("=", 1)
            if key in control_state.DEFAULTS:
                updates[key] = raw.strip().lower() in ("1", "true", "on", "yes")
        config = control_state.set_keys(**updates) if updates else control_state.load()
        if updates:
            control_state.append_event("settings", **updates)
        return _result({
            "ok": True,
            "settings": {key: bool(config.get(key)) for key in control_state.DEFAULTS},
            "error": config.get("error"),
        })

    if action == "events":
        limit = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 20
        return _result({"ok": True, "events": control_state.read_events(limit)})

    if action == "open":
        what = argv[1] if len(argv) > 1 else "status"
        if what == "status":
            for candidate in STATUS_FILES:
                path = repo / candidate
                if path.is_file():
                    return _result({"ok": True, "path": str(path)})
            return _result({"ok": False, "message": "项目里还没有状态文件"}, 1)
        return _result({"code": "UNKNOWN_TARGET", "message": what}, 2)

    return _result({"code": "UNKNOWN_ACTION", "message": action}, 2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
