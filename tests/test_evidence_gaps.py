import json
import unittest

from receipt_normalizer import GapCode, ReceiptNormalizer


def table():
    return {
        "version": "gap-table-v1",
        "valid_from_seq": 1,
        "mappings": {
            "native": {
                100: {
                    "name": "file.read",
                    "target": {"kind": "fd", "identifier": 0},
                },
                500: {
                    "name": "mux.entry",
                    "multiplex": {
                        "selector_param": 0,
                        "operations": {
                            1: {
                                "name": "sub.one",
                                "target": {"kind": "thing", "identifier": 1},
                            }
                        },
                    },
                },
            },
            "compat": {
                100: "compat.file.read",
            },
        },
    }


def switches():
    return [
        {"process_instance": "p", "source_seq": 0, "image": "/native", "abi": "native"}
    ]


class EvidenceGapTests(unittest.TestCase):
    def setUp(self):
        self.normalizer = ReceiptNormalizer(
            tables=[table()], image_switches=switches()
        )

    def test_unknown_process_emits_process_gap(self):
        receipt = self.normalizer.normalize(
            {"process_instance": "stranger", "source_seq": 2, "action_code": 100}
        )
        self.assertEqual(receipt.status, "evidence_gap")
        self.assertIn(GapCode.PROCESS_EVIDENCE_MISSING, receipt.gap_codes())
        self.assertIsNone(receipt.action_name)
        self.assertEqual(receipt.table_version, "gap-table-v1")

    def test_action_before_first_image_state_is_gap(self):
        normalizer = ReceiptNormalizer(
            tables=[table()],
            image_switches=[
                {"process_instance": "late", "source_seq": 5, "image": "/img", "abi": "native"}
            ],
        )
        receipt = normalizer.normalize(
            {"process_instance": "late", "source_seq": 3, "action_code": 100}
        )
        self.assertIn(GapCode.IMAGE_UNKNOWN, receipt.gap_codes())
        self.assertIsNone(receipt.process.abi)

    def test_conflicting_images_remain_review_gap_but_abi_can_be_used(self):
        normalizer = ReceiptNormalizer(
            tables=[table()],
            image_switches=[
                ("p", 5, "/image-a", "native"),
                ("p", 5, "/image-b", "native"),
            ],
        )
        receipt = normalizer.normalize(
            {"process_instance": "p", "source_seq": 6, "action_code": 100, "params": [9]}
        )
        codes = receipt.gap_codes()
        self.assertIn(GapCode.IMAGE_CONFLICT, codes)
        self.assertNotIn(GapCode.ABI_CONFLICT, codes)
        self.assertIsNone(receipt.process.image)
        self.assertEqual(receipt.process.abi, "native")
        self.assertEqual(receipt.action_name, "file.read")
        self.assertTrue(receipt.needs_review)

    def test_conflicting_abis_prevent_unsafe_choice(self):
        normalizer = ReceiptNormalizer(
            tables=[table()],
            image_switches=[
                {"process_instance": "p", "source_seq": 5, "image": "/same", "abi": "native"},
                {"process_instance": "p", "source_seq": 5, "image": "/same", "abi": "compat"},
            ],
        )
        receipt = normalizer.normalize(
            {"process_instance": "p", "source_seq": 6, "action_code": 100}
        )
        self.assertIn(GapCode.ABI_CONFLICT, receipt.gap_codes())
        self.assertIsNone(receipt.process.abi)
        self.assertIsNone(receipt.action_name)

    def test_explicit_same_position_order_resolves_switch(self):
        normalizer = ReceiptNormalizer(
            tables=[table()],
            image_switches=[
                {"process_instance": "p", "source_seq": 5, "order": 0, "image": "/old", "abi": "native"},
                {"process_instance": "p", "source_seq": 5, "order": 1, "image": "/new", "abi": "compat"},
            ],
        )
        receipt = normalizer.normalize(
            {"process_instance": "p", "source_seq": 5, "action_code": 100}
        )
        self.assertTrue(receipt.is_complete, receipt.to_dict())
        self.assertEqual(receipt.process.image, "/new")
        self.assertEqual(receipt.process.abi, "compat")
        self.assertEqual(receipt.action_name, "compat.file.read")

    def test_missing_table_coverage_is_gap(self):
        old = table()
        old["valid_to_seq"] = 4
        normalizer = ReceiptNormalizer(
            tables=[old], image_switches=switches()
        )
        receipt = normalizer.normalize(
            {"process_instance": "p", "source_seq": 8, "action_code": 100}
        )
        self.assertIn(GapCode.TABLE_VERSION_UNKNOWN, receipt.gap_codes())
        self.assertIsNone(receipt.table_version)
        self.assertIsNone(receipt.action_name)

    def test_overlapping_table_ranges_are_ambiguous(self):
        first = table()
        first["version"] = "overlap-a"
        first["valid_from_seq"] = 1
        first["valid_to_seq"] = 8
        second = {
            "version": "overlap-b",
            "valid_from_seq": 5,
            "mappings": {"native": {100: "other.action"}},
        }
        normalizer = ReceiptNormalizer(
            tables=[first, second], image_switches=switches()
        )
        receipt = normalizer.normalize(
            {"process_instance": "p", "source_seq": 6, "action_code": 100}
        )
        self.assertIn(GapCode.TABLE_VERSION_CONFLICT, receipt.gap_codes())
        self.assertIsNone(receipt.table_version)
        self.assertIsNone(receipt.action_name)

    def test_same_table_start_conflict_is_gap(self):
        normalizer = ReceiptNormalizer(
            tables=[
                {"version": "a", "valid_from_seq": 1, "mappings": {}},
                {"version": "b", "valid_from_seq": 1, "mappings": {}},
            ],
            image_switches=switches(),
        )
        receipt = normalizer.normalize(
            {"process_instance": "p", "source_seq": 2, "action_code": 100}
        )
        self.assertIn(GapCode.TABLE_VERSION_CONFLICT, receipt.gap_codes())

    def test_unknown_table_label_is_review_gap_even_if_mapping_exists(self):
        unversioned = table()
        unversioned["version"] = "unknown"
        normalizer = ReceiptNormalizer(
            tables=[unversioned], image_switches=switches()
        )
        receipt = normalizer.normalize(
            {"process_instance": "p", "source_seq": 2, "action_code": 100, "params": [3]}
        )
        self.assertIn(GapCode.TABLE_VERSION_UNKNOWN, receipt.gap_codes())
        self.assertEqual(receipt.action_name, "file.read")
        self.assertEqual(receipt.target.identifier, 3)
        self.assertTrue(receipt.needs_review)

    def test_serialized_result_has_action_and_object_aliases(self):
        receipt = self.normalizer.normalize(
            {"process_instance": "p", "source_seq": 2, "action_code": 100, "params": [5]}
        )
        data = receipt.to_dict()
        self.assertEqual(data["action"]["name"], "file.read")
        self.assertEqual(data["action"]["permission_action"], "file.read")
        self.assertEqual(data["permission_action"], "file.read")
        self.assertEqual(data["object"]["identifier"], 5)
        self.assertIs(data["object"], data["target"])

    def test_unknown_action_code_is_gap(self):
        receipt = self.normalizer.normalize(
            {"process_instance": "p", "source_seq": 2, "action_code": 999}
        )
        self.assertIn(GapCode.ACTION_UNMAPPED, receipt.gap_codes())
        self.assertIsNone(receipt.action_name)

    def test_missing_multiplex_selector_is_gap_but_entry_is_known(self):
        receipt = self.normalizer.normalize(
            {"process_instance": "p", "source_seq": 2, "action_code": 500, "params": []}
        )
        self.assertIn(GapCode.SELECTOR_MISSING, receipt.gap_codes())
        self.assertEqual(receipt.entry_name, "mux.entry")
        self.assertIsNone(receipt.action_name)

    def test_unknown_suboperation_is_gap(self):
        receipt = self.normalizer.normalize(
            {"process_instance": "p", "source_seq": 2, "action_code": 500, "params": [42]}
        )
        self.assertIn(GapCode.SUBOPERATION_UNKNOWN, receipt.gap_codes())
        self.assertEqual(receipt.selector_value, 42)
        self.assertIsNone(receipt.action_name)

    def test_missing_target_parameter_keeps_action_but_flags_gap(self):
        receipt = self.normalizer.normalize(
            {"process_instance": "p", "source_seq": 2, "action_code": 500, "params": [1]}
        )
        self.assertIn(GapCode.TARGET_PARAMETER_MISSING, receipt.gap_codes())
        self.assertEqual(receipt.action_name, "sub.one")
        self.assertEqual(receipt.target.kind, "thing")
        self.assertIsNone(receipt.target.identifier)

    def test_missing_action_code_without_selector_is_gap(self):
        receipt = self.normalizer.normalize(
            {"process_instance": "p", "source_seq": 2, "params": []}
        )
        self.assertIn(GapCode.ACTION_CODE_MISSING, receipt.gap_codes())

    def test_action_code_can_be_recovered_from_named_params(self):
        coded_table = {
            "version": "embedded-code",
            "valid_from_seq": 1,
            "code_selector": "entry",
            "mappings": {
                "native": {
                    100: {
                        "name": "named.decode",
                        "target": {"kind": "thing", "identifier": "object_id"},
                    }
                }
            },
        }
        normalizer = ReceiptNormalizer(
            tables=[coded_table], image_switches=switches()
        )
        receipt = normalizer.normalize(
            {
                "process_instance": "p",
                "source_seq": 2,
                "params": {"entry": 100, "object_id": "obj-7"},
            }
        )
        self.assertTrue(receipt.is_complete, receipt.to_dict())
        self.assertEqual(receipt.action_code, 100)
        self.assertEqual(receipt.action_name, "named.decode")
        self.assertEqual(receipt.target.identifier, "obj-7")

    def test_result_is_json_serializable_for_rule_review(self):
        receipt = self.normalizer.normalize(
            {"process_instance": "p", "source_seq": 2, "action_code": 999}
        )
        encoded = json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["status"], "evidence_gap")
        self.assertTrue(decoded["evidence_gaps"])


if __name__ == "__main__":
    unittest.main()
