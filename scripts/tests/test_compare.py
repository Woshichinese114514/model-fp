# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Woshichinese114514

"""离线单测：只验证打分/结论逻辑，不发任何网络请求。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpkit import fingerprint  # noqa: E402
from fpkit.compare import compare, matrix  # noqa: E402

VECTOR = {"a": 0, "hello": 0, "Hello, world!": 3, "中文分词测试": 3}


def make_fp(sfp="bc19", model="deepseek-flash", overhead=29.0, vector=None,
            top1="We", logprobs=True, cont=" Paris.", label=None, prefix=""):
    vector = vector or VECTOR
    return {
        "schema": "model-fp/1",
        "target": {"label": label or model, "base_url": "https://x", "model": model,
                   "api_prefix": prefix},
        "groups": {
            "identity": {
                "system_fingerprints": [sfp] if sfp else [],
                "id_formats": ["uuid4"],
                "model_echoes": [model],
                "usage_keys": ["prompt_tokens"],
                "chat_overhead_tokens": overhead,
            },
            "tokenizer": {"mode": "raw", "counts": vector, "vector": vector},
            "first_step": {
                "logprobs_supported": logprobs,
                "samples": [{
                    "prefix": "p1", "top1": top1, "top1_logprob": -1e-7,
                    "top": [{"t": top1, "lp": -1e-7}, {"t": "The", "lp": -16.0}],
                }],
            },
            "continuation": {"supported": True,
                             "samples": [{"prompt": "p1", "text": cont}],
                             "greedy": []},
            "protocol": {
                "stream": {"data": {"chunk_object": "chat.completion.chunk",
                                    "delta_keys": ["content"], "final_has_usage": True}},
                "json_object": {"data": {"accepted": True}},
                "tools": {"data": {"accepted": True}},
                "fim": {"data": {"accepted": True}},
                "anthropic": {"data": {"status": 200}},
            },
            "metadata.models": {"data": {"models": [{"id": model}]}},
            "metadata.balance": {"data": {"present": True}},
        },
        "cost": {"requests": 1},
        "hash": "sha256:" + "0" * 8,
    }


class CompareTest(unittest.TestCase):
    def test_identical_is_same(self):
        res = compare(make_fp(), make_fp())
        self.assertAlmostEqual(res["score"], 1.0)
        self.assertEqual(res["verdict"]["band"], "same")
        self.assertEqual(res["confidence"], "high")

    def test_system_fingerprint_mismatch_downgrades(self):
        res = compare(make_fp(sfp="aaaa"), make_fp(sfp="bbbb"))
        self.assertLess(res["score"], 0.95)
        self.assertGreaterEqual(res["score"], 0.85)
        self.assertTrue(any("system_fingerprint" in n for n in res["notes"]))

    def test_tokenizer_mismatch_is_low(self):
        other = {"a": 0, "hello": 1, "Hello, world!": 7, "中文分词测试": 9}
        res = compare(make_fp(), make_fp(vector=other))
        self.assertLess(res["score"], 0.95)
        self.assertLess(res["groups"]["tokenizer"]["score"], 0.7)

    def test_overhead_mismatch_is_flagged(self):
        res = compare(make_fp(overhead=29.0), make_fp(overhead=82.0))
        self.assertLess(res["groups"]["identity"]["score"], 0.95)
        details = {d["label"]: d for d in res["groups"]["identity"]["details"]}
        overhead = [d for lab, d in details.items() if "隐藏提示词开销" in lab]
        self.assertTrue(overhead)
        self.assertFalse(overhead[0]["match"])
        self.assertTrue(any("隐藏提示词开销不同" in n for n in res["notes"]))

    def test_missing_groups_renormalize(self):
        a, b = make_fp(), make_fp()
        for fp in (a, b):
            del fp["groups"]["tokenizer"]
            del fp["groups"]["first_step"]
        res = compare(a, b)
        self.assertAlmostEqual(res["score"], 1.0)
        self.assertFalse(res["groups"]["tokenizer"]["comparable"])
        self.assertFalse(res["groups"]["first_step"]["comparable"])

    def test_logprobs_missing_on_one_side(self):
        res = compare(make_fp(), make_fp(logprobs=False))
        self.assertFalse(res["groups"]["first_step"]["comparable"])
        self.assertEqual(res["score"], 1.0)
        self.assertTrue(any("logprobs" in n for n in res["notes"]))

    def test_low_confidence_blocks_same_band(self):
        a, b = make_fp(), make_fp()
        for fp in (a, b):
            fp["groups"] = {"metadata.models": fp["groups"]["metadata.models"]}
        res = compare(a, b)
        self.assertEqual(res["confidence"], "low")
        self.assertNotEqual(res["verdict"]["band"], "same")


class MatrixTest(unittest.TestCase):
    def test_duplicate_labels_include_model(self):
        fps = [make_fp(label="official", model="deepseek-flash"),
               make_fp(label="official", model="deepseek-v4-pro")]
        mat = matrix(fps)
        self.assertEqual(len(set(mat["labels"])), 2)
        self.assertTrue(all("deepseek" in lab for lab in mat["labels"]))

    def test_diagonal_is_one(self):
        mat = matrix([make_fp(label="a"), make_fp(label="b")])
        self.assertEqual(mat["cells"][0][0], 1.0)
        self.assertEqual(mat["cells"][0][1], mat["cells"][1][0])


class FingerprintTest(unittest.TestCase):
    def test_hash_ignores_capture_time_and_cost(self):
        a = make_fp()
        b = make_fp()
        a["hash"] = b["hash"] = fingerprint.stable_hash(
            fingerprint._hashable(a))
        b["captured_at"] = "2000-01-01T00:00:00"
        b["cost"] = {"requests": 999}
        self.assertEqual(fingerprint.stable_hash(fingerprint._hashable(a)),
                         fingerprint.stable_hash(fingerprint._hashable(b)))

    def test_hash_depends_on_content(self):
        a = make_fp()
        b = make_fp(sfp="other")
        self.assertNotEqual(fingerprint.stable_hash(fingerprint._hashable(a)),
                            fingerprint.stable_hash(fingerprint._hashable(b)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
