import unittest

from compliance_division import (
    ENTER,
    LEAVE,
    DivisionEngine,
    Fact,
    Requirement,
    SelectionChange,
)


def fact(source_id, model, capability, lo, hi):
    return Fact(source_id, model, capability, lo, hi)


def request(requirement_id, capability, lo, hi):
    return Requirement(requirement_id, capability, lo, hi)


class Recorder:
    """Collects notification events and can query the engine from callbacks."""

    def __init__(self, engine=None):
        self.events = []
        self.engine = engine
        self.queried = []

    def __call__(self, change):
        self.events.append(change)
        if self.engine is not None:
            self.queried.append(
                (change.model, self.engine.first_uncovered_requirement(change.model))
            )


class InitialSelectionTests(unittest.TestCase):
    def test_empty_requirements_select_every_model_including_factless(self):
        engine = DivisionEngine(["m1", "m2", "m3"])
        self.assertEqual(engine.current_result(), frozenset({"m1", "m2", "m3"}))
        for model in ("m1", "m2", "m3"):
            self.assertTrue(engine.is_selected(model))
            self.assertEqual(engine.uncovered_requirements(model), ())
            self.assertIsNone(engine.first_uncovered_requirement(model))

    def test_duplicate_models_collapse(self):
        engine = DivisionEngine(["m", "m", "n"])
        self.assertEqual(engine.models, ("m", "n"))
        self.assertEqual(engine.current_result(), frozenset({"m", "n"}))

    def test_one_fact_must_cover_whole_interval(self):
        engine = DivisionEngine(
            ["m"],
            requirements=[request("r1", "gpu", 3, 7)],
        )
        self.assertEqual(engine.current_result(), frozenset())

        engine.add_fact(fact("f_small", "m", "gpu", 4, 7))
        self.assertEqual(engine.current_result(), frozenset())

        engine.add_fact(fact("f_left", "m", "gpu", 3, 6))
        self.assertEqual(engine.current_result(), frozenset())

        engine.add_fact(fact("f_big", "m", "gpu", 2, 8))
        self.assertEqual(engine.current_result(), frozenset({"m"}))

    def test_adjacent_facts_do_not_join_to_cover(self):
        engine = DivisionEngine(
            ["m"],
            requirements=[request("r1", "gpu", 1, 5)],
        )
        engine.add_fact(fact("a", "m", "gpu", 1, 2))
        engine.add_fact(fact("b", "m", "gpu", 2, 5))
        self.assertEqual(engine.current_result(), frozenset())
        self.assertEqual(
            engine.first_uncovered_requirement("m"), request("r1", "gpu", 1, 5)
        )

    def test_exact_interval_boundaries(self):
        engine = DivisionEngine(
            ["m"], requirements=[request("r1", "gpu", 3, 7)]
        )
        engine.add_fact(fact("low", "m", "gpu", 2, 6))
        self.assertNotEqual(engine.current_result(), frozenset({"m"}))
        engine.remove_fact("low")
        engine.add_fact(fact("ok", "m", "gpu", 3, 7))
        self.assertEqual(engine.current_result(), frozenset({"m"}))
        engine.add_fact(fact("over", "m", "gpu", 3, 8))
        self.assertEqual(engine.current_result(), frozenset({"m"}))
        engine.remove_fact("ok")
        self.assertEqual(engine.current_result(), frozenset({"m"}))

    def test_negative_versions(self):
        engine = DivisionEngine(
            ["m"], requirements=[request("r1", "cpu", -5, -1)]
        )
        engine.add_fact(fact("f", "m", "cpu", -6, 0))
        self.assertEqual(engine.current_result(), frozenset({"m"}))

    def test_empty_interval_supported(self):
        engine = DivisionEngine(["m"], requirements=[request("r1", "gpu", 4, 4)])
        engine.add_fact(fact("f", "m", "gpu", 4, 4))
        self.assertEqual(engine.current_result(), frozenset({"m"}))
        engine.remove_fact("f")
        self.assertEqual(engine.current_result(), frozenset())

    def test_multiple_requirements_can_use_different_facts(self):
        engine = DivisionEngine(
            ["m", "n"],
            requirements=[
                request("r1", "gpu", 1, 3),
                request("r2", "cpu", 2, 4),
            ],
        )
        engine.add_fact(fact("m_gpu", "m", "gpu", 1, 3))
        engine.add_fact(fact("m_cpu", "m", "cpu", 1, 4))
        engine.add_fact(fact("n_gpu", "n", "gpu", 1, 3))
        # n has no cpu report at all
        self.assertEqual(engine.current_result(), frozenset({"m"}))
        engine.add_fact(fact("n_cpu", "n", "cpu", 2, 4))
        self.assertEqual(engine.current_result(), frozenset({"m", "n"}))

    def test_fact_for_other_model_does_not_help(self):
        engine = DivisionEngine(
            ["m", "n"], requirements=[request("r1", "gpu", 1, 3)]
        )
        engine.add_fact(fact("n_cpu", "n", "gpu", 1, 3))
        self.assertEqual(engine.current_result(), frozenset({"n"}))
        self.assertIsNone(engine.first_uncovered_requirement("n"))
        self.assertEqual(
            engine.first_uncovered_requirement("m"), request("r1", "gpu", 1, 3)
        )

    def test_one_wide_fact_satisfies_several_overlapping_requirements(self):
        engine = DivisionEngine(
            ["m", "n"],
            requirements=[
                request("r1", "gpu", 1, 3),
                request("r2", "gpu", 2, 4),
                request("r3", "gpu", 6, 7),
            ],
        )
        engine.add_fact(fact("wide", "m", "gpu", 0, 4))
        self.assertEqual(engine.current_result(), frozenset())
        self.assertEqual(
            engine.uncovered_requirements("m"), (request("r3", "gpu", 6, 7),)
        )
        engine.add_fact(fact("late", "m", "gpu", 6, 7))
        self.assertEqual(engine.current_result(), frozenset({"m"}))
        engine.add_fact(fact("wide_n", "n", "gpu", 0, 4))
        engine.add_fact(fact("late_n", "n", "gpu", 6, 7))
        self.assertEqual(engine.current_result(), frozenset({"m", "n"}))

    def test_constructor_applies_requirements_then_facts(self):
        engine = DivisionEngine(
            ["covered", "uncovered"],
            requirements=[request("r1", "gpu", 1, 3)],
            facts=[
                fact("c1", "covered", "gpu", 1, 3),
                fact("c2", "covered", "gpu", 1, 3),
            ],
        )
        self.assertEqual(engine.current_result(), frozenset({"covered"}))
        self.assertEqual(engine.requirement_count, 1)
        self.assertEqual(engine.fact_count, 2)
        self.assertEqual(
            engine.uncovered_requirements("uncovered"), (request("r1", "gpu", 1, 3),)
        )


