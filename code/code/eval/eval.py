# -*- coding: utf-8 -*-
"""bench/eval.py —— 基线统一评测（C2）

用法：
    python bench/eval.py --judges rule,heuristic
    python bench/eval.py --judges rule,heuristic,llm:qwen-plus --limit 20
    python bench/eval.py --judges all            # all = rule + heuristic + llm:qwen-plus
输出：
    results/baselines/<judge>.json  每条预测
    results/baselines/baseline_table.md / .json 汇总表
指标：
    与人工一致率 + bootstrap 95%CI、Cohen κ、supported 类 P/R/F1、五态混淆矩阵
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "judges"))
sys.path.insert(0, PROJ)

import base                      # noqa: E402
import heuristic_judge           # noqa: E402
import rule_judge                # noqa: E402

OUT = os.path.join(PROJ, "results", "baselines")
STATES = base.STATES
ZH = {"supported": "S", "partially_supported": "P", "no_evidence": "N",
      "contradicted": "C", "not_scored": "L"}


def kappa(pairs: list[tuple[str, str]]) -> float:
    n = len(pairs)
    if n == 0:
        return 0.0
    po = sum(1 for a, b in pairs if a == b) / n
    cats = set(a for a, _ in pairs) | set(b for _, b in pairs)
    pe = 0.0
    for c in cats:
        pe += (sum(1 for a, _ in pairs if a == c) / n) * (sum(1 for _, b in pairs if b == c) / n)
    return (po - pe) / (1 - pe) if pe < 1 else 0.0


def prf(pairs: list[tuple[str, str]], pos: str = "supported") -> dict:
    tp = sum(1 for p, g in pairs if p == pos and g == pos)
    fp = sum(1 for p, g in pairs if p == pos and g != pos)
    fn = sum(1 for p, g in pairs if p != pos and g == pos)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(prec, 4),
            "recall": round(rec, 4), "f1": round(f1, 4)}


def bootstrap_ci(pairs: list[tuple[str, str]], n_boot: int = 2000, seed: int = 42) -> tuple[float, float]:
    rnd = random.Random(seed)
    accs = []
    n = len(pairs)
    for _ in range(n_boot):
        sample = [pairs[rnd.randrange(n)] for _ in range(n)]
        accs.append(sum(1 for a, b in sample if a == b) / n)
    accs.sort()
    return round(accs[int(0.025 * len(accs))], 4), round(accs[int(0.975 * len(accs)) - 1], 4)


def run_judge(spec: str, items: list[dict]) -> dict[str, str]:
    if spec == "rule":
        return {it["id"]: rule_judge.judge(it) for it in items}
    if spec == "heuristic":
        return {it["id"]: heuristic_judge.judge(it) for it in items}
    if spec.startswith("llm:"):
        import llm_judge
        model = spec.split(":", 1)[1] or "qwen-plus"
        return {it["id"]: llm_judge.judge(it, model=model) for it in items}
    raise ValueError(f"未知 judge：{spec}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", default=os.path.join(HERE, "data", "blind40.json"))
    ap.add_argument("--judges", default="rule,heuristic")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    items = base.load_items(a.items)
    if a.limit:
        items = items[:a.limit]
    specs = (["rule", "heuristic", "llm:qwen-plus"] if a.judges == "all"
             else [s.strip() for s in a.judges.split(",") if s.strip()])
    os.makedirs(OUT, exist_ok=True)

    rows = []
    print(f"  样本 {len(items)} 条 ｜ 基线 {specs}")
    for spec in specs:
        print(f"▶ {spec} …", flush=True)
        try:
            preds = run_judge(spec, items)
        except Exception as e:                               # noqa: BLE001
            print(f"  ❌ {spec}: {e}")
            rows.append({"judge": spec, "error": str(e)[:200]})
            continue
        pairs = [(preds[it["id"]], it["human"]) for it in items if it.get("human")]
        pairs = [(p, g) for p, g in pairs if p in STATES and g in STATES]
        agree = sum(1 for p, g in pairs if p == g)
        rate = agree / len(pairs) if pairs else 0.0
        lo, hi = bootstrap_ci(pairs) if pairs else (0.0, 1.0)
        row = {"judge": spec, "n": len(pairs), "agree": agree,
               "rate": round(rate, 4), "ci95": [lo, hi],
               "kappa": round(kappa(pairs), 4), "prf_supported": prf(pairs),
               "pred": preds}
        rows.append(row)
        json.dump({"judge": spec, "items": [it["id"] for it in items],
                   "pred": preds},
                  open(os.path.join(OUT, spec.replace(":", "_") + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"  一致 {agree}/{len(pairs)} = {rate*100:.1f}% ｜ κ {row['kappa']:.3f} ｜ "
              f"P {row['prf_supported']['precision']:.1%}")

    # 汇总表
    L = ["# 基线对比表（automatic, 2026-11-08）", "",
         f"> 样本：{os.path.basename(a.items)}（n={len(items)}）；指标：与人工一致率（bootstrap 95%CI）、Cohen κ、supported 类 P/R/F1。", "",
         "| 方法 | n | 一致率 | 95%CI | Cohen κ | P | R | F1 |",
         "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            L.append(f"| {r['judge']} | — | ❌ {r['error'][:40]} | — | — | — | — | — |")
            continue
        p = r["prf_supported"]
        L.append(f"| {r['judge']} | {r['n']} | {r['agree']}/{r['n']} = {r['rate']*100:.1f}% | "
                 f"{r['ci95'][0]*100:.1f}%–{r['ci95'][1]*100:.1f}% | {r['kappa']:.3f} | "
                 f"{p['precision']*100:.1f}% | {p['recall']*100:.1f}% | {p['f1']*100:.1f}% |")
    L += ["", "## 说明", "",
          "- `rule` = 本工作的规则判定（默认 profile）；`heuristic` = 纯关键词/数字匹配的弱基线；",
          "- `llm:*` = 通用大模型当判官（同口径五态 prompt）；",
          "- 小样本（n=40）区间很宽，**不用于宣称谁更优**，只用于展示误差结构；",
          "- 复跑：`python bench/eval.py --judges all`。", ""]
    p = os.path.join(OUT, "baseline_table.md")
    open(p, "w", encoding="utf-8", newline="").write("\n".join(L))
    json.dump({"items": os.path.basename(a.items), "n": len(items),
               "rows": [{k: v for k, v in r.items() if k != "pred"} for r in rows]},
              open(os.path.join(OUT, "baseline_table.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n  ✅ 汇总：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
