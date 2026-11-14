#!/usr/bin/env python3
"""本地示例：串联两个模块抛出的嵌套错误，核对客户端与内部诊断输出。

运行：
    /usr/local/bin/python3 examples/run_demo.py

打印映射结果 JSON，随后逐条输出样例/边界断言结果行：
    CASE <case_id> PASS|FAIL actual=<实际值> expected=<期望值>
任一断言失败以退出码 1 结束。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from errmap import ErrorRegistry, map_error, MAX_DEPTH  # noqa: E402
from examples.modules import module_a_charge  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TRACE_ID = "req-demo-0001"
INTERFACE = "payments.charge"


def build_demo_result():
    registry = ErrorRegistry.load(os.path.join(HERE, "registry.json"))
    error_obj = module_a_charge()
    return map_error(error_obj, INTERFACE, TRACE_ID, registry)


def build_circular_result():
    registry = ErrorRegistry.load(os.path.join(HERE, "registry.json"))
    err = {"code": "PAYMENT_DECLINED", "message": "self-referencing"}
    err["cause"] = err  # 循环引用
    return map_error(err, INTERFACE, TRACE_ID, registry)


def build_deep_result(depth=64):
    registry = ErrorRegistry.load(os.path.join(HERE, "registry.json"))
    err = {"code": "DB_CONN_TIMEOUT", "message": "leaf"}
    for _ in range(depth):
        err = {"code": "WRAPPED", "message": "wrap", "cause": err}
    return map_error(err, INTERFACE, TRACE_ID, registry)


def _contains_marker(node, marker):
    if isinstance(node, dict):
        if node.get("__truncated__") == marker:
            return True
        return any(_contains_marker(v, marker) for v in node.values())
    if isinstance(node, list):
        return any(_contains_marker(v, marker) for v in node)
    return False


def main():
    results = []

    def check(case_id, actual, expected):
        ok = actual == expected
        results.append(ok)
        print("CASE %s %s actual=%r expected=%r" % (
            case_id, "PASS" if ok else "FAIL", actual, expected))

    # ---- 样例：两模块嵌套错误 ----
    demo = build_demo_result()
    print(json.dumps(demo, ensure_ascii=False, indent=2))

    check("demo.client_fixed_message",
          demo["client"]["error"]["message"], "支付未成功，请更换支付方式后重试")
    check("demo.client_code",
          demo["client"]["error"]["code"], "PAYMENT_DECLINED")
    check("demo.trace_id_consistent",
          (demo["client"]["error"]["trace_id"], demo["internal"]["trace_id"]),
          (TRACE_ID, TRACE_ID))
    check("demo.client_has_no_internals",
          sorted(demo["client"]["error"].keys()),
          ["code", "message", "trace_id"])
    check("demo.internal_interface",
          demo["internal"]["interface"], INTERFACE)

    internal_error = demo["internal"]["error"]
    check("demo.internal_keeps_cause_code",
          internal_error["cause"]["code"], "DB_CONN_TIMEOUT")
    check("demo.internal_redacts_module_a_token",
          internal_error["env"]["GATEWAY_API_TOKEN"], "***REDACTED***")
    check("demo.internal_redacts_module_b_password",
          internal_error["cause"]["env"]["DB_PASSWORD"], "***REDACTED***")
    check("demo.internal_keeps_non_secret_env",
          internal_error["cause"]["env"]["DB_HOST"], "10.0.0.8")
    check("demo.internal_keeps_stack",
          internal_error["cause"]["stack"][0], "order_store.connect:81")

    client_text = json.dumps(demo["client"], ensure_ascii=False)
    internal_text = json.dumps(demo["internal"], ensure_ascii=False)
    check("demo.client_no_secret_leak",
          ("s3cret-db-pass" in client_text) or ("tok-live-9f3c" in client_text),
          False)
    check("demo.internal_no_secret_leak",
          ("s3cret-db-pass" in internal_text)
          or ("tok-live-9f3c" in internal_text),
          False)

    # ---- 边界：循环引用有界截断 ----
    circular = build_circular_result()
    check("boundary.circular_truncated",
          _contains_marker(circular["internal"]["error"], "circular-reference"),
          True)
    check("boundary.circular_json_serializable",
          bool(json.dumps(circular, ensure_ascii=False)), True)

    # ---- 边界：超深错误链有界截断 ----
    deep = build_deep_result()
    check("boundary.deep_chain_truncated",
          _contains_marker(deep["internal"]["error"], "max-depth-exceeded"),
          True)
    check("boundary.max_depth_is_bounded", MAX_DEPTH <= 32, True)

    if all(results):
        print("DEMO RESULT: PASS (%d checks)" % len(results))
        return 0
    print("DEMO RESULT: FAIL (%d/%d checks failed)" % (
        results.count(False), len(results)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
