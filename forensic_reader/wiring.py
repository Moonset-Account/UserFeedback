"""Candidate channel wirings and Cartesian-product construction."""

from __future__ import annotations

import itertools
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .errors import WiringConfigurationError
from .reversal import NONE, normalize_reversal_mode

ChipId = Hashable


def _freeze_reversals(
    reversals: Mapping[ChipId, object] | None,
    chip_ids: Sequence[ChipId],
) -> Mapping[ChipId, str | tuple[str, ...]]:
    if reversals is None:
        return MappingProxyType({chip: NONE for chip in chip_ids})
    if not isinstance(reversals, Mapping):
        raise WiringConfigurationError("reversals must be a mapping from chip id to mode")
    unknown = [chip for chip in reversals if chip not in set(chip_ids)]
    if unknown:
        raise WiringConfigurationError(f"reversal settings reference unknown chip(s): {unknown!r}")
    normalized = {chip: normalize_reversal_mode(reversals.get(chip, NONE)) for chip in chip_ids}
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class Wiring:
    """One finite candidate wiring.

    ``order`` lists chip/channel identifiers in logical channel order.
    ``reversals`` maps each chip to one supported reversal mode.
    """

    order: tuple[ChipId, ...]
    id: str | None = None
    reversals: Mapping[ChipId, str | tuple[str, ...]] = field(default_factory=dict)
    label: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        order: Sequence[ChipId],
        id: str | None = None,  # noqa: A002 - mirrors public data vocabulary
        reversals: Mapping[ChipId, object] | object | None = None,
        label: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        reversal: object | None = None,
        name: str | None = None,
    ) -> None:
        if order is None:
            raise WiringConfigurationError("wiring order is required")
        order_tuple = tuple(order)
        if not order_tuple:
            raise WiringConfigurationError("wiring order must contain at least one chip")

        reversal_value = reversal if reversal is not None else reversals
        if isinstance(reversal_value, Mapping):
            reversal_map = reversal_value
        elif reversal_value is None:
            reversal_map = None
        else:
            # A scalar mode applies to every chip.
            reversal_map = {chip: reversal_value for chip in order_tuple}

        frozen_reversals = _freeze_reversals(reversal_map, order_tuple)
        normalized_id = None if (id is None and name is None) else str(id if id is not None else name)
        object.__setattr__(self, "order", order_tuple)
        object.__setattr__(self, "id", normalized_id)
        object.__setattr__(self, "reversals", frozen_reversals)
        object.__setattr__(self, "label", label or name)
        object.__setattr__(self, "metadata", MappingProxyType(dict(metadata or {})))

    def mode_for(self, chip_id: ChipId) -> str | tuple[str, ...]:
        return self.reversals.get(chip_id, NONE)

    def __hash__(self) -> int:
        # The generated dataclass equality includes mapping fields; this hash
        # uses stable immutable identity/order fields. Equal wirings therefore
        # always receive the same hash.
        return hash((self.id, self.order))

    def short_description(self) -> str:
        rev_parts = []
        for chip in self.order:
            mode = self.mode_for(chip)
            if mode != NONE and mode != (NONE,):
                if isinstance(mode, tuple):
                    mode_text = "+".join(mode)
                else:
                    mode_text = str(mode)
                rev_parts.append(f"{chip}:{mode_text}")
        rev_text = f", reversals={{{', '.join(rev_parts)}}}" if rev_parts else ""
        wid = self.id or "unlabeled"
        return f"{wid}(order={list(self.order)}{rev_text})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order": list(self.order),
            "reversals": {str(k): v for k, v in self.reversals.items()},
            "label": self.label,
            "metadata": dict(self.metadata),
        }


def _coerce_order(value: object, known_ids: set[ChipId] | None = None) -> tuple[ChipId, ...]:
    if isinstance(value, Wiring):
        return value.order
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise WiringConfigurationError("permutation/order must be a sequence of chip ids")
    order = tuple(value)
    if not order:
        raise WiringConfigurationError("permutation/order must not be empty")
    if len(set(order)) != len(order):
        raise WiringConfigurationError(f"permutation contains duplicate chip ids: {order!r}")
    if known_ids is not None:
        if set(order) != known_ids:
            raise WiringConfigurationError(
                f"order {order!r} must be a permutation of chip ids {sorted(map(str, known_ids))!r}"
            )
    return order


def coerce_wiring(
    value: Wiring | Mapping[str, Any] | Sequence[ChipId],
    *,
    index: int,
    chip_ids: Sequence[ChipId],
) -> Wiring:
    """Accept a :class:`Wiring`, dict, or bare order sequence."""

    known = set(chip_ids)
    if isinstance(value, Wiring):
        _coerce_order(value.order, known)
        # Ensure all known chips have an explicit normalized mode.
        missing = [chip for chip in chip_ids if chip not in value.reversals]
        if missing:
            object.__setattr__(
                value,
                "reversals",
                MappingProxyType({**dict(value.reversals), **{c: NONE for c in missing}}),
            )
        return value

    if isinstance(value, Mapping):
        order_value = (
            value.get("order")
            or value.get("channels")
            or value.get("permutation")
            or value.get("channel_order")
        )
        if order_value is None:
            raise WiringConfigurationError(f"wiring candidate #{index} has no order/channels")
        order = _coerce_order(order_value, known)
        reversal = (
            value.get("reversals")
            if value.get("reversals") is not None
            else value.get("reversal")
        )
        if reversal is None:
            reversal = value.get("byte_reversal", value.get("reverse"))
        return Wiring(
            order=order,
            id=value.get("id") or value.get("name") or f"W{index}",
            reversals=reversal if isinstance(reversal, Mapping) else None,
            reversal=None if isinstance(reversal, Mapping) else reversal,
            label=value.get("label"),
            metadata=value.get("metadata"),
        )

    order = _coerce_order(value, known)
    return Wiring(order=order, id=f"W{index}")


