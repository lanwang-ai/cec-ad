# -*- coding: utf-8 -*-
"""分层抽样工具：从抽样池抽取试点 30 条 / 正式 360 条（域 × 系统五态）。

规格依据（只读，未改动）：
  · `<REPO>/<REPO>/20261108_抽样与功效分析_v1.md` §3.7（主规格：域×五态分层、
    缺口回填顺序 C→S→P→L→N、每会话上限、锚题单列）、§3.3/§3.4（分配表）；
  · `<REPO>/<REPO>/20261110_试点门槛与时间表决定_v1.md` §1.2（试点 30 条：S6/P5/N2/C12/L5）；
  · `<REPO>/<REPO>/20261108_标注手册v2_v1.md` §7.3 / §8.1（题池、盲表字段）。

设计约束（对应验收）：
  · 只用标准库（argparse/hashlib/json/random/os/sys），不依赖 numpy/openpyxl；
  · 确定性：同 seed + 同输入 ⇒ 输出 JSON / 日志 / 映射逐字节一致（文件内不含时间戳；
    **禁止**依赖 dict/set 迭代顺序，凡需顺序处一律显式 sorted）；
  · 盲标：输出条目只保留 id/claim/evidence/domain/source/session_id，
    系统五态（state/system_state/strength）与人工答案（human/label 等）一律剥离，见 §2.7/§8.1；
  · 分层：域 → 系统五态；某层可用不足时全取并记录缺口，缺口按 C→S→P→L→N 回填
    （N 稀有、最后才动，不为凑配额牺牲其他层）；
  · 会话上限：同一 session_id 全局最多 --per-session-cap 条（控制聚类设计效应，§4.8）。

用法示例：
    # ① 正式 360 条：3 域 ×120，五态每域 50/20/5/30/15
    python -X utf8 annotate/sample.py --pool pool_all.json --out items_360.json \
        --n 360 --domains 120,120,120 --state-quota "S:50,P:20,N:5,C:30,L:15" \
        --seed 20261108 --per-session-cap 6 --log 抽样日志_360.md --id-map id_map_360.json

    # ② 试点 30 条：仅 D1（五态 6/5/2/12/5），同分布分层
    python -X utf8 annotate/sample.py --pool pool_D1.json --out items_pilot30.json \
        --n 30 --domains 30 --state-quota "S:6,P:5,N:2,C:12,L:5" --seed 20261108

    # ③ 带锚题：blind40 单独输出（不占 n），并从主样本排除重复 id/文本
    python -X utf8 annotate/sample.py ... --anchors bench/data/blind40.json

    # ④ 只按域抽、关闭五态分层
    python -X utf8 annotate/sample.py ... --state-quota none

输出（`--out`）结构：{"description","seed","n","input_sha256","items":[...]}；
`annotate/export.py` 只读 items，可直接喂现有导出工具。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys

# ============================ 常量 ============================

#: 系统五态的固定顺序（全文件凡遍历五态一律用这个顺序，保证确定性）
STATES = ["S", "P", "N", "C", "L"]
STATE_ZH = {
    "S": "有证据支持",
    "P": "部分支持",
    "N": "无证据",
    "C": "与证据矛盾",
    "L": "不判分",
}
#: 缺口回填优先顺序（§3.7：C/S/P 优先，L 次之，N 最后）
BACKFILL_ORDER = ["C", "S", "P", "L", "N"]
#: 未显式指定 --state-quota 时使用的正式方案默认配比（每域 50/20/5/30/15 = 120）
DEFAULT_STATE_QUOTA = "S:50,P:20,N:5,C:30,L:15"
#: 池内系统五态字段的别名（按优先级；state 为任务约定的主字段名）
STATE_KEYS = ("state", "system_state", "strength")
#: 盲标输出允许保留的字段（白名单；其余（含一切答案/系统判定）全部剔除）
PUBLIC_KEYS = ("id", "claim", "evidence", "domain", "source", "session_id")

# 五态别名归一（含 quality_audit 的新五态名与旧五态名；键一律 casefold 比较）
_STATE_ALIASES = {
    "s": "S", "supported": "S", "strong": "S", "有证据支持": "S",
    "p": "P", "partially_supported": "P", "partial": "P", "部分支持": "P",
    "n": "N", "no_evidence": "N", "none": "N", "无证据": "N",
    "c": "C", "contradicted": "C", "contradict": "C", "与证据矛盾": "C", "矛盾": "C",
    "l": "L", "not_scored": "L", "plan": "L", "skip": "L", "不判分": "L",
}
_STATE_TOKEN = {k.casefold(): v for k, v in _STATE_ALIASES.items()}

# 域别名归一：D1 文件/代码、D2 数据分析、D3 文案/检索（含 profile 名与旧硬编码名）
_DOMAIN_ALIASES = {
    "D1": ["D1", "general", "文件/代码", "文件代码", "文件与代码", "文件操作",
           "代码编写", "代码", "coding", "file_code"],
    "D2": ["D2", "data_analysis", "数据分析", "数据", "data"],
    "D3": ["D3", "writing_retrieval", "文案/检索", "文案检索", "文案", "检索",
           "写作", "writing"],
}
_DOMAIN_TOKEN = {}
for _canon, _aliases in _DOMAIN_ALIASES.items():
    for _alias in _aliases:
        _DOMAIN_TOKEN[_alias.casefold()] = _canon.casefold()


class SampleError(Exception):
    """可读的输入/参数错误（main 捕获后打印中文提示并以 2 退出）。"""


# ============================ 基础工具 ============================

def sha256_file(path: str) -> str:
    """文件内容的 SHA256（十六进制小写）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def derive_rng(seed: int, *parts: object) -> random.Random:
    """由 (seed, 分层键) 派生子随机数发生器。

    每个层/阶段用独立 RNG，避免「上层抽取条数变化会改变下层随机序列」这种隐性耦合；
    seed 经 sha256 派生，保证跨平台/跨 Python 版本可复现（random 只用于 shuffle）。
    """
    key = "|".join([str(seed)] + [str(p) for p in parts])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def normalize_state(value: object) -> str | None:
    """把五态值归一为 S/P/N/C/L；缺失或无法识别返回 None。"""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    return _STATE_TOKEN.get(text.casefold())


def domain_token(value: object) -> str | None:
    """把域标签归一成一个比较令牌（别名 → d1/d2/d3；未知标签 → 小写原文）。"""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    return _DOMAIN_TOKEN.get(text.casefold(), text.casefold())


