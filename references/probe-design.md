# 探针设计与打分公式

## 指纹 JSON 结构（schema: model-fp/1）

```json
{
  "schema": "model-fp/1",
  "tool_version": "1.0.0",
  "captured_at": "2026-11-07T17:19:00+0800",
  "target": {"label": "official", "base_url": "https://api.deepseek.com",
             "api_prefix": "", "model": "deepseek-flash"},
  "groups_used": ["metadata", "protocol", "tokenizer", "first_step",
                  "continuation", "identity", "determinism"],
  "groups": {
    "metadata.models":    {"status": 200, "data": {"models": [...], "top_keys": [...]}},
    "metadata.balance":   {"status": 200, "data": {"present": true, "currencies": ["CNY"]}},
    "metadata.error_shape": {"status": 400, "data": {"message_template": "...", "names_in_message": [...]}},
    "protocol.stream":    {"status": 200, "data": {"chunk_object": "...", "delta_keys": [...]}},
    "protocol.json_object": {"status": 200, "data": {"accepted": true}},
    "protocol.tools":     {"status": 200, "data": {"accepted": true}},
    "protocol.fim":       {"status": 200, "data": {"accepted": true}},
    "protocol.anthropic": {"status": 200, "data": {"top_keys": [...], "signature_equals_id": true}},
    "tokenizer":          {"mode": "raw", "counts": {"a": 2, ...}, "vector": {"a": 0, ...}},
    "first_step":         {"logprobs_supported": true, "structure": {...}, "samples": [...]},
    "continuation":       {"supported": true, "samples": [...], "greedy": [...]},
    "identity":           {"system_fingerprints": [...], "id_formats": ["uuid4"],
                           "model_echoes": [...], "usage_keys": [...],
                           "chat_overhead_tokens": 29.0},
    "determinism":        {"identical": true, "seed_accepted": true}
  },
  "cost": {"requests": 34, "prompt_tokens": 836, "completion_tokens": 86, "elapsed_s": 28.7},
  "truncated": false,
  "audit": [{"path": "/chat/completions", "status": 200, "ms": 245.7, "prompt_tokens": 35, "completion_tokens": 1}],
  "hash": "sha256:..."
}
```

`hash` 是去掉 captured_at/cost/audit 后的稳定哈希，用于判断"指纹内容是否变过"。

## tokenizer 向量

两种模式：

- `raw`：`/beta/completions` 直接给裸 prompt，取的 `usage.prompt_tokens`（含 1 个 BOS 偏移）。
- `chat_relative`：端点没有 /beta 时退化，用 chat 的 `prompt_tokens` 再减去最小值，
  常量偏移（chat 模板 + 隐藏 system prompt）正好被消掉，仍然可比。

比较时用**相对向量**（减最小值），所以两种模式可以互相比较。

## 打分公式

总分 = Σ(组权重 × 组得分) / Σ(可比组权重)。不可比的组直接剔除，权重按比例放大。

| 组 | 权重 | 内部构成 |
|---|---|---|
| identity | 0.25 | sfp 0.55 / id 格式 0.10 / model 回显 0.16 / 隐藏开销 0.12 / 前缀 0.12（仅当有身份数据） |
| tokenizer | 0.25 | L1 相似度 0.7 + 逐串一致比例 0.3 |
| continuation | 0.18 | 续写文本 difflib 0.6 + 推理前缀（截 40 字符）0.2 |
| first_step | 0.15 | top1 一致 0.4 + top1 logprob 接近 0.15 + 显著候选接近度 0.30 + 候选集合 Jaccard 0.15 |
| protocol | 0.09 | 协议信号逐项相等比例 |
| metadata | 0.08 | 模型列表 Jaccard 0.7 + 余额接口 0.3 |

first_step 里"显著候选"= logprob ≥ -14 的 top token；logprob 差的容忍度 2.5 nats
（同模型温度 0 的抖动约 0.1~0.6 nats）。低于 -14 的都是尾部噪声，直接忽略。

置信度：强证据组（identity/tokenizer/first_step）可比权重占比 ≥0.6 → high，≥0.3 → medium，否则 low；
low 时 "same/family" 结论自动降级为 "related"。

## 加一个探针

1. 在 `fpkit/probes.py` 的 `SIMPLE_PROBES` 里加一条（id/group/method/paths/body/extract），
   在 `_extract()` 里写提取逻辑，保持"只留紧凑字段"，别把整包 body 存进指纹。
2. 自定义逻辑（多请求、依赖前一个结果）就写 `run_xxx(ctx)`，在 `run_all()` 里挂上。
3. 在 `fpkit/compare.py` 加 `score_xxx()`，注册进 `SCORERS`，并调整 `DEFAULT_WEIGHTS`（和为 1）。
4. 补 `tests/test_compare.py` 的用例：同数据得 1.0、关键差异被扣分、缺组能重归一化。
