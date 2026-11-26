"""Data model for deterministic hardware-topology reconciliation.

The module intentionally uses only the Python standard library.  The structures
are small, frozen value objects so reports and examples can be serialized and
compared deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

ROLE_UPSTREAM = "upstream"
ROLE_DOWNSTREAM = "downstream"
ROLE_PEER = "peer"
UP = ROLE_UPSTREAM
DOWN = ROLE_DOWNSTREAM

END_FIRST = "first"
END_LAST = "last"

SOURCE_FROZEN = "frozen"
SOURCE_CONFIRMED = "confirmed"
SOURCE_BRIDGE = "bridge"


class TopologySpecError(ValueError):
    """Raised when a topology/request refers to missing or inconsistent data."""


@dataclass(frozen=True, order=True)
class PortRef:
    """A named interface on a device."""

    device: str
    interface: str

    @classmethod
    def parse(cls, value: "PortRef | Mapping[str, Any] | SequenceLike | str") -> "PortRef":
        if isinstance(value, PortRef):
            return value
        if isinstance(value, Mapping):
            return cls(str(value["device"]), str(value["interface"]))
        if isinstance(value, str):
            if ":" not in value:
                raise TopologySpecError(f"port reference must be device:interface, got {value!r}")
            device, interface = value.split(":", 1)
            return cls(device, interface)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return cls(str(value[0]), str(value[1]))
        raise TopologySpecError(f"invalid port reference: {value!r}")

    def __str__(self) -> str:
        return f"{self.device}:{self.interface}"


SequenceLike = list[Any] | tuple[Any, ...]


@dataclass(frozen=True)
class Interface:
    name: str
    kind: str = "any"
    role: str | None = None

    @classmethod
    def from_dict(cls, data: "Interface | Mapping[str, Any] | str") -> "Interface":
        if isinstance(data, Interface):
            return data
        if isinstance(data, str):
            return cls(name=data)
        return cls(
            name=str(data["name"]),
            kind=str(data.get("kind", "any")),
            role=data.get("role"),
        )


@dataclass(frozen=True)
class Device:
    id: str
    device_type: str
    interfaces: tuple[Interface, ...] = ()
    location: str | None = None
    stable: bool = False
    tags: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_dict(cls, data: "Device | Mapping[str, Any]") -> "Device":
        if isinstance(data, Device):
            return data
        return cls(
            id=str(data["id"]),
            device_type=str(data.get("type", data.get("device_type", "unknown"))),
            interfaces=tuple(Interface.from_dict(x) for x in data.get("interfaces", ())),
            location=data.get("location"),
            stable=bool(data.get("stable", False)),
            tags=frozenset(data.get("tags", ())),
        )

    def interface(self, name: str) -> Interface:
        for item in self.interfaces:
            if item.name == name:
                return item
        raise TopologySpecError(f"device {self.id!r} has no interface {name!r}")


@dataclass(frozen=True)
class Connection:
    a: PortRef
    b: PortRef
    kind: str = "any"

    @classmethod
    def from_dict(cls, data: "Connection | Mapping[str, Any]") -> "Connection":
        if isinstance(data, Connection):
            return data
        if "a" in data and "b" in data:
            a = PortRef.parse(data["a"])
            b = PortRef.parse(data["b"])
        else:
            endpoints = data.get("endpoints")
            if not isinstance(endpoints, (list, tuple)) or len(endpoints) != 2:
                raise TopologySpecError("connection requires a/b or endpoints=[a,b]")
            a = PortRef.parse(endpoints[0])
            b = PortRef.parse(endpoints[1])
        return cls(a=a, b=b, kind=str(data.get("kind", "any")))

    def other(self, port: PortRef) -> PortRef:
        if self.a == port:
            return self.b
        if self.b == port:
            return self.a
        raise KeyError(port)

    def involves(self, port: PortRef) -> bool:
        return self.a == port or self.b == port


@dataclass(frozen=True)
class Segment:
    """An ordered daisy-chain segment.

    ``devices`` is ordered from the nominal first/upstream end to the nominal
    last/downstream end.  The matcher may align it normally or in reverse only
    when reversal is permitted.
    """

    id: str
    devices: tuple[str, ...]
    reversible: bool = True
    first_interface: str | None = None
    last_interface: str | None = None

    @classmethod
    def from_dict(cls, data: "Segment | Mapping[str, Any]") -> "Segment":
        if isinstance(data, Segment):
            return data
        devices = tuple(str(x) for x in data.get("devices", ()))
        if len(devices) < 1:
            raise TopologySpecError(f"segment {data.get('id')!r} needs at least one device")
        return cls(
            id=str(data["id"]),
            devices=devices,
            reversible=bool(data.get("reversible", True)),
            first_interface=data.get("first_interface"),
            last_interface=data.get("last_interface"),
        )

    @property
    def first_device(self) -> str:
        return self.devices[0]

    @property
    def last_device(self) -> str:
        return self.devices[-1]

    def end_device(self, end: str) -> str:
        return self.devices[0] if end == END_FIRST else self.devices[-1]


@dataclass(frozen=True)
class BridgeConstraint:
    """A stable bridge port and the segment end normally attached to it.

    When ``allow_reverse`` is true, a reversed segment may put the opposite
    nominal end at this stable bridge port.
    """

    id: str
    bridge_port: PortRef
    segment_id: str
    end: str = END_FIRST
    interface: str | None = None
    kind: str = "any"
    allow_reverse: bool = True

    @classmethod
    def from_dict(cls, data: "BridgeConstraint | Mapping[str, Any]") -> "BridgeConstraint":
        if isinstance(data, BridgeConstraint):
            return data
        end = str(data.get("end", END_FIRST))
        if end not in (END_FIRST, END_LAST):
            raise TopologySpecError("bridge end must be 'first' or 'last'")
        return cls(
            id=str(data["id"]),
            bridge_port=PortRef.parse(data.get("bridge_port", data.get("port"))),
            segment_id=str(data["segment"] if "segment" in data else data["segment_id"]),
            end=end,
            interface=data.get("interface"),
            kind=str(data.get("kind", "any")),
            allow_reverse=bool(data.get("allow_reverse", True)),
        )


@dataclass(frozen=True)
class Topology:
    devices: tuple[Device, ...] = ()
    connections: tuple[Connection, ...] = ()
    segments: tuple[Segment, ...] = ()

    @classmethod
    def from_dict(cls, data: "Topology | Mapping[str, Any] | None") -> "Topology":
        if data is None:
            return cls()
        if isinstance(data, Topology):
            return data
        return cls(
            devices=tuple(Device.from_dict(x) for x in data.get("devices", ())),
            connections=tuple(Connection.from_dict(x) for x in data.get("connections", ())),
            segments=tuple(Segment.from_dict(x) for x in data.get("segments", ())),
        )

    def device_ids(self) -> list[str]:
        return [d.id for d in sorted(self.devices, key=lambda d: d.id)]

    def device_map(self) -> dict[str, Device]:
        return {d.id: d for d in self.devices}

    def segment_map(self) -> dict[str, Segment]:
        return {s.id: s for s in self.segments}


@dataclass(frozen=True)
class Anchor:
    logical: str
    observed: str
    location: str | None = None
    source: str = SOURCE_FROZEN
    note: str | None = None

    @classmethod
    def coerce(
        cls,
        value: "Anchor | Mapping[str, Any] | tuple[str, str]",
        *,
        source: str = SOURCE_FROZEN,
    ) -> "Anchor":
        if isinstance(value, Anchor):
            return value
        if isinstance(value, tuple) and len(value) == 2:
            return cls(str(value[0]), str(value[1]), source=source)
        if isinstance(value, Mapping):
            src = str(value.get("source", source))
            return cls(
                logical=str(value.get("logical", value.get("logical_id"))),
                observed=str(value.get("observed", value.get("observed_id"))),
                location=value.get("location"),
                source=src,
                note=value.get("note"),
            )
        raise TopologySpecError(f"invalid anchor: {value!r}")

    def key(self) -> tuple[str, str, str | None]:
        return (self.logical, self.observed, self.location)


def anchors_from_mapping(
    value: Mapping[str, str | Mapping[str, Any]] | Iterable[Any] | None,
    *,
    source: str,
) -> tuple[Anchor, ...]:
    if not value:
        return ()
    anchors: list[Anchor] = []
    if isinstance(value, Mapping):
        for logical, observed in value.items():
            if isinstance(observed, Mapping):
                item = dict(observed)
                item.setdefault("logical", logical)
                anchors.append(Anchor.coerce(item, source=source))
            else:
                anchors.append(Anchor(logical=str(logical), observed=str(observed), source=source))
    else:
        for item in value:
            anchors.append(Anchor.coerce(item, source=source))
    return tuple(anchors)
