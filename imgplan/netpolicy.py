"""Forbidden IP network policy.

The policy knows how to judge an address family correctly when an IPv4 address
is embedded in an IPv4-mapped IPv6 address (``::ffff:a.b.c.d``): the mapped
address is checked against IPv4 networks using the embedded IPv4 value, so a
forbidden IPv4 range cannot be bypassed by presenting the same address in its
IPv6 form.
"""

from __future__ import annotations

import ipaddress

from .errors import InvalidRuleError


def is_ipv4_mapped(address: ipaddress.IPv6Address) -> bool:
    """Return True when *address* is an IPv4-mapped IPv6 address (``::ffff/96``)."""
    return address.ipv4_mapped is not None


def effective_ip(
    address: str | ipaddress._BaseAddress,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the address that policy checks must be performed against.

    For an IPv4-mapped IPv6 address the embedded :class:`IPv4Address` is
    returned; every other address is returned parsed unchanged.
    """
    parsed = ipaddress.ip_address(address) if isinstance(address, str) else address
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


class ForbiddenNetworks:
    """An immutable set of forbidden IPv4/IPv6 networks."""

    def __init__(self, networks: list[str] | tuple[str, ...] = ()) -> None:
        v4: list[ipaddress.IPv4Network] = []
        v6: list[ipaddress.IPv6Network] = []
        for raw in networks:
            if not isinstance(raw, str) or not raw.strip():
                raise InvalidRuleError("forbidden network must be a non-empty string")
            try:
                net = ipaddress.ip_network(raw.strip(), strict=False)
            except ValueError as exc:
                raise InvalidRuleError(f"malformed forbidden network {raw!r}: {exc}") from exc
            if isinstance(net, ipaddress.IPv4Network):
                v4.append(net)
            else:
                v6.append(net)
        self._v4 = tuple(v4)
        self._v6 = tuple(v6)
        self._raw = tuple(networks)

    @property
    def raw_rules(self) -> tuple[str, ...]:
        return self._raw

    def contains(self, address: str | ipaddress._BaseAddress) -> bool:
        """Return True when *address* is covered by a forbidden network."""
        parsed = ipaddress.ip_address(address) if isinstance(address, str) else address
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            # Judge by the real IPv4 address against IPv4 rules only.
            return any(parsed.ipv4_mapped in net for net in self._v4)
        if isinstance(parsed, ipaddress.IPv4Address):
            return any(parsed in net for net in self._v4)
        return any(parsed in net for net in self._v6)

    def offending(self, addresses: list[str]) -> list[str]:
        """Return the normalized forbidden members of *addresses*, sorted."""
        found: set[str] = set()
        for raw in addresses:
            try:
                parsed = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise InvalidRuleError(f"malformed resolved address {raw!r}") from exc
            if self.contains(parsed):
                found.add(str(effective_ip(parsed)))
        return sorted(found)
