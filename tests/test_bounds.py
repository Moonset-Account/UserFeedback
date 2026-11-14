"""边界测试：循环引用与超深错误链的有界截断。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from errmap import MAX_DEPTH, sanitize, collect_secret_paths  # noqa: E402


def _markers(node):
    found = []
    if isinstance(node, dict):
        if "__truncated__" in node:
            found.append(node["__truncated__"])
        for value in node.values():
            found.extend(_markers(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_markers(value))
    return found


class TestCircularReference(unittest.TestCase):
    def test_self_reference_truncated(self):
        err = {"code": "X", "message": "self"}
        err["cause"] = err
        out = sanitize(err)
        self.assertEqual(out["cause"]["__truncated__"], "circular-reference")
        json.dumps(out)  # 必须可序列化

    def test_indirect_cycle_truncated(self):
        a = {"code": "A"}
        b = {"code": "B", "cause": a}
        a["cause"] = b
        out = sanitize(a)
        self.assertIn("circular-reference", _markers(out))
        json.dumps(out)

    def test_shared_subtree_without_cycle_not_truncated(self):
        leaf = {"code": "LEAF"}
        err = {"code": "X", "first": leaf, "second": leaf}
        out = sanitize(err)
        self.assertEqual(_markers(out), [])
        self.assertEqual(out["first"]["code"], "LEAF")
        self.assertEqual(out["second"]["code"], "LEAF")

    def test_collect_secret_paths_safe_on_cycle(self):
        err = {"code": "X", "secret_fields": ["env.DB_PASSWORD"]}
        err["cause"] = err
        keys, _ = collect_secret_paths(err)  # 不应死循环
        self.assertIn("DB_PASSWORD", keys)


class TestDeepChain(unittest.TestCase):
    def test_deep_chain_truncated_at_max_depth(self):
        err = {"code": "LEAF"}
        for _ in range(64):
            err = {"code": "WRAP", "cause": err}
        out = sanitize(err)
        self.assertIn("max-depth-exceeded", _markers(out))
        json.dumps(out)

    def test_chain_within_depth_not_truncated(self):
        err = {"code": "LEAF"}
        for _ in range(MAX_DEPTH - 3):
            err = {"code": "WRAP", "cause": err}
        out = sanitize(err)
        self.assertEqual(_markers(out), [])


if __name__ == "__main__":
    unittest.main()
