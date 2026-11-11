"""Typed input/output model for the label-index integrity validator.

The DNS decoding module produces a label index *before* expanding any name.
The index is two pieces of evidence:

* ``nodes``: a node table keyed by wire position.  Each node record states the
  label length at that position, whether the position is a legal label start,
  and either the successor position or a name-termination marker.
* ``records``: the resource-record index, mapping each record number to the
  start position of the name it references.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class IntegrityError(Enum):
    """Distinguishable integrity failures.

    The four categories required by the validation contract are
    ``OUT_OF_BOUNDS``, ``NON_LABEL_START``, ``EXPANSION_EXCEEDED`` and
    ``INCOMPLETE_EVIDENCE``.  ``LOOP`` and ``JUMP_BUDGET_EXCEEDED`` cover the
    path-revisit rule and the caller-supplied jump budget.
    """

    OUT_OF_BOUNDS = "out_of_bounds"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    NON_LABEL_START = "non_label_start"
    LOOP = "loop"
    EXPANSION_EXCEEDED = "expansion_exceeded"
    JUMP_BUDGET_EXCEEDED = "jump_budget_exceeded"


@dataclass(frozen=True)
class NodeRecord:
    """One entry of the label index at wire position ``position``.

    ``next_position is None`` is the name-termination marker; such a node must
    carry ``label_length == 0`` (the root label).  Any other node has a
    positive ``label_length`` and an integer successor.
    """

    position: int
    label_length: int
    is_label_start: bool
    next_position: Optional[int]


@dataclass(frozen=True)
class ResourceRecord:
    """One entry of the resource-record index."""

    record_id: int
    name_start: int


@dataclass(frozen=True)
class NameReport:
    """Verdict for one referenced name.

    Several resource records may reference the same ``name_start``; they share
    a single report and are all listed in ``affected_records`` in resource
    record index order.
    """

    name_start: int
    ok: bool
    affected_records: Tuple[int, ...]
    error_code: Optional[IntegrityError]
    error_position: Optional[int]
    expanded_bytes: int
    jumps_used: int
    path: Tuple[int, ...]
