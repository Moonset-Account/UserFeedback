"""Small POSIX-style ACL model for rehearsing new files in a shared directory.

The model implements three linked behaviours from POSIX ACL semantics
(``acl(5)``):

1. **Creation.**  When the parent directory carries a default ACL, the
   requested owner/group-class/other permission bits clip ``user::``,
   ``mask::`` and ``other::`` of the derived access ACL.  Named user and
   group entries, as well as ``group::``, keep their default values.  Without
   a default ACL the access ACL is built directly from the requested mode.
2. **Access checking.**  The file owner wins over every named entry.  A
   matching named user is evaluated next and is limited by the mask.  Then
   the permissions of every matching group (owning group plus named groups)
   are unioned and limited by the mask.  Only when nothing matched does the
   ``other`` entry apply.
3. **chmod.**  Changing the group-class bits updates ``mask::`` when an
   extended ACL exists; a plain (minimal) ACL updates ``group::`` instead.

Everything runs in-process on synthetic data.  No real filesystem ACL is
read or written, and no privilege-bearing system call is used.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import IntFlag
from types import MappingProxyType
from typing import Any

__all__ = [
    "Acl",
    "AclError",
    "AclFile",
    "Evaluation",
    "FileMode",
    "Perm",
    "Subject",
    "create_acl",
    "create_file",
    "parse_acl",
]

_ALL_BITS = 0b111


class AclError(ValueError):
    """Raised when an ACL, mode, or access request is malformed."""


class Perm(IntFlag):
    """The three POSIX permission bits."""

    NONE = 0
    EXEC = 1
    WRITE = 2
    READ = 4

    @classmethod
    def from_text(cls, text: str) -> "Perm":
        """Parse permission text such as ``rwx``, ``r-x``, ``r``, or ``5``."""
        raw = str(text).strip().lower()
        if len(raw) == 1 and raw in "01234567":
            return cls(int(raw, 8))
        if not raw or len(raw) > 3:
            raise AclError(f"invalid permission text: {text!r}")
        value = cls.NONE
        seen: set[str] = set()
        for char, bit in (("r", cls.READ), ("w", cls.WRITE), ("x", cls.EXEC)):
            if char in raw:
                seen.add(char)
                value |= bit
        if any(char not in "rwx-" for char in raw) or len(seen) != len(raw.replace("-", "")):
            raise AclError(f"invalid permission text: {text!r}")
        return value

    def to_text(self) -> str:
        """Render as ``rwx`` with ``-`` for absent bits."""
        return "".join(
            char if self & bit else "-"
            for char, bit in (("r", self.READ), ("w", self.WRITE), ("x", self.EXEC))
        )

    def __str__(self) -> str:  # pragma: no cover - trivial rendering
        return self.to_text()


FULL = Perm.READ | Perm.WRITE | Perm.EXEC


def _coerce_perm(value: Any, where: str) -> Perm:
    if isinstance(value, Perm):
        if int(value) & ~_ALL_BITS:
            raise AclError(f"{where}: permission bits out of range: {value!r}")
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise AclError(f"{where}: expected Perm or int, got {value!r}")
    if value < 0 or value & ~_ALL_BITS:
        raise AclError(f"{where}: permission bits out of range: {value!r}")
    return Perm(value)


def _coerce_mode(value: "FileMode | int | str") -> "FileMode":
    if isinstance(value, FileMode):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AclError(f"expected FileMode, int, or octal string, got {value!r}")
    return FileMode.from_octal(value)


@dataclass(frozen=True)
class FileMode:
    """Requested permission bits, already masked by the creating process' umask."""

    owner: Perm
    group: Perm
    other: Perm

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", _coerce_perm(self.owner, "mode owner"))
        object.__setattr__(self, "group", _coerce_perm(self.group, "mode group"))
        object.__setattr__(self, "other", _coerce_perm(self.other, "mode other"))

    @classmethod
    def from_octal(cls, value: int | str) -> "FileMode":
        """Build from ``750``/``0o750``/``0750`` strings or a numeric mode."""
        if isinstance(value, bool):
            raise AclError(f"invalid mode: {value!r}")
        if isinstance(value, int):
            number = value
        elif isinstance(value, str):
            raw = value.strip().lower()
            if raw.startswith("0o"):
                raw = raw[2:]
            if not raw or any(char not in "01234567" for char in raw):
                raise AclError(f"invalid octal mode: {value!r}")
            number = int(raw, 8)
        else:
            raise AclError(f"invalid mode: {value!r}")
        if not 0 <= number <= 0o777:
            raise AclError(f"invalid mode (expected 0..0o777): {value!r}")
        return cls(
            Perm((number >> 6) & 0b111),
            Perm((number >> 3) & 0b111),
            Perm(number & 0b111),
        )

    def to_octal(self) -> str:
        return f"{int(self.owner):o}{int(self.group):o}{int(self.other):o}"


