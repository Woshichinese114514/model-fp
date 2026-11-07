# 已踩过的坑与实测数据（DeepSeek 官方端点，2026-11）

## 1. 内容风控会误伤"纯文本"探针

`/beta/completions` 送 `中华人民共和国` 会返回：

```json
{"error": {"message": "Content Exists Risk (request_id: ...)", "type": "invalid_request_error"}}
```

后果：tokenizer 组原本一旦某串失败就整组退化，白花 14 个请求。
现在：`run_tokenizer()` 对单串 400 直接跳过（记入 `skipped`），只有 404/405 才判定端点无此能力。

## 2. 隐藏 system prompt（很好用的指纹）

对同一段文本：chat 的 `usage.prompt_tokens` 减去 `/beta/completions` 的计数 = chat 模板 + 隐藏 system prompt：

| 模型 | 开销 | 稳定性 |
|---|---|---|
| deepseek-flash | 29 | 连测三次都是 29 |
| deepseek-v4-pro | 82 | 连测三次都是 82 |

另有实测：v4-pro 在 `/anthropic/v1/messages` 上同样多 53 个 input token，说明是服务端注入，不是客户端模板。
这个差值很难伪造，已纳入 identity 组。

## 3. reasoning 文本在温度 0 下也不完全确定

同 prompt、`temperature=0`、甚至 `seed=7`，连发两次：

```
run1: 'We need answer one word. 2+2=4, word "four'
run2: 'We need answer one word. 2+2=4. Should answer one'
```

前 ~10 个 token 稳定，之后分叉。因此 continuation 组只比推理文本前 40 字符，且权重 0.2；
`/beta/completions` 的纯续写文本（不带推理）稳定得多，权重 0.6。

## 4. 协议面（DeepSeek 官方端点实测）

- 响应 id 是 uuid4（不是 `chatcmpl-` 前缀）
- 所有模型默认先出 `reasoning_content`，`content` 为空时 `finish_reason=length`
- 支持 `logprobs` + `top_logprobs`（20 个候选），分布挂在 `logprobs.reasoning_content` 下
- 支持 `/beta/completions`、`suffix` FIM、`response_format.json_object`（要求 prompt 含 "json"）
- 同时提供 Anthropic 兼容面 `/anthropic/v1/messages`（thinking 块的 signature 直接复用 message id）
- usage 同时有 `prompt_tokens_details.cached_tokens` 和 `prompt_cache_hit_tokens` / `miss`
- 流式 chunk 带 `system_fingerprint`，`stream_options.include_usage` 生效

## 5. 实测分数（可当回归基准）

| 对比 | 分数 | 结论 |
|---|---|---|
| deepseek-flash 连测两次 | 98.8% | 指纹一致 |
| deepseek-flash vs v4-pro | 75.7% | 相关但不一致 |
| 隔一段时间再 verify flash | 95.3% | 指纹一致（波动来自续写文本） |

分组看：同一模型 identity/tokenizer 100%，continuation 76~100%（看是否踩到非确定的那条）。
