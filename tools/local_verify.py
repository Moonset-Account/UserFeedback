#!/usr/bin/env python3
"""Local verification wrapper for the path_validation module.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case C02
    python tools/local_verify.py --case C03
    python tools/local_verify.py --case C04

Each case builds synthetic RSA/ECDSA certificate chains locally (no
network, no external services), runs the real ``path_validation``
implementation and performs real assertions.  Output is a JSON record
with the case id, assertion counts and expected/observed summaries.
Exit code is non-zero if any assertion fails, no assertion ran, or a
whole group was skipped.  No credentials or private data are printed;
certificates are identified by SHA-256 fingerprint only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import dsa, ec, rsa  # noqa: E402
from cryptography.x509.oid import (  # noqa: E402
    ExtendedKeyUsageOID,
    NameOID,
    ObjectIdentifier,
)

import path_validation as pv  # noqa: E402

# Fixed validation instant -> deterministic, replayable samples.
NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
VALID_FROM = NOW - timedelta(days=300)
VALID_TO = NOW + timedelta(days=300)
SEED_DESC = "fixed-time:2026-11-18T00:00:00Z;keys:random-per-run"


# ---------------------------------------------------------------------------
# Synthetic certificate helpers
# ---------------------------------------------------------------------------

_SERIAL = [1000]


def _next_serial() -> int:
    _SERIAL[0] += 1
    return _SERIAL[0]


def _cn(name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])


def rsa_key(bits: int = 2048):
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def build_cert(
    *,
    subject_cn: str,
    subject_key,
    issuer_cert: x509.Certificate | None,
    issuer_key,
    is_ca: bool,
    path_len: int | None = None,
    key_cert_sign: bool = True,
    san_dns: tuple[str, ...] = (),
    san_ips: tuple[str, ...] = (),
    eku_oids=None,
    not_before: datetime = VALID_FROM,
    not_after: datetime = VALID_TO,
    nc_permitted=None,
    nc_excluded=None,
    extra_critical_oid: str | None = None,
) -> x509.Certificate:
    """Build a certificate; self-signed when issuer_cert is None."""
    b = x509.CertificateBuilder()
    b = b.subject_name(_cn(subject_cn))
    b = b.issuer_name(issuer_cert.subject if issuer_cert else _cn(subject_cn))
    b = b.public_key(subject_key.public_key())
    b = b.serial_number(_next_serial())
    b = b.not_valid_before(not_before).not_valid_after(not_after)
    b = b.add_extension(x509.BasicConstraints(ca=is_ca, path_length=path_len), critical=True)
    if is_ca:
        b = b.add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=key_cert_sign,
                crl_sign=key_cert_sign,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    if san_dns or san_ips:
        names = [x509.DNSName(d) for d in san_dns] + [
            x509.IPAddress(ipaddress.ip_address(a)) for a in san_ips
        ]
        b = b.add_extension(x509.SubjectAlternativeName(names), critical=False)
    if eku_oids is not None:
        b = b.add_extension(x509.ExtendedKeyUsage(list(eku_oids)), critical=False)
    if nc_permitted is not None or nc_excluded is not None:
        b = b.add_extension(
            x509.NameConstraints(
                permitted_subtrees=nc_permitted, excluded_subtrees=nc_excluded
            ),
            critical=True,
        )
    if extra_critical_oid is not None:
        b = b.add_extension(
            x509.UnrecognizedExtension(ObjectIdentifier(extra_critical_oid), b"\x05\x00"),
            critical=True,
        )
    return b.sign(issuer_key, hashes.SHA256())


def make_ca(cn: str, key, **kw) -> x509.Certificate:
    return build_cert(subject_cn=cn, subject_key=key, issuer_cert=None,
                      issuer_key=key, is_ca=True, **kw)


def fp(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def run(pool, anchors, target, when: datetime = NOW) -> pv.ValidationResult:
    return pv.validate(pool=pool, anchors=anchors, validation_time=when, target=target)


# ---------------------------------------------------------------------------
# Assertion bookkeeping
# ---------------------------------------------------------------------------


class CaseRunner:
    def __init__(self, case_id: str):
        self.case_id = case_id
        self.checks: list[dict] = []
        self.samples: dict[str, str] = {}

    def sample(self, name: str, cert: x509.Certificate) -> None:
        self.samples[name] = fp(cert)

    def check(self, name: str, expected, observed) -> bool:
        ok = expected == observed
        self.checks.append(
            {
                "name": name,
                "ok": ok,
                "expected": _short(expected),
                "observed": _short(observed),
            }
        )
        return ok

    def check_true(self, name: str, cond: bool, observed) -> bool:
        return self.check(name, True, bool(cond) if observed is None else (cond and observed))

    def record(self) -> dict:
        passed = sum(1 for c in self.checks if c["ok"])
        failed = sum(1 for c in self.checks if not c["ok"])
        return {
            "case": self.case_id,
            "seed": SEED_DESC,
            "assertions": len(self.checks),
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "samples": self.samples,
            "checks": self.checks,
        }


def _short(value, limit: int = 160):
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 3] + "..."


def first_reasons(result: pv.ValidationResult) -> list[str]:
    return [f.reason for f in result.failures]


# ---------------------------------------------------------------------------
# C01: RSA/ECDSA chains, signatures, validity, CA/keyCertSign/pathLen/EKU
# ---------------------------------------------------------------------------


def case_c01() -> CaseRunner:
    r = CaseRunner("C01")

    # --- RSA chain: root -> intermediate (pathLen=0) -> leaf, valid ---
    root_k, interm_k, leaf_k = rsa_key(), rsa_key(), rsa_key()
    root = make_ca("C01 RSA Root", root_k)
    interm = build_cert(subject_cn="C01 RSA Intermediate", subject_key=interm_k,
                        issuer_cert=root, issuer_key=root_k, is_ca=True, path_len=0)
    leaf = build_cert(subject_cn="C01 RSA Leaf", subject_key=leaf_k,
                      issuer_cert=interm, issuer_key=interm_k, is_ca=False,
                      san_dns=("www.example.com",),
                      eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    for name, cert in [("rsa_root", root), ("rsa_interm", interm), ("rsa_leaf", leaf)]:
        r.sample(name, cert)
    res = run([leaf, interm], [root], "www.example.com")
    r.check("rsa_chain_valid", True, res.ok)
    r.check("rsa_path_length", 3, len(res.path or []))
    r.check("rsa_path_leaf_first", fp(leaf), (res.path or [None])[0] and res.path[0].sha256)
    r.check("rsa_path_anchor_last", fp(root), (res.path or [None])[-1] and res.path[-1].sha256)
    r.check("rsa_anchor_flagged", True, bool(res.path and res.path[-1].is_anchor))
    r.check("rsa_pathlen_source", fp(interm),
            res.path_len_limits[0]["source_sha256"] if res.path_len_limits else None)

    # --- ECDSA chain with mixed RSA root ---
    ec_root_k, ec_interm_k, ec_leaf_k = ec_key(), ec_key(), ec_key()
    ec_root = make_ca("C01 EC Root", ec_root_k)
    ec_interm = build_cert(subject_cn="C01 EC Intermediate", subject_key=ec_interm_k,
                           issuer_cert=ec_root, issuer_key=ec_root_k, is_ca=True)
    ec_leaf = build_cert(subject_cn="C01 EC Leaf", subject_key=ec_leaf_k,
                         issuer_cert=ec_interm, issuer_key=ec_interm_k, is_ca=False,
                         san_dns=("ec.example.com",),
                         eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    for name, cert in [("ec_root", ec_root), ("ec_interm", ec_interm), ("ec_leaf", ec_leaf)]:
        r.sample(name, cert)
    res = run([ec_leaf, ec_interm], [ec_root], "ec.example.com")
    r.check("ecdsa_chain_valid", True, res.ok)
    r.check("ecdsa_path_length", 3, len(res.path or []))

    # --- expired leaf ---
    old_leaf = build_cert(subject_cn="C01 Expired Leaf", subject_key=rsa_key(),
                          issuer_cert=interm, issuer_key=interm_k, is_ca=False,
                          san_dns=("www.example.com",),
                          eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH],
                          not_before=NOW - timedelta(days=600),
                          not_after=NOW - timedelta(days=1))
    res = run([old_leaf, interm], [root], "www.example.com")
    r.check("expired_leaf_rejected", False, res.ok)
    r.check("expired_leaf_reason", "expired", first_reasons(res)[0] if res.failures else None)

    # --- not-yet-valid leaf ---
    future_leaf = build_cert(subject_cn="C01 Future Leaf", subject_key=rsa_key(),
                             issuer_cert=interm, issuer_key=interm_k, is_ca=False,
                             san_dns=("www.example.com",),
                             eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH],
                             not_before=NOW + timedelta(days=1),
                             not_after=NOW + timedelta(days=300))
    res = run([future_leaf, interm], [root], "www.example.com")
    r.check("not_yet_valid_reason", "not_yet_valid",
            first_reasons(res)[0] if res.failures else None)

    # --- intermediate that is not a CA ---
    nonca_k = rsa_key()
    nonca = build_cert(subject_cn="C01 NonCA", subject_key=nonca_k,
                       issuer_cert=root, issuer_key=root_k, is_ca=False)
    leaf2 = build_cert(subject_cn="C01 Leaf2", subject_key=rsa_key(),
                       issuer_cert=nonca, issuer_key=nonca_k, is_ca=False,
                       san_dns=("www.example.com",),
                       eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    res = run([leaf2, nonca], [root], "www.example.com")
    r.check("non_ca_rejected", False, res.ok)
    # `nonca` is itself a syntactically valid end-entity candidate, so the
    # validator also tries it as a leaf; the provable NOT_CA fact must be
    # recorded against the nonca certificate for the leaf2 chain.
    r.check("non_ca_reason", True,
            any(f.reason == "not_ca" and f.cert_sha256 == fp(nonca)
                for f in res.failures))

    # --- CA without keyCertSign ---
    nokcs_k = rsa_key()
    nokcs = build_cert(subject_cn="C01 NoKeyCertSign", subject_key=nokcs_k,
                       issuer_cert=root, issuer_key=root_k, is_ca=True,
                       key_cert_sign=False)
    leaf3 = build_cert(subject_cn="C01 Leaf3", subject_key=rsa_key(),
                       issuer_cert=nokcs, issuer_key=nokcs_k, is_ca=False,
                       san_dns=("www.example.com",),
                       eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    res = run([leaf3, nokcs], [root], "www.example.com")
    r.check("missing_keycertsign_reason", "missing_key_cert_sign",
            first_reasons(res)[0] if res.failures else None)

    # --- pathLenConstraint exceeded: root -> A(pathLen=0) -> B -> leaf ---
    a_k, b_k = rsa_key(), rsa_key()
    interm_a = build_cert(subject_cn="C01 Interm A", subject_key=a_k,
                          issuer_cert=root, issuer_key=root_k, is_ca=True, path_len=0)
    interm_b = build_cert(subject_cn="C01 Interm B", subject_key=b_k,
                          issuer_cert=interm_a, issuer_key=a_k, is_ca=True)
    leaf4 = build_cert(subject_cn="C01 Leaf4", subject_key=rsa_key(),
                       issuer_cert=interm_b, issuer_key=b_k, is_ca=False,
                       san_dns=("www.example.com",),
                       eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    res = run([leaf4, interm_b, interm_a], [root], "www.example.com")
    r.check("pathlen_exceeded_reason", "path_len_exceeded",
            first_reasons(res)[0] if res.failures else None)

    # --- leaf without serverAuth EKU ---
    leaf5 = build_cert(subject_cn="C01 Leaf5", subject_key=rsa_key(),
                       issuer_cert=interm, issuer_key=interm_k, is_ca=False,
                       san_dns=("www.example.com",),
                       eku_oids=[ExtendedKeyUsageOID.CLIENT_AUTH])
    res = run([leaf5, interm], [root], "www.example.com")
    r.check("missing_server_eku_reason", "missing_server_eku",
            first_reasons(res)[0] if res.failures else None)

    # --- unsupported key algorithm (DSA intermediate) ---
    dsa_k = dsa.generate_private_key(key_size=2048)
    dsa_interm = build_cert(subject_cn="C01 DSA Interm", subject_key=dsa_k,
                            issuer_cert=root, issuer_key=root_k, is_ca=True)
    leaf6 = build_cert(subject_cn="C01 Leaf6", subject_key=rsa_key(),
                       issuer_cert=dsa_interm, issuer_key=dsa_k, is_ca=False,
                       san_dns=("www.example.com",),
                       eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    res = run([leaf6, dsa_interm], [root], "www.example.com")
    r.check("dsa_interm_reason", "unsupported_key_algorithm",
            first_reasons(res)[0] if res.failures else None)
    return r


# ---------------------------------------------------------------------------
# C02: SAN (DNS/IP), nameConstraints, unknown critical extensions
# ---------------------------------------------------------------------------


def _c02_chain(r: CaseRunner, tag: str, *, san_dns=(), san_ips=(),
               nc_permitted=None, nc_excluded=None, extra_oid=None):
    root_k, interm_k, leaf_k = rsa_key(), rsa_key(), rsa_key()
    root = make_ca(f"C02 {tag} Root", root_k)
    interm = build_cert(subject_cn=f"C02 {tag} Interm", subject_key=interm_k,
                        issuer_cert=root, issuer_key=root_k, is_ca=True,
                        nc_permitted=nc_permitted, nc_excluded=nc_excluded,
                        extra_critical_oid=extra_oid)
    leaf = build_cert(subject_cn=f"C02 {tag} Leaf", subject_key=leaf_k,
                      issuer_cert=interm, issuer_key=interm_k, is_ca=False,
                      san_dns=san_dns, san_ips=san_ips,
                      eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    r.sample(f"{tag}_interm", interm)
    return root, interm, leaf


def case_c02() -> CaseRunner:
    r = CaseRunner("C02")

    # DNS SAN match / mismatch
    root, interm, leaf = _c02_chain(r, "dns", san_dns=("www.example.com",))
    res = run([leaf, interm], [root], "www.example.com")
    r.check("dns_san_match", True, res.ok)
    res = run([leaf, interm], [root], "other.example.com")
    r.check("dns_san_mismatch_reason", "name_mismatch",
            first_reasons(res)[0] if res.failures else None)

    # wildcard SAN
    root, interm, leaf = _c02_chain(r, "wild", san_dns=("*.example.com",))
    res = run([leaf, interm], [root], "www.example.com")
    r.check("wildcard_match", True, res.ok)
    res = run([leaf, interm], [root], "example.com")
    r.check("wildcard_no_apex_reason", "name_mismatch",
            first_reasons(res)[0] if res.failures else None)

    # IP SAN match / mismatch
    root, interm, leaf = _c02_chain(r, "ip", san_ips=("10.0.0.5",))
    res = run([leaf, interm], [root], "10.0.0.5")
    r.check("ip_san_match", True, res.ok)
    res = run([leaf, interm], [root], "10.0.0.6")
    r.check("ip_san_mismatch_reason", "name_mismatch",
            first_reasons(res)[0] if res.failures else None)

    # permitted DNS constraint satisfied; constraint source preserved
    root, interm, leaf = _c02_chain(
        r, "permitdns", san_dns=("www.example.com",),
        nc_permitted=[x509.DNSName("example.com")])
    res = run([leaf, interm], [root], "www.example.com")
    r.check("permitted_dns_ok", True, res.ok)
    r.check("constraint_source_recorded", fp(interm),
            res.name_constraints[0].source_sha256 if res.name_constraints else None)
    r.check("constraint_value_recorded", ["example.com"],
            res.name_constraints[0].permitted_dns if res.name_constraints else None)

    # excluded DNS constraint violated
    root, interm, leaf = _c02_chain(
        r, "excludedns", san_dns=("www.bad.example.com",),
        nc_excluded=[x509.DNSName("bad.example.com")])
    res = run([leaf, interm], [root], "www.bad.example.com")
    r.check("excluded_dns_reason", "name_constraint_violation",
            first_reasons(res)[0] if res.failures else None)

    # permitted DNS constraint not satisfied
    root, interm, leaf = _c02_chain(
        r, "outsidedns", san_dns=("www.other.org",),
        nc_permitted=[x509.DNSName("example.com")])
    res = run([leaf, interm], [root], "www.other.org")
    r.check("outside_permitted_dns_reason", "name_constraint_violation",
            first_reasons(res)[0] if res.failures else None)

    # permitted IP constraint satisfied / violated
    root, interm, leaf = _c02_chain(
        r, "permitip", san_ips=("10.0.0.5",),
        nc_permitted=[x509.IPAddress(ipaddress.ip_network("10.0.0.0/8"))])
    res = run([leaf, interm], [root], "10.0.0.5")
    r.check("permitted_ip_ok", True, res.ok)
    root, interm, leaf = _c02_chain(
        r, "outsideip", san_ips=("192.168.1.1",),
        nc_permitted=[x509.IPAddress(ipaddress.ip_network("10.0.0.0/8"))])
    res = run([leaf, interm], [root], "192.168.1.1")
    r.check("outside_permitted_ip_reason", "name_constraint_violation",
            first_reasons(res)[0] if res.failures else None)

    # excluded IP constraint violated
    root, interm, leaf = _c02_chain(
        r, "excludeip", san_ips=("10.1.2.3",),
        nc_excluded=[x509.IPAddress(ipaddress.ip_network("10.0.0.0/8"))])
    res = run([leaf, interm], [root], "10.1.2.3")
    r.check("excluded_ip_reason", "name_constraint_violation",
            first_reasons(res)[0] if res.failures else None)

    # unknown critical extension -> path not applicable
    root, interm, leaf = _c02_chain(r, "unknownext", san_dns=("www.example.com",),
                                    extra_oid="1.3.6.1.4.1.55555.1")
    res = run([leaf, interm], [root], "www.example.com")
    r.check("unknown_critical_rejected", False, res.ok)
    r.check("unknown_critical_reason", "unknown_critical_extension",
            first_reasons(res)[0] if res.failures else None)
    r.check("unknown_critical_category", "not_applicable",
            res.failures[0].category if res.failures else None)
    return r


# ---------------------------------------------------------------------------
# C03: same-subject / cross-signed intermediates, path selection
# ---------------------------------------------------------------------------


def case_c03() -> CaseRunner:
    r = CaseRunner("C03")

    root_b_k = rsa_key()
    root_b = make_ca("C03 Root B", root_b_k)          # the only anchor
    root_a_k = rsa_key()
    root_a = make_ca("C03 Root A", root_a_k)          # NOT an anchor

    # Direct intermediate: subject "Shared Intermediate", key K1, signed by Root B.
    i1_k = rsa_key()
    i_direct = build_cert(subject_cn="Shared Intermediate", subject_key=i1_k,
                          issuer_cert=root_b, issuer_key=root_b_k, is_ca=True)
    # Cross-signed: SAME key K1 and subject, but issued by Root A (untrusted).
    i_cross = build_cert(subject_cn="Shared Intermediate", subject_key=i1_k,
                         issuer_cert=root_a, issuer_key=root_a_k, is_ca=True)
    # Same subject, DIFFERENT key K2, signed by Root B, but carries an
    # unknown critical extension -> that candidate path is not applicable.
    i2_k = rsa_key()
    i_alt = build_cert(subject_cn="Shared Intermediate", subject_key=i2_k,
                       issuer_cert=root_b, issuer_key=root_b_k, is_ca=True,
                       extra_critical_oid="1.3.6.1.4.1.55555.2")

    leaf_k = rsa_key()
    leaf = build_cert(subject_cn="C03 Leaf", subject_key=leaf_k,
                      issuer_cert=i_direct, issuer_key=i1_k, is_ca=False,
                      san_dns=("svc.internal.example.com",),
                      eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])

    for name, cert in [("root_b_anchor", root_b), ("interm_direct", i_direct),
                       ("interm_cross_signed", i_cross),
                       ("interm_same_name_other_key", i_alt), ("leaf", leaf)]:
        r.sample(name, cert)

    r.check("same_subject_distinct_certs", True,
            i_direct.subject == i_alt.subject == i_cross.subject
            and len({fp(i_direct), fp(i_alt), fp(i_cross)}) == 3)

    # Without the direct intermediate only unusable candidates remain:
    # both same-subject candidates must be evaluated separately (never
    # merged by subject name) and each must yield its own first reason.
    res0 = run([leaf, i_alt, i_cross], [root_b], "svc.internal.example.com")
    r.check("no_valid_path_without_direct", False, res0.ok)
    r.check("not_applicable_candidate_recorded", True,
            any(f.reason == "unknown_critical_extension"
                and f.category == "not_applicable"
                and f.cert_sha256 == fp(i_alt) for f in res0.failures))
    r.check("cross_signed_missing_issuer", True,
            any(f.reason == "missing_issuer" for f in res0.failures))
    r.check("distinct_candidate_reasons", True,
            {"unknown_critical_extension", "missing_issuer"}
            <= set(first_reasons(res0)))

    # With all candidates present the validator must select the one chain
    # that actually applies: leaf -> i_direct -> Root B (anchor).
    res = run([leaf, i_direct, i_cross, i_alt], [root_b], "svc.internal.example.com")
    r.check("path_found", True, res.ok)
    r.check("selected_interm", fp(i_direct),
            res.path[1].sha256 if res.ok and len(res.path) > 1 else None)
    r.check("path_ends_at_anchor", fp(root_b),
            res.path[-1].sha256 if res.ok else None)

    # Sanity: with ONLY the cross-signed intermediate the path must fail.
    res2 = run([leaf, i_cross], [root_b], "svc.internal.example.com")
    r.check("cross_only_fails", False, res2.ok)
    r.check("cross_only_reason", "missing_issuer",
            first_reasons(res2)[0] if res2.failures else None)
    return r


# ---------------------------------------------------------------------------
# C04: self-signed non-anchor, missing issuer, bad signature, constraint
# failure, and a full successful path with constraint sources
# ---------------------------------------------------------------------------


def case_c04() -> CaseRunner:
    r = CaseRunner("C04")

    root_k, interm_k = rsa_key(), rsa_key()
    root = make_ca("C04 Root", root_k)
    interm = build_cert(subject_cn="C04 Interm", subject_key=interm_k,
                        issuer_cert=root, issuer_key=root_k, is_ca=True,
                        nc_permitted=[x509.DNSName("example.com")])
    leaf_k = rsa_key()
    leaf = build_cert(subject_cn="C04 Leaf", subject_key=leaf_k,
                      issuer_cert=interm, issuer_key=interm_k, is_ca=False,
                      san_dns=("www.example.com",),
                      eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    for name, cert in [("root", root), ("interm", interm), ("leaf", leaf)]:
        r.sample(name, cert)

    # Full successful path, constraint source preserved.
    res = run([leaf, interm], [root], "www.example.com")
    r.check("good_chain_valid", True, res.ok)
    r.check("good_path_length", 3, len(res.path or []))
    r.check("good_path_order", [fp(leaf), fp(interm), fp(root)],
            [c.sha256 for c in res.path] if res.ok else None)
    r.check("good_constraint_source", fp(interm),
            res.name_constraints[0].source_sha256 if res.name_constraints else None)

    # Self-signed certificate that is NOT an anchor.
    rogue_k = rsa_key()
    rogue = make_ca("Rogue CA", rogue_k)
    leaf_s = build_cert(subject_cn="C04 LeafS", subject_key=rsa_key(),
                        issuer_cert=rogue, issuer_key=rogue_k, is_ca=False,
                        san_dns=("www.example.com",),
                        eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    res_s = run([leaf_s, rogue], [root], "www.example.com")
    r.check("self_signed_rejected", False, res_s.ok)
    r.check("self_signed_reason", "self_signed_not_anchor",
            first_reasons(res_s)[0] if res_s.failures else None)

    # Missing issuer: nothing in the pool issues this leaf.
    leaf_m = build_cert(subject_cn="C04 LeafM", subject_key=rsa_key(),
                        issuer_cert=None, issuer_key=root_k, is_ca=False,
                        san_dns=("www.example.com",),
                        eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    # issuer name is "C04 LeafM" itself but it is not self-signed; to get a
    # clean missing-issuer case use an issuer name nobody has:
    ghost_name = _cn("Ghost CA")
    b = (x509.CertificateBuilder()
         .subject_name(_cn("C04 LeafM2"))
         .issuer_name(ghost_name)
         .public_key(rsa_key().public_key())
         .serial_number(_next_serial())
         .not_valid_before(VALID_FROM).not_valid_after(VALID_TO)
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.SubjectAlternativeName([x509.DNSName("www.example.com")]),
                        critical=False)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                        critical=False))
    leaf_m2 = b.sign(root_k, hashes.SHA256())
    res_m = run([leaf_m2], [root], "www.example.com")
    r.check("missing_issuer_rejected", False, res_m.ok)
    r.check("missing_issuer_reason", "missing_issuer",
            first_reasons(res_m)[0] if res_m.failures else None)

    # Signature error: issuer name matches the anchor but signed by another key.
    attacker_k = rsa_key()
    b = (x509.CertificateBuilder()
         .subject_name(_cn("C04 LeafBad"))
         .issuer_name(root.subject)
         .public_key(rsa_key().public_key())
         .serial_number(_next_serial())
         .not_valid_before(VALID_FROM).not_valid_after(VALID_TO)
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.SubjectAlternativeName([x509.DNSName("www.example.com")]),
                        critical=False)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                        critical=False))
    leaf_bad = b.sign(attacker_k, hashes.SHA256())
    res_b = run([leaf_bad], [root], "www.example.com")
    r.check("bad_signature_rejected", False, res_b.ok)
    r.check("bad_signature_reason", "signature_invalid",
            first_reasons(res_b)[0] if res_b.failures else None)

    # Candidate constraint failure: intermediate excludes the target name.
    interm_x_k = rsa_key()
    interm_x = build_cert(subject_cn="C04 IntermX", subject_key=interm_x_k,
                          issuer_cert=root, issuer_key=root_k, is_ca=True,
                          nc_excluded=[x509.DNSName("blocked.example.com")])
    leaf_x = build_cert(subject_cn="C04 LeafX", subject_key=rsa_key(),
                        issuer_cert=interm_x, issuer_key=interm_x_k, is_ca=False,
                        san_dns=("www.blocked.example.com",),
                        eku_oids=[ExtendedKeyUsageOID.SERVER_AUTH])
    res_x = run([leaf_x, interm_x], [root], "www.blocked.example.com")
    r.check("constraint_failure_rejected", False, res_x.ok)
    r.check("constraint_failure_reason", "name_constraint_violation",
            first_reasons(res_x)[0] if res_x.failures else None)

    # The four failure classes are represented by distinct reason codes.
    distinct = {
        first_reasons(res_s)[0],
        first_reasons(res_m)[0],
        first_reasons(res_b)[0],
        first_reasons(res_x)[0],
    }
    r.check("failure_reasons_distinct", 4, len(distinct))
    return r


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

CASES = {
    "C01": ("RSA/ECDSA chains: signatures, validity, CA/keyCertSign/pathLen/EKU", case_c01),
    "C02": ("SAN DNS/IP, nameConstraints, unknown critical extensions", case_c02),
    "C03": ("same-subject/cross-signed intermediates, path selection", case_c03),
    "C04": ("self-signed non-anchor, missing issuer, bad signature, constraint failure", case_c04),
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="local verification wrapper")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list available cases")
    group.add_argument("--case", choices=sorted(CASES), help="run one case")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(
            [{"case": cid, "description": desc} for cid, (desc, _) in sorted(CASES.items())],
            indent=2))
        return 0

    desc, fn = CASES[args.case]
    runner = fn()
    record = runner.record()
    record["description"] = desc
    print(json.dumps(record, indent=2))

    if record["assertions"] == 0:
        return 2
    if record["skipped"] == record["assertions"]:
        return 3
    if record["failed"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
