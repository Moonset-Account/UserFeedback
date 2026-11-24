"""影响归属模块的单元测试。

全部使用本地合成数据，覆盖：
* 正常场景：整批通过、部分成员失败；
* 边界：消息无批次、步骤无报告、报告未覆盖某成员、空/非法输入校验；
* 交错：同批次多成员步骤集不同、部分失败与拆批重试交错、批内多次尝试、
  多级拆批、失败后又重试成功/再失败/重试无下文。
"""

import unittest

from impact_attribution import (
    AttributionEngine,
    Outcome,
    Status,
    StepOutcome,
)


def outcomes(path):
    """把路径压缩成 (batch_id, step, attempt, outcome) 元组列表便于断言。"""
    return [(e.batch_id, e.step, e.attempt, e.outcome) for e in path]


class TestNormalScenarios(unittest.TestCase):
    def test_all_members_succeed_single_batch(self):
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["parse", "persist"])
        eng.register_message("m2", "req-2", ["parse", "persist"])
        eng.register_batch("B1", ["m1", "m2"])
        eng.record_result("B1", "parse", 1)
        eng.record_result("B1", "persist", 1)

        result = eng.attribute_all()
        self.assertEqual(set(result), {"req-1", "req-2"})
        for attr in result.values():
            self.assertEqual(attr.status, Status.SUCCESS)
            self.assertIsNone(attr.failure)
            self.assertEqual(
                outcomes(attr.path),
                [
                    ("B1", "parse", 1, Outcome.PASSED),
                    ("B1", "persist", 1, Outcome.PASSED),
                ],
            )

    def test_partial_failure_only_marks_failed_members(self):
        # 一个步骤只报告部分消息失败，其他成员依自己的结果判定。
        eng = AttributionEngine()
        for i in (1, 2, 3):
            eng.register_message(f"m{i}", f"req-{i}", ["parse", "persist"])
        eng.register_batch("B1", ["m1", "m2", "m3"])
        eng.record_result("B1", "parse", 1, failed_message_ids=["m2"])
        eng.record_result("B1", "persist", 1)

        result = eng.attribute_all()
        self.assertEqual(result["req-1"].status, Status.SUCCESS)
        self.assertEqual(result["req-3"].status, Status.SUCCESS)
        failed = result["req-2"]
        self.assertEqual(failed.status, Status.FAILED)
        self.assertEqual(failed.failure.batch_id, "B1")
        self.assertEqual(failed.failure.step, "parse")
        self.assertEqual(failed.failure.attempt, 1)
        # 失败发生在第一个必需步骤，后续步骤不进入路径。
        self.assertEqual(
            outcomes(failed.path), [("B1", "parse", 1, Outcome.FAILED)]
        )
        # 通过成员的路径不受失败成员影响。
        self.assertEqual(
            outcomes(result["req-1"].path),
            [("B1", "parse", 1, Outcome.PASSED), ("B1", "persist", 1, Outcome.PASSED)],
        )


