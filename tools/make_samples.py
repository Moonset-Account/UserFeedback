#!/usr/bin/env python3
"""Generate the manual sample fixtures under samples/.

The fixtures are synthetic and deterministic. They are built so that,
when scanned with ``--chunk-size 64 --max-bytes 100``, every coverage
status and both tricky hit kinds (read-chunk boundary straddle and
gzip-compressed log hit) are observable:

    samples/markers.txt        two fictional markers (ids 1 and 2)
    samples/build/app.log      marker 1 straddles the 64-byte read boundary
    samples/build/server.log.gz  gzip log, marker 2 straddles the 64-byte
                                 boundary of the *decompressed* stream
    samples/build/broken.gz    gzip magic + garbage  -> corrupt
    samples/build/payload.bin  NUL byte in first chunk -> skipped (binary)
    samples/build/big.log      150+ bytes, marker past byte 100 -> truncated
    samples/build/dead_link    broken symlink        -> unreadable
    samples/build/clean.txt    no markers            -> scanned, no hits
"""

import gzip
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
BUILD = os.path.join(SAMPLES, "build")

MARKER_1 = b"FAKE-TOKEN-ALPHA-7f3d9c"  # 23 bytes, id 1
MARKER_2 = b"FAKE-TOKEN-BETA-90ab12"   # 22 bytes, id 2


def main():
    if os.path.isdir(SAMPLES):
        shutil.rmtree(SAMPLES)
    os.makedirs(BUILD)

    with open(os.path.join(SAMPLES, "markers.txt"), "wb") as fh:
        fh.write(MARKER_1 + b"\n" + MARKER_2 + b"\n")

    # marker 1 starts at offset 60, so with 64-byte chunks it straddles
    # the first read-chunk boundary; it sits on line 2.
    app_log = b"boot ok\n" + b"x" * 52 + MARKER_1 + b"\nshutdown ok\n"
    assert app_log.index(MARKER_1) == 60
    with open(os.path.join(BUILD, "app.log"), "wb") as fh:
        fh.write(app_log)

    # marker 2 starts at decompressed offset 50 (line 3) and straddles
    # the 64-byte boundary of the decompressed stream.
    server_log = b"start\n" + b"init done\n" + b"y" * 34 + MARKER_2 + b"\nend\n"
    assert server_log.index(MARKER_2) == 50
    with open(os.path.join(BUILD, "server.log.gz"), "wb") as fh:
        fh.write(gzip.compress(server_log))

    with open(os.path.join(BUILD, "broken.gz"), "wb") as fh:
        fh.write(b"\x1f\x8b" + b"this is not a real gzip stream")

    with open(os.path.join(BUILD, "payload.bin"), "wb") as fh:
        fh.write(b"\x00\x01\x02binary" + MARKER_1)

    big_log = b"z" * 150 + MARKER_1 + b"\n"
    with open(os.path.join(BUILD, "big.log"), "wb") as fh:
        fh.write(big_log)

    with open(os.path.join(BUILD, "clean.txt"), "wb") as fh:
        fh.write(b"nothing to see here\nall clear\n")

    link = os.path.join(BUILD, "dead_link")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink("nonexistent-target", link)

    print("samples written to %s" % SAMPLES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
