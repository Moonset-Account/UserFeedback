"""Core data model for the offline VLAN isolation checker.

Terminology
-----------
An *isolation domain* is local to a switch.  It is identified by the pair
``(device, vlan_id)``.  The same numeric VLAN id on two adjacent switches
may name two different isolation domains; the ``domain_pairs`` mapping on
:class:`~vlancheck.topology.Topology` declares which local domains are the
same end-to-end partition.

A port owns:

``native_vlan``
    Local domain that receives every *untagged* frame arriving on the port
    (PVID behaviour).

``tagged_ingress_vlans``
    Set of tag ids the port is willing to accept on tagged ingress.  Any
    other tag is rejected and recorded as a drop.

``egress``
    For each local domain the port carries, what happens when a frame of
    that domain leaves the port: either ``("tag", vlan_id)`` (send it
    tagged with that id) or :data:`EGRESS_UNTAG` (strip the tag).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple

#: Egress action meaning "strip the 802.1Q tag before sending".
EGRESS_UNTAG = "untag"

# Internal canonical egress actions: ("tag", int) or ("untag",)
EgressAction = Tuple[object, ...]
PortRef = Tuple[str, str]
DomainRef = Tuple[str, int]


class ConfigError(ValueError):
    """Raised when the supplied topology configuration is malformed."""


def parse_egress_action(value: object) -> EgressAction:
    """Convert a user-supplied egress action into canonical tuple form.

    Accepted forms::

        {"tag": 10}      -> ("tag", 10)
        "untag"          -> ("untag",)
        {"untag": True}  -> ("untag",)
    """
    if isinstance(value, str) and value == EGRESS_UNTAG:
        return ("untag",)
    if isinstance(value, dict):
        if "tag" in value:
            vid = value["tag"]
            if not isinstance(vid, int) or isinstance(vid, bool):
                raise ConfigError(f"egress tag must be an integer VLAN id, got {vid!r}")
            _require_valid_vlan(vid, "egress tag")
            return ("tag", vid)
        if value.get("untag") is True or "untag" in value and value["untag"]:
            return ("untag",)
    raise ConfigError(f"unsupported egress action: {value!r}")


def _require_valid_vlan(vid: object, what: str) -> int:
    if not isinstance(vid, int) or isinstance(vid, bool):
        raise ConfigError(f"{what} must be an integer VLAN id, got {vid!r}")
    if not 1 <= vid <= 4094:
        raise ConfigError(f"{what} VLAN id {vid} outside 1..4094")
    return vid


@dataclass(frozen=True)
class PortConfig:
    """Configuration of a single switched port."""

    device: str
    name: str
    native_vlan: int
    tagged_ingress_vlans: FrozenSet[int]
    egress: Dict[int, EgressAction]
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.device, str) or not self.device:
            raise ConfigError("port device must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise ConfigError("port name must be a non-empty string")
        _require_valid_vlan(self.native_vlan, "native_vlan")
        tagged = frozenset(self.tagged_ingress_vlans)
        for vid in tagged:
            _require_valid_vlan(vid, "tagged ingress vlan")
        if not isinstance(self.egress, dict) or not self.egress:
            raise ConfigError(f"port {self.ref} needs at least one egress domain")
        canonical: Dict[int, EgressAction] = {}
        wire_labels: Dict[int, int] = {}
        for vid, action in self.egress.items():
            domain_vid = _require_valid_vlan(vid, "egress domain")
            parsed = action if isinstance(action, tuple) else parse_egress_action(action)
            canonical[domain_vid] = parsed
            if parsed[0] == "tag":
                label = parsed[1]
                if label in wire_labels:
                    raise ConfigError(
                        f"port {self.ref}: wire tag {label} used by both local "
                        f"vlans {wire_labels[label]} and {domain_vid}"
                    )
                wire_labels[label] = domain_vid
        if self.native_vlan not in canonical:
            raise ConfigError(
                f"port {self.ref}: native_vlan {self.native_vlan} must also have "
                "an egress action (the port is a member of that domain)"
            )
        for vid in tagged:
            if not 1 <= vid <= 4094:
                raise ConfigError(f"tagged ingress vlan {vid} outside 1..4094")
        object.__setattr__(self, "tagged_ingress_vlans", tagged)
        object.__setattr__(self, "egress", canonical)

    @property
    def ref(self) -> PortRef:
        return (self.device, self.name)

    @property
    def member_vlans(self) -> FrozenSet[int]:
        """Local isolation domains this port belongs to."""
        return frozenset(self.egress.keys())

    @property
    def ingress_tag_map(self) -> Dict[int, int]:
        """Mapping of *wire tag id* -> *local vlan id* on tagged ingress.

        If an egress action sends local vlan ``V`` with wire tag ``N``
        (VLAN translation), an incoming frame tagged ``N`` is interpreted
        as local domain ``V``.  Any other accepted tag is interpreted as
        the same-numbered local vlan (plain trunk semantics).
        """
        mapping: Dict[int, int] = {}
        for local_vid, action in self.egress.items():
            if action[0] == "tag":
                mapping[action[1]] = local_vid
        for label in self.tagged_ingress_vlans:
            mapping.setdefault(label, label)
        return mapping

    def egress_for(self, vlan_id: int) -> Optional[EgressAction]:
        return self.egress.get(vlan_id)


@dataclass(frozen=True)
class Link:
    """A point-to-point cable between two explicitly named ports."""

    a: PortRef
    b: PortRef

    def __post_init__(self) -> None:
        for end in (self.a, self.b):
            if (
                not isinstance(end, tuple)
                or len(end) != 2
                or not all(isinstance(part, str) and part for part in end)
            ):
                raise ConfigError(f"link endpoint must be (device, port), got {end!r}")
        if self.a == self.b:
            raise ConfigError(f"link {self.a} <-> {self.b} cannot loop back on itself")

    def other_end(self, port: PortRef) -> PortRef:
        if port == self.a:
            return self.b
        if port == self.b:
            return self.a
        raise KeyError(f"port {port} is not an endpoint of link {self.a} <-> {self.b}")


# ---------------------------------------------------------------------------
# Analysis result records
# ---------------------------------------------------------------------------

# Boundary outcome constants (used on BoundaryEvent.status):
#   "ok"                      - peer reception matches a prescribed domain pair
#   "leak"                    - pair exists but peer belongs to another domain
#   "unmapped"                - no pair declared for this domain crossing
#   "untagged_native_misreceive" - tag stripped, peer's PVID is a foreign domain
#   "untagged_ok"             - tag stripped, peer's PVID is the paired domain
BOUNDARY_STATUSES = frozenset(
    {
        "ok",
        "leak",
        "unmapped",
        "untagged_ok",
        "untagged_native_misreceive",
    }
)


@dataclass(frozen=True)
class BoundaryEvent:
    """One frame crossing from an egress port into a receiving port."""

    root: DomainRef
    hops: int
    egress_port: PortRef
    ingress_port: PortRef
    sent_tag: Optional[int]  # None means the frame was sent untagged
    received_domain: DomainRef
    status: str
    expected_domain: Optional[DomainRef]

    def __post_init__(self) -> None:
        if self.status not in BOUNDARY_STATUSES:
            raise ConfigError(f"unknown boundary status {self.status!r}")

    @property
    def is_leak(self) -> bool:
        return self.status in ("leak", "untagged_native_misreceive")

    @property
    def tag_was_stripped(self) -> bool:
        return self.sent_tag is None

    def to_dict(self) -> dict:
        return {
            "root": list(self.root),
            "hops": self.hops,
            "egress_port": list(self.egress_port),
            "ingress_port": list(self.ingress_port),
            "sent_tag": self.sent_tag,
            "received_domain": list(self.received_domain),
            "status": self.status,
            "expected_domain": list(self.expected_domain)
            if self.expected_domain
            else None,
        }


@dataclass(frozen=True)
class DropEvent:
    """A tagged frame rejected at an ingress port (not in its accept list)."""

    root: DomainRef
    hops: int
    egress_port: PortRef
    ingress_port: PortRef
    rejected_tag: int

    def to_dict(self) -> dict:
        return {
            "root": list(self.root),
            "hops": self.hops,
            "egress_port": list(self.egress_port),
            "ingress_port": list(self.ingress_port),
            "rejected_tag": self.rejected_tag,
        }


@dataclass
class ReachabilityResult:
    """Everything reachable from one ingress isolation domain."""

    root: DomainRef
    reached_domains: set = field(default_factory=set)
    boundary_events: list = field(default_factory=list)
    first_leak: Optional[BoundaryEvent] = None

    @property
    def leaks(self) -> list:
        return [event for event in self.boundary_events if event.is_leak]

    @property
    def stripped_but_misreceived(self) -> list:
        return [
            event
            for event in self.boundary_events
            if event.status == "untagged_native_misreceive"
        ]

    @property
    def stripped_and_correct(self) -> list:
        return [
            event for event in self.boundary_events if event.status == "untagged_ok"
        ]

    def to_dict(self) -> dict:
        return {
            "root": list(self.root),
            "reached_domains": [list(d) for d in sorted(self.reached_domains)],
            "boundary_events": [event.to_dict() for event in self.boundary_events],
            "first_leak": self.first_leak.to_dict() if self.first_leak else None,
        }


@dataclass
class AnalysisReport:
    """Full analysis outcome across every ingress domain in the topology."""

    results: Dict[DomainRef, ReachabilityResult]
    drops: list = field(default_factory=list)

    @property
    def leaking_roots(self) -> Dict[DomainRef, BoundaryEvent]:
        return {
            root: result.first_leak
            for root, result in self.results.items()
            if result.first_leak is not None
        }

    @property
    def has_leak(self) -> bool:
        return bool(self.leaking_roots)

    def to_dict(self) -> dict:
        return {
            "results": [
                self.results[key].to_dict() for key in sorted(self.results)
            ],
            "drops": [drop.to_dict() for drop in self.drops],
            "has_leak": self.has_leak,
        }
