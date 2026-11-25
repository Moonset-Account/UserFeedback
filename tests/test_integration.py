import unittest

from confirm import (
    ConfirmationSession,
    PlaybackEvent,
    RepetitionEvent,
    Segment,
    SubmitStatus,
    plan_confirmation,
)
from tests.helpers import book, cand


def _seg(i, fields, dur):
    return Segment(i, fields, dur)


def _book():
    return book(
        cand("A", f1=("一百元",), f2=("1234", "一二三四")),
        cand("B", f1=("一百元",), f2=("5678", "五六七八")),
    )


class EndToEndTests(unittest.TestCase):
    def test_plan_then_interleaved_events_then_confirm(self):
        bk = _book()
        segs = (_seg(0, (1,), 300), _seg(1, (2,), 400), _seg(2, (1, 2), 900))
        plan = plan_confirmation(bk, "A", (1, 2), segs, 1000)
        self.assertEqual(plan.field_ids, (1, 2))
        self.assertEqual(plan.total_duration_ms, 700)

        s = ConfirmationSession(plan=plan, book=bk)
        # 事件交错到达：先复诵段 0 的字段，再播完段 0，再播完段 1，再复诵段 1
        s.on_repetition(RepetitionEvent(1, "一百元", True))
        s.on_playback(PlaybackEvent(0, "A", True))
        s.on_playback(PlaybackEvent(1, "A", True))
        s.on_repetition(RepetitionEvent(2, "一二三四", True))
        result = s.submit((1, 2))
        self.assertEqual(result.status, SubmitStatus.CONFIRMED)

    def test_supplement_then_retry_after_unfinish(self):
        bk = _book()
        plan = plan_confirmation(bk, "A", (1, 2), (_seg(0, (1,), 300), _seg(1, (2,), 400)), 2000)
        s = ConfirmationSession(plan=plan, book=bk)
        s.on_playback(PlaybackEvent(0, "A", True))
        s.on_repetition(RepetitionEvent(1, "一百元", True))
        s.on_repetition(RepetitionEvent(2, "一二三四", True))
        early = s.submit((1, 2))
        self.assertEqual(early.status, SubmitStatus.SUPPLEMENTAL_REQUIRED)
        # 补充播放第二段后重新提交，可继续
        s.on_playback(PlaybackEvent(1, "A", True))
        final = s.submit((1, 2))
        self.assertEqual(final.status, SubmitStatus.CONFIRMED)

    def test_new_identical_candidate_then_supplement_resolves(self):
        bk = _book()
        plan = plan_confirmation(bk, "A", (1, 2), (_seg(0, (1,), 300), _seg(1, (2,), 400)), 2000)
        s = ConfirmationSession(plan=plan, book=bk)
        s.on_playback(PlaybackEvent(0, "A", True))
        s.on_playback(PlaybackEvent(1, "A", True))
        s.on_repetition(RepetitionEvent(1, "一百元", True))
        s.on_repetition(RepetitionEvent(2, "一二三四", True))

        grown = book(
            cand("A", f1=("一百元",), f2=("1234", "一二三四")),
            cand("B", f1=("一百元",), f2=("5678", "五六七八")),
            cand("C", f1=("一百元",), f2=("1234", "一二三四")),
        )
        blocked = s.submit((1, 2), book=grown)
        self.assertEqual(blocked.status, SubmitStatus.SUPPLEMENTAL_REQUIRED)

        # 补充播放一个含新区分信息的段（假设金额本身仍相同，需靠别的字段区分）
        # 本例中 A 与 C 在所有字段同音，任何补充都无法区分 -> 保持补充确认
        again = s.submit((1, 2), book=grown)
        self.assertEqual(again.status, SubmitStatus.SUPPLEMENTAL_REQUIRED)
        self.assertIn("C", again.ambiguous_candidate_ids)

    def test_cannot_submit_after_confirmed(self):
        bk = _book()
        plan = plan_confirmation(bk, "A", (1, 2), (_seg(0, (1,), 300), _seg(1, (2,), 400)), 2000)
        s = ConfirmationSession(plan=plan, book=bk)
        for i in plan.segment_indices:
            s.on_playback(PlaybackEvent(i, "A", True))
        s.on_repetition(RepetitionEvent(1, "一百元", True))
        s.on_repetition(RepetitionEvent(2, "一二三四", True))
        self.assertEqual(s.submit((1, 2)).status, SubmitStatus.CONFIRMED)
        self.assertTrue(s.confirmed)
        with self.assertRaises(RuntimeError):
            s.submit((1, 2))
        with self.assertRaises(RuntimeError):
            s.on_playback(PlaybackEvent(0, "A", True))


class EventValidationTests(unittest.TestCase):
    def _session(self):
        bk = _book()
        plan = plan_confirmation(bk, "A", (1, 2), (_seg(0, (1,), 300), _seg(1, (2,), 400)), 2000)
        return ConfirmationSession(plan=plan, book=bk)

    def test_playback_wrong_target_rejected(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.on_playback(PlaybackEvent(0, "B", True))

    def test_playback_unplanned_segment_rejected(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.on_playback(PlaybackEvent(9, "A", True))


if __name__ == "__main__":
    unittest.main()
