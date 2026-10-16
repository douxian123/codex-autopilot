#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""closure_rate —— 北极星指标「用户提一次需求就闭环的比例」的唯一算法。

为什么要有这个文件
------------------
项目主线写着北极星是这个比例，但之前它只是一句文案：没有任何地方在算它。
结果是——最容易统计的「机器跑了多少动作」（landed / resolved / superseded）
天天在涨，而真正衡量产品价值的闭环率没有分母、没有分子、不会红。
本文件把它变成一个**必须能算出来的数字**：算不出来就 `unknown` 并以退出码 2
失败，绝不返回 0%，也绝不返回「正常」（「取不到」和「没有」是两回事）。

口径（改动必须先写进 decisions.jsonl）
------------------------------------
  分母 = `ops/truth/ledger.jsonl` 里 `type=="ledger"` 的全部条目
         （问题台账里的每一条，都代表被提出过一次的需求）
  分子 = 分母中 `status=="done"` **且** `evidence` 非空的条目
         （标了 done 却没有证据的，按**未闭环**算，并单独报出来）

三态：
  ok       能算出来，给出比例
  no_data  台账读得到但分母为 0（还没有提出过需求）——退出码 2
  unknown  台账缺失/损坏/不可读——退出码 2
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = OPS_DIR.parent
EXIT_OK = 0
EXIT_FAIL = 2


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _result(status, reason=None, **fields):
    out = {"status": status, "reason": reason, "generated_at": _now()}
    out.update(fields)
    return out


def _truth_dir(repo):
    return Path(repo) / "ops" / "truth"


def compute(repo=None):
    """算闭环率。只读台账，不写任何东西；失败一律返回 status!=ok 的结构。"""
    repo = Path(repo or DEFAULT_REPO).expanduser()
    ledger = _truth_dir(repo) / "ledger.jsonl"
    if not ledger.exists():
        return _result("unknown",
                       "取不到分母：台账 %s 不存在" % ledger,
                       denominator=0, numerator=0, rate_pct=None,
                       done_without_evidence=[], not_closed={}, unreadable_lines=0)
    try:
        raw_lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return _result("unknown",
                       "取不到分母：台账读取失败（%s）" % exc,
                       denominator=0, numerator=0, rate_pct=None,
                       done_without_evidence=[], not_closed={}, unreadable_lines=0)

    rows, unreadable = [], 0
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if isinstance(rec, dict) and rec.get("type") == "ledger":
            rows.append(rec)

    if unreadable:
        # 分母可能缺条目 -> 比例不可信，宁可报 unknown 也不报一个假数字。
        return _result("unknown",
                       "取不到分母：台账有 %d 行解析不了，比例不可信" % unreadable,
                       denominator=len(rows), numerator=0, rate_pct=None,
                       done_without_evidence=[], not_closed={},
                       unreadable_lines=unreadable)
    if not rows:
        return _result("no_data",
                       "取不到分母：还没有来自用户需求的问题台账条目",
                       denominator=0, numerator=0, rate_pct=None,
                       done_without_evidence=[], not_closed={},
                       unreadable_lines=0)

    closed, no_evidence = [], []
    status_counts = Counter()
    for rec in rows:
        status = str(rec.get("status") or "open")
        evidence = str(rec.get("evidence") or "").strip()
        if status == "done":
            if evidence:
                closed.append(rec.get("id") or "?")
            else:
                # 标 done 却没有命令/退出码/commit 证据：按未闭环计。
                no_evidence.append(rec.get("id") or "?")
        else:
            status_counts[status] += 1

    denominator, numerator = len(rows), len(closed)
    return _result("ok",
                   denominator=denominator,
                   numerator=numerator,
                   rate_pct=round(numerator * 100.0 / denominator, 1),
                   done_without_evidence=no_evidence,
                   not_closed=dict(status_counts),
                   unreadable_lines=0,
                   policy="closed = status==done 且 evidence 非空；"
                          "done 无证据按未闭环算")


def human_line(res):
    """给人看的一行。unknown/no_data 必须说「取不到」，不许说 0%。"""
    if res.get("status") == "ok":
        return "首问闭环率 %s%%（%s/%s 已闭环）" % (
            res.get("rate_pct"), res.get("numerator"), res.get("denominator"))
    return "首问闭环率取不到：%s" % (res.get("reason") or "未知原因")


def write_snapshot(repo, res):
    """把本次结果写进项目真相目录，供面板与主线视图读取（原子替换）。"""
    target = _truth_dir(repo) / "closure_rate.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    tmp.replace(target)
    return target


def main(argv=None):
    ap = argparse.ArgumentParser(description="计算并（可选）落盘北极星首问闭环率")
    ap.add_argument("--repo", default=str(DEFAULT_REPO), help="项目仓库根目录")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    ap.add_argument("--gate", action="store_true",
                    help="作为门禁运行：落盘 snapshot，算不出来就退出码 2")
    args = ap.parse_args(argv)

    res = compute(args.repo)
    if args.gate:
        write_snapshot(args.repo, res)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(human_line(res))
    return EXIT_OK if res.get("status") == "ok" else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