class TestRetryAttribution(unittest.TestCase):
    def test_within_batch_retry_success_attributes_to_later_attempt(self):
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["parse", "persist"])
        eng.register_batch("B1", ["m1"])
        eng.record_result("B1", "parse", 1, failed_message_ids=["m1"])
        eng.record_result("B1", "parse", 2)  # 同批次更高尝试通过
        eng.record_result("B1", "persist", 1)

        attr = eng.query_request("req-1")
        self.assertEqual(attr.status, Status.SUCCESS)
        self.assertIsNone(attr.failure)
        # 失败与成功分别归到明确的尝试号，路径保留全部尝试。
        self.assertEqual(
            outcomes(attr.path),
            [
                ("B1", "parse", 1, Outcome.FAILED),
                ("B1", "parse", 2, Outcome.PASSED),
                ("B1", "persist", 1, Outcome.PASSED),
            ],
        )

    def test_split_batch_retry_success_path_spans_batches(self):
        eng = AttributionEngine()
        steps = ["parse", "enrich", "persist"]
        eng.register_message("m1", "req-1", steps)
        eng.register_message("m2", "req-2", steps)
        eng.register_batch("B1", ["m1", "m2"])
        eng.record_result("B1", "parse", 1, failed_message_ids=["m2"])
        eng.record_result("B1", "enrich", 1)
        eng.record_result("B1", "persist", 1)
        # 拆批重试：失败成员 m2 进入子批次 B2。
        eng.register_batch("B2", ["m2"], parent_batch_id="B1")
        eng.record_result("B2", "parse", 1)
        eng.record_result("B2", "enrich", 1)
        eng.record_result("B2", "persist", 1)

        result = eng.attribute_all()
        self.assertEqual(result["req-1"].status, Status.SUCCESS)
        self.assertEqual(
            [e.batch_id for e in result["req-1"].path], ["B1", "B1", "B1"]
        )
        retried = result["req-2"]
        self.assertEqual(retried.status, Status.SUCCESS)
        self.assertIsNone(retried.failure)
        self.assertEqual(
            outcomes(retried.path),
            [
                ("B1", "parse", 1, Outcome.FAILED),
                ("B2", "parse", 1, Outcome.PASSED),
                ("B2", "enrich", 1, Outcome.PASSED),
                ("B2", "persist", 1, Outcome.PASSED),
            ],
        )

    def test_split_batch_retry_failure_attributed_to_child_attempt(self):
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["parse"])
        eng.register_batch("B1", ["m1"])
        eng.record_result("B1", "parse", 1, failed_message_ids=["m1"])
        eng.register_batch("B2", ["m1"], parent_batch_id="B1")
        eng.record_result("B2", "parse", 1, failed_message_ids=["m1"])
        eng.record_result("B2", "parse", 2, failed_message_ids=["m1"])

        attr = eng.query_request("req-1")
        self.assertEqual(attr.status, Status.FAILED)
        # 最终失败归到子批次的最高（权威）尝试。
        self.assertEqual(
            (attr.failure.batch_id, attr.failure.step, attr.failure.attempt),
            ("B2", "parse", 2),
        )
        self.assertEqual(
            outcomes(attr.path),
            [
                ("B1", "parse", 1, Outcome.FAILED),
                ("B2", "parse", 1, Outcome.FAILED),
                ("B2", "parse", 2, Outcome.FAILED),
            ],
        )

    def test_multi_level_split_chain(self):
        # 两级拆批：B1 -> B2 -> B3，最终在 B3 通过。
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["s1", "s2"])
        eng.register_batch("B1", ["m1"])
        eng.record_result("B1", "s1", 1, failed_message_ids=["m1"])
        eng.register_batch("B2", ["m1"], parent_batch_id="B1")
        eng.record_result("B2", "s1", 1)
        eng.record_result("B2", "s2", 1, failed_message_ids=["m1"])
        eng.register_batch("B3", ["m1"], parent_batch_id="B2")
        eng.record_result("B3", "s2", 1)

        attr = eng.query_request("req-1")
        self.assertEqual(attr.status, Status.SUCCESS)
        self.assertEqual(
            outcomes(attr.path),
            [
                ("B1", "s1", 1, Outcome.FAILED),
                ("B2", "s1", 1, Outcome.PASSED),
                ("B2", "s2", 1, Outcome.FAILED),
                ("B3", "s2", 1, Outcome.PASSED),
            ],
        )


