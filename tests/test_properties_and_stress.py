"""Property-style and stress tests.

The goal is evidence beyond hand-picked examples:

* a brute-force reference rounding (enumerate every representable magnitude,
  pick the closest under each tie rule) must agree with the analytic kernel on
  a dense sweep of rationals across several formats;
* random nesting schedules interleaving several domains must leave every
  domain's state identical to the state it would have had running alone.
"""

from __future__ import annotations

import bisect
import random
import unittest
from fractions import Fraction as F

from fpiso.domain import Domain
from fpiso.flags import Flag, MaskBits
from fpiso.format import Format, RoundingMode
from fpiso.rounding import round_exact
from fpiso.runtime import Runtime
from fpiso.value import FPValue


FORMATS = (
    Format("small", 4, -2, 3),
    Format("tiny", 3, -1, 1),
    Format("wide", 5, -4, 4),
    Format("p1", 1, -1, 2),
)

_GRID_CACHE: dict[str, list[F]] = {}


def reference_magnitudes(fmt: Format) -> list[F]:
    """Every non-negative representable magnitude, ascending."""

    values: set[F] = {F(0)}
    for step in range(fmt.step_min, fmt.step_max + 1):
        unit = F(2) ** step
        low, high = fmt.significand_range(step)
        for k in range(low, high + 1):
            values.add(k * unit)
    return sorted(values)


def _grid(fmt: Format) -> list[F]:
    cached = _GRID_CACHE.get(fmt.name)
    if cached is None:
        cached = reference_magnitudes(fmt)
        _GRID_CACHE[fmt.name] = cached
    return cached


def _floor_log2(value: F) -> int:
    """Independent ``e`` with ``2**e <= value < 2**(e+1)``."""

    e = 0
    if value >= 1:
        while F(2) ** (e + 1) <= value:
            e += 1
    else:
        while F(2) ** e > value:
            e -= 1
    return e


def _snap_integer(value: F, rounding: RoundingMode) -> int:
    """Independent round-to-integer with all five tie rules."""

    bottom = value.numerator // value.denominator
    rest = value - bottom
    if rest == 0:
        return bottom
    if rounding is RoundingMode.RD:
        return bottom
    if rounding is RoundingMode.RU:
        return bottom + 1
    if rounding is RoundingMode.RZ:
        return bottom if value > 0 else bottom + 1
    if rest == F(1, 2):
        if rounding is RoundingMode.RNA:
            return bottom if value < 0 else bottom + 1
        return bottom if bottom % 2 == 0 else bottom + 1
    return bottom if rest < F(1, 2) else bottom + 1


def reference_unbounded(exact: F, fmt: Format, rounding: RoundingMode) -> F:
    """Round with destination precision but an unlimited exponent range."""

    magnitude = abs(exact)
    step = _floor_log2(magnitude) - (fmt.precision - 1)
    unit = F(2) ** step
    return _snap_integer(exact / unit, rounding) * unit


def reference_in_range(exact: F, fmt: Format, rounding: RoundingMode) -> F:
    """Brute-force round a *non-overflowing* value over the full grid."""

    if exact == 0:
        return F(0)
    sign = 1 if exact > 0 else -1
    magnitude = abs(exact)
    grid = _grid(fmt)

    index = bisect.bisect_left(grid, magnitude)
    if index < len(grid) and grid[index] == magnitude:
        return exact
    below = grid[index - 1] if index > 0 else F(0)
    above = grid[index] if index < len(grid) else fmt.max_finite

    if rounding is RoundingMode.RZ:
        chosen = below
    elif rounding is RoundingMode.RD:
        chosen = below if sign > 0 else above
    elif rounding is RoundingMode.RU:
        chosen = above if sign > 0 else below
    else:
        midpoint = (below + above) / 2
        if magnitude < midpoint:
            chosen = below
        elif magnitude > midpoint:
            chosen = above
        elif rounding is RoundingMode.RNA:
            chosen = above
        else:  # RNE: nearest neighbour with an even significand
            chosen = below if _significand_is_even(below, fmt) else above

    return sign * chosen


