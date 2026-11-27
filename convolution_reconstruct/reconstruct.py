"""Core implementation of the modular convolution reconstructor.

Notation
--------
* ``n``           -- length of each input sequence (output length is
  ``2*n - 1``).
* ``abs_bound_a`` / ``abs_bound_b`` -- non-negative bounds such that
  every input entry satisfies ``|a_i| <= abs_bound_a`` and
  ``|b_j| <= abs_bound_b``.
* At output position ``k`` the exact number of summed products is
  ``t(k) = #{(i, j) : i + j = k, 0 <= i, j < n}``, so every coefficient
  lies in ``[-B_k, B_k]`` with ``B_k = t(k) * abs_bound_a * abs_bound_b``.

All arithmetic uses Python's arbitrary-precision integers, so
coefficients beyond 64 bits are handled without overflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import gcd
from typing import Iterable, Mapping, Sequence

__all__ = [
    "BlockReport",
    "ConflictRecord",
    "CongruenceConflict",
    "ConvolutionReconstructor",
    "PositionState",
    "coefficient_bound",
    "coefficient_bounds",
    "direct_convolution",
    "factorize",
    "merge_congruence",
    "prime_power_lcm",
    "reconstruct_convolution",
    "signed_residue",
    "term_count",
]


# ---------------------------------------------------------------------------
# Geometry of an integer convolution
# ---------------------------------------------------------------------------

def term_count(k: int, n: int) -> int:
    """Number of index pairs ``(i, j)`` with ``i + j == k`` and ``i, j < n``.

    Equals ``k + 1`` on the rising edge (``k < n``) and ``2*n - 1 - k`` on
    the falling edge.  Positions outside ``[0, 2*n - 2]`` participate in no
    product at all.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if k < 0 or k > 2 * n - 2:
        return 0
    if k < n:
        return k + 1
    return 2 * n - 1 - k


def coefficient_bounds(
    n: int, abs_bound_a: int, abs_bound_b: int
) -> list[int]:
    """Per-position coefficient bounds ``B_k`` for every output position."""
    if n <= 0:
        raise ValueError("n must be positive")
    if abs_bound_a < 0 or abs_bound_b < 0:
        raise ValueError("absolute bounds must be non-negative")
    product = abs_bound_a * abs_bound_b
    return [term_count(k, n) * product for k in range(2 * n - 1)]


def coefficient_bound(
    k: int, n: int, abs_bound_a: int, abs_bound_b: int
) -> int:
    """Coefficient bound at a single output position."""
    if term_count(k, n) == 0:
        raise IndexError(f"position {k} is outside the convolution of length {n}")
    return term_count(k, n) * abs_bound_a * abs_bound_b


def direct_convolution(a: Sequence[int], b: Sequence[int]) -> list[int]:
    """Plain O(n^2) integer convolution, used as the ground-truth oracle."""
    if len(a) != len(b):
        raise ValueError("input sequences must have equal length")
    n = len(a)
    out = [0] * (2 * n - 1)
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            out[i + j] += x * y
    return out


# ---------------------------------------------------------------------------
# Number-theoretic primitives: factorization, lcm, generalized CRT
# ---------------------------------------------------------------------------

def factorize(m: int) -> dict[int, int]:
    """Return the prime factorization ``{prime: exponent}`` of ``m >= 2``."""
    if not isinstance(m, int) or m < 2:
        raise ValueError("factorize expects an integer >= 2")
    factors: dict[int, int] = {}
    x = m
    p = 2
    while p * p <= x:
        while x % p == 0:
            factors[p] = factors.get(p, 0) + 1
            x //= p
        p = 3 if p == 2 else p + 2
    if x > 1:
        factors[x] = factors.get(x, 0) + 1
    return factors


def prime_power_lcm(factors: Mapping[int, int]) -> int:
    """Multiply the maximal prime powers back into an integer (the lcm)."""
    result = 1
    for prime, exponent in factors.items():
        result *= prime**exponent
    return result