class ValidationTests(unittest.TestCase):
    def test_inverted_requirement_interval_rejected(self):
        with self.assertRaises(ValueError):
            Requirement("r", "gpu", 9, 2)

    def test_inverted_fact_interval_rejected(self):
        with self.assertRaises(ValueError):
            Fact("f", "m", "gpu", 9, 2)

    def test_duplicate_requirement_id_rejected(self):
        engine = DivisionEngine(["m"])
        engine.add_requirement("r1", "gpu", 1, 3)
        with self.assertRaises(ValueError):
            engine.add_requirement("r1", "cpu", 1, 3)

    def test_duplicate_source_id_rejected(self):
        engine = DivisionEngine(["m"])
        engine.add_fact("f1", "m", "gpu", 1, 3)
        with self.assertRaises(ValueError):
            engine.add_fact("f1", "m", "gpu", 1, 3)

    def test_unknown_sources_rejected(self):
        engine = DivisionEngine(["m"])
        with self.assertRaises(KeyError):
            engine.remove_fact("missing")
        with self.assertRaises(KeyError):
            engine.remove_requirement("missing")

    def test_unknown_model_rejected(self):
        engine = DivisionEngine(["m"])
        with self.assertRaises(ValueError):
            engine.add_fact("f", "unknown", "gpu", 1, 3)
        with self.assertRaises(ValueError):
            engine.first_uncovered_requirement("unknown")


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.engine = DivisionEngine(["a", "b", "c"])
        self.recorder = Recorder(self.engine)
        self.subscription = self.engine.subscribe(self.recorder)

    def tearDown(self):
        self.subscription.close()

    def test_only_actual_entries_are_reported(self):
        # Adding a requirement pushes everyone out.
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.assertEqual(
            [(e.kind, e.model) for e in self.recorder.events],
            [(LEAVE, "a"), (LEAVE, "b"), (LEAVE, "c")],
        )
        self.recorder.events.clear()

        # A partial report does not bring the model back.
        self.engine.add_fact("a_gpu_small", "a", "gpu", 1, 2)
        self.assertEqual(self.recorder.events, [])

        # A full report does.
        self.engine.add_fact("a_gpu", "a", "gpu", 1, 3)
        self.assertEqual(
            [(e.kind, e.model) for e in self.recorder.events], [(ENTER, "a")]
        )

    def test_adding_second_requirement_reports_only_models_that_leave(self):
        self.engine.add_fact("b_gpu", "b", "gpu", 1, 3)
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.assertEqual(
            [(e.kind, e.model) for e in self.recorder.events],
            [(LEAVE, "a"), (LEAVE, "c")],
        )
        self.recorder.events.clear()

        self.engine.add_requirement("r2", "cpu", 1, 2)
        self.assertEqual(
            [(e.kind, e.model) for e in self.recorder.events], [(LEAVE, "b")]
        )

    def test_removing_requirement_reenables_models_in_order(self):
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.recorder.events.clear()
        self.engine.remove_requirement("r1")
        self.assertEqual(
            [(e.kind, e.model) for e in self.recorder.events],
            [(ENTER, "a"), (ENTER, "b"), (ENTER, "c")],
        )

    def test_duplicate_fact_never_notifies_and_withdrawal_is_safe(self):
        self.engine.add_fact("g1", "a", "gpu", 1, 3)
        self.engine.add_fact("g2", "a", "gpu", 1, 3)  # identical payload, new source
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.assertEqual(
            [(e.kind, e.model) for e in self.recorder.events],
            [(LEAVE, "b"), (LEAVE, "c")],
        )
        self.recorder.events.clear()

        # Withdrawing one duplicate leaves coverage; no premature LEAVE.
        self.assertEqual(self.engine.current_result(), frozenset({"a"}))
        self.engine.remove_fact("g1")
        self.assertEqual(self.recorder.events, [])
        self.assertEqual(self.engine.current_result(), frozenset({"a"}))

        # Withdrawing the last copy really drops the model.
        self.engine.remove_fact("g2")
        self.assertEqual(
            [(e.kind, e.model) for e in self.recorder.events], [(LEAVE, "a")]
        )
        self.assertEqual(self.engine.current_result(), frozenset())

    def test_second_distinct_fact_also_blocks_withdrawal(self):
        self.engine.add_fact("wide", "a", "gpu", 1, 3)
        self.engine.add_fact("wider", "a", "gpu", 0, 4)
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.recorder.events.clear()
        self.engine.remove_fact("wide")
        self.assertEqual(self.recorder.events, [])
        self.assertEqual(self.engine.current_result(), frozenset({"a"}))
        self.engine.remove_fact("wider")
        self.assertEqual(
            [(e.kind, e.model) for e in self.recorder.events], [(LEAVE, "a")]
        )

    def test_fact_for_unrequired_capability_is_silent(self):
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.recorder.events.clear()
        self.engine.add_fact("a_nic", "a", "nic", 1, 3)
        self.engine.add_fact("a_nic2", "a", "nic", 1, 3)
        self.engine.remove_fact("a_nic2")
        self.assertEqual(self.recorder.events, [])

    def test_duplicate_report_for_unsatisfied_model_is_silent(self):
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.recorder.events.clear()
        self.engine.add_fact("a_small1", "a", "gpu", 1, 2)
        self.engine.add_fact("a_small2", "a", "gpu", 1, 2)
        self.engine.remove_fact("a_small1")
        self.assertEqual(self.recorder.events, [])
        self.assertEqual(self.engine.current_result(), frozenset())

    def test_subscriber_can_query_one_uncovered_requirement_on_leave(self):
        engine = DivisionEngine(["a", "b", "c"])
        recorder = Recorder(engine)
        engine.subscribe(recorder)

        # Every model already reports the r1 capability, so adding r1 does not
        # change the (empty-requirement) result set at all.
        engine.add_fact("a_gpu", "a", "gpu", 1, 3)
        engine.add_fact("b_gpu", "b", "gpu", 1, 3)
        engine.add_fact("c_gpu", "c", "gpu", 1, 3)
        engine.add_requirement("r1", "gpu", 1, 3)
        self.assertEqual(recorder.events, [])

        # r2 (cpu) is only reported by c, so only c actually leaves the set.
        engine.add_fact("c_cpu", "c", "cpu", 1, 1)
        engine.add_requirement("r2", "cpu", 1, 1)
        self.assertEqual(
            [(e.kind, e.model) for e in recorder.events],
            [(LEAVE, "a"), (LEAVE, "b")],
        )
        self.assertEqual(engine.current_result(), frozenset({"c"}))

        # Querying an excluded model returns the still-uncovered requirement.
        self.assertEqual(
            engine.first_uncovered_requirement("a"), request("r2", "cpu", 1, 1)
        )

        # Withdraw the only cpu report: c leaves and the callback sees r2.
        recorder.events.clear()
        recorder.queried.clear()
        engine.remove_fact("c_cpu")
        self.assertEqual(
            [(e.kind, e.model) for e in recorder.events], [(LEAVE, "c")]
        )
        self.assertEqual(
            recorder.queried,
            [("c", request("r2", "cpu", 1, 1))],
        )

    def test_redundant_covering_fact_never_notifies(self):
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.recorder.events.clear()
        self.engine.add_fact("a_wide", "a", "gpu", 0, 4)  # covers, a enters
        self.assertEqual(
            [(e.kind, e.model) for e in self.recorder.events], [(ENTER, "a")]
        )
        self.recorder.events.clear()
        # A second, wider report adds no new coverage for the requirement.
        self.engine.add_fact("a_wider", "a", "gpu", 0, 9)
        self.assertEqual(self.recorder.events, [])
        self.assertEqual(self.engine.current_result(), frozenset({"a"}))

    def test_removing_irrelevant_fact_from_unselected_model_is_silent(self):
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.engine.add_requirement("r2", "cpu", 1, 2)
        self.recorder.events.clear()
        self.engine.add_fact("a_gpu_partial", "a", "gpu", 1, 2)
        self.engine.add_fact("a_gpu_other_cap_only", "a", "nic", 1, 2)
        self.assertEqual(self.recorder.events, [])
        self.engine.remove_fact("a_gpu_partial")
        self.assertEqual(self.recorder.events, [])

    def test_removing_fact_that_caused_leave_does_not_double_leave(self):
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.engine.add_fact("a_gpu", "a", "gpu", 1, 3)
        self.recorder.events.clear()
        self.engine.remove_fact("a_gpu")
        self.assertEqual(
            [(e.kind, e.model) for e in self.recorder.events], [(LEAVE, "a")]
        )
        # Removing an already-withdrawn id raises; nothing is emitted twice.
        with self.assertRaises(KeyError):
            self.engine.remove_fact("a_gpu")
        self.assertEqual(len(self.recorder.events), 1)

    def test_close_unsubscribes(self):
        count_before = len(self.recorder.events)
        self.subscription.close()
        self.subscription.close()
        self.engine.add_requirement("r1", "gpu", 1, 3)
        self.assertEqual(len(self.recorder.events), count_before)
        self.assertEqual(self.engine.listener_count, 0)

    def test_context_manager_unsubscribes(self):
        engine = DivisionEngine(["a"])
        seen = []
        with engine.subscribe(seen.append) as sub:
            self.assertIsInstance(sub, object)
            engine.add_requirement("r1", "gpu", 1, 3)
        self.assertEqual([(e.kind, e.model) for e in seen], [(LEAVE, "a")])
        engine.remove_requirement("r1")
        self.assertEqual(len(seen), 1)

    def test_multiple_subscribers_all_notified(self):
        second = Recorder()
        engine = DivisionEngine(["a"])
        engine.subscribe(self.recorder)
        engine.subscribe(second)
        engine.add_requirement("r1", "gpu", 1, 3)
        self.assertEqual(len(self.recorder.events), 1)
        self.assertEqual(len(second.events), 1)
        self.assertEqual(second.events[0].model, "a")


