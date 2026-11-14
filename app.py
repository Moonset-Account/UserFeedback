#!/usr/bin/env python3
"""Chunked, encrypted snapshot restore tool (verify-then-atomic-replace).

Pure Python 3 standard library. AES-256-GCM is implemented locally so the
tool has no third-party dependencies.

Storage layout
--------------
chunks_dir/<snapshot_id>/manifest.json   manifest, see _chunk_aad() fields
chunks_dir/<snapshot_id>/<any-name>.json one JSON object per chunk:
    {"index": int, "nonce": b64, "ciphertext": b64, "tag": b64}
keys_dir/<tenant>/<key_id>.json          key record:
    {"key_id": str, "key_hex": 64 hex chars, "status": "active" |
     "decrypt-only" | "revoked"}

Security properties
-------------------
* Every chunk is encrypted with AES-256-GCM. The GCM additional
  authenticated data (AAD) binds the tenant, snapshot id, key id, total
  chunk count, original file length and the chunk index, so manifest
  fields and chunk ordering are authenticated.
* Chunks may be supplied out of order / under arbitrary file names; the
  "index" field inside each chunk file is authoritative and plaintext is
  concatenated in index order.
* Duplicate chunks, missing chunks, unexpected indices, GCM
  authentication failures and restored-length mismatches are all
  reported with a reason, and any temporary file produced by the restore
  is removed.
* The target file is replaced (os.replace, atomic on POSIX) only after
  every chunk has been decrypted, authenticated and the total length
  verified. On any failure the pre-existing target file is left intact.
* Historical snapshots keep working as long as their key still exists
  under the tenant with status "active" or "decrypt-only"; "revoked" or
  missing keys are refused.
"""

import argparse
import base64
import glob
import hmac
import json
import os
import secrets
import sys
import tempfile

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RestoreError(Exception):
    """A verification or IO failure during restore; message is the reason."""


class AuthenticationError(Exception):
    """GCM tag verification failed."""


# ---------------------------------------------------------------------------
# AES-256 (FIPS-197), encryption direction only (GCM never decrypts a block)
# ---------------------------------------------------------------------------

_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)

_AES256_ROUNDS = 14


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _expand_key(key):
    """AES-256 key schedule -> list of 15 round keys, each 16 bytes (list)."""
    if len(key) != 32:
        raise ValueError("AES-256 key must be 32 bytes")
    nk = 8
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 0x01
    for i in range(nk, 4 * (_AES256_ROUNDS + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]              # RotWord
            temp = [_SBOX[b] for b in temp]         # SubWord
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    round_keys = []
    for r in range(_AES256_ROUNDS + 1):
        rk = []
        for c in range(4):
            rk.extend(words[4 * r + c])
        round_keys.append(rk)
    return round_keys


def _shift_rows(s):
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[4 * c + r] = s[4 * ((c + r) % 4) + r]
    return out


def _mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out[4 * c + 0] = _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3
        out[4 * c + 1] = a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3
        out[4 * c + 2] = a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3)
        out[4 * c + 3] = (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3)
    return out


def _aes256_encrypt_block(round_keys, block):
    """Encrypt one 16-byte block; returns a 16-byte bytes object."""
    s = [block[i] ^ round_keys[0][i] for i in range(16)]
    for rnd in range(1, _AES256_ROUNDS + 1):
        s = [_SBOX[b] for b in s]
        s = _shift_rows(s)
        if rnd != _AES256_ROUNDS:
            s = _mix_columns(s)
        s = [s[i] ^ round_keys[rnd][i] for i in range(16)]
    return bytes(s)


# ---------------------------------------------------------------------------
# GCM (NIST SP 800-38D), 96-bit nonces, 128-bit tags
# ---------------------------------------------------------------------------

_GHASH_R = 0xE1000000000000000000000000000000


def _gf_mul(x, y):
    """Multiply x by y in GF(2^128) with the GCM reduction polynomial."""
    z = 0
    v = x
    for i in range(128):
        if (y >> (127 - i)) & 1:
            z ^= v
        v = (v >> 1) ^ _GHASH_R if v & 1 else v >> 1
    return z


def _pad16(data):
    rem = len(data) % 16
    return data if rem == 0 else data + b"\x00" * (16 - rem)


