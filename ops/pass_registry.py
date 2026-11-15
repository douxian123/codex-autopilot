#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pass_registry —— 把「一个周期」拆成一组各自独立的 pass（stdlib only）。

为什么需要这一层
----------------
历史的 `central_daemon._run_cycle()` 是一个几百行的大函数：route → dispatch_exec →
回收 → tick → supersede → land → port → resolve 顺序串下来。它有三个结构性缺陷，
都在真实事故里露过头：

  1. **没有异常隔离**。任何一步抛异常，整拍后面的步骤一个都不跑。实际发生过的形态是
     「回收阶段炸了 → land 一个都不做 → 用户看到的是『自动挡什么也没干』」。
  2. **没有逐系统开关**。想只跑回收、只跑 land，只能改代码或靠一堆散落的 env。
     运维想让某个子系统停下来观察时，没有干净的闸门。
  3. **没有统一预算**。`_over_budget()` 是散在每个长步骤前手写的 if，漏写一处就是
     半小时的黑洞；而日志里看不出「这一拍到底跑了哪些、各自花了多久」。

这一层把每个子系统变成一个 `Pass`：有名字、有模式、有开关、有预算、有观测。
单 pass 异常被隔离成一条失败记录，不拖垮整拍；低于 auto 的模式（propose / observe）
**不执行副作用**，只回一个「本来会做什么」的记录，交给上层决定是否落进
`ops/truth/decisions.jsonl`。

权限等级（对应「分级自治，逐个转正」）
--------------------------------------
**每个 pass 自己声明等级**，`Registry.run()` 默认就按它自己的声明跑；给
`override` 时才整体改口径。执行副作用**只有一种情况**：这一轮对该 pass 生效的
等级恰好是 `auto`。

  * `auto`    —— 机械、可逆、有硬判据的动作。直接执行。
  * `propose` —— 语义判断类的动作（约束蒸馏、harness 配置调整、任务拆解）。
                只产出提案，不落副作用，等上层决定是否收进决策队列。
  * `observe` —— 全量观察期：连提案都不给人看，只记录「本来会做什么 + 依据」。

声明等级刻意偏保守：**只有已经有硬判据保护的动作才声明 `auto`**，语义判断类一律
先声明 `propose`。

「逐个转正」怎么发生
--------------------
把某条语义 pass 从 `propose` 转成正式执行，有两种走法，都不要改这一层的逻辑：
  1. 临时试跑：`central_daemon --once --mode auto --only <pass>`，只提升那一轮的
     口径，不改任何默认值。
  2. 正式转正：把该 pass 的声明等级从 `MODE_PROPOSE` 改成 `MODE_AUTO`，一次提交，
     可回溯、可回滚。

注意：`override=auto` 是**显式升权**，不是"跑全自动"的默认值。默认口径下语义类
pass 只提案 —— 这一点由 `test-pass-registry.py` 反向断言锁住，防止有人日后把
默认值改成 auto 而没人发现。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# 权限等级。数值只用来比较强弱，不要拿它做算术以外的事。
MODE_OBSERVE = "observe"
MODE_PROPOSE = "propose"
MODE_AUTO = "auto"

MODES = (MODE_OBSERVE, MODE_PROPOSE, MODE_AUTO)
_MODE_RANK = {MODE_OBSERVE: 0, MODE_PROPOSE: 1, MODE_AUTO: 2}


