"""Validated input model for the credential presentation planner.

The public entry point is :func:`parse_spec`, which turns a plain mapping
(typically decoded from JSON) into immutable dataclasses.  All validation is
local and defensive: malformed input raises :class:`SpecError`, never a
partially-constructed specification.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from typing import Any, Mapping, Sequence

MAX_DISCLOSURE_GROUPS = 16


class SpecError(ValueError):
    """Raised when a wallet specification is malformed."""


@dataclass(frozen=True)
class DisclosureGroup:
    """One indivisible unit of disclosure inside a credential."""

    id: str
    attributes: tuple[str, ...]
    credential_id: str
    issuer: str
    holder: str


@dataclass(frozen=True)
class Credential:
    """A verified credential owned by one holder."""

    id: str
    issuer: str
    holder: str
    groups: tuple[DisclosureGroup, ...]
    verified: bool = True


@dataclass(frozen=True)
class Alternative:
    """One substitutable attribute combination of a verification branch."""

    attributes: tuple[str, ...]
    # None means "inherit the branch-level acceptable issuers".
    acceptable_issuers: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Condition:
    """A verification branch (one thing the verifier must be convinced of)."""

    id: str
    alternatives: tuple[Alternative, ...]
    acceptable_issuers: tuple[str, ...] | None
    same_credential: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class WalletSpec:
    credentials: tuple[Credential, ...]
    conditions: tuple[Condition, ...]
    attribute_weights: Mapping[str, Fraction]

    def weight_of(self, attribute: str) -> Fraction:
        return self.attribute_weights.get(attribute, Fraction(1))


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecError(f"{where} must be an object")
    return value


def _sequence(value: Any, where: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise SpecError(f"{where} must be a list")
    return value


def _id_text(value: Any, where: str) -> str:
    """Normalise a credential/group/condition identifier to text.

    Integers are accepted because "credential number" is commonly numeric.
    Other identifiers must be non-empty strings.  Booleans are rejected even
    though Python treats them as integers.
    """

    if isinstance(value, bool) or value is None:
        raise SpecError(f"{where} must be a non-empty string or an integer")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if not value:
            raise SpecError(f"{where} must not be empty")
        return value
    raise SpecError(f"{where} must be a non-empty string or an integer")


def _attribute(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise SpecError(f"{where} must be a non-empty attribute name")
    return value


def _attribute_list(value: Any, where: str) -> tuple[str, ...]:
    items = _sequence(value, where)
    seen: set[str] = set()
    result: list[str] = []
    for index, item in enumerate(items):
        name = _attribute(item, f"{where}[{index}]")
        if name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def _non_negative_fraction(value: Any, where: str) -> Fraction:
    if isinstance(value, bool) or value is None:
        raise SpecError(f"{where} must be a non-negative finite number")
    if isinstance(value, int):
        if value < 0:
            raise SpecError(f"{where} must be non-negative")
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            raise SpecError(f"{where} must be a non-negative finite number")
        # str() keeps decimal literals such as 0.1 exact for our purposes.
        return Fraction(str(value))
    if isinstance(value, str):
        try:
            parsed = Fraction(value)
        except (ValueError, ZeroDivisionError):
            raise SpecError(f"{where} must be a number") from None
        if parsed < 0:
            raise SpecError(f"{where} must be non-negative")
        return parsed
    raise SpecError(f"{where} must be a non-negative finite number")


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise SpecError(f"{where} must be true or false")
    return value


def _issuers(value: Any, where: str) -> tuple[str, ...] | None:
    """Parse an issuer allow-list.

    ``None`` means "no restriction"; an empty list means "nothing is
    acceptable" and therefore makes the enclosing alternative unusable.
    A bare string is accepted as a one-element allow-list.
    """

    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    items = _sequence(value, where)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item:
            raise SpecError(f"{where}[{index}] must be a non-empty issuer name")
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _parse_alternative(value: Any, where: str) -> Alternative:
    if isinstance(value, Mapping):
        if "attributes" not in value:
            raise SpecError(f"{where} must contain 'attributes'")
        attributes = _attribute_list(value["attributes"], f"{where}.attributes")
        # ``None`` means "inherit the branch-level acceptable issuers"; the
        # planner intersects the two lists at plan time.
        issuers = (
            _issuers(value["acceptable_issuers"], f"{where}.acceptable_issuers")
            if "acceptable_issuers" in value
            else None
        )
    else:
        attributes = _attribute_list(value, where)
        issuers = None
    return Alternative(attributes=attributes, acceptable_issuers=issuers)


def _parse_same_credential(
    value: Any, where: str
) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    items = _sequence(value, where)
    if not items:
        return ()
    if all(isinstance(item, str) for item in items):
        raw_constraints: Sequence[Any] = [items]
    else:
        raw_constraints = items

    seen: set[frozenset[str]] = set()
    constraints: list[tuple[str, ...]] = []
    for index, raw in enumerate(raw_constraints):
        attributes = _attribute_list(raw, f"{where}[{index}]")
        if not attributes:
            raise SpecError(f"{where}[{index}] must not be empty")
        key = frozenset(attributes)
        if key not in seen:
            seen.add(key)
            constraints.append(attributes)
    return tuple(constraints)


def parse_spec(raw: Mapping[str, Any]) -> WalletSpec:
    """Validate and freeze a wallet specification.

    The top-level object accepts either ``conditions`` or ``branches`` as the
    name of the verification-branch list, but not both.
    """

    spec = _mapping(raw, "spec")
    if "conditions" in spec and "branches" in spec:
        raise SpecError("use either 'conditions' or 'branches', not both")
    conditions_raw = spec.get("conditions", spec.get("branches", []))
    credentials_raw = spec.get("credentials", [])
    if "attribute_weights" in spec and "weights" in spec:
        raise SpecError("use either 'attribute_weights' or 'weights', not both")
    weights_raw = spec.get("attribute_weights", spec.get("weights", {}))

    credentials: list[Credential] = []
    credential_ids: set[str] = set()
    group_ids: set[str] = set()
    total_groups = 0

    for c_index, raw_credential in enumerate(
        _sequence(credentials_raw, "credentials")
    ):
        where = f"credentials[{c_index}]"
        item = _mapping(raw_credential, where)
        for key in ("id", "issuer", "holder"):
            if key not in item:
                raise SpecError(f"{where} must contain '{key}'")
        credential_id = _id_text(item["id"], f"{where}.id")
        if credential_id in credential_ids:
            raise SpecError(f"duplicate credential id {credential_id!r}")
        credential_ids.add(credential_id)

        issuer = item["issuer"]
        holder = item["holder"]
        if not isinstance(issuer, str) or not issuer:
            raise SpecError(f"{where}.issuer must be a non-empty string")
        if not isinstance(holder, str) or not holder:
            raise SpecError(f"{where}.holder must be a non-empty string")
        verified = _bool(item.get("verified", True), f"{where}.verified")

        groups_raw = _sequence(item.get("groups", []), f"{where}.groups")
        groups: list[DisclosureGroup] = []
        for g_index, raw_group in enumerate(groups_raw):
            g_where = f"{where}.groups[{g_index}]"
            group_item = _mapping(raw_group, g_where)
            if "id" not in group_item or "attributes" not in group_item:
                raise SpecError(f"{g_where} must contain 'id' and 'attributes'")
            group_id = _id_text(group_item["id"], f"{g_where}.id")
            if group_id in group_ids:
                raise SpecError(f"duplicate disclosure group id {group_id!r}")
            group_ids.add(group_id)
            attributes = _attribute_list(
                group_item["attributes"], f"{g_where}.attributes"
            )
            groups.append(
                DisclosureGroup(
                    id=group_id,
                    attributes=attributes,
                    credential_id=credential_id,
                    issuer=issuer,
                    holder=holder,
                )
            )
            total_groups += 1

        credentials.append(
            Credential(
                id=credential_id,
                issuer=issuer,
                holder=holder,
                groups=tuple(groups),
                verified=verified,
            )
        )

    if total_groups > MAX_DISCLOSURE_GROUPS:
        raise SpecError(
            f"too many disclosure groups: {total_groups} "
            f"(limit is {MAX_DISCLOSURE_GROUPS})"
        )

    conditions: list[Condition] = []
    condition_ids: set[str] = set()
    for c_index, raw_condition in enumerate(
        _sequence(conditions_raw, "conditions")
    ):
        where = f"conditions[{c_index}]"
        item = _mapping(raw_condition, where)
        if "id" not in item or "alternatives" not in item:
            raise SpecError(f"{where} must contain 'id' and 'alternatives'")
        condition_id = _id_text(item["id"], f"{where}.id")
        if condition_id in condition_ids:
            raise SpecError(f"duplicate condition id {condition_id!r}")
        condition_ids.add(condition_id)

        branch_issuers = _issuers(
            item.get("acceptable_issuers"), f"{where}.acceptable_issuers"
        )

        alternatives_raw = item["alternatives"]
        alternatives_items = _sequence(alternatives_raw, f"{where}.alternatives")
        if not alternatives_items:
            raise SpecError(f"{where}.alternatives must not be empty")
        # Friendly shorthand: a flat list of attribute names is one alternative.
        if all(isinstance(entry, str) for entry in alternatives_items):
            alternatives_items = [alternatives_items]
        alternatives = tuple(
            _parse_alternative(raw_alt, f"{where}.alternatives[{a_index}]")
            for a_index, raw_alt in enumerate(alternatives_items)
        )

        if "same_credential" in item and "same_credential_groups" in item:
            raise SpecError(
                f"{where}: use either 'same_credential' or "
                "'same_credential_groups', not both"
            )
        same_raw = item.get(
            "same_credential", item.get("same_credential_groups")
        )
        same_credential = _parse_same_credential(same_raw, f"{where}.same_credential")

        conditions.append(
            Condition(
                id=condition_id,
                alternatives=alternatives,
                acceptable_issuers=branch_issuers,
                same_credential=same_credential,
            )
        )

    weights: dict[str, Fraction] = {}
    for attribute, value in _mapping(weights_raw, "attribute_weights").items():
        name = _attribute(attribute, "attribute_weights key")
        weights[name] = _non_negative_fraction(
            value, f"attribute_weights[{name!r}]"
        )

    return WalletSpec(
        credentials=tuple(credentials),
        conditions=tuple(conditions),
        attribute_weights=weights,
    )
