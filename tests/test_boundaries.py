"""Boundary and interleaving cases for contract resolution.

These cover the situations that decide whether a finding is produced at all:
overrides, branching without a merge, partial diamonds, undecided entries and
empty declarations.
"""

from __future__ import annotations

import unittest

from release_check import check, parse_spec

from support import (
    codes,
    implementation,
    interface,
    method,
    run,
    spec_document,
)


class OverrideTests(unittest.TestCase):
    def test_own_declaration_replaces_the_inherited_contract(self) -> None:
        # The parent lets billing see email; the child narrows it away.  The
        # child's declaration must win, so a child entry returning id only is
        # clean while the parent entry still reports the leak.
        document = spec_document(
            ["billing"],
            [
                interface("IProfileSource", methods={"generate": {
                    "billing": ["id", "email"],
                }}),
                interface("INarrowProfile", extends=["IProfileSource"], methods={
                    "generate": {"billing": ["id"]},
                }),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["INarrowProfile"],
                    methods={"generate": method(["billing"], ["id"])},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.findings, [])

    def test_override_is_not_intersected_with_the_parent(self) -> None:
        # The child declares a *wider* range than its parent.  Override means
        # replace, so the child entry admits the extra field and only the
        # parent entry can complain.
        document = spec_document(
            ["billing"],
            [
                interface("IProfileSource", methods={"generate": {
                    "billing": ["id"],
                }}),
                interface("IWideProfile", extends=["IProfileSource"], methods={
                    "generate": {"billing": ["id", "email"]},
                }),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IWideProfile"],
                    methods={"generate": method(["billing"], ["id", "email"])},
                )
            ],
        )

        report = run(document)

        self.assertEqual(
            codes(report),
            [("overexposure", "IProfileSource.generate", "billing", "Candidate")],
        )
        self.assertEqual(report.findings[0].allowed, ("id",))

    def test_child_can_widen_the_admitted_identity_set_for_its_own_entry(self) -> None:
        document = spec_document(
            ["billing", "support"],
            [
                interface("IProfileSource", methods={"generate": {
                    "billing": ["id"],
                }}),
                interface("IInternalProfile", extends=["IProfileSource"], methods={
                    "generate": {"billing": ["id"], "support": ["id"]},
                }),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IInternalProfile"],
                    methods={"generate": method(["billing"], ["id"])},
                )
            ],
        )

        report = run(document)

        self.assertEqual(
            codes(report),
            [("rejects-identity", "IInternalProfile.generate", "support", "Candidate")],
        )


