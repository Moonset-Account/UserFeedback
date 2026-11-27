"""Finite-population sequential sampling adjudication.

An auditor reviews a fixed synthetic population of ``N`` terminal records,
exactly ``a`` of which are anomalous (the remaining ``N - a`` are clean).
Records are revealed one at a time in a pre-fixed random permutation, i.e. a
simple random sample *without replacement*.  Review happens in rounds: the
auditor may only stop at the pre-listed checkpoints, and at a checkpoint
``n`` the null hypothesis is rejected ("the line is crossed") as soon as the
cumulative number of observed anomalies meets the integer stopping rule.

Every probability is computed exactly with :class:`fractions.Fraction`.
All first-crossing events are mutually exclusive by construction (a path
that crossed earlier is removed from the survivor set), so summing the
per-round probabilities never double counts paths that already stopped.

The module only encodes the defensive rejection rule described above; it
contains no exploit, payload or evasion logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations, permutations
from math import comb
from typing import Iterable, Mapping, Sequence

__all__ = [
    "Checkpoint",
    "Plan",
    "RoundProbability",
    "WorstCaseResult",
    "ConditionalResult",
    "ConditionalWorstCaseResult",
    "exhaustive_first_crossing",
    "exhaustive_index_permutations",
    "exhaustive_conditional",
    "exhaustive_subset_first_crossing",
]


@dataclass(frozen=True)
class Checkpoint:
    """A single permitted stopping point.

    Attributes:
        n: Number of records revealed at this checkpoint (``1 <= n <= N``).
        c: Integer stopping threshold; the line is crossed when the
            cumulative anomaly count at ``n`` satisfies the rule.
    """

    n: int
    c: int


@dataclass(frozen=True)
class RoundProbability:
    """Exact first-crossing probability at one checkpoint."""

    checkpoint: Checkpoint
    probability: Fraction


@dataclass(frozen=True)
class WorstCaseResult:
    """Worst admissible total anomaly count under the null hypothesis."""

    worst_a: int
    per_round: tuple[RoundProbability, ...]
    ever_cross: Fraction


@dataclass(frozen=True)
class ConditionalResult:
    """Conditional first-crossing probabilities after a fixed prefix."""

    observed_prefix: tuple[int, ...]
    observed_anomalies: int
    per_round: tuple[RoundProbability, ...]
    ever_cross: Fraction


@dataclass(frozen=True)
class ConditionalWorstCaseResult:
    """Worst admissible remaining anomaly count after a fixed prefix."""

    worst_remaining: int
    per_round: tuple[RoundProbability, ...]
    ever_cross: Fraction


class Plan:
    """A fixed finite-population, multi-round stopping plan.

    Args:
        N: Total population size.
        M: Maximum number of anomalies allowed under the null hypothesis;
            every ``a`` in ``range(0, M + 1)`` is an admissible total.
        checkpoints: The permitted stopping points, each either a
            :class:`Checkpoint` or an ``(n, c)`` pair.  Checkpoint positions
            must be strictly increasing and lie within ``1..N``.
        rule: ``"ge"`` (default) crosses when ``anomaly_count >= c``;
            ``"gt"`` crosses when ``anomaly_count > c`` (i.e. threshold
            ``c`` is the number still tolerated).

    Raises:
        ValueError: If any argument is inconsistent.
    """

    def __init__(
        self,
        N: int,
        M: int,
        checkpoints: Iterable[Checkpoint | tuple[int, int]],
        *,
        rule: str = "ge",
    ) -> None:
        if not isinstance(N, int) or isinstance(N, bool) or N <= 0:
            raise ValueError("N must be a positive integer")
        if not isinstance(M, int) or isinstance(M, bool) or not (0 <= M <= N):
            raise ValueError("M must be an integer in [0, N]")
        if rule not in ("ge", "gt"):
            raise ValueError("rule must be 'ge' (>=) or 'gt' (>)")

        cps: list[Checkpoint] = []
        for cp in checkpoints:
            if isinstance(cp, Checkpoint):
                point = cp
            else:
                n, c = cp
                point = Checkpoint(n, c)
            if not isinstance(point.n, int) or not (1 <= point.n <= N):
                raise ValueError("checkpoint n must be an integer in [1, N]")
            if not isinstance(point.c, int) or isinstance(point.c, bool) or point.c < 0:
                raise ValueError("checkpoint c must be a non-negative integer")
            if cps and point.n <= cps[-1].n:
                raise ValueError("checkpoint positions must be strictly increasing")
            cps.append(point)
        if not cps:
            raise ValueError("at least one checkpoint is required")

        self.N: int = N
        self.M: int = M
        self.checkpoints: tuple[Checkpoint, ...] = tuple(cps)
        self.rule: str = rule

    # ------------------------------------------------------------------ #
    # Predicates and hypergeometric building blocks
    # ------------------------------------------------------------------ #
    def crosses(self, checkpoint: Checkpoint, anomaly_count: int) -> bool:
        """Return whether ``anomaly_count`` crosses this checkpoint's rule."""
        if self.rule == "ge":
            return anomaly_count >= checkpoint.c
        return anomaly_count > checkpoint.c

    @staticmethod
    def hypergeometric(
        N: int, a: int, n: int, k: int
    ) -> Fraction:
        """P(exactly ``k`` anomalies among ``n`` draws without replacement).

        ``C(a, k) * C(N-a, n-k) / C(N, n)``, returned as an exact
        :class:`Fraction` (zero when the counts are incompatible).
        """
        if n <= 0:
            return Fraction(1 if k == 0 else 0)
        if k < 0 or k > a or n - k < 0 or n - k > N - a:
            return Fraction(0)
        return Fraction(
            comb(a, k) * comb(N - a, n - k),
            comb(N, n),
        )

    # ------------------------------------------------------------------ #
    # Prefix handling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_prefix(prefix: str | Sequence[int]) -> tuple[int, ...]:
        if isinstance(prefix, str):
            try:
                bits = tuple(int(ch) for ch in prefix)
            except (TypeError, ValueError) as exc:
                raise ValueError("prefix string must contain only '0' and '1'") from exc
        else:
            bits = tuple(prefix)
        for bit in bits:
            if not isinstance(bit, int) or isinstance(bit, bool) or bit not in (0, 1):
                raise ValueError("prefix entries must be 0 (clean) or 1 (anomaly)")
        return bits

    def _check_prefix(
        self, prefix: tuple[int, ...], a: int
    ) -> tuple[int, int]:
        """Validate a fixed observed prefix against total anomaly count ``a``.

        Returns ``(k, b)`` where ``k`` is the prefix length and ``b`` the
        number of anomalies observed in it.
        """
        k = len(prefix)
        if not (0 <= k <= self.N):
            raise ValueError("prefix length must be in [0, N]")
        b = sum(prefix)
        if b > a:
            raise ValueError(
                f"prefix contains {b} anomalies but total a={a}"
            )
        if k - b > self.N - a:
            raise ValueError(
                f"prefix contains {k - b} clean records but population only "
                f"has {self.N - a} clean records under a={a}"
            )
        return k, b

    def _future_checkpoints(self, k: int) -> tuple[Checkpoint, ...]:
        future = tuple(cp for cp in self.checkpoints if cp.n > k)
        if not future:
            raise ValueError("no checkpoint remains after the observed prefix")
        return future

    # ------------------------------------------------------------------ #
    # Exact dynamic program (survivor states, no replacement)
    # ------------------------------------------------------------------ #
    def _dp(
        self,
        N: int,
        a: int,
        start_n: int,
        start_counts: Mapping[int, Fraction],
        points: Sequence[Checkpoint],
    ) -> tuple[tuple[RoundProbability, ...], Fraction]:
        """Run the survivor-state dynamic program.

        ``start_counts`` maps the *cumulative* anomaly counts already
        observed after ``start_n`` revealed records to the probability mass
        of those (surviving) states.  Checkpoint positions in ``points``
        are absolute positions in the original sequence, and their
        thresholds apply to cumulative anomaly counts -- only the draw
        lengths between checkpoints are relative.  At each checkpoint the
        mass that crosses the rule is recorded as that round's *first*
        crossing probability and removed; the rest advances
        hypergeometrically without replacement.
        """
        per_round: list[RoundProbability] = []
        states: dict[int, Fraction] = dict(start_counts)
        prev_n = start_n

        for cp in points:
            n = cp.n
            advanced: dict[int, Fraction] = {}
            for old_k, mass in states.items():
                draw = n - prev_n
                # Remaining population at size N - prev_n holds a - old_k
                # anomalies (old_k is the cumulative count); hypergeometric
                # draw of size `draw`.
                lo = max(0, draw - ((N - prev_n) - (a - old_k)))
                hi = min(draw, a - old_k)
                for add in range(lo, hi + 1):
                    p = self.hypergeometric(
                        N - prev_n, a - old_k, draw, add
                    )
                    if p:
                        advanced[old_k + add] = (
                            advanced.get(old_k + add, Fraction(0)) + mass * p
                        )

            crossed = Fraction(0)
            survivors: dict[int, Fraction] = {}
            for total_k, mass in advanced.items():
                if self.crosses(cp, total_k):
                    crossed += mass
                else:
                    survivors[total_k] = mass

            per_round.append(RoundProbability(cp, crossed))
            states = survivors
            prev_n = n

        ever_cross = Fraction(1) - sum(states.values(), Fraction(0))
        return tuple(per_round), ever_cross

    # ------------------------------------------------------------------ #
    # Unconditional probabilities
    # ------------------------------------------------------------------ #
    def first_crossing(self, a: int) -> tuple[RoundProbability, ...]:
        """Exact per-round first-crossing probabilities for total count ``a``.

        The returned events are disjoint: round ``r`` only carries mass of
        paths that survived every earlier checkpoint, so summing the
        probabilities counts each permutation at most once.
        """
        if not isinstance(a, int) or isinstance(a, bool) or not (0 <= a <= self.N):
            raise ValueError("a must be an integer in [0, N]")
        per_round, _ = self._dp(self.N, a, 0, {0: Fraction(1)}, self.checkpoints)
        return per_round

    def ever_cross_probability(self, a: int) -> Fraction:
        """Probability of crossing at some checkpoint (sum of disjoint rounds)."""
        return sum(rp.probability for rp in self.first_crossing(a)) or Fraction(0)

    def worst_case(self) -> WorstCaseResult:
        """Find the admissible ``a in [0, M]`` maximising the crossing chance.

        Ties resolve to the smallest ``a`` (fully reported regardless).
        """
        best: WorstCaseResult | None = None
        for a in range(self.M + 1):
            per_round = self.first_crossing(a)
            ever = sum(rp.probability for rp in per_round) or Fraction(0)
            if best is None or ever > best.ever_cross:
                best = WorstCaseResult(a, per_round, ever)
        assert best is not None
        return best

    # ------------------------------------------------------------------ #
    # Conditional probabilities after a fixed observed prefix
    # ------------------------------------------------------------------ #
    def conditional_first_crossing(
        self,
        a: int,
        prefix: str | Sequence[int],
    ) -> ConditionalResult:
        """First-crossing probabilities conditioned on a fixed 0/1 prefix.

        Given the observed prefix, every remaining permutation of the
        un-revealed records is equally likely, so the continuation is a
        fresh without-replacement problem on the remaining population.
        """
        if not isinstance(a, int) or isinstance(a, bool) or not (0 <= a <= self.N):
            raise ValueError("a must be an integer in [0, N]")
        bits = self._normalize_prefix(prefix)
        k, b = self._check_prefix(bits, a)
        future = self._future_checkpoints(k)

        for cp in self.checkpoints:
            if cp.n <= k and self.crosses(cp, b):
                raise ValueError(
                    "the observed prefix already crosses checkpoint "
                    f"n={cp.n}; the process would have stopped there"
                )

        # The continuation is a without-replacement problem on the un-revealed
        # records, but thresholds stay expressed against the *cumulative*
        # anomaly count, whose starting value is b at position k.  The DP
        # therefore runs in absolute coordinates with the observed prefix as
        # the single survivor state.
        per_round, ever = self._dp(
            self.N, a, k, {b: Fraction(1)}, future
        )
        return ConditionalResult(bits, b, per_round, ever)

    def conditional_worst_case(
        self,
        prefix: str | Sequence[int],
    ) -> ConditionalWorstCaseResult:
        """Worst admissible remaining anomaly count given an observed prefix.

        Admissible totals are the ``a in [0, M]`` consistent with the
        prefix; the reported ``worst_remaining`` is ``a - observed``.
        """
        bits = self._normalize_prefix(prefix)
        k = len(bits)
        if not (0 <= k <= self.N):
            raise ValueError("prefix length must be in [0, N]")
        b = sum(bits)
        if b > self.M:
            raise ValueError(
                f"prefix already contains {b} anomalies, exceeding M={self.M}"
            )
        future = self._future_checkpoints(k)

        best: ConditionalWorstCaseResult | None = None
        for a in range(b, self.M + 1):
            if k - b > self.N - a:
                continue  # not enough clean records left: infeasible total
            result = self.conditional_first_crossing(a, bits)
            if best is None or result.ever_cross > best.ever_cross:
                best = ConditionalWorstCaseResult(
                    a - b, result.per_round, result.ever_cross
                )
        if best is None:
            raise ValueError("no admissible anomaly total is consistent with the prefix")
        return best


