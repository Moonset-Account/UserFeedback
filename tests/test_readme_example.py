"""The README example, replayed, so the documented trace stays truthful."""

from __future__ import annotations

import unittest

from cpu_sampler import build_profile

README_TRACE = {
    "images": [
        {
            "module": "libengine",
            "version": "1.0",
            "size": 4096,
            "build_id": "engine-build-1",
            "functions": [
                {
                    "name": "engine_loop",
                    "lo": 0,
                    "hi": 64,
                    "inlines": [
                        {
                            "name": "loop_step",
                            "lo": 16,
                            "hi": 48,
                            "children": [
                                {"name": "loop_body", "lo": 24, "hi": 40}
                            ],
                        }
                    ],
                },
                {"name": "engine_flush", "lo": 64, "hi": 96},
            ],
        },
        {
            "module": "libengine",
            "version": "2.0",
            "size": 4096,
            "build_id": "engine-build-2",
            "functions": [
                {"name": "engine_loop", "lo": 0, "hi": 56},
                {"name": "engine_drain", "lo": 64, "hi": 96},
            ],
        },
    ],
    "events": [
        {"type": "load", "timestamp": 10, "pid": 1,
         "module": "libengine", "version": "1.0",
         "base": 65536, "size": 4096},
        {"type": "sample", "timestamp": 20, "pid": 1,
         "frames": [65552, 65666]},
        {"type": "unload", "timestamp": 30, "pid": 1, "module": "libengine"},
        {"type": "load", "timestamp": 30, "pid": 1,
         "module": "libengine", "version": "2.0",
         "base": 65536, "size": 4096},
        {"type": "sample", "timestamp": 40, "pid": 1,
         "frames": [65552, 65666]},
    ],
}


class ReadmeExampleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.result = build_profile(README_TRACE)

    def test_first_sample_uses_build_one(self) -> None:
        stack = self.result.stacks[0]
        self.assertEqual(stack.frames[0].version, "1.0")
        self.assertEqual(stack.frames[0].name, "engine_loop")
        self.assertEqual(stack.frames[0].role, "call-site")
        self.assertEqual(stack.frames[0].lookup_address, 0x1000F)

    def test_first_sample_leaf_is_a_hole_placeholder(self) -> None:
        leaf = self.result.stacks[0].frames[-1]
        self.assertFalse(leaf.resolved)
        self.assertEqual(leaf.reason, "no-function")
        self.assertEqual(leaf.version, "1.0")

    def test_second_sample_uses_build_two(self) -> None:
        stack = self.result.stacks[1]
        self.assertEqual(stack.frames[0].version, "2.0")
        self.assertEqual(stack.frames[0].name, "engine_loop")
        self.assertEqual(stack.frames[-1].version, "2.0")

    def test_both_samples_share_the_same_raw_leaf_address(self) -> None:
        self.assertEqual(
            self.result.stacks[0].frames[-1].address,
            self.result.stacks[1].frames[-1].address,
        )


if __name__ == "__main__":
    unittest.main()
