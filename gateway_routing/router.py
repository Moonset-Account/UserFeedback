"""A per-method gateway routing table with atomic batch publication."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .errors import PathError, RouteConflictError, RoutingError, TemplateError
from .templates import Template, TemplatePair, analyze_templates, parse_template, split_path

__all__ = [
    "Conflict",
    "Route",
    "RouteMatch",
    "RouteTable",
    "Router",
]


def _coerce_template(value: str | Template) -> Template:
    if isinstance(value, Template):
        return value
    if isinstance(value, str):
        return parse_template(value)
    raise TemplateError(value, "expected a Template or a template string")


@dataclass(frozen=True)
class Route:
    """A registered rule: a template plus an opaque payload (e.g. a handler)."""

    template: Template
    payload: Any = None
    name: str | None = None

    def __post_init__(self) -> None:
        # Allow ``Route("/a/{x}")`` as well as ``Route.create(...)``.
        object.__setattr__(self, "template", _coerce_template(self.template))

    @classmethod
    def create(
        cls, template: str | Template, payload: Any = None, name: str | None = None
    ) -> "Route":
        return cls(template=_coerce_template(template), payload=payload, name=name)

    def __str__(self) -> str:
        if self.name is None:
            return self.template.text
        return f"{self.name}({self.template.text})"


def _coerce_route(value: Route | str | Template) -> Route:
    if isinstance(value, Route):
        return value
    return Route.create(value)


def _coerce_routes(
    rules: Iterable[Route | str | Template] | Route | str | Template,
) -> tuple[Route, ...]:
    """Accept an iterable of rules or a single ``Route``/template string."""

    if isinstance(rules, (Route, Template, str)):
        return (_coerce_route(rules),)
    try:
        return tuple(_coerce_route(rule) for rule in rules)
    except TypeError as exc:  # not iterable and not a recognised route
        raise RoutingError(f"expected an iterable of routes, got {rules!r}") from exc


def _normalize_method(method: object) -> str:
    if not isinstance(method, str) or not method.strip():
        raise RoutingError(f"invalid HTTP method {method!r}")
    return method.strip().upper()


@dataclass(frozen=True)
class Conflict:
    """Two rules whose path sets overlap without either strictly containing the other."""

    method: str
    a: Route
    b: Route
    pair: TemplatePair

    @property
    def witness_path(self) -> str:
        assert self.pair.witness_path is not None
        return self.pair.witness_path

    def describe(self) -> str:
        return (
            f"{self.a!s} vs {self.b!s} overlap at {self.witness_path!r} "
            f"(neither path set strictly contains the other)"
        )


@dataclass(frozen=True)
class RouteMatch:
    """The winning rule for a request, with captured parameters."""

    route: Route
    params: Mapping[str, str]
    path: str
    segments: tuple[str, ...]


def _find_conflicts(method: str, routes: tuple[Route, ...]) -> list[Conflict]:
    found: list[Conflict] = []
    for index, left in enumerate(routes):
        for right in routes[index + 1:]:
            pair = analyze_templates(left.template, right.template)
            if pair.conflict:
                found.append(Conflict(method=method, a=left, b=right, pair=pair))
    return found


@dataclass(frozen=True)
class RouteTable:
    """An immutable snapshot of the rules registered for one HTTP method.

    Constructing a table directly validates it and raises
    :class:`RouteConflictError` for an inconsistent set of rules.
    """

    method: str
    routes: tuple[Route, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        conflicts = _find_conflicts(self.method, self.routes)
        if conflicts:
            raise RouteConflictError(conflicts)

    def match(self, path: str) -> RouteMatch | None:
        """Resolve a request path to the rule whose path set is smallest.

        A validated table is a chain under set inclusion, so the minimal
        matching rule — when it exists — is unique.
        """

        segments = split_path(path)
        candidates: list[tuple[Route, dict[str, str]]] = []
        for route in self.routes:
            params = route.template.match_segments(segments)
            if params is not None:
                candidates.append((route, params))
        if not candidates:
            return None

        winner: Route | None = None
        winner_params: dict[str, str] | None = None
        for route, params in candidates:
            if winner is None:
                winner, winner_params = route, params
                continue
            current = analyze_templates(route.template, winner.template)
            if current.a_subset_b and not current.b_subset_a:
                winner, winner_params = route, params
            elif current.equal:
                # Equal languages cannot coexist in a validated table; this
                # branch only guards a hand-built, unvalidated table.
                raise RoutingError(f"ambiguous match for {path!r}")

        assert winner is not None and winner_params is not None
        return RouteMatch(
            route=winner,
            params=MappingProxyType(winner_params),
            path=path,
            segments=segments,
        )


class Router:
    """A gateway routing table keyed by HTTP method.

    Publication is transactional.  ``publish`` validates a whole batch of
    rules for one method, ``publish_batch`` several methods at once; only an
    entirely consistent batch is swapped in.  On rejection the previously
    published tables remain in force untouched.
    """

    def __init__(self) -> None:
        self._tables: dict[str, RouteTable] = {}

    # -- publication ------------------------------------------------------

    def _stage(
        self, batches: Mapping[str, Iterable[Route | str | Template]]
    ) -> tuple[dict[str, RouteTable], list[Conflict]]:
        staged: dict[str, RouteTable] = {}
        conflicts: list[Conflict] = []
        for method, rules in batches.items():
            normalized = _normalize_method(method)
            route_list = _coerce_routes(rules)
            method_conflicts = _find_conflicts(normalized, route_list)
            if method_conflicts:
                conflicts.extend(method_conflicts)
            else:
                staged[normalized] = RouteTable(method=normalized, routes=route_list)
        return staged, conflicts

    def publish(
        self, method: str, routes: Iterable[Route | str | Template] | Route | str | Template
    ) -> RouteTable:
        """Atomically replace the rules registered for ``method``."""

        normalized = _normalize_method(method)
        return self.publish_batch({normalized: routes})[normalized]

    def check(
        self, method: str, routes: Iterable[Route | str | Template] | Route | str | Template
    ) -> tuple[Conflict, ...]:
        """Dry-run a batch: return its conflicts without changing the table."""

        normalized = _normalize_method(method)
        return tuple(_find_conflicts(normalized, _coerce_routes(routes)))

    def publish_batch(
        self, batches: Mapping[str, Iterable[Route | str | Template]]
    ) -> dict[str, RouteTable]:
        """Atomically replace the rules for every method in ``batches``.

        Either every method's batch is accepted, or none of them is.
        """

        staged, conflicts = self._stage(batches)
        if conflicts:
            raise RouteConflictError(conflicts)
        self._tables.update(staged)
        return dict(staged)

    def replace_all(
        self, batches: Mapping[str, Iterable[Route | str | Template]]
    ) -> dict[str, RouteTable]:
        """Atomically replace the whole routing table with ``batches``.

        Methods absent from ``batches`` are removed.  A conflict leaves the
        previous table completely intact.
        """

        staged, conflicts = self._stage(batches)
        if conflicts:
            raise RouteConflictError(conflicts)
        self._tables = staged
        return dict(staged)

    # -- queries ----------------------------------------------------------

    def resolve(self, method: str, path: str) -> RouteMatch | None:
        """Resolve a request to the smallest rule whose language contains it.

        Unknown methods and malformed paths resolve to ``None``.
        """

        try:
            table = self._tables[_normalize_method(method)]
        except (KeyError, RoutingError):
            return None
        try:
            return table.match(path)
        except PathError:
            return None

    def table(self, method: str) -> RouteTable:
        """Return the current (immutable) table for a method."""

        normalized = _normalize_method(method)
        return self._tables.get(normalized, RouteTable(method=normalized))

    def routes(self, method: str) -> tuple[Route, ...]:
        return self.table(method).routes

    def methods(self) -> tuple[str, ...]:
        return tuple(sorted(self._tables))

    def snapshot(self) -> dict[str, tuple[Route, ...]]:
        """A copy of the live table, useful for checking atomicity."""

        return {method: table.routes for method, table in sorted(self._tables.items())}

    def __len__(self) -> int:
        return sum(len(table.routes) for table in self._tables.values())
