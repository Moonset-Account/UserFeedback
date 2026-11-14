"""Subject/group relationship expansion.

Group membership is transitive (groups may nest).  A subject matches an
ACE when the ACE names the subject itself or any group that
(transitively) contains the subject.
"""

from typing import FrozenSet, Set

from .models import SubjectDirectory
from .io_utils import load_json


def load_subject_directory(path: str) -> SubjectDirectory:
    return SubjectDirectory.from_dict(load_json(path))


def expand_members(subject_id: str, directory: SubjectDirectory) -> FrozenSet[str]:
    """Return the subject id plus every group (directly or transitively)
    that contains it.

    The result always contains the subject itself so account-level ACEs
    match.  Members of a group are deliberately not included: an ACE
    granted to a group only matches that group's members, never the
    reverse.
    """

    result: Set[str] = {subject_id}
    todo = [subject_id]
    while todo:
        current = todo.pop()
        for group_id, info in directory.groups.items():
            if group_id in result:
                continue
            if current in info["members"]:
                result.add(group_id)
                todo.append(group_id)
    return frozenset(result)


def is_privileged(subject_id: str, directory: SubjectDirectory) -> bool:
    """A subject is privileged when its own account record says so."""

    info = directory.accounts.get(subject_id)
    return bool(info and info.get("privileged"))
