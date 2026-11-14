"""Deterministic synthetic fixtures for the digest-comparison regression.

Each fixture is a pair of fixed-length byte strings.  Values are derived from
SHA-256 of stable labels, so runs produce identical inputs without randomness
and without touching any external system.  Digest values are only used as
inputs; the delivered report formats never print them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

DEFAULT_LENGTH = 16


def derive_bytes(label: str, length: int) -> bytes:
    """Derive a deterministic byte string of exactly ``length`` bytes."""
    if length < 0:
        raise ValueError("length must be non-negative")
    material = b""
    counter = 0
    seed = label.encode("utf-8")
    while len(material) < length:
        material += hashlib.sha256(seed + b":" + str(counter).encode("ascii")).digest()
        counter += 1
    return material[:length]


@dataclass(frozen=True)
class SyntheticCase:
    """One labeled input pair.

    ``case_id`` is stable across runs.  ``differ_position`` is ``None`` for the
    equality baseline and otherwise the single byte position that differs.
    """

    case_id: str
    length: int
    left: bytes
    right: bytes
    differ_position: int | None

    @property
    def expected_equal(self) -> bool:
        return self.differ_position is None

    def public_label(self) -> str:
        """A description safe for logs: no digest bytes are included."""
        if self.differ_position is None:
            return "identical digests"
        return f"differ at byte {self.differ_position}"


def build_cases(length: int = DEFAULT_LENGTH) -> list[SyntheticCase]:
    """Build the baseline equality case plus one difference case per byte."""
    if length < 1:
        raise ValueError("length must be at least 1")
    base = derive_bytes("digest-baseline", length)
    cases: list[SyntheticCase] = [
        SyntheticCase("same", length, base, base, None)
    ]
    for position in range(length):
        mutated = bytearray(base)
        mutated[position] ^= 0x01  # guarantees this position differs
        cases.append(
            SyntheticCase(
                f"diff-{position:02d}",
                length,
                base,
                bytes(mutated),
                position,
            )
        )
    return cases


def boundary_cases() -> list[SyntheticCase]:
    """Small length-1 cases for boundary coverage."""
    one = derive_bytes("boundary-one", 1)
    other = bytes([one[0] ^ 0x01])
    return [
        SyntheticCase("boundary-same", 1, one, one, None),
        SyntheticCase("boundary-diff-00", 1, one, other, 0),
    ]
