"""Typed facts, rules, proof chains, and result reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from .coercion import (
    AssuranceInputError,
    coerce_allow_set,
    coerce_authority,
    coerce_level,
    coerce_method_set,
    coerce_optional_datetime,
    coerce_optional_duration,
    optional_level,
    pick,
    require_pick,
)
from .reasons import message_for
from .timeutils import UTC, coerce_datetime


@dataclass(frozen=True)
class TimeScope:
    """Accumulated occurrence-time restrictions on a proof."""

    not_before: datetime | None = None
    not_after: datetime | None = None
    min_age: timedelta | None = None
    max_age: timedelta | None = None

    def intersect(self, other: "TimeScope") -> "TimeScope":
        not_before: datetime | None
        if self.not_before is None:
            not_before = other.not_before
        elif other.not_before is None:
            not_before = self.not_before
        else:
            not_before = max(self.not_before, other.not_before)

        not_after: datetime | None
        if self.not_after is None:
            not_after = other.not_after
        elif other.not_after is None:
            not_after = self.not_after
        else:
            not_after = min(self.not_after, other.not_after)

        min_age: timedelta | None
        if self.min_age is None:
            min_age = other.min_age
        elif other.min_age is None:
            min_age = self.min_age
        else:
            min_age = max(self.min_age, other.min_age)

        max_age: timedelta | None
        if self.max_age is None:
            max_age = other.max_age
        elif other.max_age is None:
            max_age = self.max_age
        else:
            max_age = min(self.max_age, other.max_age)

        return TimeScope(not_before, not_after, min_age, max_age)

    def violation(
        self,
        occurred_at: datetime,
        at: datetime,
        *,
        clock_skew: timedelta = timedelta(0),
    ) -> str | None:
        from .reasons import (
            AUTH_TIME_AFTER_WINDOW,
            AUTH_TIME_BEFORE_WINDOW,
            AUTH_TIME_IN_FUTURE,
            AUTH_TIME_TOO_OLD,
            AUTH_TIME_TOO_RECENT,
        )

        if self.not_before is not None and occurred_at < self.not_before:
            return AUTH_TIME_BEFORE_WINDOW
        if self.not_after is not None and occurred_at > self.not_after:
            return AUTH_TIME_AFTER_WINDOW

        age = at - occurred_at
        if age < -clock_skew:
            return AUTH_TIME_IN_FUTURE
        if self.max_age is not None and age > self.max_age:
            return AUTH_TIME_TOO_OLD
        if self.min_age is not None and age < self.min_age:
            return AUTH_TIME_TOO_RECENT
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "not_before": self.not_before.isoformat() if self.not_before else None,
            "not_after": self.not_after.isoformat() if self.not_after else None,
            "min_age_seconds": self.min_age.total_seconds() if self.min_age else None,
            "max_age_seconds": self.max_age.total_seconds() if self.max_age else None,
        }


@dataclass(frozen=True)
class Scope:
    """The monotonic scope a derived proof can still assert."""

    methods: frozenset[str]
    businesses: frozenset[str] | None = None
    time: TimeScope = field(default_factory=TimeScope)

    def intersect(
        self,
        *,
        methods: frozenset[str] | None = None,
        businesses: frozenset[str] | None = None,
        time: TimeScope | None = None,
    ) -> "Scope":
        new_methods = self.methods
        if methods is not None:
            new_methods = self.methods & methods

        if self.businesses is None:
            new_businesses = businesses
        elif businesses is None:
            new_businesses = self.businesses
        else:
            new_businesses = self.businesses & businesses

        new_time = self.time.intersect(time) if time is not None else self.time
        return Scope(new_methods, new_businesses, new_time)

    def allows_business(self, business: str | None) -> bool:
        if self.businesses is None:
            return True
        if business is None:
            return len(self.businesses) > 0
        return business in self.businesses

    def to_dict(self) -> dict[str, Any]:
        return {
            "methods": sorted(self.methods),
            "businesses": None if self.businesses is None else sorted(self.businesses),
            "time": self.time.to_dict(),
        }


@dataclass(frozen=True)
class VerificationFact:
    """An already-verified authentication fact.

    ``level`` uses larger integers for stronger assurance.  A string such as
    ``"L3"`` or ``"三级"`` is accepted by the coercion helper.
    """

    authority: str
    level: int
    method: str
    occurred_at: datetime
    methods: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        authority = coerce_authority(self.authority)
        level = coerce_level(self.level, field_name="fact level")
        try:
            occurred_at = coerce_datetime(self.occurred_at, "occurred_at")
        except ValueError as exc:
            raise AssuranceInputError(str(exc)) from exc
        method_set = coerce_method_set(self.methods or self.method, required=True)
        assert method_set is not None
        primary = str(self.method).strip() if self.method is not None else next(iter(method_set))
        if not primary:
            primary = sorted(method_set)[0]
        if primary not in method_set:
            method_set = frozenset({primary, *method_set})

        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "method", primary)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "methods", method_set)

    @classmethod
    def coerce(cls, value: Any) -> "VerificationFact":
        if isinstance(value, VerificationFact):
            return value
        if not isinstance(value, Mapping):
            raise AssuranceInputError("fact must be a VerificationFact or mapping")

        authority = require_pick(
            value,
            "authority",
            "source_authority",
            "issuer",
            "organization",
            "organisation",
            "org",
            "institution",
            "provider",
            "certifier",
            what="VerificationFact",
        )
        level = require_pick(
            value, "level", "assurance_level", "loa", "aal", "ial", "grade", what="VerificationFact"
        )
        method = require_pick(
            value,
            "method",
            "methods",
            "verification_method",
            "authentication_method",
            "auth_method",
            what="VerificationFact",
        )
        occurred_at = require_pick(
            value,
            "occurred_at",
            "timestamp",
            "time",
            "authenticated_at",
            "auth_time",
            "issued_at",
            what="VerificationFact",
        )
        methods = pick(value, "methods", "verification_methods", "authentication_methods")
        return cls(
            authority=authority,
            level=level,
            method=method if isinstance(method, str) else next(iter(coerce_method_set(method, True) or [])),
            occurred_at=occurred_at,
            methods=coerce_method_set(methods if methods is not None else method, required=True) or frozenset(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "level": self.level,
            "method": self.method,
            "methods": sorted(self.methods),
            "occurred_at": self.occurred_at.isoformat(),
        }


# Friendly alias.
Fact = VerificationFact


@dataclass(frozen=True)
class RecognitionRule:
    """A published directed mutual-recognition mapping.

    Restrictions are all optional.  When ``source_level`` is supplied, the
    default ``level_match="minimum"`` means a fact at level >= source_level can
    use the rule.  Use ``level_match="exact"`` for an exact published mapping.
    A target level can never raise the derived level above the current level.
    """

    source_authority: str
    target_authority: str
    rule_id: str = ""
    source_level: int | None = None
    target_level: int | None = None
    level_match: str = "minimum"
    allowed_methods: frozenset[str] | None = None
    businesses: frozenset[str] | None = None
    not_before: datetime | None = None
    not_after: datetime | None = None
    min_age: timedelta | None = None
    max_age: timedelta | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    priority: int = 0
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", str(self.rule_id or ""))
        object.__setattr__(self, "source_authority", coerce_authority(self.source_authority))
        object.__setattr__(self, "target_authority", coerce_authority(self.target_authority))
        object.__setattr__(
            self, "source_level", optional_level(self.source_level, field_name="source_level")
        )
        object.__setattr__(
            self, "target_level", optional_level(self.target_level, field_name="target_level")
        )
        if self.level_match not in {"minimum", "exact"}:
            raise AssuranceInputError(f"unsupported level_match: {self.level_match!r}")
        object.__setattr__(
            self,
            "allowed_methods",
            coerce_allow_set(self.allowed_methods, field_name="allowed_methods"),
        )
        object.__setattr__(
            self, "businesses", coerce_allow_set(self.businesses, field_name="businesses")
        )
        for field_name in ("not_before", "not_after", "effective_from", "effective_until"):
            value = getattr(self, field_name)
            object.__setattr__(
                self, field_name, coerce_optional_datetime(value, f"rule.{field_name}")
            )
        for field_name in ("min_age", "max_age"):
            value = getattr(self, field_name)
            object.__setattr__(
                self, field_name, coerce_optional_duration(value, f"rule.{field_name}")
            )
        try:
            object.__setattr__(self, "priority", int(self.priority))
        except (TypeError, ValueError) as exc:
            raise AssuranceInputError(f"invalid priority: {self.priority!r}") from exc
        object.__setattr__(self, "enabled", bool(self.enabled))

    @classmethod
    def coerce(cls, value: Any, *, default_id: str = "") -> "RecognitionRule":
        if isinstance(value, RecognitionRule):
            if value.rule_id:
                return value
            return value.with_id(default_id)
        if not isinstance(value, Mapping):
            raise AssuranceInputError("rule must be a RecognitionRule or mapping")

        source = require_pick(
            value,
            "source_authority",
            "from_authority",
            "issuer_authority",
            "issuer",
            "from",
            "source",
            "source_org",
            "from_org",
            what="RecognitionRule.source_authority",
        )
        target = require_pick(
            value,
            "target_authority",
            "to_authority",
            "destination_authority",
            "destination",
            "to",
            "target",
            "target_org",
            "to_org",
            what="RecognitionRule.target_authority",
        )

        source_level_value = pick(
            value,
            "source_level",
            "from_level",
            "input_level",
            "required_level",
            "minimum_source_level",
            "min_source_level",
            "level",
            nested=("conditions", "restrictions", "constraints"),
        )
        target_level_value = pick(
            value,
            "target_level",
            "to_level",
            "mapped_level",
            "output_level",
            "new_level",
            "downgrade_to",
            "result_level",
            nested=("mapping", "conditions", "restrictions", "constraints"),
        )

        # mapping={3: 2} or level_mapping={"3": 2}: infer when unambiguous.
        level_mapping = pick(value, "level_mapping", "mapping", "levels")
        if isinstance(level_mapping, Mapping) and source_level_value is not None:
            key = str(coerce_level(source_level_value, field_name="source level"))
            if key in level_mapping:
                target_level_value = level_mapping[key]
            elif int(key) in level_mapping:
                target_level_value = level_mapping[int(key)]

        level_match = pick(
            value,
            "level_match",
            "source_level_match",
            "match",
            default="minimum",
            nested=("conditions", "restrictions"),
        )
        if level_match not in {"minimum", "exact"}:
            raise AssuranceInputError(f"unsupported level_match: {level_match!r}")

        methods_value = pick(
            value,
            "allowed_methods",
            "methods",
            "method",
            "verification_methods",
            "authentication_methods",
            "allowed_verification_methods",
            nested=("conditions", "restrictions", "constraints"),
        )
        business_value = pick(
            value,
            "businesses",
            "business",
            "target_businesses",
            "target_business",
            "allowed_businesses",
            "allowed_business",
            "services",
            "service",
            "scopes",
            "scope",
            nested=("conditions", "restrictions", "constraints", "target"),
        )

        def time_pick(*names: str) -> Any:
            return pick(
                value,
                *names,
                nested=("time_restriction", "time_window", "time", "conditions", "restrictions", "constraints"),
            )

        max_age_value = time_pick(
            "max_age",
            "max_age_seconds",
            "ttl",
            "ttl_seconds",
            "valid_for",
            "validity",
            "validity_seconds",
            "freshness",
            "time_limit",
            "timeout",
        )
        if max_age_value is None:
            for key, seconds_per_unit in (
                ("max_age_minutes", 60.0),
                ("ttl_minutes", 60.0),
                ("max_age_hours", 3600.0),
                ("max_age_days", 86400.0),
            ):
                unit_value = time_pick(key)
                if unit_value is not None:
                    max_age_value = timedelta(seconds=float(unit_value) * seconds_per_unit)
                    break
        min_age_value = time_pick("min_age", "min_age_seconds", "minimum_age")
        not_before_value = time_pick(
            "not_before",
            "auth_not_before",
            "earliest_occurred_at",
            "earliest",
            "begin",
            "start",
            "start_time",
        )
        not_after_value = time_pick(
            "not_after",
            "auth_not_after",
            "latest_occurred_at",
            "latest",
            "end",
            "end_time",
        )
        effective_from_value = pick(value, "effective_from", "valid_from", "active_from", "published_at")
        effective_until_value = pick(value, "effective_until", "valid_until", "active_until", "expires_at")

        rule_id = pick(value, "rule_id", "id", "name", default=default_id) or default_id
        priority = pick(value, "priority", "weight", default=0)
        enabled = pick(value, "enabled", "active", default=True)

        try:
            priority_int = int(priority)
        except (TypeError, ValueError) as exc:
            raise AssuranceInputError(f"invalid rule priority: {priority!r}") from exc

        return cls(
            rule_id=str(rule_id),
            source_authority=coerce_authority(source),
            target_authority=coerce_authority(target),
            source_level=optional_level(source_level_value, field_name="rule.source_level"),
            target_level=optional_level(target_level_value, field_name="rule.target_level"),
            level_match=str(level_match),
            allowed_methods=coerce_allow_set(methods_value, field_name="rule.allowed_methods"),
            businesses=coerce_allow_set(business_value, field_name="rule.businesses"),
            not_before=coerce_optional_datetime(not_before_value, "rule.not_before"),
            not_after=coerce_optional_datetime(not_after_value, "rule.not_after"),
            min_age=coerce_optional_duration(min_age_value, "rule.min_age"),
            max_age=coerce_optional_duration(max_age_value, "rule.max_age"),
            effective_from=coerce_optional_datetime(effective_from_value, "rule.effective_from"),
            effective_until=coerce_optional_datetime(effective_until_value, "rule.effective_until"),
            priority=priority_int,
            enabled=bool(enabled),
        )

    def with_id(self, rule_id: str) -> "RecognitionRule":
        return RecognitionRule(
            **{**self.__dict__, "rule_id": rule_id},
        )

    @property
    def time_scope(self) -> TimeScope:
        return TimeScope(
            not_before=self.not_before,
            not_after=self.not_after,
            min_age=self.min_age,
            max_age=self.max_age,
        )

    def level_matches(self, current_level: int) -> bool:
        if self.source_level is None:
            return True
        if self.level_match == "exact":
            return current_level == self.source_level
        return current_level >= self.source_level

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "source_authority": self.source_authority,
            "target_authority": self.target_authority,
            "source_level": self.source_level,
            "target_level": self.target_level,
            "level_match": self.level_match,
            "allowed_methods": None if self.allowed_methods is None else sorted(self.allowed_methods),
            "businesses": None if self.businesses is None else sorted(self.businesses),
            "not_before": self.not_before.isoformat() if self.not_before else None,
            "not_after": self.not_after.isoformat() if self.not_after else None,
            "min_age_seconds": self.min_age.total_seconds() if self.min_age else None,
            "max_age_seconds": self.max_age.total_seconds() if self.max_age else None,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_until": self.effective_until.isoformat() if self.effective_until else None,
            "priority": self.priority,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class ProofStep:
    rule_id: str
    source_authority: str
    target_authority: str
    input_level: int
    output_level: int
    downgraded: bool = False
    checks: tuple[str, ...] = ()
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "from": self.source_authority,
            "to": self.target_authority,
            "input_level": self.input_level,
            "output_level": self.output_level,
            "downgraded": self.downgraded,
            "checks": list(self.checks),
            "note": self.note,
        }


@dataclass(frozen=True)
class ProofChain:
    fact: VerificationFact
    steps: tuple[ProofStep, ...] = ()
    scope: Scope | None = None

    @property
    def initial_authority(self) -> str:
        return self.fact.authority

    @property
    def final_authority(self) -> str:
        return self.steps[-1].target_authority if self.steps else self.fact.authority

    @property
    def initial_level(self) -> int:
        return self.fact.level

    @property
    def final_level(self) -> int:
        return self.steps[-1].output_level if self.steps else self.fact.level

    @property
    def authorities(self) -> tuple[str, ...]:
        return (self.fact.authority, *[step.target_authority for step in self.steps])

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(step.rule_id for step in self.steps)

    @property
    def length(self) -> int:
        return len(self.steps)

    @property
    def has_cycle(self) -> bool:
        seen: set[str] = set()
        for authority in self.authorities:
            if authority in seen:
                return True
            seen.add(authority)
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_authority": self.initial_authority,
            "final_authority": self.final_authority,
            "authorities": list(self.authorities),
            "rule_ids": list(self.rule_ids),
            "initial_level": self.initial_level,
            "final_level": self.final_level,
            "length": self.length,
            "has_cycle": self.has_cycle,
            "fact": self.fact.to_dict(),
            "scope": self.scope.to_dict() if self.scope else None,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class FailureCandidate:
    code: str
    message: str
    authority: str | None = None
    rule_id: str | None = None
    current_level: int | None = None
    projected_level: int | None = None
    required_level: int | None = None
    path_authorities: tuple[str, ...] = ()
    path_rule_ids: tuple[str, ...] = ()
    violations: tuple[dict[str, Any], ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)
    closeness: tuple[Any, ...] = field(default=(), compare=False)
    proof_chain: "ProofChain | None" = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "reason": self.code,
            "message": self.message or message_for(self.code),
            "authority": self.authority,
            "rule_id": self.rule_id,
            "current_level": self.current_level,
            "projected_level": self.projected_level,
            "required_level": self.required_level,
            "level_gap": (
                max(0, self.required_level - self.projected_level)
                if self.required_level is not None and self.projected_level is not None
                else None
            ),
            "path_authorities": list(self.path_authorities),
            "path_rule_ids": list(self.path_rule_ids),
            "violations": list(self.violations),
            "detail": self.detail,
            "proof_chain": self.proof_chain.to_dict() if self.proof_chain is not None else None,
        }


@dataclass(frozen=True)
class RecognitionResult:
    valid: bool
    required_level: int
    business: str | None
    evaluated_at: datetime
    chain: ProofChain | None = None
    closest: FailureCandidate | None = None
    candidates: tuple[FailureCandidate, ...] = ()
    target_authority: str | None = None
    trusted_authorities: frozenset[str] | None = None
    cycles: tuple[dict[str, Any], ...] = ()

    @property
    def reason(self) -> str | None:
        return None if self.valid else (self.closest.code if self.closest else "NO_PROOF_CHAIN")

    @property
    def message(self) -> str:
        if self.valid:
            return message_for("SUCCESS")
        if self.closest:
            return self.closest.message
        return message_for("NO_PROOF_CHAIN")

    @property
    def final_level(self) -> int | None:
        return self.chain.final_level if self.chain else None

    @property
    def effective_scope(self) -> Scope | None:
        return self.chain.scope if self.chain else None

    @property
    def closest_chain(self) -> ProofChain | None:
        return self.closest.proof_chain if self.closest is not None else None

    @property
    def best_chain(self) -> ProofChain | None:
        """Best qualifying chain on success, otherwise the closest valid prefix."""
        return self.chain if self.valid else self.closest_chain

    @property
    def best_final_level(self) -> int | None:
        chain = self.best_chain
        return chain.final_level if chain is not None else None

    def require(self) -> ProofChain:
        if self.chain is None:
            raise AssuranceInputError(f"{self.reason}: {self.message}")
        return self.chain

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "success": self.valid,
            "reason": self.reason,
            "code": self.reason,
            "message": self.message,
            "required_level": self.required_level,
            "minimum_level": self.required_level,
            "business": self.business,
            "evaluated_at": self.evaluated_at.isoformat(),
            "final_level": self.final_level,
            "best_final_level": self.best_final_level,
            "best_chain": self.best_chain.to_dict() if self.best_chain is not None else None,
            "target_authority": self.target_authority,
            "trusted_authorities": (
                sorted(self.trusted_authorities) if self.trusted_authorities is not None else None
            ),
            "chain": self.chain.to_dict() if self.chain else None,
            "effective_scope": self.effective_scope.to_dict() if self.effective_scope else None,
            "closest": self.closest.to_dict() if self.closest else None,
            "closest_chain": self.closest_chain.to_dict() if self.closest_chain else None,
            "cycles": list(self.cycles),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }
