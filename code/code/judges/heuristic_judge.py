# -*- coding: utf-8 -*-
"""启发式基线（无 LLM、无引擎）：纯关键词/数字匹配。

目的：给论文一个"最弱但有参考"的对照，证明任务不是随便匹配就能做。
规则（刻意简单）：
  1. 没有证据 → no_evidence；
  2. 证据里出现 "error/失败/超时/异常" 且断言含完成词 → contradicted；
  3. 断言里的数字全部出现在证据文本里 → supported；
  4. 否则若断言与证据有 ≥2 个中文二元组重合 → partially_supported；
  5. 否则 → no_evidence。
"""
from __future__ import annotations

import re

DONE = ("完成", "已", "成功", "写入", "生成", "保存", "通过", "修复")
ERR = ("error", "失败", "超时", "异常", "fail", "报错", "拒绝")


def _bigrams(t: str) -> set[str]:
    t = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def judge(item: dict) -> str:
    claim = str(item.get("claim") or "")
    ev = " ".join(str(e.get("output_summary") or "") for e in (item.get("evidence") or []))
    low_ev = ev.lower()
    if not ev.strip():
        return "no_evidence"
    if any(w in low_ev for w in ERR) and any(w in claim for w in DONE):
        return "contradicted"
    nums = re.findall(r"\d+(?:\.\d+)?", claim)
    if nums and all(n in ev for n in nums):
        return "supported"
    if len(_bigrams(claim) & _bigrams(ev)) >= 2:
        return "partially_supported"
    return "no_evidence"
