"""Local authoritative DNS kernel (A/AAAA/TXT answers, NS delegations).

The package is deliberately network-free: a :class:`Zone` holds a synthetic
fixture in memory and answers from it, so tests can exercise the complete
resolution matrix deterministically.
"""

from .names import (
    InvalidDNSName,
    Name,
    OutOfZoneName,
    is_subdomain,
    parse_name,
    proper_ancestors,
    to_text,
    wildcard_child,
)
from .records import (
    DEFAULT_TTL,
    SUPPORTED_QUERY_TYPES,
    SUPPORTED_RECORD_TYPES,
    InvalidRecord,
    Record,
    RecordError,
)
from .status import QUERY_TYPES, RECORD_TYPES, AnswerStatus
from .zone import (
    ANSWER,
    NODATA,
    NXDOMAIN,
    REFERRAL,
    Response,
    Zone,
    ZoneError,
)

__all__ = [
    "ANSWER",
    "AnswerStatus",
    "DEFAULT_TTL",
    "InvalidDNSName",
    "InvalidRecord",
    "NODATA",
    "NXDOMAIN",
    "Name",
    "OutOfZoneName",
    "QUERY_TYPES",
    "RECORD_TYPES",
    "REFERRAL",
    "Record",
    "RecordError",
    "Response",
    "SUPPORTED_QUERY_TYPES",
    "SUPPORTED_RECORD_TYPES",
    "Zone",
    "ZoneError",
    "is_subdomain",
    "parse_name",
    "proper_ancestors",
    "to_text",
    "wildcard_child",
]

__version__ = "1.0.0"
