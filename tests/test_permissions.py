import unittest

from safe_dump_planner import (
    DiagnosticTarget,
    DomainPolicy,
    DumpRequest,
    FieldSpec,
    PageSpec,
    PolicyRule,
    VirtualAlias,
    plan_safe_dump,
)


class PermissionIntersectionTests(unittest.TestCase):
    def test_shared_physical_page_uses_intersection_of_all_domains(self):
        request = DumpRequest(
            pages=[PageSpec("shared", size=256)],
            aliases=[
                VirtualAlias("va", "shared", 0x1000, "a"),
                VirtualAlias("vb", "shared", 0x2000, "b"),
            ],
            policies=[
                DomainPolicy(
                    "a",
                    allow=[PolicyRule(page_id="shared", ranges=[(0, 128)])],
                    allow_all_addresses=True,
                ),
                DomainPolicy(
                    "b",
                    allow=[PolicyRule(page_id="shared", ranges=[(64, 192)])],
                    allow_all_addresses=True,
                ),
            ],
            targets=[
                DiagnosticTarget("allowed", page_id="shared", ranges=[(70, 96)]),
                DiagnosticTarget("denied-gap", page_id="shared", ranges=[(0, 80)]),
            ],
        )
        plan = plan_safe_dump(request)

        self.assertEqual(
            [(r.start, r.end) for r in plan.pages["shared"].allowed_ranges],
            [(64, 128)],
        )
        self.assertEqual(plan.targets[0].status, "retained")
        denied = plan.targets[1]
        self.assertEqual(denied.status, "security_denied")
        reason = next(r for r in denied.reasons if r.code == "SECURITY_DENIED")
        self.assertEqual(reason.range.to_dict(), [0, 64])
        self.assertEqual(reason.domains, ("b",))

    def test_field_permissions_resolve_to_byte_intervals(self):
        page = PageSpec(
            "alloc",
            size=128,
            fields=[FieldSpec("next", 0, 8), FieldSpec("size", 8, 8)],
        )
        request = DumpRequest(
            pages=[page],
            aliases=[VirtualAlias("a", "alloc", domain="d")],
            policies=[
                DomainPolicy(
                    "d",
                    allow=[PolicyRule(page_id="alloc", field_names=["size"])],
                )
            ],
            targets=[
                DiagnosticTarget("wants-size", page_id="alloc", field_names=["size"]),
                DiagnosticTarget("wants-next", page_id="alloc", field_names=["next"]),
            ],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(
            plan.pages["alloc"].allowed_ranges[0].to_dict(), [8, 16]
        )
        self.assertEqual(plan.targets[0].status, "retained")
        self.assertEqual(plan.targets[1].status, "security_denied")

    def test_missing_policy_and_unlabeled_page_deny_by_default(self):
        labeled = DumpRequest(
            pages=[PageSpec("p", size=32)],
            aliases=[VirtualAlias("a", "p", domain="missing")],
            targets=[DiagnosticTarget("t", page_id="p", ranges=[(0, 8)])],
        )
        plan = plan_safe_dump(labeled)
        self.assertEqual(plan.targets[0].status, "security_denied")
        self.assertIn("missing", plan.targets[0].reasons[0].domains)

        unlabeled = DumpRequest(
            pages=[PageSpec("p", size=32)],
            targets=[DiagnosticTarget("t", page_id="p", ranges=[(0, 8)])],
        )
        plan = plan_safe_dump(unlabeled)
        self.assertEqual(plan.targets[0].status, "security_denied")
        self.assertEqual(plan.targets[0].reasons[0].code, "NO_RELEASE_POLICY")

    def test_wildcard_policy_is_fallback_for_unnamed_domain(self):
        request = DumpRequest(
            pages=[PageSpec("p", size=32)],
            aliases=[VirtualAlias("a", "p", domain="tenant")],
            policies=[DomainPolicy.full("*")],
            targets=[DiagnosticTarget("t", page_id="p", ranges=[(0, 8)])],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(plan.targets[0].status, "retained")

    def test_address_permission_also_requires_all_domains(self):
        request = DumpRequest(
            pages=[PageSpec("p", size=32, physical_address=0x1234000)],
            aliases=[VirtualAlias("a", "p", 0x4000, "a")],
            policies=[
                DomainPolicy.full("a", addresses=True),
                DomainPolicy("b", allow_all_content=True, allow_all_addresses=False),
            ],
        )
        # Explicitly make b a coherent referencing domain.
        request.pages[0].coherent_domains = ("a", "b")
        plan = plan_safe_dump(request)
        self.assertFalse(plan.pages["p"].address_allowed)


if __name__ == "__main__":
    unittest.main()
