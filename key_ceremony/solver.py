from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import CeremonySpec, Round


@dataclass(frozen=True)
class KeyCheck:
    key_id: str
    old_slot_0: str
    old_slot_1: str
    new_slot_0: str
    new_slot_1: str
    op0: int
    op1: int
    cp0: int
    cp1: int


@dataclass(frozen=True)
class Boundary:
    from_index: int
    to_index: int
    common: tuple[tuple[int, int, str], ...]
    key_checks: tuple[KeyCheck, ...]
    added_slots: tuple[str, ...]
    removed_slots: tuple[str, ...]


@dataclass(frozen=True)
class HandoverPlan:
    key_id: str
    old_holders: tuple[str, str]
    new_holders: tuple[str, str]
    selected_old_holder: str
    eligible_old_holders: tuple[str, ...]
    participants: tuple[str, ...]
    trigger: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "old_holders": list(self.old_holders),
            "new_holders": list(self.new_holders),
            "selected_old_holder": self.selected_old_holder,
            "eligible_old_holders": list(self.eligible_old_holders),
            "participants": list(self.participants),
            "trigger": self.trigger,
        }


@dataclass(frozen=True)
class TransitionPlan:
    from_round_index: int
    to_round_index: int
    from_round_id: str
    to_round_id: str
    change_count: int
    changed_slots: tuple[str, ...]
    added_slots: tuple[str, ...]
    removed_slots: tuple[str, ...]
    handovers: tuple[HandoverPlan, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_round_index": self.from_round_index,
            "to_round_index": self.to_round_index,
            "from_round_id": self.from_round_id,
            "to_round_id": self.to_round_id,
            "change_count": self.change_count,
            "changed_slots": list(self.changed_slots),
            "added_slots": list(self.added_slots),
            "removed_slots": list(self.removed_slots),
            "handovers": [h.to_dict() for h in self.handovers],
        }


def build_independent_subsets(spec: CeremonySpec) -> tuple[bool, ...]:
    n = spec.person_count
    independent = [False] * (1 << n)
    independent[0] = True
    for mask in range(1, 1 << n):
        bit = mask & -mask
        person = bit.bit_length() - 1
        rest = mask ^ bit
        independent[mask] = bool(independent[rest]) and (spec.conflict_masks[person] & rest) == 0
    return tuple(independent)


def enumerate_round_assignments(spec: CeremonySpec, round_: Round) -> list[tuple[int, ...]]:
    """Enumerate every legal assignment for one round.

    The search uses bitmasks and minimum-remaining-values ordering.  With at
    most 12 people, all person masks fit in a small Python integer.
    """

    m = len(round_.slot_ids)
    if m == 0:
        return [()]

    available = spec.availability_mask(round_)
    domains: list[int] = []
    key_positions: dict[str, list[int]] = {}

    for pos, slot_id in enumerate(round_.slot_ids):
        slot = spec.slot_by_id[slot_id]
        mask = available
        for person in spec.people:
            if not spec.slot_person_eligible(person, slot):
                mask &= ~(1 << person.index)
        domains.append(mask)
        if slot.key_id is not None:
            key_positions.setdefault(slot.key_id, []).append(pos)

    results: list[tuple[int, ...]] = []
    values = [-1] * m

    def dfs(used_mask: int, assigned_count: int) -> None:
        if assigned_count == m:
            results.append(tuple(values))
            return

        best_pos = -1
        best_candidates = 0
        best_count = 10**9

        # MRV: choose the unassigned slot with fewest currently legal people.
        for pos in range(m):
            if values[pos] != -1:
                continue
            candidates = domains[pos] & ~used_mask
            key_id = spec.slot_by_id[round_.slot_ids[pos]].key_id
            if key_id is not None:
                for other_pos in key_positions.get(key_id, ()):  # type: ignore[arg-type]
                    other = values[other_pos]
                    if other != -1:
                        candidates &= ~spec.conflict_masks[other]
            count = candidates.bit_count()
            if count == 0:
                return
            if count < best_count:
                best_count = count
                best_pos = pos
                best_candidates = candidates

        candidates = best_candidates
        while candidates:
            bit = candidates & -candidates
            candidates ^= bit
            person_index = bit.bit_length() - 1
            values[best_pos] = person_index
            dfs(used_mask | bit, assigned_count + 1)
            values[best_pos] = -1

    dfs(0, 0)
    results.sort()
    return results


