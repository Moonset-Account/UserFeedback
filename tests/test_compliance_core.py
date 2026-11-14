"""合规核对核心逻辑的单元测试（仅标准库 unittest）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compliance_core import (  # noqa: E402
    STATUS_AWAITING_ESCROW,
    STATUS_MISSING_REPORT,
    STATUS_PASS,
    STATUS_SEQUENCE_CONFLICT,
    STATUS_STALE_REPORT,
    STATUS_UNENCRYPTED,
    evaluate,
    parse_time,
)

CHECK = parse_time("2026-11-14T12:00:00Z")


def inv(volumes=("C:",), batch="B1", asset="A1"):
    return {"batch": batch, "assets": [{"asset_id": asset, "volumes": list(volumes)}]}


def rep(asset="A1", batch="B1", volume="C:", sequence=1,
        observed_at="2026-11-14T10:00:00Z", encrypted=True, key_id="K1"):
    return {
        "asset_id": asset,
        "batch": batch,
        "volume": volume,
        "sequence": sequence,
        "observed_at": observed_at,
        "encrypted": encrypted,
        "key_id": key_id,
    }


def receipt(asset="A1", batch="B1", volume="C:", key_id="K1"):
    return {"asset_id": asset, "batch": batch, "volume": volume, "key_id": key_id}


def run(inventory, reports, receipts, check_time=CHECK):
    return evaluate(inventory, reports, receipts, check_time)


def vol_status(report, asset="A1", volume="C:"):
    for d in report["devices"]:
        if d["asset_id"] == asset:
            for v in d["volumes"]:
                if v["volume"] == volume:
                    return v["status"]
    raise AssertionError(f"未找到卷结果: {asset} {volume}")


class VolumeStatusTests(unittest.TestCase):
    def test_pass_basic(self):
        r = run(inv(), [rep()], [receipt()])
        self.assertEqual(vol_status(r), STATUS_PASS)
        self.assertEqual(r["devices"][0]["status"], STATUS_PASS)

    def test_unencrypted(self):
        r = run(inv(), [rep(encrypted=False)], [receipt()])
        self.assertEqual(vol_status(r), STATUS_UNENCRYPTED)

    def test_missing_report(self):
        r = run(inv(), [], [receipt()])
        self.assertEqual(vol_status(r), STATUS_MISSING_REPORT)

    def test_stale_over_24h(self):
        r = run(inv(), [rep(observed_at="2026-11-13T11:59:59Z")], [receipt()])
        self.assertEqual(vol_status(r), STATUS_STALE_REPORT)

    def test_exactly_24h_passes(self):
        # 相差恰好 24 小时且观测时间严格早于核对时间：通过
        r = run(inv(), [rep(observed_at="2026-11-13T12:00:00Z")], [receipt()])
        self.assertEqual(vol_status(r), STATUS_PASS)

    def test_observation_equal_check_time_fails(self):
        # 观测时间必须严格早于核对时间
        r = run(inv(), [rep(observed_at="2026-11-14T12:00:00Z")], [receipt()])
        self.assertEqual(vol_status(r), STATUS_STALE_REPORT)

    def test_future_observation_fails(self):
        r = run(inv(), [rep(observed_at="2026-11-14T13:00:00Z")], [receipt()])
        self.assertEqual(vol_status(r), STATUS_STALE_REPORT)

    def test_sequence_conflict(self):
        reports = [rep(sequence=7, encrypted=True), rep(sequence=7, encrypted=False)]
        r = run(inv(), reports, [receipt()])
        self.assertEqual(vol_status(r), STATUS_SEQUENCE_CONFLICT)

    def test_same_sequence_identical_content_no_conflict(self):
        reports = [rep(sequence=7), rep(sequence=7)]
        r = run(inv(), reports, [receipt()])
        self.assertEqual(vol_status(r), STATUS_PASS)

    def test_conflict_on_key_id_difference(self):
        reports = [rep(sequence=3, key_id="K1"), rep(sequence=3, key_id="K2")]
        r = run(inv(), reports, [receipt()])
        self.assertEqual(vol_status(r), STATUS_SEQUENCE_CONFLICT)

    def test_highest_sequence_selected(self):
        # 低序号未加密、高序号已加密：以最高序号为准
        reports = [rep(sequence=1, encrypted=False), rep(sequence=2, encrypted=True)]
        r = run(inv(), reports, [receipt()])
        self.assertEqual(vol_status(r), STATUS_PASS)

    def test_other_batch_ignored(self):
        r = run(inv(batch="B2"), [rep(batch="B1")], [receipt(batch="B2")])
        self.assertEqual(vol_status(r), STATUS_MISSING_REPORT)


class EscrowTests(unittest.TestCase):
    def test_awaiting_escrow_no_receipt(self):
        r = run(inv(), [rep()], [])
        self.assertEqual(vol_status(r), STATUS_AWAITING_ESCROW)

    def test_receipt_wrong_key_after_rotation(self):
        # 轮换密钥后，旧密钥回执不能使新上报通过
        r = run(inv(), [rep(key_id="K-NEW")], [receipt(key_id="K-OLD")])
        self.assertEqual(vol_status(r), STATUS_AWAITING_ESCROW)

    def test_rotation_recovery_with_new_receipt(self):
        # 收到新密钥回执后重新通过
        reports = [rep(sequence=1, key_id="K-OLD"), rep(sequence=2, key_id="K-NEW")]
        receipts = [receipt(key_id="K-OLD"), receipt(key_id="K-NEW")]
        r = run(inv(), reports, receipts)
        self.assertEqual(vol_status(r), STATUS_PASS)
        self.assertEqual(r["devices"][0]["volumes"][0]["key_id"], "K-NEW")

    def test_receipt_wrong_batch(self):
        r = run(inv(), [rep()], [receipt(batch="B0")])
        self.assertEqual(vol_status(r), STATUS_AWAITING_ESCROW)

    def test_receipt_wrong_volume(self):
        r = run(inv(), [rep()], [receipt(volume="D:")])
        self.assertEqual(vol_status(r), STATUS_AWAITING_ESCROW)

    def test_receipt_wrong_asset(self):
        r = run(inv(), [rep()], [receipt(asset="A9")])
        self.assertEqual(vol_status(r), STATUS_AWAITING_ESCROW)


class AggregationTests(unittest.TestCase):
    def test_device_aggregation(self):
        inventory = {
            "batch": "B1",
            "assets": [{"asset_id": "A1", "volumes": ["C:", "D:"]}],
        }
        reports = [rep(volume="C:"), rep(volume="D:", encrypted=False)]
        receipts = [receipt(volume="C:"), receipt(volume="D:")]
        r = run(inventory, reports, receipts)
        dev = r["devices"][0]
        self.assertEqual(dev["status"], "FAIL")
        self.assertEqual(vol_status(r, volume="C:"), STATUS_PASS)
        self.assertEqual(vol_status(r, volume="D:"), STATUS_UNENCRYPTED)
        self.assertEqual(r["summary"]["devices_failed"], 1)
        self.assertEqual(
            r["summary"]["volume_status_counts"],
            {STATUS_PASS: 1, STATUS_UNENCRYPTED: 1},
        )

    def test_asset_level_batch_override(self):
        inventory = {
            "batch": "B-GLOBAL",
            "assets": [{"asset_id": "A1", "batch": "B-LOCAL", "volumes": ["C:"]}],
        }
        r = run(inventory, [rep(batch="B-LOCAL")], [receipt(batch="B-LOCAL")])
        self.assertEqual(vol_status(r), STATUS_PASS)
        self.assertEqual(r["devices"][0]["batch"], "B-LOCAL")


class ValidationTests(unittest.TestCase):
    def test_report_missing_field_raises(self):
        bad = rep()
        del bad["key_id"]
        with self.assertRaises(ValueError):
            run(inv(), [bad], [])

    def test_receipt_missing_field_raises(self):
        bad = receipt()
        del bad["volume"]
        with self.assertRaises(ValueError):
            run(inv(), [rep()], [bad])

    def test_bad_observed_at_raises(self):
        with self.assertRaises(ValueError):
            run(inv(), [rep(observed_at="not-a-time")], [])

    def test_parse_time_naive_treated_as_utc(self):
        dt = parse_time("2026-11-14T10:00:00")
        self.assertIsNotNone(dt.tzinfo)


if __name__ == "__main__":
    unittest.main()
