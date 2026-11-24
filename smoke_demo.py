"""Manual end-to-end smoke demonstration using synthetic data only.

Run:  python3 smoke_demo.py
"""

from datetime import date

from retention_decisions import (
    DerivedArtifact,
    Material,
    PreservationNotice,
    RetentionEngine,
    RetentionStatus,
)


def main() -> None:
    engine = RetentionEngine()
    engine.register_material(
        Material.create(
            "case-1",
            ["name", "ssn", "address", "notes"],
            date(2027, 1, 1),
        )
    )
    # Hold covers two of four columns; the others keep the original schedule.
    engine.register_notice(
        PreservationNotice.create(
            "N-1", "case-1", ["ssn", "name"], date(2026, 6, 1)
        )
    )
    # Correction filed later, backdated: narrows N-1 to ssn only.
    engine.register_notice(
        PreservationNotice.create(
            "N-2", "case-1", ["ssn"], date(2027, 2, 1),
            amends_notice_id="N-1",
        )
    )
    engine.register_artifact(
        DerivedArtifact.create(
            "A-full", "case-1", ["name", "ssn", "address", "notes"]
        )
    )
    engine.register_artifact(
        DerivedArtifact.create("A-addr", "case-1", ["address"])
    )

    for day in (date(2026, 3, 1), date(2026, 6, 1), date(2028, 3, 1)):
        decision = engine.decide("case-1", day)
        print(f"--- as of {day.isoformat()} ---")
        print("  controlled view fields :", decision.controlled_view_fields)
        print("  withdrawn fields       :", decision.withdrawn_fields)
        print("  normal view fields     :", decision.normal_view_fields)
        print("  controlled artifacts   :",
              decision.controlled_view_artifacts)
        print("  withdrawn artifacts    :", decision.withdrawn_artifacts)
        print("  normal artifacts       :", decision.normal_view_artifacts)
        for link in decision.reference_links:
            print(
                f"  ref {link.artifact_id} -> {link.field_name} "
                f"[{link.field_status.value}]"
            )

    # Replay a past day: the later correction must not apply before its date.
    replay = engine.decide("case-1", date(2027, 1, 15))
    assert replay.field_status("name") is RetentionStatus.PRESERVED
    assert replay.field_status("address") is RetentionStatus.EXPIRED
    print("\nreplay 2027-01-15 (before N-2 effective date): "
          "name still", replay.field_status("name").value)


if __name__ == "__main__":
    main()
