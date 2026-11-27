"""Presentation planner for a wallet of verified electronic credentials.

Semantics implemented here (see README for the full contract):

* A verification *condition* (branch) offers substitutable attribute
  *alternatives*.  An alternative is satisfied when disclosure groups from a
  single holder jointly cover all of its attributes, and every group used for
  it comes from an issuer accepted both by the alternative and by the branch.
* A disclosure group is indivisible.  Presenting a group that carries more
  attributes than an alternative requires is allowed, but every attribute in
  it counts towards exposure.
* Presenting the same group twice is one exposure: the objective is computed
  over the *set* of selected groups, never over how many conditions cite them.
* A presentation never mixes holders.  Each holder is solved separately and the
  best single-holder plan is returned.
* Optimization order: (1) total weight of the exposed attribute set,
  (2) fewest credentials, (3) stable natural order of credential numbers, then
  of group numbers.  Fewer disclosure groups is only a final refinement after
  every criterion the task names has tied.

The search is exact.  At most 16 disclosure groups are allowed in total, so
each holder has at most 2**16 group subsets.  For each condition a subset-zeta
transform marks every group set that contains a feasible option; intersecting
those marks over all conditions yields exactly the group sets that present a
valid witness for every condition at once.  Because feasibility is upward
closed, only inclusion-minimal feasible sets can be optimal; they are compared
with the lexicographic key described above.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
import math
import re
from typing import Any, Iterable, Mapping, Sequence

from .model import (
    Condition,
    WalletSpec,
    parse_spec,
)


@dataclass(frozen=True)
class GroupSelection:
    """One disclosure group that the plan presents."""

    group_id: str
    credential_id: str
    issuer: str
    holder: str
    attributes: tuple[str, ...]
    weight: Fraction

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "credential_id": self.credential_id,
            "issuer": self.issuer,
            "holder": self.holder,
            "attributes": list(self.attributes),
            "weight": str(self.weight),
        }


@dataclass(frozen=True)
class ConditionProvenance:
    """Where one verification condition's satisfaction came from."""

    condition_id: str
    satisfied: bool
    alternative_index: int | None
    required_attributes: tuple[str, ...]
    groups: tuple[str, ...]
    disclosed_attributes: tuple[str, ...]
    extra_attributes: tuple[str, ...]
    standalone_groups: tuple[str, ...]
    standalone_weight: Fraction | None
    standalone_credentials: tuple[str, ...]
    follows_standalone: bool
    shared_groups: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "satisfied": self.satisfied,
            "alternative_index": self.alternative_index,
            "required_attributes": list(self.required_attributes),
            "groups": list(self.groups),
            "disclosed_attributes": list(self.disclosed_attributes),
            "extra_attributes": list(self.extra_attributes),
            "standalone_groups": list(self.standalone_groups),
            "standalone_weight": (
                None if self.standalone_weight is None else str(self.standalone_weight)
            ),
            "standalone_credentials": list(self.standalone_credentials),
            "follows_standalone": self.follows_standalone,
            "shared_groups": list(self.shared_groups),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Plan:
    """The chosen presentation plus per-condition provenance."""

    satisfied: bool
    holder: str | None
    selected: tuple[GroupSelection, ...]
    exposed_attributes: tuple[str, ...]
    total_weight: Fraction
    credential_ids: tuple[str, ...]
    conditions: tuple[ConditionProvenance, ...]
    unmet_conditions: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def selected_group_ids(self) -> tuple[str, ...]:
        return tuple(item.group_id for item in self.selected)

    @property
    def credential_count(self) -> int:
        return len(self.credential_ids)

    @property
    def group_count(self) -> int:
        return len(self.selected)

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, **kwargs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "satisfied": self.satisfied,
            "holder": self.holder,
            "selected": [item.as_dict() for item in self.selected],
            "exposed_attributes": list(self.exposed_attributes),
            "total_weight": str(self.total_weight),
            "credential_ids": list(self.credential_ids),
            "conditions": [item.as_dict() for item in self.conditions],
            "unmet_conditions": list(self.unmet_conditions),
            "notes": list(self.notes),
        }


class UnsatisfiableError(ValueError):
    """Raised by :func:`require_plan` when no presentation satisfies the spec."""

    def __init__(self, plan: Plan):
        self.plan = plan
        unmet = ", ".join(plan.unmet_conditions) or "unknown"
        super().__init__(
            f"no presentation satisfies every condition; unmet: {unmet}"
        )


