"""Threshold secret reconstruction over GF(65537) with RS error correction."""

from .core import (
    INCONSISTENT,
    INSUFFICIENT,
    MAX_LENGTH,
    MAX_SHARES,
    OK,
    PRIME,
    BatchDescriptor,
    RecoveryResult,
    Share,
    generate_shares,
    make_descriptor,
    recover,
)

__all__ = [
    "PRIME",
    "MAX_SHARES",
    "MAX_LENGTH",
    "OK",
    "INSUFFICIENT",
    "INCONSISTENT",
    "BatchDescriptor",
    "Share",
    "RecoveryResult",
    "generate_shares",
    "make_descriptor",
    "recover",
]
