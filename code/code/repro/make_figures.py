# -*- coding: utf-8 -*-
"""make_figures.py —— 论文图表（英文标签，无中文字体依赖）

输出到 <REPO>/稿/：
  fig1_main_comparison.png   主结果对比（test 179，带 Wilson CI）
  fig2_label_distribution.png 人工金标准 vs 原系统 vs v2（test 179）
  fig3_confusion.png          原系统 vs 人工金标准 混淆矩阵（359）
  fig4_evidence_experiment.png 证据完整性配对实验（一致率 + 人-人 κ）
  fig5_v2_class_f1.png        v2 各类别 F1（test 179）
"""
from __future__ import annotations

import json
import math
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

PAPER = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PROJ = os.path.abspath(os.path.join(PAPER, "..", "项目", "<REPO>"))
sys.path.insert(0, os.path.join(PROJ, "bench", "judges"))
sys.path.insert(0, os.path.join(PROJ, "annotate"))
sys.stdout.reconfigure(encoding="utf-8")

import rule_v2_judge   # noqa: E402

OUT = os.path.join(PAPER, "稿")
STATES = ["supported", "partially_supported", "no_evidence", "contradicted", "not_scored"]
ZH = {"supported": "S", "partially_supported": "P", "no_evidence": "N",
      "contradicted": "C", "not_scored": "L"}