def natural_key(text: str) -> tuple[tuple[int, Any], ...]:
    """Order identifiers the way a human orders ``c2 < c10``."""

    parts = re.split(r"(\d+)", text)
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in parts
        if part != ""
    )


@dataclass(frozen=True)
class _Group:
    index: int  # global index == stable display rank
    id: str
    credential_id: str
    issuer: str
    holder: str
    attributes: tuple[str, ...]
    attr_mask: int
    verified: bool


class _Wallet:
    """Immutable, integer-indexed view of a specification."""

    def __init__(self, spec: WalletSpec):
        # The attribute universe includes everything a condition can mention,
        # so an undisclosed requirement stays a clean "unsatisfiable" result
        # instead of a KeyError.
        attributes: list[str] = []
        seen_attrs: set[str] = set()

        def remember(names: Iterable[str]) -> None:
            for name in names:
                if name not in seen_attrs:
                    seen_attrs.add(name)
                    attributes.append(name)

        for credential in spec.credentials:
            for group in credential.groups:
                remember(group.attributes)
        for condition in spec.conditions:
            for alternative in condition.alternatives:
                remember(alternative.attributes)
            for constraint in condition.same_credential:
                remember(constraint)

        self.attribute_index = {name: i for i, name in enumerate(attributes)}
        self.attributes = tuple(attributes)
        self.attr_weight = tuple(spec.weight_of(name) for name in attributes)

        # Comparisons use exact integers.  Weights are arbitrary non-negative
        # rationals, so scale every attribute weight by the common denominator;
        # sums then stay exact and are far faster than Fraction arithmetic.
        scale = 1
        for weight in self.attr_weight:
            scale = scale * weight.denominator // math.gcd(scale, weight.denominator)
        self.weight_scale = scale
        self.attr_weight_int = tuple(
            int(weight * scale) for weight in self.attr_weight
        )
        self._int_weight_cache: dict[int, int] = {0: 0}
        self._weight_cache: dict[int, Fraction] = {0: Fraction(0)}
        self._name_cache: dict[int, tuple[str, ...]] = {}

        placements = [
            (credential, group)
            for credential in spec.credentials
            for group in credential.groups
        ]
        placements.sort(
            key=lambda item: (
                natural_key(item[0].id),
                natural_key(item[1].id),
            )
        )

        groups: list[_Group] = []
        for index, (credential, group) in enumerate(placements):
            groups.append(
                _Group(
                    index=index,
                    id=group.id,
                    credential_id=credential.id,
                    issuer=credential.issuer,
                    holder=credential.holder,
                    attributes=group.attributes,
                    attr_mask=self.attr_mask(group.attributes),
                    verified=credential.verified,
                )
            )
        self.groups = tuple(groups)
        self.spec = spec

    def attr_mask(self, names: Iterable[str]) -> int:
        mask = 0
        for name in names:
            mask |= 1 << self.attribute_index[name]
        return mask

    def attr_names(self, mask: int) -> tuple[str, ...]:
        """Attribute names of a mask, sorted, so output is input-order free."""

        cached = self._name_cache.get(mask)
        if cached is None:
            cached = tuple(
                sorted(
                    (
                        self.attributes[i]
                        for i in range(len(self.attributes))
                        if mask >> i & 1
                    ),
                    key=natural_key,
                )
            )
            self._name_cache[mask] = cached
        return cached

    def mask_weight(self, attr_mask: int) -> Fraction:
        cached = self._weight_cache.get(attr_mask)
        if cached is None:
            cached = Fraction(self.mask_weight_int(attr_mask), self.weight_scale)
            self._weight_cache[attr_mask] = cached
        return cached

    def mask_weight_int(self, attr_mask: int) -> int:
        cached = self._int_weight_cache.get(attr_mask)
        if cached is None:
            low = attr_mask & -attr_mask
            index = low.bit_length() - 1
            cached = self.attr_weight_int[index] + self.mask_weight_int(attr_mask ^ low)
            self._int_weight_cache[attr_mask] = cached
        return cached

    def weight_of(self, attribute: str) -> Fraction:
        return self.attr_weight[self.attribute_index[attribute]]