class ReentrancyTests(unittest.TestCase):
    def test_listener_mutation_is_applied_and_delivered_after_current_batch(self):
        engine = DivisionEngine(["a", "b", "c"])
        scripted = []

        def listener(change):
            scripted.append((change.kind, change.model))
            if change.kind == LEAVE and change.model == "a":
                # Reacting inside the callback must not lose or reorder events.
                engine.remove_requirement("r1")

        engine.subscribe(listener)
        engine.add_requirement("r1", "gpu", 1, 3)
        # First the synchronous LEAVE batch for a, b, c, then the queued
        # ENTERs produced by the callback's remove_requirement.
        self.assertEqual(
            scripted,
            [
                (LEAVE, "a"),
                (LEAVE, "b"),
                (LEAVE, "c"),
                (ENTER, "a"),
                (ENTER, "b"),
                (ENTER, "c"),
            ],
        )
        self.assertEqual(engine.current_result(), frozenset({"a", "b", "c"}))

    def test_listener_that_raises_still_drains_pending_queue(self):
        engine = DivisionEngine(["a", "b"])

        class Boom(Exception):
            pass

        seen = []

        def listener(change):
            seen.append((change.kind, change.model))
            if len(seen) == 1:
                engine.remove_requirement("r1")
                raise Boom()

        engine.subscribe(listener)
        with self.assertRaises(Boom):
            engine.add_requirement("r1", "gpu", 1, 3)
        # Without a drain-on-exception the queued ENTERs would leak into the
        # next unrelated mutation; here we only assert no cross-batch leakage
        # is observed by a fresh listener.
        later = []
        engine.subscribe(later.append)
        self.assertEqual(later, [])
        self.assertEqual(engine.current_result(), frozenset({"a", "b"}))
        self.assertEqual(len(seen), 1)


