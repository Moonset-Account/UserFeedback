#!/usr/bin/env python3
"""Local synthetic control-room scenario; no network or external service."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from readjudicator import (  # noqa: E402
    AdjudicationSession,
    OperationRequest,
    Proof,
    RelayEdge,
    RelayGraph,
    Requirement,
    adjudicate,
)


def main() -> int:
    graph = RelayGraph(
        [
            RelayEdge.presence_handoff("floor", "control", "presence-handoff"),
            RelayEdge.device_only("gateway", "control", "device-relay"),
            RelayEdge("shared", "control", "shared-token-relay"),
        ]
    )
    proofs = [
        Proof.presence("p-alice", identity="alice", roles=["operator"], node="floor"),
        Proof.presence("p-bob", identity="bob", roles=["supervisor"], node="floor"),
        Proof.device_proximity("d-console", near="terminal-1", node="gateway"),
        Proof("break-glass-token", "credential", capabilities={"break_glass"}, node="shared"),
    ]
    operations = [
        OperationRequest(
            "O-console",
            [
                Requirement("presence", proof_kind="presence", identity_matches_request=True),
                Requirement(
                    "device_proximity",
                    proof_kind="device",
                    attributes={"near": "terminal-1"},
                ),
            ],
            identity="alice",
            entry_node="control",
            deadline=200,
        ),
        OperationRequest(
            "O-supervisor-break-glass",
            [
                Requirement("supervisor", proof_kind="presence", allowed_identities={"bob"}),
                Requirement("break_glass"),
            ],
            identity="control-room",
            entry_node="control",
            deadline=200,
        ),
        OperationRequest(
            "O-third-break-glass",
            [
                Requirement("presence", proof_kind="presence", identity_matches_request=True),
                Requirement("break_glass"),
            ],
            identity="carol",
            entry_node="control",
            deadline=200,
        ),
    ]

    result = adjudicate(operations, proofs, graph=graph, now=100, terminal_id="terminal-1")
    print("batch approved:", result.approved_ids)
    for approved in result.approved:
        print(f"  {approved.request_id}: {approved.proof_combination}")
    for rejected in result.rejected:
        print(f"batch rejected: {rejected.request_id} [{rejected.reason_code}]")
        for missing in rejected.missing_capabilities:
            print(f"  - {missing.capability}: {missing.reason_code}; {missing.message}")

    session = AdjudicationSession(
        [Proof("only-once", "generic", capabilities={"shared"})],
        now=100,
    )
    failed = session.review(
        OperationRequest("interleaved-bad", [Requirement("shared", count=2)], deadline=300)
    )
    retried = session.review(
        OperationRequest("interleaved-good", [Requirement("shared")], deadline=300)
    )
    print("interleaving:", type(failed).__name__, "->", type(retried).__name__)
    print("remaining one-time uses after rollback/retry:", session.ledger.uses_left)

    if result.approved_ids != ("O-console", "O-supervisor-break-glass"):
        raise AssertionError("synthetic batch did not produce expected maximum set")
    if type(failed).__name__ != "RejectedOperation" or type(retried).__name__ != "ApprovedOperation":
        raise AssertionError("interleaving scenario failed")
    if session.ledger.uses_left["only-once"] != 0:
        raise AssertionError("rollback/retry ledger state is incorrect")
    print("synthetic demo assertions: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
