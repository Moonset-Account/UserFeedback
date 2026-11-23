"""Executable walkthrough of the dual-version store.

Run from the workspace root::

    python3 -m dualversion.demo

Every value printed below comes from live calls against a fresh in-memory
store; nothing is hard-coded.  The script is a demo, not a test: the
authoritative verification lives in ``tests/``.
"""

from __future__ import annotations

import json

from .errors import WriteConflict
from .store import BackendStore

SECRET = b"dualversion-demo-secret-0123456789"


def _show(title: str, payload: object) -> None:
    print(f"\n## {title}")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    store = BackendStore(secret=SECRET)

    _show(
        "1. Seed canonical resources",
        [
            store.spec_add("api", "10.0.0.1:80", weight=40, drain=False),
            store.spec_add("web", "10.0.0.2:80", weight=60, drain=True),
        ],
    )

    legacy = store.legacy_get()
    _show("2. Legacy read (name/address/credential only)", legacy)
    api_credential = legacy["items"][0]["credential"]

    _show(
        "3. Legacy renames through its credential",
        store.legacy_write(api_credential, {"name": "api-v2"}),
    )
    _show("4. Canonical view after legacy rename", store.spec_get())

    # A spec writer changes an invisible field; the legacy writer merges.
    api_id = store.spec_get()["items"][0]["id"]
    api_versions = store.spec_get(api_id)["items"][0]["versions"]
    _show(
        "5. Spec bumps weight (invisible to legacy)",
        store.spec_write(api_id, {"weight": 75}, {"weight": api_versions["weight"]}),
    )
    fresh_credential = store.legacy_get()["items"][0]["credential"]
    _show(
        "6. Legacy edits address using fresh credential (merge)",
        store.legacy_write(fresh_credential, {"address": "10.0.0.11:80"}),
    )
    _show("7. Canonical view after merge", store.spec_get(api_id))

    # Two legacy writers start from the same read: second one loses.
    shared = store.legacy_get()["items"][0]["credential"]
    store.legacy_write(shared, {"address": "10.0.0.12:80"})
    try:
        store.legacy_write(shared, {"address": "10.0.0.13:80"})
    except WriteConflict as conflict:
        _show("8. Conflicting legacy write (stale credential)", conflict.to_dict())

    # Name swap: attributes must stay attached to the backend id.
    _show("9. Canonical view before swap", store.spec_get())
    items = [dict(item) for item in store.legacy_get()["items"]]
    items[0]["name"], items[1]["name"] = items[1]["name"], items[0]["name"]
    _show(
        "10. Legacy swaps two names in one commit",
        {
            "summary": store.legacy_commit(items)["summary"],
            "canonical": store.spec_get()["items"],
        },
    )

    # Explicit delete by credential.
    doomed = store.legacy_get()["items"][0]["credential"]
    _show("11. Legacy deletes one backend", store.legacy_delete(doomed))
    _show("12. Canonical view after delete", store.spec_get())

    _show("13. Mutation history", store.history())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
