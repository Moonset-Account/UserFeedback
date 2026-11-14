"""Configuration and identity validation tests."""

import unittest

from quota.config import LayerConfig, QuotaConfig
from quota.errors import ValidationError
from quota.identity import (
    canonical_account_name,
    canonical_source_address,
    require_canonical_account,
    require_request_id,
    require_tenant,
)


def config(account_limit=2, account_window=10.0, source_limit=3, source_window=20.0, ttl=10.0):
    return QuotaConfig(
        account=LayerConfig(account_limit, account_window),
        source=LayerConfig(source_limit, source_window),
        dedup_ttl_seconds=ttl,
    )


class ConfigTests(unittest.TestCase):
    def test_from_mapping_round_trip(self) -> None:
        cfg = QuotaConfig.from_mapping(
            {
                "account": {"limit": 2, "window_seconds": 10},
                "source": {"limit": 3, "window_seconds": 20.5},
                "dedup_ttl_seconds": 5,
            }
        )
        self.assertEqual(cfg.account.limit, 2)
        self.assertEqual(cfg.source.window_seconds, 20.5)
        self.assertEqual(cfg.to_dict()["dedup_ttl_seconds"], 5.0)

    def test_limit_must_be_positive_int(self) -> None:
        for bad in (0, -1, 1.5, True, "3"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    LayerConfig(limit=bad, window_seconds=10.0)

    def test_window_must_be_finite_and_positive(self) -> None:
        for bad in (0, -5, float("inf"), float("nan"), "10"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    LayerConfig(limit=1, window_seconds=bad)

    def test_unknown_and_missing_keys_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            QuotaConfig.from_mapping(
                {
                    "account": {"limit": 1, "window_seconds": 1},
                    "source": {"limit": 1, "window_seconds": 1},
                    "dedup_ttl_seconds": 1,
                    "extra": 1,
                }
            )
        with self.assertRaises(ValidationError):
            QuotaConfig.from_mapping(
                {"account": {"limit": 1, "window_seconds": 1}, "dedup_ttl_seconds": 1}
            )

    def test_zero_ttl_is_allowed_and_disables_dedup(self) -> None:
        cfg = config(ttl=0.0)
        self.assertEqual(cfg.dedup_ttl_seconds, 0.0)


class IdentityTests(unittest.TestCase):
    def test_accounts_must_arrive_canonical(self) -> None:
        self.assertEqual(require_canonical_account("alice"), "alice")
        for bad in ("Alice", " alice", "alice ", "ALICE"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    require_canonical_account(bad)

    def test_canonical_account_name_helper(self) -> None:
        self.assertEqual(canonical_account_name("  Alice.Smith "), "alice.smith")
        with self.assertRaises(ValidationError):
            canonical_account_name("   ")

    def test_source_normalized_to_compressed_form(self) -> None:
        self.assertEqual(canonical_source_address(" 203.0.113.7 "), "203.0.113.7")
        self.assertEqual(
            canonical_source_address("2001:0DB8:0000:0000:0000:0000:0000:0001"),
            "2001:db8::1",
        )
        self.assertEqual(canonical_source_address("::ffff:192.0.2.1"), "::ffff:c000:201")

    def test_ambiguous_ipv4_forms_are_rejected(self) -> None:
        # Leading zeros are rejected by ipaddress (octal ambiguity), which also
        # prevents two spellings of one address from splitting a quota bucket.
        for bad in ("203.0.113.007", "010.0.0.1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    canonical_source_address(bad)

    def test_source_rejects_non_literals_and_zones(self) -> None:
        for bad in ("example.com", "", "fe80::1%eth0", "  "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    canonical_source_address(bad)
        with self.assertRaises(ValidationError):
            canonical_source_address(203)

    def test_tenant_and_request_id_text_rules(self) -> None:
        self.assertEqual(require_tenant("tenant-a"), "tenant-a")
        self.assertEqual(require_request_id("req-1"), "req-1")
        for bad in ("", " padded", "tab\there", None, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    require_tenant(bad)


if __name__ == "__main__":
    unittest.main()
