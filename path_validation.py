"""X.509 certificate path validation for an internal TLS client.

Scope
-----
* Input: a set of PEM encoded certificates, an explicit list of trust
  anchors, a validation timestamp and a target DNS name or IP address.
* Only RSA and ECDSA keys are handled; anything else makes the affected
  candidate path fail with ``unsupported_key_algorithm``.
* Signature verification and certificate parsing are delegated to the
  ``cryptography`` package.

Properties
----------
* Intermediates may share a subject name with different keys, or be
  cross-signed.  Candidates are never merged by subject name; every
  issuer candidate is tried and the chain search continues until a
  fully valid path is found or all candidates are exhausted.
* A self-signed certificate is *not* automatically a trust anchor.  It
  only terminates a chain when it is byte-identical to an explicitly
  configured anchor.
* "Missing issuer" and "signature invalid" are reported as distinct
  reasons.
* An unknown *critical* extension does not invalidate the whole
  validation; it marks the affected candidate path as *not applicable*
  and other chains are still considered.
* Name constraints (DNS and IP) are accumulated along the path; the
  certificate that introduced each constraint is recorded as its
  source.  A path that violates a name constraint is rejected but the
  search continues with the remaining candidates.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Sequence, Union

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import ExtensionOID, ExtendedKeyUsageOID

__all__ = [
    "Reason",
    "Category",
    "Target",
    "CertInfo",
    "NameConstraintRecord",
    "CandidateFailure",
    "ValidationResult",
    "PathValidator",
    "validate",
    "load_pem_certificates",
    "parse_target",
]


class Reason(str, Enum):
    """Machine readable failure / non-applicability reasons."""

    NO_LEAF_CANDIDATE = "no_leaf_candidate"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    NOT_CA = "not_ca"
    MISSING_KEY_CERT_SIGN = "missing_key_cert_sign"
    PATH_LEN_EXCEEDED = "path_len_exceeded"
    MISSING_SERVER_EKU = "missing_server_eku"
    NAME_MISMATCH = "name_mismatch"
    NAME_CONSTRAINT_VIOLATION = "name_constraint_violation"
    UNKNOWN_CRITICAL_EXTENSION = "unknown_critical_extension"
    UNSUPPORTED_KEY_ALGORITHM = "unsupported_key_algorithm"
    MISSING_ISSUER = "missing_issuer"
    SIGNATURE_INVALID = "signature_invalid"
    SELF_SIGNED_NOT_ANCHOR = "self_signed_not_anchor"


class Category(str, Enum):
    """Whether a failed candidate is invalid or simply not applicable."""

    INVALID = "invalid"
    NOT_APPLICABLE = "not_applicable"


# Critical extensions this implementation understands and enforces.  Any
# other critical extension makes the candidate path not applicable.
_RECOGNIZED_CRITICAL_OIDS = frozenset(
    {
        ExtensionOID.BASIC_CONSTRAINTS,
        ExtensionOID.KEY_USAGE,
        ExtensionOID.EXTENDED_KEY_USAGE,
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME,
        ExtensionOID.NAME_CONSTRAINTS,
        ExtensionOID.SUBJECT_KEY_IDENTIFIER,
        ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
    }
)


# ---------------------------------------------------------------------------
# Target parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """The DNS name or IP address the leaf certificate must match."""

    is_ip: bool
    value: str  # normalised textual form
    ip: Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]] = None


def parse_target(name: str) -> Target:
    text = name.strip()
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return Target(is_ip=False, value=text.rstrip(".").lower())
    return Target(is_ip=True, value=str(ip), ip=ip)


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------


@dataclass
class CertInfo:
    subject: str
    issuer: str
    serial: int
    not_before: str
    not_after: str
    sha256: str
    is_anchor: bool


@dataclass
class NameConstraintRecord:
    """Name constraints introduced by one CA certificate in the path."""

    source_sha256: str
    source_subject: str
    permitted_dns: list[str] = field(default_factory=list)
    excluded_dns: list[str] = field(default_factory=list)
    permitted_ip: list[str] = field(default_factory=list)
    excluded_ip: list[str] = field(default_factory=list)


@dataclass
class CandidateFailure:
    """First provable reason one candidate chain failed."""

    chain: list[str]  # sha256 fingerprints of the partial chain tried
    reason: str  # Reason value
    category: str  # Category value
    detail: str
    cert_sha256: Optional[str] = None


@dataclass
class ValidationResult:
    ok: bool
    path: Optional[list[CertInfo]] = None  # leaf first, anchor last
    name_constraints: list[NameConstraintRecord] = field(default_factory=list)
    path_len_limits: list[dict] = field(default_factory=list)
    failures: list[CandidateFailure] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Low level helpers
# ---------------------------------------------------------------------------


def _fp(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def _supported_key(cert: x509.Certificate) -> bool:
    return isinstance(cert.public_key(), (rsa.RSAPublicKey, ec.EllipticCurvePublicKey))


def _verify_signature(child: x509.Certificate, issuer: x509.Certificate) -> bool:
    """Verify ``child``'s signature with ``issuer``'s public key."""
    pub = issuer.public_key()
    try:
        if isinstance(pub, rsa.RSAPublicKey):
            pub.verify(
                child.signature,
                child.tbs_certificate_bytes,
                padding.PKCS1v15(),
                child.signature_hash_algorithm,
            )
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(
                child.signature,
                child.tbs_certificate_bytes,
                ec.ECDSA(child.signature_hash_algorithm),
            )
        else:
            return False
    except InvalidSignature:
        return False
    return True


def _unknown_critical_oids(cert: x509.Certificate) -> list[str]:
    out = []
    for ext in cert.extensions:
        if ext.critical and ext.oid not in _RECOGNIZED_CRITICAL_OIDS:
            out.append(ext.oid.dotted_string)
    return out


def _basic_constraints(cert: x509.Certificate) -> Optional[x509.BasicConstraints]:
    try:
        return cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        return None


def _has_key_cert_sign(cert: x509.Certificate) -> bool:
    try:
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        # No keyUsage extension on a CA: nothing restricts keyCertSign.
        return True
    return bool(ku.key_cert_sign)


def _is_self_issued(cert: x509.Certificate) -> bool:
    return cert.issuer == cert.subject


def _is_self_signed(cert: x509.Certificate) -> bool:
    return _is_self_issued(cert) and _verify_signature(cert, cert)


# --- name matching ---------------------------------------------------------


def _dns_pattern_match(pattern: str, name: str) -> bool:
    """Match a SAN dNSName (may contain one left-most wildcard)."""
    p = pattern.lower().rstrip(".")
    n = name.lower().rstrip(".")
    if p.startswith("*."):
        suffix = p[1:]
        if not n.endswith(suffix):
            return False
        head = n[: -len(suffix)]
        return head != "" and "." not in head
    return p == n


def _dns_constraint_match(constraint: str, name: str) -> bool:
    """RFC 5280 DNS name constraint matching."""
    c = constraint.lower().rstrip(".")
    n = name.lower().rstrip(".")
    if c.startswith("."):
        # sub-domains only
        return n.endswith(c) and n != c.lstrip(".")
    return n == c or n.endswith("." + c)


def _ip_in(constraint, ip) -> bool:
    network = constraint if isinstance(constraint, (ipaddress.IPv4Network, ipaddress.IPv6Network)) else ipaddress.ip_network(constraint)
    return ip in network


# ---------------------------------------------------------------------------
# Name constraint state accumulated along a candidate path
# ---------------------------------------------------------------------------


@dataclass
class _NCState:
    permitted_dns: list = field(default_factory=list)  # (source_fp, str)
    excluded_dns: list = field(default_factory=list)
    permitted_ip: list = field(default_factory=list)  # (source_fp, network)
    excluded_ip: list = field(default_factory=list)

    def extended_with(self, cert: x509.Certificate) -> "_NCState":
        try:
            nc = cert.extensions.get_extension_for_class(x509.NameConstraints).value
        except x509.ExtensionNotFound:
            return self
        src = _fp(cert)
        new = _NCState(
            permitted_dns=list(self.permitted_dns),
            excluded_dns=list(self.excluded_dns),
            permitted_ip=list(self.permitted_ip),
            excluded_ip=list(self.excluded_ip),
        )
        for subtree in nc.permitted_subtrees or []:
            if isinstance(subtree, x509.DNSName):
                new.permitted_dns.append((src, subtree.value))
            elif isinstance(subtree, x509.IPAddress):
                new.permitted_ip.append((src, subtree.value))
        for subtree in nc.excluded_subtrees or []:
            if isinstance(subtree, x509.DNSName):
                new.excluded_dns.append((src, subtree.value))
            elif isinstance(subtree, x509.IPAddress):
                new.excluded_ip.append((src, subtree.value))
        return new

    def check(self, dns_names: Sequence[str], ips: Sequence) -> Optional[str]:
        """Return a violation description or None."""
        for name in dns_names:
            for src, c in self.excluded_dns:
                if _dns_constraint_match(c, name):
                    return f"DNS name {name!r} excluded by constraint {c!r} from {src}"
            if self.permitted_dns and not any(
                _dns_constraint_match(c, name) for _, c in self.permitted_dns
            ):
                return f"DNS name {name!r} not within permitted subtrees"
        for ip in ips:
            for src, c in self.excluded_ip:
                if _ip_in(c, ip):
                    return f"IP {ip} excluded by constraint {c} from {src}"
            if self.permitted_ip and not any(_ip_in(c, ip) for _, c in self.permitted_ip):
                return f"IP {ip} not within permitted subtrees"
        return None

    def records(self, certs_by_fp: dict) -> list[NameConstraintRecord]:
        grouped: dict[str, NameConstraintRecord] = {}
        for src, value in self.permitted_dns:
            rec = grouped.setdefault(src, _new_record(src, certs_by_fp))
            rec.permitted_dns.append(value)
        for src, value in self.excluded_dns:
            rec = grouped.setdefault(src, _new_record(src, certs_by_fp))
            rec.excluded_dns.append(value)
        for src, value in self.permitted_ip:
            rec = grouped.setdefault(src, _new_record(src, certs_by_fp))
            rec.permitted_ip.append(str(value))
        for src, value in self.excluded_ip:
            rec = grouped.setdefault(src, _new_record(src, certs_by_fp))
            rec.excluded_ip.append(str(value))
        return list(grouped.values())


def _new_record(src: str, certs_by_fp: dict) -> NameConstraintRecord:
    cert = certs_by_fp.get(src)
    subject = cert.subject.rfc4514_string() if cert is not None else ""
    return NameConstraintRecord(source_sha256=src, source_subject=subject)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def load_pem_certificates(data: Union[bytes, str]) -> list[x509.Certificate]:
    if isinstance(data, str):
        data = data.encode("ascii")
    return list(x509.load_pem_x509_certificates(data))


class PathValidator:
    """Build and validate certificate paths for one target."""

    def __init__(
        self,
        pool: Sequence[x509.Certificate],
        anchors: Sequence[x509.Certificate],
        validation_time: datetime,
        target: Union[str, Target],
    ) -> None:
        if validation_time.tzinfo is None:
            validation_time = validation_time.replace(tzinfo=timezone.utc)
        self.time = validation_time
        self.target = parse_target(target) if isinstance(target, str) else target
        self.anchors = list(anchors)
        self.anchor_fps = {_fp(a) for a in self.anchors}
        # De-duplicate by fingerprint; never merge distinct certificates
        # that merely share a subject name.
        seen: dict[str, x509.Certificate] = {}
        for cert in list(pool) + list(anchors):
            seen.setdefault(_fp(cert), cert)
        self._certs = list(seen.values())
        self._by_fp = seen
        self._leaf_dns: list[str] = []
        self._leaf_ips: list = []

    # -- public -------------------------------------------------------------

    def validate(self) -> ValidationResult:
        failures: list[CandidateFailure] = []
        leaves = sorted(self._leaf_candidates(), key=_fp)
        if not leaves:
            failures.append(
                CandidateFailure(
                    chain=[],
                    reason=Reason.NO_LEAF_CANDIDATE.value,
                    category=Category.INVALID.value,
                    detail="certificate set contains no end-entity candidate",
                )
            )
            return ValidationResult(ok=False, failures=failures)
        for leaf in leaves:
            result = self._validate_leaf(leaf, failures)
            if result is not None:
                result.failures = failures
                return result
        return ValidationResult(ok=False, failures=failures)

    # -- leaf ---------------------------------------------------------------

    def _leaf_candidates(self) -> list[x509.Certificate]:
        out = []
        for cert in self._certs:
            if _fp(cert) in self.anchor_fps:
                continue
            bc = _basic_constraints(cert)
            if bc is None or not bc.ca:
                out.append(cert)
        return out

    def _validate_leaf(
        self, leaf: x509.Certificate, failures: list[CandidateFailure]
    ) -> Optional[ValidationResult]:
        leaf_fp = _fp(leaf)

        def fail(reason: Reason, category: Category, detail: str) -> None:
            failures.append(
                CandidateFailure(
                    chain=[leaf_fp],
                    reason=reason.value,
                    category=category.value,
                    detail=detail,
                    cert_sha256=leaf_fp,
                )
            )

        unknown = _unknown_critical_oids(leaf)
        if unknown:
            fail(
                Reason.UNKNOWN_CRITICAL_EXTENSION,
                Category.NOT_APPLICABLE,
                f"unrecognised critical extensions: {', '.join(unknown)}",
            )
            return None
        if not _supported_key(leaf):
            fail(
                Reason.UNSUPPORTED_KEY_ALGORITHM,
                Category.NOT_APPLICABLE,
                "leaf public key is not RSA or ECDSA",
            )
            return None
        reason = self._check_validity(leaf)
        if reason is not None:
            fail(reason, Category.INVALID, "leaf outside its validity window")
            return None
        if not self._leaf_eku_ok(leaf):
            fail(
                Reason.MISSING_SERVER_EKU,
                Category.INVALID,
                "leaf EKU does not include id-kp-serverAuth",
            )
            return None
        ok, detail = self._leaf_name_ok(leaf)
        if not ok:
            fail(Reason.NAME_MISMATCH, Category.INVALID, detail)
            return None
        return self._extend([leaf], _NCState(), [], failures)

    def _leaf_eku_ok(self, leaf: x509.Certificate) -> bool:
        try:
            eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except x509.ExtensionNotFound:
            return True  # no EKU: any usage permitted
        return (
            ExtendedKeyUsageOID.SERVER_AUTH in eku
            or ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE in eku
        )

    def _leaf_name_ok(self, leaf: x509.Certificate) -> tuple[bool, str]:
        self._leaf_dns = []
        self._leaf_ips = []
        try:
            san = leaf.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
        except x509.ExtensionNotFound:
            return False, "leaf has no subjectAltName extension"
        self._leaf_dns = list(san.get_values_for_type(x509.DNSName))
        self._leaf_ips = list(san.get_values_for_type(x509.IPAddress))
        if self.target.is_ip:
            if self.target.ip in self._leaf_ips:
                return True, ""
            return False, f"target IP {self.target.value} not in leaf SAN"
        for pattern in self._leaf_dns:
            if _dns_pattern_match(pattern, self.target.value):
                return True, ""
        return False, f"target DNS name {self.target.value!r} not matched by leaf SAN"

    # -- chain construction -------------------------------------------------

    def _check_validity(self, cert: x509.Certificate) -> Optional[Reason]:
        if self.time < cert.not_valid_before_utc:
            return Reason.NOT_YET_VALID
        if self.time > cert.not_valid_after_utc:
            return Reason.EXPIRED
        return None

    def _extend(
        self,
        chain: list[x509.Certificate],
        nc_state: _NCState,
        path_len_limits: list[dict],
        failures: list[CandidateFailure],
    ) -> Optional[ValidationResult]:
        current = chain[-1]
        current_fp = _fp(current)
        chain_fps = [_fp(c) for c in chain]

        if current_fp in self.anchor_fps:
            return self._success(chain, nc_state, path_len_limits)

        visited = set(chain_fps)
        candidates = [
            c
            for c in self._certs
            if _fp(c) not in visited and c.subject == current.issuer
        ]

        if _is_self_issued(current):
            # A self-signed certificate only terminates a chain when it is
            # an explicit trust anchor (handled above).  Otherwise record
            # the provable fact and keep trying other candidates.
            if _is_self_signed(current):
                failures.append(
                    CandidateFailure(
                        chain=chain_fps,
                        reason=Reason.SELF_SIGNED_NOT_ANCHOR.value,
                        category=Category.INVALID.value,
                        detail=(
                            "self-signed certificate is not one of the "
                            "configured trust anchors"
                        ),
                        cert_sha256=current_fp,
                    )
                )
                if not candidates:
                    return None
            # self-issued but not self-signed: fall through, the real
            # issuer may be another certificate with the same subject.

        if not candidates:
            failures.append(
                CandidateFailure(
                    chain=chain_fps,
                    reason=Reason.MISSING_ISSUER.value,
                    category=Category.INVALID.value,
                    detail=f"no certificate found for issuer {current.issuer.rfc4514_string()!r}",
                    cert_sha256=current_fp,
                )
            )
            return None

        for cand in sorted(candidates, key=_fp):
            cand_fp = _fp(cand)
            tried = chain_fps + [cand_fp]

            unknown = _unknown_critical_oids(cand)
            if unknown:
                failures.append(
                    CandidateFailure(
                        chain=tried,
                        reason=Reason.UNKNOWN_CRITICAL_EXTENSION.value,
                        category=Category.NOT_APPLICABLE.value,
                        detail=f"unrecognised critical extensions: {', '.join(unknown)}",
                        cert_sha256=cand_fp,
                    )
                )
                continue
            if not _supported_key(cand):
                failures.append(
                    CandidateFailure(
                        chain=tried,
                        reason=Reason.UNSUPPORTED_KEY_ALGORITHM.value,
                        category=Category.NOT_APPLICABLE.value,
                        detail="issuer candidate public key is not RSA or ECDSA",
                        cert_sha256=cand_fp,
                    )
                )
                continue
            if not _verify_signature(current, cand):
                failures.append(
                    CandidateFailure(
                        chain=tried,
                        reason=Reason.SIGNATURE_INVALID.value,
                        category=Category.INVALID.value,
                        detail="signature over child certificate does not verify",
                        cert_sha256=cand_fp,
                    )
                )
                continue
            reason = self._check_validity(cand)
            if reason is not None:
                failures.append(
                    CandidateFailure(
                        chain=tried,
                        reason=reason.value,
                        category=Category.INVALID.value,
                        detail="issuer candidate outside its validity window",
                        cert_sha256=cand_fp,
                    )
                )
                continue

            if cand_fp in self.anchor_fps:
                # Explicitly trusted: signature and validity checked, the
                # anchor's own extensions are not re-validated.
                return self._success(chain + [cand], nc_state, path_len_limits)

            bc = _basic_constraints(cand)
            if bc is None or not bc.ca:
                failures.append(
                    CandidateFailure(
                        chain=tried,
                        reason=Reason.NOT_CA.value,
                        category=Category.INVALID.value,
                        detail="issuer candidate is not a CA (basicConstraints)",
                        cert_sha256=cand_fp,
                    )
                )
                continue
            if not _has_key_cert_sign(cand):
                failures.append(
                    CandidateFailure(
                        chain=tried,
                        reason=Reason.MISSING_KEY_CERT_SIGN.value,
                        category=Category.INVALID.value,
                        detail="issuer candidate keyUsage lacks keyCertSign",
                        cert_sha256=cand_fp,
                    )
                )
                continue
            ca_below = len(chain) - 1  # non-leaf CA certs below the candidate
            if bc.path_length is not None and ca_below > bc.path_length:
                failures.append(
                    CandidateFailure(
                        chain=tried,
                        reason=Reason.PATH_LEN_EXCEEDED.value,
                        category=Category.INVALID.value,
                        detail=(
                            f"pathLenConstraint {bc.path_length} exceeded: "
                            f"{ca_below} CA certificate(s) below"
                        ),
                        cert_sha256=cand_fp,
                    )
                )
                continue

            new_state = nc_state.extended_with(cand)
            violation = new_state.check(self._leaf_dns, self._leaf_ips)
            if violation is None and not self.target.is_ip:
                violation = new_state.check([self.target.value], [])
            if violation is None and self.target.is_ip:
                violation = new_state.check([], [self.target.ip])
            if violation is not None:
                failures.append(
                    CandidateFailure(
                        chain=tried,
                        reason=Reason.NAME_CONSTRAINT_VIOLATION.value,
                        category=Category.INVALID.value,
                        detail=violation,
                        cert_sha256=cand_fp,
                    )
                )
                continue

            new_limits = list(path_len_limits)
            if bc.path_length is not None:
                new_limits.append(
                    {
                        "source_sha256": cand_fp,
                        "source_subject": cand.subject.rfc4514_string(),
                        "path_len_constraint": bc.path_length,
                    }
                )
            result = self._extend(chain + [cand], new_state, new_limits, failures)
            if result is not None:
                return result
        return None

    # -- success ------------------------------------------------------------

    def _success(
        self,
        chain: list[x509.Certificate],
        nc_state: _NCState,
        path_len_limits: list[dict],
    ) -> ValidationResult:
        path = [
            CertInfo(
                subject=c.subject.rfc4514_string(),
                issuer=c.issuer.rfc4514_string(),
                serial=c.serial_number,
                not_before=c.not_valid_before_utc.isoformat(),
                not_after=c.not_valid_after_utc.isoformat(),
                sha256=_fp(c),
                is_anchor=_fp(c) in self.anchor_fps,
            )
            for c in chain
        ]
        return ValidationResult(
            ok=True,
            path=path,
            name_constraints=nc_state.records(self._by_fp),
            path_len_limits=path_len_limits,
        )


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


def validate(
    pool: Sequence[Union[x509.Certificate, bytes, str]],
    anchors: Sequence[Union[x509.Certificate, bytes, str]],
    validation_time: datetime,
    target: str,
) -> ValidationResult:
    """Validate a path to ``target`` from PEM blobs or Certificate objects."""

    def _coerce(items):
        out = []
        for item in items:
            if isinstance(item, x509.Certificate):
                out.append(item)
            else:
                out.extend(load_pem_certificates(item))
        return out

    return PathValidator(
        pool=_coerce(pool),
        anchors=_coerce(anchors),
        validation_time=validation_time,
        target=target,
    ).validate()
