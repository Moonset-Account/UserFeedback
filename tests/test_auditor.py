import copy
import unittest

from synthetic_data import build_scenario

from volume_audit import AuditDataError, audit_document, audit_handoffs


def device_report(report, device_id):
    return next(item for item in report["devices"] if item["device_id"] == device_id)


def volume(report, device_id, instance_id):
    device = device_report(report, device_id)
    return next(item for item in device["volumes"] if item["volume_instance_id"] == instance_id)


def historical_volume(report, device_id, instance_id):
    device = device_report(report, device_id)
    return next(
        item for item in device["historical_volumes"] if item["volume_instance_id"] == instance_id
    )


class SyntheticBatchAuditTests(unittest.TestCase):
    def setUp(self):
        self.document = build_scenario()
        self.report = audit_document(copy.deepcopy(self.document))

    def test_batch_summary_classifies_normal_states(self):
        summary = self.report["summary"]
        self.assertEqual(summary["device_count"], 3)
        self.assertEqual(summary["protected_count"], 1)  # D-1 new System
        self.assertEqual(summary["in_progress_count"], 1)  # D-1 new Data partial
        self.assertEqual(summary["missing_receipt_count"], 1)  # D-2 late confirmation
        self.assertEqual(summary["not_required_count"], 1)  # D-3 scratch
        self.assertEqual(
            [item["volume_instance_id"] for item in self.report["protected_volumes"]],
            ["D1-SYS-NEW"],
        )
        self.assertEqual(
            [item["volume_instance_id"] for item in self.report["in_progress_volumes"]],
            ["D1-DATA-NEW"],
        )
        self.assertEqual(
            [item["volume_instance_id"] for item in self.report["missing_receipt_volumes"]],
            ["D2-DATA-NEW"],
        )
        self.assertEqual(
            sorted(
                item["volume_instance_id"]
                for item in self.report["historical_protected_volumes"]
            ),
            ["D1-DATA-OLD", "D1-SYS-OLD"],
        )

    def test_completed_volume_uses_overlapping_receipts_and_cites_them(self):
        sys_volume = volume(self.report, "D-1", "D1-SYS-NEW")
        self.assertEqual(sys_volume["status"], "protected")
        self.assertEqual(sys_volume["state"], "completed")
        self.assertTrue(sys_volume["completed"])
        self.assertTrue(sys_volume["fully_covered"])
        self.assertEqual(sys_volume["uncovered_ranges"], [])
        self.assertEqual(
            sys_volume["actual_range"], {"start": 0, "end": 500}
        )
        self.assertEqual(
            sorted(sys_volume["citations"]),
            ["receipt:R-NEW-SYS-1", "receipt:R-NEW-SYS-2"],
        )
        self.assertTrue(sys_volume["citable"])
        self.assertIn("record_digest", sys_volume["citable_evidence"])
        self.assertTrue(sys_volume["citable_evidence"]["statement"])

    def test_partial_volume_is_in_progress_and_leaves_exact_gap(self):
        data_volume = volume(self.report, "D-1", "D1-DATA-NEW")
        self.assertEqual(data_volume["status"], "in_progress")
        self.assertEqual(data_volume["reason"], "partial_confirmed_coverage")
        self.assertEqual(
            data_volume["covered_ranges"], [{"start": 500, "end": 900}]
        )
        self.assertEqual(
            data_volume["uncovered_ranges"], [{"start": 900, "end": 1200}]
        )
        self.assertEqual(data_volume["actual_range"], {"start": 500, "end": 1200})

    def test_late_receipt_is_excluded_at_handoff_but_recorded(self):
        data_volume = volume(self.report, "D-1", "D1-DATA-NEW")
        excluded = {item["receipt_id"]: item for item in data_volume["excluded_receipts"]}
        self.assertIn("R-NEW-DATA-LATE", excluded)
        self.assertEqual(excluded["R-NEW-DATA-LATE"]["reason"], "available_after_cutoff")
        self.assertNotIn("receipt:R-NEW-DATA-LATE", data_volume["citations"])

        d2 = volume(self.report, "D-2", "D2-DATA-NEW")
        self.assertEqual(d2["status"], "missing_receipt")
        self.assertEqual(d2["reason"], "confirmation_not_available_by_cutoff")
        # The new partition's actual range is preserved, not replaced by 0..0
        # or by an old same-name volume's range.
        self.assertEqual(d2["actual_range"], {"start": 100, "end": 300})
        self.assertEqual(d2["uncovered_ranges"], [{"start": 100, "end": 300}])
        self.assertEqual(
            d2["excluded_receipts"][0]["reason"], "available_after_cutoff"
        )

    def test_same_named_new_volume_does_not_inherit_old_receipts(self):
        new_data = volume(self.report, "D-1", "D1-DATA-NEW")
        self.assertFalse(new_data["fully_covered"])
        collision_ids = {
            item["receipt_id"] for item in new_data["same_name_other_instance_receipts"]
        }
        self.assertEqual(collision_ids, {"R-OLD-DATA-1", "R-OLD-DATA-2"})
        self.assertTrue(
            all(
                item["reason"] == "different_volume_instance_not_applied"
                for item in new_data["same_name_other_instance_receipts"]
            )
        )

    def test_old_volumes_are_reported_with_citable_receipt_basis(self):
        old_system = historical_volume(self.report, "D-1", "D1-SYS-OLD")
        old_data = historical_volume(self.report, "D-1", "D1-DATA-OLD")

        self.assertTrue(old_system["was_protected_then"])
        self.assertEqual(old_system["status"], "protected")
        self.assertEqual(old_system["citations"], ["receipt:R-OLD-SYS"])
        self.assertTrue(old_system["citable"])
        self.assertIn("R-OLD-SYS", old_system["citable_evidence"]["statement"])

        self.assertTrue(old_data["was_protected_then"])
        self.assertEqual(old_data["status"], "protected")
        self.assertEqual(
            sorted(old_data["citations"]),
            ["receipt:R-OLD-DATA-1", "receipt:R-OLD-DATA-2"],
        )
        self.assertEqual(old_data["protection_completed_at"], "2026-01-12T09:00:00Z")

        d1 = device_report(self.report, "D-1")
        self.assertEqual(d1["summary"]["citable_historical_protected_count"], 2)
        self.assertEqual(self.report["summary"]["citable_historical_protected_count"], 2)

    def test_policy_not_required_volume_is_excluded_from_required_total(self):
        scratch = volume(self.report, "D-3", "D3-SCRATCH")
        self.assertEqual(scratch["status"], "not_required")
        self.assertFalse(scratch["required"])
        self.assertEqual(self.report["summary"]["required_volume_count"], 3)

    def test_functional_positional_api_matches_document_api(self):
        result = audit_handoffs(
            devices=self.document["devices"],
            layouts=self.document["layouts"],
            receipts=self.document["receipts"],
            policies=self.document["policies"],
        )
        self.assertEqual(
            result["summary"]["status_counts"],
            self.report["summary"]["status_counts"],
        )


