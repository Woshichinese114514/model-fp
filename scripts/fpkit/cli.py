# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Woshichinese114514

"""命令行入口：probe / compare / matrix / verify / models。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):  # 允许 python fpkit/cli.py 直接跑
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fpkit import fingerprint, probes
    from fpkit.client import Transport
    from fpkit.compare import compare, matrix
    from fpkit.report import render_compare, render_matrix, render_summary
else:
    from . import fingerprint, probes
    from .client import Transport
    from .compare import compare, matrix
    from .report import render_compare, render_matrix, render_summary

ENV_KEYS = ("MODEL_FP_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


# ----------------------------------------------------------------------
# 通用
# ----------------------------------------------------------------------
def _fail(message: str) -> int:
    print("错误: " + message, file=sys.stderr)
    return 2


def _write_report(path: str, content: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-") or "target"


def _load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_key(args, target_cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if getattr(args, "api_key", None):
        return args.api_key
    for name in ENV_KEYS:
        value = os.environ.get(name)
        if value:
            return value
    if target_cfg:
        env_name = target_cfg.get("api_key_env")
        if env_name and os.environ.get(env_name):
            return os.environ[env_name]
    key_file = os.path.expanduser("~/.config/model-fp/key")
    if os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value
    return None


def _resolve_target(args, config: Dict[str, Any]):
    target_name = getattr(args, "target", None)
    target_cfg: Optional[Dict[str, Any]] = None
    base_url = getattr(args, "base_url", None)
    model = getattr(args, "model", None)
    label = getattr(args, "label", None)
    if target_name:
        target_cfg = (config.get("targets") or {}).get(target_name)
        if target_cfg is None:
            raise SystemExit("错误: config 里没有目标 %r" % target_name)
        base_url = base_url or target_cfg.get("base_url")
        model = model or target_cfg.get("model")
        label = label or target_name
    if not base_url:
        raise SystemExit("错误: 需要 --base-url，或用 --target 指定 config 里的目标")
    return base_url, model, label, target_cfg


def _parse_groups(raw: Optional[str]) -> List[str]:
    if not raw:
        return list(probes.GROUP_ORDER)
    names = [x.strip() for x in raw.split(",") if x.strip()]
    bad = [n for n in names if n not in probes.GROUP_ORDER]
    if bad:
        raise SystemExit("错误: 未知指纹组 %s；可选 %s" % (bad, probes.GROUP_ORDER))
    return names


def _new_transport(args, base_url: str, key: Optional[str]) -> Transport:
    transport = Transport(base_url, key, timeout=args.timeout, retries=args.retries)
    if getattr(args, "api_prefix", None) is not None:
        transport.prefix = args.api_prefix
    else:
        chosen, tried = transport.discover_prefix()
        if chosen is None:
            print("提示: /models 探测失败，试过 %s，继续用空前缀" % (tried,), file=sys.stderr)
    return transport


def _emit_probe(args, base_url: str, key: Optional[str], model: str, label: str,
                groups: List[str]) -> Dict[str, Any]:
    transport = _new_transport(args, base_url, key)
    print("探测 %s @ %s（组：%s）" % (model, base_url, ",".join(groups)), file=sys.stderr)
    result = probes.run_all(
        transport, model, groups=groups,
        max_requests=args.max_requests,
        log=lambda msg: print(msg, file=sys.stderr),
    )
    return fingerprint.build(
        base_url=base_url, model=model, label=label,
        api_prefix=transport.prefix, probe_result=result,
        groups_used=groups, audit=transport.calls,
    )


def _default_out(label: str, model: str) -> str:
    return os.path.join("baselines", "%s-%s.json" % (_slug(label), _slug(model)))


# ----------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------
def cmd_probe(args) -> int:
    config = _load_config(args.config)
    base_url, model, label, target_cfg = _resolve_target(args, config)
    key = _resolve_key(args, target_cfg)
    groups = _parse_groups(args.groups)
    if not key:
        print("提示: 没有 API key，按匿名端点继续", file=sys.stderr)

    if args.all_models:
        transport = _new_transport(args, base_url, key)
        resp = transport.request("GET", transport.p("/models"))
        ids: List[str] = []
        if resp.ok and isinstance(resp.body, dict):
            ids = [m.get("id") for m in resp.body.get("data") or [] if isinstance(m, dict)]
        if not ids:
            return _fail("拿不到模型列表，无法 --all-models")
        print("端点模型：%s" % ", ".join(ids), file=sys.stderr)
        targets = [(mid, "baselines/%s-%s.json" % (_slug(label or "endpoint"), _slug(mid)))
                   for mid in ids]
    else:
        if not model:
            return _fail("需要 --model 或 --all-models")
        targets = [(model, args.out or _default_out(label or "endpoint", model))]

    for mid, out_path in targets:
        fp = _emit_probe(args, base_url, key, mid, label or mid, groups)
        fingerprint.save(out_path, fp)
        print("\n" + render_summary(fp))
        print("\n已保存: %s" % out_path)
        print("-" * 60)
    return 0


def cmd_compare(args) -> int:
    a = fingerprint.load(args.a)
    b = fingerprint.load(args.b)
    res = compare(a, b)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render_compare(res))
    if args.out:
        _write_report(args.out, render_compare(res))
        print("报告已写入: %s" % args.out, file=sys.stderr)
    if args.fail_below is not None and res["score"] < args.fail_below:
        print("相似度 %.4f 低于阈值 %.4f" % (res["score"], args.fail_below), file=sys.stderr)
        return 1
    return 0


def cmd_matrix(args) -> int:
    fps = [fingerprint.load(p) for p in args.files]
    mat = matrix(fps)
    print(render_matrix(mat))
    if args.out:
        _write_report(args.out, render_matrix(mat))
        print("报告已写入: %s" % args.out, file=sys.stderr)
    return 0


def cmd_verify(args) -> int:
    baseline = fingerprint.load(args.baseline)
    config = _load_config(args.config)
    base_url, model, label, target_cfg = _resolve_target(args, config)
    key = _resolve_key(args, target_cfg)
    model = model or (baseline.get("target") or {}).get("model")
    if not model:
        return _fail("缺少 --model")

    fresh = _emit_probe(args, base_url, key, model, label or "current",
                        list(probes.GROUP_ORDER))
    res = compare(baseline, fresh)
    print(render_compare(res))
    if args.save:
        fingerprint.save(args.save, fresh)
        print("\n本次指纹已保存: %s" % args.save, file=sys.stderr)
    if res["score"] < args.threshold:
        print("验证失败: %.4f < %.4f" % (res["score"], args.threshold), file=sys.stderr)
        return 1
    print("验证通过: %.4f >= %.4f" % (res["score"], args.threshold), file=sys.stderr)
    return 0


def cmd_models(args) -> int:
    config = _load_config(args.config)
    base_url, model, label, target_cfg = _resolve_target(args, config)
    key = _resolve_key(args, target_cfg)
    transport = _new_transport(args, base_url, key)
    resp = transport.request("GET", transport.p("/models"))
    if not resp.ok:
        return _fail("GET /models 失败: HTTP %s %s" % (resp.status, resp.body))
    data = (resp.body or {}).get("data") or []
    for item in data:
        if not isinstance(item, dict):
            continue
        extra = []
        for field in ("name", "context_window", "max_output_tokens"):
            if item.get(field) is not None:
                extra.append("%s=%s" % (field, item[field]))
        print("%-24s %s" % (item.get("id"), " ".join(extra)))
    return 0


# ----------------------------------------------------------------------
# argparse
# ----------------------------------------------------------------------
def _add_net_args(p) -> None:
    p.add_argument("--api-key",
                   help="API key（默认读 MODEL_FP_API_KEY / DEEPSEEK_API_KEY 等环境变量）")
    p.add_argument("--config", default=None, help="config.json 路径（配合 --target）")
    p.add_argument("--timeout", type=float, default=60.0, help="单请求超时秒数")
    p.add_argument("--retries", type=int, default=2, help="失败重试次数")


def _add_target_args(p) -> None:
    p.add_argument("--target", help="config.json 里的目标名")
    p.add_argument("--base-url", help="例如 https://api.deepseek.com 或 https://relay/v1")
    p.add_argument("--api-prefix", default=None,
                   help="强制 API 前缀（默认自动探测 空 或 /v1）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model_fp",
        description="模型指纹探测与对比：小输入、低成本地判断是不是同一个模型。",
    )
    parser.add_argument("--version", action="version",
                        version="model-fp " + fingerprint.TOOL_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="探测一个端点/模型，生成指纹 JSON")
    _add_net_args(p)
    _add_target_args(p)
    p.add_argument("--model", help="模型名，例如 deepseek-flash")
    p.add_argument("--label", help="指纹展示名，例如 official / relay")
    p.add_argument("--groups", help="只跑部分组，逗号分隔，如 identity,tokenizer")
    p.add_argument("--max-requests", type=int, default=80, help="请求上限（成本护栏）")
    p.add_argument("--all-models", action="store_true", help="探测 /models 列出的所有模型")
    p.add_argument("--out", help="输出路径（默认 baselines/<label>-<model>.json）")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("compare", help="对比两个指纹 JSON")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.add_argument("--out", help="把 Markdown 报告写入文件")
    p.add_argument("--fail-below", type=float, default=None,
                   help="低于该相似度则以退出码 1 结束（便于接 CI/cron）")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("matrix", help="多个指纹两两对比矩阵")
    p.add_argument("files", nargs="+")
    p.add_argument("--out", help="把 Markdown 报告写入文件")
    p.set_defaults(func=cmd_matrix)

    p = sub.add_parser("verify", help="现场探测并与基线比对（可作回归监控）")
    _add_net_args(p)
    _add_target_args(p)
    p.add_argument("--baseline", required=True, help="基线指纹 JSON")
    p.add_argument("--model", help="默认取基线里的模型名")
    p.add_argument("--label", default=None)
    p.add_argument("--max-requests", type=int, default=80)
    p.add_argument("--threshold", type=float, default=0.90,
                   help="通过阈值（默认 0.90；同模型实测通常 95~99%%）")
    p.add_argument("--save", help="把本次指纹另存为文件")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("models", help="列出端点上的模型")
    _add_net_args(p)
    _add_target_args(p)
    p.set_defaults(func=cmd_models)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        return _fail("找不到文件: %s" % exc)
    except ValueError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
