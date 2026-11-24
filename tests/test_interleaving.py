import unittest

from receipt_normalizer import (
    ActionRecord,
    ImageSwitch,
    ReceiptNormalizer,
    normalize_receipts,
)


def interleaved_tables():
    return [
        {
            "version": "t1",
            "valid_from_seq": 1,
            "mappings": {
                "native": {
                    100: "native.action.100",
                    200: {
                        "name": "native.multiplex",
                        "selector_param": 0,
                        "operations": {1: "native.sub.one", 2: "native.sub.two"},
                    },
                },
                "compat": {
                    100: "compat.action.100",
                    200: {
                        "name": "compat.multiplex",
                        "selector_param": 1,
                        "operations": {1: "compat.sub.from-second-param"},
                    },
                },
            },
        },
        {
            "version": "t2",
            "valid_from_seq": 10,
            "mappings": {
                "native": {
                    100: "native.action.100.table2",
                    200: "native.only-in-table2",
                },
                "compat": {
                    100: "compat.action.100.table2",
                    200: "compat.only-in-table2",
                },
            },
        },
    ]


class InterleavingTests(unittest.TestCase):
    def test_concurrent_native_compat_and_version_boundaries(self):
        switches = [
            # Arrival order is deliberately not source-ordered.
            ImageSwitch("p-native", 5, "/new-native-after-exec", "native"),
            ImageSwitch("p-compat", 0, "/compat", "compat"),
            ImageSwitch("p-native", 0, "/native", "native"),
            ImageSwitch("p-native", 5, "/new-native-after-exec", "native"),
        ]
        records = [
            ActionRecord("p-native", 1, [1, 1], action_code=200),
            ActionRecord("p-compat", 2, [99, 1], action_code=200),
            ActionRecord("p-native", 3, [], action_code=100),
            ActionRecord("p-compat", 4, [], action_code=100),
            # Two agreeing image records at seq 5 are duplicates in effect.
            ActionRecord("p-native", 5, [], action_code=100),
            ActionRecord("p-compat", 6, [], action_code=100),
            ActionRecord("p-native", 9, [], action_code=100),
            ActionRecord("p-compat", 10, [], action_code=100),
            ActionRecord("p-native", 11, [], action_code=100),
        ]

        results = normalize_receipts(
            records, tables=interleaved_tables(), image_switches=switches
        )
        by_seq = {r.source_seq: r for r in results}

        self.assertEqual(by_seq[1].action_name, "native.sub.one")
        self.assertEqual(by_seq[2].action_name, "compat.sub.from-second-param")
        self.assertEqual(by_seq[3].action_name, "native.action.100")
        self.assertEqual(by_seq[4].action_name, "compat.action.100")

        self.assertTrue(by_seq[5].is_complete, by_seq[5].to_dict())
        self.assertEqual(by_seq[5].process.image, "/new-native-after-exec")
        self.assertEqual(by_seq[5].action_name, "native.action.100")
        self.assertEqual(by_seq[6].action_name, "compat.action.100")
        self.assertEqual(by_seq[9].table_version, "t1")
        self.assertEqual(by_seq[9].action_name, "native.action.100")

        self.assertEqual(by_seq[10].table_version, "t2")
        self.assertEqual(by_seq[10].action_name, "compat.action.100.table2")
        self.assertEqual(by_seq[11].action_name, "native.action.100.table2")
        self.assertTrue(all(r.is_complete for r in results), [r.to_dict() for r in results if r.gaps])

    def test_batch_output_preserves_input_order(self):
        switches = [ImageSwitch("p", 0, "/img", "native")]
        tables = [
            {"version": "t", "valid_from_seq": 1, "mappings": {"native": {1: "a"}}}
        ]
        records = [
            {"process_instance": "p", "source_seq": 3, "action_code": 1},
            {"process_instance": "p", "source_seq": 1, "action_code": 1},
            {"process_instance": "p", "source_seq": 2, "action_code": 1},
        ]
        results = normalize_receipts(records, tables=tables, image_switches=switches)
        self.assertEqual([r.source_seq for r in results], [3, 1, 2])

    def test_image_evidence_can_arrive_after_provisional_gap(self):
        normalizer = ReceiptNormalizer(tables=interleaved_tables())
        record = ActionRecord("late-process", 2, [], action_code=100)

        provisional = normalizer.normalize(record)
        self.assertIn("process_evidence_missing", provisional.gap_codes())

        normalizer.add_image_switch(
            ImageSwitch("late-process", 0, "/img", "native")
        )
        reconciled = normalizer.normalize(record)
        self.assertTrue(reconciled.is_complete, reconciled.to_dict())
        self.assertEqual(reconciled.action_name, "native.action.100")

    def test_table_evidence_can_arrive_after_provisional_gap(self):
        normalizer = ReceiptNormalizer(
            image_switches=[ImageSwitch("p", 0, "/img", "native")]
        )
        record = {"process_instance": "p", "source_seq": 2, "action_code": 100}

        provisional = normalizer.normalize(record)
        self.assertIn("table_version_unknown", provisional.gap_codes())

        for table in interleaved_tables():
            normalizer.add_table_version(table)
        reconciled = normalizer.normalize(record)
        self.assertTrue(reconciled.is_complete, reconciled.to_dict())
        self.assertEqual(reconciled.action_name, "native.action.100")

    def test_tuple_record_shape_with_explicit_code(self):
        switches = [ImageSwitch("p", 0, "/img", "native")]
        tables = [
            {
                "version": "tuple",
                "valid_from_seq": 1,
                "mappings": {"native": {7: "tuple.action"}},
            }
        ]
        # (process, seq, code, params) is accepted in addition to dataclasses.
        results = normalize_receipts(
            [("p", 4, 7, [])], tables=tables, image_switches=switches
        )
        self.assertEqual(results[0].action_name, "tuple.action")
        self.assertTrue(results[0].is_complete)

    def test_process_instance_prevents_pid_reuse_confusion(self):
        old_instance = ("pid-100", "process-start-a")
        new_instance = ("pid-100", "process-start-b")
        switches = [
            ImageSwitch(old_instance, 0, "/old-abi-app", "compat"),
            ImageSwitch(new_instance, 0, "/new-abi-app", "native"),
        ]
        records = [
            ActionRecord(old_instance, 8, [], action_code=100),
            ActionRecord(new_instance, 8, [], action_code=100),
        ]
        results = normalize_receipts(
            records, tables=interleaved_tables(), image_switches=switches
        )
        self.assertEqual(results[0].action_name, "compat.action.100")
        self.assertEqual(results[0].process.image, "/old-abi-app")
        self.assertEqual(results[1].action_name, "native.action.100")
        self.assertEqual(results[1].process.image, "/new-abi-app")
        self.assertTrue(all(r.is_complete for r in results))


if __name__ == "__main__":
    unittest.main()