class CutoffBoundaryTests(unittest.TestCase):
    def _one_volume_report(self, receipt):
        devices = [{"device_id": "D", "handoff_at": "2026-01-10T00:00:00Z"}]
        layouts = [
            {
                "device_id": "D",
                "version": 1,
                "partitions": [
                    {"volume_name": "V", "volume_instance_id": "V1", "start": 0, "end": 10}
                ],
            }
        ]
        receipts = [] if receipt is None else [receipt]
        report = audit_handoffs(devices, layouts, receipts)
        return volume(report, "D", "V1")

    def test_receipt_at_exact_cutoff_is_available_and_effective(self):
        result = self._one_volume_report(
            {
                "receipt_id": "R-EXACT",
                "device_id": "D",
                "volume_instance_id": "V1",
                "start": 0,
                "end": 10,
                "effective_at": "2026-01-10T00:00:00Z",
                "received_at": "2026-01-10T00:00:00Z",
            }
        )
        self.assertEqual(result["status"], "protected")

    def test_availability_one_second_after_cutoff_is_excluded(self):
        result = self._one_volume_report(
            {
                "receipt_id": "R-LATE",
                "device_id": "D",
                "volume_instance_id": "V1",
                "start": 0,
                "end": 10,
                "effective_at": "2026-01-09T00:00:00Z",
                "received_at": "2026-01-10T00:00:01Z",
            }
        )
        self.assertEqual(result["status"], "missing_receipt")
        self.assertEqual(result["reason"], "confirmation_not_available_by_cutoff")

    def test_future_effective_time_is_excluded_even_if_received_early(self):
        result = self._one_volume_report(
            {
                "receipt_id": "R-FUTURE",
                "device_id": "D",
                "volume_instance_id": "V1",
                "start": 0,
                "end": 10,
                "effective_at": "2026-01-10T00:00:01Z",
                "received_at": "2026-01-01T00:00:00Z",
            }
        )
        self.assertEqual(result["status"], "missing_receipt")
        self.assertEqual(result["reason"], "receipt_not_effective_by_cutoff")

    def test_timezone_equivalent_cutoff_is_normalized(self):
        devices = [{"device_id": "D", "handoff_at": "2026-01-10T02:00:00+02:00"}]
        layouts = [
            {
                "device_id": "D",
                "version": 1,
                "partitions": [
                    {"volume_name": "V", "volume_instance_id": "V1", "start": 0, "end": 10}
                ],
            }
        ]
        receipts = [
            {
                "receipt_id": "R-TZ",
                "device_id": "D",
                "volume_instance_id": "V1",
                "start": 0,
                "end": 10,
                "effective_at": "2026-01-10T00:00:00+00:00",
            }
        ]
        report = audit_handoffs(devices, layouts, receipts)
        self.assertEqual(volume(report, "D", "V1")["status"], "protected")


