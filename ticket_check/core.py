"""Consistency checking for a two-part paper ticket.

A physical ticket carries one random bit string ("the original string").
It is folded, and two parts -- a window part and a stub part -- each copy
a subset of the cells.  A fold scheme states, for every part position, which
original cell that position shows.  The observations from both parts are
therefore observations of the *same* original cells.

The original string is further known to satisfy a set of binary parity
checks (each check lists original cells whose XOR must equal a given bit).

This module answers, using linear algebra over GF(2) only:

* can the two parts come from one original string?
* if yes, how many original strings remain possible?
* which *unobserved* cells are nevertheless forced to a unique value?
* if no, which observations and checks form an irredundant contradictory set?

It also supports errata that replace an observation while the original
record is kept for inspection.

The whole module is pure Python, dependency free, and never enumerates
strings for the main analysis.  Exhaustive enumeration is offered separately
for small tickets so that counts can be cross-checked by brute force.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping, NamedTuple, Sequence, Union

__all__ = [
    "TicketError",
    "FoldScheme",
    "Check",
    "Observation",
    "Erratum",
    "ErratumRecord",
    "EffectiveObservation",
    "CertificateItem",
    "Contradiction",
    "AnalysisResult",
    "analyze_ticket",
    "enumerate_solutions",
    "count_solutions_by_enumeration",
]


class TicketError(ValueError):
    """Raised when a ticket description is malformed (not merely inconsistent)."""


# ---------------------------------------------------------------------------
# input primitives
# ---------------------------------------------------------------------------


def _require_int(value: object, where: str) -> int:
    if type(value) is not int:  # bool is deliberately rejected
        raise TicketError(f"{where}: expected int, got {value!r}")
    return value


def _require_bit(value: object, where: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if type(value) is not int or value not in (0, 1):
        allowed = "0, 1 or None" if allow_none else "0 or 1"
        raise TicketError(f"{where}: expected {allowed}, got {value!r}")


def _xor_bits(cells: Iterable[int]) -> int:
    bits = 0
    for cell in cells:
        bits ^= 1 << cell
    return bits


@dataclass(frozen=True)
class FoldScheme:
    """The folding plan.

    ``folds`` maps a ticket-part name (for example ``"window"`` or ``"stub"``)
    to a sequence whose *position* is the local cell number printed on that
    part and whose *value* is the original-string cell number it shows.
    A folding permutation can be supplied with :meth:`from_permutation`.
    """

    n_cells: int
    folds: Mapping[str, tuple[int, ...]]

    def __post_init__(self) -> None:
        _require_int(self.n_cells, "n_cells")
        if self.n_cells <= 0:
            raise TicketError("n_cells must be positive")
        if not isinstance(self.folds, Mapping) or not self.folds:
            raise TicketError("folds must be a non-empty mapping")
        normalized: dict[str, tuple[int, ...]] = {}
        for name, sequence in self.folds.items():
            if not isinstance(name, str) or not name:
                raise TicketError("fold names must be non-empty strings")
            try:
                cells = tuple(sequence)
            except TypeError as exc:
                raise TicketError(f"fold {name!r} is not a sequence") from exc
            if not cells:
                raise TicketError(f"fold {name!r} must show at least one cell")
            for cell in cells:
                _require_int(cell, f"fold {name!r} cell")
                if not 0 <= cell < self.n_cells:
                    raise TicketError(
                        f"fold {name!r} refers to cell {cell}, outside 0..{self.n_cells - 1}"
                    )
            normalized[name] = cells
        object.__setattr__(self, "folds", MappingProxyType(normalized))

    # -- convenient constructors -------------------------------------------

    @classmethod
    def opposing(cls, n_cells: int) -> "FoldScheme":
        """Window copies cells left-to-right, stub copies them right-to-left."""
        _require_int(n_cells, "n_cells")
        if n_cells <= 0:
            raise TicketError("n_cells must be positive")
        cells = tuple(range(n_cells))
        return cls(n_cells=n_cells, folds={"window": cells, "stub": tuple(reversed(cells))})

    @classmethod
    def from_permutation(
        cls,
        permutation: Sequence[int],
        *,
        window: Sequence[int] | None = None,
        names: tuple[str, str] = ("window", "stub"),
    ) -> "FoldScheme":
        """Build a scheme from the explicit cell permutation.

        ``permutation[i]`` is the window position seen at stub position ``i``.
        If ``window`` is omitted, window position ``i`` shows original cell ``i``.
        """
        try:
            perm = tuple(permutation)
        except TypeError as exc:
            raise TicketError("permutation must be a sequence") from exc
        for value in perm:
            _require_int(value, "permutation entry")
        if sorted(perm) != list(range(len(perm))):
            raise TicketError("permutation must contain each of 0..len(permutation)-1 exactly once")
        if not isinstance(names, tuple) or len(names) != 2 or not all(
            isinstance(n, str) and n for n in names
        ):
            raise TicketError("names must be two non-empty ticket names")
        if names[0] == names[1]:
            raise TicketError("ticket names must differ")

        if window is None:
            win = tuple(range(len(perm)))
        else:
            try:
                win = tuple(window)
            except TypeError as exc:
                raise TicketError("window must be a sequence") from exc
            if len(win) != len(perm):
                raise TicketError("window length must match permutation length")
            for cell in win:
                _require_int(cell, "window cell")
                if cell < 0:
                    raise TicketError("window cells must be non-negative")
            if len(set(win)) != len(win):
                raise TicketError("window refers to the same cell twice")

        if not win:
            raise TicketError("a fold scheme needs at least one cell")
        stub = tuple(win[index] for index in perm)
        n_cells = max(win) + 1
        return cls(n_cells=n_cells, folds={names[0]: win, names[1]: stub})

    # -- lookups -----------------------------------------------------------

    @property
    def tickets(self) -> tuple[str, ...]:
        return tuple(self.folds)

    def positions(self, ticket: str) -> int:
        try:
            return len(self.folds[ticket])
        except KeyError as exc:
            raise TicketError(f"unknown ticket-part {ticket!r}") from exc

    def cell_at(self, ticket: str, position: int) -> int:
        """Original cell shown at local ``position`` of ``ticket``."""
        try:
            cells = self.folds[ticket]
        except KeyError as exc:
            raise TicketError(f"unknown ticket-part {ticket!r}") from exc
        _require_int(position, f"{ticket} position")
        if not 0 <= position < len(cells):
            raise TicketError(
                f"{ticket} position {position} outside 0..{len(cells) - 1}"
            )
        return cells[position]


@dataclass(frozen=True)
class Check:
    """One binary parity check over original-string cells.

    The XOR of the listed cells must equal ``parity`` (default 0, i.e. even
    parity).  Cell numbers must be distinct within one check.
    """

    cells: tuple[int, ...]
    parity: int = 0
    label: str = ""

    def __post_init__(self) -> None:
        try:
            cells = tuple(self.cells)
        except TypeError as exc:
            raise TicketError("check cells must be a sequence") from exc
        object.__setattr__(self, "cells", cells)
        _require_bit(self.parity, "check parity")
        if not isinstance(self.label, str):
            raise TicketError("check label must be a string")


@dataclass(frozen=True)
class Observation:
    """What one part shows at one local position.

    ``value=None`` means the cell is present but unknown (an erasure).
    """

    ticket: str
    position: int
    value: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.ticket, str) or not self.ticket:
            raise TicketError("observation ticket must be a non-empty string")
        _require_int(self.position, "observation position")
        _require_bit(self.value, "observation value", allow_none=True)


@dataclass(frozen=True)
class Erratum:
    """A correction that replaces the effective value at one part position.

    ``value=None`` removes the observation (turns it into an erasure).
    The original observation record is never destroyed.
    """

    ticket: str
    position: int
    value: int | None
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.ticket, str) or not self.ticket:
            raise TicketError("erratum ticket must be a non-empty string")
        _require_int(self.position, "erratum position")
        _require_bit(self.value, "erratum value", allow_none=True)
        if not isinstance(self.note, str):
            raise TicketError("erratum note must be a string")


# ---------------------------------------------------------------------------
# textual evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CertificateItem:
    """One observation or check participating in a contradiction."""

    kind: str  # "observation" | "check"
    label: str
    cells: tuple[int, ...]
    value: int
    ticket: str | None = None
    position: int | None = None


@dataclass(frozen=True)
class Contradiction:
    """An irredundant set of records that cannot all hold.

    For both reasons the invariant is the same: the XOR of the left-hand
    sides over ``items`` is 0, and the XOR of the right-hand sides is 1.
    ``value_conflict`` is the special case of two different observations of
    one original cell (``x_c = 0`` and ``x_c = 1``).
    """

    reason: str  # "value_conflict" | "parity_conflict"
    items: tuple[CertificateItem, ...]
    xor_rhs: int

    def describe(self) -> str:
        if self.reason == "value_conflict":
            lines = ["two observations of the same original cell disagree:"]
        else:
            lines = [
                "these records are mutually contradictory: the XOR of their "
                "left-hand sides is 0 while the XOR of their right-hand sides is 1:"
            ]
        for item in self.items:
            if item.kind == "observation":
                lines.append(
                    f"  - observation {item.label} -> original cell "
                    f"{item.cells[0]} = {item.value}"
                )
            else:
                cells = ", ".join(str(c) for c in item.cells)
                lines.append(f"  - {item.label}: cells ({cells}) parity {item.value}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ErratumRecord:
    """Audit trail entry: an erratum plus what it superseded."""

    erratum: Erratum
    previous: int | None  # effective value immediately before this erratum
    original: int | None  # value in the original observation record, if any
    had_original: bool
    replaced: bool  # whether any effective observation existed before


@dataclass(frozen=True)
class EffectiveObservation:
    """A single part position after errata, next to its original record."""

    ticket: str
    position: int
    cell: int
    original: int | None
    had_original: bool
    effective: int | None
    amended: bool


@dataclass(frozen=True)
class AnalysisResult:
    compatible: bool
    n_cells: int
    rank: int | None
    free_variables: int | None
    solution_count: int | None
    observed_cells: Mapping[int, int]
    determined_unobserved: Mapping[int, int]
    undetermined_unobserved: tuple[int, ...]
    contradiction: Contradiction | None
    effective_observations: tuple[EffectiveObservation, ...]
    erratum_records: tuple[ErratumRecord, ...]

    @property
    def status(self) -> str:
        return "consistent" if self.compatible else "contradictory"

    @property
    def unobserved_cells(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.determined_unobserved) | set(self.undetermined_unobserved)))

    def describe(self) -> str:
        if not self.compatible:
            assert self.contradiction is not None
            return (
                f"the two parts cannot come from one original string "
                f"({self.contradiction.reason}):\n{self.contradiction.describe()}"
            )
        assert self.rank is not None and self.free_variables is not None
        parts = [
            f"{self.n_cells} original cells; rank {self.rank}; "
            f"{self.free_variables} free variable(s); "
            f"{self.solution_count} possible original string(s)"
        ]
        if self.determined_unobserved:
            fixed = ", ".join(
                f"{cell}={value}" for cell, value in sorted(self.determined_unobserved.items())
            )
            parts.append(f"unobserved but uniquely fixed: {fixed}")
        else:
            parts.append("no unobserved cell is uniquely fixed")
        if self.undetermined_unobserved:
            cells = ", ".join(str(c) for c in self.undetermined_unobserved)
            parts.append(f"still undetermined: {cells}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# internal linear algebra
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Equation:
    lhs: int
    rhs: int
    kind: str
    label: str
    cells: tuple[int, ...]
    ticket: str | None = None
    position: int | None = None


class _Pivot(NamedTuple):
    lhs: int
    rhs: int
    comb: int  # bit i set => original equation i participates


def _certificate_item(equation: _Equation) -> CertificateItem:
    return CertificateItem(
        kind=equation.kind,
        label=equation.label,
        cells=equation.cells,
        value=equation.rhs,
        ticket=equation.ticket,
        position=equation.position,
    )


def _eliminate(equations: Sequence[_Equation]) -> tuple[dict[int, _Pivot], int, int | None]:
    """Forward-eliminate; return pivots, rank and a contradictory row mask."""
    pivots: dict[int, _Pivot] = {}
    rank = 0
    for index, equation in enumerate(equations):
        lhs, rhs, comb = equation.lhs, equation.rhs, 1 << index
        pivot_bit = -1
        while lhs:
            pivot_bit = (lhs & -lhs).bit_length() - 1
            pivot = pivots.get(pivot_bit)
            if pivot is None:
                break
            lhs ^= pivot.lhs
            rhs ^= pivot.rhs
            comb ^= pivot.comb
        if lhs == 0:
            if rhs:
                return pivots, rank, comb
            continue
        pivots[pivot_bit] = _Pivot(lhs, rhs, comb)
        rank += 1
    return pivots, rank, None


def _reduce_to_irredundant(
    equations: Sequence[_Equation], mask: int, n_cells: int
) -> tuple[_Equation, ...]:
    """Shrink a contradictory row set to one that is irredundant.

    View each row as the vector ``(lhs, rhs)``.  A subset is contradictory
    exactly when its vectors sum to ``(0, 1)``.  A contradictory set is
    irredundant exactly when no nonempty sub-collection sums to ``(0, 0)``,
    i.e. when the vectors are linearly independent.  (If a nonempty ``U``
    summed to ``(0, 0)``, dropping ``U`` would leave a still-contradictory
    set; conversely a proper contradictory subset has a zero-sum complement.)

    So we repeatedly find a nonempty zero-sum sub-collection and drop it,
    which preserves the ``(0, 1)`` total and strictly shrinks the set.
    """
    selected = [index for index in range(len(equations)) if mask >> index & 1]
    bit = n_cells  # the rhs lives one bit above the highest cell

    while True:
        pivots: dict[int, tuple[int, int]] = {}  # leading bit -> (combined, comb)
        dependency: int | None = None
        for position, index in enumerate(selected):
            combined = equations[index].lhs | (equations[index].rhs << bit)
            comb = 1 << position
            inserted = False
            while combined:
                lead = combined.bit_length() - 1
                existing = pivots.get(lead)
                if existing is None:
                    pivots[lead] = (combined, comb)
                    inserted = True
                    break
                combined ^= existing[0]
                comb ^= existing[1]
            if not inserted:
                # The row is a combination of earlier ones summing to (0, 0).
                dependency = comb
                break
        if dependency is None:
            return tuple(equations[index] for index in selected)
        # Drop the dependent sub-collection (its rows sum to (0, 0)); the
        # remaining rows still sum to (0, 1) because we removed a zero vector.
        removed = {
            position for position in range(len(selected)) if dependency >> position & 1
        }
        selected = [
            index for position, index in enumerate(selected) if position not in removed
        ]


def _forced_value(pivots: Mapping[int, _Pivot], cell: int) -> int | None:
    """Return the forced value of ``cell`` or None if it is still free."""
    vector = 1 << cell
    value = 0
    while vector:
        pivot_bit = (vector & -vector).bit_length() - 1
        pivot = pivots.get(pivot_bit)
        if pivot is None:
            return None
        vector ^= pivot.lhs
        value ^= pivot.rhs
    return value


# ---------------------------------------------------------------------------
# system construction
# ---------------------------------------------------------------------------


def _validate_checks(scheme: FoldScheme, checks: Iterable[Union[Check, Sequence[int]]]) -> tuple[Check, ...]:
    validated: list[Check] = []
    for index, raw in enumerate(checks):
        if isinstance(raw, Check):
            cells = tuple(raw.cells)
            parity = raw.parity
            label = raw.label
        else:
            try:
                cells = tuple(raw)
            except TypeError as exc:
                raise TicketError(
                    f"check #{index} must be a Check or a sequence of cell numbers"
                ) from exc
            parity = 0
            label = ""
        for cell in cells:
            _require_int(cell, f"check #{index} cell")
            if not 0 <= cell < scheme.n_cells:
                raise TicketError(
                    f"check #{index} refers to cell {cell}, outside 0..{scheme.n_cells - 1}"
                )
        if len(set(cells)) != len(cells):
            raise TicketError(f"check #{index} lists the same cell more than once")
        validated.append(Check(cells=cells, parity=parity, label=label or f"check#{index}"))
    return tuple(validated)


def _build_system(
    scheme: FoldScheme,
    checks: Iterable[Union[Check, Sequence[int]]],
    observations: Iterable[Observation],
    errata: Iterable[Erratum],
) -> tuple[
    FoldScheme,
    list[_Equation],
    dict[tuple[str, int], int | None],
    dict[tuple[str, int], int | None],
    dict[tuple[str, int], int],
    tuple[ErratumRecord, ...],
]:
    if not isinstance(scheme, FoldScheme):
        raise TicketError("scheme must be a FoldScheme")
    validated_checks = _validate_checks(scheme, checks)

    original: dict[tuple[str, int], int | None] = {}
    effective: dict[tuple[str, int], int | None] = {}
    cell_of: dict[tuple[str, int], int] = {}

    for index, observation in enumerate(observations):
        if not isinstance(observation, Observation):
            raise TicketError(f"observation #{index} must be an Observation")
        key = (observation.ticket, observation.position)
        if key in original:
            raise TicketError(f"duplicate observation for {observation.ticket}[{observation.position}]")
        cell = scheme.cell_at(observation.ticket, observation.position)
        original[key] = observation.value
        effective[key] = observation.value
        cell_of[key] = cell

    records: list[ErratumRecord] = []
    for index, erratum in enumerate(errata):
        if not isinstance(erratum, Erratum):
            raise TicketError(f"erratum #{index} must be an Erratum")
        key = (erratum.ticket, erratum.position)
        cell = scheme.cell_at(erratum.ticket, erratum.position)
        if key in cell_of and cell_of[key] != cell:  # pragma: no cover - scheme is deterministic
            raise TicketError("inconsistent original cell mapping")
        cell_of.setdefault(key, cell)
        previous = effective.get(key) if key in effective else None
        had_effective = key in effective
        records.append(
            ErratumRecord(
                erratum=erratum,
                previous=previous,
                original=original.get(key) if key in original else None,
                had_original=key in original,
                replaced=had_effective,
            )
        )
        effective[key] = erratum.value

    equations: list[_Equation] = []
    for key in sorted(effective):
        value = effective[key]
        if value is None:
            continue
        cell = cell_of[key]
        equations.append(
            _Equation(
                lhs=1 << cell,
                rhs=value,
                kind="observation",
                label=f"{key[0]}[{key[1]}]",
                cells=(cell,),
                ticket=key[0],
                position=key[1],
            )
        )
    for check in validated_checks:
        equations.append(
            _Equation(
                lhs=_xor_bits(check.cells),
                rhs=check.parity,
                kind="check",
                label=check.label,
                cells=check.cells,
            )
        )
    return scheme, equations, original, effective, cell_of, tuple(records)


def _observation_records(
    original: Mapping[tuple[str, int], int | None],
    effective: Mapping[tuple[str, int], int | None],
    cell_of: Mapping[tuple[str, int], int],
    records: Sequence[ErratumRecord],
) -> tuple[EffectiveObservation, ...]:
    amended_keys = {(record.erratum.ticket, record.erratum.position) for record in records}
    return tuple(
        EffectiveObservation(
            ticket=key[0],
            position=key[1],
            cell=cell_of[key],
            original=original.get(key) if key in original else None,
            had_original=key in original,
            effective=effective[key],
            amended=key in amended_keys,
        )
        for key in sorted(effective)
    )


def analyze_ticket(
    scheme: FoldScheme,
    checks: Iterable[Union[Check, Sequence[int]]],
    observations: Iterable[Observation] = (),
    errata: Iterable[Erratum] = (),
) -> AnalysisResult:
    """Check whether the two ticket parts can come from one original string.

    Returns a frozen :class:`AnalysisResult`.  This never enumerates strings:
    the count is ``2 ** (n_cells - rank)`` once the system is consistent.
    """
    scheme, equations, original, effective, cell_of, records = _build_system(
        scheme, checks, observations, errata
    )
    observed = {
        cell_of[key]: value
        for key in sorted(effective)
        if (value := effective[key]) is not None
    }
    effective_obs = _observation_records(original, effective, cell_of, records)

    def make(
        compatible: bool,
        *,
        rank: int | None,
        free_variables: int | None,
        solution_count: int | None,
        determined: Mapping[int, int] | None = None,
        undetermined: tuple[int, ...] = (),
        contradiction: Contradiction | None = None,
    ) -> AnalysisResult:
        return AnalysisResult(
            compatible=compatible,
            n_cells=scheme.n_cells,
            rank=rank,
            free_variables=free_variables,
            solution_count=solution_count,
            observed_cells=dict(observed),
            determined_unobserved=dict(determined or {}),
            undetermined_unobserved=undetermined,
            contradiction=contradiction,
            effective_observations=effective_obs,
            erratum_records=records,
        )

    # Two observations of one cell with different values: clearest certificate.
    first_by_cell: dict[int, _Equation] = {}
    for equation in equations:
        if equation.kind != "observation":
            continue
        cell = equation.cells[0]
        previous = first_by_cell.get(cell)
        if previous is not None and previous.rhs != equation.rhs:
            contradiction = Contradiction(
                reason="value_conflict",
                items=(_certificate_item(previous), _certificate_item(equation)),
                xor_rhs=1,
            )
            return make(False, rank=None, free_variables=None, solution_count=None, contradiction=contradiction)
        first_by_cell.setdefault(cell, equation)

    pivots, rank, conflict_mask = _eliminate(equations)
    if conflict_mask is not None:
        minimal = _reduce_to_irredundant(equations, conflict_mask, scheme.n_cells)
        items = tuple(_certificate_item(equation) for equation in minimal)
        xor_rhs = 0
        for item in items:
            xor_rhs ^= item.value
        contradiction = Contradiction(reason="parity_conflict", items=items, xor_rhs=xor_rhs)
        return make(False, rank=None, free_variables=None, solution_count=None, contradiction=contradiction)

    free_variables = scheme.n_cells - rank
    determined: dict[int, int] = {}
    undetermined: list[int] = []
    for cell in range(scheme.n_cells):
        if cell in observed:
            continue
        value = _forced_value(pivots, cell)
        if value is None:
            undetermined.append(cell)
        else:
            determined[cell] = value

    return make(
        True,
        rank=rank,
        free_variables=free_variables,
        solution_count=1 << free_variables,
        determined=determined,
        undetermined=tuple(undetermined),
    )


# ---------------------------------------------------------------------------
# exhaustive cross-checking for small tickets
# ---------------------------------------------------------------------------


def _enumeration_equations(
    scheme: FoldScheme,
    checks: Iterable[Union[Check, Sequence[int]]],
    observations: Iterable[Observation],
    errata: Iterable[Erratum],
) -> tuple[FoldScheme, list[tuple[int, int]]]:
    scheme, equations, *_ = _build_system(scheme, checks, observations, errata)
    return scheme, [(equation.lhs, equation.rhs) for equation in equations]


def enumerate_solutions(
    scheme: FoldScheme,
    checks: Iterable[Union[Check, Sequence[int]]],
    observations: Iterable[Observation] = (),
    errata: Iterable[Erratum] = (),
    *,
    max_cells: int = 20,
) -> Iterator[dict[int, int]]:
    """Yield every original string satisfying the system, by brute force.

    Meant for small tickets only; ``n_cells`` must not exceed ``max_cells``.
    """
    scheme, equations = _enumeration_equations(scheme, checks, observations, errata)
    n_cells = scheme.n_cells
    if n_cells > max_cells:
        raise TicketError(
            f"exhaustive enumeration needs n_cells <= {max_cells}, got {n_cells}"
        )
    for mask in range(1 << n_cells):
        for lhs, rhs in equations:
            if ((mask & lhs).bit_count() & 1) != rhs:
                break
        else:
            yield {cell: (mask >> cell) & 1 for cell in range(n_cells)}


def count_solutions_by_enumeration(
    scheme: FoldScheme,
    checks: Iterable[Union[Check, Sequence[int]]],
    observations: Iterable[Observation] = (),
    errata: Iterable[Erratum] = (),
    *,
    max_cells: int = 20,
) -> int:
    """Brute-force count of satisfying original strings (small tickets only)."""
    return sum(1 for _ in enumerate_solutions(
        scheme, checks, observations, errata, max_cells=max_cells
    ))
