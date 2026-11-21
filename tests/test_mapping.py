"""Mapping timeline: ordering, address reuse across builds, recursion."""

from __future__ import annotations

import unittest

from cpu_sampler import MappingConflictError, Profiler, UnloadWithoutMappingError

from . import fixtures as fx


class SameTimestampOrderingTest(unittest.TestCase):
    """At one instant: unload, then load, then sample."""

    def setUp(self) -> None:
        self.profiler = Profiler(fx.symbol_table())

    def test_load_then_unload_at_one_instant_is_replayed_unload_first(self) -> None:
        # Deliberately given load-before-unload, which is the wrong order.
        result = self.profiler.run(
            [
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.load(20, 1, "libengine", "2.0", fx.ENGINE_V2_BASE),
                fx.unload(20, 1, "libengine", version="1.0"),
                fx.sample(20, 1, [0x10082]),
            ]
        )
        self.assertEqual([m.version for m in result.live_mappings], ["2.0"])
        self.assertEqual(result.stacks[0].frames[0].version, "2.0")

    def test_sample_at_one_instant_sees_the_post_swap_mapping(self) -> None:
        result = self.profiler.run(
            [
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.sample(20, 1, [0x10082]),
                fx.unload(20, 1, "libengine"),
                fx.load(20, 1, "libengine", "2.0", fx.ENGINE_V2_BASE),
            ]
        )
        self.assertEqual(result.stacks[0].frames[0].version, "2.0")

    def test_same_timestamp_and_phase_keeps_input_order(self) -> None:
        result = self.profiler.run(
            [
                fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
                fx.sample(20, 1, [0x70150]),
                fx.sample(20, 1, [0x70050]),
            ]
        )
        self.assertEqual(
            [stack.frames[0].address for stack in result.stacks],
            [0x70150, 0x70050],
        )


class OutOfOrderInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profiler = Profiler(fx.symbol_table())

    def test_shuffled_events_produce_identical_results(self) -> None:
        ordered = [
            fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
            fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
            fx.sample(20, 1, [0x70200, 0x10020, 0x10082]),
            fx.unload(30, 1, "libengine"),
            fx.load(30, 1, "libengine", "2.0", fx.ENGINE_V2_BASE),
            fx.sample(40, 1, [0x70200, 0x10020, 0x10082]),
        ]
        shuffled = [ordered[i] for i in (3, 0, 5, 2, 4, 1)]
        baseline = self.profiler.run(ordered)
        replayed = self.profiler.run(shuffled)

        self.assertEqual(
            [stack.names for stack in baseline.stacks],
            [stack.names for stack in replayed.stacks],
        )
        self.assertEqual(
            [(s.timestamp, s.frames[-1].version) for s in baseline.stacks],
            [(s.timestamp, s.frames[-1].version) for s in replayed.stacks],
        )
        self.assertEqual(
            [s.frames[-1].version for s in baseline.stacks], ["1.0", "2.0"]
        )


class AddressReuseAcrossBuildsTest(unittest.TestCase):
    """Two samples at the same absolute address, two module versions."""

    def setUp(self) -> None:
        self.profiler = Profiler(fx.symbol_table())
        self.result = self.profiler.run(
            [
                fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.sample(20, 1, [0x70200, 0x10041]),
                fx.unload(30, 1, "libengine"),
                fx.load(30, 1, "libengine", "2.0", fx.ENGINE_V2_BASE),
                fx.sample(40, 1, [0x70200, 0x10041]),
            ]
        )

    def test_both_samples_come_from_the_same_absolute_address(self) -> None:
        before, after = self.result.stacks
        self.assertEqual(before.frames[-1].address, 0x10041)
        self.assertEqual(after.frames[-1].address, 0x10041)

    def test_address_resolves_against_its_own_build(self) -> None:
        before, after = self.result.stacks
        self.assertEqual(before.frames[-1].name, "engine_flush")
        self.assertEqual(before.frames[-1].version, "1.0")
        self.assertEqual(before.frames[-1].build_id, "engine-build-1")

        self.assertEqual(after.frames[-1].name, "engine_drain")
        self.assertEqual(after.frames[-1].version, "2.0")
        self.assertEqual(after.frames[-1].build_id, "engine-build-2")

    def test_generations_distinguish_the_two_mappings(self) -> None:
        before, after = self.result.stacks
        self.assertEqual(before.frames[-1].base, after.frames[-1].base)
        self.assertNotEqual(
            before.frames[-1].generation, after.frames[-1].generation
        )
        self.assertLess(
            before.frames[-1].generation, after.frames[-1].generation
        )

    def test_sample_that_lands_in_a_gap_still_records_the_build_it_saw(self) -> None:
        # Relative 0x70 is a hole in neither image, but 0x90 is a hole in the
        # 1.0 symbols and inside ``engine_alloc`` in 2.0.
        result = self.profiler.run(
            [
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.sample(20, 1, [0x10090]),
                fx.unload(30, 1, "libengine"),
                fx.load(30, 1, "libengine", "2.0", fx.ENGINE_V2_BASE),
                fx.sample(40, 1, [0x10090]),
            ]
        )
        before, after = result.stacks
        self.assertFalse(before.frames[-1].resolved)
        self.assertEqual(before.frames[-1].version, "1.0")
        self.assertEqual(after.frames[-1].name, "engine_alloc")


