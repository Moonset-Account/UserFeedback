import unittest

from session_model.model import Decision, Op, Primary
from session_model.regression import evaluate
from session_model.scenarios import Trace, run_all


def _read_decision(order, scenario, sid, generation, version, verdict):
    return Decision(
        order=order, scenario=scenario, action="READ", node="replica-x",
        session_id=sid, op_id=None, generation=generation, write_ack=None,
        replica_version=version, verdict=verdict,
    )


def _commit_decision(order, scenario, sid, op_id, generation, verdict="COMMITTED", ack=True):
    return Decision(
        order=order, scenario=scenario, action="COMMIT:LOGIN", node="primary",
        session_id=sid, op_id=op_id, generation=generation, write_ack=ack,
        replica_version=None, verdict=verdict,
    )


def _forged_trace(decisions, setup=None):
    p = Primary()
    if setup:
        setup(p)
    return Trace("forged", decisions, p, {})


def _by_rule(results, rule_id):
    return next(r for r in results if r.rule_id == rule_id)


class TestReferenceSuitePasses(unittest.TestCase):
    def test_reference_suite_all_rules_pass(self):
        results = evaluate(run_all())
        self.assertTrue(results, "回归规则不能为空")
        for r in results:
            self.assertEqual(r.status, "PASS",
                             "%s 应通过, 失败: %s" % (r.rule_id, r.failures))

    def test_new_generation_remains_accessible(self):
        # R4：通过校验的新会话仍可正常访问，且有真实 ALLOW 证据
        results = evaluate(run_all())
        r4 = _by_rule(results, "R4")
        self.assertEqual(r4.status, "PASS")
        self.assertGreaterEqual(r4.checked, 1)


class TestRegressionDetectsViolations(unittest.TestCase):
    def test_r1_stale_allow_detected(self):
        def setup(p):
            p.begin_session("s1")
            p.commit("s1", Op.LOGIN, "op-1")    # seq1 gen1
            p.commit("s1", Op.ELEVATE, "op-2")  # seq2 gen2
        trace = _forged_trace([_read_decision(1, "forged", "s1", 1, 2, "ALLOW")], setup)
        r1 = _by_rule(evaluate([trace]), "R1")
        self.assertEqual(r1.status, "FAIL")
        self.assertEqual(r1.failures[0].rule_id, "R1")
        self.assertEqual(r1.failures[0].replica_version, 2)

    def test_r2_duplicate_issuance_detected(self):
        decisions = [
            _commit_decision(1, "forged", "s1", "op-x", 1),
            _commit_decision(2, "forged", "s1", "op-x", 2),
        ]
        trace = _forged_trace(decisions)
        r2 = _by_rule(evaluate([trace]), "R2")
        self.assertEqual(r2.status, "FAIL")

    def test_r2_retry_different_generation_detected(self):
        decisions = [
            _commit_decision(1, "forged", "s1", "op-x", 1),
            _commit_decision(2, "forged", "s1", "op-x", 2,
                             verdict="COMMIT_IDEMPOTENT_REPLAY"),
        ]
        trace = _forged_trace(decisions)
        r2 = _by_rule(evaluate([trace]), "R2")
        self.assertEqual(r2.status, "FAIL")

    def test_r3_unapplied_generation_allow_detected(self):
        def setup(p):
            p.begin_session("s1")
            p.commit("s1", Op.LOGIN, "op-1", deliver_ack=False)  # seq1 gen1, ack 缺失
        trace = _forged_trace([_read_decision(1, "forged", "s1", 1, 0, "ALLOW")], setup)
        r3 = _by_rule(evaluate([trace]), "R3")
        self.assertEqual(r3.status, "FAIL")
        failure = r3.failures[0]
        self.assertEqual(failure.rule_id, "R3")
        self.assertFalse(failure.write_ack, "失败记录应关联到缺失的提交确认")
        self.assertEqual(failure.replica_version, 0)

    def test_r5_allow_after_logout_detected(self):
        def setup(p):
            p.begin_session("s1")
            p.commit("s1", Op.LOGIN, "op-1")    # seq1
            p.commit("s1", Op.LOGOUT, "op-2")  # seq2 注销
        trace = _forged_trace([_read_decision(1, "forged", "s1", 1, 2, "ALLOW")], setup)
        r5 = _by_rule(evaluate([trace]), "R5")
        self.assertEqual(r5.status, "FAIL")
        self.assertEqual(r5.failures[0].replica_version, 2)

    def test_lagging_replica_allow_is_not_violation(self):
        # 副本尚未复制到新代次/注销事件时的放行属于信息性，不构成违规
        def setup(p):
            p.begin_session("s1")
            p.commit("s1", Op.LOGIN, "op-1")    # seq1
            p.commit("s1", Op.LOGOUT, "op-2")   # seq2
        trace = _forged_trace([_read_decision(1, "forged", "s1", 1, 1, "ALLOW")], setup)
        results = evaluate([trace])
        self.assertEqual(_by_rule(results, "R1").status, "PASS")
        self.assertEqual(_by_rule(results, "R5").status, "PASS")


if __name__ == "__main__":
    unittest.main()
