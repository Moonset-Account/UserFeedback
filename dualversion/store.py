"""In-memory dual-version backend store.

Canonical (``spec``) view
    Backends are addressed by an immutable, server-assigned id.  Every mutable
    field (``name``, ``address``, ``weight``, ``drain``) carries its own version
    counter.  Writes are conditional on the exact fields they touch, so a
    concurrent edit of an unrelated field never invalidates them.

Legacy view
    Backends are presented as ``name`` + ``address`` only, plus an opaque,
    signed-and-encrypted *edit credential*.  The credential round-trips to the
    canonical id and to the versions of the two legacy-visible fields, so it is
    simultaneously an identity handle (renames are safe) and an optimistic
    concurrency token (stale edits conflict) without revealing the id.
"""

from __future__ import annotations

import copy
import json
import secrets
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .credentials import CredentialCodec
from .errors import ValidationError, WriteConflict

STABLE_FIELDS: tuple[str, ...] = ("name", "address", "weight", "drain")
LEGACY_FIELDS: tuple[str, ...] = ("name", "address")

DEFAULT_WEIGHT = 100
MIN_WEIGHT = 0
MAX_WEIGHT = 1000
MAX_NAME_LENGTH = 253
MAX_ADDRESS_LENGTH = 2048
MAX_IDEMPOTENCY_KEY_LENGTH = 256
MAX_IDEMPOTENCY_RECORDS = 1000


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{what} must be a mapping")
    return value


