#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动挡 PM 研究契约的 CLI 入口（不执行网络搜索）。"""

import argparse
import json
import os
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import pm_decision
import research


def main(argv=None):
    ap = argparse.ArgumentParser(description="Codex 自动挡 PM 研究与产品决策")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("request")
    p.add_argument("intent_id")
    p.add_argument("question")
    p.add_argument("--level", default="L2")
    p.add_argument("--risk", default="medium")
    p.add_argument("--scope", action="append", default=[])
    p = sub.add_parser("evidence")
    p.add_argument("research_id")
    p.add_argument("url")
    p.add_argument("--kind", default="unknown")
    p.add_argument("--claim", action="append", default=[])
    p.add_argument("--license", default="unknown")
    p.add_argument("--maintenance", default="unknown")
    p = sub.add_parser("option")
    p.add_argument("research_id")
    p.add_argument("name")
    p.add_argument("--disposition", default="adapt")
    p.add_argument("--rationale", default="")
    p.add_argument("--license", default="unknown")
    p.add_argument("--selected", action="store_true")
    p = sub.add_parser("decide")
    p.add_argument("research_id")
    p.add_argument("choice", choices=sorted(research.CHOICES))
    p.add_argument("--option", default="")
    p.add_argument("--why", default="")
    p.add_argument("--approve", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("research_id", nargs="?")
    sub.add_parser("classify")
    p = sub.choices["classify"]
    p.add_argument("text")
    p.add_argument("--kind", default="unknown")
    p.add_argument("--file", action="append", default=[])
    p = sub.add_parser("gate")
    p.add_argument("--intent", default=None)
    p.add_argument("--research", default=None)
    args = ap.parse_args(argv)
    repo = Path(os.environ.get("KG_REPO") or Path.cwd())
    if args.command == "request":
        out = research.create_request(repo, intent_id=args.intent_id, question=args.question,
                                       scope=args.scope, research_level=args.level,
                                       risk_level=args.risk)
    elif args.command == "evidence":
        out = research.add_evidence(repo, research_id=args.research_id, source_url=args.url,
                                     source_kind=args.kind, claims=args.claim,
                                     license_status=args.license, maintenance_signal=args.maintenance)
    elif args.command == "option":
        out = research.add_option(repo, research_id=args.research_id, name=args.name,
                                  disposition=args.disposition, rationale=args.rationale,
                                  license_status=args.license, selected=args.selected)
    elif args.command == "decide":
        out = pm_decision.decide(repo, research_id=args.research_id, choice=args.choice,
                                 selected_option=args.option, why=args.why, approved=args.approve)
    elif args.command == "status":
        out = pm_decision.brief(repo) if not args.research_id else {
            "research": research.summary(repo, args.research_id),
            "recommendation": pm_decision.recommendation(repo, args.research_id),
            "gate": pm_decision.execution_gate(repo, research_id=args.research_id),
        }
    elif args.command == "classify":
        out = pm_decision.classify(args.text, args.kind, args.file)
    else:
        out = pm_decision.execution_gate(repo, intent_id=args.intent, research_id=args.research)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
