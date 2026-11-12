# -*- coding: utf-8 -*-
"""annotate/stats.py —— 标注一致性统计（扩标注版：κ 带 bootstrap CI、Wilson、域级、加权）

与 report.py 的分工：
  - report.py：快速看一致性（点估计）；
  - 本文件：论文级统计（所有 κ/一致率带 CI、混淆矩阵、域级/来源级、可选后分层加权）。

输入格式（与 annotate/import_.py 输出一致）：
  {"annotator": "A", "labels": {"<item_id>": "supported"|"partially_supported"|"no_evidence"|"contradicted"|"not_scored", ...}, "missing": [...]}

用法：
  python -X utf8 annotate/stats.py --labels 标注_A.json 标注_B.json 标注_C.json --out 统计报告.md
  python -X utf8 annotate/stats.py --labels A.json B.json --meta items_360.json --out 报告.md --out-json 报告.json
  # 可选：后分层权重（按语料五态份额）；--weights "supported=0.45,partially_supported=0.15,..."

说明：所有区间为 95%；单比例用 Wilson；κ 用 percentile bootstrap（默认 10,000 次，seed=42）。
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

STATES = ["supported", "partially_supported", "no_evidence", "contradicted", "not_scored"]
ZH = {"supported": "S", "partially_supported": "P", "no_evidence": "N",
      "contradicted": "C", "not_scored": "L"}
ZH_FULL = {"supported": "有证据支持", "partially_supported": "部分支持", "no_evidence": "无证据",
           "contradicted": "与证据矛盾", "not_scored": "不判分"}
LETTER2STATE = {"S": "supported", "P": "partially_supported", "N": "no_evidence",
                "C": "contradicted", "L": "not_scored"}


def norm_state(v) -> str:
    """兼容手填字母 S/P/N/C/L 与全名 supported/partial 等。"""
    s = str(v).strip()
    return LETTER2STATE.get(s.upper(), s)


# ---------------------------------------------------------------- 基础函数
def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson 95% CI（单比例）。"""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def cohen_kappa(pairs: list[tuple[str, str]]) -> float:
    """Cohen's κ（两两；忽略含未知标签的对）。"""
    n = len(pairs)
    if n == 0:
        return float("nan")
    po = sum(1 for a, b in pairs if a == b) / n
    cats = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    pe = sum((sum(1 for a, _ in pairs if a == c) / n) *
             (sum(1 for _, b in pairs if b == c) / n) for c in cats)
    if pe >= 1.0:                       # 所有条目同属一类：κ 无定义，按全一致处理
        return 1.0 if abs(po - 1.0) < 1e-12 else 0.0
    return (po - pe) / (1 - pe)


def fleiss_kappa(matrix: list[list[str]]) -> float:
    """Fleiss' κ；matrix = 条目 × 标注者（需完整、同一批标注者）。"""
    if not matrix or not matrix[0] or len(matrix[0]) < 2:
        return float("nan")
    n_items, n_rat = len(matrix), len(matrix[0])
    P = []
    for row in matrix:
        cnt = Counter(row)
        s = sum(v * (v - 1) for v in cnt.values())
        P.append(s / (n_rat * (n_rat - 1)))
    Pbar = sum(P) / n_items
    total = sum(Counter(x for row in matrix for x in row).values())
    pj = {k: v / total for k, v in Counter(x for row in matrix for x in row).items()}
    Pe = sum(p * p for p in pj.values())
    if Pe >= 1.0:                       # 全部同一类：κ 无定义，按全一致处理
        return 1.0 if abs(Pbar - 1.0) < 1e-12 else 0.0
    return (Pbar - Pe) / (1 - Pe)


def bootstrap_ci(values: list, stat_fn, n_boot: int = 10000, seed: int = 42) -> tuple[float, float]:
    """对'条目级样本'做 percentile bootstrap：values 为可重抽样的单元（如 pair 列表）。"""
    n = len(values)
    if n == 0:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    stats = []
    for _ in range(n_boot):
        sample = [values[rnd.randrange(n)] for _ in range(n)]
        v = stat_fn(sample)
        if v == v:                       # 非 NaN
            stats.append(v)
    if not stats:
        return (float("nan"), float("nan"))
    stats.sort()
    lo = stats[int(0.025 * len(stats))]
    hi = stats[int(0.975 * len(stats)) - 1]
    return (lo, hi)


def fmt(x: float, nd: int = 3) -> str:
    return "—" if x != x else f"{x:.{nd}f}"