class LayoutAndIdentityInterleavingTests(unittest.TestCase):
    def test_explicit_layout_version_uses_repartitioned_scope(self):
        devices = [{"device_id": "D", "handoff_at": "2026-02-10T00:00:00Z", "layout_version": 2}]
        layouts = [
            {"device_id": "D", "version": 1, "partitions": [
                {"volume_name": "Data", "volume_instance_id": "OLD", "start": 0, "end": 100}
            ]},
            {"device_id": "D", "version": 2, "partitions": [
                {"volume_name": "Data", "volume_instance_id": "NEW", "start": 0, "end": 200}
            ]},
        ]
        receipts = [
            {"receipt_id": "R-OLD", "device_id": "D", "volume_instance_id": "OLD",
             "start": 0, "end": 100, "effective_at": "2026-01-01T00:00:00Z"},
            {"receipt_id": "R-NEW-PART", "device_id": "D", "volume_instance_id": "NEW",
             "start": 0, "end": 100, "effective_at": "2026-02-01T00:00:00Z"},
        ]
        report = audit_handoffs(devices, layouts, receipts)
        new_volume = volume(report, "D", "NEW")
        self.assertEqual(new_volume["status"], "in_progress")
        self.assertEqual(new_volume["actual_range"], {"start": 0, "end": 200})
        self.assertEqual(new_volume["uncovered_ranges"], [{"start": 100, "end": 200}])

        old_volume = historical_volume(report, "D", "OLD")
        self.assertEqual(old_volume["status"], "protected")
        self.assertEqual(old_volume["citations"], ["receipt:R-OLD"])

    def test_active_layout_is_latest_valid_at_handoff_including_equal_boundary(self):
        devices = [{"device_id": "D", "handoff_at": "2026-02-01T00:00:00Z"}]
        layouts = [
            {"device_id": "D", "version": 1, "valid_from": "2026-01-01T00:00:00Z",
             "partitions": [{"volume_name": "V", "volume_instance_id": "V1", "start": 0, "end": 1}]},
            {"device_id": "D", "version": 2, "valid_from": "2026-02-01T00:00:00Z",
             "partitions": [{"volume_name": "V", "volume_instance_id": "V2", "start": 0, "end": 2}]},
        ]
        report = audit_handoffs(devices, layouts, [])
        d = device_report(report, "D")
        self.assertEqual(d["layout_version"], 2)
        self.assertEqual(d["volumes"][0]["volume_instance_id"], "V2")

    def test_receipt_for_other_device_is_not_applied(self):
        devices = [{"device_id": "D1", "handoff_at": "2026-01-01T00:00:00Z"}]
        layouts = [{"device_id": "D1", "version": 1, "partitions": [
            {"volume_name": "V", "volume_instance_id": "SHARED-TEXT-ID", "start": 0, "end": 10}
        ]}]
        receipts = [{
            "receipt_id": "R-OTHER-DEVICE",
            "device_id": "D2",
            "volume_instance_id": "SHARED-TEXT-ID",
            "start": 0,
            "end": 10,
            "effective_at": "2025-12-31T00:00:00Z",
        }]
        report = audit_handoffs(devices, layouts, receipts)
        result = volume(report, "D1", "SHARED-TEXT-ID")
        self.assertEqual(result["status"], "missing_receipt")
        self.assertEqual(result["excluded_receipts"][0]["reason"], "device_mismatch")


class ValidationAndPolicyTests(unittest.TestCase):
    def test_strict_mode_raises_on_missing_instance_identifier(self):
        document = build_scenario()
        document["layouts"].append({
            "device_id": "D-9",
            "version": 1,
            "partitions": [{"volume_name": "Broken", "start": 0, "end": 10}],
        })
        document["devices"].append({"device_id": "D-9", "handoff_at": "2026-01-01T00:00:00Z"})
        with self.assertRaises(AuditDataError):
            audit_document(document, strict=True)

    def test_non_strict_report_contains_validation_issue(self):
        devices = [{"device_id": "D", "handoff_at": "2026-01-01T00:00:00Z"}]
        layouts = [{"device_id": "D", "version": 1, "partitions": [
            {"volume_name": "Broken", "start": 10, "end": 1}
        ]}]
        report = audit_handoffs(devices, layouts, [], strict=False)
        self.assertTrue(report["validation_issues"])
        broken = report["devices"][0]["volumes"][0]
        self.assertEqual(broken["status"], "invalid_scope")
        self.assertIsNone(broken["actual_range"])

    def test_default_required_false_makes_unlisted_volume_not_required(self):
        devices = [{"device_id": "D", "handoff_at": "2026-01-01T00:00:00Z"}]
        layouts = [{"device_id": "D", "version": 1, "partitions": [
            {"volume_name": "V", "volume_instance_id": "V1", "start": 0, "end": 10}
        ]}]
        report = audit_handoffs(devices, layouts, [], default_required=False)
        self.assertEqual(volume(report, "D", "V1")["status"], "not_required")


if __name__ == "__main__":
    unittest.main()