def merge_congruence(
    r1: int, m1: int, r2: int, m2: int
) -> tuple[int, int] | None:
    """Merge ``x ≡ r1 (mod m1)`` and ``x ≡ r2 (mod m2)``.

    Uses the generalized CRT, so ``m1`` and ``m2`` need not be coprime.
    Returns ``(r, lcm(m1, m2))`` when the constraints are compatible,
    where ``r`` is the normalized residue in ``[0, lcm)``.  Returns
    ``None`` when the two congruences contradict each other, i.e.
    ``r1 != r2 (mod gcd(m1, m2))``.
    """
    g = gcd(m1, m2)
    diff = (r2 - r1) % m2
    if diff % g != 0:
        return None
    if m1 == 1:
        return r2 % m2, m2
    if m2 == 1:
        return r1 % m1, m1
    lcm = m1 // g * m2
    # x = r1 + m1 * t, with t solving (m1/g) * t ≡ (r2-r1)/g (mod m2/g).
    t = ((diff // g) * pow(m1 // g, -1, m2 // g)) % (m2 // g)
    r = (r1 + m1 * t) % lcm
    return r, lcm


def signed_residue(residue: int, modulus: int) -> int:
    """Lift a normalized residue in ``[0, modulus)`` to ``(-m/2, m/2]``."""
    if modulus <= 0:
        raise ValueError("modulus must be positive")
    r = residue % modulus
    if r > modulus // 2:
        r -= modulus
    return r


# ---------------------------------------------------------------------------
# Per-position and per-block data containers
# ---------------------------------------------------------------------------

@dataclass
class PositionState:
    """Everything known about one output position."""

    position: int
    bound: int
    residue: int = 0          # normalized residue modulo effective_modulus
    effective_modulus: int = 1
    factors: dict[int, int] = field(default_factory=dict)
    # prime -> modulus that supplied the strongest (max-exponent) power
    factor_owners: dict[int, int] = field(default_factory=dict)
    source_moduli: list[int] = field(default_factory=list)
    published: bool = False

    def is_determined(self) -> bool:
        """True exactly when one signed integer in [-bound, bound] fits."""
        return self.effective_modulus > 2 * self.bound

    def value(self) -> int | None:
        """The unique coefficient once determined, otherwise ``None``."""
        if not self.is_determined():
            return None
        return signed_residue(self.residue, self.effective_modulus)


@dataclass(frozen=True)
class ConflictRecord:
    """A contradiction between a late block and an accepted constraint."""

    position: int
    modulus: int
    incoming_residue: int
    existing_residue: int
    existing_modulus: int
    gcd: int
    difference: int


class CongruenceConflict(Exception):
    """Raised when an incoming block contradicts an accepted residue.

    The whole block is rejected atomically; nothing is merged.  ``conflicts``
    contains every position that failed, each localized to the offending
    modulus and position.
    """

    def __init__(self, conflicts: Sequence[ConflictRecord]):
        self.conflicts: list[ConflictRecord] = list(conflicts)
        first = self.conflicts[0]
        super().__init__(
            "congruence conflict at position "
            f"{first.position} for modulus {first.modulus}: "
            f"incoming {first.incoming_residue} vs existing "
            f"{first.existing_residue} (mod {first.existing_modulus}), "
            f"gcd={first.gcd}"
        )


@dataclass(frozen=True)
class BlockReport:
    """Summary returned after a block has been accepted and merged."""

    modulus: int
    applied_positions: tuple[int, ...]
    new_factor_positions: dict[int, tuple[int, ...]]
    duplicated_factor_positions: dict[int, tuple[int, ...]]
    newly_determined_positions: tuple[int, ...]
    conflicts: tuple[ConflictRecord, ...] = ()


# ---------------------------------------------------------------------------
# The reconstructor
# ---------------------------------------------------------------------------

class ConvolutionReconstructor:
    """Merge modular convolution blocks into exact integer coefficients.

    Parameters
    ----------
    n:
        Common length of the two (unseen) input sequences.
    abs_bound_a, abs_bound_b:
        Non-negative bounds on the absolute values of their entries.
    output_length:
        Defaults to the ordinary convolution length ``2*n - 1``; a shorter
        length (a truncated / cyclic convolution) may be supplied as long
        as it does not exceed it.
    """

    def __init__(
        self,
        n: int,
        abs_bound_a: int,
        abs_bound_b: int,
        output_length: int | None = None,
    ) -> None:
        if n <= 0:
            raise ValueError("n must be positive")
        if abs_bound_a < 0 or abs_bound_b < 0:
            raise ValueError("absolute bounds must be non-negative")
        full_length = 2 * n - 1
        length = full_length if output_length is None else output_length
        if not 1 <= length <= full_length:
            raise ValueError(
                f"output_length must be in [1, {full_length}], got {length}"
            )
        self.n = n
        self.abs_bound_a = abs_bound_a
        self.abs_bound_b = abs_bound_b
        self.output_length = length
        self._bounds = [
            term_count(k, n) * abs_bound_a * abs_bound_b
            for k in range(length)
        ]
        self._states = [
            PositionState(position=k, bound=self._bounds[k])
            for k in range(length)
        ]
        self._seen_moduli: set[int] = set()

    # -- introspection ----------------------------------------------------

    @property
    def bounds(self) -> list[int]:
        """Per-position coefficient bounds (copy)."""
        return list(self._bounds)

    def state(self, position: int) -> PositionState:
        return self._states[position]

    def states(self) -> list[PositionState]:
        return list(self._states)

    def ready_positions(self) -> list[int]:
        """Positions currently satisfying the unique-reconstruction rule."""
        return [s.position for s in self._states if s.is_determined()]

    def is_determined(self, position: int) -> bool:
        return self._states[position].is_determined()

    def get_value(self, position: int) -> int | None:
        return self._states[position].value()

    def effective_modulus(self, position: int) -> int:
        return self._states[position].effective_modulus

    def accepted_moduli(self) -> set[int]:
        return set(self._seen_moduli)

    def factor_report(self) -> dict[int, dict[str, object]]:
        """For every prime factor, who supplies the maximal prime power.

        ``owner_modulus`` is the modulus contributing the strongest power
        (ties are kept at the first supplier); ``exponent`` is that maximal
        exponent and ``positions`` lists where the factor occurs.  A factor
        is duplicated information whenever a later modulus carries only a
        weaker or equal power of it.
        """
        owners: dict[int, dict[str, object]] = {}
        for state in self._states:
            for prime, exponent in state.factors.items():
                entry = owners.get(prime)
                if entry is None:
                    owners[prime] = {
                        "exponent": exponent,
                        "owner_modulus": state.factor_owners[prime],
                        "positions": [state.position],
                    }
                else:
                    cast_positions = entry["positions"]
                    assert isinstance(cast_positions, list)
                    cast_positions.append(state.position)
        return owners

    # -- block ingestion --------------------------------------------------

    def add_block(
        self,
        modulus: int,
        residues: Sequence[int] | Mapping[int, int],
        positions: Iterable[int] | None = None,
    ) -> BlockReport:
        """Merge one worker block (a residue per output position).

        ``residues`` may be:

        * a sequence of length :attr:`output_length`, or
        * a mapping ``position -> residue`` (partial / late block), or
        * a sequence paired with an explicit iterable of ``positions``.

        Residues may use either the canonical range ``[0, modulus)`` or a
        signed range; they are normalized internally.

        The block is validated against every existing constraint *before*
        any state changes, so a conflicting block leaves the reconstructor
        untouched and raises :class:`CongruenceConflict`.
        """
        if not isinstance(modulus, int) or modulus <= 0:
            raise ValueError("modulus must be a positive integer")
        normalized = self._normalize_block(residues, positions)

        # Dry run: detect every conflict before mutating anything.
        conflicts: list[ConflictRecord] = []
        planned: dict[int, tuple[int, int, dict[int, int]]] = {}
        block_factors = factorize(modulus) if modulus >= 2 else {}
        for pos, raw in normalized.items():
            state = self._states[pos]
            r = raw % modulus
            if state.effective_modulus > 1:
                merged = merge_congruence(
                    state.residue, state.effective_modulus, r, modulus
                )
                if merged is None:
                    g = gcd(state.effective_modulus, modulus)
                    conflicts.append(
                        ConflictRecord(
                            position=pos,
                            modulus=modulus,
                            incoming_residue=r,
                            existing_residue=state.residue,
                            existing_modulus=state.effective_modulus,
                            gcd=g,
                            difference=(r - state.residue) % modulus,
                        )
                    )
                    continue
                new_residue, new_modulus = merged
            else:
                new_residue, new_modulus = r, modulus
            new_factors = dict(state.factors)
            for prime, exponent in block_factors.items():
                if exponent > new_factors.get(prime, 0):
                    new_factors[prime] = exponent
            planned[pos] = (new_residue, new_modulus, new_factors)

        if conflicts:
            raise CongruenceConflict(conflicts)

        # Commit phase.
        new_factor_positions: dict[int, list[int]] = {}
        duplicated_factor_positions: dict[int, list[int]] = {}
        newly_determined: list[int] = []
        for pos, (new_residue, new_modulus, new_factors) in planned.items():
            state = self._states[pos]
            was_determined = state.is_determined()
            for prime, exponent in block_factors.items():
                old_exponent = state.factors.get(prime, 0)
                if exponent > old_exponent:
                    new_factor_positions.setdefault(prime, []).append(pos)
                    state.factor_owners[prime] = modulus
                else:
                    duplicated_factor_positions.setdefault(prime, []).append(
                        pos
                    )
            state.residue = new_residue
            state.effective_modulus = new_modulus
            state.factors = new_factors
            state.source_moduli.append(modulus)
            if not was_determined and state.is_determined():
                newly_determined.append(pos)

        self._seen_moduli.add(modulus)
        return BlockReport(
            modulus=modulus,
            applied_positions=tuple(sorted(planned)),
            new_factor_positions={
                p: tuple(ps) for p, ps in sorted(new_factor_positions.items())
            },
            duplicated_factor_positions={
                p: tuple(ps)
                for p, ps in sorted(duplicated_factor_positions.items())
            },
            newly_determined_positions=tuple(newly_determined),
        )

    # -- publishing -------------------------------------------------------

    def publish_ready(self) -> dict[int, int]:
        """Publish every currently determined position and mark it.

        Returns a mapping ``position -> coefficient`` containing only
        positions that become published by this call; already published
        positions are not repeated.
        """
        published: dict[int, int] = {}
        for state in self._states:
            if not state.published and state.is_determined():
                value = signed_residue(state.residue, state.effective_modulus)
                state.published = True
                published[state.position] = value
        return published

    def snapshot(self) -> dict[int, int]:
        """All determined values without changing publication markers."""
        return {
            s.position: s.value()
            for s in self._states
            if s.is_determined()
        }

    # -- internals --------------------------------------------------------

    def _normalize_block(
        self,
        residues: Sequence[int] | Mapping[int, int],
        positions: Iterable[int] | None,
    ) -> dict[int, int]:
        if isinstance(residues, Mapping):
            if positions is not None:
                raise ValueError(
                    "positions must not be given when residues is a mapping"
                )
            items = dict(residues)
        else:
            if positions is None:
                if len(residues) != self.output_length:
                    raise ValueError(
                        "full block needs one residue per output position "
                        f"({len(residues)} given, {self.output_length} needed)"
                    )
                items = {k: residues[k] for k in range(self.output_length)}
            else:
                pos_list = list(positions)
                if len(pos_list) != len(residues):
                    raise ValueError(
                        "positions and residues must have equal length"
                    )
                items = dict(zip(pos_list, residues))
        for pos in items:
            if not 0 <= pos < self.output_length:
                raise IndexError(
                    f"position {pos} outside [0, {self.output_length})"
                )
            if not isinstance(items[pos], int):
                raise TypeError("residues must be integers")
        return items


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------

def reconstruct_convolution(
    n: int,
    abs_bound_a: int,
    abs_bound_b: int,
    blocks: Iterable[tuple[int, Sequence[int] | Mapping[int, int]]],
) -> list[int | None]:
    """Feed ``(modulus, residues)`` blocks in order and return coefficients.

    Entries stay ``None`` until the accumulated effective modulus at that
    position exceeds twice the position-specific coefficient bound.
    """
    reconstructor = ConvolutionReconstructor(n, abs_bound_a, abs_bound_b)
    for modulus, residues in blocks:
        reconstructor.add_block(modulus, residues)
    return [
        reconstructor.get_value(k)
        for k in range(reconstructor.output_length)
    ]
