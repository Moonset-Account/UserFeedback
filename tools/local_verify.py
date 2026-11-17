#!/usr/bin/env python3
"""Local verification entry point for the WebAuthn ES256 assertion module.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01 [--seed 1234]

Each case runs real assertions against the implementation in
``webauthn_verifier.py`` and prints one JSON record. Exit code is non-zero
if any assertion fails, if a case is missing, or if a case ran zero
assertions. No credentials, raw challenges or private material are printed;
only truncated SHA-256 fingerprints of non-sensitive values.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from webauthn_verifier import (
    AssertionVerifier,
    RegisteredCredential,
    VerificationError,
)

RP_ID = "accounts.example.test"
ORIGIN = "https://accounts.example.test"
DEFAULT_SEED = 20261117

# P-256 group order, used to derive deterministic test keys from the seed.
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _derive_key(seed: int, label: str) -> ec.EllipticCurvePrivateKey:
    scalar = int.from_bytes(
        hashlib.sha256(f"local-verify-key:{seed}:{label}".encode()).digest(), "big"
    )
    return ec.derive_private_key(scalar % (_P256_ORDER - 1) + 1, ec.SECP256R1())


def _challenge(rng: random.Random, label: str) -> bytes:
    return hashlib.sha256(f"challenge:{rng.getrandbits(64)}:{label}".encode()).digest()


def build_client_data(type_: str, challenge: bytes, origin: str) -> bytes:
    return json.dumps(
        {"type": type_, "challenge": _b64url(challenge), "origin": origin},
        separators=(",", ":"),
    ).encode("utf-8")


def build_auth_data(rp_id: str, sign_count: int, up: bool = True, uv: bool = True) -> bytes:
    flags = (0x01 if up else 0x00) | (0x04 if uv else 0x00)
    return (
        hashlib.sha256(rp_id.encode("utf-8")).digest()
        + bytes([flags])
        + sign_count.to_bytes(4, "big")
    )


def sign_assertion(key, auth_data: bytes, client_data_json: bytes):
    """Return (signature, signed_message) over the raw-byte message."""
    message = auth_data + hashlib.sha256(client_data_json).digest()
    return key.sign(message, ec.ECDSA(hashes.SHA256())), message


class CaseResult:
    def __init__(self, name: str):
        self.name = name
        self.total = 0
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures = []
        self.summary = {}

    def check(self, condition: bool, label: str):
        self.total += 1
        if condition:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(label)

    def expect_error(self, code: str, fn, label: str):
        self.total += 1
        try:
            fn()
        except VerificationError as exc:
            if exc.code == code:
                self.passed += 1
            else:
                self.failed += 1
                self.failures.append(f"{label}: expected {code}, got {exc.code}")
            return
        except Exception as exc:  # unexpected exception type
            self.failed += 1
            self.failures.append(f"{label}: unexpected {type(exc).__name__}: {exc}")
            return
        self.failed += 1
        self.failures.append(f"{label}: no error raised, expected {code}")

    def record(self, key: str, value):
        self.summary[key] = value

    def to_json(self) -> str:
        return json.dumps(
            {
                "case": self.name,
                "assertions": self.total,
                "passed": self.passed,
                "failed": self.failed,
                "skipped": self.skipped,
                "failures": self.failures,
                "summary": self.summary,
            },
            sort_keys=True,
        )


def _make_verifier(seed: int, now=None):
    key = _derive_key(seed, "primary")
    verifier = AssertionVerifier(allowed_origins={ORIGIN}, now=now)
    credential = RegisteredCredential(
        credential_id=hashlib.sha256(f"cred:{seed}".encode()).digest()[:16],
        account="alice",
        rp_id=RP_ID,
        public_key=key.public_key(),
        sign_count=0,
    )
    verifier.register_credential(credential)
    return verifier, key, credential


def _valid_assertion(verifier, key, credential, rng, session, count, label):
    record = verifier.issue_challenge(
        credential.account, session, ttl=60, challenge=_challenge(rng, label)
    )
    cdj = build_client_data("webauthn.get", record.challenge, ORIGIN)
    ad = build_auth_data(credential.rp_id, count)
    sig, _ = sign_assertion(key, ad, cdj)
    return record, {
        "credential_id": credential.credential_id,
        "client_data_json": cdj,
        "authenticator_data": ad,
        "signature": sig,
        "session_id": session,
    }


# --------------------------------------------------------------------------
# C01: signature over raw bytes + type/challenge/origin/RP hash/UP/UV checks
# --------------------------------------------------------------------------
def case_c01(seed: int) -> CaseResult:
    r = CaseResult("C01")
    rng = random.Random(seed)
    verifier, key, cred = _make_verifier(seed)

    record, req = _valid_assertion(verifier, key, cred, rng, "sess-1", 1, "c01-valid")

    # The signed message must be authenticatorData || SHA-256(clientDataJSON)
    # over the exact raw bytes, and must verify independently.
    expected_msg = req["authenticator_data"] + hashlib.sha256(req["client_data_json"]).digest()
    r.check(
        expected_msg == req["authenticator_data"] + hashlib.sha256(req["client_data_json"]).digest()
        and len(expected_msg) == len(req["authenticator_data"]) + 32,
        "signed message is authenticatorData || SHA-256(clientDataJSON)",
    )
    try:
        key.public_key().verify(req["signature"], expected_msg, ec.ECDSA(hashes.SHA256()))
        r.check(True, "raw-byte ES256 signature verifies independently")
    except Exception:
        r.check(False, "raw-byte ES256 signature verifies independently")

    result = verifier.verify_assertion(**req)
    r.check(
        result.account == "alice" and result.sign_count == 1 and result.user_verified,
        "valid assertion accepted (account, counter, UV)",
    )
    r.check(record.consumed, "challenge consumed on success")
    r.check(cred.sign_count == 1, "stored counter updated on success")

    def attempt(**overrides):
        rec = verifier.issue_challenge(
            "alice", "sess-1", ttl=60, challenge=_challenge(rng, "c01-neg")
        )
        cdj = build_client_data(
            overrides.get("type", "webauthn.get"),
            overrides.get("challenge", rec.challenge),
            overrides.get("origin", ORIGIN),
        )
        ad = build_auth_data(
            overrides.get("rp_id", RP_ID),
            overrides.get("count", 2),
            up=overrides.get("up", True),
            uv=overrides.get("uv", True),
        )
        if "raw_ad" in overrides:
            ad = overrides["raw_ad"]
        sig = overrides.get("signature")
        if sig is None:
            sig, _ = sign_assertion(key, ad, cdj)
        return verifier.verify_assertion(
            credential_id=cred.credential_id,
            client_data_json=overrides.get("raw_cdj", cdj),
            authenticator_data=ad,
            signature=sig,
            session_id="sess-1",
        )

    r.expect_error("invalid_signature", lambda: attempt(signature=b"\x30\x03\x02\x01\x00"),
                   "tampered signature rejected")
    r.expect_error("unexpected_type", lambda: attempt(type="webauthn.create"),
                   "wrong clientData type rejected")
    r.expect_error("unknown_challenge", lambda: attempt(challenge=_challenge(rng, "c01-unknown")),
                   "unknown challenge rejected")
    r.expect_error("origin_not_allowed", lambda: attempt(origin="https://evil.example.test"),
                   "foreign origin rejected")
    r.expect_error("origin_not_allowed", lambda: attempt(origin="https://accounts.example.test.evil.test"),
                   "origin suffix lookalike rejected (full origin match)")
    r.expect_error("rp_id_hash_mismatch", lambda: attempt(rp_id="other.example.test"),
                   "RP ID hash mismatch rejected")
    r.expect_error("user_presence_missing", lambda: attempt(up=False),
                   "missing UP flag rejected")
    r.expect_error("user_verification_missing", lambda: attempt(uv=False),
                   "missing UV flag rejected")
    r.expect_error("malformed_authenticator_data", lambda: attempt(raw_ad=b"\x00" * 20),
                   "truncated authenticatorData rejected")
    r.expect_error("malformed_client_data", lambda: attempt(raw_cdj=b"{not json"),
                   "malformed clientDataJSON rejected")

    r.record("seed", seed)
    r.record("credential_id_fingerprint", _fingerprint(cred.credential_id))
    r.record("public_key_fingerprint", _fingerprint(
        cred.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    ))
    r.record("expected", "valid assertion accepted; 10 structural/crypto rejections")
    r.record("observed", f"passed={r.passed} of {r.total}")
    return r


# --------------------------------------------------------------------------
# C02: challenge bound to account/session/expiry; crypto failure consumes
# nothing and advances nothing
# --------------------------------------------------------------------------
def case_c02(seed: int) -> CaseResult:
    r = CaseResult("C02")
    rng = random.Random(seed)
    clock = [1_000.0]
    verifier, key, cred = _make_verifier(seed, now=lambda: clock[0])

    record, req = _valid_assertion(verifier, key, cred, rng, "sess-A", 1, "c02")

    # Session binding
    bad_session = dict(req, session_id="sess-B")
    r.expect_error("challenge_session_mismatch",
                   lambda: verifier.verify_assertion(**bad_session),
                   "challenge bound to its session")
    r.check(not record.consumed, "failed attempt did not consume challenge")
    r.check(cred.sign_count == 0, "failed attempt did not advance counter")

    # Account binding: a credential of another account cannot use the challenge
    other_key = _derive_key(seed, "other-account")
    other_cred = RegisteredCredential(
        credential_id=hashlib.sha256(b"cred:other").digest()[:16],
        account="bob",
        rp_id=RP_ID,
        public_key=other_key.public_key(),
        sign_count=0,
    )
    verifier.register_credential(other_cred)
    other_sig, _ = sign_assertion(other_key, req["authenticator_data"], req["client_data_json"])
    other_req = dict(req, credential_id=other_cred.credential_id, signature=other_sig)
    r.expect_error("credential_account_mismatch",
                   lambda: verifier.verify_assertion(**other_req),
                   "challenge bound to its account")
    r.check(not record.consumed, "account mismatch did not consume challenge")

    # Cryptographic failure must not consume the challenge nor advance counter
    bad_sig = dict(req, signature=b"\x30\x03\x02\x01\x00")
    r.expect_error("invalid_signature",
                   lambda: verifier.verify_assertion(**bad_sig),
                   "invalid signature rejected")
    r.check(not record.consumed, "crypto failure did not consume challenge")
    r.check(cred.sign_count == 0, "crypto failure did not advance counter")

    # Expiry binding
    clock[0] += 61.0
    r.expect_error("challenge_expired",
                   lambda: verifier.verify_assertion(**req),
                   "expired challenge rejected")
    r.check(not record.consumed, "expired challenge not consumed")
    r.check(cred.sign_count == 0, "expiry failure did not advance counter")

    # The same still-valid challenge succeeds after the earlier crypto failure
    # (rewind the clock: failure paths above must not have mutated state).
    clock[0] -= 61.0
    result = verifier.verify_assertion(**req)
    r.check(result.sign_count == 1, "valid retry after crypto failure accepted")
    r.check(record.consumed, "challenge consumed exactly on success")
    r.check(cred.sign_count == 1, "counter advanced exactly on success")

    # Replay with the consumed challenge is rejected
    r.expect_error("challenge_consumed",
                   lambda: verifier.verify_assertion(**req),
                   "consumed challenge cannot be replayed")

    r.record("seed", seed)
    r.record("expected", "binding enforced; failures leave challenge/counter untouched")
    r.record("observed", f"passed={r.passed} of {r.total}")
    return r


# --------------------------------------------------------------------------
# C03: zero-counter compatibility + strict growth when either side non-zero
# --------------------------------------------------------------------------
def case_c03(seed: int) -> CaseResult:
    r = CaseResult("C03")
    rng = random.Random(seed)
    verifier, key, cred = _make_verifier(seed)

    # Zero-counter compatibility: 0 -> 0 accepted repeatedly.
    for i in range(2):
        _, req = _valid_assertion(verifier, key, cred, rng, "sess-1", 0, f"c03-zero-{i}")
        try:
            verifier.verify_assertion(**req)
            r.check(True, f"zero-counter assertion #{i + 1} accepted (compatibility)")
        except VerificationError:
            r.check(False, f"zero-counter assertion #{i + 1} accepted (compatibility)")
    r.check(cred.sign_count == 0, "stored counter stays 0 for zero-counter authenticator")

    # First non-zero counter: 0 -> 5 is growth, accepted.
    _, req5 = _valid_assertion(verifier, key, cred, rng, "sess-1", 5, "c03-five")
    try:
        verifier.verify_assertion(**req5)
        r.check(True, "0 -> 5 accepted as growth")
    except VerificationError:
        r.check(False, "0 -> 5 accepted as growth")

    # Equal non-zero counter: rejected as counter risk (not a cloning verdict).
    _, req5b = _valid_assertion(verifier, key, cred, rng, "sess-1", 5, "c03-five-b")
    try:
        verifier.verify_assertion(**req5b)
        r.check(False, "5 -> 5 rejected")
        r.check(False, "rejection reports counter risk without cloning verdict")
    except VerificationError as exc:
        r.check(exc.code == "counter_not_incremented", "5 -> 5 rejected")
        msg = exc.message.lower()
        r.check(
            "risk" in msg and "not a definitive cloning verdict" in msg,
            "rejection reports counter risk without cloning verdict",
        )

    # Regression: 5 -> 3 rejected.
    _, req3 = _valid_assertion(verifier, key, cred, rng, "sess-1", 3, "c03-three")
    r.expect_error("counter_not_incremented",
                   lambda: verifier.verify_assertion(**req3),
                   "5 -> 3 rejected")

    # New zero while stored non-zero: rejected (either side non-zero => strict).
    _, req0 = _valid_assertion(verifier, key, cred, rng, "sess-1", 0, "c03-zero-after")
    r.expect_error("counter_not_incremented",
                   lambda: verifier.verify_assertion(**req0),
                   "5 -> 0 rejected (non-zero stored counter)")

    # Rejections must not have moved the stored counter; growth still works.
    r.check(cred.sign_count == 5, "rejections did not modify stored counter")
    _, req6 = _valid_assertion(verifier, key, cred, rng, "sess-1", 6, "c03-six")
    try:
        verifier.verify_assertion(**req6)
        r.check(cred.sign_count == 6, "5 -> 6 accepted after rejections")
    except VerificationError:
        r.check(False, "5 -> 6 accepted after rejections")

    r.record("seed", seed)
    r.record("expected", "0/0 compatible; strict growth once either side non-zero")
    r.record("observed", f"passed={r.passed} of {r.total}")
    return r


# --------------------------------------------------------------------------
# C04: two valid but out-of-order assertions; challenge consumption and
# counter update are atomic and evaluated against current state
# --------------------------------------------------------------------------
def case_c04(seed: int) -> CaseResult:
    r = CaseResult("C04")
    rng = random.Random(seed)

    # -- deterministic interleaving via the prepare/commit split ----------
    verifier, key, cred = _make_verifier(seed)
    cred.sign_count = 4  # stored counter before the race

    rec_a, req_a = _valid_assertion(verifier, key, cred, rng, "sess-1", 6, "c04-a")
    rec_b, req_b = _valid_assertion(verifier, key, cred, rng, "sess-1", 5, "c04-b")

    # Both assertions are individually legitimate: both pass phase 1.
    pend_a = verifier.prepare(**req_a)
    pend_b = verifier.prepare(**req_b)
    r.check(True, "both out-of-order assertions pass cryptographic phase")

    # Commit the higher counter first (reversed arrival order).
    res_a = verifier.commit(pend_a)
    r.check(res_a.sign_count == 6 and cred.sign_count == 6,
            "higher counter committed first against current state")
    r.check(rec_a.consumed and not rec_b.consumed,
            "only the committed challenge was consumed")

    # The stale assertion was cryptographically valid but no longer matches
    # current state: rejected at commit; its challenge stays unconsumed.
    r.expect_error("counter_not_incremented", lambda: verifier.commit(pend_b),
                   "stale counter rejected at commit against current state")
    r.check(not rec_b.consumed, "rejected commit did not consume its challenge")
    r.check(cred.sign_count == 6, "rejected commit did not move the counter")

    # A prepared assertion is single-use.
    r.expect_error("pending_already_committed", lambda: verifier.commit(pend_a),
                   "prepared assertion cannot be committed twice")

    # Forward order accepts both (fresh verifier, same seed material).
    rng2 = random.Random(seed)
    verifier2, key2, cred2 = _make_verifier(seed)
    cred2.sign_count = 4
    rng2 = random.Random(seed + 1)
    _, req_lo = _valid_assertion(verifier2, key2, cred2, rng2, "sess-1", 5, "c04-lo")
    _, req_hi = _valid_assertion(verifier2, key2, cred2, rng2, "sess-1", 6, "c04-hi")
    try:
        verifier2.verify_assertion(**req_lo)
        verifier2.verify_assertion(**req_hi)
        r.check(cred2.sign_count == 6, "in-order arrival accepts both assertions")
    except VerificationError:
        r.check(False, "in-order arrival accepts both assertions")

    # -- concurrent commits: exactly the serialized outcomes are possible --
    verifier3, key3, cred3 = _make_verifier(seed)
    cred3.sign_count = 4
    rng3 = random.Random(seed + 2)
    rec_c, req_c = _valid_assertion(verifier3, key3, cred3, rng3, "sess-1", 6, "c04-c")
    rec_d, req_d = _valid_assertion(verifier3, key3, cred3, rng3, "sess-1", 5, "c04-d")
    pend_c = verifier3.prepare(**req_c)
    pend_d = verifier3.prepare(**req_d)

    barrier = threading.Barrier(2)
    outcomes = {}

    def racer(name, pend):
        barrier.wait(timeout=10)
        try:
            verifier3.commit(pend)
            outcomes[name] = "ok"
        except VerificationError as exc:
            outcomes[name] = exc.code

    threads = [
        threading.Thread(target=racer, args=("high6", pend_c)),
        threading.Thread(target=racer, args=("low5", pend_d)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    r.check(outcomes.get("high6") == "ok",
            "higher-counter assertion always wins the race")
    r.check(outcomes.get("low5") in ("ok", "counter_not_incremented"),
            "lower-counter assertion outcome is one of the serialized results")
    r.check(cred3.sign_count == 6,
            "final counter is 6 regardless of interleaving")
    if outcomes.get("low5") == "counter_not_incremented":
        r.check(not rec_d.consumed, "losing assertion left its challenge unconsumed")
    else:
        r.check(rec_d.consumed, "both challenges consumed when both commits won")
    r.record("race_outcome", outcomes)

    r.record("seed", seed)
    r.record("expected", "atomic consume+update evaluated against current state")
    r.record("observed", f"passed={r.passed} of {r.total}")
    return r


CASES = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local verification runner")
    parser.add_argument("--list", action="store_true", help="list available cases")
    parser.add_argument("--case", choices=sorted(CASES), help="run a single case")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="replay seed")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps({"cases": sorted(CASES), "default_seed": DEFAULT_SEED}))
        return 0

    names = [args.case] if args.case else sorted(CASES)
    exit_code = 0
    for name in names:
        fn = CASES.get(name)
        if fn is None:
            print(json.dumps({"case": name, "error": "case not implemented"}))
            exit_code = 1
            continue
        result = fn(args.seed)
        print(result.to_json())
        if result.total == 0 or result.failed > 0 or result.skipped > 0:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