class _HolderView:
    """One holder's groups with local indices ``0..m-1`` (m <= 16)."""

    def __init__(self, wallet: _Wallet, holder: str):
        self.wallet = wallet
        self.holder = holder
        # wallet.groups is already in stable display-rank order.
        self.groups = tuple(g for g in wallet.groups if g.holder == holder)
        self.m = len(self.groups)
        self.size = 1 << self.m
        self.group_attr = tuple(g.attr_mask for g in self.groups)
        self.group_credential = tuple(g.credential_id for g in self.groups)
        self.group_issuer = tuple(g.issuer for g in self.groups)
        self.group_verified = tuple(g.verified for g in self.groups)

        credentials: list[str] = []
        seen: set[str] = set()
        for credential_id in self.group_credential:
            if credential_id not in seen:
                seen.add(credential_id)
                credentials.append(credential_id)
        self.credentials = tuple(sorted(credentials, key=natural_key))
        cred_index = {cid: i for i, cid in enumerate(self.credentials)}

        attr_union = [0] * self.size
        cred_mask = [0] * self.size
        for mask in range(1, self.size):
            low = mask & -mask
            index = low.bit_length() - 1
            rest = mask ^ low
            attr_union[mask] = attr_union[rest] | self.group_attr[index]
            cred_mask[mask] = cred_mask[rest] | (
                1 << cred_index[self.group_credential[index]]
            )
        self._attr_union = attr_union
        self._cred_mask = cred_mask
        self._credential_bit_ids = tuple(
            (1 << i, cid) for i, cid in enumerate(self.credentials)
        )
        self._key_cache: dict[int, tuple[Any, ...]] = {}

    # -- derived facts ---------------------------------------------------
    def exposed(self, local_mask: int) -> tuple[str, ...]:
        return self.wallet.attr_names(self._attr_union[local_mask])

    def weight(self, local_mask: int) -> Fraction:
        return self.wallet.mask_weight(self._attr_union[local_mask])

    def credential_ids(self, local_mask: int) -> tuple[str, ...]:
        mask = self._cred_mask[local_mask]
        if not mask:
            return ()
        return tuple(cid for bit, cid in self._credential_bit_ids if mask & bit)

    def group_ids(self, local_mask: int) -> tuple[str, ...]:
        return tuple(
            self.groups[i].id for i in range(self.m) if local_mask >> i & 1
        )

    def selections(self, local_mask: int) -> tuple[GroupSelection, ...]:
        return tuple(
            GroupSelection(
                group_id=group.id,
                credential_id=group.credential_id,
                issuer=group.issuer,
                holder=group.holder,
                attributes=group.attributes,
                weight=self.wallet.mask_weight(group.attr_mask),
            )
            for index, group in enumerate(self.groups)
            if local_mask >> index & 1
        )

    def sort_key(self, local_mask: int) -> tuple[Any, ...]:
        """Lexicographic objective, in the priority order the task fixes.

        1. total weight of the exposed attribute set,
        2. number of credentials used,
        3. credential numbers (natural order), then group numbers.

        ``len(group_ids)`` only refines exact ties on the criteria the task
        names; it is a privacy-conservative extra step that makes the order a
        strict total order and keeps dominance pruning sound (a proper subset
        always presents strictly fewer groups).
        """

        cached = self._key_cache.get(local_mask)
        if cached is None:
            credential_ids = self.credential_ids(local_mask)
            group_ids = self.group_ids(local_mask)
            cached = (
                self.wallet.mask_weight_int(self._attr_union[local_mask]),
                len(credential_ids),
                tuple(natural_key(cid) for cid in credential_ids),
                len(group_ids),
                tuple(natural_key(gid) for gid in group_ids),
            )
            self._key_cache[local_mask] = cached
        return cached

    # -- conditions ------------------------------------------------------
    def options_for(self, condition: Condition) -> tuple[list["_Option"], list[str]]:
        return _condition_options(self, condition)


@dataclass(frozen=True)
class _Option:
    """One way to satisfy a condition: a local group mask plus its alternative."""

    alternative_index: int
    local_mask: int

    def sort_key(self, view: _HolderView) -> tuple[Any, ...]:
        return (*view.sort_key(self.local_mask), self.alternative_index)


def _allowed_issuers(
    condition: Condition, alternative_issuers: Sequence[str] | None
) -> frozenset[str] | None:
    """Intersect branch-level and alternative-level issuer allow-lists.

    ``None`` on both sides means "any issuer".  A present-but-empty list makes
    the alternative unusable, which is the safe reading of "acceptable issuers".
    """

    branch = condition.acceptable_issuers
    if branch is None:
        return None if alternative_issuers is None else frozenset(alternative_issuers)
    if alternative_issuers is None:
        return frozenset(branch)
    return frozenset(branch) & frozenset(alternative_issuers)