def claim_key(claim: str) -> str:
    """断言文本去重键：折叠连续空白（同 claim 文本只留 1 条，见 §2.6 入选标准⑤）。"""
    return " ".join(str(claim).split())


def load_item_list(path: str, what: str) -> list:
    """读入条目数组：接受 {"items": [...]} 或裸数组。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise SampleError(f"{what}文件不存在：{path}")
    except json.JSONDecodeError as exc:
        raise SampleError(f"{what}文件不是合法 JSON：{path}（第 {exc.lineno} 行：{exc.msg}）")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    else:
        raise SampleError(
            f"{what}文件缺少 items 数组：{path}（顶层应为 {{\"items\": [...]}} 或裸数组）")
    return items


def _dump_json(path: str, obj: object) -> None:
    """确定性 JSON 落盘（UTF-8、缩进 1、末尾换行、固定 \\n 换行符）。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def _write_text(path: str, text: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _same_path(p1: str, p2: str) -> bool:
    return os.path.abspath(p1) == os.path.abspath(p2)


def _default_log_path(out_path: str) -> str:
    base, _ext = os.path.splitext(out_path)
    return base + "_log.md"


def _default_anchors_path(out_path: str) -> str:
    base, _ext = os.path.splitext(out_path)
    return base + "_anchors.json"


# ============================ 参数解析 ============================

def _parse_quota_map(text: str, flag: str) -> list[tuple[str, int]]:
    """解析域配额映射文本："D1:120,D2:120,D3:120" → [("D1",120), ...]。"""
    parts = [p.strip() for p in str(text).split(",") if p.strip()]
    if not parts:
        raise SampleError(f"{flag} 不能为空；示例：--quota \"D1:120,D2:120,D3:120\"")
    pairs: list[tuple[str, int]] = []
    for part in parts:
        if ":" not in part:
            raise SampleError(f"{flag} 每项需为「域名:条数」：{part!r}")
        name, num = part.split(":", 1)
        name, num = name.strip(), num.strip()
        if not name:
            raise SampleError(f"{flag} 域名不能为空：{part!r}")
        try:
            count = int(num)
        except ValueError:
            raise SampleError(f"{flag} 条数必须为整数：{part!r}")
        pairs.append((name, count))
    return pairs


def _build_domain_specs(a: argparse.Namespace) -> list[dict]:
    """把 --domains / --per-domain / --quota 统一成域规格列表（保持命令行给出顺序）。"""
    if a.domains is not None:
        text = str(a.domains).strip()
        if ":" in text:  # 允许 --domains "D1:120,D2:120" 这种写法
            pairs = _parse_quota_map(text, "--domains")
        else:
            parts = [p.strip() for p in text.split(",") if p.strip()]
            if not parts:
                raise SampleError("--domains 不能为空；示例：--domains \"120,120,120\"")
            quotas: list[int] = []
            for part in parts:
                try:
                    quotas.append(int(part))
                except ValueError:
                    raise SampleError(
                        f"--domains 需为逗号分隔的整数（如 120,120,120），无法解析：{part!r}")
            pairs = [(f"D{i + 1}", q) for i, q in enumerate(quotas)]
    elif a.per_domain is not None:
        per = int(a.per_domain)
        if per <= 0:
            raise SampleError(f"--per-domain 必须为正整数：{per}")
        if a.n % per:
            raise SampleError(
                f"--n {a.n} 不能被 --per-domain {per} 整除（余 {a.n % per}）；"
                f"请改用 --quota 指定各域配额")
        pairs = [(f"D{i + 1}", per) for i in range(a.n // per)]
    else:
        pairs = _parse_quota_map(str(a.quota), "--quota")

    if not pairs:
        raise SampleError("未给出任何域配额（--domains / --per-domain / --quota）")

    specs: list[dict] = []
    seen: dict[str, str] = {}
    for name, count in pairs:
        if count < 0:
            raise SampleError(f"域配额不能为负：{name}:{count}")
        token = domain_token(name)
        if token in seen:
            raise SampleError(
                f"域配额重复或别名冲突：{name} 与 {seen[token]} 指向同一个域")
        seen[token] = name
        specs.append({"key": name, "token": token, "quota": count})

    total = sum(spec["quota"] for spec in specs)
    if total != a.n:
        raise SampleError(
            f"域配额合计 {total} ≠ --n {a.n}；请检查 --domains/--per-domain/--quota")
    return specs


def _parse_state_quota(text: str) -> dict[str, int] | None:
    """解析域内五态配额（可为绝对数或配比）：返回 {S:...,P:...}；none/off 表示关闭分层。"""
    raw = str(text).strip()
    if not raw or raw.casefold() in ("none", "off", "0", "-", "不分层"):
        return None
    weights = {state: 0 for state in STATES}
    given: set[str] = set()
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if ":" not in part:
            raise SampleError(f"--state-quota 每项需为「状态:条数」：{part!r}")
        key, num = part.split(":", 1)
        state = normalize_state(key)
        if state is None:
            raise SampleError(
                f"--state-quota 无法识别状态 {key.strip()!r}；"
                f"允许 S/P/N/C/L（或 supported/partially_supported/no_evidence/"
                f"contradicted/not_scored）")
        if state in given:
            raise SampleError(f"--state-quota 状态重复：{state}")
        try:
            count = int(num.strip())
        except ValueError:
            raise SampleError(f"--state-quota 条数必须为整数：{part!r}")
        if count < 0:
            raise SampleError(f"--state-quota 条数不能为负：{part!r}")
        weights[state] = count
        given.add(state)
    missing = [state for state in STATES if state not in given]
    if missing:
        raise SampleError(
            f"--state-quota 需包含五态 S/P/N/C/L 全部（缺少：{'、'.join(missing)}）；"
            f"如不想做五态分层请用 --state-quota none")
    if sum(weights.values()) <= 0:
        raise SampleError("--state-quota 各状态条数合计必须 > 0")
    return weights


def apportion(total: int, weights: dict[str, int]) -> dict[str, int]:
    """把五态权重按比例分配到 total 条（最大余数法，平票按 STATES 固定顺序）。

    · weights 合计 == total 时结果就是精确配额（正式 120/试点 30 的常规用法）；
    · 否则按配比等比缩放（如权重 6/5/2/12/5 用于每域 120 条时自动放大 4 倍）。
    """
    total_w = sum(weights.values())
    raw = {state: weights[state] * total / total_w for state in STATES}
    alloc = {state: int(raw[state]) for state in STATES}  # 非负数，int 即向下取整
    remainder = total - sum(alloc.values())
    order = sorted(STATES, key=lambda s: (-(raw[s] - alloc[s]), STATES.index(s)))
    for state in order[:max(0, remainder)]:
        alloc[state] += 1
    return alloc


def _build_state_targets(specs: list[dict], weights: dict[str, int]) -> dict[str, dict[str, int]]:
    """按域生成五态目标：{域 key: {S:..,P:..,N:..,C:..,L:..}}。"""
    return {spec["key"]: apportion(spec["quota"], weights) for spec in specs}


# ============================ 条目清洗 ============================

def _validate_items(raw_items: list, skip: list[tuple[str, str]]) -> list[dict]:
    """校验条目并归一：缺 id/claim/evidence 或 evidence 元素非对象 → 记入 skip 并跳过。

    skip 元素为 (可读标签, 原因)；输出条目保留原始 id/claim/evidence 与归一后的域/五态。
    """
    cleaned: list[dict] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            skip.append((f"<第 {index} 条>", "条目不是 JSON 对象"))
            continue
        raw_id = raw.get("id")
        item_id = str(raw_id).strip() if raw_id is not None else ""
        if not item_id:
            skip.append((f"<第 {index} 条，无 id>", "缺 id"))
            continue
        claim = raw.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            skip.append((item_id, "缺 claim（或 claim 不是非空字符串）"))
            continue
        evidence = raw.get("evidence")
        if not isinstance(evidence, list):
            skip.append((item_id, "缺 evidence（或 evidence 不是数组）"))
            continue
        bad_element = None
        for ev_index, ev in enumerate(evidence):
            if not isinstance(ev, dict):
                bad_element = ev_index
                break
        if bad_element is not None:
            skip.append(
                (item_id, f"evidence 第 {bad_element + 1} 个元素不是对象（export.py 无法渲染）"))
            continue

        # 系统五态：按 STATE_KEYS 优先级取第一个非空字段
        state_raw = None
        for key in STATE_KEYS:
            if key in raw and raw[key] is not None:
                state_raw = raw[key]
                break
        state = normalize_state(state_raw)

        session_id = raw.get("session_id")
        session_id = str(session_id).strip() if session_id is not None else ""
        if not session_id:
            session_id = ""
        source = raw.get("source")
        source = str(source).strip() if source is not None else ""

        cleaned.append({
            "id": item_id,
            "claim": claim,
            "claim_key": claim_key(claim),
            "evidence": evidence,
            "domain_raw": raw.get("domain"),
            "domain_token": domain_token(raw.get("domain")),
            "source": source,
            "session_id": session_id,
            # 无 session_id 的条目按「各自独立会话」处理（无法聚类的退化口径，日志记录）
            "_sid": session_id or f"__no_session__:{item_id}",
            "state": state,
            "state_raw": state_raw,
        })
    return cleaned


def _dedupe_items(items: list[dict]) -> tuple[list[dict], list[tuple[str, str]], list[tuple[str, str]]]:
    """确定性去重：按 id 排序后先 id 去重，再按 claim 文本去重（保留排序靠前者）。

    返回 (去重后条目, 重复 id 记录, 重复 claim 记录)。
    """
    ordered = sorted(items, key=lambda it: it["id"])
    seen_ids: set[str] = set()
    dup_id: list[tuple[str, str]] = []
    uniq: list[dict] = []
    for item in ordered:
        if item["id"] in seen_ids:
            dup_id.append((item["id"], "与前面同 id 条目重复"))
            continue
        seen_ids.add(item["id"])
        uniq.append(item)
    seen_claims: dict[str, str] = {}
    dup_claim: list[tuple[str, str]] = []
    deduped: list[dict] = []
    for item in uniq:
        first_id = seen_claims.get(item["claim_key"])
        if first_id is not None:
            dup_claim.append((item["id"], f"与 {first_id} 的 claim 文本重复"))
            continue
        seen_claims[item["claim_key"]] = item["id"]
        deduped.append(item)
    return deduped, dup_id, dup_claim


# ============================ 分层抽样 ============================

def _public_item(item: dict, out_domain: str) -> dict:
    """构造盲标输出条目：只保留白名单字段，系统五态/答案一律不出现。"""
    public = {
        "id": item["id"],
        "claim": item["claim"],
        "evidence": item["evidence"],
        "domain": out_domain,
    }
    for key in ("source", "session_id"):
        value = item.get(key)
        if value:
            public[key] = value
    return public


def _sample_domain(
    spec: dict,
    candidates: list[dict],
    targets: dict[str, int] | None,
    seed: int,
    cap: int,
    session_counts: dict[str, int],
    pick,
) -> dict:
    """在单个域内做五态分层抽样 + 缺口回填 + 会话上限控制。

    targets 为 None 时只按域抽（关闭五态分层）；否则按五态层逐层抽。
    """
    domain = spec["key"]
    quota = spec["quota"]
    selected: list[dict] = []
    strata: list[dict] = []
    shortfalls: list[dict] = []
    backfill_events: list[dict] = []

    def record_into(state: str, mode: str, items: list[dict]) -> None:
        for item in items:
            selected.append({
                "item": item,
                "domain": domain,
                "state": item["state"],
                "stratum": f"{domain}/{state}" if targets is not None else domain,
                "mode": mode,
            })

    if targets is None:
        # —— 只按域抽（state 不作分层）——
        order = list(candidates)
        derive_rng(seed, spec["token"], "main", "-").shuffle(order)
        picked = pick(order, quota)
        record_into("-", "target", picked)
        gap = quota - len(picked)
        reason = _reason_text(len(candidates), len(picked), quota, cap)
        strata.append({
            "state": "-", "target": quota, "available": len(candidates),
            "first": len(picked), "first_gap": gap, "backfill": 0,
            "final": len(picked), "final_gap": gap, "reason": reason,
        })
        if gap > 0:
            shortfalls.append({"state": "-", "target": quota, "available": len(candidates),
                               "first": len(picked), "gap": gap})
    else:
        # —— 第一轮：每层按目标抽（**稀缺优先**：target/available 降序 → N/P/C/L/S）——
        # 背景：若按 STATES 固定顺序先抽 S，长会话的 per-session-cap 槽位会被 S 占满，
        # 导致 P/C/L 即使池内充足也抽不到（2026-11-10 实测 D2/D3 缺口 40 条）。
        # 改成稀缺层先占槽位后，S 作为充裕层最后抽，可显著降低缺口。
        def _scarce_rank(st: str) -> float:
            avail = sum(1 for it in candidates if it["state"] == st)
            t = targets.get(st, 0)
            return (t / avail) if avail else 0.0
        first_order = sorted(STATES, key=_scarce_rank, reverse=True)
        remaining: dict[str, list[dict]] = {}
        for state in first_order:
            target = targets.get(state, 0)
            layer = [it for it in candidates if it["state"] == state]
            order = list(layer)
            derive_rng(seed, spec["token"], "main", state).shuffle(order)
            picked = pick(order, target)
            picked_ids = {it["id"] for it in picked}
            remaining[state] = [it for it in order if it["id"] not in picked_ids]
            record_into(state, "target", picked)
            first_gap = max(0, target - len(picked))
            reason = _reason_text(len(layer), len(picked), target, cap) if first_gap else ""
            strata.append({
                "state": state, "target": target, "available": len(layer),
                "first": len(picked), "first_gap": first_gap, "backfill": 0,
                "final": len(picked), "final_gap": first_gap, "reason": reason,
            })
            if first_gap > 0:
                shortfalls.append({"state": state, "target": target,
                                   "available": len(layer), "first": len(picked),
                                   "gap": first_gap})

        # —— 第二轮：缺口按 C→S→P→L→N 回填（N 最后，不为凑配额牺牲其他层）——
        gap = quota - len(selected)
        for state in BACKFILL_ORDER:
            if gap <= 0:
                break
            order = list(remaining[state])
            if not order:
                continue
            derive_rng(seed, spec["token"], "backfill", state).shuffle(order)
            picked = pick(order, gap)
            if not picked:
                continue
            record_into(state, "backfill", picked)
            row = next(row for row in strata if row["state"] == state)
            row["backfill"] += len(picked)
            row["final"] += len(picked)
            row["final_gap"] = max(0, row["target"] - row["final"])
            backfill_events.append({"state": state, "count": len(picked)})
            gap -= len(picked)

    final_gap = quota - len(selected)
    return {
        "spec": spec,
        "targets": targets,
        "strata": strata,
        "selected": selected,
        "shortfalls": shortfalls,
        "backfill_events": backfill_events,
        "gap": final_gap,
    }


def _reason_text(available: int, picked: int, target: int, cap: int) -> str:
    """解释某层为什么没抽满（用于日志）。"""
    reasons: list[str] = []
    if available < target:
        reasons.append("层内可用不足")
    if picked < min(available, target):
        reasons.append(f"受会话上限限制（每会话 ≤{cap} 条）")
    return "、".join(reasons)


# ============================ 日志渲染 ============================

def _render_domain_quota(specs: list[dict]) -> str:
    return "，".join(f"{spec['key']}={spec['quota']}" for spec in specs)


def _render_targets(weights: dict[str, int]) -> str:
    return "、".join(f"{state}={weights[state]}" for state in STATES)


def _render_log(ctx: dict) -> str:
    """生成确定性 Markdown 抽样日志（不含时间戳，可逐字节复跑）。"""
    a = ctx["args"]
    specs = ctx["specs"]
    weights = ctx["weights"]
    stats = ctx["stats"]
    domain_results = ctx["domain_results"]
    out_info = ctx["out_info"]
    anchor_info = ctx["anchor_info"]
    final_recs = ctx["final_recs"]
    session = ctx["session"]

    lines: list[str] = []
    lines.append("# 分层抽样日志")
    lines.append("")
    lines.append("> 由 `annotate/sample.py` 自动生成，用于抽样可复现与缺口审计；同 seed + 同输入 ⇒ 逐字节可复现。")
    lines.append("> ⚠️ 本文件含**系统五态**等盲标信息，**不得随标注表分发给标注者**。")
    lines.append("")

    # ---- 0 参数与输入 ----
    lines.append("## 0. 参数与输入")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 输入池 | `{a.pool}` |")
    lines.append(f"| 输入池 SHA256 | `{ctx['pool_hash']}` |")
    lines.append(f"| 输入池条目数 | {stats['raw']} |")
    lines.append(f"| 随机种子 seed | {a.seed} |")
    lines.append(f"| 目标总数 n | {a.n} |")
    lines.append(f"| 域配额 | {_render_domain_quota(specs)} |")
    if weights is None:
        lines.append("| 五态配额（每域） | 未启用（--state-quota none，仅按域抽样） |")
        lines.append("| 五态目标生成方式 | — |")
    else:
        lines.append(f"| 五态配额（每域） | {_render_targets(weights)} |")
        if all(spec["quota"] == sum(weights.values()) for spec in specs):
            lines.append(f"| 五态目标生成方式 | 精确（权重合计 {sum(weights.values())} = 每域配额） |")
        else:
            lines.append(f"| 五态目标生成方式 | 按域配额等比分配（权重合计 {sum(weights.values())}；"
                         f"各域目标见 §2 表） |")
    lines.append(f"| 每会话上限 | {a.per_session_cap} |")
    if anchor_info is not None:
        lines.append(f"| 锚题文件 | `{anchor_info['path']}`（{len(anchor_info['public'])} 条，"
                     f"另存，不占 n） |")
    else:
        lines.append("| 锚题文件 | 未提供 |")
    lines.append(f"| 输出文件 | `{out_info['path']}` |")
    lines.append(f"| 追溯映射 | `{a.id_map}` |" if a.id_map else "| 追溯映射 | 未提供（建议用 --id-map 封存） |")
    lines.append(f"| 命令行 | `{ctx['cmdline']}` |")
    lines.append("")

    # ---- 1 池清洗 ----
    lines.append("## 1. 池清洗")
    lines.append("")
    lines.append(f"- 原始条目：{stats['raw']} 条")
    lines.append(f"- 非法/不完整条目跳过：{len(stats['invalid'])} 条")
    lines.append(f"- 重复 id 跳过：{len(stats['dup_id'])} 条；重复 claim 跳过：{len(stats['dup_claim'])} 条")
    lines.append(f"- 未匹配域配额：{stats['unmatched_total']} 条{stats['unmatched_text']}")
    lines.append(f"- 与锚题重叠排除：{len(stats['anchor_overlap'])} 条")
    if weights is not None:
        lines.append(f"- 无/无法识别系统五态（不纳入五态分层）：{len(stats['no_state'])} 条")
    if stats["fallback"]["count"]:
        lines.append(f"- 域缺失/未匹配但仅有一个域配额，归入 {specs[0]['key']}："
                     f"{stats['fallback']['count']} 条{stats['fallback']['text']}")
    lines.append(f"- 可参与分层抽样：{stats['eligible']} 条")
    lines.append("")

    # ---- 2 域×五态分层表 ----
    lines.append("## 2. 域 × 五态分层表")
    lines.append("")
    lines.append("| 域 | 五态 | 目标 | 层内可用 | 首轮入选 | 首轮缺口 | 回填入选 | 最终入选 | 最终缺口 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    total_first = total_backfill = total_available = total_target = 0
    for result in domain_results:
        for row in result["strata"]:
            total_target += row["target"]
            total_available += row["available"]
            total_first += row["first"]
            total_backfill += row["backfill"]
            lines.append(
                f"| {result['spec']['key']} | {row['state']} | {row['target']} | "
                f"{row['available']} | {row['first']} | {row['first_gap']} | "
                f"{row['backfill']} | {row['final']} | {row['final_gap']} |")
    lines.append(
        f"| **合计** | - | {total_target} | {total_available} | {total_first} | "
        f"{sum(row['first_gap'] for r in domain_results for row in r['strata'])} | "
        f"{total_backfill} | {len(final_recs)} | {a.n - len(final_recs)} |")
    lines.append("")

    # ---- 3 缺口与回填 ----
    lines.append("## 3. 缺口与回填")
    lines.append("")
    lines.append("- 回填优先顺序：C → S → P → L → N（N 稀有、最后才动；不为凑配额牺牲其他层）")
    has_gap_line = False
    for result in domain_results:
        domain = result["spec"]["key"]
        for short in result["shortfalls"]:
            state = short["state"]
            reasons = []
            if short["available"] < short["target"]:
                reasons.append("层内可用不足")
            if short["first"] < min(short["available"], short["target"]):
                reasons.append(f"受会话上限限制（每会话 ≤{a.per_session_cap} 条）")
            lines.append(
                f"- 缺口：{domain}/{state} 目标 {short['target']} 条、"
                f"层内可用 {short['available']} 条、首轮入选 {short['first']} 条、"
                f"缺口 {short['gap']} 条（原因：{'、'.join(reasons) or '未知'}）")
            has_gap_line = True
        if result["backfill_events"] and result["shortfalls"]:
            source_text = "、".join(short["state"] for short in result["shortfalls"])
            total_gap = sum(short["gap"] for short in result["shortfalls"])
            for event in result["backfill_events"]:
                lines.append(
                    f"- 回填：{domain} 从 {event['state']} 层回填 {event['count']} 条"
                    f"（补 {source_text} 层缺口合计 {total_gap} 条）")
    if not has_gap_line:
        lines.append("- 各层均满足目标：无缺口、无回填。")
    final_gap_total = a.n - len(final_recs)
    if final_gap_total > 0:
        for result in domain_results:
            if result["gap"] > 0:
                lines.append(
                    f"- 最终缺口：{result['spec']['key']} 目标 {result['spec']['quota']} 条、"
                    f"实抽 {result['spec']['quota'] - result['gap']} 条、缺口 {result['gap']} 条"
                    f"（原因：池内可用不足或达到会话上限）")
        lines.append(f"- **总计**：目标 {a.n} 条、实抽 {len(final_recs)} 条、缺口 {final_gap_total} 条。")
    else:
        lines.append(f"- 无最终缺口：实抽 {len(final_recs)} 条 = 目标 {a.n} 条。")
    lines.append("")

    # ---- 4 会话分布 ----
    lines.append("## 4. 会话分布（聚类控制）")
    lines.append("")
    lines.append(f"- 实抽条目所属会话数：{session['unique']} 个")
    lines.append(f"- 单会话最多入选：{session['max']} 条（上限 {a.per_session_cap}；"
                 f"超限会话数：{session['over']}）")
    lines.append(f"- 无 session_id 条目：{session['no_sid']} 条（按各自独立会话处理）")
    if session["top"]:
        top_text = "、".join(f"`{sid}`={count}" for sid, count in session["top"])
        lines.append(f"- 会话入选条数 Top{len(session['top'])}：{top_text}")
    lines.append("")

    # ---- 5 锚题 ----
    lines.append("## 5. 锚题（跨轮校准，不占 n）")
    lines.append("")
    if anchor_info is None:
        lines.append("- 未提供锚题（--anchors）。")
    else:
        lines.append(f"- 锚题输入：`{anchor_info['path']}`（{anchor_info['raw']} 条，"
                     f"sha256 `{anchor_info['sha256']}`）")
        lines.append(f"- 锚题非法/不完整跳过：{len(anchor_info['invalid'])} 条；"
                     f"重复 id：{len(anchor_info['dup_id'])} 条；重复 claim：{len(anchor_info['dup_claim'])} 条")
        lines.append(f"- 锚题输出：`{anchor_info['out_path']}`（{len(anchor_info['public'])} 条，"
                     f"seed 打乱；sha256 `{anchor_info['out_sha256']}`）")
        lines.append(f"- 锚题与主样本重叠：{anchor_info['overlap_count']} 条（已从主样本排除，锚题单列）")
    lines.append("")

    # ---- 6 跳过明细 ----
    lines.append("## 6. 跳过与告警明细（每类最多列 20 条）")
    lines.append("")
    if stats["invalid"]:
        lines.append(f"### 6.1 非法/不完整条目（{len(stats['invalid'])} 条）")
        lines.append("")
        for label, reason in stats["invalid"][:20]:
            lines.append(f"- `{label}`：{reason}")
        if len(stats["invalid"]) > 20:
            lines.append(f"- ……其余 {len(stats['invalid']) - 20} 条省略")
        lines.append("")
    if stats["dup_id"]:
        lines.append(f"### 6.2 重复 id（{len(stats['dup_id'])} 条）")
        lines.append("")
        for label, reason in stats["dup_id"][:20]:
            lines.append(f"- `{label}`：{reason}")
        lines.append("")
    if stats["dup_claim"]:
        lines.append(f"### 6.3 重复 claim 文本（{len(stats['dup_claim'])} 条）")
        lines.append("")
        for label, reason in stats["dup_claim"][:20]:
            lines.append(f"- `{label}`：{reason}")
        lines.append("")
    if stats["unmatched"]:
        lines.append(f"### 6.4 未匹配域配额（{stats['unmatched_total']} 条）")
        lines.append("")
        for value in sorted(stats["unmatched"], key=lambda k: (-stats["unmatched"][k], k)):
            lines.append(f"- 域标签 `{value}`：{stats['unmatched'][value]} 条（未纳入任何域配额）")
        lines.append("")
    if weights is not None and stats["no_state"]:
        lines.append(f"### 6.5 无/无法识别系统五态（{len(stats['no_state'])} 条，未纳入分层）")
        lines.append("")
        for item_id, value in stats["no_state"][:20]:
            lines.append(f"- `{item_id}`：state 值 {value!r} 无法归一为 S/P/N/C/L")
        if len(stats["no_state"]) > 20:
            lines.append(f"- ……其余 {len(stats['no_state']) - 20} 条省略")
        lines.append("")
    if stats["anchor_overlap"]:
        lines.append(f"### 6.6 与锚题重叠（{len(stats['anchor_overlap'])} 条，已从主样本排除）")
        lines.append("")
        for label in stats["anchor_overlap"][:20]:
            lines.append(f"- `{label}`")
        lines.append("")
    has_warning = any(
        (stats["invalid"], stats["dup_id"], stats["dup_claim"], stats["unmatched"],
         stats["anchor_overlap"], stats["no_state"], stats["fallback"]["count"]))
    if not has_warning:
        lines.append("- 无跳过/告警：池内条目全部可用、无重复、无未匹配域。")
        lines.append("")
    lines.append("")

    # ---- 7 产出与复现 ----
    lines.append("## 7. 产出与指纹")
    lines.append("")
    lines.append(f"- 主样本：`{out_info['path']}`（{len(final_recs)} 条，sha256 `{out_info['sha256']}`）")
    if anchor_info is not None:
        lines.append(f"- 锚题：`{anchor_info['out_path']}`（{len(anchor_info['public'])} 条，"
                     f"sha256 `{anchor_info['out_sha256']}`）")
    if a.id_map:
        lines.append(f"- 追溯映射：`{a.id_map}`（含系统五态，**封存，不得随标注表分发**）")
    else:
        lines.append("- 追溯映射：未提供；建议补 `--id-map` 封存 out_id → 原始 id/层别，供仲裁追溯。")
    lines.append(f"- 输入池 SHA256：`{ctx['pool_hash']}`")
    lines.append("- 复现命令（同 seed + 同输入 ⇒ 逐字节一致）：")
    lines.append("")
    lines.append("```powershell")
    lines.append(ctx["cmdline"])
    lines.append("```")
    lines.append("")
    lines.append("> 验收口径（§3.7）：各层实际数 ≥95% 目标或已在 §3 记录缺口；"
                 "单会话 ≤ 上限；三域 ≥100/域；S+P+C+L ≥ 目标合计 95%。")
    lines.append("")
    return "\n".join(lines)


# ============================ 主流程 ============================

def _build_session_stats(final_recs: list[dict], cap: int) -> dict:
    """统计会话分布（用于日志与聚类设计效应核对）。"""
    counts: dict[str, int] = {}
    no_sid = 0
    for rec in final_recs:
        item = rec["item"]
        if not item["session_id"]:
            no_sid += 1
        sid = item["_sid"]
        counts[sid] = counts.get(sid, 0) + 1
    values = list(counts.values())
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return {
        "unique": len(counts),
        "max": max(values) if values else 0,
        "over": sum(1 for value in values if value > cap),
        "no_sid": no_sid,
        "top": top,
    }


def _render_cmdline() -> str:
    """把真实 argv 渲染成可复读的命令行（确定性：同一命令两次运行完全一致）。

    含空白/逗号/冒号的参数加双引号，保证 PowerShell 里可直接粘贴复跑
    （避免 `--domains 120,120,120`、`--state-quota S:50,P:20` 被 shell 拆成数组）。
    """
    def quote(arg: str) -> str:
        return f'"{arg}"' if any(ch in arg for ch in " ,:\t") else arg

    parts = ["python", "-X", "utf8"] + [str(arg) for arg in sys.argv[1:]]
    return " ".join(quote(part) for part in parts)


def _run(a: argparse.Namespace) -> int:
    # ---------- 参数校验 ----------
    if a.n <= 0:
        raise SampleError(f"--n 必须为正整数：{a.n}")
    if a.per_session_cap < 1:
        raise SampleError(f"--per-session-cap 必须 ≥ 1：{a.per_session_cap}")
    if a.renumber is not None and not a.renumber.strip():
        raise SampleError("--renumber 前缀不能为空（示例：--renumber P 生成 P001…）")
    specs = _build_domain_specs(a)
    weights = _parse_state_quota(a.state_quota)

    # ---------- 路径与保护 ----------
    if _same_path(a.out, a.pool):
        raise SampleError("--out 不能与 --pool 相同（会覆盖抽样池）")
    log_path = a.log or _default_log_path(a.out)
    for flag, path in (("--log", log_path), ("--id-map", a.id_map)):
        if path and (_same_path(path, a.pool) or _same_path(path, a.out)):
            raise SampleError(f"{flag} 不能与 --pool / --out 相同（会互相覆盖）：{path}")
    anchors_path = None
    if a.anchors:
        anchors_path = a.anchors_out or _default_anchors_path(a.out)
        if _same_path(anchors_path, a.pool) or _same_path(anchors_path, a.out):
            raise SampleError("--anchors-out 不能与 --pool / --out 相同")
    elif a.anchors_out:
        raise SampleError("提供了 --anchors-out 但没有 --anchors（锚题文件）")

    # ---------- 读池 ----------
    raw_pool = load_item_list(a.pool, "输入池")
    pool_hash = sha256_file(a.pool)

    # ---------- 读锚题（先读，主样本需按 id/claim 排除锚题重复）----------
    anchor_info = None
    anchor_ids: set[str] = set()
    anchor_claims: set[str] = set()
    if anchors_path is not None:
        raw_anchors = load_item_list(a.anchors, "锚题")
        anchor_invalid: list[tuple[str, str]] = []
        anchors_clean = _validate_items(raw_anchors, anchor_invalid)
        anchors_clean, anchor_dup_id, anchor_dup_claim = _dedupe_items(anchors_clean)
        derive_rng(a.seed, "anchors").shuffle(anchors_clean)
        anchor_public = []
        for item in anchors_clean:
            anchor_ids.add(item["id"])
            anchor_claims.add(item["claim_key"])
            public = {
                "id": item["id"],
                "claim": item["claim"],
                "evidence": item["evidence"],
            }
            for key in ("domain", "source", "session_id"):
                raw_value = item.get("domain_raw") if key == "domain" else item.get(key)
                value = str(raw_value).strip() if raw_value is not None else ""
                if value:
                    public[key] = value
            anchor_public.append(public)
        _dump_json(anchors_path, {
            "description": "锚题（跨轮校准用；单独输出、不占主样本 n；系统判定/人工答案已剥离）",
            "seed": a.seed,
            "n": len(anchor_public),
            "items": anchor_public,
        })
        anchor_info = {
            "path": a.anchors,
            "raw": len(raw_anchors),
            "sha256": sha256_file(a.anchors),
            "invalid": anchor_invalid,
            "dup_id": anchor_dup_id,
            "dup_claim": anchor_dup_claim,
            "public": anchor_public,
            "out_path": anchors_path,
            "out_sha256": sha256_file(anchors_path),
            "overlap_count": 0,  # 稍后在主样本清洗中回填
        }

    # ---------- 主池清洗 ----------
    stats: dict = {
        "raw": len(raw_pool),
        "invalid": [],
        "dup_id": [],
        "dup_claim": [],
        "unmatched": {},
        "unmatched_total": 0,
        "unmatched_text": "",
        "no_state": [],
        "anchor_overlap": [],
        "fallback": {"count": 0, "values": {}, "text": ""},
        "eligible": 0,
    }

    pool_clean = _validate_items(raw_pool, stats["invalid"])
    pool_clean, stats["dup_id"], stats["dup_claim"] = _dedupe_items(pool_clean)

    # 域分配（单个域配额时，域缺失/未匹配条目归入该唯一域，日志显式记录）
    token_map = {spec["token"]: spec for spec in specs}
    single_spec = specs[0] if len(specs) == 1 else None
    assigned: list[dict] = []
    for item in pool_clean:
        spec = token_map.get(item["domain_token"]) if item["domain_token"] else None
        if spec is None and single_spec is not None:
            spec = single_spec
            stats["fallback"]["count"] += 1
            label = str(item["domain_raw"]).strip() if item["domain_raw"] is not None else "<缺失>"
            stats["fallback"]["values"][label] = stats["fallback"]["values"].get(label, 0) + 1
        if spec is None:
            label = str(item["domain_raw"]).strip() if item["domain_raw"] is not None else "<缺失>"
            stats["unmatched"][label] = stats["unmatched"].get(label, 0) + 1
            stats["unmatched_total"] += 1
            continue
        item["domain"] = spec["key"]
        assigned.append(item)

    if stats["unmatched"]:
        values = "、".join(
            f"{label}={count}" for label, count in
            sorted(stats["unmatched"].items(), key=lambda kv: (-kv[1], kv[0])))
        stats["unmatched_text"] = f"（{values}）"
    if stats["fallback"]["values"]:
        values = "、".join(
            f"{label}={count}" for label, count in
            sorted(stats["fallback"]["values"].items(), key=lambda kv: (-kv[1], kv[0])))
        stats["fallback"]["text"] = f"（{values}）"

    # 锚题重叠排除（id 或 claim 文本与锚题相同 → 主样本不用，锚题单列）
    if anchor_info is not None:
        kept: list[dict] = []
        for item in assigned:
            if item["id"] in anchor_ids or item["claim_key"] in anchor_claims:
                stats["anchor_overlap"].append(item["id"])
            else:
                kept.append(item)
        assigned = kept
        anchor_info["overlap_count"] = len(stats["anchor_overlap"])

    # 五态分层前提：启用五态配额时，无/非法五态的条目不纳入分层（日志记录）
    if weights is not None:
        eligible = []
        for item in assigned:
            if item["state"] is None:
                stats["no_state"].append((item["id"], item["state_raw"]))
            else:
                eligible.append(item)
    else:
        eligible = assigned
    stats["eligible"] = len(eligible)

    # ---------- 分域抽样 ----------
    cap = a.per_session_cap
    session_counts: dict[str, int] = {}

    def pick(ordered: list[dict], limit: int) -> list[dict]:
        """跨会话轮转取条目，遵守全局「同一会话 ≤ cap」上限。

        2026-11-10 修正：原实现“按打乱顺序贪心”，长会话会先被填满（实测 10 个会话 ×8=80），
        其余会话闲置、目标凑不满（D2/D3 各缺口 40 条）。改为**每轮从各会话各取 1 条**，
        把配额摊到尽量多的会话（也降低聚类设计效应）；同 seed 仍逐字节可复现。
        """
        picked: list[dict] = []
        if limit <= 0 or not ordered:
            return picked
        # 按会话分桶（桶内保持 ordered 的确定性顺序，作为并列时的 tie-break）
        buckets: dict[str, list[dict]] = {}
        for it in ordered:
            if session_counts.get(it["_sid"], 0) >= cap:
                continue
            buckets.setdefault(it["_sid"], []).append(it)
        if not buckets:
            return picked
        # 会话轮转顺序：先小会话后大会话（确定性）；每轮各取 1 条
        sid_order = sorted(buckets, key=lambda s: (len(buckets[s]), s))
        ptr = {s: 0 for s in buckets}
        while len(picked) < limit:
            progressed = False
            for sid in sid_order:
                if len(picked) >= limit:
                    break
                if session_counts.get(sid, 0) >= cap:
                    continue
                arr = buckets[sid]
                p = ptr[sid]
                if p >= len(arr):
                    continue
                ptr[sid] = p + 1
                session_counts[sid] = session_counts.get(sid, 0) + 1
                picked.append(arr[p])
                progressed = True
            if not progressed:
                break
        return picked

    by_domain: dict[str, list[dict]] = {}
    for item in eligible:
        by_domain.setdefault(item["domain"], []).append(item)

    state_targets = _build_state_targets(specs, weights) if weights is not None else None
    domain_results: list[dict] = []
    all_recs: list[dict] = []
    for spec in specs:
        candidates = by_domain.get(spec["key"], [])
        result = _sample_domain(
            spec=spec,
            candidates=candidates,
            targets=state_targets.get(spec["key"]) if state_targets is not None else None,
            seed=a.seed,
            cap=cap,
            session_counts=session_counts,
            pick=pick,
        )
        domain_results.append(result)
        all_recs.extend(result["selected"])

    # ---------- 输出顺序：seed 打乱 ----------
    final_recs = list(all_recs)
    derive_rng(a.seed, "final").shuffle(final_recs)

    # ---------- 构造盲标输出 + 追溯映射 ----------
    prefix = a.renumber.strip() if a.renumber is not None else None
    width = max(3, len(str(a.n))) if prefix else 0
    out_items: list[dict] = []
    id_map_items: dict[str, dict] = {}
    for index, rec in enumerate(final_recs, start=1):
        item = rec["item"]
        public = _public_item(item, rec["domain"])
        out_id = f"{prefix}{index:0{width}d}" if prefix else item["id"]
        public["id"] = out_id
        out_items.append(public)
        id_map_items[out_id] = {
            "pool_id": item["id"],
            "domain": rec["domain"],
            "state": item["state"],
            "source": item.get("source") or None,
            "session_id": item.get("session_id") or None,
            "stratum": rec["stratum"],
            "selection": rec["mode"],
        }

    out_payload = {
        "description": "分层抽样结果（仅 id/claim/evidence/domain/source/session_id；"
                       "系统五态与人工答案已剥离，供盲标导出使用）",
        "seed": a.seed,
        "n": len(out_items),
        "input_sha256": pool_hash,
        "items": out_items,
    }
    _dump_json(a.out, out_payload)
    out_sha256 = sha256_file(a.out)

    if a.id_map:
        _dump_json(a.id_map, {
            "description": "抽样追溯映射：out_id → 原始 id/域/系统五态/会话/层别；"
                           "含盲标信息，封存，不得随标注表分发",
            "seed": a.seed,
            "input": {"path": a.pool, "sha256": pool_hash, "n_raw": stats["raw"]},
            "n_requested": a.n,
            "n_sampled": len(out_items),
            "items": id_map_items,
            "anchors": (None if anchor_info is None else {
                "path": anchor_info["path"],
                "sha256": anchor_info["sha256"],
                "n": len(anchor_info["public"]),
            }),
        })

    # ---------- 日志 ----------
    session = _build_session_stats(final_recs, cap)
    out_info = {"path": a.out, "sha256": out_sha256}
    ctx = {
        "args": a,
        "specs": specs,
        "weights": weights,
        "stats": stats,
        "domain_results": domain_results,
        "out_info": out_info,
        "anchor_info": anchor_info,
        "final_recs": final_recs,
        "session": session,
        "pool_hash": pool_hash,
        "cmdline": _render_cmdline(),
    }
    _write_text(log_path, _render_log(ctx))

    # ---------- 控制台摘要 ----------
    gap = a.n - len(out_items)
    domain_counts = "、".join(
        f"{result['spec']['key']}={result['spec']['quota'] - result['gap']}"
        for result in domain_results)
    print(f"  ✅ 输入池：{stats['raw']} 条（可用 {stats['eligible']}；"
          f"非法跳过 {len(stats['invalid'])}；重复 id {len(stats['dup_id'])}、"
          f"重复 claim {len(stats['dup_claim'])}）")
    print(f"  ✅ 分层抽样：{len(out_items)}/{a.n} 条（缺口 {gap}）；域实抽：{domain_counts}；"
          f"会话 {session['unique']} 个，单会话最多 {session['max']} 条（上限 {cap}）")
    print(f"  ✅ 输出：{a.out}（sha256 {out_sha256[:12]}…）")
    print(f"  ✅ 日志：{log_path}")
    if anchor_info is not None:
        print(f"  ✅ 锚题：{anchor_info['out_path']}（{len(anchor_info['public'])} 条，不占 n；"
              f"与主样本重叠 {anchor_info['overlap_count']} 条已排除）")
    if a.id_map:
        print(f"  ✅ 追溯映射：{a.id_map}（含系统五态，封存，勿随标注表分发）")

    # 缺口不视为失败：§3.7 要求「取全部并记录缺口」，验收允许「已记录缺口」
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sample.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="分层抽样：域 × 系统五态，带会话上限、缺口回填、锚题单列与确定性复现。",
        epilog=(
            "示例：\n"
            "  正式 360：python -X utf8 annotate/sample.py --pool pool_all.json --out items_360.json \\\n"
            "      --n 360 --domains 120,120,120 --state-quota \"S:50,P:20,N:5,C:30,L:15\" \\\n"
            "      --seed 20261108 --per-session-cap 6 --log 抽样日志.md --id-map id_map.json\n"
            "  试点 30 ：python -X utf8 annotate/sample.py --pool pool_D1.json --out items_pilot30.json \\\n"
            "      --n 30 --domains 30 --state-quota \"S:6,P:5,N:2,C:12,L:5\" --seed 20261108\n"
            "  锚题   ：追加 --anchors bench/data/blind40.json（单独输出、不占 n）\n"
            "  只按域 ：--state-quota none\n"),
    )
    parser.add_argument("--pool", required=True, help="候选池 JSON（{\"items\":[...]} 或裸数组）")
    parser.add_argument("--out", required=True, help="抽中条目输出 JSON（已剥离系统判定/人工标签）")
    parser.add_argument("--n", type=int, required=True, help="目标总数（正式 360 / 试点 30）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--domains", metavar='"120,120,120"',
                       help="按位置给各域条数（按顺序命名 D1/D2/D3…；也接受 D1:120 形式）")
    group.add_argument("--per-domain", type=int, metavar="N",
                       help="每域相同的条数（要求 --n 能整除）")
    group.add_argument("--quota", metavar='"D1:120,D2:120,D3:120"', help="显式域配额")
    parser.add_argument("--state-quota", default=DEFAULT_STATE_QUOTA,
                        metavar='"S:50,P:20,N:5,C:30,L:15"',
                        help=f"域内五态配额/配比（默认正式方案 {DEFAULT_STATE_QUOTA}；"
                             f"试点用 S:6,P:5,N:2,C:12,L:5；none=关闭五态分层）")
    parser.add_argument("--seed", type=int, default=20261108, help="随机种子（默认 20261108，冻结口径）")
    parser.add_argument("--per-session-cap", type=int, default=6,
                        help="同一 session_id 最多抽几条（默认 6，控制聚类设计效应）")
    parser.add_argument("--log", help="抽样日志 Markdown（默认 <out名>_log.md）")
    parser.add_argument("--anchors", help="锚题 JSON（如 bench/data/blind40.json；单独输出、不占 n）")
    parser.add_argument("--anchors-out", help="锚题输出路径（默认 <out名>_anchors.json）")
    parser.add_argument("--id-map", help="追溯映射 JSON（out_id → 原始 id/层别/五态；封存勿分发）")
    parser.add_argument("--renumber", metavar="PREFIX",
                        help="把输出 id 重编为 PREFIX001…（如 P；默认保留池内原 id）")
    return parser


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - 非 Windows 控制台或已被重定向
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except SampleError as exc:
        print(f"❌ 抽样失败：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"❌ 文件读写失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
