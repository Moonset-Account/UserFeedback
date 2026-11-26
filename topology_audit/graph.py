"""Graph and interface compatibility helpers."""

from __future__ import annotations

from typing import Iterable, Mapping

from .model import (
    Anchor,
    BridgeConstraint,
    Device,
    END_FIRST,
    END_LAST,
    Interface,
    PortRef,
    ROLE_DOWNSTREAM,
    ROLE_UPSTREAM,
    Segment,
    Topology,
    TopologySpecError,
)


def kind_matches(a: str, b: str) -> bool:
    return a == b or a == "any" or b == "any"


def effective_role(role: str | None, reversed_segment: bool) -> str | None:
    if not reversed_segment or role is None:
        return role
    if role == ROLE_UPSTREAM:
        return ROLE_DOWNSTREAM
    if role == ROLE_DOWNSTREAM:
        return ROLE_UPSTREAM
    return role


def interface_matches(
    logical: Interface,
    observed: Interface,
    *,
    reversed_segment: bool = False,
) -> bool:
    if not kind_matches(logical.kind, observed.kind):
        return False
    expected_role = effective_role(logical.role, reversed_segment)
    # An unspecified role is a wildcard; a specified role must agree.
    if expected_role is not None and observed.role is not None:
        return expected_role == observed.role
    return True


def _perfect_interface_match(
    logical: tuple[Interface, ...],
    observed: tuple[Interface, ...],
    *,
    reversed_segment: bool,
) -> bool:
    if len(logical) != len(observed):
        return False
    if not logical:
        return True
    candidates: list[list[int]] = []
    for lif in logical:
        row = [
            j
            for j, oif in enumerate(observed)
            if interface_matches(lif, oif, reversed_segment=reversed_segment)
        ]
        if not row:
            return False
        candidates.append(row)

    used: set[int] = set()

    def assign(i: int) -> bool:
        if i == len(candidates):
            return True
        for j in candidates[i]:
            if j in used:
                continue
            used.add(j)
            if assign(i + 1):
                return True
            used.remove(j)
        return False

    return assign(0)


def devices_compatible(
    logical: Device,
    observed: Device,
    *,
    reversed_segment: bool = False,
    type_compatible=None,
) -> bool:
    if type_compatible is not None:
        if not type_compatible(logical.device_type, observed.device_type):
            return False
    elif logical.device_type != observed.device_type:
        return False
    return _perfect_interface_match(
        logical.interfaces,
        observed.interfaces,
        reversed_segment=reversed_segment,
    )


def build_adjacency(topology: Topology) -> dict[PortRef, list[tuple[int, PortRef, str]]]:
    adj: dict[PortRef, list[tuple[int, PortRef, str]]] = {}
    for device in topology.devices:
        for interface in device.interfaces:
            adj.setdefault(PortRef(device.id, interface.name), [])
    for edge_idx, conn in enumerate(topology.connections):
        adj.setdefault(conn.a, []).append((edge_idx, conn.b, conn.kind))
        adj.setdefault(conn.b, []).append((edge_idx, conn.a, conn.kind))
    return adj


def edge_between(
    topology: Topology,
    port_a: PortRef,
    port_b: PortRef,
    *,
    kind: str = "any",
) -> int | None:
    for idx, conn in enumerate(topology.connections):
        if not kind_matches(kind, conn.kind):
            continue
        if (conn.a == port_a and conn.b == port_b) or (
            conn.a == port_b and conn.b == port_a
        ):
            return idx
    return None


def edges_between_devices(
    topology: Topology,
    device_a: str,
    device_b: str,
    *,
    kind: str = "any",
) -> list[int]:
    result: list[int] = []
    for idx, conn in enumerate(topology.connections):
        if not kind_matches(kind, conn.kind):
            continue
        devices = {conn.a.device, conn.b.device}
        if devices == {device_a, device_b}:
            result.append(idx)
    return result


def segment_end_interface(
    topology: Topology,
    segment: Segment,
    end: str,
    *,
    bridge: BridgeConstraint | None = None,
) -> Interface:
    device_id = segment.end_device(end)
    device = topology.device_map()[device_id]

    explicit: str | None = None
    if bridge is not None and bridge.end == end and bridge.interface:
        explicit = bridge.interface
    elif end == END_FIRST:
        explicit = segment.first_interface
    elif end == END_LAST:
        explicit = segment.last_interface

    if explicit is not None:
        return device.interface(explicit)

    wanted_role = ROLE_UPSTREAM if end == END_FIRST else ROLE_DOWNSTREAM
    for interface in device.interfaces:
        if interface.role == wanted_role:
            return interface

    # Fall back to an outer port: a port not used to connect to the adjacent
    # device inside this segment.
    neighbor: str | None = None
    if len(segment.devices) > 1:
        if end == END_FIRST:
            neighbor = segment.devices[1]
        else:
            neighbor = segment.devices[-2]
    internal_ports: set[str] = set()
    if neighbor is not None:
        for conn in topology.connections:
            if {conn.a.device, conn.b.device} != {device_id, neighbor}:
                continue
            if conn.a.device == device_id:
                internal_ports.add(conn.a.interface)
            if conn.b.device == device_id:
                internal_ports.add(conn.b.interface)
    outer = [i for i in device.interfaces if i.name not in internal_ports]
    if len(outer) == 1:
        return outer[0]
    if device.interfaces:
        # Deterministic fallback. Explicit interfaces/roles are preferable.
        return sorted(device.interfaces, key=lambda i: i.name)[0]
    raise TopologySpecError(f"device {device_id!r} has no interfaces for segment end {end!r}")