def _covering_sets(
    required_mask: int,
    candidates: Sequence[int],
    same_constraints: Sequence[int],
    view: _HolderView,
) -> list[frozenset[int]]:
    """All inclusion-minimal feasible subsets of ``candidates``.

    A subset is feasible when it covers ``required_mask`` and every
    same-credential constraint is met by the covered attributes of a single
    credential.  Minimality is inclusion minimality among feasible sets, so a
    constraint that forces a particular group is preserved.
    """

    results: list[tuple[int, ...]] = []
    group_attr = view.group_attr
    group_credential = view.group_credential

    def constraints_ok(chosen: Sequence[int]) -> bool:
        for constraint in same_constraints:
            need = constraint & required_mask
            if not need:
                continue
            per_credential: dict[str, int] = {}
            for group_index in chosen:
                key = group_credential[group_index]
                per_credential[key] = (
                    per_credential.get(key, 0) | group_attr[group_index]
                )
            if not any((need & mask) == need for mask in per_credential.values()):
                return False
        return True

    def walk(start: int, chosen: list[int], covered: int) -> None:
        if required_mask & ~covered == 0:
            if constraints_ok(chosen):
                # Coverage is complete and every constraint holds, so this set
                # is feasible.  Every superset is non-minimal, so stop here.
                results.append(tuple(chosen))
                return
            # Coverage is complete but a same-credential constraint is not met.
            # Only adding a group that carries the missing required attribute
            # on the right credential can fix it; those groups add no coverage,
            # so we must keep descending instead of returning.
        for position in range(start, len(candidates)):
            group_index = candidates[position]
            chosen.append(group_index)
            walk(position + 1, chosen, covered | group_attr[group_index])
            chosen.pop()

    walk(0, [], 0)

    unique: set[int] = set()
    for item in results:
        mask = 0
        for group_index in item:
            mask |= 1 << group_index
        unique.add(mask)
    return _minimal_masks(unique, view.m)


def _minimal_masks(masks: Iterable[int], bits: int) -> list[frozenset[int]]:
    """Keep only masks that have no proper subset in ``masks``.

    Uses a subset-zeta transform so the work stays O(bits * 2**bits) instead
    of comparing every pair.
    """

    collection = set(masks)
    if not collection:
        return []
    # Only the bits that some mask actually uses need a dimension.
    used_bits = 0
    for mask in collection:
        used_bits |= mask
    positions = [i for i in range(bits) if used_bits >> i & 1]
    sparse_bits = len(positions)

    def compress(mask: int) -> int:
        result = 0
        for slot, original in enumerate(positions):
            if mask >> original & 1:
                result |= 1 << slot
        return result

    def expand(mask: int) -> int:
        result = 0
        for slot, original in enumerate(positions):
            if mask >> slot & 1:
                result |= 1 << original
        return result

    compressed = {compress(mask) for mask in collection}
    size = 1 << sparse_bits
    has_subset = bytearray(size)
    for mask in compressed:
        has_subset[mask] = 1
    for bit in (1 << i for i in range(sparse_bits)):
        for mask in range(size):
            if mask & bit:
                has_subset[mask] |= has_subset[mask ^ bit]

    if len(compressed) <= 1:
        return [frozenset(_mask_indices(expand(mask))) for mask in compressed]

    kept: list[frozenset[int]] = []
    for mask in sorted(compressed):
        bits_left = mask
        has_proper = False
        while bits_left:
            low = bits_left & -bits_left
            if has_subset[mask ^ low]:
                has_proper = True
                break
            bits_left ^= low
        if not has_proper:
            kept.append(frozenset(_mask_indices(expand(mask))))
    return kept


def _mask_indices(mask: int) -> tuple[int, ...]:
    indices: list[int] = []
    while mask:
        low = mask & -mask
        indices.append(low.bit_length() - 1)
        mask ^= low
    return tuple(indices)


