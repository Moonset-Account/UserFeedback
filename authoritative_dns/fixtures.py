"""Sample in-memory zone used by tests, the CLI demo and the README."""

from __future__ import annotations

from .zone import Zone

ORIGIN = "example."
DEFAULT_TTL = 300


def sample_zone() -> Zone:
    """Build the synthetic zone mirrored by ``zones/example.zone``.

    Layout (all local, no network)::

        example.            NS ns1, A 192.0.2.1, AAAA 2001:db8::1, TXT "apex"
        ns1.example.        A 192.0.2.53
        www.example.        A 192.0.2.10, AAAA 2001:db8::10, TXT "hello" "world"
        only-a.example.     A 192.0.2.11
        a.b.example.        A 192.0.2.20      (makes b.example an empty node)
        c.b.example.        A 192.0.2.21
        *.example.          A 192.0.2.99, TXT "wild"
        child.example.      NS ns1.child.example., ns2.child.example.
        deep.sub.example.   NS ns1.deep.sub.example.
    """
    zone = Zone(ORIGIN, default_ttl=DEFAULT_TTL)
    zone.add("example.", "NS", "ns1.example.")
    zone.add("example.", "A", "192.0.2.1")
    zone.add("example.", "AAAA", "2001:db8::1")
    zone.add("example.", "TXT", "apex")
    zone.add("ns1.example.", "A", "192.0.2.53")
    zone.add("www.example.", "A", "192.0.2.10")
    zone.add("www.example.", "AAAA", "2001:db8::10")
    zone.add("www.example.", "TXT", ("hello", "world"))
    zone.add("only-a.example.", "A", "192.0.2.11")
    zone.add("a.b.example.", "A", "192.0.2.20")
    zone.add("c.b.example.", "A", "192.0.2.21")
    zone.add("*.example.", "A", "192.0.2.99")
    zone.add("*.example.", "TXT", "wild")
    zone.add("child.example.", "NS", "ns1.child.example.")
    zone.add("child.example.", "NS", "ns2.child.example.")
    zone.add("deep.sub.example.", "NS", "ns1.deep.sub.example.")
    return zone
