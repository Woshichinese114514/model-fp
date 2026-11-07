"""指纹对比：分组打分、置信度、结论与可疑点。"""

from __future__ import annotations

import difflib
import math
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_WEIGHTS = {
    "identity": 0.25,
    "tokenizer": 0.25,
    "continuation": 0.18,
    "first_step": 0.15,
    "protocol": 0.09,
    "metadata": 0.08,
}

# 这几组是"强证据"，决定结论置信度
STRONG_GROUPS = ("identity", "tokenizer", "first_step")

# 首 token 分布：低于该对数概率的候选是尾部噪声，不参与比较
SALIENT_LOGPROB = -14.0
# 对数概率差在多少 nats 以内仍算同一分布（温度 0 下同模型的抖动约 0.1~0.6）
LOGPROB_TOLERANCE = 2.5

VERDICTS = [
    (0.95, "same", "指纹一致：几乎可以确定是同一个模型（同一权重/同一后端）"),
    (0.85, "family", "高度相似：同源同族，但存在可测量的差异（可能是版本、量化或网关改写）"),
    (0.65, "related", "相关但不一致：部分指纹吻合，需要人工复核具体差异"),
    (0.00, "different", "指纹不同：不像同一个模型"),
]


def _detail(label: str, a: Any, b: Any, match: Optional[bool]) -> Dict[str, Any]:
    return {"label": label, "a": a, "b": b, "match": match}