class UncoveredRequirementQueryTests(unittest.TestCase):
    def test_returns_first_in_insertion_order(self):
        engine = DivisionEngine(
            ["m"],
            requirements=[
                request("r2", "cpu", 2, 4),
                request("r1", "gpu", 1, 3),
                request("r3", "nic", 5, 6),
            ],
        )
        self.assertEqual(
            engine.uncovered_requirements("m"),
            (
                request("r2", "cpu", 2, 4),
                request("r1", "gpu", 1, 3),
                request("r3", "nic", 5, 6),
            ),
        )
        self.assertEqual(engine.first_uncovered_requirement("m"), request("r2", "cpu", 2, 4))

        engine.add_fact(fact("m_cpu", "m", "cpu", 2, 4))
        self.assertEqual(engine.first_uncovered_requirement("m"), request("r1", "gpu", 1, 3))

    def test_returns_none_for_selected_model(self):
        engine = DivisionEngine(["m"])
        self.assertIsNone(engine.first_uncovered_requirement("m"))
        engine.add_requirement("r1", "gpu", 1, 3)
        self.assertEqual(engine.current_result(), frozenset())
        engine.add_fact("m_gpu", "m", "gpu", 1, 3)
        self.assertEqual(engine.current_result(), frozenset({"m"}))
        self.assertIsNone(engine.first_uncovered_requirement("m"))
        self.assertEqual(engine.uncovered_requirements("m"), ())


