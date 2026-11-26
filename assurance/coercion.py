"""Input coercion helpers.

The public dataclasses give typed use, while the ``from_mapping`` helpers make
the module tolerant of small naming differences in locally synthesised rule
and fact data.  Coercion is deliberately conservative for security-relevant
values.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, TypeVar

from .timeutils import coerce_datetime, coerce_timedelta

T = TypeVar("T")

_CHINESE_LEVELS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


class AssuranceInputError(ValueError):
    """Raised when a fact, rule, or query cannot be interpreted."""


def pick(data: Any, *names: str, default: Any = None, nested: Iterable[str] = ()) -> Any:
    """Return the first non-None aliased key from a mapping.

    If ``nested`` is supplied, mappings such as ``conditions`` and
    ``restrictions`` are also searched.  This is only a compatibility layer;
    :class:`~assurance.models.RecognitionRule` remains the canonical API.
    """

    if not isinstance(data, Mapping):
        return default

    for name in names:
        if name in data and data[name] is not None:
            return data[name]

    for nested_name in nested:
        sub = data.get(nested_name)
        if isinstance(sub, Mapping):
            for name in names:
                if name in sub and sub[name] is not None:
                    return sub[name]

    return default


def require_pick(data: Any, *names: str, what: str) -> Any:
    value = pick(data, *names)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AssuranceInputError(f"missing required field for {what}: one of {', '.join(names)}")
    return value


def coerce_authority(value: Any) -> str:
    if value is None:
        raise AssuranceInputError("authority must not be None")
    text = str(value).strip()
    if not text:
        raise AssuranceInputError("authority must not be empty")
    return text


def coerce_level(value: Any, *, field_name: str = "level") -> int:
    if value is None:
        raise AssuranceInputError(f"{field_name} must not be None")
    if isinstance(value, bool):
        raise AssuranceInputError(f"invalid {field_name}: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise AssuranceInputError(f"{field_name} must be an integer level, got {value!r}")

    text = str(value).strip()
    if not text:
        raise AssuranceInputError(f"invalid {field_name}: {value!r}")

    if text in _CHINESE_LEVELS:
        return _CHINESE_LEVELS[text]

    # Common forms: L3, level-3, loa_3, 三级, assurance4.
    chinese = re.search(r"([零一二两三四五六七八九十])\s*级", text)
    if chinese:
        return _CHINESE_LEVELS[chinese.group(1)]

    number = re.search(r"-?\d+", text)
    if number:
        return int(number.group(0))

    raise AssuranceInputError(f"cannot parse {field_name}: {value!r}")


def optional_level(value: Any, *, field_name: str = "level") -> int | None:
    if value is None:
        return None
    return coerce_level(value, field_name=field_name)


def coerce_method_set(value: Any, *, required: bool = False) -> frozenset[str] | None:
    if value is None:
        if required:
            raise AssuranceInputError("verification method must not be None")
        return None

    if isinstance(value, str):
        items: Iterable[Any] = [value]
    elif isinstance(value, Mapping):
        # E.g. {"any_of": ["hardware", "totp"]}; canonical matching is OR.
        value = pick(value, "any_of", "in", "methods", default=value)
        if isinstance(value, str):
            items = [value]
        else:
            items = value if isinstance(value, Iterable) else [value]
    elif isinstance(value, Iterable):
        items = value
    else:
        items = [value]

    result: set[str] = set()
    for item in items:
        text = str(item).strip()
        if not text:
            raise AssuranceInputError("verification method names must not be empty")
        result.add(text)

    if required and not result:
        raise AssuranceInputError("at least one verification method is required")
    return frozenset(result)


def coerce_allow_set(value: Any, *, field_name: str = "allowed value") -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip() in {"*", "all", "ANY", "any"}:
            return None
        values: Iterable[Any] = [value]
    elif isinstance(value, Mapping):
        # Support {"in": ["a", "b"]} and {"any_of": [...]}.
        inner = pick(value, "in", "any_of", "values", "allowed")
        if inner is None:
            raise AssuranceInputError(f"invalid {field_name}: {value!r}")
        return coerce_allow_set(inner, field_name=field_name)
    elif isinstance(value, Iterable):
        values = value
    else:
        values = [value]

    result: set[str] = set()
    for item in values:
        text = str(item).strip()
        if not text:
            raise AssuranceInputError(f"{field_name} entries must not be empty")
        if text in {"*", "all", "ANY", "any"}:
            return None
        result.add(text)
    return frozenset(result)


def coerce_optional_datetime(value: Any, field_name: str) -> Any:
    if value is None:
        return None
    try:
        return coerce_datetime(value, field_name)
    except ValueError as exc:
        raise AssuranceInputError(str(exc)) from exc


def coerce_optional_duration(value: Any, field_name: str) -> Any:
    if value is None:
        return None
    try:
        return coerce_timedelta(value, field_name)
    except ValueError as exc:
        raise AssuranceInputError(str(exc)) from exc
