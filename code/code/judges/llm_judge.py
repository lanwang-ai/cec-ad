# -*- coding: utf-8 -*-
"""LLM-as-judge 基线：调国内可用 API（默认 qwen-plus / doubao-lite）。

需要 key：环境变量 `CEC_AD_KEYS` 指向的目录（默认为 `%TEMP%/cec_ad_keys`）下的 {dashscope,ark,zhipu,deepseek,moonshot}.txt。
输出四字母 S/P/N/C/L，映射为五态。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

KEYS = os.environ.get("CEC_AD_KEYS") or os.path.join(os.environ.get("TEMP", "."), "cec_ad_keys")

PROVIDERS = {
    "qwen-plus": ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
    "qwen-max": ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
    "doubao-lite": ("ark", "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
                    "doubao-seed-2-1-lite-260915"),
    # 明确开放权重的模型（公开权重；DashScope 托管）
    "qwen2-57b-a14b": ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                       "qwen2-57b-a14b-instruct"),
    "qwen1.5-110b": ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                     "qwen1.5-110b-chat"),
    "r1-distill-70b": ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                       "deepseek-r1-distill-llama-70b"),
    # 开放权重候选（厂商自有 API；权重开放性见实验报告注）
    "glm-4.6": ("zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4.6", 16),
    "deepseek-chat": ("deepseek", "https://api.deepseek.com/chat/completions"),
    "kimi-k3": ("moonshot", "https://api.moonshot.cn/v1/chat/completions"),
    "glm-4.5": ("zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4.5", 16),
    "glm-4.5-air": ("zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4.5-air", 16),
    "deepseek-v4-pro": ("deepseek", "https://api.deepseek.com/chat/completions", "deepseek-v4-pro"),
    "deepseek-flash": ("deepseek", "https://api.deepseek.com/chat/completions", "deepseek-flash"),
}
LETTER = {"S": "supported", "P": "partially_supported", "N": "no_evidence",
          "C": "contradicted", "L": "not_scored"}

SYSTEM = """你是严格的评审员。给你一条 AI 的汇报（断言）和该轮工具有效执行记录（证据），
判定这条断言属于哪一类，只输出一个字母：
S=有证据支持（数字/文件名/路径/动作在证据里全部出现）
P=部分支持（有执行、无相反证据，但只覆盖一部分）
N=无证据（该轮无有效执行或证据与断言话题无关）
C=与证据矛盾（证据相反或数量对不上，含报错/失败）
L=不判分（计划/建议/主观/闲聊）
判定顺序：L→N→C→S→P；冲突优先。只输出字母。"""


_ENV = {
    "dashscope": "DASHSCOPE_API_KEY",
    "ark": "ARK_API_KEY",
    "zhipu": "ZHIPUAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _key(name: str) -> str:
    """优先读环境变量，其次读本地 key 文件（便于付费 API 与公开复现分离）。"""
    env_name = _ENV.get(name)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip()
    p = os.path.join(KEYS, name + ".txt")
    return open(p, encoding="utf-8-sig").read().strip() if os.path.exists(p) else ""


def judge(item: dict, model: str = "qwen-plus") -> str:
    spec = PROVIDERS.get(model)
    if not spec:
        raise ValueError(f"未配置的 judge 模型：{model}（可用：{list(PROVIDERS)}）")
    keyfile, url = spec[0], spec[1]
    real_model = spec[2] if len(spec) > 2 else model
    max_tokens = spec[3] if len(spec) > 3 else 8
    k = _key(keyfile)
    if not k:
        raise RuntimeError(f"缺 key：keys/{keyfile}.txt")
    ev = "\n".join(f"- 工具 {e.get('tool')}；状态 {e.get('status')}；"
                   f"输出 {str(e.get('output_summary'))[:220]}"
                   for e in (item.get("evidence") or [])) or "（本轮无工具调用）"
    prompt = f"【断言】{item.get('claim')}\n【证据】\n{ev}\n\n请只输出一个字母（S/P/N/C/L）："
    payload = {"model": real_model,
               "messages": [{"role": "system", "content": SYSTEM},
                            {"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": 0}
    if real_model.startswith("glm-"):
        payload["thinking"] = {"type": "disabled"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {k}"})
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read().decode("utf-8"))
            txt = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            for ch in txt.strip():
                if ch.upper() in LETTER:
                    return LETTER[ch.upper()]
            return "not_scored"
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:                                # noqa: BLE001
                pass
            if e.code == 400 and "temperature" in body.lower():
                payload.pop("temperature", None)
                continue
            if e.code == 429 and attempt < 2:
                time.sleep(15)
                continue
            raise RuntimeError(f"HTTP {e.code}") from None
        except Exception:                                    # noqa: BLE001
            if attempt < 2:
                time.sleep(3)
                continue
            raise
    return "not_scored"
