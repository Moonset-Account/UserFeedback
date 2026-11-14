"""隔离状态模块单元测试。

case_id 约定：REQ-xx 对应 README 中的需求映射表。
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from isolation_module import (  # noqa: E402
    ACTION_ISOLATE,
    ACTION_RELEASE,
    DuplicateReceiptError,
    IntentAlreadyConfirmedError,
    IsolationConsole,
    NoIntentError,
    STATE_ISOLATED,
    STATE_RELEASED,
    STATE_UNKNOWN,
    STATUS_ACK_FAILED,
    STATUS_ACK_SUCCESS,
    STATUS_PENDING,
    UnknownCommandError,
    UnknownDeviceError,
)


class RequestNumberingTest(unittest.TestCase):
    """REQ-01: 模块记录每次请求的命令编号和操作序号。"""

    def test_req01_cmd_id_and_op_seq_recorded(self):
        c = IsolationConsole()
        r1 = c.request("dev-01", ACTION_ISOLATE)
        r2 = c.request("dev-01", ACTION_RELEASE)
        self.assertEqual((r1.cmd_id, r1.op_seq), (1, 1))
        self.assertEqual((r2.cmd_id, r2.op_seq), (2, 2))
        # 操作序号按设备独立递增
        r3 = c.request("dev-02", ACTION_ISOLATE)
        self.assertEqual((r3.cmd_id, r3.op_seq), (3, 1))
        history = c.send_history("dev-01")
        self.assertEqual([h["cmd_id"] for h in history], [1, 2])
        self.assertEqual([h["op_seq"] for h in history], [1, 2])


class ConsoleViewTest(unittest.TestCase):
    """REQ-02: 控制台分别显示期望状态、最后确认状态及仍待确认的命令。"""

    def test_req02_view_fields(self):
        c = IsolationConsole()
        c.request("dev-01", ACTION_ISOLATE)
        c.request("dev-01", ACTION_RELEASE)
        view = c.console_view("dev-01")
        self.assertEqual(view["expected_state"], STATE_RELEASED)      # 最新意图
        self.assertEqual(view["last_confirmed_state"], STATE_UNKNOWN)  # 尚无回执
        self.assertEqual(
            [(p["cmd_id"], p["op_seq"]) for p in view["pending_commands"]],
            [(1, 1), (2, 2)],
        )


class OutOfOrderReceiptTest(unittest.TestCase):
    """REQ-03/REQ-04: 先隔离再解除、解除回执先到；旧成功回执只补历史不覆盖。"""

    def setUp(self):
        self.c = IsolationConsole()
        self.c.request("dev-01", ACTION_ISOLATE)  # cmd 1, op 1
        self.c.request("dev-01", ACTION_RELEASE)  # cmd 2, op 2

    def test_req03_release_receipt_first_confirms_released(self):
        out = self.c.handle_receipt("dev-01", 2, "success")
        self.assertTrue(out["applied"])
        view = self.c.console_view("dev-01")
        self.assertEqual(view["last_confirmed_state"], STATE_RELEASED)
        self.assertEqual(view["confirmed_op_seq"], 2)
        # 隔离命令仍待确认
        self.assertEqual([p["cmd_id"] for p in view["pending_commands"]], [1])

    def test_req04_stale_isolate_receipt_fills_history_without_override(self):
        self.c.handle_receipt("dev-01", 2, "success")
        out = self.c.handle_receipt("dev-01", 1, "success")
        self.assertFalse(out["applied"])  # 旧操作不推进确认状态
        view = self.c.console_view("dev-01")
        self.assertEqual(view["last_confirmed_state"], STATE_RELEASED)
        self.assertEqual(view["confirmed_op_seq"], 2)
        self.assertEqual(view["pending_commands"], [])
        # 历史被补全：cmd 1 也标记为成功
        history = {h["cmd_id"]: h["status"] for h in self.c.send_history("dev-01")}
        self.assertEqual(history[1], STATUS_ACK_SUCCESS)
        self.assertEqual(history[2], STATUS_ACK_SUCCESS)

    def test_req04b_stale_failure_receipt_does_not_change_confirmed(self):
        self.c.handle_receipt("dev-01", 2, "success")
        out = self.c.handle_receipt("dev-01", 1, "failed")
        self.assertFalse(out["applied"])
        view = self.c.console_view("dev-01")
        self.assertEqual(view["last_confirmed_state"], STATE_RELEASED)
        history = {h["cmd_id"]: h["status"] for h in self.c.send_history("dev-01")}
        self.assertEqual(history[1], STATUS_ACK_FAILED)


class ResendTest(unittest.TestCase):
    """REQ-05: 失败时重发当前意图，沿用原操作序号并保留发送历史。"""

    def test_req05_resend_keeps_op_seq_and_history(self):
        c = IsolationConsole()
        c.request("dev-01", ACTION_ISOLATE)  # cmd 1, op 1
        c.handle_receipt("dev-01", 1, "failed")
        r2 = c.resend("dev-01")
        self.assertEqual(r2.op_seq, 1)       # 沿用原操作序号
        self.assertEqual(r2.cmd_id, 2)       # 新命令编号
        history = c.send_history("dev-01")
        self.assertEqual(len(history), 2)    # 发送历史保留
        self.assertEqual(history[0]["status"], STATUS_ACK_FAILED)
        self.assertEqual(history[1]["status"], STATUS_PENDING)
        # 重发成功后才推进确认状态
        c.handle_receipt("dev-01", 2, "success")
        view = c.console_view("dev-01")
        self.assertEqual(view["last_confirmed_state"], STATE_ISOLATED)
        self.assertEqual(view["pending_commands"], [])

    def test_req05b_failure_receipt_does_not_change_confirmed_state(self):
        c = IsolationConsole()
        c.request("dev-01", ACTION_ISOLATE)
        out = c.handle_receipt("dev-01", 1, "failed")
        self.assertFalse(out["applied"])
        self.assertEqual(c.console_view("dev-01")["last_confirmed_state"], STATE_UNKNOWN)


class BoundaryTest(unittest.TestCase):
    """REQ-06/07/08: 异常与边界。"""

    def test_req06_unknown_cmd_receipt_raises(self):
        c = IsolationConsole()
        c.request("dev-01", ACTION_ISOLATE)
        with self.assertRaises(UnknownCommandError):
            c.handle_receipt("dev-01", 99, "success")

    def test_req06b_receipt_for_unknown_device_raises(self):
        c = IsolationConsole()
        with self.assertRaises(UnknownDeviceError):
            c.handle_receipt("ghost", 1, "success")

    def test_req07_duplicate_receipt_raises(self):
        c = IsolationConsole()
        c.request("dev-01", ACTION_ISOLATE)
        c.handle_receipt("dev-01", 1, "success")
        with self.assertRaises(DuplicateReceiptError):
            c.handle_receipt("dev-01", 1, "success")

    def test_req08_resend_after_intent_confirmed_raises(self):
        c = IsolationConsole()
        c.request("dev-01", ACTION_ISOLATE)
        c.handle_receipt("dev-01", 1, "success")
        with self.assertRaises(IntentAlreadyConfirmedError):
            c.resend("dev-01")

    def test_req08b_resend_without_intent_raises(self):
        c = IsolationConsole()
        c.request("dev-01", ACTION_ISOLATE)
        with self.assertRaises(NoIntentError):
            c.resend("dev-02")


class CliDemoTest(unittest.TestCase):
    """REQ-09: CLI 演示先隔离再解除、解除回执先到，并给出最终设备状态。"""

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"), *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=30,
        )

    def test_req09_demo_final_state_released(self):
        proc = self.run_cli("demo")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "FINAL device=dev-01 expected=released confirmed=released pending=0",
            proc.stdout,
        )

    def test_req09b_sample_file_reproduces_same_final_state(self):
        proc = self.run_cli("run", os.path.join(ROOT, "samples", "out_of_order_events.jsonl"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "FINAL device=dev-01 expected=released confirmed=released pending=0",
            proc.stdout,
        )

    def test_req09c_resend_sample_final_state_isolated(self):
        proc = self.run_cli("run", os.path.join(ROOT, "samples", "resend_after_failure.jsonl"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "FINAL device=dev-02 expected=isolated confirmed=isolated pending=0",
            proc.stdout,
        )


if __name__ == "__main__":
    unittest.main()
