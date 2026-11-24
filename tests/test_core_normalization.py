import unittest

from receipt_normalizer import (
    ActionRecord,
    ActionMapping,
    ImageSwitch,
    ObjectSpec,
    ReceiptNormalizer,
    TableVersion,
)


def v1_table():
    return {
        "version": "decode-v1",
        "valid_from_seq": 1,
        "mappings": {
            "native": {
                100: {
                    "name": "file.read",
                    "target": {"kind": "file_descriptor", "identifier": 0},
                },
                200: {
                    "name": "process.signal",
                    "target": {
                        "kind": "process",
                        "identifier": 0,
                        "fields": {"signal": 1},
                    },
                },
                500: {
                    "name": "ipc.entry",
                    "multiplex": {
                        "selector_param": 0,
                        "operations": {
                            1: {
                                "name": "semaphore.acquire",
                                "target": {"kind": "semaphore", "identifier": 1},
                            },
                            2: {
                                "name": "shared_memory.attach",
                                "target": {
                                    "kind": "shared_memory",
                                    "identifier": 1,
                                    "fields": {"flags": 2},
                                },
                            },
                        },
                    },
                },
            },
            "compat": {
                100: {
                    "name": "file.write",
                    "target": {"kind": "file_descriptor", "identifier": 0},
                },
                500: {
                    "name": "compat.ipc.entry",
                    "multiplex": {
                        "selector_param": 2,
                        "operations": {
                            7: {
                                "name": "compat.device.control",
                                "target": {"kind": "device", "identifier": 0},
                            }
                        },
                    },
                },
            },
        },
    }


def base_switches():
    return [
        ImageSwitch("p-native", 0, "/usr/bin/native-app", "native"),
        ImageSwitch("p-compat", 0, "/usr/bin/compat-app", "compat"),
    ]


class NativeCompatDecodingTests(unittest.TestCase):
    def setUp(self):
        self.normalizer = ReceiptNormalizer(
            tables=[v1_table()], image_switches=base_switches()
        )

    def test_same_code_is_decoded_by_active_abi(self):
        native = self.normalizer.normalize(
            ActionRecord("p-native", 1, [3], action_code=100)
        )
        compat = self.normalizer.normalize(
            ActionRecord("p-compat", 2, [4], action_code=100)
        )

        self.assertTrue(native.is_complete, native.to_dict())
        self.assertTrue(compat.is_complete, compat.to_dict())

        self.assertEqual(native.process.abi, "native")
        self.assertEqual(native.process.image, "/usr/bin/native-app")
        self.assertEqual(native.action_name, "file.read")
        self.assertEqual(native.target.kind, "file_descriptor")
        self.assertEqual(native.target.identifier, 3)
        self.assertEqual(native.table_version, "decode-v1")

        self.assertEqual(compat.process.abi, "compat")
        self.assertEqual(compat.action_name, "file.write")
        self.assertEqual(compat.target.identifier, 4)

    def test_additional_target_fields_are_extracted(self):
        receipt = self.normalizer.normalize(
            ActionRecord("p-native", 3, [42, 15], action_code=200)
        )
        self.assertTrue(receipt.is_complete, receipt.to_dict())
        self.assertEqual(receipt.action_name, "process.signal")
        self.assertEqual(receipt.target.kind, "process")
        self.assertEqual(receipt.target.identifier, 42)
        self.assertEqual(receipt.target.attributes, {"signal": 15})

    def test_multiplex_selector_uses_mapping_parameter(self):
        acquire = self.normalizer.normalize(
            ActionRecord("p-native", 4, [1, 77], action_code=500)
        )
        attach = self.normalizer.normalize(
            ActionRecord("p-native", 5, [2, 88, 0o600], action_code=500)
        )

        self.assertTrue(acquire.is_complete, acquire.to_dict())
        self.assertEqual(acquire.entry_name, "ipc.entry")
        self.assertEqual(acquire.action_name, "semaphore.acquire")
        self.assertEqual(acquire.selector_value, 1)
        self.assertEqual(acquire.target.kind, "semaphore")
        self.assertEqual(acquire.target.identifier, 77)

        self.assertTrue(attach.is_complete, attach.to_dict())
        self.assertEqual(attach.action_name, "shared_memory.attach")
        self.assertEqual(attach.target.identifier, 88)
        self.assertEqual(attach.target.attributes["flags"], 0o600)

    def test_compat_multiplex_selector_position_can_differ(self):
        receipt = self.normalizer.normalize(
            ActionRecord("p-compat", 6, ["/dev/synthetic", 0, 7], action_code=500)
        )
        self.assertTrue(receipt.is_complete, receipt.to_dict())
        self.assertEqual(receipt.action_name, "compat.device.control")
        self.assertEqual(receipt.selector_value, 7)
        self.assertEqual(receipt.target.kind, "device")
        self.assertEqual(receipt.target.identifier, "/dev/synthetic")


