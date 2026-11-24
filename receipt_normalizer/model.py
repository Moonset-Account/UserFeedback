"""Core data types for audit receipt normalization.

The module intentionally uses only the Python standard library.  The public
dataclasses describe the smallest evidence needed by the normalizer:

* :class:`ActionRecord` -- raw action receipt fields;
* :class:`ImageSwitch` -- process image/ABI transition evidence;
* :class:`EvidenceGap` -- missing or contradictory evidence;
* :class:`NormalizedReceipt` -- result consumed by alert rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Hashable, Mapping, Sequence, Union

# ABI labels are deliberately strings so that callers can use other values such
# as distribution-specific multilib names.  These two constants cover the
# usual native/compatibility split.
ABI_NATIVE = "native"
ABI_COMPAT = "compat"
ABI_ANY = "*"

STATUS_COMPLETE = "complete"
STATUS_EVIDENCE_GAP = "evidence_gap"

ParamPath = Union[int, str, Sequence[Union[int, str]]]
ProcessId = Hashable


class GapCode:
    """Stable machine-readable evidence-gap codes."""

    PROCESS_EVIDENCE_MISSING = "process_evidence_missing"
    IMAGE_UNKNOWN = "image_unknown"
    IMAGE_CONFLICT = "image_conflict"
    ABI_UNKNOWN = "abi_unknown"
    ABI_CONFLICT = "abi_conflict"

    TABLE_VERSION_UNKNOWN = "table_version_unknown"
    TABLE_VERSION_CONFLICT = "table_version_conflict"

    ACTION_CODE_MISSING = "action_code_missing"
    ACTION_UNMAPPED = "action_unmapped"
    SELECTOR_MISSING = "selector_parameter_missing"
    SUBOPERATION_UNKNOWN = "suboperation_unknown"
    TARGET_PARAMETER_MISSING = "target_parameter_missing"


@dataclass(frozen=True)
class EvidenceGap:
    """A review request for a protection rule.

    ``code`` is stable enough for routing in rule tooling.  ``message`` is
    human-readable, while ``details`` carries non-secret local context such as
    a parameter index or candidate versions.
    """

    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": _jsonable(self.details),
        }


@dataclass(frozen=True)
class ActionRecord:
    """Raw action evidence.

    The problem statement's minimal shape is ``(process_instance, source_seq,
    params)``.  ``action_code`` is optional because some wire formats place
    the raw entry/code inside ``params``; configure a table ``code_selector``
    in that case.  Most audit receipts will supply the code explicitly.
    """

    process_instance: ProcessId
    source_seq: int
    params: Any = ()
    action_code: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_instance": self.process_instance,
            "source_seq": self.source_seq,
            "action_code": self.action_code,
            "params": _jsonable(self.params),
        }


@dataclass(frozen=True)
class ImageSwitch:
    """An image/ABI state transition for one process instance.

    ``source_seq`` is the first source position at which the new state is
    active.  ``order`` resolves multiple evidence records at the same source
    position when their order is independently known.
    """

    process_instance: ProcessId
    source_seq: int
    image: str | None = None
    abi: str | None = None
    order: int = 0
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_instance": self.process_instance,
            "source_seq": self.source_seq,
            "image": self.image,
            "abi": self.abi,
            "order": self.order,
            "attributes": _jsonable(self.attributes),
        }


@dataclass(frozen=True)
class ProcessInfo:
    """Process identity and image/ABI state selected for an action."""

    process_instance: ProcessId
    image: str | None = None
    abi: str | None = None
    evidence_seq: int | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_instance": self.process_instance,
            "image": self.image,
            "abi": self.abi,
            "evidence_seq": self.evidence_seq,
            "attributes": _jsonable(self.attributes),
        }


@dataclass(frozen=True)
class Target:
    """The object acted upon.

    ``identifier`` is the value selected by the mapping table.  Additional
    decoded parameters, such as a directory file descriptor or flags, go in
    ``attributes``.
    """

    kind: str
    identifier: Any = None
    parameter: ParamPath | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "identifier": _jsonable(self.identifier),
            "parameter": _jsonable(self.parameter),
            "attributes": _jsonable(self.attributes),
        }


@dataclass(frozen=True)
class NormalizedReceipt:
    """Normalization result plus any evidence gaps."""

    source_seq: int | None
    process: ProcessInfo
    action_code: Any = None
    action_name: str | None = None
    entry_name: str | None = None
    target: Target | None = None
    table_version: str | None = None
    selector_value: Any = None
    params: Any = None
    gaps: tuple[EvidenceGap, ...] = ()

    @property
    def process_instance(self) -> ProcessId:
        return self.process.process_instance

    @property
    def status(self) -> str:
        return STATUS_COMPLETE if not self.gaps else STATUS_EVIDENCE_GAP

    @property
    def is_complete(self) -> bool:
        return not self.gaps

    @property
    def needs_review(self) -> bool:
        return bool(self.gaps)

    def gap_codes(self) -> tuple[str, ...]:
        return tuple(gap.code for gap in self.gaps)

    def to_dict(self) -> dict[str, Any]:
        target = None if self.target is None else self.target.to_dict()
        action = {
            "code": _jsonable(self.action_code),
            "name": self.action_name,
            "permission_action": self.action_name,
            "entry_name": self.entry_name,
        }
        return {
            "status": self.status,
            "source_seq": self.source_seq,
            "process": self.process.to_dict(),
            "action": action,
            # Flat fields are convenient for rule engines and existing callers.
            "action_code": _jsonable(self.action_code),
            "action_name": self.action_name,
            "permission_action": self.action_name,
            "entry_name": self.entry_name,
            "object": target,
            "target": target,
            "table_version": self.table_version,
            "selector_value": _jsonable(self.selector_value),
            "params": _jsonable(self.params),
            "evidence_gaps": [gap.to_dict() for gap in self.gaps],
        }


def lookup_path(params: Any, path: ParamPath | None) -> tuple[bool, Any]:
    """Resolve a mapping-table parameter reference.

    A single integer is a positional parameter and a single string is a named
    parameter.  A sequence such as ``(0, "path")`` traverses nested lists or
    mappings.  Missing references return ``(False, None)`` rather than raising.
    """

    if path is None:
        return False, None

    if isinstance(path, (int, str)) and not isinstance(path, bool):
        steps: tuple[Any, ...] = (path,)
    else:
        steps = tuple(path)  # type: ignore[arg-type]

    current = params
    for step in steps:
        if isinstance(step, bool):
            return False, None
        if isinstance(step, int):
            if not isinstance(current, Sequence) or isinstance(current, (str, bytes, bytearray)):
                return False, None
            if -len(current) <= step < len(current):
                current = current[step]
            else:
                return False, None
        elif isinstance(step, str):
            if not isinstance(current, Mapping):
                return False, None
            # JSON object keys are strings; tolerate numeric references written
            # as strings and integer keys in in-memory dict fixtures.
            if step in current:
                current = current[step]
            elif step.isdigit() and int(step) in current:
                current = current[int(step)]
            else:
                return False, None
        else:
            return False, None
    return True, current


def _jsonable(value: Any) -> Any:
    """Convert common dataclass/tuple values to JSON-compatible values."""

    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value
