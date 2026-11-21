#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：pass 注册表必须真的有异常隔离 / 模式分级 / 预算统一。

为什么这条门禁必须存在：
  `_run_cycle` 原来是一个几百行的大函数，任何一步抛异常整拍就停。历史上真实发生
  过的形态是「回收阶段炸了 → land 一个都不做」，而用户看到的只是「自动挡什么也没干」。
  把步骤拆成 pass 之后，最容易腐化的形态是：**隔离被写成了假的** —— 比如 try
  只包住了 `fn()` 却漏了 `propose_fn`，或者某个 pass 的异常被吞得连记录都没有，
  于是「静默死亡」换个地方又回来了。

所以这里逐条断言，且每条都有反向断言（该红的时候必须红）：
  1. 单 pass 异常被隔离，后面的 pass 照跑；异常必须有 status=errored 的记录
  2. 只有恰好 auto 才执行副作用；propose / observe **绝不**调用 fn
  3. observe 与 propose 的产物可区分（observe 不给用户看，propose 出提案）
  4. 预算被统一检查：超预算的 pass 不执行，且标 over_budget
  5. 关掉的 pass 不执行，标 skipped
  6. 注册顺序就是执行顺序（route 必须在 exec 前，回收必须在派活后）
  7. 名字重复必须报错（否则会有两个同名 pass 互相覆盖观测）
  8. **默认口径 = 各 pass 自己声明的等级**：语义 pass 声明 propose 时，不给任何
     override 也绝不执行。这条是反向断言 —— 防止日后有人把默认值改成 auto，
     把「约束蒸馏」这类语义动作静默升级成全自动而没人发现。
