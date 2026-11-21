"""Parsing a plain-JSON trace into model objects.

The schema is intentionally small so it can be produced by any capture
tool.  It is documented in ``README.md``; :func:`load_trace` accepts the
already-decoded Python object (``json.loads`` the file yourself if you
want file input, keeping this module free of I/O).

Top level::

    {
      "images": [ { "module": "...", "version": "...", "size": N,
                    "build_id": "...",                      # optional
                    "functions": [ ... ] } ],
      "events": [ ... ]
    }

A function::

    { "name": "...", "lo": N, "hi": N,
      "inlines": [ { "name": "...", "lo": N, "hi": N,
                     "children": [ ... ] } ] }             # both optional

Events, discriminated by ``"type"``::

    {"type": "load",   "timestamp": N, "pid": N,
     "module": "...", "version": "...", "base": N, "size": N}
    {"type": "unload", "timestamp": N, "pid": N, "module": "...",
     "version": "...", "base": N}                          # both optional
    {"type": "sample", "timestamp": N, "pid": N, "frames": [N, ...]}
"""

from __future__ import annotations

from collections.abc import Mapping as ABCMapping
from collections.abc import Sequence as ABCSequence
from typing import TYPE_CHECKING, Any, List, Sequence, Tuple

from .errors import InputFormatError
from .events import LoadEvent, SampleEvent, UnloadEvent
from .model import FunctionRange, InlineFrame, ModuleImage
from .profiler import Profiler
from .symbols import SymbolTable

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from .profiler import ProfileResult

__all__ = ["parse_trace", "build_profile"]

_IMAGE_FIELDS = {"module", "version", "size", "functions", "build_id"}
_FUNCTION_FIELDS = {"name", "lo", "hi", "inlines"}
_INLINE_FIELDS = {"name", "lo", "hi", "children"}
_EVENT_FIELDS = {
    "load": {"type", "timestamp", "pid", "module", "version", "base", "size"},
    "unload": {"type", "timestamp", "pid", "module", "version", "base"},
    "sample": {"type", "timestamp", "pid", "frames"},
}


def _require_mapping(value: Any, where: str) -> ABCMapping:
    if not isinstance(value, ABCMapping):
        raise InputFormatError(f"{where} must be a JSON object")
    return value


def _reject_unknown(data: ABCMapping, allowed: set, where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise InputFormatError(
            f"{where} has unknown field(s): {', '.join(sorted(unknown))}"
        )


def _require(data: ABCMapping, key: str, where: str) -> Any:
    if key not in data:
        raise InputFormatError(f"{where} is missing required field {key!r}")
    return data[key]


def _int_field(data: ABCMapping, key: str, where: str) -> int:
    value = _require(data, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputFormatError(f"{where}.{key} must be an integer")
    return value


def _text_field(data: ABCMapping, key: str, where: str) -> str:
    value = _require(data, key, where)
    if not isinstance(value, str) or not value:
        raise InputFormatError(f"{where}.{key} must be a non-empty string")
    return value


def _sequence_field(
    data: ABCMapping, key: str, where: str, *, required: bool
) -> Sequence[Any]:
    if key not in data:
        if required:
            raise InputFormatError(f"{where} is missing required field {key!r}")
        return ()
    value = data[key]
    if isinstance(value, (str, bytes)) or not isinstance(value, ABCSequence):
        raise InputFormatError(f"{where}.{key} must be a JSON array")
    return value


def _parse_inline(data: Any, where: str) -> InlineFrame:
    data = _require_mapping(data, where)
    _reject_unknown(data, _INLINE_FIELDS, where)
    children = _sequence_field(data, "children", where, required=False)
    return InlineFrame(
        name=_text_field(data, "name", where),
        lo=_int_field(data, "lo", where),
        hi=_int_field(data, "hi", where),
        children=tuple(
            _parse_inline(child, f"{where}.children[{i}]")
            for i, child in enumerate(children)
        ),
    )


def _parse_function(data: Any, where: str) -> FunctionRange:
    data = _require_mapping(data, where)
    _reject_unknown(data, _FUNCTION_FIELDS, where)
    inlines = _sequence_field(data, "inlines", where, required=False)
    return FunctionRange(
        name=_text_field(data, "name", where),
        lo=_int_field(data, "lo", where),
        hi=_int_field(data, "hi", where),
        inlines=tuple(
            _parse_inline(node, f"{where}.inlines[{i}]")
            for i, node in enumerate(inlines)
        ),
    )


def _parse_image(data: Any, where: str) -> ModuleImage:
    data = _require_mapping(data, where)
    _reject_unknown(data, _IMAGE_FIELDS, where)
    functions = _sequence_field(data, "functions", where, required=False)
    build_id = data.get("build_id")
    if build_id is not None and not isinstance(build_id, str):
        raise InputFormatError(f"{where}.build_id must be a string")
    return ModuleImage(
        module=_text_field(data, "module", where),
        version=_text_field(data, "version", where),
        size=_int_field(data, "size", where),
        functions=tuple(
            _parse_function(node, f"{where}.functions[{i}]")
            for i, node in enumerate(functions)
        ),
        build_id=build_id,
    )


def _parse_event(data: Any, where: str) -> object:
    data = _require_mapping(data, where)
    kind = data.get("type")
    if kind not in _EVENT_FIELDS:
        raise InputFormatError(
            f"{where}.type must be one of "
            f"{', '.join(sorted(_EVENT_FIELDS))}, got {kind!r}"
        )
    _reject_unknown(data, _EVENT_FIELDS[kind], where)

    if kind == "load":
        return LoadEvent(
            timestamp=_int_field(data, "timestamp", where),
            pid=_int_field(data, "pid", where),
            module=_text_field(data, "module", where),
            version=_text_field(data, "version", where),
            base=_int_field(data, "base", where),
            size=_int_field(data, "size", where),
        )
    if kind == "unload":
        version = data.get("version")
        if version is not None and (not isinstance(version, str) or not version):
            raise InputFormatError(f"{where}.version must be a non-empty string")
        base = data.get("base")
        if base is not None and (
            isinstance(base, bool) or not isinstance(base, int)
        ):
            raise InputFormatError(f"{where}.base must be an integer")
        return UnloadEvent(
            timestamp=_int_field(data, "timestamp", where),
            pid=_int_field(data, "pid", where),
            module=_text_field(data, "module", where),
            version=version,
            base=base,
        )
    frames = _sequence_field(data, "frames", where, required=True)
    checked: List[int] = []
    for index, frame in enumerate(frames):
        if isinstance(frame, bool) or not isinstance(frame, int):
            raise InputFormatError(
                f"{where}.frames[{index}] must be an integer address"
            )
        checked.append(frame)
    return SampleEvent(
        timestamp=_int_field(data, "timestamp", where),
        pid=_int_field(data, "pid", where),
        frames=tuple(checked),
    )


def parse_trace(data: Any) -> Tuple[SymbolTable, List[object]]:
    """Decode a trace object into a symbol table and a list of events."""

    data = _require_mapping(data, "trace")
    _reject_unknown(data, {"images", "events"}, "trace")

    images = _sequence_field(data, "images", "trace", required=False)
    table = SymbolTable(
        _parse_image(node, f"trace.images[{i}]")
        for i, node in enumerate(images)
    )

    events = _sequence_field(data, "events", "trace", required=True)
    parsed = [
        _parse_event(node, f"trace.events[{i}]")
        for i, node in enumerate(events)
    ]
    return table, parsed


def build_profile(data: Any) -> "ProfileResult":
    """Convenience: parse a trace and immediately replay it."""

    table, events = parse_trace(data)
    return Profiler(table).run(events)
