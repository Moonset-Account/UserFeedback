"""Local, testable password-screening service and client.

The protocol is a k-anonymity style prefix/suffix lookup:

* the service owns a versioned list of fixed-length (SHA-256, 64 hex chars) digests;
* the client sends only the digest prefix and a pinned version id;
* the service returns full pages of suffixes (records plus typed padding);
* the client compares the secret suffix locally.

Every prefix in a version - including prefixes with no records - receives the
same page size and page count.  A version switch is atomic; a client that is
paging either finishes the pinned version or receives an explicit
``restart_required`` signal.
"""

from .client import HTTPTransport, LocalTransport, ScreenResult, ScreeningClient
from .digest import DIGEST_LENGTH, normalize_digest, normalize_digest_list, password_digest
from .errors import (
    DigestError,
    InvalidPrefix,
    NoCurrentVersion,
    PageOutOfRange,
    ProtocolError,
    RestartRequired,
    ScreeningError,
    VersionUnavailable,
)
from .server import ScreeningService, serve
from .store import VersionStore
from .version import DigestVersion, PageEntry, build_version

__all__ = [
    "DIGEST_LENGTH",
    "DigestError",
    "DigestVersion",
    "HTTPTransport",
    "InvalidPrefix",
    "LocalTransport",
    "NoCurrentVersion",
    "PageEntry",
    "PageOutOfRange",
    "ProtocolError",
    "RestartRequired",
    "ScreenResult",
    "ScreeningClient",
    "ScreeningError",
    "ScreeningService",
    "VersionStore",
    "VersionUnavailable",
    "build_version",
    "normalize_digest",
    "normalize_digest_list",
    "password_digest",
    "serve",
]

__version__ = "1.0.0"
