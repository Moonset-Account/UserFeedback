"""Loading the initial tree description and the event stream."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .model import (
    DIR,
    FILE,
    ROOT_ID,
    InvalidEventError,
    Namespace,
    check_name,
)


def build_initial_namespace(spec: Dict[str, Any]) -> Namespace:
    """Build the sequence-0 namespace from a declarative tree description.

    Expected shape::

        {
          "objects": {"root": "dir", "d1": "dir", "f1": "file"},
          "entries": [
            {"dir": "root", "name": "proj", "id": "d1"},
            {"dir": "d1", "name": "a.txt", "id": "f1"}
          ]
        }

    ``objects`` is optional: any directory that is only used as a parent
    and any file that is only used as a child is inferred.  The root object
    must be present (or default to ``root``/``dir``).
    """

    if not isinstance(spec, dict):
        raise InvalidEventError("initial tree must be a JSON object")

    declared = spec.get("objects", {})
    if not isinstance(declared, dict):
        raise InvalidEventError("'objects' must be an object")

    kind: Dict[str, str] = {}
    for obj, obj_kind in declared.items():
        if obj_kind not in (FILE, DIR):
            raise InvalidEventError(
                "object %r has unknown kind %r" % (obj, obj_kind)
            )
        kind[obj] = obj_kind

    kind.setdefault(ROOT_ID, DIR)
    if kind[ROOT_ID] != DIR:
        raise InvalidEventError("the root object must be a directory")

    raw_entries = spec.get("entries", [])
    if not isinstance(raw_entries, list):
        raise InvalidEventError("'entries' must be a list")

    # Any id that is used as a parent is inferred to be a directory before
    # children get their default FILE kind; otherwise an id that appears
    # first as a child and later as a parent would be mistyped.
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise InvalidEventError("each entry must be a JSON object")
        parent = raw.get("dir")
        if isinstance(parent, str):
            kind.setdefault(parent, DIR)

    entries: Dict[str, Dict[str, str]] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise InvalidEventError("each entry must be a JSON object")
        for key in ("dir", "name", "id"):
            if key not in raw:
                raise InvalidEventError(
                    "entry %r is missing field %r" % (raw, key)
                )
        parent = raw["dir"]
        name = raw["name"]
        child = raw["id"]
        check_name(name)
        if parent not in kind:
            kind[parent] = DIR
        if kind[parent] != DIR:
            raise InvalidEventError(
                "entry parent %r is not a directory" % (parent,)
            )
        if child not in kind:
            kind[child] = FILE
        bucket = entries.setdefault(parent, {})
        if name in bucket:
            raise InvalidEventError(
                "duplicate entry %r in %r" % (name, parent)
            )
        bucket[name] = child

    return Namespace(kind, entries)


def load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_events(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict) and "events" in data:
        data = data["events"]
    if not isinstance(data, list):
        raise InvalidEventError("event stream must be a JSON list")
    return data