def _aggregate(parts: List[Tuple[float, float]],
               details: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_w = sum(w for w, _ in parts)
    score = (sum(w * s for w, s in parts) / total_w) if total_w else 0.0
    return {"score": round(score, 4), "details": details, "coverage": round(total_w, 3)}


def _sfp(group: Dict[str, Any]) -> List[str]:
    return [x for x in (group.get("system_fingerprints") or []) if x]


# ----------------------------------------------------------------------
# 分组打分（返回 None 表示该组不可比）
# ----------------------------------------------------------------------
def score_identity(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ia = a.get("groups", {}).get("identity") or {}
    ib = b.get("groups", {}).get("identity") or {}
    parts: List[Tuple[float, float]] = []
    details: List[Dict[str, Any]] = []

    fa, fb = _sfp(ia), _sfp(ib)
    if fa and fb:
        same = set(fa) == set(fb)
        parts.append((0.55, 1.0 if same else 0.0))
        details.append(_detail("system_fingerprint", fa, fb, same))

    if ia.get("id_formats") and ib.get("id_formats"):
        same = ia["id_formats"] == ib["id_formats"]
        parts.append((0.10, 1.0 if same else 0.0))
        details.append(_detail("响应 id 格式", ia["id_formats"], ib["id_formats"], same))

    if ia.get("model_echoes") and ib.get("model_echoes"):
        same = ia["model_echoes"] == ib["model_echoes"]
        parts.append((0.16, 1.0 if same else 0.0))
        details.append(_detail("model 回显", ia["model_echoes"], ib["model_echoes"], same))

    oa, ob = ia.get("chat_overhead_tokens"), ib.get("chat_overhead_tokens")
    if isinstance(oa, (int, float)) and isinstance(ob, (int, float)):
        delta = abs(oa - ob)
        score = max(0.0, 1.0 - delta / 20.0)
        parts.append((0.12, score))
        details.append(_detail("隐藏提示词开销（chat 比裸 completion 多出的 prompt token）",
                               "%s" % oa, "%s" % ob, delta <= 4))

    # API 前缀只有在确实采到了身份数据时才参与打分，避免"只剩前缀"时虚高
    pa = (a.get("target") or {}).get("api_prefix")
    pb = (b.get("target") or {}).get("api_prefix")
    if parts and pa is not None and pb is not None:
        same = pa == pb
        parts.append((0.12, 1.0 if same else 0.0))
        details.append(_detail("API 前缀", pa or "(根)", pb or "(根)", same))

    if not parts:
        return None
    return _aggregate(parts, details)


def score_tokenizer(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ta = a.get("groups", {}).get("tokenizer") or {}
    tb = b.get("groups", {}).get("tokenizer") or {}
    va = ta.get("vector") or {}
    vb = tb.get("vector") or {}
    shared = sorted(set(va) & set(vb))
    if len(shared) < 3:
        return None

    diffs = [abs(va[k] - vb[k]) for k in shared]
    denom = sum(max(va[k], vb[k]) for k in shared)
    l1_sim = 1.0 - (sum(diffs) / denom) if denom else 1.0
    exact = sum(1 for k in shared if va[k] == vb[k]) / len(shared)
    res = _aggregate([(0.7, l1_sim), (0.3, exact)], [])
    res["details"] = [
        {"label": "token 数向量相似度", "a": "%.3f" % l1_sim,
         "b": "模式 %s vs %s" % (ta.get("mode"), tb.get("mode")), "match": l1_sim > 0.98},
        {"label": "逐串 token 数一致比例", "a": "%.0f%%" % (exact * 100), "b": "",
         "match": exact > 0.95},
        {"label": "string → 相对 token 数", "a": _fmt_counts(va, shared), "b": "", "match": None},
    ]
    return res


def _fmt_counts(vector: Dict[str, int], keys: List[str]) -> str:
    return ", ".join("%r:%s" % (k[:12], vector[k]) for k in keys[:8])


def _salient(sample: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for token in sample.get("top") or []:
        name, lp = token.get("t"), token.get("lp")
        if name and isinstance(lp, (int, float)) and lp >= SALIENT_LOGPROB:
            out[name] = float(lp)
    return out


def score_first_step(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    xa = a.get("groups", {}).get("first_step") or {}
    xb = b.get("groups", {}).get("first_step") or {}
    if not xa.get("logprobs_supported") or not xb.get("logprobs_supported"):
        return None
    sa = {s.get("prefix"): s for s in (xa.get("samples") or []) if s.get("top")}
    sb = {s.get("prefix"): s for s in (xb.get("samples") or []) if s.get("top")}
    shared = [p for p in sa if p in sb]
    if not shared:
        return None

    scores: List[float] = []
    details: List[Dict[str, Any]] = []
    for prefix in shared:
        A, B = sa[prefix], sb[prefix]
        top1 = 1.0 if A.get("top1") == B.get("top1") else 0.0

        la, lb = _salient(A), _salient(B)
        keys_a, keys_b = set(la), set(lb)
        jac = len(keys_a & keys_b) / len(keys_a | keys_b) if (keys_a or keys_b) else 1.0
        common = keys_a & keys_b
        if common:
            close = sum(
                math.exp(-abs(la[t] - lb[t]) / LOGPROB_TOLERANCE) for t in common
            ) / len(common)
        else:
            close = 0.0

        top1_close = 1.0
        pa, pb = A.get("top1_logprob"), B.get("top1_logprob")
        if isinstance(pa, (int, float)) and isinstance(pb, (int, float)):
            top1_close = math.exp(-abs(pa - pb) / LOGPROB_TOLERANCE)

        score = 0.40 * top1 + 0.15 * top1_close + 0.30 * close + 0.15 * jac
        scores.append(score)
        details.append({
            "label": "首 token 分布 · %r" % prefix[:26],
            "a": "top1=%r 显著候选=%s" % (A.get("top1"), sorted(la)),
            "b": "top1=%r 显著候选=%s" % (B.get("top1"), sorted(lb)),
            "match": top1 == 1.0 and jac >= 0.5,
            "extra": {"top1_match": top1 == 1.0, "salient_jaccard": round(jac, 3)},
        })
    return {"score": round(sum(scores) / len(scores), 4), "details": details,
            "coverage": 1.0}


def score_continuation(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ca = a.get("groups", {}).get("continuation") or {}
    cb = b.get("groups", {}).get("continuation") or {}
    parts: List[Tuple[float, float]] = []
    details: List[Dict[str, Any]] = []

    # 1) /beta/completions 的确定性续写文本
    if ca.get("supported") and cb.get("supported"):
        ta = {s.get("prompt"): s.get("text") or ""
              for s in ca.get("samples") or [] if "text" in s}
        tb = {s.get("prompt"): s.get("text") or ""
              for s in cb.get("samples") or [] if "text" in s}
        shared = [p for p in ta if p in tb]
        if shared:
            ratios = [difflib.SequenceMatcher(None, ta[p], tb[p]).ratio() for p in shared]
            parts.append((0.6, sum(ratios) / len(ratios)))
            details += [
                {"label": "续写 · %r" % p[:24], "a": ta[p][:40], "b": tb[p][:40],
                 "match": ta[p] == tb[p]}
                for p in shared
            ]

    # 2) chat 推理前缀文本（模型"思考风格"）
    #    注意：reasoning token 在温度 0 下也只有前 ~10 个 token 稳定，所以只比稳定前缀，
    #    并且只给较小权重，避免把采样噪声当成模型差异。
    ga = {s.get("prompt"): (s.get("text") or "")[:40]
          for s in ca.get("greedy") or [] if "text" in s}
    gb = {s.get("prompt"): (s.get("text") or "")[:40]
          for s in cb.get("greedy") or [] if "text" in s}
    shared_g = [p for p in ga if p in gb]
    if shared_g:
        ratios = [difflib.SequenceMatcher(None, ga[p], gb[p]).ratio() for p in shared_g]
        parts.append((0.2, sum(ratios) / len(ratios)))
        details += [
            {"label": "推理前缀 · %r" % p[:24], "a": ga[p][:44], "b": gb[p][:44],
             "match": ga[p] == gb[p]}
            for p in shared_g
        ]

    if not parts:
        return None
    return _aggregate(parts, details)


def _protocol_signals(fp: Dict[str, Any]) -> Dict[str, Any]:
    groups = fp.get("groups") or {}
    stream = ((groups.get("protocol.stream") or {}).get("data") or {})
    json_object = ((groups.get("protocol.json_object") or {}).get("data") or {})
    tools = ((groups.get("protocol.tools") or {}).get("data") or {})
    fim = ((groups.get("protocol.fim") or {}).get("data") or {})
    anthropic = ((groups.get("protocol.anthropic") or {}).get("data") or {})
    identity = groups.get("identity") or {}
    return {
        "stream.object": stream.get("chunk_object"),
        "stream.delta_keys": stream.get("delta_keys"),
        "stream.usage_in_chunk": stream.get("final_has_usage"),
        "json_object.accepted": json_object.get("accepted"),
        "tools.accepted": tools.get("accepted"),
        "fim.accepted": fim.get("accepted"),
        "anthropic.status": anthropic.get("status"),
        "usage_keys": identity.get("usage_keys"),
    }


def score_protocol(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sa = {k: v for k, v in _protocol_signals(a).items() if v is not None}
    sb = {k: v for k, v in _protocol_signals(b).items() if v is not None}
    shared = sorted(set(sa) & set(sb))
    if not shared:
        return None
    matched = [k for k in shared if sa[k] == sb[k]]
    details = [
        {"label": k, "a": sa[k], "b": sb[k], "match": sa[k] == sb[k]}
        for k in shared
    ]
    return {"score": round(len(matched) / len(shared), 4), "details": details,
            "coverage": 1.0}


def _model_ids(fp: Dict[str, Any]) -> set:
    data = ((fp.get("groups") or {}).get("metadata.models") or {}).get("data") or {}
    return {m.get("id") for m in (data.get("models") or []) if m.get("id")}


def score_metadata(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ma, mb = _model_ids(a), _model_ids(b)
    if not ma or not mb:
        return None
    jac = len(ma & mb) / len(ma | mb)
    parts = [(0.7, jac)]
    details = [{"label": "端点模型列表", "a": sorted(ma)[:6], "b": sorted(mb)[:6],
                "match": jac == 1.0}]
    ba = ((a.get("groups") or {}).get("metadata.balance") or {}).get("data") or {}
    bb = ((b.get("groups") or {}).get("metadata.balance") or {}).get("data") or {}
    if ba.get("present") is not None and bb.get("present") is not None:
        same = bool(ba.get("present")) == bool(bb.get("present"))
        parts.append((0.3, 1.0 if same else 0.0))
        details.append({"label": "余额接口", "a": ba.get("present"), "b": bb.get("present"),
                        "match": same})
    return _aggregate(parts, details)


SCORERS = {
    "identity": score_identity,
    "tokenizer": score_tokenizer,
    "continuation": score_continuation,
    "first_step": score_first_step,
    "protocol": score_protocol,
    "metadata": score_metadata,
}


# ----------------------------------------------------------------------
# 总结与结论
# ----------------------------------------------------------------------
def _confidence(comparable_weights: Dict[str, float],
                total_weight: float) -> Tuple[str, float]:
    strong = sum(w for g, w in comparable_weights.items() if g in STRONG_GROUPS)
    share = strong / total_weight if total_weight else 0.0
    if share >= 0.6:
        return "high", round(share, 2)
    if share >= 0.3:
        return "medium", round(share, 2)
    return "low", round(share, 2)


def _verdict(score: float, confidence: str) -> Dict[str, Any]:
    band, text = "different", VERDICTS[-1][2]
    for threshold, name, label in VERDICTS:
        if score >= threshold:
            band, text = name, label
            break
    if confidence == "low" and band in ("same", "family"):
        band = "related"
        text = "证据不足（关键指纹缺失），结论降级：" + text
    return {"band": band, "text": text}


def compare(a: Dict[str, Any], b: Dict[str, Any],
            weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    weights = weights or DEFAULT_WEIGHTS
    groups: Dict[str, Any] = {}
    comparable_weights: Dict[str, float] = {}
    weighted = 0.0
    total_weight = 0.0

    for name, weight in weights.items():
        scorer = SCORERS.get(name)
        if scorer is None:
            groups[name] = {"comparable": False, "reason": "unknown-group", "weight": weight}
            continue
        res = scorer(a, b)
        if res is None:
            groups[name] = {"comparable": False, "reason": "数据缺失", "weight": weight}
            continue
        groups[name] = {"comparable": True, "weight": weight, **res}
        comparable_weights[name] = weight
        weighted += weight * res["score"]
        total_weight += weight

    score = round(weighted / total_weight, 4) if total_weight else 0.0
    confidence, strong_share = _confidence(comparable_weights, total_weight)
    verdict = _verdict(score, confidence)

    return {
        "a": {"label": (a.get("target") or {}).get("label"),
              "base_url": (a.get("target") or {}).get("base_url"),
              "model": (a.get("target") or {}).get("model")},
        "b": {"label": (b.get("target") or {}).get("label"),
              "base_url": (b.get("target") or {}).get("base_url"),
              "model": (b.get("target") or {}).get("model")},
        "score": score,
        "verdict": verdict,
        "confidence": confidence,
        "strong_evidence_share": strong_share,
        "groups": groups,
        "notes": _notes(a, b, groups),
        "signals": {"a": _signals(a), "b": _signals(b)},
    }


def _signals(fp: Dict[str, Any]) -> Dict[str, Any]:
    identity = (fp.get("groups") or {}).get("identity") or {}
    first_step = (fp.get("groups") or {}).get("first_step") or {}
    tokenizer = (fp.get("groups") or {}).get("tokenizer") or {}
    return {
        "system_fingerprint": (identity.get("system_fingerprints") or [None])[0],
        "id_formats": identity.get("id_formats"),
        "logprobs": first_step.get("logprobs_supported"),
        "tokenizer_mode": tokenizer.get("mode"),
        "tokenizer_total": sum((tokenizer.get("vector") or {}).values()),
    }


def _notes(a: Dict[str, Any], b: Dict[str, Any], groups: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    fa = _sfp((a.get("groups") or {}).get("identity") or {})
    fb = _sfp((b.get("groups") or {}).get("identity") or {})
    if fa and fb and set(fa) != set(fb):
        notes.append("system_fingerprint 不一致：可能是不同模型/版本，或网关改写了该字段。")
    if fa and fb and set(fa) == set(fb):
        notes.append("system_fingerprint 完全一致，这是最强的同一模型信号。")

    tok = groups.get("tokenizer") or {}
    if tok.get("comparable") and tok.get("score", 0) < 0.98:
        notes.append("tokenizer 指纹有差异：字符串的 token 数分布不同（词表/分词器不同）。")

    fs = groups.get("first_step") or {}
    if fs.get("comparable") and fs.get("score", 0) < 0.9:
        notes.append("首 token 概率分布明显不同：即使名字相同，权重也可能被替换或量化。")

    oa = ((a.get("groups") or {}).get("identity") or {}).get("chat_overhead_tokens")
    ob = ((b.get("groups") or {}).get("identity") or {}).get("chat_overhead_tokens")
    if isinstance(oa, (int, float)) and isinstance(ob, (int, float)) and abs(oa - ob) > 4:
        notes.append(
            "隐藏提示词开销不同（%.0f vs %.0f token）：chat 模板或服务端注入的 system prompt "
            "不一致，通常意味着不同模型/不同版本。" % (oa, ob)
        )

    for side, fp in (("A", a), ("B", b)):
        fp_groups = fp.get("groups") or {}
        if (fp_groups.get("first_step") or {}).get("logprobs_supported") is False:
            notes.append("%s 端不支持 logprobs（部分网关会剥离该能力）。" % side)
        if (fp_groups.get("identity") or {}).get("model_echo_matches_request") is False:
            notes.append("%s 端回显的 model 与请求的不一致（疑似网关改写/路由到别的模型）。" % side)
        balance = ((fp_groups.get("metadata.balance") or {}).get("data") or {})
        if balance.get("present") is False:
            notes.append("%s 端没有 /user/balance 接口（非原生 OpenAI/DeepSeek 风格的常见特征）。" % side)
        anthropic = ((fp_groups.get("protocol.anthropic") or {}).get("data") or {})
        if anthropic.get("status") not in (200, None):
            notes.append("%s 端不支持 Anthropic 兼容面。" % side)
    return notes


# ----------------------------------------------------------------------
# 多指纹比对矩阵
# ----------------------------------------------------------------------
def matrix(fps: List[Dict[str, Any]],
           weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    n = len(fps)
    raw_labels = [(fp.get("target") or {}).get("label") or "?" for fp in fps]
    counts: Dict[str, int] = {}
    for label in raw_labels:
        counts[label] = counts.get(label, 0) + 1
    labels: List[str] = []
    for fp, label in zip(fps, raw_labels):
        if counts[label] > 1:
            label = "%s:%s" % (label, (fp.get("target") or {}).get("model"))
        labels.append(label)

    cells: List[List[Optional[float]]] = [[None] * n for _ in range(n)]
    pairs: List[Dict[str, Any]] = []
    for i in range(n):
        cells[i][i] = 1.0
        for j in range(i + 1, n):
            res = compare(fps[i], fps[j], weights)
            cells[i][j] = cells[j][i] = res["score"]
            pairs.append({
                "i": i, "j": j,
                "a": labels[i], "b": labels[j],
                "score": res["score"],
                "band": res["verdict"]["band"],
                "confidence": res["confidence"],
                "notes": res["notes"],
            })
    return {"labels": labels, "cells": cells, "pairs": pairs}
