"""Cycle-safe mutual-recognition search and closest-denial reporting."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from .coercion import (
    AssuranceInputError,
    coerce_allow_set,
    coerce_authority,
    coerce_level,
)
from .models import (
    FailureCandidate,
    ProofChain,
    ProofStep,
    RecognitionResult,
    RecognitionRule,
    Scope,
    VerificationFact,
)
from .reasons import (
    AUTHORITY_NOT_TRUSTED,
    BUSINESS_NOT_ALLOWED,
    BUSINESS_SCOPE_EMPTY,
    LEVEL_TOO_LOW,
    MAX_DEPTH_EXCEEDED,
    METHOD_NOT_ALLOWED,
    NO_OUTGOING_RULE,
    NO_PROOF_CHAIN,
    RULE_EXPIRED,
    RULE_NOT_YET_EFFECTIVE,
    SOURCE_LEVEL_MISMATCH,
    SOURCE_LEVEL_TOO_LOW,
    TARGET_AUTHORITY_NOT_REACHED,
    UNKNOWN_AUTHORITY,
    message_for,
)
from .timeutils import coerce_datetime, coerce_timedelta, utc_now


@dataclass(frozen=True)
class SearchState:
    authority: str
    level: int
    scope: Scope


class RecognitionEngine:
    """Search directed recognition mappings for a usable proof chain."""

    def __init__(
        self,
        rules: list[RecognitionRule | Mapping[str, Any]] | Mapping[str, Any] | None = None,
        *,
        trusted_authorities: Any = None,
        target_authority: str | None = None,
        clock_skew: float | int | timedelta = 0,
        max_depth: int | None = None,
        max_states: int = 100_000,
        allow_direct_fact: bool = False,
    ) -> None:
        if isinstance(rules, Mapping) and "rules" in rules:
            rules = rules["rules"]
        raw_rules = list(rules or [])
        self.rules: list[RecognitionRule] = []
        for index, raw in enumerate(raw_rules):
            rule = RecognitionRule.coerce(raw, default_id=f"rule-{index + 1}")
            if not rule.rule_id:
                rule = rule.with_id(f"rule-{index + 1}")
            self.rules.append(rule)

        self._entries: list[tuple[int, RecognitionRule]] = list(enumerate(self.rules))
        self._by_source: dict[str, list[tuple[int, RecognitionRule]]] = defaultdict(list)
        self._wildcard: list[tuple[int, RecognitionRule]] = []
        for uid, entry in self._entries:
            if entry.source_authority == "*":
                self._wildcard.append((uid, entry))
            else:
                self._by_source[entry.source_authority].append((uid, entry))

        for source in self._by_source:
            self._by_source[source].sort(key=lambda item: (-item[1].priority, item[0]))
        self._wildcard.sort(key=lambda item: (-item[1].priority, item[0]))

        self.default_trusted = self._coerce_trusted(trusted_authorities)
        self.default_target = (
            coerce_authority(target_authority) if target_authority is not None else None
        )
        self.clock_skew = coerce_timedelta(clock_skew, "clock_skew") or timedelta(0)
        self.max_depth = max_depth
        self.max_states = max_states
        self.allow_direct_fact = allow_direct_fact

    @staticmethod
    def _coerce_trusted(value: Any) -> frozenset[str] | None:
        if value is None:
            return None
        result = coerce_allow_set(value, field_name="trusted_authorities")
        return result

    def add_rule(self, rule: RecognitionRule | Mapping[str, Any]) -> "RecognitionEngine":
        index = len(self.rules)
        coerced = RecognitionRule.coerce(rule, default_id=f"rule-{index + 1}")
        if not coerced.rule_id:
            coerced = coerced.with_id(f"rule-{index + 1}")
        self.rules.append(coerced)
        uid = len(self._entries)
        entry = (uid, coerced)
        self._entries.append(entry)
        if coerced.source_authority == "*":
            self._wildcard.append(entry)
            self._wildcard.sort(key=lambda item: (-item[1].priority, item[0]))
        else:
            self._by_source[coerced.source_authority].append(entry)
            self._by_source[coerced.source_authority].sort(
                key=lambda item: (-item[1].priority, item[0])
            )
        return self

    def rules_for(self, authority: str) -> list[tuple[int, RecognitionRule]]:
        return list(self._by_source.get(authority, ())) + list(self._wildcard)

    # ------------------------------------------------------------------
    # Public evaluation entry points
    # ------------------------------------------------------------------
    def recognize(self, *args: Any, **kwargs: Any) -> RecognitionResult:
        return self.evaluate(*args, **kwargs)

    def evaluate(self, *args: Any, **kwargs: Any) -> RecognitionResult:
        fact, query = self._parse_call(*args, **kwargs)
        return self.run(fact, **query)

    @staticmethod
    def _parse_call(*args: Any, **kwargs: Any) -> tuple[VerificationFact, dict[str, Any]]:
        kwargs = dict(kwargs)

        # Explicit fact=...
        fact_value = kwargs.pop("fact", None)

        # Positional forms:
        # evaluate(fact, required_level, business=None, at=None)
        # evaluate(authority, level, method, occurred_at, required_level?, ...)
        positional = list(args)
        inferred_required: list[int] = []
        inferred_business: list[str] = []
        inferred_at: list[datetime] = []

        if positional and fact_value is None:
            first = positional.pop(0)
            if isinstance(first, (VerificationFact, Mapping)):
                fact_value = first
            elif isinstance(first, str):
                fact_parts: dict[str, Any] = {"authority": first}
                # Next three conventional fact fields.  Query arguments are
                # normally keywords; extra positionals are inferred below.
                if positional:
                    fact_parts["level"] = positional.pop(0)
                if positional:
                    fact_parts["method"] = positional.pop(0)
                if positional:
                    fact_parts["occurred_at"] = positional.pop(0)
                fact_value = fact_parts
            else:
                raise AssuranceInputError(f"unsupported first argument: {first!r}")

        # Remaining positional query values are inferred by type.
        for value in positional:
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                inferred_required.append(value)
            elif isinstance(value, datetime):
                inferred_at.append(value)
            elif isinstance(value, str):
                # A four-character ISO timestamp is handled as str below via
                # keyword; positional strings are treated as business.
                inferred_business.append(value)

        if fact_value is None:
            # Keyword fact fields.  VerificationFact.coerce understands the
            # aliases; collect all plausible fields into one mapping.
            fact_keys = (
                "authority",
                "source_authority",
                "issuer",
                "organization",
                "organisation",
                "org",
                "institution",
                "provider",
                "certifier",
                "level",
                "assurance_level",
                "loa",
                "aal",
                "ial",
                "grade",
                "method",
                "methods",
                "verification_method",
                "authentication_method",
                "auth_method",
                "occurred_at",
                "timestamp",
                "time",
                "authenticated_at",
                "auth_time",
                "issued_at",
            )
            present = {key: kwargs.pop(key) for key in fact_keys if key in kwargs}
            if present:
                fact_value = present
            else:
                raise AssuranceInputError("a VerificationFact or fact fields are required")

        fact = VerificationFact.coerce(fact_value)

        def first_kw(*names: str) -> Any:
            result: Any = None
            for name in names:
                if name in kwargs:
                    value = kwargs.pop(name)
                    if result is None and value is not None:
                        result = value
            return result

        required_level = first_kw(
            "min_level",
            "minimum_level",
            "required_level",
            "minimum_assurance_level",
            "required_assurance_level",
        )
        if required_level is None and inferred_required:
            required_level = inferred_required[0]
        if required_level is None:
            raise AssuranceInputError("required/minimum assurance level is required")
        required_level = coerce_level(required_level, field_name="required_level")

        business = first_kw(
            "business",
            "target_business",
            "service",
            "business_id",
            "target_service",
        )
        if business is None and inferred_business:
            business = inferred_business[0]
        if business is not None:
            business = str(business).strip()

        at_value = first_kw(
            "at",
            "now",
            "evaluated_at",
            "evaluation_time",
        )
        if at_value is None and inferred_at:
            at_value = inferred_at[0]
        at = coerce_datetime(at_value, "evaluation time") if at_value is not None else utc_now()

        target_authority = first_kw(
            "target_authority",
            "to_authority",
            "destination_authority",
            "target_org",
        )
        if target_authority is not None:
            target_authority = coerce_authority(target_authority)

        trusted = first_kw("trusted_authorities", "trusted_issuers", "trust_anchors")
        clock_skew = kwargs.pop("clock_skew", None)
        max_depth = kwargs.pop("max_depth", None)
        max_states = kwargs.pop("max_states", None)
        allow_direct_fact = first_kw("allow_direct_fact", "direct", "allow_zero_hop")

        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            raise AssuranceInputError(f"unknown evaluation keyword argument(s): {unknown}")

        query: dict[str, Any] = {
            "required_level": required_level,
            "business": business,
            "at": at,
        }
        if target_authority is not None:
            query["target_authority"] = target_authority
        if trusted is not None:
            query["trusted_authorities"] = trusted
        if clock_skew is not None:
            query["clock_skew"] = clock_skew
        if max_depth is not None:
            query["max_depth"] = max_depth
        if max_states is not None:
            query["max_states"] = max_states
        if allow_direct_fact is not None:
            query["allow_direct_fact"] = bool(allow_direct_fact)
        return fact, query

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def run(
        self,
        fact: VerificationFact | Mapping[str, Any],
        required_level: int,
        business: str | None = None,
        at: datetime | str | int | None = None,
        *,
        target_authority: str | None = None,
        trusted_authorities: Any = None,
        clock_skew: Any = None,
        max_depth: int | None = None,
        max_states: int | None = None,
        allow_direct_fact: bool | None = None,
    ) -> RecognitionResult:
        fact_obj = VerificationFact.coerce(fact)
        required = coerce_level(required_level, field_name="required_level")
        evaluated_at = coerce_datetime(at, "evaluation time") if at is not None else utc_now()
        target = target_authority or self.default_target
        if target is not None:
            target = coerce_authority(target)
        trusted = (
            self._coerce_trusted(trusted_authorities)
            if trusted_authorities is not None
            else self.default_trusted
        )
        skew = (
            coerce_timedelta(clock_skew, "clock_skew")
            if clock_skew is not None
            else self.clock_skew
        ) or timedelta(0)
        depth_limit = self.max_depth if max_depth is None else max_depth
        state_limit = self.max_states if max_states is None else max_states
        direct_allowed = (
            self.allow_direct_fact if allow_direct_fact is None else bool(allow_direct_fact)
        )

        initial_scope = Scope(methods=fact_obj.methods)
        initial_state = SearchState(fact_obj.authority, fact_obj.level, initial_scope)
        initial_chain = ProofChain(fact=fact_obj, steps=(), scope=initial_scope)

        queue: deque[tuple[SearchState, ProofChain]] = deque([(initial_state, initial_chain)])
        seen: set[SearchState] = {initial_state}
        successes: list[ProofChain] = []
        failures: list[FailureCandidate] = []
        cycles: list[dict[str, Any]] = []
        states_seen = 0
        limit_reached = False

        while queue:
            state, chain = queue.popleft()
            states_seen += 1
            if states_seen > state_limit:
                limit_reached = True
                failures.append(
                    self._graph_failure(
                        MAX_DEPTH_EXCEEDED,
                        state=state,
                        chain=chain,
                        required=required,
                        detail={"max_states": state_limit},
                    )
                )
                break

            # Assurance levels cannot increase.  A state below the business
            # minimum therefore cannot improve through another mapping.
            if state.level < required:
                failures.append(
                    self._level_too_low(chain=chain, state=state, required=required)
                )
                continue

            acceptable = self._is_acceptable(
                state=state,
                chain=chain,
                required=required,
                business=business,
                target=target,
                trusted=trusted,
                direct_allowed=direct_allowed,
            )

            if acceptable:
                successes.append(chain)

            entries = self.rules_for(state.authority)
            active_entries = [(uid, rule) for uid, rule in entries if rule.enabled]

            if depth_limit is not None and chain.length >= depth_limit:
                failures.append(
                    self._graph_failure(
                        MAX_DEPTH_EXCEEDED,
                        state=state,
                        chain=chain,
                        required=required,
                        detail={"max_depth": depth_limit},
                    )
                )
                continue

            if not active_entries:
                # A successful endpoint does not need an outgoing edge.
                if not acceptable:
                    if target is not None and state.authority != target:
                        code = TARGET_AUTHORITY_NOT_REACHED
                    elif trusted is not None and state.authority not in trusted:
                        code = AUTHORITY_NOT_TRUSTED
                    elif chain.length == 0 and not self.rules and not self._wildcard:
                        code = UNKNOWN_AUTHORITY
                    elif chain.length == 0 and not self._by_source.get(state.authority):
                        code = UNKNOWN_AUTHORITY
                    else:
                        code = NO_OUTGOING_RULE
                    failures.append(
                        self._graph_failure(
                            code,
                            state=state,
                            chain=chain,
                            required=required,
                            target=target,
                            trusted=trusted,
                        )
                    )
                continue

            for uid, rule in active_entries:
                violation_dicts, passed_checks, next_state, step, projected_level = (
                    self._apply_rule(
                        uid=uid,
                        rule=rule,
                        state=state,
                        chain=chain,
                        business=business,
                        at=evaluated_at,
                        skew=skew,
                    )
                )

                if violation_dicts:
                    primary = str(violation_dicts[0]["code"])
                    failures.append(
                        self._rule_failure(
                            primary,
                            rule=rule,
                            state=state,
                            chain=chain,
                            required=required,
                            projected_level=projected_level,
                            violations=tuple(violation_dicts),
                            passed_checks=passed_checks,
                        )
                    )
                    continue

                assert next_state is not None and step is not None
                next_chain = ProofChain(
                    fact=chain.fact,
                    steps=(*chain.steps, step),
                    scope=next_state.scope,
                )

                if next_chain.has_cycle:
                    cycles.append(
                        {
                            "rule_id": rule.rule_id,
                            "repeated_authority": next_state.authority,
                            "authorities": list(next_chain.authorities),
                            "final_level": next_state.level,
                            "scope": next_state.scope.to_dict(),
                            "chain": next_chain.to_dict(),
                            "exact_state_already_seen": next_state in seen,
                        }
                    )

                if next_state in seen:
                    # A cycle can return to the initial SearchState.  It must
                    # not be expanded again, but the cyclic chain itself can be
                    # a valid recognition proof (for example when the requested
                    # target authority is the starting authority).
                    if self._is_acceptable(
                        state=next_state,
                        chain=next_chain,
                        required=required,
                        business=business,
                        target=target,
                        trusted=trusted,
                        direct_allowed=direct_allowed,
                    ):
                        successes.append(next_chain)
                    continue
                seen.add(next_state)
                queue.append((next_state, next_chain))

        # Even when the state cap trips, choose any success already found.
        if successes:
            best = max(successes, key=self._success_rank)
            sorted_failures = tuple(sorted(failures, key=lambda c: c.closeness, reverse=True))
            return RecognitionResult(
                valid=True,
                required_level=required,
                business=business,
                evaluated_at=evaluated_at,
                chain=best,
                candidates=sorted_failures,
                target_authority=target,
                trusted_authorities=trusted,
                cycles=tuple(cycles),
            )

        if not failures:
            failures.append(
                FailureCandidate(
                    code=NO_PROOF_CHAIN,
                    message=message_for(NO_PROOF_CHAIN),
                    required_level=required,
                    closeness=(-10**18, 0, 0, 0, 0),
                )
            )

        # De-duplicate equivalent reports while retaining the first/path order.
        unique: dict[tuple[Any, ...], FailureCandidate] = {}
        for candidate in failures:
            key = (
                candidate.code,
                candidate.authority,
                candidate.rule_id,
                candidate.current_level,
                candidate.projected_level,
                candidate.path_authorities,
                tuple(v.get("code") for v in candidate.violations),
            )
            if key not in unique:
                unique[key] = candidate
        sorted_failures = tuple(sorted(unique.values(), key=lambda c: c.closeness, reverse=True))
        closest = sorted_failures[0]
        if limit_reached and not any(c.code == MAX_DEPTH_EXCEEDED for c in sorted_failures):
            # Defensive: state limit report is already inserted above.
            pass

        return RecognitionResult(
            valid=False,
            required_level=required,
            business=business,
            evaluated_at=evaluated_at,
            closest=closest,
            candidates=sorted_failures,
            target_authority=target,
            trusted_authorities=trusted,
            cycles=tuple(cycles),
        )

    @staticmethod
    def _is_acceptable(
        *,
        state: SearchState,
        chain: ProofChain,
        required: int,
        business: str | None,
        target: str | None,
        trusted: frozenset[str] | None,
        direct_allowed: bool,
    ) -> bool:
        if state.level < required:
            return False
        if target is not None and state.authority != target:
            return False
        if trusted is not None and state.authority not in trusted:
            return False
        if not state.scope.allows_business(business):
            return False
        if chain.length == 0 and not direct_allowed:
            if trusted is None or state.authority not in trusted:
                # An external verified fact is not itself a mutual recognition
                # proof until a published mapping has been applied.
                return False
        return True

    # ------------------------------------------------------------------
    # Rule application
    # ------------------------------------------------------------------
    def _apply_rule(
        self,
        *,
        uid: int,
        rule: RecognitionRule,
        state: SearchState,
        chain: ProofChain,
        business: str | None,
        at: datetime,
        skew: timedelta,
    ) -> tuple[list[dict[str, Any]], int, SearchState | None, ProofStep | None, int]:
        violations: list[dict[str, Any]] = []
        passed = 0
        checks: list[str] = []

        projected_level = state.level
        if rule.target_level is not None:
            projected_level = min(state.level, rule.target_level)

        # Rule publication/effective interval.
        if rule.effective_from is not None and at < rule.effective_from:
            violations.append(
                {
                    "code": RULE_NOT_YET_EFFECTIVE,
                    "message": f"规则 {rule.rule_id} 自 {rule.effective_from.isoformat()} 起生效",
                    "effective_from": rule.effective_from.isoformat(),
                    "evaluated_at": at.isoformat(),
                }
            )
        elif rule.effective_until is not None and at > rule.effective_until:
            violations.append(
                {
                    "code": RULE_EXPIRED,
                    "message": f"规则 {rule.rule_id} 已于 {rule.effective_until.isoformat()} 失效",
                    "effective_until": rule.effective_until.isoformat(),
                    "evaluated_at": at.isoformat(),
                }
            )
        else:
            if rule.effective_from is not None or rule.effective_until is not None:
                passed += 1
                checks.append("effective_time")

        # Source level.
        if rule.source_level is not None:
            if rule.level_matches(state.level):
                passed += 1
                checks.append("source_level")
            else:
                code = (
                    SOURCE_LEVEL_TOO_LOW
                    if state.level < rule.source_level
                    else SOURCE_LEVEL_MISMATCH
                )
                violations.append(
                    {
                        "code": code,
                        "message": (
                            f"规则 {rule.rule_id} 要求来源等级 "
                            f"{rule.source_level}（{rule.level_match}），当前为 {state.level}"
                        ),
                        "required_source_level": rule.source_level,
                        "current_level": state.level,
                        "match": rule.level_match,
                    }
                )

        # Verification method.  Scope.methods contains the original fact's
        # methods intersected with every earlier rule, so a cycle cannot swap
        # SMS for hardware authentication.
        if rule.allowed_methods is not None:
            accepted = state.scope.methods & rule.allowed_methods
            if accepted:
                passed += 1
                checks.append("method")
            else:
                violations.append(
                    {
                        "code": METHOD_NOT_ALLOWED,
                        "message": (
                            f"规则 {rule.rule_id} 不接受验证方式 "
                            f"{sorted(state.scope.methods)}；允许 "
                            f"{sorted(rule.allowed_methods)}"
                        ),
                        "fact_methods": sorted(state.scope.methods),
                        "allowed_methods": sorted(rule.allowed_methods),
                    }
                )

        # Target business.
        new_businesses = state.scope.businesses
        if rule.businesses is not None:
            new_businesses = rule.businesses if new_businesses is None else (
                new_businesses & rule.businesses
            )
            if business is not None:
                if business in rule.businesses and state.scope.allows_business(business):
                    passed += 1
                    checks.append("business")
                else:
                    violations.append(
                        {
                            "code": BUSINESS_NOT_ALLOWED,
                            "message": (
                                f"规则 {rule.rule_id} 不适用于业务 {business!r}；"
                                f"允许 {sorted(rule.businesses)}"
                            ),
                            "business": business,
                            "allowed_businesses": sorted(rule.businesses),
                        }
                    )
            elif len(rule.businesses) == 0:
                violations.append(
                    {
                        "code": BUSINESS_SCOPE_EMPTY,
                        "message": f"规则 {rule.rule_id} 的目标业务允许列表为空",
                        "allowed_businesses": [],
                    }
                )
            elif new_businesses is not None and len(new_businesses) == 0:
                violations.append(
                    {
                        "code": BUSINESS_SCOPE_EMPTY,
                        "message": f"规则 {rule.rule_id} 与既有业务范围交集为空",
                        "allowed_businesses": sorted(rule.businesses),
                    }
                )
            else:
                passed += 1
                checks.append("business_scope")

        # Occurrence time/freshness.
        new_time_scope = state.scope.time.intersect(rule.time_scope)
        time_code = new_time_scope.violation(chain.fact.occurred_at, at, clock_skew=skew)
        if time_code is not None:
            violations.append(
                {
                    "code": time_code,
                    "message": message_for(time_code),
                    "occurred_at": chain.fact.occurred_at.isoformat(),
                    "evaluated_at": at.isoformat(),
                    "rule_time_scope": rule.time_scope.to_dict(),
                }
            )
        elif (
            rule.not_before is not None
            or rule.not_after is not None
            or rule.min_age is not None
            or rule.max_age is not None
        ):
            passed += 1
            checks.append("occurrence_time")

        if violations:
            return violations, passed, None, None, projected_level

        new_level = state.level
        note: str | None = None
        if rule.target_level is not None:
            requested = rule.target_level
            new_level = min(state.level, requested)
            if requested > state.level:
                note = "target_level_capped_to_prevent_upgrade"

        new_scope = state.scope.intersect(
            methods=rule.allowed_methods,
            businesses=rule.businesses,
            time=rule.time_scope,
        )
        if not new_scope.methods:
            # Defensive; the method check above normally catches this.
            violations.append(
                {
                    "code": METHOD_NOT_ALLOWED,
                    "message": f"规则 {rule.rule_id} 后验证方式范围为空",
                }
            )
            return violations, passed, None, None, projected_level

        step = ProofStep(
            rule_id=rule.rule_id,
            source_authority=state.authority,
            target_authority=rule.target_authority,
            input_level=state.level,
            output_level=new_level,
            downgraded=new_level < state.level,
            checks=tuple(checks),
            note=note,
        )
        next_state = SearchState(rule.target_authority, new_level, new_scope)
        return [], passed, next_state, step, new_level

    # ------------------------------------------------------------------
    # Failure construction/ranking
    # ------------------------------------------------------------------
    def _rule_failure(
        self,
        code: str,
        *,
        rule: RecognitionRule,
        state: SearchState,
        chain: ProofChain,
        required: int,
        projected_level: int,
        violations: tuple[dict[str, Any], ...],
        passed_checks: int,
    ) -> FailureCandidate:
        primary_message = next(
            (str(v.get("message")) for v in violations if v.get("code") == code),
            message_for(code),
        )
        alignment = min(projected_level, required)
        closeness = (
            alignment,
            0,  # a blocked rule is not a formally complete proof
            passed_checks,
            -len(violations),
            chain.length,
            1,
        )
        return FailureCandidate(
            code=code,
            message=primary_message,
            authority=state.authority,
            rule_id=rule.rule_id,
            current_level=state.level,
            projected_level=projected_level,
            required_level=required,
            path_authorities=chain.authorities,
            path_rule_ids=chain.rule_ids,
            violations=violations,
            detail={"rule": rule.to_dict(), "passed_checks": passed_checks},
            closeness=closeness,
            proof_chain=chain,
        )

    def _level_too_low(
        self, *, chain: ProofChain, state: SearchState, required: int
    ) -> FailureCandidate:
        gap = required - state.level
        message = (
            f"证明链有效，终点为 {state.authority}，最终等级 {state.level}，"
            f"低于业务最低等级 {required}（差 {gap} 级）"
        )
        closeness = (
            state.level,
            1,  # complete valid chain
            chain.length + 10,
            0,
            chain.length,
            0,
        )
        return FailureCandidate(
            code=LEVEL_TOO_LOW,
            message=message,
            authority=state.authority,
            current_level=state.level,
            projected_level=state.level,
            required_level=required,
            path_authorities=chain.authorities,
            path_rule_ids=chain.rule_ids,
            detail={"level_gap": gap, "all_rules_valid": True},
            closeness=closeness,
            proof_chain=chain,
        )

    def _graph_failure(
        self,
        code: str,
        *,
        state: SearchState,
        chain: ProofChain,
        required: int,
        target: str | None = None,
        trusted: frozenset[str] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> FailureCandidate:
        extra = dict(detail or {})
        if target is not None:
            extra["target_authority"] = target
        if trusted is not None:
            extra["trusted_authorities"] = sorted(trusted)

        # A graph/trust endpoint is a complete valid rule path, but has no
        # concrete mapped rule to project beyond its current level.
        closeness = (
            min(state.level, required),
            1 if chain.length else 0,
            chain.length,
            0,
            chain.length,
            0,
        )
        return FailureCandidate(
            code=code,
            message=message_for(code),
            authority=state.authority,
            current_level=state.level,
            projected_level=state.level,
            required_level=required,
            path_authorities=chain.authorities,
            path_rule_ids=chain.rule_ids,
            detail=extra,
            closeness=closeness,
            proof_chain=chain,
        )

    @staticmethod
    def _success_rank(chain: ProofChain) -> tuple[Any, ...]:
        scope = chain.scope
        method_count = len(scope.methods) if scope else 0
        if scope is None or scope.businesses is None:
            business_breadth = 10**9
        else:
            business_breadth = len(scope.businesses)
        time_constraints = 0
        max_age_rank = 10**18
        if scope is not None:
            ts = scope.time
            time_constraints = sum(
                value is not None
                for value in (ts.not_before, ts.not_after, ts.min_age, ts.max_age)
            )
            if ts.max_age is not None:
                max_age_rank = int(ts.max_age.total_seconds())
        return (
            chain.final_level,
            method_count,
            business_breadth,
            -time_constraints,
            -max_age_rank,
            -chain.length,
            tuple(chain.rule_ids),
        )


# Common method-name aliases for callers/tests using domain-language APIs.
RecognitionEngine.match = RecognitionEngine.evaluate  # type: ignore[attr-defined]
RecognitionEngine.verify = RecognitionEngine.evaluate  # type: ignore[attr-defined]
RecognitionEngine.check = RecognitionEngine.evaluate  # type: ignore[attr-defined]
RecognitionEngine.resolve = RecognitionEngine.evaluate  # type: ignore[attr-defined]
RecognitionEngine.decide = RecognitionEngine.evaluate  # type: ignore[attr-defined]
RecognitionEngine.find_chain = RecognitionEngine.evaluate  # type: ignore[attr-defined]


# Convenience functional API.
def recognize(
    fact: Any = None,
    rules: Any = None,
    required_level: int | None = None,
    business: str | None = None,
    at: Any = None,
    *args: Any,
    min_level: int | None = None,
    **kwargs: Any,
) -> RecognitionResult:
    """Build an engine from ``rules`` and evaluate one fact.

    Positional forms include ``recognize(fact, rules, required_level)`` and
    common keyword aliases such as ``min_level``/``target_business``.
    A reversed ``recognize(rules, fact, ...)`` call is also accepted when the
    first argument is a rule collection/engine.
    """

    if isinstance(fact, (RecognitionEngine, list, tuple, set)) or (
        isinstance(fact, Mapping) and "rules" in fact and rules is not None
    ):
        fact, rules = rules, fact

    # Support recognize(required_level=..., fact=..., rules=...) and
    # recognize(fact_dict, [rules], 2, "biz", datetime).
    if required_level is None and min_level is not None:
        required_level = min_level
    for alias in ("minimum_level", "required_assurance_level"):
        if required_level is None and kwargs.get(alias) is not None:
            required_level = kwargs.pop(alias)

    if business is None and kwargs.get("target_business") is not None:
        business = kwargs.pop("target_business")
    if at is None and kwargs.get("now") is not None:
        at = kwargs.pop("now")
    if at is None and kwargs.get("evaluated_at") is not None:
        at = kwargs.pop("evaluated_at")

    engine_keys = {
        "trusted_authorities",
        "target_authority",
        "clock_skew",
        "max_depth",
        "max_states",
        "allow_direct_fact",
    }
    engine_kwargs = {k: kwargs.pop(k) for k in engine_keys if k in kwargs}
    if isinstance(rules, RecognitionEngine) and not engine_kwargs:
        engine = rules
    else:
        engine = RecognitionEngine(rules, **engine_kwargs)

    eval_kwargs: dict[str, Any] = {
        "required_level": required_level,
        "business": business,
        "at": at,
        **kwargs,
        **engine_kwargs,
    }
    return engine.evaluate(fact, *args, **eval_kwargs)


find_proof_chain = recognize
evaluate_assurance = recognize
recognize_assurance = recognize
