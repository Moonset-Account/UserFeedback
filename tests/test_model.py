import unittest

from session_model.model import Op, Primary, Replica, State, Verdict


class TestTransitionRules(unittest.TestCase):
    def setUp(self):
        self.p = Primary()
        self.p.begin_session("s1", identity="alice")

    def test_login_from_anonymous(self):
        res = self.p.commit("s1", Op.LOGIN, "op-1")
        self.assertIsNotNone(res.entry)
        self.assertEqual(res.entry.generation, 1)
        self.assertEqual(self.p.sessions["s1"].state, State.AUTHENTICATED)

    def test_elevate_requires_authenticated(self):
        res = self.p.commit("s1", Op.ELEVATE, "op-2")
        self.assertEqual(res.error, "invalid_transition")
        self.assertEqual(self.p.sessions["s1"].state, State.ANONYMOUS)
        self.assertEqual(len(self.p.log), 0)

    def test_elevate_same_identity_after_login(self):
        self.p.commit("s1", Op.LOGIN, "op-1")
        res = self.p.commit("s1", Op.ELEVATE, "op-2")
        self.assertEqual(res.entry.generation, 2)
        self.assertEqual(self.p.sessions["s1"].state, State.ELEVATED)
        # 同一身份：identity 未变
        self.assertEqual(self.p.sessions["s1"].identity, "alice")

    def test_logout_from_authenticated_and_elevated(self):
        self.p.commit("s1", Op.LOGIN, "op-1")
        res = self.p.commit("s1", Op.LOGOUT, "op-3")
        self.assertEqual(res.entry.to_state, State.LOGGED_OUT)

        self.p.begin_session("s2")
        self.p.commit("s2", Op.LOGIN, "op-4")
        self.p.commit("s2", Op.ELEVATE, "op-5")
        res2 = self.p.commit("s2", Op.LOGOUT, "op-6")
        self.assertEqual(res2.entry.to_state, State.LOGGED_OUT)

    def test_login_twice_invalid(self):
        self.p.commit("s1", Op.LOGIN, "op-1")
        res = self.p.commit("s1", Op.LOGIN, "op-7")
        self.assertEqual(res.error, "invalid_transition")

    def test_logout_terminal(self):
        self.p.commit("s1", Op.LOGIN, "op-1")
        self.p.commit("s1", Op.LOGOUT, "op-3")
        res = self.p.commit("s1", Op.LOGIN, "op-8")
        self.assertEqual(res.error, "invalid_transition")

    def test_generation_increments_per_commit(self):
        self.p.commit("s1", Op.LOGIN, "op-1")
        self.p.commit("s1", Op.ELEVATE, "op-2")
        self.assertEqual(self.p.sessions["s1"].generation, 2)
        self.assertEqual([e.seq for e in self.p.log], [1, 2])

    def test_idempotent_retry_same_op_id(self):
        first = self.p.commit("s1", Op.LOGIN, "op-1", deliver_ack=False)
        retry = self.p.commit("s1", Op.LOGIN, "op-1")
        self.assertFalse(first.replay)
        self.assertTrue(retry.replay)
        self.assertEqual(retry.entry.generation, first.entry.generation)
        self.assertEqual(len(self.p.log), 1, "重试不得重复换发")

    def test_interrupted_before_apply_not_committed(self):
        res = self.p.commit("s1", Op.LOGIN, "op-1", apply=False)
        self.assertEqual(res.error, "interrupted_before_apply")
        self.assertEqual(self.p.sessions["s1"].state, State.ANONYMOUS)
        self.assertEqual(len(self.p.log), 0)
        retry = self.p.commit("s1", Op.LOGIN, "op-1")
        self.assertIsNotNone(retry.entry)
        self.assertFalse(retry.replay)

    def test_ack_lost_still_committed(self):
        res = self.p.commit("s1", Op.LOGIN, "op-1", deliver_ack=False)
        self.assertFalse(res.ack)
        self.assertFalse(res.entry.ack_delivered)
        self.assertEqual(self.p.sessions["s1"].state, State.AUTHENTICATED)


class TestReplicaReads(unittest.TestCase):
    def setUp(self):
        self.p = Primary()
        self.p.begin_session("s1", identity="bob")
        self.p.commit("s1", Op.LOGIN, "op-1")     # seq1 gen1
        self.p.commit("s1", Op.ELEVATE, "op-2")   # seq2 gen2
        self.r = Replica("r1")

    def test_allow_current_generation(self):
        self.r.apply_up_to(self.p, 2)
        self.assertEqual(self.r.read("s1", 2), Verdict.ALLOW)

    def test_stale_generation_denied(self):
        self.r.apply_up_to(self.p, 2)
        self.assertEqual(self.r.read("s1", 1), Verdict.DENY_STALE_GENERATION)

    def test_pending_for_unreplicated_generation(self):
        self.r.apply_up_to(self.p, 1)
        self.assertEqual(self.r.read("s1", 2), Verdict.PENDING_REPLICATION)

    def test_unknown_session_safe_reject(self):
        self.assertEqual(self.r.read("s1", 1), Verdict.DENY_UNKNOWN_SESSION)
        self.assertEqual(self.r.read("ghost", 1), Verdict.DENY_UNKNOWN_SESSION)

    def test_logged_out_denied(self):
        self.p.commit("s1", Op.LOGOUT, "op-3")    # seq3
        self.r.apply_up_to(self.p, 3)
        self.assertEqual(self.r.read("s1", 1), Verdict.DENY_LOGGED_OUT)
        self.assertEqual(self.r.read("s1", 3), Verdict.DENY_LOGGED_OUT)

    def test_not_authenticated_denied(self):
        self.r.apply_up_to(self.p, 2)
        self.assertEqual(self.r.read("s1", 0), Verdict.DENY_NOT_AUTHENTICATED)

    def test_apply_up_to_ordered_and_idempotent(self):
        self.r.apply_up_to(self.p, 1)
        self.assertEqual(self.r.version, 1)
        self.assertEqual(self.r.read("s1", 1), Verdict.ALLOW)
        self.r.apply_up_to(self.p, 1)
        self.assertEqual(self.r.version, 1)
        self.r.apply_up_to(self.p, 2)
        self.assertEqual(self.r.version, 2)
        self.assertEqual(self.r.read("s1", 1), Verdict.DENY_STALE_GENERATION)

    def test_primary_read_verdicts(self):
        self.assertEqual(self.p.read("s1", 2), Verdict.ALLOW)
        self.assertEqual(self.p.read("s1", 1), Verdict.DENY_STALE_GENERATION)
        self.assertEqual(self.p.read("s1", 9), Verdict.DENY_UNKNOWN_GENERATION)
        self.assertEqual(self.p.read("ghost", 1), Verdict.DENY_UNKNOWN_SESSION)


if __name__ == "__main__":
    unittest.main()