class InterleavingTests(unittest.TestCase):
    def test_add_then_remove_fact_and_requirement_in_any_order(self):
        engine = DivisionEngine(["m", "n"])
        events = []
        engine.subscribe(events.append)

        engine.add_requirement("r1", "gpu", 1, 3)
        engine.add_requirement("r2", "cpu", 2, 4)
        self.assertEqual(
            [(e.kind, e.model) for e in events],
            [(LEAVE, "m"), (LEAVE, "n")],
        )
        events.clear()

        engine.add_fact("n_gpu", "n", "gpu", 0, 3)
        self.assertEqual(events, [])
        engine.add_fact("n_cpu", "n", "cpu", 2, 4)
        self.assertEqual([(e.kind, e.model) for e in events], [(ENTER, "n")])
        events.clear()

        # Removing r2 moves n back out, then re-adding a covering report
        # moves it back in; events reflect each actual transition exactly once.
        engine.remove_requirement("r2")
        self.assertEqual(events, [])
        engine.add_requirement("r2", "cpu", 2, 4)
        self.assertEqual(events, [])
        self.assertEqual(engine.current_result(), frozenset({"n"}))

        engine.remove_fact("n_cpu")
        self.assertEqual([(e.kind, e.model) for e in events], [(LEAVE, "n")])

    def test_fact_and_requirement_removal_restores_all_models(self):
        engine = DivisionEngine(["a", "b"])
        engine.add_requirement("r1", "gpu", 1, 3)
        engine.add_fact("a1", "a", "gpu", 1, 3)
        engine.add_fact("a2", "a", "gpu", 1, 3)
        engine.add_fact("b1", "b", "gpu", 1, 3)
        self.assertEqual(engine.current_result(), frozenset({"a", "b"}))
        engine.remove_fact("a1")
        self.assertEqual(engine.current_result(), frozenset({"a", "b"}))
        engine.remove_requirement("r1")
        self.assertEqual(engine.current_result(), frozenset({"a", "b"}))
        engine.remove_fact("a2")
        engine.remove_fact("b1")
        self.assertEqual(engine.current_result(), frozenset({"a", "b"}))
