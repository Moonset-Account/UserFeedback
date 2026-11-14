"""Plain data models for the offline ACL diagnostic.

All input data is synthetic.  Paths are fictional POSIX-style strings;
no real filesystem object is ever read.
"""

from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Permission / decision constants
# ---------------------------------------------------------------------------
ALLOW = "allow"
DENY = "deny"
UNVERIFIED = "unverified"  # 待核实：权限记录不足，无法得出确定结论

# The two questions the diagnostic answers.
MODIFY_FILE = "modify_file"        # 能否修改文件内容（对文件本身的写权限）
REPLACE_ENTRY = "replace_entry"    # 能否通过父目录替换条目（对父目录的写权限）

# ACE origins
EXPLICIT = "explicit"              # 直接挂在对象上的 ACE
INHERITED = "inherited"            # 从父目录继承的 ACE

# Actions
ACTIONS = (MODIFY_FILE, REPLACE_ENTRY)


@dataclass(frozen=True)
class ACE:
    """One access control entry.

    ace_type:   "allow" | "deny"
    principal:  account id or group id referenced by the entry
    rights:     subset of {"modify_file", "replace_entry"}; "all" means both
    inherited:  True when the entry was inherited from a parent directory
                (either recorded as inherited on the object itself, or
                propagated from an inheritable directory ACE)
    source_path: path of the object the entry is attached to; for an
                inherited entry this is the ancestor that supplied it
    """

    ace_type: str
    principal: str
    rights: frozenset
    inherited: bool
    source_path: str

    @classmethod
    def from_dict(cls, raw: dict, source_path: str) -> "ACE":
        ace_type = str(raw["ace_type"]).lower()
        if ace_type not in (ALLOW, DENY):
            raise ValueError(
                "ace_type must be 'allow' or 'deny', got %r (on %s)"
                % (raw.get("ace_type"), source_path)
            )
        raw_rights = raw.get("rights", "all")
        if isinstance(raw_rights, str):
            rights = frozenset(ACTIONS) if raw_rights == "all" else frozenset((raw_rights,))
        else:
            rights = frozenset(raw_rights)
        unknown = rights - frozenset(ACTIONS)
        if unknown:
            raise ValueError("unknown right %r in ACE on %s" % (sorted(unknown), source_path))
        return cls(
            ace_type=ace_type,
            principal=str(raw["principal"]),
            rights=rights,
            inherited=bool(raw.get("inherited", False)),
            source_path=source_path,
        )

    def to_dict(self) -> dict:
        return {
            "ace_type": self.ace_type,
            "principal": self.principal,
            "rights": sorted(self.rights),
            "inherited": self.inherited,
            "source_path": self.source_path,
        }


@dataclass
class LegacyConclusion:
    """A conclusion produced by the pre-migration periodic check."""

    subject_id: str
    action: str
    conclusion: str  # allow | deny | unverified
    basis: str = ""


@dataclass
class ServiceConfig:
    """One row of the synthetic service configuration inventory."""

    service: str
    path: str
    legacy: List[LegacyConclusion] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "ServiceConfig":
        legacy = []
        for item in raw.get("legacy_conclusions", []):
            legacy.append(
                LegacyConclusion(
                    subject_id=str(item["subject_id"]),
                    action=str(item["action"]),
                    conclusion=str(item["conclusion"]).lower(),
                    basis=str(item.get("basis", "")),
                )
            )
        return cls(service=str(raw["service"]), path=str(raw["path"]), legacy=legacy)


@dataclass
class SubjectDirectory:
    """Subject/group relationships (synthetic account records).

    accounts:   account id -> {"name": ..., "privileged": bool}
    groups:     group id   -> {"name": ..., "members": [account/group ids]}
    """

    accounts: dict
    groups: dict
    version: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "SubjectDirectory":
        accounts = {}
        for acc_id, info in raw.get("accounts", {}).items():
            accounts[str(acc_id)] = {
                "name": str(info.get("name", acc_id)),
                "privileged": bool(info.get("privileged", False)),
            }
        groups = {}
        for grp_id, info in raw.get("groups", {}).items():
            groups[str(grp_id)] = {
                "name": str(info.get("name", grp_id)),
                "members": [str(m) for m in info.get("members", [])],
            }
        return cls(accounts=accounts, groups=groups, version=str(raw.get("version", "")))


@dataclass
class PermissionSnapshot:
    """A snapshot of exported permission records for fictional paths.

    objects maps path -> {"type": "file"|"directory", "aces": [ACE],
                          "exported": bool}
    A directory present with exported=False (or missing entirely while a
    child exists) models "directory permissions were not exported".
    """

    objects: dict
    version: str = ""

    def get(self, path: str) -> Optional[dict]:
        return self.objects.get(path)
