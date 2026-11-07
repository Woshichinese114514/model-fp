# 贡献指南

感谢参与！本项目体积小、无第三方依赖，改动门槛很低。

## 环境

- Python 3.9+
- 不需要安装任何包（纯标准库）

## 跑测试

```bash
cd scripts
python3 -m unittest discover -s tests -v
```

CI 会在 Python 3.12 上跑同一套测试；单测**不联网**，所有网络交互都通过
`fpkit/client.py` 的 `Transport` 注入，测试里用构造出来的指纹对象。

## 加一组探针（四步）

1. **采集**：在 `fpkit/probes.py` 的 `SIMPLE_PROBES` 里加一条
   （`id` / `group` / `method` / `paths` / `body` / `extract`），
   在 `_extract()` 里写提取逻辑。只保留紧凑字段，不要把整个响应体存进指纹。
2. **复杂逻辑**：需要多请求或依赖前序结果时，写 `run_xxx(ctx)`，在 `run_all()` 里挂上。
3. **打分**：在 `fpkit/compare.py` 加 `score_xxx()`，注册进 `SCORERS`，
   并调整 `DEFAULT_WEIGHTS`（权重和保持为 1）。
4. **测试**：补 `tests/test_compare.py` 用例——同数据得 1.0、关键差异被扣分、缺组能重归一化。

约束（也是本项目的卖点，请别破坏）：

- 单请求 prompt ≤ 64 token、输出 ≤ 16 token；
- 新增探针要能记入 `audit` 的用量账；
- 不要引入第三方依赖。

## 提交规范

采用 [Conventional Commits](https://www.conventionalcommits.org/)：

```text
feat: 新探针 / 新子命令
fix: 修打分或采集逻辑
docs: 只改文档
test: 只改测试
refactor: 不改行为
```

## PR 清单

- [ ] `python3 -m unittest discover -s tests` 全绿
- [ ] 新增/修改探针后，用真实端点跑过一轮 `probe`，并把样例更新到 `examples/`
- [ ] 更新 `CHANGELOG.md`
- [ ] 没有把 key、真实业务 prompt 写进任何文件
