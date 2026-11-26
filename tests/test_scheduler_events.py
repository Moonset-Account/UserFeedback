import unittest

from hybrid_scheduler.events import Event, EventKind, run_event_log
from hybrid_scheduler.model import Phase, TaskKind, TaskSpec, TaskStatus
from hybrid_scheduler.resources import ResourceVector as R
from hybrid_scheduler.scheduler import HybridScheduler


def stream_spec(stream_id="s1", memory=4):
    return TaskSpec(
        id=stream_id,
        kind=TaskKind.STREAM,
        quota=R.of(memory=memory),
        phases=(Phase(id="run", resources=R.of(slots=1, memory=1)),),
        deadline=100,
    )


def boundary_batch(
    batch_id: str,
    *,
    buffer_memory: int = 0,
    continue_memory: int | None = 0,
    savepoint: bool = True,
    deadline: float | None = 50,
    phase_duration: float = 0.0,
    checkpoint_duration: float = 0.0,
):
    return TaskSpec(
        id=batch_id,
        kind=TaskKind.BATCH,
        deadline=deadline,
        phases=(
            Phase(
                id="boundary",
                duration=phase_duration,
                legal_boundary=True,
                savepoint_allowed=savepoint,
                checkpoint_buffer=R.of(memory=buffer_memory),
                checkpoint_duration=checkpoint_duration,
                continue_resources=None if continue_memory is None else R.of(memory=continue_memory),
            ),
        ),
    )