def _mode_options(spec: object, chips: Sequence[ChipId]) -> dict[ChipId, tuple[object, ...]]:
    """Normalize per-chip allowed modes into a list of options for each chip."""

    if spec is None:
        return {chip: (NONE,) for chip in chips}
    if isinstance(spec, Mapping):
        result: dict[ChipId, tuple[object, ...]] = {}
        known = set(chips)
        for chip in chips:
            if chip not in spec:
                result[chip] = (NONE,)
                continue
            allowed = spec[chip]
            if isinstance(allowed, (list, tuple, set, frozenset)):
                options = tuple(allowed)
            else:
                options = (allowed,)
            if not options:
                raise WiringConfigurationError(f"reversal mode list for chip {chip!r} is empty")
            result[chip] = tuple(normalize_reversal_mode(item) for item in options)
        unknown = [chip for chip in spec if chip not in known]
        if unknown:
            raise WiringConfigurationError(f"reversal_modes reference unknown chip(s): {unknown!r}")
        return result

    # A list or scalar shared by all chips.
    if isinstance(spec, (list, tuple, set, frozenset)):
        shared = tuple(spec)
    else:
        shared = (spec,)
    normalized_shared = tuple(normalize_reversal_mode(item) for item in shared)
    return {chip: normalized_shared for chip in chips}


def generate_wirings(
    chip_ids: Sequence[ChipId],
    *,
    permutations: Sequence[Sequence[ChipId]] | None = None,
    reversal_modes: Mapping[ChipId, object] | Sequence[object] | object | None = None,
) -> list[Wiring]:
    """Generate the Cartesian product of channel permutations and reversal choices."""

    if not chip_ids:
        raise WiringConfigurationError("at least one chip id is required")
    known = set(chip_ids)
    if permutations is None:
        orders = [tuple(p) for p in itertools.permutations(chip_ids)]
    else:
        orders = [_coerce_order(p, known) for p in permutations]
    if not orders:
        raise WiringConfigurationError("at least one channel permutation is required")

    options = _mode_options(reversal_modes, chip_ids)
    wirings: list[Wiring] = []
    index = 1
    for order in orders:
        option_lists = [options[chip] for chip in chip_ids]
        for combo in itertools.product(*option_lists):
            reversals = dict(zip(chip_ids, combo, strict=True))
            wirings.append(Wiring(order=order, id=f"W{index}", reversals=reversals))
            index += 1
    return wirings


def normalize_wirings(
    wirings: Sequence[Wiring | Mapping[str, Any] | Sequence[ChipId]] | None,
    chip_ids: Sequence[ChipId],
    *,
    permutations: Sequence[Sequence[ChipId]] | None = None,
    reversal_modes: object | None = None,
) -> list[Wiring]:
    """Normalize explicit candidates or generate them from permutation/mode records."""

    if wirings is None:
        return generate_wirings(
            chip_ids,
            permutations=permutations,
            reversal_modes=reversal_modes,
        )
    if isinstance(wirings, (Wiring, Mapping)):
        wirings = [wirings]  # type: ignore[list-item]
    elif (
        isinstance(wirings, Sequence)
        and not isinstance(wirings, (str, bytes))
        and len(wirings) > 0
        and all(
            not isinstance(item, (Wiring, Mapping))
            and isinstance(item, Hashable)
            and (isinstance(item, (str, bytes)) or not isinstance(item, Sequence))
            for item in wirings
        )
    ):
        # A bare single order such as ("A", "B") or [0, 1].  A list of orders
        # contains Sequence items and is therefore handled element-wise below.
        wirings = [wirings]  # type: ignore[list-item]

    result: list[Wiring] = []
    for index, item in enumerate(wirings, start=1):
        if isinstance(item, (Wiring, Mapping)):
            candidate = coerce_wiring(item, index=index, chip_ids=chip_ids)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            # Could be a bare order or a one-element candidate list? Treat as order.
            candidate = coerce_wiring(item, index=index, chip_ids=chip_ids)
        else:
            raise WiringConfigurationError(f"unsupported wiring candidate #{index}: {item!r}")
        if not candidate.id:
            object.__setattr__(candidate, "id", f"W{index}")
        result.append(candidate)

    ids = [w.id for w in result]
    if len(set(ids)) != len(ids):
        duplicates = sorted({wid for wid in ids if ids.count(wid) > 1})
        raise WiringConfigurationError(f"duplicate wiring id(s): {duplicates!r}")
    return result
