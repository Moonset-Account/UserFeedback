#!/usr/bin/env python3
"""Run an in-memory synthetic investigation scenario.

This uses only fabricated local events and evidence IDs; it performs no
network access and reads no real target data.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_order import OrderAdjudicator


def build_case() -> OrderAdjudicator:
    adj = OrderAdjudicator()

    # Source-internal orders.
    adj.add_sequence(
        "phone-log",
        ["phone_boot", "app_open", "message_tap", "message_send"],
        evidence_id="SEQ-PHONE",
    )
    adj.add_sequence(
        "server-log",
        ["gateway_recv", "auth_ok", "db_write"],
        evidence_id="SEQ-SERVER",
    )

    # Two independent observations link the same initiation/receipt events.
    adj.add_observation(
        "OBS-NET-1",
        source="network-capture-1",
        initiation_event="message_send",
        receipt_event="gateway_recv",
    )
    adj.add_observation(
        "OBS-NET-2",
        source="network-capture-2",
        initiation_event="message_send",
        receipt_event="gateway_recv",
    )

    # A separate scoped contradiction/correction example.
    adj.add_observation(
        "OBS-MISASSOCIATED",
        source="clock-observer",
        initiation_event="device_a_send",
        receipt_event="device_b_recv",
        scope="incident-42",
    )
    adj.add_sequence(
        "device-b-log",
        ["device_b_recv", "device_a_send"],
        evidence_id="SEQ-DEVICE-B",
        scope="incident-42",
    )
    return adj


def show_verdict(title: str, verdict) -> None:
    print(f"\n== {title} ==")
    print(f"outcome: {verdict.outcome}")
    if verdict.chain is not None:
        print(f"chain: {verdict.chain.render()}")
    if verdict.conflicts:
        for conflict in verdict.conflicts:
            print(
                "conflict: "
                f"{conflict.evidence_ids} from {conflict.source_ids} "
                f"in scope {conflict.scope!r}"
            )
    print(f"critical sources: {list(verdict.critical_sources)}")
    for source, test in verdict.removal_tests.items():
        print(
            f"  remove {source}: "
            f"evidence_survives={test.still_has_evidence}, "
            f"outcome={test.outcome_without_source}"
        )


def main() -> None:
    adj = build_case()

    normal = adj.query("phone_boot", "db_write")
    show_verdict("normal interleaved cross-device chain", normal)

    disputed = adj.query(
        "device_a_send", "device_b_recv", scope="incident-42"
    )
    show_verdict("before correction", disputed)

    adj.retract_evidence(
        "OBS-MISASSOCIATED",
        retraction_id="R-1",
        reason="observation was misassociated during correction",
    )
    corrected = adj.query(
        "device_a_send", "device_b_recv", scope="incident-42"
    )
    show_verdict("after retracting OBS-MISASSOCIATED", corrected)


if __name__ == "__main__":
    main()
