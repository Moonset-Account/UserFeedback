"""Symbol-table validation, trace parsing and call-tree aggregation."""

from __future__ import annotations

import unittest

from cpu_sampler import (
    DuplicateImageError,
    FunctionRange,
    InlineFrame,
    InputFormatError,
    ModuleImage,
    SymbolTable,
    SymbolTableError,
    ValidationError,
    aggregate_stacks,
    build_call_tree,
    build_profile,
    parse_trace,
)

from . import fixtures as fx


class SymbolTableValidationTest(unittest.TestCase):
    def test_empty_range_is_rejected(self) -> None:
        with self.assertRaises(SymbolTableError):
            FunctionRange(name="f", lo=0x10, hi=0x10)

    def test_inverted_range_is_rejected(self) -> None:
        with self.assertRaises(SymbolTableError):
            FunctionRange(name="f", lo=0x20, hi=0x10)

    def test_overlapping_siblings_are_rejected(self) -> None:
        with self.assertRaises(SymbolTableError):
            FunctionRange(
                name="f",
                lo=0,
                hi=0x40,
                inlines=(
                    InlineFrame(name="a", lo=0x00, hi=0x20),
                    InlineFrame(name="b", lo=0x10, hi=0x30),
                ),
            )

    def test_children_escaping_the_parent_are_rejected(self) -> None:
        with self.assertRaises(SymbolTableError):
            FunctionRange(
                name="f",
                lo=0x10,
                hi=0x20,
                inlines=(InlineFrame(name="a", lo=0x00, hi=0x18),),
            )

    def test_function_outside_the_image_is_rejected(self) -> None:
        with self.assertRaises(SymbolTableError):
            ModuleImage(
                module="m",
                version="1",
                size=0x100,
                functions=(FunctionRange(name="f", lo=0x80, hi=0x180),),
            )

    def test_adjacent_ranges_are_allowed(self) -> None:
        image = ModuleImage(
            module="m",
            version="1",
            size=0x100,
            functions=(
                FunctionRange(name="a", lo=0x00, hi=0x10),
                FunctionRange(name="b", lo=0x10, hi=0x20),
            ),
        )
        table = SymbolTable([image])
        self.assertEqual(table.resolve("m", "1", 0x0F).chain, ("a",))
        self.assertEqual(table.resolve("m", "1", 0x10).chain, ("b",))

    def test_unsorted_function_input_is_normalised(self) -> None:
        image = ModuleImage(
            module="m",
            version="1",
            size=0x100,
            functions=(
                FunctionRange(name="b", lo=0x20, hi=0x30),
                FunctionRange(name="a", lo=0x00, hi=0x10),
            ),
        )
        self.assertEqual(
            [f.name for f in image.functions], ["a", "b"]
        )

    def test_duplicate_image_key_is_rejected(self) -> None:
        table = SymbolTable([fx.host()])
        with self.assertRaises(DuplicateImageError):
            table.add(fx.host())

    def test_resolve_outside_the_image_returns_none(self) -> None:
        table = fx.symbol_table()
        self.assertIsNone(table.resolve("host", "0.9", -1))
        self.assertIsNone(table.resolve("host", "0.9", 0x1000))


class ParseTraceTest(unittest.TestCase):
    def _trace(self) -> dict:
        return {
            "images": [
                {
                    "module": "host",
                    "version": "0.9",
                    "size": 0x1000,
                    "functions": [
                        {
                            "name": "main",
                            "lo": 0,
                            "hi": 0x100,
                            "inlines": [
                                {"name": "main_helper", "lo": 0x20, "hi": 0x40}
                            ],
                        }
                    ],
                }
            ],
            "events": [
                {"type": "load", "timestamp": 1, "pid": 7,
                 "module": "host", "version": "0.9",
                 "base": 0x70000, "size": 0x1000},
                {"type": "sample", "timestamp": 2, "pid": 7,
                 "frames": [0x70030]},
            ],
        }

    def test_parse_and_replay(self) -> None:
        result = build_profile(self._trace())
        self.assertEqual(result.sample_count, 1)
        self.assertEqual(
            result.stacks[0].names, ("main", "main_helper")
        )

    def test_unknown_field_is_rejected(self) -> None:
        trace = self._trace()
        trace["events"][0]["pid_typo"] = 1
        with self.assertRaises(InputFormatError):
            parse_trace(trace)

    def test_missing_required_field_is_rejected(self) -> None:
        trace = self._trace()
        del trace["events"][0]["base"]
        with self.assertRaises(InputFormatError):
            parse_trace(trace)

    def test_unknown_event_type_is_rejected(self) -> None:
        trace = self._trace()
        trace["events"][1]["type"] = "sleep"
        with self.assertRaises(InputFormatError):
            parse_trace(trace)

    def test_non_integer_frame_is_rejected(self) -> None:
        trace = self._trace()
        trace["events"][1]["frames"] = [0x70030, "0x70040"]
        with self.assertRaises(InputFormatError):
            parse_trace(trace)

    def test_empty_frames_are_rejected(self) -> None:
        trace = self._trace()
        trace["events"][1]["frames"] = []
        with self.assertRaises(ValidationError):
            parse_trace(trace)

    def test_unload_without_version_or_base_is_accepted(self) -> None:
        trace = self._trace()
        trace["events"].append(
            {"type": "unload", "timestamp": 3, "pid": 7, "module": "host"}
        )
        table, events = parse_trace(trace)
        self.assertEqual(len(events), 3)


class CallTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        profiler_result = build_profile(
            {
                "images": [
                    {
                        "module": "host",
                        "version": "0.9",
                        "size": 0x1000,
                        "functions": [
                            {
                                "name": "main",
                                "lo": 0,
                                "hi": 0x100,
                                "inlines": [
                                    {"name": "main_helper", "lo": 0x20, "hi": 0x40}
                                ],
                            },
                            {"name": "run", "lo": 0x100, "hi": 0x200},
                        ],
                    }
                ],
                "events": [
                    {"type": "load", "timestamp": 1, "pid": 1,
                     "module": "host", "version": "0.9",
                     "base": 0x70000, "size": 0x1000},
                    {"type": "sample", "timestamp": 2, "pid": 1,
                     "frames": [0x70150, 0x70030]},
                    {"type": "sample", "timestamp": 3, "pid": 1,
                     "frames": [0x70150]},
                    {"type": "sample", "timestamp": 4, "pid": 1,
                     "frames": [0x70030]},
                ],
            }
        )
        self.stacks = profiler_result.stacks

    def test_inclusive_counts_follow_call_paths(self) -> None:
        tree = build_call_tree(self.stacks)
        self.assertEqual(tree.inclusive, 3)

        # ``run`` is a root child in two samples (s1, s2).
        run = tree.children[("run", "host", "0.9", False)]
        self.assertEqual(run.inclusive, 2)

        # ``main`` directly under the root appears once (s3), while the
        # ``main`` reached through ``run`` is a separate node (s1).  The
        # call tree keeps those two call paths apart.
        root_main = tree.children[("main", "host", "0.9", False)]
        self.assertEqual(root_main.inclusive, 1)
        run_main = run.children[("main", "host", "0.9", False)]
        self.assertEqual(run_main.inclusive, 1)

        root_helper = root_main.children[("main_helper", "host", "0.9", False)]
        run_helper = run_main.children[("main_helper", "host", "0.9", False)]
        self.assertEqual(root_helper.inclusive, 1)
        self.assertEqual(root_helper.self_samples, 1)
        self.assertEqual(run_helper.inclusive, 1)
        self.assertEqual(run_helper.self_samples, 1)

    def test_hotspots_are_sorted_by_self_samples(self) -> None:
        spots = aggregate_stacks(self.stacks)
        # ``main_helper`` is the leaf of two samples and therefore hottest,
        # even though those two samples reached it through different paths.
        self.assertEqual(spots[0].name, "main_helper")
        self.assertEqual(spots[0].self_samples, 2)
        self.assertEqual(spots[1].name, "run")
        self.assertEqual(spots[1].self_samples, 1)

    def test_walk_yields_every_node_once(self) -> None:
        tree = build_call_tree(self.stacks)
        names = [node.name for _, node in tree.walk()]
        self.assertEqual(names[0], "<all>")
        self.assertIn("main", names)
        self.assertIn("main_helper", names)
        self.assertIn("run", names)


class PublicApiTest(unittest.TestCase):
    def test_package_exports_the_documented_names(self) -> None:
        import cpu_sampler

        for name in cpu_sampler.__all__:
            self.assertTrue(hasattr(cpu_sampler, name), name)


if __name__ == "__main__":
    unittest.main()
