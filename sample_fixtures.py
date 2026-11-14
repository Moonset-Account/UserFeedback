"""Synthetic test fixtures: layer archives and marker rules.

All tokens here are synthetic test markers (``SYNTHETIC-TOKEN-...``); no real
credentials are used anywhere.
"""

import io
import json
import os
import tarfile

TOKEN = b"SYNTHETIC-TOKEN-ABCDEF12"
TOKEN2 = b"SYNTHETIC-TOKEN-99ZXYWVU"
BENIGN = b"nothing to see here\n"
RULE_PATTERN = r"SYNTHETIC-TOKEN-[A-Z0-9]{8}"
RULE_ID = "synthetic-token"


def write_layer(dirpath, filename, members):
    """Create a tar layer. members: list of (name, data); data None => directory."""
    path = os.path.join(dirpath, filename)
    with tarfile.open(path, "w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return path


def write_garbage_layer(dirpath, filename="corrupted.tar", payload=None):
    """Write bytes that are not a readable tar archive."""
    path = os.path.join(dirpath, filename)
    with open(path, "wb") as fh:
        fh.write(payload if payload is not None
                 else b"\xff\xfe\x00not-a-valid-tar-archive\x01\x02\x03")
    return path


def write_truncated_layer(dirpath, filename="truncated.tar"):
    """Write a valid tar cut in half (mid-archive truncation)."""
    tmp = write_layer(dirpath, "_full.tar", [("big.bin", b"A" * 8192)])
    with open(tmp, "rb") as fh:
        data = fh.read()
    os.unlink(tmp)
    path = os.path.join(dirpath, filename)
    with open(path, "wb") as fh:
        fh.write(data[: len(data) // 2])
    return path


def write_rules(dirpath, filename="rules.json"):
    path = os.path.join(dirpath, filename)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"rules": [{"id": RULE_ID, "pattern": RULE_PATTERN}]}, fh)
    return path


def build_sample_layers(dirpath):
    """Standard sample: whiteout deletion + opaque dir + clean layer.

    Returns the ordered layer paths (oldest first).
    """
    l0 = write_layer(dirpath, "layer0.tar", [
        ("app/", None),
        ("app/config.env", b"API_TOKEN=" + TOKEN + b"\n"),
        ("etc/", None),
        ("etc/secret.key", TOKEN + b"\n"),
        ("etc/old.txt", BENIGN),
    ])
    l1 = write_layer(dirpath, "layer1.tar", [
        ("app/.wh.config.env", b""),
        ("etc/.wh..wh..opq", b""),
        ("etc/new.txt", BENIGN),
    ])
    l2 = write_layer(dirpath, "layer2.tar", [
        ("docs/", None),
        ("docs/readme.md", BENIGN),
    ])
    return [l0, l1, l2]
