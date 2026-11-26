"""Geometry accounting for committable MTU/payload sizes.

The module is intentionally deterministic and dependency free.  Its central
object is :class:`PathSpec`, which records one forwarding direction's ordered
hops, nested encapsulation layers, alignment and fragmentation policy.

All byte sizes are non-negative integers.  MTU intervals use the convention
``[lower, upper]`` where ``lower`` is the smallest MTU the caller must be
ready to encounter and ``upper`` is an optional optimistic upper bound.
``None`` means that endpoint is unknown/unbounded.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Errors and low-level validation
# ---------------------------------------------------------------------------


class GeometryError(ValueError):
    """Base class for invalid geometry inputs and strict commitment failures."""


class InvalidSizeError(GeometryError):
    """A byte size, MTU or alignment value is invalid."""


class OverheadDomainError(GeometryError):
    """An overhead function deliberately rejects an input size."""


class PathInformationMissing(GeometryError):
    """Raised by ``require_committed`` when a path cannot be confirmed."""

    def __init__(self, report: "CommitmentReport") -> None:
        self.report = report
        blocker = report.blocker
        hop = getattr(blocker, "hop", None)
        hop_text = f"hop[{hop.index}]={hop.hop_id!r}" if hop else "an unknown hop"
        required = getattr(blocker, "required_mtu", None)
        super().__init__(
            f"commitment for payload={report.payload} is blocked by missing "
            f"MTU information at {hop_text}; required_mtu={required}"
        )


class CommitmentRejected(GeometryError):
    """Raised by ``require_committed`` for a concrete size/fragmentation failure."""

    def __init__(self, report: "CommitmentReport") -> None:
        self.report = report
        super().__init__(
            f"payload {report.payload} cannot be committed on "
            f"{report.path_id!r}: status={report.status}, blocker={report.blocker!r}"
        )


def byte_count(value: Any, name: str, *, minimum: int = 0) -> int:
    """Validate and return a non-negative integer byte count."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidSizeError(f"{name} must be an integer byte count, got {value!r}")
    if value < minimum:
        raise InvalidSizeError(f"{name} must be >= {minimum}, got {value}")
    return value


def _positive_int(value: Any, name: str) -> int:
    return byte_count(value, name, minimum=1)


class Direction:
    """Canonical directional labels; arbitrary strings are also accepted."""

    FORWARD = "forward"
    REVERSE = "reverse"
    UPLINK = "forward"
    DOWNLINK = "reverse"


