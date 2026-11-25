import unittest

from confirm import (
    ConfirmationSession,
    HomophoneTable,
    PlaybackEvent,
    RepetitionEvent,
    Segment,
    SubmitStatus,
    plan_confirmation,
)
from tests.helpers import book, cand


class HomophoneSubmitTests(unittest.TestCase):
    def test_homophone_rep_blocks_distinguishing_field(self):
        # 同音表把 B 的尾号并入 A 的尾号等价类，则尾号不再能区分 A/B。
        table = HomophoneTable([("1234", "一二三四", "5678", "五六七八")])
        bk = book(
            cand("A", f1=("一百元",), f2=("1234", "一二三四")),
            cand("B", f1=("一百元",), f2=("5678", "五六七八")),
        )
        segs = (Segment(0, (1,), 300), Segment(1, (2,), 400))
        # 没有可区分字段 -> 计划期即无解
        self.assertIsNone(
            plan_confirmation(bk, "A", (1, 2), segs, 2000, homophones=table)
        )

    def test_matched_alias_via_homophone_table_counts(self):
        table = HomophoneTable([("张伟", "zhang wei")])
        bk = book(cand("A", f1=("100",), f2=("张伟",)))
        segs = (Segment(0, (1,), 300), Segment(1, (2,), 400))
        plan = plan_confirmation(bk, "A", (1, 2), segs, 2000)
        s = ConfirmationSession(plan=plan, book=bk, homophones=table)
        for i in plan.segment_indices:
            s.on_playback(PlaybackEvent(i, "A", True))
        s.on_repetition(RepetitionEvent(1, "100", True))
        # 复诵 “zhangwei” 应能对上字段 2（经同音表归一）
        s.on_repetition(RepetitionEvent(2, "zhang wei", True))
        result = s.submit((1, 2))
        self.assertEqual(result.status, SubmitStatus.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
