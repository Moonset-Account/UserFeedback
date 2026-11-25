"""Injectable random sources and invertible blinding-factor selection."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .errors import RandomSourceError


@runtime_checkable
class RandomSource(Protocol):
    """The small ``random.Random`` surface required by this package."""

    def randbytes(self, size: int) -> bytes: ...

    def randrange(self, start: int, stop: int | None = None) -> int: ...


def system_random_source() -> random.SystemRandom:
    """Return the operating-system-backed source used by production defaults."""

    return random.SystemRandom()


def random_unit_factor(
    n: int,
    rng: RandomSource | None = None,
    *,
    max_attempts: int = 10_000,
) -> int:
    """Draw a random non-weak unit in ``[2, n)``.

    Rejection sampling preserves an approximately uniform choice among valid
    factors.  It also accepts any injected object implementing ``randrange``.
    """

    if isinstance(n, bool) or not isinstance(n, int) or n <= 3:
        raise ValueError("n must be an integer greater than 3")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts <= 0
    ):
        raise ValueError("max_attempts must be a positive integer")

    source = rng if rng is not None else system_random_source()
    for _ in range(max_attempts):
        factor = source.randrange(2, n)
        if (
            isinstance(factor, int)
            and not isinstance(factor, bool)
            and 2 <= factor < n
            and math.gcd(factor, n) == 1
        ):
            return factor

    raise RandomSourceError(
        f"random source failed to return an invertible factor after {max_attempts} tries"
    )


class ScriptedRandomSource:
    """Deterministic source for tests and demos.

    Each call to :meth:`randbytes` returns ``ticket`` (whose length should match
    the requested ticket length).  Factor calls consume ``factors`` in order;
    after those are exhausted, a seeded :class:`random.Random` fallback is used.
    """

    def __init__(
        self,
        *,
        ticket: bytes = b"scripted-demo-ticket",
        factors: Sequence[int] = (),
        seed: int | None = 20261126,
    ) -> None:
        self.ticket = bytes(ticket)
        self._factors = list(factors)
        self._factor_index = 0
        self._fallback = random.Random(seed)
        self.calls: list[tuple[str, int | tuple[int, int]]] = []

    def randbytes(self, size: int) -> bytes:
        self.calls.append(("randbytes", size))
        if size != len(self.ticket):
            raise AssertionError(
                f"scripted ticket has {len(self.ticket)} bytes, requested {size}"
            )
        return self.ticket

    def randrange(self, start: int, stop: int | None = None) -> int:
        self.calls.append(("randrange", (start, stop)))
        if stop is None:
            low, high = 0, start
        else:
            low, high = start, stop

        if self._factor_index < len(self._factors):
            value = self._factors[self._factor_index]
            self._factor_index += 1
            if not low <= value < high:
                raise AssertionError(
                    f"scripted factor {value} outside randrange({low}, {high})"
                )
            return value
        return self._fallback.randrange(low, high)
