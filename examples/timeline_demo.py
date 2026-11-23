"""Print the state-transition table for the archived/held/deleted scenario.

Run from the workspace root::

    python3 -I -m examples.timeline_demo

Output is produced live from ``version_lifecycle``; it is a readable trace,
not a substitute for the unittest suite.
"""

from __future__ import annotations

from version_lifecycle import ReadState, VersionedStore

KEY = "objects/report"


def destroyable(store: VersionedStore, now: float) -> tuple[str, ...]:
    if not store.has_key(KEY):
        return ()
    return store.destroyable_version_ids(KEY, now=now)


def show(store: VersionedStore, label: str, now: float) -> None:
    default = store.read(KEY, now=now)
    v1 = store.read(KEY, now=now, version_id="v1")
    ids = destroyable(store, now)
    print(
        f"{label:<34} now={now:>4.0f}  default={default.state.value:<8}"
        f" ({default.version_id or '-'})  v1={v1.state.value:<8}"
        f"  destroyable={','.join(ids) if ids else '-'}"
    )


def main() -> None:
    store = VersionedStore()
    store.put_version(KEY, "v1", archived=True, retain_until=5)
    store.put_version(KEY, "v2", retain_until=20, holds=["legal"])
    store.put_delete_marker(KEY, "d1", retain_until=10)

    print("steps (state after each transition)")
    show(store, "initial: d1 on top", 0)

    store.grant_lease(KEY, "v1", "restore-1", granted_at=5, expires_at=30)
    show(store, "grant restore lease on v1", 5)
    show(store, "marker deadline passed", 10)
    show(store, "v2 retention passed, still held", 20)

    store.release_hold(KEY, "v2", "legal")
    show(store, "hold released on v2", 25)
    plan = store.scan_expiry(now=25)
    report = store.execute_plan(plan, now=25)
    print(
        f"  sweep @25: plan {' '.join(i.version_id for i in plan.items)} ->"
        f" removed={report.removed_count} retained={report.retained_count}"
    )
    show(store, "after sweeping v2", 25)

    show(store, "lease expired", 30)
    plan = store.scan_expiry(now=30)
    report = store.execute_plan(plan, now=30)
    print(
        f"  sweep @30: plan {' '.join(i.version_id for i in plan.items)} ->"
        f" removed={report.removed_count}"
    )
    show(store, "after sweeping v1", 30)

    plan = store.scan_expiry(now=30)
    report = store.execute_plan(plan, now=30)
    print(
        f"  sweep @30: plan {' '.join(i.version_id for i in plan.items)} ->"
        f" removed={report.removed_count}"
    )
    show(store, "after sweeping the marker", 30)

    assert store.read(KEY, now=30).state is ReadState.MISSING
    print("final: key absent, default read MISSING")


if __name__ == "__main__":
    main()
