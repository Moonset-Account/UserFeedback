import dataclasses
import unittest

from session_model.scenarios import SCENARIOS, run_all


def _reads(trace, **cond):
    return [
        d for d in trace.decisions
        if d.action == "READ" and all(getattr(d, k) == v for k, v in cond.items())
    ]


class TestScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.traces = {t.scenario: t for t in run_all()}

    def test_all_scenarios_present(self):
        self.assertEqual(
            set(SCENARIOS),
            {"normal_login", "concurrent_reads", "retry_after_interruption", "late_logout"},
        )

    def test_normal_login_verdicts(self):
        t = self.traces["normal_login"]
        self.assertEqual(_reads(t, generation=1)[0].verdict, "ALLOW")
        self.assertEqual(_reads(t, generation=0)[0].verdict, "DENY_NOT_AUTHENTICATED")
        self.assertEqual(_reads(t, generation=2)[0].verdict, "PENDING_REPLICATION")
        elevate = [d for d in t.decisions if d.action == "COMMIT:ELEVATE"]
        self.assertEqual(elevate[0].verdict, "REJECT_INVALID_TRANSITION")

    def test_concurrent_reads_mixed_generations(self):
        t = self.traces["concurrent_reads"]
        self.assertEqual(_reads(t, node="replica-a", generation=2)[0].verdict, "ALLOW")
        self.assertEqual(
            _reads(t, node="replica-a", generation=1)[0].verdict, "DENY_STALE_GENERATION")
        self.assertEqual(
            _reads(t, node="replica-b", generation=2)[0].verdict, "PENDING_REPLICATION")
        lagging = _reads(t, node="replica-b", generation=1)[0]
        self.assertEqual(lagging.verdict, "ALLOW")
        self.assertEqual(lagging.replica_version, 1, "滞后副本版本应停留在 seq1")

    def test_retry_after_interruption(self):
        t = self.traces["retry_after_interruption"]
        login_entries = [e for e in t.primary.log if e.op_id == "op-r-login"]
        self.assertEqual(len(login_entries), 1, "确认丢失重试不得重复换发")
        self.assertFalse(login_entries[0].ack_delivered)
        commits = [d for d in t.decisions if d.op_id == "op-r-login"]
        self.assertEqual(commits[-1].verdict, "COMMIT_IDEMPOTENT_REPLAY")
        self.assertEqual(commits[-1].generation, 1)
        elev = [d for d in t.decisions if d.op_id == "op-r-elevate"]
        self.assertEqual(elev[0].verdict, "COMMIT_INTERRUPTED")
        self.assertEqual(elev[-1].verdict, "COMMITTED")
        self.assertEqual(_reads(t, generation=2)[0].verdict, "ALLOW")
        self.assertEqual(_reads(t, generation=1)[-1].verdict, "DENY_STALE_GENERATION")

    def test_late_logout(self):
        t = self.traces["late_logout"]
        primary_read = _reads(t, node="primary")[0]
        self.assertEqual(primary_read.verdict, "DENY_LOGGED_OUT")
        rb_reads = _reads(t, node="replica-b", generation=1)
        self.assertEqual(rb_reads[0].verdict, "ALLOW")   # 注销前基线
        self.assertEqual(rb_reads[1].verdict, "ALLOW")   # 注销事件迟到（信息性）
        self.assertLess(rb_reads[1].replica_version, 2)
        self.assertEqual(rb_reads[-1].verdict, "DENY_LOGGED_OUT")
        self.assertGreaterEqual(rb_reads[-1].replica_version, 2)

    def test_determinism(self):
        first = [[dataclasses.asdict(d) for d in t.decisions] for t in run_all()]
        second = [[dataclasses.asdict(d) for d in t.decisions] for t in run_all()]
        self.assertEqual(first, second, "确定性调度两次运行结果必须一致")

    def test_decision_records_carry_required_fields(self):
        for t in self.traces.values():
            for d in t.decisions:
                self.assertIsInstance(d.order, int)
                self.assertIsNotNone(d.verdict)
                if d.action == "READ":
                    self.assertIsNotNone(d.replica_version)
                if d.action.startswith("COMMIT"):
                    self.assertIsNotNone(d.write_ack)


if __name__ == "__main__":
    unittest.main()
