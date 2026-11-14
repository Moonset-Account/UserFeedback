"""Shared helpers for building synthetic ZIP fixtures (stdlib only)."""

import os
import stat
import struct
import zipfile

STORED = zipfile.ZIP_STORED
DEFLATED = zipfile.ZIP_DEFLATED

MODE_FILE = stat.S_IFREG | 0o644
MODE_DIR = stat.S_IFDIR | 0o755
MODE_SYMLINK = stat.S_IFLNK | 0o777
MODE_FIFO = stat.S_IFIFO | 0o644


def write_zip(path, entries, compression=DEFLATED):
    """Write a synthetic zip.

    entries: list of tuples (name, data, mode[, compression]).
      name: archive member name (str)
      data: bytes (or str, encoded as utf-8)
      mode: unix mode stored in external_attr high bits, or None
    """
    with zipfile.ZipFile(path, "w") as zf:
        for entry in entries:
            name, data = entry[0], entry[1]
            mode = entry[2] if len(entry) > 2 else None
            comp = entry[3] if len(entry) > 3 else compression
            if isinstance(data, str):
                data = data.encode("utf-8")
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            if mode is not None:
                info.external_attr = (mode & 0xFFFF) << 16
            info.compress_type = comp
            zf.writestr(info, data)
    return path


def _iter_headers(data, sig):
    off = 0
    while True:
        i = data.find(sig, off)
        if i < 0:
            return
        yield i
        off = i + 4


def patch_central_file_size(zip_path, entry_name, new_size):
    """Rewrite the *declared* uncompressed size of one entry in the central
    directory, leaving the real data and CRC untouched. Produces an archive
    whose declared size lies about the actual expanded size."""
    with open(zip_path, "rb") as f:
        data = bytearray(f.read())
    for i in _iter_headers(data, b"PK\x01\x02"):
        name_len = struct.unpack_from("<H", data, i + 28)[0]
        name = bytes(data[i + 46:i + 46 + name_len]).decode("utf-8")
        if name == entry_name:
            struct.pack_into("<I", data, i + 24, new_size)
            with open(zip_path, "wb") as f:
                f.write(bytes(data))
            return
    raise ValueError("entry not found: %r" % entry_name)


def flip_data_byte(zip_path, entry_name):
    """Flip one byte of one entry's stored data so its CRC check fails."""
    with open(zip_path, "rb") as f:
        data = bytearray(f.read())
    for i in _iter_headers(data, b"PK\x03\x04"):
        name_len = struct.unpack_from("<H", data, i + 26)[0]
        extra_len = struct.unpack_from("<H", data, i + 28)[0]
        name = bytes(data[i + 30:i + 30 + name_len]).decode("utf-8")
        if name == entry_name:
            start = i + 30 + name_len + extra_len
            data[start] ^= 0xFF
            with open(zip_path, "wb") as f:
                f.write(bytes(data))
            return
    raise ValueError("entry not found: %r" % entry_name)


def set_encryption_flag(zip_path):
    """Set the general-purpose encryption bit in all local and central
    headers (fixture for the encrypted-entry rejection path)."""
    with open(zip_path, "rb") as f:
        data = bytearray(f.read())
    for i in _iter_headers(data, b"PK\x03\x04"):
        flags = struct.unpack_from("<H", data, i + 6)[0]
        struct.pack_into("<H", data, i + 6, flags | 0x1)
    for i in _iter_headers(data, b"PK\x01\x02"):
        flags = struct.unpack_from("<H", data, i + 8)[0]
        struct.pack_into("<H", data, i + 8, flags | 0x1)
    with open(zip_path, "wb") as f:
        f.write(bytes(data))


def snapshot(dirpath):
    """Sorted relative file/dir listing of a directory tree."""
    out = []
    for root, dirs, files in os.walk(dirpath):
        for d in dirs:
            out.append(os.path.relpath(os.path.join(root, d), dirpath) + "/")
        for fn in files:
            out.append(os.path.relpath(os.path.join(root, fn), dirpath))
    return sorted(out)


def read_file(dirpath, rel):
    with open(os.path.join(dirpath, rel), "rb") as f:
        return f.read()