# ---------------------------------------------------------------------- #
# Exhaustive cross-validation helpers (synthetic data, small populations)
# ---------------------------------------------------------------------- #
def exhaustive_first_crossing(
    plan: Plan, a: int, *, max_permutations: int = 2_000_000
) -> tuple[RoundProbability, ...]:
    """Enumerate every permutation of the population and score crossings.

    Each full permutation is one equally likely realisation of the
    pre-fixed random order.  A permutation is assigned to exactly the
    *first* checkpoint where the rule is crossed; earlier survivors never
    contribute to later rounds.  Results are exact :class:`Fraction`
    values.  Refuses to run when the factorial workload exceeds
    ``max_permutations`` (use subset enumeration for larger ``N``).
    """
    N = plan.N
    if not (0 <= a <= N):
        raise ValueError("a must be an integer in [0, N]")
    total = 1
    for factor in range(2, N + 1):
        total *= factor
        if total > max_permutations:
            raise ValueError(
                f"N={N} gives {total} permutations, above the safety cap "
                f"{max_permutations}; use subset enumeration instead"
            )

    labels = (1,) * a + (0,) * (N - a)
    distinct = set(permutations(labels))
    counts = [0] * len(plan.checkpoints)
    for perm in distinct:
        seen = 0
        crossed_already = False
        for i, cp in enumerate(plan.checkpoints):
            seen = sum(perm[: cp.n])
            if not crossed_already and plan.crosses(cp, seen):
                counts[i] += 1
                crossed_already = True
    # Every distinct 0/1 label sequence corresponds to the same number of
    # underlying record permutations (a! * (N-a)!), hence is equally likely.
    denom = len(distinct)
    return tuple(
        RoundProbability(cp, Fraction(counts[i], denom))
        for i, cp in enumerate(plan.checkpoints)
    )