def build_boundary(spec: CeremonySpec, from_index: int, to_index: int) -> Boundary:
    old_round = spec.rounds[from_index]
    new_round = spec.rounds[to_index]

    common: list[tuple[int, int, str]] = []
    for new_pos, slot_id in enumerate(new_round.slot_ids):
        old_pos = old_round.slot_index.get(slot_id)
        if old_pos is not None:
            common.append((old_pos, new_pos, slot_id))

    old_set = set(old_round.slot_ids)
    new_set = set(new_round.slot_ids)
    key_checks: list[KeyCheck] = []
    for key_id in sorted(spec.key_slot_ids):
        slot_a, slot_b = spec.key_slot_ids[key_id]
        if not all(s in old_round.slot_index for s in (slot_a, slot_b)):
            continue
        if not all(s in new_round.slot_index for s in (slot_a, slot_b)):
            continue
        key_checks.append(
            KeyCheck(
                key_id=key_id,
                old_slot_0=slot_a,
                old_slot_1=slot_b,
                new_slot_0=slot_a,
                new_slot_1=slot_b,
                op0=old_round.slot_index[slot_a],
                op1=old_round.slot_index[slot_b],
                cp0=new_round.slot_index[slot_a],
                cp1=new_round.slot_index[slot_b],
            )
        )

    return Boundary(
        from_index=from_index,
        to_index=to_index,
        common=tuple(common),
        key_checks=tuple(key_checks),
        added_slots=tuple(s for s in new_round.slot_ids if s not in old_set),
        removed_slots=tuple(s for s in old_round.slot_ids if s not in new_set),
    )


def _handover_choice(
    check: KeyCheck,
    old_values: tuple[int, ...],
    new_values: tuple[int, ...],
    independent: tuple[bool, ...],
    trigger_mode: str,
) -> tuple[int, tuple[int, ...]] | None:
    o0 = old_values[check.op0]
    o1 = old_values[check.op1]
    n0 = new_values[check.cp0]
    n1 = new_values[check.cp1]

    seat_trigger = o0 != n0 and o1 != n1
    if trigger_mode == "disjoint_crew":
        trigger = seat_trigger and ({o0, o1}.isdisjoint({n0, n1}))
    else:
        trigger = seat_trigger
    if not trigger:
        return -1, ()

    new_mask = (1 << n0) | (1 << n1)
    eligible: list[int] = []
    for old_person in (o0, o1):
        mask = new_mask | (1 << old_person)
        if independent[mask]:
            eligible.append(old_person)
    if not eligible:
        return None
    selected = eligible[0]
    # Role order, deduplicated only when an old holder is also an incoming holder.
    participants: list[int] = []
    for person in (selected, n0, n1):
        if person not in participants:
            participants.append(person)
    return selected, tuple(participants)


def transition_cost(
    boundary: Boundary,
    old_values: tuple[int, ...],
    new_values: tuple[int, ...],
    independent: tuple[bool, ...],
    trigger_mode: str = "both_seats",
) -> int | None:
    cost = 0
    for op, cp, _slot_id in boundary.common:
        if old_values[op] != new_values[cp]:
            cost += 1

    for check in boundary.key_checks:
        choice = _handover_choice(check, old_values, new_values, independent, trigger_mode)
        if choice is None:
            return None
    return cost


def describe_transition(
    spec: CeremonySpec,
    boundary: Boundary,
    old_values: tuple[int, ...],
    new_values: tuple[int, ...],
    independent: tuple[bool, ...],
    trigger_mode: str = "both_seats",
) -> TransitionPlan:
    old_round = spec.rounds[boundary.from_index]
    new_round = spec.rounds[boundary.to_index]
    changed: list[str] = []
    for op, cp, slot_id in boundary.common:
        if old_values[op] != new_values[cp]:
            changed.append(slot_id)

    handovers: list[HandoverPlan] = []
    for check in boundary.key_checks:
        choice = _handover_choice(check, old_values, new_values, independent, trigger_mode)
        if choice is None:
            # The DP only accepts transitions for which this is not None.
            raise CeremonyTransitionError(
                f"internal error: incompatible handover for key {check.key_id}"
            )
        selected, participant_indices = choice
        if selected == -1:
            continue
        o0 = old_values[check.op0]
        o1 = old_values[check.op1]
        n0 = new_values[check.cp0]
        n1 = new_values[check.cp1]
        eligible_indices: list[int] = []
        new_mask = (1 << n0) | (1 << n1)
        for old_person in (o0, o1):
            if independent[new_mask | (1 << old_person)]:
                eligible_indices.append(old_person)

        handovers.append(
            HandoverPlan(
                key_id=check.key_id,
                old_holders=(spec.people[o0].id, spec.people[o1].id),
                new_holders=(spec.people[n0].id, spec.people[n1].id),
                selected_old_holder=spec.people[selected].id,
                eligible_old_holders=tuple(spec.people[i].id for i in eligible_indices),
                participants=tuple(spec.people[i].id for i in participant_indices),
                trigger=(
                    "both_seats_replaced"
                    if trigger_mode != "disjoint_crew"
                    else "all_holders_replaced"
                ),
            )
        )

    return TransitionPlan(
        from_round_index=boundary.from_index,
        to_round_index=boundary.to_index,
        from_round_id=old_round.id,
        to_round_id=new_round.id,
        change_count=len(changed),
        changed_slots=tuple(changed),
        added_slots=boundary.added_slots,
        removed_slots=boundary.removed_slots,
        handovers=tuple(handovers),
    )


class CeremonyTransitionError(RuntimeError):
    pass


def assignment_identity(round_: Round, values: tuple[int, ...]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((slot_id, value) for slot_id, value in zip(round_.slot_ids, values)))
