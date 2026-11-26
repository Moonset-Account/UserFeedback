import unittest

from hybrid_scheduler.planner import (
    ActionGroup,
    PlannerConfig,
    ReleaseAction,
    ReturnGrant,
    find_release_order,
    plan_reclamation,
    plan_resume_order,
)
from hybrid_scheduler.resources import ResourceVector as R


def grant(owner: str, memory: int) -> ReturnGrant:
    return ReturnGrant(owner, R.of(memory=memory))


class ReleaseOrderTests(unittest.TestCase):
    def test_buffer_cascade_forces_holder_first_despite_input_order(self):
        holder = ReleaseAction(
            task_id="holder",
            buffer=R.zero(),
            release=R.of(memory=2),
            returns=(grant("s1", 2),),
            event_order=99,
        )
        waiter = ReleaseAction(
            task_id="waiter",
            buffer=R.of(memory=2),
            release=R.of(memory=4),
            suspend=True,
            returns=(grant("s1", 4),),
            event_order=0,
        )
        result = find_release_order(R.zero(), [waiter, holder])
        self.assertTrue(result.feasible)
        self.assertEqual(result.order, ("holder", "waiter"))
        self.assertEqual(result.available_after, R.of(memory=6))

    def test_true_resource_cycle_is_explained_canonically(self):
        actions = [
            ReleaseAction(task_id="a", buffer=R.of(memory=2), release=R.of(memory=3), event_order=0),
            ReleaseAction(task_id="b", buffer=R.of(memory=2), release=R.of(memory=3), event_order=1),
        ]
        for permutation in (actions, list(reversed(actions))):
            with self.subTest(permutation=[a.task_id for a in permutation]):
                result = find_release_order(R.zero(), permutation)
                self.assertFalse(result.feasible)
                self.assertIsNotNone(result.cycle)
                assert result.cycle is not None
                self.assertEqual(result.cycle.nodes, ("a", "b", "a"))
                self.assertIn("资源依赖环", result.cycle.message)
                self.assertTrue(all(edge.dimension == "memory" for edge in result.cycle.edges))
                self.assertEqual(result.cycle.shortfalls["a"], R.of(memory=2))

    def test_unrelated_shortfall_without_holder_is_not_reported_as_cycle(self):
        stuck = ReleaseAction(task_id="stuck", buffer=R.of(memory=5), release=R.of(memory=1))
        result = find_release_order(R.zero(), [stuck])
        self.assertFalse(result.feasible)
        self.assertIsNone(result.cycle)
        self.assertEqual(result.wait_edges, ())

    def test_equal_ties_can_follow_event_order_or_canonical_id(self):
        a = ReleaseAction(task_id="a", event_order=0)
        b = ReleaseAction(task_id="b", event_order=1)
        preserve = find_release_order(R.zero(), [b, a], config=PlannerConfig(preserve_event_order=True))
        canonical = find_release_order(R.zero(), [b, a], config=PlannerConfig(preserve_event_order=False))
        self.assertEqual(preserve.order, ("a", "b"))
        self.assertEqual(canonical.order, ("a", "b"))  # event order and id agree here

        reversed_events = [
            ReleaseAction(task_id="a", event_order=1),
            ReleaseAction(task_id="b", event_order=0),
        ]
        preserve_rev = find_release_order(R.zero(), reversed_events, config=PlannerConfig(True))
        id_only = find_release_order(
            R.zero(), reversed_events, config=PlannerConfig(preserve_event_order=False)
        )
        self.assertEqual(preserve_rev.order, ("b", "a"))
        self.assertEqual(id_only.order, ("a", "b"))

    def test_deadline_has_priority_over_event_order(self):
        late = ReleaseAction(task_id="late", deadline=100.0, event_order=0)
        urgent = ReleaseAction(task_id="urgent", deadline=2.0, event_order=5)
        result = find_release_order(R.zero(), [late, urgent])
        self.assertEqual(result.order, ("urgent", "late"))

    def test_deadline_violation_can_make_plan_hard_infeasible(self):
        slow = ReleaseAction(
            task_id="slow",
            advance_duration=5,
            checkpoint_duration=2,
            deadline=6,
        )
        soft = plan_reclamation(available=R.zero(), groups=[ActionGroup("slow", [slow], True)])
        self.assertTrue(soft.feasible)
        self.assertEqual(len(soft.deadline_violations), 1)
        hard = plan_reclamation(
            available=R.zero(),
            groups=[ActionGroup("slow", [slow], True)],
            config=PlannerConfig(enforce_deadlines=True),
        )
        self.assertFalse(hard.feasible)
        self.assertIn("期限", hard.reason)


