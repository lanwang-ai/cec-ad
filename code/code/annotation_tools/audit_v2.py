# -*- coding: utf-8 -*-
"""audit_v2.py —— 引擎的"v2 可选模式"（不改默认行为）

用法：
  python -X utf8 项目/<REPO>/annotate/audit_v2.py <capture.json> [--out result.json] [--profile data_analysis]

做什么：
  1) 对给定 capture 跑默认引擎 audit_quality（行为与默认完全一致，默认引擎未修改）；
  2) 用 bench/judges/rule_v2_judge.py 对同一批 claim+evidence_used 重新判一遍（v2 模式）；
  3) 输出每条的 default_state / v2_state 与汇总计数。

这是"可选模式"的参考实现：默认引擎、黄金基线、572 回归全部不变；v2 只作为后处理判官。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)                     # <REPO>/
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.join(PROJ, "bench", "judges"))
sys.stdout.reconfigure(encoding="utf-8")

import capture_spec as CS        # noqa: E402
import quality_audit as QA       # noqa: E402
import rule_v2_judge             # noqa: E402


def claim_item(c: dict) -> dict:
    ev = []
    for e in (c.get("evidence_used") or []):
        text = str(e.get("output") or e.get("output_summary") or e.get("error_text") or "")
        ev.append({"tool": e.get("tool") or "?", "status": e.get("status") or "?",
                   "output_summary": text[:4000]})
    return {"claim": c.get("text") or "", "evidence": ev}


def audit(cap: dict, profile: str | None) -> dict:
    sig = CS.parse_v2(cap)
    sig["turns"] = cap.get("turns") or []
    res = QA.audit_quality(sig, cap.get("context") or {}, profile=profile)
    claims = res.get("claims") or []
    out = []
    for c in claims:
        item = claim_item(c)
        out.append({"claim": item["claim"],
                    "default_state": c.get("strength") or "not_scored",
                    "v2_state": rule_v2_judge.judge(item)})
    return {"session_id": cap.get("session_id"), "n_claims": len(out), "claims": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--out", default="")
    ap.add_argument("--profile", default="", choices=["", "general", "data_analysis", "writing_retrieval"])
    a = ap.parse_args(argv)
    cap = json.load(open(a.capture, encoding="utf-8"))
    res = audit(cap, a.profile or None)
    if a.out:
        json.dump(res, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    from collections import Counter
    print(f"  capture: {os.path.basename(a.capture)} | claims: {res['n_claims']}")
    print("  默认引擎:", dict(Counter(c["default_state"] for c in res["claims"])))
    print("  v2 模式 :", dict(Counter(c["v2_state"] for c in res["claims"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
