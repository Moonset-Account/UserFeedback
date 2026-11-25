import unittest

from volume_audit import audit_handoffs, evaluate_handoffs


class InputShapeAndSignalTests(unittest.TestCase):
    def test_camel_case_and_flat_boundary_rows_and_nested_ranges(self):
        devices = [{"deviceId": "D", "handoffAt": "2026-01-10T00:00:00Z"}]
        boundaries = [
            {
                "deviceId": "D",
                "layoutVersion": 7,
                "volumeName": "Data",
                "volumeInstanceNumber": "VOL-7",
                "startOffset": 100,
                "endOffset": 300,
            }
        ]
        receipts = [
            {
                "receiptId": "R-CAMEL",
                "deviceId": "D",
                "volumeInstanceNumber": "VOL-7",
                "status": "success",
                "effectiveAt": "2026-01-09T00:00:00Z",
                "encryptedRanges": [{"startOffset": 100, "length": 200}],
            }
        ]
        report = audit_handoffs(devices, boundaries, receipts)
        volume = report["devices"][0]["volumes"][0]
        self.assertEqual(volume["volume_instance_id"], "VOL-7")
        self.assertEqual(volume["status"], "protected")
        self.assertEqual(volume["actual_range"], {"start": 100, "end": 300})

    def test_scalar_boundaries_align_with_named_volumes(self):
        devices = [{"device_id": "D", "handoff_at": "2026-01-10T00:00:00Z"}]
        layouts = [
            {
                "device_id": "D",
                "version": 1,
                "volumes": [
                    {"volume_name": "EFI", "volume_instance_id": "EFI1"},
                    {"volume_name": "Data", "volume_instance_id": "DATA1"},
                ],
                "boundaries": [0, 50, 200],
            }
        ]
        report = audit_handoffs(devices, layouts, [])
        ranges = [v["actual_range"] for v in report["devices"][0]["volumes"]]
        self.assertEqual(ranges, [{"start": 0, "end": 50}, {"start": 50, "end": 200}])

    def test_explicit_agent_encrypting_signal_is_in_progress_without_interval(self):
        devices = [{"device_id": "D", "handoff_at": "2026-01-10T00:00:00Z"}]
        layouts = [{"device_id": "D", "version": 1, "partitions": [
            {"volume_name": "V", "volume_instance_id": "V1", "start": 0, "end": 10}
        ]}]
        receipts = [{
            "receipt_id": "R-PENDING",
            "device_id": "D",
            "volume_instance_id": "V1",
            "status": "encrypting",
        }]
        report = audit_handoffs(devices, layouts, receipts)
        volume = report["devices"][0]["volumes"][0]
        self.assertEqual(volume["status"], "in_progress")
        self.assertEqual(volume["reason"], "agent_reported_in_progress_without_confirmed_interval")
        self.assertEqual(volume["uncovered_ranges"], [{"start": 0, "end": 10}])

    def test_historical_partial_volume_is_not_marked_citable_protected(self):
        devices = [{"device_id": "D", "handoff_at": "2026-03-01T00:00:00Z", "layout_version": 2}]
        layouts = [
            {"device_id": "D", "version": 1, "valid_from": "2026-01-01T00:00:00Z",
             "valid_to": "2026-02-01T00:00:00Z",
             "partitions": [{"volume_name": "V", "volume_instance_id": "OLD", "start": 0, "end": 100}]},
            {"device_id": "D", "version": 2, "valid_from": "2026-02-01T00:00:00Z",
             "partitions": [{"volume_name": "V", "volume_instance_id": "NEW", "start": 0, "end": 100}]},
        ]
        receipts = [{
            "receipt_id": "R-OLD-PART",
            "device_id": "D",
            "volume_instance_id": "OLD",
            "start": 0,
            "end": 40,
            "effective_at": "2026-01-20T00:00:00Z",
        }]
        report = audit_handoffs(devices, layouts, receipts)
        old = report["devices"][0]["historical_volumes"][0]
        self.assertEqual(old["status"], "in_progress")
        self.assertFalse(old["was_protected_then"])
        self.assertFalse(old["citable"])
        self.assertEqual(old["uncovered_ranges"], [{"start": 40, "end": 100}])

    def test_alias_keyword_entry_point(self):
        report = evaluate_handoffs(
            device_registry=[{"device_id": "D", "handoff_at": "2026-01-01T00:00:00Z"}],
            partition_boundaries=[
                {"device_id": "D", "layout_version": 1, "volume_instance_id": "V", "start": 0, "end": 10}
            ],
            encryption_receipts=[
                {
                    "receipt_id": "R",
                    "device_id": "D",
                    "volume_instance_id": "V",
                    "start": 0,
                    "end": 10,
                    "effective_at": "2025-12-31T00:00:00Z",
                }
            ],
        )
        self.assertEqual(report["summary"]["protected_count"], 1)

    def test_numeric_logical_timestamps_preserve_ordering(self):
        devices = [{"device_id": "D", "handoff_at": 200}]
        layouts = [{"device_id": "D", "version": 1, "partitions": [
            {"volume_name": "V", "volume_instance_id": "V1", "start": 0, "end": 10}
        ]}]
        receipts = [{
            "receipt_id": "R-LOGICAL",
            "device_id": "D",
            "volume_instance_id": "V1",
            "start": 0,
            "end": 10,
            "effective_at": 100,
            "available_at": 150,
        }]
        report = audit_handoffs(devices, layouts, receipts)
        self.assertEqual(report["devices"][0]["volumes"][0]["status"], "protected")


if __name__ == "__main__":
    unittest.main()
