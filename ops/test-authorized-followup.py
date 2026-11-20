#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：自动追进度必须逐项 explicit 授权、幂等、可撤销、失败留账。

用户原始诉求是在授权下自动追击进度；最危险的腐化是「更新 PM 候选后系统自己
升权并发消息」。这里把边界写成反向门禁：
  1. inferred / 破坏性 / 无 thread 绑定直接拒绝；
  2. 同一授权重复点头不会重复发送；
  3. cancel 只撤未发送项；
  4. 发送失败留账，不会无限重试同一句；
  5. 账本损坏报 unknown，绝不降级为 0。
"""

import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import authorized_followup as af  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print("  ok   " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + (f"（{detail}）" if detail else ""))


def raises(label, exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        check(label, True)
        return
    check(label, False, "没有抛出 " + exc.__name__)


def spec(**over):
    row = {
        "ticket_id": "L-0021",
        "thread_id": "thread-abc",
        "prompt": "继续处理 L-0021：回报当前卡点和下一步。",
        "reason": "面板上用户明确点了授权追问",
        "authorization": "explicit",
        "effect": "reversible",
    }
    row.update(over)
    return row


def main():
    with tempfile.TemporaryDirectory(prefix="authorized-followup-test-") as tmp:
        root = Path(tmp)
        raises("inferred 拒绝", af.AuthorizationError,
               af.approve, spec(authorization="inferred"), root)
        raises("破坏性副作用拒绝", af.AuthorizationError,
               af.approve, spec(effect="destructive"), root)
        raises("无 thread 绑定拒绝", af.AuthorizationError,
               af.approve, spec(thread_id=""), root)
        raises("无理由拒绝", af.AuthorizationError,
               af.approve, spec(reason=""), root)

        first = af.approve(spec(), root)
        again = af.approve(spec(), root)
        check("授权后进入 approved", first["state"] == af.STATE_APPROVED, first)
        check("重复授权幂等", again.get("idempotent") is True, again)
        check("待发送只有一条", len(af.pending(root)) == 1, af.pending(root))

        sent = []
        out = af.deliver_approved(
            lambda rid, prompt: sent.append((rid, prompt)) or {"ok": True},
            root=root,
        )
        check("只发送一次", sent == [(first["request_id"], first["prompt"])], sent)
        check("发送后落 sent", out["sent"] == [first["request_id"]], out)
        check("发送后不再 pending", af.pending(root) == [], af.pending(root))
        af.deliver_approved(lambda rid, prompt: sent.append((rid, prompt)), root=root)
        check("重复投递不会再打扰", len(sent) == 1, sent)

        cancelled_spec = spec(ticket_id="L-0022", thread_id="thread-def")
        cancelled = af.approve(cancelled_spec, root)
        af.cancel(cancelled["request_id"], "用户说别催了", root)
        before = len(sent)
        af.deliver_approved(lambda rid, prompt: sent.append((rid, prompt)), root=root)
        check("撤销项不会发送", len(sent) == before, sent)

        failed_spec = spec(ticket_id="L-0023", thread_id="thread-ghi")
        failed = af.approve(failed_spec, root)

        def boom(rid, prompt):
            raise RuntimeError("宿主不可用")

        fo = af.deliver_approved(boom, root=root)
        check("失败留账", fo["failed"] and "宿主不可用" in fo["failed"][0]["error"], fo)
        af.deliver_approved(lambda rid, prompt: sent.append((rid, prompt)), root=root)
        check("失败项不无限重试", len(sent) == before, sent)

        summary = af.summary(root)
        check("摘要可区分状态", summary["status"] == "ok"
              and summary["counts"]["sent"] >= 1
              and summary["counts"]["cancelled"] >= 1
              and summary["counts"]["failed"] >= 1, summary)

        path = af.ledger_path(root)
        path.write_text("{坏行\n", encoding="utf-8")
        corrupt = af.summary(root)
        check("坏账本报 unknown", corrupt["status"] == "unknown", corrupt)

    print()
    if failures:
        print(f"authorized_followup: {len(failures)} 项未通过")
        return len(failures)
    print("authorized_followup: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
