#!/usr/bin/env python3
"""Local verification entry point for the imgplan connection planner.

Usage:
    python3 tools/local_verify.py --list
    python3 tools/local_verify.py --case C01
    python3 tools/local_verify.py --case all

Each case exercises the real implementation with real assertions. The process
exits non-zero on any failed assertion, an uncaught error, a missing entry
point, zero assertions, or an entire skipped group. One JSON record per case
is printed to stdout; nothing here performs real network I/O.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import traceback
from dataclasses import dataclass, field

# Make the workspace importable when the script is run by absolute path.
WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE not in sys.path:
    sys.path.insert(0, WORKSPACE)

import imgplan  # noqa: E402
from imgplan import (  # noqa: E402
    ConnectionPlanner,
    DomainAllowList,
    FakeClock,
    ForbiddenAddressError,
    ForbiddenNetworks,
    InMemoryResolver,
    DomainRuleError,
    InvalidRuleError,
    InvalidUrlError,
    PlannedConnector,
    PlannedSession,
    PlanError,
    PlanExpiredError,
)
from imgplan.netpolicy import effective_ip, is_ipv4_mapped  # noqa: E402


@dataclass
class CaseResult:
    case: str
    title: str
    assertions: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    seed: dict = field(default_factory=dict)
    expected: list[str] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)

    def check(self, condition: bool, label: str, *, expected: str, observed: str) -> None:
        """Record one real assertion."""
        self.assertions += 1
        if condition:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(f"{label} | expected={expected} observed={observed}")
        self.expected.append(f"{label}: {expected}")
        self.observed.append(f"{label}: {observed}")

    def check_raises(
        self, exc_type, callable_, label: str, *, code: str | None = None
    ) -> Exception:
        """Assert that callable_ raises exc_type (and optionally an error code)."""
        self.assertions += 1
        try:
            callable_()
        except exc_type as exc:  # type: ignore[misc]
            if code is not None and getattr(exc, "code", None) != code:
                self.failed += 1
                self.failures.append(
                    f"{label} | expected code={code} observed={getattr(exc, 'code', None)}"
                )
            else:
                self.passed += 1
            self.expected.append(f"{label}: raises {exc_type.__name__} code={code}")
            self.observed.append(
                f"{label}: raised {type(exc).__name__} code={getattr(exc, 'code', None)}"
            )
            return exc
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            self.failures.append(
                f"{label} | expected {exc_type.__name__} observed {type(exc).__name__}: {exc}"
            )
            self.expected.append(f"{label}: raises {exc_type.__name__}")
            self.observed.append(f"{label}: raised {type(exc).__name__}: {exc}")
            return exc
        else:
            self.failed += 1
            self.failures.append(f"{label} | expected raise {exc_type.__name__} observed=None")
            self.expected.append(f"{label}: raises {exc_type.__name__}")
            self.observed.append(f"{label}: no exception")
            return RuntimeError("expected exception not raised")

    def to_record(self) -> dict:
        return {
            "case": self.case,
            "title": self.title,
            "assertions": self.assertions,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "seed": self.seed,
            "expected_summary": self.expected,
            "observed_summary": self.observed,
            "failures": self.failures,
        }


# --------------------------------------------------------------------------- #
# C01 - label rules, full CNAME chain authorization, full address set rejection
# --------------------------------------------------------------------------- #


def case_c01() -> CaseResult:
    r = CaseResult(
        "C01",
        "domain label rules, complete CNAME chain and full address set policy",
    )
    r.seed = {
        "allow_rules": ["cdn.example.com", ".media.example.org", "*.img.example.net"],
        "forbidden": ["10.0.0.0/8", "198.51.100.0/24", "2001:db8:dead::/48"],
        "samples": "synthetic RFC5737/ documentation IPv6 addresses only",
    }
    allow = DomainAllowList(["cdn.example.com", ".media.example.org", "*.img.example.net"])
    forbidden = ForbiddenNetworks(["10.0.0.0/8", "198.51.100.0/24", "2001:db8:dead::/48"])
    clock = FakeClock(1000.0)
    resolver = InMemoryResolver()
    planner = ConnectionPlanner(allow, forbidden, resolver, clock)

    # Label rule semantics.
    r.check(allow.allows("cdn.example.com"), "exact rule hit",
            expected="True", observed=str(allow.allows("cdn.example.com")))
    r.check(not allow.allows("www.cdn.example.com"), "exact rule rejects sub-domain",
            expected="False", observed=str(allow.allows("www.cdn.example.com")))
    r.check(allow.allows("media.example.org"), "suffix rule hits apex",
            expected="True", observed=str(allow.allows("media.example.org")))
    r.check(allow.allows("a.b.media.example.org"), "suffix rule hits deep sub-domain",
            expected="True", observed=str(allow.allows("a.b.media.example.org")))
    r.check(not allow.allows("xmedia.example.org"), "suffix keeps label boundary",
            expected="False", observed=str(allow.allows("xmedia.example.org")))
    r.check(allow.allows("a.img.example.net"), "wildcard hits one label",
            expected="True", observed=str(allow.allows("a.img.example.net")))
    r.check(not allow.allows("img.example.net"), "wildcard rejects apex",
            expected="False", observed=str(allow.allows("img.example.net")))
    r.check(not allow.allows("a.b.img.example.net"), "wildcard rejects two labels",
            expected="False", observed=str(allow.allows("a.b.img.example.net")))
    r.check(allow.allows("CDN.Example.COM."), "case and trailing dot normalized",
            expected="True", observed=str(allow.allows("CDN.Example.COM.")))

    r.check_raises(InvalidRuleError, lambda: DomainAllowList(["*cdn.example.com"]),
                   "partial-label wildcard rejected", code="invalid_rule")
    r.check_raises(InvalidRuleError, lambda: DomainAllowList(["foo.*.com"]),
                   "mid-label wildcard rejected", code="invalid_rule")

    # Happy path: every chain name allowed, full address set clean.
    resolver.set_record(
        "img1.img.example.net",
        ["img1.img.example.net", "cache.media.example.org"],
        ["2001:db8:1::10", "203.0.113.10"],
        ttl_seconds=30,
    )
    plan = planner.plan_url("https://img1.img.example.net/path?token=abc")
    r.check(plan.selected_ip == "203.0.113.10", "IPv4 selected deterministically first",
            expected="203.0.113.10", observed=plan.selected_ip)
    r.check(
        list(plan.approved_addresses) == ["203.0.113.10", "2001:db8:1::10"],
        "approved set is full normalized sorted set",
        expected="['203.0.113.10', '2001:db8:1::10']",
        observed=str(list(plan.approved_addresses)),
    )
    r.check(
        list(plan.cname_chain) == ["img1.img.example.net", "cache.media.example.org"],
        "plan retains full CNAME chain",
        expected="two-name chain", observed=str(list(plan.cname_chain)),
    )

    # Chain member outside the allow list rejects even though the host is allowed.
    resolver.set_record(
        "img2.img.example.net",
        ["img2.img.example.net", "relay.attacker.test"],
        ["203.0.113.20"],
        ttl_seconds=30,
    )
    exc = r.check_raises(
        DomainRuleError,
        lambda: planner.plan_url("https://img2.img.example.net/"),
        "evil CNAME chain member rejected",
        code="domain_not_allowed",
    )
    r.check(getattr(exc, "offender", None) == "relay.attacker.test",
            "offending chain name reported",
            expected="relay.attacker.test", observed=str(getattr(exc, "offender", None)))

    # Host itself not allowed.
    resolver.set_record("nope.other.test", ["nope.other.test"], ["203.0.113.30"], 30)
    r.check_raises(DomainRuleError, lambda: planner.plan_url("https://nope.other.test/"),
                   "unlisted host rejected", code="domain_not_allowed")

    # Any forbidden address in the whole set rejects the plan.
    resolver.set_record(
        "img3.img.example.net",
        ["img3.img.example.net", "cache.media.example.org"],
        ["203.0.113.40", "10.1.2.3"],
        ttl_seconds=30,
    )
    exc = r.check_raises(
        ForbiddenAddressError,
        lambda: planner.plan_url("https://img3.img.example.net/"),
        "forbidden IPv4 anywhere in set rejects",
        code="forbidden_address",
    )
    r.check(getattr(exc, "offending", []) == ["10.1.2.3"], "forbidden v4 reported",
            expected="['10.1.2.3']", observed=str(getattr(exc, "offending", [])))

    resolver.set_record(
        "img4.img.example.net",
        ["img4.img.example.net", "cache.media.example.org"],
        ["203.0.113.41", "2001:db8:dead::5"],
        ttl_seconds=30,
    )
    exc = r.check_raises(
        ForbiddenAddressError,
        lambda: planner.plan_url("https://img4.img.example.net/"),
        "forbidden IPv6 anywhere in set rejects",
        code="forbidden_address",
    )
    r.check(getattr(exc, "offending", []) == ["2001:db8:dead::5"],
            "forbidden v6 reported",
            expected="['2001:db8:dead::5']", observed=str(getattr(exc, "offending", [])))
    return r


# --------------------------------------------------------------------------- #
# C02 - mapped v6 judgment, ports/Host/SNI, adapter address pinning
# --------------------------------------------------------------------------- #


def case_c02() -> CaseResult:
    r = CaseResult("C02", "IPv4-mapped IPv6, port/Host/SNI plan fields, adapter pinning")
    r.seed = {
        "allow_rules": [".svc.example.org"],
        "forbidden": ["169.254.0.0/16", "10.0.0.0/8"],
        "samples": "link-local/metadata range used as synthetic forbidden target",
    }
    import ipaddress

    allow = DomainAllowList([".svc.example.org"])
    forbidden = ForbiddenNetworks(["169.254.0.0/16", "10.0.0.0/8"])
    clock = FakeClock(1000.0)
    resolver = InMemoryResolver()
    planner = ConnectionPlanner(allow, forbidden, resolver, clock)

    mapped = ipaddress.ip_address("::ffff:169.254.169.254")
    plain_v6 = ipaddress.ip_address("2001:db8::1")
    r.check(is_ipv4_mapped(mapped), "mapped v6 detected",
            expected="True", observed=str(is_ipv4_mapped(mapped)))
    r.check(not is_ipv4_mapped(plain_v6), "plain v6 not mapped",
            expected="False", observed=str(is_ipv4_mapped(plain_v6)))
    r.check(str(effective_ip(mapped)) == "169.254.169.254",
            "effective IP of mapped address is embedded v4",
            expected="169.254.169.254", observed=str(effective_ip(mapped)))
    r.check(forbidden.contains("::ffff:169.254.169.254"),
            "mapped address judged by embedded v4 against v4 network",
            expected="True", observed=str(forbidden.contains("::ffff:169.254.169.254")))
    r.check(not forbidden.contains("::ffff:203.0.113.7"),
            "clean mapped address not forbidden",
            expected="False", observed=str(forbidden.contains("::ffff:203.0.113.7")))

    # Mapped metadata address in a resolution set must reject the plan.
    resolver.set_record(
        "meta.svc.example.org", ["meta.svc.example.org"],
        ["::ffff:169.254.169.254"], ttl_seconds=15,
    )
    exc = r.check_raises(
        ForbiddenAddressError,
        lambda: planner.plan_url("https://meta.svc.example.org/latest/"),
        "mapped metadata IP rejected",
        code="forbidden_address",
    )
    r.check(getattr(exc, "offending", []) == ["169.254.169.254"],
            "mapped offending address reported in v4 form",
            expected="['169.254.169.254']", observed=str(getattr(exc, "offending", [])))

    # A clean mapped address is normalized into its v4 form inside the plan.
    resolver.set_record(
        "api.svc.example.org", ["api.svc.example.org"],
        ["::ffff:203.0.113.7", "2001:db8:2::7"], ttl_seconds=45,
    )
    plan = planner.plan_url("https://api.svc.example.org/v1/data")
    r.check(plan.selected_ip == "203.0.113.7",
            "mapped v6 normalized to v4 as selected IP",
            expected="203.0.113.7", observed=plan.selected_ip)
    r.check("203.0.113.7" in plan.approved_addresses and "::ffff:203.0.113.7"
            not in plan.approved_addresses,
            "approved set stores normalized v4 only",
            expected="v4 form present, mapped form absent",
            observed=str(list(plan.approved_addresses)))

    # Default and explicit ports, Host header and SNI.
    plan_https = planner.plan_url("https://api.svc.example.org/")
    r.check(plan_https.port == 443 and plan_https.host_header == "api.svc.example.org"
            and plan_https.server_name == "api.svc.example.org",
            "https default port 443, Host/SNI bare hostname",
            expected="443 / api.svc.example.org",
            observed=f"{plan_https.port} / {plan_https.host_header} / {plan_https.server_name}")
    plan_http = planner.plan_url("http://api.svc.example.org/")
    r.check(plan_http.port == 80, "http default port 80",
            expected="80", observed=str(plan_http.port))
    plan_custom = planner.plan_url("https://api.svc.example.org:8443/x")
    r.check(plan_custom.port == 8443 and plan_custom.host_header == "api.svc.example.org:8443"
            and plan_custom.server_name == "api.svc.example.org",
            "explicit port in Host header, SNI stays bare",
            expected="8443 / host:8443 / bare SNI",
            observed=f"{plan_custom.port} / {plan_custom.host_header} / {plan_custom.server_name}")

    # Adapter pinning: only plan addresses are dialable.
    connector = PlannedConnector(plan, clock)
    conn = connector.connect()
    r.check(conn.remote_ip == plan.selected_ip and conn.port == plan.port
            and conn.host_header == plan.host_header and conn.server_name == plan.server_name,
            "default connection uses selected plan IP and plan names",
            expected=plan.selected_ip, observed=f"{conn.remote_ip}:{conn.port}")
    r.check_raises(PlanError, lambda: connector.connect("198.51.100.99"),
                   "dial of unplanned address refused")
    r.check_raises(PlanError, lambda: connector.connect("169.254.169.254"),
                   "dial of forbidden address refused by plan pinning")
    for approved in plan.approved_addresses:
        ok_conn = connector.connect(approved)
        r.check(ok_conn.remote_ip == approved,
                f"every approved address remains dialable: {approved}",
                expected=approved, observed=ok_conn.remote_ip)

    # URL shape guards.
    r.check_raises(InvalidUrlError,
                   lambda: planner.plan_url("https://user:secret@api.svc.example.org/"),
                   "userinfo URL rejected", code="invalid_url")
    r.check_raises(InvalidUrlError, lambda: planner.plan_url("ftp://api.svc.example.org/"),
                   "non-http(s) scheme rejected", code="invalid_url")
    r.check_raises(InvalidUrlError, lambda: planner.plan_url("https://203.0.113.7/"),
                   "literal IP target rejected", code="invalid_url")
    return r


# --------------------------------------------------------------------------- #
# C03 - controlled clock: snapshot expiry, recompute, per-hop re-authorization
# --------------------------------------------------------------------------- #


def case_c03() -> CaseResult:
    r = CaseResult("C03", "snapshot TTL expiry, recompute and per-hop redirect re-authorization")
    r.seed = {
        "allow_rules": [".example.org", ".example.net"],
        "forbidden": ["10.0.0.0/8"],
        "clock_start": 1000.0,
        "ttl_seconds": 30,
    }
    allow = DomainAllowList([".example.org", ".example.net"])
    forbidden = ForbiddenNetworks(["10.0.0.0/8"])
    clock = FakeClock(1000.0)
    resolver = InMemoryResolver()
    planner = ConnectionPlanner(allow, forbidden, resolver, clock)

    resolver.set_record("a.example.org", ["a.example.org"], ["203.0.113.10"], 30)
    plan = planner.plan_url("https://a.example.org/")
    r.check(plan.created_at == 1000.0 and plan.expires_at == 1030.0,
            "expiry equals now + TTL",
            expected="created 1000 / expires 1030",
            observed=f"created {plan.created_at} / expires {plan.expires_at}")
    r.check(not plan.is_expired(clock), "fresh plan valid",
            expected="False", observed=str(plan.is_expired(clock)))
    clock.advance(29)
    r.check(not plan.is_expired(clock), "valid one second before expiry",
            expected="False", observed=str(plan.is_expired(clock)))
    r.check(plan.seconds_remaining(clock) == 1.0, "remaining TTL reported",
            expected="1.0", observed=str(plan.seconds_remaining(clock)))
    clock.advance(1)
    r.check(plan.is_expired(clock), "plan stale at expiry boundary",
            expected="True", observed=str(plan.is_expired(clock)))

    # Expired plan cannot be connected from; re-planning produces a new snapshot.
    connector = PlannedConnector(plan, clock)
    r.check_raises(PlanExpiredError, lambda: connector.connect(),
                   "expired plan refuses connection", code="plan_expired")
    plan2 = planner.plan_url("https://a.example.org/")
    r.check(plan2.expires_at == 1060.0 and plan2.plan_id != plan.plan_id,
            "recompute yields fresh snapshot with new id",
            expected="expires 1060 / new id",
            observed=f"expires {plan2.expires_at} / ids {plan.plan_id} != {plan2.plan_id}")
    r.check(PlannedConnector(plan2, clock).connect().remote_ip == "203.0.113.10",
            "re-planned connection succeeds",
            expected="203.0.113.10",
            observed=PlannedConnector(plan2, clock).connect().remote_ip)
    r.check(resolver.call_count("a.example.org") == 2,
            "each planning call re-queries the resolver, no cache",
            expected="2", observed=str(resolver.call_count("a.example.org")))

    # Redirect session: two hops, two independent plans and two queries.
    resolver.set_record("b.example.org", ["b.example.org"], ["203.0.113.20"], 30)
    session = PlannedSession(planner, clock)
    hop1 = session.follow("https://a.example.org/step1")
    hop2 = session.follow("https://b.example.org/step2")
    r.check(hop1.allowed and hop2.allowed, "both hops allowed",
            expected="True/True", observed=f"{hop1.allowed}/{hop2.allowed}")
    r.check(hop1.plan.plan_id != hop2.plan.plan_id,
            "each redirect hop gets its own plan id",
            expected="different ids",
            observed=f"{hop1.plan.plan_id} vs {hop2.plan.plan_id}")
    r.check(hop1.connection.plan_id != hop2.connection.plan_id,
            "connections are bound to per-hop plans",
            expected="different plan ids",
            observed=f"{hop1.connection.plan_id} vs {hop2.connection.plan_id}")
    r.check(resolver.call_sequence().count("a.example.org") == 3
            and resolver.call_sequence().count("b.example.org") == 1,
            "resolver queried once per hop target (third a.example.org query from hop 1)",
            expected="a=3, b=1",
            observed=f"a={resolver.call_sequence().count('a.example.org')}, "
                     f"b={resolver.call_sequence().count('b.example.org')}")

    # Hop 2 domain no longer allowed: rejection does not poison or reuse hop 1.
    resolver.set_record("evil.example.com", ["evil.example.com"], ["203.0.113.66"], 30)
    hop3 = session.follow("https://evil.example.com/step3")
    r.check(not hop3.allowed and hop3.rejection_code == "domain_not_allowed",
            "disallowed redirect hop rejected with code",
            expected="False/domain_not_allowed",
            observed=f"{hop3.allowed}/{hop3.rejection_code}")
    r.check(hop3.plan is None and len(session.hops) == 3,
            "rejected hop carries no plan and earlier plans remain in session",
            expected="hop3 plan None, 3 hops recorded",
            observed=f"hop3 plan={hop3.plan}, hops={len(session.hops)}")

    # Same hostname, changed resolution between hops: no address carry-over.
    resolver.set_record("c.example.net", ["c.example.net"], ["203.0.113.30"], 30)
    h1 = session.follow("https://c.example.net/v1")
    resolver.replace_addresses("c.example.net", ["203.0.113.31"])
    h2 = session.follow("https://c.example.net/v2")
    r.check(h1.plan.approved_addresses == ("203.0.113.30",)
            and h2.plan.approved_addresses == ("203.0.113.31",),
            "each hop resolves anew; old plan keeps old frozen addresses",
            expected="hop1 .30 / hop2 .31",
            observed=f"hop1 {h1.plan.approved_addresses} / hop2 {h2.plan.approved_addresses}")
    return r


# --------------------------------------------------------------------------- #
# C04 - mutable in-memory adapter: rebinding cannot replace approved addresses
# --------------------------------------------------------------------------- #

FORBIDDEN_IMPORT_ROOTS = (
    "socket", "ssl", "http.client", "urllib.request", "urllib.response",
    "requests", "aiohttp", "httpx", "subprocess", "asyncio.base_subprocess",
)


def _scan_no_network_modules() -> list[str]:
    """Static scan: production modules must not import real-network libraries."""
    pkg_dir = os.path.join(WORKSPACE, "imgplan")
    violations: list[str] = []
    for name in sorted(os.listdir(pkg_dir)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(pkg_dir, name)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if alias.name in FORBIDDEN_IMPORT_ROOTS or root in (
                        "socket", "ssl", "requests", "aiohttp", "httpx", "subprocess"
                    ):
                        violations.append(f"{name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in FORBIDDEN_IMPORT_ROOTS or node.module.split(".")[0] in (
                    "socket", "ssl", "requests", "aiohttp", "httpx", "subprocess"
                ):
                    violations.append(f"{name}: from {node.module} import ...")
    return violations


def case_c04() -> CaseResult:
    r = CaseResult("C04", "DNS rebinding via mutable memory adapter; approved IPs cannot be swapped")
    r.seed = {
        "allow_rules": [".example.org"],
        "forbidden": ["10.0.0.0/8", "169.254.0.0/16"],
        "mutation_sequence": [
            "203.0.113.50 (clean, approved)",
            "198.51.100.50 (clean, not in approved plan)",
            "10.0.0.66 (forbidden)",
            "203.0.113.50 + 10.9.9.9 (approved plus unauthorized extra)",
            "203.0.113.50 (back to clean)",
        ],
        "network": "none; in-memory resolver and static source scan",
    }
    allow = DomainAllowList([".example.org"])
    forbidden = ForbiddenNetworks(["10.0.0.0/8", "169.254.0.0/16"])
    clock = FakeClock(2000.0)
    resolver = InMemoryResolver()
    planner = ConnectionPlanner(allow, forbidden, resolver, clock)

    resolver.set_record("rebind.example.org", ["rebind.example.org"], ["203.0.113.50"], 60)
    plan_a = planner.plan_url("https://rebind.example.org/fetch")
    r.check(plan_a.selected_ip == "203.0.113.50" and plan_a.approved_addresses == ("203.0.113.50",),
            "first resolution approved and pinned",
            expected="203.0.113.50", observed=str(plan_a.approved_addresses))

    # Rebinding: adapter now answers a different, clean IP.
    resolver.replace_addresses("rebind.example.org", ["198.51.100.50"])
    connector = PlannedConnector(plan_a, clock)
    r.check_raises(PlanError, lambda: connector.connect(resolver=resolver),
                   "pre-connect re-query cannot swap approved IP")
    r.check_raises(PlanError, lambda: connector.connect("198.51.100.50"),
                   "direct dial of rebound address refused")
    r.check(plan_a.selected_ip == "203.0.113.50"
            and plan_a.approved_addresses == ("203.0.113.50",),
            "plan stays frozen at originally approved address",
            expected="still 203.0.113.50", observed=str(plan_a.approved_addresses))
    r.check(connector.connect().remote_ip == "203.0.113.50",
            "approved IP remains dialable despite rebinding",
            expected="203.0.113.50", observed=connector.connect().remote_ip)

    # Rebind to a forbidden address: a fresh plan must be rejected.
    resolver.replace_addresses("rebind.example.org", ["10.0.0.66"])
    r.check_raises(ForbiddenAddressError,
                   lambda: planner.plan_url("https://rebind.example.org/fetch"),
                   "next-hop planning re-runs policy and rejects forbidden rebind",
                   code="forbidden_address")
    r.check(resolver.call_count("rebind.example.org") >= 3,
            "re-planning really re-queries the name each time",
            expected=">=3 queries",
            observed=str(resolver.call_count("rebind.example.org")))

    # Authorized answer plus an unauthorized extra: the extra must be refused.
    resolver.replace_addresses("rebind.example.org", ["203.0.113.50", "10.9.9.9"])
    r.check_raises(PlanError, lambda: connector.connect(resolver=resolver),
                   "fresh answer containing unauthorized extra address refused")

    # Rebind back to the clean address: a new plan can be authorized, old one unchanged.
    resolver.replace_addresses("rebind.example.org", ["203.0.113.50"])
    plan_b = planner.plan_url("https://rebind.example.org/fetch")
    r.check(plan_b.plan_id != plan_a.plan_id and plan_b.selected_ip == "203.0.113.50",
            "re-authorization creates a new plan with current resolution",
            expected="new plan id, same clean IP",
            observed=f"{plan_a.plan_id} -> {plan_b.plan_id}")
    r.check(plan_a.plan_id != plan_b.plan_id and plan_a.selected_ip == "203.0.113.50",
            "original plan remains immutable after later re-plans",
            expected="plan A untouched", observed=f"plan A ip={plan_a.selected_ip}")

    # Static proof that no real network stack is imported by production code.
    violations = _scan_no_network_modules()
    r.check(violations == [], "no socket/ssl/http-client/subprocess imports in imgplan",
            expected="[]", observed=str(violations))
    r.check(resolver.total_calls == resolver.call_count("rebind.example.org"),
            "all resolver activity stays inside the in-memory adapter",
            expected=str(resolver.total_calls),
            observed=str(resolver.call_count("rebind.example.org")))
    return r


CASES = {
    "C01": ("Domain labels, CNAME chain and full address-set policy", case_c01),
    "C02": ("IPv4-mapped IPv6, ports/Host/SNI and adapter address pinning", case_c02),
    "C03": ("Clock-driven TTL expiry, recompute and per-hop re-authorization", case_c03),
    "C04": ("Mutable in-memory rebinding cannot replace approved addresses", case_c04),
}


def run_case(case_id: str) -> CaseResult:
    _, fn = CASES[case_id]
    try:
        return fn()
    except Exception:  # noqa: BLE001 - turn unexpected errors into recorded failures
        result = CaseResult(case_id, CASES[case_id][0])
        result.assertions = 1
        result.failed = 1
        result.failures.append("uncaught error: " + traceback.format_exc())
        result.expected.append("case runs without uncaught errors")
        result.observed.append("uncaught exception (see failures)")
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="imgplan local verification")
    parser.add_argument("--list", action="store_true", help="list available cases")
    parser.add_argument("--case", dest="case_id", help="case id to run, or 'all'")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(
            [{"case": cid, "title": title} for cid, (title, _) in CASES.items()],
            indent=2, ensure_ascii=False,
        ))
        return 0

    if not args.case_id:
        parser.error("--case is required (or use --list)")
    if args.case_id != "all" and args.case_id not in CASES:
        print(json.dumps({"error": f"unknown case {args.case_id!r}",
                          "available": list(CASES)}, ensure_ascii=False))
        return 2

    selected = list(CASES) if args.case_id == "all" else [args.case_id]
    overall_ok = True
    for cid in selected:
        result = run_case(cid)
        print(json.dumps(result.to_record(), indent=2, ensure_ascii=False))
        # Zero assertions or any failure (nothing is ever skipped) is fatal.
        if result.assertions == 0 or result.failed or result.skipped:
            overall_ok = False
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
