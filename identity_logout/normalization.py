"""Normalization helpers for plain dictionaries and dataclass inputs."""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from typing import Any, Iterable

from .errors import InvalidEventError
from .model import Event, EventKind, RevocationBarrier, Watermark


_KIND_ALIASES = {
    "derive": EventKind.DERIVATION,
    "derived": EventKind.DERIVATION,
    "derivation": EventKind.DERIVATION,
    "session_derived": EventKind.DERIVATION,
    "spawn": EventKind.DERIVATION,
    "spawned": EventKind.DERIVATION,
    "create": EventKind.DERIVATION,
    "created": EventKind.DERIVATION,
    "transfer": EventKind.TRANSFER,
    "transferred": EventKind.TRANSFER,
    "transfer_sent": EventKind.TRANSFER,
    "handoff": EventKind.TRANSFER,
    "handover": EventKind.TRANSFER,
    "send": EventKind.TRANSFER,
    "sent": EventKind.TRANSFER,
    "transfer_receipt": EventKind.TRANSFER_RECEIPT,
    "receipt": EventKind.TRANSFER_RECEIPT,
    "ack": EventKind.TRANSFER_RECEIPT,
    "acked": EventKind.TRANSFER_RECEIPT,
    "acknowledgement": EventKind.TRANSFER_RECEIPT,
    "acknowledgment": EventKind.TRANSFER_RECEIPT,
    "transfer_ack": EventKind.TRANSFER_RECEIPT,
    "transfer_acknowledgement": EventKind.TRANSFER_RECEIPT,
    "received": EventKind.TRANSFER_RECEIPT,
    "signed_for": EventKind.TRANSFER_RECEIPT,
    "signoff": EventKind.TRANSFER_RECEIPT,
    "close": EventKind.CLOSE,
    "closed": EventKind.CLOSE,
    "closure": EventKind.CLOSE,
    "session_closed": EventKind.CLOSE,
    "logout": EventKind.CLOSE,
}


