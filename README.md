# model-fp · 模型指纹探测与对比

> 小输入、低成本地判断：这个 API 背后的模型，还是不是原来那个？

判断的是**身份**（同一模型/同一权重/同一后端），不是"模型好不好用"。
纯 Python 标准库，无第三方依赖，Python 3.9+。

## 它回答什么问题

- 中转站号称 deepseek-chat，实际是不是同一个模型？
- 两个 key、两个地址，是否打到同一个后端 / 同一份权重？
- 同名模型有没有被悄悄换成旧版本或量化版？
- 当回归监控：每天跑一次，指纹漂了就报警。

## 快速开始

```bash
git clone https://github.com/Woshichinese114514/model-fp.git
cd model-fp

export DEEPSEEK_API_KEY=sk-xxx

# 1) 采集基线
python3 scripts/model_fp.py probe --base-url https://api.deepseek.com \
    --model deepseek-flash --label official

# 2) 采集待测端点
python3 scripts/model_fp.py probe --base-url https://relay.example.com/v1 \
    --model deepseek-chat --label relay

# 3) 对比
python3 scripts/model_fp.py compare \
    baselines/official-deepseek-flash.json baselines/relay-deepseek-chat.json
```

回归校验（退出码 0=通过，1=漂移）：

```bash
python3 scripts/model_fp.py verify \
    --baseline baselines/official-deepseek-flash.json \
    --base-url https://relay.example.com/v1 --model deepseek-chat --threshold 0.90
```

## 作为 DSH skill 安装

本仓库根目录就是 skill 本体（`SKILL.md` + `scripts/` + `references/`）：

```bash
cp -r model-fp ~/.dsh/skills/model-fingerprint
```

之后新会话里直接说"帮我看看这个中转是不是真 DeepSeek"，技能会被自动加载。

## 7 组指纹

| 组 | 信号 | 请求 | 分辨力 |
|---|---|---|---|
| identity | system_fingerprint、响应 id 格式、model 回显、**隐藏提示词开销** | 4~6 | 同后端最强 |
| tokenizer | 14 个短串在 /beta/completions 的 prompt_tokens 向量 | 15 | 换词表立刻现形 |
| first_step | 固定前缀温度 0 的 top_logprobs（推理首 token） | 4 | 换权重会变 |
| continuation | 温度 0 续写文本 + 推理前缀（只比稳定前缀） | 6 | 换量化/版本会分叉 |
| protocol | SSE 字段、json_object、tools、FIM、Anthropic 面、usage 字段 | 7 | 网关改写会暴露 |
| metadata | /models 全字段、/user/balance、错误模板 | 4 | 官方 vs 中转 |
| determinism | 同请求两次是否逐字一致 | 2 | 采样/缓存层 |

**成本**：一轮 34 个请求 ≈ 840 prompt tokens / 80 completion tokens / 25~35 秒。
单请求 prompt 按构造 ≤64 token、输出 ≤16 token；指纹里的 `audit` 字段逐条记录用量。

## 结论怎么读

| 相似度 | 结论 | 处置 |
|---|---|---|
| ≥95% | 指纹一致 | 同一模型、同一权重、同一后端 |
| 85~95% | 高度相似 | 同族可测差异：看分组得分定位（版本/量化/网关改写） |
| 65~85% | 相关但不一致 | 人工复核分组明细与 notes |
| <65% | 指纹不同 | 不像同一个模型 |

- 置信度由强证据组（identity/tokenizer/first_step）是否可比决定，缺组自动降级。
- 同模型重复探测实测 95~99%（续写/推理文本有非确定性），阈值建议 0.90~0.95。

## 实测（DeepSeek 官方端点，2026-11）

| 对比 | 分数 | 结论 |
|---|---|---|
| deepseek-flash 连测两次 | 98.8% | 指纹一致 |
| deepseek-flash vs deepseek-v4-pro | 75.7% | 相关但不一致 |

示例见 `examples/`。

## 探测中发现的 3 个真问题（都已在代码里处理）

1. **隐藏 system prompt**：v4-pro 的 chat 请求比同文本裸 completion 多 **82** 个 prompt token，
   flash 只多 **29**（连测三次都是 29/82）。差 53 个 token = 服务端注入的隐藏指令，
   比 logprobs 更难伪造，已固化为 `chat_overhead_tokens`。
2. **内容风控误伤纯文本**：`/beta/completions` 送"中华人民共和国"会 400 `Content Exists Risk`，
   原本会打断整组 tokenizer 探针 → 改为逐串跳过 + 中性样本。
3. **reasoning 并非完全确定**：即使 `temperature=0 + seed`，推理 token 约 10 个之后仍会分叉
   → 推理文本只比稳定前缀、只给 0.2 权重，避免把噪声当模型差异。

细节见 `references/known-quirks.md`。

## 局限

- `system_fingerprint` 是"自报"的，网关可改写/删除，不单独定案。
- tokenizer 只辨认词表差异；同词表不同版本靠概率分布与续写。
- 端点若剥离 logprobs / 禁用 /beta/completions，对应组标"不可比"并自动降权，置信度下降。
- 只判断"是不是同一个"，不评价质量。

## 安全

- key 只从 `--api-key` / 环境变量 / `~/.config/model-fp/key` 读取，不写进指纹文件。
- 探针内容都是固定短串，不涉及业务数据；指纹只存协议字段与 token 计数。
- 仓库里 `.env` 已 gitignore。

## 目录

```
model-fp/
├── SKILL.md               # DSH skill 定义（触发词、用法、坑）
├── scripts/
│   ├── model_fp.py        # CLI 入口
│   ├── fpkit/             # 传输层 / 探针 / 指纹 / 打分 / 报告
│   └── tests/             # 11 个离线单测
├── references/
│   ├── probe-design.md    # schema、打分公式、如何加探针
│   └── known-quirks.md    # 已踩过的坑与实测数据
└── examples/              # 实测指纹与报告样例
```

## License

MIT
