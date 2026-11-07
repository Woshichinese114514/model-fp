# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.1] - 2026-11-07

### Fixed

- 文档里的请求数账本：完整探测 **34** 个请求（identity 为派生组、不单独发请求；tokenizer 14、protocol 5、metadata 3），此前误写成 4~6/15/7/4，合计对不上。
- 精简模式实测值更正：`--groups metadata,tokenizer` = **17**，加 identity 取样 = **19**（原文写 18）。
- 前缀探测的 `GET /models` 不再从审计中剔除，改为被 metadata.models 探针复用：审计请求数现在等于真实请求数（1.0.0 少算 1 个）。

### Changed

- `TOOL_VERSION` → 1.0.1；完整探测仍为 34 个请求、~840 prompt token。

## [1.0.0] - 2026-11-07

### Added

- 7 组模型指纹探针：`identity`、`tokenizer`、`first_step`、`continuation`、
  `protocol`、`metadata`、`determinism`。
- 5 个子命令：`probe` / `compare` / `matrix` / `verify` / `models`。
- 分组加权打分与置信度：不可比组自动剔除并重归一化权重；置信度不足时自动降级结论。
- 指纹稳定哈希（忽略采集时间与成本字段），可用于判断"内容是否变过"。
- 逐请求 `audit` 用量审计字段。
- 11 个离线单测（不联网）。
- DSH skill 包装：`SKILL.md` + `references/` + `examples/`。
- GitHub Actions CI（Python 3.12 跑单测）。

### Measured

- 同一端点重复探测 `deepseek-flash`：98.8%（指纹一致）。
- `deepseek-flash` vs `deepseek-v4-pro`：75.7%（相关但不一致）。
- 一轮完整探测：34 个请求、~840 prompt tokens、~80 completion tokens、25~35 秒。

### Notes

- 探测中发现：DeepSeek v4-pro 在服务端注入了隐藏 system prompt（chat 比裸 completion 多 82
  个 prompt token，flash 为 29），已作为 `chat_overhead_tokens` 纳入 identity 组。
- 内容风控会对部分纯文本探针返回 400 `Content Exists Risk`，已改为逐串跳过。
- reasoning token 在温度 0 下约 10 个后仍会分叉，故推理文本只比稳定前缀并降权。
