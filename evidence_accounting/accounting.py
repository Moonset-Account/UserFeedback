"""Forensic evidence accounting for temperature-controlled sample relay.

The module decides, per sample, which transport legs (segments) carry both
acceptable temperature-control evidence (a calibrated sensor attached to the
correct container) and an intact-seal record, taking the handover time
windows reported by the two transport parties into account.

Rules implemented
-----------------
1. After a container transfer a sensor that stayed on the *old* container can
   only testify for samples still carried by that old container.  Sensor
   evidence is therefore always scoped to the container it is attached to.
2. Where the two parties' handover windows do not jointly cover the claimed
   transfer instant, the ambiguous portion is *retained* (reported separately)
   rather than silently dropped or silently accepted.  Ambiguous instants are
   never part of a confirmed acceptable segment and block segment merging.
3. Adjacent acceptable intervals may be spliced together, but never across an
   ambiguous handover boundary.

Time is represented by comparable numbers on one common clock (e.g. epoch
seconds, fractional values allowed).  Every interval is half-open:
``[start, end)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .intervals import (
    Interval,
    earliest_gap,
    intersect_lists,
    merge,
    merge_with_blocked,
    subtract_many,
)

# --- Reason codes carried by GapDocument ----------------------------------

R_SENSOR_MISSING = "sensor_coverage_missing"
R_SENSOR_UNCALIBRATED = "sensor_uncalibrated_at_time"
R_SEAL_MISSING = "seal_record_missing"
R_HANDOVER_UNCONFIRMED = "handover_not_confirmed"
R_HANDOVER_AMBIGUOUS = "handover_window_ambiguous"


# --- Input records ---------------------------------------------------------


@dataclass(frozen=True)
class SampleJourney:
    """A sample's full transport journey.

    ``container_id`` is the container holding the sample before the first
    transfer.  ``transfers`` lists every subsequent relay (re-containerization)
    in chronological order.
    """

    sample_id: str
    container_id: str
    start: float
    end: float
    transfers: tuple["Transfer", ...] = ()

    def __post_init__(self) -> None:
        if not self.sample_id:
            raise ValueError("sample_id must be non-empty")
        if not self.container_id:
            raise ValueError("container_id must be non-empty")
        if not self.start < self.end:
            raise ValueError("journey start must be strictly before end")
        times = [transfer.time for transfer in self.transfers]
        if any(not self.start < time < self.end for time in times):
            raise ValueError("every transfer time must lie inside the journey")
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError("transfer times must be strictly increasing")


@dataclass(frozen=True)
class Transfer:
    """A relay event moving the sample from one container to another."""

    time: float
    from_container: str
    to_container: str
    transfer_doc_id: str

    def __post_init__(self) -> None:
        if not self.from_container or not self.to_container:
            raise ValueError("both containers must be non-empty")
        if self.from_container == self.to_container:
            raise ValueError("a transfer must change container")
        if not self.transfer_doc_id:
            raise ValueError("transfer_doc_id must be non-empty")


@dataclass(frozen=True)
class Sensor:
    """A temperature sensor attached to one container.

    ``coverage`` is the interval during which the sensor actually measured the
    container; ``calibrations`` are the intervals in which its calibration is
    valid.  The sensor can only testify while coverage and a valid calibration
    overlap, and only for the container it is attached to.
    """

    sensor_id: str
    container_id: str
    coverage: Interval
    calibrations: tuple[Interval, ...]

    def __post_init__(self) -> None:
        if not self.sensor_id or not self.container_id:
            raise ValueError("sensor_id and container_id must be non-empty")
        if not self.coverage[0] < self.coverage[1]:
            raise ValueError("sensor coverage must be a positive interval")
        for interval in self.calibrations:
            if not interval[0] < interval[1]:
                raise ValueError("calibration intervals must be positive")

    def certified_coverage(self) -> list[Interval]:
        """Intervals in which the sensor measured *and* was calibrated."""
        return intersect_lists([self.coverage], list(self.calibrations))


@dataclass(frozen=True)
class SealCheck:
    """A seal-verification record for one container over one interval."""

    seal_doc_id: str
    container_id: str
    verified_window: Interval

    def __post_init__(self) -> None:
        if not self.seal_doc_id or not self.container_id:
            raise ValueError("seal_doc_id and container_id must be non-empty")
        if not self.verified_window[0] < self.verified_window[1]:
            raise ValueError("verified_window must be a positive interval")


@dataclass(frozen=True)
class HandoverWindow:
    """One party's reported handover window for a transfer document."""

    transfer_doc_id: str
    party: str
    window: Interval

    def __post_init__(self) -> None:
        if not self.transfer_doc_id or not self.party:
            raise ValueError("transfer_doc_id and party must be non-empty")
        if not self.window[0] < self.window[1]:
            raise ValueError("handover window must be a positive interval")


