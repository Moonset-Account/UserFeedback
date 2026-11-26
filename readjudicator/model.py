"""Data models and permissive input parsing for the readjudication engine."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

PRESENCE = "presence"
DEVICE = "device"

_MISSING = object()


class AdjudicationInputError(ValueError):
    """Raised when caller-supplied adjudication input is structurally invalid."""


def _string_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, Iterable):
        return frozenset(str(v) for v in value)
    raise AdjudicationInputError(f"expected a string or iterable of strings, got {type(value)!r}")


def _attribute_set(value: Any) -> frozenset[tuple[str, str]]:
    if value is None:
        return frozenset()
    if isinstance(value, Mapping):
        return frozenset((str(k), str(v)) for k, v in value.items())
    if isinstance(value, Iterable):
        result = []
        for item in value:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise AdjudicationInputError("attributes must be a mapping or (key, value) pairs")
            result.append((str(item[0]), str(item[1])))
        return frozenset(result)
    raise AdjudicationInputError("attributes must be a mapping or iterable of pairs")


def _authorization_map(value: Any, *, kind: str, uses: int | None) -> Mapping[str, int | None]:
    if value is None:
        if kind == PRESENCE:
            amount: int | None = uses if isinstance(uses, int) else None
            return MappingProxyType({"presence_authorization": amount})
        return MappingProxyType({})
    if isinstance(value, Mapping):
        result: dict[str, int | None] = {}
        for key, amount in value.items():
            if amount is not None:
                if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                    raise AdjudicationInputError("authorization amounts must be non-negative integers or null")
            result[str(key)] = amount
        return MappingProxyType(result)
    if isinstance(value, (int,)) and not isinstance(value, bool):
        if kind != PRESENCE:
            raise AdjudicationInputError("numeric authorization shorthand is only valid for presence proofs")
        return MappingProxyType({"presence_authorization": value})
    raise AdjudicationInputError("authorizations must be a mapping, integer, or null")


@dataclass(frozen=True)
class Requirement:
    """One required capability slot.

    ``count`` expands to that many interchangeable slots.  A proof with
    ``uses > 1`` may fill more than one slot.
    """

    capability: str
    name: str | None = None
    count: int = 1
    proof_kind: str | None = None
    identity: str | None = None
    allowed_identities: frozenset[str] = field(default_factory=frozenset)
    identity_matches_request: bool = False
    identity_must_differ_from_request: bool = False
    attributes: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.capability or not isinstance(self.capability, str):
            raise AdjudicationInputError("requirement.capability must be a non-empty string")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 1:
            raise AdjudicationInputError("requirement.count must be a positive integer")
        if self.proof_kind is not None and self.proof_kind not in (PRESENCE, DEVICE):
            # Custom kinds are allowed; only reject empty values.
            if not str(self.proof_kind):
                raise AdjudicationInputError("proof_kind must not be empty")
        allowed = set(_string_set(self.allowed_identities))
        if self.identity:
            allowed.add(self.identity)
        object.__setattr__(self, "allowed_identities", frozenset(allowed))
        object.__setattr__(self, "attributes", _attribute_set(self.attributes))
        object.__setattr__(self, "name", self.name or self.capability)
        if self.identity_matches_request and self.identity_must_differ_from_request:
            raise AdjudicationInputError("a requirement cannot both match and differ from the request identity")

    @property
    def label(self) -> str:
        return self.name or self.capability


@dataclass(frozen=True)
class Proof:
    """A verified proof token.

    Verification of signatures is intentionally outside this module.  The
    engine enforces validity windows, identity/attribute constraints, remaining
    uses, authorizations, and relay policy.
    """

    proof_id: str
    kind: str
    identity: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    uses: int | None = 1
    authorizations: Mapping[str, int | None] = field(default_factory=dict)
    node: str = "control"
    valid_from: float | None = None
    expires_at: float | None = None
    attributes: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.proof_id:
            raise AdjudicationInputError("proof.proof_id/id is required")
        if not self.kind:
            raise AdjudicationInputError("proof.kind is required")
        if self.uses is not None and (not isinstance(self.uses, int) or isinstance(self.uses, bool) or self.uses < 0):
            raise AdjudicationInputError("proof.uses must be a non-negative integer or null")
        object.__setattr__(self, "capabilities", _string_set(self.capabilities))
        object.__setattr__(self, "attributes", _attribute_set(self.attributes))
        object.__setattr__(self, "authorizations", _authorization_map(self.authorizations, kind=self.kind, uses=self.uses))
        if self.valid_from is not None and self.expires_at is not None and self.valid_from > self.expires_at:
            raise AdjudicationInputError(f"proof {self.proof_id} has valid_from after expires_at")

    @property
    def one_time(self) -> bool:
        return self.uses == 1

    @property
    def capability_names(self) -> frozenset[str]:
        return self.capabilities | {self.kind}

    @property
    def attribute_map(self) -> dict[str, str]:
        return dict(self.attributes)

    @classmethod
    def presence(
        cls,
        proof_id: str,
        *,
        identity: str,
        capabilities: Iterable[str] = (),
        roles: Iterable[str] = (),
        node: str = "control",
        one_time: bool = True,
        uses: int | None | object = _MISSING,
        authorizations: int | Mapping[str, int | None] | None | object = _MISSING,
        valid_from: float | None = None,
        expires_at: float | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> "Proof":
        if uses is _MISSING:
            resolved_uses: int | None = 1 if one_time else None
        else:
            resolved_uses = uses  # type: ignore[assignment]
        caps = set(_string_set(capabilities)) | set(_string_set(roles)) | {PRESENCE}
        if authorizations is _MISSING:
            if isinstance(resolved_uses, int):
                auth: Any = {"presence_authorization": resolved_uses}
            else:
                auth = {"presence_authorization": None}
        else:
            auth = authorizations
        return cls(
            proof_id=str(proof_id),
            kind=PRESENCE,
            identity=str(identity),
            capabilities=frozenset(caps),
            uses=resolved_uses,
            authorizations=auth,  # type: ignore[arg-type]
            node=node,
            valid_from=valid_from,
            expires_at=expires_at,
            attributes=attributes or {},
        )

    @classmethod
    def device_proximity(
        cls,
        proof_id: str,
        *,
        device_id: str | None = None,
        near: str | None = None,
        identity: str | None = None,
        capabilities: Iterable[str] = (),
        node: str = "control",
        one_time: bool = True,
        uses: int | None | object = _MISSING,
        valid_from: float | None = None,
        expires_at: float | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> "Proof":
        if uses is _MISSING:
            resolved_uses: int | None = 1 if one_time else None
        else:
            resolved_uses = uses  # type: ignore[assignment]
        attrs = dict(attributes or {})
        caps = set(_string_set(capabilities)) | {DEVICE, "device_proximity"}
        if device_id is not None:
            attrs.setdefault("device_id", str(device_id))
        if near is not None:
            attrs.setdefault("near", str(near))
            caps.add(f"near:{near}")
        return cls(
            proof_id=str(proof_id),
            kind=DEVICE,
            identity=identity,
            capabilities=frozenset(caps),
            uses=resolved_uses,
            authorizations={},
            node=node,
            valid_from=valid_from,
            expires_at=expires_at,
            attributes=attrs,
        )


@dataclass(frozen=True)
class OperationRequest:
    request_id: str
    requirements: tuple[Requirement, ...]
    deadline: float | None = None
    identity: str | None = None
    entry_node: str = "control"
    duration: float = 0.0
    priority: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id:
            raise AdjudicationInputError("operation.request_id/id is required")
        reqs = tuple(requirement_from_dict(r) for r in self.requirements)
        if not reqs:
            raise AdjudicationInputError(f"operation {self.request_id} must have at least one requirement")
        object.__setattr__(self, "requirements", reqs)
        if self.deadline is not None:
            object.__setattr__(self, "deadline", float(self.deadline))
        object.__setattr__(self, "duration", float(self.duration))
        if self.duration < 0:
            raise AdjudicationInputError("operation.duration must be non-negative")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata or {})))


def requirement_from_dict(value: Requirement | str | Mapping[str, Any]) -> Requirement:
    if isinstance(value, Requirement):
        return value
    if isinstance(value, str):
        return Requirement(capability=value)
    if not isinstance(value, Mapping):
        raise AdjudicationInputError("requirement must be a string, mapping, or Requirement")
    capability = value.get("capability") or value.get("requires") or value.get("name")
    if capability is None:
        raise AdjudicationInputError("requirement requires capability/requires/name")
    return Requirement(
        capability=str(capability),
        name=value.get("name"),
        count=int(value.get("count", 1)),
        proof_kind=value.get("proof_kind") or value.get("kind"),
        identity=value.get("identity"),
        allowed_identities=value.get("allowed_identities", value.get("identities")),
        identity_matches_request=bool(value.get("identity_matches_request", value.get("bind_identity", False))),
        identity_must_differ_from_request=bool(value.get("identity_must_differ_from_request", False)),
        attributes=value.get("attributes", value.get("where", {})),
    )


def proof_from_dict(value: Proof | Mapping[str, Any], *, default_kind: str | None = None) -> Proof:
    if isinstance(value, Proof):
        return value
    if not isinstance(value, Mapping):
        raise AdjudicationInputError("proof must be a Proof or mapping")
    proof_id = value.get("proof_id") or value.get("id")
    if proof_id is None:
        raise AdjudicationInputError("proof id/proof_id is required")
    kind = value.get("kind") or value.get("proof_type") or default_kind
    if kind is None:
        raise AdjudicationInputError(f"proof {proof_id} needs kind")

    if "uses" in value and value["uses"] is not None:
        uses: int | None = int(value["uses"])
    elif "one_time" in value:
        uses = 1 if bool(value["one_time"]) else None
    else:
        uses = 1

    kwargs: dict[str, Any] = {
        "proof_id": str(proof_id),
        "kind": str(kind),
        "identity": value.get("identity"),
        "capabilities": value.get("capabilities", value.get("scopes", ())),
        "uses": uses,
        "node": value.get("node", "control"),
        "valid_from": value.get("valid_from"),
        "expires_at": value.get("expires_at", value.get("valid_until")),
        "attributes": value.get("attributes", {}),
    }
    if "authorizations" in value:
        kwargs["authorizations"] = value["authorizations"]
    elif kind == PRESENCE:
        kwargs["authorizations"] = None  # triggers kind default tied to uses
    if kind == DEVICE and value.get("near") is not None:
        attrs = dict(kwargs["attributes"])
        attrs.setdefault("near", str(value["near"]))
        if value.get("device_id") is not None:
            attrs.setdefault("device_id", str(value["device_id"]))
        kwargs["attributes"] = attrs
        caps = set(_string_set(kwargs["capabilities"])) | {"device_proximity"}
        if value.get("near") is not None:
            caps.add(f"near:{value['near']}")
        kwargs["capabilities"] = frozenset(caps)
    return Proof(**kwargs)


def operation_from_dict(value: OperationRequest | Mapping[str, Any]) -> OperationRequest:
    if isinstance(value, OperationRequest):
        return value
    if not isinstance(value, Mapping):
        raise AdjudicationInputError("operation must be an OperationRequest or mapping")
    request_id = value.get("request_id") or value.get("id")
    if request_id is None:
        raise AdjudicationInputError("operation id/request_id is required")
    requirements = value.get("requirements")
    if requirements is None:
        requirements = value.get("required", ())
    if isinstance(requirements, (str, Requirement, Mapping)):
        requirements = (requirements,)
    return OperationRequest(
        request_id=str(request_id),
        requirements=tuple(requirements),
        deadline=value.get("deadline", value.get("latest_confirmation_at", value.get("latest_confirmation_time"))),
        identity=value.get("identity", value.get("operator")),
        entry_node=value.get("entry_node", value.get("node", "control")),
        duration=float(value.get("duration", value.get("confirmation_duration", 0.0))),
        priority=int(value.get("priority", 0)),
        metadata=value.get("metadata", {}),
    )
