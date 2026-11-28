"""测试用合成目录构造辅助（不参与 unittest 发现，文件名不带 test_ 前缀）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from key_coverage.model import (
    Alias,
    DirectorySnapshot,
    DirectoryStore,
    Group,
    KeyRecord,
    MemberKind,
    MemberRef,
    Person,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)


def at(hour: int, minute: int = 0, day: int = 28) -> datetime:
    """以 2026-11-<day> HH:MM UTC 构造带时区时间。"""
    return datetime(2026, 11, day, hour, minute, 0, tzinfo=UTC)


def ref(target_id: str, kind: MemberKind, min_level: Optional[str] = None) -> MemberRef:
    return MemberRef(target_id=target_id, kind=kind, min_level=min_level)


def group(group_id: str, members, name: Optional[str] = None) -> Group:
    return Group(id=group_id, name=name or group_id, members=tuple(members))


def alias(alias_id: str, target_id: str, kind: MemberKind) -> Alias:
    return Alias(id=alias_id, target_id=target_id, kind=kind)


def key(key_id: str, valid_from=None, valid_to=None) -> KeyRecord:
    return KeyRecord(key_id=key_id, valid_from=valid_from, valid_to=valid_to)


def person(person_id: str, keys=(), name: Optional[str] = None) -> Person:
    return Person(id=person_id, name=name or person_id, keys=tuple(keys))


def build_snapshot(version, *, groups=(), aliases=(), persons=()) -> DirectorySnapshot:
    return DirectorySnapshot(
        version=version,
        groups={g.id: g for g in groups},
        aliases={a.id: a for a in aliases},
        persons={p.id: p for p in persons},
    )


def build_store(*snapshots: DirectorySnapshot) -> DirectoryStore:
    return DirectoryStore({s.version: s for s in snapshots})


def time_offset(**kwargs) -> timedelta:
    return timedelta(**kwargs)
