"""Symbolic execution over bounded integer registers.

Each register value is represented as a piecewise function of the entry
state.  A :class:`State` keeps one common partition of the entry-state box;
for every partition piece (an axis-aligned integer box) it stores one
expression per register.  Expressions are either constants
(``("const", value)``) or a single entry register plus a constant offset
(``("var", register, offset)``); the two-tuple form ``("var", r)`` is a
shorthand for offset zero.  These are the only shapes the four allowed
operation kinds can produce.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from .model import ASSIGN, SADD, SWAP, Declaration, Operation

# An expression is ("const", value) or ("var", register_index[, offset]).
Expr = Tuple
Box = Tuple[Tuple[int, int], ...]
Piece = Tuple[Box, Tuple[Expr, ...]]


def _var(register: int, offset: int = 0) -> Expr:
    return ("var", register, offset)


def _expr_kind(expr: Expr) -> str:
    return expr[0]


def _expr_value(expr: Expr):
    """Return the constant value, or (register, offset) for a variable."""
    if expr[0] == "const":
        return expr[1]
    register = expr[1]
    offset = expr[2] if len(expr) == 3 else 0
    return register, offset


def initial_state(declaration: Declaration) -> "State":
    """The identity mapping: every output register reads its entry register."""
    box = tuple((lo, hi) for lo, hi in declaration.registers)
    exprs = tuple(_var(i) for i in range(declaration.nregs))
    return State(declaration, ((box, exprs),))


class State:
    """Piecewise register mapping over the full entry-state space."""

    __slots__ = ("declaration", "pieces")

    def __init__(self, declaration: Declaration, pieces: Sequence[Piece]):
        self.declaration = declaration
        self.pieces: Tuple[Piece, ...] = tuple(pieces)

    # ------------------------------------------------------------------ #
    # Basic structure
    # ------------------------------------------------------------------ #
    def key(self) -> Tuple[Piece, ...]:
        return self.pieces

    def eval_at(self, entry: Sequence[int]) -> Tuple[Expr, ...]:
        """Return the output-expression tuple covering one concrete state."""
        for box, exprs in self.pieces:
            if all(lo <= value <= hi for value, (lo, hi) in zip(entry, box)):
                return exprs
        raise ValueError("entry state is outside the declared register bounds")

    def concrete(self, entry: Sequence[int]) -> Tuple[int, ...]:
        """Fully concrete evaluation at one entry state."""
        return tuple(
            _eval_expr(expr, entry) for expr in self.eval_at(entry)
        )

    # ------------------------------------------------------------------ #
    # Canonicalisation
    # ------------------------------------------------------------------ #
    def canonical(self) -> "State":
        """Merge adjacent pieces with identical expression tuples.

        Deterministic work-list algorithm: repeatedly pick the lowest-index
        pair of pieces that touch on exactly one axis and agree on every
        output expression, merge it, and restart until no such pair exists.
        """
        pieces = [(tuple(box), tuple(exprs)) for box, exprs in self.pieces]
        nregs = self.declaration.nregs
        while True:
            merge_index = None
            merge_axis = -1
            for i in range(len(pieces)):
                box_i, expr_i = pieces[i]
                for j in range(i + 1, len(pieces)):
                    box_j, expr_j = pieces[j]
                    if expr_i != expr_j:
                        continue
                    for axis in range(nregs):
                        (lo_i, hi_i), (lo_j, hi_j) = box_i[axis], box_j[axis]
                        same_elsewhere = all(
                            box_i[k] == box_j[k]
                            for k in range(nregs)
                            if k != axis
                        )
                        if same_elsewhere and (
                            hi_i + 1 == lo_j or hi_j + 1 == lo_i
                        ):
                            merge_index = (i, j)
                            merge_axis = axis
                            break
                    if merge_index is not None:
                        break
                if merge_index is not None:
                    break
            if merge_index is None:
                break
            i, j = merge_index
            box_i, expr_i = pieces[i]
            box_j, _ = pieces[j]
            merged_box = list(box_i)
            merged_box[merge_axis] = (
                min(box_i[merge_axis][0], box_j[merge_axis][0]),
                max(box_i[merge_axis][1], box_j[merge_axis][1]),
            )
            pieces[i] = (tuple(merged_box), expr_i)
            del pieces[j]
        pieces.sort(key=_piece_sort_key)
        return State(self.declaration, pieces)

    # ------------------------------------------------------------------ #
    # Operations
    # ------------------------------------------------------------------ #
    def apply(self, operation: Operation) -> "State":
        if operation.kind == ASSIGN:
            return self._apply_assign(operation)
        if operation.kind == SWAP:
            return self._apply_swap(operation)
        if operation.kind == SADD:
            return self._apply_sadd(operation)
        return self._apply_cond(operation)

    def _apply_assign(self, op: Operation) -> "State":
        new_pieces = [
            (box, _replace_expr(exprs, op.target, ("const", op.constant)))
            for box, exprs in self.pieces
        ]
        return State(self.declaration, new_pieces).canonical()

    def _apply_swap(self, op: Operation) -> "State":
        a, b = op.target, op.source
        new_pieces = [
            (box, _swap_expr(exprs, a, b)) for box, exprs in self.pieces
        ]
        return State(self.declaration, new_pieces).canonical()

    def _apply_sadd(self, op: Operation) -> "State":
        new_pieces: List[Piece] = []
        for box, exprs in self.pieces:
            new_pieces.extend(
                _sadd_on_piece(
                    box, exprs, op.target, op.constant,
                    self.declaration.bounds(op.target),
                )
            )
        return State(self.declaration, new_pieces).canonical()

    def _apply_cond(self, op: Operation) -> "State":
        # The guard reads the *current* value of the source register, which
        # is itself an expression.  Refine every piece along the entry
        # variable backing that expression, or decide outright for constants.
        refined: List[Tuple[bool, Box, Tuple[Expr, ...]]] = []
        for box, exprs in self.pieces:
            source_expr = exprs[op.source]
            if source_expr[0] == "const":
                refined.append(
                    (op.low <= source_expr[1] <= op.high, box, exprs)
                )
                continue
            register, offset = _expr_value(source_expr)
            # Guard: low <= x_register + offset <= high
            for sub in _cut_box(box, register, op.low - offset, op.high - offset):
                sub_lo, sub_hi = sub[register]
                inside = (
                    op.low <= sub_lo + offset
                    and sub_hi + offset <= op.high
                )
                refined.append((inside, sub, exprs))
        new_pieces: List[Piece] = []
        bounds = self.declaration.bounds(op.target)
        for inside, box, exprs in refined:
            if not inside:
                new_pieces.append((box, exprs))
                continue
            if op.action == ASSIGN:
                new_pieces.append(
                    (box, _replace_expr(exprs, op.target, ("const", op.constant)))
                )
            else:  # conditional saturating add
                new_pieces.extend(
                    _sadd_on_piece(box, exprs, op.target, op.constant, bounds)
                )
        return State(self.declaration, new_pieces).canonical()

    # ------------------------------------------------------------------ #
    # Observation and equivalence
    # ------------------------------------------------------------------ #
    def project(self, registers: Sequence[int]) -> "State":
        """Keep only the observed register expressions (for barriers)."""
        registers = tuple(registers)
        pieces = [(box, tuple(exprs[r] for r in registers)) for box, exprs in self.pieces]
        return State(self.declaration, pieces).canonical()

    def observation_boxes(self, registers: Sequence[int]):
        """Yield (entry-box, observed expression tuple) for a barrier."""
        projected = self.project(registers)
        return list(projected.pieces)

    def divergence_boxes(self, other: "State") -> List[Box]:
        """Return entry boxes on which the two mappings disagree somewhere."""
        if self.declaration.registers != other.declaration.registers:
            raise ValueError("states belong to different declarations")
        disagreements: List[Box] = []
        for box_a, exprs_a in self.pieces:
            for box_b, exprs_b in other.pieces:
                overlap = _box_overlap(box_a, box_b)
                if overlap is None:
                    continue
                if exprs_a != exprs_b:
                    disagreements.append(overlap)
        return _merge_boxes(disagreements, self.declaration.nregs)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _piece_sort_key(piece: Piece):
    box, exprs = piece
    return tuple(interval for interval in box), tuple(exprs)


def _replace_expr(exprs: Tuple[Expr, ...], index: int, new_expr: Expr) -> Tuple[Expr, ...]:
    values = list(exprs)
    values[index] = new_expr
    return tuple(values)


def _swap_expr(exprs: Tuple[Expr, ...], a: int, b: int) -> Tuple[Expr, ...]:
    values = list(exprs)
    values[a], values[b] = values[b], values[a]
    return tuple(values)


def _clamp(value: int, lo: int, hi: int) -> int:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _eval_expr(expr: Expr, entry: Sequence[int]) -> int:
    if expr[0] == "const":
        return expr[1]
    register = expr[1]
    offset = expr[2] if len(expr) == 3 else 0
    return entry[register] + offset


def _sadd_on_piece(
    box: Box,
    exprs: Tuple[Expr, ...],
    target: int,
    delta: int,
    bounds: Tuple[int, int],
) -> List[Piece]:
    """Apply saturating ``+delta`` to one piece, honouring expression offsets."""
    bound_lo, bound_hi = bounds
    target_expr = exprs[target]
    if target_expr[0] == "const":
        return [
            (
                box,
                _replace_expr(
                    exprs,
                    target,
                    ("const", _clamp(target_expr[1] + delta, bound_lo, bound_hi)),
                ),
            )
        ]
    register, offset = _expr_value(target_expr)
    lo, hi = box[register]
    # Effective expression is x_register + offset; work in x_register space.
    slices = _sadd_slices(lo, hi, offset, delta, bound_lo, bound_hi, register)
    result: List[Piece] = []
    for sub_lo, sub_hi, result_expr in slices:
        sub_box = list(box)
        sub_box[register] = (sub_lo, sub_hi)
        result.append((tuple(sub_box), _replace_expr(exprs, target, result_expr)))
    return result


def _sadd_slices(
    lo: int,
    hi: int,
    offset: int,
    delta: int,
    bound_lo: int,
    bound_hi: int,
    register: int,
):
    """Slices for ``clamp(x + offset + delta)`` over x in [lo, hi].

    Linear while ``bound_lo <= x + offset + delta <= bound_hi``, i.e.
    ``bound_lo - offset - delta <= x <= bound_hi - offset - delta``.
    Emits constant pieces at the two bounds and an offset variable piece in
    the linear middle.
    """
    shift = offset + delta
    linear_lo_full = bound_lo - shift
    linear_hi_full = bound_hi - shift
    result = []
    low_lo, low_hi = lo, min(hi, linear_lo_full - 1)
    if low_lo <= low_hi:
        result.append((low_lo, low_hi, ("const", bound_lo)))
    mid_lo, mid_hi = max(lo, linear_lo_full), min(hi, linear_hi_full)
    if mid_lo <= mid_hi:
        result.append((mid_lo, mid_hi, ("var", register, shift)))
    high_lo, high_hi = max(lo, linear_hi_full + 1), hi
    if high_lo <= high_hi:
        result.append((high_lo, high_hi, ("const", bound_hi)))
    if not result:  # pragma: no cover - defensive; one region always exists
        raise RuntimeError("sadd produced no slices")
    return result


def _cut_box(box: Box, axis: int, low: int, high: int):
    """Split a box along one axis into below/inside/above an interval."""
    lo, hi = box[axis]
    ranges = []
    if lo < low:
        ranges.append((lo, min(hi, low - 1)))
    inside_lo = max(lo, low)
    inside_hi = min(hi, high)
    if inside_lo <= inside_hi:
        ranges.append((inside_lo, inside_hi))
    if hi > high:
        ranges.append((max(lo, high + 1), hi))
    for r_lo, r_hi in ranges:
        sub = list(box)
        sub[axis] = (r_lo, r_hi)
        yield tuple(sub)


def _box_overlap(box_a: Box, box_b: Box) -> Optional[Box]:
    overlap = []
    for (lo_a, hi_a), (lo_b, hi_b) in zip(box_a, box_b):
        lo, hi = max(lo_a, lo_b), min(hi_a, hi_b)
        if lo > hi:
            return None
        overlap.append((lo, hi))
    return tuple(overlap)


def _merge_boxes(boxes: List[Box], nregs: int) -> List[Box]:
    """Merge touching boxes; used only to keep counterexample boxes tidy."""
    if not boxes:
        return []
    boxes = [tuple(box) for box in boxes]
    while True:
        merge_index = None
        merge_axis = -1
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                box_i, box_j = boxes[i], boxes[j]
                for axis in range(nregs):
                    (lo_i, hi_i), (lo_j, hi_j) = box_i[axis], box_j[axis]
                    same_elsewhere = all(
                        box_i[k] == box_j[k]
                        for k in range(nregs)
                        if k != axis
                    )
                    if same_elsewhere and (
                        hi_i + 1 == lo_j or hi_j + 1 == lo_i
                    ):
                        merge_index = (i, j)
                        merge_axis = axis
                        break
                if merge_index is not None:
                    break
            if merge_index is not None:
                break
        if merge_index is None:
            break
        i, j = merge_index
        merged = list(boxes[i])
        merged[merge_axis] = (
            min(boxes[i][merge_axis][0], boxes[j][merge_axis][0]),
            max(boxes[i][merge_axis][1], boxes[j][merge_axis][1]),
        )
        boxes[i] = tuple(merged)
        del boxes[j]
    boxes.sort()
    return boxes


def lex_smallest_point(box: Box) -> Tuple[int, ...]:
    """The lexicographically smallest integer point inside a box."""
    return tuple(lo for lo, _ in box)


def execute_symbolic(declaration: Declaration, operations: Sequence[int]) -> State:
    """Apply a sequence of declared operation indices to the identity state."""
    state = initial_state(declaration)
    for op_index in operations:
        state = state.apply(declaration.operations[op_index])
    return state


def execute_concrete(
    declaration: Declaration, entry: Sequence[int], operations: Sequence[int],
    validate: bool = True,
) -> Tuple[int, ...]:
    """Reference concrete semantics, independent of the symbolic engine."""
    values = list(entry)
    if validate:
        for r, (lo, hi) in enumerate(declaration.registers):
            if not lo <= values[r] <= hi:
                raise ValueError("entry state outside register bounds")
    for op_index in operations:
        op = declaration.operations[op_index]
        if op.kind == ASSIGN:
            values[op.target] = op.constant
        elif op.kind == SWAP:
            values[op.target], values[op.source] = values[op.source], values[op.target]
        elif op.kind == SADD:
            lo, hi = declaration.bounds(op.target)
            values[op.target] = _clamp(values[op.target] + op.constant, lo, hi)
        else:
            if op.low <= values[op.source] <= op.high:
                lo, hi = declaration.bounds(op.target)
                if op.action == ASSIGN:
                    values[op.target] = op.constant
                else:
                    values[op.target] = _clamp(
                        values[op.target] + op.constant, lo, hi
                    )
    return tuple(values)