def _significand_is_even(magnitude: F, fmt: Format) -> bool:
    for step in range(fmt.step_min, fmt.step_max + 1):
        unit = F(2) ** step
        scaled = magnitude / unit
        if scaled.denominator == 1 and fmt.representable_at(magnitude, step):
            return scaled.numerator % 2 == 0
    return True


def reference_result(exact: F, fmt: Format, rounding: RoundingMode):
    """Expected ``(FPValue, flags)`` computed independently of the kernel.

    * overflow is decided by the IEEE unbounded-exponent rule;
    * the in-range value comes from a brute-force scan of the representable
      magnitude grid (not the analytic step computation);
    * the underflow/inexact rules are re-derived from their definitions.
    """

    if exact == 0:
        return FPValue.zero(1), frozenset()

    sign = 1 if exact > 0 else -1
    magnitude = abs(exact)

    unbounded = reference_unbounded(exact, fmt, rounding)
    if abs(unbounded) > fmt.max_finite:
        flags = frozenset({Flag.OVERFLOW, Flag.INEXACT})
        if rounding in (RoundingMode.RNE, RoundingMode.RNA):
            to_inf = True
        elif rounding is RoundingMode.RU:
            to_inf = sign > 0
        elif rounding is RoundingMode.RD:
            to_inf = sign < 0
        else:
            to_inf = False
        if to_inf and fmt.has_infinity:
            return FPValue.inf(sign), flags
        return FPValue.finite(fmt.max_finite, sign=sign), flags

    rounded = reference_in_range(exact, fmt, rounding)

    flags: set[Flag] = set()
    if rounded != exact:
        flags.add(Flag.INEXACT)
        if abs(unbounded) < fmt.min_normal:
            flags.add(Flag.UNDERFLOW)
    if rounded == 0:
        return FPValue.zero(sign), frozenset(flags)
    return FPValue.finite(abs(rounded), sign=sign), frozenset(flags)


class ReferenceAgreementTests(unittest.TestCase):
    def test_dense_sweep_matches_reference(self) -> None:
        comparisons = 0
        for fmt in FORMATS:
            numerators = range(-(fmt.max_finite.numerator * 6) - 3,
                               (fmt.max_finite.numerator * 6) + 4)
            # Use a denominator that exercises subnormal, midpoint and tie grids.
            for denominator in (1, 2, 3, 4, 8, 16, 32, 64, 128):
                for numerator in numerators:
                    exact = F(numerator, denominator)
                    for rounding in RoundingMode:
                        got = round_exact(exact, fmt, rounding)
                        expected_value, expected_flags = reference_result(
                            exact, fmt, rounding
                        )
                        self.assertEqual(
                            got.value,
                            expected_value,
                            (fmt.name, exact, rounding.value, "value"),
                        )
                        self.assertEqual(
                            got.flags,
                            expected_flags,
                            (fmt.name, exact, rounding.value, "flags"),
                        )
                        comparisons += 1
        self.assertGreater(comparisons, 25_000)

    def test_binary64_like_format_spot_checks(self) -> None:
        # A format with the binary64 precision but a tiny exponent range keeps
        # the arithmetic honest on familiar constants.
        fmt = Format("p53", 53, -20, 20)
        self.assertEqual(round_exact(F(1, 3), fmt, RoundingMode.RNE).value.exact,
                         F(6004799503160661, 18014398509481984))
        self.assertEqual(round_exact(F(2) ** 100, fmt, RoundingMode.RNE).flags,
                         frozenset({Flag.OVERFLOW, Flag.INEXACT}))
        self.assertEqual(round_exact(F(2) ** -20, fmt, RoundingMode.RNE).flags,
                         frozenset())


