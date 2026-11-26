#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: 版本化 HyAPI 脚本缺本地密钥时，改指必须 fail closed。

真实风险不是「plist 没改成」，而是「plist 改成了、服务却跑不起来」：
旧脚本把 token 硬编码在本地，版本化脚本不能把 token 提交进仓库。若先把 plist
改到版本目录、却没把密钥迁到本地 secrets.env，HyAPI 三个定时任务会立刻
SystemExit，而门禁只看到路径是新的，误判为已修好。

这条测试用假 HOME 锁死三件事：
  1. 缺密钥：plan 标 missing-secrets，install 不写 plist、不 reload；
  2. 补齐密钥：install 才改指，且只改一次；
  3. 迁移工具只写本地 0600 secrets.env，不回显值，也不覆盖已有键。
"""
import ast
import importlib.util
import json
import os
import plistlib
import re
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

OPS = Path(__file__).resolve().parent
GUARDS = OPS / "guards"
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_plist(path: Path, label: str, args: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": args,
        "StartInterval": 300,
    }, sort_keys=False))


def main():
    repoint = load_module("repoint_secrets_test", GUARDS / "repoint_launchd.py")
    secrets = load_module("hyapi_secrets_test", GUARDS / "hyapi_secrets.py")

    with tempfile.TemporaryDirectory(prefix="repoint-secrets-") as tmp:
        home = Path(tmp)
        rel = "plugins/codex-autopilot/releases/9.9.9"
        versioned = home / rel / "ops" / "guards"
        versioned.mkdir(parents=True)
        (versioned / "hyapi_burner.py").write_text("# versioned\n", encoding="utf-8")
        (versioned / "hyapi_secrets.py").write_text("# helper\n", encoding="utf-8")
        (home / "plugins/codex-autopilot").mkdir(parents=True, exist_ok=True)
        (home / "plugins/codex-autopilot/install-state.json").write_text(
            json.dumps({"source": f"./{rel}"}), encoding="utf-8")

        plist = home / "Library/LaunchAgents/ai.danta.hyapi-burner.plist"
        old = str(home / ".codex-proxy/tools/hyapi_burner.py")
        write_plist(plist, "ai.danta.hyapi-burner", ["/usr/bin/python3", old])

        registry = home / "registry.json"
        registry.write_text(json.dumps({"entries": [{
            "label": "ai.danta.hyapi-burner",
            "script": "ops/guards/hyapi_burner.py",
            "siblings": ["ops/guards/hyapi_secrets.py"],
            "interval_s": 0,
            "class": "repoint",
            "requires_secrets": ["HYAPI_ADMIN_TOKEN", "HYAPI_API_KEY"],
        }]}), encoding="utf-8")

        env = {"KG_GUARD_HOME": str(home), "KG_GUARD_REGISTRY": str(registry)}
        with mock.patch.dict(os.environ, env, clear=False):
            row = repoint.plan(home)[0]
            check("缺密钥时 plan 标成 missing-secrets",
                  row["state"] == "missing-secrets", row)
            before = plist.read_bytes()
            with mock.patch.object(repoint, "_reload") as reload:
                rc = repoint.install(home, dry_run=False)
            check("缺密钥时 install 失败", rc == 1, rc)
            check("缺密钥时不碰 plist", plist.read_bytes() == before)
            check("缺密钥时不 reload", not reload.called)

            secret_file = home / ".codex-proxy" / "secrets.env"
            secret_file.parent.mkdir(parents=True, exist_ok=True)
            secret_file.write_text(
                'HYAPI_ADMIN_TOKEN="fake-admin-token"\n'
                'HYAPI_API_KEY="fake-api-key"\n', encoding="utf-8")
            os.chmod(secret_file, 0o600)

            row = repoint.plan(home)[0]
            check("密钥补齐后重新判为 stale", row["state"] == "stale", row)
            with mock.patch.object(repoint, "_reload", return_value=(True, "")) as reload:
                rc = repoint.install(home, dry_run=False)
            check("密钥补齐后 install 成功", rc == 0, rc)
            check("只 reload 一次", reload.call_count == 1, reload.call_count)
            data = plistlib.loads(plist.read_bytes())
            check("plist 已改到版本目录",
                  str(versioned / "hyapi_burner.py") in data["ProgramArguments"],
                  data["ProgramArguments"])
            check("改指前保留备份",
                  len(list(plist.parent.glob(plist.name + ".bak-*"))) == 1)
            check("改指后幂等判 ok", repoint.plan(home)[0]["state"] == "ok")

    with tempfile.TemporaryDirectory(prefix="hyapi-migrate-") as tmp:
        home = Path(tmp)
        legacy = home / ".codex-proxy" / "tools"
        legacy.mkdir(parents=True)
        (legacy / "hyapi_burner.py").write_text(
            'ADMIN = os.environ.get("HYAPI_ADMIN_TOKEN", "fake-admin-legacy")\n'
            'APIKEY = os.environ.get("HYAPI_API_KEY", "fake-key-legacy")\n',
            encoding="utf-8")
        dest = home / ".codex-proxy" / "secrets.env"
        env = {"KG_GUARD_HOME": str(home)}
        with mock.patch.dict(os.environ, env, clear=False):
            added = secrets.migrate_legacy_secrets(dest=dest, dry_run=True)
            check("迁移 dry-run 报出两个键", set(added) == set(secrets.MANAGED_SECRETS), added)
            check("迁移 dry-run 不写文件", not dest.exists())
            added = secrets.migrate_legacy_secrets(dest=dest)
            check("迁移写入两个键", set(added) == set(secrets.MANAGED_SECRETS), added)
            check("secrets.env 权限是 0600",
                  stat.S_IMODE(dest.stat().st_mode) == 0o600,
                  oct(stat.S_IMODE(dest.stat().st_mode)))
            parsed = secrets.load_env_file(dest)
            check("迁移值可读但不回显",
                  parsed.get("HYAPI_ADMIN_TOKEN") == "fake-admin-legacy"
                  and parsed.get("HYAPI_API_KEY") == "fake-key-legacy")
            again = secrets.migrate_legacy_secrets(dest=dest)
            check("重复迁移不覆盖已有键", again == [])

    real_reg = json.loads((GUARDS / "registry.json").read_text(encoding="utf-8"))
    new_labels = {
        "ai.danta.codex-proxy-watchdog",
        "ai.danta.hyapi-burner",
        "ai.danta.hyapi-deadpool-reap",
        "ai.danta.hyapi-freewatch",
        "com.danta.kg-preserve-orphans",
    }
    by_label = {e["label"]: e for e in real_reg["entries"]}
    for label in sorted(new_labels):
        entry = by_label.get(label) or {}
        rels = [entry.get("script", ""), *(entry.get("siblings") or [])]
        check(f"{label} 的源文件都在仓库里",
              bool(entry) and all(rel and (OPS.parent / rel).exists() for rel in rels),
              rels)

    for name in ("hyapi_burner.py", "hyapi_deadpool_reap.py", "hyapi_freewatch.py"):
        body = (GUARDS / name).read_text(encoding="utf-8")
        check(f"{name} 不含硬编码 token",
              not re.search(r"sk-[A-Za-z0-9_-]{16,}|[0-9a-f]{32,}", body))
        check(f"{name} 从本地密钥解析", "require_env(" in body)

    for name in ("hyapi_burner.py", "hyapi_deadpool_reap.py", "hyapi_freewatch.py",
                 "hyapi_secrets.py", "repoint_launchd.py"):
        path = GUARDS / name
        try:
            # 这些脚本被 /usr/bin/python3 (3.9) 直接执行；按 3.9 语法解析，
            # 防止新版解释器下 PASS、launchd 现场却 SyntaxError。
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path),
                      feature_version=(3, 9))
            check(f"{name} 语法可被 Python 3.9 解析", True)
        except SyntaxError as exc:
            check(f"{name} 语法可被 Python 3.9 解析", False, str(exc))

    if failures:
        print("\nlaunchd repoint secrets unit: FAIL")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nlaunchd repoint secrets unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
