"""Calibration selection for shared-scale contiguous channel groups.

Problem model
-------------
Several consecutive on-device channels may share one quantization scale so
that only a single scale header is stored per group.  For every channel the
caller supplies a small activation frequency table (value -> occurrence
count).  Each candidate signed integer bit width and scale is evaluated with
nearest-value quantization and saturating truncation::

    q = clamp(round_half_away_from_zero(x / scale), qmin, qmax)
    x_hat = q * scale

The reconstruction squared error of a channel is the frequency-weighted sum
over its table::

    sse = sum(count * (x_hat - x) ** 2)
    mse = sse / element_count

A group of contiguous channels shares one :class:`QuantSpec`; the spec is
feasible for the group only when *every* member channel satisfies
``mse <= tolerance``.  Groups are laid out independently as
``align_up(header_bytes + sum(channel_elements * bits // 8), alignment)``
bytes.  Dynamic programming over contiguous partitions then selects the
cheapest feasible layout.  If no fully feasible layout exists, a best-effort
layout minimizing the worst per-channel MSE violation is returned with
``feasible=False`` and populated ``violating_channels``.

All computations are local and deterministic; nothing is loaded from disk or
the network, and no payloads are generated or executed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import floor
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# A frequency table is either a {value: count} mapping or a sequence of
# (value, count) pairs.  Values may be ints or floats.
HistogramLike = Mapping[float, int] | Sequence[Tuple[float, int]]


@dataclass(frozen=True)
class QuantSpec:
    """A signed quantization configuration: integer bit width plus scale."""

    bits: int
    scale: float

    @property
    def qmin(self) -> int:
        return -(1 << (self.bits - 1))

    @property
    def qmax(self) -> int:
        return (1 << (self.bits - 1)) - 1


@dataclass(frozen=True)
class ChannelError:
    """Frequency-weighted reconstruction error for one channel/spec pair."""

    channel: int
    bits: int
    scale: float
    sse: float
    mse: float
    element_count: int
    saturated_count: int
    max_abs_error: float

    @property
    def spec(self) -> QuantSpec:
        return QuantSpec(self.bits, self.scale)


@dataclass(frozen=True)
class GroupSelection:
    """One chosen contiguous channel group and its shared quantization spec."""

    channels: Tuple[int, ...]
    bits: int
    scale: float
    payload_bytes: int
    total_bytes: int
    channel_errors: Tuple[ChannelError, ...]

    @property
    def spec(self) -> QuantSpec:
        return QuantSpec(self.bits, self.scale)


@dataclass(frozen=True)
class CalibrationResult:
    """Result of :func:`select_calibration`."""

    feasible: bool
    groups: Tuple[GroupSelection, ...]
    channel_errors: Tuple[ChannelError, ...]
    total_bytes: int
    tolerance_mse: float
    violating_channels: Tuple[int, ...]
    max_mse: float
    considered_specs: Tuple[QuantSpec, ...]

    def channel_group_map(self) -> Dict[int, GroupSelection]:
        """Map each channel index to the group that contains it."""
        mapping: Dict[int, GroupSelection] = {}
        for group in self.groups:
            for channel in group.channels:
                mapping[channel] = group
        return mapping


def _round_half_away_from_zero(value: float) -> int:
    """Round to nearest integer; exact halves go away from zero.

    Plain ``round()`` uses banker's rounding which would bias symmetric
    activation tables, so quantization uses the hardware-friendly
    half-away-from-zero rule instead.
    """
    if value >= 0:
        return floor(value + 0.5)
    return -floor(-value + 0.5)


def quantize_dequantize(
    values: Iterable[float], spec: QuantSpec
) -> List[float]:
    """Quantize then dequantize ``values`` under ``spec``.

    Nearest-value quantization with saturating truncation at the signed
    integer range.  Exposed as a small public helper so callers can audit
    individual reconstructions.
    """
    qmin, qmax, scale = spec.qmin, spec.qmax, spec.scale
    out: List[float] = []
    for value in values:
        q = _round_half_away_from_zero(value / scale)
        if q < qmin:
            q = qmin
        elif q > qmax:
            q = qmax
        out.append(q * scale)
    return out


def _normalize_histogram(histogram: HistogramLike) -> List[Tuple[float, int]]:
    if isinstance(histogram, Mapping):
        items = list(histogram.items())
    else:
        items = list(histogram)
    normalized: List[Tuple[float, int]] = []
    for value, count in items:
        value = float(value)
        count = int(count)
        if count < 0:
            raise ValueError("frequency table counts must be non-negative")
        if count:
            normalized.append((value, count))
    return normalized


def evaluate_channel(
    channel: int,
    histogram: HistogramLike,
    element_count: int,
    spec: QuantSpec,
) -> ChannelError:
    """Compute the frequency-weighted reconstruction error of one channel.

    ``element_count`` is the true number of elements in the channel.  The
    histogram counts must sum to exactly that number (zero-count entries may
    be omitted); a mismatch means the supplied table is incomplete and is
    rejected rather than silently reweighted.
    """
    if element_count <= 0:
        raise ValueError("element_count must be positive")
    table = _normalize_histogram(histogram)
    counted = sum(count for _, count in table)
    if counted != element_count:
        raise ValueError(
            f"channel {channel}: histogram counts sum to {counted}, "
            f"but element_count is {element_count}"
        )

    qmin, qmax, scale = spec.qmin, spec.qmax, spec.scale
    sse = 0.0
    saturated = 0
    max_abs_error = 0.0
    for value, count in table:
        q = _round_half_away_from_zero(value / scale)
        if q < qmin:
            q = qmin
            saturated += count
        elif q > qmax:
            q = qmax
            saturated += count
        reconstructed = q * scale
        err = reconstructed - value
        abs_err = abs(err)
        if abs_err > max_abs_error:
            max_abs_error = abs_err
        sse += count * err * err

    return ChannelError(
        channel=channel,
        bits=spec.bits,
        scale=spec.scale,
        sse=sse,
        mse=sse / element_count,
        element_count=element_count,
        saturated_count=saturated,
        max_abs_error=max_abs_error,
    )


def _default_spans(channel_count: int, max_group_size: int) -> Tuple[int, ...]:
    if max_group_size < 1:
        raise ValueError("max_group_size must be >= 1")
    upper = min(max_group_size, channel_count)
    return tuple(range(1, upper + 1))


def _align_up(value: int, alignment: int) -> int:
    if alignment <= 0:
        raise ValueError("alignment must be positive")
    return ((value + alignment - 1) // alignment) * alignment


@dataclass(frozen=True)
class _SpanChoice:
    """Best quantization spec found for one concrete contiguous span."""

    start: int
    length: int
    bits: int
    scale: float
    total_bytes: int
    errors: Tuple[ChannelError, ...]
    worst_mse: float
    violation: float


def _build_specs(
    supported_bits: Sequence[int], supported_scales: Sequence[float]
) -> Tuple[QuantSpec, ...]:
    if not supported_bits:
        raise ValueError("supported_bits must not be empty")
    if not supported_scales:
        raise ValueError("supported_scales must not be empty")
    seen: set[Tuple[int, float]] = set()
    specs: List[QuantSpec] = []
    for bits in supported_bits:
        if not isinstance(bits, int) or bits <= 1:
            raise ValueError("bit widths must be integers greater than 1")
        if bits % 8 != 0:
            raise ValueError(
                f"only byte-aligned widths are supported, got {bits} bits"
            )
        for scale in supported_scales:
            scale = float(scale)
            if scale <= 0.0:
                raise ValueError("scales must be positive finite numbers")
            key = (bits, scale)
            if key not in seen:
                seen.add(key)
                specs.append(QuantSpec(bits, scale))
    return tuple(specs)


def _evaluate_span(
    start: int,
    length: int,
    histograms: Sequence[HistogramLike],
    element_counts: Sequence[int],
    specs: Sequence[QuantSpec],
    tolerance_mse: float,
    header_bytes: int,
    alignment: int,
    relaxed: bool,
) -> Optional[_SpanChoice]:
    """Pick the cheapest spec for channels [start, start+length).

    In strict mode only specs whose every member channel meets the tolerance
    are considered.  In relaxed mode every spec is considered and the one
    minimizing the worst MSE violation wins (bytes break ties).
    """
    end = start + length
    span_elements = sum(element_counts[start:end])
    best: Optional[_SpanChoice] = None
    for spec in specs:
        errors = tuple(
            evaluate_channel(
                channel, histograms[channel], element_counts[channel], spec
            )
            for channel in range(start, end)
        )
        worst_mse = max(error.mse for error in errors)
        violation = max(0.0, worst_mse - tolerance_mse)
        if not relaxed and violation > 0.0:
            continue
        payload = span_elements * (spec.bits // 8)
        total = _align_up(header_bytes + payload, alignment)
        choice = _SpanChoice(
            start=start,
            length=length,
            bits=spec.bits,
            scale=spec.scale,
            total_bytes=total,
            errors=errors,
            worst_mse=worst_mse,
            violation=violation,
        )
        if best is None or _span_key(choice, relaxed) < _span_key(best, relaxed):
            best = choice
    return best


def _span_key(choice: _SpanChoice, relaxed: bool) -> Tuple[float, ...]:
    if relaxed:
        # Minimize worst violation first, then bytes, then finer error stats.
        return (
            choice.violation,
            float(choice.total_bytes),
            choice.worst_mse,
            float(choice.length),
        )
    return (float(choice.total_bytes), float(choice.length), choice.worst_mse)


def _dynamic_program(
    channel_count: int,
    spans: Sequence[int],
    choose_span,  # Callable[[int, int, bool], Optional[_SpanChoice]]
    relaxed: bool,
):
    """DP over contiguous partitions.

    Each state ``dp[i]`` covers channels [0, i) and stores
    ``(worst violation, worst MSE, total bytes, group count, choices)``.
    Strict mode ranks layouts by bytes first; relaxed mode ranks them by the
    worst MSE violation first.  Returns the chosen spans for the full range
    or ``None`` when no partition exists.
    """
    # (worst violation, worst mse, bytes, group count, choices)
    dp: List[Optional[Tuple[float, float, int, int, Tuple[_SpanChoice, ...]]]] = [
        None
    ] * (channel_count + 1)
    dp[0] = (0.0, 0.0, 0, 0, ())

    for i in range(channel_count):
        state = dp[i]
        if state is None:
            continue
        prev_violation, prev_worst_mse, prev_bytes, prev_groups, prev_choices = state
        for length in spans:
            if length > channel_count - i:
                break
            choice = choose_span(i, length, relaxed)
            if choice is None:
                continue
            total_bytes = prev_bytes + choice.total_bytes
            worst_violation = max(prev_violation, choice.violation)
            worst_mse = max(prev_worst_mse, choice.worst_mse)
            group_count = prev_groups + 1
            candidate = (
                worst_violation,
                worst_mse,
                total_bytes,
                group_count,
                prev_choices + (choice,),
            )
            if relaxed:
                key = (worst_violation, float(total_bytes), worst_mse, float(group_count))
            else:
                key = (float(total_bytes), float(group_count), worst_mse, worst_violation)
            old = dp[i + length]
            if old is None:
                dp[i + length] = candidate
            else:
                old_key = (
                    (old[0], float(old[2]), old[1], float(old[3]))
                    if relaxed
                    else (float(old[2]), float(old[3]), old[1], old[0])
                )
                if key < old_key:
                    dp[i + length] = candidate

    if dp[channel_count] is None:
        return None
    return list(dp[channel_count][4])


def select_calibration(
    histograms: Sequence[HistogramLike],
    element_counts: Sequence[int],
    supported_bits: Sequence[int],
    supported_scales: Sequence[float],
    tolerance_mse: float,
    header_bytes: int,
    alignment: int,
    groupable_ranges: Optional[Sequence[int]] = None,
    max_group_size: Optional[int] = None,
) -> CalibrationResult:
    """Select the cheapest feasible channel grouping and per-group scales.

    Parameters
    ----------
    histograms:
        One activation frequency table per channel.  Each table maps
        activation values to occurrence counts (or is a list of such pairs).
        Counts must sum to the matching ``element_counts`` entry.
    element_counts:
        Number of activation elements stored for each channel.
    supported_bits:
        Signed integer bit widths the device can load (must be byte
        aligned, e.g. ``[8, 16]``).
    supported_scales:
        Positive quantization scales the device supports.
    tolerance_mse:
        Maximum acceptable per-channel reconstruction MSE
        (``sse / element_count``).  A group is valid only when *all* of its
        member channels satisfy this bound.
    header_bytes:
        Per-group scale/metadata header cost in bytes.
    alignment:
        Group storage alignment granularity in bytes; each group occupies
        ``align_up(header_bytes + payload_bytes, alignment)`` bytes.
    groupable_ranges:
        Allowed contiguous group lengths (number of channels), e.g.
        ``[1, 2, 4]``.  Defaults to every length from 1 to
        ``max_group_size`` (or the full channel count when that is omitted).
    max_group_size:
        Convenience parameter used only when ``groupable_ranges`` is omitted.
    """
    channel_count = len(histograms)
    if channel_count == 0:
        raise ValueError("at least one channel is required")
    if len(element_counts) != channel_count:
        raise ValueError("element_counts must have one entry per channel")
    if any(count <= 0 for count in element_counts):
        raise ValueError("element counts must be positive")
    if header_bytes < 0:
        raise ValueError("header_bytes must be non-negative")
    if tolerance_mse < 0.0:
        raise ValueError("tolerance_mse must be non-negative")

    if groupable_ranges is None:
        limit = max_group_size if max_group_size is not None else channel_count
        spans = _default_spans(channel_count, limit)
    else:
        spans = tuple(sorted(set(groupable_ranges)))
        if not spans or any(span < 1 for span in spans):
            raise ValueError("groupable_ranges must contain positive lengths")
        if spans[-1] > channel_count:
            raise ValueError("groupable_ranges contains a span longer than the axis")

    specs = _build_specs(supported_bits, supported_scales)

    def choose_span(start: int, length: int, relaxed: bool) -> Optional[_SpanChoice]:
        return _evaluate_span(
            start,
            length,
            histograms,
            element_counts,
            specs,
            tolerance_mse,
            header_bytes,
            alignment,
            relaxed,
        )

    choices = _dynamic_program(channel_count, spans, choose_span, relaxed=False)
    feasible = choices is not None
    if not feasible:
        # No layout meets every channel's tolerance.  Fall back to the layout
        # minimizing the worst MSE violation so the caller still gets the
        # configuration plus concrete error evidence.
        choices = _dynamic_program(
            channel_count, spans, choose_span, relaxed=True
        )
    if choices is None:
        raise ValueError(
            "groupable_ranges cannot tile the full channel axis; "
            "ensure length 1 or another tileable combination is allowed"
        )

    groups: List[GroupSelection] = []
    all_errors: List[ChannelError] = []
    total_bytes = 0
    for choice in choices:
        end = choice.start + choice.length
        payload = sum(element_counts[choice.start:end]) * (choice.bits // 8)
        total = _align_up(header_bytes + payload, alignment)
        total_bytes += total
        group = GroupSelection(
            channels=tuple(range(choice.start, end)),
            bits=choice.bits,
            scale=choice.scale,
            payload_bytes=payload,
            total_bytes=total,
            channel_errors=choice.errors,
        )
        groups.append(group)
        all_errors.extend(choice.errors)

    all_errors.sort(key=lambda error: error.channel)
    violating = tuple(
        error.channel
        for error in all_errors
        if error.mse > tolerance_mse
    )
    max_mse = max(error.mse for error in all_errors)

    return CalibrationResult(
        feasible=feasible,
        groups=tuple(groups),
        channel_errors=tuple(all_errors),
        total_bytes=total_bytes,
        tolerance_mse=tolerance_mse,
        violating_channels=violating,
        max_mse=max_mse,
        considered_specs=specs,
    )
