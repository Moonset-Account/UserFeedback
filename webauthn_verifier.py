"""WebAuthn Level 2 ES256 (ES256 / P-256) assertion verification module.

Scope: verification of authentication (login) assertions for already
registered credentials. Registration, attestation and authenticator
extensions are out of scope.

Design notes
------------
* Verification is split into two phases so callers can control scheduling:
    - ``prepare``  : pure, non-mutating structural + cryptographic checks.
    - ``commit``   : under a lock, re-validates the challenge and the
                     signCount against *current* state, then atomically
                     consumes the challenge and updates the stored counter.
  ``verify_assertion`` is the convenience wrapper ``commit(prepare(...))``.
* No state is mutated before every cryptographic check has passed: a
  failed signature/structure check never consumes a challenge and never
  advances the stored signCount.
* Counter policy (service policy): if both stored and received counters
  are zero the assertion is accepted (compatibility with authenticators
  that do not implement a counter). If either is non-zero and the
  received value did not strictly increase, the assertion is rejected
  with a *counter risk* signal. This is a risk signal, not a verdict
  that the device has been cloned.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

# authenticatorData flag bits
UP_FLAG = 0x01  # user present
UV_FLAG = 0x04  # user verified

_MIN_AUTH_DATA_LEN = 37  # rpIdHash(32) || flags(1) || signCount(4)


class VerificationError(Exception):
    """Raised for every verification failure. ``code`` is machine readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RegisteredCredential:
    """A previously registered credential, supplied by the caller."""

    credential_id: bytes
    account: str
    rp_id: str
    public_key: ec.EllipticCurvePublicKey  # P-256
    sign_count: int = 0


@dataclass
class ChallengeRecord:
    """A server-issued challenge bound to an account and a session."""

    challenge: bytes
    account: str
    session_id: str
    expires_at: float  # epoch seconds
    consumed: bool = False


@dataclass
class PendingAssertion:
    """Result of phase 1; carries verified data into the atomic commit."""

    credential: RegisteredCredential
    challenge_record: ChallengeRecord
    new_sign_count: int
    user_verified: bool
    committed: bool = False


@dataclass
class VerificationResult:
    account: str
    credential_id: bytes
    sign_count: int
    user_verified: bool


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


