#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：状态管家只能「提卡 → 人点头才改 → 备份 → 一键回滚」，绝不自动改 config。

为什么有这条（2026-11-18）：

  `budget_gate.py` 每拍测量长驻窗口的上下文健康，但**只读**：它从不改
  `~/.codex/config.toml`。于是「压缩线/工具输出上限该不该调」仍然是用户人肉
  判断、人肉改、人肉记回滚点 —— 状态管家承诺的那条链路（提卡 → 同意才改 →
  备份 → 一键回滚）根本没接线。

本门禁在隔离目录里真起一份 config.toml，锁七件事：
  1. `propose` 只写提案、**绝不碰 config**（字节级比对）；
  2. `apply` 不给 `--confirm` 一律拒绝，config 不动；
  3. `apply --confirm` 真的改到目标值，且**只改那一行**、其余字节原样；
  4. 应用前先按字节备份，回滚能一字不差还原；
  5. 白名单外的键 / 越出安全区间的值 / 反方向调整，一律拒绝；
  6. 出卡之后 config 被人手改过 → 这张卡作废（stale），不用旧提案覆盖人的编辑；
  7. `[section]` 里的同名键永远不动（顶层与 section 不许串台）。

退出码 = 未通过项数。
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def load(env):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location("harness_tune_t", OPS / "harness_tune.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CONFIG = """# 头部注释：这些字节不许被动
model = "gpt-x"
tool_output_token_limit = 12000
model_auto_compact_token_limit = 160000

[mcp_servers.foo]
tool_output_token_limit = 99999
model_auto_compact_token_limit = 123
"""


def write_health(runtime, compactions, peak, automation="司令窗"):
    runtime.mkdir(parents=True, exist_ok=True)
    data = {"last_run": "2026-11-18 12:00:00",
            "last_report": {"threads": [{
                "automation": automation, "generation": 3, "level": "over",
                "compactions_recent": compactions, "peak_input_recent": peak,
                "last_compaction_minutes": 4, "tokens_used": 210_000_000,
            }], "usage": {}}}
    (runtime / "budget-gate.json").write_text(json.dumps(data), encoding="utf-8")


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-harness-tune-") as tmp:
        root = Path(tmp)
        codex = root / "codex"
        runtime = root / "runtime"
        codex.mkdir()
        cfg = codex / "config.toml"
        cfg.write_text(CONFIG, encoding="utf-8")
        original = cfg.read_bytes()
        write_health(runtime, compactions=5, peak=180_000)

        m = load({"CODEX_HOME": str(codex), "KG_DAEMON_HOME": str(runtime)})

        # 1) propose 只出卡，绝不碰 config。
        res = m.propose()
        check("propose 成功出卡", res.get("status") == "ok", res)
        check("propose 一个字都没改 config", cfg.read_bytes() == original)
        created = res.get("created") or []
        check("压缩风暴产出了抬压缩线的卡", any(c["key"] == "model_auto_compact_token_limit"
                                                for c in created), created)
        for c in created:
            ok, why = m.allowed_change(c["key"], c["old"], c["new"])
            check(f"提案 {c['key']} 通过安全校验", ok, why)

        card = [c for c in created if c["key"] == "model_auto_compact_token_limit"][0]
        cid = card["id"]

        # 2) 不给 --confirm 一律拒绝。
        res = m.apply(cid, confirm=False)
        check("没有 --confirm 时拒绝应用", res.get("status") == "needs_confirm", res)
        check("拒绝时 config 仍是一个字节没变", cfg.read_bytes() == original)

        # 3) 出卡后 config 被人手改 → 这张卡作废，绝不覆盖人的编辑。
        touched = original.replace(b"model = \"gpt-x\"", b"model = \"gpt-y\"")
        cfg.write_bytes(touched)
        res = m.apply(cid, confirm=True)
        check("config 被人改过后旧卡作废（stale）", res.get("status") == "stale", res)
        check("作废时人的编辑原样保留", cfg.read_bytes() == touched)

        # 4) 重新出卡后真应用：只改那一行。
        cfg.write_bytes(original)
        res = m.propose()
        card = [c for c in (res.get("created") or [])
                if c["key"] == "model_auto_compact_token_limit"][0]
        cid = card["id"]
        res = m.apply(cid, confirm=True)
        check("--confirm 后真的应用", res.get("status") == "applied", res)
        after = cfg.read_text(encoding="utf-8")
        check("目标键已改成新值",
              f"model_auto_compact_token_limit = {card['new']}\n" in after)
        # 头部注释、别的顶层键、section 里的同名键都必须原样。
        check("头部注释没动", after.startswith("# 头部注释：这些字节不许被动\n"))
        check("别的顶层键没动", 'model = "gpt-x"' in after and "tool_output_token_limit = 12000" in after)
        check("section 里的同名键没动",
              "[mcp_servers.foo]\ntool_output_token_limit = 99999\n"
              "model_auto_compact_token_limit = 123\n" in after)
        backup = Path(res.get("backup") or "")
        check("应用前留了按字节备份", backup.exists() and backup.read_bytes() == original,
              str(backup))

        # 5) 回滚能一字不差还原。
        res = m.rollback(cid)
        check("回滚成功", res.get("status") == "rolled_back", res)
        check("回滚后与原文件逐字节相同", cfg.read_bytes() == original)

        # 6) 白名单 / 区间 / 方向三条硬约束。
        ok, _ = m.allowed_change("model", 1, 2)
        check("白名单外的键拒绝", ok is False)
        ok, _ = m.allowed_change("tool_output_token_limit", 12000, 100)
        check("越出安全区间拒绝", ok is False)
        ok, _ = m.allowed_change("model_auto_compact_token_limit", 200000, 160000)
        check("反方向调压缩线拒绝", ok is False)
        ok, _ = m.allowed_change("tool_output_token_limit", 6000, 9000)
        check("反方向调工具上限拒绝", ok is False)

        # 7) 没有体检记录时不出卡（没有证据就不猜）。
        (runtime / "budget-gate.json").unlink()
        res = m.propose()
        check("取不到体检结论时不出卡", res.get("created") == [], res)

        # 8) 一键关零副作用。
        (runtime / "harness-tune").mkdir(parents=True, exist_ok=True)
        (runtime / "harness-tune" / "OFF").write_text("", encoding="utf-8")
        write_health(runtime, compactions=5, peak=180_000)
        cfg.write_bytes(original)
        res = m.propose()
        check("一键关后不提案", res.get("status") == "off" and not res.get("created"), res)
        check("一键关不改 config", cfg.read_bytes() == original)

    print()
    if failures:
        print(f"harness_tune 门禁：{len(failures)} 项未通过")
        for f in failures:
            print("  - " + f)
        return len(failures)
    print("harness_tune 门禁：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