class HighLevelSchedulerTests(unittest.TestCase):
    def test_feasible_release_grant_and_deferred_recovery_accounting(self):
        scheduler = HybridScheduler(R.of(slots=4, memory=9))
        scheduler.submit(stream_spec(), start=True)
        scheduler.submit(
            boundary_batch("b1", continue_memory=2),
            start=True,
            allocation=R.of(slots=1, memory=2),
        )
        scheduler.submit(
            boundary_batch("b2", buffer_memory=2, continue_memory=None, checkpoint_duration=0.1),
            start=True,
            allocation=R.of(slots=1, memory=2),
        )
        scheduler.lend("s1", "b1", R.of(memory=2))
        scheduler.lend("s1", "b2", R.of(memory=2))

        plan = scheduler.reclaim("s1", at=5, borrower_order=["b2", "b1"])
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.release_order, ("b1", "b2"))
        self.assertEqual(plan.suspended, ("b2",))
        # Three tasks initially occupy one slot each; suspending b2 returns its
        # slot while b1 keeps running in its slot.
        self.assertEqual(plan.available_after_grants.get("slots"), 2)
        self.assertEqual(scheduler.state.task("b1").status, TaskStatus.RUNNING)
        self.assertEqual(scheduler.state.task("b2").status, TaskStatus.PAUSED)
        self.assertTrue(scheduler.state.outstanding_by_owner("s1") == R.zero())
        self.assertIn("b2", scheduler.deferred_resume)

        scheduler.finish("s1", at=8)
        resumed = scheduler.try_resume_deferred(at=8)
        self.assertEqual(resumed, ("b2",))
        self.assertEqual(scheduler.state.task("b2").status, TaskStatus.RUNNING)

    def test_other_stream_borrower_can_break_buffer_dependency_without_pause(self):
        scheduler = HybridScheduler(R.of(memory=4))
        scheduler.submit(stream_spec("s1", 2))
        scheduler.submit(stream_spec("s2", 2))
        scheduler.submit(boundary_batch("a", buffer_memory=2, continue_memory=None))
        scheduler.submit(boundary_batch("h", buffer_memory=0, continue_memory=0))
        scheduler.lend("s1", "a", R.of(memory=2))
        scheduler.lend("s2", "h", R.of(memory=2))

        plan = scheduler.reclaim("s1")
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.release_order, ("h", "a"))
        self.assertNotIn("h", plan.suspended)
        self.assertIn("a", plan.suspended)
        # h's loan to another stream is returned as part of the coordinated
        # buffer cascade.
        self.assertTrue(scheduler.state.outstanding_by_owner("s2") == R.zero())

    def test_deferred_recovery_uses_completion_deadline_order(self):
        scheduler = HybridScheduler(R.of(memory=4))
        scheduler.submit(stream_spec(memory=4))
        scheduler.submit(boundary_batch("late", deadline=100, continue_memory=None))
        scheduler.submit(boundary_batch("urgent", deadline=1, continue_memory=None))
        scheduler.lend("s1", "late", R.of(memory=2))
        scheduler.lend("s1", "urgent", R.of(memory=2))
        plan = scheduler.reclaim("s1")
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(set(plan.suspended), {"late", "urgent"})
        self.assertEqual(set(plan.resume.deferred), {"late", "urgent"})

        scheduler.finish("s1", at=20)
        self.assertEqual(scheduler.try_resume_deferred(at=20), ("urgent", "late"))

    def test_partial_reclaim_can_release_only_requested_amount(self):
        scheduler = HybridScheduler(R.of(memory=4))
        scheduler.submit(stream_spec(memory=4))
        scheduler.submit(boundary_batch("elastic", continue_memory=2))
        scheduler.lend("s1", "elastic", R.of(memory=4))
        plan = scheduler.reclaim("s1", amount=R.of(memory=2))
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.suspended, ())
        self.assertEqual(scheduler.state.task("elastic").allocation, R.of(memory=2))
        self.assertEqual(scheduler.state.outstanding_by_owner("s1"), R.of(memory=2))

    def test_scheduler_can_choose_a_later_savepoint_with_available_buffer(self):
        spec = TaskSpec(
            id="multi-sp",
            kind=TaskKind.BATCH,
            deadline=50,
            phases=(
                Phase(id="work", duration=1),
                Phase(
                    id="early_sp",
                    depends_on=("work",),
                    legal_boundary=True,
                    savepoint_allowed=True,
                    checkpoint_buffer=R.of(memory=2),
                ),
                Phase(
                    id="later_sp",
                    depends_on=("early_sp",),
                    legal_boundary=True,
                    savepoint_allowed=True,
                    checkpoint_buffer=R.zero(),
                ),
            ),
        )
        scheduler = HybridScheduler(R.of(memory=2))
        scheduler.submit(stream_spec(memory=2))
        scheduler.submit(spec, start=True)
        scheduler.lend("s1", "multi-sp", R.of(memory=2))
        plan = scheduler.reclaim("s1")
        self.assertTrue(plan.feasible, plan.reason)
        selected = plan.selected_actions["multi-sp"]
        self.assertEqual(selected.boundary_phase, "later_sp")
        self.assertEqual(plan.suspended, ("multi-sp",))

    def test_missing_legal_boundary_returns_diagnostic_plan(self):
        spec = TaskSpec(id="stuck", kind=TaskKind.BATCH, phases=(Phase(id="compute"),))
        scheduler = HybridScheduler(R.of(memory=2))
        scheduler.submit(stream_spec(memory=2))
        scheduler.submit(spec)
        scheduler.lend("s1", "stuck", R.of(memory=2))
        plan = scheduler.reclaim("s1")
        self.assertFalse(plan.feasible)
        self.assertIsNone(plan.cycle)
        self.assertIn("合法", plan.reason)


