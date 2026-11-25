from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping

from .errors import CeremonyInputError

TimeValue = Any  # float or datetime; kept generic so ISO strings and numbers both work


@dataclass(frozen=True)
class TimeWindow:
    start: TimeValue
    end: TimeValue


@dataclass(frozen=True)
class Person:
    id: str
    index: int
    management_line: str | None
    eligible_slot_ids: frozenset[str] | None
    windows: tuple[TimeWindow, ...]
    time_constrained: bool
    allow_rounds: frozenset[Any] | None
    allow_round_indices: frozenset[int]
    deny_rounds: frozenset[Any]
    deny_round_indices: frozenset[int]


@dataclass(frozen=True)
class Slot:
    id: str
    index: int
    key_id: str | None
    seat: str | None
    eligible_person_ids: frozenset[str] | None


@dataclass
class Round:
    index: int
    id: str
    slot_ids: tuple[str, ...]
    time: TimeValue | None
    start: TimeValue | None
    end: TimeValue | None
    slot_index: dict[str, int] = field(default_factory=dict)


@dataclass
class CeremonySpec:
    raw: Mapping[str, Any]
    people: tuple[Person, ...]
    slots: tuple[Slot, ...]
    rounds: tuple[Round, ...]
    conflict_masks: tuple[int, ...]
    conflict_pairs: tuple[tuple[str, str], ...]
    key_slot_ids: dict[str, tuple[str, str]]
    slot_by_id: dict[str, Slot]
    person_by_id: dict[str, Person]

    @property
    def person_count(self) -> int:
        return len(self.people)

    def person_available(self, person: Person, round_: Round) -> bool:
        # Explicit allow/deny lists.  Integer entries in ``available_rounds``
        # are one-based round numbers; ``available_round_indices`` is 0-based.
        if person.allow_rounds is not None:
            allowed = round_.id in person.allow_rounds or (round_.index + 1) in person.allow_rounds
            if not allowed:
                return False
        if round_.index in person.allow_round_indices:
            pass
        elif person.allow_round_indices:
            return False

        if round_.id in person.deny_rounds or (round_.index + 1) in person.deny_rounds:
            return False
        if round_.index in person.deny_round_indices:
            return False

        if person.time_constrained:
            if round_.time is not None:
                if not any(w.start <= round_.time <= w.end for w in person.windows):
                    return False
            elif round_.start is not None and round_.end is not None:
                if not any(
                    w.start <= round_.start and round_.end <= w.end for w in person.windows
                ):
                    return False
            # A round without a time/window cannot be rejected by clock
            # availability; explicit round lists still apply.
        return True

    def availability_mask(self, round_: Round) -> int:
        mask = 0
        for person in self.people:
            if self.person_available(person, round_):
                mask |= 1 << person.index
        return mask

    def slot_person_eligible(self, person: Person, slot: Slot) -> bool:
        if person.eligible_slot_ids is not None and slot.id not in person.eligible_slot_ids:
            return False
        if slot.eligible_person_ids is not None and person.id not in slot.eligible_person_ids:
            return False
        return True


