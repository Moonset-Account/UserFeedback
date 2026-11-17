"""The connection planner: URL + policy -> explicit, frozen connection plan."""

from __future__ import annotations

import ipaddress
import urllib.parse
import uuid
from dataclasses import dataclass, field

from .clock import Clock
from .domains import DomainAllowList, normalize_hostname
from .errors import (
    DomainRuleError,
    ForbiddenAddressError,
    InvalidUrlError,
    ResolverError,
    UnresolvedError,
)
from .netpolicy import ForbiddenNetworks, effective_ip
from .resolver import Resolution, ResolverAdapter

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class ConnectionPlan:
    """The only thing a connection adapter is allowed to act on.

    The plan is immutable: every field is frozen and the address collection is
    a tuple, so a plan cannot be silently mutated after authorization.
    """

    plan_id: str
    url: str
    scheme: str
    request_host: str
    cname_chain: tuple[str, ...]
    port: int
    host_header: str
    server_name: str
    approved_addresses: tuple[str, ...]
    selected_ip: str
    ttl_seconds: float
    created_at: float
    expires_at: float

    def is_expired(self, clock: Clock) -> bool:
        """Snapshot is valid while ``now < expires_at``; equality means stale."""
        return clock.now() >= self.expires_at

    def seconds_remaining(self, clock: Clock) -> float:
        return max(0.0, self.expires_at - clock.now())

    def allows_address(self, address: str) -> bool:
        """Membership test against the approved set, normalized by family."""
        try:
            candidate = effective_ip(address)
        except ValueError:
            return False
        approved = {effective_ip(ip) for ip in self.approved_addresses}
        return candidate in approved

    def describe(self) -> dict:
        """Redact-free, JSON-safe summary for audit logs."""
        return {
            "plan_id": self.plan_id,
            "url": self.url,
            "request_host": self.request_host,
            "cname_chain": list(self.cname_chain),
            "port": self.port,
            "host_header": self.host_header,
            "server_name": self.server_name,
            "approved_addresses": list(self.approved_addresses),
            "selected_ip": self.selected_ip,
            "ttl_seconds": self.ttl_seconds,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class ConnectionPlanner:
    """Builds a fresh :class:`ConnectionPlan` for every target URL.

    The planner holds no per-target cache: every call queries the resolver
    adapter again and re-runs every authorization check. Redirect handling
    code therefore cannot accidentally reuse a previous hop's resolution or
    authorization - it simply calls :meth:`plan_url` again.
    """

    def __init__(
        self,
        allow_list: DomainAllowList,
        forbidden: ForbiddenNetworks,
        resolver: ResolverAdapter,
        clock: Clock,
    ) -> None:
        self._allow = allow_list
        self._forbidden = forbidden
        self._resolver = resolver
        self._clock = clock

    @property
    def resolver(self) -> ResolverAdapter:
        return self._resolver

    def plan_url(self, url: str) -> ConnectionPlan:
        parsed, scheme, host, explicit_port = self._parse_url(url)
        port = explicit_port if explicit_port is not None else _DEFAULT_PORTS[scheme]
        host_header = host if explicit_port is None else f"{host}:{explicit_port}"

        # 1. Fresh resolution. No cache is consulted or populated.
        resolution = self._query(host)

        # 2. Every name on the full CNAME chain must satisfy the domain rules.
        self._check_chain(host, resolution)

        # 3. The *whole* address set is checked; one forbidden entry rejects all.
        offending = self._forbidden.offending(list(resolution.addresses))
        if offending:
            raise ForbiddenAddressError(
                "resolved address set contains a forbidden network",
                host=host,
                offending=offending,
            )

        # Normalize addresses (deduplicated, sorted) so plan equality is stable.
        normalized = sorted(
            {str(effective_ip(addr)) for addr in resolution.addresses},
            key=self._sort_key,
        )
        selected = normalized[0]

        now = self._clock.now()
        return ConnectionPlan(
            plan_id=uuid.uuid4().hex,
            url=url,
            scheme=scheme,
            request_host=host,
            cname_chain=resolution.cname_chain,
            port=port,
            host_header=host_header,
            # SNI always carries the bare hostname, never an IP or a port.
            server_name=host,
            approved_addresses=tuple(normalized),
            selected_ip=selected,
            ttl_seconds=resolution.ttl_seconds,
            created_at=now,
            expires_at=now + resolution.ttl_seconds,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_url(url: str) -> tuple[urllib.parse.ParseResult, str, str, int | None]:
        if not isinstance(url, str) or not url.strip():
            raise InvalidUrlError("url must be a non-empty string")
        try:
            parsed = urllib.parse.urlsplit(url.strip())
        except ValueError as exc:
            raise InvalidUrlError(f"malformed URL {url!r}: {exc}") from exc
        scheme = parsed.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            raise InvalidUrlError(f"unsupported scheme {parsed.scheme!r}; only http/https")
        if not parsed.hostname:
            raise InvalidUrlError("URL has no hostname component")
        if parsed.username is not None or parsed.password is not None:
            raise InvalidUrlError("userinfo (user:pass@) is not allowed in target URLs")
        try:
            host = normalize_hostname(parsed.hostname)
        except Exception as exc:  # normalize raises InvalidRuleError subclasses
            raise InvalidUrlError(f"invalid target hostname: {exc}") from exc
        # A literal IP as target is not accepted through domain rules.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise InvalidUrlError("literal IP targets are not allowed; use a hostname")
        if parsed.port is not None and not (0 < parsed.port < 65536):
            raise InvalidUrlError(f"port out of range: {parsed.port}")
        return parsed, scheme, host, parsed.port

    def _query(self, host: str) -> Resolution:
        try:
            resolution = self._resolver.resolve(host)
        except KeyError as exc:
            raise UnresolvedError(f"resolver has no record for {host!r}", host=host) from exc
        except UnresolvedError:
            raise
        except Exception as exc:
            raise ResolverError(f"resolver failed for {host!r}: {exc}", host=host) from exc
        if not isinstance(resolution, Resolution):
            raise ResolverError(
                f"resolver returned {type(resolution).__name__}, expected Resolution",
                host=host,
            )
        if not resolution.addresses:
            raise UnresolvedError(f"resolver returned no addresses for {host!r}", host=host)
        if resolution.ttl_seconds <= 0:
            raise ResolverError(f"resolver returned non-positive TTL for {host!r}", host=host)
        if not resolution.cname_chain or resolution.cname_chain[0] != host:
            raise ResolverError(
                f"resolver CNAME chain for {host!r} must start with the queried hostname",
                host=host,
            )
        # Validate every address parses; offending() also does this but we want
        # a resolver error rather than a rule error for adapter-produced junk.
        for addr in resolution.addresses:
            try:
                ipaddress.ip_address(addr)
            except ValueError as exc:
                raise ResolverError(
                    f"resolver returned malformed address {addr!r}", host=host
                ) from exc
        return resolution

    def _check_chain(self, host: str, resolution: Resolution) -> None:
        for name in resolution.cname_chain:
            if not self._allow.allows(name):
                raise DomainRuleError(
                    f"CNAME chain member {name!r} is not covered by the domain allow list",
                    host=host,
                    offender=name,
                )

    @staticmethod
    def _sort_key(address: str) -> tuple[int, ...]:
        # IPv4 first (version 4 sorts before 6), then numeric order.
        parsed = effective_ip(address)
        return (parsed.version,) + tuple(parsed.packed)
