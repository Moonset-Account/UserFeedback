"""Tests for DependencyMatcher: full matches, shortages, Hall contention."""

import unittest

from depmatcher import (
    Artifact,
    DependencyMatcher,
    DimensionError,
    Inventory,
    MatchAnyOf,
    MatchValue,
    RuleBook,
    SlotRequirement,
    TestPackage,
)

from fixtures import build_inventory, build_packages, build_rule_book


class SuccessfulMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = build_rule_book()
        self.inventory = build_inventory()
        self.packages = build_packages()
        self.matcher = DependencyMatcher(self.rules)

    def test_full_match_uses_distinct_artifacts_per_slot(self) -> None:
        result = self.matcher.match(self.packages["pkg-e2e"], self.inventory)
        self.assertTrue(result.success)
        self.assertEqual(
            result.binding["runtime"], ("rt-lin-x64",)
        )
        # gcc11 requirement served by the exact gcc11 artifact first.
        self.assertEqual(result.binding["toolchain"], ("cc-gcc11",))
        self.assertEqual(
            result.assigned_artifact_ids, {"rt-lin-x64", "cc-gcc11"}
        )

    def test_substitution_used_when_exact_artifact_missing(self) -> None:
        inventory = Inventory(
            [
                Artifact(
                    "rt-lin-x64",
                    "runtime",
                    {"platform": "linux", "arch": "x86_64"},
                ),
                Artifact(
                    "cc-gcc12",
                    "compiler",
                    {"platform": "linux", "compiler": "gcc12"},
                    features=["lto"],
                ),
            ]
        )
        result = self.matcher.match(self.packages["pkg-e2e"], inventory)
        self.assertTrue(result.success)
        self.assertEqual(result.binding["toolchain"], ("cc-gcc12",))

    def test_package_without_slots_matches_with_empty_binding(self) -> None:
        result = self.matcher.match(self.packages["pkg-empty"], self.inventory)
        self.assertTrue(result.success)
        self.assertEqual(dict(result.binding), {})

    def test_match_is_deterministic_across_calls(self) -> None:
        first = self.matcher.match(self.packages["pkg-e2e"], self.inventory)
        second = self.matcher.match(self.packages["pkg-e2e"], self.inventory)
        self.assertEqual(first.binding, second.binding)


class CardinalityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = DependencyMatcher(build_rule_book())
        self.inventory = build_inventory()
        self.packages = build_packages()

    def test_slot_shortage_reports_candidates(self) -> None:
        result = self.matcher.match(
            self.packages["pkg-short"], self.inventory
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.contention)
        self.assertEqual(len(result.shortages), 1)
        shortage = result.shortages[0]
        self.assertEqual(shortage.slot, "sdk-pair")
        self.assertEqual(shortage.required, 2)
        # Only the stable SDK survives the release-tier exclusion.
        self.assertEqual(shortage.candidate_ids, ("sdk-min",))

    def test_count_two_consumes_two_distinct_artifacts(self) -> None:
        # Same slot demand, but on a canary tier both SDKs are allowed.
        package = TestPackage(
            "pkg-pair",
            (
                SlotRequirement(
                    "sdk-pair",
                    "sdk",
                    count=2,
                    constraints=(MatchValue("platform", "linux"),),
                ),
            ),
            attributes={"tier": "canary"},
        )
        result = self.matcher.match(package, self.inventory)
        self.assertTrue(result.success)
        self.assertEqual(
            result.binding["sdk-pair"], ("sdk-exp", "sdk-min")
        )

    def test_count_above_inventory_fails(self) -> None:
        package = TestPackage(
            "pkg-too-many",
            (SlotRequirement("runtimes", "runtime", count=99),),
        )
        result = self.matcher.match(package, self.inventory)
        self.assertFalse(result.success)
        self.assertEqual(result.shortages[0].required, 99)
        self.assertEqual(len(result.shortages[0].candidates), 3)

    def test_invalid_count_raises(self) -> None:
        with self.assertRaises(DimensionError):
            SlotRequirement("bad", "runtime", count=0)


class DistinctnessAndContentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = DependencyMatcher(build_rule_book())
        self.inventory = build_inventory()
        self.packages = build_packages()

    def test_hall_contention_when_slots_share_one_artifact(self) -> None:
        result = self.matcher.match(
            self.packages["pkg-contention"], self.inventory
        )
        self.assertFalse(result.success)
        self.assertEqual(result.shortages, ())
        self.assertIsNotNone(result.contention)
        contention = result.contention
        self.assertEqual(
            sorted(contention.slots),
            ["primary-runtime", "secondary-runtime"],
        )
        self.assertEqual(contention.required_total, 2)
        self.assertEqual(contention.candidate_ids, ("rt-lin-x64",))

    def test_distinctness_enforced_across_all_slots(self) -> None:
        # Three slots, three positions: the two flexible slots must take
        # different physical runtimes and the fixed slot its own one.
        package = TestPackage(
            "pkg-cross",
            (
                SlotRequirement(
                    "a",
                    "runtime",
                    constraints=(MatchAnyOf("platform", ("linux", "macos")),),
                ),
                SlotRequirement(
                    "b",
                    "runtime",
                    constraints=(MatchAnyOf("platform", ("linux", "macos")),),
                ),
                SlotRequirement(
                    "c",
                    "runtime",
                    constraints=(MatchValue("platform", "windows"),),
                ),
            ),
        )
        result = self.matcher.match(package, self.inventory)
        self.assertTrue(result.success)
        used = result.assigned_artifact_ids
        self.assertEqual(len(used), 3)
        self.assertEqual(used, {"rt-lin-x64", "rt-mac-arm", "rt-win-x64"})

    def test_backtracking_revises_early_choice(self) -> None:
        # Slot a can take linux or macos; slot b only linux. A greedy first
        # pick of linux for a would fail – backtracking must assign macos.
        package = TestPackage(
            "pkg-backtrack",
            (
                SlotRequirement(
                    "flex",
                    "runtime",
                    constraints=(MatchAnyOf("platform", ("linux", "macos")),),
                ),
                SlotRequirement(
                    "fixed",
                    "runtime",
                    constraints=(MatchValue("platform", "linux"),),
                ),
            ),
        )
        result = self.matcher.match(package, self.inventory)
        self.assertTrue(result.success)
        self.assertEqual(result.binding["fixed"], ("rt-lin-x64",))
        self.assertEqual(result.binding["flex"], ("rt-mac-arm",))


class InventoryTests(unittest.TestCase):
    def test_duplicate_id_rejected(self) -> None:
        artifact = Artifact("x", "runtime", {"platform": "linux"})
        with self.assertRaises(DimensionError):
            Inventory([artifact, artifact])

    def test_add_extends_inventory(self) -> None:
        inventory = Inventory()
        self.assertEqual(len(inventory), 0)
        artifact = Artifact("x", "runtime", {"platform": "linux"})
        inventory.add(artifact)
        self.assertEqual(len(inventory), 1)
        self.assertIn("x", inventory)
        self.assertEqual(inventory.get("x"), artifact)

    def test_get_missing_raises(self) -> None:
        with self.assertRaises(DimensionError):
            Inventory().get("ghost")

    def test_missing_dimension_raises(self) -> None:
        matcher = DependencyMatcher(RuleBook())
        artifact = Artifact("x", "runtime", {"arch": "x86_64"})
        package = TestPackage(
            "p",
            (SlotRequirement("s", "runtime", constraints=(MatchValue("platform", "linux"),)),),
        )
        with self.assertRaises(DimensionError):
            matcher.match(package, Inventory([artifact]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
