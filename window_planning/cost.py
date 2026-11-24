"""Fee tables and pricing of measured sorts.

The real cost of a physical operation is looked up in two *small tables*:

* a group-length table: exact lookup of the number of rows in each sorted
  group (singleton groups are not sorted and not charged);
* a comparison table: upper-bound bracket lookup of the number of comparisons
  consumed by Timsort on that group.

Both tables are supplied per planning task. A fee that neither table covers
raises :class:`MissingFeeError` instead of silently inventing a number, so the
"real fee against the given small table" rule is always enforced.
"""

from __future__ import annotations

from dataclasses import dataclass

from .physical import SortMeasurement


class FeeScheduleError(ValueError):
    """Base class for fee-table problems."""


class MissingFeeError(FeeScheduleError):
    """The given small table does not cover an observed value."""


@dataclass(frozen=True)
class FeeSchedule:
    """Two explicit small tables plus their priced lookups.

    ``length_fee`` is keyed by exact group length.
    ``comparison_brackets`` maps an inclusive upper bound to a fee; the
    smallest upper bound not smaller than the count wins.
    """

    length_fee: dict[int, int]
    comparison_brackets: dict[int, int]

    def length_price(self, size: int) -> int:
        if size not in self.length_fee:
            raise MissingFeeError(
                f"group length {size} is not covered by the length fee table "
                f"{sorted(self.length_fee)}"
            )
        return self.length_fee[size]

    def comparison_price(self, comparisons: int) -> int:
        bounds = sorted(self.comparison_brackets)
        for bound in bounds:
            if comparisons <= bound:
                return self.comparison_brackets[bound]
        raise MissingFeeError(
            f"comparison count {comparisons} exceeds every bracket "
            f"{bounds} of the comparison fee table"
        )

    def price(self, measurement: SortMeasurement) -> int:
        """Total fee of one sort operation across all of its groups."""
        total = 0
        # Group fees are charged per non-singleton sorted group.
        for size in measurement.group_sizes:
            if size <= 1:
                continue
            total += self.length_price(size)
        if measurement.comparisons > 0:
            total += self.comparison_price(measurement.comparisons)
        return total


#: Default demonstration tables (small, fully covering the bundled example).
DEFAULT_LENGTH_FEE: dict[int, int] = {
    1: 1,
    2: 2,
    3: 3,
    4: 4,
    5: 5,
    6: 6,
    7: 7,
    8: 8,
}

DEFAULT_COMPARISON_BRACKETS: dict[int, int] = {
    4: 2,
    8: 4,
    16: 8,
    32: 16,
    64: 32,
}


def default_fee_schedule() -> FeeSchedule:
    return FeeSchedule(
        length_fee=dict(DEFAULT_LENGTH_FEE),
        comparison_brackets=dict(DEFAULT_COMPARISON_BRACKETS),
    )
