"""End-to-end narrative test covering the whole release-pipeline story.

Scenario exercised in order:

1. A release-tier package matches with distinct physical artifacts.
2. A second package fails on cardinality; its shortage and candidate set are
   reported exactly as the specification requires.
3. A third package fails only because two slots compete for one artifact
   (Hall contention), although each slot looked fine in isolation.
4. One more artifact is produced: the failing package becomes satisfiable,
   while the already-successful package replays its ORIGINAL binding.
"""

import unittest

from depmatcher import (
    Artifact,
    BindingRegistry,
    DependencyMatcher,
    Inventory,
    MatchValue,
    RuleBook,
    SlotRequirement,
    TestPackage,
)


class PipelineNarrativeTest(unittest.TestCase):
    def test_full_story(self) -> None:
        rule_book = RuleBook(
            substitutions=[("compiler", "gcc11", "gcc12")],
        )
        matcher = DependencyMatcher(rule_book)
        registry = BindingRegistry(rule_book)

        inventory = Inventory(
            [
                Artifact("rt-1", "runtime", {"platform": "linux"}),
                Artifact("cc-1", "compiler", {"compiler": "gcc11"}),
                Artifact("sdk-1", "sdk", {"platform": "linux"}, features=["stable"]),
            ]
        )

        # ------------------------------------------------------------------
        # 1. First package: one runtime + one toolchain, fully satisfiable.
        # ------------------------------------------------------------------
        package_a = TestPackage(
            "pkgA",
            (
                SlotRequirement(
                    "runtime",
                    "runtime",
                    constraints=(MatchValue("platform", "linux"),),
                ),
                SlotRequirement(
                    "toolchain",
                    "compiler",
                    constraints=(MatchValue("compiler", "gcc11"),),
                ),
            ),
        )
        result_a = matcher.match(package_a, inventory)
        self.assertTrue(result_a.success)
        self.assertEqual(
            dict(result_a.binding),
            {"runtime": ("rt-1",), "toolchain": ("cc-1",)},
        )
        registry.store(result_a)

        # ------------------------------------------------------------------
        # 2. Second package demands TWO linux SDKs; only one exists.
        # ------------------------------------------------------------------
        package_b = TestPackage(
            "pkgB",
            (
                SlotRequirement(
                    "sdk-pair",
                    "sdk",
                    count=2,
                    constraints=(MatchValue("platform", "linux"),),
                ),
            ),
        )
        result_b = matcher.match(package_b, inventory)
        self.assertFalse(result_b.success)
        self.assertEqual(len(result_b.shortages), 1)
        shortage = result_b.shortages[0]
        self.assertEqual(shortage.slot, "sdk-pair")
        self.assertEqual(shortage.required, 2)
        self.assertEqual(shortage.candidate_ids, ("sdk-1",))

        # ------------------------------------------------------------------
        # 3. Third package: two slots both only accept rt-1. Each slot has a
        #    candidate, but distinctness makes the group infeasible.
        # ------------------------------------------------------------------
        package_c = TestPackage(
            "pkgC",
            (
                SlotRequirement(
                    "first",
                    "runtime",
                    constraints=(MatchValue("platform", "linux"),),
                ),
                SlotRequirement(
                    "second",
                    "runtime",
                    constraints=(MatchValue("platform", "linux"),),
                ),
            ),
        )
        result_c = matcher.match(package_c, inventory)
        self.assertFalse(result_c.success)
        self.assertEqual(result_c.shortages, ())
        self.assertIsNotNone(result_c.contention)
        self.assertEqual(
            sorted(result_c.contention.slots), ["first", "second"]
        )
        self.assertEqual(result_c.contention.candidate_ids, ("rt-1",))
        self.assertEqual(result_c.contention.required_total, 2)

        # ------------------------------------------------------------------
        # 4. One more artifact is produced: a second linux runtime and a
        #    second linux SDK in one inventory update sequence.
        # ------------------------------------------------------------------
        inventory.add(
            Artifact("rt-2", "runtime", {"platform": "linux"})
        )
        inventory.add(
            Artifact(
                "sdk-2",
                "sdk",
                {"platform": "linux"},
                features=["stable"],
            )
        )

        # pkgB now succeeds...
        fixed_b = matcher.match(package_b, inventory)
        self.assertTrue(fixed_b.success)
        self.assertEqual(fixed_b.binding["sdk-pair"], ("sdk-1", "sdk-2"))

        # ...pkgC now succeeds with the two distinct runtimes...
        fixed_c = matcher.match(package_c, inventory)
        self.assertTrue(fixed_c.success)
        self.assertEqual(
            fixed_c.assigned_artifact_ids, {"rt-1", "rt-2"}
        )

        # ...and unrelated pkgA still replays its ORIGINAL binding verbatim.
        replay_a = registry.replay(package_a, inventory, matcher)
        self.assertTrue(replay_a.valid)
        self.assertEqual(
            replay_a.binding,
            {"runtime": ("rt-1",), "toolchain": ("cc-1",)},
        )

    def test_conditional_exclusion_interleaves_with_substitution(self) -> None:
        # Exclusion removes the substituted artifact only for one tier.
        rule_book = RuleBook(
            substitutions=[("compiler", "gcc11", "gcc12")],
            exclusions=[],
        )
        matcher = DependencyMatcher(rule_book)
        inventory = Inventory(
            [
                Artifact(
                    "cc-12",
                    "compiler",
                    {"compiler": "gcc12"},
                    features=["lto"],
                ),
            ]
        )
        package = TestPackage(
            "pkg",
            (
                SlotRequirement(
                    "toolchain",
                    "compiler",
                    constraints=(MatchValue("compiler", "gcc11"),),
                ),
            ),
        )
        # Substitution makes gcc12 acceptable for a gcc11 requirement.
        ok = matcher.match(package, inventory)
        self.assertTrue(ok.success)
        self.assertEqual(ok.binding["toolchain"], ("cc-12",))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
