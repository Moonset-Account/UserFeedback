"""Normalization for device registry, layout, policy, and receipt records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import AuditDataError
from .intervals import Interval, coerce_interval
from .timeutils import parse_time


class IssueBook:
    """Collects non-fatal validation findings for a batch audit."""

    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []

    def add(self, severity: str, code: str, message: str, **context: Any) -> None:
        issue = {"severity": severity, "code": code, "message": message}
        issue.update(context)
        self.issues.append(issue)

    def error(self, code: str, message: str, **context: Any) -> None:
        self.add("error", code, message, **context)

    def warning(self, code: str, message: str, **context: Any) -> None:
        self.add("warning", code, message, **context)

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [item for item in self.issues if item["severity"] == "error"]

    def for_context(self, **context: Any) -> list[dict[str, Any]]:
        return [
            item
            for item in self.issues
            if all(item.get(key) == value for key, value in context.items())
        ]


def normalized_key(key: Any) -> str:
    return str(key).strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def pick(record: Any, *names: str, default: Any = None) -> Any:
    """Pick a field using snake_case/camelCase/hyphen-insensitive lookup."""

    if not isinstance(record, Mapping):
        return default

    for name in names:
        if name in record and record[name] is not None:
            return record[name]

    folded = {normalized_key(key): value for key, value in record.items()}
    for name in names:
        key = normalized_key(name)
        if key in folded and folded[key] is not None:
            return folded[key]
    return default


def identity_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_INSTANCE_KEYS = (
    "volume_instance_id",
    "volume_instance_number",
    "volume_instance_no",
    "instance_id",
    "instance_number",
    "instance_no",
    "volume_id",
    "volume_uuid",
    "volume_guid",
    "disk_volume_id",
    "volume_instance",
)


def extract_instance_id(record: Mapping[str, Any]) -> str | None:
    value = pick(record, *_INSTANCE_KEYS)
    if isinstance(value, Mapping):
        value = pick(
            value,
            "volume_instance_id",
            "instance_id",
            "id",
            "number",
            "volume_instance_number",
            "uuid",
            "guid",
        )
    return identity_text(value)


@dataclass
class DeviceRecord:
    device_id: str
    index: int
    handoff_at: Any = None
    layout_version: Any = None
    aliases: set[str] = field(default_factory=set)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def matches_device(self, candidate: Any) -> bool:
        candidate_id = identity_text(candidate)
        if candidate_id is None:
            return True
        return candidate_id in self.aliases or candidate_id == self.device_id


@dataclass
class PartitionRecord:
    device_id: str
    layout_version: Any
    name: str | None
    instance_id: str
    interval: Interval | None
    unit: str | None = None
    mount_point: str | None = None
    required: bool | None = None
    policy: Mapping[str, Any] | None = None
    role: str | None = None
    index: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class LayoutRecord:
    device_id: str
    version: Any
    valid_from: Any = None
    valid_to: Any = None
    partitions: list[PartitionRecord] = field(default_factory=list)
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ReceiptRecord:
    receipt_id: str
    index: int
    device_id: str | None
    volume_instance_id: str | None
    intervals: list[Interval]
    confirmed: bool
    status: str | None
    effective_at: Any
    available_at: Any
    unit: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class PolicyRecord:
    policy_id: str | None
    device_id: str | None
    layout_version: Any
    instance_id: str | None
    volume_name: str | None
    required: bool
    enabled_at: Any = None
    raw: Mapping[str, Any] = field(default_factory=dict)


def as_record_list(value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        # A mapping may itself be one record, or a collection keyed by id.
        if any(
            pick(value, key) is not None
            for key in ("devices", "device_registry", "layouts", "receipts", "partitions")
        ) and not any(
            pick(value, "device_id", "id", "volume_instance_id", "receipt_id", "start", "end")
            is not None
            for key in [0]
        ):
            for collection_key in (
                "devices",
                "device_registry",
                "registry",
                "assets",
                "laptops",
                "layouts",
                "layout_versions",
                "partition_layouts",
                "receipts",
                "encryption_receipts",
                "confirmations",
                "policies",
            ):
                nested = value.get(collection_key)
                if isinstance(nested, (list, Mapping)):
                    return as_record_list(nested)
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    raise TypeError("expected a list of records or a mapping")


def normalize_devices(value: Any, issues: IssueBook) -> list[DeviceRecord]:
    if value is None:
        return []

    records: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        # Registry may be {device_id: {...}}.
        looks_like_record = pick(value, "device_id", "id", "asset_id", "serial_number") is not None
        collection = pick(value, "devices", "device_registry", "registry", "assets", "laptops")
        if collection is not None and not looks_like_record:
            records = as_record_list(collection)
        else:
            for key, item in value.items():
                if isinstance(item, Mapping):
                    merged = dict(item)
                    merged.setdefault("device_id", key)
                    merged.setdefault("id", key)
                    records.append(merged)
    elif isinstance(value, list):
        records = [item for item in value if isinstance(item, Mapping)]
    else:
        raise TypeError("devices must be a list, mapping, or registry document")

    devices: list[DeviceRecord] = []
    alias_to_canonical: dict[str, str] = {}

    for index, raw in enumerate(records):
        device_id = identity_text(
            pick(raw, "device_id", "id", "asset_id", "asset_tag", "machine_id", "laptop_id")
        )
        if device_id is None:
            device_id = f"device-{index + 1:04d}"
            issues.error("missing_device_id", "Device record is missing a device_id", index=index)

        nested_handoff = pick(raw, "handoff", "handover", "seal", "sealing")
        handoff_value = pick(
            raw,
            "handoff_at",
            "handover_at",
            "sealed_at",
            "seal_time",
            "handoff_time",
            "handover_time",
            "audit_at",
            "as_of",
            "cutoff",
        )
        if handoff_value is None and isinstance(nested_handoff, Mapping):
            handoff_value = pick(nested_handoff, "at", "time", "timestamp", "sealed_at", "handoff_at")

        try:
            handoff_at = parse_time(handoff_value, field=f"device {device_id} handoff_at")
        except Exception as exc:  # validation captured as issue
            issues.error("invalid_handoff_time", str(exc), device_id=device_id)
            handoff_at = None

        layout_version = pick(
            raw,
            "layout_version",
            "current_layout_version",
            "active_layout_version",
            "sealed_layout_version",
            "partition_layout_version",
        )

        aliases = {device_id}
        for field_name in (
            "device_id",
            "id",
            "asset_id",
            "asset_tag",
            "serial_number",
            "serial",
            "machine_id",
            "laptop_id",
            "hardware_uuid",
            "uuid",
        ):
            text = identity_text(pick(raw, field_name))
            if text:
                aliases.add(text)

        device = DeviceRecord(
            device_id=device_id,
            index=index,
            handoff_at=handoff_at,
            layout_version=layout_version,
            aliases=aliases,
            raw=raw,
        )
        devices.append(device)
        for alias in aliases:
            alias_to_canonical.setdefault(alias, device_id)

    return devices


def build_device_alias_map(devices: list[DeviceRecord]) -> dict[str, str]:
    result: dict[str, str] = {}
    for device in devices:
        for alias in device.aliases:
            result.setdefault(alias, device.device_id)
    return result


def resolve_device_id(
    value: Any, alias_map: Mapping[str, str], *, default: str | None = None
) -> str | None:
    text = identity_text(value)
    if text is None:
        return default
    return alias_map.get(text, text)


def _partition_from_record(
    raw: Mapping[str, Any],
    issues: IssueBook,
    *,
    index: int,
    device_id: str | None,
    layout_version: Any,
    alias_map: Mapping[str, str],
) -> PartitionRecord | None:
    raw_device = pick(raw, "device_id", "device", "asset_id", "serial_number", "machine_id")
    resolved_device = resolve_device_id(raw_device, alias_map, default=device_id)
    if resolved_device is None:
        issues.error("missing_device_id", "Partition/boundary record is missing device_id", index=index)
        resolved_device = f"unassigned-device-{index + 1:04d}"

    version = pick(raw, "layout_version", "partition_layout_version", "table_version", "revision", "version")
    if version is None:
        version = layout_version if layout_version is not None else "unversioned"

    name = identity_text(
        pick(raw, "volume_name", "name", "volume", "label", "filesystem_label", "partition_name")
    )
    instance_id = extract_instance_id(raw)

    interval: Interval | None = None
    try:
        interval = coerce_interval(raw, field_name=f"partition {name or instance_id or index}")
    except Exception as exc:
        issues.error(
            "invalid_partition_range",
            str(exc),
            device_id=resolved_device,
            layout_version=version,
            volume_name=name,
            volume_instance_id=instance_id,
        )

    if instance_id is None:
        fingerprint = identity_text(pick(raw, "uuid", "guid", "partition_uuid", "unique_id"))
        if fingerprint:
            instance_id = fingerprint
        else:
            start = interval.start if interval else "unknown-start"
            end = interval.end if interval else "unknown-end"
            instance_id = f"__unidentified_volume__:{resolved_device}:{version}:{name or ''}:{start}:{end}"
            issues.error(
                "missing_volume_instance_id",
                "Partition is missing a volume instance identifier; same-name volumes cannot be safely matched",
                device_id=resolved_device,
                layout_version=version,
                volume_name=name,
            )

    required = pick(
        raw,
        "encryption_required",
        "requires_encryption",
        "must_encrypt",
        "should_encrypt",
        "required",
        "encrypt",
        "encrypted",
    )
    if required is not None and not isinstance(required, bool):
        if isinstance(required, str):
            required = required.strip().lower() in {"1", "true", "yes", "required", "on"}
        else:
            required = bool(required)

    policy = pick(raw, "encryption_policy", "policy", "policy_assignment")
    if policy is not None and not isinstance(policy, Mapping):
        policy = {"policy_id": policy, "required": True}

    return PartitionRecord(
        device_id=resolved_device,
        layout_version=version,
        name=name,
        instance_id=instance_id,
        interval=interval,
        unit=identity_text(pick(raw, "unit", "offset_unit", "range_unit", "coordinate_unit")),
        mount_point=identity_text(pick(raw, "mount_point", "mount", "path", "mountpoint")),
        required=required if isinstance(required, bool) else None,
        policy=policy if isinstance(policy, Mapping) else None,
        role=identity_text(pick(raw, "role", "partition_role", "type")),
        index=index,
        raw=raw,
    )


def _is_layout_record(raw: Mapping[str, Any]) -> bool:
    if pick(raw, "partitions", "volumes", "partition_table") is not None:
        return True
    boundaries = raw.get("boundaries")
    if isinstance(boundaries, list) and boundaries and all(isinstance(x, (int, float)) for x in boundaries):
        return True
    if isinstance(boundaries, list) and boundaries and all(isinstance(x, Mapping) for x in boundaries):
        return True
    return False


def normalize_layouts(
    value: Any, issues: IssueBook, alias_map: Mapping[str, str]
) -> dict[str, dict[Any, LayoutRecord]]:
    layouts: dict[str, dict[Any, LayoutRecord]] = {}
    if value is None:
        return layouts

    raw_records: list[Any]
    if isinstance(value, Mapping):
        collection = pick(
            value,
            "layouts",
            "layout_versions",
            "partition_layouts",
            "partition_tables",
            "partition_boundaries",
            "boundaries",
            "partitions",
        )
        raw_records = collection if isinstance(collection, list) else [value]
    elif isinstance(value, list):
        raw_records = value
    else:
        raise TypeError("layouts must be a list or mapping")

    partition_index = 0

    def ensure_layout(device_id: str, version: Any, raw: Mapping[str, Any] | None = None) -> LayoutRecord:
        device_layouts = layouts.setdefault(device_id, {})
        if version not in device_layouts:
            try:
                valid_from = parse_time(
                    pick(raw or {}, "valid_from", "effective_from", "created_at", "installed_at", "introduced_at"),
                    field="layout valid_from",
                )
                valid_to = parse_time(
                    pick(raw or {}, "valid_to", "effective_to", "superseded_at", "retired_at", "removed_at"),
                    field="layout valid_to",
                )
            except AuditDataError as exc:
                issues.error("invalid_layout_time", str(exc), device_id=device_id, layout_version=version)
                valid_from = None
                valid_to = None
            device_layouts[version] = LayoutRecord(
                device_id=device_id,
                version=version,
                valid_from=valid_from,
                valid_to=valid_to,
                raw=raw or {},
            )
        return device_layouts[version]

    for item_index, raw in enumerate(raw_records):
        if not isinstance(raw, Mapping):
            issues.error("invalid_layout_record", "Layout record must be a mapping", index=item_index)
            continue

        raw_device = pick(raw, "device_id", "device", "asset_id", "serial_number")
        device_id = resolve_device_id(raw_device, alias_map)
        version = pick(raw, "layout_version", "version", "partition_version", "table_version", "revision")

        if _is_layout_record(raw):
            if device_id is None:
                issues.error("missing_device_id", "Layout is missing device_id", index=item_index)
                device_id = f"unassigned-device-{item_index + 1:04d}"
            if version is None:
                version = f"layout-{item_index + 1:04d}"
            layout = ensure_layout(device_id, version, raw)

            children = pick(raw, "partitions", "volumes", "partition_table", default=[])
            boundaries = raw.get("boundaries")

            child_records: list[Mapping[str, Any]] = []
            scalar_boundaries: list[Number] | None = None

            if isinstance(children, list):
                for child in children:
                    if isinstance(child, Mapping):
                        child_records.append(child)
                    else:
                        child_records.append({"volume_name": str(child)})
            if isinstance(boundaries, list):
                if boundaries and all(isinstance(x, Mapping) for x in boundaries):
                    child_records.extend(x for x in boundaries if isinstance(x, Mapping))
                elif len(boundaries) >= 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in boundaries):
                    scalar_boundaries = boundaries

            for child_index, child in enumerate(child_records):
                merged = dict(raw)
                # Parent layout fields should not overwrite child fields.
                merged.update(child)
                merged.pop("partitions", None)
                merged.pop("volumes", None)
                merged.pop("partition_table", None)

                if scalar_boundaries is not None and child_index + 1 < len(scalar_boundaries):
                    merged.setdefault("start", scalar_boundaries[child_index])
                    merged.setdefault("end", scalar_boundaries[child_index + 1])

                partition = _partition_from_record(
                    merged,
                    issues,
                    index=partition_index,
                    device_id=device_id,
                    layout_version=version,
                    alias_map=alias_map,
                )
                partition_index += 1
                if partition is not None:
                    layout.partitions.append(partition)

            # Boundary coordinates without named volume records.
            if scalar_boundaries is not None and not child_records:
                for i in range(len(scalar_boundaries) - 1):
                    child = {
                        "start": scalar_boundaries[i],
                        "end": scalar_boundaries[i + 1],
                        "volume_name": f"partition-{i}",
                    }
                    merged = dict(raw)
                    merged.update(child)
                    partition = _partition_from_record(
                        merged,
                        issues,
                        index=partition_index,
                        device_id=device_id,
                        layout_version=version,
                        alias_map=alias_map,
                    )
                    partition_index += 1
                    if partition is not None:
                        layout.partitions.append(partition)
        else:
            partition = _partition_from_record(
                raw,
                issues,
                index=partition_index,
                device_id=device_id,
                layout_version=version if version is not None else "unversioned",
                alias_map=alias_map,
            )
            partition_index += 1
            if partition is not None:
                layout = ensure_layout(partition.device_id, partition.layout_version)
                layout.partitions.append(partition)

    # Derive missing valid_to from the next layout's valid_from.
    for device_layouts in layouts.values():
        ordered = sorted(
            device_layouts.values(),
            key=lambda item: (item.valid_from is None, item.valid_from or 0, version_sort_key(item.version)),
        )
        for current, following in zip(ordered, ordered[1:]):
            if current.valid_to is None and following.valid_from is not None:
                current.valid_to = following.valid_from

    return layouts


def version_sort_key(version: Any) -> tuple[int, Any]:
    if isinstance(version, bool):
        return (1, str(version))
    if isinstance(version, (int, float)):
        return (0, float(version))
    text = str(version)
    digits = "".join(ch if ch.isdigit() else " " for ch in text).split()
    if digits:
        return (0, tuple(int(x) for x in digits))
    return (1, text.lower())


_SUCCESS_STATUSES = {
    "confirmed",
    "confirm",
    "success",
    "successful",
    "succeeded",
    "ok",
    "done",
    "complete",
    "completed",
    "encrypted",
    "protected",
    "verified",
    "validated",
    "acknowledged",
    "accepted",
    "in_sync",
    "insync",
    "成功",
    "已确认",
    "已完成",
    "已加密",
    "保护完成",
}
_PENDING_STATUSES = {
    "pending",
    "queued",
    "running",
    "processing",
    "in_progress",
    "inprogress",
    "encrypting",
    "scanning",
    "scan_pending",
    "waiting",
    "正在处理",
    "处理中",
    "加密中",
    "扫描中",
    "待确认",
}
_FAILURE_STATUSES = {
    "failed",
    "failure",
    "error",
    "denied",
    "rejected",
    "revoked",
    "invalid",
    "expired",
    "unsuccessful",
    "失败",
    "错误",
    "已撤销",
}


def is_confirmed_receipt(raw: Mapping[str, Any]) -> tuple[bool, str | None]:
    status_value = pick(raw, "status", "state", "result", "confirmation_status", "agent_status", "outcome")
    status = str(status_value).strip().lower().replace("-", "_") if status_value is not None else None
    if status:
        compact = status.replace(" ", "_")
        if compact in _SUCCESS_STATUSES:
            return True, status
        if compact in _PENDING_STATUSES or compact in _FAILURE_STATUSES:
            return False, status

    explicit_false = any(
        pick(raw, key) is False
        for key in ("confirmed", "success", "successful", "ok", "verified", "validated")
    )
    if explicit_false:
        return False, status or "explicit_false"

    explicit_true = any(
        pick(raw, key) is True
        for key in ("confirmed", "success", "successful", "ok", "verified", "validated")
    )
    if explicit_true:
        return True, status or "confirmed_flag"

    if status is None:
        # The documented input feed is already "agent-confirmed successful
        # ranges"; a record without a contradictory status is usable.
        return True, None

    # Unknown status is not treated as success by a compliance component.
    return False, status


def _receipt_intervals(raw: Mapping[str, Any]) -> list[Interval]:
    intervals: list[Interval] = []
    collection = pick(
        raw,
        "intervals",
        "ranges",
        "encrypted_intervals",
        "confirmed_intervals",
        "confirmed_ranges",
        "encrypted_ranges",
        "covered_ranges",
        "blocks",
    )
    if isinstance(collection, list) and collection:
        for item in collection:
            if isinstance(item, Interval):
                intervals.append(item)
            elif isinstance(item, Mapping):
                intervals.append(coerce_interval(item, field_name="receipt interval"))
            elif isinstance(item, (list, tuple)):
                intervals.append(coerce_interval(item, field_name="receipt interval"))
            else:
                raise ValueError(f"unsupported receipt interval {item!r}")
    else:
        intervals.append(coerce_interval(raw, field_name="receipt"))
    return intervals


def normalize_receipts(
    value: Any, issues: IssueBook, alias_map: Mapping[str, str]
) -> list[ReceiptRecord]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        collection = pick(value, "receipts", "encryption_receipts", "confirmations", "agent_receipts")
        records: list[Any]
        if collection is not None:
            records = as_record_list(collection)
        else:
            records = []
            for key, item in value.items():
                if isinstance(item, Mapping):
                    merged = dict(item)
                    merged.setdefault("receipt_id", key)
                    records.append(merged)
    elif isinstance(value, list):
        records = value
    else:
        raise TypeError("receipts must be a list or mapping")

    receipts: list[ReceiptRecord] = []
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            issues.error("invalid_receipt", "Receipt must be a mapping", index=index)
            continue

        receipt_id = identity_text(
            pick(raw, "receipt_id", "id", "confirmation_id", "agent_receipt_id", "attestation_id", "evidence_id")
        )
        if receipt_id is None:
            receipt_id = f"receipt-{index + 1:04d}"
            issues.warning("missing_receipt_id", "Receipt has no receipt_id; generated a report-local reference", index=index, receipt_id=receipt_id)

        raw_device = pick(raw, "device_id", "device", "asset_id", "serial_number", "machine_id")
        device_id = resolve_device_id(raw_device, alias_map)
        instance_id = extract_instance_id(raw)
        if instance_id is None:
            issues.error(
                "missing_receipt_volume_instance_id",
                "Receipt is missing volume instance identifier and cannot prove same-volume coverage",
                receipt_id=receipt_id,
            )
            instance_id = f"__unidentified_receipt__:{receipt_id}"

        intervals: list[Interval] = []
        try:
            intervals = _receipt_intervals(raw)
        except Exception as exc:
            issues.error("invalid_receipt_range", str(exc), receipt_id=receipt_id)

        confirmed, status = is_confirmed_receipt(raw)

        effective_raw = pick(
            raw,
            "effective_at",
            "effective_time",
            "effective_from",
            "protection_effective_at",
            "completed_at",
            "completion_at",
            "encrypted_at",
            "encryption_completed_at",
            "confirmed_at",
            "timestamp",
        )
        available_raw = pick(
            raw,
            "available_at",
            "availability_at",
            "visible_at",
            "received_at",
            "recorded_at",
            "ingested_at",
            "arrived_at",
            "reported_at",
            "created_at",
            "confirmed_at",
            "issued_at",
            "effective_at",
            "timestamp",
        )
        try:
            effective_at = parse_time(effective_raw, field=f"receipt {receipt_id} effective_at")
        except Exception as exc:
            issues.error("invalid_receipt_effective_time", str(exc), receipt_id=receipt_id)
            effective_at = None
        try:
            available_at = parse_time(available_raw, field=f"receipt {receipt_id} available_at")
        except Exception as exc:
            issues.error("invalid_receipt_available_time", str(exc), receipt_id=receipt_id)
            available_at = None

        if effective_at is None:
            effective_at = available_at
        if available_at is None:
            available_at = effective_at
        if effective_at is None and confirmed:
            issues.error(
                "missing_receipt_timestamp",
                "Confirmed receipt has no effective/availability timestamp and cannot prove cutoff state",
                receipt_id=receipt_id,
            )

        receipts.append(
            ReceiptRecord(
                receipt_id=receipt_id,
                index=index,
                device_id=device_id,
                volume_instance_id=instance_id,
                intervals=intervals,
                confirmed=confirmed,
                status=status,
                effective_at=effective_at,
                available_at=available_at,
                unit=identity_text(pick(raw, "unit", "offset_unit", "range_unit")),
                raw=dict(raw),
            )
        )

    return receipts


def normalize_policies(
    value: Any, issues: IssueBook, alias_map: Mapping[str, str]
) -> list[PolicyRecord]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        collection = pick(value, "policies", "encryption_policies", "policy_assignments")
        records = as_record_list(collection) if collection is not None else []
        if collection is None:
            for key, item in value.items():
                if isinstance(item, Mapping):
                    merged = dict(item)
                    merged.setdefault("policy_id", key)
                    records.append(merged)
    elif isinstance(value, list):
        records = [item for item in value if isinstance(item, Mapping)]
    else:
        raise TypeError("policies must be a list or mapping")

    policies: list[PolicyRecord] = []
    for raw in records:
        raw_device = pick(raw, "device_id", "device", "asset_id", "serial_number")
        device_id = resolve_device_id(raw_device, alias_map)
        version = pick(raw, "layout_version", "version", "partition_version")
        instance_id = extract_instance_id(raw)
        volume_name = identity_text(pick(raw, "volume_name", "name", "volume", "mount_point"))
        policy_id = identity_text(pick(raw, "policy_id", "id", "encryption_policy_id"))

        required_value = pick(
            raw, "required", "encryption_required", "enabled", "is_enabled", "enforced", "should_encrypt"
        )
        if required_value is None:
            required = True
        elif isinstance(required_value, str):
            required = required_value.strip().lower() not in {"false", "0", "no", "off", "disabled"}
        else:
            required = bool(required_value)

        enabled_raw = pick(raw, "enabled_at", "effective_at", "applied_at", "created_at", "assigned_at")
        try:
            enabled_at = parse_time(enabled_raw, field=f"policy {policy_id or ''} enabled_at")
        except Exception as exc:
            issues.error("invalid_policy_time", str(exc), policy_id=policy_id)
            enabled_at = None

        policies.append(
            PolicyRecord(
                policy_id=policy_id,
                device_id=device_id,
                layout_version=version,
                instance_id=instance_id,
                volume_name=volume_name,
                required=required,
                enabled_at=enabled_at,
                raw=dict(raw),
            )
        )
    return policies
