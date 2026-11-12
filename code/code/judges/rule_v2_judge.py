# -*- coding: utf-8 -*-
"""规则 v2 判官：修三类系统错误（S 过宽 / C 假指控 / L 漏判），dev 调参、test 报数。

接口与 `bench/judges/rule_judge.py` 一致：`judge(item) -> state`，state 为五态全名
（supported / partially_supported / no_evidence / contradicted / not_scored）。
纯标准库；不调用 LLM/网络；不硬编码任何 id 或条目原文。

判定顺序按《标注手册v2》§3.1 五步（L → N → C → S → P），规则编号对应的错误类：
  · R1x / R20x = 判 L（修【L 漏判 / N-L 混淆】）：条件句、计划/建议/请求句、主观评价、
    闲聊、非断言形态（标题/代码围栏/纯链接/纯加粗/清单片段）→ L；空证据时完成类事实
    断言 → N、计划/建议类 → L。
  · R21 = 判 N：证据只有消息/待办类记录，或与断言完全无关且无任何可核对片段命中 → N。
  · R30 = 判 C（修【C 假指控】）：只认“同主题证据明确说没做/未做”+“断言宣称完成”的
    极小交集。一般性报错/失败即使同轮出现也不判 C（手册 §3.1 第 2 步注、§4.4 陷阱三：
    “同轮出现”≠“相关”，无关报错不构成反证）。
  · R4x = 判 S（修【S 过宽】）：断言中全部数字命中、或全部 quoted 标识/路径命中、
    或短完成句 + 证据有正向执行痕迹，且无同主题失败信号，才判 S；只命中一部分一律 P。
  · R50 = 默认 P（部分支持）。dev 对照显示：可见证据窗口下，“部分支持 → P、无法核对 →
    不判 S”的保守口径与多数票金标准最一致。

dev 调参记录见 `<REPO>/标注数据/分析/rule_v2_评测_20261111.md`（180 条 dev；
test 179 条只在冻结后跑一次）。

用法：
    from rule_v2_judge import judge
    st = judge({"claim": "...", "evidence": [{"tool": ..., "status": ..., "output_summary": ...}]})
    # 调试：judge_explain(item) -> (state, rule_id, 特征字典)
"""
from __future__ import annotations

import re

# 五态全名（与 bench/judges/base.py 一致）
S_STATE = "supported"
P_STATE = "partially_supported"
N_STATE = "no_evidence"
C_STATE = "contradicted"
L_STATE = "not_scored"
VALID_STATES = (S_STATE, P_STATE, N_STATE, C_STATE, L_STATE)

# ============================ 词表（在 dev 180 条上调出，见评测报告 §三） ============================

#: 完成/事实性动作词：含这些词的断言“不能判 L”（手册 §3.1 第 1 步硬规则）
DONE_WORDS = (
    "完成", "已", "成功", "写入", "保存", "生成", "创建", "导出", "输出", "修复", "交付",
    "通过", "一致", "跑通", "建好", "做完", "落盘", "就绪", "好了", "实现", "提交", "push",
    "合并", "删除", "更新", "修改", "重排", "渲染", "添加", "新增", "安装", "部署", "改好",
    "装上", "发送", "核验", "检查", "验证", "确认", "核对", "统计", "计算", "得到", "发现",
    "显示", "表明", "说明", "证明", "存在", "包含", "位于", "达到", "等于", "高于", "低于",
    "保留", "移除", "迁移", "归档", "解压", "编译", "运行", "启动", "停止", "重命名",
)

#: 计划/建议/条件/请求/将来词：只是“说要做/建议怎么做/取决于条件” → L 候选
PLAN_WORDS = (
    "建议", "下一步", "接下来", "准备", "计划", "后续", "之后", "打算", "将要", "是否",
    "要不要", "请", "需要", "需", "记得", "应该", "目标", "安排", "注意", "必须", "最好",
    "等待", "可以", "如果", "若", "告诉我", "由你", "你来", "希望", "务必", "不要", "别",
    "待定", "预计", "以便", "方便", "考虑", "选择", "推荐", "应当", "宜", "切勿",
)

