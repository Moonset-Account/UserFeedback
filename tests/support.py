"""Shared helpers and deterministic fixtures for the test suite."""

from __future__ import annotations

from typing import Any

from dualversion import BackendStore

# A fixed key keeps credentials deterministic across a test run.
TEST_SECRET = b"dualversion-test-secret-0123456789"


def new_store(**kwargs: Any) -> BackendStore:
    kwargs.setdefault("secret", TEST_SECRET)
    return BackendStore(**kwargs)


def by_name(store: BackendStore) -> dict[str, dict[str, Any]]:
    """Canonical items keyed by name, for order-independent assertions."""

    return {item["name"]: item for item in store.spec_get()["items"]}


def legacy_by_name(store: BackendStore) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in store.legacy_get()["items"]}


def credential_for(store: BackendStore, name: str) -> str:
    items = legacy_by_name(store)
    if name not in items:
        raise AssertionError(f"no legacy item named {name!r}")
    return items[name]["credential"]


def spec_item(store: BackendStore, backend_id: str) -> dict[str, Any]:
    return store.spec_get(backend_id)["items"][0]
