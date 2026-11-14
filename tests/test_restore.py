"""Unit tests for the chunked encrypted snapshot restore tool (app.py)."""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402


class AesGcmVectorTests(unittest.TestCase):
    """The local AES-256-GCM implementation is checked against published
    reference vectors before any restore logic is trusted."""

    def test_aes256_block_fips197(self):
        # FIPS-197 Appendix C.3 AES-256 example.
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f"
                            "101112131415161718191a1b1c1d1e1f")
        pt = bytes.fromhex("00112233445566778899aabbccddeeff")
        ct = app._aes256_encrypt_block(app._expand_key(key), pt)
        self.assertEqual(ct.hex(), "8ea2b7ca516745bfeafc49904b496089")

    def test_gcm_empty_plaintext_vector(self):
        # NIST GCM spec, AES-256, K=0, IV=0 (96-bit), empty P/AAD.
        ct, tag = app.gcm_encrypt(bytes(32), bytes(12), b"", b"")
        self.assertEqual(ct, b"")
        self.assertEqual(tag.hex(), "530f8afbc74536b9a963b4f1c4cb738b")

    def test_gcm_single_block_vector(self):
        # NIST GCM spec test case: one zero plaintext block, AES-256.
        ct, tag = app.gcm_encrypt(bytes(32), bytes(12), bytes(16), b"")
        self.assertEqual(ct.hex(), "cea7403d4d606b6e074ec5d3baf39d18")
        self.assertEqual(tag.hex(), "d0d1c8a799996bf0265b98b5d48ab919")

    def test_gcm_roundtrip_and_tamper(self):
        key = bytes(range(32))
        nonce = b"\x01" * 12
        aad = b"manifest-fields|chunk-3"
        ct, tag = app.gcm_encrypt(key, nonce, b"hello snapshot", aad)
        self.assertEqual(app.gcm_decrypt(key, nonce, ct, tag, aad), b"hello snapshot")
        with self.assertRaises(app.AuthenticationError):
            app.gcm_decrypt(key, nonce, ct, tag, b"other-aad")
        bad = bytearray(ct)
        bad[0] ^= 1
        with self.assertRaises(app.AuthenticationError):
            app.gcm_decrypt(key, nonce, bytes(bad), tag, aad)


class RestoreTestBase(unittest.TestCase):
    TENANT = "acme"
    SNAP = "snap-001"
    KEY_ID = "k1"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="snaptest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.chunks_dir = os.path.join(self.tmp, "chunks")
        self.keys_dir = os.path.join(self.tmp, "keys")
        self.target_dir = os.path.join(self.tmp, "target")
        os.makedirs(self.target_dir)
        self.target = os.path.join(self.target_dir, "data.bin")
        self.source = os.path.join(self.tmp, "source.bin")
        # 250 bytes with a 64-byte chunk size -> 4 chunks (last one short).
        self.payload = bytes((i * 7 + 3) % 256 for i in range(250))
        with open(self.source, "wb") as fh:
            fh.write(self.payload)
        self.manifest = app.create_backup(
            self.TENANT, self.SNAP, self.KEY_ID, self.source,
            self.chunks_dir, self.keys_dir, chunk_size=64,
        )

    def snap_dir(self, snap=None):
        return os.path.join(self.chunks_dir, snap or self.SNAP)

    def chunk_paths(self, snap=None):
        return sorted(
            p for p in os.listdir(self.snap_dir(snap)) if p != app.MANIFEST_NAME
        )

    def load_chunk(self, name, snap=None):
        with open(os.path.join(self.snap_dir(snap), name), encoding="utf-8") as fh:
            return json.load(fh)

    def write_chunk(self, name, obj, snap=None):
        with open(os.path.join(self.snap_dir(snap), name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def leftover_temps(self):
        return [f for f in os.listdir(self.target_dir) if ".restore-" in f]

    def set_key_status(self, key_id, status, tenant=None):
        path = os.path.join(self.keys_dir, tenant or self.TENANT, key_id + ".json")
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
        rec["status"] = status
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)


class RestoreSuccessTests(RestoreTestBase):
    def test_restore_into_empty_location(self):
        written = app.restore_snapshot(
            self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assertEqual(written, len(self.payload))
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), self.payload)
        self.assertEqual(self.leftover_temps(), [])

    def test_out_of_order_and_odd_filenames(self):
        # Rename chunk files so directory order is reversed and names carry
        # no ordering information; restore must still concatenate by index.
        for name in self.chunk_paths():
            obj = self.load_chunk(name)
            os.remove(os.path.join(self.snap_dir(), name))
            self.write_chunk("part-%02d.json" % (99 - obj["index"]), obj)
        app.restore_snapshot(
            self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), self.payload)

    def test_historical_snapshot_uses_still_authorized_old_key(self):
        # Rotate: k1 becomes decrypt-only, k2 becomes the active key. The old
        # snapshot must still restore with k1.
        app.create_backup("acme", "snap-002", "k2", self.source,
                          self.chunks_dir, self.keys_dir, chunk_size=64)
        self.set_key_status("k1", "decrypt-only")
        app.restore_snapshot(
            self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), self.payload)

    def test_empty_file_snapshot(self):
        empty = os.path.join(self.tmp, "empty.bin")
        open(empty, "wb").close()
        app.create_backup(self.TENANT, "snap-empty", self.KEY_ID, empty,
                          self.chunks_dir, self.keys_dir, chunk_size=64)
        written = app.restore_snapshot(
            self.TENANT, "snap-empty", self.target, self.chunks_dir, self.keys_dir)
        self.assertEqual(written, 0)
        self.assertEqual(os.path.getsize(self.target), 0)