def _ghash(h, aad, ciphertext):
    data = (
        _pad16(aad)
        + _pad16(ciphertext)
        + (len(aad) * 8).to_bytes(8, "big")
        + (len(ciphertext) * 8).to_bytes(8, "big")
    )
    x = 0
    for off in range(0, len(data), 16):
        x = _gf_mul(x ^ int.from_bytes(data[off:off + 16], "big"), h)
    return x


def _inc32(block_int):
    return (block_int & ~0xFFFFFFFF) | ((block_int + 1) & 0xFFFFFFFF)


def _gcm_keystream_xor(round_keys, j0, data):
    out = bytearray()
    ctr = int.from_bytes(j0, "big")
    for off in range(0, len(data), 16):
        ctr = _inc32(ctr)
        ks = _aes256_encrypt_block(round_keys, ctr.to_bytes(16, "big"))
        block = data[off:off + 16]
        out.extend(a ^ b for a, b in zip(block, ks))
    return bytes(out)


def gcm_encrypt(key, nonce, plaintext, aad):
    """AES-256-GCM encrypt; returns (ciphertext, 16-byte tag)."""
    if len(nonce) != 12:
        raise ValueError("GCM nonce must be 12 bytes")
    round_keys = _expand_key(key)
    h = int.from_bytes(_aes256_encrypt_block(round_keys, bytes(16)), "big")
    j0 = nonce + b"\x00\x00\x00\x01"
    ciphertext = _gcm_keystream_xor(round_keys, j0, plaintext)
    s = _ghash(h, aad, ciphertext)
    tag = s ^ int.from_bytes(_aes256_encrypt_block(round_keys, j0), "big")
    return ciphertext, tag.to_bytes(16, "big")


def gcm_decrypt(key, nonce, ciphertext, tag, aad):
    """AES-256-GCM decrypt; raises AuthenticationError on tag mismatch."""
    if len(nonce) != 12:
        raise AuthenticationError("nonce must be 12 bytes")
    if len(tag) != 16:
        raise AuthenticationError("tag must be 16 bytes")
    round_keys = _expand_key(key)
    h = int.from_bytes(_aes256_encrypt_block(round_keys, bytes(16)), "big")
    j0 = nonce + b"\x00\x00\x00\x01"
    s = _ghash(h, aad, ciphertext)
    expected = (s ^ int.from_bytes(_aes256_encrypt_block(round_keys, j0), "big")).to_bytes(16, "big")
    if not hmac.compare_digest(expected, tag):
        raise AuthenticationError("GCM authentication tag mismatch")
    return _gcm_keystream_xor(round_keys, j0, ciphertext)


# ---------------------------------------------------------------------------
# Manifest / chunk / key records
# ---------------------------------------------------------------------------

MANIFEST_NAME = "manifest.json"
ALLOWED_KEY_STATUS = ("active", "decrypt-only")


def _chunk_aad(manifest, index):
    """AAD binding the manifest fields and the chunk index."""
    obj = {
        "tenant": manifest["tenant"],
        "snapshot_id": manifest["snapshot_id"],
        "key_id": manifest["key_id"],
        "total_chunks": manifest["total_chunks"],
        "original_length": manifest["original_length"],
        "chunk_index": index,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _check_component(name, what):
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or "/" in name
        or "\\" in name
        or os.sep in name
        or (os.altsep and os.altsep in name)
    ):
        raise RestoreError("invalid %s %r: must be a single path component" % (what, name))


def _load_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise RestoreError("%s not found: %s" % (what, path))
    except (OSError, ValueError) as exc:
        raise RestoreError("cannot read %s %s: %s" % (what, path, exc))


