"""Packed BCD decoding and target range checks."""

from __future__ import annotations

from .errors import ErrorCode, RowError, _RowConversionFailure
from .rounding import rescale_to_target_scale
from .spec import SourceColumn, TargetColumn

CANONICAL_POSITIVE_SIGNS = frozenset({0xC})
CANONICAL_NEGATIVE_SIGNS = frozenset({0xD})
UNSIGNED_POSITIVE_SIGNS = frozenset({0xF})


def _fail(error: RowError) -> None:
    raise _RowConversionFailure(error)


def _nibble(payload: bytes | bytearray | memoryview, nibble_index: int) -> int:
    byte = payload[nibble_index // 2]
    if nibble_index % 2 == 0:
        return (byte >> 4) & 0x0F
    return byte & 0x0F


def decode_packed_mantissa(
    row: int,
    payload: object,
    source: SourceColumn,
    *,
    accept_unsigned_sign: bool = False,
) -> int:
    """Decode one packed signed DECIMAL payload into an exact integer mantissa.

    Raises :class:`_RowConversionFailure` with a distinguished error code and
    location for bad length, non-digit nibbles, bad signs, or a non-zero even
    precision padding nibble that already exceeds source precision.
    """

    width = source.packed_width
    try:
        actual_length = len(payload)  # type: ignore[arg-type]
    except Exception as exc:  # defensive: caller supplied an unusable slot
        _fail(
            RowError(
                row=row,
                code=ErrorCode.INVALID_PAYLOAD,
                message=f"payload does not have a readable length: {exc}",
            )
        )
        raise  # pragma: no cover - _fail always raises

    if actual_length != width:
        _fail(
            RowError(
                row=row,
                code=ErrorCode.INVALID_PAYLOAD_LENGTH,
                message=(
                    f"expected packed DECIMAL width {width} byte(s) for "
                    f"precision {source.precision}, got {actual_length}"
                ),
                byte_offset=0,
                limit=width,
            )
        )

    # At this point len() succeeded, but indexing may still be invalid for a
    # malformed bytes-like object. Treat that as an invalid payload.
    try:
        sign_nibble_index = 2 * width - 1
        mantissa = 0

        # Every nibble except the final sign nibble is either the leading
        # padding nibble (even precision) or a decimal digit.
        for nibble_index in range(sign_nibble_index):
            nibble = _nibble(payload, nibble_index)  # type: ignore[arg-type]
            byte_offset = nibble_index // 2

            if nibble_index == 0 and source.precision % 2 == 0:
                if nibble == 0:
                    continue
                if nibble > 9:
                    _fail(
                        RowError(
                            row=row,
                            code=ErrorCode.INVALID_DIGIT_NIBBLE,
                            message=(
                                "leading packed-decimal padding nibble must be "
                                f"0, found 0x{nibble:X}"
                            ),
                            byte_offset=byte_offset,
                            nibble_index=nibble_index,
                            bad_nibble=nibble,
                        )
                    )
                decimal_limit = 10**source.precision - 1
                _fail(
                    RowError(
                        row=row,
                        code=ErrorCode.PRECISION_OVERFLOW,
                        message=(
                            "non-zero leading padding nibble contains an extra "
                            f"source digit {nibble}"
                        ),
                        byte_offset=byte_offset,
                        nibble_index=nibble_index,
                        bad_nibble=nibble,
                        limit=decimal_limit,
                        stage="source_leading_padding",
                    )
                )

            if nibble > 9:
                _fail(
                    RowError(
                        row=row,
                        code=ErrorCode.INVALID_DIGIT_NIBBLE,
                        message=(
                            "BCD digit nibble must be 0..9, found "
                            f"0x{nibble:X}"
                        ),
                        byte_offset=byte_offset,
                        nibble_index=nibble_index,
                        bad_nibble=nibble,
                    )
                )
            mantissa = mantissa * 10 + nibble

        sign_nibble = _nibble(payload, sign_nibble_index)  # type: ignore[arg-type]
    except _RowConversionFailure:
        raise
    except Exception as exc:
        _fail(
            RowError(
                row=row,
                code=ErrorCode.INVALID_PAYLOAD,
                message=f"payload could not be indexed as bytes: {exc}",
            )
        )
        raise  # pragma: no cover

    positive_signs = set(CANONICAL_POSITIVE_SIGNS)
    if accept_unsigned_sign:
        positive_signs.update(UNSIGNED_POSITIVE_SIGNS)

    if sign_nibble in positive_signs:
        negative = False
    elif sign_nibble in CANONICAL_NEGATIVE_SIGNS:
        negative = True
    else:
        allowed = "C/D" + ("/F-as-positive" if accept_unsigned_sign else "")
        _fail(
            RowError(
                row=row,
                code=ErrorCode.INVALID_SIGN_NIBBLE,
                message=(
                    f"invalid packed DECIMAL sign nibble 0x{sign_nibble:X}; "
                    f"expected {allowed}"
                ),
                byte_offset=width - 1,
                nibble_index=sign_nibble_index,
                bad_nibble=sign_nibble,
            )
        )

    if negative and mantissa != 0:
        return -mantissa
    # A D sign with all zero digits is intentionally returned as +0.
    return mantissa


def convert_one_value(
    row: int,
    payload: object,
    source: SourceColumn,
    target: TargetColumn,
    *,
    accept_unsigned_sign: bool = False,
) -> tuple[int, bytes]:
    """Decode, directly rescale, range-check, and encode one non-null row."""

    mantissa = decode_packed_mantissa(
        row,
        payload,
        source,
        accept_unsigned_sign=accept_unsigned_sign,
    )
    value = rescale_to_target_scale(
        mantissa,
        source.scale,
        target.scale,
    )
    rounding_happened = target.scale < source.scale
    stage = "after_rounding" if rounding_happened else "after_scale_shift"

    decimal_limit = target.decimal_max_integer
    if decimal_limit is not None and abs(value) > decimal_limit:
        verb = "rounding carry" if rounding_happened else "scale expansion"
        _fail(
            RowError(
                row=row,
                code=ErrorCode.PRECISION_OVERFLOW,
                message=(
                    f"target precision {target.precision} overflowed after "
                    f"{verb}: scaled integer {value} exceeds "
                    f"[-{decimal_limit}, {decimal_limit}]"
                ),
                rounded_value=value,
                limit=decimal_limit,
                stage=stage,
            )
        )

    if value < target.signed_min or value > target.signed_max:
        _fail(
            RowError(
                row=row,
                code=ErrorCode.FIXED_WIDTH_OVERFLOW,
                message=(
                    f"value {value} does not fit in signed "
                    f"{8 * target.width}-bit storage "
                    f"[{target.signed_min}, {target.signed_max}]"
                ),
                rounded_value=value,
                limit=target.signed_max,
                stage=stage,
            )
        )

    try:
        encoded = target.encode(value)
    except OverflowError as exc:
        _fail(
            RowError(
                row=row,
                code=ErrorCode.FIXED_WIDTH_OVERFLOW,
                message=str(exc),
                rounded_value=value,
                limit=target.signed_max,
                stage=stage,
            )
        )
        raise  # pragma: no cover

    # Defensive invariant: negative zero must never survive as a distinct
    # value or encoding. Signed two's-complement zero is all zero bytes.
    if value == 0:
        value = 0
        encoded = bytes(target.width)
    return value, encoded