def pct(x: float, nd: int = 1) -> str:
    return "—" if x != x else f"{x * 100:.{nd}f}%"


# ---------------------------------------------------------------- 数据加载
def load_labels(path: str) -> dict:
    d = json.load(open(path, encoding="utf-8"))
    if "labels" not in d:
        raise SystemExit(f"❌ {path} 缺少 labels 字段（应为 import_.py 输出格式）")
    annot = d.get("annotator") or path
    labels = {}
    bad = 0
    for k, v in d["labels"].items():
        nv = norm_state(v)
        if nv in STATES:
            labels[str(k)] = nv
        else:
            bad += 1
    if bad:
        print(f"  ⚠ {path}: {bad} 条标签非法/未知，已忽略")
    return {"annotator": annot, "labels": labels, "missing": d.get("missing") or []}


def load_meta(path: str | None) -> dict[str, dict]:
    if not path:
        return {}
    d = json.load(open(path, encoding="utf-8"))
    items = d["items"] if isinstance(d, dict) else d
    return {str(it["id"]): it for it in items}


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", nargs="+", required=True, help="1~n 个 labels JSON（import_.py 输出）")
    ap.add_argument("--out", required=True, help="Markdown 报告路径")
    ap.add_argument("--out-json", default="", help="可选：JSON 结果")
    ap.add_argument("--meta", default="", help="可选：items JSON（取 domain/source 做域级拆分）")
    ap.add_argument("--bootstrap", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--weights", default="", help='可选：后分层权重，如 "supported=0.45,partially_supported=0.15,..."')
    a = ap.parse_args(argv)

    data = [load_labels(p) for p in a.labels]
    names = [d["annotator"] for d in data]
    meta = load_meta(a.meta)

    all_ids = sorted(set().union(*[set(d["labels"]) for d in data]))
    rows = []
    for iid in all_ids:
        votes = {d["annotator"]: d["labels"][iid] for d in data if iid in d["labels"]}
        if len(votes) >= 2:
            rows.append({"id": iid, "votes": votes})
    print(f"  标注者：{', '.join(names)} ｜ 有 ≥2 人标注的条目：{len(rows)} / {len(all_ids)}")

    out = ["# 标注一致性统计（论文级）", "",
           f"> 标注者：{', '.join(names)} ｜ 共同条目：{len(rows)} ｜ bootstrap：{a.bootstrap} 次（seed={a.seed}）",
           "> κ 度量「可一致执行」，不度量正确；单比例 CI 用 Wilson，κ 用 bootstrap percentile。", ""]

    result = {"annotators": names, "n_common": len(rows), "pairs": [], "fleiss": None,
              "diagnostics": {"bootstrap": a.bootstrap, "seed": a.seed}}

    # 1) 两两
    out += ["## 一、两两一致性", "",
            "| 标注者对 | n | 一致数 | 一致率 | 一致率 Wilson 95%CI | Cohen κ | κ bootstrap 95%CI |",
            "|---|---|---|---|---|---|---|"]
    for x, y in itertools.combinations(names, 2):
        pairs = [(r["votes"][x], r["votes"][y]) for r in rows if x in r["votes"] and y in r["votes"]]
        if not pairs:
            continue
        n = len(pairs)
        k = sum(1 for p, q in pairs if p == q)
        rate = k / n
        kap = cohen_kappa(pairs)
        lo_w, hi_w = wilson(k, n)
        lo_k, hi_k = bootstrap_ci(pairs, cohen_kappa, a.bootstrap, a.seed)
        out.append(f"| {x} vs {y} | {n} | {k} | {pct(rate)} | {pct(lo_w)}–{pct(hi_w)} | "
                   f"{fmt(kap)} | {fmt(lo_k)}–{fmt(hi_k)} |")
        result["pairs"].append({"a": x, "b": y, "n": n, "agree": k, "rate": round(rate, 4),
                                "rate_wilson": [round(lo_w, 4), round(hi_w, 4)],
                                "cohen_kappa": round(kap, 4),
                                "cohen_kappa_ci": [round(lo_k, 4), round(hi_k, 4)]})

    # 2) 多人（完整条目）
    complete = [r for r in rows if len(r["votes"]) == len(names)]
    if len(names) >= 3:
        matrix = [[r["votes"][nm] for nm in names] for r in complete]
        if matrix:
            fk = fleiss_kappa(matrix)
            lo_f, hi_f = bootstrap_ci(matrix, fleiss_kappa, max(1000, a.bootstrap // 2), a.seed)
            out += ["", "## 二、多人一致性（Fleiss κ，完整标注条目）", "",
                    f"- n（三人齐标的条目）：{len(matrix)}",
                    f"- **Fleiss κ = {fmt(fk)}**（bootstrap 95%CI {fmt(lo_f)}–{fmt(hi_f)}）"]
            result["fleiss"] = {"n": len(matrix), "kappa": round(fk, 4),
                                "ci": [round(lo_f, 4), round(hi_f, 4)]}

    # 3) 每人五态分布
    out += ["", "## 三、每人五态分布", "", "| 标注者 | S | P | N | C | L |", "|---|---|---|---|---|---|"]
    for d in data:
        c = Counter(d["labels"].values())
        out.append(f"| {d['annotator']} | " + " | ".join(str(c.get(s, 0)) for s in STATES) + " |")

    # 4) 混淆矩阵（第一对；完整 5×5）
    if len(names) >= 2:
        x, y = names[0], names[1]
        out += ["", f"## 四、混淆矩阵（{x} 行 × {y} 列）", "",
                "| | S | P | N | C | L |", "|---|---|---|---|---|---|"]
        for s1 in STATES:
            cells = []
            for s2 in STATES:
                cells.append(str(sum(1 for r in rows if r["votes"].get(x) == s1 and r["votes"].get(y) == s2)))
            out.append(f"| {ZH[s1]} {ZH_FULL[s1]} | " + " | ".join(cells) + " |")

    # 5) 域级（若给 meta）
    if meta:
        domains = sorted({str(meta.get(r["id"], {}).get("domain") or "未标") for r in rows})
        out += ["", "## 五、域级一致性", "",
                "| 域 | 标注者对 | n | 一致率 | Wilson 95%CI | Cohen κ |", "|---|---|---|---|---|---|"]
        for dom in domains:
            sub = [r for r in rows if str(meta.get(r["id"], {}).get("domain") or "未标") == dom]
            for x2, y2 in itertools.combinations(names, 2):
                pairs = [(r["votes"][x2], r["votes"][y2]) for r in sub
                         if x2 in r["votes"] and y2 in r["votes"]]
                if not pairs:
                    continue
                n2 = len(pairs); k2 = sum(1 for p, q in pairs if p == q)
                lo2, hi2 = wilson(k2, n2)
                out.append(f"| {dom} | {x2} vs {y2} | {n2} | {pct(k2 / n2)} | {pct(lo2)}–{pct(hi2)} | {fmt(cohen_kappa(pairs))} |")

    # 6) 后分层加权（可选）
    if a.weights:
        w = {}
        for part in a.weights.split(","):
            k2, v = part.split("=")
            w[k2.strip()] = float(v)
        # 以第一对为口径做加权一致率
        x, y = names[0], names[1]
        pairs_by_state = {s: [] for s in STATES}
        for r in rows:
            if x in r["votes"] and y in r["votes"]:
                # 权重按"人工金标准"（这里用 x 的标签）分层
                pairs_by_state[r["votes"][x]].append((r["votes"][x], r["votes"][y]))
        num = den = 0.0
        for s in STATES:
            ps = pairs_by_state[s]
            if not ps:
                continue
            num += w.get(s, 0.0) * (sum(1 for p, q in ps if p == q) / len(ps))
            den += w.get(s, 0.0)
        out += ["", "## 六、后分层加权一致率（可选）", "",
                f"- 权重：{a.weights}",
                f"- 加权一致率（{x} vs {y}）：{pct(num / den) if den else '—'}"]

    # 7) 分歧清单
    out += ["", "## 七、分歧清单（供仲裁）", ""]
    n_dis = 0
    for r in rows:
        if len(set(r["votes"].values())) > 1:
            n_dis += 1
            out.append(f"- **{r['id']}**：" + "；".join(f"{k}={ZH.get(v, v)}" for k, v in r["votes"].items()))
    if n_dis == 0:
        out.append("（无分歧）")

    out += ["", "## 八、说明", "",
            "- 单比例 CI = Wilson；κ CI = 对条目重抽样的 percentile bootstrap；",
            "- Fleiss κ 仅用'三人齐标'的完整条目；两两用每对可得条目；",
            "- 所有比例均给 n 与 CI；n 小则区间宽，不作强断言。", ""]
    open(a.out, "w", encoding="utf-8", newline="\n").write("\n".join(out))
    if a.out_json:
        json.dump(result, open(a.out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"  ✅ 报告：{a.out}" + (f" ｜ JSON：{a.out_json}" if a.out_json else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
