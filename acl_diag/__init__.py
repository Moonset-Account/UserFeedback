"""SEC-VULN-039 offline ACL diagnostic tool.

Analyzes synthetic service configuration inventories, subject/group
membership snapshots and directory permission snapshots, and decides
whether a subject can modify a service configuration file or replace
its directory entry.
"""

from .models import (
    ACE,
    ServiceConfig,
    LegacyConclusion,
    SubjectDirectory,
    PermissionSnapshot,
)
from .membership import expand_members, is_privileged, load_subject_directory
from .evaluator import (
    ALLOW,
    DENY,
    UNVERIFIED,
    MODIFY_FILE,
    REPLACE_ENTRY,
    Finding,
    evaluate_subject,
    evaluate_inventory,
    load_snapshot,
    load_inventory,
)
from .diff import build_diff
from .remediation import remediate, apply_remediation

__all__ = [
    "ACE",
    "ServiceConfig",
    "LegacyConclusion",
    "SubjectDirectory",
    "PermissionSnapshot",
    "Finding",
    "expand_members",
    "is_privileged",
    "load_subject_directory",
    "ALLOW",
    "DENY",
    "UNVERIFIED",
    "MODIFY_FILE",
    "REPLACE_ENTRY",
    "evaluate_subject",
    "evaluate_inventory",
    "load_snapshot",
    "load_inventory",
    "build_diff",
    "remediate",
    "apply_remediation",
]
