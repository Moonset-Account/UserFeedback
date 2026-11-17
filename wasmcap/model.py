"""Data model and validation for builder-exported Wasm plugin summaries.

The builder exports a JSON document describing a plugin module::

    {
      "version": 1,
      "types": {"<type_id>": {"params": ["i32", ...], "results": [...]}},
      "functions": {
          "<func>": {"type": "<type_id>", "kind": "local"}
                    | {"type": "<type_id>", "kind": "import",
                       "module": "env", "name": "host_fn"}
      },
      "capabilities": {"<import func>": ["<capability>", ...]},
      "direct_calls": [{"caller": "f", "callee": "g", "site": "f@12"}],
      "indirect_calls": [{"caller": "f", "table": "tab",
                          "expected_type": "t0", "site": "f@20"}],
      "tables": {"tab": {"externally_writable": false,
                         "initial": ["g", null]}},
      "element_updates": [{"table": "tab", "index": 1,
                           "function": "h", "at": "f@30"}],
      "entries": [{"name": "main", "function": "f",
                   "allowed_capabilities": ["log"]}]
    }

Program points ("sites") are written ``<function>@<offset>``.  Element
updates model ``table.set`` / active element segments emitted by the
builder; an update is a *possible* table write that only matters for an
entry when the function containing its program point is reachable from
that entry.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

SCHEMA_VERSION = 1


class SummaryError(ValueError):
    """Raised when a summary document is malformed or inconsistent."""


@dataclass(frozen=True)
class FuncType:
    params: tuple = ()
    results: tuple = ()


@dataclass(frozen=True)
class Function:
    name: str
    type_id: str
    kind: str  # "local" | "import"
    module: str = ""
    import_name: str = ""


@dataclass(frozen=True)
class DirectCall:
    caller: str
    callee: str
    site: str


@dataclass(frozen=True)
class IndirectCall:
    caller: str
    table: str
    expected_type: str
    site: str


@dataclass(frozen=True)
class Table:
    name: str
    externally_writable: bool
    initial: tuple  # function names or None per slot


@dataclass(frozen=True)
class ElementUpdate:
    table: str
    index: int
    function: str
    at: str  # program point "func@offset" of the write


@dataclass(frozen=True)
class Entry:
    name: str
    function: str
    allowed_capabilities: frozenset


@dataclass
class Summary:
    types: dict = field(default_factory=dict)          # type_id -> FuncType
    functions: dict = field(default_factory=dict)      # name -> Function
    capabilities: dict = field(default_factory=dict)   # import func -> frozenset[str]
    direct_calls: list = field(default_factory=list)
    indirect_calls: list = field(default_factory=list)
    tables: dict = field(default_factory=dict)         # name -> Table
    updates: list = field(default_factory=list)
    entries: list = field(default_factory=list)


def program_point_function(point: str) -> str:
    """Return the function part of a ``func@offset`` program point."""
    return point.split("@", 1)[0]


def _fail(msg):
    raise SummaryError(msg)


def _require(cond, msg):
    if not cond:
        _fail(msg)


def _require_str_list(value, what):
    _require(isinstance(value, list), f"{what} must be a list")
    for item in value:
        _require(isinstance(item, str), f"{what} entries must be strings, got {item!r}")
    return tuple(value)


def _check_point(point, what, functions):
    _require(isinstance(point, str) and "@" in point,
             f"{what} must be a program point 'func@offset', got {point!r}")
    fn = program_point_function(point)
    _require(fn in functions, f"{what} references unknown function {fn!r}")
    return fn


def parse_summary(doc) -> Summary:
    """Validate a decoded JSON document and build a :class:`Summary`."""
    _require(isinstance(doc, dict), "summary document must be a JSON object")
    version = doc.get("version", SCHEMA_VERSION)
    _require(version == SCHEMA_VERSION, f"unsupported summary version {version!r}")

    # --- types -------------------------------------------------------------
    raw_types = doc.get("types")
    _require(isinstance(raw_types, dict) and raw_types, "'types' must be a non-empty object")
    types = {}
    for type_id, spec in raw_types.items():
        _require(isinstance(spec, dict), f"type {type_id!r} must be an object")
        types[type_id] = FuncType(
            params=_require_str_list(spec.get("params", []), f"type {type_id!r} params"),
            results=_require_str_list(spec.get("results", []), f"type {type_id!r} results"),
        )

    # --- functions ---------------------------------------------------------
    raw_funcs = doc.get("functions")
    _require(isinstance(raw_funcs, dict) and raw_funcs, "'functions' must be a non-empty object")
    functions = {}
    for name, spec in raw_funcs.items():
        _require(isinstance(spec, dict), f"function {name!r} must be an object")
        kind = spec.get("kind")
        _require(kind in ("local", "import"), f"function {name!r} has bad kind {kind!r}")
        type_id = spec.get("type")
        _require(type_id in types, f"function {name!r} references unknown type {type_id!r}")
        module = spec.get("module", "")
        import_name = spec.get("name", "")
        if kind == "import":
            _require(isinstance(module, str) and isinstance(import_name, str),
                     f"import {name!r} needs string 'module' and 'name'")
        functions[name] = Function(name=name, type_id=type_id, kind=kind,
                                   module=module, import_name=import_name)

    # --- capability registry ------------------------------------------------
    raw_caps = doc.get("capabilities", {})
    _require(isinstance(raw_caps, dict), "'capabilities' must be an object")
    capabilities = {}
    for func, caps in raw_caps.items():
        _require(func in functions, f"capability registry references unknown function {func!r}")
        _require(functions[func].kind == "import",
                 f"capability registry entry {func!r} is not an imported host function")
        capabilities[func] = frozenset(_require_str_list(caps, f"capabilities of {func!r}"))

    # --- direct calls --------------------------------------------------------
    direct_calls = []
    for i, spec in enumerate(doc.get("direct_calls", [])):
        what = f"direct_calls[{i}]"
        caller, callee, site = spec.get("caller"), spec.get("callee"), spec.get("site")
        _require(caller in functions, f"{what} references unknown caller {caller!r}")
        _require(functions[caller].kind == "local", f"{what} caller {caller!r} is not local")
        _require(callee in functions, f"{what} references unknown callee {callee!r}")
        _require(_check_point(site, f"{what} site", functions) == caller,
                 f"{what} site {site!r} is not inside caller {caller!r}")
        direct_calls.append(DirectCall(caller=caller, callee=callee, site=site))

    # --- indirect calls ------------------------------------------------------
    raw_tables = doc.get("tables", {})
    _require(isinstance(raw_tables, dict), "'tables' must be an object")
    indirect_calls = []
    for i, spec in enumerate(doc.get("indirect_calls", [])):
        what = f"indirect_calls[{i}]"
        caller, table = spec.get("caller"), spec.get("table")
        expected, site = spec.get("expected_type"), spec.get("site")
        _require(caller in functions, f"{what} references unknown caller {caller!r}")
        _require(functions[caller].kind == "local", f"{what} caller {caller!r} is not local")
        _require(table in raw_tables, f"{what} references unknown table {table!r}")
        _require(expected in types, f"{what} references unknown type {expected!r}")
        _require(_check_point(site, f"{what} site", functions) == caller,
                 f"{what} site {site!r} is not inside caller {caller!r}")
        indirect_calls.append(IndirectCall(caller=caller, table=table,
                                           expected_type=expected, site=site))

    # --- tables ---------------------------------------------------------------
    tables = {}
    for name, spec in raw_tables.items():
        _require(isinstance(spec, dict), f"table {name!r} must be an object")
        initial = spec.get("initial", [])
        _require(isinstance(initial, list), f"table {name!r} 'initial' must be a list")
        slots = []
        for el in initial:
            _require(el is None or el in functions,
                     f"table {name!r} initial element references unknown function {el!r}")
            slots.append(el)
        tables[name] = Table(name=name,
                             externally_writable=bool(spec.get("externally_writable", False)),
                             initial=tuple(slots))

    # --- element updates -------------------------------------------------------
    updates = []
    for i, spec in enumerate(doc.get("element_updates", [])):
        what = f"element_updates[{i}]"
        table, index, func, at = (spec.get("table"), spec.get("index"),
                                  spec.get("function"), spec.get("at"))
        _require(table in tables, f"{what} references unknown table {table!r}")
        _require(isinstance(index, int) and not isinstance(index, bool) and index >= 0,
                 f"{what} index must be a non-negative integer, got {index!r}")
        _require(func in functions, f"{what} references unknown function {func!r}")
        host = _check_point(at, f"{what} program point", functions)
        _require(functions[host].kind == "local",
                 f"{what} program point {at!r} is not inside a local function")
        updates.append(ElementUpdate(table=table, index=index, function=func, at=at))

    # --- entries -----------------------------------------------------------------
    raw_entries = doc.get("entries")
    _require(isinstance(raw_entries, list) and raw_entries, "'entries' must be a non-empty list")
    entries = []
    seen_names = set()
    for i, spec in enumerate(raw_entries):
        what = f"entries[{i}]"
        name, func = spec.get("name"), spec.get("function")
        _require(isinstance(name, str) and name, f"{what} needs a non-empty 'name'")
        _require(name not in seen_names, f"duplicate entry name {name!r}")
        seen_names.add(name)
        _require(func in functions, f"{what} references unknown function {func!r}")
        _require(functions[func].kind == "local", f"{what} function {func!r} is not local")
        allowed = frozenset(_require_str_list(spec.get("allowed_capabilities", []),
                                              f"{what} allowed_capabilities"))
        entries.append(Entry(name=name, function=func, allowed_capabilities=allowed))

    return Summary(types=types, functions=functions, capabilities=capabilities,
                   direct_calls=direct_calls, indirect_calls=indirect_calls,
                   tables=tables, updates=updates, entries=entries)


def load_summary(path: Union[str, Path]) -> Summary:
    """Load and validate a summary JSON file."""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return parse_summary(doc)
