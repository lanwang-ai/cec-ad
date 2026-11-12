# -*- coding: utf-8 -*-
"""analyze_system_vs_gold.py —— 系统判定 vs 三人人工金标准（正式 359 条）

输入：
  - items_360.json（id/claim/evidence/domain）
  - id_map_360.json（out_id → 系统五态 state（字母）/域/会话；封存信息）
  - gold_360.json（多数票/仲裁金标准；pending 者不计入主分析）
  - labels_A/B/C.json（用于 system vs 单标注者）
输出：
  - 分析报告 markdown + JSON
  - 10 条待仲裁明细（claim/证据/三人投票）
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict

PAPER = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PROJ = os.path.abspath(os.path.join(PAPER, "..", "项目", "<REPO>"))
sys.path.insert(0, os.path.join(PROJ, "annotate"))
sys.stdout.reconfigure(encoding="utf-8")

import stats as S      # noqa: E402

STATES = ["supported", "partially_supported", "no_evidence", "contradicted", "not_scored"]
ZH = {"supported": "S", "partially_supported": "P", "no_evidence": "N",
      "contradicted": "C", "not_scored": "L"}
L2S = {v: k for k, v in ZH.items()}


def load(p):
    d = json.load(open(p, encoding="utf-8"))
    return d


def bootstrap_pairs(pairs, fn, n_boot=2000, seed=42):
    """对 (pred, gold) 对做 bootstrap。"""
    rnd = random.Random(seed)
    n = len(pairs)
    vals = []
    for _ in range(n_boot):
        smp = [pairs[rnd.randrange(n)] for _ in range(n)]
        v = fn(smp)
        if v == v:
            vals.append(v)
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1])


def cluster_bootstrap(rows, fn, n_boot=2000, seed=42):
    """按会话聚类 bootstrap（rows: {session_id, pred, gold}）。"""
    rnd = random.Random(seed)
    clusters = defaultdict(list)
    for r in rows:
        clusters[r["session_id"]].append((r["pred"], r["gold"]))
    keys = sorted(clusters)
    vals = []
    for _ in range(n_boot):
        smp = []
        for _ in keys:
            smp.extend(clusters[rnd.choice(keys)])
        v = fn(smp)
        if v == v:
            vals.append(v)
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1] if len(vals) > 1 else vals[-1])


def prf(pairs, pos="supported"):
    tp = sum(1 for p, g in pairs if p == pos and g == pos)
    fp = sum(1 for p, g in pairs if p == pos and g != pos)
    fn = sum(1 for p, g in pairs if p != pos and g == pos)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec, "f1": f1}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", default=os.path.join(PAPER, "<REPO>", "_formal", "items_360.json"))
    ap.add_argument("--idmap", default=os.path.join(PAPER, "<REPO>", "_formal", "id_map_360.json"))
    ap.add_argument("--gold", default=os.path.join(PAPER, "<REPO>", "分析", "gold_360.json"))
    ap.add_argument("--labels", nargs="+", default=[os.path.join(PAPER, "<REPO>", "回收检查", f"labels_formal_{x}.json")
                                                    for x in "ABC"])
    ap.add_argument("--out", default=os.path.join(PAPER, "<REPO>", "分析", "系统对金标准_20261111.md"))
    ap.add_argument("--out-json", default=os.path.join(PAPER, "<REPO>", "分析", "系统对金标准_20261111.json"))
    ap.add_argument("--pending-out", default=os.path.join(PAPER, "<REPO>", "分析", "待仲裁_10条_20261111.md"))
    ap.add_argument("--bootstrap", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args(argv)

    items = {it["id"]: it for it in load(a.items)["items"]}
    idmap = load(a.idmap)["items"]
    # 优先用"按域 profile 重判"的池状态（更公平的系统口径）；没有则回落到 id_map 字母
    prof_path = os.path.join(PAPER, "<REPO>", "_pool", "pool_all_profiled.json")
    prof = {}
    if os.path.exists(prof_path):
        prof = {x["id"]: x for x in load(prof_path)["items"]}
    gold = load(a.gold)
    gold_items = {it["id"]: it for it in gold["items"]}
    labels = {}
    for p in a.labels:
        d = load(p)
        labels[d["annotator"]] = d["labels"]

    rows, pending_rows = [], []
    for iid, gi in gold_items.items():
        sys_letter = (idmap.get(iid) or {}).get("state")
        pred = None
        if iid in prof and prof[iid].get("state") in STATES:
            pred = prof[iid]["state"]
        else:
            pred = L2S.get(sys_letter)
        if pred is None:
            continue
        meta = idmap[iid]
        base = {"id": iid, "pred": pred, "session_id": meta.get("session_id") or "?",
                "domain": meta.get("domain") or "?", "votes": gi.get("votes") or {}}
        if gi.get("label"):
            base["gold"] = gi["label"]
            rows.append(base)
        else:
            pending_rows.append(base)

    pairs = [(r["pred"], r["gold"]) for r in rows]
    n = len(pairs)
    agree = sum(1 for p, g in pairs if p == g)
    rate = agree / n
    lo_w, hi_w = S.wilson(agree, n)
    kap = S.cohen_kappa(pairs)
    klo_items, khi_items = bootstrap_pairs(pairs, S.cohen_kappa, a.bootstrap, a.seed)
    klo_cl, khi_cl = cluster_bootstrap(rows, S.cohen_kappa, max(1000, a.bootstrap // 2), a.seed)
    alo_cl, ahi_cl = cluster_bootstrap(rows, lambda ps: sum(1 for p, g in ps if p == g) / len(ps),
                                       max(1000, a.bootstrap // 2), a.seed)
    cm = {p: {g: 0 for g in STATES} for p in STATES}
    for p, g in pairs:
        cm[p][g] += 1
    prf_s = prf(pairs, "supported")
    severe = sum(1 for p, g in pairs if {p, g} == {"supported", "no_evidence"})

    out = ["# 系统判定 vs 人工金标准（正式 359 条，2026-11-11）", "",
           f"> 三人全量标注 359 条；多数票金标准 {n} 条；**{len(pending_rows)} 条 1-1-1 分裂待仲裁，暂不计入主分析**。",
           f"> 聚类 bootstrap：按会话（{len(set(r['session_id'] for r in rows))} 个会话）重抽样。", ""]
    out += ["## 一、总体", "",
            "| 指标 | 值 |", "|---|---|",
            f"| n（多人一致金标准） | {n} |",
            f"| 一致率 | {agree}/{n} = **{rate*100:.1f}%**（Wilson 95%CI {lo_w*100:.1f}–{hi_w*100:.1f}%） |",
            f"| 一致率（cluster bootstrap 95%CI） | {alo_cl*100:.1f}–{ahi_cl*100:.1f}% |",
            f"| Cohen κ | **{kap:.3f}**（item bootstrap {klo_items:.3f}–{khi_items:.3f}；cluster bootstrap {klo_cl:.3f}–{khi_cl:.3f}） |",
            f"| S 类精确率 | {prf_s['precision']*100:.1f}%（{prf_s['tp']}/{prf_s['tp']+prf_s['fp']}，Wilson {S.wilson(prf_s['tp'], prf_s['tp']+prf_s['fp'])[0]*100:.1f}–{S.wilson(prf_s['tp'], prf_s['tp']+prf_s['fp'])[1]*100:.1f}%，FP={prf_s['fp']}） |",
            f"| S 类召回率 | {prf_s['recall']*100:.1f}%（{prf_s['tp']}/{prf_s['tp']+prf_s['fn']}，Wilson {S.wilson(prf_s['tp'], prf_s['tp']+prf_s['fn'])[0]*100:.1f}–{S.wilson(prf_s['tp'], prf_s['tp']+prf_s['fn'])[1]*100:.1f}%，FN={prf_s['fn']}） |",
            f"| S 类 F1 | {prf_s['f1']*100:.1f}% |",
            f"| 严重错误（S↔N 方向性） | **{severe}** 条 |",
            ""]
    out += ["## 二、混淆矩阵（行=系统，列=人工金标准）", "",
            "| 系统\\人工 | S | P | N | C | L |", "|---|---|---|---|---|---|"]
    for p in STATES:
        out.append(f"| **{ZH[p]}** | " + " | ".join(str(cm[p][g]) for g in STATES) + " |")
    out.append("")

    # 域级
    out += ["## 三、域级", "",
            "| 域 | n | 一致率 | Wilson 95%CI | Cohen κ |", "|---|---|---|---|---|"]
    per_domain = {}
    for dom in sorted(set(r["domain"] for r in rows)):
        sub = [(r["pred"], r["gold"]) for r in rows if r["domain"] == dom]
        k = sum(1 for p, g in sub if p == g)
        lo, hi = S.wilson(k, len(sub))
        kp = S.cohen_kappa(sub)
        per_domain[dom] = {"n": len(sub), "agree": k, "rate": round(k / len(sub), 4),
                           "kappa": round(kp, 4)}
        out.append(f"| {dom} | {len(sub)} | {k/len(sub)*100:.1f}% | {lo*100:.1f}–{hi*100:.1f}% | {kp:.3f} |")
    out.append("")

    # 证据呈现分层（截断/片段/无证据）——低一致的主因诊断
    def _frag(c: str) -> bool:
        s = str(c).strip()
        return bool(re.match(r"^[#|>*\-\d]+[\.\)]?\s", s) or s.startswith("**") or "|" in s[:6])

    trunc = {r["id"]: any(len(str(e.get("output_summary") or "")) >= 200 for e in items[r["id"]]["evidence"])
             for r in rows}
    frag = {r["id"]: _frag(items[r["id"]]["claim"]) for r in rows}
    noev = {r["id"]: len(items[r["id"]]["evidence"]) == 0 for r in rows}

    def _sub(flag):
        ps = [(r["pred"], r["gold"]) for r in rows if flag(r["id"])]
        n2 = len(ps); a2 = sum(1 for p, g in ps if p == g)
        return n2, (a2 / n2 if n2 else 0.0), S.cohen_kappa(ps) if n2 > 1 else float("nan")

    out += ["## 三之二、证据呈现分层（关键诊断）", "",
            "| 子集 | n | 一致率 | Cohen κ |", "|---|---|---|---|"]
    for name, flag in [("全部", lambda i: True),
                       ("证据未截断（<200 字）", lambda i: not trunc[i]),
                       ("证据被截断（≥200 字）", lambda i: trunc[i]),
                       ("断言为 markdown/片段", lambda i: frag[i]),
                       ("无证据条目", lambda i: noev[i])]:
        n2, r2, k2 = _sub(flag)
        out.append(f"| {name} | {n2} | {r2*100:.1f}% | {k2:.3f} |")
    out.append("")
    out += [f"> 诊断：{sum(1 for r in rows if trunc[r['id']])}/{len(rows)} 条至少有一条证据被 200 字上限截断；"
            f"未截断子集一致率更高属**证据长度选择效应**（短证据条目本身更简单），不能作为因果证据。",
            "> **2026-11-12 修正**：第二轮同条目完整证据配对复检显示，完整证据并未提升系统-人工一致率"
            "（33.3%→25.5%），且人-人 κ 从 0.635 降到 0.264；证据呈现不是主因，主因是系统规则错误"
            "（S 过宽、C 假指控、L 漏判，见 §6 与《第二轮结论与修正》）。",
            f"> 人工判 L（不判分）比例 {sum(1 for r in rows if r['gold']=='not_scored')/len(rows)*100:.1f}%；"
            f"片段类断言 {sum(1 for r in rows if frag[r['id']])} 条（断言切分质量仍是重要噪声源）。", ""]

    # 后分层加权（按池总体 域×系统态 份额，校正分层抽样的过度抽样）
    pool_path = os.path.join(PAPER, "<REPO>", "_pool", "pool_all_profiled.json")
    if os.path.exists(pool_path):
        pool = load(pool_path)["items"]
        cnt = Counter((x["domain"], x["state"]) for x in pool)
        tot = len(pool)
        by = defaultdict(list)
        for r in rows:
            by[(r["domain"], r["pred"])].append(1 if r["pred"] == r["gold"] else 0)
        num = den = 0.0
        for key, arr in by.items():
            w = cnt.get(key, 0) / tot
            num += w * (sum(arr) / len(arr))
            den += w
        po_w = num / den if den else float("nan")
        pm = defaultdict(float)
        hm = defaultdict(float)
        for key, arr in by.items():
            w = cnt.get(key, 0) / tot / (den if den else 1)
            pm[key[1]] += w
            hs = Counter(r["gold"] for r in rows if r["domain"] == key[0] and r["pred"] == key[1])
            n3 = sum(hs.values())
            for lab, c in hs.items():
                hm[lab] += w * (c / n3)
        pe = sum(pm.get(k, 0) * hm.get(k, 0) for k in set(list(pm) + list(hm)))
        kap_w = (po_w - pe) / (1 - pe) if pe < 1 else float("nan")
        state_share = Counter(x["state"] for x in pool)
        shares = "；".join(f"{ZH.get(s, s)} {state_share.get(s, 0) / tot * 100:.1f}%" for s in STATES)
        out += ["## 三之三、后分层加权（池总体口径）", "",
                "> 目的：样本按系统五态分层（稀有类被过度抽样），加权回池总体分布。",
                f"- 加权一致率：**{po_w*100:.1f}%** ｜ 加权 Cohen κ：**{kap_w:.3f}**（池总体 域×系统态 权重）",
                f"- 池总体五态份额：{shares}", ""]

    # system vs 单标注者
    out += ["## 四、系统 vs 每位标注者（359 条，参考对比）", "",            "| 标注者 | n | 一致率 | Cohen κ |", "|---|---|---|---|"]
    per_annot = {}
    for nm, lab in labels.items():
        ps = [(L2S[idmap[i]["state"]], lab[i]) for i in lab
              if i in idmap and L2S.get(idmap[i]["state"]) and lab[i] in STATES]
        k = sum(1 for p, g in ps if p == g)
        kp = S.cohen_kappa(ps)
        per_annot[nm] = {"n": len(ps), "agree": k, "rate": round(k / len(ps), 4), "kappa": round(kp, 4)}
        out.append(f"| {nm} | {len(ps)} | {k/len(ps)*100:.1f}% | {kp:.3f} |")
    out.append("")
    out += ["> 参考（旧 40 条盲标，P0 复算）：系统 vs 标注者 A 28/40=70.0%，κ=0.599；S 类精确率 85.7%（FP=2）、召回 92.3%（FN=1）；严重错误 0。",
            "> 注意两批样本难度/分布不同（旧 40 条为高歧义夹具集；新 359 条按系统五态分层），**不可直接比较优劣**。", ""]

    # 待仲裁明细
    pend = ["# 待仲裁的 10 条（1-1-1 分裂）", "",
            "> 三人各执一词；请按手册 §4 P/C 分界与 §6 仲裁规则裁决（Lead 建议见每条）。", "",
            "| # | ID | 断言 | 证据摘要 | A | B | C |", "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(pending_rows, 1):
        it = items.get(r["id"], {})
        claim = str(it.get("claim") or "").replace("\n", " ")[:110]
        ev = " ؛ ".join(f"{e.get('tool')}({e.get('status')}): {str(e.get('output_summary'))[:90]}"
                        for e in (it.get("evidence") or [])[:2]) or "（无证据）"
        votes = r["votes"]
        pend.append(f"| {i} | {r['id']} | {claim} | {ev} | {votes.get('A','—')} | {votes.get('B','—')} | {votes.get('C','—')} |")
    open(a.pending_out, "w", encoding="utf-8", newline="\n").write("\n".join(pend))

    result = {"n": n, "agree": agree, "rate": round(rate, 4),
              "wilson": [round(lo_w, 4), round(hi_w, 4)],
              "kappa": round(kap, 4),
              "kappa_item_ci": [round(klo_items, 4), round(khi_items, 4)],
              "kappa_cluster_ci": [round(klo_cl, 4), round(khi_cl, 4)],
              "agree_cluster_ci": [round(alo_cl, 4), round(ahi_cl, 4)],
              "prf_supported": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in prf_s.items()},
              "severe": severe, "per_domain": per_domain, "per_annotator": per_annot,
              "pending": len(pending_rows), "confusion": cm}
    json.dump(result, open(a.out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(a.out, "w", encoding="utf-8", newline="\n").write("\n".join(out + ["", "---",
              f"*生成：2026-11-11；脚本 `实验/P2_20261108/analyze_system_vs_gold.py`；待仲裁明细见 `{os.path.basename(a.pending_out)}`。*"]))
    print(f"  ✅ 报告：{a.out}")
    print(f"  ✅ 待仲裁：{a.pending_out}（{len(pending_rows)} 条）")
    print(f"  总体：n={n} 一致率 {rate*100:.1f}% κ={kap:.3f} severe={severe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
