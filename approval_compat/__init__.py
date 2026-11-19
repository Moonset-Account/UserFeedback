"""Pre-release compatibility checks for approval-bearing message fields.

Public API
----------
* :class:`FieldDef` / :class:`MessageVersion` -- one published wire version.
* :class:`HopTransform` / :class:`Route` -- one fixed forwarding path.
* :func:`route_from_dict` -- load a route from JSON.
* :func:`check_route` / :func:`check_routes` -- run the analysis.
* :class:`RouteReport` -- verdict plus a per-field diagnostic trail.

A route passes only when no *new* path exists from a legacy sender's ordinary
bytes to an approval meaning.  Two ways exist to sever such a path: drop the
legacy bytes and rebuild the value from a trusted input, or never let a legacy
sender address the number at all.

Quick start::

    from approval_compat import MessageVersion, HopTransform, Route, check_route

    v1 = MessageVersion.from_raw("v1", {7: ("normal", "display_name")})
    v2 = MessageVersion.from_raw("v2", {7: ("normal", "approval")})
    report = check_route(
        Route(
            name="direct",
            origin=v1,
            destination=v2,
            hops=(HopTransform(hop="relay"),),
        )
    )
    assert not report.ok
    print(report.format_text())
"""

from .engine import (
    ABSENT,
    BLOCK,
    HOP_WRITTEN,
    INFO,
    SENDER_BYTES,
    TRUSTED,
    WARN,
    FieldTrace,
    RouteReport,
    SourceRef,
    Violation,
    check_route,
    check_routes,
    summarize,
)
from .model import (
    APPROVAL_MEANINGS,
    ENCODINGS,
    ORDINARY_MEANINGS,
    FieldDef,
    HopTransform,
    MessageVersion,
    Route,
    SchemaError,
    route_from_dict,
)

__all__ = [
    "ABSENT",
    "APPROVAL_MEANINGS",
    "BLOCK",
    "ENCODINGS",
    "HOP_WRITTEN",
    "INFO",
    "ORDINARY_MEANINGS",
    "SENDER_BYTES",
    "TRUSTED",
    "WARN",
    "FieldDef",
    "FieldTrace",
    "HopTransform",
    "MessageVersion",
    "Route",
    "RouteReport",
    "SchemaError",
    "SourceRef",
    "Violation",
    "check_route",
    "check_routes",
    "route_from_dict",
    "summarize",
]