# --- Output records --------------------------------------------------------


@dataclass(frozen=True)
class GapDocument:
    """A source document that explains the earliest uncovered position."""

    doc_id: str
    doc_type: str  # "sensor" | "seal" | "handover" | "transfer"
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class SampleAssessment:
    """The accounting result for one sample."""

    sample_id: str
    acceptable_segments: tuple[Interval, ...]
    earliest_uncovered: float | None
    gap_documents: tuple[GapDocument, ...]
    ambiguous_intervals: tuple[Interval, ...] = ()
    blocked_points: tuple[float, ...] = ()


# --- Public API ------------------------------------------------------------


def assess_all(
    journeys: Sequence[SampleJourney],
    sensors: Sequence[Sensor],
    seal_checks: Sequence[SealCheck],
    handover_windows: Sequence[HandoverWindow],
) -> dict[str, SampleAssessment]:
    """Assess every sample and return results keyed by ``sample_id``."""
    _validate_global(journeys, handover_windows)
    return {
        journey.sample_id: assess_sample(
            journey, sensors, seal_checks, handover_windows
        )
        for journey in journeys
    }


def assess_sample(
    journey: SampleJourney,
    sensors: Sequence[Sensor],
    seal_checks: Sequence[SealCheck],
    handover_windows: Sequence[HandoverWindow],
) -> SampleAssessment:
    """Assess a single sample against all supplied evidence."""

    sensors_by_container = _group_by_container(sensors, lambda s: s.container_id)
    seals_by_container = _group_by_container(
        seal_checks, lambda s: s.container_id
    )
    handovers_by_doc = _group_by(
        handover_windows, lambda h: h.transfer_doc_id
    )

    legs = _build_legs(journey)

    # Intervals that satisfy temperature + seal evidence inside each leg.
    qualified_by_leg: list[list[Interval]] = []
    for container_id, leg_span, _entry_transfer in legs:
        temperature = _temperature_evidence(
            sensors_by_container.get(container_id, ()), leg_span
        )
        seal = _seal_evidence(
            seals_by_container.get(container_id, ()), leg_span
        )
        qualified = intersect_lists(temperature, seal)
        qualified_by_leg.append(qualified)

    # Handover analysis at every transfer.
    ambiguous: list[Interval] = []
    blocked_points: list[float] = []
    # Each disputed band remembers the transfer that produced it.  Bands from
    # different transfers are kept separate until the final merge.
    raw_bands: list[tuple[Interval, Transfer]] = []
    for transfer in journey.transfers:
        windows = [
            h.window
            for h in handovers_by_doc.get(transfer.transfer_doc_id, ())
        ]
        if not windows:
            # No party reported the handover: the instant itself is disputed.
            blocked_points.append(transfer.time)
            continue
        common = _common_coverage(windows)
        # Confirmation requires BOTH parties' windows jointly covering the
        # instant.  A single reported window can never confirm a handover.
        confirmed = len(windows) >= 2 and any(
            interval[0] <= transfer.time < interval[1] for interval in common
        )
        if confirmed:
            continue
        # Unconfirmed transfer.  The disputed handover band is the full hull
        # of the reported windows: every instant between the earliest opening
        # and the latest closing lacks unanimous two-party confirmation,
        # including gaps that no party window covers at all.
        blocked_points.append(transfer.time)
        hull = (
            min(window[0] for window in windows),
            max(window[1] for window in windows),
        )
        clipped = _safe_intersect(hull, (journey.start, journey.end))
        if clipped is not None:
            ambiguous.append(clipped)
            raw_bands.append((clipped, transfer))

    ambiguous = merge(ambiguous)
    # Map every merged band back to a transfer.  Overlapping bands can only
    # come from consecutive disputed transfers; keep the earliest transfer.
    band_to_transfer: dict[Interval, Transfer] = {}
    for band in ambiguous:
        owners = [
            transfer
            for raw_band, transfer in raw_bands
            if raw_band[0] < band[1] and band[0] < raw_band[1]
        ]
        owners.sort(key=lambda transfer: transfer.time)
        band_to_transfer[band] = owners[0]

    # Confirmed evidence: every qualified interval with all disputed handover
    # bands removed.  Ambiguity is applied globally because a disputed band
    # can extend into a neighbouring leg (e.g. windows reported after the
    # claimed transfer instant fall inside the new container's leg).
    confirmed_intervals: list[Interval] = []
    for qualified in qualified_by_leg:
        remaining: list[Interval] = list(qualified)
        for ambiguity in ambiguous:
            remaining = [
                part
                for interval in remaining
                for part in subtract_many(interval, [ambiguity])
            ]
        confirmed_intervals.extend(remaining)

    acceptable = merge_with_blocked(confirmed_intervals, blocked_points)
    span: Interval = (journey.start, journey.end)
    earliest = earliest_gap(span, acceptable, blocked_points)

    gap_docs: list[GapDocument] = []
    if earliest is not None:
        gap_docs = _explain_gap(
            earliest,
            legs,
            sensors_by_container,
            seals_by_container,
            handovers_by_doc,
            blocked_points,
            ambiguous,
            band_to_transfer,
        )

    return SampleAssessment(
        sample_id=journey.sample_id,
        acceptable_segments=tuple(acceptable),
        earliest_uncovered=earliest,
        gap_documents=tuple(gap_docs),
        ambiguous_intervals=tuple(ambiguous),
        blocked_points=tuple(sorted(set(blocked_points))),
    )


