import unittest
from dataclasses import dataclass

from session_recycler import SessionState, StateEvent


class StateTrackingTests(unittest.TestCase):
    def test_tracks_temporary_tables_and_removes_dropped_objects(self):
        state = SessionState("c1")

        state.apply({"type": "temp_table_created", "table": "temp_orders"})
        state.apply(StateEvent(type="temporary_object_created", name="temp_items"))
        state.apply({"type": "create temp table", "table_name": "temp_to_drop"})

        self.assertEqual(
            {"temp_orders", "temp_items", "temp_to_drop"},
            set(state.temporary_objects),
        )
        self.assertEqual(3, len(state.temporary_tables))

        state.apply({"type": "drop_temp_table", "name": "temp_to_drop"})
        self.assertNotIn("temp_to_drop", state.temporary_objects)
        self.assertEqual({"temp_orders", "temp_items"}, set(state.temporary_tables))

    def test_session_parameters_are_compared_with_baseline(self):
        state = SessionState(
            "c1",
            baseline_parameters={"search_path": "public", "work_mem": "4MB"},
        )

        state.apply({"type": "set", "name": "search_path", "value": "analytics"})
        state.apply({"type": "session_parameter_set", "name": "extra", "value": "x"})
        changed = state.changed_session_parameters()

        self.assertEqual("analytics", changed["search_path"])
        self.assertEqual("x", changed["extra"])
        self.assertNotIn("work_mem", changed)

        # Returning a parameter to its baseline means no restoration is needed.
        state.apply({"type": "set", "name": "search_path", "value": "public"})
        self.assertNotIn("search_path", state.changed_session_parameters())

    def test_parameter_reset_to_absent_baseline_is_still_a_change(self):
        state = SessionState("c1", baseline_parameters={"managed": "on"})
        state.apply({"type": "set", "name": "managed", "value": "off"})
        state.apply({"type": "parameter_reset", "name": "managed"})

        self.assertNotIn("managed", state.session_parameters)
        self.assertIn("managed", state.changed_session_parameters())

    def test_transaction_local_parameters_are_cleared_by_commit_and_rollback(self):
        state = SessionState("c1")
        state.apply({"type": "begin", "transaction_id": "tx-1"})
        state.apply({"type": "set_local", "name": "statement_timeout", "value": "2s"})
        state.apply({"type": "set_local", "name": "app.trace", "value": "1"})

        self.assertEqual(["tx-1"], state.open_transactions)
        self.assertEqual(1, state.open_transaction_count)
        self.assertEqual(2, len(state.transaction_parameters))
        self.assertEqual("2s", state.transaction_parameters["statement_timeout"])

        state.apply({"type": "rollback", "transaction_id": "tx-1"})
        self.assertEqual([], state.open_transactions)
        self.assertEqual({}, state.transaction_parameters)

        state.apply({"type": "begin", "transaction_id": "tx-2"})
        state.apply({"type": "transaction_parameter_set", "name": "x", "value": "1"})
        state.apply({"type": "commit", "transaction_id": "tx-2"})
        self.assertEqual({}, state.transaction_parameters)

    def test_open_transaction_events_without_ids_use_current_transaction(self):
        state = SessionState("c1")
        state.apply("begin")
        first = state.current_transaction_id
        self.assertTrue(state.has_open_transaction)

        state.apply({"type": "set_local", "name": "x", "value": 1})
        self.assertEqual(first, state.current_transaction_id)

        state.apply("rollback")
        self.assertFalse(state.has_open_transaction)
        self.assertEqual({}, state.transaction_parameters)

    def test_duplicate_event_ids_are_applied_once(self):
        state = SessionState("c1")
        event = {
            "type": "temp_table_created",
            "name": "t",
            "event_id": "evt-1",
        }
        state.apply(event)
        state.apply(event)
        self.assertEqual(1, state.event_count)
        self.assertEqual(["evt-1"], list(state.applied_event_ids))

    def test_accepts_ordinary_parsed_event_objects(self):
        @dataclass
        class ParsedTempEvent:
            type: str
            name: str

        state = SessionState("c1")
        state.apply(ParsedTempEvent(type="temp_table_created", name="obj_t"))
        self.assertIn("obj_t", state.temporary_objects)

    def test_baseline_event_can_initialize_state(self):
        state = SessionState("c1")
        state.apply(
            {
                "type": "baseline",
                "parameters": {"timezone": "UTC"},
                "temporary_objects": ["baseline_temp"],
            }
        )
        self.assertEqual("UTC", state.baseline_parameters["timezone"])
        self.assertIn("baseline_temp", state.baseline_temporary_objects)
        self.assertIn("baseline_temp", state.temporary_objects)
        self.assertEqual([], [o.key for o in state.leftover_temporary_objects()])


if __name__ == "__main__":
    unittest.main()
