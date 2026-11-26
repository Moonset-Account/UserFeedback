"""Immutable adjudication result records and JSON-friendly serialization."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value


@dataclass(frozen=True)
class ProofAssignment:
    slot_index: int
    requirement: str
    capability: str
    proof_id: str
    proof_kind: str
    identity: str | None
    route_edges: tuple[str, ...] = ()
    route_nodes: tuple[str, ...] = ()
    authorization_costs: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)


@dataclass(frozen=True)
class ApprovedOperation:
    request_id: str
    identity: str | None
    order: int
    started_at: float
    confirmed_at: float
    deadline: float | None
    duration: float
    assignments: tuple[ProofAssignment, ...]

    @property
    def proof_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for assignment in self.assignments:
            if assignment.proof_id not in seen:
                seen.append(assignment.proof_id)
        return tuple(seen)

    @property
    def proof_combination(self) -> dict[str, str | list[str]]:
        result: dict[str, str | list[str]] = {}
        counts: dict[str, int] = {}
        for assignment in sorted(self.assignments, key=lambda a: a.slot_index):
            key = assignment.requirement
            counts[key] = counts.get(key, 0) + 1
            if counts[key] == 1 and key not in result:
                result[key] = assignment.proof_id
            else:
                current = result.get(key)
                if isinstance(current, str):
                    result[key] = [current, assignment.proof_id]
                elif isinstance(current, list):
                    current.append(assignment.proof_id)
        return result

    def to_dict(self) -> dict[str, Any]:
        data = _jsonable(self.__dict__)
        data["proof_ids"] = list(self.proof_ids)
        data["proof_combination"] = _jsonable(self.proof_combination)
        return data


@dataclass(frozen=True)
class MissingCapability:
    capability: str
    requirement: str
    required: int
    available_after: int | None
    available_globally: int | None
    reason_code: str
    message: str
    candidate_proof_ids: tuple[str, ...] = ()
    consumed_by: dict[str, tuple[str, ...]] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)


@dataclass(frozen=True)
class RejectedOperation:
    request_id: str
    identity: str | None
    deadline: float | None
    reason_code: str
    message: str
    missing_capabilities: tuple[MissingCapability, ...] = ()
    unmet_requirements: tuple[str, ...] = ()
    conflict_requirements: tuple[str, ...] = ()
    schedule_possible_alone: bool = True
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)


@dataclass(frozen=True)
class AdjudicationResult:
    now: float
    terminal_id: str | None
    maximum_size: int
    approved: tuple[ApprovedOperation, ...]
    rejected: tuple[RejectedOperation, ...]
    search_examined: int = 0

    @property
    def approved_ids(self) -> tuple[str, ...]:
        return tuple(op.request_id for op in self.approved)

    @property
    def rejected_ids(self) -> tuple[str, ...]:
        return tuple(op.request_id for op in self.rejected)

    @property
    def all_approved(self) -> bool:
        return not self.rejected

    @property
    def proof_combinations(self) -> dict[str, dict[str, str | list[str]]]:
        return {op.request_id: op.proof_combination for op in self.approved}

    @property
    def missing_capabilities(self) -> dict[str, tuple[MissingCapability, ...]]:
        return {op.request_id: op.missing_capabilities for op in self.rejected}

    def to_dict(self) -> dict[str, Any]:
        return {
            "now": self.now,
            "terminal_id": self.terminal_id,
            "maximum_size": self.maximum_size,
            "approved_ids": list(self.approved_ids),
            "rejected_ids": list(self.rejected_ids),
            "approved": [op.to_dict() for op in self.approved],
            "rejected": [op.to_dict() for op in self.rejected],
            "proof_combinations": {k: _jsonable(v) for k, v in self.proof_combinations.items()},
            "missing_capabilities": {
                request_id: [m.to_dict() for m in missing]
                for request_id, missing in self.missing_capabilities.items()
            },
            "search_examined": self.search_examined,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=False)