def _condition_options(
    view: _HolderView, condition: Condition
) -> tuple[list[_Option], list[str]]:
    """Every way this condition can be satisfied for one holder."""

    options: list[_Option] = []
    diagnostics: list[str] = []
    same_constraints = [
        view.wallet.attr_mask(names) for names in condition.same_credential
    ]

    for alt_index, alternative in enumerate(condition.alternatives):
        required_mask = view.wallet.attr_mask(alternative.attributes)
        if required_mask == 0:
            options.append(_Option(alt_index, 0))
            continue

        allowed = _allowed_issuers(condition, alternative.acceptable_issuers)
        eligible = [
            index
            for index in range(view.m)
            if view.group_verified[index]
            and (allowed is None or view.group_issuer[index] in allowed)
            and (view.group_attr[index] & required_mask)
        ]
        if not eligible:
            if allowed is not None:
                diagnostics.append(
                    f"alternative {alt_index} "
                    f"({', '.join(alternative.attributes)}) has no verified "
                    f"group from an acceptable issuer {sorted(allowed)!r}"
                )
            else:
                diagnostics.append(
                    f"alternative {alt_index} "
                    f"({', '.join(alternative.attributes)}) is not disclosed by "
                    "any verified group"
                )
            continue

        covers = _covering_sets(required_mask, eligible, same_constraints, view)
        if not covers:
            constraints = ", ".join(
                "{" + ", ".join(view.wallet.attr_names(mask)) + "}"
                for mask in same_constraints
                if mask & required_mask
            )
            if constraints:
                diagnostics.append(
                    f"alternative {alt_index} "
                    f"({', '.join(alternative.attributes)}) cannot keep "
                    f"same-credential constraint(s) {constraints}"
                )
            else:
                diagnostics.append(
                    f"alternative {alt_index} "
                    f"({', '.join(alternative.attributes)}) has no covering "
                    "group combination"
                )
            continue

        for cover in covers:
            local_mask = 0
            for group_index in cover:
                local_mask |= 1 << group_index
            options.append(_Option(alt_index, local_mask))
    return options, diagnostics


def _upward_closure(option_masks: Iterable[int], size: int, bits: int) -> bytearray:
    """mark[S] == 1 iff S contains at least one of ``option_masks``."""

    mark = bytearray(size)
    for mask in option_masks:
        mark[mask] = 1
    for bit in (1 << i for i in range(bits)):
        for mask in range(size):
            if mask & bit:
                mark[mask] |= mark[mask ^ bit]
    return mark


def _best_option(view: _HolderView, options: Sequence[_Option]) -> _Option | None:
    if not options:
        return None
    return min(options, key=lambda option: option.sort_key(view))


def _solve_holder(
    view: _HolderView,
) -> tuple[int, dict[str, list[_Option]]] | None:
    """Exact optimum for one holder.

    Returns the winning local group mask and the per-condition options, or
    ``None`` when this holder cannot satisfy every condition on its own.
    """

    options_by_condition: dict[str, list[_Option]] = {}
    for condition in view.wallet.spec.conditions:
        options, _ = view.options_for(condition)
        if not options:
            return None
        options_by_condition[condition.id] = options

    feasible = bytearray(b"\x01") * view.size
    for condition in view.wallet.spec.conditions:
        mark = _upward_closure(
            (option.local_mask for option in options_by_condition[condition.id]),
            view.size,
            view.m,
        )
        for mask in range(view.size):
            if not mark[mask]:
                feasible[mask] = 0

    # Feasibility is upward closed, so a feasible set that strictly contains
    # another feasible set can never win: it exposes at least as much weight
    # and uses at least as many credentials.  Only inclusion-minimal feasible
    # sets can be optimal.
    best_mask: int | None = None
    best_key: tuple[Any, ...] | None = None
    for mask in range(view.size):
        if not feasible[mask]:
            continue
        bits = mask
        dominated = False
        while bits:
            low = bits & -bits
            if feasible[mask ^ low]:
                dominated = True
                break
            bits ^= low
        if dominated:
            continue
        key = view.sort_key(mask)
        if best_key is None or key < best_key:
            best_key, best_mask = key, mask
    if best_mask is None:
        return None
    return best_mask, options_by_condition


def _attribution_key(
    view: _HolderView, option: _Option, required: Sequence[str]
) -> tuple[Any, ...]:
    """Rank feasible options when explaining an already-chosen group set.

    Prefer the explanation that discloses the least weight beyond the
    alternative's own requirements, then the smallest stand-alone footprint,
    then the earliest alternative and stable group order.  The presented group
    set is identical for every feasible option; only the story differs.
    """

    required_set = set(required)
    extra_weight = sum(
        (
            view.wallet.weight_of(name)
            for name in view.exposed(option.local_mask)
            if name not in required_set
        ),
        Fraction(0),
    )
    return (extra_weight, *option.sort_key(view))


