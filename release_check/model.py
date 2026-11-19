"""Parsing and contract resolution for release specifications.

A specification is a single JSON document with three sections:

``callers``
    The finite set of audit identities (components that call the profile
    generator through some interface).

``interfaces``
    Nodes of the interface inheritance graph.  Each interface carries
    per-method visibility declarations: ``{method: {identity: [fields]}}``.
    The listed fields are the *maximum* a given identity may observe through
    that entry point.

``implementations``
    Candidate implementations.  Each lists the identities it accepts and the
    fields it may actually return, either as one flat list valid for every
    accepted identity or as a per-identity mapping.

Contract resolution
-------------------
A declaration on an interface replaces (overrides) everything it inherits for
that method.  Without such an override, an entry inherits the contract of
*every* ancestor branch, and all branches bind simultaneously: an identity is
callable through the entry only if every branch that imposes a contract grants
it, and its visible fields are the intersection of the fields each branch
grants.  A public ancestor that exposes a method without declaring it anywhere
leaves the entry undecided instead of being guessed in either direction.

The parser is deliberately strict.  Unknown keys, dangling ``extends`` or
``implements`` targets, duplicate names, duplicate fields, unknown identities
and inheritance cycles raise :class:`InputError`, so that a typo can never
silently disable a check.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "InputError",
    "Interface",
    "ImplMethod",
    "Implementation",
    "Resolution",
    "Spec",
    "load_document",
    "parse_spec",
]


class InputError(ValueError):
    """Raised when a release specification is structurally invalid."""


# ---------------------------------------------------------------------------
# primitive parsers
# ---------------------------------------------------------------------------


def _as_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"{where}: expected a JSON object")
    return value


def _require_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{where}: expected a non-empty string")
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = set(mapping) - set(allowed)
    if unknown:
        rendered = ", ".join(sorted(unknown))
        raise InputError(f"{where}: unknown key(s): {rendered}")


def _parse_name_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InputError(f"{where}: expected a list of strings")
    seen: set[str] = set()
    names: list[str] = []
    for index, item in enumerate(value):
        name = _require_str(item, f"{where}[{index}]")
        if name in seen:
            raise InputError(f"{where}: duplicate entry {name!r}")
        seen.add(name)
        names.append(name)
    return tuple(names)


def _parse_fields(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InputError(f"{where}: expected a list of field names")
    seen: set[str] = set()
    for index, item in enumerate(value):
        name = _require_str(item, f"{where}[{index}]")
        if name in seen:
            raise InputError(f"{where}: duplicate field {name!r}")
        seen.add(name)
    return tuple(sorted(seen))


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interface:
    """A node of the interface inheritance graph."""

    name: str
    public: bool
    extends: tuple[str, ...]
    exposes: frozenset[str]
    #: method -> identity -> maximum visible fields
    declarations: Mapping[str, Mapping[str, tuple[str, ...]]]


@dataclass(frozen=True)
class ImplMethod:
    """One method of a candidate implementation."""

    accepts: frozenset[str]
    returns_flat: tuple[str, ...] | None
    returns_by_identity: Mapping[str, tuple[str, ...]]

    def return_fields(self, identity: str) -> tuple[str, ...]:
        if self.returns_flat is not None:
            return self.returns_flat
        return self.returns_by_identity.get(identity, ())


@dataclass(frozen=True)
class Implementation:
    name: str
    implements: tuple[str, ...]
    methods: Mapping[str, ImplMethod]


@dataclass(frozen=True)
class Resolution:
    """Contract visible through one ``(interface, method)`` entry.

    * ``resolved`` -- identity -> visible fields.  Only identities granted by
      every binding branch appear here; the fields are the intersection of
      what each branch allows.
    * ``origins``  -- identity -> interfaces whose declarations produced that
      limit.  Used to point a finding back at the contract it breaks.
    * ``binding``  -- whether any declaration actually governs this entry.  A
      branch that declares the method with *no* identities still binds: it
      admits nobody, which must not be mistaken for "no constraint".
    * ``missing``  -- public interfaces on the chain that expose the method
      without any declaration.  A non-empty ``missing`` means the entry is
      undecided, and ``resolved`` is empty because the unknown branch could
      widen or narrow the contract.
    """

    resolved: Mapping[str, tuple[str, ...]]
    origins: Mapping[str, tuple[str, ...]]
    missing: tuple[str, ...]
    binding: bool = False

    @property
    def pending(self) -> bool:
        return bool(self.missing)


@dataclass
class Spec:
    """A validated release specification."""

    callers: frozenset[str]
    interfaces: Mapping[str, Interface]
    implementations: tuple[Implementation, ...]
    _closure_cache: dict[str, tuple[str, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _exposed_cache: dict[str, frozenset[str]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _contract_cache: dict[tuple[str, str], Resolution] = field(
        default_factory=dict, repr=False, compare=False
    )

    # -- graph helpers ------------------------------------------------------

    def closure(self, name: str) -> tuple[str, ...]:
        """Return ``name`` plus all transitive ancestors, breadth first.

        Siblings on the same level are ordered by name so that every derived
        report is deterministic.
        """

        cached = self._closure_cache.get(name)
        if cached is not None:
            return cached
        order: list[str] = [name]
        seen: set[str] = {name}
        level: list[str] = [name]
        while level:
            parents = sorted(
                {parent for node in level for parent in self.interfaces[node].extends} - seen
            )
            if not parents:
                break
            seen.update(parents)
            order.extend(parents)
            level = parents
        cached = tuple(order)
        self._closure_cache[name] = cached
        return cached

    def exposed_methods(self, name: str) -> frozenset[str]:
        """Methods reachable through ``name``.

        A method is reachable if ``name`` or an ancestor declares it, or if
        ``name`` or an ancestor lists it in ``exposes`` (a declared part of the
        surface whose visibility declaration is missing).
        """

        cached = self._exposed_cache.get(name)
        if cached is not None:
            return cached
        methods: set[str] = set()
        for node in self.closure(name):
            interface = self.interfaces[node]
            methods.update(interface.declarations)
            methods.update(interface.exposes)
        cached = frozenset(methods)
        self._exposed_cache[name] = cached
        return cached

    # -- contract resolution ------------------------------------------------

    def resolve_method(self, name: str, method: str) -> Resolution:
        """Resolve the contract for ``method`` as seen through interface ``name``."""

        key = (name, method)
        cached = self._contract_cache.get(key)
        if cached is None:
            cached = self._compute_contract(name, method)
            self._contract_cache[key] = cached
        return cached

    def _compute_contract(self, name: str, method: str) -> Resolution:
        interface = self.interfaces[name]

        # An own declaration overrides everything inherited.
        if method in interface.declarations:
            declaration = interface.declarations[method]
            return Resolution(
                resolved={
                    identity: tuple(sorted(fields))
                    for identity, fields in declaration.items()
                },
                origins={identity: (name,) for identity in declaration},
                missing=(),
                binding=True,
            )

        parents = interface.extends
        if not parents:
            missing = (name,) if method in interface.exposes and interface.public else ()
            return Resolution({}, {}, missing)

        branches = [(parent, self.resolve_method(parent, method)) for parent in parents]

        missing = tuple(sorted({item for _, branch in branches for item in branch.missing}))
        if missing:
            # A public ancestor declares nothing: the effective contract is
            # unknown, so no identity is claimed decidable.  The ancestor is
            # named here so the caller can see *why* the entry is undecided.
            return Resolution({}, {}, missing)

        imposing = [(parent, branch) for parent, branch in branches if branch.binding]
        if not imposing:
            missing = (name,) if method in interface.exposes and interface.public else ()
            return Resolution({}, {}, missing)

        # Both (all) branches bind: an identity survives only if every
        # imposing branch grants it, and sees only the common fields.
        common = set(imposing[0][1].resolved)
        for _, branch in imposing[1:]:
            common &= set(branch.resolved)

        resolved: dict[str, tuple[str, ...]] = {}
        origins: dict[str, tuple[str, ...]] = {}
        for identity in sorted(common):
            allowed = frozenset(imposing[0][1].resolved[identity])
            for _, branch in imposing[1:]:
                allowed &= frozenset(branch.resolved[identity])
            resolved[identity] = tuple(sorted(allowed))
            origins[identity] = tuple(
                sorted(
                    {
                        source
                        for _, branch in imposing
                        for source in branch.origins[identity]
                    }
                )
            )
        return Resolution(resolved, origins, (), binding=True)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _parse_interface(raw: Any, where: str) -> Interface:
    mapping = _as_mapping(raw, where)
    _reject_unknown(mapping, {"name", "public", "extends", "exposes", "methods"}, where)
    name = _require_str(mapping.get("name"), f"{where}.name")

    public = mapping.get("public", True)
    if not isinstance(public, bool):
        raise InputError(f"{where}.public: expected true or false")

    extends = _parse_name_list(mapping.get("extends", []), f"{where}.extends")
    exposes = _parse_name_list(mapping.get("exposes", []), f"{where}.exposes")

    methods_raw = _as_mapping(mapping.get("methods", {}), f"{where}.methods")
    declarations: dict[str, Mapping[str, tuple[str, ...]]] = {}
    for method, visibility_raw in methods_raw.items():
        _require_str(method, f"{where}.methods key")
        method_where = f"{where}.methods.{method}"
        visibility = _as_mapping(visibility_raw, method_where)
        declarations[method] = {
            _require_str(identity, f"{method_where} identity"): _parse_fields(
                fields, f"{method_where}.{identity}"
            )
            for identity, fields in visibility.items()
        }

    contradictory = sorted(set(exposes) & set(declarations))
    if contradictory:
        # ``exposes`` means "on the public surface, declaration missing"; a real
        # declaration for the same method would make the two claims contradict.
        rendered = ", ".join(contradictory)
        raise InputError(
            f"{where}: method(s) both declared and listed in exposes: {rendered}"
        )

    return Interface(
        name=name,
        public=public,
        extends=extends,
        exposes=frozenset(exposes),
        declarations=declarations,
    )


def _parse_impl_method(raw: Any, where: str) -> ImplMethod:
    mapping = _as_mapping(raw, where)
    _reject_unknown(mapping, {"accepts", "returns"}, where)
    if "accepts" not in mapping:
        raise InputError(f"{where}.accepts: required")
    accepts = frozenset(_parse_name_list(mapping["accepts"], f"{where}.accepts"))

    returns_flat: tuple[str, ...] | None = None
    returns_by_identity: dict[str, tuple[str, ...]] = {}
    returns_raw = mapping.get("returns", [])
    if isinstance(returns_raw, list):
        returns_flat = _parse_fields(returns_raw, f"{where}.returns")
    elif isinstance(returns_raw, dict):
        for identity, fields in returns_raw.items():
            _require_str(identity, f"{where}.returns identity")
            returns_by_identity[identity] = _parse_fields(
                fields, f"{where}.returns.{identity}"
            )
    else:
        raise InputError(
            f"{where}.returns: expected a list of fields or a mapping identity -> fields"
        )
    return ImplMethod(
        accepts=accepts,
        returns_flat=returns_flat,
        returns_by_identity=returns_by_identity,
    )


def _parse_implementation(raw: Any, where: str) -> Implementation:
    mapping = _as_mapping(raw, where)
    _reject_unknown(mapping, {"name", "implements", "methods"}, where)
    name = _require_str(mapping.get("name"), f"{where}.name")
    implements = _parse_name_list(mapping.get("implements"), f"{where}.implements")
    if not implements:
        raise InputError(f"{where}.implements: expected at least one interface")

    methods_raw = _as_mapping(mapping.get("methods", {}), f"{where}.methods")
    methods: dict[str, ImplMethod] = {}
    for method, body in methods_raw.items():
        _require_str(method, f"{where}.methods key")
        methods[method] = _parse_impl_method(body, f"{where}.methods.{method}")
    return Implementation(name=name, implements=implements, methods=methods)


def _assert_acyclic(interfaces: Mapping[str, Interface]) -> None:
    # 0 = unvisited, 1 = on the current path, 2 = finished.
    color: dict[str, int] = {name: 0 for name in interfaces}
    for root in sorted(interfaces):
        if color[root]:
            continue
        color[root] = 1
        path: list[str] = [root]
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            name, index = stack[-1]
            parents = interfaces[name].extends
            if index < len(parents):
                stack[-1] = (name, index + 1)
                parent = parents[index]
                if color[parent] == 1:
                    cycle = path[path.index(parent) :] + [parent]
                    raise InputError("inheritance cycle: " + " -> ".join(cycle))
                if color[parent] == 0:
                    color[parent] = 1
                    path.append(parent)
                    stack.append((parent, 0))
            else:
                color[name] = 2
                stack.pop()
                path.pop()


def parse_spec(document: Any) -> Spec:
    """Validate ``document`` and build a :class:`Spec`."""

    document = _as_mapping(document, "specification")
    _reject_unknown(document, {"callers", "interfaces", "implementations"}, "specification")

    if "callers" not in document:
        raise InputError("callers: required")
    if "interfaces" not in document:
        raise InputError("interfaces: required")
    callers = _parse_name_list(document["callers"], "callers")

    raw_interfaces = document["interfaces"]
    if not isinstance(raw_interfaces, list):
        raise InputError("interfaces: expected a list")
    interfaces: dict[str, Interface] = {}
    for index, raw in enumerate(raw_interfaces):
        interface = _parse_interface(raw, f"interfaces[{index}]")
        if interface.name in interfaces:
            raise InputError(f"interfaces[{index}].name: duplicate interface {interface.name!r}")
        interfaces[interface.name] = interface

    for interface in interfaces.values():
        for parent in interface.extends:
            if parent not in interfaces:
                raise InputError(
                    f"interface {interface.name!r} extends unknown interface {parent!r}"
                )
    _assert_acyclic(interfaces)

    raw_implementations = document.get("implementations", [])
    if not isinstance(raw_implementations, list):
        raise InputError("implementations: expected a list")
    implementations: list[Implementation] = []
    seen_implementations: set[str] = set()
    for index, raw in enumerate(raw_implementations):
        implementation = _parse_implementation(raw, f"implementations[{index}]")
        if implementation.name in seen_implementations:
            raise InputError(
                f"implementations[{index}].name: duplicate implementation "
                f"{implementation.name!r}"
            )
        seen_implementations.add(implementation.name)
        for target in implementation.implements:
            if target not in interfaces:
                raise InputError(
                    f"implementation {implementation.name!r} implements unknown "
                    f"interface {target!r}"
                )
        implementations.append(implementation)

    spec = Spec(
        callers=frozenset(callers),
        interfaces=interfaces,
        implementations=tuple(implementations),
    )
    _assert_identities_known(spec)
    return spec


def _assert_identities_known(spec: Spec) -> None:
    """Every identity mentioned anywhere must be a declared caller.

    Without this an identity typo in one declaration would quietly make that
    declaration invisible to the offender it was meant to constrain.
    """

    known = spec.callers

    def check(identity: str, where: str) -> None:
        if identity not in known:
            raise InputError(
                f"{where}: identity {identity!r} is not listed in the caller set"
            )

    for interface in spec.interfaces.values():
        for method, visibility in interface.declarations.items():
            for identity in visibility:
                check(identity, f"interface {interface.name!r} method {method!r}")

    for implementation in spec.implementations:
        for method, body in implementation.methods.items():
            where = f"implementation {implementation.name!r} method {method!r}"
            for identity in sorted(body.accepts):
                check(identity, f"{where} accepts")
            for identity in body.returns_by_identity:
                check(identity, f"{where} returns")
                if identity not in body.accepts:
                    raise InputError(
                        f"{where} returns: identity {identity!r} is not accepted "
                        "by this implementation"
                    )


def load_document(path: str) -> Mapping[str, Any]:
    """Read a JSON specification from ``path`` (``-`` means standard input)."""

    if path == "-":
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise InputError(f"<stdin>: invalid JSON: {exc}") from exc
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise InputError(f"{path}: cannot read specification: {reason}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: invalid JSON: {exc}") from exc
