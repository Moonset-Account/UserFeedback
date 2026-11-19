"""Core sanitization logic for safe trajectory publishing.

A trajectory is a sequence of time-stamped planar samples.  Each sample
carries a square uncertainty region of half-side ``radius`` centred on the
sample coordinates.  Between two adjacent samples the coordinates and the
radius are assumed to vary linearly in time.

A secret region is an axis-aligned rectangle that is sensitive only during
a closed time interval ``[t_start, t_end]``.  Whenever the possible
position (the moving uncertainty square) *touches* a secret region that is
in effect at that moment, the corresponding sample is hidden or the
corresponding link is cut.

The sanitizer never invents interpolated points (so it cannot leak the
boundary of a secret region) and never reconnects the two sides of a
removed sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

__all__ = [
    "Sample",
    "SecretRegion",
    "PublishResult",
    "sanitize_trajectory",
]


@dataclass(frozen=True)
class Sample:
    """One raw trajectory sample.

    ``radius`` is the half-side of the square uncertainty region centred at
    ``(x, y)``; the possible position at the sample instant is
    ``[x - radius, x + radius] x [y - radius, y + radius]``.
    """

    t: float
    x: float
    y: float
    radius: float = 0.0

    def __post_init__(self) -> None:
        for name in ("t", "x", "y", "radius"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"{name} must be a real number, got {value!r}")
            if value != value:  # NaN check
                raise ValueError(f"{name} must not be NaN")
        if self.radius < 0:
            raise ValueError("radius must be non-negative")


@dataclass(frozen=True)
class SecretRegion:
    """Axis-aligned rectangle that is secret during ``[t_start, t_end]``.

    The time interval is closed: a region is in effect at exactly
    ``t_start`` and exactly ``t_end``.
    """

    x_min: float
    y_min: float
    x_max: float
    y_max: float
    t_start: float
    t_end: float

    def __post_init__(self) -> None:
        for name in ("x_min", "y_min", "x_max", "y_max", "t_start", "t_end"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"{name} must be a real number, got {value!r}")
            if value != value:
                raise ValueError(f"{name} must not be NaN")
        if self.x_min > self.x_max:
            raise ValueError("x_min must not exceed x_max")
        if self.y_min > self.y_max:
            raise ValueError("y_min must not exceed y_max")
        if self.t_start > self.t_end:
            raise ValueError("t_start must not exceed t_end")

    def in_effect(self, t: float) -> bool:
        """Closed-interval membership."""
        return self.t_start <= t <= self.t_end


@dataclass(frozen=True)
class PublishResult:
    """Outcome of sanitizing a trajectory.

    ``samples`` are the surviving *original* samples, in input order.
    ``segments`` are the publishable links ``(a, b)`` between samples that
    were adjacent in the input; both endpoints survived and the whole
    interpolated link is safe.  ``tracks`` groups those links into maximal
    displayable polylines; a track of length one is a standalone point.
    No element of the result is an interpolated point, and no track jumps
    over a removed sample.
    """

    samples: Tuple[Sample, ...]
    segments: Tuple[Tuple[Sample, Sample], ...]
    tracks: Tuple[Tuple[Sample, ...], ...]
    hidden_samples: Tuple[Sample, ...]


def _square_touches_region(x: float, y: float, radius: float, region: SecretRegion) -> bool:
    """Closed intersection between the uncertainty square and the rectangle."""
    return (
        x - radius <= region.x_max
        and x + radius >= region.x_min
        and y - radius <= region.y_max
        and y + radius >= region.y_min
    )


def _sample_is_hidden(sample: Sample, regions: Sequence[SecretRegion]) -> bool:
    return any(
        region.in_effect(sample.t)
        and _square_touches_region(sample.x, sample.y, sample.radius, region)
        for region in regions
    )


def _linear_inequality_interval(
    value_at_lo: float, slope: float, lo: float, hi: float, bound: float, upper: bool
) -> Tuple[float, float] | None:
    """Solve ``value(t) <= bound`` (or ``>=``) for t in [lo, hi].

    ``value`` is linear with the given slope and equals ``value_at_lo`` at
    ``t == lo``.  Returns the sub-interval of ``[lo, hi]`` where the
    inequality holds, or ``None`` if it holds nowhere.
    """
    if slope == 0:
        holds = value_at_lo <= bound if upper else value_at_lo >= bound
        return (lo, hi) if holds else None
    crossing = lo + (bound - value_at_lo) / slope
    if upper:
        # value increasing -> holds below the crossing, else above it
        return (lo, min(hi, crossing)) if slope > 0 else (max(lo, crossing), hi)
    return (max(lo, crossing), hi) if slope > 0 else (lo, min(hi, crossing))


def _axis_touch_interval(
    p0: float, p1: float, r0: float, r1: float, t0: float, t1: float,
    lo: float, hi: float, axis_min: float, axis_max: float,
) -> Tuple[float, float] | None:
    """Interval within [lo, hi] where the moving square touches the region
    along one axis, or None.

    Position ``p`` and radius ``r`` are linear between (t0, p0, r0) and
    (t1, p1, r1).  Touching means ``p(t) - r(t) <= axis_max`` and
    ``p(t) + r(t) >= axis_min`` simultaneously.
    """
    span = t1 - t0
    if span == 0:
        touches = p0 - r0 <= axis_max and p0 + r0 >= axis_min
        return (lo, hi) if touches else None
    p_slope = (p1 - p0) / span
    r_slope = (r1 - r0) / span
    # lower edge of the square: p(t) - r(t)
    lower_at_lo = p0 + p_slope * (lo - t0) - (r0 + r_slope * (lo - t0))
    # upper edge of the square: p(t) + r(t)
    upper_at_lo = p0 + p_slope * (lo - t0) + (r0 + r_slope * (lo - t0))
    first = _linear_inequality_interval(lower_at_lo, p_slope - r_slope, lo, hi, axis_max, upper=True)
    if first is None:
        return None
    second = _linear_inequality_interval(upper_at_lo, p_slope + r_slope, lo, hi, axis_min, upper=False)
    if second is None:
        return None
    start = max(first[0], second[0])
    end = min(first[1], second[1])
    return (start, end) if start <= end else None


def _segment_touches_region(a: Sample, b: Sample, region: SecretRegion) -> bool:
    """True if the interpolated uncertainty square touches the region at any
    instant while the region is in effect."""
    lo = max(a.t, region.t_start)
    hi = min(b.t, region.t_end)
    if lo > hi:
        return False
    x_interval = _axis_touch_interval(a.x, b.x, a.radius, b.radius, a.t, b.t, lo, hi, region.x_min, region.x_max)
    if x_interval is None:
        return False
    y_interval = _axis_touch_interval(a.y, b.y, a.radius, b.radius, a.t, b.t, lo, hi, region.y_min, region.y_max)
    if y_interval is None:
        return False
    start = max(x_interval[0], y_interval[0])
    end = min(x_interval[1], y_interval[1])
    return start <= end


def _segment_is_safe(a: Sample, b: Sample, regions: Sequence[SecretRegion]) -> bool:
    return not any(_segment_touches_region(a, b, region) for region in regions)


def sanitize_trajectory(
    samples: Sequence[Sample], regions: Sequence[SecretRegion] | SecretRegion
) -> PublishResult:
    """Hide samples and cut links that may reveal a secret region.

    Parameters
    ----------
    samples:
        Raw samples; times must be strictly increasing.
    regions:
        Secret rectangles, each with its closed active time interval.  A
        single ``SecretRegion`` is accepted as shorthand for a one-element
        sequence.

    Returns
    -------
    PublishResult
        Surviving original samples, publishable links between originally
        adjacent samples, and the maximal displayable tracks built from
        them.  Only input samples ever appear in the result.
    """
    samples = list(samples)
    if isinstance(regions, SecretRegion):
        regions = [regions]
    else:
        regions = list(regions)
    for prev, cur in zip(samples, samples[1:]):
        if not cur.t > prev.t:
            raise ValueError("sample times must be strictly increasing")

    hidden = [_sample_is_hidden(sample, regions) for sample in samples]
    kept = [sample for sample, is_hidden in zip(samples, hidden) if not is_hidden]
    dropped = [sample for sample, is_hidden in zip(samples, hidden) if is_hidden]

    segments: List[Tuple[Sample, Sample]] = []
    for index in range(len(samples) - 1):
        a, b = samples[index], samples[index + 1]
        if hidden[index] or hidden[index + 1]:
            continue
        if _segment_is_safe(a, b, regions):
            segments.append((a, b))

    # Group consecutive safe links into maximal tracks.  A kept sample with
    # no safe link on either side becomes a single-point track.
    tracks: List[Tuple[Sample, ...]] = []
    current: List[Sample] = []
    link_safe = [False] * (len(samples) - 1)
    for index, (a, b) in enumerate(zip(samples, samples[1:])):
        link_safe[index] = (
            not hidden[index]
            and not hidden[index + 1]
            and _segment_is_safe(a, b, regions)
        )
    for index, sample in enumerate(samples):
        if hidden[index]:
            continue
        if not current:
            current = [sample]
        elif link_safe[index - 1]:
            current.append(sample)
        else:
            tracks.append(tuple(current))
            current = [sample]
    if current:
        tracks.append(tuple(current))

    return PublishResult(
        samples=tuple(kept),
        segments=tuple(segments),
        tracks=tuple(tracks),
        hidden_samples=tuple(dropped),
    )