class TestPendingCases(unittest.TestCase):
    def test_missing_final_step_report_marks_pending(self):
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["parse", "persist"])
        eng.register_batch("B1", ["m1"])
        eng.record_result("B1", "parse", 1)
        # persist 步骤没有任何报告：缺少最终确认。

        attr = eng.query_request("req-1")
        self.assertEqual(attr.status, Status.PENDING)
        self.assertIsNone(attr.failure)
        self.assertEqual(
            outcomes(attr.path), [("B1", "parse", 1, Outcome.PASSED)]
        )

    def test_partial_confirmation_marks_uncovered_member_pending(self):
        # 最终步骤只确认了部分成员，未确认成员标为待查。
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["parse", "persist"])
        eng.register_message("m2", "req-2", ["parse", "persist"])
        eng.register_batch("B1", ["m1", "m2"])
        eng.record_result("B1", "parse", 1)
        eng.record_result("B1", "persist", 1, confirmed_message_ids=["m1"])

        result = eng.attribute_all()
        self.assertEqual(result["req-1"].status, Status.SUCCESS)
        pending = result["req-2"]
        self.assertEqual(pending.status, Status.PENDING)
        self.assertEqual(
            outcomes(pending.path),
            [
                ("B1", "parse", 1, Outcome.PASSED),
                ("B1", "persist", 1, Outcome.UNCONFIRMED),
            ],
        )

    def test_unconfirmed_first_step_marks_pending(self):
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["parse"])
        eng.register_message("m2", "req-2", ["parse"])
        eng.register_batch("B1", ["m1", "m2"])
        eng.record_result("B1", "parse", 1, confirmed_message_ids=["m1"])

        result = eng.attribute_all()
        self.assertEqual(result["req-1"].status, Status.SUCCESS)
        self.assertEqual(result["req-2"].status, Status.PENDING)
        self.assertEqual(
            outcomes(result["req-2"].path),
            [("B1", "parse", 1, Outcome.UNCONFIRMED)],
        )

    def test_failure_followed_by_silent_child_batch_is_pending(self):
        # 拆批重试已发生但子批次尚无结果：最终确认缺失，待查而非失败。
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["parse"])
        eng.register_batch("B1", ["m1"])
        eng.record_result("B1", "parse", 1, failed_message_ids=["m1"])
        eng.register_batch("B2", ["m1"], parent_batch_id="B1")

        attr = eng.query_request("req-1")
        self.assertEqual(attr.status, Status.PENDING)
        self.assertIsNone(attr.failure)
        self.assertEqual(
            outcomes(attr.path), [("B1", "parse", 1, Outcome.FAILED)]
        )

    def test_message_without_any_batch_is_pending(self):
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["parse"])
        attr = eng.query_request("req-1")
        self.assertEqual(attr.status, Status.PENDING)
        self.assertEqual(attr.path, ())


class TestInterleavedScenario(unittest.TestCase):
    def test_interleaved_members_steps_and_splits(self):
        """交错场景：同批成员步骤集不同、部分失败、多级拆批重试交错。"""
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["s1", "s2"])
        eng.register_message("m2", "req-2", ["s1", "s2", "s3"])
        eng.register_message("m3", "req-3", ["s1"])
        eng.register_batch("B1", ["m1", "m2", "m3"])
        # s1 只失败 m2；s2 只确认 m1（m2 在 s1 已失败，m3 不需要 s2）。
        eng.record_result("B1", "s1", 1, failed_message_ids=["m2"])
        eng.record_result("B1", "s2", 1, confirmed_message_ids=["m1"])
        # m2 拆批到 B2：s1 重试通过，s2 又失败。
        eng.register_batch("B2", ["m2"], parent_batch_id="B1")
        eng.record_result("B2", "s1", 1)
        eng.record_result("B2", "s2", 1, failed_message_ids=["m2"])
        # m2 再拆批到 B3：s2、s3 依次通过。
        eng.register_batch("B3", ["m2"], parent_batch_id="B2")
        eng.record_result("B3", "s2", 1)
        eng.record_result("B3", "s3", 1)

        result = eng.attribute_all()

        self.assertEqual(result["req-1"].status, Status.SUCCESS)
        self.assertEqual(
            outcomes(result["req-1"].path),
            [("B1", "s1", 1, Outcome.PASSED), ("B1", "s2", 1, Outcome.PASSED)],
        )

        self.assertEqual(result["req-3"].status, Status.SUCCESS)
        self.assertEqual(
            outcomes(result["req-3"].path), [("B1", "s1", 1, Outcome.PASSED)]
        )

        retried = result["req-2"]
        self.assertEqual(retried.status, Status.SUCCESS)
        self.assertIsNone(retried.failure)
        self.assertEqual(
            outcomes(retried.path),
            [
                ("B1", "s1", 1, Outcome.FAILED),
                ("B2", "s1", 1, Outcome.PASSED),
                ("B2", "s2", 1, Outcome.FAILED),
                ("B3", "s2", 1, Outcome.PASSED),
                ("B3", "s3", 1, Outcome.PASSED),
            ],
        )

    def test_failure_mid_chain_ignores_later_step_reports(self):
        # 中间步骤失败后，更靠后步骤的报告不进入该消息的路径。
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["a", "b", "c"])
        eng.register_batch("B1", ["m1"])
        eng.record_result("B1", "a", 1)
        eng.record_result("B1", "b", 1, failed_message_ids=["m1"])
        eng.record_result("B1", "c", 1)

        attr = eng.query_request("req-1")
        self.assertEqual(attr.status, Status.FAILED)
        self.assertEqual(
            (attr.failure.batch_id, attr.failure.step, attr.failure.attempt),
            ("B1", "b", 1),
        )
        self.assertEqual(
            outcomes(attr.path),
            [("B1", "a", 1, Outcome.PASSED), ("B1", "b", 1, Outcome.FAILED)],
        )


