"""Digest helpers using only the Python standard library."""

from __future__ import annotations

import binascii
import hashlib
import zlib

from .errors import EvidenceFormatError

_CRC_ALIASES = {"crc32", "crc-32", "zlib_crc32"}
_ADLER_ALIASES = {"adler32", "adler-32"}
_SIMPLE_ALIASES = {
    "xor": "xor8",
    "xor-8": "xor8",
    "xor8": "xor8",
    "sum": "sum8",
    "sum-8": "sum8",
    "sum8": "sum8",
    "sum-16": "sum16",
    "sum16": "sum16",
    "sum-32": "sum32",
    "sum32": "sum32",
}
_SIMPLE_SIZES = {"xor8": 1, "sum8": 1, "sum16": 2, "sum32": 4}


def normalize_algorithm(algorithm: str | None) -> str:
    if algorithm is None or str(algorithm).strip() == "":
        return "sha256"
    name = str(algorithm).strip().lower()
    if name in _CRC_ALIASES:
        return "crc32"
    if name in _ADLER_ALIASES:
        return "adler32"
    if name in _SIMPLE_ALIASES:
        return _SIMPLE_ALIASES[name]
    candidates = [
        name,
        name.replace("-", ""),
        name.replace("-", "_"),
        name.replace("_", ""),
    ]
    for candidate in candidates:
        try:
            hashlib.new(candidate)
        except (ValueError, TypeError):
            continue
        return candidate
    raise EvidenceFormatError(f"unsupported digest algorithm: {algorithm!r}")


def digest_hex(algorithm: str, data: bytes) -> str:
    algo = normalize_algorithm(algorithm)
    if algo == "crc32":
        return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
    if algo == "adler32":
        return f"{zlib.adler32(data) & 0xFFFFFFFF:08x}"
    if algo == "xor8":
        value = 0
        for byte in data:
            value ^= byte
        return f"{value:02x}"
    if algo == "sum8":
        return f"{sum(data) & 0xFF:02x}"
    if algo == "sum16":
        return f"{sum(data) & 0xFFFF:04x}"
    if algo == "sum32":
        return f"{sum(data) & 0xFFFFFFFF:08x}"
    return hashlib.new(algo, data).hexdigest()


def normalize_digest(digest: str | bytes, *, algorithm: str | None = None) -> tuple[str, str]:
    """Return ``(algorithm, lowercase_hex_digest)``.

    Strings may be plain hex or use an ``algorithm:hex`` prefix.
    """

    inferred_algorithm = algorithm
    value = digest
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).hex()
    elif not isinstance(value, str):
        raise EvidenceFormatError("digest must be a hex string or bytes")
    text = value.strip().lower().replace(" ", "")
    if ":" in text:
        prefix, suffix = text.split(":", 1)
        if not inferred_algorithm:
            inferred_algorithm = prefix
        text = suffix
    algo = normalize_algorithm(inferred_algorithm)
    if len(text) % 2:
        raise EvidenceFormatError("hex digest must contain an even number of hexadecimal digits")
    try:
        raw = binascii.unhexlify(text)
    except binascii.Error as exc:
        raise EvidenceFormatError("digest contains non-hexadecimal characters") from exc
    if algo in {"crc32", "adler32"}:
        expected_size = 4
    elif algo in _SIMPLE_SIZES:
        expected_size = _SIMPLE_SIZES[algo]
    else:
        expected_size = hashlib.new(algo).digest_size
    if len(raw) != expected_size:
        raise EvidenceFormatError(
            f"{algo} digest must be {expected_size * 2} hex characters, got {len(text)}"
        )
    return algo, text
