# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Woshichinese114514

"""model-fp 传输层：仅用 Python 标准库，兼容 OpenAI / DeepSeek / Anthropic 风格端点。

设计要点
- 每个请求都记录状态码、耗时、token 用量，便于统计"探测开销"。
- 失败不抛异常：失败状态本身也是指纹（例如端点不支持 logprobs 会返回 400）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# 只保留能暴露"这是哪家网关/框架"的响应头
INTERESTING_HEADERS = (
    "server", "via", "x-powered-by", "content-type",
    "x-request-id", "x-ds-trace-id", "x-oneapi-request-id",
    "cf-ray", "x-vllm", "x-litellm-model-id", "x-new-api-request-id",
    "x-ratelimit-limit-requests", "x-envoy-upstream-service-time", "x-cache",
)


@dataclass
class Response:
    status: int
    body: Any
    headers: Dict[str, str]
    elapsed_ms: float
    url: str
    error: Optional[str] = None
    events: Optional[List[str]] = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())


class Transport:
    """极简 HTTP 客户端：自动重试、记录用量、支持 SSE 采样。"""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
        retries: int = 2,
        backoff: float = 0.7,
        user_agent: str = "model-fp/1.0",
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.user_agent = user_agent
        self.extra_headers = dict(extra_headers or {})
        self.prefix = ""  # API 前缀 "" 或 "/v1"，由 discover_prefix 决定
        self.calls: List[Dict[str, Any]] = []
        self.spend = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def url(self, path: str) -> str:
        return self.base_url + path

    def p(self, path: str) -> str:
        """在路径前拼上已发现的 API 前缀。"""
        return self.prefix + path

    def discover_prefix(self, candidates=("", "/v1")):
        """找到能返回 /models 的 API 前缀（"" 或 "/v1"）。"""
        tried = []
        for cand in candidates:
            self.prefix = cand
            r = self.request("GET", cand + "/models")
            tried.append({"prefix": cand, "status": r.status})
            if r.ok and isinstance(r.body, dict) and isinstance(r.body.get("data"), list):
                self.prefix = cand
                self.calls.pop()  # 该请求由 probes 重新记录，避免重复计数
                self.spend["requests"] -= 1
                return tried[-1], tried
        self.prefix = ""
        return None, tried

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        stream: bool = False,
        max_events: int = 8,
    ) -> Response:
        url = self.url(path)
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if self.api_key:
            hdrs["Authorization"] = "Bearer " + self.api_key
        hdrs.update(self.extra_headers)
        hdrs.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"

        attempt = 0
        while True:
            t0 = time.time()
            events: Optional[List[str]] = None
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            try:
                resp = urllib.request.urlopen(req, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as exc:  # 4xx/5xx 也带响应体
                elapsed = (time.time() - t0) * 1000.0
                raw = exc.read() or b""
                parsed = self._parse(raw)
                resp_headers = self._filter_headers(exc.headers)
                self._record(path, exc.code, elapsed, parsed)
                if exc.code in RETRY_STATUS and attempt < self.retries:
                    attempt += 1
                    time.sleep(self.backoff * attempt)
                    continue
                return Response(exc.code, parsed, resp_headers, elapsed, url,
                                error="http_%s" % exc.code)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                elapsed = (time.time() - t0) * 1000.0
                if attempt < self.retries:
                    attempt += 1
                    time.sleep(self.backoff * attempt)
                    continue
                self._record(path, -1, elapsed, None)
                return Response(-1, None, {}, elapsed, url,
                                error=type(exc).__name__ + ": " + str(exc)[:120])

            try:
                with resp:
                    elapsed = (time.time() - t0) * 1000.0
                    resp_headers = self._filter_headers(resp.headers)
                    if stream:
                        events = self._read_sse(resp, max_events)
                        parsed = self._last_json_event(events)
                    else:
                        parsed = self._parse(resp.read())
                    status = resp.status
            except (TimeoutError, OSError) as exc:
                elapsed = (time.time() - t0) * 1000.0
                if attempt < self.retries:
                    attempt += 1
                    time.sleep(self.backoff * attempt)
                    continue
                self._record(path, -1, elapsed, None)
                return Response(-1, None, {}, elapsed, url,
                                error=type(exc).__name__ + ": " + str(exc)[:120])

            self._record(path, status, elapsed, parsed)
            if status in RETRY_STATUS and attempt < self.retries:
                attempt += 1
                time.sleep(self.backoff * attempt)
                continue
            return Response(status, parsed, resp_headers, elapsed, url, events=events)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _record(self, path: str, status: int, elapsed_ms: float, parsed: Any) -> None:
        usage = self._usage(parsed)
        self.spend["requests"] += 1
        self.spend["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.spend["completion_tokens"] += usage.get("completion_tokens", 0)
        self.calls.append(
            {
                "path": path,
                "status": status,
                "ms": round(elapsed_ms, 1),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            }
        )

    @staticmethod
    def _usage(parsed: Any) -> Dict[str, int]:
        if not isinstance(parsed, dict):
            return {}
        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            return {}
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        try:
            return {"prompt_tokens": int(prompt or 0), "completion_tokens": int(completion or 0)}
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _parse(raw: bytes) -> Any:
        if not raw:
            return None
        text = raw.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except ValueError:
            return text[:400]

    @staticmethod
    def _last_json_event(events: Optional[List[str]]) -> Any:
        for line in reversed(events or []):
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        return json.loads(payload)
                    except ValueError:
                        continue
        return None

    @staticmethod
    def _read_sse(resp, limit: int) -> List[str]:
        out: List[str] = []
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            out.append(s)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _filter_headers(headers) -> Dict[str, str]:
        picked: Dict[str, str] = {}
        try:
            items = headers.items()
        except AttributeError:
            return picked
        for name, value in items:
            low = name.lower()
            if low in INTERESTING_HEADERS or low.startswith(INTERESTING_HEADERS):
                picked[low] = str(value)[:200]
        return picked
