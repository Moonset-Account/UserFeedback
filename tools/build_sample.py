#!/usr/bin/env python3
"""Rebuild samples/demo.bin from samples/board.json (deterministic).

The demo image intentionally contains, in one single input:

  * P0 [256, 512) and P1 [512, 768): ADJACENT partitions (P1 starts
    exactly where P0 ends) -- must NOT be reported as overlapping;
  * P1 [512, 768) vs P2 [700, 900): a genuine overlap over [700, 768);
  * P3 [4000, 4512): extends past the 4096-byte image;
  * P4 [128, 192): covers part of the reserved 'bootrom' region [0, 256).

Record offsets in the image: P0@8, P1@24, P2@40, P3@56, P4@72
(header = 4-byte magic + 4-byte count, record size = 16 bytes).
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fwlayout.board import load_board  # noqa: E402

IMAGE_SIZE = 4096
PARTITIONS = [
    {"type": 1, "start": 256, "length": 256, "perms": 1},
    {"type": 2, "start": 512, "length": 256, "perms": 1},
    {"type": 3, "start": 700, "length": 200, "perms": 3},
    {"type": 4, "start": 4000, "length": 512, "perms": 1},
    {"type": 5, "start": 128, "length": 64, "perms": 1},
]


def build_image_bytes(board, partitions, image_size):
    record_size = board.record_size
    header_size = board.table_start + len(partitions) * record_size
    if image_size < header_size:
        raise ValueError("image_size too small for the declared header")
    buf = bytearray(image_size)
    if board.magic:
        buf[0:len(board.magic)] = board.magic
    buf[board.count_offset:board.table_start] = \
        len(partitions).to_bytes(board.count_size, board.endianness)
    for i, part in enumerate(partitions):
        pos = board.table_start + i * record_size
        for field in board.fields:
            buf[pos:pos + field.size] = \
                int(part[field.name]).to_bytes(field.size, board.endianness)
            pos += field.size
    return bytes(buf)


def main():
    board = load_board(os.path.join(ROOT, "samples", "board.json"))
    data = build_image_bytes(board, PARTITIONS, IMAGE_SIZE)
    out = os.path.join(ROOT, "samples", "demo.bin")
    with open(out, "wb") as fh:
        fh.write(data)
    print("wrote %s (%d bytes, %d partitions)" % (out, len(data),
                                                  len(PARTITIONS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
