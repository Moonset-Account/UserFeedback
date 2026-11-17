#!/usr/bin/env python3
"""Replayable demonstration: DNS rebinding cannot replace an approved address.

The in-memory resolver changes its answer for the same hostname while a
session runs. The demo shows:

1. hop 1 is planned against a clean address and the plan pins that address;
2. the resolver then "rebinds" the name to a different address - re-querying
   immediately before connect cannot swap the approved IP;
3. a later rebind to a forbidden address is rejected when a new plan is made;
4. every redirect hop is planned independently with no carried-over state.

Run:  python3 examples/rebinding_demo.py
No network is used.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imgplan import (  # noqa: E402
    ConnectionPlanner,
    DomainAllowList,
    FakeClock,
    ForbiddenNetworks,
    InMemoryResolver,
    PlannedConnector,
    PlannedSession,
    PlanError,
)


def main() -> int:
    allow = DomainAllowList([".example.org"])
    forbidden = ForbiddenNetworks(["10.0.0.0/8", "169.254.0.0/16"])
    clock = FakeClock(2000.0)
    resolver = InMemoryResolver()
    planner = ConnectionPlanner(allow, forbidden, resolver, clock)

    print("== 1. First resolution: clean address, plan is pinned ==")
    resolver.set_record("rebind.example.org", ["rebind.example.org"], ["203.0.113.50"], 60)
    plan = planner.plan_url("https://rebind.example.org/fetch")
    print("plan", plan.plan_id[:8], "approved =", plan.approved_addresses)

    print("\n== 2. Adapter rebinds to 198.51.100.50 before connect ==")
    resolver.replace_addresses("rebind.example.org", ["198.51.100.50"])
    connector = PlannedConnector(plan, clock)
    try:
        connector.connect(resolver=resolver)
    except PlanError as exc:
        print("re-query swap refused:", exc)
    conn = connector.connect()
    print("actual connection still goes to:", conn.remote_ip)

    print("\n== 3. Rebind to forbidden 10.0.0.66: re-planning rejects ==")
    resolver.replace_addresses("rebind.example.org", ["10.0.0.66"])
    session = PlannedSession(planner, clock)
    hop = session.follow("https://rebind.example.org/fetch")
    print("hop allowed:", hop.allowed, "code:", hop.rejection_code)

    print("\n== 4. Redirect to another host: independent per-hop plan ==")
    resolver.set_record("cdn.example.org", ["cdn.example.org"], ["203.0.113.77"], 60)
    hop2 = session.follow("https://cdn.example.org/asset.jpg")
    print("hop allowed:", hop2.allowed, "plan:", hop2.plan.plan_id[:8],
          "ip:", hop2.plan.selected_ip)
    print("resolver query sequence:", resolver.call_sequence())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
