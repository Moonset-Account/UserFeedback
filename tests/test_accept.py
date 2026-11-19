"""Acceptance cases: substitutions the checker must let through."""

from __future__ import annotations

import unittest

from release_check import parse_spec, check, check_substitution

from support import (
    codes,
    implementation,
    interface,
    method,
    run,
    single_declaration_spec,
    spec_document,
)


class AcceptableImplementationTests(unittest.TestCase):
    def test_exact_fields_for_every_admitted_identity_is_accepted(self) -> None:
        document = single_declaration_spec(
            {
                "billing": ["id", "email", "plan"],
                "analytics": ["id", "plan"],
            },
            accepts=["billing", "analytics"],
            returns={
                "billing": ["id", "email", "plan"],
                "analytics": ["id", "plan"],
            },
        )

        report = run(document)

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.findings, [])
        self.assertTrue(report.ok)

    def test_returning_a_subset_stays_inside_the_visible_range(self) -> None:
        # The rule bounds what an identity may *see*, not what it must see, so
        # a narrower response is a legal replacement.
        document = single_declaration_spec(
            {"billing": ["id", "email", "plan"]},
            accepts=["billing"],
            returns={"billing": ["id"]},
        )

        report = run(document)

        self.assertEqual(report.status, "ready")
        self.assertEqual(codes(report), [])

    def test_flat_returns_apply_to_every_accepted_identity(self) -> None:
        document = single_declaration_spec(
            {"billing": ["id", "plan"], "analytics": ["id", "plan"]},
            accepts=["billing", "analytics"],
            returns=["id", "plan"],
        )

        report = run(document)

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.findings, [])

    def test_implementation_accepting_identities_the_entry_never_admits_is_accepted(
        self,
    ) -> None:
        # Accepting extra identities is not observable through this entry: no
        # declaration grants them a call, so there is nothing to violate.
        # ``analytics`` and ``support`` are real callers, just not admitted
        # here, which keeps the specification valid while the entry stays narrow.
        document = spec_document(
            ["billing", "analytics", "support"],
            [interface("IProfileSource", methods={"generate": {"billing": ["id"]}})],
            [
                implementation(
                    "Candidate",
                    implements=["IProfileSource"],
                    methods={"generate": method(
                        ["billing", "analytics", "support"], ["id"]
                    )},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.findings, [])

    def test_no_declared_implementations_is_ready(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IProfileSource", methods={"generate": {"billing": ["id"]}})],
        )

        report = run(document)

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.findings, [])

    def test_diamond_intersection_is_accepted_when_inside_every_branch(self) -> None:
        # A diamond whose branches disagree: billing may see id+plan on the
        # read-only side and id+plan+email on the audited side, so the binding
        # range through the merged interface is the intersection, id+plan.
        document = spec_document(
            ["billing", "analytics"],
            [
                interface("IProfileSource", methods={"generate": {
                    "billing": ["id", "plan", "email"],
                    "analytics": ["id", "plan"],
                }}),
                interface("IReadOnlyProfile", extends=["IProfileSource"], methods={
                    "generate": {"billing": ["id", "plan"], "analytics": ["id"]},
                }),
                interface("IAuditedProfile", extends=["IProfileSource"], methods={
                    "generate": {
                        "billing": ["id", "plan", "email"],
                        "analytics": ["id", "plan"],
                    },
                }),
                interface("IDiamondProfile", extends=["IReadOnlyProfile", "IAuditedProfile"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IDiamondProfile"],
                    methods={"generate": method(
                        ["billing", "analytics"],
                        {"billing": ["id", "plan"], "analytics": ["id"]},
                    )},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.findings, [])

    def test_substitution_check_accepts_a_conforming_replacement(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IProfileSource", methods={"generate": {"billing": ["id"]}})],
            [
                implementation(
                    "Good",
                    implements=["IProfileSource"],
                    methods={"generate": method(["billing"], ["id"])},
                ),
                implementation(
                    "Other",
                    implements=["IProfileSource"],
                    methods={"generate": method(["billing"], ["id"])},
                ),
            ],
        )
        parsed = parse_spec(document)

        report = check_substitution(parsed, "IProfileSource", "Good")

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.findings, [])

    def test_selfcheck_of_full_run_matches_per_implementation_substitution(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IProfileSource", methods={"generate": {"billing": ["id"]}})],
            [
                implementation(
                    "Good",
                    implements=["IProfileSource"],
                    methods={"generate": method(["billing"], ["id"])},
                )
            ],
        )
        parsed = parse_spec(document)

        full = check(parsed)
        narrow = check_substitution(parsed, "IProfileSource", "Good")

        self.assertEqual(full.status, narrow.status)
        self.assertEqual(codes(full), codes(narrow))

    def test_exact_mode_accepts_a_response_covering_the_whole_range(self) -> None:
        document = single_declaration_spec(
            {"billing": ["id", "plan"]},
            accepts=["billing"],
            returns={"billing": ["id", "plan"]},
        )
        parsed = parse_spec(document)

        report = check(parsed, exact=True)

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.findings, [])


if __name__ == "__main__":
    unittest.main()
