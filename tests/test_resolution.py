"""Resolver behaviour: leaf vs. call-site addressing and inline expansion."""

from __future__ import annotations

import unittest

from cpu_sampler import PLACEHOLDER, Profiler

from . import fixtures as fx


class LeafVersusCallSiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profiler = Profiler(fx.symbol_table())
        self.result = self.profiler.run(
            [
                fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.sample(20, 1, [0x70200, 0x10020, 0x10082]),
            ]
        )
        self.stack = self.result.stacks[0]

    def test_leaf_frame_uses_the_instruction_address_unchanged(self) -> None:
        leaf = self.stack.frames[-1]
        self.assertEqual(leaf.role, "leaf")
        self.assertEqual(leaf.address, 0x10082)
        self.assertEqual(leaf.lookup_address, 0x10082)
        self.assertEqual(leaf.name, "engine_idle")

    def test_caller_frames_are_stepped_back_one_address_unit(self) -> None:
        caller = self.stack.frames[0]
        self.assertEqual(caller.address, 0x70200)
        self.assertEqual(caller.lookup_address, 0x701FF)
        self.assertEqual(caller.relative_address, 0x1FF)
        self.assertEqual(caller.name, "run")

    def test_return_address_on_a_function_boundary_looks_up_the_caller(self) -> None:
        # 0x70200 is the first byte of ``run``; as a return address it must
        # be attributed to the call site in ``main`` instead.
        result = self.profiler.run(
            [
                fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
                fx.sample(20, 1, [0x70100, 0x70050]),
            ]
        )
        stack = result.stacks[0]
        self.assertEqual(stack.frames[0].name, "main")
        self.assertEqual(stack.frames[0].relative_address, 0x0FF)
        # The leaf is not adjusted, so 0x70050 stays inside ``main``.
        self.assertEqual(stack.frames[-1].name, "main")
        self.assertEqual(stack.frames[-1].relative_address, 0x050)

    def test_leaf_address_exactly_on_a_boundary_belongs_to_the_next_function(self) -> None:
        result = self.profiler.run(
            [
                fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
                fx.sample(20, 1, [0x70100]),
            ]
        )
        self.assertEqual(result.stacks[0].frames[0].name, "run")


class InlineExpansionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profiler = Profiler(fx.symbol_table())
        self.result = self.profiler.run(
            [
                fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.sample(20, 1, [0x70200, 0x10020, 0x10082]),
            ]
        )
        self.stack = self.result.stacks[0]

    def test_full_root_to_leaf_expansion(self) -> None:
        self.assertEqual(
            self.stack.names, ("run", "engine_loop", "loop_step", "loop_body", "engine_idle")
        )

    def test_each_inline_level_shares_the_raw_frame_index(self) -> None:
        inline = [f for f in self.stack.frames if f.frame_index == 1]
        self.assertEqual([f.name for f in inline], ["engine_loop", "loop_step", "loop_body"])
        self.assertEqual([f.inline_depth for f in inline], [0, 1, 2])
        self.assertTrue(all(f.lookup_address == 0x1001F for f in inline))

    def test_outermost_inline_level_keeps_the_call_site_role(self) -> None:
        inline = [f for f in self.stack.frames if f.frame_index == 1]
        self.assertEqual(inline[0].role, "call-site")
        self.assertEqual(inline[1].role, "inline")
        self.assertEqual(inline[2].role, "inline")

    def test_address_outside_every_inline_range_stops_the_chain(self) -> None:
        # Leaf address 0x10036 => relative 0x36: inside engine_loop
        # [0x00, 0x40) but past loop_step [0x10, 0x30).
        result = self.profiler.run(
            [
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.sample(20, 1, [0x10036]),
            ]
        )
        self.assertEqual(result.stacks[0].names, ("engine_loop",))


class PlaceholderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profiler = Profiler(fx.symbol_table())

    def test_address_in_no_mapping_becomes_a_placeholder(self) -> None:
        result = self.profiler.run(
            [
                fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
                fx.sample(20, 1, [0x70200, 0x999999]),
            ]
        )
        stack = result.stacks[0]
        self.assertEqual(stack.raw_frame_count, 2)
        self.assertTrue(stack.frames[0].resolved)
        placeholder = stack.frames[-1]
        self.assertFalse(placeholder.resolved)
        self.assertEqual(placeholder.name, PLACEHOLDER)
        self.assertEqual(placeholder.reason, "no-mapping")
        self.assertEqual(placeholder.address, 0x999999)

    def test_hole_between_functions_is_a_placeholder_with_module_provenance(self) -> None:
        result = self.profiler.run(
            [
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.sample(20, 1, [0x10070]),
            ]
        )
        placeholder = result.stacks[0].frames[0]
        self.assertFalse(placeholder.resolved)
        self.assertEqual(placeholder.reason, "no-function")
        self.assertEqual(placeholder.module, "libengine")
        self.assertEqual(placeholder.version, "1.0")
        self.assertEqual(placeholder.relative_address, 0x070)

    def test_unload_then_sample_yields_a_placeholder_not_a_stale_symbol(self) -> None:
        result = self.profiler.run(
            [
                fx.load(10, 1, "libengine", "1.0", fx.ENGINE_V1_BASE),
                fx.unload(20, 1, "libengine"),
                fx.sample(30, 1, [0x10082]),
            ]
        )
        self.assertEqual(result.stacks[0].frames[0].reason, "no-mapping")
        self.assertEqual(result.stacks[0].frames[0].module, None)

    def test_placeholders_are_counted(self) -> None:
        result = self.profiler.run(
            [
                fx.load(10, 1, "host", "0.9", fx.HOST_BASE),
                fx.sample(20, 1, [0x70200, 0x999999]),
                fx.sample(21, 1, [0x70150]),
            ]
        )
        self.assertEqual(result.placeholder_count, 1)
        self.assertEqual(result.samples_with_placeholders, 1)


if __name__ == "__main__":
    unittest.main()