@dataclass(frozen=True)
class Acl:
    """An access or default ACL.

    ``mask`` is ``None`` for a minimal ACL.  Named entries are only valid
    together with a mask, mirroring the kernel's extended-ACL requirement.
    """

    user_obj: Perm
    group_obj: Perm
    other: Perm
    mask: Perm | None = None
    named_users: Mapping[Hashable, Perm] = field(default_factory=dict)
    named_groups: Mapping[Hashable, Perm] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_obj", _coerce_perm(self.user_obj, "user::"))
        object.__setattr__(self, "group_obj", _coerce_perm(self.group_obj, "group::"))
        object.__setattr__(self, "other", _coerce_perm(self.other, "other::"))
        if self.mask is not None:
            object.__setattr__(self, "mask", _coerce_perm(self.mask, "mask::"))

        named_users = {}
        for qualifier, perms in dict(self.named_users).items():
            if qualifier == "":
                raise AclError("named user entry needs a non-empty qualifier")
            named_users[qualifier] = _coerce_perm(perms, f"user:{qualifier}:")
        named_groups = {}
        for qualifier, perms in dict(self.named_groups).items():
            if qualifier == "":
                raise AclError("named group entry needs a non-empty qualifier")
            named_groups[qualifier] = _coerce_perm(perms, f"group:{qualifier}:")
        object.__setattr__(self, "named_users", MappingProxyType(named_users))
        object.__setattr__(self, "named_groups", MappingProxyType(named_groups))

        if (named_users or named_groups) and self.mask is None:
            raise AclError("named ACL entries require a mask:: entry")

    @property
    def is_extended(self) -> bool:
        return self.mask is not None or bool(self.named_users) or bool(self.named_groups)

    @property
    def group_class(self) -> Perm:
        """The permission bits exposed as the middle octal digit."""
        return self.mask if self.mask is not None else self.group_obj

    def to_mode(self) -> int:
        return (int(self.user_obj) << 6) | (int(self.group_class) << 3) | int(self.other)

    def format(self, *, default: bool = False) -> str:
        prefix = "default:" if default else ""
        lines = [f"{prefix}user::{self.user_obj.to_text()}"]
        for qualifier in sorted(self.named_users, key=str):
            perms = self.named_users[qualifier]
            lines.append(f"{prefix}user:{qualifier}:{perms.to_text()}")
        lines.append(f"{prefix}group::{self.group_obj.to_text()}")
        for qualifier in sorted(self.named_groups, key=str):
            perms = self.named_groups[qualifier]
            lines.append(f"{prefix}group:{qualifier}:{perms.to_text()}")
        if self.mask is not None:
            lines.append(f"{prefix}mask::{self.mask.to_text()}")
        lines.append(f"{prefix}other::{self.other.to_text()}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial rendering
        return self.format()

    def __hash__(self) -> int:
        return hash(
            (
                self.user_obj,
                self.group_obj,
                self.other,
                self.mask,
                frozenset(self.named_users.items()),
                frozenset(self.named_groups.items()),
            )
        )


_TOKEN_SPLIT = re.compile(r"[,\s]+")
_TAG_ALIASES = {"u": "user", "g": "group", "m": "mask", "o": "other"}


def parse_acl(text: str) -> Acl:
    """Parse a comma/whitespace separated ACL such as ``u::rwx,u:alice:r-x,...``.

    ``default:`` prefixes are accepted so the output of
    :meth:`Acl.format` can be round-tripped.
    """
    if not isinstance(text, str):
        raise AclError(f"ACL text must be a string, got {text!r}")
    tokens = [token for token in _TOKEN_SPLIT.split(text.strip()) if token]
    if not tokens:
        raise AclError("empty ACL text")

    seen: set[str] = set()
    values: dict[str, Perm] = {}
    named_users: dict[Hashable, Perm] = {}
    named_groups: dict[Hashable, Perm] = {}
    default_flags: set[bool] = set()

    for token in tokens:
        is_default = token.lower().startswith("default:")
        if is_default:
            token = token[len("default:"):]
        default_flags.add(is_default)
        parts = token.split(":")
        if len(parts) != 3:
            raise AclError(f"malformed ACL entry: {token!r}")
        kind_raw, qualifier, perms_raw = parts
        kind = _TAG_ALIASES.get(kind_raw.lower(), kind_raw.lower())
        perms = Perm.from_text(perms_raw)

        if kind in ("user", "group"):
            if qualifier == "":
                key = "user_obj" if kind == "user" else "group_obj"
                if key in seen:
                    raise AclError(f"duplicate ACL entry: {token!r}")
                seen.add(key)
                values[key] = perms
            else:
                bucket = named_users if kind == "user" else named_groups
                if qualifier in bucket:
                    raise AclError(f"duplicate ACL entry: {token!r}")
                bucket[qualifier] = perms
        elif kind == "mask":
            if qualifier != "":
                raise AclError(f"mask entry must not be qualified: {token!r}")
            if "mask" in seen:
                raise AclError(f"duplicate ACL entry: {token!r}")
            seen.add("mask")
            values["mask"] = perms
        elif kind == "other":
            if qualifier != "":
                raise AclError(f"other entry must not be qualified: {token!r}")
            if "other" in seen:
                raise AclError(f"duplicate ACL entry: {token!r}")
            seen.add("other")
            values["other"] = perms
        else:
            raise AclError(f"unknown ACL tag: {kind_raw!r}")

    missing = [name for name in ("user_obj", "group_obj", "other") if name not in values]
    if missing:
        raise AclError(f"ACL is missing required entries: {', '.join(missing)}")
    if default_flags == {True, False}:
        raise AclError("ACL text must not mix default: and access entries")
    return Acl(
        user_obj=values["user_obj"],
        group_obj=values["group_obj"],
        other=values["other"],
        mask=values.get("mask"),
        named_users=named_users,
        named_groups=named_groups,
    )


def create_acl(
    parent_default: Acl | None,
    mode: FileMode | int | str,
    umask: FileMode | int | str | None = None,
) -> Acl:
    """Derive the access ACL for a newly created file.

    With a parent default ACL the requested mode clips ``user::``,
    ``mask::`` (or ``group::`` for a minimal default ACL) and ``other::``;
    named entries and ``group::`` of the default ACL survive untouched, and
    the process umask is ignored (as ``acl(5)`` specifies).
    Without a default ACL the requested mode is reduced by the process umask
    and becomes the minimal access ACL.
    """
    if parent_default is not None and not isinstance(parent_default, Acl):
        raise AclError(f"parent default ACL must be an Acl or None, got {parent_default!r}")
    requested = _coerce_mode(mode)

    if parent_default is None:
        if umask is not None:
            mask = _coerce_mode(umask)
            requested = FileMode(
                owner=requested.owner & ~mask.owner,
                group=requested.group & ~mask.group,
                other=requested.other & ~mask.other,
            )
        return Acl(requested.owner, requested.group, requested.other)

    if parent_default.mask is not None:
        return Acl(
            user_obj=parent_default.user_obj & requested.owner,
            group_obj=parent_default.group_obj,
            other=parent_default.other & requested.other,
            mask=parent_default.mask & requested.group,
            named_users=dict(parent_default.named_users),
            named_groups=dict(parent_default.named_groups),
        )
    return Acl(
        user_obj=parent_default.user_obj & requested.owner,
        group_obj=parent_default.group_obj & requested.group,
        other=parent_default.other & requested.other,
        mask=None,
        named_users=dict(parent_default.named_users),
        named_groups=dict(parent_default.named_groups),
    )


@dataclass(frozen=True)
class Subject:
    """A caller/identity used for an access decision.

    ``uid`` is compared with the file owner and named user entries.  ``groups``
    holds supplementary groups and is compared with the file's owning group
    and named group entries.  ``primary_group`` is folded into ``groups`` and
    also supplies the owning group when the subject creates a file.

    ``label`` is only used for reporting; it defaults to the stringified uid.
    """

    uid: Hashable
    groups: Iterable[Hashable] = ()
    primary_group: Hashable | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        member_of = set(self.groups)
        if self.primary_group is not None:
            member_of.add(self.primary_group)
        object.__setattr__(self, "groups", frozenset(member_of))
        if self.label is None:
            object.__setattr__(self, "label", str(self.uid))
        elif not isinstance(self.label, str):
            object.__setattr__(self, "label", str(self.label))


@dataclass(frozen=True)
class Evaluation:
    """The rule that decided an access check and the resulting permissions."""

    rule: str
    raw: Perm
    effective: Perm
    matched_groups: frozenset[Hashable] = frozenset()
    mask: Perm | None = None


@dataclass
class AclFile:
    """A modelled file: owner, owning group, and access ACL."""

    owner: Hashable
    group: Hashable
    acl: Acl

    def evaluate(self, subject: Subject) -> Evaluation:
        if not isinstance(subject, Subject):
            raise AclError(f"subject must be a Subject, got {subject!r}")
        acl = self.acl
        limit = acl.mask if acl.mask is not None else FULL

        if subject.uid == self.owner:
            return Evaluation("owner", acl.user_obj, acl.user_obj)

        if subject.uid in acl.named_users:
            raw = acl.named_users[subject.uid]
            return Evaluation("named_user", raw, raw & limit, mask=acl.mask)

        matched = Perm.NONE
        hits: set[Hashable] = set()
        if self.group in subject.groups:
            hits.add(self.group)
            matched |= acl.group_obj
        for qualifier, perms in acl.named_groups.items():
            if qualifier in subject.groups:
                hits.add(qualifier)
                matched |= perms
        if hits:
            return Evaluation("group", matched, matched & limit, frozenset(hits), acl.mask)

        return Evaluation("other", acl.other, acl.other)

    def effective_permissions(self, subject: Subject) -> Perm:
        return self.evaluate(subject).effective

    def check_access(self, subject: Subject, requested: Perm | int | str) -> bool:
        wanted = _coerce_perm(
            Perm.from_text(requested) if isinstance(requested, str) else requested,
            "requested",
        )
        effective = self.effective_permissions(subject)
        return int(effective) & int(wanted) == int(wanted)

    def chmod(self, mode: FileMode | int | str) -> None:
        """Apply chmod semantics.

        The group-class bits go to ``mask::`` when the ACL has an extended
        mask; otherwise they go to ``group::``.  Owner and other bits always
        update ``user::`` and ``other::``.
        """
        requested = _coerce_mode(mode)
        acl = self.acl
        if acl.mask is not None:
            self.acl = replace(
                acl,
                user_obj=requested.owner,
                other=requested.other,
                mask=requested.group,
            )
        else:
            self.acl = replace(
                acl,
                user_obj=requested.owner,
                group_obj=requested.group,
                other=requested.other,
            )

    def observe(
        self, subjects: Mapping[str, Subject] | Iterable[Subject]
    ) -> dict[str, Perm]:
        """Return effective permissions for several subjects in one pass.

        Mapping input controls the labels; iterable input uses ``Subject.label``.
        Duplicate labels are rejected so a rehearsal cannot silently hide a
        subject.
        """
        if isinstance(subjects, Mapping):
            pairs = list(subjects.items())
        else:
            pairs = [(subject.label, subject) for subject in subjects]
        result: dict[str, Perm] = {}
        for label, subject in pairs:
            if not isinstance(subject, Subject):
                raise AclError(f"subject for {label!r} must be a Subject, got {subject!r}")
            if not isinstance(label, str):
                raise AclError(f"subject label must be a string, got {label!r}")
            if label in result:
                raise AclError(f"duplicate subject label: {label!r}")
            result[label] = self.effective_permissions(subject)
        return result


def create_file(
    parent_default: Acl | None,
    mode: FileMode | int | str,
    *,
    owner: Hashable,
    group: Hashable,
    umask: FileMode | int | str | None = None,
) -> AclFile:
    """Create a modelled file with an ACL derived from the parent default."""
    return AclFile(
        owner=owner,
        group=group,
        acl=create_acl(parent_default, mode, umask=umask),
    )


_DEMO_DEFAULT = (
    "user::rwx,user:alice:rwx,user:bob:rw-,group::r-x,group:devs:rwx,"
    "mask::rwx,other::---"
)


def _demo_subjects() -> dict[str, Subject]:
    return {
        "owner": Subject("root"),
        "alice": Subject("alice"),
        "bob": Subject("bob"),
        "carol": Subject("carol", groups=["staff"]),
        "dave": Subject("dave", groups=["devs"]),
        "eve": Subject("eve", groups=["visitors"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="acl_model",
        description="Rehearse ACL inheritance and effective permissions for a new file.",
    )
    parser.add_argument("--default-acl", default=_DEMO_DEFAULT, help="parent default ACL text")
    parser.add_argument(
        "--no-default-acl",
        action="store_true",
        help="model a parent without a default ACL (process umask applies)",
    )
    parser.add_argument("--umask", default="022", help="process umask for the no-default case")
    parser.add_argument("--mode", default="750", help="creation request mode (octal)")
    parser.add_argument("--chmod", default=None, help="optional later chmod mode (octal)")
    args = parser.parse_args(argv)

    try:
        if args.no_default_acl:
            parent = None
            file = create_file(
                None, args.mode, owner="root", group="staff", umask=args.umask
            )
        else:
            parent = parse_acl(args.default_acl)
            file = create_file(parent, args.mode, owner="root", group="staff")
    except AclError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if parent is None:
        print(f"Parent has no default ACL; process umask {args.umask} applies.")
    else:
        print("Parent default ACL:")
        print(parent.format(default=True))
    print()
    print(f"Created ACL (requested mode {args.mode}):")
    print(file.acl.format())
    print()

    def show(title: str) -> None:
        print(title)
        print(f"{'subject':<8} {'rule':<11} {'effective':<9}")
        for label, subject in _demo_subjects().items():
            evaluation = file.evaluate(subject)
            print(f"{label:<8} {evaluation.rule:<11} {evaluation.effective.to_text():<9}")
        print()

    show("Effective permissions after creation:")
    if args.chmod is not None:
        try:
            file.chmod(args.chmod)
        except AclError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(f"ACL after chmod {args.chmod}:")
        print(file.acl.format())
        print()
        show(f"Effective permissions after chmod {args.chmod}:")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
