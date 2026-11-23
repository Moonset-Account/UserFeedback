"""Predicate pushdown and outer-to-inner join rewrite.

The engine only moves *WHERE* predicates (:class:`~predrewrite.plans.Filter`
nodes). Join *ON* match conditions are never rewritten, because their 3VL
semantics (and the left outer join's NULL padding for unmatched left rows)
are what preserve padding rows, duplicate rows and UNKNOWN results.

Rules applied bottom-up, per top-level AND conjunct of every filter:

Inner join
==========
* a conjunct referencing only the left (or only the right) subtree's
  aliases is pushed below that subtree;
* a conjunct referencing both sides cannot be evaluated below the join and
  is retained above it;
* a conjunct referencing no alias (constant) is conservatively retained.

Left outer join
===============
Let the right subtree be the null-supplied side.
* left-only conjuncts are pushed below the left subtree exactly as for an
  inner join -- they cannot see or affect NULL-padded rows;
* the conjunction of every conjunct referencing a right-side alias is
  checked with the symbolic null-rejection proof
  (:func:`predrewrite.expressions.is_null_rejecting`). When it is provably
  FALSE/UNKNOWN on every null-padded row, the WHERE clause removes all
  padding rows and the outer join is *equivalent to an inner join*: it is
  converted, after which all conjuncts are reconsidered under the
  inner-join rules and pushing may continue recursively;
* otherwise (the conjunction can be TRUE on a padding row, or the result
  cannot be proved from the expression alone) the join stays outer and
  every right-side / cross-side conjunct is retained above it.

Every decision is recorded as a :class:`PredicateEvent` whose
``destination`` is the *real plan node id* hosting the conjunct after that
step; a predicate moved through several hops (e.g. an outer-to-inner
conversion followed by further pushdown) produces one event per hop, and
:meth:`RewriteResult.destinations` returns the final location.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .expressions import (
    Expr,
    and_exprs,
    expr_to_text,
    is_null_rejecting,
    null_status_explanation,
    referenced_aliases,
    split_conjuncts,
)
from .plans import (
    Filter,
    InnerJoin,
    LeftOuterJoin,
    Plan,
    Scan,
    plan_schema,
)

__all__ = [
    "PredicateEvent",
    "ConversionEvent",
    "RewriteResult",
    "rewrite",
]


# ---------------------------------------------------------------------------
# Trace records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PredicateEvent:
    """One hop in the life of an original top-level filter conjunct."""

    pid: str
    text: str
    action: str          # "pushed" | "retained"
    destination: str     # real plan node id hosting the conjunct after this hop
    reason: str
    aliases: frozenset[str]
    conversion_node: str | None = None  # set when the hop used LOJ -> inner


@dataclass(frozen=True)
class ConversionEvent:
    """Record of one left-outer-join to inner-join conversion."""

    outer_node_id: str
    inner_node_id: str
    triggered_by: tuple[str, ...]
    justification: str


@dataclass
class RewriteResult:
    """Output of :func:`rewrite`."""

    plan: Plan
    predicate_events: list[PredicateEvent] = field(default_factory=list)
    conversion_events: list[ConversionEvent] = field(default_factory=list)

    def destinations(self) -> dict[str, str]:
        """Final hosting plan node id for each original predicate id.

        A predicate moved through several filters maps to its final
        location (the last recorded hop).
        """
        final: dict[str, str] = {}
        for event in self.predicate_events:
            final[event.pid] = event.destination
        return final

    def events_for(self, pid: str) -> list[PredicateEvent]:
        """Return the ordered hop trace for one predicate id."""
        return [e for e in self.predicate_events if e.pid == pid]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class _Engine:
    def __init__(self) -> None:
        self.predicate_events: list[PredicateEvent] = []
        self.conversion_events: list[ConversionEvent] = []
        self._seq = 0

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    # -- public entry -----------------------------------------------------

    def rewrite(self, plan: Plan) -> Plan:
        return self._rewrite(plan)

    # -- structural recursion --------------------------------------------

    def _rewrite(self, plan: Plan) -> Plan:
        if isinstance(plan, Scan):
            return plan

        if isinstance(plan, Filter):
            # Collapse stacked filters: children are rewritten first, then
            # all conjuncts (with their original ids) are pushed together.
            child = self._rewrite(plan.source)
            conjuncts = split_conjuncts(plan.expr)
            if plan.pids and len(plan.pids) == len(conjuncts):
                pids = list(plan.pids)
            elif not plan.pids:
                pids = [self._new_id("p") for _ in conjuncts]
            else:
                # A filter whose stored pids no longer line up with its
                # conjuncts (e.g. a re-rewritten plan whose AND was nested
                # differently): keep the first known ids and synthesize the
                # rest rather than dropping provenance.
                pids = list(plan.pids[: len(conjuncts)])
                while len(pids) < len(conjuncts):
                    pids.append(self._new_id("p"))
            return self._push(child, list(zip(pids, conjuncts)))

        if isinstance(plan, (InnerJoin, LeftOuterJoin)):
            # A join with no filter above it still needs its children
            # rewritten in case they contain filters.
            left = self._rewrite(plan.left)
            right = self._rewrite(plan.right)
            return self._with_children(plan, left, right)

        raise TypeError(f"unknown plan type: {type(plan)!r}")  # pragma: no cover

    # -- filter distribution ---------------------------------------------

    def _push(self, source: Plan, items: list[tuple[str, Expr]]) -> Plan:
        """Push conjuncts ``(pid, expr)`` down onto ``source``.

        Filter nodes (and their ids) are created *after* the routing
        decision, so every trace event can reference the real node id that
        finally hosts the conjunct.
        """
        if not items:
            return source

        if isinstance(source, Scan):
            node_id = self._new_id("filter_scan")
            self._record_hop(
                items, node_id, "pushed",
                f"single-side predicate pushed to scan {source.alias}",
            )
            return self._filter_above(source, items, node_id)

        if isinstance(source, Filter):
            # Another rewritten filter sits here: merge into one node so
            # the output contains no stacked filters. The existing node id
            # is reused, and the newly arriving conjuncts log a hop to it.
            existing = list(zip(source.pids, split_conjuncts(source.expr)))
            merged = existing + items
            self._record_hop(
                items, source.node_id, "pushed",
                "merged into the existing filter at this node",
            )
            return self._filter_above(source.source, merged, source.node_id)

        if isinstance(source, InnerJoin):
            return self._push_inner(source, items)

        if isinstance(source, LeftOuterJoin):
            return self._push_outer(source, items)

        raise TypeError(f"unknown plan type: {type(source)!r}")  # pragma: no cover

    def _push_inner(
        self, join: InnerJoin, items: list[tuple[str, Expr]]
    ) -> Plan:
        left_aliases = self._aliases(join.left)
        right_aliases = self._aliases(join.right)

        left_items: list[tuple[str, Expr]] = []
        right_items: list[tuple[str, Expr]] = []
        keep_items: list[tuple[str, Expr]] = []
        reasons: dict[str, str] = {}

        for pid, expr in items:
            aliases = referenced_aliases(expr)
            if aliases and aliases <= left_aliases:
                left_items.append((pid, expr))
                reasons[pid] = (
                    f"references only left aliases {sorted(aliases)} of "
                    f"inner join {join.node_id}"
                )
            elif aliases and aliases <= right_aliases:
                right_items.append((pid, expr))
                reasons[pid] = (
                    f"references only right aliases {sorted(aliases)} of "
                    f"inner join {join.node_id}"
                )
            else:
                keep_items.append((pid, expr))
                reasons[pid] = (
                    "constant predicate; retained conservatively"
                    if not aliases
                    else "references both sides of the inner join; "
                         "it cannot be evaluated below the join"
                )

        left = self._push(join.left, left_items)
        right = self._push(join.right, right_items)
        rebuilt = InnerJoin(left=left, right=right, on=join.on,
                            node_id=join.node_id)
        if keep_items:
            node_id = self._new_id("filter_join")
            self._record_hop(
                keep_items, node_id, "retained",
                "retained above the inner join",
                reasons=reasons,
            )
            return self._filter_above(rebuilt, keep_items, node_id)
        return rebuilt

    def _push_outer(
        self, join: LeftOuterJoin, items: list[tuple[str, Expr]]
    ) -> Plan:
        left_aliases = self._aliases(join.left)
        right_aliases = self._aliases(join.right)
        null_aliases = right_aliases  # right side is the null-supplied side

        left_items: list[tuple[str, Expr]] = []
        right_items: list[tuple[str, Expr]] = []
        keep_items: list[tuple[str, Expr]] = []

        for pid, expr in items:
            aliases = referenced_aliases(expr)
            if aliases and aliases <= left_aliases:
                left_items.append((pid, expr))
            elif aliases & null_aliases:
                right_items.append((pid, expr))
            else:
                keep_items.append((pid, expr))

        # Decide whether the *conjunction* of all right-side conjuncts
        # rejects every null-padded row. The proof is over the whole AND:
        # e.g. ``r.x > 5 AND r.y IS NULL`` rejects padding rows even though
        # the ``IS NULL`` conjunct alone does not.
        can_convert = bool(right_items) and is_null_rejecting(
            and_exprs([expr for _, expr in right_items]), null_aliases
        )

        if can_convert:
            # The WHERE clause removes all padding rows, so the LOJ is
            # equivalent to an inner join.
            inner_node_id = self._new_id("inner_join")
            triggers = tuple(pid for pid, _ in right_items)
            conjunction = and_exprs([expr for _, expr in right_items])
            justification = (
                "conjunction "
                + " AND ".join(triggers)
                + " "
                + null_status_explanation(conjunction, null_aliases)
                + f"; every NULL-padded row of {join.node_id} is removed by "
                  "the WHERE clause"
            )
            self.conversion_events.append(
                ConversionEvent(
                    outer_node_id=join.node_id,
                    inner_node_id=inner_node_id,
                    triggered_by=triggers,
                    justification=justification,
                )
            )
            left = self._push(join.left, left_items)
            right = self._rewrite(join.right)
            inner = InnerJoin(
                left=left, right=right, on=join.on, node_id=inner_node_id
            )
            # Log the conversion hop for every triggering conjunct, then
            # reconsider all of them under inner-join rules: further
            # pushing (and more hops) may happen inside ``_push``.
            self._record_hop(
                right_items, inner_node_id, "pushed",
                f"outer join {join.node_id} converted to inner join "
                f"{inner_node_id}",
                conversion_node=inner_node_id,
            )
            remaining = right_items + keep_items
            # ``right_items`` is non-empty whenever can_convert holds, so
            # ``remaining`` is always non-empty here; the branch below is
            # reached on every conversion. The assertion documents that
            # invariant rather than carrying unreachable fallback code.
            assert remaining, "conversion requires at least one triggering conjunct"
            return self._push(inner, remaining)

        # Join stays outer: every right-side conjunct must remain above it,
        # because the combined filter cannot be proved to kill padding rows.
        left = self._push(join.left, left_items)
        right = self._rewrite(join.right)
        rebuilt = LeftOuterJoin(
            left=left, right=right, on=join.on, node_id=join.node_id
        )
        keep_all = right_items + keep_items
        if keep_all:
            node_id = self._new_id("filter_outer")
            reason = (
                "combined right-side filter is not provably null-rejecting: "
                + null_status_explanation(
                    and_exprs([e for _, e in right_items]), null_aliases
                )
                if right_items
                else "constant predicate retained conservatively"
            )
            self._record_hop(
                keep_all, node_id, "retained",
                f"retained above left outer join {join.node_id}",
                uniform_reason=reason,
            )
            return self._filter_above(rebuilt, keep_all, node_id)
        return rebuilt

    # -- small helpers ----------------------------------------------------

    def _filter_above(
        self, source: Plan, items: list[tuple[str, Expr]], node_id: str
    ) -> Filter:
        pids = tuple(pid for pid, _ in items)
        expr = and_exprs([expr for _, expr in items])
        return Filter(source=source, expr=expr, pids=pids, node_id=node_id)

    def _aliases(self, plan: Plan) -> frozenset[str]:
        return frozenset(alias for alias, _ in plan_schema(plan))

    def _with_children(self, join: Plan, left: Plan, right: Plan) -> Plan:
        if isinstance(join, InnerJoin):
            return InnerJoin(left=left, right=right, on=join.on,
                             node_id=join.node_id)
        return LeftOuterJoin(left=left, right=right, on=join.on,
                             node_id=join.node_id)

    def _record_hop(
        self,
        items: list[tuple[str, Expr]],
        node_id: str,
        action: str,
        reason: str,
        *,
        reasons: dict[str, str] | None = None,
        uniform_reason: str | None = None,
        conversion_node: str | None = None,
    ) -> None:
        for pid, expr in items:
            detail = (
                uniform_reason
                if uniform_reason is not None
                else (reasons or {}).get(pid, reason)
            )
            self.predicate_events.append(
                PredicateEvent(
                    pid=pid,
                    text=expr_to_text(expr),
                    action=action,
                    destination=node_id,
                    reason=detail,
                    aliases=referenced_aliases(expr),
                    conversion_node=conversion_node,
                )
            )


def rewrite(plan: Plan) -> RewriteResult:
    """Rewrite a query plan and return the new plan plus a full predicate trace.

    The input plan is not modified. Each top-level AND conjunct of every
    :class:`~predrewrite.plans.Filter` should carry a stable predicate id via
    the filter's ``pids`` field (one id per conjunct, in order); filters
    without ids are assigned synthetic ``pN`` ids.
    """
    engine = _Engine()
    new_plan = engine.rewrite(plan)
    return RewriteResult(
        plan=new_plan,
        predicate_events=list(engine.predicate_events),
        conversion_events=list(engine.conversion_events),
    )