# --- Internal helpers ------------------------------------------------------


def _validate_global(
    journeys: Sequence[SampleJourney],
    handover_windows: Sequence[HandoverWindow],
) -> None:
    seen_ids: set[str] = set()
    transfer_doc_ids: set[str] = set()
    for journey in journeys:
        if journey.sample_id in seen_ids:
            raise ValueError(f"duplicate sample_id: {journey.sample_id}")
        seen_ids.add(journey.sample_id)
        transfer_doc_ids.update(
            transfer.transfer_doc_id for transfer in journey.transfers
        )

    seen_pairs: set[tuple[str, str]] = set()
    for handover in handover_windows:
        if handover.transfer_doc_id not in transfer_doc_ids:
            raise ValueError(
                "handover window references unknown transfer document: "
                f"{handover.transfer_doc_id}"
            )
        pair = (handover.transfer_doc_id, handover.party)
        if pair in seen_pairs:
            raise ValueError(
                f"duplicate handover window for {pair[0]} / party {pair[1]}"
            )
        seen_pairs.add(pair)

    counts: dict[str, int] = {}
    for handover in handover_windows:
        counts[handover.transfer_doc_id] = (
            counts.get(handover.transfer_doc_id, 0) + 1
        )
    for doc_id, count in counts.items():
        if count > 2:
            raise ValueError(
                f"transfer {doc_id} has {count} handover windows; "
                "at most two parties are expected"
            )


def _build_legs(journey: SampleJourney) -> list[tuple[str, Interval, Transfer | None]]:
    """Split a journey into ``(container_id, span, entry_transfer)`` legs."""
    legs: list[tuple[str, Interval, Transfer | None]] = []
    cursor = journey.start
    current_container = journey.container_id
    entry_transfer: Transfer | None = None
    for transfer in journey.transfers:
        if transfer.from_container != current_container:
            raise ValueError(
                f"transfer chain mismatch at t={transfer.time}: sample is in "
                f"{current_container}, transfer expects {transfer.from_container}"
            )
        legs.append(
            (current_container, (cursor, transfer.time), entry_transfer)
        )
        cursor = transfer.time
        current_container = transfer.to_container
        entry_transfer = transfer
    legs.append((current_container, (cursor, journey.end), entry_transfer))
    return legs


def _temperature_evidence(
    container_sensors: Iterable[Sensor], leg_span: Interval
) -> list[Interval]:
    """Calibrated sensor coverage intersecting the leg, scoped to container."""
    evidence: list[Interval] = []
    for sensor in container_sensors:
        evidence.extend(
            intersect_lists(sensor.certified_coverage(), [leg_span])
        )
    return merge(evidence)


def _seal_evidence(
    container_seals: Iterable[SealCheck], leg_span: Interval
) -> list[Interval]:
    evidence = [
        overlap
        for seal in container_seals
        for overlap in [_safe_intersect(seal.verified_window, leg_span)]
        if overlap is not None
    ]
    return merge(evidence)


def _common_coverage(windows: Sequence[Interval]) -> list[Interval]:
    """Intersection of all reported party windows (the jointly covered part)."""
    if not windows:
        return []
    common: list[Interval] = [windows[0]]
    for window in windows[1:]:
        common = [
            overlap
            for interval in common
            for overlap in [_safe_intersect(interval, window)]
            if overlap is not None
        ]
        if not common:
            return []
    return common


def _safe_intersect(
    a: Interval, b: Interval
) -> Interval | None:
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return (lo, hi) if lo < hi else None


