# -*- coding: utf-8 -*-
"""collect_and_check.py —— 标注回收与检查（试点/正式通用）

流程：收齐 3 名标注者的 xlsx → 完整性/格式检查 → 生成 labels JSON
     → 一致性统计（κ/CI/一致率）→ 试点门槛判定（或正式批质检）→ 报告

用法：
  # 试点回收（3 份 试点表_A/B/C.xlsx）
  python -X utf8 实验/P2_20261108/collect_and_check.py --stage pilot \
      --files "<REPO>/回收/试点表_*.xlsx" --out-dir "<REPO>/回收检查"
  # 正式回收（可 1~3 批/人，文件名含 A/B/C 即可自动归人）
  python -X utf8 实验/P2_20261108/collect_and_check.py --stage formal \
      --files "<REPO>/回收/正式表_*.xlsx" --out-dir "<REPO>/回收检查"

输出：
  - 回收检查报告.md（缺失/非法/可疑/一致性/门槛判定）
  - labels_<A|B|C>.json（供 stats.py / adjudicate.py 直接使用）
  - summary.json
退出码：0=全部合法且（试点门槛通过）；1=有问题/门槛未过。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import sys

from openpyxl import load_workbook

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER = os.path.abspath(os.path.join(HERE, "..", ".."))          # …/<REPO>
PROJ = os.path.abspath(os.path.join(PAPER, "..", "项目", "<REPO>"))
sys.path.insert(0, os.path.join(PROJ, "annotate"))
sys.stdout.reconfigure(encoding="utf-8")

import stats as S      # noqa: E402

STATES = ["supported", "partially_supported", "no_evidence", "contradicted", "not_scored"]
ZH = {"supported": "S", "partially_supported": "P", "no_evidence": "N",
      "contradicted": "C", "not_scored": "L"}
LETTER = {"S": "supported", "P": "partially_supported", "N": "no_evidence",
          "C": "contradicted", "L": "not_scored"}


def read_table(path: str) -> tuple[dict[str, str | None], list[str], int]:
    """读一份回收表 → (id→state or None, 非法/异常说明, flag 数)。"""
    wb = load_workbook(path, data_only=True)
    if "标注表" not in wb.sheetnames:
        raise ValueError(f"{os.path.basename(path)} 缺少「标注表」sheet")
    ws = wb["标注表"]
    labels: dict[str, str | None] = {}
    notes = []
    flags = 0
    header = None
    for row in ws.iter_rows(min_row=1, values_only=True):
        if header is None:
            header = [str(c or "").strip() for c in row]
            continue
        if not row or not row[0]:
            continue
        iid = str(row[0]).strip()
        cell = str(row[3] or "").strip().upper()
        # flag 在第 5 列（E）或任意更右列
        extra = " ".join(str(c) for c in row[4:] if c)
        if extra.strip():
            flags += 1
        if not cell:
            labels[iid] = None
            notes.append(f"{iid}: 未填")
            continue
        m = re.match(r"^[SPNCL]$", cell)
        if not m:
            # 宽容：取第一个合法字母，但记录
            cand = [ch for ch in cell if ch in LETTER]
            if len(cand) == 1:
                notes.append(f"{iid}: 写法不规范 `{cell}`（已按 {cand[0]} 收）")
                labels[iid] = LETTER[cand[0]]
            else:
                labels[iid] = None
                notes.append(f"{iid}: 非法判定 `{cell}`")
            continue
        labels[iid] = LETTER[cell]
    return labels, notes, flags


def who(path: str) -> str | None:
    """从文件名推断标注者 A/B/C（兼容 正式表_A_批1.xlsx / 试点表_B.xlsx / A.xlsx）。"""
    base = os.path.basename(path)
    m = re.search(r"(?:^|[_-])([ABC])(?:[_-]|\.|$)", base)
    return m.group(1) if m else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, help="pilot | formal | round2_rare | round2_recheck 等任意阶段名")
    ap.add_argument("--files", required=True, help="glob，如 '<REPO>/回收/试点表_*.xlsx'")
    ap.add_argument("--expected", default="", help="可选：items JSON（默认自动取 pilot/formal 标准文件）")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--bootstrap", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args(argv)

    # 期望条目
    if not a.expected:
        if a.stage == "pilot":
            a.expected = os.path.join(PAPER, "<REPO>", "_pilot", "items_pilot30.json")
        else:
            a.expected = os.path.join(PAPER, "<REPO>", "_formal", "items_360.json")
    d = json.load(open(a.expected, encoding="utf-8"))
    exp_items = d["items"] if isinstance(d, dict) else d
    exp_ids = [str(it["id"]) for it in exp_items]
    exp_set = set(exp_ids)

    paths = sorted(glob.glob(a.files))
    if not paths:
        raise SystemExit(f"❌ 没有匹配文件：{a.files}")
    os.makedirs(a.out_dir, exist_ok=True)

    # 按标注者归并（同一人可多个批次文件 → 合并）
    by_person: dict[str, dict[str, str | None]] = {}
    person_flags, person_notes, person_files = {}, {}, {}
    for p in paths:
        w = who(p)
        if not w:
            print(f"  ⚠ 无法从文件名判断标注者：{os.path.basename(p)}（跳过）")
            continue
        try:
            labels, notes, flags = read_table(p)
        except Exception as e:      # noqa: BLE001
            person_notes.setdefault(w, []).append(f"{os.path.basename(p)} 读取失败：{e}")
            continue
        person_files.setdefault(w, []).append(os.path.basename(p))
        merged = by_person.setdefault(w, {})
        for k, v in labels.items():
            if v is None:
                merged.setdefault(k, None)      # 空值不覆盖已填值（多批次合并）
                continue
            if merged.get(k) is not None and merged[k] != v:
                person_notes.setdefault(w, []).append(f"{k}: 多批次重复且判定不一致（取后一个）")
            merged[k] = v
        person_flags[w] = person_flags.get(w, 0) + flags
        person_notes.setdefault(w, []).extend(notes)

    report = [f"# 回收检查报告（{a.stage}）", "",
              f"> 期望条目：{len(exp_ids)} ｜ 收到文件：{len(paths)} ｜ 标注者：{', '.join(sorted(by_person))}",
              ""]
    problems = 0
    labels_for_stats = []
    distributions = {}

    report += ["## 一、每人完整性/格式", "",
               "| 标注者 | 文件 | 已填 | 缺失 | 非法/未填 | flag | 状态 |",
               "|---|---|---|---|---|---|---|"]
    for w in sorted(by_person):
        lab = by_person[w]
        filled = sum(1 for v in lab.values() if v)
        missing = [i for i in exp_ids if lab.get(i) is None]
        extra = [i for i in lab if i not in exp_set]
        bad = len(missing) + len(extra)
        status = "✅" if bad == 0 else "❌"
        if bad:
            problems += 1
        report.append(f"| {w} | {'、'.join(person_files.get(w, []))} | {filled} | "
                      f"{len(missing)} | {len(extra)} | {person_flags.get(w, 0)} | {status} |")
        if missing:
            report.append(f"|  | 缺失示例：{missing[:5]} |  |  |  |  |  |")
        if extra:
            report.append(f"|  | 多余 ID 示例：{extra[:5]} |  |  |  |  |  |")
        distributions[w] = {s: sum(1 for v in lab.values() if v == s) for s in STATES}
        labels_for_stats.append({"annotator": w, "labels": {k: v for k, v in lab.items() if v}})

    if problems:
        report += ["", f"⚠ 有 {problems} 人存在缺失/多余条目，需先打回补齐。", ""]

    # 格式/填写问题明细
    if person_notes:
        report += ["## 一之二、格式与填写问题（前 10 条/人）", ""]
        for w in sorted(person_notes):
            notes = person_notes[w]
            report.append(f"- **{w}**（{len(notes)} 条）：" + "；".join(notes[:10]))
        report.append("")

    # 可疑检测
    report += ["## 二、可疑信号", ""]
    suspicious = []
    for i, d1 in enumerate(labels_for_stats):
        vals = list(d1["labels"].values())
        if vals:
            top = max(set(vals), key=vals.count)
            share = vals.count(top) / len(vals)
            if share >= 0.90:
                suspicious.append(f"{d1['annotator']}: 单态占比 {share:.0%}（疑似整批贴同一标签）")
        for d2 in labels_for_stats[i + 1:]:
            common = set(d1["labels"]) & set(d2["labels"])
            if common:
                same = sum(1 for k in common if d1["labels"][k] == d2["labels"][k])
                ov = same / len(common)
                if ov >= 0.90:
                    suspicious.append(f"{d1['annotator']} vs {d2['annotator']}: 逐条重合 {ov:.0%}（疑似代填/抄表）")
    report += ([f"- ⚠ {s}" for s in suspicious] if suspicious else ["- 未发现整批单态/高重合信号"])
    report.append("")

    # 一致性统计
    report += ["## 三、一致性", ""]
    if len(labels_for_stats) >= 2:
        names = [d["annotator"] for d in labels_for_stats]
        import itertools
        report += ["| 标注者对 | n | 一致率 | Wilson 95%CI | Cohen κ | κ bootstrap 95%CI |",
                   "|---|---|---|---|---|---|"]
        pairs_all = []
        for x, y in itertools.combinations(names, 2):
            lx, ly = labels_for_stats[names.index(x)]["labels"], labels_for_stats[names.index(y)]["labels"]
            common = sorted(set(lx) & set(ly))
            pairs = [(lx[i], ly[i]) for i in common]
            pairs_all.extend(pairs)
            n = len(pairs); k = sum(1 for p, q in pairs if p == q)
            lo, hi = S.wilson(k, n)
            kap = S.cohen_kappa(pairs)
            klo, khi = S.bootstrap_ci(pairs, S.cohen_kappa, a.bootstrap, a.seed)
            report.append(f"| {x} vs {y} | {n} | {k / n * 100:.1f}% | {lo * 100:.1f}%–{hi * 100:.1f}% | "
                          f"{kap:.3f} | {klo:.3f}–{khi:.3f} |")
        if len(names) >= 3:
            complete = [i for i in exp_ids
                        if all(i in d["labels"] for d in labels_for_stats)]
            matrix = [[next(d["labels"][i] for d in labels_for_stats if d["annotator"] == nm) for nm in names]
                      for i in complete]
            if matrix:
                fk = S.fleiss_kappa(matrix)
                flo, fhi = S.bootstrap_ci(matrix, S.fleiss_kappa, max(1000, a.bootstrap // 2), a.seed)
                report.append("")
                report.append(f"- **Fleiss κ = {fk:.3f}**（n={len(complete)}，bootstrap 95%CI {flo:.3f}–{fhi:.3f}）")
        report.append("")
        report.append("五态分布：")
        for w, dist in distributions.items():
            report.append(f"- {w}: " + " ｜ ".join(f"{ZH[s]}={dist.get(s, 0)}" for s in STATES))
    else:
        report.append("- 不足 2 人，无法计算一致性")
    report.append("")

    # 门槛/质检
    summary = {"stage": a.stage, "expected": len(exp_ids), "received_files": len(paths),
               "annotators": sorted(by_person), "problems": problems,
               "suspicious": suspicious,
               "distributions": distributions}
    gate = None
    if a.stage == "pilot":
        report += ["## 四、试点门槛判定（30 条）", "",
                   "门槛：Fleiss κ≥0.60（2 人取平均两两 κ）｜平均两两一致率≥70%｜S↔N 严重方向分歧≤2/30", ""]
        if len(labels_for_stats) >= 2 and problems == 0:
            import itertools
            rates, severe = [], 0
            for x, y in itertools.combinations([d["annotator"] for d in labels_for_stats], 2):
                lx = next(d["labels"] for d in labels_for_stats if d["annotator"] == x)
                ly = next(d["labels"] for d in labels_for_stats if d["annotator"] == y)
                common = sorted(set(lx) & set(ly))
                rates.append(sum(1 for i in common if lx[i] == ly[i]) / len(common))
                severe += sum(1 for i in common
                              if {lx[i], ly[i]} == {"supported", "no_evidence"})
            mean_rate = sum(rates) / len(rates)
            if len(labels_for_stats) >= 3:
                complete = [i for i in exp_ids if all(i in d["labels"] for d in labels_for_stats)]
                names = [d["annotator"] for d in labels_for_stats]
                matrix = [[next(d["labels"][i] for d in labels_for_stats if d["annotator"] == nm)
                           for nm in names] for i in complete]
                kappa = S.fleiss_kappa(matrix)
            else:
                lx = labels_for_stats[0]["labels"]; ly = labels_for_stats[1]["labels"]
                common = sorted(set(lx) & set(ly))
                kappa = S.cohen_kappa([(lx[i], ly[i]) for i in common])
            passed = (kappa >= 0.60) and (mean_rate >= 0.70) and (severe <= 2)
            gate = {"fleiss_or_pair_kappa": round(kappa, 4), "mean_pair_agreement": round(mean_rate, 4),
                    "severe_SN": severe, "passed": passed}
            report += [f"- κ = {kappa:.3f}（≥0.60 {'✅' if kappa >= 0.60 else '❌'}）",
                       f"- 平均两两一致率 = {mean_rate:.1%}（≥70% {'✅' if mean_rate >= 0.70 else '❌'}）",
                       f"- S↔N 方向分歧 = {severe}/30（≤2 {'✅' if severe <= 2 else '❌'}）",
                       "",
                       f"**判定：{'✅ 通过，可进入正式批' if passed else '❌ 未通过 → 按分歧类型分级补救 + 换 30 条新题复测'}**"]
        else:
            gate = {"passed": False, "reason": "人数不足或存在缺失/非法"}
            report.append("- ❌ 人数不足或存在缺失/非法，无法判定")

    # 输出（文件名带 stage，避免试点/正式互相覆盖）
    for d in labels_for_stats:
        json.dump(d, open(os.path.join(a.out_dir, f"labels_{a.stage}_{d['annotator']}.json"), "w",
                          encoding="utf-8"), ensure_ascii=False, indent=1)
    summary["gate"] = gate
    json.dump(summary, open(os.path.join(a.out_dir, f"summary_{a.stage}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    report_path = os.path.join(a.out_dir, f"回收检查报告_{a.stage}.md")
    open(report_path, "w", encoding="utf-8", newline="\n").write("\n".join(report))
    print(f"  ✅ 报告：{report_path}")
    print(f"  ✅ labels：{a.out_dir}\\labels_{a.stage}_*.json ｜ summary_{a.stage}.json")
    ok = (problems == 0) and (gate is None or gate.get("passed"))
    print(f"  结果：{'✅ 通过' if ok else '❌ 有问题/未过门槛'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
