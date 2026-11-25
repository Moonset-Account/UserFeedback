import unittest

from session_recycler import (
    ConnectionNotAvailableError,
    ConnectionNotFoundError,
    ConnectionStatus,
    InvalidReturnStateError,
    OperationStatus,
    SessionRecycler,
)


class ReceiptAndPoolTests(unittest.TestCase):
    def setUp(self):
        self.recycler = SessionRecycler()
        self.recycler.register(
            "c1",
            baseline_parameters={"p": "base", "timezone": "UTC"},
        )

    def _dirty_connection(self, cid="c1"):
        self.recycler.handle_event(cid, {"type": "begin", "transaction_id": "tx-1"})
        self.recycler.handle_event(
            cid, {"type": "set_local", "name": "tx_only", "value": "1"}
        )
        self.recycler.handle_event(
            cid, {"type": "set_session", "name": "p", "value": "changed"}
        )
        self.recycler.handle_event(
            cid, {"type": "temp_table_created", "name": "temp_a"}
        )

    def test_clean_return_without_actions_is_immediately_borrowable(self):
        cid = self.recycler.checkout("c1")
        op = self.recycler.return_connection(cid)

        self.assertTrue(op.is_empty)
        self.assertEqual(OperationStatus.SUCCEEDED, op.status)
        self.assertIn("c1", self.recycler.borrowable)
        self.assertTrue(self.recycler.is_borrowable("c1"))

    def test_connection_returns_to_pool_only_after_every_success_receipt(self):
        cid = self.recycler.checkout("c1")
        self._dirty_connection(cid)
        op = self.recycler.return_connection(cid)

        self.assertEqual(
            ["rollback", "param:p", "temp:temp_a"], [a.key for a in op.actions]
        )
        self.assertNotIn(cid, self.recycler.borrowable)

        rollback = op.find_action("rollback")
        param = op.find_action("param:p")
        temp = op.find_action("temp:temp_a")

        result = self.recycler.acknowledge(rollback.id, True)
        self.assertEqual("applied", result.outcome)
        self.assertNotIn(cid, self.recycler.borrowable)
        self.assertEqual(
            [], self.recycler.get_state(cid).open_transactions
        )
        self.assertEqual({}, self.recycler.get_state(cid).transaction_parameters)

        self.recycler.acknowledge(param.id, True)
        self.assertNotIn(cid, self.recycler.borrowable)
        self.assertEqual(
            "base", self.recycler.get_state(cid).session_parameters["p"]
        )

        final = self.recycler.acknowledge(temp.receipt())
        self.assertTrue(final.returned_to_pool)
        self.assertIn(cid, self.recycler.borrowable)
        self.assertEqual(ConnectionStatus.AVAILABLE, self.recycler.connections[cid].status)
        state = self.recycler.get_state(cid)
        self.assertNotIn("temp_a", state.temporary_objects)
        self.assertTrue(state.is_at_baseline())

    def test_out_of_order_success_receipts_are_correlated_by_action_id(self):
        cid = self.recycler.checkout("c1")
        self._dirty_connection(cid)
        op = self.recycler.return_connection(cid)

        temp = op.find_action("temp:temp_a")
        param = op.find_action("param:p")
        rollback = op.find_action("rollback")

        self.recycler.acknowledge(temp.id, True)
        self.assertNotIn(cid, self.recycler.borrowable)
        self.recycler.acknowledge(param.id, True)
        self.assertNotIn(cid, self.recycler.borrowable)
        result = self.recycler.acknowledge(rollback.id, True)

        self.assertTrue(result.returned_to_pool)
        self.assertIn(cid, self.recycler.borrowable)
        self.assertTrue(self.recycler.get_state(cid).is_at_baseline())

    def test_failed_current_restoration_removes_connection_from_borrowable_set(self):
        self.recycler.register("c2", baseline_parameters={"q": "base"})
        c1 = self.recycler.checkout("c1")
        c2 = self.recycler.checkout("c2")
        self.recycler.handle_event(c1, {"type": "set", "name": "p", "value": "x"})
        self.recycler.handle_event(c1, {"type": "create temp table", "name": "t"})
        self.recycler.handle_event(c2, {"type": "set", "name": "q", "value": "y"})

        op1 = self.recycler.return_connection(c1)
        op2 = self.recycler.return_connection(c2)
        self.assertEqual(set(), self.recycler.borrowable)

        param_action = op1.find_action("param:p")
        drop_action = op1.find_action("temp:t")
        self.recycler.acknowledge(param_action.id, True)
        failed = self.recycler.acknowledge(
            drop_action.id, False, error="cannot drop temp table"
        )

        self.assertEqual("failure_quarantined", failed.outcome)
        self.assertTrue(failed.quarantined)
        self.assertNotIn(c1, self.recycler.borrowable)
        self.assertEqual(
            ConnectionStatus.QUARANTINED, self.recycler.connections[c1].status
        )
        # c2 and its operation are independent.
        self.assertEqual(ConnectionStatus.RECYCLING, self.recycler.connections[c2].status)
        self.recycler.succeed_all(op2)
        self.assertEqual({"c2"}, self.recycler.borrowable)

    def test_late_success_for_another_action_after_failure_is_recorded_only(self):
        cid = self.recycler.checkout("c1")
        self._dirty_connection(cid)
        op = self.recycler.return_connection(cid)

        rollback = op.find_action("rollback")
        param = op.find_action("param:p")
        temp = op.find_action("temp:temp_a")

        self.recycler.acknowledge(rollback.id, True)
        failure = self.recycler.acknowledge(param.id, False, error="reset failed")
        self.assertTrue(failure.quarantined)

        late = self.recycler.acknowledge(temp.id, True)
        self.assertEqual("applied_late", late.outcome)
        self.assertFalse(late.applied_to_connection)
        self.assertFalse(late.returned_to_pool)
        self.assertNotIn(cid, self.recycler.borrowable)
        self.assertIn("temp_a", self.recycler.get_state(cid).temporary_objects)
        # The operation-scoped snapshot did receive the late receipt.
        self.assertNotIn("temp_a", late.operation.state_for_receipts.temporary_objects)

        # A conflicting success for the already-failed action cannot revive it.
        conflict = self.recycler.acknowledge(param.id, True)
        self.assertEqual("conflicting_terminal_receipt", conflict.outcome)
        self.assertFalse(conflict.accepted)
        self.assertNotIn(cid, self.recycler.borrowable)

    def test_duplicate_receipt_from_previous_return_does_not_ack_new_return(self):
        cid = self.recycler.checkout("c1")
        self.recycler.handle_event(cid, {"type": "set", "name": "p", "value": "first"})
        op1 = self.recycler.return_connection(cid)
        self.recycler.succeed_all(op1)
        self.assertIn(cid, self.recycler.borrowable)

        # The proxy borrows the same physical connection again.
        self.recycler.checkout(cid)
        self.recycler.handle_event(cid, {"type": "set", "name": "p", "value": "second"})
        op2 = self.recycler.return_connection(cid)
        op2_param = op2.find_action("param:p")
        self.assertEqual("second", op2_param.arguments["current_value"])

        old_param = op1.find_action("param:p")
        duplicate = self.recycler.acknowledge(old_param.id, True)

        self.assertEqual("duplicate", duplicate.outcome)
        self.assertTrue(duplicate.stale)
        self.assertFalse(duplicate.applied_to_connection)
        self.assertTrue(op2_param.pending)
        self.assertEqual("second", self.recycler.get_state(cid).session_parameters["p"])
        self.assertNotIn(cid, self.recycler.borrowable)

        self.recycler.acknowledge(op2_param.id, True)
        self.assertIn(cid, self.recycler.borrowable)
        self.assertEqual("base", self.recycler.get_state(cid).session_parameters["p"])

    def test_late_receipt_for_superseded_return_does_not_change_new_generation(self):
        cid = self.recycler.checkout("c1")
        self._dirty_connection(cid)
        op1 = self.recycler.return_connection(cid)
        old_rollback = op1.find_action("rollback")
        old_param = op1.find_action("param:p")

        self.recycler.acknowledge(old_rollback.id, True)
        op2 = self.recycler.return_connection(cid, force_new_generation=True)

        self.assertEqual(OperationStatus.SUPERSEDED, op1.status)
        self.assertEqual(["param:p", "temp:temp_a"], [a.key for a in op2.actions])
        new_param = op2.find_action("param:p")

        stale_success = self.recycler.acknowledge(old_param.id, True)
        self.assertEqual("applied_stale", stale_success.outcome)
        self.assertTrue(stale_success.stale)
        self.assertFalse(stale_success.applied_to_connection)
        self.assertTrue(new_param.pending)
        self.assertEqual(
            "changed", self.recycler.get_state(cid).session_parameters["p"]
        )

        stale_failure = self.recycler.acknowledge(op1.find_action("temp:temp_a").id, False)
        self.assertEqual("failure_applied_stale", stale_failure.outcome)
        self.assertFalse(stale_failure.quarantined)
        self.assertEqual(
            ConnectionStatus.RECYCLING, self.recycler.connections[cid].status
        )

        self.recycler.succeed_all(op2)
        self.assertIn(cid, self.recycler.borrowable)
        self.assertTrue(self.recycler.get_state(cid).is_at_baseline())

    def test_administrative_reintroduce_then_late_receipt_is_scoped_to_old_return(self):
        cid = self.recycler.checkout("c1")
        self.recycler.handle_event(cid, {"type": "set", "name": "p", "value": "bad"})
        op1 = self.recycler.return_connection(cid)
        self.recycler.acknowledge(op1.actions[0].id, False, error="boom")
        self.assertNotIn(cid, self.recycler.borrowable)

        self.recycler.reintroduce(cid, baseline_parameters={"p": "base"})
        self.assertIn(cid, self.recycler.borrowable)
        self.recycler.checkout(cid)
        op2 = self.recycler.return_connection(cid)
        self.assertTrue(op2.is_empty)

        late = self.recycler.acknowledge(op1.actions[0].id, True)
        self.assertEqual("conflicting_terminal_receipt", late.outcome)
        self.assertTrue(late.stale)
        self.assertIn(cid, self.recycler.borrowable)

    def test_double_return_is_rejected_unless_a_new_generation_is_forced(self):
        cid = self.recycler.checkout("c1")
        self.recycler.handle_event(cid, {"type": "set", "name": "p", "value": "x"})
        first = self.recycler.return_connection(cid)

        with self.assertRaises(InvalidReturnStateError):
            self.recycler.return_connection(cid)

        second = self.recycler.return_connection(cid, force_new_generation=True)
        self.assertEqual(OperationStatus.SUPERSEDED, first.status)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.id, self.recycler.connections[cid].active_operation_id)

    def test_unknown_and_unavailable_connections_raise_clear_errors(self):
        with self.assertRaises(ConnectionNotFoundError):
            self.recycler.checkout("missing")

        self.recycler.borrowable.clear()
        with self.assertRaises(ConnectionNotAvailableError):
            self.recycler.checkout()

    def test_unknown_and_mismatched_receipts_do_not_change_pool(self):
        result = self.recycler.acknowledge("op-missing:1", True)
        self.assertFalse(result.accepted)
        self.assertEqual("unknown_operation", result.outcome)

        cid = self.recycler.checkout("c1")
        self.recycler.handle_event(cid, {"type": "set", "name": "p", "value": "x"})
        op = self.recycler.return_connection(cid)
        mismatch = self.recycler.acknowledge(
            op.id, op.actions[0].id, True, connection_id="other"
        )
        self.assertEqual("connection_mismatch", mismatch.outcome)
        self.assertTrue(op.actions[0].pending)
        self.assertNotIn(cid, self.recycler.borrowable)

    def test_connection_and_key_receipt_form_resolves_current_return(self):
        cid = self.recycler.checkout("c1")
        self.recycler.handle_event(cid, {"type": "set", "name": "p", "value": "x"})
        op = self.recycler.return_connection(cid)

        result = self.recycler.acknowledge(cid, "param:p", True)
        self.assertEqual(op.id, result.operation_id)
        self.assertEqual("operation_succeeded", result.outcome)
        self.assertIn(cid, self.recycler.borrowable)

    def test_receipt_mapping_and_positional_operation_forms(self):
        cid = self.recycler.checkout("c1")
        self.recycler.handle_event(cid, {"type": "set", "name": "p", "value": "x"})
        op = self.recycler.return_connection(cid)
        action = op.actions[0]

        first = self.recycler.acknowledge(op.id, 1, True)
        self.assertEqual("operation_succeeded", first.outcome)
        self.assertIn(cid, self.recycler.borrowable)

        # A second operation exercises mapping/key forms.
        self.recycler.checkout(cid)
        self.recycler.handle_event(cid, {"type": "set", "name": "p", "value": "y"})
        op2 = self.recycler.return_connection(cid)
        second = self.recycler.acknowledge(
            {"return_id": op2.id, "action": "param:p", "ok": True}
        )
        self.assertEqual("operation_succeeded", second.outcome)
        self.assertIn(cid, self.recycler.borrowable)


if __name__ == "__main__":
    unittest.main()
