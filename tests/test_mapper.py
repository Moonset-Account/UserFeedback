"""映射层行为测试：登记错误固定文案、未知错误通用文案、链路编号一致。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from errmap import ErrorRegistry, map_error  # noqa: E402

REGISTRY = ErrorRegistry.from_dict({
    "generic_message": "系统繁忙，请稍后重试",
    "unknown_code": "INTERNAL_ERROR",
    "mappings": {
        "PAYMENT_DECLINED": {
            "client_code": "PAYMENT_DECLINED",
            "client_message": "支付未成功，请更换支付方式后重试",
        },
    },
})


class TestRegisteredMapping(unittest.TestCase):
    def test_registered_code_maps_to_fixed_message(self):
        err = {"code": "PAYMENT_DECLINED", "message": "gateway said no"}
        out = map_error(err, "payments.charge", "req-1", REGISTRY)
        self.assertEqual(out["client"]["error"]["message"],
                         "支付未成功，请更换支付方式后重试")
        self.assertEqual(out["client"]["error"]["code"], "PAYMENT_DECLINED")

    def test_registered_client_view_hides_internal_message(self):
        err = {"code": "PAYMENT_DECLINED", "message": "gateway said no"}
        out = map_error(err, "payments.charge", "req-1", REGISTRY)
        self.assertNotIn("gateway said no",
                         json.dumps(out["client"], ensure_ascii=False))


class TestUnknownMapping(unittest.TestCase):
    def test_unknown_code_uses_generic_message(self):
        err = {"code": "DB_CONN_TIMEOUT", "message": "socket timeout"}
        out = map_error(err, "payments.charge", "req-2", REGISTRY)
        self.assertEqual(out["client"]["error"]["message"], "系统繁忙，请稍后重试")
        self.assertEqual(out["client"]["error"]["code"], "INTERNAL_ERROR")

    def test_missing_code_uses_generic_message(self):
        out = map_error({"message": "boom"}, "payments.charge", "req-3", REGISTRY)
        self.assertEqual(out["client"]["error"]["code"], "INTERNAL_ERROR")

    def test_unknown_client_view_has_no_internal_fields(self):
        err = {"code": "DB_CONN_TIMEOUT", "message": "socket timeout",
               "env": {"DB_HOST": "10.0.0.8"}, "stack": ["f:1"]}
        out = map_error(err, "payments.charge", "req-4", REGISTRY)
        self.assertEqual(sorted(out["client"]["error"].keys()),
                         ["code", "message", "trace_id"])


class TestTraceIdConsistency(unittest.TestCase):
    def test_trace_id_identical_in_both_views(self):
        err = {"code": "PAYMENT_DECLINED", "cause": {"code": "X"}}
        out = map_error(err, "payments.charge", "req-trace-9", REGISTRY)
        self.assertEqual(out["client"]["error"]["trace_id"], "req-trace-9")
        self.assertEqual(out["internal"]["trace_id"], "req-trace-9")

    def test_interface_recorded_in_internal_view(self):
        out = map_error({"code": "X"}, "orders.submit", "req-5", REGISTRY)
        self.assertEqual(out["internal"]["interface"], "orders.submit")


class TestInternalViewContent(unittest.TestCase):
    def test_internal_view_keeps_nested_cause_and_stack(self):
        err = {
            "code": "PAYMENT_DECLINED",
            "cause": {"code": "DB_CONN_TIMEOUT",
                      "stack": ["order_store.connect:81"]},
        }
        out = map_error(err, "payments.charge", "req-6", REGISTRY)
        internal = out["internal"]["error"]
        self.assertEqual(internal["cause"]["code"], "DB_CONN_TIMEOUT")
        self.assertEqual(internal["cause"]["stack"],
                         ["order_store.connect:81"])


if __name__ == "__main__":
    unittest.main()