def _explain_gap(
    earliest: float,
    legs: list[tuple[str, Interval, Transfer | None]],
    sensors_by_container: dict[str, list[Sensor]],
    seals_by_container: dict[str, list[SealCheck]],
    handovers_by_doc: dict[str, list[HandoverWindow]],
    blocked_points: Sequence[float],
    ambiguous: Sequence[Interval],
    band_to_transfer: dict[Interval, Transfer],
) -> list[GapDocument]:
    docs: list[GapDocument] = []
    seen: set[str] = set()

    def add(doc_id: str, doc_type: str, reason: str, detail: str = "") -> None:
        if doc_id in seen:
            return
        seen.add(doc_id)
        docs.append(GapDocument(doc_id, doc_type, reason, detail))

    def add_handover_docs(transfer: Transfer, reason: str, detail: str) -> None:
        add(transfer.transfer_doc_id, "transfer", reason, detail)
        for handover in handovers_by_doc.get(transfer.transfer_doc_id, ()):
            add(
                f"handover:{transfer.transfer_doc_id}:{handover.party}",
                "handover",
                R_HANDOVER_AMBIGUOUS,
                f"party {handover.party} window {handover.window} "
                "does not jointly confirm the handover",
            )

    # A disputed transfer instant explains itself first.
    if earliest in blocked_points:
        _container_id, _span, entry_transfer = _leg_at(legs, earliest)
        if entry_transfer is not None and entry_transfer.time == earliest:
            add_handover_docs(
                entry_transfer,
                R_HANDOVER_UNCONFIRMED,
                f"transfer at t={earliest} from "
                f"{entry_transfer.from_container} to "
                f"{entry_transfer.to_container}",
            )
            return docs

    # An earliest gap landing inside a disputed handover band is explained by
    # the transfer and the party windows that produced the band.
    band = next(
        (
            interval
            for interval in ambiguous
            if interval[0] <= earliest < interval[1]
        ),
        None,
    )
    if band is not None and band in band_to_transfer:
        transfer = band_to_transfer[band]
        add_handover_docs(
            transfer,
            R_HANDOVER_AMBIGUOUS,
            f"t={earliest} lies inside disputed handover band {band} "
            f"for transfer {transfer.transfer_doc_id}",
        )
        return docs

    container_id, _span, _entry = _leg_at(legs, earliest)

    # Temperature evidence explanation.
    sensor_docs = sensors_by_container.get(container_id, ())
    if not sensor_docs:
        add(
            f"container:{container_id}:sensor",
            "sensor",
            R_SENSOR_MISSING,
            f"no sensor attached to container {container_id}",
        )
    else:
        for sensor in sensor_docs:
            if not (sensor.coverage[0] <= earliest < sensor.coverage[1]):
                add(
                    sensor.sensor_id,
                    "sensor",
                    R_SENSOR_MISSING,
                    f"sensor coverage {sensor.coverage} does not include "
                    f"t={earliest}",
                )
            elif not any(
                interval[0] <= earliest < interval[1]
                for interval in sensor.calibrations
            ):
                add(
                    sensor.sensor_id,
                    "sensor",
                    R_SENSOR_UNCALIBRATED,
                    f"sensor {sensor.sensor_id} has no valid calibration at "
                    f"t={earliest}",
                )

    # Seal evidence explanation.
    seal_docs = seals_by_container.get(container_id, ())
    if not seal_docs:
        add(
            f"container:{container_id}:seal",
            "seal",
            R_SEAL_MISSING,
            f"no seal verification record for container {container_id}",
        )
    else:
        for seal in seal_docs:
            if not (
                seal.verified_window[0]
                <= earliest
                < seal.verified_window[1]
            ):
                add(
                    seal.seal_doc_id,
                    "seal",
                    R_SEAL_MISSING,
                    f"seal window {seal.verified_window} does not include "
                    f"t={earliest}",
                )

    return docs


def _leg_at(
    legs: list[tuple[str, Interval, Transfer | None]], point: float
) -> tuple[str, Interval, Transfer | None]:
    for container_id, span, entry_transfer in legs:
        if span[0] <= point < span[1]:
            return container_id, span, entry_transfer
    # A point equal to the journey end belongs to the final leg.
    last_container, last_span, last_entry = legs[-1]
    if point == last_span[1]:
        return last_container, last_span, last_entry
    raise ValueError(f"point {point} is outside the journey")


def _group_by(items: Iterable, key) -> dict:  # type: ignore[no-untyped-def]
    grouped: dict = {}
    for item in items:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def _group_by_container(items: Iterable, key) -> dict:  # type: ignore[no-untyped-def]
    return _group_by(items, key)