def _pick(data: MappingABC[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _as_nonempty_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_sequence(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidEventError(f"{field_name} must be an integer, not boolean")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidEventError(f"{field_name} must be an integer") from exc
    if number < 0:
        raise InvalidEventError(f"{field_name} must be non-negative")
    return number


def normalize_kind(value: Any) -> EventKind:
    if isinstance(value, EventKind):
        return value
    if value is None:
        raise InvalidEventError("event kind/type is required")
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _KIND_ALIASES:
        raise InvalidEventError(f"unsupported event kind: {value!r}")
    return _KIND_ALIASES[key]


def event_from_obj(obj: Event | MappingABC[str, Any]) -> Event:
    """Coerce an :class:`Event` or flexible dictionary into an Event."""

    if isinstance(obj, Event):
        return obj
    if not isinstance(obj, MappingABC):
        raise InvalidEventError(
            f"event must be Event or mapping, got {type(obj).__name__}"
        )

    event_id = _as_nonempty_str(_pick(obj, "event_id", "id", "sequence_id", "seq_id"))
    if event_id is None:
        raise InvalidEventError("event_id/id is required")

    kind = normalize_kind(_pick(obj, "kind", "type", "event_type", "event_kind"))

    session_id = _as_nonempty_str(
        _pick(obj, "session_id", "session", "sid")
    )
    child_session_id = _as_nonempty_str(
        _pick(
            obj,
            "child_session_id",
            "child_id",
            "child",
            "derived_session_id",
            "new_session_id",
        )
    )
    parent_session_id = _as_nonempty_str(
        _pick(obj, "parent_session_id", "parent_id", "parent", "ancestor_session_id")
    )

    if kind is EventKind.DERIVATION:
        child_session_id = child_session_id or session_id
        session_id = child_session_id
    elif session_id is None:
        session_id = child_session_id

    from_endpoint = _as_nonempty_str(
        _pick(
            obj,
            "from_endpoint",
            "source_endpoint",
            "from",
            "source",
            "from_dc",
            "from_region",
            "source_dc",
        )
    )
    to_endpoint = _as_nonempty_str(
        _pick(
            obj,
            "to_endpoint",
            "target_endpoint",
            "destination_endpoint",
            "destination",
            "target",
            "to_dc",
            "to_region",
            "target_dc",
        )
    )
    issuer = _as_nonempty_str(
        _pick(
            obj,
            "issuer",
            "issuer_endpoint",
            "signer",
            "signing_endpoint",
            "endpoint",
            "dc",
            "region",
        )
    )
    if issuer is None:
        if kind is EventKind.TRANSFER:
            issuer = from_endpoint
        elif kind is EventKind.TRANSFER_RECEIPT:
            issuer = _as_nonempty_str(
                _pick(obj, "ack_endpoint", "acked_by", "acknowledged_by", "receiver")
            ) or to_endpoint
        elif kind is EventKind.CLOSE:
            issuer = to_endpoint or from_endpoint
    if issuer is None:
        issuer = from_endpoint

    sequence = _as_sequence(_pick(obj, "sequence", "seq", "offset", "event_sequence"), "sequence")
    predecessor_raw = _pick(
        obj,
        "predecessor_id",
        "causal_predecessor_id",
        "causal_predecessor",
        "predecessor",
        "prev_event_id",
        "prev_id",
    )
    # Numeric causal logs conventionally use 0 for "no predecessor".
    if predecessor_raw in (0, "0", "none", "null", "None", "NULL"):
        predecessor_id = None
    else:
        predecessor_id = _as_nonempty_str(predecessor_raw)
    predecessor_endpoint = _as_nonempty_str(
        _pick(
            obj,
            "predecessor_endpoint",
            "predecessor_issuer",
            "causal_predecessor_endpoint",
        )
    )
    transfer_id = _as_nonempty_str(_pick(obj, "transfer_id", "handoff_id", "handover_id"))
    receipt_id = _as_nonempty_str(_pick(obj, "receipt_id", "ack_id", "ack_receipt_id"))
    acknowledges = _as_nonempty_str(
        _pick(
            obj,
            "acknowledges_transfer_id",
            "ack_transfer_id",
            "ack_for_transfer",
            "ack_for",
        )
    )

    payload = _pick(obj, "payload", "data", "metadata") or {}
    if not isinstance(payload, MappingABC):
        payload = {"value": payload}

    return Event(
        event_id=event_id,
        kind=kind,
        session_id=session_id,
        issuer=issuer,
        sequence=sequence,
        predecessor_id=predecessor_id,
        predecessor_endpoint=predecessor_endpoint,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        transfer_id=transfer_id,
        from_endpoint=from_endpoint,
        to_endpoint=to_endpoint,
        receipt_id=receipt_id,
        acknowledges_transfer_id=acknowledges,
        payload=dict(payload),
    )


def watermarks_from_obj(
    obj: MappingABC[str, int | Watermark | MappingABC[str, Any]]
    | Iterable[Watermark | MappingABC[str, Any] | tuple[str, int]]
    | None,
) -> dict[str, int]:
    """Accept a mapping, iterable of Watermark/dicts, or ``(endpoint, seq)``."""

    if obj is None:
        return {}

    result: dict[str, int] = {}

    def add(endpoint: Any, sequence: Any) -> None:
        ep = _as_nonempty_str(endpoint)
        if ep is None:
            raise InvalidEventError("watermark endpoint must not be empty")
        seq = _as_sequence(sequence, "watermark sequence")
        if seq is None:
            raise InvalidEventError("watermark sequence is required")
        result[ep] = max(result.get(ep, 0), seq)

    if isinstance(obj, MappingABC):
        for endpoint, value in obj.items():
            if isinstance(value, Watermark):
                add(value.endpoint, value.sequence)
            elif isinstance(value, MappingABC):
                add(
                    _pick(value, "endpoint", "issuer", "dc", "name") or endpoint,
                    _pick(value, "sequence", "watermark", "seq", "offset"),
                )
            else:
                add(endpoint, value)
        return result

    for item in obj:
        if isinstance(item, Watermark):
            add(item.endpoint, item.sequence)
        elif isinstance(item, MappingABC):
            add(
                _pick(item, "endpoint", "issuer", "dc", "name"),
                _pick(item, "sequence", "watermark", "seq", "offset"),
            )
        elif isinstance(item, tuple) and len(item) == 2:
            add(item[0], item[1])
        else:
            raise InvalidEventError(f"unsupported watermark value: {item!r}")
    return result


def barrier_from_obj(
    barrier: int | MappingABC[str, int] | RevocationBarrier | None,
    root_session_id: str | None = None,
) -> RevocationBarrier | None:
    """Coerce common barrier arguments into :class:`RevocationBarrier`."""

    if barrier is None:
        return None
    if isinstance(barrier, RevocationBarrier):
        if barrier.root_session_id is None and root_session_id is not None:
            return RevocationBarrier(
                root_session_id=root_session_id,
                positions=dict(barrier.positions),
                default_sequence=barrier.default_sequence,
                barrier_id=barrier.barrier_id,
            )
        return barrier
    if isinstance(barrier, MappingABC):
        positions = watermarks_from_obj(barrier)  # same int validation/max semantics
        return RevocationBarrier(root_session_id=root_session_id, positions=positions)
    if isinstance(barrier, bool):
        raise InvalidEventError("barrier sequence must be an integer, not boolean")
    try:
        default_sequence = int(barrier)
    except (TypeError, ValueError) as exc:
        raise InvalidEventError("barrier must be an int, mapping, or RevocationBarrier") from exc
    if default_sequence < 0:
        raise InvalidEventError("barrier sequence must be non-negative")
    return RevocationBarrier(
        root_session_id=root_session_id,
        default_sequence=default_sequence,
    )
