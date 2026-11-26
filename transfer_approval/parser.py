"""Input parsing and normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Hashable

from .intervals import Interval


class ApprovalInputError(ValueError):
    """Raised when the caller supplied malformed case data."""


SUBJECT_ALIASES = {
    "send": "send",
    "sender": "send",
    "s": "send",
    "sent": "send",
    "handover_send": "send",
    "receive": "receive",
    "receiver": "receive",
    "r": "receive",
    "recv": "receive",
    "received": "receive",
    "handover_receive": "receive",
}


def require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApprovalInputError(f"{name} must be an object")
    return value


def optional_mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    return require_mapping(value, name)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def normalize_subject(value: Any) -> str:
    if value is None:
        raise ApprovalInputError("subject/side is required")
    key = str(value).strip().lower().replace("-", "_")
    if key not in SUBJECT_ALIASES:
        # Common forms such as "sender-clock" are handled by callers only when
        # an explicit subject field is also present.
        raise ApprovalInputError(f"unknown transfer subject: {value!r}")
    return SUBJECT_ALIASES[key]


def parse_number(value: Any, name: str, *, minimum: float | int | None = None) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApprovalInputError(f"{name} must be a number")
    if minimum is not None and value < minimum:
        raise ApprovalInputError(f"{name} must be >= {minimum}")
    return value


def first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def parse_interval(
    value: Any,
    *,
    name: str = "interval",
    default_inclusive: bool = True,
) -> Interval:
    """Parse an Interval, dict, point, or ``[start, end]`` array."""

    if isinstance(value, Interval):
        return value
    if value is None:
        raise ApprovalInputError(f"{name} is required")

    if isinstance(value, dict):
        if value.get("empty") is True:
            return Interval.empty_interval()
        start = first_present(value, "start", "lo", "lower", "from", "begin")
        end = first_present(value, "end", "hi", "upper", "to", "until")
        if start is None or end is None:
            interval_value = value.get("interval") or value.get("range")
            if interval_value is not None:
                return parse_interval(interval_value, name=name)
            raise ApprovalInputError(f"{name} must contain start and end")

        mode = value.get("endpoint_mode") or value.get("mode")
        start_inclusive = value.get(
            "start_inclusive",
            value.get("lower_inclusive", value.get("inclusive_start", default_inclusive)),
        )
        end_inclusive = value.get(
            "end_inclusive",
            value.get("upper_inclusive", value.get("inclusive_end", default_inclusive)),
        )
        if mode == "closed" or mode == "inclusive":
            start_inclusive = end_inclusive = True
        elif mode == "open" or mode == "exclusive":
            start_inclusive = end_inclusive = False
        elif mode == "half_open" or mode == "left_closed":
            start_inclusive, end_inclusive = True, False
        elif mode == "left_open":
            start_inclusive, end_inclusive = False, True

        return Interval(
            parse_number(start, f"{name}.start"),
            parse_number(end, f"{name}.end"),
            bool(start_inclusive),
            bool(end_inclusive),
        )

    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            start, end = value
            return Interval(
                parse_number(start, f"{name}.start"),
                parse_number(end, f"{name}.end"),
                default_inclusive,
                default_inclusive,
            )
        if len(value) == 4:
            start, end, start_inclusive, end_inclusive = value
            return Interval(
                parse_number(start, f"{name}.start"),
                parse_number(end, f"{name}.end"),
                bool(start_inclusive),
                bool(end_inclusive),
            )
        raise ApprovalInputError(f"{name} array must have 2 or 4 entries")

    number = parse_number(value, name)
    return Interval.point(number)


def parse_clock_error(value: Any) -> tuple[float | int, float | int]:
    """Return ``(maximum_earlier_error, maximum_later_error)``."""

    if value is None:
        return 0, 0
    if isinstance(value, bool):
        raise ApprovalInputError("clock_error must be a number or object")
    if isinstance(value, (int, float)):
        parse_number(value, "clock_error", minimum=0)
        return value, value
    if isinstance(value, dict):
        earlier = first_present(
            value,
            "earlier",
            "negative",
            "minus",
            "backward",
            "past",
            "lower",
            "max_earlier",
        )
        later = first_present(
            value,
            "later",
            "positive",
            "plus",
            "forward",
            "future",
            "upper",
            "max_later",
        )
        symmetric = first_present(value, "symmetric", "both", "max", "error")
        if earlier is None and later is None and symmetric is not None:
            earlier = later = symmetric
        if earlier is None:
            earlier = 0
        if later is None:
            later = 0
        earlier = parse_number(earlier, "clock_error.earlier", minimum=0)
        later = parse_number(later, "clock_error.later", minimum=0)
        return earlier, later
    raise ApprovalInputError("clock_error must be a number or object")


@dataclass(frozen=True)
class KeyRef:
    """A normalized key-group / key-set selector."""

    group: Hashable | None = None
    key_ids: frozenset[Hashable] = field(default_factory=frozenset)
    groups: frozenset[Hashable] = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        return self.group is None and not self.key_ids and not self.groups

    def selector(self) -> "KeyRef":
        # A window may advertise several groups; its own ``groups`` field is an
        # acceptance list, not an additional requirement.
        return KeyRef(group=self.group, key_ids=self.key_ids)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.group is not None:
            result["key_group"] = self.group
        if self.key_ids:
            result["key_ids"] = sorted(self.key_ids, key=str)
        if self.groups:
            result["key_groups"] = sorted(self.groups, key=str)
        return result


def parse_key_ref(value: Any, *, name: str = "key") -> KeyRef | None:
    if value is None:
        return None
    if isinstance(value, KeyRef):
        return value
    if isinstance(value, str):
        return KeyRef(group=value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return KeyRef(key_ids=frozenset(value))
    if not isinstance(value, dict):
        raise ApprovalInputError(f"{name} must be a string, list, or object")

    group = first_present(value, "key_group", "group", "key_group_id")
    groups = as_list(first_present(value, "key_groups", "groups"))
    ids_value = first_present(value, "key_ids", "keys", "key_set", "key_id")
    if ids_value is None and "key_id" in value:
        ids_value = value["key_id"]
    key_ids = frozenset(as_list(ids_value)) if ids_value is not None else frozenset()

    if group is None and not groups and not key_ids:
        return None
    all_groups = set(groups)
    if group is not None:
        all_groups.add(group)
    return KeyRef(
        group=group,
        key_ids=key_ids,
        groups=frozenset(all_groups),
    )


def key_accepts(window_key: KeyRef | None, requested: KeyRef | None) -> bool:
    """Whether a window/key declaration accepts the requested key commitment."""

    if requested is None or requested.is_empty:
        # Without a selector, an explicitly advertised key/group is enough.
        return window_key is not None and not window_key.is_empty
    if window_key is None or window_key.is_empty:
        # Secure default: a window with no key declaration accepts nothing.
        return False

    if requested.group is not None:
        allowed = set(window_key.groups)
        if window_key.group is not None:
            allowed.add(window_key.group)
        allowed.update(window_key.groups)
        if requested.group not in allowed:
            return False

    if requested.key_ids:
        if requested.group is None:
            # Without a group assertion, a group-only window cannot prove that
            # the exact requested key set is accepted.
            if not window_key.key_ids:
                return False
            if not requested.key_ids.issubset(window_key.key_ids):
                return False
        elif window_key.key_ids and not requested.key_ids.issubset(window_key.key_ids):
            # A matching group plus explicit IDs is checked when the window
            # advertises individual IDs; group-only windows are accepted as
            # authorities for that group.
            return False

    return True
