"""Construction helpers for Python mappings and JSON-style payloads."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .models import (
    Approval,
    ApprovalKind,
    AuthorizationRequest,
    Identity,
    Interval,
    Scope,
    normalize_kind,
)


def parse_temporal(value: Any) -> Any:
    """Parse ISO dates/times while leaving ordered numeric endpoints intact."""

    if isinstance(value, (date, datetime, int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 10:
            return date.fromisoformat(text)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    raise TypeError(f"unsupported interval endpoint: {value!r}")


def interval_from_mapping(data: dict[str, Any]) -> Interval[Any]:
    """Build an interval from common start/end key names.

    Date-only values supplied as ``valid_to``/``end_date`` are treated as
    inclusive calendar dates and converted to the following midnight. All other
    intervals are half-open ``[start, end)``.
    """

    start_value = _first(
        data,
        "start",
        "valid_from",
        "start_at",
        "not_before",
        "requested_from",
    )
    if start_value is None:
        raise ValueError("interval requires a start (for example valid_from/start)")
    start = parse_temporal(start_value)

    exclusive_end_value = _first(
        data,
        "end",
        "valid_until",
        "end_at",
        "end_exclusive",
        "exclusive_end",
        "not_after",
        "requested_until",
    )
    inclusive_end_value = _first(data, "valid_to", "end_date", "inclusive_end")

    if exclusive_end_value is not None:
        end = parse_temporal(exclusive_end_value)
    elif inclusive_end_value is not None:
        end = parse_temporal(inclusive_end_value)
        if isinstance(end, datetime):
            if data.get("end_inclusive") is True:
                # JSON/Python has no universal chronon. Explicit inclusive
                # datetimes are represented through the next microsecond.
                end = end + timedelta(microseconds=1)
        elif isinstance(end, date):
            end = end + timedelta(days=1)
    else:
        raise ValueError("interval requires an end (for example valid_until/end)")

    return Interval(start, end)


def build_scope(data: dict[str, Any]) -> Scope:
    """Extract a :class:`Scope` from an approval/request mapping."""

    raw_scope = data.get("scope")
    if isinstance(raw_scope, Scope):
        return raw_scope

    scope_data = dict(raw_scope) if isinstance(raw_scope, dict) else {}

    project = _first(
        data,
        "project_ids",
        "projects",
        "project_id",
        "project",
    )
    if project is None:
        project = _first(scope_data, "project_ids", "projects", "project_id", "project")

    service = _first(
        data,
        "service_ids",
        "services",
        "target_service",
        "service_id",
        "service",
        "service_account_id",
        "service_account",
    )
    if service is None:
        service = _first(
            scope_data,
            "service_ids",
            "services",
            "service_id",
            "service",
        )

    jurisdiction = _first(
        data,
        "jurisdiction_ids",
        "jurisdictions",
        "jurisdiction",
    )
    if jurisdiction is None:
        jurisdiction = _first(
            scope_data,
            "jurisdiction_ids",
            "jurisdictions",
            "jurisdiction",
        )

    return Scope.of(project=project, service=service, jurisdiction=jurisdiction)


def build_approval(value: Approval | dict[str, Any]) -> Approval:
    """Coerce a mapping into an :class:`Approval`."""

    if isinstance(value, Approval):
        return value
    if not isinstance(value, dict):
        raise TypeError("approval must be an Approval or dict")

    approval_id = str(
        _require_first(value, "approval_id", "id", "approvalId", "uuid")
    )
    kind = normalize_kind(
        _require_first(value, "kind", "type", "approval_type", "category")
    )

    person_id = _first(value, "person_id", "natural_person_id", "canonical_person_id")

    identity_value = _first(
        value, "identity", "approver", "principal", "subject", "actor"
    )
    if identity_value is None:
        directory = _first(value, "directory", "identity_directory", "issuer")
        subject = _first(
            value,
            "subject",
            "subject_id",
            "username",
            "email",
            "approver_id",
            "principal_id",
        )
        if directory is not None and subject is not None:
            identity = Identity(str(directory), str(subject))
        elif subject is not None:
            identity = Identity.parse(str(subject))
        elif person_id is not None:
            # A canonical person identifier alone is sufficient when callers do
            # not need to model a directory-specific login name.
            identity = Identity("natural-person", str(person_id))
        else:
            raise ValueError(f"approval {approval_id!r} has no approver identity")
    else:
        identity = Identity.parse(identity_value)

    interval_data = value.get("validity") or value.get("interval") or value
    if isinstance(interval_data, Interval):
        interval = interval_data
    elif isinstance(interval_data, dict):
        interval = interval_from_mapping(interval_data)
    else:
        interval = interval_from_mapping(value)

    scope = build_scope(value)

    details = value.get("details")
    if details is not None and not isinstance(details, dict):
        details = {"value": details}

    return Approval(
        approval_id=approval_id,
        kind=kind,
        identity=identity,
        interval=interval,
        person_id=None if person_id is None else str(person_id),
        scope=scope,
        details=details,
    )


def build_request(
    payload: AuthorizationRequest | dict[str, Any] | None = None,
    *,
    target_service: str | None = None,
    service_id: str | None = None,
    project_id: str | None = None,
    project: str | None = None,
    interval: Interval[Any] | tuple[Any, Any] | dict[str, Any] | None = None,
    start: Any = None,
    end: Any = None,
    jurisdiction_id: str | None = None,
) -> AuthorizationRequest:
    """Build an :class:`AuthorizationRequest` from flexible arguments."""

    data: dict[str, Any] = {}
    if isinstance(payload, AuthorizationRequest):
        return payload
    if payload is not None:
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict or AuthorizationRequest")
        data.update(payload)

    service = (
        target_service
        or service_id
        or data.get("target_service")
        or data.get("service_id")
        or data.get("service")
        or data.get("service_account_id")
    )
    proj = project_id or project or data.get("project_id") or data.get("project")
    jurisdiction = (
        jurisdiction_id
        or data.get("jurisdiction_id")
        or data.get("jurisdiction")
    )

    request_interval = _build_interval_argument(
        interval=interval,
        start=start if start is not None else data.get("start"),
        end=end if end is not None else data.get("end"),
        data=data,
    )

    if service is None:
        raise ValueError("target service is required")
    if proj is None:
        raise ValueError("project is required")

    return AuthorizationRequest(
        service_id=str(service),
        project_id=str(proj),
        interval=request_interval,
        jurisdiction_id=None if jurisdiction is None else str(jurisdiction),
    )


def _build_interval_argument(
    *,
    interval: Interval[Any] | tuple[Any, Any] | dict[str, Any] | None,
    start: Any,
    end: Any,
    data: dict[str, Any],
) -> Interval[Any]:
    if isinstance(interval, Interval):
        return interval
    if isinstance(interval, tuple) and len(interval) == 2:
        return Interval(parse_temporal(interval[0]), parse_temporal(interval[1]))
    if isinstance(interval, dict):
        return interval_from_mapping(interval)
    if start is not None and end is not None:
        return Interval(parse_temporal(start), parse_temporal(end))

    nested = data.get("requested_interval") or data.get("interval") or data.get("validity")
    if isinstance(nested, Interval):
        return nested
    if isinstance(nested, dict):
        return interval_from_mapping(nested)
    if isinstance(nested, tuple) and len(nested) == 2:
        return Interval(parse_temporal(nested[0]), parse_temporal(nested[1]))
    if data.get("start") is not None and (data.get("end") is not None or data.get("valid_until")):
        return interval_from_mapping(data)
    raise ValueError("requested interval is required")


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _require_first(data: dict[str, Any], *keys: str) -> Any:
    value = _first(data, *keys)
    if value is None:
        raise ValueError(f"missing required field; tried {', '.join(keys)}")
    return value