def plan_presentation(spec: WalletSpec | Mapping[str, Any]) -> Plan:
    """Compute the optimal single-holder presentation.

    Accepts either an already-validated :class:`WalletSpec` or a plain mapping.
    An unsatisfiable specification yields ``satisfied=False`` with diagnostics
    rather than raising; use :func:`require_plan` for the raising variant.
    """

    if not isinstance(spec, WalletSpec):
        spec = parse_spec(spec)
    wallet = _Wallet(spec)

    if not spec.conditions:
        return Plan(
            satisfied=True,
            holder=None,
            selected=(),
            exposed_attributes=(),
            total_weight=Fraction(0),
            credential_ids=(),
            conditions=(),
            unmet_conditions=(),
            notes=("no verification conditions requested",),
        )

    holders = sorted({group.holder for group in wallet.groups}, key=natural_key)
    if not holders:
        provenance = tuple(
            ConditionProvenance(
                condition_id=condition.id,
                satisfied=False,
                alternative_index=None,
                required_attributes=(),
                groups=(),
                disclosed_attributes=(),
                extra_attributes=(),
                standalone_groups=(),
                standalone_weight=None,
                standalone_credentials=(),
                follows_standalone=False,
                shared_groups=(),
                reason="wallet contains no disclosure groups",
            )
            for condition in spec.conditions
        )
        return Plan(
            satisfied=False,
            holder=None,
            selected=(),
            exposed_attributes=(),
            total_weight=Fraction(0),
            credential_ids=(),
            conditions=provenance,
            unmet_conditions=tuple(condition.id for condition in spec.conditions),
            notes=("wallet contains no disclosure groups",),
        )

    views = {holder: _HolderView(wallet, holder) for holder in holders}
    best: tuple[Any, ...] | None = None
    best_view: _HolderView | None = None
    best_mask = 0
    best_options: dict[str, list[_Option]] = {}

    for holder in holders:
        view = views[holder]
        solved = _solve_holder(view)
        if solved is None:
            continue
        local_mask, options_by_condition = solved
        # Holder identity breaks an otherwise exact tie deterministically.
        key = (*view.sort_key(local_mask), natural_key(holder))
        if best is None or key < best:
            best = key
            best_view = view
            best_mask = local_mask
            best_options = options_by_condition

    if best_view is None:
        return _unsatisfiable_plan(views)

    return _build_plan(best_view, best_mask, best_options)


def _unsatisfiable_plan(views: Mapping[str, _HolderView]) -> Plan:
    """Explain, per condition, why no holder can satisfy it."""

    anything = next(iter(views.values()))
    provenance: list[ConditionProvenance] = []
    for condition in anything.wallet.spec.conditions:
        reasons: list[str] = []
        best_standalone: tuple[Any, _HolderView, _Option] | None = None
        for holder, view in views.items():
            options, diagnostics = view.options_for(condition)
            standalone = _best_option(view, options)
            if standalone is not None:
                key = view.sort_key(standalone.local_mask)
                if best_standalone is None or key < best_standalone[0]:
                    best_standalone = (key, view, standalone)
            for note in diagnostics:
                reasons.append(f"holder {holder}: {note}")
        standalone_groups: tuple[str, ...] = ()
        standalone_weight: Fraction | None = None
        standalone_credentials: tuple[str, ...] = ()
        if best_standalone is not None:
            _, view, option = best_standalone
            standalone_groups = view.group_ids(option.local_mask)
            standalone_weight = view.weight(option.local_mask)
            standalone_credentials = view.credential_ids(option.local_mask)
        provenance.append(
            ConditionProvenance(
                condition_id=condition.id,
                satisfied=False,
                alternative_index=None,
                required_attributes=(),
                groups=(),
                disclosed_attributes=(),
                extra_attributes=(),
                standalone_groups=standalone_groups,
                standalone_weight=standalone_weight,
                standalone_credentials=standalone_credentials,
                follows_standalone=False,
                shared_groups=(),
                reason="; ".join(dict.fromkeys(reasons))
                or "no acceptable group combination",
            )
        )

    return Plan(
        satisfied=False,
        holder=None,
        selected=(),
        exposed_attributes=(),
        total_weight=Fraction(0),
        credential_ids=(),
        conditions=tuple(provenance),
        unmet_conditions=tuple(item.condition_id for item in provenance),
        notes=(
            "no single holder can satisfy every condition; mixing holders is "
            "not allowed",
        ),
    )


