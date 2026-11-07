# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Woshichinese114514

"""把对比结果 / 指纹渲染成 Markdown。"""

from __future__ import annotations

from typing import Any, Dict, List

BAND_LABEL = {
    "same": ("OK", "指纹一致"),
    "family": ("~", "高度相似"),
    "related": ("?", "相关但不一致"),
    "different": ("X", "指纹不同"),
}

GROUP_LABELS = {
    "identity": "身份信号（system_fingerprint / id 格式 / model 回显）",
    "tokenizer": "tokenizer 指纹（同一句话的 token 数分布）",
    "first_step": "首 token 概率分布（top_logprobs）",
    "continuation": "温度 0 确定性续写",
    "protocol": "协议怪癖（流式/工具/FIM/usage 字段）",
    "metadata": "端点元数据（模型列表/余额接口）",
}


def render_compare(res: Dict[str, Any], detailed: bool = True) -> str:
    a, b = res["a"], res["b"]
    band = res["verdict"]["band"]
    mark, band_name = BAND_LABEL.get(band, ("-", band))
    out: List[str] = []
    out.append("# 模型指纹对比报告\n")
    out.append("- **A**：[%s] %s @ %s" % (a.get("label"), a.get("model"), a.get("base_url")))
    out.append("- **B**：[%s] %s @ %s" % (b.get("label"), b.get("model"), b.get("base_url")))
    out.append("")
    out.append("## 结论\n")
    out.append("[%s] **%s** —— 综合相似度 **%.1f%%**（置信度 %s，强证据占比 %.0f%%）" % (
        mark, band_name, res["score"] * 100, res["confidence"],
        res.get("strong_evidence_share", 0) * 100))
    out.append("")
    out.append("> %s" % res["verdict"]["text"])
    out.append("")

    out.append("## 分组得分\n")
    out.append("| 指纹组 | 得分 | 权重 | 是否可比 |")
    out.append("|---|---|---|---|")
    for name, group in res["groups"].items():
        if not group.get("comparable"):
            out.append("| %s | — | %.2f | 不可比（%s） |" % (
                GROUP_LABELS.get(name, name), group.get("weight", 0), group.get("reason")))
            continue
        out.append("| %s | %.1f%% | %.2f | 是 |" % (
            GROUP_LABELS.get(name, name), group["score"] * 100, group.get("weight", 0)))
    out.append("")

    signals = res.get("signals") or {}
    sa, sb = signals.get("a") or {}, signals.get("b") or {}
    out.append("## 关键信号\n")
    out.append("| 信号 | A | B |")
    out.append("|---|---|---|")
    for key in ("system_fingerprint", "id_formats", "logprobs", "tokenizer_mode", "tokenizer_total"):
        out.append("| %s | %s | %s |" % (key, _cell(sa.get(key)), _cell(sb.get(key))))
    out.append("")

    notes = res.get("notes") or []
    if notes:
        out.append("## 可疑点 / 提示\n")
        for note in notes:
            out.append("- %s" % note)
        out.append("")

    if detailed:
        out.append("## 逐项细节\n")
        for name, group in res["groups"].items():
            if not group.get("comparable") or not group.get("details"):
                continue
            out.append("### %s — %.1f%%" % (GROUP_LABELS.get(name, name), group["score"] * 100))
            for detail in group["details"]:
                matched = detail.get("match")
                symbol = "v" if matched is True else ("x" if matched is False else "-")
                line = "- %s %s" % (symbol, detail.get("label"))
                if detail.get("a") not in (None, "") or detail.get("b") not in (None, ""):
                    line += "：A=%s / B=%s" % (_short(detail.get("a")), _short(detail.get("b")))
                out.append(line)
            out.append("")
    return "\n".join(out)


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "—"
    return str(value)


def _short(value: Any, n: int = 90) -> str:
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= n else text[:n] + "…"


def render_matrix(mat: Dict[str, Any]) -> str:
    labels = mat["labels"]
    out = ["# 模型指纹矩阵\n", "| | " + " | ".join(labels) + " |",
           "|---|" + "---|" * len(labels)]
    for i, row in enumerate(mat["cells"]):
        cells = []
        for j, value in enumerate(row):
            if value is None:
                cells.append("")
            elif i == j:
                cells.append("—")
            else:
                cells.append("%.1f%%" % (value * 100))
        out.append("| **%s** | %s |" % (labels[i], " | ".join(cells)))
    out.append("")
    out.append("## 两两结论\n")
    for pair in mat["pairs"]:
        mark, name = BAND_LABEL.get(pair["band"], ("-", pair["band"]))
        out.append("- [%s] %s 与 %s：%.1f%%（%s，置信度 %s）" % (
            mark, pair["a"], pair["b"], pair["score"] * 100, name, pair["confidence"]))
    out.append("")
    return "\n".join(out)


def render_summary(fp: Dict[str, Any]) -> str:
    target = fp.get("target") or {}
    groups = fp.get("groups") or {}
    cost = fp.get("cost") or {}
    identity = groups.get("identity") or {}
    models = ((groups.get("metadata.models") or {}).get("data") or {}).get("models") or []
    tokenizer = groups.get("tokenizer") or {}
    fingerprint_lines = ", ".join(identity.get("system_fingerprints") or []) or "—"
    out = [
        "目标      : %s @ %s  (model=%s, prefix=%r)" % (
            target.get("label"), target.get("base_url"), target.get("model"),
            target.get("api_prefix")),
        "指纹哈希  : %s" % fp.get("hash"),
        "system_fp : %s" % fingerprint_lines,
        "端点模型  : %s" % ", ".join(m.get("id") or "?" for m in models[:8]),
        "tokenizer : %s（%d 个字符串）" % (tokenizer.get("mode"), len(tokenizer.get("counts") or {})),
        "logprobs  : %s" % (groups.get("first_step") or {}).get("logprobs_supported"),
        "流式      : %s" % (((groups.get("protocol.stream") or {}).get("data") or {}).get("status")),
        "探测开销  : %s 请求 / %s prompt tokens / %s completion tokens / %ss" % (
            cost.get("requests"), cost.get("prompt_tokens"),
            cost.get("completion_tokens"), cost.get("elapsed_s")),
    ]
    warnings = fp.get("warnings") or []
    if warnings:
        out.append("警告      : " + "; ".join(str(w) for w in warnings[:3]))
    if fp.get("truncated"):
        out.append("注意      : 部分探针因预算上限被跳过")
    return "\n".join(out)
