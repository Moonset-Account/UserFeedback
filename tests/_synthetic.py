"""Shared synthetic-data helpers."""

from __future__ import annotations


def split_logical_image(image: bytes, chip_ids: tuple[str, ...], word_size: int) -> dict[str, bytes]:
    """Inverse of the reader's word-block interleaving for test data generation."""

    chip_count = len(chip_ids)
    if len(image) % chip_count != 0:
        raise ValueError("image length must be divisible by chip count")
    chip_length = len(image) // chip_count
    if chip_length % word_size != 0:
        raise ValueError("chip length must be divisible by word_size for this helper")

    chips = {chip_id: bytearray(chip_length) for chip_id in chip_ids}
    logical_position = 0
    for chip_offset in range(0, chip_length, word_size):
        for chip_id in chip_ids:
            chunk = image[logical_position : logical_position + word_size]
            chips[chip_id][chip_offset : chip_offset + word_size] = chunk
            logical_position += word_size
    return {chip_id: bytes(buffer) for chip_id, buffer in chips.items()}
