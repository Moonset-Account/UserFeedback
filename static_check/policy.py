"""Disclosure policy and identity-based filtering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


WILDCARDS = {"*", "public", "any", "all", "anonymous"}


@dataclass(frozen=True)
class Grant:
    """A disclosure grant for one identifier or field."""

    identifier: str
    public: bool = False
    callers: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    sources: frozenset[str] | None = None
    reason: str | None = None

    def allows(
        self,
        caller: "CallerIdentity | str | None",
        *,
        source: str | None = None,
        role_resolver: Mapping[str, frozenset[str]] | None = None,
    ) -> tuple[bool, tuple[str, ...]]:
        identity = CallerIdentity.coerce(caller)
        reasons: list[str] = []

        source_ok = True
        if self.sources is not None:
            source_ok = source is not None and source in self.sources
            if not source_ok:
                reasons.append("source_not_licensed")

        identity_ok = False
        if self.public:
            identity_ok = True
        if identity is not None:
            if identity.identity in self.callers:
                identity_ok = True
            if self.roles and (set(identity.roles) & set(self.roles)):
                identity_ok = True
            if role_resolver is not None and identity.identity in role_resolver:
                if set(role_resolver[identity.identity]) & set(self.roles):
                    identity_ok = True

        if not identity_ok:
            reasons.append("identity_not_authorized")
        if identity_ok and source_ok:
            return True, ()
        return False, tuple(reasons)


@dataclass(frozen=True)
class CallerIdentity:
    identity: str
    roles: frozenset[str] = field(default_factory=frozenset)

    @staticmethod
    def coerce(value: Any) -> "CallerIdentity | None":
        if value is None:
            return None
        if isinstance(value, CallerIdentity):
            return value
        if isinstance(value, str):
            return CallerIdentity(value)
        if isinstance(value, Mapping):
            identity = (
                value.get("id")
                or value.get("identity")
                or value.get("caller")
                or value.get("name")
            )
            if identity is None:
                raise ValueError("caller mapping requires id/identity/caller")
            roles = value.get("roles", ())
            if isinstance(roles, str):
                roles = (roles,)
            return CallerIdentity(str(identity), frozenset(roles))
        # Dataclass/object style input.
        identity = getattr(value, "id", None) or getattr(value, "identity", None)
        if identity is None:
            identity = getattr(value, "name", None)
        if identity is None:
            raise TypeError(f"unsupported caller identity: {value!r}")
        roles = getattr(value, "roles", ())
        if isinstance(roles, str):
            roles = (roles,)
        return CallerIdentity(str(identity), frozenset(roles or ()))


def _as_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(str(item) for item in value)


def normalize_grants(declarations: Any) -> dict[str, Grant]:
    """Normalize the many compact declaration forms into :class:`Grant`s.

    Supported inputs:

    * ``{"D1", "D2"}`` -- every identifier is public.
    * ``["D1", {"id": "D2", "callers": ["alice"]}]``
    * ``{"D1": ["alice", "bob"], "D2": {"public": True}}``
    * ``{"D1": {"callers": ["alice"], "sources": ["src"]}}``
    """

    if declarations is None:
        return {}

    grants: dict[str, Grant] = {}

    if isinstance(declarations, Mapping):
        for identifier, declaration in declarations.items():
            grants[str(identifier)] = _grant_from_value(str(identifier), declaration)
        return grants

    if isinstance(declarations, (str, bytes)):
        return {str(declarations): Grant(str(declarations), public=True)}

    for item in declarations:
        if isinstance(item, str):
            grants[item] = Grant(item, public=True)
        elif isinstance(item, Mapping):
            identifier = item.get("id") or item.get("identifier") or item.get("name")
            if identifier is None:
                raise ValueError("declaration requires id/identifier/name")
            grants[str(identifier)] = _grant_from_value(str(identifier), item)
        elif isinstance(item, Grant):
            grants[item.identifier] = item
        else:
            identifier = getattr(item, "identifier", None) or getattr(item, "id", None)
            if identifier is None:
                raise TypeError(f"unsupported declaration: {item!r}")
            grants[str(identifier)] = Grant(
                identifier=str(identifier),
                public=bool(getattr(item, "public", False)),
                callers=frozenset(getattr(item, "callers", ()) or ()),
                roles=frozenset(getattr(item, "roles", ()) or ()),
                sources=_optional_set(getattr(item, "sources", None)),
            )
    return grants


def _optional_set(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    return _as_set(value)


def _grant_from_value(identifier: str, value: Any) -> Grant:
    if isinstance(value, Grant):
        return value
    if value is True:
        return Grant(identifier, public=True)
    if value is False or value is None:
        # Explicitly declared but not granted to anyone.  This is useful for
        # negative tests and fail-closed policy composition.
        return Grant(identifier, public=False)
    if isinstance(value, str):
        if value in WILDCARDS:
            return Grant(identifier, public=True)
        return Grant(identifier, callers=frozenset({value}))
    if isinstance(value, (list, tuple, set, frozenset)):
        callers = _as_set(value)
        public = bool(callers & WILDCARDS)
        return Grant(
            identifier,
            public=public,
            callers=frozenset(caller for caller in callers if caller not in WILDCARDS),
        )
    if isinstance(value, Mapping):
        callers = _as_set(
            value.get("callers")
            or value.get("identities")
            or value.get("allowed")
            or value.get("allowed_callers")
        )
        public = bool(value.get("public", False)) or bool(callers & WILDCARDS)
        roles = _as_set(value.get("roles") or value.get("allowed_roles"))
        sources = value.get("sources") or value.get("allowed_sources")
        return Grant(
            identifier=identifier,
            public=public,
            callers=frozenset(caller for caller in callers if caller not in WILDCARDS),
            roles=roles,
            sources=_optional_set(sources),
            reason=value.get("reason"),
        )
    raise TypeError(f"unsupported grant for {identifier!r}: {value!r}")


class DisclosurePolicy:
    """Policy combining diagnostic, error-code, field and source grants."""

    def __init__(
        self,
        *,
        caller: Any = None,
        public_diagnostics: Any = None,
        diagnostics: Any = None,
        allowed_diagnostics: Any = None,
        public_ids: Any = None,
        declarations: Any = None,
        public_codes: Any = None,
        codes: Any = None,
        allowed_codes: Any = None,
        allowed_fields: Any = None,
        fields: Any = None,
        allowed_sources: Any = None,
        roles: Mapping[str, Iterable[str]] | None = None,
        allow_undeclared_codes: bool = False,
        allow_undeclared_fields: bool | None = None,
    ) -> None:
        self.caller = CallerIdentity.coerce(caller)

        diagnostic_input = (
            public_diagnostics
            if public_diagnostics is not None
            else diagnostics
            if diagnostics is not None
            else allowed_diagnostics
            if allowed_diagnostics is not None
            else public_ids
            if public_ids is not None
            else declarations
        )
        self.diagnostic_grants = normalize_grants(diagnostic_input)

        code_input = (
            public_codes
            if public_codes is not None
            else codes
            if codes is not None
            else allowed_codes
        )
        if code_input is None:
            # A combined declaration map may contain code entries.  We do not
            # eagerly split it because diagnostic and code checks can share it.
            self.code_grants = dict(self.diagnostic_grants)
        else:
            self.code_grants = normalize_grants(code_input)

        field_input = allowed_fields if allowed_fields is not None else fields
        self.field_grants = self._normalize_fields(field_input)
        self.source_grants = normalize_grants(allowed_sources)
        self.role_resolver: Mapping[str, frozenset[str]] | None = (
            {key: _as_set(value) for key, value in roles.items()} if roles else None
        )
        self.allow_undeclared_codes = allow_undeclared_codes
        if allow_undeclared_fields is not None:
            self.allow_undeclared_fields = allow_undeclared_fields
        elif isinstance(field_input, (list, tuple, set, frozenset)):
            # A plain list is an allowlist.  Mapping grants remain additive
            # unless the caller explicitly opts into strict field checking.
            self.allow_undeclared_fields = False
        else:
            self.allow_undeclared_fields = True

    @staticmethod
    def _normalize_fields(value: Any) -> dict[str, Grant]:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            # Mapping may be field -> identities/grant, or caller -> fields.
            # Detect caller -> iterable fields by checking collection values
            # whose entries are strings and keys do not look like field grants?
            # Ambiguous.  Support explicit {"fields": ..., "caller_fields":...}.
            if "fields" in value or "caller_fields" in value:
                grants = normalize_grants(value.get("fields"))
                caller_fields = value.get("caller_fields", {})
                if isinstance(caller_fields, Mapping):
                    for caller, fields in caller_fields.items():
                        for field_name in _as_set(fields):
                            existing = grants.get(field_name) or Grant(field_name)
                            grants[field_name] = Grant(
                                field_name,
                                public=existing.public,
                                callers=existing.callers | frozenset({str(caller)}),
                                roles=existing.roles,
                                sources=existing.sources,
                            )
                return grants
            return normalize_grants(value)
        return normalize_grants(value)

    def diagnostic_allowed(
        self, diagnostic_id: str, *, source: str | None = None
    ) -> tuple[bool, tuple[str, ...]]:
        grant = self.diagnostic_grants.get(diagnostic_id)
        if grant is None:
            return False, ("not_declared",)
        return grant.allows(
            self.caller, source=source, role_resolver=self.role_resolver
        )

    def code_allowed(self, code: str | None) -> tuple[bool, tuple[str, ...]]:
        if code is None:
            return True, ()
        grant = self.code_grants.get(code)
        if grant is None:
            if self.allow_undeclared_codes:
                return True, ("undeclared_allowed_by_default",)
            return False, ("code_not_declared_public",)
        return grant.allows(self.caller, role_resolver=self.role_resolver)

    def field_allowed(self, field_name: str) -> tuple[bool, tuple[str, ...]]:
        grant = self.field_grants.get(field_name)
        if grant is None:
            if self.allow_undeclared_fields:
                return True, ("field_not_policy_restricted",)
            return False, ("field_not_declared",)
        return grant.allows(self.caller, role_resolver=self.role_resolver)

    def source_allowed(self, source: str) -> tuple[bool, tuple[str, ...]]:
        if not self.source_grants:
            return True, ()
        grant = self.source_grants.get(source)
        if grant is None:
            return False, ("source_not_declared",)
        return grant.allows(self.caller, role_resolver=self.role_resolver)

    def allows_diagnostic_for(
        self, caller: Any, diagnostic_id: str, *, source: str | None = None
    ) -> bool:
        """Convenience predicate used by reverse identity filtering."""

        policy = self.for_caller(caller)
        return policy.diagnostic_allowed(diagnostic_id, source=source)[0]

    def for_caller(self, caller: Any) -> "DisclosurePolicy":
        if CallerIdentity.coerce(caller) == self.caller:
            return self
        return DisclosurePolicy(
            caller=caller,
            public_diagnostics=self.diagnostic_grants,
            public_codes=self.code_grants,
            allowed_fields=self.field_grants,
            allowed_sources=self.source_grants,
            roles=self.role_resolver,  # type: ignore[arg-type]
            allow_undeclared_codes=self.allow_undeclared_codes,
            allow_undeclared_fields=self.allow_undeclared_fields,
        )

def identity_filter(
    items: Iterable[Any],
    caller: Any,
    policy: DisclosurePolicy,
    *,
    id_attr: str = "diagnostic_id",
) -> tuple[list[Any], list[Any]]:
    """Split diagnostic-like items into ``(allowed, blocked)`` for caller."""

    scoped = policy.for_caller(caller)
    allowed: list[Any] = []
    blocked: list[Any] = []
    for item in items:
        identifier = item if isinstance(item, str) else getattr(item, id_attr)
        source = None if isinstance(item, str) else getattr(item, "source", None)
        ok, _ = scoped.diagnostic_allowed(str(identifier), source=source)
        (allowed if ok else blocked).append(item)
    return allowed, blocked
