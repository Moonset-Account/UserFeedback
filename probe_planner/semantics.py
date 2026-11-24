"""Deterministic observation semantics for single-fault scenarios."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Hashable

from .model import FAILED, MISSING, OK, Fault, Probe


def probe_outcome(probe: Probe, fault: Fault) -> str:
    """Return one probe's receipt status under one fault scenario.

    A lost patrol site has precedence: none of its requests return a receipt,
    including requests whose path also touches a failed dependency.  Otherwise
    a failed path/target resource gives a failure receipt, and a clean path
    gives a success receipt.
    """

    if probe.site in fault.unavailable_sites:
        return MISSING
    if probe.path_resources & fault.failed_resources:
        return FAILED
    return OK


def outcome_matrix(
    probes: Sequence[Probe], faults: Sequence[Fault]
) -> list[list[str]]:
    """Return ``matrix[fault_index][probe_index] = status``."""

    return [
        [probe_outcome(probe, fault) for probe in probes]
        for fault in faults
    ]


def signatures_for_probes(
    probes: Sequence[Probe], faults: Sequence[Fault]
) -> dict[Hashable, tuple[str, ...]]:
    """Map each fault id to its ordered observation vector."""

    matrix = outcome_matrix(probes, faults)
    return {
        fault.id: tuple(matrix[fi])
        for fi, fault in enumerate(faults)
    }


def pair_index_map(fault_count: int) -> list[tuple[int, int]]:
    """Unordered fault-index pairs in stable input order."""

    return [
        (left, right)
        for left in range(fault_count)
        for right in range(left + 1, fault_count)
    ]


def probe_distinction_mask(
    probes: Sequence[Probe], faults: Sequence[Fault]
) -> tuple[list[int], list[tuple[int, int]], list[list[str]]]:
    """Build, for every probe, a bit mask of fault pairs it distinguishes.

    Bit ``k`` corresponds to the pair returned at index ``k`` by
    :func:`pair_index_map`.  A pair is distinguished by a selected probe set
    exactly when the OR of selected masks contains that bit.
    """

    pairs = pair_index_map(len(faults))
    matrix = outcome_matrix(probes, faults)
    masks: list[int] = []

    for probe_index in range(len(probes)):
        mask = 0
        for pair_index, (left, right) in enumerate(pairs):
            if matrix[left][probe_index] != matrix[right][probe_index]:
                mask |= 1 << pair_index
        masks.append(mask)

    return masks, pairs, matrix
