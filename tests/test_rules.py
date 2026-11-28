"""Unit tests for RuleBook: equality, substitution, features, exclusions."""

import unittest

from depmatcher import (
    Artifact,
    Exclusion,
    MatchAny,
    MatchAnyOf,
    MatchValue,
    RuleBook,
    SlotRequirement,
    TestPackage,
)


class SubstitutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = RuleBook(
            substitutions=[
                ("compiler", "gcc11", "gcc12"),
                ("compiler", "gcc10", "gcc11"),
                ("platform", "linux", "rhel9"),
            ]
        )

    def test_equality_is_always_accepted(self) -> None:
        self.assertEqual(
            self.rules.accepted_values("compiler", "clang16"),
            frozenset({"clang16"}),
        )

    def test_direct_substitution(self) -> None:
        self.assertEqual(
            self.rules.accepted_values("compiler", "gcc11"),
            frozenset({"gcc11", "gcc12"}),
        )

    def test_substitution_is_transitive(self) -> None:
        self.assertEqual(
            self.rules.accepted_values("compiler", "gcc10"),
            frozenset({"gcc10", "gcc11", "gcc12"}),
        )

    def test_substitution_does_not_flow_backwards(self) -> None:
        # A gcc12 requirement must not be silently served by gcc11.
        self.assertEqual(
            self.rules.accepted_values("compiler", "gcc12"),
            frozenset({"gcc12"}),
        )

    def test_platform_substitution(self) -> None:
        self.assertEqual(
            self.rules.accepted_values("platform", "linux"),
            frozenset({"linux", "rhel9"}),
        )


class ConstraintMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = RuleBook(
            substitutions=[("compiler", "gcc11", "gcc12")]
        )
        self.package = TestPackage("pkg", ())

    def _slot(self, *constraints, kind="runtime") -> SlotRequirement:
        return SlotRequirement("s", kind, constraints=constraints)

    def test_match_value_equality(self) -> None:
        artifact = Artifact(
            "a1", "runtime", {"platform": "linux"}, features=["sse4"]
        )
        candidates = self.rules.candidates_for_slot(
            self._slot(MatchValue("platform", "linux")), [artifact], self.package
        )
        self.assertEqual([a.id for a in candidates], ["a1"])

    def test_match_value_substitution(self) -> None:
        artifact = Artifact(
            "a1", "compiler", {"compiler": "gcc12"}
        )
        candidates = self.rules.candidates_for_slot(
            self._slot(MatchValue("compiler", "gcc11"), kind="compiler"),
            [artifact],
            self.package,
        )
        self.assertEqual([a.id for a in candidates], ["a1"])

    def test_match_value_rejects_incompatible_scalar(self) -> None:
        artifact = Artifact("a1", "runtime", {"platform": "windows"})
        candidates = self.rules.candidates_for_slot(
            self._slot(MatchValue("platform", "linux")), [artifact], self.package
        )
        self.assertEqual(candidates, ())

    def test_feature_containment_requires_every_feature(self) -> None:
        rich = Artifact(
            "rich", "runtime", features=["sse4", "neon", "lto"]
        )
        poor = Artifact("poor", "runtime", features=["sse4"])
        slot = self._slot(MatchValue("features", ["sse4", "neon"]))
        self.assertEqual(
            [a.id for a in self.rules.candidates_for_slot(slot, [rich, poor], self.package)],
            ["rich"],
        )

    def test_any_of_accepts_each_branch(self) -> None:
        linux = Artifact("l", "runtime", {"platform": "linux"})
        mac = Artifact("m", "runtime", {"platform": "macos"})
        windows = Artifact("w", "runtime", {"platform": "windows"})
        slot = self._slot(MatchAnyOf("platform", ("linux", "macos")))
        self.assertEqual(
            [a.id for a in self.rules.candidates_for_slot(slot, [linux, mac, windows], self.package)],
            ["l", "m"],
        )

    def test_any_of_uses_substitution_closure(self) -> None:
        gcc12 = Artifact("g12", "compiler", {"compiler": "gcc12"})
        slot = self._slot(
            MatchAnyOf("compiler", ("clang16", "gcc11")), kind="compiler"
        )
        self.assertEqual(
            [a.id for a in self.rules.candidates_for_slot(slot, [gcc12], self.package)],
            ["g12"],
        )

    def test_match_any_accepts_any_present_value(self) -> None:
        artifact = Artifact("a1", "runtime", {"platform": "linux"})
        slot = self._slot(MatchAny("platform"))
        self.assertEqual(
            [a.id for a in self.rules.candidates_for_slot(slot, [artifact], self.package)],
            ["a1"],
        )

    def test_match_any_rejects_missing_dimension(self) -> None:
        artifact = Artifact("a1", "runtime", {"arch": "x86_64"})
        slot = self._slot(MatchAny("platform"))
        self.assertEqual(
            self.rules.candidates_for_slot(slot, [artifact], self.package), ()
        )

    def test_constraints_combine_with_logical_and(self) -> None:
        good = Artifact(
            "good",
            "runtime",
            {"platform": "linux", "arch": "x86_64"},
            features=["sse4"],
        )
        wrong_arch = Artifact(
            "wa", "runtime", {"platform": "linux", "arch": "arm64"}
        )
        slot = self._slot(
            MatchValue("platform", "linux"),
            MatchValue("arch", "x86_64"),
            MatchValue("features", ["sse4"]),
        )
        self.assertEqual(
            [a.id for a in self.rules.candidates_for_slot(slot, [good, wrong_arch], self.package)],
            ["good"],
        )

    def test_kind_filtering(self) -> None:
        runtime = Artifact("rt", "runtime", {"platform": "linux"})
        compiler = Artifact("cc", "compiler", {"platform": "linux"})
        slot = self._slot(MatchValue("platform", "linux"), kind="runtime")
        self.assertEqual(
            [a.id for a in self.rules.candidates_for_slot(slot, [runtime, compiler], self.package)],
            ["rt"],
        )


class ExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.experimental = Artifact(
            "sdk-exp",
            "sdk",
            {"platform": "linux"},
            features=["stable", "experimental"],
        )
        self.stable = Artifact(
            "sdk-min", "sdk", {"platform": "linux"}, features=["stable"]
        )
        self.rules = RuleBook(
            exclusions=[
                Exclusion(
                    "no-exp-on-release",
                    artifact_match={"features": ["experimental"]},
                    package_match={"tier": "release"},
                ),
                Exclusion(
                    "no-lto-on-debug",
                    artifact_match={"features": ["lto"]},
                    package_match={"build": "debug"},
                ),
            ]
        )
        self.slot = SlotRequirement("sdk", "sdk")

    def test_exclusion_applies_only_to_matching_package(self) -> None:
        release = TestPackage("rel", (self.slot,), {"tier": "release"})
        candidates = self.rules.candidates_for_slot(
            self.slot, [self.experimental, self.stable], release
        )
        self.assertEqual([a.id for a in candidates], ["sdk-min"])

    def test_exclusion_does_not_apply_to_other_tier(self) -> None:
        canary = TestPackage("can", (self.slot,), {"tier": "canary"})
        candidates = self.rules.candidates_for_slot(
            self.slot, [self.experimental, self.stable], canary
        )
        self.assertEqual(
            sorted(a.id for a in candidates), ["sdk-exp", "sdk-min"]
        )

    def test_artifact_and_package_conditions_both_required(self) -> None:
        lto = Artifact("cc", "compiler", features=["lto"])
        debug = TestPackage("dbg", (), {"build": "debug"})
        release = TestPackage("rel", (), {"build": "release"})
        self.assertTrue(self.rules.is_excluded(lto, debug))
        self.assertFalse(self.rules.is_excluded(lto, release))

    def test_scalar_artifact_exclusion_pattern(self) -> None:
        rules = RuleBook(
            exclusions=[
                Exclusion(
                    "block-windows",
                    artifact_match={"platform": "windows"},
                )
            ]
        )
        package = TestPackage("p", ())
        windows = Artifact("w", "runtime", {"platform": "windows"})
        linux = Artifact("l", "runtime", {"platform": "linux"})
        self.assertTrue(rules.is_excluded(windows, package))
        self.assertFalse(rules.is_excluded(linux, package))

    def test_same_artifact_excluded_for_debug_only(self) -> None:
        # Interleaving: the lto compiler is blocked on debug builds but
        # remains a valid candidate for a release package.
        rules = RuleBook(
            exclusions=[
                Exclusion(
                    "no-lto-on-debug",
                    artifact_match={"features": ["lto"]},
                    package_match={"build": "debug"},
                )
            ]
        )
        lto = Artifact(
            "cc",
            "compiler",
            {"compiler": "gcc12"},
            features=["lto", "pgo"],
        )
        slot = SlotRequirement("toolchain", "compiler")
        debug = TestPackage("dbg", (slot,), {"build": "debug"})
        release = TestPackage("rel", (slot,), {"build": "release"})
        self.assertEqual(
            rules.candidates_for_slot(slot, [lto], debug), ()
        )
        self.assertEqual(
            [a.id for a in rules.candidates_for_slot(slot, [lto], release)],
            ["cc"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