class DiscreteEventOrderTests(unittest.TestCase):
    def _independent_events(self, order):
        return [
            Event(0, EventKind.SUBMIT, {"spec": stream_spec()}),
            Event(0, EventKind.SUBMIT, {"spec": boundary_batch("x")}),
            Event(0, EventKind.SUBMIT, {"spec": boundary_batch("y")}),
            Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "x", "amount": R.of(memory=2)}),
            Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "y", "amount": R.of(memory=2)}),
            Event(5, EventKind.RECLAIM, {"stream_id": "s1", "borrower_order": order}),
        ]

    def test_independent_simultaneous_borrowers_follow_input_tie_order(self):
        first = run_event_log(self._independent_events(["x", "y"]), capacity=R.of(memory=4))
        second = run_event_log(self._independent_events(["y", "x"]), capacity=R.of(memory=4))

        plan_a = first.plans[0]
        plan_b = second.plans[0]
        self.assertEqual(plan_a.release_order, ("x", "y"))
        self.assertEqual(plan_b.release_order, ("y", "x"))
        self.assertEqual(plan_a.suspended, ())
        self.assertEqual(plan_b.suspended, ())
        # Ordering changes trace order, not resource balances/accounting.
        self.assertEqual(first.final_state.available, second.final_state.available)
        self.assertEqual(
            first.final_state.outstanding_by_owner("s1"),
            second.final_state.outstanding_by_owner("s1"),
        )

    def test_dependency_order_overrides_simultaneous_input_order(self):
        events = [
            Event(0, EventKind.SUBMIT, {"spec": stream_spec()}),
            Event(0, EventKind.SUBMIT, {"spec": boundary_batch("holder", buffer_memory=0)}),
            Event(0, EventKind.SUBMIT, {"spec": boundary_batch("waiter", buffer_memory=2, continue_memory=None)}),
            Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "holder", "amount": R.of(memory=2)}),
            Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "waiter", "amount": R.of(memory=2)}),
            # Input deliberately asks for blocked waiter first.
            Event(5, EventKind.RECLAIM, {"stream_id": "s1", "borrower_order": ["waiter", "holder"]}),
            Event(8, EventKind.STREAM_FINISH, {"task_id": "s1"}),
        ]
        result = run_event_log(events, capacity=R.of(memory=4))
        plan = result.plans[0]
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.release_order, ("holder", "waiter"))
        self.assertEqual(plan.suspended, ("waiter",))
        self.assertEqual(result.scheduler.state.task("waiter").status, TaskStatus.RUNNING)
        recovery_records = [r for r in result.records if r.kind == "recovery_scan"]
        self.assertTrue(recovery_records)

    def test_cycle_is_canonical_for_different_borrower_input_orders(self):
        def events(order):
            return [
                Event(0, EventKind.SUBMIT, {"spec": stream_spec(memory=4)}),
                Event(0, EventKind.SUBMIT, {"spec": boundary_batch("a", buffer_memory=2, continue_memory=None)}),
                Event(0, EventKind.SUBMIT, {"spec": boundary_batch("b", buffer_memory=2, continue_memory=None)}),
                Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "a", "amount": R.of(memory=2)}),
                Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "b", "amount": R.of(memory=2)}),
                Event(5, EventKind.RECLAIM, {"stream_id": "s1", "borrower_order": order}),
            ]

        r1 = run_event_log(events(["a", "b"]), capacity=R.of(memory=4))
        r2 = run_event_log(events(["b", "a"]), capacity=R.of(memory=4))
        p1, p2 = r1.plans[0], r2.plans[0]
        self.assertFalse(p1.feasible)
        self.assertFalse(p2.feasible)
        assert p1.cycle is not None and p2.cycle is not None
        self.assertEqual(p1.cycle.nodes, ("a", "b", "a"))
        self.assertEqual(p2.cycle.nodes, ("a", "b", "a"))
        # Infeasible planning must not mutate loans/resources.
        self.assertEqual(r1.final_state.outstanding_by_owner("s1"), R.of(memory=4))
        self.assertEqual(r2.final_state.available, R.zero())

    def test_three_simultaneous_borrower_permutations_change_only_tie_order(self):
        import itertools

        def run_with(order):
            events = [
                Event(0, EventKind.SUBMIT, {"spec": stream_spec(memory=6)}),
                *[Event(0, EventKind.SUBMIT, {"spec": boundary_batch(name)}) for name in ("a", "b", "c")],
                *[
                    Event(
                        0,
                        EventKind.LEND,
                        {"owner_id": "s1", "borrower_id": name, "amount": R.of(memory=2)},
                    )
                    for name in ("a", "b", "c")
                ],
                Event(5, EventKind.RECLAIM, {"stream_id": "s1", "borrower_order": order}),
            ]
            return run_event_log(events, capacity=R.of(memory=6))

        finals = set()
        for order in itertools.permutations(("a", "b", "c")):
            with self.subTest(order=order):
                result = run_with(list(order))
                self.assertTrue(result.plans[0].feasible)
                self.assertEqual(result.plans[0].release_order, order)
                self.assertEqual(result.plans[0].suspended, ())
                finals.add(result.final_state.available)
        self.assertEqual(finals, {R.zero()})

    def test_combined_same_time_reclaims_find_cross_stream_buffer_wave(self):
        def run(stream_order):
            events = [
                Event(0, EventKind.SUBMIT, {"spec": stream_spec("s1", 2)}),
                Event(0, EventKind.SUBMIT, {"spec": stream_spec("s2", 2)}),
                Event(0, EventKind.SUBMIT, {"spec": boundary_batch("a", buffer_memory=2, continue_memory=None)}),
                Event(0, EventKind.SUBMIT, {"spec": boundary_batch("h", buffer_memory=0, continue_memory=0)}),
                Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "a", "amount": R.of(memory=2)}),
                Event(0, EventKind.LEND, {"owner_id": "s2", "borrower_id": "h", "amount": R.of(memory=2)}),
                Event(5, EventKind.RECLAIM, {"stream_ids": stream_order}),
            ]
            return run_event_log(events, capacity=R.of(memory=4))

        r1 = run(["s1", "s2"])
        r2 = run(["s2", "s1"])
        self.assertEqual(len(r1.plans), 1)
        self.assertTrue(r1.plans[0].feasible, r1.plans[0].reason)
        self.assertTrue(r2.plans[0].feasible, r2.plans[0].reason)
        self.assertEqual(r1.plans[0].release_order, ("h", "a"))
        self.assertEqual(r2.plans[0].release_order, ("h", "a"))
        self.assertEqual(r1.final_state.available, r2.final_state.available)
        self.assertEqual(r1.final_state.outstanding_by_owner("s1"), R.zero())
        self.assertEqual(r1.final_state.outstanding_by_owner("s2"), R.zero())

    def test_simultaneous_reclaim_events_keep_stable_input_order(self):
        def log(reclaim_order):
            base = [
                {"time": 0, "kind": "submit", "payload": {"spec": stream_spec("s1", 2)}},
                {"time": 0, "kind": "submit", "payload": {"spec": stream_spec("s2", 2)}},
                {"time": 0, "kind": "submit", "payload": {"spec": boundary_batch("a")}},
                {"time": 0, "kind": "submit", "payload": {"spec": boundary_batch("b")}},
                {"time": 0, "kind": "lend", "payload": {"owner_id": "s1", "borrower_id": "a", "amount": R.of(memory=1)}},
                {"time": 0, "kind": "lend", "payload": {"owner_id": "s2", "borrower_id": "b", "amount": R.of(memory=1)}},
            ]
            reclaim_events = [
                {"time": 5, "kind": "reclaim", "payload": {"stream_id": sid}}
                for sid in reclaim_order
            ]
            return run_event_log(base + reclaim_events, capacity=R.of(memory=2))

        r1 = log(["s1", "s2"])
        r2 = log(["s2", "s1"])
        order1 = [record.task_id for record in r1.records if record.kind == "reclaim"]
        order2 = [record.task_id for record in r2.records if record.kind == "reclaim"]
        self.assertEqual(order1, ["s1", "s2"])
        self.assertEqual(order2, ["s2", "s1"])
        self.assertEqual(r1.final_state.available, r2.final_state.available)
        self.assertEqual(len(r1.plans), 2)
        self.assertTrue(all(plan.feasible for plan in r1.plans))


if __name__ == "__main__":
    unittest.main()
