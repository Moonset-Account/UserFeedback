"""Localised confidential redaction for a shared, acyclic document object graph.

The document model deliberately keeps content in reusable *text objects*.  An
*instance* node references a text object, and any number of pages may reach the
same instance or text object.  Redacting one instance therefore uses copy-on-
write along the page's reference path, so every other page keeps its original,
still-editable object.

Character offsets are Unicode code points (``str`` indices in Python), not UTF-16
code units and not bytes.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .errors import DocError, RedactionError

FORMAT = "docredact/1"

TEXT = "text"
CONTAINER = "container"
INSTANCE = "instance"
KINDS = frozenset({TEXT, CONTAINER, INSTANCE})

PathLike = Sequence[str] | str


def normalize_path(path: PathLike) -> tuple[str, ...]:
    """Return a path as a tuple of child keys.

    A dotted string (``"body.title"``) is accepted as a convenience; a sequence
    must contain non-empty strings.  An empty sequence addresses the page root
    itself, which is only useful when a page root is an instance.
    """

    if isinstance(path, str):
        steps = tuple(path.split("."))
    elif isinstance(path, Sequence) and not isinstance(path, (bytes, bytearray)):
        steps = tuple(path)
    else:
        raise RedactionError(f"path must be a string or a sequence, got {type(path).__name__}")

    for step in steps:
        if not isinstance(step, str) or step == "":
            raise RedactionError(f"path steps must be non-empty strings, got {step!r}")
    return steps


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RedactionError(f"{name} must be an integer, got {value!r}")
    return value


def _delete_range(text_node: Mapping[str, Any], start: int, end: int) -> tuple[str, list[dict], list[dict], str]:
    """Delete ``text[start:end]`` and remap speech segments.

    A speech segment is removed as a whole when ``[seg_start, seg_end)``
    intersects the deleted range.  Segments fully after it shift left by the
    number of deleted code points; segments fully before it are untouched.
    Adjacent segments (touching but not overlapping) are untouched.
    """

    text = text_node["text"]
    removed_text = text[start:end]
    shift = end - start
    new_text = text[:start] + text[end:]

    kept: list[dict] = []
    removed: list[dict] = []
    for segment in text_node.get("speech", []):
        seg_start = segment["start"]
        seg_end = segment["end"]
        if seg_start < end and seg_end > start:
            removed.append(dict(segment))
        elif seg_start >= end:
            kept.append({**segment, "start": seg_start - shift, "end": seg_end - shift})
        else:
            kept.append(dict(segment))
    return new_text, kept, removed, removed_text


@dataclass(frozen=True)
class RedactionResult:
    """Outcome of one :meth:`Document.redact` call."""

    document: "Document"
    page: str
    path: tuple[str, ...]
    start: int
    end: int
    removed_text: str
    removed_speech: tuple[dict, ...]
    old_text_id: str
    new_text_id: str
    old_instance_id: str
    new_instance_id: str


class Document:
    """A validated, acyclic document object graph with pages.

    ``objects`` maps object ids to nodes:

    * ``{"kind": "text", "text": str, "speech": [{"start": int, "end": int,
      "utterance": str}, ...]}``
    * ``{"kind": "container", "children": {key: object_id}}``
    * ``{"kind": "instance", "ref": object_id}``

    ``pages`` maps page names to ``{"root": object_id, "public": bool}``.
    """

    def __init__(self, objects: Mapping[str, Mapping[str, Any]], pages: Mapping[str, Mapping[str, Any]]):
        if not isinstance(objects, Mapping) or not isinstance(pages, Mapping):
            raise DocError("objects and pages must be mappings")
        self.objects: dict[str, dict[str, Any]] = copy.deepcopy(dict(objects))
        self.pages: dict[str, dict[str, Any]] = copy.deepcopy(dict(pages))
        self.validate()

    # ------------------------------------------------------------------
    # validation and construction
    # ------------------------------------------------------------------
    def validate(self) -> None:
        if not self.objects:
            raise DocError("document must contain at least one object")
        for object_id, node in self.objects.items():
            if not isinstance(object_id, str) or not object_id:
                raise DocError(f"object ids must be non-empty strings, got {object_id!r}")
            self._validate_node(object_id, node)
        if not self.pages:
            raise DocError("document must contain at least one page")
        for page, record in self.pages.items():
            if not isinstance(page, str) or not page:
                raise DocError(f"page names must be non-empty strings, got {page!r}")
            if not isinstance(record, dict):
                raise DocError(f"page {page!r} must be an object")
            root = record.get("root")
            if not isinstance(root, str) or root not in self.objects:
                raise DocError(f"page {page!r} has an unknown root {root!r}")
            if "public" in record and not isinstance(record["public"], bool):
                raise DocError(f"page {page!r} has a non-boolean public flag")
        self._reject_cycles()

    def _validate_node(self, object_id: str, node: Any) -> None:
        if not isinstance(node, dict):
            raise DocError(f"object {object_id!r} must be an object")
        kind = node.get("kind")
        if kind not in KINDS:
            raise DocError(f"object {object_id!r} has unsupported kind {kind!r}")
        if kind == TEXT:
            text = node.get("text")
            if not isinstance(text, str):
                raise DocError(f"text object {object_id!r} needs a string 'text'")
            speech = node.get("speech", [])
            if not isinstance(speech, list):
                raise DocError(f"text object {object_id!r} needs a list 'speech'")
            for index, segment in enumerate(speech):
                if not isinstance(segment, dict):
                    raise DocError(f"speech segment {index} of {object_id!r} must be an object")
                start = segment.get("start")
                end = segment.get("end")
                if isinstance(start, bool) or not isinstance(start, int):
                    raise DocError(f"speech segment {index} of {object_id!r} has a non-integer start")
                if isinstance(end, bool) or not isinstance(end, int):
                    raise DocError(f"speech segment {index} of {object_id!r} has a non-integer end")
                if not (0 <= start < end <= len(text)):
                    raise DocError(
                        f"speech segment {index} of {object_id!r} has an invalid range "
                        f"[{start}, {end}) for text of length {len(text)}"
                    )
                if not isinstance(segment.get("utterance"), str):
                    raise DocError(f"speech segment {index} of {object_id!r} needs a string 'utterance'")
        elif kind == CONTAINER:
            children = node.get("children")
            if not isinstance(children, dict):
                raise DocError(f"container {object_id!r} needs a 'children' mapping")
            for key, child in children.items():
                if not isinstance(key, str) or not key:
                    raise DocError(f"container {object_id!r} has an invalid child key {key!r}")
                if not isinstance(child, str) or child not in self.objects:
                    raise DocError(f"container {object_id!r} references unknown object {child!r}")
        else:  # INSTANCE
            ref = node.get("ref")
            if not isinstance(ref, str) or ref not in self.objects:
                raise DocError(f"instance {object_id!r} references unknown object {ref!r}")

    def _edges(self, object_id: str) -> Iterable[str]:
        node = self.objects[object_id]
        if node["kind"] == CONTAINER:
            return node["children"].values()
        if node["kind"] == INSTANCE:
            return (node["ref"],)
        return ()

    def _reject_cycles(self) -> None:
        indegree = {object_id: 0 for object_id in self.objects}
        for object_id in self.objects:
            for child in self._edges(object_id):
                indegree[child] += 1
        ready = [object_id for object_id, degree in indegree.items() if degree == 0]
        seen = 0
        while ready:
            current = ready.pop()
            seen += 1
            for child in self._edges(current):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if seen != len(self.objects):
            raise DocError("document object graph contains a cycle")

    @classmethod
    def from_dict(cls, data: Any) -> "Document":
        if not isinstance(data, dict):
            raise DocError("document must be a JSON object")
        if "format" in data and data["format"] != FORMAT:
            raise DocError(f"unsupported document format {data['format']!r}")
        objects = data.get("objects")
        pages = data.get("pages")
        if not isinstance(objects, dict) or not isinstance(pages, dict):
            raise DocError("document needs 'objects' and 'pages' mappings")
        return cls(objects, pages)

    @classmethod
    def load(cls, path: str | Path) -> "Document":
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise DocError(f"cannot load document from {path}: {exc}") from exc
        return cls.from_dict(data)

    def clone(self) -> "Document":
        return Document(copy.deepcopy(self.objects), copy.deepcopy(self.pages))

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "objects": copy.deepcopy(self.objects),
            "pages": copy.deepcopy(self.pages),
        }

    # ------------------------------------------------------------------
    # inspection
    # ------------------------------------------------------------------
    def _page_root(self, page: str) -> str:
        try:
            record = self.pages[page]
        except KeyError:
            raise RedactionError(f"unknown page {page!r}") from None
        return record["root"]

    def _resolve_path(self, page: str, path: PathLike) -> tuple[str, list[tuple[str, str]]]:
        steps = normalize_path(path)
        node_id = self._page_root(page)
        ancestors: list[tuple[str, str]] = []
        for step in steps:
            node = self.objects[node_id]
            if node["kind"] != CONTAINER:
                raise RedactionError(
                    f"path step {step!r} traverses {node['kind']} object {node_id!r}, not a container"
                )
            children = node["children"]
            if step not in children:
                raise RedactionError(
                    f"path step {step!r} is missing from container {node_id!r}"
                )
            ancestors.append((node_id, step))
            node_id = children[step]
        node = self.objects[node_id]
        if node["kind"] != INSTANCE:
            raise RedactionError(f"path {list(steps)!r} ends at {node['kind']} object {node_id!r}, not an instance")
        return node_id, ancestors

    def resolve_instance(self, page: str, path: PathLike) -> str:
        """Return the instance object id selected by ``path`` on ``page``."""

        return self._resolve_path(page, path)[0]

    def iter_instances(self, page: str) -> Iterator[tuple[tuple[str, ...], str]]:
        """Yield ``(path, instance_id)`` for every instance reachable from ``page``."""

        root = self._page_root(page)
        stack: list[tuple[tuple[str, ...], str]] = [((), root)]
        while stack:
            prefix, node_id = stack.pop()
            node = self.objects[node_id]
            kind = node["kind"]
            if kind == INSTANCE:
                yield prefix, node_id
            elif kind == CONTAINER:
                for key, child in reversed(list(node["children"].items())):
                    stack.append((prefix + (key,), child))

    def instances_referencing(self, text_id: str) -> list[tuple[str, tuple[str, ...], str]]:
        """Return ``(page, path, instance_id)`` for every instance using ``text_id``."""

        found = []
        for page in self.pages:
            for path, instance_id in self.iter_instances(page):
                if self.objects[instance_id].get("ref") == text_id:
                    found.append((page, path, instance_id))
        return found

    def iter_text_occurrences(self, page: str) -> Iterator[tuple[str, dict[str, Any]]]:
        root = self._page_root(page)
        stack: list[str] = [root]
        while stack:
            node_id = stack.pop()
            node = self.objects[node_id]
            kind = node["kind"]
            if kind == TEXT:
                yield node_id, node
            elif kind == CONTAINER:
                stack.extend(reversed(list(node["children"].values())))
            else:
                stack.append(node["ref"])

    def page_text(self, page: str) -> str:
        return "".join(node["text"] for _, node in self.iter_text_occurrences(page))

    def page_speech(self, page: str) -> list[dict[str, Any]]:
        result = []
        for object_id, node in self.iter_text_occurrences(page):
            for segment in node.get("speech", []):
                result.append({"object": object_id, **segment})
        return result

    # ------------------------------------------------------------------
    # content operations
    # ------------------------------------------------------------------
    def _text_node(self, object_id: str) -> dict[str, Any]:
        try:
            node = self.objects[object_id]
        except KeyError:
            raise RedactionError(f"unknown object {object_id!r}") from None
        if node["kind"] != TEXT:
            raise RedactionError(f"object {object_id!r} is a {node['kind']}, not text")
        return node

    def redact(self, page: str, path: PathLike, start: int, end: int) -> RedactionResult:
        """Remove ``[start, end)`` from the instance selected by ``path`` on ``page``.

        The receiver is not mutated: a new document is returned in which only
        the objects on the targeted reference path are copied.  Shared text
        objects used by other pages remain untouched.
        """

        steps = normalize_path(path)
        instance_id, ancestors = self._resolve_path(page, steps)
        instance = self.objects[instance_id]
        text_id = instance["ref"]
        text_node = self._text_node(text_id)

        start = _require_int(start, "start")
        end = _require_int(end, "end")
        length = len(text_node["text"])
        if start < 0 or end > length:
            raise RedactionError(f"range [{start}, {end}) is outside text of length {length}")
        if start >= end:
            raise RedactionError(f"range [{start}, {end}) is empty or reversed")

        new_text, new_speech, removed_speech, removed_text = _delete_range(text_node, start, end)

        document = self.clone()
        new_text_id = document._alloc_id(text_id, "redacted")
        new_instance_id = document._alloc_id(instance_id, "redacted")
        document.objects[new_text_id] = {**text_node, "text": new_text, "speech": new_speech}
        document.objects[new_instance_id] = {**instance, "ref": new_text_id}

        child_id = new_instance_id
        for parent_id, key in reversed(ancestors):
            parent = document.objects[parent_id]
            new_parent_id = document._alloc_id(parent_id, "redacted")
            children = dict(parent["children"])
            children[key] = child_id
            document.objects[new_parent_id] = {**parent, "children": children}
            child_id = new_parent_id

        document.pages[page] = {**document.pages[page], "root": child_id}
        document.validate()

        return RedactionResult(
            document=document,
            page=page,
            path=steps,
            start=start,
            end=end,
            removed_text=removed_text,
            removed_speech=tuple(removed_speech),
            old_text_id=text_id,
            new_text_id=new_text_id,
            old_instance_id=instance_id,
            new_instance_id=new_instance_id,
        )

    def _alloc_id(self, base: str, suffix: str) -> str:
        candidate = f"{base}~{suffix}"
        index = 1
        while candidate in self.objects:
            candidate = f"{base}~{suffix}#{index}"
            index += 1
        return candidate

    def edit_text(self, object_id: str, start: int, end: int, replacement: str) -> list[dict[str, Any]]:
        """Edit a text object in place; every instance referencing it sees the change.

        Speech coordinates follow the edit.  A segment whose source range is
        touched by the edit can no longer be attributed to a single source
        range, so it is dropped whole (the same rule :meth:`redact` uses).  A
        pure insertion between segments merely shifts the later ones.  Returns
        the dropped segments.
        """

        node = self._text_node(object_id)
        if not isinstance(replacement, str):
            raise RedactionError("replacement must be a string")
        start = _require_int(start, "start")
        end = _require_int(end, "end")
        length = len(node["text"])
        if start < 0 or end > length:
            raise RedactionError(f"range [{start}, {end}) is outside text of length {length}")
        if start > end:
            raise RedactionError(f"range [{start}, {end}) is reversed")

        delta = len(replacement) - (end - start)
        new_text = node["text"][:start] + replacement + node["text"][end:]

        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for segment in node.get("speech", []):
            if start == end:
                touches = segment["start"] < start < segment["end"]
            else:
                touches = segment["start"] < end and segment["end"] > start
            if touches:
                dropped.append(dict(segment))
            elif segment["start"] >= end:
                kept.append({**segment, "start": segment["start"] + delta, "end": segment["end"] + delta})
            else:
                kept.append(dict(segment))

        previous_text = node["text"]
        previous_speech = node.get("speech", [])
        node["text"] = new_text
        node["speech"] = kept
        try:
            self.validate()
        except DocError as exc:
            node["text"] = previous_text
            node["speech"] = previous_speech
            raise RedactionError(f"edit would leave invalid speech coordinates: {exc}") from exc
        return dropped

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------
    def reachable_objects(self, public_only: bool = True) -> set[str]:
        roots = []
        for record in self.pages.values():
            if public_only and not record.get("public", True):
                continue
            roots.append(record["root"])
        seen: set[str] = set()
        stack = list(roots)
        while stack:
            node_id = stack.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            stack.extend(self._edges(node_id))
        return seen

    def export(self, public_only: bool = True) -> dict[str, Any]:
        """Serialize only objects reachable from the selected pages.

        With ``public_only`` (the default) any page marked ``"public": false``
        and everything exclusively reachable through it is omitted, as are
        orphan objects.  Object and page ids are sorted so the output is
        deterministic.
        """

        keep = self.reachable_objects(public_only=public_only)
        objects = {object_id: copy.deepcopy(self.objects[object_id]) for object_id in sorted(keep)}
        pages = {
            page: copy.deepcopy(record)
            for page, record in sorted(self.pages.items())
            if record.get("public", True) or not public_only
        }
        return {"format": FORMAT, "objects": objects, "pages": pages}

    def export_to(self, path: str | Path, public_only: bool = True) -> dict[str, Any]:
        data = self.export(public_only=public_only)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
        return data


def _as_document(document: Document | Mapping[str, Any]) -> Document:
    return document if isinstance(document, Document) else Document.from_dict(document)


def extract_page_text(document: Document | Mapping[str, Any], page: str) -> str:
    """Re-extract the plain text of ``page`` from a (possibly exported) document."""

    return _as_document(document).page_text(page)


def extract_page_speech(document: Document | Mapping[str, Any], page: str) -> list[dict[str, Any]]:
    """Re-extract the speech segments of ``page``, including source objects."""

    return _as_document(document).page_speech(page)


__all__ = [
    "CONTAINER",
    "Document",
    "FORMAT",
    "INSTANCE",
    "KINDS",
    "RedactionResult",
    "TEXT",
    "extract_page_speech",
    "extract_page_text",
    "normalize_path",
]
