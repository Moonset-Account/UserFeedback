"""验收用例:运行记录与版本样本清单的对齐组件。

覆盖题面交叉约束:
- 预算按进程组合核对,兼容样本限额内完成(含峰值 == 预算的边界);
- 超限样本必须在规定阶段终止并释放资源;
- 计时被采集器中断 -> 留待补证,重跑可补缺;
- 成功的重跑不能覆盖同条件下未释放资源的记录。
"""
import unittest

from alignment import (
    RELEASE_EVENT,
    EntryStatus,
    Expectation,
    Reconciler,
    Result,
    RunRecord,
    SampleSpec,
    SubmitStatus,
)

COMPAT_SPEC = SampleSpec(
    sample_id="s-compat",
    category="text",
    process_combo=("ingest", "scan"),
    expectation=Expectation.COMPATIBLE,
    memory_budget=1024,
)
OVER_LIMIT_SPEC = SampleSpec(
    sample_id="s-over",
    category="binary",
    process_combo=("ingest",),
    expectation=Expectation.OVER_LIMIT,
    memory_budget=2048,
    required_stage="decode",
)


def compat_record(**overrides) -> RunRecord:
    base = dict(
        sample_id="s-compat",
        category="text",
        process_combo=("scan", "ingest"),  # 乱序,验证进程组合归一化
        result=Result.COMPLETED,
        limit_hit=False,
        memory_peak=1000,
        cleanup_events=(RELEASE_EVENT,),
    )
    base.update(overrides)
    return RunRecord(**base)


def over_limit_record(**overrides) -> RunRecord:
    base = dict(
        sample_id="s-over",
        category="binary",
        process_combo=("ingest",),
        result=Result.TERMINATED,
        limit_hit=True,
        memory_peak=4096,
        terminated_stage="decode",
        cleanup_events=(RELEASE_EVENT, "temp_files_purged"),
    )
    base.update(overrides)
    return RunRecord(**base)


class TestAcceptableResults(unittest.TestCase):
    """可接受结果:两类样本各自满足清单要求。"""

    def test_compatible_sample_completed_within_budget_passes(self):
        rec = Reconciler([COMPAT_SPEC])
        outcome = rec.submit(compat_record())
        self.assertEqual(outcome.status, SubmitStatus.STORED)

        report = rec.align()
        self.assertEqual(report.entries[0].status, EntryStatus.PASS)
        self.assertEqual(report.entries[0].reasons, ())
        self.assertTrue(report.is_green)

    def test_over_limit_sample_terminated_at_required_stage_and_released_passes(self):
        rec = Reconciler([OVER_LIMIT_SPEC])
        rec.submit(over_limit_record())

        report = rec.align()
        self.assertEqual(report.entries[0].status, EntryStatus.PASS)
        self.assertTrue(report.is_green)


class TestRejectedResults(unittest.TestCase):
    """应被阻止/拒绝的结果。"""

    def test_over_limit_sample_without_release_fails(self):
        rec = Reconciler([OVER_LIMIT_SPEC])
        rec.submit(over_limit_record(cleanup_events=("temp_files_purged",)))

        report = rec.align()
        entry = report.entries[0]
        self.assertEqual(entry.status, EntryStatus.FAIL)
        self.assertIn("resources not released after the run", entry.reasons)
        self.assertFalse(report.is_green)

    def test_over_limit_sample_terminated_at_wrong_stage_fails(self):
        rec = Reconciler([OVER_LIMIT_SPEC])
        rec.submit(over_limit_record(terminated_stage="parse"))

        entry = rec.align().entries[0]
        self.assertEqual(entry.status, EntryStatus.FAIL)
        self.assertTrue(any("required 'decode'" in r for r in entry.reasons))

    def test_compatible_sample_over_budget_fails(self):
        rec = Reconciler([COMPAT_SPEC])
        rec.submit(compat_record(memory_peak=1025))

        entry = rec.align().entries[0]
        self.assertEqual(entry.status, EntryStatus.FAIL)
        self.assertTrue(any("exceeds budget 1024" in r for r in entry.reasons))

    def test_compatible_sample_hitting_limit_fails(self):
        rec = Reconciler([COMPAT_SPEC])
        rec.submit(compat_record(limit_hit=True))

        entry = rec.align().entries[0]
        self.assertEqual(entry.status, EntryStatus.FAIL)
        self.assertIn("compatible sample hit the limit", entry.reasons)

    def test_successful_rerun_cannot_overwrite_unreleased_record(self):
        """成功重跑不得覆盖同条件下未释放资源的记录,违规结论必须保留。"""
        rec = Reconciler([OVER_LIMIT_SPEC])
        rec.submit(over_limit_record(cleanup_events=()))  # 未释放资源

        rerun = RunRecord(
            sample_id="s-over",
            category="binary",
            process_combo=("ingest",),
            result=Result.COMPLETED,
            limit_hit=False,
            memory_peak=512,
            cleanup_events=(RELEASE_EVENT,),
            run_seq=1,
        )
        outcome = rec.submit(rerun)
        self.assertEqual(outcome.status, SubmitStatus.REJECTED_UNRELEASED_OVERWRITE)
        self.assertFalse(outcome.accepted)

        report = rec.align()
        entry = report.entries[0]
        self.assertEqual(entry.status, EntryStatus.FAIL)
        self.assertEqual(entry.record.run_seq, 0)  # 原记录未被覆盖
        self.assertIn("resources not released after the run", entry.reasons)


