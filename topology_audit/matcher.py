"""Deterministic backtracking matcher for labeled hardware topologies."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Callable, Mapping

from .graph import (
    build_adjacency,
    devices_compatible,
    interface_matches,
    kind_matches,
    managed_logical_edge,
    segment_end_interface,
)
from .model import (
    BridgeConstraint,
    Device,
    END_FIRST,
    PortRef,
    Topology,
)
from .report import Match

TypePredicate = Callable[[str, str], bool]


@dataclass(frozen=True)
class MatcherConfig:
    exclusive_locations: frozenset[str] = frozenset()
    type_compatible: TypePredicate | None = None
    solution_limit: int | None = None


def orientation_space(
    logical: Topology,
    bridges: tuple[BridgeConstraint, ...],
) -> list[tuple[tuple[str, bool], ...]]:
    bridge_by_segment: dict[str, list[BridgeConstraint]] = {}
    for bridge in bridges:
        bridge_by_segment.setdefault(bridge.segment_id, []).append(bridge)

    choices: list[tuple[str, tuple[bool, ...]]] = []
    for segment in sorted(logical.segments, key=lambda s: s.id):
        allowed = [False]
        reversible = segment.reversible and all(
            b.allow_reverse for b in bridge_by_segment.get(segment.id, ())
        )
        if reversible:
            allowed.append(True)
        choices.append((segment.id, tuple(allowed)))

    if not choices:
        return [()]

    ids = [item[0] for item in choices]
    values = [item[1] for item in choices]
    result: list[tuple[tuple[str, bool], ...]] = []
    for combo in product(*values):
        result.append(tuple(sorted(zip(ids, combo), key=lambda x: x[0])))
    return result


def _reversed_for_device(
    device_id: str,
    segment_owner: Mapping[str, str],
    orientation: Mapping[str, bool],
) -> bool:
    segment_id = segment_owner.get(device_id)
    if segment_id is None:
        return False
    return bool(orientation.get(segment_id, False))


def _anchor_allows(
    logical_id: str,
    observed_id: str,
    hard_mapping: Mapping[str, str],
    anchor_locations: Mapping[str, str | None],
    observed: Topology,
) -> bool:
    forced = hard_mapping.get(logical_id)
    if forced is not None and forced != observed_id:
        return False
    expected_location = anchor_locations.get(logical_id)
    if expected_location is not None:
        actual = observed.device_map()[observed_id].location
        if actual != expected_location:
            return False
    return True


def _location_ok(
    observed_device: Device,
    used_exclusive_locations: Mapping[str, str],
    exclusive_locations: frozenset[str],
) -> bool:
    location = observed_device.location
    if location is None or location not in exclusive_locations:
        return True
    return location not in used_exclusive_locations


def _coarse_edges_ok(
    logical: Topology,
    observed: Topology,
    assignment: Mapping[str, str],
    ignored_edges: frozenset[int],
) -> bool:
    for edge_idx, conn in enumerate(logical.connections):
        if edge_idx in ignored_edges:
            continue
        ld1 = conn.a.device
        ld2 = conn.b.device
        if ld1 not in assignment or ld2 not in assignment:
            continue
        od1 = assignment[ld1]
        od2 = assignment[ld2]
        found = False
        for oconn in observed.connections:
            if not kind_matches(conn.kind, oconn.kind):
                continue
            devices = {oconn.a.device, oconn.b.device}
            if devices == {od1, od2}:
                found = True
                break
        if not found:
            return False
    return True


def _segment_alignment_ok(
    logical: Topology,
    observed: Topology,
    assignment: Mapping[str, str],
    orientation: Mapping[str, bool],
) -> bool:
    if not observed.segments:
        return True
    observed_sequences = [seg.devices for seg in observed.segments]
    for segment in logical.segments:
        mapped = tuple(assignment[d] for d in segment.devices)
        reversed_flag = orientation.get(segment.id, False)
        if reversed_flag:
            if not any(tuple(reversed(mapped)) == seq for seq in observed_sequences):
                return False
        else:
            if not any(mapped == seq for seq in observed_sequences):
                return False
    return True


def _candidate_port_map(
    logical: Topology,
    observed: Topology,
    assignment: Mapping[str, str],
    orientation: Mapping[str, bool],
    segment_owner: Mapping[str, str],
    ignored_logical_edges: frozenset[int],
    ignored_observed_edges: frozenset[int],
) -> dict[str, str] | None:
    ldevs = logical.device_map()
    odevs = observed.device_map()

    logical_ports: list[PortRef] = []
    candidates: dict[PortRef, list[PortRef]] = {}
    for ldev in logical.devices:
        odev = odevs[assignment[ldev.id]]
        rev = _reversed_for_device(ldev.id, segment_owner, orientation)
        for lif in ldev.interfaces:
            lp = PortRef(ldev.id, lif.name)
            logical_ports.append(lp)
            row: list[PortRef] = []
            for oif in odev.interfaces:
                if interface_matches(lif, oif, reversed_segment=rev):
                    row.append(PortRef(odev.id, oif.name))
            row.sort(key=lambda p: (p.device, p.interface))
            if not row:
                return None
            candidates[lp] = row

    filtered_l_edges = [
        (idx, conn)
        for idx, conn in enumerate(logical.connections)
        if idx not in ignored_logical_edges
    ]
    filtered_o_edges = [
        (idx, conn)
        for idx, conn in enumerate(observed.connections)
        if idx not in ignored_observed_edges
    ]
    if len(filtered_l_edges) != len(filtered_o_edges):
        return None

    observed_edge_between: dict[tuple[PortRef, PortRef, str], int] = {}
    for idx, conn in filtered_o_edges:
        key1 = (conn.a, conn.b, conn.kind)
        key2 = (conn.b, conn.a, conn.kind)
        # Parallel identical edges are retained by index in a list below.
        observed_edge_between.setdefault(key1, idx)
        observed_edge_between.setdefault(key2, idx)

    adjacency = build_adjacency(logical)
    port_map: dict[PortRef, PortRef] = {}
    used_ports: set[PortRef] = set()
    used_edges: set[int] = set()

    def available(lp: PortRef) -> list[PortRef]:
        result = []
        for op in candidates[lp]:
            if op in used_ports:
                continue
            ok = True
            for edge_idx, neighbor, kind in adjacency.get(lp, ()):
                if edge_idx in ignored_logical_edges:
                    continue
                if neighbor not in port_map:
                    continue
                np = port_map[neighbor]
                oidx = observed_edge_between.get((op, np, kind))
                if oidx is None or oidx in used_edges:
                    # Try wildcard-kind lookup if exact kind key missed.
                    oidx = _find_observed_edge(
                        observed, op, np, kind, ignored_observed_edges, used_edges
                    )
                    if oidx is None:
                        ok = False
                        break
                if oidx in used_edges:
                    ok = False
                    break
            if ok:
                result.append(op)
        return result

    def recurse() -> bool:
        if len(port_map) == len(logical_ports):
            return len(used_edges) == len(filtered_l_edges)
        unassigned = [p for p in logical_ports if p not in port_map]
        lp = min(unassigned, key=lambda p: (len(available(p)), p.device, p.interface))
        for op in available(lp):
            newly_used_edges: list[int] = []
            ok = True
            for edge_idx, neighbor, kind in adjacency.get(lp, ()):
                if edge_idx in ignored_logical_edges or neighbor not in port_map:
                    continue
                np = port_map[neighbor]
                oidx = _find_observed_edge(
                    observed, op, np, kind, ignored_observed_edges, used_edges
                )
                if oidx is None or oidx in used_edges:
                    ok = False
                    break
                used_edges.add(oidx)
                newly_used_edges.append(oidx)
            if not ok:
                for eidx in newly_used_edges:
                    used_edges.discard(eidx)
                continue

            port_map[lp] = op
            used_ports.add(op)
            if recurse():
                return True
            used_ports.remove(op)
            del port_map[lp]
            for eidx in newly_used_edges:
                used_edges.discard(eidx)
        return False

    if not recurse():
        return None
    return {f"{lp.device}:{lp.interface}": f"{op.device}:{op.interface}" for lp, op in sorted(port_map.items(), key=lambda x: str(x[0]))}


def _find_observed_edge(
    observed: Topology,
    a: PortRef,
    b: PortRef,
    kind: str,
    ignored: frozenset[int],
    used: set[int],
) -> int | None:
    for idx, conn in enumerate(observed.connections):
        if idx in ignored or idx in used:
            continue
        if not kind_matches(kind, conn.kind):
            continue
        if (conn.a == a and conn.b == b) or (conn.a == b and conn.b == a):
            return idx
    return None


@dataclass(frozen=True)
class BridgeResolution:
    observed_edge_indices: tuple[int, ...]
    details: tuple[tuple[str, str, str], ...]


def _resolve_bridges(
    logical: Topology,
    observed: Topology,
    bridges: tuple[BridgeConstraint, ...],
    assignment: Mapping[str, str],
    orientation: Mapping[str, bool],
) -> list[BridgeResolution] | None:
    """Return all deterministic bridge attachment choices that are valid."""

    choices: list[list[tuple[int, str, str]]] = []
    for bridge in sorted(bridges, key=lambda b: b.id):
        segment = logical.segment_map()[bridge.segment_id]
        reversed_flag = orientation.get(segment.id, False)
        selected_end = END_FIRST if reversed_flag else bridge.end
        if reversed_flag:
            selected_end = "last" if bridge.end == END_FIRST else END_FIRST
        lif = segment_end_interface(logical, segment, selected_end, bridge=bridge)
        lport = PortRef(segment.end_device(selected_end), lif.name)
        odev_id = assignment[lport.device]
        odev = observed.device_map()[odev_id]

        valid: list[tuple[int, str, str]] = []
        for eidx, conn in enumerate(observed.connections):
            if bridge.bridge_port not in (conn.a, conn.b):
                continue
            other = conn.other(bridge.bridge_port)
            if other.device != odev_id:
                continue
            if not kind_matches(bridge.kind, conn.kind):
                continue
            try:
                oif = odev.interface(other.interface)
            except Exception:
                continue
            if interface_matches(lif, oif, reversed_segment=reversed_flag):
                valid.append((eidx, str(lport), str(other)))
        valid.sort(key=lambda x: (x[0], x[1], x[2]))
        if not valid:
            return None
        choices.append(valid)

    if not choices:
        return [BridgeResolution((), ())]

    resolutions: list[BridgeResolution] = []
    for combo in product(*choices):
        edge_ids = tuple(item[0] for item in combo)
        # One observed edge cannot satisfy two distinct bridge attachments.
        if len(set(edge_ids)) != len(edge_ids):
            continue
        details = tuple((bridge_id, lp, op) for bridge_id, (_e, lp, op) in zip(sorted(b.id for b in bridges), combo))
        resolutions.append(
            BridgeResolution(
                observed_edge_indices=tuple(sorted(edge_ids)),
                details=tuple(sorted(details)),
            )
        )
    resolutions.sort(key=lambda r: (r.observed_edge_indices, r.details))
    return resolutions or None


def enumerate_matches(
    logical: Topology,
    observed: Topology,
    *,
    bridges: tuple[BridgeConstraint, ...] = (),
    hard_mapping: Mapping[str, str] | None = None,
    anchor_locations: Mapping[str, str | None] | None = None,
    config: MatcherConfig | None = None,
) -> tuple[list[Match], bool]:
    config = config or MatcherConfig()
    hard_mapping = dict(hard_mapping or {})
    anchor_locations = dict(anchor_locations or {})

    # Stable devices and bridge infrastructure are identity anchors.  If the
    # same stable id is absent on the observed side, the validity check below
    # turns this into zero matches rather than allowing a silent remap.
    for device in logical.devices:
        if device.stable:
            hard_mapping[device.id] = device.id
    for bridge in bridges:
        bp_device = bridge.bridge_port.device
        hard_mapping.setdefault(bp_device, bp_device)

    if len(logical.devices) != len(observed.devices):
        return [], False
    if set(hard_mapping) - set(logical.device_ids()):
        return [], False
    if any(v not in observed.device_map() for v in hard_mapping.values()):
        return [], False

    segment_owner: dict[str, str] = {}
    for segment in logical.segments:
        for device_id in segment.devices:
            segment_owner[device_id] = segment.id

    ignored_logical_edges: set[int] = set()
    for bridge in bridges:
        idx = managed_logical_edge(logical, bridge)
        if idx is not None:
            ignored_logical_edges.add(idx)

    ldevs = logical.device_map()
    odevs = observed.device_map()
    all_logical_ids = sorted(ldevs)
    all_observed_ids = sorted(odevs)

    matches: dict[tuple[tuple[str, str], ...], Match] = {}
    truncated = False

    for orientation_tuple in orientation_space(logical, bridges):
        orientation = dict(orientation_tuple)

        candidates: dict[str, list[str]] = {}
        for lid in all_logical_ids:
            ldev = ldevs[lid]
            rev = _reversed_for_device(lid, segment_owner, orientation)
            row = []
            for oid in all_observed_ids:
                odev = odevs[oid]
                if not _anchor_allows(lid, oid, hard_mapping, anchor_locations, observed):
                    continue
                if not devices_compatible(
                    ldev,
                    odev,
                    reversed_segment=rev,
                    type_compatible=config.type_compatible,
                ):
                    continue
                row.append(oid)
            row.sort()
            if not row:
                candidates = {}
                break
            candidates[lid] = row
        if not candidates:
            continue

        assignment: dict[str, str] = {}
        used: set[str] = set()
        used_locations: dict[str, str] = {}

        def dfs() -> None:
            nonlocal truncated
            if config.solution_limit is not None and len(matches) >= config.solution_limit:
                truncated = True
                return
            if len(assignment) == len(all_logical_ids):
                if not _segment_alignment_ok(logical, observed, assignment, orientation):
                    return
                if not _coarse_edges_ok(
                    logical, observed, assignment, frozenset(ignored_logical_edges)
                ):
                    return
                resolutions = _resolve_bridges(
                    logical, observed, bridges, assignment, orientation
                )
                if resolutions is None:
                    return
                for resolution in resolutions:
                    port_map = _candidate_port_map(
                        logical,
                        observed,
                        assignment,
                        orientation,
                        segment_owner,
                        frozenset(ignored_logical_edges),
                        frozenset(resolution.observed_edge_indices),
                    )
                    if port_map is None:
                        continue
                    mapping_key = tuple(sorted(assignment.items()))
                    match = Match(
                        mapping=mapping_key,
                        orientation=orientation_tuple,
                        port_mapping=tuple(sorted(port_map.items())),
                        bridge_details=resolution.details,
                    )
                    if mapping_key not in matches:
                        matches[mapping_key] = match
                    return
                return

            remaining = [x for x in all_logical_ids if x not in assignment]

            def domain_size(lid: str) -> int:
                return len(_current_domain(lid))

            def _current_domain(lid: str) -> list[str]:
                result = []
                for oid in candidates[lid]:
                    if oid in used:
                        continue
                    odev = odevs[oid]
                    if not _location_ok(
                        odev, used_locations, config.exclusive_locations
                    ):
                        continue
                    result.append(oid)
                return result

            lid = min(remaining, key=lambda x: (domain_size(x), x))
            for oid in _current_domain(lid):
                odev = odevs[oid]
                location = odev.location
                added_location = False
                if (
                    location is not None
                    and location in config.exclusive_locations
                    and location not in used_locations
                ):
                    used_locations[location] = oid
                    added_location = True
                assignment[lid] = oid
                used.add(oid)
                if _coarse_edges_ok(
                    logical, observed, assignment, frozenset(ignored_logical_edges)
                ):
                    dfs()
                used.remove(oid)
                del assignment[lid]
                if added_location:
                    del used_locations[location]
                if config.solution_limit is not None and len(matches) >= config.solution_limit:
                    truncated = True
                    return

        dfs()

    ordered = [matches[key] for key in sorted(matches)]
    return ordered, truncated
