"""探针定义与执行。

硬性约束：所有探针都是"小输入"——
- 单次请求 prompt 不超过 MAX_PROMPT_TOKENS（64）token；
- 单次请求输出不超过 MAX_OUTPUT_TOKENS（16）token；
- 默认整轮探测约 30~40 个请求、总计 < 3000 token。

采集 7 组指纹：
- metadata   端点元数据（/models、余额、错误模板、Anthropic 兼容面）
- protocol   协议怪癖（流式分块、json_object、tools、FIM、usage 字段）
- tokenizer  tokenizer 指纹（各字符串的 token 数向量）
- first_step 首 token 的 top_logprobs 分布（最灵敏的模型指纹）
- continuation 温度 0 下的确定性续写
- identity   system_fingerprint / id 格式 / model 回显
- determinism 同请求两次是否逐字一致（seed 支持）
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from .client import Transport

MAX_PROMPT_TOKENS = 64
MAX_OUTPUT_TOKENS = 16

GROUP_ORDER = [
    "metadata",
    "protocol",
    "tokenizer",
    "first_step",
    "continuation",
    "identity",
    "determinism",
]

# 覆盖数字 / 拉丁 / 汉字 / 日文 / 希腊字母 / emoji / 空白 / 代码 / 驼峰
TOKENIZER_STRINGS = [
    "a",
    "hello",
    "Hello, world!",
    "tokenization",
    "HelloWorldCamelCase",
    "1234567890",
    "中文分词测试",
    "日本語のテキスト",
    "αβγδε",
    "def fibonacci(n):",
    "🚀🔥👍",
    "über — naïve",
    "  spaces   here  ",
    "水是生命之源，",
]

# 温度 0 下首 token 的 top_logprobs 分布
FIRST_STEP_PREFIXES = [
    "The capital of France is",
    "2 + 2 =",
    "1, 1, 2, 3, 5, 8,",
    "水是生命之源，",
]

# /beta/completions 的确定性续写（不经 chat 模板）
CONTINUATION_PROMPTS = [
    "The capital of France is",
    "def add(a, b):\n    return",
    "1, 1, 2, 3, 5, 8,",
    "水是生命之源，",
]

CHAT_GREEDY_PROMPTS = [
    "Answer with one word: what is 2+2?",
    "Repeat exactly: alpha bravo charlie",
]

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _choice(body: Any) -> Dict[str, Any]:
    if isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            return choices[0]
    return {}


def _usage(body: Any) -> Dict[str, Any]:
    if isinstance(body, dict) and isinstance(body.get("usage"), dict):
        return body["usage"]
    return {}


def id_format(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "none"
    if UUID4_RE.match(value):
        return "uuid4"
    if HEX32_RE.match(value):
        return "hex32"
    for prefix in ("chatcmpl-", "cmpl-", "msg_", "resp_", "gen-", "req-"):
        if value.startswith(prefix):
            return prefix + "*"
    return "other"


def error_shape(resp) -> Dict[str, Any]:
    """把错误响应抽象成"模板"，用于跨端点比对错误风格。"""
    body = resp.body
    out: Dict[str, Any] = {"status": resp.status}
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        out["error_keys"] = sorted(err.keys())
        out["error_type"] = err.get("type")
        out["error_code"] = err.get("code")
        msg = err.get("message")
        if isinstance(msg, str):
            template = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{20,}", "<id>", msg)
            out["message_template"] = re.sub(r"\d+", "0", template)[:160]
            out["message_len"] = len(msg)
            out["has_request_id"] = bool(
                re.search(r"request[_ ]id", msg, re.I) or "request_id" in err
            )
            names = re.findall(r"deepseek-[a-z0-9.\-]+", msg)
            if names:
                out["names_in_message"] = sorted(set(names))[:8]
    elif isinstance(body, str):
        out["body_kind"] = "text"
        out["first_line"] = body.splitlines()[0][:120] if body.splitlines() else ""
    elif isinstance(body, dict):
        out["error_keys"] = sorted(body.keys())
        out["body_kind"] = "json-other"
    else:
        out["body_kind"] = type(body).__name__
    return out


def _truncate(text: Any, n: int = 120) -> Optional[str]:
    if text is None:
        return None
    return str(text)[:n]


def _collect_identity(resp, model: str) -> Optional[Dict[str, Any]]:
    """从任意 chat/completions 响应里收集身份信号。"""
    # 只认成功的 chat/completions 响应：错误体、/models、Anthropic 体都会污染身份信号
    if not resp.ok or not isinstance(resp.body, dict):
        return None
    body = resp.body
    if not isinstance(body.get("choices"), list):
        return None
    return {
        "id_format": id_format(body.get("id")),
        "model_echo": body.get("model"),
        "model_echo_matches_request": (
            (body.get("model") == model) if body.get("model") else None
        ),
        "system_fingerprint": body.get("system_fingerprint"),
        "object": body.get("object"),
        "usage_keys": sorted(_usage(body).keys()),
    }


# ----------------------------------------------------------------------
# 简单探针（单请求）
# ----------------------------------------------------------------------
def _extract(kind: str, resp, model: str) -> Dict[str, Any]:
    if kind == "models":
        body = resp.body if isinstance(resp.body, dict) else {}
        data = body.get("data") if isinstance(body.get("data"), list) else []
        models = []
        for item in data[:40]:
            if not isinstance(item, dict):
                continue
            rec: Dict[str, Any] = {"id": item.get("id")}
            for key in ("owned_by", "name", "object", "context_window",
                        "max_output_tokens", "type"):
                if item.get(key) is not None:
                    rec[key] = item[key]
            if isinstance(item.get("input_modalities"), list):
                rec["input_modalities"] = item["input_modalities"]
            if isinstance(item.get("output_modalities"), list):
                rec["output_modalities"] = item["output_modalities"]
            if isinstance(item.get("effort"), dict):
                rec["effort_levels"] = item["effort"].get("supported_levels")
            if isinstance(item.get("api_capabilities"), dict):
                rec["api_capabilities"] = sorted(item["api_capabilities"].keys())
            models.append(rec)
        return {
            "status": resp.status,
            "count": len(data),
            "top_keys": sorted(body.keys()),
            "models": models,
            "header_server": resp.header("server"),
        }

    if kind == "balance":
        body = resp.body if isinstance(resp.body, dict) else {}
        infos = body.get("balance_infos") if isinstance(body.get("balance_infos"), list) else []
        return {
            "status": resp.status,
            "present": resp.ok,
            "top_keys": sorted(body.keys()) if isinstance(body, dict) else [],
            "currencies": [i.get("currency") for i in infos if isinstance(i, dict)],
            "has_is_available": "is_available" in body if isinstance(body, dict) else False,
        }

    if kind == "error_shape":
        return error_shape(resp)

    if kind == "anthropic":
        body = resp.body if isinstance(resp.body, dict) else {}
        content = body.get("content") if isinstance(body.get("content"), list) else []
        return {
            "status": resp.status,
            "top_keys": sorted(body.keys()) if isinstance(body, dict) else [],
            "content_types": [c.get("type") for c in content if isinstance(c, dict)],
            "usage_keys": sorted(body.get("usage", {}).keys())
            if isinstance(body.get("usage"), dict) else [],
            "signature_equals_id": (
                bool(content) and isinstance(content[0], dict)
                and content[0].get("signature") == body.get("id")
            ),
            "error": error_shape(resp) if not resp.ok else None,
        }

    if kind == "stream":
        events = resp.events or []
        data_events = [e for e in events if e.startswith("data:")]
        last = resp.body if isinstance(resp.body, dict) else {}
        choice = _choice(last)
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        return {
            "status": resp.status,
            "event_count": len(events),
            "data_event_count": len(data_events),
            "chunk_object": last.get("object"),
            "chunk_keys": sorted(last.keys()) if isinstance(last, dict) else [],
            "has_system_fingerprint": "system_fingerprint" in last
            if isinstance(last, dict) else False,
            "delta_keys": sorted(delta.keys()),
            "final_has_usage": bool(_usage(last)),
            "first_event_prefix": data_events[0][:60] if data_events else None,
        }

    if kind == "json_object":
        return {"status": resp.status, "accepted": resp.ok,
                "error": error_shape(resp) if not resp.ok else None}

    if kind == "tools":
        body = resp.body if isinstance(resp.body, dict) else {}
        message = _choice(body).get("message") or {}
        return {
            "status": resp.status,
            "accepted": resp.ok,
            "message_keys": sorted(message.keys()) if isinstance(message, dict) else [],
            "has_tool_calls": bool(message.get("tool_calls")) if isinstance(message, dict) else False,
            "error": error_shape(resp) if not resp.ok else None,
        }

    if kind == "fim":
        body = resp.body if isinstance(resp.body, dict) else {}
        choice = _choice(body)
        return {
            "status": resp.status,
            "accepted": resp.ok,
            "text": _truncate(choice.get("text"), 40) if resp.ok else None,
            "error": error_shape(resp) if not resp.ok else None,
        }

    return {"status": resp.status}


SIMPLE_PROBES: List[Dict[str, Any]] = [
    {
        "id": "metadata.models",
        "group": "metadata",
        "method": "GET",
        "paths": ["/models"],
        "extract": "models",
    },
    {
        "id": "metadata.balance",
        "group": "metadata",
        "method": "GET",
        "paths": ["/user/balance"],
        "extract": "balance",
    },
    {
        "id": "metadata.error_shape",
        "group": "metadata",
        "method": "POST",
        "paths": ["/chat/completions"],
        "body": {
            "model": "model-fp-definitely-unknown",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        },
        "extract": "error_shape",
        "expected_failure": True,
    },
    {
        "id": "protocol.anthropic",
        "group": "protocol",
        "method": "POST",
        "paths": ["/anthropic/v1/messages", "/v1/messages"],
        "also_raw": True,
        "body": {
            "model": "{model}",
            "max_tokens": 2,
            "messages": [{"role": "user", "content": "hi"}],
        },
        "extract": "anthropic",
    },
    {
        "id": "protocol.stream",
        "group": "protocol",
        "method": "POST",
        "paths": ["/chat/completions"],
        "body": {
            "model": "{model}",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 2,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
        "stream": True,
        "max_events": 40,
        "extract": "stream",
    },
    {
        "id": "protocol.json_object",
        "group": "protocol",
        "method": "POST",
        "paths": ["/chat/completions"],
        "body": {
            "model": "{model}",
            "messages": [{"role": "user", "content": "reply with a json object"}],
            "max_tokens": 2,
            "response_format": {"type": "json_object"},
        },
        "extract": "json_object",
    },
    {
        "id": "protocol.tools",
        "group": "protocol",
        "method": "POST",
        "paths": ["/chat/completions"],
        "body": {
            "model": "{model}",
            "messages": [{"role": "user", "content": "what is the weather in Paris?"}],
            "max_tokens": 2,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather for a city",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
        },
        "extract": "tools",
    },
    {
        "id": "protocol.fim",
        "group": "protocol",
        "method": "POST",
        "paths": ["/beta/completions"],
        "body": {
            "model": "{model}",
            "prompt": "def add(a, b):",
            "suffix": "    return a + b",
            "max_tokens": 2,
            "temperature": 0,
        },
        "extract": "fim",
    },
]


class _Ctx:
    """一次探测的共享现场：传输层、模型名、预算、身份样本。"""

    def __init__(self, transport: Transport, model: str, max_requests: int, log):
        self.t = transport
        self.model = model
        self.max_requests = max_requests
        self.log = log
        self.identity_samples: List[Dict[str, Any]] = []
        self.truncated = False
        self.warnings: List[str] = []
        self.over_budget_prompts: List[str] = []

    def budget_left(self) -> bool:
        return self.t.spend["requests"] < self.max_requests

    def note(self, text: str) -> None:
        self.warnings.append(text)

    def observe(self, resp) -> None:
        sample = _collect_identity(resp, self.model)
        if sample:
            self.identity_samples.append(sample)
        tokens = _usage(resp.body).get("prompt_tokens")
        if isinstance(tokens, int) and tokens > MAX_PROMPT_TOKENS:
            self.over_budget_prompts.append("%s:%s" % (resp.url, tokens))


def _fill(obj: Any, model: str) -> Any:
    if isinstance(obj, dict):
        return {k: _fill(v, model) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, model) for v in obj]
    if obj == "{model}":
        return model
    return obj


def _candidates(probe: Dict[str, Any], prefix: str) -> List[str]:
    paths = [prefix + p for p in probe["paths"]]
    if probe.get("also_raw"):
        paths += [p for p in probe["paths"]]
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _run_simple(ctx: _Ctx, probe: Dict[str, Any]) -> Dict[str, Any]:
    body = _fill(probe.get("body"), ctx.model)
    last = None
    for path in _candidates(probe, ctx.t.prefix):
        if not ctx.budget_left():
            ctx.truncated = True
            return {"id": probe["id"], "skipped": "budget"}
        resp = ctx.t.request(
            probe["method"], path, body,
            stream=bool(probe.get("stream")),
            max_events=int(probe.get("max_events") or 12),
        )
        last = resp
        # 404/405 说明这个候选路径不存在，换下一个
        if resp.status in (404, 405) and path != _candidates(probe, ctx.t.prefix)[-1]:
            continue
        break
    ctx.observe(last)
    data = _extract(probe["extract"], last, ctx.model)
    data["path_used"] = last.url.replace(ctx.t.base_url, "")
    return {
        "id": probe["id"],
        "group": probe["group"],
        "status": last.status,
        "ok": last.ok,
        "expected_failure": bool(probe.get("expected_failure")),
        "ms": round(last.elapsed_ms, 1),
        "data": data,
    }


# ----------------------------------------------------------------------
# tokenizer 指纹
# ----------------------------------------------------------------------
def run_tokenizer(ctx: _Ctx) -> Dict[str, Any]:
    """优先用 /beta/completions 的原始 prompt_tokens（无 chat 模板干扰）；
    端点不支持时退化为 chat 相对计数（减去最小值后同样可比）。"""
    raw_counts: Dict[str, int] = {}
    beta_missing = False
    skipped: List[str] = []
    for text in TOKENIZER_STRINGS:
        if not ctx.budget_left():
            ctx.truncated = True
            break
        resp = ctx.t.request("POST", ctx.t.p("/beta/completions"), {
            "model": ctx.model,
            "prompt": text,
            "max_tokens": 1,
            "temperature": 0,
        })
        if resp.status in (404, 405):
            beta_missing = True
            break
        if not resp.ok:
            # 例如内容风控返回 400：跳过这一个字符串，不要让整组失效
            skipped.append(text)
            continue
        raw_counts[text] = int(_usage(resp.body).get("prompt_tokens") or 0)

    if not beta_missing and len(raw_counts) >= 8:
        return {
            "mode": "raw",
            "counts": raw_counts,
            "vector": _relative(raw_counts),
            "skipped": skipped,
            "note": "completion 原始 prompt 计数（含 1 个 BOS 偏移，向量已归一化）",
            "per_string_budget_ok": not ctx.over_budget_prompts,
        }

    chat_counts: Dict[str, int] = {}
    for text in TOKENIZER_STRINGS:
        if not ctx.budget_left():
            ctx.truncated = True
            break
        resp = ctx.t.request("POST", ctx.t.p("/chat/completions"), {
            "model": ctx.model,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": 1,
            "temperature": 0,
        })
        ctx.observe(resp)
        if not resp.ok:
            chat_counts[text] = -1
            continue
        chat_counts[text] = int(_usage(resp.body).get("prompt_tokens") or 0)
    return {
        "mode": "chat_relative",
        "counts": chat_counts,
        "vector": _relative(chat_counts),
        "note": "端点不支持 /beta/completions，使用 chat 输入长度（已减去常量偏移）",
    }


def _relative(counts: Dict[str, int]) -> Dict[str, int]:
    values = [v for v in counts.values() if isinstance(v, int) and v >= 0]
    if not values:
        return {}
    low = min(values)
    return {k: (v - low) for k, v in counts.items() if isinstance(v, int) and v >= 0}


# ----------------------------------------------------------------------
# 首 token 分布
# ----------------------------------------------------------------------
def run_first_step(ctx: _Ctx) -> Dict[str, Any]:
    logprobs_ok: Optional[bool] = None
    structure: Dict[str, Any] = {}
    samples: List[Dict[str, Any]] = []
    for prefix in FIRST_STEP_PREFIXES:
        if not ctx.budget_left():
            ctx.truncated = True
            break
        body = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": prefix}],
            "max_tokens": 1,
            "temperature": 0,
            "logprobs": True,
            "top_logprobs": 20,
        }
        resp = ctx.t.request("POST", ctx.t.p("/chat/completions"), body)
        ctx.observe(resp)
        if not resp.ok:
            if logprobs_ok is None:
                logprobs_ok = False
                plain = ctx.t.request("POST", ctx.t.p("/chat/completions"), {
                    "model": ctx.model,
                    "messages": [{"role": "user", "content": prefix}],
                    "max_tokens": 1,
                    "temperature": 0,
                })
                ctx.observe(plain)
                message = _choice(plain.body).get("message") or {}
                samples.append({
                    "prefix": prefix,
                    "plain": True,
                    "first_token": _truncate(
                        message.get("reasoning_content") or message.get("content"), 32),
                    "message_keys": sorted(message.keys()) if isinstance(message, dict) else [],
                    "error": error_shape(resp),
                })
            else:
                samples.append({"prefix": prefix, "error": error_shape(resp)})
            continue

        logprobs_ok = True
        choice = _choice(resp.body)
        message = choice.get("message") or {}
        lp = choice.get("logprobs") if isinstance(choice.get("logprobs"), dict) else {}
        structure["keys"] = sorted(lp.keys())
        sample: Dict[str, Any] = {
            "prefix": prefix,
            "message_keys": sorted(message.keys()) if isinstance(message, dict) else [],
            "finish_reason": choice.get("finish_reason"),
            "prompt_tokens": _usage(resp.body).get("prompt_tokens"),
        }
        for container in ("content", "reasoning_content"):
            seq = lp.get(container)
            if isinstance(seq, list) and seq and isinstance(seq[0], dict):
                entry = seq[0]
                sample["logprobs_under"] = container
                structure["entry_keys"] = sorted(entry.keys())
                sample["top1"] = entry.get("token")
                sample["top1_logprob"] = _round(entry.get("logprob"))
                tops = entry.get("top_logprobs")
                if isinstance(tops, list):
                    sample["top"] = [
                        {"t": t.get("token"), "lp": _round(t.get("logprob"))}
                        for t in tops[:5] if isinstance(t, dict)
                    ]
                    structure["top_n"] = len(tops)
                break
        samples.append(sample)

    return {
        "logprobs_supported": logprobs_ok,
        "structure": structure,
        "samples": samples,
    }


def _round(value: Any, ndigits: int = 4) -> Optional[float]:
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# 续写 / 贪心 / 确定性
# ----------------------------------------------------------------------
def run_continuation(ctx: _Ctx) -> Dict[str, Any]:
    samples = []
    for prompt in CONTINUATION_PROMPTS:
        if not ctx.budget_left():
            ctx.truncated = True
            break
        resp = ctx.t.request("POST", ctx.t.p("/beta/completions"), {
            "model": ctx.model,
            "prompt": prompt,
            "max_tokens": 5,
            "temperature": 0,
        })
        if resp.ok:
            choice = _choice(resp.body)
            samples.append({
                "prompt": prompt,
                "text": _truncate(choice.get("text"), 60),
                "finish_reason": choice.get("finish_reason"),
                "prompt_tokens": _usage(resp.body).get("prompt_tokens"),
            })
        else:
            samples.append({"prompt": prompt, "error": error_shape(resp)})
    supported = any("text" in s for s in samples)
    return {"supported": supported, "samples": samples}


def run_chat_greedy(ctx: _Ctx) -> Dict[str, Any]:
    samples = []
    for prompt in CHAT_GREEDY_PROMPTS:
        if not ctx.budget_left():
            ctx.truncated = True
            break
        resp = ctx.t.request("POST", ctx.t.p("/chat/completions"), {
            "model": ctx.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "temperature": 0,
        })
        ctx.observe(resp)
        choice = _choice(resp.body)
        message = choice.get("message") or {}
        samples.append({
            "prompt": prompt,
            "content": _truncate(message.get("content"), 80),
            "reasoning_content": _truncate(message.get("reasoning_content"), 80),
            "finish_reason": choice.get("finish_reason"),
            "message_keys": sorted(message.keys()) if isinstance(message, dict) else [],
            "status": resp.status,
        })
    return {"samples": samples}


def run_determinism(ctx: _Ctx) -> Dict[str, Any]:
    """同参数（温度 0 + seed）连发两次，看是否逐字一致。"""
    body = {
        "model": ctx.model,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "max_tokens": 6,
        "temperature": 0,
        "seed": 7,
    }
    first = ctx.t.request("POST", ctx.t.p("/chat/completions"), body)
    seed_ok = first.ok
    if not first.ok and first.status in (400, 422):
        body.pop("seed")
        body["max_tokens"] = 6
        first = ctx.t.request("POST", ctx.t.p("/chat/completions"), body)
    second = ctx.t.request("POST", ctx.t.p("/chat/completions"), body)
    ctx.observe(first)
    ctx.observe(second)

    def pick(resp):
        message = _choice(resp.body).get("message") or {}
        return (message.get("reasoning_content") or "") + "|" + (message.get("content") or "")

    a, b = pick(first), pick(second)
    return {
        "seed_accepted": seed_ok,
        "identical": a == b,
        "a": _truncate(a, 60),
        "b": _truncate(b, 60),
        "statuses": [first.status, second.status],
    }


# ----------------------------------------------------------------------
# 编排
# ----------------------------------------------------------------------
def run_all(
    transport: Transport,
    model: str,
    groups: Optional[List[str]] = None,
    max_requests: int = 80,
    log=None,
) -> Dict[str, Any]:
    groups = groups or GROUP_ORDER
    ctx = _Ctx(transport, model, max_requests, log or (lambda *_: None))
    started = time.time()
    out: Dict[str, Any] = {}

    if "metadata" in groups or "protocol" in groups:
        for probe in SIMPLE_PROBES:
            if probe["group"] not in groups:
                continue
            out[probe["id"]] = _run_simple(ctx, probe)
            ctx.log("  · %s -> %s" % (probe["id"], out[probe["id"]]["status"]))

    if "tokenizer" in groups:
        out["tokenizer"] = run_tokenizer(ctx)
        ctx.log("  · tokenizer(%s) %d 串" % (
            out["tokenizer"]["mode"], len(out["tokenizer"]["counts"])))

    if "first_step" in groups:
        out["first_step"] = run_first_step(ctx)
        ctx.log("  · first_step logprobs=%s" % out["first_step"]["logprobs_supported"])

    if "continuation" in groups:
        greedy = run_chat_greedy(ctx)
        out["chat_greedy"] = greedy
        continuation = run_continuation(ctx)
        # 推理前缀文本：同族不同权重在这里会明显分叉
        continuation["greedy"] = [
            {"prompt": s.get("prompt"),
             "text": (s.get("reasoning_content") or "") + "|" + (s.get("content") or "")}
            for s in greedy["samples"]
        ]
        out["continuation"] = continuation
        ctx.log("  · continuation supported=%s" % continuation["supported"])

    if "identity" in groups:
        greedy = out.get("chat_greedy") or run_chat_greedy(ctx)
        out["chat_greedy"] = greedy
        identities = ctx.identity_samples
        fingerprints = sorted({s.get("system_fingerprint") for s in identities
                               if s.get("system_fingerprint")})
        id_formats = sorted({s.get("id_format") for s in identities if s.get("id_format")})
        echos = sorted({str(s.get("model_echo")) for s in identities if s.get("model_echo")})
        echo_matches = [s.get("model_echo_matches_request") for s in identities
                        if s.get("model_echo_matches_request") is not None]
        usage_keys = sorted({k for s in identities for k in (s.get("usage_keys") or [])})
        out["identity"] = {
            "system_fingerprints": fingerprints,
            "id_formats": id_formats,
            "model_echoes": echos,
            "model_echo_matches_request": all(echo_matches) if echo_matches else None,
            "usage_keys": usage_keys,
            "samples": len(identities),
        }

    if "determinism" in groups:
        out["determinism"] = run_determinism(ctx)
        ctx.log("  · determinism identical=%s" % out["determinism"]["identical"])

    # chat 模板 + 隐藏 system prompt 的开销：同一段文本，chat 的 prompt_tokens 减去
    # 裸 completion 的 prompt_tokens。不同模型/网关在这里会差出几十个 token，很难伪造。
    first_step = out.get("first_step") or {}
    continuation = out.get("continuation") or {}
    chat_counts = {s.get("prefix"): s.get("prompt_tokens")
                   for s in first_step.get("samples") or []}
    raw_counts = {s.get("prompt"): s.get("prompt_tokens")
                  for s in continuation.get("samples") or []}
    deltas = [
        chat_counts[key] - raw_counts[key]
        for key in chat_counts
        if key in raw_counts
        and isinstance(chat_counts[key], int)
        and isinstance(raw_counts[key], int)
    ]
    if deltas:
        out.setdefault("identity", {})["chat_overhead_tokens"] = round(
            sum(deltas) / len(deltas), 1)
        out["identity"]["chat_overhead_samples"] = deltas

    if ctx.over_budget_prompts:
        out["budget_warnings"] = {
            "over_prompt_limit": ctx.over_budget_prompts[:6],
            "limit": MAX_PROMPT_TOKENS,
        }

    return {
        "groups": out,
        "budget": dict(transport.spend),
        "elapsed_s": round(time.time() - started, 1),
        "truncated": ctx.truncated,
        "warnings": ctx.warnings,
    }