def _require_str(value: Any, what: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{what} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{what} must not be empty")
    if len(normalized) > max_length:
        raise ValidationError(f"{what} must be at most {max_length} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        raise ValidationError(f"{what} must not contain control characters")
    return normalized


def _normalize_name(value: Any) -> str:
    return _require_str(value, "name", max_length=MAX_NAME_LENGTH)


def _normalize_address(value: Any) -> str:
    return _require_str(value, "address", max_length=MAX_ADDRESS_LENGTH)


def _normalize_weight(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("weight must be an integer")
    if not MIN_WEIGHT <= value <= MAX_WEIGHT:
        raise ValidationError(f"weight must be between {MIN_WEIGHT} and {MAX_WEIGHT}")
    return value


def _normalize_drain(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError("drain must be a boolean")
    return value


_NORMALIZERS = {
    "name": _normalize_name,
    "address": _normalize_address,
    "weight": _normalize_weight,
    "drain": _normalize_drain,
}


def _normalize_field(field_name: str, value: Any) -> Any:
    return _NORMALIZERS[field_name](value)


def _normalize_changes(
    changes: Any, allowed: tuple[str, ...], view: str
) -> dict[str, Any]:
    changes = _require_mapping(changes, "changes")
    normalized: dict[str, Any] = {}
    for key, value in changes.items():
        if not isinstance(key, str) or key not in allowed:
            allowed_text = ", ".join(allowed)
            raise ValidationError(
                f"{view} cannot write field {key!r}; writable fields: {allowed_text}"
            )
        normalized[key] = _normalize_field(key, value)
    return normalized


def _normalize_expected_versions(
    expected: Any, allowed: tuple[str, ...]
) -> dict[str, int]:
    if expected is None:
        return {}
    expected = _require_mapping(expected, "expected_versions")
    normalized: dict[str, int] = {}
    for key, value in expected.items():
        if not isinstance(key, str) or key not in allowed:
            allowed_text = ", ".join(allowed)
            raise ValidationError(
                f"expected_versions has unknown field {key!r}; fields: {allowed_text}"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValidationError(
                f"expected version for {key!r} must be a positive integer"
            )
        normalized[key] = value
    return normalized


def _normalize_idempotency_key(key: Any) -> str | None:
    if key is None:
        return None
    if not isinstance(key, str):
        raise ValidationError("idempotency_key must be a string")
    normalized = key.strip()
    if not normalized:
        raise ValidationError("idempotency_key must not be empty")
    if len(normalized) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValidationError(
            f"idempotency_key must be at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters"
        )
    return normalized


def _normalize_credential(value: Any, what: str = "credential") -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{what} must be a non-empty string")
    return value


def _as_list(value: Any, what: str) -> list[Any]:
    if isinstance(value, (str, bytes, Mapping)):
        raise ValidationError(f"{what} must be an iterable, not a single value")
    try:
        return list(value)
    except TypeError:
        raise ValidationError(f"{what} must be an iterable") from None


# --------------------------------------------------------------------------- #
# Internal state
# --------------------------------------------------------------------------- #


@dataclass
class _Backend:
    id: str
    name: str
    address: str
    weight: int
    drain: bool
    versions: dict[str, int]
    created_resource_version: int

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "address": self.address,
            "weight": self.weight,
            "drain": self.drain,
            "versions": {name: self.versions[name] for name in STABLE_FIELDS},
            "createdResourceVersion": self.created_resource_version,
        }


@dataclass(frozen=True)
class _DecodedCredential:
    backend_id: str
    versions: dict[str, int]


@dataclass
class _CommitItem:
    index: int
    credential: str | None
    name: str
    address: str
    decoded: _DecodedCredential | None = None
    status: str = "unchanged"
    changed: list[str] = field(default_factory=list)
    previous_name: str | None = None
    assigned_id: str | None = None


@dataclass
class _IdempotencyRecord:
    fingerprint: str
    result: dict[str, Any]


def _deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _conflict_reason(entries: list[dict[str, Any]]) -> str:
    reasons = {entry["reason"] for entry in entries}
    if len(reasons) == 1:
        return reasons.pop()
    return "multiple_conflicts"


class BackendStore:
    """A single-process, in-memory backend registry with two read/write views.

    Parameters
    ----------
    secret:
        Optional >= 16 byte key used to sign and encrypt legacy credentials.
        A random key is generated when omitted.  Supply a stable secret if
        credentials must survive a restart or be shared by several instances.
    history_limit:
        Maximum number of applied mutations retained by :meth:`history`.
    """

    def __init__(
        self, secret: bytes | None = None, *, history_limit: int = 500
    ) -> None:
        if isinstance(history_limit, bool) or not isinstance(history_limit, int):
            raise ValidationError("history_limit must be an integer")
        if history_limit < 0:
            raise ValidationError("history_limit must be non-negative")
        self._lock = threading.RLock()
        self._items: dict[str, _Backend] = {}
        self._order: list[str] = []
        self._resource_version = 0
        self._idempotency: OrderedDict[str, _IdempotencyRecord] = OrderedDict()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._codec = CredentialCodec(secret)

    # ------------------------------------------------------------------ #
    # Generic plumbing
    # ------------------------------------------------------------------ #

    def _next_backend_id(self) -> str:
        while True:
            candidate = f"be-{secrets.token_hex(6)}"
            if candidate not in self._items:
                return candidate

    def _encode_credential(self, item: _Backend) -> str:
        return self._codec.encode(
            item.id, item.versions["name"], item.versions["address"]
        )

    def _decode_credential(self, token: str) -> _DecodedCredential:
        backend_id, versions = self._codec.decode(token)
        return _DecodedCredential(backend_id=backend_id, versions=versions)

    @staticmethod
    def _fingerprint(operation: str, payload: Any) -> str:
        return json.dumps(
            [operation, payload],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def _lookup_replay(
        self, key: str | None, fingerprint: str
    ) -> dict[str, Any] | None:
        if key is None:
            return None
        record = self._idempotency.get(key)
        if record is None:
            return None
        self._idempotency.move_to_end(key)
        if record.fingerprint != fingerprint:
            raise WriteConflict(
                "idempotency_key_reuse",
                targets=[],
                resource_version=self._resource_version,
            )
        replayed = _deep_copy(record.result)
        replayed["replayed"] = True
        return replayed

    def _remember(
        self, key: str | None, fingerprint: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        if key is not None:
            self._idempotency[key] = _IdempotencyRecord(
                fingerprint=fingerprint, result=_deep_copy(result)
            )
            self._idempotency.move_to_end(key)
            while len(self._idempotency) > MAX_IDEMPOTENCY_RECORDS:
                self._idempotency.popitem(last=False)
        return result

    def _record(self, entry: dict[str, Any], resource_version: int) -> None:
        snapshot = _deep_copy(entry)
        snapshot["resourceVersion"] = resource_version
        self._history.append(snapshot)

    @staticmethod
    def _raise_conflicts(entries: list[dict[str, Any]], resource_version: int) -> None:
        if not entries:
            return
        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for entry in entries:
            key = (entry.get("id"), entry.get("reason"), entry.get("field"))
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(entry)
        raise WriteConflict(
            _conflict_reason(deduplicated),
            targets=deduplicated,
            resource_version=resource_version,
        )

    def _name_in_use(self, name: str, *, exclude_id: str | None = None) -> bool:
        return any(
            item.name == name and item.id != exclude_id
            for item in self._items.values()
        )

    def _snapshot_all(self) -> dict[str, Any]:
        return {
            "resourceVersion": self._resource_version,
            "items": [self._items[bid].snapshot() for bid in self._order],
            "order": list(self._order),
        }

    def _legacy_item(self, item: _Backend) -> dict[str, Any]:
        return {
            "name": item.name,
            "address": item.address,
            "credential": self._encode_credential(item),
        }

    # ------------------------------------------------------------------ #
    # Canonical / spec view
    # ------------------------------------------------------------------ #

    def spec_get(self, backend_id: str | None = None) -> dict[str, Any]:
        """Return a snapshot of the canonical view.

        Without ``backend_id`` the whole collection is returned (``items`` in
        ``order``).  With it, ``items`` contains just that backend.  Unknown ids
        raise :class:`KeyError`.
        """

        with self._lock:
            snapshot = self._snapshot_all()
            if backend_id is None:
                return snapshot
            if not isinstance(backend_id, str):
                raise ValidationError("backend_id must be a string")
            item = self._items.get(backend_id)
            if item is None:
                raise KeyError(backend_id)
            return {
                "resourceVersion": self._resource_version,
                "items": [item.snapshot()],
                "order": [backend_id],
            }

    def spec_add(
        self,
        name: Any,
        address: Any,
        weight: Any = DEFAULT_WEIGHT,
        drain: Any = False,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a backend and return its canonical representation."""

        normalized_name = _normalize_name(name)
        normalized_address = _normalize_address(address)
        normalized_weight = _normalize_weight(weight)
        normalized_drain = _normalize_drain(drain)
        key = _normalize_idempotency_key(idempotency_key)
        fingerprint = self._fingerprint(
            "spec_add",
            {
                "name": normalized_name,
                "address": normalized_address,
                "weight": normalized_weight,
                "drain": normalized_drain,
            },
        )
        with self._lock:
            replayed = self._lookup_replay(key, fingerprint)
            if replayed is not None:
                return replayed
            if self._name_in_use(normalized_name):
                raise ValidationError(
                    f"backend name {normalized_name!r} is already in use"
                )
            backend_id = self._next_backend_id()
            self._resource_version += 1
            item = _Backend(
                id=backend_id,
                name=normalized_name,
                address=normalized_address,
                weight=normalized_weight,
                drain=normalized_drain,
                versions={name_: 1 for name_ in STABLE_FIELDS},
                created_resource_version=self._resource_version,
            )
            self._items[backend_id] = item
            self._order.append(backend_id)
            result = {
                "operation": "spec_add",
                "status": "created",
                "id": backend_id,
                "item": item.snapshot(),
                "resourceVersion": self._resource_version,
                "replayed": False,
            }
            self._remember(key, fingerprint, result)
            self._record(
                {"operation": "spec_add", "id": backend_id, "name": item.name},
                self._resource_version,
            )
            return result

    def spec_write(
        self,
        backend_id: str,
        changes: Mapping[str, Any],
        expected_versions: Mapping[str, int] | None = None,
        *,
        force: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Conditionally update one backend in the canonical view.

        Only fields whose value actually changes are version-checked, so a
        request that touches ``weight`` is unaffected by a concurrent change to
        ``name``.  Unless ``force=True``, every changed field needs an entry in
        ``expected_versions``.  A stale entry raises :class:`WriteConflict` with
        that field and the backend id, and the whole call is a no-op.
        """

        if not isinstance(backend_id, str) or not backend_id:
            raise ValidationError("backend_id must be a non-empty string")
        normalized_changes = _normalize_changes(changes, STABLE_FIELDS, "spec")
        expected = _normalize_expected_versions(expected_versions, STABLE_FIELDS)
        if not isinstance(force, bool):
            raise ValidationError("force must be a boolean")
        key = _normalize_idempotency_key(idempotency_key)
        fingerprint = self._fingerprint(
            "spec_write",
            {
                "id": backend_id,
                "changes": normalized_changes,
                "expected": expected,
                "force": force,
            },
        )
        with self._lock:
            replayed = self._lookup_replay(key, fingerprint)
            if replayed is not None:
                return replayed
            item = self._items.get(backend_id)
            if item is None:
                self._raise_conflicts(
                    [{"id": backend_id, "reason": "target_deleted"}],
                    self._resource_version,
                )
            assert item is not None
            actual = [
                name_
                for name_, value in normalized_changes.items()
                if getattr(item, name_) != value
            ]
            if not actual:
                result = {
                    "operation": "spec_write",
                    "status": "unchanged",
                    "id": backend_id,
                    "changed": [],
                    "item": item.snapshot(),
                    "resourceVersion": self._resource_version,
                    "replayed": False,
                }
                return self._remember(key, fingerprint, result)
            if not force:
                missing = sorted(set(actual) - set(expected))
                if missing:
                    raise ValidationError(
                        "expected_versions is required for changed field(s): "
                        + ", ".join(missing)
                    )
                stale = [
                    {"id": backend_id, "field": name_, "reason": "field_modified"}
                    for name_ in sorted(actual)
                    if expected[name_] != item.versions[name_]
                ]
                self._raise_conflicts(stale, self._resource_version)
            if "name" in actual and self._name_in_use(
                normalized_changes["name"], exclude_id=backend_id
            ):
                raise ValidationError(
                    f"backend name {normalized_changes['name']!r} is already in use"
                )
            for name_ in actual:
                setattr(item, name_, normalized_changes[name_])
                item.versions[name_] += 1
            self._resource_version += 1
            result = {
                "operation": "spec_write",
                "status": "updated",
                "id": backend_id,
                "changed": sorted(actual),
                "item": item.snapshot(),
                "resourceVersion": self._resource_version,
                "replayed": False,
            }
            self._remember(key, fingerprint, result)
            self._record(
                {
                    "operation": "spec_write",
                    "id": backend_id,
                    "changed": sorted(actual),
                },
                self._resource_version,
            )
            return result

    def spec_delete(
        self,
        backend_id: str,
        expected_versions: Mapping[str, int] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Delete one backend by stable id.

        A missing target is a ``target_deleted`` conflict.  When
        ``expected_versions`` is supplied, every listed field must still match,
        which stops a caller from deleting an item that changed after it was
        read.
        """

        if not isinstance(backend_id, str) or not backend_id:
            raise ValidationError("backend_id must be a non-empty string")
        expected = _normalize_expected_versions(expected_versions, STABLE_FIELDS)
        key = _normalize_idempotency_key(idempotency_key)
        fingerprint = self._fingerprint(
            "spec_delete", {"id": backend_id, "expected": expected}
        )
        with self._lock:
            replayed = self._lookup_replay(key, fingerprint)
            if replayed is not None:
                return replayed
            item = self._items.get(backend_id)
            if item is None:
                self._raise_conflicts(
                    [{"id": backend_id, "reason": "target_deleted"}],
                    self._resource_version,
                )
            assert item is not None
            stale = [
                {"id": backend_id, "field": name_, "reason": "field_modified"}
                for name_ in sorted(expected)
                if expected[name_] != item.versions[name_]
            ]
            self._raise_conflicts(stale, self._resource_version)
            snapshot = item.snapshot()
            del self._items[backend_id]
            self._order.remove(backend_id)
            self._resource_version += 1
            result = {
                "operation": "spec_delete",
                "status": "deleted",
                "id": backend_id,
                "item": snapshot,
                "resourceVersion": self._resource_version,
                "replayed": False,
            }
            self._remember(key, fingerprint, result)
            self._record(
                {"operation": "spec_delete", "id": backend_id, "name": snapshot["name"]},
                self._resource_version,
            )
            return result

    # ------------------------------------------------------------------ #
    # Legacy view
    # ------------------------------------------------------------------ #

    def legacy_get(self) -> dict[str, Any]:
        """Return the legacy view: names, addresses and edit credentials only."""

        with self._lock:
            return {
                "resourceVersion": self._resource_version,
                "items": [self._legacy_item(self._items[bid]) for bid in self._order],
            }

    def legacy_write(
        self,
        credential: str,
        changes: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Update the legacy-visible fields of the backend behind ``credential``.

        The credential carries the ``name``/``address`` versions the caller last
        read.  Only fields whose value actually changes are version-checked, so
        a concurrent ``weight``/``drain`` edit merges cleanly.  A stale visible
        field raises :class:`WriteConflict` and changes nothing.
        """

        token = _normalize_credential(credential)
        normalized_changes = _normalize_changes(changes, LEGACY_FIELDS, "legacy")
        key = _normalize_idempotency_key(idempotency_key)
        fingerprint = self._fingerprint(
            "legacy_write", {"credential": token, "changes": normalized_changes}
        )
        with self._lock:
            replayed = self._lookup_replay(key, fingerprint)
            if replayed is not None:
                return replayed
            decoded = self._decode_credential(token)
            item = self._items.get(decoded.backend_id)
            if item is None:
                self._raise_conflicts(
                    [{"id": decoded.backend_id, "reason": "target_deleted"}],
                    self._resource_version,
                )
            assert item is not None
            actual = [
                name_
                for name_, value in normalized_changes.items()
                if getattr(item, name_) != value
            ]
            if not actual:
                result = {
                    "operation": "legacy_write",
                    "status": "unchanged",
                    "name": item.name,
                    "address": item.address,
                    "credential": self._encode_credential(item),
                    "changed": [],
                    "resourceVersion": self._resource_version,
                    "replayed": False,
                }
                return self._remember(key, fingerprint, result)
            stale = [
                {"id": decoded.backend_id, "field": name_, "reason": "field_modified"}
                for name_ in sorted(actual)
                if decoded.versions[name_] != item.versions[name_]
            ]
            self._raise_conflicts(stale, self._resource_version)
            if "name" in actual and self._name_in_use(
                normalized_changes["name"], exclude_id=decoded.backend_id
            ):
                raise ValidationError(
                    f"backend name {normalized_changes['name']!r} is already in use"
                )
            previous_name = item.name
            for name_ in actual:
                setattr(item, name_, normalized_changes[name_])
                item.versions[name_] += 1
            self._resource_version += 1
            result = {
                "operation": "legacy_write",
                "status": "renamed" if "name" in actual else "updated",
                "name": item.name,
                "address": item.address,
                "credential": self._encode_credential(item),
                "changed": sorted(actual),
                "resourceVersion": self._resource_version,
                "replayed": False,
            }
            if previous_name != item.name:
                result["previousName"] = previous_name
            self._remember(key, fingerprint, result)
            record = {
                "operation": "legacy_write",
                "id": item.id,
                "changed": sorted(actual),
            }
            if previous_name != item.name:
                record["previousName"] = previous_name
                record["name"] = item.name
            self._record(record, self._resource_version)
            return result

    def legacy_delete(
        self,
        credential: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Delete the backend behind ``credential``.

        Identity comes from the signed credential rather than a name, so a
        rename cannot redirect the deletion.  A missing target is a conflict;
        replaying the same request id returns the original success instead.
        """

        token = _normalize_credential(credential)
        key = _normalize_idempotency_key(idempotency_key)
        fingerprint = self._fingerprint("legacy_delete", {"credential": token})
        with self._lock:
            replayed = self._lookup_replay(key, fingerprint)
            if replayed is not None:
                return replayed
            decoded = self._decode_credential(token)
            item = self._items.get(decoded.backend_id)
            if item is None:
                self._raise_conflicts(
                    [{"id": decoded.backend_id, "reason": "target_deleted"}],
                    self._resource_version,
                )
            assert item is not None
            name = item.name
            del self._items[item.id]
            self._order.remove(item.id)
            self._resource_version += 1
            result = {
                "operation": "legacy_delete",
                "status": "deleted",
                "name": name,
                "credential": token,
                "resourceVersion": self._resource_version,
                "replayed": False,
            }
            self._remember(key, fingerprint, result)
            self._record(
                {"operation": "legacy_delete", "id": decoded.backend_id, "name": name},
                self._resource_version,
            )
            return result

    def legacy_commit(
        self,
        items: Iterable[Mapping[str, Any]],
        deleted_credentials: Iterable[str] = (),
        *,
        replace: bool = True,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply a legacy list in one atomic transaction.

        Each item is ``{name, address, credential?}``.  A credential marks an
        existing backend (rename and/or address edit); an item without one is a
        creation.  Credentials in ``deleted_credentials`` are explicit
        deletions.

        ``replace=True`` (default) treats the submission as the whole list: it
        fixes collection order, creates new items in place, and deletes every
        existing backend that is not listed.  ``replace=False`` merges: existing
        order is preserved, new items are appended, and unlisted backends are
        left untouched.

        Every conflict -- stale visible field, missing target, duplicate final
        name, repeated credential -- is detected before any mutation, so a batch
        either applies completely or not at all.
        """

        raw_items = _as_list(items, "items")
        parsed: list[_CommitItem] = []
        seen_credentials: set[str] = set()
        for index, raw in enumerate(raw_items):
            mapping = _require_mapping(raw, f"items[{index}]")
            unknown = set(mapping) - {"credential", "name", "address"}
            if unknown:
                raise ValidationError(
                    f"items[{index}] has fields not visible to the legacy version: "
                    + ", ".join(sorted(str(name_) for name_ in unknown))
                )
            if "name" not in mapping or "address" not in mapping:
                raise ValidationError(
                    f"items[{index}] must contain both 'name' and 'address'"
                )
            credential = mapping.get("credential")
            if credential is not None:
                credential = _normalize_credential(
                    credential, f"items[{index}].credential"
                )
                if credential in seen_credentials:
                    raise ValidationError(
                        f"items[{index}] repeats a credential from an earlier item"
                    )
                seen_credentials.add(credential)
            parsed.append(
                _CommitItem(
                    index=index,
                    credential=credential,
                    name=_normalize_name(mapping["name"]),
                    address=_normalize_address(mapping["address"]),
                )
            )

        raw_deleted = _as_list(deleted_credentials, "deleted_credentials")
        deleted_tokens: list[str] = []
        seen_deleted_tokens: set[str] = set()
        for index, token in enumerate(raw_deleted):
            normalized = _normalize_credential(
                token, f"deleted_credentials[{index}]"
            )
            if normalized in seen_deleted_tokens:
                raise ValidationError(
                    f"deleted_credentials[{index}] is a duplicate"
                )
            seen_deleted_tokens.add(normalized)
            deleted_tokens.append(normalized)

        if not isinstance(replace, bool):
            raise ValidationError("replace must be a boolean")
        key = _normalize_idempotency_key(idempotency_key)
        fingerprint = self._fingerprint(
            "legacy_commit",
            {
                "items": [
                    {
                        "credential": item.credential,
                        "name": item.name,
                        "address": item.address,
                    }
                    for item in parsed
                ],
                "deleted": deleted_tokens,
                "replace": replace,
            },
        )
        with self._lock:
            replayed = self._lookup_replay(key, fingerprint)
            if replayed is not None:
                return replayed

            # 1. Decode credentials and reject structurally ambiguous payloads.
            id_positions: dict[str, int] = {}
            for item in parsed:
                if item.credential is None:
                    continue
                item.decoded = self._decode_credential(item.credential)
                item.assigned_id = item.decoded.backend_id
                if item.assigned_id in id_positions:
                    raise ValidationError(
                        "the same backend appears more than once in items"
                    )
                id_positions[item.assigned_id] = item.index

            decoded_deleted: list[tuple[str, _DecodedCredential]] = []
            deleted_ids: set[str] = set()
            for token in deleted_tokens:
                decoded = self._decode_credential(token)
                if decoded.backend_id in deleted_ids:
                    raise ValidationError(
                        "the same backend appears more than once in deleted_credentials"
                    )
                if decoded.backend_id in id_positions:
                    raise ValidationError(
                        "a backend cannot be both submitted and deleted"
                    )
                deleted_ids.add(decoded.backend_id)
                decoded_deleted.append((token, decoded))

            # 2. Optimistic-concurrency checks against the current state.
            conflicts: list[dict[str, Any]] = []
            for item in parsed:
                if item.decoded is None:
                    continue
                current = self._items.get(item.decoded.backend_id)
                if current is None:
                    conflicts.append(
                        {"id": item.decoded.backend_id, "reason": "target_deleted"}
                    )
                    continue
                if (
                    item.name != current.name
                    and item.decoded.versions["name"] != current.versions["name"]
                ):
                    conflicts.append(
                        {
                            "id": current.id,
                            "field": "name",
                            "reason": "field_modified",
                        }
                    )
                if (
                    item.address != current.address
                    and item.decoded.versions["address"] != current.versions["address"]
                ):
                    conflicts.append(
                        {
                            "id": current.id,
                            "field": "address",
                            "reason": "field_modified",
                        }
                    )
            for _, decoded in decoded_deleted:
                if decoded.backend_id not in self._items:
                    conflicts.append(
                        {"id": decoded.backend_id, "reason": "target_deleted"}
                    )
            self._raise_conflicts(conflicts, self._resource_version)

            # 3. Classify submitted items and plan membership.
            submitted_ids = {
                item.decoded.backend_id
                for item in parsed
                if item.decoded is not None
            }
            if replace:
                pruned_ids = [
                    bid
                    for bid in self._order
                    if bid not in submitted_ids and bid not in deleted_ids
                ]
            else:
                pruned_ids = []
            disappearing = set(deleted_ids) | set(pruned_ids)

            for item in parsed:
                if item.decoded is None:
                    item.status = "created"
                    item.changed = list(LEGACY_FIELDS)
                    continue
                current = self._items[item.decoded.backend_id]
                item.previous_name = current.name
                item.changed = [
                    name_
                    for name_ in LEGACY_FIELDS
                    if getattr(current, name_) != getattr(item, name_)
                ]
                if "name" in item.changed:
                    item.status = "renamed"
                elif item.changed:
                    item.status = "updated"
                else:
                    item.status = "unchanged"

            # 4. Validate the resulting name set before touching anything.
            final_name_counts: dict[str, int] = {}
            for item in parsed:
                final_name_counts[item.name] = final_name_counts.get(item.name, 0) + 1
            if not replace:
                for bid in self._order:
                    if bid in submitted_ids or bid in disappearing:
                        continue
                    existing_name = self._items[bid].name
                    final_name_counts[existing_name] = (
                        final_name_counts.get(existing_name, 0) + 1
                    )
            duplicates = sorted(
                name_ for name_, count in final_name_counts.items() if count > 1
            )
            if duplicates:
                raise ValidationError(
                    "final backend names must be unique; duplicate(s): "
                    + ", ".join(repr(name_) for name_ in duplicates)
                )

            # 5. Pre-assign ids for creations and compute the new order.
            for item in parsed:
                if item.decoded is None:
                    item.assigned_id = self._next_backend_id()
            new_creations = [
                item.assigned_id
                for item in parsed
                if item.decoded is None and item.assigned_id is not None
            ]
            if replace:
                new_order = [
                    item.assigned_id for item in parsed if item.assigned_id is not None
                ]
                # Baseline: previous relative order of the surviving submitted
                # items, with creations appended.
                baseline_order = [bid for bid in self._order if bid in submitted_ids]
                baseline_order.extend(new_creations)
            else:
                new_order = [
                    bid for bid in self._order if bid not in disappearing
                ]
                new_order.extend(new_creations)
                # Baseline: previous order minus disappearing items, creations
                # appended.  Submitted existing items keep their positions.
                baseline_order = [
                    bid for bid in self._order if bid not in disappearing
                ]
                baseline_order.extend(new_creations)
            assert len(new_order) == len(set(new_order))
            order_changed = new_order != baseline_order

            has_item_change = any(item.status != "unchanged" for item in parsed)
            needs_mutation = bool(has_item_change or disappearing or order_changed)

            explicit_snapshots = [
                (token, self._items[decoded.backend_id].name, token)
                for token, decoded in decoded_deleted
            ]
            pruned_snapshots = [
                (self._items[bid].name, self._encode_credential(self._items[bid]))
                for bid in pruned_ids
            ]

            if not needs_mutation:
                result = self._commit_result(
                    parsed,
                    deleted_entries=[],
                    replace=replace,
                    changed=False,
                    order_changed=False,
                    resource_version=self._resource_version,
                )
                return self._remember(key, fingerprint, result)

            # 6. Apply atomically (all checks already passed).
            self._resource_version += 1
            new_resource_version = self._resource_version
            for item in parsed:
                if item.decoded is None or not item.changed:
                    continue
                current = self._items[item.decoded.backend_id]
                for name_ in item.changed:
                    setattr(current, name_, getattr(item, name_))
                    current.versions[name_] += 1
            for item in parsed:
                if item.decoded is not None or item.assigned_id is None:
                    continue
                self._items[item.assigned_id] = _Backend(
                    id=item.assigned_id,
                    name=item.name,
                    address=item.address,
                    weight=DEFAULT_WEIGHT,
                    drain=False,
                    versions={name_: 1 for name_ in STABLE_FIELDS},
                    created_resource_version=new_resource_version,
                )
            for bid in disappearing:
                self._items.pop(bid, None)
            self._order = new_order

            deleted_entries: list[dict[str, Any]] = [
                {
                    "status": "deleted",
                    "name": name_,
                    "credential": token,
                    "cause": "explicit",
                }
                for token, name_, _ in explicit_snapshots
            ]
            deleted_entries.extend(
                {
                    "status": "deleted",
                    "name": name_,
                    "credential": credential,
                    "cause": "pruned",
                }
                for name_, credential in pruned_snapshots
            )

            result = self._commit_result(
                parsed,
                deleted_entries=deleted_entries,
                replace=replace,
                changed=True,
                order_changed=order_changed,
                resource_version=new_resource_version,
            )
            self._remember(key, fingerprint, result)
            self._record(
                {
                    "operation": "legacy_commit",
                    "replace": replace,
                    "orderChanged": order_changed,
                    "items": [
                        {
                            "status": item.status,
                            "name": item.name,
                            **(
                                {"previousName": item.previous_name}
                                if item.previous_name
                                and item.previous_name != item.name
                                else {}
                            ),
                        }
                        for item in parsed
                    ],
                    "deleted": [
                        {"name": entry["name"], "cause": entry["cause"]}
                        for entry in deleted_entries
                    ],
                },
                new_resource_version,
            )
            return result

    def _commit_result(
        self,
        parsed: list[_CommitItem],
        *,
        deleted_entries: list[dict[str, Any]],
        replace: bool,
        changed: bool,
        order_changed: bool,
        resource_version: int,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for item in parsed:
            if item.status == "created":
                assert item.assigned_id is not None
                backend = self._items[item.assigned_id]
                results.append(
                    {
                        "status": "created",
                        "name": backend.name,
                        "address": backend.address,
                        "credential": self._encode_credential(backend),
                        "changed": list(LEGACY_FIELDS),
                    }
                )
                continue
            assert item.decoded is not None
            backend = self._items[item.decoded.backend_id]
            entry = {
                "status": item.status,
                "name": backend.name,
                "address": backend.address,
                "credential": self._encode_credential(backend),
                "changed": list(item.changed),
            }
            if item.previous_name is not None and item.previous_name != backend.name:
                entry["previousName"] = item.previous_name
            results.append(entry)

        summary = {
            "created": sum(1 for item in parsed if item.status == "created"),
            "renamed": sum(1 for item in parsed if item.status == "renamed"),
            "updated": sum(1 for item in parsed if item.status == "updated"),
            "unchanged": sum(1 for item in parsed if item.status == "unchanged"),
            "deletedExplicit": sum(
                1 for entry in deleted_entries if entry["cause"] == "explicit"
            ),
            "deletedPruned": sum(
                1 for entry in deleted_entries if entry["cause"] == "pruned"
            ),
            "totalItems": len(results),
            "totalDeleted": len(deleted_entries),
        }
        return {
            "operation": "legacy_commit",
            "status": "applied" if changed else "unchanged",
            "replace": replace,
            "changed": changed,
            "orderChanged": order_changed,
            "items": results,
            "deleted": deleted_entries,
            "summary": summary,
            "resourceVersion": resource_version,
            "replayed": False,
        }

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def history(self, since_resource_version: int | None = None) -> list[dict[str, Any]]:
        """Return applied mutations oldest-first (replays are not recorded)."""

        if since_resource_version is not None and (
            isinstance(since_resource_version, bool)
            or not isinstance(since_resource_version, int)
            or since_resource_version < 0
        ):
            raise ValidationError("since_resource_version must be a non-negative integer")
        with self._lock:
            entries = [ _deep_copy(entry) for entry in self._history ]
            if since_resource_version is None:
                return entries
            return [
                entry
                for entry in entries
                if entry["resourceVersion"] > since_resource_version
            ]

    @property
    def resource_version(self) -> int:
        with self._lock:
            return self._resource_version