class NestingStressTests(unittest.TestCase):
    """Random nesting schedules must never change a domain's own trajectory."""

    def test_random_schedules_preserve_solo_state(self) -> None:
        rng = random.Random(20261128)
        configs = [
            ("a", Format("small", 4, -2, 3), RoundingMode.RNE, MaskBits.all_masked()),
            ("b", Format("wide", 5, -4, 4), RoundingMode.RD, MaskBits.all_unmasked()),
            ("c", Format("tiny", 3, -1, 1), RoundingMode.RU, MaskBits.unmasking(Flag.UNDERFLOW)),
        ]
        operands = [
            (F(1, 3), F(1, 3)),
            (F(1, 64), F(1, 64)),
            (15, 15),
            (F(9, 8), F(-5, 4)),
            (F(1, 16), 1),
            (F(-3, 7), F(2, 5)),
        ]

        for trial in range(40):
            schedule = []
            for _ in range(rng.randint(6, 14)):
                domain_index = rng.randrange(3)
                op = rng.choice(["add", "mul", "fma"])
                args = rng.choice(operands)
                schedule.append((domain_index, op, args))

            # Solo run: each domain evolves with no nesting at all.
            solo = [
                Domain(name, fmt, rounding, masks)
                for name, fmt, rounding, masks in configs
            ]
            for domain_index, op, (x, y) in schedule:
                target = solo[domain_index]
                _apply(target, op, x, y)

            # Nested run: the same per-domain operation stream, but each
            # operation happens inside a randomly chosen LIFO context.
            runtime = Runtime(name=f"stress-{trial}")
            domains = [
                Domain(name, fmt, rounding, masks)
                for name, fmt, rounding, masks in configs
            ]
            for domain in domains:
                runtime.register(domain)

            cursors = [0, 0, 0]
            streams = [[] for _ in range(3)]
            for item in schedule:
                streams[item[0]].append(item)

            while any(cursors[i] < len(streams[i]) for i in range(3)):
                # Pick a random subset ordering, run one op for each domain in
                # a random nesting pattern.
                order = [i for i in range(3) if cursors[i] < len(streams[i])]
                rng.shuffle(order)
                entered = []
                for depth, domain_index in enumerate(order):
                    runtime.enter_domain(
                        domains[domain_index], audit=depth > 0
                    )
                    entered.append(domain_index)
                for domain_index in reversed(entered):
                    _, op, (x, y) = streams[domain_index][cursors[domain_index]]
                    cursors[domain_index] += 1
                    _apply(domains[domain_index], op, x, y)
                    runtime.exit_domain()
                self.assertIsNone(runtime.active)

            for index, (name, _, _, _) in enumerate(configs):
                self.assertEqual(
                    domains[index].sticky,
                    solo[index].sticky,
                    (trial, name, "sticky"),
                )
                self.assertEqual(
                    domains[index].pending,
                    solo[index].pending,
                    (trial, name, "pending"),
                )

    def test_random_schedules_never_raise_isolation_errors(self) -> None:
        rng = random.Random(7)
        runtime = Runtime()
        domains = [
            Domain(f"d{i}", Format("small", 4, -2, 3), mode, MaskBits.all_unmasked())
            for i, mode in enumerate(RoundingMode)
        ]
        for domain in domains:
            runtime.register(domain)
        for _ in range(200):
            depth = rng.randint(1, len(domains))
            entered = []
            for index in range(depth):
                runtime.enter_domain(domains[index])
                entered.append(domains[index])
            # Only the innermost domain may compute; the outer ones are paused.
            inner = entered[-1]
            inner.add(F(1, rng.randint(1, 64)), F(1, rng.randint(1, 64)))
            for _ in entered:
                runtime.exit_domain()
            self.assertIsNone(runtime.active)


def _apply(domain: Domain, op: str, x, y) -> None:
    if op == "add":
        domain.add(x, y)
    elif op == "mul":
        domain.mul(x, y)
    else:
        domain.fma(x, y, F(1, 8))


if __name__ == "__main__":
    unittest.main()
