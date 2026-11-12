# -*- coding: utf-8 -*-
"""Reproduce the main comparison table (test split, n = 179) from the released package.

No API key and no network access are required. Outputs results/reproduced_main_table.md.
"""
import json
import os
import sys
import math

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "code", "judges"))
sys.path.insert(0, os.path.join(ROOT, "code", "audit"))
sys.stdout.reconfigure(encoding="utf-8")

import rule_judge          # noqa: E402
import heuristic_judge     # noqa: E402
import rule_v2_judge       # noqa: E402

STATES = ["supported", "partially_supported", "no_evidence", "contradicted", "not_scored"]


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def kappa(pairs):
    n = len(pairs)
    cats = set(p for p, _ in pairs) | set(g for _, g in pairs)
    po = sum(1 for p, g in pairs if p == g) / n
    pe = sum((sum(1 for p, _ in pairs if p == c) / n) * (sum(1 for _, g in pairs if g == c) / n) for c in cats)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def macro_f1(pairs):
    cm = {p: {g: 0 for g in STATES} for p in STATES}
    for p, g in pairs:
        cm[p][g] += 1
    f1s = []
    for s in STATES:
        tp = cm[s][s]
        fn = sum(cm[p][s] for p in STATES if p != s)
        fp = sum(cm[s][g] for g in STATES if g != s)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(f1s) / len(f1s)


def main():
    items = {x["id"]: x for x in json.load(open(os.path.join(ROOT, "data", "annotation", "items_359_gold.json"), encoding="utf-8"))["items"]}
    split = json.load(open(os.path.join(ROOT, "data", "annotation", "split_dev_test.json"), encoding="utf-8"))["split"]
    ids = [i for i in items if split.get(i) == "test"]
    gold = {i: items[i]["human"] for i in ids}
    judges = {
        "Rule judge (minimal capture)": rule_judge.judge,
        "Heuristic": heuristic_judge.judge,
        "Rule v2": rule_v2_judge.judge,
        "Constant-P prior": lambda it: "partially_supported",
    }
    rows = []
    for name, fn in judges.items():
        pairs = [(fn(items[i]), gold[i]) for i in ids]
        k = sum(1 for p, g in pairs if p == g)
        lo, hi = wilson(k, len(pairs))
        rows.append((name, k, len(pairs), k / len(pairs), lo, hi, kappa(pairs), macro_f1(pairs)))
        print(f"{name:32s} {k:3d}/{len(pairs)} = {k/len(pairs)*100:5.1f}%  CI {lo*100:.1f}-{hi*100:.1f}  kappa {kappa(pairs):.3f}  macro-F1 {macro_f1(pairs):.3f}")

    outdir = os.path.join(ROOT, "results")
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "reproduced_main_table.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Reproduced main comparison table (test split, n = 179)\n\n")
        f.write("| Method | n | Agreement | Wilson 95% CI | kappa | macro-F1 |\n|---|---|---|---|---|---|\n")
        for name, k, n, r, lo, hi, kp, mf in rows:
            f.write(f"| {name} | {n} | {k}/{n} = {r*100:.1f}% | {lo*100:.1f}-{hi*100:.1f}% | {kp:.3f} | {mf:.3f} |\n")
    print("wrote results/reproduced_main_table.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
