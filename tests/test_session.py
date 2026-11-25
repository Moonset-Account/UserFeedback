"""会话：未播完不计入、提交重算区分性、复诵唯一性。"""

import unittest

from confirm import (
    ConfirmationSession,
    PlaybackEvent,
    RepetitionEvent,
    SubmitStatus,
    plan_confirmation,
)
from tests.helpers import amount_tail_book, book, cand


def _seg(i, fields, dur):
    from confirm import Segment

    return Segment(i, fields, dur)


def session_with_plan(book_=None, target="A"):
    book_ = book_ or amount_tail_book()
    segments = (_seg(0, (1,), 300), _seg(1, (2,), 400))
    plan = plan_confirmation(book_, target, (1, 2), segments, 2000)
    return ConfirmationSession(plan=plan, book=book_)


def complete_all(session, target="A", alias_by_field=None):
    alias_by_field = alias_by_field or {1: "一百元", 2: "一二三四"}
    for idx in session.plan.segment_indices:
        session.on_playback(PlaybackEvent(idx, target, True))
    for fid, alias in alias_by_field.items():
        session.on_repetition(RepetitionEvent(fid, alias, True))


class IncompletePlaybackTests(unittest.TestCase):
    def test_unfinished_segment_does_not_count(self):
        s = session_with_plan()
        s.on_playback(PlaybackEvent(0, "A", True))  # 只播完字段 1
        s.on_repetition(RepetitionEvent(1, "一百元", True))
        s.on_repetition(RepetitionEvent(2, "一二三四", True))
        result = s.submit((1, 2))
        self.assertEqual(result.status, SubmitStatus.SUPPLEMENTAL_REQUIRED)
        self.assertEqual(result.reason, "unfinished_segments")
        self.assertIn("2", result.detail)

    def test_playback_completed_false_does_not_count(self):
        s = session_with_plan()
        s.on_playback(PlaybackEvent(0, "A", True))
        s.on_playback(PlaybackEvent(1, "A", False))  # 未播完
        for fid, alias in {1: "一百元", 2: "一二三四"}.items():
            s.on_repetition(RepetitionEvent(fid, alias, True))
        result = s.submit((1, 2))
        self.assertEqual(result.status, SubmitStatus.SUPPLEMENTAL_REQUIRED)
        self.assertEqual(result.reason, "unfinished_segments")

    def test_no_segments_completed(self):
        s = session_with_plan()
        result = s.submit((1, 2))
        self.assertEqual(result.reason, "no_completed_segments")

    def test_completion_cannot_regress(self):
        s = session_with_plan()
        s.on_playback(PlaybackEvent(0, "A", True))
        s.on_playback(PlaybackEvent(0, "A", False))  # 迟到的未完成事件不应撤销
        self.assertEqual(s.completed_segment_indices(), (0,))


class HappyPathTests(unittest.TestCase):
    def test_full_playback_and_repetition_confirms(self):
        s = session_with_plan()
        complete_all(s)
        result = s.submit((1, 2))
        self.assertEqual(result.status, SubmitStatus.CONFIRMED)
        self.assertTrue(result.confirmed)
        self.assertEqual(result.confirmed_fields, (1, 2))

    def test_repetition_mismatch_rejected(self):
        s = session_with_plan()
        for idx in s.plan.segment_indices:
            s.on_playback(PlaybackEvent(idx, "A", True))
        s.on_repetition(RepetitionEvent(1, "一百元", True))
        s.on_repetition(RepetitionEvent(2, "五六七八", False))  # 复诵错误
        result = s.submit((1, 2))
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertEqual(result.reason, "repetition_mismatch")

    def test_missing_repetition_needs_supplement(self):
        s = session_with_plan()
        for idx in s.plan.segment_indices:
            s.on_playback(PlaybackEvent(idx, "A", True))
        s.on_repetition(RepetitionEvent(1, "一百元", True))
        result = s.submit((1, 2))
        self.assertEqual(result.reason, "missing_repetition")


class NewCandidateRaceTests(unittest.TestCase):
    def test_new_identical_candidate_forces_supplement(self):
        s = session_with_plan()
        complete_all(s)
        # 播报中新增一笔与 A 完全同音的候选 C
        grown = book(
            cand("A", f1=("一百元",), f2=("1234", "一二三四")),
            cand("B", f1=("一百元",), f2=("5678", "五六七八")),
            cand("C", f1=("一百元",), f2=("1234", "一二三四")),
        )
        s.update_candidates(grown)
        result = s.submit((1, 2))
        self.assertEqual(result.status, SubmitStatus.SUPPLEMENTAL_REQUIRED)
        self.assertEqual(result.reason, "new_indistinguishable_candidate")
        self.assertIn("C", result.ambiguous_candidate_ids)

    def test_new_candidate_at_submit_time_argument(self):
        s = session_with_plan()
        complete_all(s)
        grown = book(
            cand("A", f1=("一百元",), f2=("1234", "一二三四")),
            cand("B", f1=("一百元",), f2=("5678", "五六七八")),
            cand("C", f1=("一百元",), f2=("1234", "一二三四")),
        )
        result = s.submit((1, 2), book=grown)
        self.assertEqual(result.reason, "new_indistinguishable_candidate")

    def test_new_distinct_candidate_does_not_block(self):
        s = session_with_plan()
        complete_all(s)
        grown = book(
            cand("A", f1=("一百元",), f2=("1234", "一二三四")),
            cand("B", f1=("一百元",), f2=("5678", "五六七八")),
            cand("D", f1=("一百元",), f2=("9999", "九九九九")),
        )
        s.update_candidates(grown)
        result = s.submit((1, 2))
        self.assertEqual(result.status, SubmitStatus.CONFIRMED)


class RepetitionUniquenessTests(unittest.TestCase):
    def test_repetition_matching_two_candidates_not_unique(self):
        # A 与 C 尾号同音：复诵尾号无法唯一指向
        book_ = book(
            cand("A", f1=("一百元",), f2=("1234",)),
            cand("C", f1=("两百元",), f2=("1234",)),
        )
        # 只读金额即可区分（A/C 金额不同）
        segments = (_seg(0, (1,), 300), _seg(1, (2,), 300))
        plan = plan_confirmation(book_, "A", (1,), segments, 1000)
        s = ConfirmationSession(plan=plan, book=book_)
        s.on_playback(PlaybackEvent(plan.segment_indices[0], "A", True))
        # 复诵了一个会被两笔都解释的尾号，且该尾号未在播报中
        s.on_repetition(RepetitionEvent(1, "一百元", True))
        s.on_repetition(RepetitionEvent(2, "一二三四", True))
        result = s.submit((1,))
        # 字段 2 未播 -> 补充确认
        self.assertEqual(result.reason, "repetition_for_unannounced_field")


if __name__ == "__main__":
    unittest.main()