def _first(d: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in d and d[name] is not None:
            return d[name]
    return None


def _as_id(value: Any, what: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CeremonyInputError(f"{what} must be a string or integer ID, got {value!r}")
    text = str(value).strip()
    if not text:
        raise CeremonyInputError(f"{what} must not be empty")
    return text


def _coerce_time(value: Any, what: str) -> TimeValue:
    if isinstance(value, bool):
        raise CeremonyInputError(f"{what} has invalid boolean time {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, dt_time.min)
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
            try:
                return datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise CeremonyInputError(f"{what} is not a valid time: {value!r}") from exc
    raise CeremonyInputError(f"{what} has unsupported time type: {type(value).__name__}")


def _parse_windows(value: Any) -> tuple[TimeWindow, ...]:
    if value is None:
        return ()
    raw_windows = value if isinstance(value, (list, tuple)) else [value]
    windows: list[TimeWindow] = []
    for item in raw_windows:
        if isinstance(item, Mapping):
            start = _first(item, "start", "from", "begin")
            end = _first(item, "end", "until", "to")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            start, end = item
        else:
            raise CeremonyInputError(
                "availability windows must be {start,end} objects or [start,end] pairs"
            )
        if start is None or end is None:
            raise CeremonyInputError("availability window requires start and end")
        s = _coerce_time(start, "availability start")
        e = _coerce_time(end, "availability end")
        if s > e:
            raise CeremonyInputError(f"availability window starts after it ends: {start!r}>{end!r}")
        windows.append(TimeWindow(s, e))
    return tuple(windows)


def _as_frozenset(value: Any) -> frozenset[Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value in {"*", "all", "ANY", "any"}:
            return None
        return frozenset({value})
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return frozenset({value})
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(value)
    raise CeremonyInputError(f"expected a list of IDs or '*', got {value!r}")


def _normalize_rounds(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    rounds_value = _first(raw, "rounds", "ceremony_rounds", "schedule_rounds")
    if rounds_value is None:
        raise CeremonyInputError("specification is missing required 'rounds'")

    if isinstance(rounds_value, Mapping):
        items: list[dict[str, Any]] = []
        for round_id, body in rounds_value.items():
            if isinstance(body, (list, tuple)):
                items.append({"id": round_id, "required_slots": list(body)})
            elif isinstance(body, Mapping):
                item = dict(body)
                item.setdefault("id", round_id)
                items.append(item)
            else:
                raise CeremonyInputError(f"round {round_id!r} must be a list or object")
        return items

    if not isinstance(rounds_value, list):
        raise CeremonyInputError("'rounds' must be a list or mapping")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(rounds_value):
        if isinstance(item, (list, tuple)):
            normalized.append({"index": index, "required_slots": list(item)})
        elif isinstance(item, Mapping):
            normalized.append(dict(item))
        else:
            raise CeremonyInputError(f"round {index} must be a slot list or object")
    return normalized


def load_spec(data: Mapping[str, Any] | str | bytes | Path) -> CeremonySpec:
    """Parse and validate a ceremony specification.

    ``data`` may be a dict, JSON string/bytes, or a :class:`~pathlib.Path` to a
    JSON file.  Only standard-library JSON parsing is used.
    """

    if isinstance(data, Path):
        with data.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    elif isinstance(data, (str, bytes)):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise CeremonyInputError(f"invalid JSON specification: {exc}") from exc

    if not isinstance(data, Mapping):
        raise CeremonyInputError("specification must be a JSON object/dict")

    raw_people = _first(data, "people", "custodians", "persons")
    if not isinstance(raw_people, list):
        raise CeremonyInputError("'people' must be a list (at most 12)")
    if len(raw_people) > 12:
        raise CeremonyInputError(f"at most 12 people are supported, got {len(raw_people)}")
    for index, raw_person in enumerate(raw_people):
        if not isinstance(raw_person, Mapping):
            raise CeremonyInputError(f"person {index} must be an object")

    # Sorting by ID makes tie-breaking independent of JSON insertion order.
    raw_people = sorted(raw_people, key=lambda p: _as_id(_first(p, "id", "person_id", "name"), "person id"))

    people: list[Person] = []
    person_ids: set[str] = set()
    person_refs_for_conflicts: list[tuple[str, list[Any]]] = []

    for index, raw_person in enumerate(raw_people):
        if not isinstance(raw_person, Mapping):
            raise CeremonyInputError(f"person {index} must be an object")
        pid = _as_id(_first(raw_person, "id", "person_id", "name"), "person id")
        if pid in person_ids:
            raise CeremonyInputError(f"duplicate person id: {pid}")
        person_ids.add(pid)

        line = _first(raw_person, "management_line", "line", "management_chain", "manager_line")
        if isinstance(line, Mapping):
            line = _as_id(_first(line, "id", "name"), "management line id")
        elif line is not None:
            line = str(line)

        eligible = _as_frozenset(
            _first(raw_person, "eligible_slots", "can_hold_slots", "custody_slots", "slots")
        )
        if eligible is not None:
            eligible = frozenset(_as_id(x, "eligible slot id") for x in eligible)

        availability_value = _first(
            raw_person, "availability", "available_windows", "windows", "available_times"
        )
        windows = _parse_windows(availability_value)
        time_constrained = availability_value is not None

        allow_value = _first(raw_person, "available_rounds", "available_round_ids", "rounds")
        allow_raw = _as_frozenset(allow_value)
        allow_rounds: frozenset[Any] | None = None
        allow_indices: set[int] = set()
        if allow_raw is not None:
            string_refs = set()
            for ref in allow_raw:
                if isinstance(ref, int) and not isinstance(ref, bool):
                    # Bare integer refs are one-based round numbers in data.
                    string_refs.add(ref)
                else:
                    string_refs.add(_as_id(ref, "available round id"))
            allow_rounds = frozenset(string_refs)

        explicit_indices = _as_frozenset(
            _first(raw_person, "available_round_indices", "available_indexes")
        )
        if explicit_indices:
            allow_indices.update(int(x) for x in explicit_indices)

        deny_raw = _as_frozenset(_first(raw_person, "unavailable_rounds", "blocked_rounds"))
        deny_rounds: set[Any] = set()
        if deny_raw:
            for ref in deny_raw:
                if isinstance(ref, int) and not isinstance(ref, bool):
                    deny_rounds.add(ref)
                else:
                    deny_rounds.add(_as_id(ref, "unavailable round id"))
        deny_indices_raw = _as_frozenset(
            _first(raw_person, "unavailable_round_indices", "blocked_round_indices")
        )
        deny_indices = frozenset(int(x) for x in deny_indices_raw) if deny_indices_raw else frozenset()

        proxy_refs = _first(raw_person, "proxies", "proxy_for", "substitutes", "deputies", "backup_for")
        proxy_list: list[Any] = []
        if isinstance(proxy_refs, (list, tuple, set, frozenset)):
            proxy_list.extend(proxy_refs)
        elif proxy_refs is not None:
            proxy_list.append(proxy_refs)
        person_refs_for_conflicts.append((pid, proxy_list))

        people.append(
            Person(
                id=pid,
                index=index,
                management_line=line,
                eligible_slot_ids=eligible,
                windows=windows,
                time_constrained=time_constrained,
                allow_rounds=allow_rounds,
                allow_round_indices=frozenset(allow_indices),
                deny_rounds=frozenset(deny_rounds),
                deny_round_indices=deny_indices,
            )
        )

    person_by_id = {p.id: p for p in people}

    raw_rounds = _normalize_rounds(data)
    rounds: list[Round] = []
    round_ids: set[str] = set()
    referenced_slot_ids: set[str] = set()

    for index, raw_round in enumerate(raw_rounds):
        rid = _as_id(
            _first(raw_round, "id", "round_id", "name") or f"r{index + 1}",
            "round id",
        )
        if rid in round_ids:
            raise CeremonyInputError(f"duplicate round id: {rid}")
        round_ids.add(rid)

        raw_slots = _first(raw_round, "required_slots", "slots", "seats", "present_slots")
        if raw_slots is None:
            raise CeremonyInputError(f"round {rid} is missing required_slots")
        if not isinstance(raw_slots, (list, tuple)):
            raise CeremonyInputError(f"round {rid} required_slots must be a list")

        slot_ids: list[str] = []
        seen: set[str] = set()
        for raw_slot in raw_slots:
            if isinstance(raw_slot, Mapping):
                sid = _as_id(_first(raw_slot, "slot", "slot_id", "id"), "round slot id")
            else:
                sid = _as_id(raw_slot, "round slot id")
            if sid in seen:
                raise CeremonyInputError(f"round {rid} lists slot {sid} more than once")
            seen.add(sid)
            slot_ids.append(sid)
            referenced_slot_ids.add(sid)

        time_value = _first(raw_round, "time", "at", "instant")
        time_parsed = _coerce_time(time_value, f"round {rid} time") if time_value is not None else None
        start_value = _first(raw_round, "start", "start_time", "from")
        end_value = _first(raw_round, "end", "end_time", "to", "until")
        start_parsed = _coerce_time(start_value, f"round {rid} start") if start_value is not None else None
        end_parsed = _coerce_time(end_value, f"round {rid} end") if end_value is not None else None

        rounds.append(
            Round(
                index=index,
                id=rid,
                slot_ids=tuple(slot_ids),
                time=time_parsed,
                start=start_parsed,
                end=end_parsed,
                slot_index={sid: i for i, sid in enumerate(slot_ids)},
            )
        )

    # Parse declared slots, then synthesize bare slot records for references.
    raw_slots_value = _first(data, "slots", "custody_slots", "slot_definitions")
    slots_were_declared = raw_slots_value is not None
    raw_slot_items: list[Mapping[str, Any]] = []
    if isinstance(raw_slots_value, Mapping):
        for sid, body in raw_slots_value.items():
            if isinstance(body, Mapping):
                item = dict(body)
                item.setdefault("id", sid)
                raw_slot_items.append(item)
            else:
                raw_slot_items.append({"id": sid})
    elif isinstance(raw_slots_value, list):
        for item in raw_slots_value:
            if isinstance(item, Mapping):
                raw_slot_items.append(item)
            else:
                raw_slot_items.append({"id": item})
    elif raw_slots_value is not None:
        raise CeremonyInputError("'slots' must be a list or mapping")

    slots: list[Slot] = []
    slot_ids: set[str] = set()
    for raw_slot in raw_slot_items:
        sid = _as_id(_first(raw_slot, "id", "slot_id", "name"), "slot id")
        if sid in slot_ids:
            raise CeremonyInputError(f"duplicate slot id: {sid}")
        slot_ids.add(sid)
        key_id = _first(raw_slot, "key_id", "key", "keyId")
        key_id = str(key_id) if key_id is not None else None
        seat = _first(raw_slot, "seat", "seat_id", "holder", "seatId")
        seat = str(seat) if seat is not None else None
        eligible_people = _as_frozenset(
            _first(raw_slot, "eligible_people", "allowed_people", "holders", "custodians")
        )
        if eligible_people is not None:
            eligible_people = frozenset(_as_id(x, "eligible person id") for x in eligible_people)
        if ":" in sid and key_id is None:
            inferred_key, inferred_seat = sid.rsplit(":", 1)
            key_id = inferred_key or None
            seat = seat or inferred_seat
        slots.append(
            Slot(
                id=sid,
                index=len(slots),
                key_id=key_id,
                seat=seat,
                eligible_person_ids=eligible_people,
            )
        )

    explicit_slot_ids = set(slot_ids)
    missing_slot_ids = sorted(referenced_slot_ids - slot_ids)
    for sid in missing_slot_ids:
        key_id = None
        seat = None
        if ":" in sid:
            key_id, seat = sid.rsplit(":", 1)
        slots.append(Slot(id=sid, index=len(slots), key_id=key_id or None, seat=seat, eligible_person_ids=None))
        slot_ids.add(sid)

    slot_by_id = {s.id: s for s in slots}
    for sid in referenced_slot_ids:
        if sid not in slot_by_id:  # defensive; synthesis above guarantees it
            raise CeremonyInputError(f"unknown slot id: {sid}")

    # Apply explicit key configuration and seat order.
    raw_keys = _first(data, "keys", "dual_control_keys")
    key_configs: list[Mapping[str, Any]] = []
    if isinstance(raw_keys, Mapping):
        for kid, body in raw_keys.items():
            if isinstance(body, Mapping):
                item = dict(body)
                item.setdefault("id", kid)
                key_configs.append(item)
            else:
                key_configs.append({"id": kid, "slots": body})
    elif isinstance(raw_keys, list):
        key_configs = [k for k in raw_keys if isinstance(k, Mapping)]
    elif raw_keys is not None:
        raise CeremonyInputError("'keys' must be a list or mapping")

    key_seat_order: dict[str, list[str]] = {}
    key_declared_slot_ids: set[str] = set()
    mutable_slots = {s.id: Slot(**{**s.__dict__}) for s in slots}
    for raw_key in key_configs:
        kid = _as_id(_first(raw_key, "id", "key_id", "name"), "key id")
        seat_ids = _first(raw_key, "slots", "seats", "seat_ids", "holders")
        if seat_ids is None:
            seat_a = _first(raw_key, "seat_a", "holder_a", "first_seat")
            seat_b = _first(raw_key, "seat_b", "holder_b", "second_seat")
            seat_ids = [x for x in (seat_a, seat_b) if x is not None]
        if not isinstance(seat_ids, (list, tuple)) or len(seat_ids) != 2:
            raise CeremonyInputError(f"key {kid} must define exactly two seat/slot IDs")
        ids = [_as_id(x, f"key {kid} seat") for x in seat_ids]
        if len(set(ids)) != 2:
            raise CeremonyInputError(f"key {kid} has duplicate seats")
        key_declared_slot_ids.update(ids)
        for position, sid in enumerate(ids):
            if sid not in mutable_slots:
                # Allow key config to declare a slot not listed in top-level slots.
                mutable_slots[sid] = Slot(
                    id=sid, index=len(mutable_slots), key_id=kid, seat=str(position + 1),
                    eligible_person_ids=None,
                )
            old = mutable_slots[sid]
            mutable_slots[sid] = Slot(
                id=old.id,
                index=old.index,
                key_id=kid,
                seat=old.seat if old.seat is not None else str(position + 1),
                eligible_person_ids=old.eligible_person_ids,
            )
        key_seat_order[kid] = ids

    slots = tuple(sorted(mutable_slots.values(), key=lambda s: s.index))
    slot_by_id = {s.id: s for s in slots}

    if slots_were_declared:
        for sid in referenced_slot_ids:
            if sid not in explicit_slot_ids and sid not in key_declared_slot_ids:
                raise CeremonyInputError(
                    f"round references slot not declared in 'slots' or 'keys': {sid}"
                )

    key_to_slots: dict[str, list[Slot]] = {}
    for slot in slots:
        if slot.key_id is not None:
            key_to_slots.setdefault(slot.key_id, []).append(slot)
    key_slot_ids: dict[str, tuple[str, str]] = {}
    for kid, key_slots in key_to_slots.items():
        if kid in key_seat_order:
            ordered_ids = key_seat_order[kid]
        else:
            key_slots_sorted = sorted(key_slots, key=lambda s: (s.seat is None, s.seat or "", s.id))
            ordered_ids = [s.id for s in key_slots_sorted]
        if len(ordered_ids) != 2:
            raise CeremonyInputError(
                f"dual-control key {kid!r} must have exactly two slots, found {len(ordered_ids)}"
            )
        key_slot_ids[kid] = (ordered_ids[0], ordered_ids[1])

    # Build the undirected conflict graph.
    conflict_pairs: set[tuple[str, str]] = set()

    def add_conflict(a: Any, b: Any) -> None:
        aid = _as_id(a, "conflict person id")
        bid = _as_id(b, "conflict person id")
        if aid not in person_by_id:
            raise CeremonyInputError(f"conflict references unknown person: {aid}")
        if bid not in person_by_id:
            raise CeremonyInputError(f"conflict references unknown person: {bid}")
        if aid == bid:
            return
        conflict_pairs.add(tuple(sorted((aid, bid))))  # type: ignore[arg-type]

    lines: dict[str, list[str]] = {}
    for person in people:
        if person.management_line:
            lines.setdefault(person.management_line, []).append(person.id)

    management_lines = _first(data, "management_lines", "line_groups")
    if isinstance(management_lines, Mapping):
        for line, ids in management_lines.items():
            lines.setdefault(str(line), []).extend(_as_id(x, "management line member") for x in ids)
    elif isinstance(management_lines, list):
        for group in management_lines:
            if isinstance(group, Mapping):
                line = _first(group, "line", "id", "name")
                ids = _first(group, "members", "people", "member_ids")
                if line is not None and ids:
                    lines.setdefault(str(line), []).extend(_as_id(x, "line member") for x in ids)

    for line, ids in lines.items():
        unique_ids = sorted(set(ids))
        for a, b in combinations(unique_ids, 2):
            add_conflict(a, b)

    for group_name in ("management_groups", "same_management_line_groups", "independence_groups"):
        groups = data.get(group_name)
        if groups is None:
            continue
        if not isinstance(groups, list):
            raise CeremonyInputError(f"{group_name} must be a list of person-ID lists")
        for group in groups:
            if not isinstance(group, (list, tuple)):
                raise CeremonyInputError(f"{group_name} entries must be lists")
            ids = [_as_id(x, "management group member") for x in group]
            for a, b in combinations(sorted(set(ids)), 2):
                add_conflict(a, b)

    pair_values: list[Any] = []
    for name in (
        "proxy_pairs",
        "proxies",
        "substitute_pairs",
        "backup_pairs",
        "deputy_pairs",
        "delegate_pairs",
        "替班关系",
    ):
        value = data.get(name)
        if value is not None:
            pair_values.append(value)

    for value in pair_values:
        if not isinstance(value, list):
            raise CeremonyInputError("proxy/substitute pair lists must be lists of [a,b] pairs")
        for pair in value:
            if isinstance(pair, Mapping):
                a = _first(pair, "a", "person_a", "first", "from")
                b = _first(pair, "b", "person_b", "second", "to")
            elif isinstance(pair, (list, tuple)) and len(pair) == 2:
                a, b = pair
            else:
                raise CeremonyInputError("each proxy pair must be [person_a, person_b]")
            add_conflict(a, b)

    for pid, refs in person_refs_for_conflicts:
        for ref in refs:
            add_conflict(pid, ref)

    conflict_masks = [0] * len(people)
    for a, b in conflict_pairs:
        ia = person_by_id[a].index
        ib = person_by_id[b].index
        conflict_masks[ia] |= 1 << ib
        conflict_masks[ib] |= 1 << ia

    # Slot eligibility references are validated only now.
    for slot in slots:
        if slot.eligible_person_ids:
            for pid in slot.eligible_person_ids:
                if pid not in person_by_id:
                    raise CeremonyInputError(f"slot {slot.id} references unknown person {pid}")
        if slot.key_id is not None and slot.key_id not in key_slot_ids:
            # key_slot_ids was built from all keyed slots, so impossible; keep guard.
            raise CeremonyInputError(f"slot {slot.id} has invalid key {slot.key_id}")

    # Use slot declaration order as the canonical tuple order.  This makes
    # identical assignments hash directly across rounds with the same slot set.
    for round_ in rounds:
        ordered_ids = tuple(sorted(round_.slot_ids, key=lambda sid: slot_by_id[sid].index))
        round_.slot_ids = ordered_ids
        round_.slot_index = {sid: i for i, sid in enumerate(ordered_ids)}

    return CeremonySpec(
        raw=data,
        people=tuple(people),
        slots=tuple(slots),
        rounds=tuple(rounds),
        conflict_masks=tuple(conflict_masks),
        conflict_pairs=tuple(sorted(conflict_pairs)),
        key_slot_ids=key_slot_ids,
        slot_by_id=slot_by_id,
        person_by_id=person_by_id,
    )


def load_spec_from_json_text(text: str) -> CeremonySpec:
    return load_spec(text)