class MultipleBranchTests(unittest.TestCase):
    def test_two_parents_without_a_shared_ancestor_intersect(self) -> None:
        document = spec_document(
            ["billing"],
            [
                interface("ILeft", methods={"generate": {"billing": ["id", "email"]}}),
                interface("IRight", methods={"generate": {"billing": ["id", "plan"]}}),
                interface("IBoth", extends=["ILeft", "IRight"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IBoth"],
                    methods={"generate": method(["billing"], ["id"])},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.findings, [])

    def test_conflicting_siblings_bind_together_on_the_merge(self) -> None:
        document = spec_document(
            ["billing"],
            [
                interface("ILeft", methods={"generate": {"billing": ["id", "email"]}}),
                interface("IRight", methods={"generate": {"billing": ["id", "plan"]}}),
                interface("IBoth", extends=["ILeft", "IRight"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IBoth"],
                    methods={"generate": method(["billing"], ["id", "email"])},
                )
            ],
        )

        report = run(document)

        merged = [f for f in report.findings if f.interface == "IBoth"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].allowed, ("id",))
        self.assertEqual(merged[0].origins, ("ILeft", "IRight"))

    def test_branches_that_admit_disjoint_identities_leave_the_merge_empty(self) -> None:
        # No identity is admitted by both branches, so nothing is callable at
        # the merge and no per-identity finding can originate there.
        document = spec_document(
            ["billing", "support"],
            [
                interface("ILeft", methods={"generate": {"billing": ["id"]}}),
                interface("IRight", methods={"generate": {"support": ["id"]}}),
                interface("IBoth", extends=["ILeft", "IRight"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IBoth"],
                    methods={"generate": method(
                        ["billing", "support"],
                        {"billing": ["id"], "support": ["id", "email"]},
                    )},
                )
            ],
        )

        report = run(document)

        merged = [f for f in report.findings if f.interface == "IBoth"]
        self.assertEqual(merged, [])

    def test_deep_chain_propagates_through_the_diamond(self) -> None:
        # A four-level graph: a narrowing three levels up must still bind at
        # every entry above it, including the merge that recombines both
        # branches.  IBase is wide enough for the candidate, so it stays clean;
        # IRight and ITop must both report.
        document = spec_document(
            ["billing"],
            [
                interface("IBase", methods={"generate": {"billing": ["id", "email"]}}),
                interface("ILeft", extends=["IBase"], methods={}),
                interface("IRight", extends=["IBase"], methods={
                    "generate": {"billing": ["id"]},
                }),
                interface("ITop", extends=["ILeft", "IRight"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["ITop"],
                    methods={"generate": method(["billing"], ["id", "email"])},
                )
            ],
        )

        report = run(document)

        by_entry = {f.entry: f for f in report.findings}
        self.assertNotIn("IBase.generate", by_entry)
        self.assertNotIn("ILeft.generate", by_entry)
        self.assertEqual(by_entry["IRight.generate"].allowed, ("id",))
        self.assertEqual(by_entry["ITop.generate"].allowed, ("id",))

        # The resolution itself is what carries the constraint upward.
        spec = parse_spec(document)
        self.assertEqual(spec.resolve_method("IBase", "generate").resolved["billing"], ("email", "id"))
        self.assertEqual(spec.resolve_method("ILeft", "generate").resolved["billing"], ("email", "id"))
        self.assertEqual(spec.resolve_method("ITop", "generate").resolved["billing"], ("id",))


class PendingTests(unittest.TestCase):
    def test_public_parent_without_a_declaration_leaves_the_entry_pending(self) -> None:
        document = spec_document(
            ["billing"],
            [
                interface("IProfileSource", methods={"generate": {"billing": ["id"]}}),
                interface("IProfileReader", exposes=["generate"]),
                interface("IBoth", extends=["IProfileSource", "IProfileReader"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IBoth"],
                    methods={"generate": method(["billing"], ["id"])},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.status, "pending")
        self.assertFalse(report.ok)
        pending = {(entry.entry, entry.missing) for entry in report.pending}
        self.assertIn(("IProfileReader.generate", ("IProfileReader",)), pending)
        self.assertIn(("IBoth.generate", ("IProfileReader",)), pending)

    def test_pending_entry_is_not_also_judged(self) -> None:
        # Even an obviously leaky implementation must not be reported as an
        # error through an entry whose contract is unknown: the entry is
        # undecided, not failed.
        document = spec_document(
            ["billing"],
            [
                interface("IProfileReader", exposes=["generate"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IProfileReader"],
                    methods={"generate": method(["billing"], ["id", "secret"])},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.findings, [])
        self.assertEqual(report.status, "pending")

    def test_missing_declaration_on_a_private_interface_does_not_block(self) -> None:
        # ``public: false`` means no external caller can reach it, so there is
        # no contract to leave undecided.
        document = spec_document(
            ["billing"],
            [
                interface(
                    "IInternal-ish",
                    public=False,
                    exposes=["generate"],
                ),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IInternal-ish"],
                    methods={"generate": method(["billing"], ["id"])},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.pending, [])

    def test_a_concrete_sibling_declaration_resolves_the_other_branch(self) -> None:
        # One branch declares, the other is silent but *not* public, so the
        # declaring branch decides the merge.
        document = spec_document(
            ["billing"],
            [
                interface("IDeclared", methods={"generate": {"billing": ["id"]}}),
                interface("ISilent", public=False, exposes=["generate"]),
                interface("IMerged", extends=["IDeclared", "ISilent"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IMerged"],
                    methods={"generate": method(["billing"], ["id", "email"])},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.status, "rejected")
        self.assertIn(
            ("overexposure", "IMerged.generate", "billing", "Candidate"),
            codes(report),
        )


class EmptyAndEdgeDeclarationTests(unittest.TestCase):
    def test_empty_visibility_map_admits_nobody(self) -> None:
        document = spec_document(
            ["billing"],
            [
                interface("IProfileSource", methods={"generate": {"billing": ["id"]}}),
                interface("ILocked", extends=["IProfileSource"], methods={
                    "generate": {},
                }),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["ILocked"],
                    methods={"generate": method(["billing"], ["id"])},
                )
            ],
        )

        report = run(document)

        # The locked entry admits nobody; the parent entry still does, and the
        # candidate satisfies it, so the release is clean.
        self.assertEqual(report.findings, [])
        spec = parse_spec(document)
        locked = spec.resolve_method("ILocked", "generate")
        self.assertTrue(locked.binding)
        self.assertEqual(dict(locked.resolved), {})

    def test_empty_visibility_map_on_the_only_branch_binds(self) -> None:
        # A declaration that admits nobody must not be read as "no constraint":
        # the parent still admits billing, so its own entry is unaffected, but a
        # merge over two empty-declaring branches admits nobody.
        document = spec_document(
            ["billing"],
            [
                interface("ILeft", methods={"generate": {}}),
                interface("IRight", methods={"generate": {}}),
                interface("IBoth", extends=["ILeft", "IRight"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IBoth"],
                    methods={"generate": method(["billing"], ["id"])},
                )
            ],
        )

        spec = parse_spec(document)
        merged = spec.resolve_method("IBoth", "generate")

        self.assertTrue(merged.binding)
        self.assertEqual(dict(merged.resolved), {})
        self.assertEqual(check(spec).findings, [])

    def test_declaring_an_identity_with_no_fields_admits_it_with_an_empty_view(
        self,
    ) -> None:
        document = spec_document(
            ["billing"],
            [interface("IProfileSource", methods={"generate": {"billing": []}})],
            [
                implementation(
                    "ReadsNothing",
                    implements=["IProfileSource"],
                    methods={"generate": method(["billing"], [])},
                ),
                implementation(
                    "ReadsEverything",
                    implements=["IProfileSource"],
                    methods={"generate": method(["billing"], ["id"])},
                ),
            ],
        )

        report = run(document)

        self.assertEqual(
            codes(report),
            [("overexposure", "IProfileSource.generate", "billing", "ReadsEverything")],
        )


class ExactModeTests(unittest.TestCase):
    def test_exact_mode_reports_a_field_that_silently_disappears(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IProfileSource", methods={"generate": {
                "billing": ["id", "plan"],
            }})],
            [
                implementation(
                    "Candidate",
                    implements=["IProfileSource"],
                    methods={"generate": method(["billing"], ["id"])},
                )
            ],
        )
        spec = parse_spec(document)

        lenient = check(spec)
        strict = check(spec, exact=True)

        self.assertEqual(lenient.status, "ready")
        self.assertEqual(strict.status, "rejected")
        self.assertEqual(
            [(f.code, f.entry, f.identity) for f in strict.findings],
            [("reduced-visibility", "IProfileSource.generate", "billing")],
        )
        self.assertEqual(strict.findings[0].allowed, ("id", "plan"))
        self.assertEqual(strict.findings[0].actual, ("id",))

    def test_exact_mode_does_not_double_report_a_leak(self) -> None:
        # A range violation is reported once as overexposure; the same identity
        # must not additionally be reported for any dropped field.
        document = spec_document(
            ["billing"],
            [interface("IProfileSource", methods={"generate": {"billing": ["id"]}})],
            [
                implementation(
                    "Candidate",
                    implements=["IProfileSource"],
                    methods={"generate": method(["billing"], ["id", "plan"])},
                )
            ],
        )

        report = check(parse_spec(document), exact=True)

        self.assertEqual(
            [(f.code, f.entry, f.identity) for f in report.findings],
            [("overexposure", "IProfileSource.generate", "billing")],
        )


class StatusPrecedenceTests(unittest.TestCase):
    def test_a_violation_outranks_a_pending_entry(self) -> None:
        # One entry is decidable and broken, another is undecided.  The report
        # must say "rejected": a known violation is not softened by an
        # unrelated hole elsewhere.
        document = spec_document(
            ["billing"],
            [
                interface("IProfileSource", methods={"generate": {
                    "billing": ["id"],
                }}),
                interface("ISilent", exposes=["generate"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IProfileSource"],
                    methods={"generate": method(["billing"], ["id", "email"])},
                )
            ],
        )

        report = run(document)

        self.assertEqual(report.status, "rejected")
        self.assertFalse(report.ok)
        self.assertTrue(report.findings)
        self.assertTrue(report.pending)

    def test_private_branch_that_declares_still_binds_the_merge(self) -> None:
        # The private branch is still part of the inheritance graph: its
        # declaration limits what the merge may expose, even though no outside
        # caller can hold a private reference and so it forms no entry of its
        # own.
        document = spec_document(
            ["billing"],
            [
                interface("IPublic", methods={"generate": {
                    "billing": ["id", "email"],
                }}),
                interface(
                    "IPrivate",
                    public=False,
                    methods={"generate": {"billing": ["id"]}},
                ),
                interface("IMerged", extends=["IPublic", "IPrivate"]),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IMerged"],
                    methods={"generate": method(["billing"], ["id", "email"])},
                )
            ],
        )

        report = run(document)

        merged = [f for f in report.findings if f.interface == "IMerged"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].allowed, ("id",))
        self.assertEqual(merged[0].origins, ("IPrivate", "IPublic"))
        # The private branch forms no entry, so it never appears on its own.
        self.assertNotIn(
            "IPrivate.generate", {finding.entry for finding in report.findings}
        )


class InterleavedMultiImplementationTests(unittest.TestCase):
    def test_findings_from_several_implementations_stay_separate_and_sorted(
        self,
    ) -> None:
        document = spec_document(
            ["billing", "analytics"],
            [interface("IProfileSource", methods={"generate": {
                "billing": ["id"],
                "analytics": ["id"],
            }})],
            [
                implementation(
                    "Zeta",
                    implements=["IProfileSource"],
                    methods={"generate": method(["billing"], ["id"])},
                ),
                implementation(
                    "Alpha",
                    implements=["IProfileSource"],
                    methods={"generate": method(
                        ["billing", "analytics"], ["id", "plan"]
                    )},
                ),
            ],
        )

        report = run(document)

        self.assertEqual(
            codes(report),
            [
                ("overexposure", "IProfileSource.generate", "analytics", "Alpha"),
                ("overexposure", "IProfileSource.generate", "billing", "Alpha"),
                ("rejects-identity", "IProfileSource.generate", "analytics", "Zeta"),
            ],
        )

    def test_one_implementation_bound_to_two_interfaces_is_judged_through_both(
        self,
    ) -> None:
        document = spec_document(
            ["billing"],
            [
                interface("IA", methods={"generate": {"billing": ["id"]}}),
                interface("IB", methods={"generate": {"billing": ["id", "plan"]}}),
            ],
            [
                implementation(
                    "Candidate",
                    implements=["IA", "IB"],
                    methods={"generate": method(["billing"], ["id", "plan"])},
                )
            ],
        )

        report = run(document)

        self.assertEqual(
            codes(report),
            [("overexposure", "IA.generate", "billing", "Candidate")],
        )


if __name__ == "__main__":
    unittest.main()
