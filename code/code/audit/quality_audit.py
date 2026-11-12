# -*- coding: utf-8 -*-
"""
quality_audit.py —— **对话质量审计**：不需要标准答案，也能判断这段回答靠不靠谱

===============================================================================
【为什么重做（用户 2026-10-19 的判断，我认为是对的）】
  原来这个项目的招牌指标是：
      水分 = 表面成功率 − 核验后真实成功率
  两个数**都要求有标准答案（gold）**。而真实用户的对话**根本没有 gold**，
  所以这个指标对绝大多数场景**直接失效** —— 用户形容为「鸡肋」，准确。

  重新想清楚后，可算的东西不在「结果对不对」，而在「过程扎不扎实」：
      它说的每一句话，有多少是有证据支撑的？
  这个**从对话内部就能算**，不需要任何外部答案。

===============================================================================
【方法论：三处借鉴（先搜罗再动手）】
  ① ★ **必须用断言级，不能用字符比例**
     来源：Faithfulness Checker（agent-development-curriculum）
     > "Character-fraction scoring would allow a single short hallucinated
     >  sentence to hide inside a long faithful answer"
     一句很短编造的话，藏在很长的正确回答里，按字数算几乎看不出来。
     → 所以本模块把回答**切成原子断言**，逐条判有没有证据。

  ② **原子断言 = 一个主语 + 一个可验证命题**
     来源：eTracer（Claim-Level Grounding）、Fact in fragments
     切分后再做 **span 匹配**：把断言里的"关键片段"（数字、路径、函数名、表名）
     拿去证据里找，找不到就是**未落地**。

  ③ **六维评分（rubric），而不是一个总分**
     来源：Phoenix/LLM-Check 一类评测框架通行的六维（coherence / relevance /
     specificity / safety / polarity consistency / grounding）
     本项目改造成六维：可追溯 / 严谨 / 一致 / 具体 / 完整 / 诚实
     **每一维都必须给出依据**（哪条断言、缺什么证据），否则这个分数没有说服力。

===============================================================================
【★ 架构约束：全程规则，不调用任何大模型】
  理由同全项目：判别方与被判别方**必须不同源**；否则等于自己判自己，
  且会把对话内容拼进提示词，直接引入提示注入风险。
  代价是规则比模型粗 —— 所以每一维都写明"这是启发式判断"，不假装精确。
===============================================================================
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache as _lru_cache
from datetime import datetime

# ---------------------------------------------------------------- 词表
# ★ 中文的完成断言远不止固定几个词。
#   实证（test_quality_audit 的 case_list_style）：列表里写「已新增文件 A」「已修改文件 B」，
#   固定词表一个都没命中 → 整段只切出 2 条断言、**漏判两句完成声明**。
#   所以补一条**结构规则**：以「已」开头紧跟动作动词的短语，一律视为完成类断言。
CLAIM_DONE_RE = re.compile(
    r"已(?:新增|修改|删除|创建|生成|更新|提交|推送|读|记|入库|完成|执行|解决|处理|"
    r"改好|写好|跑完|跑通|核对|检查|验证|同步|保存|导出|修复|修正|调整|部署|上线|"
    r"通过|就绪|到位|生效|补齐|补回|移除|回滚|回退|还原|撤销|部署|"
    # ★ 2026-10-23（窗口 E，分类法 v2）：盲标暴露「完成声明被 skip」——
    #   B26「已批量重命名并压缩…」/ B31「已安装 pandas…」整句在切分阶段被丢弃，
    #   于是 test_precision 里直接返回 skip（= not_scored）。修法见 DONE_SOFT_RE：
    #   凡「已+动作」的完成声明必须进入判分，词表要覆盖常用动作。
    r"安装|合并|压缩|重命名|清理|迁移|配置|下载|上传|排序|归类|比对|发送|"
    r"统计|汇总|拆分|提取|转换|替换|封装|补充|覆盖|重写|打包|解压|截图|"
    r"标注|录入|校对|审阅|归档|备份|恢复|清洗|格式化|批量|重新|全部|成功|"
    r"把|将|对|给|向|按|用|为|从)")

# ★ 结构兜底（2026-10-23 窗口 E）：以「已」开头的句子默认是完成类断言。
#   理由：not_scored 只允许计划句/建议句/主观评价；**凡有完成词的必须进判分**
#   （分类法 v2 §六 第 3 个 bug）。词表永远补不全，所以再加一条结构规则。
#   排除「已知/已悉/已然」这类不是完成声明的开头。
DONE_SOFT_RE = re.compile(r"^(?:已(?!知|悉|然)|均已|都已经)")

# ★★ 2026-10-23（W4 切分修复）：**完成断言的生成式结构规则** ——
#   修的是「已经保存好了 / 已经合并成 / 都已经装好了」这类整句被丢的漏审。
#
#   旧写法只认「已 + 动词」（CLAIM_DONE_RE），而中文完成体最常见的形态是：
#       「已经 + 动作 + 好了/成/完/到/进」（已经保存好了 / 已经合并成 / 已经群发完了）
#   以及不带「已」的「都 / 也 + 动作 + 好了/到/完」（都查到了 / 也一并算好了）。
#   所以这里不再无脑往词表里堆词，而是拆成**三要素结构**：
#       ① 完成标记前缀：都已经 / 已经 / 均已 / 已 / 都 / 也都 / 也一并
#       ② 中间的动作与宾语（≤10 字，**不得含评价性字眼**）
#       ③ 完成补语：好 / 成 / 完 / 到 / 进 / 掉 / 齐 / 妥（带「已」的允许裸「了」）
#
#   ★ ② 里刻意排除「得/地/很/挺/蛮/够/足/多/太/不/没/未/是/这/那/像/比/更」——
#     因为「已经做得很好了」「已经很清楚了」「比上一版更容易落地」是**主观评价**，
#     不是完成声明。这条排除项是 not_scored 边界的底线：提高召回**不能**靠放松它。
#   ★ 「看到/听见/想到」也排除：「周末看到你们发的照片了」是闲聊，不是完成断言。
_DONE_GAP = (r"(?:(?!(?:得|地|很|挺|蛮|够|足|多|太|不|没|未|是|这|那|像|更|"
             # 「比」只挡**比较/对比评价**（比上一版更容易… / 比较好），
             # 不挡「比对」这类动作（已经比对完了 → 完成声明）。
             r"比(?:上一版|上一批|上次|之前|原来|以前|旧版|前一版|较)|"
             r"看到|听见|想到|觉得|感到|遇到))[^，。；！？\s])")

CLAIM_DONE_STRUCT_RE = re.compile(
    r"(?:都已经|已经|均已|已)(?![知悉然])" + _DONE_GAP + r"{1,10}?"
    + r"(?:好|成|完|到|进|掉|齐|妥|了)")

# 不带「已」的完成体：都查到了 / 也一并算好了 / 也都补进……了 / 也清理干净了。
#   补语必须**紧跟「了」**（都替换掉 ≠ 已完成，是将来动作）；
#   也不收裸「了」（「都不一样了」「也不错了」不是完成声明）。
CLAIM_DONE_ASPECT_RE = re.compile(
    r"(?:都|也都|也一并|也)(?![知悉然])" + _DONE_GAP + r"{1,8}?"
    + r"(?:好|成|完|到|进|掉|齐|妥|干净)了")

# ★ 2026-10-23（W4）：主观评价 / 闲聊的结构标记。
#   分类法 v2 规定 not_scored **只允许**计划句、建议句、主观评价、闲聊。
#   原来这类句子在切分阶段被**静默丢弃**（结果是 skip，且报告里看不到），
#   现在显式标成「主观/闲聊」并保留下来 → 判定为 not_scored（不判分、不进分母），
#   既守住边界，又让"这句为什么不判分"在报告里透明可见。
#   ★ 只认"评价句式"本身，不认孤零零的形容词，避免把事实断言误标成主观。
SUBJECTIVE_RE = re.compile(
    # ① 评价补语：写得很清楚 / 排得挺周到 / 做得不错
    r"[一-龥]{1,2}得(?:很|挺|非常|比较|相当|太|特别|格外)?"
    r"(?:好|不错|清楚|顺畅|漂亮|自然|舒服|合理|细致|到位|周到|顺利|流畅|整齐|有意思)"
    # ② 与旧版本比较：比上一版更容易落地 / 比之前清楚
    r"|比(?:上一版|上一批|上次|之前|原来|以前|旧版|前一版|原来那版)[^，。；]{0,8}?更"
    # ③ 程度副词 + 评价词：挺适合 / 蛮不错 / 相当清楚
    r"|(?:挺|蛮|相当|比较|非常|太)(?:适合|不错|好|满意|舒服|方便|清楚|合理|有意思|漂亮|顺利|周到)")
# 闲聊/寒暄：不可核对，也不该算"完成声明"
CHAT_RE = re.compile(r"(?:辛苦了|谢谢|感谢|哈哈|加油|恭喜|节日快乐|周末愉快|好久不见)")

# 断言类型：这些词一出现，说明它在**对结果下判断**
CLAIM_COMPLETION = ["已完成", "已经完成", "完成", "搞定", "已解决", "解决了", "就绪",
                    "已完成", "已推送", "推送成功", "已提交", "已生成", "已执行",
                    "已读", "已记", "已入库", "成功", "done", "completed",
                    # ★ 英文断言词（2026-10-19 留出集暴露：纯英文对话整句被丢弃）
                    "finished", "passed", "succeeded", "created", "written",
                    "saved", "deployed", "fixed", "updated", "deleted",
                    "pushed", "committed", "verified", "checked", "refactored",
                    # ★ 第四份冻结集 F14 暴露：英文完成类远不止这些
                    "green", "all pass", "all tests pass", "no failures",
                    "are green", "is green", "up and running"]
CLAIM_VERIFICATION = ["已验证", "已核对", "已检查", "校验通过", "校验完成", "确认无误",
                      "没有问题", "无误", "verified", "checked", "tests pass", "all pass",
                      "no errors", "lint clean"]
CLAIM_NEGATIVE = ["报错", "失败", "无法", "做不到", "不支持", "超时", "异常", "错误",
                  # ★ 第四份冻结集 F07 暴露：否定式**结论**也是可审计断言
                  "没有发现", "未发现", "未检测到", "没发现",
                  "error", "failed", "cannot", "unable", "timeout"]
CLAIM_HEDGE = ["应该", "大概", "可能", "也许", "我猜", "估计", "不太确定",
               "probably", "maybe", "I think"]

# ---------------------------------------------------------------- 五态判定用词表
# ★ 分类法 v2（2026-10-23 窗口 E）—— 五态改名 + 三轴 + 冲突优先。
#
#   为什么再改一次：盲标实验暴露两件事
#    ① 旧 `partial` 与 `contradict` 抢同一类情况（"数字部分吻合但有一个不符"），
#       没有优先级规则 → 人标和机判各说各话；
#    ② 旧 `none` 同时承担"没证据"和"证据无关"，`plan`/`skip` 又统计等价。
#   新分类法把三件互相独立的事拆开（可判性 / 证据状态 / 一致性关系），
#   再用**一条不可跳过的判定顺序**消除歧义（见 ground_claims 的 5 步）。
#
#   迁移映射（旧 → 新）：
#     strong → supported ｜ partial → partially_supported ｜ none → no_evidence
#     contradict → contradicted ｜ plan / skip → not_scored
STATE_LABELS = {
    "supported": "有证据支持",
    "partially_supported": "部分支持",
    "no_evidence": "无证据",
    "contradicted": "与证据矛盾",
    "not_scored": "不判分",
}
# 兼容旧引用名（外部模块若 import STRENGTH_LABELS 不至于 ImportError）
STRENGTH_LABELS = STATE_LABELS

# 五态的固定次序 + 展示/聚合优先级（数值越大越"强"，用于一句多片时取最强者）
STATE_ORDER = ["supported", "partially_supported", "no_evidence", "contradicted", "not_scored"]
STATE_PRIORITY = {"supported": 5, "partially_supported": 4,
                  "contradicted": 3, "no_evidence": 2, "not_scored": 1}

# 对外统计口径：这些态算"未获支持"（grounded=False）
UNSUPPORTED_STATES = ("no_evidence", "contradicted")
# 参与判分的四态（not_scored 不进分母）
SCORED_STATES = ("supported", "partially_supported", "no_evidence", "contradicted")
NOT_SCORED_STATE = "not_scored"

# ★ 旧名 ↔ 新名映射（冻结夹具/旧调用点的**比较层**用这一张表，不要改夹具）
LEGACY_TO_STATE = {"strong": "supported", "partial": "partially_supported",
                   "none": "no_evidence", "contradict": "contradicted",
                   "plan": "not_scored", "skip": "not_scored"}
STATE_TO_LEGACY = {"supported": "strong", "partially_supported": "partial",
                   "no_evidence": "none", "contradicted": "contradict",
                   "not_scored": "plan"}


def normalize_state(state: str | None) -> str:
    """把旧状态名归一成分类法 v2 的名字（已是新名则原样返回，未知返回 no_evidence）。"""
    s = str(state or "").strip()
    if s in STATE_LABELS:
        return s
    return LEGACY_TO_STATE.get(s, "no_evidence")


def to_legacy_state(state: str | None) -> str:
    """新名 → 旧名（供**冻结夹具 / 旧版 test_precision** 的比较层做等价映射）。

    ⚠️ 只用于比较层，不要用它往回写判定结果 —— 判定结果一律用新五态。
    """
    s = normalize_state(state)
    return STATE_TO_LEGACY.get(s, s)

# 证据里出现这些 → 该次执行是**失败的**（用于"矛盾/反证"判定）
ERROR_TEXT_RE = re.compile(
    r"(?i)(error|exception|traceback|failed|failure|fatal|abort|"
    r"报错|错误|异常|失败|崩溃|超时|timeout|no such|undefined|not found|"
    r"KeyError|TypeError|ValueError|NameError|RuntimeError)")

# 「没有报错 / 无异常 / 零失败」这类**否认问题存在**的断言
NO_ERROR_CLAIM_RE = re.compile(
    r"(?:没有|没|无|未|零)\s*(?:任何)?\s*"
    r"(?:报错|错误|异常|失败|问题|error|errors|failure|failures)")

# ★ 2026-10-23（W4）：证据里**明确说"这个动作没做"**（未做去重 / 没有合并 / 未清理）。
#   完成词覆盖修好后，这类断言第一次进入判分；若证据已明确"没做"，却按
#   「本轮有成功执行」判 supported，就是**假支持**（最伤信任的方向）。
#   实证：B33「重复行也清理干净了」vs 证据「检测到 27 行重复…未做去重」
#   → 必须 contradicted。只认"否定词 + 具体动作"，不做泛化的否定检测。
_EV_DENIED_ACTION_RE = re.compile(
    r"(?:未|没有|没|尚未|并未|未能|无法)\s*(?:真正)?\s*(?:做|进行|执行|完成)?\s*"
    r"(?:去重|清理|合并|替换|统计|导入|导出|保存|删除|修改|迁移|生成|安装|核对|检查|"
    r"排序|归类|分类|压缩|上传|下载|配置|部署|修复|更新|重命名|脱敏|分组)")

# ★ 2026-10-23（W4 切分修复）：**千分位数字**。
#   `8,600` 原先被 ASCII 逗号切断 —— 数字+单位正则只认 `\d+`，抽出来的片段是
#   「600 字」/「284 行」，与证据里的 8600/1284 对不上 → 整句失去可核对片段。
#   这里只归一**合法千分位**（逗号后正好 3 位数字，且后面不再接数字），
#   所以「第 1—8 页」「3+6」「2026-10-23」都不会被改坏。
_THOUSAND_SEP_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _strip_thousand_seps(t: str) -> str:
    """把 `8,600` / `1,284` / `12,345` 归一成 `8600` 等值；其余文本原样返回。"""
    return _THOUSAND_SEP_RE.sub("", str(t or ""))


# 千分位数字片段：在**原文**上匹配（保留"这是千分位数字"这一信息），
# 匹配到后先去掉逗号再当片段，`_cores` 才能抽出 8600 而不是 600/284。
# 单位表在这里额外收了「字」（「全文 8,600 字」），**不动**普通数字抽取的
# SPAN_PATTERNS —— 把影响面限制在"含千分位的句子"里。
_THOUSAND_UNITS = (r"(?:行|条|个|次|页|篇|天|小时|分钟|周|月|年|倍|分|处|元|岁|人|户|"
                   r"台|件|笔|份|项|家|座|名|位|字|lines?|files?|tests?|rows?|records?|"
                   r"items?|modules?|cases?|seconds?|ms|times?|errors?|pp|%|GB|MB|KB|秒|s|h|min)")
THOUSAND_SPAN_PATTERNS = [
    re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*"
               + _THOUSAND_UNITS + r"(?![0-9A-Za-z])"),
    re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?"
               r"(?![\d,]|\s*" + _THOUSAND_UNITS + r")"),
]

# 断言里值得去证据里核对的"关键片段"
SPAN_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                  # 日期
    # ★ 单位表（含时间/篇幅/金额）
    # ★★ 关键修复（2026-10-19 精确率评测发现）：
    #   原来尾部用的是 `\b`（词边界）—— 对英文有效，对中文**失效**：
    #     「119 个 md」   ｜个后是空格 → \b 成立 → 能匹配 ✅
    #     「416 个文件」  ｜个后是汉字，汉字和"个"都算 \w → 没有边界 → **匹配不到** ❌
    #   这就是「416 个文件」被判成"不是断言"、整句被丢弃的原因。
    #   修法：尾部改成 `(?![0-9A-Za-z])` —— 只排除紧跟英文/数字的情况，不排除中文。
    re.compile(r"(?:\d+(?:\.\d+)?)\s*(?:行|条|个|次|页|篇|天|小时|分钟|周|月|年|倍|分|处|"
               r"元|岁|人|户|台|件|笔|份|项|家|座|名|位|"
               r"lines?|files?|tests?|rows?|records?|items?|modules?|cases?|seconds?|ms|times?|errors?|"
               r"pp|%|GB|MB|KB|秒|s|h|min)(?![0-9A-Za-z])"),         # 数量/时间
    re.compile(r"\d+:\d+"),                                          # 时刻
    re.compile(r"[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}"),                 # git 提交区间
    # ★ 单个 git 短哈希（精确率评测 X05 漏判：99bfd9d 明明在证据里却没被提取）
    re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])"),    # git 短哈希
    re.compile(r"\b[A-Za-z]:\\\\[^\s，。；、\"]+"),          # Windows 路径
    re.compile(r"\b[\w./-]+\.(?:py|md|json|jsonl|csv|toml|txt|xlsx|pdf|sql|"
               r"yaml|yml|ini|log|sh|js|ts|html|png|jpg)(?![0-9A-Za-z])"),  # 文件名
    re.compile(r"`([^`]{2,60})`"),                          # 行内代码
    re.compile(r"\b0x[0-9a-fA-F]+\b"),                      # 退出码/地址
]

# ★ 动作词 → 该动作在证据里应出现的**可核对痕迹**（中英都列，因为工具名常是英文）
#   设计意图：让「说做了」和「真做了」能对上号，而不是只数有没有数字。
ACTION_HINTS = {
    "读取": ("read", "open", "read_file", "读取"),
    "读": ("read", "open", "读取"),
    "记": ("write", "remember", "note", "记录"),
    "入库": ("write", "create", "save", "写入"),
    "新增": ("write", "create", "created", "new file", "新增"),
    "创建": ("write", "create", "created", "新增"),
    "生成": ("write", "create", "generate", "生成"),
    "保存": ("save", "write", "保存"),
    "导出": ("export", "save", "导出"),
    "修改": ("edit", "update", "modify", "替换", "修改"),
    "编辑": ("edit", "update", "修改"),
    "删除": ("rm", "delete", "remove", "删除"),
    "移除": ("rm", "delete", "remove", "移除"),
    # ★ 2026-10-23（W1 内核精修，严重错误 B20）：词表漏了「清理」——
    #   「已清理重复记录并生成校验报告」vs 证据「python(dedup) → duplicates=41；saved cleaned.csv」
    #   旧词表只按「生成」找 write/create，找不到就落进"说做了但没做"的 no_evidence
    #   （严重错误：该判 positive 判成 none）。补齐"清理/去重"在证据里的痕迹词。
    "清理": ("clean", "cleaned", "dedup", "duplicates", "remove", "清洗", "清理"),
    "清洗": ("clean", "cleaned", "dedup", "duplicates", "清洗", "清理"),
    "推送": ("push", "推送"),
    "提交": ("commit", "提交"),
    "校验": ("校验", "check", "lint", "test", "validate", "verify"),
    "检查": ("check", "校验", "inspect", "检查", "select", "query"),
    # ★ 数据场景里"跑查询"本身就是核对动作（实证：'我核对了一遍口径' 是用 SQL 跑的）
    "核对": ("check", "diff", "对比", "核对", "select", "query", "sql"),
    "验证": ("verify", "校验", "test", "check", "验证", "select", "query", "sql"),
    "执行": ("run", "执行", "pwsh", "python", "bash"),
    "运行": ("run", "运行", "pwsh", "python"),
    "部署": ("deploy", "部署"),
    "同步": ("sync", "push", "pull", "同步"),
    "修复": ("fix", "edit", "修复"),
    "更新": ("update", "edit", "更新"),
    "回滚": ("revert", "rollback", "reset", "回滚"),
    "回退": ("revert", "rollback", "reset", "回退"),
    "还原": ("revert", "restore", "reset", "还原"),
    "撤销": ("revert", "undo", "reset", "撤销"),
}

# ★ 计划/建议类句子不是成就声明，不该用本轮执行记录去核对（实证：
#   「初赛后 14 天内只做转化链第 1–3 步」被判 weak，但它本来就是未来计划）。
# ★ 只保留**明确表未来/建议**的标记（2026-10-19 实证）：
#   原表里有「赛后」「之后」「以后」，导致「新增：…赛后触发执行单」被误判成计划句
#   —— 它明明是完成声明，只是名词里带了"赛后"二字。
# ★ 否定词（2026-10-19 留出集暴露）：句中有否定时**不能**判成「完成类」。
#   实证：「未完成，因为缺少依赖包」被判成完成类断言 → 逻辑上是"说没做到被当成做到了"。
#   对审计工具而言，这会把**诚实失败**误判成**虚假成功** —— 最不能犯的错误。
# ★ 修正（2026-10-19 真实数据暴露）：原来写的是裸否定词（含单个「不是」），
#   只要句中出现就翻转 → 「新增：… （不是待办清单）」被判成负面。
#   改成：否定词必须**紧邻**断言词，才认定是"否定了这件事"。
NEGATION_RE = re.compile(
    r"(?:未|没有|没能|未能|尚未|并未|无法|不能|不是)\s*"
    r"(?:完成|成功|通过|做到|实现|生成|创建|保存|写入|推送|提交|部署|解决|修复|读取|更新)")

PLAN_RE = re.compile(
    r"(计划|将要|将在|后续|下次|届时|一旦|建议|应该|务必|"
    r"下一步|接下来|预计|目标是|只做|限时|打算|准备|将先|先做|如果|若|"
    r"应当|理应|宜)")

# 含这些词的句子，即使有悬念标记也**不算计划句**（已经做完了）
DONE_RE = re.compile(
    r"(已完成|新增|已修改|已删除|已生成|已保存|已导出|已推送|已提交|"
    r"校验通过|已读|已记|已入库|搞定|done|completed)")

# ★ 第四份冻结集 F05 暴露：数字未必紧贴单位（「从 1200 降到 800」），
#   但只要有"变化动词 + 数字"，它就是**可核对的事实断言**。
#   这里把这个组合识别为事实断言，让数字进入片段提取。
CHANGE_VERB_RE = re.compile(r"(降到|跌到|降到|提升到|提高到|达到|增至|增长到|减少到|"
                           r"从\s*\d|到\s*\d|降到\s*\d|下降|上升|提升|降低|减少|增加)")
BARE_NUM_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:倍|%|ms|s|秒|分钟|小时)?")

SENT_SPLIT = re.compile(r"[。！？!?\n]+")
# 明显不是"断言"的句子：疑问、客套、纯列表符号
NOT_CLAIM = re.compile(r"^(?:#|>|\||-|\*|\d+\.)?\s*$|^(?:请问|你能|要不要|是否)|[？?]$")

# ★ 2026-10-25（D1，真实会话暴露）：切分器把 markdown 表格行 / 标题 / JSON 片段
#   当成「断言」送进判定。3 位真实标注者盲标 150 条后：53 条被三人一致判「不判分」，
#   系统却给了 S/P/C；其中 39 条形态可机械识别（表格行 27、列表 9、JSON 1、标题 2）。
#   这里在切分阶段就跳过这些形态，避免把「数据」当「陈述」。
NONASSERT_LINE_RE = re.compile(
    r"^\s*(?:"
    r"\|"                                   # 表格行（以 | 开头）
    r"|`{3,}"                               # 代码围栏
    r"|#{1,6}\s"                            # markdown 标题
    r"|>[^<]*$"                             # 纯引用行
    r"|\*\*[^*]{1,40}\*\*\s*:?\s*$"         # 只有一段加粗短语（小标题）
    r"|\"[^\"\n]{1,60}\"\s*:"               # JSON 键值片段
    r"|\{\s*$|^\s*\}|^\s*\]\s*$|^\s*\[\s*$"  # JSON/数组括号残片
    r")"
)

_LIST_ITEM_RE = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|>\s)")


def _shape_is_non_assertion(s: str) -> bool:
    """这句在形态上就不是「陈述」吗？（表格/标题/JSON/无完成词的列表项）"""
    t = (s or "").strip()
    if not t:
        return True
    if NONASSERT_LINE_RE.match(t):
        return True
    if t.count("|") >= 2:            # 内嵌表格行（常出现在列表项里）
        return True
    if re.fullmatch(r"[\W_]+", t):   # 纯符号
        return True
    if t.count("\n") > 1:            # 多行块（表格/代码）
        return True
    # 列表项 / 引用项：只有出现「完成词」或「数字」才可能是断言
    if _LIST_ITEM_RE.match(t):
        has_done = (any(w in t.lower() for w in CLAIM_COMPLETION)
                    or bool(DONE_SOFT_RE.search(t))
                    or bool(CLAIM_DONE_RE.search(t)))
        if not has_done and not re.search(r"\d", t):
            return True
    return False



# ---------------------------------------------------------------- 复合断言原子拆分（W8 2026-10-24）
# 为什么：旧 split_claims（v1）只按 。！？换行 切，docstring 里写死了「切不出复合命题」。
#   于是一句里并列说 3 件事只能整句判一个态：
#   · 一件没做、另外两件有证据 → 整句 supported，漏掉没做的那件；
#   · 一件矛盾、另外两件都对 → 整句 contradicted，冤枉另外两件。
# 原则：只拆**明确的分句边界**（分号 / 逗号+并列转折连词 / 圈号列表），
#   不拆名词并列（A 和 B）、不拆数字（3+6=9、1—8 页、v2.3.1）、不拆引号/路径/URL 内文本。
#   宁可少拆，不可错拆 —— 反向守卫见 test_quality_audit [45]。
_NO_SPLIT_PAIRS = [("「", "」"), ("『", "』"), ("“", "”"), ("`", "`"), ('"', '"'),
                   ("《", "》"), ("【", "】"), ("[", "]")]
_URL_RE = re.compile(r"https?://[^\s，。；、）】」』]+")
_WINPATH_RE = re.compile(r"[A-Za-z]:\\[^\s，。；、）】」』]+")
_SEMICOLON_RE = re.compile(r"[；;]")
_CONNECTOR_RE = re.compile(
    r"[，,]\s*(?:并且|同时|另外|此外|而且|然后|随后|继而|但是|但|不过|然而|可是|并|且)")
_LEAD_CONNECTOR_RE = re.compile(
    r"^(?:并且|同时|另外|此外|而且|然后|随后|继而|但是|但|不过|然而|可是|并|且)"
    r"\s*[，,]?\s*")
_CIRCLED_ENUM_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]")
_BRACKET_ENUM_RE = re.compile(r"[（(][1-9][)）]")
_CN_ENUM_RE = re.compile(r"(一是|二是|三是|四是|五是|六是)")


def _mask_no_split(s: str) -> str:
    """把**不可拆区域**（引号/书名号/反引号内文本、URL、Windows 路径）等长遮蔽。

    遮蔽后仍用同一套下标去查分隔符，命中位置可直接映射回原文。
    """
    mask = list(s)
    spans = []
    for a, b in _NO_SPLIT_PAIRS:
        for m in re.finditer(re.escape(a) + r".{0,300}?" + re.escape(b), s, re.S):
            spans.append((m.start(), m.end()))
    for pat in (_URL_RE, _WINPATH_RE):
        for m in pat.finditer(s):
            spans.append((m.start(), m.end()))
    spans.sort()
    merged = []
    for st, en in spans:
        if merged and st <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], en))
        else:
            merged.append((st, en))
    for st, en in merged:
        for i in range(st, en):
            mask[i] = "\x00"
    return "".join(mask)


def _split_by_semicolon(s: str) -> list[str]:
    """按分号拆（引号/路径内的分号不拆）。"""
    masked = _mask_no_split(s)
    cuts = [m.start() for m in _SEMICOLON_RE.finditer(masked)]
    if not cuts:
        return [s]
    pieces, prev = [], 0
    for pos in cuts:
        pieces.append(s[prev:pos])
        prev = pos + 1
    pieces.append(s[prev:])
    return [p for p in pieces if p.strip()] or [s]


def _split_by_connector(s: str) -> list[str]:
    """按「逗号 + 并列/转折连词」拆；任一侧过短则放弃该处拆分。"""
    masked = _mask_no_split(s)
    hits = list(_CONNECTOR_RE.finditer(masked))
    if not hits:
        return [s]
    pieces, prev = [], 0
    for m in hits:
        piece = s[prev:m.start()]
        rest = s[m.end():]
        if len(piece.strip()) < 4 or len(rest.strip()) < 4:
            # 任一侧太短 → 不拆（避免把「但」「并」从词里切出来）
            continue
        pieces.append(piece)
        prev = m.end()
    pieces.append(s[prev:])
    return [p for p in pieces if p.strip()] or [s]


def _split_by_enum(s: str) -> list[str]:
    """按圈号列表（①② / （1）（2） / 一是二是）拆；至少要 2 个标记才拆。"""
    masked = _mask_no_split(s)
    marks = []
    for m in _CIRCLED_ENUM_RE.finditer(masked):
        marks.append((m.start(), m.end()))
    for m in _BRACKET_ENUM_RE.finditer(masked):
        marks.append((m.start(), m.end()))
    for m in _CN_ENUM_RE.finditer(masked):
        prev = masked[m.start() - 1] if m.start() > 0 else ""
        if prev not in ("", "。", "！", "？", "；", "\n", "：", "，", ","):
            continue
        marks.append((m.start(), m.end()))
    marks.sort()
    if len(marks) < 2:
        return [s]
    pieces = []
    head = s[:marks[0][0]].strip(" ，,；;")
    if len(head) >= 4:
        pieces.append(head)
    for i, (_st, en) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(s)
        piece = s[en:end]
        if piece.strip():
            pieces.append(piece)
    return pieces if len(pieces) >= 2 else [s]


def _split_compound_atoms(s: str) -> list[str]:
    """一个句子 → 若干**原子断言**（W8）。只拆明确分句边界，其余原样返回。"""
    s = (s or "").strip()
    if not s:
        return []
    atoms = []
    for part in _split_by_semicolon(s):
        for mid in _split_by_connector(part):
            atoms.extend(_split_by_enum(mid))
    cleaned = []
    for a in atoms:
        a = a.strip().strip("*").strip(" ，,；;、")
        a = _LEAD_CONNECTOR_RE.sub("", a).strip(" ，,；;、")
        if a:
            cleaned.append(a)
    return cleaned or [s]


# ---------------------------------------------------------------- 断言切分
def _classify_sentence(s: str) -> dict | None:
    """给**一个原子句**定性：完成/负面/验证/对冲/主观/闲聊/计划 + 具体片段。

    （W8 2026-10-24）从旧 split_claims 拆出的单句判定体：
    split_claims 负责「把句子切成原子句」，本函数只负责「这一个原子句怎么定性」。
    返回 None 表示这句不该进审计（无断言词、无具体片段、也不是计划句）。
    """
    low = s.lower()
    kinds = []
    # ★ 用固定词表 + 结构正则双保险：实证里「已新增文件 A」不在词表里，
    #   只靠词表会漏判整句完成声明（test_quality_audit case_list_style）。
    # ★ 否定优先：先说"没做到"，就不能再算成"做到了"
    _neg = bool(NEGATION_RE.search(s))
    # ★ 分类法 v2（窗口 E）：完成类检测再加一条结构兜底 DONE_SOFT_RE。
    #   否则「已批量重命名并压缩 35 张照片」这类句子既没进词表、又没抽到片段，
    #   会在切分阶段被整句丢弃 → test_precision 返回 skip（not_scored）。
    #   bug 3 的口径：not_scored 只留给计划/建议/主观评价，完成声明一律进判分。
    # ★★ 2026-10-23（W4）：再补两条**生成式结构规则**（不是加词）——
    #   CLAIM_DONE_STRUCT_RE 认「已经+动作+好了/成/完/到/进」，
    #   CLAIM_DONE_ASPECT_RE 认「都/也+动作+好了/到/完」。
    #   这就是「已经保存好了 / 已经合并成 / 都已经装好了」整句被丢的修法。
    if (any(w in low for w in CLAIM_COMPLETION)
            or CLAIM_DONE_RE.search(s) or DONE_SOFT_RE.search(s)
            or CLAIM_DONE_STRUCT_RE.search(s)
            or CLAIM_DONE_ASPECT_RE.search(s)) and not _neg:
        kinds.append("完成")
    elif _neg:
        kinds.append("负面")
    if any(w in low for w in CLAIM_VERIFICATION):
        kinds.append("验证")
    if any(w in low for w in CLAIM_NEGATIVE):
        kinds.append("负面")
    if any(w in low for w in CLAIM_HEDGE):
        kinds.append("对冲")
    # ★ 2026-10-23（W4）：主观评价 / 闲聊**显式保留**为 not_scored，
    #   不再静默丢弃 —— 丢弃等于 report 里看不到，没法解释"为什么不判分"。
    #   ground_claims 第 1 步会把它们判成 not_scored（不进分母）；
    #   若同句还有完成词，则完成词优先（v2 §六：凡有完成词的必须进判分）。
    if SUBJECTIVE_RE.search(s):
        kinds.append("主观")
    if CHAT_RE.search(s):
        kinds.append("闲聊")
    # 含具体数字/路径的，即使没有断言词也算"事实性断言"
    # ★ W4：先在**原文**上抽千分位片段（8,600 → 8600），
    #   再在去掉千分位逗号的影子串上抽其余片段，两种写法不会互相污染。
    spans = []
    _is_cjk = lambda ch: "\u4e00" <= ch <= "\u9fff"

    def _add_span(value, start, end, source):
        v = _strip_thousand_seps(str(value or "")).strip()
        if len(v) < 2 or v in spans:
            return
        # ★ 假片段过滤（真实数据暴露）：数字+单位若左右**都紧邻汉字**，
        #   多半是专有名词的一部分（如「妈妈杯10天作战表」里的「10天」），不提取。
        left_cjk = start > 0 and _is_cjk(source[start - 1])
        right_cjk = end < len(source) and _is_cjk(source[end])
        if left_cjk and right_cjk:
            return
        spans.append(v)

    for pat in THOUSAND_SPAN_PATTERNS:
        for m in pat.finditer(s):
            _add_span(m.group(0), m.start(), m.end(), s)
    s_probe = _strip_thousand_seps(s)
    for pat in SPAN_PATTERNS:
        for m in pat.finditer(s_probe):
            v = (m.group(1) if m.groups() else m.group(0)) or ""
            _add_span(v, m.start(), m.end(), s_probe)
    # ★ W8（阶段六 P1 中文数词扩展）：中文数词 + 具体单位同样算**可核对片段**。
    #   否则「这次活动共有八百六十二人报名」既没有断言词、也没有阿拉伯数字，
    #   会在切分阶段被整句丢弃（claims=0 → not_scored），于是中文写法的
    #   同类计数不符（八百六十二 vs 889）永远进不了冲突判定。
    #   只收具体单位（见 `_cn_fact_spans`），约数/序号/全量/通用量词都不收。
    for sp in _cn_fact_spans(s, skip_total=_has_completion_word(s)):
        if len(spans) < 6 and sp not in spans:
            spans.append(sp)
    # ★ ① 变化动词 + 数字 → 事实断言（F05，第四份冻结集暴露）
    #   必须在 spans 计算**之后**执行 —— 否则引用未定义的 spans（会 UnboundLocalError）。
    #   例：「重构后代码行数从 1200 降到 800」数字不紧贴单位，靠本条规则救回来。
    if not kinds and not spans and CHANGE_VERB_RE.search(s):
        _bn = [m.group(1) for m in BARE_NUM_RE.finditer(s_probe)]
        if _bn:
            kinds.append("事实")
            for _x in _bn:
                if _x not in spans:
                    spans.append(_x)
    # ★ ④ 先判计划句：计划句即使没有断言词/具体片段也要**保留**，
    #   并标成 plan（不参与判分）。这样"这句为什么不判分"是透明的；
    #   原来直接在过滤阶段丢弃，等于悄悄消失，评测里看不出来。
    # ★ W4：结构规则的完成词也要压过计划标记（「计划…已…好了」不能算计划句）。
    _done_hit = bool(DONE_RE.search(s) or CLAIM_DONE_RE.search(s)
                     or DONE_SOFT_RE.search(s)
                     or CLAIM_DONE_STRUCT_RE.search(s)
                     or CLAIM_DONE_ASPECT_RE.search(s))
    is_plan = bool(PLAN_RE.search(s)) and not _done_hit
    if not kinds and not spans and not is_plan:
        return None
    if is_plan:
        kinds = kinds + ["计划"]
    return {"text": s[:220], "kinds": kinds or ["事实"], "spans": spans[:6],
            "grounded": None, "why": ""}


def split_claims(text: str) -> list[dict]:
    """把一段回答切成**原子断言**：按句读切 → 复合句原子拆分（W8）→ 单句定性。

    ★ 2026-10-24（W8）：旧实现只按 。！？换行 切，复合句只能整句判一个态。
      现在先按「分号 / 逗号+并列转折连词 / 圈号列表」把复合句拆成原子句，
      再逐原子句定性；其余过滤规则与旧版一致。
    """
    out = []
    for raw in SENT_SPLIT.split(text or ""):
        s = raw.strip().strip("*").strip()
        if not s or len(s) < 4 or NOT_CLAIM.search(s):
            continue
        # ★ 2026-10-25：形态上不是陈述（表格/标题/JSON/无完成词列表项）→ 不进审计
        if _shape_is_non_assertion(s):
            continue
        for atom in _split_compound_atoms(s):
            c = _classify_sentence(atom)
            if c:
                out.append(c)
    return out


def _count_input_sentences(text: str) -> int:
    """统计「输入句子数」——即 split_claims 的**候选断言单元**数量（与切分同一过滤口径）。

    ★ 2026-10-23（W5，外部 AI 专家审核）：旧指标把「切分覆盖率」叫成「判定覆盖率」，
      混淆了三件事：切分成功率 / 结构性不适用（not_scored）/ 弃权（本系统没有弃权机制）。
      有了输入句子数，才能把 split_rate 算清楚：
        split_rate = 被切出的断言数 ÷ 输入句子数
    ★ 2026-10-24（W8）：复合句会被拆成多个原子断言，输入侧必须同口径按原子句计数，
      否则 split_rate = 断言数/句子数 会超过 100%，与指标定义自相矛盾。
    """
    n = 0
    for raw in SENT_SPLIT.split(text or ""):
        s = raw.strip().strip("*").strip()
        if not s or len(s) < 4 or NOT_CLAIM.search(s):
            continue
        if _shape_is_non_assertion(s):
            continue
        for atom in _split_compound_atoms(s):
            if atom and len(atom) >= 4 and not NOT_CLAIM.search(atom):
                n += 1
    return n


# ---------------------------------------------------------------- 证据池
def build_evidence(turns: list, final: dict | None = None) -> list[dict]:
    """把每次工具调用变成一条**证据单元**。

    一条证据 = 它做了什么 + 实际返回了什么。
    ⚠️ 只有 output_summary 为空/含混（如"执行成功"）的，**不算有效证据** ——
       这正是"敷衍"的典型形态，必须能识别出来。
    """
    ev = []
    for t in turns or []:
        if not isinstance(t, dict):
            continue
        for c in (t.get("tool_calls") or []):
            if not isinstance(c, dict):
                continue
            # ★ 兼容 output 别名（2026-10-19 实证：我写评测夹具时用了 output，
            #   而这里读 output_summary → 所有证据被判空泛 → 召回率被误测成 58%。
            #   这是一个"测试夹具 vs 被测代码字段不一致"的坑，让代码容错更稳。）
            summary = str(c.get("output_summary") or c.get("output") or "").strip()
            vague = (not summary) or summary in ("执行成功", "成功", "ok", "已完成")
            # ★ 2026-11-06（改进②）：证据来源标签
            #   source=agent/self/generated 表示这是 agent **自己生成**的内容，
            #   不能作为支持后续断言的外部证据（修「循环引用」盲区）。
            _src = str(c.get("source") or "").strip().lower()
            _agent_gen = _src in ("agent", "self", "generated", "agent_generated", "ai")
            ev.append({
                "tool": str(c.get("tool") or ""),
                "input": str(c.get("input") or "")[:300],
                "output": summary[:400],
                "status": str(c.get("status") or "").lower(),
                "vague": vague,
                "turn": t.get("index"),
                "source": _src or "external",
                "agent_generated": _agent_gen,
            })

    # ★ ① verification_evidence 也是合法证据（实证：git push 只记在这里，没进 tool_calls）。
    #   不纳入会把**真做过的验证**判成"没做"，属于我方假指控。
    for i, v in enumerate((final or {}).get("verification_evidence") or []):
        if not isinstance(v, dict):
            continue
        how = str(v.get("how") or "").strip()
        res = str(v.get("result") or "").strip()
        ev.append({
            "tool": "verify",
            "input": how[:300],
            "output": res[:400],
            "status": "ok",
            "vague": (not how and not res),
            # 验证发生在哪一轮不确定，标记为"宣称轮"（-1 表示不限轮次）
            "turn": -1,
        })
    return ev


# ---------------------------------------------------------------- 断言落地判定

@_lru_cache(maxsize=16384)
def _norm(t: str) -> str:
    """归一化：去空白、全角转半角常见标点、统一大小写。

    ★ 为什么必须归一化：实证里「119 个」在证据中写作「119；」，
      不做归一就会**误判成"未落地"**，进而给出错误的指控 ——
      一个会误伤的报告，比没有报告更糟。
    """
    if not t:
        return ""
    t = str(t)
    table = {"，": ",", "。": ".", "；": ";", "：": ":", "（": "(", "）": ")",
             "「": '"', "」": '"', "『": '"', "』": '"', "　": " ",
             "％": "%", "％": "%"}
    for a, b in table.items():
        t = t.replace(a, b)
    # ★ 2026-10-23（W4）：千分位逗号先归一 —— 断言写「8,600」、证据写「8600」时
    #   必须能对上；不归一会把同一数字判成"对不上"。
    t = _strip_thousand_seps(t)
    return re.sub(r"\s+", "", t).lower()


def _cores(span: str) -> list[str]:
    """从一个"关键片段"里取出**可用于核对的核**。

    例：
      '119 个'      → ['119']
      '2026-10-16'  → ['2026-10-16', '2026', '10', '16']
      'atoms.py'    → ['atoms.py', 'atoms']
      '退出码 0'     → ['0']（单个 0 太泛，会被过滤）
    """
    span = str(span or "").strip()
    out = []
    if not span:
        return out
    # 日期优先整体
    m = re.search(r"\d{4}-\d{2}-\d{2}", span)
    if m:
        out.append(m.group(0))
    # 文件名
    m = re.search(r"[\w./-]+\.(?:py|md|json|jsonl|csv|toml|txt|xlsx|pdf|sql|"
                  r"yaml|yml|ini|log|sh|js|ts|html|png|jpg)", span)
    if m:
        out.append(m.group(0))
        stem = m.group(0).rsplit(".", 1)[0]
        if len(stem) >= 3:
            out.append(stem)
    # 数字：长度≥2 直接当核心；长度 1 的值只有当片段**带单位**时才保留
    #   （③ 实证：「3 个报错」vs「3 passed」—— 单数字被长度门槛吞掉，导致漏判）
    _nums = re.findall(r"\d+(?:\.\d+)?", span)
    _has_unit = bool(re.search(r"(?:行|条|个|次|页|篇|天|小时|分钟|周|月|年|倍|分|"
                               r"元|岁|人|户|台|件|笔|份|项|家|座|名|位|pp|%|"
                               r"GB|MB|KB|秒|s|h|min)", span))
    for num in _nums:
        if len(num) >= 2 or _has_unit:
            out.append(num)
    # 剩下的原串（去掉括号引号与量词）
    cleaned = re.sub(r"[（）()「」『』《》\"'`]", "", span).strip()
    if len(cleaned) >= 3:
        out.append(cleaned)
    # 去重保序
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:6]


# ★ 2026-10-23（W4）：完成/汇总类常用词 —— 判断"剩下还有没有话题词"时先剥掉它们。
_TOPIC_STOP_RE = re.compile(
    r"(?:已经|完成|执行|运行|处理|搞定|结束|成功|好了|核对|检查|确认|无误|"
    r"本次|这次|目前|现在|返回|结果|全部|一切|正常|顺利|已)")


def _topic_overlap(claim: str, ev_norm: str) -> bool:
    """断言与（已归一的）证据之间有没有**话题重合**。

    ★ 2026-10-23（W4）：修「任意一次成功执行都能给任意完成声明背书」的漏洞。
      实证 B11「日志采集脚本已经改好了…」vs 同轮证据「git log --oneline -5」——
      完成词覆盖修好后，这句第一次进入判分，却被"本轮有成功执行"判成 supported；
      按分类法 v2 第 2 步，证据与断言**话题无关**就该判 no_evidence。

    判据（保守）：
      ① 先把完成/汇总类词剥掉，若断言**只剩这些词**（如「已经完成」「已搞定」）——
         它本来就是"整轮汇总句"，没有话题可比 → 交给执行记录支撑（返回 True）；
      ② 证据里没有任何汉字（纯英文）→ 跨语言无法比话题，不据此判无关（返回 True），
         这是**已知不足**，宁可漏判也不做假指控；
      ③ 否则要求：CJK 二元组或长度≥3 的英文词，有一个在证据里出现。
    """
    claim = str(claim or "")
    ev = str(ev_norm or "")
    if not ev:
        return False
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", _TOPIC_STOP_RE.sub("", claim))
    en_toks = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", claim.lower())
    if not cjk_runs and not en_toks:
        return True
    if not re.search(r"[\u4e00-\u9fff]", ev):
        return True
    for run in cjk_runs:
        for i in range(len(run) - 1):
            bigram = _norm(run[i:i + 2])
            if bigram and bigram in ev:
                return True
    for tok in en_toks:
        if _norm(tok) in ev:
            return True
    return False


def _unit_kind(text: str) -> str | None:
    """判断一个片段/证据里的数字属于哪一类**可比值**。

    ★ 为什么必须分类（2026-10-22 窗口 D 踩到的坑）：
      旧规则「双方都有数字且无交集 → 冲突」会把**不可比的量**判成反证。
      实测 F17：「查询提速 3 倍」vs 证据「query 1.2s -> 0.4s」——
      1.2/0.4 恰好就是 3 倍，语义上支持，却被旧规则判成"数字对不上"。
      现在只有**同类量**（百分比/计数/时长）才比较；倍率与时长不可比 → 不判冲突。
    """
    t = str(text or "")
    # ★ 2026-10-23（W1 内核精修）：`pp`（百分点）必须**独立成词**才算比例。
    #   旧写法 `(?:%|％|pp|百分比)` 会把英文里的 pp 子串也当比例 ——
    #   实测 B38 证据「6 skipped(no change)」中 skip**pp**ed 被误判成 pct，
    #   于是数字 6 进不了计数池，子集和 3+6=9 根本凑不出来（缺陷 2 的隐藏原因）。
    if (re.search(r"(?:%|％|百分比)", t)
            or re.search(r"(?<![A-Za-z])pp(?![A-Za-z])", t)):
        return "pct"
    if "倍" in t:
        return "multiple"
    # ★ 2026-10-23（窗口 E）：计数必须排在时长**前面**。
    #   time 正则里有裸 `s`（秒），"tests"/"files"/"rows" 这类以 s 结尾的英文单位
    #   会被误判成时长 —— 实测 H08「48 tests」被当成"48 秒"，
    #   再与证据里的 3.2s 一比就误判成"时长对不上"（supported 被误伤成 contradicted）。
    if re.search(r"(?:行|条|个|次|页|篇|天|周|月|年|分|处|件|笔|份|项|家|座|名|位|人|户|"
                 r"lines?|files?|tests?|rows?|records?|items?|cases?|errors?|"
                 # ★ 2026-10-23（窗口 E）：数字冲突未触发（bug 1）的关键——
                 #   证据写的是「saved (1 sheet)」，旧词表不认 sheet，
                 #   于是"断言 3 个 vs 证据 1 个"被判成 supported。补齐同类计数单位。
                 r"pages?|sheets?|clauses?|functions?|scripts?|classes?|commits?|"
                 r"packages?|modules?|columns?|fields?|tables?|charts?|slides?|"
                 r"words?|chars?|passed|failed|failures?)", t):
        return "count"
    if re.search(r"(?:ms|秒|s|min|分钟|小时|h)(?![0-9A-Za-z])", t):
        return "time"
    return None


def _nums_with_units(text: str) -> list[tuple]:
    """从**原始文本**里取 (数值, 单位类别) 列表。

    ★ 2026-10-23（窗口 E）：必须在**带空格的原文**上抽取，不能在归一化后的串上抽。
      原因：`_norm()` 会删掉所有空格，「to 2 functions」变成「to2functions」，
      数字前紧邻字母 → 会被误当成标识符的一部分；
      而「sales_q3.xlsx」里的 3 才是真正要排除的标识符内数字。
      所以用负向后顾 `(?<![0-9A-Za-z_])` + 允许数字与单位之间有空格的窗口。
      实证：bug 1 的第二层原因就是 `sales_q3.xlsx` 的 3 被当成证据里的计数，
      使"断言 3 个 vs 证据 1 个"错误地"有交集 → 无冲突"。
    """
    out = []
    # ★ 2026-10-23（W4）：证据里的千分位（7,240 / 1,284）先归一，
    #   否则只会抽到 240 / 284，与断言里的 7240 / 1284 比不出冲突、也核不上。
    _raw = _strip_thousand_seps(text or "")
    for m in re.finditer(r"(?<![0-9A-Za-z_])(\d+(?:\.\d+)?)\s*([^\d\s]{0,12})", _raw):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        out.append((v, _unit_kind(m.group(2) or "")))
    return out


def _subset_sum_covers(target: float, ev_counts: list[float]) -> bool:
    """断言的**总数**能否由证据里的若干**分项**精确加出来（子集和）。

    ★ 2026-10-23（W1 内核精修，缺陷 2）：B38「已重写目录下全部 9 个脚本」
      vs 证据「rewrote 3 scripts；6 skipped(no change)」——
      3+6=9 是**子集和**，语义上是"9 个都处理了（3 个重写、6 个跳过）"，
      不是"多说了 6 个"，因此**不该算冲突**。

    ★ 防放松措施（这是与"编造个数"的分界线，必须逐条守住）：
      ① **精确相等**：不做 ± 容差 —— 编造的数是"对不上"的，容差才是放水；
      ② **至少 2 个分项**参与：否则退化成"这个数在证据里出现过"，不需要本容差；
      ③ **每个分项最多用一次**（0/1 背包），不允许同一个数反复加；
      ④ 只接受**正整数**计数：小数（比例/时长）不适用子集和；
      ⑤ **只作用于计数（count）分支**，比例/时长/裸数字分支口径不变。

    例：target=9, ev=[3,6] → True；target=9, ev=[3,5] → False（3+5=8≠9）。
    """
    try:
        t = int(round(float(target)))
    except (TypeError, ValueError):
        return False
    if abs(float(target) - t) > 1e-9 or t <= 0:
        return False
    vals = []
    for v in ev_counts or []:
        try:
            iv = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        if abs(float(v) - iv) <= 1e-9 and iv > 0:
            vals.append(iv)
    if len(vals) < 2:
        return False
    # DP：sum -> 达到该和所需的最少分项数（0/1 背包，每个分项只能用一次）
    best = {0: 0}
    for v in vals:
        for s, n in list(best.items()):
            ns = s + v
            if ns > t:
                continue
            if best.get(ns, 10 ** 9) > n + 1:
                best[ns] = n + 1
    return best.get(t, 0) >= 2


# ================================================================
# 中文数词 / 量词 / 范围词 / 约数（2026-10-23 W5）
#
# 【为什么必须做（外部 AI 专家审核 + 主窗口核实）】
#   旧检测只认阿拉伯数字，于是：
#     「今年**四个**季度营收都查到了」 vs 证据「返回 Q1、Q2 两季数据」→ 抽不出 4
#     「**五个**门店的销售表已经合并」   vs 证据「合并 3 张表」        → 抽不出 5
#     「旧数据也**全部**迁进新分区」      vs 证据「24 个分区…18 个迁完」→ 抽不出"全量"冲突
#   三条都判成 supported —— 这是**假支持**，比漏判更伤信任。
#
# 【设计边界（不能为了召回把冲突判定放松，也不能造新假指控）】
#   ① 中文数量只用于**新增的中文冲突判定**，阿拉伯数字原有口径不动；
#   ② 约数（约/大约/左右/多/余/近/超过/至少/最多…）与序号（第 3 页/第三页）
#      **不参与精确相等判定**；
#   ③ 只比**具体单位**（门店/季度/页/章节/表格/脚本/分区…），通用量词（个/张/份/条）
#      仍交给原有计数分支，避免双重判定；
#   ④ 全量词（全部/所有/每个…）不与"分项计数"做同义比较，只在证据里同一单位出现
#      **≥2 个不同精确计数**（说明至少有一项没做全）时判冲突。
# ================================================================
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_PLACE = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CN_NUM_CHARS = "零〇一二三四五六七八九十百千万两"


def _cn_num_to_int(s: str) -> int | None:
    """中文数词 → 整数；不是合法中文数词返回 None。

    覆盖：一二三四五六七八九十百千万 + 两；复合写法
      六 → 6 ｜ 十二 → 12 ｜ 二十 → 20 ｜ 二十三 → 23 ｜ 一百二十 → 120 ｜ 两千 → 2000
    """
    s = str(s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if any(ch not in _CN_NUM_CHARS for ch in s):
        return None
    total = section = number = 0
    for ch in s:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
            continue
        unit = _CN_PLACE.get(ch)
        if unit is None:
            return None
        if unit == 10000:                      # 万：结算前面的一节
            section = (section + number) * 10000
            total += section
            section = number = 0
        else:                                  # 十/百/千
            if number == 0:                    # 「十二」里的十 = 一十
                number = 1
            section += number * unit
            number = 0
    total += section + number
    return total if total > 0 else (0 if set(s) <= set("零〇") else None)


# 具体单位 → 归一单位（只用于"同类量"比较；不可比的量不判冲突）
_CN_UNIT_ALIASES = {
    # 通用量词（值不参与"具体单位"冲突，交给原有计数分支）
    "个": "count", "只": "count", "件": "count", "款": "count", "类": "count",
    "人": "person", "位": "person", "名": "person",
    # 具体单位
    "家": "store", "店": "store", "门店": "store",
    "条": "item", "项": "item", "处": "item", "轮": "round",
    "份": "copy", "张": "piece", "次": "time", "批": "batch", "组": "group",
    "页": "page", "篇": "section", "章": "section", "节": "section", "章节": "section",
    "季度": "quarter", "季": "quarter", "月份": "month", "月": "month",
    "年": "year", "天": "day", "周": "week", "小时": "hour", "分钟": "minute",
    "表格": "table", "表": "table", "工作表": "table",
    "总表": "table", "汇总表": "table", "明细表": "table", "数据表": "table",
    "柜子": "cabinet", "依赖": "dependency", "脚本": "script",
    "分区": "partition", "文件": "file", "文件夹": "folder", "目录": "folder",
    "合同": "contract", "问卷": "questionnaire",
    "照片": "photo", "记录": "record", "行": "line", "报告": "report",
    "模块": "module", "测试": "test", "用例": "case", "部门": "department",
    "流程": "process", "任务": "task", "待办事项": "task", "版本": "version",
}
# 数词后可能紧跟的量词（需要先剥掉才能找到后面的具体名词）
_CN_QUANTIFIERS = ("个", "家", "条", "份", "页", "张", "次", "项", "处", "轮",
                   "台", "件", "笔", "户", "座", "名", "位", "人", "只", "套",
                   "款", "类", "场", "批", "组", "层", "列", "行", "篇", "章",
                   "节", "门", "辆", "本", "册", "包", "箱", "盒", "盘", "期",
                   "届", "队", "所", "部", "遍", "趟", "番", "阵", "通")
_CN_NOUN_UNITS = tuple(sorted([n for n in _CN_UNIT_ALIASES if len(n) >= 2],
                              key=len, reverse=True))
_CN_SINGLE_UNITS = tuple(sorted([n for n in _CN_UNIT_ALIASES if len(n) == 1],
                                key=len, reverse=True))
# 约数 / 上下界（不参与精确相等判定）。允许「大约**有** 200 行」这种中间夹动词的写法。
_CN_INEXACT_BEFORE = re.compile(
    r"(?:约|大约|大概|差不多|将近|近|超过|至少|最多|不足|不到|逾)\s*(?:有|为|是|计)?\s*$")
_CN_INEXACT_AFTER = re.compile(r"^\s*(?:多|余|左右|以上|以下|开外)")
# 范围词：全量 / 总计（「一共/共/总计/合计」是"总数标记"，不是全量冲突）
_CN_UNIVERSAL_RE = re.compile(r"(?:全部|所有|整本|整篇|整个|每个|各个|全量|每一)")
_CN_TOTAL_RE = re.compile(r"(?:一共|总共|总计|合计|累计|共)")
# 参与"具体单位"冲突的单位白名单（时间单位/通用量词不在内，避免类别错误）
_CN_COUNTABLE = ("store", "table", "quarter", "page", "section", "cabinet",
                 "dependency", "script", "partition", "file", "contract",
                 "questionnaire", "photo", "record", "report", "module",
                 "test", "case", "department", "task", "person", "month")
# 约数互证允许的单位集（在可精确比较单位之外，再收「行」这类常见计数单位）
_CN_APPROX_UNITS = _CN_COUNTABLE + ("line",)


@_lru_cache(maxsize=8192)
def _unit_canon_from(after: str) -> str | None:
    """给定「数字之后」的文本，判断这个数字在数什么（归一单位）；判不出返回 None。

    例：'个月分区；…' → partition ｜ '张表写入' → table ｜ '个门店的' → store
        '行' → line ｜ '页' → page ｜ '位同事' → person
    """
    t = re.sub(r"^[\s，。；、：:（）()\"'`—–]+", "", str(after or ""))[:12]
    if not t:
        return None
    rest = t
    first_q = None
    for _ in range(2):
        hit = next((q for q in _CN_QUANTIFIERS if rest.startswith(q)), None)
        if not hit:
            break
        if first_q is None:
            first_q = hit
        rest = rest[len(hit):]
    # ① 量词后面紧跟的具体名词（如「个门店」→ 门店）
    for noun in _CN_NOUN_UNITS:
        if rest.startswith(noun):
            return _CN_UNIT_ALIASES[noun]
    # ② 「24 个月分区」这类：名词前面还夹了一个时间单位，先剥掉再找
    _time_prefix = re.match(r"^(?:个)?(?:月份|月|年|天|周|季度|季)", rest)
    if _time_prefix:
        _rest2 = rest[_time_prefix.end():]
        for noun in _CN_NOUN_UNITS:
            if _rest2.startswith(noun):
                return _CN_UNIT_ALIASES[noun]
    # ③ 单字单位（页/表/季/月…）
    for unit in _CN_SINGLE_UNITS:
        if rest.startswith(unit):
            return _CN_UNIT_ALIASES[unit]
    # ④ 只跟了量词（如「12 页」剥完剩空）
    if first_q:
        return _CN_UNIT_ALIASES.get(first_q)
    return None


def _unit_phrase(after: str) -> str:
    """从「数字之后」的文本里取出**可读的单位短语**（只用于报告措辞与片段）。

    例：'人报名。' → '人' ｜ '个模块已经' → '个模块' ｜ '份合同' → '份合同'

    ★ 2026-10-24（W8）：原来直接截取数字后 4 个字符（`[^，。；、]{1,4}`），
      于是「两个模块已经改完」被抽成 unit_text=『个模块已』，报告里出现
      「数量『两个模块已』= 2」这种读不懂的措辞；也会让片段拼成「2个模块已」，
      在证据里永远匹配不上。
    """
    t = re.sub(r"^[\s，。；、：:（）()\"'`—–]+", "", str(after or ""))[:12]
    if not t:
        return ""
    quant = next((q for q in _CN_QUANTIFIERS if t.startswith(q)), None)
    rest = t[len(quant):] if quant else t
    noun = next((n for n in _CN_NOUN_UNITS if rest.startswith(n)), None)
    if noun:
        return (quant or "") + noun
    for unit in _CN_SINGLE_UNITS:
        if rest.startswith(unit):
            return unit
    return quant or ""


def _extract_counts_cn_raw(text: str) -> list[dict]:
    """抽取**中文数词**的数量表达，返回结构化条目。

    每条：{value, numeral, unit, unit_text, approx, ordinal, universal, total, result, source}
      · approx    约数/上下界 → 不参与精确相等判定
      · ordinal   序号（第 3 页 / 第三个）→ 不是数量（直接跳过，不产出条目）
      · universal 每个/各个/每一 → 全量表达，不是精确数量
      · total     一共/共/总计/合计 引导的总数
      · result    成/为/生成… 之后的数量是**产出物**，不与证据里的输入量做同义比较
    """
    t = str(text or "")
    out = []
    for m in re.finditer(r"[" + _CN_NUM_CHARS + r"]{1,8}", t):
        numeral = m.group(0)
        value = _cn_num_to_int(numeral)
        if value is None:
            continue
        before = t[max(0, m.start() - 6):m.start()]
        after = t[m.end():m.end() + 14]
        if re.search(r"第\s*$", before):        # 序号
            continue
        # ★ W6：数值区间（一至八页 / 1—8 页）里的数字不是独立数量
        if (before[-1:] and before[-1:] in _RANGE_OP_CHARS) \
                or re.search(r"(?:[0-9零〇一二三四五六七八九十百千两])\s*[至到]\s*$", before):
            continue
        if (after[:1] and after[:1] in _RANGE_OP_CHARS) \
                or re.match(r"\s*[至到]\s*[0-9零〇一二三四五六七八九十百千两]", after):
            continue
        # 「五十**多**个文件」：多/余 是约数标记，且会挡在量词前面，先剥掉再判单位
        _after_unit = after
        _approx_after = bool(_CN_INEXACT_AFTER.match(after))
        _judge = re.match(r"^\s*(?:多|余)", after)
        if _judge:
            _after_unit = after[_judge.end():]
        unit = _unit_canon_from(_after_unit)
        if unit is None:
            continue
        raw_unit = _unit_phrase(_after_unit)
        out.append({
            "value": value, "numeral": numeral, "unit": unit,
            "unit_text": raw_unit,
            "approx": bool(_CN_INEXACT_BEFORE.search(before) or _approx_after),
            "ordinal": False,
            "universal": bool(re.search(r"(?:每|各)\s*$", before)),
            "total": bool(_CN_TOTAL_RE.search(before)),
            "result": bool(re.search(r"(?:成|为|得到|生成|输出|产出)\s*$", before)),
            "source": "cn",
        })
    return out


def _extract_counts_ar_raw(text: str) -> list[dict]:
    """抽取**阿拉伯数字**的数量表达（带具体单位），供中文数词冲突判定使用。

    ★ 只服务 W5 新增的中文/全量冲突，**不替换**原有 `_nums_with_units` 口径。
    """
    t = _strip_thousand_seps(text or "")
    out = []
    for m in re.finditer(r"(?<![0-9A-Za-z_.])(\d{1,7})(?![0-9A-Za-z_])", t):
        raw = m.group(1)
        value = int(raw)
        before = t[max(0, m.start() - 6):m.start()]
        after = t[m.end():m.end() + 14]
        # 序号「第 3 页」不是数量
        if re.search(r"第\s*$", before):
            continue
        # ★ W6：数值区间（1—8 页 / 3~5 条 / 2023—2024）里的数字不是独立数量
        if (before[-1:] and before[-1:] in _RANGE_OP_CHARS) \
                or re.search(r"[0-9]\s*[至到]\s*$", before):
            continue
        if (after[:1] and after[:1] in _RANGE_OP_CHARS) \
                or re.match(r"\s*[至到]\s*[0-9]", after):
            continue
        # 逐项列举里的**编号**（门店1、门店2、门店3 里的 1/2）不是数量，
        #   它们由 `_extract_enumerations` 折算成"共 3 家门店"。
        if re.match(r"\s*[、,，/和及]", after) and before[-1:] and \
                "\u4e00" <= before[-1] <= "\u9fff":
            continue
        _after_unit = after
        _approx_after = bool(_CN_INEXACT_AFTER.match(after))
        _judge = re.match(r"^\s*(?:多|余)", after)
        if _judge:
            _after_unit = after[_judge.end():]
        unit = _unit_canon_from(_after_unit)
        if unit is None:
            continue
        if 1900 <= value <= 2100 and unit != "year":
            # 单独一个年份（如「2023—2024 共 18 个分区」里的 2024）不是"数量"
            continue
        out.append({
            "value": value, "numeral": raw, "unit": unit,
            "unit_text": _unit_phrase(_after_unit),
            "approx": bool(_CN_INEXACT_BEFORE.search(before) or _approx_after),
            "ordinal": False,
            "universal": bool(re.search(r"(?:每|各)\s*$", before)),
            "total": bool(_CN_TOTAL_RE.search(before)),
            "result": bool(re.search(r"(?:成|为|得到|生成|输出|产出)\s*$", before)),
            "source": "ar",
        })
    return out


def _extract_enumerations_raw(text: str) -> list[dict]:
    """抽取**逐项列举**的数量：「门店1、门店2、门店3」→ 3 家门店；「Q1、Q2」→ 2 个季度。

    ★ 为什么需要：B12 证据「合并 门店1、门店2、门店3 三张表」没有阿拉伯总数，
      只有编号列举；不把列举折算成数量就抓不到"五个门店 vs 3 家"的冲突。
    """
    t = str(text or "")
    out = []
    # ① 中文名词 + 编号：门店1、门店2、门店3
    for m in re.finditer(r"([\u4e00-\u9fff]{2,4})\s*\d+"
                         r"(?:\s*[、,，/和及]\s*\1\s*\d+)+", t):
        base = m.group(1)
        n = len(re.findall(re.escape(base) + r"\s*\d+", m.group(0)))
        unit = _unit_canon_from(base)
        if unit and n >= 2:
            out.append({"value": n, "numeral": m.group(0)[:16], "unit": unit,
                        "unit_text": base, "approx": False, "ordinal": False,
                        "universal": False, "total": True, "result": False,
                        "source": "enum"})
    # ② 英文前缀 + 编号：Q1、Q2 / S1、S2（季度/章节）
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z]{1,2})\s*\d+"
                         r"(?:\s*[、,，/和及]\s*\1\s*\d+)+", t):
        base = m.group(1).lower()
        n = len(re.findall(re.escape(m.group(1)) + r"\s*\d+", m.group(0)))
        unit = {"q": "quarter", "s": "section", "c": "chapter",
                "v": "version"}.get(base)
        if unit and n >= 2:
            out.append({"value": n, "numeral": m.group(0)[:12], "unit": unit,
                        "unit_text": base.upper(), "approx": False,
                        "ordinal": False, "universal": False, "total": True,
                        "result": False, "source": "enum"})
    return out


# ============================================================
# ★ 2026-11-04 性能优化：数量抽取结果缓存（语义不变）
# ============================================================
# 实测（tools/profile_audit.py，218 轮）：这三个抽取器被调用 1,962 次，
# 占判定总耗时约 60%，而同一段证据文本会被**每条断言重复抽取**。
# 它们是纯函数，因此缓存"原始结果"、对外返回副本，既提速又不改变输出。
_COUNT_CACHE_MAX = 4096
_COUNT_CACHES: dict[str, dict[str, tuple]] = {}


def _cached_counts(kind: str, text: str, fn) -> list[dict]:   # noqa: ANN001
    cache = _COUNT_CACHES.setdefault(kind, {})
    hit = cache.get(text)
    if hit is None:
        raw = fn(text)
        hit = tuple(tuple(sorted(d.items())) for d in raw)
        if len(cache) >= _COUNT_CACHE_MAX:
            cache.clear()                     # 有界：超限整体清空（简单且可预测）
        cache[text] = hit
    return [dict(items) for items in hit]


def _extract_counts_cn(text: str) -> list[dict]:
    """中文数词抽取（带缓存）。"""
    return _cached_counts("cn", text, _extract_counts_cn_raw)


def _extract_counts_ar(text: str) -> list[dict]:
    """阿拉伯数字抽取（带缓存）。"""
    return _cached_counts("ar", text, _extract_counts_ar_raw)


def _extract_enumerations(text: str) -> list[dict]:
    """逐项列举抽取（带缓存）。"""
    return _cached_counts("en", text, _extract_enumerations_raw)


def _extract_counts_mixed(text: str) -> list[dict]:
    """中文数词 + 阿拉伯数字 + 逐项列举 的**统一数量抽取**（W5）。"""
    return (_extract_counts_cn(text) + _extract_counts_ar(text)
            + _extract_enumerations(text))


def _cn_fact_spans(text: str, skip_total: bool = False) -> list[str]:
    """把中文数词里**可精确比较的具体数量**归一成阿拉伯写法，当切分片段用。

    例：'共有八百六十二人报名' → ['862人'] ｜ '寄出十二份合同' → ['12份合同']

    ★ 只收 `_CN_COUNTABLE` 里的具体单位（人/份/页/文件/模块…），并且排除
      约数（大约二十份）、序号、全量与产出物；通用量词（个/张/次）也不收，
      避免把「我有一个想法」这类句子当成可核对的数量断言。
    ★ skip_total：完成/汇总句（含完成词）里的「一共/共 X 个」交给第 4 步的
      完成分支 + 子集和容差处理，不再额外造片段 —— 否则「一共九个脚本都处理
      完了」会因证据写的是「重写 3；跳过 6」而 supported→partial（V7E03 回归）。
      注意：只是不造**片段**，冲突判定仍走 `_count_unit_conflict`，不会漏冲突。
    """
    out: list[str] = []
    for e in _extract_counts_cn(text):
        if e.get("approx") or e.get("ordinal") or e.get("universal") or e.get("result"):
            continue
        if e.get("unit") not in _CN_COUNTABLE:
            continue
        if skip_total and e.get("total"):
            continue
        span = f"{int(e['value'])}{e.get('unit_text') or ''}"
        if len(span) >= 2 and span not in out:
            out.append(span)
    return out


def _range_markers(text: str) -> list[dict]:
    """列出文本里的范围表达：全量词（全部/所有/每个…）与总数标记（一共/共/总计/合计）。"""
    t = str(text or "")
    out = [{"kind": "universal", "word": m.group(0)}
           for m in _CN_UNIVERSAL_RE.finditer(t)]
    out += [{"kind": "total", "word": m.group(0)}
            for m in _CN_TOTAL_RE.finditer(t)]
    return out


# ================================================================
# W6（阶段六 P1）：约数区间互证 / 数值区间 / 跨 tool_call 计数隔离
#
# 【专家把关的两条原则】
#   ① 只收紧该收紧的：区间内**吻合**要能判 supported（约 50 vs 48），
#      区间外**仍判 contradicted**（约 50 vs 8）—— 不放松冲突方向；
#   ② 计数池必须**按 tool_call 隔离**：子集和容差只在同一个 tool_call 的分项之间生效，
#      跨 tool_call 拼分项会制造假支持（3 个 + 6 个来自两次不同执行，凑成"9 个"不成立）。
# ================================================================
# 约数互证区间规则（报告里同步展示）：
#   断言写「约 X」时，证据里的同类精确数量落在区间内 → 视为吻合；
#   落在区间外 → 仍判 contradicted。
#     X ≤ 100  → ±5（绝对量）
#     X > 100  → ±10% × X
_APPROX_ABS_MAX = 100
_APPROX_ABS_TOL = 5
_APPROX_REL_TOL = 0.10

# 数值区间（页码/数量）：1—8 页、3~5 条、12 至 20 个
_RANGE_OP_CHARS = "-—–~～"
_RANGE_RE = re.compile(r"(?<![\d.])(\d{1,7})\s*(?:-|—|–|~|～|至|到)\s*(\d{1,7})")


def _approx_tolerance(value: float) -> float:
    """约数互证的容差：≤100 取 ±5；>100 取 ±10%。"""
    v = abs(float(value))
    return _APPROX_ABS_TOL if v <= _APPROX_ABS_MAX else v * _APPROX_REL_TOL


def _approx_contains(approx_value, ev_value) -> bool:
    """证据里的数量是否落在「约 X」的合理区间内。"""
    try:
        return abs(float(ev_value) - float(approx_value)) <= _approx_tolerance(approx_value)
    except (TypeError, ValueError):
        return False


def _extract_ranges(text: str) -> list[dict]:
    """抽取**数值区间**：第 1—8 页 / 3~5 条 / 12 至 20 个。

    ★ 区间本身**不参与**数量冲突（1—8 页的 8 不是"8 页"）；
      但区间**上限**可作为"覆盖范围"线索：
      「整本 32 页译完」vs 证据「已译第 1—8 页」→ 上限 8 < 32 → 没覆盖全部。
    """
    t = _strip_thousand_seps(str(text or ""))
    out = []
    for m in _RANGE_RE.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        low, high = min(a, b), max(a, b)
        unit = _unit_canon_from(t[m.end():m.end() + 10])
        if 1900 <= low <= 2100 and 1900 <= high <= 2100:
            continue   # 年份区间（如 2023—2024）不是数量区间
        out.append({"low": low, "high": high, "unit": unit, "text": m.group(0)})
    return out


def _entry_in_range(entry: dict, ranges: list[dict]) -> bool:
    """这个数量条目是不是落在某个区间里（区间上下限都不算独立数量）。"""
    for r in ranges or []:
        if r["unit"] == entry["unit"] and r["low"] <= entry["value"] <= r["high"]:
            return True
    return False


def _approx_verdict(claim: str, ev_items: list[str]) -> tuple[str | None, list[float]]:
    """约数互证：返回 (冲突说明 or None, 命中区间内的**断言值**列表)。

    - 断言是「约 X」且证据里有同单位精确数量：
        有值落在区间内 → 命中（可支撑 supported）；
        所有值都在区间外 → 返回冲突说明（**不放松**：约 50 vs 8 仍判 contradicted）。
    - 证据侧的约数不参与（证据本身不确定，不能反过来指控断言）。
    """
    claim_counts = _extract_counts_mixed(claim)
    approx = [c for c in claim_counts
              if c["approx"] and c["unit"] in _CN_APPROX_UNITS]
    if not approx:
        return None, []
    pools = []
    for item in ev_items or []:
        cs = _extract_counts_mixed(item)
        pools.append([e for e in cs
                      if e["unit"] in _CN_APPROX_UNITS and not e["approx"] and not e["ordinal"]])
    hits, bad = [], []
    for c in approx:
        vals = [e["value"] for pool in pools for e in pool if e["unit"] == c["unit"]]
        if not vals:
            continue
        ok = [v for v in vals if _approx_contains(c["value"], v)]
        if ok:
            hits.append(c["value"])
        else:
            bad.append((c, sorted({int(v) for v in vals})[:3]))
    if bad:
        c, vals = bad[0]
        lo = c["value"] - _approx_tolerance(c["value"])
        hi = c["value"] + _approx_tolerance(c["value"])
        return (f"约数『{c['numeral']}{c['unit_text']}』≈{c['value']} 的合理区间 "
                f"[{lo:g}, {hi:g}] 不含证据里的 {vals} —— 超出约数区间"), hits
    return None, hits


def _range_coverage_conflict(claim: str, ev_items: list[str]) -> str | None:
    """全量断言 vs 证据区间上限：宣称全部 N，证据只覆盖到 1—M（M<N）→ contradicted。

    反向守卫：区间上限 ≥ N 不判冲突；非全量断言也不判；
              区间本身（1—8 页）不参与普通数量冲突（见 `_span_is_inexact`）。
    """
    if not _CN_UNIVERSAL_RE.search(claim):
        return None
    totals = [c for c in _extract_counts_mixed(claim)
              if c["unit"] in _CN_COUNTABLE and not c["approx"] and not c["ordinal"]]
    if not totals:
        return None
    for item in ev_items or []:
        for r in _extract_ranges(item):
            if not r["unit"]:
                continue
            for c in totals:
                if c["unit"] == r["unit"] and r["high"] < c["value"]:
                    return (f"句中称**全部** {c['value']} {c['unit']}，但证据里的区间 "
                            f"{r['low']}—{r['high']} 上限 {r['high']} < {c['value']}"
                            " —— 区间没有覆盖到全部")
    return None


def _span_is_inexact(text: str, span: str) -> bool:
    """这个片段在原文里是不是**约数或序号**：两者都不参与精确相等判定。"""
    t = _strip_thousand_seps(str(text or ""))
    sp = _strip_thousand_seps(str(span or ""))
    i = t.find(sp)
    if i >= 0:
        _len = len(sp)
    else:
        m = re.search(r"\d+(?:\.\d+)?", sp)
        if not m:
            return False
        i = t.find(m.group(0))
        if i < 0:
            return False
        _len = len(m.group(0))
    before = t[max(0, i - 6):i]
    if _CN_INEXACT_BEFORE.search(before):
        return True
    if re.search(r"第\s*$", before):
        return True
    # ★ W6：数值区间（第 1—8 页 / 3~5 条 / 12 至 20 个）里的数字不参与精确比较
    if (before[-1:] and before[-1:] in _RANGE_OP_CHARS) \
            or re.search(r"[0-9]\s*[至到]\s*$", before):
        return True
    after = t[i + _len:i + _len + 3]
    if (after[:1] and after[:1] in _RANGE_OP_CHARS) \
            or re.match(r"\s*[至到]\s*[0-9]", after):
        return True
    return bool(_CN_INEXACT_AFTER.match(after))


def _count_unit_conflict(claim_text: str, ev_items: list[str] | None = None,
                         ev_raw: str | None = None) -> str | None:
    """具体单位的数量冲突（W5 中文数词 + W6 阿拉伯数字统一 + 按 tool_call 隔离）。

    ① 具体单位的数量（门店/季度/页/脚本/人/行/文件…）在证据同单位里找不到，
       且不能由**同一个 tool_call 内**的分项精确子集和凑出 → contradicted；
    ② 全量断言（全部/所有/每个…）而**同一个 tool_call** 的证据里同一单位出现
       ≥2 个不同精确计数 → 至少有一项没做全 → contradicted（跨 tool_call 不拼分项）。

    不参与精确比较：约数（单独由 `_approx_verdict` 做区间互证）、序号、区间内数字、
    通用量词（个/张/份/条…仍交给原有计数分支）、产出物数量（"合并成一张总表"的 1）。
    """
    claim = str(claim_text or "")
    if not claim:
        return None
    if not ev_items:
        ev_items = [ev_raw or ""]
    claim_counts = _extract_counts_mixed(claim)
    claim_ranges = _extract_ranges(claim)
    pools = []
    for item in ev_items:
        pools.append([e for e in _extract_counts_mixed(item)
                      if not e["approx"] and not e["ordinal"]])
    # ① 具体单位：每个断言数量都要在**某个 tool_call** 里精确命中，或由其分项子集和覆盖
    for c in claim_counts:
        if c["unit"] not in _CN_COUNTABLE:
            continue
        if c["approx"] or c["ordinal"] or c["universal"] or c["result"]:
            continue
        if _entry_in_range(c, claim_ranges):      # 区间上下限不是独立数量
            continue
        matched_unit = False
        covered = False
        for pool in pools:
            vals = sorted({e["value"] for e in pool if e["unit"] == c["unit"]})
            if not vals:
                continue
            matched_unit = True
            # ★ W6：子集和只在**同一 tool_call 内**生效；跨 tool_call 不拼分项
            if c["value"] in vals or _subset_sum_covers(c["value"], vals):
                covered = True
                break
        if not matched_unit:
            continue                              # 证据里没有同单位数量 → 不可比
        if not covered:
            _all = sorted({int(e["value"]) for pool in pools for e in pool
                           if e["unit"] == c["unit"]})[:3]
            return (f"数量『{c['numeral']}{c['unit_text']}』= {c['value']}，"
                    f"与证据里的 {_all} 个 {c['unit']} 对不上（同类计数不符，"
                    "且不在同一 tool_call 的分项和里）")
    # ② 全量断言 vs 同一 tool_call 内的分项计数
    if _CN_UNIVERSAL_RE.search(claim):
        claimed_units = {c["unit"] for c in claim_counts
                         if not c["approx"] and not c["ordinal"]}
        for pool in pools:
            per_unit: dict = {}
            for e in pool:
                if e["unit"] not in _CN_COUNTABLE:
                    continue
                per_unit.setdefault(e["unit"], set()).add(e["value"])
            for unit, vals in per_unit.items():
                if unit in claimed_units:
                    continue
                if len(vals) >= 2:
                    return (f"句中是**全量**断言（全部/所有/每个…），但同一 tool_call 的证据里 "
                            f"{unit} 有 {sorted(int(v) for v in vals)[:3]} 两个不同口径 "
                            "—— 不是全部完成")
    return None


def _cn_count_conflict(claim_text: str, ev_raw: str) -> str | None:
    """兼容 W5 的单证据签名（内部转成单 tool_call 池）。"""
    return _count_unit_conflict(claim_text, [ev_raw or ""])




def _number_conflict_detail(spans, ev_norm: str, ev_raw: str | None = None,
                            claim_text: str | None = None,
                            ev_items: list[str] | None = None) -> str | None:
    """句中有数字片段、证据里也有数字，但**同类的数字对不上** → 返回冲突说明。

    ★ 这是"编造"最典型的样子：说 37 个用例，实测 12 passed。
    ★ 2026-10-22（窗口 D）：原来只返回 bool，产出 none + "数字对不上"，
      与"根本没证据"混在一起。现在返回**可解释的冲突类型**，由调用方标成 contradicted：
        · 断言里的计数在证据里完全找不到对应值（如 120 个文件 vs 30 个文件）
        · 百分比/比率对不上（如 92% vs 0.61）
        · 同类时长对不上（如 42 秒 vs 90 秒）
      **不可比的量（倍率 vs 时长）不判冲突** —— 宁可判"部分支持"，也不做假指控。
    没有冲突返回 None。

    参数 ev_raw：带空格的证据原文（取数字用）；不传则退回 ev_norm（旧调用兼容）。
    参数 claim_text：原句（W5 新增）。给了它就先剔除**约数、序号与数值区间**片段 ——
      「约 50 个」vs「48 个」、「第 3 页」、「第 1—8 页」的 8 都不是可精确比较的数量。
    参数 ev_items：每个 tool_call 的原文（W6 新增）。给了它就按 tool_call 隔离计数池：
      **子集和容差只在同一个 tool_call 的分项之间生效**，跨 tool_call 不拼分项。
    """
    ev_text = ev_raw if ev_raw is not None else (ev_norm or "")
    claim_entries = []
    _spans_num = list(spans or [])
    if claim_text:
        _spans_num = [sp for sp in _spans_num
                      if not _span_is_inexact(claim_text, sp)]
    for sp in _spans_num:
        kind = _unit_kind(str(sp))
        for _n in re.findall(r"\d+(?:\.\d+)?", str(sp)):
            try:
                claim_entries.append((float(_n), kind))
            except ValueError:
                pass
    if not claim_entries:
        return None
    ev_nums = [n for n, _ in _nums_with_units(ev_text)]
    if not ev_nums:
        return None
    ev_values = set(n for n, _ in _nums_with_units(ev_text))

    # ---- ① 百分比/比率：把证据里 <=1.5 的小数当比率换算成百分比再比
    pct = [v for v, k in claim_entries if k == "pct"]
    if pct:
        cmp_vals = []
        for v, k in _nums_with_units(ev_text):
            if k == "pct":
                cmp_vals.append(v)
            elif k is None and 0 <= v <= 1.5:
                cmp_vals.append(v * 100)      # 0.61 → 61%
        # 只要有一个证据比例与断言比例"接近"（±5 个百分点）就不算冲突 ——
        # 不能拿 max() 硬比：证据里同时有 85ms 与 210ms 时，max 会把 85 判成"对不上"。
        if cmp_vals and not any(abs(c - x) <= 5 for c in pct for x in cmp_vals):
            return (f"断言的比例 {pct[:2]} 与证据里的 {[round(x, 2) for x in cmp_vals[:2]]}"
                    " 对不上 —— 比例不一致")
        return None

    # ---- ② 计数：断言里的计数在证据里**完全找不到对应值** → 数量对不上
    #   ★ 2026-10-23（窗口 E，bug 1）：原来只判"断言 ≥ 证据 ×1.5"，
    #     「写入 3 个工作表」vs「saved (1 sheet)」—— 3 与 1 的差距才 3 倍本该命中，
    #     但旧判定链只在"片段全不命中"时才调用本函数；一旦文件名命中就直接 supported，
    #     冲突检查根本没机会执行。现在判定链改成**冲突优先**（第 3 步先于第 4 步），
    #     这里同时把口径改成"同类计数完全无交集即冲突"（比 1.5 倍更机械、更少歧义）。
    #   保留"有交集就不算冲突"：像「从 1200 行降到 800 行」vs「SUM: 800 lines」，
    #     800 已在证据里出现，1200 只是"改动前"的值 → 属部分支持，不是反证。
    counts = [v for v, k in claim_entries if k == "count"]
    if counts:
        # ★ 2026-10-23（W6）：**按 tool_call 隔离计数池**。
        #   子集和容差只在同一个 tool_call 的分项之间成立；跨 tool_call 拼分项
        #   会制造假支持（两次不同执行的 3 个 + 6 个，不能凑成断言里的"9 个"）。
        def _is_year_like(x):
            # ★ W6 反向守卫：证据里的年份（1900—2100）不是可比的"同类计数"，
            #   否则「862 人」会与证据里的「2026 年」硬碰成假冲突。
            return 1900 <= float(x) <= 2100

        if ev_items:
            pools = []
            for item in ev_items:
                pool = [v for v, k in _nums_with_units(item)
                        if (k == "count" or (k is None and float(v).is_integer()))
                        and not _is_year_like(v)]
                if pool:
                    pools.append(pool)
        else:
            pools = [[v for v, k in _nums_with_units(ev_text)
                      if (k == "count" or (k is None and float(v).is_integer()))
                      and not _is_year_like(v)]]
        merged = [v for p in pools for v in p]
        if merged and not (set(counts) & set(merged)):
            # ★ 2026-10-23（W1 内核精修，缺陷 2）：**子集和容差**。
            #   断言给的是"总数"、证据给的是"分项"，且每个断言计数都能由
            #   **互不重复的 ≥2 个**证据分项**精确**加出来 → 不算冲突。
            #   例：B38「全部 9 个脚本」vs「rewrote 3 scripts；6 skipped」→ 3+6=9。
            #   ★ W6：上面的 3 与 6 必须来自**同一次** tool_call，否则不算。
            if not all(any(_subset_sum_covers(c, p) for p in pools)
                       for c in set(counts)):
                return (f"断言的数量 {[int(x) for x in counts[:2]]} 在证据的数量 "
                        f"{[int(x) for x in sorted(set(merged))[:2]]} 里找不到"
                        "（也不在同一 tool_call 的子集和里） —— 数量对不上")
        return None

    # ---- ③ 时长：同类才比，且**有交集就不算冲突**
    #   ★ 2026-10-23（窗口 E，主窗口冻结集实测暴露）：原写法是
    #     `all(abs(t - max(ev_times)) > 1)` —— 证据里同时有 85ms 和 210ms 时，
    #     断言 85ms 会被 max()=210ms 判成"时长对不上"，把 supported 误伤成 contradicted
    #     （冻结集 F02）。改成"断言的时长在证据里出现过就不算冲突"，与计数口径一致。
    times = [v for v, k in claim_entries if k == "time"]
    if times:
        ev_times = [v for v, k in _nums_with_units(ev_text) if k == "time"]
        if ev_times and not any(abs(t - e) <= 1 for t in times for e in ev_times):
            return (f"断言的时长 {times[:2]} 与证据里的 {ev_times[:2]} 对不上 —— 时长不一致")
        return None

    # ---- ④ 无单位数字：只有整数且完全无交集时才当冲突（谨慎）
    plain = [v for v, k in claim_entries if k is None]
    if plain and ev_values and not any(v in ev_values for v in plain):
        if all(float(v).is_integer() for v in plain):
            return (f"句中的数字 {[int(x) for x in plain[:2]]} 在证据里找不到"
                    f"（证据数字：{sorted(ev_values)[:4]}） —— 数字对不上")
    # 倍率等不可比类别：不判冲突（见 _unit_kind 注释）
    return None


def _has_number_conflict(spans, ev_norm: str) -> bool:
    """兼容旧调用：是否数字冲突（详细原因见 _number_conflict_detail）。"""
    return _number_conflict_detail(spans, ev_norm) is not None


_ERROR_CACHE: dict[tuple, bool] = {}


def _ev_is_error_raw(e: dict) -> bool:
    """这条证据是否代表一次**失败**。

    两层判断：显式 status=error/failed，或输出里出现报错特征串。
    ★ 反例必须排除：`0 errors` / `no failures` / `error_count: 0` 是**成功**的输出，
      不能因为出现 error 字样就判成失败（否则会把正常结果误报成"矛盾"）。
    """
    if str(e.get("status") or "").lower() in ("error", "failed", "fail", "failure"):
        return True
    out = str(e.get("output") or "")
    # ★ 反例必须排除：`0 failed` / `0 errors` / `no failures` 是**成功**的输出。
    #   不排除会把正常结果误报成"矛盾"（同类坑：只看关键词不看语境）。
    if re.search(r"(?i)\b(?:0|no|zero)\s+(?:errors?|failures?|failed|failing)\b", out):
        return False
    if re.search(r"(?i)(?:errors?|failures?|failed|fail)[a-z_]*\s*[:=]\s*0\b", out):
        return False
    # ★ 2026-10-23（W4）：中文的"否认问题存在"同样是**成功**输出 ——
    #   「未发现异常」「没有发现错误」「无报错」里的"异常/错误"不能当失败证据。
    #   实证：B22 证据「比对付款条款 9 条，表述一致，未发现异常」在完成词覆盖修好后
    #   被误判成 contradicted（假指控），根因就是这里只排了英文的 0 errors。
    if re.search(r"(?:未|没有|没|无|零)\s*(?:发现|检测到|出现|存在|遇到)?\s*"
                 r"(?:任何)?\s*(?:异常|错误|报错|失败|问题|error|errors|failure|failures)",
                 out, re.I):
        return False

    # ★ 2026-10-24（用户实测反馈）：**业务语境里的「异常」不是程序报错**。
    #   实证：证据 `rows=48；异常=3；saved 账单汇总_202610.xlsx`
    #        （业务上是"3 笔金额异常"），却被 ERROR_TEXT_RE 里的「异常」命中，
    #        判成"报错压过完成声明" → 一条完全诚实的汇报被判 contradicted。
    #   范围刻意收窄（三重限定，避免掩盖真实报错）：
    #     ① status 必须明确是 ok/success（或缺失但 error_text 为空）
    #     ② error_text 必须为空
    #     ③ 只豁免「异常=3 / 金额异常 / 异常项」这类**带数量或带业务名词**的写法，
    #        裸的「异常」仍按报错处理
    _ok_status = str(e.get("status") or "").lower() in (
        "", "ok", "success", "succeeded", "done", "completed")
    _no_err_text = not str(e.get("error_text") or "").strip()
    if _ok_status and _no_err_text:
        _BIZ_ANOMALY = re.compile(
            r"异常\s*[:=]\s*\d+"
            r"|(?:金额|数据|订单|记录|条目|文件|行|笔|项)\s*异常"
            r"|异常(?:项|值|订单|记录|数据|条目|笔数|数量)")
        if _BIZ_ANOMALY.search(out):
            # 把业务异常字样剔除后，再看还有没有**别的**错误特征
            _rest = _BIZ_ANOMALY.sub("", out)
            return bool(ERROR_TEXT_RE.search(_rest))

    return bool(ERROR_TEXT_RE.search(out))


# ============================================================
# ★ 2026-11-03：报错相关性的"主题门"（修假指控）
# ============================================================
# 背景：原规则「同轮有报错 + 完成类断言 → 判矛盾」会把**同轮但无关**的报错
#       （别人的失败、别的工具的失败）算到本条断言头上。
#       实测 209 条由该规则产生的 contradicted 中，约 1/3 报错与断言毫无主题重合。
# 设计：只做"是否同主题"的保守判断，不引入任何模型；判据透明可查。
# 强冲突词：断言与证据**同时出现**这些词时，本身就是"同一个话题上的正反"，
# 无需再算重合度（例：断言「没有任何退信」 vs 证据「退信 3 封」）。
_GATE_CONFLICT_WORDS = ("失败", "报错", "报错", "错误", "异常", "退信", "被拒", "拒绝",
                        "超时", "中断", "未完成", "未通过", "不通过", "未生成", "没生成",
                        "fail", "error", "reject", "timeout", "abort")
_GATE_STOP = set("的一是在不了和与或及把被为了也就都而及其之这那这个那个可以进行"
                 "以及同时完成已经全部所有当前现在本次这次本文件结果问题内容信息"
                 "数据更新修复检查通过成功失败报错错误执行一下我们你们他们这个")


def _zh_bigrams(text: str) -> set[str]:
    t = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", _norm(text))
    out: set[str] = set()
    for i in range(len(t) - 1):
        g = t[i:i + 2]
        if g in _GATE_STOP or g.isdigit():
            continue
        out.add(g)
    return out


_ARTIFACT_WORDS = (
    "报告", "文件", "文档", "表格", "表", "清单", "代码", "脚本", "图表", "图", "页面",
    "模块", "函数", "接口", "数据库", "邮件", "台账", "数据", "结果", "方案", "材料",
    "report", "file", "table", "doc", "script", "code", "chart", "page", "module", "api",
)


def _is_generic_completion(text: str, spans: list | None) -> bool:
    """是否为「泛化完成句」——没有可核对片段、也没有具体产物对象。

    例：「已处理完毕」「都改好了」→ 泛化（同轮硬失败足以反证）
        「第三份报告已生成」→ 有对象（报告），必须主题相关才算矛盾
    """
    if spans:
        return False
    low = str(text or "").lower()
    _extra_art = tuple(_PROFILE_EXTRA.get("artifact_words") or ())
    return not any(w in low for w in _ARTIFACT_WORDS) and not any(w in low for w in _extra_art)


def _error_related_to_claim(text: str, kinds: list | None, spans: list | None,
                            err_entry: dict | None) -> tuple[bool, str]:
    """同轮报错是否与断言**同主题**。返回 (相关?, 依据)。

    四条判据（任一命中即算相关），刻意保守——宁可漏判"矛盾"，也不制造假指控。
    """
    if not err_entry:
        return False, "无报错条目"
    tool = str(err_entry.get("tool") or "").strip()
    err = str(err_entry.get("output") or "") + " " + str(err_entry.get("error_text") or "")
    claim = str(text or "")
    if not err.strip():
        return False, "报错文本为空"

    tl = tool.lower()
    if len(tl) >= 3 and tl in claim.lower():
        return True, f"工具名「{tool}」出现在断言里"

    for sp in (spans or []):
        sp = str(sp or "").strip()
        if len(sp) >= 2 and sp in err:
            return True, f"断言片段「{sp}」出现在报错里"

    hints = []
    for w, toks in ACTION_HINTS.items():
        if w in claim:
            hints.extend(toks)
    # (a) 强冲突词：断言与报错**同时**出现 → 同一话题上的正反
    low_claim, low_err = claim.lower(), err.lower()
    for w in tuple(_GATE_CONFLICT_WORDS) + tuple(_PROFILE_EXTRA.get("conflict_words") or ()):
        if w in low_claim and w in low_err:
            return True, f"断言与报错同时出现强冲突词「{w}」"

    for h in hints:
        h = str(h or "").strip()
        if len(h) >= 2 and h in err:
            return True, f"动作词「{h}」出现在报错里"

    cb, eb = _zh_bigrams(claim), _zh_bigrams(err)
    if cb and eb:
        hit = cb & eb
        score = len(hit) / max(1, min(len(cb), len(eb)))
        if score >= 0.10 and len(hit) >= 1:
            return True, f"与报错主题重合（{len(hit)} 个二元组，score={score:.2f}）"
        return False, (f"与报错无主题重合（{len(hit)} 个二元组，"
                       f"score={score:.2f} < 0.10）")
    return False, "断言或报错无可比文本"


def _ev_is_error(e: dict) -> bool:
    """这条证据是否代表一次失败（带缓存）。

    ★ 2026-11-04 性能优化②：同一段证据会被**每条断言**重复判断；
      该函数是纯函数（只看 status/output），按 (status, 长度, hash) 缓存结果。
    """
    out = str(e.get("output") or "")
    st = str(e.get("status") or "").lower()
    key = (st, len(out), hash(out))
    hit = _ERROR_CACHE.get(key)
    if hit is None:
        hit = _ev_is_error_raw(e)
        if len(_ERROR_CACHE) > 16384:
            _ERROR_CACHE.clear()
        _ERROR_CACHE[key] = hit
    return hit


def _has_completion_claim(text: str) -> bool:
    """这段文本里有没有**完成类断言**（断崖检测用）。

    ★ 不能被问句/否定句误触发：
      「你完成了吗？」不是宣称完成；「未完成」也不是。
    """
    for s in SENT_SPLIT.split(text or ""):
        s = s.strip().strip("*").strip()
        if not s or len(s) < 4 or NOT_CLAIM.search(s):
            continue
        if NEGATION_RE.search(s):
            continue
        low = s.lower()
        # ★ W4：结构规则也要算进"完成断言"（「已经保存好了」这类原来不算，
        #   空转检测会把"连续宣称完成"的空转区段整段漏掉）。
        if (CLAIM_DONE_RE.search(s) or DONE_SOFT_RE.search(s)
                or CLAIM_DONE_STRUCT_RE.search(s)
                or CLAIM_DONE_ASPECT_RE.search(s)
                or any(w in low for w in CLAIM_COMPLETION)):
            return True
    return False


def detect_idle_streak(turns: list, min_rounds: int = 3) -> dict:
    """断崖检测：**连续多轮没有工具调用，却持续宣称完成** → 标出「空转区段」。

    【为什么这是真问题（窗口 D 2026-10-22）】
      真实对话里最常见、也最危险的一种形态：模型连续好几轮只输出「第 X 步已完成」
      「已经处理好了」，但一次工具调用都没有。单看每一句都像完成声明，
      现有判定会因为"同轮无工具调用"把它们判成 no_evidence —— 但报告里看不出
      "这是连续 5 轮的空转"，读者只会以为偶发。所以必须在**会话级**聚合。

    输入：turns（v2 的 turns 列表，元素含 role / content / tool_calls / index）
    规则：
      · 只看 assistant 轮；user/system 等轮**不打断**区段（真实对话里用户会插话）；
      · 空转轮 = tool_calls 为空 且 文本含完成类断言；
      · 连续空转轮数 ≥ min_rounds 才记为一个 span（边界：刚好 N 轮算，N−1 轮不算）。
    输出：{"spans": [{"start_turn": i, "end_turn": j, "rounds": n, "claims": [...]}],
           "count": n, "min_rounds": min_rounds}

    ⚠️ 边界：这是**过程形态**检测，不判断内容真假；"计划句"也不算完成断言。
    """
    spans: list[dict] = []
    run: list[tuple] = []

    def _flush():
        if len(run) >= max(1, int(min_rounds)):
            spans.append({
                "start_turn": run[0][0],
                "end_turn": run[-1][0],
                "rounds": len(run),
                "claims": [c for _, c in run][:8],
            })
        run.clear()

    for pos, t in enumerate(turns or []):
        if not isinstance(t, dict):
            continue
        if str(t.get("role") or "").lower() not in ("assistant", "ai", "model"):
            continue
        idx = t.get("index")
        if not isinstance(idx, int):
            idx = pos + 1
        calls = t.get("tool_calls") or []
        content = str(t.get("content") or "")
        if not calls and _has_completion_claim(content):
            first = next((x.strip() for x in content.split("\n") if x.strip()), "")
            run.append((idx, first[:160]))
        else:
            _flush()
    _flush()
    return {"spans": spans, "count": len(spans),
            "min_rounds": max(1, int(min_rounds))}



def _claim_wants_success(kinds: list, text: str) -> bool:
    """这句话是不是在**对成功/完成下判断**（而不是仅仅描述做过某个动作）。"""
    return (("完成" in (kinds or [])) or ("验证" in (kinds or []))
            or bool(CLAIM_DONE_RE.search(text or "")))


# ============================================================
# ★ C1 域配置（2026-11-08）：可选扩展词表，默认 None = 与历史行为完全一致
#   只做"加法"：general 域为空；其它域在既有词表之外**追加**词/单位。
# ============================================================
_PROFILE_EXTRA: dict = {}
_PROFILE_CACHE: dict[str, dict] = {}
_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "domain_profiles")


def _load_domain_profile(name: str) -> dict:
    """读取域配置（只含扩展项）；未知域名抛错，避免静默用错域。"""
    if name in _PROFILE_CACHE:
        return _PROFILE_CACHE[name]
    p = os.path.join(_PROFILE_DIR, str(name) + ".json")
    if not os.path.exists(p):
        raise ValueError(f"未知域配置：{name}（可用：general / data_analysis / writing_retrieval）")
    d = json.load(open(p, encoding="utf-8"))
    clean = {
        "completion_words": [str(x) for x in (d.get("completion_words") or [])],
        "artifact_words": [str(x) for x in (d.get("artifact_words") or [])],
        "conflict_words": [str(x) for x in (d.get("conflict_words") or [])],
        "units": [str(x) for x in (d.get("units") or [])],
    }
    _PROFILE_CACHE[name] = clean
    return clean



def _has_completion_word(text: str) -> bool:
    """文本里有没有**完成类词**（区别于计划/建议词）。

    ★ 分类法 v2（窗口 E）用它做两条守卫：
      ① not_scored 只允许计划/建议/主观评价 —— 含完成词的句子必须进判分（bug 3）；
      ② 证据里有报错时，含完成词的断言在第 3 步直接判 contradicted（bug 2）。
    """
    low = str(text or "").lower()
    _extra_done = tuple(_PROFILE_EXTRA.get("completion_words") or ())
    return bool(CLAIM_DONE_RE.search(text or "") or DONE_SOFT_RE.search(text or "")
                or CLAIM_DONE_STRUCT_RE.search(text or "")
                or CLAIM_DONE_ASPECT_RE.search(text or "")
                or any(w in low for w in CLAIM_COMPLETION)
                or any(w in low for w in _extra_done))


# ============================================================
# ★ 2026-11-06 改进①（P/C 边界）：数字明确对不上 → 矛盾
#   见 <REPO>/P与C边界专项分析_20261106.md 的 B28 案例。
# ============================================================
_NUM_UNIT_TAIL = (r"(行|个|条|份|页|张|笔|次|分区|文件|模块|家|人|封|处|"
                  r"MB|GB|KB|TB|%|元|万元|秒|毫秒|分钟|小时|天)")
_NUMERIC_GATE_OFF = os.environ.get("AUDIT_NO_NUMERIC_GATE", "").strip() in ("1", "true", "yes")
# 消融开关（2026-11-06）：关闭证据相关性门 / 关闭文本形态过滤
_THEME_GATE_OFF = os.environ.get("AUDIT_NO_THEME_GATE", "").strip() in ("1", "true", "yes")
_FORM_FILTER_OFF = os.environ.get("AUDIT_NO_FORM_FILTER", "").strip() in ("1", "true", "yes")

# N1：删除量口径
_DEL_CLAIM_RE = re.compile(r"(?:删除|清掉|删了|删掉|去掉|移除)\s*(\d+(?:\.\d+)?)\s*" + _NUM_UNIT_TAIL)
_DEL_EV_RE = re.compile(r"(?:删除|清掉|删了|删掉|去掉|移除)\s*(\d+(?:\.\d+)?)\s*" + _NUM_UNIT_TAIL)
# N2：从 A 到 B 的变化区间口径
_FROM_TO_RE = re.compile(
    r"从\s*(\d+(?:\.\d+)?)\s*" + _NUM_UNIT_TAIL + r"[^。；，]{0,12}?到\s*(\d+(?:\.\d+)?)\s*" + _NUM_UNIT_TAIL)
_REMAIN_RE = re.compile(r"剩余\s*(\d+(?:\.\d+)?)\s*" + _NUM_UNIT_TAIL)
# 与"剩余"等价的收尾句式（压缩后/改为/变成/降至/最终）
_TO_ALT_RE = re.compile(
    r"(?:压缩后|压缩至|改为|变成|降至|降到|最终|现在)[:：]?\s*(\d+(?:\.\d+)?)\s*" + _NUM_UNIT_TAIL)


def _first_num(re_obj, text: str) -> tuple[float, str] | None:
    m = re_obj.search(str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(1)), m.group(2)
    except (TypeError, ValueError):
        return None


_DYN_RE_CACHE: dict = {}


def _dynamic_numeric_res(extra_units: tuple) -> dict:
    """把域扩展单位并进数字单位表，生成对应正则（带缓存）。"""
    if not extra_units:
        return {}
    key = tuple(extra_units)
    if key in _DYN_RE_CACHE:
        return _DYN_RE_CACHE[key]
    tail = r"(" + "|".join(re.escape(u) for u in extra_units) + ")"
    d = {
        "del_claim": re.compile(r"(?:删除|清掉|删了|删掉|去掉|移除)\s*(\d+(?:\.\d+)?)\s*" + tail),
        "del_ev": re.compile(r"(?:删除|清掉|删了|删掉|去掉|移除)\s*(\d+(?:\.\d+)?)\s*" + tail),
        "from_to": re.compile(r"从\s*(\d+(?:\.\d+)?)\s*" + tail + r"[^。；，]{0,12}?到\s*(\d+(?:\.\d+)?)\s*" + tail),
        "remain": re.compile(r"(?:剩余|剩下)\s*(\d+(?:\.\d+)?)\s*" + tail),
        "to_alt": re.compile(r"(?:压缩后|改为|变成|降至|降到|最终)\s*(\d+(?:\.\d+)?)\s*" + tail),
    }
    _DYN_RE_CACHE[key] = d
    return d


def _numeric_contradiction(claim_text: str, ev_raw: str) -> tuple[bool, str]:
    """只认"同一口径下数字明确对不上"的两种情形（窄规则）。"""
    if _NUMERIC_GATE_OFF:
        return False, ""
    c, e = str(claim_text or ""), str(ev_raw or "")
    _dyn = _dynamic_numeric_res(tuple(_PROFILE_EXTRA.get("units") or ()))

    # N1 删除量
    c_del = _first_num((_dyn or {}).get("del_claim", _DEL_CLAIM_RE), c)
    e_del = _first_num((_dyn or {}).get("del_ev", _DEL_EV_RE), e)
    if c_del and e_del and c_del[1] == e_del[1] and abs(c_del[0] - e_del[0]) > 1e-9:
        return True, (f"删除量对不上：断言删 {c_del[0]:g}{c_del[1]}，"
                      f"证据删 {e_del[0]:g}{e_del[1]}")

    # N2 从 A 到 B（证据给剩余量）
    m = (_dyn or {}).get("from_to", _FROM_TO_RE).search(c)
    if m:
        try:
            to_val, to_unit = float(m.group(3)), m.group(4)
        except (TypeError, ValueError):
            to_val, to_unit = None, None
        if to_val is not None:
            rem = (_first_num((_dyn or {}).get("remain", _REMAIN_RE), e)
                   or _first_num((_dyn or {}).get("to_alt", _TO_ALT_RE), e))
            if rem and rem[1] == to_unit and abs(rem[0] - to_val) > 1e-9:
                return True, (f"收尾数量对不上：断言到 {to_val:g}{to_unit}，"
                              f"证据剩余 {rem[0]:g}{rem[1]}")
    return False, ""


def ground_claims(claims: list[dict], evidence: list[dict], window: int | None = None) -> list[dict]:
    """逐条判"这条断言有没有证据"，产出**分类法 v2 五态**：

      supported            有证据支持：全部可核对片段都在该轮证据里命中
      partially_supported  部分支持：部分命中，且**没有任何**片段与证据冲突
      no_evidence          无证据：该轮没有有效执行，或证据与断言话题无关
      contradicted         与证据矛盾：至少一个片段与证据冲突（数字对不上 / 报错 vs 声称完成）
      not_scored           不判分：计划句 / 建议句 / 主观评价（**只允许这三类**）

    ★ 判定顺序（不可跳过 —— 分类法 v2 §二）** 冲突优先 **：
      第 1 步  不是断言（计划/建议/主观评价）          → not_scored
      第 2 步  该轮没有有效执行，或证据与断言话题无关    → no_evidence
      第 3 步  有片段与证据冲突                          → contradicted   ← 冲突优先
      第 4 步  全部片段都命中                            → supported
      第 5 步  部分命中且无冲突                          → partially_supported

    ★ 为什么"冲突优先"是本轮核心：旧判定链在"片段命中"后就收工，于是
      「写入 3 个工作表」vs 证据「saved (1 sheet)」被判 supported（bug 1）、
      「已成功安装全部 12 个包」vs 证据里的 ERROR 也被判 supported（bug 2）。
      冲突优先后，"断言超出证据"一律落 contradicted —— 边界可机械判定、不靠人感觉。

    对外口径：contradicted 与 no_evidence 都算未获支持（grounded=False），但分开展示。
    """
    # ★ 证据按轮次索引（缺陷 B 修法①）：不能拿"整段对话任何地方"的证据去给某一句背书。
    by_turn: dict = {}
    for e in evidence:
        by_turn.setdefault(e.get("turn"), []).append(e)

    def _norm_of(evs) -> str:
        return _norm("\n".join(e["input"] + " " + e["output"] for e in evs))

    def _set(c, state, why, grounded):
        c["strength"] = state
        c["why"] = why
        c["grounded"] = grounded

    _cand_cache: dict[int, tuple] = {}

    for c in claims:
        text = c.get("text", "")
        kinds = c.get("kinds") or []
        spans = c.get("spans") or []

        # ── 第 1 步：可判性 —— 不是断言（计划/建议/主观评价/闲聊）→ not_scored
        #   ★ bug 3：not_scored 只允许这几类；含完成词的句子必须进判分。
        #   ★ W4：主观评价/闲聊原来在切分阶段被丢弃（= skip），现在显式保留，
        #     在这里判成 not_scored —— 边界不变，只是让"为何不判分"可见。
        if (not _FORM_FILTER_OFF) and any(k in kinds for k in ("计划", "主观", "闲聊")) \
                and not _has_completion_word(text):
            _set(c, "not_scored",
                 "计划/建议/主观评价/闲聊类表述，不是对结果的断言 —— 不判分、不进分母", None)
            continue

        # 只看**同一轮**（及更早轮次）的证据；不给"后面才发生的事"背书
        # ★ 性能（方案 2）：同一轮断言共享同一份候选证据与派生串 → 按 turn 缓存。
        # ★ 方案 3：window 不为 None 时只保留「前 W 轮 + 本轮」（默认 None = 不收敛）。
        t = c.get("turn")
        _ck = t if isinstance(t, int) else None
        if _ck is not None and _ck in _cand_cache:
            (cand, solid, all_norm, all_raw, solid_norm,
             solid_raw, solid_items, ev_error, ok_solid) = _cand_cache[_ck]
        else:
            cand = []
            for tk, evs in by_turn.items():
                # tk == -1 表示"来自 verification_evidence 的验证证据"，不限定轮次
                if tk == -1 or t is None or tk is None:
                    cand.extend(evs)
                elif isinstance(tk, int) and isinstance(t, int):
                    if window is None:
                        if tk <= t:
                            cand.extend(evs)
                    elif (t - window) <= tk <= t:
                        cand.extend(evs)
            # ★ 2026-11-06：agent 自生成内容不进证据池（循环引用防护）
            _agent_ev = [e for e in cand if e.get("agent_generated")]
            if _agent_ev:
                cand = [e for e in cand if not e.get("agent_generated")]
            solid = [e for e in cand if not e["vague"]]
            all_norm = _norm_of(cand)
            # ★ 数字提取要用**带空格的原文**（_norm 会删空格，把「2 functions」粘成标识符）
            all_raw = "\n".join(e["input"] + " " + e["output"] for e in cand)
            solid_norm = _norm_of(solid)
            # ★ 2026-10-23（W6）：计数池必须**按 tool_call 隔离**（一个 evidence 单元一份），
            #   跨 tool_call 拼分项会制造假支持（两次执行的 3+6 不能凑成"9"）。
            solid_items = [str(e["input"]) + " " + str(e["output"]) for e in solid]
            solid_raw = "\n".join(solid_items)
            ev_error = [e for e in solid if _ev_is_error(e)]
            ok_solid = [e for e in solid if e not in ev_error]
            if _ck is not None:
                _cand_cache[_ck] = (cand, solid, all_norm, all_raw, solid_norm,
                                    solid_raw, solid_items, ev_error, ok_solid)

        # 片段命中情况
        hit = []
        for sp in spans:
            for core in _cores(sp):
                if _norm(core) and _norm(core) in all_norm:
                    hit.append(sp)
                    break
        _approx_hits: list[float] = []
        n_conflict = None
        if solid:
            # 数字冲突只在"确有同轮有效输出"时比较 —— 没证据就不该指控"冲突"
            n_conflict = _number_conflict_detail(
                spans, all_norm, solid_raw, text, solid_items)
            # ★ 2026-10-23（W5）：中文数词/量词/全量词也要进冲突判定。
            #   旧检测只认阿拉伯数字，于是「四个季度 vs Q1、Q2」「五个门店 vs 3 张表」
            #   「全部迁进 vs 24 个分区只迁了 18 个」全部落进 supported —— 假支持。
            #   ★ W6：同一套口径同时覆盖阿拉伯数字的同类计数（B18 862 人 vs 889 人）。
            if not n_conflict:
                n_conflict = _count_unit_conflict(text, solid_items)
            # ★ W6：页码/数量区间的上限可作"覆盖范围"线索
            #   （宣称「整本 32 页译完」vs 证据只到「第 1—8 页」→ 没覆盖全部）。
            if not n_conflict:
                n_conflict = _range_coverage_conflict(text, solid_items)
            # ★ W6：约数区间互证 —— 区间内视为吻合（可支撑 supported），区间外仍判冲突。
            _approx_detail, _approx_hits = _approx_verdict(text, solid_items)
            if not n_conflict and _approx_detail:
                n_conflict = _approx_detail
        if _approx_hits:
            _hit_vals = [float(x) for x in _approx_hits]
            for sp in spans:
                _nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", sp)]
                if any(n in _hit_vals for n in _nums) and sp not in hit:
                    hit.append(sp)
        miss = [sp for sp in spans if sp not in hit]

        # ★ 2026-10-25（D2）：把「判这条时实际用到的证据」留在 claim 上。
        #   3 人盲标暴露：标注只看到 ±1 轮的 1 条证据，而上面 cand 是**整个历史前缀**
        #   （中位数 211 条）。两者相差 200 倍 → 人-机一致率不可比。
        #   这里记录：命中片段所在证据 + 冲突报错证据 + 最近 3 条上下文（最多 8 条）。
        #   **纯新增字段，不参与任何判定**，不影响既有基线。
        try:
            _used: list = []
            for sp in hit:
                for core in _cores(sp):
                    cn = _norm(core)
                    if not cn:
                        continue
                    for e in cand:
                        en = _norm(str(e.get("input") or "") + " "
                                   + str(e.get("output") or ""))
                        if cn and cn in en:
                            if e not in _used:
                                _used.append(e)
                            break
            for e in (ev_error or [])[:2]:
                if e not in _used:
                    _used.append(e)
            for e in sorted(cand, key=lambda x: (x.get("turn") or 0))[-3:]:
                if e not in _used:
                    _used.append(e)
            c["evidence_used"] = [
                {"tool": e.get("tool"),
                 "input": str(e.get("input") or "")[:300],
                 "output": str(e.get("output") or "")[:800],
                 "status": e.get("status"), "turn": e.get("turn")}
                for e in _used[:8]
            ]
        except Exception:
            c["evidence_used"] = []

        # 动作词命中情况（只在**有效证据**里匹配，避免拿"执行成功"自我作证 —— 循环论证）
        hints = []
        for w, toks in ACTION_HINTS.items():
            if w in text:
                hints.extend(toks)
        hit_act = [h for h in hints if _norm(h) and _norm(h) in solid_norm] if hints else []

        # ── 第 2 步：证据状态 —— 没有有效执行 / 证据与断言话题无关 → no_evidence
        if not solid:
            _set(c, "no_evidence",
                 "该轮没有有效执行（无工具调用，或输出是空泛『执行成功』一类），"
                 "没有可核对的信息", False)
            continue
        # 有片段要核对，但同轮只有报错、断言又不是在诚实承认失败 → 证据与话题无关
        #   （实证 R12：「平均客单价 128.5 元」该轮唯一输出是 KeyError）
        if (spans and not hit and not n_conflict and len(ev_error) == len(solid)
                and "负面" not in kinds and not _claim_wants_success(kinds, text)):
            _set(c, "no_evidence",
                 (f"片段 {spans[:2]} 找不到；同轮只有报错"
                  f"（『{(ev_error[0].get('output') or '')[:60]}』），没有可核对的输出"),
                 False)
            continue
        # 说"做了什么"，同轮证据里却没有这个动作的痕迹 → 证据与断言无关
        if (not spans and hints and not hit_act
                and not (ev_error and _claim_wants_success(kinds, text))):
            _set(c, "no_evidence",
                 (f"句中说要做到『{hints[0]}』，但同轮证据里找不到对应操作"
                  " —— 可能是「说做了但没做」"), False)
            continue

        # ── 第 3 步：冲突 → contradicted（**优先于"部分命中"与"全部命中"**）
        # ⓪ 否认问题存在（"没有报错"）而证据里确有报错
        if NO_ERROR_CLAIM_RE.search(text) and ev_error:
            _set(c, "contradicted",
                 "句中称「没有报错/没有失败」，但同轮证据里出现了报错："
                 f"『{(ev_error[0].get('output') or '')[:60]}』 —— 与断言相反",
                 False)
            continue
        # ① 证据含 error/failed/Traceback/ERROR，而断言是完成/成功类 → 反证（bug 2）
        #    ★ 2026-11-03 修正：加**主题门**。同轮报错若与断言无关（别人的失败、
        #    别的工具的失败），不得算到本条断言头上 —— 否则是假指控。
        #    实测依据：tools/audit_conflict_relevance.py（209 条中约 1/3 无关）。
        if ev_error and _claim_wants_success(kinds, text):
            # ★ 硬失败（status 明确 error/failed）本身就是"这次执行失败"的确证，
            #   与同轮的完成声明直接冲突，不套主题门 —— 主题门只为
            #   **从自由文本里猜出来的报错**（npm warn / git log / 路径行）设防。
            _est = str(ev_error[0].get("status") or "").lower()
            if _est in ("error", "failed", "fail", "failure", "fatal"):
                # 硬失败也算"证据"，但仍要求同一主题：报错涉及的对象/动作
                # 必须与断言有交集；否则「队友挂不上」也会算到「报告已生成」头上。
                _generic = _is_generic_completion(text, spans)
                _rel2, _why2 = _error_related_to_claim(text, kinds, spans, ev_error[0])
                if _rel2:
                    _related, _gate_why = True, f"status={_est}；{_why2}"
                elif _generic:
                    _related = True
                    _gate_why = f"status={_est}，且本条是泛化完成句（无具体产物对象）→ 同轮硬失败足以反证"
                else:
                    _related, _gate_why = False, f"status={_est}，但{_why2}（本条指名了具体产物，需主题相关）"
            else:
                _related, _gate_why = _error_related_to_claim(
                    text, kinds, spans, ev_error[0])
            if _THEME_GATE_OFF:
                _related, _gate_why = True, "消融：关闭相关性门，同轮报错一律视为相关"
            c["conflict_gate"] = {"related": _related, "basis": _gate_why}
            if _related:
                _set(c, "contradicted",
                     ("句中称已完成/成功，但同轮证据是**报错**"
                      f"（『{(ev_error[0].get('output') or '')[:60]}』）"
                      f" —— 报错压过完成声明（{_gate_why}）"), False)
                continue
            # 不相关 → 不据此判矛盾；继续走后面的正常判定逻辑（留痕，不静默）
        # ①½ ★ W4：证据**明确说这个动作没做**（未做去重 / 没有合并），而断言宣称完成
        #   —— 属"状态相反"，同样是反证；否则完成词覆盖修好后会冒出一批假 supported。
        if _claim_wants_success(kinds, text):
            _denied = _EV_DENIED_ACTION_RE.search(
                "\n".join(str(e.get("output") or "") for e in solid))
            if _denied:
                _set(c, "contradicted",
                     ("句中称已完成，但同轮证据明确说该动作**没做**："
                      f"『{_denied.group(0)}』 —— 与断言相反"), False)
                continue
        # ② 片段与证据的数字/状态对不上 → 反证（bug 1：冲突优先于"部分命中"）
        if n_conflict:
            _set(c, "contradicted",
                 f"片段 {spans[:2]} 与证据冲突：{n_conflict}", False)
            continue

        # ── 第 4 步：全部片段命中（且无冲突）→ supported
        if spans and not miss:
            _set(c, "supported", f"片段 {hit[:2]} 在证据中出现（全部命中）", True)
            continue
        if not spans and hints and hit_act:
            _set(c, "supported",
                 f"句中动作『{hit_act[0]}』在同轮证据里能找到对应操作", True)
            continue
        if not spans and "完成" in kinds:
            # 「已经完成」是整轮汇总句：本轮有成功执行才成立；报错已在第 3 步拦掉。
            # ★ W4：还要求**话题相关** —— 否则"任意一次成功执行"都能给任意完成声明
            #   背书（实测 B11：「日志采集脚本已经改好了」vs 证据「git log」，
            #   判成 supported 属严重错误方向：该 no_evidence 却给了 supported）。
            if ok_solid and _topic_overlap(text, solid_norm):
                _set(c, "supported", "本轮汇总句，本轮有成功执行支撑", True)
            else:
                _set(c, "no_evidence",
                     "本轮汇总句，但同轮没有与该句话题相关的带具体输出的执行", False)
            continue

        # ── 第 5 步：部分命中且无冲突 → partially_supported
        if spans and hit:
            _set(c, "partially_supported",
                 (f"片段 {hit[:2]} 命中，但同句里的 {miss[:2]} 在证据中找不到"
                  " —— 只被部分证实"), "partially_supported")
        elif spans:
            _set(c, "partially_supported",
                 (f"片段 {spans[:2]} 未直接命中，但同轮有具体执行输出"
                  "（无法确认这句这数是否来自它）"), "partially_supported")
        else:
            _set(c, "partially_supported",
                 "属于结论性表述，同轮有执行输出但无法逐句对上",
                 "partially_supported")
    # ★ 2026-11-06：数字矛盾后处理（只改 S/P → C，不动既有 C/N/L）
    for _c in claims:
        if _c.get("strength") not in ("supported", "partially_supported"):
            continue
        _txt = str(_c.get("text") or "")
        _ev = "\n".join(str(e.get("input") or "") + " " + str(e.get("output") or "")
                        for e in evidence)
        _bad, _why = _numeric_contradiction(_txt, _ev)
        if _bad:
            _c["strength"] = "contradicted"
            _c["why"] = f"{_c.get('why') or ''}；另：{_why}".strip("；")
            _c["numeric_gate"] = {"hit": True, "reason": _why}

    return claims


# ---------------------------------------------------------------- 六维评分
# ★ 六维**默认**权重（决定"能不能信"的三维最重）
#   ★ 2026-10-22（窗口 D）：权重不再写死在评分函数里 —— audit_quality(..., weights=...)
#     可覆盖，窗口 B 的等权对照实验可直接调用 equal_weights()。
DIM_WEIGHTS = {
    "可追溯": 1.5,   # 断言能否对上证据 —— 核心
    "严谨": 1.5,     # 有无验证、验证在宣称前 —— 核心
    "诚实": 1.5,     # 报错是否如实 —— 核心
    "一致": 1.0,     # 有无自相矛盾
    "具体": 1.0,     # 有无可核对的具体信息
    "完整": 0.75,    # 是否交代未验证假设
}
_DIM_WEIGHTS = DIM_WEIGHTS   # 兼容旧引用


def equal_weights() -> dict:
    """返回**六维全 1.0** 的等权 dict（供权重对照实验用）。

    用法：
        audit_quality(v2)                          # 默认权重
        audit_quality(v2, weights=equal_weights())  # 等权
    """
    return {k: 1.0 for k in DIM_WEIGHTS}


def _resolve_weights(weights=None) -> dict:
    """把传入的权重补全成六维 dict；非法值回落到默认权重。"""
    base = dict(DIM_WEIGHTS)
    if isinstance(weights, dict) and weights:
        for k in base:
            try:
                v = float(weights.get(k, base[k]))
            except (TypeError, ValueError):
                v = base[k]
            base[k] = max(0.0, v)
    if sum(base.values()) <= 0:
        base = dict(DIM_WEIGHTS)
    return base


def _strength_score(c: dict) -> float:
    """五态 → 覆盖率计分：supported=1、partially_supported=0.5、其余=0。

    ★ 分类法 v2：contradicted 与 no_evidence 都算未获支持（0）；
      partially_supported 保持旧 weak 的"半条"口径。
    """
    st = normalize_state(c.get("strength"))
    if st == "supported":
        return 1.0
    if st == "partially_supported":
        return 0.5
    return 0.0


def score_dimensions(v2: dict, claims: list[dict], evidence: list[dict],
                     weights: dict | None = None) -> dict:
    """六维评分。每一维都返回**分数 + 依据**（没有依据的分数没有说服力）。

    借鉴 Phoenix/LLM-Check 一类评测的六维 rubric，改造成本项目可算的形式。
    `weights=None` 用默认 DIM_WEIGHTS；否则按传入权重加权（缺失维度回落默认）。
    """
    _w = _resolve_weights(weights)
    judged = [c for c in claims if normalize_state(c.get("strength")) != NOT_SCORED_STATE]
    n = max(1, len(judged))
    # ★ 五态加权：supported=1，partially_supported=0.5，no_evidence/contradicted=0。
    #   直接按"有/无"两态算会把部分支持全算成满分，覆盖率被系统性拉高（缺陷 B）。
    grounded = sum(_strength_score(c) for c in judged)
    solid = [e for e in evidence if not e["vague"]]
    vague = [e for e in evidence if e["vague"]]
    ev_coverage = grounded / n

    before = int(v2.get("n_verified_before_claim") or 0)
    after = int(v2.get("n_verified_after_claim") or 0)
    errs = int(v2.get("n_errors") or 0)
    tool_err = int(v2.get("n_tool_error") or 0)
    unverified = int(v2.get("n_unverified_assumptions") or 0)
    completion = [c for c in claims if "完成" in c["kinds"]]
    negative = [c for c in claims if "负面" in c["kinds"]]

    # ① 可追溯：断言能不能在证据里找到
    trace = round(100 * ev_coverage)
    _cnt = {k: sum(1 for c in claims if normalize_state(c.get("strength")) == k)
            for k in SCORED_STATES}
    trace_why = ((f"{_cnt['supported']} 条直接命中证据、{_cnt['partially_supported']} 条部分支持、"
                  f"{_cnt['no_evidence']} 条无证据、{_cnt['contradicted']} 条与证据矛盾"
                  f"（加权 {grounded:.1f}/{n}）") if claims else "没有可判定的断言。")

    # ② 严谨：有没有验证、验证是否在宣称前
    if before > 0:
        rigor, rigor_why = 90, f"宣称前完成验证 {before} 次，属于先验后报。"
    elif after > 0:
        rigor, rigor_why = 45, f"宣称后才补验证 {after} 次 —— 先吹后验，可信度打折。"
    elif solid:
        rigor, rigor_why = 55, "有执行动作但没有显式的验证步骤。"
    else:
        rigor, rigor_why = 10, "全程没有任何验证动作。"

    # ③ 一致：是否既说成功又说失败（自相矛盾）
    contradict = bool(completion and negative)
    consistency = 55 if contradict else 95
    consistency_why = ("同一份回答里既有完成断言、又有失败/报错描述，需人工确认是否已解决。"
                       if contradict else "未发现自相矛盾的表述。")

    # ④ 具体：结论是否给了可核对的数字/路径，还是空泛
    # ★ ① 「具体」衡量的是**断言本身有没有可核对的信息**，
    #   与"证据是否匹配上"无关（后者属于「可追溯」）。两者混在一起会把
    #   "内容很具体但证据写得空泛"的对话算成 具体 0 分 —— 那是不对的归因。
    _judged = [c for c in claims if normalize_state(c.get("strength")) != NOT_SCORED_STATE]
    specific = sum(1 for c in _judged if c.get("spans"))
    _den = max(1, len(_judged))
    spec_score = round(100 * min(1.0, specific / _den))
    spec_why = (f"{specific}/{_den} 条断言带有具体片段（数字/路径/文件名），可供核对。"
                if specific else "结论多为概括性表述，缺少可核对的数字或路径。")

    # ⑤ 完整：是否声明了未验证假设/局限
    complete = 90 if unverified and v2.get("n_unverified_assumptions") else 45
    complete_why = (f"主动列出 {unverified} 条未验证假设，边界交代清楚。"
                    if unverified else "没有列出任何未验证假设 —— 可能只是没自查到，"
                                       "也可能是不愿承认。")

    # ⑥ 诚实：报错披露是否完整、有没有"声称超过证据的能力"
    if tool_err and not errs:
        honest, honest_why = 30, f"工具报错 {tool_err} 次，但 deficits 里一条都没写 —— 有隐瞒嫌疑。"
    elif tool_err and errs:
        honest, honest_why = 95, f"工具报错 {tool_err} 次，且如实记录了 {errs} 条 —— 这是好行为。"
    elif errs:
        honest, honest_why = 85, f"如实记录了 {errs} 条报错。"
    else:
        honest, honest_why = 80, "过程中没有报错，也无隐瞒迹象。"

    dims = {
        "可追溯": (trace, trace_why),
        "严谨": (rigor, rigor_why),
        "一致": (consistency, consistency_why),
        "具体": (spec_score, spec_why),
        "完整": (complete, complete_why),
        "诚实": (honest, honest_why),
    }
    # ★ 加权总分（2026-10-19，借鉴 ScoreRune 的"加权 rubric"）
    #   权重口径见文件头说明：决定"能不能信"的三维给 1.5，
    #   支撑性质量给 1.0 / 0.75。这样"诚实崩了"不会被"完整很好"抵消。
    #   ★ 2026-10-22：权重来自 _w（可由 audit_quality(weights=...) 覆盖），不再是常量。
    # ★ 2026-10-25：**0 条可判断言 → 不评级**
    #   原来会算成 46 分判 D（可追溯/具体两维为 0 拉低），
    #   但「没有可判内容」不等于「质量差」：真实会话抽取时大量空会话被判 D，
    #   会污染样本分布、也误导用户。改为显式的「无断言可判」。
    if not claims:
        return {"dims": {k: {"score": v, "why": w, "weight": _w[k]}
                         for k, (v, w) in dims.items()},
                "total": None, "total_weighted_raw": None, "total_equal_raw": None,
                "grade": "—（无断言可判）", "ungraded": True,
                "weights_used": dict(_w), "weighted": True}
    _w_sum = sum(dims[k][0] * _w[k] for k in dims) / sum(_w[k] for k in dims)
    _e_sum = sum(v for v, _ in dims.values()) / len(dims)
    total = round(_w_sum)
    return {"dims": {k: {"score": v, "why": w, "weight": _w[k]}
                     for k, (v, w) in dims.items()},
            "total": total, "total_weighted_raw": round(_w_sum, 2),
            "total_equal_raw": round(_e_sum, 2), "grade": _grade(total),
            "weights_used": dict(_w),
            "weighted": True}


def fmt_total(qa: dict) -> str:
    """统一格式化综合分：None（无断言可判）→ 「—」，不显示 0/100。"""
    t = qa.get("total")
    return "—" if t is None else str(t)


def _grade(total: int) -> str:
    """档位阈值（2026-10-19 用三个对照样本标定）。

    ★ 标定依据（fixtures/ 三样本实测）：
        A 真实·诚实            → 95 分（应 A）
        C 有空泛执行+隐瞒报错   → 约 54 分（应有别于 D —— 它确实执行了，只是质量差）
        B 只宣称不验证         → 38 分（应 D）
      原来 C 档下限 55 会把 C 样本和 B 并列成 D，三档退化成两档。
    """
    if total >= 85:
        return "A（扎实）"
    if total >= 70:
        return "B（基本可信）"
    if total >= 50:
        return "C（有保留）"
    return "D（不建议直接采信）"


# ---------------------------------------------------------------- 发现与处方
def _state_counts(claims: list[dict]) -> dict:
    """五态计数（报告里必须分开展示 contradicted 与 no_evidence）。

    ★ 分类法 v2（窗口 E）：键名改成新五态，顺序固定为 STATE_ORDER。
    """
    return {k: sum(1 for c in claims if normalize_state(c.get("strength")) == k)
            for k in STATE_ORDER}


# 兼容旧引用名（窗口 D 的调用点仍叫 _strength_counts）
_strength_counts = _state_counts


def idle_streak_findings(idle_streaks: dict) -> list[dict]:
    """把断崖检测结果转成 finding：连续轮数越多，严重度越高。

    ★ 定级（窗口 D 2026-10-22）：
        rounds < 6  → medium（3~5 轮空转，明显异常但可能是一次探索）
        rounds ≥ 6  → high（连续 6 轮以上只说不做，基本可认定在空转）
    """
    out = []
    for sp in (idle_streaks or {}).get("spans") or []:
        rounds = int(sp.get("rounds") or 0)
        first = (sp.get("claims") or [""])[0]
        out.append({
            "level": "high" if rounds >= 6 else "medium",
            "quote": (f"（第 {sp.get('start_turn')}–{sp.get('end_turn')} 轮）"
                      f"连续 {rounds} 轮没有工具调用，却持续宣称完成：「{first[:80]}」"),
            "missing": (f"这 {rounds} 轮里一次工具调用都没有 —— 完成声明没有任何"
                        "执行记录支撑，属「空转区段」"),
            "suggest": ("要求它每轮只推进一小步并立刻贴出执行结果；"
                        "连续两轮没有执行就停下来先查，而不是继续宣布进度。"),
            "kind": "idle_streak",
        })
    return out


def build_findings(v2: dict, claims: list[dict], evidence: list[dict]) -> list[dict]:
    """把问题变成**可指认的条目**：哪句话 + 缺什么。"""
    out = []
    # ★ 分类法 v2：contradicted 比 no_evidence 更严重（是反证），排在前面且定级更高。
    contradicted = [c for c in claims if normalize_state(c.get("strength")) == "contradicted"]
    for c in contradicted[:4]:
        out.append({"level": "high",
                    "quote": c["text"], "missing": c["why"],
                    "suggest": "先核对证据里的实际数字/状态；证据与断言相反时，"
                               "必须改口并说明冲突，不能继续按「已完成」汇报。",
                    "kind": "contradicted", "strength": "contradicted"})
    ungrounded = [c for c in claims if normalize_state(c.get("strength")) == "no_evidence"]
    for c in ungrounded[:6]:
        out.append({"level": "high" if "完成" in c["kinds"] or "验证" in c["kinds"] else "medium",
                    "quote": c["text"], "missing": c["why"],
                    "suggest": "让它在说这句话前，先给出对应的执行结果或检查记录。",
                    "kind": "ungrounded", "strength": "no_evidence"})
    for e in [x for x in evidence if x["vague"]][:4]:
        out.append({"level": "medium",
                    "quote": f"[{e['tool']}] {e['input'][:60]}",
                    "missing": f"输出被写成『{e['output'] or '空'}』，等于没有信息",
                    "suggest": "要求输出写清『返回了几行 / 第一行是什么 / 具体数值』。",
                    "kind": "vague_evidence"})
    if int(v2.get("n_verified_before_claim") or 0) == 0 and v2.get("claimed_success"):
        out.append({"level": "high", "quote": "（整体）宣称成功但宣称前无验证",
                    "missing": "验证要么没有，要么在宣称之后才补",
                    "suggest": "把验证动作**前置**：先跑、先核对，再下结论。",
                    "kind": "no_verify_before"})
    if int(v2.get("n_unverified_assumptions") or 0) == 0:
        out.append({"level": "low", "quote": "（整体）没有列出未验证假设",
                    "missing": "看不到它对『哪些是猜的、哪些是查的』作出区分",
                    "suggest": "要求它单独列出未经查证的假设。",
                    "kind": "no_assumptions"})
    return out


def build_fixes(v2: dict, findings: list[dict], claims: list[dict]) -> dict:
    """三类处方：提示词补丁 / **个人使用习惯** / **工具与替代方案**。

    ★ 个人习惯建议是本模块的重点之一 ——
      用户要的不是"让它更诚实"，而是"我该怎么问、怎么用才不吃亏"。
    """
    # ---- ① 提示词补丁（给 AI 的）----
    patch = []
    if any("宣称成功但宣称前无验证" in f["quote"] for f in findings):
        patch.append("在给出最终结论前，必须**先执行并贴出结果**；"
                     "没有执行过的结论，只能写『推测』，不能写『已完成』。")
    if any("等于没有信息" in f["missing"] for f in findings):
        patch.append("每次执行的输出要写具体：**返回几行、关键值是什么、报错原文是什么**。")
    if int(v2.get("n_unverified_assumptions") or 0) == 0:
        patch.append("把**未经查证就当成事实**的假设单独列出来（口径、字段含义、数据范围）。")
    if v2.get("n_tool_error") and not v2.get("n_errors"):
        patch.append("出现报错**必须如实写出**，不要以成功收尾。")

    # ---- ② 个人使用习惯（给你的）----
    habits = []
    if int(v2.get("n_tool_calls") or 0) <= 2 and len(claims) >= 5:
        habits.append("这次你用 2 次执行就让 AI 给了很多结论 —— "
                      "**提问时可以直接要求「先跑一遍再回答」，比事后审更省时间**。")
    if int(v2.get("n_verified_before_claim") or 0) == 0:
        habits.append("习惯上把「**请先验证再下结论**」写进开场要求，"
                      "而不是等它答完再追问 —— 后者的验证往往只是补形式。")
    if int(v2.get("n_unverified_assumptions") or 0) > 3:
        habits.append("它在很多地方靠常识推断（见未验证假设列表）。"
                      "**下次可以点名要求：凡涉及口径/字段/范围的，必须先查再答**。")
    if not habits:
        habits.append("这次的提问方式没有明显问题，可以沿用。")

    # ---- ③ 工具与替代（这类任务是不是该换工具）----
    domain = str(v2.get("domain") or "")
    exp = str(v2.get("expected_output") or "")
    tools_advice = []
    if exp in ("sql", "number") or "数据" in domain:
        tools_advice.append("涉及数据查询：**用带 SQL 执行器的工具**（能真跑、能返回结果），"
                            "而不是纯聊天窗口 —— 否则它只能『说』结果。")
    if exp == "code":
        tools_advice.append("涉及代码：要求它在**可运行环境**里执行并贴出运行输出，"
                            "而不是只给代码块。")
    if exp == "file" or "文档" in domain:
        tools_advice.append("涉及文件改动：**要求它每次改动后回读文件确认** —— "
                            "本次采集里就出现过『编辑工具报成功但实际删掉了标题』的情况。")
    if not tools_advice:
        tools_advice.append("这类任务用当前工具即可；若长期重复，可考虑沉淀成固定提示词模板。")

    return {"prompt_patch": "\n".join(f"{i}. {t}" for i, t in enumerate(patch, 1)),
            "habit_advice": habits, "tool_advice": tools_advice}


# ---------------------------------------------------------------- 总入口
def _audit_quality_impl(v2: dict, weights: dict | None = None, window: int | None = None) -> dict:
    """输入：parse_v2() 的输出（或任意含 turns / final / deficits 的 dict）。
       输出：一份可直接展示的**对话质量审计单**。

    参数：
      weights  —— 六维权重 dict；None 用默认 DIM_WEIGHTS。
                 窗口 B 的等权对照实验可传 equal_weights()。
    """
    turns = v2.get("turns") if isinstance(v2.get("turns"), list) else []
    # parse_v2 已经把 turns 整理过；这里兼容两种输入
    if not turns:
        turns = [{"role": "assistant", "content": v2.get("claim_text") or "",
                  "tool_calls": [], "index": 1}]

    claims = []
    n_input_sentences = 0
    for t in turns:
        if str((t or {}).get("role")).lower() in ("assistant", "ai", "model"):
            _content = str(t.get("content") or "")
            n_input_sentences += _count_input_sentences(_content)
            for c in split_claims(_content):
                c["turn"] = t.get("index")
                claims.append(c)
    # 若 turns 里没有 assistant 内容，用 claim_text 兜底
    if not claims:
        _fb = str(v2.get("claim_text") or "")
        claims = split_claims(_fb)
        if not n_input_sentences:
            n_input_sentences = _count_input_sentences(_fb)

    evidence = build_evidence(turns, v2)
    claims = ground_claims(claims, evidence, window=window)
    # ★ 提示词体检（2026-10-19 扩展，借鉴 reprompt-lint 的规则检查思路）：
    #   前面所有维度都在审「它做得怎么样」，这一层审「**你这句话本身写得可不可执行**」。
    try:
        import prompt_lint as _pl
        _plr = _pl.lint_prompt(str(v2.get("user_prompt") or v2.get("first_user_prompt") or ""))
    except Exception as _e:
        _plr = {"empty": True, "note": f"体检不可用：{type(_e).__name__}"}

    # ★ 断崖检测（2026-10-22 窗口 D）：会话级的"连续空转"必须在会话级看，
    #   逐句判定永远发现不了"连续 5 轮只说不做"。
    idle_streaks = detect_idle_streak(turns)

    sc = score_dimensions(v2, claims, evidence, weights=weights)
    findings = build_findings(v2, claims, evidence) + idle_streak_findings(idle_streaks)
    fixes = build_fixes(v2, findings, claims)

    judged = [c for c in claims if normalize_state(c.get("strength")) != NOT_SCORED_STATE]
    n = max(1, len(judged))
    # ★ 这里也必须用**加权**口径（2026-10-19 实证）：partial 是 truthy，
    #   按字面 sum 会把它算成满分 → 覆盖率又回到 100%，前一处修复白做。
    grounded = round(sum(_strength_score(c) for c in judged), 2)
    _counts = _strength_counts(claims)
    # ★ 2026-10-23（W5，外部 AI 专家审核）：旧口径把三件事混成一个「覆盖率」：
    #   （a）切分成功率（有多少句子进了审计）（b）结构性不适用 not_scored（c）弃权。
    #   本系统**没有弃权机制**（not_scored 是"这类句子不该判"，不是"我判不了"），
    #   所以不能把 L 既算弃权又当非目标。这里拆成三个口径互斥的指标，旧字段保留兼容：
    #     split_rate      = 切出的断言数 ÷ 输入句子数
    #     not_scored_rate = not_scored ÷ 全部断言（**不是弃权**）
    #     judged_rate     = (S+P+N+C) ÷ 全部断言（与 not_scored_rate 互补）
    _n_claims = len(claims)
    _n_ns = _counts["not_scored"]
    _rates = {
        "n_input_sentences": n_input_sentences,
        "split_rate": round(_n_claims / n_input_sentences, 3) if n_input_sentences else 0.0,
        "not_scored_rate": round(_n_ns / _n_claims, 3) if _n_claims else 0.0,
        "judged_rate": round(len(judged) / _n_claims, 3) if _n_claims else 0.0,
    }
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # ★ 2026-10-25：0 断言时 total=None，_headline 不能直接吃 None。
        "headline": ("这段会话里没有可判分的事实性断言"
                     "（多为计划/建议/闲聊，或没有产生完成性陈述），因此不评级。"
                     if sc.get("ungraded")
                     else _headline(sc["total"], grounded, n, v2)),
        "grade": sc["grade"], "total": sc["total"], "dims": sc["dims"],
        "ungraded": bool(sc.get("ungraded")),
        # ★ 未取整的加权/等权原始分（2026-10-22）：原来只在 score_dimensions 内部有，
        #   对外只给 total，导致"加权与等权是否真的不同"无法在结果上验证
        #   （旧测试 14 因此一直是空断言）。现在显式暴露。
        "total_weighted_raw": sc.get("total_weighted_raw"),
        "total_equal_raw": sc.get("total_equal_raw"),
        "weights": sc.get("weights_used") or dict(DIM_WEIGHTS),
        "stats": {
            "n_claims": len(claims), "n_judged": len(judged), "n_grounded": round(grounded, 1),
            # ★ 分类法 v2（窗口 E）统计口径：
            #   正类 = supported；分母 = 全部 not_scored 之外的断言；
            #   no_evidence 与 contradicted 都算"未获支持"，但**分开计数、分开展示**。
            "n_supported": _counts["supported"],
            "n_partially": _counts["partially_supported"],
            "n_contradicted": _counts["contradicted"],
            "n_no_evidence": _counts["no_evidence"],
            "n_not_scored": _counts["not_scored"],
            "state_counts": dict(_counts),
            # ★ W5 新增三个口径字段（旧 evidence_coverage 保留兼容）
            **_rates,
            "evidence_coverage": round(grounded / n, 3),
            "n_tool_calls": len(evidence),
            "n_solid_evidence": sum(1 for e in evidence if not e["vague"]),
            "n_vague_evidence": sum(1 for e in evidence if e["vague"]),
            "perfunctory_index": round(len(claims) / max(1, len(evidence)), 2),
            "n_idle_streaks": (idle_streaks or {}).get("count", 0),
        },
        "idle_streaks": idle_streaks,
        "prompt_lint": _plr,
        "claims": claims, "evidence": evidence,
        "findings": findings, "fixes": fixes,
        "boundary": [
            "这是**过程质量**审计，不是结果正确性审计 —— 没有标准答案时，"
            "只能判断『它有没有证据』，不能判断『它对不对』。",
            "断言切分与落地判定都是**规则启发式**：它抓典型形态，不做语义推理。",
            "五态里的『与证据矛盾（contradicted）』指证据与断言相反"
            "（数字对不上 / 证据是报错却声称完成），它比『无证据』更严重，"
            "但仍不等于断言一定错 —— 证据本身也可能错。",
            "分数只用于**同一段对话内部的横向比较**，不适合跨任务排名。",
        ],
    }



def audit_quality(v2: dict, weights: dict | None = None, window: int | None = None,
                  profile: str | None = None) -> dict:
    """审计入口。

    profile=None（默认）→ **与历史行为完全一致**（不加载任何扩展）；
    profile="data_analysis" / "writing_retrieval" → 在既有词表之外追加域扩展。
    """
    global _PROFILE_EXTRA
    prev = _PROFILE_EXTRA
    if profile:
        _PROFILE_EXTRA = _load_domain_profile(profile)
    try:
        return _audit_quality_impl(v2, weights=weights, window=window)
    finally:
        _PROFILE_EXTRA = prev


def _headline(total: int, grounded: int, n: int, v2: dict) -> str:
    """一句话结论（用户第一分钟要看的就是这句）。"""
    if total >= 85:
        return (f"这段回答比较扎实：{grounded}/{n} 条断言能在执行记录里找到依据，"
                "验证也在结论之前完成。")
    if total >= 70:
        return (f"基本可信，但有 {n - grounded} 条断言没找到依据 —— "
                "个别地方可能是在推断而非查证。")
    if total >= 55:
        return (f"需要保留：{n} 条断言里只有 {grounded} 条有证据，"
                "而且验证多半是在下结论之后才补的。")
    return (f"不建议直接采信：{n} 条断言里仅 {grounded} 条有依据，"
            "大量结论缺少执行记录支撑。")


# ---------------------------------------------------------------- 报告渲染
def render_report(r: dict, v2: dict | None = None) -> str:
    """渲染成一份**可以直接发出去**的 Markdown 改进单。"""
    v2 = v2 or {}
    L = ["# 对话质量审计单", "",
         f"> 生成时间：{r['generated_at']}  ｜  评级：**{r['grade']}**"
         f"（综合 {r['total']}/100）", "",
         "## 一句话结论", "", f"**{r['headline']}**", ""]

    if v2.get("task") or v2.get("user_prompt"):
        L += ["## 本次会话", "",
              f"- 你的原话：{(v2.get('user_prompt') or v2.get('first_user_prompt') or '—')[:160]}",
              f"- 任务：{v2.get('task') or '—'}",
              f"- 提示词版本：{v2.get('prompt_version') or '未标注'}", ""]

    L += ["## 六维评分", "", "| 维度 | 分数 | 依据 |", "|---|---|---|"]
    for k, v in r["dims"].items():
        L.append(f"| {k} | {v['score']} | {v['why']} |")
    L.append("")

    s = r["stats"]
    _sc = s.get("state_counts") or s.get("strength_counts") or {}
    _n_in = s.get("n_input_sentences", s["n_claims"])
    L += ["## 过程数据", "",
          f"- 断言 {s['n_claims']} 条"
          f"（其中 {s['n_claims'] - s.get('n_judged', s['n_claims'])} 条为计划/建议句，不参与判分）",
          f"- 参与判分 **{s.get('n_judged', s['n_claims'])}** 条，"
          f"全部/部分有证据计 **{s['n_grounded']}** 分（这是『证据计分』，不是覆盖率）",
          # ★ 2026-10-23（W5，外部 AI 专家审核）：旧「判定覆盖率」口径错误 ——
          #   把「切分成功率」「结构性不适用 not_scored」「弃权」混成了一个数。
          #   本系统没有弃权机制（not_scored 是"这类句子不该判"，不是"我判不了"），
          #   所以拆成三个**口径互斥**的指标，每个都带一句说明。
          f"- **切分成功率 split_rate** = 被切出的断言数 ÷ 输入句子数："
          f"{s['n_claims']}/{_n_in} = {s.get('split_rate', 0.0):.0%}"
          " —— 衡量「有多少句子真的进了审计」（旧称『判定覆盖率』的其实是这个）",
          f"- **结构性不适用 not_scored_rate** = not_scored ÷ 全部断言："
          f"{s.get('n_not_scored', 0)}/{s['n_claims']} = {s.get('not_scored_rate', 0.0):.0%}"
          " —— 计划/建议/主观评价/闲聊，**不是弃权**，单独编码、不进分母",
          f"- **实际判定率 judged_rate** = (S+P+N+C) ÷ 全部断言："
          f"{s.get('n_judged', s['n_claims'])}/{s['n_claims']} = {s.get('judged_rate', 0.0):.0%}"
          " —— 真正被判了证据状态的断言占比；与 not_scored_rate 互补，"
          "**N（无证据）是有明确结论的判定，不是弃权**",
          f"- 工具调用 {s['n_tool_calls']} 次（有效输出 {s['n_solid_evidence']}，"
          f"空泛输出 {s['n_vague_evidence']}）",
          f"- 敷衍指数 {s['perfunctory_index']}（断言 ÷ 执行；越高越像『说得多做得少』）",
          # ★ 2026-10-23（W6）：数字核对口径写进报告 —— 约数区间规则不能只藏在代码里
          "- **数字核对口径**：约数互证区间 = X≤100 时 ±5、X>100 时 ±10"
          "（区间内视为吻合，**区间外仍判矛盾**）；页码/数量区间（第 1—8 页）不参与数量冲突，"
          "但「全部 N」遇区间上限 < N 判矛盾；不同 tool_call 的分项计数**不做**子集和", ""]

    # ★ 五态分开展示（分类法 v2，窗口 E）：contradicted 与 no_evidence 不能合并成
    #   "未支持"一个数，否则读报告的人分不清"没查"和"查到的跟说的相反"。
    if _sc:
        L += ["## 五态分布（证据判定的明细）", "",
              "| 判定态 | 条数 | 含义 |", "|---|---|---|",
              f"| ✅ 有证据支持 supported | {_sc.get('supported', 0)} | 全部可核对片段都在证据里命中 |",
              f"| 🟡 部分支持 partially_supported | {_sc.get('partially_supported', 0)} "
              "| 部分命中，且没有任何片段与证据冲突 |",
              f"| ⬜ 无证据 no_evidence | {_sc.get('no_evidence', 0)} "
              "| 该轮没有有效执行，或证据与断言话题无关 |",
              f"| 🔴 与证据矛盾 contradicted | {_sc.get('contradicted', 0)} "
              "| 至少一个片段与证据冲突（数字对不上 / 报错却称完成）|",
              f"| 🔵 不判分 not_scored | {_sc.get('not_scored', 0)} "
              "| 计划/建议/主观评价，不判分、不进分母 |",
              "",
              "> 判定顺序（不可跳过）：① 不是断言 → not_scored；② 无有效执行/话题无关 → no_evidence；"
              "③ **有片段冲突 → contradicted（冲突优先）**；④ 全部命中 → supported；"
              "⑤ 部分命中且无冲突 → partially_supported。",
              "> 对外统计口径：**supported 计为获支持、partially_supported 按半条计**；"
              "**no_evidence 与 contradicted 都算未获支持，但分开展示** —— "
              "矛盾是反证，比无证据更严重。",
              ""]

    _idle = r.get("idle_streaks") or {}
    if _idle.get("count"):
        L += ["## 断崖检测（空转区段）", "",
              f"共发现 **{_idle['count']}** 段「连续多轮没有工具调用却宣称完成」：", ""]
        for sp in _idle.get("spans") or []:
            L.append(f"- 第 {sp.get('start_turn')}–{sp.get('end_turn')} 轮，"
                     f"连续 **{sp.get('rounds')}** 轮空转")
            for c in (sp.get("claims") or [])[:3]:
                L.append(f"    - 「{c[:100]}」")
        L.append("")

    if r["findings"]:
        L += ["## 逐条问题（引用原话 + 缺什么）", ""]
        for i, f in enumerate(r["findings"], 1):
            L.append(f"{i}. **[{f['level']}]** 「{f['quote'][:120]}」")
            L.append(f"   - 缺：{f['missing']}")
            L.append(f"   - 改：{f['suggest']}")
        L.append("")

    fx = r["fixes"]
    _pl = r.get("prompt_lint") or {}
    if _pl and not _pl.get("empty"):
        L += ["## 提示词体检（看你自己的提问）", "",
              f"**{_pl.get('score', 0)}/100** —— 只衡量「指令是否可执行」，不评价内容好坏。", ""]
        if _pl.get("issues"):
            for _i, _x in enumerate(_pl["issues"], 1):
                L.append(f"{_i}. **[{_x['level']}] {_x['rule']}** ｜ 证据：「{_x['quote'][:40]}」")
                L.append(f"   - 为什么：{_x['why']}")
                L.append(f"   - 怎么改：{_x['fix']}")
        else:
            L.append("没发现可改进项 —— 这份提问已经写得比较可执行了。")
        if _pl.get("passed"):
            L.append("")
            L.append(f"已做到的：{'、'.join(_pl['passed'])}")
        L += [""]

    L += ["## 改进方案", "", "### ① 提示词补丁（发给你的 AI）", ""]
    L += ["```text", "【可信度纪律】", fx["prompt_patch"] or "（本次无需补充）", "```", ""]
    L += ["### ② 你的使用习惯（给你自己的）", ""]
    L += [f"- {h}" for h in fx["habit_advice"]] + [""]
    L += ["### ③ 工具与替代方案", ""]
    L += [f"- {t}" for t in fx["tool_advice"]] + [""]

    L += ["## 这份报告的边界", ""]
    L += [f"- {b}" for b in r["boundary"]] + [""]
    return "\n".join(L)


if __name__ == "__main__":
    import json
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) > 1:
        obj = json.load(open(sys.argv[1], encoding="utf-8-sig"))
        import capture_spec
        sig = capture_spec.parse_v2(obj)
        sig["turns"] = obj.get("turns") or []
        r = audit_quality(sig)
        print(render_report(r, sig))
    else:
        print("用法：python quality_audit.py 采集结果.json")
