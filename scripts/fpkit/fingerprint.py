"""指纹对象：构建、规范化、稳定哈希、读写。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

SCHEMA = "model-fp/1"
TOOL_VERSION = "1.0.0"


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def _hashable(fp: Dict[str, Any]) -> Dict[str, Any]:
    skip = {"hash", "captured_at", "cost", "audit"}
    return {k: v for k, v in fp.items() if k not in skip}


def build(
    *,
    base_url: str,
    model: str,
    label: Optional[str],
    api_prefix: str,
    probe_result: Dict[str, Any],
    groups_used,
    audit=None,
) -> Dict[str, Any]:
    """把 run_all 的原始结果封装成可存档、可对比的指纹对象。"""
    cost = probe_result.get("budget") or {}
    fp: Dict[str, Any] = {
        "schema": SCHEMA,
        "tool_version": TOOL_VERSION,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "target": {
            "label": label or model,
            "base_url": base_url,
            "api_prefix": api_prefix,
            "model": model,
        },
        "groups_used": list(groups_used),
        "groups": probe_result.get("groups") or {},
        "cost": {
            "requests": int(cost.get("requests") or 0),
            "prompt_tokens": int(cost.get("prompt_tokens") or 0),
            "completion_tokens": int(cost.get("completion_tokens") or 0),
            "elapsed_s": probe_result.get("elapsed_s"),
        },
        "truncated": bool(probe_result.get("truncated")),
        "warnings": probe_result.get("warnings") or [],
    }
    if audit:
        fp["audit"] = list(audit)[:120]
    fp["hash"] = stable_hash(_hashable(fp))
    return fp


def save(path: str, fp: Dict[str, Any]) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fp, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def load(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        fp = json.load(fh)
    if not isinstance(fp, dict) or "groups" not in fp:
        raise ValueError("不是合法的 model-fp 指纹文件: %s" % path)
    return fp


def title(fp: Dict[str, Any]) -> str:
    target = fp.get("target") or {}
    return "%s (%s)" % (target.get("label") or "?", target.get("model") or "?")


def signals(fp: Dict[str, Any]) -> Dict[str, Any]:
    """抽出人类最关心的几个身份信号。"""
    groups = fp.get("groups") or {}
    identity = groups.get("identity") or {}
    models = ((groups.get("metadata.models") or {}).get("data") or {}).get("models") or []
    return {
        "base_url": (fp.get("target") or {}).get("base_url"),
        "model": (fp.get("target") or {}).get("model"),
        "system_fingerprint": (identity.get("system_fingerprints") or [None])[0],
        "id_formats": identity.get("id_formats"),
        "models_on_endpoint": [m.get("id") for m in models][:8],
        "tokenizer_mode": (groups.get("tokenizer") or {}).get("mode"),
        "logprobs": (groups.get("first_step") or {}).get("logprobs_supported"),
        "stream_ok": ((groups.get("protocol.stream") or {}).get("data") or {}).get("status") == 200,
        "balance_present": ((groups.get("metadata.balance") or {}).get("data") or {}).get("present"),
        "deterministic_at_t0": (groups.get("determinism") or {}).get("identical"),
        "requests": (fp.get("cost") or {}).get("requests"),
        "prompt_tokens": (fp.get("cost") or {}).get("prompt_tokens"),
        "file_hash": fp.get("hash"),
    }
