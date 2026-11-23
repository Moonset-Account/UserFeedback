"""Validation and credential-format tests: bad input must never mutate state."""

from __future__ import annotations

import unittest

from dualversion import BackendStore, ValidationError, WriteConflict
from dualversion.credentials import CredentialCodec

from .support import TEST_SECRET, new_store


class FieldValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = new_store()
        self.item = self.store.spec_add("api", "10.0.0.1:80")

    def assertRejected(self, fn, *args, **kwargs) -> None:
        with self.assertRaises(ValidationError):
            fn(*args, **kwargs)
        self.assertEqual(len(self.store.spec_get()["items"]), 1)
        self.assertEqual(self.store.resource_version, 1)

    def test_name_must_be_non_empty_string(self) -> None:
        for bad in ("", "   ", None, 3, ["x"]):
            with self.subTest(name=bad):
                self.assertRejected(self.store.spec_add, bad, "1.1.1.1:1")

    def test_name_length_limit(self) -> None:
        self.assertRejected(self.store.spec_add, "n" * 254, "1.1.1.1:1")
        self.store.spec_add("n" * 253, "1.1.1.1:1")

    def test_name_rejects_control_characters(self) -> None:
        self.assertRejected(self.store.spec_add, "a\tb", "1.1.1.1:1")
        self.assertRejected(self.store.spec_add, "a\x7fb", "1.1.1.1:1")

    def test_address_validation(self) -> None:
        self.assertRejected(self.store.spec_add, "x", "")
        self.assertRejected(self.store.spec_add, "x", None)
        self.assertRejected(self.store.spec_add, "x", "a" * 2049)

    def test_weight_validation(self) -> None:
        for bad in (True, False, "10", 10.0, -1, 1001, None):
            with self.subTest(weight=bad):
                self.assertRejected(self.store.spec_add, "x", "1:1", weight=bad)

    def test_drain_validation(self) -> None:
        for bad in (0, 1, "true", None):
            with self.subTest(drain=bad):
                self.assertRejected(self.store.spec_add, "x", "1:1", drain=bad)

    def test_spec_write_unknown_field(self) -> None:
        self.assertRejected(self.store.spec_write, self.item["id"], {"nope": 1})

    def test_legacy_cannot_write_invisible_fields(self) -> None:
        credential = self.store.legacy_get()["items"][0]["credential"]
        for field, value in (("weight", 5), ("drain", True), ("id", "x")):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    self.store.legacy_write(credential, {field: value})
        canonical = self.store.spec_get(self.item["id"])["items"][0]
        self.assertEqual(canonical["weight"], 100)
        self.assertFalse(canonical["drain"])

    def test_expected_versions_validation(self) -> None:
        backend_id = self.item["id"]
        with self.assertRaises(ValidationError):
            self.store.spec_write(backend_id, {"weight": 2}, {"weight": 0})
        with self.assertRaises(ValidationError):
            self.store.spec_write(backend_id, {"weight": 2}, {"weight": True})
        with self.assertRaises(ValidationError):
            self.store.spec_write(backend_id, {"weight": 2}, {"bogus": 1})
        with self.assertRaises(ValidationError):
            self.store.spec_write(backend_id, {"weight": 2}, ["weight"])

    def test_write_requires_expected_version_for_changes(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.spec_write(self.item["id"], {"weight": 2})
        # No-op writes do not need a version because nothing changes.
        result = self.store.spec_write(self.item["id"], {"weight": 100})
        self.assertEqual(result["status"], "unchanged")

    def test_commit_payload_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.legacy_commit("not-a-list")
        with self.assertRaises(ValidationError):
            self.store.legacy_commit([{"name": "a"}])
        with self.assertRaises(ValidationError):
            self.store.legacy_commit([{"name": "a", "address": "1:1", "weight": 5}])
        with self.assertRaises(ValidationError):
            self.store.legacy_commit([None])
        with self.assertRaises(ValidationError):
            self.store.legacy_commit([], deleted_credentials="one-token")
        with self.assertRaises(ValidationError):
            self.store.legacy_commit([], replace="yes")
        self.assertEqual(self.store.resource_version, 1)

    def test_commit_rejects_duplicate_names(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.legacy_commit(
                [
                    {"name": "dup", "address": "1:1"},
                    {"name": "dup", "address": "2:2"},
                ]
            )
        self.assertEqual(len(self.store.spec_get()["items"]), 1)

    def test_idempotency_key_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.spec_add("x", "1:1", idempotency_key="")
        with self.assertRaises(ValidationError):
            self.store.spec_add("x", "1:1", idempotency_key=5)
        with self.assertRaises(ValidationError):
            self.store.spec_add("x", "1:1", idempotency_key="k" * 257)

    def test_history_argument_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.history(-1)
        with self.assertRaises(ValidationError):
            self.store.history(True)

    def test_constructor_validation(self) -> None:
        with self.assertRaises(ValidationError):
            BackendStore(history_limit=-1)
        with self.assertRaises(ValidationError):
            BackendStore(history_limit="5")
        with self.assertRaises(ValueError):
            BackendStore(secret=b"short")
        with self.assertRaises(TypeError):
            BackendStore(secret="text")

    def test_validation_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(ValidationError, ValueError))
        self.assertFalse(issubclass(WriteConflict, ValueError))


class CredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.codec = CredentialCodec(TEST_SECRET)

    def test_round_trip(self) -> None:
        token = self.codec.encode("be-abc", 2, 3)
        self.assertEqual(
            self.codec.decode(token), ("be-abc", {"name": 2, "address": 3})
        )

    def test_deterministic(self) -> None:
        self.assertEqual(self.codec.encode("be-abc", 1, 1), self.codec.encode("be-abc", 1, 1))

    def test_token_hides_backend_id_in_plain_text(self) -> None:
        token = self.codec.encode("be-supersecretid", 1, 1)
        self.assertNotIn("supersecret", token)
        self.assertNotIn("be-", token)

    def test_versions_are_bound(self) -> None:
        self.assertNotEqual(
            self.codec.encode("be-abc", 1, 1), self.codec.encode("be-abc", 2, 1)
        )

    def test_tampering_is_rejected(self) -> None:
        token = self.codec.encode("be-abc", 1, 1)
        prefix, blob, mac = token.split(".")
        flipped = ("A" if blob[0] != "A" else "B") + blob[1:]
        for bad in (
            f"{prefix}.{flipped}.{mac}",
            f"{prefix}.{blob}.{'A' * len(mac)}",
            "lv2." + blob + "." + mac,
            token + "x",
            "",
            "abc",
            "lv1...",
            None,
            5,
        ):
            with self.subTest(token=bad):
                with self.assertRaises(ValidationError):
                    self.codec.decode(bad)

    def test_wrong_secret_rejected(self) -> None:
        token = CredentialCodec(TEST_SECRET).encode("be-abc", 1, 1)
        with self.assertRaises(ValidationError):
            CredentialCodec(b"another-secret-value-0123456789").decode(token)

    def test_store_rejects_foreign_credentials(self) -> None:
        store_a = BackendStore(secret=b"secret-for-store-a-0123456789")
        store_b = BackendStore(secret=b"secret-for-store-b-0123456789")
        store_a.spec_add("api", "1:1")
        token = store_a.legacy_get()["items"][0]["credential"]
        with self.assertRaises(ValidationError):
            store_b.legacy_write(token, {"name": "x"})


if __name__ == "__main__":
    unittest.main()