def _build_plan(
    view: _HolderView,
    local_mask: int,
    options_by_condition: Mapping[str, Sequence[_Option]],
) -> Plan:
    attribution: dict[str, _Option] = {}
    for condition in view.wallet.spec.conditions:
        feasible = [
            option
            for option in options_by_condition[condition.id]
            if option.local_mask & ~local_mask == 0
        ]
        # ``_solve_holder`` proved at least one option is feasible.
        chosen = min(
            feasible,
            key=lambda option: _attribution_key(
                view,
                option,
                condition.alternatives[option.alternative_index].attributes,
            ),
        )
        attribution[condition.id] = chosen

    usage: dict[str, list[str]] = {}
    for condition in view.wallet.spec.conditions:
        for group_id in view.group_ids(attribution[condition.id].local_mask):
            usage.setdefault(group_id, []).append(condition.id)

    provenance: list[ConditionProvenance] = []
    for condition in view.wallet.spec.conditions:
        option = attribution[condition.id]
        standalone = _best_option(view, options_by_condition[condition.id])
        alternative = condition.alternatives[option.alternative_index]
        required = tuple(alternative.attributes)
        required_set = set(required)
        disclosed = view.exposed(option.local_mask)
        extra = tuple(name for name in disclosed if name not in required_set)
        group_ids = view.group_ids(option.local_mask)
        shared = tuple(gid for gid in group_ids if len(usage[gid]) > 1)
        follows_standalone = (
            standalone is not None and standalone.local_mask == option.local_mask
        )

        reason = (
            f"alternative {option.alternative_index} "
            f"({', '.join(required) or 'no attributes required'}) satisfied by "
            f"{', '.join(group_ids) or 'no groups'}"
        )
        if extra:
            reason += f"; discloses extra attributes: {', '.join(extra)}"
        if standalone is not None and not follows_standalone:
            reason += (
                "; on its own it would use "
                f"{', '.join(view.group_ids(standalone.local_mask))} "
                f"(weight {view.weight(standalone.local_mask)}), but the joint "
                "presentation has a lower total cost"
            )

        provenance.append(
            ConditionProvenance(
                condition_id=condition.id,
                satisfied=True,
                alternative_index=option.alternative_index,
                required_attributes=required,
                groups=group_ids,
                disclosed_attributes=disclosed,
                extra_attributes=extra,
                standalone_groups=(
                    view.group_ids(standalone.local_mask) if standalone else ()
                ),
                standalone_weight=(
                    view.weight(standalone.local_mask) if standalone else None
                ),
                standalone_credentials=(
                    view.credential_ids(standalone.local_mask) if standalone else ()
                ),
                follows_standalone=follows_standalone,
                shared_groups=shared,
                reason=reason,
            )
        )

    notes: list[str] = []
    shared_groups = sorted({gid for item in provenance for gid in item.shared_groups})
    if shared_groups:
        notes.append(
            "disclosure groups used by more than one condition (counted once): "
            + ", ".join(shared_groups)
        )
    if any(not item.follows_standalone for item in provenance):
        notes.append(
            "at least one condition is satisfied differently than its standalone "
            "optimum because the joint presentation has a lower total cost"
        )

    return Plan(
        satisfied=True,
        holder=view.holder,
        selected=view.selections(local_mask),
        exposed_attributes=view.exposed(local_mask),
        total_weight=view.weight(local_mask),
        credential_ids=view.credential_ids(local_mask),
        conditions=tuple(provenance),
        unmet_conditions=(),
        notes=tuple(notes),
    )


def require_plan(spec: WalletSpec | Mapping[str, Any]) -> Plan:
    """Like :func:`plan_presentation` but raises on an unsatisfiable spec."""

    plan = plan_presentation(spec)
    if not plan.satisfied:
        raise UnsatisfiableError(plan)
    return plan


def plan_from_dict(raw: Mapping[str, Any]) -> Plan:
    """Convenience wrapper accepting the raw JSON-shaped mapping."""

    return plan_presentation(parse_spec(raw))


__all__ = [
    "ConditionProvenance",
    "GroupSelection",
    "MAX_DISCLOSURE_GROUPS",
    "Plan",
    "SpecError",
    "UnsatisfiableError",
    "natural_key",
    "plan_from_dict",
    "plan_presentation",
    "require_plan",
]
