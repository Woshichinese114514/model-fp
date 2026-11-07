---
name: model-fingerprint
description: 模型指纹探测与对比：用极小输入（每轮约 34 个请求、840 prompt token）判断某个 API 端点背后的模型是不是"同一个"——中转站是否真在跑 deepseek-chat、两个 key 是否同一后端、同名模型有没有被悄悄换权重/量化，以及做每日回归监控。内置 7 组指纹：system_fingerprint/响应 id/隐藏提示词开销、tokenizer 计数向量、首 token top_logprobs、温度 0 续写、协议怪癖、端点元数据、确定性。当用户问「这个 API 是真 DeepSeek 吗」「这个中转有没有换模型」「两个地址是不是同一个模型」「模型有没有被降级/量化」「帮我监控模型是否被替换」时使用。Fingerprint and compare an OpenAI/DeepSeek/Anthropic-style endpoint to tell whether it is really the same model.
whenToUse: 用户想知道某个 API/中转背后到底是什么模型、是否和某个基线是同一个模型、是否被换成量化/旧版本，或想做模型回归监控。需要联网调用该端点。只做身份判断，不做越狱或提示词提取；不评价模型"好不好用"。
---

# 模型指纹探测与对比（model-fp）

一个纯标准库 Python 工具：对任意 OpenAI/DeepSeek/Anthropic 风格端点做一次**小输入**探测，
生成可存档的指纹 JSON，再把两份指纹对比出结论。

判定的是：**是不是同一个模型 / 同一份权重 / 同一个后端**，不是"模型好不好用"。

## 快速开始

脚本就在本技能目录下（下文 `$SKILL_DIR` 指本 SKILL.md 所在目录）：

```bash
# 1) 采集基线（官方端点）
DEEPSEEK_API_KEY=sk-xxx python3 "$SKILL_DIR/scripts/model_fp.py" probe \
  --base-url https://api.deepseek.com --model deepseek-flash --label official

# 2) 采集待测端点（中转 / 另一个 key）
python3 "$SKILL_DIR/scripts/model_fp.py" probe \
  --base-url https://relay.example.com/v1 --model deepseek-chat --label relay

# 3) 对比
python3 "$SKILL_DIR/scripts/model_fp.py" compare \
  baselines/official-deepseek-flash.json baselines/relay-deepseek-chat.json
```

一条命令做回归校验（退出码 0=通过，1=漂移）：

```bash
python3 "$SKILL_DIR/scripts/model_fp.py" verify \
  --baseline baselines/official-deepseek-flash.json \
  --base-url https://relay.example.com/v1 --model deepseek-chat \
  --threshold 0.90
```

key 来源优先级：`--api-key` > 环境变量（MODEL_FP_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY）> `~/.config/model-fp/key`。

## 子命令

| 命令 | 作用 |
|---|---|
| probe | 探测端点，写 `baselines/<label>-<model>.json` |
| compare | 两份指纹对比，Markdown 报告，`--json` 出机器可读结果 |
| matrix | 多份指纹两两矩阵（官方 vs 多个中转） |
| verify | 现场探测 + 比基线，按阈值给退出码（接 cron） |
| models | 列出端点模型清单 |

常用参数：`--groups identity,tokenizer`（只跑部分组，约 18 个请求）、
`--all-models`、`--max-requests`（成本上限）、`--target official`（读 config.json）。

## 测了哪些指纹

| 组 | 信号 | 请求 | 分辨力 |
|---|---|---|---|
| identity | system_fingerprint、响应 id 格式、model 回显、**隐藏提示词开销** | 4~6 | 同后端最强 |
| tokenizer | 14 个短串在 /beta/completions 的 prompt_tokens 向量 | 15 | 换词表立刻现形 |
| first_step | 固定前缀温度 0 的 top_logprobs（推理首 token） | 4 | 换权重会变 |
| continuation | 温度 0 续写文本 + 推理前缀（只比稳定前缀） | 6 | 换量化/版本会分叉 |
| protocol | SSE 字段、json_object、tools、FIM、Anthropic 面、usage 字段 | 7 | 网关改写会暴露 |
| metadata | /models 全字段、/user/balance、错误模板 | 4 | 官方 vs 中转 |
| determinism | 同请求两次是否逐字一致 | 2 | 采样/缓存层 |

成本：一轮 **34 个请求 ≈ 840 prompt tokens / 80 completion tokens / 25~35 秒**。
单请求 prompt 按构造 ≤ 64 token、输出 ≤ 16 token；指纹里的 `audit` 字段逐条记录用量。

## 怎么读结论

| 相似度 | 结论 | 处置 |
|---|---|---|
| ≥95% | 指纹一致 | 同一模型、同一权重、同一后端 |
| 85~95% | 高度相似 | 同族但可测差异：看分组得分定位（版本/量化/网关改写） |
| 65~85% | 相关但不一致 | 部分吻合，人工复核分组明细与 notes |
| <65% | 指纹不同 | 不像同一个模型 |

- 置信度 high/medium/low 由强证据组（identity/tokenizer/first_step）是否可比决定；缺组会自动降级结论。
- 同模型重复探测实测 **95~99%**（续写/推理文本本身有非确定性），阈值建议 0.90~0.95。
- 重点看报告里的「可疑点 / 提示」：它会把 sfp 不一致、model 回显被改写、缺 /user/balance、缺 logprobs 等逐条列出。

## 已知坑（都已在代码里处理，改动前先看 references/known-quirks.md）

1. **内容风控会误伤纯文本**：`/beta/completions` 送某些中文短语会 400 `Content Exists Risk`，
   整组 tokenizer 探针会被打断 → 已改为逐串跳过。
2. **reasoning 不是完全确定**：即使 temperature=0 + seed，推理 token 约 10 个后仍会分叉
   → 推理文本只比稳定前缀、只给 0.2 权重。
3. **隐藏 system prompt**：不同模型 chat 请求的 prompt 开销差很多（实测 DeepSeek flash 29 vs
   v4-pro 82 token）→ 已作为 `chat_overhead_tokens` 纳入 identity。
4. 网关若剥离 logprobs / 禁用 `/beta/completions`，对应组会标「不可比」并自动降权，别硬凑结论。

## 常见工作流

- **怀疑中转换模型**：probe 中转 → probe 官方（同 model 名）→ compare；先看 identity 与 tokenizer 两组。
- **两个 key 是否同一后端**：各 probe 一次 → compare，分数 ≥95% 基本同源。
- **模型被偷偷降级/量化**：与历史基线 compare，重点看 first_step + continuation。
- **每天监控**：cron 里跑 verify，`--threshold 0.90`，失败再人工跑 compare 出报告。

## 文件

- `scripts/model_fp.py` 入口；`scripts/fpkit/` 传输层/探针/指纹/打分/报告
- `scripts/tests/test_compare.py` 11 个离线单测（`python3 -m unittest discover -s tests`）
- `references/probe-design.md` 指纹 schema、打分公式、如何加探针
- `references/known-quirks.md` 已踩过的坑与实测数据
- `examples/baselines/` 三份实测指纹；`examples/matrix.md` 示例报告

## 安全

- key 只从参数/环境变量/`~/.config/model-fp/key` 读，不写进指纹文件；别把 key 提交到仓库。
- 探针内容都是固定短串，不涉及用户业务数据；指纹只存协议字段与 token 计数。
