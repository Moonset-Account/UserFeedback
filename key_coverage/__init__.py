"""发送前密钥覆盖检查（key coverage check）。

公开 API 见 :mod:`key_coverage.check` 与 :mod:`key_coverage.model`。
"""

from .check import check_key_coverage
from .model import (
    Alias,
    CoverageResult,
    DirectorySnapshot,
    DirectoryStore,
    Group,
    KeyDiagnostic,
    KeyRecord,
    MemberKind,
    MemberRef,
    MissingKeyFailure,
    PathNode,
    Person,
    RecipientCoverage,
    Status,
)

__all__ = [
    "check_key_coverage",
    "Alias",
    "CoverageResult",
    "DirectorySnapshot",
    "DirectoryStore",
    "Group",
    "KeyDiagnostic",
    "KeyRecord",
    "MemberKind",
    "MemberRef",
    "MissingKeyFailure",
    "PathNode",
    "Person",
    "RecipientCoverage",
    "Status",
]
