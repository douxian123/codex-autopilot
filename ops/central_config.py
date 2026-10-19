#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""central_config —— 自动挡里「项目相关」的几个旋钮，集中到这一处（stdlib only）。

为什么单独抽出来
----------------
这套编排骨架本身是项目无关的：一个输入账本、一组固定泳道、一份项目真相、
串行落盘。真正随项目变的只有四处：

  1. 主干分支叫什么（默认 main），功能分支前缀是什么（默认 codex/）；
  2. 落盘用的「主干工作树」放在哪、共享检出（node_modules 的来源）在哪；
  3. 落盘前的验证门禁跑什么命令、在哪个子目录跑；
  4. 固定泳道的名字与门禁提示（design / bug / ...）。

把这四处收进 `ops/central.config.json`，其余逻辑一行不用改。没有配置文件时，
全部走内置默认值，且默认值对「单仓、frontend 子目录、npm run build」这类常见
布局开箱即用。

优先级：环境变量 > ops/central.config.json > 内置默认值。
（环境变量仍然保留，是为了 CI / 临时覆盖；日常改一个文件就够。）
"""

import json
import os
from pathlib import Path

DEFAULTS = {
    "main_branch": "main",
    "branch_prefix": "codex/",
    # 落盘用的独立主干工作树目录。None = <repo 的父目录>/<repo 名>-trunk
    "trunk_dir": None,
    # 共享检出（只读，用来给主干 worktree 软链 node_modules）。None = git 主工作树
    "shared_dir": None,
    # 需要软链 node_modules 的顶层子目录（相对仓库根）
    "link_dirs": ["frontend", "backend"],
    # 落盘前的验证门禁。cwd 相对主干工作树；cmd 用 shell 执行。
    # 不需要门禁就设为 {"cmd": "", "cwd": "."}
    "build": {"cmd": "npm run build", "cwd": "frontend"},
    # 自我验证门禁（可选，比 build 更细）。形如：
    #   "verify": [{"name": "typecheck", "cmd": "npx tsc --noEmit", "cwd": "frontend"}]
    # None = 退化成上面 build 那一条；[] = 明确表示这个项目没有可跑的验证。
    "verify": None,
    # 按实际改动选择额外门禁。未配置时保持旧行为。
    "verify_by_change": None,
    # 归类兜底泳道：任何规则都没命中时落这里。
    "default_lane": "bug",
    # 自定义归类规则（可选）：[[泳道名, [关键词...]], ...]，第一条命中即归类。
    # 为 None 时用 truthctl 内置的通用规则。
    "lane_rules": None,
    # 固定泳道。每个 intake 必须落进其中一条。
    "lanes": [
        {"name": "design", "title": "体验与视觉", "gate": "视觉/交互相关门禁", "status": "active"},
        {"name": "bug", "title": "缺陷修复", "gate": "复现 + 回归门禁", "status": "active"},
        {"name": "arch", "title": "架构与重构", "gate": "编译 + 构建", "status": "active"},
        {"name": "gate", "title": "门禁与验证", "gate": "相关 verify 脚本", "status": "active"},
    ],
}


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def load(repo) -> dict:
    """读配置：内置默认值 <- 文件 <- 环境变量。repo 是状态根目录。"""
    repo = Path(repo)
    cfg = json.loads(json.dumps(DEFAULTS))  # 深拷贝，避免污染 DEFAULTS

    path = repo / "ops" / "central.config.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if v is None:
                        continue
                    if k == "build" and isinstance(v, dict):
                        merged = dict(cfg.get("build") or {})
                        merged.update(v)
                        cfg["build"] = merged
                    else:
                        cfg[k] = v
        except (OSError, ValueError):
            pass  # 配置坏了就用默认值，不阻断编排

    # 环境变量覆盖（保持向后兼容）
    if _env("KG_MAIN"):
        cfg["main_branch"] = _env("KG_MAIN")
    if _env("KG_BRANCH_PREFIX"):
        cfg["branch_prefix"] = _env("KG_BRANCH_PREFIX")
    if _env("KG_TRUNK"):
        cfg["trunk_dir"] = _env("KG_TRUNK")
    if _env("KG_SHARED"):
        cfg["shared_dir"] = _env("KG_SHARED")
    if _env("KG_LINK_DIRS"):
        cfg["link_dirs"] = [d for d in _env("KG_LINK_DIRS").split(",") if d]
    if _env("KG_BUILD_CMD") is not None:
        cfg.setdefault("build", {})["cmd"] = _env("KG_BUILD_CMD")
    if _env("KG_BUILD_DIR"):
        cfg.setdefault("build", {})["cwd"] = _env("KG_BUILD_DIR")
    return cfg


def trunk_dir(repo, cfg) -> Path:
    """落盘用的主干工作树路径。默认放在仓库同级、名字加 -trunk。"""
    repo = Path(repo)
    if cfg.get("trunk_dir"):
        return Path(cfg["trunk_dir"]).expanduser()
    return repo.parent / (repo.name + "-trunk")


def shared_dir(repo, cfg) -> Path:
    """共享检出路径。默认等于仓库自身（单检出场景下 node_modules 就在本仓）。"""
    repo = Path(repo)
    if cfg.get("shared_dir"):
        return Path(cfg["shared_dir"]).expanduser()
    return repo


def verify_gates(cfg) -> list:
    """把 verify 配置规范化成 [{name, cmd, cwd}]。

    没配 verify 时退化成 build 一条，让「能构建」至少成为一道最小验证；
    显式配成 [] 则表示这个项目没有可跑的验证，调用方不该编一条假的。
    """
    raw = cfg.get("verify")
    if raw is None:
        build = cfg.get("build") or {}
        cmd = (build.get("cmd") or "").strip()
        if not cmd:
            return []
        return [{"name": "build", "cmd": cmd, "cwd": build.get("cwd") or "."}]
    if isinstance(raw, str):
        raw = [raw]
    elif isinstance(raw, dict):
        raw = [raw]
    out = []
    for i, item in enumerate(raw or []):
        if isinstance(item, str):
            cmd = item.strip()
            if cmd:
                out.append({"name": "gate%d" % (i + 1), "cmd": cmd, "cwd": "."})
        elif isinstance(item, dict):
            cmd = (item.get("cmd") or "").strip()
            if not cmd:
                continue
            gate = {"name": item.get("name") or "gate%d" % (i + 1),
                    "cmd": cmd, "cwd": item.get("cwd") or "."}
            if item.get("always"):
                gate["always"] = True
            out.append(gate)
    return out


def verify_gates_for_changes(cfg, changed_paths) -> list:
    """Select gates from the actual diff while preserving the old behavior by default."""
    base = verify_gates(cfg)
    rule_cfg = cfg.get("verify_by_change")
    if not rule_cfg:
        return base
    if isinstance(rule_cfg, dict):
        if not rule_cfg.get("enabled", True):
            return base
    elif not isinstance(rule_cfg, bool) or not rule_cfg:
        return base
    else:
        rule_cfg = {"enabled": True}

    paths = [str(p).replace("\\", "/").lstrip("./") for p in (changed_paths or []) if str(p).strip()]
    out = [g for g in base if g.get("always")]
    seen = {(g.get("name"), g.get("cmd"), g.get("cwd")) for g in out}
    matched = 0
    for i, item in enumerate(rule_cfg.get("rules") or []):
        if not isinstance(item, dict):
            continue
        patterns = item.get("paths") or item.get("path") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not patterns:
            continue
        hit = any(_path_matches_any(path, patterns) for path in paths)
        cmd = str(item.get("cmd") or "").strip()
        if not hit or not cmd:
            continue
        gate = {"name": item.get("name") or "change%d" % (i + 1),
                "cmd": cmd, "cwd": item.get("cwd") or "."}
        key = (gate["name"], gate["cmd"], gate["cwd"])
        if key not in seen:
            out.append(gate)
            seen.add(key)
        matched += 1
    if matched:
        return out

    # A changed file always has a real verification floor. Prefer the configured
    # build, then the first non-always gate from verify.
    build = next((g for g in base if g.get("name") == "build"), None)
    if build is None:
        build = next((g for g in base if not g.get("always")), None)
    if build is not None:
        key = (build.get("name"), build.get("cmd"), build.get("cwd"))
        if key not in seen:
            out.append(build)
    return out


def _path_matches_any(path: str, patterns) -> bool:
    from fnmatch import fnmatchcase
    p = str(path).replace("\\", "/").lstrip("./")
    for raw in patterns:
        pat = str(raw).replace("\\", "/").lstrip("./")
        if not pat:
            continue
        if p == pat or p.startswith(pat.rstrip("/") + "/"):
            return True
        if fnmatchcase(p, pat):
            return True
    return False


NETWORK_FAILURE_MARKERS = (
    "ssl connection timeout",
    "empty reply from server",
    "operation too slow",
    "couldn't connect",
    "failed to connect",
    "connection timed out",
    "timed out",
)


def is_network_unreachable(output: str) -> bool:
    """Use one classifier for fetch/push failures so offline work stays safe."""
    text = str(output or "").lower()
    return any(marker in text for marker in NETWORK_FAILURE_MARKERS)