class TestQueryInterface(unittest.TestCase):
    def test_query_request_returns_original_request_view(self):
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["parse"])
        eng.register_batch("B1", ["m1"])
        eng.record_result("B1", "parse", 1)

        attr = eng.query_request("req-1")
        self.assertEqual(attr.request_id, "req-1")
        self.assertEqual(attr.message_id, "m1")
        self.assertEqual(attr.status, Status.SUCCESS)
        self.assertIsInstance(attr.path, tuple)
        self.assertTrue(all(isinstance(e, StepOutcome) for e in attr.path))
        # 与 attribute_all 的结果一致。
        self.assertEqual(attr, eng.attribute_all()["req-1"])

    def test_query_unknown_request_raises_key_error(self):
        eng = AttributionEngine()
        with self.assertRaises(KeyError):
            eng.query_request("no-such-request")

    def test_attribute_all_covers_every_registered_request(self):
        eng = AttributionEngine()
        eng.register_message("m1", "req-1", ["s"])
        eng.register_message("m2", "req-2", ["s"])
        eng.register_batch("B1", ["m1"])
        eng.record_result("B1", "s", 1)
        result = eng.attribute_all()
        self.assertEqual(set(result), {"req-1", "req-2"})
        self.assertEqual(result["req-1"].status, Status.SUCCESS)
        self.assertEqual(result["req-2"].status, Status.PENDING)  # 未入批，待查


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.eng = AttributionEngine()
        self.eng.register_message("m1", "req-1", ["parse", "persist"])
        self.eng.register_message("m2", "req-2", ["parse"])
        self.eng.register_batch("B1", ["m1", "m2"])

    def test_duplicate_message_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.register_message("m1", "req-9", ["parse"])

    def test_duplicate_request_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.register_message("m9", "req-1", ["parse"])

    def test_empty_steps_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.register_message("m9", "req-9", [])

    def test_duplicate_steps_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.register_message("m9", "req-9", ["parse", "parse"])

    def test_batch_with_unknown_message_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.register_batch("B2", ["m1", "ghost"])

    def test_batch_with_unknown_parent_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.register_batch("B2", ["m1"], parent_batch_id="ghost")

    def test_empty_batch_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.register_batch("B2", [])

    def test_result_for_unknown_batch_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.record_result("ghost", "parse", 1)

    def test_failed_ids_outside_batch_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.record_result("B1", "parse", 1, failed_message_ids=["ghost"])

    def test_confirmed_ids_outside_batch_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.record_result("B1", "parse", 1, confirmed_message_ids=["ghost"])

    def test_failed_must_be_subset_of_confirmed(self):
        with self.assertRaises(ValueError):
            self.eng.record_result(
                "B1",
                "parse",
                1,
                failed_message_ids=["m2"],
                confirmed_message_ids=["m1"],
            )

    def test_duplicate_report_rejected(self):
        self.eng.record_result("B1", "parse", 1)
        with self.assertRaises(ValueError):
            self.eng.record_result("B1", "parse", 1)

    def test_invalid_attempt_rejected(self):
        for bad in (0, -1, 1.5, "1"):
            with self.assertRaises(ValueError):
                self.eng.record_result("B1", "parse", bad)

    def test_step_not_required_by_covered_members_rejected(self):
        with self.assertRaises(ValueError):
            self.eng.record_result("B1", "no-such-step", 1)
        with self.assertRaises(ValueError):
            # persist 只是 m1 的步骤，只确认 m2 时不合法。
            self.eng.record_result("B1", "persist", 1, confirmed_message_ids=["m2"])

    def test_ambiguous_split_rejected_at_query(self):
        # 同一消息被拆入两个并列子批次，无法确定唯一重试路径。
        self.eng.register_batch("B2", ["m1"], parent_batch_id="B1")
        self.eng.register_batch("B3", ["m1"], parent_batch_id="B1")
        with self.assertRaises(ValueError):
            self.eng.query_request("req-1")


if __name__ == "__main__":
    unittest.main()