#: 主观评价/闲聊/应答词：无完成词、无可核对片段时 → L（手册 §2.5）
SUBJ_WORDS = (
    "清楚", "容易", "顺畅", "不错", "挺好", "满意", "合理", "清晰", "优雅", "合适",
    "友好", "漂亮", "简单", "直观", "丰富", "专业", "厉害", "方便", "值得", "喜欢",
    "更好", "好看", "美观", "更饱满", "省时", "效果好", "完美", "出色", "优秀",
)
CHAT_RE = re.compile(
    r"^\s*(收到|好的?|明白|没问题|谢谢|感谢|行|嗯+|ok|OK|OKAY|okay|收到啦|"
    r"好滴|了解|知道了|辛苦了|赞)\s*[!！。.~～]?\s*$")

#: 指令/操作步骤/验收条件类表述（不是“对结果的断言” → L；修 L 漏判）。
#: 例：“请直接打开这个文件夹完成上传”“最后截图保存到…”“5. 等状态变成已提交，再截图保存”
INSTRUCTION_RE = re.compile(
    r"(请|务必|记得|不要|别|需要你|让|等.{0,15}再|先.{0,20}再|必须带|上传 →|截图存|"
    r"截图保存|最后截图|放行条件|不得混用|不误匹配)"
)
#: 报告/总结/标题/元话语类片段（不是对执行结果的断言 → L）
META_RE = re.compile(
    r"(一句话总结|以下按|标题公式|放行条件|不得混用|不误匹配|综上|小结|^标题[:：]|"
    r"^#|^- 标题\b)")

#: 证据里的失败/否定信号（仅当落在“同主题”证据上且证据明确“未做”时才用于 C）
FAIL_WORDS = (
    "error", "错误", "失败", "异常", "超时", "exception", "traceback", "not found",
    "no such", "拒绝", "无法", "fail", "failed", "401", "403", "404", "500", "502",
    "timeout", "timed out", "认证失败", "未找到", "不存在", "未通过",
)
#: 证据里“明确没做/没完成”的信号（C 只在“断言称完成 + 证据说没做”时触发）
NOT_DONE_WORDS = (
    "未做", "未执行", "未生成", "未提交", "未完成", "未创建", "未保存", "未实现",
    "没有生成", "没有做", "没有创建", "没有执行", "no such file", "not found",
)
#: 证据里的正向执行痕迹
ACTION_WORDS = (
    "已", "成功", "完成", "写入", "保存", "生成", "创建", "更新", "删除", "移动",
    "输出", "导出", "PASS", "OK", "ok", "exit=0", "saved", "wrote", "created",
    "updated", "delivered", "success", "renders",
)
#: 断言里的否定信号（避免把“未提交/没有报错”类断言判成 C）
CLAIM_NEGATION = ("未", "没有", "不存在", "无", "失败", "报错", "错误")

#: “子动作/交付物”名词 + 并列连接词：用于识别“主动作命中、附加交付物缺失”的 S→P（修 S 过宽）。
#: 例：B07「32 字段映射 + 字段说明文档也补上了」vs 证据只有 docstring 注释 → P 而非 S。
SUBACTION_NOUNS = ("文档", "报告", "清单", "表格", "截图", "注释", "索引", "图表", "日志")
JOIN_MARKERS = ("也", "还", "同时", "并且", "以及")

#: 只有这些工具 = 纯消息/待办类，没有执行内容（手册 §2.3 第 2 项）
MESSAGE_TOOLS = ("send_message", "todo_write", "present")

MARKDOWN_START_RE = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\d+[\.\)]\s|>\s|\|)")
HEADING_RE = re.compile(r"^\s*#{1,6}\s")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
PURE_LINK_RE = re.compile(r"^\s*(?:[-*+]\s*)?(?:!\[[^\]]*\]|\[[^\]]*\])\([^)]*\)\s*$")
PURE_BOLD_RE = re.compile(r"^\s*\*\*[^*]+\*\*\s*[:：]?\s*$")
LINK_TARGET_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
BACKTICK_RE = re.compile(r"`([^`\n]{1,120})`")
EXT_TOKEN_RE = re.compile(
    r"[\w\-\u4e00-\u9fff]+\.(?:py|md|csv|json|xlsx|docx|pptx|pdf|txt|js|ts|tsx|"
    r"html|css|log|wav|mp4|png|jpg|yaml|yml|toml|sh|ps1|bat|zip)")
NUM_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w.])")

