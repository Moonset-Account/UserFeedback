"""Core handoff-time coverage and classification engine."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from .errors import AuditDataError
from .intervals import calculate_coverage
from .records import (
    DeviceRecord,
    IssueBook,
    LayoutRecord,
    PartitionRecord,
    PolicyRecord,
    ReceiptRecord,
    build_device_alias_map,
    normalize_devices,
    normalize_layouts,
    normalize_policies,
    normalize_receipts,
    pick,
    version_sort_key,
)
from .timeutils import UTC, format_time, parse_time

STATUS_PROTECTED = "protected"
STATUS_IN_PROGRESS = "in_progress"
STATUS_MISSING_RECEIPT = "missing_receipt"
STATUS_NOT_REQUIRED = "not_required"
STATUS_INVALID_SCOPE = "invalid_scope"

# Descriptive aliases for callers whose vocabulary uses "completed".
STATUS_COMPLETED = "completed"

STATUS_ZH = {
    STATUS_PROTECTED: "保护已完成",
    STATUS_IN_PROGRESS: "正在处理",
    STATUS_MISSING_RECEIPT: "缺回执",
    STATUS_NOT_REQUIRED: "不要求加密",
    STATUS_INVALID_SCOPE: "范围数据无效",
}

_UNIT_ALIASES = {
    None: None,
    "": None,
    "b": "bytes",
    "byte": "bytes",
    "bytes": "bytes",
    "octet": "bytes",
    "octets": "bytes",
    "sector": "sectors",
    "sectors": "sectors",
    "lba": "sectors",
    "lbas": "sectors",
    "logical_block": "sectors",
    "logical_blocks": "sectors",
    "block": "blocks",
    "blocks": "blocks",
}


def _canonical_unit(unit: Any) -> str | None:
    if unit is None:
        return None
    text = str(unit).strip().lower()
    return _UNIT_ALIASES.get(text, text)


def _units_compatible(left: Any, right: Any) -> bool:
    left_unit = _canonical_unit(left)
    right_unit = _canonical_unit(right)
    if left_unit is None or right_unit is None:
        return True
    return left_unit == right_unit


def _version_key(version: Any) -> tuple[int, Any]:
    return version_sort_key(version)


def versions_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    if str(left) == str(right):
        return True
    return _version_key(left) == _version_key(right)


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class HandoffAuditor:
    """Audit device/layout/receipt records at handoff cutoffs."""

    def __init__(
        self,
        devices: Any = None,
        layouts: Any = None,
        receipts: Any = None,
        policies: Any = None,
        *,
        strict: bool = False,
        default_required: bool = True,
        include_historical: bool = True,
        include_excluded_receipts: bool = True,
    ) -> None:
        self.strict = strict
        self.default_required = default_required
        self.include_historical = include_historical
        self.include_excluded_receipts = include_excluded_receipts
        self.issues = IssueBook()

        self.devices: list[DeviceRecord] = normalize_devices(devices, self.issues)
        self.alias_map = build_device_alias_map(self.devices)
        self.layouts: dict[str, dict[Any, LayoutRecord]] = normalize_layouts(
            layouts, self.issues, self.alias_map
        )
        self.receipts: list[ReceiptRecord] = normalize_receipts(
            receipts, self.issues, self.alias_map
        )
        self.policies: list[PolicyRecord] = normalize_policies(
            policies, self.issues, self.alias_map
        )

        if strict and self.issues.errors:
            raise AuditDataError(
                f"audit input contains {len(self.issues.errors)} validation error(s)",
                self.issues.issues,
            )

    # ------------------------------------------------------------------
    # Public execution methods
    # ------------------------------------------------------------------

    def audit(self, as_of: Any = None) -> dict[str, Any]:
        common_cutoff = parse_time(as_of, field="as_of/handoff_at") if as_of is not None else None
        device_reports = [self.audit_device(device, common_cutoff) for device in self.devices]

        status_counts = {
            STATUS_PROTECTED: 0,
            STATUS_IN_PROGRESS: 0,
            STATUS_MISSING_RECEIPT: 0,
            STATUS_NOT_REQUIRED: 0,
            STATUS_INVALID_SCOPE: 0,
        }
        scoped_total = 0
        historical_protected = 0
        protected_volumes: list[dict[str, Any]] = []
        in_progress_volumes: list[dict[str, Any]] = []
        missing_receipt_volumes: list[dict[str, Any]] = []
        historical_protected_volumes: list[dict[str, Any]] = []

        def volume_ref(report: dict[str, Any], volume: dict[str, Any]) -> dict[str, Any]:
            return {
                "device_id": report["device_id"],
                "handoff_at": report.get("handoff_at"),
                "layout_version": volume.get("layout_version", report.get("layout_version")),
                "volume_name": volume.get("volume_name"),
                "volume_instance_id": volume.get("volume_instance_id"),
                "status": volume.get("status"),
                "state": volume.get("state"),
                "actual_range": volume.get("actual_range"),
                "uncovered_ranges": volume.get("uncovered_ranges"),
                "protection_completed_at": volume.get("protection_completed_at"),
                "citations": volume.get("citations", []),
                "citable_evidence": volume.get("citable_evidence"),
            }

        for report in device_reports:
            for volume in report.get("volumes", []):
                status_counts[volume["status"]] = status_counts.get(volume["status"], 0) + 1
                if volume.get("required", True):
                    scoped_total += 1
                ref = volume_ref(report, volume)
                if volume["status"] == STATUS_PROTECTED:
                    protected_volumes.append(ref)
                elif volume["status"] == STATUS_IN_PROGRESS:
                    in_progress_volumes.append(ref)
                elif volume["status"] == STATUS_MISSING_RECEIPT:
                    missing_receipt_volumes.append(ref)
            for old_volume in report.get("historical_volumes", []):
                if old_volume.get("was_protected_then"):
                    historical_protected += 1
                    historical_protected_volumes.append(volume_ref(report, old_volume))

        return {
            "schema": "volume-handoff-audit/v1",
            "as_of": format_time(common_cutoff),
            "strict": self.strict,
            "summary": {
                "device_count": len(device_reports),
                "partition_count": sum(len(item.get("volumes", [])) for item in device_reports),
                "required_volume_count": scoped_total,
                "status_counts": status_counts,
                "protected_count": status_counts[STATUS_PROTECTED],
                "completed_count": status_counts[STATUS_PROTECTED],
                "in_progress_count": status_counts[STATUS_IN_PROGRESS],
                "missing_receipt_count": status_counts[STATUS_MISSING_RECEIPT],
                "not_required_count": status_counts[STATUS_NOT_REQUIRED],
                "invalid_scope_count": status_counts[STATUS_INVALID_SCOPE],
                "citable_historical_protected_count": historical_protected,
            },
            "protected_volumes": protected_volumes,
            "completed_volumes": protected_volumes,
            "in_progress_volumes": in_progress_volumes,
            "missing_receipt_volumes": missing_receipt_volumes,
            "historical_protected_volumes": historical_protected_volumes,
            "devices": device_reports,
            "validation_issues": self.issues.issues,
        }

    def audit_device(
        self, device: DeviceRecord | str, common_cutoff: datetime | None = None
    ) -> dict[str, Any]:
        if isinstance(device, str):
            device_id = device
            device_record = next((item for item in self.devices if item.device_id == device_id), None)
            if device_record is None:
                raise AuditDataError(f"unknown device_id {device_id!r}")
        else:
            device_record = device

        cutoff = device_record.handoff_at or common_cutoff
        base_report: dict[str, Any] = {
            "device_id": device_record.device_id,
            "handoff_at": format_time(cutoff),
            "device_aliases": sorted(device_record.aliases),
        }

        if cutoff is None:
            self.issues.error(
                "missing_handoff_time",
                "Device has no handoff/seal timestamp and no common as_of was supplied",
                device_id=device_record.device_id,
            )
            if self.strict:
                raise AuditDataError("missing handoff timestamp", self.issues.issues)
            base_report.update(
                {
                    "status": "audit_error",
                    "layout_version": device_record.layout_version,
                    "volumes": [],
                    "historical_volumes": [],
                    "layout_reports": [],
                    "summary": {"audit_error": "missing_handoff_time"},
                }
            )
            return base_report

        layout, selection_error = self.select_layout(device_record, cutoff)
        if layout is None:
            if self.strict:
                raise AuditDataError(
                    selection_error or "layout_not_found", self.issues.issues
                )
            base_report.update(
                {
                    "status": "audit_error",
                    "layout_version": device_record.layout_version,
                    "volumes": [],
                    "historical_volumes": [],
                    "layout_reports": [],
                    "summary": {"audit_error": selection_error or "layout_not_found"},
                }
            )
            return base_report

        active_volumes = [
            self.assess_partition(device_record, layout, partition, cutoff)
            for partition in layout.partitions
        ]

        layout_reports = [
            {
                "layout_version": layout.version,
                "active": True,
                "valid_from": format_time(layout.valid_from),
                "valid_to": format_time(layout.valid_to),
                "effective_cutoff": format_time(cutoff),
                "availability_cutoff": format_time(cutoff),
                "volumes": active_volumes,
            }
        ]

        historical_volumes: list[dict[str, Any]] = []
        if self.include_historical:
            for version, old_layout in sorted(
                self.layouts.get(device_record.device_id, {}).items(),
                key=lambda item: _version_key(item[0]),
            ):
                if versions_equal(version, layout.version):
                    continue
                retirement_cutoff = old_layout.valid_to or cutoff
                if cutoff is not None and retirement_cutoff > cutoff:
                    historical_effective_cutoff = cutoff
                else:
                    historical_effective_cutoff = retirement_cutoff
                historical_as_of = historical_effective_cutoff or cutoff

                old_assessments = [
                    self.assess_partition(
                        device_record,
                        old_layout,
                        partition,
                        cutoff,
                        effective_cutoff=historical_effective_cutoff,
                        availability_cutoff=cutoff,
                        historical=True,
                        historical_as_of=historical_as_of,
                    )
                    for partition in old_layout.partitions
                ]
                layout_reports.append(
                    {
                        "layout_version": version,
                        "active": False,
                        "valid_from": format_time(old_layout.valid_from),
                        "valid_to": format_time(old_layout.valid_to),
                        "effective_cutoff": format_time(historical_effective_cutoff),
                        "availability_cutoff": format_time(cutoff),
                        "volumes": old_assessments,
                    }
                )
                for assessment in old_assessments:
                    assessment["layout_version"] = version
                    assessment["active"] = False
                    assessment["historical"] = True
                    assessment["historical_as_of"] = format_time(historical_as_of)
                    assessment["was_protected_then"] = assessment["status"] == STATUS_PROTECTED
                    historical_volumes.append(assessment)

        counts = {
            STATUS_PROTECTED: 0,
            STATUS_IN_PROGRESS: 0,
            STATUS_MISSING_RECEIPT: 0,
            STATUS_NOT_REQUIRED: 0,
            STATUS_INVALID_SCOPE: 0,
        }
        for assessment in active_volumes:
            counts[assessment["status"]] = counts.get(assessment["status"], 0) + 1

        protected_history = sum(1 for item in historical_volumes if item.get("was_protected_then"))

        base_report.update(
            {
                "status": "audited",
                "layout_version": layout.version,
                "layout": {
                    "version": layout.version,
                    "valid_from": format_time(layout.valid_from),
                    "valid_to": format_time(layout.valid_to),
                },
                "volumes": active_volumes,
                "historical_volumes": historical_volumes,
                "layout_reports": layout_reports,
                "summary": {
                    "partition_count": len(active_volumes),
                    "status_counts": counts,
                    "protected_count": counts[STATUS_PROTECTED],
                    "completed_count": counts[STATUS_PROTECTED],
                    "in_progress_count": counts[STATUS_IN_PROGRESS],
                    "missing_receipt_count": counts[STATUS_MISSING_RECEIPT],
                    "not_required_count": counts[STATUS_NOT_REQUIRED],
                    "invalid_scope_count": counts[STATUS_INVALID_SCOPE],
                    "citable_historical_protected_count": protected_history,
                },
            }
        )
        return base_report

    def select_layout(
        self, device: DeviceRecord, cutoff: datetime
    ) -> tuple[LayoutRecord | None, str | None]:
        device_layouts = self.layouts.get(device.device_id, {})
        if not device_layouts:
            self.issues.error(
                "missing_layout",
                "No partition layout/boundaries were supplied for device",
                device_id=device.device_id,
            )
            return None, "missing_layout"

        requested = device.layout_version
        if requested is not None:
            for version, layout in device_layouts.items():
                if versions_equal(version, requested):
                    return layout, None
            self.issues.error(
                "unknown_layout_version",
                "Device registry requests an unknown layout version",
                device_id=device.device_id,
                requested_layout_version=requested,
                available_versions=list(device_layouts),
            )
            return None, "unknown_layout_version"

        active_candidates = []
        for layout in device_layouts.values():
            if layout.valid_from is None or layout.valid_from <= cutoff:
                active_candidates.append(layout)

        if not active_candidates:
            self.issues.error(
                "no_layout_active_at_cutoff",
                "All supplied layout versions become valid after the handoff cutoff",
                device_id=device.device_id,
                cutoff=format_time(cutoff),
            )
            return None, "no_layout_active_at_cutoff"

        selected = max(
            active_candidates,
            key=lambda layout: (
                layout.valid_from or datetime.min.replace(tzinfo=UTC),
                _version_key(layout.version),
            ),
        )
        return selected, None

    # ------------------------------------------------------------------
    # Policy and receipt matching
    # ------------------------------------------------------------------

    def resolve_requirement(
        self, device: DeviceRecord, partition: PartitionRecord, cutoff: datetime
    ) -> tuple[bool, dict[str, Any]]:
        if partition.required is not None:
            return partition.required, {
                "required": partition.required,
                "source": "partition_field",
                "policy_id": (partition.policy or {}).get("policy_id") if partition.policy else None,
            }

        if partition.policy is not None:
            embedded_required = pick(
                partition.policy, "required", "encryption_required", "enabled", "enforced", default=True
            )
            if isinstance(embedded_required, str):
                embedded_required = embedded_required.strip().lower() not in {
                    "false",
                    "0",
                    "no",
                    "off",
                    "disabled",
                }
            return bool(embedded_required), {
                "required": bool(embedded_required),
                "source": "embedded_policy",
                "policy_id": partition.policy.get("policy_id") or partition.policy.get("id"),
            }

        best: tuple[int, PolicyRecord] | None = None
        for policy in self.policies:
            if policy.device_id is not None and policy.device_id != device.device_id:
                # Normalization maps aliases; this fallback handles unknown ids.
                if not device.matches_device(policy.device_id):
                    continue
            if policy.enabled_at is not None and policy.enabled_at > cutoff:
                continue

            score = 0
            if policy.instance_id and policy.instance_id == partition.instance_id:
                score = 80
                if policy.layout_version is not None and versions_equal(
                    policy.layout_version, partition.layout_version
                ):
                    score = 100
            elif policy.volume_name and partition.name and policy.volume_name == partition.name:
                score = 40
                if policy.layout_version is not None and versions_equal(
                    policy.layout_version, partition.layout_version
                ):
                    score = 60
            else:
                continue

            if best is None or score > best[0]:
                best = (score, policy)

        if best is not None:
            policy = best[1]
            return policy.required, {
                "required": policy.required,
                "source": "policy_record",
                "policy_id": policy.policy_id,
                "match_score": best[0],
                "enabled_at": format_time(policy.enabled_at),
            }

        return self.default_required, {"required": self.default_required, "source": "default"}

    def select_receipts(
        self,
        device: DeviceRecord,
        partition: PartitionRecord,
        effective_cutoff: datetime,
        availability_cutoff: datetime,
    ) -> tuple[list[ReceiptRecord], list[dict[str, Any]], list[dict[str, Any]]]:
        usable: list[ReceiptRecord] = []
        excluded: list[dict[str, Any]] = []
        processing_signals: list[dict[str, Any]] = []

        for receipt in self.receipts:
            if receipt.volume_instance_id != partition.instance_id:
                continue

            if receipt.device_id is not None and not device.matches_device(receipt.device_id):
                excluded.append(
                    self._excluded_receipt(receipt, "device_mismatch")
                )
                continue

            if not receipt.confirmed:
                reason = "receipt_not_confirmed"
                if receipt.status in {
                    "pending",
                    "queued",
                    "running",
                    "processing",
                    "in_progress",
                    "encrypting",
                    "scanning",
                    "scan_pending",
                    "waiting",
                }:
                    processing_signals.append(
                        {
                            "receipt_id": receipt.receipt_id,
                            "status": receipt.status,
                            "effective_at": format_time(receipt.effective_at),
                            "available_at": format_time(receipt.available_at),
                        }
                    )
                    reason = "agent_reported_processing"
                excluded.append(self._excluded_receipt(receipt, reason))
                continue

            if receipt.effective_at is None or receipt.available_at is None:
                excluded.append(self._excluded_receipt(receipt, "missing_timestamp"))
                continue
            if receipt.available_at > availability_cutoff:
                excluded.append(self._excluded_receipt(receipt, "available_after_cutoff"))
                continue
            if receipt.effective_at > effective_cutoff:
                excluded.append(self._excluded_receipt(receipt, "effective_after_cutoff"))
                continue
            if not _units_compatible(receipt.unit, partition.unit):
                excluded.append(self._excluded_receipt(receipt, "unit_mismatch"))
                continue
            if not receipt.intervals:
                excluded.append(self._excluded_receipt(receipt, "no_interval"))
                continue

            usable.append(receipt)

        return usable, excluded, processing_signals

    @staticmethod
    def _excluded_receipt(receipt: ReceiptRecord, reason: str) -> dict[str, Any]:
        return {
            "receipt_id": receipt.receipt_id,
            "reason": reason,
            "status": receipt.status,
            "effective_at": format_time(receipt.effective_at),
            "available_at": format_time(receipt.available_at),
        }

    def same_name_other_instance_refs(
        self, device: DeviceRecord, partition: PartitionRecord
    ) -> list[dict[str, Any]]:
        if not partition.name:
            return []

        related_instance_ids: set[str] = set()
        for layout in self.layouts.get(device.device_id, {}).values():
            for candidate in layout.partitions:
                if candidate.name == partition.name and candidate.instance_id != partition.instance_id:
                    related_instance_ids.add(candidate.instance_id)

        refs: list[dict[str, Any]] = []
        for receipt in self.receipts:
            if receipt.volume_instance_id not in related_instance_ids:
                continue
            if receipt.device_id is not None and not device.matches_device(receipt.device_id):
                continue
            refs.append(
                {
                    "receipt_id": receipt.receipt_id,
                    "volume_instance_id": receipt.volume_instance_id,
                    "reason": "different_volume_instance_not_applied",
                }
            )
        return refs

    # ------------------------------------------------------------------
    # Assessment
    # ------------------------------------------------------------------

    def assess_partition(
        self,
        device: DeviceRecord,
        layout: LayoutRecord,
        partition: PartitionRecord,
        handoff_cutoff: datetime,
        *,
        effective_cutoff: datetime | None = None,
        availability_cutoff: datetime | None = None,
        historical: bool = False,
        historical_as_of: datetime | None = None,
    ) -> dict[str, Any]:
        effective_cutoff = effective_cutoff or handoff_cutoff
        availability_cutoff = availability_cutoff or handoff_cutoff

        requirement, policy_info = self.resolve_requirement(device, partition, effective_cutoff)
        actual_range = partition.interval.to_dict() if partition.interval is not None else None

        assessment: dict[str, Any] = {
            "device_id": device.device_id,
            "layout_version": partition.layout_version,
            "volume_name": partition.name,
            "volume_instance_id": partition.instance_id,
            "mount_point": partition.mount_point,
            "role": partition.role,
            "unit": _canonical_unit(partition.unit),
            "actual_range": actual_range,
            "required": requirement,
            "policy": policy_info,
            "active": not historical,
            "historical": historical,
            "handoff_as_of": format_time(handoff_cutoff),
            "effective_cutoff": format_time(effective_cutoff),
            "availability_cutoff": format_time(availability_cutoff),
        }

        if partition.interval is None:
            assessment.update(
                {
                    "status": STATUS_INVALID_SCOPE,
                    "status_zh": STATUS_ZH[STATUS_INVALID_SCOPE],
                    "state": "invalid_scope",
                    "reason": "partition_boundary_invalid",
                    "coverage": None,
                    "covered": [],
                    "uncovered": [],
                    "gaps": [],
                    "covered_ranges": [],
                    "uncovered_ranges": [],
                    "evidence": [],
                    "citations": [],
                    "citable": False,
                }
            )
            return assessment

        usable, excluded, processing_signals = self.select_receipts(
            device, partition, effective_cutoff, availability_cutoff
        )
        coverage = calculate_coverage(
            partition.interval,
            [interval for receipt in usable for interval in receipt.intervals],
        )

        evidence = self._build_evidence(partition, usable)
        contributing_ids = {item["receipt_id"] for item in evidence}
        evidence_receipts = [r for r in usable if r.receipt_id in contributing_ids]
        completed_at = self._completion_time(partition, evidence_receipts)

        same_name_refs = self.same_name_other_instance_refs(device, partition)

        if not requirement:
            status = STATUS_NOT_REQUIRED
            reason = "encryption_not_required_by_policy"
        elif coverage["fully_covered"]:
            status = STATUS_PROTECTED
            reason = "full_interval_coverage"
        elif coverage["has_any_coverage"]:
            status = STATUS_IN_PROGRESS
            reason = "partial_confirmed_coverage"
        elif processing_signals:
            status = STATUS_IN_PROGRESS
            reason = "agent_reported_in_progress_without_confirmed_interval"
        else:
            status = STATUS_MISSING_RECEIPT
            late = any(item["reason"] == "available_after_cutoff" for item in excluded)
            future = any(item["reason"] == "effective_after_cutoff" for item in excluded)
            if late:
                reason = "confirmation_not_available_by_cutoff"
            elif future:
                reason = "receipt_not_effective_by_cutoff"
            elif excluded:
                reason = "no_usable_confirmed_receipt"
            else:
                reason = "no_confirmed_receipt"

        pending_confirmation = status == STATUS_IN_PROGRESS or reason in {
            "confirmation_not_available_by_cutoff",
            "receipt_not_effective_by_cutoff",
        }

        citations = [item["citation"] for item in evidence]
        digest_input = {
            "device_id": device.device_id,
            "layout_version": partition.layout_version,
            "volume_instance_id": partition.instance_id,
            "actual_range": actual_range,
            "effective_cutoff": format_time(effective_cutoff),
            "availability_cutoff": format_time(availability_cutoff),
            "receipts": sorted(
                [
                    {
                        "receipt_id": receipt.receipt_id,
                        "effective_at": format_time(receipt.effective_at),
                        "available_at": format_time(receipt.available_at),
                        "intervals": [interval.to_dict() for interval in receipt.intervals],
                    }
                    for receipt in evidence_receipts
                ],
                key=lambda item: item["receipt_id"],
            ),
        }
        record_digest = _digest(digest_input)
        citable = status == STATUS_PROTECTED and bool(evidence)

        assessment.update(
            {
                "status": status,
                "status_zh": STATUS_ZH.get(status, status),
                "state": "completed" if status == STATUS_PROTECTED else status,
                "result": "completed" if status == STATUS_PROTECTED else status,
                "protected": status == STATUS_PROTECTED,
                "completed": status == STATUS_PROTECTED,
                "in_progress": status == STATUS_IN_PROGRESS,
                "missing_receipt": status == STATUS_MISSING_RECEIPT,
                "pending_confirmation": pending_confirmation,
                "reason": reason,
                "protection_completed_at": format_time(completed_at),
                "coverage": coverage,
                "covered": coverage["covered_ranges"],
                "uncovered": coverage["uncovered_ranges"],
                "gaps": coverage["uncovered_ranges"],
                "covered_ranges": coverage["covered_ranges"],
                # Convenience/required key: the actual unprotected extent.
                "uncovered_ranges": coverage["uncovered_ranges"],
                "coverage_ratio": coverage["coverage_ratio"],
                "fully_covered": coverage["fully_covered"],
                "evidence": evidence,
                "citations": citations,
                "citable": citable,
                "citable_evidence": {
                    "citation_refs": citations,
                    "record_digest": record_digest,
                    "digest_algorithm": "sha256-canonical-json",
                    "as_of": format_time(availability_cutoff),
                    "statement": self._citation_statement(
                        device, partition, status, evidence, completed_at, effective_cutoff
                    ),
                },
                "processing_signals": processing_signals,
                "same_name_other_instance_receipts": same_name_refs,
            }
        )

        if self.include_excluded_receipts:
            assessment["excluded_receipts"] = excluded
        else:
            assessment["excluded_receipt_count"] = len(excluded)

        if historical:
            assessment["historical_as_of"] = format_time(historical_as_of or effective_cutoff)
            assessment["was_protected_then"] = status == STATUS_PROTECTED

        return assessment

    def _build_evidence(
        self, partition: PartitionRecord, receipts: list[ReceiptRecord]
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        target = partition.interval
        assert target is not None

        for receipt in receipts:
            inside: list[dict[str, Any]] = []
            for interval in receipt.intervals:
                clipped = interval.clips(target)
                if clipped is not None:
                    inside.append(clipped.to_dict())
            if not inside:
                continue
            evidence.append(
                {
                    "receipt_id": receipt.receipt_id,
                    "citation": f"receipt:{receipt.receipt_id}",
                    "device_id": receipt.device_id,
                    "volume_instance_id": receipt.volume_instance_id,
                    "agent_status": receipt.status or "confirmed",
                    "effective_at": format_time(receipt.effective_at),
                    "available_at": format_time(receipt.available_at),
                    "confirmed_intervals": [interval.to_dict() for interval in receipt.intervals],
                    "intersecting_scope_ranges": inside,
                }
            )
        return evidence

    @staticmethod
    def _completion_time(
        partition: PartitionRecord, receipts: list[ReceiptRecord]
    ) -> datetime | None:
        if partition.interval is None:
            return None
        accumulated = []
        for receipt in sorted(
            receipts,
            key=lambda item: item.effective_at or datetime.min.replace(tzinfo=UTC),
        ):
            accumulated.extend(receipt.intervals)
            if calculate_coverage(partition.interval, accumulated)["fully_covered"]:
                return receipt.effective_at
        return None

    @staticmethod
    def _citation_statement(
        device: DeviceRecord,
        partition: PartitionRecord,
        status: str,
        evidence: list[dict[str, Any]],
        completed_at: datetime | None,
        cutoff: datetime,
    ) -> str:
        ids = ", ".join(item["receipt_id"] for item in evidence) or "no receipts"
        if status == STATUS_PROTECTED:
            when = format_time(completed_at) or format_time(cutoff)
            return (
                f"Device {device.device_id}, volume {partition.name or partition.instance_id} "
                f"({partition.instance_id}) had complete interval coverage from receipt(s) {ids}; "
                f"protection completion evidenced at {when}."
            )
        return (
            f"Volume {partition.name or partition.instance_id} ({partition.instance_id}) "
            f"has audit status {status} at {format_time(cutoff)}; receipt(s) considered: {ids}."
        )


# ----------------------------------------------------------------------
# Functional API
# ----------------------------------------------------------------------

_DOC_DEVICE_KEYS = ("devices", "device_registry", "registry", "assets", "laptops")
_DOC_LAYOUT_KEYS = (
    "layouts",
    "layout_versions",
    "partition_layouts",
    "partition_tables",
    "partition_boundaries",
    "boundaries",
    "partitions",
)
_DOC_RECEIPT_KEYS = (
    "receipts",
    "encryption_receipts",
    "confirmed_encryption_ranges",
    "confirmations",
    "agent_receipts",
)
_DOC_POLICY_KEYS = ("policies", "encryption_policies", "policy_assignments")
_DOC_TIME_KEYS = ("as_of", "handoff_at", "handover_at", "sealed_at", "cutoff", "audit_at")


def _first_document_key(document: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = pick(document, key)
        if value is not None:
            return value
    return None


def audit_document(document: Mapping[str, Any], **options: Any) -> dict[str, Any]:
    """Audit one parsed JSON-like document containing all input feeds."""

    if not isinstance(document, Mapping):
        raise AuditDataError("audit_document expects a mapping/JSON object")

    if "data" in document and isinstance(document["data"], Mapping):
        nested = dict(document["data"])
        for key, value in document.items():
            if key != "data":
                nested.setdefault(key, value)
        document = nested

    as_of = options.pop("as_of", None)
    if as_of is None:
        as_of = _first_document_key(document, _DOC_TIME_KEYS)

    settings = document.get("options") if isinstance(document.get("options"), Mapping) else {}
    merged_options = {**(settings or {}), **options}

    return HandoffAuditor(
        devices=_first_document_key(document, _DOC_DEVICE_KEYS),
        layouts=_first_document_key(document, _DOC_LAYOUT_KEYS),
        receipts=_first_document_key(document, _DOC_RECEIPT_KEYS),
        policies=_first_document_key(document, _DOC_POLICY_KEYS),
        **merged_options,
    ).audit(as_of)


def audit_handoffs(
    devices: Any = None,
    layouts: Any = None,
    receipts: Any = None,
    policies: Any = None,
    *,
    as_of: Any = None,
    strict: bool = False,
    default_required: bool = True,
    include_historical: bool = True,
    **options: Any,
) -> dict[str, Any]:
    """Audit a batch at handoff/seal cutoffs.

    A single mapping argument that looks like a combined document is accepted.
    Otherwise arguments are device registry, layout/boundary feed, receipts,
    and optional policies.
    """

    if (
        isinstance(devices, Mapping)
        and layouts is None
        and receipts is None
        and policies is None
        and any(pick(devices, key) is not None for key in _DOC_DEVICE_KEYS + _DOC_LAYOUT_KEYS + _DOC_RECEIPT_KEYS)
    ):
        return audit_document(
            devices,
            as_of=as_of,
            strict=strict,
            default_required=default_required,
            **options,
        )

    auditor = HandoffAuditor(
        devices=devices,
        layouts=layouts,
        receipts=receipts,
        policies=policies,
        strict=strict,
        default_required=default_required,
        include_historical=include_historical,
        **options,
    )
    return auditor.audit(as_of)


verify_handoffs = audit_handoffs
audit_batch = audit_handoffs


_ALIAS_KWARGS = {
    "device_registry": "devices",
    "registry": "devices",
    "assets": "devices",
    "laptops": "devices",
    "partition_boundaries": "layouts",
    "boundary_versions": "layouts",
    "layout_versions": "layouts",
    "partition_layouts": "layouts",
    "partition_tables": "layouts",
    "boundaries": "layouts",
    "encryption_receipts": "receipts",
    "agent_receipts": "receipts",
    "confirmed_encryption_ranges": "receipts",
    "confirmation_receipts": "receipts",
    "encryption_policies": "policies",
    "policy_records": "policies",
    "handoff_time": "as_of",
    "handoff_timestamp": "as_of",
    "sealed_at": "as_of",
    "seal_time": "as_of",
    "cutoff": "as_of",
}


def evaluate_handoffs(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Alias-tolerant entry point for registry/boundary/receipt keyword names."""

    normalized: dict[str, Any] = {}
    for key, value in kwargs.items():
        target = _ALIAS_KWARGS.get(key, key)
        # Explicit canonical keys win if both spellings are supplied.
        normalized.setdefault(target, value)
    return audit_handoffs(*args, **normalized)


verify = evaluate_handoffs
audit = evaluate_handoffs
evaluate = evaluate_handoffs