# ---------------------------------------------------------------------------
# Time/probe windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """A half-open confirmed interval ``[start, end)``.

    Half-open intervals make adjacent probe periods composable:
    ``[0, 10)`` and ``[10, 20)`` do not overlap in positive time, while their
    union is the continuous interval ``[0, 20)``.
    """

    start: Any
    end: Any

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise GeometryError(f"window end {self.end!r} precedes start {self.start!r}")

    def contains(self, moment: Any) -> bool:
        return self.start <= moment < self.end


WindowLike = Window | tuple[Any, Any] | Sequence[Any] | Mapping[str, Any]


def coerce_window(value: WindowLike) -> Window:
    if isinstance(value, Window):
        return value
    if isinstance(value, Mapping):
        if "start" in value and "end" in value:
            return Window(value["start"], value["end"])
        raise GeometryError(f"window mapping must contain start/end: {value!r}")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 2:
            return Window(value[0], value[1])
    raise GeometryError(f"cannot interpret {value!r} as a (start, end) window")


def normalize_windows(windows: Iterable[WindowLike] | None) -> tuple[Window, ...]:
    """Sort, validate and merge overlapping/adjacent half-open windows."""

    if not windows:
        return ()
    items = [coerce_window(w) for w in windows]
    items = [w for w in items if w.start < w.end]
    if not items:
        return ()
    items.sort(key=lambda w: w.start)

    merged: list[Window] = [items[0]]
    for window in items[1:]:
        current = merged[-1]
        if window.start <= current.end:
            end = current.end if current.end >= window.end else window.end
            merged[-1] = Window(current.start, end)
        else:
            merged.append(window)
    return tuple(merged)


def intersect_windows(
    left: Iterable[WindowLike] | None,
    right: Iterable[WindowLike] | None,
) -> tuple[Window, ...]:
    """Intersect two window collections, returning normalized half-open windows."""

    a = normalize_windows(left)
    b = normalize_windows(right)
    i = j = 0
    result: list[Window] = []
    while i < len(a) and j < len(b):
        first, second = a[i], b[j]
        start = max(first.start, second.start)
        end = min(first.end, second.end)
        if start < end:
            result.append(Window(start, end))
        if first.end < second.end:
            i += 1
        elif first.end > second.end:
            j += 1
        else:
            i += 1
            j += 1
    return tuple(result)


def intersect_all_windows(
    collections: Sequence[Iterable[WindowLike] | None],
) -> tuple[Window, ...]:
    if not collections:
        return ()
    result = normalize_windows(collections[0])
    for collection in collections[1:]:
        result = intersect_windows(result, collection)
        if not result:
            return ()
    return result


@dataclass(frozen=True)
class AvailabilitySegment:
    """A maximal interval with a fixed sorted set of available path IDs."""

    window: Window
    path_ids: tuple[str, ...]

    @property
    def path_count(self) -> int:
        return len(self.path_ids)


def availability_timeline(
    availability: Mapping[str, Iterable[WindowLike]],
) -> tuple[AvailabilitySegment, ...]:
    """Build adjacent ``[start, end)`` segments and the paths active in each."""

    normalized = {
        path_id: normalize_windows(windows)
        for path_id, windows in availability.items()
    }
    points = sorted(
        {point for windows in normalized.values() for w in windows for point in (w.start, w.end)}
    )
    segments: list[AvailabilitySegment] = []
    for left, right in zip(points, points[1:]):
        window = Window(left, right)
        if left >= right:
            continue
        active = tuple(
            sorted(
                path_id
                for path_id, windows in normalized.items()
                if any(w.start <= left < w.end for w in windows)
            )
        )
        if not active:
            continue
        if (
            segments
            and segments[-1].path_ids == active
            and segments[-1].window.end == left
        ):
            previous = segments[-1].window
            segments[-1] = AvailabilitySegment(Window(previous.start, right), active)
        else:
            segments.append(AvailabilitySegment(window, active))
    return tuple(segments)


# ---------------------------------------------------------------------------
# MTU intervals, alignment and step overhead
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MtuInterval:
    """Known effective-MTU bounds.

    ``lower`` is the conservative capacity used for promises.  ``upper`` is an
    optimistic capacity used to state a verifiable size ceiling.  ``None`` on
    an endpoint means unknown or unbounded depending on the endpoint.
    """

    lower: int | None = None
    upper: int | None = None

    def __post_init__(self) -> None:
        if self.lower is not None:
            object.__setattr__(self, "lower", _positive_int(self.lower, "mtu.lower"))
        if self.upper is not None:
            object.__setattr__(self, "upper", _positive_int(self.upper, "mtu.upper"))
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise InvalidSizeError(
                f"MTU lower bound {self.lower} exceeds upper bound {self.upper}"
            )

    @classmethod
    def exact(cls, mtu: int) -> "MtuInterval":
        value = _positive_int(mtu, "mtu")
        return cls(value, value)

    @classmethod
    def unknown(cls) -> "MtuInterval":
        return cls(None, None)

    @classmethod
    def coerce(cls, value: "MtuIntervalLike") -> "MtuInterval":
        if isinstance(value, MtuInterval):
            return value
        if value is None:
            return cls.unknown()
        if isinstance(value, bool):
            raise InvalidSizeError(f"MTU cannot be boolean: {value!r}")
        if isinstance(value, int):
            return cls.exact(value)
        if isinstance(value, Mapping):
            lower = value.get("lower", value.get("min", value.get("minimum")))
            upper = value.get("upper", value.get("max", value.get("maximum")))
            return cls(lower, upper)  # type: ignore[arg-type]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) == 1:
                return cls.exact(value[0])
            if len(value) == 2:
                return cls(value[0], value[1])  # type: ignore[arg-type]
        raise InvalidSizeError(f"cannot interpret {value!r} as an MTU interval")


MtuIntervalLike = MtuInterval | int | tuple[int | None, int | None] | Mapping[str, Any] | None


@dataclass(frozen=True)
class Alignment:
    """Smallest aligned size not smaller than the input.

    The returned size satisfies ``(size - offset) % block == 0``.
    """

    block: int
    offset: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "block", _positive_int(self.block, "alignment.block"))
        offset = byte_count(self.offset, "alignment.offset")
        if offset >= self.block:
            raise InvalidSizeError("alignment.offset must satisfy 0 <= offset < block")
        object.__setattr__(self, "offset", offset)

    def pad(self, size: int) -> int:
        value = byte_count(size, "size")
        return value + (self.offset - value) % self.block


AlignmentLike = Alignment | int | Callable[[int], int] | None


def apply_alignment(alignment: AlignmentLike, size: int, label: str) -> int:
    if alignment is None:
        return size
    if isinstance(alignment, Alignment):
        aligned = alignment.pad(size)
    elif isinstance(alignment, int) and not isinstance(alignment, bool):
        aligned = Alignment(alignment).pad(size)
    elif callable(alignment):
        aligned = alignment(size)
    else:
        raise InvalidSizeError(f"{label} must be an Alignment, positive int or callable")
    aligned = byte_count(aligned, f"{label} result")
    if aligned < size:
        raise InvalidSizeError(f"{label} shrank size {size} to {aligned}; padding must not shrink")
    return aligned


@dataclass(frozen=True)
class OverheadTier:
    """A constant overhead tier beginning at inclusive ``start``."""

    start: int
    overhead: int
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", byte_count(self.start, "tier.start"))
        object.__setattr__(self, "overhead", byte_count(self.overhead, "tier.overhead"))


OverheadLike = Callable[[int], int] | int | Sequence[Any] | None


class StepOverhead:
    """Piecewise-constant overhead with inclusive start thresholds.

    Example ``[(0, 10), (129, 40)]`` means overhead 10 for sizes 0..128 and
    40 for sizes >= 129.  The jump therefore occurs at size 129.
    """

    def __init__(self, tiers: Sequence[OverheadTier | tuple[Any, ...] | Mapping[str, Any]]):
        normalized: list[OverheadTier] = []
        for tier in tiers:
            if isinstance(tier, OverheadTier):
                normalized.append(tier)
            elif isinstance(tier, Mapping):
                normalized.append(
                    OverheadTier(
                        tier.get("start", tier.get("from")),
                        tier["overhead"],
                        tier.get("label", ""),
                    )
                )
            elif isinstance(tier, Sequence) and not isinstance(tier, (str, bytes)):
                if len(tier) == 2:
                    normalized.append(OverheadTier(tier[0], tier[1]))
                elif len(tier) == 3:
                    normalized.append(OverheadTier(tier[0], tier[1], tier[2]))
                else:
                    raise GeometryError(f"invalid overhead tier {tier!r}")
            else:
                raise GeometryError(f"invalid overhead tier {tier!r}")

        if not normalized:
            raise GeometryError("StepOverhead requires at least one tier")
        normalized.sort(key=lambda t: t.start)
        if normalized[0].start != 0:
            raise GeometryError("the first overhead tier must start at 0")
        starts = [t.start for t in normalized]
        if len(set(starts)) != len(starts):
            raise GeometryError("overhead tier starts must be unique")
        for previous, following in zip(normalized, normalized[1:]):
            if following.overhead < previous.overhead:
                raise GeometryError(
                    "step overhead must be non-decreasing; model sawtooth padding "
                    "with Alignment or a custom monotonic frame function"
                )
        self._tiers = tuple(normalized)
        self._starts = tuple(starts)

    @classmethod
    def by_upper_bounds(
        cls, brackets: Sequence[tuple[int | None, int] | tuple[int | None, int, str]]
    ) -> "StepOverhead":
        """Create tiers from inclusive upper bounds.

        ``[(128, 10), (256, 20), (None, 30)]`` is equivalent to starts
        ``[(0, 10), (129, 20), (257, 30)]``.
        """

        if not brackets:
            raise GeometryError("at least one bracket is required")
        starts: list[tuple[Any, ...]] = []
        next_start = 0
        for bracket in brackets:
            upper, overhead = bracket[0], bracket[1]
            label = bracket[2] if len(bracket) > 2 else ""
            if upper is None:
                starts.append((0 if not starts else next_start, overhead, label))
                if bracket is not brackets[-1]:
                    raise GeometryError("an unbounded overhead bracket must be last")
            else:
                upper = _positive_int(upper, "bracket.upper") if upper > 0 else byte_count(
                    upper, "bracket.upper"
                )
                if upper < next_start:
                    raise GeometryError("overhead bracket upper bounds must increase")
                starts.append((0 if not starts else next_start, overhead, label))
                next_start = upper + 1
        return cls(starts)

    @property
    def tiers(self) -> tuple[OverheadTier, ...]:
        return self._tiers

    def __call__(self, input_size: int) -> int:
        size = byte_count(input_size, "input_size")
        index = bisect_right(self._starts, size) - 1
        return self._tiers[index].overhead


def coerce_overhead(value: OverheadLike) -> Callable[[int], int]:
    if value is None:
        return lambda _size: 0
    if isinstance(value, bool):
        raise InvalidSizeError("overhead cannot be boolean")
    if isinstance(value, int):
        amount = byte_count(value, "constant overhead")
        return lambda _size, amount=amount: amount
    if callable(value):
        return value
    if isinstance(value, Sequence):
        return StepOverhead(value)
    raise InvalidSizeError(f"cannot interpret {value!r} as overhead")


# ---------------------------------------------------------------------------
# Paths and encapsulation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hop:
    hop_id: str
    mtu: MtuInterval = field(default_factory=MtuInterval.unknown)
    #: None means no independent time constraint; () means explicitly none.
    confirmed_windows: Sequence[WindowLike] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.hop_id, str) or not self.hop_id:
            raise GeometryError("hop_id must be a non-empty string")
        object.__setattr__(self, "mtu", MtuInterval.coerce(self.mtu))
        if self.confirmed_windows is not None:
            object.__setattr__(
                self,
                "confirmed_windows",
                tuple(coerce_window(window) for window in self.confirmed_windows),
            )


HopLike = Hop | int | tuple[Any, ...] | Mapping[str, Any]


def coerce_hop(value: HopLike, index: int) -> Hop:
    if isinstance(value, Hop):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Hop(f"hop-{index + 1}", MtuInterval.exact(value))
    if isinstance(value, Mapping):
        hop_id = value.get("hop_id", value.get("id", f"hop-{index + 1}"))
        windows = value.get("confirmed_windows", value.get("windows"))
        return Hop(
            str(hop_id),
            MtuInterval.coerce(value.get("mtu")),
            None if windows is None else tuple(windows),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 3:
            hop_id, mtu, windows = value
            return Hop(
                str(hop_id),
                MtuInterval.coerce(mtu),
                None if windows is None else tuple(windows),
            )
        if len(value) == 2:
            hop_id, mtu = value
            return Hop(str(hop_id), MtuInterval.coerce(mtu))
        if len(value) == 1:
            return Hop(f"hop-{index + 1}", MtuInterval.coerce(value[0]))
    raise GeometryError(f"cannot interpret hop {value!r} at index {index}")


@dataclass(frozen=True)
class TunnelLayer:
    """One nested tunnel.

    ``overhead(size)`` returns added bytes for the packet size entering this
    layer.  Layers are supplied innermost-first, so an outer overhead observes
    the size after all inner layers.
    """

    overhead: OverheadLike = 0
    name: str = ""
    input_alignment: AlignmentLike = None
    output_alignment: AlignmentLike = None


LayerLike = TunnelLayer | Callable[[int], int] | int | Sequence[Any]


def coerce_layer(value: LayerLike) -> TunnelLayer:
    if isinstance(value, TunnelLayer):
        return value
    if callable(value) or isinstance(value, int) or isinstance(value, Sequence):
        return TunnelLayer(overhead=value)  # type: ignore[arg-type]
    raise GeometryError(f"cannot interpret {value!r} as a tunnel layer")


@dataclass(frozen=True)
class FragmentationPolicy:
    """Conservative geometry for non-DF fragmentation.

    The default values describe ordinary IPv4-style fragments: 20-byte
    headers and 8-byte alignment for all non-last fragment payloads.
    """

    fragment_header_bytes: int = 20
    packet_header_bytes: int | None = None
    data_alignment: Alignment | int | None = 8
    max_datagram_bytes: int | None = 65535

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "fragment_header_bytes",
            _positive_int(self.fragment_header_bytes, "fragment_header_bytes"),
        )
        if self.packet_header_bytes is not None:
            object.__setattr__(
                self,
                "packet_header_bytes",
                byte_count(self.packet_header_bytes, "packet_header_bytes"),
            )
        if isinstance(self.data_alignment, int) and not isinstance(
            self.data_alignment, Alignment
        ):
            if self.data_alignment not in (0, 1):
                object.__setattr__(
                    self, "data_alignment", _positive_int(self.data_alignment, "data_alignment")
                )
        if self.max_datagram_bytes is not None:
            object.__setattr__(
                self,
                "max_datagram_bytes",
                _positive_int(self.max_datagram_bytes, "max_datagram_bytes"),
            )

    @classmethod
    def ipv4(cls) -> "FragmentationPolicy":
        return cls(20, 20, 8, 65535)

    @classmethod
    def generic(cls, header_bytes: int = 0, data_alignment: int | None = None) -> "FragmentationPolicy":
        return cls(header_bytes, header_bytes, data_alignment, None)


@dataclass
class PathSpec:
    path_id: str = "path"
    hops: Sequence[HopLike] = field(default_factory=list)
    layers: Sequence[LayerLike] = field(default_factory=tuple)
    #: Alignment applied to the application payload before encapsulation.
    payload_alignment: AlignmentLike = None
    #: Transmission alignment applied after all nested encapsulation.
    alignment: AlignmentLike = None
    dont_fragment: bool = True
    #: None means no path-level window constraint; an empty list means none confirmed.
    confirmed_windows: Sequence[WindowLike] | None = None
    direction: str | None = None
    fragmentation: FragmentationPolicy | None = None
    #: Optional administrative floor used only for conditional lower bounds.
    assumed_floor_mtu: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path_id, str) or not self.path_id:
            raise GeometryError("path_id must be a non-empty string")
        if not isinstance(self.dont_fragment, bool):
            raise GeometryError("dont_fragment must be a boolean")
        self.hops = tuple(coerce_hop(h, i) for i, h in enumerate(self.hops))
        if not self.hops:
            raise GeometryError(f"path {self.path_id!r} must contain at least one hop")
        self.layers = tuple(coerce_layer(layer) for layer in self.layers)
        if self.confirmed_windows is not None:
            self.confirmed_windows = tuple(
                coerce_window(window) for window in self.confirmed_windows
            )
        if self.assumed_floor_mtu is not None:
            self.assumed_floor_mtu = _positive_int(
                self.assumed_floor_mtu, "assumed_floor_mtu"
            )

    def effective_windows(self) -> tuple[Window, ...]:
        """Intersect path-level and optional per-hop confirmation windows."""

        collections: list[Sequence[Window]] = []
        if self.confirmed_windows is not None:
            collections.append(normalize_windows(self.confirmed_windows))
        for hop in self.hops:
            if hop.confirmed_windows is not None:
                collections.append(normalize_windows(hop.confirmed_windows))
        if not collections:
            return ()
        return intersect_all_windows(collections)

    def frame_size(self, payload: int) -> int:
        """Return transmitted frame size after alignment and nested layers."""

        size = byte_count(payload, "payload")
        size = apply_alignment(self.payload_alignment, size, "payload_alignment")
        for index, layer in enumerate(self.layers):
            label = f"layer[{index}]={layer.name or index}"
            size = apply_alignment(layer.input_alignment, size, f"{label}.input_alignment")
            overhead_fn = coerce_overhead(layer.overhead)
            try:
                added = overhead_fn(size)
            except OverheadDomainError:
                raise
            except GeometryError:
                raise
            except Exception as exc:  # Keep arbitrary callable failures informative.
                raise OverheadDomainError(f"{label} overhead failed for size {size}: {exc}") from exc
            added = byte_count(added, f"{label}.overhead({size})")
            size += added
            size = apply_alignment(layer.output_alignment, size, f"{label}.output_alignment")
        size = apply_alignment(self.alignment, size, "transmission_alignment")
        return size


# ---------------------------------------------------------------------------
# Reports and certificates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HopRef:
    index: int
    hop_id: str


@dataclass(frozen=True)
class SizeWitness:
    """A recomputable size inequality."""

    payload: int | None
    frame_size: int | None
    mtu: int | None
    hop: HopRef | None
    comparator: str
    holds: bool
    note: str = ""

    def expression(self) -> str:
        hop = self.hop.hop_id if self.hop else "path"
        return (
            f"payload={self.payload}, frame={self.frame_size} "
            f"{self.comparator} mtu={self.mtu} at {hop}"
        )


@dataclass(frozen=True)
class HopSizeReport:
    hop: HopRef
    mtu: MtuInterval
    safe_mtu: int | None
    optimistic_mtu: int | None
    max_payload: int | None
    optimistic_max_payload: int | None
    assumed_max_payload: int | None = None


@dataclass(frozen=True)
class PayloadBounds:
    #: Largest payload guaranteed under all known lower MTU endpoints.
    guaranteed_max: int | None
    #: Largest payload permitted by all optimistic/upper endpoints.
    optimistic_max: int | None
    #: First integer payload proven impossible by known constraints.
    proven_impossible_from: int | None
    limiting_hop: HopRef | None
    frame_at_guaranteed: int | None
    frame_at_ceiling: int | None
    witnesses: tuple[SizeWitness, ...] = ()
    conditional_floor_max: int | None = None
    conditional_floor_mtu: int | None = None


@dataclass(frozen=True)
class PathSizeReport:
    path_id: str
    direction: str | None
    bounds: PayloadBounds
    hops: tuple[HopSizeReport, ...]
    minimum_frame_size: int | None
    status: str
    missing_hop: HopRef | None
    bottleneck: HopRef | None
    error: str | None = None


@dataclass(frozen=True)
class FragmentationPlan:
    feasible: bool
    required: bool
    min_mtu: int | None
    original_frame_size: int
    fragment_count: int | None = None
    fragment_data_bytes: tuple[int, ...] = ()
    fragment_sizes: tuple[int, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class SizeViolation:
    kind: str
    hop: HopRef
    payload: int
    frame_size: int
    mtu: int
    excess: int


@dataclass(frozen=True)
class MissingInformation:
    kind: str
    hop: HopRef
    payload: int | None
    required_mtu: int | None
    bounds: PayloadBounds
    missing_lower_mtu: bool = True
    missing_upper_mtu: bool = True
    message: str = ""


@dataclass(frozen=True)
class CommitmentReport:
    path_id: str
    direction: str | None
    payload: int
    committed: bool
    status: str
    dont_fragment: bool
    frame_size: int | None
    size_report: PathSizeReport
    violations: tuple[SizeViolation, ...] = ()
    missing: tuple[MissingInformation, ...] = ()
    fragmentation: FragmentationPlan | None = None
    blocker: Any = None

    def require(self) -> "CommitmentReport":
        if self.committed:
            return self
        if self.status == "incomplete":
            raise PathInformationMissing(self)
        raise CommitmentRejected(self)


# ---------------------------------------------------------------------------
# Size solving
# ---------------------------------------------------------------------------


def _try_frame_size(path: PathSpec, payload: int) -> tuple[int | None, OverheadDomainError | None]:
    try:
        return path.frame_size(payload), None
    except OverheadDomainError as exc:
        return None, exc


def _max_for_capacity(path: PathSpec, capacity: int | None) -> int | None:
    """Largest payload with ``frame_size(payload) <= capacity``.

    ``None`` capacity means no MTU-derived finite cap.  The encapsulation
    model is contracted to be monotone non-decreasing.  Since every valid
    alignment/padding operation is non-shrinking and overhead is non-negative,
    ``frame_size(p) >= p``, giving a finite binary-search endpoint.
    """

    if capacity is None:
        return None
    capacity = _positive_int(capacity, "capacity")
    size0, error = _try_frame_size(path, 0)
    if error is not None or size0 is None or size0 > capacity:
        return None

    low = 0
    high = capacity + 1  # frame(high) >= high > capacity, without evaluating it.
    while low + 1 < high:
        middle = (low + high) // 2
        size, error = _try_frame_size(path, middle)
        if error is None and size is not None and size <= capacity:
            low = middle
        else:
            high = middle
    return low


def _hop_ref(hop: Hop, index: int) -> HopRef:
    return HopRef(index=index, hop_id=hop.hop_id)


def calculate_size_bounds(path: PathSpec) -> PathSizeReport:
    """Calculate guaranteed/optimistic payload bounds for one path."""

    if not isinstance(path, PathSpec):
        raise TypeError("calculate_size_bounds expects a PathSpec")

    minimum_frame, minimum_error = _try_frame_size(path, 0)
    hop_reports: list[HopSizeReport] = []
    guaranteed_values: list[tuple[int, HopRef]] = []
    optimistic_values: list[int] = []
    finite_optimistic: list[tuple[int, HopRef]] = []
    missing_hop: HopRef | None = None

    for index, hop in enumerate(path.hops):
        ref = _hop_ref(hop, index)
        safe_capacity = hop.mtu.lower
        optimistic_capacity = hop.mtu.upper
        if optimistic_capacity is None and safe_capacity is not None:
            # If only a lower endpoint is known, it is also a known exact-ish
            # constraint but does not provide an upper interval endpoint.
            optimistic_capacity = None

        max_safe = _max_for_capacity(path, safe_capacity) if safe_capacity is not None else None
        max_optimistic = (
            _max_for_capacity(path, optimistic_capacity)
            if optimistic_capacity is not None
            else None
        )
        assumed_max: int | None = None
        if safe_capacity is None and path.assumed_floor_mtu is not None:
            assumed_max = _max_for_capacity(path, path.assumed_floor_mtu)

        report = HopSizeReport(
            hop=ref,
            mtu=hop.mtu,
            safe_mtu=safe_capacity,
            optimistic_mtu=optimistic_capacity,
            max_payload=max_safe,
            optimistic_max_payload=max_optimistic,
            assumed_max_payload=assumed_max,
        )
        hop_reports.append(report)

        if safe_capacity is not None and max_safe is not None:
            guaranteed_values.append((max_safe, ref))
        elif safe_capacity is None and missing_hop is None:
            missing_hop = ref

        if optimistic_capacity is not None and max_optimistic is not None:
            optimistic_values.append(max_optimistic)
            finite_optimistic.append((max_optimistic, ref))

    guaranteed_max: int | None
    bottleneck: HopRef | None
    if len(guaranteed_values) == len(path.hops):
        guaranteed_max, bottleneck = min(guaranteed_values, key=lambda item: (item[0], item[1].index))
    else:
        guaranteed_max, bottleneck = None, None

    optimistic_max = min(optimistic_values) if optimistic_values else None
    optimistic_limiter = (
        min(finite_optimistic, key=lambda item: (item[0], item[1].index))[1]
        if finite_optimistic
        else None
    )

    witnesses: list[SizeWitness] = []
    frame_at_guaranteed: int | None = None
    frame_at_ceiling: int | None = None
    proven_impossible_from: int | None = None
    limiting_hop: HopRef | None = bottleneck

    if guaranteed_max is not None and bottleneck is not None:
        frame_at_guaranteed = path.frame_size(guaranteed_max)
        proven_impossible_from = guaranteed_max + 1
        frame_at_ceiling = path.frame_size(proven_impossible_from)
        bottleneck_mtu = path.hops[bottleneck.index].mtu.lower
        witnesses.append(
            SizeWitness(
                payload=guaranteed_max,
                frame_size=frame_at_guaranteed,
                mtu=bottleneck_mtu,
                hop=bottleneck,
                comparator="<=",
                holds=frame_at_guaranteed <= (bottleneck_mtu or 0),
                note="largest guaranteed witness",
            )
        )
        witnesses.append(
            SizeWitness(
                payload=proven_impossible_from,
                frame_size=frame_at_ceiling,
                mtu=bottleneck_mtu,
                hop=bottleneck,
                comparator=">",
                holds=frame_at_ceiling > (bottleneck_mtu or 0),
                note="first integer above guarantee",
            )
        )
    elif optimistic_max is not None:
        limiting_hop = optimistic_limiter
        proven_impossible_from = optimistic_max + 1
        frame_at_ceiling, _ = _try_frame_size(path, proven_impossible_from)
        optimistic_mtu = (
            path.hops[optimistic_limiter.index].mtu.upper if optimistic_limiter else None
        )
        if frame_at_ceiling is not None and optimistic_limiter is not None:
            witnesses.append(
                SizeWitness(
                    payload=proven_impossible_from,
                    frame_size=frame_at_ceiling,
                    mtu=optimistic_mtu,
                    hop=optimistic_limiter,
                    comparator=">",
                    holds=optimistic_mtu is not None and frame_at_ceiling > optimistic_mtu,
                    note="optimistic upper-bound violation",
                )
            )

    conditional_floor_max: int | None = None
    if missing_hop is not None and path.assumed_floor_mtu is not None:
        conditional_values: list[int] = []
        for report in hop_reports:
            if report.max_payload is not None:
                conditional_values.append(report.max_payload)
            elif report.assumed_max_payload is not None:
                conditional_values.append(report.assumed_max_payload)
        if len(conditional_values) == len(hop_reports):
            conditional_floor_max = min(conditional_values)

    bounds = PayloadBounds(
        guaranteed_max=guaranteed_max,
        optimistic_max=optimistic_max,
        proven_impossible_from=proven_impossible_from,
        limiting_hop=limiting_hop,
        frame_at_guaranteed=frame_at_guaranteed,
        frame_at_ceiling=frame_at_ceiling,
        witnesses=tuple(witnesses),
        conditional_floor_max=conditional_floor_max,
        conditional_floor_mtu=path.assumed_floor_mtu,
    )
    any_known_hop_infeasible = any(
        report.safe_mtu is not None and report.max_payload is None
        for report in hop_reports
    )
    if minimum_error is not None:
        status = "encapsulation_rejected"
    elif any_known_hop_infeasible:
        status = "no_feasible_payload"
    elif missing_hop is not None:
        status = "incomplete"
    else:
        status = "complete"
    return PathSizeReport(
        path_id=path.path_id,
        direction=path.direction,
        bounds=bounds,
        hops=tuple(hop_reports),
        minimum_frame_size=minimum_frame,
        status=status,
        missing_hop=missing_hop,
        bottleneck=bottleneck,
        error=str(minimum_error) if minimum_error is not None else None,
    )


def maximum_safe_payload(path: PathSpec) -> int | None:
    """Return the conservative maximum unfragmented payload, if fully known."""

    return calculate_size_bounds(path).bounds.guaranteed_max


def verify_bounds(
    path: PathSpec,
    report: PathSizeReport | CommitmentReport | None = None,
) -> tuple[SizeWitness, ...]:
    """Recompute per-hop boundary witnesses from the original path model."""

    if report is None:
        size_report = calculate_size_bounds(path)
    elif isinstance(report, CommitmentReport):
        size_report = report.size_report
    else:
        size_report = report

    checks: list[SizeWitness] = []
    for hop_report in size_report.hops:
        ref = hop_report.hop

        if hop_report.max_payload is not None and hop_report.safe_mtu is not None:
            payload = hop_report.max_payload
            frame = path.frame_size(payload)
            checks.append(
                SizeWitness(
                    payload=payload,
                    frame_size=frame,
                    mtu=hop_report.safe_mtu,
                    hop=ref,
                    comparator="<=",
                    holds=frame <= hop_report.safe_mtu,
                    note="hop-specific guaranteed boundary",
                )
            )

            next_payload = payload + 1
            next_frame, error = _try_frame_size(path, next_payload)
            if error is not None or next_frame is None:
                checks.append(
                    SizeWitness(
                        payload=next_payload,
                        frame_size=None,
                        mtu=hop_report.safe_mtu,
                        hop=ref,
                        comparator="reject",
                        holds=True,
                        note="overhead function rejects the next size",
                    )
                )
            else:
                checks.append(
                    SizeWitness(
                        payload=next_payload,
                        frame_size=next_frame,
                        mtu=hop_report.safe_mtu,
                        hop=ref,
                        comparator=">",
                        holds=next_frame > hop_report.safe_mtu,
                        note="first size above hop-specific boundary",
                    )
                )

        if (
            hop_report.optimistic_max_payload is not None
            and hop_report.optimistic_mtu is not None
        ):
            payload = hop_report.optimistic_max_payload
            frame = path.frame_size(payload)
            checks.append(
                SizeWitness(
                    payload=payload,
                    frame_size=frame,
                    mtu=hop_report.optimistic_mtu,
                    hop=ref,
                    comparator="<=",
                    holds=frame <= hop_report.optimistic_mtu,
                    note="hop-specific optimistic boundary",
                )
            )

    return tuple(checks)


# ---------------------------------------------------------------------------
# Fragmentation
# ---------------------------------------------------------------------------


def plan_fragmentation(
    *,
    frame_size: int,
    min_mtu: int | None,
    policy: FragmentationPolicy,
) -> FragmentationPlan:
    """Plan fragments using integer, verifiable chunk bounds."""

    frame = byte_count(frame_size, "frame_size")
    if policy.max_datagram_bytes is not None and frame > policy.max_datagram_bytes:
        return FragmentationPlan(
            feasible=False,
            required=True,
            min_mtu=min_mtu,
            original_frame_size=frame,
            reason=f"frame {frame} exceeds datagram maximum {policy.max_datagram_bytes}",
        )
    if min_mtu is None:
        return FragmentationPlan(
            feasible=False,
            required=True,
            min_mtu=None,
            original_frame_size=frame,
            reason="cannot fragment without a known minimum MTU",
        )

    header = policy.fragment_header_bytes
    original_header = (
        policy.packet_header_bytes
        if policy.packet_header_bytes is not None
        else header
    )
    if frame < original_header:
        return FragmentationPlan(
            feasible=False,
            required=False,
            min_mtu=min_mtu,
            original_frame_size=frame,
            reason=f"frame {frame} is smaller than modeled header {original_header}",
        )

    cap = min_mtu - header
    if cap <= 0:
        return FragmentationPlan(
            feasible=False,
            required=True,
            min_mtu=min_mtu,
            original_frame_size=frame,
            reason=f"MTU {min_mtu} leaves no data bytes after fragment header {header}",
        )

    payload_bytes = frame - original_header
    if payload_bytes <= cap:
        sizes = (frame,)
        return FragmentationPlan(
            feasible=True,
            required=False,
            min_mtu=min_mtu,
            original_frame_size=frame,
            fragment_count=1,
            fragment_data_bytes=(payload_bytes,),
            fragment_sizes=sizes,
        )

    alignment = policy.data_alignment
    if isinstance(alignment, int):
        block = alignment
    elif isinstance(alignment, Alignment):
        block = alignment.block
    else:
        block = 1
    step = (cap // block) * block if block > 1 else cap
    if step <= 0:
        return FragmentationPlan(
            feasible=False,
            required=True,
            min_mtu=min_mtu,
            original_frame_size=frame,
            reason=f"fragment data cap {cap} cannot satisfy alignment {block}",
        )

    # Smallest n with (n-1)*step + cap >= payload_bytes.
    fragment_count = 2 + (payload_bytes - cap - 1) // step if payload_bytes > cap else 1
    data_chunks = [step] * (fragment_count - 1)
    last = payload_bytes - (fragment_count - 1) * step
    data_chunks.append(last)
    if last <= 0 or last > cap:
        return FragmentationPlan(
            feasible=False,
            required=True,
            min_mtu=min_mtu,
            original_frame_size=frame,
            reason="internal fragmentation arithmetic produced an invalid last chunk",
        )
    fragment_sizes = tuple(header + chunk for chunk in data_chunks)
    if any(size > min_mtu for size in fragment_sizes):
        return FragmentationPlan(
            feasible=False,
            required=True,
            min_mtu=min_mtu,
            original_frame_size=frame,
            reason="a computed fragment exceeds the path MTU",
        )
    return FragmentationPlan(
        feasible=True,
        required=True,
        min_mtu=min_mtu,
        original_frame_size=frame,
        fragment_count=fragment_count,
        fragment_data_bytes=tuple(data_chunks),
        fragment_sizes=fragment_sizes,
    )


# ---------------------------------------------------------------------------
# Commitment checks
# ---------------------------------------------------------------------------


def check_commitment(path: PathSpec, payload: int) -> CommitmentReport:
    """Check one exact payload in one direction/path."""

    value = byte_count(payload, "payload")
    size_report = calculate_size_bounds(path)
    frame_size, overhead_error = _try_frame_size(path, value)

    if overhead_error is not None or frame_size is None:
        blocker = MissingInformation(  # Reuse a structured shape? Better dedicated dict-like.
            kind="encapsulation_rejected",
            hop=HopRef(-1, path.path_id),
            payload=value,
            required_mtu=None,
            bounds=size_report.bounds,
            message=str(overhead_error),
        )
        return CommitmentReport(
            path_id=path.path_id,
            direction=path.direction,
            payload=value,
            committed=False,
            status="encapsulation_rejected",
            dont_fragment=path.dont_fragment,
            frame_size=None,
            size_report=size_report,
            blocker=blocker,
        )

    violations: list[SizeViolation] = []
    missing: list[MissingInformation] = []
    first_missing: MissingInformation | None = None

    for index, hop in enumerate(path.hops):
        ref = _hop_ref(hop, index)
        lower = hop.mtu.lower
        if lower is not None:
            if frame_size > lower:
                violations.append(
                    SizeViolation(
                        kind="size_violation",
                        hop=ref,
                        payload=value,
                        frame_size=frame_size,
                        mtu=lower,
                        excess=frame_size - lower,
                    )
                )
        else:
            info = MissingInformation(
                kind="missing_information",
                hop=ref,
                payload=value,
                required_mtu=frame_size,
                bounds=size_report.bounds,
                missing_lower_mtu=True,
                missing_upper_mtu=hop.mtu.upper is None,
                message=(
                    f"{ref.hop_id} has no confirmed lower effective MTU; "
                    f"it must be at least {frame_size} bytes for payload {value}"
                ),
            )
            missing.append(info)
            if first_missing is None:
                first_missing = info

    min_known_mtu = min(
        (hop.mtu.lower for hop in path.hops if hop.mtu.lower is not None),
        default=None,
    )

    if violations:
        first_violation = violations[0]
        if path.dont_fragment:
            return CommitmentReport(
                path_id=path.path_id,
                direction=path.direction,
                payload=value,
                committed=False,
                status="oversize",
                dont_fragment=True,
                frame_size=frame_size,
                size_report=size_report,
                violations=tuple(violations),
                missing=tuple(missing),
                blocker=first_violation,
            )

        policy = path.fragmentation or FragmentationPolicy.ipv4()
        plan = plan_fragmentation(
            frame_size=frame_size,
            min_mtu=min_known_mtu,
            policy=policy,
        )
        if plan.feasible and first_missing is None:
            return CommitmentReport(
                path_id=path.path_id,
                direction=path.direction,
                payload=value,
                committed=True,
                status="fragmented",
                dont_fragment=False,
                frame_size=frame_size,
                size_report=size_report,
                violations=tuple(violations),
                missing=tuple(missing),
                fragmentation=plan,
                blocker=None,
            )
        if plan.feasible and first_missing is not None:
            return CommitmentReport(
                path_id=path.path_id,
                direction=path.direction,
                payload=value,
                committed=False,
                status="incomplete",
                dont_fragment=False,
                frame_size=frame_size,
                size_report=size_report,
                violations=tuple(violations),
                missing=tuple(missing),
                fragmentation=plan,
                blocker=first_missing,
            )
        return CommitmentReport(
            path_id=path.path_id,
            direction=path.direction,
            payload=value,
            committed=False,
            status="fragmentation_impossible",
            dont_fragment=False,
            frame_size=frame_size,
            size_report=size_report,
            violations=tuple(violations),
            missing=tuple(missing),
            fragmentation=plan,
            blocker=first_violation,
        )

    if first_missing is not None:
        return CommitmentReport(
            path_id=path.path_id,
            direction=path.direction,
            payload=value,
            committed=False,
            status="incomplete",
            dont_fragment=path.dont_fragment,
            frame_size=frame_size,
            size_report=size_report,
            missing=tuple(missing),
            blocker=first_missing,
        )

    return CommitmentReport(
        path_id=path.path_id,
        direction=path.direction,
        payload=value,
        committed=True,
        status="committed",
        dont_fragment=path.dont_fragment,
        frame_size=frame_size,
        size_report=size_report,
    )


def require_committed(path: PathSpec, payload: int) -> CommitmentReport:
    report = check_commitment(path, payload)
    return report.require()


# ---------------------------------------------------------------------------
# Multipath/time planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathSchedule:
    path_id: str
    direction: str | None
    report: CommitmentReport
    windows: tuple[Window, ...]

    @property
    def geometry_committed(self) -> bool:
        return self.report.committed

    @property
    def eligible_windows(self) -> tuple[Window, ...]:
        """Confirmed windows usable for scheduling the requested payload."""

        return self.windows if self.report.committed else ()

    @property
    def schedulable(self) -> bool:
        return self.report.committed and bool(self.windows)


@dataclass(frozen=True)
class CommonPayloadBound:
    """The largest payload bounded by every candidate path."""

    guaranteed_max: int | None
    optimistic_max: int | None
    limiting_path: str | None
    limiting_hop: HopRef | None
    reports: tuple[PathSizeReport, ...]

    @property
    def fully_known(self) -> bool:
        return self.guaranteed_max is not None


@dataclass
class MultipathPlan:
    payload: int
    direction: str | None
    paths: tuple[PathSchedule, ...]
    common_windows: tuple[Window, ...]
    eligible_common_windows: tuple[Window, ...]
    opportunities: tuple[AvailabilitySegment, ...]
    blockers: tuple[Any, ...]

    @property
    def all_paths_committed(self) -> bool:
        return all(item.report.committed for item in self.paths)

    def active_paths(self, moment: Any) -> tuple[str, ...]:
        for segment in self.opportunities:
            if segment.window.contains(moment):
                return segment.path_ids
        return ()

    def windows_between(self, path_a: str, path_b: str) -> tuple[Window, ...]:
        left = next((p.windows for p in self.paths if p.path_id == path_a), None)
        right = next((p.windows for p in self.paths if p.path_id == path_b), None)
        if left is None or right is None:
            raise GeometryError(f"unknown path id(s): {path_a!r}, {path_b!r}")
        left_report = next(p for p in self.paths if p.path_id == path_a)
        right_report = next(p for p in self.paths if p.path_id == path_b)
        if not left_report.geometry_committed or not right_report.geometry_committed:
            return ()
        return intersect_windows(left, right)

    def redundant_windows(self, min_paths: int = 2) -> tuple[AvailabilitySegment, ...]:
        return tuple(segment for segment in self.opportunities if segment.path_count >= min_paths)


def coerce_path(value: PathSpec | Mapping[str, Any]) -> PathSpec:
    if isinstance(value, PathSpec):
        return value
    if isinstance(value, Mapping):
        return PathSpec(**value)
    raise TypeError(f"cannot interpret {value!r} as PathSpec")


def common_payload_bounds(
    paths: Sequence[PathSpec | Mapping[str, Any]],
) -> CommonPayloadBound:
    """Return the common payload ceiling across candidate switch paths."""

    path_specs = tuple(coerce_path(path) for path in paths)
    if not path_specs:
        raise GeometryError("at least one path is required")
    reports = tuple(calculate_size_bounds(path) for path in path_specs)

    guaranteed_candidates = [
        (report.bounds.guaranteed_max, report.path_id, report.bottleneck)
        for report in reports
        if report.bounds.guaranteed_max is not None and report.bottleneck is not None
    ]
    if len(guaranteed_candidates) == len(reports):
        guaranteed_max, limiting_path, limiting_hop = min(
            guaranteed_candidates,
            key=lambda item: (item[0], item[1]),
        )
    else:
        guaranteed_max = None
        limiting_path = None
        limiting_hop = None

    optimistic_values = [
        report.bounds.optimistic_max
        for report in reports
        if report.bounds.optimistic_max is not None
    ]
    optimistic_max = min(optimistic_values) if optimistic_values else None

    return CommonPayloadBound(
        guaranteed_max=guaranteed_max,
        optimistic_max=optimistic_max,
        limiting_path=limiting_path,
        limiting_hop=limiting_hop,
        reports=reports,
    )


def maximum_common_payload(paths: Sequence[PathSpec | Mapping[str, Any]]) -> int | None:
    return common_payload_bounds(paths).guaranteed_max


def plan_multipath(
    payload: int,
    paths: Sequence[PathSpec | Mapping[str, Any]],
    *,
    direction: str | None = None,
) -> MultipathPlan:
    """Find confirmed periods where paths can carry the same payload."""

    value = byte_count(payload, "payload")
    path_specs = tuple(coerce_path(path) for path in paths)
    if not path_specs:
        raise GeometryError("at least one path is required")
    ids = [path.path_id for path in path_specs]
    if len(set(ids)) != len(ids):
        raise GeometryError(f"path_id values must be unique: {ids}")

    schedules: list[PathSchedule] = []
    blockers: list[Any] = []
    availability: dict[str, tuple[Window, ...]] = {}

    for path in path_specs:
        if direction is not None and path.direction is None:
            path.direction = direction
        report = check_commitment(path, value)
        windows = path.effective_windows()
        schedule = PathSchedule(path.path_id, path.direction, report, windows)
        schedules.append(schedule)
        if report.blocker is not None and not report.committed:
            blockers.append(report.blocker)
        if report.committed:
            availability[path.path_id] = windows

    all_committed = all(schedule.geometry_committed for schedule in schedules)
    common_all = (
        intersect_all_windows([schedule.windows for schedule in schedules])
        if all_committed
        else ()
    )
    eligible_collections = [
        schedule.eligible_windows
        for schedule in schedules
        if schedule.geometry_committed
    ]
    eligible_common = (
        intersect_all_windows(eligible_collections) if eligible_collections else ()
    )
    timeline = availability_timeline(availability)

    return MultipathPlan(
        payload=value,
        direction=direction,
        paths=tuple(schedules),
        common_windows=common_all,
        eligible_common_windows=eligible_common,
        opportunities=timeline,
        blockers=tuple(blockers),
    )


@dataclass(frozen=True)
class DirectionalSwitchPlan:
    payload: int
    forward: MultipathPlan
    reverse: MultipathPlan

    def direction(self, name: str) -> MultipathPlan:
        if name in ("forward", "up", "outbound"):
            return self.forward
        if name in ("reverse", "down", "inbound"):
            return self.reverse
        raise GeometryError(f"unknown direction {name!r}")


def plan_directions(
    payload: int,
    *,
    forward: Sequence[PathSpec | Mapping[str, Any]] = (),
    reverse: Sequence[PathSpec | Mapping[str, Any]] = (),
) -> DirectionalSwitchPlan:
    forward_paths = list(forward)
    reverse_paths = list(reverse)
    if not forward_paths or not reverse_paths:
        raise GeometryError("both forward and reverse path sets are required")
    return DirectionalSwitchPlan(
        payload=payload,
        forward=plan_multipath(payload, forward_paths, direction="forward"),
        reverse=plan_multipath(payload, reverse_paths, direction="reverse"),
    )


# Convenience aliases for callers preferring functional naming.
evaluate_path = check_commitment
assess_path = check_commitment
calculate_max_safe_payload = maximum_safe_payload
find_switch_windows = plan_multipath