"""
import importlib.util
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load_pass_registry():
    spec = importlib.util.spec_from_file_location("pass_registry_under_test",
                                                  OPS / "pass_registry.py")
    mod = importlib.util.module_from_spec(spec)
    # dataclasses 解析字段类型时要回头看 sys.modules[cls.__module__]；
    # 不注册就直接 exec_module 会让它拿到 None 并抛 AttributeError。
    # 这不是被测代码的问题，是加载方式的问题 —— 所以修在这里，不改 pass_registry。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def auto_pass(pr, name, fn, **kw):
    """构造一条「已显式转正」的 pass，等价于既有工序。

    pass_registry 的默认是 `observe` + `inferred`（都最保守），所以想真正执行副作用
    必须同时声明 `level=auto` 和 `authorization=explicit`。旧用例测的是「能执行」的
    语义，因此用这个 helper 显式转正，而不是悄悄依赖默认值。
    """
    kw.setdefault("level", pr.MODE_AUTO)
    kw.setdefault("authorization", pr.AUTH_EXPLICIT)
    return pr.Pass(name, fn, **kw)


def main():
    pr = load_pass_registry()

    # ---- 1. 异常隔离：中间的 pass 炸了，后面的照跑，且留有 errored 记录 ----
    ran = []

    def boom():
        raise RuntimeError("回收阶段炸了")

    reg = pr.Registry()
    reg.register(auto_pass(pr, "a", lambda: ran.append("a")))
    reg.register(auto_pass(pr, "b", boom))
    reg.register(auto_pass(pr, "c", lambda: ran.append("c")))
    res = reg.run()  # 默认口径：每个 pass 按自己显式声明的等级（这里都是 auto+explicit）
    by = {r.name: r for r in res}
    check("异常隔离：后续 pass 仍然执行", ran == ["a", "c"], f"ran={ran}")
    check("异常隔离：a 是 ok", by["a"].status == "ok", by["a"].status)
    check("异常隔离：b 是 errored 且带原因",
          by["b"].status == "errored" and "回收阶段炸了" in by["b"].detail,
          f"{by['b'].status} {by['b'].detail}")
    check("异常隔离：c 是 ok", by["c"].status == "ok", by["c"].status)

    # 反向断言：注册表对外承诺「异常不抛」，整轮返回正常列表而不是炸出来
    check("异常隔离：整轮不抛异常", len(res) == 3, f"len={len(res)}")

    # ---- 2. 声明 propose 的 pass 默认绝不执行；只有显式 override=auto 才执行 ----
    side = []
    proposals = []
    reg2 = pr.Registry()
    reg2.register(pr.Pass("semantic", lambda: side.append("ran"),
                          level=pr.MODE_PROPOSE,
                          propose_fn=lambda: proposals.append("prop") or "会做 X"))
    out = reg2.run()  # 不给 override
    check("默认口径：propose 级 pass 不执行副作用", side == [], f"side={side}")
    check("默认口径：propose 级 pass 产出提案", proposals == ["prop"], f"{proposals}")
    check("默认口径：状态是 proposed", out[0].status == "proposed", out[0].status)
    check("默认口径：mode 字段回显声明的等级",
          out[0].mode == pr.MODE_PROPOSE, out[0].mode)

    # 显式升权才执行：这是「逐个转正 / 临时试跑」的入口
    side2 = []
    reg2b = pr.Registry()
    reg2b.register(pr.Pass("semantic", lambda: side2.append("ran"),
                           level=pr.MODE_PROPOSE,
                           authorization=pr.AUTH_EXPLICIT))
    out2b = reg2b.run(override=pr.MODE_AUTO)
    check("显式 override=auto 才执行", side2 == ["ran"], f"side2={side2}")
    check("显式 override 后状态是 ok", out2b[0].status == "ok", out2b[0].status)

    # ---- 3. observe 与 propose 可区分（override 降权）----
    reg3 = pr.Registry()
    reg3.register(auto_pass(pr, "s1", lambda: "ran",
                            propose_fn=lambda: "会做 Y"))
    prop = reg3.run(override=pr.MODE_PROPOSE)[0]
    obs = reg3.run(override=pr.MODE_OBSERVE)[0]
    check("propose 标 proposed（要给人看）", prop.status == "proposed", prop.status)
    check("observe 标 ok（只记录，不进决策队列）", obs.status == "ok", obs.status)
    check("propose/observe 的 detail 都能带出提案",
          prop.detail == "会做 Y" and obs.detail == "会做 Y",
          f"{prop.detail!r} {obs.detail!r}")

    # ---- 4. 预算统一检查：超预算不执行 ----
    called = []
    reg4 = pr.Registry(over_budget=lambda step: True)
    reg4.register(auto_pass(pr, "expensive", lambda: called.append(1),
                            title="全量扫描"))
    out4 = reg4.run()[0]
    check("超预算：不执行 fn", called == [], f"called={called}")
    check("超预算：状态是 over_budget", out4.status == "over_budget", out4.status)

    # ---- 5. 关掉的 pass 不执行 ----
    called5 = []
    reg5 = pr.Registry()
    reg5.register(pr.Pass("off", lambda: called5.append(1), enabled=False))
    out5 = reg5.run()[0]
    check("关闭的 pass 不执行", called5 == [] and out5.status == "skipped",
          f"{called5} {out5.status}")

    # ---- 6. 注册顺序 = 执行顺序（route 在 exec 前，回收在派活后）----
    order = []
    reg6 = pr.Registry()
    for name in ("route", "exec", "reap", "land"):
        reg6.register(auto_pass(pr, name,
                                (lambda n: (lambda: order.append(n)))(name)))
    reg6.run()
    check("执行顺序 = 注册顺序", order == ["route", "exec", "reap", "land"], f"{order}")

    # ---- 7. 名字重复必须报错 ----
    reg7 = pr.Registry()
    reg7.register(auto_pass(pr, "dup", lambda: None))
    try:
        reg7.register(auto_pass(pr, "dup", lambda: None))
        check("重名必须报错", False, "没报错")
    except ValueError:
        check("重名必须报错", True)

    # ---- 8. only 过滤：只跑指定的 pass，且仍按注册顺序 ----
    order8 = []
    reg8 = pr.Registry()
    for name in ("x", "y", "z"):
        reg8.register(auto_pass(pr, name,
                                (lambda n: (lambda: order8.append(n)))(name)))
    reg8.run(only=["z", "x"])
    check("only 过滤且保序", order8 == ["x", "z"], f"{order8}")

    # ---- 9. 归一化：不认识 / 大写 / 空值都收敛到安全默认 ----
    check("模式归一：大写可识别", pr.normalize_mode("AUTO") == pr.MODE_AUTO)
    check("模式归一：垃圾值回默认", pr.normalize_mode("nonsense") == pr.MODE_PROPOSE)
    check("模式归一：None 回默认", pr.normalize_mode(None) == pr.MODE_PROPOSE)
    check("强度比较：auto 达标 auto", pr.mode_allows(pr.MODE_AUTO, pr.MODE_AUTO))
    check("强度比较：propose 不达标 auto",
          not pr.mode_allows(pr.MODE_PROPOSE, pr.MODE_AUTO))
    check("强度比较：auto 达标 propose",
          pr.mode_allows(pr.MODE_AUTO, pr.MODE_PROPOSE))

    # ---- 10. 反向断言：默认口径下语义 pass 绝不执行 ----
    # 这条是整套「分级自治」的地基。一旦有人把 effective_mode 的默认值改成 auto，
    # 约束蒸馏 / harness 配置这类语义动作就会静默变成全自动，而没有任何人被告知。
    check("effective_mode：不传 override 时用声明的等级",
          pr.effective_mode(pr.MODE_PROPOSE, None) == pr.MODE_PROPOSE)
    check("effective_mode：声明 auto 且无 override 才是 auto",
          pr.effective_mode(pr.MODE_AUTO, None) == pr.MODE_AUTO)
    check("effective_mode：override=auto 显式升权",
          pr.effective_mode(pr.MODE_PROPOSE, pr.MODE_AUTO) == pr.MODE_AUTO)
    check("effective_mode：override=observe 全局降权",
          pr.effective_mode(pr.MODE_AUTO, pr.MODE_OBSERVE) == pr.MODE_OBSERVE)
    check("effective_mode：垃圾 override 退回声明等级而不是升权",
          pr.effective_mode(pr.MODE_PROPOSE, "nonsense") == pr.MODE_PROPOSE)

    # 端到端反向断言：一个声明 propose 的 pass，在「不给 override」时副作用为零
    e2e_side = []
    e2e_reg = pr.Registry()
    e2e_reg.register(pr.Pass("distill", lambda: e2e_side.append("wrote"),
                            level=pr.MODE_PROPOSE))
    e2e_reg.run()
    check("端到端：默认口径下 propose 级 pass 零副作用", e2e_side == [], f"{e2e_side}")

    # ---- 11. 声明默认值必须 fail-closed ----
    # 新加的 pass 如果忘了声明等级/授权/副作用，必须落到最保守的一档：
    # 只观察、授权来源按系统推断、副作用按最具破坏性处理。这样忘声明的后果是
    # 「不执行」而不是「静默执行」，这是整套分级自治的安全底座。
    default_pass = pr.Pass("undeclared", lambda: None)
    check("默认 level 是 observe", default_pass.level == pr.MODE_OBSERVE,
          default_pass.level)
    check("默认 authorization 是 inferred",
          default_pass.authorization == pr.AUTH_INFERRED, default_pass.authorization)
    check("默认 effect 是 destructive",
          default_pass.effect == pr.EFFECT_DESTRUCTIVE, default_pass.effect)

    check("授权归一：垃圾值回 inferred",
          pr.normalize_authorization("garbage") == pr.AUTH_INFERRED)
    check("授权归一：大写可识别",
          pr.normalize_authorization("EXPLICIT") == pr.AUTH_EXPLICIT)
    check("副作用归一：垃圾值回 destructive（fail-closed）",
          pr.normalize_effect("garbage") == pr.EFFECT_DESTRUCTIVE)
    check("副作用归一：未知类型也回 destructive",
          pr.normalize_effect(None) == pr.EFFECT_DESTRUCTIVE)

    # ---- 12. inferred + destructive 即使 override=auto 也绝不执行 ----
    # 这是「系统推断不能触发破坏性动作」的硬闸。override=auto 是显式升权，
    # 但升的是「等级」不是「授权来源」，破坏性动作仍必须有人明确授权。
    gate_side = []
    gate_reg = pr.Registry()
    gate_reg.register(pr.Pass("danger", lambda: gate_side.append("boom"),
                              level=pr.MODE_OBSERVE,
                              authorization=pr.AUTH_INFERRED,
                              effect=pr.EFFECT_DESTRUCTIVE))
    gate_out = gate_reg.run(override=pr.MODE_AUTO)[0]
    check("inferred+destructive：override=auto 也不执行",
          gate_side == [], f"gate_side={gate_side}")
    check("inferred+destructive：状态是 proposed",
          gate_out.status == "proposed", gate_out.status)

    # 同一个 fn，只要授权来源换成 explicit，就能执行 —— 证明拦截点只在授权，
    # 不是把这条 pass 永久锁死。
    ok_side = []
    ok_reg = pr.Registry()
    ok_reg.register(pr.Pass("safe", lambda: ok_side.append("done"),
                            level=pr.MODE_AUTO,
                            authorization=pr.AUTH_EXPLICIT,
                            effect=pr.EFFECT_DESTRUCTIVE))
    ok_out = ok_reg.run()[0]
    check("explicit+destructive：无 override 也执行",
          ok_side == ["done"] and ok_out.status == "ok",
          f"{ok_side} {ok_out.status}")

    # 可逆动作不受这条闸门约束：inferred 的 route/checkpoint 类应能执行。
    rev_side = []
    rev_reg = pr.Registry()
    rev_reg.register(pr.Pass("route", lambda: rev_side.append("routed"),
                             level=pr.MODE_AUTO,
                             authorization=pr.AUTH_INFERRED,
                             effect=pr.EFFECT_REVERSIBLE))
    rev_out = rev_reg.run()[0]
    check("inferred+reversible：可逆动作放行执行",
          rev_side == ["routed"] and rev_out.status == "ok",
          f"{rev_side} {rev_out.status}")

    print()
    if failures:
        print(f"FAILED {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("selftest ok: pass 注册表的异常隔离 / 模式分级 / 预算统一都已锁定。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
