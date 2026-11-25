"""Blind-signed anonymous telemetry credential demonstration."""

from __future__ import annotations

from .encoding import (
    decode_credential_message,
    encode_credential_message,
    hash_to_unit,
    length_prefix,
)
from .errors import (
    BlindMessageError,
    BlindTelemetryError,
    DuplicateReportError,
    EncodingError,
    InvalidBlindReplyError,
    InvalidCredentialError,
    PeriodConflictError,
    RandomSourceError,
    UnknownDeviceError,
)
from .protocol import (
    AcceptedReport,
    BlindReply,
    Collector,
    Credential,
    Device,
    PreparedBlindCredential,
    Receipt,
    Registrar,
    Report,
)
from .randomness import ScriptedRandomSource, random_unit_factor
from .rsa import (
    DEMO_KEY_PAIR,
    DEMO_PRIVATE_KEY,
    DEMO_PUBLIC_KEY,
    RsaKeyPair,
    RsaPrivateKey,
    RsaPublicKey,
    demo_key_pair,
)

__version__ = "1.0.0"

__all__ = [
    "AcceptedReport",
    "BlindMessageError",
    "BlindReply",
    "BlindTelemetryError",
    "Collector",
    "Credential",
    "DEMO_KEY_PAIR",
    "DEMO_PRIVATE_KEY",
    "DEMO_PUBLIC_KEY",
    "Device",
    "DuplicateReportError",
    "EncodingError",
    "InvalidBlindReplyError",
    "InvalidCredentialError",
    "PeriodConflictError",
    "PreparedBlindCredential",
    "RandomSourceError",
    "Receipt",
    "Registrar",
    "Report",
    "RsaKeyPair",
    "RsaPrivateKey",
    "RsaPublicKey",
    "ScriptedRandomSource",
    "UnknownDeviceError",
    "__version__",
    "decode_credential_message",
    "demo_key_pair",
    "encode_credential_message",
    "hash_to_unit",
    "length_prefix",
    "random_unit_factor",
]