def _load_manifest(snap_dir):
    manifest = _load_json(os.path.join(snap_dir, MANIFEST_NAME), "manifest")
    if not isinstance(manifest, dict):
        raise RestoreError("manifest is not a JSON object")
    for field in ("tenant", "snapshot_id", "key_id"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise RestoreError("manifest field %r must be a non-empty string" % field)
    for field in ("total_chunks", "original_length"):
        value = manifest.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RestoreError("manifest field %r must be a non-negative integer" % field)
    return manifest


def _load_key(keys_dir, tenant, key_id):
    path = os.path.join(keys_dir, tenant, key_id + ".json")
    record = _load_json(path, "key record for tenant %r key %r" % (tenant, key_id))
    status = record.get("status")
    if status not in ALLOWED_KEY_STATUS:
        raise RestoreError(
            "key %r for tenant %r is not authorized for decryption (status=%r)"
            % (key_id, tenant, status)
        )
    try:
        key = bytes.fromhex(record.get("key_hex", ""))
    except ValueError:
        raise RestoreError("key record %s has invalid key_hex" % path)
    if len(key) != 32:
        raise RestoreError("key record %s must hold a 32-byte AES-256 key" % path)
    return key


def _b64decode_field(obj, field, path):
    try:
        return base64.b64decode(obj[field], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise RestoreError("chunk file %s: bad base64 field %r (%s)" % (path, field, exc))


def _load_chunks(snap_dir):
    """Read every chunk file; return {index: (nonce, ciphertext, tag)}.

    Chunk files may appear in any order and under any '*.json' name other
    than the manifest; the 'index' field inside each file is authoritative.
    """
    chunks = {}
    paths = sorted(
        p for p in glob.glob(os.path.join(snap_dir, "*.json"))
        if os.path.basename(p) != MANIFEST_NAME
    )
    for path in paths:
        obj = _load_json(path, "chunk file")
        if not isinstance(obj, dict) or not isinstance(obj.get("index"), int) \
                or isinstance(obj.get("index"), bool):
            raise RestoreError("chunk file %s: missing integer 'index'" % path)
        index = obj["index"]
        if index in chunks:
            raise RestoreError(
                "duplicate chunk: index %d supplied by more than one file (e.g. %s)"
                % (index, os.path.basename(path))
            )
        nonce = _b64decode_field(obj, "nonce", path)
        ciphertext = _b64decode_field(obj, "ciphertext", path)
        tag = _b64decode_field(obj, "tag", path)
        if len(nonce) != 12:
            raise RestoreError("chunk file %s: nonce must be 12 bytes" % path)
        if len(tag) != 16:
            raise RestoreError("chunk file %s: tag must be 16 bytes" % path)
        chunks[index] = (nonce, ciphertext, tag)
    return chunks


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def restore_snapshot(tenant, snapshot_id, target_path, chunks_dir, keys_dir):
    """Verify the whole snapshot, then atomically replace target_path.

    Returns the number of bytes written. Raises RestoreError with a human
    readable reason on any verification or IO failure; in that case the
    pre-existing target file is left untouched and temporary files are
    removed.
    """
    _check_component(tenant, "tenant")
    _check_component(snapshot_id, "snapshot id")
    snap_dir = os.path.join(chunks_dir, snapshot_id)
    if not os.path.isdir(snap_dir):
        raise RestoreError("snapshot directory not found: %s" % snap_dir)

    manifest = _load_manifest(snap_dir)
    if manifest["tenant"] != tenant:
        raise RestoreError(
            "tenant mismatch: expected %r but manifest says %r"
            % (tenant, manifest["tenant"])
        )
    if manifest["snapshot_id"] != snapshot_id:
        raise RestoreError(
            "snapshot id mismatch: expected %r but manifest says %r"
            % (snapshot_id, manifest["snapshot_id"])
        )

    key = _load_key(keys_dir, tenant, manifest["key_id"])

    chunks = _load_chunks(snap_dir)
    total = manifest["total_chunks"]
    unexpected = sorted(i for i in chunks if i < 0 or i >= total)
    if unexpected:
        raise RestoreError(
            "unexpected chunk index %s (manifest declares total_chunks=%d)"
            % (unexpected, total)
        )
    missing = [i for i in range(total) if i not in chunks]
    if missing:
        raise RestoreError("missing chunks: indices %s of %d" % (missing, total))

    parts = []
    for index in range(total):
        nonce, ciphertext, tag = chunks[index]
        aad = _chunk_aad(manifest, index)
        try:
            parts.append(gcm_decrypt(key, nonce, ciphertext, tag, aad))
        except AuthenticationError as exc:
            raise RestoreError("authentication failed for chunk %d: %s" % (index, exc))

    data = b"".join(parts)
    expected_len = manifest["original_length"]
    if len(data) != expected_len:
        raise RestoreError(
            "restored length mismatch: got %d bytes, manifest original_length=%d"
            % (len(data), expected_len)
        )

    _atomic_replace(target_path, data)
    return len(data)


def _atomic_replace(target_path, data):
    """Write data to a temp file next to the target, fsync, os.replace.

    The temp file is removed if anything fails before the rename, so a
    failed restore never leaves artifacts behind and never touches the
    existing target.
    """
    target_dir = os.path.dirname(os.path.abspath(target_path))
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".%s.restore-" % os.path.basename(target_path),
        suffix=".tmp",
        dir=target_dir,
    )
    committed = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target_path)
        committed = True
        try:
            dir_fd = os.open(target_dir, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # directory fsync is best-effort
    finally:
        if not committed:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Backup (creates synthetic chunked/encrypted snapshots; used by the demo
# and the tests, and handy for manual verification)
# ---------------------------------------------------------------------------


def create_backup(tenant, snapshot_id, key_id, source_path, chunks_dir, keys_dir,
                  chunk_size=65536, key_bytes=None):
    """Encrypt source_path into chunks_dir/snapshot_id; returns the manifest."""
    _check_component(tenant, "tenant")
    _check_component(snapshot_id, "snapshot id")
    _check_component(key_id, "key id")
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    key_path = os.path.join(keys_dir, tenant, key_id + ".json")
    if key_bytes is None:
        if os.path.exists(key_path):
            key_bytes = _load_key(keys_dir, tenant, key_id)
        else:
            key_bytes = secrets.token_bytes(32)
    if len(key_bytes) != 32:
        raise ValueError("key must be 32 bytes")
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    if not os.path.exists(key_path):
        with open(key_path, "w", encoding="utf-8") as fh:
            json.dump({"key_id": key_id, "key_hex": key_bytes.hex(), "status": "active"}, fh)

    with open(source_path, "rb") as fh:
        data = fh.read()

    chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
    manifest = {
        "tenant": tenant,
        "snapshot_id": snapshot_id,
        "key_id": key_id,
        "total_chunks": len(chunks),
        "original_length": len(data),
    }
    snap_dir = os.path.join(chunks_dir, snapshot_id)
    os.makedirs(snap_dir, exist_ok=True)
    for index, chunk in enumerate(chunks):
        nonce = secrets.token_bytes(12)
        ciphertext, tag = gcm_encrypt(key_bytes, nonce, chunk, _chunk_aad(manifest, index))
        record = {
            "index": index,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "tag": base64.b64encode(tag).decode("ascii"),
        }
        with open(os.path.join(snap_dir, "chunk-%06d.json" % index), "w",
                  encoding="utf-8") as fh:
            json.dump(record, fh)
    with open(os.path.join(snap_dir, MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Chunked AES-256-GCM snapshot backup/restore tool "
                    "(verify first, then atomically replace the target)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_restore = sub.add_parser("restore", help="verify a snapshot and atomically restore it")
    p_restore.add_argument("--tenant", required=True, help="expected tenant")
    p_restore.add_argument("--snapshot-id", required=True, help="expected snapshot id")
    p_restore.add_argument("--target", required=True, help="target file path to replace")
    p_restore.add_argument("--chunks-dir", required=True, help="directory holding snapshots")
    p_restore.add_argument("--keys-dir", required=True, help="per-tenant key directory")

    p_backup = sub.add_parser("backup", help="create a chunked encrypted snapshot")
    p_backup.add_argument("--tenant", required=True)
    p_backup.add_argument("--snapshot-id", required=True)
    p_backup.add_argument("--key-id", required=True)
    p_backup.add_argument("--source", required=True, help="file to snapshot")
    p_backup.add_argument("--chunks-dir", required=True)
    p_backup.add_argument("--keys-dir", required=True)
    p_backup.add_argument("--chunk-size", type=int, default=65536)

    args = parser.parse_args(argv)

    if args.command == "backup":
        manifest = create_backup(
            args.tenant, args.snapshot_id, args.key_id, args.source,
            args.chunks_dir, args.keys_dir, chunk_size=args.chunk_size,
        )
        print("backup created: snapshot=%s tenant=%s chunks=%d bytes=%d"
              % (manifest["snapshot_id"], manifest["tenant"],
                 manifest["total_chunks"], manifest["original_length"]))
        return 0

    try:
        written = restore_snapshot(
            args.tenant, args.snapshot_id, args.target, args.chunks_dir, args.keys_dir
        )
    except RestoreError as exc:
        print("restore failed: %s" % exc, file=sys.stderr)
        return 1
    print("restore ok: %d bytes written to %s (snapshot=%s tenant=%s)"
          % (written, args.target, args.snapshot_id, args.tenant))
    return 0


if __name__ == "__main__":
    sys.exit(main())
