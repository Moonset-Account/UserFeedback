"""Deterministic local synthetic demo; no network or real crash data."""

from __future__ import annotations

from safe_dump_planner import (
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


def build_request(budget_bytes: int = 80) -> DumpRequest:
    pointer_address = 0x8000
    return DumpRequest(
        pages=[
            PageSpec(
                "shared-slab",
                size=128,
                role="allocation",
                data=pointer_address.to_bytes(8, "little") + b"alloc-v7",
            ),
            PageSpec("downstream-record", size=128, role="allocation"),
            PageSpec("stack-page", size=128, role="stack"),
        ],
        aliases=[
            VirtualAlias("shared-a", "shared-slab", 0x4000, "a"),
            VirtualAlias("shared-b", "shared-slab", 0x6000, "b"),
            VirtualAlias("down-a", "downstream-record", pointer_address, "a"),
            VirtualAlias("down-b", "downstream-record", 0xA000, "b"),
            VirtualAlias("stack-a", "stack-page", 0x7000, "a"),
        ],
        policies=[
            DomainPolicy.full("a", addresses=True),
            DomainPolicy(
                "b",
                allow_all_addresses=True,
                allow=[
                    PolicyRule(page_id="shared-slab", ranges=[(0, 64)]),
                    PolicyRule(page_id="downstream-record", whole_page=True),
                    PolicyRule(page_id="stack-page", whole_page=True),
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
                ranges=[(0, 48)],
            ),
            DiagnosticTarget(
                "shared-page-gap",
                priority=1,
                page_id="shared-slab",
                ranges=[(70, 80)],
            ),
        ],
        budget_bytes=budget_bytes,
    )


def main() -> None:
    request = build_request()
    plan = plan_safe_dump(request)
    print(plan.explain())
    artifact = materialize_dump(request, plan)
    print("\nmanifest:")
    print(artifact.manifest)
    print("address leaks:", artifact.verify_no_restricted_addresses(request))


if __name__ == "__main__":
    main()
