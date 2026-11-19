"""Rejection cases: substitutions the checker must block, with exact location."""

from __future__ import annotations

import unittest

from release_check import parse_spec, check_substitution

from support import (
    codes,
    implementation,
    interface,
    method,
    run,
    single_declaration_spec,
    spec_document,
)


class RejectedImplementationTests(unittest.TestCase):
    def test_field_outside_the_visible_range_is_rejected(self) -> None:
        document = single_declaration_spec(
            {"analytics": ["id", "plan"]},
            accepts=["analytics"],
            returns={"analytics": ["id", "plan", "email"]},
        )

        report = run(document)

        self.assertEqual(report.status, "rejected")
        self.assertEqual(
            codes(report),
            [("overexposure", "IProfileSource.generate", "analytics", "Candidate")],
        )
        finding = report.findings[0]
        self.assertEqual(finding.allowed, ("id", "plan"))
        self.assertEqual(finding.actual, ("email", "id", "plan"))

    def test_refusing_an_admitted_identity_is_rejected(self) -> None:
        document = single_declaration_spec(
            {"billing": ["id"], "support": ["id"]},
            accepts=["billing"],
            returns={"billing": ["id"]},
        )

        report = run(document)

        self.assertEqual(report.status, "rejected")
        self.assertEqual(
            codes(report),
            [("rejects-identity", "IProfileSource.generate", "support", "Candidate")],
        )

    def test_omitting_the_method_entirely_is_rejected_for_each_identity(self) -> None:
        document = spec_document(
            ["billing", "analytics"],
            [interface("IProfileSource", methods={"generate": {
                "billing": ["id"],
                "analytics": ["id"],
            }})],
            [
                implementation(
                    "Candidate",
                    implements=["IProfileSource"],
                    methods={},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.status, "rejected")
        self.assertEqual(
            codes(report),
            [
                ("missing-method", "IProfileSource.generate", "analytics", "Candidate"),
                ("missing-method", "IProfileSource.generate", "billing", "Candidate"),
            ],
        )

    def test_per_identity_returns_are_judged_per_identity(self) -> None:
        # billing is fine, analytics leaks exactly one field: only analytics is
        # reported, and it is reported at the entry that grants it.
        document = single_declaration_spec(
            {"billing": ["id", "plan"], "analytics": ["id"]},
            accepts=["billing", "analytics"],
            returns={"billing": ["id", "plan"], "analytics": ["id", "plan"]},
        )

        report = run(document)

        self.assertEqual(
            codes(report),
            [("overexposure", "IProfileSource.generate", "analytics", "Candidate")],
        )

    def test_diamond_conflict_reports_the_constraining_branches(self) -> None:
        # billing is granted id+email+plan on the audited branch and id+plan on
        # the read-only branch.  Both bind through the merge, so email must not
        # escape, and the finding must name the branches that limit it.
        document = spec_document(
            ["billing"],
            [
                interface("IProfileSource", methods={"generate": {
                    "billing": ["id", "email", "plan"],
                }}),
                interface("IReadOnlyProfile", extends=["IProfileSource"], methods={
                    "generate": {"billing": ["id", "plan"]},
                }),
                interface("IAuditedProfile", extends=["IProfileSource"], methods={
                    "generate": {"billing": ["id", "email", "plan"]},
                }),
                interface("IDiamondProfile", extends=["IReadOnlyProfile", "IAuditedProfile"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IDiamondProfile"],
                    methods={"generate": method(
                        ["billing"], ["id", "email", "plan"]
                    )},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.status, "rejected")
        self.assertIn(
            ("overexposure", "IDiamondProfile.generate", "billing", "Candidate"),
            codes(report),
        )
        merged = [
            finding
            for finding in report.findings
            if finding.interface == "IDiamondProfile"
        ]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].allowed, ("id", "plan"))
        self.assertEqual(
            merged[0].origins, ("IAuditedProfile", "IReadOnlyProfile")
        )

    def test_identity_admitted_by_only_one_diamond_branch_is_not_callable(self) -> None:
        # support appears on the audited branch only.  Through the merge the
        # read-only branch does not grant it, so no call exists to break and
        # the implementation is not blamed for accepting it or not.
        document = spec_document(
            ["billing", "support"],
            [
                interface("IProfileSource", methods={"generate": {
                    "billing": ["id"],
                    "support": ["id"],
                }}),
                interface("IReadOnlyProfile", extends=["IProfileSource"], methods={
                    "generate": {"billing": ["id"]},
                }),
                interface("IAuditedProfile", extends=["IProfileSource"], methods={
                    "generate": {"billing": ["id"], "support": ["id"]},
                }),
                interface("IDiamondProfile", extends=["IReadOnlyProfile", "IAuditedProfile"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IDiamondProfile"],
                    methods={"generate": method(
                        ["billing", "support"],
                        {"billing": ["id"], "support": ["secret"]},
                    )},
                )
            ],
        )

        report = run(document)

        # The leaked "secret" is only visible through the merge, where support
        # is not callable, so the merge itself stays clean.
        merged = [
            finding
            for finding in report.findings
            if finding.interface == "IDiamondProfile"
        ]
        self.assertEqual(merged, [])
        # But the audited branch on its own does admit support, and there the
        # leak is real.
        self.assertIn(
            ("overexposure", "IAuditedProfile.generate", "support", "Candidate"),
            codes(report),
        )

    def test_substitution_rejects_a_leaky_replacement(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IProfileSource", methods={"generate": {"billing": ["id"]}})],
            [
                implementation(
                    "Leaky",
                    implements=["IProfileSource"],
                    methods={"generate": method(["billing"], ["id", "email"])},
                )
            ],
        )
        parsed = parse_spec(document)

        report = check_substitution(parsed, "IProfileSource", "Leaky")

        self.assertEqual(report.status, "rejected")
        self.assertEqual(
            codes(report),
            [("overexposure", "IProfileSource.generate", "billing", "Leaky")],
        )

    def test_substitution_rejects_a_replacement_that_refuses_the_caller(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IProfileSource", methods={"generate": {"billing": ["id"]}})],
            [
                implementation(
                    "LockedOut",
                    implements=["IProfileSource"],
                    methods={"generate": method([], [])},
                )
            ],
        )
        parsed = parse_spec(document)

        report = check_substitution(parsed, "IProfileSource", "LockedOut")

        self.assertEqual(
            codes(report),
            [("rejects-identity", "IProfileSource.generate", "billing", "LockedOut")],
        )


if __name__ == "__main__":
    unittest.main()
