"""Exact decimal rescaling with one nearest-even rounding decision."""

from __future__ import annotations


def rescale_to_target_scale(
    mantissa: int,
    source_scale: int,
    target_scale: int,
) -> int:
    """Rescale an exact BCD mantissa to a target fixed-int scale.

    The source value is ``mantissa * 10**(-source_scale)``. The returned integer
    represents ``result * 10**(-target_scale)``.

    No binary float and no intermediate rounded decimal is used. When digits
    must be dropped, the complete remainder is compared with exactly half of the
    drop factor, so all discarded digits participate in one tie-to-even call.

    A negative value that rounds to zero is returned as positive integer zero,
    normalizing negative zero.
    """

    if source_scale < 0 or target_scale < 0:
        raise ValueError("scales must be non-negative")

    if target_scale >= source_scale:
        result = mantissa * 10 ** (target_scale - source_scale)
        return 0 if result == 0 else result

    drop_digits = source_scale - target_scale
    factor = 10**drop_digits
    magnitude = abs(mantissa)
    kept, remainder = divmod(magnitude, factor)
    twice_remainder = 2 * remainder

    if twice_remainder > factor:
        kept += 1
    elif twice_remainder == factor and (kept & 1):
        # Exact tie: round to the nearest even target integer.
        kept += 1

    # If kept == 0, returning positive zero normalizes a negative-zero result.
    if mantissa < 0 and kept != 0:
        return -kept
    return kept
