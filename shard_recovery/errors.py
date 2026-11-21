"""Exceptions raised by the shard recovery package."""

from __future__ import annotations

__all__ = [
    "ShardRecoveryError",
    "ShardFormatError",
    "ManifestError",
    "RecoveryPublishError",
]


class ShardRecoveryError(Exception):
    """Base class for all package errors."""


class ShardFormatError(ShardRecoveryError):
    """A shard file cannot be parsed or its header is internally inconsistent."""


class ManifestError(ShardRecoveryError):
    """A checkpoint manifest is missing required fields or references bad data."""


class RecoveryPublishError(ShardRecoveryError):
    """Publishing a recovered rank was refused (e.g. the recovery failed)."""
