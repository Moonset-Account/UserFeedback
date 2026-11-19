"""Offline campus-network VLAN / isolation-domain configuration checker.

Public API:
    PortConfig, Link, Topology, analyze, load_config
    BoundaryEvent, DropEvent, ReachabilityResult, AnalysisReport
"""

from .model import (
    EGRESS_UNTAG,
    BoundaryEvent,
    ConfigError,
    DropEvent,
    Link,
    PortConfig,
    ReachabilityResult,
    AnalysisReport,
    parse_egress_action,
)
from .topology import Topology
from .analyzer import analyze
from .loader import load_config, port_ref_from_string, device_ref_from_string

__all__ = [
    "EGRESS_UNTAG",
    "BoundaryEvent",
    "ConfigError",
    "DropEvent",
    "Link",
    "PortConfig",
    "ReachabilityResult",
    "AnalysisReport",
    "Topology",
    "analyze",
    "load_config",
    "parse_egress_action",
    "port_ref_from_string",
    "device_ref_from_string",
]
