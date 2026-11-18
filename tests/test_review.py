"""事后复核测试：决定落盘、更正事件后重算并比对历史决定。"""

import unittest
from pathlib import Path

from release_gate import DecisionRepository, evaluate_bundle
from release_gate.intervals import parse_dt

from factory import application, event, exemption, iv, make_bundle, quota


def build_bundle(close_at=None, as_of="2026-11-05T12:00:00Z"):
    """豁免 11-05 08:00 OPEN；close_at 为 None 表示尚未收回。"""
    ex = exemption(
        "EX-1",
        start="2026-11-05T00:00:00Z",
        end="2026-11-09T00:00:00Z",
    )
    events = [event("E1", "EX-1", "OPEN", "2026-11-05T08:00:00Z")]
    if close_at is not None:
        events.append(event("E2", "EX-1", "CLOSE", close_at))
    return make_bundle(
        quotas=[quota(duration="PT5H")],
        exemptions=[ex],
        events=events,
        applications=[
            application(
                "APP-1",
                intervals=[iv("2026-11-06T00:00:00Z", "2026-11-06T02:00:00Z")],
            )
        ],
        as_of=as_of,
    )


class TestReview(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(__file__).resolve().parent / "_tmp_review"
        self.tmp_dir.mkdir(exist_ok=True)
        self.store_path = self.tmp_dir / "decisions.json"

    def tearDown(self):
        for child in self.tmp_dir.glob("*"):
            child.unlink()
        self.tmp_dir.rmdir()

    def test_save_and_reload_roundtrip(self):
        bundle = build_bundle()
        evaluation = evaluate_bundle(bundle)
        repo = DecisionRepository(self.store_path)
        stored = repo.save(evaluation, {"window": {"start": "x", "end": "y"}})
        self.assertEqual(len(stored), 1)
        reloaded = repo.load_all()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].decision.application_id, "APP-1")
        self.assertEqual(
            reloaded[0].decision.used_intervals,
            evaluation.decisions[0].used_intervals,
        )

    def test_correction_adds_missing_close_and_flips_decision(self):
        # 原始输入缺少 CLOSE：截至 as_of(12:00) 已开放 4h，再开 2h 共 6h > 5h，
        # 历史决定为阻止。
        original = build_bundle(close_at=None)
        original_eval = evaluate_bundle(original)
        original_decision = original_eval.decisions[0]
        self.assertFalse(original_decision.allowed)

        repo = DecisionRepository(self.store_path)
        repo.save(original_eval, {"as_of": "2026-11-05T12:00:00Z"})

        # 事后更正：补录 10:00 的实际 CLOSE（未到期已收回，真实开放仅 2h），
        # 沿用原 as_of 重算，2h + 2h = 4h <= 5h，决定应翻转为放行。
        corrected = build_bundle(close_at="2026-11-05T10:00:00Z")
        report = repo.review(corrected)
        self.assertTrue(report.has_changes)
        item = report.items[0]
        self.assertFalse(item.original_allowed)
        self.assertTrue(item.recomputed_allowed)
        self.assertTrue(item.changed)
        self.assertEqual(
            item.recomputed.used_intervals,
            [iv("2026-11-05T08:00:00Z", "2026-11-05T10:00:00Z")],
        )

    def test_correction_without_impact_keeps_decision(self):
        original = build_bundle(close_at="2026-11-05T10:00:00Z")
        repo = DecisionRepository(self.store_path)
        repo.save(evaluate_bundle(original), {"as_of": "2026-11-05T12:00:00Z"})
        # 更正内容与原输入等价（不改变任何区间），复核结论不变。
        report = repo.review(build_bundle(close_at="2026-11-05T10:00:00Z"))
        self.assertFalse(report.has_changes)
        self.assertTrue(report.items[0].original_allowed)
        self.assertTrue(report.items[0].recomputed_allowed)

    def test_explicit_review_as_of_overrides_snapshot(self):
        original = build_bundle(close_at=None, as_of="2026-11-05T12:00:00Z")
        repo = DecisionRepository(self.store_path)
        repo.save(evaluate_bundle(original), {"as_of": "2026-11-05T12:00:00Z"})
        # 指定更早的复核时点：截至 09:00 只开放 1h，1h + 2h = 3h <= 5h 放行。
        report = repo.review(
            build_bundle(close_at=None),
            as_of=parse_dt("2026-11-05T09:00:00Z"),
        )
        self.assertTrue(report.has_changes)
        self.assertTrue(report.items[0].recomputed_allowed)

    def test_review_missing_application_raises(self):
        original = build_bundle()
        repo = DecisionRepository(self.store_path)
        repo.save(evaluate_bundle(original), {"as_of": "2026-11-05T12:00:00Z"})
        corrected = build_bundle()
        corrected.applications = []
        with self.assertRaises(ValueError):
            repo.review(corrected)


if __name__ == "__main__":
    unittest.main()
