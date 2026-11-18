"""Local noise-adapter contract and a reference implementation.

The publisher never touches raw data itself: generation (aggregation plus
local differential-privacy noise) is delegated to an object implementing
the :class:`NoiseAdapter` protocol. Tests supply deterministic or
failing adapters; production code can plug in its own mechanism.
"""

from __future__ import annotations

import math
import random
from typing import Any, Protocol, runtime_checkable

from .models import DataSnapshot, Query


@runtime_checkable
class NoiseAdapter(Protocol):
    """Callable object producing a noisy result for one publication."""

    def add_noise(
        self, query: Query, snapshot: DataSnapshot
    ) -> Any:  # pragma: no cover - protocol signature
        ...


class LaplaceCountAdapter:
    """Reference adapter: count rows then add Laplace(1/sensitivity) noise.

    The snapshot payload is expected to be a sequence of rows; every row
    is counted (real adapters would apply ``query.filters`` first). The
    random source is injectable so tests stay deterministic.
    """

    def __init__(
        self,
        sensitivity: float = 1.0,
        epsilon: float = 1.0,
        rng: random.Random | None = None,
    ) -> None:
        if sensitivity <= 0:
            raise ValueError("sensitivity must be positive")
        if epsilon <= 0:
            raise ValueError("epsilon must be positive")
        self.sensitivity = float(sensitivity)
        self.epsilon = float(epsilon)
        self._rng = rng or random.Random()

    def add_noise(self, query: Query, snapshot: DataSnapshot) -> float:
        rows = snapshot.payload
        true_count = len(rows) if rows is not None else 0
        scale = self.sensitivity / self.epsilon
        # Inverse-CDF sampling of Laplace(scale) from U ~ Uniform(0, 1):
        #   U < 0.5 ->  scale * ln(2U)
        #   U >= 0.5 -> -scale * ln(2(1 - U))
        uniform = self._rng.random()
        if uniform < 0.5:
            noise = scale * math.log(2.0 * uniform) if uniform > 0.0 else 0.0
        else:
            noise = -scale * math.log(2.0 * (1.0 - uniform))
        return true_count + noise
