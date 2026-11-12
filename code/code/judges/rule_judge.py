# -*- coding: utf-8 -*-
"""规则判定基线：直接调用 quality_audit 引擎（默认 profile）。

把一条 item（claim + evidence）还原成最小 v2 轨迹，跑引擎，取与 claim 最接近的那条断言状态。
"""
from __future__ import annotations

import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJ)

import capture_spec as CS      # noqa: E402
import quality_audit as QA     # noqa: E402


def _minimal_capture(item: dict) -> dict:
    calls = [{"tool": e.get("tool") or "trace",
              "input": str(e.get("input") or "")[:200],
              "status": str(e.get("status") or "ok"),
              "output_summary": str(e.get("output_summary") or "")[:1500]}
             for e in (item.get("evidence") or [])]
    return {
        "schema_version": "trust-audit/2",
        "context": {"task": "benchmark", "user_prompt": "（benchmark 样本）",
                    "prompt_version": "blind40", "domain": "其他", "expected_output": "text"},
        "turns": [
            {"index": 1, "role": "user", "content": "（benchmark 样本）", "tool_calls": []},
            {"index": 2, "role": "assistant", "content": str(item.get("claim") or ""),
             "tool_calls": calls},
        ],
        "final": {"output": str(item.get("claim") or ""), "claimed_success": True,
                  "claim_text": str(item.get("claim") or ""), "verified": False,
                  "verification_evidence": []},
        "deficits": {"errors_encountered": [], "gave_up": False,
                     "unverified_assumptions": []},
    }


def judge(item: dict) -> str:
    cap = _minimal_capture(item)
    sig = CS.parse_v2(cap)
    sig["turns"] = cap["turns"]
    res = QA.audit_quality(sig, cap["context"])
    claims = res.get("claims") or []
    if not claims:
        return "not_scored"
    target = str(item.get("claim") or "").strip()
    best = max(claims, key=lambda c: len(set(str(c.get("text") or "")) & set(target)))
    return str(best.get("strength") or "not_scored")
