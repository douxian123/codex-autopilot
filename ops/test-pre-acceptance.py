#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预验收闸回归：缺任何产品级证据都不能打扰用户。"""

from pathlib import Path
import tempfile

import pre_acceptance


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  PASS", label)


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-pre-acceptance-") as tmp:
        root = Path(tmp)
        first = pre_acceptance.evaluate(root)
        check("没有预验收台账时是 unknown", first["status"] == "unknown")
        check("没有预验收台账时不通知用户", first["notify_user"] is False)

        for spec in pre_acceptance.CHECKS:
            pre_acceptance.record(root, spec["id"], "pass", list(spec["required_evidence"]),
                                  summary=spec["title"] + " 已由对应门禁确认")
        ready = pre_acceptance.evaluate(root)
        check("四类证据齐全才 ready", ready["status"] == "ready")
        check("ready 才允许通知用户", ready["notify_user"] is True)
        check("四类自检全部通过", ready["passed"] == 4 and ready["total"] == 4)

        pre_acceptance.record(root, "frontend", "fail", ["browser"], summary="真实交互出现错误")
        blocked = pre_acceptance.evaluate(root)
        check("前端失败会阻断用户验收", blocked["status"] == "blocked")
        check("阻断时不通知用户", blocked["notify_user"] is False)

    print("pre-acceptance: PASS")


if __name__ == "__main__":
    main()
