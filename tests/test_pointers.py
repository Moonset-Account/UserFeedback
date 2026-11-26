import unittest

from safe_dump_planner import (
    DiagnosticTarget,
    DomainPolicy,
    DumpRequest,
    FragmentNeed,
    PageSpec,
    PointerRef,
    VirtualAlias,
    materialize_dump,
    plan_safe_dump,
)


class PointerClosureTests(unittest.TestCase):
    def test_intact_pointer_creates_downstream_read_requirement(self):
        secret = 0x4000
        target_addr = 0x8000
        request = DumpRequest(
            pages=[
                PageSpec(
                    "src",
                    size=64,
                    data=secret.to_bytes(8, "little") + b"metadata",
                ),
                PageSpec("dst", size=128, physical_address=target_addr),
            ],
            aliases=[
                VirtualAlias("src-alias", "src", secret, "d"),
                VirtualAlias("dst-alias", "dst", target_addr, "d"),
            ],
            policies=[DomainPolicy.full("d", addresses=True)],
            pointers=[
                PointerRef(
                    pointer_id="next",
                    page_id="src",
                    offset=0,
                    size=8,
                    target_page="dst",
                    value=target_addr,
                    downstream=FragmentNeed(page_id="dst", ranges=[(0, 32)]),
                )
            ],
            targets=[
                DiagnosticTarget("root", page_id="src", ranges=[(0, 16)]),
            ],
            budget_bytes=48,
        )
        plan = plan_safe_dump(request)
        self.assertEqual(
            [(f.page_id, f.start, f.end) for f in plan.fragments],
            [("src", 0, 16), ("dst", 0, 32)],
        )
        decision = plan.pointer_decisions[0]
        self.assertEqual(decision.status, "clear_retained")
        self.assertTrue(decision.clear_pointer_retained)
        self.assertEqual(decision.downstream_atom_count, 1)

        rebuilt = DumpRequest(
            pages=request.pages,
            policies=request.policies,
            aliases=request.aliases,
            pointers=request.pointers,
            targets=request.targets,
            budget_bytes=47,
        )
        short_plan = plan_safe_dump(rebuilt)
        self.assertEqual(short_plan.targets[0].status, "budget_denied")
        self.assertEqual(short_plan.retained_bytes, 0)

    def test_cropped_pointer_does_not_create_downstream_need(self):
        request = DumpRequest(
            pages=[
                PageSpec(
                    "src",
                    size=64,
                    fields=[],
                ),
                PageSpec("dst", size=128),
            ],
            aliases=[VirtualAlias("a", "src", domain="d")],
            policies=[DomainPolicy.full("d", addresses=True)],
            pointers=[
                PointerRef(
                    page_id="src",
                    offset=0,
                    size=8,
                    target_page="dst",
                    downstream=FragmentNeed(page_id="dst", ranges=[(0, 32)]),
                )
            ],
            targets=[
                # Byte-range/field crop deliberately starts after the pointer.
                DiagnosticTarget("metadata-only", page_id="src", ranges=[(8, 16)]),
            ],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(plan.retained_bytes, 8)
        self.assertEqual(
            [p.status for p in plan.pointer_decisions], ["omitted"]
        )
        self.assertNotIn("dst", {f.page_id for f in plan.fragments})

    def test_restricted_target_pointer_is_masked_and_not_followed(self):
        target_addr = 0x7F10_0000
        src_addr = 0x7F00_0000
        data = target_addr.to_bytes(8, "little") + b"DIAGDATA"
        request = DumpRequest(
            pages=[
                PageSpec("src", size=64, data=data, physical_address=src_addr),
                PageSpec("dst", size=128, physical_address=target_addr),
            ],
            aliases=[
                VirtualAlias("src-a", "src", src_addr, "a"),
                VirtualAlias("src-b", "src", src_addr + 0x1000, "b"),
                VirtualAlias("dst-a", "dst", target_addr, "a"),
                VirtualAlias("dst-b", "dst", target_addr + 0x1000, "b"),
            ],
            policies=[
                DomainPolicy.full("a", addresses=True),
                DomainPolicy.full("b", addresses=False),
            ],
            pointers=[
                PointerRef(
                    page_id="src",
                    offset=0,
                    size=8,
                    target_page="dst",
                    value=target_addr,
                    downstream=FragmentNeed(page_id="dst", ranges=[(0, 32)]),
                )
            ],
            targets=[DiagnosticTarget("root", page_id="src", ranges=[(0, 16)])],
            budget_bytes=100,
        )
        plan = plan_safe_dump(request)
        self.assertFalse(plan.pages["dst"].address_allowed)
        self.assertEqual(plan.retained_bytes, 16)
        self.assertEqual(
            plan.pointer_decisions[0].status, "masked_restricted_address"
        )
        self.assertNotIn("dst", {f.page_id for f in plan.fragments})
        target = plan.targets[0]
        self.assertEqual(target.status, "retained_redacted")
        self.assertTrue(target.supported)
        self.assertTrue(target.redacted)
        self.assertFalse(target.complete)

        artifact = materialize_dump(request, plan)
        segment = next(s for s in artifact.segments if s.output_handle == plan.pages["src"].output_handle)
        self.assertEqual(segment.data, b"\x00" * 8 + b"DIAGDATA")
        self.assertEqual(artifact.verify_no_restricted_addresses(request), [])

    def test_partial_pointer_is_masked_and_not_followed(self):
        addr = 0x8000
        request = DumpRequest(
            pages=[PageSpec("src", size=64, data=addr.to_bytes(8, "little"))],
            aliases=[VirtualAlias("a", "src", domain="d")],
            policies=[DomainPolicy.full("d", addresses=True)],
            pointers=[PointerRef(page_id="src", offset=0, size=8, target_page="src")],
            targets=[DiagnosticTarget("half-pointer", page_id="src", ranges=[(0, 4)])],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(
            plan.pointer_decisions[0].status, "masked_partial_pointer"
        )
        segment_range = plan.fragments[0]
        self.assertEqual(segment_range.masked_ranges[0].to_dict(), [0, 4])
        artifact = materialize_dump(request, plan)
        self.assertEqual(artifact.segments[0].data, b"\x00\x00\x00\x00")

    def test_unresolved_pointer_is_masked_by_safe_default(self):
        request = DumpRequest(
            pages=[PageSpec("p", size=32, data=(0x1234).to_bytes(8, "little"))],
            aliases=[VirtualAlias("a", "p", domain="d")],
            policies=[DomainPolicy.full("d", addresses=True)],
            pointers=[PointerRef(page_id="p", offset=0, size=8)],
            targets=[DiagnosticTarget("unknown-ref", page_id="p", ranges=[(0, 8)])],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(
            plan.pointer_decisions[0].status, "masked_unresolved"
        )
        artifact = materialize_dump(request, plan)
        self.assertEqual(artifact.segments[0].data, bytes(8))

    def test_pointer_cycle_reaches_fixed_point(self):
        request = DumpRequest(
            pages=[PageSpec("a", size=64), PageSpec("b", size=64)],
            aliases=[
                VirtualAlias("aa", "a", domain="d"),
                VirtualAlias("ba", "b", domain="d"),
            ],
            policies=[DomainPolicy.full("d", addresses=True)],
            pointers=[
                PointerRef(
                    pointer_id="a-next",
                    page_id="a",
                    offset=0,
                    target_page="b",
                    downstream=FragmentNeed(page_id="b", ranges=[(0, 16)]),
                ),
                PointerRef(
                    pointer_id="b-next",
                    page_id="b",
                    offset=0,
                    target_page="a",
                    downstream=FragmentNeed(page_id="a", ranges=[(0, 16)]),
                ),
            ],
            targets=[DiagnosticTarget("cycle-root", page_id="a", ranges=[(0, 16)])],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(plan.retained_bytes, 32)
        self.assertEqual(
            {p.pointer_key: p.status for p in plan.pointer_decisions},
            {"a-next": "clear_retained", "b-next": "clear_retained"},
        )

    def test_raw_restricted_address_occurrence_is_found_and_masked(self):
        addr = 0xDEAD_BEEF_CAFE_0001
        data = bytes(24) + addr.to_bytes(8, "little")
        request = DumpRequest(
            pages=[
                PageSpec("p", size=64, data=data),
                PageSpec("secret", size=64, physical_address=addr),
            ],
            aliases=[
                VirtualAlias("p-a", "p", 0x4000, "d"),
                VirtualAlias("secret-a", "secret", addr, "d"),
            ],
            policies=[DomainPolicy.full("d", addresses=False)],
            targets=[DiagnosticTarget("raw", page_id="p", ranges=[(16, 40)])],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(
            plan.pages["p"].masked_ranges[0].to_dict(), [24, 32]
        )
        artifact = materialize_dump(request, plan)
        self.assertNotIn(addr.to_bytes(8, "little"), artifact.segments[0].data)
        self.assertEqual(artifact.verify_no_restricted_addresses(request), [])


if __name__ == "__main__":
    unittest.main()
