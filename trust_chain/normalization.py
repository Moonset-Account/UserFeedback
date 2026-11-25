"""Flexible normalization for plain dictionaries and dataclass inputs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .model import (
    InvalidPolicyError,
    InvalidTicketError,
    ParentSegment,
    ReviewRequirement,
    Ticket,
)

_ORIGIN_KEYS = (
    "origin",
    "origin_domain",
    "originDomain",
    "start",
    "start_domain",
    "startDomain",
    "source",
    "source_domain",
    "sourceDomain",
    "src",
    "from",
    "from_domain",
    "issuer",
    "起始域",
    "起点域",
)
_TARGET_KEYS = (
    "target",
    "target_domain",
    "targetDomain",
    "destination",
    "destination_domain",
    "destinationDomain",
    "dest",
    "dst",
    "to",
    "to_domain",
    "audience",
    "service",
    "service_domain",
    "recipient",
    "目标域",
)
_TRANSIT_KEYS = (
    "transit",
    "transit_domains",
    "transitDomains",
    "transited_domains",
    "intermediates",
    "intermediate_domains",
    "intermediateDomains",
    "middle_domains",
    "actual_transit_domains",
    "actualTransitDomains",
    "visited_domains",
    "visitedDomains",
    "hops",
    "via",
    "中转域",
    "实际中转域",
)
_PATH_KEYS = ("path", "full_path", "fullPath", "route", "chain", "domains", "路径")


def canonical_domain(value: Any, *, normalize: bool = True) -> str:
    """Return a non-empty comparable domain name.

    Domain names are treated case-insensitively and a trailing dot is ignored
    when ``normalize`` is true.  The function intentionally does not enforce
    full RFC syntax because tests and deployments may use short synthetic
    labels such as ``a`` or ``corp``.
    """

    if not isinstance(value, str):
        raise InvalidTicketError(f"domain must be a string, got {type(value).__name__}")
    domain = value.strip()
    if not domain:
        raise InvalidTicketError("domain must not be empty")
    if normalize:
        domain = domain.lower()
        if domain.endswith("."):
            domain = domain[:-1].rstrip()
    if not domain:
        raise InvalidTicketError("domain must not be empty")
    return domain


def _domain_set(values: Any, *, normalize: bool) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        return {canonical_domain(values, normalize=normalize)}
    if isinstance(values, Mapping):
        # A mapping is not a sensible set of next hops; callers may have meant
        # to provide a graph and used the wrong top-level argument.
        raise InvalidPolicyError("expected domains or an iterable of domains, got mapping")
    if isinstance(values, Iterable):
        return {canonical_domain(v, normalize=normalize) for v in values}
    raise InvalidPolicyError(f"expected iterable of domains, got {type(values).__name__}")


def _lookup(mapping: Mapping[str, Any], keys: Iterable[str]) -> tuple[bool, Any]:
    for key in keys:
        if key in mapping:
            return True, mapping[key]
    return False, None


def normalize_hop_graph(value: Any = None, *, normalize: bool = True, **aliases: Any) -> dict[str, set[str]]:
    """Normalize issued/trusted next-hop configuration.

    Accepts ``issuer -> next-hop`` mappings as well as iterables of
    ``(issuer, next_hop)`` pairs or edge dictionaries.  Additional keyword
    aliases are merged, which makes the public call surface tolerant of common
    schema names.
    """

    graph: dict[str, set[str]] = {}

    def add_edge(source: Any, target: Any) -> None:
        s = canonical_domain(source, normalize=normalize)
        t = canonical_domain(target, normalize=normalize)
        graph.setdefault(s, set()).add(t)

    sources: list[Any] = []
    if value is not None:
        sources.append(value)
    for alias_value in aliases.values():
        if alias_value is not None:
            sources.append(alias_value)

    for source in sources:
        if isinstance(source, Mapping):
            for issuer, hops in source.items():
                for hop in _domain_set(hops, normalize=normalize):
                    s = canonical_domain(issuer, normalize=normalize)
                    graph.setdefault(s, set()).add(hop)
        elif isinstance(source, Iterable) and not isinstance(source, (str, bytes)):
            for edge in source:
                if isinstance(edge, Mapping):
                    found_s, sval = _lookup(
                        edge, ("issuer", "source", "domain", "from", "child", "current")
                    )
                    found_t, tval = _lookup(
                        edge, ("next_hop", "next", "target", "to", "parent", "destination")
                    )
                    if not found_s or not found_t:
                        raise InvalidPolicyError(f"invalid hop edge: {edge!r}")
                    add_edge(sval, tval)
                elif isinstance(edge, (tuple, list)) and len(edge) == 2:
                    add_edge(edge[0], edge[1])
                else:
                    raise InvalidPolicyError(f"invalid hop edge: {edge!r}")
        else:
            raise InvalidPolicyError(f"invalid issued-hop graph: {source!r}")

    return graph


def normalize_parent_graph(value: Any = None, *, normalize: bool = True, **aliases: Any) -> dict[str, set[str]]:
    """Normalize child-to-parent edges into ``child -> {parents}``.

    A string mapping value denotes the normal unique-parent case.  A sequence
    value or repeated edge can represent ambiguous topology and will produce an
    ambiguous-segment rejection if restoration reaches that child.
    """

    graph = normalize_hop_graph(value, normalize=normalize, **aliases)
    return graph


def _normalize_target_set(value: Any, *, normalize: bool) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return frozenset({canonical_domain(value, normalize=normalize)})
    return frozenset(_domain_set(value, normalize=normalize))


def normalize_review_requirements(
    value: Any = None, *, normalize: bool = True
) -> tuple[ReviewRequirement, ...]:
    if value is None:
        return ()
    if isinstance(value, ReviewRequirement):
        value = (value,)

    requirements: list[ReviewRequirement] = []

    def add(source: Any, reviewer: Any, targets: Any = None) -> None:
        if isinstance(source, (list, tuple, set, frozenset)):
            for one_source in source:
                add(one_source, reviewer, targets)
            return
        if isinstance(reviewer, (list, tuple, set, frozenset)):
            # Secure interpretation: every explicitly listed reviewer is
            # required unless a caller constructs an any-of policy object.
            for one in reviewer:
                add(source, one, targets)
            return
        requirements.append(
            ReviewRequirement(
                source=canonical_domain(source, normalize=normalize),
                reviewer=canonical_domain(reviewer, normalize=normalize),
                targets=_normalize_target_set(targets, normalize=normalize),
            )
        )

    if isinstance(value, Mapping):
        for source, policy in value.items():
            if isinstance(policy, str):
                add(source, policy)
            elif isinstance(policy, ReviewRequirement):
                requirements.append(policy)
            elif isinstance(policy, Mapping):
                _, reviewer = _lookup(policy, ("reviewer", "review_domain", "required_reviewer", "audit_domain"))
                if reviewer is None:
                    reviewers = policy.get("reviewers") or policy.get("required_reviewers")
                    if reviewers is None:
                        raise InvalidPolicyError(f"review policy for {source!r} has no reviewer")
                    reviewer = reviewers
                _, targets = _lookup(policy, ("targets", "target", "target_domains", "audiences"))
                add(source, reviewer, targets)
            elif isinstance(policy, (list, tuple, set, frozenset)):
                add(source, policy)
            else:
                raise InvalidPolicyError(f"invalid review requirement for {source!r}: {policy!r}")
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        for item in value:
            if isinstance(item, ReviewRequirement):
                requirements.append(item)
            elif isinstance(item, Mapping):
                _, source = _lookup(
                item,
                (
                    "source",
                    "source_domain",
                    "sources",
                    "source_domains",
                    "origin",
                    "from",
                ),
            )
                _, reviewer = _lookup(
                    item, ("reviewer", "review_domain", "required_reviewer", "audit_domain")
                )
                if source is None or reviewer is None:
                    raise InvalidPolicyError(f"invalid review requirement: {item!r}")
                _, targets = _lookup(item, ("targets", "target", "target_domains"))
                add(source, reviewer, targets)
            elif isinstance(item, (tuple, list)):
                if len(item) == 2:
                    add(item[0], item[1])
                elif len(item) == 3:
                    add(item[0], item[1], item[2])
                else:
                    raise InvalidPolicyError(f"invalid review requirement: {item!r}")
            else:
                raise InvalidPolicyError(f"invalid review requirement: {item!r}")
    else:
        raise InvalidPolicyError("review_requirements must be a mapping or iterable")

    # De-duplicate while preserving stable order.
    seen: set[ReviewRequirement] = set()
    result: list[ReviewRequirement] = []
    for req in requirements:
        if req not in seen:
            seen.add(req)
            result.append(req)
    return tuple(result)


def _segment_from_mapping(item: Mapping[str, Any], *, normalize: bool) -> ParentSegment:
    _, start = _lookup(
        item,
        ("start", "start_domain", "from", "from_domain", "child", "startDomain", "起点", "起始域"),
    )
    _, end = _lookup(
        item,
        ("end", "end_domain", "to", "to_domain", "parent", "target", "endDomain", "终点", "目标父域"),
    )
    if end is None:
        raise InvalidTicketError(f"compressed segment is missing an end domain: {item!r}")
    return ParentSegment(
        start=canonical_domain(start, normalize=normalize) if start is not None else None,
        end=canonical_domain(end, normalize=normalize),
        compressed=bool(item.get("compressed", item.get("segment", True))),
    )


def coerce_route_item(item: Any, *, normalize: bool = True) -> str | ParentSegment:
    if isinstance(item, ParentSegment):
        start = (
            canonical_domain(item.start, normalize=normalize)
            if item.start is not None
            else None
        )
        end = canonical_domain(item.end, normalize=normalize) if item.end is not None else None
        if end is None:
            raise InvalidTicketError("compressed segment is missing an end domain")
        return ParentSegment(start=start, end=end, compressed=item.compressed)

    if isinstance(item, str):
        text = item.strip()
        if ".." in text:
            left, right = text.split("..", 1)
            # Ignore one extra leading separator from forms such as "a...d".
            right = right.lstrip(".")
            if left and right and ".." not in right:
                return ParentSegment(
                    start=canonical_domain(left, normalize=normalize),
                    end=canonical_domain(right, normalize=normalize),
                )
        return canonical_domain(item, normalize=normalize)

    if isinstance(item, Mapping):
        looks_like_segment = any(
            key in item
            for key in (
                "segment",
                "compressed",
                "segment_type",
                "type",
                "start",
                "start_domain",
                "end",
                "end_domain",
            )
        )
        segment_type = item.get("segment_type") or item.get("type")
        if segment_type in {"parent_chain", "compressed_segment", "segment"}:
            looks_like_segment = True
        if looks_like_segment and any(k in item for k in ("end", "end_domain", "to", "to_domain", "parent")):
            return _segment_from_mapping(item, normalize=normalize)

        _, domain = _lookup(
            item,
            ("domain", "name", "next_hop", "hop", "issuer", "source", "start", "from"),
        )
        if domain is None:
            raise InvalidTicketError(f"route item has no domain: {item!r}")
        return canonical_domain(domain, normalize=normalize)

    if isinstance(item, (tuple, list)):
        if len(item) == 2 and all(isinstance(x, str) for x in item):
            return ParentSegment(
                start=canonical_domain(item[0], normalize=normalize),
                end=canonical_domain(item[1], normalize=normalize),
            )
        if len(item) == 3 and item[0] in {"segment", "parent_segment", "compressed"}:
            return ParentSegment(
                start=canonical_domain(item[1], normalize=normalize),
                end=canonical_domain(item[2], normalize=normalize),
            )

    raise InvalidTicketError(f"unsupported route item: {item!r}")


def _is_ellipsis_marker(item: Any) -> bool:
    if isinstance(item, str):
        return item.strip() in {"...", "..", "…", ". . .", "[compressed]", "<compressed>"}
    if isinstance(item, Mapping):
        has_end = any(key in item for key in ("end", "end_domain", "to", "to_domain"))
        return (
            not has_end
            and (
                item.get("ellipsis") is True
                or item.get("compressed") is True
                or item.get("segment") is True
                or item.get("type") in {"ellipsis", "compressed_marker"}
            )
        )
    return False


def _route_item_endpoint(item: str | ParentSegment) -> str | None:
    if isinstance(item, str):
        return item
    return item.end


def collapse_route_items(
    raw_items: list[Any] | tuple[Any, ...],
    *,
    normalize: bool = True,
    initial_domain: str | None = None,
) -> list[str | ParentSegment]:
    """Convert ``left, '...', right`` notation into a parent segment."""

    items = list(raw_items)
    collapsed: list[str | ParentSegment] = []
    i = 0
    while i < len(items):
        raw = items[i]
        if _is_ellipsis_marker(raw):
            if collapsed:
                left = _route_item_endpoint(collapsed[-1])
            else:
                left = initial_domain

            j = i + 1
            while j < len(items) and _is_ellipsis_marker(items[j]):
                j += 1
            if j >= len(items):
                raise InvalidTicketError("compressed route marker has no ending domain")
            right_item = coerce_route_item(items[j], normalize=normalize)
            right = _route_item_endpoint(right_item)
            if left is None or right is None:
                raise InvalidTicketError("compressed route marker has no resolvable endpoints")
            collapsed.append(ParentSegment(start=left, end=right))
            i = j + 1
            continue

        collapsed.append(coerce_route_item(raw, normalize=normalize))
        i += 1
    return collapsed


def coerce_ticket(ticket: Any, *, normalize: bool = True) -> tuple[str | None, str | None, list[Any], list[Any] | None]:
    """Return ``(origin, target, transit, path)`` with domains canonicalized."""

    if isinstance(ticket, Ticket):
        origin = ticket.origin
        target = ticket.target
        transit = list(ticket.transit or ())
        path = list(ticket.path) if ticket.path is not None else None
    elif isinstance(ticket, Mapping):
        _, origin = _lookup(ticket, _ORIGIN_KEYS)
        _, target = _lookup(ticket, _TARGET_KEYS)

        has_path, path_value = _lookup(ticket, _PATH_KEYS)
        path = list(path_value) if has_path and path_value is not None else None

        has_transit, transit_value = _lookup(ticket, _TRANSIT_KEYS)
        if has_transit and transit_value is not None:
            if not isinstance(transit_value, (list, tuple)):
                raise InvalidTicketError("ticket transit domains must be a list or tuple")
            transit = list(transit_value)
        else:
            transit = []
    else:
        # Tolerate simple attribute objects.
        origin = getattr(ticket, "origin", None) or getattr(ticket, "start_domain", None)
        target = getattr(ticket, "target", None) or getattr(ticket, "target_domain", None)
        transit = list(getattr(ticket, "transit", ()) or getattr(ticket, "transit_domains", ()) or ())
        path_value = getattr(ticket, "path", None)
        path = list(path_value) if path_value is not None else None

    origin_domain = (
        canonical_domain(origin, normalize=normalize) if origin is not None else None
    )
    target_domain = (
        canonical_domain(target, normalize=normalize) if target is not None else None
    )

    if path is not None:
        normalized_path = collapse_route_items(path, normalize=normalize)
        return origin_domain, target_domain, [], normalized_path

    if origin_domain is None:
        raise InvalidTicketError("ticket is missing origin/start/source domain")
    if target_domain is None:
        raise InvalidTicketError("ticket is missing target/destination domain")
    normalized_transit = collapse_route_items(
        transit, normalize=normalize, initial_domain=origin_domain
    )
    return origin_domain, target_domain, normalized_transit, None
