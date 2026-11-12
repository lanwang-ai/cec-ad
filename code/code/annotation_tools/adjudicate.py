# -*- coding: utf-8 -*-
"""annotate/adjudicate.py —— 五态标注的多数票金标准 + 1-1-1 仲裁表 + 10% 质控

背景：扩标注 360 条 × 3 人。多数票 2-1 直接成金标准；1-1-1 分裂必须仲裁。
本脚本不修改任何标注者原始答案；只产出金标准与待仲裁表。

用法：
  # 1) 生成金标准 + 仲裁表
  python -X utf8 annotate/adjudicate.py --labels 标注_A.json 标注_B.json 标注_C.json \
      --items items_360.json --out-gold gold_360.json --out-arb 仲裁表.md --qc-rate 0.10
  # 2) 仲裁完成后回填（CSV 列：id,decision,reason；decision ∈ S/P/N/C/L 或全名）
  python -X utf8 annotate/adjudicate.py --apply-arb 仲裁表_已填.csv --gold gold_360.json --out-gold gold_360.json

输出：
  gold_360.json: {"meta": {...}, "items": [{id,label,source,votes,reason?}], "qc": [...], "pending": [...]}
  source ∈ majority（多数票）/ adjudicated（仲裁后）/ pending（待仲裁）
  qc：随机 10% 的多数票条目（抽查复核用；不改变金标准）
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys

sys.stdout.reconfigure(encoding="utf-8")

STATES = ["supported", "partially_supported", "no_evidence", "contradicted", "not_scored"]
ZH = {"supported": "S", "partially_supported": "P", "no_evidence": "N",
      "contradicted": "C", "not_scored": "L"}
LETTER2STATE = {"S": "supported", "P": "partially_supported", "N": "no_evidence",
                "C": "contradicted", "L": "not_scored"}


def norm_state(v) -> str:
    """兼容手填字母 S/P/N/C/L 与全名 supported 等。"""
    s = str(v).strip()
    return LETTER2STATE.get(s.upper(), s)


def load_labels(path: str) -> dict:
    d = json.load(open(path, encoding="utf-8"))
    if "labels" not in d:
        raise SystemExit(f"❌ {path} 缺少 labels 字段（应为 import_.py 输出格式）")
    labels = {str(k): norm_state(v) for k, v in d["labels"].items()
              if norm_state(v) in STATES}
    return {"annotator": d.get("annotator") or path, "labels": labels}


def load_items(path: str) -> dict:
    d = json.load(open(path, encoding="utf-8"))
    items = d["items"] if isinstance(d, dict) else d
    return {str(it["id"]): it for it in items}


def majority(votes: dict[str, str]) -> tuple[str | None, str]:
    """返回 (多数票标签 or None, 状态)：状态 ∈ unanimous/majority/split"""
    c = {}
    for v in votes.values():
        c[v] = c.get(v, 0) + 1
    top = max(c.values())
    winners = [s for s, n in c.items() if n == top]
    if len(winners) == 1:
        return winners[0], ("unanimous" if top == len(votes) else "majority")
    return None, "split"


def evidence_summary(item: dict, max_items: int = 3, width: int = 120) -> str:
    ev = item.get("evidence") or []
    parts = []
    for e in ev[:max_items]:
        if isinstance(e, dict):
            s = f"{e.get('tool', '')}({e.get('status', '')}): {e.get('output_summary', '')}"
        else:
            s = str(e)
        parts.append(s.replace("\n", " ")[:width])
    return " ； ".join(parts) if parts else "（无证据）"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", nargs="+", help="2~3 个 labels JSON")
    ap.add_argument("--items", default="", help="items JSON（取断言原文与证据摘要做仲裁表）")
    ap.add_argument("--out-gold", default="", help="金标准 JSON 输出")
    ap.add_argument("--out-arb", default="", help="仲裁表（Markdown）输出")
    ap.add_argument("--qc-rate", type=float, default=0.10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--apply-arb", default="", help="已填仲裁 CSV 路径（回填模式）")
    ap.add_argument("--gold", default="", help="回填模式下已存在的 gold JSON")
    a = ap.parse_args(argv)

    # ---------- 回填模式 ----------
    if a.apply_arb:
        if not a.gold or not a.out_gold:
            raise SystemExit("❌ 回填模式需要 --gold <已有 gold.json> 与 --out-gold <输出.json>")
        gold = json.load(open(a.gold, encoding="utf-8"))
        by_id = {it["id"]: it for it in gold["items"]}
        n_applied = 0
        with open(a.apply_arb, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                iid = str(row.get("id") or row.get("ID") or "").strip()
                dec = str(row.get("decision") or row.get("仲裁判定") or "").strip()
                reason = str(row.get("reason") or row.get("理由") or "").strip()
                if not iid or not dec:
                    continue
                lab = LETTER2STATE.get(dec.upper(), dec if dec in STATES else None)
                if lab is None:
                    print(f"  ⚠ {iid}: 非法判定 '{dec}'，跳过")
                    continue
                if iid in by_id:
                    by_id[iid].update({"label": lab, "source": "adjudicated", "reason": reason})
                    n_applied += 1
        gold["pending"] = [it["id"] for it in gold["items"] if it["source"] == "pending"]
        json.dump(gold, open(a.out_gold, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"  ✅ 回填 {n_applied} 条 → {a.out_gold}（仍 pending：{len(gold['pending'])}）")
        return 0

    # ---------- 生成模式 ----------
    if not a.labels or not a.out_gold or not a.out_arb:
        raise SystemExit("❌ 生成模式需要 --labels A B [C] --out-gold X --out-arb Y")
    data = [load_labels(p) for p in a.labels]
    names = [d["annotator"] for d in data]
    if len(names) < 2:
        raise SystemExit("❌ 至少需要 2 名标注者")
    items = load_items(a.items) if a.items else {}
    all_ids = sorted(set().union(*[set(d["labels"]) for d in data]))

    out_items, arb_rows = [], []
    for iid in all_ids:
        votes = {d["annotator"]: d["labels"][iid] for d in data if iid in d["labels"]}
        if len(votes) < 2:
            continue
        lab, kind = majority(votes)
        if lab is None and len(votes) < 3:
            # 2 人且不一致 → 必须仲裁
            arb_rows.append({"id": iid, "votes": votes, "status": "pending-arbitration"})
            out_items.append({"id": iid, "label": None, "source": "pending", "votes": votes})
            continue
        if lab is None:
            arb_rows.append({"id": iid, "votes": votes, "status": "pending-arbitration"})
            out_items.append({"id": iid, "label": None, "source": "pending", "votes": votes})
            continue
        out_items.append({"id": iid, "label": lab, "source": "majority", "votes": votes,
                          "vote_kind": kind})

    # QC 抽样（多数票条目）
    majority_ids = [it["id"] for it in out_items if it["source"] == "majority"]
    rnd = random.Random(a.seed)
    k_qc = max(1, round(a.qc_rate * len(majority_ids))) if majority_ids else 0
    qc = sorted(rnd.sample(majority_ids, k_qc)) if k_qc else []
    pending = [it["id"] for it in out_items if it["source"] == "pending"]

    gold = {"meta": {"annotators": names, "n_total": len(out_items),
                     "n_majority": len(majority_ids), "n_pending": len(pending),
                     "qc_rate": a.qc_rate, "seed": a.seed},
            "items": out_items, "qc": qc, "pending": pending}
    json.dump(gold, open(a.out_gold, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 仲裁表（Markdown；只含 1-1-1 或 2 人不一致条目）
    L = ["# 五态标注仲裁表", "",
         f"> 标注者：{', '.join(names)} ｜ 总条目：{len(out_items)} ｜ 待仲裁：{len(pending)}",
         "> 仲裁规则：只依据断言原文与本轮证据判定；不向仲裁者展示系统判定；裁决填写 S/P/N/C/L。", "",
         "| ID | 断言 | 本轮证据（摘要） | " + " | ".join(names) + " | 仲裁判定 | 理由（≥15字） | 状态 |",
         "|---|---|---|" + "---|" * len(names) + "---|---|---|"]
    for r in arb_rows:
        iid = r["id"]
        it = items.get(iid, {})
        claim = str(it.get("claim") or "").replace("\n", " ")[:110]
        ev = evidence_summary(it)
        votes = " | ".join(ZH.get(r["votes"].get(nm, ""), "—") for nm in names)
        L.append(f"| {iid} | {claim} | {ev} | {votes} |  |  | {r['status']} |")
    open(a.out_arb, "w", encoding="utf-8", newline="\n").write("\n".join(L))

    print(f"  ✅ 金标准：{a.out_gold}")
    print(f"     条目 {len(out_items)} ｜ 多数票 {len(majority_ids)} ｜ 待仲裁 {len(pending)} ｜ QC {len(qc)}")
    print(f"  ✅ 仲裁表：{a.out_arb}")
    if pending:
        print("  ⚠ 仲裁完成后再跑 --apply-arb 回填")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