def normalize_mode(value: Any, default: str = MODE_PROPOSE) -> str:
    """把任意输入收敛成一个合法模式；不认识的值回默认值而不是抛异常。

    这里刻意 fail-safe 到 `default`：写错一个模式名不该让守护起不来，
    但也不该悄悄升级成 auto。默认值是 propose，比 auto 保守。
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _MODE_RANK:
            return v
    return default


def mode_allows(actual: str, required: str) -> bool:
    """actual 是否达到 required 的强度（auto 最强）。"""
    return _MODE_RANK[normalize_mode(actual)] >= _MODE_RANK[normalize_mode(required)]


def effective_mode(declared: str, override: str | None) -> str:
    """算这一轮对某个 pass 实际生效的等级。

    没有 override 时**用 pass 自己声明的等级**——这是「分级自治」的全部含义：
    机械 pass 声明 auto 就自己跑，语义 pass 声明 propose 就只出提案。
    override 是显式升/降权（`--mode auto --only <pass>` 临时试跑，
    或 `--mode observe` 全局观察期），不参与任何默认值。
    """
    if override is None:
        return normalize_mode(declared)
    return normalize_mode(override, default=normalize_mode(declared))


@dataclass
class PassResult:
    """一个 pass 跑完的观测记录。字段都是纯数据，可直接 json.dumps。"""

    name: str
    mode: str
    status: str  # ok | skipped | proposed | errored | over_budget
    duration_s: float = 0.0
    detail: str = ""
    value: Any = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "mode": self.mode,
            "status": self.status,
            "duration_s": round(self.duration_s, 3),
            "detail": self.detail,
            "value": self.value,
        }


@dataclass
class Pass:
    """一个子系统。

    `fn` 的调用约定：无参数，返回任意可 JSON 化的值（没有返回值就当 None）。
    `fn` 只在**这一轮生效等级恰好是 auto** 时被调用；其余情况一律不产生副作用。

    `level` 是这个 pass 声明的自治等级（默认 AUTO）。语义判断类的 pass 必须显式
    声明 `MODE_PROPOSE`，否则它会在默认口径下直接执行 —— 这正是要防的静默升权。

    `propose_fn`（可选）：生效等级低于 auto 时用来产出「本来会做什么」的提案。
    不给就只留一条说明，不假装有提案内容。
    """

    name: str
    fn: Callable[[], Any]
    level: str = MODE_AUTO
    enabled: bool = True
    title: str = ""
    propose_fn: Callable[[], Any] | None = None

    def resolved_title(self) -> str:
        return self.title or self.name


class Registry:
    """按注册顺序持有的 pass 集合。

    顺序是**语义**的一部分：route 必须在 dispatch_exec 之前（否则新收进来的条目
    要等一整拍才进实现），回收必须在派活之后（它要扫几百个现场，跑前面会把派活饿死）。
    所以这里不做任何排序，只按注册顺序跑。
    """

    def __init__(self, log: Callable[[str], None] | None = None,
                 over_budget: Callable[[str], bool] | None = None):
        self._passes: list[Pass] = []
        self._log = log or (lambda _m: None)
        self._over_budget = over_budget or (lambda _step: False)

    def register(self, p: Pass) -> Pass:
        if any(x.name == p.name for x in self._passes):
            raise ValueError(f"pass 名字重复：{p.name}")
        self._passes.append(p)
        return p

    def names(self) -> list[str]:
        return [p.name for p in self._passes]

    def get(self, name: str) -> Pass | None:
        for p in self._passes:
            if p.name == name:
                return p
        return None

    def run(self, override: str | None = None,
            only: Iterable[str] | None = None,
            log_prefix: str = "[pass]") -> list[PassResult]:
        """跑一遍。

        `override=None`（默认）→ 每个 pass 按**自己声明的等级**行事。这是正常路径。
        `override="auto"` → 全局显式升权（临时试跑用，必须配 only 缩小范围）。
        `override="observe"` → 全局降权成观察期。
        `only` 给一组名字就只跑那几个（顺序仍按注册顺序）。
        """
        want = set(only) if only else None
        results: list[PassResult] = []
        for p in self._passes:
            if want is not None and p.name not in want:
                continue
            results.append(self._run_one(p, override, log_prefix))
        return results

    def _run_one(self, p: Pass, override: str | None,
                 log_prefix: str) -> PassResult:
        started = time.monotonic()
        mode = effective_mode(p.level, override)

        def done(status: str, detail: str = "", value: Any = None) -> PassResult:
            return PassResult(p.name, mode, status,
                              time.monotonic() - started, detail, value)

        if not p.enabled:
            self._log(f"{log_prefix} {p.name} 已关闭，跳过")
            return done("skipped", "pass 被关闭")

        # 只有恰好 auto 才执行副作用。propose / observe 一律不执行 ——
        # 注意这里用 == 而不是 >=，否则 observe 会被当成更强而放行。
        if mode != MODE_AUTO:
            detail = ""
            value = None
            if p.propose_fn is not None:
                try:
                    value = p.propose_fn()
                except Exception as exc:  # 提案本身不许拖垮整拍
                    self._log(f"{log_prefix} {p.name} 提案生成失败（忽略）：{exc!r}")
                    return done("errored", f"propose_fn 异常：{exc!r}")
            if isinstance(value, str):
                detail = value
            if mode == MODE_OBSERVE:
                self._log(f"{log_prefix} {p.name} observe：只记录，不落在任何地方")
                return done("ok", detail or "observe：已记录本来会做什么", value)
            self._log(f"{log_prefix} {p.name} 声明 {p.level}，只出提案，不执行")
            return done("proposed", detail, value)

        # 预算检查放在真正调用之前，且由注册表统一做，避免各步骤漏写。
        if self._over_budget(p.resolved_title()):
            return done("over_budget", f"整拍预算用满，{p.name} 留给下一拍")

        try:
            value = p.fn()
        except Exception as exc:
            # 单 pass 异常隔离：写一条失败记录，继续跑后面的 pass。
            self._log(f"{log_prefix} {p.name} 异常（已隔离，继续本轮）：{exc!r}")
            return done("errored", repr(exc))
        return done("ok", "", value)

    @staticmethod
    def summarize(results: list[PassResult]) -> dict:
        """压成一行可读摘要 + 一份按状态的计数，给日志和 state 用。"""
        counts: dict[str, int] = {}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        return {"counts": counts, "total": len(results)}
