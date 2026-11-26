"""Recursive de-identification of JSON represented DICOM attribute trees.

Supported JSON shapes
---------------------
1. **Standard DICOM JSON model** (primary)::

       {"0020000D": {"vr": "UI", "Value": ["1.2.3"]},
        "00081115": {"vr": "SQ", "Value": [ { ...item... } ]}}

2. **Keyword / simplified values**::

       {"StudyInstanceUID": "1.2.3",
        "ReferencedSequence": [{"ReferencedSOPInstanceUID": "1.2.4"}]}

3. **Explicit nodes** using ``type`` / ``value`` / ``items`` /
   ``children`` wrappers.

The transform never mutates its input.  Sequence items are recursed into
at arbitrary depth.  Private attributes that the dictionary does not
approve are dropped together with their complete content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .dictionary import FieldDictionary, FieldEntry, normalize_vr
from .uids import UidMapper

_VALUE_CONTAINERS = ("Value", "value", "values")
_ITEM_CONTAINERS = ("Value", "value", "items", "Items", "children", "Children")
_UID_DIRECT_KEYS = ("uid", "UID", "Uid")

# Sentinel telling container transforms to drop an element/entry.
_REMOVE = object()

# Snake / kebab case "..._uid" or exact "uid", plus Pascal/camel names
# ending in upper-case "UID".  Deliberately does NOT match e.g. "guid".
_UID_NAME_RE = re.compile(r"(?:^|[\s_\-.])uid$", re.IGNORECASE)


def looks_like_uid_name(key: str) -> bool:
    if not isinstance(key, str):
        return False
    if key in {"uid", "UID", "Uid"}:
        return True
    if key.endswith("UID"):
        # DICOM keywords treat "UID" as a distinct PascalCase suffix, so
        # the preceding character is normally lower case (InstanceUID).
        # Reject all-caps acronyms such as "GUID" / "UUID".
        prefix = key[:-3]
        if prefix and not prefix[-1].isupper():
            return True
    if _UID_NAME_RE.search(key):
        return True
    return False


@dataclass
class ReportEvent:
    kind: str
    path: str
    detail: Dict[str, Any] = field(default_factory=dict)


class ProcessReport:
    """Collects what a processing run did (useful for audits/tests)."""

    def __init__(self) -> None:
        self.events: List[ReportEvent] = []

    def add(self, kind: str, path: str, **detail: Any) -> None:
        self.events.append(ReportEvent(kind, path, detail))

    def _paths(self, kind: str) -> List[str]:
        return [e.path for e in self.events if e.kind == kind]

    @property
    def replaced(self) -> List[str]:
        return self._paths("uid_replaced")

    @property
    def constants_kept(self) -> List[str]:
        return self._paths("constant_kept")

    @property
    def private_removed(self) -> List[str]:
        return self._paths("private_removed")

    @property
    def approved_private_kept(self) -> List[str]:
        return self._paths("private_approved")

    def counts(self) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for event in self.events:
            result[event.kind] = result.get(event.kind, 0) + 1
        return result

    def as_dict(self) -> Dict[str, Any]:
        return {
            "counts": self.counts(),
            "events": [
                {"kind": e.kind, "path": e.path, **e.detail} for e in self.events
            ],
        }


def _node_vr(node: Dict[str, Any]) -> Optional[str]:
    for key in ("vr", "VR", "Vr"):
        value = node.get(key)
        if isinstance(value, str):
            normalized = normalize_vr(value)
            if normalized is not None:
                return normalized
    type_value = node.get("type") or node.get("data_type") or node.get("value_type")
    if isinstance(type_value, str):
        normalized = normalize_vr(type_value)
        if normalized is not None:
            has_payload = any(
                k in node for k in ("Value", "value", "values", "items", "children", "uid")
            )
            if has_payload:
                return normalized
    return None


def _looks_like_node(value: Dict[str, Any]) -> bool:
    if any(k in value for k in ("vr", "VR", "Vr")):
        return True
    if _node_vr(value) is not None:
        return True
    if "tag" in value and any(
        k in value for k in ("Value", "value", "items", "children", "uid")
    ):
        return True
    return False


class TreeProcessor:
    """Stateful processor bound to a dictionary, mapper and project."""

    def __init__(
        self,
        dictionary: Optional[FieldDictionary] = None,
        mapper: Optional[UidMapper] = None,
        *,
        project: Optional[str] = None,
        report: Optional[ProcessReport] = None,
        name_heuristics: bool = True,
    ) -> None:
        self.dictionary = dictionary or FieldDictionary()
        self.mapper = mapper or UidMapper()
        self.project = project
        self.report = report or ProcessReport()
        self.name_heuristics = name_heuristics

    # -------------------------------------------------------------- entries
    def _entry_for(self, key: Any) -> Optional[FieldEntry]:
        if self.dictionary is None or not isinstance(key, str):
            return None
        return self.dictionary.lookup(key)

    def _key_is_uid(self, key: Any, entry: Optional[FieldEntry], vr: Optional[str]) -> bool:
        if vr == "UI":
            return True
        if vr is not None:
            # The attribute/node declares an explicit non UID VR.
            return False
        if entry is not None and entry.is_uid:
            return True
        if self.name_heuristics and isinstance(key, str) and looks_like_uid_name(key):
            return True
        return False

    def _key_is_constant_field(self, entry: Optional[FieldEntry]) -> bool:
        return entry is not None and entry.is_constant_field

    # ------------------------------------------------------------- UID value
    def _map_one_uid(self, value: Any, path: str, entry: Optional[FieldEntry]) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            original = value
            stripped = value.strip()
            if stripped == "":
                return value
            # Field-level constant declaration wins.
            if self._key_is_constant_field(entry):
                self.report.add("constant_kept", path, uid=stripped, reason="dictionary_role")
                return value
            # Field entry may enumerate the exact constant values it owns.
            if entry is not None and stripped in entry.constants:
                self.report.add("constant_kept", path, uid=stripped, reason="field_constant")
                return value
            # Global registered constants (transfer syntax registry plus
            # dictionary/gateway supplied values).
            if stripped in self.mapper.preserved_uids or (
                self.dictionary is not None
                and self.dictionary.is_registered_constant_value(stripped)
            ):
                self.mapper.preserved_uids.add(stripped)
                self.report.add("constant_kept", path, uid=stripped, reason="registered")
                return value
            pseudo = self.mapper.map_uid(stripped, self.project)
            if pseudo != stripped:
                self.report.add(
                    "uid_replaced", path, original=stripped, pseudonym=pseudo,
                    project=UidMapper.normalize_project(self.project),
                )
            return pseudo
        if isinstance(value, list):
            return [self._map_one_uid(item, f"{path}[{i}]", entry)
                    for i, item in enumerate(value)]
        if isinstance(value, dict):
            return self._map_uid_wrapper(value, path, entry)
        # Numbers / bools are not valid UID payloads; leave untouched.
        return value

    def _map_uid_wrapper(
        self, node: Dict[str, Any], path: str, entry: Optional[FieldEntry]
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in node.items():
            if key in _VALUE_CONTAINERS:
                out[key] = self._map_one_uid(value, f"{path}.{key}", entry)
            elif key in _UID_DIRECT_KEYS:
                out[key] = self._map_one_uid(value, f"{path}.{key}", entry)
            else:
                out[key] = value
        return out

    # ---------------------------------------------------------------- nodes
    def _transform_node(
        self, node: Dict[str, Any], path: str, key: Any,
        entry: Optional[FieldEntry],
    ) -> Dict[str, Any]:
        vr = _node_vr(node) or (entry.vr if entry is not None else None)
        out: Dict[str, Any] = dict(node)  # shallow copy; input not mutated

        if vr == "UI" or (vr is None and self._key_is_uid(key, entry, None)):
            if self._key_is_constant_field(entry):
                self.report.add("constant_kept", path, reason="dictionary_role")
                return out
            for container in _VALUE_CONTAINERS:
                if container in node:
                    out[container] = self._map_one_uid(
                        node[container], f"{path}.{container}", entry
                    )
            for container in _UID_DIRECT_KEYS:
                if container in node:
                    out[container] = self._map_one_uid(
                        node[container], f"{path}.{container}", entry
                    )
            return out

        if vr == "SQ" or (entry is not None and entry.is_sequence):
            for container in _ITEM_CONTAINERS:
                if container in node:
                    out[container] = self.transform(node[container], f"{path}.{container}")
            return out

        # Unknown / primitive VR: walk members generically so nested
        # attributes are still discovered in non standard wrappers.
        return self._transform_map(node, path, structural=True)

    # ------------------------------------------------------------------ maps
    def _private_decision(
        self, key: Any, entry: Optional[FieldEntry], path: str
    ) -> str:
        """Return 'remove', 'keep_approved' or 'normal'."""

        if not isinstance(key, str):
            return "normal"
        if not self.dictionary.is_private(key, entry):
            return "normal"
        if self.dictionary.is_approved_private(key, entry):
            self.report.add("private_approved", path, key=key)
            return "keep_approved"
        self.report.add("private_removed", path, key=key)
        return "remove"

    def _transform_map(
        self, obj: Dict[str, Any], path: str, *, structural: bool = False
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path != "$" else f"$.{key}"
            entry = self._entry_for(key)

            if not structural:
                decision = self._private_decision(key, entry, child_path)
                if decision == "remove":
                    # Attribute and its whole content are dropped.
                    continue

            out[key] = self._transform_entry(key, value, child_path, entry)
        return out

    def _transform_entry(
        self, key: Any, value: Any, path: str, entry: Optional[FieldEntry]
    ) -> Any:
        # Standard / explicit node wrapper.
        if isinstance(value, dict) and _looks_like_node(value):
            return self._transform_node(value, path, key, entry)

        # Bare UID typed field (scalar, list or wrapper without vr).
        node_vr = _node_vr(value) if isinstance(value, dict) else None
        if self._key_is_uid(key, entry, node_vr):
            if isinstance(value, dict):
                return self._map_uid_wrapper(value, path, entry)
            return self._map_one_uid(value, path, entry)

        # Bare sequence declared by the dictionary.
        if entry is not None and entry.is_sequence:
            return self.transform(value, path)

        return self.transform(value, path)

    # --------------------------------------------------------------- public
    def transform(self, obj: Any, path: str = "$") -> Any:
        if isinstance(obj, dict):
            if _looks_like_node(obj):
                # A node may appear at the root or inside a list; derive
                # field context from its own tag/keyword when possible.
                identity = obj.get("tag") or obj.get("keyword") or obj.get("name")
                entry = self._entry_for(identity) if identity is not None else None
                if entry is None and identity is not None:
                    entry = self._entry_for(str(identity))
                # Self identifying nodes inside lists have no parent key
                # privacy check, so apply approval here.
                if identity is not None and self.dictionary.is_private(
                    str(identity), entry
                ):
                    if not self.dictionary.is_approved_private(str(identity), entry):
                        self.report.add("private_removed", path, key=str(identity))
                        return _REMOVE
                    self.report.add("private_approved", path, key=str(identity))
                return self._transform_node(obj, path, identity, entry)
            return self._transform_map(obj, path)
        if isinstance(obj, list):
            result = []
            for i, item in enumerate(obj):
                transformed = self.transform(item, f"{path}[{i}]")
                if transformed is _REMOVE:
                    continue
                result.append(transformed)
            return result
        return obj


def process_tree(
    tree: Any,
    dictionary: Union[FieldDictionary, dict, str, Path, None] = None,
    mapper: Optional[UidMapper] = None,
    *,
    project: Optional[str] = None,
    report: Optional[ProcessReport] = None,
    name_heuristics: bool = True,
) -> Any:
    """De-identify *tree* and return a new transformed tree.

    ``tree`` may be a parsed JSON object/list or a JSON document string
    (object or array).  ``dictionary`` may be a :class:`FieldDictionary`,
    a plain JSON-like dict/list, a JSON dictionary string or a path to a
    JSON dictionary file.
    """

    if isinstance(tree, (str, bytes)):
        if isinstance(tree, bytes):
            tree = tree.decode("utf-8")
        if tree.lstrip()[:1] in {"{", "["}:
            tree = json.loads(tree)
    if not isinstance(dictionary, FieldDictionary):
        dictionary = FieldDictionary.from_dict(dictionary)
    processor = TreeProcessor(
        dictionary=dictionary,
        mapper=mapper,
        project=project,
        report=report,
        name_heuristics=name_heuristics,
    )
    result = processor.transform(tree)
    return {} if result is _REMOVE else result


# Verbose / descriptive aliases.
process_attribute_tree = process_tree
deidentify_tree = process_tree
process_json_tree = process_tree
process_dicom = process_tree
deidentify_dicom = process_tree
