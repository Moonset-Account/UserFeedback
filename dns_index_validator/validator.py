"""Integrity validation over a pre-expansion DNS label index.

The validator never expands names itself.  It walks the label index exactly as
a downstream expander would, and rejects every name whose evidence is
incomplete or whose traversal violates the wire rules, so later modules only
consume records that passed the checks.

Distinguishable failure categories (see :class:`IntegrityError`):

* ``OUT_OF_BOUNDS``        -- a referenced position lies outside the address
                              domain ``[0, domain_size)``.
* ``INCOMPLETE_EVIDENCE``  -- the position is inside the domain but the node
                              table has no record for it, or a non-root node
                              carries the name-termination marker.
* ``NON_LABEL_START``      -- the position is indexed but explicitly not a
                              legal label start.
* ``LOOP``                 -- one single name path reaches the same position
                              twice.  Two *different* names sharing a tail are
                              independent and never trigger this.
* ``EXPANSION_EXCEEDED``   -- the name would need more than
                              ``max_expanded_bytes`` on the wire.
* ``JUMP_BUDGET_EXCEEDED`` -- the path takes more compression-pointer jumps
                              than the caller's ``jump_budget``.
"""

from __future__ import annotations

from typing import Iterable, Mapping, List, Optional, Tuple

from .model import IntegrityError, NameReport, NodeRecord, ResourceRecord


def validate_name_index(
    nodes: Mapping[int, NodeRecord],
    records: Iterable[ResourceRecord],
    *,
    domain_size: int,
    max_expanded_bytes: int,
    jump_budget: int,
) -> List[NameReport]:
    """Validate every name referenced by the resource-record index.

    Parameters
    ----------
    nodes:
        Node table keyed by wire position.
    records:
        Resource-record index.  One report is produced per distinct
        ``name_start``; records referencing the same start share a report.
    domain_size:
        Exclusive upper bound of the legal wire address domain.
    max_expanded_bytes:
        Caller-supplied expansion limit.  Every label costs
        ``1 + label_length`` bytes and the terminating root label costs 1.
    jump_budget:
        Caller-supplied maximum number of compression-pointer jumps per name.
        A successor is a jump when it differs from the physically adjacent
        position ``pos + 1 + label_length``.

    Returns
    -------
    list of :class:`NameReport`, ordered by first appearance of the referenced
    name in ``records``; ``affected_records`` keeps resource-record order.

    Raises
    ------
    ValueError
        For structurally impossible index content (negative lengths,
        non-root zero-length labels, invalid caller budgets).
    """

    if domain_size <= 0:
        raise ValueError("domain_size must be positive")
    if max_expanded_bytes < 1:
        raise ValueError("max_expanded_bytes must be at least 1 (root label)")
    if jump_budget < 0:
        raise ValueError("jump_budget must be non-negative")

    node_table = dict(nodes)
    for node in node_table.values():
        if node.label_length < 0:
            raise ValueError(
                f"node at {node.position} has negative label_length"
            )
        if node.label_length == 0 and node.next_position is not None:
            raise ValueError(
                f"root node at {node.position} must carry the termination "
                "marker (next_position=None)"
            )

    # Preserve first-seen order of names while grouping the records that
    # reference them; record order inside each group stays input order.
    first_seen: List[int] = []
    grouped: dict = {}
    for record in records:
        start = record.name_start
        if start not in grouped:
            grouped[start] = []
            first_seen.append(start)
        grouped[start].append(record.record_id)

    reports: List[NameReport] = []
    for name_start in first_seen:
        report = _validate_one_name(
            name_start=name_start,
            nodes=node_table,
            domain_size=domain_size,
            max_expanded_bytes=max_expanded_bytes,
            jump_budget=jump_budget,
            affected_records=tuple(grouped[name_start]),
        )
        reports.append(report)
    return reports


def _validate_one_name(
    *,
    name_start: int,
    nodes: Mapping[int, NodeRecord],
    domain_size: int,
    max_expanded_bytes: int,
    jump_budget: int,
    affected_records: Tuple[int, ...],
) -> NameReport:
    path: List[int] = []
    visited = set()
    expanded = 0
    jumps = 0
    pos = name_start

    while True:
        # 1. Address-domain check.
        if pos < 0 or pos >= domain_size:
            return _fail(
                name_start, affected_records, IntegrityError.OUT_OF_BOUNDS,
                pos, expanded, jumps, path,
            )

        # 2. Evidence check: an in-domain position the index cannot account for.
        node = nodes.get(pos)
        if node is None:
            return _fail(
                name_start, affected_records,
                IntegrityError.INCOMPLETE_EVIDENCE,
                pos, expanded, jumps, path,
            )

        # 3. Loop check: a single name path revisiting a position.  Tails shared
        #    between different names get separate traversals and never meet here.
        if pos in visited:
            return _fail(
                name_start, affected_records, IntegrityError.LOOP,
                pos, expanded, jumps, path,
            )

        # 4. Label-start check.
        if not node.is_label_start:
            return _fail(
                name_start, affected_records, IntegrityError.NON_LABEL_START,
                pos, expanded, jumps, path,
            )

        visited.add(pos)
        path.append(pos)

        # 5. Termination marker: root label, one trailing zero byte.
        if node.label_length == 0 and node.next_position is None:
            expanded += 1
            if expanded > max_expanded_bytes:
                return _fail(
                    name_start, affected_records,
                    IntegrityError.EXPANSION_EXCEEDED,
                    pos, expanded, jumps, path,
                )
            return NameReport(
                name_start=name_start,
                ok=True,
                affected_records=affected_records,
                error_code=None,
                error_position=None,
                expanded_bytes=expanded,
                jumps_used=jumps,
                path=tuple(path),
            )

        # 6. Ordinary label: account for length octet plus label bytes.
        expanded += 1 + node.label_length
        if expanded > max_expanded_bytes:
            return _fail(
                name_start, affected_records,
                IntegrityError.EXPANSION_EXCEEDED,
                pos, expanded, jumps, path,
            )

        successor = node.next_position
        if successor is None:
            # Non-root node carrying the termination marker: needed evidence is
            # missing, so the name cannot be expanded safely.
            return _fail(
                name_start, affected_records,
                IntegrityError.INCOMPLETE_EVIDENCE,
                pos, expanded, jumps, path,
            )

        # 7. A successor other than the physically adjacent wire position is a
        #    compression-pointer jump and consumes the caller's budget.
        physical_next = pos + 1 + node.label_length
        if successor != physical_next:
            jumps += 1
            if jumps > jump_budget:
                return _fail(
                    name_start, affected_records,
                    IntegrityError.JUMP_BUDGET_EXCEEDED,
                    pos, expanded, jumps, path,
                )

        pos = successor


def _fail(
    name_start: int,
    affected_records: Tuple[int, ...],
    error_code: IntegrityError,
    error_position: Optional[int],
    expanded: int,
    jumps: int,
    path: List[int],
) -> NameReport:
    return NameReport(
        name_start=name_start,
        ok=False,
        affected_records=affected_records,
        error_code=error_code,
        error_position=error_position,
        expanded_bytes=expanded,
        jumps_used=jumps,
        path=tuple(path),
    )
