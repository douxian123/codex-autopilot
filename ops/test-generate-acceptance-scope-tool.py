#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 动态验收范围工具边界回归。"""

import json
import os
import tempfile
from pathlib import Path

import central_mcp


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  PASS", label)


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-scope-tool-") as tmp:
        repo = Path(tmp) / "repo"
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks").mkdir(parents=True)
        old = os.environ.get("KG_REPO")
        os.environ["KG_REPO"] = str(repo)
        try:
            args = {"text": "修改后端 API 数据写入", "kind": "bug",
                    "files": ["backend/write.ts"], "product_shape": ["backend_service"],
                    "delivery_goal": "acceptance_ready", "project": str(repo)}
            preview, code = central_mcp.call_tool("generate_acceptance_scope", args)
            check("默认只预览", code == 0 and preview["persisted"] is False)
            check("预览不写项目文件",
                  not (repo / "ops" / "truth" / "acceptance_scope.json").exists())
            written, code = central_mcp.call_tool(
                "generate_acceptance_scope", {**args, "confirm": True})
            path = repo / "ops" / "truth" / "acceptance_scope.json"
            check("confirm 后成功落盘", code == 0 and written["persisted"] is True)
            check("落盘路径属于指定项目", Path(written["path"]).resolve() == path.resolve())
            stored = json.loads(path.read_text(encoding="utf-8"))
            check("落盘内容保留动态选择结果",
                  stored["selected_checks"] == ["engineering", "evidence"])

            original_kgctl = central_mcp.kgctl
            try:
                central_mcp.kgctl = lambda *args, **kwargs: ("intake mocked", 0)
                intake_preview, code = central_mcp.call_tool(
                    "intake_request", {"text": "修改后端 API 数据写入", "kind": "bug",
                                        "files": ["backend/write.ts"], "project": str(repo)})
                check("统一入口默认只返回验收范围预览",
                      code == 0 and intake_preview["scope_persisted"] is False)
                check("统一入口预览不覆盖已确认范围",
                      json.loads(path.read_text(encoding="utf-8"))["selected_checks"] ==
                      ["engineering", "evidence"])
                intake_confirmed, code = central_mcp.call_tool(
                    "intake_request", {"text": "修改面板交互", "kind": "visual",
                                        "files": ["frontend/Panel.tsx"],
                                        "product_shape": ["web_app"], "project": str(repo),
                                        "confirm_scope": True})
                check("统一入口显式确认后才落动态范围",
                      code == 0 and intake_confirmed["scope_persisted"] is True)
                confirmed = json.loads(path.read_text(encoding="utf-8"))
                check("统一入口落盘的是当前任务范围",
                      set(confirmed["selected_checks"]) ==
                      {"engineering", "frontend", "comprehension", "evidence"})
            finally:
                central_mcp.kgctl = original_kgctl
        finally:
            if old is None:
                os.environ.pop("KG_REPO", None)
            else:
                os.environ["KG_REPO"] = old
    print("generate-acceptance-scope-tool: PASS")


if __name__ == "__main__":
    main()
