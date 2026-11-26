"""Program loading, validation, and small dict/expression builders."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Iterable

from .core import NodeRef


class ProgramValidationError(ValueError):
    """Raised when the restricted function graph does not match the input model."""


# ---------------------------------------------------------------------------
# Expression builders (also useful from Python tests)
# ---------------------------------------------------------------------------


def var(name: str) -> dict[str, Any]:
    return {"kind": "var", "name": name}


def alias(name: str) -> dict[str, Any]:
    return {"kind": "var", "name": name, "alias": True}


def lit(value: Any, *, tenant: bool = False) -> dict[str, Any]:
    kind = "tenant_literal" if tenant else "literal"
    return {"kind": kind, "value": value}


def current_tenant() -> dict[str, Any]:
    return {"kind": "tenant"}


def tup(*elements: Any) -> dict[str, Any]:
    return {"kind": "tuple", "elements": list(elements)}


def call(callee: str, *args: Any, **properties: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"kind": "call", "callee": callee, "args": list(args)}
    node.update(properties)
    return node


def unknown(reason: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"kind": "unknown"}
    if reason:
        node["reason"] = reason
    return node


def member(object_name: str, property_name: str) -> dict[str, Any]:
    return {"kind": "member", "object": object_name, "property": property_name}


# ---------------------------------------------------------------------------
# Program model
# ---------------------------------------------------------------------------


class CacheSpec:
    def __init__(self, name: str, raw: dict[str, Any] | None = None) -> None:
        self.name = name
        self.raw = raw or {}
        exclusive = self.raw.get(
            "exclusive_tenant",
            self.raw.get("tenant_exclusive", self.raw.get("exclusive", False)),
        )
        self.exclusive_tenant: str | None
        self.exclusive_current: bool
        if exclusive is True or exclusive == "current" or exclusive == "$current":
            self.exclusive_current = True
            self.exclusive_tenant = None
        elif isinstance(exclusive, str) and exclusive:
            self.exclusive_current = False
            self.exclusive_tenant = exclusive
        elif self.raw.get("exclusive_to_current_tenant") is True or self.raw.get("tenant_exclusive") is True:
            self.exclusive_current = True
            self.exclusive_tenant = None
        else:
            self.exclusive_current = False
            self.exclusive_tenant = None

    def allows_without_tenant_key(self, current_tenant_id: str | None) -> tuple[bool, str | None]:
        if self.exclusive_current:
            return True, "cache_exclusive_to_current_tenant"
        if self.exclusive_tenant is not None:
            if current_tenant_id is not None and current_tenant_id == self.exclusive_tenant:
                return True, "cache_exclusive_to_named_tenant"
            return False, None
        return False, None


class ParamModel:
    def __init__(self, name: str, index: int, ref: NodeRef, raw: dict[str, Any] | str) -> None:
        self.name = name
        self.index = index
        self.ref = ref
        self.raw = raw if isinstance(raw, dict) else {"name": name}
        self.bound_source = self.raw.get("source") or self.raw.get("provenance")


class FunctionModel:
    def __init__(self, name: str, raw: dict[str, Any], order: list[int]) -> None:
        self.name = name
        self.raw = raw
        params = raw.get("params", raw.get("parameters", []))
        if not isinstance(params, list):
            raise ProgramValidationError(f"function {name!r} params must be a list")
        self.params: list[ParamModel] = []
        seen: set[str] = set()
        for index, param in enumerate(params):
            if isinstance(param, str):
                pname = param
                praw: dict[str, Any] = {"name": pname}
            elif isinstance(param, dict):
                pname = str(param.get("name") or param.get("id") or f"arg{index}")
                praw = param
            else:
                raise ProgramValidationError(f"invalid parameter {param!r} in {name}")
            if pname in seen:
                raise ProgramValidationError(f"duplicate parameter {pname!r} in {name}")
            seen.add(pname)
            pid = str(praw.get("node_id") or praw.get("ref") or (pname if praw.get("id_as_node") else f"param:{pname}"))
            kind = "tenant_source" if praw.get("source") == "current_tenant" else "parameter"
            ref = NodeRef(name, pid, kind, f"parameter {pname}", order[0])
            order[0] += 1
            self.params.append(ParamModel(pname, index, ref, praw))

        body = raw.get("body", raw.get("statements", []))
        if not isinstance(body, list):
            raise ProgramValidationError(f"function {name!r} body must be a statement list")
        self.body: list[dict[str, Any]] = body
        self.node_refs: dict[str, NodeRef] = {}
        self._prepare_statements(body, name, order, [0])

    def _make_ref(self, stmt: dict[str, Any], func: str, local_counter: list[int], order: list[int]) -> NodeRef:
        kind = str(stmt.get("kind", "stmt"))
        explicit_id = stmt.get("id", stmt.get("node_id"))
        if explicit_id is None:
            node_id = f"{func}.{kind}.{local_counter[0]}"
            local_counter[0] += 1
        else:
            node_id = str(explicit_id)
        if node_id in self.node_refs:
            raise ProgramValidationError(f"duplicate node id {node_id!r} in function {func!r}")
        label = stmt.get("label") or stmt.get("summary") or ""
        if not label:
            if kind in {"cache_read", "read"}:
                label = f"read {stmt.get('cache', '')}"
            elif kind in {"cache_write", "write"}:
                label = f"write {stmt.get('cache', '')}"
            elif kind == "assign":
                label = f"assign {stmt.get('target', '')}"
            elif kind in {"call", "invoke"}:
                label = f"call {stmt.get('callee') or stmt.get('target') or ''}"
            else:
                label = kind
        ref = NodeRef(func, node_id, kind, str(label), order[0])
        order[0] += 1
        self.node_refs[node_id] = ref
        stmt["_ref"] = ref
        return ref

    def _prepare_statements(
        self,
        stmts: list[Any],
        func: str,
        order: list[int],
        local_counter: list[int],
    ) -> None:
        for stmt in stmts:
            if not isinstance(stmt, dict):
                raise ProgramValidationError(f"statement must be an object, got {stmt!r}")
            self._make_ref(stmt, func, local_counter, order)
            for child_key in ("then", "else", "body", "statements"):
                child = stmt.get(child_key)
                if isinstance(child, list):
                    self._prepare_statements(child, func, order, local_counter)
                elif isinstance(child, dict):
                    self._prepare_statements([child], func, order, local_counter)
            cases = stmt.get("cases")
            if isinstance(cases, list):
                for case in cases:
                    if isinstance(case, dict) and isinstance(case.get("body"), list):
                        self._prepare_statements(case["body"], func, order, local_counter)


class Program:
    def __init__(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ProgramValidationError("program must be a JSON object")
        self.raw = copy.deepcopy(data)
        self._normalize_kinds(self.raw)
        self.current_tenant_id = self.raw.get("current_tenant_id")
        self.tenant_source_paths = {
            "ctx.tenant_id",
            "context.tenant_id",
            "request.tenant_id",
            "ctx.tenantId",
            "context.tenantId",
            "request.tenantId",
        }
        extra_paths = self.raw.get("tenant_source_paths", [])
        if isinstance(extra_paths, list):
            self.tenant_source_paths.update(str(x) for x in extra_paths)

        caches_raw = self.raw.get("caches", {})
        if not isinstance(caches_raw, dict):
            raise ProgramValidationError("caches must be an object")

        def cache_spec(value: Any) -> dict[str, Any]:
            if isinstance(value, dict):
                return value
            if value is True or value in {"current", "$current", "tenant"}:
                return {"exclusive_to_current_tenant": True}
            if isinstance(value, str) and value:
                return {"exclusive_tenant": value}
            return {}

        self.caches = {name: CacheSpec(str(name), cache_spec(spec)) for name, spec in caches_raw.items()}

        normalizers_raw = self.raw.get("normalizers", {})
        self.normalizers: dict[str, dict[str, Any]] = {}
        if isinstance(normalizers_raw, dict):
            for name, spec in normalizers_raw.items():
                self.normalizers[str(name)] = spec if isinstance(spec, dict) else {"mode": str(spec)}
        elif isinstance(normalizers_raw, list):
            for spec in normalizers_raw:
                if not isinstance(spec, dict) or "name" not in spec:
                    raise ProgramValidationError("normalizer list entries must contain name")
                self.normalizers[str(spec["name"])] = spec

        functions_raw = self.raw.get("functions", {})
        if not isinstance(functions_raw, dict) or not functions_raw:
            raise ProgramValidationError("program.functions must be a non-empty object")
        order = [0]
        self.functions: dict[str, FunctionModel] = {}
        for name, raw_fn in functions_raw.items():
            if not isinstance(raw_fn, dict):
                raise ProgramValidationError(f"function {name!r} must be an object")
            self.functions[str(name)] = FunctionModel(str(name), raw_fn, order)

        self.entrypoints = self._prepare_entrypoints(self.raw.get("entrypoints"))
        if not self.entrypoints:
            self.entrypoints = self._infer_entrypoints()

    @staticmethod
    def _normalize_kinds(value: Any) -> None:
        if isinstance(value, dict):
            if "kind" not in value and isinstance(value.get("type"), str):
                value["kind"] = value["type"]
            for child in value.values():
                Program._normalize_kinds(child)
        elif isinstance(value, list):
            for child in value:
                Program._normalize_kinds(child)

    def _prepare_entrypoints(self, raw: Any) -> list[dict[str, Any]]:
        if raw is None:
            return []
        if isinstance(raw, (str, dict)):
            raw = [raw]
        if not isinstance(raw, list):
            raise ProgramValidationError("entrypoints must be a string, object, or list")
        result: list[dict[str, Any]] = []
        for entry in raw:
            if isinstance(entry, str):
                result.append({"function": entry, "arguments": []})
            elif isinstance(entry, dict):
                fn = entry.get("function") or entry.get("name")
                if fn not in self.functions:
                    raise ProgramValidationError(f"entrypoint references unknown function {fn!r}")
                args = entry.get("arguments", entry.get("args", []))
                if args is None:
                    args = []
                if not isinstance(args, list):
                    raise ProgramValidationError(f"entrypoint {fn!r} arguments must be a list")
                result.append({"function": str(fn), "arguments": args})
            else:
                raise ProgramValidationError(f"invalid entrypoint {entry!r}")
        return result

    def _called_function_names(self) -> set[str]:
        called: set[str] = set()

        def walk(stmts: Iterable[dict[str, Any]]) -> None:
            for stmt in stmts:
                kind = stmt.get("kind")
                if kind in {"call", "invoke"}:
                    name = stmt.get("callee") or stmt.get("function") or stmt.get("target")
                    if isinstance(name, str):
                        called.add(name)
                value = stmt.get("value")
                if isinstance(value, dict) and value.get("kind") == "call":
                    name = value.get("callee") or value.get("function")
                    if isinstance(name, str):
                        called.add(name)
                for key in ("then", "else", "body", "statements"):
                    child = stmt.get(key)
                    if isinstance(child, list):
                        walk(child)
                cases = stmt.get("cases")
                if isinstance(cases, list):
                    for case in cases:
                        if isinstance(case, dict) and isinstance(case.get("body"), list):
                            walk(case["body"])

        for fn in self.functions.values():
            walk(fn.body)
        return called

    def _infer_entrypoints(self) -> list[dict[str, Any]]:
        called = self._called_function_names()
        roots = [name for name in self.functions if name not in called]
        if not roots:
            # A pure recursive SCC with no externally uncalled node: analyze all
            # members conservatively as possible entries.
            roots = list(self.functions)
        return [{"function": name, "arguments": None} for name in roots]

    def require_function(self, name: str) -> FunctionModel:
        if name not in self.functions:
            raise ProgramValidationError(f"unknown function {name!r}")
        return self.functions[name]


def load_program(source: str | Path | dict[str, Any] | Program) -> Program:
    if isinstance(source, Program):
        return source
    if isinstance(source, dict):
        return Program(source)
    if isinstance(source, Path):
        with source.open("r", encoding="utf-8") as handle:
            return Program(json.load(handle))
    if isinstance(source, str):
        path = Path(source)
        if source.lstrip().startswith("{"):
            return Program(json.loads(source))
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                return Program(json.load(handle))
        raise ProgramValidationError(f"input file does not exist and is not JSON: {source}")
    raise ProgramValidationError("unsupported program source")
