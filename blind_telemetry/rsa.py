"""Small demonstration RSA keys and integer validation.

The default key is the widely used textbook example ``p=61, q=53``.  It is
intentionally far too small for real security and exists only to make blind
RSA operations and tests transparent.  Do not use these parameters in a real
telemetry system.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from .errors import BlindMessageError, RsaParameterError

DEMO_P: Final[int] = 61
DEMO_Q: Final[int] = 53
DEMO_N: Final[int] = DEMO_P * DEMO_Q  # 3233
DEMO_E: Final[int] = 17
DEMO_D: Final[int] = 2753  # 17 * 2753 == 1 mod lcm(60, 52) and phi(3233)


def validate_unit(value: int, n: int, *, name: str = "value") -> int:
    """Validate a canonical nonzero residue coprime to ``n``."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise BlindMessageError(f"{name} must be an integer")
    if not 0 < value < n:
        raise BlindMessageError(f"{name} must be in [1, n-1]")
    if math.gcd(value, n) != 1:
        raise BlindMessageError(f"{name} must be coprime to modulus n")
    return value


@dataclass(frozen=True)
class RsaPublicKey:
    n: int
    e: int

    def __post_init__(self) -> None:
        if isinstance(self.n, bool) or not isinstance(self.n, int) or self.n <= 3:
            raise RsaParameterError("public modulus n must be an integer > 3")
        if isinstance(self.e, bool) or not isinstance(self.e, int) or self.e < 3:
            raise RsaParameterError("public exponent e must be an integer >= 3")
        if self.e >= self.n:
            raise RsaParameterError("public exponent e must be less than n")

    def raw_public_operation(self, value: int) -> int:
        return pow(value, self.e, self.n)

    def verify_signature_integer(self, signature: int, expected: int) -> bool:
        if (
            isinstance(signature, bool)
            or not isinstance(signature, int)
            or not 0 <= signature < self.n
        ):
            return False
        return self.raw_public_operation(signature) == expected


@dataclass(frozen=True)
class RsaPrivateKey:
    public_key: RsaPublicKey
    d: int

    def __post_init__(self) -> None:
        if isinstance(self.d, bool) or not isinstance(self.d, int) or self.d <= 1:
            raise RsaParameterError("private exponent d must be an integer > 1")
        if self.d >= self.public_key.n:
            raise RsaParameterError("textbook RSA private exponent should be < n")

    @property
    def n(self) -> int:
        return self.public_key.n

    @property
    def e(self) -> int:
        return self.public_key.e

    def sign_unit(self, blinded_message: int) -> int:
        """Perform the raw private RSA operation on a validated unit."""

        validate_unit(blinded_message, self.n, name="blinded_message")
        return pow(blinded_message, self.d, self.n)


@dataclass(frozen=True)
class RsaKeyPair:
    private_key: RsaPrivateKey
    public_key: RsaPublicKey


def demo_key_pair() -> RsaKeyPair:
    """Return the fixed textbook RSA demonstration key pair."""

    public = RsaPublicKey(DEMO_N, DEMO_E)
    private = RsaPrivateKey(public, DEMO_D)
    return RsaKeyPair(private_key=private, public_key=public)


DEMO_KEY_PAIR: Final[RsaKeyPair] = demo_key_pair()
DEMO_PUBLIC_KEY: Final[RsaPublicKey] = DEMO_KEY_PAIR.public_key
DEMO_PRIVATE_KEY: Final[RsaPrivateKey] = DEMO_KEY_PAIR.private_key
