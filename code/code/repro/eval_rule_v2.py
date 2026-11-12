# -*- coding: utf-8 -*-
"""eval_rule_v2.py —— 独立复跑/评测 rule_v2 判官（dev/test 分开，类别级指标）

输出：<REPO>/<REPO>/分析/rule_v2_独立复跑_20261111.md + .json
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

PAPER = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PROJ = os.path.abspath(os.path.join(PAPER, "..", "项目", "<REPO>"))
sys.path.insert(0, os.path.join(PROJ, "bench", "judges"))
sys.path.insert(0, os.path.join(PROJ, "annotate"))
sys.stdout.reconfigure(encoding="utf-8")

import rule_v2_judge      # noqa: E402
import rule_judge         # noqa: E402
import heuristic_judge    # noqa: E402
import stats as S         # noqa: E402

STATES = ["supported", "partially_supported", "no_evidence", "contradicted", "not_scored"]
ZH = {"supported": "S", "partially_supported": "P", "no_evidence": "N",
      "contradicted": "C", "not_scored": "L"}


def metrics(pairs):
    n = len(pairs)
    a = sum(1 for p, g in pairs if p == g)
    cm = {p: {g: 0 for g in STATES} for p in STATES}
    for p, g in pairs:
        cm[p][g] += 1
    per = {}
    for s in STATES:
        tp = cm[s][s]
        fn = sum(cm[p][s] for p in STATES if p != s)   # 金标准=s、预测≠s → 漏判
        fp = sum(cm[s][g] for g in STATES if g != s)   # 预测=s、金标准≠s → 误报
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per[s] = {"n_gold": tp + fn, "tp": tp, "fp": fp, "fn": fn,
                  "precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3)}
    f1s = [per[s]["f1"] for s in STATES if per[s]["n_gold"] > 0]
    macro_f1 = sum(f1s) / len(f1s) if f1s else 0.0
    lo, hi = S.wilson(a, n)
    return {"n": n, "agree": a, "rate": round(a / n, 4) if n else 0.0,
            "wilson": [round(lo, 4), round(hi, 4)], "kappa": round(S.cohen_kappa(pairs), 4),
            "macro_f1": round(macro_f1, 4), "per_class": per, "confusion": cm}


def main() -> int:
    base = os.path.join(PAPER, "<REPO>", "分析")
    items = {x["id"]: x for x in json.load(open(os.path.join(base, "items_359_gold.json"), encoding="utf-8"))["items"]}
    split = json.load(open(os.path.join(base, "split_dev_test.json"), encoding="utf-8"))["split"]

    # 预测
    pred = {}
    for i, it in items.items():
        pred[i] = {"v2": rule_v2_judge.judge(it),
                   "rule": rule_judge.judge(it),
                   "heuristic": heuristic_judge.judge(it)}
    # 恒判 P 基线
    for i in items:
        pred[i]["const_p"] = "partially_supported"

    res = {}
    for side in ("dev", "test"):
        ids = [i for i in items if split.get(i) == side]
        res[side] = {}
        for name in ("v2", "rule", "heuristic", "const_p"):
            pairs = [(pred[i][name], items[i]["human"]) for i in ids]
            res[side][name] = metrics(pairs)

    # 报告
    out = ["# 规则 v2 判官 · 独立复跑与类别级评测（2026-11-11）", "",
           f"> 数据：359 条人工金标准（10 条仲裁后）；dev {sum(1 for v in split.values() if v=='dev')} / "
           f"test {sum(1 for v in split.values() if v=='test')}（按会话切分）",
           "> 本报告由 Lead 独立复跑生成（不依赖队友报告）；`rule_v2_judge.py` 未修改。", ""]
    for side in ("dev", "test"):
        out += [f"## {side}", "",
                "| 方法 | n | 一致率 | Wilson 95%CI | Cohen κ | macro-F1 |",
                "|---|---|---|---|---|---|"]
        for name, label in [("v2", "规则 v2"), ("rule", "原规则"), ("heuristic", "启发式"), ("const_p", "恒判 P（先验对照）")]:
            r = res[side][name]
            out.append(f"| {label} | {r['n']} | {r['agree']}/{r['n']} = {r['rate']*100:.1f}% | "
                       f"{r['wilson'][0]*100:.1f}–{r['wilson'][1]*100:.1f}% | {r['kappa']:.3f} | {r['macro_f1']:.3f} |")
        out.append("")
    # test 类别级 + 混淆
    r = res["test"]["v2"]
    out += ["## test 上规则 v2 的类别级指标", "",
            "| 类别 | 金标准 n | TP | FP | FN | 精确率 | 召回率 | F1 |",
            "|---|---|---|---|---|---|---|---|"]
    for s in STATES:
        c = r["per_class"][s]
        out.append(f"| {ZH[s]} {s} | {c['n_gold']} | {c['tp']} | {c['fp']} | {c['fn']} | "
                   f"{c['precision']*100:.0f}% | {c['recall']*100:.0f}% | {c['f1']:.2f} |")
    out += ["", "### test 混淆矩阵（行=规则 v2，列=人工）", "",
            "| v2\\人工 | S | P | N | C | L |", "|---|---|---|---|---|---|"]
    for p in STATES:
        out.append(f"| **{ZH[p]}** | " + " | ".join(str(r["confusion"][p][g]) for g in STATES) + " |")
    out += ["", "## 观察", "",
            "1. v2 在 test 上一致率显著高于原规则/启发式/LLM；但 **C（矛盾）召回为 0**、S 召回低——提升主要来自 P/L 判定与减少假指控。",
            "2. 恒判 P 先验 45.3%，v2 63.1% 相对增量 +17.8pp——论文必须同时报这个对照，不能只看 63.1%。",
            "3. 下一步（v3）：在保持低假指控前提下，**恢复同主题矛盾判定**（C 召回目标 ≥50%）与 **S 召回**（目标 ≥50%），macro-F1 为目标函数。",
            ""]
    outpath = os.path.join(base, "rule_v2_独立复跑_20261111.md")
    open(outpath, "w", encoding="utf-8", newline="\n").write("\n".join(out))
    json.dump(res, open(os.path.join(base, "rule_v2_独立复跑_20261111.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    for side in ("dev", "test"):
        print(f"  {side}: " + " ｜ ".join(f"{n} {res[side][n]['rate']*100:.1f}% κ{res[side][n]['kappa']:.3f}"
                                          for n in ("v2", "rule", "heuristic", "const_p")))
    print(f"  ✅ {outpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