class ReclamationSearchTests(unittest.TestCase):
    def test_prefers_continue_option_and_skips_unneeded_optional_holder(self):
        continue_action = ReleaseAction(
            task_id="b",
            action_id="b:continue",
            release=R.of(memory=2),
            returns=(grant("s", 2),),
            suspend=False,
        )
        suspend_action = ReleaseAction(
            task_id="b",
            action_id="b:suspend",
            buffer=R.of(memory=1),
            release=R.of(memory=3),
            returns=(grant("s", 2),),
            suspend=True,
            checkpoint=True,
        )
        bystander = ReleaseAction(task_id="x", release=R.of(memory=9), suspend=True)
        plan = plan_reclamation(
            available=R.zero(),
            groups=[
                ActionGroup("b", [continue_action, suspend_action], required=True),
                ActionGroup("x", [bystander], required=False),
            ],
            claims={"s": R.of(memory=2)},
        )
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.release_order, ("b",))
        self.assertNotIn("x", plan.selected_actions)
        self.assertEqual(plan.suspended, ())

    def test_uses_optional_holder_rather_than_unnecessary_suspension(self):
        # a's preferred continue action needs a 2-unit buffer.  It also has a
        # suspend alternative that can run without buffer but pauses a.  Holder
        # h can release 2 units without disruption, so the optimal plan uses h
        # first and keeps a non-suspended.
        a_continue = ReleaseAction(
            task_id="a",
            action_id="a:continue",
            buffer=R.of(memory=2),
            release=R.of(memory=2),
            returns=(grant("s", 2),),
            suspend=False,
        )
        a_suspend = ReleaseAction(
            task_id="a",
            action_id="a:suspend",
            buffer=R.zero(),
            release=R.of(memory=3),
            returns=(grant("s", 2),),
            suspend=True,
            checkpoint=True,
        )
        h_continue = ReleaseAction(
            task_id="h",
            action_id="h:continue",
            buffer=R.zero(),
            release=R.of(memory=2),
            suspend=False,
        )
        plan = plan_reclamation(
            available=R.zero(),
            groups=[
                ActionGroup("a", [a_continue, a_suspend], required=True),
                ActionGroup("h", [h_continue], required=False),
            ],
            claims={"s": R.of(memory=2)},
        )
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.release_order, ("h", "a"))
        self.assertEqual(plan.suspended, ())
        self.assertFalse(plan.selected_actions["a"].suspend)

    def test_claims_must_be_accounted(self):
        action = ReleaseAction(task_id="a", release=R.of(memory=1), returns=(grant("s", 1),))
        plan = plan_reclamation(
            available=R.zero(),
            groups=[ActionGroup("a", [action], True)],
            claims={"s": R.of(memory=2)},
        )
        self.assertFalse(plan.feasible)
        self.assertIn("合法释放选项", plan.reason)


class ResumeOrderTests(unittest.TestCase):
    def test_edf_order_when_all_fit(self):
        result = plan_resume_order(
            R.of(memory=6),
            [
                _resume("late", 2, 100),
                _resume("urgent", 2, 1),
                _resume("middle", 2, 10),
            ],
        )
        self.assertEqual(result.resumed, ("urgent", "middle", "late"))
        self.assertEqual(result.deferred, ())

    def test_subset_search_resumes_maximum_number_and_defers_large_task(self):
        result = plan_resume_order(
            R.of(memory=4),
            [
                _resume("small-a", 2, 100),
                _resume("small-b", 2, 1),
                _resume("large", 4, 0),
            ],
        )
        self.assertEqual(result.resumed, ("small-b", "small-a"))
        self.assertEqual(result.deferred, ("large",))
        self.assertEqual(result.available_after, R.zero())


def _resume(task_id: str, memory: int, deadline: float):
    from hybrid_scheduler.planner import ResumeCandidate

    return ResumeCandidate(task_id=task_id, needs=R.of(memory=memory), deadline=deadline)


if __name__ == "__main__":
    unittest.main()