class TestBoundaryAndInterleaved(unittest.TestCase):
    """边界与交错场景。"""

    def test_memory_peak_exactly_at_budget_passes(self):
        rec = Reconciler([COMPAT_SPEC])
        rec.submit(compat_record(memory_peak=1024))  # 峰值 == 预算
        self.assertEqual(rec.align().entries[0].status, EntryStatus.PASS)

    def test_interrupted_record_pending_then_rerun_fills_gap(self):
        rec = Reconciler([COMPAT_SPEC])
        rec.submit(compat_record(result=Result.INTERRUPTED, cleanup_events=()))

        report = rec.align()
        self.assertEqual(report.entries[0].status, EntryStatus.PENDING)
        self.assertFalse(report.is_green)

        outcome = rec.submit(compat_record(run_seq=1))  # 重跑补缺
        self.assertEqual(outcome.status, SubmitStatus.GAP_FILLED)
        self.assertEqual(rec.align().entries[0].status, EntryStatus.PASS)

    def test_budget_checked_per_process_combo_with_interleaved_submissions(self):
        """同一输入类别、不同进程组合各自核对预算;记录交错提交互不影响。"""
        solo = SampleSpec("s1", "text", ("ingest",), Expectation.COMPATIBLE, 512)
        duo = SampleSpec("s2", "text", ("ingest", "scan"), Expectation.COMPATIBLE, 2048)
        rec = Reconciler([solo, duo])

        # 交错提交:duo 的记录夹在两条 solo 相关操作之间
        rec.submit(
            RunRecord("s2", "text", ("scan", "ingest"), Result.COMPLETED,
                      False, 1500, cleanup_events=(RELEASE_EVENT,))
        )
        rec.submit(
            RunRecord("s1", "text", ("ingest",), Result.COMPLETED,
                      False, 600, cleanup_events=(RELEASE_EVENT,))  # 超 solo 预算
        )

        report = rec.align()
        by_id = {e.spec.sample_id: e for e in report.entries}
        self.assertEqual(by_id["s2"].status, EntryStatus.PASS)  # 1500 <= 2048
        self.assertEqual(by_id["s1"].status, EntryStatus.FAIL)  # 600 > 512
        self.assertTrue(any("('ingest',)" in r for r in by_id["s1"].reasons))

    def test_missing_record_and_unexpected_record_reported(self):
        rec = Reconciler([COMPAT_SPEC])
        rec.submit(
            RunRecord("ghost", "text", ("ingest", "scan"), Result.COMPLETED,
                      False, 100, cleanup_events=(RELEASE_EVENT,))
        )

        report = rec.align()
        self.assertEqual(report.entries[0].status, EntryStatus.MISSING)
        self.assertEqual([r.sample_id for r in report.unexpected], ["ghost"])
        self.assertFalse(report.is_green)

    def test_final_record_rejects_duplicate_rerun(self):
        rec = Reconciler([COMPAT_SPEC])
        rec.submit(compat_record())
        outcome = rec.submit(compat_record(run_seq=1))
        self.assertEqual(outcome.status, SubmitStatus.REJECTED_DUPLICATE)
        self.assertEqual(rec.align().entries[0].record.run_seq, 0)


if __name__ == "__main__":
    unittest.main()