def exhaustive_index_permutations(
    plan: Plan, a: int, *, max_permutations: int = 4_000_000
) -> tuple[RoundProbability, ...]:
    """Stream *every* record-index permutation and score first crossings.

    Unlike :func:`exhaustive_first_crossing` (which deduplicates identical
    0/1 label sequences), this enumerates the ``N!`` permutations of the
    distinct record indices one by one, so every permutation has equal
    probability ``1/N!`` and no weighting argument is needed.  It is the
    literal "permutation by permutation" cross-validation for the
    ten-record example: ``10! = 3,628,800`` permutations take a few
    seconds and only constant extra memory.

    Each permutation is charged to exactly the first checkpoint whose
    rule its cumulative anomaly count crosses.  Raises ``ValueError`` if
    the factorial workload exceeds ``max_permutations``.
    """
    N = plan.N
    if not (0 <= a <= N):
        raise ValueError("a must be an integer in [0, N]")
    factorial = 1
    for factor in range(2, N + 1):
        factorial *= factor
        if factorial > max_permutations:
            raise ValueError(
                f"N={N} gives {factorial} permutations, above the safety "
                f"cap {max_permutations}"
            )

    # Record indices 0..a-1 are anomalous; the rest are clean.
    labels = (1,) * a + (0,) * (N - a)
    counts = [0] * len(plan.checkpoints)
    for order in permutations(range(N)):
        cumulative = 0
        prev_n = 0
        stopped = False
        for i, cp in enumerate(plan.checkpoints):
            for idx in order[prev_n: cp.n]:
                cumulative += labels[idx]
            prev_n = cp.n
            if not stopped and plan.crosses(cp, cumulative):
                counts[i] += 1
                stopped = True
    return tuple(
        RoundProbability(cp, Fraction(counts[i], factorial))
        for i, cp in enumerate(plan.checkpoints)
    )