class AssertionVerifier:
    def __init__(
        self,
        *,
        allowed_origins: Set[str],
        require_uv: bool = True,
        now: Optional[Callable[[], float]] = None,
    ):
        self._allowed_origins = set(allowed_origins)
        self._require_uv = require_uv
        self._now = now or time.time
        self._lock = threading.Lock()
        self._credentials: Dict[bytes, RegisteredCredential] = {}
        self._challenges: Dict[bytes, ChallengeRecord] = {}

    # -- caller-supplied state -------------------------------------------

    def register_credential(self, credential: RegisteredCredential) -> None:
        if not isinstance(credential.public_key.curve, ec.SECP256R1):
            raise ValueError("only P-256 (secp256r1) credentials are supported")
        with self._lock:
            self._credentials[credential.credential_id] = credential

    def issue_challenge(
        self,
        account: str,
        session_id: str,
        *,
        ttl: float,
        challenge: Optional[bytes] = None,
    ) -> ChallengeRecord:
        record = ChallengeRecord(
            challenge=challenge if challenge is not None else secrets.token_bytes(32),
            account=account,
            session_id=session_id,
            expires_at=self._now() + ttl,
        )
        with self._lock:
            self._challenges[record.challenge] = record
        return record

    # -- phase 1: pure checks, no mutation --------------------------------

    def prepare(
        self,
        *,
        credential_id: bytes,
        client_data_json: bytes,
        authenticator_data: bytes,
        signature: bytes,
        session_id: str,
    ) -> PendingAssertion:
        # clientDataJSON: parse the raw bytes; the signature later covers
        # exactly these bytes, never a re-serialization.
        try:
            client_data = json.loads(client_data_json.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationError(
                "malformed_client_data", f"clientDataJSON is not valid JSON: {exc}"
            ) from exc
        if not isinstance(client_data, dict):
            raise VerificationError("malformed_client_data", "clientDataJSON must be an object")

        if client_data.get("type") != "webauthn.get":
            raise VerificationError(
                "unexpected_type",
                f"expected type 'webauthn.get', got {client_data.get('type')!r}",
            )

        challenge_b64 = client_data.get("challenge")
        if not isinstance(challenge_b64, str):
            raise VerificationError("malformed_client_data", "missing challenge")
        try:
            challenge = _b64url_decode(challenge_b64)
        except (binascii.Error, ValueError) as exc:
            raise VerificationError(
                "malformed_client_data", f"challenge is not valid base64url: {exc}"
            ) from exc
        record = self._challenges.get(challenge)
        if record is None:
            raise VerificationError("unknown_challenge", "no such outstanding challenge")
        if record.consumed:
            raise VerificationError("challenge_consumed", "challenge was already used")
        if self._now() >= record.expires_at:
            raise VerificationError("challenge_expired", "challenge has expired")
        if record.session_id != session_id:
            raise VerificationError(
                "challenge_session_mismatch", "challenge belongs to a different session"
            )

        credential = self._credentials.get(credential_id)
        if credential is None:
            raise VerificationError("unknown_credential", "credential id is not registered")
        if credential.account != record.account:
            raise VerificationError(
                "credential_account_mismatch",
                "challenge was issued for a different account",
            )

        origin = client_data.get("origin")
        if origin not in self._allowed_origins:
            raise VerificationError(
                "origin_not_allowed", f"origin {origin!r} is not an allowed origin"
            )

        # authenticatorData
        if len(authenticator_data) < _MIN_AUTH_DATA_LEN:
            raise VerificationError(
                "malformed_authenticator_data", "authenticatorData shorter than 37 bytes"
            )
        rp_id_hash = authenticator_data[:32]
        flags = authenticator_data[32]
        sign_count = int.from_bytes(authenticator_data[33:37], "big")

        if rp_id_hash != hashlib.sha256(credential.rp_id.encode("utf-8")).digest():
            raise VerificationError("rp_id_hash_mismatch", "rpIdHash does not match the RP ID")
        if not flags & UP_FLAG:
            raise VerificationError("user_presence_missing", "UP flag is required")
        user_verified = bool(flags & UV_FLAG)
        if self._require_uv and not user_verified:
            raise VerificationError("user_verification_missing", "UV flag is required")

        # Signature over the raw-byte message authenticatorData || SHA-256(clientDataJSON)
        signed_message = authenticator_data + hashlib.sha256(client_data_json).digest()
        try:
            credential.public_key.verify(
                signature, signed_message, ec.ECDSA(hashes.SHA256())
            )
        except InvalidSignature as exc:
            raise VerificationError("invalid_signature", "ES256 signature did not verify") from exc

        return PendingAssertion(
            credential=credential,
            challenge_record=record,
            new_sign_count=sign_count,
            user_verified=user_verified,
        )

    # -- phase 2: atomic commit against current state ---------------------

    def commit(self, pending: PendingAssertion) -> VerificationResult:
        with self._lock:
            if pending.committed:
                raise VerificationError(
                    "pending_already_committed", "this prepared assertion was already committed"
                )
            record = pending.challenge_record
            if record.consumed:
                raise VerificationError("challenge_consumed", "challenge was already used")
            if self._now() >= record.expires_at:
                raise VerificationError("challenge_expired", "challenge has expired")

            credential = pending.credential
            stored = credential.sign_count
            received = pending.new_sign_count
            if stored == 0 and received == 0:
                # Compatibility: authenticator does not implement a counter.
                pass
            elif received > stored:
                pass
            else:
                raise VerificationError(
                    "counter_not_incremented",
                    "signCount did not increase "
                    f"(stored={stored}, received={received}); counter risk: "
                    "possible cloned or malfunctioning authenticator — this is "
                    "a risk signal, not a definitive cloning verdict",
                )

            # Atomic: consume the challenge and update the counter together.
            record.consumed = True
            credential.sign_count = received
            pending.committed = True
            return VerificationResult(
                account=credential.account,
                credential_id=credential.credential_id,
                sign_count=received,
                user_verified=pending.user_verified,
            )

    # -- convenience --------------------------------------------------------

    def verify_assertion(self, **kwargs) -> VerificationResult:
        """Single-shot verification: prepare then atomically commit."""
        return self.commit(self.prepare(**kwargs))