class RecursionTest(unittest.TestCase):
    def test_recursive_frames_are_both_kept(self) -> None:
        profiler = Profiler(fx.symbol_table())
        # ``main -> run -> run -> main-ish leaf`` with the same address twice.
        result = profiler.run(
            [
                fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
                fx.sample(20, 1, [0x70150, 0x70150, 0x70150, 0x70050]),
            ]
        )
        stack = result.stacks[0]
        self.assertEqual([f.name for f in stack.frames], ["run", "run", "run", "main"])
        self.assertEqual([f.frame_index for f in stack.frames], [0, 1, 2, 3])
        self.assertEqual(stack.raw_frame_count, 4)

    def test_recursion_survives_inline_expansion(self) -> None:
        profiler = Profiler(fx.symbol_table())
        result = profiler.run(
            [
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.sample(20, 1, [0x10020, 0x10020]),
            ]
        )
        # Each raw frame expands to the same three-level chain.
        names = [f.name for f in result.stacks[0].frames]
        self.assertEqual(
            names,
            [
                "engine_loop", "loop_step", "loop_body",
                "engine_loop", "loop_step", "loop_body",
            ],
        )


class MappingLifecycleErrorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profiler = Profiler(fx.symbol_table())

    def test_unloading_a_module_that_is_not_loaded_is_an_error(self) -> None:
        with self.assertRaises(UnloadWithoutMappingError):
            self.profiler.run([fx.unload(5, 1, "libengine")])

    def test_overlapping_load_in_one_process_is_an_error(self) -> None:
        with self.assertRaises(MappingConflictError):
            self.profiler.run(
                [
                    fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
                    # [0x70F00, 0x71700) overlaps the host mapping above.
                    fx.load(10, 1, "libapi", "3.1", 0x70F00, size=0x800),
                ]
            )

    def test_same_base_in_a_different_process_is_fine(self) -> None:
        result = self.profiler.run(
            [
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.load(10, 2, "libengine", "2.0", fx.ENGINE_V2_BASE),
                fx.sample(20, 1, [0x10041]),
                fx.sample(20, 2, [0x10041]),
            ]
        )
        first, second = result.stacks
        self.assertEqual(first.frames[0].name, "engine_flush")
        self.assertEqual(second.frames[0].name, "engine_drain")


class PerPidAttributionTest(unittest.TestCase):
    def test_each_pid_resolves_against_its_own_mappings(self) -> None:
        profiler = Profiler(fx.symbol_table())
        result = profiler.run(
            [
                fx.load(10, 1, "libapi", "3.1", fx.API_BASE, size=0x800),
                fx.load(10, 2, "host", "0.9", fx.HOST_BASE),
                fx.sample(20, 1, [0x40120]),
                fx.sample(20, 2, [0x70120]),
            ]
        )
        one, two = result.stacks
        self.assertEqual(one.frames[0].name, "api_dispatch")
        self.assertEqual(two.frames[0].name, "run")
        self.assertEqual(one.pid, 1)
        self.assertEqual(two.pid, 2)


if __name__ == "__main__":
    unittest.main()