def exhaustive_conditional(
    plan: Plan, a: int, prefix: str | Sequence[int]
) -> ConditionalResult:
    """Score crossings over every permutation extending a fixed prefix.

    All completions of the unrevealed positions are equally likely
    conditional on the prefix; each completion is charged to at most one
    (first-crossing) checkpoint.
    """
    bits = Plan._normalize_prefix(prefix)
    k, b = plan._check_prefix(bits, a)
    future = plan._future_checkpoints(k)
    for cp in plan.checkpoints:
        if cp.n <= k and plan.crosses(cp, b):
            raise ValueError(
                "the observed prefix already crosses checkpoint "
                f"n={cp.n}; the process would have stopped there"
            )

    rem_N = plan.N - k
    rem_a = a - b
    remaining_labels = (1,) * rem_a + (0,) * (rem_N - rem_a)
    counts = [0] * len(future)
    total = 0
    for tail in set(permutations(remaining_labels)):
        total += 1
        sequence = bits + tail
        for i, cp in enumerate(future):
            if plan.crosses(cp, sum(sequence[: cp.n])):
                counts[i] += 1
                break
    return ConditionalResult(
        bits,
        b,
        tuple(
            RoundProbability(future[i], Fraction(counts[i], total))
            for i in range(len(future))
        ),
        Fraction(sum(counts), total),
    )


def exhaustive_subset_first_crossing(
    plan: Plan, a: int
) -> tuple[RoundProbability, ...]:
    """Subset-based exhaustive check for larger ``N``.

    Every permutation with anomaly positions given by a fixed
    ``a``-subset is equally likely, and all ``C(N, a)`` subsets have the
    same probability.  Enumerating the subsets is therefore a complete
    enumeration of the distinct label sequences without a factorial blow
    up (used for the ten-record example).
    """
    N = plan.N
    if not (0 <= a <= N):
        raise ValueError("a must be an integer in [0, N]")
    counts = [0] * len(plan.checkpoints)
    total = 0
    for positions in combinations(range(1, N + 1), a):
        total += 1
        pos = set(positions)
        crossed = False
        for i, cp in enumerate(plan.checkpoints):
            seen = sum(1 for p in range(1, cp.n + 1) if p in pos)
            if not crossed and plan.crosses(cp, seen):
                counts[i] += 1
                crossed = True
    return tuple(
        RoundProbability(cp, Fraction(counts[i], total))
        for i, cp in enumerate(plan.checkpoints)
    )