class RestoreFailureTests(RestoreTestBase):
    """Every failure must come with a reason, leave the target untouched and
    leave no temporary files behind."""

    ORIGINAL_TARGET = b"pre-existing target content\n"

    def setUp(self):
        super().setUp()
        with open(self.target, "wb") as fh:
            fh.write(self.ORIGINAL_TARGET)

    def assert_failed_cleanly(self, ctx, *reason_fragments):
        self.assertIsInstance(ctx.exception, app.RestoreError)
        message = str(ctx.exception).lower()
        for fragment in reason_fragments:
            self.assertIn(fragment, message)
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), self.ORIGINAL_TARGET,
                             "failed restore must not modify the target")
        self.assertEqual(self.leftover_temps(), [],
                         "failed restore must not leave temp files")

    def test_corrupted_last_chunk(self):
        last = max(self.chunk_paths())  # chunk-000003.json
        obj = self.load_chunk(last)
        import base64
        ct = bytearray(base64.b64decode(obj["ciphertext"]))
        ct[0] ^= 0x01
        obj["ciphertext"] = base64.b64encode(bytes(ct)).decode("ascii")
        self.write_chunk(last, obj)
        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(
                self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assert_failed_cleanly(ctx, "authentication failed", "chunk 3")

    def test_tampered_tag(self):
        name = self.chunk_paths()[1]
        obj = self.load_chunk(name)
        obj["tag"] = obj["tag"][:4] + ("00" if obj["tag"][4:6] != "00" else "ff") + obj["tag"][6:]
        self.write_chunk(name, obj)
        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(
                self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assert_failed_cleanly(ctx, "authentication failed")

    def test_duplicate_chunk(self):
        first = self.chunk_paths()[0]
        obj = self.load_chunk(first)
        self.write_chunk("copy-of-first.json", obj)  # same index, second file
        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(
                self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assert_failed_cleanly(ctx, "duplicate chunk", "index 0")

    def test_missing_chunk(self):
        os.remove(os.path.join(self.snap_dir(), self.chunk_paths()[2]))
        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(
                self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assert_failed_cleanly(ctx, "missing chunks", "2")

    def test_restored_length_mismatch(self):
        # A consistently-encrypted snapshot whose manifest claims a wrong
        # original_length: GCM passes (the wrong length is part of the AAD),
        # so the explicit length check must catch it.
        key = bytes(range(32))
        tenant, snap, key_id = "acme", "snap-len", "klen"
        os.makedirs(os.path.join(self.keys_dir, tenant), exist_ok=True)
        with open(os.path.join(self.keys_dir, tenant, key_id + ".json"), "w") as fh:
            json.dump({"key_id": key_id, "key_hex": key.hex(), "status": "active"}, fh)
        payload = b"hello world" * 10
        manifest = {"tenant": tenant, "snapshot_id": snap, "key_id": key_id,
                    "total_chunks": 1, "original_length": len(payload) + 5}
        nonce = b"\x02" * 12
        ct, tag = app.gcm_encrypt(key, nonce, payload, app._chunk_aad(manifest, 0))
        snap_dir = os.path.join(self.chunks_dir, snap)
        os.makedirs(snap_dir)
        import base64
        with open(os.path.join(snap_dir, "chunk-000000.json"), "w") as fh:
            json.dump({"index": 0,
                       "nonce": base64.b64encode(nonce).decode("ascii"),
                       "ciphertext": base64.b64encode(ct).decode("ascii"),
                       "tag": base64.b64encode(tag).decode("ascii")}, fh)
        with open(os.path.join(snap_dir, app.MANIFEST_NAME), "w") as fh:
            json.dump(manifest, fh)
        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(tenant, snap, self.target, self.chunks_dir, self.keys_dir)
        self.assert_failed_cleanly(ctx, "length mismatch", "115")

    def test_tampered_manifest_fails_authentication(self):
        # Manifest fields are authenticated: flipping total_chunks breaks the
        # AAD of every chunk.
        mpath = os.path.join(self.snap_dir(), app.MANIFEST_NAME)
        with open(mpath, encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["original_length"] += 1
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(
                self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assert_failed_cleanly(ctx, "authentication failed")

    def test_wrong_tenant_rejected(self):
        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(
                "other-tenant", self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assert_failed_cleanly(ctx, "tenant mismatch")

    def test_revoked_key_rejected(self):
        self.set_key_status(self.KEY_ID, "revoked")
        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(
                self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assert_failed_cleanly(ctx, "not authorized", "revoked")

    def test_missing_key_rejected(self):
        os.remove(os.path.join(self.keys_dir, self.TENANT, self.KEY_ID + ".json"))
        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(
                self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assert_failed_cleanly(ctx, "not found")

    def test_unexpected_extra_chunk_index(self):
        obj = self.load_chunk(self.chunk_paths()[0])
        obj["index"] = 7  # manifest declares total_chunks=4
        self.write_chunk("stray.json", obj)
        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(
                self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assert_failed_cleanly(ctx, "unexpected chunk index")


class TwoPhaseDemoTest(RestoreTestBase):
    """The requested demonstration: same target file, two restore attempts.

    Phase 1 corrupts the last chunk of the backup -> restore must fail and
    the target must be preserved. Phase 2 uses the intact backup -> the
    target must be replaced by the fully restored content.
    """

    def test_corrupt_then_intact_restore_on_same_target(self):
        original_target = b"current live content of the target file\n"
        with open(self.target, "wb") as fh:
            fh.write(original_target)

        # --- phase 1: corrupted last chunk -------------------------------
        good_files = {}
        for name in self.chunk_paths():
            good_files[name] = self.load_chunk(name)
        import base64
        last = max(good_files)  # chunk-000003.json
        corrupted = dict(good_files[last])
        ct = bytearray(base64.b64decode(corrupted["ciphertext"]))
        ct[-1] ^= 0xFF
        corrupted["ciphertext"] = base64.b64encode(bytes(ct)).decode("ascii")
        self.write_chunk(last, corrupted)

        with self.assertRaises(app.RestoreError) as ctx:
            app.restore_snapshot(
                self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assertIn("authentication failed", str(ctx.exception))
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), original_target,
                             "phase 1: target must be preserved")
        self.assertEqual(self.leftover_temps(), [],
                         "phase 1: temp files must be cleaned up")

        # --- phase 2: intact backup --------------------------------------
        self.write_chunk(last, good_files[last])  # swap the good chunk back
        written = app.restore_snapshot(
            self.TENANT, self.SNAP, self.target, self.chunks_dir, self.keys_dir)
        self.assertEqual(written, len(self.payload))
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), self.payload,
                             "phase 2: target must be fully replaced")
        self.assertEqual(self.leftover_temps(), [])


if __name__ == "__main__":
    unittest.main()
