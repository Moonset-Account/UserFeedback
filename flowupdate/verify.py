"""Independent verification of a loop-free update schedule.

The verifier does not trust the solver.  Given the old and new tables and a
schedule (an ordered list of rounds), it replays the update from the old table
and, for each round, enumerates **every** subset of that round's nodes as the
set that "may already have taken effect".  If any such subset leaves a node
unable to reach the egress, the schedule is rejected.

Only after every round — and every within-round activation subset — has been
checked does the verifier confirm the final table equals the new table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence, Tuple

from .model import (
    ForwardingTable,
    find_loops,
    unreachable_nodes,
)
from .solver import Schedule, subsets, table_for_state


@dataclass(frozen=True)
class RoundVerdict:
    nodes: Tuple[str, ...]
    checked_subsets: int
    ok: bool
    failing_subset: Tuple[str, ...] = ()
    unreachable: Tuple[str, ...] = ()
    loops: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Verification:
    ok: bool
    rounds: Tuple[RoundVerdict, ...] = ()
    error: str = ""
    final_matches_new: bool = False

    def describe(self) -> str:
        if self.ok:
            total = sum(r.checked_subsets for r in self.rounds)
            return (
                f"OK: {len(self.rounds)} round(s), {total} within-round "
                "activation subset(s) checked, final table equals new table"
            )
        lines = ["FAILED: " + (self.error or "schedule is not loop-free")]
        for index, verdict in enumerate(self.rounds, start=1):
            if verdict.ok:
                continue
            lines.append(
                f"  round {index} {list(verdict.nodes)}: subset "
                f"{list(verdict.failing_subset)} leaves "
                f"{list(verdict.unreachable)} unable to reach the egress"
            )
            if verdict.loops:
                lines.append("    loops: " + "; ".join(verdict.loops))
        return "\n".join(lines)


def verify_schedule(
    old: Mapping[str, Optional[str]],
    new: Mapping[str, Optional[str]],
    rounds: Sequence[Sequence[str]],
) -> Verification:
    """Replay *rounds* and confirm it is a safe, complete update.

    *rounds* is a sequence of sequences of node names, applied in order.  Each
    round must be a subset of the nodes that actually change between the old and
    new tables, every round must contain at least one node, and together the
    rounds must cover every changed node exactly once.
    """

    changed = {n for n in (set(old) | set(new)) if old.get(n) != new.get(n)}
    seen: List[str] = []
    verdicts: List[RoundVerdict] = []
    base: ForwardingTable = dict(old)

    for index, raw_round in enumerate(rounds, start=1):
        nodes = tuple(sorted(raw_round))
        if not nodes:
            return Verification(
                ok=False,
                rounds=tuple(verdicts),
                error=f"round {index} is empty",
            )
        rejected = [n for n in nodes if n not in changed]
        if rejected:
            return Verification(
                ok=False,
                rounds=tuple(verdicts),
                error=(
                    f"round {index} lists node(s) {rejected} that do not change "
                    "between the old and new tables"
                ),
            )
        duplicates = [n for n in nodes if n in seen]
        if duplicates:
            return Verification(
                ok=False,
                rounds=tuple(verdicts),
                error=f"round {index} repeats already-applied node(s) {duplicates}",
            )

        checked = 0
        for subset in subsets(nodes):
            checked += 1
            trial = table_for_state(base, new, subset)
            bad = unreachable_nodes(trial)
            if bad:
                verdicts.append(
                    RoundVerdict(
                        nodes=nodes,
                        checked_subsets=checked,
                        ok=False,
                        failing_subset=subset,
                        unreachable=tuple(sorted(bad)),
                        loops=tuple(find_loops(trial)),
                    )
                )
                return Verification(
                    ok=False,
                    rounds=tuple(verdicts),
                    error=f"round {index} is unsafe",
                )
        verdicts.append(RoundVerdict(nodes=nodes, checked_subsets=checked, ok=True))
        seen.extend(nodes)
        base = table_for_state(base, new, nodes)

    missing = sorted(changed - set(seen))
    if missing:
        return Verification(
            ok=False,
            rounds=tuple(verdicts),
            error=f"changed node(s) {missing} were never updated",
        )

    final_matches = _same_table(base, new)
    if not final_matches:
        return Verification(
            ok=False,
            rounds=tuple(verdicts),
            error="final table does not equal the new table",
            final_matches_new=False,
        )
    return Verification(ok=True, rounds=tuple(verdicts), final_matches_new=True)


def verify(old, new, schedule: Schedule) -> Verification:
    """Verify a :class:`~flowupdate.solver.Schedule` object."""

    return verify_schedule(old, new, schedule.rounds)


def _same_table(left: Mapping[str, Optional[str]],
                right: Mapping[str, Optional[str]]) -> bool:
    keys = set(left) | set(right)
    return all(left.get(k) == right.get(k) for k in keys)
