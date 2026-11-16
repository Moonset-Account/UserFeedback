"""Internal utilities"""

import base64
import binascii
from collections.abc import Hashable, Mapping
from typing import Any


def make_hashable(value: Any) -> Hashable:
    """Return a hashable representation of ``value`` that follows ``==``.

    The returned representation is for hashing only: it is not a serialization
    format. Containers are converted recursively so that their comparison
    semantics are kept: mappings and sets are unordered while lists and tuples
    are ordered.

    Scalars are returned as they are, so Python's own hash is used for them.
    This matters because Python considers some scalar values equal even when
    they have different JSON encodings: ``True``, ``1`` and ``1.0`` all compare
    equal but serialize differently. Hashing a serialization (like JSON) could
    therefore give two equal objects different hashes, breaking the hash
    contract and allowing equal objects to coexist in a set.
    """
    if isinstance(value, Mapping):
        return (
            "mapping",
            frozenset(
                (make_hashable(key), make_hashable(item))
                for key, item in value.items()
            ),
        )

    if isinstance(value, list):
        return ("list", tuple(make_hashable(item) for item in value))

    if isinstance(value, tuple):
        return ("tuple", tuple(make_hashable(item) for item in value))

    if isinstance(value, (set, frozenset)):
        return ("set", frozenset(make_hashable(item) for item in value))

    # bytearray and memoryview compare equal to bytes but are not hashable.
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)

    return value


def b64enc(data: bytes) -> str:
    """To encode byte sequence into base64 string

    Arguments:
        data: Byte sequence to encode

    Exceptions:
        TypeError: If "data" is not byte sequence

    Returns:
        base64 string
    """

    return base64.standard_b64encode(data).decode("utf-8")


def b64dec(string: str) -> bytes:
    """To decode byte sequence from base64 string

    Arguments:
        string: base64 string to decode

    Raises:
        binascii.Error: If invalid base64-encoded string

    Returns:
        A byte sequence
    """

    data = string.encode("utf-8")
    try:
        return base64.b64decode(data, validate=True)
    except binascii.Error:
        # altchars for urlsafe encoded base64 - instead of + and _ instead of /
        return base64.b64decode(data, altchars=b"-_", validate=True)
