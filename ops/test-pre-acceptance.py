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

        for spec in pre_acceptance.acceptance_dictionary.ITEMS:
            pre_acceptance.record(root, "engineering", "pass",
                                  list(spec["evidence_required"]),
                                  item_id=spec["id"], version="test-commit",
                                  summary=spec["title"] + " 已由对应门禁确认")
        ready = pre_acceptance.evaluate(root)
        check("每条必检标准都通过才 ready", ready["status"] == "ready")
        check("ready 才允许通知用户", ready["notify_user"] is True)
        check("逐项自检数量完整", ready["passed"] == ready["total"] == len(pre_acceptance.acceptance_dictionary.ITEMS))

        pre_acceptance.record(root, "engineering", "fail", ["browser"],
                              item_id="FE-001", version="test-commit",
                              summary="真实交互出现错误")
        blocked = pre_acceptance.evaluate(root)
        check("任一逐项失败会阻断用户验收", blocked["status"] == "blocked")
        check("阻断时不通知用户", blocked["notify_user"] is False)

    print("pre-acceptance: PASS")


if __name__ == "__main__":
    main()
