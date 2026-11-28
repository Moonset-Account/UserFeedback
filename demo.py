"""End-to-end demo: merge two people, concurrent registration, split, replay.

Scenario
--------
A hospital partner and an insurer partner know the same two people (Alice and
Bob) under their own pseudonyms.  A data-quality error declared the two people
to be one identity, so the registry merged them.  While merged, both partners
register again at the same time.  Operations then splits the identity back
apart.  Finally the registry replays what each partner saw at every step.

Every login carries its source institution and the person's stable original
subject number.

Run: ``python3 demo.py``
"""

from __future__ import annotations

import json
import threading

from pseudonym_registry import PseudonymRegistry

HOSPITAL = "hospital"
INSURER = "insurer"
ALICE = "original-alice"
BOB = "original-bob"


def show(title: str, value: object) -> None:
    print(f"{title}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")


def main() -> None:
    reg = PseudonymRegistry()

    print("== 1. Independent identities ==")
    alice_at_hospital = reg.login(HOSPITAL, "hospital-idp", ALICE)
    bob_at_hospital = reg.login(HOSPITAL, "hospital-idp", BOB)
    bob_at_insurer = reg.login(INSURER, "insurer-idp", BOB)
    show("alice @ hospital", alice_at_hospital)
    show("bob @ hospital", bob_at_hospital)
    show("bob @ insurer", bob_at_insurer)

    print("\n== 2. Wrong merge: the two people are declared one identity ==")
    reg.merge(ALICE, BOB)
    show("merged groups", reg.merged_subjects())

    print("\n== 3. Both partners register at the same time ==")
    results: dict[str, str] = {}
    errors: list[BaseException] = []

    def register(partner: str, institution: str, subject: str, key: str) -> None:
        try:
            results[key] = reg.login(partner, institution, subject)
        except BaseException as exc:  # noqa: BLE001 - demo surfaces errors
            errors.append(exc)

    threads = [
        threading.Thread(target=register,
                         args=(HOSPITAL, "hospital-idp", BOB, "hospital")),
        threading.Thread(target=register,
                         args=(INSURER, "insurer-idp", ALICE, "insurer")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    show("hospital's merged handle for bob", results["hospital"])
    show("insurer's merged handle for alice", results["insurer"])

    print("\n== 4. Split back apart and restore each side ==")
    report = reg.split(BOB)
    show("split report", report.as_dict())

    print("\n== 5. What each partner sees after the split ==")
    after = {
        "alice @ hospital": reg.login(HOSPITAL, "hospital-idp", ALICE),
        "bob @ hospital": reg.login(HOSPITAL, "hospital-idp", BOB),
        "alice @ insurer": reg.login(INSURER, "insurer-idp", ALICE),
        "bob @ insurer": reg.login(INSURER, "insurer-idp", BOB),
    }
    show("handles", after)
    print()
    print(f"alice @ hospital unchanged: {after['alice @ hospital'] == alice_at_hospital}")
    print(f"bob    @ hospital unchanged: {after['bob @ hospital'] == bob_at_hospital}")
    print(f"bob    @ insurer  unchanged: {after['bob @ insurer']  == bob_at_insurer}")

    print("\n== 6. Replay: what each partner saw at registration time ==")
    for partner in (HOSPITAL, INSURER):
        print(f"{partner}'s registrations:")
        for row in reg.replay(partner, only_registrations=True):
            print("  " + json.dumps(row, ensure_ascii=False, sort_keys=True))

    print("\n== 7. Partner exports share no identifier ==")
    hospital_export = json.loads(reg.export_json(HOSPITAL))
    insurer_export = json.loads(reg.export_json(INSURER))
    show("hospital export", hospital_export)
    show("insurer export", insurer_export)
    hospital_ids = {row["handle"] for row in hospital_export}
    insurer_ids = {row["handle"] for row in insurer_export}
    print(f"\ncommon handles: {sorted(hospital_ids & insurer_ids)}")
    print(f"common identifiers overall: "
          f"{sorted(_identifiers(hospital_export) & _identifiers(insurer_export))}")


def _identifiers(export: list[dict[str, object]]) -> set[str]:
    found: set[str] = set()
    for row in export:
        for key, value in row.items():
            if key in ("handle", "status", "lineage"):
                continue
            if isinstance(value, str):
                found.add(value)
        found.add(row["handle"])
    return found


if __name__ == "__main__":
    main()