plt.rcParams.update({"figure.dpi": 160, "font.size": 9, "axes.spines.top": False,
                     "axes.spines.right": False})


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def fig1():
    names = ["Default\nengine", "Heuristic", "LLM\njudge", "Constant-P\nprior", "Rule v2"]
    ks = [57, 53, 40, 81, 113]
    ns = [179, 179, 173, 179, 179]
    rates = [k / n * 100 for k, n in zip(ks, ns)]
    err = [[r - wilson(k, n)[0] * 100 for r, (k, n) in zip(rates, zip(ks, ns))],
           [wilson(k, n)[1] * 100 - r for r, (k, n) in zip(rates, zip(ks, ns))]]
    fig, ax = plt.subplots(figsize=(6.0, 3.0))
    colors = ["#9aa5b1", "#9aa5b1", "#9aa5b1", "#cbd2d9", "#2f6fb3"]
    bars = ax.bar(names, rates, yerr=err, capsize=3, color=colors, edgecolor="white")
    for b, r in zip(bars, rates):
        ax.text(b.get_x() + b.get_width() / 2, r + 1.5, f"{r:.1f}%", ha="center", fontsize=8)
    ax.set_ylabel("Agreement with human gold (%)")
    ax.set_ylim(0, 80)
    ax.set_title("Held-out test split (n = 179), 95% Wilson CI", fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig1_main_comparison.png"))
    plt.close(fig)


def load_test():
    base = os.path.join(PAPER, "<REPO>")
    items = {x["id"]: x for x in json.load(open(os.path.join(base, "分析", "items_359_gold.json"),
                                                encoding="utf-8"))["items"]}
    split = json.load(open(os.path.join(base, "分析", "split_dev_test.json"), encoding="utf-8"))["split"]
    idmap = json.load(open(os.path.join(base, "_formal", "id_map_360.json"), encoding="utf-8"))["items"]
    L2S = {"S": "supported", "P": "partially_supported", "N": "no_evidence",
           "C": "contradicted", "L": "not_scored"}
    ids = [i for i in items if split.get(i) == "test"]
    human = {i: items[i]["human"] for i in ids}
    sysobj = {i: L2S[idmap[i]["state"]] for i in ids if i in idmap}
    v2 = {i: rule_v2_judge.judge(items[i]) for i in ids}
    return ids, human, sysobj, v2


def fig2():
    ids, human, sysd, v2 = load_test()
    cats = ["Human gold", "Default engine", "Rule v2"]
    data = {c: [0] * 5 for c in cats}
    for i in ids:
        data["Human gold"][STATES.index(human[i])] += 1
        if i in sysd:
            data["Default engine"][STATES.index(sysd[i])] += 1
        data["Rule v2"][STATES.index(v2[i])] += 1
    fig, ax = plt.subplots(figsize=(6.2, 3.0))
    x = np.arange(5)
    w = 0.26
    for k, c in enumerate(cats):
        vals = [v / len(ids) * 100 for v in data[c]]
        ax.bar(x + (k - 1) * w, vals, width=w, label=c)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{ZH[s]}\n({s.replace('_', ' ')})" for s in STATES], fontsize=7)
    ax.set_ylabel("Share of test claims (%)")
    ax.legend(frameon=False)
    ax.set_title("Label distributions on the test split (n = 179)", fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig2_label_distribution.png"))
    plt.close(fig)


def fig3():
    j = json.load(open(os.path.join(PAPER, "<REPO>", "分析", "系统对金标准_20261111.json"),
                       encoding="utf-8"))
    cm = j["confusion"]
    m = np.array([[cm[p][g] for g in STATES] for p in STATES], dtype=float)
    fig, ax = plt.subplots(figsize=(4.2, 3.4))
    im = ax.imshow(m, cmap="Blues")
    for i in range(5):
        for k in range(5):
            ax.text(k, i, int(m[i, k]), ha="center", va="center",
                    color="white" if m[i, k] > m.max() * 0.55 else "black", fontsize=8)
    ax.set_xticks(range(5))
    ax.set_xticklabels([ZH[s] for s in STATES])
    ax.set_yticks(range(5))
    ax.set_yticklabels([ZH[s] for s in STATES])
    ax.set_xlabel("Human gold")
    ax.set_ylabel("Default engine")
    ax.set_title("Confusion matrix (n = 359)", fontsize=9)
    fig.colorbar(im, ax=ax, shrink=0.8)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig3_confusion.png"))
    plt.close(fig)


def fig4():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.0, 3.0), gridspec_kw={"width_ratios": [2.2, 1]})
    groups = ["All 60", "S-over", "C-false", "Controls"]
    r1 = [33.3, 0, 0, 100]
    r2 = [25.5, 0, 0, 74]
    x = np.arange(len(groups))
    ax1.bar(x - 0.18, r1, width=0.36, label="Compressed evidence", color="#cbd2d9")
    ax1.bar(x + 0.18, r2, width=0.36, label="Complete evidence", color="#2f6fb3")
    ax1.set_xticks(x)
    ax1.set_xticklabels(groups)
    ax1.set_ylabel("Engine–human agreement (%)")
    ax1.set_ylim(0, 110)
    ax1.legend(frameon=False, fontsize=7)
    ax1.set_title("Paired evidence-completeness experiment", fontsize=9)
    ax2.bar(["Compressed", "Complete"], [0.635, 0.264], color=["#cbd2d9", "#2f6fb3"], width=0.6)
    for i, v in enumerate([0.635, 0.264]):
        ax2.text(i, v + 0.02, f"{v:.3f}", ha="center", fontsize=8)
    ax2.set_ylim(0, 0.75)
    ax2.set_ylabel("Human–human Fleiss κ")
    ax2.set_title("Annotator agreement", fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig4_evidence_experiment.png"))
    plt.close(fig)


def fig5():
    j = json.load(open(os.path.join(PAPER, "<REPO>", "分析", "rule_v2_独立复跑_20261111.json"),
                       encoding="utf-8"))
    per = j["test"]["v2"]["per_class"]
    labels = [f"{ZH[s]} (n={per[s]['n_gold']})" for s in STATES]
    f1 = [per[s]["f1"] for s in STATES]
    fig, ax = plt.subplots(figsize=(4.6, 2.8))
    bars = ax.bar(labels, f1, color="#2f6fb3")
    for b, v in zip(bars, f1):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.01, f"{v:.2f}", ha="center", fontsize=8)
    ax.set_ylim(0, 1)
    ax.set_ylabel("F1 (test, n = 179)")
    ax.set_title("Rule v2 per-class F1 (conservative judge)", fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig5_v2_class_f1.png"))
    plt.close(fig)


def main() -> int:
    fig1(); fig2(); fig3(); fig4(); fig5()
    for f in ("fig1_main_comparison.png", "fig2_label_distribution.png", "fig3_confusion.png",
              "fig4_evidence_experiment.png", "fig5_v2_class_f1.png"):
        p = os.path.join(OUT, f)
        print(f"  {'✅' if os.path.exists(p) else '❌'} {f}（{os.path.getsize(p)//1024 if os.path.exists(p) else 0} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
