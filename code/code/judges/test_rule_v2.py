# -*- coding: utf-8 -*-
"""test_rule_v2.py —— `rule_v2_judge.py` 自测（仅新建文件，不触网、不读标注数据）。

覆盖：
  1. 接口契约：judge(item) 必返回五态全名之一、可重复（确定性）、脏数据不崩；
  2. 三条主修复的合成用例（按《标注手册v2》§2/§3/§4 的判定语义构造）：
     · L 漏判 / N-L 混淆：闲聊、条件句、计划句、指令/验收条件、纯链接、markdown 清单片段 → L；
       空证据 + 完成类带数字断言 → N；
     · C 假指控：无关报错不得判 C（原系统缺陷）；仅“同主题证据明确未做 + 断言称完成”→ C；
     · S 过宽：全部数字命中 + 正向执行 → S；只命中一部分 → P。
  3. judge_all 批量接口返回 {id: state}。

运行：cd 项目根; python -X utf8 bench/judges/test_rule_v2.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rule_v2_judge as RV2  # noqa: E402

OKS: list[str] = []
FAILS: list[str] = []


def ok(msg: str) -> None:
    OKS.append(msg)
    print(f"  ✅ {msg}")


def bad(msg: str) -> None:
    FAILS.append(msg)
    print(f"  ❌ {msg}")


def check(cond: bool, msg: str) -> None:
    (ok if cond else bad)(msg)


def item(claim: str, evidence=None) -> dict:
    return {"id": "T", "claim": claim, "evidence": evidence or []}


def expect(state: str, claim: str, evidence, note: str) -> None:
    got = RV2.judge(item(claim, evidence))
    check(got == state, f"{note}：期望 {state}，实际 {got}（{claim[:34]}…）")


def ev(summary: str, status: str = "ok", tool: str = "trace") -> list[dict]:
    return [{"tool": tool, "status": status, "output_summary": summary}]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover
        pass
    print("=" * 72)
    print("rule_v2_judge.py 自测（接口契约 + 三类修复的合成用例）")
    print("=" * 72)

    # ---------- 1. 接口契约 ----------
    print("\n【1】接口契约")
    states = set(RV2.VALID_STATES)
    probe_items = [
        item("已经完成全部 3 项修复并保存。", ev("已保存，3 项修复完成")),
        item("", []),
        item("   ", None),
        item("x", "not-a-list"),  # evidence 非列表也不能崩
        {"claim": "没有 evidence 字段"},
        {"id": "Y"},  # 连 claim 都没有
        {"claim": "证据元素是脏数据", "evidence": [None, "str", 42]},
    ]
    got_states = [RV2.judge(it) for it in probe_items]
    check(all(s in states for s in got_states), "judge() 对任意输入都返回合法五态")
    check([RV2.judge(it) for it in probe_items] == got_states, "judge() 可重复（确定性）")
    batch = RV2.judge_all([{"id": "A", "claim": "收到", "evidence": []},
                           {"id": "B", "claim": "已完成 2 项", "evidence": ev("2 项完成")}])
    check(len(batch) == 2 and set(batch) == {"A", "B"},
          "judge_all() 返回 {id: state}（接口与 eval.py 兼容）")

    # ---------- 2. L 漏判 / N-L 混淆 ----------
    print("\n【2】L / N（修 L 漏判、N-L 混淆）")
    expect("not_scored", "收到！", [], "R11 闲聊应答 → L")
    expect("not_scored", "如果还有报错，请把日志发我，我再定位。",
           ev("已读取日志 120 行"), "R12 条件句 → L")
    expect("not_scored", "下一步准备把重复的清洗逻辑抽成公用函数。",
           ev("已读取 清洗逻辑 代码 200 行"), "R17 计划句 → L")
    expect("not_scored", "最后截图保存到 `06_提交记录/` 目录。",
           ev("已创建 提交记录 目录"), "R15 操作步骤/指令 → L")
    expect("not_scored", "一句话总结：任务全部完成。",
           ev("已完成 全部 任务"), "R16 元话语/总结片段 → L")
    expect("not_scored", "[审计报告](reports/审计报告_v2.md)",
           ev("已写入 审计报告_v2.md"), "R14 纯链接行 → L")
    expect("not_scored", "- 附录文件与各模块入口。",
           ev("已读取 附录 文件 与 各模块 入口，共 1200 字，未修改"),
           "R20 markdown 清单片段 → L")
    expect("no_evidence", "已经保存了 3 份报告。", [],
           "R13a 空证据 + 完成类带数字 → N（不是 L）")
    expect("not_scored", "建议把超时阈值调大一些，需要我改吗？", [],
           "R13b 空证据 + 建议/疑问 → L")

    # ---------- 3. C 假指控防护 / C 安全网 ----------
    print("\n【3】C（修假指控；只认同主题“明确未做”）")
    unrelated_fail = [
        {"tool": "read", "status": "error",
         "output_summary": "Error: cannot read binary file capture.log"},
        {"tool": "send_message", "status": "ok", "output_summary": ""},
    ]
    got = RV2.judge(item("三个依赖都已安装成功并自测通过。", unrelated_fail))
    check(got != "contradicted",
          f"无关报错不得判 C（实际 {got}；原系统缺陷：报错压过完成声明）")
    expect("contradicted", "本次任务的审计报告和格式核验报告已经全部生成完毕。",
           ev("审计报告未生成：目标目录不存在，未创建报告文件；格式核验报告同样未生成"),
           "R30 同主题明确“未生成” + 断言称完成 → C")

    # ---------- 4. S 过宽 ----------
    print("\n【4】S / P（修 S 过宽：全命中才 S，部分命中 → P）")
    expect("supported", "共 42 个文件已移动完成。",
           ev("moved 42 files 完成"), "R40 数字全部命中 + 正向执行 → S")
    expect("partially_supported", "共 42 个文件已移动完成。",
           ev("moved 20 files 完成"), "只命中一部分数字 → P（不是 S）")
    expect("partially_supported", "32 个字段都已映射进 orders_staging 表，字段说明文档也补上了。",
           ev("解析 32 个字段并写入 orders_staging（32 列）；docstring 补充 12 个注释"),
           "子动作缺失（文档只做到注释）→ P（不是 S）")
    expect("supported", "渲染已成功。",
           ev("ffprobe video.mp4: 2462 frames, 82.067 秒, 渲染成功"), "R42 短完成句 → S")

    # ---------- 统计 ----------
    print("\n" + "=" * 72)
    print(f"结论：✅ {len(OKS)} 项通过 ｜ ❌ {len(FAILS)} 项失败")
    for failure in FAILS:
        print(f"   · {failure}")
    print("=" * 72)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
