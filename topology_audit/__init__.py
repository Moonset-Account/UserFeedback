"""Topology audit toolkit for stable mapping after device re-enumeration."""

from .model import (
    SOURCE_CONFIRMED,
    SOURCE_FROZEN,
    Anchor,
    BridgeConstraint,
    Connection,
    Device,
    Interface,
    PortRef,
    Segment,
    Topology,
    TopologySpecError,
)
from .report import AnchorFeedback, AuditReport, Match
from .session import AuditSession, ConfirmationResult, audit_topology
from .io import audit_from_file, load_request_json, session_from_file

__all__ = [
    "Anchor",
    "AnchorFeedback",
    "AuditReport",
    "AuditSession",
    "BridgeConstraint",
    "ConfirmationResult",
    "Connection",
    "Device",
    "Interface",
    "Match",
    "PortRef",
    "Segment",
    "Topology",
    "TopologySpecError",
    "audit_topology",
    "audit_from_file",
    "load_request_json",
    "session_from_file",
    "SOURCE_CONFIRMED",
    "SOURCE_FROZEN",
]

__version__ = "1.0.0"