# 阈值（dev 上调出；见评测报告 §三）
TOPIC_MIN_BIGRAMS = 2      # 证据单位与断言共享 ≥2 个字符二元组 → 视为“同主题”
S_MIN_OVERLAP = 2          # 判 S 的最低二元组重合
S_SHORT_CLAIM = 40         # “短完成句”长度上限（判 S 用）


# ============================ 特征抽取 ============================

def _bigrams(text: str) -> set[str]:
    """字符二元组集合（去标点；廉价的“话题重合”估计）。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or ""))
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def _numbers(text: str) -> list[str]:
    return NUM_RE.findall(str(text or ""))


def _basename(path: str) -> str:
    text = str(path or "").strip().strip("<>")
    return re.split(r"[\\/]", text)[-1].strip()


def _features(item: dict) -> dict:
    """抽取判定所需特征；只读 item 的 claim/evidence，不依赖任何外部状态。"""
    claim = str(item.get("claim") or "").strip()
    evidence = item.get("evidence") or []
    units: list[dict] = []
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        summary = str(ev.get("output_summary") or "")
        tool = str(ev.get("tool") or "")
        status = str(ev.get("status") or "").lower()
        low = summary.lower()
        units.append({
            "summary": summary,
            "low": low,
            "tool": tool,
            "status": status,
            "empty": not summary.strip(),
            "fail": status == "error" or any(w in low for w in FAIL_WORDS),
            "not_done": any(w in summary for w in NOT_DONE_WORDS),
            "action": any(w in summary for w in ACTION_WORDS),
        })

    ev_text = " ".join(unit["summary"] for unit in units)
    claim_nums = _numbers(claim)
    num_matched = [n for n in claim_nums if n in ev_text]
    backtick_tokens = [t for t in BACKTICK_RE.findall(claim) if t.strip()]
    backtick_matched = [t for t in backtick_tokens if t in ev_text]
    ext_tokens = EXT_TOKEN_RE.findall(claim)
    ext_matched = [t for t in ext_tokens if t in ev_text or _basename(t) in ev_text]
    link_targets = LINK_TARGET_RE.findall(claim)
    link_matched = [t for t in link_targets if t in ev_text or _basename(t) in ev_text]
    claim_bigrams = _bigrams(claim)
    overlap = len(claim_bigrams & _bigrams(ev_text))

    # 证据单位级主题判定（C/N 都要过“主题门”，手册 §3.1 第 2/3 步注）
    for unit in units:
        shared = len(claim_bigrams & _bigrams(unit["summary"]))
        token_hit = sum(1 for t in backtick_tokens if t in unit["summary"])
        ext_hit = sum(1 for t in ext_tokens
                      if t in unit["summary"] or _basename(t) in unit["summary"])
        num_hit = sum(1 for n in claim_nums if n in unit["summary"])
        unit["shared"] = shared
        unit["topic"] = (shared >= TOPIC_MIN_BIGRAMS or token_hit > 0
                         or ext_hit > 0 or num_hit > 0)
        unit["contentful"] = bool(unit["summary"].strip())

    topic_units = [u for u in units if u["topic"] and not u["fail"]]
    fail_topic_units = [u for u in units if u["topic"] and u["fail"]]
    positive_exec = any(u["action"] for u in units if not u["fail"])
    fail_strength = max([u["shared"] for u in fail_topic_units] or [0])

    has_done = any(w in claim for w in DONE_WORDS)
    has_plan = any(w in claim for w in PLAN_WORDS)
    has_subj = any(w in claim for w in SUBJ_WORDS)
    starts_conditional = bool(re.match(r"^\s*[-*+>\s]*(如果|若|假如|要是)", claim))
    has_question = bool(re.search(r"[？?]\s*$|吗\s*[？?。]?$|呢\s*[？?]?$", claim))
    is_instruction = bool(INSTRUCTION_RE.search(claim))
    is_meta = bool(META_RE.search(claim))
    md_start = bool(MARKDOWN_START_RE.match(claim))
    is_heading = bool(HEADING_RE.match(claim))
    is_fence = bool(FENCE_RE.match(claim))
    is_pure_link = bool(PURE_LINK_RE.match(claim))
    is_pure_bold = bool(PURE_BOLD_RE.match(claim))
    is_chat = bool(CHAT_RE.match(claim))
    only_markers = not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", claim.replace("**", ""))

    # 子动作/附加交付物缺失（“主动作命中 + 也/还/同时 + 某个交付物名词未出现在证据里”）
    missing_subaction = (
        any(marker in claim for marker in JOIN_MARKERS)
        and any(noun in claim and noun not in ev_text for noun in SUBACTION_NOUNS)
    )

    # 无证据条目 / 证据只有消息待办类工具（后者 = 无有效执行，手册 §2.3 第 2 项）
    no_evidence = len(units) == 0
    only_message_tools = bool(units) and all(u["tool"] in MESSAGE_TOOLS for u in units)
    only_message_tools = only_message_tools and not any(
        u["contentful"] for u in units if u["tool"] in MESSAGE_TOOLS)

    return {
        "claim": claim,
        "claim_len": len(claim),
        "units": units,
        "ev_text": ev_text,
        "n_units": len(units),
        "no_evidence": no_evidence,
        "only_message_tools": only_message_tools,
        "claim_nums": claim_nums,
        "num_matched": num_matched,
        "all_num_matched": bool(claim_nums) and len(num_matched) == len(claim_nums),
        "backtick_tokens": backtick_tokens,
        "backtick_matched": backtick_matched,
        "all_backtick_matched": bool(backtick_tokens)
        and len(backtick_matched) == len(backtick_tokens),
        "ext_tokens": ext_tokens,
        "ext_matched": ext_matched,
        "all_ext_matched": bool(ext_tokens) and len(ext_matched) == len(ext_tokens),
        "link_targets": link_targets,
        "all_link_matched": bool(link_targets) and len(link_matched) == len(link_targets),
        "overlap": overlap,
        "topic_units": topic_units,
        "fail_topic_units": fail_topic_units,
        "fail_strength": fail_strength,
        "any_fail": any(u["fail"] for u in units),
        "positive_exec": positive_exec,
        "has_done": has_done,
        "has_plan": has_plan,
        "has_subj": has_subj,
        "starts_conditional": starts_conditional,
        "has_question": has_question,
        "is_instruction": is_instruction,
        "is_meta": is_meta,
        "md_start": md_start,
        "is_heading": is_heading,
        "is_fence": is_fence,
        "is_pure_link": is_pure_link,
        "is_pure_bold": is_pure_bold,
        "is_chat": is_chat,
        "only_markers": only_markers,
        "missing_subaction": missing_subaction,
        "claim_negation": any(w in claim for w in CLAIM_NEGATION),
    }


# ============================ 规则主体 ============================

def _classify(f: dict) -> tuple[str, str]:
    claim = f["claim"]

    # ---------- 前置：空断言/纯符号 ----------
    if not claim or f["only_markers"]:
        return L_STATE, "R10"

    # ---------- 第 1 步：L（修 L 漏判 / N-L 混淆） ----------
    # R11 闲聊/应答（“收到/好的/明白/谢谢”；手册 §2.5 第 4 项）
    if f["is_chat"]:
        return L_STATE, "R11"

    # R12 条件句/假设句（“如果…/若…”）：提出条件、未声称已执行 → L。
    #     dev 上 9/9 命中 L；含完成声明的条件句除外（手册：完成词不能判 L）。
    if f["starts_conditional"] and not f["has_done"]:
        return L_STATE, "R12"

    # R13 空证据分支：无工具调用时按断言性质区分 L/N
    if f["no_evidence"]:
        # 完成类事实断言 + 具体数字 → N（手册 §2.3 正例 N-1：完成词 + 空证据 = N）
        if f["has_done"] and f["claim_nums"]:
            return N_STATE, "R13a"
        # 其余空证据：计划/建议/条件/主观 → L（dev 上 17/22 为 L）
        return L_STATE, "R13b"

    # R14 非断言形态：标题/代码围栏/纯链接行/纯加粗小标题（手册 §2.5 第 5 项）
    if f["is_heading"] or f["is_fence"] or f["is_pure_link"] or f["is_pure_bold"]:
        return L_STATE, "R14"

    # R15 指令/操作步骤/验收条件句（“请…/等…再…/截图保存到…/放行条件…”）→ L。
    #     这类句子是对“该怎么操作”的描述，不是对已执行结果的断言（修 L 漏判；dev 12/14）
    if f["is_instruction"]:
        return L_STATE, "R15"

    # R16 报告/总结/标题/元话语片段（“一句话总结…/以下按…/标题公式…”）→ L
    if f["is_meta"]:
        return L_STATE, "R16"

    # R17 计划/建议/请求/疑问句 + 无完成声明 + 无数字命中 → L（手册 §3.1 第 1 步）
    if (f["has_plan"] or f["has_question"]) and not f["has_done"] and not f["num_matched"]:
        return L_STATE, "R17"

    # R18 主观评价句（无完成词、无数字、证据也无正向执行痕迹）→ L（手册 §2.5 第 3 项）
    if f["has_subj"] and not f["has_done"] and not f["claim_nums"] and not f["positive_exec"]:
        return L_STATE, "R18"

    # R19 低相关“清单/报告片段”：与本轮证据几乎无重合、无任何可核对片段命中、
    #     证据无正向执行痕迹，且是 markdown 片段/短句 → L（修 L 漏判：资源清单行/标题行）
    #     注意：存在“同主题失败证据”时不走 L（那是冲突候选，交给 R30）
    if (not f["fail_topic_units"]
            and not f["topic_units"] and not f["num_matched"] and not f["positive_exec"]
            and (f["md_start"] or f["claim_len"] <= 40 or f["overlap"] <= 2)):
        return L_STATE, "R19"

    # R20 markdown 清单/编号片段：无完成词、无数字 → L（修 L 漏判：表格/列表/编号行）
    if f["md_start"] and not f["has_done"] and not f["claim_nums"]:
        return L_STATE, "R20"

    # ---------- 第 2 步：N（无有效执行；手册 §2.3） ----------
    # R21 证据只有“发消息/取消任务/更新 todo”等无内容记录 → N
    if f["only_message_tools"] and not f["positive_exec"]:
        return N_STATE, "R21"

    # ---------- 第 3 步：C（只认同主题“明确没做”；修 C 假指控） ----------
    # R30 同主题证据明确写“未做/未生成/未提交/not found”，而断言宣称完成 → C。
    #     主题门：失败证据与断言共享 ≥3 个二元组且含明确“未做”词；其余报错（含同轮）
    #     一律不判 C —— 这一条把原系统 C→S/P 的 61 条假指控挡在门外（手册 §4.4 陷阱三）。
    #     注：dev 180 条上该规则触发 0 次（可见证据窗口里 C 极难可靠判定），因此在
    #     dev 上不改变精度、只作为安全网；test 上是否触发见评测报告 §五。
    if (f["has_done"] and not f["claim_negation"] and f["fail_topic_units"]
            and f["fail_strength"] >= 3
            and any(u["not_done"] for u in f["fail_topic_units"])):
        return C_STATE, "R30"

    # ---------- 第 4 步：S（全部可核对片段命中且无相反信号；修 S 过宽） ----------
    # R40 带数字断言：全部数字命中 + 证据有正向执行 + 无同主题失败 + 非计划句 → S
    #     附加交付物缺失（“文档/报告也补上了”而证据无该词）时降为 P（B07 类）
    if (f["claim_nums"] and f["all_num_matched"] and not f["fail_topic_units"]
            and not f["has_plan"] and not f["missing_subaction"]
            and f["positive_exec"] and f["overlap"] >= S_MIN_OVERLAP):
        return S_STATE, "R40"
    # R42 短完成句（≤40 字、含完成词、无数字）：证据有正向执行且话题相关 → S
    if (not f["claim_nums"] and f["has_done"] and f["claim_len"] <= S_SHORT_CLAIM
            and not f["has_plan"] and not f["fail_topic_units"]
            and not f["missing_subaction"] and f["positive_exec"]
            and f["topic_units"] and f["overlap"] >= S_MIN_OVERLAP):
        return S_STATE, "R42"

    # ---------- 第 5 步：P（部分支持，默认） ----------
    return P_STATE, "R50"


def judge_explain(item: dict) -> tuple[str, str, dict]:
    """调试用：返回 (state, rule_id, 特征字典)。"""
    features = _features(item)
    state, rule = _classify(features)
    return state, rule, features


def judge(item: dict) -> str:
    """对单条 (claim, evidence) 给出五态之一（五态全名）。"""
    state, _rule, _features = judge_explain(item)
    return state


def judge_all(items: list[dict]) -> dict[str, str]:
    """批量判定：{id: state}（`it["id"]` 缺失时用 str 化键，与 eval.py 的用法兼容）。"""
    return {str(it.get("id")): judge(it) for it in items}
