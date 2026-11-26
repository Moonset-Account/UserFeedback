import unittest

from safe_dump_planner import (
    ByteRange,
    DiagnosticTarget,
    DomainPolicy,
    DumpRequest,
    FragmentNeed,
    PageSpec,
    PointerRef,
    PolicyRule,
    VirtualAlias,
    materialize_dump,
    plan_safe_dump,
)


class SyntheticIncidentIntegrationTests(unittest.TestCase):
    def test_shared_crash_node_dump_plan_end_to_end(self):
        pointer_address = 0x8000
        request = DumpRequest(
            pages=[
                PageSpec(
                    "shared-slab",
                    size=128,
                    role="allocation",
                    data=pointer_address.to_bytes(8, "little") + b"alloc-v7" + bytes(112),
                ),
                PageSpec("downstream-record", size=128, role="allocation"),
            ],
            aliases=[
                VirtualAlias("shared-a", "shared-slab", 0x4000, "a"),
                VirtualAlias("shared-b", "shared-slab", 0x6000, "b"),
                VirtualAlias("down-a", "downstream-record", pointer_address, "a"),
                VirtualAlias("down-b", "downstream-record", 0xA000, "b"),
            ],
            policies=[
                DomainPolicy.full("a", addresses=True),
                DomainPolicy(
                    "b",
                    allow_all_addresses=True,
                    allow=[
                        PolicyRule(page_id="shared-slab", ranges=[(0, 64)]),
                        PolicyRule(page_id="downstream-record", whole_page=True),
                    ],
                ),
            ],
            pointers=[
                PointerRef(
                    pointer_id="alloc-next",
                    page_id="shared-slab",
                    offset=0,
                    size=8,
                    target_page="downstream-record",
                    value=pointer_address,
                    downstream=FragmentNeed(
                        page_id="downstream-record", ranges=[(0, 32)]
                    ),
                )
            ],
            targets=[
                DiagnosticTarget(
                    "primary-allocation",
                    priority=10,
                    page_id="shared-slab",
                    # Interleaves pointer bytes [0,8] with the ASCII header [8,16]
                    # and additional metadata [16,48].
                    ranges=[(0, 48)],
                ),
                DiagnosticTarget(
                    "domain-b-only-gap",
                    priority=1,
                    page_id="shared-slab",
                    ranges=[(70, 80)],
                ),
            ],
            budget_bytes=80,
        )

        plan = plan_safe_dump(request)
        self.assertEqual(
            plan.pages["shared-slab"].allowed_ranges,
            (ByteRange(0, 64),),
        )
        self.assertEqual(
            [(f.page_id, f.start, f.end) for f in plan.fragments],
            [("shared-slab", 0, 48), ("downstream-record", 0, 32)],
        )
        self.assertEqual(plan.retained_bytes, 80)
        self.assertEqual(plan.targets[0].status, "retained")
        self.assertEqual(plan.targets[1].status, "security_denied")
        self.assertEqual(plan.pointer_decisions[0].status, "clear_retained")

        artifact = materialize_dump(request, plan)
        shared_segment = next(
            s for s in artifact.segments if s.start == 0 and s.end == 48
        )
        self.assertEqual(
            shared_segment.data[:8], pointer_address.to_bytes(8, "little")
        )
        self.assertTrue(shared_segment.data[8:16].startswith(b"alloc-v7"))
        self.assertEqual(artifact.verify_no_restricted_addresses(request), [])

    def test_alias_target_and_virtual_address_pointer_resolution(self):
        request = DumpRequest(
            pages=[PageSpec("src", size=64), PageSpec("dst", size=64)],
            aliases=[
                VirtualAlias("src-va", "src", 0x4000, "d"),
                VirtualAlias("dst-va", "dst", 0x8000, "d"),
            ],
            policies=[DomainPolicy.full("d", addresses=True)],
            pointers=[
                PointerRef(
                    page_id="src",
                    offset=0,
                    domain="d",
                    target_virtual_address=0x8000,
                    downstream=FragmentNeed(alias_id="dst-va", ranges=[(0, 16)]),
                )
            ],
            targets=[DiagnosticTarget("by-alias", alias_id="src-va", ranges=[(0, 8)])],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(plan.targets[0].status, "retained")
        self.assertEqual(plan.pointer_decisions[0].target_page, "dst")
        self.assertEqual(plan.retained_bytes, 24)

    def test_budget_one_byte_smaller_reports_deficit_and_discards_chain(self):
        def build(budget):
            return DumpRequest(
                pages=[PageSpec("src", size=64), PageSpec("dst", size=64)],
                aliases=[VirtualAlias("a", "src", domain="d"), VirtualAlias("b", "dst", domain="d")],
                policies=[DomainPolicy.full("d", addresses=True)],
                pointers=[
                    PointerRef(
                        page_id="src",
                        offset=0,
                        target_page="dst",
                        downstream=FragmentNeed(page_id="dst", ranges=[(0, 32)]),
                    )
                ],
                targets=[DiagnosticTarget("t", page_id="src", ranges=[(0, 16)])],
                budget_bytes=budget,
            )

        self.assertEqual(plan_safe_dump(build(48)).targets[0].status, "retained")
        tight = plan_safe_dump(build(47))
        self.assertEqual(tight.targets[0].status, "budget_denied")
        self.assertEqual(tight.targets[0].reasons[0].details["deficit_bytes"], 1)


if __name__ == "__main__":
    unittest.main()
