"""Tests for BindingRegistry: store, replay, inventory growth and shrinkage."""

import unittest

from depmatcher import (
    Artifact,
    Binding,
    BindingRegistry,
    DependencyMatcher,
    DimensionError,
    Inventory,
    MatchValue,
    SlotRequirement,
    TestPackage,
)

from fixtures import build_inventory, build_packages, build_rule_book


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = build_rule_book()
        self.matcher = DependencyMatcher(self.rules)
        self.registry = BindingRegistry(self.rules)
        self.inventory = build_inventory()
        self.packages = build_packages()

    def test_successful_match_can_be_stored(self) -> None:
        result = self.matcher.match(self.packages["pkg-e2e"], self.inventory)
        binding = self.registry.store(result)
        self.assertIsInstance(binding, Binding)
        self.assertTrue(self.registry.has("pkg-e2e"))
        self.assertEqual(
            self.registry.get("pkg-e2e").assignment,
            {"runtime": ("rt-lin-x64",), "toolchain": ("cc-gcc11",)},
        )

    def test_storing_failed_match_raises(self) -> None:
        result = self.matcher.match(self.packages["pkg-short"], self.inventory)
        self.assertFalse(result.success)
        with self.assertRaises(DimensionError):
            self.registry.store(result)

    def test_replay_reproduces_original_binding_after_artifact_added(self) -> None:
        result = self.matcher.match(self.packages["pkg-e2e"], self.inventory)
        self.registry.store(result)

        # A brand-new linux gcc12 toolchain is produced afterwards.
        self.inventory.add(
            Artifact(
                "cc-gcc12-new",
                "compiler",
                {"platform": "linux", "compiler": "gcc12"},
                features=["lto"],
            )
        )
        replay = self.registry.replay(
            self.packages["pkg-e2e"], self.inventory, self.matcher
        )
        self.assertTrue(replay.valid)
        # The unrelated package keeps its ORIGINAL physical artifacts.
        self.assertEqual(
            replay.binding,
            {"runtime": ("rt-lin-x64",), "toolchain": ("cc-gcc11",)},
        )

    def test_added_artifact_can_fix_previously_failing_package(self) -> None:
        # Initially the release-tier package cannot get two linux SDKs.
        failed = self.matcher.match(self.packages["pkg-short"], self.inventory)
        self.assertFalse(failed.success)

        # Producing a second stable SDK unblocks it.
        self.inventory.add(
            Artifact(
                "sdk-stable-2",
                "sdk",
                {"platform": "linux", "arch": "x86_64"},
                features=["stable"],
            )
        )
        fixed = self.matcher.match(self.packages["pkg-short"], self.inventory)
        self.assertTrue(fixed.success)
        self.assertEqual(
            fixed.binding["sdk-pair"], ("sdk-min", "sdk-stable-2")
        )

        # Meanwhile the earlier package still replays its old binding.
        good = self.matcher.match(self.packages["pkg-e2e"], self.inventory)
        self.registry.store(good)
        replay = self.registry.replay(
            self.packages["pkg-e2e"], self.inventory, self.matcher
        )
        self.assertTrue(replay.valid)
        self.assertEqual(
            replay.binding["toolchain"], ("cc-gcc11",)
        )

    def test_replay_fails_when_artifact_removed(self) -> None:
        result = self.matcher.match(self.packages["pkg-e2e"], self.inventory)
        self.registry.store(result)

        shrunk = Inventory(
            [a for a in self.inventory.all if a.id != "rt-lin-x64"]
        )
        replay = self.registry.replay(
            self.packages["pkg-e2e"], shrunk, self.matcher
        )
        self.assertFalse(replay.valid)
        self.assertTrue(
            any("rt-lin-x64" in reason for reason in replay.reasons)
        )

    def test_replay_without_stored_binding(self) -> None:
        replay = self.registry.replay(
            self.packages["pkg-e2e"], self.inventory, self.matcher
        )
        self.assertFalse(replay.valid)
        self.assertEqual(replay.reasons, ("no stored binding",))

    def test_replay_detects_slot_topology_change(self) -> None:
        result = self.matcher.match(self.packages["pkg-e2e"], self.inventory)
        self.registry.store(result)

        changed = TestPackage(
            "pkg-e2e",
            (
                SlotRequirement(
                    "runtime",
                    "runtime",
                    constraints=(MatchValue("platform", "linux"),),
                ),
            ),
        )
        replay = self.registry.replay(changed, self.inventory, self.matcher)
        self.assertFalse(replay.valid)
        self.assertTrue(
            any("toolchain" in reason for reason in replay.reasons)
        )

    def test_replay_detects_lost_compatibility(self) -> None:
        # Store a valid binding, then drop the bound artifact and replace it
        # with a different id: the stored binding can no longer resolve.
        result = self.matcher.match(self.packages["pkg-e2e"], self.inventory)
        self.registry.store(result)

        swapped = Inventory(
            [a for a in self.inventory.all if a.id != "cc-gcc11"]
            + [
                Artifact(
                    "cc-gcc11b",
                    "compiler",
                    {"platform": "linux", "compiler": "gcc11"},
                    features=["lto"],
                )
            ]
        )
        replay = self.registry.replay(
            self.packages["pkg-e2e"], swapped, self.matcher
        )
        self.assertFalse(replay.valid)
        self.assertTrue(
            any("cc-gcc11" in reason for reason in replay.reasons)
        )

    def test_binding_rejects_reused_artifact(self) -> None:
        with self.assertRaises(DimensionError):
            Binding("bad", {"a": ("x",), "b": ("x",)})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
