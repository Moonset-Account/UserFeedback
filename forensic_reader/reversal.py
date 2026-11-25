"""Byte transformation modes allowed for an individual chip dump.

The two order-reversing modes most relevant to the stated problem are:

* ``word`` -- reverse bytes inside every ``word_size``-byte word/chunk;
* ``chip`` -- reverse the complete chip image.

The additional ``bit_reverse`` and ``complement`` spellings are accepted for
records that use those common hardware conventions; they are never enabled
unless explicitly named.
"""

from __future__ import annotations

from collections.abc import Sequence

from .errors import WiringConfigurationError

NONE = "none"
WORD = "word"
CHIP = "chip"
BIT_REVERSE = "bit_reverse"
COMPLEMENT = "complement"

_ALIASES = {
    False: NONE,
    0: NONE,
    None: NONE,
    "none": NONE,
    "normal": NONE,
    "identity": NONE,
    "raw": NONE,
    "as-is": NONE,
    "asis": NONE,
    "no": NONE,
    "false": NONE,
    True: CHIP,
    1: CHIP,
    "reverse": CHIP,
    "reversed": CHIP,
    "all": CHIP,
    "whole": CHIP,
    "chip": CHIP,
    "chip_reverse": CHIP,
    "full": CHIP,
    "full_reverse": CHIP,
    "reverse_chip": CHIP,
    "entire": CHIP,
    "word": WORD,
    "word_reverse": WORD,
    "reverse_word": WORD,
    "within_word": WORD,
    "word_swap": WORD,
    "byte_swap": WORD,
    "swap": WORD,
    "endian": WORD,
    "endian_swap": WORD,
    "bit_reverse": BIT_REVERSE,
    "reverse_bits": BIT_REVERSE,
    "bits": BIT_REVERSE,
    "bit_swap": BIT_REVERSE,
    "complement": COMPLEMENT,
    "invert": COMPLEMENT,
    "inverted": COMPLEMENT,
    "not": COMPLEMENT,
    "bitwise_not": COMPLEMENT,
}

_BIT_REVERSE_TABLE = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))


def normalize_reversal_mode(mode: object) -> str | tuple[str, ...]:
    """Normalize a mode spelling.

    A sequence of modes denotes composition in the order supplied.
    """

    if isinstance(mode, (str, bytes)) and not isinstance(mode, str):
        # bytes is not a sensible mode, but avoid treating bytes as Sequence below.
        raise WiringConfigurationError(f"unsupported reversal mode: {mode!r}")
    if isinstance(mode, Sequence) and not isinstance(mode, (str, bytes)):
        modes = tuple(normalize_reversal_mode(item) for item in mode)
        flat: list[str] = []
        for item in modes:
            if isinstance(item, tuple):
                flat.extend(item)
            else:
                flat.append(item)
        return tuple(flat)
    if isinstance(mode, str):
        key = mode.strip().lower().replace("-", "_").replace(" ", "_")
        # Strings such as "word-reverse" become word_reverse; repeated underscores
        # are harmless because aliases are checked after collapse.
        while "__" in key:
            key = key.replace("__", "_")
    else:
        key = mode  # type: ignore[assignment]
    try:
        result = _ALIASES[key]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise WiringConfigurationError(f"unsupported reversal mode: {mode!r}") from exc
    return result


def _reverse_words(data: bytes, word_size: int) -> bytes:
    if word_size <= 1:
        return data
    pieces: list[bytes] = []
    for start in range(0, len(data), word_size):
        pieces.append(data[start : start + word_size][::-1])
    return b"".join(pieces)


def transform_chip(data: bytes, mode: object, word_size: int) -> bytes:
    """Apply one reversal mode or a sequence of modes to a chip image."""

    if not isinstance(word_size, int) or isinstance(word_size, bool) or word_size <= 0:
        raise WiringConfigurationError("word_size must be a positive integer")
    normalized = normalize_reversal_mode(mode)
    modes = normalized if isinstance(normalized, tuple) else (normalized,)
    result = data
    for operation in modes:
        if operation == NONE:
            continue
        if operation == CHIP:
            result = result[::-1]
        elif operation == WORD:
            result = _reverse_words(result, word_size)
        elif operation == BIT_REVERSE:
            result = result.translate(_BIT_REVERSE_TABLE)
        elif operation == COMPLEMENT:
            result = bytes(value ^ 0xFF for value in result)
        else:  # pragma: no cover - normalize_reversal_mode prevents this
            raise WiringConfigurationError(f"unsupported reversal operation: {operation}")
    return result
