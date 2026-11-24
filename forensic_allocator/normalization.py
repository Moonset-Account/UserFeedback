"""Input normalization for dataclasses, mappings, and simple objects."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
from typing import Any

from .errors import ValidationError
from .models import Exhibit, Reviewer


def _raw_value(obj: Any, keys: tuple[str, ...], default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        for key in keys:
            if key in obj:
                return obj[key]
        return default
    for key in keys:
        if hasattr(obj, key):
            return getattr(obj, key)
    return default


def _nonempty_string(value: Any, field_name: str) -> str:
    if value is None:
        raise ValidationError(f"{field_name} is required")
    text = str(value).strip()
    if not text:
        raise ValidationError(f"{field_name} must be a non-empty string")
    return text


def _string_set(value: Any, field_name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    elif isinstance(value, Iterable):
        values = value
    else:
        raise ValidationError(f"{field_name} must be a string or iterable of strings")

    result: set[str] = set()
    for item in values:
        text = _nonempty_string(item, field_name)
        result.add(text)
    return frozenset(result)


def _capacity(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        # Reject floats because a fractional assignment capacity is not meaningful.
        raise ValidationError("reviewer capacity must be a non-negative integer")
    if value < 0:
        raise ValidationError("reviewer capacity must be non-negative")
    return value


def _priority(value: Any) -> int | float:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValidationError("exhibit priority must be numeric")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValidationError("exhibit priority must be finite")
        return value
    raise ValidationError("exhibit priority must be an integer or float")


def coerce_exhibit(obj: Exhibit | Mapping[str, Any] | Any) -> Exhibit:
    exhibit_id = _nonempty_string(
        _raw_value(obj, ("exhibit_id", "id", "证物ID", "证物编号", "编号")),
        "exhibit_id",
    )
    qualifications = _string_set(
        _raw_value(
            obj,
            (
                "required_qualifications",
                "required_quals",
                "qualifications",
                "资质",
                "所需资质",
            ),
            (),
        ),
        "required_qualifications",
    )
    source_group = _raw_value(
        obj,
        ("source_group", "homology_group", "same_source_group", "同源组", "证物同源组"),
    )
    if source_group is not None:
        source_group = _nonempty_string(source_group, "source_group")
    priority = _priority(_raw_value(obj, ("priority", "优先级"), 0))

    return Exhibit(
        exhibit_id=exhibit_id,
        required_qualifications=qualifications,
        source_group=source_group,
        priority=priority,  # type: ignore[arg-type]
    )


def coerce_reviewer(obj: Reviewer | Mapping[str, Any] | Any) -> Reviewer:
    reviewer_id = _nonempty_string(
        _raw_value(obj, ("reviewer_id", "id", "复核人ID", "编号")),
        "reviewer_id",
    )
    lab = _nonempty_string(
        _raw_value(obj, ("lab", "laboratory", "lab_name", "所属实验室", "实验室")),
        "lab",
    )
    qualifications = _string_set(
        _raw_value(obj, ("qualifications", "quals", "资质"), ()),
        "qualifications",
    )
    capacity = _capacity(
        _raw_value(obj, ("capacity", "max_items", "acceptable_count",
                         "receivable_count", "可接收件数", "名额", "容量"), 1)
    )
    name_value = _raw_value(obj, ("name", "reviewer_name", "姓名"))
    name = None if name_value is None else str(name_value).strip() or None
    exposed = _string_set(
        _raw_value(
            obj,
            (
                "exposed_source_groups",
                "contacted_source_groups",
                "conflict_source_groups",
                "接触同源组",
                "接触过的同源组",
                "回避同源组",
            ),
            (),
        ),
        "exposed_source_groups",
    )
    recused_exhibits = _string_set(
        _raw_value(
            obj,
            ("recused_exhibit_ids", "conflict_exhibit_ids", "回避证物", "回避证物ID"),
            (),
        ),
        "recused_exhibit_ids",
    )
    active_value = _raw_value(obj, ("active", "is_active", "在岗"), True)
    if not isinstance(active_value, bool):
        raise ValidationError("reviewer active flag must be a boolean")

    return Reviewer(
        reviewer_id=reviewer_id,
        lab=lab,
        qualifications=qualifications,
        capacity=capacity,
        name=name,
        exposed_source_groups=exposed,
        recused_exhibit_ids=recused_exhibits,
        active=active_value,
    )


def normalize_inputs(
    exhibits: Iterable[Exhibit | Mapping[str, Any]],
    reviewers: Iterable[Reviewer | Mapping[str, Any]],
) -> tuple[tuple[Exhibit, ...], tuple[Reviewer, ...]]:
    """Coerce and validate inputs, returning ID-sorted reviewers."""

    exhibit_tuple = tuple(coerce_exhibit(item) for item in exhibits)
    reviewer_tuple = tuple(coerce_reviewer(item) for item in reviewers)

    if not exhibit_tuple:
        raise ValidationError("at least one exhibit is required")
    if not reviewer_tuple:
        raise ValidationError("at least one reviewer is required")

    exhibit_ids = [item.exhibit_id for item in exhibit_tuple]
    reviewer_ids = [item.reviewer_id for item in reviewer_tuple]
    if len(set(exhibit_ids)) != len(exhibit_ids):
        raise ValidationError("exhibit_id values must be unique")
    if len(set(reviewer_ids)) != len(reviewer_ids):
        raise ValidationError("reviewer_id values must be unique")

    # Reviewer order affects only deterministic tie-breaking.
    reviewer_tuple = tuple(sorted(reviewer_tuple, key=lambda item: item.reviewer_id))
    return exhibit_tuple, reviewer_tuple
