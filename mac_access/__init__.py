"""mac_access: label-based access control for endpoint policy verification.

Public API:

* :class:`Label`, :class:`Clearance` -- sensitivity labels and clearance bounds.
* :class:`AccessMonitor` -- the reference monitor (register subjects/objects,
  open shared mappings, read with upgrade, queue/deliver write-backs).
* :class:`AccessStatus`, :class:`ReadResult`, :class:`WriteResult` -- explicit
  outcomes so callers can distinguish denied reads, revoked mappings and
  successful writes.
"""

from .labels import Clearance, Label
from .monitor import (
    AccessControlError,
    AccessMonitor,
    AccessStatus,
    ForeignHandleError,
    Handle,
    Mapping,
    MemoryObject,
    Process,
    ReadResult,
    UnknownHandleError,
    UnknownObjectError,
    UnknownProcessError,
    WriteResult,
)

__all__ = [
    "Label",
    "Clearance",
    "AccessStatus",
    "AccessControlError",
    "UnknownProcessError",
    "UnknownObjectError",
    "UnknownHandleError",
    "ForeignHandleError",
    "ReadResult",
    "WriteResult",
    "MemoryObject",
    "Mapping",
    "Handle",
    "Process",
    "AccessMonitor",
]