class ImageAndTablePositionTests(unittest.TestCase):
    def test_image_switch_at_source_position_selects_new_abi(self):
        # Deliberately supplied out of chronological order.
        switches = [
            ImageSwitch("p-exec", 10, "/usr/bin/compat-worker", "compat"),
            ImageSwitch("p-exec", 0, "/usr/bin/launcher", "native"),
        ]
        normalizer = ReceiptNormalizer(tables=[v1_table()], image_switches=switches)

        before = normalizer.normalize(
            ActionRecord("p-exec", 9, [11], action_code=100)
        )
        boundary = normalizer.normalize(
            ActionRecord("p-exec", 10, [12], action_code=100)
        )
        after = normalizer.normalize(
            ActionRecord("p-exec", 11, [13], action_code=100)
        )

        self.assertEqual(before.action_name, "file.read")
        self.assertEqual(before.process.image, "/usr/bin/launcher")
        self.assertEqual(before.process.evidence_seq, 0)

        # at_seq is documented as the first inclusive source position.
        self.assertEqual(boundary.action_name, "file.write")
        self.assertEqual(boundary.process.abi, "compat")
        self.assertEqual(boundary.process.image, "/usr/bin/compat-worker")
        self.assertEqual(boundary.process.evidence_seq, 10)

        self.assertEqual(after.action_name, "file.write")
        self.assertTrue(all(r.is_complete for r in (before, boundary, after)))

    def test_partial_switch_evidence_carries_forward_other_fields(self):
        switches = [
            ImageSwitch("p-partial", 0, "/initial-image", "native"),
            # This evidence only reports the ABI transition.
            ImageSwitch("p-partial", 8, abi="compat"),
        ]
        normalizer = ReceiptNormalizer(tables=[v1_table()], image_switches=switches)
        receipt = normalizer.normalize(
            ActionRecord("p-partial", 9, [2], action_code=100)
        )
        self.assertTrue(receipt.is_complete, receipt.to_dict())
        self.assertEqual(receipt.process.image, "/initial-image")
        self.assertEqual(receipt.process.abi, "compat")
        self.assertEqual(receipt.action_name, "file.write")

    def test_table_version_is_selected_by_action_source_seq(self):
        tables = [
            {
                "version": "decode-v2",
                "valid_from_seq": 10,
                "mappings": {"native": {700: "modern.action"}},
            },
            {
                "version": "decode-v1",
                "valid_from_seq": 1,
                "mappings": {"native": {700: "legacy.action"}},
            },
        ]
        switches = [ImageSwitch("p", 0, "/img", "native")]
        normalizer = ReceiptNormalizer(tables=tables, image_switches=switches)

        old = normalizer.normalize(ActionRecord("p", 9, [], action_code=700))
        new_boundary = normalizer.normalize(ActionRecord("p", 10, [], action_code=700))
        new_later = normalizer.normalize(ActionRecord("p", 30, [], action_code=700))

        self.assertEqual(old.action_name, "legacy.action")
        self.assertEqual(old.table_version, "decode-v1")
        self.assertEqual(new_boundary.action_name, "modern.action")
        self.assertEqual(new_boundary.table_version, "decode-v2")
        self.assertEqual(new_later.action_name, "modern.action")

    def test_typed_table_api(self):
        table = TableVersion(
            version="typed",
            valid_from_seq=0,
            mappings={
                "native": {
                    1: ActionMapping(
                        name="typed.action",
                        target=ObjectSpec(kind="thing", identifier="id"),
                    )
                }
            },
        )
        normalizer = ReceiptNormalizer(
            tables=[table],
            image_switches=[ImageSwitch("p", 0, "/img", "native")],
        )
        receipt = normalizer.normalize(
            ActionRecord("p", 1, {"id": "object-1"}, action_code=1)
        )
        self.assertTrue(receipt.is_complete, receipt.to_dict())
        self.assertEqual(receipt.action_name, "typed.action")
        self.assertEqual(receipt.target.identifier, "object-1")

    def test_nested_parameter_paths_and_wildcard_mapping(self):
        table = TableVersion(
            version="paths",
            valid_from_seq=0,
            mappings={
                "*": {
                    7: ActionMapping(
                        name="common.action",
                        target=ObjectSpec(
                            kind="nested-thing",
                            identifier=("payload", "id"),
                            fields={"first_item": ("items", 0)},
                        ),
                    )
                }
            },
        )
        normalizer = ReceiptNormalizer(tables=[table])

        known_process = normalizer.normalize(
            ActionRecord(
                "known",
                2,
                {"payload": {"id": "obj-9"}, "items": ["first"]},
                action_code=7,
            )
        )
        self.assertEqual(known_process.action_name, "common.action")
        self.assertEqual(known_process.target.identifier, "obj-9")
        self.assertEqual(known_process.target.attributes, {"first_item": "first"})

        # Wildcard is explicitly ABI-independent, so it can still be decoded
        # when process evidence is missing; the result remains a review gap.
        unknown_process = normalizer.normalize(
            ActionRecord(
                "missing",
                2,
                {"payload": {"id": "obj-10"}, "items": ["x"]},
                action_code=7,
            )
        )
        self.assertEqual(unknown_process.action_name, "common.action")
        self.assertTrue(unknown_process.needs_review)
        self.assertIn("process_evidence_missing", unknown_process.gap_codes())

    def test_json_decoded_numeric_string_code_keys_are_supported(self):
        import json

        table = {
            "version": "json-table",
            "valid_from_seq": 1,
            "mappings": {
                "native": {
                    "100": {
                        "name": "json.key.action",
                        "target": {"kind": "fd", "identifier": 0},
                    }
                }
            },
        }
        decoded_table = json.loads(json.dumps([table]))
        normalizer = ReceiptNormalizer(
            tables=decoded_table,
            image_switches=[
                {
                    "process_instance": "p",
                    "source_seq": 0,
                    "image": "/synthetic/image",
                    "abi": "native",
                }
            ],
        )
        receipt = normalizer.normalize(
            {"process_instance": "p", "source_seq": 2, "action_code": 100, "params": [8]}
        )
        self.assertTrue(receipt.is_complete, receipt.to_dict())
        self.assertEqual(receipt.action_name, "json.key.action")
        self.assertEqual(receipt.target.identifier, 8)


if __name__ == "__main__":
    unittest.main()
