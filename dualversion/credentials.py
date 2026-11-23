"""Credential codec for the legacy view.

A legacy item is addressed by ``name``, which is mutable, so names cannot be
identity.  Each legacy item therefore also carries an *edit credential*: an
opaque token that round-trips to the canonical backend id plus the versions of
the two legacy-visible fields (``name`` and ``address``).

The token must not expose the canonical id in readable form (the legacy version
only sees names and addresses), so the payload is encrypted, not merely
encoded.  The construction is deterministic *encrypt-then-MAC*:

* ``tag  = HMAC(secret, "kid" || backend_id)[:8]`` -- a keyed identifier used to
  derive the keystream, so the same backend always gets the same token and the
  plaintext id never appears;
* ``ciphertext = plaintext XOR keystream(secret, tag)`` -- truncated SHA-256
  counter mode;
* ``mac = HMAC(secret, "mac" || tag || ciphertext)[:16]`` -- verified before the
  ciphertext is decrypted, in constant time.

Binding identity *and* the observed field versions into the token is what makes
all of the following work:

* renaming a backend does not change its identity;
* swapping two names cannot move canonical (weight/drain) attributes between
  ids;
* a legacy write can detect that a legacy-visible field changed underneath it
  while still merging edits to fields it is not touching;
* two legacy writers that started from the same read cannot both succeed.
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

from .errors import ValidationError

_PREFIX = "lv1"
_TAG_BYTES = 8
_MAC_BYTES = 16
_MIN_SECRET_BYTES = 16
_MAX_TOKEN_LENGTH = 4096
_MAX_PAYLOAD_BYTES = 1024


def _b64encode(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    if not text or len(text) > _MAX_TOKEN_LENGTH:
        raise ValueError("invalid base64 segment")
    if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in text):
        raise ValueError("invalid base64 segment")
    return urlsafe_b64decode(text + "=" * (-len(text) % 4))


class CredentialCodec:
    """Signs, encrypts and verifies legacy edit credentials."""

    def __init__(self, secret: bytes | None = None) -> None:
        if secret is None:
            secret = secrets.token_bytes(32)
        if not isinstance(secret, (bytes, bytearray)):
            raise TypeError("credential secret must be bytes")
        secret = bytes(secret)
        if len(secret) < _MIN_SECRET_BYTES:
            raise ValueError(
                f"credential secret must be at least {_MIN_SECRET_BYTES} bytes"
            )
        self._secret = secret

    # -- primitives ------------------------------------------------------- #

    def _mac(self, message: bytes) -> bytes:
        return hmac.new(self._secret, b"lv1-mac\x00" + message, hashlib.sha256).digest()[
            :_MAC_BYTES
        ]

    def _tag_for(self, backend_id: str) -> bytes:
        return hmac.new(
            self._secret,
            b"lv1-kid\x00" + backend_id.encode("utf-8"),
            hashlib.sha256,
        ).digest()[:_TAG_BYTES]

    def _keystream(self, tag: bytes, length: int) -> bytes:
        chunks: list[bytes] = []
        counter = 0
        while sum(len(chunk) for chunk in chunks) < length:
            chunks.append(
                hmac.new(
                    self._secret,
                    b"lv1-stream\x00" + tag + counter.to_bytes(4, "big"),
                    hashlib.sha256,
                ).digest()
            )
            counter += 1
        return b"".join(chunks)[:length]

    # -- public API ------------------------------------------------------- #

    def encode(self, backend_id: str, name_version: int, address_version: int) -> str:
        if not isinstance(backend_id, str) or not backend_id:
            raise ValueError("backend id must be a non-empty string")
        for label, value in (
            ("name version", name_version),
            ("address version", address_version),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        payload = json.dumps(
            {"i": backend_id, "n": name_version, "a": address_version},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        tag = self._tag_for(backend_id)
        keystream = self._keystream(tag, len(payload))
        ciphertext = bytes(a ^ b for a, b in zip(payload, keystream))
        mac = self._mac(tag + ciphertext)
        return f"{_PREFIX}.{_b64encode(tag + ciphertext)}.{_b64encode(mac)}"

    def decode(self, token: Any) -> tuple[str, dict[str, int]]:
        if not isinstance(token, str):
            raise ValidationError("credential must be a string")
        if not token or len(token) > _MAX_TOKEN_LENGTH:
            raise ValidationError("malformed backend credential")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != _PREFIX:
            raise ValidationError("malformed backend credential")
        try:
            blob = _b64decode(parts[1])
            mac = _b64decode(parts[2])
        except (ValueError, binascii.Error):
            raise ValidationError("malformed backend credential") from None
        if len(mac) != _MAC_BYTES or len(blob) <= _TAG_BYTES:
            raise ValidationError("malformed backend credential")
        if len(blob) - _TAG_BYTES > _MAX_PAYLOAD_BYTES:
            raise ValidationError("malformed backend credential")
        tag, ciphertext = blob[:_TAG_BYTES], blob[_TAG_BYTES:]
        if not hmac.compare_digest(mac, self._mac(tag + ciphertext)):
            raise ValidationError("backend credential signature mismatch")
        payload = bytes(
            a ^ b
            for a, b in zip(ciphertext, self._keystream(tag, len(ciphertext)))
        )
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("malformed backend credential") from None
        if (
            not isinstance(data, dict)
            or set(data) != {"i", "n", "a"}
            or not isinstance(data["i"], str)
            or not data["i"]
        ):
            raise ValidationError("malformed backend credential")
        versions: dict[str, int] = {}
        for field_name, key in (("name", "n"), ("address", "a")):
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValidationError("malformed backend credential")
            versions[field_name] = value
        return data["i"], versions
