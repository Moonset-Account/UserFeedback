import unittest

from session_recycler import ActionKind, SessionState, plan_recovery


class RecoveryPlanningTests(unittest.TestCase):
    def test_clean_session_needs_no_actions(self):
        state = SessionState(
            "c1",
            baseline_parameters={"timezone": "UTC"},
        )
        plan = plan_recovery(state)
        self.assertTrue(plan.is_empty)
        self.assertEqual([], plan.actions)

    def test_session_parameter_actions_use_baseline_or_reset(self):
        state = SessionState("c1", baseline_parameters={"a": "1", "kept": "x"})
        state.set_session_parameter("a", "2")
        state.set_session_parameter("extra", "on")

        plan = plan_recovery(state)
        param_actions = [
            a for a in plan.actions if a.kind == ActionKind.RESTORE_SESSION_PARAMETER
        ]
        self.assertEqual(["a", "extra"], [a.target for a in param_actions])

        by_target = {a.target: a for a in param_actions}
        self.assertEqual("1", by_target["a"].arguments["baseline_value"])
        self.assertFalse(by_target["a"].arguments["baseline_is_default"])
        self.assertIn("SET", by_target["a"].arguments["sql"])

        self.assertIsNone(by_target["extra"].arguments["baseline_value"])
        self.assertTrue(by_target["extra"].arguments["baseline_is_default"])
        self.assertEqual('RESET "extra"', by_target["extra"].arguments["sql"])

    def test_plan_order_is_rollback_then_parameters_then_temporary_objects(self):
        state = SessionState("c1", baseline_parameters={"p": "base"})
        state.begin_transaction("tx-1")
        state.set_transaction_parameter("local_p", "1", "tx-1")
        state.set_session_parameter("p", "changed")
        state.create_temporary_object("z_table")
        state.create_temporary_object("a_table")

        plan = plan_recovery(state)
        kinds_and_targets = [(a.kind, a.target) for a in plan.actions]

        self.assertEqual(
            [
                (ActionKind.ROLLBACK_TRANSACTION, "tx-1"),
                (ActionKind.RESTORE_SESSION_PARAMETER, "p"),
                (ActionKind.DROP_TEMPORARY_OBJECT, "a_table"),
                (ActionKind.DROP_TEMPORARY_OBJECT, "z_table"),
            ],
            kinds_and_targets,
        )
        self.assertEqual(
            ["local_p"],
            [s.target for s in plan.suppressed],
        )
        self.assertIn(
            "rollback_discards_transaction_local_parameter",
            plan.suppressed[0].reason,
        )
        self.assertNotIn(
            ActionKind.RESET_TRANSACTION_PARAMETER,
            [a.kind for a in plan.actions],
        )

    def test_closed_transaction_does_not_create_rollback(self):
        state = SessionState("c1")
        state.begin_transaction()
        state.set_transaction_parameter("x", "1")
        state.commit_transaction()

        plan = plan_recovery(state)
        self.assertEqual([], plan.actions)
        self.assertEqual([], plan.suppressed)

    def test_temporary_object_that_was_dropped_is_not_planned(self):
        state = SessionState("c1")
        state.create_temporary_object("t1")
        state.create_temporary_object("t2")
        state.drop_temporary_object("t1")

        plan = plan_recovery(state)
        drop_targets = [
            a.target for a in plan.actions if a.kind == ActionKind.DROP_TEMPORARY_OBJECT
        ]
        self.assertEqual(["t2"], drop_targets)

    def test_baseline_temporary_objects_are_not_dropped_as_leftovers(self):
        state = SessionState("c1", baseline_temporary_objects=["baseline_t"])
        state.create_temporary_object("extra_t")

        plan = plan_recovery(state)
        self.assertEqual(["extra_t"], [a.target for a in plan.actions])


if __name__ == "__main__":
    unittest.main()
