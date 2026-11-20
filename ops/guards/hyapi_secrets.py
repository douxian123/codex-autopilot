#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HyAPI 密钥解析：代码进版本库，密钥留在本地。

背景
----
`~/.codex-proxy/tools/hyapi_*.py` 原来把 admin token / api key 硬编码在脚本里。
把这些脚本原样搬进 `releases/<version>/ops/guards/` 等于把密钥提交进版本库，
这是不允许的；把它们改成薄转发又不行，因为它们有真实业务逻辑，薄转发会
让逻辑继续停在没人升级的位置。

这里的边界是：
  * 业务逻辑：版本化，随发版更新；
  * 密钥：留在 `~/.codex-proxy/secrets.env`（0600）或环境变量里；
  * 迁移期兼容：若 secrets.env 还没有对应键，从旧的本地脚本里读取字面值。
    这段兼容只在旧的本地脚本存在时生效，不会把值写回仓库。

`--migrate` 会把旧脚本里的键写进本地 secrets.env，只补缺失项，不覆盖已有值，
也不回显任何密钥内容。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

MANAGED_SECRETS = ("HYAPI_ADMIN_TOKEN", "HYAPI_API_KEY")

LEGACY_SCRIPTS = (
    "~/.codex-proxy/tools/hyapi_burner.py",
    "~/.codex-proxy/tools/hyapi_deadpool_reap.py",
    "~/.codex-proxy/tools/hyapi_freewatch.py",
)


def home() -> Path:
    raw = os.environ.get("KG_GUARD_HOME")
    return Path(raw).expanduser() if raw else Path.home()


def _expand(raw: str, base: Path) -> Path:
    if raw.startswith("~/"):
        return base / raw[2:]
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return base / raw.lstrip("./")
    return path


def secrets_path() -> Path:
    raw = os.environ.get("HYAPI_SECRETS_FILE")
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else home() / path
    return home() / ".codex-proxy" / "secrets.env"


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        value = raw.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except Exception:
                value = value[1:-1] if value.endswith('"') else value
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        if key:
            out[key] = str(value)
    return out


def _extract_literal(text: str, name: str) -> str:
    """从 `os.environ.get("NAME", "literal")` 这种旧写法里取默认值。"""
    pattern = re.compile(
        r'["\']' + re.escape(name) + r'["\']\s*,\s*["\']([^"\'\r\n]+)["\']')
    match = pattern.search(text)
    return match.group(1) if match else ""


def _from_legacy(name: str) -> str:
    base = home()
    for raw in LEGACY_SCRIPTS:
        path = _expand(raw, base)
        if not path.is_file():
            continue
        try:
            value = _extract_literal(path.read_text(encoding="utf-8"), name)
        except Exception:
            continue
        if value:
            return value
    return ""


def resolve_secret(name: str) -> str:
    """环境变量 > 本地 secrets.env > 旧脚本兼容读取。"""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    value = load_env_file(secrets_path()).get(name, "").strip()
    if value:
        return value
    return _from_legacy(name)


def require_env(name: str) -> str:
    value = resolve_secret(name)
    if not value:
        raise SystemExit(
            f"× 缺少 {name}：请设置环境变量，或写入 {secrets_path()}（权限 0600）。"
            f"可先运行：python3 ops/guards/hyapi_secrets.py --migrate（只补本地，不回显密钥）")
    return value


def migrate_legacy_secrets(dest: Path | None = None, *, overwrite: bool = False,
                           dry_run: bool = False) -> list[str]:
    """把旧脚本里的密钥补进本地 secrets.env；值不回显。"""
    path = dest or secrets_path()
    merged = load_env_file(path)
    added: list[str] = []
    for name in MANAGED_SECRETS:
        if name in merged and not overwrite:
            continue
        value = _from_legacy(name)
        if not value:
            continue
        merged[name] = value
        added.append(name)
    if not added or dry_run:
        return added

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for key in sorted(merged):
            fh.write(f"{key}={json.dumps(merged[key], ensure_ascii=False)}\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true",
                        help="把旧脚本里的密钥补进本地 secrets.env")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报告会迁移几个键，不写文件")
    parser.add_argument("--overwrite", action="store_true",
                        help="覆盖 secrets.env 里已有的同名键（默认只补缺失）")
    args = parser.parse_args(argv)
    if not args.migrate:
        parser.print_help()
        return 2
    added = migrate_legacy_secrets(overwrite=args.overwrite, dry_run=args.dry_run)
    action = "将写入" if args.dry_run else "已写入"
    print(f"{action} {len(added)} 个密钥到 {secrets_path()}（值不回显）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