def segment_end_port(
    topology: Topology,
    segment: Segment,
    end: str,
    *,
    bridge: BridgeConstraint | None = None,
) -> PortRef:
    interface = segment_end_interface(topology, segment, end, bridge=bridge)
    return PortRef(segment.end_device(end), interface.name)


def anchored_logical_port(
    topology: Topology,
    bridge: BridgeConstraint,
    segment: Segment,
) -> PortRef:
    return segment_end_port(topology, segment, bridge.end, bridge=bridge)


def managed_logical_edge(topology: Topology, bridge: BridgeConstraint) -> int | None:
    segment = topology.segment_map()[bridge.segment_id]
    anchored = anchored_logical_port(topology, bridge, segment)
    return edge_between(topology, bridge.bridge_port, anchored, kind=bridge.kind)


def validate_topology(topology: Topology, *, name: str = "topology") -> None:
    devices = topology.device_map()
    if len(devices) != len(topology.devices):
        raise TopologySpecError(f"{name}: device ids must be unique")

    for port_owner in topology.devices:
        names = [i.name for i in port_owner.interfaces]
        if len(names) != len(set(names)):
            raise TopologySpecError(f"{name}: duplicate interface names on {port_owner.id!r}")

    for idx, conn in enumerate(topology.connections):
        for port in (conn.a, conn.b):
            device = devices.get(port.device)
            if device is None:
                raise TopologySpecError(
                    f"{name} connection #{idx}: unknown device {port.device!r}"
                )
            try:
                device.interface(port.interface)
            except TopologySpecError as exc:
                raise TopologySpecError(f"{name} connection #{idx}: {exc}") from exc

    segment_owner: dict[str, str] = {}
    segments = topology.segment_map()
    if len(segments) != len(topology.segments):
        raise TopologySpecError(f"{name}: segment ids must be unique")
    for segment in topology.segments:
        for device_id in segment.devices:
            if device_id not in devices:
                raise TopologySpecError(
                    f"{name} segment {segment.id!r}: unknown device {device_id!r}"
                )
            if device_id in segment_owner:
                raise TopologySpecError(
                    f"{name}: device {device_id!r} is in segments "
                    f"{segment_owner[device_id]!r} and {segment.id!r}; "
                    "orientation would be ambiguous"
                )
            segment_owner[device_id] = segment.id

        for pos in range(len(segment.devices) - 1):
            a = segment.devices[pos]
            b = segment.devices[pos + 1]
            if not edges_between_devices(topology, a, b):
                raise TopologySpecError(
                    f"{name} segment {segment.id!r}: {a!r} and {b!r} "
                    "are declared adjacent but have no connection"
                )

        for end in (END_FIRST, END_LAST):
            try:
                segment_end_interface(topology, segment, end)
            except TopologySpecError:
                # A one-device segment or underspecified interfaces may still
                # be validated by bridge-specific logic.
                pass


def validate_bridges(topology: Topology, bridges: Iterable[BridgeConstraint]) -> None:
    devices = topology.device_map()
    segments = topology.segment_map()
    for bridge in bridges:
        if bridge.segment_id not in segments:
            raise TopologySpecError(f"bridge {bridge.id!r}: unknown segment {bridge.segment_id!r}")
        bp = bridge.bridge_port
        if bp.device not in devices:
            raise TopologySpecError(f"bridge {bridge.id!r}: unknown bridge device {bp.device!r}")
        try:
            devices[bp.device].interface(bp.interface)
        except TopologySpecError as exc:
            raise TopologySpecError(f"bridge {bridge.id!r}: {exc}") from exc
        if bridge.interface is not None:
            dev_id = segments[bridge.segment_id].end_device(bridge.end)
            try:
                devices[dev_id].interface(bridge.interface)
            except TopologySpecError as exc:
                raise TopologySpecError(f"bridge {bridge.id!r}: {exc}") from exc
        edge = managed_logical_edge(topology, bridge)
        if edge is None:
            raise TopologySpecError(
                f"bridge {bridge.id!r}: no {bridge.kind!r} connection found between "
                f"{bp} and the {bridge.end} end of segment {bridge.segment_id!r}"
            )


def validate_anchors(
    anchors: Iterable[Anchor],
    logical: Topology,
    observed: Topology,
) -> None:
    ld = logical.device_map()
    od = observed.device_map()
    for anchor in anchors:
        if anchor.logical not in ld:
            raise TopologySpecError(f"anchor references unknown logical device {anchor.logical!r}")
        if anchor.observed not in od:
            raise TopologySpecError(
                f"anchor for {anchor.logical!r} references unknown observed device "
                f"{anchor.observed!r}"
            )
        if anchor.location is not None:
            location = od[anchor.observed].location
            if location != anchor.location:
                raise TopologySpecError(
                    f"anchor {anchor.logical}->{anchor.observed} expects location "
                    f"{anchor.location!r}, but observed location is {location!r}"
                )


def make_type_predicate(
    anonymous_types: Iterable[str] = (),
    type_families: Mapping[str, str] | None = None,
):
    anonymous = frozenset(anonymous_types)
    families = dict(type_families or {})

    def family(type_name: str) -> str:
        return families.get(type_name, type_name)

    def compatible(logical_type: str, observed_type: str) -> bool:
        if logical_type == observed_type:
            return True
        lf = family(logical_type)
        of = family(observed_type)
        return lf == of and lf in anonymous

    return compatible
